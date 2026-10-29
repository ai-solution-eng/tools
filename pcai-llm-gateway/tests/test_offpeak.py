"""Off-peak (demand-shaped) billing tests.

Covers the whole feature: the evaluator's hysteresis state machine, the
zero-failure fraction() helper, fail-safe behavior on every MCP failure
path, the first-float parse of PromQL result shapes, the fraction stamped
on settle() rows, the FRACTION-WEIGHTED budget-gate math (ledger keeps
TRUE usage), the X-PCAI-Billing response header, the /admin/offpeak
snapshot route, and the lifespan start/stop gating.

The MCP call is faked at the offpeak._call_tool seam (the session/opener
boundary); DB access is faked with the repo's monkeypatch-the-module-db
pattern. No live Postgres, no network.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import offpeak, registry, toolrelay, traces
from pcai_llm.routing.router import Endpoint, Router

MODEL = "offpeak-model"


@pytest.fixture(autouse=True)
def clean_offpeak_state():
    """Every test starts and ends with pristine evaluator state (and no
    background task leaking across tests)."""
    offpeak.reset()
    yield offpeak.state
    offpeak.reset()


def _fake_registry(monkeypatch: pytest.MonkeyPatch, servers: list[dict[str, Any]]) -> None:
    async def fake_servers() -> list[dict[str, Any]]:
        return servers

    monkeypatch.setattr(registry, "mcp_servers", fake_servers)


def _fake_opener(
    monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]], result: Any = None, error: Exception | None = None
) -> None:
    """Replace offpeak._call_tool (the session/opener seam) with a script."""

    async def fake_call(
        url: str, headers: dict[str, str], tool: str, arguments: dict[str, Any], timeout_s: float
    ) -> Any:
        calls.append(
            {"url": url, "headers": dict(headers), "tool": tool, "arguments": arguments, "timeout_s": timeout_s}
        )
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(offpeak, "_call_tool", fake_call)


# ---------------------------------------------------------------------------
# hysteresis state machine
# ---------------------------------------------------------------------------


def _prom_server() -> dict[str, Any]:
    return {
        "id": "srv1",
        "name": "prometheus",
        "url": "http://prometheus.svc:9090",
        "headers": {},
        "auth_secret": None,
        "auth_token_enc": None,
        "enabled": True,
        "tool_policy": "all",
        "allowed_tools": [],
    }


def test_hysteresis_enter_between_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """< enter_pct -> active; between the thresholds -> UNCHANGED; > exit_pct
    -> idle again. Each settle-visible fraction follows the state."""
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_enter_pct", 20.0)
    monkeypatch.setattr(settings, "offpeak_exit_pct", 35.0)
    monkeypatch.setattr(settings, "offpeak_fraction", 0.5)
    _fake_registry(monkeypatch, [_prom_server()])
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls, result='{"status":"success","data":{"result":[{"value":[1730000000,"10.0"]}]}}')

    asyncio.run(offpeak.evaluate_once())
    assert offpeak.state.active is True, "value below enter_pct must ENTER low-demand"
    assert offpeak.fraction() == 0.5
    assert offpeak.state.last_value_pct == 10.0
    assert offpeak.state.last_error is None
    assert offpeak.state.state_since is not None
    since = offpeak.state.state_since

    # inside the hysteresis band: unchanged (still active, no state flip)
    _fake_opener(monkeypatch, calls, result='{"data":{"result":[{"value":[1730000000,"27.5"]}]}}')
    asyncio.run(offpeak.evaluate_once())
    assert offpeak.state.active is True, "hysteresis band must NOT flip the state"
    assert offpeak.fraction() == 0.5
    assert offpeak.state.last_value_pct == 27.5
    assert offpeak.state.state_since == since, "no transition -> state_since unchanged"

    # above exit_pct: leave low-demand, full price
    _fake_opener(monkeypatch, calls, result='{"data":{"result":[{"value":[1730000000,"88.1"]}]}}')
    asyncio.run(offpeak.evaluate_once())
    assert offpeak.state.active is False, "value above exit_pct must EXIT low-demand"
    assert offpeak.fraction() == 1.0
    assert offpeak.state.state_since != since

    # and back in below enter_pct after exiting (the switch keeps working)
    _fake_opener(monkeypatch, calls, result="3.0")
    asyncio.run(offpeak.evaluate_once())
    assert offpeak.state.active is True and offpeak.fraction() == 0.5

    # the opener seam saw the configured promql/tool and the metadata timeout
    assert calls and calls[0]["tool"] == "prom_query"
    assert calls[0]["arguments"] == {"query": settings.offpeak_promql}
    assert calls[0]["timeout_s"] == 15.0
    assert calls[0]["url"] == "http://prometheus.svc:9090"


def test_hysteresis_thresholds_are_exclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly AT a threshold stays in the band (strict < / > comparisons)."""
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_enter_pct", 20.0)
    monkeypatch.setattr(settings, "offpeak_exit_pct", 35.0)
    offpeak._apply(20.0)
    assert offpeak.state.active is False  # at enter -> band
    offpeak._apply(35.0)
    assert offpeak.state.active is False  # at exit -> band
    offpeak._apply(19.999)
    assert offpeak.state.active is True
    offpeak._apply(35.001)
    assert offpeak.state.active is False


# ---------------------------------------------------------------------------
# fraction() math
# ---------------------------------------------------------------------------


def test_fraction_disabled_is_always_full_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "offpeak_enabled", False)
    monkeypatch.setattr(settings, "offpeak_fraction", 0.5)
    # even a stale active state must not discount while disabled
    offpeak.state.active = True
    offpeak.state.enabled = False
    offpeak.state.fraction = 0.5
    assert offpeak.fraction() == 1.0


def test_fraction_active_vs_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_fraction", 0.25)
    offpeak.state.enabled = True
    offpeak.state.active = False
    assert offpeak.fraction() == 1.0
    offpeak.state.active = True
    offpeak.state.fraction = 0.25
    assert offpeak.fraction() == 0.25


