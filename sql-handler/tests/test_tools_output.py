"""Tests for the search_tables tool and structured (json/csv) tool output."""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import server
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import TableInfo

TABLES = [
    TableInfo(name="work_order", schema="workorder", format="parquet"),
    TableInfo(name="work_order_note", schema="workorder", format="parquet"),
]


class SearchStubEngine:
    """Engine surface the tool functions touch, with canned results."""

    def __init__(self, results=None, arrow=None):
        self._results = results or []
        self._arrow = arrow

    def search_tables(self, query):
        return self._results

    def query_duckdb(self, sql, limit=None, params=None, version_as_of=None):
        return self._arrow

    def scan_arrow(self, table, columns=None, limit=None, version_as_of=None):
        return self._arrow


@pytest.fixture
def stub(monkeypatch):
    eng = SearchStubEngine(
        results=[
            {
                "table": "workorder/work_order",
                "name": "work_order",
                "qualified_name": "workorder_work_order",
                "format": "parquet",
                "source": "default",
                "description": "Work order headers, one row per order",
                "matched_columns": ["amount"],
                "score": 150,
            }
        ],
        arrow=pa.table({"id": [1, 2], "amount": [10.5, 20.0]}),
    )
    monkeypatch.setattr(server, "_handler", lambda: eng)
    return eng


# ---------------------------------------------------------------- search


def test_search_tables_rendering(stub):
    text = server.search_tables("work order amount")
    assert "1 table(s) matching" in text
    assert "workorder/work_order (parquet)" in text
    assert "columns: amount" in text
    assert "Work order headers" in text


def test_search_tables_no_matches(stub):
    stub._results = []
    assert "No tables match" in server.search_tables("zzz")


def test_search_tables_error_path(monkeypatch):
    class Boom:
        def search_tables(self, _):
            raise RuntimeError("boom")

    monkeypatch.setattr(server, "_handler", lambda: Boom())
    assert "Error searching tables" in server.search_tables("x")


def test_engine_search_matches_name_and_catalog(tmp_path, monkeypatch):


    # Real engine over parquet fixtures + catalog descriptions.
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1], "amount": [10.0], "kind": ["a"]}), d / "p.parquet")

    class P:
        kind = "fake"

        def list_tables(self):
            return TABLES

        def table_uri(self, info):
            return f"fake://{info.path}"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    cat = tmp_path / "catalog.json"
    cat.write_text(
        json.dumps(
            {
                "tables": {
                    "workorder/work_order": {
                        "description": "maintenance work orders",
                        "columns": {"amount": "total cost in USD"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SQLHANDLER_CATALOG", str(cat))
    eng = SqlEngine(P())

    # matches on table name
    hits = eng.search_tables("work_order")
    assert hits and hits[0]["table"] == "workorder/work_order"
    # matches on catalog description terms
    hits = eng.search_tables("maintenance")
    assert hits and hits[0]["description"].startswith("maintenance")
    # matches on documented column + reports the column
    hits = eng.search_tables("cost usd")
    assert hits and "amount" in hits[0]["matched_columns"]
    # no match
    assert eng.search_tables("completely unrelated") == []
    # empty query
    assert eng.search_tables("") == []


# ------------------------------------------------------- structured output


def test_run_sql_json_output(stub):
    out = server.run_sql("SELECT * FROM t", output_format="json")
    payload = json.loads(out)
    assert payload["columns"] == ["id", "amount"]
    assert payload["rows"] == [[1, 10.5], [2, 20.0]]
    assert payload["n_rows"] == 2


def test_run_sql_csv_output(stub):
    out = server.run_sql("SELECT * FROM t", output_format="csv")
    lines = out.strip().splitlines()
    assert lines[0] == "id,amount"
    assert lines[1] == "1,10.5"


def test_run_sql_markdown_default(stub):
    out = server.run_sql("SELECT * FROM t")
    assert "id" in out and "10.5" in out and "|" in out


def test_scan_table_json_output(stub):
    out = server.scan_table("t", limit=5, output_format="json")
    payload = json.loads(out)
    assert payload["columns"] == ["id", "amount"]


def test_output_rows_cap_applies_to_json(monkeypatch):
    eng = SearchStubEngine(arrow=pa.table({"x": list(range(50))}))
    monkeypatch.setattr(server, "_handler", lambda: eng)
    monkeypatch.setenv("SQLHANDLER_MAX_OUTPUT_ROWS", "10")
    payload = json.loads(server.run_sql("SELECT * FROM t", output_format="json"))
    assert payload["n_rows"] == 10


# ---------------------------------------------------------------- params


class ParamsEngine:
    def __init__(self, arrow):
        self._arrow = arrow
        self.last_params = None

    def query_duckdb(self, sql, limit=None, params=None, version_as_of=None):
        self.last_params = params
        return self._arrow


def test_run_sql_named_params_passthrough(monkeypatch):
    import pyarrow as pa

    eng = ParamsEngine(pa.table({"id": [2], "kind": ["b"]}))
    monkeypatch.setattr(server, "_handler", lambda: eng)
    out = server.run_sql(
        "SELECT * FROM work_order WHERE kind = $k", params={"k": "b"}
    )
    assert eng.last_params == {"k": "b"}
    assert "Error" not in out


def test_engine_params_named_and_positional(tmp_path):
    from sqlhandler.engine import SqlEngine

    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True)
    pq.write_table(
        pa.table({"id": [1, 2, 3], "kind": ["a", "b", "a"]}), d / "p.parquet"
    )

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    eng = SqlEngine(P())
    r1 = eng.query_duckdb("SELECT count(*) AS n FROM work_order WHERE kind = $k", params={"k": "a"})
    assert r1.to_pydict() == {"n": [2]}
    r2 = eng.query_duckdb("SELECT count(*) AS n FROM work_order WHERE id > ? AND kind = ?", params=[1, "a"])
    assert r2.to_pydict() == {"n": [1]}  # id 3 only (id 2 is kind b)
    # invalid params are refused with a clear error
    with pytest.raises(ValueError):
        eng.query_duckdb("SELECT 1", params="not-a-container")
    with pytest.raises(ValueError):
        eng.query_duckdb("SELECT 1", params={"x": {"nested": 1}})


def test_dispatcher_rejects_non_container_params():
    text, is_error = server._dispatch_tool("run_sql", {"sql": "SELECT 1", "params": "bad"})
    assert is_error is True
    assert "object or an array" in text
