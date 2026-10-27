"""Flat per-tool MCP surface (the approved design): naming, listing, calls,
cache, failover — and the contextvar evidence the module docstring cites.

Shape notes for the HTTP tests: the gateway serves the stateless streamable-
http app with json_response=True, so every POST is one JSON-RPC exchange. The
installed SDK (2026-07-28 "modern" protocol era) requires a per-request
envelope — ``params._meta`` carrying protocolVersion + clientCapabilities and
matching ``MCP-Protocol-Version`` / ``Mcp-Method`` (+ ``Mcp-Name`` for
tools/call) headers — and serves it WITHOUT an initialize handshake
(mcp/server/_streamable_http_modern.py).

test_flat_listing_http_contextvar_flows is the pinned EVIDENCE for design §3:
the flattened tool can only appear when the auth wrapper's ``_caller``
contextvar is visible INSIDE the SDK's tools/list dispatch path — if it were
not, _flat_tools would raise (no authenticated key) and the fail-safe would
degrade the listing to the two gateway tools, failing the assertion.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_mcp
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import registry
from pcai_llm.core.budgets import GateResult
from pcai_llm.mcpf.federation import is_allowed

PV = "2026-07-28"
_META = {"io.modelcontextprotocol/protocolVersion": PV, "io.modelcontextprotocol/clientCapabilities": {}}


# ---------------------------------------------------------------------------
# fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_surface():
    """Isolate the module-level state the flat surface owns: the TTL listing
    cache, the sanitized-name map, and the registry snapshot cache the
    helper plants (all outlive a single request by design, so tests must
    clear/restore them explicitly)."""
    routes_mcp.listing_cache_clear()
    routes_mcp._sanitized_flat_names.clear()
    saved_registry = registry._mcp_servers_cache
    yield
    routes_mcp.listing_cache_clear()
    routes_mcp._sanitized_flat_names.clear()
    registry._mcp_servers_cache = saved_registry


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["mcp"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


def _server(
    name: str,
    *,
    url: str | None = None,
    priority: int = 0,
    tool_policy: str = "all",
    allowed_tools: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "url": url or f"http://{name}.svc.cluster.local:9100/mcp",
        "headers": {},
        "auth_secret": None,
        "auth_token_enc": None,
        "enabled": True,
        "tool_policy": tool_policy,
        "allowed_tools": allowed_tools or [],
        "priority": priority,
    }


def _rpc(i: int, method: str, params: dict | None = None) -> dict:
    p: dict[str, Any] = {"_meta": dict(_META)}
    if params:
        p.update(params)
    return {"jsonrpc": "2.0", "id": i, "method": method, "params": p}


def _headers(method: str | None = None, name: str | None = None) -> dict[str, str]:
    h = {"Authorization": "Bearer flat-test-key", "mcp-protocol-version": PV}
    if method:
        h["mcp-method"] = method
    if name:
        h["mcp-name"] = name
    return h


class _FakeBlock:
    def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
        return {"type": "text", "text": "hi"}


class _FakeResult:
    def __init__(self, *, is_error: bool = False) -> None:
        self.is_error = is_error
        self.content = [_FakeBlock()]


class _FakeSession:
    def __init__(self, result: _FakeResult | None = None) -> None:
        self.result = result or _FakeResult()

    async def call_tool(self, tool: str, arguments: dict) -> _FakeResult:
        return self.result


def _patch_surface(
    monkeypatch: pytest.MonkeyPatch,
    key: KeyRecord,
    grants: set[tuple[str, str]],
    servers: list[dict[str, Any]],
    *,
    listings: dict[str, list[dict]] | None = None,
    upstream: Any = None,
    settles: list[tuple] | None = None,
    rate_calls: list[int] | None = None,
) -> None:
    """Wire the flat surface's collaborators to fakes: auth, ACL grants,
    registry snapshot, upstream listings (per URL substring), upstream call
    session, rate gate and ledger (recorded)."""

    async def fake_authenticate(token: str) -> KeyRecord:
        return key

    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return grants

    async def fake_fetch(url: str, headers: dict, timeout_s: float | None = None):
        for fragment, tools in (listings or {}).items():
            if fragment in url:
                return tools
        raise AssertionError(f"unexpected listing fetch for {url}")

    async def fake_rate(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
        if rate_calls is not None:
            rate_calls.append(1)
        return GateResult(True)

    async def fake_settle(*a: Any, **k: Any) -> None:
        if settles is not None:
            settles.append((a[2], a[5]))  # (endpoint, status)

    async def default_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        return await op(_FakeSession())

    monkeypatch.setattr(routes_mcp, "authenticate", fake_authenticate)
    monkeypatch.setattr(routes_mcp, "load_grants", fake_grants)
    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch)
    monkeypatch.setattr(routes_mcp, "check_rate_limit", fake_rate)
    monkeypatch.setattr(routes_mcp, "settle", fake_settle)
    monkeypatch.setattr(routes_mcp, "_upstream_session", upstream or default_upstream)
    registry._mcp_servers_cache = servers


def _client() -> TestClient:
    """Fresh gateway server per test: the SDK's StreamableHTTPSessionManager
    refuses run() twice on one instance, and each TestClient lifespan runs
    it — the module-level _mcp singleton would fail the second HTTP test."""
    app = FastAPI()
    routes_mcp.mount_server(app, routes_mcp._new_gateway_server())
    return TestClient(app, raise_server_exceptions=False)


def _call_with_key(key: KeyRecord, coro: Any) -> Any:
    """Run one direct gate-chain call under an authenticated caller (the
    auth wrapper normally sets this contextvar; direct unit calls set it
    here — same mechanism test_mcp_creds uses for _mcp_call)."""
    set_caller = routes_mcp._caller.set
    cv_token = set_caller(key)
    try:
        return asyncio.run(coro)
    finally:
        routes_mcp._caller.reset(cv_token)


def _seed_listing(server: dict[str, Any], tools: list[dict]) -> None:
    """Pre-seed the TTL cache with a fresh entry (failover eligibility reads
    the cache WITHOUT fetching — see _peek_cached_tools)."""
    routes_mcp._tools_listing_cache[routes_mcp._listing_cache_key(server)] = (time.monotonic() + 60.0, tools)


# ---------------------------------------------------------------------------
# naming: sanitize + resolve (design §2/§4)
# ---------------------------------------------------------------------------


def test_flat_name_grammar_and_determinism() -> None:
    assert routes_mcp.flat_tool_name("alpha", "search") == "alpha__search"
    # disallowed characters (dots, spaces, slashes...) become underscores
    assert routes_mcp.flat_tool_name("we.ird", "get pod") == "we_ird__get_pod"
    # deterministic: same input, same output (stable across replicas/restarts)
    assert routes_mcp.flat_tool_name("we.ird", "get pod") == "we_ird__get_pod"
    # every produced name satisfies the MCP grammar
    import re

    grammar = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
    for server, tool in (("a", "b"), ("we.ird/x", "get pod"), ("s" * 70, "t" * 70), ("-", "_")):
        assert grammar.match(routes_mcp.flat_tool_name(server, tool)), (server, tool)


def test_flat_name_truncation_deterministic_suffix() -> None:
    raw_server, raw_tool = "s" * 70, "t" * 70  # raw flattened length 142 > 64
    n1 = routes_mcp.flat_tool_name(raw_server, raw_tool)
    n2 = routes_mcp.flat_tool_name(raw_server, raw_tool)
    assert n1 == n2 and len(n1) == 64
    # 56-char body + 8-char sha256 suffix of the FULL raw name
    import hashlib

    raw = f"{raw_server}__{raw_tool}"
    expect_suffix = hashlib.sha256(raw.encode()).hexdigest()[:8]
    assert n1.endswith(expect_suffix)
    assert n1[:56] == "s" * 56  # the raw prefix chars are grammar-legal already


def test_resolve_flat_name_longest_prefix() -> None:
    # server names are unique; the longest matching server prefix wins
    assert routes_mcp.resolve_flat_name("ml-cache__t", ["ml", "ml-cache", "s"]) == ("ml-cache", "t")
    # tool names may themselves contain '__' — only the server boundary splits
    assert routes_mcp.resolve_flat_name("s__a__b", ["s"]) == ("s", "a__b")
    # a bare server prefix with no tool part is NOT a match
    with pytest.raises(routes_mcp.ToolError):
        routes_mcp.resolve_flat_name("s__", ["s"])


def test_resolve_flat_name_sanitized_map_fallback() -> None:
    # 'we.ird' can never prefix-match raw ('we_ird__t' does not start with
    # 'we.ird__') — the map from the last listing resolves it
    routes_mcp._remember_sanitized({"we_ird__t": ("we.ird", "t")})
    assert routes_mcp.resolve_flat_name("we_ird__t", ["alpha"]) == ("we.ird", "t")


def test_resolve_flat_name_unknown_is_tool_error() -> None:
    with pytest.raises(routes_mcp.ToolError):
        routes_mcp.resolve_flat_name("nope__x", ["alpha"])


# ---------------------------------------------------------------------------
# tools/list — flat surface, ACL filtering, no-leak discipline
# ---------------------------------------------------------------------------


def test_flat_listing_http_contextvar_flows(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE design-§3 evidence: the flattened tool appears in a REAL HTTP
    tools/list round trip, which is only possible if the auth wrapper's
    _caller contextvar is visible inside the SDK's list_tools dispatch (on
    any context loss the fail-safe degrades to the two gateway tools and
    this assertion fails). Also pins: schema/annotations carried, and a
    zero-grant server contributes NO tools and NO URL."""
    key = _key()
    servers = [_server("alpha", priority=0), _server("beta", priority=5)]
    listings: dict[str, list[dict]] = {
        "alpha": [
            {
                "tool": "search",
                "description": "search it",
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                "annotations": {"read_only_hint": True},
            },
            {"tool": "fetch", "description": "not granted"},
        ],
        "beta": [{"tool": "query", "description": "d"}],
    }
    _patch_surface(monkeypatch, key, {("alpha", "search")}, servers, listings=listings)
    with _client() as tc:
        r = tc.post("/mcp", json=_rpc(1, "tools/list"), headers=_headers("tools/list"))
    assert r.status_code == 200, r.text
    tools = r.json()["result"]["tools"]
    names = [t["name"] for t in tools]
    # gateway tools stay listed (compat surface), granted flat tool appended
    assert names == ["mcp_catalog", "mcp_call", "alpha__search"]
    flat = tools[2]
    assert flat["inputSchema"] == {"type": "object", "properties": {"q": {"type": "string"}}}
    assert flat["annotations"] == {"readOnlyHint": True}
    assert flat["description"] == "search it"
    # no-leak discipline: the ungranted server's name and URL appear NOWHERE
    assert "beta" not in r.text
    # not-granted tool on a granted server is filtered per-key
    assert "alpha__fetch" not in names


