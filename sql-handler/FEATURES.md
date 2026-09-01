# SQLhandler — Features

Fast, direct SQL access to columnar lake data — OneLake/Delta Lake, S3/MinIO
Parquet, Delta-on-S3, Iceberg catalogs, and NFS/local directories — exposed as
an **MCP server** with a bundled read-only web UI. This document describes the
features of the **current stack** (`main` working tree, version **1.0.0**;
the last tagged release was 0.8.0 — everything below is the feature wave built
as "0.9.0" and shipped as 1.0.0, a deliberate version jump for the feature
lift, unless marked otherwise).

---

## Current stack

| Layer | Technology |
|---|---|
| Language | Python ≥ 3.11, fully type-checked (mypy) and linted (ruff) |
| MCP | `mcp>=2.0` SDK — low-level `MCPServer`, stateless streamable-HTTP at `/mcp` |
| SQL engine | DuckDB ≥ 1.0 (per-process connection; aggregations/joins push down to the lake) |
| Data access | pyarrow ≥ 17 (S3/Azure filesystems + dataset engine), `deltalake` ≥ 1.0 (native Delta reader incl. OneLake ABFS), optional `pyiceberg[pyarrow]` (+ SQLAlchemy for SQL catalogs) |
| HTTP | uvicorn + Starlette ASGI app hosting `/mcp`, the JSON API, `/ui`, `/health`, `/ready`, `/metrics` |
| Web UI | one self-contained `index.html` — no build step, no CDN, no framework |
| Deploy | Helm chart for Kubernetes / PCAI (oauth2-proxy gateway, non-root hardened image, health/startup probes) |
| Ops | zero extra dependencies — Prometheus text exposition and JSONL audit log are hand-rolled |

The `DataProvider` interface is the only extension point for a new source:
each backend (onelake, s3, iceberg, nfs/file) is a subclass plus one
`make_provider` branch.

---

## 1. Agent ergonomics (MCP surface)

Features that make LLM agents effective against the lake on the first try.

### Tools
- `list_tables` — enumerate tables (any backend), annotated with catalog
  descriptions when configured; source-qualified names in federated mode.
- `search_tables` — keyword search over table/column names **and** semantic
  catalog docs, for when the table list is long.
- `describe_table` — columns/types/URI + catalog docs.
- `profile_table` — column-level statistics **before** writing SQL: min/max,
  approx distinct count, null %, avg/std, q25/q50/q75, exact row count from
  Parquet/Delta metadata. Optional comma-separated `columns` subset. Scans a
  bounded sample (`SQLHANDLER_PROFILE_MAX_ROWS`, default 1M; 0 = full) and is
  cached like describe.
- `run_sql` — DuckDB SQL with `output_format` (**markdown** default / json /
  csv), bind `params`, and `version_as_of` time travel.
- `scan_table` — pyarrow column/row pull with row limit; same formats and
  time travel.

### Resources & prompts (the other MCP primitives)
- `sqlhandler://catalog` — every table + its business description (data dictionary).
- `sqlhandler://table/<name>/schema` — one table's schema + column docs
  (also exposed as the resource template `sqlhandler://table/{table}/schema`).
- `sqlhandler://query-memory` — the last 50 query outcomes
  (`SQLHANDLER_QUERY_MEMORY_SIZE`), so later agent sessions reuse proven SQL
  patterns instead of rediscovering them.
- Prompts: `explore-data` (list → catalog → profile → SQL) and
  `analyze-table` (profile-first deep dive on one table).

### Semantic catalog
`SQLHANDLER_CATALOG=<file.json>` merges human-written table/column
documentation into `list_tables` / `describe_table` / resources. Hot-reloaded
on change (cached describes invalidated); a missing/broken file never breaks
queries.

### Self-correction loops
- **Did-you-mean errors** — a bad table name in SQL returns the nearest real
  table names, so the agent self-corrects in one round-trip.
- **Usage-driven prewarm** — with `SQLHANDLER_PREWARM_TABLES` unset, the
  busiest tables of the previous run (usage counts persisted with the
  disk-warm cache) are prewarmed on restart.

---

## 2. Query engine capabilities

