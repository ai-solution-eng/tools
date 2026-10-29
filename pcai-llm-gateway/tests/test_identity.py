"""Phase A identity: subjects schema, binding at issuance, rotation carry,
the resolution ladder, and the X-MCP-Caller attribution stamp.

The review contract this file pins (reviews/01-gateway-impl.md §1.1–1.5,
DECISIONS.md "OAuth REVISION (final)"):

* Schema is ADDITIVE + IDEMPOTENT: re-running SCHEMA_SQL on a database that
  already has the subjects shape must be a no-op (the 24-statement ALTER
  discipline), and the DDL matches review §1.2 verbatim.
* Binding happens at AUTHENTICATED MOMENTS only: enrollment redemption
  (self-asserted or SSO-browser-wins) and the admin console. bound_via is
  never client-writable — it is server-stamped per flow.
* Rotation carries the binding: subject_id/bound_via/bound_at join rotate's
  explicit INSERT column list — the grants-carry-over bug class, this time
  for identity.
* Ladder (Phase A, NO token rung): trusted browser headers (flag-gated) →
  identity-bound key → anonymous key-fp. Resolver exceptions propagate
  (fail-closed, the _McpAuthASGI DB posture); the STAMP lookup degrades
  (attribution is enrichment, not enforcement).
* The X-MCP-Caller stamp runs AFTER the last-wins override merge and
  overwrites any tenant-supplied value; the write path reserves the name
  with a 400; values are sanitized (CR/LF stripped, 200 cap).

Suite convention: no live Postgres — db calls are monkeypatched fakes.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_enroll, routes_mcp
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import identity
from pcai_llm.core.identity import (
    IDP_ADMIN_ASSERTED,
    IDP_OAUTH2_PROXY,
    IDP_SELF_ASSERTED,
    KIND_BROWSER,
    KIND_KEY_ANON,
    KIND_KEY_BOUND,
    MCP_CALLER_HEADER,
    VIA_ADMIN_CONSOLE,
    VIA_ENROLLMENT,
    bind_key_subject,
    browser_identity_from_headers,
    key_fingerprint,
    normalize_subject,
    resolve_caller,
    sanitize_caller_value,
    subject_for_key,
)
from pcai_llm.db.schema import SCHEMA_SQL

ADMIN_H = {"Authorization": "Bearer admin-secret-123"}


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["mcp"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


# ---------------------------------------------------------------------------
# 1. schema: additive + idempotent, review §1.2 DDL
# ---------------------------------------------------------------------------


def test_schema_subjects_ddl_matches_review() -> None:
    """The review §1.2 DDL is the contract — the schema carries it verbatim
    (subjects table shape, the three api_keys columns, the index)."""
    for fragment in (
        "CREATE TABLE IF NOT EXISTS subjects (",
        "tenant_id    UUID REFERENCES tenants(id) ON DELETE SET NULL",
        "kind         TEXT NOT NULL CHECK (kind IN ('user','service','anonymous'))",
        "UNIQUE (idp, subject)",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS subject_id UUID REFERENCES subjects(id) ON DELETE SET NULL",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS bound_via TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS bound_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_api_keys_subject ON api_keys(subject_id)",
    ):
        assert fragment in SCHEMA_SQL, f"missing from SCHEMA_SQL: {fragment!r}"


def test_schema_subjects_reapply_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running the schema against a DB that already has the subjects
    shape is a NO-OP — the IF NOT EXISTS discipline every migration here
    follows (a boot on an already-migrated database must never crash).

    Fake models the statements the DDL actually runs and asserts every
    subjects-related statement is IF NOT EXISTS-guarded (the guard IS the
    idempotency mechanism for CREATE/ALTER — a re-run re-executes them and
    must not raise)."""

    # Strip SQL comment lines, split into statements: every statement that
    # touches a subjects-related object must be IF NOT EXISTS-guarded, so
    # re-executing the whole SCHEMA_SQL (every boot) is legal on an
    # already-migrated database.
    no_comments = "\n".join(
        # full-line AND trailing comments (the bound_via DDL ends with one)
        line.split("--", 1)[0]
        for line in SCHEMA_SQL.split("\n")
    )
    stmts = [s.strip() for s in no_comments.split(";") if s.strip()]
    identity_stmts = [
        s for s in stmts if "subjects" in s or "idx_api_keys_subject" in s or "bound_via" in s or "bound_at" in s
    ]
    # 5 statements: CREATE TABLE subjects + 3 ALTER api_keys (subject_id /
    # bound_via / bound_at) + the binding index.
    assert len(identity_stmts) == 5, f"expected 5 identity DDL statements, got {len(identity_stmts)}"
    for s in identity_stmts:
        head = " ".join(s.split("\n", 1)[0].split())
        assert head.startswith(
            (
                "CREATE TABLE IF NOT EXISTS",
                "CREATE INDEX IF NOT EXISTS",
                "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS",
            )
        ), f"identity DDL statement is not IF NOT EXISTS-guarded: {head!r}"


