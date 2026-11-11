"""Per-model USD pricing (admin-configured), applied at settle time.

Prices are USD per 1M tokens (the industry convention) for input / output /
cached. The cost of a request is computed AND STORED on its ledger row at
request time — repricing later must not silently rewrite history. Cache:
in-memory, refreshed on boot and on every admin write (the table is tiny).

P10 units: a model_costs row may name a non-token unit (second, char, ...)
and the matching rows then price over billable_units via compute_units at
input_per_m as the per-1M-UNIT rate. See UNITS below for which units current
routes actually emit — the vocabulary is wider than the route surface.
"""

from __future__ import annotations

import asyncio
import decimal
import logging
import math
import time
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

# `Any` (not float) for the unit column — P10 widened the row shape; the
# token rates stay float/numeric at the call sites that price with them.
_cache: dict[str, dict[str, Any]] = {}
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


async def _load() -> dict[str, dict[str, Any]]:
    global _cache, _loaded_at
    if time.monotonic() - _loaded_at > _TTL:
        async with _load_lock:
            # Re-check under the lock: the winner has already refreshed.
            if time.monotonic() - _loaded_at > _TTL:
                # `unit` (P10) rides along for compute_units/unit_mismatches;
                # compute() ignores it, so the token path stays byte-identical.
                rows = await db.fetch(
                    "SELECT model_name, input_per_m, output_per_m, cached_per_m, unit FROM model_costs"
                )
                _cache = {r["model_name"]: dict(r) for r in rows}
                _loaded_at = time.monotonic()
    return _cache


async def invalidate() -> None:
    global _loaded_at
    _loaded_at = float("-inf")


# --- unpriced-model fallback rate (cost_fallback_per_m) --------------------
# An unpriced model used to be FREE against every USD budget, forever: compute()
# returns None, the ledger stores cost_usd NULL, and the budget SUM ignores
# NULL — so a tenant holding only a USD budget could burn unbounded GPU time on
# any newly registered model (audit 2026-11-10 §7). The knob is OFF by default
# (0.0 = keep the honest "unknown price" NULL); an operator turns it on to make
# the USD gate bound spend on models nobody has priced yet. ONE rate applied to
# every token the row billed — cached included: the fallback has no cached
# tier, and assuming a cache discount it cannot know would under-price exactly
# the traffic a spend gate exists to bound.
# Read through getattr so this module keeps working on a deployment whose
# Settings predates the field (default 0.0 = feature off).
_FALLBACK_KNOB = "cost_fallback_per_m"


def fallback_rate() -> Decimal | None:
    """The configured fallback USD/1M rate, or None when the knob is off/junk.

    None (not 0.0) is the "no fallback" signal: a non-finite or negative knob
    must never price a row, and a 0.0 rate would be indistinguishable from the
    legacy "unpriced" NULL in the ledger.
    """
    try:
        rate = Decimal(str(getattr(settings, _FALLBACK_KNOB, 0.0) or 0.0))
    except (decimal.InvalidOperation, ValueError, TypeError):
        return None
    if not rate.is_finite() or rate <= 0:
        return None
    return rate


def _fallback_cost(prompt: int, completion: int) -> Decimal | None:
    """Price a row at the fallback rate; None when the knob is off."""
    rate = fallback_rate()
    if rate is None:
        return None
    try:
        tokens = max(0, int(prompt)) + max(0, int(completion))
        cost = Decimal(str(tokens)) / 1_000_000 * rate
        out = cost.quantize(Decimal("0.000001"))
        return out if out.is_finite() else None
    except (decimal.InvalidOperation, ValueError, TypeError):
        return None


async def compute(model: str, prompt: int, completion: int, cached: int) -> Decimal | None:
    """USD cost for one request; None when the model is unpriced.

    An unpriced model prices at the FALLBACK rate when cost_fallback_per_m is
    configured (default off -> None, the historical behaviour).
    """
    costs = (await _load()).get(model)
    if costs is None:
        return _fallback_cost(prompt, completion)
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


async def stored_unit(model: str) -> str | None:
    """The unit model_costs STORES for `model` ('token' on a pre-P10 row), or
    None when the model is unpriced.

    The settle path (core.budgets) needs the STORED unit — not the route's
    estimate tag — to decide whether a tagged row may keep the legacy token
    arithmetic: that gate must read the operator's configuration, never the
    tag (cross-validation wave 2, 2026-11-10). Reads the same TTL cache the
    pricers use, so it adds no second source of truth and no extra query on a
    warm cache. None (unpriced) is NOT an error — the caller prices the row
    through its unit path and keeps the honest NULL.
    """
    costs = (await _load()).get(model)
    if costs is None:
        return None
    return costs.get("unit") or "token"


