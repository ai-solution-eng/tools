"""Agent traces: best-effort writes, relay_stream step integration,
request-grouped reads, and the admin surface.

The unit suite never touches Postgres — `pcai_llm.core.traces.db` is
monkeypatched with a fake (repo fake-db style). Insert column order pinned
here: (request_id, key_id, user_id, model, iteration, phase, server, tool,
ms, prompt_tokens, completion_tokens, status, thread_id).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.core import toolrelay, traces
from pcai_llm.core.budgets import GateResult


def _recent_since() -> str:
    """Yesterday, ISO — always inside the admin window cap regardless of when this suite runs."""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=1)).isoformat().replace("+00:00", "Z")


CATALOG = [
    {"server": "logsearch", "tool": "search_logs", "description": "search"},
    {"server": "applygate", "tool": "plan_apply", "description": "plan"},
]

GRANTS = {("logsearch", "search_logs")}

# index of each column in record()'s INSERT (assertions read better)
COL = {
    "request_id": 0,
    "key_id": 1,
    "user_id": 2,
    "model": 3,
    "iteration": 4,
    "phase": 5,
    "server": 6,
    "tool": 7,
    "ms": 8,
    "prompt_tokens": 9,
    "completion_tokens": 10,
    "status": 11,
    "thread_id": 12,
}


class TraceFakeDB:
    """Fake db: captures agent_traces INSERTs, serves canned grouped rows,
    records read queries so tests can assert filters/bindings."""

    def __init__(self) -> None:
        self.inserts: list[tuple] = []
        self.grouped: list[dict] = []
        self.queries: list[tuple[str, tuple]] = []
        self.fail = False

    async def execute(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        if self.fail:
            raise RuntimeError("db down")
        if "agent_traces" in sql:
            self.inserts.append(args)
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return [dict(r) for r in self.grouped]

    async def fetchrow(self, sql: str, *args: Any):
        self.queries.append((sql, args))

    async def fetchval(self, sql: str, *args: Any):
        return None


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> TraceFakeDB:
    fake = TraceFakeDB()
    monkeypatch.setattr(traces, "db", fake)
    return fake


@pytest.fixture
def relay_fake(monkeypatch: pytest.MonkeyPatch) -> tuple[TraceFakeDB, list[tuple]]:
    """Fixture for driving relay_stream directly with a key: patches the
    traces db AND the settle the loop makes (keyed runs settle every model
    call — the settle contract itself is pinned by test_relay_settle.py;
    here settles are recorded so trace/settle parity is assertable).

    check_budgets is stubbed to OK: these tests own the TRACING contract;
    the mid-loop budget gate is fail-closed now (audit fix), so an unstubbed
    gate would end every keyed run at iteration 1 under this fake (its
    budget reads hit the unconnected module db). The fail-closed path has
    its own dedicated tests (test_relay_settle.py)."""
    fake = TraceFakeDB()
    monkeypatch.setattr(traces, "db", fake)
    settles: list[tuple] = []

    async def fake_settle(
        key_id: Any,
        model: str,
        endpoint: str,
        prompt: int,
        completion: int,
        status: str,
        latency_ms: int,
        request_id: str,
        cached_tokens: int = 0,
        billing_fraction: float = 1.0,
    ) -> None:
        settles.append((key_id, model, endpoint, prompt, completion, status, latency_ms, request_id, billing_fraction))

    monkeypatch.setattr(toolrelay, "settle", fake_settle)

    from pcai_llm.core.budgets import GateResult

    async def fake_check_budgets(key_id: Any, model: str, est_tokens: int = 0) -> GateResult:
        return GateResult(True)

    monkeypatch.setattr(toolrelay, "check_budgets", fake_check_budgets)
    return fake, settles


# ---------------------------------------------------------------------------
# record(): best-effort single INSERT
# ---------------------------------------------------------------------------


def test_record_writes_one_row(fake_db: TraceFakeDB) -> None:
    key_id, user_id = uuid.uuid4(), uuid.uuid4()
    asyncio.run(
        traces.record(
            "req-1",
            key_id,
            user_id,
            "m1",
            2,
            "tool",
            server="logsearch",
            tool="search_logs",
            ms=33,
            status="ok",
        )
    )
    assert len(fake_db.inserts) == 1
    row = fake_db.inserts[0]
    assert row[COL["request_id"]] == "req-1"
    assert row[COL["key_id"]] == key_id
    assert row[COL["user_id"]] == user_id
    assert row[COL["model"]] == "m1"
    assert row[COL["iteration"]] == 2
    assert row[COL["phase"]] == "tool"
    assert row[COL["server"]] == "logsearch" and row[COL["tool"]] == "search_logs"
    assert row[COL["ms"]] == 33
    assert row[COL["prompt_tokens"]] == 0 and row[COL["completion_tokens"]] == 0
    assert row[COL["status"]] == "ok"


def test_record_defaults_and_chat_shape(fake_db: TraceFakeDB) -> None:
    asyncio.run(traces.record("req-2", uuid.uuid4(), None, "m1", 0, "chat", ms=5, prompt_tokens=7, completion_tokens=3))
    row = fake_db.inserts[0]
    assert row[COL["server"]] == "" and row[COL["tool"]] == ""
    assert row[COL["status"]] == "ok"  # default
    assert row[COL["user_id"]] is None  # user attribution optional


def test_record_swallows_db_failure_and_logs(fake_db: TraceFakeDB, caplog: pytest.LogCaptureFixture) -> None:
    fake_db.fail = True
    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.traces"):
        asyncio.run(  # must NOT raise — traces never fail a request
            traces.record("req-3", uuid.uuid4(), None, "m1", 0, "chat")
        )
    assert fake_db.inserts == []
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_record_swallows_even_when_db_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real failure mode in keyless/dev runs: db pool never connected."""

    class BoomDB:
        async def execute(self, sql: str, *args: Any):
            raise RuntimeError("database not connected")

    monkeypatch.setattr(traces, "db", BoomDB())
    asyncio.run(traces.record("req-4", None, None, "m1", 0, "chat"))  # no raise


