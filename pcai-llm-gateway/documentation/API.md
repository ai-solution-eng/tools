# API Reference — pcai-llm-gateway

Method/path/auth/body for every HTTP surface, sourced from the route code
(`api/routes_proxy.py`, `routes_conversations.py`, `routes_enroll.py`,
`routes_admin.py`, `routes_mcp.py`, `routes_dispatch.py`,
`routes_effective.py`). Behavior and rationale live in
[`FEATURES.md`](FEATURES.md); deploy knobs in [`DEPLOYMENT.md`](DEPLOYMENT.md).

## 1. Basics

**Base URL**: the gateway serves on port 9200 (in-cluster
`http://pcai-llm-gateway-service.<ns>.svc.cluster.local:9200`); through the
PCAI Istio gateway it is `https://pcai-llm-gateway.<your-domain>` with the
same paths. No Swagger/OpenAPI on prod pods (`docs_url=None` — the control
plane stays minimal); `GET /admin/help` is the curl-friendly discovery
index.

**Four credential classes:**

| Token | Shape | Carries | Unlocks |
|---|---|---|---|
| Virtual key | `hllm-...` bearer (minted via `/admin/keys` or `/v1/enroll`) | scopes + budgets + ACLs | `/v1/*` (`proxy` scope), `/mcp` (`mcp` scope), its own conversations + usage |
| Admin key | operator Secret, comma-separated list | the admin plane only | `/admin/*` |
| Enrollment code | `enroll-...` (minted via `/admin/enrollments`) | one redemption | `POST /v1/enroll` only |
| IdP token (Phase B, optional) | `Authorization: Bearer eyJ...` (Keycloak JWT) | identity claims (sub/email/groups) | nothing BY ITSELF — validates for attribution; authorizes only through its subject's shadow key (`PCAI_LLM_TOKEN_SHADOW_KEYS`) |
| Browser identity (Phase B, optional) | `X-Auth-Request-*` headers injected by the platform oauth2-proxy (subject: Preferred-Username → User → Email; groups: `X-Forwarded-Groups`, fallback `X-Auth-Request-Groups`) | a subject + groups — no credential material | nothing BY ITSELF — trusted only when `PCAI_LLM_TRUST_PROXY_IDENTITY=true`; identifies the caller on `GET /v1/identity` and `POST /v1/keys/for-me` (whose identity IS these headers) and never authorizes a request alone |

The three credential tokens are stored/compared in SHA-256 hash space;
plaintext is returned exactly once at mint time and never again. Fail
closed everywhere: missing/unknown/revoked/expired → 401; scope missing →
403; an empty admin config DISABLES the whole admin surface (503), never
"allow all".

