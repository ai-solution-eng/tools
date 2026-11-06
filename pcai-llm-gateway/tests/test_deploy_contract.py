"""Deploy/ops contract pins — no database, no helm binary required.

Two drift guards that both failed live (AUDIT-r5):

1. PY311 SYNTAX FLOOR (audit #40): pyproject declares
   requires-python = ">=3.11", but PEP 695 type-parameter lists
   (def f[T](...)) are Python 3.12+ SYNTAX — a dev box on 3.14 imports
   the drift fine and only the oldest supported interpreter explodes.
   ast.parse(feature_version=(3, 11)) over every src file is the gate.

2. PARTITION-RUNBOOK PARITY (audit #13): scripts/partition_ledger.py
   hardcodes the ledger's column shape and index names while SCHEMA_SQL
   keeps evolving underneath it. Two live fatals: routing_probabilities
   (added 2026-10-23) was missing from COLUMNS, so the INSERT..SELECT *
   copy died on arity; usage_ledger_pkey was missing from the legacy
   index renames, so the parent's ADD PRIMARY KEY collided with the
   index name a table RENAME does not rename. Compare the script against
   the evolved schema so the next drift is a red test, not a dead
   runbook.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from pcai_llm.db.schema import SCHEMA_SQL

REPO_ROOT = Path(__file__).resolve().parents[1]
PARTITION_SCRIPT = REPO_ROOT / "scripts" / "partition_ledger.py"


# ---------------------------------------------------------------------------
# 1. the py311 syntax floor (audit #40)
# ---------------------------------------------------------------------------


class TestPy311SyntaxFloor:
    def test_every_src_file_parses_at_feature_version_3_11(self) -> None:
        """ast.parse(feature_version=(3, 11)) over all of src/ — the exact
        check that flagged routes_mcp.py's PEP 695 _upstream_session[T]
        (3.12+ syntax under a >=3.11 floor)."""
        failures: list[str] = []
        for path in sorted((REPO_ROOT / "src").rglob("*.py")):
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 11))
            except SyntaxError as e:
                failures.append(f"{path.relative_to(REPO_ROOT)}:{e.lineno}: {e.msg}")
        assert failures == [], "3.11-incompatible syntax under requires-python >=3.11:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# 2. partition-runbook parity (audit #13)
# ---------------------------------------------------------------------------


def _script_columns() -> list[str]:
    """Column names, in order, from partition_ledger.py's COLUMNS block —
    the parent shape the INSERT..SELECT * copy is positional against."""
    text = PARTITION_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'COLUMNS = """(.*?)"""', text, re.DOTALL)
    assert m, "COLUMNS block not found in scripts/partition_ledger.py"
    cols = re.findall(r"^\s*(\w+)\s+[A-Z]", m.group(1), re.MULTILINE)
    assert cols, "COLUMNS block parsed empty"
    return cols


def _schema_ledger_columns() -> list[str]:
    """The evolved usage_ledger column set IN PHYSICAL ORDER for a fresh
    database: CREATE TABLE column order, then each ADD COLUMN evolution
    appended. (The IF NOT EXISTS re-adds of columns already inline —
    cached_tokens/cost_usd — are no-ops on a fresh boot and must not move
    position.) That physical order is what SELECT * returns, i.e. what
    the parent's COLUMNS must match for the positional copy."""
    m = re.search(r"CREATE TABLE IF NOT EXISTS usage_ledger \((.*?)\n\);", SCHEMA_SQL, re.DOTALL)
    assert m, "usage_ledger CREATE TABLE not found in SCHEMA_SQL"
    cols: list[str] = []
    for name in re.findall(r"^\s{4}([a-z_][a-z0-9_]*)\s+[A-Z]", m.group(1), re.MULTILINE):
        if name not in cols:
            cols.append(name)
    for name in re.findall(r"ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS ([a-z_][a-z0-9_]*)", SCHEMA_SQL):
        if name not in cols:
            cols.append(name)
    return cols


