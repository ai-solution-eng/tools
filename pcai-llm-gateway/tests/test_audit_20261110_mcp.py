"""Regression tests for the 2026-11-10 audit, remediation zone R3
(MCP relay & federation): P1-5, P1-8, P2-3(read side), P2-4, P2-11, P2-23,
P2-32, P2-33, P2-35 and the matching P3 lines.

Every test here fails on the pre-fix code and pins the FIXED behaviour.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from pcai_llm.api import routes_mcp
from pcai_llm.core import toolcompress, toolcompress_store, toolrelay
from pcai_llm.mcpf import arg_policy, federation

KEY = uuid.uuid4()


# ---------------------------------------------------------------------------
# P1-5 — the gateway_mcp_calls_total{server} label is REGISTRY-bounded
# ---------------------------------------------------------------------------


def _sample(server: str, outcome: str, model: str = "unattributed") -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value("gateway_mcp_calls_total", {"server": server, "outcome": outcome, "model": model})
    return 0.0 if value is None else float(value)


@pytest.fixture()
def quiet_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """_settle_quiet's two side effects (ledger settle + chained audit row)."""

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(routes_mcp, "settle", noop)
    from pcai_llm.core import chain_audit

    monkeypatch.setattr(chain_audit, "write_mcp_call", noop)


def test_unknown_server_call_ticks_the_bounded_bucket(quiet_settle: None) -> None:
    """An unknown-server /mcp call must tick server="unknown", never the
    caller's string — the label is what a caller can mint series with."""
    pytest.importorskip("prometheus_client")
    caller_name = "minted-" + uuid.uuid4().hex
    before_unknown = _sample("unknown", "error")
    before_caller = _sample(caller_name, "error")
    asyncio.run(routes_mcp._settle_quiet(KEY, f"mcp:{caller_name}:t", "error", time.monotonic(), "rid"))
    assert _sample("unknown", "error") == before_unknown + 1
    assert _sample(caller_name, "error") == before_caller, "the caller's string became a label"


def test_resolved_server_call_ticks_the_registry_name(quiet_settle: None) -> None:
    """The resolved path keeps full attribution (the fix must not blind the
    metric): a registry name passed as metric_server is the label."""
    pytest.importorskip("prometheus_client")
    before = _sample("k8s-ops", "ok")
    asyncio.run(
        routes_mcp._settle_quiet(
            KEY, "mcp:k8s-ops:get_resource", "ok", time.monotonic(), "rid", metric_server="k8s-ops"
        )
    )
    assert _sample("k8s-ops", "ok") == before + 1


