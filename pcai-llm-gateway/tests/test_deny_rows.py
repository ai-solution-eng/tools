"""D-F1 deny rows — the admin write path for allowed=FALSE mcp_tool_acls.

The deny-row SCHEMA and read path have existed since the ACL table was
born (federation.load_grants folds them in, rotate/copy-from carry them),
but until this wave NO admin write produced one: the PUT was DELETE-all +
INSERT-TRUE-only, so under the DEFAULT tool_relay_grant_mode="scope" the
relay merge re-added the whole policy-admitted catalog after the grant
filter and unticking a collection in the console was a silent no-op —
and every PUT edit erased any documented deny that existed.

Covered here: PUT writes FALSE rows for `denies` (same transaction as the
TRUE grants) and no longer wipes pre-existing denies on a grants-only
edit; GET returns both row kinds flagged with `allowed`; the per-server
PATCH (off/on/catalog); and federation.load_denies (the scope-merge
subtract seam routes_proxy calls).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.mcpf import federation

# ---------------------------------------------------------------------------
# fakes — the same sql-sniffing FakeDB style tests/test_admin.py established
# ---------------------------------------------------------------------------


class FakeConn:
    """Acquired-connection stand-in: records every statement IN ORDER so
    tests can assert the transaction shape (DELETE-then-INSERTs).

    INSERTs are applied against the fake's PK-keyed row map with Postgres'
    own conflict semantics: a bare INSERT on an existing (key_id, server,
    tool) RAISES, `ON CONFLICT (…) DO UPDATE SET allowed = X` upserts,
    `ON CONFLICT DO NOTHING` skips. ``conn.fetch`` is provided too (the
    PATCH mode="catalog" pre-DELETE capture reads inside the transaction)."""

    def __init__(self, db: DenyDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)

    async def executemany(self, sql: str, args: Any):
        self._db.executed.append((sql, list(args)))
        # apply INSERT rows so row-state assertions see what the endpoint
        # wrote (args tuples are (key_id, server, tool); allowed comes from
        # the VALUES literal in the statement — TRUE grants / FALSE denies)
        if "INSERT INTO mcp_tool_acls" not in sql:
            return
        allowed = "TRUE)" in sql
        for a in args:
            pk = (a[0], a[1], a[2])
            exists = pk in self._db.rows
            if not exists:
                self._db.rows[pk] = allowed
            elif "DO UPDATE SET allowed = TRUE" in sql:
                self._db.rows[pk] = True
            elif "DO UPDATE SET allowed = FALSE" in sql:
                self._db.rows[pk] = False
            elif "DO NOTHING" in sql:
                continue
            else:
                # no ON CONFLICT clause: real Postgres raises
                # UniqueViolationError on mcp_tool_acls_pkey
                raise FakeUniqueViolation(
                    'duplicate key value violates unique constraint "mcp_tool_acls_pkey" '
                    f"DETAIL: Key (key_id, server, tool)=({pk[0]}, {pk[1]}, {pk[2]}) already exists."
                )


class FakeUniqueViolation(Exception):
    """Stands in for ``asyncpg.UniqueViolationError``: the fake raises it on
    a bare INSERT that collides with the (key_id, server, tool) PK, exactly
    as Postgres does — that is the failure mode the two 500s in the review
    were, and the old 4-tuple-set fake could not see either of them."""


class DenyDB:
    """Minimal mcp_tool_acls in-memory fake. The SQL dispatch is sniffed on
    the same fragments the real statements carry (AND allowed / NOT allowed
    / INSERT INTO mcp_tool_acls) — exactly how tests/test_admin_ui.py's
    fakes tell statements apart.

    Rows are keyed by the REAL primary key ``(key_id, server, tool)`` with
    ``allowed`` as the VALUE. The previous shape (a set of
    ``(key_id, server, tool, allowed)`` 4-tuples) let ``(k,s,t,True)`` and
    ``(k,s,t,False)`` coexist — something the Postgres PK forbids — which is
    precisely why the PUT re-grant 500 and the PATCH-catalog 500 passed the
    suite. A bare INSERT that collides now RAISES, and ``ON CONFLICT``
    clauses are emulated by their effect (DO UPDATE SET allowed = TRUE /
    FALSE, DO NOTHING), so a missing ON CONFLICT fails here the way it fails
    on real Postgres.
    """

    def __init__(self, existing: set[uuid.UUID] | None = None) -> None:
        self.existing = existing if existing is not None else set()
        self.rows: dict[tuple[uuid.UUID, str, str], bool] = {}
        self.executed: list[tuple] = []
        self.catalog_tools: list[dict] = []  # live-catalog answer for PATCH off
        self.profile_live: tuple[set, set] | None = None  # load_live_profile_grants answer

    def acquire(self):
        return FakeConn(self)

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM profiles" in sql:
            return None
        if "FROM users" in sql:
            return {"id": args[0]} if args[0] != uuid.UUID(int=0) else None
        return {"id": uuid.uuid4()}

    async def fetch(self, sql: str, *args: Any):
        key = args[0]
        if "mcp_tool_acls" in sql:
            rows = [(pk, allowed) for pk, allowed in self.rows.items() if pk[0] == key]
            if "AND allowed" in sql:
                rows = [r for r in rows if r[1]]
            if "AND NOT allowed" in sql:
                rows = [r for r in rows if not r[1]]
            if "server = $2" in sql and len(args) > 1:
                rows = [r for r in rows if r[0][1] == args[1]]
            return [
                {"server": pk[1], "tool": pk[2], "allowed": allowed}
                for pk, allowed in sorted(rows, key=lambda r: (r[0][1], r[0][2]))
            ]
        return []

    async def fetchval(self, sql: str, *args: Any):
        if "FROM api_keys" in sql:  # _exists
            return 1 if args[0] in self.existing else None
        return 1

    async def execute(self, sql: str, *args: Any):
        key = args[0]
        if "DELETE FROM mcp_tool_acls" in sql:
            if "AND allowed" in sql:
                doomed = [pk for pk, allowed in self.rows.items() if pk[0] == key and allowed]
            elif "AND NOT allowed" in sql and len(args) > 1 and "server = $2" in sql:
                doomed = [pk for pk, allowed in self.rows.items() if pk[0] == key and not allowed and pk[1] == args[1]]
            else:  # unscoped delete (legacy shape) — must NOT appear anymore
                doomed = [pk for pk in self.rows if pk[0] == key]
                self.legacy_delete = True
            for pk in doomed:
                del self.rows[pk]
            self.executed.append((sql, args))
            return "DELETE 0"
        return "OK"

    def seed(self, key: uuid.UUID, pairs: list[tuple[str, str, bool]]) -> None:
        for server, tool, allowed in pairs:
            pk = (key, server, tool)
            # the PK is real here: seeding both polarities of one pair is the
            # contradiction the schema forbids, not a valid fixture.
            assert pk not in self.rows, f"fixture would violate the mcp_tool_acls PK: {pk}"
            self.rows[pk] = allowed

    def allowed_of(self, key: uuid.UUID, server: str, tool: str) -> bool | None:
        """The pair's single row polarity: True/False, or None = no row (the
        PK means "no row", "grant" and "deny" are the only three states)."""
        return self.rows.get((key, server, tool))

    def pairs_of(self, key: uuid.UUID) -> dict[tuple[str, str], bool]:
        return {(srv, tool): allowed for (k, srv, tool), allowed in self.rows.items() if k == key}


def _client_of(monkeypatch: pytest.MonkeyPatch, fake: DenyDB) -> TestClient:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-123"}


# ---------------------------------------------------------------------------
# (a) PUT writes FALSE rows for denies and TRUE for grants, one transaction
# ---------------------------------------------------------------------------


def test_put_writes_true_and_false_rows_in_one_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    tc = _client_of(monkeypatch, fake)
    r = tc.put(
        f"/admin/keys/{kid}/mcp-grants",
        json={
            "grants": [
                {"server": "logsearch", "tool": "search_logs"},
                {"server": "logsearch", "tool": "get_pod_logs"},
            ],
            "denies": [{"server": "logsearch", "tool": "delete_index"}],
        },
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key_id": str(kid), "grants": 2, "denies": 1}
    # order: DELETE(TRUE rows only) -> TRUE inserts -> FALSE inserts
    assert fake.executed and "AND allowed" in fake.executed[0][0]
    true_inserts = [e for e in fake.executed[1:] if "VALUES ($1,$2,$3,TRUE)" in e[0]]
    false_inserts = [e for e in fake.executed[1:] if "FALSE" in e[0]]
    assert len(true_inserts) == 1 and len(true_inserts[0][1]) == 2
    assert len(false_inserts) == 1
    assert fake.pairs_of(kid) == {
        ("logsearch", "search_logs"): True,
        ("logsearch", "get_pod_logs"): True,
        ("logsearch", "delete_index"): False,
    }
    # replace DELETE must NEVER be unscoped again (that erased deny rows)
    assert not getattr(fake, "legacy_delete", False)


def test_put_deny_and_grant_conflict_resolves_to_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pair listed in both lists resolves to the TRUE grant — the deny is
    dropped from the write set instead of storing contradictory rows."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    tc = _client_of(monkeypatch, fake)
    r = tc.put(
        f"/admin/keys/{kid}/mcp-grants",
        json={
            "grants": [{"server": "s", "tool": "t"}],
            "denies": [{"server": "s", "tool": "t"}, {"server": "s", "tool": "u"}],
        },
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["denies"] == 1  # only the non-contradictory deny written
    assert fake.allowed_of(kid, "s", "t") is True
    assert fake.allowed_of(kid, "s", "t") is not False
    assert fake.allowed_of(kid, "s", "u") is False


def test_put_unknown_key_404(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = DenyDB(existing=set())
    tc = _client_of(monkeypatch, fake)
    r = tc.put(
        f"/admin/keys/{uuid.uuid4()}/mcp-grants",
        json={"grants": [], "denies": []},
        headers=_h(),
    )
    assert r.status_code == 404
    assert fake.executed == []


# ---------------------------------------------------------------------------
# (g) destructive-edit regression: grants-only PUT no longer wipes denies
# ---------------------------------------------------------------------------


def test_put_grants_only_preserves_preexisting_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE regression: before this fix the PUT's DELETE-all erased existing
    allowed=FALSE rows on every edit (resurrecting deliberately denied
    pairs under the scope merge). Denies must persist unless the body's
    `denies` explicitly re-states them."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("logsearch", "delete_index", False), ("old", "tool", True)])
    tc = _client_of(monkeypatch, fake)
    r = tc.put(
        f"/admin/keys/{kid}/mcp-grants",
        json={"grants": [{"server": "logsearch", "tool": "search_logs"}]},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    # old TRUE rows replaced, the deny row untouched
    assert fake.allowed_of(kid, "old", "tool") is not True
    assert fake.allowed_of(kid, "logsearch", "search_logs") is True
    assert fake.allowed_of(kid, "logsearch", "delete_index") is False


def test_put_explicit_denies_replaces_held_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body that DOES send denies still controls the deny set: the held
    deny survives when re-stated..."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("s", "a", False)])
    tc = _client_of(monkeypatch, fake)
    r = tc.put(
        f"/admin/keys/{kid}/mcp-grants",
        json={"grants": [], "denies": [{"server": "s", "tool": "a"}]},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert fake.allowed_of(kid, "s", "a") is False


# ---------------------------------------------------------------------------
# (a2) BLOCKER 1: re-granting a denied pair is the PRIMARY re-enable workflow
# ---------------------------------------------------------------------------


def test_put_regrant_of_denied_pair_flips_the_row_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for review §1.1 (machine-verified 500 on real Postgres).

    How the operator gets here: `PATCH mode="off"` (the console's ✕ disable
    all) writes its deny set with `… DO UPDATE SET allowed = FALSE`, which
    CONVERTS every held TRUE row into a FALSE row. The console's only
    per-tool re-enable is ticking that struck-through pair and pressing Save
    → `PUT grants=[pair]` while the FALSE row still exists. The old TRUE
    insert had no ON CONFLICT clause, so it died on `mcp_tool_acls_pkey`
    (`duplicate key value violates unique constraint`), the transaction
    rolled back, and the deny stayed in force.

    Now the TRUE insert upserts (`ON CONFLICT (key_id, server, tool) DO
    UPDATE SET allowed = TRUE`): the pair is granted again."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("srv", "toolX", False)])  # the deny PATCH mode="off" left
    tc = _client_of(monkeypatch, fake)
    r = tc.put(
        f"/admin/keys/{kid}/mcp-grants",
        json={"grants": [{"server": "srv", "tool": "toolX"}]},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key_id": str(kid), "grants": 1, "denies": 0}
    assert fake.allowed_of(kid, "srv", "toolX") is True
    # the upsert is what makes it work — a bare INSERT would have raised
    true_inserts = [e for e in fake.executed if "VALUES ($1,$2,$3,TRUE)" in e[0]]
    assert true_inserts and "ON CONFLICT (key_id, server, tool) DO UPDATE SET allowed = TRUE" in true_inserts[0][0]


def test_put_regrant_of_denied_pair_also_listed_in_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same body the console can send when a pair is re-ticked while the
    deny set still mentions it: the deny is filtered out of the write set
    FIRST, so it is the TRUE insert that must survive the existing FALSE row
    (review §1.1, second harness case)."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("srv", "toolX", False)])
    tc = _client_of(monkeypatch, fake)
    r = tc.put(
        f"/admin/keys/{kid}/mcp-grants",
        json={
            "grants": [{"server": "srv", "tool": "toolX"}],
            "denies": [{"server": "srv", "tool": "toolX"}],
        },
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["denies"] == 0
    assert fake.allowed_of(kid, "srv", "toolX") is True


def test_put_grants_and_denies_overlap_grant_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) A pair in BOTH lists resolves to the grant — and the overlap must
    not leave a contradictory (impossible) second row behind: the fake keys
    rows by the real PK, so a surviving FALSE row would fail these asserts."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("srv", "both", False), ("srv", "denied_only", False)])
    tc = _client_of(monkeypatch, fake)
    r = tc.put(
        f"/admin/keys/{kid}/mcp-grants",
        json={
            "grants": [{"server": "srv", "tool": "both"}, {"server": "srv", "tool": "plain"}],
            "denies": [
                {"server": "srv", "tool": "both"},
                {"server": "srv", "tool": "denied_only"},
                {"server": "srv", "tool": "new_deny"},
            ],
        },
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key_id": str(kid), "grants": 2, "denies": 2}
    assert fake.pairs_of(kid) == {
        ("srv", "both"): True,
        ("srv", "plain"): True,
        ("srv", "denied_only"): False,
        ("srv", "new_deny"): False,
    }


# ---------------------------------------------------------------------------
# (b) GET returns both row kinds with allowed flags
# ---------------------------------------------------------------------------


def test_get_returns_grants_and_denies_with_allowed_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("a", "t1", True), ("a", "t2", False)])
    tc = _client_of(monkeypatch, fake)
    r = tc.get(f"/admin/keys/{kid}/mcp-grants", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"server": "a", "tool": "t1", "allowed": True} in body
    assert {"server": "a", "tool": "t2", "allowed": False} in body
    assert len(body) == 2


def test_get_unknown_key_404(monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _client_of(monkeypatch, DenyDB(existing=set()))
    assert tc.get(f"/admin/keys/{uuid.uuid4()}/mcp-grants", headers=_h()).status_code == 404


def test_get_unions_profile_live_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console's deny math needs the pairs the key can call WITHOUT an
    ACL row of its own (review §3.1): a profile live-grant pair is callable,
    so the GET returns it as an allowed row with source="profile" — without
    it the tree renders the pair unticked and unticking could never write a
    deny. Non-profile rows keep their exact previous shape (no `source`)."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("applygate", "own_grant", True)])
    monkeypatch.setattr(
        routes_admin,
        "load_live_profile_grants",
        _live_grants_of({("applygate", "profile_pair"), ("applygate", "own_grant")}),
    )
    tc = _client_of(monkeypatch, fake)
    r = tc.get(f"/admin/keys/{kid}/mcp-grants", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json() == [
        {"server": "applygate", "tool": "own_grant", "allowed": True},
        {"server": "applygate", "tool": "profile_pair", "allowed": True, "source": "profile"},
    ]


def test_get_profile_union_keeps_a_key_level_deny_in_force(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key-level FALSE row is the pair's single row and must NOT be
    duplicated by a profile row — load_grants subtracts it, so the GET must
    agree with the enforcement path (a pair appears once, denied)."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("applygate", "denied_pair", False)])
    monkeypatch.setattr(
        routes_admin,
        "load_live_profile_grants",
        _live_grants_of({("applygate", "denied_pair")}),
    )
    tc = _client_of(monkeypatch, fake)
    body = tc.get(f"/admin/keys/{kid}/mcp-grants", headers=_h()).json()
    assert body == [{"server": "applygate", "tool": "denied_pair", "allowed": False}]


def test_get_degrades_to_key_rows_when_profile_lookup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same fail-open posture as federation.load_grants: the console must
    still render the key's own rows when the profile layer faults."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("a", "t1", True), ("a", "t2", False)])

    async def boom(_kid):
        raise RuntimeError("profile layer down")

    monkeypatch.setattr(routes_admin, "load_live_profile_grants", boom)
    tc = _client_of(monkeypatch, fake)
    r = tc.get(f"/admin/keys/{kid}/mcp-grants", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json() == [
        {"server": "a", "tool": "t1", "allowed": True},
        {"server": "a", "tool": "t2", "allowed": False},
    ]


# ---------------------------------------------------------------------------
# (c) PATCH mode="off" — deny the union (held + live catalog), bounded at 500
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_off(monkeypatch: pytest.MonkeyPatch):
    """Registry snapshot with one server; load_live_profile_grants stubbed
    (None = stamps-only profile — the common path)."""

    class _Reg:
        async def mcp_servers(self):
            return [
                {
                    "name": "applygate",
                    "url": "https://applygate",
                    "headers": {},
                    "auth_secret": None,
                    "auth_token_enc": None,
                }
            ]

    monkeypatch.setattr(routes_admin, "registry", _Reg())
    monkeypatch.setattr(routes_admin, "load_live_profile_grants", _async_none)


async def _async_none(_key_id):
    return None


def _live_grants_of(pairs: set) -> Any:
    """Factory: a load_live_profile_grants stand-in resolving to `pairs` as
    the profile layer's grant set (empty deny set)."""

    async def _f(_key_id):
        return (pairs, set())

    return _f


