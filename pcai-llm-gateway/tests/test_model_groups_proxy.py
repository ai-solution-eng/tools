"""Model groups (F3) — proxy integration (routes_proxy + the chat path).

Exercised against the REAL core modules (core.model_groups,
core.load_snapshot, core.stickiness) with an in-memory database stand-in,
the pattern tests/test_proxy.py and tests/test_session_affinity.py use:

* a direct group request resolves to a member and serves it (X-PCAI-Group);
* the grp: pin keeps a conversation on its member when load would move it,
  and an invalid pin (member removed / whole pool dead) re-picks;
* the admission walk fails over INSIDE the group;
* a group in a per-model fallback chain resolves to a member;
* the key's allowed_models is never widened by a group (member grant AND
  group grant covered);
* 503 when no member qualifies; the feature is inert when disabled;
* /v1/models advertises groups (owned_by 'group') when enabled.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import canonical, load_snapshot, model_fallbacks, model_groups, model_routing, stickiness
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.routing.router import Endpoint, Router

ALPHA = "alpha"
BETA = "beta"
GAMMA = "gamma"
GROUP = "grp-chat"
AUTH = {"Authorization": "Bearer hllm-x"}
PROMPT = [{"role": "user", "content": "hello"}]


class FakeDB:
    """Budget/rate stubs PLUS an in-memory model_stickiness table.

    The sticky table answers the aged SELECT (model + age_s) and the
    upsert/delete the stickiness module issues; every other statement gets
    the test_proxy FakeDB behaviour (no profile, low window counters).
    """

    def __init__(self) -> None:
        self.sticky: dict[tuple[Any, str], tuple[str, float]] = {}
        self.settled: list[tuple] = []

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM model_stickiness" in sql:
            row = self.sticky.get((args[0], args[1]))
            if row is None:
                return None
            return {"model": row[0], "age_s": row[1]}
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def execute(self, sql: str, *args: Any):
        if "INSERT INTO model_stickiness" in sql:
            self.sticky[(args[0], args[1])] = (args[2], 0.0)
            return "OK"
        if "DELETE FROM model_stickiness" in sql:
            self.sticky.pop((args[0], args[1]), None)
            return "OK"
        self.settled.append((sql, args))
        return "INSERT 0 1"


def _key(allowed_models: list[str] | None = None) -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=allowed_models or [],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


@pytest.fixture
def make_env(monkeypatch: pytest.MonkeyPatch):
    """Factory: fake auth + fake db + mock router + an injected group map.

    Every seam the feature touches is the REAL one (model_groups.load /
    group_for / pick_member, load_snapshot.member_score, the stickiness
    helpers); only the group MAP and the load SCORES are injected, because
    those are the two facts the core modules own and a test must control.
    A caller-supplied scores dict is read LIVE, so a test can change the
    load between turns.
    """

    def _make(
        allowed: list[str] | None = None,
        members: tuple[str, ...] | None = (ALPHA, BETA),
        scores: dict[str, float] | None = None,
        fail: set[str] | None = None,
        group_enabled: bool = True,
        affinity_enabled: bool = False,
    ):
        key = _key(allowed)

        import pcai_llm.api.auth as auth_mod

        async def fake_authenticate(plaintext: str) -> KeyRecord:
            return key

        monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)

        fake_db = FakeDB()
        monkeypatch.setattr(budgets_mod, "db", fake_db)
        monkeypatch.setattr(cascade_mod, "db", fake_db)
        monkeypatch.setattr(costs_mod, "db", fake_db)
        monkeypatch.setattr(stickiness, "db", fake_db)

        seen: list[dict[str, Any]] = []
        failing = set(fail or ())

        def handler(request: httpx2.Request) -> httpx2.Response:
            payload = json.loads(request.content or b"{}")
            served = str(payload.get("model") or "")
            seen.append({"url": str(request.url), "model": served})
            if served in failing:
                return httpx2.Response(500, json={"error": "boom"})
            if payload.get("stream"):
                events = [
                    {"model": served, "choices": [{"index": 0, "delta": {"content": "hi"}}]},
                    {
                        "model": served,
                        "choices": [],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    },
                ]
                body = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
                body += b"data: [DONE]\n\n"
                return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})
            return httpx2.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "model": served,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
                },
            )

        mock_router = Router.with_mock(handler)
        for m in (ALPHA, BETA, GAMMA):
            mock_router.set_endpoints(m, [Endpoint(id="ep-" + m, model_name=m, base_url="http://fake-" + m + "/v1")])
        monkeypatch.setattr(routes_proxy, "model_router", mock_router)

        group_map: dict[str, Any] = {}
        if members is not None:
            group_map[GROUP] = model_groups.ModelGroup(
                name=GROUP, model_type="chat", policy="load", members=tuple(members)
            )

        async def fake_load(force: bool = False) -> dict[str, Any]:
            return dict(group_map)

        monkeypatch.setattr(model_groups, "load", fake_load)
        monkeypatch.setattr(model_groups, "snapshot", lambda: dict(group_map))
        monkeypatch.setattr(model_groups, "group_for", lambda name: group_map.get(name))

        score_map: dict[str, float] = scores if scores is not None else {}
        monkeypatch.setattr(load_snapshot, "member_score", lambda member: score_map.get(member))
        monkeypatch.setattr(settings, "model_groups_enabled", group_enabled)
        monkeypatch.setattr(settings, "session_affinity_enabled", affinity_enabled)

        app = FastAPI()
        app.include_router(routes_proxy.router)
        tc = TestClient(app, raise_server_exceptions=False)
        return tc, fake_db, mock_router, seen, key

    return _make


def _chat(
    tc: TestClient,
    model: str,
    messages: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
):
    return tc.post(
        "/v1/chat/completions",
        json={"model": model, "messages": messages or PROMPT},
        headers={**AUTH, **(headers or {})},
    )


def _served(seen: list[dict[str, Any]]) -> str:
    return seen[-1]["model"]


def _fp(key: KeyRecord, messages: list[dict[str, Any]] | None = None) -> str:
    fingerprint = canonical.thread_key(messages or PROMPT, key.id)
    assert fingerprint, "the conversation must have a fingerprint"
    return fingerprint


def _grp_pin(fake_db: FakeDB, key: KeyRecord, messages: list[dict[str, Any]] | None = None):
    return fake_db.sticky.get((key.id, stickiness.SCOPE_GROUP + _fp(key, messages)))


# ---------------------------------------------------------------------------
# 1. direct group request -> member
# ---------------------------------------------------------------------------


def test_direct_group_request_serves_a_member(make_env) -> None:
    tc, _, _, seen, _ = make_env(scores={ALPHA: 5.0, BETA: 0.0})
    r = _chat(tc, GROUP)
    assert r.status_code == 200, r.text
    assert _served(seen) == BETA, seen
    assert r.headers.get("x-pcai-group") == GROUP
    assert r.headers.get("x-pcai-served-model") == BETA
    assert seen[-1]["url"].startswith("http://fake-beta"), seen


def test_plain_model_request_has_no_group_header(make_env) -> None:
    tc, _, _, seen, _ = make_env()
    r = _chat(tc, ALPHA)
    assert r.status_code == 200, r.text
    assert _served(seen) == ALPHA
    assert "x-pcai-group" not in {k.lower() for k in r.headers}


# ---------------------------------------------------------------------------
# 2. grp: pin lifecycle
# ---------------------------------------------------------------------------


def test_grp_pin_keeps_the_conversation_on_its_member(make_env) -> None:
    scores = {ALPHA: 5.0, BETA: 0.0}
    tc, fake_db, _, seen, key = make_env(scores=scores)
    assert _chat(tc, GROUP).status_code == 200
    assert _served(seen) == BETA
    assert _grp_pin(fake_db, key) == (BETA, 0.0)

    # Flip the load: without the pin this request would move to ALPHA.
    scores.update({ALPHA: 0.0, BETA: 5.0})
    r2 = _chat(tc, GROUP)
    assert r2.status_code == 200, r2.text
    assert _served(seen) == BETA, "the grp: pin must outrank the load pick"


def test_invalid_grp_pin_repicks_and_rewrites(make_env) -> None:
    tc, fake_db, _, seen, key = make_env(members=(ALPHA,), scores={ALPHA: 0.0})
    # BETA left the group: the stored pin names a model the group no longer
    # contains.
    fake_db.sticky[(key.id, stickiness.SCOPE_GROUP + _fp(key))] = (BETA, 0.0)
    r = _chat(tc, GROUP)
    assert r.status_code == 200, r.text
    assert _served(seen) == ALPHA, "a member that left the group must be re-picked"
    assert _grp_pin(fake_db, key) == (ALPHA, 0.0), "the stale pin must be replaced"


def test_dead_member_pool_drops_the_grp_pin(make_env) -> None:
    tc, fake_db, mock_router, seen, key = make_env(scores={ALPHA: 0.0, BETA: 5.0})
    assert _chat(tc, GROUP).status_code == 200
    assert _served(seen) == ALPHA
    assert _grp_pin(fake_db, key) == (ALPHA, 0.0)
    # ALPHA's whole pool goes unhealthy: the pin cannot qualify any more.
    for ep in mock_router.pool(ALPHA).endpoints:
        ep.cooldown_until = time.monotonic() + 999
    r2 = _chat(tc, GROUP)
    assert r2.status_code == 200, r2.text
    assert _served(seen) == BETA
    assert _grp_pin(fake_db, key) == (BETA, 0.0)


# ---------------------------------------------------------------------------
# 3. admission failover stays inside the group
# ---------------------------------------------------------------------------


def test_admission_failover_stays_inside_the_group(make_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, fake_db, _, seen, key = make_env(scores={ALPHA: 0.0, BETA: 5.0})
    real_gates = routes_proxy._run_gates

    async def gates(key, model, est_tokens, endpoint, request_id, started, precomputed_budget=None):
        if model == ALPHA:
            raise HTTPException(429, "rate limited (test)")
        return await real_gates(key, model, est_tokens, endpoint, request_id, started, precomputed_budget)

    monkeypatch.setattr(routes_proxy, "_run_gates", gates)
    r = _chat(tc, GROUP)
    assert r.status_code == 200, r.text
    assert _served(seen) == BETA, "a 429 on one member must fail over to a sibling"
    assert r.headers.get("x-pcai-fallback") == ALPHA
    assert r.headers.get("x-pcai-group") == GROUP
    # The pin follows the member that actually served.
    assert _grp_pin(fake_db, key) == (BETA, 0.0)


# ---------------------------------------------------------------------------
# 4. a group in a per-model fallback chain
# ---------------------------------------------------------------------------


def test_fallback_chain_group_entry_resolves(make_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _, _, seen, _ = make_env(scores={ALPHA: 0.0, BETA: 5.0}, fail={ALPHA})

    async def chain_for(model_name: str) -> list[str]:
        return [GROUP] if model_name == ALPHA else []

    monkeypatch.setattr(model_fallbacks, "chain_for", chain_for)
    r = _chat(tc, ALPHA)
    assert r.status_code == 200, r.text
    assert _served(seen) == BETA, seen
    assert r.headers.get("x-pcai-fallback") == ALPHA


# ---------------------------------------------------------------------------
# 5. the ACL is never widened by a group
# ---------------------------------------------------------------------------


def test_member_grant_never_widens_the_acl(make_env) -> None:
    """A key allowed ONE member may use the group, and only that member."""
    scores = {ALPHA: 5.0, BETA: 0.0}  # load would prefer BETA
    tc, _, _, seen, _ = make_env(allowed=[ALPHA], scores=scores)
    r = _chat(tc, GROUP)
    assert r.status_code == 200, r.text
    assert _served(seen) == ALPHA, "BETA is outside the key's allowed_models"


def test_group_grant_makes_every_member_reachable(make_env) -> None:
    scores = {ALPHA: 0.0, BETA: 5.0}
    tc, _, _, seen, _ = make_env(allowed=[GROUP], scores=scores)
    assert _chat(tc, GROUP).status_code == 200
    assert _served(seen) == ALPHA
    scores.update({ALPHA: 5.0, BETA: 0.0})
    # A DIFFERENT conversation (different first user message -> no pin).
    other = [{"role": "user", "content": "another conversation"}]
    assert _chat(tc, GROUP, messages=other).status_code == 200
    assert _served(seen) == BETA, "naming the group grants its members"


def test_group_outside_the_acl_is_forbidden(make_env) -> None:
    tc, _, _, _, _ = make_env(allowed=[GAMMA], scores={ALPHA: 0.0, BETA: 0.0})
    r = _chat(tc, GROUP)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 6. capacity answers / feature off
# ---------------------------------------------------------------------------


def test_503_when_no_member_qualifies(make_env) -> None:
    tc, _, mock_router, _, _ = make_env(scores={ALPHA: 0.0, BETA: 0.0})
    for m in (ALPHA, BETA):
        for ep in mock_router.pool(m).endpoints:
            ep.cooldown_until = time.monotonic() + 999
    r = _chat(tc, GROUP)
    assert r.status_code == 503, r.text
    assert "no member of model group" in r.text


def test_group_is_inert_when_disabled(make_env) -> None:
    tc, fake_db, _, _, _ = make_env(group_enabled=False)
    r = _chat(tc, GROUP)
    assert r.status_code == 404, r.text  # not registered, no profile ordering
    assert fake_db.sticky == {}


# ---------------------------------------------------------------------------
# 7. endpoint affinity still works alongside groups
# ---------------------------------------------------------------------------


def test_endpoint_pin_still_works(make_env) -> None:
    tc, _, mock_router, seen, key = make_env(affinity_enabled=True, scores={ALPHA: 0.0})
    mock_router.set_endpoints(
        ALPHA,
        [
            Endpoint(id="ep-a1", model_name=ALPHA, base_url="http://fake-a1/v1", priority=0),
            Endpoint(id="ep-a2", model_name=ALPHA, base_url="http://fake-a2/v1", priority=1),
        ],
    )
    asyncio.run(stickiness.set_endpoint_pin(key.id, _fp(key), "ep-a2"))
    r = _chat(tc, ALPHA)
    assert r.status_code == 200, r.text
    assert seen[-1]["url"].startswith("http://fake-a2"), "the ep: pin must still be honoured"


# ---------------------------------------------------------------------------
# 8. streaming: a COMPLETED stream refreshes the pins
# ---------------------------------------------------------------------------


def test_streaming_completion_refreshes_the_endpoint_pin(make_env) -> None:
    """A streamed turn must not silently lose its endpoint pin.

    Before this the ep: pin was only refreshed on the non-streaming path, so
    a conversation that always streamed dropped its pin after the TTL and
    re-rolled its endpoint (a full re-prefill).
    """
    tc, fake_db, _, _, key = make_env(affinity_enabled=True, scores={ALPHA: 0.0})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": ALPHA, "messages": PROMPT, "stream": True},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert "data:" in r.text
    assert fake_db.sticky.get((key.id, stickiness.SCOPE_ENDPOINT + _fp(key))) == ("ep-alpha", 0.0)


def test_streaming_group_request_carries_the_group_and_pins_the_member(make_env) -> None:
    tc, fake_db, _, _, key = make_env(scores={ALPHA: 5.0, BETA: 0.0})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": GROUP, "messages": PROMPT, "stream": True},
        headers=AUTH,
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("x-pcai-group") == GROUP
    assert _grp_pin(fake_db, key) == (BETA, 0.0)


# ---------------------------------------------------------------------------
# 9. /v1/models
# ---------------------------------------------------------------------------


def test_models_listing_advertises_groups(make_env) -> None:
    tc, _, _, _, _ = make_env(scores={ALPHA: 0.0, BETA: 0.0})
    r = tc.get("/v1/models", headers=AUTH)
    assert r.status_code == 200, r.text
    by_id = {m["id"]: m for m in r.json()["data"]}
    assert GROUP in by_id, list(by_id)
    entry = by_id[GROUP]
    assert entry["owned_by"] == "group"
    assert entry["object"] == "model"
    assert entry["pcai"]["type"] == "group"
    assert entry["pcai"]["members"] == [ALPHA, BETA]


def test_models_listing_hides_groups_when_disabled(make_env) -> None:
    tc, _, _, _, _ = make_env(group_enabled=False)
    r = tc.get("/v1/models", headers=AUTH)
    assert r.status_code == 200, r.text
    assert GROUP not in {m["id"] for m in r.json()["data"]}


# ---------------------------------------------------------------------------
# 9. routing composability: a group is a routing candidate
# ---------------------------------------------------------------------------


def test_routing_available_accepts_a_group_with_one_healthy_member(make_env) -> None:
    _tc, _, _, _, key = make_env(scores={ALPHA: 0.0, BETA: 0.0})
    ok = routes_proxy._routing_available(key)
    assert ok("grp-chat") is True
    assert ok(ALPHA) is True


def test_routing_available_rejects_a_group_when_every_member_is_dead(make_env) -> None:
    _tc, _, mock_router, _, key = make_env()
    for member in (ALPHA, BETA):
        for ep in mock_router.pool(member).endpoints:
            ep.cooldown_until = time.monotonic() + 999
    assert routes_proxy._routing_available(key)("grp-chat") is False


def test_routing_available_uses_per_member_capacity(make_env) -> None:
    _tc, _, _, _, key = make_env()
    both_blocked = {ALPHA: {"ok": False}, BETA: {"ok": False}}
    one_free = {ALPHA: {"ok": False}, BETA: {"ok": True}}
    assert routes_proxy._routing_available(key, both_blocked)("grp-chat") is False
    assert routes_proxy._routing_available(key, one_free)("grp-chat") is True


def test_routing_available_respects_the_group_acl(make_env) -> None:
    _tc, _, _, _, key = make_env(allowed=[GAMMA])
    assert routes_proxy._routing_available(key)("grp-chat") is False
    _tc2, _, _, _, key2 = make_env(allowed=[ALPHA])
    assert routes_proxy._routing_available(key2)("grp-chat") is True


def test_routed_model_can_select_a_group_as_its_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """routing__<group> is a legal ceiling: the router returns the GROUP name
    and the normal admission path resolves its member (the direct-group path is
    already pinned by test_direct_group_request_serves_a_member)."""
    key = _key()

    async def _policy(user_id):
        return ("active", "")

    async def _global() -> bool:
        return True

    monkeypatch.setattr(model_routing, "profile_routing_config", _policy)
    monkeypatch.setattr(model_routing, "global_enabled", _global)
    monkeypatch.setattr(settings, "routing_enabled", True)
    model, route = asyncio.run(
        model_routing.resolve_routed_model(
            key,
            {"messages": PROMPT},
            f"routing__{GROUP}",
            {ALPHA, GROUP},
            [],
            [ALPHA, GROUP],
            available=lambda name: True,
        )
    )
    assert model == GROUP
    assert route.ceiling == GROUP


# ---------------------------------------------------------------------------
# 10. agents as routing candidates (per-key hard models)
# ---------------------------------------------------------------------------


def test_routing_available_accepts_an_offered_agent(make_env) -> None:
    _tc, _, mock_router, _, key = make_env()
    agent = types.SimpleNamespace(name="agent-a", model=ALPHA, offered=True)
    assert routes_proxy._routing_available(key, agents={"agent-a": agent})("agent-a") is True
    # not offered by the key's profile -> never a candidate
    hidden = types.SimpleNamespace(name="agent-a", model=ALPHA, offered=False)
    assert routes_proxy._routing_available(key, agents={"agent-a": hidden})("agent-a") is False
    # the pinned model's health decides
    for ep in mock_router.pool(ALPHA).endpoints:
        ep.cooldown_until = time.monotonic() + 999
    assert routes_proxy._routing_available(key, agents={"agent-a": agent})("agent-a") is False


def test_routed_model_can_select_a_per_key_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """extra_candidates carries the key's agents into the router window; a
    routing__<agent> ceiling resolves to the agent name (the agent block then
    maps it to its pinned model)."""
    key = _key()

    async def _policy(user_id):
        return ("active", "")

    async def _global() -> bool:
        return True

    monkeypatch.setattr(model_routing, "profile_routing_config", _policy)
    monkeypatch.setattr(model_routing, "global_enabled", _global)
    monkeypatch.setattr(settings, "routing_enabled", True)
    model, route = asyncio.run(
        model_routing.resolve_routed_model(
            key,
            {"messages": PROMPT},
            "routing__agent-a",
            {ALPHA, "agent-a"},
            [],
            [ALPHA],
            available=lambda name: True,
            extra_candidates=["agent-a"],
        )
    )
    assert model == "agent-a"
    assert route.ceiling == "agent-a"


def test_routing_active_serves_an_agents_pinned_model(make_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: an ACTIVE routing request may pick a per-key agent; the
    agent block then applies it and the request is served by its pinned model."""
    tc, _, _, seen, _key = make_env(scores={ALPHA: 0.0, BETA: 5.0})
    from pcai_llm.core import agents as agents_mod
    from pcai_llm.core import model_routing
    from pcai_llm.core import routing_config as rc

    agent = agents_mod.Agent(
        id=uuid.uuid4(),
        name="agent-a",
        description="narrow agent",
        servers=(),
        model=ALPHA,
        system_prompt="",
        enabled=True,
        offered=True,
    )

    async def _for_key(key_id):
        return [agent]

    monkeypatch.setattr(agents_mod, "for_key", _for_key)
    monkeypatch.setattr(settings, "routing_agents_enabled", True)

    async def _order(key_id):
        return [ALPHA]

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", _order)

    async def _policy(user_id):
        return ("active", "")

    monkeypatch.setattr(model_routing, "profile_routing_config", _policy)

    async def _global() -> bool:
        return True

    monkeypatch.setattr(model_routing, "global_enabled", _global)
    monkeypatch.setattr(settings, "routing_enabled", True)

    async def _cfg() -> dict:
        return {
            "route_default_model": False,
            "allowed_models": [],
            "inject_gpu_load": False,
            "max_prompt_chars": 8000,
        }

    monkeypatch.setattr(rc, "get_config", _cfg)

    async def _decision_settings() -> dict:
        return {"mode": "off", "model": ""}

    monkeypatch.setattr(rc, "decision_settings", _decision_settings)

    r = _chat(tc, "routing__agent-a")
    assert r.status_code == 200, r.text
    assert _served(seen) == ALPHA, seen


