"""Retention for the two tables added in waves 5-6 (wave 7, 2026-11-23).

tool_approval_requests (the approval gate's observations) and judge_verdicts
(the agent judge's quality record) both grow without bound, and neither is
covered by the retention_*_days loop in core/housekeeping.py: that loop is fed
by settings fields that exist TODAY, while these two knobs do not exist in
config.py yet (the Lead owns that file). Both steps therefore read their knob
with getattr(settings, <name>, <default>) — the module constants
DEFAULT_TOOL_APPROVAL_RETENTION_DAYS / DEFAULT_JUDGE_VERDICT_RETENTION_DAYS.

The contract under test:
  * 0 = keep forever: not ONE retention statement is issued;
  * a positive window issues exactly one DELETE, on the right table, with the
    window as its make_interval argument;
  * the cut-off COLUMN is the row's recency (last_seen / judged_at), and the
    DELETE is EVALUATED here, not string-matched, so keying on the wrong
    column changes which rows survive;
  * a fault is swallowed and logged at DEBUG (housekeeping never fails a sweep);
  * reclaimed rows are logged at INFO only when > 0 (a 60 s loop that logs
    "pruned 0 rows" every minute is noise, and noise hides the real prunes);
  * nothing else is deleted — not the token table, not the usage ledger, not
    the trace tables.

No live Postgres: the repo's monkeypatch-the-module-db pattern.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from pcai_llm.core import housekeeping

LOG = "pcai_llm.core.housekeeping"

# The two tables this zone owns. Kept as one tuple so the "only these" test
# below cannot drift away from the parametrised cases.
APPROVAL = "tool_approval_requests"
VERDICTS = "judge_verdicts"
OWNED = (APPROVAL, VERDICTS)

_CUTOFF_RE = re.compile(r"WHERE\s+(\w+)\s*<\s*now\(\)\s*-\s*make_interval")


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


class RetentionFakeDB:
    """Records every statement and EVALUATES the two retention deletes.

    Evaluating (instead of only string-matching) puts the cut-off COLUMN in the
    assertion: the column is parsed out of the SQL and used to age the rows, so
    keying the prune on first_seen instead of last_seen — or on an insertion
    time instead of judged_at — deletes the wrong rows here.
    """

    def __init__(
        self,
        *,
        approval_rows: list[dict[str, Any]] | None = None,
        verdict_rows: list[dict[str, Any]] | None = None,
        tag: str | None = None,
        fail_on: tuple[str, ...] = (),
    ) -> None:
        self.approval_rows = approval_rows if approval_rows is not None else []
        self.verdict_rows = verdict_rows if verdict_rows is not None else []
        self.tag = tag
        self.fail_on = fail_on
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    @property
    def sql(self) -> list[str]:
        return [sql for sql, _args in self.executed]

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if any(needle in sql for needle in self.fail_on):
            raise RuntimeError("db hiccup")
        if self.tag is not None:
            return self.tag
        if "DELETE FROM " + APPROVAL in sql:
            return f"DELETE {self._sweep(self.approval_rows, 'last_seen', sql, args)}"
        if "DELETE FROM " + VERDICTS in sql:
            return f"DELETE {self._sweep(self.verdict_rows, 'judged_at', sql, args)}"
        return "DELETE 0"

    @staticmethod
    def _sweep(rows: list[dict[str, Any]], default_column: str, sql: str, args: tuple[Any, ...]) -> int:
        match = _CUTOFF_RE.search(sql)
        column = match.group(1) if match else default_column
        cutoff = datetime.now(UTC) - timedelta(days=int(args[0]))
        keep = [row for row in rows if row[column] >= cutoff]
        deleted = len(rows) - len(keep)
        rows[:] = keep
        return deleted


def _fake_settings(**overrides: Any) -> SimpleNamespace:
    """A stand-in for the Settings object used by the sweep.

    Every OTHER knob is 0 (nothing else prunes, so the recorded statements are
    only ours), and the two new knobs are ABSENT unless a test supplies them —
    which is exactly what exercises the getattr default in production today.
    """
    base: dict[str, Any] = {
        "embedding_cache_ttl_s": 0,
        "tool_compress_store_ttl_s": 0,
        "trace_payload_retention_days": 0,
        "retention_trace_days": 0,
        "retention_conversation_days": 0,
        "retention_stickiness_days": 0,
        "retention_canonical_days": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _sweep(monkeypatch: pytest.MonkeyPatch, db: RetentionFakeDB, **overrides: Any) -> None:
    """Run ONE housekeeping sweep against the fake db and fake settings.

    response_cache.purge_expired is the only OTHER unconditional statement in
    the sweep, so it is silenced; the rate_windows prune stays (it is counter
    state, not a retention knob) and is filtered out by the assertions.
    """

    async def _noop() -> None:
        return None

    monkeypatch.setattr(housekeeping, "settings", _fake_settings(**overrides))
    monkeypatch.setattr(housekeeping, "db", db)
    monkeypatch.setattr(housekeeping.response_cache, "purge_expired", _noop)
    await housekeeping._sweep_once()


def _owned_sqls(db: RetentionFakeDB) -> list[str]:
    return [sql for sql in db.sql if any(table in sql for table in OWNED)]


def _args_for(db: RetentionFakeDB, table: str) -> tuple[Any, ...]:
    return next(args for sql, args in db.executed if table in sql)


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.levelno == level]


# ---------------------------------------------------------------------------
# 1. the shipped defaults (the knobs are not in config.py yet)
# ---------------------------------------------------------------------------


def test_shipped_defaults_are_the_documented_windows() -> None:
    """30 days for approval observations, 90 for the quality record.

    The brief fixes both numbers; a default that drifts silently changes how
    much decision input / evidence the fleet keeps.
    """
    assert housekeeping.DEFAULT_TOOL_APPROVAL_RETENTION_DAYS == 30
    assert housekeeping.DEFAULT_JUDGE_VERDICT_RETENTION_DAYS == 90


async def test_absent_knob_falls_back_to_the_shipped_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The knobs do not exist in config.py yet: getattr must supply the default.

    This is the LIVE path on today's deployment, so it is the one that has to
    work without the setting being declared anywhere.
    """
    db = RetentionFakeDB()
    await _sweep(monkeypatch, db)

    approval = next(sql for sql in db.sql if APPROVAL in sql)
    verdicts = next(sql for sql in db.sql if VERDICTS in sql)
    assert "make_interval(days => $1)" in approval
    assert "make_interval(days => $1)" in verdicts
    assert _args_for(db, APPROVAL) == (30,)
    assert _args_for(db, VERDICTS) == (90,)


