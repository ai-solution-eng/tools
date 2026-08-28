"""Tests for the read-only web UI / JSON API layer (``sqlhandler.webui``)."""

import datetime
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import FileConfig
from sqlhandler.engine import SqlEngine
from sqlhandler.file import FileProvider
from sqlhandler.webui import (
    _clamp_limit,
    api_describe,
    api_preview,
    api_query,
    api_status,
    api_tables,
    arrow_to_payload,
    assert_readonly,
)

# ---------------------------------------------------------------------------
# read-only guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t",
        "select * from t",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "EXPLAIN SELECT * FROM t",
        "SHOW TABLES",
        "PRAGMA table_info(t)",
        "VALUES (1, 2)",
        "  (SELECT 1)",  # leading paren is tolerated
        "SELECT * FROM t; SELECT 2;",  # multi-statement all read-only
    ],
)
def test_assert_readonly_allows(sql):
    assert assert_readonly(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "CREATE TABLE t (a int)",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN b int",
        "MERGE INTO t USING x ON 1=1",
        "GRANT SELECT TO u",
        "SELECT 1; DROP TABLE t",  # one bad statement fails the whole query
    ],
)
def test_assert_readonly_rejects(sql):
    with pytest.raises(ValueError):
        assert_readonly(sql)


# ---------------------------------------------------------------------------
# JSON-safe payload conversion
# ---------------------------------------------------------------------------


def test_arrow_to_payload_json_safe():
    utc = datetime.UTC
    table = pa.table(
        {
            "id": [1, 2, 3],
            "name": ["a", "b", "c"],
            "when": [
                datetime.datetime(2024, 1, 1, 12, 0, 0, tzinfo=utc),
                None,
                datetime.datetime(2024, 2, 2, tzinfo=utc),
            ],
            "amount": [Decimal("1.50"), Decimal("2.25"), None],
            "flag": [True, False, None],
        }
    )
    payload = arrow_to_payload(table, limit=2)
    assert payload["n_rows"] == 2
    assert payload["truncated"] is True
    assert len(payload["rows"]) == 2
    assert payload["columns"] == ["id", "name", "when", "amount", "flag"]
    # datetime -> isoformat string
    assert payload["rows"][0][2] == "2024-01-01T12:00:00+00:00"
    # None preserved
    assert payload["rows"][1][2] is None
    # decimal -> float
    assert payload["rows"][0][3] == 1.5


def test_arrow_to_payload_empty():
    table = pa.table({"a": pa.array([], type=pa.int64())})
    payload = arrow_to_payload(table)
    assert payload["columns"] == ["a"]
    assert payload["rows"] == []
    assert payload["n_rows"] == 0
    assert payload["truncated"] is False


# ---------------------------------------------------------------------------
# limit clamping
# ---------------------------------------------------------------------------


def test_clamp_limit():
    assert _clamp_limit(None) == 100
    assert _clamp_limit(0) == 100
    assert _clamp_limit(-5) == 100
    assert _clamp_limit(10) == 10
    assert _clamp_limit(5000) == 1000


# ---------------------------------------------------------------------------
# API handlers against a local file backend
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path):
    pq.write_table(
        pa.table(
            {
                "id": [1, 2, 3],
                "name": ["x", "y", "z"],
                "qty": [1.5, 2.5, 3.5],
            }
        ),
        str(tmp_path / "orders.parquet"),
    )
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    return SqlEngine(provider, cache_ttl=3600, dataset_cache_ttl=3600)


def test_api_status(engine):
    status = api_status(engine)
    assert status["status"] == "ok"
    assert status["backend"] == "nfs"
    assert status["version"]


def test_api_tables(engine):
    tables = api_tables(engine)
    assert any(t["name"] == "orders" for t in tables["tables"])


def test_api_describe(engine):
    desc = api_describe(engine, "orders")
    assert desc["n_columns"] == 3
    cols = {c["name"] for c in desc["columns"]}
    assert cols == {"id", "name", "qty"}
    assert "uri" in desc


def test_api_query(engine):
    payload = api_query(engine, "SELECT id, name FROM orders WHERE qty > 2")
    assert payload["columns"] == ["id", "name"]
    assert payload["rows"] == [[2, "y"], [3, "z"]]
    assert payload["n_rows"] == 2
    assert payload["duration_ms"] >= 0


def test_api_query_limiting(engine):
    payload = api_query(engine, "SELECT * FROM orders", limit=2)
    assert len(payload["rows"]) == 2
    assert payload["n_rows"] == 2
    assert payload["truncated"] is True


def test_api_query_rejects_writes(engine):
    with pytest.raises(ValueError):
        api_query(engine, "DELETE FROM orders")


def test_api_preview(engine):
    payload = api_preview(engine, "orders", limit=2)
    assert payload["table"] == "orders"
    assert payload["columns"] == ["id", "name", "qty"]
    assert len(payload["rows"]) == 2
    assert payload["n_rows"] == 2
    assert payload["truncated"] is True
