"""Client tool-server dispatch (ROADMAP §7 phase 2 wave B) — the SPIKE.

What these tests prove, in order of risk:

1. the three-message protocol end-to-end through the REAL HTTP routes
   (TestClient): offer → dispatch → long-poll claim → result → the
   relay-side future resolves;
2. delivery-once / replay-denial: a second poll never re-delivers a
   claimed job; a second result POST is 409 and dropped;
3. auth failures (401/403) on every dispatch route;
4. bounded failure: an offline client surfaces as a tool ERROR after the
   timeout — never a hang;
5. the executor adapter's gate discipline (rate refusal, settle under
   `local:<tool>`, loop-guard denial);
6. THE WAVE'S WHOLE POINT: a relayed turn where the model calls a
   client-offered tool (`client__bash`), the "client" (test double) polls
   + answers over the dispatch routes, and the stream CONTINUES to a
   final answer WITHOUT ending in finish_reason=tool_calls.

The spike is flag-gated (settings.client_tool_dispatch, default False):
route tests flip it via monkeypatch; the relay-integration test drives
toolrelay.relay_stream directly with the dispatch executor. Wave B2
(bottom section) proves the PRODUCTION wiring through the real app:
create_app() with the flag on mounts the routes AND arms the relay path
(offer → catalog → dispatch executor), plus the fail-safe and the
shared wall-clock deadline decision.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_dispatch
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import clientside, toolrelay

HEADERS = {"Authorization": "Bearer test-key"}
KEY_A = uuid.uuid4()
USER_A = uuid.uuid4()

# --- wave B2 production-wiring test constants (bottom of this file) --------
MODEL_B2 = "b2-model"


def _model_stream_sse(calls: list[list[dict]]):
    """MockTransport handler for create_app()-level relay tests: one scripted
    OpenAI SSE stream per model call (test_relay_settle._model_stream shape,
    inlined so this file stays self-contained)."""
    import json as _json

    import httpx2

    n = {"i": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        i = n["i"]
        n["i"] += 1
        chunks = calls[min(i, len(calls) - 1)]
        body = b"".join(b"data: " + _json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    return handler


CATALOG = [{"server": "logsearch", "tool": "search_logs", "description": "search"}]
GRANTS = {("logsearch", "search_logs")}

BASH_OFFER = {
    "tools": [
        {
            "name": "bash",
            "description": "Run a shell command on the user's machine",
            "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            "timeout_s": 30,
        }
    ]
}


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": KEY_A,
        "user_id": USER_A,
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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """The dispatch router on a bare app with the flag ON and auth faked
    to a fixed key (routes_conversations test pattern)."""
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    clientside.hub.revoke(KEY_A)
    import pcai_llm.api.auth as auth_mod

    async def fake_auth(p: str) -> KeyRecord:
        return _key()

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    app = FastAPI()
    app.include_router(routes_dispatch.router)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    clientside.hub.revoke(KEY_A)


def _register(c: TestClient, **over: Any) -> dict:
    body = dict(BASH_OFFER)
    body.update(over)
    r = c.post("/v1/dispatch/register", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# protocol round trip through the real HTTP routes
# ---------------------------------------------------------------------------


def test_register_and_introspect(client: TestClient) -> None:
    out = _register(client)
    assert out["client_id"] and set(out["tools"]) == {"bash"}
    # Re-register is an UPDATE: same client identity, refreshed offer.
    again = _register(client, ttl_s=120)
    assert again["client_id"] == out["client_id"]
    # Introspection returns the live offer with the schema.
    got = client.get("/v1/dispatch/offer", headers=HEADERS).json()
    assert got["client_id"] == out["client_id"]
    assert got["tools"][0]["name"] == "bash"
    assert got["tools"][0]["inputSchema"]["properties"]["command"]["type"] == "string"


def test_offer_ttl_expiry_takes_tools_out(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _register(client, ttl_s=60)  # clamped minimum
    # Force expiry by rewinding the stored deadline (monotonic clock).
    offer = clientside.hub.offer_lookup(KEY_A)
    assert offer is not None
    offer.expires_at = time.monotonic() - 1.0
    assert clientside.hub.offer_lookup(KEY_A) is None
    r = client.get("/v1/dispatch/offer", headers=HEADERS)
    assert r.status_code == 404


async def test_offer_dispatch_resolve_round_trip(client: TestClient) -> None:
    """The full channel: relay dispatch (async) + client poll (HTTP) +
    result (HTTP) resolve the future — driven CONCURRENTLY like real
    processes, not call-then-claim sequencing. The poll loop yields to
    the event loop so the dispatch task can enqueue before the (sync)
    TestClient request blocks its portal thread."""
    _register(client)
    dispatch_task = asyncio.ensure_future(clientside.hub.dispatch(KEY_A, "bash", {"command": "ls -la"}))
    # The long-poll (wait_s=0 keeps the test instant) must claim the job.
    deadline = time.monotonic() + 5
    job: dict | None = None
    while time.monotonic() < deadline and job is None:
        r = client.get("/v1/dispatch/poll?wait_s=0", headers=HEADERS)
        if r.status_code == 200:
            job = r.json()
            break
        await asyncio.sleep(0.01)
    assert job is not None, "dispatch job never became claimable"
    assert job["tool"] == "bash" and job["arguments"] == {"command": "ls -la"}
    assert job["dispatch_id"] and job["deadline_s"] >= 1.0
    # Answer it; the relay-side future resolves with the content.
    r = client.post(
        "/v1/dispatch/result",
        json={"dispatch_id": job["dispatch_id"], "ok": True, "content": "total 4"},
        headers=HEADERS,
    )
    assert r.status_code == 200 and r.json() == {"settled": True}
    ok, content = await asyncio.wait_for(dispatch_task, timeout=5)
    assert ok and content == "total 4"


def test_poll_empty_is_204(client: TestClient) -> None:
    _register(client)
    r = client.get("/v1/dispatch/poll?wait_s=0", headers=HEADERS)
    assert r.status_code == 204


def test_dispatch_without_offer_is_tool_error(client: TestClient) -> None:
    """No live offer -> immediate tool error (default-deny), never a hang."""
    ok, content = asyncio.run(clientside.hub.dispatch(KEY_A, "bash", {}))
    assert not ok and "no client tool offer" in content


def test_dispatch_unknown_tool_is_denied(client: TestClient) -> None:
    _register(client)
    ok, content = asyncio.run(clientside.hub.dispatch(KEY_A, "rm_rf_everything", {}))
    assert not ok and "not in the client's live tool offer" in content


# ---------------------------------------------------------------------------
# delivery-once + replay denial
# ---------------------------------------------------------------------------


async def test_double_delivery_denial(client: TestClient) -> None:
    """A claimed job is gone: a second poll cannot re-deliver it."""
    _register(client)
    task = asyncio.ensure_future(clientside.hub.dispatch(KEY_A, "bash", {"command": "whoami"}))
    deadline = time.monotonic() + 5
    job: dict | None = None
    while time.monotonic() < deadline and job is None:
        r = client.get("/v1/dispatch/poll?wait_s=0", headers=HEADERS)
        if r.status_code == 200:
            job = r.json()
        await asyncio.sleep(0.01)
    assert job is not None
    # The SAME claim from a second poll (retry, second tab): empty.
    assert client.get("/v1/dispatch/poll?wait_s=0", headers=HEADERS).status_code == 204
    # First answer settles; a REPLAY of the result is 409 and dropped —
    # the settled value is the FIRST one.
    r1 = client.post(
        "/v1/dispatch/result", json={"dispatch_id": job["dispatch_id"], "ok": True, "content": "first"}, headers=HEADERS
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/v1/dispatch/result",
        json={"dispatch_id": job["dispatch_id"], "ok": True, "content": "REPLAY"},
        headers=HEADERS,
    )
    assert r2.status_code == 409
    ok, content = await asyncio.wait_for(task, timeout=5)
    assert ok and content == "first"


async def test_result_from_foreign_key_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A result is acceptable only from the OFFERING key (cross-key
    dispatch is impossible by construction): a foreign key's answer gets
    409 and the real answer still settles."""
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    the_key = _key()
    other_key = _key(id=uuid.uuid4())
    clientside.hub.revoke(the_key.id)
    clientside.hub.revoke(other_key.id)
    import pcai_llm.api.auth as auth_mod

    current = {"k": the_key}

    async def pinned_auth(p: str) -> KeyRecord:
        return current["k"]

    monkeypatch.setattr(auth_mod, "authenticate", pinned_auth)
    app = FastAPI()
    app.include_router(routes_dispatch.router)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            assert c.post("/v1/dispatch/register", json=BASH_OFFER, headers=HEADERS).status_code == 200
            task = asyncio.ensure_future(clientside.hub.dispatch(the_key.id, "bash", {}))
            job = await _poll_once(c)
            assert job is not None
            current["k"] = other_key
            r = c.post(
                "/v1/dispatch/result",
                json={"dispatch_id": job["dispatch_id"], "ok": True, "content": "foreign"},
                headers=HEADERS,
            )
            assert r.status_code == 409
            current["k"] = the_key
            r = c.post(
                "/v1/dispatch/result",
                json={"dispatch_id": job["dispatch_id"], "ok": True, "content": "mine"},
                headers=HEADERS,
            )
            assert r.status_code == 200
            ok, content = await asyncio.wait_for(task, timeout=5)
            assert ok and content == "mine"
    finally:
        clientside.hub.revoke(the_key.id)
        clientside.hub.revoke(other_key.id)