def test_flat_listing_zero_grants_no_upstream_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero grants: exactly the two gateway tools, and ZERO upstream calls
    (default-deny pays no fan-out and surfaces no URLs)."""

    async def no_fetch(url: str, headers: dict, timeout_s: float | None = None):
        raise AssertionError(f"no upstream fetch allowed for a zero-grant key (got {url})")

    key = _key()
    _patch_surface(monkeypatch, key, set(), [_server("alpha")], upstream=no_fetch)
    with _client() as tc:
        r = tc.post("/mcp", json=_rpc(1, "tools/list"), headers=_headers("tools/list"))
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert names == ["mcp_catalog", "mcp_call"]


def test_flat_listing_ungranted_server_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ACL filter applies to the SERVER dimension before any fetch: only
    granted servers are contacted."""
    key = _key()

    async def fake_fetch(url: str, headers: dict, timeout_s: float | None = None):
        if "beta" in url:
            raise AssertionError("ungranted server must not be fetched")
        return [{"tool": "search", "description": "d"}]

    _patch_surface(monkeypatch, key, {("alpha", "search")}, [_server("alpha"), _server("beta")])
    # swap in the discriminating fetch
    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch)
    with _client() as tc:
        r = tc.post("/mcp", json=_rpc(1, "tools/list"), headers=_headers("tools/list"))
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "alpha__search" in names and "beta__search" not in names


