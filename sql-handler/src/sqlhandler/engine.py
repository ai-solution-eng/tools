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

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence

import pyarrow as pa

from .provider import DataProvider, LakehouseError, TableInfo

logger = logging.getLogger("sqlhandler.engine")


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


def _safe_ident(name: str) -> str:
    """Quote an identifier for DuckDB if it contains special characters."""
    if name.replace("_", "").isalnum():
        return name
    return '"' + name.replace('"', '""') + '"'


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
        # Reused open Datasets (metadata handles, NOT row data):
        # path -> (ts, dset, version), LRU-bounded.
        self._dataset_cache: OrderedDict[str, tuple[float, object, object | None]] = OrderedDict()
        self._dataset_hits = 0
        self._dataset_misses = 0
        # Delta snapshot-version checks are throttled to this many seconds
        # per table (0 = check on every reuse).
        self._version_checked_at: dict[str, float] = {}
        self._lock = threading.RLock()
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

    def _auto_refresh_loop(self) -> None:
        """Keep the table list warm: refresh in the background every TTL."""
        while True:
            time.sleep(max(self.cache_ttl, 1))
            try:
                self._maybe_refresh_async()
            except Exception:
                logger.debug("auto list refresh skipped", exc_info=True)

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

    def _open_dataset(self, info: TableInfo):
        """Return a pyarrow Dataset for info, reusing a cached one when fresh.

        For versionable sources (Delta Lake) the cached handle is invalidated
        when the source's snapshot version changes, so new ETL commits are
        visible without waiting out the dataset cache TTL. The version check is
        throttled to the configured interval per table (0 = every reuse) to
        keep the per-query cost negligible. Non-versioned sources (plain
        Parquet/S3/Iceberg) reuse until the TTL as before.
        """
        # Source-aware cache key so same-named tables in federated sources
        # never share a dataset handle.
        key = (info.source, info.path)
        now = time.monotonic()
        hit = None
        with self._lock:
            cand = self._dataset_cache.get(key)
            if cand is not None and now - cand[0] < self.dataset_cache_ttl:
                hit = cand
        if hit is not None:
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
        dset = self.provider.open_dataset(info)
        version = self._safe_version(info)
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
        with self._lock:
            self._describe_misses += 1
            if self.cache_ttl > 0:
                self._describe_cache[key] = (time.monotonic(), result)
        return result

    # ------------------------------------------------------------- scans
    def scan_arrow(
        self,
        table: str,
        columns: Sequence[str] | None = None,
        filters: Sequence | None = None,
        limit: int | None = None,
    ) -> pa.Table:
        """Read a table as an in-memory Arrow table.

        filters may be a single pyarrow.compute expression or a list of
        expressions (AND-ed). Column projection and predicates push down.
        """
        info = self._resolve(table)
        dset = self._open_dataset(info)

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
    def query_duckdb(self, sql: str, limit: int | None = None) -> pa.Table:
        """Execute SQL via DuckDB over the provider Datasets.

        Each referenced table is registered as a DuckDB view backed by its
        pyarrow Dataset; DuckDB pushes predicates/projections into the scan.
        """
        import duckdb

        con = duckdb.connect()
        try:
            self._register_schema(con, sql)
            rel = con.sql(sql)
            # Enforce a row cap inside the query (LIMIT pushdown) so an
            # unbounded SELECT cannot materialize millions of rows in memory.
            cap = _max_rows()
            eff = limit if (limit is not None and limit >= 0) else None
            if cap > 0:
                eff = min(eff, cap) if eff is not None else cap
            if eff is not None:
                rel = rel.limit(eff)
            arrow_table = rel.arrow()
            # DuckDB returns a RecordBatchReader; materialize for slicing.
            if isinstance(arrow_table, pa.RecordBatchReader):
                arrow_table = arrow_table.read_all()
        except Exception as exc:
            raise LakehouseError(f"DuckDB query failed: {exc}") from exc
        finally:
            con.close()

        if limit is not None and limit >= 0 and arrow_table.num_rows > limit:
            arrow_table = arrow_table.slice(0, limit)
        return arrow_table

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

    def _register_schema(self, con, sql: str) -> None:
        """Register each referenced table as a DuckDB view over its Dataset.

        Each table gets its qualified view (``[source_]schema_name``) always,
        and its bare-name view only when that name is globally unique - so
        same-named tables across federated sources (or schemas) can't silently
        shadow each other. Queries should prefer qualified names.
        """
        name_counts: dict[str, int] = {}
        for info in self.list_tables():
            name_counts[info.name] = name_counts.get(info.name, 0) + 1
        for info in self._referenced_tables(sql):
            views = {_safe_ident(info.qualified_name)}
            if name_counts.get(info.name, 0) <= 1:
                views.add(_safe_ident(info.name))
            try:
                dset = self._open_dataset(info)
            except Exception:
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
            }