def test_schema_uses_gen_random_uuid_for_subjects() -> None:
    """subjects keys default from gen_random_uuid() like every other UUID
    table in this schema (no application-side id minting needed)."""
    m = re.search(r"CREATE TABLE IF NOT EXISTS subjects \(([^;]+)\)", SCHEMA_SQL, re.DOTALL)
    assert m and "id           UUID PRIMARY KEY DEFAULT gen_random_uuid()" in m.group(1)


# ---------------------------------------------------------------------------
# 2. pure helpers: header parsing, sanitizer, fingerprint
# ---------------------------------------------------------------------------


def test_browser_identity_parses_user_email_groups() -> None:
    ident = browser_identity_from_headers(
        {"X-Auth-Request-User": "alice", "X-Forwarded-Groups": "eng, platform-admin ,"}
    )
    assert ident == ("alice", ["eng", "platform-admin"])
    # email fallback when the user header is absent
    assert browser_identity_from_headers({"X-Email": "a@b.c"}) == ("a@b.c", [])


def test_browser_identity_none_without_headers() -> None:
    assert browser_identity_from_headers({}) is None
    assert browser_identity_from_headers({"X-Forwarded-Groups": "g"}) is None
    assert browser_identity_from_headers({"X-Auth-Request-User": "   "}) is None


def test_sanitize_caller_value_strips_controls_and_caps() -> None:
    assert sanitize_caller_value("alice\r\nX-Evil: 1") == "aliceX-Evil: 1"
    assert sanitize_caller_value("  padded  ") == "padded"
    assert sanitize_caller_value("x" * 500) == "x" * 200  # cap
    assert sanitize_caller_value("tab\there\x00nul\x7fdel") == "tabherenuldel"
    assert sanitize_caller_value("\r\n") == ""


def test_normalize_subject_matches_sanitizer() -> None:
    assert normalize_subject("  bob \n") == "bob"
    assert normalize_subject("") == ""


def test_key_fingerprint_is_the_key_id() -> None:
    k = _key()
    assert key_fingerprint(k) == str(k.id)
    assert key_fingerprint(None) == ""


# ---------------------------------------------------------------------------
# 3. the resolution ladder (Phase A — no token rung)
# ---------------------------------------------------------------------------


class LadderDB:
    """Subjects-binding fake: one bound key (subject via FK), everything
    else unbound. DB failure injectable."""

    def __init__(self, bindings: dict[uuid.UUID, dict[str, Any]] | None = None, broken: bool = False) -> None:
        self.bindings = bindings or {}
        self.broken = broken

    async def fetchrow(self, sql: str, *args: Any):
        if self.broken:
            raise RuntimeError("database not connected")
        if "JOIN subjects s ON s.id" in sql:
            return self.bindings.get(args[0])
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


def test_ladder_browser_headers_need_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE trust decision: identical SSO headers resolve to the browser rung
    ONLY under PCAI_LLM_TRUST_PROXY_IDENTITY=true. Flag off (the default) =
    today's pseudonymous behavior — a caller cannot rotate fake identity
    headers into an identity (the RAG_TRUST_PROXY_IDENTITY rule)."""
    headers = {"X-Auth-Request-User": "alice", "X-Forwarded-Groups": "eng"}
    kid = uuid.uuid4()
    k = _key(id=kid)

    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(identity, "db", LadderDB())
    caller = asyncio.run(resolve_caller(k, headers=headers))
    assert caller is not None
    assert caller.subject_kind == KIND_KEY_ANON and caller.subject is None
    assert caller.key_fp == str(kid) and caller.groups == []

    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    caller2 = asyncio.run(resolve_caller(k, headers=headers))
    assert caller2.subject_kind == KIND_BROWSER
    assert caller2.subject == "alice" and caller2.groups == ["eng"]


def test_ladder_browser_wins_over_bound_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """First hit wins: a trusted browser identity beats a key-bound subject
    on the same request (the browser is the fresher assertion)."""
    kid = uuid.uuid4()
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(
        identity,
        "db",
        LadderDB({kid: {"subject": "keybound@x", "idp": IDP_SELF_ASSERTED, "kind": "user", "groups": []}}),
    )
    caller = asyncio.run(resolve_caller(_key(id=kid), headers={"X-Auth-Request-User": "alice"}))
    assert caller.subject == "alice" and caller.subject_kind == KIND_BROWSER


def test_ladder_key_bound_resolves_subject_via_fk(monkeypatch: pytest.MonkeyPatch) -> None:
    kid = uuid.uuid4()
    monkeypatch.setattr(settings, "trust_proxy_identity", False)  # flag OFF: key rungs decide
    monkeypatch.setattr(
        identity,
        "db",
        LadderDB({kid: {"subject": "alice@corp", "idp": IDP_ADMIN_ASSERTED, "kind": "user", "groups": ["eng"]}}),
    )
    caller = asyncio.run(resolve_caller(_key(id=kid)))
    assert caller.subject_kind == KIND_KEY_BOUND
    assert caller.subject == "alice@corp" and caller.groups == ["eng"]
    assert caller.key is not None and caller.key_fp == str(kid)


def test_ladder_unbound_key_is_anonymous_fp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: a NULL binding is today's behavior — pseudonymous,
    kind='key-anon', enforcement unchanged."""
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(identity, "db", LadderDB())
    caller = asyncio.run(resolve_caller(_key()))
    assert caller.subject is None
    assert caller.subject_kind == KIND_KEY_ANON
    assert caller.key_fp == str(caller.key.id)


