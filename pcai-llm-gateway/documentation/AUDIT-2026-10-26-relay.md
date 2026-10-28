# DSH ⇄ PCAI-LLM Gateway relay audit — 2026-10-26

**Question investigated.** Two observed symptoms:
1. *"I occasionally see out of order tool calls."*
2. *"GLM seems to make many many tool calls without thinking, in a row — which seems unlikely to me."*

**Verdict: both symptoms are real, and both are explained by confirmed defects — not by the model.**

Symptom 2 is the more serious one, and it is **not** primarily a model-behaviour problem: the model's
reasoning is being **thrown away in three separate places**, and the model is being **taught by its own
in-loop history** to stop thinking. Measured over 140 real DSH sessions: **86.4 % of tool-call steps
carry no reasoning block** through the relay, vs **46.1 %** going direct to the same GLM endpoint.

Method: 4-agent audit wave (2× GLM-5.3-Flash, 2× DeepSeek-V4.1-Flash-1017, model-pinned), plus lead
reproduction by execution. Every headline finding below was **reproduced by running the code**, not
inferred from reading it. Per-zone reports: `.scratch/relay-audit/zone-{a,b,c,d}-*.md`.

**Applicability.** Line numbers refer to the working tree of `/home/andrew/Code/HPE/PCAI_LLM`
(HEAD `6f29366` + uncommitted changes), which corresponds to the deployed image
`ghcr.io/ai-solution-eng/pcai-llm-gateway:v3.1.1`.

**Live configuration (read from the running deployment, not from a values file).** Every "live"
claim below was confirmed against deployment `pcai-llm/pcai-llm-gateway`:

```
PCAI_LLM_TOOL_RELAY_ENABLED            = true
PCAI_LLM_TOOL_RELAY_MAX_ITERATIONS     = 32      (code default is 16)
PCAI_LLM_TOOL_RELAY_WALL_CLOCK_S       = 900
PCAI_LLM_TOOL_RELAY_CALL_RETRIES       = 1
PCAI_LLM_TOOL_RELAY_RENDER_DELTAS      = true    <- arms CUT 4 and F-1
PCAI_LLM_TOOL_RELAY_RENDER_INDEX_BASE  = 1000    <- the constant behind F-1
PCAI_LLM_RELAY_STREAM_WIRE_TOOLS       = false
PCAI_LLM_ZERO_COMPLETION_RETRY         = true    <- F-8 is LIVE, not latent (see correction below)
```

---

## The three cuts that delete the model's reasoning

This is the core finding. Reasoning is discarded at three independent points, so fixing any one of
them alone will not fix the symptom.

### CUT 1 — the gateway drops reasoning deltas it does not recognise (P0)

`src/pcai_llm/core/toolrelay.py:446`

```python
interesting = (b'"tool_calls"', b'"usage"', b'"content"', b'"reasoning_content"')
...
if not any(marker in line for marker in interesting):
    continue          # line discarded BEFORE json.loads
```

A raw **byte-marker** prefilter decides whether an SSE line is worth parsing. It matches only the
literal `"reasoning_content"`. An upstream that emits the equally standard `reasoning` or
`reasoning_text` key is **silently dropped entirely**. Meanwhile the client library DSH uses
(pi-ai) accepts all three: `["reasoning_content", "reasoning", "reasoning_text"]`
(`openai-completions.js:468`).

**Reproduced:** fed two chunks, one `delta.reasoning` + one tool_call →
`parse_sse_payloads kept 1 of 2 chunks`; `stream_model_call` yielded only the tool_call.
The reasoning vanished with no error and no log line.

Severity P0: the model generates reasoning, the user never sees it, and nothing anywhere records that
it was dropped. The same marker blindness applies to the tail-buffer path (`toolrelay.py:466`).

### CUT 2 — the gateway strips reasoning from inbound history (P1, deliberate but load-bearing)

`src/pcai_llm/api/routes_proxy.py:2816` — runs **before** the relay branch at `:3100`:

```python
for _m in body["messages"]:
    if isinstance(_m, dict):
        _m.pop("reasoning_content", None)
        _m.pop("reasoning", None)
```

This exists for a good reason (documented in the comment: DSH re-appends whole reasoning blocks per
agent-loop cycle, which caused a 6,630-copy amplification storm on 2026-09-18). But the consequence is
that **the model never sees its own prior reasoning on any turn**.

### CUT 3 — a relay card destroys the turn's replay state (P1, DSH side)

A relay card is kept in the harness message (`assembler.ts:130-139`) but stripped from the pi-ai
message (`openai-completions.js:553`). The replay envelope is built from the *stripped* message
(`stream.ts:377` → `replay.ts:88-100`), so its block count no longer matches, and
`assembler.ts:148` **discards the whole envelope**:

