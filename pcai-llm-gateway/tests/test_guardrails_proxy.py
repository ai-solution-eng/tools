"""Guardrail + MCP request-body opt-in behavior at the PROXY boundary.

The full chat pipeline (cascade → pre_call guardrails → gates → upstream →
post_call guardrails → settle) is exercised against a mocked upstream and
monkeypatched seams. The guardrails ENGINE is faked here on purpose: these
tests pin the WIRING (where hooks run, what lands in the ledger, what the
client sees) — the engine's own semantics are covered by
test_guardrails_core.py, the admin surface by test_guardrails_admin.py.
No live Postgres, no live MCP servers.
"""

from __future__ import annotations

import json
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
from pcai_llm.routing.router import Endpoint, Router

MODEL = "test-model"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeDB:
    """Budget/ledger stub: no budgets configured, records every settle."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def execute(self, sql: str, *args: Any):
        self.settled.append(args)
        return "INSERT 0 1"


class FakeBlocked(Exception):
    """Mirror of core.guardrails.GuardrailBlocked for wiring tests."""

    def __init__(self, name: str, mode: str, detail: str, engine: str = "blocklist", reply_text: str = "") -> None:
        super().__init__(f"blocked by guardrail '{name}': {detail}")
        self.name, self.mode, self.detail, self.engine, self.reply_text = name, mode, detail, engine, reply_text


def _ev(name: str, mode: str, engine: str, action: str, detail: str = "") -> Any:
    return SimpleNamespaceEvent(name, mode, engine, action, detail)


class SimpleNamespaceEvent:
    """Mirror of core.guardrails.GuardrailEvent: attribute access + as_dict."""

    def __init__(self, name: str, mode: str, engine: str, action: str, detail: str) -> None:
        self.guardrail, self.mode, self.engine, self.action, self.detail = name, mode, engine, action, detail

    def as_dict(self) -> dict[str, str]:
        return {
            "guardrail": self.guardrail,
            "mode": self.mode,
            "engine": self.engine,
            "action": self.action,
            "detail": self.detail,
        }


def _spec(name: str, mode: str, engine: str = "blocklist", config: dict | None = None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid.uuid4(), name=name, mode=mode, engine=engine, config=config or {}, enabled=True)


def _fake_guardrails(
    pre: list[Any] | None = None,
    post: list[Any] | None = None,
    *,
    pre_result: Any = None,  # ("masked_messages", events) | None = pass-through
    post_result: Any = None,  # (text, events, blocked)
) -> Any:
    from types import SimpleNamespace

    pre = pre or []
    post = post or []

    async def guardrails_for_model(model_name: str):
        return [*pre, *post]

    async def run_pre_call(guards, messages, http):
        if isinstance(pre_result, BaseException):
            raise pre_result
        if pre_result is None:
            return messages, []
        msgs, events = pre_result
        return msgs, events

    async def run_post_call(guards, text, http):
        if post_result is None:
            return text, [], False
        return post_result

    return SimpleNamespace(
        guardrails_for_model=guardrails_for_model,
        run_pre_call=run_pre_call,
        run_post_call=run_post_call,
        GuardrailBlocked=FakeBlocked,
    )


# --- upstream payloads ------------------------------------------------------

SSE_BODY = (
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"'
    + MODEL.encode()
    + b'","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"'
    + MODEL.encode()
    + b'","choices":[{"index":0,"delta":{"content":"hello "},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"'
    + MODEL.encode()
    + b'","choices":[{"index":0,"delta":{"content":"world"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"'
    + MODEL.encode()
    + b'","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: {"id":"c1","object":"chat.completion.chunk","model":"'
    + MODEL.encode()
    + b'","choices":[],"usage":{"prompt_tokens":11,"completion_tokens":5,"total_tokens":16}}\n\n'
    b"data: [DONE]\n\n"
)

JSON_UPSTREAM = {
    "id": "cmpl-1",
    "object": "chat.completion",
    "model": MODEL,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello world"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
}

TOOLCALL_UPSTREAM = {
    "id": "cmpl-2",
    "object": "chat.completion",
    "model": MODEL,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call1", "type": "function", "function": {"name": "srv1__tool1", "arguments": '{"x":1}'}}
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
}


def _key(scopes: list[str] | None = None) -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=scopes or ["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Fake auth + fake db + mock upstream + fake guardrails module.

    request.param (optional dict) overrides the fake guardrails module.
    Returns (TestClient, FakeDB, captured) — captured holds the last
    upstream JSON body and whether the upstream was reached.
    """
    monkeypatch.setenv("PCAI_LLM_DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    key = _key()
    gr_fake = getattr(request, "param", None) or _fake_guardrails()

    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)

    import pcai_llm.core.cascade as cascade_mod
    import pcai_llm.core.costs as costs_mod

    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    monkeypatch.setattr(cascade_mod, "db", fake_db)
    monkeypatch.setattr(costs_mod, "db", fake_db, raising=False)

    captured: dict[str, Any] = {"upstream_body": None, "reached": False}

    def handler(req: httpx2.Request) -> httpx2.Response:
        captured["reached"] = True
        captured["upstream_body"] = json.loads(req.content.decode())
        if captured["upstream_body"].get("stream"):
            return httpx2.Response(200, content=SSE_BODY)
        if captured["upstream_body"].get("expect_toolcalls"):
            return httpx2.Response(200, json=TOOLCALL_UPSTREAM)
        return httpx2.Response(200, json=JSON_UPSTREAM)

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake-upstream/v1")])
    monkeypatch.setattr(rp, "model_router", mock_router)
    monkeypatch.setattr(rp, "guardrails", gr_fake)

    app = FastAPI()
    app.include_router(rp.router)
    tc = TestClient(app, raise_server_exceptions=False)
    return tc, fake_db, captured


