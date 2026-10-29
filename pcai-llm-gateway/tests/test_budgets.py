"""Budget-window math and rate-limit logic (pure parts).

The window boundaries are what make day/week/month budgets meaningful —
test them without a DB by importing the window function directly.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core.budgets import _window_start


def test_day_window_midnight_utc() -> None:
    now = datetime(2026, 9, 15, 14, 30, 59, tzinfo=UTC)
    ws = _window_start(now, "day")
    assert (ws.hour, ws.minute, ws.second) == (0, 0, 0)
    assert ws.day == 15


def test_week_window_is_monday() -> None:
    # 2026-09-15 is a Tuesday → window starts Monday 2026-09-14
    now = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    ws = _window_start(now, "week")
    assert ws.weekday() == 0  # Monday
    assert ws.day == 14


def test_month_window_is_first() -> None:
    now = datetime(2026, 9, 15, 9, 45, tzinfo=UTC)
    ws = _window_start(now, "month")
    assert ws.day == 1 and ws.hour == 0


def test_all_windows_are_utc_and_naive_free() -> None:
    now = datetime.now(UTC)
    for period in ("day", "week", "month"):
        ws = _window_start(now, period)
        assert ws.tzinfo is UTC
# ---------------------------------------------------------------------------
# Audit 2026-10-29 — zone 4 (C1 / F1 / C3 / C6): budget-gate arithmetic,
# non-finite limits, prediction-charge release, non-positive rate limits.
#
# Each test below FAILS on the pre-fix code; the pre-fix predicate/behaviour is
# quoted in the test and (where it is cheap) emulated inline so the failure is
# provable without reverting the fix.
# ---------------------------------------------------------------------------


class GateFakeDB:
    """Minimal db seam for check_budgets: the EXISTS probe, the two windowed
    ledger SUMs and the lineage PK lookup.

    Query classification by the SAME SQL substrings the repo's other budget
    fakes match (tests/test_budget_gate_cache.py), so the shape assertions
    hold against the real call sites. `spent` is served verbatim from the
    row list — these tests pin the GATE's arithmetic, not the SQL.
    """

    def __init__(
        self,
        token_rows: list[dict[str, Any]] | None = None,
        usd_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.token_rows = token_rows or []
        self.usd_rows = usd_rows or []

    async def fetchval(self, sql: str, *a: Any) -> Any:
        if "SELECT EXISTS" in sql:
            rows = self.token_rows if "FROM budgets" in sql else self.usd_rows
            return bool(rows)
        return None

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        return None  # no rotation lineage — the normal key

    async def fetch(self, sql: str, *a: Any) -> list[dict[str, Any]]:
        if "FROM budgets" in sql:
            return list(self.token_rows)
        if "FROM cost_budgets" in sql:
            return list(self.usd_rows)
        return []


def _token_row(limit: int, spent: int, period: str = "day") -> dict[str, Any]:
    return {"model": "*", "period": period, "limit_tokens": limit, "spent": spent}


def _usd_row(limit_usd: float, spent_usd: float, period: str = "day") -> dict[str, Any]:
    return {"model": "*", "period": period, "limit_usd": limit_usd, "spent": spent_usd}


def test_c1_token_gate_refuses_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """C1: spent == limit is EXHAUSTED for the token gate.

    PRE-FIX (budgets.py:335): `if spent + est_billable > limit` where
    est_billable = est_tokens * (1 - budget_cached_exempt_fraction) = est/10 at
    the deployed 0.9. At spent == limit == 100000 that admitted est 0/5/9/10
    and refused only from 1024 — and the two zero-estimate paths (the relay's
    per-iteration gate, /mcp) were therefore admitted FOREVER. The USD gate
    (:354) and model_capacity (:947) both use `spent >= limit`.
    """
    fake = GateFakeDB(token_rows=[_token_row(100_000, 100_000)])
    monkeypatch.setattr(budgets_mod, "db", fake)

    # the pre-fix predicate, emulated from the quoted code
    frac = min(1.0, max(0.0, float(budgets_mod.settings.budget_cached_exempt_fraction)))
    for est in (0, 5, 9):
        assert 100_000 + int(est * (1.0 - frac)) <= 100_000, "pre-fix code ADMITTED this"

    for est in (0, 5, 9, 10, 1024):
        res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m", est))
        assert res.ok is False, f"an exhausted budget must refuse (est={est})"
        assert "budget" in res.reason and "exceeded" in res.reason


def test_c1_token_gate_still_admits_one_token_of_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hard stop is at equality only: spent == limit - 1 with no estimate
    still admits, so the fix cannot strand a key one token below its cap."""
    fake = GateFakeDB(token_rows=[_token_row(100_000, 99_999)])
    monkeypatch.setattr(budgets_mod, "db", fake)
    assert asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m", 0)).ok is True


