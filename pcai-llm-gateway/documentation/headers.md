# Request headers — deep reference

The data plane is configured **per request** by a small set of `X-PCAI-*`
headers. This document covers the five that shape the **server-side tool relay**
and the **canonical conversation store**, plus the headers they interact with.

It is a consolidated deep-dive: the same facts appear in
[API.md](API.md#L49-L121) (the response-header table and the relay protocol) and
[FEATURES.md](FEATURES.md#tool-use-relay-server-side-agentic-loop) (the feature
rationale). If this document and the code disagree, **the code wins** — every
section links to it.

Verified against the 4.2.1 working tree (2026-11-20).

## At a glance

| Header | Layer | Accepted values | Fallback when absent | Scope |
|---|---|---|---|---|
| `X-PCAI-Execute-Tools` | relay on/off | `1` / `0` only | key's `relay_default` (false) | `POST /v1/chat/completions` |
| `X-PCAI-Compress-Tools` | relay result rewriting | `1`/`on`/`true`/`yes`, `0`/`off`/`false`/`no`, `shadow` | deployment `settings.relay.compress.mode` (code default `off`) | relayed turns only |
| `X-PCAI-Relay-Deltas` | relay stream rendering | exactly `1` | off (also needs a deployment flag) | relayed turns only |
| `X-PCAI-Stream-Events` | relay stream telemetry | `1` / `0` only | key's `stream_pcai_events` | relayed turns only |
| `X-PCAI-Conversation` | conversation bookkeeping | `raw` (case-insensitive) | canonical handling on (fleet switch `canonical_conversations`) | all chat requests |

Three rules apply to all five:

1. **The header always wins over the default it overrides** (key default or
   deployment default), except where a global kill switch sits above both.
2. **A value the header does not recognise is not an error.** It falls back to
   the default (or, for `X-PCAI-Compress-Tools`, to `off`).
3. **They are independent.** Any combination is legal; the only coupling is that
   three of them are read *inside* the relay branch and are therefore inert when
   the relay is off (see §2).

---

## 1. What each header does

### 1.1 `X-PCAI-Execute-Tools` — turn on the in-cluster tool relay

The master switch for the agentic loop. With it on, the gateway **executes the
model's tool calls itself**, in-cluster, against the key's MCP grants, and feeds
the results back — looping until the model answers or a guard trips. Only the
final answer and the `{"pcai": …}` event lines cross to the client, so there is
no client round-trip (VPN hop) per tool call.

**Resolution** ([routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L115-L130)):

1. `1` → relay on, even for a key whose default is off.
2. `0` → relay off, even for a key whose default is on.
3. absent → the key's `relay_default` column (console: Keys ▸ edit).
4. `PCAI_LLM_TOOL_RELAY_ENABLED=false` (global kill switch) wins over all of
   the above; a `1` then silently does nothing.

Only `1`/`0` are accepted (after strip + lowercase). `"true"` is **not** a
value — it falls back to the key default.

**Body opt-in (no header needed).** An OpenAI-idiomatic entry in `tools`
engages the relay for that request
([API.md](API.md#L210-L228)):

```json
{"type": "mcp", "server": "logsearch", "require_approval": "never"}
```

- `require_approval: "never"` (default) — same as the header: execute
  in-cluster. With `server`, the relay narrows to that server's granted tools
  (unregistered server → 400; nothing granted → 400 naming the reason).
- `require_approval: "always"` — **no** server-side execution: the granted
  catalog is exposed as plain OpenAI function tools and the model's
  `tool_calls` are returned untouched for the client to run.
- mcp-type entries are stripped from the payload forwarded upstream, in both
  modes.

An explicit `X-PCAI-Execute-Tools: 0` combined with a body execution request is
a deliberate **400**, never a silent ignore
([routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L5239-L5252)).

**What changes when it is on:**

- **`tools` is injected** from the key's held MCP grants, named
  `<server>__<tool>`; the client's own function tools are still forwarded and
  offered (so the model can hand back to them).
- **The response is always SSE** (`X-PCAI-Relay: streaming`), even if the body
  said `stream: false` — the relay builds its own upstream body with
  `"stream": true`.
- **Preconditions run before the gates**: no MCP grants at all → `400 "tool
  execution requested but this key has no MCP grants"`. That check runs before
  the concurrency increment so a grantless key cannot leak a slot.
- **Guards**: per-turn unit budget (default 16; profile-overridable), wall-clock
  cap (600 s default, clamped to the ingress timeout minus 30 s), identical-call
  loop detection, and a ledger settle for every model call and every executed
  tool call (tool rows are 0-token `mcp:<server>:<tool>` rows).
- **Handback**: a call to a *client* tool (bash/edit, or a name the key is not
  granted) ends the stream with `finish_reason=tool_calls` plus a `handback`
  event carrying a `thread` ref
  ([toolrelay.py](../src/pcai_llm/core/toolrelay.py#L3026-L3043)).
- **Shadow/twin**: a client tool whose name matches a granted gateway tool is
  shadowed by the in-cluster twin, so the model calls the faster implementation
  while the client keeps its own name in the transcript
  ([toolrelay.py](../src/pcai_llm/core/toolrelay.py#L690-L768)).

**Gotchas:** requires MCP grants; applies to `/v1/chat/completions` only;
always answers as SSE.

---

### 1.2 `X-PCAI-Compress-Tools` — deterministic tool-result compression

Opts a **relayed** turn into compression of oversized tool results
([toolcompress.py](../src/pcai_llm/core/toolcompress.py#L141-L157),
[API.md](API.md#L75-L89)).

- **Modes:** `1`/`on`/`true`/`yes` → `on`; `0`/`off`/`false`/`no` →
  `off`; `shadow` → compute and report, rewrite nothing. **Unknown values are
  off, never a 400** — a typo must not fail a turn.
- **Fallback:** the deployment default `settings.relay.compress.mode` (code
  default `off`; a site can ship `shadow` to measure the saving on real
  traffic before enabling).
- **What `on` does:** recognised result kinds (JSON arrays, NDJSON, logs,
  search output, diffs) above 4,096 chars with at least a 15% final saving are
  rewritten by *selection* — error records, statistical outliers, boundary
  context, ERROR/stack-trace/summary lines, first matches per file and changed
  lines survive; redundancy is dropped. Unknown shapes, small results and
  marginal savings pass through untouched.
- **Retrieval:** the original is stored under a content digest (TTL-bound) and a
  marker tells the model to call the synthetic `pcai__retrieve` tool
  (`{"digest", "offset", "limit"}`). That tool is offered only while the mode is
  `on`, so the tool list stays stable and the cached prefix survives.
- **`shadow`** computes and reports the saving via in-stream `pcai.compress`
  events (`kind`/`digest`/`before`/`after`) without rewriting.
- **Cache-safe:** the transform is a pure function of the text (no LLM, no
  network, no second request), so the same bytes in produce the same bytes out
  and a client re-sending history keeps the provider prefix cache warm.
- **Fail-open:** a store failure passes the original through — never a marker
  pointing at an unretrievable digest.
- **Response header:** the effective mode is echoed as
  `X-PCAI-Compress-Tools: on|shadow` (absent = off).

**Scope:** read inside the relay branch only
([routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L5988-L5996)); with the
relay off it is inert, not an error.

---

### 1.3 `X-PCAI-Relay-Deltas` — render server-executed calls as native deltas

During a relayed turn, each in-cluster tool call can be re-emitted as a
**native OpenAI `tool_call` delta** stamped `pcai.relay: true`, so a
render-capable client can show a real tool card for a call it did not make
([toolrelay.py](../src/pcai_llm/core/toolrelay.py#L1549-L1559)).

**Two gates, both required:**

1. deployment flag `settings.relay.renderDeltas` (chart default false), and
2. the request header exactly `"1"`
   ([routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L5672)).

The header alone does nothing unless the site turns the flag on. This is
deliberately conservative: a client that does not understand the stamp would
**execute the call a second time** (the 2026-09-21 double-execution incident).
Rendered deltas start at index `tool_relay_render_index_base` (1000) so they
can never collide with handback deltas, which start at 0; the index cursor is
monotone for the whole response, so multiple iterations cannot fuse two calls
into one card ([toolrelay.py](../src/pcai_llm/core/toolrelay.py#L2955-L2975)).

**Not the same as `settings.relay.streamWireTools`** — that knob emits unstamped,
executable-looking deltas and must stay **false** for clients that execute what
they see ([DEPLOYMENT.md](DEPLOYMENT.md#L355)).

**Scope:** relayed turns only.

---

### 1.4 `X-PCAI-Stream-Events` — the `{"pcai": …}` telemetry lines

Controls whether the relay's in-stream telemetry events are emitted at all:
`thinking`, `iteration`, `tool_call`/`tool_error`, `guard`,
`model_usage`, `compress`, `handback`
([toolrelay.py](../src/pcai_llm/core/toolrelay.py#L1539-L1548)).

- `0` → a byte-clean, OpenAI-spec-compliant SSE stream. This exists because
  strict clients (`@ai-sdk/openai-compatible`, `openai-py >= 2.45`) abort on a
  `data:` line that carries neither `choices` nor `error`.
- `1` → force the events on for this request.
- absent → the key's `stream_pcai_events` default. New keys default **false**
  since the 2026-09-24 consumer inventory; the console and enrollment surfaces
  opt their own keys in explicitly.
- There is deliberately **no global kill switch**: the key default plus this
  header are the whole surface.

Only `1`/`0` are accepted; anything else falls back to the key default.

**Gotcha:** `0` also suppresses the `handback` event, and therefore the
`thread` ref that `X-PCAI-Continue` needs. The `finish_reason=tool_calls`
chunk still arrives, so the client still knows to run the calls — it just has to
re-send the transcript.

**Scope:** relayed turns only (plain chat emits no `pcai` events).

---

### 1.5 `X-PCAI-Conversation: raw` — opt out of the canonical store

The gateway normally fingerprints every conversation and keeps a canonical copy
(`canonical_conversations`, on by default). This header opts the request out of
**all** of it
([routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L4366-L4368),
[#L4476-L4482](../src/pcai_llm/api/routes_proxy.py#L4476-L4482)):

- relay path: no reconcile, no store read/write, incoming messages used as-is;
- plain-chat path: no fire-and-forget capture;
- consequently: no `X-PCAI-Continue` continuation (nothing stored to continue)
  and no feed into the memory distiller.

It does **not** change what the model sees — the canonical store is bookkeeping,
never a rewrite (the design is fail-safe by construction: a bug there can
mislabel a turn but never alter the upstream payload).

Only the literal `raw` does anything; other values are ignored.

**Interaction:** `X-PCAI-Continue` is processed *before* the raw escape
([routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L4289-L4368)), so sending
both means the continue path still runs. A raw turn cannot produce a
continuable thread.

**Scope:** all chat requests (relay and plain).

---

## 2. Combining them

They are independent per-request knobs; sending all five is valid. The real
constraint is **dependency**, not conflict:

| Combination | Result |
|---|---|
| `Execute-Tools: 1` + any of Compress / Relay-Deltas / Stream-Events | works — the intended shape |
| Compress / Relay-Deltas / Stream-Events **without** the relay on | **inert** (not an error): read only inside the relay branch |
| `Compress-Tools: 1` + `Stream-Events: 0` | compression still happens and the response header still reports the mode, but the `pcai.compress` reporting events are suppressed |
| `Relay-Deltas: 1` + `Stream-Events: 0` | **good combo** — rendered deltas are native OpenAI chunks, so a strict client gets tool-call rendering without the non-spec `pcai` lines |
| `Relay-Deltas: 1` alone | inert unless the deployment flag `settings.relay.renderDeltas` is on |
| `Execute-Tools: 0` + `Compress-Tools: 1` | compress is inert; no error |
| `Execute-Tools: 0` + `{type: mcp}` in the body | 400 (loud, with the `require_approval: "always"` pointer) |
| `Conversation: raw` + anything | orthogonal — it only removes conversation bookkeeping; but it disables `X-PCAI-Continue` and memory capture |

A fully-loaded set (valid):

```http
X-PCAI-Execute-Tools: 1
X-PCAI-Compress-Tools: 1
X-PCAI-Relay-Deltas: 1
X-PCAI-Stream-Events: 1
X-PCAI-Conversation: raw
```

`raw` only removes the conversation bookkeeping; drop it if you want
result-only continuation (`X-PCAI-Continue`).

---

## 3. When does a tool call actually go through the gateway?

Two different questions hide in "through the gateway": *executed by* the
gateway, and *routed over* the gateway.

### Executed in-cluster by the gateway (relay on)

- Model calls a **granted `<server>__<tool>`** → the relay executor calls the
  MCP server, re-checking the key's grants, the server's tool policy and the RPM
  window on every call, and settles a 0-token `mcp:<server>:<tool>` ledger row.
- **Gateway-internal synthetic tools**: `pcai_tools` (surface discovery) and
  `pcai__retrieve` (compression expansion) — executed by the gateway itself,
  not an MCP server.

### Not executed by the gateway, even with the relay on

- **Client tools** — a plain OpenAI function tool (bash/edit etc.). The model's
  call ends the stream with `finish_reason=tool_calls`; the client runs it
  locally; the result comes back either as a full transcript or via
  `X-PCAI-Continue`.
- **Ungranted or unknown tool names** — reported to the model as denied, never
  executed (default-deny holds inside the loop).
- **`require_approval: "always"`** — the granted catalog is exposed as plain
  function tools and the model's `tool_calls` are returned untouched for the
  client to execute (human-in-the-loop). The gateway never runs them; if the
  client then calls `/mcp`, *that* call is ACL-checked and settled there.
- **`client__<tool>`** (flag-gated client tool-server) — the gateway brokers a
  long-poll dispatch, but the client executes.

### No relay at all (header absent + key default off, or explicit `0`)

Pure passthrough. `tools` is forwarded upstream untouched, the model's
`tool_calls` come back to the client, and the client executes them however it
likes. The gateway never sees the execution.

### Outside the chat path

- A direct `POST /mcp` call is the gateway's MCP federation surface — ACL, RPM
  and ledger apply there regardless of the relay.
- A client with its **own** MCP connection to the same server (over its own
  network path) bypasses the gateway entirely. This is what the relay's
  shadow/twin logic exists for: the same server is reachable in-cluster, so a
  client tool named like a granted gateway tool is shadowed by its faster twin.
- Non-chat endpoints (`/v1/embeddings`, `/v1/responses`, audio, rerank) never
  relay.

### Summary

| Situation | Who executes | Gateway sees the call? |
|---|---|---|
| Relay on + granted MCP tool | gateway (in-cluster) | yes — ACL, RPM, ledger |
| Relay on + client tool / ungranted name | client | yes (handback), no execution |
| Relay on + `require_approval: "always"` | client | catalog only; execution is client-side |
| Relay off | client | no — passthrough |
| Direct `/mcp` call | gateway (federation) | yes — ACL, RPM, ledger |
| Client's own MCP connection | client | no |

**One-line summary:** the gateway executes a tool only when the relay is
engaged and the call names a tool the key is actually granted; everything else
is either handed back to the client or never executed at all.

---

## 4. Related headers

| Header | Purpose |
|---|---|
| `X-PCAI-Continue: <thread>` | Result-only continuation: send ONLY the new `role=tool` results for a handed-back turn; the gateway stitches its stored canonical list + pending handback state. Requires canonical conversations (503 when disabled); a raw turn cannot continue. |
| `X-PCAI-Session: <id>` | Client-supplied affinity key: pins the conversation to the endpoint that served it (KV-cache warmth) when session affinity is enabled. Absent → the conversation fingerprint is used. |
| `X-PCAI-Stream-Thinking: 0` | Suppress reasoning deltas on a relayed stream (harness clients whose agent loop re-appends whole reasoning blocks). Steps stay traced and rendered. |
| `X-PCAI-Metadata: 1` | Opt a NON-streaming chat response body into the `"pcai": {"served_model", "endpoint", "cost_usd", "cached_tokens"}` block. Without it the upstream bytes are relayed verbatim. |
| `X-PCAI-Model` / `X-PCAI-Duration` | Audio routes: model selection and duration hint (price input). |
| `X-PCAI-Purpose` | Marks auxiliary calls (e.g. session-title requests) so the canonical store skips them. |

---

## 5. Code map

| Concern | Where |
|---|---|
| Relay three-state resolution | [routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L115-L130) |
| Stream-events resolution | [routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L133-L147) |
| Relay pre-flight (grants, 400s) | [routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L5216-L5271) |
| Relay branch + per-request flags | [routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L5658-L5674) |
| Compression resolution | [routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L5988-L5996), [toolcompress.py](../src/pcai_llm/core/toolcompress.py#L141-L157) |
| Relay response headers | [routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L6144-L6150) |
| Canonical raw escape / capture | [routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L4366-L4368), [#L4476-L4482](../src/pcai_llm/api/routes_proxy.py#L4476-L4482) |
| `X-PCAI-Continue` | [routes_proxy.py](../src/pcai_llm/api/routes_proxy.py#L4289-L4368) |
| Relay loop, events, handback | [toolrelay.py](../src/pcai_llm/core/toolrelay.py#L1427), [#L2202-L2222](../src/pcai_llm/core/toolrelay.py#L2202-L2222), [#L2955-L2975](../src/pcai_llm/core/toolrelay.py#L2955-L2975) |
| Shadow/twin logic | [toolrelay.py](../src/pcai_llm/core/toolrelay.py#L690-L768) |
| Settings | [config.py](../src/pcai_llm/config.py#L307-L378), [#L476-L496](../src/pcai_llm/config.py#L476-L496) |
| Public contract | [API.md](API.md#L183-L228), [FEATURES.md](FEATURES.md#tool-use-relay-server-side-agentic-loop) |

---

## Gotchas worth memorising

- `X-PCAI-Execute-Tools` and `X-PCAI-Stream-Events` accept **only** `1`/`0`;
  `"true"` falls back to the default. `X-PCAI-Relay-Deltas` requires the exact
  string `1`.
- `X-PCAI-Compress-Tools` is the only one that tolerates word values and
  unknown values (→ `off`).
- A relayed turn is always SSE, even if the body said `stream: false`.
- `X-PCAI-Compress-Tools` / `X-PCAI-Relay-Deltas` / `X-PCAI-Stream-Events`
  do nothing without the relay — they are not errors, just inert.
- `X-PCAI-Relay-Deltas` needs the deployment flag as well as the header.
- `X-PCAI-Stream-Events: 0` also hides the handback `thread` ref.
- `X-PCAI-Conversation: raw` disables `X-PCAI-Continue` and memory capture.