def test_relay_acl_denial_ticks_unknown_for_a_hallucinated_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay's in-loop ACL denial derives its server from MODEL text, so a
    hallucinated "<random>__x" name must land in the fixed bucket."""
    pytest.importorskip("prometheus_client")

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    class _Gate:
        ok = True

    async def ok_gate(*_a: Any, **_k: Any) -> _Gate:
        return _Gate()

    monkeypatch.setattr(toolrelay, "check_budgets", ok_gate)
    monkeypatch.setattr(toolrelay, "settle", noop)
    monkeypatch.setattr(toolrelay.traces, "record", noop)
    from pcai_llm.core import chain_audit

    monkeypatch.setattr(chain_audit, "write_mcp_call", noop)

    ghost = "ghost-" + uuid.uuid4().hex
    script = [
        {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": f"{ghost}__x", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
        {"content": "done"},
    ]
    idx = {"n": 0}

    def stream_once(_msgs: Any, _tools: Any) -> Any:
        r = script[min(idx["n"], len(script) - 1)]
        idx["n"] += 1

        async def raw():
            chunks = []
            if r.get("content"):
                chunks.append({"choices": [{"delta": {"content": r["content"]}}]})
            for tc in r.get("tool_calls") or []:
                chunks.append({"choices": [{"delta": {"tool_calls": [{"index": 0, **tc}]}}]})
            if "usage" in r:
                chunks.append({"usage": r["usage"]})
            for c in chunks:
                yield b"data: " + json.dumps(c).encode() + b"\n\n"

        class R:
            async def aiter_bytes(self):
                async for b in raw():
                    yield b

        async def gen():
            async for ev in toolrelay.stream_model_call(R()):
                yield ev

        return gen()

    async def execute(_server: str, _tool: str, _args: dict) -> tuple[bool, Any]:
        raise AssertionError("an ACL-denied tool must never execute")

    async def run() -> str:
        return b"".join(
            [
                chunk
                async for chunk in toolrelay.relay_stream(
                    [{"role": "user", "content": "go"}],
                    {("logsearch", "search_logs")},
                    [{"server": "logsearch", "tool": "search_logs", "description": "s"}],
                    stream_once,
                    execute,
                    "rid",
                    "m1",
                    key_id=KEY,
                )
            ]
        ).decode()

    before_unknown = _sample("unknown", "denied")
    before_ghost = _sample(ghost, "denied")
    text = asyncio.run(run())
    assert '"detail": "denied by ACL"' in text
    assert _sample("unknown", "denied") == before_unknown + 1
    assert _sample(ghost, "denied") == before_ghost, "the model's string became a label"


# ---------------------------------------------------------------------------
# P1-8 — the DEFAULT tools/list fan-out is bounded-CONCURRENT, not serial
# ---------------------------------------------------------------------------


def _async_value(value: Any):
    async def fn(*_a: Any, **_k: Any) -> Any:
        return value

    return fn


def _servers(n: int) -> list[dict[str, Any]]:
    return [
        {
            "name": f"srv{i}",
            "url": f"http://srv{i}.svc.cluster.local:9100/mcp",
            "headers": {},
            "auth_secret": None,
            "auth_token_enc": None,
            "enabled": True,
            "tool_policy": "all",
            "allowed_tools": [],
            "priority": i,
        }
        for i in range(n)
    ]


def test_flat_tools_fan_out_is_concurrent(monkeypatch: pytest.MonkeyPatch) -> None:
    servers = _servers(3)
    state = {"active": 0, "max": 0}

    async def fake_fetch(server: dict[str, Any]) -> list[dict[str, Any]]:
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        await asyncio.sleep(0.02)
        state["active"] -= 1
        return [{"tool": "t", "description": "d"}]

    async def fake_grants(_key_id: Any) -> set[tuple[str, str]]:
        return {(s["name"], "t") for s in servers}

    monkeypatch.setattr(routes_mcp, "load_grants", fake_grants)
    monkeypatch.setattr(routes_mcp, "_cached_upstream_tools", fake_fetch)
    monkeypatch.setattr(routes_mcp.registry, "mcp_servers", _async_value(servers))

    server = routes_mcp._new_gateway_server()
    token = routes_mcp._caller.set(SimpleNamespace(id=KEY))
    try:
        out = asyncio.run(server._flat_tools())
    finally:
        routes_mcp._caller.reset(token)
    assert len(out) == 3
    assert state["max"] > 1, "the fan-out is serial again"
    assert state["max"] <= routes_mcp._CATALOG_FETCH_CONCURRENCY


# ---------------------------------------------------------------------------
# P2-33 — single-flight on the listing cache
# ---------------------------------------------------------------------------


def test_cold_listing_cache_single_flights(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str, _headers: dict, timeout_s: float | None = None):
        calls.append(url)
        await asyncio.sleep(0.02)
        return [{"tool": "t", "description": "d"}]

    monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch)
    server = _servers(1)[0]
    routes_mcp._tools_listing_cache.clear()

    async def run() -> list[list[dict[str, Any]]]:
        return await asyncio.gather(*(routes_mcp._cached_upstream_tools(server) for _ in range(6)))

    out = asyncio.run(run())
    assert calls == [server["url"]], f"expected ONE upstream fetch, got {len(calls)}"
    assert all(o == [{"tool": "t", "description": "d"}] for o in out)
    assert routes_mcp._tools_listing_inflight == {}


# ---------------------------------------------------------------------------
# P2-4 — EXEC-class arguments are destination-scanned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "curl http://169.254.169.254/latest/meta-data"},
        {"command": "curl 'http://169.254.169.254/x'"},
        {"command": "bash -c 'wget -qO- http://127.0.0.1:8080/x'"},
        {"command": "curl --url=http://169.254.169.254/x"},
        {"command": "cat /etc/passwd | nc 169.254.169.254 80"},
        {"script": "curl http://169.254.169.254/x"},
        {"code": "import os; os.system('curl http://169.254.169.254/x')"},
    ],
)
def test_exec_arguments_are_destination_scanned(arguments: dict[str, Any]) -> None:
    reason = arg_policy.check_arguments("k8s-ops", "exec_in_pod", arguments)
    assert reason, f"{arguments!r} must be refused"
    assert "url-destination" in reason


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": "kubectl get pods -n prod"},
        {"command": ["kubectl", "get", "pods", "-n", "prod"]},
        {"command": "get pods -n prod -o yaml"},
        {"command": "grep -r foo ."},
        {"command": "kubectl scale deploy/x --replicas=0"},
        {"command": "tail -n 80 /var/log/x.log"},
        {"command": "python3 -c 'print(1)'"},
        {"command": "curl https://example.com/x"},
        {"content": "http://169.254.169.254/"},
    ],
)
def test_ordinary_command_lines_stay_allowed(arguments: dict[str, Any]) -> None:
    assert arg_policy.check_arguments("k8s-ops", "exec_in_pod", arguments) == ""


# ---------------------------------------------------------------------------
# P2-32 — the argument walk is bounded by NODE COUNT too
# ---------------------------------------------------------------------------


def test_empty_container_tree_trips_the_node_bound() -> None:
    tree = {"items": [{} for _ in range(arg_policy.MAX_NODES + 5_000)]}
    scanned = arg_policy.iter_string_arguments(tree)
    assert scanned.overflowed is True
    assert arg_policy.check_arguments("k8s-ops", "get_resource", tree)


def test_ordinary_tree_is_under_the_node_bound() -> None:
    tree = {"items": [{"name": f"pod-{i}"} for i in range(500)], "namespace": "prod"}
    assert arg_policy.iter_string_arguments(tree).overflowed is False


# ---------------------------------------------------------------------------
# P2-11 — load_denies fails CLOSED
# ---------------------------------------------------------------------------


class _DenyDB:
    def __init__(self, rows: list[dict[str, Any]], raises: Exception | None = None) -> None:
        self._rows = rows
        self._raises = raises

    async def fetch(self, _sql: str, *_a: Any) -> list[dict[str, Any]]:
        if self._raises is not None:
            raise self._raises
        return self._rows


@pytest.fixture(autouse=True)
def _clear_deny_snapshots() -> Any:
    federation._deny_snapshots.clear()
    yield
    federation._deny_snapshots.clear()


def test_load_denies_propagates_with_no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(federation, "db", _DenyDB([], raises=RuntimeError("pool busted")))
    with pytest.raises(RuntimeError):
        asyncio.run(federation.load_denies(uuid.uuid4()))


def test_load_denies_serves_the_last_good_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    kid = uuid.uuid4()
    monkeypatch.setattr(federation, "db", _DenyDB([{"server": "s", "tool": "t"}]))
    assert asyncio.run(federation.load_denies(kid)) == {("s", "t")}
    monkeypatch.setattr(federation, "db", _DenyDB([], raises=RuntimeError("pool busted")))
    assert asyncio.run(federation.load_denies(kid)) == {("s", "t")}, "a blip must not re-open the pair"


# ---------------------------------------------------------------------------
# P2-23 — a diff ending on a change line still compresses
# ---------------------------------------------------------------------------

_DIFF_ENDS_ON_CHANGE = """diff --git a/x b/x
index 1111111..2222222 100644
--- a/x
+++ b/x
@@ -1,5 +1,6 @@
 context one
 context two
