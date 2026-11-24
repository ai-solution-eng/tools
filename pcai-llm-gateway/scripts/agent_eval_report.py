#!/usr/bin/env python3
"""Read the agent quality layer: verdict counts, the worst recent turns, payload health.

WHY THIS EXISTS (2026-11-23, cross-review R5). Two tables make up the quality layer and
both were WRITE-ONLY: core/agent_eval.py writes one row per (turn, verdict) to
`agent_eval_verdicts`, and core/trace_payloads.py writes what the agent SAW to
`agent_trace_payloads`, but nothing in the repo ever READ them. An operator who turned the
knobs on (`agent_eval_enabled`, `trace_payload_sampling` > 0) therefore saw NOTHING: no way
to check the tables were filling, no way to see which turns were bad, no way to tell a
working feature from a silently broken one. This script is the reader that makes the tables
useful NOW -- no model, no judge, no writes, no new dependency. (The calibrated LLM judge is
a separate, later decision; this only reports what the deterministic half already proves.)

WHAT IT PRINTS:

  (a) verdict counts by verdict over the last N days (`--days`, default 7), with the
      number of turns scored -- so "nothing is firing" and "nothing is being recorded"
      are distinguishable at a glance;
  (b) the WORST recent turns: request_id, every non-clean verdict and its reason. Turns
      are ranked by their worst single finding, then by how many findings they carry
      (core.agent_eval's report order is the severity order), then by recency, over the
      most recent `--scan-rows` verdict rows in the window (`--limit` of them shown);
  (c) `agent_trace_payloads` health, TABLE-WIDE (that is where retention lives): rows,
      oldest/newest row, and the share of rows that were truncated or redacted.

Every statement is a SELECT. It never writes, never scores anything itself and never
changes a setting; an empty table is a normal answer, not an error (the flags default OFF,
and the report names them so an empty window is explainable).

USAGE (from a checkout, or `kubectl exec` into the gateway pod so it sees the same env):

  python scripts/agent_eval_report.py                 # last 7 days, 10 worst turns
  python scripts/agent_eval_report.py --days 30 --limit 25
  python scripts/agent_eval_report.py --days 1 --scan-rows 20000

Exit codes: 0 = the report was produced (empty sections included); 2 = the database is not
configured; 1 = the database could not be reached or rejected a read.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcai_llm.config import settings  # noqa: E402
from pcai_llm.core import agent_eval  # noqa: E402
from pcai_llm.db import Database  # noqa: E402

# Bound on the one connect: a report must fail visibly rather than hang behind a wedged
# endpoint (the same reasoning as core.tool_pin.LOAD_TIMEOUT_S).
CONNECT_TIMEOUT_S = 10.0

# READ-ONLY, by construction: these are the only statements this script can issue.
# (a) counts per verdict in the window, with the turns each verdict touched.
_COUNTS_SQL = """
SELECT verdict, count(*) AS rows, count(DISTINCT request_id) AS turns
FROM agent_eval_verdicts
WHERE ts >= now() - make_interval(days => $1)
GROUP BY verdict
"""
# Window context: how many distinct turns were scored at all (a clean turn is one row).
_TURNS_TOTAL_SQL = """
SELECT count(DISTINCT request_id) AS turns
FROM agent_eval_verdicts
WHERE ts >= now() - make_interval(days => $1)
"""
# (b) the ranking input, BOUNDED: newest rows first, so the "worst recent turns" section
# costs one indexed scan even when the window holds millions of rows.
_RECENT_SQL = """
SELECT request_id, verdict, reason, ts
FROM agent_eval_verdicts
WHERE ts >= now() - make_interval(days => $1)
ORDER BY ts DESC, id DESC
LIMIT $2
"""
# (c) table-wide payload health: retention is a property of the whole table, not of the
# window, so this read carries no ts filter.
_PAYLOAD_SQL = """
SELECT count(*) AS rows,
       min(ts) AS oldest,
       max(ts) AS newest,
       count(*) FILTER (WHERE truncated) AS truncated,
       count(*) FILTER (WHERE redacted) AS redacted