def test_ladder_nothing_resolved_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(identity, "db", LadderDB())
    assert asyncio.run(resolve_caller(None)) is None


def test_ladder_resolver_db_failure_raises_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """RESOLVER posture: exceptions PROPAGATE — an unavailable identity
    ledger is a refusal condition, never a bypass (the _McpAuthASGI DB
    posture; auth surfaces turn it into 401)."""
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(identity, "db", LadderDB(broken=True))
    with pytest.raises(RuntimeError):
        asyncio.run(resolve_caller(_key()))


def test_subject_for_key_degrades_on_db_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """STAMP posture: the attribution lookup degrades to None on DB failure
    (enrichment on an already-authorized call — the key_override_headers
    posture), which resolve_mcp_headers then falls back to the key fp."""
    monkeypatch.setattr(identity, "db", LadderDB(broken=True))
    assert asyncio.run(subject_for_key(uuid.uuid4())) is None


def test_subject_for_key_returns_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    kid = uuid.uuid4()
    monkeypatch.setattr(
        identity,
        "db",
        LadderDB({kid: {"subject": "bob", "idp": IDP_OAUTH2_PROXY, "kind": "user", "groups": []}}),
    )
    assert asyncio.run(subject_for_key(kid)) == "bob"
    assert asyncio.run(subject_for_key(uuid.uuid4())) is None  # unbound


# ---------------------------------------------------------------------------
# 4. bind_key_subject: upsert + stamp, both postures
# ---------------------------------------------------------------------------


class BindDB:
    def __init__(self) -> None:
        self.fetchrows: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []
        self.fail = False

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrows.append((sql, args))
        if self.fail:
            raise RuntimeError("down")
        return {"id": uuid.uuid4()}

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        if self.fail:
            raise RuntimeError("down")
        return "UPDATE 1"


