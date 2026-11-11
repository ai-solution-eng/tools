"""Admin API: SSO tier profiles (F1) + model-group CRUD/preview (F3).

Two seams live in routes_admin and are pinned here:

- the tenant SSO TIER pair (sso_admin_profile_id / sso_admin_groups). Its PUT
  must stay byte-compatible with a body carrying only profile_id (every console
  and script written before the tier split sends exactly that), and must apply
  the admin fields ONLY when the caller actually sent them.
- the model-group curation API. Member validation is delegated to
  core.model_groups and the preview's scores come from core.load_snapshot —
  both written in parallel — so those two modules are replaced through the
  package attribute (the routes import them LAZILY inside the handlers). These
  tests therefore pin the ROUTE contract (status codes, SQL shape, audit
  action, cache invalidation, no-pin-on-preview) instead of duplicating the
  core modules' own unit tests.
"""

from __future__ import annotations

import json
import types
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings

ADMIN_KEY = "admin-secret-123"


def _h() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_KEY}"}


# ---------------------------------------------------------------------------
# fakes: the admin routes' SQL shapes + the two core modules under construction
# ---------------------------------------------------------------------------


class FakeConn:
    """Stands in for an acquired asyncpg connection (transaction + all)."""

    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def fetchrow(self, sql: str, *a: Any):
        return await self._db.fetchrow(sql, *a)

    async def fetchval(self, sql: str, *a: Any):
        return await self._db.fetchval(sql, *a)

    async def execute(self, sql: str, *a: Any):
        return await self._db.execute(sql, *a)

    async def executemany(self, sql: str, args: Any):
        return await self._db.executemany(sql, args)

    async def fetch(self, sql: str, *a: Any):
        return await self._db.fetch(sql, *a)


class FakeDB:
    """Just enough SQL-shape dispatch for the tenant + model-group routes.

    The fake mirrors the statements literally — including the DYNAMIC parameter
    positions of the tenant UPDATE — so a change to a route's SQL fails here
    instead of silently passing. That SQL is the contract the mint reads.
    """

    def __init__(self) -> None:
        self.tenants: dict[uuid.UUID, dict[str, Any]] = {}
        self.profiles: dict[uuid.UUID, dict[str, Any]] = {}
        self.groups: dict[str, dict[str, Any]] = {}
        # Registered pool names (the fallback-chain validation's registry read).
        self.models: set[str] = set()
        self.fetchrows: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchvals: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def acquire(self):
        return FakeConn(self)

    async def fetchval(self, sql: str, *a: Any):
        self.fetchvals.append((sql, a))
        if "FROM tenants" in sql:
            return 1 if a and a[0] in self.tenants else None
        if "FROM profiles" in sql:
            return 1 if a and a[0] in self.profiles else None
        if "FROM model_groups" in sql:
            return 1 if a and a[0] in self.groups else None
        return None

    async def fetchrow(self, sql: str, *a: Any):
        self.fetchrows.append((sql, a))
        if "defaults_json" in sql:
            return self.profiles.get(a[0])
        if "UPDATE tenants" in sql:
            row = dict(self.tenants[a[0]])
            for pos, value in enumerate(a[1:], start=2):
                if f"sso_default_profile_id = ${pos}" in sql:
                    row["sso_default_profile_id"] = value
                elif f"sso_admin_profile_id = ${pos}" in sql:
                    row["sso_admin_profile_id"] = value
                elif f"sso_admin_groups = ${pos}" in sql:
                    row["sso_admin_groups"] = value
            self.tenants[a[0]] = row
            return {
                key: row.get(key)
                for key in (
                    "id",
                    "name",
                    "sso_default_profile_id",
                    "sso_admin_profile_id",
                    "sso_admin_groups",
                )
            }
        if "DELETE FROM model_groups" in sql:
            if self.groups.pop(a[0], None) is None:
                return None
            return {"name": a[0]}
        if "INSERT INTO model_groups" in sql:
            name, model_type, policy, members_json, description = a
            row = self.groups.get(name) or {"name": name, "created_at": "2026-11-08T00:00:00+00:00"}
            row.update(
                {
                    "model_type": model_type,
                    "policy": policy,
                    # stored/returned as the JSON STRING, exactly like asyncpg
                    # hands back a JSONB column (no codec registered)
                    "members": members_json,
                    "description": description,
                }
            )
            self.groups[name] = row
            return dict(row)
        return None

    async def fetch(self, sql: str, *a: Any):
        self.fetches.append((sql, a))
        if "FROM model_groups" in sql:
            return [dict(g) for g in self.groups.values()]
        if "FROM model_endpoints" in sql:
            return [{"model_name": m} for m in sorted(self.models)]
        if "FROM tenants" in sql:
            out = []
            for tenant in self.tenants.values():
                row = dict(tenant)
                for col, name_col in (
                    ("sso_default_profile_id", "sso_default_profile_name"),
                    ("sso_admin_profile_id", "sso_admin_profile_name"),
                ):
                    profile_id = tenant.get(col)
                    profile = self.profiles.get(profile_id) if isinstance(profile_id, uuid.UUID) else None
                    row[name_col] = (profile or {}).get("name")
                out.append(row)
            return out
        return []

    async def execute(self, sql: str, *a: Any):
        self.executed.append((sql, a))
        return "OK"

    async def executemany(self, sql: str, args: Any):
        self.executed.append((sql, args))
        return "OK"