def _post(tc: TestClient, body: dict, headers: dict | None = None):
    return tc.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer hllm-x", **(headers or {})})


# ---------------------------------------------------------------------------
# pre_call wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            pre=[_spec("pii-guard", "pre_call")], pre_result=(None, [_ev("pii-guard", "pre_call", "blocklist", "pass")])
        )
    ],
    indirect=True,
)
def test_pre_call_pass_events_reach_ledger(env) -> None:
    tc, fake_db, _captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 200, r.text
    row = fake_db.settled[-1]
    assert row[8] == "ok"
    assert "pii-guard" in row[12]  # guardrail_events JSONB on the SAME row


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            pre=[_spec("pii-guard", "pre_call")],
            pre_result=FakeBlocked("pii-guard", "pre_call", "blocked keyword 'weapon'", engine="blocklist"),
        )
    ],
    indirect=True,
)
def test_pre_call_block_400_no_upstream_ledgers_blocked(env) -> None:
    tc, fake_db, captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "a weapon"}]})
    assert r.status_code == 400, r.text
    assert "pii-guard" in r.text and "weapon" in r.text
    assert not captured["reached"], "blocked request must never reach the upstream"
    row = fake_db.settled[-1]
    assert row[8] == "guardrail_blocked"
    assert "blocked" in row[12] and "pii-guard" in row[12]


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            pre=[_spec("pii-guard", "pre_call")],
            pre_result=FakeBlocked(
                "pii-guard",
                "pre_call",
                "blocked keyword 'weapon'",
                engine="blocklist",
                reply_text="That topic is blocked by policy. The turn will not be answered.",
            ),
        )
    ],
    indirect=True,
)
def test_pre_call_soft_block_json_reply_200_ledger_still_blocked(env) -> None:
    """Soft block (config carries reply_text): a normal 200 chat completion
    with the notice as the assistant message — and the LEDGER still records
    guardrail_blocked exactly like the hard path (audit unchanged)."""
    tc, fake_db, captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "a weapon"}]})
    assert r.status_code == 200, r.text
    assert r.headers.get("x-pcai-guardrail-blocked") == "pii-guard"
    body = r.json()
    assert body["model"] == MODEL
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "That topic is blocked by policy. The turn will not be answered.",
    }
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert not captured["reached"], "soft-blocked request must never reach the upstream either"
    row = fake_db.settled[-1]
    assert row[8] == "guardrail_blocked"
    assert "blocked" in row[12] and "pii-guard" in row[12]


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            pre=[_spec("pii-guard", "pre_call")],
            pre_result=FakeBlocked(
                "pii-guard",
                "pre_call",
                "blocked keyword 'weapon'",
                engine="blocklist",
                reply_text="That topic is blocked by policy.",
            ),
        )
    ],
    indirect=True,
)
def test_pre_call_soft_block_stream_reply_sse(env) -> None:
    """Soft block on a stream:true request: a single-shot SSE answer with the
    notice as the content delta — no 400 anywhere, [DONE] terminated."""
    tc, fake_db, captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "a weapon"}], "stream": True})
    assert r.status_code == 200, r.text
    assert "event-stream" in r.headers["content-type"]
    assert r.headers.get("x-pcai-guardrail-blocked") == "pii-guard"
    assert "That topic is blocked by policy." in r.text
    assert b"guardrail_blocked" not in r.content
    assert b"[DONE]" in r.content
    assert not captured["reached"]
    row = fake_db.settled[-1]
    assert row[8] == "guardrail_blocked"  # ledger unchanged on the stream path too


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            pre=[_spec("pii-guard", "pre_call")],
            pre_result=(
                [{"role": "user", "content": "hi [REDACTED]"}],
                [_ev("pii-guard", "pre_call", "regex_pii", "masked", "EMAILx1")],
            ),
        )
    ],
    indirect=True,
)
def test_pre_call_mask_rewrites_messages_upstream_sees_masked(env) -> None:
    tc, _fake_db, captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hi a@b.com"}]})
    assert r.status_code == 200, r.text
    assert captured["upstream_body"]["messages"][0]["content"] == "hi [REDACTED]"


