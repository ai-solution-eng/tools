"""SQLhandler MCP server - direct SQL access to columnar data, backend-agnostic.

Instead of round-tripping rows through the EzPresto/PrestoDB JDBC bridge, the
server exposes each table as a pyarrow Dataset and executes SQL with DuckDB
over it, pushing predicates and column projection down to the Parquet/Delta scan.

Backends are pluggable via SQLHANDLER_BACKEND:
  * onelake (default) - Microsoft Fabric OneLake (Delta Lake over ABFS)
  * s3 / minio        - S3-compatible object storage (Parquet files)
  * iceberg           - Apache Iceberg through a REST/SQL catalog

Transport
---------
The server is built on the LOW-LEVEL MCP 2.0 Server (mcp.server.lowlevel.Server),
which speaks the interoperable streamable-http protocol including the standard
initialize handshake. Any MCP client (DSH assistants, the official Python/TS
SDKs, MCP Inspector, Codex, Claude Code, etc.) can connect. The higher-level
MCPServer shipped in mcp>=2.0.0 only implements the newer stateless
per-request protocol and rejects the initialize handshake, which blocks
standard tools; we deliberately avoid that class here.

Tools exposed:
  * list_tables        - enumerate the tables in the configured source
  * search_tables      - keyword search over names/columns/catalog descriptions
  * describe_table     - inspect columns/types of a table
  * profile_table      - column-level statistics (min/max, null %, distinct,
                         quantiles) so agents write correct filters first try
  * run_sql            - execute SQL via DuckDB (aggregations etc.); output
                         as markdown, JSON or CSV
  * scan_table         - pull rows via pyarrow with column projection + limit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading

import uvicorn
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

from . import __version__, mcp_resources, observability
from .config import (
    load_backend_config,
    load_cache_config,
    load_dotenv,
    load_source_providers,
)
from .engine import SqlEngine
from .provider import make_provider
from .webui import register_ui

logger = logging.getLogger("sqlhandler")

# --------------------------------------------------------------------------
# MCP server (standard MCP, interoperable initialize handshake)
# --------------------------------------------------------------------------


async def _handle_list_tools(ctx, params) -> ListToolsResult:
    """Return tools/list results (wired onto the low-level Server below)."""
    return ListToolsResult(tools=_TOOLS)


def _dispatch_tool(name: str, args: dict) -> tuple[str, bool]:
    """Run one tool synchronously; returns (text, is_error).

    Kept as a plain function so the async handler can offload it to a worker
    thread (asyncio.to_thread) and keep the event loop responsive for other
    sessions and health checks while a long scan is running.
    """
    try:
        if name == "list_tables":
            return list_tables(), False
        elif name == "describe_table":
            return describe_table(str(args.get("table", ""))), False
        elif name == "profile_table":
            cols = args.get("columns")
            col_list = (
                [c.strip() for c in str(cols).split(",") if c.strip()] if cols else None
            )
            return profile_table(str(args.get("table", "")), col_list), False
        elif name == "search_tables":
            return search_tables(str(args.get("query", ""))), False
        elif name == "run_sql":
            params = args.get("params")
            if params is not None and not isinstance(params, (dict, list)):
                return "Error running SQL: params must be an object or an array.", True
            return run_sql(
                str(args.get("sql", "")),
                args.get("limit"),
                args.get("output_format") or "markdown",
                params,
                args.get("version_as_of"),
            ), False
        elif name == "scan_table":
            # An explicit limit of 0 is honored (empty result); only a
            # missing limit defaults to 100.
            raw_limit = args.get("limit")
            limit = int(raw_limit) if raw_limit is not None else 100
            return scan_table(
                str(args.get("table", "")),
                args.get("columns"),
                limit,
                args.get("output_format") or "markdown",
                args.get("version_as_of"),
            ), False
        return f"Unknown tool: {name}", True
    except Exception as exc:
        return str(exc), True


async def _handle_call_tool(ctx, params: CallToolRequestParams) -> CallToolResult:
    """Serve tools/call, dispatching to the underlying tool functions."""
    text, is_error = await asyncio.to_thread(_dispatch_tool, params.name, params.arguments or {})
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        is_error=is_error,
    )


_TOOLS = [
    Tool(
        name="list_tables",
        description="List the tables available in the configured data source.",
        input_schema={"type": "object", "properties": {}},
    ),
    Tool(
        name="describe_table",
        description="Return column names, types, and the canonical URI for a table.",
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": 'Table name; use "schema/name" when the source uses schemas.',
                }
            },
            "required": ["table"],
        },
    ),
    Tool(
        name="profile_table",
        description=(
            "Column-level statistics for a table: min/max, approx distinct count, "
            "null %, avg/std and q25/q50/q75 quantiles, plus the total row count. "
            "Use it before writing filters/aggregations to pick the right columns, "
            "value ranges and predicates on the first try instead of guessing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": 'Table name; use "schema/name" when the source uses schemas.',
                },
                "columns": {
                    "type": "string",
                    "description": "Optional comma-separated subset of columns to profile (default: all).",
                },
            },
            "required": ["table"],
        },
    ),
    Tool(
        name="search_tables",
        description=(
            "Find tables by keyword: matches table names, columns and catalog "
            "descriptions. Use when the table list is long or you don't know "
            "which table holds what."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords to look for (e.g. 'work order amount')."},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="run_sql",
        description=(
            "Execute a SQL query against the source tables and return results. "
            "Tables are referenced by folder name (e.g. work_order_header, or schema/name). "
            "Aggregations, filters, and joins are pushed into the scan."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL SELECT to run against the source tables.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Optional max number of rows to return. Default: capped by "
                        "SQLHANDLER_MAX_ROWS (1000)."
                    ),
                },
                "output_format": {
                    "type": "string",
                    "enum": ["markdown", "json", "csv"],
                    "description": (
                        "Result rendering: markdown (default, human/LLM friendly), "
                        "json ({columns, rows} — compact, machine-parseable) or csv."
                    ),
                },
                "params": {
                    "description": (
                        "Optional bind parameters: an object for named $placeholders "
                        "(e.g. {\"status\": \"open\"}) or an array for positional ?. "
                        "Keeps reusable query templates injection-safe."
                    ),
                },
                "version_as_of": {
                    "type": "integer",
                    "description": (
                        "Optional historical snapshot for time travel: a Delta snapshot "
                        "version (nfs/onelake backends) or Iceberg snapshot id. Applies "
                        "to every versionable table the query touches."
                    ),
                },
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="scan_table",
        description=(
            "Fetch rows/columns from a table via pyarrow (columnar). Prefer run_sql "
            "when filters or aggregations can be pushed into the scan; use this to "
            "sample raw columns or feed a programmatic caller without writing SQL."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": 'Table name; use "schema/name" when the source uses schemas.',
                },
                "columns": {
                    "type": "string",
                    "description": "Optional comma-separated list of columns to project.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 100).",
                },
                "output_format": {
                    "type": "string",
                    "enum": ["markdown", "json", "csv"],
                    "description": "Result rendering (default markdown).",
                },
                "version_as_of": {
                    "type": "integer",
                    "description": (
                        "Optional historical snapshot for time travel: a Delta snapshot "
                        "version (nfs/onelake) or Iceberg snapshot id."
                    ),
                },
            },
            "required": ["table"],
        },
    ),
]

mcp = Server(
    "sqlhandler",
    title="SQLhandler MCP Server",
    # Single-sourced from sqlhandler.__version__ so the MCP handshake,
    # /api/status and pyproject.toml always agree (bump_version.sh updates
    # __init__.py; hardcoding a second number here is how 0.5.1/0.5.3/0.6.0
    # drifted apart).
    version=__version__,
    description=("Direct, fast SQL access to columnar data (OneLake/Delta, S3/MinIO/Parquet, Iceberg) as MCP tools."),
    instructions=(
        "Direct, fast access to columnar data (OneLake/Delta or S3/MinIO/Parquet) "
        "as an EzPresto replacement. Use list_tables to discover tables, describe_table "
        "for schema, profile_table for column statistics (value ranges, null %, distinct "
        "counts — helps write correct filters first try), and run_sql / scan_table to "
        "query. Prefers predicate filters and column projections to avoid full scans."
    ),
    on_list_tools=_handle_list_tools,
    on_call_tool=_handle_call_tool,
    # Resources + prompts (see mcp_resources.py): the other MCP primitives.
    # The handlers reuse the process-wide engine and its caches via _handler.
    on_list_resources=mcp_resources.handle_list_resources,
    on_list_resource_templates=mcp_resources.handle_list_resource_templates,
    on_read_resource=mcp_resources.handle_read_resource,
    on_list_prompts=mcp_resources.handle_list_prompts,
    on_get_prompt=mcp_resources.handle_get_prompt,
)

_transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)


async def _run_stdio(server: Server) -> None:
    """Serve over stdio until the client closes the pipe."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# --------------------------------------------------------------------------