# ---------------------------------------------------------------------------
# relay_stream integration: one trace row per step
# ---------------------------------------------------------------------------


def _tool_call(name: str, args: str = "{}") -> dict:
    return {"index": 0, "id": "c1", "type": "function", "function": {"name": name, "arguments": args}}


def _scripted_stream(chunks_by_call: list[list[dict]]):
    """stream_once factory: one scripted SSE model stream per call."""
    calls = {"n": 0}

    def stream_once(msgs: list[dict], tools: list[dict] | None):
        chunks = chunks_by_call[min(calls["n"], len(chunks_by_call) - 1)]
        calls["n"] += 1

        class R:
            async def aiter_bytes(self):
                for c in chunks:
                    yield b"data: " + json.dumps(c).encode() + b"\n\n"

        async def gen():
            async for ev in toolrelay.stream_model_call(R()):
                yield ev

        return gen()

    return stream_once


async def _ok_exec(server: str, tool: str, args: dict) -> tuple[bool, Any]:
    """Executor stand-in: one successful MCP tool call."""
    return True, {"hits": 3}


def _run_relay(stream_once, execute, **kwargs) -> str:
    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                kwargs.pop("messages", [{"role": "user", "content": "go"}]),
                kwargs.pop("grants", GRANTS),
                CATALOG,
                stream_once,
                execute,
                kwargs.pop("request_id", "req-relay"),
                kwargs.pop("model", "m1"),
                **kwargs,
            )
        ]

    return b"".join(asyncio.run(run())).decode()


