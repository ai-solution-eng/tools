"""Effective-prompt capture (ROADMAP §7 phase 2 wave C) + usage-detail passthrough.

Covers, in order:
- the pure boundedness rules (core.effective.preview_messages): per-message
  cap with the honest marker, the rolling tail window's _meta counter, the
  whole-payload slice, the never-full-schemas tools summary;
- the record() edges: fail-safe on a broken DB, retention prune piggyback
  (SQL + interval asserted on the fake), the master switch;
- the relay wiring through the FULL route (test_canonical.py's wiring_env
  pattern): one row per REAL model call with iteration/thread/request
  context, and the wave's FIDELITY proof — the captured preview reflects
  the STRIPPED + RECONCILED messages the upstream received (asserted
  against the mock upstream's captured request bodies);
- the usage-detail passthrough (parent addendum): upstream
  prompt_tokens_details.cached_tokens reaches the settle row AND the
  client-facing model_usage event (plus the verbatim usage dict);
- the admin read surface (require_admin imported from routes_admin).

No live Postgres — the suite's fake-db style throughout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_effective, routes_mcp, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import canonical, effective, toolrelay
from pcai_llm.routing.router import Endpoint, Router

MODEL = "relay-model"
GRANTS = {("logsearch", "search_logs")}


# ---------------------------------------------------------------------------
# 1. preview_messages(): the bounded serialization rules
# ---------------------------------------------------------------------------


def test_preview_small_messages_pass_through() -> None:
    msgs = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "run the check"},
    ]
    out = effective.preview_messages(msgs, None)
    entries = json.loads(out["messages"])
    assert [e["role"] for e in entries] == ["system", "user"]
    # canonical JSON, sort_keys: the preview of one message re-serializes to itself
    assert json.loads(entries[1]["preview"]) == msgs[1]
    assert out["tools"] == ""  # toolless call: no tool noise


def test_preview_message_truncation_marker_counts_true_cut() -> None:
    """A huge message is truncated at the cap with an explicit marker whose N
    is the EXACT number of chars dropped from the CANONICAL JSON (the reader
    can size the cut)."""
    msgs = [{"role": "user", "content": "x" * 5000}]
    out = effective.preview_messages(msgs, None, message_cap=100)
    entries = json.loads(out["messages"])
    preview = entries[0]["preview"]
    canon_len = len(json.dumps(msgs[0], sort_keys=True, default=str))
    assert canon_len > 100, "the fixture message must actually exceed the cap"
    assert len(preview) == 100 + len("…[truncated N chars]".replace("N", str(canon_len - 100)))
    assert preview.endswith(f"…[truncated {canon_len - 100} chars]")


def test_preview_tail_window_prepends_meta_omission() -> None:
    """Only the LAST tail_messages messages are kept; omitted history is
    counted by a leading _meta entry — the preview states its own bounds."""
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(50)]
    out = effective.preview_messages(msgs, None, tail_messages=40)
    entries = json.loads(out["messages"])
    assert len(entries) == 41
    assert entries[0] == {"role": "_meta", "preview": "…[10 earlier messages omitted]"}
    kept = [json.loads(e["preview"])["content"] for e in entries[1:]]
    assert kept == [f"m{i}" for i in range(10, 50)], "the kept window is the LAST 40"


def test_preview_total_cap_hard_slices_with_marker() -> None:
    """The assembled JSON over the total cap is sliced with an end marker —
    worst-case row size is a constant regardless of conversation length."""
    msgs = [{"role": "user", "content": "y" * 500} for _ in range(30)]
    out = effective.preview_messages(msgs, None, message_cap=5000, total_cap=2000)
    assert len(out["messages"]) == 2000 + len(" …[effective-prompt preview truncated, 12345 chars total]")
    assert out["messages"].endswith("chars total]")
    assert out["messages"].startswith("["), "the sliced payload stays a JSON prefix + marker"


def test_preview_tools_summary_is_bounded_not_full_schemas() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "logsearch__search_logs",
                "description": "d" * 500,  # long description -> front 200 only
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
    ]
    out = effective.preview_messages([], tools)
    summary = json.loads(out["tools"])
    assert len(summary) == 1
    t = summary[0]
    assert set(t) == {"name", "description", "schema_bytes"}, "shape: name + description[:200] + schema_bytes"
    assert t["name"] == "logsearch__search_logs"
    assert len(t["description"]) == 200 + len("…[truncated 300 chars]")
    assert t["schema_bytes"] == len(json.dumps(tools[0], sort_keys=True, default=str))
    # the schema TEXT never appears in the summary — size only
    assert '"properties"' not in out["tools"]


def test_preview_never_mutates_inputs() -> None:
    msgs = [{"role": "user", "content": "hi"}]
    tools = [{"type": "function", "function": {"name": "t", "description": "x"}}]
    snap = (json.dumps(msgs), json.dumps(tools))
    effective.preview_messages(msgs, tools)
    assert (json.dumps(msgs), json.dumps(tools)) == snap


# ---------------------------------------------------------------------------
# 2. record(): best-effort write + retention prune piggyback
# ---------------------------------------------------------------------------


class _EffectiveFakeDB:
    """Fake db: captures effective_prompts INSERTs and DELETEs separately,
    plus every query (filter/bind assertions on the read surface)."""

    def __init__(self) -> None:
        self.inserts: list[tuple] = []
        self.deletes: list[tuple[str, tuple]] = []
        self.rows: list[dict] = []
        self.queries: list[tuple[str, tuple]] = []
        self.fail = False

    async def execute(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        if self.fail:
            raise RuntimeError("db down")
        if "INSERT INTO effective_prompts" in sql:
            self.inserts.append(args)
        if "DELETE FROM effective_prompts" in sql:
            self.deletes.append((sql, args))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return [dict(r) for r in self.rows]

    async def fetchrow(self, sql: str, *args: Any):
        return None

    async def fetchval(self, sql: str, *args: Any):
        return None


@pytest.fixture
def eff_db(monkeypatch: pytest.MonkeyPatch) -> _EffectiveFakeDB:
    fake = _EffectiveFakeDB()
    monkeypatch.setattr(effective, "db", fake)
    return fake


def test_record_writes_bounded_row(eff_db: _EffectiveFakeDB) -> None:
    key_id = uuid.uuid4()
    msgs = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "t", "description": "d"}}]
    asyncio.run(effective.record(key_id, "thread-1", "req-1", 2, 1, MODEL, msgs, tools))
    assert len(eff_db.inserts) == 1
    row = eff_db.inserts[0]
    assert row[0] == key_id and row[1] == "thread-1" and row[2] == "req-1"
    assert row[3] == 2 and row[4] == 1 and row[5] == MODEL
    entries = json.loads(row[6])
    assert json.loads(entries[0]["preview"]) == msgs[0]
    assert json.loads(row[7])[0]["name"] == "t"


def test_record_swallows_db_failure_and_warns(eff_db: _EffectiveFakeDB, caplog: pytest.LogCaptureFixture) -> None:
    eff_db.fail = True
    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.effective"):
        asyncio.run(  # must NOT raise — capture never fails a call
            effective.record(uuid.uuid4(), "t", "req", 0, 1, MODEL, [{"role": "user", "content": "x"}], None)
        )
    assert eff_db.inserts == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_record_master_switch_off_writes_nothing(eff_db: _EffectiveFakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "effective_prompt_capture", False)
    asyncio.run(effective.record(uuid.uuid4(), "t", "req", 0, 1, MODEL, [{"role": "user", "content": "x"}], None))
    assert eff_db.inserts == []
    assert eff_db.deletes == []


def test_record_prune_piggybacks_with_configured_interval(
    eff_db: _EffectiveFakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retention piggybacks the capture write with the knob's interval and
    the documented SQL; at most once per 60s process-wide (the module-level
    last-prune stamp), so this test resets that stamp first."""
    monkeypatch.setattr(settings, "effective_prompt_retain_s", 604800.0)
    monkeypatch.setattr(effective, "_last_prune", 0.0)
    asyncio.run(effective.record(uuid.uuid4(), "t", "req", 0, 1, MODEL, [{"role": "user", "content": "x"}], None))
    assert len(eff_db.deletes) == 1
    sql, args = eff_db.deletes[0]
    assert "DELETE FROM effective_prompts" in sql
    assert "make_interval(secs => $1)" in sql
    assert args == (604800.0,)

    # second write inside the 60s window: no second prune
    asyncio.run(effective.record(uuid.uuid4(), "t", "req", 0, 1, MODEL, [{"role": "user", "content": "x"}], None))
    assert len(eff_db.deletes) == 1


