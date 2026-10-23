"""REMEDIATION 11c — the ``mcp_catalog`` inventory is ACL-FILTERED per server.

The verified gap: zero-grant servers were already hidden (the
``visible_servers`` filter), but for any server where the key held AT LEAST
ONE grant the handler appended the server's FULL raw tool listing plus its
internal URL. A one-grant key therefore saw every tool name/schema the
upstream exposed and the in-cluster service URL. Calls were never at risk
(the gate chain is unchanged) — this was inventory disclosure only.

The tests below pin the closure, matching the no-leak discipline
``_flat_tools`` already had (see tests/test_mcp_flat.py):
  * 1 grant on a 3-tool server -> exactly that ONE tool in servers[];
  * zero-grant server -> absent entirely (name AND url appear nowhere);
  * all-granted server -> all tools, url present (unchanged behavior);
  * the "unavailable" error marker and the {"servers", "granted"} response
    shape survive;
  * a key with grants on two servers is filtered independently per server.

Fakes follow the established pattern from tests/test_mcp_flat.py: the
collaborators (auth key, load_grants, registry snapshot, upstream listings)
are monkeypatched, so no Postgres and no live upstream are involved.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from pcai_llm.api import routes_mcp
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import registry
from pcai_llm.mcpf import federation

# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_listing_cache():
    """The TTL listing cache outlives a request by design; clear it (and
    restore the registry snapshot) so tests stay isolated."""
    routes_mcp.listing_cache_clear()
    saved = registry._mcp_servers_cache
    yield
    routes_mcp.listing_cache_clear()
    registry._mcp_servers_cache = saved


def _key() -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-CF",
        scopes=["mcp"],
        allowed_models=[],
        rate_limit_rpm=60,
        rate_limit_tpm=100_000,
        max_concurrent=0,
        expires_at=None,
    )


def _server(name: str, *, priority: int = 0) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "url": f"http://{name}.svc.cluster.local:9100/mcp",
        "headers": {},
        "auth_secret": None,
        "auth_token_enc": None,
        "enabled": True,
        "tool_policy": "all",
        "allowed_tools": [],
        "priority": priority,
    }


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"tool": n, "description": f"{n} does {n} things"} for n in names]


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    grants: set[tuple[str, str]],
    servers: list[dict[str, Any]],
    listings: dict[str, list[dict]] | None = None,
) -> None:
    """Wire _mcp_catalog's collaborators: ACL grants (federation.load_grants
    reads module-level ``db``), the registry snapshot, and the upstream
    listings (matched by a substring of the server URL)."""

    class GrantsDB:
        async def fetch(self, sql: str, *a: Any):
            return []

    async def fake_fetch(url: str, headers: dict, timeout_s: float | None = None):
        for fragment, tools in (listings or {}).items():
            if fragment in url:
                return tools
        raise AssertionError(f"unexpected listing fetch for {url}")

    monkeypatch.setattr(federation, "db", GrantsDB())

    # The grants are planted on the pure layer's caller-visible seam: the
    # handler imported load_grants by name, so patch it there.
    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return grants

    monkeypatch.setattr(routes_mcp, "load_grants", fake_grants)
    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch)
    registry._mcp_servers_cache = servers


def _catalog(key: KeyRecord) -> str:
    """Run the handler under an authenticated caller (the auth wrapper sets
    this contextvar in production)."""
    set_caller = routes_mcp._caller.set
    cv_token = set_caller(key)
    try:
        return asyncio.run(routes_mcp._mcp_catalog())
    finally:
        routes_mcp._caller.reset(cv_token)


# ---------------------------------------------------------------------------
# REMEDIATION 11c — per-server inventory filtering
# ---------------------------------------------------------------------------


def test_catalog_one_grant_on_three_tool_server_shows_only_that_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE finding: a single grant on a 3-tool server must expose exactly
    that one tool — not the upstream's full inventory."""
    key = _key()
    _patch(
        monkeypatch,
        {("alpha", "search")},
        [_server("alpha")],
        listings={"alpha": _tools("search", "delete_everything", "admin_reset")},
    )
    out = json.loads(_catalog(key))
    assert [s["server"] for s in out["servers"]] == ["alpha"]
    tools = out["servers"][0]["tools"]
    assert [t["tool"] for t in tools] == ["search"]
    # the server URL survives for a GRANTED server (operators debug with it)
    assert out["servers"][0]["url"] == "http://alpha.svc.cluster.local:9100/mcp"
    # and the ungranted inventory leaks NOWHERE in the raw response
    assert "delete_everything" not in json.dumps(out)
    assert "admin_reset" not in json.dumps(out)
    assert "admin_reset does admin_reset things" not in json.dumps(out)
    # the flat granted list carries the granted pair only
    assert [(g["server"], g["tool"]) for g in out["granted"]] == [("alpha", "search")]


