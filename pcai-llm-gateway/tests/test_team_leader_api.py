"""Team-leader console API tests (fake-db style — the test_admin.py seam pattern).

What is pinned here, per the contract:
- leader auth: both envelopes (Bearer + X-API-Key), 401 on miss, the prefix fetch;
- SCOPING: a key id outside the enrollment is a 404 on every id-taking route
  (PATCH / DELETE / budgets / per-key usage) — the scoped WHERE, never
  fetch-then-compare, and never a 403 (no existence oracle);
- POST /keys stamps enrollment_id + tenant + the enrollment profile's presets
  and returns the plaintext exactly once;
- the ENVELOPE CHECK: 409 with the numbers on violation, allowed under it,
  with the key's own current rows excluded on the replace-all path;
- overview numbers (total / allocated / spent / unallocated / key counts);
- scoped usage + summary + traces (the scope rides INSIDE the SQL / traces
  call, and a foreign trace id is a 404);
- /team_leader/presented-models serves the SAME payload object the admin
  route serves (the presented_models_payload() delegation).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_team_leader
from pcai_llm.config import settings

EID = uuid.uuid4()
TENANT = uuid.uuid4()
PROFILE_ID = uuid.uuid4()
FOREIGN_KEY = uuid.uuid4()
LEADER_KEY = "hllm-team-leader-plaintext"


def _enrollment(**over: Any) -> Any:
    """A contract-shaped TeamEnrollment (the REAL core.teams dataclass —
    this file doubles as a shape check on the core surface the routes
    import)."""
    from pcai_llm.core.teams import TeamEnrollment

    base: dict[str, Any] = {
        "id": EID,
        "label": "station-9",
        "tenant_id": TENANT,
        "profile_id": PROFILE_ID,
        "budget_tokens": 1_000_000,
        "budget_usd": 50.0,
        "budget_period": "month",
        "overflow_enabled": True,
    }
    base.update(over)
    return TeamEnrollment(**base)


class _FakeConn:
    """Acquired-connection stand-in (test_admin.FakeConn shape): delegates to
    the parent fake so the transactional mint / replace-all paths run
    through the same SQL-shape dispatch as the module pool."""

    def __init__(self, db: TeamFakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        # Snapshot the transactional state so __aexit__ can model a ROLLBACK
        # (audit W6 F4: a refused mint must give its seat back).
        self._db._txn_mint_uses = self._db.mint_uses
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None = None, *exc: object) -> bool:
        if exc_type is not None:
            self._db.mint_uses = self._db._txn_mint_uses  # the raise rolls back
        return False

    async def executemany(self, sql: str, args: Any):
        self._db.bulk.append((sql, list(args)))

    async def fetchrow(self, sql: str, *args: Any):
        return await self._db.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any):
        return await self._db.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)


class TeamFakeDB:
    """Stateful fake for the team-leader surface (dispatch on SQL shape).
    Records every statement so tests can assert the scoped WHERE clauses and
    the stamped columns."""

    def __init__(self) -> None:
        self.fetchrows: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []
        # The mint allowance (audit W6 #2): models the redeem door's seat
        # predicate so a test can exhaust it. None = unlimited (the default,
        # which keeps every pre-existing mint test byte-identical).
        self.mint_max_uses: int | None = None
        self.mint_uses = 0
        self._txn_mint_uses = 0
        # Statement ordering across seams (audit W6 F4): the seat claim and the
        # envelope check append here so a pin can assert which ran first.
        self.events: list[str] = []
        self.bulk: list[tuple[str, list]] = []
        self.scoped_ok = True  # the WHERE id = $1 AND enrollment_id = $2 probe
        self.key_rows: list[dict[str, Any]] = []  # core.keys.list_keys output
        self.own_alloc = {"tokens": 0, "usd": 0.0}
        self.usage_rows: list[dict[str, Any]] = []
        self.summary_totals: dict[str, Any] = {}
        self.profile_row: dict[str, Any] | None = None
        self.patch_row: dict[str, Any] | None = None
        self.spend_ancestors: dict[str, Any] = {"spend_ancestors": []}
        # THREAD B: the top-up queue (pending probe + the enrollment's rows)
        self.topup_pending = False
        self.topup_rows: list[dict[str, Any]] = []
        self.fetch_sqls: list[str] = []  # fetch()-executed SQL, for shape pins
        # Recorded by the monkeypatched core helpers (assigned from OUTSIDE the
        # class, so the declarations have to live here for the type checker).
        self.leader_key_presented: str = ""
        self.spend_args: tuple[Any, ...] = ()
        self.alloc_args: tuple[Any, ...] = ()
        self.available_args: tuple[Any, ...] = ()
        self.state_args: tuple[Any, ...] = ()
        self.allocated_tokens: int = 0
        self.allocated_usd: float = 0.0

    async def fetchrow(self, sql: str, *args: Any):
        self.fetchrows.append((sql, args))
        if "team_leader_key_prefix" in sql and "FROM enrollments" in sql:
            return {"team_leader_key_prefix": "hllm-LD00", "team_leader_key_created_at": datetime.now(UTC)}
        if "SELECT team_leader_key_prefix FROM enrollments" in sql:
            return {"team_leader_key_prefix": "hllm-LD00"}
        if "JOIN tenants t ON t.id = e.tenant_id" in sql:
            return {"tenant_name": "acme", "profile_name": "starter"}
        if "count(*) FILTER (WHERE revoked_at IS NULL)" in sql:
            return {"total": 4, "active": 3}
        if "SELECT (SELECT COALESCE(SUM(limit_tokens), 0) FROM budgets" in sql:
            return dict(self.own_alloc)
        if "FROM usage_ledger" in sql:
            out = dict(self.summary_totals)
            # The console's "Billed tokens" is a BUDGET number, so the fake models
            # the statement's OWN FILTER: a row outside the costed set is free
            # (audit W6 F2). Modelling the predicate is what makes the pin bite —
            # a fake that ignored it would report the same number either way.
            if self.usage_rows:
                rows = list(self.usage_rows)
                if "FILTER (WHERE l.status IN" in sql:
                    allowed = re.findall(r"'([^']+)'", sql.split("FILTER (WHERE l.status IN", 1)[1].split(")", 1)[0])
                    rows = [r for r in rows if r.get("status") in allowed]
                frac = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
                out["billed_tokens"] = sum(
                    max(int(r.get("total_tokens", 0)) - int(r.get("cached_tokens", 0)) * frac, 0)
                    * float(r.get("billing_fraction", 1.0))
                    for r in rows
                )
            return out
        if "spend_ancestors" in sql and "FROM api_keys" in sql:
            return dict(self.spend_ancestors)
        if "INSERT INTO users" in sql:
            return {"id": uuid.uuid4(), "name": args[1]}
        if "INSERT INTO api_keys" in sql:
            return {"id": uuid.uuid4()}
        if "FROM profiles WHERE id" in sql:
            return self.profile_row
        if "UPDATE api_keys SET" in sql:
            return self.patch_row
        if "INSERT INTO enrollment_topup_requests" in sql:
            return {
                "id": uuid.uuid4(),
                "enrollment_id": args[0],
                "requested_tokens": args[1],
                "requested_usd": args[2],
                "note": args[3],
                "status": "pending",
                "created_at": "now",
                "resolved_at": None,
            }
        return None

    async def fetchval(self, sql: str, *args: Any):
        if "SELECT 1 FROM api_keys WHERE id = $1 AND enrollment_id = $2" in sql:
            return 1 if self.scoped_ok else None
        if "FROM enrollment_topup_requests" in sql:
            # the ONE-open-ask probe: truthy only when a pending row exists
            return 1 if self.topup_pending else None
        return 1

    async def fetch(self, sql: str, *args: Any):
        self.fetch_sqls.append(sql)
        if "FROM enrollment_topup_requests" in sql:
            return list(self.topup_rows)
        if "FROM usage_ledger" in sql:
            if "GROUP BY l.model" in sql:
                return [{"model": "m1", "requests": 2, "cost_usd": 0.5}]
            if "GROUP BY k.name, u.name" in sql:
                return [{"key_name": "k", "user_name": "u", "requests": 2, "cost_usd": 0.5}]
            return list(self.usage_rows)
        if "FROM api_keys k" in sql:
            return list(self.key_rows)
        return []

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        if "UPDATE enrollments SET uses = uses + 1" in sql:
            self.events.append("seat_claim")
            # the redeem door's own predicate (routes_enroll._REDEEM_SQL):
            # a seat is claimed only while uses < max_uses
            if self.mint_max_uses is not None and self.mint_uses >= self.mint_max_uses:
                return "UPDATE 0"
            self.mint_uses += 1
            return "UPDATE 1"
        return "UPDATE 1"

    def acquire(self):
        return _FakeConn(self)


class _AuditRecorder:
    """Captures routes_team_leader._audit calls (the module imported the
    function object, so the patch is on the routes module)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, request: Request, **kw: Any) -> None:
        self.calls.append(kw)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    enrollment = _enrollment()
    fake = TeamFakeDB()
    import pcai_llm.core.budgets as budgets_mod
    import pcai_llm.core.keys as keys_mod
    import pcai_llm.core.teams as teams_mod

    # --- wave-2 pool model (the in-test state the helpers read) ---
    # The ROUTE only ever consults the core helpers, so the state lives
    # here and the fakes re-derive available exactly per the invariant:
    # available = cap - spent - SUM(committed of keys != exclude).
    # committed entries are (tokens, usd) per key id. team_spend reads the
    # SAME pool: the 409 message's spent number must agree with the
    # available math (in production both read the ledger).
    pool: dict[str, Any] = {
        "no_cap": False,
        "cap_tokens": 1_000_000,
        "spent_tokens": 0,
        "cap_usd": 50.0,
        "spent_usd": 0.0,
        "committed": {},
    }

    async def fake_auth(plaintext: str):
        fake.leader_key_presented = plaintext
        return enrollment if plaintext == LEADER_KEY else None

    async def fake_spend(eid: uuid.UUID, period: str):
        fake.spend_args = (eid, period)
        return (pool["spent_tokens"], pool["spent_usd"])

    async def fake_alloc(eid: uuid.UUID, period: str):
        fake.alloc_args = (eid, period)
        return (fake.allocated_tokens, fake.allocated_usd)

    def _committed_others(exclude_key_id: uuid.UUID | None) -> tuple[int, float]:
        t = sum(ct for k, (ct, cu) in pool["committed"].items() if k != exclude_key_id)
        u = sum(cu for k, (ct, cu) in pool["committed"].items() if k != exclude_key_id)
        return t, u

    async def fake_available(
        eid: uuid.UUID,
        period: str,
        *,
        exclude_key_id: uuid.UUID | None = None,
    ):
        fake.available_args = (eid, period, exclude_key_id)
        fake.events.append("envelope_check")
        if pool["no_cap"]:
            return None, None
        ct, cu = _committed_others(exclude_key_id)
        return (pool["cap_tokens"] - pool["spent_tokens"] - ct, pool["cap_usd"] - pool["spent_usd"] - cu)

    async def fake_state(eid: uuid.UUID, period: str):
        fake.state_args = (eid, period)
        return {k: SimpleNamespace(committed_tokens=ct, committed_usd=cu) for k, (ct, cu) in pool["committed"].items()}

    stamps: list[uuid.UUID] = []

    async def fake_stamp(eid: uuid.UUID, *, min_interval_s: float = 3600.0) -> None:
        stamps.append(eid)

    fake.allocated_tokens, fake.allocated_usd = 200_000, 0.0
    invalidated: list[Any] = []

    def fake_invalidate(eid: Any = None) -> None:
        invalidated.append(eid)

    monkeypatch.setattr(teams_mod, "authenticate_leader", fake_auth)
    monkeypatch.setattr(teams_mod, "team_spend", fake_spend)
    monkeypatch.setattr(teams_mod, "team_allocated", fake_alloc)
    monkeypatch.setattr(teams_mod, "invalidate", fake_invalidate)
    # raising=False: the wave-2 helpers may not be landed on core yet —
    # the fakes stand in either way (and keep standing in after they land)
    monkeypatch.setattr(teams_mod, "team_available", fake_available, raising=False)
    monkeypatch.setattr(teams_mod, "team_allocation_state", fake_state, raising=False)
    monkeypatch.setattr(teams_mod, "stamp_leader_used", fake_stamp, raising=False)
    # every db seam the team surface touches (the test_admin.py fixture
    # pattern: the module handle AND the core modules it calls through)
    monkeypatch.setattr(routes_team_leader, "db", fake)
    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    audit = _AuditRecorder()
    monkeypatch.setattr(routes_team_leader, "_audit", audit)
    monkeypatch.setattr(settings, "admin_api_keys", "")  # admin surface unused here
    app = FastAPI()
    app.include_router(routes_team_leader.router)
    tc = TestClient(app, raise_server_exceptions=False)
    tc.fake = fake  # type: ignore[attr-defined]
    tc.enrollment = enrollment  # type: ignore[attr-defined]
    tc.audit = audit  # type: ignore[attr-defined]
    tc.invalidated = invalidated  # type: ignore[attr-defined]
    tc.pool = pool  # type: ignore[attr-defined]
    tc.stamps = stamps  # type: ignore[attr-defined]
    return tc


