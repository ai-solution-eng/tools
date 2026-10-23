"""Agent trace persistence: one row per relay step, request-grouped reads.

WHAT: the observability twin of the usage ledger. While `settle()` answers
"what did this cost", `agent_traces` answers "what did the agent DO and how
long did each hop take" — one row per model iteration (phase='chat': wall
time + observed usage) and one per tool call (phase='tool': server/tool/ms/
status), all grouped by request_id.

DISCIPLINE (mirrors the schema comment): trace writes are BEST-EFFORT —
`record()` swallows every failure at WARNING. A broken/missing trace
pipeline must never fail a request that the ledger already settled.

READS: `recent()` / `get()` return REQUEST-GROUPED timelines in ONE query
(GROUP BY request_id with json_agg of the steps ordered by ts, joined to
api_keys/users for the actor names and to subjects for the Phase A identity
projection) — the admin console's trace explorer shape:

    [{request_id, key_id, user_id, user_name, key_name, model,
      bound_via, subject, subject_idp,
      started_ts, last_ts,
      steps: [{iteration, phase, server, tool, ms, prompt_tokens,
               completion_tokens, status}, ...],
      totals: {steps, tool_ms, chat_ms, tool_calls}}, ...]
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from ..db import db

log = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO agent_traces
    (request_id, key_id, user_id, model, iteration, phase,
     server, tool, ms, prompt_tokens, completion_tokens, status, thread_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""


async def record(
    request_id: str,
    key_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    model: str,
    iteration: int,
    phase: str,
    server: str = "",
    tool: str = "",
    ms: int | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    status: str = "ok",
    thread_id: str | None = None,
) -> None:
    """Write ONE trace row. BEST-EFFORT: never raises — a trace write
    failure is logged at WARNING and swallowed (trace writes must never
    fail a request; the ledger settle that already happened is the record
    that matters).

    `key_id` is NOT NULL in the schema: callers without a key (keyless
    unit-test runs of the relay) skip tracing upstream of this function,
    the same gate the settle contract uses.
    """
    try:
        await db.execute(
            _INSERT_SQL,
            request_id,
            key_id,
            user_id,
            model,
            iteration,
            phase,
            server,
            tool,
            ms,
            prompt_tokens,
            completion_tokens,
            status,
            thread_id,
        )
    except Exception:
        log.warning(
            "agent trace write failed (request_id=%s phase=%s iteration=%s) — continuing",
            request_id,
            phase,
            iteration,
            exc_info=True,
        )


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one GROUP BY row into the documented timeline shape.

    asyncpg returns json_agg as TEXT — accept either a JSON string or an
    already-decoded list (fake-DB tests build plain dicts). Totals are
    derived from the steps themselves, so the read path stays one query.
    """
    steps: Any = row.get("steps") or []
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except (json.JSONDecodeError, TypeError):
            steps = []
    steps = [dict(s) for s in steps]
    tool_ms = sum(int(s.get("ms") or 0) for s in steps if s.get("phase") == "tool")
    chat_ms = sum(int(s.get("ms") or 0) for s in steps if s.get("phase") == "chat")
    return {
        "request_id": row.get("request_id"),
        "key_id": row.get("key_id"),
        "user_id": row.get("user_id"),
        "user_name": row.get("user_name"),
        "key_name": row.get("key_name"),
        # Phase A identity projections (read-time join through the key's
        # subject FK): bound_via '' / subject None for unbound keys.
        "bound_via": row.get("bound_via") or "",
        "subject": row.get("subject"),
        "subject_idp": row.get("subject_idp"),
        "model": row.get("model"),
        "started_ts": row.get("started_ts"),
        "last_ts": row.get("last_ts"),
        "thread_id": row.get("thread_id"),
        "steps": steps,
        "totals": {
            "steps": len(steps),
            "tool_ms": tool_ms,
            "chat_ms": chat_ms,
            "tool_calls": sum(1 for s in steps if s.get("phase") == "tool"),
            # client handbacks: the model asked for tools the CLIENT executes
            # (harness-side loops like DSH) — surfaced separately so a card
            # can say "2 steps · 1 handed to client" instead of looking
            # toolless
            "handed_back": sum(1 for s in steps if s.get("phase") == "tool" and s.get("status") == "handed_back"),
        },
    }


async def _fetch_grouped(conds: list[str], args: list[Any], limit: int) -> list[dict[str, Any]]:
    """The single grouped query behind recent()/get(): GROUP BY request_id,
    steps aggregated in ts order, actor names joined in. One round-trip.
    """
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    args.append(limit)
    rows = await db.fetch(
        f"""
        SELECT t.request_id, t.key_id, t.user_id,
               -- FIRST-step model, not MIN (audit finding 20): a request that
               -- cascaded or failover-switched mid-stream carries several
               -- models under one request_id; MIN picked the
               -- lexicographically-smallest name, mis-attributing cost in
               -- the trace explorer exactly where attribution matters.
               (array_agg(t.model ORDER BY t.ts, t.id))[1] AS model,
               MIN(t.ts)    AS started_ts,
               MAX(t.ts)    AS last_ts,
               MIN(t.thread_id) AS thread_id,
               k.name AS key_name, u.name AS user_name,
               k.bound_via AS bound_via, s.subject AS subject, s.idp AS subject_idp,
               COALESCE(
                   json_agg(json_build_object(
                       'iteration', t.iteration,
                       'phase', t.phase,
                       'server', t.server,
                       'tool', t.tool,
                       'ms', t.ms,
                       'prompt_tokens', t.prompt_tokens,
                       'completion_tokens', t.completion_tokens,
                       'status', t.status
                   ) ORDER BY t.ts, t.id),
                   '[]'::json
               ) AS steps
        FROM agent_traces t
        LEFT JOIN api_keys k ON k.id = t.key_id
        LEFT JOIN users u ON u.id = t.user_id
        LEFT JOIN subjects s ON s.id = k.subject_id
        {where}
        GROUP BY t.request_id, t.key_id, t.user_id, k.name, u.name, k.bound_via, s.subject, s.idp
        ORDER BY MAX(t.ts) DESC
        LIMIT ${len(args)}
        """,
        *args,
    )
    return [_shape(dict(r)) for r in rows]


async def recent(
    user_id: uuid.UUID | None = None,
    key_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Request-grouped trace timelines, newest request first.

    Filters (AND-ed): user_id, key_id, since (rows at/after the timestamp).
    `limit` bounds the number of REQUESTS returned (each carries its full
    step list); floor-clamped to 1. The ≤200 cap is the admin route's
    concern — internal callers may ask for more.
    """
    conds: list[str] = []
    args: list[Any] = []
    if user_id is not None:
        args.append(user_id)
        conds.append(f"t.user_id = ${len(args)}")
    if key_id is not None:
        args.append(key_id)
        conds.append(f"t.key_id = ${len(args)}")
    if since is not None:
        args.append(since)
        conds.append(f"t.ts >= ${len(args)}::timestamptz")
    return await _fetch_grouped(conds, args, max(1, limit))


async def get(request_id: str) -> dict[str, Any] | None:
    """One request's full trace timeline (same shape as recent() entries),
    or None when nothing was traced for it."""
    rows = await _fetch_grouped(["t.request_id = $1"], [request_id], 1)
    return rows[0] if rows else None