**The `X-API-Key` envelope (data plane).** Behind an SSO-chained host the
proxy injects `Authorization: Bearer <id_token>` into every forwarded
request, so a client cannot deliver its virtual key via the Authorization
header at all — `X-API-Key: hllm-…` is the additional envelope for exactly
that posture. REST (`require_key`) and `/mcp` share ONE envelope+precedence
resolver, so the two surfaces cannot drift. Precedence is
DE-ESCALATION-ONLY (fleet convention D19): an explicit envelope OUTRANKS the
co-forwarded proxy Bearer; a DEAD envelope is never rescued by it (hard 401
— two keys presented with one dead is a refusal, never a silent rescue, and
a dead envelope must not escalate to the token rung on the Bearer's behalf);
with NO envelope present, resolution is byte-for-byte the pre-envelope
behavior. The admin console sends the envelope too on chained deployments —
inert on the skip-auth `/admin` paths (DEPLOYMENT.md § SSO self-mint rollout
step 3).

**Response headers worth reading** (data plane):

| Header | Meaning |
|---|---|
| `X-Request-ID` | Correlate client↔ledger↔traces↔logs |
| `X-Budget-Warning: 80%|95% ...` | A matching period budget crossed the threshold (visibility only, never a refusal) |
| `X-RateLimit-Remaining-Requests` / `-Tokens` | Fixed-window counters after the increment (SDKs back off natively) |
| `Retry-After` | On 429 refusals (rate/concurrency) and the enroll throttle |
| `X-Cache: HIT` | Embeddings answered from the cache (zero upstream spend) |
| `X-PCAI-Billing: offpeak-<fraction>` | Settled at a discounted billing fraction (absent = normal billing) |
| `X-PCAI-Relay: streaming` | The tool relay answered this request (SSE with `pcai` events) |
| `X-PCAI-Route` | The complexity-routing decision in effect for THIS chat response — on every response of a profile whose `routing_policy` is `shadow`/`active`: `shadow ceiling=<m> would=<m> reason=<cut>`, `active ceiling=<m> served=<m> reason=<cut>`, `off` (profile opted in, fleet switch down), or `... reason=direct` for non-pseudo models. Absent when the profile policy is `off` |
| `X-PCAI-Served-Model` | The model that ACTUALLY answered (post-cascade/post-routing/post-fallback truth) — on every chat response |
| `X-PCAI-Endpoint` | The endpoint id (a `model_endpoints` row) that served this response — on every chat response |
| `X-PCAI-Fallback` | The model this request was failed over FROM (the per-model fallback chain, an alias's profile-ordering tail, or an admission-time budget/rate promotion). Absent = the primary answered |

Request header `X-PCAI-Stream-Events: 1|0` overrides the key's
`stream_pcai_events` default for THIS relayed stream: `0` strips the
in-stream `{"pcai": …}` telemetry events for a byte-clean OpenAI-spec
stream (strict validating clients); `1` forces them on; absent → the key
(NEW keys default FALSE since the 2026-09-24 consumer inventory — the
console and enrollment surfaces opt their own keys in explicitly)
default (see FEATURES.md § Key model).

Request header `X-PCAI-Metadata: 1` opts a NON-STREAMING chat response's
body into the `"pcai": {"served_model", "endpoint", "cost_usd"?, "cached_tokens"?}`
block (serving facts + computed cost when the model is priced). Without it —
or on any stream — the upstream JSON/SSE bytes are relayed byte-identical
(transparent proxy; body mutation is strictly opt-in).

### Result-only continuation (§7 wave C)

A relayed turn that ends in a handback (finish_reason=tool_calls for
client-side tools) carries `"thread": "<id>"` on its final `{"pcai":
{"event": "handback"}}` SSE event. A client MAY then answer with a
RESULT-ONLY request instead of re-sending the whole transcript:

- Header: `X-PCAI-Continue: <thread>` (the thread ref from the handback
  event; the conversation is keyed to YOUR key — other keys' threads are
  invisible).
- Body: `messages` = ONLY the new role=tool results answering the
  handed-back calls. The gateway stitches its stored canonical list + the
  awaiting handback state + your results, verifies the continuation shape,
  and relays — no transcript re-send (a full-history relay turn measured
  214k input tokens; this protocol sends the results alone).

Errors (fail loud, never a silent mis-relay): 400 unknown thread / no
awaiting handback / results don't answer the handed-back calls; 503 when
canonical conversations are disabled. `X-PCAI-Conversation: raw` still
opts a request out of all conversation handling. Loop accounting is
PER-TURN (2026-09-25, supersedes the earlier per-conversation counter):
the relay's iteration guard resumes from the stored counter ONLY while the
requests keep answering handbacks (continuation shape) — a fresh user
message resets the budget, so `toolRelayMaxIterations` bounds one user
turn's relay loop, not the whole conversation.

**Error shapes**: the `/v1` proxy + `/admin` use FastAPI's `{"detail": ...}`;
the conversations surface uses `{"error": ...}` (one shape for the chat
console). Unknown → 404, malformed → 400, over-cap → 413, gates → 429,
pool saturation → 503 `{"error": "database busy, retry shortly"}`.
Unexpected 500s are opaque with the request id attached.

---

## 2. Data plane (`/v1`)

All `/v1` routes require a virtual key with the `proxy` scope
(`Authorization: Bearer hllm-...`), except conversations (any valid key) and
enroll (public).

### 2.1 `POST /v1/chat/completions`

OpenAI-compatible passthrough: the body leaves exactly as it arrived
(streaming supported via `"stream": true`), the upstream JSON comes back
exactly as received. Body must be a JSON object with `model` + `messages`
(400 otherwise); body cap `PCAI_LLM_MAX_BODY_BYTES` (32 MiB default, 413 —
chunked bodies are bounded too).

Pipeline: model resolution (pinned direct / profile cascade for aliases or
exhausted models / complexity-routed pseudo-models, below) → pre_call
guardrails → relay pre-flight (if requested) →
gates (budgets → RPM/TPM → concurrency) → pool pick → upstream →
post_call guardrails → settle ledger.

| Status | When |
|---|---|
| 200 | Served (or SSE stream opened) |
| 400 | Body not JSON / missing `model`+`messages` / non-chat endpoint type / relay requested with no MCP grants / malformed `tools` mcp entry / mcp execution requested while the relay is off / requested MCP server not registered or ungranted / **guardrail blocked (pre- or post-call; `error.code = "guardrail_blocked"`)** |
| 403 | Key lacks `proxy`, or model not in `allowed_models` (including a `routing__X` ceiling the key may not use) |
| 404 | Model not registered / bare `routing` against a profile without a model ordering / `routing__X` naming an unregistered model |
| 413 | Body over the cap (declared or chunked) |
| 429 | Budget exhausted / RPM-TPM exceeded / concurrency cap (all with reasons; cascade exhaustion includes `cascade_attempts`) |
| 502 | Upstream connection failed |
| 503 | No healthy endpoint (all in breaker cooldown) / database busy |

**Complexity routing (pseudo-models)** — when the key's profile opted in
(`profiles.routing_policy` = `shadow` or `active`, fleet switch
`PCAI_LLM_ROUTING_ENABLED` on), `model` may be `routing` (ceiling = the
profile ordering's best model) or `routing__X` (ceiling = X — 404 when
unregistered, 403 when outside `allowed_models`); the gateway serves the
cheapest profile-ladder model the request's complexity score fits — never
above the ceiling, never below the profile's `routing_min_model` floor.
`shadow` serves exactly as if routing were off but stamps the would-serve
model on the ledger; `active` serves the routed model through the normal
pinned/budget machinery (a 429 walk can never promote above the ceiling).
`X-PCAI-Route` (§1) announces the decision on every response of an
opted-in profile. A profile without a model ordering 404s bare `routing`
(`routing__X` serves X pinned). Mechanism and rollout rationale:
[FEATURES.md](FEATURES.md) § Routing.

**Tool relay** — request header `X-PCAI-Execute-Tools: 1` (or the key's
`relay_default` when the header is absent; `0` opts out). With the relay
on, `tools` is injected from the key's MCP grants (`<server>__<tool>`
naming) and the response is SSE (`X-PCAI-Relay: streaming`):

```
data: {"pcai": {"event": "thinking",    "iteration": 0, "text": "<reasoning delta>"}}
data: {"pcai": {"event": "iteration",   "iteration": 0, "text": "<intermediate text>"}}
data: {"pcai": {"event": "tool_call",   "server": "logsearch", "tool": "search_logs",
                "ok": true, "ms": 812, "args": {...}, "result": "<truncated preview>"}}
data: {"pcai": {"event": "tool_error",  "server": "...", "tool": "...", "detail": "denied by ACL", "args": {...}}}
data: {"pcai": {"event": "guard",       "detail": "max tool iterations (16) reached"}}
data: {"pcai": {"event": "model_usage", "iteration": 1, "prompt": 431, "completion": 87}}
data: {"id": "...", "object": "chat.completion.chunk", "model": "...",
       "choices": [{"index": 0, "delta": {"content": "<final answer>"}, "finish_reason": "stop"}]}
data: [DONE]
```

The final answer is one chunk BY DESIGN (token streaming inside the relay is
a roadmap item). Guards: max iterations (default 16 — one iteration is ONE
model round, which may batch several parallel tool calls, not one tool call),
wall clock (600 s, clamped in code to the upstream timeout minus the 30 s
ingress headroom), identical-call loop detection; every model call and tool
call settles the ledger, and a mid-relay budget exhaustion closes the stream
with a `guard` event. Every tool execution re-checks the key's grants, the
server's tool policy and the RPM window.

**MCP request-body opt-in** (OpenAI-idiomatic; no custom header needed) —
include in `tools` an entry `{"type": "mcp", "server"?: "<registered name>",
"require_approval"?: "never" | "always"}`:

- `require_approval: "never"` (default) — in-cluster execution for THIS
  request (same as the header). With `"server"`, the relay narrows to that
  server's granted tools (unregistered server → 400; nothing granted →
  400 with the reason). An explicit `X-PCAI-Execute-Tools: 0` still wins —
  a body execution request under it is a 400 pointing at
  `require_approval: "always"`, never a silently ignored tool request.