def _script_legacy_index_renames() -> set[str]:
    """The indexes partition_ledger.py renames on the legacy table."""
    text = PARTITION_SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"for idx in \(([^)]*)\)", text, re.DOTALL)
    assert m, "legacy_index_renames tuple not found in scripts/partition_ledger.py"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _schema_ledger_indexes() -> set[str]:
    """Every index name SCHEMA_SQL can create on usage_ledger: the explicit
    CREATE INDEXes plus the implicit PK index (a table RENAME does not
    rename its indexes, so the legacy table keeps ALL of these names)."""
    idx = set(re.findall(r"CREATE INDEX IF NOT EXISTS (\w+) ON usage_ledger\(", SCHEMA_SQL))
    idx.add("usage_ledger_pkey")
    return idx


class TestPartitionLedgerParity:
    def test_columns_match_evolved_schema_in_order(self) -> None:
        """The parent's COLUMNS must equal the evolved schema column-for-
        column AND position-for-position: the copy is INSERT..SELECT *,
        which Postgres matches POSITIONALLY (audit #13b —
        routing_probabilities shipped 2026-10-23 and the arity-19 parent
        failed the copy outright)."""
        assert _script_columns() == _schema_ledger_columns()

    def test_legacy_index_renames_cover_every_ledger_index(self) -> None:
        """Every index the legacy table keeps — INCLUDING usage_ledger_pkey
        (a table RENAME does not rename its indexes; audit #13a) — must be
        renamed before the parent's canonical-named indexes are created,
        or ADD PRIMARY KEY / CREATE INDEX dies with 'already exists'."""
        assert _script_legacy_index_renames() == _schema_ledger_indexes()

    def test_parent_recreates_every_secondary_index(self) -> None:
        """The partitioned parent must recreate every secondary index the
        flat table had — idx_ledger_ts rode the rollups (audit P1-4) and
        was silently dropped by a parent that recreates only the two
        (key_id, ts) indexes."""
        text = PARTITION_SCRIPT.read_text(encoding="utf-8")
        created = set(re.findall(r"CREATE INDEX (\w+)\s+ON usage_ledger \(", text))
        assert created == _schema_ledger_indexes() - {"usage_ledger_pkey"}

    def test_status_vocabulary_check_survives_the_swap(self) -> None:
        """The flat table's ck_ledger_status dies with the legacy DROP and
        SCHEMA_SQL only restores it on the NEXT boot — while the script's
        closing line promises the gateway needs no restart. The migration
        must stamp the constraint on the parent itself, NOT VALID like the
        boot DDL, with the SAME status vocabulary (verbatim copy —
        cross-val on audit #13) — and it must do so AFTER the copy INSERT:
        NOT VALID skips only the initial scan, every INSERT is still
        checked, and the legacy rows were never validated (their own CHECK
        was added NOT VALID too) — a pre-copy stamp aborts the copy
        mid-swap on the first retired status (wave-2 review)."""
        text = PARTITION_SCRIPT.read_text(encoding="utf-8")
        m = re.search(r"ck_ledger_status\s+CHECK \(status IN \(([^)]+)\)\) NOT VALID", text)
        assert m, "ck_ledger_status constraint missing from the migration SQL"
        schema_m = re.search(r"ck_ledger_status\s+CHECK \(status IN \(([^)]+)\)\) NOT VALID", SCHEMA_SQL)
        assert schema_m, "ck_ledger_status not found in SCHEMA_SQL (renamed/moved?)"
        script_vocabulary = re.findall(r"'([^']+)'", m.group(1))
        schema_vocabulary = re.findall(r"'([^']+)'", schema_m.group(1))
        assert script_vocabulary == schema_vocabulary
        # PLACEMENT pin (the part presence alone cannot see): the stamp must
        # follow the copy INSERT — statement ORDER, not mere existence.
        copy_at = text.find("INSERT INTO usage_ledger SELECT * FROM usage_ledger_legacy")
        stamp_at = text.find("ALTER TABLE usage_ledger ADD CONSTRAINT ck_ledger_status")
        assert copy_at != -1 and stamp_at != -1
        assert stamp_at > copy_at, (
            "ck_ledger_status stamped BEFORE the copy INSERT — NOT VALID does not cover copied rows"
        )
        assert text.count("ADD CONSTRAINT ck_ledger_status") == 1, (
            "constraint stamped twice (leftover at the pre-copy position)"
        )


