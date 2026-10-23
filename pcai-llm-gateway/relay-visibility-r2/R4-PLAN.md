# DSH r4 — relayed tool calls render as REAL tool-call cards

Everything below is written to be executed **from the deepseek-harness repo root**
(`/home/andrew/Projects/deepseek-harness`), with write permission there. No gateway
(PCAI_LLM) changes are needed — the gateway's Wave-R1 plumbing is complete and its
deployed values already have `toolRelayRenderDeltas: true`; the enrolled DSH key
already carries `stream_pcai_events=True`.

## Context (verified live on this tree)

- HEAD = `f6d211b29d`, zones touched below are clean vs HEAD. Working tree has
  unrelated staged deletions (`ui-content-width`, `session-memory-logger`,
  `apps/cli/package.json`, `tsconfig.host.json`, lockfile) — **do not commit or
  revert anything outside the relay scope**; leave those staged files exactly as
  they are.
- Current r3 behavior (commit `8ac28dfe3e` + `e7a7efaf32`): relayed calls are
  suppressed as tool-call blocks and rendered as synthetic **reasoning**
  tombstones (`[tool] … → ok`) at stream index ≥ 1 000 000. That is why the user
  sees them "wrapped in thinking".
- What a "real tool call" is in DSH: a `tool-call` block streamed as
  `block-start` → `tool-call-delta` → `block-end` by the adapter
  (`packages/llm/llm/src/types.ts` StreamChunk union). The chat UI turns each
  block-end into a `tool/call`-keyed node (`packages/client/ui-chat/src/client/
  conversation-nodes/tool.ts` match on `assistant/live-chunk` `block-end` with
  `block.type === 'tool-call'`), which renders as a Tool row card; the final
  look comes from the settled `tool/result` **or**, when the block carries its
  own `result` string, straight from the block (`call.argsRaw`, `state`,
  `output` all derive in `ui-tool/src/client/tool/models/tool-call-model.ts`).

## The r4 contract (one sentence)

A relay call streams a **real tool-call block** whose `block-end` carries the
block's own `result` string (gateway outcome + preview), the assembler keeps any
result-bearing tool-call block **out of the assembled message** (so the agent
loop can never execute it and replay never pairs it), and the durable stream log
still replays the exact bytes.

Why this is safe (all four verified against the live code):

1. **Agent loop** (`packages/core/agent-loop/src/agent.ts:532`): executes only
   `message.content.filter(block => block.type === 'tool-call')` — the assembled
   content. Result-bearing blocks are dropped at assembly, so no dispatch.
2. **Replay** (`packages/llm/llm/src/assembler.ts` `assembled()` + `interruptedBlocks()`):
   max-token truncation already filters tool calls; we extend the same single
   keep/drop decision to result-bearing ones, in BOTH `blocks()` and the replay
   envelope (`envelope.blocks` pruned in step) and in `interruptedBlocks()`.
   `toPiReplayState` (`packages/llm/llm-pi-ai/src/replay.ts:78`) maps per-block
   from the (filtered) pi-ai message content, so provider replay metadata stays
   index-aligned automatically.
3. **Gateway inbound**: the relay strip (`PCAI_LLM/src/pcai_llm/api/
   routes_proxy.py:2421`) removes only `reasoning_content`/`reasoning` keys from
   inbound messages; since result-bearing blocks never persist into
   `assistant/message` content, nothing relay-shaped is ever re-sent. (Even a
   hypothetical leak is inert: `detect_continuation` only treats assistant
   messages WITH tool_calls + matching role=tool results as a handback, and
   relay calls always have in-cluster results, so no client-side execution is
   implied.)
4. **UI tool rows**: `toolResultSection` in the session-memory-logger plugin
   (`~/Projects/dsh-plugins/packages/session-memory-logger/src/index.ts:129`)
   falls back to `resultText(result)` alone when `calls.get(callId)` misses —
   the gateway's result preview still lands in memory docs. No repo change
   needed there.

## Change 1 — `packages/llm/llm-pi-ai/src/stream.ts` (r3 → r4)