-old
+new
"""

_DIFF_ENDS_ON_CONTEXT = """diff --git a/x b/x
index 1111111..2222222 100644
--- a/x
+++ b/x
@@ -1,5 +1,6 @@
-old
+new
 context one
"""

_DIFF_NO_CONTEXT = """diff --git a/x b/x
index 1111111..2222222 100644
--- a/x
+++ b/x
@@ -1,5 +1,6 @@
-old
+new
"""


def test_diff_ending_on_a_change_line_compresses() -> None:
    out = toolcompress._compress_diff(_DIFF_ENDS_ON_CHANGE, "d", toolcompress.DEFAULT_BUDGET_CHARS)
    assert out is not None, "the old omitted == 0 test only asked about the LAST line"
    assert "+new" in out and "context lines omitted" in out


def test_diff_ending_on_context_still_compresses() -> None:
    out = toolcompress._compress_diff(_DIFF_ENDS_ON_CONTEXT, "d", toolcompress.DEFAULT_BUDGET_CHARS)
    assert out is not None and "+new" in out


def test_diff_without_context_lines_passes_through() -> None:
    assert toolcompress._compress_diff(_DIFF_NO_CONTEXT, "d", toolcompress.DEFAULT_BUDGET_CHARS) is None


# ---------------------------------------------------------------------------
# P3 — _compress_search keeps ERROR-signal matches past the 60-match cap
# ---------------------------------------------------------------------------


def test_search_signal_match_survives_the_match_cap() -> None:
    lines = [f"/var/log/pod-{i}.log:{i}: ordinary info line" for i in range(80)]
    lines.append("/var/log/pod-99.log:99: permission denied opening socket")
    out = toolcompress._compress_search("\n".join(lines), "d", toolcompress.DEFAULT_BUDGET_CHARS)
    assert out is not None
    assert "permission denied" in out, "the error signal was dropped by the 60-match cap"


# ---------------------------------------------------------------------------
# P2-35 — compression has an INPUT ceiling (no multi-MB parse on the loop)
# ---------------------------------------------------------------------------


def _json_array_text(n: int = 200) -> str:
    return json.dumps([{"id": i, "msg": "request completed in 12ms"} for i in range(n)])


def test_compression_skips_the_parse_above_the_input_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    text = _json_array_text()
    assert len(text) > 4_000
    seen: list[str] = []
    real = toolcompress.detect_kind
    monkeypatch.setattr(toolcompress, "detect_kind", lambda t: seen.append(t) or real(t))
    assert toolcompress.compress_tool_text(text, max_input_chars=1_000) is None
    assert seen == [], "the candidate was parsed despite the ceiling"
    assert toolcompress.compress_tool_text(text, max_input_chars=len(text) + 1) is not None


def test_default_input_ceiling_is_absolute() -> None:
    """The ceiling bounds the PARSE, so it is absolute — a small caller budget
    must not lower it (a 700 KB payload compressed to 8,000 chars is the
    feature working, not an attack)."""
    ceiling = toolcompress.MAX_INPUT_CHARS
    assert ceiling > 500_000, "a legitimate sub-MB payload must still compress"
    huge = "[" + ",".join(['{"a":1}'] * (ceiling // 8 + 1_000)) + "]"
    assert len(huge) > ceiling
    assert toolcompress.compress_tool_text(huge) is None


def test_relay_passes_the_store_cap_as_the_input_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.config import settings

    captured: dict[str, Any] = {}

    def fake_compress(text: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return None

    monkeypatch.setattr(toolcompress, "compress_tool_text", fake_compress)
    asyncio.run(
        toolrelay._prepare_tool_content(
            [{"type": "text", "text": _json_array_text()}], mode="on", key_id=KEY, server="s", tool="t"
        )
    )
    assert captured["max_input_chars"] == settings.tool_compress_max_store_chars


# ---------------------------------------------------------------------------
# P3 — not_stored stats never publish a digest that was not persisted
# ---------------------------------------------------------------------------


def test_not_stored_stats_blank_the_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken_save(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("postgres down")

    monkeypatch.setattr(toolcompress_store, "save", broken_save)
    body = _json_array_text(300)
    text, stats = asyncio.run(
        toolrelay._prepare_tool_content([{"type": "text", "text": body}], mode="on", key_id=KEY, server="s", tool="t")
    )
    assert stats is not None
    assert stats["stored"] is False
    assert stats["digest"] == ""
    # The pass-through form is the SERIALIZED envelope (the pre-fix shape), and
    # it must carry no compression marker: a marker would point at a digest
    # nothing was stored under.
    assert toolcompress.MARKER_SIG not in text
    assert json.loads(text)[0]["text"] == body


# ---------------------------------------------------------------------------
# P3 — per-key quota in tool_compress_store (and the chars column is read)
# ---------------------------------------------------------------------------


class _StoreDB:
    def __init__(self, used: int, present: bool = False) -> None:
        self.used = used
        self.present = present
        self.inserts: list[tuple[Any, ...]] = []
        self.queries: list[str] = []

    async def fetchrow(self, sql: str, *_a: Any) -> dict[str, Any]:
        self.queries.append(sql)
        assert "chars" in sql, "the quota must read the chars column"
        return {"used": self.used, "present": self.present}

    async def execute(self, _sql: str, *args: Any) -> str:
        self.inserts.append(args)
        return "INSERT 0 1"


def test_store_refuses_when_the_key_quota_is_full(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _StoreDB(used=toolcompress_store.KEY_QUOTA_CHARS - 10)
    monkeypatch.setattr(toolcompress_store, "db", fake)
    with pytest.raises(toolcompress_store.StoreQuotaExceeded):
        asyncio.run(toolcompress_store.save(KEY, "d1", "x" * 100))
    assert fake.inserts == [], "a quota breach must not write the row"


def test_store_allows_an_idempotent_resave_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _StoreDB(used=toolcompress_store.KEY_QUOTA_CHARS, present=True)
    monkeypatch.setattr(toolcompress_store, "db", fake)
    asyncio.run(toolcompress_store.save(KEY, "d1", "x" * 100))
    assert len(fake.inserts) == 1


def test_store_writes_under_the_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _StoreDB(used=1_000)
    monkeypatch.setattr(toolcompress_store, "db", fake)
    asyncio.run(toolcompress_store.save(KEY, "d1", "x" * 100))
    assert len(fake.inserts) == 1


# ---------------------------------------------------------------------------
# P2-3 (read side) — override header VALUES are charset-validated
# ---------------------------------------------------------------------------


class _CredsDB:
    def __init__(self, raw: str) -> None:
        self.raw = raw

    async def fetchrow(self, _sql: str, *_a: Any) -> dict[str, Any]:
        return {"headers_enc": "enc"}


def test_unsafe_override_header_values_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    tab = chr(9)
    payload = json.dumps(
        {
            "X-Good": "plain-value",
            "X-Tab": "a" + tab + "b",
            "X-CRLF": "evil" + chr(13) + chr(10) + "X-Injected: 1",
        }
    )
    monkeypatch.setattr(routes_mcp, "db", _CredsDB(payload))
    monkeypatch.setattr(routes_mcp.secretbox, "decrypt", lambda _enc: payload)
    headers = asyncio.run(routes_mcp.key_override_headers(KEY, "srv"))
    assert headers == {"X-Good": "plain-value", "X-Tab": "a" + tab + "b"}


def test_unicode_override_header_value_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"X-Unicode": "sm" + chr(246) + "rgas"})
    monkeypatch.setattr(routes_mcp, "db", _CredsDB(payload))
    monkeypatch.setattr(routes_mcp.secretbox, "decrypt", lambda _enc: payload)
    assert asyncio.run(routes_mcp.key_override_headers(KEY, "srv")) == {}


# ---------------------------------------------------------------------------
# P3 — preset admin-prefix rules go through is_admin_only_tool (case-insensitive)
# ---------------------------------------------------------------------------


def test_preset_admin_rules_are_case_insensitive() -> None:
    catalog = {
        ("sqlhandler", "Admin_Grants"),
        ("sqlhandler", "run_sql"),
        ("k8s-ops", "get_resource"),
    }
    observe = federation.preset_pairs("observe", catalog)
    assert ("sqlhandler", "Admin_Grants") not in observe
    admin = federation.preset_pairs("admin", catalog)
    assert ("sqlhandler", "Admin_Grants") in admin


def test_admin_prefix_on_a_non_admin_server_is_not_admin_only() -> None:
    """The documented contract (MCP_ADMIN_ONLY_TOOL_PREFIXES): the prefix is
    only meaningful on the admin-class server, and the CALL-TIME gate
    (is_admin_only_tool / admin_tool_refusal) never refuses a tool merely
    NAMED admin_* elsewhere — so a stamp may carry it."""
    assert federation.is_admin_only_tool("k8s-ops", "admin_debug") is False
    assert federation.is_admin_only_tool("sqlhandler", "Admin_Grants") is True


# ---------------------------------------------------------------------------
# P3 — the catalog never renders USERINFO from an upstream URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://alpha.svc.cluster.local:9100/mcp", "http://alpha.svc.cluster.local:9100/mcp"),
        ("http://svc", "http://svc"),
        ("", ""),
        ("http://user:pass@alpha.svc:9100/mcp", "http://alpha.svc:9100/mcp"),
        ("https://token@alpha.svc/mcp?x=1", "https://alpha.svc/mcp?x=1"),
    ],
)
def test_catalog_url_strips_userinfo(url: str, expected: str) -> None:
    assert routes_mcp._catalog_url(url) == expected


# ---------------------------------------------------------------------------
# Cross-validation wave 1 — exec-scan bypasses (P1 x2, P3 x1)
# ---------------------------------------------------------------------------


def test_padded_command_is_refused_not_truncated() -> None:
    """P1: the token slice used to scan a PREFIX and allow the rest, so ~96
    characters of padding hid the destination. The bound is now a REFUSAL
    (REASON_TOO_COMPLEX), matching iter_string_arguments' stated posture."""
    padded = "A=1 " * 32 + "curl http://169.254.169.254/x"
    reason = arg_policy.check_arguments("k8s-ops", "exec_in_pod", {"command": padded})
    assert "arguments-too-complex" in reason