- **Parameterized queries** — `run_sql` accepts bind `params`: an object for
  named `$placeholders` or an array for positional `?`. Reusable templates
  stay injection-safe.
- **Time travel** — `version_as_of` on `run_sql` / `scan_table`: a Delta
  snapshot version (nfs / onelake / Delta-on-S3) or an Iceberg snapshot id.
  Applies to every versionable table the query touches; plain-Parquet tables
  in the same query are a clear error. Historical datasets are cached per
  version (a snapshot never changes).
- **Query timeout** — `SQLHANDLER_QUERY_TIMEOUT` (seconds, 0 = off) interrupts
  a query inside DuckDB: no leaked threads, clean error to the caller. Applies
  to MCP tools and the web API alike.
- **Concurrency cap** — `SQLHANDLER_MAX_CONCURRENT_QUERIES` (default 8,
  0 = unlimited) bounds simultaneous DuckDB queries per pod; excess queries
  queue up to `SQLHANDLER_QUEUE_TIMEOUT` (default 30 s) then fail with a clear
  error instead of piling up on the container.
- **Output formats** — every result-returning tool/endpoint renders markdown
  (human/LLM-friendly), compact JSON (`{columns, rows}`), or CSV.
- **Delta tables on S3** — `S3_FORMAT=auto|parquet|delta` on the s3 backend:
  `auto` (default) detects Delta tables by their `_delta_log` and reads the
  rest as plain Parquet, so one bucket can mix formats with time travel where
  a Delta log exists.

---

## 3. Web API & UI (read-only data explorer)

Same engine, caches, and read-only guard as the MCP tools — no extra deployment.

### Async query jobs (long-running queries)
| Endpoint | Purpose |
|---|---|
| `POST /api/query/async` | validate + start; returns `{"query_id", "state"}` |
| `GET /api/query/{id}` | status: `running`/`done`/`error`/`cancelled`, columns, n_rows |
| `GET /api/query/{id}/rows?offset=&limit=` | paginated rows (page cap 1000) |
| `DELETE /api/query/{id}` | cancel a running job (DuckDB interrupt) |

Finished jobs are kept for `SQLHANDLER_ASYNC_JOB_TTL` seconds (default 900),
up to 100 tracked jobs; the registry refuses with HTTP 429 when full of
running jobs.

Jobs, the synchronous query path, and the query timeout all run on the **same
`QueryJob` primitive** (own thread, DuckDB `con.interrupt()` for cancellation)
— one implementation serving the MCP tools, the JSON API, and the async
registry, so cancel/timeout semantics cannot drift between surfaces.

### Profile & export endpoints
- `POST /api/profile` — column statistics (min/max, null %, distinct, quartiles).
- `POST /api/export` — download a query or table as **CSV or Parquet**
  (attachment), capped by `SQLHANDLER_EXPORT_MAX_ROWS` (default 100 000;
  0 = hard 1M ceiling).

### UI features
- Searchable table list with format badges and `source/schema/name` labels in
  federated mode; per-table schema view.
- **Column stats panel** — lazy "Profile table" button per table.
- SQL editor (Ctrl+Enter) with row limit; results table with timing and
  copy-CSV.
- **Export buttons** — CSV and Parquet download of the current query.
- **Charts** — one-click inline SVG bar chart of the first numeric column
  (dependency-free).
- **Saved queries + history** — per-browser (localStorage): save/reload/delete
  queries; history keeps the last 30 successful runs.
- Light/dark theme (OS-aware, persisted) with HPE branding.
- **Read-only guarantee** — every statement is parsed with DuckDB's own
  grammar; only plain `SELECT` (plus `EXPLAIN SELECT`) is accepted. Writes,
  `PRAGMA`/`SET`, and `COPY` are rejected. Results are clamped to
  `SQLHANDLER_MAX_ROWS` (default 1000).

---

## 4. Observability & ops

- **Prometheus metrics** — `GET /metrics` renders the text exposition
  (0.0.4) with no extra dependency:
  `sqlhandler_queries_total{outcome}` (ok/error/timeout/cancelled),
  `sqlhandler_query_duration_seconds` histogram, `sqlhandler_query_rows_total`,
  `sqlhandler_cache_{hits,misses}_total{cache}` (describe/profile/dataset),
  and gauges for table count, process RSS, and the container memory limit.
