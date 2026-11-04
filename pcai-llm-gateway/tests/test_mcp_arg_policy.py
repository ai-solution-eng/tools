"""Gate-chain integration: the argument-level policy stage inside
`routes_mcp._gated_mcp_call` (the /mcp surface).

What these pin (SPEC 3.2 order):
  * the refusal happens BEFORE the upstream session is opened (the fake
    session factory must never run) and BEFORE the tool-policy gate;
  * the ledger settles the refusal under the distinguishable
    status="policy_blocked" (not "error"), and the metric carries the
    policy's own BOUNDED reason;
  * the refusal surfaces as ToolError, i.e. an is_error CallToolResult for the
    client — the same posture as an ACL denial;
  * an ACL denial still wins (a caller without the grant never learns policy
    details), and warn mode / exemptions let the call through;
  * payload telemetry never refuses.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, ClassVar

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from pcai_llm.api import routes_mcp
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import registry
from pcai_llm.core.budgets import GateResult
from pcai_llm.mcpf import arg_policy as ap


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "mcp_arg_policy", "enforce", raising=False)
    monkeypatch.setattr(settings, "mcp_arg_policy_exempt", "", raising=False)
    monkeypatch.setattr(settings, "mcp_arg_telemetry", True, raising=False)
    monkeypatch.setattr(settings, "mcp_separate_read_egress", "warn", raising=False)
    saved = registry._mcp_servers_cache
    yield
    registry._mcp_servers_cache = saved


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


def _server(name: str = "searxng", *, tool_policy: str = "all") -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "url": f"http://{name}.svc.cluster.local:9100/mcp",
        "headers": {},
        "auth_secret": None,
        "auth_token_enc": None,
        "enabled": True,
        "tool_policy": tool_policy,
        "allowed_tools": [],
        "priority": 0,
    }


class _Env:
    """Fakes for one gate-chain call, with spies for the assertions."""

    def __init__(self, key: KeyRecord, grants: set[tuple[str, str]], servers: list[dict[str, Any]]) -> None:
        self.key = key
        self.grants = grants
        self.servers = servers
        self.settles: list[tuple[str, str]] = []
        self.upstream_calls = 0
        self.metrics: list[tuple] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Env:
        env = self

        async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
            return env.grants

        async def fake_rate(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
            return GateResult(True)

        async def fake_settle(*a: Any, **k: Any) -> None:
            env.settles.append((str(a[2]), str(a[5])))  # (endpoint, status)

        async def fake_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
            env.upstream_calls += 1
            raise AssertionError("upstream session must NOT be opened for a policy refusal")

        monkeypatch.setattr(routes_mcp, "load_grants", fake_grants)
        monkeypatch.setattr(routes_mcp, "check_rate_limit", fake_rate)
        monkeypatch.setattr(routes_mcp, "settle", fake_settle)
        monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream)
        monkeypatch.setattr(ap._metrics, "observe_mcp_policy_block", lambda *a: env.metrics.append(a))
        monkeypatch.setattr(ap._metrics, "observe_mcp_call", lambda *a: env.metrics.append(a))
        monkeypatch.setattr(ap._metrics, "observe_mcp_arg_telemetry", lambda *a: env.metrics.append(a))
        registry._mcp_servers_cache = env.servers
        return env

    def call(self, server: str, tool: str, arguments: dict) -> Any:
        token = routes_mcp._caller.set(self.key)
        try:
            return asyncio.run(routes_mcp._gated_mcp_call(server, tool, arguments))
        finally:
            routes_mcp._caller.reset(token)


def _raise_capture(env: _Env, server: str, tool: str, arguments: dict) -> ToolError:
    with pytest.raises(ToolError) as excinfo:
        env.call(server, tool, arguments)
    return excinfo.value


# ---------------------------------------------------------------------------
# the refusal contract
# ---------------------------------------------------------------------------


def test_metadata_url_is_refused_before_the_upstream_session(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env(_key(), {("searxng", "fetch_content")}, [_server()]).install(monkeypatch)
    err = _raise_capture(env, "searxng", "fetch_content", {"url": "http://169.254.169.254/latest/meta-data/"})
    assert "MCP argument policy" in str(err)
    assert env.upstream_calls == 0, "the refusal must precede the upstream session"
    assert ("mcp:searxng:fetch_content", "policy_blocked") in env.settles
    assert env.metrics == [
        ("searxng", "fetch_content", ap.REASON_DESTINATION),
        ("searxng", "policy_blocked", ""),
    ]


def test_cluster_host_is_refused_on_the_egress_server(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env(_key(), {("searxng", "fetch_content")}, [_server()]).install(monkeypatch)
    err = _raise_capture(env, "searxng", "fetch_content", {"url": "http://prometheus.svc.cluster.local:9090/"})
    assert "cluster-host" in str(err)
    assert env.upstream_calls == 0
    assert env.metrics[0] == ("searxng", "fetch_content", ap.REASON_CLUSTER_HOST)


def test_file_scheme_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _Env(_key(), {("searxng", "fetch_content")}, [_server()]).install(monkeypatch)
    err = _raise_capture(env, "searxng", "fetch_content", {"url": "file:///etc/passwd"})
    assert "url-scheme" in str(err)
    assert env.upstream_calls == 0


def test_admin_only_tool_is_refused_without_the_mcp_admin_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Z2's admin_tool_refusal runs FIRST in the chain."""
    env = _Env(_key(), {("sqlhandler", "admin_key_mint")}, [_server("sqlhandler")]).install(monkeypatch)
    err = _raise_capture(env, "sqlhandler", "admin_key_mint", {"label": "x"})
    assert "mcp_admin" in str(err)
    assert env.upstream_calls == 0
    assert ("mcp:sqlhandler:admin_key_mint", "policy_blocked") in env.settles
    assert env.metrics[0] == ("sqlhandler", "admin_key_mint", ap.ADMIN_TOOL_REASON)