def test_relay_stream_records_chat_and_tool_rows(relay_fake) -> None:
    """One chat row per model iteration (observed usage + wall ms) and one
    tool row per executed tool call — additive: the SSE events are intact."""
    fake, settles = relay_fake
    key_id, user_id = uuid.uuid4(), uuid.uuid4()
    text = _run_relay(
        _scripted_stream(
            [
                [
                    {"choices": [{"delta": {"content": "let me look"}}]},
                    {"choices": [{"delta": {"tool_calls": [_tool_call("logsearch__search_logs")]}}]},
                    {"usage": {"prompt_tokens": 7, "completion_tokens": 3}},
                ],
                [
                    {"choices": [{"delta": {"content": "here you go"}}]},
                    {"usage": {"prompt_tokens": 20, "completion_tokens": 4}},
                ],
            ]
        ),
        _ok_exec,
        key_id=key_id,
        user_id=user_id,
    )
    chat = [r for r in fake.inserts if r[COL["phase"]] == "chat"]
    tool = [r for r in fake.inserts if r[COL["phase"]] == "tool"]
    assert len(chat) == 2, "one chat trace per model iteration"
    assert [r[COL["iteration"]] for r in chat] == [0, 1]
    assert all(r[COL["status"]] == "ok" for r in chat)
    assert all(r[COL["key_id"]] == key_id and r[COL["user_id"]] == user_id for r in chat)
    assert [r[COL["prompt_tokens"]] for r in chat] == [7, 20]  # observed usage
    assert [r[COL["completion_tokens"]] for r in chat] == [3, 4]
    assert [r[COL["status"]] for r in chat] == [s[5] for s in settles] == ["ok", "ok"], (
        "trace status matches the settle status for every model call"
    )
    assert all(r[COL["ms"]] is not None for r in chat), "wall ms per model stream"
    assert len(tool) == 1
    trow = tool[0]
    assert trow[COL["iteration"]] == 0
    assert trow[COL["server"]] == "logsearch" and trow[COL["tool"]] == "search_logs"
    assert trow[COL["status"]] == "ok" and trow[COL["ms"]] is not None
    assert trow[COL["prompt_tokens"]] == 0  # tools consume no model tokens
    # existing behavior unchanged: tool event + final delta + [DONE] still flow
    assert '"event": "tool_call"' in text and '"event": "model_usage"' in text
    assert "here you go" in text and text.rstrip().endswith("data: [DONE]")


def test_relay_stream_records_estimate_fallback(relay_fake) -> None:
    """No upstream usage -> the trace carries the same estimate the settle
    uses (an estimate beats no row)."""
    fake, settles = relay_fake
    _run_relay(
        _scripted_stream([[{"choices": [{"delta": {"content": "done"}}]}]]),
        _ok_exec,
        messages=[{"role": "user", "content": "x" * 40}],  # 40//4 = 10 prompt tokens
        key_id=uuid.uuid4(),
        max_tokens=7,
    )
    chat = [r for r in fake.inserts if r[COL["phase"]] == "chat"]
    assert chat and chat[0][COL["prompt_tokens"]] == 17 and chat[0][COL["completion_tokens"]] == 0
    assert settles and settles[0][3] == 17, "trace mirrors the settle's estimate"


def test_relay_stream_records_denied_tool(relay_fake) -> None:
    """A tool NOT granted for the key traces status='denied' (its own ACL
    emission site), never executes, and the loop still finishes."""
    fake, _settles = relay_fake
    _run_relay(
        _scripted_stream(
            [
                [{"choices": [{"delta": {"tool_calls": [_tool_call("applygate__plan_apply")]}}]}],
                [{"choices": [{"delta": {"content": "ok, skipping"}}]}],
            ]
        ),
        _ok_exec,
        key_id=uuid.uuid4(),
    )
    tool = [r for r in fake.inserts if r[COL["phase"]] == "tool"]
    assert len(tool) == 1
    assert tool[0][COL["server"]] == "applygate" and tool[0][COL["tool"]] == "plan_apply"
    assert tool[0][COL["status"]] == "denied"


def test_relay_stream_records_error_for_unknown_tool_name(relay_fake) -> None:
    """A name without the `server__tool` separator is a trace 'error' step."""
    fake, _settles = relay_fake
    _run_relay(
        _scripted_stream(
            [
                [{"choices": [{"delta": {"tool_calls": [_tool_call("not_an_mcp_tool")]}}]}],
                [{"choices": [{"delta": {"content": "wrapping up"}}]}],
            ]
        ),
        _ok_exec,
        key_id=uuid.uuid4(),
    )
    tool = [r for r in fake.inserts if r[COL["phase"]] == "tool"]
    assert len(tool) == 1
    assert tool[0][COL["server"]] == "" and tool[0][COL["tool"]] == "not_an_mcp_tool"
    assert tool[0][COL["status"]] == "error"


