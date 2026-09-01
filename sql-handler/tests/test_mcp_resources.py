"""Tests for the MCP resources + prompts (mcp_resources.py) and query memory.

The async handlers are exercised directly with asyncio.run() and a stubbed
sqlhandler.server._handler, so no server or event-loop transport is needed;
the engine behind the stub is a real SqlEngine over local Parquet fixtures
where rendering requires describe/list behavior.
"""

import asyncio

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import mcp_resources as mr
from sqlhandler.engine import SqlEngine
from sqlhandler.provider import LakehouseError, TableInfo

TABLES = [
    TableInfo(name="work_order", schema="workorder", format="parquet"),
    TableInfo(name="work_order_note", schema="workorder", format="parquet"),
]


class CatalogStubEngine:
    """Just the surface the resource handlers use."""

    def __init__(self, descriptions=None):
        self.descriptions = descriptions or {}

    def list_tables(self):
        return TABLES

    def table_description(self, info):
        return self.descriptions.get(info.path, "")

    def describe_table(self, table):
        if table in ("workorder/work_order", "work_order"):
            return {
                "table": table,
                "uri": "fake://workorder/work_order",
                "description": "Work order headers",
                "columns": [
                    {"name": "id", "type": "int64"},
                    {"name": "amount", "type": "double", "description": "USD total"},
                ],
                "n_columns": 2,
            }
        raise LakehouseError(f"Table '{table}' not found")

    def query_memory(self):
        return [
            {"ts": "t", "sql": "SELECT 1", "duration_ms": 1.0, "n_rows": 1, "error": None},
        ]


@pytest.fixture
def stub_engine(monkeypatch):
    eng = CatalogStubEngine({"workorder/work_order": "Work order headers"})
    monkeypatch.setattr("sqlhandler.server._handler", lambda: eng)
    return eng


# ----------------------------------------------------------------- resources


def test_list_resources_includes_catalog_memory_and_tables(stub_engine):
    res = asyncio.run(mr.handle_list_resources(None, None)).resources
    uris = [str(r.uri) for r in res]
    assert "sqlhandler://catalog" in uris
    assert "sqlhandler://query-memory" in uris
    # table path percent-encoded (schema/name contains a slash)
    assert "sqlhandler://table/workorder%2Fwork_order/schema" in uris
    by_uri = {str(r.uri): r for r in res}
    schema_res = by_uri["sqlhandler://table/workorder%2Fwork_order/schema"]
    assert schema_res.description == "Work order headers"


def test_list_resource_templates_exposes_schema_template():
    res = asyncio.run(mr.handle_list_resource_templates(None, None)).resource_templates
    assert [t.uri_template for t in res] == ["sqlhandler://table/{table}/schema"]


def test_read_resource_table_schema(stub_engine):
    params = type("P", (), {"uri": "sqlhandler://table/workorder%2Fwork_order/schema"})()
    result = asyncio.run(mr.handle_read_resource(None, params))
    text = result.contents[0].text
    assert result.contents[0].mime_type == "text/markdown"
    assert "Work order headers" in text
    assert "| id | int64 |" in text
    assert "USD total" in text  # column doc merged


def test_read_resource_catalog_and_query_memory(stub_engine):
    cat = asyncio.run(mr.handle_read_resource(None, type("P", (), {"uri": "sqlhandler://catalog"})()))
    assert "Work order headers" in cat.contents[0].text
    mem = asyncio.run(mr.handle_read_resource(None, type("P", (), {"uri": "sqlhandler://query-memory"})()))
    assert "SELECT 1" in mem.contents[0].text


def test_read_resource_unknown_raises_mcp_error(stub_engine):
    from mcp.shared.exceptions import MCPError

    with pytest.raises(MCPError):
        asyncio.run(mr.handle_read_resource(None, type("P", (), {"uri": "sqlhandler://nope"})()))
    with pytest.raises(MCPError):
        asyncio.run(mr.handle_read_resource(None, type("P", (), {"uri": "sqlhandler://table/missing/schema"})()))


def test_catalog_resource_mentions_missing_catalog(stub_engine):
    eng = CatalogStubEngine({})  # no descriptions anywhere
    text = mr._catalog_text(eng)
    assert "No semantic-catalog descriptions" in text


# ------------------------------------------------------------------- prompts


def test_list_prompts():
    prompts = asyncio.run(mr.handle_list_prompts(None, None)).prompts
    assert {p.name for p in prompts} == {"explore-data", "analyze-table"}


def test_get_prompt_explore_data(stub_engine):
    params = type("P", (), {"name": "explore-data", "arguments": {"goal": "churn by month"}})()
    result = asyncio.run(mr.handle_get_prompt(None, params))
    text = result.messages[0].content.text
    assert "churn by month" in text
    assert "profile_table" in text


def test_get_prompt_analyze_table(stub_engine):
    params = type("P", (), {"name": "analyze-table", "arguments": {"table": "workorder/work_order"}})()
    result = asyncio.run(mr.handle_get_prompt(None, params))
    assert "workorder/work_order" in result.messages[0].content.text


def test_get_prompt_errors(stub_engine):
    from mcp.shared.exceptions import MCPError

    with pytest.raises(MCPError):
        asyncio.run(mr.handle_get_prompt(None, type("P", (), {"name": "analyze-table", "arguments": {}})()))
    with pytest.raises(MCPError):
        asyncio.run(mr.handle_get_prompt(None, type("P", (), {"name": "nope", "arguments": {}})()))


# -------------------------------------------------------------- query memory


def _make_engine(tmp_path, **kw):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2, 3], "amount": [10.5, 20.0, 30.5], "kind": ["a", "b", "a"]}),
        d / "part.parquet",
    )

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return f"fake://{info.path}"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    return SqlEngine(P(), **kw)


def test_query_memory_records_success_and_failure(tmp_path):
    eng = _make_engine(tmp_path)
    eng.query_duckdb("SELECT count(*) AS n FROM work_order")
    mem = eng.query_memory()
    assert len(mem) == 1
    assert mem[0]["n_rows"] == 1
    assert mem[0]["error"] is None
    assert "count(*)" in mem[0]["sql"]
    try:
        eng.query_duckdb("SELECT nope FROM work_order")
    except LakehouseError:
        pass
    mem = eng.query_memory()
    assert len(mem) == 2
    assert mem[-1]["error"] and "nope" in mem[-1]["error"]


def test_query_memory_capped_and_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_QUERY_MEMORY_SIZE", "2")
    eng = _make_engine(tmp_path)
    for _ in range(5):
        eng.query_duckdb("SELECT count(*) AS n FROM work_order")
    assert len(eng.query_memory()) == 2

    monkeypatch.setenv("SQLHANDLER_QUERY_MEMORY_SIZE", "0")
    eng2 = _make_engine(tmp_path)
    eng2.query_duckdb("SELECT count(*) AS n FROM work_order")
    assert eng2.query_memory() == []


def test_query_memory_resource_renders_rows(tmp_path):
    eng = _make_engine(tmp_path)
    eng.query_duckdb("SELECT count(*) AS n FROM work_order")
    text = mr._query_memory_text(eng)
    assert "SELECT count(*) AS n FROM work_order" in text
