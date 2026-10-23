# Roadmap — Candidate Ideas

**Status: brainstormed, NOT committed scope.** Nothing here exists in code;
items are listed so the next design conversation starts from the real
constraint, not from scratch. Order is loose.

## 0. Shipped 2026-09-24 (promoted out of this backlog — see CHANGELOG)

Four items from the "essential add-ons" review are no longer candidates:

- **Custom Prometheus metrics** — SHIPPED (core/metrics.py + 12 call sites
  + prometheusrule.yaml). This unblocks the latency-aware routing idea in
  §4's spirit: `gateway_ttft_seconds{model}` is the per-model TTFT signal
  a weighted-selection sketch wanted.
- **Admin mutation audit trail** — SHIPPED (admin_audit + core/audit.py +
  GET /admin/audit + console tab; 33 instrumented actions). The usage
  ledger stays the data-plane artifact; this is the control-plane twin.
- **Chart network isolation** — SHIPPED (networkpolicy.yaml +
  authorizationpolicy.yaml; g2 example OFF, hosted-trial ON, per the
  fleet chart convention).
- **Per-key `stream_pcai_events`** — SHIPPED (RFC-PER-KEY-STREAM-EVENTS:
  per-key flag default-ON + X-PCAI-Stream-Events override). Its §5
  "later" item — flipping the DEFAULT to FALSE after a consumer inventory
  (console UI, DSH, OpenWebUI-style frontends) — remains an OPEN decision
  and is deliberately NOT scheduled: it changes what "OpenAI-compatible"
  means for unknown clients out of the box.
- **Ledger partitioning** — SHIPPED as a run-once path
  (scripts/partition_ledger.py + startup checker + schema.py note): the
  ledger is created unpartitioned (zero change to today's approach — the
  budget engine's ledger-SUM design is untouched), converts to monthly
  RANGE partitions ONLY when the operator runs the script, and the
  budget gate PRUNES afterwards (verified end-to-end on a live Postgres).
- **Retention policies (§5 below)** — SHIPPED (retention_trace_days /
  retention_conversation_days, both default 0 = disabled, riding the
  registry-refresh housekeeping hook; the ledger is never pruned).
- **Caller-aware /v1/models (§6 below)** — SHIPPED (pcai.type +
  pcai.primary on every registered entry; the console's existing
  rendering lights up with zero UI change).

Still open from this review: the per-model off-peak rules (§1) and the
live-ACL-grant question (§3) remain the next design conversations.

## 1. Per-model off-peak rules

Today the off-peak evaluator samples ONE fleet-wide PromQL and every
discounted request bills at one fraction (profile or env default). That is
the wrong shape when models live on different hardware: GLM-4 on the
dedicated GPU node sits idle overnight (discount is correct), while Qwen on
the shared node stays busy (the fleet-wide average says "off-peak" and
under-charges a model that is still contended).

Sketch: an `offpeak_rules` table — (model match, promql, enter/exit pct,
fraction) — with the evaluator resolving each rule independently and
`settle` stamping the fraction of the rule matching the SERVED model. The
env knobs become the fallback rule. Open question: how many PromQL
round-trips per tick are acceptable through the MCP path (batch, or
per-rule cadence).

## 2. Auto-provisioned per-key memory datasets

The per-key MCP credential override exists precisely so each tenant can
address its own memory dataset — but today an admin must create that
dataset by hand, then paste the `X-Memory-Dataset` / `X-Dataset-Password`
headers into the key's creds dialog. The natural close of the loop: on
enrollment (or on first redeem of a profile that declares "wants memory"),
provision a private dataset and stamp the creds automatically.

Blocked on verification, not design: the MultimodalRAG dataset
create-with-password API needs a reachable, authenticated call path from
the gateway, a naming convention (`<tenant>-<user>` or per-key UUID), and
a decision about who may ever delete the dataset (the gateway must not
become a second dataset admin UI).

## 3. ACL-grant admin API beyond profiles

MCP grants today are per-key (replace-all) or preset-stamped onto keys at
profile-creation time. Both miss the operational case: tightening a
sensitive server's tools across EVERY existing key of a profile (or the
fleet) is N console clicks today. Sketch: either make profile presets
LIVE (grants evaluated from the profile at call time, with per-key rows as
exceptions) or a bulk-grant endpoint over a profile/key filter. The first
changes the trust model (revoking a profile grant must then affect running
keys immediately — desirable, but it must be an explicit decision).