def test_flat_listing_missing_schema_gets_object_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream tools without a declared inputSchema still list (MCP Tool
    requires one) with the generic object schema — degrade, don't drop."""
    key = _key()
    _patch_surface(
        monkeypatch,
        key,
        {("alpha", "bare")},
        [_server("alpha")],
        listings={"alpha": [{"tool": "bare", "description": ""}]},
    )
    with _client() as tc:
        r = tc.post("/mcp", json=_rpc(1, "tools/list"), headers=_headers("tools/list"))
    flat = next(t for t in r.json()["result"]["tools"] if t["name"] == "alpha__bare")
    assert flat["inputSchema"] == {"type": "object"}


def test_kill_switch_reverts_listing_and_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """PCAI_LLM_MCP_FLAT_TOOLS=0: tools/list is exactly the two gateway
    tools and a flattened name is an unknown-tool error — the pre-flat
    surface, bit-for-bit."""
    monkeypatch.setattr(settings, "mcp_flat_tools", False)
    key = _key()
    _patch_surface(
        monkeypatch,
        key,
        {("alpha", "search")},
        [_server("alpha")],
        listings={"alpha": [{"tool": "search", "description": "d"}]},
    )
    with _client() as tc:
        r = tc.post("/mcp", json=_rpc(1, "tools/list"), headers=_headers("tools/list"))
        names = [t["name"] for t in r.json()["result"]["tools"]]
        assert names == ["mcp_catalog", "mcp_call"]

        rc = tc.post(
            "/mcp",
            json=_rpc(2, "tools/call", {"name": "alpha__search", "arguments": {}}),
            headers=_headers("tools/call", "alpha__search"),
        )
    assert rc.status_code == 200  # tool-error RESULT, never an HTTP error
    assert rc.json()["result"]["isError"] is True


def test_fetch_tools_captures_annotations(monkeypatch: pytest.MonkeyPatch) -> None:
    """_fetch_tools carries upstream Tool.annotations through (additive key)."""
    from mcp.types import Tool, ToolAnnotations

    class UpSession:
        async def list_tools(self):
            return type(
                "R",
                (),
                {
                    "tools": [
                        Tool(
                            name="search",
                            description="d",
                            input_schema={"type": "object"},
                            annotations=ToolAnnotations(read_only_hint=True, title="Search"),
                        ),
                        Tool(name="bare", description="", input_schema={"type": "object"}),
                    ]
                },
            )()

    async def fake_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        return await op(UpSession())

    monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream)
    out = asyncio.run(routes_mcp._fetch_tools("http://x", {}))
    assert out[0]["annotations"] == {"read_only_hint": True, "title": "Search"}
    assert "annotations" not in out[1]  # absent upstream annotations stay absent
    assert out[0]["inputSchema"] == {"type": "object"}


# ---------------------------------------------------------------------------
# tools/call — flattened names funnel through the ONE gate chain
# ---------------------------------------------------------------------------


