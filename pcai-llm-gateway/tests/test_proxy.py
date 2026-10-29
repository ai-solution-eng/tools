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

import json
import time
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



# ---------------------------------------------------------------------------
# audit wave 2026-10-29 (zone 3b) regressions — loop guard, SSE framing,
# model-name rewrite, relay loop hygiene, canonical capture guard
# ---------------------------------------------------------------------------


def test_zc9_loop_guard_refusal_reports_the_real_hit_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit ZC-9: the refusal used to say "made {len(hits) + 1}x" — on the
    block (5 executed attempts) the model was told "made 6x". The blocked
    call is not one of the made ones."""
    monkeypatch.setattr(routes_proxy, "_loop_guard", {})
    kid = "key-zc9"
    args = {"q": "same"}
    for _ in range(routes_proxy._LOOP_GUARD_MAX_HITS):
        assert routes_proxy.loop_guard_check(kid, "logsearch", "search_logs", args) is None
    reason = routes_proxy.loop_guard_check(kid, "logsearch", "search_logs", args)
    assert reason is not None
    assert f"made {routes_proxy._LOOP_GUARD_MAX_HITS}x" in reason, reason
    assert "made 6x" not in reason


def test_c2_loop_guard_keeps_a_blocked_loop_blocked_under_a_distinct_call_flood(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit C-2: eviction used to pop the OLDEST INSERTION, which is the
    hottest blocked fingerprint (blocked hits were never recorded, so its
    position froze at first insertion). 5000 distinct calls re-armed a
    blocked identical-call loop, one eviction at a time. Eviction now
    prefers below-threshold entries and records blocked attempts as
    tombstones, so the active block survives the flood — and a fingerprint
    whose window empties re-arms naturally."""
    import time as _time

    monkeypatch.setattr(routes_proxy, "_loop_guard", {})
    kid = "key-c2"
    args = {"c": "echo hi"}
    for _ in range(routes_proxy._LOOP_GUARD_MAX_HITS):
        assert routes_proxy.loop_guard_check(kid, "bash", "run", args) is None
    assert routes_proxy.loop_guard_check(kid, "bash", "run", args) is not None, "precondition: blocked"
    hot_fp = (kid, "bash", "run", '{"c": "echo hi"}')

    for i in range(routes_proxy._LOOP_GUARD_MAX_ENTRIES):
        assert routes_proxy.loop_guard_check(kid + "-flood", "searxng", "fetch", {"u": str(i)}) is None

    assert len(routes_proxy._loop_guard) <= routes_proxy._LOOP_GUARD_MAX_ENTRIES, "cap still holds"
    assert hot_fp in routes_proxy._loop_guard, "the ACTIVE blocked fingerprint was evicted by the flood"
    assert routes_proxy.loop_guard_check(kid, "bash", "run", args) is not None, "flood re-armed the blocked loop"

    # once the window empties (aged entries pruned), the fingerprint re-arms
    monkeypatch.setattr(
        routes_proxy,
        "_loop_guard",
        {hot_fp: [_time.monotonic() - routes_proxy._LOOP_GUARD_WINDOW_S - 1] * 6},
    )
    assert routes_proxy.loop_guard_check(kid, "bash", "run", args) is None, "expired entries must re-arm"


