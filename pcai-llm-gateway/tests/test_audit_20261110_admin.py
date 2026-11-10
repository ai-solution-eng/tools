"""Regression tests for the 2026-11-10 audit wave, remediation zone R4
(admin API & registry). Each test names its finding; the fixtures mirror
tests/test_admin.py (FakeDB pattern, mocked DB -- no live Postgres)."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pcai_llm.api import routes_admin
from pcai_llm.config import settings

# ---------------------------------------------------------------------------
# fakes -- minimal RecordingDB (records every SQL text the route sends)
# ---------------------------------------------------------------------------


class _Conn:
    def __init__(self, db: RecordingDB) -> None:
        self._db = db

    def transaction(self) -> _Conn:
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, *args: Any) -> str:
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return await self._db.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> None:
        return None

    async def fetchval(self, sql: str, *args: Any) -> int:
        return 1


class RecordingDB:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self._rows = rows or []

    def acquire(self) -> _Conn:
        return _Conn(self)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((sql, args))
        return self._rows

    async def fetchrow(self, sql: str, *args: Any) -> None:
        return None

    async def fetchval(self, sql: str, *args: Any) -> int:
        return 1

    async def execute(self, sql: str, *args: Any) -> str:
        return "OK"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-0123456789abcdef0123456789ab")
    fake = RecordingDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-0123456789abcdef0123456789ab"}


def _stub_request() -> SimpleNamespace:
    """A request stand-in for direct endpoint invocation: _audit resolves
    actor/request-id/source-ip off it and every lookup degrades to None."""
    return SimpleNamespace(state=SimpleNamespace(), headers={}, client=None)


# ---------------------------------------------------------------------------
# P1-1: GET /admin/usage 500s for 4 of its 5 group_by modes -- the SELECT dims
# carry "expr AS alias" and were reused verbatim as the GROUP BY list;
# PostgreSQL's group_by_item accepts only a_expr. The fix splits select vs
# group expressions; here we assert the shipped SQL shape.
# ---------------------------------------------------------------------------

_USAGE_GROUP_CASES = {
    # group_by -> (an alias the SELECT must carry, the bare GROUP BY clause)
    "key": ("key_name", "l.key_id, k.name, k.key_prefix"),
    "model": (None, "l.model"),
    "tenant": ("tenant_name", "t.id, t.name"),
    "day": ("day", "date_trunc('day', l.ts)"),
    "subject": ("subject_idp", "k.subject_id, s.subject, s.idp, s.kind"),
}


@pytest.mark.parametrize("group_by", sorted(_USAGE_GROUP_CASES))
def test_usage_group_by_selects_aliases_groups_bare(client, group_by: str) -> None:
    tc, fake = client
    r = tc.get("/admin/usage?group_by=" + group_by + "&limit=5", headers=_h())
    assert r.status_code == 200, r.text
    sql, _args = fake.queries[0]
    select_part = sql.split("FROM usage_ledger")[0]
    group_part = sql.split("GROUP BY")[1].split("ORDER BY")[0]
    alias, bare_group = _USAGE_GROUP_CASES[group_by]
    if alias is not None:
        assert (" AS " + alias) in select_part, select_part
    assert " AS " not in group_part, "GROUP BY must be bare a_expr, got: " + group_part
    assert bare_group in group_part, group_part


# ---------------------------------------------------------------------------
# P2-9: upsert_model probed a not-yet-validated URL before the SSRF gate --
# _probe_endpoint_type fires a real GET plus up to four POSTs with a 20 s
# timeout; _check_url_async must refuse loopback/link-local/metadata targets
# FIRST. Assert the call ORDER.
# ---------------------------------------------------------------------------


def test_upsert_model_checks_url_before_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def fake_check(urls: list[str], what: str) -> None:
        order.append("check")

    async def fake_probe(base_url: str, api_key_secret: Any, x: Any) -> SimpleNamespace:
        order.append("probe")
        return SimpleNamespace(type=None, reason="unknown")

    async def fake_refresh() -> None:
        pass

    async def fake_audit(*a: Any, **k: Any) -> None:
        pass

    monkeypatch.setattr(routes_admin, "_check_url_async", fake_check)
    monkeypatch.setattr(routes_admin, "_probe_endpoint_type", fake_probe)
    monkeypatch.setattr(routes_admin, "_refresh_registry_best_effort", fake_refresh)
    monkeypatch.setattr(routes_admin, "_audit", fake_audit)
    monkeypatch.setattr(routes_admin, "db", RecordingDB())
    body = routes_admin.ModelUpsert(
        model_name="m-detect",
        endpoints=[routes_admin.EndpointSpec(base_url="http://10.0.0.5:8000/v1")],
    )
    asyncio.run(routes_admin.upsert_model(body, _stub_request(), None))
    assert order == ["check", "probe"], "SSRF gate must run first, got " + repr(order)


# ---------------------------------------------------------------------------
# P2-10a: the model-upsert shadow guard consulted the model_groups table
# UNGATED while the data plane gates on settings.model_groups_enabled
# (default False) -- with the flag off it refused config for a feature that
# is inert. Both directions asserted.
# ---------------------------------------------------------------------------


def test_upsert_model_shadow_guard_gated_on_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.core.model_groups as mg

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-0123456789abcdef0123456789ab")
    calls: list[str] = []

    async def fake_load(force: bool = False) -> dict[str, Any]:
        calls.append("load")
        return {}

    def fake_group_for(name: str) -> Any:
        calls.append("group_for:" + name)
        return mg.ModelGroup(name=name, model_type="chat", policy="load", members=("m-detect",))

    async def fake_refresh() -> None:
        pass

    async def fake_audit(*a: Any, **k: Any) -> None:
        pass

    monkeypatch.setattr(mg, "load", fake_load)
    monkeypatch.setattr(mg, "group_for", fake_group_for)
    monkeypatch.setattr(routes_admin, "_refresh_registry_best_effort", fake_refresh)
    monkeypatch.setattr(routes_admin, "_audit", fake_audit)
    monkeypatch.setattr(routes_admin, "db", RecordingDB())
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)

    payload = {"model_name": "grp-shadow", "endpoints": [{"base_url": "http://10.0.0.5:8000/v1"}]}

    # FLAG OFF (the default): the guard must not even consult the table.
    monkeypatch.setattr(settings, "model_groups_enabled", False)
    r = tc.post("/admin/models", json=payload, headers=_h())
    assert r.status_code == 200, r.text
    assert calls == [], "shadow guard consulted model_groups with the flag off"

    # FLAG ON: the guard runs and refuses a pool shadowing a group.
    calls.clear()
    monkeypatch.setattr(settings, "model_groups_enabled", True)
    r = tc.post("/admin/models", json=payload, headers=_h())
    assert r.status_code == 400, r.text
    assert "may not shadow a group" in r.text
    assert "load" in calls


# ---------------------------------------------------------------------------
# P2-10b: _validate_fallback_chain accepted model-group names UNGATED -- with
# the flag off a group-named fallback was saved as dead config (the walker
# never resolves groups when the flag is off).
# ---------------------------------------------------------------------------


def test_fallback_chain_group_names_gated_on_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.core.model_groups as mg

    fake = RecordingDB(rows=[{"model_name": "m1"}])
    monkeypatch.setattr(routes_admin, "db", fake)

    load_calls: list[int] = []

    async def fake_load(force: bool = False) -> dict[str, Any]:
        load_calls.append(1)
        return {}

    monkeypatch.setattr(mg, "load", fake_load)

    # FLAG OFF: the group name is just an unknown name -- refused, and the
    # loader is never consulted.
    monkeypatch.setattr(settings, "model_groups_enabled", False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_admin._validate_fallback_chain("m1", ["grp-x"]))
    assert exc.value.status_code == 400
    assert "unknown fallback name" in str(exc.value.detail)
    assert load_calls == []

    # FLAG ON: a chat group is an accepted chain entry.
    monkeypatch.setattr(settings, "model_groups_enabled", True)
    monkeypatch.setattr(
        mg,
        "snapshot",
        lambda: {"grp-x": mg.ModelGroup(name="grp-x", model_type="chat", policy="load", members=("m1",))},
    )
    out = asyncio.run(routes_admin._validate_fallback_chain("m1", ["grp-x"]))
    assert out == ["grp-x"]
    assert load_calls == [1]


# ---------------------------------------------------------------------------
# P3 doc drift: AUDIT_ACTIONS_DOC must stay a SUPERSET of the actions the
# admin-plane writers emit. Walked from the AST so the next emitter cannot
# silently drift the doc again.
# ---------------------------------------------------------------------------


def test_audit_actions_doc_covers_every_emitted_action() -> None:
    from pcai_llm.api import routes_enroll

    doc = set(routes_admin.AUDIT_ACTIONS_DOC.split())
    emitted: set[str] = set()
    # Admin-plane emitters only: routes_mcp_self's self-service vocabulary
    # (mcp.creds.self*, mcp.grants.self) is intentionally outside this doc --
    # those rows audit the /v1/self surface, never an admin actor.
    for mod in (routes_admin, routes_enroll):
        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "_audit":
                continue
            for kw in node.keywords:
                if kw.arg != "action":
                    continue
                # a plain literal OR a conditional ("a" if cond else "b"):
                # collect every string constant in the expression.
                for c in ast.walk(kw.value):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        emitted.add(c.value)
    missing = sorted(emitted - doc)
    assert not missing, "actions emitted but undocumented in AUDIT_ACTIONS_DOC: " + repr(missing)


# ---------------------------------------------------------------------------
# P3: the "NAME CHARSET" invariant covered tenants/users/keys but not profile
# and agent names -- both must refuse HTML metacharacters / control chars,
# while ordinary display names keep passing.
# ---------------------------------------------------------------------------


def test_profile_and_agent_names_enforce_name_charset() -> None:
    for bad in ("<script>", "a&b", "quote'inside"):
        with pytest.raises(ValidationError):
            routes_admin.ProfileCreate(name=bad)
        with pytest.raises(ValidationError):
            routes_admin.AgentUpsert(name=bad)
    routes_admin.ProfileCreate(name="ops - default (eu)")
    routes_admin.AgentUpsert(name="research-agent: v2")


# ---------------------------------------------------------------------------
# P3: the list_keys enrollment filter passed LIKE wildcards through -- the
# query is parameterised, so no injection, but a needle carrying % or _
# silently broadened the match ("100%" matched every label containing
# "100"). The route now escapes; assert both the helper and the wire value.
# ---------------------------------------------------------------------------


def test_like_literal_escape_escapes_metacharacters() -> None:
    assert routes_admin._like_literal_escape("100%") == "100\\%"
    assert routes_admin._like_literal_escape("a_b") == "a\\_b"
    assert routes_admin._like_literal_escape("back\\\\slash") == "back\\\\\\\\slash"
    assert routes_admin._like_literal_escape("plain") == "plain"


def test_list_keys_enrollment_needle_is_escaped_on_the_wire(client, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _fake = client
    seen: dict[str, Any] = {}

    async def fake_list_keys(**kwargs: Any) -> list[dict[str, Any]]:
        seen.update(kwargs)
        return [{"id": "k1"}]

    monkeypatch.setattr(routes_admin, "list_keys", fake_list_keys)
    # budget_status_many rides core.budgets' own db handle — patch it too
    # (the same seams tests/test_admin.py patches for its client fixture).
    import pcai_llm.core.budgets as budgets_mod

    monkeypatch.setattr(budgets_mod, "db", RecordingDB())
    r = tc.get("/admin/keys?enrollment=100%25_station", headers=_h())
    assert r.status_code == 200, r.text
    assert seen["enrollment_search"] == "100\\%\\_station"


# ---------------------------------------------------------------------------
# P3: model-groups cache invalidate/load race -- a load() in flight BEFORE the
# commit could publish pre-commit rows back into _CACHE for a full TTL. The
# fix is a generation counter: load() publishes only a snapshot fetched under
# the generation it captured.
# ---------------------------------------------------------------------------


@pytest.fixture
def mg_state() -> Any:
    import pcai_llm.core.model_groups as mg

    saved = (mg._CACHE, mg._LOADED_AT, mg._GEN)
    yield mg
    mg._CACHE, mg._LOADED_AT, mg._GEN = saved


_GROUP_ROWS = [{"name": "grp", "model_type": "chat", "policy": "load", "members": [{"model": "m1"}], "description": ""}]


def test_invalidate_neutralises_inflight_load(mg_state: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mg = mg_state

    async def fetch_that_invalidates(sql: str, *args: Any) -> list[dict[str, Any]]:
        # the admin commit + invalidate() land WHILE this fetch is in flight
        await mg.invalidate()
        return _GROUP_ROWS  # the PRE-commit rows the fetch already had

    monkeypatch.setattr(mg.db, "fetch", fetch_that_invalidates)
    out = asyncio.run(mg.load(force=True))
    assert out == {}, "pre-commit rows must not be published after a concurrent invalidate()"
    assert mg._CACHE is None
    assert not mg.is_group("grp")


def test_load_publishes_when_generation_is_stable(mg_state: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mg = mg_state

    async def stable_fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        return _GROUP_ROWS

    monkeypatch.setattr(mg.db, "fetch", stable_fetch)
    out = asyncio.run(mg.load(force=True))
    assert "grp" in out
    assert mg.is_group("grp")


def test_fault_after_invalidate_does_not_stamp_stale_freshness(mg_state: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mg = mg_state

    async def boom(sql: str, *args: Any) -> list[dict[str, Any]]:
        await mg.invalidate()
        raise RuntimeError("db down")

    monkeypatch.setattr(mg.db, "fetch", boom)
    out = asyncio.run(mg.load(force=True))
    assert out == {}
    assert mg._LOADED_AT is None, "a superseded attempt must not cache the fault for the TTL"


# ---------------------------------------------------------------------------
# FOLLOW-UP (2026-11-10, R5 integration): the chain verifier now carries an
# HMAC tail anchor; the endpoint must SURFACE anchored/anchor_status — an
# operator reading a bare ok:true must not mistake an unanchored trail for a
# tamper-proof one. The verifier functions are faked at core.chain_audit (the
# handler imports them at call time), so chain_audit.py itself is untouched.
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_client(client, monkeypatch: pytest.MonkeyPatch):
    from pcai_llm.core import chain_audit

    def install(admin: dict[str, Any], mcp: dict[str, Any]):
        async def fake_admin() -> dict[str, Any]:
            return admin

        async def fake_mcp() -> dict[str, Any]:
            return mcp

        monkeypatch.setattr(chain_audit, "verify_admin_audit_chain", fake_admin)
        monkeypatch.setattr(chain_audit, "verify_mcp_call_chain", fake_mcp)

    tc, _fake = client
    return tc, install


def _chain(
    ok: bool = True,
    anchored: bool = True,
    anchor_status: str = "anchored",
    table: str = "admin_audit",
) -> dict[str, Any]:
    return {
        "ok": ok,
        "table": table,
        "checked": 3,
        "first_bad_id": None,
        "kind": "",
        "unchained": 0,
        "anchored": anchored,
        "anchor_status": anchor_status,
    }


def test_audit_chain_status_surfaces_anchor_fields(chain_client) -> None:
    tc, install = chain_client
    install(_chain(), _chain(table="mcp_call_audit"))
    r = tc.get("/admin/audit/chain", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["anchored"] is True
    assert body["anchor_status"] == "anchored"
    # the per-trail dicts (which now carry the verifier's anchor verdict) pass
    # through untouched — an operator can see WHICH trail lost its anchor.
    assert body["admin_audit"]["anchored"] is True
    assert body["mcp_call_audit"]["anchor_status"] == "anchored"


@pytest.mark.parametrize(
    "admin_status,mcp_status,expected",
    [
        ("anchored", "anchored", "anchored"),
        ("stale", "anchored", "stale"),  # rows appended after the last anchor
        ("unanchored", "anchored", "unanchored"),  # the never-anchored residual
        ("unavailable", "anchored", "unavailable"),  # verifier fault must not read as verified
        ("anchored", "anchor-mismatch", "anchor-mismatch"),  # worst-of across trails
    ],
)
def test_audit_chain_status_anchor_rollup_is_worst_of(
    chain_client, admin_status: str, mcp_status: str, expected: str
) -> None:
    tc, install = chain_client
    # core.chain_audit's anchored vocabulary: "anchored" AND "stale" report
    # anchored=True (a stale anchor still vouches for its prefix); every
    # other state reports anchored=False.
    anch = lambda s: s in ("anchored", "stale")
    admin = _chain(anchored=anch(admin_status), anchor_status=admin_status)
    mcp = _chain(table="mcp_call_audit", anchored=anch(mcp_status), anchor_status=mcp_status)
    # core.chain_audit's own contract: a mismatch hardens the verdict to ok=false
    if mcp_status == "anchor-mismatch":
        mcp = {**mcp, "ok": False}
    install(admin, mcp)
    r = tc.get("/admin/audit/chain", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["anchor_status"] == expected, body
    assert body["anchored"] is (anch(admin_status) and anch(mcp_status))
    if mcp_status == "anchor-mismatch":
        assert body["ok"] is False, "a full-recompute tamper must not read as ok:true"


# ---------------------------------------------------------------------------
# FOLLOW-UP 2 (budgets integration): GET /admin/costs must surface the
# pricing-COVERAGE blind spot (audit 2026-11-10 §7) — "unpriced" lists
# registered models with no model_costs row WITHOUT logging (the console
# refreshes this endpoint; warn_unpriced would write a WARNING per refresh),
# and "unit_mismatch" must be a silent no-op until P10 adds model_costs.unit.
# The costs seams are faked at core.costs (module attributes; the endpoint
# holds the module object), model_router at routes_admin.
# ---------------------------------------------------------------------------


class _FakeModelRouter:
    """model_router stand-in: named pools, each a list of endpoint model_types."""

    def __init__(self, pools: dict[str, list[str]]) -> None:
        self._pools = pools

    def models(self) -> list[str]:
        return sorted(self._pools)

    def pool(self, name: str) -> Any:
        types = self._pools.get(name)
        if types is None:
            return None
        return SimpleNamespace(endpoints=[SimpleNamespace(model_type=t) for t in types])


@pytest.fixture
def costs_client(client, monkeypatch: pytest.MonkeyPatch):
    from pcai_llm.core import costs as costs_mod

    state: dict[str, Any] = {"rows": [], "rates": {}}

    async def fake_list_costs() -> list[dict[str, Any]]:
        return list(state["rows"])

    async def fake_load() -> dict[str, dict[str, float]]:
        return state["rates"]  # consumed by the REAL costs.unpriced filter

    monkeypatch.setattr(costs_mod, "list_costs", fake_list_costs)
    monkeypatch.setattr(costs_mod, "_load", fake_load)
    tc, _fake = client
    return tc, state


def _cost_row(model: str, **extra: Any) -> dict[str, Any]:
    row = {
        "model_name": model,
        "input_per_m": 1.0,
        "output_per_m": 2.0,
        "cached_per_m": 0.5,
        "updated_at": "2026-11-10T00:00:00+00:00",
    }
    row.update(extra)
    return row


def test_costs_read_surfaces_unpriced_without_logging(
    costs_client, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tc, state = costs_client
    monkeypatch.setattr(
        routes_admin, "model_router", _FakeModelRouter({"m-priced": ["chat"], "m-embed": ["embedding"]})
    )
    state["rows"] = [_cost_row("m-priced")]
    state["rates"] = {"m-priced": {"input_per_m": 1.0, "output_per_m": 2.0}}
    with caplog.at_level("WARNING"):
        r = tc.get("/admin/costs", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    # the blind spot is NAMED — a registered model with no model_costs row
    assert body["unpriced"] == ["m-embed"]
    assert body["costs"] == state["rows"]  # the price projection is unchanged
    # ...and the read surface must NOT log: the console refreshes it per visit
    assert "UNPRICED" not in caplog.text, "a console refresh must not emit the warn_unpriced WARNING"


def test_costs_read_unit_mismatch_is_silent_noop_without_unit_column(
    costs_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # P10 (model_costs.unit) is NOT started: rows carry no "unit" field and
    # the endpoint must skip the check without error.
    tc, state = costs_client
    monkeypatch.setattr(routes_admin, "model_router", _FakeModelRouter({"m-priced": ["chat"]}))
    state["rows"] = [_cost_row("m-priced")]
    state["rates"] = {"m-priced": {"input_per_m": 1.0}}
    r = tc.get("/admin/costs", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["unit_mismatch"] == []


def test_costs_read_reports_unit_mismatch_when_column_present(costs_client, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, state = costs_client
    monkeypatch.setattr(
        routes_admin,
        "model_router",
        _FakeModelRouter(
            {
                "m-chat-bad": ["chat"],  # seconds on a token-billed route
                "m-embed-ok": ["embedding"],  # tokens — matches
                "m-mixed-bad": ["chat", "embedding"],  # seconds matches NEITHER route
                "m-mixed-ok": ["chat", "embedding"],  # tokens matches BOTH routes' unit
            }
        ),
    )
    state["rows"] = [
        _cost_row("m-chat-bad", unit="seconds"),
        _cost_row("m-embed-ok", unit="tokens"),
        _cost_row("m-mixed-bad", unit="seconds"),
        _cost_row("m-mixed-ok", unit="tokens"),
    ]
    state["rates"] = {r["model_name"]: {"input_per_m": 1.0} for r in state["rows"]}
    r = tc.get("/admin/costs", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    reported = {m["model"]: m for m in body["unit_mismatch"]}
    assert set(reported) == {"m-chat-bad", "m-mixed-bad"}, body
    assert reported["m-chat-bad"]["expected"] == "tokens"
    assert reported["m-chat-bad"]["model_type"] == "chat"
    # a MIXED pool bills both unit kinds across its endpoints — a unit must
    # match at least ONE route's unit to be consistent
    assert reported["m-mixed-bad"]["expected"] == "tokens"
    assert "m-embed-ok" not in reported and "m-mixed-ok" not in reported
