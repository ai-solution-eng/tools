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

`list` is keyed by SERVER, so "which tool deletes a dataset?" used to cost a
whole server's catalogue in context. `search` (2026-11-23) ranks the same
in-memory catalogue by relevance instead -- BM25 over server name, tool name
and description -- so a verb the model knows but a server it does not costs a
handful of candidate lines. It is additive: `list`/`describe` are unchanged.

Every function here is PURE and I/O-free so the surface can be tested without
a relay, a registry, or a model. The ONE exception is a lazy settings read
inside `search` (`tool_search_max_results`); a caller that passes an explicit
cap -- every test -- keeps the call pure. toolrelay owns the wiring and loop.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
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

# --- query-granular search (2026-11-23) -----------------------------------
# BM25 constants are the textbook defaults: k1=1.5 saturates term frequency
# (the 2nd occurrence of a word in a 40-token description must not double the
# score) and b=0.75 normalises by document length. With ~100 documents of
# 20-60 tokens the tuning space is not worth a knob.
SEARCH_K1 = 1.5
SEARCH_B = 0.75
# A query word appearing in a tool or server NAME is the strongest signal
# available -- `delete_dataset` answers "delete dataset" outright -- and term
# frequency is what BM25 reads, so name tokens are emitted more than once.
SEARCH_TOOL_NAME_WEIGHT = 3
SEARCH_SERVER_NAME_WEIGHT = 2
# Mirrors settings.tool_search_max_results; used only when a caller passes no
# explicit cap (tests) or the setting is somehow absent.
DEFAULT_SEARCH_MAX_RESULTS = 10


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
                "  search <query>    -> the most relevant tools across ALL servers, ranked;"
                " then describe the server it names\n"
                "  describe <server> -> FULL parameter schemas; those tools then become"
                " directly callable on your next turn\n"
                "Call this BEFORE concluding you lack a capability.\n"
                "Servers:\n" + directory
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "search", "describe"]},
                    "server": {"type": "string", "description": "Server to list, search or describe"},
                    "query": {
                        "type": "string",
                        "description": "search only: what you want to do, e.g. 'delete dataset'",
                    },
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


def _search_tokens(text: Any) -> list[str]:
    """Lower-cased word tokens: camel/snake/kebab aware, plural-folded. PURE.

    The fold is deliberately crude -- one trailing "s" -- because it only has
    to be applied CONSISTENTLY on both sides. A query for "dataset" must reach
    a tool called `search_datasets`, and it does not matter that "status"
    folds to "statu" as long as the document folds the same way. Anything
    cleverer (Porter, a stemmer dependency) is not worth the risk here: a
    wrong stem silently ranks the right tool out of the list.
    """
    out: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", str(text or "")):
        if not chunk:
            continue
        # snake_case/kebab-case already split above; camelCase is not.
        for part in re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", chunk).split():
            tok = part.lower()
            if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
                tok = tok[:-1]
            out.append(tok)
    return out


def _search_corpus(index: dict[str, list[dict]]) -> tuple[list[dict], list[list[str]]]:
    """(entries, token lists) in a STABLE order. PURE.

    Stable because a tie must not depend on dict iteration: the same catalog
    and query have to rank identically on every pod and every run, or the
    gate output is not reproducible.
    """
    entries: list[dict] = []
    docs: list[list[str]] = []
    for server in sorted(index):
        for t in sorted(index[server], key=lambda x: str(x.get("tool"))):
            entries.append(t)
            docs.append(
                _search_tokens(server) * SEARCH_SERVER_NAME_WEIGHT
                + _search_tokens(t.get("tool")) * SEARCH_TOOL_NAME_WEIGHT
                + _search_tokens(t.get("description"))
            )
    return entries, docs


def _bm25_scores(docs: list[list[str]], query_terms: list[str]) -> list[float]:
    """Standard BM25 over `docs` for `query_terms`. PURE."""
    if not docs:
        return []
    total = len(docs)
    counts = [Counter(d) for d in docs]
    avgdl = sum(len(d) for d in docs) / total or 1.0
    scores = [0.0] * total
    for term in set(query_terms):
        df = sum(1 for c in counts if term in c)
        if not df:
            continue
        # log(1 + ...) keeps idf POSITIVE even for a term in every document,
        # so a common word can never push a relevant tool DOWN the list.
        idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
        for i, c in enumerate(counts):
            tf = c.get(term, 0)
            if not tf:
                continue
            denom = tf + SEARCH_K1 * (1.0 - SEARCH_B + SEARCH_B * len(docs[i]) / avgdl)
            scores[i] += idf * (tf * (SEARCH_K1 + 1.0)) / denom
    return scores