# ---------------------------------------------------------------------------
# post_call wiring — non-streaming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            post=[_spec("leak-guard", "post_call")],
            post_result=(
                "hello world",
                [_ev("leak-guard", "post_call", "blocklist", "blocked", "blocked keyword 'x'")],
                True,
            ),
        )
    ],
    indirect=True,
)
def test_post_call_block_nonstream_400_real_spend_ledgered(env) -> None:
    tc, fake_db, captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "guardrail_blocked"
    assert captured["reached"], "post_call runs AFTER the upstream call"
    row = fake_db.settled[-1]
    assert row[8] == "guardrail_blocked"
    assert row[3] == 11 and row[4] == 5  # REAL observed usage on the blocked row
    assert "leak-guard" in row[12]


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            post=[_spec("pii-guard", "post_call")],
            post_result=("[REDACTED]", [_ev("pii-guard", "post_call", "regex_pii", "masked", "EMAILx1")], False),
        )
    ],
    indirect=True,
)
def test_post_call_mask_nonstream_rewrites_body_and_header(env) -> None:
    tc, fake_db, _captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "email me"}]})
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "[REDACTED]"
    assert r.headers.get("x-pcai-guardrail-modified") == "pii-guard"
    row = fake_db.settled[-1]
    assert row[8] == "ok" and "masked" in row[12]


# ---------------------------------------------------------------------------
# post_call wiring — streaming (buffered guard wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env", [_fake_guardrails(post=[_spec("len-guard", "post_call")])], indirect=True)
def test_post_call_pass_stream_replays_verbatim(env) -> None:
    tc, fake_db, _captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "stream": True})
    assert r.status_code == 200, r.text
    assert "event-stream" in r.headers["content-type"]
    assert b"hello " in r.content and b"world" in r.content and b"[DONE]" in r.content
    assert b"guardrail_blocked" not in r.content
    row = fake_db.settled[-1]
    assert row[8] == "ok"
    assert row[3] == 11 and row[4] == 5  # usage captured from the stream


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            post=[_spec("leak-guard", "post_call")],
            post_result=("", [], True),
        )
    ],
    indirect=True,
)
def test_post_call_block_stream_discards_content(env) -> None:
    tc, fake_db, _captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "stream": True})
    assert r.status_code == 200  # SSE channel itself is a 200; error rides inside
    assert b"guardrail_blocked" in r.content
    assert b"hello " not in r.content and b"world" not in r.content, "blocked stream must not leak content"
    assert b"[DONE]" in r.content
    row = fake_db.settled[-1]
    assert row[8] == "guardrail_blocked"


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            post=[_spec("pii-guard", "post_call")],
            post_result=("hello [REDACTED]", [_ev("pii-guard", "post_call", "regex_pii", "masked", "EMAILx1")], False),
        )
    ],
    indirect=True,
)
def test_post_call_mask_stream_resynthesizes_final_chunk(env) -> None:
    tc, fake_db, _captured = env
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "stream": True})
    assert r.status_code == 200
    assert b"[REDACTED]" in r.content
    assert b"world" not in r.content, "unmasked deltas must be suppressed"
    assert b'"finish_reason":"stop"' in r.content.replace(b" ", b"") or b"finish_reason" in r.content
    assert b"[DONE]" in r.content
    row = fake_db.settled[-1]
    assert row[8] == "ok"


# ---------------------------------------------------------------------------
# MCP request-body opt-in
# ---------------------------------------------------------------------------


def _patch_mcp(monkeypatch: pytest.MonkeyPatch, grants: set, servers: list[dict], tools: list[dict]) -> None:
    import pcai_llm.api.routes_mcp as rm
    import pcai_llm.mcpf.federation as fed

    async def fake_load_grants(key_id):
        return grants

    async def fake_mcp_servers():
        return servers

    async def fake_fetch_tools(url, headers, timeout_s=None):
        return tools

    def fake_build_headers(headers, secret, enc):
        return {}

    monkeypatch.setattr(fed, "load_grants", fake_load_grants)
    monkeypatch.setattr(rp.registry, "mcp_servers", fake_mcp_servers)
    monkeypatch.setattr(rm, "_fetch_tools", fake_fetch_tools)
    monkeypatch.setattr(rm, "build_mcp_headers", fake_build_headers)
    monkeypatch.setattr(rp.settings, "tool_relay_grant_mode", "explicit")


@pytest.mark.parametrize("env", [_fake_guardrails()], indirect=True)
def test_mcp_body_optin_triggers_relay_and_strips_mcp_entries(env, monkeypatch) -> None:
    tc, _fake_db, captured = env
    _patch_mcp(
        monkeypatch,
        {("srv1", "tool1")},
        [{"name": "srv1", "url": "http://mcp/x", "headers": {}, "tool_policy": "all"}],
        [
            {
                "tool": "tool1",
                "description": "d",
                "inputSchema": {"type": "object", "properties": {"x": {"type": "integer"}}},
            }
        ],
    )
    r = _post(
        tc,
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"type": "mcp", "server": "srv1"}],
            "stream": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("x-pcai-relay") == "streaming", "body opt-in must engage the relay"
    up = captured["upstream_body"]
    assert up["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "srv1__tool1",
                "description": "d",
                # Audit fix: tools_payload now carries the upstream inputSchema
                # (no silent empty-args execution on malformed model output).
                "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
            },
        }
    ], "relay injects granted catalog as function tools; mcp entries stripped"
    assert not any(isinstance(t, dict) and t.get("type") == "mcp" for t in up["tools"])


@pytest.mark.parametrize("env", [_fake_guardrails()], indirect=True)
def test_mcp_body_optin_without_grants_400(env, monkeypatch) -> None:
    tc, _fake_db, _captured = env
    _patch_mcp(monkeypatch, set(), [{"name": "srv1", "url": "http://mcp/x", "headers": {}, "tool_policy": "all"}], [])
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user"}], "tools": [{"type": "mcp", "server": "srv1"}]})
    assert r.status_code == 400
    assert "grants" in r.text