def test_acl_denial_still_wins_over_the_argument_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ungranted tool must never learn policy details: the ACL gate runs
    first and settles under its OWN status ('denied', the wave-2 vocabulary),
    never 'policy_blocked'."""
    env = _Env(_key(), set(), [_server()]).install(monkeypatch)
    err = _raise_capture(env, "searxng", "fetch_content", {"url": "http://169.254.169.254/"})
    assert "denied" in str(err)
    assert ("mcp:searxng:fetch_content", "denied") in env.settles
    # The INTENT is "the argument stage must not run", so assert on the
    # POLICY-stage metrics specifically rather than on "no metrics at all":
    # the call-outcome counter (gateway_mcp_calls_total, P2-1) legitimately
    # records this refusal as (searxng, error, "") - a metric that says a call
    # errored, not one that leaks a policy verdict to an ungranted tool.
    _policy_reasons = set(ap.REASONS) | {
        ap.ADMIN_TOOL_REASON,
        ap.READ_EGRESS_REASON,
        ap.READ_EGRESS_WARN_REASON,
        "admin-tool-refusal-unavailable",
        "read-egress-check-unavailable",
    }
    assert not [m for m in env.metrics if len(m) == 3 and m[2] in _policy_reasons], (
        "the argument stage must not run for an ungranted tool"
    )


def test_read_egress_enforce_refuses_on_the_mcp_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_separate_read_egress", "enforce", raising=False)
    env = _Env(
        _key(),
        {("logsearch", "search_logs"), ("searxng", "fetch_content")},
        [_server("logsearch")],
    ).install(monkeypatch)
    err = _raise_capture(env, "logsearch", "search_logs", {"pattern": "x"})
    assert "read+egress" in str(err)
    assert env.upstream_calls == 0
    assert ("mcp:logsearch:search_logs", "policy_blocked") in env.settles


# ---------------------------------------------------------------------------
# the allowed paths
# ---------------------------------------------------------------------------


def test_warn_mode_lets_the_call_through_and_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_arg_policy", "warn", raising=False)

    class _Block:
        def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
            return {"type": "text", "text": "ok"}

    class _Result:
        is_error = False
        content: ClassVar[list[Any]] = [_Block()]

    env = _Env(_key(), {("searxng", "fetch_content")}, [_server()])

    async def ok_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        env.upstream_calls += 1
        return _Result()

    env.install(monkeypatch)
    monkeypatch.setattr(routes_mcp, "_upstream_session", ok_upstream)
    out = env.call("searxng", "fetch_content", {"url": "http://169.254.169.254/"})
    assert '"ok": true' in out
    assert env.upstream_calls == 1
    assert ("searxng", "fetch_content", ap.REASON_DESTINATION) in env.metrics


def test_exempt_tool_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_arg_policy_exempt", "searxng.*", raising=False)

    class _Block:
        def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
            return {"type": "text", "text": "ok"}

    class _Result:
        is_error = False
        content: ClassVar[list[Any]] = [_Block()]

    env = _Env(_key(), {("searxng", "fetch_content")}, [_server()])
    env.install(monkeypatch)

    async def ok_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        env.upstream_calls += 1
        return _Result()

    monkeypatch.setattr(routes_mcp, "_upstream_session", ok_upstream)
    env.call("searxng", "fetch_content", {"url": "http://127.0.0.1/"})
    assert env.upstream_calls == 1


def test_off_mode_restores_the_pre_hardening_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_arg_policy", "off", raising=False)

    class _Block:
        def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
            return {"type": "text", "text": "ok"}

    class _Result:
        is_error = False
        content: ClassVar[list[Any]] = [_Block()]

    env = _Env(_key(), {("searxng", "fetch_content")}, [_server()])
    env.install(monkeypatch)

    async def ok_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        env.upstream_calls += 1
        return _Result()

    monkeypatch.setattr(routes_mcp, "_upstream_session", ok_upstream)
    env.call("searxng", "fetch_content", {"url": "http://169.254.169.254/"})
    assert env.upstream_calls == 1


def test_payload_telemetry_never_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Block:
        def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
            return {"type": "text", "text": "ok"}

    class _Result:
        is_error = False
        content: ClassVar[list[Any]] = [_Block()]

    env = _Env(_key(), {("workbench", "write_file")}, [_server("workbench")])
    env.install(monkeypatch)

    async def ok_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        env.upstream_calls += 1
        return _Result()

    monkeypatch.setattr(routes_mcp, "_upstream_session", ok_upstream)
    out = env.call("workbench", "write_file", {"workspace": "w", "path": "a.txt", "content": "A" * 800})
    assert '"ok": true' in out
    assert ("workbench", "write_file", "base64-blob") in env.metrics


def test_failover_candidate_runs_the_argument_stage_for_its_own_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failover target is a DIFFERENT server with a different class, so the
    primary's verdict does not transfer: a cluster-internal URL that is
    allowed on a non-egress primary must still be refused when the call fails
    over to an egress-class server (cross-validation wave 1, P3)."""
    import time

    routes_mcp.listing_cache_clear()
    primary = _server("workbench")
    secondary = _server("searxng")
    key = _key()
    settles: list[tuple[str, str]] = []
    touched: list[str] = []

    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return {("workbench", "run"), ("searxng", "run")}

    async def fake_rate(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
        return GateResult(True)

    async def fake_settle(*a: Any, **k: Any) -> None:
        settles.append((str(a[2]), str(a[5])))

    async def upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        touched.append(url)
        raise RuntimeError("primary is down")

    monkeypatch.setattr(routes_mcp, "load_grants", fake_grants)
    monkeypatch.setattr(routes_mcp, "check_rate_limit", fake_rate)
    monkeypatch.setattr(routes_mcp, "settle", fake_settle)
    monkeypatch.setattr(routes_mcp, "_upstream_session", upstream)
    monkeypatch.setattr(settings, "mcp_failover_attempts", 2, raising=False)
    registry._mcp_servers_cache = [primary, secondary]
    # the secondary is a failover candidate only if its CACHED listing exposes
    # the tool (cache peek, never a fresh fetch)
    routes_mcp._tools_listing_cache[routes_mcp._listing_cache_key(secondary)] = (
        time.monotonic() + 60.0,
        [{"tool": "run", "description": "x"}],
    )
    try:
        token = routes_mcp._caller.set(key)
        try:
            out = asyncio.run(
                routes_mcp._gated_mcp_call(
                    "workbench",
                    "run",
                    {"url": "http://prometheus.svc.cluster.local:9090/"},
                    failover=True,
                )
            )
        finally:
            routes_mcp._caller.reset(token)
    finally:
        routes_mcp.listing_cache_clear()

    assert '"ok": false' in out
    assert touched == [primary["url"]], "the refused candidate must never be contacted"
    assert "cluster-host" in out, "the refusal must be reported in attempts"
    assert ("mcp:searxng:run", "policy_blocked") in settles


# ---------------------------------------------------------------------------
# F1 (audit w6): the DISCOVERY surfaces are metered and the fan-out bounded
# ---------------------------------------------------------------------------


def test_discovery_rate_gate_refuses_tools_list_and_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: tools/list and mcp_catalog ran NO rate gate — with a cold TTL one
    request opened N concurrent upstream listing sessions. Both discovery
    surfaces now use the SAME check_rate_limit the gate chain uses, with the
    SAME refusal shape: settle "rate_limited" + a ToolError result."""
    key = _key()
    env = _Env(key, {("searxng", "fetch")}, [_server("searxng")]).install(monkeypatch)

    async def refused_rate(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
        return GateResult(False, reason="RPM limit 60 exceeded (61 this minute)")

    monkeypatch.setattr(routes_mcp, "check_rate_limit", refused_rate)
    server = routes_mcp._new_gateway_server()

    token = routes_mcp._caller.set(key)
    try:
        with pytest.raises(ToolError) as list_exc:
            asyncio.run(server.list_tools())
        with pytest.raises(ToolError) as cat_exc:
            asyncio.run(routes_mcp._mcp_catalog())
    finally:
        routes_mcp._caller.reset(token)

    assert "rate limited" in str(list_exc.value), "tools/list must refuse with the standard shape"
    assert "rate limited" in str(cat_exc.value), "mcp_catalog must refuse with the standard shape"
    assert ("mcp:_discovery:tools_list", "rate_limited") in env.settles, env.settles
    assert ("mcp:_discovery:mcp_catalog", "rate_limited") in env.settles, env.settles


def test_catalog_fan_out_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: a cold-cache mcp_catalog used to open ONE upstream listing session
    per registered server, all at once (N-parallel gather). The per-request
    semaphore caps the amplification at _CATALOG_FETCH_CONCURRENCY."""
    key = _key()
    env = _Env(key, {("searxng", "fetch")}, [_server(f"s{i}") for i in range(6)]).install(monkeypatch)
    routes_mcp.listing_cache_clear()
    state = {"cur": 0, "max": 0}

    async def tracked(server: dict[str, Any]) -> list[dict[str, Any]]:
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.02)
        state["cur"] -= 1
        return []

    monkeypatch.setattr(routes_mcp, "_cached_upstream_tools", tracked)

    token = routes_mcp._caller.set(key)
    try:
        out = asyncio.run(routes_mcp._mcp_catalog())
    finally:
        routes_mcp._caller.reset(token)

    assert env.upstream_calls == 0, "the tracker replaced the fetch — no real sessions"
    # getattr default: pre-fix the module has no cap constant — the pin must
    # fail on the MEASURED concurrency (6-parallel), not at setup
    cap = getattr(routes_mcp, "_CATALOG_FETCH_CONCURRENCY", 4)
    assert state["max"] <= cap, (f"cold fan-out opened {state['max']} concurrent sessions; cap is {cap}",)
    assert "servers" in out


