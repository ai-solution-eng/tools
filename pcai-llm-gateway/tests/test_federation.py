"""MCP federation ACL semantics — the default-DENY contract.

A multi-tenant gateway must never leak a tool to a key that was not
explicitly granted it: a server-level allowlist is trivially bypassed the
moment a server gains one new tool, so grants are per (server, tool).
"""

from __future__ import annotations

import pytest

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

# ---------------------------------------------------------------------------
# Server-class taxonomy + presets + admin hard stop (P0-3, hardening wave)
# ---------------------------------------------------------------------------

from pcai_llm.mcpf.federation import (  # noqa: E402
    EGRESS_SERVERS,
    MCP_ADMIN_ONLY_TOOL_PREFIXES,
    MCP_PROFILE_PRESETS,
    MCP_SERVER_CLASSES,
    READ_SERVERS,
    admin_tool_refusal,
    is_admin_only_tool,
    is_write_tool,
    preset_catalog_pairs,
    preset_pairs,
    read_egress_violation,
    separate_read_egress_mode,
)

# a fleet-shaped catalog: read servers, the egress server, mutate servers,
# sqlhandler carrying BOTH read tools and admin verbs
FLEET = [
    {"server": "k8s-ops", "tool": "get_pod_logs"},
    {"server": "k8s-ops", "tool": "triage"},
    {"server": "k8s-ops", "tool": "exec_in_pod"},
    {"server": "k8s-ops", "tool": "run_kubectl"},
    {"server": "prometheus", "tool": "prom_query"},
    {"server": "logsearch", "tool": "search_logs"},
    {"server": "mm-rag", "tool": "search_dataset"},
    # REAL registered write verbs (wave-1 review P1-2) — the catalog itself,
    # so the observe assertions below are against actual names, not the rule
    {"server": "mm-rag", "tool": "dataset_add_documents"},
    {"server": "mm-rag", "tool": "dataset_delete_documents"},
    {"server": "mm-rag", "tool": "dataset_replace_document"},
    {"server": "mm-rag", "tool": "add_memory"},
    {"server": "mm-rag", "tool": "delete_memory"},
    {"server": "mm-rag", "tool": "forget_session"},
    {"server": "mm-rag", "tool": "set_memory_dataset"},
    {"server": "mm-rag", "tool": "deselect_dataset"},
    {"server": "mm-rag", "tool": "unlock_dataset"},
    {"server": "mm-rag", "tool": "create_short_term_memory"},
    {"server": "mm-rag", "tool": "end_short_term_memory"},
    {"server": "seaborn", "tool": "plot"},
    {"server": "sqlhandler", "tool": "run_sql"},
    {"server": "sqlhandler", "tool": "query_save"},
    {"server": "sqlhandler", "tool": "query_delete"},
    {"server": "sqlhandler", "tool": "admin_grants"},
    {"server": "sqlhandler", "tool": "admin_key_mint"},
    {"server": "searxng", "tool": "search"},
    {"server": "searxng", "tool": "fetch_content"},
    {"server": "applygate", "tool": "apply_manifest"},
    {"server": "applygate", "tool": "plan_apply"},
    {"server": "applygate", "tool": "delete_resource"},
    {"server": "applygate", "tool": "plan_delete"},
    {"server": "ezapp-deploy", "tool": "apply_ezappconfig"},
    {"server": "workbench", "tool": "write_file"},
]
FLEET_PAIRS = {(e["server"], e["tool"]) for e in FLEET}


def test_class_table_is_the_single_source() -> None:
    # the derived views are projections of the ONE constant, and the four
    # presets reference only classes the table actually defines
    classes = {"read", "egress", "mutate", "admin"}
    assert all(c <= classes for c in MCP_SERVER_CLASSES.values())
    assert EGRESS_SERVERS == {"searxng"}
    assert "sqlhandler" in READ_SERVERS and "searxng" not in READ_SERVERS
    assert set(MCP_PROFILE_PRESETS) == {"observe", "egress", "mutate", "admin"}