# ---------------------------------------------------------------------------
# 3. agent_traces read indexes (audit A-3, 2026-11-02)
# ---------------------------------------------------------------------------


class TestTraceReadIndexes:
    def test_unfiltered_explorer_has_a_ts_leading_index(self) -> None:
        """The admin/team traces listing filters on ts ONLY (its `since`
        defaults to 30 days) and then groups by request_id. With no
        ts-leading index that is a sequential scan + hash aggregate over a
        table that is NEVER pruned by default (retention_trace_days = 0), on
        the pool the data plane shares. Same defect class as idx_ledger_ts
        (audit P1-4), which was added for exactly this reason."""
        # whitespace-normalized: the schema aligns index names in a column
        compact = " ".join(SCHEMA_SQL.split())
        assert "CREATE INDEX IF NOT EXISTS idx_traces_ts ON agent_traces(ts DESC);" in compact, (
            "agent_traces needs a ts-leading index for the unfiltered trace explorer"
        )

    def test_conversation_read_index_is_key_thread_ts(self) -> None:
        """The conversation-grouped read (owner request, 2026-11-02) selects
        the N most recent threads under the actor filters and then reads every
        row of them, ordered by ts - a (key_id, thread_id, ts) shape, the same
        one effective_prompts already uses for its thread reads."""
        assert (
            "CREATE INDEX IF NOT EXISTS idx_traces_key_thread_ts ON agent_traces(key_id, thread_id, ts DESC);"
            in SCHEMA_SQL
        ), "the conversation read needs (key_id, thread_id, ts DESC)"


# ---------------------------------------------------------------------------
# 4. partition HORIZON maintenance (audit A-5, 2026-11-02)
# ---------------------------------------------------------------------------