def test_discovery_gate_fails_open_on_limiter_infrastructure_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """F1 follow-up: the gate distinguishes "the limiter could not be
    evaluated" (a DB blip inside check_rate_limit) from "the limit was
    exceeded". A limiter outage must not become a discovery outage: an
    INFRASTRUCTURE error is logged at WARNING and the listing proceeds —
    still bounded by the TTL caches, the 15 s metadata bound and the
    fan-out cap. The OVER-LIMIT path never fails open (the refusal pin
    above keeps proving that)."""
    import logging

    key = _key()
    env = _Env(key, {("searxng", "fetch")}, [_server("searxng")]).install(monkeypatch)

    async def broken_limiter(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
        raise RuntimeError("database not connected")

    monkeypatch.setattr(routes_mcp, "check_rate_limit", broken_limiter)

    token = routes_mcp._caller.set(key)
    try:
        with caplog.at_level(logging.WARNING, logger="pcai_llm.api.routes_mcp"):
            out = asyncio.run(routes_mcp._mcp_catalog())
    finally:
        routes_mcp._caller.reset(token)

    assert "servers" in out, "a limiter outage must not refuse discovery"
    assert any("rate gate unavailable" in r.getMessage() for r in caplog.records), caplog.text
    assert env.settles == [], "nothing was refused, so no refusal row exists"