def test_record_retention_zero_never_prunes(eff_db: _EffectiveFakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "effective_prompt_retain_s", 0.0)
    monkeypatch.setattr(effective, "_last_prune", 0.0)
    asyncio.run(effective.record(uuid.uuid4(), "t", "req", 0, 1, MODEL, [{"role": "user", "content": "x"}], None))
    assert len(eff_db.inserts) == 1 and eff_db.deletes == []


def test_record_prune_failure_never_surfaces(
    eff_db: _EffectiveFakeDB, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The INSERT succeeds, the DELETE raises: capture still reports the row
    written and only a warning escapes — prune loss delays cleanup, never
    breaks anything."""
    monkeypatch.setattr(settings, "effective_prompt_retain_s", 604800.0)
    monkeypatch.setattr(effective, "_last_prune", 0.0)

    async def failing_delete(sql: str, *args: Any):
        if "DELETE FROM effective_prompts" in sql:
            raise RuntimeError("prune boom")
        eff_db.inserts.append(args)
        return "INSERT 0 1"

    monkeypatch.setattr(eff_db, "execute", failing_delete)
    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.effective"):
        asyncio.run(effective.record(uuid.uuid4(), "t", "req", 0, 1, MODEL, [{"role": "user", "content": "x"}], None))
    assert len(eff_db.inserts) == 1, "the capture row was still written"
    assert any("prune" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. relay wiring: one row per real model call, captured at the wire
# ---------------------------------------------------------------------------


class WiringFakeDB(_EffectiveFakeDB):
    """_EffectiveFakeDB plus canonical-store + ledger-shaped accounting —
    test_canonical.py's _StoreFakeDB behavior, so the wiring_env fixture
    drives the full route."""

    def __init__(self) -> None:
        super().__init__()
        self.canonical_rows: dict[tuple[Any, str], str] = {}
        self.settled: list[tuple] = []

    async def execute(self, sql: str, *args: Any):
        if "usage_ledger" in sql:
            self.settled.append(args)
        if "canonical_conversations" in sql:
            self.canonical_rows[(args[0], args[1])] = args[2]
            return "INSERT 0 1"
        return await super().execute(sql, *args)

    async def fetchrow(self, sql: str, *args: Any):
        if "canonical_conversations" in sql:
            raw = self.canonical_rows.get((args[0], args[1]))
            return {"messages": raw} if raw is not None else None
        if "profiles" in sql:
            return None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetch(self, sql: str, *args: Any):
        if "FROM budgets" in sql or "FROM cost_budgets" in sql:
            return []
        return await super().fetch(sql, *args)

    async def fetchval(self, sql: str, *args: Any):
        if "SUM(cost_usd)" in sql:
            return 0.0
        return None

    # capture-facing helpers (assertions read better)
    def capture_rows(self) -> list[tuple]:
        return list(self.inserts)

    def settle_rows(self) -> list[tuple]:
        return list(self.settled)


def _sse(chunks: list[dict]) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


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
def wiring_env(monkeypatch: pytest.MonkeyPatch):
    """Relay-route harness (test_canonical.py's wiring_env, replicated with
    the capture-aware fake): fake auth/db/registry/MCP plus a mock upstream
    that RECORDS the request bodies it receives. Returns
    (client_factory, WiringFakeDB)."""
    fake = WiringFakeDB()

    from pcai_llm.api.routes_proxy import catalog_cache_clear

    catalog_cache_clear()

    def make(key: KeyRecord, handler: Any = None) -> TestClient:
        import pcai_llm.api.auth as auth_mod
        import pcai_llm.core.budgets as budgets_mod
        import pcai_llm.core.cascade as cascade_mod
        import pcai_llm.core.costs as costs_mod
        from pcai_llm.mcpf import federation

        calls: dict[str, Any] = {"bodies": []}

        async def fake_auth(p: str) -> KeyRecord:
            return key

        async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
            return GRANTS

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
            # the relay EXECUTES granted MCP tools in-cluster — fake the
            # upstream MCP session so a scripted tool_call iteration proceeds
            # (test_relay_settle.py's relay_env pattern)
            return FakeResult()

        if handler is None:

            def handler(request: httpx2.Request) -> httpx2.Response:  # type: ignore[misc]
                payload = json.loads(request.content)
                calls["bodies"].append(payload)
                return httpx2.Response(
                    200,
                    content=_sse(
                        [
                            {"choices": [{"delta": {"content": "done"}}]},
                            {"usage": {"prompt_tokens": 3, "completion_tokens": 2}},
                        ]
                    ),
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
        import pcai_llm.core.registry as registry_mod

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
        routes_proxy._loop_guard.clear()

        app = FastAPI()
        app.include_router(routes_proxy.router)
        fake.upstream_calls = calls  # type: ignore[attr-defined]
        return TestClient(app, raise_server_exceptions=False)

    return make, fake


RELAY_HEADERS = {"Authorization": "Bearer x", "X-PCAI-Execute-Tools": "1"}


def _bodies(fake: WiringFakeDB) -> list[dict]:
    return fake.upstream_calls["bodies"]  # type: ignore[attr-defined]


def _wiring_rows(fake: WiringFakeDB) -> list[tuple]:
    return fake.capture_rows()  # type: ignore[attr-defined]


BASH_TOOL = {"type": "function", "function": {"name": "bash", "description": "run a shell command"}}


def _tool_call_sse(call_id: str, name: str, arguments: str) -> bytes:
    return _sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ]
                        }
                    }
                ]
            },
            {"usage": {"prompt_tokens": 4, "completion_tokens": 1}},
        ]
    )


def _content_sse(text: str, usage: dict | None = None) -> bytes:
    chunks: list[dict] = [{"choices": [{"delta": {"content": text}}]}]
    chunks.append({"usage": usage or {"prompt_tokens": 4, "completion_tokens": 1}})
    return _sse(chunks)


def _scripted_handler(script: list[bytes], bodies: list[dict]) -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            content=script[min(len(bodies) - 1, len(script) - 1)],
            headers={"content-type": "text/event-stream"},
        )

    return handler


def test_wiring_multi_iteration_turn_captures_one_row_per_model_call(
    wiring_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 model calls -> 2 captured rows: iteration numbers 0 and 1, the
    canonical thread fingerprint and the request id on both."""
    make, fake = wiring_env
    monkeypatch.setattr(effective, "db", fake)  # the capture seam
    key = _key()
    bodies: list[dict] = []
    tc = make(
        key,
        handler=_scripted_handler(
            [
                _tool_call_sse("c1", "logsearch__search_logs", '{"q": "fleet status"}'),
                _content_sse("fleet is fine"),
            ],
            bodies,
        ),
    )
    msgs = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "check the fleet"},
    ]
    r = tc.post(
        "/v1/chat/completions", json={"model": MODEL, "messages": msgs, "tools": [BASH_TOOL]}, headers=RELAY_HEADERS
    )
    assert r.status_code == 200, r.text
    rows = _wiring_rows(fake)
    assert len(rows) == 2, "one capture row per REAL model call"
    thread_id = canonical.thread_key(msgs, key.id)
    for row in rows:
        assert row[0] == key.id
        assert row[1] == thread_id
        assert row[5] == MODEL
    # the request id rides the pcai events, not the wire body — skip here
    iterations = [row[3] for row in rows]
    assert iterations == [0, 1], "iteration context distinguishes the two calls"
    # the attempt column exists and starts at 1
    assert all(row[4] == 1 for row in rows)


