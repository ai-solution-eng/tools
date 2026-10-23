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
"""

from __future__ import annotations

import logging
import uuid

from ..db import db
from . import metrics

log = logging.getLogger(__name__)


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
