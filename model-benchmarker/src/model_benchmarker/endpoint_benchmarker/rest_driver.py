"""REST driver: one simulated user looping over the reference query pool.

Same closed-loop shape as the original Multimodal RAG benchmark script's
``run_user`` (each user keeps one request in flight, samples the pool with a
per-user seeded RNG, stops after the level duration), but the request shape
comes from the :class:`~endpoint_benchmarker.targets.RestTarget` so *any*
endpoint can be driven, not just ``GET /api/datasets/{name}/search``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import httpx

from .stats import BenchmarkStats, err_key
from .targets import RestTarget

log = logging.getLogger("endpoint_benchmarker")


def _count_results(data: object) -> int:
    """Best-effort result-count extraction from an arbitrary JSON response.

    ``{"results": [...]}`` (MM RAG shape) -> len of that list; a bare JSON
    array -> its length; anything else -> 1 (a response arrived and parsed).
    """
    if isinstance(data, dict):
        for key in ("results", "documents", "hits", "matches", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return len(value)
        return 1
    if isinstance(data, list):
        return len(data)
    return 1


async def run_user_rest(
    user_id: int,
    client: httpx.AsyncClient,
    target: RestTarget,
    queries: list[str],
    duration: float,
    ramp_delay: float,
    stats: BenchmarkStats,
    call_timeout: float = 120.0,
    seed: int | None = None,
) -> None:
    rng = random.Random(seed if seed is not None else user_id)

    if ramp_delay > 0:
        await asyncio.sleep(ramp_delay)

    end_time = time.monotonic() + duration
    while time.monotonic() < end_time:
        query = rng.choice(queries)
        url, params, body = target.build_request(query)
        t0 = time.monotonic()
        try:
            resp = await client.request(
                target.method,
                url,
                params=params,
                headers=target.headers,
                content=body,
                timeout=call_timeout,
            )
            elapsed = time.monotonic() - t0
            if 200 <= resp.status_code < 300:
                nbytes = len(resp.content)
                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError):
                    data = None
                stats.record_success(elapsed, n_results=_count_results(data), n_bytes=nbytes, status=resp.status_code)
                log.debug(
                    "[user=%s] %s %s -> %s in %.0fms (%d results, %d bytes)",
                    user_id,
                    target.method,
                    url,
                    resp.status_code,
                    elapsed * 1000,
                    _count_results(data),
                    nbytes,
                )
            else:
                snippet = resp.text[:120].replace("\n", " ")
                stats.record_failure(
                    elapsed,
                    f"HTTP {resp.status_code}: {snippet}" if resp.status_code >= 400 else f"HTTP {resp.status_code}",
                    status=resp.status_code,
                )
                log.debug("[user=%s] HTTP %s in %.0fms: %s", user_id, resp.status_code, elapsed * 1000, snippet)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            stats.record_failure(elapsed, err_key(exc))
            log.debug("[user=%s] request failed in %.0fms: %s", user_id, elapsed * 1000, exc)
