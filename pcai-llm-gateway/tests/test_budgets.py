"""Budget-window math and rate-limit logic (pure parts).

The window boundaries are what make day/week/month budgets meaningful —
test them without a DB by importing the window function directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