def test_evaluate_once_when_disabled_resets_to_full_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled feature evaluates to the off state — and never calls MCP."""
    monkeypatch.setattr(settings, "offpeak_enabled", False)
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls)
    asyncio.run(offpeak.evaluate_once())
    assert calls == [], "no MCP traffic while the feature is disabled"
    assert offpeak.state.enabled is False and offpeak.state.active is False
    assert offpeak.fraction() == 1.0


# ---------------------------------------------------------------------------
# fail-safe: every failure path -> full price + last_error
# ---------------------------------------------------------------------------


def test_fail_safe_server_missing_from_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    # the registry knows OTHER servers only — the configured one is absent
    other = _prom_server()
    other["name"] = "logsearch"
    _fake_registry(monkeypatch, [other])
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls)
    asyncio.run(offpeak.evaluate_once())
    assert calls == [], "a missing server must never reach the MCP opener"
    assert offpeak.state.active is False
    assert offpeak.state.last_error is not None and "not in the enabled registry" in offpeak.state.last_error
    assert offpeak.fraction() == 1.0


def test_fail_safe_registry_server_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disabled servers drop out of the registry snapshot — same missing case."""
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    _fake_registry(monkeypatch, [])
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls)
    asyncio.run(offpeak.evaluate_once())
    assert offpeak.state.active is False and offpeak.fraction() == 1.0
    assert offpeak.state.last_error is not None


def test_fail_safe_unreachable_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    _fake_registry(monkeypatch, [_prom_server()])
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls, error=ConnectionError("connection refused"))
    asyncio.run(offpeak.evaluate_once())  # must NOT raise
    assert offpeak.state.active is False
    assert offpeak.state.last_error is not None and "connection refused" in offpeak.state.last_error
    assert offpeak.fraction() == 1.0


def test_fail_safe_unparseable_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    _fake_registry(monkeypatch, [_prom_server()])
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls, result="no data")
    asyncio.run(offpeak.evaluate_once())
    assert offpeak.state.active is False
    assert offpeak.state.last_error is not None and "no numeric value" in offpeak.state.last_error
    assert offpeak.fraction() == 1.0


def test_fail_safe_clears_on_next_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    _fake_registry(monkeypatch, [_prom_server()])
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls, error=TimeoutError("timed out"))
    asyncio.run(offpeak.evaluate_once())
    assert offpeak.state.last_error is not None
    _fake_opener(monkeypatch, calls, result="5.0")
    asyncio.run(offpeak.evaluate_once())
    assert offpeak.state.last_error is None, "success must clear last_error"
    assert offpeak.state.active is True and offpeak.fraction() == 0.5


# ---------------------------------------------------------------------------
# first-float parsing (json + text shapes)
# ---------------------------------------------------------------------------


def _text_block(text: str) -> Any:
    """Stand-in for an MCP TextContent block."""
    return SimpleNamespace(text=text)


def test_first_float_prometheus_vector_json() -> None:
    """The vector sample is [epoch_ts, "value"] — the VALUE, not the epoch."""
    payload = (
        '{"status":"success","data":{"resultType":"vector",'
        '"result":[{"metric":{"gpu":"0"},"value":[1730000000,"23.5"]}]}}'
    )
    assert offpeak.first_float(_text_block(payload)) == 23.5


def test_first_float_prometheus_scalar_json() -> None:
    payload = '{"status":"success","data":{"resultType":"scalar","result":[1730000000,"41.25"]}}'
    assert offpeak.first_float(_text_block(payload)) == 41.25


def test_first_float_plain_text_and_bare_shapes() -> None:
    assert offpeak.first_float(_text_block("gpu_utilization 37.5")) == 37.5
    assert offpeak.first_float("42") == 42.0
    assert offpeak.first_float({"value": [1730000000, "12.5"]}) == 12.5
    assert offpeak.first_float([1730000000, "9.75"]) == 9.75  # bare sample pair
    assert offpeak.first_float(_text_block("avg: -3.5 percent")) == -3.5


def test_first_float_no_number_returns_none() -> None:
    assert offpeak.first_float(_text_block("no data")) is None
    assert offpeak.first_float(None) is None
    assert offpeak.first_float("") is None

# ---------------------------------------------------------------------------
# D-3 (audit 2026-10-29): an EMPTY Prometheus result is NO SAMPLE, not zero
# ---------------------------------------------------------------------------


def test_d3_first_float_empty_result_is_no_sample() -> None:
    """A SUCCESSFUL answer with an EMPTY result set still carries numbers
    (elapsed_ms, seriesFetched). PRE-FIX first_float fell back to a regex
    over the WHOLE payload and returned one of those metadata numbers as the
    utilization sample (verified on the pre-fix module: 3.0 and 0.0 for the
    two shapes below) — near zero, below enter_pct, so the discount engaged
    exactly when demand was UNKNOWN: the fail-safe inverted. Empty result =
    None, whatever else the envelope contains."""
    assert (
        offpeak.first_float(
            {"status": "success", "data": {"resultType": "vector", "result": []}, "elapsed_ms": 3}
        )
        is None
    )
    assert (
        offpeak.first_float(
            {"status": "success", "data": {"resultType": "vector", "result": []}, "stats": {"seriesFetched": 0}}
        )
        is None
    )
    assert offpeak.first_float({"status": "success", "data": {"resultType": "scalar", "result": []}}) is None