async def test_c7_cr_only_framing_parses_in_the_proxy_scan() -> None:
    """Audit C-7 (proxy twin): the ledger line cursor split on LF only, so a
    CR-only (spec-legal, WHATWG) upstream parsed as ONE line — no usage, no
    [DONE], an empty-but-successful stream."""

    class FakeResp:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        async def aiter_bytes(self):
            for c in self._chunks:
                yield c

        async def aclose(self) -> None:
            return None

    fed: list[bytes] = []

    class FakeLedger:
        completed = False

        def feed(self, line: bytes) -> None:
            fed.append(line)

        async def finish(self) -> None:
            return None

    cr_stream = [
        b'data: {"id":"c","choices":[{"delta":{"content":"hi"}}]}\r',
        b"\r",
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\r',
        b"\r",
        b"data: [DONE]\r",
    ]
    gen = routes_proxy._relay_sse(FakeResp(cr_stream), FakeLedger())
    out = b"".join([chunk async for chunk in gen])
    assert out == b"".join(cr_stream), "pass-through bytes stay byte-identical"
    data_lines = [ln for ln in fed if ln.startswith(b"data:")]
    assert len(data_lines) == 3, f"CR-framed lines must parse individually: {data_lines!r}"
    assert any(b"[DONE]" in ln for ln in fed), "[DONE] must be seen by the ledger scan"
    assert any(b"total_tokens" in ln for ln in fed), "the usage line must reach the settle scan"


async def test_c7_peek_gate_recognises_a_cr_framed_data_line() -> None:
    """Audit C-7 (peek twin): the TTFT gate's data-line scan also split on
    LF only, so a CR-only stream waited out the whole failover budget before
    failing over (and the bytes below never counted as progress)."""

    class FakeResp:
        async def aiter_bytes(self):
            yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\r'
            yield b"\r"
            yield b"data: [DONE]\r"

        async def aclose(self) -> None:
            return None

    peeked, chunks = await routes_proxy._peek_first_data_line(FakeResp(), budget_s=2.0)
    assert peeked.startswith(b'data: {"choices"'), f"first data line must be recognized: {peeked!r}"
    assert chunks is not None


