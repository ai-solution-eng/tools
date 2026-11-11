"""REMEDIATION R2 (audit 2026-11-10) — budgets, costs and routing.

One section per finding; the audit id is in each docstring.

  * P1-4   the TPM cache refund is bounded by the request's OWN admission charge
  * P2-15  per-key agent models (extra_candidates) land INSIDE the floor window
  * P2-16  a model_costs read fault cannot lose the ledger row
  * P2-17  routing_config never caches a degraded defaults-only config
  * P2-31  the gate reads spend_ancestors ONCE per admission
  * P3     X-PCAI-Route renders "-" when the shadow router made no pick
  * P3     price_hints skips non-finite rates
  * P3     the offpeak ratio cache uses -inf (not 0.0) as its never-loaded sentinel
  * §7     an unpriced model is priced at the fallback rate when configured, and
           the admin/startup surface can name the unpriced models

The DB seams follow the repo convention: monkeypatch the module-level db
handle with a fake that answers the statements by shape.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_routing as mr
from pcai_llm.core import offpeak
from pcai_llm.core import routing_config as rc

# ---------------------------------------------------------------------------
# shared fakes
# ---------------------------------------------------------------------------


class GateDB:
    """The budget/rate seam: EXISTS probes, the lineage read, the windowed SUMs,
    the rate-window upsert and the clamped refund.

    Counts the spend_ancestors reads (P2-31) and records every refund ATTEMPT
    (P1-4), so both findings are asserted against the real call sites.
    """

    def __init__(self) -> None:
        self.token_budgets: list[dict[str, Any]] = []
        self.usd_budgets: list[dict[str, Any]] = []
        self.ancestors: list[uuid.UUID] = []
        self.ancestor_reads = 0
        self.window_tokens = 0
        self.requests = 0
        self.refund_attempts: list[int] = []
        self.ledger_rows: list[tuple] = []
        # The admission cache-ratio history (rate_limit_predict_cached).
        self.cached_sum = 0.0
        self.prompt_sum = 0.0

    async def fetchval(self, sql: str, *a: Any) -> Any:
        if "SELECT EXISTS" in sql:
            rows = self.token_budgets if "FROM budgets" in sql else self.usd_budgets
            return bool(rows)
        return None

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        if "spend_ancestors" in sql:
            self.ancestor_reads += 1
            return {"spend_ancestors": self.ancestors}
        if "SUM(cached_tokens)" in sql:
            return {"cached": self.cached_sum, "prompt": self.prompt_sum}
        if "INSERT INTO rate_windows" in sql:
            self.requests += 1
            self.window_tokens += int(a[2])
            return {"requests": self.requests, "tokens": self.window_tokens}
        if "WITH cur AS" in sql:
            prior = self.window_tokens
            self.window_tokens = max(0, prior - int(a[2]))
            self.refund_attempts.append(int(a[2]))
            return {"prior": prior, "applied": self.window_tokens}
        return None

    async def fetch(self, sql: str, *a: Any) -> list[Any]:
        if "FROM budgets" in sql:
            return [{**b, "spent": 0} for b in self.token_budgets]
        if "FROM cost_budgets" in sql:
            return [{**b, "spent": 0.0} for b in self.usd_budgets]
        return []

    async def execute(self, sql: str, *a: Any) -> str:
        if "usage_ledger" in sql:
            self.ledger_rows.append(a)
        return "OK"


class NoCostsDB:
    """Costs seam stub: model_costs is empty, so every row settles unpriced."""

    async def fetch(self, sql: str, *a: Any) -> list[Any]:
        return []

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        return None

    async def execute(self, sql: str, *a: Any) -> str:
        return "OK"

    async def fetchval(self, sql: str, *a: Any) -> Any:
        return None


@pytest.fixture(autouse=True)
def _clean_module_state():
    """The charge memories and the routing cache are process-global."""
    budgets_mod._admission_charges.clear()
    budgets_mod._predict_charges.clear()
    budgets_mod._predict_cache.clear()
    yield
    budgets_mod._admission_charges.clear()
    budgets_mod._predict_charges.clear()
    budgets_mod._predict_cache.clear()
    rc._cache = {}
    rc._cache_at = 0.0


# ---------------------------------------------------------------------------
# P1-4 — the cache refund is bounded by the request's own admission charge
# ---------------------------------------------------------------------------


async def test_refund_is_bounded_by_the_admission_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE EXPLOIT: admission charges the TEXT-ONLY estimate (~1k on agent
    traffic) while cached_tokens is the upstream's REAL prompt size (~50k). The
    refund used to be clamped only against the WINDOW BALANCE, so it drained the
    minute counter — including OTHER requests' charges — to 0 and TPM stopped
    enforcing for the rest of the minute.

    The refund is now min(cached_tokens, this request's admission charge), so a
    request can only give back what it took.
    """
    fake = GateDB()
    fake.window_tokens = 5_000  # charges belonging to OTHER in-flight requests
    fake.requests = 3
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", True)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", False)
    monkeypatch.setattr(costs_mod, "db", NoCostsDB())

    kid = uuid.uuid4()
    gate = await budgets_mod.check_rate_limit(kid, rpm=0, tpm=100_000, est_tokens=1_000, request_id="r1", model="m")
    assert gate.ok
    assert fake.window_tokens == 6_000

    # The upstream reports a 50k prompt of which 49k came from prefix cache.
    await budgets_mod.settle(kid, "m", "chat", 50_000, 100, "ok", 5, "r1", cached_tokens=49_000)

    assert fake.refund_attempts == [1_000], "refund must be clamped by the 1k admission charge"
    assert fake.window_tokens == 5_000, "other requests' charges must survive the refund"
    record = budgets_mod._admission_charges["r1"]
    assert (record.charge, record.refunded) == (1_000, 1_000), "the whole charge was given back"


