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
    # Upstream HTTP connection-pool ceiling for the ONE shared httpx client
    # (chat + embeddings + MCP relay all ride it). Without an explicit limit
    # httpx defaults to max_connections=100 / max_keepalive_connections=20 —
    # and every in-flight SSE stream holds a connection for its WHOLE
    # completion, so concurrent streams beyond the cap queue invisibly
    # (no upstream metric shows it; it surfaces only as gateway-side TTFT).
    # Size against the largest upstream max-running-requests you expect to
    # fan into, not "unlimited". 0 keeps the httpx defaults (pre-2.2.2
    # byte-identical behavior).
    upstream_pool_max_connections: int = 0
    upstream_pool_max_keepalive: int = 20
    upstream_pool_keepalive_expiry_s: float = 75.0
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

    # --- TPM cache-exempt (rate_limit_exclude_cached_tokens) ---
    # The ADMISSION estimate (_estimate_tokens) charges the key's TPM minute
    # window for the FULL prompt (len//4) plus a completion expectation — it
    # has no knowledge of upstream prompt caching, so a high-cache-hit
    # deployment (a ~500k-token context re-sent every turn) 429'd instantly
    # despite the upstream serving most of those tokens from prefix cache.
    # At settle time the gateway now refunds the upstream-reported
    # prompt-cache hits (prompt_tokens_details.cached_tokens) from the key's
    # CURRENT minute window, clamped at 0 (a refund can never mint tokens
    # the window never recorded). Embedding cache_hit rows never ran a gate,
    # so they are excluded — refunding them would credit tokens that were
    # never charged.
    rate_limit_exclude_cached_tokens: bool = True

    # --- TPM admission cache prediction (rate_limit_predict_cached) ---
    # The refund above is POST-HOC: it cannot influence the admission
    # decision that already happened, and a long call settles in the window
    # AFTER the one it charged. Measured consequence on the live gateway: a
    # single 800k-token context charges ~808k at admission (8x a 100k TPM
    # budget) and is refused BEFORE the upstream ever reports that 780k of it
    # was prefix-cached. The refund then lands minutes late, in a rolled-over
    # window, and is discarded.
    #
    # This knob makes the admission charge use the key's OWN MEASURED cache
    # ratio (token-weighted cached/prompt over its recent ledger rows,
    # CACHED in-process) instead of the blind full estimate. A key with no
    # history is charged the FULL estimate, so a new key cannot exploit it;
    # and because the ratio is measured from what the upstream actually
    # reported, a caller cannot inflate it except by genuinely being
    # cache-served.
    #
    # Reconciliation (rate_limit_predict_reconcile) settles the difference
    # against the TRUE usage, so a caller who busts its own cache (a nonce in
    # the prompt) is charged the real billable tokens at settle - the debt
    # lands in the window and bounds the bypass to ONE cheap admission per
    # window rather than a sustained multiplier.
    rate_limit_predict_cached: bool = False
    # Settle the predicted-vs-actual difference (only meaningful with the
    # prediction on). When off, the charge is purely predictive and a
    # cache-busting caller keeps the discount - do not disable this without
    # a reason.
    rate_limit_predict_reconcile: bool = True
    # How many recent ledger rows the measured ratio looks at, and how long
    # the in-process cache holds it. Bounded so the admission path never
    # grows an unbounded query; idx_ledger_key_ts serves it.
    rate_limit_predict_rows: int = 50
    rate_limit_predict_ttl_s: int = 60

    # --- period-budget cached exemption (budget_cached_exempt_fraction) ---
    # The SAME problem the TPM refund fixes, on the slower axis: period token
    # budgets summed FULL prompt_tokens, so a ~500k-token context re-sent
    # every turn burned its (mostly cache-served) prompt against the
    # day/week/month cap and hard-limited the conversation's length no
    # matter how large the budget. The ledger-derived token spend now
    # DISCOUNTS the upstream-reported cached tokens:
    #   billable = total_tokens - cached_tokens * exempt_fraction
    #   1.0 = fully exempt (cached tokens count 0) · 0.9 = 1/10 weight ·
    #   0.0 = legacy (full count). The ADMISSION estimate is discounted by
    #   the same factor (assume the request may be as cache-served as its
    #   history): the settle brings the ledger truth either way, so the
    #   residual budget overshoot is bounded by the CONCURRENT in-flight
    #   requests' undiscounted remainder — the same trade the post-paid USD
    #   gate already accepts. TRUE tokens stay on the ledger row for audit
    #   and the usage-summary rollups; only the budget CONSUMPTION changes.
    budget_cached_exempt_fraction: float = 0.9

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

    # --- reasoning continuity (2026-10-26) -------------------------------
    # Whether the model is shown its OWN prior reasoning: in-loop (the
    # assistant rows a relay turn builds between tool calls) and inbound (the
    # reasoning a client replays in its history).
    #
    # WHY THIS EXISTS: with both paths off, a 32-iteration relay turn hands the
    # model up to 31 consecutive examples of itself emitting tool calls with no
    # thinking in front of them — an in-context demonstration to skip reasoning.
    # Measured over 140 real sessions: 86.1% of relayed tool-call steps carried
    # no reasoning, vs 46.1% going direct to the same model. The model was being
    # TAUGHT to stop thinking by its own replayed history.
    #
    # WHY IT IS SAFE NOW (the 2026-09-18 storm): that incident was UNBOUNDED
    # growth — a client re-appended each streamed reasoning block per cycle and
    # one stored event reached 6,630 copies of a single line (~401 KB). The old
    # answer was to strip reasoning entirely. The bound is the real fix: every
    # re-fed block is capped at reasoning_history_max_chars, so the worst case
    # over a 32-iteration turn is 32 x cap (~63 KB at the 2000-char default)
    # instead of unbounded. Verified against the served endpoint: GLM accepts
    # its own reasoning back in every shape, including alongside tool_calls.
    #
    # 0 disables re-feeding entirely (the pre-2026-10-26 behaviour, kept as the
    # escape hatch). Raise the cap only with the token budget in mind — this is
    # input that rides EVERY subsequent iteration of the turn.
    reasoning_history_enabled: bool = True
    reasoning_history_max_chars: int = 2000
    # 'explicit' (default, 2026-10-09 operator decision): a key sees ONLY its
    # held (server, tool) pairs — the profile's live-grant presets ∪ its own
    # ACL rows (federation.load_grants), deny rows still winning. The profile
    # IS the envelope: a server the profile withholds (applygate, k8s-ops…)
    # is invisible and uncallable no matter what scopes the key carries.
    # 'scope' (the pre-2026-10-09 default, now opt-in legacy): an mcp-SCOPED
    # key gets all policy-admitted tools of every enabled server — "registered
    # servers attach to the model". INCOMPATIBLE with profile least-privilege
    # when self-mint stamps the mcp scope on every key (the SSO poweruser
    # leak, 2026-10-09): only for deployments that grant mcp scope deliberately.
    tool_relay_grant_mode: str = "explicit"
    # Self-service MCP surface (homepage "MCP tools" dialog): a keyed user
    # manages THEIR OWN key's per-server credential overrides (mcp_server_creds
    # — same Fernet write-only discipline as the admin path) and toggles tool
    # grants WITHIN their entitlement (held pairs; under legacy scope mode
    # also the scope-admitted pairs). A self-grant outside that set is 403 —
    # self-service redistributes the admin's envelope, never expands it.
    # VISIBILITY (2026-10-09): the panel lists only entitled tools, and omits
    # a whole server the key holds nothing on — withheld servers are not
    # shown disabled, they do not appear. False hides the /v1/mcp/selfservice
    # routes entirely (admin paths unchanged).
    mcp_self_service: bool = True
    # Cloud shadowing: a client MCP tool (mcp__<server>__<tool>) whose TOOL
    # name matches exactly one granted gateway tool is dropped from the
    # OFFER — the model calls the in-cluster twin and the relay executes it
    # without the client's VPN hop. Handback remains the safety net for
    # calls made under the client's original name. Kill switch for a
    # semantic-mismatch surprise (same tool name, different behavior).
    tool_relay_shadow_mcp: bool = True

    # Stamped inert tool-call deltas (Wave R1): mid-loop, the relay re-emits
    # each in-cluster tool call as a NATIVE OpenAI tool_call delta stamped
    # with `pcai.relay: true`, so render-capable clients (DSH) can show real
    # cards for server-executed calls. NOT relay_stream_wire_tools: these
    # deltas carry the stamp, and only clients that understand the stamp
    # (suppress execution, render display-only) opt in by sending
    # X-PCAI-Relay-Deltas: 1; every other client gets today's bytes. The
    # tool_call index space starts at tool_relay_render_index_base so a
    # rendered delta can never merge with a handback's enumeration (which
    # starts at 0).
    tool_relay_render_deltas: bool = False
    tool_relay_render_index_base: int = 1000

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
    # off by default answers no debugging questions. NOTE (audit P2-4): this
    # is INDEPENDENT of the retention_* knobs (those default OFF = keep
    # forever for their own tables) — an operator reading the retention
    # knobs must not conclude nothing is retained; this capture writes up to
    # a 64 KB preview per REAL model call and prunes at
    # effective_prompt_retain_s (7 days). Helm values expose both knobs.
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

    # --- MCP argument-level policy stage (hardening P0-1) -----------------
    # The gateway's ACLs are PAIR-granular ("may this key use
    # searxng.fetch_content?") while the exfiltration class this closes is
    # ARGUMENT-granular: a model hands a URL to a PERMITTED tool and the
    # gateway's own pod makes the request. This stage walks every string
    # argument (bounded: depth 6, 200 strings, 8 KiB scanned each) and applies
    # the fleet's ONE SSRF deny-list (core/url_guard) to DESTINATION-shaped
    # strings. It runs on BOTH model-driven paths: /mcp (_gated_mcp_call) and
    # the relay (_relay_executor).
    #
    # enforce (default) | warn | off. Enforce is the default because the
    # deny-list is a DESTINATION deny-list — it does not stop a legitimate
    # public web fetch. An unrecognised value FAILS CLOSED to enforce (a typo
    # must never silently disable the stage).
    mcp_arg_policy: str = "enforce"
    # Per-tool escape hatch: comma-separated 'server.tool' globs (e.g.
    # "searxng.*,workbench.*"). Empty = no exemptions. No fleet tool is known
    # to legitimately take an internal URL today.
    mcp_arg_policy_exempt: str = ""
    # Egress-class servers (comma-separated name globs): only for these are
    # in-cluster names denied and DNS resolved. Empty = the federation table's
    # egress class (searxng). Every OTHER fleet server legitimately talks to
    # *.svc.cluster.local, so this must stay narrow.
    mcp_arg_egress_servers: str = ""
    # Deny in-cluster hostnames (.svc, .svc.cluster.local, .cluster.local, ...)
    # on egress-class servers: a model has no business fetching the cluster
    # control plane through a web fetcher.
    mcp_arg_deny_cluster_hosts: bool = True
    # Resolve egress-class HOSTNAMES at argument time (2 s bound) and deny a
    # name that maps into a denied range. A resolution FAILURE is not evidence
    # of danger and allows the call (the documented url_guard residual).
    mcp_arg_policy_resolve: bool = True
    # Payload-shape telemetry (hardening P2-3): observe base64-blob /
    # long-query / long-path argument shapes on the metric, NEVER refuse.
    # A false refusal would break legitimate large payloads; the payload axis
    # needs an egress proxy, not a heuristic.
    mcp_arg_telemetry: bool = True

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
    # canonical_conversations (the distiller's INPUT store): full message
    # history as JSONB, and the biggest table on a busy gateway -- nothing
    # else prunes it, so without this it grows without bound. DISTILLED-ONLY
    # by design: a row is deletable only once a marker proves a memory was
    # written for it (see _prune_canonical_store). Rows awaiting distill are
    # never deleted, so enabling this cannot lose un-distilled conversations.
    # 0 = disabled (the same opt-in contract as the two knobs above).
    retention_canonical_days: int = 0
    # model_stickiness (load-aware cascade): one row per (key, conversation
    # thread) recording the head model the conversation started on. Unlike
    # the two knobs above this one ships ON (default 14) — sticky rows are
    # pure routing state (a row whose model left the profile ordering or the
    # key's ACL is dropped on sight), so the table must not grow forever on
    # a deployment that never opts in. 0 = keep forever (the operator's
    # explicit choice, same semantics as the two above).
    retention_stickiness_days: int = 14

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

    # --- load-aware cascade head (auto entry, off-peak-driven) ---
    # The `auto` cascade entry picks its HEAD model by live off-peak state
    # at conversation START and the choice is STICKY for the conversation
    # ("if it went to GLM, stay with GLM"). OFF by fleet default: while
    # false, resolve_cascade behaves byte-for-byte as before (no stickiness
    # read/write, no off-peak read, profile-order head) — the critical
    # backwards-compat gate. The pick reads the SAME per-model hysteresis
    # state offpeak.effective_for bills by (billing itself is untouched);
    # with no per-model eval configured the feature is inert (every
    # candidate ranks equally -> profile order). Any fault in the
    # stickiness store or the signal degrades to the profile-order head —
    # a routing nicety must never break admission.
    load_aware_cascade: bool = False

    # Demand-aware head EXCLUSION list (operator decision, 2026-10): model
    # names the load-aware head must never pick as a conversation-start
    # head. The legacy `auto` walk and the failover tail keep the FULL
    # profile order — an excluded model is still reachable as a pinned
    # request and as a cascade fall-through when every higher candidate is
    # budget-exhausted; it just never WINS the demand-aware head pick
    # (stickiness included: a stored sticky head on the exclusion list is
    # dropped and re-picked). The G2 use case: Qwen3.8-27B stays on the
    # ladder as the budget-overflow rung, but new conversations should
    # always head to GLM/Deepseek. Names must match registered model names
    # EXACTLY (same convention as offpeakModelPromql keys). A list naming
    # every candidate degrades gracefully: _reorder_head finds no eligible
    # candidate and returns the list unchanged (the plain profile order),
    # so the feature self-disables rather than bricking admission.
    demand_aware_excluded_models: list[str] = Field(default_factory=list, max_length=100)

    # --- per-model max_tokens ceiling (the "never forward a cap the model
    # cannot satisfy" guard) ---
    # A MODEL-LEVEL cap: virtual model name -> the largest max_tokens that
    # model's upstream can satisfy. An entry caps max_tokens on EVERY
    # upstream call serving that virtual model — every endpoint, every
    # path (non-streaming, streaming, tool relay), every failover hop
    # (the clamp follows the model ACTUALLY serving the attempt, not the
    # one requested). Models with no entry are untouched. JSON object in
    # the env (pydantic parses it), e.g.:
    # PCAI_LLM_MAX_TOKENS_CEILING='{"Qwen3.8-27B": 32768}'
    # WHY: clients ship a fixed max_tokens (the operator's `auto` entry
    # sends 262144) and the gateway forwarded it VERBATIM; an upstream
    # whose context is 262144 then rejects any request where
    # max_tokens + prompt > context with an HTTP 400 at the API layer —
    # ~86% of Qwen's calls failed in 6h while GLM (1M ctx) and Deepseek
    # were unaffected. The ceiling is applied SILENTLY: the client's
    # larger value is REDUCED, never raised (a small client value stays
    # small — this is a cap, not a floor), so a request that would have
    # 400'd is served with a shorter completion allowance instead.
    # FAIL-SAFE: a 0 or negative entry is IGNORED (no cap) rather than
    # clamping every request to zero output — a malformed value must
    # never mute a model.
    max_tokens_ceiling: dict[str, int] = Field(default_factory=dict)

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
    # Conversation stickiness (never-downgrade): a routed conversation holds a
    # capability FLOOR and may escalate above it but never fall below it, so a
    # thread that opened with a trivial turn cannot pin later hard work to the
    # cheap model. Default OFF — it changes which model serves, so it is opt-in.
    # The DB routing config (sticky_routing) is the operator override; this env
    # is the deployment default and the kill switch.
    sticky_routing: bool = False
    # Idle reset for that floor, in seconds. A conversation untouched for this
    # long forgets its floor and re-decides fresh; without it a long thread
    # climbs monotonically to the ceiling. 0 = never reset.
    sticky_routing_idle_reset_s: int = 1800
    # Promote the legacy `auto` alias into routing. Default OFF: `auto` keeps
    # its documented meaning (the profile-order budget cascade), so arming
    # routing never silently changes what an existing client's request does.
    # The explicit routing / routing__X names always route regardless.
    route_default_model: bool = False
    # Complexity cut points, in estimated tokens, on the request score that
    # maps to a position on the profile's cheap-first ladder: score < tier1
    # serves the cheapest ladder model (>= routing_min_model floor), score
    # < tier2 the middle segment, score >= tier2 the ceiling itself. Defaults
    # sized so a short chat turn (~a few hundred words incl. system prompt)
    # stays on the cheapest tier while a RAG/agent context with tooling
    # lands mid-ladder or above; tune per fleet from the shadow-ledger data.
    routing_tier1_tokens: int = 1500
    routing_tier2_tokens: int = 6000
    # REMOVED 2026-10-26: routing_tool_bytes_per_token. Tool definitions are
    # no longer scored at all (see complexity_score's tool_tokens=0 note): an
    # agent harness ships the same catalog every turn, so the byte term was a
    # constant that inflated every request toward the ceiling without
    # separating easy from hard. The knob outlived its consumer and an
    # operator tuning it saw no effect -- removed rather than left as a decoy.
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
    # TTFT failover budget (the "200-then-queue-stall" defect, 2026-10-04):
    # after a 2xx, _stream_chat waits up to this many seconds for the
    # upstream's first SSE DATA line before declaring the attempt stalled
    # and walking the fallback chain (keepalive COMMENT lines don't count —
    # they are exactly what a queued SGLang emits while stalled). 0 disables
    # the gate entirely (byte-identical legacy path: commit to the 2xx
    # whatever its latency). Default 45s: long enough for a cold but healthy
    # endpoint's first token, far short of the 600s upstream timeout a
    # wedged-but-alive upstream used to hold.
    failover_ttft_s: float = 45.0
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

    # Read+egress separation (P0-3, 2026-10-26 hardening): the one control
    # against a key holding BOTH a read-class grant (k8s-ops, logsearch,
    # mm-rag, sqlhandler read tools, prometheus, seaborn) AND an egress-class
    # grant (searxng) — the combination that reads data and then sends it,
    # i.e. the exfiltration channel of the plan's attack. The pure predicate
    # is mcpf.federation.read_egress_violation over the server classes in
    # MCP_SERVER_CLASSES (one constant).
    #   enforce — a granted read+egress pair combination REFUSES the call
    #             (ledger status 'policy_blocked', metric reason 'read+egress')
    #   warn    — DEFAULT: metric + one WARNING log line naming the key id and
    #             the offending (read, egress) pair; the call proceeds.
    #             Deliberate default: the live session key holds 111/111
    #             tools, so flipping to enforce without operator review of
    #             existing grants would take the fleet's own agent offline
    #             mid-flight. Review the warn log / gateway_mcp_policy_block
    #             series, split keys with preset stamps, THEN flip here.
    #   off     — the check is skipped entirely.
    # An unrecognized value resolves to warn (fail-safe, and a typo'd env
    # var must not take the gateway down at boot).
    mcp_separate_read_egress: str = "warn"

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

    # --- session distiller (the harness-independent memory pipeline) ---
    # Master switch for the /v1/distill surface. False leaves the router
    # UNMOUNTED (the routes_mcp_self pattern: an off surface 404s like a
    # route that never existed). Even when on, the surface is INERT for a
    # key whose memory feature is not attached: no enabled registry row for
    # enroll_memory_server, or no stamped {X-Memory-Dataset,
    # X-Dataset-Password} pair on the key - both are required (see
    # routes_distill.memory_attached).
    distiller_enabled: bool = False
    # Which registered MCP server is THE memory server for the distiller, and
    # which tool writes memories on it (mm-rag: add_memory). Deployment
    # topology, not runtime policy — the admin panel tunes model/scheduling,
    # not which server holds memories.
    distiller_memory_server: str = "mm-rag"
    distiller_memory_tool: str = "add_memory"
    # The cheap capability probe the distiller calls (with the KEY'S OWN
    # credentials) before spending model tokens: answers available/writable/
    # unlocked for the CALLING identity. mm-rag exposes memory_status; a
    # backend without it degrades to tools/list presence checking.
    distiller_memory_probe_tool: str = "memory_status"
    # Fleet-designated summarizer model for distillation. Empty = the
    # calling key's OWN routing (resolve_cascade on auto): tenant-visible
    # cost, profile ordering, tenant budgets. Set a model name to pin a
    # cheap uniform summarizer instead - still settled against the calling
    # key (the distill is real spend, the ledger must see it).
    distiller_model: str = ""
    # Distill output cap (the DSH plugin default maxDistillTokens).
    # 4096 (raised from the plugin's 2048, 2026-10-23): the summary contract
    # targets 300-1200 words of path/command-dense markdown, which alone can
    # pass 2048 tokens -- at 2048 the cap cut two live summaries mid-document
    # (finish_reason="length" with content present) that then shipped whole.
    # The cap only binds when hit, so raising it costs nothing on calls that
    # finish early.
    distiller_max_tokens: int = 4096
    # Reject a distill whose reply hit the max_tokens cap MID-DOCUMENT
    # (2026-10-23, the truncated-write defect): finish_reason="length" with
    # NON-empty content used to pass as a successful distill and was written
    # whole. true (default) raises core.distiller.DistillTruncatedError
    # instead: map-stage parts keep their partial notes (intermediate
    # compression), the FINAL call fails, and the row stays due (with
    # distiller_raw_fallback=false) so a bigger budget or the next sweep
    # produces a complete summary -- no truncated document ever ships. The
    # retry is backoff-gated like every model failure; the log names this
    # knob. false = the pre-fix write-anyway behavior (truncation is silent
    # there -- not recommended).
    distiller_reject_truncated: bool = True
    # Near-duplicate suppression at the memory write (2026-10-23). The dup
    # clusters in the live store are CROSS-SESSION: the same session story
    # re-distilled from parallel DSH threads (sqlhandler deployment distilled
    # 4x from 4 different session ids; hugepages runbook 3x; pricing PDF 3x)
    # -- mm-rag folds only at cosine >= 0.995, far below independent LLM
    # paraphrases. Before writing a project-summary, search the dataset with
    # the document's own text; a highly similar existing document from a
    # DIFFERENT session suppresses the write (the cluster killer); a similar
    # document from the SAME session allows it (the designed re-distill
    # update for continued conversations). Best-effort: probe failures write
    # through.
    distiller_dedup_superseded: bool = True
    # The MCP tool the dedup probe calls (the memory server's search).
    distiller_memory_search_tool: str = "search_memory"
    # Token-containment threshold for "near-duplicate": |hit ∩ new| /
    # min(|hit|, |new|) over lowercase word tokens of the body text.
    # CALIBRATED 2026-10-23 on the live store: sqlhandler near-dup siblings
    # score 0.796-0.828, unrelated topic-siblings 0.222-0.391 -- 0.70 sits
    # in the wide gap between the two populations.
    distiller_dedup_containment: float = 0.70
    # Raw-transcript fallback on a FAILED distill (2026-10-10).
    #
    # true (the historical DSH contract): when a model call fails AFTER the
    # gates passed, write the raw transcript as kind=session_history instead of
    # losing the session. The cost is that the memory store stops being a clean
    # project map: mm-rag CHUNKS the raw transcript, so ONE failed distill can
    # land ~100+ documents of transcript fragments that then dilute recall
    # (measured: 126 such chunks from a single session).
    #
    # false (recommended where the conversation is already retained): log the
    # reason and leave the row DUE, so the next sweep retries it. Nothing is
    # lost because canonical_conversations still holds the conversation -- the
    # memory store simply never receives anything but distilled summaries.
    #
    # Note this only governs the "model was reachable but failed/returned no
    # text" case (DistillModelFailure). A missing model route or memory server
    # (DistillUnavailable) never wrote a fallback and still retries cleanly.
    distiller_raw_fallback: bool = True
    # Per-call upstream deadline for one distill LLM call. The map-reduce
    # chunker bounds the NUMBER of calls; this bounds EACH call - well under
    # the 600s model timeout because a distill is background-quality
    # enrichment, not an interactive turn.
    distiller_llm_timeout_s: float = 120.0
    # Suppress the summarizer model's THINKING TRACE on distill calls
    # (2026-10-20, the "upstream produced no text" defect).
    #
    # A reasoning model (Qwen3.x served by SGLang with --reasoning-parser
    # qwen3, GLM, DeepSeek-R1-class) emits its chain of thought into
    # reasoning_content and the ANSWER into content. The distill call reads
    # content only, so when the thinking trace consumes the whole max_tokens
    # budget the reply arrives 200 OK with content="" - every distill fails
    # while the upstream looks healthy. Measured on the fleet Qwen3.8-27B:
    # 69% of distill calls failed this way at max_tokens=2048.
    #
    # true (default) sends chat_template_kwargs {"enable_thinking": false} -
    # the Qwen3/SGLang switch that skips the trace entirely. Endpoints that
    # do not know the field ignore it (it is a chat-template kwarg, not a
    # sampling parameter), so this is safe on non-reasoning models.
    distiller_disable_thinking: bool = True
    # Wall-clock budget for ONE scheduled distill sweep (all rows). Out of
    # budget, remaining due rows defer to the next housekeeping pass — the
    # sweep shares its task with the retention prunes and must never monopolize
    # the loop (cross-review finding).
    distiller_sweep_budget_s: float = 120.0
    # Attachment-gate / extraction-skip backoff (2026-10-09 stall fix): a row
    # the engine skips (memory gate down, transcript collapsed) leaves the
    # oldest-first due window for this long before it is retried, so a broken
    # dependency can no longer pin the window and starve newer rows. The skip
    # is NOT a distilled marker — the row returns to the due set when the
    # window lapses, so a recovered gate still drains the backlog.
    distiller_gate_backoff_s: float = 900.0
    # Title-request exclusion (2026-10-17). Harnesses fire an auxiliary
    # "session title" model call (DSH: after EVERY human prompt). Those calls
    # are real requests and are served/billed normally, but they are not
    # conversations: capturing them makes the distiller write one bogus
    # "session summary" per prompt, whose entire content is a title string.
    # true (default) = never capture them and never select an already-stored
    # one for distill (core.title_requests owns the detection and its
    # false-positive safety rules). false = the pre-fix behavior, kept as an
    # escape hatch in case a deployment relies on title rows for diagnostics.
    canonical_skip_title_requests: bool = True
    # Minimum characters a DISTILLED memory body must carry before it is
    # written to the memory dataset. 0 (default) = no floor.
    #
    # MEASURED 2026-10-17 on the live store, at MEMORY level (600 memories
    # reconstructed from 1,723 stored chunks — mm-rag splits one memory across
    # several chunk documents, so CHUNK counts are not document counts and
    # analysing them as such inflates every short-document figure):
    #
    #     floor  150 / 200 / 250  -> drops 2 memories (0.3%)
    #     floor  300              -> drops 3
    #     floor  500              -> drops 6
    #
    # Both of the two memories under 250 were title artifacts ("Agent Swarm
    # Short-Term Memory", "Título de sesión de pruebas unitarias ST-MEM" with a
    # memory header bolted on). So a 250 floor is NOT dangerous here — an
    # earlier revision of this comment claimed it would delete "58 real
    # fact-cards", which was wrong: that number came from counting 220-char
    # CHUNK FRAGMENTS (the opening slice of a multi-chunk memory) as if each
    # were a complete short card.
    #
    # The default stays 0 anyway, for a different and honest reason: the floor
    # is nearly REDUNDANT with the title guard above. The only two memories it
    # would catch are exactly the ones canonical_skip_title_requests now
    # prevents at capture time. Keep it as an optional second net (150 is the
    # safe value), not as the primary defence. Tune only with a measurement.
    distiller_min_doc_chars: int = 0
    # --- observability ---
    metrics_enabled: bool = True