async def _poll_once(c: TestClient, timeout_s: float = 5.0) -> dict | None:
    """Claim attempt with retries: the dispatch task enqueues at its next
    await point, so the first (sync) poll can legitimately 204. Mirrors a
    real client's poll loop."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = c.get("/v1/dispatch/poll?wait_s=0", headers=HEADERS)
        if r.status_code == 200:
            return r.json()
        await asyncio.sleep(0.01)
    return None


# ---------------------------------------------------------------------------
# auth failures on every route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/v1/dispatch/register"),
        ("get", "/v1/dispatch/poll"),
        ("post", "/v1/dispatch/result"),
        ("get", "/v1/dispatch/offer"),
    ],
)
def test_missing_key_is_401(client: TestClient, method: str, path: str) -> None:
    r = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
    assert r.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/v1/dispatch/register"),
        ("get", "/v1/dispatch/poll"),
        ("post", "/v1/dispatch/result"),
        ("get", "/v1/dispatch/offer"),
    ],
)
def test_wrong_scope_is_403(monkeypatch: pytest.MonkeyPatch, method: str, path: str) -> None:
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    clientside.hub.revoke(KEY_A)
    import pcai_llm.api.auth as auth_mod

    # A key WITHOUT the proxy scope: 403.
    async def no_proxy_auth(p: str) -> KeyRecord:
        return _key(scopes=["mcp"])

    monkeypatch.setattr(auth_mod, "authenticate", no_proxy_auth)
    app = FastAPI()
    app.include_router(routes_dispatch.router)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = getattr(c, method)(path, **({"json": {}} if method == "post" else {}), headers=HEADERS)
        assert r.status_code == 403
    clientside.hub.revoke(KEY_A)


def test_flag_off_surface_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inert in prod: flag off -> every dispatch route 404s (the surface
    must not even advertise itself)."""
    monkeypatch.setattr(settings, "client_tool_dispatch", False)
    import pcai_llm.api.auth as auth_mod

    async def fake_auth(p: str) -> KeyRecord:
        return _key()

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    app = FastAPI()
    app.include_router(routes_dispatch.router)
    with TestClient(app, raise_server_exceptions=False) as c:
        assert c.post("/v1/dispatch/register", json=BASH_OFFER, headers=HEADERS).status_code == 404
        assert c.get("/v1/dispatch/poll", headers=HEADERS).status_code == 404


