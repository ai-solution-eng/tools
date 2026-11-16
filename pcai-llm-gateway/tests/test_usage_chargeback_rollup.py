"""Chargeback rollup groups by (key NAME, user) — rotations must not split rows.

REPORTED 2026-10-20: the console's "By key — chargeback view" listed the SAME
key name several times with unrelated cost figures (andrew-admin x4). Cause:
the rollup did `GROUP BY l.key_id, k.name, u.name`, and
`POST /keys/{id}/rotate` mints a NEW key row with the SAME name/user while
revoking the old one — so every rotation generation became its own row.

A chargeback view answers "who consumed what"; that answer must survive
rotation. These tests pin the merge AND the two safety properties that make
the merge safe:

  1. u.name STAYS in the GROUP BY — two DIFFERENT users who happen to use the
     same key name must never collapse into one row.
  2. The rollup reports `generations` (count of distinct key_id), so a rotated
     credential stays visibly rotated instead of masquerading as one key.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.db import Database

HEADERS = {"Authorization": "Bearer admin-secret"}


def _client(fake: Any) -> TestClient:
    routes_admin.db = cast(Database, fake)  # type: ignore[assignment]
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


class _RollupDB:
    """Captures the by-key SQL so the grouping contract can be asserted."""

    def __init__(self) -> None:
        self.by_key_sql = ""
        self.by_model_sql = ""

    async def fetchrow(self, sql: str, *a: Any):
        return {"requests": 0}

    async def fetchval(self, sql: str, *a: Any):
        # MIN(ts) drives the console's retained-history label; None = empty
        # ledger. Without this stub /admin/usage/summary 500s on
        # AttributeError before any grouping assertion runs.
        if "min(ts)" in sql.lower():
            return None
        return 0

    async def fetch(self, sql: str, *a: Any):
        # Capture the PARENT by-key rollup specifically. The per-model
        # breakdown (2026-10-26) groups by k.name too, so a looser predicate
        # let the child query overwrite the parent's SQL and broke the
        # grouping assertions below. `AS generations` is unique to the parent.
        if "AS generations" in sql:
            self.by_key_sql = sql
        elif "GROUPING SETS ((model)" in sql:
            self.by_model_sql = sql
        return []


@pytest.fixture(autouse=True)
def _admin_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret")
    yield


def test_by_key_groups_on_name_and_user_not_key_id() -> None:
    """The rotation fix: no key_id in the GROUP BY, name+user retained."""
    fake = _RollupDB()
    t = _client(fake)
    assert t.get("/admin/usage/summary", headers=HEADERS).status_code == 200

    sql = " ".join(fake.by_key_sql.split())  # normalize whitespace
    assert sql, "by-key query was never issued"

    group_by = sql.split("GROUP BY", 1)[1].split("ORDER BY", 1)[0]
    assert "l.key_id" not in group_by, (
        "grouping by key_id re-splits a rotated key into one row per generation: " + group_by
    )
    assert "k.name" in group_by, "the key name is the rollup identity"
    assert "u.name" in group_by, (
        "u.name MUST stay in the GROUP BY — api_keys.name is not unique, so "
        "dropping the user would merge two different people's spend: " + group_by
    )


def test_by_key_reports_generations() -> None:
    """A rolled-up row says how many key generations fed it."""
    fake = _RollupDB()
    t = _client(fake)
    assert t.get("/admin/usage/summary", headers=HEADERS).status_code == 200
    sql = " ".join(fake.by_key_sql.split())
    assert "count(DISTINCT l.key_id)" in sql, (
        "the rollup must count distinct key_id so a rotated key is visible as "
        "rotated rather than looking like one long-lived key"
    )
    assert "AS generations" in sql


# ---------------------------------------------------------------------------
# Per-row ledger projection must carry the columns the console renders
# ---------------------------------------------------------------------------


class _LedgerRowDB:
    """Captures the per-row ledger SELECT (the `format=json` path)."""

    def __init__(self) -> None:
        self.row_sql = ""

    async def fetchrow(self, sql: str, *a: Any):
        return {"requests": 0}

    async def fetch(self, sql: str, *a: Any):
        if "FROM usage_ledger l" in sql and "k.name AS key_name" in sql:
            self.row_sql = sql
        return []


def test_ledger_rows_project_cached_tokens_and_cost() -> None:
    """REPORTED 2026-10-23: every ledger row showed Cached=0 and Cost=—.

    The console renders `cached_tokens`, `cost_usd` and the billing-fraction
    view from each row, but the per-row SELECT omitted all three. The
    formatters then made the omission INVISIBLE as data: fmtInt(undefined)
    renders a hard '0' and fmtUSD(undefined) renders '—', so the table
    asserted "the upstream never caches and nothing is priced" while the
    rollup directly above it reported 98.4% cache-served and $583 spent.
    Two halves of one tab contradicting each other is the tell: the per-row
    projection must carry every column the row renderer reads.
    """
    fake = _LedgerRowDB()
    t = _client(fake)
    assert t.get("/admin/usage?format=json&limit=5", headers=HEADERS).status_code == 200

    sql = " ".join(fake.row_sql.split())  # normalize whitespace
    assert sql, "the per-row ledger query was never issued"
    for col in ("l.cached_tokens", "l.cost_usd", "l.billing_fraction"):
        assert col in sql, f"{col} is rendered by the console but not projected: {sql}"
    # the pre-existing identity columns must survive the addition
    for col in ("l.prompt_tokens", "l.completion_tokens", "l.total_tokens", "k.name AS key_name"):
        assert col in sql


# ---------------------------------------------------------------------------
# Unit-aware rollups must not emit the GROUPING SETS NULL subrow twice
# ---------------------------------------------------------------------------


def test_rollup_sql_drops_the_null_unit_subrow() -> None:
    """REPORTED 2026-11-13: every By-model and By-key row rendered TWICE.

    Both rollups group by GROUPING SETS ((group), (group, unit_kind)) so the
    per-unit sums can be folded into `billable_units_by_unit`. GROUPING SETS
    is a UNION ALL of the two groupings and does NOT deduplicate: ordinary
    chat rows carry unit_kind IS NULL, so the (group, unit_kind) set emits a
    group identical to the (group) total, and `_fold_unit_rows` appended both.
    The HAVING clause keeps the true total (GROUPING = 1) and the real
    per-unit subrows, and drops the phantom NULL subrow.
    """
    fake = _RollupDB()
    t = _client(fake)
    assert t.get("/admin/usage/summary", headers=HEADERS).status_code == 200

    by_model = " ".join(fake.by_model_sql.split())
    by_key = " ".join(fake.by_key_sql.split())
    assert "HAVING GROUPING(unit_kind) = 1 OR unit_kind IS NOT NULL" in by_model, by_model
    assert "HAVING GROUPING(l.unit_kind) = 1 OR l.unit_kind IS NOT NULL" in by_key, by_key


def test_fold_unit_rows_does_not_duplicate_the_null_unit_subrow() -> None:
    """The fold keeps ONE row per group: total + NULL subrow + real subrow.

    This is the cohere-transcribe shape from the live report: a total of 18
    requests, a phantom NULL-unit partial of 5, and a real 'second' subrow.
    Before the fix the fold returned TWO rows (18 and 5) for one model.
    """
    rows = [
        {"model": "cohere-transcribe", "requests": 18, "unit_kind": None, "unit_units": 0.0},
        {"model": "cohere-transcribe", "requests": 5, "unit_kind": None, "unit_units": 0.0},
        {"model": "cohere-transcribe", "requests": 18, "unit_kind": "second", "unit_units": 30.0},
    ]
    folded = routes_admin._fold_unit_rows(rows, ("model",))

    assert len(folded) == 1, f"one row per model — got {folded}"
    assert folded[0]["requests"] == 18
    assert folded[0]["billable_units_by_unit"] == {"second": 30.0}
