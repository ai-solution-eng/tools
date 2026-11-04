"""Enrollment team-admin tests: the budget envelope + team-leader key admin
surface (fake-db style — the test_enroll.py seam pattern).

Pinned here, per the contract:
- POST /admin/enrollments validates the envelope (period in
  core.teams.TEAM_PERIODS -> 400 otherwise; negative/zero/non-finite budgets
  rejected) and, with with_team_leader=True, returns the leader key
  plaintext ONCE;
- GET /admin/enrollments is BACKWARD COMPATIBLE: only ADDS keys per row
  (budget_*, overflow_enabled, team_leader_key_*, key_count, spend_tokens,
  allocated_tokens) — an old console keeps working;
- PATCH /admin/enrollments/{id} edits the envelope and invalidates the
  gate's enrollment lookups;
- team-leader rotate returns a NEW prefix; revoke is idempotent;
- DELETE /admin/enrollments/{id} forgets a REVOKED row (active = 409) —
  minted keys keep working, the FK hands their enrollment_id NULL;
- the redeem stamps api_keys.enrollment_id (the team membership write);
- the admin keys list forwards the enrollment_id / label-substring filters;
- presented_models_payload() is the single payload source for the admin
  route (the refactor is behavior-preserving).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_enroll
from pcai_llm.config import settings

ADMIN_H = {"Authorization": "Bearer admin-secret-123"}


class _FakeConn:
    """Acquired-connection stand-in (the test_enroll.py shape)."""

    def __init__(self, db: EnrollTeamFakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def executemany(self, sql: str, args: Any):
        self._db.bulk.append((sql, list(args)))

    async def fetchrow(self, sql: str, *args: Any):
        return await self._db.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any):
        return await self._db.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)


class EnrollTeamFakeDB:
    """Stateful fake for the enroll-admin team surface (dispatch on SQL
    shape; records every statement the tests assert on)."""

    def __init__(self) -> None:
        self.fetchrows: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []
        self.bulk: list[tuple[str, list]] = []
        self.enrollments: list[dict[str, Any]] = []
        self.tenant_exists = True
        self.profile_exists = True
        self.enrollment_exists = True
        # the atomic redeem UPDATE's row; None = dead code
        self.enroll_row: dict[str, Any] | None = None
        self.tenant_name: str | None = "acme"
        self.patch_row: dict[str, Any] | None = None
        # THREAD B: the top-up queue (admin side)
        self.topup_row: dict[str, Any] | None = None  # _topup_or_404's read
        self.topup_resolved: dict[str, Any] | None = None  # conditional UPDATE RETURNING
        self.enroll_caps_row: dict[str, Any] | None = None  # caps read + RETURNING
        self.admin_topup_rows: list[dict[str, Any]] = []
        # DELETE /admin/enrollments/{id}: the row's label+revoked_at read, and
        # how many keys still reference it (audit context, not a blocker).
        self.enroll_delete_row: dict[str, Any] | None = None
        self.enroll_key_count: int = 0

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrows.append((sql, args))
        if "SELECT label, revoked_at FROM enrollments" in sql:
            return self.enroll_delete_row
        if "UPDATE enrollments SET uses" in sql:
            return self.enroll_row
        if "INSERT INTO users" in sql:
            return {"id": uuid.uuid4(), "name": args[1]}
        if "INSERT INTO api_keys" in sql:
            return {"id": uuid.uuid4()}
        if "INSERT INTO enrollments" in sql:
            return {
                "id": uuid.uuid4(),
                "label": args[1],
                "tenant_id": args[2],
                "profile_id": args[3],
                "max_uses": args[4],
                "uses": 0,
                "expires_at": args[5],
                "revoked_at": None,
                "created_at": "now",
            }
        if "UPDATE enrollments SET" in sql and "CASE WHEN" in sql:
            # the approve's IN-SQL cap addition (RETURNING = resulting caps)
            return self.enroll_caps_row
        if "SELECT budget_tokens, budget_usd, budget_period FROM enrollments" in sql:
            return self.enroll_caps_row
        if "FROM enrollment_topup_requests WHERE id" in sql:
            return self.topup_row
        if "UPDATE enrollment_topup_requests" in sql:
            # the conditional resolve: None = lost the race
            return self.topup_resolved
        if "UPDATE enrollments SET" in sql:
            return self.patch_row
        return None

    async def fetchval(self, sql: str, *args: Any):
        if "count(*) FROM api_keys" in sql:
            return self.enroll_key_count
        if "FROM tenants" in sql:
            if "SELECT 1" in sql:
                return 1 if self.tenant_exists else None
            return self.tenant_name
        if "FROM enrollments" in sql:
            return 1 if self.enrollment_exists else None
        if "FROM profiles" in sql:
            return 1 if self.profile_exists else None
        return 1

    async def fetch(self, sql: str, *args: Any):
        self.fetch_sqls: list[str] = getattr(self, "fetch_sqls", [])
        self.fetch_sqls.append(sql)
        self.fetch_args: list[tuple] = getattr(self, "fetch_args", [])
        self.fetch_args.append(args)
        if "FROM enrollment_topup_requests r" in sql:
            return list(self.admin_topup_rows)
        if "FROM enrollments e" in sql:
            return list(self.enrollments)
        return []

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        return "UPDATE 1"

    def acquire(self):
        return _FakeConn(self)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    fake = EnrollTeamFakeDB()
    import pcai_llm.core.keys as keys_mod
    import pcai_llm.core.teams as teams_mod

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)  # create_key + authenticate seams
    # the core.teams surface is FAKED at the module boundary (its own
    # internals are test_teams_core.py's concern): mint/rotate/revoke
    # record their enrollment id, spend/allocated answer fixed numbers.
    teams_state: dict[str, Any] = {"mints": [], "revokes": [], "invalidated": []}

    async def fake_mint(enrollment_id: uuid.UUID, *, conn: Any = None):
        teams_state["mints"].append(enrollment_id)
        return ("hllm-team-leader-PLAINTEXT", "hllm-team-")

    async def fake_revoke(enrollment_id: uuid.UUID, *, conn: Any = None):
        teams_state["revokes"].append(enrollment_id)
        return True

    async def fake_spend(eid: uuid.UUID, period: str):
        return (11, 0.5)

    async def fake_alloc(eid: uuid.UUID, period: str):
        return (7, 0.0)

    async def fake_cap_spend(eid: uuid.UUID, scopes: list[tuple[str, str]]):
        # the listing attaches per-cap spend (2026-10-30); the fixture fakes the
        # team helpers at the module boundary, so this one rides along
        return {(model, period): (0, 0.0) for model, period in scopes}

    def fake_invalidate(eid: Any = None) -> None:
        teams_state["invalidated"].append(eid)

    monkeypatch.setattr(teams_mod, "mint_leader_key", fake_mint)
    monkeypatch.setattr(teams_mod, "revoke_leader_key", fake_revoke)
    monkeypatch.setattr(teams_mod, "team_spend", fake_spend)
    monkeypatch.setattr(teams_mod, "team_allocated", fake_alloc)
    monkeypatch.setattr(teams_mod, "team_cap_spend", fake_cap_spend)
    monkeypatch.setattr(teams_mod, "invalidate", fake_invalidate)
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_enroll.router)
    app.include_router(routes_enroll.admin_router)
    # THREAD B: the top-up QUEUE endpoints live on the admin router
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    tc.fake = fake  # type: ignore[attr-defined]
    tc.teams_state = teams_state  # type: ignore[attr-defined]
    # audit recorder for the ADMIN routes (routes_admin._audit; the enroll
    # routes' own _enroll_audit is fail-soft and asserted nowhere here)
    admin_audit_calls: list[dict[str, Any]] = []

    async def record_admin_audit(request: Any, **kw: Any) -> None:
        admin_audit_calls.append(kw)

    monkeypatch.setattr(routes_admin, "_audit", record_admin_audit)
    tc.admin_audit_calls = admin_audit_calls  # type: ignore[attr-defined]
    return tc


# ---------------------------------------------------------------------------
# POST /admin/enrollments — the envelope + the leader mint
# ---------------------------------------------------------------------------


def test_create_enrollment_with_team_leader_key(env) -> None:
    r = env.post(
        "/admin/enrollments",
        json={
            "label": "station-9",
            "tenant_id": str(uuid.uuid4()),
            "max_uses": 4,
            "budget_tokens": 10_000_000,
            "budget_usd": 50.0,
            "budget_period": "month",
            "overflow_enabled": True,
            "with_team_leader": True,
        },
        headers=ADMIN_H,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["team_leader_key"] == "hllm-team-leader-PLAINTEXT"
    assert body["team_leader_key_prefix"] == "hllm-team-"
    # the mint was FOR this enrollment (fake row id is a UUID; the JSON
    # body renders it as a string)
    assert [str(m) for m in env.teams_state["mints"]] == [body["id"]]  # type: ignore[attr-defined]
    # the envelope rode the INSERT
    insert_args = next(a for s, a in env.fake.fetchrows if "INSERT INTO enrollments" in s)  # type: ignore[attr-defined]
    assert insert_args[6] == 10_000_000
    assert insert_args[7] == 50.0
    assert insert_args[8] == "month"
    assert insert_args[9] is True


def test_create_enrollment_without_leader_key_has_no_field(env) -> None:
    """Backward compat: without with_team_leader the response has no
    team_leader_key at all (the pre-feature shape)."""
    r = env.post(
        "/admin/enrollments",
        json={"label": "l", "tenant_id": str(uuid.uuid4()), "max_uses": 4},
        headers=ADMIN_H,
    )
    assert r.status_code == 201
    assert "team_leader_key" not in r.json()
    assert env.teams_state["mints"] == []  # type: ignore[attr-defined]


def test_create_enrollment_bad_period_400(env) -> None:
    r = env.post(
        "/admin/enrollments",
        json={"label": "l", "tenant_id": str(uuid.uuid4()), "max_uses": 4, "budget_period": "hour"},
        headers=ADMIN_H,
    )
    assert r.status_code == 400
    assert "budget_period" in r.json()["detail"]


def test_create_enrollment_nonpositive_budgets_rejected(env) -> None:
    tid = str(uuid.uuid4())
    for bad in ({"budget_tokens": 0}, {"budget_tokens": -5}, {"budget_usd": 0}, {"budget_usd": -1.5}):
        r = env.post("/admin/enrollments", json={"label": "l", "tenant_id": tid, **bad}, headers=ADMIN_H)
        assert r.status_code == 422, bad
    # a value the COLUMN cannot hold is refused at the API (the same
    # discipline core.budgets applies to budget rows: a limit above the
    # column bound would otherwise be a NumericValueOutOfRange 500 on a
    # mint whose contract is 422)
    r = env.post(
        "/admin/enrollments",
        json={"label": "l", "tenant_id": tid, "budget_usd": 200_000_000.0},
        headers=ADMIN_H,
    )
    assert r.status_code == 422
    r = env.post(
        "/admin/enrollments",
        json={"label": "l", "tenant_id": tid, "budget_tokens": 2**63},
        headers=ADMIN_H,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /admin/enrollments — additive columns only
# ---------------------------------------------------------------------------


def test_list_enrollments_adds_team_fields_without_renaming(env) -> None:
    """An OLD-shape row (exactly the columns the pre-team projection had)
    gains the new keys; nothing is renamed or removed, and no code material
    appears."""
    eid = uuid.uuid4()
    env.fake.enrollments = [  # type: ignore[attr-defined]
        {
            "id": eid,
            "label": "onboarding",
            "tenant_name": "acme",
            "profile_name": "starter",
            "max_uses": 0,
            "uses": 3,
            "expires_at": None,
            "revoked_at": None,
            "created_at": "t",
        }
    ]
    r = env.get("/admin/enrollments", headers=ADMIN_H)
    assert r.status_code == 200
    row = r.json()[0]
    # the OLD keys are all still there, unchanged
    for key in (
        "id",
        "label",
        "tenant_name",
        "profile_name",
        "max_uses",
        "uses",
        "expires_at",
        "revoked_at",
        "created_at",
    ):
        assert key in row, key
    assert row["tenant_name"] == "acme" and row["profile_name"] == "starter"
    assert row["uses"] == 3 and row["max_uses"] == 0
    assert "code" not in row and "code_hash" not in row
    # the NEW keys are additive
    for key in (
        "budget_tokens",
        "budget_usd",
        "budget_period",
        "overflow_enabled",
        "team_leader_key_prefix",
        "team_leader_key_created_at",
        "team_leader_key_revoked_at",
        "team_leader_key_last_used_at",
        "key_count",
        "spend_tokens",
        "allocated_tokens",
    ):
        assert key in row, key
    assert row["key_count"] == 0
    assert row["team_leader_key_last_used_at"] is None  # old-shape row degrades, never KeyErrors
    assert row["spend_tokens"] == 11 and row["allocated_tokens"] == 7


# ---------------------------------------------------------------------------
# PATCH /admin/enrollments/{id} — envelope editing
# ---------------------------------------------------------------------------


def test_patch_enrollment_updates_envelope(env) -> None:
    env.fake.patch_row = {  # type: ignore[attr-defined]
        "id": "x",
        "label": "l",
        "max_uses": 5,
        "expires_at": None,
        "budget_tokens": 5000,
        "budget_usd": None,
        "budget_period": "month",
        "overflow_enabled": False,
    }
    eid = uuid.uuid4()
    r = env.patch(
        f"/admin/enrollments/{eid}",
        json={"budget_tokens": 5000, "overflow_enabled": False, "expires_at": None},
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["budget_tokens"] == 5000
    assert r.json()["overflow_enabled"] is False
    # the gate's cached enrollment lookups were invalidated FOR this team
    assert env.teams_state["invalidated"] == [eid]  # type: ignore[attr-defined]


def test_patch_enrollment_bad_period_400(env) -> None:
    r = env.patch(f"/admin/enrollments/{uuid.uuid4()}", json={"budget_period": "hour"}, headers=ADMIN_H)
    assert r.status_code == 400


def test_patch_enrollment_unknown_404_and_empty_400(env) -> None:
    env.fake.enrollment_exists = False  # type: ignore[attr-defined]
    assert env.patch(f"/admin/enrollments/{uuid.uuid4()}", json={"max_uses": 1}, headers=ADMIN_H).status_code == 404
    env.fake.enrollment_exists = True  # type: ignore[attr-defined]
    assert env.patch(f"/admin/enrollments/{uuid.uuid4()}", json={}, headers=ADMIN_H).status_code == 400


# ---------------------------------------------------------------------------
# team-leader rotate / revoke (admin)
# ---------------------------------------------------------------------------


def test_rotate_team_leader_returns_new_plaintext(env) -> None:
    eid = uuid.uuid4()
    r1 = env.post(f"/admin/enrollments/{eid}/team-leader/rotate", headers=ADMIN_H)
    assert r1.status_code == 201, r1.text
    body = r1.json()
    assert body["team_leader_key"] == "hllm-team-leader-PLAINTEXT"
    assert body["team_leader_key_prefix"] == "hllm-team-"
    assert env.teams_state["mints"] == [eid]  # type: ignore[attr-defined]
    # a SECOND rotate re-mints (prefix changes per mint)
    r2 = env.post(f"/admin/enrollments/{eid}/team-leader/rotate", headers=ADMIN_H)
    assert r2.status_code == 201
    assert len(env.teams_state["mints"]) == 2  # type: ignore[attr-defined]


def test_rotate_unknown_enrollment_404(env) -> None:
    env.fake.enrollment_exists = False  # type: ignore[attr-defined]
    assert env.post(f"/admin/enrollments/{uuid.uuid4()}/team-leader/rotate", headers=ADMIN_H).status_code == 404


def test_revoke_team_leader_idempotent(env) -> None:
    eid = uuid.uuid4()
    r1 = env.post(f"/admin/enrollments/{eid}/team-leader/revoke", headers=ADMIN_H)
    r2 = env.post(f"/admin/enrollments/{eid}/team-leader/revoke", headers=ADMIN_H)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json() == {"revoked": True}
    assert env.teams_state["revokes"] == [eid, eid]  # type: ignore[attr-defined]


def test_revoke_unknown_enrollment_404(env) -> None:
    env.fake.enrollment_exists = False  # type: ignore[attr-defined]
    assert env.post(f"/admin/enrollments/{uuid.uuid4()}/team-leader/revoke", headers=ADMIN_H).status_code == 404


# ---------------------------------------------------------------------------
# DELETE /admin/enrollments/{id} — cleanup for REVOKED rows only
# ---------------------------------------------------------------------------


def test_delete_enrollment_forgets_revoked_row(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The table-cleanup half of revoke (2026-10-30): a revoked row can be
    forgotten. The key count is audit context, NOT a blocker — the FK hands
    those keys enrollment_id NULL and they keep working; top-up requests
    cascade server-side (their table declares ON DELETE CASCADE)."""
    fake: EnrollTeamFakeDB = env.fake  # type: ignore[attr-defined]
    fake.enroll_delete_row = {"label": "FDE-team", "revoked_at": "now"}  # type: ignore[attr-defined]
    fake.enroll_key_count = 2  # type: ignore[attr-defined]
    # the enroll routes audit through THEIR OWN fail-soft helper (not
    # routes_admin._audit), so the recorder rides the module boundary —
    # the fixture's admin_audit_calls never sees this route.
    audit_calls: list[dict[str, Any]] = []

    async def record_audit(request: Any, **kw: Any) -> None:
        audit_calls.append(kw)

    monkeypatch.setattr(routes_enroll, "_enroll_audit", record_audit)
    r = env.delete(f"/admin/enrollments/{uuid.uuid4()}", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "keys_ungrouped": 2}
    sql, args = next((s, a) for s, a in fake.executed if s.startswith("DELETE FROM enrollments"))
    assert "$1" in sql and len(args) == 1
    # audited with the label + the key count, so the trail explains what died
    call = audit_calls[-1]
    assert call["action"] == "enroll.delete"
    assert call["resource_type"] == "enrollment"
    assert call["details"] == {"label": "FDE-team", "keys_ungrouped": 2}


