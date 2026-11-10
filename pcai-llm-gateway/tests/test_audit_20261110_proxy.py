"""Regression tests for audit 2026-11-10 zone R1 (proxy hot path).

One test group per finding, named by its audit id, so a future reader can go
from a failure straight to the report section:

  P1-2   concurrency-slot leak on the plain streaming dispatch
  P1-3   stream_once must never serve a breaker-cooled endpoint
  P1-7   prefix_hashes returns [] (not None) for a clean prefix
  P2-1   response-cache key carries the metadata variant + guards re-run on a hit
  P2-2   X-PCAI-Session is bounded and hashed before it becomes a row key
  P2-19  the masked replay drops [DONE] by INDEX (split / spaceless framings)
  P2-20  a generic 400 is not model-attributable
  P2-21  the admission estimator charges non-ASCII at >= 1 token/char
  P2-25  _guarded_sse releases the slot AFTER the replay and caps its buffer
  P2-26  completion_cap has no opinion below MIN_COMPLETION_TOKENS
  P2-30  the plain-chat canonical capture is not scheduled for a rejected
         request, and a no-op extend does not rewrite the row
  P3-12  the streaming failover recursion keeps affinity_session/group_name
  P3-13  /v1/models pcai.type uses any(), not endpoints[0]

No live Postgres and no live upstream: the same fake-db / mock-transport
harness the other proxy tests use.
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy as rp
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import canonical as canonical_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import context_window, model_fallbacks, stickiness
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import response_cache as rc
from pcai_llm.routing.router import Endpoint, Pool, Router

MODEL = "r1-model"
OTHER = "r1-other-model"

SSE_BODY = (
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
    b"data: [DONE]\n\n"
)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


class FakeDB:
    """Budget / ledger / cache stub: no budgets, records settles and cache rows."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []
        self.cache: dict[tuple[Any, ...], bytes] = {}
        self.capture_reads: list[Any] = []
        self.capture_writes: list[Any] = []

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "response_body" in sql:
            body = self.cache.get((a[0], a[1], a[2]))
            return {"response_body": body} if body is not None else None
        if "profiles" in sql:
            return None
        if "canonical_conversations" in sql:
            self.capture_reads.append(a)
            return None
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
        if "canonical_conversations" in sql:
            self.capture_writes.append(a)
        self.settled.append(a)
        return "INSERT 0 1"


def _key(*, max_concurrent: int = 0) -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=max_concurrent,
        expires_at=None,
    )


def _json_upstream(text: str = "hello") -> dict[str, Any]:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Fake auth + fake db + mock upstream. Returns (make, fake, key)."""
    monkeypatch.setenv("PCAI_LLM_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    key = _key()
    fake = FakeDB()

    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake, raising=False)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(model_fallbacks, "db", fake)
    monkeypatch.setattr(rc, "db", fake)
    monkeypatch.setattr(rp, "db", fake, raising=False)
    rp._in_flight.clear()
    rp._capture_tasks.clear()

    def make(handler, *, models: tuple[str, ...] = (MODEL,), cooling: tuple[str, ...] = ()) -> TestClient:
        r = Router.with_mock(handler)
        for name in models:
            ep = Endpoint(id=f"ep-{name}", model_name=name, base_url=f"http://{name}/v1", model_type="chat")
            if name in cooling:
                ep.cooldown_until = time.monotonic() + 600.0
            r.set_endpoints(name, [ep])
        monkeypatch.setattr(rp, "model_router", r)
        app = FastAPI()
        app.include_router(rp.router)
        return TestClient(app, raise_server_exceptions=False)

    yield make, fake, key
    rp._in_flight.clear()
    rp._capture_tasks.clear()


def _post(tc: TestClient, body: dict, headers: dict | None = None):
    return tc.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer hllm-x", **(headers or {})})


# ---------------------------------------------------------------------------
# P1-2 — the concurrency slot is released exactly once, on every exit
# ---------------------------------------------------------------------------


def test_p1_2_once_wrapper_runs_the_release_at_most_once() -> None:
    calls: list[int] = []
    release = rp._once(lambda: calls.append(1))
    for _ in range(5):
        release()
    assert calls == [1], "the per-KEY counter must be decremented exactly once per admitted request"


def test_p1_2_plain_streaming_dispatch_failure_releases_the_slot(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raise between the gates and response construction used to leak the slot.

    Pre-fix the plain streaming dispatch sat OUTSIDE both release paths, so an
    exception here left the key's _in_flight counter permanently incremented —
    the key is then 429-bricked on its concurrency cap until the pod restarts.
    """
    make, _fake, key = env
    key.max_concurrent = 2

    async def boom(*a: Any, **kw: Any):
        raise rp.fastapi.HTTPException(502, "upstream request failed")

    monkeypatch.setattr(rp, "_stream_chat", boom)
    tc = make(lambda req: httpx2.Response(200, content=SSE_BODY))
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 502, r.text
    assert key.id not in rp._in_flight, "the concurrency slot leaked on the plain streaming dispatch"


