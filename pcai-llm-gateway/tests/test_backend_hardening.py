"""Regression tests for the backend-hardening batch:

P0: budget_status USD rows, PATCH mcp-servers enabled preservation,
    metadata timeouts.
P1: bounded body read (413), SSRF guard, key PATCH, endpoint health
    surface + probe, ledger explorer (filters/actor/CSV), rate-limit
    headers, breaker-state preservation across refresh, rate_windows
    prune, DB pool settings + bounded acquire.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_mcp, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import Settings, settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import registry
from pcai_llm.db import PoolUnavailable
from pcai_llm.db import db as db_mod
from pcai_llm.routing.router import Endpoint, Router

# ---------------------------------------------------------------------------
# P0.3: budget_status emits USD rows alongside token rows
# ---------------------------------------------------------------------------


class BudgetFakeDB:
    def __init__(self) -> None:
        self.token_budgets: list[dict] = []
        self.usd_budgets: list[dict] = []
        self.spent = 0
        self.spent_usd = 0.0

    async def fetch(self, sql: str, *a: Any):
        if "FROM budgets" in sql:
            # single aggregate query: budget rows joined to window spend
            return [{**b, "spent": self.spent} for b in self.token_budgets]
        if "FROM cost_budgets" in sql:
            return [{**b, "spent": self.spent_usd} for b in self.usd_budgets]
        return []

    async def fetchval(self, sql: str, *a: Any):
        if "SUM(cost_usd)" in sql:
            return self.spent_usd
        if "SUM(total_tokens)" in sql:
            return self.spent
        return None


def test_budget_status_emits_usd_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = BudgetFakeDB()
    fake.token_budgets = [{"model": "*", "period": "month", "limit_tokens": 1000}]
    fake.usd_budgets = [{"model": "m1", "period": "day", "limit_usd": 10.0}]
    fake.spent = 830
    fake.spent_usd = 4.25
    monkeypatch.setattr(budgets_mod, "db", fake)

    kid = uuid.uuid4()
    out = asyncio.run(budgets_mod.budget_status(kid))
    kinds = sorted(b["kind"] for b in out)
    assert kinds == ["tokens", "usd"]
    tok = next(b for b in out if b["kind"] == "tokens")
    # ORIGINAL token shape intact (current consumers keep working)
    assert tok["limit"] == 1000 and tok["spent"] == 830
    assert tok["model"] == "*" and tok["period"] == "month" and "window_start" in tok
    usd = next(b for b in out if b["kind"] == "usd")
    assert usd["limit_usd"] == 10.0 and usd["spent_usd"] == 4.25
    assert usd["model"] == "m1" and usd["period"] == "day"


# ---------------------------------------------------------------------------
# P0.8: PATCH /admin/mcp-servers/{name} preserves `enabled` when omitted
# ---------------------------------------------------------------------------


class McpPatchFakeDB:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.updates: list[tuple] = []

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM mcp_servers" in sql:
            return {
                "url": "http://svc",
                "headers_json": "{}",
                "auth_secret": None,
                "tool_policy": "all",
                "allowed_tools_json": "[]",
                "enabled": self.enabled,
            }
        if "UPDATE mcp_servers" in sql:
            self.updates.append(args)
            return {
                "id": "1",
                "name": "svc",
                "url": "http://svc",
                "auth_secret": None,
                "tool_policy": args[2],
                "allowed_tools_json": args[3],
                "enabled": args[1],
            }
        return None

    async def execute(self, sql: str, *a: Any):
        return "UPDATE 1"


def _admin_client(monkeypatch: pytest.MonkeyPatch, fake: Any) -> TestClient:
    import pcai_llm.core.registry as registry_mod

    async def no_refresh() -> None:
        pass

    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_admin, "db", fake)
    monkeypatch.setattr(registry_mod, "refresh", no_refresh)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def _ah() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-123"}


@pytest.mark.parametrize("row_enabled", [False, True])
def test_patch_mcp_server_preserves_enabled(monkeypatch: pytest.MonkeyPatch, row_enabled: bool) -> None:
    """A policy-only console edit must NOT resurrect a disabled (kill-switch)
    server — omitted `enabled` keeps the row's current value."""
    fake = McpPatchFakeDB(enabled=row_enabled)
    tc = _admin_client(monkeypatch, fake)
    r = tc.patch("/admin/mcp-servers/svc", json={"tool_policy": "allowlist"}, headers=_ah())
    assert r.status_code == 200, r.text
    assert fake.updates, "UPDATE never ran"
    # UPDATE args: (name, enabled, tool_policy, allowed_tools_json)
    assert fake.updates[0][1] is row_enabled, "omitted enabled must preserve the row's current value"
    assert r.json()["enabled"] is row_enabled


def test_patch_mcp_server_explicit_enabled_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = McpPatchFakeDB(enabled=True)
    tc = _admin_client(monkeypatch, fake)
    r = tc.patch("/admin/mcp-servers/svc", json={"enabled": False}, headers=_ah())
    assert r.status_code == 200
    assert fake.updates[0][1] is False


# ---------------------------------------------------------------------------
# P0.5: metadata MCP fetches are bounded (15 s), not the 600 s upstream timeout
# ---------------------------------------------------------------------------


def test_mcp_catalog_uses_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float | None] = []

    async def fake_fetch_tools(url: str, headers: dict, timeout_s: float | None = None):
        seen.append(timeout_s)
        return []

    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch_tools)
    registry._mcp_servers_cache = [
        {
            "id": "1",
            "name": "svc",
            "url": "http://svc",
            "headers": {},
            "auth_secret": None,
            "auth_token_enc": None,
            "enabled": True,
            "tool_policy": "all",
            "allowed_tools": [],
        }
    ]
    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-X",
        scopes=["mcp"],
        allowed_models=[],
        rate_limit_rpm=60,
        rate_limit_tpm=100000,
        max_concurrent=0,
        expires_at=None,
    )

    class GrantsDB:
        async def fetch(self, sql: str, *a: Any):
            return []

    monkeypatch.setattr(routes_mcp, "registry", registry)
    import pcai_llm.mcpf.federation as fed

    monkeypatch.setattr(fed, "db", GrantsDB())
    ctx = routes_mcp._caller.set(key)
    try:
        out = asyncio.run(routes_mcp._mcp_catalog())
    finally:
        routes_mcp._caller.reset(ctx)
    assert json.loads(out)["servers"] == []
    assert seen == [15.0], "mcp_catalog metadata fetch must pass timeout_s=15.0"


