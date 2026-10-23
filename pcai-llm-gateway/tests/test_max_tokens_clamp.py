"""Per-model max_tokens ceiling — the "never forward a cap the model cannot
satisfy" guard.

LIVE DEFECT this pins: the operator's `auto` cascade entry sends
`max_tokens: 262144`. The gateway forwarded the body VERBATIM; Qwen3.8-27B
runs with context 262144 and rejects any request where
`max_tokens + prompt > context` with an HTTP 400 at the API layer (SGLang
logged only 400s — no sampling entry). Measured: 260 errors vs 42 OK in 6h
(~86% failure) while GLM (1M ctx) and Deepseek were unaffected. Compounding
it, the load-aware cascade ranks the idle-but-failing Qwen FIRST, so every
NEW conversation headed into the 400 wall.

Contract under test (`config.clamp_max_tokens`, ONE helper shared by every
upstream-forwarding site so they cannot drift):
  * an entry caps `max_tokens` for that virtual model on EVERY upstream call;
  * absent model = no cap; 0/negative entry = ignored (fail-safe);
  * the clamp is SILENT and one-way: a larger client value is reduced, a
    smaller one is never raised;
  * the caller's body is NEVER mutated (the ledger settle + admission
    estimate read the client's original numbers);
  * the zero-copy fast path returns the SAME object when nothing changes
    (this helper is on the hot path of every proxied request).

Integration coverage drives real /v1/chat/completions requests through the
existing MockTransport + FakeDB harness (test_proxy.py / test_ttft_gate.py /
test_relay_settle.py shapes) and asserts the UPSTREAM received the clamped
value while the client's request body was untouched — for the non-streaming,
streaming (SSE) and tool-relay paths.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import clamp_max_tokens, settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_fallbacks
from pcai_llm.routing.router import Endpoint, Router

MODEL = "capped-model"
CLIENT_CAP = 262144  # the operator's `auto` entry value
CEILING = 32768


# ---------------------------------------------------------------------------
# pure helper — contract + hot-path invariants
# ---------------------------------------------------------------------------


def test_absent_model_returns_same_object() -> None:
    """A model with no entry is untouched — and zero-copy (hot path)."""
    body = {"model": "other", "max_tokens": 262144}
    out = clamp_max_tokens(body, "uncapped-model", {MODEL: CEILING})
    assert out is body, "no-entry path must not copy the body"


def test_empty_ceilings_map_is_inert() -> None:
    """The feature is opt-in: an empty map changes nothing, at any value."""
    body = {"model": MODEL, "max_tokens": 262144}
    assert clamp_max_tokens(body, MODEL, {}) is body


def test_absent_max_tokens_is_untouched() -> None:
    """Nothing to clamp — the key is absent, so the body is returned as-is."""
    body = {"model": MODEL, "messages": []}
    out = clamp_max_tokens(body, MODEL, {MODEL: CEILING})
    assert out is body
    assert "max_tokens" not in out


@pytest.mark.parametrize("value", [1, 100, CEILING])
def test_value_at_or_below_ceiling_is_untouched(value: int) -> None:
    """A cap never RAISES a small client value; equal is a no-op."""
    body = {"model": MODEL, "max_tokens": value}
    out = clamp_max_tokens(body, MODEL, {MODEL: CEILING})
    assert out is body, f"value {value} <= ceiling {CEILING} must be a no-op"
    assert out["max_tokens"] == value


def test_value_above_ceiling_is_lowered() -> None:
    """THE fix: the oversized client cap is silently reduced to the ceiling."""
    body = {"model": MODEL, "max_tokens": CLIENT_CAP}
    out = clamp_max_tokens(body, MODEL, {MODEL: CEILING})
    assert out is not body, "a clamp must produce a new dict"
    assert out["max_tokens"] == CEILING
    assert body["max_tokens"] == CLIENT_CAP, "the CALLER's body must never be mutated"


def test_clamp_does_not_mutate_input_and_keeps_other_keys() -> None:
    """Everything except the one key is byte-identical in the new dict."""
    body = {"model": MODEL, "max_tokens": CLIENT_CAP, "temperature": 0.2, "stream": True, "messages": [{"a": 1}]}
    snapshot = json.dumps(body, sort_keys=True)
    out = clamp_max_tokens(body, MODEL, {MODEL: CEILING})
    assert json.dumps(body, sort_keys=True) == snapshot, "input body was mutated"
    assert out["max_tokens"] == CEILING
    assert {k: v for k, v in out.items() if k != "max_tokens"} == {k: v for k, v in body.items() if k != "max_tokens"}


@pytest.mark.parametrize("ceiling", [0, -1, -32768])
def test_zero_or_negative_ceiling_is_ignored(ceiling: int) -> None:
    """FAIL-SAFE: a malformed entry must not mute the model.

    Clamping to 0 would allow no output at all — worse than the 400 it was
    meant to prevent. A non-positive entry means "no cap".
    """
    body = {"model": MODEL, "max_tokens": CLIENT_CAP}
    out = clamp_max_tokens(body, MODEL, {MODEL: ceiling})
    assert out is body
    assert out["max_tokens"] == CLIENT_CAP


@pytest.mark.parametrize("value", ["262144", 262144.5, None, [262144], {"n": 1}])
def test_non_int_max_tokens_untouched(value: Any) -> None:
    """Non-numeric values are left alone — the estimator already guards them.

    A string cap is a client bug that _estimate_tokens absorbs; silently
    rewriting it here would mask it and could change billing semantics.
    """
    body = {"model": MODEL, "max_tokens": value}
    out = clamp_max_tokens(body, MODEL, {MODEL: CEILING})
    assert out is body, f"non-int {value!r} must be untouched"
    assert out["max_tokens"] == value


@pytest.mark.parametrize("value", [0, -5])
def test_non_positive_max_tokens_untouched(value: int) -> None:
    """0/negative max_tokens is not an over-cap request — nothing to lower."""
    body = {"model": MODEL, "max_tokens": value}
    out = clamp_max_tokens(body, MODEL, {MODEL: CEILING})
    assert out is body
    assert out["max_tokens"] == value


def test_bool_is_not_a_token_count() -> None:
    """bool is an int subclass — True must not read as ceiling 1 / value 1."""
    # ceiling side: True would clamp everything to one token
    body = {"model": MODEL, "max_tokens": CLIENT_CAP}
    assert clamp_max_tokens(body, MODEL, {MODEL: True}) is body  # type: ignore[dict-item]
    # value side: True is not a numeric cap to compare against
    body2 = {"model": MODEL, "max_tokens": True}
    assert clamp_max_tokens(body2, MODEL, {MODEL: CEILING}) is body2


@pytest.mark.parametrize("bad", ["32768", 32768.0, None])
def test_malformed_ceiling_entry_ignored(bad: Any) -> None:
    """A non-int ceiling (JSON/env typo) is ignored, not applied."""
    body = {"model": MODEL, "max_tokens": CLIENT_CAP}
    out = clamp_max_tokens(body, MODEL, {MODEL: bad})
    assert out is body
    assert out["max_tokens"] == CLIENT_CAP


def test_ceiling_applies_per_model_not_globally() -> None:
    """MODEL-LEVEL cap: the entry keys the serving model, nothing else."""
    ceilings = {MODEL: CEILING}
    capped = clamp_max_tokens({"max_tokens": CLIENT_CAP}, MODEL, ceilings)
    other = {"max_tokens": CLIENT_CAP}
    assert capped["max_tokens"] == CEILING
    assert clamp_max_tokens(other, "GLM-5.3-Flash", ceilings) is other


def test_setting_field_exists_and_defaults_empty() -> None:
    """The knob is a JSON map on Settings (env PCAI_LLM_MAX_TOKENS_CEILING).

    default_factory=dict (mirroring offpeak_model_promql) — the field is an
    opt-in map, so an unconfigured deployment gets {} and the clamp is inert.
    """
    field = type(settings).model_fields["max_tokens_ceiling"]
    assert field.default_factory is dict  # type: ignore[attr-defined]
    assert settings.max_tokens_ceiling == {} or isinstance(settings.max_tokens_ceiling, dict)
    assert "PCAI_LLM_" + "max_tokens_ceiling".upper() == "PCAI_LLM_MAX_TOKENS_CEILING"


# ---------------------------------------------------------------------------
# integration harness (test_ttft_gate.py shape: MockTransport + FakeDB)
# ---------------------------------------------------------------------------


class FakeDB:
    """Budget/chain stub: no budgets, accumulates settle INSERT args."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []
        self.chain_rows: dict[str, list[str]] = {}

    async def fetch(self, sql: str, *a: Any):
        if "FROM model_fallbacks" in sql:
            return [{"fallback_model": n} for n in self.chain_rows.get(a[0], [])]
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


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Fake auth + fake db + mock upstream; returns (FakeDB, key, factory).

    The factory takes the upstream handler so each test scripts its own wire.
    """
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

    def make(handler, models: dict[str, str] | None = None) -> TestClient:
        r = Router.with_mock(handler)
        for name, base in (models or {MODEL: "http://fake-upstream/v1"}).items():
            r.set_endpoints(name, [Endpoint(id=f"ep-{name}", model_name=name, base_url=base, model_type="chat")])
        monkeypatch.setattr(routes_proxy, "model_router", r)
        app = FastAPI()
        app.include_router(routes_proxy.router)
        return TestClient(app, raise_server_exceptions=False)

    return fake, key, make


@pytest.fixture
def ceilings(monkeypatch: pytest.MonkeyPatch):
    """Install a ceiling map on the live settings instance.

    `settings` is a pydantic BaseSettings instance read at CALL time by every
    clamp site (`settings.max_tokens_ceiling`), so patching the instance dict
    is exactly what a deployment's env would produce. monkeypatch restores it.
    """
    monkeypatch.setitem(settings.__dict__, "max_tokens_ceiling", {MODEL: CEILING})
    return {MODEL: CEILING}


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer hllm-x"}


def _chat_response(model: str = MODEL) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
        },
    )


def _sse_bytes(chunks: list[dict]) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# (a) non-streaming path — _proxy_json
# ---------------------------------------------------------------------------


def test_nonstreaming_upstream_receives_clamped_value(env, ceilings) -> None:
    """THE defect path: client sends 262144, the upstream must see the
    ceiling — and the client's own body must be untouched."""
    fake, _key_rec, make = env
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return _chat_response()

    tc = make(handler)
    body = {"model": MODEL, "max_tokens": CLIENT_CAP, "messages": [{"role": "user", "content": "hello"}]}
    snapshot = json.dumps(body, sort_keys=True)
    r = tc.post("/v1/chat/completions", json=body, headers=_h())

    assert r.status_code == 200, r.text
    assert seen["body"]["max_tokens"] == CEILING, "upstream did not receive the clamped max_tokens"
    assert json.dumps(body, sort_keys=True) == snapshot, "the client's request body was mutated"
    assert body["max_tokens"] == CLIENT_CAP, "the caller's dict must keep the client value (settle/estimate read it)"
    # the ledger settles REAL usage, unaffected by the clamp
    ok = [row for row in fake.settled if row[8] == "ok"]
    assert ok and ok[0][3] == 11 and ok[0][4] == 5