def test_delete_enrollment_refuses_active_409(env) -> None:
    """An ACTIVE enrollment is never silently deleted — revoke first. That is
    the one guard between "cleanup" and "the team vanished"."""
    fake: EnrollTeamFakeDB = env.fake  # type: ignore[attr-defined]
    fake.enroll_delete_row = {"label": "BunchOBabies", "revoked_at": None}  # type: ignore[attr-defined]
    r = env.delete(f"/admin/enrollments/{uuid.uuid4()}", headers=ADMIN_H)
    assert r.status_code == 409
    assert "revoke" in r.json()["detail"].lower()
    assert not any(s.startswith("DELETE FROM enrollments") for s, _ in fake.executed)


def test_delete_enrollment_unknown_404(env) -> None:
    fake: EnrollTeamFakeDB = env.fake  # type: ignore[attr-defined]
    fake.enroll_delete_row = None  # type: ignore[attr-defined]
    assert env.delete(f"/admin/enrollments/{uuid.uuid4()}", headers=ADMIN_H).status_code == 404


# ---------------------------------------------------------------------------
# redeem stamps enrollment_id
# ---------------------------------------------------------------------------


def test_redeem_stamps_enrollment_id(env) -> None:
    """THE team-membership write: a redeemed key lands in the enrollment's
    team (api_keys.enrollment_id), which is what scopes it into the
    team-leader console and the team envelope."""
    eid = uuid.uuid4()
    env.fake.enroll_row = {"id": eid, "tenant_id": uuid.uuid4(), "profile_id": None}  # type: ignore[attr-defined]

    code = routes_enroll.generate_code()
    r = env.post("/v1/enroll", json={"code": code, "name": "Alice"}, headers=ADMIN_H)
    assert r.status_code == 201, r.text
    fake: EnrollTeamFakeDB = env.fake  # type: ignore[attr-defined]
    key_insert = next(a for s, a in fake.fetchrows if "INSERT INTO api_keys" in s)
    assert eid in key_insert, "api_keys.enrollment_id must be stamped at redeem"


