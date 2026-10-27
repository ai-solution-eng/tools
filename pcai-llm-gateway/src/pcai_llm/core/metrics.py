"""Prometheus metrics for the gateway (wave: observability essentials).

WHY this module exists: /metrics previously mounted prometheus_client's
DEFAULT registry, which exported only Python process metrics — every
gateway-relevant fact (requests, budget rejections, breaker state, relay
depth) was invisible to Prometheus. The usage LEDGER holds the same facts
but is queryable only through Postgres/admin; a scrape-time Counter is the
surface alerting rules need (5xx ratio, breaker-open, budget-exhaustion
rate).

Design rules (all learned the hard way in this fleet):

1. SINGLE IMPORT POINT. Every other module imports its metrics from HERE —
   never `from prometheus_client import ...` directly at a call site — so
   the optional-dependency guard exists in exactly one place.
2. NO-OP SHIMS on ImportError. prometheus_client is an OPTIONAL dependency
   (the app mounts /metrics import-guarded — app.py's additive-metrics
   convention); the shims below have the same names and method signatures
   as the real objects, so call sites stay UNCONDITIONAL and a missing
   dependency degrades to "metrics are no-ops", never to request failures.
3. NO PER-KEY LABELS. Keys are unbounded and tenant-churned; a per-key
   label would grow the registry without bound and leak tenant names into
   a surface that is exposed on the same port as the data plane. Per-key
   questions belong to the ledger (which is access-controlled), not to
   Prometheus. `model` is bounded by the endpoint registry, `endpoint`
   by fleet size, `server` by the MCP registry — all acceptable.
4. LEDGER-VOCABULARY STATUS. The `status` label reuses usage_ledger.status
   verbatim (ok|error|cache_hit|budget_exceeded|rate_limited) so a metric
   and its ledger row can never disagree about what happened — the
   counter is bumped INSIDE settle() (core.budgets), the single choke
   point every outcome already flows through, rather than at ~25
   scattered call sites that would drift.

Nothing here reads config: /metrics itself stays gated by
PCAI_LLM_METRICS_ENABLED in app.py; this module only defines the series.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

try:  # optional dependency — see design rule 2
    from prometheus_client import Counter, Gauge, Histogram

    _REAL = True
except ImportError:  # pragma: no cover - exercised via tests' sys.modules shim
    _REAL = False
    Counter = Gauge = Histogram = None  # type: ignore[assignment,misc]

# LLM-shaped latency buckets: generation runs 1s..minutes; the default
# prometheus buckets (.005..10) would put every real LLM call in the top
# bucket and make the histogram useless.
_LLM_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0)


class _NoopMetric:
    """Stand-in with the real metric's method surface; records nothing."""

    def labels(self, *_a: Any, **_k: Any) -> _NoopMetric:
        return self

    def inc(self, _amount: float = 1.0) -> None:
        return None

    def dec(self, _amount: float = 1.0) -> None:
        return None

    def set(self, _value: float) -> None:
        return None

    def observe(self, _value: float) -> None:
        return None