## 4. Final-answer token streaming inside the relay

The relay's final answer is currently emitted as ONE chat.completion.chunk
— by design: the chat console renders tool steps and thinking live, and a
single final chunk keeps the "what actually served" meta handling simple.
Interleaving streamed final-answer deltas with the step events is the
OpenAI-native shape clients expect, so this is worth revisiting when a
non-console consumer of the relay shows up; it requires coordinated UI
changes (the rAF batcher and the meta-chip logic both assume one final
payload).

## 5. Retention policies for traces + conversations

The usage ledger is the audit artifact — kept forever, by intent. The
operational tables are not: `agent_traces` grows one row per relay step and
`conversations`/`conversation_messages` hold full transcripts per key, none
of them pruned today. On embedded-Postgres units that insert volume is also
WAL volume, so the PVC and archive story feel it first. Sketch:
opportunistic TTL prunes (same pattern as the `rate_windows` prune) — e.g.
traces 30 days, conversations configurable per deployment — plus a
documented ledger-retention knob for operators who DO want the ledger
bounded. Nothing is deleted until the policy exists and is off-by-default.

## 6. Caller-aware `GET /v1/models` (documented intent)

FEATURES.md documents the intended shape — key-visible models ordered by
the profile cascade with `pcai.primary`/`pcai.type` metadata — as
planned-not-shipped; the current endpoint returns a flat name list. The
chat UI already renders both fields, so this is backend-only work that
lights up existing UI.

## 7. Server-owned conversations: the endpoint has the conversation,
   the client serves tools to it

Framing (Andrew, 2026-09-18): the proxy should be as transparent as
possible for the user while keeping tool calls as close to the model as
possible — ideally the model endpoint "has" the conversation and the
client machine is just where some of its tools live. What makes this
more than phrasing: today the CLIENT drives the agent loop and re-sends
the whole conversation every handback cycle (a DSH relay turn measured
214k input tokens; a 32-request/71-step conversation fragmenting into
32 trace records). The inversion:

- **Canonical conversation state moves server-side.** The gateway keeps
  the authoritative message list (the `conversations` /
  `conversation_messages` tables already exist; `thread_id` already
  groups traces per conversation). Client handback continuations shrink
  from "full transcript re-send" to "result of call X" + session ref.
  One conversation = one continuous trace with every step.
- **The upstream receives a gateway-controlled, bounded context.**
  0.8.1 already strips inbound `reasoning_content`/`reasoning` (streamed
  out for review, never sent back in — thinking is per-turn scratch) and
  truncation/summarization of old history slots into the same choke
  point: "what the model sees per call" becomes an enforced, auditable
  policy, not whatever the client chose to resend. A hard per-request
  context budget turns "don't send too much" into an invariant.
- **Stable, append-only history = prefix caching.** vLLM/SGLang APC
  wants a byte-stable prefix; client-driven resends perturb it every
  cycle (reasoning echo, reordering), collapsing hit rates. Server-owned
  append-only history makes each cycle's re-prefill mostly a cache hit —
  cheaper per turn, and the ledger stops charging for re-computed
  context.
- **Client tools become remote tools over the existing /mcp
  connection.** `bash`/`edit` stop being protocol handbacks and become
  ordinary tools the relay invokes DOWN the connection the client
  already holds (the gateway cannot reach an off-cluster laptop; the
  client dials in and serves). Trust stays honest: the model requests,
  the client's local approval policy still gates every execution.
  The model's tool schema is identical whether a tool executes
  in-cluster or on the client.