async def test_relay_multi_settle_refunds_are_cumulatively_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CROSS-VALIDATION 2026-11-10: the relay settles ONCE PER ITERATION with the
    same request_id (toolrelay's per-iteration settle). The first settle used to
    CONSUME the charge record, so iterations 2..N found nothing to bound them and
    refunded their FULL cached_tokens — measured 1_000 charged against
    [1_000, 59_000, 69_000] refunded (129x), which drains the minute window and
    stops TPM enforcing.

    The record now survives and accumulates, so the TOTAL refund of one request
    can never exceed its admission charge.
    """
    fake = GateDB()
    fake.window_tokens = 5_000  # other requests' charges
    fake.requests = 3
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", True)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", False)
    monkeypatch.setattr(costs_mod, "db", NoCostsDB())

    kid = uuid.uuid4()
    await budgets_mod.check_rate_limit(kid, rpm=0, tpm=100_000, est_tokens=1_000, request_id="relay-1", model="m")
    assert fake.window_tokens == 6_000

    # Three relay iterations, each reporting a big cached prompt.
    for cached in (59_000, 59_000, 69_000):
        await budgets_mod.settle(kid, "m", "chat", 60_000, 100, "ok", 5, "relay-1", cached_tokens=cached)
        assert "relay-1" in budgets_mod._admission_charges, "the charge record must survive every settle of the request"

    # A settle whose allowance is exhausted claims 0 and issues NO refund query
    # at all, so exactly one clamped attempt is recorded.
    assert fake.refund_attempts == [1_000], (
        f"the refund must be bounded CUMULATIVELY by the admission charge, saw {fake.refund_attempts}"
    )
    assert sum(fake.refund_attempts) == 1_000, "total refunded must equal the 1k charge, never more"
    assert fake.window_tokens == 5_000, "the minute window keeps every other request's charge"
    record = budgets_mod._admission_charges["relay-1"]
    assert record.refunded == record.charge == 1_000


async def test_predicted_request_never_falls_into_the_refund_branch_on_later_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same multiplicity, prediction ON: the first reconcile POPS the predicted
    charge (the C3 memory contract), so later iterations used to look
    unpredicted and run the REFUND mechanism as well — both mechanisms for one
    request. The mechanism choice now rides the surviving admission record, so
    a predicted request reconciles once and refunds nothing after that."""
    fake = GateDB()
    fake.window_tokens = 1_000
    fake.cached_sum, fake.prompt_sum = 500.0, 1_000.0  # measured ratio 0.5
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", True)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_reconcile", True)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", True)
    monkeypatch.setattr(costs_mod, "db", NoCostsDB())

    kid = uuid.uuid4()
    await budgets_mod.check_rate_limit(kid, rpm=0, tpm=100_000, est_tokens=1_000, request_id="p1", model="m")
    assert fake.window_tokens == 1_500, "predicted charge is half the estimate"

    # Iteration 1: true billable 100 vs charged 500 -> one 400-token refund.
    await budgets_mod.settle(kid, "m", "chat", 1_000, 10, "ok", 5, "p1", cached_tokens=900)
    # Iterations 2-3: must adjust NOTHING (no second mechanism, no new refund).
    await budgets_mod.settle(kid, "m", "chat", 1_000, 10, "ok", 5, "p1", cached_tokens=900)
    await budgets_mod.settle(kid, "m", "chat", 1_000, 10, "ok", 5, "p1", cached_tokens=900)

    assert fake.refund_attempts == [400], f"only the first settle may reconcile, saw refunds {fake.refund_attempts}"
    assert fake.window_tokens == 1_100, "the reconcile landed exactly once"


