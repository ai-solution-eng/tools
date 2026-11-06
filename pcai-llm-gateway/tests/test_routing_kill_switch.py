"""The Routing panel's `enabled` kill switch must stop routing, not just the pill.

WHY THIS FILE EXISTS (P1-10, audit 2026-10-30): routing_config stores,
validates and DISPLAYS an `enabled` toggle (the panel's PAUSED pill) and its
own module header promises the runtime switch is AND-ed with
settings.routing_enabled. The request path gated only on the profile policy
and the ENV flag, so an operator pausing routing during an incident read
"PAUSED" while decision-model spend and downgrades continued. The AND the
panel renders (routes_admin.effective_enabled) and the AND the router honours
must be the SAME AND, or the console lies in exactly the moment an operator
needs it.

`enabled` is FLEET-level (routing_config is global), so it is read through a
cached accessor next to global_policy/global_min_model -- the same
TTL-cached routing_config.get_config() the policy/floor/ladder readers use,
never a second DB read on the hot path.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_routing, routing_config
from pcai_llm.routing.router import Endpoint, Router

ORDER = ["llm-0", "llm-1", "llm-2"]  # best-first: llm-0 is the ceiling
CHEAPEST = "llm-2"  # what an ENABLED router serves for a small prompt


def _config(**values: Any):
    """A routing_config.get_config stand-in (the merged-config shape)."""

    async def _cfg() -> dict[str, Any]:
        return {"routing_policy": "active", "routing_min_model": CHEAPEST, "enabled": True, **values}

    return _cfg


def _policy(policy: str, min_model: str = ""):
    """A profile_routing_config stand-in (the router's per-user seam)."""

    async def _inner(user_id: Any) -> tuple[str, str]:
        return policy, min_model

    return _inner


class _Key:
    user_id = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _fresh_caches():
    """Both caches are process-global: a stale one would hide the flag."""
    model_routing._policy_cache.clear()
    model_routing._floor_warned.clear()
    routing_config._cache = {}
    routing_config._cache_at = 0.0
    yield
    model_routing._policy_cache.clear()
    model_routing._floor_warned.clear()
    routing_config._cache = {}
    routing_config._cache_at = 0.0


async def _resolve(decide, requested: str):
    return await model_routing.resolve_routed_model(
        _Key(),
        {"messages": [{"role": "user", "content": "hi"}]},
        requested,
        set(ORDER),
        [],
        ORDER,
        available=None,
        decide=decide,
    )


# ---------------------------------------------------------------------------
# the accessor: the flag the panel writes, read the way the panel reads it
# ---------------------------------------------------------------------------


async def test_kill_switch_reads_the_stored_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routing_config, "get_config", _config(enabled=False))
    assert await model_routing.global_enabled() is False
    monkeypatch.setattr(routing_config, "get_config", _config(enabled=True))
    assert await model_routing.global_enabled() is True


async def test_kill_switch_defaults_to_enabled_when_the_row_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config that does not mention `enabled` (or a partial test fake) must
    not pause routing: the shipped default is enabled=True."""

    async def _cfg() -> dict[str, Any]:
        return {"routing_policy": "active"}

    monkeypatch.setattr(routing_config, "get_config", _cfg)
    assert await model_routing.global_enabled() is True


async def test_kill_switch_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable config must not take routing down — routing_config's own
    contract is degrade-not-fail (last known policy), and the shipped default
    is ON. A kill switch that failed CLOSED would turn a DB blip into a
    routing outage."""

    async def _boom() -> dict[str, Any]:
        raise RuntimeError("db down")

    monkeypatch.setattr(routing_config, "get_config", _boom)
    assert await model_routing.global_enabled() is True


# ---------------------------------------------------------------------------
# the request path: paused means "serve it the way 'off' serves it"
# ---------------------------------------------------------------------------


async def test_paused_routing_serves_the_pseudo_name_unengaged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The audit's exact repro: profile active + panel PAUSED.

    PRE-FIX the request was downgraded to the cheap model and a decision call
    was paid for, while the panel said PAUSED."""
    calls: list[int] = []

    async def decide(candidates: list[str], body: dict[str, Any], score: int):
        calls.append(1)
        return model_routing.DecisionOutcome(ok=True, choice=CHEAPEST, confidence=1.0, reason="decision")

    monkeypatch.setattr(model_routing, "profile_routing_config", _policy("active", CHEAPEST))
    monkeypatch.setattr(model_routing.settings, "routing_enabled", True)
    monkeypatch.setattr(routing_config, "get_config", _config(enabled=False))

    model, route = await _resolve(decide, "routing")
    assert model == "routing", "a paused router must leave the pseudo name to the plain alias walk"
    assert route.engaged is False
    assert route.policy == "active", "the panel state is still reported (it is what was paused)"
    assert model_routing.route_header(route) == "off"
    assert calls == [], "a paused router must never spend a decision-model call"


async def test_paused_routing_reports_off_on_a_direct_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned request on an armed profile carries the state header; when the
    panel has paused routing that header must read 'off' (engaged=False), not
    claim the profile is routing."""
    monkeypatch.setattr(model_routing, "profile_routing_config", _policy("active", CHEAPEST))
    monkeypatch.setattr(model_routing.settings, "routing_enabled", True)
    monkeypatch.setattr(routing_config, "get_config", _config(enabled=False))

    model, route = await _resolve(None, "llm-1")
    assert model == "llm-1"
    assert route.engaged is False
    assert model_routing.route_header(route) == "off"


