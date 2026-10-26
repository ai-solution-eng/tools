"""Complexity-driven model routing tests (pseudo-models routing / routing__X).

Unit-first coverage of the pure decision module (core.model_routing) — name
detection, the complexity score, the ladder mapping with floor/ceiling
clamps, and the fail-safe header/ledger shapers — then proxy integration
against a mocked upstream + ledger for every policy state: 'off' (byte-for-
byte legacy), 'shadow' (would-serve stamped, ceiling still served),
'active' (routed model served through the pinned machinery), the routing__X
hard boundary, the no-ladder rules, and the caller-aware /v1/models catalog.

The ledger rows are captured positionally from the settle() INSERT args:
(key_id, model, endpoint, prompt, completion, total, cached, cost, status,
latency, request_id, billing_fraction, guardrail_events, routing_policy,
routing_ceiling, routing_served, routing_reason) — indexes 13..16 are the
routing columns.
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
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_routing
from pcai_llm.routing.router import Endpoint, Router

AUTH = {"Authorization": "Bearer hllm-x"}
ORDER = ["llm-0", "llm-1", "llm-2"]  # best-first (llm-0 = the ceiling)
CHEAP_FIRST = ["llm-2", "llm-1", "llm-0"]  # the ladder the router walks


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


# ---------------------------------------------------------------------------
# pure: pseudo-model detection
# ---------------------------------------------------------------------------


def test_detect_bare() -> None:
    p = model_routing.detect_pseudo_model("routing")
    assert p is not None and p.kind == "bare" and p.target == ""


def test_detect_named() -> None:
    p = model_routing.detect_pseudo_model("routing__llm-1")
    assert p is not None and p.kind == "named" and p.target == "llm-1"


def test_detect_empty_suffix_not_pseudo() -> None:
    # bare `routing__` (no ceiling) is a plain unknown name — the alias path
    # decides its fate, routing never engages on it.
    assert model_routing.detect_pseudo_model("routing__") is None


def test_detect_plain_names_untouched() -> None:
    for name in ("llm-0", "auto", "routingx", "routing_x", "my-routing", ""):
        assert model_routing.detect_pseudo_model(name) is None


# ---------------------------------------------------------------------------
# pure: complexity score
# ---------------------------------------------------------------------------


def test_score_is_chars_over_four() -> None:
    body = {"messages": [{"role": "user", "content": "x" * 400}]}
    assert model_routing.complexity_score(body) == 100


def test_score_grows_with_prompt_size() -> None:
    small = model_routing.complexity_score({"messages": [{"role": "user", "content": "x" * 800}]})
    big = model_routing.complexity_score({"messages": [{"role": "user", "content": "x" * 8000}]})
    assert big > small


def test_score_system_prompt_counts() -> None:
    # the system prompt is just a message — RAG/agent system blocks are
    # usually the largest single chunk of the request. The delta is the
    # prompt term PLUS the second-message turn adder.
    with_sys = model_routing.complexity_score(
        {"messages": [{"role": "system", "content": "y" * 400}, {"role": "user", "content": "hi"}]}
    )
    without = model_routing.complexity_score({"messages": [{"role": "user", "content": "hi"}]})
    assert with_sys - without == 100 + settings.routing_turn_adder_tokens


def test_score_tools_add_bytes_term() -> None:
    base = model_routing.complexity_score({"messages": [{"role": "user", "content": "hi"}]})
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"x": "y" * 640}}}]

    with_tools = model_routing.complexity_score({"messages": [{"role": "user", "content": "hi"}], "tools": tools})
    assert with_tools > base
    # the byte term dominates: 64 bytes ≈ 1 score token
    assert with_tools - base >= 10


def test_score_turns_add() -> None:
    one = model_routing.complexity_score({"messages": [{"role": "user", "content": "hi"}]})
    three = model_routing.complexity_score(
        {
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "yo"},
                {"role": "user", "content": "go"},
            ]
        }
    )
    assert three - one == 2 * settings.routing_turn_adder_tokens


def test_score_max_tokens_capped_and_discounted() -> None:
    # agentic configs inflate max_tokens (262144 is a normal harness value
    # while completions run in the hundreds) — the term must saturate at the
    # cap, never scale 1:1 with the ask
    body = {"messages": [{"role": "user", "content": "hi"}]}
    huge = model_routing.complexity_score({**body, "max_tokens": 262144})
    at_cap = model_routing.complexity_score({**body, "max_tokens": 4096})
    assert huge == at_cap == 512
    assert model_routing.complexity_score({**body, "max_tokens": 0}) == model_routing.complexity_score(body)
    assert model_routing.complexity_score({**body, "max_tokens": 800}) == model_routing.complexity_score(body) + 100


# ---------------------------------------------------------------------------
# pure: the ladder mapping
# ---------------------------------------------------------------------------


def _pick(score: int, ceiling: str = "llm-0", floor: str = "llm-2", available: Any = None) -> Any:
    return model_routing.pick_serving_model(CHEAP_FIRST, ceiling, floor, score, available=available)


def test_small_request_serves_floor() -> None:
    r = _pick(10)
    assert r.model == "llm-2" and r.reason == "prompt_tokens<tier1"


def test_mid_request_serves_window_middle() -> None:
    r = _pick(settings.routing_tier1_tokens + 1)
    assert r.model == "llm-1" and r.reason == "prompt_tokens<tier2"


def test_huge_request_serves_ceiling() -> None:
    r = _pick(settings.routing_tier2_tokens + 1)
    assert r.model == "llm-0" and r.reason == "prompt_tokens>=tier2"


def test_no_floor_no_downgrade() -> None:
    assert _pick(10, floor="").model == "llm-0"


def test_unknown_floor_no_downgrade() -> None:
    assert _pick(10, floor="ghost").model == "llm-0"


def test_floor_above_ceiling_clamped() -> None:
    # a contradictory config (floor pricier than ceiling) must never make
    # the router serve ABOVE the ceiling — that rule is absolute
    assert _pick(10, ceiling="llm-1", floor="llm-0").model == "llm-1"


def test_ceiling_off_ladder_serves_ceiling() -> None:
    r = model_routing.pick_serving_model(CHEAP_FIRST, "ghost", "llm-2", 10)
    assert r.model == "ghost" and r.reason == "no_ladder"


def test_named_ceiling_caps_the_walk() -> None:
    # window [llm-2, llm-1]: even a tier-2 request stops at llm-1
    r = model_routing.pick_serving_model(CHEAP_FIRST, "llm-1", "llm-2", settings.routing_tier2_tokens + 1)
    assert r.model == "llm-1" and r.reason == "prompt_tokens>=tier2"


def test_unavailable_walks_up_within_window() -> None:
    healthy = {"llm-1", "llm-0"}
    r = _pick(10, available=lambda m: m in healthy)
    assert r.model == "llm-1"  # cheapest AVAILABLE model that still fits


def test_nothing_available_serves_ceiling() -> None:
    assert _pick(10, available=lambda m: False).model == "llm-0"


def test_available_floor_wins() -> None:
    r = _pick(10, available=lambda m: True)
    assert r.model == "llm-2"


# ---------------------------------------------------------------------------
# pure: cut-point clamp
# ---------------------------------------------------------------------------


def test_cut_points_monotonic_under_misconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "routing_tier1_tokens", 5000)
    monkeypatch.setattr(settings, "routing_tier2_tokens", 100)  # inverted
    t1, t2 = model_routing._cut_points()
    assert t1 == 5000 and t2 == 5001


# ---------------------------------------------------------------------------
# pure: header + ledger shapers
# ---------------------------------------------------------------------------


def test_header_shadow_shape() -> None:
    d = model_routing.RouteDecision(
        policy="shadow", engaged=True, ceiling="llm-0", would="llm-2", reason="prompt_tokens<tier1", stamp=True
    )
    assert model_routing.route_header(d) == "shadow ceiling=llm-0 would=llm-2 reason=prompt_tokens<tier1"


def test_header_active_reports_actual_model() -> None:
    d = model_routing.RouteDecision(
        policy="active", engaged=True, ceiling="llm-0", served="llm-2", reason="r", stamp=True
    )
    # the budget gates promoted the request after the router picked — the
    # header must not claim a model that did not serve
    assert model_routing.route_header(d, "llm-1") == "active ceiling=llm-0 served=llm-1 reason=r"
    assert model_routing.route_header(d, "") == "active ceiling=llm-0 served=llm-2 reason=r"


def test_header_master_off_reads_off() -> None:
    d = model_routing.RouteDecision(policy="active", engaged=False)
    assert model_routing.route_header(d) == "off"


def test_header_policy_off_is_absent() -> None:
    assert model_routing.route_header(None) is None
    assert model_routing.route_header(model_routing.RouteDecision(policy="off")) is None
    assert model_routing.route_header(model_routing.RouteDecision()) is None


def test_stamp_shadow_uses_would() -> None:
    d = model_routing.RouteDecision(
        policy="shadow", engaged=True, ceiling="llm-0", would="llm-2", reason="prompt_tokens<tier1", stamp=True
    )
    assert model_routing.ledger_stamp(d, "llm-0") == {
        "routing_policy": "shadow",
        "routing_ceiling": "llm-0",
        "routing_served": "llm-2",  # would-serve; the model column keeps the truth
        "routing_reason": "prompt_tokens<tier1",
        # no decision model answered -> empty, so the column default stands
        "routing_probabilities": "",
    }


def test_stamp_active_uses_actual() -> None:
    d = model_routing.RouteDecision(
        policy="active", engaged=True, ceiling="llm-0", served="llm-2", reason="r", stamp=True
    )
    # the gates may have promoted the request elsewhere after the pick —
    # the ledger records what really billed, never the router's intention
    assert model_routing.ledger_stamp(d, "llm-1")["routing_served"] == "llm-1"


def test_stamp_absent_when_no_routing() -> None:
    assert model_routing.ledger_stamp(None, "m") == {}
    assert model_routing.ledger_stamp(model_routing.RouteDecision(policy="active", stamp=False), "m") == {}


# ---------------------------------------------------------------------------
# pure: profile policy lookup (TTL cache + fail-safe)
# ---------------------------------------------------------------------------


class PolicyDB:
    def __init__(self, row: dict[str, Any] | None, fail: bool = False) -> None:
        self.row, self.fail = row, fail
        self.calls = 0

    async def fetchrow(self, sql: str, *args: Any):
        self.calls += 1
        if self.fail:
            raise RuntimeError("db down")
        return self.row


@pytest.fixture(autouse=True)
def _reset_routing_state():
    model_routing._policy_cache.clear()
    model_routing._floor_warned.clear()
    yield
    model_routing._policy_cache.clear()
    model_routing._floor_warned.clear()


async def test_policy_lookup_reads_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    db = PolicyDB({"routing_policy": "active", "routing_min_model": "llm-2"})
    monkeypatch.setattr(model_routing, "db", db)
    assert await model_routing.profile_routing_config(uuid.uuid4()) == ("active", "llm-2")


async def test_policy_lookup_fail_safe_off_and_uncached(monkeypatch: pytest.MonkeyPatch) -> None:
    db = PolicyDB(None, fail=True)
    monkeypatch.setattr(model_routing, "db", db)
    uid = uuid.uuid4()
    assert await model_routing.profile_routing_config(uid) == ("off", "")
    assert len(model_routing._policy_cache) == 0  # failures must not stick


async def test_policy_unknown_value_reads_off(monkeypatch: pytest.MonkeyPatch) -> None:
    db = PolicyDB({"routing_policy": "banana", "routing_min_model": "llm-2"})
    monkeypatch.setattr(model_routing, "db", db)
    assert await model_routing.profile_routing_config(uuid.uuid4()) == ("off", "llm-2")


async def test_policy_lookup_is_ttl_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    db = PolicyDB({"routing_policy": "shadow", "routing_min_model": ""})
    monkeypatch.setattr(model_routing, "db", db)
    uid = uuid.uuid4()
    await model_routing.profile_routing_config(uid)
    await model_routing.profile_routing_config(uid)
    assert db.calls == 1


# ---------------------------------------------------------------------------
# settle() backward compatibility + routing columns
# ---------------------------------------------------------------------------


class SettleFakeDB:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        self.inserts.append((sql, args))
        return "INSERT 0 1"


async def test_settle_existing_signature_writes_empty_routing_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SettleFakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)  # settle -> costs.compute reads model_costs
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 10, 5, "ok", 5, "r-1")
    sql, args = fake.inserts[-1]
    assert "routing_policy" in sql and len(args) == 18
    # the four routing_* columns plus routing_probabilities all default to ''
    assert args[13] == "" and args[14] == "" and args[15] == "" and args[16] == ""
    assert args[17] == "", "routing_probabilities defaults empty (no decision ran)"
    # pre-existing positional indexes are untouched
    assert args[1] == "m" and args[2] == "chat" and args[5] == 15 and args[11] == 1.0


async def test_settle_carries_routing_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = SettleFakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    await budgets_mod.settle(
        uuid.uuid4(),
        "llm-0",
        "chat",
        10,
        5,
        "ok",
        5,
        "r-2",
        routing_policy="shadow",
        routing_ceiling="llm-0",
        routing_served="llm-2",
        routing_reason="prompt_tokens<tier1",
    )
    _, args = fake.inserts[-1]
    assert args[13] == "shadow" and args[14] == "llm-0" and args[15] == "llm-2" and args[16] == "prompt_tokens<tier1"


# ---------------------------------------------------------------------------
# proxy integration (mocked upstream + ledger)
# ---------------------------------------------------------------------------


class RoutingFakeDB:
    """Profiles + rate-window + ledger stub. The routing-policy query and
    the cascade's model_order query are distinguished by their columns."""

    def __init__(self, order: list[str], policy: str, min_model: str) -> None:
        self.order, self.policy, self.min_model = order, policy, min_model
        self.rows: list[tuple] = []  # settle() INSERT arg tuples

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


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Build a TestClient with a configurable routing profile. Returns an
    installer: env(policy=..., min_model=..., order=..., allowed=...) →
    (client, fake_db, upstream_requests)."""

    def _install(
        policy: str = "off",
        min_model: str = "",
        order: list[str] | None = ORDER,
        allowed: list[str] | None = None,
    ):
        fake_db = RoutingFakeDB(order or [], policy, min_model)
        key = _key(allowed)

        import pcai_llm.api.auth as auth_mod
        import pcai_llm.core.cascade as cascade_mod
        import pcai_llm.core.model_routing as mr_mod

        async def fake_authenticate(plaintext: str) -> KeyRecord:
            return key

        monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
        monkeypatch.setattr(budgets_mod, "db", fake_db)
        monkeypatch.setattr(costs_mod, "db", fake_db)  # settle -> costs.compute
        monkeypatch.setattr(cascade_mod, "db", fake_db)
        monkeypatch.setattr(mr_mod, "db", fake_db)
        mr_mod._policy_cache.clear()
        mr_mod._floor_warned.clear()

        upstream_bodies: list[dict[str, Any]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content)
            upstream_bodies.append(body)
            if body.get("stream"):
                payload = (
                    b'data: {"id":"1","object":"chat.completion.chunk","choices":[{"index":0,'
                    b'"delta":{"content":"hi"},"finish_reason":null}]}\n\ndata: [DONE]\n\n'
                )
                return httpx2.Response(200, content=payload, headers={"content-type": "text/event-stream"})
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
        tc = TestClient(app, raise_server_exceptions=False)
        return tc, fake_db, upstream_bodies

    return _install


def _chat(tc: TestClient, model: str, content: str = "hello", **extra: Any):
    body: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": content}], **extra}
    return tc.post("/v1/chat/completions", json=body, headers=AUTH)


# --- policy 'off': byte-for-byte legacy ------------------------------------


def test_off_policy_routing_is_plain_alias(env) -> None:
    tc, db, upstream = env(policy="off")
    r = _chat(tc, "routing")
    assert r.status_code == 200, r.text
    # the alias walk serves model_order[0]; the upstream still received the
    # alias name (today's transparent-proxy behavior, untouched)
    assert upstream[0]["model"] == "routing"
    assert r.json()["model"] == "routing"
    assert "x-pcai-route" not in {k.lower() for k in r.headers}
    row = db.rows[-1]
    assert row[1] == "llm-0"  # ledger records what actually served
    assert row[13] == "" and row[14] == "" and row[15] == "" and row[16] == ""


def test_off_policy_routing_named_is_plain_alias(env) -> None:
    tc, _db, upstream = env(policy="off")
    r = _chat(tc, "routing__llm-1")
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == "routing__llm-1"  # alias: name rides through
    assert "x-pcai-route" not in {k.lower() for k in r.headers}


def test_direct_request_no_header_when_policy_off(env) -> None:
    tc, _db, _upstream = env(policy="off")
    r = _chat(tc, "llm-1")
    assert r.status_code == 200
    assert "x-pcai-route" not in {k.lower() for k in r.headers}


# --- policy 'shadow': serve as off, stamp would-serve -----------------------


def test_shadow_stamps_would_serve_keeps_truth(env) -> None:
    tc, db, upstream = env(policy="shadow", min_model="llm-2")
    r = _chat(tc, "routing")
    assert r.status_code == 200, r.text
    assert r.headers["x-pcai-route"] == "shadow ceiling=llm-0 would=llm-2 reason=prompt_tokens<tier1"
    # served EXACTLY as 'off' would: pseudo name rides through, alias walk
    # serves the ceiling model
    assert upstream[0]["model"] == "routing"
    row = db.rows[-1]
    assert row[1] == "llm-0"  # model column = actually-served (ledger truth)
    assert row[13] == "shadow"
    assert row[14] == "llm-0"
    assert row[15] == "llm-2"  # would-serve
    assert row[16] == "prompt_tokens<tier1"


def test_shadow_direct_request_header_only(env) -> None:
    tc, db, _upstream = env(policy="shadow", min_model="llm-2")
    r = _chat(tc, "llm-1")
    assert r.status_code == 200
    assert r.headers["x-pcai-route"] == "shadow ceiling=llm-1 would=llm-1 reason=direct"
    assert db.rows[-1][13] == ""  # direct requests never consulted the router


# --- policy 'active': serve the routed model --------------------------------


def test_active_serves_routed_cheap_model(env) -> None:
    tc, db, upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "routing")
    assert r.status_code == 200, r.text
    # the upstream sees (and echoes) the routed model, not the pseudo name
    assert upstream[0]["model"] == "llm-2"
    assert r.json()["model"] == "llm-2"
    assert r.headers["x-pcai-route"] == "active ceiling=llm-0 served=llm-2 reason=prompt_tokens<tier1"
    row = db.rows[-1]
    assert row[1] == "llm-2" and row[13] == "active" and row[14] == "llm-0" and row[15] == "llm-2"


def test_active_huge_request_serves_ceiling(env) -> None:
    tc, _db, upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "routing", content="x" * 26000)  # ~6500 est tokens >= tier2
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == "llm-0"
    assert "prompt_tokens>=tier2" in r.headers["x-pcai-route"]


def test_active_named_ceiling_respected(env) -> None:
    tc, _db, upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "routing__llm-1")
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == "llm-2"  # cheapest in window [llm-2, llm-1]
    assert r.headers["x-pcai-route"] == "active ceiling=llm-1 served=llm-2 reason=prompt_tokens<tier1"


def test_active_named_ceiling_caps_huge_requests(env) -> None:
    tc, _db, upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "routing__llm-1", content="x" * 26000)
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == "llm-1"  # NEVER above the named ceiling
    assert r.headers["x-pcai-route"] == "active ceiling=llm-1 served=llm-1 reason=prompt_tokens>=tier2"


def test_active_named_unregistered_is_404(env) -> None:
    tc, _db, _upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "routing__ghost")
    assert r.status_code == 404
    assert "not a registered model" in r.text


def test_active_named_not_allowed_is_403(env) -> None:
    tc, _db, _upstream = env(policy="active", min_model="llm-2", allowed=["llm-0"])
    r = _chat(tc, "routing__llm-1")
    assert r.status_code == 403
    assert "not allowed" in r.text


def test_active_walk_respects_acl_boundary(env) -> None:
    # allowed_models is a hard boundary inside the walk too: a key allowed
    # only llm-0 gets llm-0 even though the ladder starts cheaper
    tc, db, upstream = env(policy="active", min_model="llm-2", allowed=["llm-0"])
    r = _chat(tc, "routing")
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == "llm-0"
    assert db.rows[-1][1] == "llm-0"


def test_active_direct_request_header(env) -> None:
    tc, db, _upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "llm-1")
    assert r.status_code == 200
    assert r.headers["x-pcai-route"] == "active ceiling=llm-1 served=llm-1 reason=direct"
    assert db.rows[-1][13] == ""  # direct: header only, no ledger stamp


def test_active_streaming_header_and_stamp(env) -> None:
    tc, db, upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "routing", stream=True)
    assert r.status_code == 200, r.text
    # headers go out at stream start
    assert r.headers["x-pcai-route"] == "active ceiling=llm-0 served=llm-2 reason=prompt_tokens<tier1"
    assert upstream[0]["model"] == "llm-2"
    row = db.rows[-1]
    assert row[1] == "llm-2" and row[13] == "active" and row[15] == "llm-2"


def test_shadow_streaming_header(env) -> None:
    tc, db, _upstream = env(policy="shadow", min_model="llm-2")
    r = _chat(tc, "routing", stream=True)
    assert r.status_code == 200, r.text
    assert r.headers["x-pcai-route"] == "shadow ceiling=llm-0 would=llm-2 reason=prompt_tokens<tier1"
    row = db.rows[-1]
    assert row[1] == "llm-0" and row[15] == "llm-2"  # truth vs would-serve


# --- no-ladder rules (unprofiled key / empty model_order) --------------------


def test_unprofiled_bare_routing_404(env) -> None:
    tc, _db, _upstream = env(policy="off", order=[])
    r = _chat(tc, "routing")
    assert r.status_code == 404
    assert "routing requires a profile model ordering" in r.text


def test_unprofiled_named_serves_pinned(env) -> None:
    tc, db, upstream = env(policy="off", order=[])
    r = _chat(tc, "routing__llm-1")
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == "llm-1"  # pinned semantics, no ladder to walk
    assert r.json()["model"] == "llm-1"
    assert "x-pcai-route" not in {k.lower() for k in r.headers}  # no profile → no header
    assert db.rows[-1][1] == "llm-1"


def test_active_profile_empty_order_bare_routing_404(env) -> None:
    # even an opted-in profile cannot route without a ladder
    tc, _db, _upstream = env(policy="active", min_model="llm-2", order=[])
    r = _chat(tc, "routing")
    assert r.status_code == 404
    assert "routing requires a profile model ordering" in r.text


# --- fail-safes --------------------------------------------------------------


def test_master_switch_off_reads_off_and_serves_legacy(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "routing_enabled", False)
    tc, db, upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "routing")
    assert r.status_code == 200, r.text
    assert upstream[0]["model"] == "routing"  # alias behavior (as 'off')
    assert r.headers["x-pcai-route"] == "off"  # opted-in profile, switch down
    assert db.rows[-1][13] == ""  # nothing stamped: routing did not run


def test_router_error_degrades_to_legacy(env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("router exploded")

    monkeypatch.setattr(model_routing, "pick_serving_model", boom)
    tc, db, upstream = env(policy="active", min_model="llm-2")
    r = _chat(tc, "routing")
    assert r.status_code == 200, r.text  # never fails the request
    assert upstream[0]["model"] == "routing"  # legacy alias path
    assert db.rows[-1][1] == "llm-0"  # ...which serves the ceiling first
    assert "x-pcai-route" not in {k.lower() for k in r.headers}


def test_floor_without_min_model_warns_once_only(env, caplog: pytest.LogCaptureFixture) -> None:
    tc, _db, _upstream = env(policy="active", min_model="")  # no floor configured
    import logging

    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.model_routing"):
        assert _chat(tc, "routing").status_code == 200
        assert _chat(tc, "routing").status_code == 200
    warnings = [rec for rec in caplog.records if "routing_min_model" in rec.getMessage()]
    assert len(warnings) == 1  # one-time, not per request


# --- /v1/models caller-aware catalog -----------------------------------------


def test_models_lists_routing_entries_when_opted_in(env) -> None:
    tc, _db, _upstream = env(policy="active", min_model="llm-2")
    r = tc.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    data = r.json()["data"]
    ids = [m["id"] for m in data]
    assert "routing" in ids
    for m in ORDER:
        assert f"routing__{m}" in ids
    entry = next(m for m in data if m["id"] == "routing")
    assert entry["pcai"] == {"type": "routing"}
    named = next(m for m in data if m["id"] == "routing__llm-1")
    assert named["pcai"] == {"type": "routing"}


def test_models_lists_nothing_new_when_policy_off(env) -> None:
    tc, _db, _upstream = env(policy="off")
    r = tc.get("/v1/models", headers=AUTH)
    ids = [m["id"] for m in r.json()["data"]]
    assert "routing" not in ids
    assert not any(i.startswith("routing__") for i in ids)


def test_models_lists_nothing_new_when_master_off(env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "routing_enabled", False)
    tc, _db, _upstream = env(policy="active", min_model="llm-2")
    r = tc.get("/v1/models", headers=AUTH)
    ids = [m["id"] for m in r.json()["data"]]
    assert "routing" not in ids