def test_relay_stream_maps_executor_refusals(relay_fake) -> None:
    """The executor collapses refusals into (False, detail): the owned
    detail prefixes map to rate_limited / denied; anything else is error."""
    fake, _settles = relay_fake

    async def rate_limited(server: str, tool: str, args: dict) -> tuple[bool, Any]:
        return False, "rate limited: RPM limit 1 exceeded (2 this minute)"

    _run_relay(
        _scripted_stream(
            [
                [{"choices": [{"delta": {"tool_calls": [_tool_call("logsearch__search_logs")]}}]}],
                [{"choices": [{"delta": {"content": "gave up"}}]}],
            ]
        ),
        rate_limited,
        key_id=uuid.uuid4(),
    )
    tool = [r for r in fake.inserts if r[COL["phase"]] == "tool"]
    assert tool and tool[0][COL["status"]] == "rate_limited"

    fake.inserts.clear()

    async def failed(server: str, tool: str, args: dict) -> tuple[bool, Any]:
        return False, "upstream failed: ConnectError"

    _run_relay(
        _scripted_stream(
            [
                [{"choices": [{"delta": {"tool_calls": [_tool_call("logsearch__search_logs")]}}]}],
                [{"choices": [{"delta": {"content": "gave up"}}]}],
            ]
        ),
        failed,
        key_id=uuid.uuid4(),
    )
    tool = [r for r in fake.inserts if r[COL["phase"]] == "tool"]
    assert tool and tool[0][COL["status"]] == "error"


