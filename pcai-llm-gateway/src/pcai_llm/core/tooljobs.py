"""Async tool-call JOBS - the gateway-owned registry behind a detached tool call.

WHY this module exists (design: documentation/design/async-tool-execution.md):

A relay tool call is bounded by the INGRESS ENVELOPE, not by the tool. The
relay's wall clock is min(tool_relay_wall_clock_s, upstream_timeout_s -
tool_relay_ingress_headroom_s) - 570 s on code defaults, 870 s on G2, inside a
960 s Istio virtualService timeout (core.toolrelay.wall_clock_deadline). Past
that, the loop's asyncio.timeout (core/toolrelay.py:3640) turns the call into
"error: tool execution exceeded the relay wall-clock budget" and THE WORK IS
LOST - a 20-minute lake query pays 14 minutes and gets an error string.

Two fleet MCP servers already worked around that by growing their own job
stores (SQLhandler src/sqlhandler/jobs.py; MultimodalRAG _UploadJobTracker), and
both hit the same scale-out bug: a per-process registry means a poll landing on
another replica answers "unknown job id" (SQLhandler's live 2026-09 incident -
a 4-replica release where 3 of every 4 status calls 404'd). This module is the
piece that must be right BEFORE anything is allowed to detach: ONE Postgres
registry, addressable from any replica, with atomic single-statement
transitions.

It deliberately contains NO relay wiring and NO settings gate. It is inert
until a later wave imports it - the routes_dispatch precedent (shipped as a
module, mounted behind a flag, then defaulted off).

DESIGN COMMITMENTS - each one rejects a concrete alternative:

1. POSTGRES, NOT PROCESS MEMORY. ROADMAP already rejected stateful /mcp because
   it "sacrifices the any-replica property for the whole federation"; an
   in-memory job hub would reintroduce exactly that for tool calls. Every fleet
   implementation that chose memory (then a filesystem store, then Redis) was
   working around the absence of this table.

2. EVERY TRANSITION IS ONE STATEMENT WITH A STATE PREDICATE. No
   read-modify-write anywhere. A complete() racing a cancel() must be resolved
   by the database, not by whichever replica happened to read first; that is
   what makes the terminal state honest when the gateway runs N replicas.

3. OWNER SCOPE IS THE API KEY, and a wrong key gets the SAME answer as a bogus
   id (SQLhandler jobs.py:42-49). A job id is a bearer capability; an error
   that distinguishes "not yours" from "not there" confirms existence to a
   caller that is guessing. get()/claim_result() therefore filter on key_id and
   return None, never a distinct "forbidden".

4. MCP "tasks" VOCABULARY, NOT A FIFTH DIALECT. The state names are the spec's
   (working | input_required | completed | failed | cancelled, mcp_types
   TaskStatus), so these rows can answer tasks/get for an /mcp client later
   without a translation layer. Cancellation is a FLAG, not a state: "we asked,
   the work may still be unwinding" is a moment EVERY cancel passes through,
   and modelling it as a state forces every reader to handle a phantom state.
   That flag-as-flag discipline is learned from SQLhandler's effective_state().

5. THE RESULT IS CLAIMED ONCE. A resumed request can be replayed by a retrying
   harness; injecting the same tool result into the conversation twice is worse
   than a 409. claim_result() is a single UPDATE guarded by
   "result_claimed_at IS NULL", so exactly one consumer wins - the same
   dispatch-once discipline as core.clientside.claim().

WHAT THIS MODULE DOES NOT DECIDE: whether a call detaches, when, and how the
result reaches the model. Those are relay policy (design doc sections 4-5) and
belong to the wave that wires it. This module only guarantees that a job, once
created, has exactly one truthful state at every instant, from any replica.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ..db import db

log = logging.getLogger(__name__)

# --- state vocabulary (MCP tasks TaskStatus; see design commitment 4) --------
STATE_WORKING = "working"
STATE_INPUT_REQUIRED = "input_required"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

#: States from which nothing may transition again. Immutability is enforced in
#: SQL (every UPDATE carries "state <> ALL(TERMINAL)"), not by callers.
TERMINAL: tuple[str, ...] = (STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED)
#: States a job may be found in while it is still doing work.
ACTIVE: tuple[str, ...] = (STATE_WORKING, STATE_INPUT_REQUIRED)

# --- bounds -----------------------------------------------------------------
#: Retention clamp. MIN keeps a caller from minting an un-pollable job; MAX
#: keeps a leaked row from outliving the operator's interest in it. The VALUE
#: is policy (the dispatch surface clamps 60..86400 too); the SHAPE - a clamp
#: that never raises - is what matters here.
MIN_TTL_S = 60.0
MAX_TTL_S = 86400.0

#: A result larger than this is not retained; the job fails instead. Honest and
#: bounded: a truncated JSON body is a corrupt artifact, not a small one, so we
#: never store a prefix of it. (The model-facing cap is a different number -
#: toolcompress handles what reaches a prompt.)
MAX_RESULT_BYTES = 4 * 1024 * 1024

#: Per-key admission cap, mirroring core.clientside.MAX_PENDING_PER_KEY: a key
#: that detaches work and never collects it must not accumulate unbounded
#: background load. Overflow refuses the NEW job, which the relay can surface as
#: a normal tool error the model can route around.
MAX_ACTIVE_PER_KEY = 64


@dataclass(frozen=True)
class Job:
    """One detached tool call. Frozen: a row is a FACT, not a mutable handle."""

    job_id: uuid.UUID
    key_id: uuid.UUID
    server: str
    tool: str
    args_sha256: str
    state: str
    status_message: str | None
    cancel_requested: bool
    result_claimed_at: Any  # datetime | None; opaque here (ordering is SQL's job)
    created_at: Any
    updated_at: Any
    expires_at: Any

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


# --- pure helpers (unit-tested without a database) --------------------------


def is_terminal(state: str) -> bool:
    """True for a state nothing may leave."""
    return state in TERMINAL


def can_transition(src: str, dst: str) -> bool:
    """May a job move src -> dst? The authority is the SQL predicate; this is
    the same table in Python so a caller can be refused before a round-trip.

    Deliberately NOT a state machine with queued/starting phases: a job exists
    only once it is working (the row is the acceptance), so there is no
    pre-work state to model. input_required is kept because MCP's elicitation
    flow needs it and dropping it later would be a wire break.
    """
    if is_terminal(src):
        return False
    if dst in ACTIVE:
        return True  # working <-> input_required
    return dst in TERMINAL


def effective_state(job: Job) -> str:
    """The state to REPORT, which can differ from the stored one.

    A cancelled job whose worker has not yet noticed is still 'working' in the
    row: the interrupt was requested, the upstream call is unwinding. Reporting
    that as 'working' invites a caller to keep polling for a result that will
    never come; reporting 'cancelled' too early claims an outcome the worker has
    not confirmed. The honest answer to a poller is 'cancelled' as soon as the
    request is recorded, because that IS the answer to "will I get a result?" -
    the work may finish, but its result is discarded.
    """
    if job.cancel_requested and not job.terminal:
        return STATE_CANCELLED
    return job.state


def clamp_ttl(ttl_s: float | None) -> float:
    """Clamp a requested retention to [MIN_TTL_S, MAX_TTL_S]. NEVER raises.

    NaN/inf are coerced to MAX_TTL_S rather than propagating: NaN compares False
    against both bounds, so a naive clamp passes it through and every
    expiry comparison silently answers "not expired" forever - the same class
    of bug the dispatch surface fixed for its ttl_s (routes_dispatch.py:110).
    """
    if ttl_s is None:
        return MAX_TTL_S
    try:
        val = float(ttl_s)
    except (TypeError, ValueError):
        return MAX_TTL_S
    if math.isnan(val):
        return MAX_TTL_S
    if math.isinf(val):
        return MAX_TTL_S if val > 0 else MIN_TTL_S
    return min(max(val, MIN_TTL_S), MAX_TTL_S)


def result_available(job: Job) -> bool:
    """True when a result exists AND nobody has claimed it yet."""
    return job.state == STATE_COMPLETED and job.result_claimed_at is None


def _row_to_job(row: Any) -> Job:
    """asyncpg.Record -> Job. Kept tolerant of a plain mapping so tests can
    feed dicts without constructing Records."""
    get = row.__getitem__
    return Job(
        job_id=get("job_id"),
        key_id=get("key_id"),
        server=get("server"),
        tool=get("tool"),
        args_sha256=get("args_sha256"),
        state=get("state"),
        status_message=get("status_message"),
        cancel_requested=bool(get("cancel_requested")),
        result_claimed_at=get("result_claimed_at"),
        created_at=get("created_at"),
        updated_at=get("updated_at"),
        expires_at=get("expires_at"),
    )


#: Canonical DDL. The project convention is db/schema.py for every table; this
#: constant exists so the module is testable and reviewable on its own, and so
#: the wave that wires it has one snippet to paste. Both copies are idempotent,
#: so having it in schema.py as well is harmless.
DDL = """
CREATE TABLE IF NOT EXISTS tool_jobs (
    job_id            UUID PRIMARY KEY,
    key_id            UUID NOT NULL,
    server            TEXT NOT NULL,
    tool              TEXT NOT NULL,
    args_sha256       TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'working',
    status_message    TEXT,
    cancel_requested  BOOLEAN NOT NULL DEFAULT FALSE,
    result_body       TEXT,
    result_bytes      INTEGER,
    result_claimed_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ NOT NULL,
    CONSTRAINT tool_jobs_state_vocab CHECK (
        state IN ('working','input_required','completed','failed','cancelled')
    )
);
-- The active-list read (a model asking "what am I waiting on?") and the
-- per-key admission count both scan exactly this slice.
CREATE INDEX IF NOT EXISTS idx_tool_jobs_active ON tool_jobs(key_id, created_at DESC)
    WHERE state IN ('working','input_required');