Replace the relay state machine (the block starting
`// PCAI gateway relay (r3):` through the end of `renderTombstone`, plus the
`toolcall_start` / `toolcall_delta` / `toolcall_end` / `done` / default arms
listed below).

New state (keeps the existing maps, drops `nextRelayIndex`):

```ts
type RelayCall = {
  id: string
  name: string
  args: string
  pcai?: { ok: boolean; ms?: number; result?: string }
  opened?: boolean   // block-start + delta emitted
  closed?: boolean   // result-bearing block-end emitted
}
```

New helpers:

```ts
const relayArguments = (call: RelayCall): string => {
  const flat = call.args.replace(/\s+/gu, ' ').trim()
  if (flat.length === 0) return '{}'
  return flat.length > 2000 ? `${flat.slice(0, 2000)}…` : flat
}

/** block-start + ONE full-identity delta, at the STAMPED block's contentIndex. */
function* openRelayToolCall(call: RelayCall): Generator<StreamChunk> {
  if (call.opened === true) return
  call.opened = true
  sawRelayRender = true
  yield { type: 'block-start', index: call.contentIndex ?? 0, blockType: 'tool-call' }
  yield {
    type: 'tool-call-delta',
    index: call.contentIndex ?? 0,
    id: brandString<ToolCallId>(call.id),
    name: call.name,
    argumentsDelta: relayArguments(call),
  }
}

/** Result-bearing block-end; idempotent (telemetry event + done flush never double-emit). */
function* closeRelayToolCall(call: RelayCall): Generator<StreamChunk> {
  if (call.closed === true || call.opened !== true) return
  call.closed = true
  const ms = call.pcai !== undefined && typeof call.pcai.ms === 'number' ? ` (${call.pcai.ms}ms)` : ''
  const outcome = call.pcai === undefined ? 'executed server-side'
    : `${call.pcai.ok ? 'ok' : 'error'}${ms}`
  const preview = call.pcai !== undefined && typeof call.pcai.result === 'string'
    && call.pcai.result.length > 0 ? collapseWhitespace(call.pcai.result, 4000) : ''
  yield {
    type: 'block-end',
    index: call.contentIndex ?? 0,
    block: {
      type: 'tool-call',
      id: brandString<ToolCallId>(call.id),
      name: call.name,
      arguments: relayArguments(call),
      result: `\n→ ${outcome}${preview === '' ? '' : `\n${preview}\n`}`,
    },
  }
}
```

Arm changes inside `toStreamChunks`:

- `toolcall_start` (relay branch): same recording as r3 **plus** set
  `call.contentIndex = event.contentIndex` and `yield* openRelayToolCall(call)`
  immediately (record FIRST, then open, so contentIndex is set). The stamped
  block's own pi-ai index IS the harness block index — the provider's r4 dist
  patch keeps every index original and unstamped (handback) blocks untouched,
  so no synthetic index space is needed anymore.
- `toolcall_delta` (relay branch): unchanged accumulation (`call.args += delta`).
- `toolcall_end` (relay branch): r3 suppressed; r4 keeps suppression of the
  *provider's* end event (`if (relayCalls.has(event.contentIndex)) break`) —
  the result-bearing end comes from the telemetry join below, which fires one
  event LATER in stream order (see r4 dist patch). No change to the unstamped
  arm.
- default (`pcai_event`) arm: on `tool_call`/`tool_error`, join as today, then
  `if (call.opened !== true) yield* openRelayToolCall(call)` (defensive: a
  provider that never streamed the stamped delta still gets a visible row)
  followed by `yield* closeRelayToolCall(call)`.
- `done` arm: flush closure for telemetry-less calls:
  ```ts
  for (const call of relayCalls.values()) {
    if (call.pcai === undefined) yield* closeRelayToolCall(call)
  }
  ```
  Keep the `sawRelayRender` EMPTY_RESPONSE guard verbatim (still needed: pi-ai's
  dist strip removes stamped blocks from the message, so a relay-only `done`
  has zero content blocks).

Update the file-head doc-comment paragraph about "display-only … never reach
the harness as tool-call blocks" to describe r4 (result-bearing blocks dropped
at assembly).