def test_relay_stream_records_budget_exceeded_marker(relay_fake, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mid-loop budget exhaustion: the marker settle gets a matching
    budget_exceeded chat trace, and the loop stops cleanly."""
    fake, settles = relay_fake

    async def exhausted(key_id: Any, model: str, est: int) -> GateResult:
        return GateResult(False, "month budget 5 exceeded (spent 8)")

    monkeypatch.setattr(toolrelay, "check_budgets", exhausted)
    text = _run_relay(
        _scripted_stream(
            [
                [
                    {"choices": [{"delta": {"tool_calls": [_tool_call("logsearch__search_logs")]}}]},
                    {"usage": {"prompt_tokens": 8, "completion_tokens": 0}},
                ],
                [{"choices": [{"delta": {"content": "should not get here"}}]}],
            ]
        ),
        _ok_exec,
        key_id=uuid.uuid4(),
    )
    chat = [r for r in fake.inserts if r[COL["phase"]] == "chat"]
    assert [r[COL["status"]] for r in chat] == ["ok", "budget_exceeded"]
    assert settles[-1][5] == "budget_exceeded", "trace mirrors the marker settle"
    marker = chat[-1]
    assert marker[COL["prompt_tokens"]] == 8  # what was spent so far
    assert '"guard"' in text and "budget exceeded" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_stream_without_key_records_nothing(fake_db: TraceFakeDB) -> None:
    """key_id=None (the unit-test path) writes no traces — the schema's
    key_id is NOT NULL; same gate the settle contract uses."""
    text = _run_relay(
        _scripted_stream(
            [
                [{"choices": [{"delta": {"tool_calls": [_tool_call("logsearch__search_logs")]}}]}],
                [{"choices": [{"delta": {"content": "done"}}]}],
            ]
        ),
        _ok_exec,
    )
    assert fake_db.inserts == []
    assert '"tool_call"' in text and text.rstrip().endswith("data: [DONE]")


def test_relay_stream_survives_trace_db_failure(relay_fake, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE best-effort contract: a broken trace pipeline must not disturb
    the stream — events, settle, and [DONE] all still happen."""
    fake, _settles = relay_fake
    fake.fail = True
    text = _run_relay(
        _scripted_stream(
            [
                [
                    {"choices": [{"delta": {"tool_calls": [_tool_call("logsearch__search_logs")]}}]},
                    {"usage": {"prompt_tokens": 2, "completion_tokens": 1}},
                ],
                [{"choices": [{"delta": {"content": "done"}}]}],
            ]
        ),
        _ok_exec,
        key_id=uuid.uuid4(),
    )
    assert '"event": "tool_call"' in text and "done" in text
    assert text.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# recent() / get(): request-grouped reads
# ---------------------------------------------------------------------------


def _step(phase: str, ms: int, **over: Any) -> dict:
    base = {
        "iteration": 0,
        "phase": phase,
        "server": "" if phase == "chat" else "logsearch",
        "tool": "" if phase == "chat" else "search_logs",
        "ms": ms,
        "prompt_tokens": 5 if phase == "chat" else 0,
        "completion_tokens": 2 if phase == "chat" else 0,
        "status": "ok",
    }
    base.update(over)
    return base


def _grouped_row(request_id: str = "req-a", steps: list[dict] | None = None, **over: Any) -> dict:
    row: dict[str, Any] = {
        "request_id": request_id,
        "key_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "user_name": "alice",
        "key_name": "k1",
        "model": "m1",
        "started_ts": datetime(2025, 1, 1, tzinfo=UTC),
        "last_ts": datetime(2025, 1, 1, 0, 1, tzinfo=UTC),
        "steps": steps
        if steps is not None
        else [
            _step("chat", 100, iteration=0),
            _step("tool", 30),
            _step("chat", 70, iteration=1),
        ],
    }
    row.update(over)
    return row


def test_recent_groups_requests_with_totals(fake_db: TraceFakeDB) -> None:
    fake_db.grouped = [_grouped_row("req-a"), _grouped_row("req-b")]
    out = asyncio.run(traces.recent())
    assert [t["request_id"] for t in out] == ["req-a", "req-b"]
    a = out[0]
    assert a["key_name"] == "k1" and a["user_name"] == "alice"
    assert a["model"] == "m1"
    assert a["started_ts"] < a["last_ts"]
    assert [s["phase"] for s in a["steps"]] == ["chat", "tool", "chat"]
    assert a["totals"] == {"steps": 3, "tool_ms": 30, "chat_ms": 170, "tool_calls": 1, "handed_back": 0}
    assert a["steps"][1] == {
        "iteration": 0,
        "phase": "tool",
        "server": "logsearch",
        "tool": "search_logs",
        "ms": 30,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "status": "ok",
    }


def test_recent_filters_and_limit_binding(fake_db: TraceFakeDB) -> None:
    uid, kid = uuid.uuid4(), uuid.uuid4()
    since = datetime(2025, 3, 1, tzinfo=UTC)
    asyncio.run(traces.recent(user_id=uid, key_id=kid, since=since, limit=7))
    sql, args = fake_db.queries[-1]
    assert uid in args and kid in args and since in args
    assert args[-1] == 7, "limit binds last"
    assert "t.user_id = $" in sql and "t.key_id = $" in sql and "t.ts >= $" in sql
    assert "GROUP BY t.request_id" in sql and "json_agg" in sql
    assert "LEFT JOIN api_keys" in sql and "LEFT JOIN users" in sql


def test_recent_parses_json_string_steps(fake_db: TraceFakeDB) -> None:
    """Real asyncpg returns json_agg as TEXT — the read path must decode it."""
    row = _grouped_row("req-json")
    row["steps"] = json.dumps(row["steps"])
    fake_db.grouped = [row]
    out = asyncio.run(traces.recent())
    assert out[0]["totals"] == {"steps": 3, "tool_ms": 30, "chat_ms": 170, "tool_calls": 1, "handed_back": 0}
    assert len(out[0]["steps"]) == 3


def test_recent_no_filters_runs_unconditional_group_by(fake_db: TraceFakeDB) -> None:
    fake_db.grouped = []
    out = asyncio.run(traces.recent(limit=25))
    assert out == []
    sql, args = fake_db.queries[-1]
    assert "WHERE" not in sql
    assert args == (25,)


def test_get_returns_trace_or_none(fake_db: TraceFakeDB) -> None:
    fake_db.grouped = [_grouped_row("req-a")]
    got = asyncio.run(traces.get("req-a"))
    assert got is not None and got["request_id"] == "req-a"
    assert got["totals"]["steps"] == 3
    sql, args = fake_db.queries[-1]
    assert "t.request_id = $1" in sql and args[0] == "req-a"
    fake_db.grouped = []
    assert asyncio.run(traces.get("missing")) is None


# ---------------------------------------------------------------------------
# admin routes
# ---------------------------------------------------------------------------


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-123"}


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch, fake_db: TraceFakeDB) -> TestClient:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def test_admin_traces_require_admin_header(admin_client: TestClient) -> None:
    assert admin_client.get("/admin/traces").status_code == 401
    assert admin_client.get("/admin/traces", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert admin_client.get("/admin/traces/req-1").status_code == 401


def test_admin_traces_list_200(admin_client: TestClient, fake_db: TraceFakeDB) -> None:
    fake_db.grouped = [_grouped_row("req-a")]
    r = admin_client.get("/admin/traces", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and body[0]["request_id"] == "req-a"
    assert body[0]["totals"] == {"steps": 3, "tool_ms": 30, "chat_ms": 170, "tool_calls": 1, "handed_back": 0}
    assert body[0]["key_name"] == "k1"


def test_admin_traces_filters_pass_through(admin_client: TestClient, fake_db: TraceFakeDB) -> None:
    uid, kid = str(uuid.uuid4()), str(uuid.uuid4())
    r = admin_client.get(
        "/admin/traces",
        headers=_h(),
        params={"user_id": uid, "key_id": kid, "since": _recent_since(), "limit": 10},
    )
    assert r.status_code == 200
    _, args = fake_db.queries[-1]
    assert len(args) == 4  # user_id, key_id, since, limit


def test_admin_traces_bad_since_400(admin_client: TestClient) -> None:
    r = admin_client.get("/admin/traces", headers=_h(), params={"since": "not-a-date"})
    assert r.status_code == 400
    assert "ISO-8601" in r.json()["detail"]


def test_admin_traces_limit_capped_at_200(admin_client: TestClient, fake_db: TraceFakeDB) -> None:
    r = admin_client.get("/admin/traces", headers=_h(), params={"limit": 5000})
    assert r.status_code == 200
    _, args = fake_db.queries[-1]
    assert args[-1] == 200
    fake_db.queries.clear()
    admin_client.get("/admin/traces", headers=_h())  # default 50
    _, args = fake_db.queries[-1]
    assert args[-1] == 50


def test_admin_trace_detail_200_and_404(admin_client: TestClient, fake_db: TraceFakeDB) -> None:
    r = admin_client.get("/admin/traces/req-a", headers=_h())
    assert r.status_code == 404, "nothing traced yet"
    fake_db.grouped = [_grouped_row("req-a")]
    r = admin_client.get("/admin/traces/req-a", headers=_h())
    assert r.status_code == 200
    assert r.json()["request_id"] == "req-a"
    assert r.json()["steps"][0]["phase"] == "chat"


def test_admin_help_lists_traces(admin_client: TestClient) -> None:
    r = admin_client.get("/admin/help", headers=_h())
    assert r.status_code == 200
    endpoints = r.json()["endpoints"]
    assert "GET    /admin/traces" in endpoints
    assert "GET    /admin/traces/{request_id}" in endpoints


# ---------------------------------------------------------------------------
# handback tracing (client-side harness loops — the DSH case)
# ---------------------------------------------------------------------------


def test_pure_client_handback_traced(relay_fake) -> None:
    """A harness running its OWN tool loop (DSH et al.): the model calls the
    client's tool, the relay hands back, the loop ends client-side. The
    handback MUST trace as phase='tool' server='client' status='handed_back'
    — without it every such request shows as '1 step, 0 tools' and the
    trace waterfall hides what the agent actually asked to do."""
    fake, _ = relay_fake
    key_id, user_id = uuid.uuid4(), uuid.uuid4()
    out = _run_relay(
        _scripted_stream(
            [
                [
                    {"choices": [{"delta": {"tool_calls": [_tool_call("bash", '{"cmd":"ls"}')]}}]},
                    {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                ]
            ]
        ),
        _ok_exec,
        key_id=key_id,
        user_id=user_id,
        client_tools=[{"type": "function", "function": {"name": "bash", "description": ""}}],
    )
    assert "handback" in out
    hb = [r for r in fake.inserts if r[COL["phase"]] == "tool" and r[COL["status"]] == "handed_back"]
    assert len(hb) == 1, f"expected exactly one handback row, got {fake.inserts}"
    assert hb[0][COL["server"]] == "client"
    assert hb[0][COL["tool"]] == "bash"
    assert hb[0][COL["ms"]] is None, "the client executes it later — no server wall-clock"
    assert hb[0][COL["key_id"]] == key_id and hb[0][COL["user_id"]] == user_id
    executed = [r for r in fake.inserts if r[COL["phase"]] == "tool" and r[COL["status"]] in ("ok", "error")]
    assert not executed, "nothing was executed in-cluster"


def test_mixed_batch_traces_exec_and_handback(relay_fake) -> None:
    """Mixed turn (one gateway tool + one client tool): the gateway tool
    executes and traces as before; the client tool hands back and traces
    under its CLIENT-FACING name."""
    fake, _ = relay_fake
    key_id, user_id = uuid.uuid4(), uuid.uuid4()
    out = _run_relay(
        _scripted_stream(
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
                                            "function": {"name": "logsearch__search_logs", "arguments": '{"q":"x"}'},
                                        },
                                        {
                                            "index": 1,
                                            "id": "c2",
                                            "type": "function",
                                            "function": {"name": "bash", "arguments": '{"cmd":"ls"}'},
                                        },
                                    ]
                                }
                            }
                        ]
                    },
                    {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                ]
            ]
        ),
        _ok_exec,
        key_id=key_id,
        user_id=user_id,
        client_tools=[{"type": "function", "function": {"name": "bash", "description": ""}}],
    )
    assert "handback" in out
    tool = [r for r in fake.inserts if r[COL["phase"]] == "tool"]
    executed = [r for r in tool if r[COL["server"]] == "logsearch"]
    hb = [r for r in tool if r[COL["status"]] == "handed_back"]
    assert len(executed) == 1 and executed[0][COL["tool"]] == "search_logs"
    assert len(hb) == 1 and hb[0][COL["server"]] == "client" and hb[0][COL["tool"]] == "bash"


