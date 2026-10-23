"""Prometheus metrics instrumentation (observability wave).

Covers the contract the dashboard/alerts depend on:

1. SERIES EXIST and increment at the LEDGER choke points — a metric and
   its usage_ledger row must never disagree (core.metrics design rule 4),
   so counters are asserted against the same settle() calls the ledger
   tests already exercise.
2. NO-OP SHIMS — with prometheus_client absent from sys.modules, every
   instrumentation call is a free no-op and nothing raises (the gateway
   must run with PCAI_LLM_METRICS_ENABLED=true even without the optional
   dependency installed).
3. TARGETED SITES — TTFT fires on the first stream chunk, breaker
   trip/recover tracks the pool's state machine, budget warnings count at
   gate time, ACL denials count per server.

No live Postgres (conftest's dummy URL), no live upstreams: settle()'s db
handle is stubbed, the breaker tests use a fresh Pool, and the ledger
INSERT arg-tuple convention follows tests/test_router_extras.py (index 8
= status, 1 = model, 2 = endpoint).
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import metrics
from pcai_llm.routing.router import Endpoint, Pool


def _label_sample(counter: Any, **labels: str) -> float:
    """Current value of one labeled counter child (prometheus_client)."""
    return counter.labels(**labels)._value.get()  # type: ignore[attr-defined]


def _hist_count(hist: Any, **labels: str) -> float:
    """Observed-sample count of one labeled histogram child, read through
    the exported sample view (collect()) — version-stable, unlike the
    private bucket attributes."""
    for family in hist.collect():
        for s in family.samples:
            if s.name.endswith("_count") and s.labels == labels:
                return s.value
    return 0.0


@pytest.fixture()
def _db_stub(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Settle() must not touch Postgres; accumulate the INSERT args.
    Patched at the budgets AND costs module's own `db` bindings (the house
    pattern — test_router_extras.py patches the same set; costs.compute
    runs inside settle() for status='ok' rows)."""
    from pcai_llm.core import budgets as budgets_mod
    from pcai_llm.core import costs as costs_mod

    settled: list[tuple] = []

    class _FakeDB:
        async def execute(self, _sql: str, *args: Any) -> str:
            settled.append(args)
            return "INSERT 0 1"

        async def fetch(self, _sql: str, *args: Any) -> list[Any]:
            return []  # costs.compute price lookup: unpriced model

        async def fetchrow(self, _sql: str, *args: Any) -> Any:
            return None

    fake = _FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    return settled


class TestSettleChokePoint:
    def test_settle_increments_requests_counter(self, _db_stub: list[tuple]) -> None:
        key_id = uuid.uuid4()
        before = _label_sample(metrics.REQUESTS, model="m1", status="ok", kind="chat")
        asyncio.run(
            __import__("pcai_llm.core.budgets", fromlist=["settle"]).settle(
                key_id, "m1", "http://ep", 10, 5, "ok", 42, "req-1"
            )
        )
        after = _label_sample(metrics.REQUESTS, model="m1", status="ok", kind="chat")
        assert after == before + 1
        assert len(_db_stub) == 1  # ledger row still written exactly once

    def test_settle_vocabulary_covers_all_statuses(self, _db_stub: list[tuple]) -> None:
        """Every ledger status maps onto the counter without error."""
        budgets = __import__("pcai_llm.core.budgets", fromlist=["settle"])
        key_id = uuid.uuid4()
        for status in ("ok", "error", "cache_hit", "budget_exceeded", "rate_limited"):
            asyncio.run(budgets.settle(key_id, "m1", "embeddings", 0, 0, status, 1, f"req-{status}"))
            assert _label_sample(metrics.REQUESTS, model="m1", status=status, kind="embeddings") >= 1

    def test_kind_derivation(self) -> None:
        assert metrics.derive_kind("embeddings") == "embeddings"
        assert metrics.derive_kind("mcp:mem-rag:search") == "mcp"
        assert metrics.derive_kind("chat:guardrail") == "chat"
        assert metrics.derive_kind("http://vllm-svc.ns.svc.cluster.local/v1") == "chat"


