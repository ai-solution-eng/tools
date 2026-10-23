# Relay visibility — DSH consumption of PCAI stamped inert tool-call deltas (r2)

Makes gateway-relayed (server-executed) MCP tool calls **visible in DSH** without
executing anything client-side. This is the corrected, fully verified revision of
the `relay-visibility/` kit (r1): the r1 stream mapping dropped the streamed
arguments from the tombstone (`({})` always), removed relay blocks from pi-ai's
content **before** `toolcall_end` emission (shifting the contentIndex of any
following handback block and mis-pairing its `block-end`), and mis-aligned the
replay envelope (dropping replay state on every relay turn). r2 fixes all three
and verifies the result end-to-end.

## Contract implemented

1. Stamped inert tool-call deltas — chunk carries top-level `pcai.relay: true`
   with `delta.tool_calls` whose `index` starts at 1000 (never merges with
   handback deltas, which enumerate from 0). Display-only: marked `pcaiRelay`,
   stripped from the assembled message, never executed, no `finish_reason`
   `tool_calls` — the turn finishes `stop` in the same stream.
2. Standalone SSE data objects (no `choices`) — `{"pcai": {"event":
   "tool_call"|"tool_error", id, tool, ok, ms, result(≤4000 chars)}}`. The
   OpenAI SDK `Stream` JSON-parses every non-`[DONE]` data line with no shape
   validation, so these flow through as `chunk.pcai` (verified in
   openai@6.40.0 `core/streaming.js`).

## What DSH renders

In the reasoning stream, at the call's stream position:

```
\n[tool] mm-rag__search_dataset({"dataset_name":"logs","query":"errors"}) \n→ ok (2450ms)\n10 hits · top score 0.91\n
```

- args: whitespace-collapsed raw streamed JSON, capped at 200 chars (`…` suffix)
- outcome: `\n→ ok (Nms)\n` / `\n→ error (Nms)\n`, joined by call id (fallback:
  tool name, then most-recent relay call)
- preview: one whitespace-collapsed line capped at ~300 chars
- telemetry-less call (event never arrived): `\n→ executed server-side\n`
- unknown `pcai_event` kinds: ignored, never crash
- a relay-only stream yields non-empty reasoning + finish kind `stop` (never
  `EMPTY_RESPONSE`); a genuinely empty stream still maps to `EMPTY_RESPONSE`
- unstamped (handback) tool calls: byte-identical to the unpatched mapping

## Files (apply from the deepseek-harness repo root via `./apply-dsh.sh`)

| Target | Source in kit |
|---|---|
| `patches/@earendil-works__pi-ai@0.85.1.patch` | `work/@earendil-works__pi-ai@0.85.1.patch` — superset of the previous patch (7 old hunks verbatim + 3 new `openai-completions.js` hunks); verified to apply to the pristine 0.85.1 tarball state AND (with `patch(1) --forward`) over an already-patched install |
| `node_modules/.pnpm/@earendil-works+pi-ai@0.85.1_patch_hash=…/dist/api/openai-completions.js` | `work/openai-completions.js` — the live installed copy with the same edits (byte-identical to pristine+patch) |
| `packages/llm/llm-pi-ai/src/stream.ts` | `work/llm-pi-ai-relay-render.patch` (git apply) |
| `packages/llm/llm-pi-ai/lib/index.js` | same patch — the compiled form of the src change (runtime loads `lib`, not `src`) |
| `packages/llm/llm-pi-ai/tests/stream-relay.spec.ts` | `work/tests/stream-relay.spec.ts` — 10 new tests |

Generators (`apply-dist-edits.mjs`, `apply-lib-edits.mjs`) reproduce both work
files byte-for-byte from the pristine baselines in `orig/`.

## The three dist hunks (openai-completions.js)

1. `chunk.pcai && !Array.isArray(chunk.choices)` → push
   `{ type: "pcai_event", pcai, partial }` and continue (before choice handling).
2. Inside the `delta.tool_calls` loop, **after** `ensureToolCallBlock` resolves
   the block: `chunk.pcai?.relay === true` → `block.pcaiRelay = true`.
3. After the `finishBlock` loop (all `toolcall_end`s emitted against original
   indexes): `output.content = output.content.filter(b => !(b.type ===
   "toolCall" && b.pcaiRelay))`.

## The stream.ts mapping

- `toolcall_start` with `pcaiRelay` → record `{id, name, args:""}` in
  `relayCalls`/`relayById`/`relayByName`/`lastRelay`, emit nothing.
- `toolcall_delta` for a relay block → `call.args += delta`, emit nothing
  (pi-ai's patched dist no longer parses args incrementally — `partialArgs` is
  the raw accumulator — so the fragments ARE the argument JSON).
