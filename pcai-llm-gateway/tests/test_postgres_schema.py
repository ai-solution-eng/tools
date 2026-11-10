"""Real-Postgres integration tests — the gap that let two P1s ship.

WHY THIS FILE EXISTS. Every other test in this suite drives DB fakes, and the
schema is pinned by substring greps. That is precisely how two P1s survived a
green run (AUDIT/REMEDIATION 2026-11-10):

  * `GROUP BY <expr> AS <alias>` — a PostgreSQL syntax error, invisible to a
    fake that returns []. 4 of the 5 /admin/usage rollup modes 500'd in
    production.
  * `conversations.key_group SET NOT NULL` with no DEFAULT — aborts the whole
    implicit schema transaction (hence the boot) when a rolling-upgrade pod
    inserts a row between the backfill and the ALTER.

Both are cheap to catch with a real server. This module starts a THROWAWAY
cluster (initdb + pg_ctl on a unix socket under tmp_path) and skips cleanly
when the PostgreSQL binaries are not installed, so it never blocks a
developer or a CI image that has no postgres.

RUN IT:  .venv/bin/python -m pytest -q tests/test_postgres_schema.py
REQUIRES: `initdb` and `pg_ctl` on PATH (postgresql-server binaries).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from pcai_llm.db.schema import SCHEMA_SQL

PG_BIN = shutil.which("pg_ctl") is not None and shutil.which("initdb") is not None

pytestmark = pytest.mark.skipif(not PG_BIN, reason="PostgreSQL server binaries (initdb/pg_ctl) not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def pg() -> Iterator[str]:
    """A throwaway cluster; yields its unix-socket directory."""
    tmp = tempfile.mkdtemp(prefix="pcai-pg-")
    data = os.path.join(tmp, "data")
    sockdir = os.path.join(tmp, "sock")
    os.makedirs(sockdir)
    port = _free_port()
    try:
        subprocess.run(
            ["initdb", "-D", data, "-U", "postgres", "--auth=trust"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "pg_ctl",
                "-D",
                data,
                "-o",
                f"-k {sockdir} -p {port} -c listen_addresses=''",
                "-l",
                os.path.join(tmp, "log"),
                "start",
            ],
            check=True,
            capture_output=True,
        )
        for _ in range(60):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(os.path.join(sockdir, f".s.PGSQL.{port}"))
                break
            except OSError:
                time.sleep(0.1)
        os.environ["PCAI_TEST_PG_SOCK"] = sockdir
        os.environ["PCAI_TEST_PG_PORT"] = str(port)
        yield sockdir
    finally:
        subprocess.run(["pg_ctl", "-D", data, "stop"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


async def _connect(dbname: str = "postgres"):
    import asyncpg

    return await asyncpg.connect(
        host=os.environ["PCAI_TEST_PG_SOCK"],
        port=int(os.environ["PCAI_TEST_PG_PORT"]),
        user="postgres",
        database=dbname,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 1. Schema idempotency — the property every migration here claims
# ---------------------------------------------------------------------------


def test_schema_applies_twice(pg: str) -> None:
    """SCHEMA_SQL must be re-runnable on every boot.

    db/__init__.py runs the whole file as ONE implicit transaction, so any
    unguarded statement that fails on a second boot takes the pod down.
    """

    async def go() -> None:
        conn = await _connect()
        try:
            for attempt in (1, 2, 3):
                try:
                    await conn.execute(SCHEMA_SQL)
                except Exception as exc:
                    pytest.fail(f"SCHEMA_SQL apply #{attempt} failed: {type(exc).__name__}: {exc}")
            # The P10 columns and the key_group DEFAULT must all be present.
            cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='usage_ledger' AND column_name IN ('billable_units','unit_kind')"
                )
            }
            assert cols == {"billable_units", "unit_kind"}
            for table in ("api_keys", "conversations"):
                default = await conn.fetchval(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name=$1 AND column_name='key_group'",
                    table,
                )
                assert default and "gen_random_uuid" in default, (
                    f"{table}.key_group needs a DEFAULT before SET NOT NULL — a "
                    "rolling-upgrade insert between the backfill and the ALTER "
                    "aborts the whole boot transaction otherwise"
                )
        finally:
            await conn.close()

    _run(go())


def test_schema_tolerates_a_stale_out_of_vocabulary_unit(pg: str) -> None:
    """A pre-existing bad `unit` value must NOT abort the boot.

    This is the P10 P1: the CHECK was added VALID, so one stale row (written by
    an intermediate build, a script, or hand SQL) made the whole implicit
    schema transaction roll back on EVERY replica, forever. NOT VALID is what
    makes the row visible-and-fixable instead of fatal.
    """

    async def go() -> None:
        conn = await _connect()
        try:
            await conn.execute(SCHEMA_SQL)
            await conn.execute("ALTER TABLE model_costs DROP CONSTRAINT IF EXISTS ck_model_costs_unit")
            await conn.execute(
                "INSERT INTO model_costs (model_name, input_per_m, output_per_m, cached_per_m, unit) "
                "VALUES ('legacy-stale','1','1','1','seconds') ON CONFLICT (model_name) DO NOTHING"
            )
            try:
                await conn.execute(SCHEMA_SQL)
            except Exception as exc:
                pytest.fail(
                    "a stale out-of-vocabulary unit row aborted the schema apply "
                    f"({type(exc).__name__}: {exc}) — the CHECK must be NOT VALID"
                )
            valid = await conn.fetchval("SELECT convalidated FROM pg_constraint WHERE conname='ck_model_costs_unit'")
            assert valid is False, "ck_model_costs_unit must be added NOT VALID"
            kept = await conn.fetchval("SELECT unit FROM model_costs WHERE model_name='legacy-stale'")
            assert kept == "seconds", "the offending row must survive so an operator can see it"
        finally:
            await conn.close()

    _run(go())


# ---------------------------------------------------------------------------
# 2. The SQL the routes actually build — the P1-1 class
# ---------------------------------------------------------------------------


class _Recorder:
    """Records the SQL a route builds, returns no rows (the route is the point)."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    async def fetch(self, sql: str, *a: Any) -> list[Any]:
        self.sql.append(sql)
        return []

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        self.sql.append(sql)
        return None

    async def fetchval(self, sql: str, *a: Any) -> Any:
        self.sql.append(sql)
        return 0

    async def execute(self, sql: str, *a: Any) -> str:
        self.sql.append(sql)
        return "UPDATE 0"


