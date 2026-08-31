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
  * describe_table     - inspect columns/types of a table
  * run_sql            - execute SQL via DuckDB (aggregations etc.)
  * scan_table         - pull rows via pyarrow with column projection + limit
"""

from __future__ import annotations

import argparse
import asyncio
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
from starlette.responses import JSONResponse

from . import __version__
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
        elif name == "run_sql":
            return run_sql(str(args.get("sql", "")), args.get("limit")), False
        elif name == "scan_table":
            # An explicit limit of 0 is honored (empty result); only a
            # missing limit defaults to 100.
            raw_limit = args.get("limit")
            limit = int(raw_limit) if raw_limit is not None else 100
            return scan_table(
                str(args.get("table", "")),
                args.get("columns"),
                limit,
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
        name="run_sql",
        description=(
            "Execute a SQL query against the source tables and return results as a table. "
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
            },
            "required": ["sql"],
        },
    ),
    Tool(
        name="scan_table",
        description="Fetch rows/columns from a table via pyarrow (columnar).",
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
        "for schema, and run_sql / scan_table to query. Prefers predicate filters and "
        "column projections to avoid full scans."
    ),
    on_list_tools=_handle_list_tools,
    on_call_tool=_handle_call_tool,
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
            if cache.prewarm_tables:
                logger.info("prewarming schema cache for %d table(s)", len(cache.prewarm_tables))
                threading.Thread(
                    target=_prewarm,
                    args=(_handler_singleton, cache.prewarm_tables),
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
        return "Tables:\n" + "\n".join(f"  - {t.name}" for t in tables)
    except Exception as exc:
        return f"Error listing tables: {exc}"


def describe_table(table: str) -> str:
    """Return column names, types, and the canonical URI for a table."""
    try:
        handler = _handler()
        info = handler.describe_table(table)
        lines = [f"Table: {info['table']}", f"URI: {info['uri']}"]
        lines.append("Columns:")
        for c in info["columns"]:
            lines.append(f"  - {c['name']}: {c['type']}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error describing table: {exc}"


def run_sql(sql: str, limit: int | None = None) -> str:
    """Execute a SQL query against the source tables and return results as a table.

    Args:
        sql: The SQL SELECT to run against the source tables.
        limit: Optional max rows to return; SQLHANDLER_MAX_ROWS (default 1000)
            caps the result either way.
    """
    try:
        handler = _handler()
        arrow = handler.query_duckdb(sql, limit=limit)
        return _arrow_to_markdown(arrow, max_rows=limit)
    except Exception as exc:
        return f"Error running SQL: {exc}"


def scan_table(table: str, columns: str | None = None, limit: int = 100) -> str:
    """Fetch rows/columns from a table via pyarrow (columnar).

    Args:
        table: Table name (schema/name when the source uses schemas).
        columns: Optional comma-separated list of columns to project.
        limit: Max rows to return (default 100).
    """
    try:
        handler = _handler()
        col_list = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
        arrow = handler.scan_arrow(table, columns=col_list, limit=int(limit))
        return _arrow_to_markdown(arrow, max_rows=int(limit))
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
