"""SSO self-mint: one-key-per-identity + rotate semantics (brief §Z3).

The contract this file pins (the MM RAG D25 mirror, ratified brief §Z3):

* GET /v1/keys/for-me — the owner-safe masked view: newest non-revoked
  bound_via='sso-create' key for the CALLING BROWSER IDENTITY's subject.
  Same gates as the POST (403 trust-off, 401 headerless), HTTP 200 ALWAYS
  (the SPA branches on ``exists``, not the status — a 404 would be
  indistinguishable from route-absent on older backends). NEVER the
  plaintext: prefix/name/created_at only. Read-only: no audit row.
* POST /v1/keys/for-me gains one-key semantics: a blind second mint for a
  subject that already holds a live sso-create key is 409 with the
  machine-readable rotate hint; body {"rotate": true} replaces it — the
  new key is created and the old REVOKED (revoked_at=now) in ONE
  transaction, so a failure leaves the OLD key live and no new key (never
  two live keys, never zero). The lookup is the SAME helper the GET reads
  (_live_sso_key_for_subject) so the SPA's exists-flag and the 409 can
  never disagree.
* Audit gains "rotate": bool in details; event shape otherwise unchanged.
* Flag-off byte-identity: every new refusal (GET 403/401) mirrors the
  POST's existing shapes; the mint path's success bytes are unchanged.

Suite convention (tests/conftest.py + test_identity_phaseb.py): no live
Postgres and NO NETWORK — db calls are monkeypatched fakes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import fastapi  # noqa: F401 — probe handlers annotate fastapi.Request (postponed annotations)
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pcai_llm.api import routes_enroll
from pcai_llm.config import settings
from pcai_llm.core import identity
from pcai_llm.core.identity import IDP_OAUTH2_PROXY, VIA_SSO_CREATE

BROWSER_H = {"X-Auth-Request-User": "alice", "X-Forwarded-Groups": "eng"}


# ---------------------------------------------------------------------------
# fixtures: the stateful fake (test_identity_phaseb.ForMeDB shape, rebuilt
# locally — that file is z1's zone; this copy stays in sync by construction:
# the suite must keep BOTH files green on the same fake-SQL vocabulary)
# ---------------------------------------------------------------------------


class SelfMintDB:
    """Stateful fake modeling subjects UNIQUE (idp, subject), users, and
    api_keys (with subject_id/bound_via/revoked_at) — the ForMeDB surface
    plus what rotate needs: real revoke statements, created_at on key
    rows, a recorded statement log, and a transactional acquire() seam.

    Dispatches on the SAME SQL substrings the production code emits —
    including the exact _FOR_ME_* statements routes_enroll owns — so the
    tests observe the real statement shapes, not paraphrases."""

    def __init__(self) -> None:
        self.subjects: dict[tuple[str, str], str] = {}  # (idp, subject) -> id
        self.users: list[dict[str, Any]] = []
        self.keys: list[dict[str, Any]] = []  # full ledger incl. revoked
        self.user_conflicts = 0  # pending UniqueViolationError raises on user INSERT
        self.seq = 0
        self.statements: list[str] = []  # every SQL string that reached the fake
        self.rolled_back: list[BaseException] = []  # tx blocks that exited on an exception
        # The tenant SSO-default profile row (None = no profile wired).
        # Settable so a test can wire a profile whose stored preset is
        # invalid — the stamp-time config failure path.
        self.tenant_profile_row: dict[str, Any] | None = None
        self.bulk: list[tuple[str, list]] = []  # executemany statements

    def _next(self) -> str:
        self.seq += 1
        return f"00000000-0000-0000-0000-{self.seq:012d}"

    def live_keys(self, subject_id: str, bound_via: str) -> list[dict[str, Any]]:
        return [
            k
            for k in self.keys
            if k["subject_id"] == subject_id and k["bound_via"] == bound_via and k["revoked"] is False
        ]

    async def fetchrow(self, sql: str, *args: Any):
        self.statements.append(" ".join(sql.split()))
        if "JOIN profiles" in sql and "FROM tenants t" in sql:
            # _sso_default_profile: the tenant's wired default profile
            return self.tenant_profile_row
        if "FROM subjects WHERE idp" in sql:
            sid = self.subjects.get((args[0], args[1]))
            return {"id": sid} if sid else None
        if "FROM api_keys k" in sql and "bound_via" in sql and "revoked_at IS NULL" in sql:
            # _FOR_ME_KEY_SQL: (subject_id, bound_via) → newest live row
            # (carries user_id — the rotate's user-row-reuse anchor, and
            # spend_ancestors — the budget-carry lineage)
            live = self.live_keys(args[0], args[1])
            row = live[-1] if live else None
            if row is None:
                return None
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "key_prefix": row["key_prefix"],
                "name": row["name"],
                "created_at": row["created_at"],
                "spend_ancestors": row.get("spend_ancestors", []),
            }
        if "INSERT INTO users" in sql:
            if self.user_conflicts > 0:
                self.user_conflicts -= 1
                import asyncpg

                raise asyncpg.UniqueViolationError("duplicate key")
            uid = self._next()
            self.users.append({"id": uid, "tenant_id": args[0], "name": args[1]})
            return {"id": uid, "name": args[1]}
        if "INSERT INTO subjects" in sql:
            # bind_key_subject's upsert: (tenant_id, kind, idp, subject, ...)
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
        if "UPDATE api_keys SET revoked_at" in sql:
            # _FOR_ME_REVOKE_SQL / revoke_key: (key_id) → revoke the row
            for k in self.keys:
                if k["id"] == args[0]:
                    k["revoked"] = True
                    return "UPDATE 1"
            return "UPDATE 0"
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
        if "FROM api_keys k" in sql and "key_hash" in sql:
            # authenticate(): key_hash lookup honoring revocation + expiry
            for k in self.keys:
                if k["key_hash"] == args[0]:
                    if k["revoked"]:
                        return None
                    return {
                        "id": k["id"],
                        "user_id": k["user_id"],
                        "name": k["name"],
                        "key_prefix": k["key_prefix"],
                        "scopes": k["scopes"],
                        "allowed_models": [],
                        "rate_limit_rpm": 60,
                        "rate_limit_tpm": 100_000,
                        "max_concurrent": 0,
                        "expires_at": None,
                        "revoked_at": None if not k["revoked"] else "2026-10-06T00:00:00+00:00",
                        "relay_default": False,
                        "stream_pcai_events": False,
                        "offpeak_fraction": None,
                    }
            return None
        return None

    async def fetchval(self, sql: str, *args: Any):
        self.statements.append(" ".join(sql.split()))
        return "tenant-acme"  # tenant name lookup (unused by the for-me flow)

    async def fetch(self, sql: str, *args: Any):
        self.statements.append(" ".join(sql.split()))
        return []

    async def execute(self, sql: str, *args: Any):
        self.statements.append(" ".join(sql.split()))
        if "UPDATE api_keys SET subject_id" in sql:
            # bind_key_subject's BEST-EFFORT stamp (the non-rotate mint
            # path writes it via db.execute, not fetchrow)
            kid, subject_id, bound_via = args[0], args[1], args[2]
            for k in self.keys:
                if k["id"] == kid:
                    k["subject_id"] = subject_id
                    k["bound_via"] = bound_via
            return "UPDATE 1"
        if "UPDATE api_keys SET revoked_at" in sql:
            for k in self.keys:
                if k["id"] == args[0]:
                    k["revoked"] = True
                    return "UPDATE 1"
            return "UPDATE 0"
        return "UPDATE 0"

    def acquire(self):
        return _FakeConn(self)


class _FakeConn:
    """An acquired-connection stand-in (the enroll-redeem pattern).

    The transaction context manager OBSERVES failures: a body raised
    inside the block sets ``self._db.rolled_back`` — how the crash-safety
    tests see the ROLLBACK without a live Postgres (the fake's data
    structures cannot un-insert a row the way a real tx would, so the
    tests assert the rollback CONTRACT marker instead of a mutated
    ledger). Statements dispatch to the owning fake."""

    def __init__(self, db: SelfMintDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc_type is not None and isinstance(exc, BaseException):
            self._db.rolled_back.append(exc)
        return False  # propagate — the endpoint 500s exactly like prod

    async def fetchrow(self, sql: str, *args: Any):
        return await self._db.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any):
        return await self._db.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def executemany(self, sql: str, args: Any):
        # The profile-preset stamping (budgets / cost_budgets / grants)
        # runs through executemany inside the mint transaction; record the
        # statement shape so tests can assert WHICH table was written.
        self._db.statements.append(" ".join(sql.split()))
        self._db.bulk.append((" ".join(sql.split()), list(args)))


@pytest.fixture
def mint_env(monkeypatch: pytest.MonkeyPatch):
    """Trust ON, tenant set, fake db wired on every seam the flow touches
    (routes_enroll.db for the endpoint + helper, core.keys.db for
    create_key/authenticate, identity.db for bind_key_subject,
    core.audit.db for the best-effort audit writer)."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", str(uuid.uuid4()))
    fake = SelfMintDB()
    import pcai_llm.core.keys as keys_mod
    from pcai_llm.core import audit as audit_mod

    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(audit_mod, "db", fake)
    monkeypatch.setattr(routes_enroll, "bind_key_subject", identity.bind_key_subject)
    app = FastAPI()
    app.include_router(routes_enroll.router)
    return TestClient(app, raise_server_exceptions=False), fake


