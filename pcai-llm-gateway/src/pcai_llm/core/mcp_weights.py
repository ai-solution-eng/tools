"""Per-MCP-server tool-relay WEIGHTS — what ONE call to a server charges.

The relay's per-turn bound is a BUDGET in fractional UNITS, not a count of
model rounds (2026-11-03; db/schema.py carries the WHY). This module is the
weights half of that arithmetic: it publishes `{server name -> unit weight}`
from the control plane so core.toolrelay.RelayGuard can charge each tool call
its server's price. An absent server weighs 1.0 — the neutral default every
pre-existing row lands on — so with no weights configured a call costs one
unit.

NOT byte-for-byte the old bound, deliberately (cross-review 2026-11-03). The
removed check counted MODEL ROUNDS, once per round; this charges per tool CALL.
A round that emits k parallel calls therefore costs k units here and 1 there:
the two agree only for single-call rounds, and for parallel batches the new
bound is STRICTER, never looser. A budget sized for round counts must be
scaled up accordingly (the relay's own default was raised for that reason).

Read discipline (same shape as core.model_routing.profile_routing_config):
- TTL-cached for 60 s: weights change on OPERATOR ACTION, not per request, so
  a cached dict read is what keeps the relay's hot path free of a per-turn
  query. Admin writes call invalidate() so a console edit is live at once.
- NEVER raises. Any lookup failure returns {} UNGACHED: a DB blip then costs
  exactly ONE turn its per-server pricing (every server charges the neutral
  1.0 and the budget still bounds the turn) instead of pinning the stale map
  for a minute. The relay must never fail a request over a weight lookup.
- Enabled servers only: a disabled server is unreachable through the relay, so
  its weight is irrelevant; omitting it just means "1.0 if it ever appears".

Why REAL and not INTEGER (schema): 0.1 is the useful shape — a cheap server
("charge a tenth of a call") is what makes the budget express COST instead of
CALL COUNT. Values <= 0 are dropped here (a free server would let a loop on it
run to the wall-clock bound alone); the admin API already floors writes at 0.01.
"""

from __future__ import annotations

import logging
import time

from ..db import db

log = logging.getLogger(__name__)

# Weights change on operator action, not per request — the same staleness
# trade (and the same 60 s) as core.model_routing's profile-policy cache.
_TTL_S = 60.0
# None = never loaded (or invalidated); {} = loaded, no weighted servers.
_cache: dict[str, float] | None = None
_cache_at = 0.0


async def weights() -> dict[str, float]:
    """server name -> unit weight charged per call. NEVER raises; {} on fault.

    Returns a COPY: the relay may hold it for a whole turn, and a caller that
    mutated the cached dict would corrupt every other in-flight turn in this
    process (the registry snapshot's per-read copy discipline).
    """
    global _cache, _cache_at
    now = time.monotonic()
    if _cache is not None and now - _cache_at < _TTL_S:
        return dict(_cache)
    try:
        rows = await db.fetch("SELECT name, tool_weight FROM mcp_servers WHERE enabled")
    except Exception as exc:
        # UNGACHED (see module docstring): one degraded turn, not a minute.
        log.warning("mcp_weights: lookup failed (degrading to the neutral 1.0 weight): %s", exc)
        return {}
    out: dict[str, float] = {}
    for row in rows or []:
        # .get, not [] — asyncpg Records AND the tests' dict fakes both
        # implement it, and a fake/legacy row without the column degrades to
        # the neutral weight instead of a KeyError (the core.keys idiom).
        try:
            name = str(row["name"])
            raw = row.get("tool_weight")
            weight = 1.0 if raw is None else float(raw)
        except (KeyError, TypeError, ValueError):
            continue  # one malformed row must not unpin every other server
        if weight > 0:
            out[name] = weight
    _cache = out
    _cache_at = now
    return dict(out)


def invalidate() -> None:
    """Drop the cached weights — the admin-write hook (MCP server upserts).

    Synchronous on purpose: it runs inline in the admin write path beside the
    registry-change hooks, and there is nothing to await.
    """
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0
