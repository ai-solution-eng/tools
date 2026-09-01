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
  POST /api/query/async -> {"query_id", "state"} (long queries; poll /rows)
  GET  /api/query/{id}          -> job status (state, columns, n_rows)
  GET  /api/query/{id}/rows     -> paginated result rows (offset/limit)
  DELETE /api/query/{id}        -> cancel a running job
  POST /api/preview   -> {"columns": [...], "rows": [[...]], "n_rows", "duration_ms"}
  POST /api/profile   -> column-level statistics (min/max, null %, distinct, quantiles)
  POST /api/export    -> CSV/Parquet file download of a query or table (attachment)

The HTML page is served at ``/`` and ``/ui``.
"""

from __future__ import annotations

import asyncio
import math
import time as _time
import uuid
from collections import OrderedDict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from starlette.responses import HTMLResponse, JSONResponse, Response

from .engine import LakehouseError, QueryJob, SqlEngine, _max_rows, _validate_params, _validate_snapshot_version

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


def api_query(engine: SqlEngine, sql: str, limit: int | None = None, params: object | None = None) -> dict:
    import time

    safe = assert_readonly(sql)
    params = _validate_params(params)  # raises ValueError-like LakehouseError early
    limit = _clamp_limit(limit)
    t0 = time.monotonic()
    arrow = engine.query_duckdb(safe, limit=limit, params=params)
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


def _json_safe_deep(value):
    """Recursively convert Decimal/bytes/etc. values for JSON serialization.

    DuckDB's SUMMARIZE returns avg/std etc. as strings, but the count column
    arrives as Decimal through Arrow — JSONResponse refuses Decimals.
    """
    if isinstance(value, dict):
        return {k: _json_safe_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe_deep(v) for v in value]
    return _json_safe(value)


def api_profile(engine: SqlEngine, table: str, columns: list[str] | None = None) -> dict:
    """Column-level statistics (shares the engine's profile cache with MCP)."""
    import time

    t0 = time.monotonic()
    profile = _json_safe_deep(engine.profile_table(table, columns=columns))
    profile["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return profile


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
# async query jobs (submit / poll / paginate / cancel)
# ---------------------------------------------------------------------------

# Max rows per page from GET /api/query/{id}/rows — keeps any single page
# response bounded regardless of what the client asks for.
_MAX_PAGE_ROWS = 1000


def _job_ttl() -> int:
    """Seconds a finished job stays fetchable (SQLHANDLER_ASYNC_JOB_TTL)."""
    import os

    raw = os.environ.get("SQLHANDLER_ASYNC_JOB_TTL", "")
    try:
        return max(int(raw), 0) if raw else 900
    except ValueError:
        return 900


class QueryJobManager:
    """Process-wide registry of async query jobs (bounded, TTL-evicted).

    Jobs are the SAME :class:`~sqlhandler.engine.QueryJob` the synchronous
    path runs, so a submitted query honors SQLHANDLER_QUERY_TIMEOUT, the row
    caps and the query-memory hook. Results are spooled in memory as Arrow
    tables (already limited by SQLHANDLER_MAX_ROWS) and served paginated.
    """

    def __init__(self, max_jobs: int = 100):
        self._jobs: OrderedDict[str, tuple[QueryJob, float | None]] = OrderedDict()
        self._max_jobs = max_jobs

    def submit(self, engine: SqlEngine, sql: str, limit=None, params=None, version_as_of=None) -> dict:
        """Validate + start a job; returns {"query_id", "state"}.

        Validation happens BEFORE the job starts, so a bad payload is a
        synchronous error, not a job that immediately fails.
        """
        _validate_params(params)
        if version_as_of is not None:
            _validate_snapshot_version(version_as_of, "Time travel")
        self._cleanup()
        if len(self._jobs) >= self._max_jobs:
            return {"error": "Too many tracked queries; retry later.", "status": 429}
        job = QueryJob(engine, sql, limit=limit, params=params, version_as_of=version_as_of)
        query_id = uuid.uuid4().hex
        self._jobs[query_id] = (job, None)  # None finished_at = running
        return {"query_id": query_id, "state": job.state}

    def get(self, query_id: str) -> QueryJob | None:
        self._cleanup()
        entry = self._jobs.get(query_id)
        return entry[0] if entry else None

    def cancel(self, query_id: str) -> dict | None:
        job = self.get(query_id)
        if job is None:
            return None
        interrupted = job.cancel()
        return {"query_id": query_id, "state": job.state, "interrupted": interrupted}

    def _cleanup(self) -> None:
        """Evict finished jobs past the TTL, then enforce the size cap."""
        ttl = _job_ttl()
        now = _time.monotonic()
        for qid, (job, finished_at) in list(self._jobs.items()):
            if finished_at is None and job.state != "running":
                self._jobs[qid] = (job, now)
        if ttl > 0:
            for qid, (job, finished_at) in list(self._jobs.items()):
                if finished_at is not None and now - finished_at > ttl:
                    del self._jobs[qid]
        # Make room for the submit that triggered cleanup: evict oldest
        # finished jobs until BELOW the cap (a full registry of RUNNING
        # jobs is refused at submit time, never evicted here).
        while len(self._jobs) >= self._max_jobs:
            for qid, (job, finished_at) in self._jobs.items():
                if finished_at is not None:
                    del self._jobs[qid]
                    break
            else:
                break


def api_async_query(engine: SqlEngine, manager: QueryJobManager, body: dict) -> dict:
    """POST /api/query/async — start a read-only query, return a job id."""
    sql = str(body.get("sql", ""))
    safe = assert_readonly(sql)  # ValueError -> 400 (route wrapper)
    result = manager.submit(
        engine,
        safe,
        limit=body.get("limit"),
        params=body.get("params"),
        version_as_of=body.get("version_as_of"),
    )
    if result.get("error"):
        return result  # carries its own "status" for the route wrapper
    result["sql"] = safe
    return result


def api_query_status(manager: QueryJobManager, query_id: str) -> dict:
    """GET /api/query/{id} — job status (no row data)."""
    job = manager.get(query_id)
    if job is None:
        return {"error": f"Unknown query id: {query_id}", "status": 404}
    payload = job.info()
    payload["query_id"] = query_id
    if job.state == "done":
        arrow = job.result
        payload["columns"] = [f.name for f in arrow.schema]
        payload["n_rows"] = arrow.num_rows
        payload["elapsed_ms"] = round((job.elapsed_ms or 0), 1)
    return payload


def api_query_rows(manager: QueryJobManager, query_id: str, offset: int = 0, limit: int = 100) -> dict:
    """GET /api/query/{id}/rows — paginate the spooled result."""
    job = manager.get(query_id)
    if job is None:
        return {"error": f"Unknown query id: {query_id}", "status": 404}
    if job.state == "running":
        return {"query_id": query_id, "state": "running"}
    if job.state == "error":
        return {"error": job.error, "status": 400}
    if job.state == "cancelled":
        return {"error": "Query was cancelled.", "status": 400}
    arrow = job.result
    try:
        offset = max(int(offset), 0)
        limit = min(max(int(limit), 0), _MAX_PAGE_ROWS)
    except (TypeError, ValueError):
        return {"error": "offset/limit must be integers.", "status": 400}
    page = arrow.slice(offset, limit)
    payload = arrow_to_payload(page)
    payload.update(
        {
            "query_id": query_id,
            "state": "done",
            "offset": offset,
            "page_size": limit,
            "total_rows": arrow.num_rows,
        }
    )
    return payload


def api_query_cancel(manager: QueryJobManager, query_id: str) -> dict:
    """DELETE /api/query/{id} — cancel a running job."""
    result = manager.cancel(query_id)
    if result is None:
        return {"error": f"Unknown query id: {query_id}", "status": 404}
    return result


# ---------------------------------------------------------------------------
# export (CSV / Parquet file downloads)
# ---------------------------------------------------------------------------

_EXPORT_DEFAULT_ROWS = 10_000


def _export_max_rows() -> int:
    """Hard row cap for exports (SQLHANDLER_EXPORT_MAX_ROWS, default 100k).

    Exports intentionally get a HIGHER cap than the on-screen/LLM result cap
    (SQLHANDLER_MAX_ROWS, default 1000) — a file download is the point. 0
    here still means a hard 1,000,000-row ceiling so no request can try to
    materialize an unbounded file.
    """
    import os

    raw = os.environ.get("SQLHANDLER_EXPORT_MAX_ROWS", "")
    try:
        value = max(int(raw), 0) if raw else 100_000
    except ValueError:
        return 100_000
    return value if value > 0 else 1_000_000


def api_export(engine: SqlEngine, body: dict) -> dict:
    """POST /api/export — download a query or table result as CSV/Parquet.

    Accepts {"sql": ...} (read-only guard applies) or {"table": ...}
    (preview-style scan), optional "limit" (clamped to
    SQLHANDLER_EXPORT_MAX_ROWS) and "format" ("csv" | "parquet"). Returns
    {content: bytes, media_type, filename} for the route to send as an
    attachment.
    """
    import io

    import pyarrow.parquet as pq

    fmt = str(body.get("format", "csv")).strip().lower()
    if fmt not in ("csv", "parquet"):
        raise ValueError(f"Unsupported export format {fmt!r}; use 'csv' or 'parquet'.")
    try:
        raw_limit = body.get("limit")
        limit = _EXPORT_DEFAULT_ROWS if raw_limit is None else max(int(raw_limit), 0)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer.")
    cap = min(limit, _export_max_rows()) if limit > 0 else _export_max_rows()

    table = body.get("table")
    sql = body.get("sql")
    if sql:
        safe = assert_readonly(str(sql))  # ValueError -> 400
        arrow = engine.query_duckdb(safe, limit=cap, row_cap=cap)
        name = "query"
    elif table:
        arrow = engine.scan_arrow(str(table), limit=cap)
        name = str(table).replace("/", "_")
    else:
        raise ValueError("Provide either 'sql' or 'table' to export.")

    if fmt == "csv":
        content = arrow.to_pandas().to_csv(index=False).encode("utf-8")
        media_type = "text/csv"
    else:
        sink = io.BytesIO()
        pq.write_table(arrow, sink)
        content = sink.getvalue()
        media_type = "application/octet-stream"
    return {"content": content, "media_type": media_type, "filename": f"{name}.{fmt}"}


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
            return JSONResponse(
                api_query(
                    engine_getter(),
                    str(body.get("sql", "")),
                    body.get("limit"),
                    body.get("params"),
                )
            )
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

    async def profile(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            cols = body.get("columns")
            col_list = [str(c) for c in cols if str(c).strip()] if isinstance(cols, list) else None
            return JSONResponse(api_profile(engine_getter(), str(body.get("table", "")), col_list))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ---- async query jobs (long queries: submit, poll, paginate, cancel)
    manager = QueryJobManager()

    def _response(payload: dict) -> JSONResponse:
        """payload dicts may carry a "status" hint for the HTTP code."""
        status = payload.pop("status", None)
        return JSONResponse(payload, status_code=status if isinstance(status, int) else 200)

    async def query_async(request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object."}, status_code=400)
        try:
            # submit may block on the query-concurrency gate — never block
            # the event loop.
            return _response(await asyncio.to_thread(api_async_query, engine_getter(), manager, body))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LakehouseError as exc:
            # concurrency-gate refusals (queue wait expired) → 429
            return JSONResponse({"error": str(exc)}, status_code=429)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    async def query_job_status(request) -> JSONResponse:
        return _response(api_query_status(manager, request.path_params["query_id"]))

    async def query_job_rows(request) -> JSONResponse:
        params = request.query_params
        return _response(
            api_query_rows(
                manager,
                request.path_params["query_id"],
                params.get("offset", 0),
                params.get("limit", 100),
            )
        )

    async def query_job_cancel(request) -> JSONResponse:
        return _response(api_query_cancel(manager, request.path_params["query_id"]))

    app.add_route("/", html_page, methods=["GET"])
    app.add_route("/ui", html_page, methods=["GET"])
    app.add_route("/ui/index.html", html_page, methods=["GET"])
    app.add_route("/api/status", status, methods=["GET"])
    app.add_route("/api/tables", tables, methods=["GET"])
    app.add_route("/api/describe", describe, methods=["POST"])
    app.add_route("/api/query", query, methods=["POST"])
    app.add_route("/api/query/async", query_async, methods=["POST"])
    app.add_route("/api/query/{query_id}", query_job_status, methods=["GET"])
    app.add_route("/api/query/{query_id}/rows", query_job_rows, methods=["GET"])
    app.add_route("/api/query/{query_id}", query_job_cancel, methods=["DELETE"])
    async def export(request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
        try:
            result = api_export(engine_getter(), body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return Response(
            content=result["content"],
            media_type=result["media_type"],
            headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"'},
        )

    app.add_route("/api/preview", preview, methods=["POST"])
    app.add_route("/api/profile", profile, methods=["POST"])
    app.add_route("/api/export", export, methods=["POST"])