- `require_approval: "always"` — NO server-side execution: the granted
  catalog (including upstream `inputSchema`s) is exposed as ordinary
  OpenAI function tools and the model's `tool_calls` come back untouched
  for the CLIENT to execute (human-in-the-loop flows, off-cluster
  harnesses). Continue the conversation by sending the tool result back
  as a `tool` message; each client-side call against `/mcp` is settled
  and ACL-checked there as usual.
- mcp-type entries are stripped from the payload forwarded upstream in
  both modes.

**Content guardrails** — models may carry guardrails attached in the admin
console (§4.8). `pre_call` guards run BEFORE the gates: a block is a 400
(`error.code = "guardrail_blocked"`, message names the guardrail) that
never consumes rate window, budget or a concurrency slot; a mask rewrites
the request messages before they leave. `post_call` guards run on the
assistant text AFTER the upstream answer: a block is a 400 (the REAL token
spend is settled as a `guardrail_blocked` ledger row — the upstream call
happened); a mask rewrites the response body and adds
`X-PCAI-Guardrail-Modified: <guardrail names>`. Streaming responses are
buffered when the model has post_call guards: pass replays the stream
byte-identical, masked suppresses the content deltas and resynthesizes one
final chunk, blocked discards the content and emits a terminal SSE error
envelope (`data: {"error": {...}}` + `[DONE]`). On the relay path pre_call
always applies and post_call judges the final answer; guardrail verdicts
land on the settled ledger rows (relay path: a 0-token `chat:guardrail`
marker row). Every verdict — pass included — is recorded in
`usage_ledger.guardrail_events`.

### 2.2 `POST /v1/embeddings`

OpenAI-compatible passthrough; body must carry `model` + `input`. Identical
`(model, input)` **string** inputs are answered from the Postgres cache —
`X-Cache: HIT`, zero upstream work, zero budget (a `cache_hit` ledger row
stays visible). List inputs pass through uncached (a cached hit would
return one embedding where N were requested). Cached rows can also ride
upstream prompt-cache (`cached_tokens`).

### 2.3 `GET /v1/models`

Registered virtual model names in OpenAI shape
(`{"object":"list","data":[{"id","object":"model","created","owned_by"}]}`),
filtered by the key's `allowed_models`. No gate, no ledger row. Additive
synthetic entries: `auto` (`pcai.type: "cascade"`, `pcai.order: [...]`)
when the key's profile has a model ordering with a callable candidate, and
— when the key's profile `routing_policy` is `shadow`/`active` with the
fleet switch on — `routing` plus `routing__{X}` for each registered chat
model the key may use (each `pcai.type: "routing"`). Off-policy keys see
none of the routing names.

**Silent truncation is now announced.** The response carries
`X-PCAI-Models-Denied: <n>` — the number of registered models this key's
`allowed_models` filtered out (`0` when the ACL is empty = allow-all).
Without it a key owner could not tell "the gateway does not have that
model" from "my key may not use it", which is exactly the confusion the
chat console's dropdown used to produce. The JSON body is unchanged, so
existing clients are byte-identical; the header is additive metadata for
consoles and operators. `/admin/presented-models` is the admin-side view
of the same registry.

### 2.4 `GET /v1/usage`

The calling key's own state — the "why am I 429ing" surface:

```json
{
  "key": {"id", "name", "prefix"},
  "limits": {"rpm", "tpm"},
  "this_minute": {"requests", "tokens"},
  "budgets": [ {"kind": "tokens", "model", "period", "limit", "spent", "window_start"},
               {"kind": "usd",    "model", "period", "limit_usd", "spent_usd", "window_start"} ],
  "recent": [ {"ts","model","endpoint","prompt_tokens","completion_tokens",
               "total_tokens","status","latency_ms"} ]   // 20 most recent
}
```

### 2.5 Conversations — `/v1/conversations`

Any valid virtual key (no extra scope); every route is scoped to the
calling key — a foreign/unknown id is a plain 404, identical to missing.
Errors are `{"error": ...}` bodies.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/v1/conversations` | — | `{"data": [{id, title, updated_at, message_count}]}` (newest activity first) |
| POST | `/v1/conversations` | `{title?}` | 201 `{id, title, updated_at}` |
| GET | `/v1/conversations/{id}` | — | `{id, title, updated_at, messages: [{seq, role, content, ts}]}` |
| PATCH | `/v1/conversations/{id}` | `{title}` | `{id, title, updated_at}` (title sanitized, ≤120) |
| PUT | `/v1/conversations/{id}/messages` | `{messages: [{role, content}]}` | `{saved, updated_at}` — REPLACE-ALL transcript; roles `user|assistant`; caps 200 messages / 32 000 chars (413 over-cap) |
| DELETE | `/v1/conversations/{id}` | — | `{"deleted": true}` (messages cascade) |

### 2.6 Session distiller — `/v1/distill`

Any valid virtual key. Turns a transcript into the calling key memory
dataset (project-summary via the canonical prompt; errors `{"error": ...}`).
**Inert contract**: a key whose memory feature is not attached (no enabled
`mm-rag` registry row, or no stamped `{X-Memory-Dataset, X-Dataset-Password}`
pair) gets `200 {"ok": false, "inert": true}` and nothing runs — harnesses
call unconditionally. Feature flag `PCAI_LLM_DISTILLER_ENABLED` unmounts
both routes (404) when false.

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/v1/distill` | `{transcript, session_id?, max_tokens?}` | `{ok, inert, kind, detail, session_id}` — kind `project-summary`; model failure on the inline surface writes nothing |
| POST | `/v1/distill/conversations/{id}` | — | `{ok, inert, kind, detail, conversation_id}` — distills the STORED conversation; model failure degrades to the raw transcript as `session_history` (a session is never lost) |

Scheduled distillation (housekeeping sweep of quiet, memory-attached
conversations; re-distill on continuation) is admin-configurable —
`GET/PUT /admin/distiller/config` (summarizer model, off-peak-only
scheduling, quiet window, output cap, runtime enable).

**Attaching a key** (the credential chain behind the inert contract):
`PUT /v1/mcp/creds/{server}` (self-service, `mcp` scope) or
`PUT /admin/keys/{key_id}/mcp-creds` (admin) store per-key header overrides
write-only; `GET` returns header NAMES only. Full walkthrough, including the
mm-rag key/★-dataset side: [MEMORY.md](MEMORY.md).

