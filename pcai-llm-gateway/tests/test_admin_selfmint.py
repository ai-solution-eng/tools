"""Admin-governed SSO self-mint eligibility (the Z3 scope ADDITION).

The contract this file pins:

* DATA MODEL: users.self_mint_enabled (db/schema.py, additive migration
  ALTER ... DEFAULT TRUE — existing rows stay eligible; the flag is
  written ONLY by the admin API).
* ADMIN API (routes_admin.py, AdminDep-gated):
  - PUT /admin/keys/self-mint-eligibility — body {user_id OR subject,
    allowed: bool}; exactly-one-selector (400 otherwise); subject → user
    resolved THROUGH the binding the mint flow reads; 404 on unknown
    selector; audit event key.self_mint_eligibility (actor conventions
    identical to every other admin mutation).
  - GET /admin/keys/self-mint-eligibility — the console's list: user rows
    + their bound subjects + the flag.
* ENFORCEMENT (routes_enroll.py for-me): POST and GET consult z2's
  routes_identity._self_mint_allowed_for_subject (the ONE lookup — the
  probe's flags.self_mint and the mint's gate cannot drift). A blocked
  subject gets the SAME 403 a flags-off deployment gives — the refusal
  never distinguishes "surface off" from "not eligible". An eligibility
  STORE FAILURE is a different class: the exception propagates and the
  routes map it to 503 (z2's fail-closed probe semantics — an outage
  must never read as "not eligible").
* The console (admin.html Keys tab): the per-row toggle calls the PUT with
  user_id (pinned by source-shape tests here, following
  test_admin_ui.py's seam approach).

Suite convention: no live Postgres — db seams are monkeypatched fakes.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_enroll, routes_identity
from pcai_llm.config import settings
from pcai_llm.core.identity import IDP_OAUTH2_PROXY, VIA_SSO_CREATE

ADMIN_H = {"Authorization": "Bearer admin-secret-123"}
BROWSER_H = {"X-Auth-Request-User": "alice", "X-Forwarded-Groups": "eng"}


# ---------------------------------------------------------------------------
# fakes: the admin write/read fake + the eligibility read z2's function runs
# ---------------------------------------------------------------------------


class EligDB:
    """users.self_mint_enabled + the subject→user resolution surface.

    `user_flags` IS the users table's eligibility column (one entry per
    user); `subject_user` maps browser subjects to user ids (the binding
    resolution); every statement is recorded so tests can pin the exact
    UPDATE the write route runs."""

    def __init__(self) -> None:
        self.user_flags: dict[str, bool] = {}  # user_id -> self_mint_enabled
        self.user_names: dict[str, str] = {}  # user_id -> name
        self.subject_user: dict[str, str] = {}  # subject -> user_id
        self.executed: list[tuple[str, tuple]] = []
        self.fetchrows: list[tuple[str, tuple]] = []
        self.broken = False

    async def executemany(self, sql: str, args: Any) -> None:
        for a in args:
            self.executed.append((" ".join(sql.split()), tuple(a)))

    def user(self, name: str, enabled: bool = True) -> str:
        uid = str(uuid.uuid4())
        self.user_flags[uid] = enabled
        self.user_names[uid] = name
        return uid

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrows.append((" ".join(sql.split()), args))
        if self.broken:
            raise RuntimeError("database not connected")
        if "UPDATE users SET self_mint_enabled" in sql:
            # both selector shapes UPDATE the same table; arg 0 is the
            # direct user_id OR (for the subject shape) unused — the
            # subquery's result is resolved by the fake from subject_users
            uid: str | None
            if "WHERE id = $1" in sql:
                uid = str(args[0])
            else:
                uid = self.subject_user.get(str(args[0]))
            if uid is None or uid not in self.user_flags:
                return None
            self.user_flags[uid] = bool(args[1])
            return {"id": uid, "name": self.user_names[uid], "self_mint_enabled": bool(args[1])}
        return None

    async def fetch(self, sql: str, *args: Any):
        self.executed.append((" ".join(sql.split()), args))
        if self.broken:
            raise RuntimeError("database not connected")
        if "self_mint_enabled" in sql and "FROM users" in sql:
            return [
                {
                    "user_id": uid,
                    "user_name": self.user_names[uid],
                    "self_mint_enabled": flag,
                    "subjects": [s for s, u in self.subject_user.items() if u == uid],
                }
                for uid, flag in sorted(self.user_flags.items(), key=lambda kv: self.user_names[kv[0]])
            ]
        return []

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def execute(self, sql: str, *args: Any):
        self.executed.append((" ".join(sql.split()), args))
        return "UPDATE 1"

    def acquire(self):
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, db: EligDB) -> None:
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

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def executemany(self, sql: str, args: Any) -> None:
        await self._db.executemany(sql, args)


class _AuditCapture:
    """The core.audit.db seam — records the admin_audit INSERT args."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def execute(self, sql: str, *args: Any):
        self.rows.append({"sql": " ".join(sql.split()), "args": args})
        return "INSERT 0 1"