def test_c1_projected_estimate_still_refuses_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """The projected half of the predicate is kept: a big estimate below the
    cap is still refused when the (discounted) projection overshoots."""
    fake = GateFakeDB(token_rows=[_token_row(100_000, 99_990)])
    monkeypatch.setattr(budgets_mod, "db", fake)
    frac = min(1.0, max(0.0, float(budgets_mod.settings.budget_cached_exempt_fraction)))
    est = 10_000  # 99_990 + int(10_000*(1-frac)) > 100_000
    assert 99_990 + int(est * (1.0 - frac)) > 100_000
    assert asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m", est)).ok is False


def test_c1_usd_and_token_gates_agree_at_equality(monkeypatch: pytest.MonkeyPatch) -> None:
    """All three surfaces agree at spent == limit: the token gate, the USD
    gate and model_capacity all call the budget exhausted."""
    fake = GateFakeDB(token_rows=[_token_row(1_000, 1_000)], usd_rows=[_usd_row(10.0, 10.0)])
    monkeypatch.setattr(budgets_mod, "db", fake)
    token_only = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m", 0))
    assert token_only.ok is False and "1_000" not in token_only.reason

    monkeypatch.setattr(budgets_mod, "db", GateFakeDB(usd_rows=[_usd_row(10.0, 10.0)]))
    usd_only = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m", 0))
    assert usd_only.ok is False and "cost budget" in usd_only.reason


def test_f1_split_preset_budgets_rejects_non_finite_limit_usd() -> None:
    """F1: NaN/Infinity must be a ValueError at the preset validator.

    PRE-FIX (budgets.py:117): `raw_usd <= 0` is False for NaN, so NaN passed,
    was persisted as a NUMERIC 'NaN', and then `spent >= nan` was always False
    (the USD gate could never refuse the row) while `int(spent*100/nan)`
    raised ValueError -> 500 on EVERY admission for that key. json.loads
    parses bare NaN/Infinity, so an admin payload reaches this.
    """
    import math

    from pcai_llm.core.budgets import MAX_LIMIT_USD, split_preset_budgets

    nan, inf = float("nan"), float("inf")
    assert not (nan <= 0), "the pre-fix positivity check let NaN through"
    for bad in (nan, inf):
        with pytest.raises(ValueError, match="finite"):
            split_preset_budgets([{"period": "month", "limit_usd": bad}])
    # -inf never reaches the finiteness check: it IS <= 0, so the positivity
    # gate answers (a different message, still a ValueError, still refused)
    with pytest.raises(ValueError, match="positive"):
        split_preset_budgets([{"period": "month", "limit_usd": float("-inf")}])
    # the column bound still answers for a huge finite value, and a huge INT
    # (math.isfinite(10**400) raises OverflowError) is the bound's business
    with pytest.raises(ValueError, match="column maximum"):
        split_preset_budgets([{"period": "month", "limit_usd": MAX_LIMIT_USD * 2}])
    with pytest.raises(ValueError, match="column maximum"):
        split_preset_budgets([{"period": "month", "limit_usd": 10**400}])
    # a finite preset still passes, unchanged
    _, usd = split_preset_budgets([{"period": "month", "limit_usd": 2.5}])
    assert usd == [("*", "month", 2.5)]
    assert math.isfinite(2.5)