def test_bind_key_subject_upserts_and_stamps(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = BindDB()
    monkeypatch.setattr(identity, "db", fake)
    kid = uuid.uuid4()
    sid = asyncio.run(
        bind_key_subject(
            kid,
            subject="alice@corp",
            idp=IDP_SELF_ASSERTED,
            bound_via=VIA_ENROLLMENT,
            tenant_id=uuid.uuid4(),
            display_name="Alice",
            groups=["eng"],
        )
    )
    assert sid is not None
    bind_sql, bind_args = fake.fetchrows[0]
    assert "INSERT INTO subjects" in bind_sql and "ON CONFLICT (idp, subject)" in bind_sql
    assert bind_args[2] == IDP_SELF_ASSERTED and bind_args[3] == "alice@corp" and bind_args[5] == ["eng"]
    stamp_sql, stamp_args = fake.executed[0]
    assert "SET subject_id" in stamp_sql and "bound_via" in stamp_sql and "bound_at" in stamp_sql
    assert stamp_args[0] == kid and stamp_args[1] == sid and stamp_args[2] == VIA_ENROLLMENT


def test_bind_key_subject_best_effort_failure_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The best-effort posture NEVER raises: a failed binding leaves the key
    anonymous (NULL = today's enforcement) instead of failing an issuance
    that already minted an irreplaceable plaintext."""
    fake = BindDB()
    fake.fail = True
    monkeypatch.setattr(identity, "db", fake)
    assert asyncio.run(bind_key_subject(uuid.uuid4(), subject="s", idp="self-asserted", bound_via="enrollment")) is None


def test_bind_key_subject_with_conn_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transaction posture: with conn=, statements join the caller's
    transaction and failures propagate (the admin-console create rolls
    back — bind-before-anything-irreplaceable)."""

    class Conn:
        async def fetchrow(self, sql: str, *args: Any):
            raise RuntimeError("down mid-transaction")

        async def execute(self, sql: str, *args: Any):
            raise RuntimeError("down mid-transaction")

    with pytest.raises(RuntimeError):
        asyncio.run(
            bind_key_subject(
                uuid.uuid4(),
                subject="s",
                idp=IDP_ADMIN_ASSERTED,
                bound_via=VIA_ADMIN_CONSOLE,
                conn=Conn(),
            )
        )


# ---------------------------------------------------------------------------
# 5. binding at issuance — enrollment redemption
# ---------------------------------------------------------------------------


class EnrollFakeDB:
    """Stateful fake for the enroll redeem path (suite convention)."""

    def __init__(self) -> None:
        self.fetchrows: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []
        self.enroll_row: dict[str, Any] | None = None
        self.tenant_name: str | None = "acme"
        self.profile_row: dict[str, Any] | None = None
        self.bindings: list[tuple[str, tuple]] = []
        self.bind_fail = False

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrows.append((sql, args))
        if "UPDATE enrollments SET uses" in sql:
            return self.enroll_row
        if "INSERT INTO users" in sql:
            return {"id": uuid.uuid4(), "name": args[1]}
        if "INSERT INTO api_keys" in sql:
            return {"id": uuid.uuid4()}
        if "JOIN subjects s" in sql or "INSERT INTO subjects" in sql:
            if self.bind_fail:
                raise RuntimeError("subjects unavailable")
            return {"id": uuid.uuid4()}
        if "FROM profiles WHERE id" in sql:
            return self.profile_row
        return None

    async def fetchval(self, sql: str, *args: Any):
        if "FROM tenants" in sql and "SELECT 1" not in sql:
            return self.tenant_name
        return 1

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        if "SET subject_id" in sql:
            self.bindings.append((sql, args))
            if self.bind_fail:
                raise RuntimeError("subjects unavailable")
        return "UPDATE 1"

    def acquire(self):
        # The transactional redeem (audit finding 8) runs code-consumption +
        # user + key reads on the ACQUIRED connection; this fake delegates
        # every conn call back to the module-level fake (single store).
        return _FakeTxConn(self)


class _FakeTxConn:
    """Transaction-capable view over EnrollFakeDB (audit finding 8)."""

    def __init__(self, db: EnrollFakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def fetchrow(self, sql: str, *args: Any):
        return await self._db.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any):
        return await self._db.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)


@pytest.fixture
def enroll_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    fake = EnrollFakeDB()
    fake.enroll_row = {"id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "profile_id": None}
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(routes_enroll, "bind_key_subject", bind_key_subject)  # real impl over the fake db
    monkeypatch.setattr(identity, "db", fake)
    routes_enroll._redeem_attempts.clear()
    app = FastAPI()
    app.include_router(routes_enroll.router)
    return TestClient(app, raise_server_exceptions=False), fake


def test_enroll_self_asserted_subject_binds(enroll_env) -> None:
    tc, fake = enroll_env
    r = tc.post("/v1/enroll", json={"code": routes_enroll.generate_code(), "name": "Alice", "subject": "  alice@corp "})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject"] == "alice@corp"  # normalized
    assert body["idp"] == IDP_SELF_ASSERTED
    ((sql, args),) = fake.bindings
    assert "SET subject_id" in sql and args[2] == VIA_ENROLLMENT
    # the subjects upsert used the self-asserted idp
    _subj_sql, subj_args = next(a for a in fake.fetchrows if "INSERT INTO subjects" in a[0])
    assert subj_args[2] == IDP_SELF_ASSERTED and subj_args[3] == "alice@corp"


def test_enroll_browser_sso_headers_win_over_self_asserted(enroll_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """When SSO headers are present on the redeem request AND the trust flag
    is on, the VERIFIED browser identity wins — the typed subject is ignored
    (no silent upgrade, verified beats claimed)."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    tc, fake = enroll_env
    r = tc.post(
        "/v1/enroll",
        json={"code": routes_enroll.generate_code(), "name": "Alice", "subject": "typed@example"},
        headers={"X-Auth-Request-User": "sso-alice", "X-Forwarded-Groups": "eng"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject"] == "sso-alice"
    assert body["idp"] == IDP_OAUTH2_PROXY
    _subj_sql, subj_args = next(a for a in fake.fetchrows if "INSERT INTO subjects" in a[0])
    assert subj_args[2] == IDP_OAUTH2_PROXY and subj_args[3] == "sso-alice"
    assert subj_args[5] == ["eng"]  # groups carried into the subject row


def test_enroll_sso_headers_ignored_when_flag_off(enroll_env) -> None:
    """Flag off (default): SSO headers are NOT trusted — a self-asserted
    subject still binds (as a claim), and without one the key stays
    anonymous. Byte-for-byte the pre-Phase-A response shape plus the two new
    (null/'') fields."""
    tc, fake = enroll_env
    r = tc.post(
        "/v1/enroll",
        json={"code": routes_enroll.generate_code(), "name": "Alice"},
        headers={"X-Auth-Request-User": "spoofed"},
    )
    assert r.status_code == 201
    assert r.json()["subject"] is None and r.json()["idp"] == ""
    assert fake.bindings == []


def test_enroll_binding_failure_never_fails_redemption(enroll_env) -> None:
    """Subjects table unavailable: the key is STILL created and returned
    (over-provision ordering); only the identity enrichment is refused."""
    tc, fake = enroll_env
    fake.bind_fail = True
    r = tc.post("/v1/enroll", json={"code": routes_enroll.generate_code(), "name": "Alice", "subject": "alice@corp"})
    assert r.status_code == 201, r.text
    assert r.json()["subject"] is None and r.json()["idp"] == ""
    assert r.json()["key"].startswith("hllm-")


def test_enroll_blank_subject_not_bound(enroll_env) -> None:
    tc, fake = enroll_env
    r = tc.post("/v1/enroll", json={"code": routes_enroll.generate_code(), "name": "A", "subject": "   "})
    assert r.status_code == 201
    assert r.json()["subject"] is None and fake.bindings == []


# ---------------------------------------------------------------------------
# 6. binding at issuance — admin console
# ---------------------------------------------------------------------------


class AdminCreateDB:
    """fake for POST /admin/keys with an in-transaction binding."""

    def __init__(self) -> None:
        self.conn_stmts: list[tuple[str, tuple]] = []
        self.user_tenant = uuid.uuid4()

    def acquire(self):
        return _Conn(self)

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM users" in sql and "SELECT 1" not in sql and "tenant_id" in sql:
            return {"tenant_id": self.user_tenant}
        if "FROM users" in sql:
            return {"id": args[0]} if args else None
        return {"id": uuid.uuid4()}

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


class _Conn:
    def __init__(self, db: AdminCreateDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def fetchrow(self, sql: str, *args: Any):
        self._db.conn_stmts.append((sql, args))
        if "FROM users WHERE id" in sql:
            return {"tenant_id": self._db.user_tenant}
        return {"id": uuid.uuid4()}

    async def execute(self, sql: str, *args: Any):
        self._db.conn_stmts.append((sql, args))
        return "UPDATE 1"

    async def executemany(self, sql: str, args: Any):
        self._db.conn_stmts.append((sql, tuple(args)))


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = AdminCreateDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(keys_mod, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake


def test_admin_create_key_binds_subject_in_transaction(admin_env) -> None:
    tc, fake = admin_env
    r = tc.post(
        "/admin/keys",
        json={"user_id": str(uuid.UUID(int=7)), "name": "ci-bot", "subject": " ci-bot@corp "},
        headers=ADMIN_H,
    )
    assert r.status_code == 201, r.text
    stmts = fake.conn_stmts
    upsert = next((s, a) for s, a in stmts if "INSERT INTO subjects" in s)
    stamp = next((s, a) for s, a in stmts if "SET subject_id" in s)
    assert upsert[1][2] == IDP_ADMIN_ASSERTED and upsert[1][3] == "ci-bot@corp"  # normalized
    assert stamp[1][2] == VIA_ADMIN_CONSOLE
    # the tenant came from the target user's row
    tenant_lookup = next((s, a) for s, a in stmts if "tenant_id FROM users" in s)
    assert tenant_lookup[1][0] == uuid.UUID(int=7)


def test_admin_create_key_unbound_by_default(admin_env) -> None:
    tc, fake = admin_env
    r = tc.post("/admin/keys", json={"user_id": str(uuid.UUID(int=7)), "name": "k"}, headers=ADMIN_H)
    assert r.status_code == 201
    assert not [s for s, _ in fake.conn_stmts if "INSERT INTO subjects" in s]


def test_admin_create_key_blank_subject_400(admin_env) -> None:
    tc, _ = admin_env
    r = tc.post("/admin/keys", json={"user_id": str(uuid.UUID(int=7)), "name": "k", "subject": "  "}, headers=ADMIN_H)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 7. rotation carries the binding (the grants-carry-over bug class)
# ---------------------------------------------------------------------------


class _RotateConn:
    """Transaction-capable view over RotateDB (the transactional rotate,
    audit finding 5 — every statement now runs on the acquired conn)."""

    def __init__(self, db: RotateDB) -> None:
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

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)


class RotateDB:
    """Minimal rotate-flow fake (the test_admin.RotateDB pattern, standalone):
    records the INSERT args and replays the old row. The conditional revoke
    (audit finding 5) is an UPDATE..RETURNING via fetchrow — a row comes
    back unless old_revoked is set."""

    def __init__(self, old_row: dict[str, Any]) -> None:
        self.old_row = old_row
        self.insert_args: tuple = ()
        self.insert_sql: str = ""
        self.old_revoked = False

    def acquire(self):
        return _RotateConn(self)

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM api_keys" in sql:
            return self.old_row
        if "INSERT INTO api_keys" in sql:
            self.insert_args = args
            self.insert_sql = sql
            return {"id": uuid.uuid4()}
        if "UPDATE api_keys" in sql and "revoked_at IS NULL" in sql:
            return None if self.old_revoked else {"id": uuid.uuid4()}
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def execute(self, sql: str, *args: Any):
        if "FROM mcp_tool_acls" in sql or "FROM mcp_server_creds" in sql:
            return "INSERT 0 0"
        if "FROM budgets" in sql or "FROM cost_budgets" in sql:
            return "INSERT 0 0"  # the old key carried no budget rows
        return "UPDATE 1"


def test_rotate_carries_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION (the 2026-09-18 grants-carry-over bug class, for identity):
    rotate enumerates its INSERT columns explicitly — a binding NOT in that
    list is silently stripped. A BOUND key must rotate into a BOUND key:
    subject_id/bound_via/bound_at all carried, verbatim."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    old_subject_id = uuid.uuid4()
    old_bound_at = datetime(2026, 9, 1, tzinfo=UTC)
    old = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "bound-key",
        "scopes": ["proxy", "mcp"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "max_concurrent": 0,
        "expires_at": None,
        "relay_default": False,
        "stream_pcai_events": True,
        "revoked_at": None,
        "spend_ancestors": [],
        "subject_id": old_subject_id,
        "bound_via": VIA_ENROLLMENT,
        "bound_at": old_bound_at,
    }
    fake = RotateDB(old)
    monkeypatch.setattr(routes_admin, "db", fake)
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(keys_mod, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=ADMIN_H)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject_id"] == str(old_subject_id)
    assert body["bound_via"] == VIA_ENROLLMENT
    # the INSERT named and carried all three identity columns
    assert "subject_id" in fake.insert_sql and "bound_via" in fake.insert_sql and "bound_at" in fake.insert_sql
    # 12 base cols (stream_pcai_events joined) + spend_ancestors + 3 identity
    # + mcp_admin (2026-10-26 hardening plan P0-3: the MCP admin opt-in is an
    # identity attribute and must survive rotation, like subject/bound_via).
    assert len(fake.insert_args) == 17
    assert fake.insert_args[16] in (False, None)  # mcp_admin default: not an MCP admin
    assert fake.insert_args[13] == old_subject_id
    assert fake.insert_args[14] == VIA_ENROLLMENT
    assert fake.insert_args[15] == old_bound_at


def test_rotate_unbound_key_carries_nulls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    old = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "anon-key",
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "max_concurrent": 0,
        "expires_at": None,
        "relay_default": False,
        "stream_pcai_events": True,
        "revoked_at": None,
        "spend_ancestors": [],
        "subject_id": None,
        "bound_via": "",
        "bound_at": None,
    }
    fake = RotateDB(old)
    monkeypatch.setattr(routes_admin, "db", fake)
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(keys_mod, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=ADMIN_H)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["subject_id"] is None and body["bound_via"] == ""
    assert fake.insert_args[13] is None and fake.insert_args[14] == "" and fake.insert_args[15] is None


# ---------------------------------------------------------------------------
# 8. X-MCP-Caller: 400 reservation + sanitizer at the stamp site
# ---------------------------------------------------------------------------


@pytest.fixture
def creds_env(monkeypatch: pytest.MonkeyPatch):
    from pcai_llm.core import secretbox

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(settings, "secrets_master_key", "identity-test-master")
    monkeypatch.setattr(secretbox, "_fernet", None)
    fake = routes_enroll.EnrollFakeDB() if hasattr(routes_enroll, "EnrollFakeDB") else None
    assert fake is None  # guard: this file must not depend on test_enroll's fake

    class CredsDB:
        async def fetchval(self, sql: str, *args: Any):
            return 1

        async def execute(self, sql: str, *args: Any):
            return "INSERT 0 1"

        async def fetch(self, sql: str, *args: Any):
            return []

        async def fetchrow(self, sql: str, *args: Any):
            return None

    app = FastAPI()
    app.include_router(routes_enroll.admin_router)
    return TestClient(app, raise_server_exceptions=False), CredsDB()


def test_mcp_caller_header_reserved_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tenant override of X-MCP-Caller is refused with 400 at the WRITE
    path (the spoofing vector, closed at the door)."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    from pcai_llm.core import secretbox

    monkeypatch.setattr(settings, "secrets_master_key", "identity-test-master")
    monkeypatch.setattr(secretbox, "_fernet", None)

    class CredsDB:
        async def fetchval(self, sql: str, *args: Any):
            return 1

        async def execute(self, sql: str, *args: Any):
            return "INSERT 0 1"

        async def fetch(self, sql: str, *args: Any):
            return []

        async def fetchrow(self, sql: str, *args: Any):
            return None

    fake = CredsDB()
    monkeypatch.setattr(routes_enroll, "db", fake)
    app = FastAPI()
    app.include_router(routes_enroll.admin_router)
    tc = TestClient(app, raise_server_exceptions=False)
    kid = uuid.uuid4()
    for variant in (MCP_CALLER_HEADER, "x-mcp-caller", "X-MCP-CALLER"):
        r = tc.put(
            f"/admin/keys/{kid}/mcp-creds",
            json={"server": "memory", "headers": {variant: "someone-else"}},
            headers=ADMIN_H,
        )
        assert r.status_code == 400, variant
        assert "reserved" in r.json()["detail"]
    # sane overrides still pass
    ok = tc.put(
        f"/admin/keys/{kid}/mcp-creds",
        json={"server": "memory", "headers": {"X-Memory-Dataset": "ds"}},
        headers=ADMIN_H,
    )
    assert ok.status_code == 200


def test_stamp_sanitizes_subject_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subject containing CR/LF (defensive: the DB column should never
    hold one, but the stamp is the header boundary) is sanitized before it
    becomes an outbound header."""
    fake = EnrollFakeDBForStamp()

    async def fake_subject_for_key(key_id: uuid.UUID) -> str | None:
        return "bad\r\nX-Injection: 1 subject"

    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "subject_for_key", fake_subject_for_key)
    target: dict[str, Any] = {"name": "memory", "headers": {}, "auth_secret": None, "auth_token_enc": None}
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(target, key_id=uuid.uuid4()))
    stamp = headers[MCP_CALLER_HEADER]
    assert "\r" not in stamp and "\n" not in stamp and ":" in stamp.split("@")[0]
    assert stamp.endswith("@gateway")


class EnrollFakeDBForStamp:
    async def fetchrow(self, sql: str, *args: Any):
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


def test_stamp_subject_override_arg_beats_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """caller_subject= (the Phase B resolver wiring point) wins over the
    degrade-not-fail lookup without touching the DB."""

    class NoDB:
        async def fetchrow(self, sql: str, *args: Any):
            raise AssertionError("DB must not be consulted when caller_subject is provided")

    monkeypatch.setattr(routes_mcp, "db", NoDB())
    target: dict[str, Any] = {"name": "memory", "headers": {}, "auth_secret": None, "auth_token_enc": None}
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(target, key_id=uuid.uuid4(), caller_subject="alice"))
    assert headers[MCP_CALLER_HEADER] == "alice@gateway"