# _MintDB is hoisted to module level (used by mint_env AND the F4 data-model test):
class _MintDB(EligDB):
    """EligDB + the api_keys/subjects surfaces the mint path runs (also
    the state model z2's eligibility lookup reads: users flags +
    subject bindings + sso-create keys)."""

    """EligDB + the api_keys/subjects surfaces the mint path runs."""

    def __init__(self) -> None:
        super().__init__()
        self.subjects: dict[tuple[str, str], str] = {}
        self.users_tbl: list[dict[str, Any]] = []
        self.keys: list[dict[str, Any]] = []
        self.seq = 0
        # tenant → SSO default profile (the mint's _sso_default_profile read);
        # profile rows: id → defaults_json payload
        self.tenant_profiles: dict[str, str | None] = {}
        self.profiles_tbl: dict[str, dict[str, Any]] = {}
        self.budgets: list[tuple[str, str, str, int]] = []
        self.mcp_acls: list[tuple[str, str, str]] = []

    def _next(self) -> str:
        self.seq += 1
        return f"00000000-0000-0000-0000-{self.seq:012d}"

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrows.append((" ".join(sql.split()), args))
        if self.broken:
            raise RuntimeError("database not connected")
        if "UPDATE users SET self_mint_enabled" in sql:
            return await EligDB.fetchrow(self, sql, *args)
        if "self_mint_enabled" in sql and "FROM subjects s" in sql:
            # z2's REAL eligibility lookup (routes_identity
            # _SELF_MINT_ELIGIBILITY_SQL): subject → newest live
            # sso-create key → its user's flag. Args: (idp, bound_via,
            # subject).
            sid = self.subjects.get((str(args[0]), str(args[2])))
            if sid is None:
                return None
            live = [k for k in self.keys if k["subject_id"] == sid and k["bound_via"] == args[1] and not k["revoked"]]
            if not live:
                return None
            uid = live[-1]["user_id"]
            flag = self.user_flags.get(uid)
            return {"self_mint_enabled": flag} if flag is not None else None
        if "FROM subjects WHERE idp" in sql:
            sid = self.subjects.get((args[0], args[1]))
            return {"id": sid} if sid else None
        if "FROM api_keys k" in sql and "bound_via" in sql and "revoked_at IS NULL" in sql:
            sid = args[0]
            live = [k for k in self.keys if k["subject_id"] == sid and k["bound_via"] == args[1] and not k["revoked"]]
            if not live:
                return None
            newest = live[-1]
            return {
                "id": newest["id"],
                "user_id": newest["user_id"],
                "key_prefix": newest["key_prefix"],
                "name": newest["name"],
                "created_at": newest["created_at"],
                "spend_ancestors": newest.get("spend_ancestors", []),
            }
        if "INSERT INTO users" in sql:
            uid = self._next()
            self.users_tbl.append({"id": uid, "tenant_id": args[0], "name": args[1]})
            return {"id": uid, "name": args[1]}
        if "INSERT INTO subjects" in sql:
            pair = (args[2], args[3])
            if pair not in self.subjects:
                self.subjects[pair] = self._next()
            return {"id": self.subjects[pair]}
        if "UPDATE api_keys SET subject_id" in sql:
            kid, subject_id, bound_via = args[0], args[1], args[2]
            for k in self.keys:
                if k["id"] == kid:
                    k["subject_id"] = subject_id
                    k["bound_via"] = bound_via
            return "UPDATE 1"
        if "INSERT INTO api_keys" in sql:
            kid = self._next()
            self.keys.append(
                {
                    "id": kid,
                    "user_id": args[0],
                    "name": args[1],
                    "key_hash": args[2],
                    "key_prefix": args[3],
                    "scopes": list(args[4]),
                    # the INSERT's $7/$8: rate_limit_rpm / rate_limit_tpm
                    "rate_limit_rpm": args[6],
                    "rate_limit_tpm": args[7],
                    "spend_ancestors": list(args[11]) if len(args) >= 12 else [],
                    "subject_id": None,
                    "bound_via": "",
                    "revoked": False,
                    "created_at": "2026-10-06T00:00:00+00:00",
                }
            )
            return {"id": kid}
        if "UPDATE api_keys SET spend_ancestors" in sql:
            # the SSO rotate's lineage carry: (new_key_id, ancestors+old_id)
            for k in self.keys:
                if k["id"] == args[0]:
                    k["spend_ancestors"] = list(args[1])
            return "UPDATE 1"
        if "JOIN profiles p ON p.id = t.sso_default_profile_id" in sql:
            # the mint's tenant default-profile read (args: tenant id)
            pid = self.tenant_profiles.get(str(args[0]))
            if pid is None:
                return None
            prow = self.profiles_tbl.get(pid)
            return {"id": pid, "defaults_json": prow["defaults_json"]} if prow else None
        return None

    async def execute(self, sql: str, *args: Any):
        self.executed.append((" ".join(sql.split()), args))
        if "UPDATE api_keys SET revoked_at" in sql:
            for k in self.keys:
                if k["id"] == args[0]:
                    k["revoked"] = True
                    return "UPDATE 1"
            return "UPDATE 0"
        if "UPDATE api_keys SET subject_id" in sql:
            # _STAMP_BINDING_SQL via bind_key_subject's conn= path:
            # args = (key_id, subject_id, bound_via)
            kid, subject_id, bound_via = args[0], args[1], args[2]
            for k in self.keys:
                if k["id"] == kid:
                    k["subject_id"] = subject_id
                    k["bound_via"] = bound_via
            return "UPDATE 1"
        return "UPDATE 0"

    async def executemany(self, sql: str, args: Any) -> None:
        if "INSERT INTO budgets" in sql:
            for a in args:
                self.budgets.append((a[0], a[1], a[2], a[3]))
            return
        if "INSERT INTO mcp_tool_acls" in sql:
            for a in args:
                self.mcp_acls.append((a[0], a[1], a[2]))
            return


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = EligDB()
    audit = _AuditCapture()
    monkeypatch.setattr(routes_admin, "db", fake)
    from pcai_llm.core import audit as audit_mod

    monkeypatch.setattr(audit_mod, "db", audit)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake, audit


