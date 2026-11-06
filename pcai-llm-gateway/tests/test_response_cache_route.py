"""Route-level exact-match response cache (chat, non-streaming).

Pins the contract:
* active mode serves the SECOND identical deterministic request from the
  cache - the upstream is called ONCE, X-Cache: HIT, and one free cache_hit
  ledger row is settled (no budget, no rate-window token);
* a non-deterministic request (temperature 1) is never served from cache;
* shadow mode measures without serving: both requests reach the upstream,
  nothing is returned from cache, but the entry IS stored for measurement;
* mode off is byte-identical to the legacy path (no lookup, no store).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_fallbacks
from pcai_llm.core import response_cache as rc
from pcai_llm.routing.router import Endpoint, Router

MODEL = "cache-model"


class FakeDB:
    def __init__(self) -> None:
        self.settled: list[tuple] = []
        self.cache: dict[tuple[Any, ...], bytes] = {}

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "response_body" in sql:
            body = self.cache.get((a[0], a[1], a[2]))
            return {"response_body": body} if body is not None else None
        if "spend_ancestors" in sql:
            return {"spend_ancestors": None}
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetchval(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        if "INSERT INTO response_cache" in sql:
            self.cache[(a[0], a[1], a[2])] = a[3]
        if "usage_ledger" in sql:
            self.settled.append(a)
        return "INSERT 0 1"

    def settles(self, status: str | None = None) -> list[tuple]:
        return [r for r in self.settled if status is None or r[8] == status]


def _key() -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    fake = FakeDB()
    key = _key()

    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    monkeypatch.setattr(model_fallbacks, "db", fake)
    monkeypatch.setattr(rc, "db", fake)
    routes_proxy._in_flight.clear()

    def make(handler) -> TestClient:
        r = Router.with_mock(handler)
        r.set_endpoints(
            MODEL,
            [Endpoint(id=f"ep-{MODEL}", model_name=MODEL, base_url="http://cache-upstream/v1", model_type="chat")],
        )
        monkeypatch.setattr(routes_proxy, "model_router", r)
        app = FastAPI()
        app.include_router(routes_proxy.router)
        return TestClient(app, raise_server_exceptions=False)

    return fake, make


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer hllm-x"}


def _body(**over: Any) -> dict[str, Any]:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0,
    }
    body.update(over)
    return body


def _ok(n: int) -> dict[str, Any]:
    return {
        "id": f"cmpl-{n}",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }


def test_active_mode_serves_the_second_request(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, make = env
    monkeypatch.setitem(settings.__dict__, "response_cache_mode", "active")
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=_ok(calls["n"]))

    client = make(handler)
    first = client.post("/v1/chat/completions", json=_body(), headers=_h())
    assert first.status_code == 200
    assert first.headers.get("x-cache") is None
    assert calls["n"] == 1

    second = client.post("/v1/chat/completions", json=_body(), headers=_h())
    assert second.status_code == 200
    assert second.headers.get("x-cache") == "HIT"
    assert calls["n"] == 1, "the cache hit must not reach the upstream"
    assert second.json()["id"] == "cmpl-1", "the stored answer is replayed byte-identically"

    hits = fake.settles("cache_hit")
    assert len(hits) == 1 and hits[0][2] == "chat"


def test_temperature_one_is_never_cached(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake, make = env
    monkeypatch.setitem(settings.__dict__, "response_cache_mode", "active")
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=_ok(calls["n"]))

    client = make(handler)
    client.post("/v1/chat/completions", json=_body(temperature=1), headers=_h())
    client.post("/v1/chat/completions", json=_body(temperature=1), headers=_h())
    assert calls["n"] == 2


def test_shadow_mode_measures_without_serving(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, make = env
    monkeypatch.setitem(settings.__dict__, "response_cache_mode", "shadow")
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=_ok(calls["n"]))

    client = make(handler)
    client.post("/v1/chat/completions", json=_body(), headers=_h())
    second = client.post("/v1/chat/completions", json=_body(), headers=_h())

    assert calls["n"] == 2, "shadow mode must never serve from cache"
    assert second.headers.get("x-cache") is None
    assert len(fake.cache) == 1, "shadow mode still stores, so would-be hits can be measured"
    assert fake.settles("cache_hit") == []


def test_mode_off_is_the_legacy_path(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, make = env
    monkeypatch.setitem(settings.__dict__, "response_cache_mode", "off")
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=_ok(calls["n"]))

    client = make(handler)
    client.post("/v1/chat/completions", json=_body(), headers=_h())
    client.post("/v1/chat/completions", json=_body(), headers=_h())
    assert calls["n"] == 2
    assert fake.cache == {}, "off must not write cache rows"