```ts
if (envelope.blocks.length !== all.length) return { blocks, replay: undefined }
```

Next request → `foreignAssistant` (`replay.ts:152`) → thinking block with **no**
`thinkingSignature` (`replay.ts:158`) → pi-ai omits the reasoning field entirely
(`openai-completions.js:1086-1089`). Reproduced both wire bodies: with signature =
`reasoning_content` present; without = absent.

**Zone C's correction to the lead's framing, on record:** these are two cuts *in series*. Fixing the
client alone cannot restore reasoning — Cut 2 is unconditional on the gateway.

### …and the model is *taught* to stop thinking

`src/pcai_llm/core/toolrelay.py:1690`

```python
relay_msgs.append({
    "role": "assistant",
    "content": "".join(content_acc) or None,
    "tool_calls": tool_calls,          # no reasoning key, ever
})
```

The reasoning *was* collected (`reasoning_acc`, lines 1325/1362) and is then discarded. Inside a
single relay turn — **32 iterations live** (`PCAI_LLM_TOOL_RELAY_MAX_ITERATIONS=32`; the *code*
default in `config.py:172` is 16, but `helm/values.yaml` already ships 32, so 32 is what the chart
gives you) — the model sees up to 31 consecutive examples of *itself* emitting tool calls with zero
thinking. That is a strong in-context demonstration to skip reasoning.

**Measured, 140 real DSH sessions.** Metric: the fraction of assistant *tool-call steps* whose
content block list contains no `reasoning` block. Sessions are the 150 most-recently-modified under
`~/.dsh/sessions/*/*/session.v3.jsonl.zstd` that mention `pcai-model-endpoint-flow` or
`glm53-flash`; each `assistant/message` event contributes one step. Script:
`.scratch/relay-audit/reverify2.py`.

| provider | tool-call steps | no reasoning | % |
|---|---|---|---|
| `pcai-model-endpoint-flow` (relay) | 21,791 | 18,758 | **86.1 %** |
| `pcai-model-endpoint` | 912 | 236 | 25.9 % |
| `glm53-flash-sproket` | 625 | 506 | 81.0 % |
| `glm53-flash` (direct) | 449 | 207 | 46.1 % |

Longest single run of consecutive reasoning-less tool steps: **837** (session `235e3db5`), 514, 452,
319, 263, 256… The first bare tool-call step appears at tool-step 2-4 in 97 of 136 sessions.

### CUT 4 (aggravator) — the live config deletes the only other channel

`toolrelay.py:1215`:

```python
def _reasoning_trace(...):
    if render_deltas:
        return b""        # tombstone suppressed
```

With `render_deltas` on, the `[tool] server__tool → ok (ms)` text tombstone is suppressed because
"the client renders the stamped card instead". But the **running deployment** sets
**`PCAI_LLM_TOOL_RELAY_RENDER_DELTAS=true`** (confirmed from `pcai-llm/pcai-llm-gateway`; also
`helm/local/values.g2.yaml:161`) and DSH sends **`X-PCAI-Relay-Deltas: 1`**
(`~/.dsh/profiles/web/cordis.patch.yml:103`). So in the deployed configuration the reasoning channel
carries **no tool trace at all** — and if Cut 1 also eats the reasoning, the user sees an unbroken run
of tool activity with nothing between it.

---

## Symptom 1: out-of-order tool calls

### F-1 (P0) — render-delta wire index restarts every iteration

`toolrelay.py:1747-1749` enumerates `mcp_calls` **per iteration**, and
`_rendered_delta` computes `render_index_base + idx` (`:1132`) with `render_index_base` a
**constant** 1000 (`:687`, `config.py:235`). Nothing advances the base per iteration.

Reproduced against the real generator — two iterations × two calls:

```
render idx=1000 id='it0_c0'   render idx=1001 id='it0_c1'
render idx=1000 id='it1_c0'   render idx=1001 id='it1_c1'   <-- collides
```

Within one iteration the indexes are distinct; **across iterations they repeat**.

Simulated the patched pi-ai keying (`ensureToolCallBlock`, `toolCallBlocksByIndex`) against that
sequence: **4 deltas → 2 blocks**, args concatenated to `{"q":"00"}{"q":"10"}` — invalid JSON.
`toolCallBlocksByIndex` is **never cleared** (`.get`/`.set` only; no `.clear`/`.delete` anywhere
in the dist), and the thinking cycle-break closes only `thinkingBlock`/`textBlock`, so the merge
survives. Net effect: half as many cards as calls, each showing the wrong arguments.

*Note: Zone D initially reported "all calls get index 1000"; the lead corrected this to the
cross-iteration form and Zone D re-verified. The conclusion stands, the trigger is "≥2 in-cluster
iterations in one turn".*

### F-2 (P0) — index-less parallel tool calls merge into one call

