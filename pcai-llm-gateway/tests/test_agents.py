"""Agent profiles (2026-10-29) — named tool surfaces exposed as model endpoints.

WHAT THIS FILE PINS
-------------------
1. The PURE seams: an agent can only ever HIDE tools (narrow_grants /
   narrow_catalog), the resolver matches model ids EXACTLY, and a hand-edited
   NULL servers column reads as a chat-only agent (hide, never reveal).
2. The ADMIN contract: a name that shadows a model or a reserved alias, a
   server that does not exist, and a pinned model that cannot serve chat are all
   refused at save time — where the operator can see why — instead of being
   stored as config that silently does nothing.
3. The REQUEST path: naming an agent selects it; naming one the key's profile
   does not offer is a 403 (never a silent cascade with the profile's full
   surface — the widening this feature exists to prevent); an unpinned agent
   rides the SAME cascade 'auto' does; and GET /v1/models advertises exactly the
   agents the request path would accept.

Mock-upstream discipline mirrors test_proxy.py: in-process Router + mock
transport, no live Postgres.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import agents as agents_mod
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import toolrelay
from pcai_llm.routing.router import Endpoint, Router

MODEL = "test-model"
ADMIN_H = {"Authorization": "Bearer admin-secret"}
KEY_H = {"Authorization": "Bearer hllm-x"}

# Upstream request bodies captured by the proxy fixture's mock transport.
_SEEN: list[str] = []


def _agent(**kw: Any) -> agents_mod.Agent:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "Admin-Agent",
        "description": "cluster sweeps",
        "servers": ("k8s-ops", "prometheus"),
        "model": "",
        "system_prompt": "",
        "enabled": True,
        "offered": True,
    }
    base.update(kw)
    return agents_mod.Agent(**base)


# ---------------------------------------------------------------------------
# pure seams
# ---------------------------------------------------------------------------


def test_pick_matches_model_ids_exactly() -> None:
    """Agent ids ARE model ids: this fleet's ids are case-sensitive strings
    (GLM-5.3-Flash, Qwen3.8-27B), so a near-miss must not resolve — it falls
    through to the alias path, which is what any unregistered name does today."""
    a = _agent()
    assert agents_mod.pick([a], "Admin-Agent") is a
    assert agents_mod.pick([a], "admin-agent") is None
    assert agents_mod.pick([a], "Admin-Agent ") is None
    assert agents_mod.pick([], "Admin-Agent") is None


def test_narrow_grants_only_ever_hides() -> None:
    grants = {("k8s-ops", "get_pods"), ("searxng", "search")}
    assert agents_mod.narrow_grants(grants, ("k8s-ops",)) == {("k8s-ops", "get_pods")}
    # no agent -> unchanged (the pre-feature behaviour, byte for byte)
    assert agents_mod.narrow_grants(grants, None) == grants
    # an agent naming a server the key does not hold grants NOTHING back
    assert agents_mod.narrow_grants(grants, ("applygate",)) == set()
    # an empty agent is a chat-only agent, not an error
    assert agents_mod.narrow_grants(grants, ()) == set()


def test_narrow_catalog_keeps_the_directory_honest() -> None:
    catalog = [
        {"server": "k8s-ops", "tool": "get_pods"},
        {"server": "searxng", "tool": "search"},
    ]
    out = agents_mod.narrow_catalog(catalog, ("searxng",))
    assert [t["tool"] for t in out] == ["search"]
    assert agents_mod.narrow_catalog(catalog, None) is catalog


def test_row_mapping_reads_null_servers_as_chat_only() -> None:
    """A hand-edited row with servers NULL must HIDE tools, not reveal them."""
    a = agents_mod._row_to_agent(
        {
            "id": uuid.uuid4(),
            "name": "x",
            "description": None,
            "servers": None,
            "model": None,
            "system_prompt": None,
            "enabled": True,
            "offered": False,
        }
    )
    assert a.servers == () and a.model == "" and a.system_prompt == ""
    assert a.description == "" and a.offered is False


async def test_for_key_raises_on_a_registry_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CALLER decides the fault posture (the request path fails open, the
    listing fails soft) — so the seam must not swallow it into an empty list,
    which would be indistinguishable from "this key has no agents"."""

    class _Boom:
        async def fetch(self, sql: str, *a: Any):
            raise RuntimeError("db down")

    monkeypatch.setattr(agents_mod, "db", _Boom())
    with pytest.raises(agents_mod.AgentRegistryUnavailable):
        await agents_mod.for_key(uuid.uuid4())


