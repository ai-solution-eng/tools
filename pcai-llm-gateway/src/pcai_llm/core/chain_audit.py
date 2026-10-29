"""Hash-chained audit helpers (2026-10-26 hardening plan P2-2).

WHY: applygate's audit file is hash-chained and verifies ok live (check it
any time with the applygate verify_audit_chain tool - the count moves, the
mechanism does not). The GATEWAY's own record - which key/model asked for which tool with
which arguments - was plain rows in Postgres: an operator (or anyone who
gets the DB) could edit or delete them and nothing would notice. For the
exfiltration attack class this is the record that matters, so it is the one
that gets tamper evidence.

TWO TRAILS, ONE SHAPE (db/schema.py holds the DDL and the full rationale):
  * admin_audit    - control plane; chained inside core.audit.audit_action.
  * mcp_call_audit - the MCP tool calls themselves; written by write_mcp_call
                     from the gate chain and the relay executor.

CHAIN SHAPE: row_hash = sha256(prev_hash || "|" || canonical_row_text).
The first row chains from "" (the empty prev). canonical_row_text is the
pipe-joined field list with None normalised to "" - so a field REORDER or a
type change is a chain break, which is the point.

RESIDUAL (stated, not hidden): the prev_hash is read in the same statement
that inserts, so two CONCURRENT writers can both chain from the same prev -
a FORK, not a tamper. verify_*() reports a fork distinctly from a break, and
both admin mutations and MCP tool calls are low-volume relative to chat, so
this is an accepted residual rather than a locking hot path.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from ..db import db

log = logging.getLogger(__name__)


def is_unique_violation(exc: BaseException) -> bool:
    """True when exc is a UNIQUE-constraint violation (the fork-guard race).

    Matched by class name AND message so the module does not have to import
    asyncpg (chain_audit is used by tests that fake the db seam entirely).
    """
    if type(exc).__name__ in ("UniqueViolationError", "IntegrityError"):
        return True
    text = str(exc).lower()
    return "duplicate key value" in text or "unique constraint" in text


def canonical(parts: list[Any]) -> str:
    """Pipe-joined, None-normalised, str()-ed field list."""
    return "|".join("" if p is None else str(p) for p in parts)


def chain_hash(prev_hash: str, parts: list[Any]) -> str:
    """row_hash for one row: sha256 over prev_hash + the canonical row text."""
    material = (prev_hash or "") + "|" + canonical(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def args_digest(arguments: Any) -> tuple[str, int]:
    """(sha256 hex, byte length) of the arguments payload.

    The trail stores the DIGEST, never the arguments: it must answer "what was
    asked" without becoming a second copy of the payload - and without storing
    whatever the model may have been trying to exfiltrate. Deterministic JSON
    (sort_keys, no spaces) so the same call always digests the same.
    """
    try:
        blob = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # broad-by-design: a digest must never fail a tool call
        try:
            blob = repr(arguments)
        except Exception:  # a hostile __repr__ must not escape either
            blob = "<undigestable>"
    raw = blob.encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest(), len(raw)


# NOTE (Python 3.14.6 parser gotcha, MEASURED twice on 2026-10-26 - once in a
# minimal repro, once against this module): a BARE "*" with NO trailing comma,
# alone on its own line, followed by the parameter list, silently DROPS the
# first keyword-only parameter from the signature. Exact repro (fails):
#     async def f(\n    *\n    key_id,\n    server: str,\n) -> None: ...
#   -> ast kwonlyargs == ['server']          (key_id becomes a LOCAL)
# Both of these work (ast kwonlyargs == ['key_id', 'server']):
#     async def f(\n    *,\n    key_id,\n    server: str,\n) -> None: ...
#     async def f(*, key_id, server: str) -> None: ...
# So the "*," form is fine and the bare "*" form is not. Keep this signature
# on one line - do not reformat it.
async def write_mcp_call(*, key_id: Any,
    server: str,
    tool: str,
    arguments: Any,
    outcome: str,
    model: str = "",
    request_id: str = "",
    detail: str = "",
) -> None:
    """Append ONE chained mcp_call_audit row. Best-effort BY CONTRACT: an
    audit failure logs loudly and never fails the tool call it describes
    (the same tradeoff core.audit documents at length - a missing trail line
    must not turn a successful tool call into a reported failure)."""
    try:
        digest, size = args_digest(arguments)
    except Exception:  # never-fatal: a digest fault must not fail the call
        log.exception("mcp_call_audit: could not digest arguments (row will record an empty digest)")
        digest, size = "", 0
    chained_args = (
        key_id,
        server[:200],
        tool[:200],
        digest,
        size,
        outcome,
        (model or "")[:200],
        (request_id or "")[:200],
        (detail or "")[:200],
        "|" + canonical([key_id, server[:200], tool[:200], digest, size, outcome,
                         (model or "")[:200], (request_id or "")[:200], (detail or "")[:200]]),
    )
    # FORK GUARD (wave-2 review P2-3): prev_hash is read INSIDE the inserting
    # statement, so two concurrent writers would both chain from the same prev
    # and the trail would FORK - which the verifier correctly reports as
    # not-ok, making the tamper detector red from birth on a busy gateway.
    # The schema now carries a partial UNIQUE index on prev_hash, so the loser
    # of that race gets a unique violation instead; re-running the statement
    # re-reads prev and chains onto the winner. Three attempts is generous: a
    # retry only loses when three writers land in the same instant.
    last_exc: BaseException | None = None
    for _attempt in range(3):
        try:
            await db.execute(
                """
                INSERT INTO mcp_call_audit
                    (key_id, server, tool, args_sha256, args_bytes, outcome, model,
                     request_id, detail, prev_hash, row_hash)
                SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, p.prev_hash,
                       encode(sha256(convert_to(p.prev_hash || $10, 'UTF8')), 'hex')
                  FROM (SELECT coalesce((SELECT row_hash FROM mcp_call_audit
                                          ORDER BY id DESC LIMIT 1), '') AS prev_hash) p
                """,
                *chained_args,
            )
            return
        except Exception as exc:
            last_exc = exc
            if not is_unique_violation(exc):
                break
    try:
        if last_exc is None:  # pragma: no cover - defensive
            return
        raise last_exc
    except Exception:
        # DEGRADED PATH, deliberate: the chain is an ENHANCEMENT to a trail
        # that must exist. If the chained INSERT cannot run (a database
        # without the built-in sha256(bytea), a pre-migration schema), losing
        # the audit row would be strictly worse than losing its tamper
        # evidence - so retry WITHOUT the chain columns and say so loudly.
        # The verifier reports these rows as kind="unchained", which is the
        # honest label for them.
        log.warning(
            "mcp_call_audit: chained insert failed - retrying UNCHAINED (rows verify as kind=unchained)",
            exc_info=True,
        )
        try:
            await db.execute(
                """
                INSERT INTO mcp_call_audit
                    (key_id, server, tool, args_sha256, args_bytes, outcome, model,
                     request_id, detail)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                *chained_args[:9],
            )
        except Exception:
            log.exception(
                "mcp_call_audit WRITE FAILED (call %s/%s outcome=%s succeeded; audit row lost) - check database health",
                server, tool, outcome,
            )