def test_p1_2_base_exception_on_the_plain_dispatch_still_releases(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation is a BaseException — the guard must be BaseException-wide."""

    class _Disconnect(BaseException):
        pass

    make, _fake, key = env
    key.max_concurrent = 2

    async def boom(*a: Any, **kw: Any):
        raise _Disconnect

    monkeypatch.setattr(rp, "_stream_chat", boom)
    tc = make(lambda req: httpx2.Response(200, content=SSE_BODY))
    try:
        _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True})
    except _Disconnect:
        pass  # the ASGI layer may or may not re-raise a bare BaseException
    assert key.id not in rp._in_flight, "a BaseException (client disconnect) leaked the concurrency slot"


def test_p1_2_a_successful_stream_keeps_the_slot_until_the_generator_ends(env) -> None:
    """Ownership TRANSFERS: releasing at response construction would void the cap."""
    make, _fake, key = env
    key.max_concurrent = 2
    tc = make(lambda req: httpx2.Response(200, content=SSE_BODY))
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 200
    assert b"hello" in r.content
    assert key.id not in rp._in_flight, "the generator must release once the stream is over"


@pytest.mark.asyncio
async def test_p1_2_response_layer_backstop_releases_a_never_started_stream() -> None:
    """A cancellation during http.response.start must still release the slot.

    Starlette's stream_response sends http.response.start BEFORE it iterates the
    body, and a NEVER-STARTED async generator executes no body — so the
    generator's finally cannot run and the per-key counter stays incremented
    (429-bricked key until the pod restarts). The response wrapper is the
    backstop for exactly that exit.
    """
    released: list[int] = []
    started: list[int] = []

    async def body():
        started.append(1)
        yield b"never-delivered"

    resp = rp._SlotReleasingStreamingResponse(
        body(), media_type="text/event-stream", release_slot=lambda: released.append(1)
    )

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            raise RuntimeError("cancelled during response start")

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    with pytest.raises(RuntimeError):
        await resp({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert not started, "precondition: the body generator was never iterated"
    assert released == [1], "the response-layer backstop did not give the slot back"


@pytest.mark.asyncio
async def test_p1_2_response_layer_backstop_releases_on_a_normal_completion() -> None:
    released: list[int] = []

    async def body():
        yield b"chunk"

    resp = rp._SlotReleasingStreamingResponse(
        body(), media_type="text/event-stream", release_slot=lambda: released.append(1)
    )
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    await resp({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body", "http.response.body"]
    assert released == [1], "exactly one release per response"


# ---------------------------------------------------------------------------
# P1-3 — stream_once must not fall back to endpoints[0]
# ---------------------------------------------------------------------------


def _patch_mcp(monkeypatch: pytest.MonkeyPatch, on_catalog: Any = None) -> None:
    """Grant one tool so the relay pre-flight succeeds; no live MCP servers."""
    import pcai_llm.api.routes_mcp as rm
    import pcai_llm.mcpf.federation as fed

    async def fake_load_grants(key_id):
        return {("srv1", "tool1")}

    async def fake_load_denies(key_id):
        return set()

    async def fake_mcp_servers():
        return [{"name": "srv1", "url": "http://mcp/x", "headers": {}, "tool_policy": "all"}]

    async def fake_fetch_tools(url, headers, timeout_s=None):
        if on_catalog is not None:
            on_catalog()
        return [{"tool": "tool1", "description": "d", "inputSchema": {"type": "object"}}]

    def fake_build_headers(headers, secret, enc):
        return {}

    monkeypatch.setattr(fed, "load_grants", fake_load_grants)
    monkeypatch.setattr(fed, "load_denies", fake_load_denies)
    monkeypatch.setattr(rp.registry, "mcp_servers", fake_mcp_servers)
    monkeypatch.setattr(rm, "_fetch_tools", fake_fetch_tools)
    monkeypatch.setattr(rm, "build_mcp_headers", fake_build_headers)
    monkeypatch.setattr(rp.settings, "tool_relay_grant_mode", "explicit")
    rp.catalog_cache_clear()


def _relay_body() -> dict[str, Any]:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "mcp", "server": "srv1"}],
    }


def test_p1_3_relay_iteration_never_serves_a_cooled_endpoint(env, monkeypatch) -> None:
    """The endpoint cools AFTER admission; the next iteration must not serve it.

    Pool.pick() returns None precisely when every endpoint is breaker-cooling
    or probe-failed. The old "or pool.endpoints[0]" served it anyway — ignoring
    the breaker, the priority tiers and weight==0 standby — so a tripped breaker
    bought nothing on the dominant agent path.
    """
    make, _fake, _key = env
    reached: list[dict] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        reached.append(json.loads(req.content.decode()))
        return httpx2.Response(200, content=SSE_BODY)

    tc = make(handler)
    pool = rp.model_router.pool(MODEL)

    def cool_after_admission() -> None:
        for ep in pool.endpoints:
            ep.cooldown_until = time.monotonic() + 600.0

    _patch_mcp(monkeypatch, on_catalog=cool_after_admission)
    r = _post(tc, _relay_body(), headers={"X-PCAI-Execute-Tools": "1"})
    assert r.status_code == 200, r.text
    assert not reached, "a breaker-cooled endpoint must never be served (endpoints[0] fallback is back)"
    # No fallback configured: the relay's OWN transport-failure close, not a
    # synthesised "(model error: no healthy endpoint)" answer event.
    assert b"model error" in r.content
    assert b"[DONE]" in r.content


def test_p1_3_a_cooled_primary_hands_the_turn_to_a_healthy_fallback(env, monkeypatch) -> None:
    """The pick-None hop must actually REACH the relay's failover walk.

    Cross-validation wave 1: the first fix yielded ("error", …) + ("content", …),
    but relay_stream's walk fires only on a transport exception (call_failed) or
    budget exhaustion — an error EVENT just stamps the settle status. So the
    turn ended with "(model error: no healthy endpoint)" as the assistant answer
    while a perfectly healthy fallback sat unused.
    """
    make, _fake, _key = env
    reached: list[str] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        reached.append(str(json.loads(req.content.decode()).get("model")))
        return httpx2.Response(200, content=SSE_BODY)

    tc = make(handler, models=(MODEL, OTHER))
    pool = rp.model_router.pool(MODEL)

    def cool_after_admission() -> None:
        for ep in pool.endpoints:
            ep.cooldown_until = time.monotonic() + 600.0

    _patch_mcp(monkeypatch, on_catalog=cool_after_admission)
    # Stand in for the profile ordering's tail (the fake db has no profile).
    monkeypatch.setattr(rp, "_relay_fallback_tail", lambda *a, **kw: [OTHER])
    r = _post(tc, _relay_body(), headers={"X-PCAI-Execute-Tools": "1"})
    assert r.status_code == 200, r.text
    assert MODEL not in reached, "the cooled primary was served"
    assert OTHER in reached, "the failover walk never took the hop"
    assert b"hello" in r.content, "the fallback's answer did not reach the client"


# ---------------------------------------------------------------------------
# P1-7 — prefix_hashes: [] means "clean", None means "not computed"
# ---------------------------------------------------------------------------


def test_p1_7_prefix_hashes_returns_an_empty_list_for_a_clean_prefix() -> None:
    stored = [{"role": "user", "content": "hi"}]
    incoming = [*stored, {"role": "assistant", "content": "yo"}]
    assert canonical_mod.prefix_hashes(stored, incoming) == []
    assert canonical_mod.prefix_hashes(stored, [{"role": "user", "content": "different"}]) == [0]


def test_p1_7_a_shared_verdict_costs_exactly_one_hash_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the finding: ONE walk per extend turn, not three."""
    real = canonical_mod.message_hash
    calls = {"n": 0}

    def counting(message: Any) -> str:
        calls["n"] += 1
        return real(message)

    monkeypatch.setattr(canonical_mod, "message_hash", counting)
    stored = [{"role": "user", "content": "hi"}]
    incoming = [
        *stored,
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    verdict = canonical_mod.prefix_hashes(stored, incoming)
    after_walk = calls["n"]
    assert after_walk == 2 * len(stored)  # canonical[idx] + incoming[idx], one walk

    _messages, mode = canonical_mod.reconcile(stored, incoming, prefix_verdict=verdict)
    assert mode == canonical_mod.MODE_EXTEND
    assert calls["n"] == after_walk, "reconcile recomputed the walk it was handed"

    detected, _info = canonical_mod.detect_continuation(stored, incoming, prefix_verdict=verdict)
    assert detected is True
    assert calls["n"] == after_walk, "detect_continuation recomputed the walk it was handed"


def test_p1_7_none_still_means_not_computed() -> None:
    stored = [{"role": "user", "content": "hi"}]
    incoming = [*stored, {"role": "assistant", "content": "yo"}]
    assert canonical_mod.reconcile(stored, incoming)[1] == canonical_mod.MODE_EXTEND
    assert canonical_mod.reconcile(stored, [{"role": "user", "content": "x"}])[1] == canonical_mod.MODE_RESET


# ---------------------------------------------------------------------------
# P2-1 — the response-cache key and the guardrail stage on a hit
# ---------------------------------------------------------------------------


def test_p2_1_request_hash_has_two_metadata_variants() -> None:
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    assert rc.request_hash(body) != rc.request_hash(body, want_metadata=True)
    assert rc.request_hash(body) == rc.request_hash(body, want_metadata=False)


def test_p2_1_a_metadata_less_caller_never_hits_a_metadata_body(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp.settings, "response_cache_mode", "active")
    make, _fake, _key = env
    seen: list[int] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(1)
        return httpx2.Response(200, json=_json_upstream())

    tc = make(handler)
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    first = _post(tc, body, headers={"X-PCAI-Metadata": "1"})
    assert first.status_code == 200, first.text
    assert "pcai" in first.json(), "precondition: the metadata variant stored a pcai block"

    plain = _post(tc, body)
    assert plain.status_code == 200, plain.text
    assert "pcai" not in plain.json(), "a caller who never asked must not receive the pcai block"
    assert len(seen) == 2, "the two variants must be separate cache entries (no cross-variant hit)"

    again = _post(tc, body)
    assert again.headers.get("x-cache") == "HIT", "the metadata-less variant must hit its own entry"


def _fake_post_guardrails(post_result: Any) -> Any:
    async def guardrails_for_model(model_name: str):
        return [SimpleNamespace(id=uuid.uuid4(), name="leak-guard", mode="post_call", engine="blocklist", config={})]

    async def run_pre_call(guards, messages, http):
        return messages, []

    async def run_post_call(guards, text, http):
        return post_result

    return SimpleNamespace(
        guardrails_for_model=guardrails_for_model,
        run_pre_call=run_pre_call,
        run_post_call=run_post_call,
    )


def _ev(name: str, action: str) -> Any:
    return SimpleNamespace(
        guardrail=name,
        mode="post_call",
        engine="blocklist",
        action=action,
        detail="",
        as_dict=lambda: {"guardrail": name, "mode": "post_call", "engine": "blocklist", "action": action, "detail": ""},
    )


def test_p2_1_a_cache_hit_reruns_post_call_guardrails(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit used to replay the stored bytes verbatim, skipping the guard stage."""
    monkeypatch.setattr(rp.settings, "response_cache_mode", "active")
    make, _fake, _key = env
    seen: list[int] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(1)
        return httpx2.Response(200, json=_json_upstream("my key is SECRET-1"))

    tc = make(handler)
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    first = _post(tc, body)
    assert first.status_code == 200 and "SECRET-1" in first.text

    monkeypatch.setattr(rp, "guardrails", _fake_post_guardrails(("[REDACTED]", [_ev("leak-guard", "masked")], False)))
    hit = _post(tc, body)
    assert hit.headers.get("x-cache") == "HIT"
    assert len(seen) == 1, "precondition: this is a cache hit"
    assert "SECRET-1" not in hit.text, "the cached answer was replayed WITHOUT the post_call mask"
    assert "[REDACTED]" in hit.text
    assert hit.headers.get("x-pcai-guardrail-modified") == "leak-guard"


def test_p2_1_a_cache_hit_can_be_blocked_by_a_new_guardrail(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp.settings, "response_cache_mode", "active")
    make, _fake, _key = env

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_json_upstream("something now forbidden"))

    tc = make(handler)
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    assert _post(tc, body).status_code == 200

    monkeypatch.setattr(rp, "guardrails", _fake_post_guardrails(("", [_ev("leak-guard", "blocked")], True)))
    hit = _post(tc, body)
    assert hit.status_code == 400, hit.text
    assert "guardrail_blocked" in hit.text


def test_p2_16_a_metadata_cost_read_fault_does_not_fail_the_response(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The opt-in metadata cost must never 500 a response the upstream was paid for.

    _proxy_json's want_metadata path runs AFTER settle() wrote the ledger row and
    after the upstream answered, and costs._load() propagates DB faults — so an
    unguarded read turned a model_costs blip into a 500 post-spend, and the
    client's retry was a second GPU bill. The block simply omits cost_usd.
    """
    make, _fake, _key = env

    async def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("model_costs read failed")

    monkeypatch.setattr(costs_mod, "compute", boom)
    tc = make(lambda req: httpx2.Response(200, json=_json_upstream()))
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}, headers={"X-PCAI-Metadata": "1"})
    assert r.status_code == 200, r.text
    pcai = r.json()["pcai"]
    assert pcai["served_model"] == MODEL, "the rest of the metadata block must still ride"
    assert "cost_usd" not in pcai, "an unreadable cost must be OMITTED, never guessed"


# ---------------------------------------------------------------------------
# P2-2 — X-PCAI-Session is bounded and hashed
# ---------------------------------------------------------------------------


def test_p2_2_affinity_key_hashes_and_bounds() -> None:
    assert stickiness.affinity_key(None) is None
    assert stickiness.affinity_key("") is None
    assert stickiness.affinity_key("x" * (stickiness.AFFINITY_KEY_MAX + 1)) is None
    assert stickiness.affinity_key("bad value!") is None  # space + '!' outside the charset
    digest = stickiness.affinity_key("sess-1:abc.def")
    assert digest is not None and len(digest) == 64
    assert stickiness.affinity_key("  sess-1:abc.def  ") == digest, "the value is stripped before hashing"
    assert digest != "sess-1:abc.def", "the raw client value must never become the row key"


def test_p2_2_an_invalid_session_header_is_dropped_not_refused(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Affinity is routing state: a junk header must still be served."""
    make, _fake, _key = env
    tc = make(lambda req: httpx2.Response(200, json=_json_upstream()))
    r = _post(
        tc,
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-PCAI-Session": "bad session id with spaces"},
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# P2-19 — the masked replay drops [DONE] by index
# ---------------------------------------------------------------------------


def _start_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """A STARTED router: _guarded_sse evaluates model_router.client before
    handing it to the guard engine, and the module-level singleton is not
    started outside the app lifespan."""
    monkeypatch.setattr(rp, "model_router", Router.with_mock(lambda req: httpx2.Response(200, content=b"")))


def _guarded(*, post_result: Any, chunks: list[bytes], key: KeyRecord, release_slot: Any = None):
    """A _guarded_sse generator over fixed chunks."""

    async def inner():
        for chunk in chunks:
            yield chunk

    return rp._guarded_sse(
        inner(),
        None,
        [SimpleNamespace(id=uuid.uuid4(), name="leak-guard", mode="post_call", engine="blocklist", config={})],
        key=key,
        model=MODEL,
        request_id="rid",
        started=time.monotonic(),
        release_slot=release_slot,
        relay_mode=True,
    )


def _masking_guardrails() -> Any:
    return _fake_post_guardrails(("[REDACTED]", [_ev("leak-guard", "masked")], False))


async def _noop_settle(*a: Any, **kw: Any) -> None:
    """relay_mode marker row: the direct tests have no db handle."""
    return


@pytest.mark.asyncio
async def test_p2_19_split_done_frame_is_dropped_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    _start_router(monkeypatch)
    monkeypatch.setattr(rp, "guardrails", _masking_guardrails())
    monkeypatch.setattr(rp, "settle", _noop_settle)
    key = _key()
    chunks = [
        b'data: {"choices":[{"index":0,"delta":{"content":"SECRET"},"finish_reason":null}]}\n\n',
        b"data: [DO",
        b"NE]\n\n",
    ]
    out = [c async for c in _guarded(post_result=None, chunks=chunks, key=key)]
    joined = b"".join(out)
    assert joined.count(b"[DONE]") == 1, "a straddled [DONE] leaked ahead of the synthetic masked final"
    assert b"SECRET" not in joined
    assert b"[REDACTED]" in joined


@pytest.mark.asyncio
async def test_p2_19_spaceless_done_frame_is_dropped_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    _start_router(monkeypatch)
    monkeypatch.setattr(rp, "guardrails", _masking_guardrails())
    monkeypatch.setattr(rp, "settle", _noop_settle)
    key = _key()
    chunks = [
        b'data: {"choices":[{"index":0,"delta":{"content":"SECRET"},"finish_reason":null}]}\n\n',
        b"data:[DONE]\n\n",
    ]
    out = [c async for c in _guarded(post_result=None, chunks=chunks, key=key)]
    joined = b"".join(out)
    assert joined.count(b"[DONE]") == 1, "a spaceless [DONE] leaked ahead of the synthetic masked final"
    assert b"SECRET" not in joined


# ---------------------------------------------------------------------------
# P2-20 — a generic 400 is not model-attributable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "model", "expected"),
    [
        (b"the field 'temperature' does not exist in this API", "glm", False),
        (b'{"error":"unsupported field","detail":"parameter top_p does not exist"}', "glm", False),
        (b"deployment glm does not exist", "glm", True),
        (b"The model 'glm' does not exist", "glm", True),
        (b'{"echo":"' + b"y" * 400 + b'","model":"glm"}', "glm", False),
        (b"y" * 400 + b" glm", "glm", False),  # name beyond the head window
        (b"Invalid request: max_tokens must be positive", "glm", False),
    ],
)
def test_p2_20_only_model_attributable_400s_are_eligible(body: bytes, model: str, expected: bool) -> None:
    assert rp._is_model_attributable_4xx(400, body, model) is expected


# ---------------------------------------------------------------------------
# P2-21 — non-ASCII is charged at >= 1 token per character
# ---------------------------------------------------------------------------


def test_p2_21_non_ascii_admission_estimate_is_not_under_counted() -> None:
    cjk = "日本語テスト"  # 6 characters, 18 UTF-8 bytes
    assert rp._message_text_tokens({"content": cjk}) == 6
    assert rp._estimate_tokens({"messages": [{"role": "user", "content": cjk}], "max_tokens": 1}) == 7
    # ASCII is byte-identical to the shipped len//4 heuristic.
    assert rp._message_text_tokens({"content": "x" * 40}) == 10
    assert rp._message_text_tokens({"content": [{"type": "text", "text": "ab"}]}) == 0


# ---------------------------------------------------------------------------
# P2-25 — release AFTER the replay, and a bounded buffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_25_the_slot_is_released_after_the_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    _start_router(monkeypatch)
    monkeypatch.setattr(rp, "guardrails", _fake_post_guardrails(("hello", [], False)))
    order: list[str] = []
    key = _key()
    agen = _guarded(
        post_result=None,
        chunks=[SSE_BODY],
        key=key,
        release_slot=lambda: order.append("release"),
    )
    first = await agen.__anext__()
    assert first
    assert order == [], "the concurrency slot was released BEFORE the buffered bytes were replayed"
    async for _ in agen:
        pass
    assert order == ["release"]


@pytest.mark.asyncio
async def test_p2_25_an_oversized_buffer_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _start_router(monkeypatch)
    monkeypatch.setattr(rp, "guardrails", _fake_post_guardrails(("hello", [], False)))
    monkeypatch.setattr(rp, "_GUARDED_SSE_MAX_BUFFER_BYTES", 64)
    settled: list[tuple] = []

    async def fake_settle(*a: Any, **kw: Any) -> None:
        settled.append(a)

    monkeypatch.setattr(rp, "settle", fake_settle)
    closed: list[int] = []

    async def inner():
        try:
            for _ in range(5):
                yield b'data: {"choices":[{"index":0,"delta":{"content":"SECRET"}}]}\n\n'
        finally:
            closed.append(1)

    key = _key()
    out = [
        c
        async for c in rp._guarded_sse(
            inner(),
            None,
            [SimpleNamespace(id=uuid.uuid4(), name="leak-guard", mode="post_call", engine="blocklist", config={})],
            key=key,
            model=MODEL,
            request_id="rid",
            started=time.monotonic(),
            relay_mode=True,
        )
    ]
    joined = b"".join(out)
    assert closed, "the abandoned upstream stream was not closed"
    assert b"guardrail_buffer_exceeded" in joined
    assert b"SECRET" not in joined, "an unjudged answer must never be delivered"
    assert settled, "the overflow must still settle a ledger row"


# ---------------------------------------------------------------------------
# P2-26 — completion_cap has no opinion below the minimum floor
# ---------------------------------------------------------------------------


def test_p2_26_completion_cap_is_none_below_the_minimum_floor() -> None:
    floor = context_window.MIN_COMPLETION_TOKENS
    prompt = 8192 - context_window.SAFETY_MARGIN_TOKENS - (floor - 1)
    assert context_window.completion_cap(context_limit=8192, est_prompt=prompt, requested=4096) is None

    fits_prompt = 8192 - context_window.SAFETY_MARGIN_TOKENS - floor
    assert context_window.completion_cap(context_limit=8192, est_prompt=fits_prompt, requested=4096) == floor


# ---------------------------------------------------------------------------
# P2-30 — no capture before the gates, no no-op rewrite
# ---------------------------------------------------------------------------


def test_p2_30_a_rejected_request_never_schedules_a_canonical_capture(env, monkeypatch: pytest.MonkeyPatch) -> None:
    make, _fake, _key = env
    called: list[int] = []

    async def recorder(*a: Any, **kw: Any) -> None:
        called.append(1)

    monkeypatch.setattr(rp, "_capture_chat_canonical", recorder)
    tc = make(lambda req: httpx2.Response(200, json=_json_upstream()))
    r = _post(tc, {"model": "ghost-model", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404, r.text
    assert not called, "a 404 request paid a canonical read+write before the gates"
    assert not rp._capture_tasks


@pytest.mark.asyncio
async def test_p2_30_a_no_op_extend_does_not_rewrite_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = [{"role": "user", "content": "hi"}]
    writes: list[Any] = []
    reads: list[Any] = []

    async def fake_load_row(db, key_id, thread_id):
        reads.append(thread_id)
        return list(stored), 0

    async def fake_save(*a: Any, **kw: Any) -> None:
        writes.append(a)

    monkeypatch.setattr(canonical_mod, "load_row", fake_load_row)
    monkeypatch.setattr(canonical_mod, "save", fake_save)
    monkeypatch.setattr(rp.settings, "canonical_conversations", True)
    from pcai_llm.core import title_requests

    monkeypatch.setattr(title_requests, "is_title_request", lambda body, headers: False)
    key = _key()

    await rp._capture_chat_canonical(key, {"messages": list(stored)}, "", None)
    assert reads, "precondition: the stored row was read"
    assert not writes, "a re-sent identical history must not re-serialise the whole conversation JSONB"

    await rp._capture_chat_canonical(key, {"messages": [*stored, {"role": "user", "content": "next"}]}, "", None)
    assert len(writes) == 1, "a genuine extend must still persist"


# ---------------------------------------------------------------------------
# P3-12 — the streaming failover recursion keeps the pin context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p3_12_streaming_failover_threads_the_pin_context(env, monkeypatch: pytest.MonkeyPatch) -> None:
    make, _fake, key = env

    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content.decode())
        if body.get("model") == MODEL:
            return httpx2.Response(500, json={"error": "boom"})
        return httpx2.Response(200, content=SSE_BODY)

    tc = make(handler, models=(MODEL, OTHER))
    del tc  # only the patched router is needed

    real = rp._stream_chat
    calls: list[dict[str, Any]] = []

    async def spy(*a: Any, **kw: Any):
        calls.append(kw)
        return await real(*a, **kw)

    monkeypatch.setattr(rp, "_stream_chat", spy)

    router = rp.model_router
    pool = router.pool(MODEL)
    assert pool is not None
    ep = pool.endpoints[0]
    resp = await rp._stream_chat(
        pool,
        ep,
        f"{ep.base_url}/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        {},
        key,
        MODEL,
        10,
        "rid",
        time.monotonic(),
        release_slot=lambda: None,
        fallback_chain=[OTHER],
        affinity_session="sess-1",
        group_name="grp",
    )
    assert len(calls) == 2, "the 5xx did not walk the chain"
    assert calls[-1].get("affinity_session") == "sess-1", "the failover hop dropped the affinity session"
    assert calls[-1].get("group_name") == "grp", "the failover hop dropped the group name"
    assert resp is not None


# ---------------------------------------------------------------------------
# P3-13 — /v1/models pcai.type is any(), not endpoints[0]
# ---------------------------------------------------------------------------


def test_p3_13_models_type_ignores_registration_order(env, monkeypatch: pytest.MonkeyPatch) -> None:
    make, _fake, _key = env
    r = Router.with_mock(lambda req: httpx2.Response(200, json={"data": []}))
    # FIRST endpoint is a non-chat type; the pool still serves chat.
    r.set_endpoints(
        MODEL,
        [
            Endpoint(id="ep-embed", model_name=MODEL, base_url="http://x/v1", model_type="embedding"),
            Endpoint(id="ep-chat", model_name=MODEL, base_url="http://x/v1", model_type="chat"),
        ],
    )
    monkeypatch.setattr(rp, "model_router", r)
    tc = make(lambda req: httpx2.Response(200, json={"data": []}))
    resp = tc.get("/v1/models", headers={"Authorization": "Bearer hllm-x"})
    assert resp.status_code == 200, resp.text
    entry = next(m for m in resp.json()["data"] if m["id"] == MODEL)
    assert entry["pcai"]["type"] == "chat", "pcai.type used endpoints[0] instead of pool_has_type"


def test_p3_13_pool_has_type_fail_open_default() -> None:
    assert rp.pool_has_type(None, "chat") is True
    assert rp.pool_has_type(Pool(endpoints=[]), "chat") is True
