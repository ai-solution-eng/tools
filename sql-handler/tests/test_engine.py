"""Unit tests for the shared SQL engine (caches + scans + SQL), no network.

Exercises SqlEngine + a tiny in-memory/temp-dir DataProvider: parquet files
on the local filesystem stand in for the source, so DuckDB + pyarrow run
for real while nothing touches the network.
"""

import threading
import time

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler.engine import SqlEngine
from sqlhandler.provider import LakehouseError, TableInfo

TABLES = [
    TableInfo(name="work_order", schema="workorder", format="parquet"),
    TableInfo(name="work_order_note", schema="workorder", format="parquet"),
]


class FakeProvider:
    """A DataProvider backed by a local temp directory of Parquet files."""

    kind = "fake"

    def __init__(self, root):
        self.root = root
        self.list_calls = 0
        self.open_calls: list[str] = []

    def list_tables(self):
        self.list_calls += 1
        return TABLES

    def table_uri(self, info):
        return f"fake://{info.path}"

    def open_dataset(self, info):
        self.open_calls.append(info.path)
        d = self.root / info.path
        if (d / "part.parquet").exists():
            return pad.dataset(str(d), format="parquet")
        # Arbitrary tables in the cache tests: serve a tiny in-memory dataset.
        return pad.dataset(pa.table({"id": [1], "x": ["a"]}))


def _write(root, rel, table):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, d / "part.parquet")


def _make_engine(tmp_path, **kw):
    _write(
        tmp_path,
        "workorder/work_order",
        pa.table(
            {
                "id": [1, 2, 3],
                "amount": [10.5, 20.0, 30.5],
                "kind": ["a", "b", "a"],
            }
        ),
    )
    _write(
        tmp_path,
        "workorder/work_order_note",
        pa.table(
            {
                "note_id": [10, 20],
                "text": ["hi", "bye"],
            }
        ),
    )
    provider = FakeProvider(tmp_path)
    return SqlEngine(provider, **kw), provider


# ---------------------------------------------------------------------------
# list_tables cache
# ---------------------------------------------------------------------------


def test_list_tables_cached_within_ttl(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600)
    t1 = eng.list_tables()
    t2 = eng.list_tables()
    assert t1 == t2
    assert provider.list_calls == 1  # second call served from cache


def test_list_tables_not_cached_when_disabled(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=0)
    eng.list_tables()
    eng.list_tables()
    assert provider.list_calls == 2


class BlockingFakeProvider(FakeProvider):
    """FakeProvider whose list_tables can be made to block on demand."""

    def __init__(self, root):
        super().__init__(root)
        self._hold = threading.Event()
        self._hold.set()  # set = allow listing; clear = block it

    def release(self):
        self._hold.set()

    def list_tables(self):
        if not self._hold.is_set():
            self._hold.wait()  # block until released
        return super().list_tables()


def _wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_list_tables_serves_stale_then_async_refresh(tmp_path):
    # A stale list is returned immediately, and a background thread refreshes it.
    provider = BlockingFakeProvider(tmp_path)
    eng = SqlEngine(provider, cache_ttl=3600)
    assert eng.list_tables() == TABLES
    assert provider.list_calls == 1

    provider._hold.clear()  # make the next listing block
    eng._tables_ts = 0.0  # age the cache past the TTL
    t0 = time.monotonic()
    result = eng.list_tables()
    dt = time.monotonic() - t0
    assert result == TABLES  # served from cache
    assert dt < 1.0  # did NOT wait on the blocked listing
    assert provider.list_calls == 1  # listing not re-done on the call itself

    provider.release()  # release the background refresh
    assert _wait_until(lambda: provider.list_calls == 2)
    assert _wait_until(lambda: eng._tables_ts > 0.0)
    assert eng.cache_stats()["list_refreshing"] is False


def test_list_tables_async_disabled_blocks_on_stale(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, list_async_refresh=False)
    eng.list_tables()
    assert provider.list_calls == 1
    assert eng.cache_stats()["list_async_refresh"] is False

    eng._tables_ts = 0.0
    assert eng.list_tables() == TABLES
    assert provider.list_calls == 2  # synchronous refresh on the call


def test_list_tables_auto_refresh_without_calls(tmp_path):
    # The daemon timer re-lists every cache_ttl even with no callers.
    eng, provider = _make_engine(tmp_path, cache_ttl=1)
    eng.list_tables()
    assert provider.list_calls == 1
    assert _wait_until(lambda: provider.list_calls >= 2, timeout=5)


def test_resolve_preserves_location_for_deep_schema(tmp_path):
    # A table whose folder is deeper than its logical schema/name (e.g.
    # finance/b/c -> schema b, name c) must keep its physical location when
    # resolved by schema/name or qualified name, or the dataset can't be
    # opened (open_dataset falls back to info.path otherwise).
    provider = FakeProvider(tmp_path)
    provider.list_tables = lambda: [
        TableInfo(name="c", schema="b", format="parquet", location="finance/b/c"),
        *TABLES,
    ]
    eng = SqlEngine(provider, cache_ttl=3600, list_async_refresh=False)
    assert eng._resolve("b/c").location == "finance/b/c"  # schema/name path
    assert eng._resolve("b_c").location == "finance/b/c"  # qualified name
    assert eng._resolve("c").name == "c"  # bare name