# ---------------------------------------------------------------------------
# P1.13: chunked (no Content-Length) bodies are HARD-capped — 413
# ---------------------------------------------------------------------------


def test_json_body_caps_chunked_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_body_bytes", 64)

    async def receive():
        return {"type": "http.request", "body": b"x" * 200, "more_body": False}

    request = routes_proxy.Request(
        {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}, receive
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_proxy._json_body(request))
    assert ei.value.status_code == 413


def test_json_body_caps_declared_and_parses_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_body_bytes", 64)

    # declared oversized -> 413 before buffering
    async def receive_big():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = routes_proxy.Request({"type": "http", "headers": [(b"content-length", b"999999")]}, receive_big)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(routes_proxy._json_body(req))
    assert ei.value.status_code == 413

    # normal body still parses
    async def receive_body():
        return {"type": "http.request", "body": b'{"a": 1}', "more_body": False}

    req2 = routes_proxy.Request({"type": "http", "headers": [(b"content-length", b"7")]}, receive_body)
    assert asyncio.run(routes_proxy._json_body(req2)) == {"a": 1}

    # invalid JSON still maps to 400 (previous behavior preserved)
    async def receive_bad():
        return {"type": "http.request", "body": b"not json", "more_body": False}

    req3 = routes_proxy.Request({"type": "http", "headers": []}, receive_bad)
    with pytest.raises(HTTPException) as ei3:
        asyncio.run(routes_proxy._json_body(req3))
    assert ei3.value.status_code == 400


# ---------------------------------------------------------------------------
# P1.14: SSRF guard on admin-registered URLs
# ---------------------------------------------------------------------------


def test_check_url_denies_metadata_and_loopback() -> None:
    for bad in (
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:9200/",
        "http://[::1]:9200/",
        "http://localhost:9200/",
        "ftp://internal/x",
        "file:///etc/passwd",
    ):
        with pytest.raises(HTTPException) as ei:
            routes_admin._check_url(bad, "base_url")
        assert ei.value.status_code == 400


def test_check_url_allows_incluster_targets() -> None:
    """RFC1918 and *.svc.cluster.local are LEGITIMATE upstreams here — the
    default posture stays permissive for the in-cluster deployment."""
    for good in (
        "http://10.1.2.3:8000/v1",
        "http://192.168.1.10:8080",
        "http://172.16.0.5",
        "http://vllm-pool.default.svc.cluster.local:8000/v1",
        "https://api.example.com/v1",
    ):
        routes_admin._check_url(good, "base_url")  # must not raise


def test_check_url_extra_deny_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "url_deny_cidrs", ["10.0.0.0/8"])
    with pytest.raises(HTTPException):
        routes_admin._check_url("http://10.1.2.3:8000/v1", "base_url")
    routes_admin._check_url("http://192.168.1.10:8080", "base_url")  # still fine


# ---------------------------------------------------------------------------
# P1.15: PATCH /admin/keys/{key_id}
# ---------------------------------------------------------------------------


class KeyPatchFakeDB:
    def __init__(self) -> None:
        self.updates: list[tuple] = []
        self.exists = True
        self.patched_key_id = uuid.uuid4()

    async def fetchval(self, sql: str, *a: Any):
        if "FROM api_keys" in sql:
            return 1 if self.exists else None
        return None

    async def fetchrow(self, sql: str, *args: Any):
        if "UPDATE api_keys" in sql:
            self.updates.append((sql, args))
            return {
                "id": self.patched_key_id,
                "user_id": "u1",
                "name": args[0] if "name = $1" in sql else "orig",
                "scopes": ["proxy"],
                "allowed_models": [],
                "rate_limit_rpm": 30,
                "rate_limit_tpm": 50000,
                "max_concurrent": 2,
                "relay_default": False,
                "stream_pcai_events": True,
                "expires_at": None,
            }
        return None