def test_flat_call_http_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flattened tools/call resolves to (server, tool) and produces a normal
    (non-error) result whose text is the gate chain's JSON contract; the
    ledger settles under the real mcp:<server>:<tool> endpoint."""
    key = _key()
    settles: list[tuple] = []
    _patch_surface(
        monkeypatch,
        key,
        {("alpha", "search")},
        [_server("alpha")],
        listings={"alpha": [{"tool": "search", "description": "d"}]},
        settles=settles,
    )
    with _client() as tc:
        r = tc.post(
            "/mcp",
            json=_rpc(1, "tools/call", {"name": "alpha__search", "arguments": {"q": "x"}}),
            headers=_headers("tools/call", "alpha__search"),
        )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["ok"] is True and payload["server"] == "alpha" and payload["tool"] == "search"
    assert settles == [("mcp:alpha:search", "ok")]


def test_flat_call_denied_is_error_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-deny on the flattened path: no grant -> tool-error RESULT with
    HTTP 200 (streamable-http clients expect a JSON-RPC result)."""
    key = _key()
    settles: list[tuple] = []
    _patch_surface(monkeypatch, key, set(), [_server("alpha")], settles=settles)
    with _client() as tc:
        r = tc.post(
            "/mcp",
            json=_rpc(1, "tools/call", {"name": "alpha__search", "arguments": {}}),
            headers=_headers("tools/call", "alpha__search"),
        )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "denied" in result["content"][0]["text"]
    # "denied" is its own ledger status since the 2026-10-26 hardening wave
    # (P2-2 fidelity): a policy refusal must be distinguishable from an upstream
    # failure, and must not pollute the error-rate series.
    assert settles == [("mcp:alpha:search", "denied")]


def test_flat_call_unknown_name_is_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _key()
    _patch_surface(monkeypatch, key, {("alpha", "search")}, [_server("alpha")])
    with _client() as tc:
        r = tc.post(
            "/mcp",
            json=_rpc(1, "tools/call", {"name": "ghost__x", "arguments": {}}),
            headers=_headers("tools/call", "ghost__x"),
        )
    assert r.status_code == 200
    assert r.json()["result"]["isError"] is True


def test_flat_call_rate_gate_runs_once_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The RPM gate is per REQUEST: even a failover multi-attempt call must
    consume exactly one rate-window check (the gate chain's position is
    before candidate iteration)."""
    key = _key()
    rate_calls: list[int] = []

    async def broken_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        raise RuntimeError("connection refused")

    _patch_surface(
        monkeypatch,
        key,
        {("alpha", "t"), ("beta", "t")},
        [_server("alpha"), _server("beta")],
        upstream=broken_upstream,
        rate_calls=rate_calls,
    )
    _seed_listing(_server("beta"), [{"tool": "t"}])
    with _client() as tc:
        tc.post(
            "/mcp",
            json=_rpc(1, "tools/call", {"name": "alpha__t", "arguments": {}}),
            headers=_headers("tools/call", "alpha__t"),
        )
    assert len(rate_calls) == 1


# ---------------------------------------------------------------------------
# TTL listing cache (design §5)
# ---------------------------------------------------------------------------


def test_listing_cache_ttl_hit_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str, headers: dict, timeout_s: float | None = None):
        calls.append(url)
        return [{"tool": "t", "description": "d"}]

    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch)
    server = _server("alpha")
    asyncio.run(routes_mcp._cached_upstream_tools(server))
    asyncio.run(routes_mcp._cached_upstream_tools(server))
    assert calls == [server["url"]], "second call within the TTL must be served from cache"

    # expired entry -> refetch
    routes_mcp._tools_listing_cache[routes_mcp._listing_cache_key(server)] = (
        time.monotonic() - 1.0,
        [{"tool": "stale", "description": "d"}],
    )
    out = asyncio.run(routes_mcp._cached_upstream_tools(server))
    assert calls == [server["url"], server["url"]]
    assert out == [{"tool": "t", "description": "d"}]


def test_listing_cache_stale_served_on_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Marked choice: on a fetch failure WITH a cached (even expired) entry,
    the STALE listing is served — discovery availability beats freshness.
    Without an entry the error propagates (caller skips the server)."""
    calls: list[str] = []

    async def fake_fetch(url: str, headers: dict, timeout_s: float | None = None):
        calls.append(url)
        if len(calls) == 1:
            return [{"tool": "old", "description": "d"}]
        raise RuntimeError("upstream died")

    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch)
    server = _server("alpha")
    first = asyncio.run(routes_mcp._cached_upstream_tools(server))
    # expire the entry, then fail the refresh
    routes_mcp._tools_listing_cache[routes_mcp._listing_cache_key(server)] = (
        time.monotonic() - 1.0,
        first,
    )
    stale = asyncio.run(routes_mcp._cached_upstream_tools(server))
    assert stale == [{"tool": "old", "description": "d"}]

    routes_mcp.listing_cache_clear()
    with pytest.raises(RuntimeError):
        asyncio.run(routes_mcp._cached_upstream_tools(server))


def test_listing_cache_key_is_key_independent_and_covers_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cache identity is the LISTING request's inputs (name/url/headers/
    credential) — never the calling key (payload is key-independent; ACL
    filtering stays per-request), and a header change is a cache MISS."""
    s1 = _server("alpha")
    s2 = dict(s1, headers={"X-Extra": "1"})
    assert routes_mcp._listing_cache_key(s1) != routes_mcp._listing_cache_key(s2)
    assert routes_mcp._listing_cache_key(dict(s1)) == routes_mcp._listing_cache_key(s1)


def test_listing_cache_invalidated_through_registry_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalidation is driven BY core.registry (the funnel every admin MCP
    mutation goes through) via the registry-change hook — routes_admin never
    reaches into routes_mcp internals, and /admin/registry/refresh clears
    the cache too."""
    server = _server("alpha")
    _seed_listing(server, [{"tool": "t"}])
    assert routes_mcp._tools_listing_cache  # seeded

    class RegistryFakeDB:
        async def fetch(self, sql: str, *a: Any):
            return []

        async def execute(self, sql: str, *a: Any):
            return "DELETE 0"

    monkeypatch.setattr(registry, "db", RegistryFakeDB())
    asyncio.run(registry.refresh())
    assert routes_mcp._tools_listing_cache == {}


