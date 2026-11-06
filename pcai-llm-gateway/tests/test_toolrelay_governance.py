"""Tool-relay GOVERNANCE: per-profile budget UNITS + per-server fractional WEIGHTS.

The per-turn cap used to be a COUNT of model rounds. It is now a BUDGET of
fractional units (db/schema.py carries the WHY), and every MCP server carries a
weight that prices its calls. This module locks the arithmetic and the seams:

  * RelayGuard charges sum(weight(server_of(call))) per batch, aborts when the
    spend EXCEEDS the budget, and keeps the wall-clock / identical-call checks
    untouched;
  * server attribution goes through the existing name algebra, so a
    harness-prefixed spelling is priced as the server it reaches;
  * the budget-exhaustion message keeps the ask-the-user handoff, and the other
    aborts keep "answer from what you have";
  * a NULL profile budget inherits settings.tool_relay_max_iterations;
  * core.mcp_weights degrades to {} (never raises) and invalidates on writes;
  * the admin API round-trips tool_relay_budget and tool_weight.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.core import mcp_weights, registry, toolrelay
from pcai_llm.core.keys import key_record_from_row
from pcai_llm.core.toolrelay import RelayGuard

CATALOG = [
    {"server": "logsearch", "tool": "search_logs", "description": "search"},
    {"server": "cheap", "tool": "ping", "description": "cheap"},
]

GRANTS = {("logsearch", "search_logs"), ("cheap", "ping")}


def _tc(name: str, args: dict | None = None, call_id: str = "c1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args or {})}}


# ---------------------------------------------------------------------------
# RelayGuard: the unit arithmetic
# ---------------------------------------------------------------------------


def test_fractional_weight_charges_a_tenth() -> None:
    """0.1-weight server: ten calls = 1.0 unit, the eleventh aborts.

    This is the whole point of a REAL weight — the same budget buys 10x the
    calls on a cheap server, which a round COUNT could never express."""
    g = RelayGuard(1.0, 60, weights={"cheap": 0.1})
    for i in range(10):
        assert g.check(i, [_tc("cheap__ping", {"i": i}, f"c{i}")]) == "", f"call {i} must be affordable"
    assert g.spent == pytest.approx(1.0), "ten 0.1-weight calls charge exactly one unit"
    assert g.check(10, [_tc("cheap__ping", {"i": 10}, "c10")]) == "tool budget exhausted (1.10/1.00 units)"


def test_unknown_server_charges_the_neutral_weight() -> None:
    """A server with no row (or a name the weights map never heard of) costs
    the neutral 1.0 — an unweighted fleet keeps the old bound exactly."""
    g = RelayGuard(1.0, 60, weights={"cheap": 0.1})
    assert g.check(0, [_tc("ghost__x", {"i": 0})]) == ""
    assert g.spent == pytest.approx(1.0)
    assert g.check(1, [_tc("ghost__y", {"i": 1})]) == "tool budget exhausted (2.00/1.00 units)"


def test_prefixed_name_resolves_to_the_same_server() -> None:
    """A harness-prefixed call (mcp__pcai-llm-gateway__cheap__ping) must be
    priced as "cheap", not as its first name segment "mcp" — otherwise every
    prefixed call silently charges the neutral weight."""
    g = RelayGuard(1.0, 60, weights={"cheap": 0.1}, catalog=CATALOG)
    assert g.check(0, [_tc("mcp__pcai-llm-gateway__cheap__ping", {"i": 0})]) == ""
    assert g.spent == pytest.approx(0.1), "the prefixed spelling resolved to the cheap server"

    bare = RelayGuard(1.0, 60, weights={"cheap": 0.1}, catalog=CATALOG)
    assert bare.check(0, [_tc("cheap__ping", {"i": 0})]) == ""
    assert bare.spent == pytest.approx(g.spent), "both spellings charge the same server"

    # Without a catalog the fallback split names "mcp" — still bounded, just
    # unpriced. Documents WHY relay_stream passes the granted catalog.
    uncatalogued = RelayGuard(1.0, 60, weights={"cheap": 0.1})
    assert uncatalogued.check(0, [_tc("mcp__pcai-llm-gateway__cheap__ping", {"i": 0})]) == ""
    assert uncatalogued.spent == pytest.approx(1.0)


def test_budget_charges_every_in_cluster_call() -> None:
    """A batch charges its whole in-cluster set; a CLIENT handback call is not
    the gateway's work and must not be charged (the relay passes its in-cluster
    partition), or a mixed batch would abort instead of handing back."""
    g = RelayGuard(2.0, 60, weights={"logsearch": 1.0, "cheap": 0.5})
    batch = [_tc("logsearch__search_logs", {"q": "a"}, "c1"), _tc("cheap__ping", {}, "c2")]
    assert g.check(0, batch) == ""
    assert g.spent == pytest.approx(1.5)
    g2 = RelayGuard(2.0, 60, weights={"logsearch": 1.0, "cheap": 0.5})
    assert g2.check(0, batch, charge_calls=batch[:1]) == ""
    assert g2.spent == pytest.approx(1.0)


def test_wall_clock_and_loop_guards_unchanged() -> None:
    """The budget is the ONLY new bound: wall clock and identical-call
    detection keep their exact messages and precedence."""
    import time

    wall = RelayGuard(10.0, 0.0)
    time.sleep(0.01)
    assert "wall-clock" in wall.check(0, [])

    loop = RelayGuard(10.0, 60)
    call = _tc("logsearch__search_logs", {"q": "same"})
    assert loop.check(0, [call]) == ""
    assert "loop detected" in loop.check(1, [call])


# ---------------------------------------------------------------------------
# relay_stream: budget handoff, inheritance, and the profile override
# ---------------------------------------------------------------------------


def _relay(model_effects: list[dict], **kwargs: Any) -> tuple[str, int, list[list[dict]]]:
    """Drive the PRODUCTION loop with scripted model responses.

    Returns (sse_text, executed_call_count, wrap_up_tool_messages). Same
    harness shape as tests/test_toolrelay.py._stream_relay, plus a capture of
    the toolless wrap-up pass's role=tool inputs (where the halt instruction
    rides)."""
    idx = {"n": 0}
    executed = {"n": 0}
    wrap_inputs: list[list[dict]] = []

    def stream_once(msgs, tools):
        if tools is None:
            wrap_inputs.append([m for m in msgs if m.get("role") == "tool"])
        i = idx["n"]
        idx["n"] += 1
        r = model_effects[min(i, len(model_effects) - 1)]

        async def raw_chunks():
            chunks = []
            if r.get("content"):
                chunks.append({"choices": [{"delta": {"content": r["content"]}}]})
            for tc in r.get("tool_calls") or []:
                chunks.append({"choices": [{"delta": {"tool_calls": [{"index": 0, **tc}]}}]})
            if "usage" in r:
                chunks.append({"usage": r["usage"]})
            for c in chunks:
                yield b"data: " + json.dumps(c).encode() + b"\n\n"

        class R:
            async def aiter_bytes(self):
                async for b in raw_chunks():
                    yield b

        async def gen():
            async for ev in toolrelay.stream_model_call(R()):
                yield ev

        return gen()

    async def execute(server, tool, args):
        executed["n"] += 1
        return (True, {"ok": executed["n"]})

    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                kwargs.pop("messages", [{"role": "user", "content": "go"}]),
                kwargs.pop("grants", GRANTS),
                kwargs.pop("catalog", CATALOG),
                stream_once,
                execute,
                "rid",
                kwargs.pop("model", "m1"),
                **kwargs,
            )
        ]

    text = b"".join(asyncio.run(run())).decode()
    return text, executed["n"], wrap_inputs


def _tool_round(name: str, i: int, args: dict | None = None) -> dict:
    return {
        "content": None,
        "tool_calls": [{"id": f"c{i}", "function": {"name": name, "arguments": json.dumps(args or {"i": i})}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    }


def test_exhaustion_message_and_ask_user_handoff() -> None:
    """Budget exhaustion keeps the 2026-10 operator handoff: the wrap-up pass
    is told to summarize and ASK THE USER (their next turn is a fresh budget).
    The SSE carries the guard detail, never the model-facing instruction."""
    text, executed, wrap_inputs = _relay(
        [_tool_round("logsearch__search_logs", 0), _tool_round("logsearch__search_logs", 1), {"content": "summary?"}],
        tool_weights={},
        max_iterations=1.0,
    )
    assert "tool budget exhausted (2.00/1.00 units)" in text
    assert executed == 1, "the aborted batch must not execute"
    assert text.rstrip().endswith("data: [DONE]")
    assert "ASK THE USER" not in text, "the instruction is model-facing, not user-facing"
    assert wrap_inputs, "a toolless wrap-up call must have happened"
    assert any("ASK THE USER" in json.dumps(m) for m in wrap_inputs)


def test_loop_abort_does_not_ask_the_user() -> None:
    """The handoff is for the BUDGET only: a repeated identical call must not
    be laundered into a permission request."""
    same = {
        "content": None,
        "tool_calls": [{"id": "c1", "function": {"name": "logsearch__search_logs", "arguments": '{"q": "x"}'}}],
    }
    text, _executed, wrap_inputs = _relay([same, same, {"content": "done"}], tool_weights={}, max_iterations=16.0)
    assert "loop detected" in text
    assert wrap_inputs and any("Tool use halted" in json.dumps(m) for m in wrap_inputs)
    assert not any("ASK THE USER" in json.dumps(m) for m in wrap_inputs)


def test_profile_budget_overrides_the_site_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PROFILE's budget is what bounds the turn — not the site default.

    The wiring is one line at the routes layer (max_iterations=key.tool_relay_budget,
    None = inherit), so the guard must honour an explicit budget while settings
    still says 16."""
    monkeypatch.setattr(settings, "tool_relay_max_iterations", 16)
    key = key_record_from_row(_key_row(tool_relay_budget=1))
    assert key is not None and key.tool_relay_budget == 1
    text, executed, _wrap = _relay(
        [_tool_round("logsearch__search_logs", 0), _tool_round("logsearch__search_logs", 1), {"content": "end"}],
        tool_weights={},
        max_iterations=key.tool_relay_budget,
    )
    assert "tool budget exhausted (2.00/1.00 units)" in text, "the profile budget fired, not the site 16"
    assert executed == 1