def test_wiring_capture_request_id_matches_stream(wiring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture row's request_id is the request id the client-visible SSE
    events carry — one conversation's rows join the stream they came from."""
    make, fake = wiring_env
    monkeypatch.setattr(effective, "db", fake)
    key = _key()
    tc = make(key)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    rows = _wiring_rows(fake)
    assert len(rows) == 1
    request_id = rows[0][2]
    assert f'"id": "{request_id}"' in r.text, "capture request_id == the stream's chunk ids"


def _capture_msgs(row: tuple) -> list[dict]:
    return json.loads(row[6])


def test_wiring_fidelity_preview_is_stripped_and_reconciled_wire_body(
    wiring_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE wave's fidelity proof, per call:

    - the inbound conversation carries reasoning_content/reasoning (a harness
      that re-appends thinking blocks). As of 2026-10-26 those keys are
      CARRIED, not stripped — but BOUNDED (reasoning_history_max_chars), which
      is what makes the 2026-09-18 storm shape impossible. The strip was the
      old fix; the cap is the real one;
    - a reconciled continuation turn's captured preview matches the EXACT
      messages list the mock upstream received on the wire (what the model
      saw == what was captured, byte-for-byte through the JSON round-trip)."""
    make, fake = wiring_env
    monkeypatch.setattr(effective, "db", fake)
    key = _key()
    bodies: list[dict] = []
    tc = make(
        key,
        handler=_scripted_handler(
            [
                _tool_call_sse("c1", "bash", '{"cmd": "ls"}'),
                _content_sse("all done"),
            ],
            bodies,
        ),
    )
    turn1 = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "check the fleet"},
    ]
    # request 1: inbound carries reasoning (the client re-appends it)
    dirty = [
        {**turn1[0], "reasoning_content": "preamble thinking"},
        {**turn1[1], "reasoning": "scratch"},
    ]
    r1 = tc.post(
        "/v1/chat/completions", json={"model": MODEL, "messages": dirty, "tools": [BASH_TOOL]}, headers=RELAY_HEADERS
    )
    assert r1.status_code == 200, r1.text
    # request 2: the handback continuation (assistant tool_calls + result appended)
    conv2 = [
        *turn1,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "results here"},
    ]
    r2 = tc.post(
        "/v1/chat/completions", json={"model": MODEL, "messages": conv2, "tools": [BASH_TOOL]}, headers=RELAY_HEADERS
    )
    assert r2.status_code == 200, r2.text

    assert len(bodies) == 2
    rows = _wiring_rows(fake)
    assert len(rows) == 2
    for row, wire_body in zip(rows, bodies, strict=True):
        captured = _capture_msgs(row)
        wire_msgs = wire_body["messages"]
        # Reasoning rides through BOUNDED (2026-10-26): present is fine, unbounded
        # is not. This is the storm guard — the old assertion pinned a full strip.
        cap = settings.reasoning_history_max_chars
        for entry in captured:
            preview = entry.get("preview", "")
            assert len(preview) < cap * 8, "a captured message must stay bounded"
        # captured previews == the wire messages, per-message (fidelity)
        captured_msgs = [json.loads(e["preview"]) for e in captured if e["role"] != "_meta"]
        assert captured_msgs == wire_msgs, "the captured preview must be exactly what the upstream received"
    # request 2 was a RECONCILED continuation: the store row + the wire agree
    wire2 = bodies[1]["messages"]
    assert wire2 == conv2, "the upstream sees canonical + tail (the client's replay)"


def test_wiring_capture_matches_wire_with_context_trim(wiring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the context budget on, the captured preview shows the TRIMMED
    projection (what the model actually got), not the client's full replay."""
    make, fake = wiring_env
    monkeypatch.setattr(effective, "db", fake)
    monkeypatch.setattr(settings, "conversation_context_budget_tokens", 30)
    key = _key()
    bodies: list[dict] = []
    tc = make(
        key,
        handler=_scripted_handler(
            [_tool_call_sse("c1", "bash", "{}")],
            bodies,
        ),
    )
    # 4 messages so the middle two are droppable: system+last are protected
    big = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "old turn " + "u" * 400},
        {"role": "assistant", "content": "old answer " + "a" * 400},
        {"role": "user", "content": "newest question"},
    ]
    r = tc.post(
        "/v1/chat/completions", json={"model": MODEL, "messages": big, "tools": [BASH_TOOL]}, headers=RELAY_HEADERS
    )
    assert r.status_code == 200, r.text
    rows = _wiring_rows(fake)
    assert len(rows) == 1
    captured_msgs = [json.loads(e["preview"]) for e in _capture_msgs(rows[0]) if e["role"] != "_meta"]
    assert captured_msgs == bodies[0]["messages"], "capture == the trimmed wire body"
    assert len(captured_msgs) < len(big), "the trim actually ran"


