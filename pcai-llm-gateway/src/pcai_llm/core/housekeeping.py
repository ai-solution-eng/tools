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

log = logging.getLogger(__name__)

CADENCE_S = 60.0

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
        # Off-peak-only gate (single combined condition per SIM102): the
        # global evaluator hysteresis state — fail-safe: a disabled evaluator
        # is never "low demand", so the sweep just waits.
        if await distiller_config.offpeak_only() and not (offpeak.state.enabled and offpeak.state.active):
            return
        await distill_schedule.sweep()
    except Exception:
        # WARNING, not DEBUG (2026-10-14): the outermost distiller catch. At
        # DEBUG a fault here is invisible at LOG_LEVEL=INFO, which is exactly
        # how the v2.5.0 feed outage stayed silent for hours (a schema-drift
        # error inside sweep() reached no operator-visible surface at all).
        log.warning("housekeeping: distill sweep skipped", exc_info=True)


async def _loop() -> None:
    while True:
        try:
            await _sweep_once()
            await _distill_sweep()
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
