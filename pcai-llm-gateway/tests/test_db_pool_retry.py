"""db._make_pool startup-race hardening (2026-10-08).

Pins the bounded hold-and-retry around asyncpg.create_pool: transient
ConnectionRefused during the mesh sidecar's warm-up window is absorbed
(3 attempts, short backoff), while a persistently wrong configuration
still fails loud after the window — db.connect() stays fail-LOUD, just
not fail-FIRST. No live Postgres, no network: asyncpg.create_pool is
monkeypatched (the suite convention).
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest

from pcai_llm.config import settings
from pcai_llm.db import Database

# the discrete-mode kwargs _connect_kwargs needs (settings baseline)
_TENANT_NONE = None


@pytest.fixture()
def discrete_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "database_url", "")
    monkeypatch.setattr(settings, "db_host", "pg.local")
    monkeypatch.setattr(settings, "db_port", 5432)
    monkeypatch.setattr(settings, "db_user", "u")
    monkeypatch.setattr(settings, "db_password", "p")
    monkeypatch.setattr(settings, "db_name", "d")
    monkeypatch.setattr(settings, "db_pool_min", 1)
    monkeypatch.setattr(settings, "db_pool_max", 2)


class _FakePool:
    async def execute(self, *_a: Any, **_k: Any) -> str:
        return "OK"


@pytest.mark.asyncio()
async def test_transient_refusal_is_absorbed(discrete_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """ConnectionRefused on the first two attempts (the sidecar warm-up
    window), success on the third ⇒ the pool is returned, create_pool was
    called exactly 3 times, and the sleeps used the short backoff ladder."""
    calls: list[int] = []
    sleeps: list[float] = []

    async def fake_create_pool(**_kwargs: Any):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionRefusedError("[Errno 111] Connection refused")
        return _FakePool()

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    db = Database()
    pool = await db._make_pool()
    assert isinstance(pool, _FakePool)
    assert len(calls) == 3
    assert sleeps == [1.5, 3.0]  # 1.5 * attempt, attempt = 1, 2


@pytest.mark.asyncio()
async def test_persistent_refusal_fails_loud(discrete_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DSN that is simply WRONG must still crash fast and visibly — the
    retry window is bounded (3 attempts), then the last exception
    propagates to db.connect()'s fail-loud exit."""
    calls: list[int] = []
    sleeps: list[float] = []

    async def fake_create_pool(**_kwargs: Any):
        calls.append(1)
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    db = Database()
    with pytest.raises(ConnectionRefusedError):
        await db._make_pool()
    assert len(calls) == 3  # bounded — not an infinite retry loop
    assert sleeps == [1.5, 3.0]


@pytest.mark.asyncio()
async def test_non_transient_postgres_error_also_retries_within_window(
    discrete_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """asyncpg.PostgresError is in the absorbed class too (a PG that is
    up but briefly rejecting, e.g. crash-recovery) — but the window stays
    bounded, and a non-(OSError|PostgresError) exception propagates on
    attempt 1 (unrelated config bugs must not be retried)."""
    calls: list[int] = []

    async def fake_create_pool(**_kwargs: Any):
        calls.append(1)
        raise asyncpg.InvalidPasswordError("bad password")

    async def fake_sleep(_delay: float) -> None:
        pass

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    db = Database()
    with pytest.raises(asyncpg.InvalidPasswordError):
        await db._make_pool()
    assert len(calls) == 3
