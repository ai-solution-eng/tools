"""Canonical-store retention tests (housekeeping).

canonical_conversations is the DISTILLER'S INPUT: full message history as
JSONB, and nothing else in the codebase ever prunes it, so on a busy gateway it
is the biggest table and grows without bound. The prune added 2026-10-10 is
therefore DISTILLED-ONLY — a row is deletable only once a canonical_distills
marker proves a memory was written for it — so enabling retention can never
lose an un-distilled conversation.

These tests pin that contract, the 0 = disabled default, the throttle (the
housekeeping loop ticks every 60s; the prune must not run every minute), and
the error-swallowing discipline every housekeeping step shares.

No live Postgres: the repo's monkeypatch-the-module-db pattern.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import housekeeping


class PruneFakeDB:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.args: list[tuple] = []

    async def execute(self, sql: str, *a: Any):
        self.executed.append(sql)
        self.args.append(a)
        return "DELETE 3"


def _reset_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear the throttle by moving the sentinel into the PAST, not to 0.0.
    # The gate is `time.monotonic() - _last_canonical_prune < INTERVAL`, and
    # monotonic() is HOST UPTIME, not wall clock: on a machine up for less
    # than the 3600 s interval, `now - 0.0` is still < INTERVAL, so the prune
    # returns early and the test asserts on a DELETE that was never issued
    # (StopIteration). Passing locally on a long-lived host and failing in a
    # fresh container is the signature of this bug.
    monkeypatch.setattr(housekeeping, "_last_canonical_prune", -float("inf"))


def _canonical_stmt(fake: PruneFakeDB) -> str:
    return next(s for s in fake.executed if "DELETE FROM canonical_conversations" in s)


def test_prune_is_distilled_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The load-bearing safety property: the DELETE must be gated on an
    EXISTS canonical_distills marker. Without that gate the prune would delete
    conversations that have not become memories yet, which is data loss."""
    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    _reset_throttle(monkeypatch)

    asyncio.run(housekeeping._prune_canonical_store())

    stmt = _canonical_stmt(fake)
    # distilled-only: an EXISTS over the marker table, matched on the PK pair
    assert "EXISTS" in stmt
    assert "canonical_distills" in stmt
    assert "d.key_id = c.key_id" in stmt
    assert "d.thread_id = c.thread_id" in stmt
    # age comes from the MARKER (proof-of-write), not the row's own updated_at
    assert "d.distilled_at < now() - make_interval(days => $1)" in stmt
    # and the row must also be quiet past the same cutoff
    assert "c.updated_at < now() - make_interval(days => $1)" in stmt

    idx = fake.executed.index(stmt)
    assert fake.args[idx] == (90,)


def test_zero_days_keeps_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 = disabled, the same opt-in contract as the sibling retention knobs:
    nothing is deleted until the operator sets a policy."""
    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 0)
    _reset_throttle(monkeypatch)

    asyncio.run(housekeeping._prune_canonical_store())
    assert fake.executed == []


def test_sweep_runs_the_canonical_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    """_sweep_once must actually call it — a prune nobody invokes is dead code."""
    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    _reset_throttle(monkeypatch)

    asyncio.run(housekeeping._sweep_once())
    assert any("DELETE FROM canonical_conversations" in s for s in fake.executed)


def test_prune_is_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The housekeeping loop ticks every CADENCE_S (60s); a delete over the
    biggest table must NOT run every minute. Two immediate calls => one DELETE."""
    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    _reset_throttle(monkeypatch)

    asyncio.run(housekeeping._prune_canonical_store())
    asyncio.run(housekeeping._prune_canonical_store())

    assert len([s for s in fake.executed if "DELETE FROM canonical_conversations" in s]) == 1


def test_prune_runs_again_after_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Throttled, not one-shot: once the window lapses the prune runs again."""
    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    monkeypatch.setattr(housekeeping, "_last_canonical_prune", 0.0)
    monkeypatch.setattr(housekeeping, "_CANONICAL_PRUNE_INTERVAL_S", 0.0)

    asyncio.run(housekeeping._prune_canonical_store())
    asyncio.run(housekeeping._prune_canonical_store())

    assert len([s for s in fake.executed if "DELETE FROM canonical_conversations" in s]) == 2


def test_prune_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prune fault must never fail the sweep (the housekeeping contract)."""

    class Boom(PruneFakeDB):
        async def execute(self, sql: str, *a: Any):
            if "canonical_conversations" in sql:
                raise RuntimeError("db hiccup")
            return await super().execute(sql, *a)

    monkeypatch.setattr(housekeeping, "db", Boom())
    monkeypatch.setattr(settings, "retention_canonical_days", 90)
    _reset_throttle(monkeypatch)

    asyncio.run(housekeeping._sweep_once())  # must NOT raise


def test_knob_defaults_off() -> None:
    """The shipped default is 0: an operator opts IN to deleting canonical rows."""
    assert settings.retention_canonical_days == 0
