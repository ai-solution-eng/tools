"""Regression tests for the relay-settle P0 batch (security#1 / perf#3+#4+#5,
security#8) and the relay-path P1s (rate headers, catalog timeout,
stream_options negotiation, mid-loop budget enforcement).

The relay loop previously settled NOTHING for its model calls — token AND
USD budgets were fully bypassed for X-PCAI-Execute-Tools requests. These
tests pin the new contract: every relay model call produces a ledger row,
mid-loop budget exhaustion stops the loop cleanly, and the relay executor
enforces the same policy/rate gates /mcp does.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import fastapi
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_mcp, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import registry
from pcai_llm.mcpf import federation
from pcai_llm.routing.router import Endpoint, Router

MODEL = "relay-model"


class LedgerFakeDB:
    """Fake db that ACCUMULATES settle rows so budget SUMs see relay spend —
    mimicking the real ledger-derived budget math against the fake."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []  # settle() INSERT arg tuples
        self.budget_rows: list[dict] = []
        self.cost_budget_rows: list[dict] = []
        self.executed_sql: list[str] = []
        self.pre_spent_tokens = 0  # ledger spend that predates this test's request

    def _spent(self, model: str) -> float:
        return self.pre_spent_tokens + sum(
            r[5] for r in self.rows if r[8] in ("ok", "error") and (r[1] == model or model == "*")
        )

    async def fetch(self, sql: str, *a: Any):
        self.executed_sql.append(sql)
        if "FROM budgets" in sql:
            # single aggregate query: budget rows joined to window spend
            return [{**b, "spent": self._spent(b["model"])} for b in self.budget_rows]
        if "FROM cost_budgets" in sql:
            return [{**b, "spent": 0.0} for b in self.cost_budget_rows]
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetchval(self, sql: str, *a: Any):
        if "SUM(total_tokens)" in sql:
            _, _, model = a
            return self.pre_spent_tokens + sum(
                r[5] for r in self.rows if r[8] in ("ok", "error") and (r[1] == model or model == "*")
            )
        if "SUM(cost_usd)" in sql:
            return 0.0
        return None

    async def execute(self, sql: str, *a: Any):
        self.executed_sql.append(sql)
        await asyncio.sleep(0.001)  # real yield point: cancellable in-flight I/O
        if "usage_ledger" in sql:
            self.rows.append(a)  # (key_id, model, endpoint, prompt, completion,
            #                      total, cached, cost, status, latency, request_id)
        return "INSERT 0 1"

    def settles(self, status: str | None = None) -> list[tuple]:
        return [r for r in self.rows if status is None or r[8] == status]


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


GRANTS = {("logsearch", "search_logs")}