# ---------------------------------------------------------------------------
# admin keys list — enrollment filters
# ---------------------------------------------------------------------------


def test_admin_keys_filter_by_enrollment_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_list_keys(**kw: Any):
        seen.update(kw)
        return []

    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(keys_mod, "list_keys", fake_list_keys)
    # routes_admin imported the FUNCTION object; patch the routes module ref
    monkeypatch.setattr(routes_admin, "list_keys", fake_list_keys)
    monkeypatch.setattr(budgets_mod, "db", EnrollTeamFakeDB())
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    eid = uuid.uuid4()
    r = tc.get(f"/admin/keys?enrollment_id={eid}", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert seen["enrollment_id"] == eid


def test_admin_keys_filter_by_label_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_list_keys(**kw: Any):
        seen.update(kw)
        return []

    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(keys_mod, "list_keys", fake_list_keys)
    monkeypatch.setattr(routes_admin, "list_keys", fake_list_keys)
    monkeypatch.setattr(budgets_mod, "db", EnrollTeamFakeDB())
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/keys?enrollment=%20station%20", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    # trimmed; forwarded as the case-insensitive substring needle
    assert seen["enrollment_search"] == "station"
    # no filter -> no needle
    seen.clear()
    assert tc.get("/admin/keys", headers=ADMIN_H).status_code == 200
    assert seen["enrollment_search"] is None and seen["enrollment_id"] is None


# ---------------------------------------------------------------------------
# presented-models: the payload has ONE source
# ---------------------------------------------------------------------------


def test_admin_presented_models_delegates_to_the_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel: dict[str, Any] = {"aliases": [], "models": [], "routing": [], "agents": [], "env": {}, "counts": {}}

    async def fake_payload() -> dict[str, Any]:
        return sentinel

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_admin, "presented_models_payload", fake_payload)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/presented-models", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert r.json() == sentinel