def _patch_tools(monkeypatch: pytest.MonkeyPatch, tools: list[str]) -> None:
    async def fake_fetch_tools(_url, _headers, timeout_s=None):
        return [{"tool": t, "description": ""} for t in tools]

    import pcai_llm.api.routes_mcp as rm

    monkeypatch.setattr(rm, "_fetch_tools", fake_fetch_tools)


def test_patch_off_writes_union_of_held_and_catalog(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("applygate", "held_tool", True), ("other", "x", True)])
    _patch_tools(monkeypatch, ["apply_manifest", "plan_apply"])
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "off"}, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "off" and body["pairs_written"] == 3
    # UNION = held(applygate:held_tool) + catalog(apply_manifest, plan_apply);
    # the OTHER server's grant is untouched, no FALSE rows appear on it.
    assert fake.allowed_of(kid, "applygate", "held_tool") is False
    assert fake.allowed_of(kid, "applygate", "apply_manifest") is False
    assert fake.allowed_of(kid, "applygate", "plan_apply") is False
    assert fake.allowed_of(kid, "other", "x") is True
    assert not any(srv == "other" and not allowed for (srv, _t), allowed in fake.pairs_of(kid).items())


def test_patch_off_bounded_at_500(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    _patch_tools(monkeypatch, [f"tool_{i}" for i in range(501)])
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "off"}, headers=_h())
    assert r.status_code == 400
    assert "cap is 500" in r.json()["detail"]
    assert fake.rows == {}  # nothing written past the cap