### 2.7 `POST /v1/enroll` — redeem an enrollment code

PUBLIC by design — the code is the credential. Body `{"code": "enroll-...",
"name": "Alice"}` (name 1–64 chars after trim). Optional `subject` (self-
asserted binding, ignored when trusted browser headers are present) and
`memory_opt_in` (`bool | null` — see below). Throttled 10 redemptions/
hour per client IP (429, `Retry-After: 3600`); every dead-code path answers
the same `400 "invalid, expired, or exhausted code"` (no per-cause oracle).

```json
// 201
{"key": "hllm-...", "key_prefix": "hllm-xxxxxx", "user": "Alice",
 "tenant": "fleet", "budgets": [...],
 "subject": null, "idp": "",
 "memory": {"provisioned": false},
 "message": "copy this key now — it is shown once"}
```

The minted key carries `proxy` + `mcp` scopes; a profile-bound code stamps
the profile's preset budgets + MCP grants on at creation.

**Memory opt-in** (`memory_opt_in`): `true` provisions a private RAG dataset
`mem-<tenant>-<user>-<key6>` on the registered memory MCP server
(`PCAI_LLM_ENROLL_MEMORY_SERVER`, default `mm-rag`) and stamps
`X-Memory-Dataset` / `X-Dataset-Password` onto the key's per-server
credential row; `null`/omitted falls back to the profile preset
(`"memory": {"provision": …}` in `defaults_json`) then the deployment
default (`PCAI_LLM_ENROLL_MEMORY_DEFAULT`, false). On success the `memory`
block is `{"provisioned": true, "dataset": "…", "password": "…"}` — the
password is shown ONCE here, then only stored Fernet-encrypted. Provisioning
failure NEVER fails the redemption: `memory` reports
`{"provisioned": false, "error": "…"}` and the key is still issued (a
missing/unset `PCAI_LLM_SECRETS_MASTER_KEY` prevents even the create —
nothing is provisioned that cannot be bound). The gateway never deletes
datasets.

### 2.7 Identity probe + SSO self-service key (Phase B)

**`GET /v1/identity`** — the whoami probe the SPA calls BEFORE it has a key.
PUBLIC by design (no auth dependency — there is nothing here to authorize;
the response only describes the calling request back to itself) and resolved
by the SAME shared envelope resolver the enforcement paths run
(`require_key` / `/mcp`), so the probe cannot drift from enforcement. The
response carries EXACTLY six top-level keys, never more — the anti-oracle
contract (a refused shape must not become a distinguishable oracle for
"which credential failed"). RESOLVED-anonymous is exactly:

```json
{"authenticated": false, "subject": null, "idp": null,
 "key_prefix": null, "scopes": [], "flags": {"self_mint": false}}
```

- `authenticated` — a LEDGER KEY anchors the request (bearer key, `X-API-Key`
  key, or a validated token's shadow key). A browser-header identity alone is
  `authenticated: false` — enforcement still 401s it until a key exists; the
  probe surfaces its subject so the SPA can show who it is pre-mint.
- `subject` / `idp` — the resolved browser subject, or the key's subjects
  binding (`oauth2-proxy` / `keycloak` / `enrollment` / `self-asserted` /
  `admin-asserted`); browser-header and token-only identities have no
  binding row, so `idp` is null there.
- `flags.self_mint` — the exact precondition under which `POST
  /v1/keys/for-me` would succeed: `PCAI_LLM_TRUST_PROXY_IDENTITY=true` +
  `PCAI_LLM_SSO_KEY_CREATE_ENABLED=true` + a browser-header identity on THIS
  request + the subject's admin-governed eligibility
  (`users.self_mint_enabled` via `GET/PUT /admin/keys/self-mint-eligibility`;
  a subject with no live `sso-create` key is default-eligible — first mint).
- Fail-closed: resolver/DB/JWKS failures → **503** ("identity resolution
  unavailable") — never the anonymous shape (an unavailable IdP must not
  read as "you are anonymous").

Behind the ezaf-gateway SSO chain, an unauthenticated BROWSER is 302'd to
Keycloak by the EDGE before the app sees the route — correct and required
(the identity IS the headers the chain injects; DEPLOYMENT.md § SSO
self-mint rollout step 3).

**`POST /v1/keys/for-me`** — for a BROWSER caller behind oauth2-proxy with
`PCAI_LLM_TRUST_PROXY_IDENTITY=true` (and `PCAI_LLM_SSO_KEY_CREATE_ENABLED=true`,
default false): mints a key bound to the CALLER'S OWN identity — no body, no
subject choice, no grant choice (scopes fixed `proxy`+`mcp`, default limits,
grants stay admin-controlled). 403 when trust is off, 401 without identity
headers. The plaintext is shown once, like every mint.

```json
// 201
{"key": "hllm-...", "key_prefix": "hllm-xxxxxx", "subject": "alice", "idp": "oauth2-proxy",
 "message": "copy this key now — it is shown once"}
```

**One key per identity:** a second mint for a subject that already holds a
live `sso-create` key is refused with **409** unless the body is
`{"rotate": true}` — rotate REVOKES the old key in the SAME transaction that
mints the new one (never two live keys, never zero; a crash mid-rotate
leaves the retryable pre-rotate state — old key live, none minted). Junk or
absent body = legacy semantics (`rotate=false`). Subject eligibility is
admin-governed (`users.self_mint_enabled`); a blocked subject reads the SAME
403 a flags-off deployment gives — the refusal never distinguishes "surface
off" from "not eligible" — and an eligibility-store failure is 503, never a
403.

**`GET /v1/keys/for-me`** — the owner-safe masked view of the calling
identity's own key, same gates as the POST (403 trust-off/blocked, 401
without browser headers). HTTP 200 ALWAYS — a 404 would be
indistinguishable from route-absent on older backends — with the state in
the body: `{"exists": false}`, or `{"exists": true, "key_prefix", "name",
"created_at"}`. Never the plaintext: prefix/name/timestamp only.

**Token validation (Phase B, optional):** when `PCAI_LLM_TOKEN_ISSUER` /
`PCAI_LLM_TOKEN_JWKS_URL` / `PCAI_LLM_TOKEN_AUDIENCE` are all set, a JWT-shaped
bearer is validated against the IdP's JWKS (issuer+audience pinned, keys
TTL-cached with a refresh-once on an unknown kid). An INVALID or EXPIRED token
never hard-401s a request that also authenticates as a virtual key (the
ladder falls through — this is what keeps the ~30-min token TTL invisible to
headless key holders); a VALID token with no matching key resolves an identity
for attribution but authorizes nothing (401) unless token shadow-keys are
enabled, in which case the subject's newest non-revoked `token-shadow` key
(first sighting lazily provisions user + key) is the authorization anchor.