def test_registry_selects_and_orders_by_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry snapshot carries the fleet priority and orders by
    (priority ASC, name ASC) — the order mcp_catalog / tools/list inherit
    and the failover chain follows."""
    seen_sql: list[str] = []

    class RegistryFakeDB:
        async def fetch(self, sql: str, *a: Any):
            seen_sql.append(sql)
            if "FROM mcp_servers" in sql:
                return [
                    {
                        "id": "1",
                        "name": "zeta",
                        "url": "http://z",
                        "headers_json": "{}",
                        "auth_secret": None,
                        "auth_token_enc": None,
                        "enabled": True,
                        "tool_policy": "all",
                        "allowed_tools_json": "[]",
                        "priority": 10,
                    },
                    {
                        "id": "2",
                        "name": "alpha",
                        "url": "http://a",
                        "headers_json": "{}",
                        "auth_secret": None,
                        "auth_token_enc": None,
                        "enabled": True,
                        "tool_policy": "all",
                        "allowed_tools_json": "[]",
                        "priority": 0,
                    },
                ]
            return []

        async def execute(self, sql: str, *a: Any):
            return "DELETE 0"

    monkeypatch.setattr(registry, "db", RegistryFakeDB())
    asyncio.run(registry.refresh())
    mcp_sql = next(s for s in seen_sql if "FROM mcp_servers" in s)
    assert "ORDER BY priority ASC, name ASC" in mcp_sql
    # the in-memory test cache is ordered as the DB would return it
    ordered = [_server("alpha", priority=0), _server("zeta", priority=10)]
    registry._mcp_servers_cache = ordered
    names = [s["name"] for s in asyncio.run(registry.mcp_servers())]
    assert names == ["alpha", "zeta"]
    assert asyncio.run(registry.mcp_servers())[1]["priority"] == 10


# ---------------------------------------------------------------------------
# failover (design §7) — flattened calls only
# ---------------------------------------------------------------------------


def _failover_env(monkeypatch: pytest.MonkeyPatch, *, attempts: int = 2):
    """alpha (primary, transport-dead) -> beta (healthy, same tool).
    Returns (alpha, beta, settles, key) for endpoint assertions."""
    import httpx2

    key = _key()
    settles: list[tuple] = []
    alpha, beta = _server("alpha", priority=0), _server("beta", priority=5)

    async def real_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        if "alpha" in url:
            raise httpx2.ConnectError("connect timeout")
        return await op(_FakeSession())

    _patch_surface(
        monkeypatch,
        key,
        {("alpha", "search"), ("beta", "search")},
        [alpha, beta],
        upstream=real_upstream,
        settles=settles,
    )
    _seed_listing(beta, [{"tool": "search"}])
    _seed_listing(alpha, [{"tool": "search"}])
    monkeypatch.setattr(settings, "mcp_failover_attempts", attempts)
    return alpha, beta, settles, key


def test_flat_call_failover_on_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _alpha, _beta, settles, key = _failover_env(monkeypatch)
    out = json.loads(_call_with_key(key, routes_mcp._gated_mcp_call("alpha", "search", {}, failover=True)))
    assert out["ok"] is True and out["server"] == "beta", out
    # each attempt settled under its ACTUAL endpoint, in order
    assert settles == [("mcp:alpha:search", "error"), ("mcp:beta:search", "ok")]


def test_failover_skips_ungranted_and_policy_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-deny survives failover: a candidate without an explicit grant,
    or policy-denied, is not a candidate at all — the call reports the
    transport failure of the primary."""
    key = _key()
    settles: list[tuple] = []
    alpha = _server("alpha", priority=0)
    beta_nogrant = _server("beta", priority=1)
    gamma_policy = _server("gamma", priority=2, tool_policy="allowlist", allowed_tools=["other"])
    delta_ok = _server("delta", priority=3)

    async def real_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        if "alpha" in url:
            import httpx2

            raise httpx2.ConnectError("dead")
        return await op(_FakeSession())

    # gamma IS granted but its tool_policy allowlist does not admit 'search'
    # (policy gate) — beta is not granted at all (ACL gate)
    _patch_surface(
        monkeypatch,
        key,
        {("alpha", "search"), ("gamma", "search"), ("delta", "search")},
        [alpha, beta_nogrant, gamma_policy, delta_ok],
        upstream=real_upstream,
        settles=settles,
    )
    for sv in (beta_nogrant, gamma_policy, delta_ok):
        _seed_listing(sv, [{"tool": "search"}])
    out = json.loads(_call_with_key(key, routes_mcp._gated_mcp_call("alpha", "search", {}, failover=True)))
    assert out["ok"] is True and out["server"] == "delta", out
    assert settles == [("mcp:alpha:search", "error"), ("mcp:delta:search", "ok")]