async def test_model_capacity_reads_the_lineage_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """P2-31 on the routing hot path: model_capacity runs BOTH budget families
    per listing and used to pay the same api_keys PK read twice."""
    fake = GateDB()
    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1_000_000}]
    fake.usd_budgets = [{"model": "*", "period": "month", "limit_usd": 100.0}]
    monkeypatch.setattr(budgets_mod, "db", fake)

    out = await budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"])
    assert set(out) == {"m-a", "m-b"}
    assert fake.ancestor_reads == 1, "one lineage read for both budget families"


async def test_zero_charge_admission_cannot_refund(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate charged 0 tokens (the relay's per-iteration gate, or an empty
    estimate) must refund 0 — a refund larger than the charge is tokens the key
    never spent."""
    fake = GateDB()
    fake.window_tokens = 2_000
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", True)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", False)
    monkeypatch.setattr(costs_mod, "db", NoCostsDB())

    kid = uuid.uuid4()
    await budgets_mod.check_rate_limit(kid, rpm=0, tpm=100_000, est_tokens=0, request_id="r0", model="m")
    await budgets_mod.settle(kid, "m", "chat", 40_000, 10, "ok", 5, "r0", cached_tokens=39_000)

    assert fake.refund_attempts == [], "no charge -> nothing to refund"
    assert fake.window_tokens == 2_000


async def test_settle_without_a_recorded_charge_keeps_the_legacy_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settle whose admission was never recorded (no request_id, or a path
    that never ran check_rate_limit) has no charge to bound the refund by, so
    the historical behaviour stands. Pinned so the fallback is deliberate."""
    fake = GateDB()
    fake.window_tokens = 50_000
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", True)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", False)
    monkeypatch.setattr(costs_mod, "db", NoCostsDB())

    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 50_000, 100, "ok", 5, "", cached_tokens=49_000)
    assert fake.refund_attempts == [49_000]


# ---------------------------------------------------------------------------
# P2-31 — ONE spend_ancestors read per admission (both budget families)
# ---------------------------------------------------------------------------


async def test_gate_reads_spend_ancestors_once_for_both_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token family and the cost family used to each call the SUM helper,
    which each re-read the same api_keys row — two identical PK round trips for
    one admission."""
    fake = GateDB()
    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1_000_000}]
    fake.usd_budgets = [{"model": "*", "period": "month", "limit_usd": 100.0}]
    monkeypatch.setattr(budgets_mod, "db", fake)

    res = await budgets_mod.check_budgets(uuid.uuid4(), "m", 100)
    assert res.ok
    assert fake.ancestor_reads == 1, "one lineage read, shared by both budget families"


async def test_spend_lineage_memo_reads_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The memo the gate hands to both families reads the api_keys row once, and
    the read is LAZY (a seam that overrides the aggregate never pays for it)."""
    fake = GateDB()
    monkeypatch.setattr(budgets_mod, "db", fake)

    lineage = budgets_mod._SpendLineage(uuid.uuid4())
    assert fake.ancestor_reads == 0, "constructing the memo must not touch the DB"
    assert await lineage.get() == []
    assert await lineage.get() == []
    assert fake.ancestor_reads == 1