---

## 3. MCP surface (`/mcp`)

MCP 2.0 streamable-http (stateless — any replica serves any request),
behind a pure-ASGI wrapper that authenticates the caller (401) and requires
the `mcp` scope (403) before the MCP session manager ever sees the request.
Authentication runs the SAME shared envelope resolver as REST (§1 —
`X-API-Key` envelope + bearer, one code path), so `/mcp` and `/v1` cannot
drift in how a key is resolved.

**`tools/list`** (flat surface, default): the two gateway tools FIRST
(compat surface for off-cluster consumers), followed by the calling key's
GRANTED upstream tools as individual MCP tools named `{server}__{tool}`
(sanitized to the MCP name grammar — disallowed chars → `_`; names over 64
chars → 56-char body + 8-char sha256 suffix of the raw name), each carrying
the upstream tool's real `inputSchema` and `annotations`. ACL-filtered per
request (default-deny): a server with zero grants for the key contributes
nothing — not even its URL; zero grants → only the gateway tools. Kill
switch `PCAI_LLM_MCP_FLAT_TOOLS=0` reverts to exactly the two-gateway-tool
listing (flattened names become unknown-tool errors — the pre-flat
surface).

| Tool | Arguments | Returns |
|---|---|---|
| `mcp_catalog` | — | The federated tool catalog filtered to what THIS key's ACLs allow (default-deny: nothing appears until granted), servers in fleet priority order (`priority` ASC, then name) |
| `mcp_call` | `{server, tool, arguments}` | The upstream tool result; refusals are structured (`denied by ACL`, `denied by server policy`, rate limited) |
| `{server}__{tool}` | the upstream tool's own inputSchema | The same gated execution as `mcp_call`; may fail over on transport failure (below) |

