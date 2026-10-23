"""Admin API tests — auth gate + validation, mocked DB.

The admin dependency is the fail-closed boundary: 503 when disabled, 401 on
bad/missing keys (hash-space compare), and strict payload validation before
anything reaches Postgres.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.core.keys import hash_key


class FakeConn:
    """Stands in for an acquired asyncpg connection (transaction + all).
    fetchrow/execute delegate to the parent fake so SQL-shape dispatch
    (RotateDB's create→copy-grants→creds→revoke sequence, the transactional
    rotate from audit finding 5) works through the conn exactly as it did
    through the module pool."""

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

    async def fetchval(self, sql: str, *args: Any):
        return await self._db.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, str]]:
        """Default: no rows (the DB seam this fake replaces has no generic
        fetch); CountFakeDB.acquire overrides it per-instance below."""
        return []

    async def executemany(self, sql: str, args: Any):
        self._db.executed.append((sql, list(args)))


class FakeDB:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.executed: list[tuple] = []

    def acquire(self):
        return FakeConn(self)

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM profiles" in sql:
            return None  # no profile unless the test injects one
        if "FROM users" in sql:
            return {"id": args[0]} if args[0] != uuid.UUID(int=0) else None
        return {"id": uuid.uuid4(), **getattr(self, "inserted", {})}

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def execute(self, sql: str, *args: Any):
        return "INSERT 0 1"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = FakeDB()
    # Patch every db seam the admin path touches: routes_admin's own handle
    # AND core.keys/core.budgets module-level references (imported by value).
    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-123"}


def test_admin_disabled_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "")
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/keys")
    assert r.status_code == 503


def test_admin_bad_key_401(client) -> None:
    tc, _ = client
    assert tc.get("/admin/keys", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert tc.get("/admin/keys").status_code == 401  # missing header


def test_admin_good_key_passes(client) -> None:
    tc, _ = client
    r = tc.get("/admin/keys", headers=_h())
    assert r.status_code == 200


def test_create_key_invalid_scope_400(client) -> None:
    tc, _ = client
    r = tc.post(
        "/admin/keys",
        json={"user_id": str(uuid.uuid4()), "name": "k", "scopes": ["bogus"]},
        headers=_h(),
    )
    assert r.status_code == 400


def test_create_budget_bad_period_400(client) -> None:
    tc, _ = client
    kid = uuid.uuid4()
    r = tc.post(
        f"/admin/keys/{kid}/budgets",
        json={"budgets": [{"model": "*", "period": "fortnight", "limit_tokens": 100}]},
        headers=_h(),
    )
    assert r.status_code == 400


def test_create_budget_nonpositive_400(client) -> None:
    tc, _ = client
    kid = uuid.uuid4()
    r = tc.post(
        f"/admin/keys/{kid}/budgets",
        json={"budgets": [{"model": "*", "period": "day", "limit_tokens": 0}]},
        headers=_h(),
    )
    assert r.status_code == 400


def test_create_model_bad_url_400(client) -> None:
    tc, _ = client
    r = tc.post(
        "/admin/models",
        json={"model_name": "m", "endpoints": [{"base_url": "ftp://nope"}]},
        headers=_h(),
    )
    assert r.status_code == 400


def test_create_key_returns_plaintext_once(client) -> None:
    tc, _ = client
    r = tc.post(
        "/admin/keys",
        json={"user_id": str(uuid.uuid4()), "name": "k"},
        headers=_h(),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["key"].startswith("hllm-")
    assert body["key_prefix"] == body["key"][:10]  # prefix is derived, not a replacement
    assert "key_hash" not in body  # hash never leaves the server


def test_delete_model_with_slash_in_name(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """'Qwen/Qwen3.8-27B-FP8' broke the console remove with a plain path param
    (single segment; a %2F-encoded URL is decoded back into the path before
    routing) — every slash-named pool 404'd on delete. `{model_name:path}`
    must carry slash names, in both raw and %2F-encoded form."""
    import pcai_llm.core.registry as registry_mod

    tc, _fake = client

    class CountFakeDB(FakeDB):
        async def fetchrow(self, sql: str, *args: Any):
            if "count(*)" in sql:
                return {"n": 2}
            return await super().fetchrow(sql, *args)

        def acquire(self):
            conn = FakeConn(self)

            # conn.fetch returns ROWS (the handler len()s them) — the old
            # fake returned a list from fetchval (a SCALAR in real asyncpg),
            # which masked the TypeError-500 on every real delete
            async def fetch(sql: str, *args: Any) -> list[dict[str, str]]:
                return [{"id": "ep1"}, {"id": "ep2"}]

            conn.fetch = fetch  # type: ignore[method-assign]
            return conn

    monkeypatch.setattr(routes_admin, "db", CountFakeDB())

    async def _noop():
        return None

    monkeypatch.setattr(registry_mod, "refresh", _noop)

    r = tc.delete("/admin/models/Qwen/Qwen3.8-27B-FP8", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "Qwen/Qwen3.8-27B-FP8"
    assert body["endpoints_removed"] == 2

    r = tc.delete("/admin/models/Qwen%2FQwen3.8-27B-FP8", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "Qwen/Qwen3.8-27B-FP8"


# ---------------------------------------------------------------------------
# key rotation (POST /admin/keys/{key_id}/rotate)
# ---------------------------------------------------------------------------


def _old_key_row(**over: Any) -> dict[str, Any]:
    """A faithful api_keys row for rotate's SELECT (every one of these
    columns must be carried onto the new key). subject_id/bound_via/bound_at
    are the Phase A identity binding — an UNBOUND key carries NULL/''/NULL
    verbatim; the bound-key carry is pinned by test_rotate_carries_binding."""
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "ci-bot",
        "scopes": ["proxy", "mcp"],
        "allowed_models": ["m1"],
        "rate_limit_rpm": 30,
        "rate_limit_tpm": 50_000,
        "max_concurrent": 4,
        "expires_at": None,
        "relay_default": True,
        "stream_pcai_events": True,
        "revoked_at": None,
        "spend_ancestors": [],
        "subject_id": None,
        "bound_via": "",
        "bound_at": None,
    }
    base.update(over)
    return base


class RotateDB(FakeDB):
    """api_keys-aware fake for the rotate flow: the SELECT returns `old_row`,
    the INSERT mints a fresh id (args recorded for the carry-over assert),
    execute() replays a configurable command tag (the revoke's UPDATE tag or
    the grant-copy's INSERT tag — one row either way unless overridden), and
    every statement is recorded IN ORDER so the
    create → copy-grants → revoke ordering is assertable."""

    def __init__(
        self,
        old_row: dict[str, Any] | None,
        revoke_tag: str = "UPDATE 1",
        grant_copy_tag: str = "INSERT 0 1",
        creds_copy_tag: str = "INSERT 0 1",
        budget_copy_tag: str = "INSERT 0 1",
    ) -> None:
        super().__init__()
        self.old_row = old_row
        self.revoke_tag = revoke_tag
        self.grant_copy_tag = grant_copy_tag
        self.creds_copy_tag = creds_copy_tag
        self.budget_copy_tag = budget_copy_tag
        self.calls: list[str] = []
        self.insert_args: tuple = ()
        self.grant_copy_args: tuple = ()
        self.grant_copy_sql: str = ""
        self.creds_copy_args: tuple = ()
        self.creds_copy_sql: str = ""
        self.budget_copy_sqls: list[str] = []
        self.budget_copy_args: list[tuple] = []

    async def fetchrow(self, sql: str, *args: Any):
        # ONE dispatch method (a second def would shadow this one — Python
        # keeps the last): the old-key SELECT, the new-key INSERT, and the
        # conditional revoke (audit finding 5 — UPDATE..RETURNING; the
        # configured revoke_tag decides whether a row came back: "UPDATE 1"
        # → revoked, anything else → already-revoked no-op).
        if "FROM api_keys" in sql:
            self.calls.append("select old key")
            return self.old_row
        if "INSERT INTO api_keys" in sql:
            self.calls.append("insert new key")
            self.insert_args = args
            return {"id": uuid.uuid4()}
        if "UPDATE api_keys" in sql and "revoked_at IS NULL" in sql:
            self.calls.append("revoke old key")
            if self.revoke_tag.endswith("1"):
                return {"id": uuid.uuid4()}
            return None
        return await super().fetchrow(sql, *args)

    async def execute(self, sql: str, *args: Any):
        if "FROM mcp_tool_acls" in sql:
            self.calls.append("copy mcp grants")
            self.grant_copy_args = args
            self.grant_copy_sql = sql
            return self.grant_copy_tag
        if "FROM mcp_server_creds" in sql:
            self.calls.append("copy mcp creds")
            self.creds_copy_args = args
            self.creds_copy_sql = sql
            return self.creds_copy_tag
        if "FROM budgets" in sql or "FROM cost_budgets" in sql:
            self.calls.append("copy budget limits")
            self.budget_copy_sqls.append(sql)
            self.budget_copy_args.append(args)
            return self.budget_copy_tag
        # (the revoke no longer goes through execute — it is a conditional
        # UPDATE..RETURNING via fetchrow above, inside the rotation txn)
        self.calls.append("revoke old key (legacy tag path)")
        return self.revoke_tag


def _install_rotate_db(monkeypatch: pytest.MonkeyPatch, db: RotateDB) -> None:
    # routes_admin's handler AND core.keys.revoke_key (db resolved from the
    # core.keys module global at call time) must see the SAME fake
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(routes_admin, "db", db)
    monkeypatch.setattr(keys_mod, "db", db)


def test_rotate_key_full_contract(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rotate = NEW key with the SAME name + attributes, old key revoked —
    and the full plaintext is in the response exactly once (hash-only
    storage means the plaintext must not appear in any INSERT argument)."""
    tc, _ = client
    old = _old_key_row()
    db = RotateDB(old)
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=_h())
    assert r.status_code == 201, r.text
    body = r.json()
    # full plaintext ONCE + both ids
    assert body["key"].startswith("hllm-") and len(body["key"]) > 40
    assert body["key_prefix"] == body["key"][:10]
    assert body["old_key_id"] == str(old["id"])
    assert body["id"] != body["old_key_id"]
    assert body["old_revoked"] is True
    assert body["grants_copied"] == 1  # grant rows carried over (fake tag INSERT 0 1)
    assert body["creds_copied"] == 1  # credential-override rows carried too
    assert (
        body["budgets_copied"] == 2
    )  # token + USD budget LIMIT rows carried (2 statements' tags)    assert "key_hash" not in body  # hash never leaves the server
    # SAME name + attributes carried onto the INSERT (12 base columns — the
    # RFC-PER-KEY-STREAM-EVENTS flag joined — + spend_ancestors (the
    # budget-carry lineage) + the 3 Phase A identity columns —
    # subject_id/bound_via/bound_at, which the rotate regression test below
    # pins for a BOUND key)
    insert_args = db.insert_args
    assert len(insert_args) == 16
    assert insert_args[0] == old["user_id"]
    assert insert_args[1] == "ci-bot"  # same NAME (feedback: "same name")
    assert insert_args[4] == ["proxy", "mcp"]  # scopes
    assert insert_args[5] == ["m1"]  # allowed_models
    assert insert_args[6:9] == (30, 50_000, None)  # rpm/tpm/expires_at
    assert insert_args[9] is True  # relay_default
    assert (
        insert_args[10] is True
    )  # stream_pcai_events — rotation copies the OLD row's value verbatim (default flip: NEW keys mint False unless opted in)
    assert insert_args[11] == 4  # max_concurrent
    # spend lineage: the new key inherits the old row's ancestors + the old
    # id itself — the budget gates sum the whole chain (spend survives)
    assert list(insert_args[12]) == [old["id"]]
    assert insert_args[13] is None and insert_args[14] == "" and insert_args[15] is None  # unbound key
    assert insert_args[3] == body["key"][:10]  # key_prefix
    # hash-only storage: plaintext is never an argument, its hash is
    assert hash_key(body["key"]) == insert_args[2]
    assert all(body["key"] != a for a in insert_args if isinstance(a, str))
    # ORDER: create → copy grants → copy creds → copy budget limits → revoke
    # (the partial-failure contract: identity rows AND budget limits land on
    # the new key while the old one exists; all inside the one transaction)
    assert db.calls == [
        "select old key",
        "insert new key",
        "copy mcp grants",
        "copy mcp creds",
        "copy budget limits",
        "copy budget limits",
        "revoke old key",
    ]
    # the copies moved the rows old→new: INSERT..SELECT $new_id ... WHERE key_id = $old_id
    assert db.grant_copy_args == (uuid.UUID(body["id"]), old["id"])
    assert db.creds_copy_args == (uuid.UUID(body["id"]), old["id"])
    # the budget copies hit BOTH families and merge per-key edits onto the
    # profile presets without duplication
    assert {sql.split()[2] for sql in db.budget_copy_sqls} == {"budgets", "cost_budgets"}
    assert all("ON CONFLICT (key_id, model, period) DO NOTHING" in sql for sql in db.budget_copy_sqls)
    assert all(args == (uuid.UUID(body["id"]), old["id"]) for args in db.budget_copy_args)
    # the creds copy preserves the ciphertext VERBATIM (key-independent: the
    # master key decrypts it, not the key credential)
    assert "headers_enc" in db.creds_copy_sql and "server_name" in db.creds_copy_sql


