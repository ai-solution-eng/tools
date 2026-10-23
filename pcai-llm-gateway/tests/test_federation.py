"""MCP federation ACL semantics — the default-DENY contract.

A multi-tenant gateway must never leak a tool to a key that was not
explicitly granted it: a server-level allowlist is trivially bypassed the
moment a server gains one new tool, so grants are per (server, tool).
"""

from __future__ import annotations

from pcai_llm.mcpf.federation import (
    DEFAULT_DENY,
    is_allowed,
    visible_tools,
)

CATALOG = [
    {"server": "applygate", "tool": "apply_manifest", "description": "governed apply"},
    {"server": "applygate", "tool": "plan_apply", "description": "dry-run"},
    {"server": "logsearch", "tool": "search_logs", "description": "log search"},
    {"server": "logsearch", "tool": "get_pod_logs", "description": "pod logs"},
]


def test_default_deny_constant() -> None:
    assert DEFAULT_DENY is True


def test_no_grants_see_nothing() -> None:
    assert visible_tools(CATALOG, set()) == []


def test_granted_pairs_are_visible() -> None:
    grants = {("logsearch", "search_logs")}
    seen = visible_tools(CATALOG, grants)
    assert [t["tool"] for t in seen] == ["search_logs"]


def test_is_allowed_exact_pair_only() -> None:
    grants = {("applygate", "plan_apply")}
    assert is_allowed(grants, "applygate", "plan_apply")
    # server-level wildcard does NOT exist — a new tool on a granted server
    # is invisible until explicitly granted
    assert not is_allowed(grants, "applygate", "apply_manifest")
    assert not is_allowed(grants, "logsearch", "search_logs")


def test_unknown_server_never_allowed() -> None:
    assert not is_allowed({("x", "y")}, "unknown", "tool")