# ---------------------------------------------------------------------------
# admin: validation refuses config that could never work
# ---------------------------------------------------------------------------


class _AdminDB:
    """Just enough for the agent endpoints: the upsert RETURNING, the join, and
    the profile-agent write."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.agent_rows = rows or []
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any):
        if "INSERT INTO profiles" in sql:
            return {
                "id": uuid.uuid4(),
                "name": args[0],
                "model_order": args[1],
                "defaults_json": args[2],
                "offpeak_fraction": args[3],
                "routing_policy": args[4],
                "routing_min_model": args[5],
            }
        if "INSERT INTO agent_profiles" in sql:
            return {
                "id": uuid.uuid4(),
                "name": args[0],
                "description": args[1],
                "servers": list(args[2]),
                "model": args[3],
                "system_prompt": args[4],
                "enabled": args[5],
            }
        if "DELETE FROM agent_profiles" in sql:
            return {"id": uuid.uuid4()} if args[0] == "Admin-Agent" else None
        return None

    async def fetch(self, sql: str, *args: Any):
        if "profile_agents" in sql and "JOIN profiles" in sql:
            return [{"agent_id": "a1", "name": "user"}]
        if "FROM agent_profiles WHERE name = ANY" in sql:
            # the profile->agents write resolves NAMES to ids, 400s on a miss
            return [{"id": uuid.uuid4(), "name": n} for n in (args[0] or [])]
        return []

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        return "OK"

    async def fetchval(self, sql: str, *args: Any):
        return 1


class _Registry:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    async def mcp_servers(self) -> list[dict[str, Any]]:
        return [{"name": n} for n in self.names]


def _router() -> Router:
    r = Router()
    r.set_endpoints(MODEL, [Endpoint(id="e1", model_name=MODEL, base_url="http://a/v1", model_type="chat")])
    r.set_endpoints("emb", [Endpoint(id="e2", model_name="emb", base_url="http://b/v1", model_type="embedding")])
    return r


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret")
    db = _AdminDB()
    monkeypatch.setattr(routes_admin, "db", db)
    monkeypatch.setattr(routes_admin, "model_router", _router())
    monkeypatch.setattr(routes_admin, "registry", _Registry(["k8s-ops", "prometheus", "searxng"]))
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), db


def _body(**kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "Admin-Agent", "servers": ["k8s-ops"]}
    body.update(kw)
    return body


def test_agent_upsert_happy_path(admin_env) -> None:
    tc, _ = admin_env
    r = tc.post("/admin/agents", json=_body(description="sweeps", model=MODEL), headers=ADMIN_H)
    assert r.status_code == 201, r.text
    assert r.json()["servers"] == ["k8s-ops"] and r.json()["model"] == MODEL


def test_agent_name_cannot_shadow_a_model(admin_env) -> None:
    """A registered model of that name wins every resolution, so the agent would
    be advertised and unreachable — refuse it at save time."""
    tc, _ = admin_env
    r = tc.post("/admin/agents", json=_body(name=MODEL), headers=ADMIN_H)
    assert r.status_code == 400 and "already a registered model" in r.text


def test_agent_name_cannot_take_a_reserved_alias(admin_env) -> None:
    tc, _ = admin_env
    for reserved in ("auto", "auto-demand-aware", "routing", "routing__x"):
        r = tc.post("/admin/agents", json=_body(name=reserved), headers=ADMIN_H)
        assert r.status_code == 400, reserved


def test_agent_servers_must_exist(admin_env) -> None:
    """A server name that is not registered would resolve to a narrower surface
    than the operator wrote — silently. Fail loud instead."""
    tc, _ = admin_env
    r = tc.post("/admin/agents", json=_body(servers=["k8s-ops", "ghost-server"]), headers=ADMIN_H)
    assert r.status_code == 400 and "ghost-server" in r.text


def test_agent_model_must_serve_chat(admin_env) -> None:
    tc, _ = admin_env
    r = tc.post("/admin/agents", json=_body(model="emb"), headers=ADMIN_H)
    assert r.status_code == 400 and "not chat" in r.text
    r2 = tc.post("/admin/agents", json=_body(model="ghost-model"), headers=ADMIN_H)
    assert r2.status_code == 400 and "not a registered model" in r2.text


def test_agent_delete_404_for_unknown(admin_env) -> None:
    tc, _ = admin_env
    assert tc.delete("/admin/agents/nope", headers=ADMIN_H).status_code == 404
    assert tc.delete("/admin/agents/Admin-Agent", headers=ADMIN_H).status_code == 204


# ---------------------------------------------------------------------------
# request path: selection, the 403, and the listing
# ---------------------------------------------------------------------------


class _ProxyDB:
    """No budgets; a profile ordering so an UNPINNED agent can resolve."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return {"model_order": [MODEL]}
        return {"requests": 1, "tokens": 10}

    async def execute(self, sql: str, *a: Any):
        self.settled.append(a)
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *a: Any):
        return None


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch):
    def _install(agents: list[agents_mod.Agent] | None = None):
        _SEEN.clear()
        key = KeyRecord(
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

        import pcai_llm.api.auth as auth_mod
        import pcai_llm.core.cascade as cascade_mod
        import pcai_llm.core.costs as costs_mod

        async def fake_authenticate(plaintext: str) -> KeyRecord:
            return key

        monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
        fake_db = _ProxyDB()
        monkeypatch.setattr(routes_proxy, "db", fake_db)
        monkeypatch.setattr(budgets_mod, "db", fake_db)
        monkeypatch.setattr(cascade_mod, "db", fake_db)
        monkeypatch.setattr(costs_mod, "db", fake_db)

        class _AgentDB:
            async def fetch(self, sql: str, *a: Any):
                return [
                    {
                        "id": a_.id,
                        "name": a_.name,
                        "description": a_.description,
                        "servers": list(a_.servers),
                        "model": a_.model,
                        "system_prompt": a_.system_prompt,
                        "enabled": a_.enabled,
                        "offered": a_.offered,
                    }
                    for a_ in (agents or [])
                ]

        monkeypatch.setattr(agents_mod, "db", _AgentDB())

        def handler(request: httpx2.Request) -> httpx2.Response:
            # Every upstream call this turn made — the prompt-injection tests
            # assert on what the model was ACTUALLY sent, not on our bookkeeping.
            _SEEN.append(request.content.decode())
            return httpx2.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "model": MODEL,
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
                },
            )

        r = Router.with_mock(handler)
        r.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake-upstream/v1")])
        # An EMBEDDING pool: the listing must not advertise an agent pinned to a
        # model that cannot serve chat (it would be a guaranteed 400).
        r.set_endpoints("emb", [Endpoint(id="ep2", model_name="emb", base_url="http://fake-emb/v1", model_type="embedding")])
        monkeypatch.setattr(routes_proxy, "model_router", r)
        # ONE router for both handles: production imports the SAME registry
        # singleton in routes_proxy and core.cascade, and core.cascade.serves_chat
        # (the chat-type predicate the listing and the cascade walk both use)
        # reads cascade's handle.
        monkeypatch.setattr(cascade_mod, "model_router", r)
        app = FastAPI()
        app.include_router(routes_proxy.router)
        return TestClient(app, raise_server_exceptions=False)

    return _install