# ---------------------------------------------------------------------------
# validation + bounded failure
# ---------------------------------------------------------------------------


def test_register_validation(client: TestClient) -> None:
    assert client.post("/v1/dispatch/register", json={"tools": []}, headers=HEADERS).status_code == 400
    bad = {"tools": [{"name": "bad name!", "description": "x"}]}
    assert client.post("/v1/dispatch/register", json=bad, headers=HEADERS).status_code == 400
    big = {"tools": [{"name": "bash", "inputSchema": {"x": "y" * 40000}}]}
    assert client.post("/v1/dispatch/register", json=big, headers=HEADERS).status_code == 413


async def test_offline_client_times_out_as_tool_error(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE never-a-hang guarantee: nobody polls, nobody answers — the
    dispatch returns a tool ERROR after the bounded timeout."""
    _register(client)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 1.0)
    started = time.monotonic()
    ok, content = await clientside.hub.dispatch(KEY_A, "bash", {"command": "sleep"})
    elapsed = time.monotonic() - started
    assert not ok and "timed out" in content
    assert 0.5 <= elapsed < 5, "timeout must be bounded, not immediate, not unbounded"


async def test_per_tool_timeout_clamped_to_knob(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-tool timeout_s cannot extend past the gateway knob."""
    _register(client)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 1.0)
    offer = clientside.hub.offer_lookup(KEY_A)
    assert offer is not None
    # The offer declares 30s; the knob clamps to 1s — bounded by the knob.
    started = time.monotonic()
    ok, content = await clientside.hub.dispatch(KEY_A, "bash", {})
    assert not ok and "timed out" in content
    assert time.monotonic() - started < 5


async def test_queue_overflow_refuses(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A client that stopped polling accumulates at most the queue bound;
    new dispatches refuse immediately instead of piling up."""
    _register(client)
    monkeypatch.setattr(clientside, "MAX_PENDING_PER_KEY", 3)
    tasks = [asyncio.ensure_future(clientside.hub.dispatch(KEY_A, "bash", {"i": i})) for i in range(3)]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and clientside.hub.pending_count(KEY_A) < 3:
        await asyncio.sleep(0.01)
    ok, content = await clientside.hub.dispatch(KEY_A, "bash", {"i": "overflow"})
    assert not ok and "queue full" in content
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# the executor adapter: same gate discipline as _gated_mcp_call
# ---------------------------------------------------------------------------


class _Gate:
    def __init__(self, ok: bool, reason: str = "") -> None:
        self.ok = ok
        self.reason = reason


async def test_executor_gates_rate_and_settles_local() -> None:
    """The executor's gate chain with a NO-OP transport double: the
    dispatch is answered by a concurrent hub-settler (standing in for the
    polling client), so the test observes the settle endpoint + trace
    status around a REAL dispatch cycle."""
    settled: list[tuple] = []
    traces_seen: list[tuple] = []

    async def rate_ok(key_id, rpm, tpm, est):
        return _Gate(True)

    async def rate_exhausted(key_id, rpm, tpm, est):
        return _Gate(False, "RPM limit 5 exceeded")

    async def settle(key_id, model, endpoint, prompt, completion, status, latency, request_id):
        settled.append((endpoint, status))

    async def trace(tool, status, ms):
        traces_seen.append((tool, status, ms))

    clientside.hub.offer(KEY_A, [clientside.ClientTool(name="bash", description="shell")], 900)
    exec_fn = clientside.build_client_tool_executor(KEY_A, "req-1", rate_gate=rate_ok, settle_fn=settle, trace_fn=trace)

    async def answer_when_claimed() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = clientside.hub.claim(KEY_A)
            if job is not None:
                assert clientside.hub.settle_result(KEY_A, job.dispatch_id, True, "out")
                return
            await asyncio.sleep(0.01)

    answerer = asyncio.ensure_future(answer_when_claimed())
    ok, content = await asyncio.wait_for(exec_fn(clientside.CLIENT_PSEUDO_SERVER, "bash", {"command": "ls"}), 5)
    await asyncio.wait_for(answerer, 5)
    assert ok and content == "out"
    assert ("local:bash", "ok") in settled, "must settle under local:<tool>"
    assert traces_seen and traces_seen[0][0] == "bash" and traces_seen[0][1] == "ok"

    settled.clear()
    exec_denied = clientside.build_client_tool_executor(
        KEY_A, "req-2", rate_gate=rate_exhausted, settle_fn=settle, trace_fn=trace
    )
    ok, content = await exec_denied(clientside.CLIENT_PSEUDO_SERVER, "bash", {})
    assert not ok and "rate limited" in content
    assert ("local:bash", "error") in settled, "refusals settle too (ledger truth)"


async def test_executor_loop_guard_denies() -> None:
    async def rate_ok(key_id, rpm, tpm, est):
        return _Gate(True)

    async def settle(key_id, model, endpoint, *rest):
        pass

    def guard(tool, args):
        return "loop guard: identical call bash made 3x"

    clientside.hub.offer(KEY_A, [clientside.ClientTool(name="bash")], 900)
    exec_fn = clientside.build_client_tool_executor(
        KEY_A, "req-3", rate_gate=rate_ok, settle_fn=settle, loop_guard_fn=guard
    )
    ok, content = await exec_fn(clientside.CLIENT_PSEUDO_SERVER, "bash", {})
    assert not ok and "loop guard" in content


async def test_executor_wall_clock_clamp(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch timeout is clamped to the relay's remaining wall
    clock — a client tool can never extend the relay's clean-close
    guarantee."""
    _register(client)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 60.0)
    exec_fn = clientside.build_client_tool_executor(
        KEY_A,
        "req-4",
        rate_gate=lambda *a: _async_gate(),
        settle_fn=lambda *a, **k: None,
        wall_clock_deadline=time.monotonic() + 1.0,
    )
    started = time.monotonic()
    ok, content = await exec_fn(clientside.CLIENT_PSEUDO_SERVER, "bash", {})
    assert not ok and "timed out" in content
    assert time.monotonic() - started < 5


