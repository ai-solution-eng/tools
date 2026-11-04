"""Team leader tier — core/teams.py + the budget gate's envelope branch.

Fake-db style (the suite convention: tests/test_enroll.py, tests/test_budgets.py):
the module-level db handle of each module under test is replaced with a
STATEFUL fake that dispatches on SQL shape and MODELS the predicates the real
statements carry (revoked/expired leader keys, the ledger's status filter, the
period window). The tests then pin the CONTRACT — which rows are visible and
what the gate decides — instead of a SQL string, except where only the
database can enforce a rule: those predicates are asserted on the captured
statement text (the "SQL invariant" assertions), because a fail-closed
predicate that exists only in Python is a predicate an attacker can outrun.

Coverage map (contract):
  * authenticate_leader: hit / miss / revoked leader key / revoked enrollment /
    expired enrollment (fail closed, all four kill switches in SQL);
  * mint + revoke: hash-only storage, idempotent revoke, re-mint un-revokes;
  * enrollment_for_key: positive AND negative caching, TTL, invalidate(),
    a DB error is never cached;
  * team_spend: the SAME rows the per-key gate counts (status filter, window,
    fraction weighting, the cache-discounted token expression);
  * check_budgets: non-team unchanged, under allocation, overflow admitted,
    overflow refused, envelope exhausted, no-own-rows still gated, no envelope
    unchanged, plus the tolerant seam and the non-finite guards;
  * list_keys: the enrollment projection and both filters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import keys as keys_mod
from pcai_llm.core import teams as teams_mod
from pcai_llm.core.budgets import _window_start
from pcai_llm.core.teams import TEAM_PERIODS, KeyAllocation, TeamEnrollment

# ---------------------------------------------------------------------------
# fake db seams
# ---------------------------------------------------------------------------


class TeamFakeDB:
    """Stateful seam for core/teams.py.

    Models the SQL it is asked for: the four leader-key kill switches, the
    api_keys -> enrollments join, the two UPDATEs, and the team aggregates.
    spend_override lets the GATE tests pin the envelope's answer without
    building a ledger (the ledger math has its own test).
    """

    def __init__(self) -> None:
        self.enrollments: dict[uuid.UUID, dict[str, Any]] = {}
        self.key_enrollment: dict[uuid.UUID, uuid.UUID] = {}
        self.ledger: list[dict[str, Any]] = []
        # Per-key budget rows (the ALLOCATIONS). Model-specific rows are
        # separate entries on purpose: team_allocation_state must SUM them.
        self.budget_rows: list[dict[str, Any]] = []
        self.cost_rows: list[dict[str, Any]] = []
        self.allocated: tuple[int, float] = (0, 0.0)
        self.spend_override: tuple[int, float] | None = None
        self.spend_calls: list[tuple[str, tuple]] = []
        # team_cap_spend (the PER-MODEL scopes): override keyed by
        # (model, WINDOW START datetime) with the model '*' meaning the pooled
        # scope. The key is the window the statement will be called with (see
        # _window() below), NEVER a period inferred from it: on a MONDAY the
        # day-start IS the week-start and on the 1st it IS the month-start, so
        # an inferred label collides and a miss silently reads (0, 0.0) — the
        # false-green generator audit W6 #5 found.
        self.cap_spend_override: dict[tuple[str, datetime], tuple[int, float]] | None = None
        self.cap_spend_calls: list[tuple[str, tuple]] = []
        # Revoked seats (owner decision P2-7): the promise set excludes them, and
        # the fake applies that ONLY when the statement carries the predicate —
        # otherwise a pin could not tell the fix from the fake being nice.
        self.revoked_keys: set[uuid.UUID] = set()
        # team_scope_available: per-(model, period) allocation/spend rows.
        self.scope_calls: list[tuple[str, tuple]] = []
        self.allocated_calls: list[tuple] = []
        self.allocated_sql: list[str] = []
        self.allocation_calls: list[tuple] = []
        self.fetchrow_sql: list[str] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.stamp_writes = 0
        self.stamp_skipped = 0
        self.lookups = 0  # enrollment_for_key round-trips (cache misses)
        self.leader_calls = 0
        self.fail_lookups = 0  # raise on the next N enrollment lookups

    # -- helpers ---------------------------------------------------------
    def _leader_row(self, key_hash: str) -> dict[str, Any] | None:
        """The leader-key SELECT, predicates included (fail closed)."""
        now = datetime.now(UTC)
        for row in self.enrollments.values():
            if row.get("team_leader_key_hash") != key_hash:
                continue
            if row.get("team_leader_key_revoked_at") is not None:
                continue  # a revoked leader key authorizes nothing
            if row.get("revoked_at") is not None:
                continue  # a closed team closes its console
            expires = row.get("expires_at")
            if expires is not None and expires <= now:
                continue  # an expired enrollment closes it too
            return dict(row)
        return None

    def _mint(self, enrollment_id: uuid.UUID, key_hash: str, prefix: str, sql: str = "") -> Any:
        row = self.enrollments.get(enrollment_id)
        if row is None:
            return None
        row["team_leader_key_hash"] = key_hash
        row["team_leader_key_prefix"] = prefix
        row["team_leader_key_created_at"] = datetime.now(UTC)
        row["team_leader_key_revoked_at"] = None
        # MODELS the statement: the stamp reset lands only when the SQL carries
        # it, so a pin can tell "rotation clears the dead credential's use" from
        # "the fake cleared it for us" (audit W6 F3). Whitespace is normalised —
        # the SET list is column-aligned, so the spacing is not semantic.
        if "team_leader_key_last_used_at = NULL" in re.sub(r"\s+", " ", sql):
            row["team_leader_key_last_used_at"] = None
        return {"id": enrollment_id}

    def _spend_for_key(self, key_id: uuid.UUID, start: datetime) -> dict[str, float]:
        """Reference implementation of the ledger SUM for ONE key: the status
        filter, the window, the fraction weight and the cache discount, all
        the predicates the real SQL carries."""
        discount = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
        tokens = 0.0
        usd = 0.0
        for r in self.ledger:
            if r["key_id"] != key_id:
                continue
            if r["status"] not in budgets_mod._COSTED_STATUSES:
                continue  # cache_hit rows are FREE; guardrail_blocked rows are real spend
            if r["ts"] < start:
                continue
            total = int(r["total_tokens"])
            cached = min(int(r["cached_tokens"]), total)
            billable = max(total - cached * discount, 0)
            tokens += billable * float(r["billing_fraction"])
            usd += float(r["cost_usd"] or 0.0) * float(r["billing_fraction"])
        return {"tokens": tokens, "usd": usd}

    def _spend(self, enrollment_id: uuid.UUID, start: datetime) -> dict[str, float]:
        """team_spend's aggregate: every key of the team, same per-key math."""
        tokens = 0.0
        usd = 0.0
        for key_id, eid in self.key_enrollment.items():
            if eid != enrollment_id:
                continue
            one = self._spend_for_key(key_id, start)
            tokens += one["tokens"]
            usd += one["usd"]
        return {"tokens": tokens, "usd": usd}

    def _allocation_state(
        self, enrollment_id: uuid.UUID, period: str, start: datetime, *, live_only: bool = False
    ) -> list[dict[str, Any]]:
        """team_allocation_state's one statement, row by row: EVERY key of the
        team (no ledger/budget row needed to appear), allocations summed per
        period, spend from the shared ledger math."""
        out: list[dict[str, Any]] = []
        for key_id, eid in self.key_enrollment.items():
            if eid != enrollment_id:
                continue
            if live_only and key_id in self.revoked_keys:
                continue  # a revoked seat promises nothing (owner decision P2-7)
            alloc_tokens = sum(
                int(r["limit_tokens"]) for r in self.budget_rows if r["key_id"] == key_id and r["period"] == period
            )
            alloc_usd = sum(
                float(r["limit_usd"]) for r in self.cost_rows if r["key_id"] == key_id and r["period"] == period
            )
            spend = self._spend_for_key(key_id, start)
            out.append(
                {
                    "key_id": key_id,
                    "alloc_tokens": alloc_tokens,
                    "alloc_usd": alloc_usd,
                    "spent_tokens": spend["tokens"],
                    "spent_usd": spend["usd"],
                }
            )
        return out

    # -- asyncpg surface ------------------------------------------------
    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.fetchrow_sql.append(sql)
        if "FROM enrollments e" in sql and "WHERE e.id = $1" in sql:
            row = self.enrollments.get(a[0])
            return dict(row) if row is not None else None
        if "team_leader_key_hash = $1" in sql:
            self.leader_calls += 1
            return self._leader_row(a[0])
        if "JOIN enrollments e ON e.id = k.enrollment_id" in sql:
            self.lookups += 1
            if self.fail_lookups > 0:
                self.fail_lookups -= 1
                raise RuntimeError("enrollment lookup unavailable")
            enrollment_id = self.key_enrollment.get(a[0])
            row = self.enrollments.get(enrollment_id) if enrollment_id is not None else None
            return dict(row) if row is not None else None
        if "UPDATE enrollments" in sql:
            return self._mint(a[0], a[1], a[2], sql)
        if "FILTER (WHERE" in sql and "FROM usage_ledger l" in sql:
            self.cap_spend_calls.append((sql, a))
            # one FILTER clause per AGGREGATE (tokens then usd share a cond):
            # every second one is a scope, (window, optional model).
            pattern = "FILTER [(]WHERE l[.]ts >= [$]([0-9]+)::timestamptz(?: AND l[.]model = [$]([0-9]+))?"
            conds = re.findall(pattern, sql)
            scopes = conds[0::2]

            def _sum(eid: uuid.UUID, window: datetime, model: str) -> tuple[float, float]:
                discount = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
                tokens = usd = 0.0
                for key_id, eid2 in self.key_enrollment.items():
                    if eid2 != eid:
                        continue
                    for r in self.ledger:
                        if r["key_id"] != key_id or r["status"] not in budgets_mod._COSTED_STATUSES:
                            continue
                        if r["ts"] < window:
                            continue
                        if model != "*" and r.get("model", "*") != model:
                            continue
                        total = int(r["total_tokens"])
                        cached = min(int(r["cached_tokens"]), total)
                        tokens += max(total - cached * discount, 0) * float(r["billing_fraction"])
                        usd += float(r["cost_usd"] or 0.0) * float(r["billing_fraction"])
                return tokens, usd

            out: dict[str, float] = {}
            for i, (w_idx, m_idx) in enumerate(scopes):
                window = a[int(w_idx) - 1]
                model = a[int(m_idx) - 1] if m_idx else "*"
                tokens = usd = 0.0
                if self.cap_spend_override is not None:
                    # KEYED BY THE WINDOW ITSELF (audit W6 #5): no inference, so
                    # a day and a month scope can never be confused.
                    ov = self.cap_spend_override.get((model, window), (0, 0.0))
                    tokens, usd = float(ov[0]), float(ov[1])
                else:
                    tokens, usd = _sum(a[0], window, model)
                out[f"s{i}_tokens"] = float(tokens)
                out[f"s{i}_usd"] = float(usd)
            return out
        if "FROM usage_ledger l" in sql:
            self.spend_calls.append((sql, a))
            if self.spend_override is not None:
                return {"tokens": float(self.spend_override[0]), "usd": float(self.spend_override[1])}
            return self._spend(a[0], a[1])
        if "FROM budgets b" in sql:
            self.allocated_calls.append(a)
            self.allocated_sql.append(sql)
            return {"tokens": float(self.allocated[0]), "usd": float(self.allocated[1])}
        return None

    async def fetch(self, sql: str, *a: Any) -> Any:
        if "SUM(limit_tokens) AS alloc_tokens" in sql:
            # team_scope_available: per-key allocation (model-specific rows plus
            # any-model '*' rows) and per-key spend for ONE scope.
            self.scope_calls.append((sql, a))
            eid, period, model, start = a[0], a[1], a[2], a[3]
            discount = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
            rows: list[dict[str, Any]] = []
            live_only = "k.revoked_at IS NULL" in sql  # MODELS the predicate
            for kid, eid2 in self.key_enrollment.items():
                if eid2 != eid:
                    continue
                if live_only and kid in self.revoked_keys:
                    continue  # a revoked seat promises nothing (owner decision P2-7)
                alloc_t = sum(
                    int(r["limit_tokens"])
                    for r in self.budget_rows
                    if r["key_id"] == kid and r["period"] == period and (r["model"] == model or r["model"] == "*")
                )
                alloc_u = sum(
                    float(r["limit_usd"])
                    for r in self.cost_rows
                    if r["key_id"] == kid and r["period"] == period and (r["model"] == model or r["model"] == "*")
                )
                spent_t = spent_u = 0.0
                for r in self.ledger:
                    if r["key_id"] != kid or r["status"] not in budgets_mod._COSTED_STATUSES:
                        continue
                    if r["ts"] < start:
                        continue
                    if model != "*" and r.get("model", "*") != model:
                        continue
                    total = int(r["total_tokens"])
                    cached = min(int(r["cached_tokens"]), total)
                    spent_t += max(total - cached * discount, 0) * float(r["billing_fraction"])
                    spent_u += float(r["cost_usd"] or 0.0) * float(r["billing_fraction"])
                rows.append(
                    {
                        "key_id": kid,
                        "alloc_tokens": alloc_t,
                        "alloc_usd": alloc_u,
                        "spent_tokens": spent_t,
                        "spent_usd": spent_u,
                    }
                )
            return rows
        # ORDER MATTERS: the allocation-state statement embeds the same
        # "SELECT id FROM api_keys WHERE enrollment_id = $1" subselect that
        # key_ids_in_enrollment issues, so its own marker must be tested first.
        if "alloc_tokens" in sql and "FROM api_keys k" in sql:
            self.allocation_calls.append((sql, a))
            return self._allocation_state(a[0], a[1], a[2], live_only="k.revoked_at IS NULL" in sql)
        if "SELECT id FROM api_keys WHERE enrollment_id" in sql:
            return [{"id": kid} for kid, eid in self.key_enrollment.items() if eid == a[0]]
        return []

    async def fetchval(self, sql: str, *a: Any) -> Any:
        return None

    async def execute(self, sql: str, *a: Any) -> str:
        self.execute_calls.append((sql, a))
        if "team_leader_key_last_used_at = now()" in sql:
            row = self.enrollments.get(a[0])
            if row is None:
                return "UPDATE 0"
            interval = a[1]  # a timedelta: the SQL casts $2 to ::interval
            last = row.get("team_leader_key_last_used_at")
            now = datetime.now(UTC)
            if last is not None and last > now - interval:
                self.stamp_skipped += 1
                return "UPDATE 0"  # the throttle predicate matched no row
            row["team_leader_key_last_used_at"] = now
            self.stamp_writes += 1
            return "UPDATE 1"
        if "team_leader_key_revoked_at = now()" in sql:
            row = self.enrollments.get(a[0])
            if (
                row is None
                or row.get("team_leader_key_hash") is None
                or row.get("team_leader_key_revoked_at") is not None
            ):
                return "UPDATE 0"
            row["team_leader_key_revoked_at"] = datetime.now(UTC)
            return "UPDATE 1"
        return "UPDATE 0"