def _chat(tc: TestClient, model: str):
    return tc.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "x"}]},
        headers=KEY_H,
    )


def test_offered_agent_is_selectable_and_rides_the_cascade(proxy_env) -> None:
    """An UNPINNED agent is 'auto' with a narrower tool surface: it resolves
    through the profile ordering, and the response reports the model that
    actually served (same contract the cascade alias has always had)."""
    tc = proxy_env([_agent()])
    r = _chat(tc, "Admin-Agent")
    assert r.status_code == 200, r.text
    assert r.json()["model"] == MODEL


def test_pinned_agent_serves_its_model(proxy_env) -> None:
    tc = proxy_env([_agent(model=MODEL)])
    r = _chat(tc, "Admin-Agent")
    assert r.status_code == 200, r.text
    assert r.json()["model"] == MODEL


def test_agent_not_offered_is_403_never_a_silent_cascade(proxy_env) -> None:
    """THE WIDENING GUARD: an agent this key's profile does not offer must be
    refused. Falling through to the generic alias path would serve the request
    with the profile's FULL tool surface — the exact opposite of what the caller
    asked for."""
    tc = proxy_env([_agent(offered=False)])
    r = _chat(tc, "Admin-Agent")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["type"] == "agent_not_offered"


def test_unknown_name_still_takes_the_alias_path(proxy_env) -> None:
    """Backward compatibility: a name that is NOT an agent keeps today's
    behaviour (cascade through the profile ordering, not a 403)."""
    tc = proxy_env([])
    assert _chat(tc, "some-client-alias").status_code == 200