def clamp_max_tokens(body: dict, model: str, ceilings: dict[str, int]) -> dict:
    """Lower `body["max_tokens"]` to `model`'s ceiling; never raise it.

    THE single clamp used by every upstream-forwarding site (routes_proxy's
    non-streaming, streaming and relay paths, plus toolrelay's per-iteration
    body) — one implementation so the sites cannot drift (settings.
    max_tokens_ceiling, see its comment for the live defect this guards).

    Contract (hot path — this runs on EVERY proxied request):
      * returns a NEW dict when a clamp is needed; the caller's `body` is
        NEVER mutated (the ledger settle and the admission estimate read it
        afterwards, and must keep seeing the CLIENT's numbers);
      * returns the SAME object, zero-copy, whenever nothing needs to change:
          - `model` has no ceiling entry (the common case: opt-in feature);
          - `max_tokens` is absent (nothing to clamp);
          - the entry is 0/negative (ignored, fail-safe) or not a real int
            (a malformed map value must not mute the model);
          - `max_tokens` is not an int (bool excluded — `True` is not a
            token count) or not positive: the estimator already guards
            non-numeric values, and clamping them here could only make
            things worse;
          - the value is already <= the ceiling (a cap never RAISES a small
            client value, and equal is a no-op);
      * everything else in the body is byte-identical — only the one key is
        replaced.
    """
    if not ceilings:
        return body
    ceiling = ceilings.get(model)
    # bool is an int subclass: True would read as ceiling 1 and clamp every
    # request to one token — reject it with the other malformed entries.
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling <= 0:
        return body
    value = body.get("max_tokens")
    if not isinstance(value, int) or isinstance(value, bool) or value <= ceiling:
        return body
    return {**body, "max_tokens": ceiling}


# database_url is sourced from env at runtime (pydantic-settings), which
# the constructor stub cannot see — hence the narrow ignore below.
settings = Settings()  # type: ignore[call-arg]
