"""Per-model fallback chains (router extras, review §4a).

A virtual model can carry an ADMIN-CURATED fallback chain, independent of
the profile `model_order` cascade: when a request's model cannot serve —
every endpoint cooling/absent (`pool.pick()==None`) or an upstream
transport failure / 5xx after its breaker report — the chain is walked
instead of the request ending answerless.

The chain is not the ONLY runtime failover source: for ALIAS requests
(`auto`) the routes layer appends the profile ordering's tail — see
`routes_proxy._relay_fallback_tail` — as the FINAL segment of the walk, so
an alias whose admitted model cannot serve keeps cascading even when no
chain was curated. The per-model chain stays FIRST (an explicit per-model
sign-off outranks the ordering), and both segments share the one
`model_fallback_max` bound. `chain_for` itself stays a pure per-model read;
the tail is composed by the caller.

Design contract (mirrors cascade.py's discipline):
- PINNED requests keep their failover chain, too — a pin says "serve THIS
  model", and this module only engages when THAT model demonstrably cannot
  (no healthy endpoint / upstream 5xx). It never overrides a model that
  answered.
- The chain is a PREFERENCE list, never a policy bypass. Every candidate
  is re-gated at WALK time by the caller (registered + key-allowed +
  healthy chat pool + `check_budgets` headroom) — a candidate that is
  itself exhausted or forbidden is skipped, not served. `chain_for` is a
  pure DB read; the gates live in the routes layer where KeyRecord and the
  router snapshot are already in hand (same split as cascade.resolve_cascade
  vs routes_proxy._admit_with_failover).
- Bounded: the walk tries at most `settings.model_fallback_max` candidates
  (default 2) after the primary. 0 = chains are never walked (kill switch).
- Ledger authoritative: each ATTEMPT settles under its ACTUAL model — the
  same per-candidate rule the MCP failover applies (routes_mcp) and the
  relay's per-iteration settle applies. The response's X-PCAI-Fallback
  header reports the chain hop.
"""

from __future__ import annotations

from ..config import settings
from ..db import db


async def chain_for(model_name: str) -> list[str]:
    """The model's fallback names, rank-ordered, deduped (best-effort read).

    A DB fault degrades to an EMPTY chain — the fallback feature must never
    turn a registry hiccup into a request failure (the primary's own error
    handling is already in flight when this runs).
    """
    if settings.model_fallback_max <= 0:
        return []
    try:
        rows = await db.fetch(
            """
            SELECT fallback_model FROM model_fallbacks
            WHERE model_name = $1
            ORDER BY rank ASC, fallback_model ASC
            """,
            model_name,
        )
    except Exception:
        return []
    out: list[str] = []
    for r in rows:
        name = str(r["fallback_model"])
        if name and name != model_name and name not in out:
            out.append(name)
    return out[: max(0, settings.model_fallback_max)]