def test_models_lists_offered_agents_only(proxy_env) -> None:
    tc = proxy_env([_agent(), _agent(name="Hidden-Agent", offered=False)])
    body = tc.get("/v1/models", headers=KEY_H).json()
    ids = [m["id"] for m in body["data"]]
    assert "Admin-Agent" in ids and "Hidden-Agent" not in ids
    entry = next(m for m in body["data"] if m["id"] == "Admin-Agent")
    assert entry["object"] == "model"
    assert entry["pcai"]["type"] == "agent"
    assert entry["pcai"]["servers"] == ["k8s-ops", "prometheus"]
    assert entry["pcai"]["model"] == "auto"


def test_models_hides_an_agent_pinned_to_a_forbidden_model(proxy_env) -> None:
    """Advertising an agent whose pinned model the key may not call would be a
    guaranteed 403 at send time."""
    tc = proxy_env([_agent(model="emb")])
    ids = [m["id"] for m in tc.get("/v1/models", headers=KEY_H).json()["data"]]
    assert "Admin-Agent" not in ids
    assert MODEL in ids  # the real pools are still advertised

# ---------------------------------------------------------------------------
# system prompt: prepended per CALL, never into the conversation of record
# ---------------------------------------------------------------------------


def test_with_system_prefix_lands_after_a_client_system_message() -> None:
    """The client's framing owns the tool protocol and stays FIRST; the agent's
    role text lands before the conversation. Two system messages, not a merge —
    the client's text stays byte-intact for its own hashing and prefix cache."""
    msgs = [{"role": "system", "content": "client framing"}, {"role": "user", "content": "hi"}]
    out = toolrelay.with_system_prefix(msgs, "agent role")
    assert [m["role"] for m in out] == ["system", "system", "user"]
    assert out[0]["content"] == "client framing" and out[1]["content"] == "agent role"
    assert msgs == [{"role": "system", "content": "client framing"}, {"role": "user", "content": "hi"}], (
        "the input list must never be mutated — it is the handback boundary"
    )


def test_with_system_prefix_leads_when_the_client_sent_none() -> None:
    out = toolrelay.with_system_prefix([{"role": "user", "content": "hi"}], "agent role")
    assert out[0] == {"role": "system", "content": "agent role"} and out[1]["role"] == "user"


def test_with_system_prefix_is_a_no_op_when_empty() -> None:
    """No agent (or an agent with no prompt) must be byte-for-byte today's
    request — same list object, not a copy."""
    msgs = [{"role": "user", "content": "hi"}]
    assert toolrelay.with_system_prefix(msgs, "") is msgs


def test_agent_prompt_rides_the_upstream_call(proxy_env) -> None:
    """The prompt reaches the WIRE — asserted on the captured request body, not
    on our own bookkeeping."""
    tc = proxy_env([_agent(system_prompt="You are the cluster sweep agent.")])
    assert _chat(tc, "Admin-Agent").status_code == 200
    sent = json.loads(_SEEN[-1])
    assert sent["messages"][0] == {"role": "system", "content": "You are the cluster sweep agent."}


def test_agent_prompt_follows_the_client_system_message(proxy_env) -> None:
    tc = proxy_env([_agent(system_prompt="agent role")])
    r = tc.post(
        "/v1/chat/completions",
        json={
            "model": "Admin-Agent",
            "messages": [
                {"role": "system", "content": "client framing"},
                {"role": "user", "content": "x"},
            ],
        },
        headers=KEY_H,
    )
    assert r.status_code == 200, r.text
    sent = json.loads(_SEEN[-1])
    # 2026-10-29: the agent prompt still FOLLOWS the client's framing, but the
    # two system messages are merged into one before the body leaves the
    # gateway (core.wire_compat). SGLang's Qwen templates reject a second
    # system message with 400 "System message must be at the beginning", so an
    # agent served by a Qwen-routed model would fail every turn otherwise.
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]
    assert sent["messages"][0]["content"] == "client framing\n\nagent role"
    assert sent["messages"][1]["content"] == "x"