def test_patch_off_catalog_failure_degrades_to_held(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    """Upstream list_tools down: degrade to the HELD pairs (a partial
    disable beats a 502) — the deny write never depends on the catalog.
    (e) The degradation is REPORTED, not silent (review §1.4): the response
    carries catalog_ok=false and so does the audit row, so a partial
    disable cannot read as a complete one."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("applygate", "held_tool", True)])

    async def boom(*_a, **_k):
        raise RuntimeError("upstream down")

    audited: list[dict] = []

    async def fake_audit(request, **kwargs):
        audited.append(kwargs)

    import pcai_llm.api.routes_mcp as rm

    monkeypatch.setattr(rm, "_fetch_tools", boom)
    monkeypatch.setattr(routes_admin, "_audit", fake_audit)
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "off"}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["pairs_written"] == 1
    assert r.json()["catalog_ok"] is False
    assert audited and audited[0]["details"]["catalog_ok"] is False
    assert audited[0]["details"]["pairs_written"] == 1  # stays truthful
    assert fake.allowed_of(kid, "applygate", "held_tool") is False


def test_patch_off_catalog_ok_true_when_the_fetch_succeeds(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    """The happy path says so too — catalog_ok is present on every "off"
    response, so a client can branch on it without treating absence as a
    failure."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    _patch_tools(monkeypatch, ["t1"])
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "off"}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["catalog_ok"] is True


