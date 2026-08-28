"""Read-only web UI + JSON API for the SQLhandler MCP server.

Serves a self-contained HTML explorer (``index.html``) and a small JSON API
that reuses the SAME process-wide ``SqlEngine`` (and its caches) that backs
the MCP tools. The API is deliberately **read-only**: only SELECT / WITH /
EXPLAIN / SHOW / PRAGMA / VALUES statements are accepted, so the UI is a
safe human front-end for the same data the MCP agents query.

Endpoints (all JSON unless noted):

  GET  /api/status    -> {"status": "ok", "version", "backend"}
  GET  /api/tables    -> {"tables": [{"name", "path", "format"}]}
  POST /api/describe  -> {"table", "uri", "columns": [{"name", "type"}]}
  POST /api/query     -> {"columns": [...], "rows": [[...]], "n_rows", "duration_ms"}
  POST /api/preview   -> {"columns": [...], "rows": [[...]], "n_rows", "duration_ms"}

The HTML page is served at ``/`` and ``/ui``.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse

from .engine import SqlEngine

# Statements a read-only explorer may run. Anything else (INSERT/UPDATE/DELETE/
# CREATE/DROP/ALTER/MERGE/GRANT/...) is rejected before it reaches DuckDB.
_ALLOWED_SQL_PREFIXES = ("SELECT", "WITH", "EXPLAIN", "SHOW", "PRAGMA", "VALUES")

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000  # aligned with the server-side SQLHANDLER_MAX_ROWS cap

_HTML = (Path(__file__).parent / "ui" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# read-only guard
# ---------------------------------------------------------------------------


def assert_readonly(sql: str) -> str:
    """Return the trimmed SQL if it is a read-only statement, else raise.

    Splits on ``;`` (top-level) and rejects the query if ANY statement is not
    in the allowed read-only prefixes.
    """
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    if not statements:
        raise ValueError("Empty SQL statement.")
    for stmt in statements:
        head = stmt.lstrip(" \t\r\n(")
        first = head.split(None, 1)[0].rstrip(")") if head else ""
        if first.upper() not in _ALLOWED_SQL_PREFIXES:
            raise ValueError(
                f"Read-only UI: statement starting with '{first or '<empty>'}' is not allowed. "
                "Only SELECT / WITH / EXPLAIN / SHOW / PRAGMA / VALUES queries are permitted."
            )
    return statements[0] if len(statements) == 1 else sql


# ---------------------------------------------------------------------------
# JSON-safe row conversion
# ---------------------------------------------------------------------------


def _json_safe(value):
    """Convert one Arrow/pandas scalar into a JSON-serialisable value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        # NaN / Inf are not valid JSON.
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    # numpy / pandas scalars expose .item()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _json_safe(item())
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return repr(value)


def arrow_to_payload(arrow, limit: int | None = None) -> dict:
    """Convert a pyarrow Table into a JSON payload dict (columns + rows).

    ``n_rows`` is the number of rows in the payload (after any slicing);
    ``truncated`` is True when a positive ``limit`` was requested and the
    result was capped at it (more rows may exist upstream).
    """
    columns = [f.name for f in arrow.schema] if arrow is not None else []
    if arrow is None or arrow.num_rows == 0:
        return {"columns": columns, "rows": [], "n_rows": 0, "truncated": False}
    rows = arrow.to_pylist()
    truncated = False
    if limit is not None and limit > 0:
        if len(rows) > limit:
            rows = rows[:limit]
        # A request that hit the limit likely has more rows upstream.
        truncated = len(rows) >= limit
    return {
        "columns": columns,
        "rows": [[_json_safe(row.get(col)) for col in columns] for row in rows],
        "n_rows": len(rows),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# API handler implementations (engine passed in for testability)
# ---------------------------------------------------------------------------


def api_status(engine: SqlEngine) -> dict:
    from . import __version__

    return {
        "status": "ok",
        "version": __version__,
        "backend": engine.provider.kind if hasattr(engine.provider, "kind") else "unknown",
    }


def api_tables(engine: SqlEngine) -> dict:
    return {
        "tables": [
            {
                "name": t.name,
                "path": t.path,
                "qualified_name": t.qualified_name,
                "schema": t.schema,
                "format": t.format,
            }
            for t in engine.list_tables()
        ]
    }


def api_describe(engine: SqlEngine, table: str) -> dict:
    info = engine.describe_table(table)
    return {
        "table": table,
        "uri": info["uri"],
        "columns": info["columns"],
        "n_columns": info["n_columns"],
    }


def api_query(engine: SqlEngine, sql: str, limit: int | None = None) -> dict:
    import time

    safe = assert_readonly(sql)
    limit = _clamp_limit(limit)
    t0 = time.monotonic()
    arrow = engine.query_duckdb(safe, limit=limit)
    duration_ms = round((time.monotonic() - t0) * 1000, 1)
    payload = arrow_to_payload(arrow, limit=limit)
    payload.update({"sql": safe, "duration_ms": duration_ms})
    return payload


def api_preview(engine: SqlEngine, table: str, limit: int | None = None) -> dict:
    import time

    limit = _clamp_limit(limit)
    t0 = time.monotonic()
    arrow = engine.scan_arrow(table, limit=limit)
    duration_ms = round((time.monotonic() - t0) * 1000, 1)
    payload = arrow_to_payload(arrow, limit=limit)
    payload.update({"table": table, "duration_ms": duration_ms})
    return payload


def _clamp_limit(limit: int | None) -> int:
    """Normalise a client limit to (1, _MAX_LIMIT], defaulting to _DEFAULT_LIMIT."""
    if limit is None or limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


# ---------------------------------------------------------------------------
# Starlette route wiring
# ---------------------------------------------------------------------------


def register_ui(app, engine_getter) -> None:
    """Add the UI page + read-only JSON API routes to the Starlette app.

    ``engine_getter`` is a zero-arg callable returning the process-wide
    ``SqlEngine`` (so the same caches back the UI and the MCP tools).
    """

    def html_page(_request) -> HTMLResponse:
        return HTMLResponse(_HTML)

    async def status(_request) -> JSONResponse:
        return JSONResponse(api_status(engine_getter()))

    async def tables(_request) -> JSONResponse:
        try:
            return JSONResponse(api_tables(engine_getter()))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def describe(request) -> JSONResponse:
        body = await request.json()
        try:
            return JSONResponse(api_describe(engine_getter(), str(body.get("table", ""))))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def query(request) -> JSONResponse:
        body = await request.json()
        try:
            return JSONResponse(
                api_query(engine_getter(), str(body.get("sql", "")), body.get("limit"))
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def preview(request) -> JSONResponse:
        body = await request.json()
        try:
            return JSONResponse(
                api_preview(engine_getter(), str(body.get("table", "")), body.get("limit"))
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    app.add_route("/", html_page, methods=["GET"])
    app.add_route("/ui", html_page, methods=["GET"])
    app.add_route("/ui/index.html", html_page, methods=["GET"])
    app.add_route("/api/status", status, methods=["GET"])
    app.add_route("/api/tables", tables, methods=["GET"])
    app.add_route("/api/describe", describe, methods=["POST"])
    app.add_route("/api/query", query, methods=["POST"])
    app.add_route("/api/preview", preview, methods=["POST"])