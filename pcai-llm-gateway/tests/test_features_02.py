"""Tests for the 0.2 feature batch: embedding cache, /v1/usage, budget
warnings, concurrency cap."""

from __future__ import annotations

import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import embedcache
from pcai_llm.routing.router import Endpoint, Router

MODEL = "emb-model"


class FakeDB:
    """Records settle/cache SQL; configurable behaviors per test."""

    def __init__(self, cache_rows: dict[str, Any] | None = None) -> None:
        self.cache_rows = cache_rows or {}
        self.executed: list[tuple] = []
        self.budget_spent_pct = 0  # 0-100+; drives warn levels via fake fetchval

    async def fetch(self, sql: str, *args: Any):
        if "FROM budgets" in sql:
            # single aggregate query now (budget rows joined to window spend)
            if self.budget_spent_pct <= 0:
                return []
            return [{"model": "*", "period": "month", "limit_tokens": 1000, "spent": self.budget_spent_pct * 10}]
        if "FROM cost_budgets" in sql:
            return []
        if "usage_ledger" in sql:
            return [
                {
                    "ts": "t",
                    "model": MODEL,
                    "endpoint": "embeddings",
                    "prompt_tokens": 1,
                    "completion_tokens": 0,
                    "total_tokens": 1,
                    "status": "ok",
                    "latency_ms": 5,
                }
            ]
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "profiles" in sql:
            return None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetchval(self, sql: str, *args: Any):
        if "usage_ledger" in sql and "SUM" in sql:
            return self.budget_spent_pct * 10  # spent for the 1000-token budget
        if "embedding_cache" in sql:
            return self.cache_rows.get("embedding")
        return None

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        return "INSERT 0 1"


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 1000,
        "rate_limit_tpm": 1_000_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


def _upstream(handler) -> Router:
    r = Router.with_mock(handler)
    r.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1", model_type="embedding")])
    return r


EMB_RESPONSE = {
    "object": "list",
    "model": MODEL,
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
    "usage": {"prompt_tokens": 7, "total_tokens": 7},
}


def _client(monkeypatch: pytest.MonkeyPatch, key: KeyRecord, fake: FakeDB, router: Router) -> TestClient:
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(embedcache, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)  # /v1/usage rate-window + ledger reads
    monkeypatch.setattr(routes_proxy, "model_router", router)
    app = FastAPI()
    app.include_router(routes_proxy.router)
    return TestClient(app, raise_server_exceptions=False)


def _emb_body() -> dict[str, Any]:
    return {"model": MODEL, "input": "hello world"}


# --- embedding cache ---


def test_cache_miss_then_hit_zero_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDB()
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=EMB_RESPONSE)

    tc = _client(monkeypatch, _key(), fake, _upstream(handler))

    r1 = tc.post("/v1/embeddings", json=_emb_body(), headers={"Authorization": "Bearer x"})
    assert r1.status_code == 200 and r1.headers.get("x-cache") is None
    assert calls["n"] == 1
    # miss settled normally
    assert any("usage_ledger" in sql for sql, _ in fake.executed)

    # the store wrote a cache row: wire it into the fake
    for sql, args in fake.executed:
        if "embedding_cache" in sql:
            fake.cache_rows["embedding"] = args[3]
    fake.executed.clear()

    r2 = tc.post("/v1/embeddings", json=_emb_body(), headers={"Authorization": "Bearer x"})
    assert r2.status_code == 200 and r2.headers.get("x-cache") == "HIT"
    assert r2.json()["data"][0]["embedding"] == [0.1, 0.2, 0.3]
    assert calls["n"] == 1  # upstream NOT called again
    # hit path: a cache_hit ledger row (VISIBLE for reporting) that the
    # budget sums EXCLUDE (zero budget consumption)
    hits = [a for sql, a in fake.executed if "usage_ledger" in sql]
    assert hits, "cache hit must be ledgered for visibility"
    assert hits[0][8] == "cache_hit"  # status
    assert hits[0][6] == hits[0][3]  # all input tokens were cache-served


def test_cache_disabled_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.config import settings

    monkeypatch.setattr(settings, "embedding_cache_enabled", False)
    fake = FakeDB(cache_rows={"embedding": [9.9]})
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=EMB_RESPONSE)

    tc = _client(monkeypatch, _key(), fake, _upstream(handler))
    r = tc.post("/v1/embeddings", json=_emb_body(), headers={"Authorization": "Bearer x"})
    assert r.status_code == 200 and calls["n"] == 1  # cache row ignored
    assert r.headers.get("x-cache") is None


def test_cache_key_separator_prevents_collision() -> None:
    assert embedcache.cache_key("ab", "c") != embedcache.cache_key("a", "bc")


# --- /v1/usage self-service ---


def test_usage_endpoint_returns_own_view(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDB()
    tc = _client(monkeypatch, _key(), fake, _upstream(lambda req: httpx2.Response(200, json=EMB_RESPONSE)))
    r = tc.get("/v1/usage", headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    body = r.json()
    assert body["key"]["prefix"] == "hllm-XX"
    assert "budgets" in body and "this_minute" in body and "recent" in body


# --- budget warnings ---


def test_warning_at_80_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDB()
    fake.budget_spent_pct = 85  # 850/1000 tokens spent

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=EMB_RESPONSE)

    tc = _client(monkeypatch, _key(), fake, _upstream(handler))
    r = tc.post("/v1/embeddings", json=_emb_body(), headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.headers.get("x-budget-warning", "").startswith("80%")


def test_warning_at_95_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDB()
    fake.budget_spent_pct = 97

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=EMB_RESPONSE)

    tc = _client(monkeypatch, _key(), fake, _upstream(handler))
    r = tc.post("/v1/embeddings", json=_emb_body(), headers={"Authorization": "Bearer x"})
    assert r.headers.get("x-budget-warning", "").startswith("95%")


def test_no_warning_below_80(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDB()
    fake.budget_spent_pct = 40

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=EMB_RESPONSE)

    tc = _client(monkeypatch, _key(), fake, _upstream(handler))
    r = tc.post("/v1/embeddings", json=_emb_body(), headers={"Authorization": "Bearer x"})
    assert r.headers.get("x-budget-warning") is None


# --- concurrency cap ---


def test_concurrency_cap_429_and_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Second concurrent request 429s; after release the slot frees."""
    fake = FakeDB()
    key = _key(max_concurrent=1)
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=EMB_RESPONSE)

    router = _upstream(handler)
    # concurrency tests exercise the CHAT path: chat-typed pool
    router.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1", model_type="chat")])
    tc = _client(monkeypatch, key, fake, router)

    # simulate one request already in flight
    routes_proxy._in_flight[key.id] = 1
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 429
    assert "concurrency limit" in r.text
    assert calls["n"] == 0  # never reached upstream

    # release → next request admitted; counter returns to 1 during flight
    routes_proxy._in_flight[key.id] = 0
    r2 = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r2.status_code == 200
    assert calls["n"] == 1
    assert key.id not in routes_proxy._in_flight  # released AND popped at zero


def test_concurrency_zero_means_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDB()
    router = _upstream(lambda req: httpx2.Response(200, json=EMB_RESPONSE))
    # chat-typed pool (this test exercises the CHAT path)
    router.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1", model_type="chat")])
    tc = _client(monkeypatch, _key(max_concurrent=0), fake, router)
    routes_proxy._in_flight = {}  # clean slate
    for _ in range(5):
        r = tc.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "x"}]},
            headers={"Authorization": "Bearer x"},
        )
        assert r.status_code == 200