async def test_c6_fallback_hop_rewrites_the_upstream_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit C-6 (non-streaming path): body["model"] kept the CLIENT's name
    on every fallback hop, so the wire asked for the model that just failed
    while the ledger and X-PCAI-Served-Model named the fallback — a strict
    upstream 400s the hop AFTER its chain entry was consumed."""
    from pcai_llm.core import budgets as budgets_mod

    class FakeDB:
        async def fetch(self, sql: str, *args: Any):
            return []

        async def fetchrow(self, sql: str, *args: Any):
            if "profiles" in sql:
                return None
            if "rate_windows" in sql:
                return {"requests": 1, "tokens": 10}
            return None

        async def execute(self, sql: str, *args: Any):
            return "INSERT 0 1"

    monkeypatch.setattr(budgets_mod, "db", FakeDB())

    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if body["model"] == MODEL:
            return httpx2.Response(500, json={"error": "boom"})
        return httpx2.Response(
            200,
            json={
                "id": "cmpl-fb",
                "object": "chat.completion",
                "model": "fb-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            },
        )

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1")])
    mock_router.set_endpoints("fb-model", [Endpoint(id="ep2", model_name="fb-model", base_url="http://fake/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    key = _key()
    pool = mock_router.pool(MODEL)
    ep = pool.pick()
    resp = await routes_proxy._proxy_json(
        pool,
        ep,
        "http://fake/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]},
        {},
        key,
        MODEL,
        "chat",
        10,
        "req-c6",
        time.monotonic(),
        fallback_chain=["fb-model"],
    )
    assert resp.status_code == 200
    assert [b["model"] for b in bodies] == [MODEL, "fb-model"], (
        f"every hop must name ITS model upstream: {[b['model'] for b in bodies]}"
    )
    assert resp.headers.get("x-pcai-fallback") == MODEL


async def test_c6_stream_fallback_hop_rewrites_the_upstream_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit C-6 (streaming path): _stream_chat's attempt loop is the TTFT /
    5xx failover walk — every attempt must name its own model upstream."""
    from pcai_llm.core import budgets as budgets_mod

    class FakeDB:
        async def fetch(self, sql: str, *args: Any):
            return []

        async def fetchrow(self, sql: str, *args: Any):
            if "profiles" in sql:
                return None
            if "rate_windows" in sql:
                return {"requests": 1, "tokens": 10}
            return None

        async def execute(self, sql: str, *args: Any):
            return "INSERT 0 1"

    monkeypatch.setattr(budgets_mod, "db", FakeDB())

    bodies: list[dict] = []
    sse = (
        b'data: {"id":"c","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        b'data: {"id":"c","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if body["model"] == MODEL:
            return httpx2.Response(500, json={"error": "boom"})
        return httpx2.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1")])
    mock_router.set_endpoints("fb-model", [Endpoint(id="ep2", model_name="fb-model", base_url="http://fake/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    key = _key()
    pool = mock_router.pool(MODEL)
    ep = pool.pick()
    resp = await routes_proxy._stream_chat(
        pool,
        ep,
        "http://fake/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "stream": True},
        {},
        key,
        MODEL,
        10,
        "req-c6s",
        time.monotonic(),
        fallback_chain=["fb-model"],
    )
    payload = b"".join([chunk async for chunk in resp.body_iterator])
    assert b"[DONE]" in payload and b'"content":"hi"' in payload.replace(b" ", b"")
    # the failover walk may re-try the primary without stream_options (that
    # pre-existing 4xx retry is not this finding); the INVARIANT is: every
    # attempt names ITS model, so the fallback attempt is distinguishable on
    # the wire — pre-fix, the fb hop still carried the client's name and got
    # 500'd by this handler (no [DONE] payload at all).
    models = [b["model"] for b in bodies]
    assert set(models) == {MODEL, "fb-model"}, f"unexpected models on the wire: {models}"
    assert models[-1] == "fb-model", f"the serving fallback hop must name fb-model: {models}"
    assert resp.headers.get("x-pcai-fallback") == MODEL



# ---------------------------------------------------------------------------
# relay-turn tests (audit wave 2026-10-29): tool_choice hygiene, iteration
# histogram, canonical capture guard, handback flush boundary
# ---------------------------------------------------------------------------


class _RelayFakeDB(FakeDB):
    """FakeDB + canonical-store reads answered as absent (the reconcile path
    then takes its documented 'new' mode and writes through execute())."""

    async def fetchrow(self, sql: str, *args: Any):
        if "canonical_conversations" in sql:
            return None
        return await super().fetchrow(sql, *args)


def _tool_call_sse(name: str = "logsearch__search_logs") -> bytes:
    return (
        b'data: {"id":"c1","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call1",'
        b'"type":"function","function":{"name":"' + name.encode() + b'","arguments":"{}"}}]}}]}\n\n'
        b'data: {"id":"c1","choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":1,"total_tokens":8}}\n\n'
        b"data: [DONE]\n\n"
    )


def _content_sse(text: str) -> bytes:
    return (
        b'data: {"id":"c2","choices":[{"delta":{"content":"'
        + text.encode()
        + b'"},"finish_reason":null}]}\n\n'
        b'data: {"id":"c2","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
        b"data: [DONE]\n\n"
    )


@pytest.fixture
def relay_env(monkeypatch: pytest.MonkeyPatch):
    """Full-app relay harness (modeled on tests/test_relay_settle.py's): fake
    auth/db/registry/MCP fetch; scripted SSE per model call; records every
    upstream body. Returns (make, state)."""
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod
    import pcai_llm.core.costs as costs_mod
    import pcai_llm.core.registry as registry_mod
    from pcai_llm.api import routes_mcp
    from pcai_llm.mcpf import federation

    fake = _RelayFakeDB()
    state: dict[str, Any] = {"bodies": [], "model_calls": 0}

    from pcai_llm.api.routes_proxy import catalog_cache_clear

    catalog_cache_clear()

    async def fake_upstream_session(url, headers, op, timeout_s=None):
        class _Block:
            def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
                return {"type": "text", "text": "ok"}

        class _Result:
            is_error = False

            def __init__(self) -> None:
                self.content = [_Block()]

        return _Result()

    async def fake_grants(key_id):
        return {("logsearch", "search_logs")}

    async def fake_fetch_tools(url, headers, timeout_s=None):
        return [{"tool": "search_logs", "description": "search"}]

    def make(key: KeyRecord, chunks_by_call: list[bytes]) -> TestClient:
        async def fake_auth(p: str) -> KeyRecord:
            return key

        def handler(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content.decode())
            state["bodies"].append(body)
            n = state["model_calls"]
            state["model_calls"] += 1
            return httpx2.Response(
                200,
                content=chunks_by_call[min(n, len(chunks_by_call) - 1)],
                headers={"content-type": "text/event-stream"},
            )

        monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
        monkeypatch.setattr(budgets_mod, "db", fake)
        monkeypatch.setattr(costs_mod, "db", fake)
        monkeypatch.setattr(cascade_mod, "db", fake)
        monkeypatch.setattr(routes_proxy, "db", fake)
        monkeypatch.setattr(federation, "db", fake)
        monkeypatch.setattr(federation, "load_grants", fake_grants)
        monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch_tools)
        monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream_session)
        monkeypatch.setattr(routes_mcp, "resolve_mcp_headers", lambda *a, **k: {})
        monkeypatch.setattr(
            registry_mod,
            "_mcp_servers_cache",
            [
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
            ],
        )
        mock_router = Router.with_mock(handler)
        mock_router.set_endpoints(
            MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1", model_type="chat")]
        )
        monkeypatch.setattr(routes_proxy, "model_router", mock_router)
        routes_proxy._in_flight.clear()
        monkeypatch.setattr(routes_proxy, "_loop_guard", {})
        catalog_cache_clear()
        app = FastAPI()
        app.include_router(routes_proxy.router)
        return TestClient(app, raise_server_exceptions=False)

    return make, state


def test_zc7_relay_downgrades_tool_choice_after_the_first_loop_call(relay_env) -> None:
    """Audit ZC-7: the client's tool_choice rode verbatim into EVERY loop
    call — "required" forces each upstream call to return tool calls, so the
    loop could only end at the guards and the caller got the max-iterations
    wrap-up instead of an answer. The client's choice governs call 1; from
    call 2 on (tool results are in the messages) the model must be free to
    answer in text."""
    make, state = relay_env
    tc = make(_key(), [_tool_call_sse(), _content_sse("all done")])
    r = tc.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "search and answer"}],
            "tools": [{"type": "mcp", "server": "logsearch"}],
            "tool_choice": "required",
        },
        headers={"Authorization": "Bearer hllm-x", "X-PCAI-Execute-Tools": "1"},
    )
    assert r.status_code == 200, r.text
    assert state["model_calls"] == 2
    first, second = state["bodies"][0], state["bodies"][1]
    assert first.get("tool_choice") == "required", "call 1 keeps the client's choice"
    assert second.get("tool_choice") == "auto", (
        f"loop calls must be free to answer in text: {second.get('tool_choice')!r}"
    )


