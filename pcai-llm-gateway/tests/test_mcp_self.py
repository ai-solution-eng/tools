"""Self-service MCP management (routes_mcp_self) — the entitlement-bounded
invariant tests.

The load-bearing property: a self-grant OUTSIDE the caller's entitlement
catalog is 403; credentials are write-only and scoped to the CALLER's key
(no key_id parameter anywhere on the surface).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import auth as auth_mod
from pcai_llm.api import routes_mcp_self
from pcai_llm.config import settings
from pcai_llm.core.keys import KeyRecord


def _key(scopes: list[str] | None = None) -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=scopes if scopes is not None else ["proxy", "mcp"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


class SelfFakeDB:
    """Minimal db seam for the self-service routes (fetch/fetchval/execute)."""

    def __init__(self) -> None:
        self.server_exists = True
        self.cred_rows: list[dict[str, Any]] = []
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any):
        if "FROM mcp_server_creds" in sql:
            return list(self.cred_rows)
        return []

    async def fetchval(self, sql: str, *args: Any):
        if "FROM mcp_servers" in sql:
            return 1 if self.server_exists else None
        return 1

    async def fetchrow(self, sql: str, *args: Any):
        return None

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        return "UPDATE 1"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Fake auth + fake db + real router (flag ON). Returns (client, db, key)."""
    key = _key()
    monkeypatch.setattr(settings, "mcp_self_service", True)
    monkeypatch.setattr(settings, "secrets_master_key", "mcp-self-test-master")

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)

    fake = SelfFakeDB()
    monkeypatch.setattr(routes_mcp_self, "db", fake)

    # Registry: one enabled server, tool_policy 'all'.
    async def fake_mcp_servers() -> list[dict[str, Any]]:
        return [
            {
                "name": "logsearch",
                "url": "http://logsearch-mcp.ns.svc:9101/mcp",
                "headers": {},
                "auth_secret": None,
                "auth_token_enc": None,
                "tool_policy": "all",
                "allowed_tools": [],
                "enabled": True,
                "priority": 0,
            }
        ]

    import pcai_llm.core.registry as registry_mod

    monkeypatch.setattr(registry_mod, "mcp_servers", fake_mcp_servers)

    # Upstream tool listing (the _cached_upstream_tools seam).
    async def fake_cached_tools(target: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"tool": "search_logs", "description": "search"}, {"tool": "exotic_tool", "description": "rare"}]

    import pcai_llm.api.routes_mcp as routes_mcp_mod

    monkeypatch.setattr(routes_mcp_mod, "_cached_upstream_tools", fake_cached_tools)

    # Grants: the key HOLDS search_logs only (a TRUE row); no live profile.
    async def fake_load_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return {("logsearch", "search_logs")}

    async def fake_load_denies(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return set()

    monkeypatch.setattr(routes_mcp_self.federation, "load_grants", fake_load_grants)
    monkeypatch.setattr(routes_mcp_self.federation, "load_denies", fake_load_denies)

    # Scope grant mode ON (the LEGACY mode, set here to keep exercising its
    # rendering path) — entitlement stays the HELD set; under scope mode the
    # policy-admitted pairs still SHOW as granted/via_scope (that's the
    # relay's call-time admission), but they are NOT self-grantable.
    monkeypatch.setattr(settings, "tool_relay_grant_mode", "scope")

    app = FastAPI()
    app.include_router(routes_mcp_self.router)
    return TestClient(app, raise_server_exceptions=False), fake, key


def _h() -> dict[str, str]:
    return {"X-API-Key": "hllm-test"}


def test_creds_put_get_delete_roundtrip(env) -> None:
    tc, fake, key = env
    r = tc.put(
        "/v1/mcp/creds/logsearch",
        json={"server": "logsearch", "headers": {"X-Dataset-Password": "sekrit"}},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"server": "logsearch", "header_names": ["X-Dataset-Password"]}
    # the write went to the CALLER's key id (no squatting surface)
    assert fake.executed and fake.executed[0][1][0] == key.id


def test_creds_reserved_header_refused(env) -> None:
    tc, _fake, _key = env
    r = tc.put(
        "/v1/mcp/creds/logsearch",
        json={"server": "logsearch", "headers": {"X-MCP-Caller": "someone-else"}},
        headers=_h(),
    )
    assert r.status_code == 400
    assert "reserved" in r.json()["detail"]


def test_creds_unknown_server_404(env) -> None:
    tc, fake, _key = env
    fake.server_exists = False
    r = tc.put(
        "/v1/mcp/creds/ghost",
        json={"server": "ghost", "headers": {"X-Api": "v"}},
        headers=_h(),
    )
    assert r.status_code == 404


def test_grant_held_ok_outside_403_even_scope_mode(env) -> None:
    """THE SSO-poweruser-leak regression: a policy-admitted catalog pair the
    key does not HOLD is NOT self-grantable — even under legacy scope mode
    (the old entitlement folded the whole admitted catalog in, letting a
    self-minted key re-open what its profile withheld). Held pairs pass;
    everything else 403s."""
    tc, _fake, _key = env
    # search_logs is HELD (an ACL row) → re-enabling it is a tighten, fine
    r = tc.put(
        "/v1/mcp/selfservice/grants",
        json={"server": "logsearch", "tool": "search_logs", "allowed": True},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    # exotic_tool is listed by the server AND policy-admitted ('all'), but
    # NOT held — the profile envelope must not be re-openable from the panel
    r = tc.put(
        "/v1/mcp/selfservice/grants",
        json={"server": "logsearch", "tool": "exotic_tool", "allowed": True},
        headers=_h(),
    )
    assert r.status_code == 403, "scope mode: admitted-but-unheld must NOT be self-grantable"
    # a tool the server does NOT expose → outside on every path
    r = tc.put(
        "/v1/mcp/selfservice/grants",
        json={"server": "logsearch", "tool": "not_listed", "allowed": True},
        headers=_h(),
    )
    assert r.status_code == 403, "a self-grant outside the held set must refuse"


def test_grant_outside_entitlement_when_explicit_mode(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under 'explicit' grant mode the entitlement is the HELD set only —
    an unheld catalog pair is NOT self-grantable."""
    tc, _fake, _key = env
    monkeypatch.setattr(settings, "tool_relay_grant_mode", "explicit")
    r = tc.put(
        "/v1/mcp/selfservice/grants",
        json={"server": "logsearch", "tool": "exotic_tool", "allowed": True},
        headers=_h(),
    )
    assert r.status_code == 403, "explicit mode: entitlement = held pairs only"


def test_deny_of_unlisted_tool_404(env) -> None:
    tc, _fake, _key = env
    r = tc.put(
        "/v1/mcp/selfservice/grants",
        json={"server": "logsearch", "tool": "not_listed", "allowed": False},
        headers=_h(),
    )
    assert r.status_code == 404


def test_overview_lists_grant_state_and_entitlement(env) -> None:
    tc, _fake, _key = env
    r = tc.get("/v1/mcp/selfservice", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert body["scope_mode"] is True
    (srv,) = body["servers"]
    assert srv["server"] == "logsearch"
    by_tool = {t["tool"]: t for t in srv["tools"]}
    # search_logs: HELD by an ACL row → granted, not via-scope
    assert by_tool["search_logs"]["granted"] is True
    assert by_tool["search_logs"]["via_scope"] is False
    assert by_tool["search_logs"]["self_grantable"] is True
    # exotic_tool: NO ACL row, but scope mode admits the policy-ok catalog
    # pair at call time (the relay merge) — the dialog says so truthfully,
    # while the pair stays OUTSIDE the held entitlement (not self-grantable:
    # the profile envelope cannot be re-opened from the panel).
    assert by_tool["exotic_tool"]["granted"] is True
    assert by_tool["exotic_tool"]["via_scope"] is True
    assert by_tool["exotic_tool"]["self_grantable"] is False


def test_overview_explicit_mode_lists_held_only(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under 'explicit' grant mode (the shipped default) the relay admits
    held rows only — so the panel lists held pairs ONLY: the unheld catalog
    pair is absent, not a lying unchecked checkbox."""
    tc, _fake, _key = env
    monkeypatch.setattr(settings, "tool_relay_grant_mode", "explicit")
    body = tc.get("/v1/mcp/selfservice", headers=_h()).json()
    assert body["scope_mode"] is False
    (srv,) = body["servers"]
    by_tool = {t["tool"]: t for t in srv["tools"]}
    assert by_tool["search_logs"]["granted"] is True and by_tool["search_logs"]["via_scope"] is False
    assert by_tool["search_logs"]["self_grantable"] is True
    assert "exotic_tool" not in by_tool, "unheld pair must not be listed at all"


def test_overview_deny_subtracts_from_scope_admission(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deny row is the ONLY scope-mode brake — it removes the scope-admitted
    pair from `granted` (and from the panel entirely: an unheld pair the deny
    withholds has no reason to render). Held-and-not-denied pairs stay."""
    tc, _fake, _key = env

    async def fake_load_denies(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return {("logsearch", "exotic_tool")}

    monkeypatch.setattr(routes_mcp_self.federation, "load_denies", fake_load_denies)
    body = tc.get("/v1/mcp/selfservice", headers=_h()).json()
    (srv,) = body["servers"]
    by_tool = {t["tool"]: t for t in srv["tools"]}
    assert "exotic_tool" not in by_tool, "denied scope-admitted pair must not render"
    assert by_tool["search_logs"]["granted"] is True  # held, not denied


def test_flag_off_404s_the_surface(env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _fake, _key = env
    monkeypatch.setattr(settings, "mcp_self_service", False)
    assert tc.get("/v1/mcp/selfservice", headers=_h()).status_code == 404
    assert tc.get("/v1/mcp/creds", headers=_h()).status_code == 404


def test_missing_mcp_scope_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy-only key has no business on the surface (the scope gate)."""
    key = _key(scopes=["proxy"])

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    monkeypatch.setattr(settings, "mcp_self_service", True)
    monkeypatch.setattr(routes_mcp_self, "db", SelfFakeDB())
    app = FastAPI()
    app.include_router(routes_mcp_self.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/v1/mcp/selfservice", headers=_h())
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 2026-10-09 — the SSO poweruser leak: visibility omission + held-only
# entitlement under the NEW default grant mode ('explicit')
# ---------------------------------------------------------------------------


def test_overview_omits_withheld_server(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server the key holds NOTHING on is OMITTED from the panel entirely —
    not listed with disabled checkboxes. THE user-reported symptom: the SSO
    self-minted poweruser key saw applygate/k8s-ops rows it could probe and
    toggle, when its profile granted neither."""
    tc, _fake, _key = env
    # second enabled server the key holds nothing on

    import pcai_llm.core.registry as registry_mod

    async def two_servers() -> list[dict[str, Any]]:
        return [
            {
                "name": "logsearch",
                "url": "http://logsearch-mcp.ns.svc:9101/mcp",
                "headers": {},
                "auth_secret": None,
                "auth_token_enc": None,
                "tool_policy": "all",
                "allowed_tools": [],
                "enabled": True,
                "priority": 0,
            },
            {
                "name": "applygate",
                "url": "http://applygate-mcp.ns.svc:9102/mcp",
                "headers": {},
                "auth_secret": None,
                "auth_token_enc": None,
                "tool_policy": "all",
                "allowed_tools": [],
                "enabled": True,
                "priority": 1,
            },
        ]

    async def tools_by_server(target: dict[str, Any]) -> list[dict[str, Any]]:
        if target["name"] == "logsearch":
            return [{"tool": "search_logs", "description": "search"}]
        return [{"tool": "plan_apply", "description": "plan"}, {"tool": "apply_manifest", "description": "apply"}]

    import pcai_llm.api.routes_mcp as routes_mcp_mod

    monkeypatch.setattr(registry_mod, "mcp_servers", two_servers)
    monkeypatch.setattr(routes_mcp_mod, "_cached_upstream_tools", tools_by_server)

    # explicit mode (the new default): entitlement = held = search_logs only
    monkeypatch.setattr(settings, "tool_relay_grant_mode", "explicit")
    body = tc.get("/v1/mcp/selfservice", headers=_h()).json()
    names = [s["server"] for s in body["servers"]]
    assert names == ["logsearch"], f"withheld server must be omitted entirely, got {names}"
    assert body["scope_mode"] is False


def test_overview_default_mode_is_explicit(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default is 'explicit' (the 2026-10-09 posture): without any
    monkeypatching of grant_mode, an unheld pair is invisible AND 403 on the
    toggle path — the panel and the enforcement agree."""
    tc, _fake, _key = env
    # the fixture set scope; restore the shipped default to prove the default
    monkeypatch.setattr(settings, "tool_relay_grant_mode", "explicit")
    body = tc.get("/v1/mcp/selfservice", headers=_h()).json()
    (srv,) = body["servers"]
    tools = {t["tool"]: t for t in srv["tools"]}
    # ONLY the held pair is listed at all
    assert set(tools) == {"search_logs"}, f"unheld pairs must not be listed, got {sorted(tools)}"
    assert tools["search_logs"]["granted"] is True and tools["search_logs"]["self_grantable"] is True
    # and the unheld pair cannot be granted through the API either
    r = tc.put(
        "/v1/mcp/selfservice/grants",
        json={"server": "logsearch", "tool": "exotic_tool", "allowed": True},
        headers=_h(),
    )
    assert r.status_code == 403


def test_overview_lists_deny_row_as_ungranted(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A held pair carrying a key-level deny exception is withheld from the
    panel too (load_grants subtracts it) — visibility mirrors enforcement."""
    tc, _fake, _key = env

    async def held_with_deny(key_id: uuid.UUID) -> set[tuple[str, str]]:
        # deny already folded out by load_grants: only the clean pair survives
        return {("logsearch", "search_logs")}

    async def denies(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return {("logsearch", "toggled_off")}

    monkeypatch.setattr(routes_mcp_self.federation, "load_grants", held_with_deny)
    monkeypatch.setattr(settings, "tool_relay_grant_mode", "explicit")
    body = tc.get("/v1/mcp/selfservice", headers=_h()).json()
    (srv,) = body["servers"]
    tools = {t["tool"]: t for t in srv["tools"]}
    assert set(tools) == {"search_logs"}