async def _async_gate() -> Any:
    return _Gate(True)


# ---------------------------------------------------------------------------
# THE relay integration test — the wave's whole point
# ---------------------------------------------------------------------------


def _scripted_model(calls: list[list[dict]]) -> Any:
    """stream_once factory: one scripted SSE model call per invocation.
    Chunk shapes: {"tool_calls": [ ... ]} or {"content": "text"}."""

    calls_seen = [0]  # FACTORY scope: advances across the relay loop's calls

    def stream_once(msgs: list[dict], tools: list[dict] | None):
        n = calls_seen[0]
        calls_seen[0] += 1
        chunks = calls[min(n, len(calls) - 1)]

        async def gen():
            for c in chunks:
                if "tool_calls" in c:
                    yield ("tool_calls", c["tool_calls"])
                else:
                    yield ("content", c.get("content", ""))
            yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

        return gen()

    return stream_once


def test_relayed_turn_dispatches_client_tool_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WAVE'S WHOLE POINT: the model calls `client__bash`; the "client"
    (test double) polls over the REAL HTTP route and answers; the stream
    CONTINUES — the final chunk carries finish_reason=stop and the answer,
    NOT finish_reason=tool_calls. No handback, no re-send loop."""
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 10.0)
    clientside.hub.revoke(KEY_A)
    clientside.hub.offer(
        KEY_A,
        [
            clientside.ClientTool(
                name="bash",
                description="Run a shell command",
                input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            )
        ],
        900,
    )

    import pcai_llm.api.auth as auth_mod

    async def fake_auth(p: str) -> KeyRecord:
        return _key()

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    app = FastAPI()
    app.include_router(routes_dispatch.router)
    http = TestClient(app, raise_server_exceptions=False)

    model_calls: list[list[dict[str, Any]]] = [
        # iteration 0: the model calls the CLIENT tool (dispatched DOWN)
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "client__bash", "arguments": '{"command": "ls -la"}'},
                    }
                ]
            }
        ],
        # iteration 1: with the tool result IN the conversation, the model answers
        [{"content": "The directory listing shows 4 entries."}],
    ]

    settled: list[tuple] = []

    async def settle(key_id, model, endpoint, prompt, completion, status, latency, request_id):
        settled.append((endpoint, status))

    async def rate_ok(key_id, rpm, tpm, est):
        return _Gate(True)

    async def trace(tool, status, ms):
        pass

    exec_fn = clientside.build_client_tool_executor(
        KEY_A, "req-relay-1", rate_gate=rate_ok, settle_fn=settle, trace_fn=trace
    )

    async def scenario():
        relay_task = asyncio.ensure_future(
            _collect(
                toolrelay.relay_stream(
                    [{"role": "user", "content": "list my files"}],
                    GRANTS,
                    CATALOG,
                    _scripted_model(model_calls),
                    lambda *a: _noop_exec(),  # MCP executor — must NOT be reached
                    "req-relay-1",
                    "test-model",
                    client_tools=[
                        {
                            "type": "function",
                            "function": {
                                "name": "client__bash",
                                "description": "Run a shell command",
                                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                            },
                        }
                    ],
                    client_tool_execute=exec_fn,
                )
            )
        )
        # The "client": poll + answer, exactly what DSH's plugin will do.
        answered = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not answered:
            r = http.get("/v1/dispatch/poll?wait_s=0", headers=HEADERS)
            if r.status_code == 200:
                job = r.json()
                assert job["tool"] == "bash" and job["arguments"] == {"command": "ls -la"}
                a = http.post(
                    "/v1/dispatch/result",
                    json={"dispatch_id": job["dispatch_id"], "ok": True, "content": "file-a.txt\nfile-b.txt"},
                    headers=HEADERS,
                )
                assert a.status_code == 200
                answered = True
                break
            await asyncio.sleep(0.01)
        assert answered, "dispatch never polled"
        return await asyncio.wait_for(relay_task, timeout=10)

    chunks = asyncio.run(scenario())
    text = b"".join(chunks).decode()

    # 1. The tool_call event carries server="client" (trace/console story)
    assert '"event": "tool_call"' in text
    assert '"server": "client"' in text and '"tool": "bash"' in text and '"ok": true' in text
    # 2. The tool result landed in the loop (pcai event preview)...
    assert "file-a.txt" in text
    # 3. ...and in the model's conversation (second model call receives it)
    assert "The directory listing shows 4 entries." in text
    # 4. NO handback: the stream does NOT end with finish_reason=tool_calls
    assert '"finish_reason": "tool_calls"' not in text
    assert '"finish_reason": "stop"' in text
    assert text.rstrip().endswith("data: [DONE]")
    # 5. Ledger truth: one settle under local:bash
    assert ("local:bash", "ok") in settled


async def _noop_exec() -> tuple[bool, Any]:
    raise AssertionError("MCP executor must not be reached for client pseudo-server calls")


async def _collect(agen: Any) -> list[bytes]:
    return [c async for c in agen]


def test_relayed_turn_client_denial_flows_back_as_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client's local approval policy REFUSES (ok=false): the refusal
    text becomes the model's role=tool error and the loop CONTINUES —
    the model self-corrects instead of hanging or handing back."""
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 10.0)
    clientside.hub.revoke(KEY_A)
    clientside.hub.offer(KEY_A, [clientside.ClientTool(name="bash", description="shell")], 900)

    import pcai_llm.api.auth as auth_mod

    async def fake_auth(p: str) -> KeyRecord:
        return _key()

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    app = FastAPI()
    app.include_router(routes_dispatch.router)
    http = TestClient(app, raise_server_exceptions=False)

    model_calls: list[list[dict[str, Any]]] = [
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "client__bash", "arguments": '{"command": "rm -rf /"}'},
                    }
                ]
            }
        ],
        [{"content": "Understood — I won't run destructive commands."}],
    ]

    async def rate_ok(key_id, rpm, tpm, est):
        return _Gate(True)

    async def settle(*a, **k):
        pass

    exec_fn = clientside.build_client_tool_executor(KEY_A, "req-relay-2", rate_gate=rate_ok, settle_fn=settle)

    async def scenario():
        relay_task = asyncio.ensure_future(
            _collect(
                toolrelay.relay_stream(
                    [{"role": "user", "content": "clean my disk"}],
                    set(),
                    [],
                    _scripted_model(model_calls),
                    lambda *a: _noop_exec(),
                    "req-relay-2",
                    "test-model",
                    client_tools=[{"type": "function", "function": {"name": "client__bash", "description": "shell"}}],
                    client_tool_execute=exec_fn,
                )
            )
        )
        answered = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not answered:
            r = http.get("/v1/dispatch/poll?wait_s=0", headers=HEADERS)
            if r.status_code == 200:
                job = r.json()
                a = http.post(
                    "/v1/dispatch/result",
                    json={"dispatch_id": job["dispatch_id"], "ok": False, "error": "approval denied by local policy"},
                    headers=HEADERS,
                )
                assert a.status_code == 200
                answered = True
                break
            await asyncio.sleep(0.01)
        assert answered
        return await asyncio.wait_for(relay_task, timeout=10)

    chunks = asyncio.run(scenario())
    text = b"".join(chunks).decode()
    assert "approval denied by local policy" in text  # refusal reached the model
    assert '"ok": false' in text  # ...as a failed tool_call event
    assert '"finish_reason": "tool_calls"' not in text  # loop CONTINUED
    assert "won't run destructive commands" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relayed_turn_offline_client_yields_error_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    """Client offline: the dispatch times out into a tool error and the
    relay still answers — never a hang, never a handback."""
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 1.0)
    clientside.hub.revoke(KEY_A)
    clientside.hub.offer(KEY_A, [clientside.ClientTool(name="bash")], 900)

    model_calls: list[list[dict[str, Any]]] = [
        [{"tool_calls": [{"id": "c1", "type": "function", "function": {"name": "client__bash", "arguments": "{}"}}]}],
        [{"content": "Your machine is unreachable; nothing was run."}],
    ]

    async def rate_ok(key_id, rpm, tpm, est):
        return _Gate(True)

    async def settle(*a, **k):
        pass

    exec_fn = clientside.build_client_tool_executor(KEY_A, "req-relay-3", rate_gate=rate_ok, settle_fn=settle)

    chunks = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                _scripted_model(model_calls),
                lambda *a: _noop_exec(),
                "req-relay-3",
                "test-model",
                client_tools=[{"type": "function", "function": {"name": "client__bash"}}],
                client_tool_execute=exec_fn,
            )
        )
    )
    text = b"".join(chunks).decode()
    assert "timed out" in text
    assert '"ok": false' in text
    assert '"finish_reason": "tool_calls"' not in text
    assert "unreachable" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_no_executor_client_call_handbacks_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Byte-compatibility guard: WITHOUT the dispatch executor, a
    client-offered tool call still handbacks exactly as before (the flag
    keeps prod behavior inert even if an offer somehow exists)."""
    model_calls: list[list[dict[str, Any]]] = [
        [
            {
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}
                ]
            }
        ],
        [{"content": "done"}],
    ]

    async def noop(server, tool, args):
        raise AssertionError("no executor should run")

    chunks = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                _scripted_model(model_calls),
                noop,
                "req-legacy",
                "test-model",
                client_tools=[{"type": "function", "function": {"name": "bash"}}],
            )
        )
    )
    text = b"".join(chunks).decode()
    assert '"finish_reason": "tool_calls"' in text  # handback preserved
    assert '"event": "handback"' in text


def test_dispatch_executor_mixed_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MIXED batch (client tool + in-cluster MCP tool) with dispatch
    armed: the MCP half runs in-cluster, the client half dispatches, and
    the loop CONTINUES on both results — no partial handback."""
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 10.0)
    clientside.hub.revoke(KEY_A)
    clientside.hub.offer(KEY_A, [clientside.ClientTool(name="bash")], 900)

    import pcai_llm.api.auth as auth_mod

    async def fake_auth(p: str) -> KeyRecord:
        return _key()

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    app = FastAPI()
    app.include_router(routes_dispatch.router)
    http = TestClient(app, raise_server_exceptions=False)

    model_calls: list[list[dict[str, Any]]] = [
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "logsearch__search_logs", "arguments": '{"q": "gpu"}'},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "client__bash", "arguments": '{"command": "df -h"}'},
                    },
                ]
            }
        ],
        [{"content": "Both results are in."}],
    ]

    executed: list[tuple[str, str]] = []

    async def mcp_exec(server, tool, args):
        executed.append((server, tool))
        return True, {"found": "stuff"}

    async def rate_ok(key_id, rpm, tpm, est):
        return _Gate(True)

    async def settle(*a, **k):
        pass

    exec_fn = clientside.build_client_tool_executor(KEY_A, "req-mixed", rate_gate=rate_ok, settle_fn=settle)

    async def scenario():
        relay_task = asyncio.ensure_future(
            _collect(
                toolrelay.relay_stream(
                    [{"role": "user", "content": "check the disk"}],
                    GRANTS,
                    CATALOG,
                    _scripted_model(model_calls),
                    mcp_exec,
                    "req-mixed",
                    "test-model",
                    client_tools=[{"type": "function", "function": {"name": "client__bash", "description": "shell"}}],
                    client_tool_execute=exec_fn,
                )
            )
        )
        answered = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not answered:
            r = http.get("/v1/dispatch/poll?wait_s=0", headers=HEADERS)
            if r.status_code == 200:
                job = r.json()
                assert job["tool"] == "bash"
                assert (
                    http.post(
                        "/v1/dispatch/result",
                        json={"dispatch_id": job["dispatch_id"], "ok": True, "content": "/dev/sda1 80%"},
                        headers=HEADERS,
                    ).status_code
                    == 200
                )
                answered = True
                break
            await asyncio.sleep(0.01)
        assert answered
        return await asyncio.wait_for(relay_task, timeout=10)

    chunks = asyncio.run(scenario())
    text = b"".join(chunks).decode()
    assert executed == [("logsearch", "search_logs")]  # MCP half in-cluster
    assert '"server": "client"' in text and '"server": "logsearch"' in text  # both halves happened
    assert "80%" in text  # dispatch result reached the model
    assert '"finish_reason": "tool_calls"' not in text  # NO handback even mixed
    assert "Both results are in." in text
    assert text.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# WAVE B2 — production integration