Costs chosen deliberately: (1) loop control (iterations, wall clock,
budget, the bounded retry) becomes gateway-enforced across the WHOLE
conversation — a stuck client tool call consumes relay budget and needs
the same bounded-call treatment model calls got. (2) The gateway becomes
per-conversation stateful — Postgres-backed so replicas still share it,
but handback continuation must stick to the conversation owner (the
conversations feature already works this way). (3) The stateless-any-
replica property degrades to stateless-per-conversation — acceptable,
worth saying out loud.

Relationship to existing pieces: bounded relay retry (0.8.1), inbound
reasoning strip (0.8.1), cross-request loop guards, trace `thread_id`,
and the conversations tables are all prerequisites that already shipped.
The genuinely new artifacts: result-only continuation protocol
(session ref + tool result, versioned), the client-side tool-server
plugin (DSH profile, local approval gating), per-conversation routing
stickiness, and the enforced context budget.

End state, in Andrew's words: "the model endpoint has the conversation;
my machine is just where some of its tools live."

### § 7 phase 2 wave B — client tool-server: protocol design (2026-09-18)

Wave A shipped the substrate: canonical conversations (core.canonical),
handback-continuation detection (detect_continuation), and trace linkage
across the handback boundary. This wave designs — and spikes end-to-end —
the inversion the roadmap actually names: client tools (`bash`, `edit`)
stop being handbacks and become ORDINARY TOOLS the relay invokes, with
results returning INTO the running loop. The stream never ends in
`finish_reason: tool_calls` for a client tool again; the client's local
approval policy still gates every execution (the model requests, the
machine decides).

**The core problem.** The gateway cannot reach an off-cluster laptop. The
client dials IN to `/mcp`. Dispatching a tool call DOWN to the client
needs a reverse channel over the client↔gateway connection the client
already holds. Three shapes were evaluated against the INSTALLED SDK
(mcp 2.2.0, .venv — file/line evidence below, not vibes).

**Shape (a) — MCP sampling/callback inversion: REJECTED, SDK evidence.**

The idea: DSH answers server→client JSON-RPC requests over `/mcp`, i.e.
registers itself as a tool servant, and the relay dispatches `bash` as a
server→client request. Three independent blockers in the installed SDK:

1. *No request primitive that can carry a tool dispatch.* The wire
   surface map (`mcp_types/methods.py`, `SERVER_REQUESTS`) is closed per
   protocol version: at 2025-11-25 it admits exactly `ping`,
   `roots/list`, `sampling/createMessage`, `elicitation/create`; at
   2026-07-28 it is EMPTY — "the modern protocol forbids server-initiated
   JSON-RPC requests" (`mcp/server/runner.py`,
   `_NoServerRequestsDispatchContext`, which wraps every loop-era context
   to force `can_send_request=False`). A custom method would need
   `parse_server_request` surface surgery inside `mcp_types` (an
   SDK fork) AND a client-side `_on_request` route
   (`mcp/client/session.py` routes only the typed union: sampling /
   elicitation / roots) — so DSH could never receive it without patching
   the client SDK too. Abusing `sampling/createMessage` (a chat prompt)
   to smuggle a `bash` invocation corrupts the one spec'd semantics the
   client legitimately implements (LLM sampling) — rejected on principle.

2. *The gateway's `/mcp` is stateless by fleet decision.* mounted with
   `stateless_http=True, json_response=True` (routes_mcp.mount_server) —
   any replica serves any request. In that mode the session manager
   builds a fresh transport PER REQUEST (`_handle_stateless_request`)
   whose dispatch context is hard-wired `can_send_request=False`
   (`_SingleExchangeDispatchContext`, `mcp/server/_streamable_http_modern
   .py:99`), `Connection.from_envelope` installs `_NO_CHANNEL`, and the
   docstring says it outright: "a server-initiated request tied to the
   call raises `NoBackChannelError` ... its notifications are dropped".
   A server-initiated request would additionally have to survive into a
   LATER request's handling (the tool call arrives minutes after the
   last POST) — there is no session to carry it.