def test_f1_usd_gate_refuses_a_persisted_nan_limit_instead_of_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1 defence in depth: a NaN limit already in cost_budgets (pre-fix row,
    or a hand-written INSERT) must REFUSE, not admit and not 500.

    PRE-FIX: `spent_usd >= nan` is False (never refuses) and the warn
    percentage `int(spent_usd * 100 / limit)` raises ValueError (500).
    """
    fake = GateFakeDB(usd_rows=[_usd_row(float("nan"), 1.0)])
    monkeypatch.setattr(budgets_mod, "db", fake)
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m", 0))
    assert res.ok is False
    assert "non-finite" in res.reason


class RateWindowFakeDB:
    """db seam for check_rate_limit: the window upsert + the release UPDATE."""

    def __init__(self, requests: int = 1, tokens: int = 10) -> None:
        self.requests = requests
        self.tokens = tokens
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        if "INSERT INTO rate_windows" in sql:
            return {"requests": self.requests, "tokens": self.tokens}
        return None

    async def execute(self, sql: str, *a: Any) -> str:
        self.executed.append((sql, a))
        return "UPDATE 1"


def test_c6_non_positive_rpm_and_tpm_are_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    """C6: rpm/tpm <= 0 means UNLIMITED, not "refuse every request".

    PRE-FIX (budgets.py:399-416): `if row["requests"] > rpm` with rpm=0 is
    `1 > 0` -> refused EVERY request for the life of the key, with the
    nonsense message "RPM limit 0 exceeded (1 this minute)". Profile presets
    are raw JSON and unvalidated, so {"rate_limit_rpm": 0} mints such keys.
    """
    fake = RateWindowFakeDB(requests=1, tokens=10)
    monkeypatch.setattr(budgets_mod, "db", fake)
    res = asyncio.run(budgets_mod.check_rate_limit(uuid.uuid4(), rpm=0, tpm=0, est_tokens=5))
    assert res.ok is True
    assert res.remaining_requests is None and res.remaining_tokens is None
    assert fake.executed == [], "an admitted call must not release its charge"


def test_c6_positive_limits_still_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix must not disarm the gate for a CONFIGURED limit."""
    monkeypatch.setattr(budgets_mod, "db", RateWindowFakeDB(requests=61, tokens=10))
    res = asyncio.run(budgets_mod.check_rate_limit(uuid.uuid4(), rpm=60, tpm=100_000, est_tokens=5))
    assert res.ok is False and "RPM limit 60" in res.reason

    monkeypatch.setattr(budgets_mod, "db", RateWindowFakeDB(requests=1, tokens=100_001))
    res = asyncio.run(budgets_mod.check_rate_limit(uuid.uuid4(), rpm=60, tpm=100_000, est_tokens=5))
    assert res.ok is False and "TPM limit 100000" in res.reason


class SettleFakeDB:
    """db seam for settle(): the ledger INSERT plus the window UPDATEs."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *a: Any) -> str:
        self.executed.append((sql, a))
        return "INSERT 0 1"

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        return None

    async def fetchval(self, sql: str, *a: Any) -> Any:
        return None


def test_c3_zero_prompt_settle_releases_the_predicted_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    """C3: a settle with prompt_tokens == 0 (EVERY 429/refusal path) must
    release its admission charge.

    PRE-FIX (_reconcile_predicted_charge): `if prompt_tokens <= 0 ...: return`
    ran BEFORE `_predict_charges.pop(...)`, so every refused request left an
    entry behind for an hour (the 8192-watermark prune only drops expired
    ones) — unbounded growth on a throttled key, and a later request reusing
    that request_id reconciled against ANOTHER request's charge.
    """
    monkeypatch.setattr(budgets_mod, "db", SettleFakeDB())
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", True)
    budgets_mod._predict_charges.clear()
    budgets_mod._remember_charge("req-refused", 500)
    assert "req-refused" in budgets_mod._predict_charges

    asyncio.run(
        budgets_mod.settle(
            uuid.uuid4(), "m", "chat", 0, 0, "error", 1, "req-refused"
        )
    )
    assert "req-refused" not in budgets_mod._predict_charges, "a zero-prompt settle must release its charge"
    budgets_mod._predict_charges.clear()


def test_c3_reconcile_still_applies_a_real_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    """The release must not disarm reconciliation for a REAL settle."""
    fake = SettleFakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", True)
    budgets_mod._predict_charges.clear()
    budgets_mod._remember_charge("req-ok", 100)  # charged 100 at admission
    asyncio.run(budgets_mod.settle(uuid.uuid4(), "m", "chat", 700, 0, "error", 1, "req-ok"))
    assert "req-ok" not in budgets_mod._predict_charges
    # true billable 700 - charged 100 -> a +600 window correction
    ups = [a for sql, a in fake.executed if "rate_windows" in sql and "tokens = tokens +" in sql]
    assert ups and ups[-1][2] == 600, f"expected a +600 reconciliation, saw {ups}"
    budgets_mod._predict_charges.clear()
