"""Shared, backend-agnostic SQL engine.

A :class: SqlEngine owns everything that does not care *where* the data
lives: the in-process caches (table list, describe results, open pyarrow
Datasets), the DuckDB query path, columnar scans, prewarm and cache stats.
It talks to storage only through the thin :class: DataProvider interface,
so OneLake and S3/MinIO (and any future backend) share exactly this code.

Design notes (kept from the original OneLake handler):

- A single process-wide engine is reused (a fresh one per tool call would
  silently discard the table-list cache on every request).
- list_tables / describe_table are cached for cache_ttl seconds - these
  hit the metadata endpoint (DFS REST / S3 list) and the file footers, and
  agents repeat them every session.
- Open pyarrow Datasets are reused per table for dataset_cache_ttl seconds,
  LRU-bounded by dataset_cache_tables - this skips re-reading the metadata
  (Delta _delta_log / Parquet footer) on every query. Only the handle is
  held; rows are still read from the source on each scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

import pyarrow as pa

from . import observability, resources
from .provider import DataProvider, LakehouseError, TableInfo, _validate_snapshot_version

logger = logging.getLogger("sqlhandler.engine")


def _query_memory_size() -> int:
    """How many recent query outcomes to remember (SQLHANDLER_QUERY_MEMORY_SIZE).

    Recorded (sql, duration, rows, error) tuples power the query-memory MCP
    resource — later agent sessions reuse proven query patterns instead of
    rediscovering them. 0 disables recording. Default 50.
    """
    raw = os.environ.get("SQLHANDLER_QUERY_MEMORY_SIZE", "")
    try:
        return max(int(raw), 0) if raw else 50
    except ValueError:
        return 50


# Usage counts flush to the disk-warm cache after this many dataset opens
# (piggybacked on the per-query note_query hook — no IO on the scan path).
_USAGE_SAVE_EVERY = 20


def _query_timeout() -> float:
    """Per-query wall-clock timeout in seconds (SQLHANDLER_QUERY_TIMEOUT).

    Applies to the DuckDB SQL path (MCP run_sql and the web API alike);
    0 (the default) keeps the old no-timeout behavior. On expiry the query
    is interrupted inside DuckDB (not leaked) and a LakehouseError surfaces.
    """
    raw = os.environ.get("SQLHANDLER_QUERY_TIMEOUT", "")
    try:
        return max(float(raw), 0.0) if raw else 0.0
    except ValueError:
        return 0.0


# After a timeout-triggered interrupt, how long to wait for the query thread
# to unwind before reporting the timeout anyway (the result is discarded
# either way; this only bounds the error latency).
_CANCEL_GRACE_SECONDS = 5.0


def _max_concurrent_queries() -> int:
    """Max simultaneous DuckDB queries (SQLHANDLER_MAX_CONCURRENT_QUERIES).

    Each QueryJob runs in its own thread against its own DuckDB connection;
    without a cap an agent burst (or a loop of eager clients) can pile up
    dozens of scans on the same pod. Default 8; 0 = unlimited (old behavior).
    Excess queries QUEUE (waiting for a slot) up to SQLHANDLER_QUEUE_TIMEOUT
    seconds, then fail with a clear error instead of running unbounded.
    """
    raw = os.environ.get("SQLHANDLER_MAX_CONCURRENT_QUERIES", "")
    try:
        return max(int(raw), 0) if raw else 8
    except ValueError:
        return 8


def _queue_timeout() -> float:
    """Seconds a query may wait for a concurrency slot (SQLHANDLER_QUEUE_TIMEOUT)."""
    raw = os.environ.get("SQLHANDLER_QUEUE_TIMEOUT", "")
    try:
        return max(float(raw), 0.0) if raw else 30.0
    except ValueError:
        return 30.0


class _QueryGate:
    """A resizable-ish concurrency gate for queries (semaphore based).

    Sized once from SQLHANDLER_MAX_CONCURRENT_QUERIES at first use. Acquire
    blocks (queueing) up to SQLHANDLER_QUEUE_TIMEOUT; cross-thread release
    is safe with a plain Semaphore.
    """

    def __init__(self):
        self._sem: threading.Semaphore | None = None
        self._size = -1
        self._lock = threading.Lock()

    def _semaphore(self) -> threading.Semaphore | None:
        size = _max_concurrent_queries()
        if size <= 0:
            return None
        with self._lock:
            if self._sem is None or self._size != size:
                # Tests reconfigure via env: rebuild when the size changes.
                self._sem = threading.Semaphore(size)
                self._size = size
            return self._sem

    def acquire(self) -> None:
        sem = self._semaphore()
        if sem is None:
            return
        timeout = _queue_timeout()
        if not sem.acquire(timeout=timeout):
            raise LakehouseError(
                f"Too many concurrent queries (limit {_max_concurrent_queries()}), and the "
                f"queue wait of {_queue_timeout()}s expired. Retry later."
            )

    def release(self) -> None:
        sem = self._sem
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass


_query_gate = _QueryGate()


class QueryJob:
    """One DuckDB query running on its own thread: cancellable and observable.

    The engine's synchronous :meth:`SqlEngine.query_duckdb` is implemented ON
    TOP of this class (submit + wait), and the web API's async-query endpoints
    submit the same jobs and poll them — one execution path for both, so
    timeouts and cancellation behave identically everywhere.

    Cancellation works because the connection lives for the duration of the
    job: ``cancel()`` calls DuckDB's ``interrupt()`` from the caller's thread,
    which raises inside the running query.
    """

    def __init__(
        self,
        engine: SqlEngine,
        sql: str,
        limit: int | None = None,
        params: object | None = None,
        version_as_of: int | None = None,
        row_cap: int | None = None,
    ):
        if version_as_of is not None:
            _validate_snapshot_version(version_as_of, "Time travel")
        self._engine = engine
        self.sql = sql
        self._limit = limit
        self._params = _validate_params(params)
        self._version = version_as_of
        # Explicit cap override for this query (None = SQLHANDLER_MAX_ROWS).
        # The export endpoint raises it so a file download isn't truncated
        # by the (much lower) default LLM-payload cap.
        self._row_cap = row_cap
        self._lock = threading.Lock()
        self._con = None  # live only while the query runs (cancel handle)
        self._state = "running"
        self._error: str | None = None
        self._result: pa.Table | None = None
        self._cancelled = False
        self._t0 = time.monotonic()
        self._elapsed_ms: float | None = None
        # Concurrency gate: acquire BEFORE the thread starts so an over-cap
        # query queues in the caller's thread (works for both the sync path
        # and the async API, whose submit runs off the event loop). Raises
        # LakehouseError when the queue wait expires — construction fails,
        # no job exists.
        _query_gate.acquire()
        self._gate_held = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="sqlhandler-query"
        )
        self._thread.start()

    # ---------------------------------------------------------- execution
    def _run(self) -> None:
        import duckdb

        con = duckdb.connect()
        with self._lock:
            self._con = con
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            self._engine._register_schema(con, self.sql, version=self._version)
            rel = con.sql(self.sql, params=self._params)
            # Row-cap semantics: a LIMIT is pushed into the query so an
            # unbounded SELECT can't materialize millions of rows in memory.
            cap = self._row_cap if self._row_cap is not None else _max_rows()
            eff = self._limit if (self._limit is not None and self._limit >= 0) else None
            if cap > 0:
                eff = min(eff, cap) if eff is not None else cap
            if eff is not None:
                rel = rel.limit(eff)
            arrow_table = rel.arrow()
            if isinstance(arrow_table, pa.RecordBatchReader):
                arrow_table = arrow_table.read_all()
            if eff is not None and arrow_table.num_rows > eff:
                arrow_table = arrow_table.slice(0, eff)
            with self._lock:
                if self._cancelled:
                    # cancel() raced the finish and lost — the caller still
                    # asked for cancellation, so the result is discarded.
                    self._state = "cancelled"
                    self._error = "Query was cancelled."
                    self._result = None
                else:
                    self._result = arrow_table
                    self._state = "done"
                    self._elapsed_ms = (time.monotonic() - self._t0) * 1000
            if self._state == "done":
                self._engine._record_outcome(
                    self.sql, self._elapsed_ms, arrow_table.num_rows, state="ok"
                )
        except Exception as exc:
            elapsed = (time.monotonic() - self._t0) * 1000
            with self._lock:
                self._elapsed_ms = elapsed
                if self._cancelled:
                    self._state = "cancelled"
                    self._error = "Query was cancelled."
                else:
                    self._state = "error"
                    hinted = _with_hints(self._engine, self.sql, exc)
                    self._error = f"DuckDB query failed: {hinted}"
            self._engine._record_outcome(
                self.sql, elapsed, None, state=self._state, error=self._error
            )
        finally:
            try:
                con.close()
            except Exception:
                pass
            with self._lock:
                self._con = None
            if self._gate_held:
                self._gate_held = False
                _query_gate.release()

    # ------------------------------------------------------------ control
    def cancel(self) -> bool:
        """Interrupt a running query; True if a live query was interrupted."""
        with self._lock:
            self._cancelled = True
            con = self._con
        if con is None:
            return False
        try:
            con.interrupt()
            return True
        except Exception:
            return False

    def wait(self, timeout: float | None = None) -> str:
        """Block until the job finishes (or ``timeout`` elapses); returns state."""
        self._thread.join(timeout)
        return self.state

    @property
    def state(self) -> str:
        """running | done | error | cancelled."""
        with self._lock:
            if self._state == "running" and not self._thread.is_alive():
                # Defensive: a thread that died without recording a state is
                # an error, not an eternal "running".
                self._state = "error"
                self._error = self._error or "Query thread ended without a result."
            return self._state

    @property
    def elapsed_ms(self) -> float | None:
        return self._elapsed_ms

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def result(self) -> pa.Table:
        """The arrow result; raises LakehouseError unless the job is done."""
        state = self.state
        if state == "done":
            assert self._result is not None
            return self._result
        if state == "cancelled":
            raise LakehouseError("Query was cancelled.")
        raise LakehouseError(self._error or f"Query did not complete (state: {state}).")

    def info(self) -> dict:
        """Status snapshot for the async-query API (no row data)."""
        return {
            "sql": self.sql,
            "state": self.state,
            "elapsed_ms": round(self._elapsed_ms, 1) if self._elapsed_ms is not None else None,
            "error": self._error,
        }


def _max_rows() -> int:
    """Server-side row cap for SQL results (SQLHANDLER_MAX_ROWS, default 1000).

    Prevents a client with no limit from materializing an unbounded result
    (e.g. SELECT * over a multi-million-row table) that would exhaust memory.
    0 disables the cap.
    """
    raw = os.environ.get("SQLHANDLER_MAX_ROWS", "")
    try:
        return max(int(raw), 0) if raw else 1000
    except ValueError:
        return 1000


def _profile_max_rows() -> int:
    """Row cap for the profiling input (SQLHANDLER_PROFILE_MAX_ROWS).

    ``profile_table`` scans data to compute column statistics; this bounds
    how many rows of each table are summarized (default 1,000,000; 0 = the
    full table). Statistics over a large uniform sample are what an LLM
    needs to write correct filters; the exact full-table row count still
    comes from the Parquet/Delta metadata for free.
    """
    raw = os.environ.get("SQLHANDLER_PROFILE_MAX_ROWS", "")
    try:
        return max(int(raw), 0) if raw else 1_000_000
    except ValueError:
        return 1_000_000


def _duckdb_fs_lockdown(con) -> None:
    """Disable DuckDB's own file/network access for SQL queries (default on).

    Tables reach DuckDB as registered pyarrow datasets, and ALL object-store
    IO (S3/ABFS/NFS) is done by pyarrow *outside* DuckDB — so locking DuckDB's
    built-in filesystems away costs nothing and closes real holes on a
    network-facing endpoint: `read_csv('/etc/passwd')`, `COPY ... TO '/tmp'`,
    `parquet_scan(...)` of local files, and extension-based URL fetches
    (httpfs) all fail closed. Opt out with SQLHANDLER_DUCKDB_FILE_ACCESS=1 if
    a query genuinely needs DuckDB file/table functions.
    """
    raw = os.environ.get("SQLHANDLER_DUCKDB_FILE_ACCESS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return
    try:
        con.execute("SET disabled_filesystems='LocalFileSystem'")
        con.execute("SET autoinstall_known_extensions=false")
        con.execute("SET autoload_known_extensions=false")
    except Exception:  # older DuckDB without a knob: fail open rather than break queries
        logger.debug("DuckDB filesystem lockdown partially unavailable", exc_info=True)


_budget_logged = False


def _apply_memory_budget(con) -> None:
    """Give DuckDB a budget derived from the container's own limits (fail open).

    DuckDB's defaults are sized from the NODE's RAM (80% of /proc/meminfo) —
    on a big node with a small pod that means a single wide scan can grow RSS
    past the pod's cgroup limit and get the process OOMKilled, wiping every
    in-process cache (table list, describe results, open datasets). Setting
    ``memory_limit`` to a fraction of the container limit makes DuckDB spill
    to ``temp_directory`` (k8s: mount an emptyDir there) instead; ``threads``
    matches the CPU limit so the pool doesn't get sized from the node's cores.
    Every step is best-effort: an old DuckDB or an unreadable cgroup just
    leaves DuckDB's defaults in place.
    """
    global _budget_logged
    try:
        budget = resources.duckdb_budget()
        if not budget:
            return
        mem = budget.get("memory_limit")
        if mem:
            con.execute(f"SET memory_limit='{mem}'")
        threads = budget.get("threads")
        if threads:
            con.execute(f"SET threads={int(threads)}")
        temp_dir = budget.get("temp_directory")
        if temp_dir:
            safe_dir = str(temp_dir).replace("'", "")
            os.makedirs(safe_dir, exist_ok=True)
            con.execute(f"SET temp_directory='{safe_dir}'")
        con.execute("SET preserve_insertion_order=false")
        if not _budget_logged:
            _budget_logged = True
            logger.info(
                "duckdb budget applied: memory_limit=%s threads=%s temp_directory=%s "
                "(container limit: %s bytes / %s cpus)",
                mem,
                threads,
                temp_dir,
                budget.get("container_memory_bytes"),
                budget.get("container_cpu_count"),
            )
    except Exception:
        logger.debug("DuckDB memory budget not applied", exc_info=True)


def _safe_table_name(table: str) -> str:
    """Reject table names that try to escape their source root.

    Guards the engine's resolve-fallback (which builds ``<schema>/<name>``
    from raw user input): no absolute paths, no ``..`` traversal segments,
    no NUL bytes. Legitimate names are bare identifiers or ``schema/name``.
    """
    if not table or "\x00" in table or table.startswith(("/", "\\")):
        raise LakehouseError(f"Invalid table name: {table!r}")
    if any(part == ".." for part in table.replace("\\", "/").split("/")):
        raise LakehouseError(f"Invalid table name (path traversal): {table!r}")
    return table


def _safe_ident(name: str) -> str:
    """Quote an identifier for DuckDB if it contains special characters."""
    if name.replace("_", "").isalnum():
        return name
    return '"' + name.replace('"', '""') + '"'


def _validate_params(params: object) -> object | None:
    """Validate user-supplied query parameters (named dict or positional list).

    Only scalar parameter values are accepted (str, int, float, bool, bytes,
    datetime/date/time, Decimal, None) — nested containers are refused, since
    list/struct parameters need matching SQL types and would otherwise fail
    deep inside DuckDB with a confusing error. Raises ValueError (a client
    input error, mapped to HTTP 400 by the web API).
    """
    import datetime
    from decimal import Decimal

    if params is None:
        return None
    if isinstance(params, dict):
        if not all(isinstance(k, str) for k in params):
            raise ValueError("Query params: named parameters need string keys.")
        values = list(params.values())
    elif isinstance(params, (list, tuple)):
        values = list(params)
    else:
        # ValueError, not TypeError: this is CLIENT INPUT validation, mapped
        # to HTTP 400 by the web API (a TypeError would surface as a 500).
        raise ValueError(  # noqa: TRY004
            "Query params must be an object ({$name: value}) or an array (positional ?)."
        )
    scalars = (str, int, float, bool, bytes, datetime.datetime, datetime.date, datetime.time, Decimal)
    for v in values:
        if v is not None and not isinstance(v, scalars):
            raise ValueError(
                "Query params must be scalars (str/int/float/bool/datetime/Decimal/None); "
                f"got {type(v).__name__}."
            )
    return params


# ---------------------------------------------------------------------------
# "did you mean" error hints
# ---------------------------------------------------------------------------

_TABLE_ERROR = re.compile(r"[Tt]able with name [\"']?([\w/]+)[\"']? does not exist")
_COLUMN_ERROR = re.compile(r'[Cc]olumn "?([\w ]+)"? (?:not found|does not exist)')


def _with_hints(engine: SqlEngine, sql: str, exc: Exception) -> Exception:
    """Attach nearest-name suggestions to table/column resolution errors.

    LLM agents self-correct in one round-trip when the error says what WAS
    available ("did you mean: amount, order_type") instead of three
    blind retries. Best-effort: any failure here returns the original
    exception untouched.
    """
    try:
        import difflib

        msg = str(exc)

        def _suggest(name: str, candidates: list[str]) -> list[str]:
            pool = sorted(set(candidates))
            close = difflib.get_close_matches(name, pool, n=3, cutoff=0.35)
            partial = [c for c in pool if c not in close and name.lower() in c.lower()]
            return (close + partial)[:3]

        m = _TABLE_ERROR.search(msg)
        if m:
            names = [i.qualified_name for i in engine.list_tables()]
            hints = _suggest(m.group(1), names)
            if hints:
                exc = LakehouseError(
                    f"{msg}\nDid you mean one of: {', '.join(hints)}?"
                )
            return exc
        m = _COLUMN_ERROR.search(msg)
        if m:
            # DuckDB >= 1.x already prints "Candidate bindings: ..." for
            # unknown columns — don't duplicate its suggestions.
            if "Candidate bindings" in msg:
                return exc
            columns: list[str] = []
            for info in engine._referenced_tables(sql):
                try:
                    dset = engine._open_dataset(info)
                    columns.extend(f.name for f in dset.schema)
                except Exception:
                    continue
            hints = _suggest(m.group(1), columns)
            if hints:
                exc = LakehouseError(f"{msg}\nDid you mean one of: {', '.join(hints)}?")
            return exc
    except Exception:
        return exc
    return exc


class SqlEngine:
    """Runs SQL + columnar scans over a DataProvider, with in-process caching."""

    def __init__(
        self,
        provider: DataProvider,
        cache_ttl: int = 3600,
        dataset_cache_ttl: int = 3600,
        dataset_cache_tables: int = 8,
        version_check_interval: int = 10,
        list_async_refresh: bool = True,
        cache_dir: str | None = None,
    ):
        self.provider = provider
        self.cache_ttl = cache_ttl
        self.dataset_cache_ttl = dataset_cache_ttl
        self.dataset_cache_tables = dataset_cache_tables
        self.version_check_interval = version_check_interval
        self._tables: list[TableInfo] | None = None
        self._tables_ts: float = 0.0
        # list_tables is served from the cache immediately (never blocks the
        # caller on a slow S3/DFS listing): a stale list is refreshed on a
        # background thread, and a daemon timer re-lists every cache_ttl so
        # the cache stays warm between calls. Disabled when cache_ttl == 0
        # (caching off) or when list_async_refresh is False.
        self._async_list = bool(list_async_refresh) and cache_ttl > 0
        self._list_refreshing = False
        self._describe_cache: dict[str, tuple[float, dict]] = {}
        self._describe_hits = 0
        self._describe_misses = 0
        # Profile results (column statistics) — same TTL discipline as
        # describe, but keyed additionally by the requested column subset:
        # (source, path, columns) -> (ts, profile dict).
        self._profile_cache: dict[tuple, tuple[float, dict]] = {}
        self._profile_hits = 0
        self._profile_misses = 0
        # Reused open Datasets (metadata handles, NOT row data):
        # path -> (ts, dset, version), LRU-bounded.
        self._dataset_cache: OrderedDict[str, tuple[float, object, object | None]] = OrderedDict()
        self._dataset_hits = 0
        self._dataset_misses = 0
        # Delta snapshot-version checks are throttled to this many seconds
        # per table (0 = check on every reuse).
        self._version_checked_at: dict[str, float] = {}
        self._lock = threading.RLock()
        # Semantic catalog (SQLHANDLER_CATALOG): an optional JSON file of
        # human-written table/column descriptions merged into describe /
        # list output so agents see business meaning, not just dtypes.
        # Hot-reloaded on mtime change; a missing/broken file degrades to an
        # empty catalog (never an error).
        self._catalog_path = os.environ.get("SQLHANDLER_CATALOG", "").strip() or None
        self._catalog_mtime: float | None = None
        self._catalog_data: dict = {}
        # Query memory: recent query outcomes for the query-memory MCP
        # resource (self-improving loop — agents reuse proven patterns).
        self._query_memory: deque = deque(maxlen=_query_memory_size())
        # Per-table usage counts (source, path) -> accesses through
        # _open_dataset. Powers usage-driven prewarm: when no explicit
        # SQLHANDLER_PREWARM_TABLES is configured, the busiest tables from
        # the previous run (persisted in the disk-warm cache) are warmed
        # instead — the server teaches itself what to pre-warm.
        self._table_usage: dict[tuple[str, str], int] = {}
        # Opens since the last disk-cache write; note_query flushes the
        # usage counts to disk every _USAGE_SAVE_EVERY opens so a restart
        # prewarms from fresh popularity without IO per query.
        self._unsaved_opens = 0
        # Disk-warm layer (SQLHANDLER_CACHE_DIR, k8s: mount an emptyDir there):
        # describe/table-list results are persisted after each fill and
        # reloaded at startup, so a container restart (OOMKill, node drain)
        # no longer costs a full cold metadata fetch. The engine's own caches
        # stay in-process and TTL-driven; disk entries are re-validated
        # against the wall-clock TTL at load, and never outlive the same
        # cache_ttl the memory layer uses.
        env_cache_dir = os.environ.get("SQLHANDLER_CACHE_DIR", "").strip()
        self._cache_dir = cache_dir or (env_cache_dir or None)
        if self._cache_dir and self.cache_ttl > 0:
            self._load_cache_from_disk()
        if self._async_list:
            threading.Thread(
                target=self._auto_refresh_loop,
                daemon=True,
                name="sqlhandler-list-autorefresh",
            ).start()

    # ---------------------------------------------------------------- list
    def list_tables(self) -> list[TableInfo]:
        """List the tables the provider exposes (cached for cache_ttl).

        With async refresh (the default) the cached list is returned
        immediately - even a stale one - and the list is re-fetched on a
        background thread so callers never block on the slow S3/DFS listing.
        A daemon timer also refreshes the list every ``cache_ttl`` seconds, so
        it is kept fresh automatically between calls. When caching is disabled
        (``cache_ttl == 0``) or ``list_async_refresh`` is off, every call
        re-lists synchronously, preserving the original behavior.
        """
        now = time.monotonic()
        with self._lock:
            have = self._tables is not None
            fresh = have and (now - self._tables_ts < self.cache_ttl)
        if have and (fresh or self._async_list):
            if not fresh:  # async_list must be enabled here
                self._maybe_refresh_async()  # serve stale, refresh in background
            return self._tables
        # No cache yet, or caching disabled: fill synchronously so callers
        # always get a current result.
        tables = self.provider.list_tables()
        with self._lock:
            self._tables = tables
            self._tables_ts = time.monotonic()
            self._list_refreshing = False
        self._save_cache_to_disk()
        return tables

    def _maybe_refresh_async(self) -> None:
        """Start a background list refresh (no-op when one is in flight)."""
        with self._lock:
            if self._list_refreshing:
                return
            self._list_refreshing = True
        try:
            threading.Thread(
                target=self._refresh_list_worker,
                daemon=True,
                name="sqlhandler-list-refresh",
            ).start()
        except Exception:
            with self._lock:
                self._list_refreshing = False
            raise

    def _refresh_list_worker(self) -> None:
        """Re-run provider.list_tables(); fills the cache, never raises."""
        try:
            tables = self.provider.list_tables()
        except Exception:
            logger.exception("background list-tables refresh failed")
            with self._lock:
                self._list_refreshing = False
            return
        with self._lock:
            self._tables = tables
            self._tables_ts = time.monotonic()
            self._list_refreshing = False
        self._save_cache_to_disk()

    def _auto_refresh_loop(self) -> None:
        """Keep the table list warm: refresh in the background every TTL."""
        while True:
            time.sleep(max(self.cache_ttl, 1))
            try:
                self._maybe_refresh_async()
            except Exception:
                logger.debug("auto list refresh skipped", exc_info=True)

    # ------------------------------------------------------- disk warm layer
    def _cache_file(self) -> Path:
        return Path(self._cache_dir) / "metadata-cache.json"

    def _load_cache_from_disk(self) -> None:
        """Reload describe/table-list results persisted by a previous run.

        Every entry carries a wall-clock epoch and is only accepted when
        younger than ``cache_ttl`` — the same lifetime the memory layer uses,
        so a warm entry is never *more* stale than an in-process one. Loaded
        entries get fresh monotonic timestamps (the in-process TTL restarts),
        and each step is best-effort: a missing, corrupt or unwritable file
        just means a cold start, exactly as before.
        """
        try:
            path = self._cache_file()
            if not path.exists():
                return
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            now = time.time()
            tables = data.get("tables")
            if isinstance(tables, dict) and now - float(tables.get("ts", 0)) < self.cache_ttl:
                items = tables.get("items")
                if isinstance(items, list) and items:
                    infos = [TableInfo(**item) for item in items if isinstance(item, dict)]
                    if infos:
                        self._tables = infos
                        self._tables_ts = time.monotonic()
                        logger.info("warm-started table list from %s (%d tables)", path, len(infos))
            describes = data.get("describes")
            if isinstance(describes, list):
                loaded = 0
                for entry in describes:
                    if not isinstance(entry, dict):
                        continue
                    if now - float(entry.get("ts", 0)) >= self.cache_ttl:
                        continue
                    key = (str(entry.get("source", "default")), str(entry.get("path", "")))
                    result = entry.get("result")
                    if isinstance(result, dict):
                        self._describe_cache[key] = (time.monotonic(), result)
                        loaded += 1
                if loaded:
                    logger.info("warm-started %d describe result(s) from %s", loaded, path)
            # Usage counts restore without a TTL (they are a popularity
            # prior for the next prewarm, not a freshness-sensitive value).
            usage = data.get("usage")
            if isinstance(usage, list):
                restored = 0
                for row in usage:
                    if isinstance(row, list) and len(row) == 3:
                        try:
                            key = (str(row[0]), str(row[1]))
                            self._table_usage[key] = max(
                                self._table_usage.get(key, 0), int(row[2])
                            )
                            restored += 1
                        except (TypeError, ValueError):
                            continue
                if restored:
                    logger.info("warm-started usage counts for %d table(s)", restored)
        except Exception:
            logger.debug("disk cache warm-start skipped", exc_info=True)

    def _save_cache_to_disk(self) -> None:
        """Persist the current caches for the next process start (best-effort).

        Called right after a synchronous cache fill, so the wall-clock epoch
        written here is the fill time; entries already on disk get their
        epoch refreshed, which at most extends one entry's disk lifetime by a
        single TTL — the memory layer's own TTL still governs correctness.
        """
        if not self._cache_dir or self.cache_ttl <= 0:
            return
        try:
            import json

            with self._lock:
                payload = {
                    "tables": {
                        "ts": time.time(),
                        "items": [asdict(info) for info in self._tables or []],
                    },
                    "describes": [
                        {
                            "source": source,
                            "path": path,
                            "ts": time.time(),
                            "result": result,
                        }
                        for (source, path), (_, result) in self._describe_cache.items()
                    ],
                    "usage": [
                        [source, path, count]
                        for (source, path), count in self._table_usage.items()
                    ],
                }
            cache_dir = Path(self._cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._cache_file())
        except Exception:
            logger.debug("disk cache save skipped", exc_info=True)

    # ------------------------------------------------------------- catalog
    def _catalog(self) -> dict:
        """Return the semantic catalog's ``tables`` mapping (hot-reloaded).

        The file is re-read whenever its mtime changes, so editing the
        catalog (hand-written or seeded by auto-profiling) takes effect
        without a restart. Any problem reading/parsing it logs a warning
        and yields an empty catalog — a broken catalog must never break
        queries.
        """
        path = self._catalog_path
        if not path:
            return {}
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return {}
        if mtime != self._catalog_mtime:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                tables = data.get("tables") if isinstance(data, dict) else None
                self._catalog_data = tables if isinstance(tables, dict) else {}
                logger.info(
                    "semantic catalog loaded: %d table entr%s from %s",
                    len(self._catalog_data),
                    "y" if len(self._catalog_data) == 1 else "ies",
                    path,
                )
            except Exception:
                logger.warning("semantic catalog %s unreadable; ignoring it", path, exc_info=True)
                self._catalog_data = {}
            self._catalog_mtime = mtime
            # Catalog documentation is merged INTO cached describe results,
            # so a catalog edit must invalidate them or the old wording
            # would keep being served until the TTL expires.
            with self._lock:
                self._describe_cache.clear()
        return self._catalog_data

    def _catalog_for(self, info: TableInfo) -> dict:
        """The catalog entry for a table, matched by path/qualified/bare name."""
        catalog = self._catalog()
        if not catalog:
            return {}
        for key in (info.path, info.qualified_name, info.name):
            entry = catalog.get(key)
            if isinstance(entry, dict):
                return entry
        return {}

    def table_description(self, info: TableInfo) -> str:
        """Human description for a table from the semantic catalog ('' if none)."""
        return str(self._catalog_for(info).get("description") or "")

    # ------------------------------------------------------------- resolve
    def _resolve(self, table: str) -> TableInfo:
        """Resolve a bare name (or schema/name) to a TableInfo.

        Matches the discovered tables first so the physical ``location`` is
        preserved: a table's logical ``schema/name`` can differ from its
        object-store folder when the folder is nested more than one level
        deep (e.g. ``finance/b/c`` -> schema ``b``, name ``c``). Falling back
        to constructing a fresh TableInfo would drop ``location`` and break
        opening the dataset.
        """
        _safe_table_name(table)
        for info in self.list_tables():
            if info.path == table or info.name == table or info.qualified_name == table:
                return info
        if "/" in table:
            schema, name = table.split("/", 1)
            if schema and name:
                return TableInfo(name=name, schema=schema)
        raise LakehouseError(f"Table '{table}' not found in data source")

    # ------------------------------------------------------------ datasets
    def _safe_version(self, info: TableInfo):
        """Run the provider's cheap version check; never raise."""
        try:
            return self.provider.check_version(info)
        except Exception:
            return None

    def _open_dataset(self, info: TableInfo, version: int | None = None):
        """Return a pyarrow Dataset for info, reusing a cached one when fresh.

        For versionable sources (Delta Lake) the cached handle is invalidated
        when the source's snapshot version changes, so new ETL commits are
        visible without waiting out the dataset cache TTL. The version check is
        throttled to the configured interval per table (0 = every reuse) to
        keep the per-query cost negligible. Non-versioned sources (plain
        Parquet/S3/Iceberg) reuse until the TTL as before.

        ``version`` (time travel) pins a HISTORICAL snapshot: the cache key
        includes it and freshness checks are skipped entirely (a historical
        snapshot never changes — it stays valid until the TTL evicts it).
        """
        # Source-aware cache key so same-named tables in federated sources
        # never share a dataset handle; the requested snapshot version is
        # part of the key so historical reads never alias the live dataset.
        key = (info.source, info.path, version)
        now = time.monotonic()
        hit = None
        with self._lock:
            cand = self._dataset_cache.get(key)
            if cand is not None and now - cand[0] < self.dataset_cache_ttl:
                hit = cand
        if hit is not None and version is not None:
            # Historical snapshot: immutable, serve from cache until TTL.
            with self._lock:
                self._dataset_cache.move_to_end(key)
                self._dataset_hits += 1
            return hit[1]
        if hit is not None and version is None:
            _, cached_dset, cached_ver = hit
            if cached_ver is None:
                with self._lock:
                    self._dataset_cache.move_to_end(key)
                    self._dataset_hits += 1
                return cached_dset
            with self._lock:
                last = self._version_checked_at.get(key, 0.0)
            if self.version_check_interval > 0 and now - last < self.version_check_interval:
                with self._lock:
                    self._dataset_cache.move_to_end(key)
                    self._dataset_hits += 1
                return cached_dset
            current = self._safe_version(info)
            with self._lock:
                self._version_checked_at[key] = time.monotonic()
                if current == cached_ver:
                    self._dataset_cache.move_to_end(key)
                    self._dataset_hits += 1
                    return cached_dset
        dset = self.provider.open_dataset(info, version)
        version = None if version is not None else self._safe_version(info)
        # One access = one bump, whether the open was fresh or revalidated.
        with self._lock:
            self._table_usage[(info.source, info.path)] = (
                self._table_usage.get((info.source, info.path), 0) + 1
            )
            self._unsaved_opens += 1
        with self._lock:
            self._dataset_misses += 1
            self._version_checked_at[key] = time.monotonic()
            if self.dataset_cache_ttl > 0 and self.dataset_cache_tables > 0:
                self._dataset_cache[key] = (time.monotonic(), dset, version)
                self._dataset_cache.move_to_end(key)
                while len(self._dataset_cache) > self.dataset_cache_tables:
                    self._dataset_cache.popitem(last=False)
        return dset

    # ------------------------------------------------------------ describe
    def describe_table(self, table: str) -> dict:
        """Return column names/types and the canonical URI for a table.

        Cached in-process for cache_ttl seconds keyed by the resolved table
        path, so frequently-described tables come from memory instead of
        re-opening the metadata on every agent call.
        """
        info = self._resolve(table)
        key = (info.source, info.path)
        now = time.monotonic()
        # Poll the catalog's mtime BEFORE the cache lookup: catalog
        # documentation is merged into cached describe results, so an edited
        # catalog must invalidate them (see _catalog()). One stat() per call.
        if self._catalog_path:
            self._catalog()
        with self._lock:
            hit = self._describe_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._describe_hits += 1
                return hit[1]
        dset = self._open_dataset(info)
        schema = dset.schema
        result = {
            "table": table,
            "uri": self.provider.table_uri(info),
            "columns": [{"name": f.name, "type": str(f.type)} for f in schema],
            "n_columns": len(schema),
        }
        # Merge semantic-catalog documentation when present: a table-level
        # description plus per-column notes. LLMs write far better SQL with
        # the business meaning attached, and the catalog is optional — no
        # entry means the output is exactly as before.
        entry = self._catalog_for(info)
        if entry:
            if entry.get("description"):
                result["description"] = str(entry["description"])
            if entry.get("aliases"):
                result["aliases"] = [str(a) for a in entry["aliases"]][:8]
            col_docs = entry.get("columns")
            if isinstance(col_docs, dict):
                for col in result["columns"]:
                    doc = col_docs.get(col["name"])
                    if doc:
                        col["description"] = str(doc)
        with self._lock:
            self._describe_misses += 1
            if self.cache_ttl > 0:
                self._describe_cache[key] = (time.monotonic(), result)
        self._save_cache_to_disk()
        return result

    # ------------------------------------------------------------- search
    def search_tables(self, query: str, limit: int = 20) -> list[dict]:
        """Find tables matching a free-text query (names, columns, catalog docs).

        Deliberately cheap: matches against the cached table list, the
        semantic catalog (descriptions/aliases/column docs) and any already
        cached describe results — it never triggers a schema fetch per
        table, so it stays usable when ``list_tables`` would flood a model's
        context with hundreds of entries.

        Returns a list of matches, best first:
          ``{"table", "name", "qualified_name", "format", "source",
             "description", "matched_columns", "score"}``
        """
        q = query.strip().lower()
        if not q:
            return []
        terms = [t for t in re.split(r"[^a-z0-9_]+", q) if t]
        results: list[dict] = []
        with self._lock:
            cached_describes = {k: v for k, v in self._describe_cache.items()}
        for info in self.list_tables():
            entry = self._catalog_for(info)
            desc = str(entry.get("description") or "")
            aliases = [str(a) for a in entry.get("aliases") or []]
            col_docs = entry.get("columns") if isinstance(entry.get("columns"), dict) else {}
            # Column names from an already-cached describe (never a fetch).
            cached = cached_describes.get((info.source, info.path))
            col_names = [c["name"] for c in cached[1].get("columns", [])] if cached else []

            hay_name = f"{info.source}/{info.path}".lower()
            hay_docs = " ".join([desc, *aliases, *map(str, col_docs.values())]).lower()
            # Column index: name -> doc text (doc may be empty for cache-only
            # columns). A column "matches" when a term hits its name OR its
            # catalog documentation.
            col_index = {**{c: "" for c in col_names}, **col_docs}
            score = 0
            matched_cols: list[str] = []
            if info.name.lower() == q or info.path.lower() == q or info.qualified_name.lower() == q:
                score += 100
            elif q in hay_name:
                score += 80
            if any(q in a.lower() for a in aliases):
                score += 60
            if terms:
                if any(t in hay_name for t in terms):
                    score += 40
                if any(t in hay_docs for t in terms):
                    score += 30
                matched_cols = [
                    c
                    for c, doc in col_index.items()
                    if any(t in c.lower() or t in str(doc).lower() for t in terms)
                ]
                if matched_cols:
                    score += 30
            elif q:
                matched_cols = [c for c, doc in col_index.items() if q in c.lower() or q in str(doc).lower()]
                if matched_cols:
                    score += 30
            if score <= 0:
                continue
            results.append(
                {
                    "table": info.path,
                    "name": info.name,
                    "qualified_name": info.qualified_name,
                    "format": info.format,
                    "source": info.source,
                    "description": desc,
                    "matched_columns": matched_cols[:10],
                    "score": score,
                }
            )
        results.sort(key=lambda r: (-r["score"], r["table"]))
        return results[: max(limit, 0)]

    # ------------------------------------------------------------- profile
    def profile_table(self, table: str, columns: Sequence[str] | None = None) -> dict:
        """Column-level statistics for a table (cached like describe_table).

        Runs DuckDB's ``SUMMARIZE`` over the table's registered Dataset, so
        the same storage path (pyarrow scan) does the IO and the container
        memory budget applies. Returns, per column: min/max, approx distinct
        count, null percentage, avg/std and the q25/q50/q75 quantiles —
        exactly what an agent needs to write correct filters without
        trial-and-error queries. The full-table row count comes from the
        Parquet/Delta metadata (cheap) and is reported separately from the
        number of rows actually summarized (bounded by
        ``SQLHANDLER_PROFILE_MAX_ROWS``).

        Args:
            table: table name (``schema/name`` when the source uses schemas).
            columns: optional subset of columns to profile (default: all).
        """
        info = self._resolve(table)
        col_key = tuple(columns) if columns else ()
        key = (info.source, info.path, col_key)
        now = time.monotonic()
        with self._lock:
            hit = self._profile_cache.get(key)
            if hit is not None and now - hit[0] < self.cache_ttl:
                self._profile_hits += 1
                return hit[1]

        dset = self._open_dataset(info)

        # Full-table row count from metadata (Parquet row-group counts /
        # Delta log stats) — no data IO for well-formed files.
        try:
            n_rows: int | None = int(dset.count_rows())
        except Exception:
            n_rows = None

        import duckdb

        cap = _profile_max_rows()
        con = duckdb.connect()
        try:
            _duckdb_fs_lockdown(con)
            _apply_memory_budget(con)
            view = "_sqlhandler_profile_target"
            con.register(view, dset)
            col_sel = ", ".join(_safe_ident(c) for c in columns) if columns else "*"
            inner = f"SELECT {col_sel} FROM {view}"
            if cap > 0:
                inner = f"SELECT * FROM ({inner}) LIMIT {cap}"
            profiled = con.sql(f"SELECT count(*) FROM ({inner})").fetchone()
            profiled_rows = int(profiled[0]) if profiled else 0
            summary = con.sql(f"SUMMARIZE {inner}").arrow()
            if isinstance(summary, pa.RecordBatchReader):
                summary = summary.read_all()
        except Exception as exc:
            raise LakehouseError(f"Profiling failed for table '{table}': {exc}") from exc
        finally:
            con.close()

        result = {
            "table": table,
            "uri": self.provider.table_uri(info),
            "n_rows": n_rows,
            "profiled_rows": profiled_rows,
            "profile_max_rows": cap,
            "n_columns": len(summary),
            "columns": [
                {
                    "name": str(r.get("column_name", "")),
                    "type": str(r.get("column_type", "")),
                    "min": r.get("min"),
                    "max": r.get("max"),
                    "approx_unique": r.get("approx_unique"),
                    "null_pct": r.get("null_percentage"),
                    "avg": r.get("avg"),
                    "std": r.get("std"),
                    "q25": r.get("q25"),
                    "q50": r.get("q50"),
                    "q75": r.get("q75"),
                    "non_null": r.get("count"),
                }
                for r in summary.to_pylist()
            ],
        }
        with self._lock:
            self._profile_misses += 1
            if self.cache_ttl > 0:
                self._profile_cache[key] = (time.monotonic(), result)
        return result

    def usage_top_tables(self, n: int = 5) -> tuple[str, ...]:
        """The n most-accessed tables this/past run (for usage-driven prewarm).

        Read counts are accumulated in-memory and persisted with the disk
        warm cache, so a restarted server warms what the previous one
        actually served — no hand-maintained SQLHANDLER_PREWARM_TABLES.
        """
        with self._lock:
            ranked = sorted(self._table_usage.items(), key=lambda kv: -kv[1])
        return tuple(path for (_, path), _ in ranked[: max(n, 0)])

    # -------------------------------------------------------- query memory
    def note_query(self, sql: str, duration_ms: float, n_rows: int | None, error: str | None = None) -> None:
        """Record one query outcome for the query-memory resource (best-effort).

        SQL text is truncated; failures are recorded too so agents can see
        what NOT to repeat.
        """
        if self._query_memory.maxlen and self._unsaved_opens >= _USAGE_SAVE_EVERY:
            self._unsaved_opens = 0
            self._save_cache_to_disk()
        if not self._query_memory.maxlen:
            return
        with self._lock:
            self._query_memory.append(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                    "sql": sql[:500],
                    "duration_ms": round(duration_ms, 1),
                    "n_rows": n_rows,
                    "error": error[:200] if error else None,
                }
            )

    def query_memory(self) -> list[dict]:
        """Recent query outcomes, oldest first (a snapshot copy)."""
        with self._lock:
            return list(self._query_memory)

    def _record_outcome(self, sql: str, duration_ms: float | None, n_rows: int | None, state: str, error: str | None = None) -> None:
        """Single outcome choke point: query memory + metrics + audit log.

        Called by QueryJob for every finished query (ok, error, cancelled);
        each consumer is individually best-effort so observability can never
        break a query.
        """
        self.note_query(sql, duration_ms or 0.0, n_rows, error)
        observability.metrics.record_query(
            state, (duration_ms or 0.0) / 1000.0, n_rows
        )
        observability.audit_query(sql, state, duration_ms, n_rows, error)

    # ------------------------------------------------------------- scans
    def scan_arrow(
        self,
        table: str,
        columns: Sequence[str] | None = None,
        filters: Sequence | None = None,
        limit: int | None = None,
        version_as_of: int | None = None,
    ) -> pa.Table:
        """Read a table as an in-memory Arrow table.

        filters may be a single pyarrow.compute expression or a list of
        expressions (AND-ed). Column projection and predicates push down.

        ``version_as_of`` reads a historical snapshot (Delta version or
        Iceberg snapshot id) instead of the current one.
        """
        info = self._resolve(table)
        _validate_snapshot_version(version_as_of, "Time travel") if version_as_of is not None else None
        dset = self._open_dataset(info, version_as_of)

        expr = None
        for f in filters or []:
            expr = f if expr is None else (expr & f)

        scan = dset.scanner(
            columns=list(columns) if columns else None,
            filter=expr,
            batch_size=65536,
        )
        if limit is not None and limit >= 0:
            # head() stops the scan early; slicing after to_table() would
            # first load the ENTIRE table into memory.
            return scan.head(limit)
        return scan.to_table()

    # ------------------------------------------------------------ duckdb
    def query_duckdb(
        self,
        sql: str,
        limit: int | None = None,
        params: object | None = None,
        version_as_of: int | None = None,
        row_cap: int | None = None,
    ) -> pa.Table:
        """Execute SQL via DuckDB over the provider Datasets.

        Each referenced table is registered as a DuckDB view backed by its
        pyarrow Dataset; DuckDB pushes predicates/projections into the scan.
        The connection runs with DuckDB's own filesystem access disabled
        (see :func:`_duckdb_fs_lockdown`): all storage IO is pyarrow's, and
        DuckDB-side local-file reads / URL fetches / COPY are refused by
        default (``SQLHANDLER_DUCKDB_FILE_ACCESS=1`` opts out).

        Args:
            sql: the SELECT (or WITH ...) statement to run.
            limit: optional row cap applied inside the query.
            params: optional bind parameters — a dict for named ``$name``
                placeholders or a list for positional ``?`` ones. Values must
                be scalars (see :func:`_validate_params`). Bind parameters
                keep client-side query templates injection-safe.
            version_as_of: optional historical snapshot applied to every
                versionable table the query touches (Delta snapshot version
                or Iceberg snapshot id). Plain-Parquet tables in the same
                query are an error (they have no history).
        """
        if version_as_of is not None:
            _validate_snapshot_version(version_as_of, "Time travel")
        timeout = _query_timeout()
        job = QueryJob(
            self,
            sql,
            limit=limit,
            params=params,
            version_as_of=version_as_of,
            row_cap=row_cap,
        )
        if timeout > 0:
            job.wait(timeout)
            if job.state == "running":
                # Interrupt inside DuckDB so the worker thread unwinds and
                # the connection is closed instead of leaking.
                job.cancel()
                job.wait(_CANCEL_GRACE_SECONDS)
                raise LakehouseError(
                    f"Query timed out after {timeout}s (SQLHANDLER_QUERY_TIMEOUT) and was cancelled."
                )
        else:
            job.wait()
        return job.result

    def _referenced_tables(self, sql: str) -> list[TableInfo]:
        """Return the tables referenced by a SQL query.

        Matches table identifiers (bare or qualified) against the known
        tables, so we only open the datasets the query actually touches.
        """
        known = self.list_tables()
        wanted: dict[tuple[str, str], TableInfo] = {}
        for info in known:
            for ident in (re.escape(info.name), re.escape(info.qualified_name)):
                if re.search(rf"\b{ident}\b", sql):
                    wanted[(info.source, info.path)] = info
                    break
        return list(wanted.values())

    def _register_schema(self, con, sql: str, version: int | None = None) -> None:
        """Register each referenced table as a DuckDB view over its Dataset.

        Each table gets its qualified view (``[source_]schema_name``) always,
        and its bare-name view only when that name is globally unique - so
        same-named tables across federated sources (or schemas) can't silently
        shadow each other. Queries should prefer qualified names.

        ``version`` (time travel) opens every versionable table at that
        historical snapshot instead of the current one.
        """
        name_counts: dict[str, int] = {}
        for info in self.list_tables():
            name_counts[info.name] = name_counts.get(info.name, 0) + 1
        for info in self._referenced_tables(sql):
            views = {_safe_ident(info.qualified_name)}
            if name_counts.get(info.name, 0) <= 1:
                views.add(_safe_ident(info.name))
            try:
                dset = self._open_dataset(info, version)
            except Exception:
                if version is not None:
                    # A time-travel query must fail loudly: silently skipping
                    # the table would produce a misleading "table not found"
                    # instead of the real reason (e.g. plain Parquet has no
                    # version history).
                    raise
                logger.debug("Could not open dataset for %s", info.path)
                continue
            for view in views:
                try:
                    con.register(view, dset)
                except Exception:
                    logger.debug("Could not register view %s from %s", view, info.path)

    # -------------------------------------------------------------- help
    def prewarm(self, tables: Sequence[str]) -> dict[str, str]:
        """Fill the describe cache for tables; return per-table outcome.

        Failures are recorded per table and never raised (a table may be
        temporarily unavailable or renamed).
        """
        outcomes: dict[str, str] = {}
        for name in tables:
            try:
                self.describe_table(name)
                outcomes[name] = "ok"
            except Exception as exc:
                outcomes[name] = f"error: {exc}"
                logger.warning("prewarm describe %s failed: %s", name, exc)
        return outcomes

    def cache_stats(self) -> dict:
        """Small in-memory snapshot of the metadata caches (for observability)."""
        with self._lock:
            return {
                "describe_cached_tables": len(self._describe_cache),
                "describe_hits": self._describe_hits,
                "describe_misses": self._describe_misses,
                "profile_cached_tables": len(self._profile_cache),
                "profile_hits": self._profile_hits,
                "profile_misses": self._profile_misses,
                "dataset_cached_tables": len(self._dataset_cache),
                "dataset_hits": self._dataset_hits,
                "dataset_misses": self._dataset_misses,
                "tables_cached": self._tables is not None,
                "tables_cached_age_s": round((time.monotonic() - self._tables_ts), 1)
                if self._tables is not None
                else None,
                "list_async_refresh": self._async_list,
                "list_refreshing": self._list_refreshing,
                "cache_ttl": self.cache_ttl,
                "dataset_cache_ttl": self.dataset_cache_ttl,
                # Resource budget derived from the container's cgroup limits,
                # plus the process's current RSS — so an OOM-bound pod (RSS
                # climbing toward the limit) is visible before the kernel
                # kills it and wipes every cache.
                "container_memory_bytes": resources.container_memory_bytes(),
                "container_cpu_count": resources.container_cpu_count(),
                "process_rss_bytes": resources.process_rss_bytes(),
            }