3. *Even a stateful flip buys nothing for the relay.* Stateful mode +
   SSE responses + a standalone GET stream (`_request_streams
   [GET_STREAM_KEY]`) can deliver server→client requests, but (i) the
   client SDK only starts its GET stream after `notifications/
   initialized` when a session id was issued (`streamable_http.py`
   post_writer → `start_get_stream`, and `handle_get_stream` returns
   immediately when `self.session_id` is None — i.e. stateless servers
   never get one), (ii) JSON-response mode drops any related-to-request
   message with no wire form (`message_router`: "Dropped message related
   to request ... in JSON mode"), and (iii) it sacrifices the
   any-replica property (session ids pin a replica; the load balancer
   must become sticky) for the whole federation surface to serve ONE
   feature. Cost/benefit inverted: the whole fleet's transport posture
   would change to avoid a long-poll endpoint.

The evidence also sets the long-term boundary: when the MCP spec (or the
SDK) admits generic server→client requests over streamable-http with a
stateless posture, shape (a) becomes the clean replacement and the
dispatch endpoints below are deleted — the relay-side executor contract
(dispatch → result/error) survives unchanged.

**Shape (c) — client-as-registered-MCP-server via loopback tunnel:
REJECTED.** The client hosts a real local MCP server; the gateway
registers it as an upstream `mcp_servers` row reached through a tunnel
the client keeps open. Rejected without a spike: it needs tunnel
infrastructure (reverse tunnel with auth + reconnection + lifecycle)
that is a product in itself, it silently routes tenant code execution
through the cluster's registry/admin plane (an admin grants "a server",
and the endpoint is someone's laptop), and the failure modes (tunnel
flap, NAT rebinding) are exactly the class the relay's bounded-call
work exists to prevent. Shape (b) reaches the same trust boundary with
zero new infra.

**Shape (b) — long-poll dispatch over the SAME bearer key: CHOSEN.**

Transport-agnostic, no SDK surgery, no schema (in-memory state; see
persistence boundary below). The client, knowing it offers local tools,
holds a second HTTP connection to the same gateway with the same key.
The relay enqueues a dispatch job when the model calls a client-offered
tool; the long-poll returns it; the client executes (its local approval
policy decides), POSTs the result; the relay's asyncio future resolves
and the loop continues. Latency is one client round trip (sub-second on
a VPN where the /mcp calls already run); delivery-once is enforced by
the server (a job is claimable exactly once); offline clients surface
as tool errors after a bounded timeout — never a hang.

#### Endpoints (all under the existing bearer auth; router `routes_dispatch.py`)

Every route requires the proxy-scope bearer key (`require_scope("proxy")`)
— the SAME key that talks to `/v1/chat/completions`. The key's identity
(key_id) scopes the entire surface: an offer is visible only to
dispatch jobs minted for the same key, and a job's result is acceptable
only from the key that offered the tool.

1. `POST /v1/dispatch/register` — the OFFER.
   Body:
   ```json
   {
     "tools": [
       {
         "name": "bash",
         "description": "Run a shell command on the user's machine",
         "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}},
         "timeout_s": 300
       }
     ],
     "ttl_s": 900
   }
   ```
   Semantics: registers (replaces — re-register is an UPDATE, never a
   duplicate) the key's tool-server identity; returns
   `{"client_id": "<uuid>", "expires_at": "...", "tools": ["bash", ...]}`.
   `client_id` is derived per (key_id) — one offer per key, latest wins.
   Tool names must match `^[a-zA-Z0-9_-]{1,64}$`; schemas are bounded
   (≤ 64 tools, ≤ 32 KiB each) with 400/413 mirroring the conversations
   error contract. The offer is ALSO accepted as a capability statement
   on the relay path (below) so the model's tool schema is identical in
   shape to any MCP tool's. TTL default 900 s, clamp [60, 86400]; a
   re-register extends. There is NO deregister endpoint in the spike:
   TTL expiry + key revocation are the revocation story (an admin
   revoking a key kills its offer by the same auth failure that kills
   its requests).