@pytest.mark.parametrize("group_by", ["key", "model", "tenant", "day", "subject"])
def test_usage_rollup_sql_executes_on_real_postgres(pg: str, monkeypatch: pytest.MonkeyPatch, group_by: str) -> None:
    """Every group_by mode must build SQL PostgreSQL will actually accept.

    THE P1: the SELECT list and the GROUP BY shared one string, so four of the
    five modes emitted `GROUP BY date_trunc('day', l.ts) AS day` — a syntax
    error (`group_by_item` accepts only `a_expr`; `AS` is legal only in a
    target list). Every fake returned [] and every test passed.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pcai_llm.api import routes_admin
    from pcai_llm.config import settings

    rec = _Recorder()
    monkeypatch.setattr(routes_admin, "db", rec)
    monkeypatch.setattr(settings, "admin_api_keys", "pg-test-admin")
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)

    r = tc.get(
        "/admin/usage",
        params={"group_by": group_by, "limit": 10},
        headers={"Authorization": "Bearer pg-test-admin"},
    )
    assert r.status_code == 200, r.text
    rollups = [s for s in rec.sql if "GROUP BY" in s]
    assert rollups, f"no rollup SQL captured for group_by={group_by}"

    async def go() -> None:
        conn = await _connect()
        try:
            await conn.execute(SCHEMA_SQL)
            for sql in rollups:
                # prepare() parses, rewrites and plans the statement without
                # executing it — a syntax error or a bad column surfaces here
                # exactly as it did in production.
                try:
                    await conn.prepare(sql)
                except Exception as exc:
                    pytest.fail(
                        f"group_by={group_by} builds SQL PostgreSQL rejects ({type(exc).__name__}: {exc})\n---\n{sql}"
                    )
        finally:
            await conn.close()

    _run(go())


# ---------------------------------------------------------------------------
# 3. settle() writes the immutable row — including the P10 columns
# ---------------------------------------------------------------------------


def test_settle_inserts_a_row_with_the_unit_columns(pg: str) -> None:
    """A billing row must land, and an out-of-range quantity must not lose it.

    P10 P1: `billable_units >= 1e12` overflowed NUMERIC(18,6), asyncpg raised
    INSIDE settle, and the ledger row was simply gone for a request the
    upstream had already served.
    """
    from pcai_llm.core import budgets as budgets_mod

    async def go() -> None:
        conn = await _connect()
        try:
            await conn.execute(SCHEMA_SQL)
            tenant = await conn.fetchval("INSERT INTO tenants (name) VALUES ('t-pg') RETURNING id")
            user = await conn.fetchval("INSERT INTO users (tenant_id, name) VALUES ($1,'u-pg') RETURNING id", tenant)
            key = await conn.fetchval(
                "INSERT INTO api_keys (user_id, name, key_prefix, key_hash, scopes) "
                "VALUES ($1,'k-pg','hllm-pg',$2,$3::text[]) RETURNING id",
                user,
                uuid.uuid4().hex,
                ["proxy"],
            )

            # Point the module seam at the real connection.
            class _DB:
                async def execute(self, sql: str, *a: Any) -> str:
                    return await conn.execute(sql, *a)

                async def fetch(self, sql: str, *a: Any) -> Any:
                    return await conn.fetch(sql, *a)

                async def fetchrow(self, sql: str, *a: Any) -> Any:
                    return await conn.fetchrow(sql, *a)

                async def fetchval(self, sql: str, *a: Any) -> Any:
                    return await conn.fetchval(sql, *a)

            orig = budgets_mod.db
            budgets_mod.db = _DB()  # type: ignore[assignment]
            try:
                # 1) an ordinary row lands with the P10 columns
                await budgets_mod.settle(
                    key,
                    "m",
                    "chat",
                    100,
                    20,
                    "ok",
                    5,
                    uuid.uuid4().hex,
                    billable_units=12.5,
                    unit_kind="second",
                )
                row = await conn.fetchrow(
                    "SELECT billable_units, unit_kind FROM usage_ledger WHERE key_id=$1 ORDER BY id DESC LIMIT 1",
                    key,
                )
                assert row is not None, "settle wrote no ledger row"
                assert float(row["billable_units"]) == pytest.approx(12.5)
                assert row["unit_kind"] == "second"

                # 2) an UNSTORABLE quantity must NOT lose the row
                await budgets_mod.settle(
                    key,
                    "m",
                    "chat",
                    100,
                    20,
                    "ok",
                    5,
                    uuid.uuid4().hex,
                    billable_units=2e12,
                    unit_kind="second",
                )
                n = await conn.fetchval("SELECT count(*) FROM usage_ledger WHERE key_id=$1", key)
                assert n == 2, "an out-of-range billable_units LOST the ledger row"
            finally:
                budgets_mod.db = orig  # type: ignore[assignment]
        finally:
            await conn.close()

    _run(go())


# ---------------------------------------------------------------------------
# 4. The propagate-grants CTE — the audit's one refuted hypothesis, pinned
# ---------------------------------------------------------------------------


def test_propagate_grants_cte_is_safe_when_the_key_already_holds_the_pairs(pg: str) -> None:
    """The audit flagged this CTE as snapshot-hazardous; a live PG refuted it.

    `WITH gone AS (DELETE ...) INSERT ... ON CONFLICT DO UPDATE` is correct even
    when the key already holds exactly the pairs being re-inserted. Pinned here
    so a future "fix" cannot be applied to a non-bug.
    """

    async def go() -> None:
        conn = await _connect()
        try:
            await conn.execute(SCHEMA_SQL)
            tenant = await conn.fetchval("INSERT INTO tenants (name) VALUES ('t-cte') RETURNING id")
            user = await conn.fetchval("INSERT INTO users (tenant_id, name) VALUES ($1,'u-cte') RETURNING id", tenant)
            key = await conn.fetchval(
                "INSERT INTO api_keys (user_id, name, key_prefix, key_hash, scopes) "
                "VALUES ($1,'k-cte','hllm-cte',$2,$3::text[]) RETURNING id",
                user,
                uuid.uuid4().hex,
                ["mcp"],
            )
            await conn.execute(
                "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
                "VALUES ($1,'srv','t1',TRUE),($1,'srv','t2',TRUE)",
                key,
            )
            # the exact shape routes_admin's propagate-grants emits
            await conn.execute(
                "WITH gone AS (DELETE FROM mcp_tool_acls WHERE key_id IN ($1)) "
                "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
                "VALUES ($1,'srv','t1',TRUE),($1,'srv','t2',TRUE) "
                "ON CONFLICT (key_id, server, tool) DO UPDATE SET allowed = TRUE",
                key,
            )
            rows = await conn.fetch(
                "SELECT server, tool, allowed FROM mcp_tool_acls WHERE key_id=$1 ORDER BY tool", key
            )
            assert [(r["tool"], r["allowed"]) for r in rows] == [("t1", True), ("t2", True)]
        finally:
            await conn.close()

    _run(go())
