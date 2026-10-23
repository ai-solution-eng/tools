"""Effective-prompt review surface (ROADMAP §7 phase 2 wave C: transparency).

Admin reads for the capture written by core.effective: per model call, what
the upstream ACTUALLY received — post reasoning-strip, post canonical
reconciliation, post context-budget trim. WHY a dedicated small router
instead of a routes_admin block: it needs exactly one endpoint, and the
admin gate (`require_admin`, hash-set bearer over settings.admin_api_keys,
503 when the admin API is disabled) is IMPORTED from routes_admin rather
than reimplemented — one auth definition for every /admin surface, so a
future hardening there cannot leave this route behind.

Contract when capture is OFF (or faulted away): the route still works and
returns {"rows": []} — the table is simply empty. The read does not depend
on the capture flag, which keeps the operator contract simple: 200 always
(auth permitting), empty = nothing captured.

Bounded previews come back as stored (they were bounded at WRITE time by
the effective_prompt_* caps) — this surface adds no size bounds of its own
beyond the row limit clamp, because the rows themselves are the bounded
artifact.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter

from .routes_admin import AdminDep

router = APIRouter(prefix="/admin", tags=["admin"])

LIMIT_CAP = 200  # newest-first rows per call — the same cap discipline as /admin/traces


@router.get("/effective-prompts")
async def list_effective_prompts(
    _: AdminDep,
    key_id: uuid.UUID | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Newest-first captured effective prompts for one key (optionally one
    thread). `limit` clamps to [1, 200], default 50.

    key_id is the primary filter (rows are keyed per key): an admin
    investigating a complaint scopes to the key, then narrows by thread_id
    (the canonical conversation fingerprint) to read one conversation's
    per-call prompts in order.
    """
    from ..core import effective

    if key_id is None:
        return {"rows": []}
    rows = await effective.recent(key_id, thread_id, max(1, min(limit, LIMIT_CAP)))
    return {"rows": rows}
