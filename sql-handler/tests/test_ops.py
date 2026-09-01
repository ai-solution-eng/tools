"""Tests for ops features: /metrics exposition, audit log, concurrency gate, API token."""

import asyncio
import json
import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler import observability
from sqlhandler.engine import LakehouseError, QueryJob, SqlEngine
from sqlhandler.observability import Metrics, audit_query, token_matches
from sqlhandler.provider import TableInfo
from sqlhandler.server import _ApiTokenMiddleware


def _make_engine(tmp_path, **kw):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"id": [1, 2, 3, 4, 5], "kind": ["a", "b", "a", "b", "a"]}), d / "part.parquet")

    class P:
        kind = "fake"

        def list_tables(self):
            return [TableInfo(name="work_order", schema="workorder", format="parquet")]

        def table_uri(self, info):
            return "fake://"

        def open_dataset(self, info, version=None):
            import pyarrow.dataset as pad

            return pad.dataset(str(d), format="parquet")

    return SqlEngine(P(), **kw)


# ------------------------------------------------------------------ metrics


def test_metrics_record_and_render():
    m = Metrics()
    m.record_query("ok", 0.05, 10, "t1")
    m.record_query("ok", 1.5, 3, "t1")
    m.record_query("error", 0.2, None, "t1")
    text = m.render()
    assert 'sqlhandler_queries_total{outcome="ok"} 2' in text
    assert 'sqlhandler_queries_total{outcome="error"} 1' in text
    assert 'sqlhandler_query_duration_seconds_bucket{outcome="ok",le="0.05"} 1' in text
    assert 'sqlhandler_query_duration_seconds_bucket{outcome="ok",le="+Inf"} 2' in text
    assert 'sqlhandler_query_duration_seconds_count{outcome="ok"} 2' in text
    assert 'sqlhandler_query_rows_total{table="t1"} 13' in text
    assert text.endswith("\n")


def test_metrics_engine_gauges(tmp_path):
    m = Metrics()
    eng = _make_engine(tmp_path)
    eng.query_duckdb("SELECT count(*) AS n FROM work_order")
    text = m.render(eng)
    assert "sqlhandler_tables 1" in text
    assert 'sqlhandler_cache_hits_total{cache="describe"} 0' in text
    assert "sqlhandler_process_rss_bytes" in text


def test_engine_records_outcomes(tmp_path):
    m = observability.metrics
    eng = _make_engine(tmp_path)
    eng.query_duckdb("SELECT count(*) AS n FROM work_order")
    try:
        eng.query_duckdb("SELECT nope FROM work_order")
    except LakehouseError:
        pass
    text = m.render()
    assert 'sqlhandler_queries_total{outcome="ok"}' in text
    assert 'sqlhandler_queries_total{outcome="error"}' in text


# -------------------------------------------------------------------- audit


def test_audit_log_jsonl(tmp_path):
    logfile = tmp_path / "audit.jsonl"
    audit_query("SELECT 1", "ok", 12.3, 1, None)
    # logging off -> nothing written anywhere we know of
    import os

    os.environ["SQLHANDLER_AUDIT_LOG"] = str(logfile)
    try:
        audit_query("SELECT 42", "ok", 5.0, 1, None)
        audit_query("SELECT bad FROM nowhere", "error", 1.0, None, "Table not found")
    finally:
        os.environ.pop("SQLHANDLER_AUDIT_LOG", None)
    lines = logfile.read_text().strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["event"] == "query" and rec["sql"] == "SELECT 42" and rec["state"] == "ok"
    assert rec["n_rows"] == 1
    rec2 = json.loads(lines[1])
    assert rec2["state"] == "error" and rec2["error"]


def test_audit_via_engine_query(tmp_path):
    logfile = tmp_path / "audit.jsonl"
    eng = _make_engine(tmp_path)
    import os

    os.environ["SQLHANDLER_AUDIT_LOG"] = str(logfile)
    try:
        eng.query_duckdb("SELECT count(*) AS n FROM work_order")
    finally:
        os.environ.pop("SQLHANDLER_AUDIT_LOG", None)
    rec = json.loads(logfile.read_text().strip())
    assert rec["state"] == "ok" and "work_order" in rec["sql"]