def rank_tools(
    catalog: list[dict],
    query: str,
    *,
    limit: int,
    server: str = "",
) -> list[dict]:
    """The best `limit` catalog entries for `query`, most relevant first. PURE.

    `server` narrows the corpus to one server (the model already knows where
    the tool lives and wants the exact name). An unknown server yields no
    hits -- the caller reports the unknown server itself, with the list, so
    the model can correct the spelling.

    Only positive scores are returned: a query word that appears nowhere must
    come back as "no matches", never as an arbitrary tail of the catalogue.
    """
    index = _catalog_index(catalog)
    if server:
        index = {server: index[server]} if server in index else {}
    entries, docs = _search_corpus(index)
    terms = _search_tokens(query)
    if not entries or not terms:
        return []
    scores = _bm25_scores(docs, terms)
    ranked = sorted(
        ((s, e) for s, e in zip(scores, entries) if s > 0.0),
        key=lambda pair: (-pair[0], str(pair[1].get("server")), str(pair[1].get("tool"))),
    )
    return [entry for _score, entry in ranked[: max(0, int(limit))]]


def _resolve_max_results(explicit: int | None) -> int:
    """The search cap: the caller's value, else settings.tool_search_max_results.

    Lazy import on purpose. This module stays import-light, and the setting is
    read ONLY when a `search` call actually happens -- `list`/`describe` never
    touch config, so their behaviour cannot change with a setting.
    """
    if explicit is not None:
        return int(explicit)
    from ..config import settings

    return int(getattr(settings, "tool_search_max_results", DEFAULT_SEARCH_MAX_RESULTS))


def handle_meta_call(
    arguments: Any,
    catalog: list[dict],
    promoted: set[str] | None = None,
    *,
    max_results: int | None = None,
) -> tuple[str, set[str]]:
    """Execute one meta-tool call locally. PURE. Returns (result_text, promoted).

    NEVER raises: a malformed argument comes back as an error STRING the model
    can read and correct, because this sits on the tool path where an exception
    would abort a turn that is otherwise fine.

    `max_results` caps `search` and is resolved from
    settings.tool_search_max_results when omitted, so the relay call site needs
    no new argument; tests pass it explicitly and stay config-free.
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

    if action == "search":
        cap = _resolve_max_results(max_results)
        if cap <= 0:
            # An EMPTY result reads as "no such tool" and sends the model
            # hunting for a capability it has; say why, and what to do instead.
            disabled = (
                "error: pcai_tools search is disabled"
                f" (tool_search_max_results={cap}); use action 'list' with a server,"
                " or 'describe' for full schemas"
            )
            return disabled, promoted
        raw_query = arguments.get("query")
        if raw_query is not None and not isinstance(raw_query, str):
            # Stringifying a list/dict would search for the repr and answer a
            # question the model did not ask. Refuse so it can correct itself.
            return "error: query must be a string", promoted
        query = str(raw_query or "").strip()
        if not query:
            empty = 'error: search requires a non-empty query (e.g. {"action":"search","query":"delete dataset"})'
            return empty, promoted
        if server and server not in index:
            return f"error: unknown server {server!r}. Servers: {', '.join(sorted(index))}", promoted
        hits = rank_tools(catalog, query, limit=cap, server=server)
        searched = len(index[server]) if server else sum(len(v) for v in index.values())
        if not hits:
            nothing = (
                f'no tools matched "{query}" ({searched} tool(s) searched).'
                " Try different words, or action 'list' to browse the catalogue."
            )
            return nothing, promoted
        lines = [f"- {t.get('server')}/{t.get('tool')}: {_one_line(t.get('description'))}" for t in hits]
        # The server is named on every line AND in the next step: the model
        # cannot `describe` a hit it cannot address, and describe is what
        # promotes the server so the real tool becomes callable.
        return (
            "\n".join(
                [f'search "{query}": {len(hits)} of {searched} tool(s), most relevant first', *lines]
                + [f"next: describe {hits[0].get('server')} for full parameter schemas"]
            ),
            promoted,
        )

    return f"error: unknown action {action!r} (expected 'list', 'search' or 'describe')", promoted