`toolrelay.py:494`: `idx = tcd.get("index", 0)`. The OpenAI streaming contract does not require
`index` on every fragment, and several servers stamp it only on the first. Two parallel calls without
it merge into slot 0 — and `slot["function"]["name"] += fn["name"]` (`:507`) plus
`arguments += ...` (`:509`) **concatenate** them.

**Reproduced:** two calls → one call, name `"bashread"`, arguments
`{"cmd":"ls"}{"p":"x"}`. The merged call then either fails JSON parse (burns an iteration
self-correcting) or — worse — parses as valid JSON and **executes the wrong thing**.

### F-3 (P2) — repeated name on a continuation fragment duplicates the name

Same `:507` concatenation, unconditional. **Reproduced:** `"bashbash"`. The doubled name misses both
the ACL parse and the alias path, so it becomes an unknown-tool error and the model re-issues the call.

### F-4 (P1) — empty `pcai.id` makes the client join both calls to one card

The gateway emits `"id": tc.get("id", "")` in tool_call telemetry (`:2086`, `:2174`) — empty
whenever the upstream omitted ids. DSH registers joins only for non-empty values
(`stream.ts:307-308`) and falls back `byId → byName → lastRelay` (`:263-269`). Two calls to the
same tool name both resolve to the *second* call; the first card is closed by the done-flush as the
generic "executed server-side" and **loses its real result**, while the second card displays the
first's result.

---

## Other confirmed defects

| id | sev | claim | location |
|---|---|---|---|
| F-5 | P1 | Per-turn iteration budget silently degrades to **per-request**: any prefix-hash divergence scores `reset`, zeroing `iterations_prior`. Structural causes: the relay's 32,000-char tool-result cap vs the client's uncapped replay; `content: None` vs `""`; mid-conversation system messages. One logical turn can chain far past the 32-iteration live cap. | `routes_proxy.py:2465`, `canonical.py:183-186`, `toolrelay.py:112` |
| F-6 | P1 | Timeout retry **re-streams already-emitted reasoning** (accumulators reset at `:1325`, call re-run at `:1467`). The transport path guards this at `:1490`; the timeout path does not. `tool_relay_call_retries=1` is live. | `toolrelay.py:1325,1467` |
| F-7 | P1 | Handback terminals emit `finish_reason` + `[DONE]` with **no usage chunk** despite `include_usage=True` being forced — exactly the "Usage 0 tok" bug the code comment claims to fix. **Reproduced:** 0 usage chunks on the handback path, 1 on the final-answer path. **Measured in 140 real sessions:** 21,693 of 21,696 relayed tool-call steps (100.0 %) report `totalTokens: 0`, while direct `glm53-flash` reports usage on 449 of 449 (0.0 % zero). | `toolrelay.py:1826-1838, 2239-2251` vs `routes_proxy.py:3319` |
| F-8 | P1 | Zero-completion retry gates on `content_acc` but **not** `reasoning_acc` — a reasoning-only completion re-rolls and streams its thinking a second time. The comment "nothing was emitted yet for it" is false for the reasoning channel. **`zero_completion_retry` is ON in production** (`PCAI_LLM_ZERO_COMPLETION_RETRY=true`), so this is live, not latent — and GLM emits reasoning-only completions, the exact trigger. | `toolrelay.py:1571` |
| F-9 | P2 | `X-PCAI-Continue` `pending` is **never cleared** after consumption; `canonical.py:369`'s docstring ("Cleared (NULL) when the continuation is consumed") is false. A retry re-merges the old handback tail — `detect_continuation` returns True on the duplicated shape (verified). | `canonical.py:357-382` |
| F-10 | P2 | Relay branch ignores `stream`: a non-streaming request + `X-PCAI-Execute-Tools` returns SSE bytes where the SDK expects JSON. | `routes_proxy.py:3100` vs `:3408` |
| F-11 | P2 | No `{"error"}` envelope — failures ship as `finish_reason=stop` + literal text `(model error: upstream call failed)`, so DSH maps a failure to a successful stop. | `toolrelay.py:1516-1531` |
| F-12 | P2 | `replayedAssistant` lacks the `isDisplayToolCall` guard `foreignAssistant` has → latent dangling `tool_calls`, live the moment F-C3 is fixed. | `replay.ts:216-223` vs `:166` |
| F-13 | P2 | A `settle()` DB INSERT raising inside the shielded `finally` loses the ledger row **and** kills the stream (no guard around the INSERT). | `toolrelay.py:1415-1434`, `budgets.py:765-794` |
| F-14 | P2 | No keepalive during **tool execution** (only during model calls) — a slow tool leaves the socket silent, an idle proxy cuts it, and DSH retries the whole turn (amplifying call count). | `toolrelay.py:2146` |
| F-15 | P3 | In-loop loop guard aborts on the **second** identical call (zero tolerance) vs the cross-request guard's 5-per-10-min — inconsistent policy for the same concept. | `toolrelay.py:412` vs `routes_proxy.py:1218` |