# Process-wide engine + warm-up
# --------------------------------------------------------------------------

_handler_lock = threading.Lock()
_handler_singleton: SqlEngine | None = None


def _handler() -> SqlEngine:
    """Return the process-wide SqlEngine, building it on first use.

    The data source is selected by SQLHANDLER_BACKEND (onelake by default,
    or s3/minio) and its config is built from the environment. On first use
    the engine is created and, when pre-warm tables are configured, a daemon
    thread warms the describe cache in the background so the first agent
    describe is already a cache hit.
    """
    global _handler_singleton
    with _handler_lock:
        if _handler_singleton is None:
            load_dotenv()
            # Federated multi-source mode (SQLHANDLER_SOURCES) or single backend.
            provider = load_source_providers()
            if provider is None:
                _, config = load_backend_config()
                provider = make_provider(config)
            cache = load_cache_config()
            _handler_singleton = SqlEngine(
                provider,
                cache_ttl=cache.ttl_seconds,
                dataset_cache_ttl=cache.dataset_cache_ttl,
                dataset_cache_tables=cache.dataset_cache_tables,
                version_check_interval=cache.version_check_interval,
                list_async_refresh=cache.list_async_refresh,
            )
            # Prewarm: explicit SQLHANDLER_PREWARM_TABLES wins; otherwise the
            # busiest tables from the previous run (usage counts persisted in
            # the disk-warm cache) are warmed — the server teaches itself
            # what to prewarm instead of relying on a hand-maintained list.
            prewarm_tables = cache.prewarm_tables or _handler_singleton.usage_top_tables()
            if prewarm_tables:
                logger.info(
                    "prewarming schema cache for %d table(s)%s",
                    len(prewarm_tables),
                    "" if cache.prewarm_tables else " (usage-driven)",
                )
                threading.Thread(
                    target=_prewarm,
                    args=(_handler_singleton, prewarm_tables),
                    daemon=True,
                    name="sqlhandler-prewarm",
                ).start()
        return _handler_singleton