@pytest.fixture
def mint_env(monkeypatch: pytest.MonkeyPatch):
    """The for-me surface with the eligibility lookup stubbed at z2's
    function (its implementation is z2's file; here the agreed signature
    is faked per-test via the module attribute routes_enroll consults)."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", str(uuid.uuid4()))
    import pcai_llm.core.keys as keys_mod
    from pcai_llm.core import audit as audit_mod
    from pcai_llm.core import identity

    fake = _MintDB()
    audit = _AuditCapture()
    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(audit_mod, "db", audit)
    monkeypatch.setattr(routes_enroll, "bind_key_subject", identity.bind_key_subject)
    app = FastAPI()
    app.include_router(routes_enroll.router)
    return TestClient(app, raise_server_exceptions=False), fake


# ---------------------------------------------------------------------------
# 1. admin API: eligibility write + read round-trip
# ---------------------------------------------------------------------------


def test_put_by_user_id_roundtrips(admin_env) -> None:
    tc, fake, _audit = admin_env
    uid = fake.user("alice")
    r = tc.put("/admin/keys/self-mint-eligibility", json={"user_id": uid, "allowed": False}, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"user_id": uid, "user_name": "alice", "self_mint_enabled": False}
    assert fake.user_flags[uid] is False
    # flip back on — the toggle is a real two-way switch
    r2 = tc.put("/admin/keys/self-mint-eligibility", json={"user_id": uid, "allowed": True}, headers=ADMIN_H)
    assert r2.status_code == 200 and r2.json()["self_mint_enabled"] is True


def test_put_by_subject_resolves_through_binding(admin_env) -> None:
    tc, fake, _audit = admin_env
    uid = fake.user("alice")
    fake.subject_user["alice@corp"] = uid
    r = tc.put("/admin/keys/self-mint-eligibility", json={"subject": "alice@corp", "allowed": False}, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert r.json()["user_id"] == uid and r.json()["self_mint_enabled"] is False


def test_put_by_subject_is_normalized(admin_env) -> None:
    """The subject selector gets the same hygiene every caller-supplied
    subject gets (trim etc.) — a blocked write must target the SAME string
    the mint lookup reads."""
    tc, fake, _audit = admin_env
    uid = fake.user("alice")
    fake.subject_user["alice@corp"] = uid
    r = tc.put(
        "/admin/keys/self-mint-eligibility", json={"subject": "  alice@corp  ", "allowed": False}, headers=ADMIN_H
    )
    assert r.status_code == 200, r.text
    # the UPDATE received the TRIMMED subject (the subject-shape UPDATE
    # carries the subject as its $1)
    upd = next(
        a for s, a in fake.fetchrows if "UPDATE users SET self_mint_enabled" in s and "WHERE s.subject = $1" in s
    )
    assert str(upd[0]) == "alice@corp"


def test_put_requires_exactly_one_selector(admin_env) -> None:
    tc, fake, _audit = admin_env
    uid = fake.user("alice")
    both = tc.put(
        "/admin/keys/self-mint-eligibility",
        json={"user_id": uid, "subject": "alice@corp", "allowed": False},
        headers=ADMIN_H,
    )
    assert both.status_code == 400
    neither = tc.put("/admin/keys/self-mint-eligibility", json={"allowed": False}, headers=ADMIN_H)
    assert neither.status_code == 400
    assert fake.user_flags[uid] is True  # nothing written by the refusals


def test_put_unknown_selector_404(admin_env) -> None:
    tc, _fake, _audit = admin_env
    r = tc.put(
        "/admin/keys/self-mint-eligibility", json={"user_id": str(uuid.uuid4()), "allowed": False}, headers=ADMIN_H
    )
    assert r.status_code == 404
    r2 = tc.put("/admin/keys/self-mint-eligibility", json={"subject": "nobody@corp", "allowed": False}, headers=ADMIN_H)
    assert r2.status_code == 404


def test_put_requires_admin(admin_env) -> None:
    tc, fake, _audit = admin_env
    uid = fake.user("alice")
    r = tc.put("/admin/keys/self-mint-eligibility", json={"user_id": uid, "allowed": False})
    assert r.status_code == 401
    assert fake.user_flags[uid] is True  # unchanged
    r503 = tc.put(
        "/admin/keys/self-mint-eligibility", json={"user_id": uid, "allowed": False}, headers={"Authorization": ""}
    )
    assert r503.status_code in (401, 403)


def test_get_lists_eligibility_with_subjects(admin_env) -> None:
    tc, fake, _audit = admin_env
    a = fake.user("alice", enabled=False)
    b = fake.user("bob", enabled=True)
    fake.subject_user["alice@corp"] = a
    fake.subject_user["bob@corp"] = b
    fake.subject_user["alice-alt@corp"] = a  # a second binding on the same user
    r = tc.get("/admin/keys/self-mint-eligibility", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    rows = r.json()
    by_name = {row["user_name"]: row for row in rows}
    assert by_name["alice"]["self_mint_enabled"] is False
    assert by_name["alice"]["user_id"] == a
    assert sorted(by_name["alice"]["subjects"]) == ["alice-alt@corp", "alice@corp"]
    assert by_name["bob"]["self_mint_enabled"] is True
    # hashes/secrets never ride the admin list projection
    assert all("key" not in json_keys for row in rows for json_keys in [set(row)])


def test_get_requires_admin(admin_env) -> None:
    tc, _fake, _audit = admin_env
    assert tc.get("/admin/keys/self-mint-eligibility").status_code == 401


# ---------------------------------------------------------------------------
# 2. audit: the eligibility change leaves a trail
# ---------------------------------------------------------------------------


def test_put_writes_audit_event(admin_env) -> None:
    tc, fake, audit = admin_env
    uid = fake.user("alice")
    tc.put("/admin/keys/self-mint-eligibility", json={"user_id": uid, "allowed": False}, headers=ADMIN_H)
    assert len(audit.rows) == 1
    row = audit.rows[0]
    args = row["args"]
    # audit_action's INSERT shape: (actor_key_id, actor_subject, actor_source,
    # action, resource_type, resource_id, details-json, request_id, source_ip)
    assert args[3] == "key.self_mint_eligibility"
    assert args[4] == "user"
    assert args[5] == uid
    assert '"allowed": false' in args[6] or '"allowed": false' in str(args[6]).lower()
    assert args[2] == "admin-key"  # the bearer-key actor source


def test_put_audit_blocked_subject_carries_by_subject(admin_env) -> None:
    tc, fake, audit = admin_env
    uid = fake.user("alice")
    fake.subject_user["alice@corp"] = uid
    tc.put("/admin/keys/self-mint-eligibility", json={"subject": "alice@corp", "allowed": False}, headers=ADMIN_H)
    assert len(audit.rows) == 1
    details = str(audit.rows[0]["args"][6])
    assert '"by_subject": true' in details.replace("True", "true")


# ---------------------------------------------------------------------------
# 3. enforcement: the for-me surfaces consult the shared eligibility lookup
# ---------------------------------------------------------------------------


def _install_eligibility(monkeypatch: pytest.MonkeyPatch, allowed_for: dict[str, bool]) -> list[str]:
    """Stub z2's _self_mint_allowed_for_subject at the agreed signature and
    record which subjects the for-me flow consulted."""
    consulted: list[str] = []

    async def fake_allowed(subject: str) -> bool:
        consulted.append(subject)
        return allowed_for.get(subject, True)  # default TRUE (backward compat)

    from pcai_llm.api import routes_identity

    monkeypatch.setattr(routes_identity, "_self_mint_allowed_for_subject", fake_allowed)
    return consulted


def test_for_me_post_blocked_403_indistinguishable_from_flag_off(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _fake = mint_env
    _install_eligibility(monkeypatch, {"alice": False})
    r = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert r.status_code == 403
    # THE anti-oracle pin: byte-identical to the flags-off refusal — a
    # blocked subject probing learns nothing about WHICH gate refused.
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    r_flag_off = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert r_flag_off.status_code == 403
    assert r.content == r_flag_off.content


def test_for_me_get_blocked_403_indistinguishable_from_flag_off(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _fake = mint_env
    _install_eligibility(monkeypatch, {"alice": False})
    r = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert r.status_code == 403
    assert r.json()["detail"] == "SSO key creation is not enabled on this deployment"
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    r_flag_off = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert r_flag_off.status_code == 403
    assert r.content == r_flag_off.content


def test_for_me_blocked_subject_cannot_even_learn_key_existence(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Eligibility is checked BEFORE the one-key lookup: a blocked subject's
    GET never reaches the key query — no exists:true/false oracle."""
    tc, fake = mint_env
    _install_eligibility(monkeypatch, {"alice": False})
    r = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert r.status_code == 403
    assert not any("FROM api_keys" in s for s, _ in fake.fetchrows)