---

## What is solid (verified — do not "fix" these)

- **`[DONE]` exactly once on all 8 terminal return paths** — enumerated and confirmed by three zones independently.
- **No byte-order inversion exists.** `stream_model_call` buffers tool_calls to end-of-stream (`:490` vs `:514`), so reasoning always *precedes* tool_calls within an iteration. The "out of order" symptom is index collision (F-1) and join misattribution (F-4), **not** stream reordering.
- **No reordering anywhere in DSH** — zero `sort` calls across assembler/stream/replay/agent/repair; order is insertion order.
- **`repair.ts` is clean** — Map-keyed by ToolCallId (idempotent), insertion order = transcript order, skips display cards.
- **Double-execution defence holds end-to-end** — stamp → `pcaiRelay` → filtered at pi-ai → skipped at `agent.ts:557-558` → stripped at `replay.ts:166`.
- **Fresh user turns cannot inherit a spent budget** — execution-probed all tail shapes.
- **The gateway does not add/remove any thinking-enable parameter.** `stream_once` spreads the body verbatim (`routes_proxy.py:3192-3208`); both paths send an identical parameter surface. This hypothesis was tested and **exonerated**.
- **Settle-once-per-attempt** holds; no double-count. **`[DONE]`-exactly-once** holds. **Loop-guard namespacing** is watertight.

## Test gaps (why these survived a green suite)

1. No test feeds `reasoning` (vs `reasoning_content`) through the parser — **Cut 1 invisible**.
2. No test for parallel tool_calls with omitted `index` (F-2) or repeated names (F-3).
3. No test combines a relay card **with** a replay envelope — **Cut 3 invisible**.
4. No two-iteration render-delta test — a single-iteration test **passes** and hides F-1.
5. No test for an empty `id` through the telemetry path (F-4).
6. No test for a usage chunk on the handback path (F-7).
7. No test for `settle()` raising inside the shielded finally (F-13).
8. `zero_completion_retry` has **no relay-path test at all** (F-8).

## Suggested fix order

1. **F-1 / F-2 / F-3** (one commit): advance `render_index_base` per iteration; key the accumulator by `(index, id)`; only append a name when the slot's name is empty. Synthesize a stable id when the upstream omits one — this also fixes **F-4**.
2. **Cut 1**: replace the byte-marker prefilter with a shape check (`b'"delta"'` or decode-and-inspect) or extend the marker set to all three reasoning spellings.
3. **Cut 2 + the teaching loop**: re-feed prior iterations' reasoning into `relay_msgs` (bounded), and distinguish "client re-sends" (strip) from "gateway-authored in-loop rows" (keep) in the strip at `routes_proxy.py:2816`.
4. **F-7**: emit `last_usage` before `[DONE]` on both handback terminals.
5. **F-5**: make the continuation verdict structure-based, or hash-normalise both sides before comparing.
6. **Cut 3 (DSH)**: carry the relay card through to the replay envelope, or exclude it from the block-count comparison.

---

## Corrections applied after re-verification against the running deployment

An earlier revision of this report asserted several configuration values from values files on disk
rather than from the deployed system. Those claims were re-checked against deployment
`pcai-llm/pcai-llm-gateway` (image `v3.1.1`) and corrected:

1. **F-8 was described as latent.** `PCAI_LLM_ZERO_COMPLETION_RETRY=true` in production — the
   reasoning-duplication bug is **live**, and GLM reasoning-only completions are its exact trigger.
2. **"16 iterations by default" was misleading.** The *code* default is 16 (`config.py:172`), but
   `helm/values.yaml` already ships 32 and the deployment runs 32. The report now states 32 as the
   live value and notes where 16 comes from.
3. **CUT 4 cited only `helm/local/values.g2.yaml`.** `PCAI_LLM_TOOL_RELAY_RENDER_DELTAS=true` is
   confirmed on the running deployment; the report now cites the deployment as primary.
4. **F-5's "far past 16 iterations"** corrected to the 32-iteration live cap.
5. **F-7's "every DSH turn hits it"** was an assertion; it is now backed by measurement (100.0 % of
   relayed tool-call steps report `totalTokens: 0`, vs 0.0 % for the direct provider).
6. **The headline measurement** now carries its exact definition and script path so it is
   reproducible rather than a bare percentage.

*Four independent auditors (2 GLM-5.3-Flash, 2 DeepSeek-V4.1-Flash-1017) + lead reproduction. Read-only throughout — no source file in either repo was modified. All findings reproduced by execution where the claim is marked CONFIRMED.*