def _prewarm(handler: SqlEngine, tables: tuple[str, ...]) -> None:
    """Warm the schemas for the tables your queries hit most often."""
    try:
        outcomes = handler.prewarm(tables)
        failed = [t for t, o in outcomes.items() if o != "ok"]
        if failed:
            logger.warning("prewarm failed for: %s", ", ".join(failed))
        else:
            logger.info("prewarmed describe cache for %d table(s)", len(outcomes))
    except Exception:
        logger.exception("prewarm failed")


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def list_tables() -> str:
    """List the tables available in the configured data source."""
    try:
        handler = _handler()
        tables = handler.list_tables()
        if not tables:
            return "No tables found in the configured data source."
        lines = ["Tables:"]
        for t in tables:
            # Catalog descriptions annotate the list when present (compact:
            # name first, description after an em dash), so agents can pick
            # the right table without a describe round-trip per candidate.
            desc = handler.table_description(t)
            lines.append(f"  - {t.name}" + (f" — {desc}" if desc else ""))
        return "\n".join(lines)
    except Exception as exc:
        return f"Error listing tables: {exc}"


def describe_table(table: str) -> str:
    """Return column names, types, and the canonical URI for a table."""
    try:
        handler = _handler()
        info = handler.describe_table(table)
        lines = [f"Table: {info['table']}", f"URI: {info['uri']}"]
        if info.get("description"):
            lines.append(f"Description: {info['description']}")
        if info.get("aliases"):
            lines.append(f"Also known as: {', '.join(info['aliases'])}")
        lines.append("Columns:")
        for c in info["columns"]:
            line = f"  - {c['name']}: {c['type']}"
            if c.get("description"):
                line += f" — {c['description']}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as exc:
        return f"Error describing table: {exc}"


def profile_table(table: str, columns: list[str] | None = None) -> str:
    """Return per-column statistics for a table as a markdown table."""
    try:
        handler = _handler()
        p = handler.profile_table(table, columns=columns)
        header = [f"Table: {p['table']}"]
        if p.get("n_rows") is not None:
            header.append(
                f"Rows: {p['n_rows']}"
                + (
                    f" (profiled {p['profiled_rows']}, capped by SQLHANDLER_PROFILE_MAX_ROWS)"
                    if p.get("profile_max_rows", 0) > 0
                    and p.get("profiled_rows") == p.get("profile_max_rows")
                    else ""
                )
            )
        import pandas as pd

        df = pd.DataFrame(p["columns"])
        return "\n".join(header) + "\n\n" + df.to_markdown(index=False)
    except Exception as exc:
        return f"Error profiling table: {exc}"


