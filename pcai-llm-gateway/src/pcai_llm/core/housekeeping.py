"""Periodic housekeeping (audit finding 16): ONE bounded loop for the
prunes that previously only ran when the registry refreshed.

WHY: `DELETE FROM rate_windows ...` piggybacked registry.refresh() — which
fires on BOOT and on ADMIN MCP/MODEL MUTATIONS ONLY. A busy data plane with
an untouched admin API grew one row per key per minute for the process
lifetime (~1.4M rows/day at 1000 keys), degrading autovacuum and the
/v1/usage read. The retention prunes (agent_traces / conversations) had the
same dependency. effective_prompts prunes piggybacked its own capture write
(fine — it writes on every model call), so it stays where it is.

CONTRACT (unchanged): the usage LEDGER is never pruned (it is the audit
AND the budget engine — deleting rows would silently raise every key's
headroom). Retention deletes ONLY what the operator opted into
(retention_*_days > 0); rate_windows is pure ephemeral counter state
(2-minute windows) and is always pruned. Every statement is error-
swallowed — housekeeping must never fail anything.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import settings
from ..db import db
from ..routing.router import router as model_router
from . import context_window, embedcache, response_cache, toolcompress_store, trace_payloads, upstream_auth

log = logging.getLogger(__name__)

CADENCE_S = 60.0

# Retention defaults for the two tables added in waves 5-6 (2026-11-23):
# tool_approval_requests (the approval gate's observations) and judge_verdicts
# (the agent judge's quality record). Neither knob exists in config.py YET —
# that file belongs to the Lead — so both steps read their knob with
# getattr(settings, <name>, <default>): today the default below is what runs,
# and the day the setting lands the operator's value overrides it with no
# second edit here (the same "knob not deployed yet" shape core/approval.py
# uses for tool_approval_mode). Unlike the retention_*_days knobs, which ship 0
# = off, these two default ON: both tables are unbounded-growth tables and the
# point of this wave is that they stop growing. An explicit 0 still disables
# either prune (0 = keep forever, the operator contract shared with every
# other retention knob).
DEFAULT_TOOL_APPROVAL_RETENTION_DAYS = 30
# Verdicts are the QUALITY RECORD — the only evidence of what the judge said
# about a turn — so they are kept far longer than the trace payloads (7d).
DEFAULT_JUDGE_VERDICT_RETENTION_DAYS = 90

# The canonical-store prune is throttled: the loop ticks every CADENCE_S, but a
# delete over the biggest table does not need to run every minute (and the
# existing prunes are cheap by comparison). Mirrors the "prunes piggyback a
# rarer hook" discipline the old registry-refresh wiring used.
_CANONICAL_PRUNE_INTERVAL_S = 3600.0
# NONE = "never pruned in this process" (audit C8, 2026-10-29). The old 0.0
# sentinel was compared against time.monotonic(), which is HOST UPTIME — on a
# node that has been up for less than an hour the throttle therefore swallowed
# the first prune for up to an hour after every pod start (core/costs.py:22-27
# documents and fixes the same defect class with float("-inf")). A sentinel
# that cannot be confused with a monotonic reading is the honest fix.
_last_canonical_prune: float | None = None

_task: asyncio.Task | None = None


async def _sweep_once() -> None:
    # 1) rate_windows: pure per-minute counter state, older than 2 windows
    #    is dead (the upsert hot path never deletes).
    try:
        await db.execute("DELETE FROM rate_windows WHERE window_start < now() - interval '2 minutes'")
    except Exception:
        log.debug("housekeeping: rate_windows prune skipped", exc_info=True)

    # 1b) exact-match response cache: expired answers are dead weight (the
    #     lookup already ignores them, so this is pure space reclamation).
    #     Not an operator retention knob — the TTL is the policy.
    await response_cache.purge_expired()

    # 1c) embedding cache (audit P2-28, 2026-11-10): the one cache table
    #     with no TTL and no prune — unbounded growth on RAG-heavy fleets.
    #     Vectors never go stale, so this is pure storage reclamation and
    #     ships OFF (0 = keep forever, the retention-knob convention).
    try:
        if settings.embedding_cache_ttl_s > 0:
            reclaimed = await embedcache.prune(settings.embedding_cache_ttl_s)
            if reclaimed:
                log.info("housekeeping: pruned %d embedding cache rows", reclaimed)
    except Exception:
        log.debug("housekeeping: embedding_cache prune skipped", exc_info=True)

    # 1d) compressed tool-result originals (X-PCAI-Compress-Tools): pure
    #     retrieval data, TTL-bound. A pruned row only means a later
    #     pcai__retrieve answers "expired" — the turn that compressed it
    #     already used the text. Error-swallowed like every other sweep.
    try:
        if settings.tool_compress_store_ttl_s > 0:
            reclaimed = await toolcompress_store.prune(settings.tool_compress_store_ttl_s)
            if reclaimed:
                log.info("housekeeping: pruned %d compressed tool results", reclaimed)
    except Exception:
        log.debug("housekeeping: tool_compress_store prune skipped", exc_info=True)

    # 1e) agent trace payloads (2026-11-23): tool arguments and results, the
    #     highest-sensitivity rows the gateway stores — everything the fleet
    #     actually touched. Retention is operator-opted like the rest
    #     (trace_payload_retention_days; 0 = keep until manually pruned), which
    #     is why the shipped default is a 7-day window instead of forever.
    #     trace_payloads.prune is itself a no-op returning 0 at days <= 0
    #     (defence in depth: this guard is the first of two), and
    #     error-swallowed like every other sweep — a prune fault only delays
    #     reclamation.
    try:
        if settings.trace_payload_retention_days > 0:
            reclaimed = await trace_payloads.prune(settings.trace_payload_retention_days)
            if reclaimed:
                log.info("housekeeping: pruned %d trace payload rows", reclaimed)
    except Exception:
        log.debug("housekeeping: trace_payloads prune skipped", exc_info=True)

    # 1f) tool approval observations (wave 7, 2026-11-23): warn mode's decision
    #     input — ONE row per distinct (key, server, tool, args_fingerprint)
    #     that WOULD have needed approval, upserted so a repeat increments
    #     times_seen instead of adding a row. In warn mode every destructive
    #     call shape the fleet ever makes leaves a row behind forever, so it
    #     needs a window like the rest. Keyed on last_seen (the upsert
    #     refreshes it), NOT first_seen: a shape still being observed never
    #     ages out, only one the fleet has STOPPED calling — and that row is
    #     re-created by the next call, so the operator loses a stale decision
    #     prompt, never the live one. The token table (tool_approvals) is never
    #     pruned here: a grant's lifetime is the operator's expiry, and
    #     expiring a live token by age would silently discard a human decision.
    #     The knob is not in config.py yet, hence the getattr default; 0 still
    #     means keep forever (the prune re-checks that itself).
    try:
        days = int(getattr(settings, "tool_approval_retention_days", DEFAULT_TOOL_APPROVAL_RETENTION_DAYS))
        if days > 0:
            reclaimed = await prune_tool_approval_requests(days)
            if reclaimed:
                log.info("housekeeping: pruned %d tool approval request rows", reclaimed)
    except Exception:
        log.debug("housekeeping: tool_approval_requests prune skipped", exc_info=True)

    # 1g) judge verdicts (wave 7, 2026-11-23): one row per verdict per judged
    #     turn (core/judge.py, B1), and the quality trend the release gate
    #     reads. The window lands BEFORE the volume does — the judge sweep is
    #     what starts filling the table, and a retention knob added after the
    #     growth has already happened is the defect this wave exists to avoid.
    #     90 days by default: a verdict is the evidence behind a number the
    #     gate reads, so a trend window shorter than a release cycle could not
    #     show a regression. Same getattr contract as 1f.
    try:
        days = int(getattr(settings, "judge_verdict_retention_days", DEFAULT_JUDGE_VERDICT_RETENTION_DAYS))
        if days > 0:
            reclaimed = await prune_judge_verdicts(days)
            if reclaimed:
                log.info("housekeeping: pruned %d judge verdict rows", reclaimed)
    except Exception:
        log.debug("housekeeping: judge_verdicts prune skipped", exc_info=True)

    # 2) operator-opted retention (0 = disabled — nothing is deleted until
    #    the policy exists, the roadmap contract).
    for table, days, cutoff_col in (
        ("agent_traces", settings.retention_trace_days, "ts"),
        ("conversations", settings.retention_conversation_days, "updated_at"),
        # model_stickiness (load-aware cascade): pure routing state, pruned
        # by the last touch — a conversation's head is only worth keeping
        # while the conversation is alive (default 14d; 0 = keep forever).
        ("model_stickiness", settings.retention_stickiness_days, "updated_at"),
    ):
        if days <= 0:
            continue
        try:
            tag = await db.execute(
                f"DELETE FROM {table} WHERE {cutoff_col} < now() - make_interval(days => $1)",
                days,
            )
            if tag and not tag.endswith(" 0"):
                log.info("housekeeping: pruned %s from %s (>%dd)", tag.split()[-1], table, days)
        except Exception:
            log.debug("housekeeping: retention prune skipped (%s)", table, exc_info=True)

    # 3) canonical-store retention — DELIBERATELY SEPARATE from the loop above,
    #    because it is the only prune that can lose input data.
    #
    #    canonical_conversations is the DISTILLER'S INPUT: it holds full message
    #    history as JSONB and nothing else ever prunes it, so on a busy gateway
    #    it is the biggest table and grows without bound. The rule here is
    #    therefore not "delete anything old" but "delete only what has already
    #    become a memory": a row is deletable ONLY when a canonical_distills
    #    marker proves a successful write landed for it. Rows still awaiting
    #    distill are never touched, so enabling this cannot lose an
    #    un-distilled conversation — the worst case is that storage is not
    #    reclaimed yet.
    #
    #    Age is measured from the MARKER's distilled_at, not the row's
    #    updated_at: the marker is the proof-of-write, and keying off it means
    #    a conversation that keeps growing is only eligible once its latest
    #    distill is itself old.
    #
    #    Deletion cascades to canonical_distills (FK ON DELETE CASCADE), so
    #    markers do not accumulate as orphans.
    await _prune_canonical_store()


async def _prune_canonical_store() -> None:
    """Delete distilled canonical rows older than retention_canonical_days.

    Throttled to _CANONICAL_PRUNE_INTERVAL_S and error-swallowed like every
    other housekeeping step: a prune fault must never fail the sweep."""
    global _last_canonical_prune

    days = settings.retention_canonical_days
    if days <= 0:
        return
    now = time.monotonic()
    if _last_canonical_prune is not None and now - _last_canonical_prune < _CANONICAL_PRUNE_INTERVAL_S:
        return
    _last_canonical_prune = now
    try:
        tag = await db.execute(
            """
            DELETE FROM canonical_conversations c
            WHERE c.updated_at < now() - make_interval(days => $1)
              AND EXISTS (
                    SELECT 1 FROM canonical_distills d
                    WHERE d.key_id = c.key_id
                      AND d.thread_id = c.thread_id
                      -- THE PROOF MUST BE ABOUT THIS CONTENT (audit F3,
                      -- 2026-10-29): canonical_distills has a PRIMARY KEY and
                      -- NO foreign key to canonical_conversations, so a marker
                      -- can outlive the row it described and be re-created for
                      -- a NEW conversation that reuses the (key_id, thread_id)
                      -- pair — e.g. the marker left behind by an earlier
                      -- capture, or one written before the row was replaced.
                      -- Such a marker proves nothing about the CURRENT text,
                      -- and matching it deleted never-distilled transcripts,
                      -- defeating the "cannot lose an un-distilled
                      -- conversation" contract this prune is built on. The
                      -- state snapshot must be the row's CURRENT updated_at
                      -- (the exact value mark_canonical_distilled stores, and
                      -- the complement of the due query's
                      -- "distilled_row_state < c.updated_at"). A marker whose
                      -- snapshot was stamped with now() by an older build never
                      -- matches, so that row is simply not reclaimed — the
                      -- conservative direction for the one prune that can lose
                      -- input data.
                      AND d.distilled_row_state = c.updated_at
                      AND d.distilled_at < now() - make_interval(days => $1)
                      -- a row currently suppressed by a gate skip was never
                      -- re-distilled after its last change: pruning it here
                      -- would discard content that still owes a memory
                      -- (audit P0-2)
                      AND d.gate_failed_at IS NULL
              )
            """,
            days,
        )
        if tag and not tag.endswith(" 0"):
            log.info(
                "housekeeping: pruned %s distilled canonical row(s) (>%dd, distilled-only)",
                tag.split()[-1],
                days,
            )
    except Exception:
        log.warning("housekeeping: canonical-store prune skipped", exc_info=True)


def _reclaimed_rows(tag: str) -> int:
    """Rows removed, read from an asyncpg command tag ("DELETE 5").

    execute() hands back the TAG and nothing else, so a driver (or a test
    fake) that returns something unexpected must read as "nothing reclaimed"
    rather than crash the sweep — a wrong count only costs a log line.
    """
    try:
        return int(str(tag).split()[-1])
    except (ValueError, IndexError):
        return 0


async def prune_tool_approval_requests(days: int) -> int:
    """Delete approval observations not SEEN for N days; returns rows reclaimed.

    tool_approval_requests is the operator's decision input (core/approval.py
    owns the write), one row per distinct (key, server, tool,
    args_fingerprint) shape that would have needed approval. Warn mode is
    observe-only, so on a fleet that never switches to enforce this table is
    the only record of the destructive calls the gate saw — and it grows for
    every new shape, forever. This window is what bounds it.

    Age is measured from last_seen, not first_seen: the upsert refreshes
    last_seen on every repeat, so a shape still being observed is NEVER
    eligible no matter how old it is, and only a shape the fleet has stopped
    calling ages out. That is the same "age from the last touch" rule
    model_stickiness uses (step 2), and it is why enabling this cannot delete
    the decision input for a live call pattern.

    NOT the token table: tool_approvals rows live and die by the operator's
    own expires_at/revoked_at, never by a retention window.

    days <= 0 is a no-op returning 0 — the operator contract shared with every
    other retention knob, re-checked HERE as well as at the call site (defence
    in depth, the trace_payloads.prune shape).

    Raises on a DB fault; the housekeeping step is error-swallowed like every
    other one there. The predicate is a sequential scan (the DDL deliberately
    ships no second index: the table is the number of DISTINCT call shapes,
    tens of rows), which is why it needs no throttle of its own.
    """
    if days <= 0:
        return 0
    tag = await db.execute(
        "DELETE FROM tool_approval_requests WHERE last_seen < now() - make_interval(days => $1)",
        int(days),
    )
    return _reclaimed_rows(tag)


async def prune_judge_verdicts(days: int) -> int:
    """Delete judge verdicts older than N days; returns rows reclaimed.

    judge_verdicts is the QUALITY RECORD (core/judge.py, B1 commit 1): one row
    per verdict per judged turn, stamped with the prompt_version that produced
    it, and the source of the calibration trend the release gate reads. It
    starts growing the moment the judge sweep runs, so the window ships BEFORE
    the volume — a retention knob added after the growth has already happened
    is exactly the defect this wave exists to avoid.

    Age is judged_at, the row's own event time (the column the trend sorts by),
    so the window means "verdicts about turns older than N days" regardless of
    when the insert landed.

    days <= 0 is a no-op returning 0, re-checked here as at the call site
    (defence in depth). Raises on a DB fault; the housekeeping step is
    error-swallowed like every other one there. The predicate is a sequential
    scan — neither shipped index leads with judged_at alone — which is
    acceptable for a table that starts empty and a sweep that runs once a
    minute.
    """
    if days <= 0:
        return 0
    tag = await db.execute(
        "DELETE FROM judge_verdicts WHERE judged_at < now() - make_interval(days => $1)",
        int(days),
    )
    return _reclaimed_rows(tag)


async def _distill_sweep() -> None:
    """The distiller's scheduled pass (best-effort, error-swallowed like
    every housekeeping step): due quiet conversations of memory-attached
    keys, distilled when distiller runtime config says so. offpeak_only
    gates WHEN: with it on, the sweep runs only while the offpeak
    evaluator reports low demand (GPU-sparing default); explicit
    /v1/distill calls ignore this gate (a human asked)."""
    try:
        from . import distill_schedule, distiller_config, offpeak

        if not await distiller_config.is_enabled():
            return
        # Off-peak-only gate. Prefer the DISTILLER MODEL's own evaluator when
        # one is configured: the sweep spends GPU on that model, so the fleet
        # average is the wrong signal (live 2026-11-05: Qwen3.8-27B sat at
        # ~1.6% — off-peak — while the fleet average was ~24%, and the sweep
        # stayed paused for hours). None = no model pinned (caller routing)
        # or no per-model evaluator: fall back to the global hysteresis. A
        # per-model False is a BUSY signal and must never be overridden by an
        # active global state.
        if await distiller_config.offpeak_only():
            model = await distiller_config.get_model()
            per_model = offpeak.model_active(model) if model else None
            if per_model is None:
                if not (offpeak.state.enabled and offpeak.state.active):
                    return
            elif not per_model:
                return
        await distill_schedule.sweep()
    except Exception:
        # WARNING, not DEBUG (2026-10-14): the outermost distiller catch. At
        # DEBUG a fault here is invisible at LOG_LEVEL=INFO, which is exactly
        # how the v2.5.0 feed outage stayed silent for hours (a schema-drift
        # error inside sweep() reached no operator-visible surface at all).
        log.warning("housekeeping: distill sweep skipped", exc_info=True)


async def _context_window_sweep() -> None:
    """Re-attempt context-window discovery for any upstream still unknown.

    WHY (live defect, 2026-10-29 — found in the 3.4.5 rollout). Discovery runs
    inside registry.refresh(), which fires on BOOT and on a committed admin
    mutation ONLY. A probe that timed out or 401'd during that one moment left
    the POD permanently blind: immediately after the cut, one replica knew all
    three windows and the other knew only GLM's, so 3 of 8 forced-Qwen requests
    still returned "(model error 400)" while the sibling replica served the
    same requests fine. A window is a fact the upstream publishes continuously,
    so a pod that missed it has to keep asking.

    Cost in the steady state is near-ZERO I/O: every upstream with a known AND
    FRESH window is filtered out here (a stale one is re-probed, audit w6 F1),
    and context_window.probe additionally enforces PROBE_RETRY_SECONDS so a
    permanently broken upstream is retried once a minute at most.
    Error-swallowed like every other housekeeping step.
    """
    entries: list[tuple[str, dict[str, str]]] = []
    for name in model_router.models():
        pool = model_router.pool(name)
        if pool is None:
            continue
        for ep in pool.endpoints:
            # Unknown OR stale (audit w6 F1): a learned window older than
            # WINDOW_TTL_S must be re-probed, or a shrunken upstream window is
            # never re-read and the stale one over-sizes every request.
            if context_window.cached(ep.base_url) is None or context_window.stale(ep.base_url):
                entries.append((ep.base_url, upstream_auth.upstream_headers(ep)))
    if not entries:
        return
    await context_window.refresh_windows(entries)


async def _loop() -> None:
    while True:
        try:
            await _sweep_once()
            await _distill_sweep()
            try:
                await _context_window_sweep()
            except Exception:
                log.warning("housekeeping: context-window sweep skipped", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — defensive, the loop never dies
            log.exception("housekeeping sweep failed unexpectedly")
        await asyncio.sleep(CADENCE_S)


async def start() -> None:
    """Start the sweep loop (idempotent; called from the app lifespan)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="housekeeping")
    log.info("housekeeping loop started (cadence %.0fs)", CADENCE_S)


async def stop() -> None:
    """Cancel the loop (idempotent)."""
    global _task
    task = _task
    _task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def reset() -> None:
    """Test seam."""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
