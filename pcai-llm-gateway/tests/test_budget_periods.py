"""Quarter/year budget periods — window math and SUM placeholder lineage.

The 2026-11-02 wave widened the period set (day|week|month -> +quarter|year)
in THREE places that must never disagree: core.budgets.VALID_PERIODS (the
gate + preset validation), core.teams.TEAM_PERIODS (the enrollment envelope)
and the admin route's VALID_PERIODS. The dangerous half is the spend SUMs:
they bind one timestamptz placeholder PER PERIOD through a hand-numbered
CASE, and a numbering drift there was a real-PG CannotCoerceError that 500d
every budgeted call while every fake-seam test stayed green — so the tests
below pin the SQL text itself (one ::timestamptz placeholder per period,
lineage last and distinct), which no fake DB would otherwise notice.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from pcai_llm.api import routes_admin
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import teams as teams_mod
from pcai_llm.core.budgets import _WINDOW_PERIODS, _window_start

# ---------------------------------------------------------------------------
# _window_start: quarter/year boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("when", "want"),
    [
        # Jan 1 00:00 UTC; Apr 1; Jul 1; Oct 1 — plus the edges that must NOT
        # roll into the next (or previous) quarter.
        (datetime(2026, 1, 15, 14, 30, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        (datetime(2026, 2, 28, 23, 59, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        (datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        (datetime(2026, 4, 1, 0, 0, 0, 1, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)),
        (datetime(2026, 6, 30, 12, 0, tzinfo=UTC), datetime(2026, 4, 1, tzinfo=UTC)),
        (datetime(2026, 7, 4, 9, 0, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
        (datetime(2026, 9, 30, 23, 59, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
        (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
        (datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)),
    ],
)
def test_quarter_window_boundaries(when: datetime, want: datetime) -> None:
    ws = _window_start(when, "quarter")
    assert ws == want.replace(tzinfo=UTC)
    assert ws.tzinfo is UTC


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 4, 9, 30, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
    ],
)
def test_year_window_is_jan_first_utc_midnight(when: datetime) -> None:
    ws = _window_start(when, "year")
    assert (ws.year, ws.month, ws.day) == (when.year, 1, 1)
    assert (ws.hour, ws.minute, ws.second, ws.microsecond) == (0, 0, 0, 0)
    assert ws.tzinfo is UTC


def test_quarter_and_year_agree_on_jan_first() -> None:
    # The one date where the two windows coincide — a budget created on the
    # year's first day must not double-charge through two different boundaries.
    when = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)
    assert _window_start(when, "quarter") == _window_start(when, "year") == datetime(2026, 1, 1, tzinfo=UTC)


def test_window_ordering_year_quarter_month_week_day() -> None:
    # The wider the period, the earlier (or equal) the boundary — the same
    # now must satisfy year <= quarter <= month <= week <= day.
    now = datetime.now(UTC)
    starts = {p: _window_start(now, p) for p in ("year", "quarter", "month", "week", "day")}
    assert starts["year"] <= starts["quarter"] <= starts["month"] <= starts["week"] <= starts["day"]
    for ws in starts.values():
        assert ws.tzinfo is UTC


def test_quarter_boundary_is_oct_first_for_q4() -> None:
    # 2026-11-02 is inside Q4: the quarter boundary is Oct 1 00:00 UTC — NOT
    # the day 90 days back and NOT a local-midnight offset.
    now = datetime(2026, 11, 2, 16, 13, tzinfo=UTC)
    ws = _window_start(now, "quarter")
    assert (ws.month, ws.day) == (10, 1)
    assert (ws.hour, ws.minute) == (0, 0)
    assert ws.tzinfo is UTC


# ---------------------------------------------------------------------------
# The spend SUMs: five numbered timestamptz placeholders, lineage last
# ---------------------------------------------------------------------------


class _SumCaptureDB:
    """Captures every statement + args; answers the lineage PK lookup.

    fetchrow is implemented (the modern seam) so _budget_sums takes the
    fetchrow branch; it returns None = no spend_ancestors on this fake =
    empty lineage, the normal key shape.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.calls.append((sql, a))
        return None

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self.calls.append((sql, a))
        return None

    async def fetch(self, sql: str, *a: Any) -> list[Any]:
        self.calls.append((sql, a))
        return []


_FROZEN_NOW = datetime(2026, 11, 4, 16, 13, tzinfo=UTC)  # a Wednesday: day != week


class _FrozenDateTime(datetime):
    """datetime replacement whose now() is pinned — makes the window args
    EXACTLY assertable instead of only shape-checkable."""

    @classmethod
    def now(cls, tz: Any = None) -> Any:  # mimics datetime.now loosely; Any keeps the override simple
        return _FROZEN_NOW.replace(tzinfo=tz) if tz is not None else _FROZEN_NOW.replace(tzinfo=None)


def _sum_calls(fake: _SumCaptureDB) -> list[tuple[str, tuple[Any, ...]]]:
    return [(sql, a) for sql, a in fake.calls if "usage_ledger" in sql]