# --------------------------------------------------------------------------- describe cache
# ---------------------------------------------------------------------------


def test_describe_cached_within_ttl(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)
    first = eng.describe_table("workorder/work_order")
    second = eng.describe_table("workorder/work_order")
    assert len(provider.open_calls) == 1  # second call served from describe cache
    assert first == second
    stats = eng.cache_stats()
    assert stats["describe_hits"] >= 1
    assert stats["describe_cached_tables"] == 1


def test_describe_not_cached_when_disabled(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=0, dataset_cache_ttl=0)
    eng.describe_table("workorder/work_order")
    eng.describe_table("workorder/work_order")
    assert len(provider.open_calls) == 2


def test_describe_cache_expires_after_ttl(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=60, dataset_cache_ttl=0)
    eng.describe_table("workorder/work_order")
    key = "workorder/work_order"
    eng._describe_cache[key] = (0.0, eng._describe_cache[key][1])  # age past TTL
    eng.describe_table("workorder/work_order")
    assert len(provider.open_calls) == 2


def test_describe_columns_and_uri(tmp_path):
    eng, _ = _make_engine(tmp_path)
    d = eng.describe_table("work_order")
    assert d["uri"] == "fake://workorder/work_order"
    assert {c["name"] for c in d["columns"]} == {"id", "amount", "kind"}
    assert d["n_columns"] == 3


# --------------------------------------------------------------------------- dataset -- cache
# ---------------------------------------------------------------------------


def test_dataset_cached_within_ttl(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=3600)
    info = TABLES[0]
    d1 = eng._open_dataset(info)
    d2 = eng._open_dataset(info)
    assert d1 is d2
    assert len(provider.open_calls) == 1
    assert eng.cache_stats()["dataset_hits"] >= 1


def test_dataset_not_cached_when_disabled(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=0, dataset_cache_ttl=0)
    info = TableInfo("a", "s")
    eng._open_dataset(info)
    eng._open_dataset(info)
    assert len(provider.open_calls) == 2


def test_dataset_lru_eviction(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=3600, dataset_cache_tables=2)
    for name in ("a", "b", "c"):
        eng._open_dataset(TableInfo(name, "s"))
    assert len(eng._dataset_cache) == 2  # capped
    before = len(provider.open_calls)
    eng._open_dataset(TableInfo("a", "s"))  # "a" was evicted -> re-open
    assert len(provider.open_calls) == before + 1


# --------------------------------------------------------------------- prewarm --
# ---------------------------------------------------------------------


def test_prewarm_populates_describe_cache(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600, dataset_cache_ttl=0)
    outcomes = eng.prewarm(("workorder/work_order", "workorder/work_order_note"))
    assert outcomes == {
        "workorder/work_order": "ok",
        "workorder/work_order_note": "ok",
    }
    assert len(provider.open_calls) == 2
    # A direct describe afterwards is now a cache hit (no extra open).
    eng.describe_table("workorder/work_order")
    assert len(provider.open_calls) == 2


def test_prewarm_records_failures(tmp_path):
    eng, provider = _make_engine(tmp_path, cache_ttl=3600)

    def boom(info):
        raise RuntimeError("unavailable")

    provider.open_dataset = boom
    outcomes = eng.prewarm(("workorder/work_order",))
    assert outcomes["workorder/work_order"].startswith("error:")


# --------------------------------------------------------------------- scans + SQL --
# ---------------------------------------------------------------------


def test_scan_arrow_with_projection_and_limit(tmp_path):
    eng, _ = _make_engine(tmp_path)
    t = eng.scan_arrow("work_order", columns=["id"], limit=2)
    assert t.column_names == ["id"]
    assert t.num_rows == 2


def test_scan_arrow_with_filter(tmp_path):
    import pyarrow.compute as pc

    eng, _ = _make_engine(tmp_path)
    t = eng.scan_arrow("work_order", columns=["id"], filters=[pc.field("amount") > 15])
    assert t.to_pydict()["id"] == [2, 3]


def test_query_duckdb(tmp_path):
    eng, _ = _make_engine(tmp_path)
    arrow = eng.query_duckdb("SELECT count(*) AS cnt, sum(amount) AS total FROM work_order")
    assert arrow.to_pydict() == {"cnt": [3], "total": [61.0]}


def test_query_duckdb_join_across_tables(tmp_path):
    eng, _ = _make_engine(tmp_path)
    arrow = eng.query_duckdb("SELECT w.kind, n.note_id FROM work_order w JOIN work_order_note n ON 1=1 LIMIT 3")
    assert arrow.num_rows == 3


def test_query_duckdb_unknown_table_raises(tmp_path):
    eng, _ = _make_engine(tmp_path)
    with pytest.raises(LakehouseError):
        eng.query_duckdb("SELECT * FROM missing_thing")
