"""Read-only web UI + JSON API for the SQLhandler MCP server.

Serves a self-contained HTML explorer (``index.html``) and a small JSON API
that reuses the SAME process-wide ``SqlEngine`` (and its caches) that backs
the MCP tools. The API is deliberately **read-only**: every statement is
parsed with DuckDB's own grammar and only plain SELECT queries (plus EXPLAIN
of a SELECT) are accepted, so the UI is a safe human front-end for the same
data the MCP agents query. Additionally, the DuckDB connection used for
these queries has local-file access disabled (see ``engine.py``), so a
SELECT cannot read files inside the container or COPY results out.

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

from .engine import SqlEngine, _max_rows

_DEFAULT_LIMIT = 100
# Fallback UI cap when SQLHANDLER_MAX_ROWS is unset or 0 (unlimited for the
# MCP path): the browser API still needs a bounded payload size.
_FALLBACK_MAX_LIMIT = 1000

_HTML = (Path(__file__).parent / "ui" / "index.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# read-only guard
# ---------------------------------------------------------------------------


def _parse_statement_types(sql: str) -> list[str]:
    """Parse ``sql`` with DuckDB's real parser; return one type per statement.

    Uses DuckDB's own grammar (string literals, comments and multi-statement
    text are handled correctly), unlike keyword prefix checks. Raises
    ValueError when the text does not parse at all.
    """
    import duckdb

    try:
        statements = duckdb.extract_statements(sql)
    except Exception as exc:
        raise ValueError(f"Could not parse SQL: {exc}") from exc
    return [str(s.type).split(".")[-1] for s in statements]


def _explain_inner_sql(statement: str) -> str:
    """Strip a leading ``EXPLAIN [ANALYZE] [VERBOSE]`` and return the rest."""
    rest = statement
    for keyword in ("EXPLAIN", "ANALYZE", "VERBOSE"):
        rest = rest.lstrip(" \t\r\n(")
        parts = rest.split(None, 1)
        if parts and parts[0].upper() == keyword:
            rest = parts[1] if len(parts) > 1 else ""
        else:
            break
    return rest


def assert_readonly(sql: str) -> str:
    """Return the trimmed SQL if it is a read-only statement, else raise.

    Every parsed statement must be a plain SELECT (DuckDB parses WITH/VALUES/
    SHOW/DESCRIBE/SUMMARIZE as SELECT too). EXPLAIN is allowed only when the
    explained statement is itself a SELECT — ``EXPLAIN ANALYZE INSERT``
    actually executes the insert, so it is rejected. PRAGMA/SET, COPY, and
    every write statement are rejected regardless of position.

    This guards the *web UI / JSON API* only. The MCP ``run_sql`` tool is for
    trusted agent callers and does not pass through this filter (see README).
    """
    text = sql.strip()
    if not text:
        raise ValueError("Empty SQL statement.")
    types = _parse_statement_types(text)
    for stmt_type, statement in zip(types, _split_statements(text), strict=False):
        # Belt and braces: PRAGMA is a SET alias in DuckDB, and some
        # table-valued pragmas even parse as SELECT — reject the keyword
        # itself, the web UI has no use for it.
        head = statement.lstrip(" \t\r\n(")
        if head.upper().startswith("PRAGMA"):
            raise ValueError(
                "Read-only UI: PRAGMA statements are not allowed. "
                "Only SELECT / WITH / VALUES / EXPLAIN SELECT queries are permitted."
            )
        if stmt_type == "EXPLAIN":
            inner = _explain_inner_sql(statement)
            for inner_type in _parse_statement_types(inner):
                if inner_type != "SELECT":
                    raise ValueError(
                        f"Read-only UI: EXPLAIN of a {inner_type} statement is not allowed "
                        "(EXPLAIN ANALYZE would execute it). Only SELECT queries are permitted."
                    )
        elif stmt_type != "SELECT":
            raise ValueError(
                f"Read-only UI: {stmt_type} statements are not allowed. "
                "Only SELECT / WITH / VALUES / EXPLAIN SELECT queries are permitted."
            )
    return text


def _split_statements(sql: str) -> list[str]:
    """Best-effort per-statement text slices matching the parsed statements.

    Only used for EXPLAIN inner-statement inspection, where a rough slice is
    enough (the inner text is re-parsed, not executed as-is).
    """
    parts, current = [], []
    in_string = False
    for char in sql:
        if char == "'" and not in_string:
            in_string = True
        elif char == "'" and in_string:
            in_string = False
        if char == ";" and not in_string:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [p for p in (part.strip() for part in parts) if p]


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
                "source": t.source,
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
    """Normalise a client limit to (1, cap], defaulting to _DEFAULT_LIMIT.

    The cap follows ``SQLHANDLER_MAX_ROWS`` (same knob as the MCP path) when
    it is set to a positive value, so raising it raises the UI cap too. When
    it is 0 (unlimited) the UI still clamps to _FALLBACK_MAX_LIMIT to keep
    the JSON payload bounded.
    """
    cap = _max_rows()
    if cap <= 0:
        cap = _FALLBACK_MAX_LIMIT
    if limit is None or limit <= 0:
        return min(_DEFAULT_LIMIT, cap)
    return min(limit, cap)


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
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            return JSONResponse(api_describe(engine_getter(), str(body.get("table", ""))))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def query(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            return JSONResponse(api_query(engine_getter(), str(body.get("sql", "")), body.get("limit")))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def preview(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            return JSONResponse(api_preview(engine_getter(), str(body.get("table", "")), body.get("limit")))
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
