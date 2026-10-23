"""Runtime configuration.

12-factor: every knob is an env var (the Helm chart renders them from
values). Secrets are NEVER config values — the chart references
operator-created Secrets by name (the applygate lesson: an envsubst apply
once emptied a live key Secret).
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PCAI_LLM_", env_file=None)

    # --- server ---
    host: str = "0.0.0.0"
    port: int = 9200
    log_level: str = "INFO"
    # Uvicorn's timeout_keep_alive: idle keep-alive connections close after
    # this many seconds. The 5s default makes high-fanout consumers (agents,
    # OpenAI SDKs with connection pools) re-TLS-handshake constantly at
    # scale; 75s matches common LB/proxy idle timeouts.
    keep_alive_timeout_s: int = 75
    # Uvicorn limit_concurrency: hard cap on concurrent connections+tasks
    # before uvicorn sheds load with 503. Unbounded by default (uvicorn
    # default None), a burst grows task count (and memory) until the box
    # dies; a bounded cap fails FAST instead, matching the PoolUnavailable
    # 503 discipline. 0 = disabled (no cap).
    limit_concurrency: int = 0

    # --- database: TWO equivalent modes (exactly one must be complete) ---
    # (a) full URL (external-Postgres deployments, Secret by reference):
    database_url: str = ""
    # (b) discrete fields — lets the CHART inject the password via
    # secretKeyRef (a Secret cannot feed part of a URL string). Used by
    # embedded-Postgres mode; db.connect() fails loud unless exactly one
    # complete mode is present.
    db_host: str = ""
    db_port: int = 5432
    db_user: str = ""
    db_password: str = ""
    db_name: str = ""

    # --- database pool sizing (perf audit: hardcoded min=2/max=10 starved
    # the 14-27-round-trip hot path on a single worker) ---
    db_pool_min: int = 5
    db_pool_max: int = 25
    # Bounded wait for HOT-PATH connection acquisition (fetch/execute): a
    # request that cannot get a connection inside this window fails fast
    # with 503 instead of queueing forever behind a saturated pool.
    db_acquire_timeout_s: float = 2.0

    # --- proxy behavior ---
    # Timeout for a single upstream model call (chat/embeddings).
    upstream_timeout_s: float = 600.0
    # Per-endpoint circuit breaker: after N consecutive failures an endpoint
    # cools down for this long before being retried.
    endpoint_failure_threshold: int = 5
    endpoint_cooldown_s: float = 60.0
    # CA bundle for the UPSTREAM httpx client (model endpoints + MCP servers).
    # Empty (default) = httpx's default trust store (system CAs). Set it to a
    # mounted CA-bundle path when a site's PCAI ingress presents a PRIVATE CA:
    # httpx then verifies against it like any other CA. The AgentBuilder
    # lesson: when verification is awkward, the easy wrong path is
    # verify=False (TLS verification off for remote endpoints — bearer
    # tokens exposed to MITM); make "verify against the SITE CA" the easy
    # RIGHT path instead. There is deliberately NO verify=False knob.
    upstream_ca_bundle: str = ""
    # Max request body size for proxied calls (bytes).
    max_body_bytes: int = 32 * 1024 * 1024

    # --- key auth ---
    # Fail closed when the key ledger is unavailable: requests are refused,
    # never silently allowed.
    admin_api_keys: str = ""  # comma-separated; empty disables the admin API

    # Master key for Fernet-at-rest credentials (per-endpoint API keys stored
    # encrypted in Postgres via the admin panel). From a K8s Secret by ref.
    # Empty = credential storage disabled (by-ref Secret names still work).
    secrets_master_key: str = ""

    # --- tool-use relay (server-side model<->MCP loop) ---
    # Client opts in with header X-PCAI-Execute-Tools. The loop runs
    # in-cluster; guards below are the AgentBuilder lessons ported.
    tool_relay_enabled: bool = True
    # 16 MODEL rounds (not tool calls — a round may batch several parallel
    # calls): a serial research turn (search, then a fetch per page, then
    # synthesize) burns ~1 round per call and exhausted 8 mid-research. The
    # real runaway protection is unchanged: wall clock, identical-call loop
    # detection, and the per-call budget/ledger gates.
    tool_relay_max_iterations: int = 16
    # 600s: the harness-relay turn shape (big prefill + one generation emitting
    # a large parallel tool-call batch) legitimately crosses 4 minutes; the old
    # 240s default killed real work ("model error: relay model call timed out"
    # surfaced to the client instead of an answer). Ceiling is enforced in
    # relay_stream: the guard clamps to the Istio ingress timeout MINUS
    # headroom, so a raised value can never race the VS 600s 504.
    tool_relay_wall_clock_s: float = 600.0
    # Headroom kept below the ingress timeout when clamping the wall clock:
    # the relay must always be able to close ITS stream cleanly (guard event +
    # error chunk + [DONE]) before Istio shoots the connection down with a
    # bare 504 the client can't parse.
    tool_relay_ingress_headroom_s: float = 30.0
    # TOTAL extra attempts per relay TURN (not per call) for a bounded model
    # call that expired on timeout. 0 = today's behavior byte-for-byte. The
    # retry budget comes OUT OF the remaining wall clock (never extends it),
    # so the ingress clean-close guarantee still holds. Only the TIMEOUT
    # failure class retries — a call that returned an answer (even an error
    # answer) is real work, never re-rolled; duplicate GPU spend on
    # generations that actually completed server-side is why this is bounded
    # to one.
    tool_relay_call_retries: int = 1
    # 'scope' (default): an mcp-SCOPED key gets all policy-admitted tools of
    # every enabled server automatically — "registered servers attach to the
    # model". Explicit per-key grants still refine/restrict (allowlist-mode
    # servers only offer their admitted tools).
    # 'explicit': keys see ONLY their hand-granted (server, tool) pairs.
    tool_relay_grant_mode: str = "scope"
    # Cloud shadowing: a client MCP tool (mcp__<server>__<tool>) whose TOOL
    # name matches exactly one granted gateway tool is dropped from the
    # OFFER — the model calls the in-cluster twin and the relay executes it
    # without the client's VPN hop. Handback remains the safety net for
    # calls made under the client's original name. Kill switch for a
    # semantic-mismatch surprise (same tool name, different behavior).
    tool_relay_shadow_mcp: bool = True

    # --- server-owned conversations (ROADMAP §7, phase 1: reconciliation) ---
    # Master switch for canonical conversation reconciliation on the relay
    # path: each relayed turn is fingerprinted against the stored canonical
    # message list (core.canonical) and the extend/reset verdict is persisted
    # so the gateway becomes the conversation's owner WITHOUT client changes.
    # False = fully inert: no DB read, no write, raw incoming messages —
    # byte-for-byte today's behavior (the fail-safe position).
    canonical_conversations: bool = True
    # Hard per-request context budget, in ESTIMATED tokens (same len//4
    # heuristic as the gates), for the message list a RELAYED call sends
    # upstream. 0 = no trimming (default — phase 1 ships the mechanism off).
    # When > 0 and the reconciled list estimates over budget, the OLDEST
    # messages are dropped (system preamble + newest turn always kept) until
    # it fits — the "don't send too much" invariant enforced at the choke
    # point, instead of whatever the client chose to resend. Trimming is a
    # per-call projection: the stored canonical conversation is never
    # truncated by it.
    conversation_context_budget_tokens: int = 0

    # --- effective-prompt capture (ROADMAP §7 phase 2 wave C: transparency) ---
    # Andrew's transparency requirement: with the inbound reasoning strip
    # (0.8.1) and canonical reconciliation + optional trim-to-budget (0.8.2)
    # in place, "the conversation the user reads is not the conversation the
    # model received" — debugging a bad answer means reconstructing what the
    # model ACTUALLY saw per call. Wave C records that effective prompt per
    # model call (stripped, reconciled, post-trim — the exact message list at
    # the upstream choke point) and exposes it to admins for review.
    # FAIL-SAFE by construction: capture is strictly observational — ANY
    # fault (DB down, a bug here) logs a warning and the relay turn proceeds
    # untouched; with the feature off (or faulted away), nothing exists and
    # nothing is affected. Default ON because a transparency feature that is
    # off by default answers no debugging questions.
    effective_prompt_capture: bool = True
    # Per-message preview cap (chars): the canonical-JSON serialization of one
    # message is hard-truncated here with an explicit marker. Bounded by
    # design — a capture row must never rival the prompt itself in size
    # (debugging aid, not a second transcript store).
    effective_prompt_message_cap: int = 2000
    # Rolling tail window: only the LAST N messages are captured per call; a
    # _meta marker counts what was omitted. 40 covers the working set of a
    # real agent loop (recent turns + newest tool results) while bounding a
    # 100-message harness replay to a readable row.
    effective_prompt_tail_messages: int = 40
    # Whole-payload cap (chars) on the assembled messages JSON — the final
    # bound that makes worst-case row size a constant, not a function of
    # conversation length.
    effective_prompt_total_cap: int = 65536
    # Retention (seconds) for captured rows; 0 = keep forever. Opportunistic
    # prune piggybacks the capture write at most once/60s process-wide —
    # 7 days of debugging evidence, not an unbounded table on the PVC/WAL.
    effective_prompt_retain_s: float = 7 * 24 * 3600.0

    # --- client tool-server dispatch (ROADMAP §7 phase 2 wave B) ---
    # The client-tool-server spike surface: a key may REGISTER its local
    # tools (POST /v1/dispatch/register) and answer relay dispatches via a
    # long-poll (GET /v1/dispatch/poll + POST /v1/dispatch/result), so the
    # model calls the client's bash/edit like any in-cluster tool and the
    # result returns INTO the running relay loop. Default OFF (fail-safe):
    # the router stays inert in production until wave B2 flips it after
    # human review of the design doc — no route is mounted and no relay
    # path consults the offer registry while this is false.
    client_tool_dispatch: bool = False
    # Bounded wait for one client tool dispatch (seconds): a client that is
    # offline/busy produces a tool ERROR after this long, never a hang.
    # Per-tool timeout_s values from the offer are clamped to [1, this].
    client_tool_dispatch_timeout_s: float = 120.0

    # --- MCP federation flat surface ---
    # tools/list re-exposes the calling key's granted upstream tools as
    # individual MCP tools named '<server>__<tool>' (each with the upstream
    # inputSchema/annotations). The per-key ACL filter and the single gate
    # chain are unchanged — flat calls funnel through the same path as
    # mcp_call. False = kill switch: tools/list reverts to exactly the two
    # gateway tools (mcp_catalog/mcp_call) and flattened names resolve to
    # unknown-tool errors, i.e. the pre-flat surface bit-for-bit.
    mcp_flat_tools: bool = True
    # TTL (seconds) for the per-server upstream listing cache — raw,
    # KEY-INDEPENDENT tool listings shared by tools/list and mcp_catalog;
    # per-key ACL filtering always happens after the cached fetch. The cache
    # is invalidated on every admin MCP-registry mutation. Cold start: the
    # first listing after boot still pays the upstream fan-out (bounded at
    # the 15 s metadata timeout per server) — accepted.
    mcp_catalog_cache_s: float = 60.0
    # Flattened-call failover: retries AFTER the primary server on
    # transport-level failure (unreachable/timeout/protocol error — never on
    # upstream tool-error results, which are real answers). Explicit
    # mcp_call(server, tool) targets never fail over: the caller named the
    # server. Candidates must be granted + policy-admitted + present in the
    # cached listing.
    mcp_failover_attempts: int = 2
    # Stringy-arguments tolerance for the federated MCP surfaces (flat
    # '<server>__<tool>' AND explicit mcp_call): some client harnesses wrap
    # upstream tool arguments one level too deep — the observed live shape
    # (2026-09-21, DSH four-part calls) is {'arguments': '<json-string>'},
    # which fleet pydantic models reject as missing-field (or worse, silently
    # ignore on defaulted params). When set, exactly ONE level of that shape
    # is unwrapped: str payload JSON-decoded; dict payload used as-is. All
    # other shapes pass through untouched — a LEGITIMATE argument named
    # 'arguments' (MCP-compliant, upstream-owned) is never rewritten; a
    # wrapped-but-not-JSON string raises the upstream's own missing-field
    # error so misshapen calls stay visible instead of being masked.
    mcp_unwrap_arguments: bool = True

    # --- embedding cache ---
    # Dedupes identical (model, input) embedding calls — the RAG-app
    # optimization: cached hits cost zero upstream GPU work and zero budget.
    embedding_cache_enabled: bool = True
    # Inputs larger than this (bytes) are never cached — giant payloads
    # buy nothing and bloat the table.
    embedding_cache_max_input_bytes: int = 64 * 1024

    # --- retention (ROADMAP §5: operational tables are NOT the ledger) ---
    # The usage ledger is the audit artifact and is NEVER pruned (the budget
    # engine sums it; deleting rows would silently raise every key's
    # headroom). The OPERATIONAL tables are different: agent_traces grows one
    # row per relay step and conversations/conversation_messages hold full
    # transcripts per key — on embedded-Postgres units that insert volume is
    # also WAL volume, so the PVC feels it first. Prunes are OFF by default
    # (nothing is deleted until the operator opts in — the roadmap's own
    # contract), piggyback the registry-refresh housekeeping hook (error-
    # swallowed, same as the rate_windows prune), and run at most once per
    # refresh. 0 = disabled.
    retention_trace_days: int = 0
    retention_conversation_days: int = 0

    # --- off-peak (demand-shaped) billing ---
    # When enabled, a background evaluator periodically samples an
    # admin-configurable PromQL through the REGISTERED prometheus MCP server
    # (in-cluster) and hysteresis-switches a low-demand state (enter below
    # enter_pct, exit above exit_pct, unchanged in the band between). Every
    # settle stamps the ledger row with the CURRENT billing_fraction; budget
    # gates then consume tokens*fraction / cost*fraction while the ledger
    # keeps TRUE usage for audit. Disabled, or ANY evaluator failure ->
    # fraction 1.0 (fail-safe: never discount by accident).
    offpeak_enabled: bool = False
    offpeak_check_s: float = 30
    offpeak_promql: str = "avg(avg_over_time(DCGM_FI_DEV_GPU_UTIL[5m]))"
    offpeak_enter_pct: float = 20
    offpeak_exit_pct: float = 35
    offpeak_fraction: float = 0.5
    # The named ENABLED registry server the evaluator calls (resolved from
    # the same mcp_servers snapshot the federation layer uses).
    offpeak_mcp_server: str = "prometheus"
    offpeak_tool: str = "prom_query"
    # Per-model off-peak queries: model name -> PromQL returning a 0-100 load
    # percentage for THE RESOURCES THAT MODEL SERVES ON. A model with an entry
    # bills by its OWN hysteresis state; models without one fall back to the
    # global offpeak_promql. JSON object in the env (pydantic parses it), e.g.:
    # PCAI_LLM_OFFPEAK_MODEL_PROMQL='{"GLM-5.3-Flash": "100 * avg(avg_over_time(sglang:token_usage{pod=~\"glm-53-flash-predictor.*\"}[5m]))"}'
    # WHY: the global GPU query averages EVERY GPU in the cluster — training
    # jobs and idle nodes dilute a busy model's utilization to the point the
    # discount flips while its GPUs are pegged (live: GLM's 4 GPUs at ~90%,
    # global average ~22%, enter threshold 20%).
    offpeak_model_promql: dict[str, str] = Field(default_factory=dict)

    # Native wire visibility for the tool relay: re-emit the model's
    # tool_call deltas as native OpenAI chunks DURING the loop (no
    # finish_reason — that stays at the turn's true end). WHY THE DEFAULT IS
    # NOW OFF: the deltas were designed as INFORMATIONAL ("the relay owns
    # execution regardless"), but the DSH harness's OpenAI stream client
    # EXECUTES any tool_call delta it sees — for in-cluster (pure-cloud)
    # batches that meant double execution: the relay runs the tool in-
    # cluster, DSH re-runs the same call against /mcp, and for flat names
    # DSH holds no registration it fails with "unknown tool" and feeds that
    # phantom error back to the model (observed live 2026-09-21, trace
    # 948c1228: relay executed all five calls ok while the harness errored
    # every one). In-cluster visibility survives as the relay's reasoning-
    # text trace ("[tool] server__tool → ok (ms)") and the pcai tool_call
    # SSE events. Handback deltas (client-offered tools) are emitted
    # unconditionally and are NOT affected by this flag. Set true only for
    # clients that genuinely render deltas without executing them.
    relay_stream_wire_tools: bool = False

    # --- content guardrails ---
    # Default timeout for a guardrail 'webhook' engine POST (per-guardrail
    # config timeout_s overrides it). The call itself fails OPEN on timeout
    # (pass-with-event) — a slow moderator degrades the audit trail, it
    # must never stall the relay.
    guardrail_webhook_timeout_s: float = 10.0

    # --- complexity-driven model routing (pseudo-models routing/routing__X) ---
    # Master switch (core.model_routing). Per-profile policy ('off'/'shadow'/
    # 'active' in profiles.routing_policy) still gates who participates; this
    # kill-switch flips the whole feature off fleet-wide without touching any
    # profile — an opted-in profile then behaves exactly like 'off' (and its
    # X-PCAI-Route header reads "off" so the state is visible). Fail-safe:
    # any router error serves the profile ceiling, never fails the request.
    routing_enabled: bool = True
    # Complexity cut points, in estimated tokens, on the request score that
    # maps to a position on the profile's cheap-first ladder: score < tier1
    # serves the cheapest ladder model (>= routing_min_model floor), score
    # < tier2 the middle segment, score >= tier2 the ceiling itself. Defaults
    # sized so a short chat turn (~a few hundred words incl. system prompt)
    # stays on the cheapest tier while a RAG/agent context with tooling
    # lands mid-ladder or above; tune per fleet from the shadow-ledger data.
    routing_tier1_tokens: int = 1500
    routing_tier2_tokens: int = 6000
    # Tool-definition JSON bytes per score token. Tool schemas are dense,
    # key-heavy JSON that tokenizes far worse than prose (braces, quotes and
    # repeated names dominate), so the plain chars/4 heuristic undercounts
    # them; 64 bytes ≈ 1 token keeps a 12 KB toolset from masquerading as
    # 12 K tokens of prose. The byte term also subsumes tool COUNT (more
    # tools = more bytes), which is why there is no separate per-tool adder.
    routing_tool_bytes_per_token: int = 64
    # Score adder per message beyond the first. chars/4 already counts the
    # accumulated transcript, but conversation STATE (earlier turns, tool
    # results, corrections) demands a stronger model even when the latest
    # turn is short — the adder is the heuristic for that gap.
    routing_turn_adder_tokens: int = 100

    # --- router extras (review §4a/4b/4c; ledger authoritative throughout) ---
    # Per-model fallback chains (model_fallbacks table, admin-managed via
    # GET/PUT /admin/models/{name}/fallbacks): the maximum fallback MODELS
    # tried after the primary when it cannot serve (pool.pick()==None) or
    # fails with an upstream 5xx / transport error after its breaker report.
    # Each candidate is re-gated (registered, key-allowed, healthy chat pool,
    # check_budgets headroom) at walk time — a fallback is never a budget
    # bypass. 0 = configured chains are never walked (kill switch).
    model_fallback_max: int = 2
    # Zero-completion insurance: when a chat call answers 2xx with EMPTY
    # content, zero completion tokens and finish_reason=stop, retry the
    # identical call EXACTLY once (both attempts settle — the first was real
    # GPU spend; shared request_id correlates the pair). Applies to the
    # non-streaming JSON path and the relay's final answer; NEVER to plain
    # streaming (buffering breaks transparency). DEFAULT OFF (fail-safe):
    # flip after observing the empty-completion rate in the ledger.
    zero_completion_retry: bool = False

    # --- SSRF guard (admin-registered upstream URLs) ---
    # Hard-denied ALWAYS: non-http(s) schemes, loopback (127.0.0.0/8, ::1)
    # and link-local/metadata ranges (169.254.0.0/16 incl. 169.254.169.254,
    # fe80::/10) — a leaked admin key must not become a metadata-service
    # one-liner. RFC1918 and *.svc.cluster.local stay ALLOWED by default:
    # in-cluster model/MCP upstreams ARE private addresses here.
    # Extra denies for stricter sites: CIDR strings, JSON array env
    # (e.g. PCAI_LLM_URL_DENY_CIDRS='["10.0.0.0/8"]').
    url_deny_cidrs: list[str] = []

    # --- identity (Phase A of the fleet identity architecture) ---
    # Trust the oauth2-proxy identity headers (X-Auth-Request-User / X-Email /
    # X-Forwarded-Groups) on the browser rung of the identity resolver
    # (core/identity.py). DEFAULT OFF: the headers are client-spoofable at
    # the pod — the flag is safe ONLY where an enforcing proxy overwrites
    # them on every request AND an Istio AuthorizationPolicy (pcai-helm-lib
    # _authorizationpolicy.tpl, off by default) keeps unauthenticated
    # callers from bypassing the proxy. The flag alone is NOT the control;
    # flipping it without proxy + AuthorizationPolicy lets any caller assert
    # any identity. Mirrors RAG_TRUST_PROXY_IDENTITY (the fleet convention).
    trust_proxy_identity: bool = False
    # Stamp X-MCP-Caller: <subject or key-fp> on every upstream fleet MCP
    # call (resolve_mcp_headers — the single choke point both the /mcp call
    # path and the relay executor use). ATTRIBUTION-NEVER-AUTHORIZATION:
    # the header is non-secret, non-authorizing metadata; upstreams may
    # record it in their audit chain and must never authorize off it. The
    # name is RESERVED: tenants cannot supply it via mcp_server_creds (400
    # at write) and the gateway's stamp overwrites any stale value (the
    # last-wins merge order would otherwise let a tenant override spoof
    # identity — the verified vector this closes).
    mcp_caller_attribution: bool = True

    # --- identity Phase B (token validation — review §1.1 rung 2) ---
    # Validates Keycloak-issued JWTs on the Authorization: Bearer header via
    # the IdP's JWKS endpoint. ALL THREE must be set for a token to be
    # examined; with any of them empty (the default) the token rung is
    # INERT and every request resolves exactly as today (Phase B flag-off
    # byte-identity). The gateway is NEVER a token issuer and NEVER
    # refreshes or stores tokens — validation only (DECISIONS "OAuth
    # REVISION (final)": OAuth terminates at the gateway).
    token_issuer: str = ""
    token_jwks_url: str = ""
    token_audience: str = ""
    # TTL (seconds) for the cached JWKS signing keys (Keycloak rotates
    # keys); an UNKNOWN key id (kid) triggers an immediate refresh-once
    # before the token is rejected.
    token_jwks_cache_s: float = 300.0
    # Clock-leeway (seconds) granted JWT exp/nbf checks (small; never a
    # refresh path — an expired token that fails here falls through the
    # ladder, it does not get renewed).
    token_leeway_s: float = 5.0
    # Token shadow-keys: first VALID token sighting for an unknown subject
    # lazily provisions user+key (bound_via='token-shadow', scopes
    # ['proxy','mcp'], config-sourced limits) so the ledger keeps its
    # key_id anchor. DEFAULT OFF (fail-safe): off = tokens validate but
    # never provision — the pre-Phase-B anonymous-fp posture.
    token_shadow_keys: bool = False
    # Shadow-key provisioning targets: the tenant users are created under
    # and the key's name prefix. tenant_id empty = shadow-keys disabled
    # even with the flag on (a shadow key must live in SOME tenant).
    token_shadow_tenant_id: str = ""
    token_shadow_key_name: str = "token-shadow"
    # Shadow-key defaults (config-sourced; the console can tighten them
    # per key afterwards, exactly like enrollment-created keys).
    token_shadow_rate_limit_rpm: int = 60
    token_shadow_rate_limit_tpm: int = 100_000
    # /v1/keys/for-me: browser-header identity (ladder rung 1, requires
    # PCAI_LLM_TRUST_PROXY_IDENTITY) may mint a key bound to its OWN
    # subject. Grants stay admin-controlled; no admin escalation. The
    # endpoint is the surface — this flag gates it (403 when off).
    sso_key_create_enabled: bool = False
    # Admin gate second path (review §1.5): browser-header identity in
    # ANY of these groups (comma-separated, case-sensitive match against
    # X-Forwarded-Groups) authenticates the admin API when
    # PCAI_LLM_TRUST_PROXY_IDENTITY=true. DEFAULT EMPTY = the group path
    # is off; direct callers keep the bearer-key path unchanged.
    admin_groups: str = ""

    # --- enrollment-time per-user memory datasets (review §2; DECISIONS
    # "APPROVED w/ refinement — opt-in at redemption, not always-provision") ---
    # Deployment default for the redeem request's `memory_opt_in` field: a
    # redeem request that does not pass the field (None) falls back to this.
    # DEFAULT FALSE (fail-safe: no gateway-side provisioning unless the
    # deployment opts in) — the user's decision was "prompt them for memory"
    # with per-deployment defaults living in configuration.
    enroll_memory_default: bool = False
    # Registry name of the RAG MCP server the memory datasets are provisioned
    # on (the same name the fleet bundle registers, `mm-rag`). The dataset
    # create call resolves THIS row from the mcp_servers registry snapshot —
    # never a free-form URL (SSRF posture: the URL is admin-registered and
    # _check_url-validated at registration; enrollment never takes one).
    enroll_memory_server: str = "mm-rag"
    # Bounded 409-retry: when RAG reports the name taken (a re-redeem or a
    # rare collision), the name is suffixed -2/-3/-4 up to this many EXTRA
    # attempts before provisioning is abandoned (reported, never fatal).
    enroll_memory_name_retries: int = 3

    # --- observability ---
    metrics_enabled: bool = True


# database_url is sourced from env at runtime (pydantic-settings), which
# the constructor stub cannot see — hence the narrow ignore below.
settings = Settings()  # type: ignore[call-arg]