def _sse_bytes(chunks: list[dict]) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def _model_stream(chunks_by_call: list[list[dict]], calls: dict) -> Any:
    """MockTransport handler: one SSE stream per model call, scripted."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        n = calls["model"]
        calls["model"] += 1
        calls.setdefault("bodies", []).append(json.loads(request.content))
        chunks = chunks_by_call[min(n, len(chunks_by_call) - 1)]
        return httpx2.Response(200, content=_sse_bytes(chunks), headers={"content-type": "text/event-stream"})

    return handler


@pytest.fixture
def relay_env(monkeypatch: pytest.MonkeyPatch):
    """Wire fake auth/db/registry/MCP fetch for RELAY-path tests.

    Returns (client_factory, LedgerFakeDB, spies). The factory takes the
    key + upstream handler so tests control both sides.
    """
    fake = LedgerFakeDB()
    calls: dict[str, Any] = {"fetch_tools": []}
    upstream_calls: dict[str, int] = {"n": 0}
    # the relay's catalog TTL cache is module-level process state — each test
    # must see a cold preflight or _fetch_tools is never called
    from pcai_llm.api.routes_proxy import catalog_cache_clear

    catalog_cache_clear()

    def make(key: KeyRecord, handler, tool_policy: str = "all", allowed_tools: list[str] | None = None) -> TestClient:
        import pcai_llm.api.auth as auth_mod
        import pcai_llm.core.cascade as cascade_mod

        async def fake_auth(p: str) -> KeyRecord:
            return key

        async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
            return GRANTS

        async def fake_fetch_tools(url: str, headers: dict, timeout_s: float | None = None):
            calls["fetch_tools"].append({"url": url, "timeout_s": timeout_s})
            return [{"tool": "search_logs", "description": "search"}]

        class FakeBlock:
            def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
                return {"type": "text", "text": "ok"}

        class FakeResult:
            def __init__(self) -> None:
                self.is_error = False
                self.content = [FakeBlock()]

        async def fake_upstream_session(url, headers, op, timeout_s=None):
            upstream_calls["n"] += 1
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
        registry._mcp_servers_cache = [
            {
                "id": "srv1",
                "name": "logsearch",
                "url": "http://logsearch.svc:8080",
                "headers": {},
                "auth_secret": None,
                "auth_token_enc": None,
                "enabled": True,
                "tool_policy": tool_policy,
                "allowed_tools": allowed_tools or [],
            }
        ]
        r = Router.with_mock(handler)
        r.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1", model_type="chat")])
        monkeypatch.setattr(routes_proxy, "model_router", r)
        routes_proxy._in_flight.clear()

        app = FastAPI()
        app.include_router(routes_proxy.router)
        return TestClient(app, raise_server_exceptions=False)

    return make, fake, calls, upstream_calls


RELAY_HEADERS = {"Authorization": "Bearer x", "X-PCAI-Execute-Tools": "1"}


def test_relay_settles_every_model_call(relay_env) -> None:
    """THE budget-bypass fix: a relay request's model calls now produce
    ledger rows with the upstream's real usage."""
    make, fake, calls, _upstream = relay_env
    calls["model"] = 0
    handler = _model_stream(
        [
            [
                {"choices": [{"delta": {"content": "all done"}}]},
                {"usage": {"prompt_tokens": 11, "completion_tokens": 5}},
            ]
        ],
        calls,
    )
    key = _key()
    tc = make(key, handler)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    ok_rows = fake.settles("ok")
    assert ok_rows, "relay model call produced NO ledger row"
    row = ok_rows[0]
    assert row[0] == key.id
    assert row[1] == MODEL and row[2] == "chat"
    assert row[3] == 11 and row[4] == 5, "real upstream usage must be settled"
    # and the SSE stream still carries the usage event for the client
    assert '"model_usage"' in r.text and '"prompt": 11' in r.text


def test_relay_settle_falls_back_to_estimate_without_usage(relay_env) -> None:
    """Upstreams that never emit usage still settle — on the token estimate,
    never a missing row (missing rows silently under-charge budgets)."""
    make, fake, calls, _upstream = relay_env
    calls["model"] = 0
    body_content = "x" * 40  # 40 chars -> estimate 10 prompt tokens
    handler = _model_stream([[{"choices": [{"delta": {"content": "done"}}]}]], calls)
    tc = make(_key(), handler)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "max_tokens": 7, "messages": [{"role": "user", "content": body_content}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    ok_rows = fake.settles("ok")
    assert ok_rows and ok_rows[0][3] == 17 and ok_rows[0][4] == 0, (
        f"expected estimate (40//4 + max_tokens 7 = 17, 0), got {(ok_rows[0][3], ok_rows[0][4]) if ok_rows else None}"
    )


def test_relay_midloop_budget_exhaustion_stops_loop(relay_env) -> None:
    """Per-iteration budget check: once relay spend crosses the limit the
    loop STOPS (no further model calls), a guard event + [DONE] reach the
    client, and a budget_exceeded marker row records what was spent."""
    make, fake, calls, _upstream = relay_env
    fake.budget_rows = [{"model": "*", "period": "month", "limit_tokens": 5}]
    calls["model"] = 0
    handler = _model_stream(
        [
            # call 1: asks for a tool (usage 8 tokens -> over the 5 limit)
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
                                        "function": {"name": "logsearch__search_logs", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"usage": {"prompt_tokens": 8, "completion_tokens": 0}},
            ],
            # call 2 must NEVER happen
            [{"choices": [{"delta": {"content": "should not get here"}}]}],
        ],
        calls,
    )
    tc = make(_key(), handler)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert calls["model"] == 1, "mid-loop exhaustion must stop before the next model call"
    assert '"guard"' in r.text and "budget exceeded" in r.text
    assert r.text.rstrip().endswith("data: [DONE]")
    assert fake.settles("ok"), "the first call's spend must still be settled"
    markers = fake.settles("budget_exceeded")
    assert markers, "budget_exceeded marker row missing"
    assert markers[0][3] == 8  # what was spent in the relay so far