def test_nonstreaming_uncapped_model_passes_value_through(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A model with no entry keeps the client's value verbatim (opt-in)."""
    monkeypatch.setitem(settings.__dict__, "max_tokens_ceiling", {"some-other-model": CEILING})
    _fake, _key_rec, make = env
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return _chat_response()

    tc = make(handler)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "max_tokens": CLIENT_CAP, "messages": [{"role": "user", "content": "hi"}]},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert seen["body"]["max_tokens"] == CLIENT_CAP


def test_nonstreaming_no_ceiling_configured_is_byte_identical(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no ceilings configured at all, the wire body is unchanged."""
    monkeypatch.setitem(settings.__dict__, "max_tokens_ceiling", {})
    _fake, _key_rec, make = env
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return _chat_response()

    tc = make(handler)
    body = {"model": MODEL, "max_tokens": CLIENT_CAP, "messages": [{"role": "user", "content": "hi"}]}
    r = tc.post("/v1/chat/completions", json=body, headers=_h())
    assert r.status_code == 200, r.text
    assert seen["body"] == body, "no ceilings configured must be a byte-identical passthrough"


# ---------------------------------------------------------------------------
# (b) streaming path — _stream_chat (SSE)
# ---------------------------------------------------------------------------


def test_streaming_upstream_receives_clamped_value(env, ceilings) -> None:
    """The SSE path rebuilds send_body per iteration; the clamp must apply to
    the body ACTUALLY SENT (including the stream_options merge)."""
    _fake, _key_rec, make = env
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            content=_sse_bytes([{"choices": [{"index": 0, "delta": {"content": "hi"}}]}]),
            headers={"content-type": "text/event-stream"},
        )

    tc = make(handler)
    body = {
        "model": MODEL,
        "max_tokens": CLIENT_CAP,
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    }
    snapshot = json.dumps(body, sort_keys=True)
    with tc.stream("POST", "/v1/chat/completions", json=body, headers=_h()) as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())

    assert "hi" in text, f"stream did not relay content: {text!r}"
    assert seen["body"]["max_tokens"] == CEILING, "streaming upstream did not receive the clamped value"
    # the stream_options merge still happened — the clamp must not drop it
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert json.dumps(body, sort_keys=True) == snapshot, "the client's request body was mutated by the stream path"


def test_streaming_client_supplied_stream_options_survive_clamp(env, ceilings) -> None:
    """A client that already sent stream_options keeps theirs (the `body if
    body.get('stream_options')` branch) — clamp applies there too."""
    _fake, _key_rec, make = env
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            content=_sse_bytes([{"choices": [{"index": 0, "delta": {"content": "ok"}}]}]),
            headers={"content-type": "text/event-stream"},
        )

    tc = make(handler)
    body = {
        "model": MODEL,
        "max_tokens": CLIENT_CAP,
        "stream": True,
        "stream_options": {"include_usage": False},
        "messages": [{"role": "user", "content": "hi"}],
    }
    with tc.stream("POST", "/v1/chat/completions", json=body, headers=_h()) as r:
        assert r.status_code == 200
        "".join(r.iter_text())

    assert seen["body"]["max_tokens"] == CEILING
    assert seen["body"]["stream_options"] == {"include_usage": False}, "client stream_options must ride verbatim"


# ---------------------------------------------------------------------------
# (c) relay path — stream_once (the body the relay's model calls send)
# ---------------------------------------------------------------------------


def _relay_env(monkeypatch: pytest.MonkeyPatch, fake: FakeDB, handler):
    """Minimal relay wiring: grants + catalog stubs, one chat endpoint."""
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.registry as registry_mod
    from pcai_llm.api import routes_mcp
    from pcai_llm.api.routes_proxy import catalog_cache_clear
    from pcai_llm.mcpf import federation

    key = _key()
    catalog_cache_clear()

    async def fake_auth(p: str) -> KeyRecord:
        return key

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

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    monkeypatch.setattr(federation, "db", fake)
    monkeypatch.setattr(federation, "load_grants", fake_grants)
    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch_tools)
    monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream_session)
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
    r = Router.with_mock(handler)
    r.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1", model_type="chat")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    routes_proxy._in_flight.clear()

    app = FastAPI()
    app.include_router(routes_proxy.router)
    return TestClient(app, raise_server_exceptions=False)


def test_relay_upstream_receives_clamped_value(env, ceilings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay's model calls go through stream_once — the body it builds
    must carry the clamped max_tokens for the SERVING model."""
    fake, _key_rec, _make = env
    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            content=_sse_bytes(
                [
                    {"choices": [{"index": 0, "delta": {"content": "all done"}}]},
                    {"usage": {"prompt_tokens": 11, "completion_tokens": 5}},
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    tc = _relay_env(monkeypatch, fake, handler)
    body = {"model": MODEL, "max_tokens": CLIENT_CAP, "messages": [{"role": "user", "content": "hi"}]}
    snapshot = json.dumps(body, sort_keys=True)
    r = tc.post(
        "/v1/chat/completions",
        json=body,
        headers={**_h(), "X-PCAI-Execute-Tools": "1"},
    )

    assert r.status_code == 200, r.text
    assert bodies, "relay made no upstream model call"
    assert bodies[0]["max_tokens"] == CEILING, "the relay's upstream body did not receive the clamped value"
    assert json.dumps(body, sort_keys=True) == snapshot, "the client's request body was mutated by the relay path"
    assert body["max_tokens"] == CLIENT_CAP


# ---------------------------------------------------------------------------
# (d) failover — the clamp follows the model ACTUALLY serving the attempt
# ---------------------------------------------------------------------------


def test_clamp_follows_the_failover_model(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failover hop gets ITS OWN ceiling, not the requested model's.

    The requested model's upstream dies with a 5xx; the chain candidate
    serves. The candidate is uncapped, so IT must receive the client's
    original value even though the requested model carries a ceiling.
    """
    fake, _key_rec, make = env
    other = "healthy-model"
    monkeypatch.setitem(settings.__dict__, "max_tokens_ceiling", {MODEL: CEILING})
    fake.chain_rows[MODEL] = [other]
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        seen.append((request.url.host, payload.get("max_tokens")))
        if "failing" in request.url.host:
            return httpx2.Response(500, json={"error": "boom"})
        return _chat_response(model=other)

    tc = make(handler, models={MODEL: "http://failing-upstream/v1", other: "http://healthy-upstream/v1"})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "max_tokens": CLIENT_CAP, "messages": [{"role": "user", "content": "hi"}]},
        headers=_h(),
    )

    assert r.status_code == 200, r.text
    assert len(seen) == 2, f"expected the primary attempt then the fallback, got {seen}"
    assert seen[0] == ("failing-upstream", CEILING), "the capped model's attempt must be clamped"
    assert seen[1] == ("healthy-upstream", CLIENT_CAP), "the uncapped fallback must get the client's value"


def test_clamp_applies_when_fallback_is_itself_capped(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The inverse: the REQUESTED model is uncapped and the fallback is
    capped — the hop that can actually 400 is the one clamped."""
    fake, _key_rec, make = env
    other = "capped-fallback"
    monkeypatch.setitem(settings.__dict__, "max_tokens_ceiling", {other: CEILING})
    fake.chain_rows[MODEL] = [other]
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        seen.append((request.url.host, payload.get("max_tokens")))
        if "failing" in request.url.host:
            return httpx2.Response(500, json={"error": "boom"})
        return _chat_response(model=other)

    tc = make(handler, models={MODEL: "http://failing-upstream/v1", other: "http://capped-upstream/v1"})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "max_tokens": CLIENT_CAP, "messages": [{"role": "user", "content": "hi"}]},
        headers=_h(),
    )

    assert r.status_code == 200, r.text
    assert seen[0] == ("failing-upstream", CLIENT_CAP)
    assert seen[1] == ("capped-upstream", CEILING), "the capped fallback must be clamped on its own attempt"
