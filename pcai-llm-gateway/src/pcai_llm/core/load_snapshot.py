"""Live per-member load view for model-group selection (background snapshot).

WHY a snapshot and not a query: the pick runs on the REQUEST path, and
Prometheus is a second network hop that can be slow or down. Group selection
only needs three numbers per pod (queue depth, running requests, KV-cache
percent) and it needs them NOW, so a background task folds the metrics into
an in-memory ``member -> min queue`` map that the request path reads with a
dict lookup. Same discipline as core.offpeak: the data plane never waits on
the monitoring plane.

Contract (fail-open, like every other routing read):

* ``start()`` is idempotent and a NO-OP when settings.model_groups_enabled is
  false — a disabled feature costs nothing (no task, no queries).
* A refresh NEVER raises: ``endpoint_metrics.load_rows()`` is best-effort by
  its own contract, and this module additionally tolerates the function
  being absent (it is added in a parallel change). A missing collector means
  "no data", which the picker already handles as a uniform random draw.
* STALENESS is the safety valve: a snapshot older than
  settings.model_group_load_stale_s reads as NO DATA for every member, so a
  Prometheus outage degrades selection to uniform random instead of herding
  every conversation onto whichever member happened to be idle when the
  collector last worked.
* The score is the MIN queue across a member's pods: a member is as
  available as its least-loaded replica. A mean would let one jammed replica
  hide behind idle siblings, which is exactly the state the pick exists to
  avoid.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

# How long stop() waits for the loop to notice the stop event. The refresh
# itself is bounded by endpoint_metrics' own query timeout; this only keeps a
# hung collector from delaying shutdown.
_STOP_TIMEOUT_S = 5.0

# Floor on the refresh interval: a mis-set 0 would spin the loop hot, and a
# Prometheus that answers instantly is still a monitoring-plane load.
_MIN_REFRESH_S = 0.1

# Pool name -> min queue across that pool's rows.
_SCORES: dict[str, float] = {}
# Normalised model_name -> min queue, for rows the pool join could NOT
# attribute (row.pool == ""). Group members are registry pool names, so this
# is only a fallback — but a pool registered under a slightly different name
# than the pod's model path is exactly the case it rescues.
_KEY_SCORES: dict[str, float] = {}
# Monotonic time of the last SUCCESSFUL refresh; None = never.
_LAST_OK: float | None = None

_TASK: asyncio.Task[None] | None = None
_STOP: asyncio.Event | None = None
# The missing-collector warning is logged ONCE: the refresh loop would
# otherwise repeat it every interval for the life of the process.
_WARNED_MISSING = False

# models--Qwen--Qwen3.8-27B/snapshots/<sha> -> Qwen/Qwen3.8-27B -> qwen3827b.
# Mirrors endpoint_metrics._model_key (the pod -> pool join's own rule); it is
# duplicated rather than imported so this module keeps working if that
# private helper moves — the two are pinned to the same behaviour by test.
_MODEL_PATH_RE = re.compile(r"models--([^/]+)")
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _model_key(raw: str) -> str:
    """Normalise a model path or pool name to a comparable key. PURE."""
    text = str(raw or "")
    m = _MODEL_PATH_RE.search(text)
    if m:
        text = m.group(1).replace("--", "/")
    text = text.rstrip("/").rsplit("/", 1)[-1]
    return _NON_ALNUM.sub("", text.lower())


def _pools() -> dict[str, list[str]]:
    """Pool name -> endpoint base URLs, from the live registry. NEVER raises.

    Imported inside the function: the router owns the pools and importing it
    at module scope would couple the metrics path to the data plane's
    startup order (endpoint_metrics is deliberately router-free for the same
    reason).
    """
    try:
        from ..routing.router import router as model_router
    except Exception:
        log.debug("model groups: router unavailable — no pools for attribution", exc_info=True)
        return {}
    out: dict[str, list[str]] = {}
    try:
        names = model_router.models()
    except Exception:
        log.debug("model groups: router.models() failed — no pools for attribution", exc_info=True)
        return {}
    for name in names:
        try:
            pool = model_router.pool(name)
            urls = [ep.base_url for ep in (pool.endpoints if pool is not None else []) if getattr(ep, "base_url", "")]
        except Exception:
            log.debug("model groups: pool %r unreadable — skipped", name, exc_info=True)
            continue
        if urls:
            out[name] = urls
    return out


def _queue_of(row: dict[str, Any]) -> float | None:
    """The row's queue depth as a finite float, or None when unusable. PURE."""
    value = row.get("queue")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _min_queue(queues: list[float | None]) -> float:
    """The MIN queue of a member's rows; a row without a value counts as 0.0.

    "Row present but the query returned no sample" is a healthy, idle pod as
    far as selection is concerned — reporting it as NO DATA would make the
    picker ignore the member entirely, which is the opposite of what an idle
    member should attract.
    """
    known = [q for q in queues if q is not None]
    return min(known) if known else 0.0


