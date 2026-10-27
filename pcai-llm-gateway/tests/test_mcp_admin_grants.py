"""P0-3 grant surface — admin_* hard stop, mcp_admin key wiring, profile presets.

The control under test is a DEFAULT: the api_keys.mcp_admin column is FALSE for
every key that did not explicitly opt in, so no model-driven key (and no test
fake / pre-migration row that simply lacks the field) can ever hold
sqlhandler's admin_* verbs on the MCP surface. The profile-preset stamping
tests pin the second half: a preset changes what a key of that profile sees,
and the feature being UNUSED changes nothing.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.core import keys
from pcai_llm.mcpf import federation

# ---------------------------------------------------------------------------
# fakes (same sql-sniffing style tests/test_admin.py established)
# ---------------------------------------------------------------------------


class FakeConn:
    """Acquired-connection stand-in delegating to the parent fake (the
    create-key path writes INSIDE a transaction, like the real flow)."""

    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def fetchrow(self, sql: str, *args: Any):
        return await self._db.fetchrow(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def executemany(self, sql: str, args: Any):
        self._db.queries.append((sql, tuple(map(tuple, args))))


class FakeDB:
    """Canned authenticate/load_key_record rows + INSERT capture."""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.queries: list[tuple[str, tuple]] = []
        self.last_insert: tuple[str, tuple] | None = None

    def acquire(self):
        return FakeConn(self)

    async def fetchrow(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        if "INSERT INTO api_keys" in sql:
            self.last_insert = (sql, args)
            return {"id": uuid.uuid4()}
        return self.row

    async def fetch(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return []

    async def execute(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return "UPDATE 1"


@pytest.fixture
def patched_db(monkeypatch: pytest.MonkeyPatch):
    def _install(row: dict[str, Any] | None) -> FakeDB:
        fake = FakeDB(row)
        monkeypatch.setattr(keys, "db", fake)
        return fake

    return _install


def _key_row(**over: Any) -> dict[str, Any]:
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "session-key",
        "key_prefix": "hllm-ABCD",
        "scopes": ["proxy", "mcp"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "max_concurrent": 0,
        "expires_at": None,
        "revoked_at": None,
        "relay_default": False,
        "stream_pcai_events": True,
        "offpeak_fraction": None,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# mcp_admin: the column is the control — default False, missing field = False
# ---------------------------------------------------------------------------


async def test_key_row_without_mcp_admin_authenticates_and_is_refused_admin(patched_db) -> None:
    """THE control test: a row that predates the column (or a fake that omits
    the field) still authenticates — and the record it yields is REFUSED
    admin_* on the MCP surface."""
    # NOTE: the call is the fixture's side effect (it patches the db seam);
    # the binding was never used, so it is deliberately not assigned.
    patched_db(_key_row())  # NO mcp_admin key at all
    rec = await keys.authenticate("plaintext-irrelevant")
    assert rec is not None
    assert rec.mcp_admin is False  # getattr-safe default: False, never a KeyError
    refusal = federation.admin_tool_refusal("sqlhandler", "admin_key_mint", rec.mcp_admin)
    assert refusal  # REFUSED
    # ...while its ordinary grants stay call-eligible under the same gate
    assert federation.admin_tool_refusal("sqlhandler", "run_sql", rec.mcp_admin) == ""


async def test_authenticate_select_names_the_column_and_reads_true(patched_db) -> None:
    """The SELECT must name k.mcp_admin (a SELECT missing it would silently
    revert every flagged key to the default) and a TRUE row must round-trip."""
    fake = patched_db(_key_row(mcp_admin=True))
    rec = await keys.authenticate("x")
    select_sql, _ = fake.queries[0]
    assert "k.mcp_admin" in select_sql
    assert rec is not None and rec.mcp_admin is True


async def test_load_key_record_carries_mcp_admin(patched_db) -> None:
    patched_db(_key_row(mcp_admin=True))
    rec = await keys.load_key_record(uuid.uuid4())
    assert rec is not None and rec.mcp_admin is True
    patched_db(_key_row())
    rec = await keys.load_key_record(uuid.uuid4())
    assert rec is not None and rec.mcp_admin is False


async def test_create_key_never_writes_mcp_admin(patched_db) -> None:
    """core.keys.create_key (the ENROLLMENT / self-mint path) must not even
    accept the flag: only the admin console endpoint can mint an
    admin-capable key, and it defaults False."""
    fake = patched_db({"id": uuid.uuid4()})
    await keys.create_key(uuid.uuid4(), "k", ["proxy", "mcp"])
    insert_sql, _ = fake.last_insert
    assert "mcp_admin" not in insert_sql


def test_keyrecord_default_is_false() -> None:
    rec = keys.KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-x",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=60,
        rate_limit_tpm=1000,
        max_concurrent=0,
        expires_at=None,
    )
    assert rec.mcp_admin is False


# ---------------------------------------------------------------------------
# admin-API surface: create / patch expose the flag
# ---------------------------------------------------------------------------


class _KeyWriteDB(FakeDB):
    """FakeDB + fetchval (the _exists seam) for the key-mutation endpoints;
    the PATCH's UPDATE..RETURNING must be answered with a row (the endpoint
    asserts it, exactly like the real pool would always return one)."""

    async def fetchval(self, sql: str, *args: Any):
        return 1  # _exists: every id exists

    async def fetchrow(self, sql: str, *args: Any):
        if "UPDATE api_keys" in sql:
            return {
                "id": uuid.uuid4(), "user_id": uuid.uuid4(), "name": "k",
                "scopes": ["proxy"], "allowed_models": [], "rate_limit_rpm": 60,
                "rate_limit_tpm": 1000, "max_concurrent": 0, "expires_at": None,
                "relay_default": False, "stream_pcai_events": True, "mcp_admin": True,
            }
        return await super().fetchrow(sql, *args)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = _KeyWriteDB()
    import pcai_llm.core.budgets as budgets_mod

    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(keys, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-123"}


def test_admin_create_key_defaults_mcp_admin_false(client) -> None:
    tc, fake = client
    r = tc.post(
        "/admin/keys",
        json={"user_id": str(uuid.uuid4()), "name": "k"},
        headers=_h(),
    )
    assert r.status_code == 201
    insert_sql, args = fake.last_insert
    assert "mcp_admin" in insert_sql
    assert args[-1] is False  # the DEFAULT FALSE is written, not omitted


def test_admin_create_key_can_opt_in_and_patch_carries_column(client) -> None:
    tc, fake = client
    r = tc.post(
        "/admin/keys",
        json={"user_id": str(uuid.uuid4()), "name": "ops", "mcp_admin": True},
        headers=_h(),
    )
    assert r.status_code == 201
    _sql, args = fake.last_insert
    assert args[-1] is True
    # and the PATCH path names the column too
    r2 = tc.patch(f"/admin/keys/{uuid.uuid4()}", json={"mcp_admin": True}, headers=_h())
    assert r2.status_code == 200
    upd_sql = fake.queries[-1][0]
    assert "mcp_admin" in upd_sql


# ---------------------------------------------------------------------------
# profile presets: stamping changes a profile key's grants; unused = no change
# ---------------------------------------------------------------------------


FLEET_CATALOGS = {
    "k8s-ops": [{"tool": "get_pod_logs"}, {"tool": "triage"}, {"tool": "exec_in_pod"}, {"tool": "run_kubectl"}],
    "prometheus": [{"tool": "prom_query"}],
    "logsearch": [{"tool": "search_logs"}],
    "mm-rag": [
        {"tool": "search_dataset"},
        # REAL registered write verbs (wave-1 review P1-2): the observe stamp
        # must exclude every one of these against the catalog itself
        {"tool": "dataset_add_documents"},
        {"tool": "dataset_delete_documents"},
        {"tool": "dataset_replace_document"},
        {"tool": "add_memory"},
        {"tool": "delete_memory"},
        {"tool": "forget_session"},
        {"tool": "set_memory_dataset"},
        {"tool": "deselect_dataset"},
        {"tool": "unlock_dataset"},
        {"tool": "create_short_term_memory"},
        {"tool": "end_short_term_memory"},
    ],
    "seaborn": [{"tool": "plot"}],
    "sqlhandler": [
        {"tool": "run_sql"},
        {"tool": "query_save"},
        {"tool": "query_delete"},
        {"tool": "admin_grants"},
        {"tool": "admin_key_mint"},
    ],
    "searxng": [{"tool": "search"}, {"tool": "fetch_content"}],
    "applygate": [{"tool": "apply_manifest"}, {"tool": "plan_apply"}],
    "ezapp-deploy": [{"tool": "apply_ezappconfig"}],
    "workbench": [{"tool": "write_file"}],
}


class _ProfileStampDB:
    """Captures the profile upsert (INSERT INTO profiles ... RETURNING) and
    echoes the stored defaults_json back, so tests can assert EXACTLY what
    the stamp wrote — the same document load_live_profile_grants reads."""

    def __init__(self) -> None:
        self.last_upsert: tuple[str, tuple] | None = None

    async def fetchrow(self, sql: str, *args: Any):
        if "INSERT INTO profiles" in sql:
            self.last_upsert = (sql, args)
            return {
                "id": uuid.uuid4(),
                "name": args[0],
                "model_order": args[1],
                "defaults_json": args[2],
                "offpeak_fraction": args[3],
                "routing_policy": args[4],
                "routing_min_model": args[5],
            }
        return None

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchval(self, sql: str, *a: Any):
        return 1

    async def execute(self, sql: str, *a: Any):
        return "INSERT 0 1"


class _FakeRegistry:
    """Stand-in for core.registry: every classified server is registered."""

    async def mcp_servers(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "url": "http://" + name + ".svc:9000/mcp", "headers": {},
             "auth_secret": None, "auth_token_enc": None}
            for name in sorted(federation.MCP_SERVER_CLASSES)
        ]


@pytest.fixture
def stamp_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = _ProfileStampDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(routes_admin, "registry", _FakeRegistry())

    async def _fake_fetch_tools(url: str, headers: dict[str, str], timeout_s: float | None = None):
        server = url.split("//", 1)[1].split(".", 1)[0]
        return list(FLEET_CATALOGS.get(server, []))

    import pcai_llm.api.routes_mcp as routes_mcp_mod

    monkeypatch.setattr(routes_mcp_mod, "_fetch_tools", _fake_fetch_tools)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake


def test_stamping_observe_preset_writes_merged_grants(stamp_client) -> None:
    tc, fake = stamp_client
    r = tc.post(
        "/admin/profiles",
        json={
            "name": "observer",
            "model_order": [],
            "mcp_grants": [{"server": "k8s-ops", "tool": "list_namespaces"}],
            "mcp_grant_preset": "observe",
        },
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["mcp_grant_preset"] == "observe"
    assert body["preset_catalog_ok"] is True
    assert body["preset_unavailable_servers"] == []
    stored = json.loads(fake.last_upsert[1][2])
    assert stored["mcp_grant_preset"] == "observe"
    grants = {tuple(g) for g in stored["mcp_grants"]}
    # explicit grant survived the merge...
    assert ("k8s-ops", "list_namespaces") in grants
    # ...and the preset added every READ pair — with admin verbs AND every
    # write verb (catalog-asserted against the real fleet names, wave-1
    # review P1-2) excluded
    assert ("sqlhandler", "admin_grants") not in grants
    assert ("sqlhandler", "admin_key_mint") not in grants
    # wave-2: run_sql is the read-by-contract exception and RIDES observe;
    # the persistence verbs stay out
    assert ("sqlhandler", "run_sql") in grants
    assert ("sqlhandler", "query_save") not in grants
    assert ("sqlhandler", "query_delete") not in grants
    assert ("k8s-ops", "exec_in_pod") not in grants
    # run_kubectl: read-by-contract exception, RIDES observe (wave-2)
    assert ("k8s-ops", "run_kubectl") in grants
    for verb in (
        "dataset_add_documents", "dataset_delete_documents",
        "dataset_replace_document", "add_memory", "delete_memory",
        "forget_session", "set_memory_dataset", "deselect_dataset",
        "unlock_dataset", "create_short_term_memory", "end_short_term_memory",
    ):
        assert ("mm-rag", verb) not in grants, verb
    assert ("searxng", "fetch_content") not in grants
    assert ("applygate", "apply_manifest") not in grants
    # wave-2: the ONLY write-shaped verbs the observe stamp may carry are
    # the two documented read-by-contract run tools
    from pcai_llm.mcpf.federation import READ_ONLY_RUN_TOOLS, is_write_tool

    assert grants, "stamp resolved nothing — the test would be vacuous"
    write_shaped = {g for g in grants if is_write_tool(g[1])}
    assert write_shaped <= READ_ONLY_RUN_TOOLS
    assert {("sqlhandler", "run_sql"), ("k8s-ops", "run_kubectl")} <= grants  # reads ride observe


def test_admin_preset_stamps_admin_verbs_only(stamp_client) -> None:
    tc, _ = stamp_client
    r = tc.post("/admin/profiles", json={"name": "ops", "mcp_grant_preset": "admin"}, headers=_h())
    assert r.status_code == 201
    grants = {tuple(g) for g in r.json()["mcp_grants"]}
    assert grants == {("sqlhandler", "admin_grants"), ("sqlhandler", "admin_key_mint")}
    # and a key holding even THESE grants is still call-refused without the
    # key-level mcp_admin flag (belt to the suspenders)
    for server, tool in grants:
        assert federation.admin_tool_refusal(server, tool, False)


def test_egress_preset_is_searxng_only(stamp_client) -> None:
    tc, _ = stamp_client
    r = tc.post("/admin/profiles", json={"name": "web", "mcp_grant_preset": "egress"}, headers=_h())
    assert r.status_code == 201
    grants = {tuple(g) for g in r.json()["mcp_grants"]}
    assert grants == {("searxng", "search"), ("searxng", "fetch_content")}


def test_unknown_preset_is_rejected_before_write(stamp_client) -> None:
    tc, fake = stamp_client
    r = tc.post("/admin/profiles", json={"name": "x", "mcp_grant_preset": "everything"}, headers=_h())
    assert r.status_code == 422  # Literal validation, BEFORE any catalog work
    assert fake.last_upsert is None  # nothing written


def test_unavailable_server_is_reported_not_swallowed(stamp_client, monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.api.routes_mcp as routes_mcp_mod

    async def _dead_fetch_tools(url: str, headers: dict[str, str], timeout_s: float | None = None):
        raise TimeoutError("upstream wedged")

    monkeypatch.setattr(routes_mcp_mod, "_fetch_tools", _dead_fetch_tools)
    tc, _ = stamp_client
    r = tc.post("/admin/profiles", json={"name": "y", "mcp_grant_preset": "observe"}, headers=_h())
    assert r.status_code == 201
    body = r.json()
    assert body["preset_catalog_ok"] is False
    assert set(body["preset_unavailable_servers"]) == set(federation.MCP_SERVER_CLASSES)
    assert body["preset_grants"] == 0  # fail-safe: nothing stamped, nothing implied


def test_feature_unused_changes_nothing(stamp_client) -> None:
    """No existing key's grants change when the feature is unused: a POST
    without mcp_grant_preset stores defaults_json with NO preset key and
    mcp_grants exactly as the caller sent them."""
    tc, fake = stamp_client
    r = tc.post(
        "/admin/profiles",
        json={"name": "legacy", "model_order": [], "mcp_grants": [{"server": "logsearch", "tool": "search_logs"}]},
        headers=_h(),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["mcp_grant_preset"] is None
    stored = json.loads(fake.last_upsert[1][2])
    assert "mcp_grant_preset" not in stored
    assert stored["mcp_grants"] == [["logsearch", "search_logs"]]


def test_stamp_changes_what_a_profile_key_sees(stamp_client) -> None:
    """End-to-end at the pure layer: before the stamp a key of this profile
    sees nothing (default-deny); after the observe stamp its live grants are
    exactly the preset selection — via the SAME defaults_json document the
    endpoint wrote."""
    tc, fake = stamp_client
    r = tc.post("/admin/profiles", json={"name": "obs2", "mcp_grant_preset": "observe"}, headers=_h())
    assert r.status_code == 201
    stored = json.loads(fake.last_upsert[1][2])
    # simulate the live-grants evaluation load_grants performs for a key of
    # this profile (defaults present -> pairs callable at call time)
    from pcai_llm.core.enroll_memory import parse_profile_defaults

    defaults = parse_profile_defaults(json.dumps(stored))
    assert defaults.get("live_grants") is None  # stamping alone does NOT flip live mode
    pairs = {(str(g[0]), str(g[1])) for g in defaults["mcp_grants"]}
    catalog = [
        {"server": s, "tool": t}
        for s, tools in FLEET_CATALOGS.items()
        for t in [x["tool"] for x in tools]
    ]
    seen = federation.visible_tools(catalog, pairs)
    seen_pairs = {(t["server"], t["tool"]) for t in seen}
    assert seen_pairs == pairs
    # the exfiltration combination is unreachable from this profile
    assert federation.read_egress_violation(pairs) is None
    assert not {p for p in seen_pairs if federation.is_admin_only_tool(*p)}
