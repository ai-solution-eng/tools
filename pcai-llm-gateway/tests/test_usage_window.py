"""The usage/chargeback window: custom `until` bounds and their wiring.

audit P1-4 bounded the summary to a `since` + implicit now. The custom
range (quarterly/yearly chargeback reports) pins BOTH ends: _bounded_window
grows an `until` validated in the _parse_since style (ISO-8601 WITH a UTC
offset — a naive string would be container-local the moment asyncpg
encodes it), never earlier than the window start, span capped at
_MAX_WINDOW_DAYS. The endpoint must pass BOTH bounds into every query
(a fake db captures the args — a bound computed but not sent would show
up as a silent wrong number, not a failure)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from pcai_llm.api import routes_admin as ra

# ---------------------------------------------------------------------------
# _bounded_window: the until parameter
# ---------------------------------------------------------------------------


def test_until_is_accepted_and_returned() -> None:
    since = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    until = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    s, u = ra._bounded_window(since.isoformat(), default_days=None, until=until.isoformat())
    assert s == since
    assert u == until


def test_until_rejects_garbage() -> None:
    since = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(HTTPException) as exc:
        ra._bounded_window(since.isoformat(), default_days=30, until="not-a-date")
    assert exc.value.status_code == 400
    assert "until must be an ISO-8601 datetime" in str(exc.value.detail)


def test_until_rejects_naive_datetime() -> None:
    # audit P2-6 mirror: a tz-naive until would be container-local when
    # asyncpg encodes it — same refusal as a naive since.
    since = datetime(2026, 10, 1, tzinfo=UTC)
    with pytest.raises(HTTPException) as exc:
        ra._bounded_window(since.isoformat(), default_days=30, until="2026-12-31T00:00:00")
    assert exc.value.status_code == 400
    assert "until must include a UTC offset" in str(exc.value.detail)


def test_until_before_since_is_a_400() -> None:
    now = datetime.now(UTC)
    with pytest.raises(HTTPException) as exc:
        ra._bounded_window(now.isoformat(), default_days=30, until=(now - timedelta(hours=1)).isoformat())
    assert exc.value.status_code == 400
    assert "until must not be earlier than since" in str(exc.value.detail)


def test_span_over_the_cap_is_a_400_even_with_until() -> None:
    # 2026-01-01 -> 2027-01-03 is 367 days: over _MAX_WINDOW_DAYS. The cap
    # is a query-cost guard and must hold for the custom range too.
    with pytest.raises(HTTPException) as exc:
        ra._bounded_window(
            datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
            default_days=30,
            until=datetime(2027, 1, 3, tzinfo=UTC).isoformat(),
        )
    assert exc.value.status_code == 400
    assert "window too large" in str(exc.value.detail)


def test_span_of_exactly_366_days_is_allowed() -> None:
    # 2026 is not a leap year: Jan 1 2026 -> Jan 2 2027 = 366 days — ON the
    # cap, so it must pass (the check is strictly greater-than).
    s, u = ra._bounded_window(
        datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
        default_days=30,
        until=datetime(2027, 1, 2, tzinfo=UTC).isoformat(),
    )
    assert s == datetime(2026, 1, 1, tzinfo=UTC)
    assert u == datetime(2027, 1, 2, tzinfo=UTC)


def test_until_with_default_since_uses_the_default_window_start() -> None:
    # until without an explicit since: the default since (now-30d) applies,
    # and the pair is validated like any other.
    now = datetime.now(UTC)
    until = (now + timedelta(days=1)).isoformat()
    s, u = ra._bounded_window(None, default_days=30, until=until)
    assert s is not None and u is not None
    assert u == datetime.fromisoformat(until)
    assert abs((s - (now - timedelta(days=30))).total_seconds()) < 5


def test_until_without_any_since_is_a_400() -> None:
    # The no-bound shape (default_days=None) means NO window at all — an
    # until there has nothing to anchor to. Explicit 400, never silence.
    with pytest.raises(HTTPException) as exc:
        ra._bounded_window(None, default_days=None, until=datetime(2026, 12, 31, tzinfo=UTC).isoformat())
    assert exc.value.status_code == 400
    assert "until requires an explicit since" in str(exc.value.detail)


def test_no_until_keeps_the_legacy_now_bound() -> None:
    # The pre-until contract is untouched: since -> (since, now).
    since = datetime(2026, 10, 1, tzinfo=UTC)
    s, u = ra._bounded_window(since.isoformat(), default_days=30)
    assert s == since
    assert u is not None
    assert abs((u - datetime.now(UTC)).total_seconds()) < 5


# ---------------------------------------------------------------------------
# usage_summary: both bounds reach every query and the response block
# ---------------------------------------------------------------------------


class _WindowCapDB:
    """Records (sql, args) of the rollups; answers the rest neutrally."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.calls.append((sql, a))
        return {}

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self.calls.append((sql, a))
        return None

    async def fetch(self, sql: str, *a: Any) -> list[Any]:
        self.calls.append((sql, a))
        return []


def test_usage_summary_passes_since_and_until_into_every_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _WindowCapDB()
    monkeypatch.setattr(ra, "db", fake)
    since = "2026-10-01T00:00:00+00:00"
    until = "2026-12-31T23:59:59+00:00"
    out = asyncio.run(ra.usage_summary(None, since=since, until=until))
    rollups = [(sql, a) for sql, a in fake.calls if "WHERE" in sql]
    assert len(rollups) == 4, "totals + by_model + by_key + by_key_model"
    for sql, args in rollups:
        assert len(args) == 2, sql
        assert args[0] == datetime.fromisoformat(since)
        assert args[1] == datetime.fromisoformat(until)
        assert "ts >= $1" in sql and "ts <= $2" in sql
    # ... and the window block echoes the effective span for the console note
    assert out["window"]["since"] == since
    assert out["window"]["until"] == until


def test_usage_summary_until_still_defaults_to_now(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _WindowCapDB()
    monkeypatch.setattr(ra, "db", fake)
    since = "2026-10-01T00:00:00+00:00"
    out = asyncio.run(ra.usage_summary(None, since=since))
    for _sql, args in fake.calls:
        if not args:
            continue
        assert len(args) == 2
        assert args[0] == datetime.fromisoformat(since)
        # until = the call moment: 'now' at query build, not a pinned instant
        assert abs((args[1] - datetime.now(UTC)).total_seconds()) < 5
        assert args[1] > args[0]
    assert out["window"]["until"] is not None


# ---------------------------------------------------------------------------
# UI: the window select offers the presets and a working custom range
# ---------------------------------------------------------------------------


def test_admin_usage_window_has_presets_and_custom_range() -> None:
    html = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    # quarterly/yearly presets sit beside the day-count ones
    block = html[html.index('id="usage-window"') : html.index("</select>", html.index('id="usage-window"'))]
    assert 'value="90"' in block and "Quarterly" in block
    assert 'value="365"' in block and "Yearly" in block
    assert 'value="custom">' in block
    # the custom range inputs exist and are datetime-local
    assert 'type="datetime-local" id="usage-custom-start"' in html
    assert 'type="datetime-local" id="usage-custom-end"' in html
    # ... and the loader sends BOTH bounds for custom, ISO with offset
    assert "until='" in html or "&until=" in html or "+ '&until='" in html
    assert "toISOString()" in html
    # the selection and the custom bounds both persist for the session
    assert "pcai-admin-usage-window" in html
    assert "pcai-admin-usage-custom" in html