def _h() -> dict[str, str]:
    return {"Authorization": f"Bearer {LEADER_KEY}"}


# ---------------------------------------------------------------------------
# leader auth
# ---------------------------------------------------------------------------


def test_missing_key_is_401(env) -> None:
    assert env.get("/team_leader/overview").status_code == 401
    assert env.get("/team_leader/keys").status_code == 401


def test_invalid_key_is_401(env) -> None:
    bad = {"Authorization": "Bearer hllm-not-a-leader-key"}
    assert env.get("/team_leader/overview", headers=bad).status_code == 401
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    # the presented (wrong) value reached the auth seam — nothing short-circuited
    assert fake.leader_key_presented == "hllm-not-a-leader-key"


def test_both_envelopes_authenticate(env) -> None:
    assert env.get("/team_leader/overview", headers=_h()).status_code == 200
    r = env.get("/team_leader/overview", headers={"X-API-Key": LEADER_KEY})
    assert r.status_code == 200


def test_dead_explicit_envelope_is_a_hard_401(env) -> None:
    """D19 fail-closed: a dead X-API-Key is 401 even if a (here absent)
    bearer could try — no envelope laundering."""
    r = env.get("/team_leader/overview", headers={"X-API-Key": "hllm-dead"})
    assert r.status_code == 401


