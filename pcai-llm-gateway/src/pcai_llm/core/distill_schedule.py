"""Distill scheduling: the DSH plugin session lifecycle, gateway-side.

The DSH plugin distills a session when it goes QUIET (debounce) and
RE-distills when it continues, so the stored memory always tracks the
conversation latest state (a new distill REPLACES the picture; RAG
near-dedup folds the superseded generations). This module is that same
lifecycle for gateway-persisted conversations:

  * mark_stale: after replace_messages the conversation is due for
    distill (delete the marker row - a missing row = due).
  * mark_distilled: after a successful distill, record the transcript
    length distilled (the marker).
  * due(): a conversation is due when unmarked, or its current message
    count exceeds the distilled marker (it continued), or the marker is
    older than the debounce quiet-window.

The housekeeping loop sweeps due conversations for keys whose memory
feature is attached and distills them (offpeak_only gates WHEN; explicit
/v1/distill calls always run). All state access is best-effort: a marker
failure must never fail a conversation write or the sweep.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from ..db import db
from . import metrics

log = logging.getLogger(__name__)

# Attachment-gate backoff (2026-10-09 production stall). A row skipped because
# its key fails the memory attachment gate is NOT marked distilled (the marker
# means "a write landed"), but an unmarked row is re-selected OLDEST-FIRST
# every pass. When the gate itself is broken -- an empty registry snapshot
# makes resolve_memory_server() return None for EVERY key -- the same LIMIT-20
# window is retried forever and nothing newer is ever reached: the queue
# freezes, and because the skip path only logged at INFO the stall was silent
# for days. Suppress a gate-skipped row for this long so the window advances;
# the row returns to the due set automatically once the window lapses, so a
# recovered gate still drains the backlog. The window is
# settings.distiller_gate_backoff_s (default 900s).


# Per-ROW model-time allowance for one sweep pass (audit D-6, forward-progress
# rule). The sweep budget bounds the SHARED housekeeping loop; it must never
# bound a single row so tightly that the row can never finish. One row is up to
# ~25 map calls x distiller_llm_timeout_s (120 s default) = ~50 minutes of model
# time, and the due query is OLDEST-FIRST: a row that always aborts mid-map
# never writes a marker, never backs off, and is re-selected first on EVERY
# pass — starving everything behind it forever. That is the 2026-10-09
# head-of-line stall shape this module already carries two scars from.
#
# The rule the sweep implements:
#   * the FIRST row of a pass is never cut by the sweep budget at all
#     (unbounded — the pass exists to make progress, and row #1 is the row
#     that has been waiting longest);
#   * every later row gets max(sweep_budget, ROW_BUDGET_S) from ITS OWN start,
#     so a large row still gets a generous slice before the loop is released;
#   * the shared budget keeps gating whether another row is STARTED.
# A row is therefore never starved indefinitely by the sweep budget alone.
ROW_BUDGET_S = 900.0


def row_budget_seconds() -> float:
    """The per-row model-time allowance: max(sweep budget, ROW_BUDGET_S).

    <= 0 sweep budget means "no limit" (audit F2), which also means every row
    is unbounded. Kept as a function so a test/operator can see the rule in
    one place rather than reconstructing it from the loop.
    """
    from ..config import settings

    budget = settings.distiller_sweep_budget_s
    if budget <= 0:
        return 0.0  # 0 = unbounded
    return max(budget, ROW_BUDGET_S)


def gate_backoff_seconds() -> float:
    """The gate-skip suppression window (settings-tunable; 0 = retry next
    pass, which lets a broken gate pin the oldest-first window — not
    recommended, kept for escape-hatch completeness)."""
    from ..config import settings

    return settings.distiller_gate_backoff_s


async def mark_stale(conversation_id: uuid.UUID, key_id: uuid.UUID) -> None:
    """The conversation continued: drop the distilled marker (best-effort).

    WARNING, not DEBUG (adversarial review 2026-10-14): this was the last
    distiller swallow still invisible at LOG_LEVEL=INFO, and it is a REAL
    hole rather than a cosmetic one. The marker is what the due predicate
    compares against (`message count > distilled_seq`); if this DELETE fails
    silently, a conversation that shrank or rewrote its transcript keeps a
    stale marker, can never satisfy the predicate again, and is skipped
    FOREVER with no operator-visible trace. The delete itself must stay
    best-effort (it runs inside the conversation-write path and must never
    fail a user's write) -- but its failure must be visible.
    """
    try:
        await db.execute("DELETE FROM conversation_distills WHERE conversation_id = $1", conversation_id)
    except Exception:
        log.warning(
            "distill marker delete skipped (conv=%s) -- if this repeats, the conversation "
            "may never re-enter the due set",
            conversation_id,
            exc_info=True,
        )


async def mark_distilled(
    conversation_id: uuid.UUID,
    key_id: uuid.UUID,
    distilled_seq: int,
) -> None:
    """Record the distilled state (upsert; best-effort)."""
    try:
        await db.execute(
            "INSERT INTO conversation_distills (conversation_id, key_id, distilled_seq) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (conversation_id) DO UPDATE SET "
            "distilled_seq = $3, distilled_at = now(), gate_failed_at = NULL",
            conversation_id,
            key_id,
            distilled_seq,
        )
    except Exception:
        # WARNING, not DEBUG: this swallow hid a global distiller stall.
        log.warning("distill marker write skipped", exc_info=True)


async def mark_gate_skip(conversation_id: uuid.UUID, key_id: uuid.UUID) -> None:
    """Record an attachment-gate skip for one conversation so it leaves the
    due window for GATE_BACKOFF_S (best-effort, same contract as
    mark_canonical_gate_skip). distilled_seq is preserved, so a recovered
    gate still sees the conversation as due and drains the backlog."""
    try:
        await db.execute(
            "INSERT INTO conversation_distills (conversation_id, key_id, distilled_seq, gate_failed_at) "
            "VALUES ($1, $2, 0, now()) "
            "ON CONFLICT (conversation_id) DO UPDATE SET gate_failed_at = now()",
            conversation_id,
            key_id,
        )
    except Exception:
        log.warning("gate-skip marker write skipped (conv=%s)", conversation_id, exc_info=True)


async def due_conversations(limit: int = 20) -> list[dict[str, Any]]:
    """Conversations due for distill: quiet, memory-attached, and (never
    distilled OR continued since the last distill).

    "Quiet" = no message write in the debounce window (the DSH debounce,
    at housekeeping cadence). "Memory-attached" = the key carries the
    stamped memory pair (joined via mcp_server_creds - the same source
    the attachment gate reads). The sweep NEVER distills a key that has
    no memory feature: inert by construction, not by exception.
    """
    from ..core import distiller_config

    debounce = await distiller_config.debounce_minutes()
    # NOTE (2026-10-17): no title-request guard here, deliberately. Auxiliary
    # session-title calls arrive on /v1/chat/completions, which writes the
    # CANONICAL store only; the conversations store is written by the
    # tenant-facing /v1/conversations route (a human's own chat), so a title
    # call cannot create a row here. The guard lives where the rows are made
    # (routes_proxy capture sites) and where they are swept
    # (due_canonical_conversations).
    try:
        return await db.fetch(
            """
            SELECT c.id AS conversation_id, c.key_id, count(m.id) AS msg_count, c.title,
                   d.gate_failed_at
            FROM conversations c
            JOIN conversation_messages m ON m.conversation_id = c.id
            JOIN api_keys k ON k.id = c.key_id
            JOIN mcp_server_creds cred
              ON cred.key_id = c.key_id AND cred.server_name = $1
            LEFT JOIN conversation_distills d ON d.conversation_id = c.id
            WHERE c.updated_at < now() - make_interval(mins => $2)
              AND k.revoked_at IS NULL
              AND (k.expires_at IS NULL OR k.expires_at > now())
              AND (d.conversation_id IS NULL OR (
                    SELECT count(*) FROM conversation_messages m2
                    WHERE m2.conversation_id = c.id) > d.distilled_seq)
              AND (d.gate_failed_at IS NULL
                   OR d.gate_failed_at < now() - make_interval(secs => $4))
              AND (d.conversation_id IS NULL OR d.lease_until IS NULL
                   OR d.lease_until < now())
            GROUP BY c.id, c.key_id, c.title, d.gate_failed_at
            ORDER BY c.updated_at ASC
            LIMIT $3
            """,
            _memory_server_name(),
            debounce,
            max(1, limit),
            gate_backoff_seconds(),  # $4 = gate-skip suppression window
        )
    except Exception:
        # WARNING, not DEBUG (2026-10-14): this swallow made the incident
        # invisible. The due query referenced a column no migration created;
        # it raised on EVERY pass and the only trace was the Postgres error
        # log -- the sweep looked healthy while writing nothing cluster-wide.
        log.warning("distill due sweep skipped", exc_info=True)
        return []


def decode_jsonb_messages(messages: Any) -> Any:
    """Decode a JSONB payload that asyncpg handed back as a STRING.

    The pool registers no JSONB codec (db.create_pool has no `init`;
    canonical.py documents the convention), so a `jsonb` column arrives as
    the raw JSON TEXT, not a Python list. Every predicate that asks "is this
    a list of messages?" is therefore False on the real row shape — the
    title detector silently never fired in production (audit F1/B-F1, the
    2026-10-20 title-stub leak reopened).

    Decodes at the BOUNDARY (the one place the row leaves the driver) so
    every downstream consumer sees the shape the code was written for.
    Anything that is not a JSON array of dicts is returned UNCHANGED: a
    malformed payload must never be silently replaced, and build_transcript
    already has its own recovery for the double-encoded shapes.
    """
    if not isinstance(messages, str):
        return messages
    import json

    try:
        parsed = json.loads(messages)
    except (ValueError, TypeError):
        return messages
    # A LIST OF DICTS only (cross-validation XV-2): the column holds message
    # rows. A JSON array of scalars is a different payload shape, and
    # rewriting it into a message list would corrupt it. Anything else is
    # handed through unchanged — build_transcript has its own recovery for
    # the double-encoded shapes.
    if isinstance(parsed, list) and all(isinstance(m, dict) for m in parsed):
        return parsed
    return messages


def _exclude_title_rows(rows: list[Any], limit: int, dropped_out: list[Any] | None = None) -> list[Any]:
    """Drop stored session-title rows from the due window, then trim to LIMIT.

    2026-10-17: rows captured from auxiliary session-title calls before the
    capture guard existed are still in canonical_conversations and would keep
    distilling into bogus memories forever. They are dropped from the due set
    but deliberately NOT marked distilled — if the detector is ever narrowed,
    they become eligible again instead of being silently buried. (The sweep
    stamps the bounded gate-skip backoff instead, so a dropped row cannot pin
    the oldest-first window; see distill_schedule.sweep.)

    JSONB DECODED FIRST (audit F1): the row's `messages` arrives as a STRING
    from asyncpg, and `is_title_messages` starts with
    `if not isinstance(messages, list) ... return False` — so this whole
    filter was INERT in production (every row classified as "not a title")
    while passing every unit test, because the tests inject a list.

    Never raises: an unclassifiable row is KEPT (a real conversation is far
    dearer than one stray title memory), and a detector fault must not stop
    the feed. Extracted from due_canonical_conversations so the due query
    itself stays a single, auditable SQL + param-order block.
    """
    from . import title_requests

    kept: list[Any] = []
    for row in rows:
        # NEVER MUTATE THE RECORD (cross-validation XV-1). asyncpg hands back a
        # Record, which is IMMUTABLE: item assignment raises TypeError. Doing
        # it in place was swallowed by the `except` below — so the filter
        # stayed INERT on real rows (exactly the defect being fixed) — and on
        # the filter-off path it raised on every sweep and stopped the feed.
        # dict(row) is also the shape downstream expects (the sweep already
        # passes dict(row) into the engine).
        decoded = row if isinstance(row, dict) else dict(row)
        try:
            decoded["messages"] = decode_jsonb_messages(decoded.get("messages"))
            is_title = title_requests.is_title_messages(decoded["messages"])
        except Exception:
            is_title = False  # unclassifiable -> keep
        if not is_title:
            kept.append(decoded)
        elif dropped_out is not None:
            dropped_out.append(decoded)
    return kept[: max(1, limit)]


def _memory_server_name() -> str:
    from ..config import settings

    return settings.enroll_memory_server


async def sweep() -> int:
    """One housekeeping pass: distill due conversations. Returns the count
    of distills ATTEMPTED (not succeeded - failures are logged and the
    marker stays absent, so the next sweep retries).

    Writes are counted separately (audit D-4): the engine returns True only
    when a memory actually landed, and only THAT stamps writes_total{ok} and
    the last-write timestamp. The sweep used to stamp both on passes that
    wrote NOTHING (gate skips, noise skips, unchanged-content skips), so the
    "time since the feed last wrote" alert stayed quiet while the feed was
    dead."""
    import time

    from ..api.routes_distill import distill_due_canonical, distill_due_conversation
    from ..config import settings

    rows = await due_conversations()
    # Title rows dropped by the due query are reported back so the sweep can
    # stamp them with the bounded gate-skip backoff: they are never marked
    # distilled (nothing was written), but an unmarked row is re-selected
    # OLDEST-FIRST every pass, so once the detector actually fires (audit F1)
    # a pile of legacy title rows would pin the window and starve newer rows.
    dropped_titles: list[dict[str, Any]] = []
    canonical_rows = await due_canonical_conversations(dropped_out=dropped_titles)
    # Publish the due depth BEFORE claiming (2026-10-14): this is the series
    # that makes a stalled feed alertable. A sweep that RAISES leaves the
    # previous value standing rather than zeroing it, so the gauge cannot
    # silently "look healthy" while the query itself is broken.
    metrics.observe_distill_due("conversations", len(rows))
    metrics.observe_distill_due("canonical", len(canonical_rows))
    # Cross-replica claim (2026-10-11 canonical, 2026-10-14 conversations):
    # N pods sweep concurrently under the HPA (min replicas 2); lease the rows
    # THIS pod won so a conversation distills at most once cluster-wide per
    # lease window. The canonical path was fixed first and the conversations
    # path was missed -- the surviving duplicate-summary source.
    rows = await _claim_conversations(rows)
    canonical_rows = await _claim_canonical_rows(canonical_rows)
    for row in dropped_titles:
        try:
            await mark_canonical_gate_skip(row["key_id"], str(row["thread_id"]))
        except Exception:
            log.warning("title-row backoff write skipped (thread=%s)", str(row.get("thread_id", ""))[:16], exc_info=True)
    attempted = 0
    written = 0
    started = time.monotonic()
    # Per-sweep wall-clock budget: the housekeeping loop shares this task
    # with the retention prunes, and one conversation's map-reduce can be
    # 25 sequential LLM calls (cross-review finding: an unbounded sweep can
    # block the loop for hours). When the budget is out, remaining due rows
    # simply wait for the next pass — they stay due (no marker on
    # failure/skip), so nothing is lost.
    #
    # <= 0 MEANS UNBOUNDED (audit F2). It used to mean "break before the first
    # row": the loop compared elapsed (>= 0) > 0 on the very first iteration,
    # so an operator setting 0 for "no limit" silently disabled the entire
    # memory feed while every dashboard stayed green. A negative or zero
    # budget is now read as the explicit no-limit choice it looks like.
    #
    # The budget is ALSO handed to the engine as a monotonic deadline, which
    # it re-checks before every model call (audit D-6): a row with 24-30 map
    # calls x 120 s used to hold this shared loop for ~48 minutes because the
    # check only ran BETWEEN rows.
    #
    # FORWARD-PROGRESS RULE (audit D-6 follow-up): a per-call check against
    # the SHARED budget alone livelocks the biggest conversations — a row
    # whose map stage needs more than one sweep budget is aborted every pass,
    # writes no marker, backs off nothing, and is re-selected OLDEST-FIRST
    # forever. So:
    #   * row #1 of a pass is NEVER cut by the sweep budget (unbounded
    #     deadline): the pass exists to make progress, and row #1 is the row
    #     that has waited longest;
    #   * rows #2+ get row_budget_seconds() = max(sweep_budget, ROW_BUDGET_S)
    #     measured from their OWN start, so a large row still gets a generous
    #     slice before the shared loop is released;
    #   * the shared budget keeps deciding whether another row is STARTED.
    # Only rows #2+ can raise DistillBudgetExceeded, which is exactly the case
    # where the shared loop genuinely must be released.
    budget = settings.distiller_sweep_budget_s
    unbounded = budget <= 0
    row_allowance = row_budget_seconds()  # 0.0 = unbounded

    def _out_of_budget() -> bool:
        return (not unbounded) and time.monotonic() - started > budget

    def _row_deadline(row_index: int) -> float | None:
        """None = this row may run to completion (row #1, or no budget)."""
        if row_index == 0 or row_allowance <= 0:
            return None
        return time.monotonic() + row_allowance

    for row in rows:
        if _out_of_budget():
            log.info(
                "distill sweep: budget %ss exhausted after %d of %d due — deferring the rest",
                budget,
                attempted,
                len(rows),
            )
            break
        attempted += 1
        try:
            # row #1 is exempt from the sweep budget (see the forward-progress
            # rule above); attempted == 1 means this IS the first row.
            wrote = await distill_due_conversation(dict(row), deadline=_row_deadline(attempted - 1))
        except Exception:
            metrics.observe_distill_write("conversations", "failed")
            log.warning(
                "scheduled distill failed (conv=%s) - retried next sweep",
                row.get("conversation_id"),
                exc_info=True,
            )
        else:
            # D-4: only a REAL write counts as ok. The engine returns True only
            # when a memory landed; a gate skip, a noise skip or a
            # suppressed-duplicate mark is "skipped" and must never advance the
            # last-write gauge (that is the gauge the feed alert watches).
            written += 1 if wrote else 0
            metrics.observe_distill_write("conversations", "ok" if wrote else "skipped")
    for row in canonical_rows:
        if _out_of_budget():
            log.info(
                "distill sweep: budget %ss exhausted before canonical rows — deferring the rest",
                budget,
            )
            break
        attempted += 1
        try:
            wrote = await distill_due_canonical(dict(row), deadline=_row_deadline(attempted - 1))
        except Exception:
            metrics.observe_distill_write("canonical", "failed")
            log.warning(
                "canonical distill failed (thread=%s) - retried next sweep",
                row.get("thread_id", "")[:16],
                exc_info=True,
            )
        else:
            written += 1 if wrote else 0
            metrics.observe_distill_write("canonical", "ok" if wrote else "skipped")
    if rows or canonical_rows:
        log.info(
            "distill sweep: %d conversation rows + %d canonical rows due, %d attempted, %d written",
            len(rows),
            len(canonical_rows),
            attempted,
            written,
        )
    return attempted


# ---------------------------------------------------------------------------
# canonical-store sweep (relay-captured conversations: DSH + every harness
# that relays through the gateway, zero client changes)
# ---------------------------------------------------------------------------


async def mark_canonical_distilled(
    key_id: uuid.UUID, thread_id: str, row_updated_at: Any = None, content_hash: str = ""
) -> None:
    """Record that the canonical row (key_id, thread_id) was distilled, with
    the ROW'S updated_at AS THE STATE SNAPSHOT (DeepSeek-V4.1 P1): the due
    query compares distilled_row_state < c.updated_at, so a same-tick write
    can never mark an updated row as distilled. row_updated_at None (tests)
    falls back to now() for both columns."""
    try:
        if row_updated_at is not None:
            await db.execute(
                "INSERT INTO canonical_distills (key_id, thread_id, distilled_at, distilled_row_state, distilled_content_hash) "
                "VALUES ($1, $2, now(), $3, $4) "
                "ON CONFLICT (key_id, thread_id) DO UPDATE SET "
                "distilled_at = now(), distilled_row_state = $3, gate_failed_at = NULL, distilled_content_hash = $4",
                key_id,
                thread_id,
                row_updated_at,
                content_hash,
            )
        else:
            await db.execute(
                "INSERT INTO canonical_distills (key_id, thread_id, distilled_at, distilled_row_state, distilled_content_hash) "
                "VALUES ($1, $2, now(), now(), '') "
                "ON CONFLICT (key_id, thread_id) DO UPDATE SET "
                "distilled_at = now(), distilled_row_state = now(), gate_failed_at = NULL",
                key_id,
                thread_id,
            )
    except Exception:
        # WARNING, not DEBUG (2026-10-09): this swallow hid a three-day global
        # distiller stall -- the marker never landed, so rows stayed due
        # forever, and at LOG_LEVEL=INFO the failure left no trace at all.
        log.warning("canonical distill marker write skipped", exc_info=True)


async def mark_canonical_gate_skip(key_id: uuid.UUID, thread_id: str) -> None:
    """Record an attachment-gate skip so the row leaves the due window for
    GATE_BACKOFF_S (best-effort). Deliberately NOT a distilled marker: no
    write landed. The INSERT branch stamps distilled_row_state with the EPOCH
    sentinel: the column is NOT NULL DEFAULT now() (schema.py), and a default
    now() would compare GREATER than the row's updated_at in the due predicate
    (which requires updated_at < now() - debounce), silently retiring the row
    forever (audit P0-1: permanent backlog discard). The sentinel keeps the
    row due once the backoff lapses; the DO UPDATE branch deliberately does
    NOT touch the column, so a REAL distill marker survives a later gate
    skip. Raises nothing; a failed backoff write must never fail the sweep."""
    try:
        await db.execute(
            "INSERT INTO canonical_distills (key_id, thread_id, distilled_row_state, gate_failed_at) "
            "VALUES ($1, $2, 'epoch'::timestamptz, now()) "
            "ON CONFLICT (key_id, thread_id) DO UPDATE SET gate_failed_at = now()",
            key_id,
            thread_id,
        )
    except Exception:
        log.warning("canonical gate-skip marker write skipped", exc_info=True)


async def drop_marker(conversation_id: uuid.UUID) -> None:
    """The conversation is gone/purged: remove any marker row so the due
    sweep stops selecting it. The conversation_distills FK REFERENCES
    conversations(id) ON DELETE CASCADE — a marker for a deleted row is
    already gone; this handles the marker-rows-that-outlive-their-row edge
    and gives the "gone" path a real no-op-it action (audit P1-1: the old
    mark_distilled(0) was FK-rejected and swallowed, starving the queue
    head). Best-effort; raises nothing.
    """
    try:
        await db.execute("DELETE FROM conversation_distills WHERE conversation_id = $1", conversation_id)
    except Exception:
        log.warning("conversation distill marker drop skipped", exc_info=True)


async def drop_canonical_marker(key_id: uuid.UUID, thread_id: str) -> None:
    """The conversation continued (a newer relay turn reconciled): the marker
    must go, or the sweep would never re-distill it. The reconcile path does
    NOT call this (it only saves rows); instead the due query treats a marker
    older than the row's updated_at as stale — so this helper exists only for
    explicit reset flows and tests."""
    try:
        await db.execute(
            "DELETE FROM canonical_distills WHERE key_id = $1 AND thread_id = $2",
            key_id,
            thread_id,
        )
    except Exception:
        # WARNING, not DEBUG (same 2026-10-14 audit as mark_stale): an
        # invisible failure here strands the thread the same way.
        log.warning("canonical distill marker delete skipped", exc_info=True)


async def _claim_canonical_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Atomically lease the selected rows for THIS pod (cross-replica sweep
    fix, 2026-10-11): the SELECT above is racy across N pods - both replicas
    due-queried the same rows and distilled them twice (live: every thread
    carried TWO near-identical summaries 3s apart). The claim is a short
    lease stamped with this pod's identity; a second sweeper skips live
    leases, and a crashed holder's lease lapses (self-healing). Rows that
    lose the race are dropped from the returned list - they stay due and the
    winner's marker (or lease lapse) governs the retry."""
    import os

    if not rows:
        return rows
    pod = os.environ.get("HOSTNAME", "pod-unknown")
    key_ids = [r["key_id"] for r in rows]
    thread_ids = [r["thread_id"] for r in rows]
    try:
        claimed = await db.fetch(
            """
            INSERT INTO canonical_distills (key_id, thread_id, sweep_lease, lease_until, distilled_row_state)
            SELECT u.key_id, u.thread_id, $3, now() + make_interval(secs => 300), '-infinity'::timestamptz
            FROM unnest($1::uuid[], $2::text[]) AS u(key_id, thread_id)
            JOIN canonical_conversations c ON c.key_id = u.key_id AND c.thread_id = u.thread_id
            ON CONFLICT (key_id, thread_id) DO UPDATE
              SET sweep_lease = $3, lease_until = now() + make_interval(secs => 300)
              WHERE (canonical_distills.sweep_lease IS NULL OR canonical_distills.lease_until IS NULL OR canonical_distills.lease_until < now())
            RETURNING key_id, thread_id
            """,
            key_ids,
            thread_ids,
            pod,
        )
        claimed_set = {(r["key_id"], r["thread_id"]) for r in claimed}
    except Exception:
        # WARNING, not DEBUG (2026-10-14): this swallow hid a broken claim
        # statement -- the deployed build carried `-infinity()::timestamptz`,
        # which is not valid Postgres (`infinity` is a literal, not a
        # function; and the :: binds tighter than the unary minus), so EVERY
        # claim raised and the fail-open path silently disabled the
        # cross-replica lease. The feed kept working but the duplication fix
        # did not: two pods distilled the same thread ~0.5s apart (live
        # duplicate pair, session ccdaeaaea9ede679).
        log.warning("canonical sweep claim skipped (rows stay due, lease inactive)", exc_info=True)
        return rows
    won = [r for r in rows if (r["key_id"], r["thread_id"]) in claimed_set]
    lost = len(rows) - len(won)
    if lost:
        log.info("distill sweep: %d row(s) claimed by another replica - skipped this pass", lost)
    return won


async def _claim_conversations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Atomically lease the selected CONVERSATION rows for THIS pod.

    The canonical path got this lease on 2026-10-11; the conversations path
    did NOT, which is why duplicate summaries kept appearing for sessions
    distilled through it. The gateway runs min-replicas=2 (HPA), so BOTH pods
    sweep every 60s, both due-queried the same conversation, and both distilled
    it -- producing two differently-worded summaries of one session seconds
    apart (text-similarity dedupe cannot catch that: they are independent LLM
    generations).

    Same contract as _claim_canonical_rows: a short lease stamped with this
    pod's identity; a second sweeper skips live leases; a crashed holder's
    lease lapses and self-heals. distilled_seq is seeded with 0 (NOT a
    distilled marker -- the due predicate is "message count > distilled_seq",
    so the row stays due and a recovered gate/lease still drains the backlog).
    Rows that lose the race are dropped from the returned list.
    """
    import os

    if not rows:
        return rows
    pod = os.environ.get("HOSTNAME", "pod-unknown")
    conv_ids = [r["conversation_id"] for r in rows]
    key_ids = [r["key_id"] for r in rows]
    try:
        claimed = await db.fetch(
            """
            INSERT INTO conversation_distills
              (conversation_id, key_id, distilled_seq, sweep_lease, lease_until)
            SELECT u.conversation_id, u.key_id, 0, $3, now() + make_interval(secs => 300)
            FROM unnest($1::uuid[], $2::uuid[]) AS u(conversation_id, key_id)
            JOIN conversations c ON c.id = u.conversation_id
            ON CONFLICT (conversation_id) DO UPDATE
              SET sweep_lease = $3, lease_until = now() + make_interval(secs => 300)
              WHERE (conversation_distills.sweep_lease IS NULL
                     OR conversation_distills.lease_until IS NULL
                     OR conversation_distills.lease_until < now())
            RETURNING conversation_id
            """,
            conv_ids,
            key_ids,
            pod,
        )
        claimed_set = {r["conversation_id"] for r in claimed}
    except Exception:
        # WARNING, not DEBUG (same lesson as the canonical claim): a broken
        # claim statement must not silently disable the lease again.
        log.warning("conversation sweep claim skipped (rows stay due, lease inactive)", exc_info=True)
        return rows
    won = [r for r in rows if r["conversation_id"] in claimed_set]
    lost = len(rows) - len(won)
    if lost:
        log.info("distill sweep: %d conversation row(s) claimed by another replica - skipped this pass", lost)
    return won


async def due_canonical_conversations(
    limit: int = 20, dropped_out: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Canonical rows due for distill: quiet (updated_at past the debounce),
    memory-attached, and never-distilled OR updated since the last distill
    (a continued conversation re-distills - the DSH lifecycle). Oldest first.

    "Memory-attached" is enforced TWICE: the SQL join proves the key exists,
    is not revoked/expired, and carries a creds row for the memory server;
    the engine probe still answers whether the key can actually write.
    Without the join, unattached/revoked rows are selected OLDEST-FIRST every
    pass and silently dropped by the engine gate WITHOUT a marker - the batch
    (LIMIT 20) is then permanently occupied and newer eligible rows STARVE
    (2026-10-09 production finding: 20 stuck rows blocked every distill).

    The SQL join is necessary but NOT sufficient: a live-but-unattached key
    (or a broken gate -- an empty registry snapshot makes resolve_memory_server
    return None for EVERY key) still passes it, and the engine gate then skips
    the row without a marker. Such rows are suppressed for GATE_BACKOFF_S via
    gate_failed_at so they cannot pin the window (2026-10-09 second stall:
    the same 20 rows were retried for days because the skip wrote no state)."""
    from ..config import settings
    from ..core import distiller_config

    debounce = await distiller_config.debounce_minutes()
    # OVER-FETCH WINDOW (2026-10-17): title-row exclusion is applied in PYTHON
    # below, not in SQL. A JSONB LIKE over a heterogeneous message payload is
    # exactly the kind of fragile predicate that silently stops matching after
    # a shape change (the file's own history: a broken due query raised on
    # every pass and starved the whole feed). Fetching a wider window and
    # filtering down keeps the SQL boring, and the over-fetch is what prevents
    # the starvation bug documented in this module's sibling docstring — if we
    # fetched exactly LIMIT rows and discarded the title ones, the batch could
    # be consumed by rows that are then thrown away, and newer eligible rows
    # would never be reached.
    fetch_limit = max(1, limit)
    if settings.canonical_skip_title_requests:
        fetch_limit = min(fetch_limit * 4, 400)
    try:
        rows = await db.fetch(
            """
            SELECT c.key_id, c.thread_id, c.messages, c.updated_at, d.distilled_content_hash,
                   d.gate_failed_at
            FROM canonical_conversations c
            JOIN api_keys k ON k.id = c.key_id
              AND k.revoked_at IS NULL
              AND (k.expires_at IS NULL OR k.expires_at > now())
            JOIN mcp_server_creds cred
              ON cred.key_id = c.key_id AND cred.server_name = $3
            LEFT JOIN canonical_distills d
              ON d.key_id = c.key_id AND d.thread_id = c.thread_id
            WHERE c.updated_at < now() - make_interval(mins => $1)
              AND (d.thread_id IS NULL OR d.distilled_row_state < c.updated_at)
              AND (d.gate_failed_at IS NULL
                   OR d.gate_failed_at < now() - make_interval(secs => $4))
              AND (d.thread_id IS NULL OR d.lease_until IS NULL OR d.lease_until < now())
            ORDER BY c.updated_at ASC
            LIMIT $2
            """,
            debounce,
            fetch_limit,  # $2 = LIMIT — over-fetched, then trimmed by _exclude_title_rows
            _memory_server_name(),  # $3 = cred.server_name
            gate_backoff_seconds(),  # $4 = gate-skip suppression window
        )
    except Exception:
        # WARNING, not DEBUG (2026-10-14): the canonical sweep is the ONLY path
        # the DSH/relay memory feed depends on. Swallowing its failures at
        # DEBUG hid a total feed outage for hours (see the sibling above).
        log.warning("canonical distill due sweep skipped", exc_info=True)
        return []

    if settings.canonical_skip_title_requests:
        return _exclude_title_rows(rows, limit, dropped_out)
    # Still decode at the boundary when the filter is off: the ENGINE reads
    # row["messages"] too, and a JSONB string there is the same production
    # shape the filter was blind to.
    # Copy, never mutate (see _exclude_title_rows): a Record rejects item
    # assignment, and raising here would stop the whole canonical feed.
    return [
        {
            **(row if isinstance(row, dict) else dict(row)),
            "messages": decode_jsonb_messages(row.get("messages")),
        }
        for row in rows
    ]
