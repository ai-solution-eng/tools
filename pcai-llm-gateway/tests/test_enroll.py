"""Enrollment tests: public self-service redeem + admin code management.

The enroll door is unauthenticated BY DESIGN (the code is the credential),
so these tests pin its defenses instead: 32-byte codes stored hash-only,
ONE generic rejection message for every dead-code shape (no oracle), the
per-IP redeem throttle, and the single-statement atomic redeem. The admin
surface (mint/list/revoke) is pinned for its once-only code disclosure.

The memory-dataset section (review §2) pins the opt-in matrix (request →
profile preset → deployment default), the never-fatal provisioning
contract (a dead RAG still mints the key), the naming convention, the
409 bounded-suffix retry, the Fernet credential stamp, and the SSRF
posture (no free-form URL — the registry row is the only URL source).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import asyncpg
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_enroll
from pcai_llm.config import settings
from pcai_llm.core import enroll_memory, secretbox
from pcai_llm.core.keys import authenticate, hash_key

ADMIN_H = {"Authorization": "Bearer admin-secret-123"}


class _FakeConn:
    """Stands in for an acquired asyncpg connection (transaction +
    executemany + the reads the transactional redeem runs on the conn: the
    code-consumption UPDATE and the profile-seed SELECT — the redeem
    transaction (audit finding 8) runs them on the ACQUIRED connection, so
    the fake must dispatch them the same way)."""

    def __init__(self, db: EnrollFakeDB) -> None:
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


class EnrollFakeDB:
    """Stateful fake for the enroll path (suite convention: dispatch on SQL
    shape). Records every statement so tests can assert the redeem contract
    and the profile-seeding writes; models the reads the flow makes."""

    def __init__(self) -> None:
        self.fetchrows: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []
        self.bulk: list[tuple[str, list]] = []
        # What the atomic redeem UPDATE returns; None = dead code.
        self.enroll_row: dict[str, Any] | None = None
        self.tenant_name: str | None = "acme"
        self.tenant_exists = True
        self.profile_row: dict[str, Any] | None = None
        self.profile_exists = True
        self.enrollment_exists = True
        self.user_conflicts = 0  # UniqueViolationError raises left on user INSERT
        self.user_names: list[str] = []
        self.api_keys: dict[str, dict[str, Any]] = {}  # key_hash -> authenticate row
        self.enrollments: list[dict[str, Any]] = []
        # mcp_server_creds upserts written by the caller-subject stamp
        # (_patch_stamp_db teaches the execute seam below): (key_id, server)
        # -> encrypted credential blob.
        self._creds: dict[tuple[uuid.UUID, str], str] = {}

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrows.append((sql, args))
        if "UPDATE enrollments SET uses" in sql:
            return self.enroll_row
        if "INSERT INTO users" in sql:
            if self.user_conflicts > 0:
                self.user_conflicts -= 1
                raise asyncpg.UniqueViolationError("duplicate key")
            self.user_names.append(args[1])
            return {"id": uuid.uuid4(), "name": args[1]}
        if "INSERT INTO api_keys" in sql:
            # (user_id, name, key_hash, key_prefix, scopes, allowed_models, rpm, tpm, expires_at)
            row = {
                "id": uuid.uuid4(),
                "user_id": args[0],
                "name": args[1],
                "key_prefix": args[3],
                "scopes": list(args[4]),
                "allowed_models": list(args[5]),
                "rate_limit_rpm": args[6],
                "rate_limit_tpm": args[7],
                "max_concurrent": 0,
                "expires_at": None,
                "revoked_at": None,
                "relay_default": False,
                "stream_pcai_events": True,
                "offpeak_fraction": None,
                "spend_ancestors": list(args[11]) if len(args) >= 12 else [],
            }
            self.api_keys[args[2]] = row
            return {"id": row["id"]}
        if "FROM api_keys" in sql and "key_hash" in sql:
            return self.api_keys.get(args[0])
        if "FROM profiles WHERE id" in sql:
            return self.profile_row if self.profile_exists else None
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
                "created_at": datetime.now(UTC),
            }
        return None

    async def fetchval(self, sql: str, *args: Any):
        if "FROM tenants" in sql:
            if "SELECT 1" in sql:
                return 1 if self.tenant_exists else None
            return self.tenant_name
        if "FROM enrollments" in sql:
            return 1 if self.enrollment_exists else None
        if "FROM profiles" in sql:
            return 1 if self.profile_exists else None
        if "FROM api_keys" in sql:
            return 1
        return 1

    async def fetch(self, sql: str, *args: Any):
        if "FROM enrollments e" in sql:
            return list(self.enrollments)
        return []

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        return "UPDATE 1"

    def acquire(self):
        return _FakeConn(self)

    def redeem_stmts(self) -> list[tuple[str, tuple]]:
        return [(s, a) for s, a in self.fetchrows if "UPDATE enrollments SET uses" in s]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = EnrollFakeDB()
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)  # create_key + authenticate seams
    routes_enroll._redeem_attempts.clear()
    app = FastAPI()
    app.include_router(routes_enroll.router)
    app.include_router(routes_enroll.admin_router)
    return TestClient(app, raise_server_exceptions=False), fake


def _code() -> str:
    return routes_enroll.generate_code()


def _redeemed(profile_id: uuid.UUID | None) -> dict[str, Any]:
    return {"id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "profile_id": profile_id}


PROFILE_DEFAULTS = {
    "budgets": [{"model": "*", "period": "month", "limit_tokens": 100_000}],
    "mcp_grants": [["logsearch", "search_logs"]],
}


# ---------------------------------------------------------------------------
# public redeem
# ---------------------------------------------------------------------------


def test_enroll_happy_path_key_authenticates(env) -> None:
    tc, fake = env
    pid = uuid.uuid4()
    fake.enroll_row = _redeemed(pid)
    fake.profile_row = {"id": pid, "name": "starter", "defaults_json": json.dumps(PROFILE_DEFAULTS)}
    code = _code()

    r = tc.post("/v1/enroll", json={"code": code, "name": "  Alice  "})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["key"].startswith("hllm-")
    assert body["key_prefix"] == body["key"][:10]
    assert body["user"] == "Alice"  # stripped
    assert body["tenant"] == "acme"
    assert body["budgets"] == PROFILE_DEFAULTS["budgets"]
    assert "shown once" in body["message"]

    # the returned key WORKS: authenticate() resolves it with enroll scopes
    rec = asyncio.run(authenticate(body["key"]))
    assert rec is not None
    assert rec.scopes == ["proxy", "mcp"]
    assert rec.name == "Alice"

    # profile presets were stamped onto the key (budgets + mcp grants)
    assert len(fake.bulk) == 2
    (b_sql, b_rows), (g_sql, g_rows) = fake.bulk
    assert "INSERT INTO budgets" in b_sql
    assert b_rows == [(rec.id, "*", "month", 100_000)]
    assert "INSERT INTO mcp_tool_acls" in g_sql
    assert g_rows == [(rec.id, "logsearch", "search_logs")]


def test_enroll_atomic_redeem_statement(env) -> None:
    """The redeem is ONE guarded UPDATE: concurrent redeemers serialize on
    the row lock; every guard lives in the WHERE so no read-modify-write
    race exists, and the lookup key is the HASH (plaintext never a SQL arg)."""
    tc, fake = env
    fake.enroll_row = _redeemed(None)
    code = _code()
    assert tc.post("/v1/enroll", json={"code": code, "name": "A"}).status_code == 201

    stmts = fake.redeem_stmts()
    assert len(stmts) == 1, "redeem must be exactly one statement"
    sql, args = stmts[0]
    assert "uses = uses + 1" in sql
    assert "revoked_at IS NULL" in sql
    assert "(expires_at IS NULL OR expires_at > now())" in sql
    assert "(max_uses = 0 OR uses < max_uses)" in sql
    assert "RETURNING id, tenant_id, profile_id" in sql
    assert args[0] == hash_key(code)
    assert code not in args


def test_enroll_dead_codes_all_identical_400(env) -> None:
    """expired / exhausted / revoked / unknown / malformed — one generic
    message, byte-identical bodies (no state oracle for code brute-forcers)."""
    tc, fake = env
    fake.enroll_row = None
    r1 = tc.post("/v1/enroll", json={"code": _code(), "name": "A"})
    r2 = tc.post("/v1/enroll", json={"code": "enroll-totally-made-up", "name": "A"})
    r3 = tc.post("/v1/enroll", json={"code": "not-even-the-right-shape", "name": "A"})
    assert r1.status_code == r2.status_code == r3.status_code == 400
    assert r1.json() == r2.json() == r3.json()
    assert r1.json()["detail"] == routes_enroll._GENERIC_CODE_REJECTION


def test_enroll_malformed_code_never_touches_db(env) -> None:
    tc, fake = env
    r = tc.post("/v1/enroll", json={"code": "garbage", "name": "A"})
    assert r.status_code == 400
    assert fake.redeem_stmts() == []


def test_enroll_throttles_after_ten_attempts_per_ip(env) -> None:
    tc, fake = env
    fake.enroll_row = None
    for _ in range(10):
        assert tc.post("/v1/enroll", json={"code": _code(), "name": "A"}).status_code == 400
    r11 = tc.post("/v1/enroll", json={"code": _code(), "name": "A"})
    assert r11.status_code == 429
    assert "Retry-After" in r11.headers
    # per-IP: a different address still reaches the redeem path
    other = tc.post(
        "/v1/enroll",
        json={"code": _code(), "name": "A"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert other.status_code == 400


def test_enroll_name_validation(env) -> None:
    tc, fake = env
    fake.enroll_row = _redeemed(None)
    assert tc.post("/v1/enroll", json={"code": _code(), "name": "   "}).status_code == 422
    assert tc.post("/v1/enroll", json={"code": _code(), "name": "x" * 65}).status_code == 422
    assert tc.post("/v1/enroll", json={"code": _code(), "name": "x" * 64}).status_code == 201
    assert tc.post("/v1/enroll", json={"code": _code(), "name": "a"}).status_code == 201


def test_enroll_duplicate_user_name_gets_suffix(env) -> None:
    """users has UNIQUE (tenant_id, name) but keys are identity — a name
    collision must never fail someone's enrollment."""
    tc, fake = env
    fake.enroll_row = _redeemed(None)
    fake.user_conflicts = 1
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "Alice"})
    assert r.status_code == 201, r.text
    # first INSERT hit the UNIQUE (tenant_id, name) constraint and was
    # retried with a random suffix — exactly one user row landed
    assert len(fake.user_names) == 1
    assert fake.user_names[0].startswith("Alice-")
    assert r.json()["user"] == fake.user_names[0]