def test_failover_requires_cached_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A candidate whose CACHED listing lacks the tool is never tried (cache
    peek only — no fresh fetches during failover)."""
    key = _key()
    settles: list[tuple] = []
    alpha, beta = _server("alpha", priority=0), _server("beta", priority=5)

    async def real_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        if "alpha" in url:
            import httpx2

            raise httpx2.ConnectError("dead")
        raise AssertionError("beta must not be called — its cached listing lacks the tool")

    _patch_surface(
        monkeypatch,
        key,
        {("alpha", "search"), ("beta", "search")},
        [alpha, beta],
        upstream=real_upstream,
        settles=settles,
    )
    _seed_listing(beta, [{"tool": "other"}])  # tool NOT in beta's cached listing
    out = json.loads(_call_with_key(key, routes_mcp._gated_mcp_call("alpha", "search", {}, failover=True)))
    assert out["ok"] is False
    assert "attempts" not in out  # single candidate -> unchanged contract shape
    assert settles == [("mcp:alpha:search", "error")]


def test_failover_attempt_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries after the primary are bounded by mcp_failover_attempts."""
    key = _key()
    settles: list[tuple] = []
    servers = [
        _server("alpha", priority=0),
        _server("b1", priority=1),
        _server("b2", priority=2),
        _server("b3", priority=3),
    ]

    async def real_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        import httpx2

        raise httpx2.ConnectError("all dead")

    grants = {(s["name"], "t") for s in servers}
    _patch_surface(monkeypatch, key, grants, servers, upstream=real_upstream, settles=settles)
    for s in servers[1:]:
        _seed_listing(s, [{"tool": "t"}])
    monkeypatch.setattr(settings, "mcp_failover_attempts", 1)  # primary + 1 retry
    out = json.loads(_call_with_key(key, routes_mcp._gated_mcp_call("alpha", "t", {}, failover=True)))
    assert out["ok"] is False and out["attempts"] == ["alpha: ConnectError", "b1: ConnectError"]
    assert [s[0] for s in settles] == ["mcp:alpha:t", "mcp:b1:t"]


def test_failover_not_triggered_by_upstream_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Upstream tool-error results are REAL ANSWERS — the tool ran and
    refused; failover is for TRANSPORT failure only."""
    key = _key()
    settles: list[tuple] = []
    alpha, beta = _server("alpha", priority=0), _server("beta", priority=5)

    async def real_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        return await op(_FakeSession(_FakeResult(is_error=True)))

    _patch_surface(
        monkeypatch,
        key,
        {("alpha", "t"), ("beta", "t")},
        [alpha, beta],
        upstream=real_upstream,
        settles=settles,
    )
    _seed_listing(beta, [{"tool": "t"}])
    out = json.loads(_call_with_key(key, routes_mcp._gated_mcp_call("alpha", "t", {}, failover=True)))
    assert out["ok"] is False and out["server"] == "alpha"
    assert settles == [("mcp:alpha:t", "error")]


def test_explicit_mcp_call_never_fails_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented asymmetry: mcp_call NAMES its server, so a transport
    failure is reported as-is even when a perfect failover candidate exists."""
    _alpha, _beta, settles, key = _failover_env(monkeypatch)
    out = json.loads(_call_with_key(key, routes_mcp._mcp_call("alpha", "search", {})))
    assert out["ok"] is False and out["server"] == "alpha"
    assert out["error"].startswith("upstream call failed:")
    assert "attempts" not in out and settles == [("mcp:alpha:search", "error")]


# ---------------------------------------------------------------------------
# mcp_catalog — unchanged contract + priority ordering
# ---------------------------------------------------------------------------