def test_client_loop_guard_denial_traced(relay_fake) -> None:
    """A client call refused by the cross-request loop guard is fed back
    in-loop — it traces as server='client' status='denied', NOT as a
    handback (it was never handed anywhere)."""
    fake, _ = relay_fake
    key_id, user_id = uuid.uuid4(), uuid.uuid4()

    def guard(name: str, args: dict) -> str | None:
        return "loop guard: identical call made too many times"

    _run_relay(
        _scripted_stream(
            [
                [
                    {"choices": [{"delta": {"tool_calls": [_tool_call("bash")]}}]},
                    {"usage": {"prompt_tokens": 5, "completion_tokens": 1}},
                ],
                [
                    {"choices": [{"delta": {"content": "answering from what I have"}}]},
                    {"usage": {"prompt_tokens": 6, "completion_tokens": 3}},
                ],
            ]
        ),
        _ok_exec,
        key_id=key_id,
        user_id=user_id,
        client_tools=[{"type": "function", "function": {"name": "bash", "description": ""}}],
        client_loop_guard=guard,
    )
    denied = [r for r in fake.inserts if r[COL["server"]] == "client" and r[COL["status"]] == "denied"]
    assert len(denied) == 1 and denied[0][COL["tool"]] == "bash"
    assert not any(r[COL["status"]] == "handed_back" for r in fake.inserts), "a guarded call is never handed back"