## Change 2 — `packages/llm/llm/src/assembler.ts`: the drop decision

One new predicate + three touch points (all inside `BlockAssembler`):

```ts
/** A relay-rendered gateway call carries its own result text (≤4000 chars);
 *  it must never reach the agent loop's executable message or replay. */
private static isDisplayToolCall(block: ContentBlock): boolean {
  return block.type === 'tool-call' && typeof (block as { result?: unknown }).result === 'string'
}
```

1. In `assembled()`: change
   ```ts
   const kept = this.finish.kind === 'max-tokens'
     ? all.map(block => block.type !== 'tool-call')
     : undefined
   ```
   to
   ```ts
   const kept = this.finish.kind === 'max-tokens'
     ? all.map(block => block.type !== 'tool-call' && !BlockAssembler.isDisplayToolCall(block))
     : all.map(block => !BlockAssembler.isDisplayToolCall(block))
   ```
   (The existing `blocks.length === all.length` replay-passthrough check below
   handles the "nothing dropped" case; when something IS dropped the envelope
   filter already runs — verify that line reads
   `replay: kept === undefined || blocks.length === all.length ? envelope : {…filter…}`
   and still compiles; with `kept` now always defined the first operand is
   simply `blocks.length === all.length`.)
2. In `interruptedBlocks()`: the type gate already excludes tool calls
   (`type !== 'text' && type !== 'reasoning'` returns undefined) — no change.
3. `push()` needs no change: result-bearing blocks arrive via `block-end` and
   are stored like any other; the drop happens only in `assembled()`.

Add one invariant comment near `assembled()`: the agent loop executes every
tool-call block in the assembled message (`agent-loop/src/agent.ts`), so a
display-only tool-call (self-result) must never appear there.

## Change 3 — pi-ai provider dist patch (r3 → r4), file
`patches/@earendil-works__pi-ai@0.87.1.patch` + installed copy at
`node_modules/.pnpm/@earendil-works+pi-ai@0.87.1_patch_hash=8ebfc43950f10ed9a5728f5c415d28abf4b0b232bae6210_68edd6877bf4a92d46200f695c2d1123/
node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js`

Two edits (apply to BOTH the installed dist and the patch file; regenerate the
patch with `diff -u` against the pristine 0.87.1 base, or hand-edit the patch
hunks to match):

**(a) In the telemetry branch** (currently the block at ~line 399 that finalizes
the relay block via `finishBlock(relayBlock)`): after the existing
`if (relayBlock !== undefined && relayBlock.pcaiRelay === true) { … }`, keep
`finishBlock` as-is BUT make the finalized block carry the gateway result. The
cleanest r4 form replaces the r3 "finalize-once guard" comment body with:

```js
if (chunk.pcai.event === "tool_call" || chunk.pcai.event === "tool_error") {
    const relayBlock =
        (typeof chunk.pcai.id === "string" ? toolCallBlocksById.get(chunk.pcai.id) : undefined) || lastRelayBlock;
    if (relayBlock !== undefined && relayBlock.pcaiRelay === true && relayBlock.pcaiFinished !== true) {
        relayBlock.pcaiFinished = true;
        relayBlock.pcaiResult = chunk.pcai.event === "tool_call"
            ? (typeof chunk.pcai.result === "string" ? chunk.pcai.result : "")
            : (typeof chunk.pcai.detail === "string" ? chunk.pcai.detail : "");
        relayBlock.pcaiOk = chunk.pcai.event === "tool_call" && chunk.pcai.ok === true;
        relayBlock.pcaiMs = typeof chunk.pcai.ms === "number" ? chunk.pcai.ms : undefined;
        // r4: hand the harness a REAL tool-call end carrying the block's own
        // result (the gateway executed server-side). The harness stream.ts
        // re-emits its result-bearing block-end from this; the end-of-stream
        // finishBlock below must not duplicate it (pcaiFinished guard).
        stream.push({
            type: "toolcall_end",
            contentIndex: getContentIndex(relayBlock),
            toolCall: relayBlock,
            partial: output,
        });
    }
}
```