def test_admin_only_tool_prefix_scoped_to_sqlhandler() -> None:
    assert MCP_ADMIN_ONLY_TOOL_PREFIXES == ("admin_",)
    assert is_admin_only_tool("sqlhandler", "admin_key_mint")
    assert is_admin_only_tool("sqlhandler", "admin_policy_set")
    # a tool merely NAMED admin_* elsewhere is NOT admin-only
    assert not is_admin_only_tool("k8s-ops", "admin_thing")
    assert not is_admin_only_tool("sqlhandler", "run_sql")
    assert not is_admin_only_tool("sqlhandler", "whoami")


def test_admin_tool_refusal_control_default() -> None:
    # DEFAULT FALSE is the control: an unflagged key is refused, a flagged
    # key passes, and the refusal never leaks policy details
    refusal = admin_tool_refusal("sqlhandler", "admin_key_mint", False)
    assert refusal
    assert "mcp_admin" in refusal
    assert admin_tool_refusal("sqlhandler", "admin_key_mint", True) == ""
    # non-admin tools are never refused by this gate regardless of the flag
    assert admin_tool_refusal("sqlhandler", "run_sql", False) == ""
    assert admin_tool_refusal("k8s-ops", "triage", False) == ""


def test_read_egress_violation_detects_the_exfiltration_shape() -> None:
    # the article's attack shape: read a source AND hold the sender
    grants = {("logsearch", "search_logs"), ("searxng", "fetch_content")}
    assert read_egress_violation(grants) == (("logsearch", "search_logs"), ("searxng", "fetch_content"))
    # read-only: clean. egress-only: clean. empty: clean.
    assert read_egress_violation({("logsearch", "search_logs")}) is None
    assert read_egress_violation({("searxng", "search")}) is None
    assert read_egress_violation(set()) is None
    # deterministic pick: same set -> same reported pair, every time
    big = FLEET_PAIRS
    assert read_egress_violation(big) == read_egress_violation(set(big))


def test_preset_pairs_resolution() -> None:
    observe = set(preset_pairs("observe", FLEET_PAIRS))
    # reads survive; EVERYTHING state-changing is cut (rules 1+3)
    assert ("k8s-ops", "get_pod_logs") in observe
    assert ("mm-rag", "search_dataset") in observe
    assert ("sqlhandler", "admin_grants") not in observe  # rule 1
    # wave-2: the READ-ONLY run tools MUST ride observe — run_sql is
    # SELECT-only by contract, run_kubectl is annotated read_only_hint: true;
    # wave-1's fix regressed this (observe could list tables but not select
    # a row) and the old test asserted the regression
    assert ("sqlhandler", "run_sql") in observe  # READ_ONLY_RUN_TOOLS exception
    assert ("k8s-ops", "run_kubectl") in observe  # READ_ONLY_RUN_TOOLS exception
    assert ("sqlhandler", "query_save") not in observe  # rule 3 (mid-name save_)
    assert ("k8s-ops", "exec_in_pod") not in observe  # rule 3
    assert ("mm-rag", "dataset_delete_documents") not in observe  # rule 3 (segment)
    assert ("searxng", "search") not in observe  # egress never rides observe
    assert ("applygate", "apply_manifest") not in observe

    egress = set(preset_pairs("egress", FLEET_PAIRS))
    assert egress == {("searxng", "search"), ("searxng", "fetch_content")}

    mutate = set(preset_pairs("mutate", FLEET_PAIRS))
    assert {("applygate", "apply_manifest"), ("applygate", "plan_apply"),
            ("applygate", "delete_resource"), ("applygate", "plan_delete"),
            ("ezapp-deploy", "apply_ezappconfig"), ("workbench", "write_file"),
            ("mm-rag", "dataset_delete_documents"), ("mm-rag", "add_memory")} <= mutate
    # sqlhandler carries no mutate CLASS, so its write verbs ride no preset:
    # they are reachable only through explicit per-key/per-profile grants
    assert ("sqlhandler", "run_sql") not in mutate
    assert not {p for p in mutate if p[0] in EGRESS_SERVERS}

    admin = set(preset_pairs("admin", FLEET_PAIRS))
    assert admin == {("sqlhandler", "admin_grants"), ("sqlhandler", "admin_key_mint")}