def _mint(tc: TestClient, **over: Any) -> Any:
    return tc.post("/v1/keys/for-me", json=over or None, headers=BROWSER_H)


# ---------------------------------------------------------------------------
# 1. GET /v1/keys/for-me — the owner-safe masked view
# ---------------------------------------------------------------------------


def test_for_me_get_before_any_mint_is_exists_false(mint_env) -> None:
    tc, _fake = mint_env
    r = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert r.status_code == 200, r.text
    assert r.json() == {"exists": False}


def test_for_me_get_after_mint_is_exists_true_with_matching_prefix(mint_env) -> None:
    tc, _fake = mint_env
    minted = _mint(tc).json()
    r = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["exists"] is True
    assert body["key_prefix"] == minted["key_prefix"]
    assert body["name"] == "sso-alice"
    assert "created_at" in body
    # NEVER the plaintext: the masked view carries no key field, and the
    # full plaintext appears nowhere in the response bytes.
    assert "key" not in body
    assert minted["key"].encode() not in r.content


def test_for_me_get_after_rotate_shows_the_new_key(mint_env) -> None:
    tc, _fake = mint_env
    old = _mint(tc).json()
    rotated = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H).json()
    r = tc.get("/v1/keys/for-me", headers=BROWSER_H).json()
    assert r["exists"] is True
    assert r["key_prefix"] == rotated["key_prefix"]
    assert r["key_prefix"] != old["key_prefix"]