def test_d3_first_float_metadata_only_dict_is_no_sample() -> None:
    """A parsed dict with NO sample in it must not fall back to scanning the
    whole payload text: any number left in a Prometheus envelope is response
    metadata, never a utilization. Plain text keeps the regex fallback."""
    # no "data" key: _extract finds the only number (took) -> that IS the sample
    assert offpeak.first_float({"status": "success", "took": 12}) == 12.0
    # a dict whose ONLY numbers ride in metadata keys we deliberately don't scan
    assert offpeak.first_float({"status": "error", "elapsed_ms": 12, "errorType": "timeout"}) is None
    assert offpeak.first_float({"status": "success", "data": {"resultType": "matrix", "result": []}}) is None
    # non-dict shapes keep the legacy fallback (free text / bare lists)
    assert offpeak.first_float(_text_block("gpu_utilization 37.5")) == 37.5
    assert offpeak.first_float([1730000000, "9.75"]) == 9.75


def test_d3_real_samples_still_parse_after_empty_guard() -> None:
    """The empty-result guard must not eat REAL samples."""
    payload = '{"status":"success","data":{"resultType":"vector","result":[{"metric":{},"value":[1730000000,"23.5"]}]}}'
    assert offpeak.first_float(_text_block(payload)) == 23.5
    assert offpeak.first_float({"status": "success", "data": {"resultType": "scalar", "result": [1730000000, "7.5"]}}) == 7.5


def test_d3_empty_result_evaluator_fails_safe_full_price(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evaluator-level: an empty Prometheus result must land in _fail_state
    (idle + full price + last_error), NOT in _apply_state(≈0) -> discount ON."""
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    _fake_registry(monkeypatch, [_prom_server()])
    calls: list[dict[str, Any]] = []
    _fake_opener(
        monkeypatch,
        calls,
        result={"status": "success", "data": {"resultType": "vector", "result": []}, "elapsed_ms": 3},
    )
    asyncio.run(offpeak.evaluate_once())
    st = offpeak.state
    assert st.active is False and st.fraction == 1.0 and offpeak.fraction() == 1.0
    assert st.last_value_pct is None, "no sample was parsed — nothing to stamp"
    assert st.last_error is not None and "no numeric value" in st.last_error


# ---------------------------------------------------------------------------
# settle stores the fraction / gate math is fraction-weighted
# ---------------------------------------------------------------------------


class WeightedLedgerDB:
    """Fake db that keeps ledger rows WITH their billing_fraction and
    evaluates the budget aggregate exactly like the real query:
    SUM(col * billing_fraction) over ok/error rows (window filters omitted —
    every fake row is in-window)."""

    def __init__(self) -> None:
        self.ledger: list[dict[str, Any]] = []
        self.token_budgets: list[dict[str, Any]] = []
        self.usd_budgets: list[dict[str, Any]] = []
        self.captured_sql: list[str] = []

    def _weighted(self, col: str) -> float:
        # Mirrors the REAL aggregate expression (core.budgets._budget_token_expr):
        # the token SUM discounts the upstream-reported cached tokens by
        # budget_cached_exempt_fraction; the cost SUM is untouched (costs
        # price cache hits at cached_per_m instead).
        frac = min(1.0, max(0.0, float(budgets_mod.settings.budget_cached_exempt_fraction)))

        def _row(r: dict[str, Any]) -> float:
            v = r[col]
            if col == "total_tokens" and frac > 0.0:
                total, cached = r["total_tokens"], r.get("cached_tokens", 0)
                v = max(total - min(cached, total) * frac, 0)
            return v * r["billing_fraction"]

        return sum(
            _row(r)
            for r in self.ledger
            if r["status"] in ("ok", "error")  # cache_hit rows are FREE
        )

    async def fetch(self, sql: str, *a: Any):
        self.captured_sql.append(sql)
        if "FROM budgets" in sql:
            return [{**b, "spent": self._weighted("total_tokens")} for b in self.token_budgets]
        if "FROM cost_budgets" in sql:
            return [{**b, "spent": self._weighted("cost_usd")} for b in self.usd_budgets]
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetchval(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        self.captured_sql.append(sql)
        if "usage_ledger" in sql:
            # settle() arg order: key_id, model, endpoint, prompt, completion,
            # total, cached, cost, status, latency, request_id, billing_fraction
            self.ledger.append(
                {
                    "model": a[1],
                    "total_tokens": a[5],
                    "cached_tokens": a[6],
                    "cost_usd": float(a[7]) if a[7] is not None else 0.0,
                    "status": a[8],
                    "billing_fraction": a[11],
                }
            )
        return "INSERT 0 1"


def test_settle_stores_billing_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The INSERT carries the fraction (default 1.0, explicit when passed)."""
    fake = WeightedLedgerDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)  # costs.compute reads model_costs
    kid = uuid.uuid4()
    asyncio.run(budgets_mod.settle(kid, "m", "chat", 100, 10, "ok", 5, "rid-1", billing_fraction=0.5))
    asyncio.run(budgets_mod.settle(kid, "m", "chat", 7, 3, "ok", 5, "rid-2"))
    assert len(fake.ledger) == 2
    assert fake.ledger[0]["billing_fraction"] == 0.5, "explicit fraction must be the INSERT arg"
    assert fake.ledger[1]["billing_fraction"] == 1.0, "default settle stays full price"
    insert_sqls = [sql for sql in fake.captured_sql if "usage_ledger" in sql]
    assert insert_sqls and "billing_fraction" in insert_sqls[0]