FROM agent_trace_payloads
"""

# Severity used to rank turns. core.agent_eval.VERDICTS is already ordered most-serious
# first with `clean` last (its docstring calls that REPORT ORDER), so the weight is
# DERIVED from the module vocabulary instead of a second hand-maintained list drifting
# from the first: not_granted 4, tool_error 3, loop 2, step_budget 1, clean 0.
_SEVERITY = {verdict: len(agent_eval.VERDICTS) - 1 - index for index, verdict in enumerate(agent_eval.VERDICTS)}
_ORDER = {verdict: index for index, verdict in enumerate(agent_eval.VERDICTS)}


class CommandError(Exception):
    """A failure the operator must READ: the message and the exit code it implies."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class Turn(NamedTuple):
    """One scored turn that has findings: what the worst-turns section ranks and prints.

    A NamedTuple rather than a dataclass on purpose: this file is a SCRIPT, and the
    repo's established way to load one in a test is importlib.spec_from_file_location,
    which on Python 3.14 breaks a @dataclass module that was never registered in
    sys.modules (dataclasses resolves the module namespace). A NamedTuple has no such
    dependency, so the script stays loadable by path as well as runnable.
    """

    request_id: str
    findings: tuple[tuple[str, str], ...]  # (verdict, reason), in report order
    severity: int  # the WORST single finding on the turn (primary rank key)
    weight: int  # the SUM of the turn's finding severities (secondary rank key)
    last_ts: Any  # the newest row timestamp for the turn (datetime or None)


def _field(row: Any, name: str, default: Any = None) -> Any:
    """One column from a dict or an asyncpg Record (both index by name)."""
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return default


def _text(row: Any, name: str) -> str:
    value = _field(row, name)
    return "" if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):  # pragma: no cover - a non-numeric COUNT
        return 0


def _epoch(value: Any) -> float:
    """Wall-clock seconds for a TIMESTAMPTZ value (0.0 when unusable)."""
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):  # pragma: no cover - a hostile datetime
            return 0.0
    return 0.0


def _fmt_ts(value: Any) -> str:
    """A TIMESTAMPTZ column as a readable ISO-8601 string ("" when NULL/unreadable)."""
    if isinstance(value, datetime):
        try:
            return value.isoformat(sep=" ", timespec="seconds")
        except (ValueError, OverflowError):  # pragma: no cover - a hostile datetime
            return str(value)
    return "" if value is None else str(value)


def _share(part: Any, whole: Any) -> str:
    """`part` as a percentage of `whole` ("n/a" when there is no whole to divide by)."""
    numerator, denominator = _int(part), _int(whole)
    if denominator <= 0:
        return "n/a"
    return f"{100.0 * numerator / denominator:.1f}%"