def test_for_me_allowed_subject_mints_normally(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, fake = mint_env
    consulted = _install_eligibility(monkeypatch, {"alice": True})
    r = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert r.status_code == 201, r.text
    assert len(fake.keys) == 1
    assert consulted == ["alice"]


def test_for_me_default_eligible_when_unknown_subject(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subject z2's lookup has NO entry for is eligible (the data-model
    default TRUE — existing users were minting-eligible before the column)."""
    tc, _fake = mint_env
    _install_eligibility(monkeypatch, {})  # nothing recorded → default True
    assert tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H).status_code == 201


def test_rotate_blocked_403(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A subject blocked AFTER minting cannot rotate either — the gate sits
    before the rotate path, not only before first mint."""
    tc, _fake = mint_env
    allowed = _install_eligibility(monkeypatch, {"alice": True})
    first = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert first.status_code == 201
    allowed.clear()
    allowed.append("alice-blocked-now")  # flip the recorded entry

    # re-stub with the subject now blocked
    from pcai_llm.api import routes_identity

    async def now_blocked(subject: str) -> bool:
        return False

    monkeypatch.setattr(routes_identity, "_self_mint_allowed_for_subject", now_blocked)
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert r.status_code == 403


def test_eligibility_lookup_failure_is_503_not_403(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """z2's lookup raising (DB down) must NEVER read as allowed OR as
    "not eligible" — the agreed wave-1 posture: the exception propagates
    out of routes_enroll._self_mint_allowed and the route maps it to 503,
    matching the probe's fail-closed semantics
    (test_eligibility_hook_failure_is_503_fail_closed). A 403 here would
    deny every user during an outage while lying about the cause."""
    tc, _fake = mint_env
    from pcai_llm.api import routes_identity

    async def broken(subject: str) -> bool:
        raise RuntimeError("database not connected")

    monkeypatch.setattr(routes_identity, "_self_mint_allowed_for_subject", broken)
    r = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert r.status_code == 503
    assert "available" in r.json()["detail"]  # an outage message, not the gates-off refusal
    # and the GET surface behaves identically (both readers, one posture)
    r_get = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert r_get.status_code == 503
    assert r_get.json()["detail"] == r.json()["detail"]


def test_eligibility_false_is_403_but_failure_is_503_distinct(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two refusal classes stay distinct: a SUCCESSFUL lookup that says
    no → 403 (anti-oracle, indistinguishable from flags-off); a lookup that
    CANNOT answer → 503 (infrastructure, honest about the outage)."""
    tc, _fake = mint_env
    from pcai_llm.api import routes_identity

    _install_eligibility(monkeypatch, {"alice": False})
    blocked = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert blocked.status_code == 403

    async def broken(subject: str) -> bool:
        raise RuntimeError("eligibility store down")

    monkeypatch.setattr(routes_identity, "_self_mint_allowed_for_subject", broken)
    outage = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert outage.status_code == 503
    assert outage.content != blocked.content  # never conflated


def test_enroll_wraps_z2_function_not_its_own_read(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring pin: routes_enroll consults routes_identity's function —
    patching IT changes the mint's answer (a local second implementation
    would make this test fail by ignoring the patch)."""
    tc, _fake = mint_env
    _install_eligibility(monkeypatch, {"alice": False})
    assert tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H).status_code == 403
    _install_eligibility(monkeypatch, {"alice": True})
    assert tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H).status_code == 201


def test_self_mint_allowed_default_true_out_of_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    """z2's function EXISTS at the agreed signature (it has landed in
    routes_identity.py) and is a coroutine — the import inside
    routes_enroll._self_mint_allowed must succeed in the real tree."""
    import inspect

    from pcai_llm.api import routes_identity

    fn = routes_identity._self_mint_allowed_for_subject
    assert inspect.iscoroutinefunction(fn)
    # z2's LANDED body: subject → binding → user flag via identity.db.
    # Wire a fake carrying NO state at all: every lookup path misses →
    # the default-eligible branch answers True for the unknown subject
    # (the docstring contract: no bound key/user → first mint stays open).
    from pcai_llm.core import identity

    class _EmptyDB:
        async def fetchrow(self, sql: str, *args: Any):
            return None

    monkeypatch.setattr(identity, "db", _EmptyDB())
    assert asyncio.run(fn("unknown-subject")) is True
    # and the wrapper resolves + calls it (pass-through posture)
    assert asyncio.run(routes_enroll._self_mint_allowed("unknown-subject")) is True


# ---------------------------------------------------------------------------
# 4. the console toggle (admin.html Keys tab) — source-shape pins
# ---------------------------------------------------------------------------


def test_admin_html_has_self_mint_column_and_toggle() -> None:
    """The Keys tab renders a Self-mint column with a per-row toggle that
    PUTs {user_id, allowed} — the api() helper's body contract (it
    JSON-stringifies opts.body itself, so the handler passes a plain
    object)."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert ">Self-mint</th>" in s
    assert "selfMintCell" in s and "toggleSelfMint" in s
    assert "window.toggleSelfMint" in s
    # the PUT hits the exact endpoint with the exact body shape
    assert "api('/keys/self-mint-eligibility', {" in s
    assert "body: { user_id: userId, allowed: allowed }," in s
    # undefined-eligibility rows render the muted dash, never a fake toggle
    assert "typeof k.self_mint_enabled !== 'boolean'" in s
    # no key-material in the toggle path (the flag is per-USER)
    assert "user_id" in s and "key_id" not in s.split("window.toggleSelfMint")[1].split("});")[0]


def test_admin_html_script_blocks_node_check() -> None:
    """The fleet UI hygiene rule: every <script> block of the edited page
    parses under node --check (a broken toggle must never ship)."""
    import subprocess
    import tempfile

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    blocks = re.findall(r"<script>(.*?)</script>", s, re.DOTALL)
    assert blocks, "no script blocks found"
    for i, block in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(block)
            path = f.name
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, f"admin.html script block {i} failed node --check:\n{r.stderr}"


# ---------------------------------------------------------------------------
# 4b. the DATA-MODEL contract end-to-end (wave-1 finding F4): admin write →
# users.self_mint_enabled → z2's REAL lookup reads it back
# ---------------------------------------------------------------------------


def test_eligibility_data_model_contract(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """F4's integration pin, run against z2's REAL lookup body (it has
    landed): the admin PUT persists users.self_mint_enabled; z2's
    _self_mint_allowed_for_subject reads the SAME column through the
    SAME subject→user resolution — write and read land on one flag.
    Uses the _MintDB fake (models users + subjects + api_keys), so no
    monkeypatching of the lookup itself: when z2 swaps/tunes the body,
    this test keeps holding as long as the data flow does."""
    tc, _fake, _audit = admin_env
    # ONE state model for BOTH the admin write and z2's lookup read —
    # _MintDB models users flags + subject bindings + sso-create keys,
    # which is exactly the data flow under test. Wire it on every seam:
    # routes_admin.db (the PUT/GET statements), identity.db (the lookup).
    db_fake = _MintDB()
    from pcai_llm.core import identity as identity_mod

    monkeypatch.setattr(identity_mod, "db", db_fake)
    monkeypatch.setattr(routes_admin, "db", db_fake)

    # mint through the ADMIN-side state model: a user + an sso-create
    # key binding (what the first mint leaves behind)
    uid = db_fake.user("alice", enabled=True)
    db_fake.subjects[(IDP_OAUTH2_PROXY, "alice")] = "sid-alice"
    db_fake.keys.append(
        {
            "id": "key-alice",
            "user_id": uid,
            "name": "sso-alice",
            "key_hash": "hash",
            "key_prefix": "hllm-AAAA",
            "scopes": ["proxy", "mcp"],
            "subject_id": "sid-alice",
            "bound_via": VIA_SSO_CREATE,
            "revoked": False,
            "created_at": "2026-10-06T00:00:00+00:00",
        }
    )

    # 1. the REAL lookup reads the ADMIN-WRITTEN flag: allowed user → True
    assert asyncio.run(routes_identity._self_mint_allowed_for_subject("alice")) is True

    # 2. admin blocks THROUGH THE API (the write side of this very file)
    r = tc.put(
        "/admin/keys/self-mint-eligibility",
        json={"user_id": uid, "allowed": False},
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text
    assert db_fake.user_flags[uid] is False

    # 3. the SAME real lookup now reads BLOCKED — the write and the read
    # landed on one flag (the F4 contract: nothing-enforced is over)
    assert asyncio.run(routes_identity._self_mint_allowed_for_subject("alice")) is False

    # 4. and unblocking restores eligibility through the same path
    r2 = tc.put(
        "/admin/keys/self-mint-eligibility",
        json={"user_id": uid, "allowed": True},
        headers=ADMIN_H,
    )
    assert r2.status_code == 200
    assert asyncio.run(routes_identity._self_mint_allowed_for_subject("alice")) is True

    # 5. an unknown subject (no binding row at all) stays default-eligible
    assert asyncio.run(routes_identity._self_mint_allowed_for_subject("nobody")) is True


def test_admin_roundtrip_blocked_shows_in_list(admin_env) -> None:
    """The API round-trip alone (no lookup involved): PUT blocked → the
    GET list reports allowed=false for that user — the console toggle's
    read-your-writes contract."""
    tc, fake, _audit = admin_env
    uid = fake.user("alice")
    fake.subject_user["alice@corp"] = uid
    tc.put("/admin/keys/self-mint-eligibility", json={"user_id": uid, "allowed": False}, headers=ADMIN_H)
    rows = {r["user_id"]: r for r in tc.get("/admin/keys/self-mint-eligibility", headers=ADMIN_H).json()}
    assert rows[uid]["self_mint_enabled"] is False
    tc.put("/admin/keys/self-mint-eligibility", json={"user_id": uid, "allowed": True}, headers=ADMIN_H)
    rows = {r["user_id"]: r for r in tc.get("/admin/keys/self-mint-eligibility", headers=ADMIN_H).json()}
    assert rows[uid]["self_mint_enabled"] is True


# ---------------------------------------------------------------------------
# 5. the schema migration contract
# ---------------------------------------------------------------------------


def test_schema_migration_additive_default_true() -> None:
    """The migration is idempotent ADD COLUMN IF NOT EXISTS with a
    NOT NULL TRUE default — existing rows stay eligible, NULLs never
    occur, and re-running schema.py on an upgraded db is a no-op."""
    s = open("src/pcai_llm/db/schema.py", encoding="utf-8").read()
    m = re.search(r"ALTER TABLE users ADD COLUMN IF NOT EXISTS self_mint_enabled[^;]+;", s)
    assert m, "users.self_mint_enabled migration missing from db/schema.py"
    stmt = m.group(0)
    assert "NOT NULL" in stmt and "DEFAULT TRUE" in stmt


# ---------------------------------------------------------------------------
# 6. tenant SSO default-profile stamping (2026-10-08 feature)
# ---------------------------------------------------------------------------

_PROFILE_DEFAULTS = {
    "budgets": [{"model": "GLM-5.3-Flash", "period": "day", "limit_tokens": 100_000}],
    "mcp_grants": [["prometheus", "prom_query"]],
    "limits": {"rate_limit_rpm": 240, "rate_limit_tpm": 400_000},
}


def _configure_tenant_profile(fake: Any, tenant: str) -> None:
    pid = "p0000000-0000-0000-0000-000000000001"
    fake.tenant_profiles[tenant] = pid
    fake.profiles_tbl[pid] = {"defaults_json": __import__("json").dumps(_PROFILE_DEFAULTS)}


def test_for_me_mint_stamps_tenant_default_profile(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """With a tenant default profile configured, a mint stamps the profile's
    budgets + MCP grants AND the limit presets OVERRIDE the bare defaults
    (the console create path's 'profile is the declared baseline' rule)."""
    tc, fake = mint_env
    _install_eligibility(monkeypatch, {"alice": True})
    _configure_tenant_profile(fake, str(settings.token_shadow_tenant_id))
    r = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert r.status_code == 201, r.text
    key = fake.keys[-1]
    assert key["rate_limit_rpm"] == 240 and key["rate_limit_tpm"] == 400_000
    assert len(fake.budgets) == 1 and fake.budgets[0][1] == "GLM-5.3-Flash"
    assert fake.mcp_acls == [(key["id"], "prometheus", "prom_query")]
    # scopes are NEVER widened by a profile
    assert key["scopes"] == ["proxy", "mcp"]


def test_for_me_rotate_re_stamps_tenant_default_profile(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rotate issues a NEW key row — the profile stamping MUST repeat or
    the grants silently die at first rotation. After rotate: exactly one
    live key, carrying the budget + ACL rows; the old key revoked."""
    tc, fake = mint_env
    _install_eligibility(monkeypatch, {"alice": True})
    _configure_tenant_profile(fake, str(settings.token_shadow_tenant_id))
    assert tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H).status_code == 201
    old_id = fake.keys[-1]["id"]
    budgets_before = len(fake.budgets)
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert r.status_code == 201, r.text
    new_key = fake.keys[-1]
    assert new_key["id"] != old_id
    assert not new_key["revoked"] and next(k for k in fake.keys if k["id"] == old_id)["revoked"]
    assert len(fake.budgets) == budgets_before + 1  # re-stamped on the new row
    assert fake.mcp_acls[-1][0] == new_key["id"]
    assert new_key["rate_limit_rpm"] == 240


def test_for_me_without_tenant_profile_keeps_bare_grant(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No tenant default profile (the NULL column) = byte-identical to
    pre-feature behavior: bare rpm/tpm, zero budget/ACL rows."""
    tc, fake = mint_env
    _install_eligibility(monkeypatch, {"alice": True})
    r = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert r.status_code == 201, r.text
    key = fake.keys[-1]
    assert key["rate_limit_rpm"] == 60 and key["rate_limit_tpm"] == 100_000
    assert fake.budgets == [] and fake.mcp_acls == []


def test_schema_migration_adds_tenant_sso_default_profile() -> None:
    """The tenants.sso_default_profile_id migration exists: ADD COLUMN IF NOT
    EXISTS, FK to profiles, ON DELETE SET NULL (a deleted profile degrades
    the mint to the bare grant — never blocks it)."""
    s = open("src/pcai_llm/db/schema.py", encoding="utf-8").read()
    m = re.search(r"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sso_default_profile_id[^;]+;", s)
    assert m, "tenants.sso_default_profile_id migration missing from db/schema.py"
    stmt = m.group(0)
    assert "REFERENCES profiles(id)" in stmt and "ON DELETE SET NULL" in stmt