if _REAL:
    REQUESTS = Counter(
        "gateway_requests_total",
        "Request outcomes, vocabulary identical to usage_ledger.status.",
        ["model", "status", "kind"],
    )
    LATENCY = Histogram(
        "gateway_request_latency_seconds",
        "End-to-end request latency per model and kind.",
        ["model", "kind"],
        buckets=_LLM_BUCKETS,
    )
    TTFT = Histogram(
        "gateway_ttft_seconds",
        "Time to first streamed response chunk.",
        ["model"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
    )
    BUDGET_REJECTIONS = Counter(
        "gateway_budget_rejections_total",
        "Calls refused by a period budget gate.",
        ["kind"],  # tokens | usd (which budget family refused)
    )
    RATE_LIMITED = Counter(
        "gateway_rate_limited_total",
        "Calls refused by a rate/backpressure gate.",
        ["scope"],  # rpm | tpm | concurrency
    )
    BREAKER_OPEN = Gauge(
        "gateway_breaker_open",
        "1 while an endpoint is in cooldown (breaker tripped).",
        ["endpoint"],
    )
    BREAKER_TRIPS = Counter(
        "gateway_breaker_trips_total",
        "Times an endpoint's breaker tripped into cooldown.",
        ["endpoint"],
    )
    RELAY_ITERATIONS = Histogram(
        "gateway_relay_iterations",
        "Model iterations consumed per relay turn (completed streams; "
        "aborted streams surface via requests/latency instead).",
        ["model"],
        buckets=(1, 2, 3, 5, 8, 12, 16, 24, 32),
    )
    MCP_ACL_DENIED = Counter(
        "gateway_mcp_acl_denied_total",
        "MCP calls denied by the default-deny ACL or server tool policy.",
        ["server"],
    )
    # Argument-level policy stage (P0-1, 2026-10-26 hardening plan): the ACL
    # gate answers "may this key use this tool?"; it never sees the ARGUMENTS.
    # This counter makes the new argument stage visible separately from the
    # ACL wall, so "the model tried to reach the metadata service" is a
    # distinguishable series from "the model was not granted the tool".
    # reason vocabulary (bounded, NOT the caller's text):
    #   url-destination | cluster-host | scheme | ip-literal | too-deep
    MCP_POLICY_BLOCKED = Counter(
        "gateway_mcp_policy_blocked_total",
        "MCP calls refused by the argument-level policy stage (destination "
        "deny-list) before the upstream server was contacted.",
        ["server", "tool", "reason"],
    )
    # Structural telemetry for the "retrieved content is data" rule (P2-3):
    # a call whose arguments carry a payload-shaped blob is FLAGGED here, not
    # refused (blocking payloads needs an egress proxy; see the plan's
    # stated limit). signal vocabulary: base64-blob | long-query | long-path
    MCP_ARG_TELEMETRY = Counter(
        "gateway_mcp_arg_telemetry_total",
        "MCP calls whose arguments looked payload-shaped (exfiltration "
        "telemetry for the argument stage; never a refusal).",
        ["server", "tool", "signal"],
    )
    # P2-1 (attribution): MCP counters carried only {tool, outcome} with no
    # caller/model dimension, so the plan's "<1 in 100,000 calls" event was
    # visible as a rate and unattributable. Design rule 3 still forbids a
    # per-KEY label (keys are unbounded, tenant-churned, and the /metrics port
    # is public); the two BOUNDED dimensions that answer "who" are used
    # instead:
    #   model  - bounded by the endpoint registry; "" normalises to
    #            "unattributed" (the relay path settles with model="*" today -
    #            see the P1-1 note in routes_proxy).
    #   server - bounded by the MCP registry.
    # Per-KEY attribution stays in usage_ledger / mcp_call_audit, which are
    # access-controlled and now hash-chained.
    MCP_CALLS = Counter(
        "gateway_mcp_calls_total",
        "MCP tool calls by server, terminal outcome and serving model "
        "(attribution for the exfiltration class; per-key detail lives in "
        "the chained mcp_call_audit trail, never in a metric label).",
        ["server", "outcome", "model"],
    )
    BUDGET_WARNING = Counter(
        "gateway_budget_warning_total",
        "Responses emitted with an X-Budget-Warning threshold header.",
        ["threshold"],  # 80 | 95
    )
    ADMIN_AUTH_FAILURES = Counter(
        "gateway_admin_auth_failures_total",
        "Failed /admin/* authentications (brute-force telemetry the audit "
        "trail can't answer — it records only successful mutations).",
        ["reason"],  # invalid-key | missing-key | group-denied
    )
    UPSTREAM_AUTH_DEGRADED = Gauge(
        "gateway_upstream_auth_degraded",
        "1 while an upstream credential is unresolved (undecryptable panel "
        "token or missing by-ref Secret env) — calls go out UNAUTHENTICATED "
        "and succeed invisibly on upstreams that don't require auth.",
        ["server"],
    )
    CASCADE_HEAD_PICKS = Counter(
        "gateway_cascade_head_picks_total",
        "Cascade head picks for the auto-demand-aware entry "
        "(load-aware cascade). reason: sticky = a conversation-start "
        "pick was reused from the sticky record, load = off-peak "
        "per-model state chose the head, default = feature off/inert "
        "and the profile order head stands, unhealthy = the named "
        "model was EXCLUDED from the ranking because its pool has no "
        "available endpoint (an additional tick beside the pick's own "
        "reason, so the exclusion rate is alertable).",
        ["picked", "reason"],  # picked = registered model name; reason: sticky | load | default | unhealthy
    )
    TPM_CACHE_REFUND = Counter(
        "gateway_tpm_cache_refund_tokens_total",
        "Prompt-cache tokens refunded from TPM rate windows at settle "
        "(rate_limit_exclude_cached_tokens). Window-rollover discards are "
        "reported separately so refunds + discards reconcile against the "
        "ledger cached_tokens column.",
        ["outcome"],  # refunded | discarded
    )
    # --- session-memory distiller (2026-10-14) -----------------------------
    # Added after a TOTAL feed outage that nothing could detect: the sweep
    # raised on every pass, the exception was swallowed at log.debug, /health
    # stayed 200 and no metric existed -- so the only way to notice was a human
    # seeing the memory count stop growing. These three series make the feed
    # observable:
    #   DUE_ROWS      - rows the sweep selected but has not yet written; a
    #                   sustained non-zero value IS the outage signature (the
    #                   backlog was stuck > 0 the whole time while nothing was
    #                   written). Normal steady state hovers near 0.
    #   LAST_WRITE_TS - Unix time of the last SUCCESSFUL memory write; a stale
    #                   value means the pipeline stopped end to end.
    #   WRITES        - completed writes by path/outcome, so a rising
    #                   outcome=failed rate is visible before the store stalls.
    DISTILL_DUE_ROWS = Gauge(
        "gateway_distill_due_rows",
        "Conversations/canonical rows currently due for distillation "
        "(selected but not yet written). Sustained > 0 = the feed is stalled.",
        ["path"],  # conversations | canonical
    )
    DISTILL_LAST_WRITE_TS = Gauge(
        "gateway_distill_last_write_timestamp_seconds",
        "Unix timestamp of the last SUCCESSFUL memory write by the distiller. "
        "Stale = the automatic memory feed has stopped.",
    )
    DISTILL_WRITES = Counter(
        "gateway_distill_writes_total",
        "Distiller memory writes by path and outcome.",
        ["path", "outcome"],  # outcome: ok | failed | skipped
    )
    # --- decision-model routing (2026-10-26) -------------------------------
    # Added because NOTHING could answer "how often does the router call the
    # decision model, and how often does that change the served model?" -- so
    # every threshold (confidence floor, switch margin, call cadence) was
    # tuned blind. These three series close that gap:
    #   DECISION_CALLS  - one tick per decision attempt, by outcome. A high
    #                     outcome=abstained rate means the floor is mis-set;
    #                     outcome=error means the endpoint is unhealthy.
    #   DECISION_LATENCY- the wall time the seam adds to a routed request
    #                     (measured ~300ms on a small state, bounded by the
    #                     configured timeout). This is the user-facing cost.
    #   ROUTE_SWITCHES  - how often the router CHANGES the served model, split
    #                     by whether hysteresis held it. switched is the number
    #                     that decides whether a re-prefill budget is at risk.
    DECISION_CALLS = Counter(
        "gateway_decision_calls_total",
        "Decision-model routing attempts by outcome. outcome: served (the "
        "model's choice was adopted), held (the choice was rejected by the "
        "switch hysteresis margin), abstained (below the confidence floor or "
        "malformed), error (transport/timeout), skipped (fewer than two "
        "candidates, so no call was made).",
        ["outcome"],
    )
    DECISION_LATENCY = Histogram(
        "gateway_decision_latency_ms",
        "Wall time of one decision-model call, in milliseconds.",
        buckets=(50, 100, 200, 300, 500, 750, 1000, 1500, 2500, 5000),
    )
    ROUTE_SWITCHES = Counter(
        "gateway_route_switches_total",
        "Routing outcomes that move a conversation between models. outcome: "
        "switched (the incumbent changed), held (a challenger was blocked by "
        "the switch hysteresis margin), same (the pick equalled the "
        "incumbent), cold (no incumbent yet), shadow_would (shadow mode: a "
        "switch WOULD have happened but nothing was served, so nothing was "
        "re-prefilled).",
        ["outcome"],
    )

else:
    # Deliberate conditional re-definition (import guard) — the mypy-sanctioned
    # pattern; the shims share the real metrics' method surface by construction.
    REQUESTS: Any = _NoopMetric()  # type: ignore[no-redef]
    LATENCY: Any = _NoopMetric()  # type: ignore[no-redef]
    TTFT: Any = _NoopMetric()  # type: ignore[no-redef]
    BUDGET_REJECTIONS: Any = _NoopMetric()  # type: ignore[no-redef]
    RATE_LIMITED: Any = _NoopMetric()  # type: ignore[no-redef]
    BREAKER_OPEN: Any = _NoopMetric()  # type: ignore[no-redef]
    BREAKER_TRIPS: Any = _NoopMetric()  # type: ignore[no-redef]
    RELAY_ITERATIONS: Any = _NoopMetric()  # type: ignore[no-redef]
    MCP_ACL_DENIED: Any = _NoopMetric()  # type: ignore[no-redef]
    MCP_POLICY_BLOCKED: Any = _NoopMetric()  # type: ignore[no-redef]
    MCP_ARG_TELEMETRY: Any = _NoopMetric()  # type: ignore[no-redef]
    MCP_CALLS: Any = _NoopMetric()  # type: ignore[no-redef]
    BUDGET_WARNING: Any = _NoopMetric()  # type: ignore[no-redef]
    ADMIN_AUTH_FAILURES: Any = _NoopMetric()  # type: ignore[no-redef]
    UPSTREAM_AUTH_DEGRADED: Any = _NoopMetric()  # type: ignore[no-redef]
    CASCADE_HEAD_PICKS: Any = _NoopMetric()  # type: ignore[no-redef]
    TPM_CACHE_REFUND: Any = _NoopMetric()  # type: ignore[no-redef]
    DISTILL_DUE_ROWS: Any = _NoopMetric()  # type: ignore[no-redef]
    DISTILL_LAST_WRITE_TS: Any = _NoopMetric()  # type: ignore[no-redef]
    DISTILL_WRITES: Any = _NoopMetric()  # type: ignore[no-redef]
    DECISION_CALLS: Any = _NoopMetric()  # type: ignore[no-redef]
    DECISION_LATENCY: Any = _NoopMetric()  # type: ignore[no-redef]
    ROUTE_SWITCHES: Any = _NoopMetric()  # type: ignore[no-redef]


def observe_relay_iterations(model: str, iterations: int) -> None:
    """One completed relay turn's iteration depth (labeled by served model
    so a tenant-heavy relay model is visible without per-key labels)."""
    RELAY_ITERATIONS.labels(model=model).observe(iterations)


def observe_acl_denial(server: str) -> None:
    """One default-deny ACL / server-tool-policy refusal (the two gates
    are morally one deny wall; the ledger distinguishes them in the row's
    error text — the metric keeps cardinality at one label)."""
    MCP_ACL_DENIED.labels(server=server).inc()


def observe_mcp_policy_block(server: str, tool: str, reason: str) -> None:
    """One argument-stage refusal (P0-1). Labels stay bounded: server and
    tool come from the registry + the grant the caller already holds, and
    reason is a fixed vocabulary chosen by the policy stage itself — the
    caller's URL/host text NEVER becomes a label (design rule 3)."""
    MCP_POLICY_BLOCKED.labels(server=server, tool=tool, reason=reason).inc()


def observe_mcp_arg_telemetry(server: str, tool: str, signal: str) -> None:
    """One payload-shaped-argument observation (P2-3 structural rule).
    Telemetry only — the call proceeds."""
    MCP_ARG_TELEMETRY.labels(server=server, tool=tool, signal=signal).inc()


def observe_mcp_call(server: str, outcome: str, model: str = "") -> None:
    """One MCP tool call's terminal outcome, attributed to the SERVING MODEL.

    outcome vocabulary: ok | error | denied | policy_blocked (bounded).
    model "" becomes "unattributed" — an empty label value is legal but a
    named bucket is what makes the unattributed share alertable, which is the
    point of P2-1: you cannot act on a rare event you cannot attribute.
    """
    MCP_CALLS.labels(server=server, outcome=outcome, model=model or "unattributed").inc()


def observe_tpm_cache_refund(tokens: int, discarded: bool) -> None:
    """One settle-time TPM refund: refunded when the admission-time window
    row was still live (tokens subtracted), discarded when the minute
    window had rolled over between admission and settle (nothing to adjust -
    the refund is dropped rather than credited to a window the request never
    belonged to). Summing refunded+discarded reconciles against the ledger
    cached_tokens column."""
    TPM_CACHE_REFUND.labels(outcome="discarded" if discarded else "refunded").inc(max(0, tokens))


def observe_decision_call(outcome: str, latency_ms: int | None = None) -> None:
    """One decision-model routing attempt.

    outcome: served | abstained | error | skipped. latency_ms is recorded only
    when a call was actually made (skipped makes none, and a 0 in the latency
    histogram would drag the mean down with non-events).
    """
    DECISION_CALLS.labels(outcome=outcome).inc()
    if latency_ms is not None and latency_ms >= 0:
        DECISION_LATENCY.observe(latency_ms)


def observe_route_switch(outcome: str) -> None:
    """One routing outcome relative to the conversation's incumbent.

    outcome: switched | held | same | cold. `switched` is the series that
    decides whether the re-prefill budget is at risk; `held` counts the
    switches the hysteresis margin blocked (the knob's own effectiveness).
    """
    ROUTE_SWITCHES.labels(outcome=outcome).inc()


def observe_distill_due(path: str, rows: int) -> None:
    """Rows currently due for distill on one path (conversations|canonical).

    Sustained non-zero is the outage signature: during the 2026-10-14 stall
    the due set was non-empty every pass while nothing was ever written.
    """
    DISTILL_DUE_ROWS.labels(path=path).set(max(0, rows))


def observe_distill_write(path: str, outcome: str) -> None:
    """One distiller write attempt outcome (ok|failed|skipped).

    On success this also stamps DISTILL_LAST_WRITE_TS, so a single alert can
    watch "time since the feed last wrote anything" instead of guessing at
    throughput thresholds.
    """
    DISTILL_WRITES.labels(path=path, outcome=outcome).inc()
    if outcome == "ok":
        import time as _time

        DISTILL_LAST_WRITE_TS.set(_time.time())


def derive_kind(endpoint: str) -> str:
    """Map a settle() endpoint argument onto the metric `kind` label.

    Mirrors how the ledger uses the endpoint column: "embeddings" is the
    embeddings cache path, "mcp:*" / "mcp " prefixes are federation/relay
    tool calls, everything else is a chat-model call (chat settles pass
    the pool endpoint URL, so default to chat rather than guessing).
    """
    if endpoint == "embeddings":
        return "embeddings"
    if endpoint.startswith(("mcp:", "mcp ")):
        return "mcp"
    return "chat"


def observe_settle(
    model: str,
    endpoint: str,
    status: str,
    latency_ms: int,
    kind: str = "",
) -> None:
    """One request outcome — called from settle() so metric and ledger
    agree by construction (design rule 4). `kind` overrides the derived
    label for callers whose endpoint column doesn't start with mcp:.
    """
    k = kind or derive_kind(endpoint)
    REQUESTS.labels(model=model, status=status, kind=k).inc()
    LATENCY.labels(model=model, kind=k).observe(latency_ms / 1000.0)


def observe_ttft(model: str, seconds: float) -> None:
    """Time to first streamed chunk (elapsed seconds, computed by the
    caller from its own started-clock — monotonic vs wall differences
    stay in the caller)."""
    TTFT.labels(model=model).observe(seconds)


def breaker_trip(endpoint: str) -> None:
    BREAKER_TRIPS.labels(endpoint=endpoint).inc()
    BREAKER_OPEN.labels(endpoint=endpoint).set(1)


def breaker_recover(endpoint: str) -> None:
    BREAKER_OPEN.labels(endpoint=endpoint).set(0)


def budget_warning(threshold: int) -> None:
    BUDGET_WARNING.labels(threshold=str(threshold)).inc()


def admin_auth_failure(reason: str) -> None:
    """One failed /admin/* authentication (reason: invalid-key | missing-key
    | group-denied) — the durable brute-force signal the audit trail
    cannot provide (it records only successful mutations)."""
    ADMIN_AUTH_FAILURES.labels(reason=reason).inc()


def upstream_auth_degraded(server: str, degraded: bool) -> None:
    """Set/clear the per-server degraded-credential gauge (callers decide
    the server label; 1 = calling unauthenticated)."""
    UPSTREAM_AUTH_DEGRADED.labels(server=server).set(1 if degraded else 0)


# reason vocabulary for cascade_head_pick (kept beside the counter so the
# contract lives in exactly one place — the cascade implementation imports
# this to validate/derive its reason):
#   sticky    — conversation-start pick reused from the sticky record
#   load      — off-peak per-model state chose the head (fresh pick)
#   default   — feature off/inert; the profile-order head stands unchanged
#   unhealthy — the named model was EXCLUDED from the load ranking because
#               its pool has no available endpoint. Recorded as an
#               ADDITIONAL tick beside the pick's own reason (one tick per
#               exclusion event, not a replacement), so the exclusion rate
#               is alertable without a second counter.
CASCADE_PICK_REASONS = ("sticky", "load", "default", "unhealthy")


def cascade_head_pick(picked: str, reason: str) -> None:
    """One cascade head decision (load-aware cascade; design brief rule 6).

    picked = the head model name (bounded by the endpoint registry —
    acceptable label cardinality per design rule 3). `reason` should come
    from CASCADE_PICK_REASONS; an unknown value is recorded verbatim rather
    than raising — a metrics call can never fail an admission (the same
    no-op-safe posture as every helper here; the tests pin the vocabulary).
    """
    CASCADE_HEAD_PICKS.labels(picked=picked, reason=reason).inc()