# ---------------------------------------------------------------------------
# 2. 0 = keep forever (the operator contract shared with every other knob)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("knob", "table"),
    [
        ("tool_approval_retention_days", APPROVAL),
        ("judge_verdict_retention_days", VERDICTS),
    ],
)
async def test_zero_days_issues_no_statement(monkeypatch: pytest.MonkeyPatch, knob: str, table: str) -> None:
    """0 = keep forever: the step issues nothing for that table, not even a
    no-op DELETE (a statement is I/O, and an operator who said "keep" gets
    zero I/O from the knob)."""
    db = RetentionFakeDB()
    await _sweep(monkeypatch, db, **{knob: 0})
    assert not any(table in sql for sql in db.sql), f"0 days must not touch {table}"


@pytest.mark.parametrize(
    ("knob", "prune_name"),
    [
        ("tool_approval_retention_days", "prune_tool_approval_requests"),
        ("judge_verdict_retention_days", "prune_judge_verdicts"),
    ],
)
async def test_zero_days_does_not_even_call_the_prune(
    monkeypatch: pytest.MonkeyPatch, knob: str, prune_name: str
) -> None:
    """The call-site guard is the FIRST of the two guards: at 0 the sweep must
    not call the prune at all (a prune call is I/O even when it returns 0)."""
    db = RetentionFakeDB()
    calls: list[int] = []

    async def spy(days: int) -> int:
        calls.append(days)
        return 0

    monkeypatch.setattr(housekeeping, prune_name, spy)
    await _sweep(monkeypatch, db, **{knob: 0})
    assert calls == [], f"{prune_name} must not be called at 0 days, got {calls}"


# ---------------------------------------------------------------------------
# 3. a positive window issues exactly one DELETE, on the right table/column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("knob", "table", "column", "other"),
    [
        ("tool_approval_retention_days", APPROVAL, "last_seen", "judge_verdict_retention_days"),
        ("judge_verdict_retention_days", VERDICTS, "judged_at", "tool_approval_retention_days"),
    ],
)
async def test_positive_window_issues_exactly_one_delete(
    monkeypatch: pytest.MonkeyPatch, knob: str, table: str, column: str, other: str
) -> None:
    db = RetentionFakeDB()
    await _sweep(monkeypatch, db, **{knob: 7, other: 0})

    owned = _owned_sqls(db)
    assert len(owned) == 1, f"exactly one retention delete expected, got {owned}"
    assert f"DELETE FROM {table}" in owned[0]
    assert f"{column} < now() - make_interval(days => $1)" in owned[0]
    assert _args_for(db, table) == (7,)


async def test_window_removes_only_rows_older_than_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DELETE is executed, not just recorded: rows inside the window stay.

    Defaults: 30 days for approval observations, 90 for verdicts.
    """
    db = RetentionFakeDB(
        approval_rows=[{"id": "stale", "last_seen": _ago(40)}, {"id": "fresh", "last_seen": _ago(5)}],
        verdict_rows=[{"id": "stale", "judged_at": _ago(120)}, {"id": "fresh", "judged_at": _ago(10)}],
    )
    await _sweep(monkeypatch, db)

    assert [row["id"] for row in db.approval_rows] == ["fresh"]
    assert [row["id"] for row in db.verdict_rows] == ["fresh"]


async def test_approval_retention_ages_from_last_seen_not_first_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shape the fleet STILL calls is refreshed by the upsert, so its
    first_seen can be arbitrarily old while last_seen is fresh. It must survive:
    keying the window on first_seen would delete the operator's live decision
    input for a call pattern that is happening right now."""
    db = RetentionFakeDB(
        approval_rows=[{"id": "still-called", "first_seen": _ago(400), "last_seen": _ago(1)}],
    )
    await _sweep(monkeypatch, db, judge_verdict_retention_days=0)

    assert [row["id"] for row in db.approval_rows] == ["still-called"]


