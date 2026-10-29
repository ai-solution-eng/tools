"""Capacity-aware routing (2026-10-28).

The operator requirement: "model_list should show, for a given api key, what
models still have token capacity at that moment." Two surfaces are pinned here:

  1. _routing_available -- the predicate whose survivors ARE {{model_list}} in
     the decision prompt and the window the heuristic walks. A model the key
     cannot afford right now must not be offered to the router (otherwise the
     router picks it and admission 429s the request).
  2. GET /v1/models -- the client-facing listing, which now carries
     pcai.capacity per entry.

Both FAIL OPEN: capacity is a filter, never a gate. A budget-table fault must
degrade to the previous behaviour, never to an empty ladder or an empty listing.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.routing.router import Endpoint, Pool


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 1000,
        "rate_limit_tpm": 1_000_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


@pytest.fixture()
def two_models(monkeypatch):
    monkeypatch.setattr(routes_proxy.model_router, "models", lambda: ["GLM-5.3-Flash", "Qwen3.8-27B"])

    def pool_for(m: str):
        # None for an unknown name, like the real registry: the predicate
        # must treat "not registered" as unavailable without consulting
        # capacity at all.
        if m not in ("GLM-5.3-Flash", "Qwen3.8-27B"):
            return None
        return Pool(endpoints=[Endpoint(id=f"{m}-1", model_name=m, base_url=f"http://{m}", model_type="chat")])

    monkeypatch.setattr(routes_proxy.model_router, "pool", pool_for)
    return ["GLM-5.3-Flash", "Qwen3.8-27B"]


def test_available_excludes_exhausted_model(two_models) -> None:
    """A model whose capacity says not-ok is NOT offered to the router."""
    cap = {
        "GLM-5.3-Flash": {"ok": True, "remaining_tokens": 500, "blocked_by": ""},
        "Qwen3.8-27B": {"ok": False, "remaining_tokens": 0, "blocked_by": "day token budget exhausted"},
    }
    ok = routes_proxy._routing_available(_key(), cap)
    assert ok("GLM-5.3-Flash") is True
    assert ok("Qwen3.8-27B") is False


def test_available_treats_unevaluated_model_as_available(two_models) -> None:
    """A model missing from the capacity map was not evaluated -> available.

    Capacity is a FILTER, never a gate: the admission gate still enforces, and
    a partial map must not silently shrink the ladder.
    """
    ok = routes_proxy._routing_available(_key(), {"Qwen3.8-27B": {"ok": False}})
    assert ok("GLM-5.3-Flash") is True
    assert ok("Qwen3.8-27B") is False


def test_available_without_capacity_is_unchanged(two_models) -> None:
    """No capacity argument (the pre-2026-10-28 call shape) behaves exactly as before."""
    ok = routes_proxy._routing_available(_key())
    assert ok("GLM-5.3-Flash") is True
    assert ok("Qwen3.8-27B") is True


def test_available_still_enforces_acl_and_health(two_models) -> None:
    """Capacity never overrides the two older bounds: ACL and endpoint health."""
    cap = {"Qwen3.8-27B": {"ok": True, "remaining_tokens": 10, "blocked_by": ""}}
    ok = routes_proxy._routing_available(_key(allowed_models=["GLM-5.3-Flash"]), cap)
    assert ok("Qwen3.8-27B") is False  # ACL wins over capacity
    assert ok("GLM-5.3-Flash") is True
    assert routes_proxy._routing_available(_key())("not-registered") is False


async def test_list_models_annotates_capacity(monkeypatch, two_models) -> None:
    """Every registered entry carries pcai.capacity for THIS key."""

    async def fake_capacity(key_id, models):
        assert list(models) == ["GLM-5.3-Flash", "Qwen3.8-27B"]
        return {
            "GLM-5.3-Flash": {"ok": True, "remaining_tokens": 1234, "blocked_by": ""},
            "Qwen3.8-27B": {
                "ok": False,
                "remaining_tokens": 0,
                "blocked_by": "day token budget 100 exhausted (spent 100)",
            },
        }

    monkeypatch.setattr(budgets_mod, "model_capacity", fake_capacity)

    async def no_order(key_id):
        return []

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", no_order)
    res = await routes_proxy.list_models(_key())
    by_id = {m["id"]: m for m in res["data"]}
    assert by_id["GLM-5.3-Flash"]["pcai"]["capacity"]["remaining_tokens"] == 1234
    assert by_id["Qwen3.8-27B"]["pcai"]["capacity"]["ok"] is False
    # exhausted models are ANNOTATED, not dropped -- the listing keeps a stable
    # shape so a drained budget is distinguishable from an ACL narrowing.
    assert set(by_id) == {"GLM-5.3-Flash", "Qwen3.8-27B"}


async def test_list_models_fails_open_on_capacity_fault(monkeypatch, two_models) -> None:
    """A capacity fault must not empty the listing."""

    async def boom(key_id, models):
        raise RuntimeError("database not connected")

    monkeypatch.setattr(budgets_mod, "model_capacity", boom)

    async def no_order(key_id):
        return []

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", no_order)
    res = await routes_proxy.list_models(_key())
    assert {m["id"] for m in res["data"]} == {"GLM-5.3-Flash", "Qwen3.8-27B"}
    assert all("capacity" not in m["pcai"] for m in res["data"])
