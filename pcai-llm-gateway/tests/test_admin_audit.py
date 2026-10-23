"""Admin mutation audit trail tests (unit-mocked, suite convention).

Pins the four contracts the audit feature exists to guarantee:

1. A representative mutation PER CATEGORY writes exactly one admin_audit
   row with the right action/resource/actor shape (routes faked at the
   routes_admin/routes_enroll db seams; the trail observed at its OWN
   seam — core.audit.db — which is deliberately separate, so operation
   fakes elsewhere in the suite keep passing untouched).
2. REDACTION: credential VALUES never reach the audit row — the mcp-creds
   route records header NAMES only, and details_redact() itself is pinned
   for the secret-ish key patterns and the headers-container rule.
3. NEVER-FATAL: an audit-write failure (broken seam, DB down) must not
   fail the admin operation — the response stays 2xx and the operation's
   own writes are unaffected.
4. GET /admin/audit: admin-gated, filters AND-ed, limit clamped, CSV
   export mirrors the /admin/usage pattern (text/csv + attachment).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_enroll
from pcai_llm.config import settings
from pcai_llm.core import audit as audit_mod
from pcai_llm.core import secretbox
from pcai_llm.core.audit import REDACTED, details_redact, stable_actor_label

ADMIN_H = {"Authorization": "Bearer admin-secret-123"}


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class AuditTrailFake:
    """The audit seam: records every admin_audit INSERT (args verified) and
    can be told to FAIL (the never-fatal test). Mirrors a working pool."""

    FAILING = False

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.fail = False

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO admin_audit" not in sql:
            raise AssertionError(f"audit seam got a non-audit statement: {sql[:60]}")
        if self.fail:
            raise RuntimeError("audit db down (simulated)")
        self.rows.append(
            {
                "actor_key_id": args[0],
                "actor_subject": args[1],
                "actor_source": args[2],
                "action": args[3],
                "resource_type": args[4],
                "resource_id": args[5],
                "details": json.loads(args[6]),
                "request_id": args[7],
                "source_ip": args[8],
            }
        )
        return "INSERT 0 1"


class OpFakeDB:
    """The operation seams (routes_admin.db / routes_enroll.db): enough
    reads for the mutation handlers to reach their write, plus a record of
    every executed statement so the tests can assert the OPERATION still
    ran when the audit fails."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.keys_exist = True
        self.users_exist = True
        self.tenants_exist = True
        self.profiles_exist = True
        self.guardrails_exist = True
        self.server_exists = True
        self.enrollment_exists = True
        self.known_models: list[str] = []

    def acquire(self):
        return _OpConn(self)

    async def fetchval(self, sql: str, *args: Any):
        if "FROM api_keys" in sql:
            return 1 if self.keys_exist else None
        if "FROM users" in sql:
            return 1 if self.users_exist else None
        if "FROM tenants" in sql:
            return 1 if self.tenants_exist else None
        if "FROM profiles" in sql:
            return 1 if self.profiles_exist else None
        if "FROM guardrails" in sql:
            return 1 if self.guardrails_exist else None
        if "FROM mcp_servers" in sql:
            return 1 if self.server_exists else None
        if "FROM enrollments" in sql:
            return 1 if self.enrollment_exists else None
        if "count(*)" in sql:
            return 0
        return 1

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM guardrails" in sql and "config_json" in sql and "INSERT" not in sql:
            return {
                "id": uuid.uuid4(),
                "name": "g",
                "mode": "blocklist",
                "engine": "blocklist",
                "config_json": "{}",
                "enabled": True,
            }
        if "INSERT INTO guardrails" in sql:
            return {
                "id": uuid.uuid4(),
                "name": args[0],
                "mode": args[1],
                "engine": args[2],
                "config_json": args[3],
                "enabled": args[4],
                "created_at": None,
            }
        if "FROM profiles" in sql:
            return {"id": uuid.uuid4(), "name": "p", "defaults_json": "{}"}
        if "INSERT INTO" in sql:
            return {"id": uuid.uuid4(), "name": "x"}
        if "UPDATE" in sql:
            return {"id": uuid.uuid4(), "name": "x"}
        return {"id": uuid.uuid4()}

    async def fetch(self, sql: str, *args: Any):
        if "model_name" in sql and "SELECT DISTINCT" in sql:
            return [{"model_name": m} for m in self.known_models]
        return []

    async def execute(self, sql: str, *args: Any):
        self.executed.append(sql)
        return "UPDATE 1"