@pytest.mark.parametrize("env", [_fake_guardrails()], indirect=True)
def test_mcp_unknown_server_400(env, monkeypatch) -> None:
    tc, _fake_db, _captured = env
    _patch_mcp(
        monkeypatch,
        {("srv1", "tool1")},
        [{"name": "srv1", "url": "http://mcp/x", "headers": {}, "tool_policy": "all"}],
        [],
    )
    r = _post(tc, {"model": MODEL, "messages": [{"role": "user"}], "tools": [{"type": "mcp", "server": "ghost"}]})
    assert r.status_code == 400
    assert "ghost" in r.text


@pytest.mark.parametrize("env", [_fake_guardrails()], indirect=True)
def test_mcp_header_zero_conflict_400(env, monkeypatch) -> None:
    tc, _fake_db, _captured = env
    _patch_mcp(
        monkeypatch,
        {("srv1", "tool1")},
        [{"name": "srv1", "url": "http://mcp/x", "headers": {}, "tool_policy": "all"}],
        [],
    )
    r = _post(
        tc,
        {"model": MODEL, "messages": [{"role": "user"}], "tools": [{"type": "mcp", "server": "srv1"}]},
        headers={"X-PCAI-Execute-Tools": "0"},
    )
    assert r.status_code == 400
    assert "require_approval" in r.text


@pytest.mark.parametrize("env", [_fake_guardrails()], indirect=True)
def test_mcp_approval_passthrough_forwards_toolcalls_untouched(env, monkeypatch) -> None:
    tc, fake_db, captured = env
    _patch_mcp(
        monkeypatch,
        {("srv1", "tool1")},
        [{"name": "srv1", "url": "http://mcp/x", "headers": {}, "tool_policy": "all"}],
        [
            {
                "tool": "tool1",
                "description": "d",
                "inputSchema": {"type": "object", "properties": {"x": {"type": "integer"}}},
            }
        ],
    )
    r = _post(
        tc,
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "go"}],
            "tools": [{"type": "mcp", "server": "srv1", "require_approval": "always"}],
            "expect_toolcalls": True,
        },
    )
    assert r.status_code == 200, r.text
    assert "x-pcai-relay" not in {k.lower() for k in r.headers}, "passthrough must NOT engage the relay"
    up = captured["upstream_body"]
    assert up["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "srv1__tool1",
                "description": "d",
                "parameters": {"type": "object", "properties": {"x": {"type": "integer"}}},
            },
        }
    ], "granted catalog exposed as function tools WITH the upstream inputSchema"
    assert r.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "srv1__tool1"
    row = fake_db.settled[-1]
    assert row[8] == "ok"


@pytest.mark.parametrize("env", [_fake_guardrails()], indirect=True)
def test_mcp_invalid_approval_value_400(env, monkeypatch) -> None:
    tc, _fake_db, _captured = env
    _patch_mcp(monkeypatch, set(), [], [])
    r = _post(
        tc,
        {
            "model": MODEL,
            "messages": [{"role": "user"}],
            "tools": [{"type": "mcp", "server": "srv1", "require_approval": "sometimes"}],
        },
    )
    assert r.status_code == 400
    assert "require_approval" in r.text


# ---------------------------------------------------------------------------
# _split_request_tools (pure)
# ---------------------------------------------------------------------------


def test_split_request_tools_mixed() -> None:
    mcp, fn = rp._split_request_tools(
        [
            {"type": "function", "function": {"name": "f"}},
            {"type": "mcp", "server": " s1 "},
            {"type": "mcp", "require_approval": "always"},
        ]
    )
    assert fn == [{"type": "function", "function": {"name": "f"}}]
    assert mcp == [{"server": "s1", "require_approval": "never"}, {"server": None, "require_approval": "always"}]


def test_split_request_tools_invalid() -> None:
    with pytest.raises(ValueError):
        rp._split_request_tools([{"type": "mcp", "server": ""}])
    with pytest.raises(ValueError):
        rp._split_request_tools([{"type": "mcp", "require_approval": "sometimes"}])
    with pytest.raises(ValueError):
        rp._split_request_tools("nope")
    assert rp._split_request_tools(None) == ([], [])


# ---------------------------------------------------------------------------
# audit wave 2026-10-29 (zone 3b): ZC-3 masked-replay leak, ZC-5 reasoning
# strip spelling
# ---------------------------------------------------------------------------