def test_budget_sums_binds_five_windows_in_canonical_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-key gate SUM: exactly five $n::timestamptz placeholders, in
    _WINDOW_PERIODS order, each mapped to ITS OWN arg, lineage last and
    distinct."""
    fake = _SumCaptureDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "datetime", _FrozenDateTime)
    kid = uuid.uuid4()
    asyncio.run(budgets_mod._budget_sums("budgets", "limit_tokens", "total_tokens", kid, None))

    expected = {p: _window_start(_FROZEN_NOW, p) for p in _WINDOW_PERIODS}
    for sql, args in _sum_calls(fake):
        # one timestamptz placeholder PER period — a period added without a
        # WHEN (or a WHEN without an arg) breaks this count, not just the guard
        phs = re.findall(r"\$(\d+)::timestamptz", sql)
        assert len(phs) == len(_WINDOW_PERIODS) == 5, sql
        assert phs == ["2", "3", "4", "5", "6"], sql  # $2..$6 after $1 = key_id
        # the five args ARE the five window starts, day -> year, value-distinct
        wins = list(args[1:6])
        assert wins == [expected[p] for p in ("day", "week", "month", "quarter", "year")]
        assert len(set(wins)) == 5
        assert all(isinstance(w, datetime) and w.tzinfo is UTC for w in wins)
        # lineage is LAST, its number is one past the last window, and the
        # SQL binds it exactly once as a uuid[] — the 2026-10 drift shape
        lineage = args[-1]
        assert lineage == [kid]  # empty ancestors -> just the key itself
        last_num = int(phs[-1]) + 1
        assert "ANY($" + str(last_num) + "::uuid[])" in sql
        assert len(args) == last_num


def test_budget_sums_numbering_survives_a_pinned_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """model=m shifts EVERY number by one ($2 = the model) — the windows
    must move with it, not collide with the model or the lineage arg."""
    fake = _SumCaptureDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "datetime", _FrozenDateTime)
    kid = uuid.uuid4()
    asyncio.run(budgets_mod._budget_sums("cost_budgets", "limit_usd", "cost_usd", kid, "m"))
    sql, args = _sum_calls(fake)[0]
    assert args[0] == kid and args[1] == "m"
    phs = re.findall(r"\$(\d+)::timestamptz", sql)
    assert phs == ["3", "4", "5", "6", "7"], sql
    # lineage: $8, len(args) == 8, and that number appears exactly once
    assert "ANY($" + str(8) + "::uuid[])" in sql
    assert len(args) == 8
    assert sql.count("$8") == 1


def test_budget_status_many_binds_five_windows_after_the_key_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console rollup: IN ($1..$n) keys, then n+1..n+5 = the windows in
    _WINDOW_PERIODS order. The n-offset is how the old code 500d real PG."""
    fake = _SumCaptureDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "datetime", _FrozenDateTime)
    kids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    asyncio.run(budgets_mod.budget_status_many(kids))

    expected = {p: _window_start(_FROZEN_NOW, p) for p in _WINDOW_PERIODS}
    n = len(kids)
    for sql, args in _sum_calls(fake):
        assert "IN ($1, $2, $3)" in sql
        phs = re.findall(r"\$(\d+)::timestamptz", sql)
        assert phs == [str(n + i + 1) for i in range(5)], sql
        wins = list(args[n : n + 5])
        assert wins == [expected[p] for p in ("day", "week", "month", "quarter", "year")]
        assert len(args) == n + 5  # keys + five windows, nothing else


def test_period_sets_agree_across_the_three_owners() -> None:
    """One period vocabulary: the gate, the team envelope and the admin
    route must accept exactly the same set — quarter and year included,
    hour still NOT a period (it is the rate limiter job, not a budget)."""
    want = {"day", "week", "month", "quarter", "year"}
    assert set(budgets_mod.VALID_PERIODS) == want
    assert set(teams_mod.TEAM_PERIODS) == want
    assert set(routes_admin.VALID_PERIODS) == want
    assert set(budgets_mod._WINDOW_PERIODS) == want
    assert "hour" not in want


def test_split_preset_budgets_accepts_quarter_and_year() -> None:
    """The validation chain (profile presets -> key budgets -> team
    envelope) must let quarter/year through — a preset the schema allows
    but the validator refuses is a 400 on a row the gate would enforce."""
    token_rows, usd_rows = budgets_mod.split_preset_budgets(
        [
            {"model": "*", "period": "quarter", "limit_tokens": 1000},
            {"model": "m", "period": "year", "limit_usd": 10.0},
        ]
    )
    assert token_rows == [("*", "quarter", 1000)]
    assert usd_rows == [("m", "year", 10.0)]


def test_team_normalize_period_passes_quarter_and_year_through() -> None:
    # A stored budget_period of quarter/year must keep ITS OWN window, not
    # fall back to the month default (that would silently re-window a team
    # pool and log the row as junk).
    assert teams_mod._normalize_period("quarter") == "quarter"
    assert teams_mod._normalize_period("year") == "year"
    assert teams_mod._normalize_period(None) == teams_mod.DEFAULT_TEAM_PERIOD


# ---------------------------------------------------------------------------
# UI: every budget-period dropdown offers quarter and year
# ---------------------------------------------------------------------------


def _select_block(html: str, select_id: str) -> str:
    i = html.index('id="' + select_id + '"')
    j = html.index("</select>", i)
    return html[i:j]


@pytest.mark.parametrize(
    "select_id",
    ["p-b-period", "e-budget-period", "b-period", "ee-period"],
)
def test_admin_period_dropdowns_offer_quarter_and_year(select_id: str) -> None:
    html = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    block = _select_block(html, select_id)
    assert ">quarter<" in block, select_id + " lacks quarter"
    assert ">year<" in block, select_id + " lacks year"


def test_team_leader_period_dropdown_offers_quarter_and_year() -> None:
    html = open("src/pcai_llm/ui/team_leader.html", encoding="utf-8").read()
    block = _select_block(html, "tb-period")
    assert ">quarter<" in block and ">year<" in block