def test_patch_off_profile_live_grants_count_as_held(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    """Under live grants the profile's preset pairs reach the key at call
    time — 'off' must deny them too, not just per-key TRUE rows."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    monkeypatch.setattr(
        routes_admin,
        "load_live_profile_grants",
        _live_grants_of({("applygate", "profile_tool")}),
    )
    _patch_tools(monkeypatch, [])
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "off"}, headers=_h())
    assert r.status_code == 200, r.text
    assert fake.allowed_of(kid, "applygate", "profile_tool") is False


def test_patch_off_unknown_server_404(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/nope", json={"mode": "off"}, headers=_h())
    assert r.status_code == 404
    assert fake.executed == []


# ---------------------------------------------------------------------------
# (d) PATCH mode="on" — deletes FALSE rows only
# ---------------------------------------------------------------------------


def test_patch_on_deletes_only_false_rows(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(
        kid,
        [
            ("applygate", "denied_one", False),
            ("applygate", "denied_two", False),
            ("applygate", "granted", True),
            ("other", "denied_elsewhere", False),
        ],
    )
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "on"}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["pairs_written"] == 0
    assert fake.allowed_of(kid, "applygate", "denied_one") is not False
    assert fake.allowed_of(kid, "applygate", "denied_two") is not False
    # grants untouched, other servers' denies untouched
    assert fake.allowed_of(kid, "applygate", "granted") is True
    assert fake.allowed_of(kid, "other", "denied_elsewhere") is False


# ---------------------------------------------------------------------------
# (e) PATCH mode="catalog" — restore the server's deny rows as they stood
# ---------------------------------------------------------------------------


def test_patch_catalog_leaves_held_true_rows_untouched(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    """BLOCKER 2 regression (review §1.2, option b): a key that HOLDS a TRUE
    row on the server used to make this mode a guaranteed 500 — the old code
    re-inserted FALSE for the HELD pairs, whose TRUE rows the DELETE had left
    alone, and the (key_id, server, tool) PK rejected it. On real Postgres
    that was `UniqueViolationError … (…, srv, held) already exists` and the
    whole transaction rolled back.

    'catalog' now restores the deny state the server had before the call:
    the pre-op FALSE rows are captured in the transaction, deleted, and
    re-inserted. Grants are not touched — no TRUE row becomes a deny."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(
        kid,
        [
            ("applygate", "held_granted", True),
            ("applygate", "stray_deny", False),
            ("other", "elsewhere", False),
        ],
    )
    monkeypatch.setattr(
        routes_admin,
        "load_live_profile_grants",
        _live_grants_of({("applygate", "profile_pair")}),
    )
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "catalog"}, headers=_h())
    assert r.status_code == 200, r.text
    # exactly the pre-op deny set was re-written
    assert r.json()["pairs_written"] == 1
    assert fake.allowed_of(kid, "applygate", "stray_deny") is False
    # the held TRUE row keeps its grant — and is NOT given a deny row
    assert fake.allowed_of(kid, "applygate", "held_granted") is True
    # a profile-granted pair is not invented into a deny either
    assert fake.allowed_of(kid, "applygate", "profile_pair") is None
    # other servers untouched
    assert fake.allowed_of(kid, "other", "elsewhere") is False