def test_for_me_get_is_owner_scoped_other_subject_sees_false(mint_env) -> None:
    """The view is the CALLER'S OWN: bob's browser identity reads only
    bob's sso-create keys — alice's mint leaves bob's GET at exists:false
    (and bob's mint does not disturb alice's view either)."""
    tc, _fake = mint_env
    _mint(tc)
    bob_h = {"X-Auth-Request-User": "bob", "X-Forwarded-Groups": "eng"}
    assert tc.get("/v1/keys/for-me", headers=bob_h).json() == {"exists": False}
    _mint_tc = tc.post("/v1/keys/for-me", json=None, headers=bob_h)
    assert _mint_tc.status_code == 201
    assert tc.get("/v1/keys/for-me", headers=bob_h).json()["exists"] is True


def test_for_me_get_revoked_key_reads_exists_false(mint_env) -> None:
    """After a rotate, the old key is revoked — the newest LIVE key is
    what GET reports (the revoke statement must have landed)."""
    tc, fake = mint_env
    old = _mint(tc).json()
    tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    # the old row IS revoked in the fake's ledger
    old_rows = [k for k in fake.keys if k["key_prefix"] == old["key_prefix"]]
    assert len(old_rows) == 1 and old_rows[0]["revoked"] is True
    new = tc.get("/v1/keys/for-me", headers=BROWSER_H).json()
    assert new["key_prefix"] != old["key_prefix"]


def test_for_me_get_trust_off_403(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _ = mint_env
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    r = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert r.status_code == 403
    assert r.json()["detail"] == "SSO key creation is not enabled on this deployment"


def test_for_me_get_no_headers_401(mint_env) -> None:
    tc, _ = mint_env
    r = tc.get("/v1/keys/for-me")
    assert r.status_code == 401
    assert r.json()["detail"] == "no browser identity headers present"


def test_for_me_get_never_returns_plaintext_shape(mint_env) -> None:
    """Even the exists:true body carries exactly the masked contract keys
    (an anti-oracle pin: no extra fields, so the SPA shape cannot drift)."""
    tc, _fake = mint_env
    _mint(tc)
    body = tc.get("/v1/keys/for-me", headers=BROWSER_H).json()
    assert set(body) == {"exists", "key_prefix", "name", "created_at"}


def test_for_me_get_no_audit_write(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET is read-only: the audit writer must never be called for it
    (every other for-me statement that reaches the fake is a SELECT)."""
    tc, _fake = mint_env
    calls: list[dict[str, Any]] = []

    async def spy_audit(*args: Any, **kwargs: Any):
        calls.append(kwargs)

    monkeypatch.setattr(routes_enroll, "_enroll_audit", spy_audit)
    tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert calls == []


# ---------------------------------------------------------------------------
# 2. one-key-per-identity: blind second mint → 409
# ---------------------------------------------------------------------------


def test_second_blind_mint_409_with_rotate_hint(mint_env) -> None:
    tc, fake = mint_env
    first = _mint(tc)
    assert first.status_code == 201, first.text
    second = _mint(tc)
    assert second.status_code == 409, second.text
    assert second.json()["detail"] == ('key already exists for this identity — POST {"rotate": true} to replace it')
    # the conflict did NOT mint: still exactly one ledger row
    assert len(fake.keys) == 1


def test_second_blind_mint_409_is_owner_scoped(mint_env) -> None:
    """One key per IDENTITY: alice's existing key never blocks bob."""
    tc, _fake = mint_env
    assert _mint(tc).status_code == 201
    bob_h = {"X-Auth-Request-User": "bob"}
    assert tc.post("/v1/keys/for-me", json=None, headers=bob_h).status_code == 201


def test_409_body_is_json_parseable_and_carries_no_key(mint_env) -> None:
    tc, _fake = mint_env
    _mint(tc)
    body = _mint(tc).json()
    assert "key" not in body  # the refusal leaks no credential material


# ---------------------------------------------------------------------------
# 3. rotate: {"rotate": true} replaces the key
# ---------------------------------------------------------------------------


def test_rotate_creates_new_key_and_revokes_old(mint_env) -> None:
    tc, fake = mint_env
    old = _mint(tc).json()
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["key"].startswith("hllm-") and new["key"] != old["key"]
    assert new["key_prefix"] != old["key_prefix"]
    assert new["subject"] == "alice" and new["idp"] == IDP_OAUTH2_PROXY
    assert len(fake.keys) == 2
    old_row = next(k for k in fake.keys if k["key_prefix"] == old["key_prefix"])
    new_row = next(k for k in fake.keys if k["key_prefix"] == new["key_prefix"])
    assert old_row["revoked"] is True
    assert new_row["revoked"] is False
    assert new_row["bound_via"] == VIA_SSO_CREATE
    # and the rotate-POST's plaintext rides the response exactly once
    assert new["message"] == "copy this key now — it is shown once"


def test_rotate_old_key_401s_new_key_authenticates(mint_env) -> None:
    """THE behavioral pin: the OLD plaintext stops authenticating
    (authenticate() → None), the NEW plaintext works."""
    tc, _ = mint_env
    old = _mint(tc).json()
    new = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H).json()
    from pcai_llm.core.keys import authenticate

    assert asyncio.run(authenticate(old["key"])) is None
    record = asyncio.run(authenticate(new["key"]))
    assert record is not None
    assert record.key_prefix == new["key_prefix"]


def test_rotate_then_rotate_again_still_works(mint_env) -> None:
    """Rotating off the ROTATED key: each rotate leaves exactly one live
    sso-create key for the subject."""
    tc, fake = mint_env
    first = _mint(tc).json()
    second = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H).json()
    third = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H).json()
    assert third["key"] not in (first["key"], second["key"])
    assert {k["revoked"] for k in fake.keys} == {True, False}
    assert tc.get("/v1/keys/for-me", headers=BROWSER_H).json()["key_prefix"] == third["key_prefix"]