def test_team_leader_presents_the_same_payload_object() -> None:
    """The team console serves the byte-identical advertised view: the route
    IS a delegation to the admin payload builder (no second builder to
    drift)."""
    import inspect

    from pcai_llm.api import routes_team_leader

    src = inspect.getsource(routes_team_leader.team_presented_models)
    assert "presented_models_payload()" in src


# ---------------------------------------------------------------------------
# THREAD B - the ADMIN side of the top-up queue
# ---------------------------------------------------------------------------


def _topup_row(status: str = "pending", tokens: int | None = 5_000_000, usd: float | None = None) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "enrollment_id": uuid.uuid4(),
        "requested_tokens": tokens,
        "requested_usd": usd,
        "note": "please",
        "status": status,
        "created_at": "now",
        "resolved_at": None,
        "resolved_by": "",
    }


def _resolved_copy(row: dict[str, Any], status: str) -> dict[str, Any]:
    out = dict(row)
    out["status"] = status
    out["resolved_at"] = "just-now"
    out["resolved_by"] = "admin-key:abcd1234ef56"
    return out


def test_admin_topup_list_filters_and_bounds(env) -> None:
    env.fake.admin_topup_rows = [
        {"id": "t1", "status": "pending", "enrollment_label": "station-9", "tenant_name": "acme"}
    ]  # type: ignore[attr-defined]
    r = env.get("/admin/topup-requests?status=pending&limit=10", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert r.json()[0]["enrollment_label"] == "station-9"
    assert r.json()[0]["tenant_name"] == "acme"
    fake: EnrollTeamFakeDB = env.fake  # type: ignore[attr-defined]
    sql = [s for s in fake.fetch_sqls if "FROM enrollment_topup_requests r" in s][-1]
    assert "r.status = $1" in sql, sql
    assert "LIMIT $2" in sql, sql
    # bad status filter is a 400; the limit is capped at 200
    assert env.get("/admin/topup-requests?status=whenever", headers=ADMIN_H).status_code == 400
    env.get("/admin/topup-requests?limit=99999", headers=ADMIN_H)
    args = fake.fetch_args[-1]  # the capped LIMIT is the last fetch arg
    assert args[-1] == 200


def test_admin_approve_adds_to_existing_cap(env) -> None:
    """THE core semantic: approve ADDS the amount to the existing cap (never
    replaces it), in SQL, atomically with the resolution."""
    row = _topup_row(tokens=5_000_000)
    env.fake.topup_row = row  # type: ignore[attr-defined]
    env.fake.topup_resolved = _resolved_copy(row, "approved")  # type: ignore[attr-defined]
    env.fake.enroll_caps_row = {
        "budget_tokens": 15_000_000,
        "budget_usd": 50.0,
        "budget_period": "month",
    }  # type: ignore[attr-defined]
    tid = row["id"]
    r = env.post(f"/admin/topup-requests/{tid}/approve", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "approved"
    assert d["applied"] == {"tokens": 5_000_000, "usd": None}
    assert d["enrollment_caps"]["budget_tokens"] == 15_000_000  # 10M + 5M
    assert d["requested_tokens"] == 5_000_000
    fake: EnrollTeamFakeDB = env.fake  # type: ignore[attr-defined]
    sql, args = next((s, a) for s, a in fake.fetchrows if "UPDATE enrollments SET" in s and "CASE WHEN" in s)
    # the ADD is IN the SQL (a replace-regression fails this pin)
    assert "COALESCE(budget_tokens, 0) + $2::bigint" in sql, sql
    assert "COALESCE(budget_usd, 0) + $3::numeric" in sql, sql
    assert args[0] == row["enrollment_id"] and args[1] == 5_000_000 and args[2] is None
    cond = next(a for s, a in fake.fetchrows if "UPDATE enrollment_topup_requests" in s)
    assert cond[0] == tid
    # the gate cache was invalidated for the RIGHT enrollment
    assert env.teams_state["invalidated"][-1] == row["enrollment_id"]  # type: ignore[attr-defined]
    # audited with the admin action + applied/new-cap numbers
    call = env.admin_audit_calls[-1]  # type: ignore[attr-defined]
    assert call["action"] == "enroll.topup.approve"
    assert call["details"]["applied_tokens"] == 5_000_000
    assert call["details"]["new_caps"]["budget_tokens"] == 15_000_000


def test_admin_approve_establishes_cap_when_none(env) -> None:
    """The establish-a-cap case: the enrollment has NO cap of that kind
    (NULL) - the approved amount BECOMES the cap (COALESCE(NULL, 0) +
    amount). The untouched kind stays NULL."""
    row = _topup_row(tokens=None, usd=20.0)
    env.fake.topup_row = row  # type: ignore[attr-defined]
    env.fake.topup_resolved = _resolved_copy(row, "approved")  # type: ignore[attr-defined]
    env.fake.enroll_caps_row = {"budget_tokens": None, "budget_usd": 20.0, "budget_period": "month"}  # type: ignore[attr-defined]
    tid = row["id"]
    r = env.post(f"/admin/topup-requests/{tid}/approve", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["applied"] == {"tokens": None, "usd": 20.0}
    assert d["enrollment_caps"] == {"budget_tokens": None, "budget_usd": 20.0, "budget_period": "month"}


def test_admin_approve_override_approves_less(env) -> None:
    """The override: the admin approves LESS than asked (the response's
    applied != requested, and the trail carries both)."""
    row = _topup_row(tokens=5_000_000)
    env.fake.topup_row = row  # type: ignore[attr-defined]
    env.fake.topup_resolved = _resolved_copy(row, "approved")  # type: ignore[attr-defined]
    env.fake.enroll_caps_row = {"budget_tokens": 12_000_000, "budget_usd": None, "budget_period": "month"}  # type: ignore[attr-defined]
    tid = row["id"]
    r = env.post(f"/admin/topup-requests/{tid}/approve", json={"tokens": 2_000_000}, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["tokens"] == 2_000_000
    assert r.json()["requested_tokens"] == 5_000_000


def test_admin_approve_absent_body_means_as_requested(env) -> None:
    row = _topup_row(tokens=5_000_000)
    env.fake.topup_row = row  # type: ignore[attr-defined]
    env.fake.topup_resolved = _resolved_copy(row, "approved")  # type: ignore[attr-defined]
    env.fake.enroll_caps_row = {"budget_tokens": 15_000_000, "budget_usd": None, "budget_period": "month"}  # type: ignore[attr-defined]
    tid = row["id"]
    r = env.post(f"/admin/topup-requests/{tid}/approve", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert r.json()["applied"]["tokens"] == 5_000_000


def test_admin_approve_already_resolved_409(env) -> None:
    """THE double-click guard: resolving twice must not double the budget."""
    env.fake.topup_row = _topup_row(status="approved")  # type: ignore[attr-defined]
    r = env.post(f"/admin/topup-requests/{uuid.uuid4()}/approve", headers=ADMIN_H)
    assert r.status_code == 409
    assert "already approved" in r.json()["detail"]


def test_admin_approve_race_409(env) -> None:
    """The conditional UPDATE re-checks pending INSIDE the transaction: a
    concurrent resolution loses the race and refuses (nothing applied)."""
    env.fake.topup_row = _topup_row()  # read sees pending
    env.fake.topup_resolved = None  # the UPDATE matches nothing
    env.fake.enroll_caps_row = {"budget_tokens": 1_000_000, "budget_usd": None, "budget_period": "month"}  # type: ignore[attr-defined]  # the caps read precedes the tx guard
    r = env.post(f"/admin/topup-requests/{uuid.uuid4()}/approve", headers=ADMIN_H)
    assert r.status_code == 409
    assert "concurrently" in r.json()["detail"]


def test_admin_approve_unknown_404(env) -> None:
    env.fake.topup_row = None  # type: ignore[attr-defined]
    assert env.post(f"/admin/topup-requests/{uuid.uuid4()}/approve", headers=ADMIN_H).status_code == 404


def test_admin_approve_resulting_cap_overflow_400(env) -> None:
    """existing + approved must still fit the column - refused BEFORE the
    transaction with a readable 400 (nothing resolves)."""
    row = _topup_row(tokens=5_000_000)
    env.fake.topup_row = row  # type: ignore[attr-defined]
    env.fake.enroll_caps_row = {"budget_tokens": 2**63 - 1, "budget_usd": None, "budget_period": "month"}  # type: ignore[attr-defined]
    tid = row["id"]
    r = env.post(f"/admin/topup-requests/{tid}/approve", headers=ADMIN_H)
    assert r.status_code == 400
    assert "column maximum" in r.json()["detail"]
    fake: EnrollTeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert not any("UPDATE enrollment_topup_requests" in s for s, _ in fake.fetchrows)


def test_admin_deny_resolves_without_touching_caps(env) -> None:
    row = _topup_row(usd=12.5, tokens=None)
    env.fake.topup_row = row  # type: ignore[attr-defined]
    env.fake.topup_resolved = _resolved_copy(row, "denied")  # type: ignore[attr-defined]
    tid = row["id"]
    r = env.post(f"/admin/topup-requests/{tid}/deny", json={"note": "revisit next quarter"}, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "denied"
    fake: EnrollTeamFakeDB = env.fake  # type: ignore[attr-defined]
    # the caps are NEVER touched by a deny
    assert not any("CASE WHEN" in s for s, _ in fake.fetchrows)
    # and the gate cache is NOT invalidated (nothing changed)
    assert env.teams_state["invalidated"] == []  # type: ignore[attr-defined]
    call = env.admin_audit_calls[-1]  # type: ignore[attr-defined]
    assert call["action"] == "enroll.topup.deny"
    assert call["details"]["deny_note"] == "revisit next quarter"


def test_admin_deny_already_resolved_409(env) -> None:
    env.fake.topup_row = _topup_row(status="denied")  # type: ignore[attr-defined]
    r = env.post(f"/admin/topup-requests/{uuid.uuid4()}/deny", headers=ADMIN_H)
    assert r.status_code == 409
    assert "already denied" in r.json()["detail"]


def test_admin_topup_endpoints_require_admin(env) -> None:
    # no bearer at all -> 401 (the AdminDep gate)
    assert env.get("/admin/topup-requests").status_code == 401
    assert env.post(f"/admin/topup-requests/{uuid.uuid4()}/approve").status_code == 401
    assert env.post(f"/admin/topup-requests/{uuid.uuid4()}/deny").status_code == 401


def test_list_enrollments_exposes_per_model_caps(env) -> None:
    """Derived rows carry ONE cap per (model, kind, PERIOD) x the mint
    allowance — the console renders one line per pool instead of an opaque
    pooled number. The pooled budget_tokens stays (backward compat + the
    approve dialog) and is the sum of the presets whose period MATCHES the
    enrollment's, so the day preset never sums into it.

    A preset keeps its OWN period (2026-11-02 decision, revised): a day preset
    is a DAY team pool and a month preset a MONTH pool, and BOTH bind — the
    gate checks every cap for the model against that cap's own period. They
    are independent bounds, not a fan-out of one number: the day pool is what
    stops a team burning a month's allowance in an afternoon. (An earlier
    revision folded the day preset into the month one, which silently deleted
    the daily team bound — hence TWO entries are expected here.)"""
    env.fake.enrollments = [
        {
            "id": uuid.uuid4(),
            "label": "BunchOBabies",
            "tenant_name": "fleet",
            "profile_name": "baby-user",
            "max_uses": 4,
            "uses": 2,
            "expires_at": None,
            "revoked_at": None,
            "created_at": "t",
            "defaults_json": json.dumps(
                {
                    "budgets": [
                        {"model": "GLM-5.3-Flash", "period": "month", "limit_tokens": 1_000_000},
                        {"model": "GLM-5.3-Flash", "period": "day", "limit_tokens": 10_000},
                    ]
                }
            ),
        }
    ]
    r = env.get("/admin/enrollments", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    row = r.json()[0]
    assert row["budget_tokens"] == 4_000_000, "pooled month cap = 1M x 4 — the day preset never sums in"
    assert row["budget_derived"] is True
    assert row["budget_caps"] == [
        {
            "model": "GLM-5.3-Flash",
            "period": "month",
            "limit_tokens": 4_000_000,
            "limit_usd": None,
            "spent_tokens": 0,
            "spent_usd": 0.0,
        },
        {
            "model": "GLM-5.3-Flash",
            "period": "day",
            "limit_tokens": 40_000,
            "limit_usd": None,
            "spent_tokens": 0,
            "spent_usd": 0.0,
        },
    ]


def test_listing_caps_follow_derived_dimensions_only(env) -> None:
    """F3 (2026-10-30 review): an explicitly-capped dimension must not show
    preset caps — the listing and the gate share apply_derived_envelope, so
    the table can never advertise a per-model pool the gate does not enforce."""
    env.fake.enrollments = [
        {
            "id": uuid.uuid4(),
            "label": "mixed",
            "tenant_name": "fleet",
            "profile_name": "baby-user",
            "max_uses": 4,
            "uses": 1,
            "expires_at": None,
            "revoked_at": None,
            "created_at": "t",
            "budget_usd": 50.0,  # explicit USD: the derivation must NOT cap it
            "defaults_json": json.dumps(
                {
                    "budgets": [
                        {"model": "GLM-5.3-Flash", "period": "month", "limit_tokens": 1_000_000},
                        {"model": "GLM-5.3-Flash", "period": "month", "limit_usd": 2.5},
                    ]
                }
            ),
        }
    ]
    r = env.get("/admin/enrollments", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    row = r.json()[0]
    assert row["budget_usd"] == 50.0, "the explicit USD cap wins"
    assert all(c["limit_usd"] is None for c in row["budget_caps"]), "no USD caps: that dimension is explicit"
    assert any(c["limit_tokens"] for c in row["budget_caps"]), "the derived token dimension keeps its pools"