def test_patch_catalog_restores_exactly_the_pre_op_deny_set(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    """'catalog' is capture → delete → re-insert of THIS server's FALSE rows:
    a deny the operator documented (including one for a pair the key does not
    hold at all) survives verbatim, and nothing new is invented."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(
        kid,
        [
            ("applygate", "held_tool", True),
            ("applygate", "documented_deny", False),
            ("applygate", "another_deny", False),
        ],
    )
    tc = _client_of(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "catalog"}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["pairs_written"] == 2
    assert fake.pairs_of(kid) == {
        ("applygate", "held_tool"): True,
        ("applygate", "documented_deny"): False,
        ("applygate", "another_deny"): False,
    }


def test_patch_catalog_is_idempotent_on_a_held_true_key(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    """Running the mode twice must not drift (the harness's case 5: the old
    code 500'd on the FIRST call, so the second was unreachable)."""
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    fake.seed(kid, [("applygate", "held", True), ("applygate", "denied", False)])
    tc = _client_of(monkeypatch, fake)
    for _ in range(2):
        r = tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "catalog"}, headers=_h())
        assert r.status_code == 200, r.text
    assert fake.pairs_of(kid) == {("applygate", "held"): True, ("applygate", "denied"): False}


# ---------------------------------------------------------------------------
# audit trail
# ---------------------------------------------------------------------------