def test_observe_preset_against_real_catalog_carries_no_write_verbs() -> None:
    """Wave-1 review P1-2, pinned the right way round: assert against the
    CATALOG (real registered tool names), not against the rule — for every
    (server, tool) pair the observe preset stamps, the tool name must NOT
    match any write prefix/segment/name. This is the test that was missing
    when the rule asserted itself and 14 mutating verbs slipped through."""
    from pcai_llm.mcpf.federation import READ_ONLY_RUN_TOOLS

    observe = preset_pairs("observe", FLEET_PAIRS)
    assert observe, "observe stamp resolved nothing — the test would be vacuous"
    for server, tool in observe:
        # the ONLY write-shaped verbs allowed are the two documented
        # read-by-contract run tools (wave-2); every other write verb,
        # every admin verb, every egress server stays out
        if is_write_tool(tool):
            assert (server, tool) in READ_ONLY_RUN_TOOLS, f"observe stamps write verb {server}.{tool}"
        assert not tool.startswith(MCP_ADMIN_ONLY_TOOL_PREFIXES), f"observe stamps admin verb {server}.{tool}"
        assert server in READ_SERVERS and server not in EGRESS_SERVERS
    # and the read-by-contract pair is PRESENT (the wave-1 regression pin)
    assert {("sqlhandler", "run_sql"), ("k8s-ops", "run_kubectl")} <= set(observe)


def test_write_verb_rule_pins_the_reviewed_catalog() -> None:
    """The rule itself, against the actual fleet tool inventory: every verb
    named in the wave-1 review (plus the neighbors it implies) is a write;
    every plainly-read tool is not. If a server renames a tool into or out
    of these shapes, this test is the tripwire to review the constant."""
    for t in (
        # mm-rag (the wave-1 review list, verbatim)
        "dataset_delete_documents", "end_short_term_memory", "dataset_add_documents",
        "dataset_replace_document", "add_memory", "delete_memory", "forget_session",
        "set_memory_dataset", "deselect_dataset", "unlock_dataset",
        "create_short_term_memory",
        # sqlhandler persistence verbs
        "query_save", "query_delete", "query_cancel",
        # k8s-ops execution
        "exec_in_pod", "run_kubectl",
        # applygate / ezapp-deploy destructive verbs
        "plan_delete", "delete_chart", "apply_manifest", "apply_ezappconfig",
        # workbench writes
        "write_file", "delete_file", "sandbox_run",
    ):
        assert is_write_tool(t), t
    # wave-2: the two read-only run tools are write-SHAPED but read-only BY
    # CONTRACT — the WRITE rule still classifies them (it is name-based and
    # must stay simple); the PRESET rule carries the exact-pair exception.
    # If either server widens the verb's behavior, remove the pair from
    # READ_ONLY_RUN_TOOLS and this line fails loudly.
    from pcai_llm.mcpf.federation import READ_ONLY_RUN_TOOLS

    assert READ_ONLY_RUN_TOOLS == {("sqlhandler", "run_sql"), ("k8s-ops", "run_kubectl")}
    assert is_write_tool("run_sql") and is_write_tool("run_kubectl")  # shape, unchanged
    for t in (
        "search_dataset", "get_dataset_files", "search_logs", "get_pod_logs",
        "prom_query", "triage", "cluster_health", "check_rbac",
        "verify_audit_chain", "sample_rows", "query_result", "query_status",
        "column_stats", "describe_media", "transcribe_audio", "whoami",
        "search", "fetch_content", "describe_table", "profile_table",
        "get_resource",
    ):
        assert not is_write_tool(t), t
    # plan_apply MATCHES the apply segment by design (documented in the
    # constant): a server-side dry-run is still the mutate server's surface,
    # so it is kept out of every non-mutate stamp. Under-provision is the
    # safe direction; workbench.sandbox_run likewise matches "run".
    assert is_write_tool("plan_apply")