def test_gate_math_sums_are_fraction_weighted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two ledger rows (800 tokens @1.0 + 800 @0.5): TRUE usage is 1600,
    budget consumption is 1200 — the SUM halves the discounted row."""
    fake = WeightedLedgerDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    kid = uuid.uuid4()
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 800, 0, "ok", 5, "rid-1", billing_fraction=1.0))
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 800, 0, "ok", 5, "rid-2", billing_fraction=0.5))
    assert sum(r["total_tokens"] for r in fake.ledger) == 1600, "ledger keeps TRUE usage"

    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1201}]
    res = asyncio.run(budgets_mod.check_budgets(kid, MODEL, 0))
    assert res.ok, "weighted spend (1200) fits the 1201 limit; unweighted (1600) would not"

    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1100}]
    res = asyncio.run(budgets_mod.check_budgets(kid, MODEL, 0))
    assert not res.ok and "1200" in res.reason, "gate consumes the WEIGHTED spend"

    # the REAL aggregate query must be fraction-weighted (pins the SQL, not
    # just the fake's arithmetic). Two legal shapes: the legacy
    # SUM(total_tokens * fraction) when budget_cached_exempt_fraction is 0,
    # and the cache-discounted GREATEST(total - LEAST(cached, total)*frac, 0)
    # * fraction otherwise (default 0.9 = cached tokens fully exempt). Both
    # are fraction-weighted; neither may lose the fraction factor.
    assert any(
        "SUM(l.total_tokens * l.billing_fraction)" in sql
        or ("GREATEST(l.total_tokens" in sql and "l.billing_fraction)" in sql)
        for sql in fake.captured_sql
    ), "token budget SUM must be fraction-weighted (legacy or cache-discounted)"
    assert any("SUM(l.cost_usd * l.billing_fraction)" in sql for sql in fake.captured_sql), (
        "cost budget SUM must be fraction-weighted"
    )


def test_c1_token_gate_refuses_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1 (audit 2026-10-29): spent >= limit is an EXHAUSTED budget even at
    zero estimate. The old gate admitted it: spent + est*(1-frac) > limit
    with est=0 is spent > limit, so a fully-spent budget kept admitting on
    the zero-estimate relay/MCP paths forever. Weighted spend 1200 vs limit
    1200 must now refuse (the 1201 variant that still admits lives in
    test_gate_math_sums_are_fraction_weighted)."""
    fake = WeightedLedgerDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    kid = uuid.uuid4()
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 800, 0, "ok", 5, "rid-1", billing_fraction=1.0))
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 800, 0, "ok", 5, "rid-2", billing_fraction=0.5))
    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1200}]
    res = asyncio.run(budgets_mod.check_budgets(kid, MODEL, 0))
    assert not res.ok, "spent == limit is exhausted; zero-estimate must refuse (audit C1)"
    assert "1200" in res.reason


def test_cost_budget_gate_is_fraction_weighted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = WeightedLedgerDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    kid = uuid.uuid4()
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 100, 0, "ok", 5, "rid-1", billing_fraction=1.0))
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 100, 0, "ok", 5, "rid-2", billing_fraction=0.5))
    # true cost 4.0, weighted 3.0 (costs.compute is unpriced -> rows cost 0;
    # stamp cost directly to isolate the gate math)
    fake.ledger[0]["cost_usd"] = 2.0
    fake.ledger[1]["cost_usd"] = 2.0

    # post-paid gate: spent >= limit refuses (overshoot bounded by one response)
    fake.usd_budgets = [{"model": "*", "period": "month", "limit_usd": 3.01}]
    res = asyncio.run(budgets_mod.check_budgets(kid, MODEL, 0))
    assert res.ok, "weighted cost spend (3.0) fits the 3.01 limit"

    fake.usd_budgets = [{"model": "*", "period": "month", "limit_usd": 2.5}]
    res = asyncio.run(budgets_mod.check_budgets(kid, MODEL, 0))
    assert not res.ok and "cost budget" in res.reason


def test_budget_status_reflects_weighted_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    """budget_status inherits the weighted aggregate (same query path) while
    limits stay as configured."""
    fake = WeightedLedgerDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    kid = uuid.uuid4()
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 800, 0, "ok", 5, "rid-1", billing_fraction=1.0))
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 800, 0, "ok", 5, "rid-2", billing_fraction=0.5))
    fake.ledger[0]["cost_usd"] = 2.0
    fake.ledger[1]["cost_usd"] = 2.0
    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 2000}]
    fake.usd_budgets = [{"model": "*", "period": "month", "limit_usd": 10.0}]

    out = asyncio.run(budgets_mod.budget_status(kid))
    tok = next(b for b in out if b["kind"] == "tokens")
    usd = next(b for b in out if b["kind"] == "usd")
    assert tok["spent"] == 1200, "token view shows the weighted spend"
    assert tok["limit"] == 2000
    assert usd["spent_usd"] == pytest.approx(3.0), "usd view shows the weighted spend"
    assert usd["limit_usd"] == 10.0


# ---------------------------------------------------------------------------
# relay settle sites pass the CURRENT fraction
# ---------------------------------------------------------------------------


def test_relay_settle_passes_active_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """toolrelay settles stamp offpeak.fraction() read AT the settle moment."""
    settles: list[tuple[tuple, dict[str, Any]]] = []

    async def spy_settle(*a: Any, **kw: Any) -> None:
        settles.append((a, kw))

    async def spy_record(*a: Any, **kw: Any) -> None:
        pass

    monkeypatch.setattr(toolrelay, "settle", spy_settle)
    monkeypatch.setattr(traces, "record", spy_record)
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_fraction", 0.5)
    offpeak.state.enabled = True
    offpeak.state.active = True
    offpeak.state.fraction = 0.5

    async def stream_once(msgs: list[dict], tools: list[dict] | None) -> Any:
        yield ("usage", {"prompt_tokens": 7, "completion_tokens": 3})

    async def consume() -> None:
        async for _ in toolrelay.relay_stream(
            [{"role": "user", "content": "hi"}],
            set(),
            [],
            stream_once,
            None,  # type: ignore[arg-type]  # no tool calls -> executor unused
            "rid",
            MODEL,
            key_id=uuid.uuid4(),
            key_fraction=0.5,  # caller resolves the KEY's base (profile ?? env)
        ):
            pass

    asyncio.run(consume())
    assert settles and settles[0][1].get("billing_fraction") == 0.5, (
        "the relay model-call settle must carry the CURRENT off-peak fraction"
    )