def _partition_module():
    """Import scripts/partition_ledger.py as a module.

    It is a standalone script (not part of the installed package), so it is
    loaded by path. Module-level logging.basicConfig is a no-op under pytest
    (the root logger already has handlers).
    """
    spec = importlib.util.spec_from_file_location("partition_ledger_under_test", PARTITION_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeConn:
    """The two reads/writes _precheck/_ensure_horizon issue, nothing more."""

    def __init__(self, default_rows: int | None = None, relkind: bytes | None = b"r") -> None:
        self.default_rows = default_rows
        self.relkind = relkind
        self.executed: list[str] = []

    async def execute(self, sql: str) -> str:
        self.executed.append(sql)
        return "CREATE TABLE"

    async def fetchval(self, sql: str):
        if "relkind" in sql:
            if "usage_ledger_default" in sql:
                return b"r" if self.default_rows is not None else None
            return self.relkind
        return self.default_rows


class TestPartitionHorizon:
    def test_already_partitioned_takes_the_maintenance_path(self) -> None:
        """Pre-fix _precheck RAISED SystemExit("already partitioned — nothing
        to do"), so every re-run was a silent no-op: the horizon expired with
        nothing ever creating another month, and rows began landing in
        usage_ledger_default — which then BLOCKS creating that month."""
        mod = _partition_module()
        conn = _FakeConn(default_rows=0, relkind=b"p")
        already, rows = asyncio.run(mod._precheck(conn))
        assert already is True and rows == 0, "an already-partitioned ledger must reach maintenance, not exit"

    def test_horizon_extends_one_month_past_the_conversion_range(self) -> None:
        mod = _partition_module()
        now = datetime(2026, 11, 2, tzinfo=UTC)
        conversion = mod._month_bounds(now)
        horizon = mod._horizon_bounds(now)
        assert conversion[-1][2] == "usage_ledger_2027_02", "the conversion range is unchanged (now + 3)"
        assert horizon[-1][2] == "usage_ledger_2027_03", "the re-run horizon must reach one month FURTHER"
        assert [b[2] for b in horizon] == [
            "usage_ledger_2026_11",
            "usage_ledger_2026_12",
            "usage_ledger_2027_01",
            "usage_ledger_2027_02",
            "usage_ledger_2027_03",
        ]

    def test_default_warning_is_silent_at_zero_and_loud_above(self) -> None:
        mod = _partition_module()
        assert mod._default_warning(0) is None
        assert mod._default_warning(None) is None
        text = mod._default_warning(7)
        assert text is not None
        assert "usage_ledger_default holds 7 row" in text
        assert "would be violated" in text, "the warning must name WHY the partition cannot be created"
        assert "DELETE FROM usage_ledger_default" in text, "the warning must carry the recovery"

    def test_ensure_horizon_creates_every_month_idempotently(self) -> None:
        mod = _partition_module()
        conn = _FakeConn(default_rows=0)
        asyncio.run(mod._ensure_horizon(conn, datetime(2026, 11, 2, tzinfo=UTC)))
        joined = "\n".join(conn.executed)
        assert "CREATE TABLE IF NOT EXISTS usage_ledger_2027_03 PARTITION OF usage_ledger" in joined
        assert "CREATE TABLE IF NOT EXISTS usage_ledger_2026_11 PARTITION OF usage_ledger" in joined
        assert len(conn.executed) == 5, "one IF NOT EXISTS statement per horizon month (idempotent)"

    def test_ensure_horizon_warns_on_a_non_empty_default(self, caplog: logging.LogRecord) -> None:
        mod = _partition_module()
        conn = _FakeConn(default_rows=42)
        with caplog.at_level(logging.WARNING):  # type: ignore[attr-defined]
            asyncio.run(mod._ensure_horizon(conn, datetime(2026, 11, 2, tzinfo=UTC)))
        messages = [r.getMessage() for r in caplog.records]  # type: ignore[attr-defined]
        assert any("usage_ledger_default holds 42 row" in m for m in messages), messages


# ---------------------------------------------------------------------------
# 5. PrometheusRule arithmetic (audit #39 + a live 2026-10-30 finding)
# ---------------------------------------------------------------------------

PROMETHEUS_RULE = REPO_ROOT / "helm" / "templates" / "prometheusrule.yaml"


class TestPrometheusRuleExpressions:
    def test_error_ratio_alert_keeps_its_comparison(self) -> None:
        """A bare ratio is a valid instant vector, so an alerting rule with
        no comparison fires on ANY traffic instead of above 10% (audit #39).
        The threshold belongs in the expression, not the summary text."""
        text = PROMETHEUS_RULE.read_text(encoding="utf-8")
        start = text.find("- alert: GatewayHighErrorRatio")
        assert start != -1, "GatewayHighErrorRatio rule missing"
        nxt = text.find("- alert:", start + 1)
        block = text[start : nxt if nxt != -1 else len(text)]
        assert re.search(r">\s*0\.10", block), (
            "GatewayHighErrorRatio has no comparison — a bare ratio fires on any traffic"
        )

    def test_backlog_alert_aggregates_the_per_replica_counter(self) -> None:
        """increase() is PER POD. With the sweep LEASE only one replica
        distils, so a bare "increase(...) == 0" matches the idle replica and
        the rule is true whenever a row is due — permanently pending on a
        healthy feed (found live on the 2-replica site, 2026-10-30). The
        expression must ask the aggregate question: did ANY replica write?"""
        text = PROMETHEUS_RULE.read_text(encoding="utf-8")
        line = next(
            (ln for ln in text.splitlines() if 'gateway_distill_writes_total{outcome="ok"}[1h]' in ln),
            None,
        )
        assert line is not None, "backlog rule expression missing"
        compact = re.sub(r"\s+", "", line)
        assert 'max(increase(gateway_distill_writes_total{outcome="ok"}[1h]))' in compact, (
            "increase() is per-replica and must be aggregated — a bare == 0 matches the idle replica"
        )