def search_tables(query: str) -> str:
    """Keyword search over table names/columns/catalog descriptions."""
    try:
        handler = _handler()
        results = handler.search_tables(query)
        if not results:
            return f"No tables match {query!r}. Try broader keywords, or run list_tables."
        lines = [f"Found {len(results)} table(s) matching {query!r}:"]
        for r in results:
            line = f"  - {r['table']} ({r['format']})"
            if r["matched_columns"]:
                line += f" — columns: {', '.join(r['matched_columns'])}"
            lines.append(line)
            if r["description"]:
                lines.append(f"      {r['description']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error searching tables: {exc}"


def run_sql(
    sql: str,
    limit: int | None = None,
    output_format: str = "markdown",
    params: object | None = None,
    version_as_of: int | None = None,
) -> str:
    """Execute a SQL query against the source tables and return results.

    Args:
        sql: The SQL SELECT to run against the source tables.
        limit: Optional max rows to return; SQLHANDLER_MAX_ROWS (default 1000)
            caps the result either way.
        output_format: markdown (default) | json | csv.
        params: optional bind parameters (named dict or positional list).
    """
    try:
        handler = _handler()
        arrow = handler.query_duckdb(sql, limit=limit, params=params, version_as_of=version_as_of)
        return _arrow_to_output(arrow, max_rows=limit, fmt=output_format)
    except Exception as exc:
        return f"Error running SQL: {exc}"


def scan_table(
    table: str,
    columns: str | None = None,
    limit: int = 100,
    output_format: str = "markdown",
    version_as_of: int | None = None,
) -> str:
    """Fetch rows/columns from a table via pyarrow (columnar).

    Prefer run_sql when filters or aggregations can be pushed into the scan;
    use this to sample raw columns or feed a programmatic caller without
    writing SQL.

    Args:
        table: Table name (schema/name when the source uses schemas).
        columns: Optional comma-separated list of columns to project.
        limit: Max rows to return (default 100).
        output_format: markdown (default) | json | csv.
    """
    try:
        handler = _handler()
        col_list = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
        arrow = handler.scan_arrow(table, columns=col_list, limit=int(limit), version_as_of=version_as_of)
        return _arrow_to_output(arrow, max_rows=int(limit), fmt=output_format)
    except Exception as exc:
        return f"Error scanning table: {exc}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# Hard cap on markdown rows returned to a client, regardless of the requested
# limit (SQLHANDLER_MAX_OUTPUT_ROWS, default 1000). Guards against a client
# asking for an unbounded result set producing a huge payload / OOM.
_MAX_OUTPUT_ROWS = 1000


def _arrow_to_markdown(arrow, max_rows: int | None = 100) -> str:
    """Render a pyarrow Table as a compact markdown table for an LLM."""
    try:
        raw = os.environ.get("SQLHANDLER_MAX_OUTPUT_ROWS", str(_MAX_OUTPUT_ROWS))
        try:
            cap = int(raw)
        except ValueError:
            cap = _MAX_OUTPUT_ROWS  # garbage env value: fall back to the default cap
        cap = max(cap, 0)
        if cap > 0 and max_rows is not None:
            max_rows = min(max_rows, cap)
        if cap > 0 and arrow.num_rows > cap:
            arrow = arrow.slice(0, cap)
        df = arrow.to_pandas()
        if max_rows is not None and len(df) > max_rows:
            df = df.head(max_rows)
        return df.to_markdown(index=False)
    except Exception:
        return str(arrow)


def _arrow_to_output(arrow, max_rows: int | None, fmt: str) -> str:
    """Render a pyarrow Table as markdown (default), JSON, or CSV.

    All formats share the same row cap (SQLHANDLER_MAX_OUTPUT_ROWS) so a
    machine-readable format can't smuggle an unbounded payload either. JSON
    reuses the web API's payload shape ({columns, rows, n_rows, truncated});
    CSV is pandas' RFC-style rendering (header row, no index).
    """
    import csv
    import io

    from .webui import arrow_to_payload

    raw = os.environ.get("SQLHANDLER_MAX_OUTPUT_ROWS", str(_MAX_OUTPUT_ROWS))
    try:
        cap = int(raw)
    except ValueError:
        cap = _MAX_OUTPUT_ROWS
    cap = max(cap, 0)
    if cap > 0 and max_rows is not None:
        max_rows = min(max_rows, cap)
    if cap > 0 and arrow.num_rows > cap:
        arrow = arrow.slice(0, cap)

    fmt = (fmt or "markdown").strip().lower()
    if fmt == "json":
        return json.dumps(arrow_to_payload(arrow, limit=max_rows), default=str)
    if fmt == "csv":
        payload = arrow_to_payload(arrow, limit=max_rows)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(payload["columns"])
        writer.writerows(payload["rows"])
        return buf.getvalue()
    return _arrow_to_markdown(arrow, max_rows=max_rows)


class _ApiTokenMiddleware:
    """ASGI middleware: require a shared token on every /api/* request.

    Comparison is constant-time (hmac.compare_digest). The token protects
    the JSON API on deployments without gateway auth; the MCP endpoint and
    the UI stay governed by their own layers.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith("/api"):
            provided = ""
            for k, v in scope.get("headers", []):
                if k == b"authorization":
                    provided = v.decode("latin-1")
                    break
                if k == b"x-api-token":
                    provided = v.decode("latin-1")
                    break
            if not observability.token_matches(provided, self.token):
                resp = JSONResponse({"error": "Unauthorized: missing or invalid API token."}, status_code=401)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main(argv: list | None = None) -> None:
    # Load .env FIRST so every setting -- including SQLHANDLER_TRANSPORT,
    # which is read by argparse below -- can come from the env file. The
    # later load_dotenv() inside _handler() stays for programmatic callers
    # and is a no-op once variables are set (existing env always wins).
    load_dotenv()
    parser = argparse.ArgumentParser(description="SQLhandler MCP server")
    parser.add_argument(
        "--transport",
        default=os.environ.get("SQLHANDLER_TRANSPORT", "stdio"),
        choices=["stdio", "streamable-http"],
        help="MCP transport (stdio or streamable-http).",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=9097, type=int)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.transport == "stdio":
        asyncio.run(_run_stdio(mcp))
        return

    # Streamable HTTP with the standard initialize handshake AND stateless
    # per-request handling (stateless_http=True). The low-level Server serves
    # the standard handshake so any MCP client can connect, while each request
    # stays self-contained so the deployment can scale to multiple replicas
    # (no in-memory per-pod session to get "Session not found").
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security,
    )

    # Read-only web explorer + JSON API (list/describe/query/preview) over
    # the same process-wide engine. Served at / and /ui; API under /api.
    register_ui(app, _handler)

    # Liveness/readiness routes for the k8s probes.
    async def _health(_request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def _ready(_request) -> JSONResponse:
        # Backend-aware readiness: only report "ready" when the configured data
        # source is actually reachable (OneLake DFS token+list, S3 list,
        # Iceberg catalog, or NFS root). If the credential/endpoint breaks, the
        # pod drops out of the Service so traffic stops reaching a dead backend
        # and the failure becomes visible. Disable with SQLHANDLER_READINESS_CHECK=0.
        if os.environ.get("SQLHANDLER_READINESS_CHECK", "1").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return JSONResponse({"status": "ready"})
        try:
            engine = await asyncio.wait_for(asyncio.to_thread(_handler), timeout=2)
            err = await asyncio.wait_for(asyncio.to_thread(engine.provider.check_connection), timeout=15)
        except TimeoutError:
            return JSONResponse({"status": "not ready", "error": "backend check timed out"}, status_code=503)
        except Exception as exc:
            return JSONResponse({"status": "not ready", "error": str(exc)}, status_code=503)
        if err:
            return JSONResponse({"status": "not ready", "error": err}, status_code=503)
        return JSONResponse({"status": "ready"})

    app.add_route("/health", _health)
    app.add_route("/ready", _ready)

    # Prometheus scrape endpoint (Prometheus text exposition; engine gauges
    # included via the same process-wide engine — list_tables is cached so
    # scraping is cheap).
    async def _metrics(_request) -> Response:
        try:
            engine = await asyncio.to_thread(_handler)
        except Exception:
            engine = None
        return Response(
            content=observability.metrics.render(engine),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    app.add_route("/metrics", _metrics)

    # When SQLHANDLER_API_TOKEN is set, every /api/* call must present it
    # (Authorization: Bearer <token> or X-API-Token: <token>) — for machine
    # callers of the JSON API on deployments that are NOT behind the PCAI
    # oauth2-proxy gateway. /mcp, /ui and /health|/ready are unaffected.
    api_token = os.environ.get("SQLHANDLER_API_TOKEN", "").strip()
    if api_token:
        app.add_middleware(_ApiTokenMiddleware, token=api_token)
    # Browser CORS: any origin by default (the API is read-only and typically
    # sits behind the PCAI gateway). Restrict in production with e.g.
    # SQLHANDLER_CORS_ORIGINS="https://ui.example.com,https://other.example.com".
    origins_raw = os.environ.get("SQLHANDLER_CORS_ORIGINS", "*").strip()
    allow_origins = [o.strip() for o in origins_raw.split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