def test_relay_settle_marker_carries_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mid-loop budget_exceeded marker settle also stamps the fraction."""
    settles: list[tuple[tuple, dict[str, Any]]] = []

    async def spy_settle(*a: Any, **kw: Any) -> None:
        settles.append((a, kw))

    async def spy_record(*a: Any, **kw: Any) -> None:
        pass

    monkeypatch.setattr(toolrelay, "settle", spy_settle)
    monkeypatch.setattr(toolrelay, "check_budgets", _refusing_gate)
    monkeypatch.setattr(traces, "record", spy_record)
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    offpeak.state.enabled = True
    offpeak.state.active = True
    offpeak.state.fraction = 0.5

    calls = {"n": 0}

    async def stream_once(msgs: list[dict], tools: list[dict] | None) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            yield (
                "tool_calls",
                [
                    {
                        "index": 0,
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "srv__tool", "arguments": "{}"},
                    }
                ],
            )
            yield ("usage", {"prompt_tokens": 8, "completion_tokens": 0})
        else:
            yield ("usage", {"prompt_tokens": 1, "completion_tokens": 0})

    async def execute(server: str, tool: str, arguments: dict) -> tuple[bool, Any]:
        return True, "ok"

    async def consume() -> None:
        async for _ in toolrelay.relay_stream(
            [{"role": "user", "content": "hi"}],
            {("srv", "tool")},
            [{"server": "srv", "tool": "tool", "description": ""}],
            stream_once,
            execute,
            "rid",
            MODEL,
            key_id=uuid.uuid4(),
            key_fraction=0.5,  # caller resolves the KEY's base (profile ?? env)
        ):
            pass

    asyncio.run(consume())
    statuses = [a[5] for a, _kw in settles]
    assert "budget_exceeded" in statuses, "the marker settle must have fired"
    marker = next(a for a, _kw in settles if a[5] == "budget_exceeded")
    assert marker[3] == 8  # spent so far in the relay
    marker_kw = next(kw for a, kw in settles if a[5] == "budget_exceeded")
    assert marker_kw.get("billing_fraction") == 0.5, "marker rows stamp the fraction too"


async def _refusing_gate(key_id: uuid.UUID, model: str, est_tokens: int = 0) -> budgets_mod.GateResult:
    return budgets_mod.GateResult(False, "month budget 5 exceeded (spent 8)")


def test_relay_settle_full_price_when_feature_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero behavior change when the feature is off: fraction() is 1.0."""
    settles: list[tuple[tuple, dict[str, Any]]] = []

    async def spy_settle(*a: Any, **kw: Any) -> None:
        settles.append((a, kw))

    async def spy_record(*a: Any, **kw: Any) -> None:
        pass

    monkeypatch.setattr(toolrelay, "settle", spy_settle)
    monkeypatch.setattr(traces, "record", spy_record)

    async def stream_once(msgs: list[dict], tools: list[dict] | None) -> Any:
        yield ("usage", {"prompt_tokens": 7, "completion_tokens": 3})

    async def consume() -> None:
        async for _ in toolrelay.relay_stream(
            [{"role": "user", "content": "hi"}],
            set(),
            [],
            stream_once,
            None,  # type: ignore[arg-type]
            "rid",
            MODEL,
            key_id=uuid.uuid4(),
            key_fraction=0.5,  # caller resolves the KEY's base (profile ?? env)
        ):
            pass

    assert offpeak.fraction() == 1.0
    asyncio.run(consume())
    assert settles and settles[0][1].get("billing_fraction") == 1.0


# ---------------------------------------------------------------------------
# X-PCAI-Billing response header
# ---------------------------------------------------------------------------


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 1000,
        "rate_limit_tpm": 1_000_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


def test_relay_headers_announce_offpeak_fraction() -> None:
    """Unit: the shared response-header builder adds the header ONLY when
    the effective fraction differs from full price (covers the JSON, plain
    stream and relay response-build sites, which all route through it)."""
    headers = routes_proxy._relay_headers({"X-Request-ID": "r1"}, "r1")
    assert "X-PCAI-Billing" not in headers, "full price must NOT announce a discount"

    offpeak.state.enabled = True
    offpeak.state.active = True
    offpeak.state.fraction = 0.5
    headers = routes_proxy._relay_headers({}, "r1", 0.5)
    assert headers["X-PCAI-Billing"] == "offpeak-0.5"


