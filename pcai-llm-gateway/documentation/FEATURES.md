# Features — pcai-llm-gateway

One gateway, three surfaces (`/v1` OpenAI-compatible proxy, `/mcp` federated
MCP, `/admin` control plane) and one Postgres control plane behind them.
Endpoint-by-endpoint request/response shapes live in
[`API.md`](API.md); this document describes how each mechanism actually
behaves and why.

## Budget semantics

Budgets are **ledger-derived**: a budget row says (key, model, period,
limit); the spent amount is a SUM over `usage_ledger` in the window. There
is no "remaining" column — no read-modify-write, no check-then-act race.

- **Two kinds**: token budgets (`budgets.limit_tokens`) and USD cost budgets
  (`cost_budgets.limit_usd`), both (key, model-or-`'*'`, period ∈
  `day|week|month|quarter|year`, UNIQUE per triple). A key may carry several; every
  matching budget must have headroom or the call is refused — whichever
  limit hits first stops the key. USD budgets are gate-checked post-paid
  (`spent >= limit`): an in-flight request's true cost is known only after
  the response, so overshoot is bounded by one response.
- **Off-peak-aware sums**: budget spend is FRACTION-WEIGHTED
  (`SUM(col * billing_fraction)`) — a row settled inside a low-demand
  off-peak window consumes only its fraction of budget, while the ledger
  keeps TRUE tokens/cost for audit (see [Off-peak billing](#off-peak-billing)).
  With the feature off every row carries fraction 1.0 and the math is the
  plain SUM.
- **Settle-after**: the gate runs **before** the upstream call (with a
  len//4 token estimate) and the **real** usage is settled into the ledger
  after the call — `ok`/`error` both consume, `budget_exceeded`/
  `rate_limited`/`cache_hit` rows do not (cache hits are free). Streaming
  settles in the generator's `finally` (the only hook that fires on both
  the happy path and client disconnects, shielded against cancellation so
  aborted streams still count).
- **Overshoot trade-off**: concurrent requests may overshoot a limit by
  their own in-flight usage — the correct trade, since strictness would
  mean serializing every call on a lock.
- **Visibility**: `X-Budget-Warning: <80|95>% of a period budget consumed`
  on responses (including the relay stream) when a matching budget crosses
  80%/95% — pure visibility, never a refusal. The band is the TIGHTER of the
  key's own allocation and the enrollment's TEAM envelope: a team pool at 95%
  is "about to run out" even when the key's own allocation has room.
- **`GET /v1/models` capacity projection**: every registered entry carries
  `pcai.capacity = {ok, remaining_tokens, blocked_by, warn}` for the CALLING
  key. `ok` means "the ADMISSION GATE would admit this", not merely "spent <
  limit": the per-key verdict (charged with the admission estimate) and the
  team envelope (per-model caps, pooled outer bound, overflow swallow) are the
  SAME evaluators `check_budgets` runs, so visibility cannot drift from
  enforcement. Routing pseudo-model entries carry the WINDOWED form —
  `{ok, warn, candidates: {ok, blocked}, blocked_by?}` over their
  floor..ceiling ladder — so a picker can render "Unavailable to you" and "You
  are about to run out" without guessing. Fail-open: a budget-table fault
  degrades to `ok: true` / `warn: 0` (the previous listing), never to an
  empty one.

## Rate limits

Independent of budgets: **fixed-window RPM/TPM per key** in the
`rate_windows` table (1-minute windows), incremented **atomically** via
`INSERT ... ON CONFLICT DO UPDATE ... RETURNING` on every accepted request.
Limits come from the key record (`rate_limit_rpm`, default 60;
`rate_limit_tpm`, default 100 000). A monthly token budget does not stop a
10k-RPM runaway agent — the 1-minute window does. Over-limit requests are
refused (429, `Retry-After`) and ledgered as `rate_limited`; admitted calls
carry `X-RateLimit-Remaining-Requests` / `-Tokens` so OpenAI-compatible SDK
clients back off natively. MCP tool calls (0 tokens) consume the RPM axis
only — TPM is blind to them.

## Key model

- **Generation**: `hllm-` + 32 random bytes (urlsafe) — ~256 bits.
- **Storage**: SHA-256 hash only (`key_hash`, UNIQUE); the first 10 chars
  are kept as `key_prefix` for UI display. **Plaintext is returned exactly
  once at creation or rotation and never recoverable.**
- **Scopes**: `scopes TEXT[]` — `{proxy, mcp, admin}`; a request is 403 if
  it needs a scope the key lacks.
- **`allowed_models`**: empty = all registered models; non-empty = a hard
  boundary (also intersected with profile cascade orderings — ordering
  entries the key may not use are skipped, not denied).
- **`max_concurrent`** (default 0 = unlimited): caps the key's simultaneous
  in-flight requests — RPM/TPM bound request *rate*, this bounds
  long-lived streams. The live count is per-process (per pod); the slot is
  held for a stream's lifetime and released on every exit path.
- **`relay_default`**: when TRUE the tool relay engages WITHOUT the client
  sending `X-PCAI-Execute-Tools` (for off-cluster consumers whose cluster
  MCPs are external to them). Three-state per request: the header always
  wins (`1` forces on, `0` opts out); absent → the key default. The global
  kill switch (`PCAI_LLM_TOOL_RELAY_ENABLED=false`) wins over everything.
- **Lifecycle**: `expires_at` and `revoked_at` are enforced at every
  authentication (fail closed — unknown/revoked/expired → 401). **Editing**
  is `PATCH /admin/keys/{id}` in place — name, scopes, models, limits,
  concurrency, expiry (explicit JSON null clears), relay default — live
  immediately (keys are read from Postgres per request); hash/prefix are
  never editable. **Rotation** (`POST /admin/keys/{id}/rotate`) is a
  server-side one-call flow — see the next bullet.
- **Rotation**: `POST /admin/keys/{key_id}/rotate` mints a NEW key with the
  SAME name and attributes copied verbatim from the old row (scopes,
  allowed_models, RPM/TPM, `max_concurrent`, `expires_at`, `relay_default`),
  then revokes the old one — create-first-then-revoke, so every partial
  failure OVER-provisions (a crash between the two writes leaves both keys
  live; the reverse order could strand every client of the old key on 401s
  with no replacement in hand, since the plaintext only ever exists in the
  create response). Fail closed: unknown id → 404; an already-revoked key
  refuses to rotate (409) BEFORE any write — a revoked row is audit
  history, not a live credential to re-issue from. The 201 returns the full
  plaintext ONCE (`{id, key, key_prefix, old_key_id, old_revoked,
  grants_copied, creds_copied, budgets_copied}`; the DB keeps only the
  SHA-256 hash and the plaintext is never an INSERT parameter — no recovery
  path). The server CARRIES OVER the key's identity rows onto the new id in
  one INSERT..SELECT each, verbatim: the `mcp_tool_acls` grant rows
  (federation grants, allow AND deny — the 2026-09-18 incident fix), the
  `mcp_server_creds` per-server credential-override rows (the memory
  dataset/password identity — losing them forced re-entering them after
  every rotation), and the budget LIMIT rows (`budgets` + `cost_budgets`,
  ON CONFLICT DO NOTHING — a raw-API rotate used to leave the fresh key
  with no budget rows at all, i.e. unlimited; the console's post-rotate
  budget re-POST stays as an idempotent no-op). **Spend survives rotation**:
  the new key inherits `spend_ancestors = old.spend_ancestors || [old.id]`,
  and the budget gates + console budget view sum ledger rows over the whole
  lineage (`key_id = ANY(spend_ancestors || id)`), so rotating can never
  reset an in-window period budget — the SSO self-mint rotation
  budget-reset bypass (a user-rotatable key resetting its own counter
  indefinitely, fixed 2026-10-09). Calendar windows are NOT carried: the
  in-window counter survives, day/week/month boundaries reset normally.
  RPM/TPM counters still reset on rotate (1-minute burst windows). Console
  UX: one shared show-once reveal at create and rotate ("Shown only once —
  copy it now; it cannot be recovered.") with one-click copy, and the keys
  list stays prefix-only with a muted full-key-shown-once hint — the old
  client-side reissue flow (a renamed copy that kept the old key alive) is
  removed.
- **Key material** appears in no projection, ever; the ledger references
  revoked keys so history stays attributable.
- **`stream_pcai_events`** (per-key, default TRUE — RFC-PER-KEY-STREAM-EVENTS):
  relayed streams carry the in-stream `{"pcai": …}` SSE telemetry events by
  default (the console's live rendering depends on them), but those events
  are not valid OpenAI chunks — strict validating clients (opencode
  @ai-sdk/openai-compatible, clearwing openai-py ≥ 2.45) abort the stream on
  the first one. A key flipped OFF (create body or `PATCH /admin/keys/{id}`,
  console: Keys ▸ edit, `no-events` pill on flipped keys) gets a byte-clean
  spec-compliant stream: chunks, `reasoning_content`, the standard
  `include_usage` chunk, the `{"error": …}` envelope, `[DONE]` and keepalive
  comments — the telemetry is suppressed, nothing else changes. Per request
  the header `X-PCAI-Stream-Events: 1|0` overrides the key default (same
  three-state shape as `X-PCAI-Execute-Tools`; there is deliberately NO
  global kill switch — the key default + header are the whole surface).
- **Token shadow-keys** (identity Phase B; `PCAI_LLM_TOKEN_SHADOW_KEYS`,
  chart key `settings.tokenShadowKeys`, default OFF): a first-seen VALID
  IdP token subject lazily provisions user + ledger key
  (`bound_via='token-shadow'`, scopes proxy+mcp) so usage attribution
  keeps its `key_id` anchor when callers authenticate with short-lived
  tokens instead of virtual keys. Limits come from
  `settings.tokenShadowRateLimitRpm` / `tokenShadowRateLimitTpm`
  (defaults 60 / 100 000 — config-sourced; the console can tighten them
  per key afterwards, exactly like enrollment-created keys); shadow keys
  are created under `settings.tokenShadowTenantId` (REQUIRED with the
  flag — without a tenant the flag is inert) named
  `settings.tokenShadowKeyName` (`token-shadow`). Lookup is
  UNIQUE (idp, subject) → newest non-revoked shadow key. Off = tokens
  validate but never provision (the anonymous-fp posture).

## Tool-use relay (server-side agentic loop)

- **What**: `X-PCAI-Execute-Tools: 1` on `/v1/chat/completions` (or a key's
  `relay_default`) makes the gateway EXECUTE the model's tool calls
  in-cluster — against the key's MCP grants — and feed results back,
  looping until the model answers or a guard trips. The whole loop runs
  in-cluster; only the final answer + tool events cross to the client.
  Latency win: no VPN round-trip per tool call.
- **Tools**: named `<server>__<tool>`; with the relay on, the tools array
  is INJECTED from the key's effective grants — exactly its HELD pairs (the
  profile's live-grant presets ∪ its own ACL rows; the `explicit` grant mode
  is the shipped default — the profile IS the envelope, and a withheld server
  is invisible and uncallable no matter what scopes the key carries). The
  legacy opt-in `scope` mode re-adds every policy-admitted tool of enabled
  servers for mcp-scoped keys — only for deployments that grant mcp scope
  deliberately. Default-deny holds INSIDE the loop: a tool
  the key lacks is reported to the model as denied, never executed. The
  relay executor re-enforces the per-server tool policy and the key's RPM
  window — the automated path is never a policy bypass.
- **Guards** (AgentBuilder lessons, ported): max iterations (16 — one
  iteration is ONE model round, which may batch several parallel tool calls,
  not one tool call; sized for serial research turns: search + a fetch per
  page + synthesize; the cap bounds ONE user turn's relay loop — a
  handback-continuation request resumes the turn's counter, a fresh user
  message resets it), wall-clock
  cap (`settings.toolRelayWallClockS` — chart default 900 s, clamped in
  code to the upstream timeout minus the 30 s
  ingress headroom so a burned budget still closes the stream before the
  VS deadline), identical-call loop detection (abort + one clean wrap-up
  pass without tools), and a **ledger settle for every model call AND every
  executed tool call** (model rows via `chat` with observed upstream usage;
  tool rows as 0-token `mcp:<server>:<tool>` rows). A mid-relay budget
  re-gate stops the loop cleanly on exhaustion — iterations 2..N are new
  model calls the pre-loop gate never saw.
- **Bounded retry-once** (`toolRelayCallRetries`, default 1): a bounded
  model call that expires on timeout retries once PER TURN when the
  remaining wall clock affords another attempt (≥ 10 s) — the retry budget
  comes OUT OF the remaining wall clock (the guard deadline never moves,
  so the stream always closes cleanly before the ingress timeout);
  accumulators reset per attempt, each attempt settles its own ledger row,
  and a second timeout falls through to the unchanged clean close. `0`
  restores the immediate clean-close behavior. The wrap-up pass is never
  retried.
- **SSE protocol**: intermediate steps stream as
  `data: {"pcai": {...}}` events — `thinking` (reasoning deltas),
  `iteration` (intermediate text), `tool_call`/`tool_error` (server, tool,
  ok, ms, `args`, truncated `result`), `guard`, `model_usage` — then the
  final answer as one normal OpenAI `chat.completion.chunk` and `[DONE]`.
  The `X-PCAI-Relay: streaming` response header marks the mode. The chat
  UI renders tool steps as expandable chips (args + result preview) and
  thinking inline. A key with `stream_pcai_events=false` (or a request
  carrying `X-PCAI-Stream-Events: 0`) gets the same turn WITHOUT the
  `{"pcai": …}` events — a byte-clean OpenAI-spec stream for strict
  validating clients (see Key model).
- **Tool-result compression** (`X-PCAI-Compress-Tools: 1|0|shadow`,
  default OFF): deterministic, model-free selection over oversized tool
  results. A recognised kind (JSON array, NDJSON, logs, search output, diff)
  keeps its signal — error records, statistical outliers, boundary context,
  ERROR/stack-trace/summary lines, first matches per file, changed lines —
  and drops the redundancy; unknown shapes, small results (< 4,096 chars) and
  marginal savings (< 15%) pass through untouched. The original is stored
  under its content digest and the marker tells the model to call the
  synthetic `pcai__retrieve` tool (offered on every iteration of an ON turn,
  so the tool list is stable and the cached prefix survives). No LLM, no
  network, no second request: the transform is a pure function of the text,
  which is what makes it cache-safe and cheap enough for the request path.
  `shadow` reports the saving via `pcai.compress` events without rewriting.
  A store failure fails the COMPRESSION, not the turn (the original passes
  through). Retention is `toolCompressStoreTtlS` (0 disables reclamation).
- **Request semantics**: the relay pre-flight (grants check) runs BEFORE
  the gates — a grantless relay request cannot leak its concurrency slot —
  and a catalog fetch per granted server happens before the first token
  (bounded at 15 s; a hung MCP server delays TTFT once, not 600 s).
- **Request-body opt-in** (OpenAI-idiomatic, no custom header):
  `tools:[{"type":"mcp","server"?:"…","require_approval"?:"never"|"always"}]`
  engages the relay per request (default), narrowed to `server` when
  present. `require_approval:"always"` is the human-in-the-loop mode: the
  granted catalog is exposed as plain OpenAI function tools (with the
  upstream `inputSchema`s) and the model's `tool_calls` are returned
  UNTOUCHED for the client to execute — the gateway never runs them.
  mcp-type entries are stripped from the payload forwarded upstream; an
  explicit `X-PCAI-Execute-Tools: 0` still wins over a body execution
  request (loud 400, not a silent ignore).

## Tool-definition pinning (rug-pull defence)

An MCP server can rewrite a tool's `description` or `inputSchema` AFTER the
operator approved the grant. The ACL admits the NAME, so the model is then
offered a definition nobody reviewed — and a rewritten description is an
instruction channel straight into its context. This is the "rug-pull" class in
the OWASP MCP guidance.

- **What is pinned**: a 16-hex sha256 over a canonical JSON encoding of
  `{name, description, inputSchema, annotations}` (`core/tool_pin.py`,
  pure and I/O-free). Sorted keys and whitespace normalisation make the digest
  stable across runs and dict ordering; `annotations` is included because
  `destructiveHint`/`readOnlyHint` are exactly what a rug-pull would flip.
  The relay catalog carries all four fields (`api/routes_proxy._relay_catalog`)
  so the pin covers the whole definition, not just its prose.
- **When it is observed**: once per relay request, at the catalog → offered-surface
  boundary (`toolrelay.pin_catalog_with_verdicts`). The verdict is
  FIRST_SEEN / MATCH / CHANGED per `(server, tool)`; pins are in-memory, so a
  restart re-records them (a persisted pin table is a deliberate non-goal of
  this cut — it needs a schema migration).
- **Modes** (`tool_pin_mode`): `off` inert; `warn` logs the old → new digest
  and keeps offering the tool; `block` drops the changed tool from the
  OFFERED surface **and** refuses the call at dispatch
  (`toolrelay.pin_dispatch_refusal`). Both halves matter — the offer alone is
  bypassable by history replay, where the model names a tool it saw in an
  earlier turn. A refusal is an ordinary `role=tool` error naming
  `server__tool` and both digests, so the relay loop keeps iterating. Unknown
  values fall back to `warn`: a typo must neither silently disable the defence
  nor block every tool.
- **A VANISHED tool is a rug-pull too.** Dropping a definition from
  `tools/list` used to leave the call ungated — the ACL admits by NAME, so a
  server could pin D1, rewrite to D2 and then simply stop listing the tool,
  and the executor would run D2 forever. In `block` mode a tool that this
  store has pinned, that this turn's grants expect, and that the listing no
  longer carries is refused at dispatch ("vanished from the listing since this
  grant approved it") and reported during the offer pass. A tool that was
  NEVER pinned still dispatches: the defence must not break a first run.
- **Fail-open on fault**: a fingerprint error or a missing verdict for an
  unpinned tool dispatches the call. A pinning bug must never break a turn.

## Tool-result injection screening

The gateway is the only component that sees every MCP tool result before it
re-enters the model. `core/guardrails.py` covers user text pre-call and
assistant text post-call; tool output is the third channel and was unguarded.

- `core/tool_injection.py` is a pure, model-free scanner over four families:
  instruction override, role/control-token spoofing, exfiltration shapes
  (destination + credential in one payload), and encoded-payload smells.
  Severity is `high` only for a clear override or role spoof; everything else
  is `low`, because a false positive costs tokens while a missed override
  costs the turn.
- `tool_injection_screen`: `off` inert; `warn` logs and passes through;
  `annotate` prepends a short machine-readable provenance header naming the
  server, tool and finding count WITHOUT removing content; `block` replaces
  the body with a short refusal that keeps the tool-call shape valid. Applied
  in `_prepare_tool_content` (in-cluster, discovery meta-tool and client
  results) and on the `pcai__retrieve` path, so a withheld original cannot be
  re-fetched unscreened. Unknown values fall back to `warn` (and log once —
  a typo like `enforce` must not silently disable the gate).
- **Two shapes are screened, not one.** The detector runs over the
  JSON-serialized envelope AND the unwrapped single-text-block candidate, and
  takes the worst severity. Screening only the envelope was a real blind spot:
  inside `json.dumps` a newline rides as `\n`, so the role-label detectors
  could never match the default (compression-off) tool-result shape.
- **Normalised matching.** Text is NFKC-normalised, stripped of
  format/zero-width/bidi/soft-hyphen characters and homoglyph-folded before
  phrase matching, with reasons quoted back from the ORIGINAL text. Without
  this, `іgnore` (Cyrillic і), `ig\u200bnore`, `ig\u00adnore` and
  `ignore\nall\nprevious\ninstructions` all scored clean in every mode.
- **The provenance header is not forgeable.** `annotate` emits a fresh
  per-result `token=<16 hex>` that appears only in the real header, and a body
  that hand-writes the gateway's own header shape is itself a
  `provenance_spoof` finding — otherwise the model would see two
  indistinguishable headers and could be talked into trusting the forged one.

## Content guardrails

Per-model content policy, ported from the hpe-llm-gateway design and
reimplemented on the ledger. Named guardrails attach to virtual model
names (`model_guardrails` → `model_endpoints.model_name`); every request
against the model runs its enabled guards in name order.

- **Engines**: `blocklist` (keyword deny/mask, every occurrence masked),
  `regex_pii` (email / phone / credit-card masking — cards only when the
  digit run is Luhn-valid, so order numbers and phone-shaped runs are not
  flagged), `length` (character caps, 100k in / 200k out defaults),
  `webhook` (external moderator POST, verdict `block|mask|pass`).
- **Enforcement is strict; delivery is fail-open.** pre_call blocks refuse
  the request (400, `guardrail_blocked`) BEFORE the gates — a policy block
  never consumes rate window, budget, or a concurrency slot. post_call
  blocks refuse the ANSWER (400) while the ledger keeps the REAL upstream
  spend on the `guardrail_blocked` row — the call happened, the audit must
  see it. A webhook that is unreachable, erroring or non-JSON degrades to
  pass-with-event: a flaky moderator must never take the data plane down,
  and the failure stays visible. A broken policy LOOKUP fails open the
  same way (logged).
- **Guards compose**: each engine sees the previous engine's output
  (mask-then-scan); the first block stops the run.
- **Verdicts are ledger rows**: every guardrail yields exactly one event
  per run — pass included — stored as `usage_ledger.guardrail_events`
  JSONB next to the cost facts. Filter/group in the usage explorer; the
  "what did the policy do" story never needs a second store.
- **Streaming**: buffered when the model carries post_call guards — pass
  replays the stream byte-identical, masked suppresses the content deltas
  and resynthesizes one final chunk (the mask cannot leak through a
  partially-delivered delta), blocked discards the content and emits a
  terminal SSE error envelope. Non-relay verdicts ride the request's main
  ledger row; relayed requests judge the FINAL answer and record verdicts
  on a 0-token `chat:guardrail` marker row (relay hop rows stay pure cost
  facts).
- **Admin**: CRUD + per-model attachment at `/admin/guardrails`
  (§4.8 in API.md, console Guardrails tab). Webhook URLs pass the same
  SSRF gate as model endpoints and MCP servers.

## Agent traces

The observability twin of the usage ledger: `settle()` answers "what did
this cost", `agent_traces` answers "what did the agent DO and how long did
each hop take".

- **One row per relay step**: phase `chat` per model iteration (wall-clock
  ms, prompt/completion tokens, status `ok|error|budget_exceeded`) and
  phase `tool` per tool call (server, tool, ms, status
  `ok|error|denied|rate_limited`), all grouped by `request_id`, attributed
  to key + user.
- **Best-effort writes**: `record()` swallows its own failures — a broken
  trace pipeline must never fail a request the ledger already settled.
  Keyless (unit-test) relay runs write nothing.
- **Reads**: `GET /admin/traces` (filters `user_id`/`key_id`/`since`,
  request-grouped, newest first) and `GET /admin/traces/{request_id}`
  return the full timeline in one query — steps in ts order plus derived
  totals (`tool_ms`, `chat_ms`, call count). The console Traces tab renders
  a conversation as ONE stacked step table: each request in the thread is a
  turn-header row (double rule above, per-user shade) whose step rows
  follow back-to-back — no per-request cards or repeated table headers.
  `Tokens (in + out)` on a step bills the model call (tool rows are 0+0 —
  their results ride the NEXT step's input).
- **Client handbacks are traced** (status `handed_back`, `server='client'`,
  `ms` NULL): when the model calls tools the CLIENT offered — a harness
  running its own loop (DSH, sproket) — the relay hands the calls back and
  the client executes them OUTSIDE the gateway. Without these rows every
  such request degenerated to "1 step, 0 tools", hiding what the agent
  actually asked to do; with them a DSH turn reads
  `2 steps · 1 handed to client · model 3.7s`. Loop-guard-refused client
  calls trace as `denied` (they were never handed anywhere). Handback rows
  never inflate `tool_ms` (no server wall-clock exists).
- **Harness-prefixed tool names never wedge the relay** (the name-duality
  fix): a harness that wraps the gateway's own `/mcp` endpoint under its
  connection prefix (opencode's `mcp__pcai-llm-gateway__<server>__<tool>`)
  offers each tool under TWO spellings — its own prefixed form and the
  relay's bare `<server>__<tool>` offer. Twin matching (`shadow_client_mcp`)
  is suffix-based over the `__`-separated tails, so the double-prefixed
  client tool is deduped from the OFFER (no dual spelling for the model to
  imitate) and mixed-batch handbacks rewrite correctly. A model call that
  still arrives under the harness's spelling (history replay predating the
  offer merge) ALIASES to the granted in-cluster twin instead of erroring
  `unknown tool` or ACL-denying on a bogus parsed server — suffix match
  against the granted catalog, never a guess (ambiguous tails alias
  nothing), and the grant gate re-checks the resolved pair. Refusals on
  gateway-shaped names append the granted-tool list so the model can
  self-correct in one step.
- **Effective-prompt capture** (transparency wave; on by default —
  `PCAI_LLM_EFFECTIVE_PROMPT_CAPTURE`, app-level config, not a chart
  lever): with the inbound reasoning strip + canonical reconciliation in
  place, "the conversation the user reads is not the conversation the
  model received" — so every relayed model call records the EFFECTIVE
  prompt (stripped, reconciled, post-trim) and `GET
  /admin/effective-prompts?key_id=&thread_id=&limit=` reads them back
  newest-first (bounded previews; rows retained ~7 days by an
  opportunistic prune). Strictly observational: any fault logs a warning
  and the relay turn proceeds untouched. Debugging aid, not a second
  transcript store.

## Prometheus sweep agent (built-in preset)

- **What**: `prometheus-sweep` — a curated agent profile that turns the
  cluster's current alert set into one prioritized, evidence-backed JSON
  report. It is not a new route: it is an agent profile installed through
  `POST /admin/agents` (an upsert) and selected by naming it as the request
  `model`. The definition lives in `core/agent_presets` (reviewed and tested
  as code); `scripts/seed_agents.py` seeds it as config. No boot-time writes.
- **Report-only by construction**: its servers are `prometheus`, `k8s-ops`
  and `logsearch`, each narrowed to read tools. `applygate` is absent,
  k8s-ops is narrowed past exec/kubectl/secrets, logsearch past its export
  writer — the strongest mutation the agent could attempt is one its surface
  does not contain. The prompt states the same contract; the grant
  intersection is what enforces it.
- **Deterministic where it matters**: a model writes the prose, so runs are
  not byte-identical. What IS fixed: the numbered tool order (one
  `prom_alerts`, one `prom_rules`, then at most two evidence calls per
  defect), the clustering rules (one defect per alertname+namespace+workload;
  a stuck Job's three alerts are one defect), the priority ladder (P1 active
  damage / self-escalating, P2 schedule, P3 noise or expected), the
  classification vocabulary (leak, plateau, undersized, crashloop, capacity,
  config-drift, monitoring), the sort keys, and exactly one JSON object as the
  reply. `prompt_version` is echoed into every report, so a stored report's
  provenance is answerable without reading the agent row.
- **Alert hygiene is encoded**: a cache-tier workload at its plateau (HiCache
  predictor, KV/engram table sized to a budget) is P3 with a growth-ladder
  recommendation, never a P1 — the plateau IS the healthy steady state, and
  chronic criticals train operators to ignore alerts.
- **Honest about budget**: the relay's per-turn tool budget
  (`toolRelayMaxIterations` — chart default 32, the reference g2 site 16) is
  smaller than a full sweep. The agent investigates top-down, P1 first, and
  marks what it did not reach `unverified` with cause "not investigated: tool
  budget"; every firing alert still appears in exactly one defect or in
  `noise`, and a relay that ends tool use for budget still gets a report. A
  profile offering the agent should raise `tool_relay_budget`.
- **Install**: `scripts/seed_agents.py` — `--dry-run` validates server names
  against the live registry and tool names against each server's live
  `list_tools`; `--offer-to-profile` appends the agent to a profile's
  selection (union, never removal). An unreachable MCP server downgrades its
  name check to a warning: installing config must not depend on every server
  being up.

## Saved conversations (per key group)

- **Key-GROUP-scoped server-side transcripts**: every conversation belongs to
  the credential's `key_group` — the rotation-stable identity every rotate
  copies forward — and every query filters by it, so a foreign group is a
  plain 404 with no existence oracle. `key_id` stays on the row as
  provenance (which credential created it). A rotation (the admin rotate, or
  the SSO "Sign in with SSO" replacement) therefore keeps the user's history;
  independent keys of one user get their own group and stay isolated.
  Routes behind the SAME bearer auth as the proxy (any valid key — no extra
  scope).
- **Replace-all storage model**: the client pushes the FULL transcript per
  turn (`PUT /v1/conversations/{id}/messages`); the delete+insert swap is
  one transaction, so a concurrent reader sees the old or the new
  transcript, never a mix. Idempotent per turn by construction.
- **Caps enforced app-side** (the DB never becomes the abuse surface):
  ≤200 messages, ≤32 000 chars each, titles ≤120 (auto-titled from the
  first user message's first 60 chars). Over-cap is 413; malformed is 400;
  this surface answers `{"error": ...}` bodies, not FastAPI's `detail`.
- **Why server-side**: the chat console's left panel survives tabs and
  devices; the transcript is the caller's data (a keyholder moving between
  machines needs no local storage).

## Self-service enrollment (codes → keys)

- **The admin-burden killer**: instead of the admin creating user + key +
  budgets + grants per newcomer, the admin mints a CODE bound to
  tenant (+ optional profile) — `POST /admin/enrollments` — and hands it
  out. The holder redeems at the PUBLIC `POST /v1/enroll` (`{code, name}`)
  and gets a personal `hllm-` key with `proxy`+`mcp` scopes, and the
  profile's preset budgets + MCP grants stamped on at creation in ONE
  transaction. A preset budget is ONE limit kind per entry — a **token
  budget** (`limit_tokens` → `budgets`) or a **dollar budget**
  (`limit_usd` → `cost_budgets`) — and BOTH kinds are stamped: the token
  presets land in `budgets`, the dollar presets in `cost_budgets`, inside
  that same transaction (all-or-nothing, like the grants).
- **The code is the credential** (no auth on `/v1/enroll` BY DESIGN).
  Brute force is answered by entropy and silence, not by an auth gate:
  32-byte urlsafe codes, SHA-256 hash-only storage (a DB leak cannot redeem
  anything), ONE generic rejection message ("invalid, expired, or exhausted
  code" — no expired/exhausted/revoked oracle), an atomic single-statement
  redeem that cannot double-spend past `max_uses` (Postgres re-evaluates
  the WHERE clause for the losing concurrent redeemer), and a per-IP
  redeem throttle (10 attempts/hour → 429).
- **Admin management**: `GET /admin/enrollments` (usage state, no hashes
  ever leave the DB), `POST /admin/enrollments/{id}/revoke`. Name
  collisions inside a tenant are resolved with a short random suffix —
  keys are identity, names are labels.
- **Lifecycle**: `max_uses` (0 = unlimited), `expires_days`, revocation.
  Reissue = mint a new code.

## SSO self-mint (browser identity → key)

- **The second self-service path** (identity Phase B): behind the
  platform's oauth2-proxy chain a BROWSER identity resolves without any
  key — the identity ladder's browser-header rung (`X-Auth-Request-*`
  headers an enforcing proxy overwrites on every request; the ezaf-gateway
  edge classifies these paths, not the app). Two flags gate the surfaces
  (`settings.trustProxyIdentity`, `settings.ssoKeyCreateEnabled` — both
  default OFF) and are INERT until the coupled rollout in
  [`DEPLOYMENT.md`](DEPLOYMENT.md) § SSO self-mint rollout lands: flipping
  them before the edge chain + AuthorizationPolicy enforce trusts
  spoofable headers (the AuthorizationPolicy is what makes the headers
  un-spoofable in-cluster — the two moves are atomic).
- **`GET /v1/identity`** — the public whoami probe the chat page calls on
  boot: resolves the browser identity (subject + groups) or returns the
  exact 6-key anonymous anti-oracle shape (no oracle for "which header
  failed"); 503 fail-closed on resolver errors. The green identity chip
  renders from it whenever a subject resolves — identity DISCLOSURE,
  independent of the mint flag: you can SEE you're on SSO before you can
  MINT.
- **`POST /v1/keys/for-me`** — one key per browser identity: no body, no
  subject choice, no grant choice (scopes fixed `proxy`+`mcp`, limits
  fixed 60 rpm / 100k tpm; grants stay admin-controlled). A second mint
  for a subject that already holds a live `sso-create` key is refused with
  409 unless the body is `{"rotate": true}` — rotate REVOKES the old key
  in the SAME transaction that mints the new one (never two live keys,
  never zero; a failed mint leaves the old key live) and the new key
  INHERITS the old key's `key_group`, so saved conversations survive the
  replacement. Admin-governed
  eligibility: `users.self_mint_enabled` (a first mint is default-eligible)
  via `GET/PUT /admin/keys/self-mint-eligibility`.
- **The minted profile is tiered by IdP groups (2026-11-08)**: a tenant
  holds a DEFAULT SSO profile and an optional ADMIN profile (`PUT
  /admin/tenants/{id}/sso-default-profile`; console Enroll tab). At mint
  AND rotate, when the caller's IdP groups intersect the tenant's
  `sso_admin_groups` list the admin profile is stamped instead of the
  default; the default profile otherwise; the bare fixed grant when
  neither slot is set. The PCAI admin tier is the literal `admin` in the
  Keycloak `groups` claim — there is no `member` string; absence of a
  match is "not an admin". When token validation is configured, the groups
  come from the JWKS-VALIDATED id_token, which outranks the proxy-injected
  `X-Auth-Request-Groups` header — but ONLY for the token's OWN subject
  (sub / email / preferred_username): a valid token identifying a DIFFERENT
  person is ignored, so a leaked admin id_token cannot stamp its groups
  onto another caller's key. The header stays the fallback — an IdP blip
  must not fail a mint the chain already authenticated. On ROTATE the user
  row follows the tier when it points at one of the tenant's two SSO
  slots; a hand-assigned profile is never re-scoped. Scopes, the bare
  grant and the eligibility gate are unchanged; an empty group list turns
  the split off and mints byte-identically to the single-profile behavior.
- **The chat page keeps the association (2026-11-03)**: the minted key is
  stored with SSO PROVENANCE, so the chip keeps rendering "Signed in as
  <subject> · oidc" after sign-in and across reloads — boot auto-connects
  the stored key in the SSO flavor (`connectKey({sso:true})`) instead of
  flipping to "API-key user" the moment the mint lands. The mint card is a
  FIRST-mint affordance only: it hides as soon as the browser holds a
  working key, so the old loop ("clicking the SSO button ALWAYS gets me a
  new rotated key") cannot happen. An SSO key is auto-remembered in
  localStorage — it is this browser's device credential, unlike a pasted
  secret — while the remember box stays visible and un-checkable. A browser
  WITHOUT the key (new tab/device, cleared storage) sees the chip admit
  "no key on this browser yet", and the 409 dialog NAMES the existing key
  (prefix + created-at from `GET /v1/keys/for-me`) before offering the
  destructive replace, stating that conversations follow the key group.
- **The `X-API-Key` envelope** — behind a chained host the proxy injects
  `Authorization: Bearer <id_token>` into every forwarded request, so the
  browser SPA cannot deliver a minted virtual key via the Authorization
  header at all (the MM RAG D25 lesson). `X-API-Key: hllm-…` is the
  additional envelope for exactly that posture. Precedence is
  DE-ESCALATION-ONLY (fleet convention D19): an explicit envelope
  OUTRANKS the co-forwarded proxy Bearer (a 30-min identity token) because
  the envelope is the caller's own long-lived ledger key for the SAME
  subject; a DEAD envelope is never rescued by it (hard 401 — two keys
  presented with one dead is a refusal, never a silent rescue; a dead
  envelope must not escalate to the token rung on the Bearer's behalf).
  With NO envelope present resolution is byte-for-byte the pre-envelope
  behavior. REST (`require_key`) and `/mcp` share ONE resolver, so the
  two surfaces cannot drift. The admin console sends the envelope too for
  chained deployments (inert on the skip-auth `/admin` paths — see
  DEPLOYMENT.md § SSO self-mint rollout step 3).
- **Token shadow-keys** (the headless twin of this feature — first-seen
  token subjects lazily get a ledger key for attribution) are covered in
  § Key model above.

## Per-key MCP credential overrides

- **The multi-tenant memory problem**: some MCP servers key tenant identity
  on request headers (the memory-MCP pattern: `X-API-Key` holding a
  user-minted RAG key; the legacy `X-Memory-Dataset` / `X-Dataset-Password`
  pair addresses a dataset directly). Server-level auth is one fleet-wide
  identity — per-USER identity needs per-key headers.
- **Storage**: Fernet-encrypted JSON `{header: value}` per
  (key_id, server_name) in `mcp_server_creds`, keyed off the same master
  key as every panel credential. **Write-only**: PUT replaces (upsert),
  GET lists which servers + which header NAMES only, DELETE clears —
  plaintext values never return from any endpoint. Names must be
  `[A-Za-z0-9-]+` (≤16 headers, values ≤512 chars — they become upstream
  request headers), and an empty PUT is refused (clearing is DELETE's job).
- **Merge order**: server-level headers first (static headers + by-ref
  Secret + panel token), then the key's overrides merged LAST-WINS — a
  tenant override may ADD headers or REPLACE a server-level one. Only the
  CALL paths use them (direct `/mcp`, the relay executor); catalog/metadata
  fetches stay key-independent.
- **Separation of concerns**: credentials are not grants — a key can carry
  memory headers for a server whose tools it has not been granted.

## Enrollment-time memory datasets (LEGACY opt-in at redemption)

> **Legacy path (2026-11-04).** The current RAG model is SSO-managed: a user
> mints a key in the RAG app (`/access`), ★-binds a dataset there, and the
> backend resolves dataset+password from that key — the gateway then needs
> only the `X-API-Key` override. The provisioning below remains for explicit
> API/profile callers; the redeem dialog no longer offers it and the console
> no longer models a dataset.

- **The admin-burden loop, closed**: the per-key credential override above
  still needed an admin to hand-create each user's dataset and paste the
  header pair into the creds dialog. The REDEEM itself can do it: the
  redeem request gains `memory_opt_in` (`bool | null`) — `null`/omitted
  falls back to the deployment default (`PCAI_LLM_ENROLL_MEMORY_DEFAULT`,
  chart `settings.enrollMemoryDefault`, **false**), and a profile bound to
  the code can preset the cohort with `"memory": {"provision": true}` in
  its `defaults_json` (request > profile > deployment default).
- **Provisioning**: the gateway resolves the RAG server by REGISTRY NAME
  (`PCAI_LLM_ENROLL_MEMORY_SERVER`, default `mm-rag`) from the same
  `mcp_servers` snapshot the federation layer serves, derives the REST
  origin from that row's registered MCP URL (the `/mcp` suffix stripped —
  no free-form URL env exists; the SSRF gate stays the admin registration's
  `_check_url`), authenticates with the SAME server-level credential
  resolution every MCP call uses (panel token first, then the by-ref
  Secret env), and `POST`s `{name, password}` to `/api/datasets`.
- **Naming**: `mem-<tenant>-<user>-<key6>` (key6 = first 6 hex of the key
  id) — human-readable, grammar-sanitized to RAG's `[A-Za-z0-9._-]`, and
  globally unique even across two "Alice"s (the key fragment
  disambiguates). A 409 (name taken — a re-redeem or a squatter) gets a
  bounded suffix retry (`-2`, `-3`, … `enrollMemoryNameRetries` extra
  attempts) — never a blind adopt of a name the gateway can't prove it
  owns.
- **Bind, don't display**: on success the pair is stamped into
  `mcp_server_creds (key_id, server)` through the same write-only Fernet
  path the console uses — it merges into THAT key's relay calls to THAT
  server only. The generated password rides the redemption response ONCE
  (alongside the one-time key), then lives only encrypted.
- **Never fatal, never deleting**: the key is created FIRST (create →
  provision → stamp), so every failure mode — RAG unreachable, HTTP 5xx,
  registry row absent, master key unset (nothing is even created in that
  case) — reports `memory: {provisioned: false, error: …}` and the key
  still comes back live. The gateway PROVISIONS and BINDS, never deletes:
  dataset deletion stays a RAG-side operation (RAG's delete requires the
  dataset password, which only the gateway's stored row and the one-time
  response know); the gateway's only "delete" is clearing its own creds
  row (orphaning the dataset — visible in RAG's list).
- **Rotation**: `mcp_server_creds` rows copy verbatim on rotate (the
  2026-09 fix), so the memory identity rides to the new key with no
  re-encryption and no re-prompt.

## Off-peak billing

Demand-shaped budget consumption: GPU demand is spiky — a tenant burning
budget at 3 AM costs the fleet no foreground capacity, so charging their
budget full price wastes headroom.

- **Evaluator**: a background loop (default every 30 s) samples a
  configurable PromQL (default `avg(avg_over_time(DCGM_FI_DEV_GPU_UTIL[5m]))`)
  through the REGISTERED Prometheus MCP server — in-cluster, behind the
  same registry + header resolution every other MCP caller uses — and
  hysteresis-switches a low-demand state: value < `enter_pct` (20) → active;
  value > `exit_pct` (35) → idle; between → UNCHANGED (no flapping at the
  boundary). Off by default; enable only where the Prometheus MCP is
  actually deployed.
- **Fraction precedence** (per request): the key's PROFILE fraction
  (`profiles.offpeak_fraction`, console-editable, read at authenticate —
  live on the next request) when set, else the gateway env default
  (`PCAI_LLM_OFFPEAK_FRACTION`, 0.5). The EFFECTIVE fraction is the base
  during an active low-demand window, else 1.0.
- **Ledger truth**: each settle stamps the row with the fraction in effect
  AT that moment (`usage_ledger.billing_fraction`); the ledger keeps TRUE
  tokens/cost. Only the BUDGET GATES consume `tokens × fraction` /
  `cost × fraction` — a 0.5 window makes every budget last 2×.
  `fraction()` is read fresh per settle, so a mid-relay flip is honored per
  iteration.
- **Fail-safe everywhere**: disabled / never-checked / evaluator error /
  idle → 1.0 (never discount by accident). Any evaluator failure lands in
  `last_error` at full price; WARNING only when the error signature changes
  (no 2 880 identical warnings/day).
- **Result parsing is defensive**: JSON parsed structurally (a Prometheus
  vector sample is `[epoch, "value"]` — the value rides LAST, a naive
  "first number" would read the epoch); plain text falls back to the first
  numeric token EXCEPT error-shaped text; `isError=true` tool results are
  rejected outright (an HTTP 400 status code is not a GPU utilization).
- **Visibility**: `X-PCAI-Billing: offpeak-<fraction>` on responses billed
  at a discount (normal billing adds no header); `GET /admin/offpeak` is a
  read-only snapshot (state, thresholds, last sample/error); console
  Off-peak tab auto-refreshes while visible.
- **Evaluator knobs** (chart keys under `settings.`, env
  `PCAI_LLM_OFFPEAK_*` — full defaults in DEPLOYMENT.md § values →
  environment variables): `offpeakCheckS` cadence (30), `offpeakEnterPct` /
  `offpeakExitPct` hysteresis (20 / 35), `offpeakPromql` global demand
  query (DCGM GPU util, 5 m window), `offpeakFraction` gateway default
  fraction (0.5), and `offpeakMcpServer` / `offpeakTool` naming the
  registered Prometheus MCP server + tool that serves the query
  (`prometheus` / `prom_query`).
- **Per-model evaluators** (`settings.offpeakModelPromql`, chart key
  `settings.offpeakModelPromql`): model name → PromQL returning a 0-100
  load percentage for THE RESOURCES THAT MODEL SERVES ON. A model with an
  entry bills by its OWN hysteresis state; models without one fall back to
  the global query. WHY: the global GPU average dilutes a busy model with
  every idle GPU in the cluster — observed live: the serving GPUs at
  ~53-90% while the 16-GPU fleet average sat at 19.6%, flipping the
  discount ON during peak (enter threshold 20). Scoping note: DCGM series
  carry NO pod label — scope by `Hostname` + `device` range (or
  `exported_namespace`), e.g.
  `avg(avg_over_time(DCGM_FI_DEV_GPU_UTIL{Hostname="pcai-se-scs04.hst.lab",device=~"nvidia[4-7]"}[5m]))`.
  Validate the query in Prometheus first: a scoped query matching zero
  series parses no number → that model's evaluator fails safe to FULL
  price with `last_error` (never a silent permanent discount). Per-model
  states are surfaced in `GET /admin/offpeak` (`models`) and rendered on
  the console Off-peak card.
- **Per-model pricing ratio** (the Off-peak panel's ratio editor):
  `PUT /admin/offpeak/models/{model_name}/fraction` (`{fraction: 0–1}`
  or `{fraction: null}` to clear) overrides ONE model's discount while
  its governing evaluator is ACTIVE — precedence: panel override →
  profile fraction → gateway default; inactive always bills 1.0. Stored
  in `offpeak_model_config`, TTL-refreshed in the evaluator loop,
  invalidated on write; the ledger keeps TRUE usage, so edits are
  forward-looking commercial terms only.

## MCP federation

Registered MCP servers (name + URL, typically `*.svc.cluster.local` in-cluster)
are federated behind the gateway's single `/mcp` endpoint (MCP 2.0,
stateless streamable-http).

- **Per-tool ACLs, default-DENY**: `mcp_tool_acls` rows are
  (key_id, server, tool, allowed). Access is checked **per tool** — a
  server-level allowlist is trivially bypassed by a server that gains one
  tool, so the ACL granularity is the tool.
- **Flat per-tool surface (default)**: `tools/list` returns the calling
  key's GRANTED tools as individual MCP tools named `{server}__{tool}`
  (sanitized to the MCP name grammar — disallowed characters become `_`,
  names over 64 chars are truncated to a 56-char body plus an 8-char
  sha256 suffix of the raw name), each carrying the upstream tool's real
  inputSchema and annotations. The listing is ACL-filtered per request
  (default-deny): a server on which the key holds ZERO grants contributes
  nothing — not even its URL; a key with zero grants sees only the two
  gateway tools (and triggers zero upstream fetches). The gateway tools
  `mcp_catalog` + `mcp_call` remain listed FIRST as the compat surface for
  off-cluster consumers that still call by explicit name.
- **One choke point, two name forms**: `tools/call` accepts flattened AND
  legacy names, and EVERY path funnels through the same gate chain —
  validation → RPM rate limit → registry lookup → per-key ACL
  (default-deny) → server tool-policy → proxied call → ledger settle. The
  flat surface re-exposes upstream tools BY NAME only; it does not create
  a second enforcement path. Flattened names resolve longest-prefix
  against the current enabled registry, with a merged sanitization map as
  the fallback for names sanitization mangled. Every call is
  ACL-checked, server-policy-checked, RATE-limited (RPM axis only) and
  ledgered as `mcp:<server>:<tool>`.
- **Fleet priority**: additive `mcp_servers.priority` (lower = preferred —
  the same convention as model pools) orders the registry snapshot, hence
  the `mcp_catalog`/`tools/list` output order AND the failover chain.
  `POST /admin/mcp-servers` accepts an optional `priority` (default 0);
  `PATCH /admin/mcp-servers/{name}` preserves the current value when
  omitted (an explicit 0 is honored); the console has a Priority field on
  register and an inline-editable table cell (instant-apply PATCH).
- **Failover (flattened calls only — a deliberate asymmetry)**: on a
  TRANSPORT failure (unreachable, timeout, protocol error — never an
  upstream tool-error result, which is a real answer) a flattened call
  retries the next priority-ordered enabled server that (a) exposes the
  tool per the CACHED listing, (b) the key holds an explicit grant on, and
  (c) passes tool policy — bounded by `PCAI_LLM_MCP_FAILOVER_ATTEMPTS` (2)
  retries after the primary. Each attempt settles under its own
  `mcp:<server>:<tool>`; the final error carries the attempts. Explicit
  `mcp_call(server, tool)` NEVER fails over: the caller named the server,
  and silently moving the call would change what was addressed.
- **Listing cache**: per-server upstream listings are TTL-cached
  (`PCAI_LLM_MCP_CATALOG_CACHE_S`, default 60 s) — key-independent
  payloads (per-key ACL filtering always happens after the cached fetch),
  server-level auth only, invalidated via a registry-change hook fired
  inside `registry.refresh()` (every admin register/update/toggle/refresh
  funnels there). STALE-ON-ERROR: a fetch failure with a cached entry
  present serves the STALE listing (discovery availability beats
  freshness); without an entry the server is skipped-with-error as
  before. Cold start pays a bounded fan-out once (15 s/server metadata
  timeout).
- **Cross-replica convergence**: the registry snapshot is per-process, so a
  mutation served by one replica used to leave its siblings stale until they
  restarted (console "test" 404s, relay `unknown server`). The registry now
  polls a DB watermark (`count(*)` + `max(updated_at)` over `mcp_servers` and
  `model_endpoints`, the stamps maintained by a trigger so out-of-band SQL
  edits count) every `PCAI_LLM_REGISTRY_WATCH_S` (5 s) and refreshes only when
  it moved; the console's two registry reads additionally re-check on read
  (`PCAI_LLM_REGISTRY_ADMIN_FRESH_S`, 1 s, fail-open). The local refresh after
  a committed mutation stays — that is what makes the SERVING replica
  immediately correct.
- **Kill switch**: `PCAI_LLM_MCP_FLAT_TOOLS=0` reverts `tools/list` to
  exactly the two gateway tools and flattened names to unknown-tool
  errors — the pre-flat surface, bit-for-bit.
- **Server tool policy**: `all` (default — registering a server means its
  tools are grantable) or `allowlist` (only listed tools are grantable AND
  callable — defense-in-depth on top of the per-key pairs). An EMPTY
  allowlist carries no operator intent and is migrated to `all`.
- **Credential modes** (first hit wins): panel token (Fernet-encrypted,
  master-key gated, rotation = paste + Save), by-ref K8s Secret
  (`auth_secret` → env `PCAI_LLM_MCP_KEY__<NAME>`), or open. Per-key
  overrides layer on top (see above).

## Routing

- **Profiles — budget-driven model cascade**: a profile carries an
  ordered model list (`profiles.model_order`, e.g.
  `['llm-0','llm-1','llm-2']`) and users attach it via
  `PUT /admin/users/{id}/profile`. Each request independently serves the
  **first model in the ordering with budget headroom** — as users run out
  of LLM 0 they land on LLM 1, then LLM 2, with no switch-over event to
  manage (the ordering emerges from the ledger). Semantics: a **pinned**
  request to an available model never cascades (pinning = "I need THIS
  model"); only budget exhaustion on the pinned model promotes it to the
  ordering. Unregistered names ("auto") resolve through the ordering. The
  key's `allowed_models` is a hard boundary. Total exhaustion → 429 with a
  per-candidate breakdown (`cascade_attempts`). The response's `model`
  field always reports what actually served, and the ledger records it.

- **Load-aware cascade head** (opt-in: `loadAwareCascade.enabled` = env
  `PCAI_LLM_LOAD_AWARE_CASCADE`, default **false**): the `auto` cascade
  entry picks its HEAD once per conversation — at START, keyed by
  `thread_key` — from the per-model **off-peak state** the billing
  evaluator already maintains (ACTIVE outranks loaded/never-sampled; ties
  keep profile order; off-peak off = all equal). The pick is **sticky for
  the conversation** (Postgres-backed `model_stickiness`, replica-safe):
  "if it went to GLM, stay with GLM" — only a START re-picks, plus a
  mid-conversation drop+re-pick if the sticky head leaves the servable set
  (deregistered, or out of the key's ACL/ordering). Pinned
  requests, budget/rate gates and 429 failover walk unchanged, off-peak
  **billing untouched**, feature off = **byte-identical** admission; every
  head decision logs one INFO line and bumps
  `gateway_cascade_head_picks_total{picked,reason=sticky|load|default}`.

- **Complexity routing — the pseudo-models `routing` / `routing__X`**:
  clients ask for "the cheapest model that fits" and the gateway decides
  against the profile's OWN ladder — no registry entries, no migration
  (the names are recognized by SHAPE, never registered in
  `model_endpoints`). A profile opts in per-request-policy; every other
  request path passes through untouched.

- **Routing policies** (`profiles.routing_policy`, default `off`):
  `off` — both pseudo names behave byte-for-byte as they do today, i.e.
  as unregistered aliases cascading through `model_order`; `shadow` —
  serve exactly as `off` would, but compute the routing decision and
  stamp it on the ledger row; `active` — serve the routed model through
  the normal pinned/budget machinery (budget exhaustion still walks the
  ordering, and admission failover treats the routed model as PINNED so a
  429 walk can never promote the request above the ceiling). Both knobs
  are console-editable (`POST /admin/profiles`; `routing_min_model` must
  be empty or name a model ON the ladder — a floor off the ladder is
  silently unusable downstream, so the API refuses it with a 400) and go
  live within ~60 s, no restart: the proxy reads them through a per-user
  TTL policy cache (profile knobs change on operator action, not per
  request). The fleet kill switch (`PCAI_LLM_ROUTING_ENABLED=false`) makes
  an opted-in profile behave exactly like `off` — its `X-PCAI-Route`
  header reads `off` so the state stays visible.

- **Routing ladder (ceiling/floor)**: the ladder is the profile's
  `model_order` REVERSED (cheap-first). `routing` → ceiling = first model
  of the ordering; `routing__X` → ceiling = X (hard boundary:
  unregistered → 404, not in the key's `allowed_models` → 403 — also
  enforced inside the availability walk, so a routing name can never
  launder a model past the ACL). Floor = the profile's
  `routing_min_model` position, clamped to never sit above the ceiling;
  an empty or unknown floor pins to the ceiling (no downgrade) with a
  one-time warning. A profile with NO `model_order`: bare `routing` →
  404 ("routing requires a profile model ordering"); `routing__X` →
  serve X pinned.

- **Hard models as rungs (2026-11-08)**: a chat MODEL GROUP and — with
  `PCAI_LLM_ROUTING_AGENTS_ENABLED` on — the key's OFFERED AGENTS are
  routing candidates alongside pools. A group is ONE rung whose member is
  picked at request time (and pinned); an agent is a hard model whose tool
  surface + system prompt is applied when the router picks it (safe:
  routing runs BEFORE the request is prepared). A group may sit on the
  profile ladder (one rung instead of three) and in the routing allowlist;
  agents are PER-KEY (the profile's agent menu), so they join the window
  automatically at the cheap end — their capability is not declared on the
  ladder. Both are advertised as `routing__<name>` and can be named as a
  ceiling.

- **Routing score** (deterministic, pre-generation, no extra model
  calls): chars/4 over every message's content (the system prompt rides
  along) + tool-definition JSON bytes // 64 + 100 per message beyond the
  first + min(max_tokens, 4096) // 8. Two cut points (defaults 1500 /
  6000 estimated tokens) map the score to a ladder position: below tier 1
  serves the floor, below tier 2 the middle of the floor..ceiling window,
  at/above tier 2 the ceiling. Unavailable models walk UP within the
  window (the request gets the cheapest AVAILABLE model that fits — where
  available means the ADMISSION GATE would admit it: key ACL, healthy chat
  pool, and budget headroom for THIS request's estimate, per-key AND team
  envelope); a
  router error or any doubt serves the ceiling — routing can only
  DOWNGRADE when the heuristics positively say the cheap model fits,
  never as a side effect of a failure, and the choice is made before the
  first token (a stream cannot switch models mid-flight; mid-relay budget
  exhaustion still walks the ordering as capacity failover).

- **Routing visibility**: `X-PCAI-Route` rides EVERY chat response of an
  opted-in profile (streaming, non-streaming and relayed) —
  `shadow ceiling=<c> would=<w> reason=<cut>`, `active ceiling=<c>
  served=<s> reason=<cut>`, `off` (profile opted in, fleet switch down),
  `reason=direct` for non-pseudo requests; absent entirely when the
  profile policy is `off`. The ledger's `routing_policy` /
  `routing_ceiling` / `routing_served` / `routing_reason` columns are
  stamped on pseudo-model requests only: shadow stamps the would-serve
  model while the `model` column keeps recording what actually served
  (ledger truth); active stamps the post-gate ACTUAL (the budget gates
  may have promoted the request after the router picked).

- **`GET /v1/models` routing entries**: when the CALLING key's profile
  policy is shadow/active and the master switch is on, the listing
  appends `routing` + `routing__{X}` for each registered chat model, chat
  group, and (with the agents flag) offered agent the key may use, each with
  `"pcai": {"type": "routing"}` and — when capacity is
  known — `pcai.capacity` aggregated over that entry's floor..ceiling window
  (`ok` = at least one rung can serve, `warn` = the tightest rung's band,
  `candidates` = {ok, blocked} counts). Off-policy keys
  see nothing new — for them the names behave as plain aliases, and
  listing them would suggest semantics the profile never signed up for.

- **Routing runtime config — the console's Routing panel**
  (`GET/PUT /admin/routing/config`, `POST /admin/routing/prompt-preview`):
  DB-backed operator overrides over the env defaults, the same shape as
  the distiller config (`core.routing_config`; 60 s TTL cache, degrade-
  not-fail, one transaction per write, every edit audited as
  `routing.config`). It carries four things the env alone could not:

  1. **Which models the router may route TO** (`allowed_models`, empty =
     every registered chat model). This is the router's own ceiling, NOT
     a key ACL — a key's `allowed_models` stays a hard per-key boundary
     that no routing decision can widen (`routing__X` still 403s on a
     model the key may not use).
  2. **The decision-model prompt** — a template for the model that will
     eventually replace the heuristics.
  3. **Live GPU-load injection** into that prompt, sourced from the SAME
     off-peak evaluator the billing discount reads, so the prompt and the
     price can never disagree about how busy the fleet is.
  4. **`enabled`**, AND-ed with `PCAI_LLM_ROUTING_ENABLED`: the env flag
     owns the mount, the row owns whether routing acts.

  Both name fields validate against the registered **chat** pools, not the
  whole registry: the ladder is a chat concept, and an allowlist naming only
  embedding pools would intersect to empty against the chat candidates — which
  the allow-all fallback then reads as "every chat model", the opposite of the
  saved restriction. Refusing a non-chat name at save time keeps that fallback
  reserved for ids that genuinely vanished from the registry.

  **`decision_mode` — off | shadow | active (default `off`).** This is the
  switch that actually consults a decision model:

  | mode | behaviour |
  |---|---|
  | `off` | the shipped heuristics, byte-identical, even with `decision_model` set |
  | `shadow` | the model is asked and its answer recorded, but the heuristics serve — labelled data with zero latency risk |
  | `active` | the model choice serves, when it clears `decision_min_confidence` |

  `off` is the default on purpose: pointing the knob at a model must never
  change behaviour until an operator says so. The rollout is therefore a
  two-step — set `decision_model`, run `shadow` to collect what the model
  WOULD have served against what actually served, then flip to `active`.

  **The confidence floor is load-bearing.** d3-turbo returns calibrated
  probabilities, and a measured 3-way model choice on a real request came back
  at **0.43** — it genuinely does not always know. Below
  `decision_min_confidence` (default 0.60) the heuristics stand and the
  deferral is logged. Serving a coin-flip in preference to a deterministic
  heuristic would make answers worse, breaking the router core contract that
  routing can only downgrade when the cheap model positively fits.

  **What the model sees is a SUMMARY, not the prompt.** d3-turbo advertises
  `validated_input_tokens: 32768` and answered in ~790ms on a small state;
  shipping a 100k-token conversation would blow the budget and dominate the
  latency. The state carries the structural signals that separate "cheap model
  fits" from "needs the ceiling": estimated prompt tokens, message/tool counts,
  requested `max_tokens`, and per-candidate cost and live GPU utilization. It
  never contains message content.

  **A decision can never escape the ladder.** The model is offered exactly the
  floor..ceiling window the heuristics walk (already filtered by the key ACL
  and endpoint health), and an answer naming anything outside that set is
  discarded — the decision path cannot launder a model past a profile ordering
  or a key boundary.

  **Every failure is fail-safe.** Transport error, timeout
  (`decision_timeout_ms`, default 2500, clamped 250–30000), malformed payload,
  an unoffered choice, or low confidence all resolve to "the heuristics stand".
  A decision model that is down costs a downgrade opportunity, never a request.

  The decision endpoint is registered like any other pool with
  `model_type: "decision"` — the vLLM-SR System One family (`POST
  /v1/systemone`, no `/v1/chat/completions`, `output_tokens` always 0). That
  type keeps it out of `/v1/models` and out of the routing ladder: it CHOOSES
  a model, it cannot SERVE one.

  The audit row for a save records the prompt's LENGTH, never its body —
  the trail is append-only and never pruned, so a 20 000-char prompt would
  otherwise be copied into every row.

  The prompt is a **template, not `str.format`**: no `eval`, no attribute
  access, and an unknown `{{placeholder}}` is left VERBATIM so a typo is
  visible in the preview instead of being silently swallowed.
  `{{models}}` / `{{model_list}}` / `{{decision_model}}` carry the
  routing context; `{{gpu_load}}` / `{{gpu_load_json}}` carry the live
  utilization (`- Qwen3.8-27B: 42.1% util (low demand, 0.5x)`, or
  `- <name>: no data` when the evaluator has no sample — it never
  fabricates a number). With `inject_gpu_load` off both GPU placeholders
  render `(gpu load injection is off)`. `max_prompt_chars` truncates the
  RENDERED prompt so a large snapshot cannot blow the model's context.

  The preview route renders with the SAVED config plus in-memory
  overrides and **writes nothing** — it is a read, so an operator can
  iterate on a prompt without leaving audit rows for every keystroke.

- **`GET /v1/models` announces what it hid**: the response carries
  `X-PCAI-Models-Denied: <n>` — how many registered models this key's
  `allowed_models` filtered out (`0` for an allow-all key). The JSON body
  is unchanged, so existing clients are byte-identical. This closes a real
  support trap: a key that could not see a model it expected had no way to
  distinguish "the gateway does not serve that model" from "my key is not
  allowed to use it", and the chat console's dropdown silently showed the
  shorter list. `/admin/presented-models` is the admin-side counterpart —
  the registry-truth view of everything `/v1/models` can present
  (aliases, routing pseudo-models, registered pools with their health,
  ceiling, off-peak state and routing eligibility), so an operator can see
  the whole advertised surface without impersonating a key.

- **A profile's `model_order` is also an ACL — and now says so.**
  Enrollment derives a key's `allowed_models` from the profile's ordering
  (`[m for m in model_order if m in registered]`), which is the right
  least-privilege default but made a stale id in the ordering silently
  REMOVE a model from every enrolled key — no error, no warning, just a
  shorter dropdown. `POST /admin/profiles` now refuses an ordering entry
  that is not a registered model, naming it, and `GET /admin/profiles`
  reports `stale_models` (ordering entries with no pool) and `acl_effect`
  (the ordering ∩ registry, i.e. what an enrolled key will actually be
  allowed) so the console can show the double duty explicitly.

- **Shadow-first rollout, and a deliberate name reservation**: a learned
  router needs LABELED data before it can be trusted with spend — the
  shadow ledger pairs what actually served (truth) with what the
  heuristic WOULD have served, making the fleet's own traffic the
  evaluation set (and future training set). And `model: "routing"`
  previously resolved as a plain alias; registering the semantics CHANGES
  that — for opted-in profiles only (`off` profiles keep the legacy
  behavior).

- **Pools**: a virtual model name maps to N physical endpoints
  (`model_endpoints`: base_url, priority, weight, model_type, enabled).
  Typical pools: vLLM/SGLang stacks, quantization variants, cache regimes.
- **Type gates**: `/v1/chat/completions` only serves `chat` pools and
  `/v1/embeddings` only `embedding` pools — a chat request must never hit
  an embedding endpoint.
- **Selection**: among enabled + cooled-down endpoints, the best (lowest)
  `priority` tier wins; within a tier, `weight`ed random. **`weight: 0` =
  standby** — receives no load-balanced traffic (it still serves via
  failover when nothing else in the tier is healthy). A tier where every
  endpoint has weight 0 is served in deterministic registration order.
- **`GET /v1/models`**: the caller-aware catalog — registered model names
  (filtered by the key's `allowed_models`), a synthetic `auto` entry
  (`pcai.type: "cascade"`, `pcai.order: [...]`) when the key's profile has
  a model ordering, and — when the key's profile opted into complexity
  routing (above) with the master switch on — the `routing` pseudo-model
  entries (`pcai.type: "routing"`). Endpoint-level priority/weight stay
  hidden — routing is the gateway's business.
- **Circuit breaker (per endpoint, in-memory)**: after
  `endpoint_failure_threshold` (default 5) consecutive failures the endpoint
  cools down for `endpoint_cooldown_s` (default 60 s) before being retried;
  a success resets the counter. Bad replicas quarantine themselves; the
  pool fails over within the healthy tier. **Breaker state survives
  registry refreshes** (carried by endpoint id) — a console save during an
  upstream outage must not hand a dead endpoint a fresh strike budget.
- **Credentials**: three modes, first hit wins — panel token
  (`auth_token_enc`, Fernet), by-ref Secret name (`api_key_secret` → env
  `PCAI_LLM_UPSTREAM_KEY__<NAME>`), or open. Never inline, never returned.
- **Transport**: all upstream calls ride the in-cluster fabric
  (`svc.cluster.local`) over one shared httpx client with
  `upstream_timeout_s` (app default 600 s; the chart ships 900 via
  `settings.upstreamTimeoutS`) and `follow_redirects=False`.

## Session memory distiller

> **Configuring memory?** Start with [MEMORY.md](MEMORY.md) — the end-to-end
> walkthrough (mint a RAG key → create and ★ a dataset → attach the header
> override → prove the feed moves). This section describes the pipeline
> internals; MEMORY.md is the operator/user path into it.

One canonical pipeline turns a conversation transcript into a structured
project-summary memory (`[MEMORY INDEX | project: … | type: session-summary |
topics: …]` + What we did / Key decisions & rationale / Gotchas / How to
proceed), written to the calling key's RAG memory dataset. It replaces the
three per-harness implementations (DSH plugin, opencode agent, OWUI filter):
one prompt, one taxonomy, one credential path, one place to maintain.

* **Trigger**: `POST /v1/distill` (inline transcript) or
  `POST /v1/distill/conversations/{id}` (a stored conversation — the
  zero-payload shim for harnesses that push via /v1/conversations). Both are
  keyed surfaces: the X-API-Key envelope is the credential.
* **Attachment gate (capability probe)**: before ANY spend, the gateway
  calls the memory server's probe tool (`memory_status` on mm-rag) WITH the
  key's own credentials; the BACKEND resolves the caller's identity from the
  presented key (mm-rag D15 X-API-Key -> the /access-page star binding or
  `MEMORY_DATASET`) and answers available/writable/unlocked about its own
  caller. Not attached -> `200 {"ok": false, "inert": true}` and NOTHING
  runs. TTL-cached (60s). The memory server, write tool, and probe tool are
  deployment config (`distillerMemory*` values) — any backend with an
  identity-routed write tool integrates, not just mm-rag.
* **Universal capture (2026-10-09)**: the sweep's substrate is the
  canonical store — which EVERY harness using the gateway feeds
  automatically. Relayed agents (tool relay) capture post-loop; plain chat
  agents (raw SDK clients, opencode, OWUI raw mode) capture fire-and-forget
  on input and converge to the full conversation as clients resend history.
  No client changes; `X-PCAI-Conversation: raw` still opts a request out
  entirely. One-shot rows (single exchange) are never distilled.
* **Lifecycle (the DSH approach)**: long transcripts are map-reduced in
  adaptive windows before the final structured summary; a conversation that
  CONTINUES after distillation is re-distilled automatically (its marker is
  dropped on the next write; the scheduled sweep re-runs it when quiet).
* **Scheduling**: the housekeeping loop distills quiet conversations of
  memory-attached keys (debounce window, default 5 min). Admin-configurable
  (Admin UI → Memory Distiller): summarizer model (default: the caller's own
  routing), off-peak-only scheduling (scheduled distills run only while the
  SUMMARIZER MODEL's per-model off-peak evaluator reports low demand; the
  global evaluator is the fallback when no model is pinned or no per-model
  query is configured), quiet window, output cap, runtime enable. DB-backed
  overrides over the env defaults, audited.
* **Cost**: every distill LLM call settles ONE ledger row against the owning
  key and writes a `phase=distill` agent trace — the spend is attributed
  where the work happened, never free.
* **Failure semantics**: a memory-write failure is reported as `ok: false`,
  never a 5xx; a skipped row (gate down, collapsed transcript) backs off for
  `distillerGateBackoffS` (default 900 — a broken dependency cannot starve
  newer rows) and returns to the due set when the window lapses. On a MODEL
  failure after the gates passed, `distillerRawFallback` decides: `true`
  (app default, the historical DSH contract) writes the raw transcript as
  `session_history` so a session is never lost — but mm-rag chunks a raw
  transcript (measured: 126 fragments from one session), diluting recall;
  `false` (the shipped G2 example) leaves the row due and retries — nothing
  is lost (the conversation stays in `canonical_conversations`) and the
  memory store only ever receives distilled summaries.

## Usage surfaces

- **Admin ledger explorer** (`GET /admin/usage`): last N ledger rows across
  ALL keys with filters (`key_id`, `user_id`, `model`, `status`, `since`,
  `until`), an ACTOR join (`key_name`, `key_prefix`, `user_name`,
  `tenant_name` — "who called what when"), GROUP BY rollups
  (`key|model|tenant|day` → requests/tokens/cached/hits/cost) and CSV
  export (`format=csv` → `text/csv` + attachment; 1000-row export cap).
  This is the chargeback/audit artifact; per-user shading in the console
  tints each user's rows and trace cards one of six ramps so one person
  reads as one color across tabs.
- **Rollup** (`GET /admin/usage/summary`): totals + per-model split
  (requests, in/out/cached tokens, cache hits, USD).
- **Self-service** (`GET /v1/usage`): the calling key's budgets (both
  kinds, limit vs fraction-weighted spent), the current-minute rate window
  vs RPM/TPM, and the 20 most recent outcomes — the "why am I 429ing"
  surface, no admin involved. The chat UI's usage panel calls it and
  auto-opens on a 429.

## Endpoint health

- **Passive surface** (`GET /admin/endpoints`): every registered endpoint
  row merged with its live in-memory breaker state — `state`
  (healthy|cooling|disabled), `consecutive_failures`,
  `cooldown_remaining_s`, `last_error`, `last_ok_at`/`last_fail_at`. The
  breaker was previously invisible; a dead replica was discovered by END
  USERS.
- **Active probe** (`POST /admin/models/{model_name}/probe`): GETs the
  endpoint's `/models` (both vLLM and SGLang implement it) with a hard 5 s
  timeout — probes the endpoint the router would pick right now. No ledger
  row (metadata call). MCP servers have the equivalent live check in
  `GET /admin/mcp-servers/{name}/tools` (a real `list_tools`, 15 s bound).
- Console: status dots + a `test` button per row in the Models and MCP
  tabs. A row's `edit` button loads the whole registration into the top
  form (MCP servers and model pools alike): model-pool edit reads
  `GET /admin/models/{name}/pool`, which returns the by-ref Secret NAME
  and an `auth_configured` boolean — never a credential value. Both tables
  share one action block: equal ghost buttons with the test result on its
  own line underneath, so a wide `ok · 122ms` or a fail message wraps
  inside the cell instead of re-flowing the row. The MCP Tools dialog renders
  upstream tool descriptions as whitelisted markdown (links, bold, `code`
  spans, leading-dash list items) — escape-first on semi-trusted upstream
  content: the text is
  entity-escaped BEFORE any transform and link URLs are charset-restricted
  (https? only, no quotes/whitespace), so upstream markup can never reach
  `innerHTML` as tags. No raw-HTML passthrough, no images/headings/tables.
- **KV cache detail on `GET /admin/endpoint-metrics` (2026-11-08)**: the
  rows gain `kv_*` fields read from the inference servers' own gauges
  (SGLang-only — vLLM rows read null): the GPU KV pool (`kv_gpu_pct` =
  the ACTIVE usage ratio — evictable radix-cache tokens are not holding
  it — plus the absolute used / evictable / max token gauges), the
  HiCache host-RAM L2 (`kv_host_*` — occupancy near 100% at
  write_through steady state is normal, not a fault), and the storage
  tier, which has NO occupancy gauge — `kv_disk_configured` (bool) +
  `kv_disk_hit_tps` (activity only) are all the disk story can be. The
  Off-peak tab's per-model cards render the strip under the sparkline
  (tiers with no data omitted entirely, replicas aggregated, the
  per-replica breakdown in the row tooltip). The same module's
  `load_rows()` is the 3-query load snapshot the model groups'
  background refresher consumes.
- **All-models cache utilization summary on the Off-peak tab (2026-11-09)**:
  a card above the per-model evaluators aggregates EVERY endpoint row —
  including models with no per-model evaluator — into POD MEMORY (the pod's
  container working set vs its memory limit), RAM (cached) (host HiCache L2 pool
  occupancy, tokens), and DISK (GPU active usage stays on the per-model
  cards: vLLM reports a ratio with no absolute pool size). POD MEMORY is the
  raw utilization (`container_memory_working_set_bytes` /
  `kube_pod_container_resource_limits`), summed over the cache-bearing pods —
  process memory (weights + host pool + runtime), not a token-to-byte
  conversion. RAM (cached) is the same `kv_host_used_tokens` /
  `kv_host_total_tokens` the per-model strips read, summed across pods
  (weighted percent, never a mean of ratios). The engine exports NO storage
  occupancy gauge, so a real disk number is read from the L3 VOLUMES:
  `settings.endpointMetricsDiskSelector` (env
  `PCAI_LLM_ENDPOINT_METRICS_DISK_SELECTOR`) is a PromQL label selector
  over `kubelet_volume_stats_*` naming them — on G2 the Mooncake store's SSD
  offload PVCs — and `GET /admin/endpoint-metrics` gains
  `disk: {used_bytes, total_bytes, pct, selector}` (null when unset/unknown,
  never a fabricated 0%). Without a volume number the summary falls back to
  L3 activity/status; two extra PromQL calls run per snapshot only when the
  selector is set.

## Router extras

- **Per-model fallback chains** (`model_fallbacks` table;
  `GET/PUT /admin/models/{name}/fallbacks`): admin-curated failover per
  virtual model, layered on top of the profile ordering. Walked when the
  serving model cannot pick (`pool.pick()==None` — the pre-chain behavior
  was a hard 503 even for a pinned request) or answers with an upstream
  transport error / 5xx (post breaker-report — the pre-chain behavior
  relayed the bare 5xx). Every hop is re-gated at walk time: registered,
  inside the key's `allowed_models`, a HEALTHY chat pool, and fresh
  `check_budgets` headroom (an exhausted candidate settles its own
  `budget_exceeded` row and is skipped) — the chain is a preference list,
  never a policy or budget bypass. Each attempt settles under its ACTUAL
  model; `X-PCAI-Fallback` names the model failed over FROM. Plain
  streaming fails over only BEFORE the first byte; mid-stream death is
  unchanged. Bounded by `PCAI_LLM_MODEL_FALLBACK_MAX` (chart key
  `settings.modelFallbackMax`, default 2; 0 = chains never walk).
  **Alias runtime continuation**: for an ALIAS request (`auto`) the profile
  ordering's TAIL — the models AFTER the serving one — is appended as the
  FINAL segment of the same walk, so an `auto` whose admitted model cannot
  serve (dead endpoint, transport error, 5xx) keeps cascading down the
  ordering even when no chain was curated, instead of 503ing answerless.
  The per-model chain is tried FIRST (an explicit per-model sign-off
  outranks the ordering) and both segments share the one
  `PCAI_LLM_MODEL_FALLBACK_MAX` bound. Pinned requests never cascade: their
  runtime net stays the per-model chain.
  Validation at save time: registered names only, no self-fallback,
  duplicates collapsed.
- **Router metadata in responses**: `X-PCAI-Served-Model` +
  `X-PCAI-Endpoint` headers on every chat response (always — no
  transparency cost, single `_relay_headers` choke point). The body
  `"pcai": {...}` block (served model, endpoint, `cost_usd` when priced,
  `cached_tokens`) rides ONLY on request header `X-PCAI-Metadata: 1` AND
  non-streaming responses — the module is a declared transparent proxy;
  body mutation is opt-in.
- **Zero-completion insurance** (`PCAI_LLM_ZERO_COMPLETION_RETRY`, chart
  key `settings.zeroCompletionRetry`, default OFF): 2xx + empty assistant
  text (string or content-parts shape) + `completion_tokens==0` +
  `finish_reason=='stop'` triggers ONE identical retry — non-streaming
  JSON path and the relay's final answer only, never plain streaming. BOTH
  attempts settle under the shared `request_id` (the first attempt was
  real GPU spend; budgets see both).
- **Native wire visibility for in-cluster tool calls**
  (`PCAI_LLM_RELAY_STREAM_WIRE_TOOLS`, chart key
  `settings.toolRelayStreamWireTools`, default OFF): when on, the relay
  re-emits the model's `tool_call` deltas as native OpenAI chunks DURING
  the loop — informational only (no `finish_reason`; the relay still owns
  execution). OFF by default because executing stream clients (DSH)
  double-execute the re-emitted calls and phantom-error flat names — see
  DEPLOYMENT.md § values → environment variables for the full incident
  rationale. Handback deltas for CLIENT-offered tools are emitted
  unconditionally and are not affected by this flag.
- **Client tool-server dispatch** (chart keys `settings.clientToolDispatch`
  / `settings.clientToolDispatchTimeoutS`, env
  `PCAI_LLM_CLIENT_TOOL_DISPATCH` / `..._TIMEOUT_S`, default off / 120 s):
  when enabled, a key may register its local tools
  (`POST /v1/dispatch/register`, answered over the
  `/v1/dispatch/poll` + `/v1/dispatch/result` long-poll channel) and the
  relay merges `client__<tool>` into the model toolset per key — the model
  calls the client's local tools like any in-cluster tool and the result
  returns INTO the running relay loop. Routes 404 while the flag is off;
  keys without a live offer see byte-for-byte the handback behavior above.
  The timeout bounds ONE dispatch (an offline/busy client yields a tool
  ERROR after this long, never a hang; per-tool `timeout_s` values from an
  offer are clamped to `[1, timeout]`).

## Model groups

Named sets of EQUIVALENT chat models, selected by live load instead of a
fixed order — the load-balanced sibling of the ordered fallback chain
(§ Router extras): a chain says "try these in order", a group says "any
of these, take the idle one".

- **What a group is** (`model_groups` table; admin CRUD + read-only
  preview via `GET/PUT/DELETE /admin/model-groups` and `POST
  /admin/model-groups/{name}/preview`, API.md § 4.3). A group is a
  first-class ROUTE TARGET: a request may name it directly, a per-model
  fallback chain may list it, and `GET /v1/models` advertises it
  (`owned_by: "group"`, `pcai.type: "group"`) when the key may use it.
- **The pick** (per request, only with `model_groups_enabled` on):
  power-of-two over live queue depth — two members sampled, the one with
  the lower MINIMUM queue across its endpoints wins. Load is a BACKGROUND
  Prometheus snapshot (refreshed every `model_group_load_refresh_s`, 5 s;
  NEVER queried on the request path — the pick reads the in-memory view);
  a snapshot older than `model_group_load_stale_s` (30 s) degrades the
  pick to random, so a Prometheus outage must never stop model selection.
  A member with no load data draws a NEUTRAL rank over the OBSERVED score
  range (a single observed value ties it; no data at all is uniform
  [0,1]) — an unmonitored model is neither herded onto nor starved from.
- **The pins**: the conversation is pinned to the chosen member (`grp:`
  stickiness scope), and — with session affinity enabled too — to one of
  its endpoints (`ep:` scope), so the next turn does not re-prefill. A
  pin sticks until the member leaves the group or the key's ACL, or the
  member's WHOLE pool is unhealthy — one failed call keeps it (re-picking
  would cost a full re-prefill); the endpoint pin's drop-on-failure is
  unchanged.
- **Policy and budgets**: the SELECTED member bills and carries the ACL —
  a group never bypasses `allowed_models` or budgets (a key that may use
  one member may use the group; one that may use none gets 503 when no
  member qualifies — also the all-unhealthy answer). Groups never enter
  budget-capacity maps or the routing ladder.
- **v1 limits, stated**: chat members only; ONE level (a member may not
  be a group); `load` is the only policy (any other value is refused at
  save time, not silently coerced); no group-level budgets; groups do NOT
  enter profile ordering or the `auto` cascade. `X-PCAI-Group` names
  the group on the response (the member rides `X-PCAI-Served-Model`, the
  endpoint `X-PCAI-Endpoint`); a group reached via a fallback-chain hop
  is named by those two headers instead.
- **Config**: `PCAI_LLM_MODEL_GROUPS_ENABLED` (default off — off makes a
  group name unroutable while the admin API still stores the rows and
  routing is byte-identical), `PCAI_LLM_MODEL_GROUP_LOAD_REFRESH_S` (5),
  `PCAI_LLM_MODEL_GROUP_LOAD_STALE_S` (30); chart keys
  `settings.modelGroupsEnabled` / `modelGroupLoadRefreshS` /
  `modelGroupLoadStaleS`.

## Admin mutation audit trail

The control-plane twin of the usage ledger: the ledger answers "what did
the DATA plane cost", `admin_audit` answers "who changed the control
plane, when".

- **One append-only row per admin mutation**: all mutating `/admin` routes
  (33 instrumented actions — key/model/budget/MCP/profile/guardrail/
  off-peak/enrollment create-patch-delete-revoke) plus the 6
  enroll-surface routes write one row (action, resource type/id, actor
  key + stable actor label, client address, redacted details JSON).
  Best-effort by design: `audit_action` never fails the operation it
  records — a broken audit pipeline must not take the admin API down.
- **No secret material, ever**: `details_redact()` enforces the
  no-secret-material rule before the row is written (credential values
  never appear; only names/counts/shape).
- **Read surface**: `GET /admin/audit` (admin-gated, filters AND-ed:
  since/until/actor_key_id/action/resource_type, `LIMIT/OFFSET` paging
  capped at 500, `format=csv` export) and the console's Audit tab.
  The trail is append-only and never pruned — like the usage ledger.

## Ledger schema

`CREATE TABLE IF NOT EXISTS` (+ guarded additive migrations) — applied
automatically at startup; idempotent on every database state (fresh, any
prior release), existing tables untouched.

| Table | Purpose |
|---|---|
| `tenants`, `users` | Tenancy roots (tenant → users → keys; `users.profile_id`). |
| `profiles` | Named cascade orderings (`model_order`) + key-creation presets (`defaults_json`: budgets, mcp_grants) + per-profile `offpeak_fraction` + complexity-routing knobs (`routing_policy` `off\|shadow\|active`, `routing_min_model` floor). A preset budget is a token budget (`limit_tokens`) or a dollar budget (`limit_usd`), exactly one kind per entry (period `day\|week\|month\|quarter\|year`, positive); keys minted from the profile get BOTH kinds stamped — token presets into `budgets`, dollar presets into `cost_budgets`. |
| `api_keys` | Hashed keys: `key_hash`, `key_prefix`, `scopes`, `allowed_models`, `rate_limit_rpm/tpm`, `max_concurrent`, `expires_at`, `revoked_at`, `relay_default`. |
| `budgets` | Token budgets: (key, model-or-`'*'`, period ∈ day/week/month/quarter/year, `limit_tokens`) — UNIQUE per triple. |
| `cost_budgets` | USD cost budgets, parallel shape (`limit_usd`); gate-checked POST-PAID alongside the token budgets (`spent >= limit` — the in-flight request's cost is only known after the response, so overshoot is bounded by one response), and INERT for an unpriced model (`cost_usd` NULL contributes nothing to the SUM). |
| `model_costs` | Per-model USD prices (input/output/cached per 1M tokens) that price `usage_ledger.cost_usd` at request time; absent row = unpriced (cost NULL). |
| `usage_ledger` | One immutable row per request outcome: key, model, `endpoint` (`chat|embeddings|mcp:<server>:<tool>|chat:guardrail`), prompt/completion/total/cached tokens, `cost_usd`, `status` (`ok|error|cache_hit|budget_exceeded|rate_limited|guardrail_blocked`), latency, request id, `billing_fraction`, `guardrail_events` JSONB, complexity-routing audit columns (`routing_policy/ceiling/served/reason`, stamped on pseudo-model requests under shadow/active only). Indexed `(key_id, ts DESC)` and `(key_id, model, ts DESC)`. |
| `rate_windows` | Fixed-window RPM/TPM counters (PK key + window_start); pruned opportunistically. |
| `model_endpoints` | Pool entries per virtual model name (priority/weight/`model_type`, by-ref `api_key_secret` or Fernet `auth_token_enc`). |
| `mcp_servers` | Federated server registry (name, URL, static headers by-ref, auth modes, `tool_policy` + allowlist, `priority` — lower = preferred, orders listing + failover, enabled). |
| `mcp_tool_acls` | Per-tool ACLs (PK key+server+tool). |
| `embedding_cache` | Embedding cache (PK cache_key = sha256(model+input); JSONB vector). No TTL/eviction today — sized for the corpus, the known trade-off. |
| `agent_traces` | One row per relay step (request_id, key, user, model, iteration, phase chat\|tool, server/tool, ms, tokens, status). |
| `conversations`, `conversation_messages` | Key-scoped saved transcripts (caps app-enforced; messages seq-numbered, UNIQUE per conversation). |
| `enrollments` | Invite codes (code_hash UNIQUE, label, tenant, profile, `max_uses`/`uses`, expiry, revocation). |
| `mcp_server_creds` | Per-key per-server Fernet-encrypted header overrides (UNIQUE key+server). |
| `guardrails`, `model_guardrails` | Content policy (name, mode pre_call\|post_call, engine, `config_json`, enabled) and the per-model attachments (PK model_name + guardrail_id, FK cascade). |
| `model_fallbacks` | Per-model fallback chains (router extras): (model_name, rank, fallback_model), UNIQUE (model_name, fallback_model) — the walk order for a pinned request whose model cannot serve; every hop re-gated at request time. |
| `admin_audit` | Append-only control-plane trail: one row per admin mutation (action, resource, actor, redacted details). Never pruned; read via `GET /admin/audit` (§ Admin mutation audit trail). |

## API reference

The full method/path/auth/body/response reference now lives in
[`API.md`](API.md). Group map:

| Group | Endpoints | Auth |
|---|---|---|
| Proxy | `POST /v1/chat/completions` (+ relay), `POST /v1/embeddings`, `GET /v1/models`, `GET /v1/usage`; `POST /v1/responses` = deliberate **501** (not implemented; SGLang upstream gaps #17853/#13292 — facade planned) | virtual key, `proxy` (501 needs none) |
| Conversations | `/v1/conversations` CRUD (5 routes) | virtual key |
| Client tool dispatch | `POST /v1/dispatch/register`, `GET /v1/dispatch/poll`, `POST /v1/dispatch/result`, `GET /v1/dispatch/offer` | virtual key; routes exist only when `PCAI_LLM_CLIENT_TOOL_DISPATCH=true` (404 while off) |
| Enrollment | `POST /v1/enroll` | none (the code IS the credential) |
| Identity | `GET /v1/identity` (whoami probe — § SSO self-mint), `POST /v1/keys/for-me` (browser identity; requires `PCAI_LLM_TRUST_PROXY_IDENTITY` + `PCAI_LLM_SSO_KEY_CREATE_ENABLED`) | trusted browser identity |
| Tenancy | `/admin/tenants`, `/admin/tenants/{id}/users` | admin key |
| Keys | `/admin/keys` (+ PATCH, rotate, budgets, usage, mcp-grants, mcp-creds) | admin key |
| Models | `/admin/models` (+ DELETE, probe, fallbacks GET/PUT), `GET /admin/endpoints` | admin key |
| Model groups | `/admin/model-groups` (+ DELETE, `POST /admin/model-groups/{name}/preview`) | admin key |
| MCP admin | `/admin/mcp-servers` (+ PATCH, tools), `POST /admin/registry/refresh` | admin key |
| Costs | `POST/GET /admin/costs` | admin key |
| Profiles | `/admin/profiles` (+ DELETE), `PUT /admin/users/{id}/profile` | admin key |
| Usage | `GET /admin/usage` (+ `/summary`) | admin key |
| Audit | `GET /admin/audit` (filters, paging, CSV export) | admin key |
| Traces | `GET /admin/traces[/{request_id}]` | admin key |
| Effective prompts | `GET /admin/effective-prompts` (per key/thread capture review) | admin key |
| Off-peak | `GET /admin/offpeak`, `PUT /admin/offpeak/models/{model}/fraction` | admin key |
| Enrollments | `/admin/enrollments` (+ revoke) | admin key |
| Guardrails | `/admin/guardrails` (+ models picker, attach, PATCH, DELETE) | admin key |

Data-plane extras: `/mcp` (virtual key + `mcp` scope; the MCP 2.0 surface
with `mcp_catalog` + `mcp_call` tools), `GET /health` (liveness, inert),
`GET /ready` (readiness — Postgres round-trip), `GET /metrics`
(Prometheus, when enabled). `GET /admin/help` is the AdminDep-gated
discovery index; `GET /admin` and `GET /` serve the consoles.
