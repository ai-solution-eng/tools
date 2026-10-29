"""Canonical-store prune: marker PROVENANCE + the first-hour throttle
(audit 2026-10-29, zone 4 — findings F3 and C8).

Why a separate file: tests/test_canonical_retention.py owns the retention
CONTRACT (distilled-only, 0 = disabled, throttle, error-swallowing) and belongs
to the distilling zone's write scope; these two regressions are core
housekeeping defects and live here so the two changes cannot collide.

F3 — the prune's EXISTS matched ORPHANED canonical_distills markers.
     canonical_distills has a PRIMARY KEY and NO foreign key to
     canonical_conversations, so a marker can outlive the row it described and
     be matched against a NEW conversation that reuses the same
     (key_id, thread_id) pair. Deleting on such a marker discards a
     never-distilled transcript, defeating the "cannot lose an un-distilled
     conversation" contract the prune exists to honour.
F8/C8 — `_last_canonical_prune = 0.0` was compared against
     time.monotonic() (HOST UPTIME), so on a node up for less than the 3600 s
     interval the FIRST prune after every pod start was skipped.

No live Postgres: the repo's monkeypatch-the-module-db pattern.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import housekeeping


class MarkerFakeDB:
    """In-memory canonical_conversations + canonical_distills.

    The DELETE is EVALUATED, not just recorded: the fake applies the same
    predicates the real statement spells out (row age, marker age, gate-skip
    suppression, and — when the statement carries it — the state snapshot
    match), so removing a predicate from the SQL changes the RESULT here
    instead of only a string comparison.
    """

    def __init__(self, rows: list[dict[str, Any]], markers: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.markers = markers
        self.executed: list[str] = []

    async def execute(self, sql: str, *a: Any) -> str:
        self.executed.append(sql)
        if "DELETE FROM canonical_conversations" not in sql:
            return "DELETE 0"
        cutoff = datetime.now(UTC) - timedelta(days=int(a[0]))
        requires_state = "d.distilled_row_state = c.updated_at" in sql
        keep: list[dict[str, Any]] = []
        deleted = 0
        for row in self.rows:
            proof = any(
                m["key_id"] == row["key_id"]
                and m["thread_id"] == row["thread_id"]
                and m["distilled_at"] < cutoff
                and m["gate_failed_at"] is None
                and (not requires_state or m["distilled_row_state"] == row["updated_at"])
                for m in self.markers
            )
            if row["updated_at"] < cutoff and proof:
                deleted += 1
            else:
                keep.append(row)
        self.rows = keep
        return f"DELETE {deleted}"


def _row(key_id: str, thread: str, updated_at: datetime) -> dict[str, Any]:
    return {"key_id": key_id, "thread_id": thread, "updated_at": updated_at}


def _marker(
    key_id: str,
    thread: str,
    distilled_at: datetime,
    distilled_row_state: datetime,
    gate_failed_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "key_id": key_id,
        "thread_id": thread,
        "distilled_at": distilled_at,
        "distilled_row_state": distilled_row_state,
        "gate_failed_at": gate_failed_at,
    }


def _clear_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(housekeeping, "_last_canonical_prune", None)


def test_f3_prune_requires_the_marker_to_describe_the_current_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """F3 — an ORPHANED marker must not authorize deleting the row.

    PRE-FIX (housekeeping.py:115-125): the EXISTS matched on (key_id,
    thread_id) + distilled_at age + gate_failed_at only. A marker left by a
    PREVIOUS incarnation of the pair (the row was re-captured afterwards, so
    updated_at moved on) therefore deleted a conversation that was never
    distilled at its current state.
    """
    old = datetime.now(UTC) - timedelta(days=200)
    recent = datetime.now(UTC) - timedelta(days=1)
    fake = MarkerFakeDB(
        rows=[
            _row("k1", "truly-distilled", old),  # marker matches the CURRENT state
            _row("k1", "re-captured", recent),  # marker describes an EARLIER state
        ],
        markers=[
            _marker("k1", "truly-distilled", old, old),
            _marker("k1", "re-captured", old, old),  # distilled_row_state is stale
        ],
    )
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    _clear_throttle(monkeypatch)

    asyncio.run(housekeeping._prune_canonical_store())

    remaining = sorted(r["thread_id"] for r in fake.rows)
    assert remaining == ["re-captured"], f"only a state-matched marker may authorize a delete, kept {remaining}"
    stmt = next(s for s in fake.executed if "DELETE FROM canonical_conversations" in s)
    assert "d.distilled_row_state = c.updated_at" in stmt, "the statement itself must carry the state snapshot"


def test_f3_gate_skipped_marker_still_protects_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-existing gate-skip protection is untouched by the new predicate."""
    old = datetime.now(UTC) - timedelta(days=200)
    fake = MarkerFakeDB(
        rows=[_row("k1", "gate-skipped", old)],
        markers=[_marker("k1", "gate-skipped", old, old, gate_failed_at=old)],
    )
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    _clear_throttle(monkeypatch)

    asyncio.run(housekeeping._prune_canonical_store())
    assert [r["thread_id"] for r in fake.rows] == ["gate-skipped"]


class _Clock:
    """Stands in for the `time` module inside housekeeping: a monotonic
    reading of 5 s is what a pod sees on a host that booted five seconds ago."""

    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def test_c8_first_prune_runs_on_a_freshly_booted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """C8 — the canonical prune must run on the FIRST tick of a new process,
    even when the host's monotonic clock reads less than the 1 h interval.

    PRE-FIX (housekeeping.py:38, 107): `_last_canonical_prune = 0.0` and
    `if now - _last_canonical_prune < _CANONICAL_PRUNE_INTERVAL_S: return`
    with now = time.monotonic() (HOST UPTIME). On a host up 5 s the first prune
    was skipped for the next 3595 s, after EVERY pod restart.
    """
    fake = MarkerFakeDB(rows=[], markers=[])
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    monkeypatch.setattr(housekeeping, "time", _Clock(5.0))
    monkeypatch.setattr(housekeeping, "_last_canonical_prune", None)  # fresh process

    # the pre-fix arithmetic, emulated: 5.0 - 0.0 < 3600 -> early return
    assert 5.0 - 0.0 < housekeeping._CANONICAL_PRUNE_INTERVAL_S

    asyncio.run(housekeeping._prune_canonical_store())
    assert any("DELETE FROM canonical_conversations" in s for s in fake.executed), (
        "the first prune of a fresh process must not be throttled by host uptime"
    )


def test_c8_throttle_still_applies_within_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sentinel change must not disarm the throttle: two calls in one
    process still issue ONE delete."""
    fake = MarkerFakeDB(rows=[], markers=[])
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    monkeypatch.setattr(housekeeping, "time", _Clock(5.0))
    monkeypatch.setattr(housekeeping, "_last_canonical_prune", None)

    asyncio.run(housekeeping._prune_canonical_store())
    asyncio.run(housekeeping._prune_canonical_store())
    assert len([s for s in fake.executed if "DELETE FROM canonical_conversations" in s]) == 1
