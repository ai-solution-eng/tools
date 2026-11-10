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

NAMESPACING (defect fix, 2026-10-23): the table is shared by independent
features -- the cascade's load-aware HEAD (auto-demand-aware) and the routing
never-downgrade FLOOR. Both key on (key_id, thread_key), and the thread
fingerprint is identical for a given conversation, so without a namespace the
cascade's head silently became routing's floor: a conversation first served by
auto-demand-aware at GLM would have every later ROUTED turn held at GLM
regardless of the request. Rows are therefore namespaced by SCOPE at the
thread-key level (no schema change; thread_key is TEXT). The cascade path keeps
its historical un-prefixed key so its stored rows and behaviour are unchanged.
A THIRD scope, ep: (session affinity), pins a conversation to a physical
ENDPOINT id — under that scope the model column holds an endpoint id, not a
model name. Same discipline: the raw fingerprint is identical across features,
so the prefix is the ONLY thing keeping the three from reading each other's
rows; every ep: writer/reader goes through the endpoint helpers below, which
apply the scope internally so callers can never mix it up with a model row.
A FOURTH scope, grp: (model groups), pins a conversation to the MEMBER its
group resolved to — the model column holds a MODEL NAME, so it must never
share a row with the cascade head or the routing floor either. Same
discipline: the group helpers below own the prefix.
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
# must keep working); routing and session-affinity prefix so the features can
# never read each other's rows. See the module docstring's NAMESPACING note.
SCOPE_ROUTING = "routing:"
SCOPE_ENDPOINT = "ep:"
SCOPE_GROUP = "grp:"


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


# ---------------------------------------------------------------------------
# Endpoint pins (session affinity) — the ep: scope
# ---------------------------------------------------------------------------
#
# The pin is ROUTING state, never policy: the caller (routes_proxy chat path)
# validates the stored endpoint against the LIVE pool on every read (inside
# Pool.pick) and drops it when the endpoint cooled down or vanished. The
# ep: scope keeps it out of the cascade head's and routing floor's rows
# (NAMESPACING note above).


async def get_endpoint_pin(key_id: uuid.UUID, session: str | None, ttl_s: float) -> str | None:
    """The conversation's pinned ENDPOINT id, or None.

    `session` is the RAW X-PCAI-Session value; the ep: scope is applied HERE
    (callers never see or write the namespaced key, so the three scopes can
    never collide at the call site). TTL rides the row's own updated_at: a
    pin older than ttl_s is expired — dropped, so it cannot outlive its
    lease — and reported as absent. ttl_s <= 0 disables the expiry (the
    housekeeping retention sweep still applies).

    NEVER raises (module contract): any fault reads as "no pin", and the
    pick falls back to normal rotation — a stickiness outage must never
    change the response shape.
    """
    tk = scoped_thread_key(session, SCOPE_ENDPOINT)
    if not tk:
        return None
    row = await get_sticky_row(key_id, tk)
    if row is None:
        return None
    pin, age = row
    if ttl_s > 0 and age > ttl_s:
        await drop_sticky(key_id, tk)
        return None
    return pin


async def set_endpoint_pin(key_id: uuid.UUID, session: str | None, endpoint_id: str) -> None:
    """Persist (or refresh) the conversation's endpoint pin.

    Best-effort, never raises (set_sticky's contract): the call that
    produced the pin already resolved, so a lost write costs one re-decision
    on the next turn. A re-serve refreshes updated_at, which is also the
    TTL clock — an idle conversation's pin expires on its own.
    """
    await set_sticky(key_id, scoped_thread_key(session, SCOPE_ENDPOINT), endpoint_id)


async def drop_endpoint_pin(key_id: uuid.UUID, session: str | None) -> None:
    """Forget the pin (breaker trip / transport error / upstream refusal).

    Best-effort, never raises: the pin just proved stale, and leaving it in
    place would only cost one ignored read before the live-pool validation
    rejects it anyway.
    """
    await drop_sticky(key_id, scoped_thread_key(session, SCOPE_ENDPOINT))


# ---------------------------------------------------------------------------
# Group pins (model groups) — the grp: scope
# ---------------------------------------------------------------------------
#
# A group request resolves to a MEMBER; the conversation stays on that member
# so the next turn does not re-roll the load pick. Re-picking per turn would
# defeat the point of a conversation: the member's KV cache would go cold on
# every turn. The pin is ROUTING state, never policy: the caller re-validates
# the stored member against the LIVE registry, the group's membership and the
# key's ACL on every read, and drops it when it no longer qualifies — the
# same discipline as the ep: pin.
#
# TTL: same contract as the ep: helper -- the CALLER owns the lease and the age
# comes from the row's updated_at, so an idle conversation's pin expires on its
# own. Both pins describe one conversation, so the routes layer passes the same
# settings.session_affinity_ttl_s it passes the endpoint pin.


async def get_group_pin(key_id: uuid.UUID, session: str | None, ttl_s: float) -> str | None:
    """The conversation's pinned group MEMBER, or None.

    ``session`` is the RAW affinity key (X-PCAI-Session or the conversation
    fingerprint); the grp: scope is applied HERE, exactly like the ep:
    helpers, so a caller can never address the cascade head's row by mistake.
    The TTL rides the row's updated_at: a pin older than the lease is expired
    — dropped, so it cannot outlive it — and reads as absent. ttl_s <= 0
    disables the expiry (the housekeeping retention sweep still applies).

    NEVER raises (module contract): any fault reads as "no pin", and the
    group re-picks by load — a stickiness outage must never change the
    response shape.
    """
    tk = scoped_thread_key(session, SCOPE_GROUP)
    if not tk:
        return None
    row = await get_sticky_row(key_id, tk)
    if row is None:
        return None
    pin, age = row
    if ttl_s > 0 and age > ttl_s:
        await drop_sticky(key_id, tk)
        return None
    return pin


async def set_group_pin(key_id: uuid.UUID, session: str | None, member: str) -> None:
    """Persist (or refresh) the conversation's group member pin.

    Best-effort, never raises (set_sticky's contract): the call that produced
    the pin already resolved, so a lost write costs one re-pick on the next
    turn. A re-serve refreshes updated_at, which is also the TTL clock.
    """
    await set_sticky(key_id, scoped_thread_key(session, SCOPE_GROUP), member)


async def drop_group_pin(key_id: uuid.UUID, session: str | None) -> None:
    """Forget the group pin (member left the group, its pool is unhealthy).

    Best-effort, never raises: the pin just proved stale, and leaving it in
    place would only cost one ignored read before the caller's live
    validation rejects it anyway.
    """
    await drop_sticky(key_id, scoped_thread_key(session, SCOPE_GROUP))


def make_writer(key_id: uuid.UUID, thread_key: str | None, defer: bool = False) -> Callable[[str], Any] | None:
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
