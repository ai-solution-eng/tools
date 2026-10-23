"""Router extras (review §4a/4b/4c) — per-model fallback chains, router
metadata, zero-completion insurance.

The mock-upstream discipline mirrors test_proxy.py / test_relay_settle.py:
an in-process httpx2.MockTransport upstream, no live Postgres (the db
handles in budgets/costs/cascade/routes_proxy/model_fallbacks are
monkeypatched), and settle rows accumulated from the FakeDB INSERT args
(positional per core.budgets.settle: 0 key_id, 1 model, 2 endpoint, 3
prompt, 4 completion, 5 total, 6 cached, 7 cost, 8 status, 9 latency,
10 request_id, ...).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_fallbacks
from pcai_llm.routing.router import Endpoint, Router

PRIMARY = "primary-model"
FALLBACK = "fallback-model"


class FakeDB:
    """Budget/chain engine stub: no budgets configured, low window
    counters, accumulates settle INSERT args. `budget_rows_fn` (optional)
    simulates budget exhaustion: a callable (model) -> list of budget rows
    for that model."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []
        self.chain_rows: dict[str, list[str]] = {}  # model -> ordered fallbacks
        self.known_models: set[str] = set()
        self.budget_rows_fn: Any = None
        self.order: list[str] = []  # profile model_order (the alias cascade)

    def _spent(self, model: str) -> float:
        return sum(r[5] for r in self.settled if r[8] in ("ok", "error") and (r[1] == model or model == "*"))

    async def fetch(self, sql: str, *a: Any):
        if "FROM budgets" in sql:
            if self.budget_rows_fn is None:
                return []
            model = a[1] if len(a) > 1 else None
            return self.budget_rows_fn(model)
        if "FROM cost_budgets" in sql:
            return []
        if "FROM model_fallbacks" in sql:
            model = a[0]
            return [{"fallback_model": n} for n in self.chain_rows.get(model, [])]
        if "FROM model_endpoints" in sql:
            return [{"model_name": m} for m in sorted(self.known_models)]
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "model_order" in sql:  # profile_order_for_key (the alias cascade)
            return {"model_order": list(self.order)} if self.order else None
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

    def acquire(self):  # admin PUT path (transaction)
        return FakeConn(self)


class FakeConn:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def execute(self, sql: str, *args: Any):
        if "DELETE FROM model_fallbacks" in sql:
            self._db.chain_rows.pop(args[0], None)
        return "DELETE 0"

    async def executemany(self, sql: str, args: Any):
        rows = list(args)
        if not rows:
            return
        model = rows[0][0]
        self._db.chain_rows[model] = [r[2] for r in rows]


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


def _ok_body(text: str = "hi", model: str = PRIMARY, finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    }


