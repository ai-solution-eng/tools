"""Tests for federated multi-source support (MultiProvider + SQLHANDLER_SOURCES).

Covers source tagging, routing to the owning provider, config parsing, and
cross-source SQL (including bare-name collision gating in the engine).
"""

import json

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq

from sqlhandler.engine import SqlEngine
from sqlhandler.provider import MultiProvider, TableInfo


class DirProvider:
    """Tiny provider: every .parquet in a directory is a table (stem = name)."""

    kind = "dir"

    def __init__(self, root):
        self.root = root

    def list_tables(self):
        out = []
        for p in sorted(self.root.glob("*.parquet")):
            out.append(TableInfo(name=p.stem, format="parquet", location=p.name))
        return out

    def table_uri(self, info):
        return f"file://{self.root / info.location}"

    def open_dataset(self, info):
        return pad.dataset(str(self.root / info.location), format="parquet")


def _write(root, name, table):
    root.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, root / name)


# --------------------------------------------------------------------------- TableInfo


def test_qualified_name_includes_source_only_when_set():
    assert TableInfo(name="orders").qualified_name == "orders"
    assert TableInfo(name="orders", schema="sales").qualified_name == "sales_orders"
    assert TableInfo(name="orders", schema="sales", source="wh").qualified_name == "wh_sales_orders"
    assert TableInfo(name="orders", source="wh").qualified_name == "wh_orders"


# --------------------------------------------------------------------------- MultiProvider


def test_multiprovider_merges_and_tags_sources(tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    _write(d1, "orders.parquet", pa.table({"id": [1]}))
    _write(d2, "customers.parquet", pa.table({"id": [1]}))
    mp = MultiProvider([DirProvider(d1), DirProvider(d2)], ["sales", "crm"])
    tables = mp.list_tables()
    assert [(t.source, t.name) for t in tables] == [("crm", "customers"), ("sales", "orders")]
    assert all(t.source in ("sales", "crm") for t in tables)
    assert mp.source_count == 2


def test_multiprovider_routes_open_and_uri_to_owner(tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    _write(d1, "orders.parquet", pa.table({"id": [1]}))
    _write(d2, "customers.parquet", pa.table({"id": [1]}))
    mp = MultiProvider([DirProvider(d1), DirProvider(d2)], ["sales", "crm"])
    tables = mp.list_tables()
    sales = next(t for t in tables if t.source == "sales")
    crm = next(t for t in tables if t.source == "crm")
    assert str(d1) in mp.table_uri(sales)
    assert str(d2) in mp.table_uri(crm)
    assert mp.open_dataset(sales).to_table().to_pydict()["id"] == [1]


# --------------------------------------------------------------------------- config


def test_load_source_providers_parses_json():
    raw = json.dumps(
        [
            {"name": "sales", "backend": "s3", "bucket": "b1", "accessKey": "k", "secretKey": "s"},
            {
                "name": "inventory",
                "backend": "s3",
                "bucket": "b2",
                "accessKey": "k",
                "secretKey": "s",
                "prefix": "raw",
            },
        ]
    )
    from sqlhandler.config import load_source_providers

    mp = load_source_providers({"SQLHANDLER_SOURCES": raw})
    assert mp is not None
    assert mp.source_count == 2
    assert mp.sources == ["sales", "inventory"]


def test_load_config_none_when_unset():
    from sqlhandler.config import load_source_providers

    assert load_source_providers({"SQLHANDLER_SOURCES": ""}) is None


def test_load_config_invalid_json_raises():
    from sqlhandler.config import load_source_providers

    try:
        load_source_providers({"SQLHANDLER_SOURCES": "{nope"})
    except ValueError:
        return
    raise AssertionError("expected ValueError for invalid JSON")


# --------------------------------------------------------------------------- engine cross-source


def test_engine_cross_source_join(tmp_path):
    d1, d2 = tmp_path / "sales", tmp_path / "inventory"
    _write(
        d1,
        "orders.parquet",
        pa.table({"order_id": [1, 2, 3], "cust": [10, 20, 10], "amount": [1.0, 2.0, 3.0]}),
    )
    _write(d2, "customers.parquet", pa.table({"cust_id": [10, 20], "name": ["a", "b"]}))
    mp = MultiProvider([DirProvider(d1), DirProvider(d2)], ["sales", "inventory"])
    eng = SqlEngine(mp, cache_ttl=3600, list_async_refresh=False)
    res = eng.query_duckdb(
        "SELECT s.amount, i.name FROM sales_orders s JOIN inventory_customers i ON s.cust = i.cust_id ORDER BY s.amount"
    )
    d = res.to_pydict()
    assert d["amount"] == [1.0, 2.0, 3.0]
    assert d["name"] == ["a", "b", "a"]


def test_engine_bare_name_collision_not_registered(tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    _write(d1, "orders.parquet", pa.table({"x": [1]}))
    _write(d2, "orders.parquet", pa.table({"y": [2]}))
    mp = MultiProvider([DirProvider(d1), DirProvider(d2)], ["a", "b"])
    eng = SqlEngine(mp, cache_ttl=3600, list_async_refresh=False)
    # source-qualified names work…
    assert eng.query_duckdb("SELECT x FROM a_orders").to_pydict()["x"] == [1]
    assert eng.query_duckdb("SELECT y FROM b_orders").to_pydict()["y"] == [2]
    # …but the ambiguous bare name is not registered.
    try:
        eng.query_duckdb("SELECT x FROM orders")
    except Exception:
        pass
    else:
        raise AssertionError("ambiguous bare name 'orders' should not resolve")