def test_relay_executor_denies_by_server_policy(relay_env) -> None:
    """security#8: the relay executor enforces per-server tool policy —
    an allowlisted server with the tool NOT on it is refused (and settled
    as an error row) exactly like /mcp refuses it."""
    make, fake, calls, upstream = relay_env
    calls["model"] = 0
    handler = _model_stream(
        [
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
                                        "function": {"name": "logsearch__search_logs", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"usage": {"prompt_tokens": 2, "completion_tokens": 1}},
            ],
            [{"choices": [{"delta": {"content": "ok, tool refused"}}]}],
        ],
        calls,
    )
    tc = make(_key(), handler, tool_policy="allowlist", allowed_tools=["other_tool"])
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert upstream["n"] == 0, "policy-denied tool must never reach the upstream server"
    assert '"ok": false' in r.text, "tool_call event must report the failure"
    # "denied" is its own ledger status since the 2026-10-26 hardening wave
    # (P2-2 fidelity) - a policy refusal is not an upstream error.
    denied_rows = [t for t in fake.settles("denied") if str(t[2]).startswith("mcp:")]
    assert denied_rows, "policy denial must settle a denied row (mirrors /mcp)"


def test_relay_executor_enforces_rate_limit(relay_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """security#8: the relay's tool bursts consume the key's RPM window —
    an exhausted window refuses the tool call (and never hits upstream)."""
    make, _fake, calls, upstream = relay_env

    rate_calls = {"n": 0}

    async def exhausted_rate(key_id, rpm, tpm, est, request_id=""):
        rate_calls["n"] += 1
        if rate_calls["n"] == 1:  # the request's own gate: room left
            return budgets_mod.GateResult(True, remaining_requests=1, remaining_tokens=100)
        return budgets_mod.GateResult(False, "RPM limit 1 exceeded (2 this minute)")

    monkeypatch.setattr(routes_proxy, "check_rate_limit", exhausted_rate)
    calls["model"] = 0
    handler = _model_stream(
        [
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
                                        "function": {"name": "logsearch__search_logs", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"usage": {"prompt_tokens": 2, "completion_tokens": 1}},
            ],
            [{"choices": [{"delta": {"content": "ok"}}]}],
        ],
        calls,
    )
    tc = make(_key(), handler)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert upstream["n"] == 0, "rate-limited tool must never reach upstream"


def test_relay_catalog_fetch_uses_bounded_timeout(relay_env) -> None:
    """security#7: the relay's per-server catalog fetch passes a short
    metadata timeout (15s), not the 600s upstream timeout — one hung MCP
    server must not freeze the relay's first request."""
    make, _fake, calls, _upstream = relay_env
    calls["model"] = 0
    handler = _model_stream([[{"choices": [{"delta": {"content": "hi"}}]}]], calls)
    tc = make(_key(), handler)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert calls["fetch_tools"], "relay must fetch the granted server's catalog"
    assert calls["fetch_tools"][0]["timeout_s"] == 15.0