async def test_enabled_routing_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: with the kill switch ON the router behaves exactly as
    before (downgrades and pays for the decision call)."""
    calls: list[int] = []

    async def decide(candidates: list[str], body: dict[str, Any], score: int):
        calls.append(1)
        return model_routing.DecisionOutcome(ok=True, choice=CHEAPEST, confidence=1.0, reason="decision")

    monkeypatch.setattr(model_routing, "profile_routing_config", _policy("active", CHEAPEST))
    monkeypatch.setattr(model_routing.settings, "routing_enabled", True)
    monkeypatch.setattr(routing_config, "get_config", _config(enabled=True))

    model, route = await _resolve(decide, "routing")
    assert model == CHEAPEST
    assert route.engaged is True
    assert model_routing.route_header(route) == f"active ceiling=llm-0 served={CHEAPEST} reason=decision:1.00"
    assert calls == [1]


async def test_env_switch_off_still_wins_and_skips_the_config_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """settings.routing_enabled=False short-circuits BEFORE the config read:
    the env flag owns the mount and must not pay for a config lookup."""
    reads: list[int] = []

    async def _cfg() -> dict[str, Any]:
        reads.append(1)
        return {"enabled": True}

    monkeypatch.setattr(model_routing, "profile_routing_config", _policy("active", CHEAPEST))
    monkeypatch.setattr(model_routing.settings, "routing_enabled", False)
    monkeypatch.setattr(routing_config, "get_config", _cfg)

    model, route = await _resolve(None, "routing")
    assert model == "routing" and route.engaged is False
    assert reads == [], "the env switch must short-circuit the kill-switch read"


# ---------------------------------------------------------------------------
# end to end through the proxy: the operator's pause is what SERVES
# ---------------------------------------------------------------------------


class _ProxyDB:
    """Profiles + rate-window + ledger stub (SQL-shape dispatch)."""

    def __init__(self, order: list[str], policy: str, min_model: str) -> None:
        self.order, self.policy, self.min_model = order, policy, min_model
        self.rows: list[tuple] = []

    async def fetch(self, sql: str, *args: Any):
        return []  # no budgets configured: every gate has headroom

    async def fetchrow(self, sql: str, *args: Any):
        if "routing_policy" in sql:
            return {"routing_policy": self.policy, "routing_min_model": self.min_model}
        if "profiles" in sql:
            return {"model_order": self.order} if self.order else None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def execute(self, sql: str, *args: Any):
        if "usage_ledger" in sql:
            self.rows.append(args)
        return "INSERT 0 1"


def _key() -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch):
    """A proxy TestClient whose routing_config carries the given kill switch."""

    def _install(*, enabled: bool):
        import pcai_llm.api.auth as auth_mod
        import pcai_llm.core.cascade as cascade_mod

        fake = _ProxyDB(ORDER, "active", CHEAPEST)
        key = _key()

        async def fake_authenticate(plaintext: str) -> KeyRecord:
            return key

        monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
        monkeypatch.setattr(budgets_mod, "db", fake)
        monkeypatch.setattr(costs_mod, "db", fake)  # settle -> costs.compute
        monkeypatch.setattr(cascade_mod, "db", fake)
        monkeypatch.setattr(model_routing, "db", fake)
        monkeypatch.setattr(routes_proxy, "db", fake)
        monkeypatch.setattr(model_routing.settings, "routing_enabled", True)
        monkeypatch.setattr(routing_config, "get_config", _config(enabled=enabled))
        model_routing._policy_cache.clear()
        model_routing._floor_warned.clear()

        upstream_bodies: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content)
            upstream_bodies.append(body)
            return httpx2.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "model": body.get("model"),  # echo: what the upstream saw
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
                },
            )

        mock_router = Router.with_mock(handler)
        for m in ORDER:
            mock_router.set_endpoints(m, [Endpoint(id=f"ep-{m}", model_name=m, base_url="http://fake-upstream/v1")])
        monkeypatch.setattr(routes_proxy, "model_router", mock_router)

        app = FastAPI()
        app.include_router(routes_proxy.router)
        return TestClient(app, raise_server_exceptions=False), fake, upstream_bodies

    return _install


def _chat(tc: TestClient, model: str) -> Any:
    return tc.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hello"}]},
        headers={"Authorization": "Bearer hllm-x"},
    )


def test_proxy_serves_the_alias_walk_when_the_panel_paused_routing(proxy_env) -> None:
    """PAUSED must be served exactly as 'off' is: the pseudo name stays an
    alias, the ceiling model serves, no routing stamp reaches the ledger and
    the header says 'off'."""
    tc, db, upstream = proxy_env(enabled=False)
    r = _chat(tc, "routing")
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == "llm-0", "a paused router must not downgrade"
    assert r.json()["model"] == "llm-0"
    assert r.headers["x-pcai-route"] == "off"
    row = db.rows[-1]
    assert row[1] == "llm-0"  # ledger records what actually served
    assert row[13] == "" and row[14] == "" and row[15] == "" and row[16] == ""


def test_proxy_downgrades_when_the_panel_left_routing_enabled(proxy_env) -> None:
    """The control: the same request with the kill switch ON still routes."""
    tc, db, upstream = proxy_env(enabled=True)
    r = _chat(tc, "routing")
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == CHEAPEST
    assert r.headers["x-pcai-route"].startswith(f"active ceiling=llm-0 served={CHEAPEST}")
    assert db.rows[-1][13] == "active"