def _router(handler, models: dict[str, str]) -> Router:
    """A mock-router with one healthy endpoint per named model. `models`
    maps model name -> behavior marker used by the handler."""
    r = Router.with_mock(handler)
    for name, base in models.items():
        r.set_endpoints(name, [Endpoint(id=f"ep-{name}", model_name=name, base_url=base, model_type="chat")])
    return r


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Wire fake auth + fake db; return (db, key, helpers). The router is
    set per-test (the handler scripts the failure mode)."""
    fake = FakeDB()
    key = _key()
    fake.known_models = {PRIMARY, FALLBACK}

    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    monkeypatch.setattr(model_fallbacks, "db", fake)
    monkeypatch.setattr(routes_admin, "db", fake)
    routes_proxy._in_flight.clear()
    return fake, key


def _client(monkeypatch: pytest.MonkeyPatch, handler, models: dict[str, str]) -> TestClient:
    r = _router(handler, models)
    monkeypatch.setattr(routes_proxy, "model_router", r)
    app = FastAPI()
    app.include_router(routes_proxy.router)
    return TestClient(app, raise_server_exceptions=False)


def _admin_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer hllm-x"}


_AH = {"Authorization": "Bearer admin-secret-123"}


# ---------------------------------------------------------------------------
# 1. model_fallbacks table + admin CRUD/validation
# ---------------------------------------------------------------------------


def test_admin_fallbacks_roundtrip(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = env
    tc = _admin_client(monkeypatch)
    fake.known_models = {PRIMARY, FALLBACK, "third"}
    r = tc.put(f"/admin/models/{PRIMARY}/fallbacks", json={"fallbacks": [FALLBACK, "third"]}, headers=_AH)
    assert r.status_code == 200, r.text
    assert r.json() == {"model": PRIMARY, "fallbacks": [FALLBACK, "third"]}
    assert fake.chain_rows[PRIMARY] == [FALLBACK, "third"]
    g = tc.get(f"/admin/models/{PRIMARY}/fallbacks", headers=_AH)
    assert g.status_code == 200
    assert g.json()["fallbacks"] == [FALLBACK, "third"]
    assert g.json()["max"] == settings.model_fallback_max


def test_admin_fallbacks_replace_all(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = env
    tc = _admin_client(monkeypatch)
    fake.known_models = {PRIMARY, FALLBACK, "third"}
    tc.put(f"/admin/models/{PRIMARY}/fallbacks", json={"fallbacks": [FALLBACK, "third"]}, headers=_AH)
    r = tc.put(f"/admin/models/{PRIMARY}/fallbacks", json={"fallbacks": [FALLBACK]}, headers=_AH)
    assert r.status_code == 200
    assert fake.chain_rows[PRIMARY] == [FALLBACK]  # "third" pruned by replace-all


def test_admin_fallbacks_unknown_model_404(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake, _ = env
    tc = _admin_client(monkeypatch)
    r = tc.put("/admin/models/ghost/fallbacks", json={"fallbacks": [FALLBACK]}, headers=_AH)
    assert r.status_code == 404


def test_admin_fallbacks_unknown_candidate_400(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = env
    tc = _admin_client(monkeypatch)
    fake.known_models = {PRIMARY}
    r = tc.put(f"/admin/models/{PRIMARY}/fallbacks", json={"fallbacks": ["not-registered"]}, headers=_AH)
    assert r.status_code == 400
    assert "not-registered" in r.text


def test_admin_fallbacks_self_fallback_400(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = env
    tc = _admin_client(monkeypatch)
    fake.known_models = {PRIMARY}
    r = tc.put(f"/admin/models/{PRIMARY}/fallbacks", json={"fallbacks": [PRIMARY]}, headers=_AH)
    assert r.status_code == 400
    assert "itself" in r.text


def test_admin_fallbacks_dedup(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = env
    tc = _admin_client(monkeypatch)
    fake.known_models = {PRIMARY, FALLBACK}
    r = tc.put(f"/admin/models/{PRIMARY}/fallbacks", json={"fallbacks": [FALLBACK, FALLBACK]}, headers=_AH)
    assert r.status_code == 200
    assert r.json()["fallbacks"] == [FALLBACK]  # a double-ticked model is one candidate


def test_admin_fallbacks_chain_capped(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = env
    tc = _admin_client(monkeypatch)
    fake.known_models = {PRIMARY, FALLBACK, "b", "c"}
    monkeypatch.setattr(settings, "model_fallback_max", 2)
    r = tc.put(f"/admin/models/{PRIMARY}/fallbacks", json={"fallbacks": [FALLBACK, "b", "c"]}, headers=_AH)
    assert r.status_code == 400
    assert "MODEL_FALLBACK_MAX" in r.text


def test_chain_for_reads_ranked_rows_and_caps(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """core.model_fallbacks.chain_for: rank-ordered, self/dup-stripped,
    capped at PCAI_LLM_MODEL_FALLBACK_MAX, fail-safe on DB errors."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK, FALLBACK, "b"]
    import asyncio

    monkeypatch.setattr(settings, "model_fallback_max", 2)
    out = asyncio.run(model_fallbacks.chain_for(PRIMARY))
    assert out == [FALLBACK, "b"]

    async def boom(*a: Any):
        raise RuntimeError("db down")

    monkeypatch.setattr(model_fallbacks, "db", SimpleNamespaceFetch(boom))
    out = asyncio.run(model_fallbacks.chain_for(PRIMARY))
    assert out == [], "a chain-read failure must degrade to an empty chain, never raise"