def test_patch_key_updates_limits_and_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeyPatchFakeDB()
    tc = _admin_client(monkeypatch, fake)
    r = tc.patch(
        f"/admin/keys/{fake.patched_key_id}",
        json={
            "rate_limit_rpm": 30,
            "rate_limit_tpm": 50000,
            "max_concurrent": 2,
            "scopes": ["proxy", "mcp"],
        },
        headers=_ah(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # identity material NEVER mutates or leaks through the patch
    assert "key_hash" not in body and "key_prefix" not in body and "key" not in body
    sql, args = fake.updates[0]
    assert "rate_limit_rpm = $" in sql and "rate_limit_tpm = $" in sql
    assert "max_concurrent = $" in sql and "scopes = $" in sql
    assert "key_hash" not in sql and "key_prefix" not in sql
    # SET order mirrors the provided fields (name omitted -> scopes first)
    assert args == (["proxy", "mcp"], 30, 50000, 2, fake.patched_key_id)


def test_patch_key_clears_expiry_with_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit null expires_at = clear the expiry; omitted = untouched."""
    fake = KeyPatchFakeDB()
    tc = _admin_client(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{uuid.uuid4()}", json={"expires_at": None}, headers=_ah())
    assert r.status_code == 200, r.text
    sql, args = fake.updates[0]
    assert "expires_at = $1" in sql and args[0] is None


def test_patch_key_404_and_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeyPatchFakeDB()
    tc = _admin_client(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{uuid.uuid4()}", json={"name": "x"}, headers=_ah())
    assert r.status_code == 200
    r3 = tc.patch(f"/admin/keys/{uuid.uuid4()}", json={}, headers=_ah())
    assert r3.status_code == 400, "empty patch body must be refused"
    fake.exists = False
    r2 = tc.patch(f"/admin/keys/{uuid.uuid4()}", json={"name": "x"}, headers=_ah())
    assert r2.status_code == 404


def test_patch_key_invalid_scope_400(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = KeyPatchFakeDB()
    tc = _admin_client(monkeypatch, fake)
    r = tc.patch(f"/admin/keys/{uuid.uuid4()}", json={"scopes": ["nope"]}, headers=_ah())
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# P1.16: endpoint health surface + probe
# ---------------------------------------------------------------------------


class EndpointsFakeDB:
    async def fetch(self, sql: str, *a: Any):
        if "FROM model_endpoints" in sql:
            return [
                {
                    "id": "ep-live",
                    "model_name": "m",
                    "base_url": "http://a/v1",
                    "priority": 0,
                    "weight": 1,
                    "model_type": "chat",
                    "enabled": True,
                },
                {
                    "id": "ep-gone",
                    "model_name": "m2",
                    "base_url": "http://b/v1",
                    "priority": 0,
                    "weight": 1,
                    "model_type": "chat",
                    "enabled": False,
                },
            ]
        return []


def test_admin_endpoints_merges_breaker_state(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as time_mod

    tc = _admin_client(monkeypatch, EndpointsFakeDB())
    failing = Endpoint(id="ep-live", model_name="m", base_url="http://a/v1")
    failing.consecutive_failures = 3
    failing.cooldown_until = time_mod.monotonic() + 120
    failing.last_error = "upstream status 502"
    failing.last_fail_at = time_mod.time()
    r_router = Router()
    r_router.set_endpoints("m", [failing])
    monkeypatch.setattr(routes_admin, "model_router", r_router)

    r = tc.get("/admin/endpoints", headers=_ah())
    assert r.status_code == 200, r.text
    rows = {row["id"]: row for row in r.json()}
    live = rows["ep-live"]
    assert live["state"] == "cooling"
    assert live["consecutive_failures"] == 3
    assert 0 < live["cooldown_remaining_s"] <= 120
    assert live["last_error"] == "upstream status 502"
    assert live["last_fail_at"] is not None and live["last_ok_at"] is None
    # disabled DB row with no live state reports as disabled, no breaker data
    gone = rows["ep-gone"]
    assert gone["state"] == "disabled" and gone["consecutive_failures"] == 0


class ProbeFakeDB(EndpointsFakeDB):
    async def fetch(self, sql: str, *a: Any):
        return []


class FakeProbeClient:
    status = 200

    def __init__(self, **kw: Any) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        self.url = url
        return self


def test_probe_model_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _admin_client(monkeypatch, ProbeFakeDB())
    r_router = Router()
    r_router.set_endpoints("m", [Endpoint(id="ep", model_name="m", base_url="http://a/v1")])
    monkeypatch.setattr(routes_admin, "model_router", r_router)
    seen: dict[str, Any] = {}

    class Ok(FakeProbeClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
            seen["url"] = url
            seen["headers"] = headers or {}
            self.status_code = 200
            return self

    monkeypatch.setattr(routes_admin.httpx2, "AsyncClient", Ok)
    r = tc.post("/admin/models/m/probe", headers=_ah())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["status_code"] == 200
    assert isinstance(body["latency_ms"], int)
    assert seen["url"] == "http://a/v1/models"
    # the probe must present the SAME headers routing would (request id —
    # and the bearer when the endpoint stores a credential): an unauthenticated
    # GET reads 403 on authed upstreams and shows a false pool failure
    assert seen["headers"].get("X-Request-ID", "").startswith("probe-")


def test_probe_model_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _admin_client(monkeypatch, ProbeFakeDB())
    r_router = Router()
    r_router.set_endpoints("m", [Endpoint(id="ep", model_name="m", base_url="http://a/v1")])
    monkeypatch.setattr(routes_admin, "model_router", r_router)

    class Dead(FakeProbeClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
            raise httpx2.ConnectError("refused")

    monkeypatch.setattr(routes_admin.httpx2, "AsyncClient", Dead)
    r = tc.post("/admin/models/m/probe", headers=_ah())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False and body["error"] == "ConnectError"


def test_probe_unknown_model_404(monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _admin_client(monkeypatch, ProbeFakeDB())
    monkeypatch.setattr(routes_admin, "model_router", Router())
    r = tc.post("/admin/models/ghost/probe", headers=_ah())
    assert r.status_code == 404


def test_probe_model_with_slash_in_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slash-named pools ("Qwen/Qwen3.8-27B-FP8") 404'd on probe with a plain
    path param — `{model_name:path}` carries them (console test-button bug)."""
    tc = _admin_client(monkeypatch, ProbeFakeDB())
    r_router = Router()
    r_router.set_endpoints(
        "Qwen/Qwen3.8-27B-FP8",
        [Endpoint(id="ep", model_name="Qwen/Qwen3.8-27B-FP8", base_url="http://a/v1")],
    )
    monkeypatch.setattr(routes_admin, "model_router", r_router)
    seen: dict[str, str] = {}

    class Ok(FakeProbeClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
            seen["url"] = url
            self.status_code = 200
            return self

    monkeypatch.setattr(routes_admin.httpx2, "AsyncClient", Ok)
    r = tc.post("/admin/models/Qwen/Qwen3.8-27B-FP8/probe", headers=_ah())
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert seen["url"] == "http://a/v1/models"


# ---------------------------------------------------------------------------
# P1.17: ledger explorer — filters, actor join, CSV export
# ---------------------------------------------------------------------------


class UsageFakeDB:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any):
        self.queries.append((sql, args))
        if "GROUP BY" in sql:
            base = {
                "requests": 3,
                "input_tokens": 10,
                "output_tokens": 4,
                "cached_tokens": 0,
                "cache_hits": 0,
                "cost_usd": 0.5,
            }
            if "AS key_name" in sql:
                return [{"key_id": "k1", "key_name": "team-key", "key_prefix": "hllm-AB", **base}]
            if "date_trunc" in sql:
                return [{"day": "2026-01-05T00:00:00Z", **base}]
            return [{"model": "m1", **base}]
        return [
            {
                "id": "r1",
                "key_id": "k1",
                "model": "m1",
                "endpoint": "chat",
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
                "status": "ok",
                "latency_ms": 5,
                "request_id": "rid",
                "ts": "t",
                "key_name": "team-key",
                "key_prefix": "hllm-AB",
                "user_name": "alice",
                "tenant_name": "acme",
            }
        ]


def test_usage_filters_and_actor_join(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = UsageFakeDB()
    tc = _admin_client(monkeypatch, fake)
    kid = uuid.uuid4()
    r = tc.get(
        "/admin/usage",
        params={
            "key_id": str(kid),
            "model": "m1",
            "status": "ok",
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-02-01T00:00:00Z",
            "limit": 50,
        },
        headers=_ah(),
    )
    assert r.status_code == 200, r.text
    sql, args = fake.queries[0]
    assert "key_name" in sql and "user_name" in sql and "tenant_name" in sql
    assert "LEFT JOIN api_keys" in sql and "LEFT JOIN tenants" in sql
    assert "l.key_id = $1" in sql and "l.model = $2" in sql and "l.status = $3" in sql
    assert "l.ts >= $4" in sql and "l.ts <= $5" in sql
    assert str(kid) in str(args[0]) or args[0] == kid
    row = r.json()[0]
    assert row["key_name"] == "team-key" and row["tenant_name"] == "acme"


def test_usage_group_by_day_and_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = UsageFakeDB()
    tc = _admin_client(monkeypatch, fake)
    r = tc.get("/admin/usage", params={"group_by": "day"}, headers=_ah())
    assert r.status_code == 200, r.text
    sql, _ = fake.queries[0]
    assert "date_trunc('day', l.ts)" in sql and "GROUP BY" in sql

    r2 = tc.get("/admin/usage", params={"group_by": "key", "format": "csv"}, headers=_ah())
    assert r2.status_code == 200
    assert r2.headers["content-type"].startswith("text/csv")
    assert "attachment" in r2.headers["content-disposition"]
    first_line = r2.text.splitlines()[0]
    assert "key_name" in first_line


def test_usage_group_by_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    tc = _admin_client(monkeypatch, UsageFakeDB())
    assert tc.get("/admin/usage", params={"group_by": "bogus"}, headers=_ah()).status_code == 400
    assert tc.get("/admin/usage", params={"format": "xlsx"}, headers=_ah()).status_code == 400


# ---------------------------------------------------------------------------
# P1.12: breaker state survives replace_pools (registry refresh)
# ---------------------------------------------------------------------------


def test_replace_pools_carries_breaker_state() -> None:
    import time as time_mod

    r = Router()
    dead = Endpoint(id="ep1", model_name="m", base_url="http://a/v1")
    dead.consecutive_failures = 3
    dead.cooldown_until = time_mod.monotonic() + 60
    r.set_endpoints("m", [dead])
    r.replace_pools({"m": [Endpoint(id="ep1", model_name="m", base_url="http://a/v1")]})
    pool = r.pool("m")
    assert pool is not None
    ep = pool.endpoints[0]
    assert ep.consecutive_failures == 3, "refresh must NOT reset a failing endpoint's strikes"
    assert ep.cooldown_until > time_mod.monotonic(), "cooldown must survive the swap"
    # different endpoint id -> fresh state
    r.replace_pools({"m": [Endpoint(id="ep9", model_name="m", base_url="http://a/v1")]})
    pool9 = r.pool("m")
    assert pool9 is not None
    assert pool9.endpoints[0].consecutive_failures == 0


# ---------------------------------------------------------------------------
# P1.10: rate_windows pruned opportunistically on registry.refresh
# ---------------------------------------------------------------------------


class PruneFakeDB:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.executed_args: list[tuple] = []

    async def fetch(self, sql: str, *a: Any):
        return []

    async def execute(self, sql: str, *a: Any):
        self.executed.append(sql)
        self.executed_args.append(a)
        return "DELETE 0"


def test_housekeeping_prunes_rate_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Moved OUT of registry.refresh (audit finding 16): the piggyback prune
    only fired on boot + admin writes; an idle-admin deployment grew
    rate_windows for the process lifetime. The housekeeping loop owns it
    now (60s cadence, error-swallowed)."""
    from pcai_llm.core import housekeeping

    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    asyncio.run(housekeeping._sweep_once())
    assert any("DELETE FROM rate_windows" in s and "2 minutes" in s for s in fake.executed)


def test_housekeeping_swallows_prune_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same never-fail contract, now on the loop (audit finding 16)."""
    from pcai_llm.core import housekeeping

    class BrokenPrune(PruneFakeDB):
        async def execute(self, sql: str, *a: Any):
            if "rate_windows" in sql:
                raise RuntimeError("db hiccup")
            return "DELETE 0"

    monkeypatch.setattr(housekeeping, "db", BrokenPrune())
    asyncio.run(housekeeping._sweep_once())  # must NOT raise


# ---------------------------------------------------------------------------
# P1.9: pool sizing settings + bounded acquire + 503 mapping
# ---------------------------------------------------------------------------


def test_settings_db_pool_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PCAI_LLM_DB_POOL_MIN", "2")
    monkeypatch.setenv("PCAI_LLM_DB_POOL_MAX", "7")
    s = Settings(database_url="postgresql://t:t@localhost/t")
    assert s.db_pool_min == 2 and s.db_pool_max == 7
    monkeypatch.delenv("PCAI_LLM_DB_POOL_MIN")
    monkeypatch.delenv("PCAI_LLM_DB_POOL_MAX")
    s2 = Settings(database_url="postgresql://t:t@localhost/t")
    assert s2.db_pool_min == 5 and s2.db_pool_max == 25  # defaults


class NeverPool:
    async def acquire(self):  # pragma: no cover - never returns
        await asyncio.Event().wait()

    async def release(self, conn: Any) -> None:  # pragma: no cover
        pass


def test_bounded_acquire_raises_pool_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "db_acquire_timeout_s", 0.05)
    monkeypatch.setattr(db_mod, "pool", NeverPool())
    with pytest.raises(PoolUnavailable):
        asyncio.run(db_mod.fetchval("SELECT 1"))


def test_pool_unavailable_maps_to_503() -> None:
    from pcai_llm.app import create_app

    app = create_app()
    assert PoolUnavailable in app.exception_handlers
    handler = app.exception_handlers[PoolUnavailable]
    request = routes_proxy.Request({"type": "http", "method": "GET", "path": "/v1/chat/completions", "headers": []})
    result = handler(request, PoolUnavailable("exhausted"))
    assert isinstance(result, Awaitable)  # registered PoolUnavailable handler is async
    resp = asyncio.run(result)
    assert resp.status_code == 503


def test_duration_header_stamped_on_responses() -> None:
    """Every response carries X-PCAI-Duration-Ms (true server handling time).
    The console's [slow] telemetry reads this instead of client wall time,
    which conflates VPN/ingress connection setup with handler latency."""
    from fastapi.testclient import TestClient

    from pcai_llm.app import create_app

    tc = TestClient(create_app(), raise_server_exceptions=False)
    r = tc.get("/health")
    assert r.status_code == 200
    ms = r.headers.get("x-pcai-duration-ms")
    assert ms is not None and ms.isdigit() and int(ms) >= 0


def test_duration_header_middleware_is_pure_asgi() -> None:
    """The duration middleware must stay pure ASGI (no BaseHTTPMiddleware).

    The decorator form wrapped every request — SSE streams and the /mcp mount
    included — in Starlette's memory-stream/task layer, with known streaming
    and cancellation edge cases. This pins the replacement: the app's
    middleware registration holds DurationHeaderMiddleware itself, so a future
    regression to the decorator form fails here. (The runtime stack is built
    lazily by Starlette; user_middleware is the registration record.)"""
    from pcai_llm.app import DurationHeaderMiddleware, create_app

    app = create_app()
    registered = [m.cls for m in app.user_middleware]
    assert DurationHeaderMiddleware in registered, (
        f"DurationHeaderMiddleware not registered; got {registered} — the BaseHTTPMiddleware form is back"
    )
    # ...and it must be the OUTERMOST layer (registered first = wrapped last).
    assert registered[0] is DurationHeaderMiddleware


def test_duration_header_stamped_before_stream_body() -> None:
    """Streaming responses carry the header on http.response.start, so it is
    visible BEFORE the body streams (unchanged contract from the decorator
    version) — and body frames pass through untouched. Raw ASGI app on
    purpose: the contract under test is the middleware's, not SSE's."""
    import asyncio

    from starlette.types import Message, Receive, Scope, Send

    from pcai_llm.app import DurationHeaderMiddleware

    async def stream_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b'data: {"chunk": 1}\n\n', "more_body": True})
        await send({"type": "http.response.body", "body": b'data: {"chunk": 2}\n\n', "more_body": False})

    captured: list[tuple[str, Message]] = []

    async def capture_send(message: Message) -> None:
        captured.append((message["type"], message))

    async def noop_receive() -> Message:  # never called by the streaming app
        return {"type": "http.request"}

    async def run() -> None:
        wrapped = DurationHeaderMiddleware(stream_app)
        scope: Scope = {"type": "http", "method": "GET", "path": "/stream", "headers": []}
        await wrapped(scope, noop_receive, capture_send)

    asyncio.run(run())
    kinds = [k for k, _ in captured]
    assert kinds[0] == "http.response.start"
    start = dict(captured)["http.response.start"]
    header_names = [k.lower() for k, _ in start["headers"]]
    assert b"x-pcai-duration-ms" in header_names
    body = b"".join(m.get("body", b"") for t, m in captured if t == "http.response.body")
    assert b'"chunk": 1' in body and b'"chunk": 2' in body  # frames pass through intact


