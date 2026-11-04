"""Agent trace persistence: one row per relay step, request-grouped reads.

WHAT: the observability twin of the usage ledger. While `settle()` answers
"what did this cost", `agent_traces` answers "what did the agent DO and how
long did each hop take" — one row per model iteration (phase='chat': wall
time + observed usage) and one per tool call (phase='tool': server/tool/ms/
status), all grouped by request_id.

DISCIPLINE (mirrors the schema comment): trace writes are BEST-EFFORT —
`record()` swallows every failure at WARNING. A broken/missing trace
pipeline must never fail a request that the ledger already settled.

READS: both return REQUEST-GROUPED timelines (GROUP BY request_id with
json_agg of the steps ordered by ts, joined to api_keys/users for the actor
names and to subjects for the Phase A identity projection) — the admin
console's trace explorer shape. The ROW shape is one entry per request, but
the SELECTION differs (2026-11-02, owner request): `recent()` bounds
CONVERSATIONS — it picks the N most recent thread_ids (rows with no thread
fall back to one unit per request) and then returns every request of those
threads, so a long conversation renders complete instead of as the fragment
of its requests that happened to land in a request-counted window.
`get()` stays request-scoped and single-query. Explorer shape:

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
     server, tool, ms, prompt_tokens, completion_tokens, status, thread_id,
     cached_tokens)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
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
    cached_tokens: int = 0,
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
            max(0, int(cached_tokens)),
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
    already-decoded list (fake-DB tests build plain dicts).

    TOTALS COME FROM SQL when the row carries them (2026-11-02, conversation
    reads): a conversation now returns IN FULL, so the payload's only bound
    is the per-request step array. Deriving the totals from that array would
    make them a function of what the payload happened to carry; the SQL
    aggregates (count/SUM ... FILTER) are exact for the request regardless.
    A row without them (a hand-built test row, an older seam) still derives
    them from the steps, so the shape is unchanged for every caller.
    """
    steps: Any = row.get("steps") or []
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except (json.JSONDecodeError, TypeError):
            steps = []
    steps = [dict(s) for s in steps]
    sql_steps = row.get("steps_total")
    if sql_steps is None:
        totals = {
            "steps": len(steps),
            "tool_ms": sum(int(s.get("ms") or 0) for s in steps if s.get("phase") == "tool"),
            "chat_ms": sum(int(s.get("ms") or 0) for s in steps if s.get("phase") == "chat"),
            "tool_calls": sum(1 for s in steps if s.get("phase") == "tool"),
            # client handbacks: the model asked for tools the CLIENT executes
            # (harness-side loops like DSH) — surfaced separately so a card
            # can say "2 steps · 1 handed to client" instead of looking
            # toolless
            "handed_back": sum(1 for s in steps if s.get("phase") == "tool" and s.get("status") == "handed_back"),
            # prefix-cache hits the upstream reported for this request: the
            # honest "how much of the prompt was already warm" number.
            "cached_tokens": sum(int(s.get("cached_tokens") or 0) for s in steps),
            "prompt_tokens": sum(int(s.get("prompt_tokens") or 0) for s in steps),
        }
    else:
        totals = {
            "steps": int(sql_steps),
            "tool_ms": int(row.get("tool_ms") or 0),
            "chat_ms": int(row.get("chat_ms") or 0),
            "tool_calls": int(row.get("tool_calls") or 0),
            "handed_back": int(row.get("handed_back") or 0),
            "cached_tokens": int(row.get("cached_tokens") or 0),
            "prompt_tokens": int(row.get("prompt_tokens") or 0),
        }
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
        "totals": totals,
    }


# One step's JSON. The step ARRAY is the only unbounded part of the payload (a
# runaway relay loop can emit thousands of steps for one request), which is
# why every total below is computed by SQL rather than from this array.
_STEP_JSON = """json_build_object(
                       'iteration', t.iteration,
                       'phase', t.phase,
                       'server', t.server,
                       'tool', t.tool,
                       'ms', t.ms,
                       'prompt_tokens', t.prompt_tokens,
                       'completion_tokens', t.completion_tokens,
                       'cached_tokens', t.cached_tokens,
                       'status', t.status
                   )"""