def test_admin_only_tool_case_insensitive() -> None:
    """Wave-1 review P3: case must not gate a security predicate. Not
    exploitable today (the pair ACL grants exact strings and sqlhandler's
    dispatch is exact-match) but the predicate must not depend on that."""
    assert is_admin_only_tool("SQLHandler", "Admin_Grants")
    assert is_admin_only_tool("sqlhandler", "ADMIN_KEY_MINT")
    assert not is_admin_only_tool("k8s-ops", "Admin_Thing")


def test_preset_pairs_unknown_preset_raises() -> None:
    import pytest as _pytest
    with _pytest.raises(ValueError):
        preset_pairs("everything", FLEET_PAIRS)  # a typo must be loud


def test_preset_catalog_pairs_shape() -> None:
    catalogs = {"k8s-ops": [{"tool": "triage"}], "searxng": [], "dead": None}
    assert preset_catalog_pairs(catalogs) == {("k8s-ops", "triage")}


def test_separate_read_egress_mode_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.config import settings
    assert separate_read_egress_mode() == "warn"  # the DEFAULT (spec §3 D3.3)
    assert separate_read_egress_mode("enforce") == "enforce"
    assert separate_read_egress_mode("off") == "off"
    assert separate_read_egress_mode("bogus") == "warn"  # fail-safe, never boot-fatal
    monkeypatch.setattr(settings, "mcp_separate_read_egress", "enforce")
    assert separate_read_egress_mode() == "enforce"
    # wave-1 review P2-2: case/whitespace must not silently degrade to warn —
    # " ENFORCE " resolving to warn would be the exact not-enforcing failure
    # the fail-safe default exists to avoid
    assert separate_read_egress_mode(" ENFORCE ") == "enforce"
    assert separate_read_egress_mode("Enforce") == "enforce"
    assert separate_read_egress_mode("OFF") == "off"
    assert separate_read_egress_mode(" warn\t") == "warn"
    assert separate_read_egress_mode("bogus") == "warn"  # still fail-safe


def test_read_egress_metric_reason_vocabulary() -> None:
    """Wave-1 review P2-3b: warn must never tick the 'blocked' reason. The
    series gateway_mcp_policy_blocked_total means BLOCKED; the warn-mode
    reason carries a distinct '-warn' suffix (Z1 imports the constants)."""
    from pcai_llm.mcpf.federation import (
        MCP_READ_EGRESS_ENFORCE_REASON,
        MCP_READ_EGRESS_WARN_REASON,
    )

    assert MCP_READ_EGRESS_WARN_REASON == "read+egress-warn"
    assert MCP_READ_EGRESS_ENFORCE_REASON == "read+egress"
    assert MCP_READ_EGRESS_WARN_REASON != MCP_READ_EGRESS_ENFORCE_REASON


def test_stamping_a_preset_changes_what_a_profile_key_sees() -> None:
    # live-grants profile: the key of a preset-stamped profile sees the
    # preset's pairs AT CALL TIME (mcpf.load_live_profile_grants folds
    # defaults_json["mcp_grants"] into load_grants) — here at the pure
    # visible_tools layer, before vs after an observe stamp
    before_grants: set[tuple[str, str]] = set()
    assert visible_tools(FLEET, before_grants) == []  # default-deny before stamping

    observe = set(preset_pairs("observe", FLEET_PAIRS))
    seen = visible_tools(FLEET, observe)
    seen_pairs = {(t["server"], t["tool"]) for t in seen}
    assert seen_pairs == observe
    # and the exfiltration combination is UNREACHABLE from an observe key:
    # no egress pair, no admin verb
    assert not seen_pairs & {("searxng", "search"), ("searxng", "fetch_content")}
    assert not {p for p in seen_pairs if is_admin_only_tool(*p)}