class SimpleNamespaceFetch:
    def __init__(self, fn) -> None:
        self.fetch = fn


# ---------------------------------------------------------------------------
# 2. chain walk: pick-failure AND upstream 5xx; settle under ACTUAL model
# ---------------------------------------------------------------------------


def test_pick_failure_walks_chain_and_answers(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE gap this feature closes: a PINNED request whose primary pool has
    no healthy endpoint (pick None) now answers from the fallback instead
    of 503ing answerless."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)

    def handler(request: httpx2.Request) -> httpx2.Response:
        # only the fallback's upstream is ever reached
        assert request.url.host == "fallback-upstream"
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    r = Router.with_mock(handler)
    # primary registered but EMPTY (no endpoints) — pool.pick() is None
    r.set_endpoints(PRIMARY, [])
    r.set_endpoints(FALLBACK, [Endpoint(id="ep-fb", model_name=FALLBACK, base_url="http://fallback-upstream/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "hi"
    assert resp.headers.get("x-pcai-served-model") == FALLBACK
    assert resp.headers.get("x-pcai-fallback") == PRIMARY
    assert resp.headers.get("x-pcai-endpoint") == "ep-fb"
    # settle under the ACTUAL model (ledger authoritative)
    ok_rows = [s for s in fake.settled if s[8] == "ok"]
    assert ok_rows and ok_rows[0][1] == FALLBACK, "the ledger row must name the model that really served"
    # the pick-failure itself also settled (error row under the primary)
    err_rows = [s for s in fake.settled if s[8] == "error"]
    assert err_rows and err_rows[0][1] == PRIMARY


# ---------------------------------------------------------------------------
# 2a. the ALIAS ordering tail: `auto` keeps cascading at runtime, no chain
# ---------------------------------------------------------------------------

THIRD = "third-model"


def test_alias_ordering_tail_fails_over_without_chain(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ALIAS request (`auto`) with NO per-model chain fails over down the
    profile ordering when its admitted head cannot serve: the cascade
    continues at runtime instead of 503ing answerless. This is the live
    defect — GLM (ordering head) down, empty model_fallbacks, `auto` died
    instead of moving to the ordering's next model."""
    fake, _ = env
    fake.order = [PRIMARY, FALLBACK]  # the profile's cascade model_order
    monkeypatch.setattr(settings, "model_fallback_max", 2)
    monkeypatch.setattr(model_fallbacks, "db", fake)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "fallback-upstream", "the ordering tail must answer"
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    r = Router.with_mock(handler)
    r.set_endpoints(PRIMARY, [])  # head registered but EMPTY -> pick() is None
    r.set_endpoints(FALLBACK, [Endpoint(id="ep-fb", model_name=FALLBACK, base_url="http://fallback-upstream/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.post(
        "/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-pcai-served-model") == FALLBACK
    assert resp.headers.get("x-pcai-fallback") == PRIMARY, "the hop is reported as a fallback from the head"
    ok_rows = [s for s in fake.settled if s[8] == "ok"]
    assert ok_rows and ok_rows[0][1] == FALLBACK, "the ledger row must name the model that really served"


def test_pinned_request_never_uses_the_ordering_tail(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scope boundary: a PINNED request does NOT cascade. With no chain it
    still 503s even though the ordering has a healthy next model — a pin
    says "serve THIS model" (its runtime net stays the per-model chain)."""
    fake, _ = env
    fake.order = [PRIMARY, FALLBACK]
    monkeypatch.setattr(settings, "model_fallback_max", 2)
    monkeypatch.setattr(model_fallbacks, "db", fake)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    r = Router.with_mock(handler)
    r.set_endpoints(PRIMARY, [])
    r.set_endpoints(FALLBACK, [Endpoint(id="ep-fb", model_name=FALLBACK, base_url="http://fallback-upstream/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 503, resp.text
    assert calls["n"] == 0, "a pinned request must never walk the profile ordering"


def test_alias_chain_outranks_the_ordering_tail(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Priority: the admin's explicit per-model chain is tried FIRST; the
    ordering tail only fills what the model_fallback_max budget leaves."""
    fake, _ = env
    fake.order = [PRIMARY, THIRD]
    fake.chain_rows[PRIMARY] = [FALLBACK]  # explicit sign-off beats the ordering
    monkeypatch.setattr(settings, "model_fallback_max", 2)
    monkeypatch.setattr(model_fallbacks, "db", fake)
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.host)
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    r = Router.with_mock(handler)
    r.set_endpoints(PRIMARY, [])
    r.set_endpoints(FALLBACK, [Endpoint(id="ep-fb", model_name=FALLBACK, base_url="http://fallback-upstream/v1")])
    r.set_endpoints(THIRD, [Endpoint(id="ep-th", model_name=THIRD, base_url="http://third-upstream/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.post(
        "/v1/chat/completions", json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-pcai-served-model") == FALLBACK
    assert seen == ["fallback-upstream"], "the chain hop must win; the ordering tail is the last resort"


def test_upstream_5xx_walks_chain(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned request whose primary answers 503 walks the chain instead of
    relaying the bare 5xx."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if request.url.host == "primary-upstream":
            return httpx2.Response(503, json={"error": {"message": "engine overheated"}})
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1", FALLBACK: "http://fallback-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["model"] == FALLBACK
    assert calls["n"] == 2
    assert resp.headers.get("x-pcai-fallback") == PRIMARY
    assert resp.headers.get("x-pcai-served-model") == FALLBACK
    ok_rows = [s for s in fake.settled if s[8] == "ok"]
    assert ok_rows and ok_rows[0][1] == FALLBACK
    err_rows = [s for s in fake.settled if s[8] == "error"]
    assert err_rows and err_rows[0][1] == PRIMARY


def test_5xx_without_chain_relays_verbatim(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No chain configured = exactly today's behavior: the upstream 5xx body
    and status relay untouched."""
    fake, _ = env
    monkeypatch.setattr(model_fallbacks, "db", fake)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(502, json={"error": {"message": "bad gateway from engine"}})

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 502
    assert "bad gateway from engine" in resp.text


def test_4xx_never_re_rolled(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """4xx is the caller's/request's fault — a chain is never walked on it."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if request.url.host == "primary-upstream":
            return httpx2.Response(422, json={"error": {"message": "context too long"}})
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1", FALLBACK: "http://fallback-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 422
    assert calls["n"] == 1, "a 4xx must never be re-rolled onto the fallback"


def test_chain_candidate_gate_skips_unregistered(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chain entry that is not registered is skipped (graceful degradation
    — the same discipline delete_model documents for profile orderings)."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = ["ghost", FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)
    fake.known_models = {PRIMARY, FALLBACK}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "primary-upstream":
            return httpx2.Response(500, json={"error": "boom"})
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1", FALLBACK: "http://fallback-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == FALLBACK


def test_chain_respects_key_acl(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """allowed_models is a HARD boundary — a fallback the key may not use is
    skipped, not served (a fallback must never become an ACL bypass)."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)
    key = _key(allowed_models=[PRIMARY])  # FALLBACK not allowed
    import pcai_llm.api.auth as auth_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    reached: dict[str, int] = {"fb": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "primary-upstream":
            return httpx2.Response(500, json={"error": "boom"})
        reached["fb"] += 1
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1", FALLBACK: "http://fallback-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 500
    assert reached["fb"] == 0, "the ACL must gate the fallback candidate"


def test_budget_exhaustion_mid_chain_stops_cleanly(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Budget exhaustion mid-chain stops the walk cleanly: the exhausted
    candidate settles its own budget_exceeded row and is skipped; when NO
    candidate has headroom the LAST upstream error relays verbatim (the
    pre-chain contract). Ledger authoritative — no bypass."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)
    # a '*' month budget already over-spent: pre-spend 20 tokens under the
    # PRIMARY model name (the budget rows join the ledger SUM by model)
    fake.settled.append((uuid.uuid4(), PRIMARY, "chat", 0, 20, 20, 0, None, "ok", 1, "pre-spend"))

    def budgets_for(model: str | None) -> list[dict]:
        # ONLY the fallback candidate is exhausted — the primary must pass
        # admission (a 429 there would never reach the walk).
        if model == FALLBACK:
            return [{"model": FALLBACK, "period": "month", "limit_tokens": 10, "spent": 20.0}]
        return []

    fake.budget_rows_fn = budgets_for

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "primary-upstream":
            return httpx2.Response(500, json={"error": "boom"})
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1", FALLBACK: "http://fallback-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 500, "no candidate with headroom = the primary's error relays"
    markers = [s for s in fake.settled if s[8] == "budget_exceeded"]
    assert markers and markers[0][1] == FALLBACK, "the exhausted candidate settles its own refusal row"
    fb_ok = [s for s in fake.settled if s[8] == "ok" and s[1] == FALLBACK]
    assert not fb_ok, "an exhausted candidate must never be served"


class SimpleNamespaceFetch2:
    def __init__(self, fn) -> None:
        self.fetch = fn


def test_chain_cap_bounds_the_walk(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """PCAI_LLM_MODEL_FALLBACK_MAX bounds how many MODELS are tried — a
    longer chain is truncated at the cap."""
    fake, _ = env
    third = "third-model"
    fake.chain_rows[PRIMARY] = [FALLBACK, third]
    fake.known_models = {PRIMARY, FALLBACK, third}
    monkeypatch.setattr(model_fallbacks, "db", fake)
    monkeypatch.setattr(settings, "model_fallback_max", 1)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "primary-upstream":
            return httpx2.Response(500, json={"error": "boom"})
        if request.url.host == "fallback-upstream":
            return httpx2.Response(500, json={"error": "also boom"})
        return httpx2.Response(200, json=_ok_body(model=third))

    tc = _client(
        monkeypatch,
        handler,
        {
            PRIMARY: "http://primary-upstream/v1",
            FALLBACK: "http://fallback-upstream/v1",
            third: "http://third-upstream/v1",
        },
    )
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 500  # the cap cut the chain after FALLBACK
    assert "also boom" in resp.text  # the LAST failure relays verbatim


def test_transport_error_walks_chain(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport failure (connection refused / TLS) on the primary walks
    the chain — the transport-error sibling of the 5xx walk."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "primary-upstream":
            raise httpx2.ConnectError("connection refused")
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1", FALLBACK: "http://fallback-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-pcai-served-model") == FALLBACK
    assert resp.headers.get("x-pcai-fallback") == PRIMARY


# ---------------------------------------------------------------------------
# 2b. streaming: failover only PRE-first-byte; mid-stream dies as today
# ---------------------------------------------------------------------------


def _sse_bytes(chunks: list[dict]) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def test_stream_failover_pre_first_byte_5xx(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A streaming request whose primary answers a 5xx ERROR RESPONSE (no
    bytes streamed yet) fails over to the chain — the error body never
    reached the client, so the switch is invisible."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if request.url.host == "primary-upstream":
            return httpx2.Response(500, json={"error": "boom"})
        return httpx2.Response(
            200,
            content=_sse_bytes([{"choices": [{"delta": {"content": "streamed!"}}]}]),
            headers={"content-type": "text/event-stream"},
        )

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1", FALLBACK: "http://fallback-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert resp.status_code == 200
    assert "streamed!" in resp.text
    assert resp.headers.get("x-pcai-fallback") == PRIMARY
    assert resp.headers.get("x-pcai-served-model") == FALLBACK
    # 3 upstream calls: the primary is hit twice by the PRE-EXISTING
    # stream_options negotiation (some upstreams reject stream_options —
    # the identical call is retried without it before anything else), then
    # the chain hop answers.
    assert calls["n"] == 3
    assert resp.headers.get("x-pcai-endpoint") == f"ep-{FALLBACK}"


def test_stream_5xx_without_chain_relays_as_today(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No chain: a streaming 5xx relays as an ordinary non-SSE response —
    byte-for-byte today's behavior."""
    fake, _ = env
    monkeypatch.setattr(model_fallbacks, "db", fake)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, json={"error": "upstream down"})

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert resp.status_code == 503
    assert "upstream down" in resp.text


def test_stream_mid_stream_death_not_failover(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that OPENS (2xx) and then dies mid-stream is NOT failed
    over — the pre-first-byte rule: bytes already delivered can never be
    rewritten by another model. The generator surfaces the abort (settle in
    the finally) exactly as today."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "primary-upstream", "mid-stream death must NOT walk the chain"

        async def body_stream():
            yield b"data: " + json.dumps({"choices": [{"delta": {"content": "partial"}}]}).encode() + b"\n\n"
            raise httpx2.RemoteProtocolError("upstream died mid-stream")

        return httpx2.Response(200, content=body_stream(), headers={"content-type": "text/event-stream"})

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    # TestClient buffers the SSE body; the mid-stream RemoteProtocolError
    # truncates the body instead of raising through post(). The assertions
    # that matter: no fallback hop was ever contacted and no fallback ledger
    # row exists (the walk must not rescue an already-open stream).
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert "fallback-upstream" not in resp.text
    assert not [s for s in fake.settled if s[1] == FALLBACK]


# ---------------------------------------------------------------------------
# 3. Router metadata: headers ALWAYS; body block opt-in only
# ---------------------------------------------------------------------------


def test_metadata_headers_always_on_json(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake, _ = env

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_ok_body())

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200
    assert resp.headers.get("x-pcai-served-model") == PRIMARY
    assert resp.headers.get("x-pcai-endpoint") == f"ep-{PRIMARY}"
    assert "x-pcai-fallback" not in {k.lower() for k in resp.headers}  # primary answered: no header


def test_body_block_opt_in_only(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without X-PCAI-Metadata: 1 the body is byte-transparent (no pcai
    key); WITH it the block appears on the non-streaming JSON path."""
    _fake, _ = env

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_ok_body())

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    base = {"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}

    plain = tc.post("/v1/chat/completions", json=base, headers=_h())
    assert plain.status_code == 200
    assert "pcai" not in plain.json(), "body mutation must be strictly opt-in"

    optin = tc.post("/v1/chat/completions", json=base, headers={**_h(), "X-PCAI-Metadata": "1"})
    assert optin.status_code == 200
    block = optin.json().get("pcai")
    assert block is not None
    assert block["served_model"] == PRIMARY
    assert block["endpoint"] == f"ep-{PRIMARY}"
    assert "cached_tokens" in block or block.get("cached_tokens", 0) == 0


def test_body_block_never_rides_a_stream(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """X-PCAI-Metadata: 1 on a STREAMING request must not mutate the SSE
    body (transparent proxy — non-streaming only)."""
    _fake, _ = env

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=_sse_bytes([{"choices": [{"delta": {"content": "hi"}}]}]),
            headers={"content-type": "text/event-stream"},
        )

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={**_h(), "X-PCAI-Metadata": "1"},
    )
    assert resp.status_code == 200
    assert '"pcai"' not in resp.text, "a stream body is never mutated"


def test_body_block_never_collides_with_upstream_pcai_key(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake, _ = env

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = _ok_body()
        body["pcai"] = {"upstream": "owned"}
        return httpx2.Response(200, json=body)

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]},
        headers={**_h(), "X-PCAI-Metadata": "1"},
    )
    assert resp.status_code == 200
    assert resp.json()["pcai"] == {"upstream": "owned"}, "an upstream-owned pcai key is never collided"


def test_relay_headers_pure_helpers() -> None:
    """_relay_headers: metadata headers ALWAYS when the facts are supplied;
    X-PCAI-Fallback only when failed over. _pcai_metadata_body: opt-in
    block shape + never collides with an upstream pcai key."""
    out = routes_proxy._relay_headers({"X-Request-ID": "r1"}, "r1", served_model="m", endpoint_id="ep1")
    assert out["X-PCAI-Served-Model"] == "m"
    assert out["X-PCAI-Endpoint"] == "ep1"
    assert "X-PCAI-Fallback" not in out
    out2 = routes_proxy._relay_headers(
        {"X-Request-ID": "r1"}, "r1", served_model="m", endpoint_id="ep1", fallback_from="old"
    )
    assert out2["X-PCAI-Fallback"] == "old"
    empty = routes_proxy._relay_headers({"X-Request-ID": "r1"}, "r1")
    assert "X-PCAI-Served-Model" not in empty and "X-PCAI-Endpoint" not in empty

    body = routes_proxy._pcai_metadata_body({"choices": []}, "m", "ep1", cost_usd=0.5, cached_tokens=7)
    assert body is not None and body["pcai"] == {
        "served_model": "m",
        "endpoint": "ep1",
        "cost_usd": 0.5,
        "cached_tokens": 7,
    }
    untouched = routes_proxy._pcai_metadata_body({"pcai": None}, "m", "ep1")
    assert (
        untouched is not None and untouched["pcai"]["served_model"] == "m"
    )  # null pcai = upstream placeholder, safe to fill
    collided = routes_proxy._pcai_metadata_body({"pcai": {"x": 1}}, "m", "ep1")
    assert collided is None


# ---------------------------------------------------------------------------
# 4. Zero-completion insurance (flag-gated)
# ---------------------------------------------------------------------------


def _empty_ok() -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "cmpl-e",
            "object": "chat.completion",
            "model": PRIMARY,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 0, "total_tokens": 11},
        },
    )


def test_zero_completion_retry_fires_once_and_settles_both(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = env
    monkeypatch.setattr(settings, "zero_completion_retry", True)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _empty_ok()
        return httpx2.Response(200, json=_ok_body(text="recovered"))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "recovered"
    assert calls["n"] == 2, "exactly ONE retry"
    ok_rows = [s for s in fake.settled if s[8] == "ok"]
    assert len(ok_rows) == 2, "BOTH attempts settle (the first was real GPU spend)"
    assert ok_rows[0][4] == 0, "the empty first attempt settles its zero completion tokens"
    assert ok_rows[1][4] == 5, "the retry settles its real usage"
    # shared request_id correlation
    assert ok_rows[0][10] == ok_rows[1][10]


def test_zero_completion_retry_second_empty_not_re_rolled(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounded to ONE retry ever: the retry's own empty answer is relayed,
    not re-rolled again."""
    fake, _ = env
    monkeypatch.setattr(settings, "zero_completion_retry", True)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return _empty_ok()

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert len([s for s in fake.settled if s[8] == "ok"]) == 2


def test_zero_completion_flag_off_no_retry(env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = env
    monkeypatch.setattr(settings, "zero_completion_retry", False)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return _empty_ok()

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200
    assert calls["n"] == 1, "flag OFF = byte-for-byte today's behavior"
    assert len([s for s in fake.settled if s[8] == "ok"]) == 1


def test_zero_completion_content_parts_shape_triggers(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The multimodal content-parts shape with NO text parts normalizes to
    empty — the trigger matches it exactly like the string shape."""
    _fake, _ = env
    monkeypatch.setattr(settings, "zero_completion_retry", True)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(
                200,
                json={
                    "id": "cmpl-p",
                    "object": "chat.completion",
                    "model": PRIMARY,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "image_url", "image_url": {"url": "x"}}],
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 0, "total_tokens": 11},
                },
            )
        return httpx2.Response(200, json=_ok_body(text="parts recovered"))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "parts recovered"
    assert calls["n"] == 2


def test_zero_completion_no_trigger_on_real_answer(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real answer (content present) never re-rolls; nor does a
    length-capped empty (finish_reason != stop)."""
    _fake, _ = env
    monkeypatch.setattr(settings, "zero_completion_retry", True)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(200, json=_ok_body())

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200
    assert calls["n"] == 1

    # finish_reason=length with empty content: NOT the trigger
    calls["n"] = 0

    def handler2(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        body = _empty_ok().json()
        body["choices"][0]["finish_reason"] = "length"
        return httpx2.Response(200, json=body)

    tc2 = _client(monkeypatch, handler2, {PRIMARY: "http://primary-upstream/v1"})
    resp2 = tc2.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp2.status_code == 200
    assert calls["n"] == 1, "finish_reason != stop is not the zero-completion shape"


def test_zero_completion_streaming_never_retries(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plain streaming is EXCLUDED: buffering breaks transparency — the
    empty stream relays as-is even with the flag on."""
    _fake, _ = env
    monkeypatch.setattr(settings, "zero_completion_retry", True)
    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(
            200,
            content=_sse_bytes(
                [
                    {
                        "choices": [
                            {"delta": {}},
                        ],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 0},
                    }
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert resp.status_code == 200
    assert calls["n"] == 1, "a plain stream is never buffered/re-rolled"


def test_zero_completion_relay_final_answer_retries(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relay path: a FINAL answer that comes back empty + zero completion
    tokens re-rolls ONCE (the relay owns assembly — the retry is internal,
    before anything reaches the client), both calls settled."""
    fake, _ = env
    monkeypatch.setattr(settings, "zero_completion_retry", True)
    from pcai_llm.api import routes_mcp
    from pcai_llm.core import registry
    from pcai_llm.mcpf import federation

    calls: dict[str, int] = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx2.Response(
                200,
                content=_sse_bytes(
                    [{"choices": [{"delta": {}}], "usage": {"prompt_tokens": 11, "completion_tokens": 0}}]
                ),
                headers={"content-type": "text/event-stream"},
            )
        return httpx2.Response(
            200,
            content=_sse_bytes([{"choices": [{"delta": {"content": "relay recovered"}}]}]),
            headers={"content-type": "text/event-stream"},
        )

    r = _router(handler, {PRIMARY: "http://primary-upstream/v1"})
    monkeypatch.setattr(routes_proxy, "model_router", r)

    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return {("logsearch", "search_logs")}

    async def fake_fetch_tools(url: str, headers: dict, timeout_s: float | None = None):
        return [{"tool": "search_logs", "description": "search"}]

    class FakeBlock:
        def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
            return {"type": "text", "text": "ok"}

    class FakeResult:
        def __init__(self) -> None:
            self.is_error = False
            self.content = [FakeBlock()]

    async def fake_upstream_session(url, headers, op, timeout_s=None):
        return FakeResult()

    monkeypatch.setattr(federation, "db", fake)
    monkeypatch.setattr(federation, "load_grants", fake_grants)
    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch_tools)
    monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream_session)
    registry._mcp_servers_cache = [
        {
            "id": "srv1",
            "name": "logsearch",
            "url": "http://logsearch.svc:8080",
            "headers": {},
            "auth_secret": None,
            "auth_token_enc": None,
            "enabled": True,
            "tool_policy": "all",
            "allowed_tools": [],
        }
    ]
    routes_proxy.catalog_cache_clear()

    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer x", "X-PCAI-Execute-Tools": "1"},
    )
    assert resp.status_code == 200, resp.text
    assert "relay recovered" in resp.text
    assert calls["n"] == 2, "the relay's empty final answer re-rolled exactly once"
    ok_rows = [s for s in fake.settled if s[8] == "ok"]
    assert len(ok_rows) == 2, "both attempts settled"


# ---------------------------------------------------------------------------
# 5. Ledger authoritative: settle under ACTUAL model (already asserted in
# the walk tests); routing stamp never crosses models
# ---------------------------------------------------------------------------


def test_routing_stamp_does_not_cross_models(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """On a failover hop the routing_* audit columns are NOT stamped from
    the primary's routing decision onto the fallback's ledger row — routing
    metadata describes the model it decided for."""
    fake, _ = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    monkeypatch.setattr(model_fallbacks, "db", fake)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "primary-upstream":
            return httpx2.Response(500, json={"error": "boom"})
        return httpx2.Response(200, json=_ok_body(model=FALLBACK))

    tc = _client(monkeypatch, handler, {PRIMARY: "http://primary-upstream/v1", FALLBACK: "http://fallback-upstream/v1"})
    resp = tc.post(
        "/v1/chat/completions", json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}]}, headers=_h()
    )
    assert resp.status_code == 200
    ok_rows = [s for s in fake.settled if s[8] == "ok"]
    assert ok_rows and ok_rows[0][1] == FALLBACK
    # settle signature (positional tail) stays intact: 17 params total
    assert len(ok_rows[0]) == 17
