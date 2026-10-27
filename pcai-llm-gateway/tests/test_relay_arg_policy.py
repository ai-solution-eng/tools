"""Relay-path argument policy (hardening P0-1 on the MODEL-DRIVEN path).

The relay (`routes_proxy._relay_executor`) is where a model's tool calls
actually execute during a chat turn — the path that carries the
read-then-exfiltrate pattern. It duplicates the gate chain inline instead of
calling /mcp's `_gated_mcp_call`, so the argument stage must be enforced
there too, with the SAME `mcpf.arg_policy.gate_call`.

Pinned here:
  * a metadata-URL argument returns (False, <policy message>) — this path's
    contract, which the relay surfaces to the model as a tool error;
  * the upstream session factory is NEVER called on a refusal;
  * the ledger settles status="policy_blocked";
  * the admin-tool refusal and the read+egress separation fire here as well;
  * a legitimate call still reaches the upstream (the stage is not a blanket
    refusal).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, ClassVar

import pytest

from pcai_llm.api import routes_mcp, routes_proxy
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
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 1000,
        "rate_limit_tpm": 1_000_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


def _server(name: str) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "url": f"http://{name}.svc:8080",
        "headers": {},
        "auth_secret": None,
        "auth_token_enc": None,
        "enabled": True,
        "tool_policy": "all",
        "allowed_tools": [],
    }


class _Block:
    def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
        return {"type": "text", "text": "ok"}


class _Result:
    is_error = False
    content: ClassVar[list[Any]] = [_Block()]


class _Relay:
    """Fakes for one relay-executor call, with spies."""

    def __init__(self, servers: list[dict[str, Any]] | None = None) -> None:
        self.settles: list[tuple[str, str]] = []
        self.upstream_calls = 0
        self.metrics: list[tuple] = []
        self.servers = servers or [_server("searxng"), _server("sqlhandler"), _server("logsearch")]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _Relay:
        relay = self

        async def fake_settle(*a: Any, **k: Any) -> None:
            relay.settles.append((str(a[2]), str(a[5])))

        async def fake_rate(key_id: Any, rpm: int, tpm: int, est: int):
            return GateResult(True)

        async def spy_upstream(url: str, headers: dict, op: Any, timeout_s: float | None = None):
            relay.upstream_calls += 1
            return _Result()

        async def fake_headers(target: dict, key_id: Any = None, **kw: Any) -> dict:
            return {}

        monkeypatch.setattr(routes_proxy, "settle", fake_settle)
        monkeypatch.setattr(routes_proxy, "check_rate_limit", fake_rate)
        monkeypatch.setattr(routes_proxy, "loop_guard_check", lambda *a, **k: "")
        monkeypatch.setattr(routes_mcp, "_upstream_session", spy_upstream)
        monkeypatch.setattr(routes_mcp, "resolve_mcp_headers", fake_headers)
        monkeypatch.setattr(ap._metrics, "observe_mcp_policy_block", lambda *a: relay.metrics.append(a))
        monkeypatch.setattr(ap._metrics, "observe_mcp_call", lambda *a: relay.metrics.append(a))
        registry._mcp_servers_cache = relay.servers
        return relay

    def execute(self, key: KeyRecord, grants: set[tuple[str, str]] | None = None):
        executor = asyncio.run(routes_proxy._relay_executor(key, "req-relay-arg", grants=grants))
        return executor

    def run(self, key: KeyRecord, server: str, tool: str, arguments: dict, grants=None) -> tuple[bool, Any]:
        executor = self.execute(key, grants)
        return asyncio.run(executor(server, tool, arguments))


def test_relay_refuses_metadata_url_without_opening_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _Relay().install(monkeypatch)
    ok, payload = relay.run(
        _key(), "searxng", "fetch_content", {"url": "http://169.254.169.254/latest/meta-data/"}
    )
    assert ok is False
    assert "MCP argument policy" in str(payload) and "169.254.169.254" not in str(payload)
    assert relay.upstream_calls == 0, "the refusal must not open an upstream session"
    assert ("mcp:searxng:fetch_content", "policy_blocked") in relay.settles
    assert relay.metrics == [
        ("searxng", "fetch_content", ap.REASON_DESTINATION),
        ("searxng", "policy_blocked", ""),
    ]


def test_relay_refuses_cluster_host(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _Relay().install(monkeypatch)
    ok, payload = relay.run(
        _key(), "searxng", "fetch_content", {"url": "http://kube-dns.kube-system.svc.cluster.local/"}
    )
    assert ok is False and "cluster-host" in str(payload)
    assert relay.upstream_calls == 0


def test_relay_refuses_admin_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _Relay().install(monkeypatch)
    ok, payload = relay.run(_key(), "sqlhandler", "admin_key_mint", {"label": "x"})
    assert ok is False and "mcp_admin" in str(payload)
    assert relay.upstream_calls == 0
    assert ("mcp:sqlhandler:admin_key_mint", "policy_blocked") in relay.settles


def test_relay_read_egress_enforce_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_separate_read_egress", "enforce", raising=False)
    relay = _Relay().install(monkeypatch)
    grants = {("logsearch", "search_logs"), ("searxng", "fetch_content")}
    ok, payload = relay.run(_key(), "logsearch", "search_logs", {"pattern": "x"}, grants=grants)
    assert ok is False and "read+egress" in str(payload)
    assert relay.upstream_calls == 0


def test_relay_allows_a_legitimate_call(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = _Relay().install(monkeypatch)
    ok, _ = relay.run(_key(), "searxng", "search", {"query": "climate policy 2026"})
    assert ok is True
    assert relay.upstream_calls == 1
    assert ("mcp:searxng:search", "ok") in relay.settles


def test_relay_allows_a_payload_argument_carrying_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """False-positive guard on the hot path: a URL stored as DATA is not a
    destination (the relay is where a model most often writes text)."""
    relay = _Relay().install(monkeypatch)
    ok, _ = relay.run(_key(), "searxng", "search", {"query": "http://169.254.169.254/"})
    assert ok is True
    assert relay.upstream_calls == 1


def test_relay_warn_mode_lets_the_call_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_arg_policy", "warn", raising=False)
    relay = _Relay().install(monkeypatch)
    ok, _ = relay.run(_key(), "searxng", "fetch_content", {"url": "http://127.0.0.1/"})
    assert ok is True
    assert relay.upstream_calls == 1
    assert ("searxng", "fetch_content", ap.REASON_DESTINATION) in relay.metrics
