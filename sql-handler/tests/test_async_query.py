"""Tests for QueryJob (timeout/cancel) and the async query API + pagination."""

import time

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler.engine import LakehouseError, QueryJob, SqlEngine
from sqlhandler.provider import TableInfo
from sqlhandler.webui import (
    QueryJobManager,
    api_async_query,
    api_query_cancel,
    api_query_rows,
    api_query_status,
)


def _make_engine(tmp_path):
    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"id": [1, 2, 3, 4, 5], "kind": ["a", "b", "a", "b", "a"]}),
        d / "part.parquet",
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

    return SqlEngine(P(), cache_ttl=0)


def _patch_register(monkeypatch, sleep=None):
    """Replace schema registration (optionally with a slow one)."""
    import sqlhandler.engine as eng_mod

    def register(self, con, sql, version=None):
        if sleep is not None:
            time.sleep(sleep)
        con.register("work_order", pa.table({"id": [1, 2, 3, 4, 5], "kind": ["a", "b", "a", "b", "a"]}))

    monkeypatch.setattr(eng_mod.SqlEngine, "_register_schema", register)


# ---------------------------------------------------------------- QueryJob


def test_query_job_lifecycle(tmp_path):
    eng = _make_engine(tmp_path)
    job = QueryJob(eng, "SELECT * FROM work_order WHERE kind = 'a'")
    assert job.wait(10) == "done"
    assert job.result.to_pydict() == {"id": [1, 3, 5], "kind": ["a", "a", "a"]}
    assert job.state == "done"
    assert job.elapsed_ms is not None and job.elapsed_ms >= 0


def test_query_job_error_surfaces_message(tmp_path):
    eng = _make_engine(tmp_path)
    job = QueryJob(eng, "SELECT nope FROM work_order")
    job.wait(10)
    assert job.state == "error"
    assert job.error and "nope" in job.error
    with pytest.raises(LakehouseError, match="nope"):
        _ = job.result


def test_query_job_validates_upfront(tmp_path):
    eng = _make_engine(tmp_path)
    with pytest.raises(ValueError):
        QueryJob(eng, "SELECT 1", params={"x": [1, 2]})  # nested container
    with pytest.raises(LakehouseError, match="non-negative integer"):
        QueryJob(eng, "SELECT 1", version_as_of="zero")


def test_query_timeout_interrupts(monkeypatch, tmp_path):
    monkeypatch.setenv("SQLHANDLER_QUERY_TIMEOUT", "0.2")
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    t0 = time.monotonic()
    with pytest.raises(LakehouseError, match="timed out"):
        eng.query_duckdb("SELECT * FROM work_order")
    assert time.monotonic() - t0 < 5  # bounded, not the full sleep


def test_no_timeout_by_default(tmp_path):
    eng = _make_engine(tmp_path)
    arrow = eng.query_duckdb("SELECT count(*) AS n FROM work_order")
    assert arrow.to_pydict() == {"n": [5]}


def test_cancel_running_job(monkeypatch, tmp_path):
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    job = QueryJob(eng, "SELECT * FROM work_order")
    time.sleep(0.1)  # let the thread start and take the connection
    job.cancel()
    job.wait(10)
    assert job.state == "cancelled"
    with pytest.raises(LakehouseError, match="cancelled"):
        _ = job.result


# ------------------------------------------------------------- async API


def test_async_submit_status_rows_flow(tmp_path):
    eng = _make_engine(tmp_path)
    mgr = QueryJobManager()
    submitted = api_async_query(eng, mgr, {"sql": "SELECT * FROM work_order WHERE kind = 'b'"})
    assert submitted["state"] in ("running", "done")
    qid = submitted["query_id"]

    status = api_query_status(mgr, qid)
    for _ in range(100):
        status = api_query_status(mgr, qid)
        if status["state"] != "running":
            break
        time.sleep(0.05)
    assert status["state"] == "done"
    assert status["n_rows"] == 2
    assert status["columns"] == ["id", "kind"]

    page = api_query_rows(mgr, qid, offset=0, limit=1)
    assert page["rows"] == [[2, "b"]]
    assert page["total_rows"] == 2
    page2 = api_query_rows(mgr, qid, offset=1, limit=10)
    assert page2["rows"] == [[4, "b"]]


def test_async_readonly_guard_applies(tmp_path):
    eng = _make_engine(tmp_path)
    mgr = QueryJobManager()
    # Called directly (no route wrapper), the guard raises; the key property
    # is that nothing was ever submitted.
    with pytest.raises(ValueError, match="CREATE"):
        api_async_query(eng, mgr, {"sql": "CREATE TABLE x (a int)"})
    assert mgr._jobs == {}  # nothing was started


def test_async_unknown_id_404(tmp_path):
    _make_engine(tmp_path)  # engine unused: unknown ids 404 before any engine call
    mgr = QueryJobManager()
    assert api_query_status(mgr, "nope")["status"] == 404
    assert api_query_rows(mgr, "nope")["status"] == 404
    assert api_query_cancel(mgr, "nope")["status"] == 404


def test_async_cancel_endpoint(tmp_path, monkeypatch):
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    mgr = QueryJobManager()
    submitted = api_async_query(eng, mgr, {"sql": "SELECT * FROM work_order"})
    time.sleep(0.1)
    result = api_query_cancel(mgr, submitted["query_id"])
    assert result["interrupted"] is True
    for _ in range(100):
        if api_query_status(mgr, submitted["query_id"])["state"] != "running":
            break
        time.sleep(0.05)
    assert api_query_status(mgr, submitted["query_id"])["state"] == "cancelled"


def test_manager_evicts_oldest_finished(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLHANDLER_ASYNC_JOB_TTL", "0")  # no TTL; size cap only
    eng = _make_engine(tmp_path)
    mgr = QueryJobManager(max_jobs=2)
    ids = []
    for _ in range(4):
        r = api_async_query(eng, mgr, {"sql": "SELECT count(*) AS n FROM work_order"})
        qid = r["query_id"]
        for _ in range(100):
            if api_query_status(mgr, qid)["state"] != "running":
                break
            time.sleep(0.05)
        ids.append(qid)
    # Invariants: the cap is never exceeded, the OLDEST jobs were evicted
    # first, and the newest job is still fetchable (polling also triggers
    # cleanup, so the exact final count can be cap-1 — assert the invariants).
    assert len(mgr._jobs) <= 2
    assert ids[0] not in mgr._jobs and ids[1] not in mgr._jobs
    assert ids[-1] in mgr._jobs


def test_manager_rejects_when_all_running(tmp_path, monkeypatch):
    _patch_register(monkeypatch, sleep=1.0)
    eng = _make_engine(tmp_path)
    mgr = QueryJobManager(max_jobs=1)
    first = api_async_query(eng, mgr, {"sql": "SELECT * FROM work_order"})
    second = api_async_query(eng, mgr, {"sql": "SELECT * FROM work_order"})
    assert second["status"] == 429
    api_query_cancel(mgr, first["query_id"])
