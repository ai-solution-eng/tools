"""Async database access layer (asyncpg, pool created at startup).

Pool sizing comes from Settings (env PCAI_LLM_DB_POOL_MIN/MAX) rather than
being hardcoded: the hot path makes 14-27 serial round-trips per request,
so a too-small pool IS the throughput ceiling (perf audit). Hot-path
acquisition (fetch/fetchrow/fetchval/execute) uses a BOUNDED wait — a
request that cannot get a connection inside settings.db_acquire_timeout_s
raises PoolUnavailable, which the app maps to a fast 503 instead of an
unbounded queue.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from ..config import settings
from .schema import SCHEMA_SQL

log = logging.getLogger(__name__)

# Advisory threshold for the unpartitioned-ledger warning (rows). A ledger
# under this size answers the budget gate's windowed SUMs comfortably off
# the (key_id, ts) index; past it, conversion to monthly partitions
# (scripts/partition_ledger.py) stops the heap scan cost from growing with
# total table age. Deliberately generous — the nudge is about growth
# PLANNING, not an emergency.
_LEDGER_PARTITION_WARN_ROWS = 50_000_000


class PoolUnavailable(RuntimeError):
    """Hot-path acquisition exceeded the bounded wait (pool exhausted).

    Surfaced to clients as 503 by the app-level handler — fast failure is
    the point: backpressure belongs at the load balancer, not in a silent
    per-request queue.
    """


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await self._make_pool()
        async with self.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
        log.info("database connected and schema ensured")
        # Advisory, never fatal: a partitioning/size problem must not take the
        # gateway down (the ledger functions fine unpartitioned — it's a
        # growth-planning signal, not a health check).
        try:
            await self._check_ledger_partitioning()
        except Exception:
            log.debug("ledger partitioning check skipped", exc_info=True)

    @staticmethod
    async def _check_ledger_partitioning() -> None:
        """Warn when the usage ledger is UNPARTITIONED and growing large.

        The ledger is created unpartitioned (schema.py note); conversion to
        monthly RANGE partitions is a deliberate run-once migration
        (scripts/partition_ledger.py), never startup DDL — an online
        conversion locks/rewrites the table, unacceptable per-boot. This
        checker is the operator's nudge: past the row threshold, the budget
        gate's SUMs and the admin rollups pay for the unpartitioned heap on
        every request. One query against pg_class (estimated rows — cheap,
        no table scan), fired once per boot.
        """
        assert db.pool is not None
        row = await db.pool.fetchrow(
            """
            SELECT c.reltuples::bigint AS est_rows
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema() AND c.relname = 'usage_ledger'
            """
        )
        est = int(row["est_rows"] or 0) if row else 0
        if est >= _LEDGER_PARTITION_WARN_ROWS:
            log.warning(
                "usage_ledger has ~%s rows and is UNPARTITIONED — run "
                "scripts/partition_ledger.py to convert it to monthly RANGE "
                "partitions (transparent to all queries; see schema.py note)",
                f"{est:,}",
            )

    @staticmethod
    def _connect_kwargs() -> dict[str, Any]:
        """Resolve the DB connection config: full URL XOR discrete fields.

        Fail LOUD at startup on incomplete configuration — a gateway that
        half-knows its database must never boot (same fail-closed posture
        as key auth).
        """
        if settings.database_url:
            return {"dsn": settings.database_url}
        parts = {
            "host": settings.db_host,
            "port": settings.db_port,
            "user": settings.db_user,
            "password": settings.db_password,
            "database": settings.db_name,
        }
        if all(str(v) for v in parts.values()):
            return parts
        missing = [k for k, v in parts.items() if not str(v)]
        raise RuntimeError(
            "database configuration incomplete: set PCAI_LLM_DATABASE_URL "
            f"(URL mode) OR all of PCAI_LLM_DB_* (discrete mode); missing: {missing}"
        )

    async def _make_pool(self) -> asyncpg.Pool:
        # ⚠️ STARTUP RACE HARDENING (2026-10-08, seen on every G2 rollout
        # since the namespace went meshed): the app's FIRST outbound PG
        # connect can race the Istio sidecar's outbound-listener
        # programming — the socket gets refused while envoy is still
        # warming, db.connect() (fail-loud, no retry) exits 3, and the pod
        # needs exactly one kubelet restart before booting clean. Never
        # repeats after first boot — but every deploy costs a crash event,
        # probe warnings, and an HPA metrics blip. Hold-and-retry for a
        # bounded window instead: ConnectionRefused/OSError during pool
        # creation retries a few times with short backoff BEFORE the
        # fail-loud exit (which stays — a genuinely wrong DSN must still
        # crash fast and visibly; only the TRANSIENT boot window gets
        # absorbed). ~3-4s worst-case added boot time, only on the racy
        # path (healthy clusters connect on attempt 1 and pay nothing).
        attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await asyncpg.create_pool(
                    **self._connect_kwargs(),
                    min_size=settings.db_pool_min,
                    max_size=settings.db_pool_max,
                    command_timeout=30,
                )
            except (OSError, asyncpg.PostgresError) as exc:
                last_exc = exc
                if attempt == attempts:
                    break
                delay = 1.5 * attempt
                log.warning(
                    "db pool creation refused (attempt %d/%d) — retrying in %.1fs "
                    "(mesh sidecar warm-up race; a wrong DSN still fails loud after the window)",
                    attempt,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def acquire(self):
        """Raw pool acquisition context manager (UNBOUNDED wait).

        For explicit transaction blocks (admin writes); hot-path reads use
        the bounded acquire below.
        """
        if self.pool is None:
            raise RuntimeError("database not connected")
        return self.pool.acquire()

    @asynccontextmanager
    async def acquire_bounded(self) -> AsyncIterator[asyncpg.Connection]:
        """Hot-path acquisition with a bounded wait (perf audit).

        pool.acquire() alone blocks indefinitely under exhaustion — every
        request queues and p99 latency explodes silently. Here the wait is
        capped at settings.db_acquire_timeout_s and exhaustion surfaces as
        PoolUnavailable (-> 503).
        """
        if self.pool is None:
            raise RuntimeError("database not connected")
        timeout = settings.db_acquire_timeout_s
        try:
            conn = await asyncio.wait_for(self.pool.acquire(), timeout=timeout)
        except TimeoutError:  # asyncio.TimeoutError is an alias since 3.11
            raise PoolUnavailable(f"database pool exhausted: no connection within {timeout}s") from None
        try:
            yield conn
        finally:
            await self.pool.release(conn)

    async def fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        async with self.acquire_bounded() as conn:
            return await conn.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> asyncpg.Record | None:
        async with self.acquire_bounded() as conn:
            return await conn.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async with self.acquire_bounded() as conn:
            return await conn.fetchval(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        async with self.acquire_bounded() as conn:
            return await conn.execute(sql, *args)


db = Database()
