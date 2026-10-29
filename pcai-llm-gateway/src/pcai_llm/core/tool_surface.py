"""Two-level MCP tool surface for the relay (2026-10-28).

WHY THIS EXISTS (measured): inlining every granted MCP tool into the model
request cost 34,459 prompt tokens for a 111-tool / 10-server catalogue —
75,698 chars of DESCRIPTION prose (~18.9k tokens) and 35,380 chars of JSON
SCHEMA (~8.8k tokens), plus overhead. That is a floor on every relayed
request, and it is why a 236k-token conversation overflowed a 262k model
window and returned 400.

The fix is progressive disclosure, in the shape the operator asked for:
  * the prompt carries the SERVER DIRECTORY (name + operator description),
    so the model knows what exists without a round trip;
  * `list` returns every tool name + one-line summary (all servers, or one);
  * `describe` returns full parameter schemas for one server AND promotes it,
    so its real tools become directly callable from the next turn.

The model pays discovery once per relay turn and then has real tools for the
iterations that matter, instead of carrying all 111 definitions every time.

This module is PURE and I/O-free so the surface can be tested without a
relay, a registry, or a model. toolrelay owns the wiring and the loop.
"""

from __future__ import annotations

import json
from typing import Any

# The meta-tool the model calls to discover tools. Deliberately NOT shaped
# like "server__tool": toolrelay.parse_tool_name splits on TOOLSEP, and a name
# that parses would be mistaken for a granted MCP tool and routed to the
# federation instead of being handled locally.
META_TOOL = "pcai_tools"

# Surface modes. "full" is the pre-2026-10-28 behaviour byte-for-byte, so a
# deployment rolls back by config alone.
MODE_FULL = "full"
MODE_HYBRID = "hybrid"
MODES = (MODE_FULL, MODE_HYBRID)

# One-line summaries in the LIST output. The list is a directory, not the
# tool documentation: the full text arrives on DESCRIBE.
LIST_DESCRIPTION_CHARS = 120
# Server directory lines are tighter still — the directory rides EVERY
# request, so it must stay a directory and not become a description.
SERVER_DESCRIPTION_CHARS = 90


def _one_line(text: Any, cap: int = LIST_DESCRIPTION_CHARS) -> str:
    """First sentence-ish, single line, hard-capped. PURE."""
    s = " ".join(str(text or "").split())
    if len(s) <= cap:
        return s
    return s[: cap - 1].rstrip() + "\u2026"


def _catalog_index(catalog: list[dict]) -> dict[str, list[dict]]:
    """server -> [tool dicts], stable order. PURE."""
    out: dict[str, list[dict]] = {}
    for t in catalog or []:
        server = str(t.get("server") or "")
        if not server:
            continue
        out.setdefault(server, []).append(t)
    return out


def _tool_spec(t: dict, sep: str) -> dict:
    """One OpenAI tool definition from a catalog entry. PURE."""
    schema = t.get("inputSchema")
    return {
        "type": "function",
        "function": {
            "name": f"{t['server']}{sep}{t['tool']}",
            "description": t.get("description") or f"{t['server']}/{t['tool']}",
            "parameters": schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
        },
    }


def meta_tool_spec(catalog: list[dict], servers: dict[str, str] | None = None) -> dict:
    """The single discovery tool offered in hybrid mode. PURE.

    The server DIRECTORY rides inline: a bare list of names forces a round
    trip just to learn that mm-rag is for documents and prometheus is for
    metrics. ~10 servers x ~40 tokens is cheap against the 34k this replaces,
    and it is the difference between the model reaching for a tool and
    concluding it has none. Descriptions come from mcp_servers.description
    (operator-editable); a server without one still shows name + tool count.
    """
    index = _catalog_index(catalog)
    if servers is None:
        # Derive from the catalog itself: toolrelay's caller stamps
        # `server_description` on every entry, so no second lookup and no
        # signature change is needed to render the directory.
        servers = {}
        for t in catalog or []:
            name = str(t.get("server") or "")
            desc = str(t.get("server_description") or "")
            if name and desc and name not in servers:
                servers[name] = desc
    lines = []
    for name in sorted(index):
        desc = _one_line((servers or {}).get(name) or "", SERVER_DESCRIPTION_CHARS)
        suffix = ": " + desc if desc else ""
        lines.append(f"- {name} ({len(index[name])} tools){suffix}")
    directory = "\n".join(lines)
    return {
        "type": "function",
        "function": {
            "name": META_TOOL,
            "description": (
                "Discover the MCP tools you can call. The full definitions are NOT listed up"
                " front because they cost ~30k tokens; use this tool to find them.\n"
                "  list              -> every tool name + one-line summary, all servers\n"
                "  list <server>     -> that server's tools with one-line summaries\n"
                "  describe <server> -> FULL parameter schemas; those tools then become"
                " directly callable on your next turn\n"
                "Call this BEFORE concluding you lack a capability.\n"
                "Servers:\n" + directory
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "describe"]},
                    "server": {"type": "string", "description": "Server to list or describe"},
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "describe only: limit to these tool names (default: all on the server)",
                    },
                },
                "required": ["action"],
            },
        },
    }