- `toolcall_end` for a relay block → emit nothing.
- `pcai_event` (reached via `default:` — it is outside pi-ai's declared union) →
  join by id/name/lastRelay, then emit into ONE synthetic reasoning block
  (index 1_000_000, far above pi-ai's 0..n): header line, outcome line, preview.
- `done` → flush tombstones for calls without telemetry, map finish reason;
  `EMPTY_RESPONSE` while `sawRelayRender` → `{ kind: 'stop' }`.
- Replay alignment is preserved: relay blocks are removed from pi-ai's message
  before `done`, so `toPiReplayState`'s per-block entries match the emitted
  blocks exactly.

## Runtime consumption (verified)

`@deepseek-ai/dsh-llm-pi-ai` loads **`lib/index.js`** at runtime (package
`exports` → `./lib/index.js`; `toStreamChunks` is inlined there, ~line 1465).
`lib/` is a build artifact of `src/` via `pnpm run build:lib:host` (tsc project
references + tsdown). The repo's vitest resolves workspace imports from **src**
(`vite-tsconfig-paths` wins over package exports), so tests exercise `src/`
while production exercises `lib/` — the kit patches both, byte-consistently.
No network or package installs were needed; `build:lib:host` was NOT run (the
repo checkout is read-only in the verification environment) — the lib patch is
the hand-compiled equivalent, verified by execution.

## Verification performed (r2)

- `tsc` clean on modified `stream.ts` with the repo's strict base flags
  (`strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`,
  `noImplicitOverride`, `noFallthroughCasesInSwitch`, `noUnusedLocals/Parameters`,
  es2024) across the full transitive program.
- vitest: **114 passed** (10 new relay + 81 convert + 2 tool-argument-streaming
  + 21 compat-upgrade), repo config, no failures.
- Live SSE smoke through the patched dist: relay delta stamped → suppressed;
  `pcai_event` joined by id; handback call intact at stable index; indexes 1000+
  never merge with handback 0-based deltas.
- Full-stack smoke through `PiAiAdapter.stream()` (patched lib + patched dist +
  mock gateway): tombstone with full args → ok (2450ms) → preview; executable
  tool-call block-ends contain ONLY the handback call; finish `stop`.
- Patch-file integrity: applies cleanly to the reconstructed pristine 0.85.1
  base (reverse of the old patch over the installed copy); result byte-identical
  to the verified work dist; other 5 files' hunks unchanged; `node --check` OK.
- Repo patch (`llm-pi-ai-relay-render.patch`): applies cleanly, result
  byte-identical to work src/lib.

## r3 — cycle breaks and per-call tombstones (2026-10-02)

Closes the DSH 2026-10-02 stale-panel report: on relayed multi-cycle turns the
single r2 tombstone block swallowed every cycle's tool section AND pi-ai's
single thinking/text accumulators never reset, so one Think panel grew at the
top of the message while the already-final narration below sat stale, and each
tombstone was capped at 200/300 chars inside the thinking stream.

Two coordinated halves, same gating as before (settings flag AND
`X-PCAI-Relay-Deltas: 1`; events additionally need `X-PCAI-Stream-Events`):

- **pi-ai dist (`openai-completions.js`, via `apply-dist-edits.mjs`)** — on a
  `pcai.thinking` event whose `iteration` increases, the open thinking/text
  blocks are `finishBlock`ed mid-stream (thinking_end/text_end at the current
  indexes) and the accumulators cleared, so the next cycle opens FRESH blocks
  at its true stream position (per-cycle Think panels, no welded narration).
  Relay tool telemetry (`tool_call`/`tool_error`) now also finalizes the
  display-only block AT ITS STREAM POSITION (`toolcall_end`, original index);
  `finishBlock` gained a finalize-once guard (`pcaiFinished`) so the
  end-of-stream loop never duplicates the card.
- **Harness adapter (`llm-pi-ai`, via the regenerated patch + `apply-lib-edits.mjs`)** —
  one synthetic reasoning tombstone PER relay call, opened and closed at the
  call's telemetry event (not one ever-growing block); caps raised to
  2000-char args / 4000-char preview (display-only — the gateway strips relay
  reasoning from replay, so nothing model-visible grows). Suppression is
  unchanged: relay calls NEVER reach the harness as tool-call blocks, because
  the agent loop executes every tool-call block in the assembled message
  (`packages/core/agent-loop/src/agent.ts`) and a relay call has no client-side
  result.

Gateway side (this repo): the `[tool] …` reasoning-tombstone text is suppressed
when `render_deltas` is on (`toolrelay._reasoning_trace`) — a delta-rendering
client already shows the call as a card + event preview, and the tombstone text
glued the tool section into the following cycle's thinking block.

`work/llm-pi-ai-relay-render.patch` is a r2→r3 TRANSITION patch: it applies to
the r2-patched installed files (which is how `apply-dsh.sh` consumed it).
Regenerate from pristine with the kit generators if the baselines move.