def test_proxy_json_response_carries_billing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration (JSON chat path): an active off-peak state surfaces on the
    proxied response; a disabled feature adds no header."""
    fake = WeightedLedgerDB()
    key = _key()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "c1",
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5},
            },
        )

    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    routes_proxy._in_flight.clear()
    r = Router.with_mock(handler)
    r.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1", model_type="chat")])
    monkeypatch.setattr(routes_proxy, "model_router", r)

    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)

    def post() -> Any:
        return tc.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer x"},
        )

    # feature off: no billing header
    resp = post()
    assert resp.status_code == 200, resp.text
    assert "x-pcai-billing" not in resp.headers

    # active low-demand state: header announces the discount
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    offpeak.state.enabled = True
    offpeak.state.active = True
    offpeak.state.fraction = 0.5
    resp = post()
    assert resp.status_code == 200
    assert resp.headers.get("x-pcai-billing") == "offpeak-0.5"

    # back to full price mid-life: header disappears again
    offpeak.state.active = False
    offpeak.state.fraction = 1.0
    resp = post()
    assert "x-pcai-billing" not in resp.headers


# ---------------------------------------------------------------------------
# /admin/offpeak snapshot route
# ---------------------------------------------------------------------------


def test_admin_offpeak_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    assert tc.get("/admin/offpeak").status_code == 401
    assert tc.get("/admin/offpeak", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_admin_offpeak_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_enter_pct", 20.0)
    monkeypatch.setattr(settings, "offpeak_exit_pct", 35.0)
    monkeypatch.setattr(settings, "offpeak_check_s", 45.0)
    monkeypatch.setattr(settings, "offpeak_promql", "avg(up)")
    monkeypatch.setattr(settings, "offpeak_mcp_server", "prom")
    monkeypatch.setattr(settings, "offpeak_tool", "prom_query")
    offpeak.state.enabled = True
    offpeak.state.active = True
    offpeak.state.fraction = 0.5
    offpeak.state.last_value_pct = 12.5
    offpeak.state.last_check = "2026-01-01T00:00:00+00:00"
    offpeak.state.state_since = "2026-01-01T00:00:00+00:00"
    offpeak.state.last_error = None

    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/offpeak", headers={"Authorization": "Bearer admin-secret-123"})
    assert r.status_code == 200
    body = r.json()
    assert body.pop("models") == {}
    assert body == {
        "enabled": True,
        "active": True,
        "fraction": 0.5,
        "enter_pct": 20.0,
        "exit_pct": 35.0,
        "check_s": 45.0,
        "promql": "avg(up)",
        "mcp_server": "prom",
        "tool": "prom_query",
        "last_value_pct": 12.5,
        "last_check": "2026-01-01T00:00:00+00:00",
        "last_error": None,
        "state_since": "2026-01-01T00:00:00+00:00",
        "history": [],
        "default_fraction": 0.5,
    }


def test_admin_offpeak_help_lists_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/help", headers={"Authorization": "Bearer admin-secret-123"})
    assert r.status_code == 200
    assert "GET    /admin/offpeak" in r.json()["endpoints"]


def test_admin_offpeak_surfaces_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-safe visibility: a failed evaluator shows idle + the error."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    offpeak.state.enabled = True
    offpeak.state.active = False
    offpeak.state.fraction = 1.0
    offpeak.state.last_error = "connection refused"
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    body = tc.get("/admin/offpeak", headers={"Authorization": "Bearer admin-secret-123"}).json()
    assert body["active"] is False and body["fraction"] == 1.0
    assert body["last_error"] == "connection refused"


# ---------------------------------------------------------------------------
# lifespan gating
# ---------------------------------------------------------------------------


def _patch_lifespan_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize every lifespan dependency except the offpeak wiring."""
    import pcai_llm.app as app_mod

    async def _noop() -> None:
        pass

    monkeypatch.setattr(app_mod.db, "connect", _noop)
    monkeypatch.setattr(app_mod.db, "close", _noop)
    monkeypatch.setattr(app_mod.model_router, "start", _noop)
    monkeypatch.setattr(app_mod.model_router, "close", _noop)
    monkeypatch.setattr(registry, "refresh", _noop)


def test_lifespan_disabled_starts_no_task(monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.app as app_mod

    monkeypatch.setattr(settings, "offpeak_enabled", False)
    _patch_lifespan_deps(monkeypatch)
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls)

    async def run() -> None:
        async with app_mod.lifespan(FastAPI()):
            assert offpeak._task is None, "disabled feature must not spawn the evaluator"

    asyncio.run(run())
    assert calls == [] and offpeak._task is None