def test_totals_surface_handed_back(fake_db) -> None:
    """_shape() adds a handed_back count so the console can say
    '2 steps · 1 handed to client' instead of looking toolless."""
    key_id, user_id = uuid.uuid4(), uuid.uuid4()
    fake_db.grouped = [
        {
            "request_id": "r1",
            "key_id": key_id,
            "user_id": user_id,
            "user_name": "u",
            "key_name": "k",
            "model": "m1",
            "started_ts": None,
            "last_ts": None,
            "steps": [
                {
                    "iteration": 0,
                    "phase": "chat",
                    "server": "",
                    "tool": "",
                    "ms": 100,
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "status": "ok",
                },
                {
                    "iteration": 0,
                    "phase": "tool",
                    "server": "client",
                    "tool": "bash",
                    "ms": None,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "status": "handed_back",
                },
                {
                    "iteration": 0,
                    "phase": "tool",
                    "server": "logsearch",
                    "tool": "search_logs",
                    "ms": 33,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "status": "ok",
                },
            ],
        }
    ]
    rows = asyncio.run(traces.recent())
    t = rows[0]["totals"]
    assert t["steps"] == 3
    assert t["handed_back"] == 1
    assert t["tool_calls"] == 2
    assert t["tool_ms"] == 33, "handback rows carry ms=None — they never inflate tool wall-clock"


# ---------------------------------------------------------------------------
# conversation threads (trace grouping across a multi-turn harness session)
# ---------------------------------------------------------------------------