def test_overview_shape(env) -> None:
    env.pool["spent_tokens"], env.pool["spent_usd"] = 123_456, 1.23  # type: ignore[attr-defined]
    r = env.get("/team_leader/overview", headers=_h())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["enrollment"]["id"] == str(EID)
    assert d["enrollment"]["label"] == "station-9"
    assert d["enrollment"]["tenant_name"] == "acme"
    assert d["budget"] == {
        "period": "month",
        "tokens": 1_000_000,
        "usd": 50.0,
        "overflow_enabled": True,
        # 2026-10-30: the console must be able to tell an admin-set cap from
        # one derived from the profile x max_uses, and to render "active / N".
        "derived": False,
        "max_uses": 0,
    }
    assert d["spend"] == {"tokens": 123_456, "usd": 1.23}
    assert d["allocated"] == {"tokens": 200_000, "usd": 0.0}
    assert d["unallocated"] == {"tokens": 800_000, "usd": 50.0}
    assert d["keys"] == {"total": 4, "active": 3, "max_uses": 0}
    assert d["leader"]["key_prefix"] == "hllm-LD00"
    assert d["leader"]["created_at"]
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    # the numbers come from the gate's own math, for THIS enrollment + period
    assert fake.spend_args == (EID, "month")
    assert fake.alloc_args == (EID, "month")


# ---------------------------------------------------------------------------
# scoping (the no-oracle 404)
# ---------------------------------------------------------------------------


def test_scoped_404_on_patch(env) -> None:
    env.fake.scoped_ok = False  # type: ignore[attr-defined]
    r = env.patch(f"/team_leader/keys/{FOREIGN_KEY}", json={"name": "x"}, headers=_h())
    assert r.status_code == 404
    # the scoped WHERE ran; the UPDATE never did
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert not any("UPDATE api_keys SET" in s for s, _ in fake.fetchrows)


def test_scoped_404_on_delete(env) -> None:
    env.fake.scoped_ok = False  # type: ignore[attr-defined]
    r = env.delete(f"/team_leader/keys/{FOREIGN_KEY}", headers=_h())
    assert r.status_code == 404


def test_scoped_404_on_budgets(env) -> None:
    env.fake.scoped_ok = False  # type: ignore[attr-defined]
    body = {"budgets": [{"model": "*", "period": "month", "limit_tokens": 100}]}
    r = env.put(f"/team_leader/keys/{FOREIGN_KEY}/budgets", json=body, headers=_h())
    assert r.status_code == 404
    # and the envelope check never ran for a foreign key
    assert not any("DELETE FROM budgets" in s for s, _ in env.fake.executed)  # type: ignore[attr-defined]


def test_scoped_404_on_key_usage(env) -> None:
    env.fake.scoped_ok = False  # type: ignore[attr-defined]
    r = env.get(f"/team_leader/keys/{FOREIGN_KEY}/usage", headers=_h())
    assert r.status_code == 404


def test_scoped_404_on_usage_key_filter(env) -> None:
    env.fake.scoped_ok = False  # type: ignore[attr-defined]
    r = env.get(f"/team_leader/usage?key_id={FOREIGN_KEY}", headers=_h())
    assert r.status_code == 404