# ---------------------------------------------------------------------------
# P2-16 — a model_costs read fault cannot lose the immutable ledger row
# ---------------------------------------------------------------------------


async def test_cost_read_fault_still_writes_the_ledger_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The upstream has already been paid: the billing row must land even when
    the price lookup dies. The cost column models "unpriced" as NULL, so the row
    is written with cost_usd = None instead of being lost."""
    fake = GateDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", False)

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("model_costs unreadable")

    monkeypatch.setattr(costs_mod, "compute", _boom)

    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 100, 10, "ok", 5, "r1")

    assert len(fake.ledger_rows) == 1, "the ledger row must still be written"
    row = fake.ledger_rows[0]
    assert row[7] is None, "cost_usd is NULL when the price lookup failed"
    assert row[3] == 100 and row[4] == 10, "the token truth is unaffected"


# ---------------------------------------------------------------------------
# P2-17 — routing_config never caches a degraded defaults-only config
# ---------------------------------------------------------------------------


async def test_boot_fault_serves_defaults_without_caching_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At boot there is no last known policy. The old fault path fell through to
    assigning the merged defaults to the cache and PINNED them for a full TTL —
    the reset the module header promises never to make. Now the merge is served
    uncached, so recovery is immediate."""
    rc._cache = {}
    rc._cache_at = 0.0

    async def _boom(sql: str, *a: Any) -> list[Any]:
        raise RuntimeError("routing_config unreadable")

    monkeypatch.setattr(rc.db, "fetch", _boom)
    got = await rc.get_config()
    assert got["decision_model"] == rc.DEFAULTS["decision_model"]
    assert rc._cache == {}, "a degraded merge must never be promoted into the cache"
    assert rc._cache_at == 0.0

    async def _ok(sql: str, *a: Any) -> list[Any]:
        return [{"key": "decision_model", "value": rc._encode("GLM-5.3-Flash")}]

    monkeypatch.setattr(rc.db, "fetch", _ok)
    recovered = await rc.get_config()
    assert recovered["decision_model"] == "GLM-5.3-Flash", "a recovered DB is read on the next call"


async def test_warm_fault_keeps_last_known_policy_and_throttles_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a warm cache the fault path must keep the LAST KNOWN policy (not the
    defaults) and stamp the refresh, so an outage is one query per TTL rather
    than one per request."""
    primed = {**rc.defaults(), "decision_model": "GLM-last-known"}
    rc._cache = primed
    rc._cache_at = time.monotonic() - rc._CACHE_TTL_S - 1.0

    reads = {"n": 0}

    async def _boom(sql: str, *a: Any) -> list[Any]:
        reads["n"] += 1
        raise RuntimeError("routing_config unreadable")

    monkeypatch.setattr(rc.db, "fetch", _boom)
    got = await rc.get_config()
    assert got is primed, "the last known policy is kept — never the defaults merge"
    assert reads["n"] == 1

    await rc.get_config()
    assert reads["n"] == 1, "a fault must not turn the outage into a per-request DB storm"


# ---------------------------------------------------------------------------
# P2-15 — per-key agent models are reachable when a floor is configured
# ---------------------------------------------------------------------------

QWEN = "Qwen3.8-27B"
GLM = "GLM-5.3-Flash"
DS = "Deepseek-V4.1-Flash-1017"
AGENT = "agent-coder"


class _Key:
    def __init__(self) -> None:
        self.user_id = uuid.uuid4()
        self.id = uuid.uuid4()
        self.allowed_models: list[str] = []


async def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: dict[str, Any],
    decide: Any = None,
) -> tuple[str, mr.RouteDecision, list[str]]:
    """resolve_routed_model with a GLM floor and the agent as a per-key extra."""

    async def _policy(user_id: Any) -> tuple[str, str]:
        return "active", GLM

    async def _enabled() -> bool:
        return True

    monkeypatch.setattr(mr, "profile_routing_config", _policy)
    monkeypatch.setattr(mr, "global_enabled", _enabled)
    monkeypatch.setattr(settings, "routing_enabled", True)
    offered: list[str] = []

    async def _decide(candidates: list[str], _body: dict[str, Any], _score: int) -> Any:
        offered.extend(candidates)
        return mr.DecisionOutcome(ok=False, reason="abstained")

    model, ctx = await mr.resolve_routed_model(
        _Key(),
        body,
        "routing__" + DS,
        {QWEN, GLM, DS, AGENT},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=_decide if decide is not None else None,
        extra_candidates=[AGENT],
    )
    return model, ctx, offered


async def test_extra_candidates_are_offered_to_the_decision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prepend used to shift floor_pos up by len(extras), leaving every
    extra BELOW the floor — so with a floor set the per-key agent was
    unreachable by the decision channel too."""
    body = {"model": "routing__" + DS, "messages": [{"role": "user", "content": "hi"}]}
    _model, _ctx, offered = await _resolve(monkeypatch, body=body, decide=True)
    assert offered, "a 3-rung window must reach the decision seam"
    assert AGENT in offered, f"the per-key agent must be inside the offered window, got {offered}"
    assert GLM in offered and DS in offered


