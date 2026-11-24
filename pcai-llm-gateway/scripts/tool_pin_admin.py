#!/usr/bin/env python3
"""Operator lever for tool-definition pins: SEE what was approved, CLEAR one so it is approved again.

WHY THIS EXISTS (2026-11-23, cross-review R3). `tool_pin_mode: block` is a DURABLE
control: the approved fingerprint lives in the `tool_pins` table, so a pod restart
no longer re-approves whatever the MCP server happens to be serving (core/tool_pin.py).
That durability is the defence -- and it is also the trap. When a server LEGITIMATELY
changes a definition (a new description, a new schema field), that one tool is refused
on every relay turn until an operator re-approves it, and before this script the only
way to do that was to hand-edit Postgres: no supported path, no way to read the digests
first, and nothing that said what the operator was supposed to type.
`PinStore.reset()` exists but it is process-internal -- it has no operator surface.

WHAT IT DOES (read one table, delete rows -- nothing else):

  list                     print every pin: server, tool, fingerprint, first_seen,
                           last_seen. Read FIRST: the digest you are about to retire
                           should be the one you meant.
  clear --server S --tool T
                           delete ONE pin. The next observation of that tool is
                           FIRST_SEEN, so the definition being served is approved.
  clear --all --yes        delete EVERY pin (a fleet-wide re-approval). Requires
                           --yes: it is the one irreversible operation here.

WHAT IT DOES NOT DO: it cannot touch a RUNNING gateway's in-memory copy of the pins
(a separate process owns that memory), so `block` mode keeps refusing a changed
definition until the gateway that holds the pin is RESTARTED. Restart it after a
clear. It also never re-approves anything itself: deleting the row means "the next
observation is a first observation", and the operator's lever is that restart, not a
silent grant -- a rewritten definition must never be able to approve itself.

FAIL LOUD: a missing database configuration, an unreachable database and a missing
`tool_pins` table (a cluster whose gateway never booted, so the schema was never
created) each print the reason and exit non-zero. An empty listing is never used to
mean "the database was not consulted".

USAGE (from a checkout, or `kubectl exec` into the gateway pod so it sees the same env):

  python scripts/tool_pin_admin.py list
  python scripts/tool_pin_admin.py clear --server logsearch --tool search_logs
  python scripts/tool_pin_admin.py clear --all --yes

Exit codes: 0 = success; 2 = database not configured / bad arguments; 1 = the
database could not be reached or rejected the statement.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcai_llm.core import tool_pin  # noqa: E402
from pcai_llm.db import Database  # noqa: E402

# Bound on the one connect: an operator command must fail visibly rather than hang
# behind a wedged endpoint (the same reasoning as tool_pin.LOAD_TIMEOUT_S).
CONNECT_TIMEOUT_S = 10.0

# The admin LIST read. tool_pin._SELECT_ALL_SQL is the store's LOAD query and cannot
# serve this command: it does not select last_seen (observation history -- the column
# that tells an operator a tool is still being served from one that vanished) and it
# carries no ordering. Nothing else is copied: --all deletes with
# tool_pin._CLEAR_SQL VERBATIM, and the one-key delete has no constant to reuse (the
# store only ever clears the whole table, so there is no narrower statement in it).
_LIST_SQL = "SELECT server, tool, fingerprint, first_seen, last_seen FROM tool_pins ORDER BY server, tool"
_DELETE_ONE_SQL = "DELETE FROM tool_pins WHERE server = $1 AND tool = $2"

_COLUMNS = ("server", "tool", "fingerprint", "first_seen", "last_seen")


class CommandError(Exception):
    """A failure the operator must READ: the message and the exit code it implies."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _connection_kwargs() -> dict[str, Any]:
    """The gateway's OWN connection config (one validation point, never a copy).

    Database._connect_kwargs() is where URL-mode vs discrete-mode is validated, and
    it is fail-loud with the exact missing fields -- the same reason
    core.tool_pin._DbPinBackend._aload uses it instead of re-reading the environment
    here. A second copy of that logic would drift from the gateway's.
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


def _missing_table() -> CommandError:
    """The one schema fault an operator will actually hit on a fresh cluster."""
    return CommandError(
        "the tool_pins table does not exist: this database has never booted the gateway, "
        "so its schema was never created and there are no pins yet.",
        code=1,
    )


def _fmt_ts(value: Any) -> str:
    """A TIMESTAMPTZ column as a readable ISO-8601 string ("" when NULL/unreadable)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        try:
            return value.isoformat(sep=" ", timespec="seconds")
        except (ValueError, OverflowError):  # pragma: no cover - a hostile datetime
            return str(value)
    return str(value)


def _cell(row: Any, name: str) -> str:
    """One column as text. Accepts a dict or an asyncpg Record (both index by name)."""
    try:
        value = row[name]
    except (KeyError, IndexError):  # pragma: no cover - a row missing a selected column
        return ""
    return _fmt_ts(value) if name.endswith("_seen") else ("" if value is None else str(value))