def test_relay_sends_stream_options_and_strips_on_4xx(relay_env) -> None:
    """stream_options include_usage rides the relayed body; an upstream that
    4xxs on it is retried WITHOUT it (usage then falls back to estimate)."""
    make, _fake, _calls, _upstream = relay_env
    bodies: list[dict] = []
    attempts: list[list[dict]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        b = json.loads(request.content)
        bodies.append(b)
        if "stream_options" in b:
            return httpx2.Response(400, json={"error": "stream_options unsupported"})
        attempts.append(b)
        return httpx2.Response(
            200,
            content=_sse_bytes([{"choices": [{"delta": {"content": "hi"}}]}]),
            headers={"content-type": "text/event-stream"},
        )

    tc = make(_key(), handler)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert bodies and "stream_options" in bodies[0]
    assert bodies[0]["stream_options"] == {"include_usage": True}
    assert attempts and "stream_options" not in attempts[0], "4xx on stream_options must be retried without it"
    assert '"model_usage"' in r.text


def test_relay_response_carries_budget_warning_and_rate_headers(relay_env) -> None:
    """gaps P1/P9 on the relay path: X-Budget-Warning (was dropped on relay
    responses) and X-RateLimit-Remaining-* both ride the StreamingResponse."""
    make, fake, calls, _upstream = relay_env
    fake.budget_rows = [{"model": "*", "period": "month", "limit_tokens": 1000}]
    fake.pre_spent_tokens = 950  # 95% -> warn threshold
    calls["model"] = 0
    handler = _model_stream([[{"choices": [{"delta": {"content": "hi"}}]}]], calls)
    tc = make(_key(), handler)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    # 1 request this minute against rpm=1000 -> 999 remaining (fake fetchrow)
    assert r.headers.get("x-ratelimit-remaining-requests") == "999"
    assert r.headers.get("x-ratelimit-remaining-tokens") == "999990"
    assert r.headers.get("x-pcai-relay") == "streaming"


def test_relay_400_no_grants_releases_slot(relay_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """security#2: the 'no MCP grants' 400 fired AFTER the concurrency
    increment with no release — N such requests bricked a capped key. The
    check now hoists BEFORE the gates, so no slot is ever taken."""
    make, _fake, _calls, _upstream = relay_env
    key = _key(max_concurrent=2, scopes=["proxy"])  # no mcp scope, grants patched away below

    async def no_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return set()

    tc = make(key, _model_stream([[{"choices": [{"delta": {"content": "x"}}]}]], {"model": 0}))
    monkeypatch.setattr(federation, "load_grants", no_grants)  # after make(): wins
    for _ in range(5):
        r = tc.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers=RELAY_HEADERS,
        )
        assert r.status_code == 400
    assert routes_proxy._in_flight.get(key.id, 0) == 0, (
        "the key must NOT be bricked: no slot may leak on the relay-400 path"
    )


def test_postgate_exception_releases_slot(relay_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any unexpected post-gate exception (e.g. registry DB failure while
    building the relay catalog) must release the concurrency slot."""
    make, _fake, _calls, _upstream = relay_env
    key = _key(max_concurrent=1)

    async def broken_servers():
        raise RuntimeError("db down")

    monkeypatch.setattr(registry, "mcp_servers", broken_servers)
    tc = make(key, _model_stream([[{"choices": [{"delta": {"content": "x"}}]}]], {"model": 0}))
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 500
    assert routes_proxy._in_flight.get(key.id, 0) == 0, "post-gate exception leaked the slot"


def test_relay_sse_finish_survives_disconnect_cancellation(relay_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """perf#4: a client disconnect CANCELS the stream task — the finally
    must still release the slot (sync, first) and complete the ledger
    settle (shielded), not die at its first await."""
    _make, fake, _calls, _upstream = relay_env
    key = _key()

    parked = asyncio.Event()

    async def chunked() -> Any:
        yield b'data: {"model":"m","choices":[{"delta":{"content":"hi"}}]}\n\n'
        await parked.wait()
        yield b"data: [DONE]\n\n"

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=chunked(), headers={"content-type": "text/event-stream"})

    r = Router.with_mock(handler)
    r.set_endpoints(MODEL, [Endpoint(id="ep", model_name=MODEL, base_url="http://fake/v1")])
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)

    releases: list[int] = []

    import anyio

    async def main() -> None:
        req = r.client.build_request("POST", "http://fake/v1/chat/completions", json={})
        resp = await r.client.send(req, stream=True)
        ledger = routes_proxy._StreamLedger(key, MODEL, 10, "rid", time.monotonic())
        agen = routes_proxy._relay_sse(resp, ledger, release_slot=lambda: releases.append(1))
        first = await agen.__anext__()
        assert b"hi" in first

        async def consume() -> None:
            async for _ in agen:
                pass

        # anyio task group = Starlette's actual runtime semantics: cancelling
        # the scope is LEVEL-TRIGGERED, so every await inside the finally
        # would raise until the scope exits (plain asyncio delivers the
        # cancellation once and would pass even without the shield).
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await anyio.sleep(0.1)  # parked mid-stream (chunk1 consumed)
            tg.cancel_scope.cancel()

        # BOTH side effects must have survived the disconnect cancellation
        assert releases == [1], "slot leaked on disconnect"
        assert fake.settles(), "ledger settle skipped on disconnect"
        await resp.aclose()

    anyio.run(main)


# ---------------------------------------------------------------------------
# admission failover: rate/budget 429 walks the profile ordering
# ---------------------------------------------------------------------------


def test_admit_with_failover_walks_candidates_on_rate_429(monkeypatch) -> None:
    """A rate-limit 429 on the chosen model walks the profile ordering instead
    of failing the turn — the conversation survives on the next candidate."""
    import asyncio

    from pcai_llm.api import routes_proxy

    calls: list[str] = []

    async def gates(key, model, est, endpoint, request_id, started, precomputed_budget=None):
        calls.append(model)
        if model == "primary":
            raise fastapi.HTTPException(429, "TPM limit 100000 exceeded (400000 this minute)")
        return SimpleNamespace(warn_level=0, rate_headers={})

    monkeypatch.setattr(routes_proxy, "_run_gates", gates)
    key = SimpleNamespace(id=uuid.uuid4())
    model, _gate, failed_from = asyncio.run(
        routes_proxy._admit_with_failover(key, ["primary", "fallback-model"], 100, "rid", 0.0, None)
    )
    assert model == "fallback-model" and failed_from == "primary"
    assert calls == ["primary", "fallback-model"]


def test_admit_with_failover_concurrency_does_not_walk(monkeypatch) -> None:
    """A CONCURRENCY 429 does not fail over — the cap is key-wide; another
    model cannot help (the walk would just double-charge the window)."""
    import asyncio

    from pcai_llm.api import routes_proxy

    async def gates(key, model, est, endpoint, request_id, started, precomputed_budget=None):
        raise fastapi.HTTPException(429, "concurrency limit 2 exceeded (2 in flight)")

    monkeypatch.setattr(routes_proxy, "_run_gates", gates)
    key = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(fastapi.HTTPException) as ei:
        asyncio.run(routes_proxy._admit_with_failover(key, ["a", "b"], 1, "rid", 0.0, None))
    assert "concurrency limit" in str(ei.value.detail)


def test_admit_with_failover_all_exhausted_raises_last(monkeypatch) -> None:
    """Every candidate exhausted -> the LAST candidate's refusal surfaces (the
    429 body explains the whole walk via the attempts detail)."""
    import asyncio

    from pcai_llm.api import routes_proxy

    async def gates(key, model, est, endpoint, request_id, started, precomputed_budget=None):
        raise fastapi.HTTPException(429, f"TPM limit exceeded on {model}")

    monkeypatch.setattr(routes_proxy, "_run_gates", gates)
    key = SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(fastapi.HTTPException) as ei:
        asyncio.run(routes_proxy._admit_with_failover(key, ["a", "b"], 1, "rid", 0.0, None))
    assert "on b" in str(ei.value.detail)


# ---------------------------------------------------------------------------
# mid-relay fallback tail + the `auto` cascade alias (the "GLM budget dies
# mid-turn → flow into Qwen" fix — the user-baby debug case)
# ---------------------------------------------------------------------------


def test_relay_fallback_tail_continues_after_serving_model() -> None:
    """Pinned GLM + profile ordering [GLM, Qwen, other]: the mid-relay chain
    is the ordering AFTER the serving model — a pinned request whose model
    dies of budget mid-stream flows into the next ordered model instead of
    ending answerless."""
    registered = {"GLM-5.3-Flash", "Qwen3.8-27B", "other"}
    order = ["GLM-5.3-Flash", "Qwen3.8-27B", "other"]
    assert routes_proxy._relay_fallback_tail("GLM-5.3-Flash", order, registered, []) == [
        "Qwen3.8-27B",
        "other",
    ]


def test_relay_fallback_tail_filters_registered_and_allowed() -> None:
    registered = {"GLM-5.3-Flash", "Qwen3.8-27B"}
    order = ["GLM-5.3-Flash", "ghost", "Qwen3.8-27B"]
    # unregistered candidates are skipped (admission would 404 them anyway)
    assert routes_proxy._relay_fallback_tail("GLM-5.3-Flash", order, registered, []) == ["Qwen3.8-27B"]
    # allowed_models is a hard boundary, not a preference
    assert (
        routes_proxy._relay_fallback_tail(
            "GLM-5.3-Flash", ["GLM-5.3-Flash", "Qwen3.8-27B"], registered, ["GLM-5.3-Flash"]
        )
        == []
    )
    # serving model not itself in the ordering → the whole ordering chains
    # (filtered to registered; "ghost" drops out)
    assert routes_proxy._relay_fallback_tail("solo", order, registered, []) == [
        "GLM-5.3-Flash",
        "Qwen3.8-27B",
    ]
    # no ordering → no chain (mid-relay exhaustion halts, as before)
    assert routes_proxy._relay_fallback_tail("GLM-5.3-Flash", [], registered, []) == []


async def test_list_models_auto_entry_precedes_registered(monkeypatch) -> None:
    """The `auto` alias is first-class in /v1/models when the key's profile
    has an ordering — the chat dropdown picks it up with no special-casing.
    Suppressed when there is no ordering (auto would 404 at send time) and
    when no ordered candidate is callable (registered ∧ allowed)."""
    monkeypatch.setattr(routes_proxy.model_router, "models", lambda: ["GLM-5.3-Flash", "Qwen3.8-27B"])

    async def with_order(key_id):
        return ["GLM-5.3-Flash", "Qwen3.8-27B"]

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", with_order)
    res = await routes_proxy.list_models(_key())
    # The alias PAIR (2026-10-04): `auto` stays FIRST (legacy semantics, its
    # entry shape unchanged) and `auto-demand-aware` is advertised directly
    # after it — the load-aware head as an explicit opt-in name.
    assert [m["id"] for m in res["data"]] == ["auto", "auto-demand-aware", "GLM-5.3-Flash", "Qwen3.8-27B"]
    assert res["data"][0]["pcai"]["type"] == "cascade"
    assert res["data"][0]["pcai"]["alias"] is True
    assert res["data"][0]["pcai"]["order"] == ["GLM-5.3-Flash", "Qwen3.8-27B"]
    # The new alias carries the distinguishing marker; `auto` must NOT.
    assert res["data"][1]["id"] == "auto-demand-aware"
    assert res["data"][1]["pcai"]["demand_aware"] is True
    assert "demand_aware" not in res["data"][0]["pcai"]

    async def no_order(key_id):
        return []

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", no_order)
    res = await routes_proxy.list_models(_key())
    assert [m["id"] for m in res["data"]] == ["GLM-5.3-Flash", "Qwen3.8-27B"]

    # ordering exists but nothing callable: allowed_models excludes it all
    res = await routes_proxy.list_models(_key(allowed_models=["other"]))
    assert [m["id"] for m in res["data"]] == []


# ---------------------------------------------------------------------------
# caller-aware /v1/models (roadmap §6 — documented intent shipped)
# ---------------------------------------------------------------------------


async def test_list_models_pcai_type_and_primary(monkeypatch) -> None:
    """Every registered entry carries pcai.type (pool endpoint model_type);
    the FIRST cascade-ordered callable model is pcai.primary — the same
    fields the chat console's dropdown has rendered since the meta-chip
    wave, now lit up for every OpenAI-compatible consumer."""
    monkeypatch.setattr(routes_proxy.model_router, "models", lambda: ["emb-model", "GLM-5.3-Flash", "Qwen3.8-27B"])

    def pool_for(m: str):
        from pcai_llm.routing.router import Endpoint, Pool

        t = {"emb-model": "embedding"}.get(m, "chat")
        return Pool(endpoints=[Endpoint(id=f"{m}-1", model_name=m, base_url=f"http://{m}", model_type=t)])

    monkeypatch.setattr(routes_proxy.model_router, "pool", pool_for)

    async def with_order(key_id):
        return ["Qwen3.8-27B", "GLM-5.3-Flash"]  # cascade prefers Qwen

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", with_order)
    res = await routes_proxy.list_models(_key())
    by_id = {m["id"]: m for m in res["data"]}
    assert by_id["emb-model"]["pcai"]["type"] == "embedding"
    assert by_id["GLM-5.3-Flash"]["pcai"]["type"] == "chat"
    # 2026-10-28: the entry shape gained pcai.capacity (this key's budget
    # headroom for that model right now). Assert it explicitly rather than
    # loosening the comparison: the fail-open shape is part of the contract
    # (no DB in this test -> "not evaluated" must read as available, never
    # as blocked).
    assert by_id["Qwen3.8-27B"]["pcai"] == {
        "type": "chat",
        "primary": True,
        "capacity": {"ok": True, "remaining_tokens": None, "blocked_by": ""},
    }
    assert by_id["GLM-5.3-Flash"]["pcai"].get("primary") is None  # not first
    # auto entry still precedes with its own cascade type
    assert res["data"][0]["id"] == "auto" and res["data"][0]["pcai"]["type"] == "cascade"

    # unprofiled key: types ride, no primary anywhere
    async def no_order(key_id):
        return []

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", no_order)
    res = await routes_proxy.list_models(_key())
    assert all("primary" not in m.get("pcai", {}) for m in res["data"])
    assert all("type" in m.get("pcai", {}) for m in res["data"] if m["id"] != "auto")
