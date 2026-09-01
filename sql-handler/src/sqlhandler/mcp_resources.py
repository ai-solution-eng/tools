"""MCP resources + prompts for the SQLhandler server.

Tools are the primary surface (list_tables / describe_table / profile_table /
run_sql / scan_table); this module adds the OTHER two MCP primitives on the
same low-level Server:

- **Resources** — read-only context a host can pull without a tool call:
    * ``sqlhandler://catalog``              — every table + its semantic-catalog
                                              description (the "data dictionary")
    * ``sqlhandler://table/<name>/schema``  — one table's schema + column docs
    * ``sqlhandler://query-memory``         — recent successful queries recorded
                                              by the engine, so later agent
                                              sessions reuse proven SQL patterns
- **Resource templates** — the same table-schema resource as a URI template for
  programmatic clients: ``sqlhandler://table/{table}/schema``

- **Prompts** — reusable workflows the host can instantiate:
    * ``explore-data``   — guided discovery: list → describe → profile → SQL
    * ``analyze-table``  — deep-dive one table (profile-first workflow)

Everything here reuses the process-wide :class:`~sqlhandler.engine.SqlEngine`
(and its caches) that backs the MCP tools and the web UI, and degrades
gracefully: a resource error surfaces as an MCP error for that single read,
never a server failure.
"""

from __future__ import annotations

import asyncio
import urllib.parse

from mcp_types import (
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextContent,
    TextResourceContents,
)

from .engine import SqlEngine

_SCHEME = "sqlhandler://"
_CATALOG_URI = f"{_SCHEME}catalog"
_QUERY_MEMORY_URI = f"{_SCHEME}query-memory"
_TABLE_PREFIX = f"{_SCHEME}table/"
_SCHEMA_SUFFIX = "/schema"
_TABLE_TEMPLATE = f"{_SCHEME}table/{{table}}/schema"

_MARKDOWN = "text/markdown"


# ---------------------------------------------------------------------------
# rendering (plain sync functions — testable without an event loop)
# ---------------------------------------------------------------------------


def _schema_text(engine: SqlEngine, table: str) -> str:
    """Markdown schema (+ catalog docs) for one table."""
    d = engine.describe_table(table)
    lines = [f"# Table: {d['table']}", "", f"URI: `{d['uri']}`", ""]
    if d.get("description"):
        lines += [d["description"], ""]
    if d.get("aliases"):
        lines += [f"Also known as: {', '.join(d['aliases'])}", ""]
    lines += ["| column | type | description |", "|---|---|---|"]
    for c in d["columns"]:
        lines.append(f"| {c['name']} | {c['type']} | {c.get('description', '')} |")
    return "\n".join(lines)


def _catalog_text(engine: SqlEngine) -> str:
    """Markdown overview of every table + its semantic-catalog description."""
    tables = engine.list_tables()
    lines = ["# Table catalog", ""]
    if not tables:
        lines.append("No tables found in the configured data source.")
        return "\n".join(lines)
    described = False
    for t in tables:
        desc = engine.table_description(t)
        described = described or bool(desc)
        lines.append(f"- **{t.name}** ({t.format})" + (f" — {desc}" if desc else ""))
    if not described:
        lines += [
            "",
            (
                "No semantic-catalog descriptions are configured. Set SQLHANDLER_CATALOG "
                "to a JSON file (see README) to document tables/columns for agents."
            ),
        ]
    return "\n".join(lines)


def _query_memory_text(engine: SqlEngine) -> str:
    """Markdown of recent query outcomes (the agent's own query history)."""
    memory = engine.query_memory()
    lines = [
        "# Query memory",
        "",
        (
            "Recent successful queries run against this server (newest first). "
            "Reuse and adapt these proven patterns instead of starting from scratch."
        ),
        "",
    ]
    if not memory:
        lines.append("No queries recorded yet.")
        return "\n".join(lines)
    lines += ["| # | sql | ms | rows |", "|---|---|---|---|"]
    for i, q in enumerate(reversed(memory), 1):
        sql = q.get("sql", "").replace("\n", " ").replace("|", "\\|")
        if len(sql) > 200:
            sql = sql[:197] + "..."
        lines.append(f"| {i} | `{sql}` | {q.get('duration_ms')} | {q.get('n_rows')} |")
    return "\n".join(lines)


def _parse_table_uri(uri: str) -> str | None:
    """Table name from a ``sqlhandler://table/<name>/schema`` URI, or None.

    The table name is percent-encoded (schema/name paths contain slashes);
    anything that does not match the exact shape returns None so callers can
    fall through to their own error handling.
    """
    if not uri.startswith(_TABLE_PREFIX) or not uri.endswith(_SCHEMA_SUFFIX):
        return None
    name = uri[len(_TABLE_PREFIX) : -len(_SCHEMA_SUFFIX)]
    return urllib.parse.unquote(name) if name else None


# ---------------------------------------------------------------------------
# async handlers (wired onto the low-level Server; engine calls off-loop)
# ---------------------------------------------------------------------------