def _format_rows(rows: Sequence[Any]) -> str:
    """The `list` output: a count, a header and one line per pin (or a clear empty state)."""
    if not rows:
        return (
            "no tool-definition pins recorded: nothing has been pinned yet, or every pin was cleared.\n"
            "The next observation of each tool is FIRST_SEEN, so it is approved as served."
        )
    table = [tuple(_cell(row, column) for column in _COLUMNS) for row in rows]
    header = tuple(column.upper() for column in _COLUMNS)
    widths = [max(len(line[index]) for line in (header, *table)) for index in range(len(_COLUMNS))]
    lines = [f"{len(table)} pin(s) in tool_pins:", ""]
    for line in (header, *table):
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip())
    return "\n".join(lines)


def _clear_plan(server: str | None, tool: str | None, all_pins: bool, yes: bool) -> tuple[str, tuple[str, ...]]:
    """Decide WHAT to delete. PURE: (sql, params), or a CommandError for a bad ask."""
    server = (server or "").strip()
    tool = (tool or "").strip()
    if all_pins:
        if server or tool:
            raise CommandError("--all clears every pin; do not also pass --server/--tool.", code=2)
        if not yes:
            raise CommandError(
                "--all deletes EVERY tool-definition pin, so the whole fleet re-approves on its "
                "next observation. Re-run with --yes if that is what you mean.",
                code=2,
            )
        # The store's own "forget everything" statement, verbatim.
        return tool_pin._CLEAR_SQL, ()
    if not server or not tool:
        raise CommandError(
            "clear needs a target: --server S --tool T for one pin, or --all --yes for every pin.",
            code=2,
        )
    return _DELETE_ONE_SQL, (server, tool)


def _deleted(tag: Any) -> int:
    """Rows affected from a command tag ("DELETE 1"); 0 when it is unreadable."""
    try:
        return int(str(tag).split()[-1])
    except (ValueError, IndexError):  # pragma: no cover - a driver that changed the tag shape
        return 0


async def _cmd_list(conn: asyncpg.Connection) -> int:
    """Print every pin. The read is the audit step that precedes a clear."""
    try:
        rows = await conn.fetch(_LIST_SQL)
    except asyncpg.UndefinedTableError as exc:
        raise _missing_table() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the listing query: {exc}", code=1) from exc
    print(_format_rows(rows))
    return 0


async def _cmd_clear(conn: asyncpg.Connection, sql: str, params: tuple[str, ...], all_pins: bool) -> int:
    """Delete the targeted pin(s) and say what it means for the running gateway."""
    try:
        tag = await conn.execute(sql, *params)
    except asyncpg.UndefinedTableError as exc:
        raise _missing_table() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the delete: {exc}", code=1) from exc
    deleted = _deleted(tag)
    target = "every pin" if all_pins else f"{params[0]}__{params[1]}"
    if deleted == 0 and all_pins:
        print(
            "no pins recorded: nothing was deleted (the table is already empty, so every tool's "
            "next observation is FIRST_SEEN)."
        )
        return 0
    if deleted == 0:
        print(
            f"no pin matched {target}: nothing was deleted. Its next observation is already "
            "FIRST_SEEN, so there is nothing to re-approve."
        )
        return 0
    print(f"cleared {deleted} pin(s): {target}")
    print(
        "The running gateway still holds the retired digest in MEMORY, so `block` mode keeps "
        "refusing the changed definition until it is RESTARTED. Restart it now; the next "
        "observation after the restart is FIRST_SEEN and the definition being served is approved.",
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    """Open ONE connection, run the one command, always close it."""
    # Validate the clear target BEFORE connecting: a bad ask must not need a database.
    sql: str = ""
    params: tuple[str, ...] = ()
    all_pins = False
    if args.command != "list":
        sql, params = _clear_plan(args.server, args.tool, args.all, args.yes)
        all_pins = bool(args.all)
    conn = await _open()
    try:
        if args.command == "list":
            return await _cmd_list(conn)
        return await _cmd_clear(conn, sql, params, all_pins)
    finally:
        await conn.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The CLI surface: `list`, or `clear` with exactly one kind of target."""
    ap = argparse.ArgumentParser(
        prog="tool_pin_admin.py",
        description="List or clear tool-definition pins (the operator's re-approval lever).",
        epilog=(
            "A clear deletes the DURABLE record; the running gateway must be restarted before the "
            "definition being served is approved again."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="print every pin: server, tool, fingerprint, first_seen, last_seen")
    clear = sub.add_parser("clear", help="delete pin(s) so the next observation is FIRST_SEEN")
    clear.add_argument("--server", metavar="S", help="MCP server name of the pin to clear")
    clear.add_argument("--tool", metavar="T", help="tool name of the pin to clear")
    clear.add_argument("--all", action="store_true", help="clear EVERY pin (requires --yes)")
    clear.add_argument("--yes", action="store_true", help="confirm --all")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse, run, report a CommandError as a message + its exit code."""
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except CommandError as exc:
        print(f"tool_pin_admin: {exc.message}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
