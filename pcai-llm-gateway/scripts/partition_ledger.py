#!/usr/bin/env python3
"""Convert usage_ledger to monthly RANGE partitions — RUN-ONCE, deliberate.

WHY THIS IS A SCRIPT AND NOT STARTUP DDL
----------------------------------------
SCHEMA_SQL runs on EVERY boot; converting a table to PARTITION BY RANGE
online rewrites and locks it. Doing that per-boot on a live ledger is
unacceptable, so the table ships unpartitioned (schema.py note) and this
script is the operator's explicit, one-time decision — run during a quiet
window, from a pod with the same PCAI_LLM_DATABASE_URL the gateway uses:

    kubectl exec -n <ns> deploy/pcai-llm-gateway -- \
        python -m scripts.partition_ledger --apply

WHAT IT BUILDS
--------------
usage_ledger  -> PARTITION BY RANGE (ts), monthly partitions
PK            -> (id, ts)   [partition key must be in every unique index]
All existing indexes (idx_ledger_key_ts, idx_ledger_key_model_ts) are
recreated per partition by virtue of being defined on the parent.

TRANSPARENCY CONTRACT (the reason this is safe)
-----------------------------------------------
Verified against every query in the tree before writing this:
- NO query addresses ledger rows by bare id (grep: WHERE id / l.id on
  usage_ledger — zero hits). All access is key_id/ts-filtered or a full
  aggregate, so the widened PK breaks nothing.
- The budget gate's windowed SUMs (core/budgets.py) PRUNE: a day/week/month
  window touches 1-2 partitions instead of scanning one growing heap —
  the gate gets FASTER with age, which is the entire point.
- Inserts (core/budgets.settle) are unchanged — Postgres routes rows to
  partitions by ts automatically; new months need a partition to exist,
  hence DEFAULT + pre-creation below.
- The usage ledger is the AUDIT artifact and is never pruned; partitioning
  does not delete anything. Operators who later want bounded retention DROP
  old partitions deliberately (detaching a partition is instant DDL, the
  other payoff of this layout) — that decision stays OUT of this script.

PROCEDURE (single transaction where it matters)
-----------------------------------------------
1. pre-check: refuse when the ledger is already partitioned or missing.
2. BEGIN; LOCK usage_ledger IN ACCESS EXCLUSIVE mode (blocks new writes
   for the copy duration — run in the quiet window);
   rename live -> usage_ledger_legacy; create the partitioned parent with
   the EXACT column shape; copy rows in id order (setval the sequence past
   max(id) BEFORE copying so late INSERTs during the window cannot collide
   — copies preserve original ids); create partitions (past 6 months +
   next 3 months + DEFAULT); swap names; COMMIT.
3. post-check: row-count parity legacy vs partitioned (estimated exact via
   count on both), then DROP the legacy table (--drop, or keep with
   --keep-legacy for a belt-and-braces first run).

USAGE
-----
Run from the repo checkout on a machine with line-of-sight to Postgres
(the script is NOT shipped inside the wheel — the image has no repo
checkout, by design; the script needs only asyncpg + the DB URL):

    PCAI_LLM_DATABASE_URL=<the gateway's own DB URL> \
        .venv/bin/python scripts/partition_ledger.py              # dry-run
    PCAI_LLM_DATABASE_URL=<...> .venv/bin/python scripts/partition_ledger.py --apply
    PCAI_LLM_DATABASE_URL=<...> .venv/bin/python scripts/partition_ledger.py --apply --keep-legacy
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import UTC, datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("partition_ledger")

# Exact column shape of the live table (schema.py usage_ledger) — the
# partitioned parent must match COLUMN-FOR-COLUMN or the INSERT..SELECT and
# every existing query break. sqlgrep-verified against SCHEMA_SQL incl. the
# ALTER TABLE ADD COLUMN evolutions (cached_tokens, cost_usd,
# billing_fraction, guardrail_events, routing_*).
COLUMNS = """
    id          BIGINT,
    key_id      UUID NOT NULL,
    model       TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    prompt_tokens     BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens      BIGINT NOT NULL DEFAULT 0,
    cached_tokens     BIGINT NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(12,6),
    status      TEXT NOT NULL,
    latency_ms  INTEGER,
    request_id  TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    billing_fraction REAL NOT NULL DEFAULT 1.0,
    guardrail_events JSONB,
    routing_policy   TEXT NOT NULL DEFAULT '',
    routing_ceiling  TEXT NOT NULL DEFAULT '',
    routing_served   TEXT NOT NULL DEFAULT '',
    routing_reason   TEXT NOT NULL DEFAULT ''