async def handle_list_resources(ctx, params) -> ListResourcesResult:
    """resources/list: static per-table schemas + the catalog + query memory."""
    from .server import _handler  # local import: avoids a server<->module cycle

    def _build():
        engine = _handler()
        resources: list[Resource] = [
            Resource(
                uri=_CATALOG_URI,
                name="Table catalog",
                description="All tables with their semantic-catalog descriptions (the data dictionary).",
                mime_type=_MARKDOWN,
            ),
            Resource(
                uri=_QUERY_MEMORY_URI,
                name="Query memory",
                description="Recent successful queries — reuse proven SQL patterns across sessions.",
                mime_type=_MARKDOWN,
            ),
        ]
        for t in engine.list_tables():
            desc = engine.table_description(t) or None
            resources.append(
                Resource(
                    uri=f"{_TABLE_PREFIX}{urllib.parse.quote(t.path, safe='')}{_SCHEMA_SUFFIX}",
                    name=f"Schema: {t.path}",
                    description=desc,
                    mime_type=_MARKDOWN,
                )
            )
        return resources

    return ListResourcesResult(resources=await asyncio.to_thread(_build))


async def handle_list_resource_templates(ctx, params) -> ListResourceTemplatesResult:
    return ListResourceTemplatesResult(
        resource_templates=[
            ResourceTemplate(
                uri_template=_TABLE_TEMPLATE,
                name="Table schema",
                description=(
                    "Schema (+ semantic-catalog column docs) for one table; the table "
                    "name is percent-encoded, e.g. sqlhandler://table/workorder%2Fwork_order/schema"
                ),
                mime_type=_MARKDOWN,
            )
        ]
    )


async def handle_read_resource(ctx, params) -> ReadResourceResult:
    """resources/read for the sqlhandler:// scheme."""
    from .server import _handler

    uri = str(params.uri)
    table = _parse_table_uri(uri)

    def _render() -> str:
        engine = _handler()
        if table is not None:
            return _schema_text(engine, table)
        if uri == _CATALOG_URI:
            return _catalog_text(engine)
        if uri == _QUERY_MEMORY_URI:
            return _query_memory_text(engine)
        raise LookupError(f"Unknown resource: {uri}")

    try:
        text = await asyncio.to_thread(_render)
    except Exception as exc:
        # Map to an MCP application error for THIS read only. MCPError is what
        # the SDK turns into a JSON-RPC application error (not a transport one).
        from mcp.shared.exceptions import MCPError
        from mcp_types import INTERNAL_ERROR, INVALID_PARAMS

        code = INVALID_PARAMS if isinstance(exc, LookupError) else INTERNAL_ERROR
        raise MCPError(code=code, message=str(exc)) from exc
    return ReadResourceResult(
        contents=[TextResourceContents(uri=uri, mime_type=_MARKDOWN, text=text)]
    )


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

_PROMPTS = [
    Prompt(
        name="explore-data",
        description="Guided discovery of the data source: list tables, read the catalog, then profile before writing SQL.",
        arguments=[
            PromptArgument(
                name="goal",
                description="What you want to learn from the data (free text)",
                required=False,
            )
        ],
    ),
    Prompt(
        name="analyze-table",
        description="Deep-dive one table: schema, profile statistics, then targeted queries.",
        arguments=[
            PromptArgument(name="table", description="Table name (schema/name when the source uses schemas)", required=True)
        ],
    ),
]


async def handle_list_prompts(ctx, params) -> ListPromptsResult:
    return ListPromptsResult(prompts=_PROMPTS)


async def handle_get_prompt(ctx, params) -> GetPromptResult:
    from mcp.shared.exceptions import MCPError
    from mcp_types import INVALID_PARAMS

    def _bad(msg: str) -> MCPError:
        return MCPError(code=INVALID_PARAMS, message=msg)

    name = params.name
    args = params.arguments or {}
    if name == "explore-data":
        goal = str(args.get("goal", "")).strip()
        text = (
            "You are working with a SQLhandler MCP server (fast read-only SQL over "
            "columnar lake data). Goal: "
            + (goal or "explore what data is available and summarize what you find")
            + ".\n\nWorkflow:\n"
            "1. Read the sqlhandler://catalog resource (or call list_tables) to see the "
            "tables and their business descriptions.\n"
            "2. Pick the relevant tables; call describe_table for their schemas.\n"
            "3. Call profile_table on the columns you intend to filter or aggregate — "
            "use min/max, distinct counts and null % to pick correct predicates.\n"
            "4. Write one focused run_sql query (prefer qualified table names, filter "
            "early, limit the output). Check sqlhandler://query-memory for proven "
            "query patterns first.\n"
            "5. Present the result with a short interpretation, not just the raw rows."
        )
    elif name == "analyze-table":
        table = str(args.get("table", "")).strip()
        if not table:
            raise _bad("analyze-table requires a 'table' argument")
        text = (
            f"Analyze the table '{table}' on this SQLhandler MCP server.\n\n"
            "Workflow:\n"
            f"1. describe_table('{table}') for the schema (column docs included when a "
            "semantic catalog is configured).\n"
            f"2. profile_table('{table}') — note value ranges, distinct counts and null "
            "percentages before writing any SQL.\n"
            f"3. Ask and answer at least three interesting questions about '{table}' with "
            "run_sql (filters, group-bys, trends). Reference sqlhandler://query-memory "
            "for reusable patterns.\n"
            "4. Summarize findings: what the table contains, data quality observations "
            "(nulls, skew, outliers), and 2-3 follow-up analyses worth running."
        )
    else:
        raise _bad(f"Unknown prompt: {name}")
    return GetPromptResult(messages=[PromptMessage(role="user", content=TextContent(type="text", text=text))])