def test_catalog_zero_grant_server_absent_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server with zero grants appears in NEITHER servers[] nor granted[] —
    no name, no url, no tools (the previously-fixed half, re-pinned)."""
    key = _key()
    _patch(
        monkeypatch,
        {("alpha", "search")},
        [_server("alpha", priority=0), _server("beta", priority=5)],
        listings={"alpha": _tools("search"), "beta": _tools("query")},
    )
    raw = _catalog(key)
    out = json.loads(raw)
    assert [s["server"] for s in out["servers"]] == ["alpha"]
    assert "beta" not in raw
    assert "query" not in raw


def test_catalog_all_granted_server_shows_all_tools_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged behavior for a fully-granted server: every tool + the url."""
    key = _key()
    _patch(
        monkeypatch,
        {("alpha", "search"), ("alpha", "fetch"), ("alpha", "query")},
        [_server("alpha")],
        listings={"alpha": _tools("search", "fetch", "query")},
    )
    out = json.loads(_catalog(key))
    entry = out["servers"][0]
    assert entry["url"] == "http://alpha.svc.cluster.local:9100/mcp"
    assert [t["tool"] for t in entry["tools"]] == ["search", "fetch", "query"]
    assert [(g["server"], g["tool"]) for g in out["granted"]] == [
        ("alpha", "search"),
        ("alpha", "fetch"),
        ("alpha", "query"),
    ]


def test_catalog_filters_each_server_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grants on two servers are applied per server — a server's own raw
    inventory never bleeds into another's, nor past its own grant set."""
    key = _key()
    _patch(
        monkeypatch,
        {("alpha", "search"), ("beta", "query")},
        [_server("alpha", priority=0), _server("beta", priority=5)],
        listings={"alpha": _tools("search", "extra_a"), "beta": _tools("extra_b", "query")},
    )
    out = json.loads(_catalog(key))
    assert [s["server"] for s in out["servers"]] == ["alpha", "beta"]
    assert [t["tool"] for t in out["servers"][0]["tools"]] == ["search"]
    assert [t["tool"] for t in out["servers"][1]["tools"]] == ["query"]
    raw = json.dumps(out)
    assert "extra_a" not in raw and "extra_b" not in raw


def test_catalog_unavailable_marker_and_shape_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A granted-but-dead server keeps the stable {"server", "error":
    "unavailable"} entry (no url, no tools, no raw exception text), and the
    response shape stays {"servers", "granted"}."""
    key = _key()

    class GrantsDB:
        async def fetch(self, sql: str, *a: Any):
            return []

    async def dead_fetch(url: str, headers: dict, timeout_s: float | None = None):
        raise RuntimeError("connect to 10.0.0.7:9100 refused")  # must not reach the tenant

    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return {("alpha", "search")}

    monkeypatch.setattr(federation, "db", GrantsDB())
    monkeypatch.setattr(routes_mcp, "load_grants", fake_grants)
    monkeypatch.setattr(routes_mcp, "_fetch_tools", dead_fetch)
    registry._mcp_servers_cache = [_server("alpha")]

    raw = _catalog(key)
    out = json.loads(raw)
    assert set(out.keys()) == {"servers", "granted"}
    assert out["servers"] == [{"server": "alpha", "error": "unavailable"}]
    assert out["granted"] == []
    # REDACTED: the internal host from the exception never reaches the tenant
    assert "10.0.0.7" not in raw


def test_catalog_zero_grants_still_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-deny fast path unchanged: no grants -> no servers, no urls."""
    key = _key()
    _patch(monkeypatch, set(), [_server("alpha")], listings={"alpha": _tools("search")})
    out = json.loads(_catalog(key))
    assert out["servers"] == [] and out["granted"] == []