def build_tools(
    catalog: list[dict],
    *,
    mode: str = MODE_FULL,
    promoted: set[str] | None = None,
    sep: str = "__",
    servers: dict[str, str] | None = None,
) -> list[dict]:
    """The tools array to offer the model for THIS iteration. PURE.

    full   -> every granted tool (pre-2026-10-28 behaviour, unchanged)
    hybrid -> the meta-tool (+ server directory), plus every tool of a
              PROMOTED server
    """
    if not catalog:
        return []
    if mode != MODE_HYBRID:
        return [_tool_spec(t, sep) for t in catalog]
    promoted = promoted or set()
    out = [meta_tool_spec(catalog, servers)]
    index = _catalog_index(catalog)
    for server in sorted(promoted):
        for t in index.get(server, []):
            out.append(_tool_spec(t, sep))
    return out


def _all_tools_text(index: dict[str, list[dict]]) -> str:
    """Every tool, one line each, grouped by server. PURE."""
    lines: list[str] = []
    for server in sorted(index):
        lines.append(f"{server}:")
        for t in sorted(index[server], key=lambda x: str(x.get("tool"))):
            lines.append(f"  {t['tool']}: {_one_line(t.get('description'))}")
    return "\n".join(lines)


def handle_meta_call(
    arguments: Any,
    catalog: list[dict],
    promoted: set[str] | None = None,
) -> tuple[str, set[str]]:
    """Execute one meta-tool call locally. PURE. Returns (result_text, promoted).

    NEVER raises: a malformed argument comes back as an error STRING the model
    can read and correct, because this sits on the tool path where an exception
    would abort a turn that is otherwise fine.
    """
    promoted = set(promoted or ())
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except (TypeError, ValueError):
            return "error: pcai_tools arguments must be a JSON object", promoted
    if not isinstance(arguments, dict):
        return "error: pcai_tools arguments must be a JSON object", promoted

    action = str(arguments.get("action") or "").strip().lower()
    index = _catalog_index(catalog)
    server = str(arguments.get("server") or "").strip()

    if action == "list":
        if not server:
            return _all_tools_text(index), promoted
        tools = index.get(server)
        if not tools:
            return f"error: unknown server {server!r}. Servers: {', '.join(sorted(index))}", promoted
        lines = [
            f"{t['tool']}: {_one_line(t.get('description'))}" for t in sorted(tools, key=lambda x: str(x.get("tool")))
        ]
        return f"{server} ({len(tools)} tools):\n" + "\n".join(lines), promoted

    if action == "describe":
        if not server:
            return "error: describe requires a server", promoted
        tools = index.get(server)
        if not tools:
            return f"error: unknown server {server!r}. Servers: {', '.join(sorted(index))}", promoted
        wanted = arguments.get("tools")
        if wanted is not None and not isinstance(wanted, list):
            # Silently widening to ALL tools would answer a different
            # question than the model asked (and spend the context it was
            # trying to save). Refuse so it can correct itself.
            return "error: tools must be an array of tool names", promoted
        if isinstance(wanted, list) and wanted:
            keep = {str(w) for w in wanted}
            tools = [t for t in tools if str(t.get("tool")) in keep]
            if not tools:
                return f"error: none of {sorted(keep)} exist on {server!r}", promoted
        payload = [
            {
                "tool": t.get("tool"),
                "description": t.get("description"),
                # Same fallback _tool_spec uses: a schema-less tool must
                # describe itself as an empty OBJECT, not null, or the model
                # cannot tell "no arguments" from "schema unavailable".
                "parameters": t.get("inputSchema")
                if isinstance(t.get("inputSchema"), dict)
                else {"type": "object", "properties": {}},
            }
            for t in sorted(tools, key=lambda x: str(x.get("tool")))
        ]
        # PROMOTION: the caller keeps this set, so the next iteration offers
        # these tools directly and the model can call them.
        promoted.add(server)
        return json.dumps(payload, default=str), promoted

    return f"error: unknown action {action!r} (expected 'list' or 'describe')", promoted