def test_l2_relayed_request_skips_the_plain_chat_canonical_capture(relay_env, monkeypatch) -> None:
    """Audit L-2: the plain-chat capture ran on RELAYED requests too — a
    second load+save against the same (key_id, thread_id) row on the hottest
    path, behind a commit comment claiming the guard existed. The capture is
    now skipped for requests whose three-state relay verdict is ON (header,
    key default, or the {type: mcp} body opt-in)."""
    make, _state = relay_env
    captured: list[dict] = []

    async def spy_capture(key, body, raw_header, headers=None):
        captured.append({"messages": body.get("messages")})

    monkeypatch.setattr(routes_proxy, "_capture_chat_canonical", spy_capture)
    tc = make(_key(), [_content_sse("hi")])

    # 1. header-forced relay: no capture
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "r1"}]},
        headers={"Authorization": "Bearer hllm-x", "X-PCAI-Execute-Tools": "1"},
    )
    assert r.status_code == 200, r.text
    assert not any(c["messages"][0]["content"] == "r1" for c in captured), (
        "relayed (header) request must not run the plain-chat capture"
    )

    # 2. body-opt-in relay: no capture
    r = tc.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "r2"}],
            "tools": [{"type": "mcp", "server": "logsearch"}],
        },
        headers={"Authorization": "Bearer hllm-x"},
    )
    assert r.status_code == 200, r.text
    assert not any(c["messages"][0]["content"] == "r2" for c in captured), (
        "relayed (body opt-in) request must not run the plain-chat capture"
    )

    # 3. an explicit header '0' + body opt-in is the conflict 400 (never a
    #    capture either — and the actionable message still wins)
    r = tc.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "mcp", "server": "logsearch"}],
        },
        headers={"Authorization": "Bearer hllm-x", "X-PCAI-Execute-Tools": "0"},
    )
    assert r.status_code == 400 and "require_approval" in r.text