Note: `relayBlock.arguments` at this point is still the raw accumulated string
(`parseStreamingJson` runs only in the end-of-stream `finishBlock`), and
`partialArgs` is still present — the harness stream.ts ignores the parsed
arguments on relay ends (it uses its own accumulator), and the final
`finishBlock` loop later re-parses and deletes the scratch fields; the
`pcaiFinished` guard makes that second end emission a no-op **for the stream**,
while `delete block.partialArgs` etc. must still run — so DO NOT `return` from
`finishBlock` when `pcaiFinished` is set for a relay block that already
streamed its end; instead, split the guard: skip the `stream.push(…toolcall_end…)`
but keep the argument-parse/scratch-delete lines. Concretely, in
`finishBlock`'s `toolCall` branch wrap ONLY the final `stream.push` in
`if (block.pcaiFinished !== true)`, and remove the early-return:

```js
else if (block.type === "toolCall") {
    if (block.customInput) { …unchanged… }
    else {
        block.arguments = parseStreamingJson(block.partialArgs);
    }
    delete block.partialArgs;
    delete block.customInput;
    delete block.streamIndex;
    if (block.pcaiFinished !== true) {          // r4: relay end already streamed at its telemetry event
        stream.push({ type: "toolcall_end", contentIndex, toolCall: block, partial: output });
    }
}
```

(The r3 early-return form `if (block.pcaiFinished === true) return;` is what
must be REPLACED by this split — otherwise the scratch buffers never clear.)

**(b) In the end-of-stream strip** (~line 537), keep the existing
`output.content = output.content.filter(… pcaiRelay …)` exactly as-is — it
removes stamped blocks from the message after `finishBlock` ran, which is what
keeps `toPiReplayState` and the EMPTY_RESPONSE guard coherent.

**(c) Add a small append in the same telemetry branch** so the harness sees the
event stream position even when a relay block never got stamped deltas (gateway
`_rendered_delta` always sends them, but be robust): inside the same
`if (chunk.pcai.event === "tool_call" …)` block BEFORE the
`stream.push({ type: "pcai_event" …})` line, nothing more is needed — the
pcai_event forward already exists and stream.ts's defensive `openRelayToolCall`
covers the no-delta case.

Also update the patch header comment (it names r3 cycle breaks) to r4.

Sanity: `node --check` the installed dist file after editing; the patch file
must still apply to the pristine 0.87.1 base
(`pnpm patch` workflow or `patch -p1 --forward` against a fresh extract in
/tmp — /tmp is writable).

## Change 4 — `packages/llm/llm-pi-ai/src/adapter.ts`: stream-events opt-in

In `streamWithSnapshot`, extend the `headers:` line (currently
`headers: requestHeaders(profile.headers)`) so the gateway's per-key stream
events are guaranteed even for keys enrolled before the flag existed:

```ts
headers: requestHeaders({
  // The relay renderer joins gateway telemetry (`{"pcai": …}` events) to the
  // stamped tool-call deltas; a key without the per-key default would strip
  // the events and leave every card "executed server-side" forever.
  'X-PCAI-Stream-Events': '1',
  ...profile.headers,
}),
```

(`requestHeaders` only filters attribution collisions; the casing merge keeps
profile overrides working. `X-PCAI-Relay-Deltas: "1"` stays in the profile
config — it is deployment-owned.)

Update `packages/llm/llm-pi-ai/tests/adapter.spec.ts` "merges profile headers"
only if it asserts the exact header set (it asserts two keys, so it passes
unchanged); add an assertion that `x-pcai-stream-events` is `'1'` in the same
test.

## Change 5 — tests

**Rewrite `packages/llm/llm-pi-ai/tests/stream-relay.spec.ts`** (12 existing
tests) to the r4 expectations:

- Event order for one call:
  `block-start(tool-call, idx)` → `tool-call-delta{full args}` → (telemetry
  event) → `block-end{ tool-call, result: '\n→ ok (2450ms)\n10 hits…\n' }`.
- No reasoning-delta / no `[tool]` text anywhere in relay output.
- Error variant: `result: '\n→ error (80ms)\n'` (+ detail as preview when the
  event carries `detail` instead of `result`).