@pytest.fixture
def audit_log(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every _audit() call (the route's own writer is patched; the
    trail's own DB seam is not this test's concern)."""
    calls: list[dict[str, Any]] = []

    async def _recorder(conn: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(routes_admin, "audit_action", _recorder)
    return calls


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, audit_log: list[dict[str, Any]]):
    monkeypatch.setattr(settings, "admin_api_keys", ADMIN_KEY)
    fake = FakeDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake


@pytest.fixture
def groups_core(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Controllable stand-in for core.model_groups (written in parallel).

    The routes import it lazily inside the handlers, so replacing the package
    attribute is enough. Every seam the admin API uses is present with its
    documented signature; tests override what they assert on.
    """
    import pcai_llm.core as core_pkg

    mod: Any = types.ModuleType("pcai_llm.core.model_groups")
    mod.validate_members = lambda members, all_models, groups: None
    mod.snapshot = dict
    mod.group_for = lambda name: None
    mod.pick_member = lambda group, allowed, score: None
    mod.invalidated = []
    mod.loaded = []

    async def _load(force: bool = False) -> dict[str, Any]:
        mod.loaded.append(force)
        return {}

    async def _invalidate() -> None:
        mod.invalidated.append(True)

    mod.load = _load
    mod.invalidate = _invalidate
    monkeypatch.setattr(core_pkg, "model_groups", mod, raising=False)
    return mod


@pytest.fixture
def load_snapshot_core(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Stand-in for core.load_snapshot: member_score reads a per-test map."""
    import pcai_llm.core as core_pkg

    mod: Any = types.ModuleType("pcai_llm.core.load_snapshot")
    mod.scores = {}
    mod.member_score = lambda member: mod.scores.get(member)
    monkeypatch.setattr(core_pkg, "load_snapshot", mod, raising=False)
    return mod


class _FakePool:
    def __init__(self, model_type: str = "chat", healthy: int = 1) -> None:
        self.endpoints = [types.SimpleNamespace(model_type=model_type)]
        self._healthy = [object()] * healthy

    def healthy(self) -> list[object]:
        return self._healthy


class FakeRouter:
    def __init__(self, pools: dict[str, _FakePool]) -> None:
        self._pools = pools

    def models(self) -> list[str]:
        return sorted(self._pools)

    def pool(self, name: str) -> _FakePool | None:
        return self._pools.get(name)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> FakeRouter:
    fake = FakeRouter({"glm": _FakePool(), "qwen": _FakePool(), "embed": _FakePool("embedding")})
    monkeypatch.setattr(routes_admin, "model_router", fake)
    return fake


def _tenant(db: FakeDB, name: str = "acme") -> uuid.UUID:
    tenant_id = uuid.uuid4()
    db.tenants[tenant_id] = {
        "id": tenant_id,
        "name": name,
        "created_at": "2026-11-08T00:00:00+00:00",
        "sso_default_profile_id": None,
        "sso_admin_profile_id": None,
        "sso_admin_groups": "",
    }
    return tenant_id


def _profile(db: FakeDB, name: str = "power", budgets: list[dict[str, Any]] | None = None) -> uuid.UUID:
    profile_id = uuid.uuid4()
    db.profiles[profile_id] = {
        "id": profile_id,
        "name": name,
        "defaults_json": json.dumps({"budgets": budgets if budgets is not None else [], "mcp_grants": []}),
    }
    return profile_id


def _group(db: FakeDB, name: str, members: list[str], **extra: Any) -> None:
    row = {
        "name": name,
        "model_type": "chat",
        "policy": "load",
        "members": json.dumps([{"model": m, "weight": 1.0} for m in members]),
        "description": "",
        "created_at": "2026-11-08T00:00:00+00:00",
    }
    row.update(extra)
    db.groups[name] = row


# ---------------------------------------------------------------------------
# F1: GET /admin/tenants carries the tier pair
# ---------------------------------------------------------------------------


def test_list_tenants_exposes_the_tier_fields(client) -> None:
    tc, db = client
    tenant_id = _tenant(db)
    admin_profile_id = _profile(db, name="admin-power")
    db.tenants[tenant_id].update({"sso_admin_profile_id": admin_profile_id, "sso_admin_groups": "admin,ops"})

    r = tc.get("/admin/tenants", headers=_h())
    assert r.status_code == 200, r.text
    row = r.json()[0]
    # the pre-existing fields are untouched
    assert row["name"] == "acme"
    assert "sso_default_profile_name" in row
    assert row["sso_admin_profile_id"] == str(admin_profile_id)
    assert row["sso_admin_profile_name"] == "admin-power"
    assert row["sso_admin_groups"] == "admin,ops"
    sql = db.fetches[-1][0]
    assert "LEFT JOIN profiles p ON p.id = t.sso_default_profile_id" in sql
    assert "LEFT JOIN profiles ap ON ap.id = t.sso_admin_profile_id" in sql


# ---------------------------------------------------------------------------
# F1: PUT /admin/tenants/{id}/sso-default-profile
# ---------------------------------------------------------------------------


def test_put_sso_profile_id_only_is_backward_compatible(client, audit_log: list[dict[str, Any]]) -> None:
    """The pre-tier body must behave exactly as before: profile_id applied,
    the tier columns NOT in the statement, the same audit action."""
    tc, db = client
    tenant_id = _tenant(db)
    profile_id = _profile(db)

    r = tc.put(f"/admin/tenants/{tenant_id}/sso-default-profile", json={"profile_id": str(profile_id)}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["sso_default_profile_id"] == str(profile_id)
    sql, args = db.fetchrows[-1]
    # only (tenant_id, profile_id) are bound: the tier columns are not in the
    # SET clause (they are still in RETURNING, which is the response shape)
    assert len(args) == 2
    assert "sso_admin_profile_id = $" not in sql
    assert "sso_admin_groups = $" not in sql
    assert db.tenants[tenant_id]["sso_admin_groups"] == ""
    call = audit_log[-1]
    assert call["action"] == "tenant.sso_default_profile.set"
    assert call["details"] == {"profile_id": str(profile_id), "admin_profile_id": None, "admin_groups": ""}


def test_put_sso_sets_admin_profile_and_normalizes_groups(client, audit_log: list[dict[str, Any]]) -> None:
    tc, db = client
    tenant_id = _tenant(db)
    profile_id = _profile(db, name="member-power")
    admin_profile_id = _profile(db, name="admin-power")

    r = tc.put(
        f"/admin/tenants/{tenant_id}/sso-default-profile",
        json={
            "profile_id": str(profile_id),
            "admin_profile_id": str(admin_profile_id),
            "admin_groups": " admin , ops,admin,,  ",
        },
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sso_default_profile_id"] == str(profile_id)
    assert body["sso_admin_profile_id"] == str(admin_profile_id)
    # trimmed, empties dropped, duplicates collapsed (order preserved)
    assert body["sso_admin_groups"] == "admin,ops"
    assert db.tenants[tenant_id]["sso_admin_groups"] == "admin,ops"
    assert audit_log[-1]["details"]["admin_groups"] == "admin,ops"
    assert audit_log[-1]["details"]["admin_profile_id"] == str(admin_profile_id)


def test_put_sso_admin_profile_unknown_is_404(client) -> None:
    tc, db = client
    tenant_id = _tenant(db)
    r = tc.put(
        f"/admin/tenants/{tenant_id}/sso-default-profile",
        json={"admin_profile_id": str(uuid.uuid4())},
        headers=_h(),
    )
    assert r.status_code == 404, r.text
    assert "not found" in r.json()["detail"]
    assert db.tenants[tenant_id]["sso_admin_profile_id"] is None


def test_put_sso_admin_profile_invalid_preset_is_400(client) -> None:
    """The same wire-time preset gate the default slot has: a bad preset here
    would break every ADMIN mint/rotate instead, long after the operator left
    the screen."""
    tc, db = client
    tenant_id = _tenant(db)
    admin_profile_id = _profile(db, name="admin-power", budgets=[{"model": "*", "period": "month"}])
    r = tc.put(
        f"/admin/tenants/{tenant_id}/sso-default-profile",
        json={"admin_profile_id": str(admin_profile_id)},
        headers=_h(),
    )
    assert r.status_code == 400, r.text
    assert "cannot be the SSO admin profile" in r.json()["detail"]
    assert "invalid budget preset" in r.json()["detail"]


def test_put_sso_admin_groups_rejects_overlong_and_control_chars(client) -> None:
    tc, db = client
    tenant_id = _tenant(db)
    url = f"/admin/tenants/{tenant_id}/sso-default-profile"
    over = "g" * (routes_admin._ADMIN_GROUP_MAX_LEN + 1)
    r = tc.put(url, json={"admin_groups": over}, headers=_h())
    assert r.status_code == 400, r.text
    assert "exceeds" in r.json()["detail"]
    r2 = tc.put(url, json={"admin_groups": "adm\nin"}, headers=_h())
    assert r2.status_code == 400, r2.text
    assert "control character" in r2.json()["detail"]
    assert db.tenants[tenant_id]["sso_admin_groups"] == ""


def test_put_sso_admin_profile_null_clears_it(client) -> None:
    """An explicit null on the tier profile is "no admin tier", not "leave it"."""
    tc, db = client
    tenant_id = _tenant(db)
    admin_profile_id = _profile(db, name="admin-power")
    db.tenants[tenant_id]["sso_admin_profile_id"] = admin_profile_id
    r = tc.put(
        f"/admin/tenants/{tenant_id}/sso-default-profile",
        json={"admin_profile_id": None},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["sso_admin_profile_id"] is None
    assert db.tenants[tenant_id]["sso_admin_profile_id"] is None


def test_put_sso_admin_groups_null_clears_the_split(client) -> None:
    """Explicit null is the documented "tier split off": the sentinel is '',
    never a string of commas."""
    tc, db = client
    tenant_id = _tenant(db)
    db.tenants[tenant_id]["sso_admin_groups"] = "admin"
    r = tc.put(
        f"/admin/tenants/{tenant_id}/sso-default-profile",
        json={"admin_groups": None},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["sso_admin_groups"] == ""
    assert db.tenants[tenant_id]["sso_admin_groups"] == ""


def test_put_sso_omitted_admin_fields_are_not_cleared(client) -> None:
    """A body that predates the tier split must not WIPE a configured tier —
    the presence check, not the None default, decides."""
    tc, db = client
    tenant_id = _tenant(db)
    admin_profile_id = _profile(db, name="admin-power")
    db.tenants[tenant_id].update({"sso_admin_profile_id": admin_profile_id, "sso_admin_groups": "admin"})

    r = tc.put(f"/admin/tenants/{tenant_id}/sso-default-profile", json={"profile_id": None}, headers=_h())
    assert r.status_code == 200, r.text
    assert db.tenants[tenant_id]["sso_admin_profile_id"] == admin_profile_id
    assert db.tenants[tenant_id]["sso_admin_groups"] == "admin"


def test_sso_put_is_admin_gated(client) -> None:
    tc, db = client
    tenant_id = _tenant(db)
    r = tc.put(f"/admin/tenants/{tenant_id}/sso-default-profile", json={"admin_groups": "admin"})
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# F3: model-group curation
# ---------------------------------------------------------------------------


def test_list_model_groups_parses_jsonb_members(client) -> None:
    tc, db = client
    _group(db, "fast", ["glm", "qwen"])
    # a hand-edited row with bare names must degrade, not 500
    db.groups["legacy"] = {
        "name": "legacy",
        "model_type": "chat",
        "policy": "load",
        "members": json.dumps(["glm"]),
        "description": "",
        "created_at": "2026-11-08T00:00:00+00:00",
    }

    r = tc.get("/admin/model-groups", headers=_h())
    assert r.status_code == 200, r.text
    groups = {g["name"]: g for g in r.json()["groups"]}
    assert groups["fast"]["members"] == ["glm", "qwen"]
    assert groups["legacy"]["members"] == ["glm"]
    assert groups["fast"]["model_type"] == "chat"
    assert groups["fast"]["policy"] == "load"
    assert "created_at" in groups["fast"]


def test_put_group_delegates_member_validation(client, groups_core: Any, router: FakeRouter) -> None:
    """The core validator's reason IS the 400 detail — the operator must read
    the same sentence the data plane would enforce."""
    tc, db = client
    groups_core.validate_members = lambda members, all_models, groups: "member 'ghost' is not a registered model"
    r = tc.put("/admin/model-groups/fast", json={"members": ["ghost"]}, headers=_h())
    assert r.status_code == 400, r.text
    assert "not a registered model" in r.json()["detail"]
    assert db.groups == {}, "a refused member set must never reach the table"


def test_put_group_rejects_a_non_chat_member(client, groups_core: Any, router: FakeRouter) -> None:
    """The core validator has no registry types, so the chat-only rule lives
    here: a group that could pick an embedding pool is unroutable."""
    tc, db = client
    r = tc.put("/admin/model-groups/fast", json={"members": ["glm", "embed"]}, headers=_h())
    assert r.status_code == 400, r.text
    assert "must be chat models" in r.json()["detail"]
    assert "embed (embedding)" in r.json()["detail"]
    assert db.groups == {}


def test_put_group_rejects_reserved_and_registered_names(client, groups_core: Any, router: FakeRouter) -> None:
    tc, db = client
    r = tc.put("/admin/model-groups/auto", json={"members": ["glm"]}, headers=_h())
    assert r.status_code == 400, r.text
    assert "reserved cascade alias" in r.json()["detail"]
    r2 = tc.put("/admin/model-groups/glm", json={"members": ["qwen"]}, headers=_h())
    assert r2.status_code == 400, r2.text
    assert "already a registered model" in r2.json()["detail"]
    assert db.groups == {}


def test_put_group_rejects_bad_type_and_policy(client, groups_core: Any, router: FakeRouter) -> None:
    tc, _db = client
    r = tc.put("/admin/model-groups/fast", json={"members": ["glm"], "policy": "weighted"}, headers=_h())
    assert r.status_code == 400 and "not implemented" in r.json()["detail"]
    r2 = tc.put("/admin/model-groups/fast", json={"members": ["glm"], "model_type": "embedding"}, headers=_h())
    assert r2.status_code == 400 and "chat-only" in r2.json()["detail"]


def test_put_group_stores_weighted_members_and_invalidates(
    client, groups_core: Any, router: FakeRouter, audit_log: list[dict[str, Any]]
) -> None:
    tc, db = client
    r = tc.put(
        "/admin/model-groups/fast",
        json={"members": ["glm", "qwen"], "description": "the fast tier"},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["members"] == ["glm", "qwen"]
    assert body["description"] == "the fast tier"
    sql, args = db.fetchrows[-1]
    assert "INSERT INTO model_groups" in sql and "$4::jsonb" in sql
    assert "ON CONFLICT (name) DO UPDATE" in sql
    # stored in the documented [{model, weight}] shape, weight 1.0 (reserved)
    assert json.loads(args[3]) == [{"model": "glm", "weight": 1.0}, {"model": "qwen", "weight": 1.0}]
    # the routing cache is dropped AFTER the commit
    assert groups_core.invalidated == [True]
    assert audit_log[-1]["action"] == "model_group.create"
    assert audit_log[-1]["resource_id"] == "fast"
    assert audit_log[-1]["details"]["members"] == ["glm", "qwen"]


def test_put_group_trims_member_names(client, groups_core: Any, router: FakeRouter) -> None:
    """Names are stored trimmed: the validator trims before it checks, so a
    padded name must not reach the column (it would never match the registry)."""
    tc, db = client
    r = tc.put("/admin/model-groups/fast", json={"members": [" glm ", "", "qwen"]}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["members"] == ["glm", "qwen"]
    stored = json.loads(db.fetchrows[-1][1][3])
    assert stored == [{"model": "glm", "weight": 1.0}, {"model": "qwen", "weight": 1.0}]


def test_put_existing_group_audits_update(
    client, groups_core: Any, router: FakeRouter, audit_log: list[dict[str, Any]]
) -> None:
    tc, db = client
    _group(db, "fast", ["glm"])
    r = tc.put("/admin/model-groups/fast", json={"members": ["qwen"]}, headers=_h())
    assert r.status_code == 200, r.text
    assert audit_log[-1]["action"] == "model_group.update"
    assert db.groups["fast"]["members"] == json.dumps([{"model": "qwen", "weight": 1.0}])


def test_delete_group_404_then_success(client, groups_core: Any, audit_log: list[dict[str, Any]]) -> None:
    tc, db = client
    r = tc.delete("/admin/model-groups/ghost", headers=_h())
    assert r.status_code == 404, r.text
    assert groups_core.invalidated == [], "a 404 must not touch the routing cache"

    _group(db, "fast", ["glm"])
    r2 = tc.delete("/admin/model-groups/fast", headers=_h())
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"deleted": True, "name": "fast"}
    assert "fast" not in db.groups
    assert groups_core.invalidated == [True]
    assert audit_log[-1]["action"] == "model_group.delete"


def test_preview_reports_scores_and_never_pins(
    client, groups_core: Any, load_snapshot_core: Any, router: FakeRouter
) -> None:
    tc, db = client
    _group(db, "fast", ["glm", "qwen"])
    groups_core.group_for = lambda name: types.SimpleNamespace(name="fast", members=("glm", "qwen"))
    groups_core.pick_member = lambda group, allowed, score: "qwen"
    load_snapshot_core.scores = {"glm": 4.0, "qwen": 1.5}

    r = tc.post("/admin/model-groups/fast/preview", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["member"] == "qwen"
    assert body["candidates"] == [
        {"model": "glm", "score": 4.0, "healthy": True},
        {"model": "qwen", "score": 1.5, "healthy": True},
    ]
    # read-only: no cache churn, no write, no pin
    assert groups_core.invalidated == []
    assert groups_core.loaded == [False]
    assert not [sql for sql, _ in db.fetchrows if "INSERT" in sql or "UPDATE" in sql]
    assert db.executed == []


def test_preview_404s_an_unknown_group(client, groups_core: Any, load_snapshot_core: Any) -> None:
    tc, _db = client
    r = tc.post("/admin/model-groups/ghost/preview", headers=_h())
    assert r.status_code == 404, r.text
    assert "not found" in r.json()["detail"]


def test_group_routes_are_admin_gated(client) -> None:
    tc, _db = client
    assert tc.get("/admin/model-groups").status_code == 401
    assert tc.put("/admin/model-groups/fast", json={"members": ["glm"]}).status_code == 401
    assert tc.delete("/admin/model-groups/fast").status_code == 401
    assert tc.post("/admin/model-groups/fast/preview").status_code == 401


# ---------------------------------------------------------------------------
# F3: the same routes against the REAL core.model_groups (the admin API must
# speak the module's actual contract, not just the stub's)
# ---------------------------------------------------------------------------


def test_put_group_uses_the_real_member_validator(client, router: FakeRouter) -> None:
    """No stub: the route's 400 IS core.model_groups.validate_members' reason,
    so the operator reads the sentence the data plane enforces."""
    tc, db = client
    r = tc.put("/admin/model-groups/fast", json={"members": []}, headers=_h())
    assert r.status_code == 400, r.text
    assert "at least one member" in r.json()["detail"]
    r2 = tc.put("/admin/model-groups/fast", json={"members": ["ghost"]}, headers=_h())
    assert r2.status_code == 400, r2.text
    assert "unknown model: ghost" in r2.json()["detail"]
    assert db.groups == {}, "a refused member set must never reach the table"


def test_put_group_refuses_a_member_that_is_a_group(
    client, router: FakeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ONE LEVEL guarantee (db/schema.py) is enforced at the WRITE path:
    a member naming another group can never be stored, so the pick can never
    recurse."""
    from pcai_llm.core import model_groups as real_groups

    tc, db = client
    monkeypatch.setattr(
        real_groups,
        "snapshot",
        lambda: {"fast": real_groups.ModelGroup(name="fast", model_type="chat", policy="load", members=("glm",))},
    )
    r = tc.put("/admin/model-groups/other", json={"members": ["glm", "fast"]}, headers=_h())
    assert r.status_code == 400, r.text
    assert "is a group" in r.json()["detail"]
    assert "other" not in db.groups


# ---------------------------------------------------------------------------
# F3: the admin "presented models" view lists groups (config truth)
# ---------------------------------------------------------------------------


def test_presented_models_includes_groups_when_enabled(
    client, groups_core: Any, router: FakeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc, _db = client
    monkeypatch.setattr(settings, "model_groups_enabled", True)
    groups_core.load = _groups_loader(
        {
            "fast": types.SimpleNamespace(
                name="fast", model_type="chat", policy="load", members=("glm", "qwen"), description="d"
            )
        }
    )
    r = tc.get("/admin/presented-models", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert [g["id"] for g in body["groups"]] == ["fast"]
    group = body["groups"][0]
    assert group["type"] == "group"
    assert group["owned_by"] == "group"
    assert group["members"] == ["glm", "qwen"]
    assert group["present"] is True
    assert body["env"]["model_groups_enabled"] is True
    # a present group counts toward the presented total, like an agent/alias
    assert body["counts"]["presented"] >= 1


def test_presented_models_omits_groups_when_disabled(
    client, groups_core: Any, router: FakeRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    tc, _db = client
    monkeypatch.setattr(settings, "model_groups_enabled", False)
    r = tc.get("/admin/presented-models", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["groups"] == []
    assert groups_core.loaded == [], "the loader is not consulted while the fleet switch is off"


def _groups_loader(groups: dict[str, Any]):
    async def _load(force: bool = False) -> dict[str, Any]:
        return groups

    return _load


# ---------------------------------------------------------------------------
# Routing composability: groups are first-class routing candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_candidates_include_chat_groups_only(groups_core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The routing panel's checkbox source (and routing__<X> advertisement)
    lists chat GROUPS alongside chat pools; a non-chat group is never a chat
    rung."""
    monkeypatch.setattr(settings, "model_groups_enabled", True)
    groups_core.snapshot = lambda: {
        "grp-chat": types.SimpleNamespace(name="grp-chat", model_type="chat"),
        "grp-embed": types.SimpleNamespace(name="grp-embed", model_type="embedding"),
    }
    got = await routes_admin._routing_chat_models()
    assert "grp-chat" in got
    assert "grp-embed" not in got


# ---------------------------------------------------------------------------
# Fallback-chain composability: a chain entry may name a chat model group
# ---------------------------------------------------------------------------


def test_fallback_chain_accepts_a_chat_model_group(client, groups_core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-model fallback chain can name a group: the data plane's walker
    resolves it to a member (routes_proxy._next_fallback_candidate), so the
    save path must accept it too — otherwise the composability is unreachable
    through the admin API.

    The validator is gated on settings.model_groups_enabled (audit 2026-11-10
    P2-10), so the flag is on here — with it off, group names are correctly
    rejected as unknown."""
    monkeypatch.setattr(settings, "model_groups_enabled", True)
    tc, db = client
    db.models = {"alpha"}
    groups_core.snapshot = lambda: {"grp-chat": types.SimpleNamespace(model_type="chat")}
    r = tc.put("/admin/models/alpha/fallbacks", json={"fallbacks": ["grp-chat"]}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["fallbacks"] == ["grp-chat"]


def test_fallback_chain_rejects_a_non_chat_group(client, groups_core: Any) -> None:
    """A non-chat group would be skipped by the walker — saving it is dead
    config, so it is refused like an unknown name."""
    tc, db = client
    db.models = {"alpha"}
    groups_core.snapshot = lambda: {"grp-embed": types.SimpleNamespace(model_type="embedding")}
    r = tc.put("/admin/models/alpha/fallbacks", json={"fallbacks": ["grp-embed"]}, headers=_h())
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Review-fix regressions (2026-11-08): F1-2, F3-1, F3-2
# ---------------------------------------------------------------------------


def test_put_sso_tier_only_body_does_not_clear_the_default(client) -> None:
    """F1-2: the apply-only-when-present rule is SYMMETRIC — a partial
    admin-tier save must not NULL the tenant's default profile."""
    tc, db = client
    tenant_id = _tenant(db)
    profile_id = _profile(db, name="member-default")
    db.tenants[tenant_id].update({"sso_default_profile_id": profile_id})

    r = tc.put(
        f"/admin/tenants/{tenant_id}/sso-default-profile",
        json={"admin_groups": "admin"},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert db.tenants[tenant_id]["sso_default_profile_id"] == profile_id
    assert db.tenants[tenant_id]["sso_admin_groups"] == "admin"


def test_put_sso_empty_body_is_400(client) -> None:
    """An empty body is a no-op UPDATE otherwise — refuse it explicitly."""
    tc, db = client
    tenant_id = _tenant(db)
    r = tc.put(f"/admin/tenants/{tenant_id}/sso-default-profile", json={}, headers=_h())
    assert r.status_code == 400, r.text


def test_group_member_with_chat_not_first_is_accepted(client, router: FakeRouter) -> None:
    """F3-1: ANY chat endpoint makes the pool chat-servable — the data plane's
    serves_chat() is an any() predicate, not endpoints[0]."""
    tc, _db = client

    class _MixedPool:
        def __init__(self) -> None:
            self.endpoints = [
                types.SimpleNamespace(model_type="embedding"),
                types.SimpleNamespace(model_type="chat"),
            ]

        def healthy(self) -> list[object]:
            return [object()]

    router._pools["mixed"] = _MixedPool()
    r = tc.put("/admin/model-groups/g1", json={"members": ["mixed"]}, headers=_h())
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("name", ["routing", "routing__glm"])
def test_group_name_reserved_routing_pseudo_model(client, name: str) -> None:
    """F3-2: detect_pseudo_model claims these names before any group lookup, so
    a group saved under one would be stored and unaddressable."""
    tc, _db = client
    r = tc.put(f"/admin/model-groups/{name}", json={"members": ["glm"]}, headers=_h())
    assert r.status_code == 400, r.text


def test_model_upsert_refuses_a_group_name(
    client, router: FakeRouter, groups_core: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3-2: a pool registered under an existing group's name would be
    silently unaddressable — the write path refuses it."""
    tc, _db = client
    # audit 2026-11-10 P2-10: the write-path group guards are gated on
    # settings.model_groups_enabled (default False), like every data-plane
    # consumer — enable it so the guard is actually exercised.
    monkeypatch.setattr(settings, "model_groups_enabled", True)
    groups_core.group_for = lambda name: object() if name == "grp" else None

    async def _noop_check(urls: list[str], what: str) -> None:
        return None

    monkeypatch.setattr(routes_admin, "_check_url_async", _noop_check)
    r = tc.post(
        "/admin/models",
        json={
            "model_name": "grp",
            "model_type": "chat",
            "endpoints": [{"base_url": "http://model.example/v1"}],
        },
        headers=_h(),
    )
    assert r.status_code == 400, r.text
    assert "already a model group" in r.text