# ---------------------------------------------------------------------------


DISPATCH_ROUTES = [
    ("post", "/v1/dispatch/register"),
    ("get", "/v1/dispatch/poll"),
    ("post", "/v1/dispatch/result"),
    ("get", "/v1/dispatch/offer"),
]


def test_flag_on_mounts_dispatch_routes__flag_off_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2 mounting: create_app() with the flag ON exposes all 4 dispatch
    routes (401 without a key — mounted and auth-gated); with the flag OFF
    (the production default) the surface is UNMOUNTED: plain 404 from an
    app that never included the router."""
    from pcai_llm.app import create_app

    # Flag ON: mounted. require_scope 401s before anything else (no key) —
    # that PROVES the route exists and is wired to the shared auth dep.
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    app = create_app()
    for method, path in DISPATCH_ROUTES:
        r = getattr(TestClient(app), method)(path, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 401, f"{method} {path}: mounted surface must be auth-gated, got {r.status_code}"

    # Flag OFF (default): NOT mounted — plain 404s, the surface is inert.
    monkeypatch.setattr(settings, "client_tool_dispatch", False)
    app = create_app()
    for method, path in DISPATCH_ROUTES:
        r = getattr(TestClient(app), method)(path, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 404, f"{method} {path}: flag-off must be unmounted, got {r.status_code}"


def test_b2_wiring_failsafe_offer_lookup_raises_handbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2 FAIL-SAFE: a crashing offer lookup (or any wiring fault) must
    never break the relay turn — the request proceeds with
    client_tool_execute=None and the model's client__tool call HANDS BACK
    (finish_reason=tool_calls), exactly today's behavior."""
    from pcai_llm.api import routes_proxy
    from pcai_llm.app import create_app
    from pcai_llm.core import budgets as budgets_mod
    from pcai_llm.core import costs as costs_mod
    from pcai_llm.mcpf import federation
    from pcai_llm.routing.router import Endpoint, Router

    key = _key()
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 10.0)
    clientside.hub.revoke(KEY_A)  # no offer — irrelevant anyway, lookup raises

    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return GRANTS

    async def fake_fetch_tools(url: str, headers: dict, timeout_s: float | None = None):
        return [{"tool": "search_logs", "description": "search"}]

    class FakeDB:
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
            return "INSERT 0 1"

    fake = FakeDB()
    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)

    monkeypatch.setattr(federation, "db", fake)
    monkeypatch.setattr(federation, "load_grants", fake_grants)
    import pcai_llm.api.routes_mcp as routes_mcp_mod

    monkeypatch.setattr(routes_mcp_mod, "_fetch_tools", fake_fetch_tools)
    from pcai_llm.core import registry as registry_mod

    registry_mod._mcp_servers_cache = [
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

    model_calls: list[list[dict[str, Any]]] = [
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "client__bash", "arguments": '{"command": "ls"}'},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
        [{"choices": [{"delta": {"content": "handed back, client will answer"}}]}],
    ]
    r = Router.with_mock(_model_stream_sse(model_calls))
    r.set_endpoints(MODEL_B2, [Endpoint(id="ep1", model_name=MODEL_B2, base_url="http://fake/v1", model_type="chat")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    routes_proxy._in_flight.clear()
    routes_proxy._loop_guard.clear()
    from pcai_llm.api.routes_proxy import catalog_cache_clear

    catalog_cache_clear()

    # THE fault: the offer lookup itself explodes.
    def boom(key_id: Any) -> Any:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(clientside.hub, "offer_lookup", boom)

    app = create_app()
    # NOTE: TestClient WITHOUT `with` never runs the app lifespan — exactly
    # what these app-level tests want (no live DB; every DB surface the
    # request touches is monkeypatched above). Same pattern as test_ui.py.
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post(
        "/v1/chat/completions",
        json={
            "model": MODEL_B2,
            "messages": [{"role": "user", "content": "list my files"}],
            "stream": True,
            # DSH's real shape: the harness offers its tools as plain OpenAI
            # function tools. With the executor de-armed by the fault, the
            # model's call on one of them handbacks (offer-based dispatch
            # needs the executor; a plain-name call takes the mixed-mode
            # handback path).
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "client__bash",
                        "description": "Run a shell command",
                        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                    },
                }
            ],
        },
        headers={"Authorization": "Bearer x", "X-PCAI-Execute-Tools": "1", "X-PCAI-Conversation": "raw"},
    )
    assert r.status_code == 200, r.text
    text = r.text
    # The turn was NOT killed: it streamed to completion exactly as a
    # flag-off request would — the handback event fires and the stream ends
    # with finish_reason=tool_calls + [DONE]: the harness gets its tool call
    # back to execute itself, byte-for-byte today's behavior.
    assert '"event": "handback"' in text
    assert '"finish_reason": "tool_calls"' in text
    assert text.rstrip().endswith("data: [DONE]")