async def test_extra_candidates_are_reachable_by_the_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same defect, heuristic channel: a mid-tier request must be able to land
    on the agent instead of only the floor/ceiling rungs."""
    body = {
        "model": "routing__" + DS,
        "messages": [{"role": "user", "content": "x" * 12_000}],  # ~3000 est tokens: tier1..tier2
    }
    model, _ctx, _offered = await _resolve(monkeypatch, body=body, decide=None)
    assert model == AGENT, f"the mid-tier pick must be able to reach the agent, got {model}"


# ---------------------------------------------------------------------------
# P3 — X-PCAI-Route honesty
# ---------------------------------------------------------------------------


def test_shadow_header_renders_a_dash_when_the_router_made_no_pick() -> None:
    """would='' is the documented honest "no pick". The header used to fall back
    to the actually-served model, announcing a shadow claim that was never
    made."""
    d = mr.RouteDecision(
        policy="shadow",
        engaged=True,
        ceiling=DS,
        would="",
        reason="nothing_available",
        stamp=True,
    )
    assert mr.route_header(d, "routing") == f"shadow ceiling={DS} would=- reason=nothing_available"


def test_shadow_header_still_names_a_real_pick() -> None:
    d = mr.RouteDecision(policy="shadow", engaged=True, ceiling=DS, would=QWEN, reason="tier1", stamp=True)
    assert mr.route_header(d, "routing") == f"shadow ceiling={DS} would={QWEN} reason=tier1"


# ---------------------------------------------------------------------------
# P3 — price_hints skips non-finite rates
# ---------------------------------------------------------------------------


async def test_price_hints_omit_non_finite_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """compute() has a finite guard; price_hints did not, so the decision model
    was told "in nan / out 1 USD"."""

    async def _load() -> dict[str, dict[str, float]]:
        return {
            "nan-in": {"input_per_m": float("nan"), "output_per_m": 1.0, "cached_per_m": 0.0},
            "inf-out": {"input_per_m": 1.0, "output_per_m": float("inf"), "cached_per_m": 0.0},
            "priced": {"input_per_m": 0.5, "output_per_m": 1.5, "cached_per_m": 0.1},
        }

    monkeypatch.setattr(costs_mod, "_load", _load)
    hints = await costs_mod.price_hints(["nan-in", "inf-out", "priced"])
    assert hints == {"priced": "in 0.5 / out 1.5 USD per 1M tokens"}


# ---------------------------------------------------------------------------
# P3 — the offpeak ratio cache loads on a low-uptime host
# ---------------------------------------------------------------------------


def test_offpeak_ratio_cache_loads_on_a_low_uptime_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.0 as the "never loaded" sentinel against time.monotonic() (UPTIME-based)
    means a host younger than the TTL never loads the console's ratios at all —
    the fourth instance of the class costs.py documents as fixed."""
    import asyncio

    offpeak.reset()
    try:
        assert offpeak._overrides_loaded_at == float("-inf")

        class _DB:
            def __init__(self) -> None:
                self.reads = 0

            async def fetch(self, sql: str, *a: Any) -> list[Any]:
                self.reads += 1
                return [{"model_name": GLM, "fraction": 0.25}]

        fake = _DB()
        monkeypatch.setattr(offpeak, "db", fake)
        # Host uptime 10s: shorter than the 60s TTL. With the 0.0 sentinel the
        # TTL test is 10 - 0 < 60 -> the cache never loads.
        monkeypatch.setattr(time, "monotonic", lambda: 10.0)
        asyncio.run(offpeak.load_overrides())
        assert fake.reads == 1, "the first load must not be skipped by the TTL"
        assert offpeak._overrides == {GLM: 0.25}
    finally:
        offpeak.reset()