def _zc3_chunks() -> list[bytes]:
    """One upstream chunk per leak channel: content, native reasoning (both
    spellings), tool-call argument fragments, and the two pcai events that
    carry text (thinking + tool result preview)."""
    line = lambda o: b"data: " + json.dumps(o).encode() + b"\n\n"
    return [
        line({"choices": [{"delta": {"content": "the answer is ACME-Secret"}}]}),
        line({"choices": [{"delta": {"reasoning_content": "reasoning leaks ACME-Secret"}}]}),
        line({"choices": [{"delta": {"reasoning": "second spelling leaks ACME-Secret"}}]}),
        line(
            {
                "choices": [
                    {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"cmd": "ACME-Secret"}'}}]}}
                ]
            }
        ),
        line({"pcai": {"event": "thinking", "iteration": 0, "text": "thinking leaks ACME-Secret too"}}),
        line(
            {
                "pcai": {
                    "event": "tool_call",
                    "id": "c1",
                    "server": "s",
                    "tool": "t",
                    "ok": True,
                    "ms": 1,
                    "args": "{}",
                    "result": "tool output ACME-Secret",
                }
            }
        ),
        # The ACL-denied tool_error event in the shape toolrelay ACTUALLY
        # emits (audit r5 #2a): the PARSED arguments as a dict, no result
        # field, no other string channel. The old chunk pinned
        # "args": "{}" — a shape no emitter produces — so the str-only
        # classifier looked correct while this one was replayed verbatim.
        line(
            {
                "pcai": {
                    "event": "tool_error",
                    "id": "c2",
                    "server": "s",
                    "tool": "t",
                    "detail": "denied by ACL",
                    "args": {"cmd": "cat ACME-Secret"},
                }
            }
        ),
        b"data: [DONE]\n\n",
    ]


async def test_zc3_masked_replay_drops_every_text_channel_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit ZC-3 + xv round 2 (findings 1/2/5): the masked path DROPS every
    text-carrying chunk whole (content, all three reasoning spellings,
    tool-call argument fragments, pcai thinking/tool events) and ships ONE
    consolidated final carrying the MASKED ANSWER ONLY. No per-fragment
    re-masking (a split secret's fragments pass unchanged under any
    per-chunk rule), no engine amplification (one run_post_call per
    request), and no reasoning/tool JSON glued into the answer."""
    import time as _time

    calls: list[str] = []

    class _MaskingGuardrails:
        async def run_post_call(self, guards, text, http):
            calls.append(text)
            return text.replace("ACME-Secret", "[REDACTED]"), [], False

    monkeypatch.setattr(rp, "guardrails", _MaskingGuardrails())
    monkeypatch.setattr(rp, "model_router", SimpleNamespace(client=None))

    async def inner():
        for c in _zc3_chunks():
            yield c

    out: list[bytes] = []
    async for chunk in rp._guarded_sse(
        inner(),
        None,
        [_spec("leak-guard", "post_call")],
        key=_key(),
        model=MODEL,
        request_id="req-zc3",
        started=_time.monotonic(),
        relay_mode=True,
    ):
        out.append(chunk)
    body = b"".join(out)
    assert b"[REDACTED]" in body, "the masked replacement must reach the client"
    assert b"ACME-Secret" not in body, f"the unmasked text leaked: {body!r}"
    # the consolidated final is the ANSWER ONLY — no reasoning, no tool JSON,
    # no pcai text glued into delta.content (finding 2)
    final = [c for c in out if b"chatcmpl-guarded" in c]
    assert len(final) == 1
    final_obj = json.loads(final[0][len(b"data: ") :].split(b"\n")[0])
    assert final_obj["choices"][0]["delta"]["content"] == "the answer is [REDACTED]"
    # non-text frames still replay: finish_reason=stop survives the masked path
    assert b'"finish_reason":"stop"' in body.replace(b" ", b"") or b"finish_reason" in body
    # exactly one [DONE] (the synthesized terminal) and no engine amplification
    assert body.count(b"data: [DONE]") == 1
    assert len(calls) == 1, f"one assembled verdict per request, got {len(calls)}"


async def test_zc3_split_secret_is_not_recoverable_from_a_masked_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """xv round 2, finding 1 (CRITICAL): a secret SPLIT across two content
    deltas defeats any per-fragment guard rule — the only safe masked shape
    drops the text-carrying chunks whole. Client-side assembly (what an
    OpenAI SDK does) must not recover the secret."""
    import time as _time

    class _MaskingGuardrails:
        async def run_post_call(self, guards, text, http):
            return text.replace("ACME-SECRET-KEY-12345", "[REDACTED]"), [], False

    monkeypatch.setattr(rp, "guardrails", _MaskingGuardrails())
    monkeypatch.setattr(rp, "model_router", SimpleNamespace(client=None))

    async def inner():
        yield b'data: {"choices":[{"delta":{"content":"your key is ACME-SECRET-"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"KEY-12345 ok"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    out: list[bytes] = []
    async for chunk in rp._guarded_sse(
        inner(),
        None,
        [_spec("leak-guard", "post_call")],
        key=_key(),
        model=MODEL,
        request_id="req-zc3s",
        started=_time.monotonic(),
        relay_mode=True,
    ):
        out.append(chunk)
    assembled = b"".join(out)
    # simulate the client's content assembly
    client_text = ""
    for line in assembled.split(b"\n"):
        if not line.startswith(b"data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        delta = (obj.get("choices") or [{}])[0].get("delta", {}) if isinstance(obj, dict) else {}
        content = delta.get("content")
        if isinstance(content, str):
            client_text += content
    assert "ACME-SECRET-KEY-12345" not in client_text, f"split secret recoverable by the client: {client_text!r}"
    assert "[REDACTED] ok" in client_text, "the masked answer is delivered"


async def test_zc3_tool_data_does_not_block_a_clean_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """xv round 2, finding 4: tool-call ARGUMENTS and tool RESULTS are data
    the caller supplied or the tool produced — a blocklist keyword in a grep
    pattern or a tool's stdout must not discard an otherwise clean answer.
    The assembled verdict sees content + native reasoning only."""
    import time as _time

    calls: list[str] = []

    class _BlocklistGuardrails:
        async def run_post_call(self, guards, text, http):
            calls.append(text)
            if "password" in text:
                return text, [], True
            return text, [], False

    monkeypatch.setattr(rp, "guardrails", _BlocklistGuardrails())
    monkeypatch.setattr(rp, "model_router", SimpleNamespace(client=None))

    async def inner():
        # Valid JSON with the emitter's DICT args (audit r5 #2a): the old
        # single-quote-inside-double-quote line was unparseable, so this test
        # proved nothing about the classifier it names.
        yield b'data: {"pcai": {"event": "tool_call", "id": "c1", "server": "s", "tool": "t", "ok": true, "ms": 1, "args": {"pattern": "password"}, "result": "the password file"}}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"done, nothing found"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    out: list[bytes] = []
    async for chunk in rp._guarded_sse(
        inner(),
        None,
        [_spec("blk-guard", "post_call")],
        key=_key(),
        model=MODEL,
        request_id="req-zc3t",
        started=_time.monotonic(),
        relay_mode=True,
    ):
        out.append(chunk)
    body = b"".join(out)
    assert not any(b"guardrail_blocked" in c for c in out), "tool data must not block the answer"
    assert b"done, nothing found" in body
    assert "password" not in " ".join(calls), "the guard must never even SEE tool data"


async def test_zc3_whole_answer_block_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assembled verdict (content + reasoning) still BLOCKS the stream —
    only tool-side data is out of scope, not the block capability."""
    import time as _time

    class _BlocklistGuardrails:
        async def run_post_call(self, guards, text, http):
            if "ACME-Secret" in text:
                return text, [], True
            return text, [], False

    async def fake_settle(*a, **k):
        return None

    monkeypatch.setattr(rp, "guardrails", _BlocklistGuardrails())
    monkeypatch.setattr(rp, "model_router", SimpleNamespace(client=None))
    monkeypatch.setattr(rp, "settle", fake_settle)

    async def inner():
        yield b'data: {"choices":[{"delta":{"content":"the answer is ACME-Secret"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    out = [
        chunk
        async for chunk in rp._guarded_sse(
            inner(),
            None,
            [_spec("blk-guard", "post_call")],
            key=_key(),
            model=MODEL,
            request_id="req-zc3b",
            started=_time.monotonic(),
            relay_mode=True,
        )
    ]
    assert any(b"guardrail_blocked" in c for c in out), "a blocked answer must surface the envelope"
    assert not any(b"ACME-Secret" in c for c in out)