def test_scoped_404_on_foreign_trace(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """traces.get receives the enrollment scope; a request id whose key is
    outside it is None -> the same 404 as an unknown id."""
    import pcai_llm.core.traces as traces_mod

    seen: dict[str, Any] = {}

    async def fake_get(request_id: str, enrollment_id: uuid.UUID | None = None):
        seen["args"] = (request_id, enrollment_id)

    monkeypatch.setattr(traces_mod, "get", fake_get)
    r = env.get("/team_leader/traces/req-foreign", headers=_h())
    assert r.status_code == 404
    assert seen["args"] == ("req-foreign", EID)


# ---------------------------------------------------------------------------
# POST /team_leader/keys — the teammate mint
# ---------------------------------------------------------------------------


def test_mint_stamps_enrollment_and_tenant(env) -> None:
    env.enrollment.profile_id = None  # type: ignore[attr-defined]  # no presets this mint
    r = env.post("/team_leader/keys", json={"name": "teammate-a"}, headers=_h())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["key"].startswith("hllm-")
    assert body["key_prefix"] == body["key"][:10]
    assert "shown once" in body["message"]
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    # tenant stamping: the user row was created in the ENROLLMENT's tenant
    user_insert = next(a for s, a in fake.fetchrows if "INSERT INTO users" in s)
    assert user_insert[0] == TENANT
    # enrollment stamping: the key INSERT carries the enrollment id
    key_insert = next(a for s, a in fake.fetchrows if "INSERT INTO api_keys" in s)
    assert EID in key_insert, "api_keys.enrollment_id must be stamped at mint"
    # the mint invalidated the gate's enrollment lookups
    assert env.invalidated == [None]  # type: ignore[attr-defined]


def test_mint_seeds_profile_presets(env) -> None:
    env.fake.profile_row = {  # type: ignore[attr-defined]
        "id": PROFILE_ID,
        "name": "starter",
        "defaults_json": json.dumps(
            {
                "budgets": [{"model": "*", "period": "month", "limit_tokens": 5_000}],
                "mcp_grants": [["srv", "tool"]],
            }
        ),
        "model_order": [],
    }
    r = env.post("/team_leader/keys", json={"name": "teammate-b"}, headers=_h())
    assert r.status_code == 201, r.text
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    bulk = fake.bulk
    assert any("INSERT INTO budgets" in s for s, _ in bulk), "profile token preset stamped"
    assert any("INSERT INTO mcp_tool_acls" in s for s, _ in bulk), "profile grants stamped"
    # scopes are the fixed enroll pair — no admin/mcp-admin escalation
    key_insert = next(a for s, a in fake.fetchrows if "INSERT INTO api_keys" in s)
    assert list(key_insert[4]) == ["proxy", "mcp"]


def test_mint_with_allocation_writes_envelope_rows(env) -> None:
    env.enrollment.profile_id = None  # type: ignore[attr-defined]
    r = env.post(
        "/team_leader/keys",
        json={"name": "teammate-c", "budget_tokens": 250_000, "budget_usd": 5.0},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    b_insert = next(a for s, a in fake.executed if "INSERT INTO budgets" in s)
    assert b_insert[1] == "*" and b_insert[2] == "month" and b_insert[3] == 250_000
    usd_insert = next(a for s, a in fake.executed if "INSERT INTO cost_budgets" in s)
    assert usd_insert[1] == "*" and usd_insert[2] == "month" and usd_insert[3] == 5.0


def test_mint_envelope_violation_409(env) -> None:
    """Spend-aware refusal: cap 1M, 900k already SPENT (the pool the team
    consumed), no commitments elsewhere -> available 100k; requesting
    500k is a 409 naming ALL FOUR pool numbers, and NOTHING is written
    (no user, no key)."""
    env.enrollment.profile_id = None  # type: ignore[attr-defined]
    env.pool["spent_tokens"] = 900_000  # type: ignore[attr-defined]
    r = env.post("/team_leader/keys", json={"name": "x", "budget_tokens": 500_000}, headers=_h())
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "cap 1000000" in detail, detail
    assert "spent 900000" in detail, detail
    assert "committed to other keys 0" in detail, detail
    assert "available 100000" in detail, detail
    assert "requested 500000" in detail, detail
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert not any("INSERT INTO api_keys" in s for s, _ in fake.fetchrows)


def test_mint_envelope_ok_when_under(env) -> None:
    env.enrollment.profile_id = None  # type: ignore[attr-defined]
    env.pool["spent_tokens"] = 200_000  # type: ignore[attr-defined]
    r = env.post("/team_leader/keys", json={"name": "x", "budget_tokens": 500_000}, headers=_h())
    assert r.status_code == 201, r.text


def test_mint_envelope_skipped_when_no_cap(env) -> None:
    """No cap on the enrollment -> team_available answers (None, None) ->
    the allocation is advisory (the per-key gate still enforces it), so a
    huge allocation is accepted."""
    env.enrollment.profile_id = None  # type: ignore[attr-defined]
    env.enrollment.budget_tokens = None  # type: ignore[attr-defined]
    env.pool["no_cap"] = True  # type: ignore[attr-defined]
    r = env.post("/team_leader/keys", json={"name": "x", "budget_tokens": 10**9}, headers=_h())
    assert r.status_code == 201, r.text


def test_mint_subject_blank_400(env) -> None:
    env.enrollment.profile_id = None  # type: ignore[attr-defined]
    r = env.post("/team_leader/keys", json={"name": "x", "subject": "   "}, headers=_h())
    assert r.status_code == 400


def test_mint_audits_team_key_create(env) -> None:
    env.enrollment.profile_id = None  # type: ignore[attr-defined]
    env.post("/team_leader/keys", json={"name": "x"}, headers=_h())
    call = env.audit.calls[-1]  # type: ignore[attr-defined]
    assert call["action"] == "team.key.create"
    assert call["actor_source"] == "team-leader"
    assert call["actor_subject"] == "station-9"  # the enrollment LABEL, never key material


def test_mint_consumes_the_enrollments_mint_allowance(env) -> None:
    """FIX 2 (audit W6 #2): the delegated mint draws on the SAME seat counter the
    redeem door uses (enrollments.uses / max_uses), inside the mint transaction.

    Pre-fix the handler never read max_uses and never incremented uses, so a
    leader could mint an unbounded number of credentials from a one-mint
    enrollment while the admin's 'uses / max_uses' column stayed at 0."""
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    env.enrollment.max_uses = 1  # type: ignore[attr-defined]
    fake.mint_max_uses = 1

    r1 = env.post("/team_leader/keys", json={"name": "teammate-a"}, headers=_h())
    assert r1.status_code == 201, r1.text
    assert r1.json()["key"], "the first mint spends the only seat and returns the plaintext once"
    assert fake.mint_uses == 1, "the mint consumed a seat on the redeem door's counter"

    before = len(fake.fetchrows)
    r2 = env.post("/team_leader/keys", json={"name": "teammate-b"}, headers=_h())
    assert r2.status_code == 409, r2.text
    assert "mint allowance" in r2.json()["detail"]
    assert fake.mint_uses == 1, "a refused mint must not consume a seat"
    assert not any("INSERT INTO api_keys" in s for s, _ in fake.fetchrows[before:]), (
        "the 409 fires before a key is written (the seat claim is the first statement)"
    )


def test_mint_name_rejects_html_metacharacters(env) -> None:
    """FIX 6 (audit W6, defence in depth): a key NAME is a display value the team
    console, the admin traces explorer and the CSV exports all render. The sinks
    escape; this closes the STORED value at the source so a delegated leader
    cannot persist a tag for a future sink to forget. Clean 422, never a 500."""
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    r = env.post("/team_leader/keys", json={"name": "<img src=x onerror=alert(1)>"}, headers=_h())
    assert r.status_code == 422, r.text
    assert not any("INSERT INTO api_keys" in s for s, _ in fake.fetchrows), "nothing was minted"
    # a control character is refused too
    assert env.post("/team_leader/keys", json={"name": "a\u0000b"}, headers=_h()).status_code == 422


def test_mint_name_accepts_ordinary_unicode(env) -> None:
    """The rule is CONSERVATIVE, not ASCII-only: spaces, unicode and
    . - _ / : ( ) are ordinary key names and must keep minting."""
    r = env.post("/team_leader/keys", json={"name": "Ops — West/EU (2026)"}, headers=_h())
    assert r.status_code == 201, r.text
    assert r.json()["key"]


def test_usage_summary_billed_tokens_skips_non_costed_rows(env) -> None:
    """PIN (audit W6 F2): "Billed tokens" is a BUDGET number, so it must count
    exactly what the gate charges.

    cache_hit rows cost no upstream spend (core.budgets._COSTED_STATUSES exempts
    them), so they must not inflate the team console's number either — the
    console and the gate must never tell two stories about one budget."""
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    fake.usage_rows = [
        {"status": "ok", "total_tokens": 1_000, "cached_tokens": 0, "billing_fraction": 1.0},
        {"status": "cache_hit", "total_tokens": 5_000, "cached_tokens": 0, "billing_fraction": 1.0},
    ]
    r = env.get("/team_leader/usage/summary", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["totals"]["billed_tokens"] == 1_000, (
        "the cache_hit row is free upstream, so it must not count as billed tokens"
    )


def test_envelope_check_runs_inside_the_mint_transaction(env) -> None:
    """PIN (audit W6 F4): the envelope check must run AFTER the seat claim, inside
    the mint transaction.

    The seat claim takes the enrollment row lock, so a concurrent mint serializes
    behind this transaction and its check then sees the earlier mint's COMMITTED
    allocation. Pre-fix the check ran before the transaction opened, so two mints
    could read the same headroom and both write (over-committed promises)."""
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    fake.events.clear()
    r = env.post("/team_leader/keys", json={"name": "x", "budget_tokens": 500_000}, headers=_h())
    assert r.status_code == 201, r.text
    assert "seat_claim" in fake.events and "envelope_check" in fake.events
    assert fake.events.index("seat_claim") < fake.events.index("envelope_check"), (
        "the seat claim must come FIRST: its row lock is what makes a concurrent "
        "mint's envelope check see this mint's committed allocation"
    )
    # ...and a refusal unwinds the seat claim with the rest of the transaction
    env.pool["spent_tokens"] = 10_000_000  # type: ignore[attr-defined]
    fake.events.clear()
    before = fake.mint_uses
    r2 = env.post("/team_leader/keys", json={"name": "y", "budget_tokens": 500_000}, headers=_h())
    assert r2.status_code == 409, r2.text
    assert "seat_claim" in fake.events, "the claim ran before the envelope refused"
    assert fake.mint_uses == before, "and the refusal rolled that claim back"


def test_patch_name_rejects_html_metacharacters(env) -> None:
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    r = env.patch(f"/team_leader/keys/{FOREIGN_KEY}", json={"name": "<script>x</script>"}, headers=_h())
    assert r.status_code == 422, r.text
    assert not any("UPDATE api_keys SET" in s for s, _ in fake.fetchrows), "no write reached the row"


# ---------------------------------------------------------------------------
# budgets replace + envelope
# ---------------------------------------------------------------------------


def _budget_body(tokens: int) -> dict[str, Any]:
    return {"budgets": [{"model": "*", "period": "month", "limit_tokens": tokens}]}


def test_budget_replace_envelope_violation_409(env) -> None:
    """Spend-aware refusal, replace-all shape: pool cap 1000, no spend,
    committed = this key 100 + another key 250. The helper EXCLUDES this
    key's own commitment, so available = 1000 - 250 = 750; requesting 751
    is a 409 that names ALL FOUR pool numbers."""
    env.pool["committed"] = {}  # type: ignore[attr-defined]
    kid = uuid.uuid4()
    other = uuid.uuid4()
    env.pool["cap_tokens"] = 1000  # type: ignore[attr-defined]
    env.pool["committed"][kid] = (100, 0.0)  # type: ignore[attr-defined]
    env.pool["committed"][other] = (250, 0.0)  # type: ignore[attr-defined]
    r = env.put(f"/team_leader/keys/{kid}/budgets", json=_budget_body(751), headers=_h())
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "cap 1000" in detail and "spent 0" in detail, detail
    assert "committed to other keys 250" in detail, detail  # EXCLUDES this key's own 100
    assert "available 750" in detail, detail
    assert "requested 751" in detail, detail
    # the helper was consulted with THIS key excluded
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert fake.available_args[2] == kid
    # nothing was written before the refusal
    assert not any("DELETE FROM budgets" in s for s, _ in fake.executed)


def test_budget_replace_exclusion_is_load_bearing(env) -> None:
    """THE exclusion proof: the same request lands ONLY because the key's
    own 100 commitment is excluded (a check that counted it would see
    available 650 and refuse 750)."""
    kid = uuid.uuid4()
    other = uuid.uuid4()
    env.pool["cap_tokens"] = 1000  # type: ignore[attr-defined]
    env.pool["committed"][kid] = (100, 0.0)  # type: ignore[attr-defined]
    env.pool["committed"][other] = (250, 0.0)  # type: ignore[attr-defined]
    r = env.put(f"/team_leader/keys/{kid}/budgets", json=_budget_body(750), headers=_h())
    assert r.status_code == 200, r.text
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert any("DELETE FROM budgets" in s for s, _ in fake.executed)
    assert any("DELETE FROM cost_budgets" in s for s, _ in fake.executed)
    assert env.invalidated[-1] == EID  # type: ignore[attr-defined]
    call = env.audit.calls[-1]  # type: ignore[attr-defined]
    assert call["action"] == "team.budget.set" and call["actor_source"] == "team-leader"


def test_budget_replace_bad_period_400(env) -> None:
    kid = uuid.uuid4()
    r = env.put(
        f"/team_leader/keys/{kid}/budgets",
        json={"budgets": [{"model": "*", "period": "hour", "limit_tokens": 10}]},
        headers=_h(),
    )
    assert r.status_code == 400


def test_budget_replace_empty_list_400(env) -> None:
    r = env.put(f"/team_leader/keys/{uuid.uuid4()}/budgets", json={"budgets": []}, headers=_h())
    assert r.status_code == 400


def test_budget_replace_both_limits_400(env) -> None:
    body = {"budgets": [{"model": "*", "period": "month", "limit_tokens": 10, "limit_usd": 1.0}]}
    r = env.put(f"/team_leader/keys/{uuid.uuid4()}/budgets", json=body, headers=_h())
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# keys list + per-key usage (scoped)
# ---------------------------------------------------------------------------


def test_keys_list_scoped_with_budget_status(env) -> None:
    kid = uuid.uuid4()
    env.fake.key_rows = [  # type: ignore[attr-defined]
        {"id": kid, "name": "teammate-a", "key_prefix": "hllm-AA"},
    ]
    r = env.get("/team_leader/keys", headers=_h())
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["id"] == str(kid)
    assert rows[0]["budget_status"] == []  # attached from budget_status_many


def test_key_usage_shape(env) -> None:
    kid = uuid.uuid4()
    env.fake.usage_rows = [  # type: ignore[attr-defined]
        {"id": 1, "model": "m1", "total_tokens": 10, "status": "ok"},
    ]
    r = env.get(f"/team_leader/keys/{kid}/usage", headers=_h())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["recent"][0]["model"] == "m1"
    assert "budgets" in d


def test_usage_query_carries_the_scope_first(env) -> None:
    r = env.get("/team_leader/usage?model=m1&limit=5", headers=_h())
    assert r.status_code == 200
    assert r.json() == []  # the fake ledger is empty; the point is the SQL below


def test_usage_scope_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE scoping pin: the ledger query embeds the enrollment subselect as
    the FIRST condition and binds the enrollment id as $1."""
    import pcai_llm.core.teams as teams_mod

    captured: dict[str, Any] = {}

    class _DB:
        async def fetch(self, sql: str, *args: Any):
            captured["sql"] = sql
            captured["args"] = args
            return []

        async def fetchrow(self, sql: str, *args: Any):
            return None

        async def fetchval(self, sql: str, *args: Any):
            return 1

        async def execute(self, sql: str, *args: Any):
            return "OK"

    enrollment = _enrollment()

    async def fake_auth(plaintext: str):
        return enrollment

    monkeypatch.setattr(teams_mod, "authenticate_leader", fake_auth)
    monkeypatch.setattr(routes_team_leader, "db", _DB())
    app = FastAPI()
    app.include_router(routes_team_leader.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/team_leader/usage?limit=3", headers=_h())
    assert r.status_code == 200
    assert "l.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = $1)" in captured["sql"]
    assert captured["args"][0] == EID


def test_usage_group_by_and_csv(env) -> None:
    r = env.get("/team_leader/usage?group_by=model", headers=_h())
    assert r.status_code == 200
    assert r.json()[0]["model"] == "m1"
    bad = env.get("/team_leader/usage?group_by=nope", headers=_h())
    assert bad.status_code == 400
    csv_r = env.get("/team_leader/usage?format=csv", headers=_h())
    assert csv_r.status_code == 200
    assert "text/csv" in csv_r.headers["content-type"]


def test_usage_summary_scoped(env) -> None:
    env.fake.summary_totals = {"requests": 7, "cost_usd": 1.5}  # type: ignore[attr-defined]
    r = env.get("/team_leader/usage/summary", headers=_h())
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["totals"] == {"requests": 7, "cost_usd": 1.5}
    assert d["window"]["default_days"] == 30
    # The team console renders the admin's chargeback table, so the payload
    # must carry the window metadata that labels its widest option from REAL
    # retention (2026-10-30: the TL page had a bare rollup with no window).
    assert d["window"]["max_days"] == 366
    assert "oldest_ts" in d["window"]


def test_usage_summary_selects_the_billed_columns() -> None:
    """Drift guard (2026-10-30): the team-leader usage tab now shows the SAME
    three tables as /admin/usage — totals, by model, by key — including the
    BILLED columns (tokens/cost x the billing fraction in force, i.e. the
    off-peak discount view). Totals had them; by_model and by_key did not, so
    a leader saw raw cost where the admin saw billed cost. All three
    aggregates must select them."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src/pcai_llm/api/routes_team_leader.py"
    body = src.read_text(encoding="utf-8")
    body = body[body.index("async def team_usage_summary") : body.index("async def team_traces")]
    assert body.count("AS billed_tokens") == 3, "totals + by_model + by_key must all select it"
    assert body.count("AS billable_tokens") == 2, "by_model + by_key carry the per-row budget number"
    assert "_billable_tokens_sql" in body, "the budget expression is imported, never re-spelled"
    helper = src.read_text(encoding="utf-8")
    assert "_budget_token_value(alias)" in helper and "_COSTED_SQL" in helper, (
        "the helper delegates to the gate's expression AND its costed-status set "
        "(_COSTED_SQL) — one number, one money predicate, everywhere"
    )
    assert body.count("AS billed_cost_usd") == 3, "totals + by_model + by_key must all select it"


def test_traces_list_passes_enrollment_scope(env, monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.core.traces as traces_mod

    seen: dict[str, Any] = {}

    async def fake_recent(**kw: Any):
        seen.update(kw)
        return [{"request_id": "r1", "steps": []}]

    monkeypatch.setattr(traces_mod, "recent", fake_recent)
    r = env.get("/team_leader/traces?limit=5", headers=_h())
    assert r.status_code == 200
    assert r.json()[0]["request_id"] == "r1"
    assert seen["enrollment_id"] == EID


def test_trace_detail_in_scope(env, monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.core.traces as traces_mod

    async def fake_get(request_id: str, enrollment_id: uuid.UUID | None = None):
        assert enrollment_id == EID
        return {"request_id": request_id, "steps": []}

    monkeypatch.setattr(traces_mod, "get", fake_get)
    r = env.get("/team_leader/traces/req-1", headers=_h())
    assert r.status_code == 200
    assert r.json()["request_id"] == "req-1"


def test_presented_models_delegates_to_the_admin_payload(env, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = {"models": [{"id": "m"}], "counts": {"presented": 1}}

    async def fake_payload() -> dict[str, Any]:
        return sentinel

    monkeypatch.setattr(routes_team_leader, "presented_models_payload", fake_payload)
    r = env.get("/team_leader/presented-models", headers=_h())
    assert r.status_code == 200
    assert r.json() == sentinel


def test_help_lists_the_surface(env) -> None:
    r = env.get("/team_leader/help", headers=_h())
    assert r.status_code == 200
    eps = "\n".join(r.json()["endpoints"])
    for frag in ("/team_leader/overview", "/team_leader/keys", "/team_leader/usage", "/team_leader/traces"):
        assert frag in eps


# ---------------------------------------------------------------------------
# wave 2 — the SPEND-AWARE envelope (the user's worked example)
# ---------------------------------------------------------------------------


def test_worked_example_twelve_seats_nine_million_spent(env) -> None:
    """THE user example, end-to-end through the route: 12M/month cap, 6
    seats of 1M allocated and 9M already SPENT (overflow let them exceed
    their allocations, so every seat's committed remainder is 0). Pool
    left = 3M -> seats 7-9 are each granted 1M and seat 10 is a 409 naming
    available 0."""
    env.enrollment.profile_id = None  # type: ignore[attr-defined]
    env.enrollment.budget_tokens = 12_000_000  # type: ignore[attr-defined]
    env.pool["cap_tokens"] = 12_000_000  # type: ignore[attr-defined]
    env.pool["spent_tokens"] = 9_000_000  # type: ignore[attr-defined]
    # six existing seats, fully consumed: committed_i = max(0, 1M - spent) = 0
    for _ in range(6):
        env.pool["committed"][uuid.uuid4()] = (0, 0.0)  # type: ignore[attr-defined]

    granted: list[str] = []
    for seat in range(3):
        r = env.post(
            "/team_leader/keys",
            json={"name": f"seat-{7 + seat}", "budget_tokens": 1_000_000},
            headers=_h(),
        )
        assert r.status_code == 201, r.text
        granted.append(r.json()["id"])
        # the granted seat's commitment now occupies the pool
        env.pool["committed"][uuid.UUID(granted[-1])] = (1_000_000, 0.0)  # type: ignore[attr-defined]

    # seat 10: the pool is fully committed
    r4 = env.post(
        "/team_leader/keys",
        json={"name": "seat-10", "budget_tokens": 1_000_000},
        headers=_h(),
    )
    assert r4.status_code == 409, r4.text
    detail = r4.json()["detail"]
    assert "cap 12000000" in detail, detail
    assert "spent 9000000" in detail, detail
    assert "committed to other keys 3000000" in detail, detail
    assert "available 0" in detail, detail
    assert "requested 1000000" in detail, detail
    # the refused mint burned NOTHING
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    api_key_inserts = [a for s, a in fake.fetchrows if "INSERT INTO api_keys" in s]
    assert len(api_key_inserts) == 3


def test_worked_example_usd_refusal_names_all_four_numbers(env) -> None:
    """Same invariant on the USD side: cap 50.0, spent 48.0, no commitments
    -> available 2.0; requesting 5.0 is a 409 with every number."""
    env.enrollment.profile_id = None  # type: ignore[attr-defined]
    env.pool["spent_usd"] = 48.0  # type: ignore[attr-defined]
    r = env.post(
        "/team_leader/keys",
        json={"name": "x", "budget_usd": 5.0},
        headers=_h(),
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "USD budget: cap 50.0" in detail, detail
    assert "spent 48.0" in detail, detail
    # an empty committed set renders as 0 (sum of no remainders)
    assert "committed to other keys 0" in detail, detail
    assert "available 2.0" in detail, detail
    assert "requested 5.0" in detail, detail


# ---------------------------------------------------------------------------
# wave 2 — PATCH /team_leader/settings (the leader's ONE knob)
# ---------------------------------------------------------------------------


def test_settings_toggles_overflow(env) -> None:
    r = env.patch("/team_leader/settings", json={"overflow_enabled": False}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json() == {"enrollment_id": str(EID), "overflow_enabled": False}
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    upd = next(a for s, a in fake.executed if "UPDATE enrollments SET overflow_enabled" in s)
    assert upd == (EID, False)
    # the gate's cached lookups were invalidated FOR this enrollment
    assert env.invalidated[-1] == EID  # type: ignore[attr-defined]
    # audited with the leader vocabulary
    call = env.audit.calls[-1]  # type: ignore[attr-defined]
    assert call["action"] == "team.settings.set"
    assert call["actor_source"] == "team-leader"
    assert call["actor_subject"] == "station-9"


def test_settings_toggle_reflected_in_next_overview(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: the toggle writes the row, the next authenticated
    overview serves the NEW flag (no stale cache, no stale read)."""
    import pcai_llm.core.teams as teams_mod

    holder = {"overflow": True}

    async def fresh_auth(plaintext: str):
        if plaintext != LEADER_KEY:
            return None
        return _enrollment(overflow_enabled=holder["overflow"])

    async def stamping_execute(sql: str, *args: Any):
        env.fake.executed.append((sql, args))  # type: ignore[attr-defined]
        if "UPDATE enrollments SET overflow_enabled" in sql:
            holder["overflow"] = bool(args[1])
        return "UPDATE 1"

    monkeypatch.setattr(teams_mod, "authenticate_leader", fresh_auth)
    monkeypatch.setattr(env.fake, "execute", stamping_execute)  # type: ignore[attr-defined]
    assert env.patch("/team_leader/settings", json={"overflow_enabled": False}, headers=_h()).status_code == 200
    r = env.get("/team_leader/overview", headers=_h())
    assert r.status_code == 200
    assert r.json()["budget"]["overflow_enabled"] is False


def test_settings_401_without_a_leader_key(env) -> None:
    r = env.patch("/team_leader/settings", json={"overflow_enabled": False})
    assert r.status_code == 401


def test_settings_cannot_touch_the_cap(env) -> None:
    """THE delegation boundary: the cap is admin-only. An extra field is a
    loud 422 (extra='forbid') and no write happens — a leader can never
    raise their own total through this surface."""
    r = env.patch(
        "/team_leader/settings",
        json={"overflow_enabled": True, "budget_tokens": 999_999_999},
        headers=_h(),
    )
    assert r.status_code == 422
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert not any("UPDATE enrollments" in s for s, _ in fake.executed)


def test_settings_unknown_field_refused(env) -> None:
    r = env.patch("/team_leader/settings", json={"overflow": True}, headers=_h())
    assert r.status_code == 422
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert not any("UPDATE enrollments" in s for s, _ in fake.executed)


# ---------------------------------------------------------------------------
# wave 2 — leader-key health stamps + the overview pool numbers
# ---------------------------------------------------------------------------


def test_leader_auth_stamps_usage(env) -> None:
    assert env.get("/team_leader/overview", headers=_h()).status_code == 200
    assert env.stamps == [EID]  # type: ignore[attr-defined]


def test_leader_auth_failure_never_stamps(env) -> None:
    env.get("/team_leader/overview", headers={"Authorization": "Bearer nope"})
    assert env.stamps == []  # type: ignore[attr-defined]


def test_overview_pool_numbers(env) -> None:
    """committed = SUM of the per-key remainders; available = the SAME
    helper the envelope check consults (card and check cannot disagree).
    cap 10M / $50, spent 3M / $10, committed 1.5M / $2.5."""
    env.enrollment.budget_tokens = 10_000_000  # type: ignore[attr-defined]
    env.pool["cap_tokens"], env.pool["spent_tokens"] = 10_000_000, 3_000_000  # type: ignore[attr-defined]
    env.pool["cap_usd"], env.pool["spent_usd"] = 50.0, 10.0  # type: ignore[attr-defined]
    env.pool["committed"] = {uuid.uuid4(): (1_000_000, 1.5), uuid.uuid4(): (500_000, 1.0)}  # type: ignore[attr-defined]
    r = env.get("/team_leader/overview", headers=_h())
    assert r.status_code == 200
    d = r.json()
    # the pre-wave blocks are untouched (additive only)
    assert d["allocated"] == {"tokens": 200_000, "usd": 0.0}
    assert d["spend"] == {"tokens": 3_000_000, "usd": 10.0}
    assert d["unallocated"] == {"tokens": 9_800_000, "usd": 50.0}
    # the NEW blocks
    assert d["committed"] == {"tokens": 1_500_000, "usd": 2.5}
    assert d["available"] == {"tokens": 5_500_000, "usd": 37.5}


def test_overview_available_null_when_no_cap(env) -> None:
    env.enrollment.budget_tokens = None  # type: ignore[attr-defined]
    env.enrollment.budget_usd = None  # type: ignore[attr-defined]
    env.pool["no_cap"] = True  # type: ignore[attr-defined]
    r = env.get("/team_leader/overview", headers=_h())
    assert r.status_code == 200
    d = r.json()
    assert d["committed"] == {"tokens": 0, "usd": 0.0}
    assert d["available"] == {"tokens": None, "usd": None}


def test_help_lists_settings(env) -> None:
    r = env.get("/team_leader/help", headers=_h())
    assert "PATCH  /team_leader/settings" in "\n".join(r.json()["endpoints"])


# ---------------------------------------------------------------------------
# THREAD B — the leader side of the top-up queue
# ---------------------------------------------------------------------------


def test_topup_request_created_201(env) -> None:
    r = env.post("/team_leader/topup-requests", json={"tokens": 5_000_000, "note": "grew the team"}, headers=_h())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "pending"
    assert d["requested_tokens"] == 5_000_000
    assert d["requested_usd"] is None
    assert d["note"] == "grew the team"
    assert d["id"] and d["created_at"]
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    # the ask is bound to THIS enrollment and carries the amounts verbatim
    ins = next(a for s, a in fake.fetchrows if "INSERT INTO enrollment_topup_requests" in s)
    assert ins[0] == EID and ins[1] == 5_000_000 and ins[2] is None
    # audited with the leader vocabulary, never key material
    call = env.audit.calls[-1]  # type: ignore[attr-defined]
    assert call["action"] == "team.topup.request"
    assert call["actor_source"] == "team-leader"
    assert call["actor_subject"] == "station-9"


def test_topup_request_needs_an_amount(env) -> None:
    r = env.post("/team_leader/topup-requests", json={"note": "just vibes"}, headers=_h())
    assert r.status_code == 400
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert not any("INSERT INTO enrollment_topup_requests" in s for s, _ in fake.fetchrows)


def test_topup_request_second_pending_409(env) -> None:
    """ONE open ask at a time: a second pending request for the enrollment
    is refused with a detail that says so."""
    env.fake.topup_pending = True  # type: ignore[attr-defined]
    r = env.post("/team_leader/topup-requests", json={"tokens": 1_000}, headers=_h())
    assert r.status_code == 409
    assert "pending" in r.json()["detail"]
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    assert not any("INSERT INTO enrollment_topup_requests" in s for s, _ in fake.fetchrows)


def test_topup_request_amount_and_note_validation(env) -> None:
    """Amounts are column-bounded exactly like the allocation fields (a
    request the cap column could not hold would 500 the admin's approve);
    the note is bounded at 500."""
    for bad in (
        {"tokens": 0},
        {"tokens": -5},
        {"tokens": 2**63},
        {"usd": 0},
        {"usd": -1.0},
        {"usd": 200_000_000.0},
        {"tokens": 100, "note": "x" * 501},
    ):
        r = env.post("/team_leader/topup-requests", json=bad, headers=_h())
        assert r.status_code == 422, bad


def test_topup_list_scoped_and_bounded(env) -> None:
    """The queue read is scoped IN THE WHERE (another enrollment's asks
    never enter the query) and bounded at 50."""
    env.fake.topup_rows = [{"id": "b-newest", "status": "pending"}, {"id": "a-older", "status": "denied"}]  # type: ignore[attr-defined]
    r = env.get("/team_leader/topup-requests", headers=_h())
    assert r.status_code == 200
    assert [row["id"] for row in r.json()] == ["b-newest", "a-older"]
    fake: TeamFakeDB = env.fake  # type: ignore[attr-defined]
    list_sqls = [s for s in fake.fetch_sqls if "FROM enrollment_topup_requests" in s]
    assert list_sqls, "the list must query the queue"
    sql = list_sqls[-1]
    assert "WHERE enrollment_id = $1" in sql, sql
    assert "LIMIT 50" in sql, sql