# ---------------------------------------------------------------------------
# §7 — unpriced models: fallback rate + the surface that names them
# ---------------------------------------------------------------------------


class _FallbackSettings:
    """Stand-in for the Settings object carrying the fallback knob.

    The knob is read through getattr, so a deployment whose Settings predates
    the field reads 0.0 (feature off) — that is the default-off test below, run
    against the REAL settings. Turning it on needs the attribute to exist, and
    the field itself belongs to config.py (outside this zone), so the enabled
    cases pin the knob through this shim.
    """

    def __init__(self, rate: float) -> None:
        self.cost_fallback_per_m = rate


async def test_unpriced_model_stays_unpriced_when_the_fallback_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _load() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(costs_mod, "_load", _load)
    # The shipped knob defaults to 0.0 = OFF: an unpriced model keeps its NULL.
    monkeypatch.setattr(costs_mod.settings, "cost_fallback_per_m", 0.0)
    assert costs_mod.fallback_rate() is None
    assert await costs_mod.compute("brand-new", 1_000_000, 0, 0) is None
    # And an explicitly-zero knob is OFF, not "free" — 0.0 cannot be told apart
    # from the legacy unpriced NULL in the ledger.
    monkeypatch.setattr(costs_mod, "settings", _FallbackSettings(0.0))
    assert await costs_mod.compute("brand-new", 1_000_000, 0, 0) is None


async def test_fallback_rate_prices_an_unpriced_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knob's whole point: with a rate configured the USD gate can bind on a
    model nobody priced, instead of NULL-ing forever."""

    async def _load() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(costs_mod, "_load", _load)
    monkeypatch.setattr(costs_mod, "settings", _FallbackSettings(2.0))
    assert await costs_mod.compute("brand-new", 1_000_000, 0, 0) == costs_mod.Decimal("2.000000")
    assert await costs_mod.compute("brand-new", 0, 0, 0) == costs_mod.Decimal("0.000000")


async def test_fallback_rate_reaches_the_ledger_cost_column(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = GateDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", False)

    async def _load() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(costs_mod, "_load", _load)
    monkeypatch.setattr(costs_mod, "settings", _FallbackSettings(10.0))

    await budgets_mod.settle(uuid.uuid4(), "unpriced", "chat", 1_000_000, 0, "ok", 5, "r1")
    assert fake.ledger_rows[0][7] is not None, "a configured fallback must price the row"


async def test_unpriced_and_warn_unpriced_name_the_gap(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _load() -> dict[str, Any]:
        return {"priced": {"input_per_m": 1.0, "output_per_m": 1.0, "cached_per_m": 0.0}}

    monkeypatch.setattr(costs_mod, "_load", _load)
    assert await costs_mod.unpriced(["priced", "new-b", "new-a", "new-a"]) == ["new-a", "new-b"]

    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.costs"):
        missing = await costs_mod.warn_unpriced(["priced", "new-a"])
    assert missing == ["new-a"]
    assert any("UNPRICED" in rec.getMessage() for rec in caplog.records)


async def test_unpriced_is_fail_safe_on_a_cost_table_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom() -> dict[str, Any]:
        raise RuntimeError("model_costs unreadable")

    monkeypatch.setattr(costs_mod, "_load", _boom)
    assert await costs_mod.unpriced(["a"]) == [], "a fault must not invent a warning list"
