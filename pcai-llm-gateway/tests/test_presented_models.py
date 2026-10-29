"""What /v1/models advertises — the admin-side view, and the header that
announces what it hid.

WHY THIS FILE EXISTS (2026-10-23): a key could not see Qwen3.8-27B in the chat
console's dropdown and nothing anywhere said why. "The gateway does not serve
that model" and "my key is not allowed to use it" looked IDENTICAL from the
client side, because /v1/models filters by the key's allowed_models silently.
Two surfaces fix that:

  * GET /v1/models now stamps X-PCAI-Models-Denied: <n> — the count of
    registered models the key's ACL filtered out (0 for an allow-all key). The
    JSON BODY is byte-identical, so every existing client is unaffected; the
    header is additive metadata for consoles and operators.
  * GET /admin/presented-models is the operator-side counterpart: everything
    the data plane CAN present — the alias pair with its ordering, the routing
    pseudo-models, and every registered pool with its health, ceiling, off-peak
    state and eligibility. It needs no proxy-scoped key, so an operator can
    inspect the advertised surface without impersonating one.

The honesty contract these tests pin: the admin view reports REGISTRY truth +
CONFIG truth, never one key's filtered view. An admin key holds no profile, so
'order' is DERIVED (the CHAT pools, name-sorted) and every alias reason says
so rather than presenting a derivation as fact.

Mock-upstream discipline mirrors test_routing_model.py: an in-process Router
with endpoints set directly, no live Postgres.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import routing_config as rc
from pcai_llm.routing.router import Endpoint, Router

ADMIN_H = {"Authorization": "Bearer admin-secret"}
KEY_H = {"Authorization": "Bearer hllm-x"}


def _key(allowed: list[str] | None = None) -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=allowed or [],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


def _router() -> Router:
    """Two chat pools + one embedding pool: enough to assert the chat/type
    split that both surfaces depend on."""
    r = Router()
    r.set_endpoints("llm-chat", [Endpoint(id="e1", model_name="llm-chat", base_url="http://a/v1", model_type="chat")])
    r.set_endpoints("llm-big", [Endpoint(id="e2", model_name="llm-big", base_url="http://b/v1", model_type="chat")])
    r.set_endpoints("emb", [Endpoint(id="e3", model_name="emb", base_url="http://c/v1", model_type="embedding")])
    return r


# ---------------------------------------------------------------------------
# /v1/models — X-PCAI-Models-Denied (the "why is my model missing" header)
# ---------------------------------------------------------------------------


class _ProxyDB:
    """The two reads the listing makes: the profile ordering and nothing else
    (no budgets — a listing is not gated)."""

    def __init__(self, order: list[str] | None = None) -> None:
        self.order = order or []

    async def fetch(self, sql: str, *a: Any) -> list[Any]:
        return []

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        if "profiles" in sql:
            return {"model_order": self.order} if self.order else None
        return None

    async def fetchval(self, sql: str, *a: Any) -> Any:
        return None

    async def execute(self, sql: str, *a: Any) -> str:
        return "OK"


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch):
    """Mount the proxy router with a fixed key + in-process registry."""

    def _install(allowed: list[str] | None = None, order: list[str] | None = None):
        key = _key(allowed)

        import pcai_llm.api.auth as auth_mod

        async def fake_authenticate(plaintext: str) -> KeyRecord:
            return key

        monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
        monkeypatch.setattr(routes_proxy, "db", _ProxyDB(order))
        # ONE router instance for both handles: in production routes_proxy and
        # core.cascade import the SAME registry singleton, and the listing now
        # consults core.cascade.serves_chat (chat-only primary/cascade), so a
        # fixture that patches only the proxy's handle would give the two
        # modules different registries — a shape production cannot have.
        _r = _router()
        monkeypatch.setattr(routes_proxy, "model_router", _r)
        monkeypatch.setattr(cascade_mod, "model_router", _r)
        monkeypatch.setattr(routes_proxy, "profile_order_for_key", _order_fn(order))
        app = FastAPI()
        app.include_router(routes_proxy.router)
        return TestClient(app, raise_server_exceptions=False)

    return _install


def _order_fn(order: list[str] | None):
    async def _inner(key_id: Any) -> list[str]:
        return order or []

    return _inner


def test_denied_header_is_zero_for_an_allow_all_key(proxy_env) -> None:
    tc = proxy_env()
    r = tc.get("/v1/models", headers=KEY_H)
    assert r.status_code == 200, r.text
    assert r.headers["x-pcai-models-denied"] == "0"


def test_denied_header_counts_what_the_acl_filtered(proxy_env) -> None:
    """THE fix for the silent truncation: an allowlist that omits a registered
    model now ANNOUNCES the omission instead of just returning a shorter list."""
    tc = proxy_env(allowed=["llm-chat"])
    r = tc.get("/v1/models", headers=KEY_H)
    assert r.status_code == 200
    assert r.headers["x-pcai-models-denied"] == "2"  # llm-big + emb hidden
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["llm-chat"]


def test_denied_header_is_zero_when_the_acl_covers_everything(proxy_env) -> None:
    tc = proxy_env(allowed=["llm-chat", "llm-big", "emb"])
    r = tc.get("/v1/models", headers=KEY_H)
    assert r.headers["x-pcai-models-denied"] == "0"


def test_body_is_unchanged_by_the_header(proxy_env) -> None:
    """The header is ADDITIVE metadata: the JSON body must be byte-identical to
    what a client saw before it existed (no new key, same entries)."""
    tc = proxy_env()
    body = tc.get("/v1/models", headers=KEY_H).json()
    assert body["object"] == "list"
    for entry in body["data"]:
        assert set(entry) == {"id", "object", "created", "owned_by", "pcai"}


def test_primary_is_never_a_non_chat_model(proxy_env) -> None:
    """``pcai.primary`` means "what ``auto`` would resolve to", and ``auto``
    is a CHAT walk. A profile ordering that heads with an embedding pool (the
    seeder's default profile does) must not mark it primary — the console would
    preselect it in the chat dropdown and the first send would 400."""
    tc = proxy_env(order=["emb", "llm-chat"])
    body = tc.get("/v1/models", headers=KEY_H).json()
    assert [m["id"] for m in body["data"] if m["pcai"].get("primary")] == ["llm-chat"]


def test_routing_entries_are_gated_by_the_fleet_switch(proxy_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no profile policy an admin/plain key sees no routing names — and
    the master switch alone does not change that (the key still has no
    profile). This is the data-plane half of the presented-models honesty
    contract."""
    monkeypatch.setattr(settings, "routing_enabled", True)
    tc = proxy_env()
    ids = [m["id"] for m in tc.get("/v1/models", headers=KEY_H).json()["data"]]
    assert "routing" not in ids and not any(i.startswith("routing__") for i in ids)


# ---------------------------------------------------------------------------
# GET /admin/presented-models
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret")
    rc._cache = {}
    rc._cache_at = 0.0
    monkeypatch.setattr(routes_admin, "model_router", _router())
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def _fake_config(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    """Pin the routing config the presented view reads (it is async, so a plain
    monkeypatch of get_config is the seam — no DB in this file)."""
    merged = {**rc.defaults(), **values}

    async def _get() -> dict[str, Any]:
        return merged

    monkeypatch.setattr(rc, "get_config", _get)


def test_presented_models_reports_the_whole_surface(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_config(monkeypatch)
    r = admin_env.get("/admin/presented-models", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("aliases", "models", "routing", "env", "counts"):
        assert key in d, key

    # the alias pair, both present, with the DERIVED ordering (chat-first)
    aliases = {a["id"]: a for a in d["aliases"]}
    assert set(aliases) == {"auto", "auto-demand-aware"}
    # CHAT ONLY (2026-10-29): the cascade is a chat walk — the embedding pool
    # is NOT a step in it (it used to be appended to the tail, which rendered
    # "auto: ... → qwen3-vl-embedding" for a walk the data plane cannot make).
    assert aliases["auto"]["order"] == ["llm-big", "llm-chat"]
    assert aliases["auto"]["demand_aware"] is False
    assert aliases["auto-demand-aware"]["demand_aware"] is True
    for a in d["aliases"]:
        assert a["present"] is True
        # the view never claims to know one key's ordering
        assert "admin view" in a["reason"] and "profile order" in a["reason"]

    # every registered pool, with its type and health
    models = {m["id"]: m for m in d["models"]}
    assert set(models) == {"llm-chat", "llm-big", "emb"}
    assert models["emb"]["type"] == "embedding"
    assert models["llm-chat"]["endpoints"] == 1 and models["llm-chat"]["healthy"] == 1
    # primary marks the first slot of the derived ordering (chat first)
    assert models["llm-big"]["primary"] is True
    assert models["emb"]["primary"] is False
    # routing_eligible follows the CONFIG, and is chat-only
    assert models["llm-chat"]["routing_eligible"] is True
    assert models["emb"]["routing_eligible"] is False

    assert d["counts"]["registered"] == 3


def test_presented_models_aliases_absent_when_no_chat_pool(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """CHAT ONLY (2026-10-29): an embedding-only registry has no cascade to
    advertise — ``auto`` would 400 on every request. The panel must say so
    instead of deriving an order from pools the chat path can never serve."""
    _fake_config(monkeypatch)
    r = Router()
    r.set_endpoints("emb", [Endpoint(id="e9", model_name="emb", base_url="http://c/v1", model_type="embedding")])
    monkeypatch.setattr(routes_admin, "model_router", r)
    d = admin_env.get("/admin/presented-models", headers=ADMIN_H).json()
    assert [a["present"] for a in d["aliases"]] == [False, False]
    assert d["aliases"][0]["order"] == []
    assert [m["id"] for m in d["models"]] == ["emb"]
    # no chat pool -> no ``routing__<model>`` either (a chat concept)
    assert [x["id"] for x in d["routing"]] == ["routing"]


def test_presented_models_aliases_absent_when_registry_empty(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_config(monkeypatch)
    monkeypatch.setattr(routes_admin, "model_router", Router())
    d = admin_env.get("/admin/presented-models", headers=ADMIN_H).json()
    assert [a["present"] for a in d["aliases"]] == [False, False]
    assert d["models"] == []
    assert d["counts"]["registered"] == 0


def test_presented_models_routing_entries_follow_the_fleet_switch(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_config(monkeypatch)
    monkeypatch.setattr(settings, "routing_enabled", False)
    d = admin_env.get("/admin/presented-models", headers=ADMIN_H).json()
    assert all(not x["present"] for x in d["routing"])
    assert "PCAI_LLM_ROUTING_ENABLED is off" in d["routing"][0]["reason"]
    # env block reports the truth the reasons refer to
    assert d["env"]["routing_enabled"] is False

    monkeypatch.setattr(settings, "routing_enabled", True)
    d2 = admin_env.get("/admin/presented-models", headers=ADMIN_H).json()
    ids = [x["id"] for x in d2["routing"]]
    # one per CHAT pool — the embedding pool is never a routing candidate
    assert ids == ["routing", "routing__llm-big", "routing__llm-chat"]
    assert all(x["present"] for x in d2["routing"])


def test_presented_models_routing_eligible_follows_the_config(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """allowed_models is the ROUTER's own ceiling (config truth), deliberately
    separate from a key's ACL — the panel must show the two as different
    things."""
    _fake_config(monkeypatch, allowed_models=["llm-chat"])
    d = admin_env.get("/admin/presented-models", headers=ADMIN_H).json()
    eligible = {m["id"]: m["routing_eligible"] for m in d["models"]}
    assert eligible == {"llm-chat": True, "llm-big": False, "emb": False}


def test_presented_models_reports_ceiling_exclusion_and_offpeak(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_config(monkeypatch)
    monkeypatch.setattr(settings, "max_tokens_ceiling", {"llm-chat": 32768})
    monkeypatch.setattr(settings, "demand_aware_excluded_models", ["llm-big"])
    monkeypatch.setattr(settings, "offpeak_enabled", True)

    from pcai_llm.core import offpeak

    monkeypatch.setattr(
        offpeak,
        "models_snapshot",
        lambda: {"llm-chat": {"active": True, "fraction": 0.5, "last_value_pct": 12.5}},
    )
    d = admin_env.get("/admin/presented-models", headers=ADMIN_H).json()
    models = {m["id"]: m for m in d["models"]}
    assert models["llm-chat"]["max_tokens_ceiling"] == 32768
    assert models["llm-chat"]["offpeak"] == {"value_pct": 12.5, "active": True, "fraction": 0.5}
    # a model with no evaluator entry reports nulls, never a fabricated 0%
    assert models["llm-big"]["offpeak"] == {"value_pct": None, "active": None, "fraction": None}
    assert models["llm-big"]["demand_aware_excluded"] is True
    assert models["llm-chat"]["demand_aware_excluded"] is False


def test_presented_models_counts_presented_entries(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_config(monkeypatch)
    monkeypatch.setattr(settings, "routing_enabled", True)
    d = admin_env.get("/admin/presented-models", headers=ADMIN_H).json()
    # 3 pools + 2 aliases + (1 bare routing + 2 per-chat-model) = 8
    assert d["counts"]["presented"] == 8
    assert d["counts"]["registered"] == 3


def test_presented_models_needs_no_proxy_scope(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator inspects the advertised surface with an ADMIN key — no
    proxy-scoped virtual key, no impersonation."""
    _fake_config(monkeypatch)
    assert admin_env.get("/admin/presented-models").status_code == 401
    assert admin_env.get("/admin/presented-models", headers=ADMIN_H).status_code == 200