def test_rotate_unknown_key_404(client, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _ = client
    db = RotateDB(None)
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{uuid.uuid4()}/rotate", headers=_h())
    assert r.status_code == 404
    assert db.calls == ["select old key"]  # refused before any write


def test_rotate_revoked_key_refused_409(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a revoked key is audit history (the usage ledger points
    at it), not a live credential to re-issue from."""
    tc, _ = client
    old = _old_key_row(revoked_at="2025-01-01T00:00:00+00:00")
    db = RotateDB(old)
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=_h())
    assert r.status_code == 409
    assert "revoked" in r.json()["detail"]
    assert db.calls == ["select old key"]  # refused before any write


def test_rotate_requires_admin_key(client) -> None:
    tc, _ = client
    kid = uuid.uuid4()
    assert tc.post(f"/admin/keys/{kid}/rotate").status_code == 401  # missing bearer
    assert tc.post(f"/admin/keys/{kid}/rotate", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_rotate_revoke_race_still_201(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """A concurrent revoke between rotate's SELECT and its UPDATE makes the
    revoke a no-op guard: the request still succeeds (the new key is live)
    and the response says old_revoked=false instead of failing."""
    tc, _ = client
    old = _old_key_row()
    db = RotateDB(old, revoke_tag="UPDATE 0")
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["old_revoked"] is False


def test_rotate_grants_copied_allow_and_deny_rows(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-09-18 incident fix: a rotated key must NOT lose its
    federation grants (default-deny turned a fresh key_id with zero ACL rows
    into ~102 tools → 2). The carry-over is ONE INSERT..SELECT copying EVERY
    old row — allow AND allowed=FALSE deny rows both (a deny documents a
    deliberate exception; dropping it would silently resurrect the grant)."""
    tc, _ = client
    old = _old_key_row()
    db = RotateDB(old, grant_copy_tag="INSERT 0 102")  # e.g. the fleet catalog
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=_h())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["grants_copied"] == 102
    # single statement, verbatim column copy — the SQL itself preserves the
    # `allowed` column (allow AND deny rows alike); no per-row executemany
    # with a hardcoded TRUE (that shape would erase documented denies).
    assert "SELECT $1, server, tool, allowed FROM mcp_tool_acls" in db.grant_copy_sql
    assert db.grant_copy_args == (uuid.UUID(body["id"]), uuid.UUID(body["old_key_id"]))


def test_rotate_grants_copied_zero_when_old_key_had_none(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old key with no ACL rows rotates cleanly: grants_copied=0, no
    failure — the carry-over is an empty copy, not an error."""
    tc, _ = client
    old = _old_key_row()
    db = RotateDB(old, grant_copy_tag="INSERT 0 0")
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["grants_copied"] == 0


def test_rotate_creds_copied_and_counted(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-server MCP credential overrides (mcp_server_creds — the
    memory-MCP X-Memory-Dataset / X-Dataset-Password identity) must survive
    rotation like the grants do. Losing them forced the user to re-enter
    dataset + password after every rotate — which the release-iteration
    workflow (recreate the key, bump the tag) hit every single time. The
    ciphertext is key-INDEPENDENT (Fernet under the master key, not under
    the key credential), so the copy is a verbatim INSERT..SELECT."""
    tc, _ = client
    old = _old_key_row()
    db = RotateDB(old, creds_copy_tag="INSERT 0 1")  # one server's override set
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=_h())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["creds_copied"] == 1
    # verbatim copy of the ciphertext — no decrypt/re-encrypt round trip
    # (the master key is the only secret involved and it did not change)
    assert "SELECT $1, server_name, headers_enc FROM mcp_server_creds" in db.creds_copy_sql
    assert db.creds_copy_args == (uuid.UUID(body["id"]), uuid.UUID(body["old_key_id"]))


def test_rotate_creds_copied_zero_when_old_key_had_none(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old key with no stored MCP credentials rotates cleanly:
    creds_copied=0, no failure."""
    tc, _ = client
    old = _old_key_row()
    db = RotateDB(old, creds_copy_tag="INSERT 0 0")
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["creds_copied"] == 0


def test_rotate_extends_spend_lineage_of_a_rotated_key(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """REGRESSION (the SSO rotation budget-reset bypass, fixed 2026-10-09):
    spend is DERIVED — SUM(usage_ledger WHERE key_id = …) — so before the
    lineage carry, every rotation zeroed the in-window budget counter and a
    user who could rotate owned an infinite budget. Rotate the ROTATED key:
    the new row's spend_ancestors must be old.spend_ancestors || [old.id] —
    the WHOLE chain, not just the immediate predecessor."""
    tc, _ = client
    grandparent = uuid.uuid4()
    parent = _old_key_row(spend_ancestors=[grandparent])
    db = RotateDB(parent)
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{parent['id']}/rotate", headers=_h())
    assert r.status_code == 201, r.text
    # insert arg 12 = spend_ancestors: grandparent chain + the parent id
    assert list(db.insert_args[12]) == [grandparent, parent["id"]]


def test_rotate_budgets_copied_zero_when_old_key_had_none(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old key with no budget rows rotates cleanly: budgets_copied=0,
    no failure (the carry-over is an empty copy, not an error)."""
    tc, _ = client
    old = _old_key_row()
    db = RotateDB(old, budget_copy_tag="INSERT 0 0")
    _install_rotate_db(monkeypatch, db)
    r = tc.post(f"/admin/keys/{old['id']}/rotate", headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["budgets_copied"] == 0


# ---------------------------------------------------------------------------
# MCP grants copy-from (POST /admin/keys/{key_id}/mcp-grants/copy-from)
# ---------------------------------------------------------------------------


class _GrantsCopyDB(FakeDB):
    """Fake for the copy-from flow: `_exists` answers for exactly the ids in
    `existing` (404s for anything else), a real count() for the self-copy
    path, and the DELETE + INSERT..SELECT pair recorded per statement."""

    def __init__(self, existing: set[uuid.UUID] | None = None) -> None:
        super().__init__()
        self.existing = existing if existing is not None else set()
        self.calls: list[str] = []
        self.copy_args: tuple = ()

    async def fetchval(self, sql: str, *args: Any):
        if "FROM api_keys" in sql:
            return 1 if args[0] in self.existing else None  # _exists
        if "count(*)" in sql:
            return 2  # self-copy path: the key already holds 2 grant rows
        return await super().fetchval(sql, *args)

    def acquire(self):
        return _CopyConn(self)


class _CopyConn:
    """Acquired-connection fake recording the delete + copy pair in order."""

    def __init__(self, db: _GrantsCopyDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def execute(self, sql: str, *args: Any):
        self._db.calls.append("delete target grants" if "DELETE" in sql else "insert select copy")
        if "INSERT" in sql:
            self._db.copy_args = args
        return "INSERT 0 3"

    async def fetchval(self, sql: str, *args: Any):
        return None


def _grants_client(monkeypatch: pytest.MonkeyPatch, fake: _GrantsCopyDB) -> TestClient:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def test_mcp_grants_copy_from_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace-all the target's grants with the source's rows in ONE
    transaction — the operator repair tool for any key (e.g. Fran's) without
    clicking the whole catalog back in. Response carries both ids and the
    copied count."""
    target, source = uuid.uuid4(), uuid.uuid4()
    fake = _GrantsCopyDB(existing={target, source})
    tc = _grants_client(monkeypatch, fake)
    r = tc.post(
        f"/admin/keys/{target}/mcp-grants/copy-from",
        json={"source_key_id": str(source)},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key_id": str(target), "source_key_id": str(source), "grants": 3}
    # copy direction: INSERT INTO the TARGET, SELECT from the SOURCE rows
    assert fake.copy_args == (target, source)
    # the copy ran inside a transaction (acquire() + conn.execute, not a
    # bare top-level execute)
    assert any("insert select copy" in c for c in fake.calls)


def test_mcp_grants_copy_from_replaces_existing_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target that ALREADY holds grants gets the delete-then-refill swap —
    no merge, no duplicate (server, tool) rows (the PK (key_id, server,
    tool) would reject them, and the incident report demands replace
    semantics)."""
    target, source = uuid.uuid4(), uuid.uuid4()
    fake = _GrantsCopyDB(existing={target, source})
    tc = _grants_client(monkeypatch, fake)
    r = tc.post(
        f"/admin/keys/{target}/mcp-grants/copy-from",
        json={"source_key_id": str(source)},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert fake.calls == ["delete target grants", "insert select copy"]
    assert fake.copy_args == (target, source)


def test_mcp_grants_copy_from_unknown_source_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: an unknown SOURCE key 404s — and nothing is written (the
    target keeps its current grants rather than being emptied)."""
    target, source = uuid.uuid4(), uuid.uuid4()
    fake = _GrantsCopyDB(existing={target})  # source NOT in the table
    tc = _grants_client(monkeypatch, fake)
    r = tc.post(
        f"/admin/keys/{target}/mcp-grants/copy-from",
        json={"source_key_id": str(source)},
        headers=_h(),
    )
    assert r.status_code == 404
    assert "source key" in r.json()["detail"]
    assert fake.calls == []  # refused before any write


def test_mcp_grants_copy_from_unknown_target_404(monkeypatch: pytest.MonkeyPatch) -> None:
    target, source = uuid.uuid4(), uuid.uuid4()
    fake = _GrantsCopyDB(existing={source})  # target NOT in the table
    tc = _grants_client(monkeypatch, fake)
    r = tc.post(
        f"/admin/keys/{target}/mcp-grants/copy-from",
        json={"source_key_id": str(source)},
        headers=_h(),
    )
    assert r.status_code == 404
    assert fake.calls == []  # refused before any write


def test_mcp_grants_copy_from_self_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """source == target must NOT run the delete-then-refill (the DELETE would
    empty the key before the SELECT could read it — same key). It is a
    no-op that echoes the current grant count."""
    kid = uuid.uuid4()
    fake = _GrantsCopyDB(existing={kid})
    tc = _grants_client(monkeypatch, fake)
    r = tc.post(
        f"/admin/keys/{kid}/mcp-grants/copy-from",
        json={"source_key_id": str(kid)},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key_id": str(kid), "source_key_id": str(kid), "grants": 2}
    assert fake.calls == []  # no DELETE, no INSERT..SELECT ran


def test_mcp_grants_copy_from_requires_admin_key(monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _grants_client(monkeypatch, _GrantsCopyDB())
    kid = uuid.uuid4()
    assert (
        tc.post(f"/admin/keys/{kid}/mcp-grants/copy-from", json={"source_key_id": str(uuid.uuid4())}).status_code == 401
    )
    assert (
        tc.post(
            f"/admin/keys/{kid}/mcp-grants/copy-from",
            json={"source_key_id": str(uuid.uuid4())},
            headers={"Authorization": "Bearer nope"},
        ).status_code
        == 401
    )


def test_help_lists_mcp_grants_copy_from(client) -> None:
    """The route inventory must include the new endpoint (the console and
    runbooks discover the surface through /admin/help)."""
    tc, _ = client
    endpoints = tc.get("/admin/help", headers=_h()).json()["endpoints"]
    assert "POST   /admin/keys/{key_id}/mcp-grants/copy-from" in endpoints


class _ProfileRoutingDB:
    """Captures the profile upsert (SQL + args) and echoes a RETURNING row,
    so tests can assert the exact columns the API writes — the same columns
    core.model_routing.profile_routing_config reads on the proxy path."""

    def __init__(self) -> None:
        # () = nothing upserted yet (tests assert refusal-before-write via == ())
        self.last_upsert: tuple[str, tuple] | tuple[()] = ()
        self.last_select: str = ""

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
        self.last_select = sql
        return []

    async def fetchval(self, sql: str, *a: Any):
        return 1

    async def execute(self, sql: str, *a: Any):
        return "INSERT 0 1"


def _routing_client(monkeypatch: pytest.MonkeyPatch, fake: _ProfileRoutingDB) -> TestClient:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def test_profile_routing_columns_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /admin/profiles persists + returns the routing rollout pair, and
    the upsert covers BOTH the INSERT and the ON CONFLICT arm (a shadow-mode
    save that silently kept the old policy on update would arm nothing)."""
    fake = _ProfileRoutingDB()
    tc = _routing_client(monkeypatch, fake)
    r = tc.post(
        "/admin/profiles",
        json={
            "name": "team-alpha",
            "model_order": ["llm-2", "llm-8"],
            "routing_policy": "shadow",
            "routing_min_model": "llm-2",
        },
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["routing_policy"] == "shadow"
    assert j["routing_min_model"] == "llm-2"
    upsert = fake.last_upsert
    assert upsert != (), "the routing POST must have written the row"
    assert len(upsert) == 2
    sql, args = upsert
    assert "routing_policy" in sql and "routing_min_model" in sql
    # both arms of the upsert write the pair (INSERT column list + UPDATE set)
    assert "routing_policy, routing_min_model)" in sql
    assert "routing_policy = $5, routing_min_model = $6" in sql
    assert "RETURNING id, name, model_order, defaults_json, offpeak_fraction, routing_policy, routing_min_model" in sql
    assert args[4] == "shadow" and args[5] == "llm-2"


def test_profile_routing_defaults_off_and_no_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Additive fields with defaults: a body that omits them lands exactly as
    the schema defaults ('off' / no floor) — legacy console callers are
    byte-for-byte unchanged in behavior."""
    fake = _ProfileRoutingDB()
    tc = _routing_client(monkeypatch, fake)
    r = tc.post("/admin/profiles", json={"name": "legacy", "model_order": ["m1"]}, headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["routing_policy"] == "off"
    assert r.json()["routing_min_model"] == ""
    upsert = fake.last_upsert
    assert upsert != (), "the legacy POST must have written the row"
    assert len(upsert) == 2
    _, args = upsert
    assert args[4] == "off" and args[5] == ""


def test_profile_memory_preset_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enrollment-memory preset (review §2) persists in defaults_json
    under "memory" and round-trips through GET. Null (omitted) means the
    profile declares NOTHING — the redeemer's answer, else the deployment
    default, applies (never silently provisioned)."""
    fake = _ProfileRoutingDB()
    tc = _routing_client(monkeypatch, fake)
    r = tc.post(
        "/admin/profiles",
        json={
            "name": "memory-cohort",
            "model_order": ["m1"],
            "memory": {"provision": True},
        },
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    defaults = json.loads(r.json()["defaults_json"])
    assert defaults["memory"] == {"provision": True}

    # omitted = not declared (no "memory" key at all)
    r2 = tc.post("/admin/profiles", json={"name": "plain", "model_order": ["m1"]}, headers=_h())
    assert r2.status_code == 201
    assert "memory" not in json.loads(r2.json()["defaults_json"])

    # GET round-trips the preset (a GET that drops it would reset on save)
    class _EchoDB(_ProfileRoutingDB):
        async def fetch(self, sql: str, *a: Any):
            self.last_select = sql
            return [
                {
                    "id": uuid.uuid4(),
                    "name": "memory-cohort",
                    "model_order": ["m1"],
                    "defaults_json": json.dumps({"memory": {"provision": True}}),
                    "offpeak_fraction": None,
                    "routing_policy": "off",
                    "routing_min_model": "",
                }
            ]

    fake2 = _EchoDB()
    tc2 = _routing_client(monkeypatch, fake2)
    rows = tc2.get("/admin/profiles", headers=_h()).json()
    assert rows[0]["memory"] == {"provision": True}


def test_profile_routing_min_model_off_ladder_refused_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a floor naming a model OUTSIDE the ladder is silently
    unusable downstream (core.model_routing pins the floor to the ceiling =
    no downgrade, warning per user) — refuse it before anything is written."""
    fake = _ProfileRoutingDB()
    tc = _routing_client(monkeypatch, fake)
    r = tc.post(
        "/admin/profiles",
        json={
            "name": "bad-floor",
            "model_order": ["llm-2", "llm-8"],
            "routing_policy": "active",
            "routing_min_model": "llm-70b",
        },
        headers=_h(),
    )
    assert r.status_code == 400
    assert "routing_min_model" in r.json()["detail"]
    assert "llm-70b" in r.json()["detail"]
    assert fake.last_upsert == ()  # refused BEFORE any DB write


def test_profile_routing_unknown_policy_rejected_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """routing_policy is a closed Literal ('off'|'shadow'|'active') — a
    hand-rolled value must never reach the DB (the proxy fail-safes unknown
    values to 'off' anyway, but the admin API is the only writer and stays
    strict)."""
    fake = _ProfileRoutingDB()
    tc = _routing_client(monkeypatch, fake)
    r = tc.post(
        "/admin/profiles",
        json={
            "name": "bad-policy",
            "model_order": ["m1"],
            "routing_policy": "banana",
        },
        headers=_h(),
    )
    assert r.status_code == 422
    assert fake.last_upsert == ()  # nothing written


def test_list_profiles_select_includes_routing_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /admin/profiles must SELECT the routing pair so the console
    round-trips everything POST accepts (a GET that drops them would reset
    the policy on every edit-and-save)."""
    fake = _ProfileRoutingDB()
    tc = _routing_client(monkeypatch, fake)
    r = tc.get("/admin/profiles", headers=_h())
    assert r.status_code == 200
    assert "routing_policy" in fake.last_select and "routing_min_model" in fake.last_select


# ---------------------------------------------------------------------------
# F1/F5 (SSO self-mint wave-1 review): the console's Self-mint column
# renders from GET /admin/keys rows — core.keys.list_keys must PROJECT
# users.self_mint_enabled, or selfMintCell's boolean gate sees undefined
# and the toggle is unreachable from the UI (the string-grep console tests
# cannot see this; the reviewer asked for a REAL-payload test).
# ---------------------------------------------------------------------------


class _KeysRowDB(FakeDB):
    """FakeDB whose fetch() answers core.keys.list_keys' SELECT with one
    projected row (the join the real query performs), so the route returns
    a payload shaped like production."""

    def __init__(self, user_id: uuid.UUID) -> None:
        super().__init__()
        self._user_id = user_id
        self.last_keys_select = ""

    async def fetch(self, sql: str, *args: Any):
        if "FROM api_keys k" in sql:
            self.last_keys_select = sql
            return [
                {
                    "id": uuid.uuid4(),
                    "user_id": self._user_id,
                    "user_name": "alice",
                    "name": "k",
                    "key_prefix": "hllm-AA",
                    "scopes": ["proxy"],
                    "allowed_models": [],
                    "rate_limit_rpm": 60,
                    "rate_limit_tpm": 100000,
                    "expires_at": None,
                    "revoked_at": None,
                    "created_at": "now",
                    "relay_default": False,
                    "stream_pcai_events": True,
                    "bound_via": None,
                    "bound_at": None,
                    "subject": None,
                    "subject_idp": None,
                    "self_mint_enabled": True,
                }
            ]
        return []


def test_list_keys_projects_self_mint_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Self-mint column's data source: GET /admin/keys rows carry
    self_mint_enabled as a BOOLEAN, projected from the users join. Also
    pins the SQL shape — the column must come from the users side (u.*),
    not the api_keys side, so a per-user flag rides the existing JOIN."""
    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.keys as keys_mod

    uid = uuid.uuid4()
    fake = _KeysRowDB(uid)
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/keys", headers=_h())
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    row = rows[0]
    # THE finding: the console's `typeof k.self_mint_enabled === 'boolean'`
    # gate must see a real boolean, not undefined (else: muted dash forever).
    assert isinstance(row["self_mint_enabled"], bool) and row["self_mint_enabled"] is True
    # and the projection reads the USER column (per-user flag, users JOIN)
    assert "u.self_mint_enabled" in fake.last_keys_select


# ---------------------------------------------------------------------------
# Profile-on-keys (2026-10-17): profiles are USER-scoped — api_keys carries
# NO profile column, and core.cascade.profile_order_for_key resolves through
# api_keys -> users -> profiles. Before this, the console had NO control to
# reassign an existing user's profile (Keys > Profile was create-time only;
# the tenant SSO default only affects new mints), so a user could sit on a
# stale ordering indefinitely with no UI path to fix it.
# ---------------------------------------------------------------------------


def test_list_keys_projects_user_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /admin/keys rows carry the USER's profile id+name so the console
    can render (and reassign) the ordering each key actually cascades
    through. Pins the SQL shape: the columns come from the users JOIN and a
    LEFT JOIN on profiles — NOT from api_keys, which has no such column."""
    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.keys as keys_mod

    uid = uuid.uuid4()
    fake = _KeysRowDB(uid)
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/keys", headers=_h())
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    # The projection is user-scoped: the id rides the users JOIN ...
    assert "u.profile_id AS user_profile_id" in fake.last_keys_select
    # ... and the NAME comes from a LEFT JOIN on profiles (a user may carry
    # no profile at all — NULL must survive, not inner-join the row away).
    assert "LEFT JOIN profiles p ON p.id = u.profile_id" in fake.last_keys_select
    assert "p.name AS user_profile_name" in fake.last_keys_select


def test_admin_console_has_profile_reassign_control() -> None:
    """The console must expose a real reassign control, not just a display
    column: the reported gap was that an existing user's profile could not
    be changed from the UI at all. String-level (this suite is the UI's own
    contract test), pinning the handler, the PUT target, and the null-clears path."""
    html = (Path(__file__).resolve().parents[1] / "src" / "pcai_llm" / "ui" / "admin.html").read_text(encoding="utf-8")
    # the cell renders from the projected field and is clickable
    assert "const profileCell = k =>" in html
    assert "if (!('user_profile_id' in k))" in html, "must gate on backend support"
    assert 'onclick="reassignProfile(this,' in html
    # the action exists and PUTs the documented route
    assert "window.reassignProfile = (btn, userId)" in html
    assert "'/users/' + userId + '/profile'" in html
    assert "method: 'PUT'" in html
    # clearing a profile is a real state (no ordering => alias requests 404),
    # so the picker offers it and sends an explicit null
    assert '<option value="">' in html
    assert "|| null" in html
    # the keys table gained exactly one column (10 -> 11) and the empty-row
    # colspan follows, or the no-keys state renders a ragged table
    assert ">Profile</th>" in html
    assert 'colspan="11"' in html