def test_l2_plain_chat_request_still_captures(relay_env, monkeypatch) -> None:
    """The L-2 guard must not eat the FEATURE: a non-relayed request still
    schedules the plain-chat capture."""
    make, _state = relay_env
    captured: list[dict] = []

    async def spy_capture(key, body, raw_header, headers=None):
        captured.append({"messages": body.get("messages")})

    monkeypatch.setattr(routes_proxy, "_capture_chat_canonical", spy_capture)
    tc = make(_key(), [_content_sse("ok")])
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "remember me"}]},
        headers={"Authorization": "Bearer hllm-x"},
    )
    assert r.status_code == 200, r.text
    assert captured and captured[0]["messages"][0]["content"] == "remember me"


async def test_c4_context_budget_keeps_the_pending_tail_flush_boundary(monkeypatch) -> None:
    """Audit C-4: with the context budget ON, the relay loop sees the TRIMMED
    projection while the store holds the UNTRIMMED canonical list — the old
    prefix check (final[:len(stored)] == stored) compared across the two
    shapes, the flush silently skipped, and X-PCAI-Continue 400'd with "no
    awaiting handback state" for the whole turn. The boundary is now passed
    in from the routes layer; the store-vs-adopted race check is unchanged."""
    key = _key()
    stored_untrimmed = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": f"turn {i}"} for i in range(16)
    ]
    projection = [stored_untrimmed[0], stored_untrimmed[-1]]  # what trim_to_budget leaves
    assistant_tool_calls = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    tool_result = {"role": "tool", "tool_call_id": "c1", "content": "result"}
    final_from_relay = [*projection, assistant_tool_calls, tool_result]

    saved: dict[str, Any] = {}

    class CanonicalDB:
        async def fetchrow(self, sql: str, *args: Any):
            return {"messages": json.dumps(stored_untrimmed), "iterations": 0}

        async def execute(self, sql: str, *args: Any):
            if "pending" in sql:
                saved["pending_tail"] = args[2]
                saved["iterations"] = args[3]
            return "UPDATE 1"

    monkeypatch.setattr(routes_proxy, "db", CanonicalDB())

    flush = routes_proxy._persist_conversation_state(
        key, canonical_input=stored_untrimmed, relay_projection=projection
    )
    await flush("thread-c4", final_from_relay, 3)
    assert saved.get("pending_tail") == json.dumps([assistant_tool_calls, tool_result], default=str), (
        "the awaiting-client tail must be persisted from the PROJECTION boundary"
    )
    assert saved.get("iterations") == 3

    # the race detector still fires when the store moved under us
    class MovedDB(CanonicalDB):
        async def fetchrow(self, sql: str, *args: Any):
            return {"messages": json.dumps([{"role": "user", "content": "raced"}]), "iterations": 0}

    monkeypatch.setattr(routes_proxy, "db", MovedDB())
    saved.clear()
    flush2 = routes_proxy._persist_conversation_state(
        key, canonical_input=stored_untrimmed, relay_projection=projection
    )
    await flush2("thread-c4", final_from_relay, 3)
    assert saved == {}, "a raced store must still skip the flush"


