#!/usr/bin/env python3
"""Operator lever for the destructive-tool approval gate: SEE what was asked, GRANT one shape, REVOKE a token.

WHY THIS EXISTS (2026-11-23, wave 5). The gateway had no gate between an agent and a cluster mutation: the
ACL answers "may this key call this tool", never "should THIS call happen"
(documentation/design/tool-approval-gate.md). The gate in api/routes_mcp.py::_gated_mcp_call closes that, and
this script is the ONLY supported way to open it: an approval is a row in tool_approvals, and an operator
who had to hand-edit Postgres would have no supported path, no read before write, and nothing that told them
what to type.

THE LOOP (observe -> grant -> enforce):
  * tool_approval_mode=warn (the shipped default is "off"; warn is phase 1 of the rollout) records every call
    that WOULD have needed a token in tool_approval_requests, and lets it run untouched.
  * `list` shows those pending requests (one line per key + server + tool + arguments fingerprint, with the
    number of times each was seen) and every LIVE token.
  * `grant --request <id>` turns ONE request into a token scoped to that exact arguments fingerprint, with a
    mandatory use count and expiry. `grant --any-args` deliberately issues the WEAKER any-arguments shape
    (labelled as such everywhere it is shown) and prints a warning: it does not pin the namespace/table/id
    the call names.
  * In enforce mode the next identical call consumes one use. No gateway restart is needed: the gate reads
    the table on every call (unlike tool pins, which a running process caches in memory).

CEILINGS: a token may not exceed settings.tool_approval_token_max_uses / settings.tool_approval_token_ttl_s.
A grant above either ceiling is REFUSED (exit 2) rather than silently clamped -- a clamped grant would look
like what the operator asked for while authorizing something else.

FAIL LOUD: a missing database configuration, an unreachable database, a missing table (a cluster whose
gateway never booted, so the schema was never created), an unknown request id and a bad --ttl each print the
reason and exit non-zero. An empty listing is never used to mean "the database was not consulted".

WHERE TO RUN IT: scripts/ is NOT baked into the gateway image, so this cannot be run with kubectl exec
inside the pod. Run it from a checkout that can reach the database (it reads the gateway PCAI_LLM_DATABASE_URL
or PCAI_LLM_DB_* configuration).

USAGE:

  python scripts/tool_approval_admin.py list
  python scripts/tool_approval_admin.py grant --request 3f1c... --uses 1 --ttl 15m
  python scripts/tool_approval_admin.py revoke 9a2e...

Exit codes: 0 = success; 2 = database not configured / bad arguments; 1 = the database could not be
reached or rejected the statement.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcai_llm.config import settings  # noqa: E402
from pcai_llm.core.audit import audit_action  # noqa: E402
from pcai_llm.db import Database  # noqa: E402

# Bound on the one connect: an operator command must fail visibly rather than hang behind a wedged endpoint
# (the same reasoning as tool_pin.CONNECT_TIMEOUT_S).
CONNECT_TIMEOUT_S = 10.0

# The pending-request read. Newest activity first: the operator is deciding what to grant NOW.
_REQUESTS_SQL = """
SELECT request_id, key_id, server, tool, args_fingerprint,
       first_seen, last_seen, times_seen, mode
  FROM tool_approval_requests
 ORDER BY last_seen DESC
"""
# The live-token read: the same liveness predicate the gate applies (revoked and expired tokens are not
# shown -- an operator deciding whether to grant must not mistake a dead token for a live one).
_LIVE_TOKENS_SQL = """
SELECT id, key_id, server, tool, args_fingerprint, max_uses, uses_so_far,
       expires_at, issued_by, reason, issued_at
  FROM tool_approvals
 WHERE revoked_at IS NULL AND expires_at > now()
 ORDER BY expires_at
"""
_REQUEST_ONE_SQL = """
SELECT request_id, key_id, server, tool, args_fingerprint
  FROM tool_approval_requests
 WHERE request_id = $1
"""
# One INSERT, explicit id: the operator gets the id back on stdout and it is the revoke handle.
_GRANT_SQL = """
INSERT INTO tool_approvals
    (id, key_id, server, tool, args_fingerprint, max_uses, expires_at, issued_by, reason)
VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(secs => $7), $8, $9)
RETURNING id, expires_at
"""
_REVOKE_SQL = "UPDATE tool_approvals SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL RETURNING id"
_TOKEN_STATE_SQL = "SELECT revoked_at, expires_at FROM tool_approvals WHERE id = $1"

_TTL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd]?)$")
_TTL_UNITS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class CommandError(Exception):
    """A failure the operator must READ: the message and the exit code it implies."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _connection_kwargs() -> dict[str, Any]:
    """The gateway's OWN connection config (one validation point, never a copy).

    Database._connect_kwargs() is where URL-mode vs discrete-mode is validated, and it is fail-loud with the
    exact missing fields -- the same reason core.tool_pin._DbPinBackend._aload uses it. A second copy of that
    logic here would drift from the gateway's.
    """
    try:
        return Database._connect_kwargs()
    except RuntimeError as exc:
        raise CommandError(
            f"the database is not configured: {exc}\n"
            "Set PCAI_LLM_DATABASE_URL (the gateway's own URL) or every PCAI_LLM_DB_* variable, then re-run.\n"
            "NOTE: scripts/ is NOT baked into the gateway image, so this cannot be run with kubectl exec inside\n"
            "the pod -- run it from a checkout that can reach the database (or copy it into the pod yourself).",
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
        "the tool_approval_requests / tool_approvals tables do not exist: this database has never booted a"
        " gateway with the approval gate (2026-11-23+) in its schema, so no request has ever been recorded.",
        code=1,
    )


