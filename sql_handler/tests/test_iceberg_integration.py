"""Integration test for the Iceberg backend (optional).

Runs end-to-end against a local SQL Iceberg catalog + a local filesystem
warehouse (no network). Enable with:

    export SQLHANDLER_TEST_ICEBERG=1
    pytest tests/test_iceberg_integration.py -q
"""

import os
import tempfile

import pyarrow as pa
import pytest
from pyiceberg.catalog.sql import SqlCatalog

from sqlhandler.config import IcebergConfig
from sqlhandler.engine import SqlEngine
from sqlhandler.iceberg import IcebergProvider

CATALOG_NAME = "sqlhandler"  # must match the provider's catalog name


def _catalog_name():
    return os.environ.get("SQLHANDLER_TEST_ICEBERG_CATALOG", CATALOG_NAME)


def test_iceberg_end_to_end():
    if not os.environ.get("SQLHANDLER_TEST_ICEBERG"):
        pytest.skip("Set SQLHANDLER_TEST_ICEBERG=1 to run")

    wh = tempfile.mkdtemp(prefix="iceberg-wh-")
    uri = "sqlite:///" + wh + "/catalog.db"

    # Write side: create a namespace + tables with a SQL catalog.
    writer = SqlCatalog(_catalog_name(), uri=uri, warehouse=wh)
    writer.create_namespace("db")
    sales = writer.create_table(
        "db.sales",
        schema=pa.schema(
            [
                pa.field("id", pa.int64()),
                pa.field("name", pa.string()),
                pa.field("amount", pa.float64()),
            ]
        ),
    )
    sales.append(pa.table({"id": [1, 2, 3], "name": ["a", "b", "c"], "amount": [10.0, 20.0, 30.0]}))
    sales.append(pa.table({"id": [4], "name": ["d"], "amount": [40.0]}))
    writer.create_table("db.items", schema=pa.schema([pa.field("sku", pa.string())])).append(
        pa.table({"sku": ["x", "y"]})
    )
    writer.close()

    eng = SqlEngine(
        IcebergProvider(IcebergConfig(catalog_type="sql", catalog_uri=uri, warehouse=wh, catalog_name=_catalog_name()))
    )

    tables = eng.list_tables()
    assert [t.path for t in tables] == ["db/items", "db/sales"]

    d = eng.describe_table("db/sales")
    assert {c["name"] for c in d["columns"]} == {"id", "name", "amount"}
    assert wh in d["uri"]

    arrow = eng.query_duckdb("SELECT count(*) AS c, sum(amount) AS s FROM sales")
    assert arrow.to_pydict() == {"c": [4], "s": [100.0]}

    scan = eng.scan_arrow("db/sales", columns=["id"], limit=2)
    assert scan.num_rows == 2
