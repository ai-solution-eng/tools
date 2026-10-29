"""Global routing arming + ladder (2026-10-28).

Routing used to be armed per-profile and to borrow profiles.model_order (a
CASCADE ordering) as its capability ladder. Two production consequences: the
routing panel could read decision_mode=active while every profile sat at the
'off' default (nothing served, no visible cause), and a profile with an empty
model_order made bare `routing` 404. These tests pin the global defaults and
the one-way inheritance rule.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pcai_llm.core import model_routing, routing_config


def _fake_db(row: dict[str, Any] | None):
    class _DB:
        async def fetchrow(self, *a, **k):
            return row

    return _DB()


@pytest.fixture(autouse=True)
def clear_caches():
    model_routing._policy_cache.clear()
    yield
    model_routing._policy_cache.clear()


async def test_global_policy_reads_config(monkeypatch) -> None:
    async def cfg():
        return {"routing_policy": "active"}

    monkeypatch.setattr(routing_config, "get_config", cfg)
    assert await model_routing.global_policy() == "active"


async def test_global_policy_rejects_unknown_value(monkeypatch) -> None:
    async def cfg():
        return {"routing_policy": "banana"}

    monkeypatch.setattr(routing_config, "get_config", cfg)
    assert await model_routing.global_policy() == "off"


async def test_global_policy_fails_safe_on_error(monkeypatch) -> None:
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(routing_config, "get_config", boom)
    assert await model_routing.global_policy() == "off"


async def test_global_ladder_reads_config(monkeypatch) -> None:
    async def cfg():
        return {"model_order": ["Qwen3.8-27B", "GLM-5.3-Flash"]}

    monkeypatch.setattr(routing_config, "get_config", cfg)
    assert await model_routing.global_ladder() == ["Qwen3.8-27B", "GLM-5.3-Flash"]


async def test_global_ladder_empty_on_error(monkeypatch) -> None:
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(routing_config, "get_config", boom)
    assert await model_routing.global_ladder() == []


async def test_profile_off_inherits_global(monkeypatch) -> None:
    """A profile left at the 'off' default follows the fleet arming."""
    monkeypatch.setattr(model_routing, "db", _fake_db({"routing_policy": "off", "routing_min_model": ""}))

    async def cfg():
        return {"routing_policy": "active"}

    monkeypatch.setattr(routing_config, "get_config", cfg)
    policy, _min = await model_routing.profile_routing_config(uuid.uuid4())
    assert policy == "active"


async def test_profile_explicit_arm_wins_over_global(monkeypatch) -> None:
    """An explicitly armed profile keeps its own setting (early opt-in)."""
    monkeypatch.setattr(model_routing, "db", _fake_db({"routing_policy": "shadow", "routing_min_model": ""}))

    async def cfg():
        return {"routing_policy": "active"}

    monkeypatch.setattr(routing_config, "get_config", cfg)
    policy, _min = await model_routing.profile_routing_config(uuid.uuid4())
    assert policy == "shadow"


async def test_profile_lookup_failure_stays_off(monkeypatch) -> None:
    """A DB failure is fail-safe 'off' and must NOT read the global config

    (an unreadable profile must never arm routing)."""

    class _Boom:
        async def fetchrow(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(model_routing, "db", _Boom())
    called = []

    async def cfg():
        called.append(1)
        return {"routing_policy": "active"}

    monkeypatch.setattr(routing_config, "get_config", cfg)
    policy, _min = await model_routing.profile_routing_config(uuid.uuid4())
    assert policy == "off"
    assert called == [], "global config must not be consulted after a profile lookup failure"