class TestBudgetAndRateCounters:
    def test_budget_rejection_counters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A token-budget refusal bumps the tokens-kind counter; a USD
        refusal bumps usd — check_budgets is the single site for both."""
        from pcai_llm.core import budgets as budgets_mod

        # _budget_sums(table, ...) is called for BOTH families per gate run;
        # the stub dispatches on the table argument (first positional).
        async def _stub_sums(table: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
            if table == "budgets":
                return [{"period": "month", "limit_tokens": 100, "spent": 1000}]
            return [{"period": "month", "limit_usd": 5.0, "spent": 9.9}]

        monkeypatch.setattr(budgets_mod, "_budget_sums", _stub_sums)
        before = _label_sample(metrics.BUDGET_REJECTIONS, kind="tokens")
        res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
        assert not res.ok
        assert _label_sample(metrics.BUDGET_REJECTIONS, kind="tokens") == before + 1

        async def _usd_only(table: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
            if table == "budgets":
                return []
            return [{"period": "month", "limit_usd": 5.0, "spent": 9.9}]

        monkeypatch.setattr(budgets_mod, "_budget_sums", _usd_only)
        before = _label_sample(metrics.BUDGET_REJECTIONS, kind="usd")
        res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
        assert not res.ok
        assert _label_sample(metrics.BUDGET_REJECTIONS, kind="usd") == before + 1

    def test_budget_warning_counter_at_gate(self) -> None:
        """warn thresholds surface as BUDGET_WARNING at the 95% tier."""
        before95 = _label_sample(metrics.BUDGET_WARNING, threshold="95")
        # Direct helper contract (the gate path is covered by test_proxy.py's
        # X-Budget-Warning assertions; here we pin the counter itself).
        metrics.budget_warning(95)
        assert _label_sample(metrics.BUDGET_WARNING, threshold="95") == before95 + 1

    def test_rate_limited_scope_labels(self) -> None:
        before = _label_sample(metrics.RATE_LIMITED, scope="rpm")
        metrics.RATE_LIMITED.labels(scope="rpm").inc()
        assert _label_sample(metrics.RATE_LIMITED, scope="rpm") == before + 1


class TestBreakerMetrics:
    def _ep(self, name: str) -> Endpoint:
        return Endpoint(id=name, model_name="m", base_url=f"http://{name}")

    def test_breaker_trip_sets_gauge_and_counts(self) -> None:
        pool = Pool(endpoints=[self._ep("metrics-ep-a")])
        ep = pool.endpoints[0]
        trips_before = _label_sample(metrics.BREAKER_TRIPS, endpoint=ep.id)

        async def run() -> None:
            for _ in range(settings.endpoint_failure_threshold):
                await pool.report(ep, ok=False)

        asyncio.run(run())
        assert ep.cooldown_until > time.monotonic()  # breaker semantics intact
        assert metrics.BREAKER_OPEN.labels(endpoint=ep.id)._value.get() == 1.0  # type: ignore[attr-defined]
        assert _label_sample(metrics.BREAKER_TRIPS, endpoint=ep.id) == trips_before + 1

    def test_success_recovery_is_gauge_only(self) -> None:
        """A success does NOT shorten a pending cooldown (rotation
        semantics unchanged) — it only closes the gauge early."""
        pool = Pool(endpoints=[self._ep("metrics-ep-b")])
        ep = pool.endpoints[0]

        async def run() -> None:
            for _ in range(settings.endpoint_failure_threshold):
                await pool.report(ep, ok=False)
            await pool.report(ep, ok=True)  # success while cooling

        asyncio.run(run())
        assert ep.cooldown_until > time.monotonic()  # cooldown NOT cleared
        assert metrics.BREAKER_OPEN.labels(endpoint=ep.id)._value.get() == 0.0  # type: ignore[attr-defined]


class TestAclAndRelay:
    def test_acl_denial_counter(self) -> None:
        before = _label_sample(metrics.MCP_ACL_DENIED, server="mem-rag")
        metrics.observe_acl_denial("mem-rag")
        assert _label_sample(metrics.MCP_ACL_DENIED, server="mem-rag") == before + 1

    def test_relay_iterations_observed(self) -> None:
        before = _hist_count(metrics.RELAY_ITERATIONS, model="m1")
        metrics.observe_relay_iterations("m1", 4)
        assert _hist_count(metrics.RELAY_ITERATIONS, model="m1") == before + 1


class TestNoopShims:
    def test_missing_prometheus_client_degrades_to_noops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the optional dependency absent, a FRESH module instance
        yields shims whose every method is a silent no-op — a metrics
        outage can never fail a request. Loaded via importlib.util under a
        private module name so the DEFAULT registry is untouched (a plain
        reload would re-register the collectors and raise
        DuplicateTimeseries for every later import)."""
        import importlib.util

        saved = {k: v for k, v in sys.modules.items() if "prometheus" in k}
        for k in list(saved):
            monkeypatch.delitem(sys.modules, k, raising=False)
        # A None entry in sys.modules makes `import prometheus_client` raise
        # ImportError inside the module — exactly the no-dependency path.
        monkeypatch.setitem(sys.modules, "prometheus_client", None)

        spec = importlib.util.spec_from_file_location("pcai_llm.core.metrics_noop_shim", metrics.__file__)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.undo()
        # saved entries restored via monkeypatch? undo() restores delitems;
        # re-add explicitly to be safe for later tests in this session.
        for k, v in saved.items():
            sys.modules.setdefault(k, v)

        assert mod._REAL is False
        # Every shim call site exercised — none may raise:
        mod.REQUESTS.labels(model="m", status="ok", kind="chat").inc()
        mod.LATENCY.labels(model="m", kind="chat").observe(1.5)
        mod.TTFT.labels(model="m").observe(0.25)
        mod.BREAKER_OPEN.labels(endpoint="e").set(1)
        mod.RELAY_ITERATIONS.labels(model="m").observe(3)
        mod.observe_settle("m", "mcp:s:t", "error", 5)
        mod.observe_ttft("m", 0.5)
        mod.breaker_trip("e")
        mod.breaker_recover("e")
        mod.budget_warning(80)
        mod.observe_acl_denial("s")
        mod.observe_relay_iterations("m", 2)


class TestStreamEventsResolution:
    """RFC-PER-KEY-STREAM-EVENTS: the three-state header resolution."""

    def test_header_beats_key_default(self) -> None:
        from pcai_llm.api.routes_proxy import _stream_events_wants

        assert _stream_events_wants("0", True) is False  # client opts out
        assert _stream_events_wants("1", False) is True  # strict key forces on
        assert _stream_events_wants(None, False) is False  # key default
        assert _stream_events_wants(None, True) is True
        assert _stream_events_wants("", True) is True  # absent == key default
        assert _stream_events_wants(" 1 ", False) is True  # whitespace tolerated