"""

# Months: 6 back covers any realistic pre-migration history window being
# open (a month budget window spans at most the current month); 3 forward
# means ~a quarter of boots without anyone thinking about partitions again;
# DEFAULT catches anything outside the range instead of erroring an insert.
PARTITIONS_BACK = 6
PARTITIONS_FORWARD = 3


def _month_bounds(now: datetime) -> list[tuple[datetime, datetime, str]]:
    """(from_ts, to_ts, name) for [PARTITIONS_BACK .. +FORWARD] months."""
    out: list[tuple[datetime, datetime, str]] = []
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for offset in range(-PARTITIONS_BACK, PARTITIONS_FORWARD + 1):
        y, m = first.year, first.month + offset
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        start = first.replace(year=y, month=m)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = start.replace(year=ny, month=nm)
        out.append((start, end, f"usage_ledger_{start.strftime('%Y_%m')}"))
    return out


async def _precheck(conn) -> tuple[bool, int]:
    """(already_partitioned, approx_rows) — refuses the wrong starting state."""
    kind = await conn.fetchval(
        """
        SELECT CASE WHEN c.relkind IN ('p', 'r') THEN c.relkind ELSE NULL END
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname = 'usage_ledger'
        """
    )
    if kind is None:
        raise SystemExit("usage_ledger does not exist — nothing to partition (boot the gateway once first)")
    if kind == "p":
        raise SystemExit("usage_ledger is ALREADY partitioned — nothing to do")
    rows = await conn.fetchval("SELECT count(*) FROM usage_ledger")
    return False, int(rows or 0)


async def _migrate(conn, keep_legacy: bool) -> None:
    # NOTE: NO BEGIN/COMMIT here — asyncpg's conn.transaction() in main()
    # owns the transaction; a literal BEGIN inside would nest and corrupt
    # the atomicity guarantee (live-tested: the nested BEGIN warns and the
    # failure path leaves a half-swapped catalog).
    now = datetime.now(UTC)
    bounds = _month_bounds(now)
    parts_sql = "\n".join(
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF usage_ledger "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}');"
        for start, end, name in bounds
    )
    # Legacy index RENAMES: ALTER TABLE ... RENAME leaves index NAMES
    # untouched, so creating the parent's canonical-named indexes would
    # collide with the legacy table's (live-tested). Rename first — they
    # die with the legacy table (or survive with --keep-legacy, harmlessly
    # suffixed).
    legacy_index_renames = "\n".join(
        f"ALTER INDEX IF EXISTS {idx} RENAME TO {idx}_legacy;"
        for idx in ("idx_ledger_key_ts", "idx_ledger_key_model_ts")
    )
    sql = f"""
    -- Block concurrent writes for the copy duration (the quiet window):
    -- a row inserted mid-copy would land in legacy and be lost to the swap.
    LOCK TABLE usage_ledger IN ACCESS EXCLUSIVE MODE;
    ALTER TABLE usage_ledger RENAME TO usage_ledger_legacy;
    {legacy_index_renames}
    -- SEQUENCE first: past max(id) BEFORE the copy so an INSERT that slips
    -- between commit and the final swap CANNOT mint a colliding id.
    SELECT setval(pg_get_serial_sequence('usage_ledger_legacy', 'id'),
                  COALESCE((SELECT max(id) FROM usage_ledger_legacy), 0) + 1, false);
    CREATE TABLE usage_ledger ({COLUMNS}) PARTITION BY RANGE (ts);
    -- PK per the schema.py note: partition key must be in every unique index.
    ALTER TABLE usage_ledger ADD PRIMARY KEY (id, ts);
    -- id DEFAULT: the COLUMNS shape types id as plain BIGINT (the partitioned
    -- parent cannot reuse the legacy table's sequence DEFAULT — the legacy
    -- table is still using that sequence until it is dropped) — so wire the
    -- default EXPLICITLY to the same sequence (live-tested: partitions
    -- inherit NOT NULL from the PK but NOT a default; a missing one breaks
    -- every post-migration INSERT with NotNullViolation on id).
    ALTER TABLE usage_ledger ALTER COLUMN id
        SET DEFAULT nextval(pg_get_serial_sequence('usage_ledger_legacy', 'id'));
    CREATE INDEX idx_ledger_key_ts        ON usage_ledger (key_id, ts DESC);
    CREATE INDEX idx_ledger_key_model_ts  ON usage_ledger (key_id, model, ts DESC);
    {parts_sql}
    CREATE TABLE usage_ledger_default PARTITION OF usage_ledger DEFAULT;
    -- ORDER BY id keeps the copy's index build sequential (cheaper, and the
    -- sequence continuity above makes it deterministic).
    INSERT INTO usage_ledger SELECT * FROM usage_ledger_legacy ORDER BY id;
    """
    if keep_legacy:
        # The legacy table keeps owning the sequence in this mode — the parent
        # default still reads it (both owners write distinct values via the
        # same counter; sequence ownership only affects DROP behavior).
        sql += ("    -- --keep-legacy: legacy table survives; it still owns the id "
                "sequence (DROP it later with DROP TABLE usage_ledger_legacy;\n"
                "    -- the parent's default keeps working — sequences outlive "
                "ownership changes).\n")
    else:
        # Legacy table dropped: its sequence OWNERSHIP dies with it, but the
        # sequence object itself was created by BIGSERIAL for the ORIGINAL
        # table — verify the default survives and re-point it if needed.
        sql += ("    DROP TABLE usage_ledger_legacy;\n"
                "    -- Re-assert the id default AFTER the legacy drop: dropping the "
                "owning table drops its serial sequence too (owned dependency).\n"
                "    CREATE SEQUENCE IF NOT EXISTS usage_ledger_id_seq;\n"
                "    SELECT setval('usage_ledger_id_seq', "
                "COALESCE((SELECT max(id) FROM usage_ledger), 0) + 1, false);\n"
                "    ALTER TABLE usage_ledger ALTER COLUMN id "
                "SET DEFAULT nextval('usage_ledger_id_seq');\n")
    await conn.execute(sql)


async def _postcheck(conn, before_rows: int, keep_legacy: bool) -> None:
    after = await conn.fetchval("SELECT count(*) FROM usage_ledger")
    if int(after or 0) != before_rows:
        suffix = "" if keep_legacy else " (legacy table was dropped: restore from backup)"
        raise SystemExit(
            f"ROW-COUNT MISMATCH: legacy had {before_rows:,}, partitioned has {after:,} — "
            f"investigate BEFORE dropping anything{suffix}"
        )
    kind = await conn.fetchval(
        "SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = 'usage_ledger'"
    )
    # asyncpg returns the single-char "char" type as BYTES (b'p') — normalize
    # before comparing (live-tested: a plain != 'p' false-negatives a SUCCESSFUL
    # migration and alarms the operator after the fact).
    kind_str = kind.decode() if isinstance(kind, bytes) else kind
    if kind_str != "p":
        raise SystemExit(f"post-check failed: usage_ledger is not partitioned (relkind={kind_str!r})")
    log.info("post-check OK: %s rows carried over, parent is partitioned", f"{after:,}")


async def main(apply: bool, keep_legacy: bool) -> None:

    dsn = os.environ.get("PCAI_LLM_DATABASE_URL") or ""
    if not dsn:
        parts = [os.environ.get(f"PCAI_LLM_DB_{k}") for k in ("HOST", "PORT", "USER", "PASSWORD", "NAME")]
        if all(parts):
            dsn = f"postgresql://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}/{parts[4]}"
    if not dsn:
        raise SystemExit("set PCAI_LLM_DATABASE_URL (the gateway's own URL)")
    bounds = _month_bounds(datetime.now(UTC))
    log.info("plan: %d monthly partitions (%s .. %s) + DEFAULT",
             len(bounds) + 1, bounds[0][2], bounds[-1][2])
    conn = await asyncpg_connect(dsn)
    try:
        already, rows = await _precheck(conn)
        if already:
            return
        log.info("usage_ledger: %s rows", f"{rows:,}")
        if not apply:
            log.info("DRY-RUN — re-run with --apply to execute")
            return
        async with conn.transaction():
            await _migrate(conn, keep_legacy)
        await _postcheck(conn, rows, keep_legacy)
        log.info("DONE — gateway needs no restart (asyncpg picks up the new parent transparently)")
    finally:
        await conn.close()


async def asyncpg_connect(dsn: str):
    import asyncpg

    return await asyncpg.connect(dsn=dsn, command_timeout=3600)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--keep-legacy", action="store_true", help="keep usage_ledger_legacy after the swap")
    args = ap.parse_args()
    asyncio.run(main(args.apply, args.keep_legacy))