@pytest.mark.parametrize(
    "env",
    [_fake_guardrails()],
    indirect=True,
)
def test_zc5_inbound_reasoning_cap_covers_the_reasoning_text_spelling(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Audit ZC-5: the inbound cap/strip covered reasoning_content and
    reasoning but NOT reasoning_text (the third pi-ai spelling the relay
    itself recognises) — a client replaying reasoning under that key bypassed
    the cap and the 2026-09-18 amplification hole reopened through it."""
    tc, _fake_db, captured = env
    big = "cycle " + "x" * 4000
    r = _post(
        tc,
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "short reasoning",
                    "reasoning": "short reasoning 2",
                    "reasoning_text": big,
                },
                {"role": "user", "content": "next"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    up = captured["upstream_body"]["messages"][0]
    assert len(up["reasoning_text"]) <= 2000 + len("…[earlier reasoning trimmed]…"), (
        f"reasoning_text bypassed the cap: {len(up['reasoning_text'])} chars upstream"
    )


@pytest.mark.parametrize(
    "env",
    [_fake_guardrails()],
    indirect=True,
)
def test_zc5_strip_removes_all_three_reasoning_spellings(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the keep-reasoning knob OFF the strip is the rollback: it must
    remove reasoning under ALL THREE spellings, not just the two it knew."""
    tc, _fake_db, captured = env
    monkeypatch.setattr(rp.settings, "reasoning_history_enabled", False)
    r = _post(
        tc,
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "assistant",
                    "content": "answer",
                    "reasoning_content": "a",
                    "reasoning": "b",
                    "reasoning_text": "c",
                },
                {"role": "user", "content": "next"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    up = captured["upstream_body"]["messages"][0]
    assert "reasoning_content" not in up
    assert "reasoning" not in up
    assert "reasoning_text" not in up, "the third spelling survived the strip (ZC-5)"


@pytest.mark.parametrize(
    "env",
    [
        _fake_guardrails(
            post=[_spec("pii-guard", "post_call")],
            post_result=("clean [REDACTED]", [_ev("pii-guard", "post_call", "regex_pii", "masked", "SECRETx1")], False),
        )
    ],
    indirect=True,
)
def test_zc3_nonstreaming_mask_covers_every_model_text_channel(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """xv round 2, finding 3: the non-streaming path masked ONLY
    message.content — reasoning_content and tool_calls relayed the secret
    the answer just lost. The masked turn now has the stream's exact shape:
    masked content, reasoning spellings and tool_calls DROPPED, finish
    downgraded from tool_calls to stop."""
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.cascade as cascade_mod
    import pcai_llm.core.costs as costs_mod
    from pcai_llm.routing.router import Endpoint as _Ep
    from pcai_llm.routing.router import Router as _Router

    key = _key()

    async def fake_auth(p: str) -> KeyRecord:
        return key

    secret_json = {
        "id": "cmpl-3",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "the answer is ACME-SECRET-KEY-12345",
                    "reasoning_content": "I looked up ACME-SECRET-KEY-12345",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "t", "arguments": '{"k": "ACME-SECRET-KEY-12345"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=secret_json)

    fake_db = FakeDB()
    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    monkeypatch.setattr(cascade_mod, "db", fake_db)
    monkeypatch.setattr(costs_mod, "db", fake_db)
    mock_router = _Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [_Ep(id="ep1", model_name=MODEL, base_url="http://fake/v1")])
    monkeypatch.setattr(rp, "model_router", mock_router)

    app = FastAPI()
    app.include_router(rp.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "go"}]},
        headers={"Authorization": "Bearer hllm-x"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    msg = body["choices"][0]["message"]
    assert "ACME-SECRET-KEY-12345" not in json.dumps(body), "the secret survived the non-streaming mask"
    assert msg["content"] == "clean [REDACTED]"
    assert "reasoning_content" not in msg and "tool_calls" not in msg, (
        "non-content channels must be dropped, not relayed"
    )
    assert body["choices"][0]["finish_reason"] == "stop"
    assert r.headers.get("x-pcai-guardrail-modified") == "pii-guard"


# ---------------------------------------------------------------------------
# audit r5 #2b / #2c / #11 — the remaining masked-replay channels, the
# per-model billing fraction, and the disconnect-during-guardrails slot leak
# ---------------------------------------------------------------------------


async def test_r5_2b_masked_stream_drops_a_multi_choice_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A chunk that packs SEVERAL choices carries model text under a
    non-zero index.

    PRE-FIX (_sse_text_of_payload / _sse_answer_texts_of_payload read
    obj["choices"][0] only): a chunk whose first choice carries no text and
    whose second does classified as text-free — it was replayed VERBATIM on
    the masked path AND the guard never judged that sample.
    """
    import time as _time

    calls: list[str] = []

    class _MaskingGuardrails:
        async def run_post_call(self, guards, text, http):
            calls.append(text)
            return text.replace("ACME-SECRET-KEY-12345", "[REDACTED]"), [], False

    monkeypatch.setattr(rp, "guardrails", _MaskingGuardrails())
    monkeypatch.setattr(rp, "model_router", SimpleNamespace(client=None))

    async def inner():
        yield b'data: {"choices":[{"index":0,"delta":{"content":"the answer is ACME-SECRET-KEY-12345"}}]}\n\n'
        # ONE chunk, TWO choices: choice 0 carries no text at all.
        yield (
            b'data: {"choices":[{"index":0,"delta":{}},'
            b'{"index":1,"delta":{"content":" sample two ACME-SECRET-KEY-12345"}}]}\n\n'
        )
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    out: list[bytes] = []
    async for chunk in rp._guarded_sse(
        inner(),
        None,
        [_spec("leak-guard", "post_call")],
        key=_key(),
        model=MODEL,
        request_id="req-r5-2b",
        started=_time.monotonic(),
        relay_mode=True,
    ):
        out.append(chunk)
    body = b"".join(out)
    assert b"ACME-SECRET-KEY-12345" not in body, f"the non-zero-index choice leaked: {body!r}"
    assert b"[REDACTED]" in body
    assert "ACME-SECRET-KEY-12345" in calls[0], "the guard must judge EVERY choice, not just choices[0]"


async def test_r5_2c_masked_stream_drops_an_event_split_across_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx2 hands over whatever the socket returned, so an SSE event can
    straddle two network reads.

    PRE-FIX (one json.loads per read, no line cursor): both halves of a
    straddled event classified as text-free — the guard never saw the text
    (so no mask fired) and the masked replay shipped both halves verbatim,
    which the client reassembles into the unmasked answer.
    """
    import time as _time

    calls: list[str] = []

    class _MaskingGuardrails:
        async def run_post_call(self, guards, text, http):
            calls.append(text)
            return text.replace("ACME-SECRET-KEY-12345", "[REDACTED]"), [], False

    monkeypatch.setattr(rp, "guardrails", _MaskingGuardrails())
    monkeypatch.setattr(rp, "model_router", SimpleNamespace(client=None))

    async def inner():
        yield b'data: {"choices":[{"delta":{"content":"the answer is ACME-SECRET-KEY-12345"}}]}\n\n'
        # ONE event, TWO reads: the boundary falls mid-JSON.
        yield b'data: {"choices":[{"delta":{"content":"and again ACME-SECRET-KEY-'
        yield b'12345"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    out: list[bytes] = []
    async for chunk in rp._guarded_sse(
        inner(),
        None,
        [_spec("leak-guard", "post_call")],
        key=_key(),
        model=MODEL,
        request_id="req-r5-2c",
        started=_time.monotonic(),
        relay_mode=True,
    ):
        out.append(chunk)
    body = b"".join(out)
    assert b"ACME-SECRET-KEY-12345" not in body, f"a straddled event leaked: {body!r}"
    assert b"ACME-SECRET-KEY-" not in body, "half of the straddled event was replayed"
    assert "ACME-SECRET-KEY-12345" in calls[0], (
        "the line cursor must reassemble the straddled event for the guard (pre-fix the guard "
        "never saw it, so no mask could fire)"
    )
    assert body.count(b"data: [DONE]") == 1


@pytest.mark.parametrize(
    "env",
    [_fake_guardrails()],
    indirect=True,
)
def test_r5_2b_nonstreaming_mask_rewrites_every_choice(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A {"n": 2} request has TWO sampled answers; the masked path rewrote
    only choices[0].

    PRE-FIX (the JSON rewrite touched embedding_json["choices"][0] only): the
    response carried the masked answer in choice 0 next to the UNMASKED
    sample in choice 1 — well-formed JSON, no wire-splitting needed.
    """
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.cascade as cascade_mod
    import pcai_llm.core.costs as costs_mod
    from pcai_llm.routing.router import Endpoint as _Ep
    from pcai_llm.routing.router import Router as _Router

    key = _key()

    async def fake_auth(p: str) -> KeyRecord:
        return key

    class _MaskingGuardrails:
        async def guardrails_for_model(self, model_name: str):
            return [_spec("pii-guard", "post_call")]

        async def run_pre_call(self, guards, messages, http):
            return messages, []

        async def run_post_call(self, guards, text, http):
            return text.replace("ACME-SECRET-KEY-12345", "[REDACTED]"), [], False

    n2 = {
        "id": "cmpl-n2",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "sample one: ACME-SECRET-KEY-12345"},
                "finish_reason": "stop",
            },
            {
                "index": 1,
                "message": {"role": "assistant", "content": "sample two: ACME-SECRET-KEY-12345"},
                "finish_reason": "stop",
            },
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
    }

    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=n2)

    fake_db = FakeDB()
    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    monkeypatch.setattr(cascade_mod, "db", fake_db)
    monkeypatch.setattr(costs_mod, "db", fake_db)
    monkeypatch.setattr(rp, "guardrails", _MaskingGuardrails())
    mock_router = _Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [_Ep(id="ep1", model_name=MODEL, base_url="http://fake/v1")])
    monkeypatch.setattr(rp, "model_router", mock_router)

    app = FastAPI()
    app.include_router(rp.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "n": 2, "messages": [{"role": "user", "content": "go"}]},
        headers={"Authorization": "Bearer hllm-x"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["choices"]) == 2, "the masked turn must keep the requested choice count"
    assert "ACME-SECRET-KEY-12345" not in json.dumps(body), "an unmasked sample survived in a non-zero choice"
    # The guard judges ONE assembled answer — the join of every sampled text —
    # so the masked turn delivers that one masked answer in EVERY choice. The
    # alternative (one engine call per choice) multiplies webhook latency by
    # n, the amplification the masked-path design explicitly rejects.
    for choice in body["choices"]:
        assert "[REDACTED]" in choice["message"]["content"]
        assert choice["message"]["content"] == body["choices"][0]["message"]["content"]


async def test_r5_11_cancel_during_post_call_still_settles_and_releases_the_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that walks away DURING a post_call guardrail must not leak the
    concurrency slot or the ledger row.

    PRE-FIX: the buffering loop was guarded by `except BaseException`, but the
    `await guardrails.run_post_call(...)` that follows sat under only
    `except Exception` — and CancelledError is a BaseException. A client
    disconnecting during a 10 s webhook guardrail skipped BOTH
    ledger.finish() (lost ledger row -> under-billing) and release_slot()
    (the _in_flight counter never decrements -> the key is 429-bricked on its
    concurrency cap until the pod restarts).
    """
    import asyncio as _asyncio

    finished: list[bool] = []
    released: list[bool] = []
    entered = _asyncio.Event()

    class _Ledger:
        def __init__(self) -> None:
            self.pre_events: list[Any] = []
            self.extra_events: list[Any] = []
            self.override_status: str | None = None

        async def finish(self) -> None:
            finished.append(True)

    class _SlowGuardrails:
        async def run_post_call(self, guards, text, http):
            entered.set()
            await _asyncio.sleep(10)  # the 10 s webhook guardrail, unbounded by the client
            return text, [], False

    monkeypatch.setattr(rp, "guardrails", _SlowGuardrails())
    monkeypatch.setattr(rp, "model_router", SimpleNamespace(client=None))

    async def inner():
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def consume() -> None:
        async for _chunk in rp._guarded_sse(
            inner(),
            _Ledger(),  # type: ignore[arg-type]
            [_spec("hook-guard", "post_call", engine="webhook")],
            key=_key(),
            model=MODEL,
            request_id="req-r5-11",
            started=0.0,
            release_slot=lambda: released.append(True),
        ):
            pass

    task = _asyncio.create_task(consume())
    await entered.wait()
    task.cancel()
    with pytest.raises(_asyncio.CancelledError):
        await task
    assert finished == [True], "the ledger row was lost: finish() never ran for the cancelled request"
    assert released == [True], "the concurrency slot leaked: release_slot() never ran"