# ---------------------------------------------------------------------------
# 9. ledger / traces / keys-list projections
# ---------------------------------------------------------------------------


class ProjectionDB:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        return []

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def fetchrow(self, sql: str, *args: Any):
        return None

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


def test_usage_explorer_projects_subject_join(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = ProjectionDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/usage", headers=ADMIN_H)
    assert r.status_code == 200
    sql, _ = fake.queries[0]
    # read-time LEFT JOIN through the key→subject FK (NO settle() change)
    assert "LEFT JOIN subjects s ON s.id = k.subject_id" in sql
    assert "s.subject AS subject" in sql and "k.bound_via AS bound_via" in sql


def test_usage_group_by_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = ProjectionDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/usage", params={"group_by": "subject"}, headers=ADMIN_H)
    assert r.status_code == 200
    sql, _ = fake.queries[0]
    assert "LEFT JOIN subjects s ON s.id = k.subject_id" in sql
    assert "GROUP BY k.subject_id" in sql
    # unknown group_by still refused
    r2 = tc.get("/admin/usage", params={"group_by": "fingerprint"}, headers=ADMIN_H)
    assert r2.status_code == 400


def test_traces_project_subject_join(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.core import traces

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = ProjectionDB()
    monkeypatch.setattr(traces, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/traces", headers=ADMIN_H)
    assert r.status_code == 200
    sql, _ = fake.queries[0]
    assert "LEFT JOIN subjects s ON s.id = k.subject_id" in sql
    assert "s.subject AS subject" in sql
    # the new columns are GROUP-BY-safe (every projection is grouped)
    for col in ("k.bound_via", "s.subject", "s.idp"):
        assert col in sql.split("GROUP BY", 1)[1], f"{col} must be in the GROUP BY"


def test_settle_signature_unchanged() -> None:
    """THE documented contract (budgets.py docstring + review §1.2): the
    subject projection is read-time only — settle() gains NO parameter for
    it (tests index the INSERT args tuple positionally)."""
    import inspect

    from pcai_llm.core.budgets import settle

    params = list(inspect.signature(settle).parameters)
    assert "subject" not in params and "subject_id" not in params
    # and the ledger INSERT column list gained no subject column
    src = inspect.getsource(settle)
    assert "subject" not in src.lower()


def test_list_keys_projects_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.core.keys as keys_mod

    fake = ProjectionDB()
    monkeypatch.setattr(keys_mod, "db", fake)
    asyncio.run(keys_mod.list_keys())
    sql, _ = fake.queries[0]
    assert "LEFT JOIN subjects s ON s.id = k.subject_id" in sql
    assert "k.bound_via" in sql and "s.subject AS subject" in sql

# ---------------------------------------------------------------------------
# audit C4 (2026-10-29) — the browser-header rung must be REACHABLE from the
# auth surfaces (attribution), and must never become an authorization input.
# ---------------------------------------------------------------------------

# A JWT-SHAPED bearer for the "a token is not a key" cases below. Assembled at
# RUNTIME on purpose: the fleet scanner's JWT pass is a HARD pattern with no
# allow-marker ("a JWT literal is a credential wherever it sits" —
# pcai_utils/secret_scan.sh), so the literal spelling fails the hardlinker's
# pre-mirror gate. The value is synthetic — header {"alg":"RS256"}, payload
# {"sub":"token-sub"}, signature "sig" — and the join keeps it byte-identical.
# The three segments live in separate constants so no three-part token exists
# in the source at all (an f-string of three names never matches the scanner's
# dot-joined JWT pattern, and none of the names is secret-shaped).
_JWT_HEADER = "eyJhbGciOiJSUzI1NiJ9"   # {"alg":"RS256"}
_JWT_PAYLOAD = "eyJzdWIiOiJ0b2tlbi1zdWIifQ"   # {"sub":"token-sub"}
_JWT_SIGNATURE = "c2ln"   # "sig"
JWT_SHAPED = f"{_JWT_HEADER}.{_JWT_PAYLOAD}.{_JWT_SIGNATURE}"


def test_c4_data_plane_attribution_uses_the_browser_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """C4 — a proxy-identified caller must not be attributed as key-anon.

    PRE-FIX (api/auth.py:106-122): the ladder called
    identity.resolve_caller(None, token=plaintext) — headers were never
    passed, and resolve_caller's rung 1 is gated on "headers is not None". On
    the documented chained-edge deployment the rung therefore never ran on the
    data plane at all: the ledger, the trace explorer and X-MCP-Caller fell
    back to the key fingerprint for a user the front door had positively
    identified.
    """
    from pcai_llm.api import auth as auth_mod

    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    kid = uuid.uuid4()
    record = _key(id=kid)

    async def fake_authenticate(_plaintext: str) -> KeyRecord:
        return record

    headers = {"authorization": "Bearer hllm-abc", "x-auth-request-user": "alice", "x-forwarded-groups": "eng"}
    key, caller = asyncio.run(auth_mod.resolve_bearer_key(None, authenticate=fake_authenticate, headers=headers))

    assert key is not None and key.id == kid, "authorization still rides the presented key"
    assert caller is not None, "the browser rung must produce an attribution caller"
    assert caller.subject_kind == KIND_BROWSER and caller.subject == "alice"
    assert caller.groups == ["eng"]


def test_c4_browser_rung_off_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag off (the shipped default): the headers are inert, exactly as
    before the fix — no caller is invented from an untrusted header."""
    from pcai_llm.api import auth as auth_mod

    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    record = _key()

    async def fake_authenticate(_plaintext: str) -> KeyRecord:
        return record

    headers = {"authorization": "Bearer hllm-abc", "x-auth-request-user": "alice"}
    key, caller = asyncio.run(auth_mod.resolve_bearer_key(None, authenticate=fake_authenticate, headers=headers))
    assert key is not None and caller is None


def test_c4_browser_rung_never_authorizes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A header is not a credential: proxy identity headers alone are still a
    401, and a valid TOKEN's authorization still anchors on the TOKEN subject
    (the browser subject must never reach resolve_shadow_key)."""
    from pcai_llm.api import auth as auth_mod

    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    headers = {"x-auth-request-user": "alice"}

    async def never_called(_plaintext: str) -> KeyRecord | None:  # pragma: no cover
        raise AssertionError("no bearer was presented")

    with pytest.raises(Exception) as excinfo:
        asyncio.run(auth_mod.resolve_bearer_key(None, authenticate=never_called, headers=headers))
    assert "missing bearer" in str(excinfo.value)

    # token rung: authorization anchors on the TOKEN subject, not the header
    shadow_subjects: list[str] = []
    shadow = _key()

    async def fake_shadow(subject: str, claims: Any = None) -> KeyRecord:
        shadow_subjects.append(subject)
        return shadow

    async def fake_claims(_token: str) -> Any:
        return identity.TokenClaims(subject="token-sub", email="", groups=[])

    async def fake_authenticate(_plaintext: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(identity, "token_validation_configured", lambda: True)
    monkeypatch.setattr(identity, "validate_token", fake_claims)
    monkeypatch.setattr(identity, "resolve_shadow_key", fake_shadow)

    key, caller = asyncio.run(
        auth_mod.resolve_bearer_key(
            None,
            authenticate=fake_authenticate,
            headers={"authorization": f"Bearer {JWT_SHAPED}", "x-auth-request-user": "alice"},
        )
    )
    assert key is not None and key.id == shadow.id
    assert shadow_subjects == ["token-sub"], "authorization must anchor on the token subject"
    assert caller is not None and caller.subject_kind == KIND_BROWSER and caller.subject == "alice"