def test_l2_approval_always_on_a_relay_default_key_still_captures(relay_env, monkeypatch) -> None:
    """xv round 2, finding 10: require_approval:"always" is the CLIENT-
    execution path — the pre-flight flips the relay OFF for it even on a
    relay-default key, so the early L-2 guard must not swallow its
    plain-chat capture."""
    make, _state = relay_env
    captured: list[dict] = []

    async def spy_capture(key, body, raw_header, headers=None):
        captured.append({"messages": body.get("messages")})

    monkeypatch.setattr(routes_proxy, "_capture_chat_canonical", spy_capture)
    key = _key()
    key.relay_default = True
    tc = make(key, [_content_sse("ok")])
    r = tc.post(
        "/v1/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": "approval-always turn"}],
            "tools": [{"type": "mcp", "server": "logsearch", "require_approval": "always"}],
        },
        headers={"Authorization": "Bearer hllm-x"},
    )
    assert r.status_code == 200, r.text
    assert "x-pcai-relay" not in {k.lower() for k in r.headers}, "precondition: approval-always is NOT relayed"
    assert any(c["messages"][0]["content"] == "approval-always turn" for c in captured), (
        "a NON-relayed (client-execution) request must still be captured"
    )


def test_c2_steady_retry_rearms_after_the_window(monkeypatch) -> None:
    """xv round 2, finding 7: recording blocked attempts as tombstones kept
    the fingerprint's window non-empty under steady retry — a legitimate
    identical call retried once a minute was banned for the LIFE of the
    process (0/120 allowed over 2h; pre-fix 56/64). Blocked attempts are not
    recorded, so the block lapses when the window empties."""
    fake_clock = {"now": 1_000_000.0}

    def fake_monotonic() -> float:
        return fake_clock["now"]

    monkeypatch.setattr(routes_proxy.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(routes_proxy, "_loop_guard", {})
    kid, args = "key-steady", {"c": "same-call"}
    allowed = blocked = 0
    for _ in range(120):  # one retry per minute for 2 hours
        fake_clock["now"] += 60.0
        if routes_proxy.loop_guard_check(kid, "bash", "run", args) is None:
            allowed += 1
        else:
            blocked += 1
    assert allowed > 0, "a steady retryer must not be banned for the life of the process"
    assert blocked > 0, "the window must still block the immediate repeats"

    # finding 8: per-fingerprint state is capped at MAX_HITS regardless of
    # how many times the block fires (pre-tombstone post-fix: 20000 floats)
    monkeypatch.setattr(routes_proxy, "_loop_guard", {})
    fake_clock["now"] = 2_000_000.0
    for _ in range(20000):
        routes_proxy.loop_guard_check(kid, "bash", "run", {"fast": True})
    stored = max(len(v) for v in routes_proxy._loop_guard.values())
    assert stored <= routes_proxy._LOOP_GUARD_MAX_HITS, f"unbounded per-fingerprint state: {stored}"


def test_zc9_steady_state_refusal_reports_the_executed_count(monkeypatch) -> None:
    """xv round 2, finding 9: in the steady-state block the count must stay
    the EXECUTED count — tombstone arithmetic made attempt 20000 report
    "made 19999x" for 5 executed calls."""
    fake_clock = {"now": 3_000_000.0}

    def fake_monotonic() -> float:
        return fake_clock["now"]

    monkeypatch.setattr(routes_proxy.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(routes_proxy, "_loop_guard", {})
    kid, args = "key-zc9s", {"c": "same"}
    for _ in range(20000):
        fake_clock["now"] += 1.0
        reason = routes_proxy.loop_guard_check(kid, "bash", "run", args)
    assert reason is not None
    assert "made 5x" in reason, f"steady-state count drifted: {reason!r}"
    assert "made 19999" not in reason