2. `GET /v1/dispatch/poll` — the long-poll. Query: `?wait_s=<0..60>`
   (default 25). Semantics: returns the NEXT dispatch job for this key,
   or 204 after `wait_s` of nothing. Body:
   ```json
   {
     "dispatch_id": "<uuid>",
     "tool": "bash",
     "arguments": {"command": "ls -la"},
     "dispatched_at": "...",
     "deadline_s": 120
   }
   ```
   Delivery-once: a job is ATOMICALLY claimed by the poll (the queue is
   popped under the registry's lock before the response is written) —
   the same poll from a second tab or a retry after a dropped response
   NEVER re-delivers. A client that received a job and crashed before
   answering simply lets the job time out (the relay side already holds
   a bounded future); redelivery-on-reconnect is deliberately NOT
   provided (the model re-issues; a duplicate execution of a side-effect
   tool is the worse outcome — documented tradeoff). The poll returns
   401/403 exactly like every other surface; a long-poll that would
   outlive the ingress budget is clamped server-side.

3. `POST /v1/dispatch/result` — the ANSWER.
   Body: `{"dispatch_id": "...", "ok": true, "content": "<string |
   object>"}` or `{"dispatch_id": "...", "ok": false, "error":
   "approval denied by local policy"}`. Returns 200 `{"settled": true}`
   or 409 when the id is unknown/expired/already-settled (an idempotent
   double-POST is REFUSED, not replayed: dispatch-once extends to the
   result — the first answer wins, a second POST gets 409 and the
   content is dropped). Response size bound (≤ 1 MiB, 413 over).

4. `GET /v1/dispatch/offer` — introspection (the relay's catalog source;
   also lets DSH verify its own registration). Returns the key's current
   offer: `{"client_id", "expires_at", "tools": [...]}` or 404 when no
   live offer. Read-only.

#### Relay-side: `core/clientside.py` — offer registry + dispatch futures

- `offer(key_id, tools, ttl_s)` / `offer_lookup(key_id)` — the offer
  table: `{key_id: ClientOffer(client_id, tools, expires_at)}`, monotonic
  clock, expiry checked at read.
- `dispatch(key_id, tool, args, timeout_s) -> tuple[bool, Any]` — mints
  a job (`uuid4`), enqueues it on the key's poll queue, parks on an
  `asyncio.Future` bounded by `timeout_s`. Resolution paths:
  result-POST → set future; TTL/timeout → `TimeoutError` → the caller
  sees `(False, "error: client tool timed out after Ns (client offline
  or busy)")`; unknown/duplicate dispatch_id → 409, future untouched.
  Queue bound: ≤ 64 pending jobs per key; overflow refuses the dispatch
  immediately (a client not polling must not accumulate unbounded work).
- `settle_result(key_id, dispatch_id, ok, payload)` — validates
  membership + not-yet-settled, resolves the future, marks settled.
- A pending-jobs counter + generation counter for tests (no DB, no
  persistence — process restart loses offers AND pending dispatches;
  the model just sees a tool error; documented, accepted for wave B1).

#### Executor adapter: same gate discipline as `_gated_mcp_call`

The relay's executor closure gains a client-tool branch (behind
`client_tool_dispatch`, default OFF — spike ships inert): when the model
names a tool that is in the KEY's live offer (the offer is registered
into the relay catalog under pseudo-server `client`), the executor runs:

1. cross-request loop guard — `loop_guard_check(key.id, "client",
   tool, args)`, the SAME fingerprint space the handback path uses
   (a dispatch loop is the same pathology);