async def _verify(table: str, fields: list[str]) -> dict[str, Any]:
    """Walk one chained table by id ASC and report the worst real break.

    SEVERITY-ORDERED VERDICT (audit C5, 2026-10-29). The walk used to RETURN at
    the first row whose hash did not match, which made one degraded row (a
    never-fatal unchained write, or a pre-migration row with row_hash = '')
    the operator's ONLY verdict — every later row was never examined, so a
    real edit further down the trail was reported as "unchained", i.e. as a
    schema problem rather than as tamper. Three classes, in the order they must
    be surfaced:

      * break           - the row's hash does not match its OWN content
                          (edited fields, or an edited row_hash): TAMPER.
      * fork-or-legacy  - the row is internally consistent but does not follow
                          THIS row: two concurrent writers chained from the
                          same prev (the accepted residual), a row written
                          before chaining existed, or a spliced-in row.
      * unchained       - row_hash was never written (the degraded path). Not
                          tamper; the remedy is the DB/schema.

    A degraded row is COUNTED and stepped over (`prev` advances to its own
    hash — '' is the true predecessor state the next row chained from), so the
    verdict is the most severe class present anywhere in the trail, and
    `unchained` reports how many degraded rows were seen. `first_bad_id`
    names the first row OF THE REPORTED CLASS and `checked` is the number of
    rows the walk examined (the whole trail).

    NOT DONE (deliberate, still open): keyset paging. The fetch is one
    unbounded `ORDER BY id ASC`, so a large never-pruned trail materialises in
    one result set on the pool the data plane shares — an operational concern
    tracked separately from this correctness fix.
    """
    rows = await db.fetch(
        f"SELECT id, prev_hash, row_hash, {", ".join(fields)} FROM {table} ORDER BY id ASC"
    )
    prev = ""
    checked = 0
    unchained = 0
    first_unchained: dict[str, Any] | None = None
    first_fork: dict[str, Any] | None = None
    first_break: dict[str, Any] | None = None
    for row in rows:
        want = chain_hash(prev, [row[f] for f in fields])
        got = row["row_hash"] or ""
        if got != want:
            # A degraded row's hash is '' - it can never be "internally
            # consistent", so the self-consistency probe is skipped for it.
            self_consistent = bool(got) and chain_hash(row["prev_hash"] or "", [row[f] for f in fields]) == got
            finding = {
                "ok": False,
                "table": table,
                "first_bad_id": row["id"],
                "kind": "unchained" if not got else ("fork-or-legacy" if self_consistent else "break"),
                "expected": want,
                "found": got,
            }
            if not got:
                unchained += 1
                if first_unchained is None:
                    first_unchained = finding
            elif not self_consistent:
                if first_break is None:
                    first_break = finding
            elif first_fork is None:
                first_fork = finding
        # ADVANCE UNCONDITIONALLY: a degraded row's '' IS the predecessor state
        # the following row chained from, so stepping over it keeps every later
        # row's expectation correct (this is what lets the walk reach a real
        # edit that sits after a degraded row).
        prev = got
        checked += 1
    # `checked` is the number of rows this verification EXAMINED (the whole
    # trail now, always) — in both the ok and the not-ok shape.
    worst = first_break or first_fork or first_unchained
    if worst is None:
        return {
            "ok": True,
            "table": table,
            "checked": checked,
            "first_bad_id": None,
            "kind": "",
            "unchained": 0,
        }
    return {**worst, "checked": checked, "unchained": unchained}


async def verify_admin_audit_chain() -> dict[str, Any]:
    """Verify admin_audit. Fields MUST match the order core.audit.audit_action
    chains over - a change in one place without the other is a chain break."""
    return await _verify("admin_audit", ADMIN_AUDIT_CHAIN_FIELDS)


async def verify_mcp_call_chain() -> dict[str, Any]:
    return await _verify("mcp_call_audit", MCP_CALL_CHAIN_FIELDS)


# The chained field lists are MODULE CONSTANTS so the writer and the verifier
# cannot drift: audit_action imports ADMIN_AUDIT_CHAIN_FIELDS and builds its
# hash from exactly these, in exactly this order.
ADMIN_AUDIT_CHAIN_FIELDS: list[str] = [
    "actor_key_id",
    "actor_subject",
    "actor_source",
    "action",
    "resource_type",
    "resource_id",
    "details_sha256",
    "request_id",
    "source_ip",
]

MCP_CALL_CHAIN_FIELDS: list[str] = [
    "key_id",
    "server",
    "tool",
    "args_sha256",
    "args_bytes",
    "outcome",
    "model",
    "request_id",
    "detail",
]