class OwnBudgetFakeDB:
    """The per-key budget seam check_budgets already uses (probe + two
    windowed ledger SUMs + the lineage PK lookup). queries records the SHAPE
    order so the non-team path can be pinned byte-for-byte."""

    def __init__(
        self,
        token_rows: list[dict[str, Any]] | None = None,
        usd_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.token_rows = token_rows or []
        self.usd_rows = usd_rows or []
        self.queries: list[str] = []

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self.queries.append("probe")
        if "SELECT EXISTS" in sql:
            rows = self.token_rows if "FROM budgets" in sql else self.usd_rows
            return bool(rows)
        return None

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.queries.append("lineage")
        return None  # no rotation lineage — the normal key

    async def fetch(self, sql: str, *a: Any) -> Any:
        self.queries.append("sum")
        if "FROM budgets" in sql:
            return list(self.token_rows)
        if "FROM cost_budgets" in sql:
            return list(self.usd_rows)
        return []


class KeysFakeDB:
    """Seam for core.keys.list_keys / create_key (captures the statement)."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.sql = ""
        self.args: tuple = ()

    async def fetch(self, sql: str, *a: Any) -> Any:
        self.sql, self.args = sql, a
        return list(self.rows)

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.sql, self.args = sql, a
        return {"id": uuid.uuid4()}


# ---------------------------------------------------------------------------
# row builders + fixtures
# ---------------------------------------------------------------------------


def _enrollment_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "label": "team-a",
        "tenant_id": uuid.uuid4(),
        "profile_id": None,
        "budget_tokens": None,
        "budget_usd": None,
        "budget_period": "month",
        "overflow_enabled": True,
        "team_leader_key_hash": None,
        "team_leader_key_prefix": "",
        "team_leader_key_created_at": None,
        "team_leader_key_revoked_at": None,
        "revoked_at": None,
        "expires_at": None,
    }
    row.update(over)
    return row


def _window(period: str) -> datetime:
    """The window start team_cap_spend passes for `period` — the key
    cap_spend_override is keyed by (audit W6 #5). Call it immediately before the
    gate call so the two reads cannot straddle a window boundary."""
    return _window_start(datetime.now(UTC), period)


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, when: datetime) -> dict[str, datetime]:
    """Pin "now" for the team-side windows and return the three window starts.

    A test whose premise needs a DAY window and a MONTH window to be DIFFERENT
    instants cannot be calendar-independent otherwise: on the 1st of a month they
    ARE the same instant, and one ledger row legitimately satisfies both — so
    the fixture (which keys by window) can only report one number for the pair.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:  # typeshed pins Self; a frozen clock is not one
            return when if tz is None else when.astimezone(tz)

    monkeypatch.setattr(teams_mod, "datetime", _Frozen)
    monkeypatch.setattr(budgets_mod, "datetime", _Frozen)
    return {p: _window_start(when, p) for p in ("day", "week", "month")}


def _token_row(limit: int, spent: int, period: str = "month") -> dict[str, Any]:
    return {"model": "*", "period": period, "limit_tokens": limit, "spent": spent}


def _usd_row(limit_usd: float, spent_usd: float, period: str = "month") -> dict[str, Any]:
    return {"model": "*", "period": period, "limit_usd": limit_usd, "spent": spent_usd}


@pytest.fixture(autouse=True)
def _clean_team_cache():
    """The enrollment cache is process-global: every test starts and ends
    with an empty one (a leaked positive would mask a lookup)."""
    teams_mod.invalidate()
    yield
    teams_mod.invalidate()


def _install_team(monkeypatch: pytest.MonkeyPatch, **over: Any) -> tuple[TeamFakeDB, dict[str, Any]]:
    fake = TeamFakeDB()
    row = _enrollment_row(**over)
    fake.enrollments[row["id"]] = row
    monkeypatch.setattr(teams_mod, "db", fake)
    return fake, row


def _join(fake: TeamFakeDB, row: dict[str, Any], key_id: uuid.UUID) -> uuid.UUID:
    fake.key_enrollment[key_id] = row["id"]
    return key_id


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    """Install BOTH seams check_budgets touches and hand back a builder.

    budgets.db is the per-key path's seam; teams.db is the envelope's. They
    are separate module handles in production too, so a test that patched only
    one would be exercising a shape that cannot exist.
    """

    def _install(
        *,
        token_rows: list[dict[str, Any]] | None = None,
        usd_rows: list[dict[str, Any]] | None = None,
        enrollment: dict[str, Any] | None = None,
        spend: tuple[int, float] | None = None,
        cap_spend: dict[tuple[str, datetime], tuple[int, float]] | None = None,
    ) -> tuple[OwnBudgetFakeDB, TeamFakeDB]:
        own = OwnBudgetFakeDB(token_rows=token_rows, usd_rows=usd_rows)
        team = TeamFakeDB()
        if enrollment is not None:
            team.enrollments[enrollment["id"]] = enrollment
        team.spend_override = spend
        team.cap_spend_override = cap_spend
        monkeypatch.setattr(budgets_mod, "db", own)
        monkeypatch.setattr(teams_mod, "db", team)
        return own, team

    return _install


# ---------------------------------------------------------------------------
# authenticate_leader — fail closed on all four kill switches
# ---------------------------------------------------------------------------


def test_authenticate_leader_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    plaintext = "hllm-leader-secret"
    fake, row = _install_team(
        monkeypatch,
        team_leader_key_hash=keys_mod.hash_key(plaintext),
        budget_tokens=5_000,
        budget_usd=12.5,
        budget_period="week",
        overflow_enabled=False,
    )
    team = asyncio.run(teams_mod.authenticate_leader(plaintext))
    assert isinstance(team, TeamEnrollment)
    assert team.id == row["id"] and team.label == "team-a"
    assert (team.budget_tokens, team.budget_usd) == (5_000, 12.5)
    assert team.budget_period == "week" and team.overflow_enabled is False
    assert team.has_budget is True
    # the HASH is what is looked up — the plaintext never reaches the query
    assert fake.leader_calls == 1
    assert keys_mod.hash_key(plaintext) not in fake.fetchrow_sql[0]
    assert plaintext not in fake.fetchrow_sql[0]


def test_authenticate_leader_miss_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_team(monkeypatch, team_leader_key_hash=keys_mod.hash_key("the-real-key"))
    assert asyncio.run(teams_mod.authenticate_leader("not-the-key")) is None


def test_authenticate_leader_empty_plaintext_never_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing bearer must not become a hash-of-empty lookup (and must not
    match an enrollment whose hash is somehow the empty-string digest)."""
    fake, _ = _install_team(monkeypatch, team_leader_key_hash=keys_mod.hash_key(""))
    assert asyncio.run(teams_mod.authenticate_leader("")) is None
    assert fake.leader_calls == 0


def test_authenticate_leader_revoked_leader_key(monkeypatch: pytest.MonkeyPatch) -> None:
    plaintext = "hllm-rotated-away"
    _install_team(
        monkeypatch,
        team_leader_key_hash=keys_mod.hash_key(plaintext),
        team_leader_key_revoked_at=datetime.now(UTC),
    )
    assert asyncio.run(teams_mod.authenticate_leader(plaintext)) is None


def test_authenticate_leader_revoked_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closing the team closes its console, even with a live leader key."""
    plaintext = "hllm-still-hashed"
    _install_team(
        monkeypatch,
        team_leader_key_hash=keys_mod.hash_key(plaintext),
        revoked_at=datetime.now(UTC),
    )
    assert asyncio.run(teams_mod.authenticate_leader(plaintext)) is None


def test_authenticate_leader_expired_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    plaintext = "hllm-expired"
    _fake, row = _install_team(
        monkeypatch,
        team_leader_key_hash=keys_mod.hash_key(plaintext),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    assert asyncio.run(teams_mod.authenticate_leader(plaintext)) is None
    # one minute of future is still live: expiry is a real comparison, not a
    # blanket refusal for every row that carries the column
    row["expires_at"] = datetime.now(UTC) + timedelta(minutes=5)
    assert asyncio.run(teams_mod.authenticate_leader(plaintext)) is not None


def test_authenticate_leader_sql_carries_every_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The four predicates must live in SQL: a revoked key/team or an expired
    enrollment that is filtered in Python only would still be LOADED — and any
    later code path holding the row could authorize from it."""
    fake, _ = _install_team(monkeypatch, team_leader_key_hash=keys_mod.hash_key("k"))
    asyncio.run(teams_mod.authenticate_leader("k"))
    sql = fake.fetchrow_sql[0]
    assert "team_leader_key_hash = $1" in sql
    assert "team_leader_key_revoked_at IS NULL" in sql
    assert "e.revoked_at IS NULL" in sql, "the ENROLLMENT's own revoked_at, not the key's"
    assert "expires_at IS NULL OR" in sql and "> now()" in sql


# ---------------------------------------------------------------------------
# mint / revoke — hash-only storage, idempotent revoke, re-mint un-revokes
# ---------------------------------------------------------------------------


def test_mint_leader_key_stores_only_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake, row = _install_team(monkeypatch)
    plaintext, prefix = asyncio.run(teams_mod.mint_leader_key(row["id"]))
    assert plaintext.startswith("hllm-")
    assert prefix == plaintext[:10]
    assert row["team_leader_key_prefix"] == prefix
    assert row["team_leader_key_hash"] == keys_mod.hash_key(plaintext)
    assert row["team_leader_key_created_at"] is not None
    # the plaintext is nowhere in the stored row (the prefix is a 10-char
    # slice of it, so only the FULL string matters here)
    assert plaintext not in json.dumps(row, default=str)
    # and the freshly minted key authenticates
    assert asyncio.run(teams_mod.authenticate_leader(plaintext)) is not None


def test_mint_leader_key_unknown_enrollment_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TeamFakeDB()
    monkeypatch.setattr(teams_mod, "db", fake)
    with pytest.raises(LookupError):
        asyncio.run(teams_mod.mint_leader_key(uuid.uuid4()))


def test_revoke_leader_key_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake, row = _install_team(monkeypatch)
    plaintext, _ = asyncio.run(teams_mod.mint_leader_key(row["id"]))
    assert asyncio.run(teams_mod.revoke_leader_key(row["id"])) is True
    stamp = row["team_leader_key_revoked_at"]
    assert stamp is not None
    # the credential is dead immediately ...
    assert asyncio.run(teams_mod.authenticate_leader(plaintext)) is None
    # ... and a retry is a no-op, not an error and not a re-stamp
    assert asyncio.run(teams_mod.revoke_leader_key(row["id"])) is False
    assert row["team_leader_key_revoked_at"] == stamp
    # the audit trail survives the revoke
    assert row["team_leader_key_hash"] is not None
    assert row["team_leader_key_prefix"] == plaintext[:10]


def test_revoke_without_a_leader_key_answers_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _, row = _install_team(monkeypatch)
    assert asyncio.run(teams_mod.revoke_leader_key(row["id"])) is False


def test_remint_clears_the_revocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-mint IS the rotate: the old plaintext dies, the row is live again."""
    _fake, row = _install_team(monkeypatch)
    first, _ = asyncio.run(teams_mod.mint_leader_key(row["id"]))
    asyncio.run(teams_mod.revoke_leader_key(row["id"]))
    second, _ = asyncio.run(teams_mod.mint_leader_key(row["id"]))
    assert second != first
    assert row["team_leader_key_revoked_at"] is None
    assert row["team_leader_key_hash"] == keys_mod.hash_key(second)
    assert asyncio.run(teams_mod.authenticate_leader(second)) is not None
    assert asyncio.run(teams_mod.authenticate_leader(first)) is None


def test_mint_accepts_a_caller_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """The admin mint-with-enrollment path writes the code and the leader key
    in ONE transaction: mint_leader_key must run on the caller's connection."""

    class Conn:
        def __init__(self) -> None:
            self.calls = 0

        async def fetchrow(self, sql: str, *a: Any) -> Any:
            self.calls += 1
            return {"id": a[0]}

    conn = Conn()
    plaintext, prefix = asyncio.run(teams_mod.mint_leader_key(uuid.uuid4(), conn=conn))
    assert conn.calls == 1 and plaintext.startswith("hllm-") and prefix == plaintext[:10]


# ---------------------------------------------------------------------------
# enrollment_for_key — caching, negative caching, TTL, invalidate
# ---------------------------------------------------------------------------


def test_enrollment_for_key_is_cached_then_invalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch, budget_tokens=1_000)
    kid = _join(fake, row, uuid.uuid4())
    first = asyncio.run(teams_mod.enrollment_for_key(kid))
    second = asyncio.run(teams_mod.enrollment_for_key(kid))
    assert first is not None and first is second
    assert fake.lookups == 1, "the second read must be served from the cache"
    teams_mod.invalidate(row["id"])
    assert asyncio.run(teams_mod.enrollment_for_key(kid)) is not None
    assert fake.lookups == 2


def test_enrollment_for_key_caches_a_negative_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TeamFakeDB()
    monkeypatch.setattr(teams_mod, "db", fake)
    kid = uuid.uuid4()
    assert asyncio.run(teams_mod.enrollment_for_key(kid)) is None
    assert asyncio.run(teams_mod.enrollment_for_key(kid)) is None
    assert fake.lookups == 1, "most keys are in no team — the negative must be cached"


def test_invalidate_drops_negatives_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key that JOINS a team must not be served a stale no-team answer."""
    fake, row = _install_team(monkeypatch)
    kid = uuid.uuid4()
    assert asyncio.run(teams_mod.enrollment_for_key(kid)) is None  # cached negative
    _join(fake, row, kid)  # the key is enrolled AFTER the negative was cached
    teams_mod.invalidate(row["id"])
    team = asyncio.run(teams_mod.enrollment_for_key(kid))
    assert team is not None and team.id == row["id"]


def test_invalidate_without_an_id_clears_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch)
    kid = _join(fake, row, uuid.uuid4())
    asyncio.run(teams_mod.enrollment_for_key(kid))
    teams_mod.invalidate()
    assert teams_mod._CACHE == {}


def test_enrollment_cache_expires_after_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch)
    kid = _join(fake, row, uuid.uuid4())
    teams_mod.invalidate()
    monkeypatch.setattr(teams_mod, "_CACHE_TTL", -1.0)  # write already-expired entries
    asyncio.run(teams_mod.enrollment_for_key(kid))
    assert fake.lookups == 1
    asyncio.run(teams_mod.enrollment_for_key(kid))
    assert fake.lookups == 2, "an expired entry must be re-read"


def test_a_db_error_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch)
    kid = _join(fake, row, uuid.uuid4())
    fake.fail_lookups = 1
    with pytest.raises(RuntimeError):
        asyncio.run(teams_mod.enrollment_for_key(kid))
    assert asyncio.run(teams_mod.enrollment_for_key(kid)) is not None
    assert fake.lookups == 2, "the failed lookup must not have been memoized"


def test_enrollment_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TeamFakeDB()
    monkeypatch.setattr(teams_mod, "db", fake)
    monkeypatch.setattr(teams_mod, "_CACHE_MAX", 2)
    for _ in range(5):
        asyncio.run(teams_mod.enrollment_for_key(uuid.uuid4()))
    assert len(teams_mod._CACHE) <= 2


def test_enrollment_for_key_tolerates_a_partial_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A seam that answers with a row from some OTHER query (no label column)
    must read as no-team, never raise into the gate."""

    class OddFake(TeamFakeDB):
        async def fetchrow(self, sql: str, *a: Any) -> Any:
            return {"id": uuid.uuid4()}  # not an enrollment projection

    monkeypatch.setattr(teams_mod, "db", OddFake())
    assert asyncio.run(teams_mod.enrollment_for_key(uuid.uuid4())) is None


def test_key_ids_in_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch)
    k1, k2 = _join(fake, row, uuid.uuid4()), _join(fake, row, uuid.uuid4())
    _join(fake, _enrollment_row(), uuid.uuid4())  # another team
    ids = asyncio.run(teams_mod.key_ids_in_enrollment(row["id"]))
    assert set(ids) == {k1, k2}


def test_team_enrollment_without_caps_has_no_budget() -> None:
    team = TeamEnrollment(
        id=uuid.uuid4(),
        label="t",
        tenant_id=uuid.uuid4(),
        profile_id=None,
        budget_tokens=None,
        budget_usd=None,
        budget_period="month",
        overflow_enabled=True,
    )
    assert team.has_budget is False
    # 2026-11-02: quarter/year joined the budget periods (schema.py CHECK widened
    # the same day); hour stays out — it is the rate limiter's axis.
    assert TEAM_PERIODS == frozenset({"day", "week", "month", "quarter", "year"})


# ---------------------------------------------------------------------------
# team_spend / team_allocated — the same rows the per-key gate counts
# ---------------------------------------------------------------------------


def _ledger_row(
    key_id: uuid.UUID,
    *,
    status: str = "ok",
    total: int = 0,
    cached: int = 0,
    cost: float = 0.0,
    fraction: float = 1.0,
    ts: datetime | None = None,
    model: str = "*",
) -> dict[str, Any]:
    return {
        "key_id": key_id,
        "model": model,
        "status": status,
        "total_tokens": total,
        "cached_tokens": cached,
        "cost_usd": cost,
        "billing_fraction": fraction,
        "ts": ts or datetime.now(UTC),
    }


def test_team_spend_counts_exactly_what_the_per_key_gate_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """status filter, fraction weighting, the cache-discounted token SUM and
    the team scope — all four decide the number."""
    monkeypatch.setattr(settings, "budget_cached_exempt_fraction", 0.5)
    fake, row = _install_team(monkeypatch)
    eid = row["id"]
    k1, k2, k3, k_other, k_none = (uuid.uuid4() for _ in range(5))
    for kid in (k1, k2, k3):
        _join(fake, row, kid)
    other = _enrollment_row(label="team-b")
    fake.enrollments[other["id"]] = other
    _join(fake, other, k_other)
    start = _window_start(datetime.now(UTC), "month")
    fake.ledger = [
        # counted: ok, 1000 total with 400 cached at a 0.5 discount -> 800
        _ledger_row(k1, total=1_000, cached=400, cost=2.0, fraction=1.0, ts=start + timedelta(minutes=1)),
        # counted: error rows consume budget too; half-price fraction
        _ledger_row(k2, status="error", total=200, cost=1.0, fraction=0.5, ts=start + timedelta(minutes=2)),
        # NOT counted: a cache hit is free
        _ledger_row(k3, status="cache_hit", total=500, cost=9.0, ts=start + timedelta(minutes=3)),
        # NOT counted: another team / no team at all
        _ledger_row(k_other, total=999, cost=99.0, ts=start + timedelta(minutes=4)),
        _ledger_row(k_none, total=999, cost=99.0, ts=start + timedelta(minutes=5)),
        # NOT counted: before the window
        _ledger_row(k1, total=999, cost=99.0, ts=start - timedelta(minutes=1)),
    ]
    tokens, usd = asyncio.run(teams_mod.team_spend(eid, "month"))
    assert tokens == 900, "800 (ok, cache-discounted) + 100 (error, half-price fraction)"
    assert usd == 2.5, "2.0 * 1.0 + 1.0 * 0.5"
    sql, args = fake.spend_calls[0]
    assert args[0] == eid
    # the SQL invariants a fake cannot enforce: the status predicate, the team
    # scope, the window, and the SAME cache-discounted expression the gate uses
    # audit r5 #9: the status predicate comes from the SHARED set (imported
    # from core.budgets), so a post-call 'guardrail_blocked' row — real GPU
    # spend with real tokens — is counted by the team envelope too.
    assert f"l.status IN ({budgets_mod._COSTED_SQL})" in sql
    assert "guardrail_blocked" in sql
    assert "JOIN api_keys k ON k.id = l.key_id" in sql
    assert "k.enrollment_id = $1" in sql
    assert "l.ts >= $2::timestamptz" in sql
    assert "GREATEST(" in sql and "LEAST(" in sql, "the cache discount must ride the token SUM"
    assert "l.cost_usd * l.billing_fraction" in sql, "the USD SUM stays exact (no double discount)"


def test_team_spend_uses_the_period_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The window start handed to the aggregate is the SAME UTC window the
    per-key gate computes for that period (never the DB session's)."""
    fake, row = _install_team(monkeypatch)
    kid = _join(fake, row, uuid.uuid4())
    day_start = _window_start(datetime.now(UTC), "day")
    fake.ledger = [
        _ledger_row(kid, total=10, ts=day_start + timedelta(minutes=1)),
        _ledger_row(kid, total=1_000, ts=day_start - timedelta(minutes=1)),
    ]
    before = _window_start(datetime.now(UTC), "day")
    tokens, _ = asyncio.run(teams_mod.team_spend(row["id"], "day"))
    after = _window_start(datetime.now(UTC), "day")
    assert tokens == 10, "only the row inside the day window may count"
    assert fake.spend_calls[0][1][1] in (before, after)


def test_team_spend_unknown_period_falls_back_to_month(monkeypatch: pytest.MonkeyPatch) -> None:
    """budget_period has no CHECK constraint: junk normalizes to the schema
    default instead of meaning no window at all."""
    fake, row = _install_team(monkeypatch)
    asyncio.run(teams_mod.team_spend(row["id"], "fortnight"))
    month_start = _window_start(datetime.now(UTC), "month")
    assert fake.spend_calls[0][1][1] == month_start


def test_team_allocated_sums_the_per_key_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch)
    fake.allocated = (3_000, 12.5)
    assert asyncio.run(teams_mod.team_allocated(row["id"], "month")) == (3_000, 12.5)
    args = fake.allocated_calls[0]
    assert args[0] == row["id"] and args[1] == "month" and args[2] == "month"


# ---------------------------------------------------------------------------
# check_budgets — the envelope branch
# ---------------------------------------------------------------------------


def test_gate_non_team_key_refusal_is_byte_identical(gate) -> None:
    """(a) A key in no team takes the pre-feature path exactly: same refusal
    string, same query order, and the envelope is never read."""
    own, team = gate(token_rows=[_token_row(500, 800)])
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert res.ok is False
    assert res.reason == "month budget 500 exceeded (spent 800)"
    assert own.queries == ["probe", "lineage", "sum"], "the own path is unchanged"
    assert team.spend_calls == []


def test_gate_non_team_key_without_budget_rows_fires_only_the_probes(gate) -> None:
    own, team = gate()
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 100_000))
    assert res.ok is True and res.warn == 0
    assert own.queries == ["probe", "probe"]
    assert team.spend_calls == []