def test_command_at_the_token_bound_is_still_scanned() -> None:
    """The refusal must not swallow ordinary long command lines: a command AT
    the bound is scanned in full, so a destination in its last token is found
    and a destination-free one is allowed."""
    # exactly _EXEC_MAX_TOKENS tokens: the bound is "more than", not "at least"
    at_bound = " ".join(["arg"] * (arg_policy._EXEC_MAX_TOKENS - 2) + ["curl", "http://169.254.169.254/x"])
    assert len(at_bound.split()) == arg_policy._EXEC_MAX_TOKENS
    assert "url-destination" in arg_policy.check_arguments("k8s-ops", "exec_in_pod", {"command": at_bound})
    plain = " ".join(["arg"] * (arg_policy._EXEC_MAX_TOKENS - 1))
    assert arg_policy.check_arguments("k8s-ops", "exec_in_pod", {"command": plain}) == ""


@pytest.mark.parametrize(
    "command",
    [
        "curl http://%31%32%37.0.0.1/",
        "curl http://%31%36%39.254.169.254/",
        "wget http://%31%36%39%2e254.169.254/latest/meta-data",
        "curl --url=%31%32%37.0.0.1:8080/x",
    ],
)
def test_percent_encoded_destination_in_a_command_is_refused(command: str) -> None:
    """P1: _exec_reason used to return before the unquote() the main path
    applies, so a percent-encoded host parsed as an unresolvable NAME and was
    allowed while the client decodes it and connects to the denied address."""
    reason = arg_policy.check_arguments("k8s-ops", "exec_in_pod", {"command": command})
    assert reason, f"{command!r} must be refused"
    assert "url-destination" in reason