def test_duration_header_passes_through_non_http_scopes() -> None:
    """lifespan/websocket scopes bypass the wrapper untouched (the /mcp
    streamable-http app and the /metrics mount must see the real scope)."""
    import asyncio

    from starlette.types import Message, Receive, Scope, Send

    from pcai_llm.app import DurationHeaderMiddleware

    seen: list[Scope] = []

    async def probe_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope)

    async def noop_receive() -> Message:
        return {"type": "lifespan.startup"}

    async def noop_send(message: Message) -> None:
        pass

    async def run() -> None:
        wrapped = DurationHeaderMiddleware(probe_app)
        await wrapped({"type": "lifespan"}, noop_receive, noop_send)
        await wrapped({"type": "websocket", "path": "/x", "headers": []}, noop_receive, noop_send)

    asyncio.run(run())
    assert [s["type"] for s in seen] == ["lifespan", "websocket"]  # scope objects, unmodified


# ---------------------------------------------------------------------------
# P1.18: X-RateLimit-* headers on admitted calls (plain JSON path)
# ---------------------------------------------------------------------------


MODEL = "hdr-model"


class RateHeaderFakeDB:
    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return None
        if "rate_windows" in sql:
            return {"requests": 3, "tokens": 100}
        return None

    async def fetchval(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        return "INSERT 0 1"


def test_plain_chat_emits_rate_limit_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = RateHeaderFakeDB()
    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-X",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod
    from pcai_llm.core import costs as costs_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
        )

    r = Router.with_mock(handler)
    r.set_endpoints(MODEL, [Endpoint(id="ep", model_name=MODEL, base_url="http://fake/v1", model_type="chat")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    routes_proxy._in_flight.clear()

    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-ratelimit-remaining-requests") == "997"  # 1000 - 3
    assert resp.headers.get("x-ratelimit-remaining-tokens") == "999900"


# ---------------------------------------------------------------------------
# usage-detail passthrough (plain paths) — cached_tokens must reach the
# ledger row on the NON-relay chat paths too: the relay settled it since
# 0.9.0 (toolrelay._cached_tokens) but _stream_chat and _proxy_json
# flattened usage.prompt_tokens_details away, so a high-cache-hit-rate
# deployment ledgered cached_tokens=0 everywhere outside the relay.
# ---------------------------------------------------------------------------


class UsageCaptureFakeDB:
    """Fake db that ACCUMULATES settle() INSERT arg tuples (column order per
    core.budgets.settle: 0 key_id, 1 model, 2 endpoint, 3 prompt, 4
    completion, 5 total, 6 cached_tokens, 7 cost, 8 status)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetchval(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        if "usage_ledger" in sql:
            self.rows.append(a)
        return "INSERT 0 1"


def _plain_chat_env(monkeypatch: pytest.MonkeyPatch, fake: Any, handler: Any) -> TestClient:
    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-X",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod
    from pcai_llm.core import costs as costs_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    r = Router.with_mock(handler)
    r.set_endpoints(MODEL, [Endpoint(id="ep", model_name=MODEL, base_url="http://fake/v1", model_type="chat")])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    routes_proxy._in_flight.clear()
    app = FastAPI()
    app.include_router(routes_proxy.router)
    return TestClient(app, raise_server_exceptions=False)


def test_plain_nonstream_settles_cached_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain non-streaming chat call whose upstream reports
    prompt_tokens_details.cached_tokens settles the ledger row with that
    cached_tokens (usage_ledger INSERT column 6) — not 0."""
    fake = UsageCaptureFakeDB()
    usage = {
        "prompt_tokens": 9000,
        "completion_tokens": 100,
        "total_tokens": 9100,
        "prompt_tokens_details": {"cached_tokens": 4471},
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": usage})

    tc = _plain_chat_env(monkeypatch, fake, handler)
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200, resp.text
    ok_rows = [row for row in fake.rows if row[8] == "ok"]
    assert ok_rows, "the request settled"
    assert ok_rows[0][6] == 4471, f"cached_tokens must reach the ledger row; got {ok_rows[0][6]}"


def test_plain_stream_settles_cached_tokens_and_negotiates_stream_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain STREAMING chat call: the gateway must ADD
    stream_options.include_usage (the client never sent it) so the upstream's
    final usage chunk reaches the _StreamLedger, and the settled row carries
    prompt_tokens_details.cached_tokens. An upstream that rejects
    stream_options is retried WITHOUT it (same negotiation stream_once
    applies) — the turn still succeeds."""
    fake = UsageCaptureFakeDB()
    usage = {
        "prompt_tokens": 9000,
        "completion_tokens": 100,
        "total_tokens": 9100,
        "prompt_tokens_details": {"cached_tokens": 4471},
    }
    attempts: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        attempts.append(body)
        if "stream_options" in body:
            return httpx2.Response(400, json={"error": {"message": "stream_options unsupported"}})
        chunks = (
            b'data: {"model":"m","choices":[{"delta":{"content":"hi"}}]}\n\n'
            b"data: " + json.dumps({"model": "m", "choices": [], "usage": usage}).encode() + b"\n\n"
            b"data: [DONE]\n\n"
        )
        return httpx2.Response(200, content=chunks, headers={"content-type": "text/event-stream"})

    tc = _plain_chat_env(monkeypatch, fake, handler)
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200, resp.text
    assert "hi" in resp.text, "the stream content still reached the client"
    # first attempt carried include_usage; the retry dropped it (negotiation)
    assert attempts and "stream_options" in attempts[0]
    assert attempts[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in attempts[1]
    ok_rows = [row for row in fake.rows if row[8] == "ok"]
    assert ok_rows, "the stream settled"
    assert ok_rows[0][6] == 4471, f"cached_tokens must reach the ledger row; got {ok_rows[0][6]}"


def test_plain_stream_absent_details_keep_cached_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """No usage detail -> the settled row keeps cached_tokens=0 (no
    fabricated facts) while the real prompt/completion tokens still land."""
    fake = UsageCaptureFakeDB()

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        chunks = (
            b'data: {"model":"m","choices":[{"delta":{"content":"hi"}}]}\n\n'
            b"data: "
            + json.dumps({"model": "m", "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
            + b"\n\n"
            b"data: [DONE]\n\n"
        )
        assert "stream_options" in body
        return httpx2.Response(200, content=chunks, headers={"content-type": "text/event-stream"})

    tc = _plain_chat_env(monkeypatch, fake, handler)
    resp = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"Authorization": "Bearer x"},
    )
    assert resp.status_code == 200, resp.text
    ok_rows = [row for row in fake.rows if row[8] == "ok"]
    assert ok_rows and ok_rows[0][6] == 0, "no detail means cached stays 0"
    assert ok_rows[0][3] == 7 and ok_rows[0][4] == 3, "real usage tokens still settle"


# ---------------------------------------------------------------------------
# P1.11: _StreamLedger pre-filter — usage capture unaffected, parses saved
# ---------------------------------------------------------------------------


def test_stream_ledger_prefilter_skips_content_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-X",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=60,
        rate_limit_tpm=100000,
        max_concurrent=0,
        expires_at=None,
    )
    ledger = routes_proxy._StreamLedger(key, "m", 5, "rid", 0.0)
    real_loads = json.loads
    calls = {"n": 0}

    def counting(s: Any) -> Any:
        calls["n"] += 1
        return real_loads(s)

    monkeypatch.setattr(json, "loads", counting)
    for _ in range(10):
        ledger.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n')  # skipped
    ledger.feed(b'data: {"usage": {"prompt_tokens": 3, "completion_tokens": 4}}\n')
    ledger.feed(b"data: [DONE]\n")
    assert calls["n"] == 1, "content deltas must skip json.loads entirely"
    assert ledger.usage == {"prompt_tokens": 3, "completion_tokens": 4}
    assert ledger.done_chunks == 1


# ---------------------------------------------------------------------------
# Defer-item minimum: failed-admin-auth warning + short-key startup warning
# ---------------------------------------------------------------------------


def test_failed_admin_auth_logs_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    tc = _admin_client(monkeypatch, EndpointsFakeDB())
    with caplog.at_level(logging.WARNING, logger="pcai_llm.api.routes_admin"):
        tc.get("/admin/endpoints", headers={"Authorization": "Bearer wrong"})
    assert any("admin auth failed" in rec.message for rec in caplog.records)
    # no key material in the log
    assert all("wrong" not in rec.getMessage() for rec in caplog.records)


def test_short_admin_key_startup_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "short-key, " + "x" * 40)
    with caplog.at_level(logging.WARNING, logger="pcai_llm.api.routes_admin"):
        routes_admin.warn_short_admin_keys()
    msgs = [rec.getMessage() for rec in caplog.records if "shorter than 32" in rec.message]
    assert msgs and "1 configured admin key" in msgs[0]
    assert "short-key" not in msgs[0], "no key material in the warning"


# ---------------------------------------------------------------------------
# P2.19: budget-gate consolidation — two aggregate queries, cascade gate reuse
# ---------------------------------------------------------------------------


def test_check_budgets_is_two_aggregates_not_nplus1(monkeypatch: pytest.MonkeyPatch) -> None:
    """perf#1: the gate ran one serial SUM per budget row (~14 round-trips
    for 2+2 budgets); it must be exactly TWO aggregate queries (one per
    budget kind) regardless of budget count."""

    class CountingDB(BudgetFakeDB):
        def __init__(self) -> None:
            super().__init__()
            self.fetch_count = 0

        async def fetch(self, sql: str, *a: Any):
            self.fetch_count += 1
            return await super().fetch(sql, *a)

    fake = CountingDB()
    fake.token_budgets = [
        {"model": "*", "period": "month", "limit_tokens": 1000},
        {"model": "m1", "period": "day", "limit_tokens": 500},
        {"model": "m2", "period": "week", "limit_tokens": 900},
    ]
    fake.usd_budgets = [
        {"model": "*", "period": "month", "limit_usd": 10.0},
        {"model": "m1", "period": "day", "limit_usd": 2.0},
    ]
    monkeypatch.setattr(budgets_mod, "db", fake)
    res = asyncio.run(budgets_mod.check_budgets(uuid.uuid4(), "m1", 0))
    assert res.ok
    assert fake.fetch_count == 2, f"expected 2 aggregate queries, got {fake.fetch_count}"


def test_run_gates_skips_recheck_with_precomputed_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """perf#1 second half: the cascade already gated the pinned model —
    _run_gates must reuse that GateResult instead of re-running the SUMs."""
    budget_calls = {"n": 0}

    class NoBudgetDB(RateHeaderFakeDB):
        async def fetch(self, sql: str, *a: Any):
            if "FROM budgets" in sql or "FROM cost_budgets" in sql:
                budget_calls["n"] += 1
            return []

    monkeypatch.setattr(budgets_mod, "db", NoBudgetDB())
    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-X",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )
    gate = asyncio.run(
        routes_proxy._run_gates(
            key, MODEL, 10, "chat", "rid", 0.0, precomputed_budget=budgets_mod.GateResult(True, warn=95)
        )
    )
    assert gate.warn_level == 95, "precomputed warn level must flow through"
    assert budget_calls["n"] == 0, "check_budgets must NOT run again"


def test_cascade_resolution_carries_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_cascade returns the winning candidate's GateResult so the
    proxy can skip the redundant recheck."""
    import pcai_llm.core.cascade as cascade_mod

    class CascadeDB:
        async def fetchrow(self, sql: str, *a: Any):
            if "profiles" in sql:  # JOIN profiles p ... SELECT p.model_order
                return {"model_order": ["m1", "m2"]}
            return None

        async def fetch(self, sql: str, *a: Any):
            return []  # no budgets -> headroom everywhere

    monkeypatch.setattr(cascade_mod, "db", CascadeDB())
    monkeypatch.setattr(budgets_mod, "db", CascadeDB())  # check_budgets reads budgets.db
    res = asyncio.run(cascade_mod.resolve_cascade(uuid.uuid4(), "m1", {"m1", "m2"}, [], 10))
    assert res.ok and res.model == "m1"
    assert res.gate is not None and res.gate.ok, "pinned resolution must carry its gate"


def test_relay_wants_three_state_matrix() -> None:
    """Per-request header wins; absent header falls back to the key's
    relay_default; the global kill-switch beats everything (feature: per-key
    relay default so off-cluster consumers like sproket relay with ZERO
    client config while local harnesses keep their own tools)."""
    from pcai_llm.api.routes_proxy import _relay_wants

    # header forced on — even for a default-off key
    assert _relay_wants("1", False, True) is True
    # explicit opt-out beats a default-on key
    assert _relay_wants("0", True, True) is False
    # absent header -> the key's default
    assert _relay_wants(None, True, True) is True
    assert _relay_wants(None, False, True) is False
    # unknown header values never coerce the relay ON (fail toward local)
    assert _relay_wants("true", True, True) is True  # falls back to key default
    assert _relay_wants("true", False, True) is False
    # case/whitespace tolerance on the two real values
    assert _relay_wants(" 1 ", False, True) is True
    assert _relay_wants("0", True, True) is False
    # global kill-switch wins over everything
    assert _relay_wants("1", True, False) is False
    assert _relay_wants(None, True, False) is False


def test_patch_key_relay_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """PATCH relay_default persists and round-trips in the RETURNING row."""
    fake = KeyPatchFakeDB()
    tc = _admin_client(monkeypatch, fake)
    r = tc.patch(
        f"/admin/keys/{fake.patched_key_id}",
        json={"relay_default": True},
        headers=_ah(),
    )
    assert r.status_code == 200, r.text
    sql, args = fake.updates[0]
    assert "relay_default = $" in sql
    assert args[0] is True
    # the fake's RETURNING row is static — assert the PROJECTION exposes the
    # column (the DB does the actual persisting)
    assert "relay_default" in r.json()


def test_offpeak_key_fraction_precedence() -> None:
    """Profile fraction beats the env default; unset profile inherits the
    env default; the demand state gates BOTH via effective()."""
    from pcai_llm.core import offpeak
    from pcai_llm.core.keys import KeyRecord

    def rec(pf: float | None) -> KeyRecord:
        import uuid as _u

        # Keyword for the trailing fields: the positional order changed when
        # stream_pcai_events joined KeyRecord (RFC-PER-KEY-STREAM-EVENTS) —
        # positional 0.25 would land on the new flag, not offpeak_fraction.
        return KeyRecord(
            _u.uuid4(),
            _u.uuid4(),
            "k",
            "hllm-x",
            ["proxy"],
            [],
            60,
            1000,
            0,
            None,
            relay_default=False,
            stream_pcai_events=True,
            offpeak_fraction=pf,
        )

    assert offpeak.key_fraction(rec(0.25)) == 0.25  # profile wins
    assert offpeak.key_fraction(rec(None)) == 0.5  # env default (0.5)

    monkey = pytest.MonkeyPatch()
    try:
        # INACTIVE window: everything bills full price, whatever the base
        monkey.setattr(offpeak.state, "enabled", True)
        monkey.setattr(offpeak.state, "active", False)
        assert offpeak.effective(0.25) == 1.0
        # ACTIVE window: the key's base applies
        monkey.setattr(offpeak.state, "active", True)
        assert offpeak.effective(0.25) == 0.25
        assert offpeak.effective(offpeak.key_fraction(rec(None))) == 0.5
    finally:
        monkey.undo()


def test_create_profile_roundtrips_offpeak_fraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /admin/profiles persists + returns offpeak_fraction."""

    class ProfFakeDB:
        def __init__(self) -> None:
            self.last: tuple = ()

        async def fetchrow(self, sql: str, *args: Any):
            if "INSERT INTO profiles" in sql:
                self.last = (sql, args)
                return {
                    "id": uuid.uuid4(),
                    "name": args[0],
                    "model_order": args[1],
                    "defaults_json": args[2],
                    "offpeak_fraction": args[3],
                }
            return None

        async def fetch(self, sql: str, *a: Any):
            return []

    fake = ProfFakeDB()
    tc = _admin_client(monkeypatch, fake)
    r = tc.post(
        "/admin/profiles",
        json={
            "name": "team-alpha",
            "model_order": [],
            "budgets": [],
            "mcp_grants": [],
            "offpeak_fraction": 0.5,
        },
        headers=_ah(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["offpeak_fraction"] == 0.5
    sql, args = fake.last
    assert "offpeak_fraction = $4" in sql
    assert args[3] == 0.5


def test_retention_prunes_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """ROADMAP §5 contract: NOTHING is deleted until the operator opts in.
    Default retention days = 0 -> the prunes never run, and the usage ledger
    has no prune path at all (grep-level guarantee asserted on the SQL seen)."""
    fake = PruneFakeDB()
    monkeypatch.setattr(registry, "db", fake)
    monkeypatch.setattr(settings, "retention_trace_days", 0)
    monkeypatch.setattr(settings, "retention_conversation_days", 0)
    asyncio.run(registry.refresh())
    sqls = fake.executed
    assert not any("DELETE FROM agent_traces" in s for s in sqls)
    assert not any("DELETE FROM conversations" in s for s in sqls)
    # the ledger is never pruned — no DELETE touches it, ever
    assert not any("DELETE FROM usage_ledger" in s for s in sqls)


def test_retention_prunes_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive day counts prune traces + conversations (messages follow via
    ON DELETE CASCADE) — now through the housekeeping loop (audit finding 16;
    was the registry-refresh piggyback)."""
    from pcai_llm.core import housekeeping

    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_trace_days", 30)
    monkeypatch.setattr(settings, "retention_conversation_days", 14)
    asyncio.run(housekeeping._sweep_once())
    sqls = fake.executed
    trace_del = next(s for s in sqls if "DELETE FROM agent_traces" in s)
    conv_del = next(s for s in sqls if "DELETE FROM conversations" in s)
    assert "make_interval" in trace_del and "make_interval" in conv_del
    # ledger untouched
    assert not any("DELETE FROM usage_ledger" in s for s in sqls)

# ---------------------------------------------------------------------------
# F6 (audit 2026-10-29): /mcp rate-limit refusals settle "rate_limited"
# ---------------------------------------------------------------------------


def test_f6_mcp_rate_limit_refusal_settles_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rate-gate refusal on /mcp settled outcome "error" — inflating the
    error-rate alert for what is tenant policy activity, not a gateway
    failure. The chat path settles "rate_limited" (routes_proxy:639); the
    /mcp surface must agree. Refusal itself still raises ToolError."""
    from types import SimpleNamespace

    from mcp.server.mcpserver.exceptions import ToolError

    from pcai_llm.api.auth import KeyRecord

    settled: list[tuple[str, str]] = []

    async def fake_settle_quiet(key_id, endpoint, status, started, request_id, **kwargs):
        settled.append((endpoint, status))

    async def gate_full(*a, **k):
        return SimpleNamespace(ok=False, reason="RPM limit exceeded (60/min)")

    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-F6",
        scopes=["mcp"],
        allowed_models=[],
        rate_limit_rpm=60,
        rate_limit_tpm=100_000,
        max_concurrent=0,
        expires_at=None,
    )
    monkeypatch.setattr(routes_mcp, "_current_key", lambda: key)
    monkeypatch.setattr(routes_mcp, "check_rate_limit", gate_full)
    monkeypatch.setattr(routes_mcp, "_settle_quiet", fake_settle_quiet)

    token = routes_mcp._caller.set(key)
    try:
        with pytest.raises(ToolError, match="rate limited"):
            asyncio.run(routes_mcp._gated_mcp_call("prometheus", "query", {"query": "up"}))
    finally:
        routes_mcp._caller.reset(token)

    assert settled == [("mcp:prometheus:query", "rate_limited")], settled