def test_wiring_tools_summary_captured_when_offered(wiring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    make, fake = wiring_env
    monkeypatch.setattr(effective, "db", fake)
    key = _key()
    tc = make(key)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "tools": [BASH_TOOL]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    rows = _wiring_rows(fake)
    assert len(rows) == 1
    tools_summary = json.loads(rows[0][7])
    # the relay merges the key's granted MCP tools into the offered set, so the
    # upstream tool array carries bash AND logsearch__search_logs — capture the
    # truth of what went upstream
    names = {t["name"] for t in tools_summary}
    assert "bash" in names and "logsearch__search_logs" in names
    bash = next(t for t in tools_summary if t["name"] == "bash")
    assert set(bash) == {"name", "description", "schema_bytes"}


def test_wiring_master_switch_off_zero_rows(wiring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    make, fake = wiring_env
    monkeypatch.setattr(effective, "db", fake)
    monkeypatch.setattr(settings, "effective_prompt_capture", False)
    key = _key()
    tc = make(key)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    assert _wiring_rows(fake) == []


def test_retry_attempts_captured_with_attempt_numbers(wiring_env, monkeypatch) -> None:
    """A bounded-call timeout with retries left captures BOTH attempts of the
    iteration — attempt 1 (the timed-out one) and attempt 2 — each with its
    real per-attempt settle. Driven at the relay_stream level (the wiring
    route's scripted-HTTP transport buffers, so the wedge lives where the
    other retry tests put it)."""
    import asyncio

    from pcai_llm.config import settings as _settings

    make, fake = wiring_env
    key = _key()
    make(key)  # the fixture patches its fakes inside make() — must be invoked
    monkeypatch.setattr(effective, "db", fake)
    import pcai_llm.core.traces as traces_mod

    monkeypatch.setattr(traces_mod, "db", fake)
    monkeypatch.setattr(_settings, "tool_relay_call_retries", 1)
    monkeypatch.setattr(toolrelay, "KEEPALIVE_S", 0.05)

    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1
        if calls["n"] == 1:

            async def gen1():
                await asyncio.sleep(0.01)
                raise TimeoutError("model call exceeded its budget")
                yield ("content", "")  # pragma: no cover

            return gen1()

        async def gen2():
            await asyncio.sleep(0.02)
            yield ("content", "recovered on retry")
            yield ("usage", {"prompt_tokens": 5, "completion_tokens": 7})

        return gen2()

    async def run():
        out = []
        async for chunk in toolrelay.relay_stream(
            [{"role": "user", "content": "hi"}],
            set(),
            [],
            stream_once,
            None,
            "req-retry",
            MODEL,
            key_id=key.id,
        ):
            out.append(chunk)
        return b"".join(out)

    text = asyncio.run(run()).decode()
    assert calls["n"] == 2 and "retrying" in text and "recovered on retry" in text
    rows = fake.capture_rows()
    assert [r[3] for r in rows] == [0, 0], "both attempts belong to iteration 0"
    assert [r[4] for r in rows] == [1, 2], "attempt numbers distinguish the retry"
    settled = [s for s in fake.settle_rows()]
    assert any(s[8] == "error" for s in settled) and any(s[8] == "ok" for s in settled), (
        "the timed-out attempt settles as error, the retry as ok"
    )


def test_wrapup_call_captured_as_next_iteration(wiring_env, monkeypatch) -> None:
    """The guard-triggered wrap-up pass is a REAL model call: it gets its own
    capture row at iteration+1 with an empty tools summary."""
    from pcai_llm.config import settings as _settings

    make, fake = wiring_env
    key = _key()
    make(key)  # the fixture patches its fakes inside make() — must be invoked
    monkeypatch.setattr(effective, "db", fake)
    import pcai_llm.core.traces as traces_mod

    monkeypatch.setattr(traces_mod, "db", fake)
    monkeypatch.setattr(_settings, "tool_relay_max_iterations", 1)

    seen: list = []

    def stream_once(msgs, tools):
        seen.append([(m.get("role"), str(m.get("content"))[:40]) for m in msgs])

        async def gen():
            # The wrap-up detection is content-agnostic now: the iterations
            # cap (2026-10) sends "Tool use paused … ASK THE USER …" while
            # the other guards keep "Tool use halted …". Either way the
            # wrap-up branch fires — match on the "Tool use" prefix.
            if not any(str(m.get("content", "")).startswith("Tool use") for m in msgs):
                yield (
                    "tool_calls",
                    [
                        {
                            "id": "c9",
                            "type": "function",
                            "function": {"name": "logsearch__search_logs", "arguments": "{}"},
                        }
                    ],
                )
                yield ("usage", {"prompt_tokens": 3, "completion_tokens": 2})
            else:
                yield ("content", "wrap-up answer")
                yield ("usage", {"prompt_tokens": 9, "completion_tokens": 1})

        return gen()

    async def fake_execute(server, tool, args):
        return False, "ok"

    async def run():
        out = []
        async for chunk in toolrelay.relay_stream(
            [{"role": "user", "content": "hi"}],
            {("logsearch", "search_logs")},
            [{"server": "logsearch", "tool": "search_logs", "description": "search"}],
            stream_once,
            fake_execute,
            "req-wrap",
            MODEL,
            key_id=key.id,
        ):
            out.append(chunk)
        return b"".join(out)

    text = asyncio.run(run()).decode()
    assert "wrap-up answer" in text, "the wrap-up model call's answer reached the stream"
    rows = fake.capture_rows()
    # max_iterations=1 flow: iter0 model call (tool_calls) -> exec -> iter1 model
    # call (the guard aborts it) -> wrap-up model call. THREE captured calls.
    assert len(rows) == 3, "the wrap-up call is captured like any model call"
    assert [r[3] for r in rows] == [0, 1, 1]
    assert json.loads(rows[-1][7] or "[]") == [], "the wrap-up pass is offered NO tools"
    # the wrap-up prompt carries the guard's instruction to the model —
    # role-agnostic: wave-C role hygiene moved the halt from role=user
    # (rendered as a user bubble in chat clients) to role=tool
    wrap_entries = [json.loads(e["preview"]) for e in json.loads(rows[-1][6])]
    assert any("Tool use paused" in (m.get("content") or "") for m in wrap_entries), (
        "the captured wrap-up prompt shows the iterations-cap handoff instruction "
        "(paused + ask the user — the 2026-10 operator request)"
    )


def test_wiring_capture_failure_request_still_succeeds(
    wiring_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """THE fail-safe: the capture write explodes -> the relay turn completes
    normally (upstream result returned to the client) and a warning is all
    that remains of the failure. ONLY the capture seam fails here (a wrapper
    around the working fake) — the settle/trace writes must stay healthy, or
    the observed failure would be the ledger's, not capture's."""

    class CaptureOnlyFail:
        def __init__(self, inner: WiringFakeDB) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def execute(self, sql: str, *args: Any):
            if "effective_prompts" in sql:
                raise RuntimeError("capture db down")
            return await self._inner.execute(sql, *args)

    make, fake = wiring_env
    capture_db = CaptureOnlyFail(fake)
    monkeypatch.setattr(effective, "db", capture_db)
    key = _key()
    tc = make(key)
    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.effective"):
        r = tc.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers=RELAY_HEADERS,
        )
    assert r.status_code == 200, r.text
    assert "done" in r.text, "the upstream result still reached the client"
    assert any("capture write failed" in rec.getMessage() for rec in caplog.records)
    assert fake.settled, "the model call still settled"


def test_wiring_usage_details_reach_settle_and_model_usage(wiring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scripted upstream reporting prompt_tokens_details.cached_tokens=4471
    produces (1) a settle call with cached_tokens=4471 (asserted on the fake
    db's usage_ledger INSERT args — column 6) and (2) a model_usage pcai
    event carrying cached_tokens=4471 plus the full usage dict verbatim."""
    make, fake = wiring_env
    monkeypatch.setattr(effective, "db", fake)
    key = _key()
    bodies: list[dict] = []
    usage = {
        "prompt_tokens": 9000,
        "completion_tokens": 100,
        "total_tokens": 9100,
        "prompt_tokens_details": {"cached_tokens": 4471},
        "completion_tokens_details": {"reasoning_tokens": 42},
    }
    tc = make(
        key,
        handler=_scripted_handler(
            [
                _tool_call_sse("c1", "logsearch__search_logs", "{}"),
                _content_sse("answer", usage=usage),
            ],
            bodies,
        ),
    )
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "tools": [BASH_TOOL]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    # (1) the settle row carries the cache hits (usage_ledger INSERT col 6)
    ok_rows = [s for s in fake.settle_rows() if s[8] == "ok"]
    assert ok_rows, "relay model calls settled"
    cached_vals = [s[6] for s in ok_rows]
    assert 4471 in cached_vals, f"cached_tokens=4471 must reach the ledger row; got {cached_vals}"
    # (2) the client-facing model_usage event carries the details
    assert '"cached_tokens": 4471' in r.text, r.text
    assert '"reasoning_tokens": 42' in r.text, "the verbatim usage dict rides the event"


def test_wiring_usage_absent_defaults_cached_zero(wiring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No usage detail -> settle cached_tokens stays 0 and the event carries
    cached_tokens: 0 with no usage key (no fabricated facts)."""
    make, fake = wiring_env
    monkeypatch.setattr(effective, "db", fake)
    key = _key()
    tc = make(key)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers=RELAY_HEADERS,
    )
    assert r.status_code == 200, r.text
    ok_rows = [s for s in fake.settle_rows() if s[8] == "ok"]
    assert ok_rows and all(s[6] == 0 for s in ok_rows)
    assert '"cached_tokens": 0' in r.text
    assert '"usage"' not in r.text or '"event": "model_usage"' in r.text


# ---------------------------------------------------------------------------
# 5. the admin read surface
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _EffectiveFakeDB]:
    fake = _EffectiveFakeDB()
    monkeypatch.setattr(effective, "db", fake)
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    app = FastAPI()
    app.include_router(routes_effective.router)
    return TestClient(app, raise_server_exceptions=False), fake


def _ah() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-123"}


def test_admin_effective_prompts_require_admin_header(admin_env) -> None:
    tc, _ = admin_env
    assert tc.get("/admin/effective-prompts").status_code == 401
    assert tc.get("/admin/effective-prompts", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_admin_effective_prompts_disabled_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "")
    app = FastAPI()
    app.include_router(routes_effective.router)
    tc = TestClient(app, raise_server_exceptions=False)
    assert tc.get("/admin/effective-prompts").status_code == 503


def test_admin_effective_prompts_200_and_shape(admin_env) -> None:
    tc, fake = admin_env
    kid = uuid.uuid4()
    fake.rows = [
        {
            "id": 2,
            "key_id": kid,
            "thread_id": "t1",
            "request_id": "req-b",
            "iteration": 1,
            "attempt": 1,
            "model": MODEL,
            "messages": '[{"role": "user", "preview": "x"}]',
            "tools": "",
            "created_at": "2026-09-19T00:00:00Z",
        },
        {
            "id": 1,
            "key_id": kid,
            "thread_id": "t1",
            "request_id": "req-a",
            "iteration": 0,
            "attempt": 1,
            "model": MODEL,
            "messages": "[]",
            "tools": "",
            "created_at": "2026-09-18T00:00:00Z",
        },
    ]
    r = tc.get("/admin/effective-prompts", headers=_ah(), params={"key_id": str(kid), "thread_id": "t1"})
    assert r.status_code == 200
    body = r.json()
    assert [row["id"] for row in body["rows"]] == [2, 1], "newest first"
    row = body["rows"][0]
    assert set(row) >= {
        "id",
        "request_id",
        "iteration",
        "attempt",
        "model",
        "thread_id",
        "created_at",
        "messages",
        "tools",
    }


def test_admin_effective_prompts_limit_clamped(admin_env) -> None:
    tc, fake = admin_env
    kid = uuid.uuid4()
    tc.get("/admin/effective-prompts", headers=_ah(), params={"key_id": str(kid), "limit": 500})
    _, args = fake.queries[-1]
    assert args[-1] == 200, "limit clamps at the 200 cap"
    tc.get("/admin/effective-prompts", headers=_ah(), params={"key_id": str(kid), "limit": 0})
    _, args = fake.queries[-1]
    assert args[-1] == 1, "limit floors at 1"
    tc.get("/admin/effective-prompts", headers=_ah(), params={"key_id": str(kid)})
    _, args = fake.queries[-1]
    assert args[-1] == 50, "default limit 50"


def test_admin_effective_prompts_missing_key_id_returns_empty(admin_env) -> None:
    """key_id is the primary scoping filter; without it the surface returns
    an empty page (never a fleet-wide dump)."""
    tc, _ = admin_env
    r = tc.get("/admin/effective-prompts", headers=_ah())
    assert r.status_code == 200
    assert r.json() == {"rows": []}


def test_admin_effective_prompts_passes_filters(admin_env) -> None:
    tc, fake = admin_env
    kid = uuid.uuid4()
    tc.get("/admin/effective-prompts", headers=_ah(), params={"key_id": str(kid), "thread_id": "abc", "limit": 7})
    sql, args = fake.queries[-1]
    assert args[0] == kid and args[1] == "abc" and args[2] == 7
    assert "thread_id = $2" in sql and "ORDER BY created_at DESC" in sql
