"""Per-model USD pricing (admin-configured), applied at settle time.

Prices are USD per 1M tokens (the industry convention) for input / output /
cached. The cost of a request is computed AND STORED on its ledger row at
request time — repricing later must not silently rewrite history. Cache:
in-memory, refreshed on boot and on every admin write (the table is tiny).
"""

from __future__ import annotations

import asyncio
import decimal
import logging
import time
from decimal import Decimal

from ..db import db

log = logging.getLogger(__name__)

_cache: dict[str, dict[str, float]] = {}
# NEGATIVE INFINITY, not 0.0: time.monotonic() is uptime-based, so on a host
# whose uptime is below the TTL the comparison "monotonic() - 0.0 > 60" is
# FALSE at startup and the cache never loads -- every request then bills as
# UNPRICED until the host crosses 60s of uptime.
_loaded_at = float("-inf")
_TTL = 60.0


# One refresh at a time. Without it, every concurrent routing decision that
# lands during a 60s TTL expiry fires its own full-table SELECT -- a thundering
# herd on the pool that the routing path (bounded at 2500ms) cannot afford.
_load_lock = asyncio.Lock()


async def _load() -> dict[str, dict[str, float]]:
    global _cache, _loaded_at
    if time.monotonic() - _loaded_at > _TTL:
        async with _load_lock:
            # Re-check under the lock: the winner has already refreshed.
            if time.monotonic() - _loaded_at > _TTL:
                rows = await db.fetch("SELECT model_name, input_per_m, output_per_m, cached_per_m FROM model_costs")
                _cache = {r["model_name"]: dict(r) for r in rows}
                _loaded_at = time.monotonic()
    return _cache


async def invalidate() -> None:
    global _loaded_at
    _loaded_at = float("-inf")


async def compute(model: str, prompt: int, completion: int, cached: int) -> Decimal | None:
    """USD cost for one request; None when the model is unpriced."""
    costs = (await _load()).get(model)
    if costs is None:
        return None
    uncached_prompt = max(0, prompt - cached)
    # Defense in depth (audit P1-1): a poisoned rate (NaN/Infinity leaked in
    # before the pydantic guard, or a row written by an older build) must never
    # 500 the settle path — the row prices as UNPRICED (None) instead.
    try:
        rates = (
            Decimal(str(costs["input_per_m"])),
            Decimal(str(costs["output_per_m"])),
            Decimal(str(costs["cached_per_m"])),
        )
        if any(not r.is_finite() for r in rates):
            log.warning("costs.compute: non-finite rate for %r — treating as unpriced", model)
            return None
        cost = (
            Decimal(str(uncached_prompt)) / 1_000_000 * rates[0]
            + Decimal(str(completion)) / 1_000_000 * rates[1]
            + Decimal(str(cached)) / 1_000_000 * rates[2]
        )
        out = cost.quantize(Decimal("0.000001"))
        return out if out.is_finite() else None
    except (decimal.InvalidOperation, ValueError):
        log.warning("costs.compute: unquantizable rate for %r — treating as unpriced", model)
        return None


async def set_cost(model_name: str, input_per_m: float, output_per_m: float, cached_per_m: float) -> None:
    await db.execute(
        "INSERT INTO model_costs (model_name, input_per_m, output_per_m, cached_per_m) "
        "VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (model_name) DO UPDATE SET input_per_m = $2, output_per_m = $3, "
        "cached_per_m = $4, updated_at = now()",
        model_name,
        input_per_m,
        output_per_m,
        cached_per_m,
    )
    await invalidate()


async def delete_cost(model_name: str) -> bool:
    """Remove a model's price row (a renamed/decommissioned model leaves the
    old name's price behind forever otherwise). Ledger history is untouched:
    costs were computed AND STORED per row at request time — deletion only
    affects future requests, which ledger NULL (unpriced)."""
    tag = await db.execute("DELETE FROM model_costs WHERE model_name = $1", model_name)
    await invalidate()
    return tag == "DELETE 1"


async def price_hints(models: list[str]) -> dict[str, str]:
    """Compact per-model price string for the routing decision state.

    The decision model is told to break ties by cost, but nothing populated
    candidates[].cost, so that instruction could never fire. The rates already
    exist (model_costs, the same table the settle path bills from), so the hint
    is derived from them rather than hand-maintained anywhere: one
    "in X / out Y USD per 1M tokens" line per PRICED model, omitted for
    unpriced ones (a model with no rate is unknown, not free).
    NEVER raises: a cost-table fault must cost a tie-break, never a request.
    """
    try:
        rates = await _load()
    except Exception as exc:
        log.warning("costs.price_hints: cost table unreadable (%s) - no hints", type(exc).__name__)
        return {}
    hints: dict[str, str] = {}
    for name in models:
        row = rates.get(name)
        if not isinstance(row, dict):
            continue
        try:
            hints[name] = (
                f"in {float(row['input_per_m']):g} / out {float(row['output_per_m']):g} USD per 1M tokens"
            )
        except (TypeError, ValueError, KeyError):
            continue
    return hints


async def list_costs() -> list[dict]:
    return [
        dict(r)
        for r in await db.fetch(
            "SELECT model_name, input_per_m, output_per_m, cached_per_m, updated_at "
            "FROM model_costs ORDER BY model_name"
        )
    ]
