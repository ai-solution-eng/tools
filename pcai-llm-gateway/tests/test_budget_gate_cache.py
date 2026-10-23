"""REMEDIATION P4 — budget-gate EXISTS short-circuit (hot-path cost).

check_budgets used to pay for BOTH windowed ledger-SUM aggregates on every
admission and every relay iteration even when the key carries no budget rows
at all. It now probes the tiny catalog tables first (two EXISTS lookups on
the indexed key_id columns — the ledger is never touched) and only then runs
the aggregate path.

Semantics pinned here:
* no matching row in EITHER family -> admissible, with ZERO ledger-SUM
  queries fired (the whole point of the fix);
* any matching row -> the FULL aggregate path, unchanged (SUMs fire; the
  refusal/warn math stays the tests/test_offpeak.py + test_audit_fixes.py
  contract — those suites must keep passing untouched);
* a model-specific row for a DIFFERENT model is not a matching row — no
  SUMs, still admissible;
* the probe is fail-safe: a seam that cannot answer it (legacy fakes return
  None/0) or a DB blip falls back to the SUMs, never skips the gate.

The DB seam is a counting FakeDB in the repo's monkeypatch-the-module-db
pattern (tests/test_budgets.py / test_offpeak.py WeightedLedgerDB style):
queries are classified by the same SQL substrings the production fakes
match, so "query shape" assertions hold against the real call sites.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from pcai_llm.core import budgets as budgets_mod


class BudgetGateDB:
    """Counting fake: records every query by SHAPE, answers the EXISTS probe
    from its row lists, and would blow up (explicit marker) on any ledger-SUM
    it was not supposed to see."""

    def __init__(self) -> None:
        self.token_budgets: list[dict[str, Any]] = []
        self.usd_budgets: list[dict[str, Any]] = []
        self.ledger_rows: list[dict[str, Any]] = []
        self.probe_count = 0
        self.sum_count = 0
        self.queries: list[str] = []  # shapes in call order
        self.probe_unsupported = False  # simulate a legacy fake seam

    # -- query shape classification (the SAME markers the repo's fakes use) --
    @staticmethod
    def _is_probe(sql: str) -> bool:
        return "SELECT EXISTS" in sql and ("FROM budgets" in sql or "FROM cost_budgets" in sql)

    @staticmethod
    def _is_sum(sql: str) -> bool:
        return "usage_ledger" in sql and ("FROM budgets" in sql or "FROM cost_budgets" in sql)

    async def fetchval(self, sql: str, *a: Any):
        self.queries.append("probe" if self._is_probe(sql) else "other")
        if not self._is_probe(sql):
            return None  # non-probe fetchval: not our concern (repo fake norm)
        self.probe_count += 1
        if self.probe_unsupported:
            return None  # what WeightedLedgerDB-style fakes answer
        table = "budgets" if "FROM budgets" in sql else "cost_budgets"
        rows = self.token_budgets if table == "budgets" else self.usd_budgets
        # Re-implement the probe's filter on the fake rows: key matches
        # trivially (single-key fake); model filter is '*' OR the pinned model.
        _key_id, *rest = a
        model = rest[0] if rest and rest[0] is not None else None
        matches = [r for r in rows if model is None or r["model"] == "*" or r["model"] == model]
        return bool(matches)

    async def fetch(self, sql: str, *a: Any):
        self.queries.append("sum" if self._is_sum(sql) else "other-fetch")
        if self._is_sum(sql):
            self.sum_count += 1
            if "FROM budgets" in sql:
                return [{**b, "spent": self._weighted("total_tokens")} for b in self.token_budgets]
            return [{**b, "spent": self._weighted("cost_usd")} for b in self.usd_budgets]
        return []

    async def fetchrow(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        self.queries.append("execute")
        return "OK"

    def _weighted(self, col: str) -> float:
        return sum(r[col] * r["billing_fraction"] for r in self.ledger_rows if r["status"] in ("ok", "error"))


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> BudgetGateDB:
    fake = BudgetGateDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    return fake


# ---------------------------------------------------------------------------
# (1) key with NO budget rows: 2 probes, ZERO SUMs, still admissible
# ---------------------------------------------------------------------------


def test_no_budget_rows_fires_only_probes_and_stays_admissible(fake_db: BudgetGateDB) -> None:
    kid = uuid.uuid4()
    res = asyncio.run(budgets_mod.check_budgets(kid, "any-model", 500))

    assert res.ok, "a key with no budget rows must remain ADMISSIBLE"
    assert res.warn == 0
    assert res.remaining_requests is None and res.remaining_tokens is None
    assert fake_db.probe_count == 2, "one EXISTS probe per budget family"
    assert fake_db.sum_count == 0, "ZERO ledger-SUM queries when no budget rows exist"
    assert fake_db.queries == ["probe", "probe"], (
        f"probe-then-clear path must fire ONLY the probes, saw {fake_db.queries}"
    )


def test_est_tokens_does_not_trigger_the_sum_path(fake_db: BudgetGateDB) -> None:
    """A nonzero est_tokens must not conjure a budget out of nowhere — the
    all-clear is an all-clear regardless of the estimated request size."""
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m", 100_000))
    assert res.ok and fake_db.sum_count == 0 and fake_db.probe_count == 2


# ---------------------------------------------------------------------------
# (2) key WITH a matching row: full path, SUMs fire, refusal math unchanged
# ---------------------------------------------------------------------------


def test_matching_row_runs_full_path(fake_db: BudgetGateDB) -> None:
    fake_db.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1000}]
    fake_db.ledger_rows = [
        {"total_tokens": 400, "cost_usd": 0.0, "status": "ok", "billing_fraction": 1.0},
        {"total_tokens": 400, "cost_usd": 0.0, "status": "ok", "billing_fraction": 0.5},
    ]
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))

    assert res.ok, "weighted spend (600) fits the 1000 limit"
    assert res.warn == 0  # 60% — under the 80% band
    assert fake_db.probe_count == 1, "the probe short-circuits on the FIRST matching family"
    assert fake_db.sum_count == 2, "a matching row must run BOTH aggregate SUMs"
    assert fake_db.queries == ["probe", "sum", "sum"]


def test_matching_row_under_limit_warn_thresholds_intact(fake_db: BudgetGateDB) -> None:
    """With a matching row the gate runs the aggregates; the warn band math
    (80/95) rides the unchanged full path."""
    fake_db.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1000}]
    fake_db.ledger_rows = [
        {"total_tokens": 950, "cost_usd": 0.0, "status": "ok", "billing_fraction": 1.0},
    ]
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert res.ok and res.warn == 95, "95% of the limit must surface warn=95 through the probe path"

    fake_db.ledger_rows = [
        {"total_tokens": 820, "cost_usd": 0.0, "status": "ok", "billing_fraction": 1.0},
    ]
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert res.ok and res.warn == 80


def test_matching_row_refusal_identical_shape(fake_db: BudgetGateDB) -> None:
    """The refusal reason is produced by the UNCHANGED aggregate path."""
    fake_db.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 500}]
    fake_db.ledger_rows = [
        {"total_tokens": 800, "cost_usd": 0.0, "status": "ok", "billing_fraction": 1.0},
    ]
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert not res.ok
    assert res.reason == "month budget 500 exceeded (spent 800)", (
        "refusal string must stay byte-identical to the pre-short-circuit path"
    )
    # Sequential gate semantics preserved: the token-family refusal returns
    # BEFORE the cost-family aggregate runs (probe saw tokens=True and
    # stopped; the SUM loop refused inside the token family).
    assert fake_db.sum_count == 1 and fake_db.probe_count == 1


def test_usd_budget_rows_also_trigger_full_path(fake_db: BudgetGateDB) -> None:
    """A token-row-free key with a cost_budget row still gets the full gate —
    the probe covers BOTH families."""
    fake_db.usd_budgets = [{"model": "*", "period": "month", "limit_usd": 10.0}]
    fake_db.ledger_rows = [
        {"total_tokens": 0, "cost_usd": 10.01, "status": "ok", "billing_fraction": 1.0},
    ]
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert not res.ok and "cost budget" in res.reason
    assert fake_db.probe_count == 2 and fake_db.sum_count == 2


# ---------------------------------------------------------------------------
# (3) model-specific row for a DIFFERENT model: no SUMs, admissible
# ---------------------------------------------------------------------------


def test_model_row_for_other_model_never_reaches_sums(fake_db: BudgetGateDB) -> None:
    fake_db.token_budgets = [{"model": "other-model", "period": "day", "limit_tokens": 100}]
    fake_db.usd_budgets = [{"model": "other-model", "period": "day", "limit_usd": 5.0}]
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))

    assert res.ok, "a budget scoped to another model must not gate this key's request"
    assert fake_db.sum_count == 0, "the model filter applies at the PROBE — no SUM may run"
    assert fake_db.probe_count == 2


# ---------------------------------------------------------------------------
# fail-safe: unsupported seam / DB blip -> full path (never gate-open)
# ---------------------------------------------------------------------------


def test_unsupported_probe_seam_falls_back_to_full_path(fake_db: BudgetGateDB) -> None:
    """Legacy fakes (WeightedLedgerDB et al.) answer fetchval with None.
    That MUST behave exactly like the pre-fix gate: SUMs fire, rows decide."""
    fake_db.probe_unsupported = True
    fake_db.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 500}]
    fake_db.ledger_rows = [
        {"total_tokens": 800, "cost_usd": 0.0, "status": "ok", "billing_fraction": 1.0},
    ]
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert not res.ok and "500" in res.reason, "None-probe must fall back to the SUMs (fail-safe)"
    assert fake_db.sum_count == 1, "fallback path is the UNCHANGED gate (token refusal first)"


def test_unsupported_probe_seam_admissible_key_still_admissible(fake_db: BudgetGateDB) -> None:
    fake_db.probe_unsupported = True
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert res.ok and fake_db.sum_count == 2, "fallback runs the (empty) aggregates: same all-clear"


def test_probe_db_failure_falls_back_to_full_path(fake_db: BudgetGateDB) -> None:
    """A DB blip on the probe itself must never skip the gate — the SUMs run
    and their own verdict decides."""

    class FlakyProbeDB(BudgetGateDB):
        async def fetchval(self, sql: str, *a: Any):
            if self._is_probe(sql):
                self.probe_count += 1
                raise RuntimeError("connection reset")
            return await super().fetchval(sql, *a)

    flaky = FlakyProbeDB()
    flaky.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 500}]
    flaky.ledger_rows = [
        {"total_tokens": 800, "cost_usd": 0.0, "status": "ok", "billing_fraction": 1.0},
    ]
    import pcai_llm.core.budgets as bm

    old_db = bm.db
    bm.db = flaky
    try:
        res = asyncio.run(bm.check_budgets(uuid.uuid4(), "m1", 0))
    finally:
        bm.db = old_db
    assert not res.ok and "500" in res.reason, "probe failure must degrade to the full gate"
    assert flaky.sum_count == 1, "fallback path is the UNCHANGED gate (token refusal first)"


# ---------------------------------------------------------------------------
# probe internals: model-star semantics + unpinned scan
# ---------------------------------------------------------------------------


def test_probe_filter_star_matches_any_model(fake_db: BudgetGateDB) -> None:
    """/ budget rows (any model) are matching rows for every pinned model."""
    fake_db.token_budgets = [{"model": "*", "period": "week", "limit_tokens": 10}]
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "whatever", 0))
    assert fake_db.sum_count == 2 and fake_db.probe_count == 1, (
        "probe stops at the first matching family; both SUMs run"
    )
    assert res.ok  # weighted spend 0 < 10


def test_budget_status_unaffected_by_probe(fake_db: BudgetGateDB) -> None:
    """budget_status keeps its two-aggregate shape (admin view shows rows even
    at zero spend) — the short-circuit belongs to the GATE only."""
    fake_db.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1000}]
    out = asyncio.run(budgets_mod.budget_status(uuid.uuid4()))
    assert [b["kind"] for b in out] == ["tokens"]
    assert fake_db.sum_count == 2 and fake_db.probe_count == 0, "budget_status must not probe"