def _flag(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def rank_turns(rows: Sequence[Any], limit: int) -> list[Turn]:
    """Group verdict rows by turn and return the WORST `limit` of them. PURE.

    "Worst" is ordered by three keys, in this order:
      1. the turn's WORST single finding (_SEVERITY of its most severe verdict) --
         one DENIED call outranks a turn whose findings are all operational, even
         when the latter has more of them;
      2. the SUM of the turn's finding severities (more findings is worse);
      3. recency, then request_id, so the order is total and a re-run prints the
         same report.
    Clean turns are not findings and never appear.
    """
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        request_id = _text(row, "request_id")
        if request_id:
            grouped.setdefault(request_id, []).append(row)
    turns: list[Turn] = []
    for request_id, turn_rows in grouped.items():
        findings: list[tuple[str, str]] = []
        weight = 0
        last_ts: Any = None
        for row in turn_rows:
            verdict = _text(row, "verdict")
            ts = _field(row, "ts")
            if ts is not None and (last_ts is None or _epoch(ts) > _epoch(last_ts)):
                last_ts = ts
            if verdict == agent_eval.CLEAN:
                continue
            findings.append((verdict, _text(row, "reason")))
            weight += _SEVERITY.get(verdict, 1)
        if not findings:
            continue
        findings.sort(key=lambda finding: _ORDER.get(finding[0], len(agent_eval.VERDICTS)))
        severity = max(_SEVERITY.get(verdict, 1) for verdict, _reason in findings)
        turns.append(Turn(request_id, tuple(findings), severity, weight, last_ts))
    turns.sort(key=lambda turn: (-turn.severity, -turn.weight, -_epoch(turn.last_ts), turn.request_id))
    return turns[: max(0, limit)]


def format_counts(rows: Sequence[Any], *, days: int, turns_scored: int) -> str:
    """Section (a): one line per verdict in report order, plus any unknown verdict. PURE."""
    heading = f"verdict counts, last {days} day(s)  ({turns_scored} turn(s) scored)"
    if not rows:
        return (
            f"{heading}\n"
            "  no verdict rows in the window: nothing was scored. agent_eval_enabled is\n"
            "  OFF by default -- turn it on (and keep it on for a while) before reading this."
        )
    by_verdict = {_text(row, "verdict"): row for row in rows}
    lines = [heading]
    for verdict in agent_eval.VERDICTS:
        row = by_verdict.get(verdict)
        lines.append(f"  {verdict:<12} {_int(_field(row, 'rows')):>7,} row(s) {_int(_field(row, 'turns')):>7,} turn(s)")
    unknown = sorted(set(by_verdict) - set(agent_eval.VERDICTS))
    for verdict in unknown:
        row = by_verdict[verdict]
        lines.append(
            f"  {verdict or '(blank)':<12} {_int(_field(row, 'rows')):>7,} row(s) "
            f"{_int(_field(row, 'turns')):>7,} turn(s)  <- NOT in the verdict vocabulary"
        )
    return "\n".join(lines)


def format_turns(turns: Sequence[Turn], *, days: int, scanned: int, turns_scored: int) -> str:
    """Section (b): the ranked worst turns, or a precise empty state. PURE."""
    if turns_scored <= 0:
        return f"worst recent turns, last {days} day(s):\n  no verdict rows in the window - nothing to rank."
    if not turns:
        return (
            f"worst recent turns, last {days} day(s):\n"
            f"  no findings - every scored turn was clean ({turns_scored} turn(s) scored)."
        )
    lines = [
        (
            f"worst recent turns, last {days} day(s) "
            f"(ranked over the {scanned:,} most recent verdict rows; showing {len(turns)})"
        )
    ]
    for turn in turns:
        lines.append("")
        lines.append(
            f"  {_fmt_ts(turn.last_ts) or '(no ts)'}  {turn.request_id}  weight {turn.weight} (worst {turn.severity})"
        )
        for verdict, reason in turn.findings:
            lines.append(f"    {verdict:<12} {reason}")
    return "\n".join(lines)


def format_payload_stats(row: Any, *, retention_days: int) -> str:
    """Section (c): table-wide payload health, or the empty state that names the knob. PURE."""
    heading = f"agent_trace_payloads (table-wide; retention {retention_days} day(s), 0 = keep)"
    total = _int(_field(row, "rows"))
    if total <= 0:
        return (
            f"{heading}\n"
            "  no payload rows recorded: trace_payload_sampling is 0.0 (OFF) by default.\n"
            "  Payloads are captured PER REQUEST -- raise the sampling rate and a sampled\n"
            "  turn stores both its args and its result rows."
        )
    truncated = _int(_field(row, "truncated"))
    redacted = _int(_field(row, "redacted"))
    return "\n".join(
        [
            heading,
            f"  rows        {total:,}",
            f"  oldest      {_fmt_ts(_field(row, 'oldest')) or '(none)'}",
            f"  newest      {_fmt_ts(_field(row, 'newest')) or '(none)'}",
            f"  truncated   {_share(truncated, total):>6}  ({truncated:,} of {total:,})",
            f"  redacted    {_share(redacted, total):>6}  ({redacted:,} of {total:,})",
        ]
    )


def format_flags() -> str:
    """The knob values that explain an empty or a non-empty report. PURE (reads settings)."""
    return (
        "flags: "
        f"agent_eval_enabled={_flag(settings.agent_eval_enabled)} "
        f"agent_eval_max_steps={settings.agent_eval_max_steps} "
        f"agent_eval_loop_threshold={settings.agent_eval_loop_threshold} "
        f"trace_payload_sampling={settings.trace_payload_sampling} "
        f"trace_payload_retention_days={settings.trace_payload_retention_days}"
    )


def _connection_kwargs() -> dict[str, Any]:
    """The gateway's OWN connection config (one validation point, never a copy).

    Database._connect_kwargs() is where URL-mode vs discrete-mode is validated, and it is
    fail-loud with the exact missing fields -- the same reason
    core.tool_pin._DbPinBackend._aload uses it instead of re-reading the environment.
    """
    try:
        return Database._connect_kwargs()
    except RuntimeError as exc:
        raise CommandError(
            f"the database is not configured: {exc}\n"
            "Set PCAI_LLM_DATABASE_URL (the gateway's own URL) or every PCAI_LLM_DB_* "
            "variable, then re-run. NOTE: scripts/ is NOT baked into the gateway image, so this\n"
            "cannot be run with kubectl exec inside the pod -- run it from a checkout that can\n"
            "reach the database (or copy it into the pod yourself).",
            code=2,
        ) from exc


async def _open() -> asyncpg.Connection:
    """Connect with the gateway's own configuration; every fault is a readable error."""
    kwargs = _connection_kwargs()
    try:
        return await asyncpg.connect(timeout=CONNECT_TIMEOUT_S, **kwargs)
    except (OSError, asyncpg.PostgresError) as exc:
        raise CommandError(f"cannot connect to the database: {exc}", code=1) from exc


def _read_fault(exc: Exception) -> CommandError:
    """Translate one read fault into the sentence an operator needs."""
    if isinstance(exc, asyncpg.UndefinedTableError):
        return CommandError(
            "the quality-layer tables do not exist: this database has never booted the gateway, "
            "so its schema was never created (and nothing has been recorded).",
            code=1,
        )
    return CommandError(f"the database rejected a read: {exc}", code=1)


async def _fetch(conn: asyncpg.Connection, sql: str, *params: Any) -> list[Any]:
    try:
        return list(await conn.fetch(sql, *params))
    except asyncpg.PostgresError as exc:
        raise _read_fault(exc) from exc


async def _fetchrow(conn: asyncpg.Connection, sql: str, *params: Any) -> Any:
    try:
        return await conn.fetchrow(sql, *params)
    except asyncpg.PostgresError as exc:
        raise _read_fault(exc) from exc


async def _run(args: argparse.Namespace) -> int:
    """Read the three sections over ONE connection, then print the report."""
    conn = await _open()
    try:
        counts = await _fetch(conn, _COUNTS_SQL, args.days)
        total = await _fetchrow(conn, _TURNS_TOTAL_SQL, args.days)
        recent = await _fetch(conn, _RECENT_SQL, args.days, args.scan_rows)
        payload = await _fetchrow(conn, _PAYLOAD_SQL)
    finally:
        await conn.close()
    turns_scored = _int(_field(total, "turns"))
    turns = rank_turns(recent, args.limit)
    print(f"agent quality layer report -- window: last {args.days} day(s)")
    print(format_flags())
    print()
    print(format_counts(counts, days=args.days, turns_scored=turns_scored))
    print()
    print(format_turns(turns, days=args.days, scanned=len(recent), turns_scored=turns_scored))
    print()
    print(format_payload_stats(payload, retention_days=settings.trace_payload_retention_days))
    return 0


def _positive_int(value: str) -> int:
    """argparse type: an integer >= 1 (a zero-day or zero-row window is a mistake)."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {number}")
    return number


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The CLI surface: a window, how many bad turns to show, and how far to scan."""
    ap = argparse.ArgumentParser(
        prog="agent_eval_report.py",
        description="Read the agent quality layer: verdict counts, worst recent turns, payload health.",
        epilog=(
            "Read-only: every statement is a SELECT. An empty report means the flags are off "
            "(agent_eval_enabled / trace_payload_sampling), not that the reader is broken."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--days", type=_positive_int, default=7, metavar="N", help="window for verdicts and turns (default 7)"
    )
    ap.add_argument("--limit", type=_positive_int, default=10, metavar="N", help="worst turns to show (default 10)")
    ap.add_argument(
        "--scan-rows",
        type=_positive_int,
        default=5000,
        metavar="N",
        help="most recent verdict rows to rank over (default 5000)",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse, run, report a CommandError as a message + its exit code."""
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except CommandError as exc:
        print(f"agent_eval_report: {exc.message}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