def test_mcp_catalog_priority_order_and_no_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog keeps its contract (granted-only servers, skip-with-error)
    and now iterates in fleet priority order."""
    key = _key()
    servers = [_server("alpha", priority=0), _server("beta", priority=5)]
    listings = {
        "alpha": [{"tool": "search", "description": "d"}],
        "beta": [{"tool": "query", "description": "d"}],
    }
    _patch_surface(monkeypatch, key, {("alpha", "search"), ("beta", "query")}, servers, listings=listings)
    out = json.loads(_call_with_key(key, routes_mcp._mcp_catalog()))
    assert [s["server"] for s in out["servers"]] == ["alpha", "beta"]
    assert [g["server"] for g in out["granted"]] == ["alpha", "beta"]

    # zero-grant key: no servers, no URLs (existing discipline, re-pinned)
    key2 = _key()
    _patch_surface(monkeypatch, key2, set(), servers, listings=listings)
    out2 = json.loads(_call_with_key(key2, routes_mcp._mcp_catalog()))
    assert out2["servers"] == [] and out2["granted"] == []


def test_mcp_catalog_bounded_timeout_still_15s(monkeypatch: pytest.MonkeyPatch) -> None:
    """The metadata-fetch audit bound survives the cache refactor: cache-miss
    fetches still pass timeout_s=15.0."""
    seen: list[float | None] = []

    async def fake_fetch(url: str, headers: dict, timeout_s: float | None = None):
        seen.append(timeout_s)
        return []

    key = _key()
    _patch_surface(monkeypatch, key, set(), [_server("alpha")])
    # AFTER _patch_surface: this counting fake must be the one installed
    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch)
    _call_with_key(key, routes_mcp._mcp_catalog())
    assert seen == [15.0]


# ---------------------------------------------------------------------------
# admin API — priority field (register/update)
# ---------------------------------------------------------------------------


class _AdminFakeDB:
    """Just enough of asyncpg for the two MCP-server endpoints."""

    def __init__(self, existing: dict[str, Any] | None = None) -> None:
        self.existing = existing or {}
        self.upsert_args: tuple | None = None
        self.patch_args: tuple | None = None

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM mcp_servers" in sql and "UPDATE" not in sql:
            return self.existing or None
        if "INSERT INTO mcp_servers" in sql:
            self.upsert_args = args
            return {
                "id": "1",
                "name": args[0],
                "url": args[1],
                "auth_secret": None,
                "auth_configured": False,
                "tool_policy": args[5],
                "allowed_tools_json": args[6],
                "enabled": args[7],
                "priority": args[8],
            }
        if "UPDATE mcp_servers" in sql:
            self.patch_args = args
            return {
                "id": "1",
                "name": args[0],
                "url": "http://svc",
                "auth_secret": None,
                "tool_policy": args[2],
                "allowed_tools_json": args[3],
                "enabled": args[1],
                "priority": args[4],
            }
        return None

    async def fetch(self, sql: str, *a: Any):
        return []

    async def execute(self, sql: str, *a: Any):
        return "OK"


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")

    async def no_refresh() -> None:
        pass

    monkeypatch.setattr(registry, "refresh", no_refresh)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


_AH = {"Authorization": "Bearer admin-secret-123"}


def test_admin_upsert_accepts_priority(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _AdminFakeDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    r = admin_env.post(
        "/admin/mcp-servers",
        json={"name": "alpha", "url": "http://alpha.svc.cluster.local/mcp", "priority": 7},
        headers=_AH,
    )
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 7
    assert fake.upsert_args is not None and fake.upsert_args[8] == 7
    # default stays the neutral 0 (additive migration convention)
    r2 = admin_env.post(
        "/admin/mcp-servers", json={"name": "alpha", "url": "http://alpha.svc.cluster.local/mcp"}, headers=_AH
    )
    assert r2.status_code == 200
    assert r2.json()["priority"] == 0


def test_admin_patch_priority_preservation_and_zero(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    # omitted priority keeps the row's current value (never silently resets)
    fake = _AdminFakeDB(
        existing={
            "url": "http://svc",
            "headers_json": "{}",
            "auth_secret": None,
            "tool_policy": "all",
            "allowed_tools_json": "[]",
            "enabled": True,
            "priority": 3,
        }
    )
    monkeypatch.setattr(routes_admin, "db", fake)
    r = admin_env.patch("/admin/mcp-servers/svc", json={"tool_policy": "all"}, headers=_AH)
    assert r.status_code == 200, r.text
    assert r.json()["priority"] == 3
    assert fake.patch_args is not None and fake.patch_args[4] == 3

    # an EXPLICIT 0 survives the falsy trap (same lesson as `enabled`)
    fake2 = _AdminFakeDB(
        existing={
            "url": "http://svc",
            "headers_json": "{}",
            "auth_secret": None,
            "tool_policy": "all",
            "allowed_tools_json": "[]",
            "enabled": True,
            "priority": 9,
        }
    )
    monkeypatch.setattr(routes_admin, "db", fake2)
    r2 = admin_env.patch("/admin/mcp-servers/svc", json={"priority": 0}, headers=_AH)
    assert r2.status_code == 200
    assert fake2.patch_args is not None and fake2.patch_args[4] == 0
    assert r2.json()["priority"] == 0

    # legacy row without the column (migration window) defaults to 0
    fake3 = _AdminFakeDB(
        existing={
            "url": "http://svc",
            "headers_json": "{}",
            "auth_secret": None,
            "tool_policy": "all",
            "allowed_tools_json": "[]",
            "enabled": True,
        }
    )
    monkeypatch.setattr(routes_admin, "db", fake3)
    r3 = admin_env.patch("/admin/mcp-servers/svc", json={}, headers=_AH)
    assert r3.status_code == 200
    assert fake3.patch_args is not None and fake3.patch_args[4] == 0


# ---------------------------------------------------------------------------
# federation regression pin: the gate chain uses the SAME ACL predicate
# ---------------------------------------------------------------------------


def test_gate_chain_uses_default_deny_predicate() -> None:
    """Guard against the flat surface growing its own ACL notion: the shared
    chain consumes federation.is_allowed (default-deny, exact pair only)."""
    grants = {("alpha", "search")}
    assert is_allowed(grants, "alpha", "search")
    assert not is_allowed(grants, "alpha", "other")
    assert not is_allowed(set(), "alpha", "search")


# ---------------------------------------------------------------------------
# stringy-arguments tolerance: the observed live wrap {'arguments': '<json>'}
# (2026-09-21 DSH four-part calls) must not reach the fleet as-is
# ---------------------------------------------------------------------------


class _CaptureSession:
    """Records the arguments each call_tool received."""

    def __init__(self, seen: list) -> None:
        self._seen = seen

    async def call_tool(self, tool: str, arguments: dict) -> _FakeResult:
        self._seen.append(arguments)
        return _FakeResult()


def test_unwrap_stringy_arguments_shapes() -> None:
    f = routes_mcp._unwrap_stringy_arguments
    # the live shape: single 'arguments' key with a JSON-object string
    assert f({"arguments": '{"q": 1}'}) == {"q": 1}
    # multi-key / empty / non-'arguments' dicts pass through untouched
    passthrough = {"q": 1}
    assert f(passthrough) is passthrough
    assert f({}) == {}
    assert f({"arguments": '{"q": 1}', "other": 2}) == {"arguments": '{"q": 1}', "other": 2}
    # a LEGITIMATE 'arguments' OBJECT param (dict payload) is never rewritten —
    # only the pathological string shape unwraps
    legit_obj = {"arguments": {"sql": "SELECT 1"}}
    assert f(legit_obj) == legit_obj
    # wrapped-but-not-JSON string: left as-is (upstream owns the visible error)
    assert f({"arguments": "not json"}) == {"arguments": "not json"}
    # JSON non-object payload: arity-changing — refuse to guess
    assert f({"arguments": "[1, 2]"}) == {"arguments": "[1, 2]"}
    assert f({"arguments": '"flat"'}) == {"arguments": '"flat"'}
    # input never mutated
    original = {"arguments": '{"q": 1}'}
    f(original)
    assert original == {"arguments": '{"q": 1}'}


def test_unwrap_stringy_arguments_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_unwrap_arguments", False)
    wrapped = {"arguments": '{"q": 1}'}
    assert routes_mcp._unwrap_stringy_arguments(wrapped) == wrapped


def test_stringy_arguments_unwrapped_at_gate_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration: the exact wrapped shape seen live from the harness is
    decoded before the upstream session sees it — the fleet receives the
    INNER object, so required params validate."""
    key = _key()
    seen: list = []

    async def capture_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        return await op(_CaptureSession(seen))

    _patch_surface(monkeypatch, key, {("alpha", "search")}, [_server("alpha")], upstream=capture_upstream)
    wrapped = {"arguments": json.dumps({"resource": "pods", "verb": "list"})}
    out = json.loads(_call_with_key(key, routes_mcp._gated_mcp_call("alpha", "search", wrapped)))
    assert out["ok"] is True, out
    assert seen == [{"resource": "pods", "verb": "list"}]


