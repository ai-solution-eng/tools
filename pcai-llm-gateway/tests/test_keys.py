"""Key management against a MOCKED database layer.

The db layer is a thin asyncpg wrapper, so tests stub db.fetch/fetchrow/
execute. This verifies the auth contract: unknown key → None, revoked →
None, expired → None, and the plaintext key NEVER round-trips through any
stored value.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pcai_llm.core import keys


class FakeDB:
    """Records SQL + args; returns a canned row for authenticate()."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple]] = []
        self.tag = "UPDATE 1"  # execute() return tag (revoke success signal)

    async def fetchrow(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return self.row

    async def fetch(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return []

    async def execute(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return self.tag


def _row(**over: Any) -> dict[str, Any]:
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "test-key",
        "key_prefix": "hllm-ABCD",
        "scopes": ["proxy", "mcp"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "max_concurrent": 0,
        "expires_at": None,
        "revoked_at": None,
        "relay_default": False,
        "stream_pcai_events": True,
        "offpeak_fraction": None,
    }
    base.update(over)
    return base


@pytest.fixture
def patched_db(monkeypatch: pytest.MonkeyPatch):
    def _install(row: dict[str, Any] | None) -> FakeDB:
        fake = FakeDB(row)
        monkeypatch.setattr(keys, "db", fake)
        return fake

    return _install


async def test_authenticate_happy_path(patched_db) -> None:
    fake = patched_db(_row())
    rec = await keys.authenticate("whatever-plaintext")
    assert rec is not None
    assert rec.scopes == ["proxy", "mcp"]
    # the lookup must be BY HASH, not by plaintext
    sql, args = fake.queries[0]
    assert "key_hash = $1" in sql
    assert args[0] == keys.hash_key("whatever-plaintext")


async def test_authenticate_unknown_key_fails_closed(patched_db) -> None:
    patched_db(None)
    assert await keys.authenticate("bogus") is None


async def test_authenticate_revoked_fails_closed(patched_db) -> None:
    patched_db(_row(revoked_at=datetime.now(UTC)))
    assert await keys.authenticate("x") is None


async def test_authenticate_expired_fails_closed(patched_db) -> None:
    patched_db(_row(expires_at=datetime.now(UTC) - timedelta(hours=1)))
    assert await keys.authenticate("x") is None


def test_generate_key_prefix_and_hash_roundtrip() -> None:
    plaintext, h = keys.generate_key()
    assert plaintext.startswith("hllm-")
    assert keys.hash_key(plaintext) == h
    # hash is not reversible: the plaintext must not appear in it
    assert plaintext not in h


async def test_revoke_key_reports_update_tag(patched_db) -> None:
    """revoke_key is the rotate endpoint's revoke step: its boolean is what
    `old_revoked` in the rotate response surfaces. True on a real revocation
    (UPDATE 1), False when the row was already revoked / raced (UPDATE 0)."""
    fake = patched_db(None)
    assert await keys.revoke_key(uuid.uuid4()) is True
    sql, _args = fake.queries[-1]
    # the guard matters: never re-stamp revoked_at on an already-revoked row
    assert "revoked_at IS NULL" in sql
    fake.tag = "UPDATE 0"
    assert await keys.revoke_key(uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# stream_pcai_events (RFC-PER-KEY-STREAM-EVENTS) — per-key flag round-trip
# ---------------------------------------------------------------------------


async def test_stream_pcai_events_roundtrip(patched_db) -> None:
    """create → authenticate → list carries the flag; default TRUE; the
    SELECT names the column (a SELECT missing it would silently revert
    every key to the dataclass default)."""
    patched_db(_row(stream_pcai_events=False))
    rec = await keys.authenticate("x")
    assert rec is not None and rec.stream_pcai_events is False
    # create_key passes the flag positionally after relay_default; the fake
    # must answer the INSERT..RETURNING id with a row (hence the id dict).
    fake2 = patched_db({"id": uuid.uuid4()})
    await keys.create_key(uuid.uuid4(), "k", ["proxy"], stream_pcai_events=False)
    insert_sql, insert_args = fake2.queries[0]
    assert "stream_pcai_events" in insert_sql
    assert insert_args[-2] is False  # 11th INSERT value = the flag
    assert insert_args[-1] == []  # 12th = spend_ancestors (default: a fresh mint has no lineage)
    # authenticate's SELECT must read the column
    fake3 = patched_db(_row())
    await keys.authenticate("x")
    select_sql, _ = fake3.queries[0]
    assert "stream_pcai_events" in select_sql


def test_stream_pcai_events_default_true() -> None:
    """Default TRUE: deploy is a no-op for every existing key (RFC §5)."""
    rec = keys.KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-x",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=60,
        rate_limit_tpm=1000,
        max_concurrent=0,
        expires_at=None,
    )
    assert rec.stream_pcai_events is True
    assert rec.relay_default is False