def test_b2_deadline_passed_to_executor_and_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2 deadline decision: the routes layer computes ONE wall-clock
    deadline (toolrelay.wall_clock_deadline) and passes it BOTH to the
    executor (the dispatch's bounded wait is clamped to the REMAINING
    budget) and to relay_stream (which adopts it as its guard deadline).

    Proven here through the REAL mounted route: a WEDGED client (claims the
    job, never answers) + a deadline already inside its last second -> the
    relay finishes BEFORE the per-tool/knob timeout would allow, i.e. the
    dispatch waited only out the remaining wall clock. Also proves the guard
    adopted the passed deadline (its expiry closes the turn cleanly, no
    504-shaped hang)."""
    from pcai_llm.api import routes_proxy
    from pcai_llm.app import create_app
    from pcai_llm.core import budgets as budgets_mod
    from pcai_llm.core import costs as costs_mod
    from pcai_llm.core import registry as registry_mod
    from pcai_llm.mcpf import federation
    from pcai_llm.routing.router import Endpoint, Router

    key = _key()
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    # The knob + per-tool timeout are LARGE: without the wall-clock clamp the
    # dispatch would wait 60s for the wedged client.
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 60.0)
    clientside.hub.revoke(KEY_A)
    clientside.hub.offer(
        KEY_A,
        [clientside.ClientTool(name="bash", description="shell", timeout_s=60)],
        900,
    )

    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return GRANTS

    async def fake_fetch_tools(url: str, headers: dict, timeout_s: float | None = None):
        return [{"tool": "search_logs", "description": "search"}]

    class FakeDB:
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
            return "INSERT 0 1"

    fake = FakeDB()
    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    monkeypatch.setattr(federation, "db", fake)
    monkeypatch.setattr(federation, "load_grants", fake_grants)
    import pcai_llm.api.routes_mcp as routes_mcp_mod

    monkeypatch.setattr(routes_mcp_mod, "_fetch_tools", fake_fetch_tools)
    registry_mod._mcp_servers_cache = [
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

    model_calls: list[list[dict[str, Any]]] = [
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "client__bash", "arguments": '{"command": "ls"}'},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
        # second call: ANOTHER tool call — the guard then evaluates against
        # the (already expired) passed deadline and trips, proving relay_stream
        # ADOPTED the routes-layer deadline rather than computing its own.
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c2",
                                    "type": "function",
                                    "function": {"name": "client__bash", "arguments": '{"command": "pwd"}'},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    ]
    r = Router.with_mock(_model_stream_sse(model_calls))
    r.set_endpoints(MODEL_B2, [Endpoint(id="ep1", model_name=MODEL_B2, base_url="http://fake/v1", model_type="chat")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    routes_proxy._in_flight.clear()
    routes_proxy._loop_guard.clear()
    from pcai_llm.api.routes_proxy import catalog_cache_clear

    catalog_cache_clear()

    # THE WEDGED CLIENT: nobody ever polls or answers. The hub spy counts
    # ENQUEUED dispatches — proof the dispatch was minted toward the client
    # (the relay end of the reverse channel) and then bounded by the deadline.
    wedged = {"minted": 0}

    class WedgedHub(clientside.ClientToolHub):
        async def dispatch(self, key_id, tool, arguments, timeout_s=None):  # type: ignore[override]
            wedged["minted"] += 1
            wedged["timeout_s"] = timeout_s
            return await super().dispatch(key_id, tool, arguments, timeout_s=timeout_s)

    wedged_hub = WedgedHub()
    monkeypatch.setattr(clientside, "hub", wedged_hub)
    wedged_hub.revoke(KEY_A)
    wedged_hub.offer(
        KEY_A,
        [clientside.ClientTool(name="bash", description="shell", timeout_s=60)],
        900,
    )

    # The relay deadline is already INSIDE its last second: the dispatch's
    # remaining budget is ~0 (clamped to the 1s floor), so the executor
    # returns its timeout error almost immediately and the guard (same
    # deadline) closes the turn — total elapsed far below the 60s knob.
    monkeypatch.setattr(
        routes_proxy.toolrelay,
        "wall_clock_deadline",
        lambda *a, **k: time.monotonic() + 0.4,
    )

    app = create_app()
    tc = TestClient(app, raise_server_exceptions=False)
    t0 = time.monotonic()
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL_B2, "messages": [{"role": "user", "content": "list my files"}], "stream": True},
        headers={"Authorization": "Bearer x", "X-PCAI-Execute-Tools": "1", "X-PCAI-Conversation": "raw"},
    )
    elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    text = r.text
    # The dispatch WAS minted (the reverse channel's relay end ran), with a
    # timeout clamped to the remaining wall clock (<=1s floor), NOT the 60s
    # knob or the per-tool 60s.
    assert wedged["minted"] == 1, "exactly one client dispatch must have been minted"
    assert wedged["timeout_s"] <= 1.0, (
        f"dispatch timeout {wedged['timeout_s']} not clamped to the passed wall-clock deadline"
    )
    # ...and the whole turn closed on that deadline, far under the knob:
    assert elapsed < 15, f"relay waited {elapsed:.1f}s — the wall-clock clamp did not reach the executor"
    assert '"event": "tool_call"' in text and '"ok": false' in text
    # The guard ADOPTED the passed deadline (it trips before the second call
    # executes — the wall clock it evaluates is the routes-computed one).
    assert '"event": "guard"' in text
    assert "wall-clock" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_b2_production_path_dispatch_through_real_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE B2 DELIVERABLE — the full production path through the REAL app:

    create_app() with the flag ON -> POST /v1/chat/completions (relay header)
    -> _relay_catalog offers client__bash from the KEY'S LIVE OFFER (no
    hand-written client_tools in the body) -> the model's scripted call names
    client__bash -> routes_proxy's executor dispatches DOWN the long-poll
    channel -> the test-double client (the DSH plugin's behavior) polls the
    REAL mounted route, answers over /v1/dispatch/result -> the stream
    CONTINUES to finish_reason=stop with the tool result in the conversation.

    This is the wave-B spike's scenario proven at the app level: nothing is
    wired by hand — the executor, the catalog entry, and the deadline all
    come from the production wiring."""
    from pcai_llm.api import routes_proxy
    from pcai_llm.app import create_app
    from pcai_llm.core import budgets as budgets_mod
    from pcai_llm.core import costs as costs_mod
    from pcai_llm.core import registry as registry_mod
    from pcai_llm.mcpf import federation
    from pcai_llm.routing.router import Endpoint, Router

    key = _key()
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    monkeypatch.setattr(settings, "client_tool_dispatch_timeout_s", 10.0)
    # The catalog TTL cache is process state — cold-start each app-level test.
    from pcai_llm.api.routes_proxy import catalog_cache_clear

    catalog_cache_clear()

    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return GRANTS

    async def fake_fetch_tools(url: str, headers: dict, timeout_s: float | None = None):
        return [{"tool": "search_logs", "description": "search"}]

    class FakeDB:
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
            return "INSERT 0 1"

    fake = FakeDB()
    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    monkeypatch.setattr(federation, "db", fake)
    monkeypatch.setattr(federation, "load_grants", fake_grants)
    import pcai_llm.api.routes_mcp as routes_mcp_mod

    monkeypatch.setattr(routes_mcp_mod, "_fetch_tools", fake_fetch_tools)
    registry_mod._mcp_servers_cache = [
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

    model_calls: list[list[dict[str, Any]]] = [
        # iteration 0: the model calls the CLIENT tool (it was OFFERED from
        # the key's live offer via the production wiring)
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "client__bash", "arguments": '{"command": "ls -la"}'},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
        # iteration 1: the answer, once the tool result is in the loop
        [
            {
                "choices": [{"delta": {"content": "The directory listing shows 2 entries."}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            }
        ],
    ]
    r = Router.with_mock(_model_stream_sse(model_calls))
    r.set_endpoints(MODEL_B2, [Endpoint(id="ep1", model_name=MODEL_B2, base_url="http://fake/v1", model_type="chat")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    routes_proxy._in_flight.clear()
    routes_proxy._loop_guard.clear()
    catalog_cache_clear()

    # The "client" (DSH plugin's exact behavior) registers its offer over the
    # REAL mounted route, then polls + answers in a background thread while
    # the relayed request streams on this thread.
    #
    # SHARED-PORTAL NOTE: the client thread and the relay request run
    # CONCURRENTLY against the same app, and the dispatch future is resolved
    # from the result-POST handler while hub.dispatch awaits it inside the
    # relay stream. `with TestClient(...)` gives every request the SAME
    # event loop (one portal), so the answer wakes the waiting relay
    # immediately — without it each request gets its own portal/loop and the
    # waiter only observes the answer at its timeout expiry (correct result,
    # multi-second latency). The lifespan runs under `with`, so the two DB
    # lifecycle hooks become no-ops and registry.refresh is stubbed (it
    # would otherwise clobber the mock pools faked above).
    import pcai_llm.db as db_mod

    async def fake_connect(self):  # pragma: no cover - trivial
        self.pool = None

    async def fake_close(self):  # pragma: no cover - trivial
        self.pool = None

    monkeypatch.setattr(db_mod.db, "connect", fake_connect.__get__(db_mod.db))
    monkeypatch.setattr(db_mod.db, "close", fake_close.__get__(db_mod.db))
    import pcai_llm.core.registry as registry_pkg

    async def no_refresh() -> None:
        pass

    monkeypatch.setattr(registry_pkg, "refresh", no_refresh)

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as http:
        reg = http.post(
            "/v1/dispatch/register",
            json={
                "tools": [
                    {
                        "name": "bash",
                        "description": "Run a shell command",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                        "timeout_s": 10,
                    }
                ],
                "ttl_s": 900,
            },
            headers=HEADERS,
        )
        assert reg.status_code == 200, reg.text
        assert reg.json()["tools"] == ["bash"]

        answered = threading.Event()
        poll_errors: list[str] = []

        def client_loop() -> None:
            """poll (long-poll, like the plugin) -> answer -> done."""
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not answered.is_set():
                try:
                    rp = http.get("/v1/dispatch/poll?wait_s=1", headers=HEADERS)
                except Exception as exc:  # pragma: no cover - transport hiccups
                    poll_errors.append(str(exc))
                    time.sleep(0.05)
                    continue
                if rp.status_code == 204:
                    continue
                if rp.status_code != 200:
                    poll_errors.append(f"poll status {rp.status_code}")
                    time.sleep(0.05)
                    continue
                job = rp.json()
                assert job["tool"] == "bash" and job["arguments"] == {"command": "ls -la"}
                a = http.post(
                    "/v1/dispatch/result",
                    json={"dispatch_id": job["dispatch_id"], "ok": True, "content": "file-a.txt\nfile-b.txt"},
                    headers=HEADERS,
                )
                assert a.status_code == 200, a.text
                answered.set()
                return

        worker = threading.Thread(target=client_loop, daemon=True)
        worker.start()
        try:
            r = http.post(
                "/v1/chat/completions",
                json={"model": MODEL_B2, "messages": [{"role": "user", "content": "list my files"}], "stream": True},
                headers={"Authorization": "Bearer x", "X-PCAI-Execute-Tools": "1", "X-PCAI-Conversation": "raw"},
            )
        finally:
            answered.wait(5)
            worker.join(5)
        assert r.status_code == 200, r.text
        assert answered.is_set(), f"test-double client never got the dispatch; poll_errors={poll_errors}"
        assert not poll_errors, f"poll errors: {poll_errors}"
        text = r.text

        # 1. The tool was OFFERED from the live offer and EXECUTED via dispatch
        assert '"event": "tool_call"' in text
        assert '"server": "client"' in text and '"tool": "bash"' in text and '"ok": true' in text
        # 2. The result returned INTO the loop: model's answer reflects it
        assert "file-a.txt" in text
        assert "The directory listing shows 2 entries." in text
        # 3. NO handback — finish_reason is stop, the stream completed in ONE request
        assert '"finish_reason": "tool_calls"' not in text
        assert '"finish_reason": "stop"' in text
        assert text.rstrip().endswith("data: [DONE]")