def test_flat_call_stringy_arguments_http_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP-level reproduction of the live byte-for-byte shape: a tools/call
    whose params.arguments is {'arguments': '<json-string>'} executes against
    the inner object."""
    key = _key()
    seen: list = []

    async def capture_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        return await op(_CaptureSession(seen))

    _patch_surface(monkeypatch, key, {("alpha", "search")}, [_server("alpha")], upstream=capture_upstream)
    with _client() as tc:
        r = tc.post(
            "/mcp",
            json=_rpc(
                9,
                "tools/call",
                {"name": "alpha__search", "arguments": {"arguments": json.dumps({"q": "x"})}},
            ),
            headers=_headers("tools/call", name="alpha__search"),
        )
    assert r.status_code == 200, r.text
    assert seen == [{"q": "x"}]


def test_live_grants_asyncpg_safe_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The v1.4.0 field bug: the live-profile deny query aggregated an
    ANONYMOUS record[] (array_agg((server, tool))) — asyncpg has no codec
    for the record type and raised on EVERY call, putting the whole relay
    into tool-error retry loops. Pin the asyncpg-safe shape: the deny pairs
    ride TEXT elements 'server<US>tool' (chr(31)-delimited), and a resolver
    failure degrades to stamps-only grants (fail-open) instead of taking
    the ACL gate down."""

    class LiveDB:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def fetch(self, sql: str, *args: Any):
            self.queries.append(sql)
            if "key_denies" in sql:
                # Postgres TEXT[] arrives decoded as a list of str
                return [
                    {
                        "defaults_json": json.dumps({"live_grants": True, "mcp_grants": [["sqlhandler", "run_sql"]]}),
                        "key_denies": ["legacy" + chr(31) + "tool_a"],
                    }
                ]
            return []

    from pcai_llm.mcpf import federation

    fake = LiveDB()
    monkeypatch.setattr(federation, "db", fake)

    # deny pairs split back into tuples; profile grants ride
    pairs, denies = asyncio.run(federation.load_live_profile_grants(uuid.uuid4()))
    assert ("sqlhandler", "run_sql") in pairs
    assert ("legacy", "tool_a") in denies
    # asyncpg-safe: the statement must NOT aggregate an anonymous composite
    sql = fake.queries[0]
    assert "array_agg((a.server" not in sql, "record[] has no asyncpg codec — regression of the v1.4.0 field bug"
    assert "chr(31)" in sql or "||" in sql, "deny pairs must ride a delimited TEXT element"

    # fail-open: a resolver fault degrades to stamps-only, never a 500 —
    # the key-rows query still succeeds; only the live layer breaks
    class BrokenDB:
        async def fetch(self, sql: str, *args: Any):
            if "key_denies" in sql:
                raise RuntimeError("asyncpg: no codec for record")
            return []

    monkeypatch.setattr(federation, "db", BrokenDB())
    grants = asyncio.run(federation.load_grants(uuid.uuid4()))
    assert grants == set(), "resolver failure must degrade to the key's own rows (fail-open)"