- Telemetry-less call: block-end `result: '\n→ executed server-side\n'` at done.
- Two calls: two blocks in stream order, each joined by id; duplicate
  `toolcall_end`s still render exactly one card.
- Whitespace collapse + 2000/4000 caps still enforced (relayArguments /
  preview).
- Unstamped handback call: byte-identical to the unpatched mapping (test
  unchanged).
- Relay-only turn still finishes `stop` (EMPTY_RESPONSE guard).
- NEW assembled-message invariant (the core safety property): feed the r4
  chunks through `BlockAssembler` (import from `@deepseek-ai/dsh-llm`) and
  assert `assembler.blocks()` contains NO tool-call block (all relay blocks
  self-result) while an unstamped handback call DOES survive assembly; also
  assert `finish`/`replayState` alignment (`assembler.replayState` stays
  undefined-or-consistent when blocks were dropped).
- Live dist smoke (mirror `tool-argument-streaming.spec.ts`): mock SSE server
  script with a stamped relay delta sequence + `{"pcai":…}` telemetry lines →
  import `stream` from `@earendil-works/pi-ai/api/openai-completions` (vitest
  resolves the patched dist) and assert the patched provider emits
  `toolcall_start`(stamped)/`toolcall_delta`s/`toolcall_end`(with result fields
  on the block) at the telemetry position, and that end-of-stream emits NO
  second `toolcall_end` for the same block.

**`packages/llm/llm/tests/assembler.spec.ts`** (find the existing assembler
spec; add one test): pushing a `block-end` whose block is a tool-call with a
string `result` → `blocks()` omits it; a normal tool-call survives; mixed with
max-tokens finish both filters compose.

## Change 6 — build + verify (exact commands)

```bash
cd /home/andrew/Projects/deepseek-harness

# 1. host lib build (runtime loads lib/index.js, NOT src) — includes tsc -b strict
timeout 900 pnpm run build:lib:host

# 2. unit suites
timeout 300 node_modules/.bin/vitest run packages/llm/llm-pi-ai/tests --configLoader runner
timeout 300 node_modules/.bin/vitest run packages/llm/llm-pi-ai/tests/stream-relay.spec.ts --configLoader runner
timeout 300 node_modules/.bin/vitest run packages/llm/llm/tests/assembler.spec.ts --configLoader runner
timeout 300 node_modules/.bin/vitest run packages/client/ui-chat --configLoader runner
timeout 300 node_modules/.bin/vitest run packages/client/ui-tool --configLoader runner

# 3. patch integrity: pristine-base apply check
git stash push -m "r4-wip" -- patches/ packages/llm   # ONLY these paths, never the pre-existing staged deletions
# (then extract pristine pi-ai, apply patch, compare to installed dist, restore)
```

Caveats from this session:
- `vitest` needs `--configLoader runner` here (EROFS on `.vite-temp` otherwise).
- `pnpm build:lib:host` runs `tsc -b tsconfig.host.json` — the strict gate is
  the real typecheck; fix stream.ts/assembler.ts type errors, do not suppress.
- `node_modules/.vite-temp` writes fail → always use `--configLoader runner`.
- `pnpm` itself is at `~/.dsh/bin/pnpm` (conda ML14 env has node).

## Change 7 — deploy notes (out of repo, recorded for the user)

- The installed `/opt/dsh` tree inside the G2 pod gets the dist+lib files via
  the existing surgery kit (`relay-visibility-r2/surgery/RUNBOOK.md` flow);
  regenerate `work/openai-completions.js` + `work/lib-index.js` from THIS repo
  after the build, bump the kit README (r4 section), and re-run the pod steps.
- The local launcher (`~/.dsh/bin/dsh_sb`, hardcoded `DSH_REPO=/home/andrew/
  Projects/deepseek-harness`) picks the new `apps/cli/lib` + package libs up on
  restart; only the two patched files need syncing there.

## Rollback

Single commit range on top of `f6d211b29d`; `git revert` of the r4 commit
restores r3 byte-identically (the dist patch file ships in-tree). The gateway
is untouched, so no server-side rollback exists or is needed.