# ---------------------------------------------------------------------------
# near miss: a case-only difference is a typo, not an alias request
# ---------------------------------------------------------------------------


def test_near_miss_matches_case_only() -> None:
    a = _agent()
    assert agents_mod.near_miss([a], "admin-agent") is a
    assert agents_mod.near_miss([a], "ADMIN-AGENT") is a
    # an exact match is NOT a near miss (pick() owns that), and a real
    # difference is not either — it is an ordinary alias request.
    assert agents_mod.near_miss([a], "Admin-Agent") is None
    assert agents_mod.near_miss([a], "admin_agent") is None
    assert agents_mod.near_miss([a], "other") is None


def test_agent_refuses_a_client_requested_server_it_excludes(proxy_env) -> None:
    """A request that names an agent AND explicitly asks for a server the agent
    does not include is self-contradictory: serve it either way and someone is
    lied to (the model gets a surface it was told it had, or the client gets one
    it asked not to have). The check runs BEFORE either tool path — the
    approval-passthrough branch used to narrow silently and could ship an empty
    tools array upstream (cross-validation 2026-10-29)."""
    tc = proxy_env([_agent(servers=("searxng",))])
    r = tc.post(
        "/v1/chat/completions",
        json={
            "model": "Admin-Agent",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "mcp", "server": "k8s-ops"}],
        },
        headers=KEY_H,
    )
    assert r.status_code == 400, r.text
    assert "does not include server" in r.text and "k8s-ops" in r.text


def test_case_typo_is_refused_by_name(proxy_env) -> None:
    """A lowercase id would otherwise fall through to the generic alias path and
    be served the profile's FULL surface — the widening an agent exists to
    prevent. Refuse it and name the intended agent instead."""
    tc = proxy_env([_agent()])
    r = _chat(tc, "admin-agent")
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert detail["type"] == "agent_name_case"
    assert detail["did_you_mean"] == "Admin-Agent"


def test_no_agent_means_no_injected_message(proxy_env) -> None:
    """An ordinary request must be byte-identical to the pre-feature shape."""
    tc = proxy_env([])
    assert _chat(tc, MODEL).status_code == 200
    sent = json.loads(_SEEN[-1])
    assert all(m["role"] != "system" for m in sent["messages"])


# ---------------------------------------------------------------------------
# profile <-> agent selection: three-state, so an older console cannot wipe it
# ---------------------------------------------------------------------------


def test_profile_save_binds_agents(admin_env) -> None:
    tc, db = admin_env
    r = tc.post("/admin/profiles", json={"name": "user", "agents": ["Admin-Agent"]}, headers=ADMIN_H)
    assert r.status_code == 201, r.text
    assert r.json()["agents"] == ["Admin-Agent"]
    assert any("INSERT INTO profile_agents" in sql for sql, _ in db.executed)


def test_profile_save_refuses_an_undefined_agent(admin_env) -> None:
    """A profile offering a non-existent agent would read as configured while
    offering nothing — the stale-model_order defect class this endpoint already
    refuses for models."""
    tc, _ = admin_env

    class _MissDB(_AdminDB):
        async def fetch(self, sql: str, *args: Any):
            if "FROM agent_profiles WHERE name = ANY" in sql:
                return []
            return await super().fetch(sql, *args)

    routes_admin.db = _MissDB()  # type: ignore[assignment]
    r = tc.post("/admin/profiles", json={"name": "user", "agents": ["ghost-agent"]}, headers=ADMIN_H)
    assert r.status_code == 400 and "ghost-agent" in r.text


def test_profile_save_without_agents_leaves_the_selection_alone(admin_env) -> None:
    """THE THREE-STATE GUARD: agents omitted (None) must not touch the join
    table — otherwise every older console POST silently clears the profile's
    agents (the destructive-by-omission hazard the separate table exists to
    avoid)."""
    tc, db = admin_env
    r = tc.post("/admin/profiles", json={"name": "user"}, headers=ADMIN_H)
    assert r.status_code == 201, r.text
    assert not any("profile_agents" in sql for sql, _ in db.executed)

