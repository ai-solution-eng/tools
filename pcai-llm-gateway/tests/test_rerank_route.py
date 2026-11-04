"""Rerank proxy route (POST /v1/rerank).

The fleet runs rerankers (Qwen3-VL-Reranker) that the RAG pipeline calls
directly at the upstream. This route puts them behind the same key, gates,
ledger rows and served-model headers as chat and embeddings, forwarding the
body UNCHANGED (query/documents may be strings or multimodal content dicts).

Pinned here:
* a valid rerank request reaches `{base_url}/rerank`, relays the upstream
  JSON verbatim, stamps X-PCAI-Served-Model, and settles ONE ledger row with
  endpoint='rerank' and the upstream's token usage;
* a non-reranker pool is refused 400 (a chat model must not answer /rerank);
* a missing field is a 400, an unregistered model is a 404 - both BEFORE any
  gate or upstream call;
* vLLM reports usage.total_tokens (not prompt_tokens) for rerank; that total
  is the billable prompt count, so the ledger row carries it rather than the
  len//4 estimate.
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
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_fallbacks
from pcai_llm.routing.router import Endpoint, Router

MODEL = "qwen3-vl-reranker"


class FakeDB:
    """Budget/chain stub: no budgets, accumulates settle INSERT args."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetchval(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
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
    """Fake auth + fake db + per-test router. Returns (fake_db, client factory)."""
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
    routes_proxy._in_flight.clear()

    def make(handler, model_type: str = "reranker") -> TestClient:
        r = Router.with_mock(handler)
        r.set_endpoints(
            MODEL,
            [
                Endpoint(
                    id=f"ep-{MODEL}",
                    model_name=MODEL,
                    base_url="http://rerank-upstream/v1",
                    model_type=model_type,
                )
            ],
        )
        monkeypatch.setattr(routes_proxy, "model_router", r)
        app = FastAPI()
        app.include_router(routes_proxy.router)
        app.include_router(routes_proxy.rerank_alias_router)
        return TestClient(app, raise_server_exceptions=False)

    return fake, make


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer hllm-x"}


def _body() -> dict[str, Any]:
    return {
        "model": MODEL,
        "query": "what is the KV cache?",
        "documents": ["a document about attention", "another about caching"],
    }


def test_rerank_relays_and_settles(env) -> None:
    fake, make = env

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/rerank"
        return httpx2.Response(
            200,
            json={
                "id": "r1",
                "model": MODEL,
                "usage": {"total_tokens": 42},
                "results": [
                    {"index": 0, "relevance_score": 0.91},
                    {"index": 1, "relevance_score": 0.12},
                ],
            },
        )

    client = make(handler)
    resp = client.post("/v1/rerank", json=_body(), headers=_h())

    assert resp.status_code == 200
    assert resp.json()["results"][0]["relevance_score"] == 0.91
    assert resp.headers["x-pcai-served-model"] == MODEL
    ok = fake.settles("ok")
    assert len(ok) == 1
    assert ok[0][2] == "rerank"  # endpoint label
    assert ok[0][3] == 42  # prompt tokens from usage.total_tokens


def test_rerank_rejects_a_chat_pool(env) -> None:
    _fake, make = env

    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover
        raise AssertionError("must not reach the upstream")

    client = make(handler, model_type="chat")
    resp = client.post("/v1/rerank", json=_body(), headers=_h())

    assert resp.status_code == 400
    assert "not reranker" in resp.text


def test_rerank_requires_model_query_documents(env) -> None:
    _fake, make = env

    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover
        raise AssertionError("must not reach the upstream")

    client = make(handler)
    body = _body()
    body.pop("documents")
    resp = client.post("/v1/rerank", json=body, headers=_h())

    assert resp.status_code == 400
    assert "documents" in resp.text


def test_rerank_native_path_alias(env) -> None:
    """A client that strips the trailing /v1 from its base_url (the RAG
    pipeline's MultiModalReranker does exactly that) must reach the SAME
    governed handler at POST /rerank - not 404 and fall back to the upstream."""
    fake, make = env

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/rerank"  # upstream is unchanged
        return httpx2.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})

    client = make(handler)
    resp = client.post("/rerank", json=_body(), headers=_h())

    assert resp.status_code == 200
    assert resp.json()["results"][0]["relevance_score"] == 0.5
    assert fake.settles("ok")[0][2] == "rerank"


def test_rerank_unknown_model_is_404(env) -> None:
    _fake, make = env

    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover
        raise AssertionError("must not reach the upstream")

    client = make(handler)
    body = _body()
    body["model"] = "no-such-model"
    resp = client.post("/v1/rerank", json=body, headers=_h())

    assert resp.status_code == 404