def test_lifespan_enabled_starts_and_stops_task(monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.app as app_mod

    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_check_s", 3600.0)  # park after the first tick
    _patch_lifespan_deps(monkeypatch)
    _fake_registry(monkeypatch, [_prom_server()])
    calls: list[dict[str, Any]] = []
    _fake_opener(monkeypatch, calls, result="50.0")

    async def run() -> None:
        async with app_mod.lifespan(FastAPI()):
            assert offpeak._task is not None and not offpeak._task.done()
            assert offpeak.state.enabled is True
            # let the first tick run: 50.0 > exit_pct -> idle, full price
            for _ in range(50):
                if offpeak.state.last_check is not None:
                    break
                await asyncio.sleep(0.01)
            assert offpeak.state.last_check is not None, "the evaluator ticked during lifespan"
            assert offpeak.state.active is False and offpeak.fraction() == 1.0

    asyncio.run(run())
    assert offpeak._task is None, "shutdown must stop the evaluator"
    assert offpeak.state.enabled is False and offpeak.fraction() == 1.0


# ---------------------------------------------------------------------------
# per-model off-peak: GLM tracks GLM's GPUs, Qwen tracks Qwen's
# ---------------------------------------------------------------------------


def test_effective_for_routes_to_per_model_states() -> None:
    """A model with its own query bills by ITS hysteresis state; unknown
    models fall back to the global state. (The global GPU average diluted
    GLM's ~90% GPU load to ~22% cluster-wide — per-model queries fix the
    discount being applied while the serving GPUs are pegged.)"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        settings,
        "offpeak_model_promql",
        {"GLM-5.3-Flash": "q-glm", "Qwen3.8-27B": "q-qwen"},
    )
    monkeypatch.setattr(
        offpeak,
        "_model_evals",
        {
            "GLM-5.3-Flash": ("q-glm", offpeak._State(label="GLM-5.3-Flash", enabled=True, active=True, fraction=0.5)),
            "Qwen3.8-27B": ("q-qwen", offpeak._State(label="Qwen3.8-27B", enabled=True, active=False, fraction=1.0)),
        },
    )
    try:
        assert offpeak.effective_for("GLM-5.3-Flash", 0.5) == 0.5  # its OWN active state
        assert offpeak.effective_for("Qwen3.8-27B", 0.5) == 1.0  # its OWN idle state
        assert offpeak.effective_for("unknown-model", 0.5) == 1.0  # global fallback (idle)
    finally:
        monkeypatch.undo()


def test_evaluate_once_samples_global_and_per_model_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """evaluate_once samples the global query AND each per-model query; the
    states evolve independently."""
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_model_promql", {"GLM-5.3-Flash": "q-glm"})
    _fake_registry(monkeypatch, [{"name": "prometheus", "url": "http://prom:9090"}])

    async def fake_call(url, headers, tool, arguments, timeout_s):
        q = arguments["query"]
        if q == "q-glm":
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text="10")])  # low -> active
        if q == settings.offpeak_promql:
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text="90")])  # high -> idle
        raise AssertionError(f"unexpected query {q!r}")

    monkeypatch.setattr(offpeak, "_call_tool", fake_call)
    monkeypatch.setattr(offpeak, "_model_evals", {"GLM-5.3-Flash": ("q-glm", offpeak._State(label="GLM-5.3-Flash"))})
    asyncio.run(offpeak.evaluate_once())

    assert offpeak.effective_for("GLM-5.3-Flash", 0.5) == 0.5  # model-specific LOW demand
    assert offpeak.effective(0.5) == 1.0  # global HIGH demand -> full price
    snap = offpeak.models_snapshot()
    assert snap["GLM-5.3-Flash"]["active"] is True
    assert snap["GLM-5.3-Flash"]["last_value_pct"] == 10.0


def test_evaluate_once_model_failure_is_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """One model's query failing fails ONLY that model's state (full price +
    last_error); the global state and other models are untouched."""
    import httpx2 as _httpx2

    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_model_promql", {"GLM-5.3-Flash": "q-glm"})
    _fake_registry(monkeypatch, [{"name": "prometheus", "url": "http://prom:9090"}])

    async def fake_call(url, headers, tool, arguments, timeout_s):
        if arguments["query"] == "q-glm":
            raise _httpx2.ConnectError("prometheus unreachable")
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="10")])  # global low -> active

    monkeypatch.setattr(offpeak, "_call_tool", fake_call)
    monkeypatch.setattr(offpeak, "_model_evals", {"GLM-5.3-Flash": ("q-glm", offpeak._State(label="GLM-5.3-Flash"))})
    asyncio.run(offpeak.evaluate_once())

    assert offpeak.effective_for("GLM-5.3-Flash", 0.5) == 1.0  # fail-safe: full price
    assert "prometheus unreachable" in (offpeak.models_snapshot()["GLM-5.3-Flash"]["last_error"] or "")
    assert offpeak.effective(0.5) == 0.5  # global unaffected


# ---------------------------------------------------------------------------
# per-model ratio overrides + utilization history (the Off-peak panel)
# ---------------------------------------------------------------------------


class _FakeRatioDB:
    """offpeak.db seam: the offpeak_model_config table in memory."""

    def __init__(self, rows: list[tuple[str, float]] | None = None):
        self.rows = list(rows or [])
        self.execs: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *a: Any):
        if "FROM offpeak_model_config" in sql:
            return [{"model_name": m, "fraction": f} for m, f in self.rows]
        return []

    async def execute(self, sql: str, *a: Any):
        self.execs.append((sql, a))
        if "DELETE FROM offpeak_model_config" in sql:
            self.rows = [(m, f) for m, f in self.rows if m != a[0]]
        elif "INSERT INTO offpeak_model_config" in sql:
            self.rows = [(m, f) for m, f in self.rows if m != a[0]] + [(a[0], a[1])]
        return "OK"


def test_ratio_override_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """While a model's governing evaluator is ACTIVE: the panel override
    first, then the request's base (profile ?? env). Inactive -> 1.0 ALWAYS —
    the override is a discount knob, never a surcharge; and an unattached
    model with a stored override rides the global evaluator's state."""
    fake = _FakeRatioDB([("GLM-5.3-Flash", 0.7), ("Qwen3.8-27B", 0.3), ("unattached", 0.25)])
    monkeypatch.setattr(offpeak, "db", fake)
    asyncio.run(offpeak.load_overrides(force=True))
    monkeypatch.setattr(
        offpeak,
        "_model_evals",
        {
            "GLM-5.3-Flash": ("q-glm", offpeak._State(label="GLM", enabled=True, active=True, fraction=0.5)),
            "Qwen3.8-27B": ("q-qwen", offpeak._State(label="Qwen", enabled=True, active=False, fraction=1.0)),
        },
    )
    monkeypatch.setattr(offpeak, "state", offpeak._State(enabled=True, active=True, fraction=0.5))

    assert offpeak.effective_for("GLM-5.3-Flash", 0.5) == 0.7  # override beats base
    assert offpeak.effective_for("Qwen3.8-27B", 0.5) == 1.0  # its own evaluator idle -> full price
    assert offpeak.effective_for("unattached", 0.5) == 0.25  # global active + override
    assert offpeak.effective_for("no-override-model", 0.5) == 0.5  # global active, base


def test_ratio_set_and_clear_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """set_override persists (upsert), refreshes the cache, and None deletes
    the row entirely — no tombstone. Slash-containing names are plain dict
    keys here and :path params at the route."""
    fake = _FakeRatioDB()
    monkeypatch.setattr(offpeak, "db", fake)

    asyncio.run(offpeak.set_override("Qwen/Qwen3.8-27B-FP8", 0.4))
    assert offpeak._overrides == {"Qwen/Qwen3.8-27B-FP8": 0.4}
    asyncio.run(offpeak.set_override("Qwen/Qwen3.8-27B-FP8", 0.6))  # upsert, not duplicate
    assert [m for m, _ in fake.rows] == ["Qwen/Qwen3.8-27B-FP8"]
    assert fake.rows[0][1] == 0.6
    asyncio.run(offpeak.set_override("Qwen/Qwen3.8-27B-FP8", None))
    assert offpeak._overrides == {} and fake.rows == []
    assert any("DELETE FROM offpeak_model_config" in sql for sql, _ in fake.execs)


def test_ratio_refresh_failure_keeps_last_known(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB blip during the TTL refresh must NOT wipe commercial terms —
    the cache keeps the last known ratios (and the billing path reads that
    cache, so requests keep billing correctly through the blip)."""

    class BoomDB:
        async def fetch(self, sql: str, *a: Any):
            raise RuntimeError("db down")

        async def execute(self, sql: str, *a: Any):
            return "OK"

    monkeypatch.setattr(offpeak, "db", BoomDB())
    offpeak._overrides.update({"GLM-5.3-Flash": 0.7})
    asyncio.run(offpeak.load_overrides(force=True))
    assert offpeak._overrides == {"GLM-5.3-Flash": 0.7}


def test_history_capped_and_exposed_in_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful samples ring-buffer per state (240 ≈ 2 h at 30 s) and ride
    the snapshot for the panel's sparkline; the override field rides too."""
    st = offpeak._State(label="m")
    monkeypatch.setattr(offpeak, "_model_evals", {"m": ("q", st)})
    for i in range(offpeak._HISTORY_MAX + 25):
        offpeak._apply_state(st, float(i % 100))
    assert len(st.history) == offpeak._HISTORY_MAX
    snap = offpeak.models_snapshot()
    assert len(snap["m"]["history"]) == offpeak._HISTORY_MAX
    assert snap["m"]["fraction_override"] is None


def test_admin_offpeak_ratio_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /admin/offpeak/models/{model}/fraction: set, clear (null),
    validation bounds (0, >1) and admin-gating — with a SLASH-containing
    model name exercising the {model_name:path} converter (the DELETE
    /models lesson: a plain path param 404s slash names before routing)."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = _FakeRatioDB()
    monkeypatch.setattr(offpeak, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    h = {"Authorization": "Bearer admin-secret-123"}

    r = tc.put("/admin/offpeak/models/Qwen/Qwen3.8-27B-FP8/fraction", json={"fraction": 0.4}, headers=h)
    assert r.status_code == 200 and r.json() == {"model": "Qwen/Qwen3.8-27B-FP8", "fraction": 0.4}
    assert offpeak._overrides.get("Qwen/Qwen3.8-27B-FP8") == 0.4

    r = tc.put("/admin/offpeak/models/Qwen/Qwen3.8-27B-FP8/fraction", json={"fraction": None}, headers=h)
    assert r.status_code == 200 and r.json()["fraction"] is None
    assert "Qwen/Qwen3.8-27B-FP8" not in offpeak._overrides

    assert tc.put("/admin/offpeak/models/m/fraction", json={"fraction": 0}, headers=h).status_code == 422
    assert tc.put("/admin/offpeak/models/m/fraction", json={"fraction": 1.5}, headers=h).status_code == 422
    assert tc.put("/admin/offpeak/models/m/fraction", json={"fraction": 0.5}).status_code == 401


# ---------------------------------------------------------------------------
# cache-discounted token budgets (budget_cached_exempt_fraction)
# ---------------------------------------------------------------------------


def test_billable_tokens_math(monkeypatch: pytest.MonkeyPatch) -> None:
    """_billable_tokens: total - cached*frac, clamped. 0.9 = cached fully
    exempt; 0.1 = the operator's one-tenth option; 0.0 = legacy full count."""
    from pcai_llm.core.budgets import _billable_tokens

    monkeypatch.setattr(budgets_mod.settings, "budget_cached_exempt_fraction", 0.9)
    # uncached remainder + cached at one-tenth: (1000-900) + 900*0.1 = 190
    assert _billable_tokens(1000, 900) == 190
    assert _billable_tokens(1000, 0) == 1000  # cold call pays full
    assert _billable_tokens(1000, 1000) == 100  # fully cached still pays 1/10
    monkeypatch.setattr(budgets_mod.settings, "budget_cached_exempt_fraction", 0.1)
    assert _billable_tokens(1000, 900) == 910  # one-tenth weight
    monkeypatch.setattr(budgets_mod.settings, "budget_cached_exempt_fraction", 0.0)
    assert _billable_tokens(1000, 900) == 1000  # legacy
    monkeypatch.setattr(budgets_mod.settings, "budget_cached_exempt_fraction", 0.9)
    # poisoned row (cached > total) clamps the exemption to total:
    # billable = (500-500) + 500*0.1 = 50 — never negative
    assert _billable_tokens(500, 900) == 50


def test_budget_expr_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """_budget_token_expr: legacy shape at fraction 0, GREATEST-guarded
    discount otherwise; the fraction factor never disappears."""
    from pcai_llm.core.budgets import _budget_token_expr

    monkeypatch.setattr(budgets_mod.settings, "budget_cached_exempt_fraction", 0.0)
    assert _budget_token_expr("l") == "SUM(l.total_tokens * l.billing_fraction)"
    monkeypatch.setattr(budgets_mod.settings, "budget_cached_exempt_fraction", 0.9)
    expr = _budget_token_expr("l")
    assert "GREATEST(l.total_tokens - LEAST(l.cached_tokens, l.total_tokens) * 0.9, 0)" in expr
    assert expr.endswith("* l.billing_fraction)")


def test_budget_gate_discounts_cached_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the fake ledger: a mostly-cached row consumes only
    its billable remainder against the token budget."""
    fake = WeightedLedgerDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "budget_cached_exempt_fraction", 0.9)
    kid = uuid.uuid4()
    # one 10_000-token call, 9_500 cache-served
    asyncio.run(budgets_mod.settle(kid, MODEL, "chat", 10_000, 0, "ok", 5, "rid-1", cached_tokens=9_500))
    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1000}]
    # billable = (10_000-9_500) + 9_500*0.1 = 1_450 > 1_000 -> refused
    res = asyncio.run(budgets_mod.check_budgets(kid, MODEL, 0))
    assert not res.ok
    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 2000}]
    res = asyncio.run(budgets_mod.check_budgets(kid, MODEL, 0))
    assert res.ok
    # and the legacy fraction restores the full count (10_000 > 2_000)
    monkeypatch.setattr(budgets_mod.settings, "budget_cached_exempt_fraction", 0.0)
    res = asyncio.run(budgets_mod.check_budgets(kid, MODEL, 0))
    assert not res.ok and "10000" in res.reason