def test_record_carries_thread_id(fake_db: TraceFakeDB) -> None:
    asyncio.run(traces.record("req-1", uuid.uuid4(), None, "m", 0, "chat", thread_id="t-abc"))
    assert fake_db.inserts[0][COL["thread_id"]] == "t-abc"


def test_grouped_read_surfaces_thread_id(fake_db: TraceFakeDB) -> None:
    fake_db.grouped = [
        {
            "request_id": "req-1",
            "key_id": uuid.uuid4(),
            "user_id": None,
            "model": "m",
            "started_ts": "2026-01-01T00:00:00Z",
            "last_ts": "2026-01-01T00:00:01Z",
            "thread_id": "t-abc",
            "key_name": "k",
            "user_name": "u",
            "steps": [
                {
                    "iteration": 0,
                    "phase": "chat",
                    "server": "",
                    "tool": "",
                    "ms": 5,
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "status": "ok",
                }
            ],
        }
    ]
    rows = asyncio.run(traces.recent())
    assert rows[0]["thread_id"] == "t-abc"


def test_thread_key_stable_across_growing_history() -> None:
    """The harness re-sends the accumulated history every step: the FIRST
    user message is the seed — a growing tail must NOT change the thread;
    a different key or seed must."""
    msgs1 = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "story time"},
        {"role": "assistant", "content": "ok"},
    ]
    msgs2 = [msgs1[0], *msgs1[1:], {"role": "user", "content": "more"}]  # same seed, longer tail
    assert toolrelay._thread_key(msgs1, "k") == toolrelay._thread_key(msgs2, "k")
    assert toolrelay._thread_key(msgs1, "k") != toolrelay._thread_key(msgs1, "other-key")
    assert toolrelay._thread_key([{"role": "user", "content": "different"}], "k") != (toolrelay._thread_key(msgs1, "k"))
    # system-preamble variation does NOT change the thread (seed = first USER msg)
    m_a = [{"role": "system", "content": "A"}, {"role": "user", "content": "s"}]
    m_b = [{"role": "system", "content": "B"}, {"role": "user", "content": "s"}]
    assert toolrelay._thread_key(m_a, "k") == toolrelay._thread_key(m_b, "k")
    # multimodal (list-shaped) content canonicalizes instead of crashing
    m_mm = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert toolrelay._thread_key(m_mm, "k") == toolrelay._thread_key(m_mm, "k")
    # no user message at all → None (defensive)
    assert toolrelay._thread_key([{"role": "system", "content": "s"}], "k") is None


def test_relay_stream_steps_share_one_thread(relay_fake) -> None:
    """Every trace row of ONE relayed request carries the same thread id —
    and a LATER request replaying the same conversation (grown history,
    new request_id) lands in the SAME thread: the DSH conversation reads as
    one grouped event, not N disconnected cards."""
    fake, _ = relay_fake
    key_id = uuid.uuid4()
    _run_relay(
        _scripted_stream(
            [
                [
                    {"choices": [{"delta": {"content": "let me check"}}]},
                    {"choices": [{"delta": {"tool_calls": [_tool_call("logsearch__search_logs")]}}]},
                    {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
                ],
                [
                    {"choices": [{"delta": {"content": "part 2"}}]},
                    {"usage": {"prompt_tokens": 6, "completion_tokens": 3}},
                ],
            ]
        ),
        _ok_exec,
        request_id="req-thread-1",
        key_id=key_id,
    )
    chat = [r for r in fake.inserts if r[COL["phase"]] == "chat"]
    assert len(chat) == 2
    tids = {r[COL["thread_id"]] for r in chat}
    assert len(tids) == 1 and None not in tids, "all steps of one request share the thread"

    # the conversation continues: grown history, NEW request_id — same seed
    _run_relay(
        _scripted_stream([[{"choices": [{"delta": {"content": "part 3"}}]}]]),
        _ok_exec,
        request_id="req-thread-2",
        key_id=key_id,
        messages=[
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "part 1"},
            {"role": "user", "content": "go on"},
        ],
    )
    later = [r for r in fake.inserts if r[COL["request_id"]] == "req-thread-2"]
    assert later, "second request traced"
    assert all(r[COL["thread_id"]] == next(iter(tids)) for r in later), (
        "the follow-up request joins the SAME conversation thread"
    )