def test_gate_team_key_under_its_allocation_is_admitted(gate) -> None:
    """(b) Under the key's own allocation: admitted, envelope read, no warn."""
    enrollment = _enrollment_row(budget_tokens=10_000)
    _own, team = gate(
        token_rows=[_token_row(1_000, 100)],
        enrollment=enrollment,
        spend=(500, 0.0),
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is True and res.warn == 0
    assert len(team.spend_calls) == 1, "every team admission reads the envelope"


def test_gate_overflow_admits_past_the_key_allocation(gate) -> None:
    """(c) overflow_enabled=True: the unallocated remainder of the team budget
    is a shared pool, so an exhausted per-key allocation does NOT refuse."""
    enrollment = _enrollment_row(budget_tokens=10_000, overflow_enabled=True)
    _own, team = gate(
        token_rows=[_token_row(100, 200)],  # own allocation blown
        enrollment=enrollment,
        spend=(9_999, 0.0),  # the envelope has one token of headroom
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is True, "overflow must swallow the per-key refusal"
    # TWO aggregates now: the envelope check, then team_available's spend-aware
    # headroom (T - S - committed) on the rescue path — the price of never
    # overflowing into a teammate's promise (2026-10-30).
    assert len(team.spend_calls) == 2


def test_gate_overflow_off_refuses_at_the_key_allocation(gate) -> None:
    """(d) overflow_enabled=False: the per-key allocation is terminal, and the
    envelope is not even read (a hard cap costs no extra query)."""
    enrollment = _enrollment_row(budget_tokens=10_000, overflow_enabled=False)
    _own, team = gate(
        token_rows=[_token_row(100, 200)],
        enrollment=enrollment,
        spend=(0, 0.0),  # plenty of team headroom — irrelevant
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False
    assert res.reason == "month budget 100 exceeded (spent 200)"
    assert team.spend_calls == []


def test_gate_envelope_exhausted_refuses_every_team_key(gate) -> None:
    """(e) The envelope is a HARD cap: once spent >= limit, every key of the
    team is refused — including keys far below their own allocation."""
    enrollment = _enrollment_row(budget_tokens=1_000)
    _own, team = gate(
        token_rows=[_token_row(100_000, 10)],  # each key is nearly untouched
        enrollment=enrollment,
        spend=(1_000, 0.0),
    )
    for _ in range(3):
        kid = _join(team, enrollment, uuid.uuid4())
        res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
        assert res.ok is False
        assert res.reason == "team month budget 1000 exceeded (spent 1000)"


def test_gate_usd_envelope_exhausted_refuses(gate) -> None:
    enrollment = _enrollment_row(budget_usd=5.0)
    _own, team = gate(enrollment=enrollment, spend=(0, 5.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False
    assert res.reason == "team month cost budget $5.00 exceeded (spent $5.0000)"


def test_gate_envelope_refuses_on_the_period_it_was_configured_for(gate) -> None:
    enrollment = _enrollment_row(budget_tokens=1_000, budget_period="day")
    _own, team = gate(enrollment=enrollment, spend=(1_000, 0.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False and "team day budget" in res.reason


def test_gate_team_key_with_no_own_rows_is_still_gated_by_the_envelope(gate) -> None:
    """(f) The EXISTS short-circuit must not skip the team: a teammate with no
    allocation at all still draws from the envelope."""
    enrollment = _enrollment_row(budget_tokens=1_000)
    own, team = gate(enrollment=enrollment, spend=(1_000, 0.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False and "team month budget" in res.reason
    assert own.queries == ["probe", "probe"], "the own path still short-circuits"
    # and the same key is admitted while the envelope has headroom
    team.spend_override = (999, 0.0)
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is True


def test_gate_team_without_an_envelope_is_unchanged(gate) -> None:
    """(g) A team that configures no caps enforces nothing: the per-key
    verdict passes through untouched and the envelope is never read."""
    enrollment = _enrollment_row()  # both caps NULL
    _own, team = gate(
        token_rows=[_token_row(100, 200)],
        enrollment=enrollment,
        spend=(10**9, 10**9),  # would refuse everything if it were consulted
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False
    assert res.reason == "month budget 100 exceeded (spent 200)"
    assert team.spend_calls == []


def test_gate_team_warn_band_rides_through(gate) -> None:
    """The per-key warn band still surfaces on an admitted team call."""
    enrollment = _enrollment_row(budget_tokens=10_000)
    _own, team = gate(
        token_rows=[_token_row(1_000, 950)],
        enrollment=enrollment,
        spend=(0, 0.0),
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is True and res.warn == 95


def test_gate_survives_a_seam_without_a_team_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake seam that cannot answer the team lookup degrades to no-team —
    the per-key gate still decides, and nothing is raised."""
    monkeypatch.setattr(budgets_mod, "db", OwnBudgetFakeDB(token_rows=[_token_row(100, 200)]))
    monkeypatch.setattr(teams_mod, "db", object())  # no fetchrow at all
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert res.ok is False and res.reason == "month budget 100 exceeded (spent 200)"


def test_gate_survives_a_team_lookup_error(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A DB fault in the team lookup must not fail an admission (the envelope
    is skipped, the per-key caps still apply) and must be visible in the log."""
    fake = TeamFakeDB()
    fake.fail_lookups = 99
    monkeypatch.setattr(teams_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", OwnBudgetFakeDB(token_rows=[_token_row(100, 10)]))
    with caplog.at_level(logging.WARNING, logger=budgets_mod.log.name):
        res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert res.ok is True
    assert any("team budget lookup failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_gate_refuses_a_non_finite_usd_envelope(gate, bad: float) -> None:
    """Mirror of the USD gate's non-finite guard: spent >= nan is always
    False, so without this a NaN envelope would look configured and enforce
    nothing."""
    enrollment = _enrollment_row(budget_usd=bad)
    _own, team = gate(enrollment=enrollment, spend=(0, 1.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False and "non-finite" in res.reason


def test_gate_refuses_a_non_finite_usd_spend(gate) -> None:
    enrollment = _enrollment_row(budget_usd=5.0)
    _own, team = gate(enrollment=enrollment, spend=(0, float("nan")))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False and "non-finite" in res.reason


def test_gate_envelope_at_equality_is_exhausted(gate) -> None:
    """spent == limit is exhausted, exactly like the per-key token/USD gates."""
    enrollment = _enrollment_row(budget_tokens=1_000)
    _own, team = gate(enrollment=enrollment, spend=(1_000, 0.0))
    kid = _join(team, enrollment, uuid.uuid4())
    assert asyncio.run(budgets_mod.check_budgets(kid, "m1", 0)).ok is False
    team.spend_override = (999, 0.0)
    assert asyncio.run(budgets_mod.check_budgets(kid, "m1", 0)).ok is True


def test_gate_envelope_does_not_charge_the_admission_estimate(gate) -> None:
    """The envelope is post-paid: a huge estimate must not refuse a team call
    that the per-key gate admits (the estimate belongs to the per-key test)."""
    enrollment = _enrollment_row(budget_tokens=1_000)
    _own, team = gate(enrollment=enrollment, spend=(999, 0.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 10**6))
    assert res.ok is True, "the estimate applies to the own-budget test only"


# ---------------------------------------------------------------------------
# keys — enrollment projection + filters
# ---------------------------------------------------------------------------


def _keys_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "user_name": "alice",
        "name": "k",
        "key_prefix": "hllm-AA",
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "expires_at": None,
        "revoked_at": None,
        "created_at": "now",
        "relay_default": False,
        "stream_pcai_events": True,
        "mcp_admin": False,
        "bound_via": None,
        "bound_at": None,
        "subject": None,
        "subject_idp": None,
        "self_mint_enabled": False,
        "user_profile_id": None,
        "user_profile_name": None,
        "enrollment_id": None,
        "enrollment_label": None,
    }
    row.update(over)
    return row


def test_list_keys_projects_the_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    eid = uuid.uuid4()
    fake = KeysFakeDB([_keys_row(enrollment_id=eid, enrollment_label="team-a")])
    monkeypatch.setattr(keys_mod, "db", fake)
    rows = asyncio.run(keys_mod.list_keys())
    assert rows[0]["enrollment_id"] == eid
    assert rows[0]["enrollment_label"] == "team-a"
    # LEFT JOIN: a key outside any team must survive as NULL, never be dropped
    assert "LEFT JOIN enrollments e ON e.id = k.enrollment_id" in fake.sql
    assert "k.enrollment_id, e.label AS enrollment_label" in fake.sql


def test_list_keys_unteamed_row_reads_null(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeysFakeDB([_keys_row()])
    monkeypatch.setattr(keys_mod, "db", fake)
    row = asyncio.run(keys_mod.list_keys())[0]
    assert row["enrollment_id"] is None and row["enrollment_label"] is None


def test_list_keys_filters_by_enrollment_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeysFakeDB()
    monkeypatch.setattr(keys_mod, "db", fake)
    eid = uuid.uuid4()
    asyncio.run(keys_mod.list_keys(enrollment_id=eid))
    assert fake.args == (eid,)
    assert "k.enrollment_id = $1" in fake.sql


def test_list_keys_filters_by_enrollment_label_search(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeysFakeDB()
    monkeypatch.setattr(keys_mod, "db", fake)
    asyncio.run(keys_mod.list_keys(enrollment_search="  ACME  "))
    assert fake.args == ("ACME",), "the search term is trimmed and bound as a parameter"
    assert "e.label ILIKE '%' || $1 || '%'" in fake.sql
    assert "ACME" not in fake.sql, "the term must never be concatenated into the SQL"


def test_list_keys_ignores_an_empty_search(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeysFakeDB()
    monkeypatch.setattr(keys_mod, "db", fake)
    asyncio.run(keys_mod.list_keys(enrollment_search="   "))
    assert fake.args == ()
    assert "ILIKE" not in fake.sql, "a cleared search box must not hide every row"


def test_list_keys_caps_the_search_length(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeysFakeDB()
    monkeypatch.setattr(keys_mod, "db", fake)
    asyncio.run(keys_mod.list_keys(enrollment_search="x" * 250))
    assert len(fake.args[0]) == 100


def test_list_keys_numbers_placeholders_across_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeysFakeDB()
    monkeypatch.setattr(keys_mod, "db", fake)
    uid, eid = uuid.uuid4(), uuid.uuid4()
    asyncio.run(keys_mod.list_keys(user_id=uid, enrollment_id=eid, enrollment_search="acme"))
    assert fake.args == (uid, eid, "acme")
    assert "k.user_id = $1" in fake.sql
    assert "k.enrollment_id = $2" in fake.sql
    assert "e.label ILIKE '%' || $3 || '%'" in fake.sql
    # the console's default filter still applies
    assert "k.revoked_at IS NULL" in fake.sql


def test_list_keys_include_revoked_drops_the_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeysFakeDB()
    monkeypatch.setattr(keys_mod, "db", fake)
    asyncio.run(keys_mod.list_keys(include_revoked=True))
    assert "k.revoked_at IS NULL" not in fake.sql


def test_create_key_stamps_the_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeysFakeDB()
    monkeypatch.setattr(keys_mod, "db", fake)
    eid = uuid.uuid4()
    plaintext, key_id = asyncio.run(keys_mod.create_key(uuid.uuid4(), "k", ["proxy"], enrollment_id=eid))
    assert plaintext.startswith("hllm-") and key_id is not None
    assert "enrollment_id" in fake.sql
    # key_group is the trailing parameter now (None = a fresh group of one);
    # the enrollment rides the slot before it.
    assert fake.args[-2] == eid, "the enrollment rides the second-to-last INSERT parameter"
    # a plain mint (the admin/CLI path) is unchanged: no team, and the fresh
    # key starts its own group of one (generated app-side; see create_key).
    asyncio.run(keys_mod.create_key(uuid.uuid4(), "k", ["proxy"]))
    assert fake.args[-2] is None  # enrollment_id
    assert isinstance(fake.args[-1], uuid.UUID)  # key_group


# ---------------------------------------------------------------------------
# team_allocation_state / team_available — the pool is shared, spend counts
# ---------------------------------------------------------------------------


def _alloc_row(key_id: uuid.UUID, tokens: int, model: str = "*", period: str = "month") -> dict[str, Any]:
    return {"key_id": key_id, "model": model, "period": period, "limit_tokens": tokens}


def _cost_row(key_id: uuid.UUID, usd: float, model: str = "*", period: str = "month") -> dict[str, Any]:
    return {"key_id": key_id, "model": model, "period": period, "limit_usd": usd}


def test_key_allocation_committed_is_clamped() -> None:
    """committed = max(0, alloc - spent): an out-spent seat reserves nothing
    (its over-spend is already in the team total), an unallocated one too."""
    kid = uuid.uuid4()
    over = KeyAllocation(key_id=kid, alloc_tokens=1_000, alloc_usd=1.0, spent_tokens=2_500, spent_usd=2.5)
    assert over.committed_tokens == 0 and over.committed_usd == 0.0
    none_alloc = KeyAllocation(key_id=kid, alloc_tokens=0, alloc_usd=0.0, spent_tokens=0, spent_usd=0.0)
    assert none_alloc.committed_tokens == 0 and none_alloc.committed_usd == 0.0
    part = KeyAllocation(key_id=kid, alloc_tokens=1_000, alloc_usd=5.0, spent_tokens=250, spent_usd=1.25)
    assert part.committed_tokens == 750 and part.committed_usd == 3.75


def test_team_allocation_state_reports_every_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """One row per key of THIS team: allocations summed per period, spend from
    the gate's own ledger math, unallocated and unspent keys still present."""
    fake, row = _install_team(monkeypatch)
    eid = row["id"]
    start = _window_start(datetime.now(UTC), "month")
    # k1: TWO model-specific rows summing to one 1M allocation, partly spent
    k1 = _join(fake, row, uuid.uuid4())
    fake.budget_rows += [_alloc_row(k1, 600_000, model="m-a"), _alloc_row(k1, 400_000, model="m-b")]
    fake.ledger.append(_ledger_row(k1, total=300_000))
    # k2: no allocation at all, but it spent
    k2 = _join(fake, row, uuid.uuid4())
    fake.ledger.append(_ledger_row(k2, total=500))
    # k3: allocated in USD, never used
    k3 = _join(fake, row, uuid.uuid4())
    fake.cost_rows.append(_cost_row(k3, 2.0))
    # k4: a cache hit only — free
    k4 = _join(fake, row, uuid.uuid4())
    fake.ledger.append(_ledger_row(k4, status="cache_hit", total=5_000, cost=9.0))
    # k5: another enrollment's key — invisible here
    other = _enrollment_row(label="team-b")
    fake.enrollments[other["id"]] = other
    k5 = _join(fake, other, uuid.uuid4())
    fake.ledger.append(_ledger_row(k5, total=7_000))
    # k6: spend OUTSIDE the window, plus a DAY-period allocation row
    k6 = _join(fake, row, uuid.uuid4())
    fake.ledger.append(_ledger_row(k6, total=9_000, ts=start - timedelta(minutes=1)))
    fake.budget_rows.append(_alloc_row(k6, 5_000, period="day"))

    state = asyncio.run(teams_mod.team_allocation_state(eid, "month"))
    assert set(state) == {k1, k2, k3, k4, k6}, "every key of the team, and only this team's"
    assert state[k1].alloc_tokens == 1_000_000, "model-specific rows SUM into one allocation"
    assert state[k1].spent_tokens == 300_000
    assert state[k1].committed_tokens == 700_000
    assert state[k2].alloc_tokens == 0 and state[k2].spent_tokens == 500
    assert state[k2].committed_tokens == 0, "no allocation, nothing reserved"
    assert state[k3].alloc_usd == 2.0 and state[k3].spent_usd == 0.0
    assert state[k3].committed_usd == 2.0
    assert state[k4].spent_tokens == 0, "cache_hit rows are free"
    assert state[k6].spent_tokens == 0, "outside the window"
    assert state[k6].alloc_tokens == 0, "a day-period row is not a month allocation"


def test_team_allocation_state_empty_team(monkeypatch: pytest.MonkeyPatch) -> None:
    _, row = _install_team(monkeypatch)
    assert asyncio.run(teams_mod.team_allocation_state(row["id"], "month")) == {}


def test_team_allocation_state_sql_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    """The predicates a fake cannot enforce: the status filter, the window,
    the period filter on BOTH allocation families, the team scope, and the
    gate's own cache-discounted token expression."""
    fake, row = _install_team(monkeypatch)
    before = _window_start(datetime.now(UTC), "month")
    asyncio.run(teams_mod.team_allocation_state(row["id"], "month"))
    after = _window_start(datetime.now(UTC), "month")
    sql, args = fake.allocation_calls[0]
    assert args[0] == row["id"] and args[1] == "month"
    assert args[2] in (before, after), "the window start is the budget engine's"
    assert f"l.status IN ({budgets_mod._COSTED_SQL})" in sql
    assert "guardrail_blocked" in sql, "the rollup must count a post-call block's real spend (r5 #9)"
    assert "GREATEST(" in sql and "LEAST(" in sql, "the cache discount must ride the token SUM"
    assert "l.cost_usd * l.billing_fraction" in sql
    assert "l.ts >= $3::timestamptz" in sql
    assert "b.period = $2" in sql and "c.period = $2" in sql
    assert "WHERE k.enrollment_id = $1" in sql
    assert "LEFT JOIN" in sql, "a key with no spend/allocation must still appear"


def _worked_example_team(monkeypatch: pytest.MonkeyPatch) -> tuple[TeamFakeDB, dict[str, Any]]:
    """12 seats x 1M, six allocated, 9M already spent — the user's example."""
    fake, row = _install_team(monkeypatch, budget_tokens=12_000_000)
    for _ in range(6):
        kid = _join(fake, row, uuid.uuid4())
        fake.budget_rows.append(_alloc_row(kid, 1_000_000))
        # each of the six has already spent past its own 1M allocation
        fake.ledger.append(_ledger_row(kid, total=1_500_000))
    return fake, row


def test_team_available_worked_example(monkeypatch: pytest.MonkeyPatch) -> None:
    """T=12M, six seats allocated 1M each, 9M spent: every allocated seat has
    out-spent its own allocation, so SUM(committed)=0 and 12M-9M=3M is left.
    Three more 1M grants consume exactly that; a fourth is refused at 0."""
    fake, row = _worked_example_team(monkeypatch)
    eid = row["id"]
    assert asyncio.run(teams_mod.team_available(eid, "month")) == (3_000_000, None)

    for _ in range(3):
        kid = _join(fake, row, uuid.uuid4())
        fake.budget_rows.append(_alloc_row(kid, 1_000_000))
    assert asyncio.run(teams_mod.team_available(eid, "month")) == (0, None)

    kid = _join(fake, row, uuid.uuid4())  # a fourth grant
    fake.budget_rows.append(_alloc_row(kid, 1_000_000))
    assert asyncio.run(teams_mod.team_available(eid, "month")) == (0, None), "clamped, never negative"


def test_team_available_counts_actual_spend_against_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spend alone consumes the pool: with no allocations at all, the whole
    cap minus the team's spend is available."""
    fake, row = _install_team(monkeypatch, budget_tokens=1_000_000)
    kid = _join(fake, row, uuid.uuid4())
    fake.ledger.append(_ledger_row(kid, total=400_000))
    assert asyncio.run(teams_mod.team_available(row["id"], "month")) == (600_000, None)


def test_team_available_excludes_the_key_being_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replace-all case: the key's own CURRENT commitment is about to be
    deleted by the write, so it must not count against its new request."""
    fake, row = _worked_example_team(monkeypatch)
    eid = row["id"]
    grants = []
    for _ in range(3):
        kid = _join(fake, row, uuid.uuid4())
        fake.budget_rows.append(_alloc_row(kid, 1_000_000))
        grants.append(kid)
    assert asyncio.run(teams_mod.team_available(eid, "month")) == (0, None)
    assert asyncio.run(teams_mod.team_available(eid, "month", exclude_key_id=grants[0])) == (
        1_000_000,
        None,
    )


def test_team_available_usd_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch, budget_usd=50.0)
    eid = row["id"]
    k1 = _join(fake, row, uuid.uuid4())
    fake.cost_rows.append(_cost_row(k1, 10.0))
    fake.ledger.append(_ledger_row(k1, total=0, cost=4.0))
    k2 = _join(fake, row, uuid.uuid4())
    fake.cost_rows.append(_cost_row(k2, 5.0))
    # T=50, S=4, committed = (10-4) + (5-0) = 11 -> 35
    assert asyncio.run(teams_mod.team_available(eid, "month")) == (None, 35.0)
    # the USD pool honours exclude_key_id too (the replace-all path is
    # kind-agnostic: a key's own commitment must not count for either kind)
    assert asyncio.run(teams_mod.team_available(eid, "month", exclude_key_id=k1)) == (None, 41.0)


def test_team_available_without_a_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """No cap of a kind => None for that kind, and nothing is even queried:
    an enrollment without an envelope enforces nothing."""
    fake, row = _install_team(monkeypatch)  # both caps NULL
    assert asyncio.run(teams_mod.team_available(row["id"], "month")) == (None, None)
    assert fake.allocation_calls == [] and fake.spend_calls == []


def test_team_available_uses_the_enrollments_own_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap exists for ONE window: a caller asking for another period must
    still get the enrollment's pool (a day's spend against a monthly cap would
    understate it)."""
    fake, row = _worked_example_team(monkeypatch)
    assert asyncio.run(teams_mod.team_available(row["id"], "day")) == (3_000_000, None)
    assert fake.allocation_calls[0][1][1] == "month"


def test_team_available_unknown_enrollment_is_uncapped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = TeamFakeDB()
    monkeypatch.setattr(teams_mod, "db", fake)
    assert asyncio.run(teams_mod.team_available(uuid.uuid4(), "month")) == (None, None)


# ---------------------------------------------------------------------------
# stamp_leader_used — the throttled, best-effort last-used telemetry
# ---------------------------------------------------------------------------


def test_stamp_leader_used_writes_when_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch)
    assert row.get("team_leader_key_last_used_at") is None
    asyncio.run(teams_mod.stamp_leader_used(row["id"]))
    assert fake.stamp_writes == 1
    assert row["team_leader_key_last_used_at"] is not None
    sql, args = fake.execute_calls[-1]
    assert "team_leader_key_last_used_at = now()" in sql
    # the throttle lives in the UPDATE's own predicate (atomic, not a read-then-write)
    assert "team_leader_key_last_used_at IS NULL" in sql
    assert "now() - $2::interval" in sql
    # the interval travels as a timedelta (asyncpg's interval codec)
    assert isinstance(args[1], timedelta) and args[1].total_seconds() == 3600.0


def test_stamp_leader_used_skips_when_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch)
    row["team_leader_key_last_used_at"] = datetime.now(UTC)
    asyncio.run(teams_mod.stamp_leader_used(row["id"]))
    assert fake.stamp_skipped == 1 and fake.stamp_writes == 0
    # once the throttle window has passed the stamp lands again
    row["team_leader_key_last_used_at"] = datetime.now(UTC) - timedelta(hours=2)
    asyncio.run(teams_mod.stamp_leader_used(row["id"]))
    assert fake.stamp_writes == 1


def test_stamp_leader_used_honours_the_interval_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(monkeypatch)
    row["team_leader_key_last_used_at"] = datetime.now(UTC) - timedelta(minutes=30)
    asyncio.run(teams_mod.stamp_leader_used(row["id"], min_interval_s=60.0))
    assert fake.stamp_writes == 1, "a 60s throttle lets a 30-minute-old stamp through"


def test_stamp_leader_used_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A telemetry stamp must never fail an auth that already succeeded (the
    core.traces.record posture)."""

    class BrokenFake(TeamFakeDB):
        async def execute(self, sql: str, *a: Any) -> str:
            raise RuntimeError("db down")

    monkeypatch.setattr(teams_mod, "db", BrokenFake())
    asyncio.run(teams_mod.stamp_leader_used(uuid.uuid4()))  # must not raise


# ---------------------------------------------------------------------------
# DERIVED ENVELOPE (2026-10-30): an enrollment minted WITH a profile but
# WITHOUT an explicit cap gets max_uses x the profile's per-key presets for the
# envelope period. Without it the team was uncapped, overflow_enabled had no
# pool to overflow into, and a key's own allocation was the only bound.
# ---------------------------------------------------------------------------


def _profile(defaults: dict[str, Any]) -> str:
    return json.dumps(defaults)


BABY_PRESETS = {
    "budgets": [
        {"model": "Deepseek-V4.1-Flash-1017", "period": "day", "limit_tokens": 10_000},
        {"model": "Deepseek-V4.1-Flash-1017", "period": "month", "limit_tokens": 1_000_000},
        {"model": "GLM-5.3-Flash", "period": "day", "limit_tokens": 10_000},
        {"model": "GLM-5.3-Flash", "period": "month", "limit_tokens": 1_000_000},
        {"model": "Qwen3.8-27B", "period": "month", "limit_tokens": 10_000_000},
    ]
}


def test_derived_envelope_multiplies_the_period_presets_by_the_mint_allowance() -> None:
    """4 mints x 12,000,000 monthly = 48,000,000 — the number the console must
    show and the gate must enforce. The DAY presets are excluded: summing a
    daily limit into a monthly cap would invent a number."""
    tokens, usd = teams_mod._derived_envelope(_profile(BABY_PRESETS), 4, "month")
    assert tokens == 48_000_000
    assert usd is None


def test_derived_envelope_is_none_for_unlimited_mints() -> None:
    """max_uses == 0 means UNLIMITED mints: there is no N, so there is no
    derived cap. An honest "no cap", never a guess."""
    assert teams_mod._derived_envelope(_profile(BABY_PRESETS), 0, "month") == (None, None)
    assert teams_mod._derived_envelope(_profile(BABY_PRESETS), None, "month") == (None, None)


def test_derived_envelope_ignores_a_period_the_profile_does_not_budget() -> None:
    """The POOLED number is period-scoped: a profile that budgets day+month
    derives no WEEK envelope. (The per-model caps still exist for their own
    periods — a daily preset stays a DAILY pool, see the cap test below.)"""
    assert teams_mod._derived_envelope(_profile(BABY_PRESETS), 4, "week") == (None, None)


def test_derived_envelope_sums_usd_presets_and_survives_junk() -> None:
    usd_profile = {"budgets": [{"model": "*", "period": "month", "limit_usd": 2.5}]}
    assert teams_mod._derived_envelope(_profile(usd_profile), 4, "month") == (None, 10.0)
    # unreadable / absent profile JSON degrades to "no derived cap" — the same
    # fail-safe direction as the mapper (the per-key caps still apply).
    assert teams_mod._derived_envelope("{not json", 4, "month") == (None, None)
    assert teams_mod._derived_envelope(None, 4, "month") == (None, None)
    assert teams_mod._derived_envelope({"budgets": "nope"}, 4, "month") == (None, None)


def test_row_mapper_derives_only_when_the_cap_is_absent() -> None:
    """An EXPLICIT row value always wins; a missing one is filled from the
    profile and flagged so the console can say where it came from."""
    base = {
        "id": uuid.uuid4(),
        "label": "team",
        "tenant_id": uuid.uuid4(),
        "profile_id": uuid.uuid4(),
        "budget_period": "month",
        "overflow_enabled": True,
        "max_uses": 4,
        "defaults_json": _profile(BABY_PRESETS),
    }
    derived = teams_mod.team_enrollment_from_row({**base, "budget_tokens": None, "budget_usd": None})
    assert derived is not None
    assert derived.budget_tokens == 48_000_000
    assert derived.budget_derived is True
    assert derived.max_uses == 4
    assert derived.has_budget is True, "a derived cap IS a cap — the gate must enforce it"

    explicit = teams_mod.team_enrollment_from_row({**base, "budget_tokens": 1000, "budget_usd": None})
    assert explicit is not None
    assert explicit.budget_tokens == 1000, "an explicit cap is never overwritten"
    assert explicit.budget_derived is False


# ---------------------------------------------------------------------------
# PER-MODEL derived caps (2026-10-30): every preset x N is its own team pool
# ---------------------------------------------------------------------------


def test_derive_envelope_yields_one_cap_per_preset() -> None:
    """Owner decision (audit W6 P2-8): ONE period per enrollment, so every
    preset folds to it — one cap per (model, kind), never a per-period fan-out.

    Each preset keeps its OWN period: a daily preset is a DAILY team pool, a
    monthly one a MONTHLY pool, and BOTH bind (the gate checks each cap against
    its own period's spend). Only the POOLED number is period-scoped."""
    tokens, usd, caps = teams_mod.derive_envelope(_profile(BABY_PRESETS), 4, "month")
    assert (tokens, usd) == (48_000_000, None), "pooled = the MONTH presets x N"
    got = {(c.model, c.period): (c.limit_tokens, c.limit_usd) for c in caps}
    assert got == {
        ("Deepseek-V4.1-Flash-1017", "day"): (40_000, None),
        ("Deepseek-V4.1-Flash-1017", "month"): (4_000_000, None),
        ("GLM-5.3-Flash", "day"): (40_000, None),
        ("GLM-5.3-Flash", "month"): (4_000_000, None),
        ("Qwen3.8-27B", "month"): (40_000_000, None),
    }
    assert len(caps) == 5, "one cap per preset period — the DAY pools must survive"
    w_tokens, w_usd, w_caps = teams_mod.derive_envelope(_profile(BABY_PRESETS), 4, "week")
    assert (w_tokens, w_usd) == (None, None), "day/month presets never sum into a week cap"
    assert len(w_caps) == 5, "the caps exist whatever the enrollment period is"


def test_derive_envelope_star_preset_is_a_pooled_cap() -> None:
    tokens, usd, caps = teams_mod.derive_envelope(
        _profile({"budgets": [{"model": "*", "period": "month", "limit_tokens": 2_000_000}]}), 3, "month"
    )
    assert (tokens, usd) == (6_000_000, None)
    assert [(c.model, c.limit_tokens) for c in caps] == [("*", 6_000_000)]


def test_derive_envelope_skips_junk_periods_and_limits() -> None:
    junk = {
        "budgets": [
            {"model": "m", "period": "fortnight", "limit_tokens": 5},
            {"model": "m", "period": "month", "limit_tokens": 0},
            {"model": "m", "period": "month", "limit_tokens": "lots"},
            "not-a-dict",
            {"model": "ok", "period": "month", "limit_tokens": 7},
        ]
    }
    tokens, _usd, caps = teams_mod.derive_envelope(_profile(junk), 2, "month")
    assert tokens == 14
    assert [(c.model, c.limit_tokens) for c in caps] == [("ok", 14)]


def test_row_mapper_caps_follow_only_derived_dimensions() -> None:
    """Mixed envelope: an EXPLICIT dimension stays pooled (the operator's
    number replaced the derivation there); the caps carry only what the
    derivation actually filled, and a fully explicit row derives nothing."""
    base = {
        "id": uuid.uuid4(),
        "label": "team",
        "tenant_id": uuid.uuid4(),
        "profile_id": uuid.uuid4(),
        "budget_period": "month",
        "overflow_enabled": True,
        "max_uses": 4,
        "defaults_json": _profile(BABY_PRESETS),
    }
    mixed = teams_mod.team_enrollment_from_row({**base, "budget_tokens": None, "budget_usd": 50.0})
    assert mixed is not None
    assert (mixed.budget_tokens, mixed.budget_usd) == (48_000_000, 50.0)
    assert mixed.derived_dims == frozenset({"tokens"})
    assert mixed.model_caps and all(c.limit_usd is None for c in mixed.model_caps)
    assert any(c.limit_tokens == 4_000_000 for c in mixed.model_caps)
    explicit = teams_mod.team_enrollment_from_row({**base, "budget_tokens": 1000, "budget_usd": 50.0})
    assert explicit is not None
    assert explicit.model_caps == () and explicit.derived_dims == frozenset()


def _caps_enrollment(**over: Any) -> dict[str, Any]:
    return _enrollment_row(
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        defaults_json=_profile(BABY_PRESETS),
        **over,
    )


def test_gate_derived_caps_refuse_per_model(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    """GLM month: 4 mints x 1M = 4,000,000 — a team pool FOR GLM. Spent to
    the cap on GLM refuses a GLM request even though the key itself is far
    under its own allocation; an all-derived team pays ONE aggregate.

    The clock is frozen: the premise needs the DAY pool BELOW its cap while the
    MONTH pool is AT its cap, and on the 1st of a month those two windows are
    the same instant (one ledger row satisfies both), so no calendar-independent
    fixture can express it."""
    windows = _freeze_clock(monkeypatch, datetime(2026, 11, 18, 12, 0, tzinfo=UTC))
    enrollment = _caps_enrollment()
    _own, team = gate(
        enrollment=enrollment,
        cap_spend={("GLM-5.3-Flash", windows["month"]): (4_000_000, 0.0)},
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is False
    assert res.reason == "team month budget for GLM-5.3-Flash 4000000 exceeded (spent 4000000)"
    assert len(team.cap_spend_calls) == 1
    assert team.spend_calls == [], "no SECOND query: the pooled scope rides the same aggregate"


def test_gate_derived_caps_admit_below_the_cap(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    # frozen clock: the day and month pools must be DISTINCT instants (they are
    # the same instant on the 1st, see the refuse test above).
    windows = _freeze_clock(monkeypatch, datetime(2026, 11, 18, 12, 0, tzinfo=UTC))
    enrollment = _caps_enrollment()
    _own, team = gate(
        enrollment=enrollment,
        cap_spend={
            ("GLM-5.3-Flash", windows["month"]): (3_999_999, 0.0),
            ("GLM-5.3-Flash", windows["day"]): (39_999, 0.0),
        },
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    # warn 95 (2026-11-03): the TEAM band now rides the gate's warn too. The
    # derived month pool is at 3,999,999/4,000,000 — "about to run out" is
    # exactly what the header must say; admission is unaffected.
    assert res.ok is True and res.warn == 95
    assert len(team.cap_spend_calls) == 1, "day + month scopes ride ONE aggregate"


def test_gate_overflow_draws_only_on_the_matching_pool(gate) -> None:
    """overflow ON: a GLM MONTH refusal is swallowed by GLM MONTH headroom — and
    when that same pool is exhausted, the TEAM refusal names it.

    The pool is the MONTH one because every preset folds into the enrollment's
    pool it names (audit W6 P2-8 keeps one pool per PRESET period, so the DAY
    pool is a real daily bound alongside the monthly one)."""
    enrollment = _caps_enrollment(overflow_enabled=True)
    _own, team = gate(
        token_rows=[_token_row(10_000, 10_000, "day")],
        enrollment=enrollment,
        cap_spend={("GLM-5.3-Flash", _window("day")): (39_999, 0.0)},
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is True, "headroom in the GLM day pool swallows the GLM day refusal"

    exhausted = _caps_enrollment(overflow_enabled=True)
    _own2, team2 = gate(
        token_rows=[_token_row(10_000, 10_000, "day")],
        enrollment=exhausted,
        cap_spend={("GLM-5.3-Flash", _window("day")): (40_000, 0.0)},
    )
    kid2 = _join(team2, exhausted, uuid.uuid4())
    res2 = asyncio.run(budgets_mod.check_budgets(kid2, "GLM-5.3-Flash", 0))
    assert res2.ok is False
    assert res2.reason == "team day budget for GLM-5.3-Flash 40000 exceeded (spent 40000)"


def test_gate_overflow_cannot_draw_across_models_or_periods(gate) -> None:
    """A GLM DAY refusal with NO GLM day pool stands even though the GLM month
    pool has headroom — a refusal is rescued only by the pool of the same
    (model, period, kind). The no-pool case is real: leaders hand out per-key
    allocations, so a key can carry a row the profile never preset."""
    enrollment = _enrollment_row(
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        overflow_enabled=True,
        defaults_json=_profile({"budgets": [{"model": "GLM-5.3-Flash", "period": "month", "limit_tokens": 1_000_000}]}),
    )
    _own, team = gate(
        token_rows=[_token_row(10_000, 10_000, "day")],
        enrollment=enrollment,
        cap_spend={("GLM-5.3-Flash", _window("month")): (0, 0.0)},
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is False
    assert res.reason == "day budget 10000 exceeded (spent 10000)"


def test_gate_star_cap_pools_across_models(gate) -> None:
    """model='*': ONE pool across every model — 12M x 4 mints for the whole
    team no matter which model burned it."""
    enrollment = _enrollment_row(
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        defaults_json=_profile({"budgets": [{"model": "*", "period": "month", "limit_tokens": 12_000_000}]}),
    )
    _own, team = gate(enrollment=enrollment, cap_spend={("*", _window("month")): (48_000_000, 0.0)})
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "some-model", 0))
    assert res.ok is False
    assert res.reason == "team month budget for any model 48000000 exceeded (spent 48000000)"


def test_gate_model_without_a_cap_falls_back_to_pooled(gate) -> None:
    """A model no preset names keeps the pooled outer bound (the derived month
    sum) — the same accidental bound it always had, never unbounded."""
    enrollment = _caps_enrollment()
    _own, team = gate(enrollment=enrollment, spend=(48_000_000, 0.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "unknown-model", 0))
    assert res.ok is False
    assert res.reason == "team month budget 48000000 exceeded (spent 48000000)"
    assert team.cap_spend_calls == []


def test_gate_usd_derived_cap_refuses(gate) -> None:
    enrollment = _enrollment_row(
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        defaults_json=_profile({"budgets": [{"model": "GLM-5.3-Flash", "period": "month", "limit_usd": 2.5}]}),
    )
    _own, team = gate(enrollment=enrollment, cap_spend={("GLM-5.3-Flash", _window("month")): (0, 10.0)})
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is False
    assert res.reason == "team month cost budget for GLM-5.3-Flash $10.00 exceeded (spent $10.0000)"


# ---------------------------------------------------------------------------
# Review findings (2026-10-30): the pooled overflow must not rescue refusals
# it has no pool for, and an unreadable row is never rescuable.
# ---------------------------------------------------------------------------


def test_gate_pooled_overflow_needs_a_same_kind_pool(gate) -> None:
    """F1: an explicit envelope rescues a per-key refusal only when it caps
    the SAME kind for its own period. A token refusal with a USD-only
    envelope has no token pool to overflow into — it stands."""
    enrollment = _enrollment_row(budget_usd=100.0, budget_tokens=None, overflow_enabled=True)
    _own, team = gate(token_rows=[_token_row(100, 200)], enrollment=enrollment, spend=(0, 0.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False
    assert res.reason == "month budget 100 exceeded (spent 200)"


def test_gate_pooled_overflow_needs_the_envelopes_own_period(gate) -> None:
    """F1: a DAY refusal is not rescued by a MONTH envelope — different window."""
    enrollment = _enrollment_row(budget_tokens=10_000, overflow_enabled=True)
    _own, team = gate(token_rows=[_token_row(100, 200, "day")], enrollment=enrollment, spend=(0, 0.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False
    assert res.reason == "day budget 100 exceeded (spent 200)"


def test_gate_nonfinite_usd_refusal_is_never_rescued(gate) -> None:
    """F2: an unreadable USD row is not a pool — overflow headroom must not
    admit it (audit F1's fail-closed intent)."""
    enrollment = _enrollment_row(budget_usd=100.0, overflow_enabled=True)
    _own, team = gate(usd_rows=[_usd_row(float("nan"), 0.0)], enrollment=enrollment, spend=(0, 0.0))
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False
    assert "non-finite" in res.reason


# ---------------------------------------------------------------------------
# Spend-aware overflow (2026-10-30): headroom is UNCOMMITTED headroom —
# T - S - SUM(max(0, alloc_k - spent_k)). A teammate's untouched allocation
# is not available to a key that has out-spent its own.
# ---------------------------------------------------------------------------


def test_gate_overflow_needs_uncommitted_headroom(gate) -> None:
    """T=10k, S=9k, a teammate holds an untouched 1k allocation: the pool is
    fully committed, so the over-allocation key is REFUSED despite raw
    headroom (T - S = 1k)."""
    enrollment = _enrollment_row(budget_tokens=10_000, overflow_enabled=True)
    _own, team = gate(
        token_rows=[_token_row(100, 200)],
        enrollment=enrollment,
        spend=(9_000, 0.0),
    )
    kid = _join(team, enrollment, uuid.uuid4())
    other = _join(team, enrollment, uuid.uuid4())
    team.budget_rows = [{"key_id": other, "period": "month", "limit_tokens": 1_000, "model": "*"}]
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is False
    assert res.reason == "team month budget fully committed — no overflow headroom left"


def test_gate_overflow_admits_into_the_uncommitted_remainder(gate) -> None:
    """Same envelope, teammate commits only 500: 500 tokens remain unpromised,
    so the overflow is admitted."""
    enrollment = _enrollment_row(budget_tokens=10_000, overflow_enabled=True)
    _own, team = gate(
        token_rows=[_token_row(100, 200)],
        enrollment=enrollment,
        spend=(9_000, 0.0),
    )
    kid = _join(team, enrollment, uuid.uuid4())
    other = _join(team, enrollment, uuid.uuid4())
    team.budget_rows = [{"key_id": other, "period": "month", "limit_tokens": 500, "model": "*"}]
    res = asyncio.run(budgets_mod.check_budgets(kid, "m1", 0))
    assert res.ok is True


def test_gate_derived_overflow_needs_uncommitted_scope_headroom(gate) -> None:
    """The same rule per (model, period) pool: the GLM-day pool has 1k raw
    headroom, but a teammate's untouched 2k GLM-day allocation exceeds it —
    the overflow is refused."""
    enrollment = _caps_enrollment(overflow_enabled=True)
    _own, team = gate(
        token_rows=[_token_row(10_000, 10_000, "day")],
        enrollment=enrollment,
        cap_spend={("GLM-5.3-Flash", _window("day")): (39_000, 0.0)},
    )
    kid = _join(team, enrollment, uuid.uuid4())
    other = _join(team, enrollment, uuid.uuid4())
    start = _window_start(datetime.now(UTC), "day")
    team.ledger = [_ledger_row(kid, total=39_000, ts=start + timedelta(minutes=1), model="GLM-5.3-Flash")]
    team.budget_rows = [{"key_id": other, "period": "day", "limit_tokens": 2_000, "model": "GLM-5.3-Flash"}]
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is False
    assert "fully committed" in res.reason


def test_gate_derived_overflow_admits_into_uncommitted_scope_remainder(gate) -> None:
    """Teammate commits only 500 of the GLM-day pool: 500 unpromised remain."""
    enrollment = _caps_enrollment(overflow_enabled=True)
    _own, team = gate(
        token_rows=[_token_row(10_000, 10_000, "day")],
        enrollment=enrollment,
        cap_spend={("GLM-5.3-Flash", _window("day")): (39_000, 0.0)},
    )
    kid = _join(team, enrollment, uuid.uuid4())
    other = _join(team, enrollment, uuid.uuid4())
    start = _window_start(datetime.now(UTC), "day")
    team.ledger = [_ledger_row(kid, total=39_000, ts=start + timedelta(minutes=1), model="GLM-5.3-Flash")]
    team.budget_rows = [{"key_id": other, "period": "day", "limit_tokens": 500, "model": "GLM-5.3-Flash"}]
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is True


def test_team_scope_available_subtracts_committed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper itself: cap - scope spend - SUM(max(0, alloc - spent))."""
    fake, row = _install_team(
        monkeypatch,
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        defaults_json=_profile(BABY_PRESETS),
    )
    k1 = _join(fake, row, uuid.uuid4())
    k2 = _join(fake, row, uuid.uuid4())
    start = _window_start(datetime.now(UTC), "day")
    fake.ledger = [_ledger_row(k1, total=30_000, ts=start + timedelta(minutes=1), model="GLM-5.3-Flash")]
    fake.budget_rows = [
        {"key_id": k1, "period": "day", "limit_tokens": 10_000, "model": "GLM-5.3-Flash"},
        {"key_id": k2, "period": "day", "limit_tokens": 5_000, "model": "GLM-5.3-Flash"},
    ]
    avail_t, avail_u = asyncio.run(teams_mod.team_scope_available(row["id"], "GLM-5.3-Flash", "day", 40_000, None))
    # spend 30k; k1 is over its 10k (commits 0), k2 commits 5k
    assert avail_t == 40_000 - 30_000 - 5_000
    assert avail_u is None


def test_team_cap_spend_sql_never_nests_aggregates(gate) -> None:
    """Regression (2026-10-30 live 500): team_cap_spend brings its own
    SUM(...) FILTER per scope, so it must interpolate the per-row VALUE
    expression, never the SUM form — SUM(SUM(...)) is a Postgres
    GroupingError the fake DB can never catch."""
    enrollment = _caps_enrollment()
    _own, team = gate(enrollment=enrollment, cap_spend={("GLM-5.3-Flash", _window("month")): (0, 0.0)})
    kid = _join(team, enrollment, uuid.uuid4())
    asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    sql = team.cap_spend_calls[0][0]
    assert "SUM(SUM(" not in sql
    assert "SUM(GREATEST(" in sql, "the cache-discounted value rides the FILTER SUM"


def test_team_scope_available_sql_never_nests_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, row = _install_team(
        monkeypatch,
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        defaults_json=_profile(BABY_PRESETS),
    )
    _join(fake, row, uuid.uuid4())
    asyncio.run(teams_mod.team_scope_available(row["id"], "GLM-5.3-Flash", "day", 40_000, None))
    sql = fake.scope_calls[0][0]
    assert "SUM(SUM(" not in sql
    assert "SUM(GREATEST(" in sql


# ---------------------------------------------------------------------------
# Audit W6 (2026-11-02) pins. FIX 1 (pooled outer bound, one aggregate) and FIX 3
# (window-keyed fixture) live here; FIX 2's route-level pin lives in
# tests/test_team_leader_api.py, where it exercises the real route.
# ---------------------------------------------------------------------------


def test_gate_derived_pooled_total_bounds_the_team(gate) -> None:
    """PIN (audit W6 #1): a DERIVED envelope's pooled number is the SUM of the
    per-model pools, so it is an OUTER bound on the team's total spend — a
    named-model request must not be admitted just because its OWN pool has
    headroom while the team is already at the envelope.

    Pre-fix the pooled block was guarded by 'tokens' not in derived_dims, which
    is false for every derived team, so this request was ADMITTED (ok=True) and
    the team could reach ~2x the number the console shows.

    The pooled total is the '*' scope (no model filter) of the SAME aggregate as
    the caps, so the outer bound costs ONE round trip — the second assertion
    pins that property (it was a separate team_spend call before the fold)."""
    enrollment = _caps_enrollment()  # derived: pooled month = 48,000,000
    _own, team = gate(
        enrollment=enrollment,
        cap_spend={
            ("GLM-5.3-Flash", _window("month")): (0, 0.0),  # this model: empty
            ("*", _window("month")): (48_000_000, 0.0),  # the team total: at the envelope
        },
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is False
    assert res.reason == "team month budget 48000000 exceeded (spent 48000000)"
    assert len(team.cap_spend_calls) == 1, "ONE aggregate carries the caps AND the pooled total"
    assert team.spend_calls == [], "the pooled bound must not cost a SECOND round trip"


def test_fake_cap_spend_keys_on_the_window_not_an_inferred_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN (audit W6 #5): cap_spend_override is keyed by the WINDOW DATETIME.

    The old fixture inferred a scope's period from its window (month -> week ->
    day, first match wins). On a MONDAY the day-start and the week-start are the
    SAME instant, so the day window was labelled 'week', the override lookup
    missed and returned (0, 0.0): every ADMIT assertion passed without the pool
    ever being read and every REFUSE assertion failed. 2026-11-02 is a Monday —
    the worst case, pinned here so it can never regress on a calendar."""
    fake, row = _install_team(
        monkeypatch,
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        defaults_json=_profile(BABY_PRESETS),
    )
    monday = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # a Monday
    windows = _freeze_clock(monkeypatch, monday)
    assert windows["day"] != windows["month"], "the Monday case is day == week, not day == month"
    fake.cap_spend_override = {
        ("GLM-5.3-Flash", windows["day"]): (11, 0.0),
        ("GLM-5.3-Flash", windows["month"]): (22, 0.0),
    }
    scopes = [("GLM-5.3-Flash", "day"), ("GLM-5.3-Flash", "month")]
    got = asyncio.run(teams_mod.team_cap_spend(row["id"], scopes))
    assert got[("GLM-5.3-Flash", "day")][0] == 11, "the day scope must read the day window's entry"
    assert got[("GLM-5.3-Flash", "month")][0] == 22


def test_a_caps_only_team_is_still_a_team(gate) -> None:
    """PIN (audit W6 P2-8): a preset that names ANOTHER period is a REAL pool of
    its OWN period, and a team with no pooled cap must still be consulted.

    Pre-fix a day-only profile on a month enrollment derived pooled=None, so
    apply_derived_envelope dropped the caps and has_budget went False:
    budgets.py skipped the envelope ENTIRELY, the team enforced nothing and the
    overflow switch was inert. The period is NOT folded away — a daily pool is a
    real daily bound — the fix is has_budget counting a caps-only team."""
    day_only = _profile({"budgets": [{"model": "GLM-5.3-Flash", "period": "day", "limit_tokens": 10_000}]})
    enrollment = _enrollment_row(
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        budget_period="month",
        defaults_json=day_only,
    )
    derived = teams_mod.team_enrollment_from_row(enrollment)
    assert derived is not None
    assert derived.has_budget is True, "a caps-only team IS a team — the gate must consult it"
    assert derived.budget_tokens is None, "no month preset -> no pooled monthly number"
    assert [(c.model, c.period, c.limit_tokens) for c in derived.model_caps] == [("GLM-5.3-Flash", "day", 40_000)], (
        "the day preset stays a DAY pool"
    )
    # ...and it BINDS: the team is refused at the day pool (pre-fix: never asked).
    _own, team = gate(
        enrollment=enrollment,
        cap_spend={("GLM-5.3-Flash", _window("day")): (40_000, 0.0)},
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is False, "the day pool must bind"
    assert res.reason == "team day budget for GLM-5.3-Flash 40000 exceeded (spent 40000)"
    # CONTROL: a matching-period preset derives exactly the same cap as before.
    month_only = _profile({"budgets": [{"model": "GLM-5.3-Flash", "period": "month", "limit_tokens": 1_000_000}]})
    same = teams_mod.team_enrollment_from_row(
        _enrollment_row(
            budget_tokens=None,
            budget_usd=None,
            max_uses=4,
            budget_period="month",
            defaults_json=month_only,
        )
    )
    assert same is not None
    assert same.budget_tokens == 4_000_000
    assert [(c.model, c.period, c.limit_tokens) for c in same.model_caps] == [("GLM-5.3-Flash", "month", 4_000_000)]


def test_day_and_month_presets_are_two_pools_and_the_day_one_binds(gate) -> None:
    """PIN (the owner's live report): a profile with BOTH a day and a month
    preset for one model yields TWO team pools, and the DAY pool binds on its
    own — a team far under its monthly envelope is still refused when its daily
    pool is exhausted.

    This is the regression the (since-reverted) period FOLD caused: collapsing
    the day preset into the month one by "larger wins" left the team with no
    daily bound at all."""
    both = _profile(
        {
            "budgets": [
                {"model": "GLM-5.3-Flash", "period": "day", "limit_tokens": 10_000},
                {"model": "GLM-5.3-Flash", "period": "month", "limit_tokens": 1_000_000},
            ]
        }
    )
    enrollment = _enrollment_row(
        budget_tokens=None,
        budget_usd=None,
        max_uses=4,
        budget_period="month",
        defaults_json=both,
    )
    derived = teams_mod.team_enrollment_from_row(enrollment)
    assert derived is not None
    assert derived.budget_tokens == 4_000_000, "the pooled number is the MONTH presets x N"
    assert sorted((c.period, c.limit_tokens) for c in derived.model_caps) == [
        ("day", 40_000),
        ("month", 4_000_000),
    ], "TWO pools — the day preset must not be folded into the month one"
    # The DAY pool is exhausted; the MONTH pool has ~99% of its cap left.
    _own, team = gate(
        enrollment=enrollment,
        cap_spend={
            ("GLM-5.3-Flash", _window("day")): (40_000, 0.0),
            ("GLM-5.3-Flash", _window("month")): (40_000, 0.0),
        },
    )
    kid = _join(team, enrollment, uuid.uuid4())
    res = asyncio.run(budgets_mod.check_budgets(kid, "GLM-5.3-Flash", 0))
    assert res.ok is False, "the exhausted DAY pool must refuse even with month headroom"
    assert res.reason == "team day budget for GLM-5.3-Flash 40000 exceeded (spent 40000)"
    assert len(team.cap_spend_calls) == 1, "day + month scopes ride ONE aggregate"


def test_revoked_key_releases_its_unspent_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN (owner decision A / audit W6 P2-7): a departed seat's promise is
    released at REVOCATION, not at period rollover.

    The committed subtraction is SUM(max(0, alloc - spent)) over the team's keys;
    pre-fix it covered EVERY key of the enrollment, so a revoked key's untouched
    allocation reserved pool forever and the replacement seat got a 409. The
    same "live key" predicate must drive the console's 'allocated' too, or the
    pool would disagree with itself between the gate and the console."""
    fake, row = _install_team(monkeypatch, budget_tokens=2_000_000, budget_usd=None)
    live = _join(fake, row, uuid.uuid4())
    departed = _join(fake, row, uuid.uuid4())
    fake.budget_rows = [
        {"key_id": live, "period": "month", "limit_tokens": 500_000, "model": "*"},
        {"key_id": departed, "period": "month", "limit_tokens": 1_000_000, "model": "*"},
    ]
    # CONTROL: while both seats are live, both promises reserve the pool.
    avail_live, _u = asyncio.run(teams_mod.team_available(row["id"], "month"))
    assert avail_live == 2_000_000 - 1_500_000, "an ACTIVE key's promise still reduces availability"
    # The departed teammate is revoked: their untouched 1M comes straight back.
    fake.revoked_keys.add(departed)
    avail_after, _u2 = asyncio.run(teams_mod.team_available(row["id"], "month"))
    assert avail_after == 2_000_000 - 500_000, (
        "the revoked seat's unspent allocation must be AVAILABLE again, not reserved until rollover"
    )
    # ...and the console's 'allocated' carries the SAME predicate (one promise set).
    asyncio.run(teams_mod.team_allocated(row["id"], "month"))
    assert "k.revoked_at IS NULL" in fake.allocated_sql[-1], (
        "the console's allocated total must exclude revoked seats too, or the pool "
        "disagrees with itself between the gate and the console"
    )


def test_team_scope_available_scopes_the_ledger_to_the_enrollment(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN (audit W6 F1): the ledger subquery must carry the ENROLLMENT predicate.

    The outer `WHERE k.enrollment_id = $1` only prunes the JOIN — without the
    subselect the aggregate still GROUPS every tenant's usage_ledger rows before
    the join can discard them, on the per-key refusal path (a hot path: every
    request a key makes once it is over its allocation)."""
    fake, row = _install_team(monkeypatch, budget_tokens=None, budget_usd=None, max_uses=4)
    k1 = _join(fake, row, uuid.uuid4())
    foreign = uuid.uuid4()
    fake.key_enrollment[foreign] = uuid.uuid4()  # another enrollment's key
    start = _window_start(datetime.now(UTC), "day")
    fake.ledger = [
        _ledger_row(k1, total=1_000, ts=start + timedelta(minutes=1), model="GLM-5.3-Flash"),
        _ledger_row(foreign, total=999_999, ts=start + timedelta(minutes=1), model="GLM-5.3-Flash"),
    ]
    avail_t, _avail_u = asyncio.run(teams_mod.team_scope_available(row["id"], "GLM-5.3-Flash", "day", 40_000, None))
    sql, _args = fake.scope_calls[-1]
    assert "l.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = $1)" in sql, (
        "the ledger aggregate must be scoped to the enrollment (the same subselect "
        "idiom traces.recent uses), or it scans every tenant's rows on the refusal path"
    )
    assert avail_t == 40_000 - 1_000, "a key from another enrollment must not contribute"


def test_rotate_clears_the_previous_credentials_last_used_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """PIN (audit W6 F3): a rotated leader key must not inherit the OLD
    credential's last-use stamp.

    `stamp_leader_used` throttles its write to one per hour, so a stale stamp is
    not merely cosmetic: for up to an hour after a rotation the admin console
    reports the DEAD key's last use as the live key's."""
    _fake, row = _install_team(monkeypatch, budget_tokens=None, budget_usd=None)
    row["team_leader_key_hash"] = keys_mod.hash_key("the-old-leader-key")
    row["team_leader_key_last_used_at"] = datetime(2026, 1, 1, tzinfo=UTC)
    plaintext, prefix = asyncio.run(teams_mod.mint_leader_key(row["id"]))
    assert plaintext and prefix == plaintext[:10]
    assert row["team_leader_key_last_used_at"] is None, (
        "rotation must start a fresh usage record — the old stamp belongs to the dead key"
    )