**`tools/call`** accepts flattened AND legacy names; every call funnels
through ONE gate chain — validation → RPM rate limit → registry lookup →
per-key ACL (default-deny) → server tool-policy → proxied call → ledger
settle. **Failover asymmetry (deliberate)**: a flattened call that hits a
TRANSPORT failure (unreachable/timeout/protocol error — never an upstream
tool-error result, which is a real answer) retries the next priority-ordered
enabled server whose cached listing exposes the tool, that the key holds an
explicit grant on, and whose tool policy admits it — up to
`PCAI_LLM_MCP_FAILOVER_ATTEMPTS` (2) retries after the primary; each
attempt settles under its own `mcp:<server>:<tool>`; the final error
carries `attempts`. Explicit `mcp_call(server, tool)` NEVER fails over —
the caller named the server. Upstream listings are TTL-cached per server
(`PCAI_LLM_MCP_CATALOG_CACHE_S`, default 60 s; key-independent, invalidated
on every registry mutation, stale listing served on upstream error).
Per-key MCP header overrides (below) merge into the upstream headers
last-wins on the call path. On every keyed call the gateway stamps the
upstream with `X-MCP-Caller: <subject or key-fp>@gateway` (audit
legibility; attribution-never-authorization) and — only when the key is
bound to a real subject — `X-MCP-Caller-Subject: <subject>` (bare, no
suffix) + `X-MCP-Caller-Class: <subject_kind>` (`browser|token|key-bound|
key-anon` — the fleet receiver's vocabulary); all three names are reserved
from per-key overrides, and an unbound key emits only `X-MCP-Caller`.

---

## 4. Admin plane (`/admin`)

Every route requires `Authorization: Bearer <admin key>` (503 when no admin
keys are configured). `GET /admin` is the console; `GET /admin/help` the
JSON discovery index.

### 4.1 Tenancy

| Method | Path | Body / notes | Response |
|---|---|---|---|
| POST | `/admin/tenants` | `{name}` | 201 `{id, name, created_at}` (409 duplicate) |
| GET | `/admin/tenants` | — | `[{id, name, created_at}]` |
| GET | `/admin/tenants/{tenant_id}/users` | — | `[{id, tenant_id, name, profile_id, created_at}]` |
| POST | `/admin/tenants/{tenant_id}/users` | `{name}` | 201 (409 duplicate in tenant) |

### 4.2 Keys

| Method | Path | Body / notes | Response |
|---|---|---|---|
| POST | `/admin/keys` | `{user_id, name, scopes?, allowed_models?, rate_limit_rpm?, rate_limit_tpm?, expires_at?, relay_default?, stream_pcai_events?, profile_id?}` — `stream_pcai_events` defaults FALSE (spec-clean; console/enroll opt in). With `profile_id`: attaches the profile to the user AND stamps its preset budgets + MCP grants in one transaction | 201 `{id, key, key_prefix}` — **plaintext shown once** |
| GET | `/admin/keys` | `?include_revoked=` | `[{...key fields, budget_status: [...]}]` — hashes never included |
| PATCH | `/admin/keys/{key_id}` | any of `{name, scopes, allowed_models, rate_limit_rpm, rate_limit_tpm, max_concurrent, expires_at, relay_default, stream_pcai_events}`; `expires_at: null` CLEARS expiry; omitted fields untouched; identity material absent | the updated row — live immediately (keys are read per request) |
| DELETE | `/admin/keys/{key_id}` | — | `{"revoked": bool}` (idempotent revoke) |
| POST | `/admin/keys/{key_id}/rotate` | — | 201 `{id, key, key_prefix, old_key_id, old_revoked, grants_copied, creds_copied, budgets_copied}` — mints a NEW key with the SAME name + attributes copied verbatim (scopes, allowed_models, RPM/TPM, max_concurrent, expires_at, relay_default), then revokes the old (create-first-then-revoke: a partial failure leaves both keys live, never none). **Plaintext shown once** — the DB keeps only the hash. The server CARRIES OVER the identity rows in one INSERT..SELECT each: `mcp_tool_acls` grants (allow AND deny; `grants_copied`), `mcp_server_creds` per-server credential overrides (`creds_copied` — the memory dataset/password identity survives rotation verbatim), and the `budgets`/`cost_budgets` LIMIT rows (`budgets_copied`, ON CONFLICT DO NOTHING). **Spend survives rotation**: the new key inherits `spend_ancestors` (+ the old id) and the budget gates sum the lineage's in-window ledger spend — rotating never resets a period budget (calendar windows still reset normally; RPM/TPM counters reset — 1-minute burst windows). 404 unknown key; 409 already-revoked (refused before any write) |
| POST | `/admin/keys/{key_id}/budgets` | `{"budgets": [{model, period, limit_tokens|limit_usd}]}` — exactly one limit per entry; REPLACE-ALL both kinds in one transaction | `{key_id, budgets: [...status]}` |
| GET | `/admin/keys/{key_id}/usage` | — | `{budgets: [...], recent: [50 ledger rows]}` |
| GET | `/admin/keys/{key_id}/mcp-grants` | — | `[{server, tool, allowed[, source]}]` — the key's TRUE grants AND `allowed=FALSE` deny rows, unioned with the owner profile's LIVE grant presets (`allowed: true, source: "profile"`; a key-level deny always wins, so a pair never appears twice) |
| PUT | `/admin/keys/{key_id}/mcp-grants` | `{"grants": [{server, tool}], "denies": [{server, tool}]?}` — replace-all of the GRANT set in one transaction; `denies` (≤500) writes `allowed=FALSE` rows for unticked-but-offered pairs, and pre-existing denies survive a grants-only body. A pair in BOTH lists resolves to the grant. Re-granting a pair that holds a deny row upserts it TRUE (`ON CONFLICT … DO UPDATE SET allowed = TRUE`) — the console's per-tool re-enable gesture | `{key_id, grants: N, denies: N}` |
| PATCH | `/admin/keys/{key_id}/mcp-grants/{server}` | `{"mode": "off"\|"on"\|"catalog"}` — one-press per-server deny op (audit `mcp.acl.server.set`). `off` writes FALSE rows for the key's held pairs (TRUE rows + profile presets) ∪ the server's live catalog, capped at 500 pairs; `on` deletes the server's FALSE rows (grants untouched); `catalog` restores the server's deny rows exactly as they stood (capture → delete → re-insert), never touching a TRUE row | `{key_id, server, mode, pairs_written[, catalog_ok]}` — `catalog_ok: false` marks an `off` whose live-catalog fetch failed (degraded to the held pairs; also in the audit row). 404 unknown key/server, 400 past the cap |
| POST | `/admin/keys/{key_id}/mcp-grants/copy-from` | `{"source_key_id": "…"}` — replace-all refill from the DONOR key's rows in one INSERT..SELECT, so allow AND `allowed=FALSE` deny rows both survive (the plain PUT is upsert-safe: a re-granted pair flips its deny row TRUE, and a grants-only body leaves existing denies in place). Self-copy is a no-op (returns the current count); unknown target/source → 404 | `{key_id, source_key_id, grants: N}` |
| PUT | `/admin/keys/{key_id}/mcp-creds` | `{server, headers: {Name: value}}` — per-key MCP credential overrides; ≤16 headers, values ≤512 chars, names `[A-Za-z0-9-]+`; empty refused (clearing is DELETE); 503 without the master key | `{key_id, server, header_names}` — **values never return** |
| GET | `/admin/keys/{key_id}/mcp-creds` | — | `[{server, header_names}]` |
| DELETE | `/admin/keys/{key_id}/mcp-creds/{server}` | — | `{"deleted": true, "server"}` |

### 4.3 Models + endpoint pools

| Method | Path | Body / notes | Response |
|---|---|---|---|
| POST | `/admin/models` | `{model_name, model_type?, endpoints: [{base_url, priority?, weight?, api_key_secret?, api_key?}]}` — upsert per (model, base_url); endpoints NOT in the payload are PRUNED; `api_key` is panel credential (`null` keep, `""` clear, value set — stored Fernet, never returned); URLs pass the SSRF guard; registry refreshed | live pool names `["..."]` |
| GET | `/admin/models` | — | live pool names (post-refresh state) |
| DELETE | `/admin/models/{model_name}` | — | `{deleted, endpoints_removed, live_models}` — cascade entries skip unregistered models; no data loss |
| POST | `/admin/models/{model_name}/probe` | — | `{ok, status_code, latency_ms, endpoint[, error]}` — GET `{base_url}/models`, 5 s timeout, no ledger row |
| POST | `/admin/models/{model_name}/detect-type` | — | `{model, endpoint, served_id, type, current_type, reason, evidence, suggested_model_type}` — probes each capability surface with a VALID minimal payload and reports the detected `model_type`. **`200` is the only status that proves a surface is SERVED**; a `400`/`422` only proves the route is mounted (vLLM/SGLang mount `/embeddings` and `/rerank` even on a chat model), and an empty probe body cannot tell the two apart. The native vLLM-SR runtime is identified from its card (`surfaces`/`family`) with no probe at all. `type: ""` = could not tell — an honest unknown rather than a guess. Explicit admin action: each surface is a real upstream request, so it never runs on a registry refresh and writes no ledger row |
| GET | `/admin/models/{model_name}/fallbacks` | — | `{model, fallbacks: [names in walk order], max: PCAI_LLM_MODEL_FALLBACK_MAX}` |
| PUT | `/admin/models/{model_name}/fallbacks` | `{fallbacks: [names in walk order]}` — replace-all; every name must be a registered model, self-fallback refused, duplicates collapsed, length capped at `PCAI_LLM_MODEL_FALLBACK_MAX`. The chain is walked when the model cannot pick or answers 5xx/transport-error; each hop re-checks registration, the key's ACL, a healthy chat pool, and budget headroom — a fallback is never a policy or budget bypass | `{model, fallbacks}` |
| GET | `/admin/endpoints` | — | every endpoint row merged with live breaker state: `{id, model_name, base_url, priority, weight, model_type, enabled, state: healthy\|cooling\|disabled, consecutive_failures, cooldown_remaining_s, last_error, last_ok_at, last_fail_at}` |

### 4.4 MCP registry

| Method | Path | Body / notes | Response |
|---|---|---|---|
| POST | `/admin/mcp-servers` | `{name, url, headers?, auth_secret?, auth_token?, tool_policy? (all\|allowlist), allowed_tools?, enabled?, priority? (int, default 0 — lower = preferred, orders the registry snapshot: catalog/listing output AND the flattened-call failover chain)}` — upsert by name; `auth_token` null=keep / `""`=clear / value=set (Fernet); SSRF-guarded URL | the row incl. `priority` (`auth_configured: bool`, never the token) |
| GET | `/admin/mcp-servers` | — | registry snapshot in fleet priority order (ciphertext stripped → `auth_configured`) |
| PATCH | `/admin/mcp-servers/{name}` | any of `{enabled, tool_policy, allowed_tools, priority}` — omitted fields PRESERVE the current value (`enabled` so a policy-only edit must not resurrect a killed server; `priority` so an inline edit cannot silently reset it — an explicit 0 is honored) | updated row |
| GET | `/admin/mcp-servers/{name}/tools` | — | `{server, policy, allowed_tools, tools: [{..., allowed}]}` — LIVE `list_tools` (full admin view, 15 s bound); also the MCP "test" button |
| POST | `/admin/registry/refresh` | — | `{models, mcp_servers}` — resync caches after out-of-band changes (breaker state survives) |

### 4.5 Costs, profiles, usage, traces, off-peak

| Method | Path | Body / notes | Response |
|---|---|---|---|
| POST | `/admin/costs` | `{model_name, input_per_m, output_per_m, cached_per_m}` (USD per 1M tokens) | `{model_name, priced: true}` |
| GET | `/admin/costs` | — | all configured prices |
| DELETE | `/admin/costs/{model_name}` | — | `{deleted: bool}` (404 when not priced) — removes the price row; ledger history stays put (costs were computed AND stored per row at request time; removal affects future requests only, which ledger NULL = unpriced). `{model_name:path}` carries slash-containing names ("Qwen/Qwen3.8-27B-FP8") |
| POST | `/admin/profiles` | `{name, model_order?, budgets? [{model, period, limit_tokens\|limit_usd}], mcp_grants? [{server, tool}], offpeak_fraction? (0–1, null = inherit), routing_policy? (off\|shadow\|active, default off — anything else 422), routing_min_model? (default "" = no downgrade; must be "" or name a model IN `model_order`, else 400), memory? ({provision: bool} — the enrollment-memory preset; null = undeclared)}` — upsert by name; each `budgets` entry carries exactly ONE limit kind (`limit_tokens` = token budget → `budgets`, `limit_usd` = dollar budget → `cost_budgets`), period `day\|week\|month`, positive | profile incl. expanded presets (both budget kinds round-trip) + both routing fields + the memory preset |
| GET | `/admin/profiles` | — | all profiles (cascade + presets + fraction + `routing_policy`/`routing_min_model`); `budgets` round-trips both limit kinds (`limit_tokens` and `limit_usd`) exactly as POST accepted them |
| DELETE | `/admin/profiles/{profile_id}` | `?force=true` detaches attached users | `{deleted, detached_users}` (409 while users are attached, without force) |
| PUT | `/admin/users/{user_id}/profile` | `{profile_id: <uuid|null>}` (null clears) | the user row |
| GET | `/admin/usage` | filters `key_id, user_id, model, status, since, until, limit (≤1000)`, `group_by=key|model|tenant|day`, `format=json|csv` | ledger rows with actor join (`key_name`, `key_prefix`, `user_name`, `tenant_name`), rollups, or `text/csv` attachment |
| GET | `/admin/usage/summary` | — | `{totals: {requests, tokens in/out/cached, cache_hits, cost_usd}, by_model: [...]}` |
| GET | `/admin/traces` | `?user_id=&key_id=&since=ISO-8601&limit= (≤200, default 50)` | request-grouped timelines: `[{request_id, key_id, user_id, user_name, key_name, model, started_ts, last_ts, steps: [{iteration, phase, server, tool, ms, prompt_tokens, completion_tokens, status}], totals: {steps, tool_ms, chat_ms, tool_calls}}]` |
| GET | `/admin/traces/{request_id}` | — | one request's timeline (404 when nothing traced) |
| GET | `/admin/offpeak` | — | read-only evaluator snapshot: `{enabled, active, fraction, enter_pct, exit_pct, check_s, promql, mcp_server, tool, last_value_pct, last_check, last_error, state_since}` |
| PUT | `/admin/offpeak/models/{model_name}/fraction` | `{fraction: <0–1>}` or `{fraction: null}` (clear — falls back to profile fraction, then the gateway default). Precedence while the model's governing evaluator is ACTIVE: override → profile → default; inactive bills 1.0. `{model_name:path}` carries slash-containing names | the updated row |
| GET | `/admin/presented-models` | — | the admin-side "what does `/v1/models` advertise" view: `{aliases: [{id, type, order, demand_aware, present, reason}], models: [{id, type, primary, endpoints, healthy, max_tokens_ceiling, offpeak{value_pct,active,fraction}, demand_aware_excluded, routing_eligible}], routing: [{id, type, present, reason}], env{routing_enabled, load_aware_cascade}, counts{presented,registered}}`. Registry truth + config truth — an admin key holds no profile, so `order` is derived (chat-first, then name) and each alias `reason` says so |

### 4.6 Model routing (decision-model prep)

Runtime configuration for the complexity router (`core.model_routing`) and
for the DECISION model that will replace its heuristics (vllm-sr /
d3-turbo). Storage is DB-backed with a 60 s TTL cache — `core.routing_config`
mirrors `core.distiller_config`: the env flags are deployment defaults, these
rows are the operator override layer, and a DB blip keeps the last known
policy rather than resetting it mid-flight.

`enabled` is AND-ed with `PCAI_LLM_ROUTING_ENABLED` — the env flag still owns
the route/mount, the DB row owns whether routing *acts*. The effective state
is what `/admin/routing/config` reports as `effective_enabled`.

| Method | Path | Body / notes | Response |
|---|---|---|---|
| GET | `/admin/routing/config` | — | `{config, defaults, overridden: [keys present in DB], registered, chat_models, env_enabled, effective_enabled, placeholders, gpu}` |
| PUT | `/admin/routing/config` | any of `{enabled, decision_model, allowed_models, prompt, inject_gpu_load, gpu_load_source, max_prompt_chars}`; a field explicitly `null` CLEARS its row back to the default; an OMITTED field is untouched (the console sends `null` when the operator unticks every model); unknown keys 400. `decision_model` must be `""` or a registered **chat** pool; every `allowed_models` entry must be a registered **chat** pool (an embedding/reranker pool can never be a routing candidate, and an allowlist of them would intersect to empty — which reads as "every chat model", the opposite of the intent); `gpu_load_source` is `offpeak` only; `max_prompt_chars` 200–60000; `prompt` ≤ 20 000 chars. The audit row records the prompt's LENGTH, not its body (the trail is append-only and never pruned) | `{config}` (merged, effective) |
| POST | `/admin/routing/prompt-preview` | `{prompt?, decision_model?, inject_gpu_load?}` — renders with the SAVED config plus these in-memory overrides. **Writes nothing**, no audit row | `{rendered, truncated, chars, gpu, models, used_placeholders}` |

`allowed_models` is the set of models the router may ROUTE TO (empty = every
registered chat model); it is deliberately separate from a key's
`allowed_models` ACL, which remains a hard per-key boundary the router can
never widen.

**Prompt templating.** The decision-model prompt is a template, not a
`str.format` string — no `eval`, no attribute access, and an unknown
`{{placeholder}}` is left VERBATIM so a typo is visible in the preview
instead of being silently eaten:

| Placeholder | Renders |
|---|---|
| `{{models}}` | the router's allowed models, comma-joined |
| `{{model_list}}` | one `- <name>` line per model |
| `{{gpu_load}}` | one line per model with its live utilization and demand state — `- Qwen3.8-27B: 42.1% util (low demand, 0.5x)`, or `- <name>: no data` |
| `{{gpu_load_json}}` | the same data as JSON (sorted keys) for a structured prompt |
| `{{decision_model}}` | the configured decision model name |

GPU load comes from the SAME off-peak evaluator the billing discount uses
(`core.offpeak`, the panel documented in §4.5) — one signal, so the prompt
and the price can never disagree about how busy the fleet is. When
`inject_gpu_load` is false the two GPU placeholders render as
`(gpu load injection is off)`; when the evaluator has no data (disabled, or
no per-model PromQL configured) they render the `no data` form rather than
fabricating a number. `max_prompt_chars` truncates the RENDERED prompt, so a
large GPU snapshot cannot blow the decision model's context.

### 4.7 Enrollment codes

| Method | Path | Body / notes | Response |
|---|---|---|---|
| POST | `/admin/enrollments` | `{label, tenant_id, profile_id?, max_uses? (0 = unlimited), expires_days?}` | 201 — includes `"code": "enroll-..."` **shown once** |
| GET | `/admin/enrollments` | — | all codes with usage state (tenant/profile names, uses, expiry, revocation; hashes never leave the DB) |
| POST | `/admin/enrollments/{enrollment_id}/revoke` | — | `{"revoked": true}` (idempotent) |

### 4.8 Ops

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/help` | Admin-gated discovery index (endpoint map; the kubectl recipe was deliberately removed — an authenticated admin already holds the key) |
| GET | `/health` | Liveness — process up; returns `{"status":"ok","version":...}` |
| GET | `/ready` | Readiness — a real Postgres round-trip; 503 takes the pod out of the Service |
| GET | `/metrics` | Prometheus exposition (when `metrics.enabled=true`) |
| GET | `/`, `/ui` | Chat UI (key holders); `GET /admin` console (admin key via sessionStorage, Bearer on every fetch — never a cookie) |

### 4.9 Guardrails (content policy)

Named guardrails attached per virtual model name. Engines: `blocklist`
(`{"keywords": [...], "action": "block"|"mask"}`), `regex_pii`
(`{"entities": subset of EMAIL/PHONE/CREDIT_CARD}` — cards require a
Luhn-valid digit run), `length` (`{"max_chars": N}`; defaults 100k in /
200k out), `webhook` (`{"url", "api_key_env"?, "timeout_s"?}` — verdict
`{"verdict": "block"|"mask"|"pass"}`; FAIL-OPEN on transport/HTTP errors,
visible in the verdict event). Webhook URLs pass the same SSRF gate
(`_check_url`) as model endpoints and MCP servers. All routes admin-gated.

Any pre_call guardrail may carry a non-empty `reply_text` (validated as a
string on every engine): a block by that guardrail is then a SOFT block —
the request still settles as a `guardrail_blocked` ledger row with the
verdict event, but the client receives a normal 200 chat completion whose
assistant message is the `reply_text` notice (stream requests get a
single-shot SSE answer), zero usage, plus an `X-PCAI-Guardrail-Blocked:
<guardrail name>` response header for clients that want to detect the
block and purge the offending turn from their conversation history. The
gateway is stateless — the purge itself is the client's job; absent the
header handling, the notice re-fires every turn (the blocked text keeps
riding in `messages`). Without `reply_text` the block is the documented
hard 400.

| Method | Path | Body / notes | Response |
|---|---|---|---|
| GET | `/admin/guardrails` | — | list (name-ordered) with attached `models` |
| POST | `/admin/guardrails` | `{name, mode, engine, config}` | 201; per-rule 400s; duplicate name 409 |
| GET | `/admin/guardrails/models` | — | `{"models": [...]}` registered model names (attachment picker) |
| PUT | `/admin/guardrails/{id}/models` | `{"model_names": [...]}` (replace-all; empty = detach all) | updated attachment list; unknown names 400 with zero writes |
| PATCH | `/admin/guardrails/{id}` | `{mode?, engine?, config?, enabled?}` | updated row (mode/engine/config validated as a triple) |
| DELETE | `/admin/guardrails/{id}` | — | 204 (attachments cascade) |

### 4.10 Admin audit trail + effective prompts

Append-only control-plane trail (the write side instruments every mutating
admin + enroll route; behavior in FEATURES.md § Admin mutation audit trail)
and the effective-prompt review surface (FEATURES.md § Agent traces →
Effective-prompt capture).

| Method | Path | Body / notes | Response |
|---|---|---|---|
| GET | `/admin/audit` | filters AND-ed: `since`/`until` (ISO), `actor_key_id`, `action` (exact, e.g. `key.create`), `resource_type`; `limit` (default 100, cap 500), `offset`; `format=json\|csv` (csv = text/csv attachment, same projection) | newest-first rows `{id, ts, actor_key_id, actor_subject, actor_source, action, resource_type, resource_id, details, request_id, source_ip}` — details are secret-redacted at write time; rows immutable, never pruned |
| GET | `/admin/effective-prompts` | filters: `key_id` (primary), `thread_id` (one conversation's per-call prompts in order), `limit` (default 50, cap 200) | newest-first captured effective prompts (post reasoning-strip, post canonical reconciliation, post trim) — the rows themselves are the bounded artifact; capture-off/faulted → `{"rows": []}` with 200 |