2. RPM rate gate — `check_rate_limit(...)` exactly as the MCP executor;
3. settle — `settle(key.id, "*", f"local:{tool}", 0, 0, status, ms,
   request_id)` — endpoint naming `local:<tool>` keeps the ledger
   row distinct from `mcp:<server>:<tool>` (never billed as an upstream
   call; visible in /admin usage as its own endpoint);
4. trace — `traces.record(..., phase="tool", server="client",
   tool=<tool>, status=...)` — the trace card shows client executions
   under `server="client"`, same namespace the handback path already
   uses, so the console reads one story;
5. dispatch with the per-tool timeout clamped to the relay's REMAINING
   wall-clock budget (a dispatch can never extend the relay beyond the
   ingress clean-close guarantee the bounded-call work bought).

Denials flow back as tool errors: the client's local approval policy
rejects an execution by POSTing `ok=false` — the relay feeds the error
text to the model as the role=tool content (identical to an upstream
tool error), so the model self-corrects. A client that never answers
surfaces as the timeout error above. In BOTH cases the loop CONTINUES —
no handback, no finish_reason=tool_calls.

#### How the offered tools reach the model

The relay catalog (`_relay_catalog`) gains, behind the flag, one
pseudo-server entry per key: `server="client"`, tools = the live
offer's tools with their real `inputSchema`/descriptions. The model's
offer then contains `client__bash` next to `logsearch__search_logs` —
same shape, same naming grammar, same tool schema fidelity as MCP
tools. The executor's branch matches on the catalog entry (not name
shape), so a client tool named identically to a granted MCP tool is
NOT ambiguous — the pseudo-server namespace separates them (`client__
bash` vs `logsearch__bash`); shadowing (`tool_relay_shadow_mcp`) never
touches the `client` pseudo-server (it has no in-cluster twin).

#### Security analysis

- *Auth reuse*: the same bearer key gates offer, poll, and result. No
  second credential, no new revocation surface — key revocation kills
  the dispatch channel by the same 401 that kills chat. The offer is
  scoped to key_id; cross-key dispatch is impossible by construction
  (jobs are keyed, results are validated against the offering key).
- *Dispatch-once*: the queue pop is the delivery; a second poll cannot
  see a delivered job. Result-once: the first result settles; a replay
  gets 409 and is dropped (replay denial — a captured result POST is
  worthless after the first).
- *Replay across restarts*: in-memory state; a gateway restart empties
  offers and pending jobs (the client re-registers on its next poll
  404/loop; pending dispatches time out as tool errors). Accepted for
  B1; persistence is wave B2/C work if warranted.
- *TTL expiry*: the offer expires server-side (monotonic clock); the
  relay catalog is computed per request, so an expired offer disappears
  from the model's toolset without operator action. A dispatch racing
  expiry is refused as unknown-tool (default-deny: no live offer, no
  client tool).
- *Offline client*: bounded timeout (per-tool `timeout_s`, clamped to
  the relay wall clock) → tool error into the loop. Never a hang: the
  future is the timeout's, the SSE keepalive cadence already covers the
  wait, and the ingress clean-close guarantee is untouched.
- *Capability hygiene*: the offer can only ADD tools the key's operator
  chose to serve; the relay still gates every call through rate+loop+
  settle. A compromised client cannot escalate — it executes only what
  its own machine already permits.

#### Boundary with wave C

Wave B1 (this wave): design + spike, flag-off in production. Wave B2
(production integration): wire `routes_dispatch` into app.py behind
`client_tool_dispatch`, extend `_relay_catalog` + `_relay_executor`
in routes_proxy (the only substrate files B2 touches), add the DSH-side
offer/poll/answer plugin, and decide persistence (offers across
restarts) + multi-replica note (offers are per-replica memory until
persisted; the DSH client pins one gateway URL per conversation today,
so per-replica state is coherent with current routing). Wave C
(result-only continuation protocol + context budget) rides on top:
once tool results return INTO the loop, the handback replay — and
therefore the 214k-token re-send — disappears; the canonical
conversation becomes append-only server state.