def _fold(rows: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, float]]:
    """(pool scores, model-key scores) from load_rows rows. PURE, total."""
    pool_q: dict[str, list[float | None]] = {}
    key_q: dict[str, list[float | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        queue = _queue_of(row)
        pool = str(row.get("pool") or "").strip()
        if pool:
            pool_q.setdefault(pool, []).append(queue)
            continue
        key = _model_key(str(row.get("model_name") or ""))
        if key:
            key_q.setdefault(key, []).append(queue)
    return (
        {name: _min_queue(queues) for name, queues in pool_q.items()},
        {key: _min_queue(queues) for key, queues in key_q.items()},
    )


async def _refresh_once() -> None:
    """One fold of the live metrics into the in-memory view. NEVER raises."""
    global _SCORES, _KEY_SCORES, _LAST_OK, _WARNED_MISSING
    try:
        from . import endpoint_metrics as em
    except Exception:
        log.warning("model groups: endpoint_metrics unavailable — no load data", exc_info=True)
        return
    loader = getattr(em, "load_rows", None)
    if loader is None:
        if not _WARNED_MISSING:
            _WARNED_MISSING = True
            log.warning(
                "model groups: endpoint_metrics.load_rows is missing — the load snapshot stays "
                "empty and selection falls back to a uniform random draw (fail-open)"
            )
        return
    try:
        rows = await loader(_pools())
    except Exception:
        log.warning("model groups: load snapshot refresh failed — keeping the previous view", exc_info=True)
        return
    if not isinstance(rows, list):
        log.warning("model groups: load snapshot returned %s, not a list — ignored", type(rows).__name__)
        return
    scores, key_scores = _fold(rows)
    _SCORES = scores
    _KEY_SCORES = key_scores
    _LAST_OK = time.monotonic()


def _interval_s() -> float:
    try:
        configured = float(settings.model_group_load_refresh_s or 0.0)
    except (TypeError, ValueError):
        configured = 0.0
    return max(_MIN_REFRESH_S, configured)


async def _run(stop: asyncio.Event) -> None:
    """Refresh until ``stop`` is set. The ONLY exit paths are stop/cancel."""
    while not stop.is_set():
        try:
            await _refresh_once()
        except Exception:  # _refresh_once is total; belt and braces for the loop
            log.warning("model groups: load snapshot iteration failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_interval_s())
        except TimeoutError:
            continue


def start() -> None:
    """Start the background refresh task. Idempotent; no-op when disabled."""
    global _TASK, _STOP
    if not settings.model_groups_enabled:
        log.debug("model groups: load snapshot disabled (model_groups_enabled=false)")
        return
    if _TASK is not None and not _TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("model groups: start() called outside a running loop — load snapshot not started")
        return
    stop = asyncio.Event()
    _STOP = stop
    _TASK = loop.create_task(_run(stop), name="model-group-load-snapshot")
    log.info("model groups: load snapshot started (refresh %.1fs)", _interval_s())


async def stop() -> None:
    """Stop the background task (safe when never started; NEVER raises)."""
    global _TASK, _STOP
    task = _TASK
    stop_event = _STOP
    _TASK = None
    _STOP = None
    if stop_event is not None:
        stop_event.set()
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=_STOP_TIMEOUT_S)
    except TimeoutError:
        log.warning("model groups: load snapshot did not stop within %.1fs — cancelled", _STOP_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("model groups: load snapshot task ended with an error", exc_info=True)
    log.info("model groups: load snapshot stopped")


def age_s() -> float:
    """Seconds since the last SUCCESSFUL refresh (inf when never refreshed)."""
    if _LAST_OK is None:
        return math.inf
    return max(0.0, time.monotonic() - _LAST_OK)


def is_fresh() -> bool:
    """True when the last successful refresh is inside the staleness window.

    stale_s <= 0 is read as "never fresh" (the window is disabled, not
    infinite): an operator turning the window off must get the documented
    no-data fallback, never a snapshot that stays authoritative forever.
    """
    try:
        stale = float(settings.model_group_load_stale_s or 0.0)
    except (TypeError, ValueError):
        stale = 0.0
    if stale <= 0:
        return False
    return age_s() <= stale


def member_score(member: str) -> float | None:
    """The member's min queue depth, or None when there is no usable data.

    None covers all three "pick blind" cases — the feature never refreshed,
    the snapshot went stale, or the member has no reporting pod — so the
    picker treats them identically (a uniform random draw). NEVER raises.
    """
    if not member or not is_fresh():
        return None
    score = _SCORES.get(member)
    if score is not None:
        return score
    return _KEY_SCORES.get(_model_key(member))