# ---------------------------------------------------------------------------
# 4. the defence-in-depth guard inside each prune
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("days", [0, -1])
async def test_prune_helpers_are_noops_at_or_below_zero(monkeypatch: pytest.MonkeyPatch, days: int) -> None:
    """The call-site guard is the first of two; the prune itself must also
    refuse (the trace_payloads.prune shape), so a future caller cannot delete
    rows by passing 0."""
    db = RetentionFakeDB()
    monkeypatch.setattr(housekeeping, "db", db)

    assert await housekeeping.prune_tool_approval_requests(days) == 0
    assert await housekeeping.prune_judge_verdicts(days) == 0
    assert db.executed == []


@pytest.mark.parametrize("tag", ["", "None", "garbage", "DELETE"])
async def test_unusable_command_tag_reads_as_zero(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    """The reclaimed count comes from an asyncpg command tag. An unexpected one
    must read as "nothing reclaimed", never crash the sweep."""
    db = RetentionFakeDB(tag=tag)
    monkeypatch.setattr(housekeeping, "db", db)

    assert await housekeeping.prune_judge_verdicts(90) == 0


# ---------------------------------------------------------------------------
# 5. logging: fault at DEBUG, reclamation at INFO only when > 0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("knob", "table", "other"),
    [
        ("tool_approval_retention_days", APPROVAL, "judge_verdict_retention_days"),
        ("judge_verdict_retention_days", VERDICTS, "tool_approval_retention_days"),
    ],
)
async def test_prune_fault_is_swallowed_and_logged_at_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    knob: str,
    table: str,
    other: str,
) -> None:
    """A prune fault must never fail the sweep (housekeeping's contract), and it
    must be visible at DEBUG for a diagnosis at LOG_LEVEL=DEBUG."""
    db = RetentionFakeDB(fail_on=(table,))
    with caplog.at_level(logging.DEBUG, logger=LOG):
        await _sweep(monkeypatch, db, **{knob: 5, other: 0})  # must NOT raise

    skipped = [msg for msg in _messages(caplog, logging.DEBUG) if f"{table} prune skipped" in msg]
    assert len(skipped) == 1, f"expected one DEBUG skip line for {table}, got {skipped}"
    # a swallowed fault reclaims nothing, so it must not claim a prune either
    assert _messages(caplog, logging.INFO) == []


# The fragment each step's INFO line carries (the human-readable noun, NOT the
# table name: the log line says "tool approval request rows").
@pytest.mark.parametrize(
    ("knob", "other", "fragment"),
    [
        ("tool_approval_retention_days", "judge_verdict_retention_days", "pruned 4 tool approval request rows"),
        ("judge_verdict_retention_days", "tool_approval_retention_days", "pruned 4 judge verdict rows"),
    ],
)
async def test_reclaimed_rows_are_logged_at_info(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    knob: str,
    other: str,
    fragment: str,
) -> None:
    db = RetentionFakeDB(tag="DELETE 4")
    with caplog.at_level(logging.INFO, logger=LOG):
        await _sweep(monkeypatch, db, **{knob: 5, other: 0})

    assert any(fragment in msg for msg in _messages(caplog, logging.INFO))


@pytest.mark.parametrize(
    ("knob", "other", "fragment"),
    [
        ("tool_approval_retention_days", "judge_verdict_retention_days", "tool approval request"),
        ("judge_verdict_retention_days", "tool_approval_retention_days", "judge verdict"),
    ],
)
async def test_nothing_is_logged_at_info_when_nothing_is_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    knob: str,
    other: str,
    fragment: str,
) -> None:
    """The sweep runs every 60 s: an INFO line per minute saying "pruned 0 rows"
    is noise, and noise hides the prunes that did something."""
    db = RetentionFakeDB(tag="DELETE 0")
    with caplog.at_level(logging.INFO, logger=LOG):
        await _sweep(monkeypatch, db, **{knob: 5, other: 0})

    assert not [msg for msg in _messages(caplog, logging.INFO) if fragment in msg]


# ---------------------------------------------------------------------------
# 6. the hard constraint: ONLY the two named tables are ever deleted
# ---------------------------------------------------------------------------


async def test_the_sweep_deletes_from_no_other_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never agent_traces / agent_trace_payloads (own retention), never an
    audit/ledger table, and never tool_approvals (the TOKEN table: a grant's
    lifetime is the operator's expires_at/revoked_at, not a retention window)."""
    db = RetentionFakeDB()
    await _sweep(monkeypatch, db)  # both defaults ON

    deleted = {match.group(1) for sql in db.sql if (match := re.search(r"DELETE\s+FROM\s+(\w+)", sql))}
    assert deleted == {"rate_windows", *OWNED}, f"unexpected DELETE targets: {sorted(deleted)}"
    assert "DELETE FROM tool_approvals" not in "\n".join(db.sql)