def _grouped_select(where: str) -> str:
    """The per-REQUEST timeline projection shared by every read shape.

    thread_id is a GROUPING KEY, not an aggregate (2026-11-02). It is
    functionally dependent on request_id in every writer, so the row shape is
    unchanged — but MIN() could silently pick one value if a request ever
    carried two, and the conversation read needs the value to be exact.

    TOTALS ARE SQL (count/SUM ... FILTER): the conversation read returns whole
    threads, so the totals must not be a function of how many steps the
    payload happened to carry.
    """
    return f"""
        SELECT t.request_id, t.key_id, t.user_id, t.thread_id,
               -- FIRST-step model, not MIN (audit finding 20): a request that
               -- cascaded or failover-switched mid-stream carries several
               -- models under one request_id; MIN picked the
               -- lexicographically-smallest name, mis-attributing cost in
               -- the trace explorer exactly where attribution matters.
               (array_agg(t.model ORDER BY t.ts, t.id))[1] AS model,
               MIN(t.ts)    AS started_ts,
               MAX(t.ts)    AS last_ts,
               count(*)     AS steps_total,
               COALESCE(SUM(t.cached_tokens), 0)::bigint AS cached_tokens,
               COALESCE(SUM(t.prompt_tokens), 0)::bigint AS prompt_tokens,
               COALESCE(SUM(t.ms) FILTER (WHERE t.phase = 'tool'), 0) AS tool_ms,
               COALESCE(SUM(t.ms) FILTER (WHERE t.phase = 'chat'), 0) AS chat_ms,
               count(*) FILTER (WHERE t.phase = 'tool') AS tool_calls,
               count(*) FILTER (WHERE t.phase = 'tool' AND t.status = 'handed_back') AS handed_back,
               k.name AS key_name, u.name AS user_name,
               k.bound_via AS bound_via, s.subject AS subject, s.idp AS subject_idp,
               COALESCE(
                   json_agg({_STEP_JSON} ORDER BY t.ts, t.id),
                   '[]'::json
               ) AS steps
        FROM agent_traces t
        LEFT JOIN api_keys k ON k.id = t.key_id
        LEFT JOIN users u ON u.id = t.user_id
        LEFT JOIN subjects s ON s.id = k.subject_id
        {where}
        GROUP BY t.request_id, t.key_id, t.user_id, t.thread_id,
                 k.name, u.name, k.bound_via, s.subject, s.idp
        ORDER BY MAX(t.ts) DESC
    """


async def _fetch_grouped(conds: list[str], args: list[Any], limit: int) -> list[dict[str, Any]]:
    """ONE request-scoped grouped query — get()'s path.

    Kept as the single-statement shape: get() addresses exactly one
    request_id, so the conversation selection below would be a wasted round
    trip.
    """
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    rows = await db.fetch(_grouped_select(where) + f"\n        LIMIT ${len(args) + 1}", *args, limit)
    return [_shape(dict(r)) for r in rows]


async def _select_units(conds: list[str], args: list[Any], limit: int) -> tuple[list[str], list[str]]:
    """The N most recent CONVERSATION UNITS under the filters.

    A unit is a non-NULL thread_id — one conversation, however many requests
    it spans — or, for rows with NO thread (pre-column rows, the distill trace
    write, a request whose messages carry no user turn), a single request_id.
    Unthreaded traffic therefore keeps the old per-request grouping instead of
    collapsing into one pseudo-conversation.

    WHY a separate query: the bound must apply to UNITS, and no LIMIT can be
    applied before a GROUP BY request_id inside one statement. The cost is one
    extra round trip on an admin/observability route (never the data plane);
    the fetch that follows is key-driven by what this returns.
    """

    def branch(pred: str) -> str:
        return f"WHERE {' AND '.join([*conds, pred])}"

    sql = f"""
        SELECT t.thread_id AS thread_id, NULL::text AS request_id, MAX(t.ts) AS last_ts
        FROM agent_traces t
        {branch("t.thread_id IS NOT NULL")}
        GROUP BY t.thread_id
        UNION ALL
        SELECT NULL::text AS thread_id, t.request_id AS request_id, MAX(t.ts) AS last_ts
        FROM agent_traces t
        {branch("t.thread_id IS NULL")}
        GROUP BY t.request_id
        ORDER BY last_ts DESC
        LIMIT ${len(args) + 1}
    """
    rows = await db.fetch(sql, *args, limit)
    threads: list[str] = []
    requests: list[str] = []
    for raw in rows:
        row = raw if isinstance(raw, dict) else dict(raw)
        thread = row.get("thread_id")
        request = row.get("request_id")
        if thread is not None:
            threads.append(str(thread))
        elif request is not None:
            requests.append(str(request))
    return threads, requests