def test_patch_off_audits_mcp_acl_server_set(monkeypatch: pytest.MonkeyPatch, registry_off) -> None:
    kid = uuid.uuid4()
    fake = DenyDB(existing={kid})
    _patch_tools(monkeypatch, ["t1"])
    audited: list[dict] = []

    async def fake_audit(request, **kwargs):
        audited.append(kwargs)

    monkeypatch.setattr(routes_admin, "_audit", fake_audit)
    tc = _client_of(monkeypatch, fake)
    tc.patch(f"/admin/keys/{kid}/mcp-grants/applygate", json={"mode": "off"}, headers=_h())
    assert audited and audited[0]["action"] == "mcp.acl.server.set"
    assert audited[0]["details"]["server"] == "applygate"
    assert audited[0]["details"]["mode"] == "off"
    assert audited[0]["details"]["pairs_written"] == 1
    # the new action is registered in the documented inventory
    assert "mcp.acl.server.set" in routes_admin.AUDIT_ACTIONS_DOC


# ---------------------------------------------------------------------------
# (f) federation.load_denies — FALSE pairs + error isolation
# ---------------------------------------------------------------------------


class _DenyRowDB:
    """fetch() returns FALSE rows; every other shape unused."""

    def __init__(self, rows: list[dict], raises: Exception | None = None) -> None:
        self._rows = rows
        self._raises = raises

    async def fetch(self, _sql, *args):
        if self._raises is not None:
            raise self._raises
        return self._rows


async def test_load_denies_returns_false_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    kid = uuid.uuid4()
    monkeypatch.setattr(
        federation,
        "db",
        _DenyRowDB([{"server": "s", "tool": "t"}, {"server": "s", "tool": "u"}]),
    )
    denies = await federation.load_denies(kid)
    assert denies == {("s", "t"), ("s", "u")}