- **Audit log** — `SQLHANDLER_AUDIT_LOG=/path/audit.jsonl` appends one JSON
  line per query outcome (`ts`, `event`, `sql`, `state`, `duration_ms`,
  `n_rows`, `error`) — compliance-grade, SIEM-friendly. Best-effort writes
  never break a query.
- **API token** — `SQLHANDLER_API_TOKEN` requires `Authorization: Bearer` or
  `X-API-Token` (constant-time compared) on every `/api/*` request, for
  deployments not already behind the oauth2-proxy gateway. `/mcp`, `/ui`,
  `/health`, `/ready` are unaffected.
- **Resilience (carried from 0.8.0)** — cgroup-proportional DuckDB memory
  budgets with spill-to-disk, disk-warm metadata cache, and
  health/readiness/startup probes.

### Deployment note (known chart gap)

The Helm chart still renders a **fixed env list**, so the new `SQLHANDLER_*`
knobs are not yet chart-configurable. Concretely: the semantic-catalog file
needs a ConfigMap + volume mount, the audit log needs a writable volume
(emptyDir, or PVC if it must survive restarts), and `/metrics` needs optional
scrape annotations. Until a chart follow-up lands, configure the knobs via
`docker run -e` — an image-only rollout is **default-safe** (timeout, audit,
and token off; `S3_FORMAT=auto`; export cap 100k; the one new *active* default
is the concurrency cap of 8).

---

## Feature → code → tests map

| Feature | Module(s) | Tests |
|---|---|---|
| `profile_table` / `search_tables` tools, params, time travel, timeout, concurrency gate, query memory | `engine.py`, `server.py` | `test_engine.py` |
| MCP resources + prompts | `mcp_resources.py`, `server.py` | `test_mcp_resources.py` |
| Semantic catalog merge/hot-reload | `engine.py` | `test_engine.py` |
| Output formats (markdown/json/csv) | `engine.py`, `webui.py` | `test_tools_output.py` |
| Async query jobs (submit/poll/rows/cancel) | `webui.py`, `engine.py` | `test_async_query.py`, `test_webui.py` |
| `/api/profile`, `/api/export` | `webui.py` | `test_webui.py` |
| Delta on S3 (`S3_FORMAT`) | `s3.py`, `config.py` | `test_s3_delta.py` |
| Metrics, audit log, API token | `observability.py`, `server.py` | `test_ops.py` |
| UI: stats panel, charts, export, saved queries | `ui/index.html` | `test_webui.py` |

All 62 tests in the five new test files (`test_ops`, `test_mcp_resources`,
`test_async_query`, `test_s3_delta`, `test_tools_output`) pass on the current
tree.

## New configuration knobs (this wave)

| Variable | Default | Purpose |
|---|---|---|
| `SQLHANDLER_CATALOG` | — | Semantic catalog JSON file (hot-reloaded) |
| `SQLHANDLER_QUERY_MEMORY_SIZE` | 50 | Query-memory ring size behind `sqlhandler://query-memory` |
| `SQLHANDLER_PROFILE_MAX_ROWS` | 1000000 | Sample cap for profiling (0 = full table) |
| `SQLHANDLER_QUERY_TIMEOUT` | 0 (off) | DuckDB interrupt after N seconds |
| `SQLHANDLER_MAX_CONCURRENT_QUERIES` | 8 | Per-pod concurrency cap (0 = unlimited) |
| `SQLHANDLER_QUEUE_TIMEOUT` | 30 | Seconds a query may wait for a slot |
| `SQLHANDLER_ASYNC_JOB_TTL` | 900 | Seconds finished async jobs are kept |
| `SQLHANDLER_EXPORT_MAX_ROWS` | 100000 | Row cap for CSV/Parquet exports (0 = 1M ceiling) |
| `SQLHANDLER_AUDIT_LOG` | — | JSONL audit file path |
| `SQLHANDLER_API_TOKEN` | — | Bearer/X-API-Token gate for `/api/*` |
| `S3_FORMAT` | auto | `auto` \| `parquet` \| `delta` for the s3 backend |