async def _fetch_units(
    conds: list[str], args: list[Any], threads: list[str], requests: list[str]
) -> list[dict[str, Any]]:
    """EVERY row of the selected conversations, grouped per request.

    NO LIMIT, deliberately: a selected conversation comes back in full — that
    is the whole point. The payload is bounded by the unit count above and by
    the per-request step array, never by truncating a conversation.
    """
    unit = (
        f"(t.thread_id = ANY(${len(args) + 1}::text[])"
        f" OR (t.thread_id IS NULL AND t.request_id = ANY(${len(args) + 2}::text[])))"
    )
    where = f"WHERE {' AND '.join([*conds, unit])}"
    rows = await db.fetch(_grouped_select(where), *args, threads, requests)
    return [_shape(dict(r)) for r in rows]


async def recent(
    user_id: uuid.UUID | None = None,
    key_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = 50,
    enrollment_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Request-grouped trace timelines, newest request first.

    Filters (AND-ed): user_id, key_id, since (rows at/after the timestamp),
    enrollment_id (team leader tier).

    `limit` bounds CONVERSATIONS, not requests (2026-11-02, owner request).
    The N most recent units are selected first — a unit is a thread_id, or a
    single request when the thread is NULL — and then EVERY request of those
    units is returned. The row shape is unchanged (one entry per request, the
    console groups them by thread_id as before), so a long conversation now
    renders COMPLETE instead of as whatever fragment of its requests happened
    to land in a request-counted window. floor-clamped to 1; the ≤200 cap is
    the admin route's concern — internal callers may ask for more.

    enrollment_id scopes the read to ONE team: the predicate is a subselect on
    api_keys.enrollment_id rather than a join, because the GROUP BY is keyed on
    t.key_id and an enrollment JOIN would need a second grouping key. A
    subselect keeps the existing shape (and its indexes) exactly as the admin
    explorer sees them — the team console is the same query with one more AND,
    applied to BOTH the unit selection and the fetch. An id outside the
    caller's scope therefore matches no row: 404, never 403 (no existence
    oracle) — a leader can never probe another team's conversations.
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
    if enrollment_id is not None:
        args.append(enrollment_id)
        conds.append(f"t.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = ${len(args)})")
    threads, requests = await _select_units(conds, args, max(1, limit))
    if not threads and not requests:
        # Nothing due: one query, and the fetch would bind two empty arrays.
        return []
    return await _fetch_units(conds, args, threads, requests)


async def get(request_id: str, enrollment_id: uuid.UUID | None = None) -> dict[str, Any] | None:
    """One request's full trace timeline (same shape as recent() entries),
    or None when nothing was traced for it.

    enrollment_id (team leader tier) adds the SAME team-scoping predicate as
    recent(): a request traced for a key outside the caller's enrollment
    resolves to None, which the route maps to 404 — a team leader can never
    probe for the existence of another team's request ids.
    """
    conds = ["t.request_id = $1"]
    args: list[Any] = [request_id]
    if enrollment_id is not None:
        args.append(enrollment_id)
        conds.append(f"t.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = ${len(args)})")
    rows = await _fetch_grouped(conds, args, 1)
    return rows[0] if rows else None