def _fmt_ts(value: Any) -> str:
    """A TIMESTAMPTZ column as a readable ISO-8601 string (empty when NULL/unreadable)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        try:
            return value.isoformat(sep=" ", timespec="seconds")
        except (ValueError, OverflowError):  # pragma: no cover - a hostile datetime
            return str(value)
    return str(value)


def _fmt_ttl(seconds: float) -> str:
    """A duration as the operator typed it would be: 15m, 2h, 30s."""
    if seconds % 86400 == 0:
        return f"{int(seconds // 86400)}d"
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds)}s"


def _parse_ttl(text: str | None) -> float:
    """Parse --ttl ("900", "30s", "15m", "2h", "1d") into seconds. PURE.

    A bare number is SECONDS (the setting's own unit). Anything else is a usage error -- guessing at a lifetime is
    exactly the mistake this gate exists to prevent.
    """
    raw = (text or "").strip().lower()
    if not raw:
        raise CommandError("--ttl is empty; give a duration such as 15m, 2h or 900s.", code=2)
    match = _TTL_RE.match(raw)
    if match is None:
        raise CommandError(
            f"cannot parse --ttl {text!r}: use a number with an optional s/m/h/d suffix, e.g. 15m.", code=2
        )
    seconds = float(match.group(1)) * _TTL_UNITS[match.group(2)]
    if seconds <= 0:
        raise CommandError("--ttl must be greater than zero: a token that expires immediately is not a grant.", code=2)
    return seconds


def _operator() -> str:
    """A best-effort operator identity for issued_by / the admin audit row."""
    return os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser() or "unknown"


def _ceilings() -> tuple[int, int]:
    """(max uses, max ttl seconds) a token may not exceed (settings, read-only)."""
    uses = int(getattr(settings, "tool_approval_token_max_uses", 1) or 1)
    ttl = int(getattr(settings, "tool_approval_token_ttl_s", 900) or 900)
    return max(1, uses), max(1, ttl)


def _check_ceilings(uses: int, ttl_s: float) -> None:
    """Refuse a grant ABOVE the configured ceilings (never silently clamp)."""
    max_uses, max_ttl = _ceilings()
    if uses < 1:
        raise CommandError("--uses must be at least 1: an approval with no uses authorizes nothing.", code=2)
    if uses > max_uses:
        raise CommandError(
            f"--uses {uses} exceeds the configured ceiling tool_approval_token_max_uses={max_uses}."
            f" Grant {max_uses} use(s), or raise the setting deliberately for the whole fleet.",
            code=2,
        )
    if ttl_s > max_ttl:
        raise CommandError(
            f"--ttl {_fmt_ttl(ttl_s)} exceeds the configured ceiling tool_approval_token_ttl_s={max_ttl}s."
            " Grant a shorter lifetime, or raise the setting deliberately for the whole fleet.",
            code=2,
        )


def _cell(row: Any, name: str) -> str:
    """One column as text. Accepts a dict or an asyncpg Record (both index by name)."""
    try:
        value = row[name]
    except (KeyError, IndexError):  # pragma: no cover - a row missing a selected column
        return ""
    return "" if value is None else str(value)


def _ts_cell(row: Any, name: str) -> str:
    """One TIMESTAMPTZ column as text (empty when NULL/unreadable)."""
    try:
        return _fmt_ts(row[name])
    except (KeyError, IndexError):  # pragma: no cover - a row missing a selected column
        return ""


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """A fixed-width table (header + rows), rstrip-ed so no line carries trailing space."""
    widths = [max(len(line[index]) for line in (header, *rows)) for index in range(len(header))]
    out = ["  ".join(cell.ljust(width) for cell, width in zip(header, widths)).rstrip()]
    out.extend("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in rows)
    return out


def _any_args_label(fingerprint: Any) -> str:
    """The fingerprint cell, with the WEAKER any-arguments shape labelled explicitly."""
    return "*ANY ARGS*" if fingerprint is None else str(fingerprint)


def _any_key_label(key_id: Any) -> str:
    """The key-scope cell, with the WEAKER any-key shape labelled explicitly."""
    return "*ANY KEY*" if key_id is None else str(key_id)


def _covered(requests: Sequence[Any], tokens: Sequence[Any]) -> set[str]:
    """Which request_ids already have a matching LIVE token (so list shows what is still pending).

    A token covers a request when server/tool/fingerprint agree AND its key scope admits the request's key:
    key_id NULL is the fleet-wide shape, otherwise the ids must match. Any-arguments tokens (fingerprint
    NULL) cover every request for their tool -- that is the weaker shape, and the token table says so.
    """
    covered: set[str] = set()
    for request in requests:
        for token in tokens:
            if token["server"] != request["server"] or token["tool"] != request["tool"]:
                continue
            if token["args_fingerprint"] is not None and token["args_fingerprint"] != request["args_fingerprint"]:
                continue
            if token["key_id"] is not None and str(token["key_id"]) != str(request["key_id"]):
                continue
            covered.add(str(request["request_id"]))
            break
    return covered


def _format_requests(rows: Sequence[Any], covered: set[str]) -> str:
    """The pending-request table: what an operator needs to decide what to grant."""
    if not rows:
        return (
            "no approval requests recorded: no call has needed a token yet (tool_approval_mode is off, or"
            " nothing on the tool_approval_required list has been called)."
        )
    header = ("STATE", "REQUEST_ID", "SERVER", "TOOL", "FINGERPRINT", "KEY_ID", "TIMES", "LAST_SEEN", "MODE")
    table = [
        (
            "covered" if str(row["request_id"]) in covered else "PENDING",
            str(row["request_id"]),
            _cell(row, "server"),
            _cell(row, "tool"),
            _any_args_label(row["args_fingerprint"]),
            _cell(row, "key_id"),
            _cell(row, "times_seen"),
            _ts_cell(row, "last_seen"),
            _cell(row, "mode"),
        )
        for row in rows
    ]
    lines = [f"{len(table)} approval request(s) in tool_approval_requests:", ""]
    lines.extend(_table(header, table))
    lines.append("")
    lines.append("Grant the exact shape a pending row names:  grant --request <REQUEST_ID> --uses 1 --ttl 15m")
    return "\n".join(lines)


def _format_tokens(rows: Sequence[Any]) -> str:
    """The live-token table. The two weaker shapes are labelled, never implied."""
    if not rows:
        return "no live approval tokens: every listed call still needs a grant (or every token was revoked/expired)."
    header = (
        "TOKEN_ID",
        "SERVER",
        "TOOL",
        "FINGERPRINT",
        "KEY_ID",
        "USES",
        "ISSUED_AT",
        "EXPIRES_AT",
        "ISSUED_BY",
        "REASON",
    )
    table = [
        (
            str(row["id"]),
            _cell(row, "server"),
            _cell(row, "tool"),
            _any_args_label(row["args_fingerprint"]),
            _any_key_label(row["key_id"]),
            _cell(row, "uses_so_far") + "/" + _cell(row, "max_uses"),
            _ts_cell(row, "issued_at"),
            _ts_cell(row, "expires_at"),
            _cell(row, "issued_by"),
            _cell(row, "reason"),
        )
        for row in rows
    ]
    lines = [f"{len(table)} live approval token(s) in tool_approvals:", ""]
    lines.extend(_table(header, table))
    return "\n".join(lines)


async def _cmd_list(conn: asyncpg.Connection) -> int:
    """Print pending requests and live tokens. The read that precedes every grant."""
    try:
        requests = await conn.fetch(_REQUESTS_SQL)
        tokens = await conn.fetch(_LIVE_TOKENS_SQL)
    except asyncpg.UndefinedTableError as exc:
        raise _missing_table() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the approval listing: {exc}", code=1) from exc
    print(_format_requests(requests, _covered(requests, tokens)))
    print()
    print(_format_tokens(tokens))
    return 0


def _token_id(text: str | None) -> uuid.UUID:
    """Parse a UUID argument, loudly (a typo must not silently address nothing)."""
    try:
        return uuid.UUID(str(text).strip())
    except (TypeError, ValueError, AttributeError) as exc:
        raise CommandError(f"{text!r} is not a token/request id (expected a UUID).", code=2) from exc


async def _cmd_grant(conn: asyncpg.Connection, args: argparse.Namespace) -> int:
    """Turn ONE recorded request into a token scoped to its exact arguments fingerprint."""
    request_id = _token_id(args.request)
    max_uses, max_ttl = _ceilings()
    uses = max_uses if args.uses is None else int(args.uses)
    ttl_s = float(max_ttl) if args.ttl is None else _parse_ttl(args.ttl)
    _check_ceilings(uses, ttl_s)
    try:
        request = await conn.fetchrow(_REQUEST_ONE_SQL, request_id)
    except asyncpg.UndefinedTableError as exc:
        raise _missing_table() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the request lookup: {exc}", code=1) from exc
    if request is None:
        raise CommandError(
            f"no approval request {request_id}: nothing recorded that shape, so there is nothing to grant."
            " Run list first -- a grant is made against a request the gateway actually saw.",
            code=1,
        )
    fingerprint = None if args.any_args else str(request["args_fingerprint"])
    token_id = uuid.uuid4()
    issued_by = args.issued_by or _operator()
    reason = args.reason or "granted from approval request " + str(request_id)
    try:
        row = await conn.fetchrow(
            _GRANT_SQL,
            token_id,
            request["key_id"],
            str(request["server"]),
            str(request["tool"]),
            fingerprint,
            uses,
            ttl_s,
            issued_by,
            reason,
        )
    except asyncpg.UndefinedTableError as exc:
        raise _missing_table() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the grant: {exc}", code=1) from exc
    await audit_action(
        conn,
        actor_subject=issued_by,
        actor_source="script",
        action="tool_approval.grant",
        resource_type="tool_approval",
        resource_id=str(token_id),
        details={
            "request_id": str(request_id),
            "server": str(request["server"]),
            "tool": str(request["tool"]),
            "args_fingerprint": fingerprint,
            "any_args": fingerprint is None,
            "max_uses": uses,
            "ttl_s": ttl_s,
            "reason": reason,
        },
    )
    print(f"granted approval token {token_id}")
    print(f"  server/tool: {request['server']}__{request['tool']}")
    print(f"  arguments:   {_any_args_label(fingerprint)}")
    print(f"  uses:        {uses}")
    print(f"  expires:     {_fmt_ts(row['expires_at'] if row is not None else None)} (in {_fmt_ttl(ttl_s)})")
    print(f"  issued by:   {issued_by}")
    if fingerprint is None:
        print(
            "WARNING: --any-args issued the WEAKER shape: this token authorizes ANY arguments of the tool,",
            " so it does not pin the namespace/table/id the call names. Prefer the request's fingerprint.",
        )
    print("The gateway reads this table on every call -- no restart is needed. Revoke with:")
    print(f"  python scripts/tool_approval_admin.py revoke {token_id}")
    return 0


async def _cmd_revoke(conn: asyncpg.Connection, token_id: uuid.UUID) -> int:
    """Kill ONE token. Idempotent, but never silent about which state it found."""
    try:
        row = await conn.fetchrow(_REVOKE_SQL, token_id)
    except asyncpg.UndefinedTableError as exc:
        raise _missing_table() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the revoke: {exc}", code=1) from exc
    if row is not None:
        await audit_action(
            conn,
            actor_subject=_operator(),
            actor_source="script",
            action="tool_approval.revoke",
            resource_type="tool_approval",
            resource_id=str(token_id),
            details={},
        )
        print(f"revoked approval token {token_id}")
        print("The next call it authorized now needs a new grant -- no gateway restart is needed.")
        return 0
    try:
        state = await conn.fetchrow(_TOKEN_STATE_SQL, token_id)
    except asyncpg.UndefinedTableError as exc:
        raise _missing_table() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the token lookup: {exc}", code=1) from exc
    if state is None:
        raise CommandError(f"no approval token {token_id} exists: nothing was revoked.", code=1)
    if state["revoked_at"] is not None:
        print(f"token {token_id} was already revoked at {_fmt_ts(state['revoked_at'])}: nothing changed.")
        return 0
    print(
        f"token {token_id} is no longer live (expired at {_fmt_ts(state['expires_at'])}): nothing was revoked,"
        " and it already authorizes nothing."
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    """Open ONE connection, run the one command, always close it."""
    # Validate the arguments BEFORE connecting: a bad ask must not need a database.
    token_id = uuid.uuid4()
    if args.command == "revoke":
        token_id = _token_id(args.token_id)
    elif args.command == "grant":
        _token_id(args.request)
        _check_ceilings(
            _ceilings()[0] if args.uses is None else int(args.uses),
            float(_ceilings()[1]) if args.ttl is None else _parse_ttl(args.ttl),
        )
    conn = await _open()
    try:
        if args.command == "list":
            return await _cmd_list(conn)
        if args.command == "grant":
            return await _cmd_grant(conn, args)
        return await _cmd_revoke(conn, token_id)
    finally:
        await conn.close()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The CLI surface: list, grant one request, revoke one token."""
    ap = argparse.ArgumentParser(
        prog="tool_approval_admin.py",
        description="List, grant and revoke destructive-tool approval tokens (the operator lever).",
        epilog=(
            "A grant is made against a RECORDED request (see list), and it authorizes one exact arguments"
            " fingerprint unless --any-args is passed. scripts/ is not in the gateway image: run this from a"
            " checkout that can reach the database."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="pending approval requests + live tokens")
    grant = sub.add_parser("grant", help="issue a token for one recorded request")
    grant.add_argument("--request", required=True, metavar="ID", help="request_id from list")
    grant.add_argument("--uses", type=int, default=None, metavar="N", help="uses to authorize (default: the ceiling)")
    grant.add_argument("--ttl", default=None, metavar="D", help="lifetime, e.g. 30s/15m/2h (default: the ceiling)")
    grant.add_argument("--reason", default="", metavar="TEXT", help="why this shape is approved (recorded)")
    grant.add_argument("--issued-by", default="", metavar="WHO", help="operator identity (default: $USER)")
    grant.add_argument(
        "--any-args",
        action="store_true",
        help="WEAKER: authorize ANY arguments of the tool instead of the request's fingerprint",
    )
    revoke = sub.add_parser("revoke", help="kill one token (it authorizes nothing afterwards)")
    revoke.add_argument("token_id", metavar="TOKEN_ID", help="token id from list or a previous grant")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse, run, report a CommandError as a message + its exit code."""
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except CommandError as exc:
        print(f"tool_approval_admin: {exc.message}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