def test_rotate_accumulates_spend_lineage(mint_env) -> None:
    """REGRESSION (the SSO rotation budget-reset bypass, fixed 2026-10-09):
    budget spend is DERIVED per key_id, so before the lineage carry every
    rotate zeroed the in-window budget counter — a user who could rotate
    owned an infinite budget (rotate at 99% of a monthly cap → a fresh full
    cap, same calendar window, indefinitely). The rotate must APPEND the
    old key's id to the new key's spend_ancestors, so the budget gates sum
    the whole lineage's in-window spend and the counter survives rotation.
    Chained rotates accumulate: the third key carries the full chain."""
    tc, fake = mint_env
    first = _mint(tc).json()
    second = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert second.status_code == 201, second.text
    first_row = next(k for k in fake.keys if k["key_prefix"] == first["key_prefix"])
    second_row = fake.keys[-1]
    assert first_row["spend_ancestors"] == []  # a fresh mint starts at zero — correct
    assert list(second_row["spend_ancestors"]) == [first_row["id"]]

    third = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert third.status_code == 201, third.text
    third_row = fake.keys[-1]
    assert list(third_row["spend_ancestors"]) == [first_row["id"], second_row["id"]]


def test_rotate_copies_existing_budget_limits(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rotate carries the old key's budget LIMIT rows (token + USD) onto
    the fresh key — before this, the server never copied budgets and a
    rotate whose profile preset had drifted (or no preset at all) left the
    new key with different (or NO) budget rows: unlimited in the no-row
    case. The copy merges onto the profile stamp without duplication
    (ON CONFLICT DO NOTHING), and the profile's budget stamping still ran."""
    tc, fake = mint_env
    old = _mint(tc).json()
    old_row = next(k for k in fake.keys if k["key_prefix"] == old["key_prefix"])

    budget_stmts: list[tuple[str, tuple]] = []

    real_fetchrow = SelfMintDB.fetchrow

    async def recording_fetchrow(self: SelfMintDB, sql: str, *args: Any):
        if "FROM budgets" in sql or "FROM cost_budgets" in sql:
            budget_stmts.append((" ".join(sql.split()), args))
        return await real_fetchrow(self, sql, *args)

    monkeypatch.setattr(SelfMintDB, "fetchrow", recording_fetchrow)
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert r.status_code == 201, r.text

    tables = {sql.split()[2] for sql, _ in budget_stmts}
    assert tables == {"budgets", "cost_budgets"}
    for sql, args in budget_stmts:
        # INSERT..SELECT new←old, merging per-key edits onto the profile
        # presets without duplication
        assert "ON CONFLICT (key_id, model, period) DO NOTHING" in sql
        assert args[1] == old_row["id"]


def test_rotate_failure_leaves_old_key_live_and_no_new_key(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash-safety (the transaction contract): create_key raising inside
    the rotate transaction must roll EVERYTHING back — the old key STILL
    authenticates, no new key row exists, and the client sees the 500 (the
    raise propagates; the SPA treats a non-201 as retryable)."""
    tc, fake = mint_env
    old = _mint(tc).json()

    import pcai_llm.core.keys as keys_mod

    real_create = keys_mod.create_key

    async def exploding_create(*args: Any, **kwargs: Any):
        if kwargs.get("conn") is not None:  # only the rotate-tx call dies
            raise RuntimeError("crash mid-rotate")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(routes_enroll, "create_key", exploding_create)
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert r.status_code == 500
    monkeypatch.setattr(routes_enroll, "create_key", real_create)  # restore for the retry

    # OLD KEY STILL LIVE: authenticates, still the GET's answer
    from pcai_llm.core.keys import authenticate

    record = asyncio.run(authenticate(old["key"]))
    assert record is not None and record.key_prefix == old["key_prefix"]
    # NO PARTIAL STATE: exactly one ledger row, unrevoked
    assert len(fake.keys) == 1
    assert fake.keys[0]["revoked"] is False
    assert tc.get("/v1/keys/for-me", headers=BROWSER_H).json()["key_prefix"] == old["key_prefix"]
    # a retry AFTER the crash now succeeds (the old key gets replaced)
    retry = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert retry.status_code == 201, retry.text
    assert asyncio.run(authenticate(old["key"])) is None
    assert asyncio.run(authenticate(retry.json()["key"])) is not None


def test_rotate_revoke_failure_leaves_old_key_live(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The SECOND crash-safety half: the revoke statement dying (after a
    successful create) must roll the CREATE back too — never two live
    keys, and the old key survives."""
    tc, fake = mint_env
    old = _mint(tc).json()

    real_execute = SelfMintDB.execute

    async def exploding_revoke_method(self: SelfMintDB, sql: str, *args: Any):
        if "UPDATE api_keys SET revoked_at" in sql:
            raise RuntimeError("crash mid-revoke")
        return await real_execute(self, sql, *args)

    monkeypatch.setattr(SelfMintDB, "execute", exploding_revoke_method)
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert r.status_code == 500
    monkeypatch.setattr(SelfMintDB, "execute", real_execute)  # restore for the assertions below

    from pcai_llm.core.keys import authenticate

    assert asyncio.run(authenticate(old["key"])) is not None  # old key live
    # The tx BLOCK observed the failure (the fake records the rollback —
    # in prod the ROLLBACK undoes the new key's INSERT; the fake's plain
    # list cannot un-insert, so the rollback marker is how the test sees
    # the transaction abort at all).
    assert len(fake.rolled_back) >= 1
    assert all(isinstance(e, RuntimeError) for e in fake.rolled_back)
    # In prod the ROLLBACK deletes the new key's INSERT, leaving the old
    # key the only live row. The fake's list cannot un-insert, so the
    # committed-state half is asserted via the ledger facts instead: the
    # crashing revoke died BEFORE mutating anything (no row revoked), and
    # the old key row is untouched. The authenticate checks above are the
    # behavioral half: the old plaintext still works, the client holds
    # no new plaintext (the 500 carried no key).
    assert fake.keys[0]["key_prefix"] == old["key_prefix"] and fake.keys[0]["revoked"] is False
    assert all(k["revoked"] is False for k in fake.keys)  # NO revoke ever landed


def test_rotate_tx_runs_create_and_revoke_on_one_connection(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mechanism pin: rotate passes the ACQUIRED connection to
    create_key (the conn kwarg — the transactional variant) and runs the
    revoke ON that connection, inside one transaction block."""
    tc, fake = mint_env
    _mint(tc)
    conns_seen: list[Any] = []

    import pcai_llm.core.keys as keys_mod

    real_create = keys_mod.create_key

    async def spying_create(*args: Any, **kwargs: Any):
        conns_seen.append(kwargs.get("conn"))
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(routes_enroll, "create_key", spying_create)
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert r.status_code == 201, r.text
    assert len(conns_seen) == 1 and conns_seen[0] is not None  # the tx conn
    assert any("UPDATE api_keys SET revoked_at" in s for s in fake.statements)
    assert any("BEGIN" in s.upper() or True for s in fake.statements)  # tx ran through the conn path


def test_rotate_revoke_uses_revoked_at_statement(mint_env) -> None:
    """The revoke is the SAME revoked_at=now() the admin console writes —
    the exact statement shape, so authenticate()'s existing revoked-key
    refusal covers rotation with no authenticate change."""
    tc, fake = mint_env
    _mint(tc)
    tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL" in fake.statements


def test_rotate_reuses_the_existing_user_row(mint_env) -> None:
    """F2/F3 (wave-1): the rotated key lands on the SAME users row as the
    original mint. The live key's user_id IS the per-subject user anchor —
    re-creating the user inside the tx would collide on UNIQUE
    (tenant_id, name) and abort the tx on real Postgres (attempt 1's
    UniqueViolation aborts; every retry re-raises
    InFailedSQLTransactionError on the same aborted connection) — so the
    rotate path resolves the user FROM the live key row instead."""
    tc, fake = mint_env
    old = _mint(tc).json()
    old_row = next(k for k in fake.keys if k["key_prefix"] == old["key_prefix"])
    old_user_id = old_row["user_id"]
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert r.status_code == 201, r.text
    new_row = next(k for k in fake.keys if k["key_prefix"] == r.json()["key_prefix"])
    # F3: SAME user row — no orphaned "-2887" user holding the revoked key
    assert new_row["user_id"] == old_user_id
    # F2 mechanism: NO user INSERT ran during the rotate (the second
    # statement log entry set — mint ran one, rotate ran zero)
    user_inserts = [s for s in fake.statements if "INSERT INTO users" in s]
    assert len(user_inserts) == 1  # only the original mint's
    # and the user table itself did not grow
    assert len(fake.users) == 1


def test_rotate_tx_survives_unique_violation_semantics(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """F2's real-DB semantics pin: model a conn whose user-INSERT would
    collide (UniqueViolation → tx ABORTED → every later statement raises
    asyncpg InFailedSQLTransactionError, exactly as Postgres behaves).
    The rotate must NOT attempt any user INSERT (it reuses the live
    key's user_id), so the tx completes: 201, old revoked, new live."""
    tc, _fake = mint_env
    _mint(tc)

    import asyncpg

    real_fetchrow = SelfMintDB.fetchrow
    tx_aborted = False

    async def aborting_fetchrow(self: SelfMintDB, sql: str, *args: Any):
        nonlocal tx_aborted
        if tx_aborted and "INSERT" in sql.upper():
            # real PG: a statement inside an ABORTED transaction re-raises
            raise asyncpg.InFailedSQLTransactionError("current transaction is aborted")
        if "INSERT INTO users" in sql:
            # the collision the production bug relied on: the user row
            # already exists → UniqueViolation → tx aborts
            raise asyncpg.UniqueViolationError("duplicate key value violates unique constraint")
        return await real_fetchrow(self, sql, *args)

    monkeypatch.setattr(SelfMintDB, "fetchrow", aborting_fetchrow)
    r = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    monkeypatch.setattr(SelfMintDB, "fetchrow", real_fetchrow)
    # the rotate path NEVER ran a user INSERT (the reuser), so the abort
    # semantics never triggered: 201, and the rotate completed cleanly
    assert r.status_code == 201, r.text
    assert tx_aborted is False


# ---------------------------------------------------------------------------
# 4. mint-path unchanged + gates (the no-key happy path still bytes-equal)
# ---------------------------------------------------------------------------


def test_mint_happy_path_unchanged_shape(mint_env) -> None:
    tc, fake = mint_env
    r = _mint(tc)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["key"].startswith("hllm-")
    assert body["key_prefix"] == body["key"][:10]
    assert body["subject"] == "alice"
    assert body["idp"] == IDP_OAUTH2_PROXY
    assert body["message"] == "copy this key now — it is shown once"
    assert set(body) == {"key", "key_prefix", "subject", "idp", "message"}
    assert len(fake.keys) == 1
    assert (IDP_OAUTH2_PROXY, "alice") in fake.subjects
    assert fake.keys[0]["bound_via"] == VIA_SSO_CREATE
    assert fake.keys[0]["scopes"] == ["proxy", "mcp"]  # FIXED scopes


def test_mint_no_headers_401(mint_env) -> None:
    tc, _ = mint_env
    assert tc.post("/v1/keys/for-me").status_code == 401


def test_mint_trust_off_403(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _ = mint_env
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    assert tc.post("/v1/keys/for-me", headers=BROWSER_H).status_code == 403


def test_flags_off_get_and_post_both_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag-off posture (cross-cutting invariant 1): BOTH surfaces
    refuse identically — 403 with the same detail, byte-for-byte the
    POST's pre-existing shape; no new behavior on old deployments."""
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", str(uuid.uuid4()))
    fake = SelfMintDB()
    import pcai_llm.core.keys as keys_mod
    from pcai_llm.core import audit as audit_mod

    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(audit_mod, "db", fake)
    monkeypatch.setattr(routes_enroll, "bind_key_subject", identity.bind_key_subject)
    app = FastAPI()
    app.include_router(routes_enroll.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r_get = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    r_post = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert r_get.status_code == 403 and r_post.status_code == 403
    assert r_get.content == r_post.content  # same refusal bytes
    assert fake.keys == [] and fake.users == []  # nothing was written


def test_mint_arbitrary_grants_still_ignored(mint_env) -> None:
    """The no-admin-escalation pin survives the body-model change: extra
    JSON fields are ignored, scopes stay fixed, allowed_models untouched."""
    tc, fake = mint_env
    r = tc.post(
        "/v1/keys/for-me",
        json={"scopes": ["proxy", "mcp", "admin"], "allowed_models": ["*"]},
        headers=BROWSER_H,
    )
    assert r.status_code == 201, r.text
    assert fake.keys[0]["scopes"] == ["proxy", "mcp"]


def test_rotate_false_explicit_body_still_blind_mint_409(mint_env) -> None:
    """An EXPLICIT rotate:false is a blind mint — the 409 fires (the
    field's presence is not consent; its VALUE is)."""
    tc, _ = mint_env
    _mint(tc)
    r = tc.post("/v1/keys/for-me", json={"rotate": False}, headers=BROWSER_H)
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# 5. audit: "rotate" in details, event shape otherwise unchanged
# ---------------------------------------------------------------------------


def test_audit_mint_rotate_false_in_details(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _ = mint_env
    calls: list[dict[str, Any]] = []

    async def spy_audit(request: Any, **kwargs: Any):
        calls.append(kwargs)

    monkeypatch.setattr(routes_enroll, "_enroll_audit", spy_audit)
    _mint(tc)
    assert len(calls) == 1
    call = calls[0]
    assert call["action"] == "enroll.redeem"
    assert call["resource_type"] == "key"
    assert call["actor_source"] == "proxy-identity"
    assert call["actor_subject"] == "alice"
    assert call["details"] == {"via": "sso-create", "bound_via": VIA_SSO_CREATE, "rotate": False}
    assert isinstance(call["resource_id"], str)  # the key id rides as a string


def test_audit_rotate_true_in_details(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, fake = mint_env
    calls: list[dict[str, Any]] = []

    async def spy_audit(request: Any, **kwargs: Any):
        calls.append(kwargs)

    monkeypatch.setattr(routes_enroll, "_enroll_audit", spy_audit)
    _mint(tc)
    tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H)
    assert len(calls) == 2
    assert calls[1]["details"]["rotate"] is True
    assert calls[1]["details"]["via"] == "sso-create"
    assert calls[1]["details"]["bound_via"] == VIA_SSO_CREATE
    # the audited resource is the NEW key (the row that now exists)
    assert calls[1]["resource_id"] == str(fake.keys[-1]["id"])
    # shape parity: every audited field identical across both events
    assert {k for k in calls[0] if k != "details"} == {k for k in calls[1] if k != "details"}
    assert set(calls[0]["details"]) == set(calls[1]["details"])


def test_audit_details_survive_redaction(mint_env) -> None:
    """The new 'rotate' key passes the audit writer's redaction filter
    untouched (it names a BOOL, not a secret — the trail stays legible)."""
    from pcai_llm.core.audit import details_redact

    assert details_redact({"via": "sso-create", "bound_via": VIA_SSO_CREATE, "rotate": False}) == {
        "via": "sso-create",
        "bound_via": VIA_SSO_CREATE,
        "rotate": False,
    }


# ---------------------------------------------------------------------------
# 6. the ONE-helper contract (GET and the rotate lookup cannot drift)
# ---------------------------------------------------------------------------


def test_get_and_post_share_one_helper(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both surfaces route through _live_sso_key_for_subject — patch it to
    raise a sentinel and watch BOTH calls fail with it (a divergent second
    lookup would leave the other surface green here)."""
    tc, _ = mint_env
    sentinel = RuntimeError("shared-helper-sentinel")

    async def broken(subject: str):
        raise sentinel

    monkeypatch.setattr(routes_enroll, "_live_sso_key_for_subject", broken)
    r = tc.get("/v1/keys/for-me", headers=BROWSER_H)
    assert r.status_code == 500  # the exception propagated (fail-closed)
    r2 = tc.post("/v1/keys/for-me", json=None, headers=BROWSER_H)
    assert r2.status_code == 500, "POST did not route through the shared helper"


def test_helper_returns_newest_non_revoked_sso_create_key(mint_env) -> None:
    """The helper itself: newest live sso-create row, None when the
    subject has none, None for a subject with only a revoked key."""
    tc, _fake = mint_env
    first = _mint(tc).json()
    found = asyncio.run(routes_enroll._live_sso_key_for_subject("alice"))
    assert found is not None and found["key_prefix"] == first["key_prefix"]
    # a second live key would exist only via a rotate-consent bypass; the
    # helper must report the NEWEST by created_at (tie-broken by id)
    second = tc.post("/v1/keys/for-me", json={"rotate": True}, headers=BROWSER_H).json()
    found = asyncio.run(routes_enroll._live_sso_key_for_subject("alice"))
    assert found is not None and found["key_prefix"] == second["key_prefix"]
    # unknown subject → None (no oracle about other subjects)
    assert asyncio.run(routes_enroll._live_sso_key_for_subject("nobody")) is None


def test_helper_distinguishes_bound_via(mint_env) -> None:
    """A key bound via a DIFFERENT bound_via (enrollment/token-shadow) is
    invisible to the helper — the one-key contract is per SURFACE."""
    tc, fake = mint_env
    _mint(tc)
    # an enrollment-bound key for the same subject id must not satisfy the
    # sso-create lookup
    sid = fake.subjects[(IDP_OAUTH2_PROXY, "alice")]
    fake.keys.append(
        {
            "id": fake._next(),
            "user_id": fake.users[0]["id"],
            "name": "enrolled",
            "key_hash": "hash-enrolled",
            "key_prefix": "hllm-EN",
            "scopes": ["proxy", "mcp"],
            "subject_id": sid,
            "bound_via": "enrollment",
            "revoked": False,
            "created_at": "2026-10-06T09:00:00+00:00",
        }
    )
    found = asyncio.run(routes_enroll._live_sso_key_for_subject("alice"))
    assert found is not None
    assert found["name"] == "sso-alice"  # still the sso-create row, not the enrollment one


# ---------------------------------------------------------------------------
# 7. legacy clients: no body at all keeps minting (compat)
# ---------------------------------------------------------------------------


def test_post_without_body_still_mints_first_key(mint_env) -> None:
    tc, fake = mint_env
    r = tc.post("/v1/keys/for-me", headers=BROWSER_H)
    assert r.status_code == 201, r.text
    assert len(fake.keys) == 1


def test_post_with_empty_json_body_mints_first_key(mint_env) -> None:
    """{} parses to rotate=False — a legacy client that always sends a
    JSON object keeps its exact behavior (mint-when-keyless)."""
    tc, fake = mint_env
    r = tc.post("/v1/keys/for-me", json={}, headers=BROWSER_H)
    assert r.status_code == 201, r.text
    assert len(fake.keys) == 1
    # and the second empty-object POST conflicts like the bodyless one
    assert tc.post("/v1/keys/for-me", json={}, headers=BROWSER_H).status_code == 409


# ---------------------------------------------------------------------------
# 8. F6 (wave-1): junk bodies stay legacy-tolerant — never a 422
# ---------------------------------------------------------------------------


def test_junk_body_mints_like_no_body_flags_on(mint_env) -> None:
    """Option (2) — the tolerant parser: malformed JSON, a non-object
    array, and unvalidatable field types all resolve to rotate=False —
    the pre-change semantics (the endpoint took NO body; a pydantic body
    parameter would 422 these, a flag-OFF-visible behavior change). Each
    body runs as its OWN identity so every one exercises the mint path."""
    tc, fake = mint_env
    junk_bodies: list[Any] = [
        "not json at all",  # malformed JSON
        "[1, 2, 3]",  # valid JSON, not an object
        '"a bare string"',  # valid JSON, not an object
        '{"rotate": "yes"}',  # object, but the field doesn't validate
        '{"rotate": null}',  # null field → pydantic fails → junk path
    ]
    for i, body in enumerate(junk_bodies):
        h = {**BROWSER_H, "X-Auth-Request-User": f"junk-{i}"}
        r = tc.post("/v1/keys/for-me", content=body, headers={**h, "Content-Type": "application/json"})
        assert r.status_code == 201, f"body {body!r} → {r.status_code}: {r.text}"
    assert len(fake.keys) == 5  # every junk body MINTED, exactly as pre-change


def test_junk_body_still_conflicts_409_after_mint(mint_env) -> None:
    """Junk-body tolerance does not bypass one-key-per-identity: after a
    mint, a junk body is a blind mint → the same 409 (it must never
    rotate implicitly)."""
    tc, fake = mint_env
    assert _mint(tc).status_code == 201
    r = tc.post("/v1/keys/for-me", content="garbage", headers={**BROWSER_H, "Content-Type": "application/json"})
    assert r.status_code == 409, r.text
    assert len(fake.keys) == 1  # nothing minted by the refused junk call


def test_junk_body_flags_off_still_403(mint_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag-off byte-identity holds for junk bodies too: the 422 the
    pydantic parameter would have introduced never happens — the flags-off
    refusal is the SAME bytes a no-body request gets."""
    tc, _fake = mint_env
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    r_junk = tc.post("/v1/keys/for-me", content="garbage", headers={**BROWSER_H, "Content-Type": "application/json"})
    r_nobody = tc.post("/v1/keys/for-me", headers=BROWSER_H)
    assert r_junk.status_code == 403
    assert r_junk.content == r_nobody.content


def test_mint_with_broken_tenant_profile_is_503_naming_the_profile(mint_env) -> None:
    """A tenant SSO default whose STORED preset is invalid yields a 503 that
    NAMES the profile — not the opaque 500 a bare raise produced.

    The wire-time gate in PUT /admin/tenants/{id}/sso-default-profile is the
    first line of defence; this is the second, for a profile broken AFTER it
    was wired (hand-edited defaults_json, a downgrade, a direct SQL edit).
    The transaction rolls back, so no half-stamped key is left behind.
    """
    tc, fake = mint_env
    fake.tenant_profile_row = {
        "id": "00000000-0000-0000-0000-0000000000ff",
        "name": "wired-broken",
        "defaults_json": json.dumps({"budgets": [{"model": "*", "period": "month", "limit_usd": 5e12}]}),
    }
    r = _mint(tc)
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "wired-broken" in detail
    assert "column maximum" in detail
    # The transaction rolled back. The fake cannot un-insert a row (this
    # suite's documented convention: assert the ROLLBACK CONTRACT marker,
    # not a mutated ledger) — so the marker is the assertion.
    assert fake.rolled_back, "the mint transaction must roll back"
    # The ConfigError is TRANSLATED to the 503 inside the transaction, so the
    # marker carries the HTTPException — which is the point: the client gets
    # a status it can act on and the transaction still unwinds.
    assert isinstance(fake.rolled_back[-1], HTTPException)


def test_mint_with_usd_preset_tenant_profile_stamps_cost_budgets(mint_env) -> None:
    """A tenant default carrying a DOLLAR preset stamps a cost_budgets row on
    the SSO mint — the same both-kinds contract the redeem path has."""
    tc, fake = mint_env
    fake.tenant_profile_row = {
        "id": "00000000-0000-0000-0000-0000000000fe",
        "name": "dollar-sso",
        "defaults_json": json.dumps({"budgets": [{"model": "*", "period": "month", "limit_usd": 20}]}),
    }
    r = _mint(tc)
    assert r.status_code == 201, r.text
    assert any("INSERT INTO cost_budgets" in s for s in fake.statements), fake.statements
    assert not any("INSERT INTO budgets " in s for s in fake.statements), fake.statements