@pytest.mark.parametrize(
    "command",
    [
        "curl 2130706433",
        "curl 0x7f000001",
        "nc 169.254.169.254 80",
    ],
)
def test_compact_ip_literal_in_a_command_is_refused(command: str) -> None:
    """P3: a bare decimal/hex address has no '.' or ':' (and a decimal above
    2^24 does not decode into 0.0.0.0/8), so the old shape gate skipped it."""
    reason = arg_policy.check_arguments("k8s-ops", "exec_in_pod", {"command": command})
    assert "url-destination" in reason


# ---------------------------------------------------------------------------
# Resolver saturation — the argument stage REFUSES, it does not skip
# ---------------------------------------------------------------------------


def test_resolver_saturation_refuses_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A saturated bounded resolver means the destination was NEVER VETTED.
    url_guard raises a distinct ResolverSaturated (deliberately not a
    gaierror/OSError) so this stage can refuse instead of taking the
    fail-open "cannot resolve" path — otherwise a model that wedges the pool
    switches the SSRF gate off for its own next call."""

    def saturated(*_a: Any, **_k: Any) -> list[str]:
        raise arg_policy.url_guard.ResolverSaturated("pool full")

    monkeypatch.setattr(arg_policy.url_guard, "resolve_host_addresses", saturated)
    # check_string PROPAGATES it (the classification helper stays honest about
    # "the check did not happen")...
    with pytest.raises(arg_policy.url_guard.ResolverSaturated):
        arg_policy.check_string("searxng", "fetch_content", "url", "https://example.com/x")
    # ...and check_arguments turns it into a bounded REFUSAL, not an allow.
    reason = arg_policy.check_arguments("searxng", "fetch_content", {"url": "https://example.com/x"})
    assert reason, "saturation must REFUSE, not allow"
    assert "resolver-saturated" in reason


def test_unresolvable_host_still_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented degradation is unchanged: a name the resolver cannot
    answer says nothing about the name, so the call proceeds (url_guard design
    rule 4). Only saturation is a refusal."""
    import socket

    def unresolvable(*_a: Any, **_k: Any) -> list[str]:
        raise socket.gaierror(socket.EAI_NONAME, "no such host")

    monkeypatch.setattr(arg_policy.url_guard, "resolve_host_addresses", unresolvable)
    assert arg_policy.check_arguments("searxng", "fetch_content", {"url": "https://example.com/x"}) == ""


def test_short_numbers_in_a_command_stay_exempt() -> None:
    """The compact-literal rule must not eat ports/counts: "0", "80" and "443"
    are ports, and the literal decoder reads them as 0.0.x.y (denied)."""
    for command in ("sleep 0", "tail -n 80 f", "curl --max-time 443 https://example.com/x"):
        assert arg_policy.check_arguments("k8s-ops", "exec_in_pod", {"command": command}) == ""
    assert arg_policy._EXEC_MIN_BARE_LITERAL >= 4
