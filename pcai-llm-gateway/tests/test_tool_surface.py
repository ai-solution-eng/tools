"""Two-level tool surface (2026-10-28).

WHY: inlining every granted MCP tool cost 34,459 prompt tokens for a
111-tool / 10-server catalogue -- a floor on every relayed request, and the
reason a 236k conversation overflowed a 262k model window (upstream 400).
Hybrid mode offers ONE discovery tool and inlines a server's real tools only
after the model asks for them.
"""

from __future__ import annotations

import json

from pcai_llm.core import tool_surface as ts
from pcai_llm.core.toolrelay import TOOLSEP, tools_payload


def _cat() -> list[dict]:
    return [
        {
            "server": "prometheus",
            "tool": "prom_query",
            "description": "Run a PromQL query. " + "x" * 400,
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
        {
            "server": "prometheus",
            "tool": "prom_alerts",
            "description": "List firing alerts",
            "inputSchema": {"type": "object"},
        },
        {"server": "k8s-ops", "tool": "list_pods", "description": "List pods", "inputSchema": {}},
    ]


def test_full_mode_is_byte_identical_to_tools_payload() -> None:
    """Rollback safety: mode="full" must be exactly the pre-change surface."""
    assert ts.build_tools(_cat(), mode=ts.MODE_FULL, sep=TOOLSEP) == tools_payload(_cat())


def test_hybrid_offers_only_the_meta_tool_before_promotion() -> None:
    tools = ts.build_tools(_cat(), mode=ts.MODE_HYBRID, sep=TOOLSEP)
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == ts.META_TOOL
    # the meta-tool must NOT look like a granted tool (parse_tool_name splits on TOOLSEP)
    assert TOOLSEP not in ts.META_TOOL


def test_hybrid_inlines_a_promoted_server_only() -> None:
    tools = ts.build_tools(_cat(), mode=ts.MODE_HYBRID, promoted={"prometheus"}, sep=TOOLSEP)
    names = [t["function"]["name"] for t in tools]
    assert names[0] == ts.META_TOOL
    assert f"prometheus{TOOLSEP}prom_query" in names
    assert f"prometheus{TOOLSEP}prom_alerts" in names
    assert f"k8s-ops{TOOLSEP}list_pods" not in names  # not promoted


def test_list_without_server_returns_every_tool() -> None:
    """`list` with no server is the "what can I do at all" call."""
    text, promoted = ts.handle_meta_call({"action": "list"}, _cat())
    assert "prometheus:" in text and "k8s-ops:" in text
    assert "prom_query:" in text and "list_pods:" in text
    assert promoted == set()  # listing does NOT promote


def test_list_with_server_narrows_to_it() -> None:
    text, _ = ts.handle_meta_call({"action": "list", "server": "prometheus"}, _cat())
    assert "prometheus (2 tools):" in text
    assert "prom_query:" in text and "list_pods" not in text


def test_describe_promotes_and_returns_schemas() -> None:
    text, promoted = ts.handle_meta_call({"action": "describe", "server": "prometheus"}, _cat())
    assert promoted == {"prometheus"}
    payload = json.loads(text)
    assert {p["tool"] for p in payload} == {"prom_query", "prom_alerts"}
    by_tool = {p["tool"]: p for p in payload}
    assert by_tool["prom_query"]["parameters"]["properties"]["query"]["type"] == "string"


def test_describe_can_be_narrowed_to_named_tools() -> None:
    text, promoted = ts.handle_meta_call(
        {"action": "describe", "server": "prometheus", "tools": ["prom_alerts"]}, _cat()
    )
    assert promoted == {"prometheus"}
    assert [p["tool"] for p in json.loads(text)] == ["prom_alerts"]


def test_errors_are_strings_never_raises() -> None:
    """This sits on the tool path: a bad call must be readable, not fatal."""
    assert ts.handle_meta_call({"action": "nope"}, _cat())[0].startswith("error:")
    assert ts.handle_meta_call({"action": "describe"}, _cat())[0].startswith("error:")
    assert "unknown server" in ts.handle_meta_call({"action": "list", "server": "x"}, _cat())[0]
    assert ts.handle_meta_call("not json", _cat())[0].startswith("error:")
    assert ts.handle_meta_call(None, _cat())[0].startswith("error:")
    assert ts.handle_meta_call({"action": "describe", "server": "k8s-ops", "tools": ["nope"]}, _cat())[0].startswith(
        "error:"
    )


def test_promotion_is_cumulative_across_calls() -> None:
    _t, p = ts.handle_meta_call({"action": "describe", "server": "prometheus"}, _cat())
    _t2, p2 = ts.handle_meta_call({"action": "describe", "server": "k8s-ops"}, _cat(), p)
    assert p2 == {"prometheus", "k8s-ops"}


def test_list_descriptions_are_capped() -> None:
    """The directory must stay cheap: a 400-char description is truncated."""
    text, _ = ts.handle_meta_call({"action": "list", "server": "prometheus"}, _cat())
    line = next(l for l in text.splitlines() if l.startswith("prom_query:"))
    assert len(line) < ts.LIST_DESCRIPTION_CHARS + 40
    assert "\u2026" in line


def test_server_directory_renders_descriptions_from_the_catalog() -> None:
    """The operator's ask: the prompt carries the server list WITH descriptions.

    The description rides on each catalog entry as `server_description`
    (stamped by _relay_catalog), so the surface renders a directory with no
    second lookup and no signature change.
    """
    cat = [
        {
            "server": "mm-rag",
            "tool": "search_dataset",
            "description": "Search",
            "server_description": "multimodal RAG: documents, images, audio",
        },
        {
            "server": "prometheus",
            "tool": "prom_query",
            "description": "Query",
            "server_description": "Prometheus metrics queries",
        },
    ]
    desc = ts.build_tools(cat, mode=ts.MODE_HYBRID)[0]["function"]["description"]
    assert "- mm-rag (1 tools): multimodal RAG: documents, images, audio" in desc
    assert "- prometheus (1 tools): Prometheus metrics queries" in desc
    # a server with no description still appears, just without the suffix
    bare = ts.build_tools([{"server": "x", "tool": "y", "description": "d"}], mode=ts.MODE_HYBRID)
    assert "- x (1 tools)" in bare[0]["function"]["description"]