def test_audit_bad_path_never_raises(tmp_path):
    import os

    os.environ["SQLHANDLER_AUDIT_LOG"] = str(tmp_path / "missing_dir" / "audit.jsonl")
    try:
        audit_query("SELECT 1", "ok", 1.0, 1, None)  # must not raise
    finally:
        os.environ.pop("SQLHANDLER_AUDIT_LOG", None)


# ---------------------------------------------------------- concurrency cap


def _patch_register(monkeypatch, sleep=None):
    import sqlhandler.engine as eng_mod

    def register(self, con, sql, version=None):
        if sleep is not None:
            time.sleep(sleep)
        con.register("work_order", pa.table({"id": [1, 2, 3, 4, 5]}))

    monkeypatch.setattr(eng_mod.SqlEngine, "_register_schema", register)


def test_concurrency_gate_queues_and_releases(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", "1")
    monkeypatch.setenv("SQLHANDLER_QUEUE_TIMEOUT", "5")
    _patch_register(monkeypatch, sleep=0.3)
    eng = _make_engine(tmp_path)
    t0 = time.monotonic()
    a = QueryJob(eng, "SELECT * FROM work_order")
    b = QueryJob(eng, "SELECT * FROM work_order")  # must queue behind a
    a.wait(10)
    b.wait(10)
    assert a.state == "done" and b.state == "done"
    assert time.monotonic() - t0 >= 0.6  # b ran after a's slot freed


def test_concurrency_gate_queue_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", "1")
    monkeypatch.setenv("SQLHANDLER_QUEUE_TIMEOUT", "0.2")
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    a = QueryJob(eng, "SELECT * FROM work_order")
    time.sleep(0.1)
    with pytest.raises(LakehouseError, match="Too many concurrent queries"):
        QueryJob(eng, "SELECT * FROM work_order")
    a.cancel()
    a.wait(10)


def test_gate_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("SQLHANDLER_MAX_CONCURRENT_QUERIES", raising=False)
    _patch_register(monkeypatch)
    eng = _make_engine(tmp_path)
    # more concurrent jobs than any sane cap, all succeed
    jobs = [QueryJob(eng, "SELECT * FROM work_order") for _ in range(12)]
    for j in jobs:
        j.wait(10)
    assert all(j.state == "done" for j in jobs)


# ---------------------------------------------------------------- api token


def test_token_matching():
    assert token_matches("Bearer secret", "secret")
    assert token_matches("secret", "secret")
    assert token_matches(" Bearer secret ", "secret")
    assert not token_matches("Bearer wrong", "secret")
    assert not token_matches("", "secret")
    assert token_matches("anything", "")  # no token configured = allow


def _run_middleware(app, path, headers):
    scope = {
        "type": "http",
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    received = {}

    async def send(message):
        if message["type"] == "http.response.start":
            received["status"] = message["status"]

    async def receive():
        return {"type": "http.request"}

    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})

    mw = _ApiTokenMiddleware(inner_app, "secret")
    asyncio.run(mw(scope, receive, send))
    return received.get("status")


def test_token_middleware_blocks_and_allows():
    assert _run_middleware(None, "/api/tables", {}) == 401
    assert _run_middleware(None, "/api/tables", {"Authorization": "Bearer wrong"}) == 401
    assert _run_middleware(None, "/api/tables", {"Authorization": "Bearer secret"}) == 200
    assert _run_middleware(None, "/api/tables", {"X-API-Token": "secret"}) == 200
    # non-/api paths pass through untouched
    assert _run_middleware(None, "/ui", {}) == 200
    assert _run_middleware(None, "/mcp", {}) == 200
    assert _run_middleware(None, "/health", {}) == 200