async def set_cost(
    model_name: str,
    input_per_m: float,
    output_per_m: float,
    cached_per_m: float,
    unit: str = "token",
) -> None:
    await db.execute(
        "INSERT INTO model_costs (model_name, input_per_m, output_per_m, cached_per_m, unit) "
        "VALUES ($1, $2, $3, $4, $5) "
        "ON CONFLICT (model_name) DO UPDATE SET input_per_m = $2, output_per_m = $3, "
        "cached_per_m = $4, unit = $5, updated_at = now()",
        model_name,
        input_per_m,
        output_per_m,
        cached_per_m,
        unit,
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
            inp, out = float(row["input_per_m"]), float(row["output_per_m"])
        except (TypeError, ValueError, KeyError):
            continue
        # Non-finite guard, mirroring compute() (audit 2026-11-10 P3): a NaN /
        # Infinity rate would hand the decision model "in nan / out 1 USD" and
        # poison the cost tie-break it is told to use.
        if not (math.isfinite(inp) and math.isfinite(out)):
            log.warning("costs.price_hints: non-finite rate for %r - hint omitted", name)
            continue
        hints[name] = f"in {inp:g} / out {out:g} USD per 1M tokens"
    return hints


async def unpriced(models: Iterable[str]) -> list[str]:
    """The models in `models` that have NO model_costs row, sorted.

    The USD-budget blind spot (audit 2026-11-10 §7): an unpriced model ledgers
    cost_usd NULL, and the budget SUM ignores NULL, so a tenant with only a USD
    budget can spend without bound on it. This is the WARNING half of the fix —
    the admin/startup surface names the gap instead of leaving it invisible;
    cost_fallback_per_m is the enforcing half.

    Fail-safe: an unreadable cost table returns [] (a fault must not invent a
    warning list), and duplicates are collapsed.
    """
    try:
        rates = await _load()
    except Exception as exc:
        log.warning("costs.unpriced: cost table unreadable (%s) - cannot report", type(exc).__name__)
        return []
    return sorted({str(m) for m in models if str(m) not in rates})


async def warn_unpriced(models: Iterable[str]) -> list[str]:
    """Log ONE WARNING naming the models with no model_costs row; return them.

    Wired at app startup (app.py lifespan, after the registry refresh) so the
    gap is named once per boot; the admin costs/routing read surface can call it
    too. One line per surface, never per request — the cost table is operator
    input, not per-request state.

    P10 EXTENSION (same warning, same surface — deliberately not a second
    guard): when `types_lookup` is installed (app.py hands the registry's
    model_type view over at boot; module-attribute injection, the same style
    tests use for `db`), a PRICED model whose model_costs.unit cannot match
    the unit its route bills is reported in the same breath as a
    misconfiguration — a wrong-rate conversion must be NAMED, never silently
    billed. The whole guard stays best-effort: a fault here must never block
    startup.
    """
    missing = await unpriced(models)
    if missing:
        log.warning(
            "costs: %d model(s) have no model_costs row and bill as UNPRICED"
            " (USD budgets cannot bound them; set a price or cost_fallback_per_m): %s",
            len(missing),
            ", ".join(missing),
        )
    if types_lookup is not None:
        try:
            mismatched = await unit_mismatches(models, types_lookup)
        except Exception:
            log.debug("costs: unit-mismatch check skipped", exc_info=True)
            mismatched = []
        for m in mismatched:
            log.warning(
                "costs: MISCONFIGURED unit — model %r is priced per %s but its routes "
                "(%s) bill per %s; set model_costs.unit='%s' or the rows ledger NULL",
                m["model"],
                m["unit"],
                m["model_type"],
                m["expected"],
                m["expected"],
            )
    return missing


# --- unit-aware pricing (P10) -----------------------------------------------
# model_costs gains a unit column ('token'|'char'|'second'|'image'|'request');
# every pre-existing row is 'token' and keeps billing through compute() exactly
# as before. The unit-aware routes (audio seconds, images, ...) settle through
# compute_units, which prices at input_per_m as the per-1M-UNIT rate and
# REFUSES a row whose stored unit does not match the unit the route bills:
# silently converting seconds through a token rate would be the exact
# 'bills at the wrong rate' defect the guard exists to name.
#
# VOCABULARY vs ROUTES (cross-validation wave 2): 'token' and 'second' are the
# only units a current route actually bills (chat/embeddings tokens; the audio
# paths). 'char', 'image' and 'request' are part of the vocabulary so an
# operator can pre-price for them, but NO route emits a matching unit_kind
# today — a model priced per one of those ledgers every real row cost NULL
# (the mismatch guard fires, never a wrong-rate conversion) until a route
# grows that unit. That is the honest state, and the boot WARNING names it.
UNITS: tuple[str, ...] = ("token", "char", "second", "image", "request")

# The unit(s) each registry model_type is EXPECTED to bill (P10). Voice is the
# only type whose honest unit is not (only) the token: a duration is a
# measurement, a char-derived token count is a proxy (see routes_proxy's audio
# paths). A voice pool may legitimately bill EITHER way depending on what the
# operator priced — measured seconds when a duration is available, or the TTS
# char proxy when the operator priced chars — so both are accepted here and a
# 'char'-priced voice model is NOT a misconfiguration. Unknown model types are
# never reported (extensible without touching this map).
EXPECTED_UNIT_BY_TYPE: dict[str, tuple[str, ...]] = {
    "chat": ("token",),
    "embedding": ("token",),
    "reranker": ("token",),
    "decision": ("token",),
    "voice": ("second", "char"),
}

# (model, billed unit) pairs already warned about this cache generation. A
# misconfigured unit is an OPERATOR fault, not per-request state: warn once per
# (model, unit) per cache refresh, not once per request — the same one-line-
# per-surface discipline as warn_unpriced.
_mismatch_warned: set[tuple[str, str]] = set()

# Optional registry view for the boot guard (P10): name -> the model_types
# serving it. app.py installs it once at startup so warn_unpriced's SAME
# warning surface can also name unit misconfigurations; None (the default) =
# the guard covers only the unpriced half. Module-attribute injection — the
# same style every test in this tree uses for `db`.
types_lookup: Callable[[str], Iterable[str]] | None = None


def _warn_unit_mismatch(model: str, billed: str, stored: str) -> None:
    key = (model, billed)
    if key in _mismatch_warned:
        return
    _mismatch_warned.add(key)
    log.warning(
        "costs: model %r is priced per %s but its route bills per %s — the row is NOT "
        "billed (cost NULL) until the unit matches (set model_costs.unit='%s'); "
        "misconfiguration, not a missing rate",
        model,
        stored,
        billed,
        billed,
    )


async def compute_units(model: str, units: float, unit: str) -> Decimal | None:
    """USD cost for a unit-aware row: units of 'unit', priced at input_per_m
    as the per-1M-UNIT rate. None when the model is UNPRICED or when the stored
    model_costs.unit does not match the billed unit — a mismatch returns None
    (ledger cost NULL) AND logs a WARNING (once per model/unit per cache
    generation) rather than silently converting through the wrong rate.

    No fallback-rate path: cost_fallback_per_m is a per-1M-TOKEN rate; applying
    it to seconds or images would fabricate a conversion the operator never
    configured. An unpriced unit-aware row stays honestly NULL.
    """
    costs = (await _load()).get(model)
    if costs is None:
        return None
    stored = costs.get("unit") or "token"
    if stored != unit:
        _warn_unit_mismatch(model, unit, stored)
        return None
    try:
        rate = Decimal(str(costs["input_per_m"]))
        if not rate.is_finite():
            log.warning("costs.compute_units: non-finite rate for %r — treating as unpriced", model)
            return None
        qty = Decimal(str(max(0.0, float(units))))
        out = (qty / 1_000_000 * rate).quantize(Decimal("0.000001"))
        return out if out.is_finite() else None
    except (decimal.InvalidOperation, ValueError, TypeError):
        log.warning("costs.compute_units: unquantizable rate for %r — treating as unpriced", model)
        return None


async def unit_mismatches(
    models: Iterable[str],
    types_of: Callable[[str], Iterable[str]],
) -> list[dict[str, str]]:
    """Priced models whose model_costs.unit cannot match what their route bills.

    types_of(name) yields the registry model_types serving 'name' (the caller
    owns the registry — core.costs stays import-light). A MIXED pool may bill
    more than one unit (per-endpoint routes); the stored unit must match at
    least one of them. Unknown model types are skipped, not guessed. Fail-safe:
    an unreadable cost table returns [] — a fault must not invent a report.
    """
    try:
        rates = await _load()
    except Exception as exc:
        log.warning("costs.unit_mismatches: cost table unreadable (%s) - cannot report", type(exc).__name__)
        return []
    out: list[dict[str, str]] = []
    for name in sorted({str(m) for m in models}):
        row = rates.get(name)
        if not isinstance(row, dict):
            continue  # unpriced — the unpriced report owns that gap
        stored = row.get("unit") or "token"
        known_types = sorted({t for t in types_of(name) if t in EXPECTED_UNIT_BY_TYPE})
        expected_units = {u for t in known_types for u in EXPECTED_UNIT_BY_TYPE[t]}
        if expected_units and stored not in expected_units:
            out.append(
                {
                    "model": name,
                    "unit": stored,
                    "expected": "/".join(sorted(expected_units)),
                    "model_type": ",".join(known_types),
                }
            )
    return out


async def list_costs() -> list[dict]:
    return [
        dict(r)
        for r in await db.fetch(
            "SELECT model_name, input_per_m, output_per_m, cached_per_m, unit, updated_at "
            "FROM model_costs ORDER BY model_name"
        )
    ]