async def test_load_denies_empty_when_no_false_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(federation, "db", _DenyRowDB([]))
    assert await federation.load_denies(uuid.uuid4()) == set()


async def test_load_denies_isolates_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ACL gate must never 500 because of this read: a DB fault degrades
    to an empty deny set (same fail-visibility posture as load_grants'
    live-profile guard)."""
    monkeypatch.setattr(federation, "db", _DenyRowDB([], raises=RuntimeError("pool busted")))
    assert await federation.load_denies(uuid.uuid4()) == set()


async def test_load_denies_signature_for_scope_merge_seam() -> None:
    """The routes_proxy scope-merge agent binds to this EXACT contract:
    importable by name, one uuid arg, set of (server, tool) tuples."""
    import inspect

    from pcai_llm.mcpf.federation import load_denies as ld

    sig = inspect.signature(ld, eval_str=True)  # eval_str: PEP 563 string annotations
    assert list(sig.parameters) == ["key_id"]
    assert sig.parameters["key_id"].annotation is uuid.UUID
    assert sig.return_annotation == set[tuple[str, str]]


# ---------------------------------------------------------------------------
# (d) the scope-merge seam: a denied pair is absent from the relay catalog
# ---------------------------------------------------------------------------


def _relay_catalog_env(monkeypatch: pytest.MonkeyPatch, offers: list[str], denies: set[tuple[str, str]]):
    """Minimal wiring for routes_proxy._relay_catalog in scope mode: one
    enabled server offering `offers`, load_denies stubbed to `denies`.

    Mirrors the coordinator's verified repro (offered == ["a", "c"] once the
    denied pair "b" is subtracted after the scope union)."""
    import pcai_llm.api.routes_mcp as rm
    import pcai_llm.api.routes_proxy as rp
    from pcai_llm.core import registry as registry_mod

    async def fake_fetch_tools(_url, _headers, timeout_s=None):
        return [{"tool": t, "description": ""} for t in offers]

    monkeypatch.setattr(rm, "_fetch_tools", fake_fetch_tools)
    monkeypatch.setattr(rp, "registry", registry_mod)  # the module-level import

    class _Reg:
        async def mcp_servers(self):
            return [
                {
                    "name": "srv",
                    "url": "https://srv.invalid",
                    "headers": {},
                    "auth_secret": None,
                    "auth_token_enc": None,
                    "enabled": True,
                    "tool_policy": "all",
                    "allowed_tools": [],
                }
            ]

    monkeypatch.setattr(registry_mod, "mcp_servers", _Reg().mcp_servers)
    monkeypatch.setattr(federation, "load_denies", _denies_of(denies))
    monkeypatch.setattr(rp.settings, "tool_relay_grant_mode", "scope")
    rp.catalog_cache_clear()


def _denies_of(pairs: set[tuple[str, str]]):
    async def _f(_key_id):
        return pairs

    return _f


async def test_relay_catalog_scope_merge_drops_denied_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) THE regression the feature exists for: under the default "scope"
    grant mode the merge re-adds the whole policy-admitted catalog, so a
    denied pair must be subtracted AFTER the union — with an explicit grant
    for it, a scope grant, and a live catalog offer all in play."""
    import pcai_llm.api.routes_proxy as rp
    from pcai_llm.api.auth import KeyRecord

    kid = uuid.uuid4()
    _relay_catalog_env(monkeypatch, offers=["a", "b", "c"], denies={("srv", "b")})
    key = KeyRecord(
        id=kid,
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy", "mcp"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )
    # the denied pair is ALSO explicitly granted (a stale TRUE row a deny
    # overrides) — the union must not resurrect it
    catalog, effective = await rp._relay_catalog(key, {("srv", "a"), ("srv", "b")})
    assert [t["tool"] for t in catalog] == ["a", "c"], "denied pair must be absent after the scope union"
    assert effective == {("srv", "a"), ("srv", "c")}


async def test_relay_catalog_scope_merge_keeps_undelied_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: with no denies the same wiring offers everything the policy
    admits — so the previous test fails for the RIGHT reason (subtraction),
    not because the catalog never had the pair."""
    import pcai_llm.api.routes_proxy as rp
    from pcai_llm.api.auth import KeyRecord

    _relay_catalog_env(monkeypatch, offers=["a", "b", "c"], denies=set())
    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy", "mcp"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )
    catalog, _ = await rp._relay_catalog(key, set())
    assert [t["tool"] for t in catalog] == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# (g) the console's Save math — extracted from admin.html and run under node
# ---------------------------------------------------------------------------


def _extract_js(name: str) -> str:
    """Pull one top-level `function name(...) { … }` body out of admin.html
    (the idiom tests/test_admin_ui.py established for esc/renderToolDesc)."""
    import re
    from pathlib import Path

    src = Path("src/pcai_llm/ui/admin.html").read_text()
    m = re.search(rf"function {name}\(.*?\n\}}", src, re.DOTALL)
    assert m, f"{name} not extractable from admin.html"
    return m.group(0)


def _run_node(fn_src: str, harness: str) -> None:
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(fn_src + "\n" + harness)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"UI math mismatch:\n{r.stdout}\n{r.stderr}"


def test_ui_grant_save_body_writes_a_deny_for_an_unticked_held_pair() -> None:
    """D-F1's headline no-op (review §3.1): unticking a pair that WAS held
    (a TRUE grant or a profile live-grant pair) must produce a deny. The old
    math derived the deny set from the held DENY rows, so an unticked held
    pair appeared in neither input and wrote nothing — under the default
    scope grant mode the relay re-added it and the untick did nothing."""
    harness = """
const t = (n, got, want) => {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  if (g !== w) { console.error(`FAIL ${n}: got ${g} want ${w}`); process.exit(1); }
};
const rows = [
  {server: "srv", tool: "A", allowed: true},
  {server: "srv", tool: "P", allowed: true, source: "profile"},
  {server: "srv", tool: "B", allowed: false},
];
// A unticked, P unticked, B re-ticked (B was denied-held before)
t("untick-held-pairs", grantSaveBody(rows, [["srv", "B"]]).denies,
  [{server: "srv", tool: "A"}, {server: "srv", tool: "P"}]);
// nothing unticked -> no denies
t("no-change", grantSaveBody(rows, [["srv", "A"], ["srv", "P"], ["srv", "B"]]).denies, []);
// everything unticked -> every held pair denied
t("untick-all", grantSaveBody(rows, []).denies,
  [{server: "srv", tool: "A"}, {server: "srv", tool: "P"}]);
// a pair that was never held and is not ticked is NOT a deny (nothing to hold back)
t("untouched-not-denied", grantSaveBody([{server: "srv", tool: "A", allowed: true}],
  [["srv", "A"], ["srv", "Z"]]).denies, []);
// re-ticking a denied pair MUST appear in grants (the PUT upserts it TRUE)
t("re-tick-goes-into-grants", grantSaveBody(rows, [["srv", "B"]]).grants,
  [{server: "srv", tool: "B"}]);
// a profile pair the operator never ticked stays a live preset, not a key grant
t("profile-pair-not-materialised", grantSaveBody(rows, [["srv", "A"]]).grants,
  [{server: "srv", tool: "A"}]);
// untick+re-tick of a profile pair inside one session is a no-op: no key row
// is written, so the live preset keeps deciding it (and no deny is left over)
const rt = grantSaveBody(rows, [["srv", "P"]]);
t("profile-pair-re-ticked-is-a-no-op", rt.grants, []);
t("profile-pair-re-ticked-writes-no-deny", rt.denies, [{server: "srv", tool: "A"}]);
// a pair whose deny row the operator re-ticks is NOT profile-marked (its row
// is allowed=false), so it must land in grants for the PUT to flip it TRUE
t("re-tick-denied-pair-not-profile-filtered",
  grantSaveBody([{server: "srv", tool: "D", allowed: false}], [["srv", "D"]]).grants,
  [{server: "srv", tool: "D"}]);
"""
    _run_node(_extract_js("grantSaveBody"), harness)


def test_ui_save_uses_the_pure_body_builder() -> None:
    """The Save handler must call the pinned builder (a re-inlined copy of
    the math would drift away from the tested contract)."""
    from pathlib import Path

    src = Path("src/pcai_llm/ui/admin.html").read_text()
    assert "const body = grantSaveBody(rows, selected);" in src
    assert "denyDelta(" not in src, "the old deny-rows-only math must be gone"


def test_ui_rotate_fallback_splits_grants_and_denies() -> None:
    """The rotate fallback re-PUTs the GET array; with the new GET shape it
    must send allowed rows as grants and FALSE rows as denies — re-sending a
    deny as a grant would re-enable a deliberately denied pair."""
    import re
    from pathlib import Path

    src = Path("src/pcai_llm/ui/admin.html").read_text()
    block = re.search(
        r"const acl = await api\(`/keys/\$\{id\}/mcp-grants`\);.*?grants, denies \} \}\);", src, re.DOTALL
    )
    assert block, "rotate fallback block not found in admin.html"
    text = block.group(0)
    assert "filter(g => g.allowed !== false)" in text and "grants" in text
    assert "filter(g => g.allowed === false)" in text and "denies" in text
    assert "body: { grants }" not in text, "rotate must not re-PUT the raw GET array as grants"