def test_null_profile_budget_inherits_the_site_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """NULL profile budget = inherit settings.tool_relay_max_iterations (read
    as the same unit budget) — the pre-feature bound, unchanged."""
    monkeypatch.setattr(settings, "tool_relay_max_iterations", 1)
    key = key_record_from_row(_key_row())  # no tool_relay_budget column at all
    assert key is not None and key.tool_relay_budget is None
    text, executed, _wrap = _relay(
        [_tool_round("logsearch__search_logs", 0), _tool_round("logsearch__search_logs", 1), {"content": "end"}],
        tool_weights={},
        max_iterations=key.tool_relay_budget,  # None -> relay_stream reads settings
    )
    assert "tool budget exhausted (2.00/1.00 units)" in text
    assert executed == 1


def test_relay_stream_defaults_weights_to_the_cached_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the caller passes no tool_weights, relay_stream prices calls from
    core.mcp_weights (a 0.5-weight server lets four calls run under a 2-unit
    budget before the fifth aborts)."""

    class _DB:
        async def fetch(self, sql: str, *a: Any):
            return [{"name": "logsearch", "tool_weight": 0.5}]

    monkeypatch.setattr(mcp_weights, "db", _DB())
    mcp_weights.invalidate()
    rounds = [_tool_round("logsearch__search_logs", i) for i in range(5)]
    text, executed, _wrap = _relay([*rounds, {"content": "end"}], max_iterations=2.0)
    assert executed == 4, "four 0.5-unit calls fit a 2-unit budget"
    assert "tool budget exhausted (2.50/2.00 units)" in text
    mcp_weights.invalidate()


# ---------------------------------------------------------------------------
# core.mcp_weights: caching discipline + degrade
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_weight_cache():
    mcp_weights.invalidate()
    yield
    mcp_weights.invalidate()


def test_weights_degrade_to_empty_on_db_fault() -> None:
    """A DB fault must NEVER fail a relay turn: weights() returns {} (every
    call then charges the neutral 1.0) and logs — it does not raise."""

    class _Boom:
        async def fetch(self, sql: str, *a: Any):
            raise RuntimeError("database not connected")

    import pcai_llm.core.mcp_weights as mw

    original = mw.db
    mw.db = _Boom()  # type: ignore[assignment]
    try:
        assert asyncio.run(mw.weights()) == {}
    finally:
        mw.db = original


def test_weights_reads_enabled_servers_and_filters_non_positive() -> None:
    class _DB:
        def __init__(self, rows: list[dict]) -> None:
            self.rows = rows
            self.sql = ""

        async def fetch(self, sql: str, *a: Any):
            self.sql = sql
            return self.rows

    import pcai_llm.core.mcp_weights as mw

    fake = _DB(
        [
            {"name": "cheap", "tool_weight": 0.1},
            {"name": "heavy", "tool_weight": 2.5},
            {"name": "free", "tool_weight": 0},
        ]
    )
    original = mw.db
    mw.db = fake  # type: ignore[assignment]
    try:
        mcp_weights.invalidate()
        assert asyncio.run(mw.weights()) == {"cheap": 0.1, "heavy": 2.5}
        assert "WHERE enabled" in fake.sql, "disabled servers carry no relay weight"
        fake.sql = ""
        assert asyncio.run(mw.weights()) == {"cheap": 0.1, "heavy": 2.5}
        assert fake.sql == "", "the TTL cache served the second read"
        mcp_weights.invalidate()
        fake.rows = [{"name": "cheap", "tool_weight": 0.25}]
        assert asyncio.run(mw.weights()) == {"cheap": 0.25}
    finally:
        mw.db = original


def test_weights_returns_a_copy() -> None:
    class _DB:
        async def fetch(self, sql: str, *a: Any):
            return [{"name": "cheap", "tool_weight": 0.1}]

    import pcai_llm.core.mcp_weights as mw

    original = mw.db
    mw.db = _DB()  # type: ignore[assignment]
    try:
        mcp_weights.invalidate()
        first = asyncio.run(mw.weights())
        first["cheap"] = 99.0
        assert asyncio.run(mw.weights()) == {"cheap": 0.1}, "a caller must not mutate the shared cache"
    finally:
        mw.db = original


# ---------------------------------------------------------------------------
# Admin API round-trip
# ---------------------------------------------------------------------------


class _AdminFake:
    """Just enough asyncpg for the profile + MCP-server admin surfaces."""

    def __init__(self, profile_rows: list[dict] | None = None, weight_rows: list[dict] | None = None) -> None:
        self.profile_rows = profile_rows or []
        self.weight_rows = weight_rows or []
        self.upserts: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any):
        s = " ".join(sql.split())
        if "FROM profiles WHERE name" in s:
            return None  # no stored profile: the INSERT arm
        if "INSERT INTO profiles" in s:
            self.upserts.append((s, args))
            return {
                "id": uuid.uuid4(),
                "name": args[0],
                "model_order": args[1],
                "defaults_json": args[2],
                "offpeak_fraction": args[3],
                "routing_policy": args[4],
                "routing_min_model": args[5],
                "tool_relay_budget": args[6],
            }
        if "INSERT INTO mcp_servers" in s:
            self.upserts.append((s, args))
            return {
                "id": "1",
                "name": args[0],
                "url": args[1],
                "auth_secret": None,
                "auth_configured": False,
                "tool_policy": args[5],
                "allowed_tools_json": args[6],
                "enabled": args[7],
                "priority": args[8],
                "description": args[9],
                "tool_weight": args[10],
            }
        return None

    async def fetch(self, sql: str, *args: Any):
        s = " ".join(sql.split())
        if "FROM profile_agents" in s:
            return []
        if "FROM profiles" in s:
            return self.profile_rows
        if "FROM mcp_servers" in s:
            return self.weight_rows
        return []

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def execute(self, sql: str, *args: Any):
        return "INSERT 0 1"


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")

    async def _no_refresh() -> None:
        pass

    monkeypatch.setattr(registry, "refresh", _no_refresh)
    monkeypatch.setattr(registry, "ensure_fresh", _no_refresh)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def _admin_h() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-123"}


def test_admin_profile_tool_relay_budget_roundtrip(admin_client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _AdminFake()
    monkeypatch.setattr(routes_admin, "db", fake)
    r = admin_client.post("/admin/profiles", json={"name": "team-b", "tool_relay_budget": 7}, headers=_admin_h())
    assert r.status_code == 201, r.text
    assert r.json()["tool_relay_budget"] == 7, "the save response round-trips the budget"
    sql, args = fake.upserts[-1]
    assert "tool_relay_budget" in sql, "the INSERT carries the column"
    assert "tool_relay_budget = $7" in sql, "the ON CONFLICT arm writes it too"
    assert "tool_relay_budget" in sql.split("RETURNING")[1], "RETURNING carries it back"
    assert args[6] == 7, "bound at $7 (the routing pair keeps its historical $5/$6 slots)"


def test_admin_profile_list_carries_tool_relay_budget(admin_client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _AdminFake(
        profile_rows=[
            {
                "id": uuid.uuid4(),
                "name": "team-b",
                "model_order": [],
                "defaults_json": "{}",
                "offpeak_fraction": None,
                "routing_policy": "off",
                "routing_min_model": "",
                "tool_relay_budget": 7,
            }
        ]
    )
    monkeypatch.setattr(routes_admin, "db", fake)
    r = admin_client.get("/admin/profiles", headers=_admin_h())
    assert r.status_code == 200, r.text
    assert r.json()[0]["tool_relay_budget"] == 7


def test_admin_mcp_server_tool_weight_roundtrip(admin_client, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _AdminFake(weight_rows=[{"name": "cheap", "tool_weight": 0.25}])
    monkeypatch.setattr(routes_admin, "db", fake)

    async def _servers() -> list[dict]:
        return [{"id": "1", "name": "cheap", "url": "http://cheap.internal/mcp", "headers": {}, "auth_token_enc": None}]

    monkeypatch.setattr(registry, "mcp_servers", _servers)
    mcp_weights.invalidate()

    r = admin_client.post(
        "/admin/mcp-servers",
        json={"name": "cheap", "url": "http://cheap.internal/mcp", "tool_weight": 0.25},
        headers=_admin_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["tool_weight"] == 0.25
    sql, args = fake.upserts[-1]
    assert "tool_weight" in sql, "the upsert carries the weight column"
    assert "tool_weight = $11" in sql, "an EXPLICIT weight is written, not coalesced"
    assert args[10] == 0.25, "bound last, so every existing positional reader is untouched"
    assert mcp_weights._cache is None, "an MCP-server write invalidates the relay weight cache"

    g = admin_client.get("/admin/mcp-servers", headers=_admin_h())
    assert g.status_code == 200, g.text
    assert g.json()[0]["tool_weight"] == 0.25


def test_admin_mcp_server_tool_weight_omitted_keeps_stored(admin_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """THREE-STATE: a client that does not mention tool_weight must not reprice
    the server (cross-review 2026-11-03; the release gate then forced the
    mechanism change this test pins).

    The POST is a FULL UPSERT — it rewrites every column — so an omitted weight
    would otherwise silently reset an expensive server to the 1.0 default, a
    config wipe that looks like a successful save. The keep is expressed as
    SQL-text branching rather than COALESCE($11, ...), because the static
    SQL<->schema gate counts VALUES expressions by splitting on commas and read
    the comma-bearing expression as a twelfth column. Both the branch and the
    bound value are asserted, so a future refactor back to COALESCE fails HERE
    (and at the gate) instead of silently repricing servers in production.
    """
    fake = _AdminFake(weight_rows=[{"name": "heavy", "tool_weight": 3.0}])
    monkeypatch.setattr(routes_admin, "db", fake)

    async def _servers() -> list[dict]:
        return [
            {
                "id": "1",
                "name": "heavy",
                "url": "http://heavy.internal/mcp",
                "headers": {},
                "auth_token_enc": None,
            }
        ]

    monkeypatch.setattr(registry, "mcp_servers", _servers)
    mcp_weights.invalidate()

    r = admin_client.post(
        "/admin/mcp-servers",
        json={"name": "heavy", "url": "http://heavy.internal/mcp"},  # weight omitted
        headers=_admin_h(),
    )
    assert r.status_code == 200, r.text
    sql, args = fake.upserts[-1]
    assert "tool_weight = mcp_servers.tool_weight" in sql, (
        "an omitted weight must KEEP the stored one; a full upsert that rewrites "
        "every column would silently reprice an expensive server to the default"
    )
    assert args[10] == 1.0, "the INSERT arm still binds a concrete value (the column is NOT NULL)"


def test_admin_mcp_server_tool_weight_bounds(admin_client, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 0 weight would make a server's calls free (a loop on it would run to
    the wall-clock bound alone) and >100 would spend a turn's whole budget on
    one call — both are refused at the edge."""
    monkeypatch.setattr(routes_admin, "db", _AdminFake())
    for bad in (0, -1.0, 100.01):
        r = admin_client.post(
            "/admin/mcp-servers",
            json={"name": "cheap", "url": "http://cheap.internal/mcp", "tool_weight": bad},
            headers=_admin_h(),
        )
        assert r.status_code == 422, f"tool_weight={bad} must be refused: {r.text}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _key_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-abc",
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100000,
        "max_concurrent": 0,
        "expires_at": None,
        "revoked_at": None,
        "relay_default": False,
        "stream_pcai_events": True,
        "offpeak_fraction": None,
        "mcp_admin": False,
    }
    row.update(overrides)
    return row
