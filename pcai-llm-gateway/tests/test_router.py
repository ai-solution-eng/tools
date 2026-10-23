"""Endpoint-pool selection and circuit-breaker state machine.

Pure logic tests against a fresh Router/Pool — no DB, no upstream.
"""

from __future__ import annotations

import time

from pcai_llm.config import settings
from pcai_llm.routing.router import Endpoint, Pool, Router


def _ep(name: str, priority: int = 0, weight: int = 1) -> Endpoint:
    return Endpoint(id=name, model_name="m", base_url=f"http://{name}", priority=priority, weight=weight)


def test_pick_prefers_lowest_priority_tier() -> None:
    pool = Pool(endpoints=[_ep("a", priority=1), _ep("b", priority=0)])
    for _ in range(20):
        ep = pool.pick()
        assert ep is not None and ep.id == "b"


def test_pick_weighted_among_equal_priority() -> None:
    pool = Pool(endpoints=[_ep("a", weight=1), _ep("b", weight=9)])
    picks: set[str] = set()
    for _ in range(200):
        ep = pool.pick()
        assert ep is not None
        picks.add(ep.id)
    assert picks == {"a", "b"}  # both reachable; distribution not asserted (nondeterminism)


def test_pick_none_when_all_cooled_down() -> None:
    ep = _ep("a")
    ep.cooldown_until = time.monotonic() + 999
    pool = Pool(endpoints=[ep])
    assert pool.pick() is None


def test_breaker_opens_after_threshold() -> None:
    import asyncio

    async def run() -> None:
        pool = Pool(endpoints=[_ep("a")])
        ep = pool.endpoints[0]
        for _ in range(settings.endpoint_failure_threshold):
            await pool.report(ep, ok=False)
        assert ep.cooldown_until > time.monotonic()

    asyncio.run(run())


def test_breaker_resets_on_success() -> None:
    import asyncio

    async def run() -> None:
        pool = Pool(endpoints=[_ep("a")])
        ep = pool.endpoints[0]
        await pool.report(ep, False)
        await pool.report(ep, False)
        await pool.report(ep, True)  # success clears the streak
        for _ in range(settings.endpoint_failure_threshold - 1):
            await pool.report(ep, False)
        assert ep.cooldown_until == 0.0  # not opened

    asyncio.run(run())


def test_router_registers_models() -> None:
    r = Router()
    r.set_endpoints("qwen", [_ep("vllm-1")])
    r.set_endpoints("glm", [_ep("vllm-2")])
    assert r.models() == ["glm", "qwen"]
    assert r.pool("qwen") is not None
    assert r.pool("nope") is None
