"""Exact-match response cache: key derivation, eligibility, fail-open store.

The cache is only as safe as its key: two requests that differ in anything
that changes the answer must not share an entry, and a cache fault must
never fail a request.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import response_cache as rc


def test_hash_ignores_transport_only_fields() -> None:
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    b = {**a, "stream": False, "stream_options": {"include_usage": True}, "user": "u1", "request_id": "r1"}
    assert rc.request_hash(a) == rc.request_hash(b)


def test_hash_changes_with_the_answer_determinants() -> None:
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    assert rc.request_hash(base) != rc.request_hash({**base, "model": "other"})
    assert rc.request_hash(base) != rc.request_hash({**base, "temperature": 0.7})
    assert rc.request_hash(base) != rc.request_hash({**base, "max_tokens": 5})
    assert rc.request_hash(base) != rc.request_hash({**base, "messages": [{"role": "user", "content": "different"}]})


def test_eligible_requires_explicit_zero_temperature() -> None:
    assert rc.eligible({"messages": [], "temperature": 0})
    assert rc.eligible({"messages": [], "temperature": 0.0})
    assert not rc.eligible({"messages": [], "temperature": 1})
    assert not rc.eligible({"messages": []})  # absent = server default (1) = a sample
    assert not rc.eligible({"messages": [], "temperature": 0, "stream": True})


class CacheDB:
    def __init__(self) -> None:
        self.rows: dict[tuple[Any, ...], bytes] = {}
        self.executed: list[tuple[str, tuple]] = []
        self.fail = False

    async def fetchrow(self, sql: str, *a: Any):
        if self.fail:
            raise RuntimeError("db down")
        return {"response_body": self.rows.get((a[0], a[1], a[2]))}

    async def execute(self, sql: str, *a: Any):
        if self.fail:
            raise RuntimeError("db down")
        self.executed.append((sql, a))
        if "INSERT INTO response_cache" in sql:
            self.rows[(a[0], a[1], a[2])] = a[3]
        return "INSERT 0 1"


@pytest.fixture()
def cache_db(monkeypatch: pytest.MonkeyPatch) -> CacheDB:
    fake = CacheDB()
    monkeypatch.setattr(rc, "db", fake)
    monkeypatch.setitem(settings.__dict__, "response_cache_mode", "active")
    monkeypatch.setitem(settings.__dict__, "response_cache_ttl_s", 60)
    monkeypatch.setitem(settings.__dict__, "response_cache_max_bytes", 1024)
    return fake


def test_store_then_lookup(cache_db: CacheDB) -> None:
    key, model, h = uuid.uuid4(), "m", "abc"
    asyncio.run(rc.store(key, model, h, b'{"ok":true}'))
    assert asyncio.run(rc.lookup(key, model, h)) == b'{"ok":true}'
    # tenant scoping: another key never sees it
    assert asyncio.run(rc.lookup(uuid.uuid4(), model, h)) is None


def test_oversize_body_is_not_stored(cache_db: CacheDB) -> None:
    key, model, h = uuid.uuid4(), "m", "big"
    asyncio.run(rc.store(key, model, h, b"x" * 2048))
    assert asyncio.run(rc.lookup(key, model, h)) is None


def test_db_fault_is_a_miss_not_an_error(cache_db: CacheDB) -> None:
    cache_db.fail = True
    key = uuid.uuid4()
    assert asyncio.run(rc.lookup(key, "m", "h")) is None
    asyncio.run(rc.store(key, "m", "h", b"{}"))  # must not raise


def test_mode_normalisation(monkeypatch: pytest.MonkeyPatch) -> None:
    for raw, want in (("off", "off"), ("shadow", "shadow"), ("ACTIVE", "active"), ("bogus", "off"), (None, "off")):
        monkeypatch.setitem(settings.__dict__, "response_cache_mode", raw)
        assert rc.mode() == want
