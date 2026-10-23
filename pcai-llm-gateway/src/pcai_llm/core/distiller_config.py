"""Distiller runtime configuration: DB-backed, admin-editable.

The env/Settings knobs (config.py) are the DEPLOYMENT defaults; this
module is the OPERATOR override layer the admin panel writes through,
following the offpeak_model_config pattern: rows, TTL-cached, loaded
degrade-not-fail (a DB blip keeps the last known policy, never resets
distillation to defaults mid-flight).

Configured here (the asks):
  * model            - the summarizer model (empty = the calling key own
                       routing). "Qwen" in the fleet bundle.
  * offpeak_only     - when true, housekeeping-driven distills run ONLY
                       while the offpeak evaluator reports low demand
                       (the GPU-sparing default); explicit /v1/distill
                       calls always run (a human asked).
  * enabled          - runtime on/off over the env flag (both must be
                       true for the surface to act; the env flag still
                       unmounts the router entirely).
  * debounce_minutes - quiet-window before housekeeping distills a
                       conversation (the DSH plugin debounceMs, in minutes
                       at housekeeping cadence).
  * max_tokens       - distill output cap (0 = the env default).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

_CACHE_TTL_S = 60.0
_cache: dict[str, Any] = {}
_cache_at = 0.0

DEFAULTS: dict[str, Any] = {
    "model": "",
    "offpeak_only": False,
    "enabled": True,
    "debounce_minutes": 5,
    "max_tokens": 0,
}


# TEXT-column type tags (DeepSeek-V4.1 cross-review P0): asyncpg round-trips a
# Python bool through a TEXT column as the string "false"/"true", and
# bool("false") is True — an admin's offpeak_only=false would read back ON,
# silently inverting the policy. Every stored value carries an explicit type
# tag ("b:", "i:", "s:") so the decode is exact; untagged legacy values
# (pre-fix rows) read as strings and pass through the per-key coercion
# fallbacks, which behave as before.
def _encode(value: Any) -> str:
    if isinstance(value, bool):
        return "b:true" if value else "b:false"
    if isinstance(value, int):
        return "i:" + str(value)
    return "s:" + str(value)


def _decode(raw: Any) -> Any:
    if isinstance(raw, (bool, int)):
        return raw  # a real typed column (tests' fake stores Python objects)
    text = str(raw)
    if text.startswith("b:"):
        return text[2:] == "true"
    if text.startswith("i:"):
        try:
            return int(text[2:])
        except ValueError:
            return text
    if text.startswith("s:"):
        return text[2:]
    return text  # legacy untagged row: string passthrough


async def _config() -> dict[str, Any]:
    """The merged runtime config (env defaults <- DB overrides), TTL-cached,
    degrade-not-fail."""
    global _cache, _cache_at
    now = time.monotonic()
    if _cache and now - _cache_at < _CACHE_TTL_S:
        return _cache
    merged = dict(DEFAULTS)
    try:
        rows = await db.fetch("SELECT key, value FROM distiller_config")
        for row in rows:
            if row["key"] in merged:
                merged[row["key"]] = _decode(row["value"])
    except Exception as exc:
        log.warning("distiller config refresh failed (%s) - keeping last known policy", type(exc).__name__)
        if _cache:
            return _cache
    _cache = merged
    _cache_at = now
    return merged


async def invalidate() -> None:
    """Force the next read to hit the DB (admin write path).

    Clears BOTH the timestamp and the cached dict (DeepSeek-V4.1 review): an
    in-flight reader that assigned _cache after its DB read but before this
    invalidation would otherwise restore the stale dict WITH a fresh
    timestamp, pinning stale admin config for a full TTL."""
    global _cache, _cache_at
    _cache = {}
    _cache_at = 0.0


async def get(key: str) -> Any:
    cfg = await _config()
    return cfg.get(key, DEFAULTS.get(key))


async def get_model() -> str:
    """The configured summarizer model (empty = the key own routing). Falls
    back to settings.distiller_model when the DB has no row (env default)."""
    model = await get("model")
    if model is None or model == "":
        return settings.distiller_model
    return str(model)


async def offpeak_only() -> bool:
    v = await get("offpeak_only")
    return bool(v)


async def is_enabled() -> bool:
    """The AND of the env flag and the runtime switch: the env flag still
    owns the mount (unmounted = 404); this owns whether distills act."""
    if not settings.distiller_enabled:
        return False
    v = await get("enabled")
    return bool(v)


async def debounce_minutes() -> int:
    v = await get("debounce_minutes")
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return int(DEFAULTS["debounce_minutes"])


async def max_tokens() -> int:
    v = await get("max_tokens")
    try:
        n = int(v)
    except (TypeError, ValueError):
        return settings.distiller_max_tokens
    return n if n > 0 else settings.distiller_max_tokens


async def set_values(values: dict[str, Any]) -> dict[str, Any]:
    """Admin write: upsert each provided key, clear keys set to None,
    return the merged effective config. Unknown keys are refused by the
    routes layer (this module only stores what it knows).

    ONE transaction (DeepSeek-V4.1 cross-review): a multi-key PUT that dies
    midway must not leave a half-applied admin mutation that the caller is
    told succeeded. Bounded acquire (D-P11): pool exhaustion surfaces fast,
    never queues silently."""
    known = {k: v for k, v in values.items() if k in DEFAULTS}
    if known:
        async with db.acquire_bounded() as conn, conn.transaction():
            for key, value in known.items():
                if value is None:
                    await conn.execute("DELETE FROM distiller_config WHERE key = $1", key)
                else:
                    await conn.execute(
                        "INSERT INTO distiller_config (key, value) VALUES ($1, $2) "
                        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()",
                        key,
                        _encode(value),
                    )
    await invalidate()
    return await _config()