class _OpConn:
    def __init__(self, db: OpFakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def execute(self, sql: str, *args: Any):
        self._db.executed.append(sql)
        return "UPDATE 1"

    async def executemany(self, sql: str, args: Any):
        self._db.executed.append(sql)

    async def fetchrow(self, sql: str, *args: Any):
        # Delegate to the parent fake's SQL-shape dispatch (the transactional
        # redeem (audit finding 8) runs its reads on the acquired conn).
        return await self._db.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any):
        return []


class _RegistryFake:
    """registry.refresh's own seam: empty SELECTs (nothing registered) plus
    the opportunistic rate-window prune."""

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        return "DELETE 0"


class AuditQueryFake(OpFakeDB):
    """Adds the admin_audit READ shape (filters arrive as args; the fake
    records them so tests assert filter/paging plumbing without a DB)."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__()
        self.read_rows = rows or []
        self.read_args: tuple = ()
        self.read_sql = ""

    async def fetch(self, sql: str, *args: Any):
        if "FROM admin_audit" in sql:
            self.read_sql = sql
            self.read_args = args
            return self.read_rows
        return await super().fetch(sql, *args)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def trail(monkeypatch: pytest.MonkeyPatch) -> AuditTrailFake:
    t = AuditTrailFake()
    monkeypatch.setattr(audit_mod, "db", t)
    return t


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, trail):
    """Admin API app (routes_admin + enroll admin router) with faked seams."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = OpFakeDB()
    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)  # budget_status after replace
    monkeypatch.setattr(routes_enroll, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    app.include_router(routes_enroll.admin_router)
    return TestClient(app, raise_server_exceptions=False), fake, trail


# ---------------------------------------------------------------------------
# 1. one audit row per representative mutation, per category
# ---------------------------------------------------------------------------


def test_key_create_audited(env) -> None:
    tc, _fake, trail = env
    r = tc.post("/admin/keys", json={"user_id": str(uuid.uuid4()), "name": "ci-bot"}, headers=ADMIN_H)
    assert r.status_code == 201
    assert len(trail.rows) == 1
    row = trail.rows[0]
    assert row["action"] == "key.create"
    assert row["resource_type"] == "key"
    assert row["actor_source"] == "admin-key"
    # the actor label: a stable digest prefix, never the key itself
    assert row["actor_subject"] == stable_actor_label("admin-secret-123")
    assert "admin-secret-123" not in json.dumps(trail.rows)  # secret material never lands
    assert row["details"]["name"] == "ci-bot"


def test_key_revoke_audited(env) -> None:
    tc, _fake, trail = env
    kid = uuid.uuid4()
    r = tc.delete(f"/admin/keys/{kid}", headers=ADMIN_H)
    assert r.status_code == 200
    row = trail.rows[0]
    assert row["action"] == "key.revoke" and row["resource_type"] == "key"
    assert row["resource_id"] == str(kid)


def test_budget_set_audited(env) -> None:
    tc, _fake, trail = env
    r = tc.post(
        f"/admin/keys/{uuid.uuid4()}/budgets",
        json={"budgets": [{"model": "*", "period": "month", "limit_tokens": 1000}]},
        headers=ADMIN_H,
    )
    assert r.status_code == 200
    row = trail.rows[0]
    assert row["action"] == "budget.set"
    assert row["details"]["budgets"][0]["limit_tokens"] == 1000


def test_model_upsert_audited(env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _fake, trail = env
    import pcai_llm.core.registry as registry_mod

    monkeypatch.setattr(registry_mod, "db", _RegistryFake())  # refresh() reads its own seam
    monkeypatch.setattr(settings, "secrets_master_key", "audit-test-master")
    monkeypatch.setattr(secretbox, "_fernet", None)
    r = tc.post(
        "/admin/models",
        json={
            "model_name": "m1",
            "endpoints": [{"base_url": "http://vllm-svc.ns.svc.cluster.local/v1", "api_key": "sk-upstream-secret"}],
        },
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text
    row = trail.rows[0]
    assert row["action"] == "model.upsert" and row["resource_type"] == "model"
    assert row["resource_id"] == "m1"
    # the upstream credential VALUE never reaches the trail — presence marker only
    blob = json.dumps(trail.rows)
    assert "sk-upstream-secret" not in blob
    assert row["details"]["endpoints"][0]["panel_credential_set"] is True


def test_mcp_server_upsert_audited(env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _fake, trail = env
    import pcai_llm.core.registry as registry_mod

    # the route's panel credential path needs the master key (suite pattern:
    # fresh Fernet derivation per test); registry.refresh reads its OWN db seam
    monkeypatch.setattr(registry_mod, "db", _RegistryFake())
    monkeypatch.setattr(settings, "secrets_master_key", "audit-test-master")
    monkeypatch.setattr(secretbox, "_fernet", None)
    r = tc.post(
        "/admin/mcp-servers",
        json={"name": "mem-rag", "url": "http://mem-rag.ns.svc.cluster.local:8000", "auth_token": "bearer-secret-xyz"},
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text
    row = trail.rows[0]
    assert row["action"] == "mcp.server.upsert" and row["resource_type"] == "mcp_server"
    assert "bearer-secret-xyz" not in json.dumps(trail.rows)
    assert row["details"]["auth_token_set"] is True


def test_mcp_acl_set_audited(env) -> None:
    tc, _fake, trail = env
    kid = str(uuid.uuid4())
    r = tc.put(
        f"/admin/keys/{kid}/mcp-grants",
        json={"grants": [{"server": "logsearch", "tool": "search_logs"}]},
        headers=ADMIN_H,
    )
    assert r.status_code == 200
    row = trail.rows[0]
    assert row["action"] == "mcp.acl.set"
    assert row["details"]["grants"] == [["logsearch", "search_logs"]]


def test_creds_set_audited_names_only(env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _fake, trail = env
    # the credential write needs the master key (test_mcp_creds pattern)
    monkeypatch.setattr(settings, "secrets_master_key", "audit-test-master")
    monkeypatch.setattr(secretbox, "_fernet", None)
    kid = str(uuid.uuid4())
    r = tc.put(
        f"/admin/keys/{kid}/mcp-creds",
        json={
            "server": "mm-rag",
            "headers": {"X-Memory-Dataset": "alice-notes", "X-Dataset-Password": "hunter2-super-secret"},
        },
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text
    assert len(trail.rows) == 1
    blob = json.dumps(trail.rows)
    # THE redaction case: credential VALUES never land (not even via a
    # forgotten pre-redaction — details_redact runs at the single writer)
    assert "hunter2-super-secret" not in blob
    assert "alice-notes" not in blob  # header VALUES are credentials too
    assert trail.rows[0]["action"] == "mcp.creds.set"
    assert trail.rows[0]["details"]["header_names"] == ["X-Dataset-Password", "X-Memory-Dataset"]
    assert trail.rows[0]["details"]["header_count"] == 2


def test_profile_upsert_audited(env) -> None:
    tc, _fake, trail = env
    r = tc.post("/admin/profiles", json={"name": "team-x", "model_order": ["m1"]}, headers=ADMIN_H)
    assert r.status_code == 201
    row = trail.rows[0]
    assert row["action"] == "profile.upsert" and row["resource_type"] == "profile"
    assert row["details"]["name"] == "team-x"


def test_offpeak_set_audited(env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _fake, trail = env
    import pcai_llm.core.offpeak as offpeak_mod

    class NoRefreshDB:
        async def execute(self, sql: str, *args: Any) -> str:
            return "UPDATE 1"

        async def fetch(self, sql: str, *args: Any):
            return []

    monkeypatch.setattr(offpeak_mod, "db", NoRefreshDB())
    r = tc.put("/admin/offpeak/models/Qwen/Qwen3.8-27B-FP8/fraction", json={"fraction": 0.5}, headers=ADMIN_H)
    assert r.status_code == 200
    row = trail.rows[0]
    assert row["action"] == "offpeak.set"
    assert row["resource_id"] == "Qwen/Qwen3.8-27B-FP8"  # the :path converter's slash name
    assert row["details"]["fraction"] == 0.5


def test_guardrail_create_audited(env) -> None:
    tc, _fake, trail = env
    # the create path hits UNIQUE-violation INSERT via fetchrow returning a
    # row; the OpFakeDB INSERT path returns {"id", "name"} — enough
    r = tc.post(
        "/admin/guardrails",
        json={"name": "no-pii", "mode": "pre_call", "engine": "blocklist", "config": {"keywords": ["x"]}},
        headers=ADMIN_H,
    )
    assert r.status_code == 201, r.text
    row = trail.rows[0]
    assert row["action"] == "guardrail.create" and row["resource_type"] == "guardrail"


def test_enroll_redeem_audited(monkeypatch: pytest.MonkeyPatch, trail) -> None:
    """The PUBLIC door audits with actor_source='enroll' — no admin bearer,
    the actor is the minted key/subject; the CODE never rides the trail."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")

    class EnrollFake(OpFakeDB):
        def __init__(self) -> None:
            super().__init__()
            self.enroll_row = {"id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "profile_id": None}

        async def fetchrow(self, sql: str, *args: Any):
            if "UPDATE enrollments SET uses" in sql:
                return self.enroll_row
            if "INSERT INTO users" in sql:
                return {"id": uuid.uuid4(), "name": args[1]}
            if "INSERT INTO api_keys" in sql:
                return {"id": uuid.uuid4()}
            if "FROM tenants" in sql:
                return {"name": "acme"}
            return None

        async def fetchval(self, sql: str, *args: Any):
            if "FROM tenants" in sql:
                return "acme"
            return 1

        async def fetch(self, sql: str, *args: Any):
            return []

    fake = EnrollFake()
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    routes_enroll._redeem_attempts.clear()
    app = FastAPI()
    app.include_router(routes_enroll.router)
    tc = TestClient(app, raise_server_exceptions=False)

    code = routes_enroll.generate_code()
    r = tc.post("/v1/enroll", json={"code": code, "name": "Alice"}, headers={"X-Request-Id": "req-audit-1"})
    assert r.status_code == 201, r.text
    assert len(trail.rows) == 1
    row = trail.rows[0]
    assert row["action"] == "enroll.redeem"
    assert row["actor_source"] == "enroll"
    assert row["resource_type"] == "key"
    assert row["request_id"] == "req-audit-1"  # the X-Request-Id header chain (app.py convention)
    assert code not in json.dumps(trail.rows)  # the credential code never lands


def test_enrollment_mint_audited(env) -> None:
    tc, _fake, trail = env
    r = tc.post("/admin/enrollments", json={"label": "onboarding", "tenant_id": str(uuid.uuid4())}, headers=ADMIN_H)
    assert r.status_code == 201, r.text
    row = trail.rows[0]
    assert row["action"] == "enroll.code.mint" and row["resource_type"] == "enrollment"
    assert row["details"]["label"] == "onboarding"


# ---------------------------------------------------------------------------
# 2. redaction helper
# ---------------------------------------------------------------------------


def test_details_redact_secret_keys() -> None:
    payload = {
        "api_key": "sk-123",
        "auth_token": "tok",
        "X-Dataset-Password": "p",
        "nested": {"credential": {"value": "v"}, "keep": 1},
        "key_name": "keep-me",  # 'key_name' is metadata, not a secret — stays
        "key_id": "also-keep",
    }
    out = details_redact(payload)
    assert out["api_key"] == REDACTED
    assert out["auth_token"] == REDACTED
    assert out["X-Dataset-Password"] == REDACTED
    assert out["nested"]["credential"] == REDACTED
    assert out["nested"]["keep"] == 1
    assert out["key_name"] == "keep-me"
    assert out["key_id"] == "also-keep"


def test_details_redact_headers_container() -> None:
    # header VALUES are upstream credentials BY DEFINITION (the memory-MCP
    # override shape) — every value under a headers container redacts, even
    # innocent-looking names.
    out = details_redact({"headers": {"X-Memory-Dataset": "alice", "X-Custom": "v"}})
    assert out["headers"] == {"X-Memory-Dataset": REDACTED, "X-Custom": REDACTED}


def test_details_redact_never_raises() -> None:
    # a circular payload must not break the audit write path
    cyc: dict[str, Any] = {}
    cyc["self"] = cyc
    assert isinstance(details_redact(cyc), dict)


# ---------------------------------------------------------------------------
# 3. audit failure NEVER fails the operation
# ---------------------------------------------------------------------------


def test_audit_failure_does_not_fail_operation(env, trail, caplog) -> None:
    tc, fake, trail = env
    trail.fail = True
    kid = uuid.uuid4()
    with caplog.at_level(logging.ERROR, logger="pcai_llm.core.audit"):
        r = tc.delete(f"/admin/keys/{kid}", headers=ADMIN_H)
    assert r.status_code == 200  # the OPERATION succeeded
    assert r.json() == {"revoked": True}
    assert any("admin_audit WRITE FAILED" in rec.message for rec in caplog.records)  # loud log
    assert fake.executed  # the operation's own statements ran


def test_audit_failure_does_not_fail_mcp_creds(env, trail, monkeypatch: pytest.MonkeyPatch) -> None:
    """The credential route — the highest-stakes redaction case — must ALSO
    survive an audit outage (never-fatal contract is unconditional)."""
    tc, _fake, trail = env
    monkeypatch.setattr(settings, "secrets_master_key", "audit-test-master")
    monkeypatch.setattr(secretbox, "_fernet", None)
    trail.fail = True
    kid = str(uuid.uuid4())
    r = tc.put(
        f"/admin/keys/{kid}/mcp-creds",
        json={"server": "mm-rag", "headers": {"X-Dataset-Password": "p"}},
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["header_names"] == ["X-Dataset-Password"]


# ---------------------------------------------------------------------------
# 4. GET /admin/audit — gating, filters, paging, CSV
# ---------------------------------------------------------------------------


def _read_env(monkeypatch: pytest.MonkeyPatch, trail, rows: list[dict[str, Any]]):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = AuditQueryFake(rows)
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake


AUDIT_ROW = {
    "id": 1,
    "ts": "2026-01-01T00:00:00+00:00",
    "actor_key_id": None,
    "actor_subject": "admin-key:abcdef123456",
    "actor_source": "admin-key",
    "action": "key.create",
    "resource_type": "key",
    "resource_id": "11111111-1111-1111-1111-111111111111",
    "details": {"name": "ci-bot"},
    "request_id": "req-1",
    "source_ip": "10.0.0.1",
}


def test_audit_route_requires_admin(monkeypatch: pytest.MonkeyPatch, trail) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = AuditQueryFake([AUDIT_ROW])
    monkeypatch.setattr(routes_admin, "db", fake)
    tc = TestClient(FastAPI() if False else _app(routes_admin.router), raise_server_exceptions=False)
    assert tc.get("/admin/audit").status_code == 401
    assert tc.get("/admin/audit", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert tc.get("/admin/audit", headers=ADMIN_H).status_code == 200


def _app(router) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_audit_route_filters_and_paging(monkeypatch: pytest.MonkeyPatch, trail) -> None:
    tc, fake = _read_env(monkeypatch, trail, [AUDIT_ROW])
    r = tc.get(
        "/admin/audit",
        headers=ADMIN_H,
        params={
            "action": "key.create",
            "resource_type": "key",
            "actor_key_id": 7,
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-02-01T00:00:00Z",
            "limit": 30,
            "offset": 60,
        },
    )
    assert r.status_code == 200
    args = fake.read_args
    # filter args arrive in order: since, until, actor_key_id, action, resource_type, limit, offset
    assert len(args) == 7
    assert args[2] == 7
    assert args[3] == "key.create"
    assert args[4] == "key"
    assert args[5] == 30  # requested limit preserved (<= cap)
    assert args[6] == 60  # offset plumbed
    assert "ORDER BY ts DESC" in fake.read_sql  # newest-first contract
    body = r.json()
    assert body[0]["action"] == "key.create"
    assert body[0]["details"] == {"name": "ci-bot"}  # JSONB decoded to a dict


def test_audit_route_limit_clamped(monkeypatch: pytest.MonkeyPatch, trail) -> None:
    tc, fake = _read_env(monkeypatch, trail, [])
    assert tc.get("/admin/audit", headers=ADMIN_H, params={"limit": 100000}).status_code == 200
    assert fake.read_args[-2] == 500  # clamped to the cap
    assert tc.get("/admin/audit", headers=ADMIN_H, params={"limit": 0}).status_code == 200
    assert fake.read_args[-2] == 1  # clamped to >= 1


def test_audit_route_bad_format_400(monkeypatch: pytest.MonkeyPatch, trail) -> None:
    tc, _fake = _read_env(monkeypatch, trail, [])
    assert tc.get("/admin/audit", headers=ADMIN_H, params={"format": "xml"}).status_code == 400


def test_audit_route_csv_export(monkeypatch: pytest.MonkeyPatch, trail) -> None:
    tc, _fake = _read_env(monkeypatch, trail, [AUDIT_ROW])
    r = tc.get("/admin/audit", headers=ADMIN_H, params={"format": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and "admin-audit.csv" in r.headers["content-disposition"]
    body = r.text
    assert "key.create" in body and "admin_audit" not in body
    # same /admin/usage CSV convention: header row from the projection keys
    assert body.splitlines()[0].startswith("id,ts")


# ---------------------------------------------------------------------------
# schema contract (static: the suite has no live Postgres — the DDL text is
# the contract; guarded-diff pins the append-only shape)
# ---------------------------------------------------------------------------


def test_schema_admin_audit_append_only_and_redaction_doc() -> None:
    from pcai_llm.db.schema import SCHEMA_SQL

    tbl = SCHEMA_SQL[SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS admin_audit") :]
    tbl = tbl[: tbl.index(";")]
    # the columns the spec names
    for col in (
        "actor_key_id",
        "actor_subject",
        "actor_source",
        "action",
        "resource_type",
        "resource_id",
        "details",
        "request_id",
        "source_ip",
    ):
        assert col in tbl
    assert "JSONB" in tbl
    # the three query indexes exist
    for idx in ("idx_admin_audit_ts", "idx_admin_audit_actor", "idx_admin_audit_resource"):
        assert f"CREATE INDEX IF NOT EXISTS {idx}" in SCHEMA_SQL
    # the REDACTION + append-only rules are DOCUMENTED where the next editor
    # will read them (schema.py's why-comments, house style)
    assert "redact" in SCHEMA_SQL.lower()
    assert "append-only" in SCHEMA_SQL.lower()


def test_redaction_vocabulary_covers_cookie() -> None:
    """A Cookie header is a credential and must redact on BOTH surfaces that
    share this vocabulary.

    Regression this pins: routes_admin MCP-server redaction used to carry
    its own inline regex containing `cookie`. The refactor to the shared
    core.audit._SECRET_KEY_RE dropped that alternative, so a Cookie parked in
    an MCP server headers map came back in CLEARTEXT on every console refresh
    and on /admin/registry/refresh - while the code comment claimed the two
    lists "cannot drift".
    """
    from pcai_llm.core.audit import _SECRET_KEY_RE, details_redact

    for name in ("Cookie", "cookie", "Set-Cookie", "Authorization", "X-Api-Key"):
        assert _SECRET_KEY_RE.search(name), f"{name} must be treated as credential-ish"
    # ...and end to end through the single writer
    out = details_redact({"headers": {"Cookie": "session=abc", "X-Custom": "v"}})
    assert out["headers"]["Cookie"] == REDACTED
    # the metadata contract still holds: bare key compounds stay readable
    assert not _SECRET_KEY_RE.search("key_id")