def test_routing_active_serves_a_group_member(make_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end: an ACTIVE routing request with a group ceiling resolves the
    group, then the normal admission path picks its least-loaded member and
    serves it. The client names ONE rung (the group), not its members."""
    tc, _, _, seen, _key = make_env(scores={ALPHA: 5.0, BETA: 0.0})
    from pcai_llm.core import model_routing
    from pcai_llm.core import routing_config as rc

    async def _order(key_id):
        return [ALPHA, GROUP]

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", _order)

    async def _policy(user_id):
        return ("active", "")

    monkeypatch.setattr(model_routing, "profile_routing_config", _policy)

    async def _global() -> bool:
        return True

    monkeypatch.setattr(model_routing, "global_enabled", _global)
    monkeypatch.setattr(settings, "routing_enabled", True)

    async def _cfg() -> dict:
        return {
            "route_default_model": False,
            "allowed_models": [GROUP],
            "inject_gpu_load": False,
            "max_prompt_chars": 8000,
        }

    monkeypatch.setattr(rc, "get_config", _cfg)

    async def _decision_settings() -> dict:
        return {"mode": "off", "model": ""}

    monkeypatch.setattr(rc, "decision_settings", _decision_settings)

    r = _chat(tc, f"routing__{GROUP}")
    assert r.status_code == 200, r.text
    assert r.headers.get("x-pcai-group") == GROUP
    assert _served(seen) == BETA, seen  # the least-loaded member
    assert seen[-1]["url"].startswith("http://fake-beta"), seen
