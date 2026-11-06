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


async def test_list_models_routing_entries_carry_windowed_capacity(monkeypatch, two_models) -> None:
    """A routing pseudo-model's capacity is its floor..ceiling WINDOW.

    ok = at least one rung can serve (the router walks the window); warn = the
    tightest rung's band; the candidate counts let a client say "1 of 2 rungs
    is out of budget". This is what lets a picker render "Unavailable to you"
    for a routing selection the key's budget cannot use at all.
    """

    async def cap(key_id, models):
        return {
            "GLM-5.3-Flash": {"ok": True, "remaining_tokens": 500, "blocked_by": "", "warn": 80},
            "Qwen3.8-27B": {"ok": False, "remaining_tokens": 0, "blocked_by": "day budget", "warn": 0},
        }

    monkeypatch.setattr(budgets_mod, "model_capacity", cap)

    async def order(key_id):
        return ["GLM-5.3-Flash", "Qwen3.8-27B"]

    async def policy(user_id):
        return ("active", "Qwen3.8-27B")  # floor = the cheap model

    async def enabled():
        return True

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", order)
    monkeypatch.setattr(routes_proxy.model_routing, "profile_routing_config", policy)
    monkeypatch.setattr(routes_proxy.model_routing, "global_enabled", enabled)
    monkeypatch.setattr(routes_proxy.settings, "routing_enabled", True)

    res = await routes_proxy.list_models(_key())
    by_id = {m["id"]: m for m in res["data"]}
    # Bare "routing" ceilings at order[0] (GLM); floor Qwen -> window [Qwen, GLM].
    bare = by_id["routing"]["pcai"]["capacity"]
    assert bare["ok"] is True
    assert bare["warn"] == 80
    assert bare["candidates"] == {"ok": 1, "blocked": 1}
    # routing__Qwen has a ONE-rung window (floor == ceiling) and that rung is
    # out of budget: the selection is unavailable to this key.
    qwen = by_id["routing__Qwen3.8-27B"]["pcai"]["capacity"]
    assert qwen["ok"] is False
    assert qwen["blocked_by"]
    assert qwen["candidates"] == {"ok": 0, "blocked": 1}
    # routing__GLM spans the same two-rung window, so it stays usable.
    assert by_id["routing__GLM-5.3-Flash"]["pcai"]["capacity"]["ok"] is True


async def test_list_models_routing_window_demotes_an_unticked_head(monkeypatch, two_models) -> None:
    """Bare "routing" ceilings at the most capable TICKED model (R2).

    The request path demotes an unticked ladder head to the most capable
    allowlisted model; the listing must use the SAME ceiling, or it counts a
    rung the router never picks.
    """
    from pcai_llm.core import routing_config

    async def cap(key_id, models):
        return {
            "GLM-5.3-Flash": {"ok": True, "remaining_tokens": 500, "blocked_by": "", "warn": 0},
            "Qwen3.8-27B": {"ok": False, "remaining_tokens": 0, "blocked_by": "day budget", "warn": 0},
        }

    async def order(key_id):
        return ["GLM-5.3-Flash", "Qwen3.8-27B"]

    async def policy(user_id):
        return ("active", "")  # no floor

    async def enabled():
        return True

    async def cfg():
        # GLM (the ladder head) is UNTICKED; only Qwen is routable.
        return {"allowed_models": ["Qwen3.8-27B"]}

    monkeypatch.setattr(budgets_mod, "model_capacity", cap)
    monkeypatch.setattr(routes_proxy, "profile_order_for_key", order)
    monkeypatch.setattr(routes_proxy.model_routing, "profile_routing_config", policy)
    monkeypatch.setattr(routes_proxy.model_routing, "global_enabled", enabled)
    monkeypatch.setattr(routes_proxy.settings, "routing_enabled", True)
    monkeypatch.setattr(routing_config, "get_config", cfg)

    res = await routes_proxy.list_models(_key())
    by_id = {m["id"]: m for m in res["data"]}
    bare = by_id["routing"]["pcai"]["capacity"]
    # One-rung window (Qwen only) and that rung is out of budget.
    assert bare["candidates"] == {"ok": 0, "blocked": 1}
    assert bare["ok"] is False


async def test_list_models_routing_window_includes_ticked_off_ladder_models(monkeypatch, two_models) -> None:
    """A ticked model ABSENT from model_order is a real rung (expand_candidates).

    The request path widens the ladder with allowlisted off-ladder models at
    the cheap end; a listing that only walked model_order would hide a rung the
    router can actually pick.
    """
    from pcai_llm.core import routing_config

    async def cap(key_id, models):
        return {
            "GLM-5.3-Flash": {"ok": True, "remaining_tokens": 500, "blocked_by": "", "warn": 0},
            "Qwen3.8-27B": {"ok": False, "remaining_tokens": 0, "blocked_by": "day budget", "warn": 0},
        }

    async def order(key_id):
        return ["GLM-5.3-Flash"]  # Qwen is NOT on the ladder

    async def policy(user_id):
        return ("active", "")

    async def enabled():
        return True

    async def cfg():
        return {"allowed_models": ["GLM-5.3-Flash", "Qwen3.8-27B"]}

    monkeypatch.setattr(budgets_mod, "model_capacity", cap)
    monkeypatch.setattr(routes_proxy, "profile_order_for_key", order)
    monkeypatch.setattr(routes_proxy.model_routing, "profile_routing_config", policy)
    monkeypatch.setattr(routes_proxy.model_routing, "global_enabled", enabled)
    monkeypatch.setattr(routes_proxy.settings, "routing_enabled", True)
    monkeypatch.setattr(routing_config, "get_config", cfg)

    res = await routes_proxy.list_models(_key())
    by_id = {m["id"]: m for m in res["data"]}
    bare = by_id["routing"]["pcai"]["capacity"]
    # Qwen (off-ladder, ticked) rides the cheap end; GLM is the ceiling.
    assert bare["candidates"] == {"ok": 1, "blocked": 1}
    assert bare["ok"] is True