def test_enroll_without_profile_skips_seeding(env) -> None:
    tc, fake = env
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A"})
    assert r.status_code == 201
    assert r.json()["budgets"] == []
    assert fake.bulk == []


def test_enroll_vanished_profile_still_enrolls(env) -> None:
    """Profile deleted between mint and redeem: key succeeds, no presets."""
    tc, fake = env
    fake.enroll_row = _redeemed(uuid.uuid4())
    fake.profile_row = None  # gone
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A"})
    assert r.status_code == 201
    assert r.json()["budgets"] == []
    assert fake.bulk == []


# ---------------------------------------------------------------------------
# admin enrollment management
# ---------------------------------------------------------------------------


def test_admin_create_enrollment_code_shown_once(env) -> None:
    tc, fake = env
    tid = uuid.uuid4()
    pid = uuid.uuid4()
    r = tc.post(
        "/admin/enrollments",
        json={"label": "onboarding", "tenant_id": str(tid), "profile_id": str(pid), "max_uses": 5, "expires_days": 7},
        headers=ADMIN_H,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"].startswith("enroll-")
    assert "code_hash" not in body, "the hash never leaves the server"
    assert body["max_uses"] == 5 and body["uses"] == 0

    insert_args = next(a for s, a in fake.fetchrows if "INSERT INTO enrollments" in s)
    assert insert_args[0] == hash_key(body["code"])  # stored hashed, plaintext nowhere
    expires_at = insert_args[5]
    assert expires_at is not None
    assert (expires_at - datetime.now(UTC)).total_seconds() > 6 * 86400  # ~7 days out


def test_admin_create_enrollment_unknown_tenant_404(env) -> None:
    tc, fake = env
    fake.tenant_exists = False
    r = tc.post("/admin/enrollments", json={"label": "l", "tenant_id": str(uuid.uuid4())}, headers=ADMIN_H)
    assert r.status_code == 404


def test_admin_create_enrollment_unknown_profile_404(env) -> None:
    tc, fake = env
    fake.profile_exists = False
    r = tc.post(
        "/admin/enrollments",
        json={"label": "l", "tenant_id": str(uuid.uuid4()), "profile_id": str(uuid.uuid4())},
        headers=ADMIN_H,
    )
    assert r.status_code == 404


def test_admin_create_enrollment_negative_max_uses_422(env) -> None:
    tc, _ = env
    r = tc.post(
        "/admin/enrollments", json={"label": "l", "tenant_id": str(uuid.uuid4()), "max_uses": -1}, headers=ADMIN_H
    )
    assert r.status_code == 422


def test_admin_list_enrollments_no_code_material(env) -> None:
    tc, fake = env
    fake.enrollments = [
        {
            "id": uuid.uuid4(),
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
    r = tc.get("/admin/enrollments", headers=ADMIN_H)
    assert r.status_code == 200
    row = r.json()[0]
    assert row["tenant_name"] == "acme" and row["profile_name"] == "starter"
    assert row["uses"] == 3 and row["max_uses"] == 0
    assert "code" not in row and "code_hash" not in row


def test_admin_revoke_enrollment_idempotent(env) -> None:
    tc, fake = env
    eid = uuid.uuid4()
    r1 = tc.post(f"/admin/enrollments/{eid}/revoke", headers=ADMIN_H)
    r2 = tc.post(f"/admin/enrollments/{eid}/revoke", headers=ADMIN_H)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json() == {"revoked": True}
    fake.enrollment_exists = False
    assert tc.post(f"/admin/enrollments/{uuid.uuid4()}/revoke", headers=ADMIN_H).status_code == 404


def test_admin_enrollment_surface_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "")  # disabled -> 503
    app = FastAPI()
    app.include_router(routes_enroll.admin_router)
    tc = TestClient(app, raise_server_exceptions=False)
    assert tc.get("/admin/enrollments").status_code == 503

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    app2 = FastAPI()
    app2.include_router(routes_enroll.admin_router)
    tc2 = TestClient(app2, raise_server_exceptions=False)
    assert tc2.get("/admin/enrollments").status_code == 401  # missing bearer
    assert tc2.get("/admin/enrollments", headers={"Authorization": "Bearer nope"}).status_code == 401


# ---------------------------------------------------------------------------
# enrollment-time per-user memory datasets (review §2)
# ---------------------------------------------------------------------------

_MM_RAG_SERVER: dict[str, Any] = {
    "id": "srv-rag",
    "name": "mm-rag",
    "url": "http://rag-mcp-server-mcp.mm-rag.svc.cluster.local:9090/mcp",
    "headers": {},
    "auth_secret": "rag-key",
    "auth_token_enc": None,
    "enabled": True,
    "tool_policy": "all",
    "allowed_tools": [],
    "priority": 0,
}


class _MemoryHarness:
    """Stateful fake for the memory provisioning path: a registry snapshot,
    an httpx2 MockTransport standing in for RAG's REST API, and the
    mcp_server_creds rows the stamp writes.

    Response scripting: `status` (an int) answers every create; `script`
    (a list of ints) answers one call each in order and takes precedence
    while it has entries; `dead` raises ConnectError instead of answering.
    """

    def __init__(self) -> None:
        self.registry: list[dict[str, Any]] = [dict(_MM_RAG_SERVER)]
        self.status = 201
        self.script: list[int] = []
        self.dead = False
        self.calls: list[dict[str, Any]] = []

    def transport(self) -> httpx2.MockTransport:
        h = self

        def handler(request: httpx2.Request) -> httpx2.Response:
            h.calls.append(
                {
                    "url": str(request.url),
                    "headers": dict(request.headers),
                    "body": json.loads(request.content.decode() or "{}"),
                }
            )
            if h.dead:
                raise httpx2.ConnectError("refused")
            status = h.script.pop(0) if h.script else h.status
            return httpx2.Response(status, json={"status": "ok"})

        return httpx2.MockTransport(handler)


@pytest.fixture
def mem_env(monkeypatch: pytest.MonkeyPatch, env):
    """The enroll env plus the memory seams: registry snapshot, RAG
    MockTransport, master key, and default settings."""
    monkeypatch.setattr(settings, "secrets_master_key", "enroll-memory-master")
    monkeypatch.setattr(secretbox, "_fernet", None)
    monkeypatch.setattr(settings, "enroll_memory_default", False)
    monkeypatch.setattr(settings, "enroll_memory_server", "mm-rag")
    monkeypatch.setattr(settings, "enroll_memory_name_retries", 3)
    h = _MemoryHarness()
    monkeypatch.setattr(enroll_memory.registry, "_mcp_servers_cache", h.registry)
    _patch_transport(monkeypatch, h)
    _patch_stamp_db(monkeypatch, fake=env[1])
    return env, h


def _patch_transport(monkeypatch: pytest.MonkeyPatch, h: _MemoryHarness) -> None:
    """Every httpx2.AsyncClient the provisioning path opens rides the
    harness's MockTransport (args/kwargs otherwise untouched). NOTE: the
    factory binds the REAL class BEFORE the module attribute is swapped —
    `enroll_memory.httpx2` IS the shared httpx2 module, so a factory that
    resolved AsyncClient through the module at call time would recurse."""
    real_cls = httpx2.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx2.AsyncClient:
        kwargs.pop("transport", None)
        return real_cls(*args, transport=h.transport(), **kwargs)

    monkeypatch.setattr(enroll_memory.httpx2, "AsyncClient", factory)


def _patch_stamp_db(monkeypatch: pytest.MonkeyPatch, fake: EnrollFakeDB) -> None:
    """Teach the enroll fake the mcp_server_creds upsert the stamp writes
    (the module's db seam is core.enroll_memory's own)."""

    fake._creds = {}

    async def execute(sql: str, *args: Any):
        fake.executed.append((sql, args))
        if sql.lstrip().startswith("INSERT INTO mcp_server_creds"):
            fake._creds[(args[0], args[1])] = args[2]
            return "INSERT 0 1"
        return "UPDATE 1"

    monkeypatch.setattr(fake, "execute", execute)
    monkeypatch.setattr(enroll_memory, "db", fake)


def _stamped_headers(fake: EnrollFakeDB, key_id: uuid.UUID) -> dict[str, str] | None:
    enc = getattr(fake, "_creds", {}).get((key_id, "mm-rag"))
    if enc is None:
        return None
    raw = secretbox.decrypt(enc)
    return (
        json.loads(raw) if raw else None
    )  # --- opt-in resolution (pure decision matrix) ------------------------------


def test_memory_opt_in_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Request > profile preset > deployment default — the tri-state from
    the review: None falls back, explicit values always win."""
    monkeypatch.setattr(settings, "enroll_memory_default", False)
    assert enroll_memory.memory_opt_in_requested(None, None) is False
    assert enroll_memory.memory_opt_in_requested(True, None) is True
    assert enroll_memory.memory_opt_in_requested(False, None) is False
    # profile preset beats the deployment default in BOTH directions
    assert enroll_memory.memory_opt_in_requested(None, {"memory": {"provision": True}}) is True
    monkeypatch.setattr(settings, "enroll_memory_default", True)
    assert enroll_memory.memory_opt_in_requested(None, None) is True
    assert enroll_memory.memory_opt_in_requested(None, {"memory": {"provision": False}}) is False
    assert enroll_memory.memory_opt_in_requested(False, {"memory": {"provision": True}}) is False
    # malformed profile shapes degrade to "not declared" (never provision)
    bad: Any
    for bad in (
        {"memory": "yes"},
        {"memory": {"provision": "true"}},
        {"memory": {}},
        "garbage",
    ):
        monkeypatch.setattr(settings, "enroll_memory_default", False)
        assert enroll_memory.memory_opt_in_requested(None, bad) is False, bad


def test_memory_naming_convention() -> None:
    """mem-<tenant>-<user>-<key6> — RAG-grammar-sanitized fragments, key6 =
    the first 6 hex of the key id (the collision-proof carrier)."""
    kid = uuid.UUID("1234567890abcdef1234567890abcdef")
    name = enroll_memory.dataset_name_for("acme", "Alice", kid)
    assert name == "mem-acme-Alice-123456"
    # two "Alice"s in different tenants are distinguishable by the key6 tail
    other = enroll_memory.dataset_name_for("acme", "Alice", uuid.UUID("f0000000000000000000000000000000"))
    assert other == "mem-acme-Alice-f00000" and other != name
    # grammar: every char is [A-Za-z0-9._-] (RAG's validator rejects the rest)
    messy = enroll_memory.dataset_name_for("tenant 4/2", "Zoë O'Brien", kid)
    assert messy.startswith("mem-tenant_4_2-")
    for ch in messy:
        assert ch.isalnum() or ch in "._-", ch


def test_memory_naming_truncation_keeps_key6() -> None:
    """A long tenant/user name must never eat the key6 tail — the key
    fragment is the uniqueness, the readable prefix is only a label."""
    kid = uuid.UUID("abcdef1234567890abcdef1234567890")
    name = enroll_memory.dataset_name_for("t" * 100, "u" * 100, kid)
    assert name.endswith("abcdef")
    assert len(name) <= 200


# --- the redeem-flow integration ------------------------------------------


def test_redeem_opt_in_true_provisions_and_stamps(mem_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """opt-in true → dataset created on the registered server's REST API
    with the server-level credential, and the pair stamped Fernet-encrypted
    onto THIS key's (key, server) creds row; the password rides the
    response exactly once."""
    (tc, fake), h = mem_env
    monkeypatch.setenv("PCAI_LLM_MCP_KEY__RAG_KEY", "rag-server-token")  # the by-ref server credential
    fake.enroll_row = _redeemed(None)
    _patch_stamp_db(monkeypatch, fake)
    code = _code()
    r = tc.post("/v1/enroll", json={"code": code, "name": "Alice", "memory_opt_in": True})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["memory"]["provisioned"] is True
    ds = body["memory"]["dataset"]
    assert ds.startswith("mem-acme-Alice-")
    # RAG received ONE create call with the pair, on the API surface
    # derived from the registered MCP URL (…/mcp stripped)
    assert len(h.calls) == 1
    call = h.calls[0]
    assert call["url"] == "http://rag-mcp-server-mcp.mm-rag.svc.cluster.local:9090/api/datasets"
    assert call["body"] == {"name": ds, "password": body["memory"]["password"]}
    # server-level auth (the gateway's own credential) rode the call
    assert call["headers"].get("authorization") == "Bearer rag-server-token"
    # stamped: Fernet ciphertext on THIS key's row for THIS server only
    rec = asyncio.run(authenticate(body["key"]))
    stamped = _stamped_headers(fake, rec.id)
    assert stamped is not None
    assert stamped == {"X-Memory-Dataset": ds, "X-Dataset-Password": body["memory"]["password"]}
    # the password appears ONCE in the response body (stored ciphertext is
    # not it) — and never in any admin listing the creds surface returns
    assert r.text.count(body["memory"]["password"]) == 1
    assert body["memory"]["password"] not in fake._creds[(rec.id, "mm-rag")]


def test_redeem_opt_in_false_provisions_nothing(mem_env) -> None:
    (tc, fake), h = mem_env
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A", "memory_opt_in": False})
    assert r.status_code == 201
    assert r.json()["memory"] == {"provisioned": False}
    assert h.calls == []
    assert not hasattr(fake, "_creds") or fake._creds == {}


def test_redeem_opt_in_absent_uses_deployment_default(mem_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """None (omitted) → PCAI_LLM_ENROLL_MEMORY_DEFAULT. Default false →
    nothing; default true → provisioned (the "default header flags in
    configuration" decision)."""
    (tc, fake), h = mem_env
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A"})
    assert r.status_code == 201
    assert r.json()["memory"] == {"provisioned": False}
    assert h.calls == []

    monkeypatch.setattr(settings, "enroll_memory_default", True)
    _patch_stamp_db(monkeypatch, fake)
    fake.enroll_row = _redeemed(None)
    r2 = tc.post("/v1/enroll", json={"code": _code(), "name": "B"})
    assert r2.status_code == 201
    assert r2.json()["memory"]["provisioned"] is True
    assert len(h.calls) == 1


def test_redeem_profile_memory_preset_applies_when_request_absent(mem_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The profile-level override ("memory": {"provision": true}) opts the
    cohort in when the request carries no explicit answer."""
    (tc, fake), h = mem_env
    pid = uuid.uuid4()
    fake.enroll_row = _redeemed(pid)
    fake.profile_row = {
        "id": pid,
        "name": "memory",
        "defaults_json": json.dumps({"budgets": [], "mcp_grants": [], "memory": {"provision": True}}),
    }
    _patch_stamp_db(monkeypatch, fake)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A"})
    assert r.status_code == 201
    assert r.json()["memory"]["provisioned"] is True
    assert len(h.calls) == 1
    # explicit false in the request beats the profile preset
    fake.enroll_row = _redeemed(pid)
    r2 = tc.post("/v1/enroll", json={"code": _code(), "name": "A2", "memory_opt_in": False})
    assert r2.status_code == 201
    assert r2.json()["memory"] == {"provisioned": False}
    assert len(h.calls) == 1


def test_redeem_rag_unreachable_key_still_issued(mem_env) -> None:
    """THE CONTRACT: provisioning failure NEVER fails the redemption. A
    dead RAG (transport error) reports provisioned=false + an opaque error
    and the key comes back live and authenticating."""
    (tc, fake), h = mem_env
    h.dead = True
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A", "memory_opt_in": True})
    assert r.status_code == 201, "redemption must survive a dead RAG"
    body = r.json()
    assert body["memory"]["provisioned"] is False
    assert body["memory"]["error"] == "ConnectError"
    rec = asyncio.run(authenticate(body["key"]))
    assert rec is not None and rec.scopes == ["proxy", "mcp"]


def test_redeem_rag_500_never_fails_redemption(mem_env, monkeypatch: pytest.MonkeyPatch) -> None:
    (tc, fake), h = mem_env
    h.status = 500
    _patch_stamp_db(monkeypatch, fake)
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A", "memory_opt_in": True})
    assert r.status_code == 201
    assert r.json()["memory"] == {"provisioned": False, "error": "HTTP 500"}


def test_redeem_memory_server_not_registered_reports(mem_env, monkeypatch: pytest.MonkeyPatch) -> None:
    (tc, fake), h = mem_env
    h.registry.clear()  # mm-rag disabled/absent from the registry snapshot
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A", "memory_opt_in": True})
    assert r.status_code == 201
    body = r.json()
    assert body["memory"]["provisioned"] is False
    assert "mm-rag" in body["memory"]["error"] and "registry" in body["memory"]["error"]
    assert h.calls == []


def test_redeem_memory_without_master_key_503_style_report(mem_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No Fernet master key = no place to stamp the credential — reported,
    never fatal, and NOTHING is created on RAG (no orphaned unprotected
    dataset): the stamp check runs BEFORE the create call."""
    (tc, fake), h = mem_env
    monkeypatch.setattr(settings, "secrets_master_key", "")
    monkeypatch.setattr(secretbox, "_fernet", None)
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A", "memory_opt_in": True})
    assert r.status_code == 201
    body = r.json()
    assert body["memory"]["provisioned"] is False
    assert "PCAI_LLM_SECRETS_MASTER_KEY" in body["memory"]["error"]
    assert h.calls == [], "no dataset may be created when it cannot be bound"


def test_memory_409_bounded_suffix_retry(mem_env) -> None:
    """409 on the base name → -2/-3 suffixes; each retry sends a FRESH
    create (never an adopt of a name we can't prove ownership of); a
    non-409 failure stops the walk immediately; exhausting the budget
    reports failure without failing the redemption."""
    (tc, fake), h = mem_env
    fake.enroll_row = _redeemed(None)
    # script: base name 409, -2 409, -3 201 — each attempt a FRESH create
    h.script = [409, 409, 201]
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "Alice", "memory_opt_in": True})
    assert r.status_code == 201
    body = r.json()
    assert body["memory"]["provisioned"] is True
    assert body["memory"]["dataset"].endswith("-3")
    assert len(h.calls) == 3
    names = [c["body"]["name"] for c in h.calls]
    assert names[1] == names[0] + "-2"
    assert names[2] == names[0] + "-3"
    # every attempt carries the SAME password (one secret for the dataset)
    assert len({c["body"]["password"] for c in h.calls}) == 1
    assert body["memory"]["dataset"] == names[2]


def test_memory_409_exhausted_reports_failure(mem_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every attempt 409s (a pathological squatter): bounded at 1 base +
    3 retries, the redemption succeeds, the memory block reports failure,
    and NO credential is stamped (nothing was created)."""
    (tc, fake), h = mem_env
    h.status = 409
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "Alice", "memory_opt_in": True})
    assert r.status_code == 201
    body = r.json()
    assert body["memory"]["provisioned"] is False
    assert body["memory"]["error"] == "HTTP 409"
    assert len(h.calls) == 4  # base + 3 bounded retries
    assert not hasattr(fake, "_creds") or fake._creds == {}


# --- SSRF posture ---------------------------------------------------------


def test_memory_ssrf_no_freeform_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RAG URL comes ONLY from the registry row. No PCAI_LLM_* env may
    inject one: the module reads no URL env at all (settings has no such
    knob), and the base URL is derived from the registered MCP URL."""
    import os

    rag_ish = [k for k in os.environ if "RAG" in k.upper() and k.startswith("PCAI_LLM_")]
    assert rag_ish == [], f"no RAG URL env may exist on the gateway settings surface: {rag_ish}"
    # the API surface derives from stripping the /mcp suffix of the
    # registered URL — same host, the VS-routed API service
    assert enroll_memory._api_base_url("http://rag-mcp-server-mcp.ns.svc:9090/mcp") == (
        "http://rag-mcp-server-mcp.ns.svc:9090"
    )
    assert enroll_memory._api_base_url("http://rag-mcp-server-api.ns.svc:80") == ("http://rag-mcp-server-api.ns.svc:80")


def test_memory_url_follows_registry_row(mem_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Moving the registration moves the call: the URL is read from the
    snapshot at provision time, never cached or taken from elsewhere."""
    (tc, fake), h = mem_env
    h.registry[0]["url"] = "http://rag2.other-ns.svc.cluster.local:9090/mcp"
    _patch_stamp_db(monkeypatch, fake)
    fake.enroll_row = _redeemed(None)
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "A", "memory_opt_in": True})
    assert r.status_code == 201
    assert r.json()["memory"]["provisioned"] is True
    assert h.calls[0]["url"].startswith("http://rag2.other-ns.svc.cluster.local:9090")


def test_memory_rotate_carries_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rotation copies mcp_server_creds rows verbatim (the 2026-09 fix) —
    the memory credential pair rides along with zero special-casing (the
    INSERT..SELECT copies EVERY (server, headers_enc) row of the old key).
    Pinned against the REAL rotate SQL here so a regression that special-
    cases the memory server away is caught in the enroll tests too."""
    from pcai_llm.api import routes_admin

    sql_captured: dict[str, str] = {}

    class _RotateConn:
        """Transaction-capable view (the transactional rotate, audit finding 5)."""

        def __init__(self, outer: RotateFake) -> None:
            self._outer = outer

        def transaction(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object):
            return False

        async def fetchrow(self, sql: str, *args: Any):
            return await self._outer.fetchrow(sql, *args)

        async def execute(self, sql: str, *args: Any):
            return await self._outer.execute(sql, *args)

    class RotateFake:
        def acquire(self):
            return _RotateConn(self)

        async def fetchrow(self, sql: str, *args: Any):
            if "FROM api_keys" in sql:
                return {
                    "user_id": uuid.uuid4(),
                    "name": "k",
                    "scopes": ["proxy", "mcp"],
                    "allowed_models": [],
                    "rate_limit_rpm": 60,
                    "rate_limit_tpm": 1000,
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
            if "INSERT INTO api_keys" in sql:
                return {"id": uuid.uuid4()}
            if "UPDATE api_keys" in sql and "revoked_at IS NULL" in sql:
                return {"id": uuid.uuid4()}  # conditional revoke: row came back = revoked
            return None

        async def execute(self, sql: str, *args: Any):
            if "FROM mcp_server_creds" in sql:
                sql_captured["sql"] = sql
                return "INSERT 0 1"
            if "FROM budgets" in sql or "FROM cost_budgets" in sql:
                return "INSERT 0 0"  # no budget rows on the old key
            return "UPDATE 1"

    fake = RotateFake()
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    old_id = uuid.uuid4()
    # A minimal Request stand-in: the handler now resolves audit context from
    # the request (no HTTP surface asserted here — the fake makes the audit
    # write a harmless no-op, same fail-soft path as an unpatched audit seam).
    stub_request = SimpleNamespace(
        headers={}, client=None, state=SimpleNamespace(), url=SimpleNamespace(path="/admin/keys/x/rotate")
    )
    asyncio.run(routes_admin.rotate_key(old_id, None, stub_request))  # type: ignore[arg-type]
    assert "SELECT $1, server_name, headers_enc FROM mcp_server_creds WHERE key_id = $2" in sql_captured["sql"]


# --- /v1/keys/for-me stays memory-free (scope of the feature) --------------


def test_for_me_has_no_memory_field(mem_env) -> None:
    """Memory provisioning is an ENROLLMENT-redeem feature (review §2:
    "the redeem flow gains memory_opt_in") — the SSO for-me surface does
    not carry it (its request model has no memory field; its response has
    no memory block)."""
    (_tc, _fake), _h = mem_env
    from pcai_llm.api.routes_enroll import EnrollRequest, ForMeKeyCreated

    assert "memory_opt_in" not in ForMeKeyCreated.model_fields
    assert "memory_opt_in" in EnrollRequest.model_fields


def test_enroll_usd_preset_lands_in_cost_budgets(env) -> None:
    """A profile carrying a DOLLAR budget preset stamps a cost_budgets row at
    redeem — and a token preset in the same profile still lands in budgets.

    Before the split existed, _read_profile_seed_tx validated limit_tokens as
    a REQUIRED positive int, so a USD-only preset was refused at redeem and a
    mixed profile seeded only its token half. Both kinds must now be written
    in the SAME transaction (all-or-nothing, like the grants).
    """
    tc, fake = env
    pid = uuid.uuid4()
    fake.enroll_row = _redeemed(pid)
    mixed = {
        "budgets": [
            {"model": "*", "period": "month", "limit_usd": 20},
            {"model": "m1", "period": "day", "limit_tokens": 5_000},
        ],
        "mcp_grants": [],
    }
    fake.profile_row = {"id": pid, "name": "starter", "defaults_json": json.dumps(mixed)}

    r = tc.post("/v1/enroll", json={"code": _code(), "name": "Dollar"})
    assert r.status_code == 201, r.text
    rec = asyncio.run(authenticate(r.json()["key"]))
    assert rec is not None

    by_table: dict[str, list] = {}
    for sql, rows in fake.bulk:
        table = "cost_budgets" if "cost_budgets" in sql else "budgets"
        by_table.setdefault(table, []).extend(rows)
    assert by_table.get("budgets") == [(rec.id, "m1", "day", 5_000)]
    assert by_table.get("cost_budgets") == [(rec.id, "*", "month", 20.0)]


def test_enroll_usd_only_preset_is_accepted(env) -> None:
    """A dollar-ONLY profile (no token preset at all) redeems cleanly — the
    old validator required limit_tokens on every entry and would have 400d a
    redeem that then burned the single-use code."""
    tc, fake = env
    pid = uuid.uuid4()
    fake.enroll_row = _redeemed(pid)
    fake.profile_row = {
        "id": pid,
        "name": "dollar-only",
        "defaults_json": json.dumps({"budgets": [{"model": "*", "period": "week", "limit_usd": 7.5}]}),
    }
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "Only"})
    assert r.status_code == 201, r.text
    rec = asyncio.run(authenticate(r.json()["key"]))
    assert rec is not None
    sqls = [sql for sql, _rows in fake.bulk]
    assert not any("INSERT INTO budgets " in s for s in sqls), sqls
    usd = [rows for sql, rows in fake.bulk if "cost_budgets" in sql]
    assert usd == [[(rec.id, "*", "week", 7.5)]]


def test_enroll_invalid_preset_refuses_without_burning_the_code(env) -> None:
    """An invalid stored preset is a 400 naming the profile, raised BEFORE any
    write — the redeem transaction rolls back, so the single-use code survives
    (the "code burned by a 500" defect)."""
    tc, fake = env
    pid = uuid.uuid4()
    fake.enroll_row = _redeemed(pid)
    fake.profile_row = {
        "id": pid,
        "name": "broken",
        "defaults_json": json.dumps({"budgets": [{"model": "*", "period": "month"}]}),
    }
    r = tc.post("/v1/enroll", json={"code": _code(), "name": "X"})
    assert r.status_code == 400, r.text
    assert "broken" in r.json()["detail"]
    assert "limit_tokens" in r.json()["detail"] or "limit_usd" in r.json()["detail"]
    # nothing was stamped (the refusal precedes every preset write)
    assert not [s for s, _ in fake.bulk if "INSERT INTO" in s]

# ---------------------------------------------------------------------------
# SSO default profile -> the USER row (2026-10-29)
# ---------------------------------------------------------------------------


async def test_bind_user_profile_writes_only_when_null() -> None:
    """The tenant default profile has to reach users.profile_id, or every
    profile-scoped feature (cascade ordering, routing policy, off-peak
    fraction, agents) sees "no profile" while the key carries the stamped
    presets. It is a DEFAULT, not an override: a user an operator placed by hand
    keeps their profile, so the UPDATE is conditional."""
    from pcai_llm.api import routes_enroll

    calls: list[tuple[str, tuple]] = []

    class _Conn:
        async def execute(self, sql: str, *a: Any):
            calls.append((sql, a))
            return "UPDATE 1"

    uid, pid = uuid.uuid4(), uuid.uuid4()
    await routes_enroll._bind_user_profile(_Conn(), uid, {"id": pid, "name": "poweruser"})
    assert len(calls) == 1
    sql, args = calls[0]
    assert "UPDATE users SET profile_id" in sql
    assert "profile_id IS NULL" in sql, "must never re-scope a hand-assigned user"
    assert args == (uid, pid)

    # No tenant default profile -> no write at all (the bare mint grant).
    calls.clear()
    await routes_enroll._bind_user_profile(_Conn(), uid, None)
    assert calls == []
