"""Proxy pipeline integration tests — mocked upstream, mocked ledger.

The full gate chain (body guard → validation → model ACL → budget → rate
limit → pool pick → forward → settle) is exercised against an in-process
httpx2.MockTransport upstream. No live Postgres: the db handles used by
budgets, cascade, costs and the authenticate dependency are monkeypatched
(costs was missed once — an ISOLATED run of this file died with
RuntimeError("database not connected") from settle → costs.compute →
costs._load, while combined runs masked it behind costs' 60s module cache
warmed by earlier test modules).
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
from pcai_llm.routing.router import Endpoint, Router

MODEL = "test-model"


class FakeDB:
    """Budget engine stub: no budgets configured, low window counters."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchrow(self, sql: str, *args: Any):
        # profile_order_for_key lookup: no profile → None (no cascade)
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def execute(self, sql: str, *args: Any):
        self.settled.append(args)
        return "INSERT 0 1"


def _key(allowed_models: list[str] | None = None) -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=allowed_models or [],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Wire fake auth + fake db + mock upstream; return (TestClient, FakeDB)."""
    key = _key()

    import pcai_llm.api.auth as auth_mod

    # Patch the REQUEST-TIME seam: require_key calls authenticate inside
    # pcai_llm.api.auth per request (patching require_scope post-import is a
    # no-op — the dependency closures were frozen at import time).
    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)

    import pcai_llm.api.routes_proxy as rp

    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    # cascade resolution reads profiles via db too — same fake
    import pcai_llm.core.cascade as cascade_mod

    monkeypatch.setattr(cascade_mod, "db", fake_db)
    # cost computation reads its own module-global handle at settle time —
    # patch it with the same fake (FakeDB.fetch returns [] → compute None →
    # the ledger row records cost NULL, which every assertion here allows).
    import pcai_llm.core.costs as costs_mod

    monkeypatch.setattr(costs_mod, "db", fake_db)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx2.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
            },
        )

    # routes_proxy imported the router BY VALUE (`import ... as model_router`),
    # so patch the name the routes' module actually resolves.
    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(
        MODEL,
        [
            Endpoint(
                id="ep1",
                model_name=MODEL,
                base_url="http://fake-upstream/v1",
            )
        ],
    )
    monkeypatch.setattr(rp, "model_router", mock_router)

    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    return tc, fake_db


def test_chat_completions_happy_path_relays_and_settles(env) -> None:
    tc, fake_db = env
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]}
    r = tc.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer hllm-x"})
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    assert "x-request-id" in {k.lower() for k in r.headers}
    assert fake_db.settled, "settle (ledger write) never ran"
    row = fake_db.settled[-1]
    assert row[1] == MODEL and row[2] == "chat"
    assert row[3] == 11 and row[4] == 5  # real usage, not estimate


def test_missing_model_rejected(env) -> None:
    tc, _ = env
    r = tc.post("/v1/chat/completions", json={"messages": []}, headers={"Authorization": "Bearer hllm-x"})
    assert r.status_code == 400


def test_unregistered_model_404(env) -> None:
    tc, _ = env
    r = tc.post(
        "/v1/chat/completions",
        json={"model": "ghost", "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer hllm-x"},
    )
    assert r.status_code == 404


def test_missing_bearer_401(env) -> None:
    """Runs WITHOUT patching require_scope — the real auth dependency 401s."""
    tc, _ = env
    r = tc.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# The Responses API is deliberately NOT implemented (2026-10-11): SGLang
# /v1/responses has array-input validation failures (sglang#17853) and no
# custom function tools (sglang#13292) - a translated relay request would
# break at the model call. The gateway refuses LOUDLY with 501 so an
# Agents-SDK client gets a route-shaped refusal, not a 404.
# ---------------------------------------------------------------------------


def test_responses_api_is_loud_501() -> None:
    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post(
        "/v1/responses",
        json={"model": "whatever", "input": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 501
    body = r.json()
    err = body.get("error") or body.get("detail")
    assert err, body
    assert err.get("code") == "responses_api_unsupported"
    assert "chat/completions" in err.get("message", "")


def test_responses_api_refuses_before_auth() -> None:
    """The 501 fires WITHOUT an API key: the route itself refuses, so the shape
    is identical for every client regardless of credentials."""
    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post("/v1/responses", json={"model": "m", "input": "hi"})
    assert r.status_code == 501
    assert r.json()["detail"]["code"] == "responses_api_unsupported"
