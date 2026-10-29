"""Sticky cascade heads — one durable head model per (key, conversation).

WHY: the `auto` cascade entry picks its head model by live off-peak state
at conversation START, and the choice is STICKY for that conversation
("if it went to GLM, stay with GLM"). This module owns that one durable
fact — a `model_stickiness` row per (key, thread fingerprint); the
resolution rules that read/write it live in core.cascade.resolve_cascade.

Contract (the fail-open discipline of mcpf.federation.load_grants):

* ``get_sticky`` NEVER raises: any DB fault logs and returns None ("no
  sticky head"), so the caller falls back to the profile ordering. A
  stickiness outage must never break admission.
* ``set_sticky`` / ``drop_sticky`` are best-effort: a failed write logs and
  is forgotten — the request it belongs to is already resolved, and the
  next turn simply re-picks.
* Replica-safe by construction: the record is a Postgres row, not process
  memory, so a conversation hopping replicas keeps its head.
* Rows are ROUTING state, never policy: the caller validates the stored
  model against the live registered + key-allowed set on every read and
  drops the row when it no longer qualifies — a stale preference can never
  resurrect a model the key may no longer use.
* Retention: housekeeping prunes rows older than
  settings.retention_stickiness_days (default 14; 0 = keep forever).

NAMESPACING (defect fix, 2026-10-23): the table is shared by TWO independent
features -- the cascade's load-aware HEAD (auto-demand-aware) and the routing
never-downgrade FLOOR. Both key on (key_id, thread_key), and the thread
fingerprint is identical for a given conversation, so without a namespace the
cascade's head silently became routing's floor: a conversation first served by
auto-demand-aware at GLM would have every later ROUTED turn held at GLM
regardless of the request. Rows are therefore namespaced by SCOPE at the
thread-key level (no schema change; thread_key is TEXT). The cascade path keeps
its historical un-prefixed key so its stored rows and behaviour are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any

from ..db import db
from . import metrics

log = logging.getLogger(__name__)

# Strong references to in-flight sticky writes. A bare create_task can be
# garbage-collected mid-flight, and an unbounded burst under load is its own
# outage -- same discipline as cascade._sticky_write_later. Bounded: past the
# cap the oldest task is cancelled, because a dropped floor refresh costs one
# re-decision while unbounded growth costs the process.
_PENDING: set[asyncio.Task] = set()
_PENDING_MAX = 1024

# Row namespaces. The cascade path uses NO prefix (its rows predate this and
# must keep working); routing prefixes so the two features can never read each
# other's rows. See the module docstring's NAMESPACING note.
SCOPE_ROUTING = "routing:"


def scoped_thread_key(thread_key: str | None, scope: str) -> str | None:
    """Namespace one conversation fingerprint to one feature. PURE.

    None in, None out (no seed / non-chat), so callers keep their
    "no fingerprint means no stickiness" contract.
    """
    if not thread_key:
        return None
    return scope + thread_key


async def get_sticky(key_id: uuid.UUID, thread_key: str | None) -> str | None:
    """The conversation's sticky head model, or None.

    None means "no record" OR "the read failed" — deliberately
    indistinguishable to the caller (both resolve to the profile-order
    head), so a stickiness outage can never change the response shape.
    """
    if not thread_key:
        return None
    try:
        row = await db.fetchrow(
            "SELECT model FROM model_stickiness WHERE key_id = $1 AND thread_key = $2",
            key_id,
            thread_key,
        )
        model = row["model"] if row else None
    except Exception:
        log.warning("stickiness: read failed (key %s) — proceeding without a sticky head", key_id, exc_info=True)
        return None
    return str(model) if model else None


async def set_sticky(key_id: uuid.UUID, thread_key: str | None, model: str) -> None:
    """Persist the conversation's head model (best-effort; never raises).

    Upsert keyed on (key_id, thread_key): a re-pick overwrites the row —
    last write wins across replicas, because the head is a preference, not
    a lease.
    """
    if not thread_key or not model:
        return
    try:
        await db.execute(
            "INSERT INTO model_stickiness (key_id, thread_key, model) VALUES ($1, $2, $3) "
            "ON CONFLICT (key_id, thread_key) DO UPDATE SET model = $3, updated_at = now()",
            key_id,
            thread_key,
            model,
        )
    except Exception:
        log.warning("stickiness: write failed (key %s, model %r) — the pick will not stick", key_id, model)


async def drop_sticky(key_id: uuid.UUID, thread_key: str | None) -> None:
    """Forget a conversation's head model (best-effort; never raises).

    Called when the stored model fell out of the live registered /
    key-allowed set: the ACL and the registry are authoritative, so the
    stale preference is removed and the caller re-picks fresh.
    """
    if not thread_key:
        return
    try:
        await db.execute(
            "DELETE FROM model_stickiness WHERE key_id = $1 AND thread_key = $2",
            key_id,
            thread_key,
        )
    except Exception:
        log.warning("stickiness: drop failed (key %s) — stale head left in place", key_id)


async def get_sticky_row(key_id: uuid.UUID, thread_key: str | None) -> tuple[str, float] | None:
    """The conversation sticky head AND its age in seconds, or None.

    Separate from get_sticky (which the cascade path uses and must stay
    byte-identical) so the routing never-downgrade floor can apply an idle
    reset without a second query: age comes from the row updated_at, and the
    floor is refreshed on every served turn, so age means "time since this
    conversation last routed" -- which is exactly the idle signal.

    NEVER raises (the module contract): any fault returns None, i.e. "no
    floor", so a stickiness outage degrades to plain routing.
    """
    if not thread_key:
        return None
    try:
        row = await db.fetchrow(
            "SELECT model, EXTRACT(EPOCH FROM (now() - updated_at)) AS age_s "
            "FROM model_stickiness WHERE key_id = $1 AND thread_key = $2",
            key_id,
            thread_key,
        )
    except Exception:
        log.warning("stickiness: aged read failed (key %s) - no floor", key_id, exc_info=True)
        return None
    if not row:
        return None
    model = row["model"]
    if not model:
        return None
    try:
        age = float(row["age_s"])
    except (TypeError, ValueError, KeyError):
        age = 0.0
    return str(model), age


def make_writer(
    key_id: uuid.UUID, thread_key: str | None, defer: bool = False
) -> Callable[[str], Any] | None:
    """A fire-and-forget writer bound to one conversation, or None.

    Returned to the router as its sticky_write callable. The router is
    I/O-free by contract, so it hands back the model it actually SERVED and
    this schedules the persist without adding latency to the request.

    WHY a bounded task set rather than a bare create_task: a bare task can be
    garbage-collected mid-flight (the classic fire-and-forget footgun), and an
    unbounded burst of them under load is its own outage. This mirrors
    cascade._sticky_write_later, which owns the same discipline for the
    demand-aware path.

    DEFER (audit 2026-10-29 P3, XV-3): the router picks a model BEFORE the
    budget gates and admission failover run, so an eager write here records a
    model that may never serve — and never-downgrade then holds the NEXT turn
    at it. With `defer=True` the writer returns a one-argument finisher
    instead of persisting: the caller invokes finisher(actual_model) once the
    serving model is known, and the row can never name a model that did not
    bill. Default False keeps every existing caller eager (byte-identical).
    """
    if not thread_key:
        return None

    def _write(model: str) -> Any:
        if not model:
            return None
        if defer:
            # Nothing is persisted until the caller knows what served. A
            # finisher that is never invoked writes nothing — which is the
            # correct outcome for a request that never got that far.
            def _finish(actual_model: str) -> None:
                _persist(str(actual_model or model))

            return _finish
        _persist(model)
        return None

    def _persist(model: str) -> None:
        if not model:
            return
        try:
            # Enforce the cap BEFORE adding (cascade._sticky_write_later's
            # idiom): drop the OLDEST task rather than growing without bound
            # under a write stall. set_sticky is idempotent and a lost floor
            # refresh costs one re-decision, never correctness.
            while len(_PENDING) >= _PENDING_MAX:
                oldest = next(iter(_PENDING))
                oldest.cancel()
                _PENDING.discard(oldest)
            task = asyncio.create_task(set_sticky(key_id, thread_key, model))
        except RuntimeError:  # no running loop (tests calling synchronously)
            log.debug("stickiness: no event loop - floor not persisted")
            return
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)

    return _write


def observe_head_pick(picked: str, reason: str, thread_key: str | None = None, ranks: str = "") -> None:
    """One INFO log + one counter tick per cascade head decision.

    ``reason`` is one of metrics.CASCADE_PICK_REASONS — sticky | load |
    default (the counter's own vocabulary constant). ``ranks`` is the
    preformatted per-candidate rank list ("GLM-5.3-Flash=0,Deepseek-V4=1").
    The thread key is logged as an 8-char prefix — it is a routing handle,
    not something an operator needs in full.
    """
    log.info(
        "cascade: head pick model=%s reason=%s thread=%s ranks=%s",
        picked,
        reason,
        (thread_key or "")[:8],
        ranks,
    )
    metrics.cascade_head_pick(picked, reason)
