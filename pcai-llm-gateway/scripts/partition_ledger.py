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
All existing indexes (usage_ledger_pkey, idx_ledger_key_ts,
idx_ledger_key_model_ts, idx_ledger_ts) are recreated on the parent and
thus per partition — the PK via ADD PRIMARY KEY, the rest via CREATE INDEX.

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
1. pre-check: refuse when the ledger is missing. An ALREADY-PARTITIONED ledger
   takes the MAINTENANCE path instead (see RE-RUN below) — never a no-op.
0. PRECONDITION (2026-11-10): boot the release that owns the CURRENT column
   set at least once BEFORE running this. The copy below is "INSERT INTO
   usage_ledger SELECT * FROM usage_ledger_legacy", which is positional: on a
   pre-migration flat table the two newest columns (billable_units, unit_kind —
   added 2026-11-10) are absent, so the SELECT yields fewer expressions than the
   target has columns. PG 18 tolerates that (the trailing columns land NULL);
   older servers reject it with "INSERT has more target columns than
   expressions". Either way it fails INSIDE the transaction and rolls back
   cleanly, but booting first avoids the confusing abort.
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

RE-RUN (2026-11-02, audit A-5) — the horizon is NOT self-maintaining
--------------------------------------------------------------------
The conversion creates [now-6 .. now+3] monthly partitions + a DEFAULT. Nothing
else in the tree ever creates another one, so from month +4 every ledger row
lands in usage_ledger_default, which (a) defeats the partition pruning the
conversion exists for and (b) BLOCKS creating that month's partition later
("updated partition constraint for default partition would be violated by some
row"). Re-running this script is therefore the MAINTENANCE action: on an
already-partitioned ledger it ensures every month from THIS month through
+PARTITIONS_FORWARD+1 exists (CREATE TABLE IF NOT EXISTS, safe to re-run) and
warns loudly when the DEFAULT partition holds any row. Run it on a schedule
(monthly is ample) — it is idempotent and never touches existing rows.

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
# partitioned parent must match COLUMN-FOR-COLUMN (name AND order — the
# copy is INSERT..SELECT *) or the copy and every existing query break.
# Verified against SCHEMA_SQL incl. every ALTER TABLE ADD COLUMN evolution
# (cached_tokens, cost_usd, billing_fraction, guardrail_events, routing_* —
# routing_probabilities 2026-10-23 was missed once, audit #13b); pinned by
# tests/test_deploy_contract.py so the next column cannot drift silently.
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
    routing_reason   TEXT NOT NULL DEFAULT '',
    routing_probabilities TEXT NOT NULL DEFAULT '',
    -- P10 unit-aware billing (schema.py appends these AFTER
    -- routing_probabilities; the positional INSERT..SELECT * copy below
    -- depends on this physical order — pinned by tests/test_deploy_contract.py)
    billable_units   NUMERIC(18,6),
    unit_kind        TEXT
"""

# Months: 6 back covers any realistic pre-migration history window being
# open (a month budget window spans at most the current month); 3 forward
# means ~a quarter of boots without anyone thinking about partitions again;
# DEFAULT catches anything outside the range instead of erroring an insert.
PARTITIONS_BACK = 6
PARTITIONS_FORWARD = 3
# One month BEYOND the conversion horizon (2026-11-02, audit A-5): a re-run of
# this script is the HORIZON MAINTENANCE action (see RE-RUN above), and it
# leaves one month of margin so a late run still cannot land rows in DEFAULT.
PARTITIONS_EXTRA = 1


def _months(first: datetime, lo: int, hi: int) -> list[tuple[datetime, datetime, str]]:
    """(from_ts, to_ts, name) for month offsets [lo .. hi] from the given first."""
    out: list[tuple[datetime, datetime, str]] = []
    for offset in range(lo, hi + 1):
        y, m = first.year, first.month + offset
        y += (m - 1) // 12
        m = (m - 1) % 12 + 1
        start = first.replace(year=y, month=m)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = start.replace(year=ny, month=nm)
        out.append((start, end, f"usage_ledger_{start.strftime('%Y_%m')}"))
    return out


def _month_bounds(now: datetime) -> list[tuple[datetime, datetime, str]]:
    """(from_ts, to_ts, name) for [PARTITIONS_BACK .. +FORWARD] months."""
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _months(first, -PARTITIONS_BACK, PARTITIONS_FORWARD)


def _horizon_bounds(now: datetime) -> list[tuple[datetime, datetime, str]]:
    """The partitions a (re-)run must ENSURE exist: this month through one
    month BEYOND the conversion horizon.

    The conversion range above is a one-off; this is the range a RE-RUN
    guarantees. A month that already exists is a no-op under IF NOT EXISTS, so
    re-running only ever pushes the horizon forward.
    """
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return _months(first, 0, PARTITIONS_FORWARD + PARTITIONS_EXTRA)


def _default_warning(rows: int | None) -> str | None:
    """The loud warning for a non-empty DEFAULT partition, or None.

    A row in usage_ledger_default is a row OUTSIDE the monthly horizon: it
    defeats partition pruning, and while it is there Postgres REFUSES to create
    that month's partition ("updated partition constraint for default partition
    would be violated by some row"). Pure, so the runbook pin can assert the
    text without a database.
    """
    if not rows:
        return None
    return (
        f"usage_ledger_default holds {rows:,} row(s) — those months are OUTSIDE the partition "
        f"horizon. Partition pruning does NOT apply to them, and creating the partition for such "
        f"a month FAILS while they are there (updated partition constraint for default partition "
        f"would be violated by some row). Move them out first: create the month's partition, "
        f"INSERT INTO usage_ledger_<YYYY_MM> SELECT * FROM usage_ledger_default WHERE ts >= ...; "
        f"DELETE FROM usage_ledger_default WHERE ts >= ...; then re-run this script."
    )


async def _default_partition_rows(conn) -> int | None:
    """Row count in the DEFAULT partition; None when it does not exist."""
    kind = await conn.fetchval(
        "SELECT relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = 'usage_ledger_default'"
    )
    kind_str = kind.decode() if isinstance(kind, bytes) else kind
    if kind_str is None:
        return None
    return int(await conn.fetchval("SELECT count(*) FROM usage_ledger_default") or 0)


async def _ensure_horizon(conn, now: datetime) -> None:
    """Ensure every month through the forward horizon exists, then report the
    DEFAULT partition. IDEMPOTENT and safe to re-run at any time.

    Each statement is attempted on its own: a month whose rows already landed
    in DEFAULT raises (see _default_warning) and MUST NOT abort the remaining
    months — the failure IS the signal, so it is logged loudly and the loop
    continues.
    """
    ensured = 0
    horizon = _horizon_bounds(now)
    for start, end, name in horizon:
        try:
            await conn.execute(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF usage_ledger "
                f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}');"
            )
            ensured += 1
        except Exception as exc:
            log.warning(
                "could not ensure partition %s (%s) — rows for that month are already in "
                "usage_ledger_default; move them out and re-run (see the warning below)",
                name,
                exc,
            )
    log.info("horizon ensured through %s (%d month statement(s))", horizon[-1][2], ensured)
    warning = _default_warning(await _default_partition_rows(conn))
    if warning:
        log.warning(warning)


async def _precheck(conn) -> tuple[bool, int]:
    """(already_partitioned, approx_rows) — refuses the wrong starting state."""
    kind = await conn.fetchval(
        """
        SELECT CASE WHEN c.relkind IN ('p', 'r') THEN c.relkind ELSE NULL END
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname = 'usage_ledger'
        """
    )
    # asyncpg returns the single-char "char" type as BYTES (b'p') — the same
    # trap _postcheck documents and decodes ("live-tested: a plain != 'p'
    # false-negatives a SUCCESSFUL migration"). Undecoded here, the
    # already-partitioned guard below NEVER matched, so a re-run took the
    # ACCESS EXCLUSIVE lock and failed mid-migration on "relation already
    # exists" instead of being the no-op it claims to be.
    kind_str = kind.decode() if isinstance(kind, bytes) else kind
    if kind_str is None:
        raise SystemExit("usage_ledger does not exist — nothing to partition (boot the gateway once first)")
    if kind_str == "p":
        # ALREADY partitioned is NOT "nothing to do" (2026-11-02, audit A-5):
        # main() takes the MAINTENANCE path instead — ensure the forward
        # horizon and report a non-empty DEFAULT. Raising here made every
        # re-run a silent no-op, which is how the horizon quietly expired.
        return True, 0
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
    # collide with the legacy table's (live-tested). That includes the
    # PRIMARY KEY's index — still named usage_ledger_pkey after the table
    # rename, so the parent's ADD PRIMARY KEY below dies with "relation
    # already exists" (audit #13a). Rename first — they die with the
    # legacy table (or survive with --keep-legacy, harmlessly suffixed).
    legacy_index_renames = "\n".join(
        f"ALTER INDEX IF EXISTS {idx} RENAME TO {idx}_legacy;"
        for idx in ("usage_ledger_pkey", "idx_ledger_key_ts", "idx_ledger_key_model_ts", "idx_ledger_ts")
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
    CREATE INDEX idx_ledger_ts            ON usage_ledger (ts DESC);
    {parts_sql}
    CREATE TABLE usage_ledger_default PARTITION OF usage_ledger DEFAULT;
    -- ORDER BY id keeps the copy's index build sequential (cheaper, and the
    -- sequence continuity above makes it deterministic).
    INSERT INTO usage_ledger SELECT * FROM usage_ledger_legacy ORDER BY id;
    -- Status-vocabulary CHECK: the flat table's ck_ledger_status dies with
    -- the legacy DROP and SCHEMA_SQL only restores it on the NEXT boot —
    -- while this script's closing line promises the gateway needs no
    -- restart. AFTER the copy, deliberately (wave-2 review): NOT VALID
    -- skips only the INITIAL scan — every INSERT is still checked, and
    -- the legacy table's own CHECK was added NOT VALID too (schema.py),
    -- so a pre-constraint row carrying a retired status would abort the
    -- copy mid-swap and burn the quiet window. The copy runs
    -- unconstrained; the stamp below trusts the copied rows exactly as
    -- the boot DDL trusts existing ones. ALTER TABLE on the parent
    -- recurses to the partitions created above; verbatim from schema.py
    -- (cross-val on audit #13; pinned by tests/test_deploy_contract.py).
    ALTER TABLE usage_ledger DROP CONSTRAINT IF EXISTS ck_ledger_status;
    ALTER TABLE usage_ledger ADD CONSTRAINT ck_ledger_status
        CHECK (status IN ('ok','error','cache_hit','budget_exceeded','rate_limited','guardrail_blocked','policy_blocked','denied','routing')) NOT VALID;
    """
    if keep_legacy:
        # The legacy table keeps owning the sequence in this mode — the parent
        # default still reads it (both owners write distinct values via the
        # same counter; sequence ownership only affects DROP behavior).
        sql += (
            "    -- --keep-legacy: legacy table survives; it still owns the id "
            "sequence (DROP it later with DROP TABLE usage_ledger_legacy;\n"
            "    -- the parent's default keeps working — sequences outlive "
            "ownership changes).\n"
        )
    else:
        # Legacy table dropped: its sequence OWNERSHIP dies with it, but the
        # sequence object itself was created by BIGSERIAL for the ORIGINAL
        # table — verify the default survives and re-point it if needed.
        sql += (
            "    DROP TABLE usage_ledger_legacy;\n"
            "    -- Re-assert the id default AFTER the legacy drop: dropping the "
            "owning table drops its serial sequence too (owned dependency).\n"
            "    CREATE SEQUENCE IF NOT EXISTS usage_ledger_id_seq;\n"
            "    SELECT setval('usage_ledger_id_seq', "
            "COALESCE((SELECT max(id) FROM usage_ledger), 0) + 1, false);\n"
            "    ALTER TABLE usage_ledger ALTER COLUMN id "
            "SET DEFAULT nextval('usage_ledger_id_seq');\n"
        )
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
    log.info("plan: %d monthly partitions (%s .. %s) + DEFAULT", len(bounds) + 1, bounds[0][2], bounds[-1][2])
    conn = await asyncpg_connect(dsn)
    try:
        already, rows = await _precheck(conn)
        if already:
            # MAINTENANCE PATH (2026-11-02, audit A-5): an already-partitioned
            # ledger still needs its forward horizon pushed out, or rows start
            # landing in usage_ledger_default — which defeats pruning and then
            # BLOCKS creating that month's partition. Idempotent; safe to re-run.
            horizon = _horizon_bounds(datetime.now(UTC))
            log.info(
                "usage_ledger is ALREADY partitioned — ensuring the horizon %s .. %s",
                horizon[0][2],
                horizon[-1][2],
            )
            if not apply:
                log.info("DRY-RUN — re-run with --apply to create any missing month and report a non-empty DEFAULT")
                return
            await _ensure_horizon(conn, datetime.now(UTC))
            log.info("DONE — horizon maintained; nothing else was touched")
            return
        log.info("usage_ledger: %s rows", f"{rows:,}")
        if not apply:
            log.info("DRY-RUN — re-run with --apply to execute")
            return
        async with conn.transaction():
            await _migrate(conn, keep_legacy)
        await _postcheck(conn, rows, keep_legacy)
        # The conversion range stops at +FORWARD; push the horizon one month
        # past it so a fresh run leaves the same margin a re-run does.
        await _ensure_horizon(conn, datetime.now(UTC))
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