-- The reaper's scan. Separate index because expiry is a GLOBAL sweep, not a
-- per-key read: it must not be able to starve on one busy key.
CREATE INDEX IF NOT EXISTS idx_tool_jobs_expiry ON tool_jobs(expires_at);
"""


class JobStore:
    """Postgres-backed job registry. Every method is one statement (see
    design commitment 2) except create(), which is one INSERT..SELECT so that
    the admission check and the insert cannot be split by a concurrent create.
    """

    async def create(
        self,
        *,
        key_id: uuid.UUID,
        server: str,
        tool: str,
        args_sha256: str,
        ttl_s: float | None = None,
    ) -> Job | None:
        """Register a detached call. None = admission refused (per-key cap).

        Returns None rather than raising for the CAP because the cap is a normal
        condition the relay reports to the model as a tool error ("too many
        background jobs; wait or cancel one"). A raised exception would take the
        turn down instead of letting the model route around it.
        """
        job_id = uuid.uuid4()
        ttl = clamp_ttl(ttl_s)
        row = await db.fetchrow(
            """
            INSERT INTO tool_jobs
                (job_id, key_id, server, tool, args_sha256, state, expires_at)
            SELECT $1, $2, $3, $4, $5, 'working', now() + make_interval(secs => $6)
            WHERE (
                SELECT count(*) FROM tool_jobs
                WHERE key_id = $2 AND state = ANY($7::text[])
            ) < $8
            RETURNING *
            """,
            job_id,
            key_id,
            server,
            tool,
            args_sha256,
            float(ttl),
            list(ACTIVE),
            MAX_ACTIVE_PER_KEY,
        )
        if row is None:
            log.info(
                "tooljobs: admission refused for key %s (cap %d active)",
                key_id,
                MAX_ACTIVE_PER_KEY,
            )
            return None
        return _row_to_job(row)

    async def get(self, key_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
        """Owner-scoped read. A wrong key is INDISTINGUISHABLE from a bogus id
        (design commitment 3) - both return None."""
        row = await db.fetchrow(
            "SELECT * FROM tool_jobs WHERE job_id = $1 AND key_id = $2",
            job_id,
            key_id,
        )
        return _row_to_job(row) if row is not None else None

    async def list_active(self, key_id: uuid.UUID, limit: int = 20) -> list[Job]:
        """This key's unfinished jobs, newest first. The read behind "what am I
        still waiting on?" - the discovery verb both fleet implementations
        needed and only one documented."""
        rows = await db.fetch(
            """
            SELECT * FROM tool_jobs
            WHERE key_id = $1 AND state = ANY($2::text[])
            ORDER BY created_at DESC
            LIMIT $3
            """,
            key_id,
            list(ACTIVE),
            max(1, min(int(limit), MAX_ACTIVE_PER_KEY)),
        )
        return [_row_to_job(r) for r in rows or []]

    async def complete(self, job_id: uuid.UUID, result: str, *, status_message: str | None = None) -> bool:
        """Finish a job with a result. False = the job was no longer active
        (already terminal: completed twice, cancelled, reaped) or the result was
        too large to retain (the job is then failed, not silently truncated).

        The "state <> ALL(TERMINAL)" predicate is the race arbiter: a complete()
        that loses to a cancel() writes nothing and returns False, so the caller
        can discard the result instead of resurrecting a cancelled job.
        """
        body = result if isinstance(result, str) else str(result)
        nbytes = len(body.encode("utf-8", "replace"))
        if nbytes > MAX_RESULT_BYTES:
            # Refuse, and say so in the job's own state: a caller polling gets an
            # error explaining why, instead of a truncated artifact it would
            # parse as corrupt.
            await self.fail(
                job_id,
                f"result not retained: {nbytes} bytes exceeds the {MAX_RESULT_BYTES}-byte job result cap",
            )
            return False
        status = await db.execute(
            """
            UPDATE tool_jobs
            SET state = 'completed',
                result_body = $2,
                result_bytes = $3,
                status_message = COALESCE($4, status_message),
                updated_at = now()
            WHERE job_id = $1 AND state <> ALL($5::text[])
            """,
            job_id,
            body,
            nbytes,
            status_message,
            list(TERMINAL),
        )
        return _affected(status) == 1

    async def fail(self, job_id: uuid.UUID, message: str, *, state: str = STATE_FAILED) -> bool:
        """Finish a job without a result. 'state' is overridable so a cooperative
        cancel that the worker confirmed can land as 'cancelled' rather than a
        generic failure - the two are different answers to the caller."""
        if state not in TERMINAL:
            raise ValueError(f"fail() needs a terminal state, got {state!r}")
        status = await db.execute(
            """
            UPDATE tool_jobs
            SET state = $2, status_message = $3, updated_at = now()
            WHERE job_id = $1 AND state <> ALL($4::text[])
            """,
            job_id,
            state,
            (message or "")[:2000],
            list(TERMINAL),
        )
        return _affected(status) == 1

    async def request_cancel(self, key_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        """Record a cancel REQUEST. True = it was recorded (the job was active
        and belongs to this key). The worker is responsible for observing the
        flag and calling fail(state='cancelled'); a job whose worker never
        observes it still expires via the reaper, so a lost cancel cannot pin a
        row forever.

        Deliberately NOT a transition: the gateway cannot interrupt an arbitrary
        upstream tool (there is no universal interrupt point - SQLhandler's
        DuckDB interrupt() is the exception, not the rule). Claiming 'cancelled'
        here would be a lie for most servers; the flag plus effective_state()
        gives the caller the honest answer immediately.
        """
        status = await db.execute(
            """
            UPDATE tool_jobs
            SET cancel_requested = TRUE, updated_at = now()
            WHERE job_id = $1 AND key_id = $2 AND state <> ALL($3::text[])
            """,
            job_id,
            key_id,
            list(TERMINAL),
        )
        return _affected(status) == 1

    async def claim_result(self, key_id: uuid.UUID, job_id: uuid.UUID) -> str | None:
        """Hand the result over ONCE, atomically. None = nothing to claim (not
        completed, already claimed, or not this key's job).

        The single UPDATE is what makes a replayed request safe: two consumers
        racing (a retrying harness, two replicas) cannot both win, because
        'result_claimed_at IS NULL' is in the predicate (design commitment 5).
        """
        row = await db.fetchrow(
            """
            UPDATE tool_jobs
            SET result_claimed_at = now(), updated_at = now()
            WHERE job_id = $1 AND key_id = $2
              AND state = 'completed' AND result_claimed_at IS NULL
            RETURNING result_body
            """,
            job_id,
            key_id,
        )
        return row["result_body"] if row is not None else None

    async def reap_expired(self, limit: int = 100) -> int:
        """Delete expired jobs; returns how many. Global sweep, called by a
        housekeeping loop - the existing core.housekeeping 60 s tick is the
        natural host (design doc section 9).

        Deleting a row loses its audit trail, which is why the audit row (a
        SEPARATE, append-only table) is written at completion and not here: the
        job table is working state, the audit table is the record.
        """
        rows = await db.fetch(
            "DELETE FROM tool_jobs WHERE job_id IN ("
            "  SELECT job_id FROM tool_jobs WHERE expires_at < now()"
            "  ORDER BY expires_at LIMIT $1"
            ") RETURNING job_id",
            max(1, int(limit)),
        )
        return len(rows or [])


def _affected(status: Any) -> int:
    """Row count from an asyncpg execute() status tag ('UPDATE 1').

    Tolerated as a bare int for monkeypatched tests. A parse failure returns -1
    (NOT 0) so a caller can never mistake an unreadable status for "no row
    matched" - which would silently drop a legitimate completion.
    """
    if isinstance(status, int):
        return status
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return -1


#: Process-wide store, the core.clientside hub precedent. Stateless itself -
#: all state is in Postgres - so instantiating it per call would be equally
#: correct; the singleton just keeps call sites uniform.
jobs = JobStore()


def now_s() -> float:
    """Monotonic clock for TTL arithmetic in callers that need it. Exposed here
    so a wiring wave does not invent its own (the wall_clock_deadline lesson:
    one formula, both callers, so the two cannot drift)."""
    return time.monotonic()
