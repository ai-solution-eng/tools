"""ASGI-level tests for the webapp's API-key gate, pages and free-text
target launch flow. Calls the assembled ASGI app directly (the same call
path uvicorn uses) -- no socket needed."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from model_benchmarker.webapp import runner as R  # noqa: E402
from model_benchmarker.webapp.app import create_app  # noqa: E402


def _call(app, method, path, headers=None, body=None):
    # split any ?query off the path (FastAPI routes on path only; the query
    # travels in scope["query_string"] for request.query_params to parse)
    path_part, _, query_part = path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": method,
        "path": path_part,
        "raw_path": path.encode(),
        "query_string": query_part.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }
    status_box = {}
    parts = []

    async def receive():
        return {"type": "http.request", "body": body or b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status_box["status"] = message["status"]
        elif message["type"] == "http.response.body":
            parts.append(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status_box.get("status", 0), b"".join(parts)


@pytest.fixture()
def gated_app(monkeypatch, tmp_path):
    monkeypatch.setenv("BENCH_API_KEYS", "test-key-1,test-key-2")
    monkeypatch.setenv("BENCH_WORK_DIR", str(tmp_path / "w"))
    monkeypatch.setenv(
        R.TARGET_SUGGESTIONS_ENV,
        json.dumps(
            [
                {"name": "model-a (chat)", "url": "http://model-a.svc:8000", "kind": "chat"},
                {"name": "rag (MCP)", "url": "http://rag.svc/mcp", "kind": "endpoint"},
            ]
        ),
    )
    return create_app(str(tmp_path / "w"))


def test_estimator_page_public(gated_app):
    status, body = _call(gated_app, "GET", "/")
    assert status == 200
    assert b"LLM memory estimator" in body


def test_health_public(gated_app):
    status, body = _call(gated_app, "GET", "/healthz")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_status_public_reports_auth(gated_app):
    status, body = _call(gated_app, "GET", "/api/status")
    assert status == 200
    payload = json.loads(body)
    assert payload["auth_required"] is True


def test_benchmark_page_gated(gated_app):
    status, _ = _call(gated_app, "GET", "/benchmark/chat")
    assert status == 401
    status, body = _call(gated_app, "GET", "/benchmark/chat", headers={"X-API-Key": "test-key-1"})
    assert status == 200
    assert b"Chat benchmark" in body


def test_targets_gated(gated_app):
    status, _ = _call(gated_app, "GET", "/api/targets")
    assert status == 401
    status, body = _call(gated_app, "GET", "/api/targets", headers={"Authorization": "Bearer test-key-2"})
    assert status == 200
    targets = json.loads(body)["targets"]
    assert [t["name"] for t in targets] == ["model-a (chat)", "rag (MCP)"]


def test_runs_api_gated(gated_app):
    status, _ = _call(gated_app, "GET", "/api/runs")
    assert status == 401
    status, _ = _call(gated_app, "GET", "/api/runs", headers={"X-API-Key": "test-key-1"})
    assert status == 200


def test_launch_free_text_target(gated_app):
    headers = {"X-API-Key": "test-key-1", "Content-Type": "application/json"}

    class FakePopen:
        def __init__(self, argv, **kw):
            self.pid = 1

        def wait(self, timeout=None):
            return 0

    import time

    from model_benchmarker.webapp import runner as R2

    real = R2.subprocess.Popen
    R2.subprocess.Popen = FakePopen
    try:
        status, body = _call(
            gated_app,
            "POST",
            "/api/runs",
            headers=headers,
            body=json.dumps(
                {"kind": "chat", "target": "http://any-host:8000", "params": {"number_users": "1"}}
            ).encode(),
        )
        assert status == 202
        assert json.loads(body)["status"] == "running"
        # target embedded in URL credentials -> 400
        status, _ = _call(
            gated_app,
            "POST",
            "/api/runs",
            headers=headers,
            body=json.dumps({"kind": "chat", "target": "http://u:p@h", "params": {}}).encode(),
        )
        assert status == 400
        # missing target -> 400
        status, _ = _call(
            gated_app,
            "POST",
            "/api/runs",
            headers=headers,
            body=json.dumps({"kind": "chat", "params": {}}).encode(),
        )
        assert status == 400
    finally:
        R2.subprocess.Popen = real
    assert R2.subprocess.Popen is real
    del time


def test_estimate_api_public(gated_app):
    llama = {
        "model_type": "llama",
        "hidden_size": 4096,
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "head_dim": 128,
        "vocab_size": 128256,
        "intermediate_size": 14336,
        "torch_dtype": "bfloat16",
    }
    status, body = _call(
        gated_app,
        "POST",
        "/api/estimate",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"config_json": llama, "gpu": "H200", "gpus": 1, "tp": 1}).encode(),
    )
    assert status == 200
    assert json.loads(body)["fits"] is True


def test_open_auth_still_gates_pages(monkeypatch, tmp_path):
    monkeypatch.delenv("BENCH_API_KEYS", raising=False)
    monkeypatch.delenv("MCP_API_KEYS", raising=False)
    monkeypatch.setenv("BENCH_WORK_DIR", str(tmp_path / "w"))
    app = create_app(str(tmp_path / "w"))
    status, _ = _call(app, "GET", "/benchmark/chat")
    assert status == 200


# ---------------------------------------------------------------------------
# results-page compare feature (GET /api/results/compare + compare.py)
# Appended tests only; the fixtures above are the Lead's.
# ---------------------------------------------------------------------------

from model_benchmarker.webapp import compare as C  # noqa: E402

_CHAT_MD = """# demo run

Per-level latency / throughput (multiturn):

| ctx | users | task | failed | TTFT turn1 P50 (ms) | TTFT turn1 P95 (ms) | TTFT turn1 P99 (ms) | TTFT turn1 P100 (ms) | TTFT-post P50 (ms) | TTFT-post P95 (ms) | TTFT-post P99 (ms) | TTFT-post P100 (ms) | tokens/s P50 | tokens/s P95 | tokens/s P99 | tokens/s P100 |
|:---|:---|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1 | coding | 0 | 930.6 | 930.6 | 930.6 | 930.6 | 510.4 | 847.2 | 882.1 | 890.8 | 331.1 | 318.0 | 317.6 | 317.5 |
| 0 | 1 | coding | 0 | 800.0 | 800.0 | 800.0 | 800.0 | 500.0 | 700.0 | 800.0 | 900.0 | 300.0 | 290.0 | 280.0 | 270.0 |
| 0 | 4 | mixed | 2 | 382.8 | 473.3 | 473.4 | 473.5 | 511.3 | 665.8 | 685.0 | 689.8 | 220.4 | 205.6 | 205.4 | 205.3 |
"""

_EP_RUNJSON_TEMPLATE = {
    "run_id": "x",
    "tool": "endpoint-benchmarker",
    "started_utc": "2026-01-01T00:00:00+00:00",
    "finished_utc": "2026-01-01T00:05:00+00:00",
    "config": {"mode": "rest", "url": "http://m:8000", "percentiles": ["p50", "p95", "p99"]},
    "levels": [],
    "summary": {"rows": [], "knee": None, "notes": []},
}


def _ep_row(concurrency: int, rps: float, lat_p50: float, lat_p99: float, lat_max: float, **extra) -> dict:
    row = {
        "concurrency": concurrency,
        "total": 100,
        "success_rate_pct": 99.0,
        "rps": rps,
        "rps_per_user": round(rps / concurrency, 2),
        "lat_min": 1.0,
        "lat_mean": lat_p50,
        "lat_p50": lat_p50,
        "lat_p95": lat_p99 * 0.9,
        "lat_p99": lat_p99,
        "lat_max": lat_max,
        "avg_results": 1.0,
        "avg_bytes": 128.0,
        "window_s": 60.0,
    }
    row.update(extra)
    return row


def _meta(run_id: str, kind: str, target: str, params: dict) -> dict:
    return {
        "run_id": run_id,
        "kind": kind,
        "endpoint": "http://m:8000",
        "target_url": target,
        "params": params,
        "started_utc": "2026-01-01T00:00:00+00:00",
        "status": "success",
        "exit_code": 0,
        "finished_utc": "2026-01-01T00:05:00+00:00",
    }


def _fabricate_runs(work: Path, runs: list[tuple[str, str, dict, dict]]) -> None:
    """Write run_meta.json (+ run.json or report.md) into BENCH_WORK_DIR/runs/
    so _scan_workdir adopts them at create_app time. payload_file carries the
    artifact content under "summary" (endpoint run.json) or "report_md"
    (chat); WITHOUT that key no artifact is written (interrupted-run fixture)."""
    for run_id, kind, params, payload_file in runs:
        run_dir = work / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_meta.json").write_text(
            json.dumps(_meta(run_id, kind, "http://m:8000", params)), encoding="utf-8"
        )
        if kind == "endpoint" and "summary" in payload_file:
            doc = json.loads(json.dumps(_EP_RUNJSON_TEMPLATE))
            doc["run_id"] = run_id
            doc["summary"] = payload_file["summary"]
            (run_dir / "run.json").write_text(json.dumps(doc), encoding="utf-8")
        elif kind == "chat" and "report_md" in payload_file:
            (run_dir / "report.md").write_text(payload_file["report_md"], encoding="utf-8")


@pytest.fixture()
def compare_app(monkeypatch, tmp_path):
    """App over a work dir pre-seeded with fabricated runs (2 endpoint with
    overlapping+distinct levels, 1 chat, 1 empty/interrupted)."""
    monkeypatch.setenv("BENCH_API_KEYS", "test-key-1")
    work = tmp_path / "w"
    monkeypatch.setenv("BENCH_WORK_DIR", str(work))
    _fabricate_runs(
        work,
        [
            (
                "ep-run-a",
                "endpoint",
                {"mode": "rest", "sweep": "1,4", "headers": {"X-Env": "trial"}},
                {
                    "summary": {
                        "rows": [
                            _ep_row(1, 100.0, 2.0, 4.0, 5.0, gpu_util_mean=40.0),
                            _ep_row(4, 250.0, 8.0, 12.0, 20.0),
                        ],
                        "knee": {"concurrency": 4, "criterion": "latency", "detail": "p99 3x best"},
                        "notes": [],
                    }
                },
            ),
            (
                "ep-run-b",
                "endpoint",
                {"mode": "rest", "sweep": "4"},
                {
                    "summary": {
                        "rows": [
                            _ep_row(4, 500.0, 4.0, 6.0, 10.0, success_rate_pct=98.5),
                        ],
                        "knee": None,
                        "notes": [],
                    }
                },
            ),
            (
                "chat-run-c",
                "chat",
                {"number_users": "1,4", "context_length": "0", "tasks": "coding,creative"},
                {
                    "report_md": _CHAT_MD,
                },
            ),
            ("dead-run-d", "chat", {"number_users": "1"}, {}),  # no artifacts at all
        ],
    )
    return create_app(str(work))


def _compare(app, ids):
    status, body = (
        _call(app, "GET", "/api/results/compare", headers={}, query=None)
        if False
        else _call(app, "GET", "/api/results/compare?ids=" + ids)
    )
    return status, json.loads(body) if status == 200 else body


def test_compare_requires_ids(compare_app):
    status, _body = _call(compare_app, "GET", "/api/results/compare?ids=")
    assert status == 400
    status, _body = _call(compare_app, "GET", "/api/results/compare")
    assert status == 400  # FastAPI: ids is a required query param


def test_compare_caps_at_four_ids(compare_app):
    ids = ",".join(f"a{i}" for i in range(5))
    status, body = _call(compare_app, "GET", "/api/results/compare?ids=" + ids)
    assert status == 400
    assert b"at most 4" in body


def test_compare_single_id_rejected(compare_app):
    status, _body = _call(compare_app, "GET", "/api/results/compare?ids=ep-run-a")
    assert status == 400


def test_compare_endpoint_runs_merged_rows_and_deltas(compare_app):
    status, body = _call(compare_app, "GET", "/api/results/compare?ids=ep-run-a,ep-run-b")
    assert status == 200
    data = json.loads(body)
    assert data["missing"] == []
    assert [r["run_id"] for r in data["runs"]] == ["ep-run-a", "ep-run-b"]
    assert data["runs"][0]["params_label"] == "rest sweep=1,4"
    assert data["runs"][1]["params_label"] == "rest sweep=4"
    # header values must not leak into the public payload
    assert "X-Env" not in json.dumps(data)
    assert "trial" not in json.dumps(data)
    # one endpoint section: union of labels N=1 (a only) + N=4 (both)
    (section,) = [s for s in data["sections"] if s["kind"] == "endpoint"]
    assert section["reference_run"] == "ep-run-a"
    assert [row["label"] for row in section["rows"]] == ["N=1", "N=4"]
    n4 = next(row for row in section["rows"] if row["label"] == "N=4")
    assert n4["cells"]["rps"] == {"ep-run-a": 250.0, "ep-run-b": 500.0}
    # delta semantics: positive = improvement; rps doubled -> +100.0
    assert n4["delta_vs_reference"]["rps"] == {"ep-run-b": 100.0}
    # latency halved on a lower-is-better metric -> +50.0 (improvement)
    assert n4["delta_vs_reference"]["lat_p50_ms"] == {"ep-run-b": 50.0}
    assert n4["delta_vs_reference"]["lat_p99_ms"] == {"ep-run-b": 50.0}
    assert n4["delta_vs_reference"]["lat_max_ms"] == {"ep-run-b": 50.0}
    # N=1 exists only for the reference run -> no delta entries at all
    n1 = next(row for row in section["rows"] if row["label"] == "N=1")
    assert n1["cells"]["rps"] == {"ep-run-a": 100.0}
    assert n1["delta_vs_reference"] == {}
    # the reference run itself never carries a delta
    assert "ep-run-a" not in json.dumps(n4["delta_vs_reference"])


def test_compare_reports_missing_ids(compare_app):
    status, body = _call(compare_app, "GET", "/api/results/compare?ids=ep-run-a,nope,ep-run-b")
    assert status == 200
    data = json.loads(body)
    assert data["missing"] == ["nope"]
    assert [r["run_id"] for r in data["runs"]] == ["ep-run-a", "ep-run-b"]


def test_compare_all_unknown_ids_400(compare_app):
    status, body = _call(compare_app, "GET", "/api/results/compare?ids=nope,nada")
    assert status == 400
    assert b"no such run" in body


def test_compare_mixed_kinds_two_sections(compare_app):
    status, body = _call(compare_app, "GET", "/api/results/compare?ids=chat-run-c,ep-run-a,dead-run-d")
    assert status == 200
    data = json.loads(body)
    # the dead run is requested but has metadata; it survives as a row with
    # an error note, not a "missing" id (it EXISTS, it just has no summary)
    assert data["missing"] == []
    dead = next(r for r in data["runs"] if r["run_id"] == "dead-run-d")
    assert dead["rows"] == []
    assert dead["error"] == "no machine-readable summary"
    kinds = [s["kind"] for s in data["sections"]]
    assert kinds == ["endpoint", "chat"]  # endpoint first, then chat
    chat = next(s for s in data["sections"] if s["kind"] == "chat")
    assert chat["reference_run"] == "chat-run-c"
    # repeated (ctx,users,task) rows collapse to the LAST occurrence
    labels = [row["label"] for row in chat["rows"]]
    assert labels == ["ctx=0 users=1 coding", "ctx=0 users=4 mixed"]
    coding = chat["rows"][0]
    assert coding["cells"]["ttft_turn1_p50_ms"] == {"chat-run-c": 800.0}  # last row wins, not 930.6
    assert coding["cells"]["tokens_p50"] == {"chat-run-c": 300.0}
    assert coding["cells"]["ttft_post_p50_ms"] == {"chat-run-c": 500.0}
    assert coding["cells"]["failed"] == {"chat-run-c": 0.0}
    mixed = chat["rows"][1]
    assert mixed["cells"]["failed"] == {"chat-run-c": 2.0}


def test_compare_public_no_auth_header(compare_app):
    """The compare endpoint is part of the public results surface: no key."""
    status, body = _call(compare_app, "GET", "/api/results/compare?ids=ep-run-a,ep-run-b")
    assert status == 200
    assert json.loads(body)["sections"]


def test_compare_knee_carried(compare_app):
    _status, body = _call(compare_app, "GET", "/api/results/compare?ids=ep-run-a,ep-run-b")
    data = json.loads(body)
    a = next(r for r in data["runs"] if r["run_id"] == "ep-run-a")
    assert a["knee"] == {"concurrency": 4, "criterion": "latency", "detail": "p99 3x best"}
    b = next(r for r in data["runs"] if r["run_id"] == "ep-run-b")
    assert b["knee"] is None


# -- unit tests: compare.py pure logic ---------------------------------------


def test_build_comparison_delta_sign_per_direction():
    runs = [
        {
            "run_id": "ref",
            "kind": "endpoint",
            "rows": [{"label": "N=4", "metrics": {"rps": 100.0, "lat_p50_ms": 100.0}}],
        },
        {"run_id": "b", "kind": "endpoint", "rows": [{"label": "N=4", "metrics": {"rps": 200.0, "lat_p50_ms": 50.0}}]},
        {"run_id": "c", "kind": "endpoint", "rows": [{"label": "N=4", "metrics": {"rps": 50.0, "lat_p50_ms": 150.0}}]},
    ]
    out = C.build_comparison(runs)
    (section,) = out["sections"]
    (row,) = section["rows"]
    # rps: higher-is-better -> doubling = +100, halving = -50
    assert row["delta_vs_reference"]["rps"] == {"b": 100.0, "c": -50.0}
    # latency: lower-is-better -> halving = +50 (better), 1.5x = -50 (worse)
    assert row["delta_vs_reference"]["lat_p50_ms"] == {"b": 50.0, "c": -50.0}


def test_build_comparison_info_metrics_have_no_delta():
    runs = [
        {"run_id": "ref", "kind": "endpoint", "rows": [{"label": "N=1", "metrics": {"gpu_util_mean": 30.0}}]},
        {"run_id": "b", "kind": "endpoint", "rows": [{"label": "N=1", "metrics": {"gpu_util_mean": 90.0}}]},
    ]
    out = C.build_comparison(runs)
    (section,) = out["sections"]
    (row,) = section["rows"]
    assert row["cells"]["gpu_util_mean"] == {"ref": 30.0, "b": 90.0}
    assert "gpu_util_mean" not in row["delta_vs_reference"]


def test_build_comparison_mixed_kind_sections_and_label_union():
    runs = [
        {"run_id": "c1", "kind": "chat", "rows": [{"label": "ctx=0 users=1 coding", "metrics": {"tokens_p50": 300.0}}]},
        {"run_id": "e1", "kind": "endpoint", "rows": [{"label": "N=1", "metrics": {"rps": 10.0}}]},
        {
            "run_id": "e2",
            "kind": "endpoint",
            "rows": [{"label": "N=1", "metrics": {"rps": 20.0}}, {"label": "N=8", "metrics": {"rps": 40.0}}],
        },
    ]
    out = C.build_comparison(runs)
    assert [s["kind"] for s in out["sections"]] == ["endpoint", "chat"]
    ep = out["sections"][0]
    assert [row["label"] for row in ep["rows"]] == ["N=1", "N=8"]  # union by first appearance
    assert ep["delta_vs_reference"] if False else ep["rows"][0]["cells"]["rps"] == {"e1": 10.0, "e2": 20.0}
    assert ep["reference_run"] == "e1"
    chat = out["sections"][1]
    assert chat["reference_run"] == "c1"
    assert chat["rows"][0]["delta_vs_reference"] == {}


def test_build_comparison_zero_ref_value_no_delta():
    runs = [
        {"run_id": "ref", "kind": "endpoint", "rows": [{"label": "N=1", "metrics": {"rps": 0.0}}]},
        {"run_id": "b", "kind": "endpoint", "rows": [{"label": "N=1", "metrics": {"rps": 10.0}}]},
    ]
    out = C.build_comparison(runs)
    (row,) = out["sections"][0]["rows"]
    assert row["delta_vs_reference"] == {}  # divide-by-zero guarded


def test_normalize_endpoint_rows_key_shapes():
    doc = {
        "summary": {
            "rows": [
                {
                    "concurrency": 4,
                    "total": 10,
                    "rps": 12.5,
                    "success_rate_pct": 98.0,
                    "lat_p50": 5.0,
                    "lat_p99": 9.0,
                    "lat_max": 11.0,
                    "lat_p99.9": 12.0,
                    "avg_bytes": 100,
                    "window_s": 5,
                },
            ],
            "knee": None,
            "notes": [],
        }
    }
    rows = C.normalize_endpoint_rows(doc)
    (row,) = rows
    assert row["label"] == "N=4"
    # percentile keys normalized to lat_pNN_ms; diagnostic keys dropped
    assert row["metrics"]["lat_p50_ms"] == 5.0
    assert row["metrics"]["lat_p99_ms"] == 9.0
    assert row["metrics"]["lat_p99.9_ms"] == 12.0
    assert "avg_bytes" not in row["metrics"]
    assert "window_s" not in row["metrics"]
    assert "concurrency" not in row["metrics"]


def test_normalize_chat_rows_last_occurrence_wins():
    rows = C.normalize_chat_rows(_CHAT_MD)
    assert [r["label"] for r in rows] == ["ctx=0 users=1 coding", "ctx=0 users=4 mixed"]
    first = rows[0]["metrics"]
    assert first["ttft_turn1_p50_ms"] == 800.0  # LAST of the two repeated rows
    assert first["ttft_turn1_p95_ms"] == 800.0
    assert first["tokens_p50"] == 300.0
    assert first["ttft_post_p50_ms"] == 500.0
    assert first["failed"] == 0.0


def test_normalize_chat_rows_no_table_returns_empty():
    assert C.normalize_chat_rows("# empty\n\nno levels completed yet\n") == []


def test_params_label_shapes():
    assert C.params_label("endpoint", {"mode": "mcp", "sweep": "1,2"}) == "mcp sweep=1,2"
    assert C.params_label("endpoint", {"mode": "rest", "N": 8}) == "rest N=8"
    assert C.params_label("endpoint", {"headers": {"Authorization": "REDACTED"}}) == "rest"
    assert C.params_label("chat", {"number_users": [1, 4], "context_length": "0", "tasks": "coding"}) == (
        "users=1,4 ctx=0 coding"
    )
    assert C.params_label("chat", {}) == "chat"
    # no header values ever reach the label
    assert "secret-value" not in C.params_label("endpoint", {"headers": {"X-Key": "secret-value"}, "mode": "rest"})


def test_runner_compare_data_endpoint(monkeypatch, tmp_path):
    from model_benchmarker.webapp import runner as R

    _fabricate_runs(
        tmp_path,
        [
            (
                "ep-x",
                "endpoint",
                {"mode": "rest", "sweep": "2", "api_key": "sk-super-secret", "headers": {"Auth": "Bearer x"}},
                {"summary": {"rows": [_ep_row(2, 42.0, 3.0, 6.0, 7.0)], "knee": None, "notes": []}},
            )
        ],
    )
    mgr = R.RunManager(tmp_path)
    data = mgr.compare_data("ep-x")
    assert data["kind"] == "endpoint"
    assert data["rows"][0]["metrics"]["rps"] == 42.0
    blob = json.dumps(data)
    assert "sk-super-secret" not in blob
    assert "Bearer x" not in blob
    with pytest.raises(KeyError):
        mgr.compare_data("missing-run")


def test_runner_compare_data_no_summary(monkeypatch, tmp_path):
    from model_benchmarker.webapp import runner as R

    _fabricate_runs(tmp_path, [("dead", "endpoint", {"mode": "rest"}, {})])
    mgr = R.RunManager(tmp_path)
    data = mgr.compare_data("dead")
    assert data["rows"] == []
    assert data["error"] == "no machine-readable summary"


def test_runner_compare_data_chat(monkeypatch, tmp_path):
    from model_benchmarker.webapp import runner as R

    _fabricate_runs(tmp_path, [("ch", "chat", {"number_users": "1"}, {"report_md": _CHAT_MD})])
    mgr = R.RunManager(tmp_path)
    data = mgr.compare_data("ch")
    assert data["kind"] == "chat"
    assert data["rows"][0]["label"] == "ctx=0 users=1 coding"
    assert data["error"] is None


# ---------------------------------------------------------------------------
# results library: the committed results/ tree on /results + lib: compare ids
# ---------------------------------------------------------------------------


@pytest.fixture()
def library_root(tmp_path, monkeypatch):
    """A fake results/ tree: one chat artifact, one memory artifact, one junk
    file, one subdirectory-without-files."""
    root = tmp_path / "results"
    (root / "mymodel").mkdir(parents=True)
    (root / "mymodel" / "H200_sglang_hicachex2.md").write_text(
        "# H200_sglang_hicachex2\n\n| ctx | users | task | failed | TTFT turn1 P50 (ms) | TTFT turn1 P95 (ms) |"
        " TTFT-post P50 (ms) | TTFT-post P95 (ms) | tokens/s P50 | tokens/s P95 |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
        "| 0 | 1 | coding | 0 | 100 | 110 | 50 | 60 | 300 | 280 |\n"
        "| 32768 | 4 | coding | 0 | 200 | 220 | 80 | 90 | 250 | 240 |\n",
        encoding="utf-8",
    )
    (root / "mymodel" / "H200_memory.md").write_text(
        "# Memory estimate: mymodel\n\nGenerated 2026-01-01 00:00 UTC\n\nStatus: ok\n\n"
        "## Memory configuration\n\n| Key | Value |\n|---|---|\n| tool | memory-estimate |\n",
        encoding="utf-8",
    )
    (root / "mymodel" / "NOTES.md").write_text("just prose, no tables\n", encoding="utf-8")
    (root / "emptydir").mkdir()
    monkeypatch.setenv("BENCH_RESULTS_DIR", str(root))
    return root


def test_library_scan_shapes(monkeypatch, tmp_path, library_root):
    from model_benchmarker.webapp.library import scan_library

    lib = scan_library()
    assert lib["root"] == str(library_root)
    assert [m["slug"] for m in lib["models"]] == ["mymodel"]  # empty dir skipped
    kinds = {s["file"]: s["kind"] for s in lib["models"][0]["setups"]}
    assert kinds["H200_sglang_hicachex2.md"] == "chat"
    assert kinds["H200_memory.md"] == "memory"
    assert "NOTES.md" not in kinds  # prose is not machine-readable


def test_library_disabled_without_results_dir(monkeypatch):
    from model_benchmarker.webapp.library import scan_library

    monkeypatch.setenv("BENCH_RESULTS_DIR", "/nonexistent/results-dir")
    lib = scan_library()
    assert lib == {"root": None, "models": []}


def test_library_entry_resolves_and_guards(monkeypatch, tmp_path, library_root):
    from model_benchmarker.webapp.library import library_entry

    e = library_entry("lib:mymodel/H200_sglang_hicachex2")
    assert e is not None and e["kind"] == "chat"
    assert e["rows"] and e["rows"][0]["label"] == "ctx=0 users=1 coding"
    assert "H200" in e["params_label"]  # setup parsed from the filename
    # memory artifacts resolve but carry no comparable rows
    m = library_entry("lib:mymodel/H200_memory")
    assert m is not None and m["kind"] == "memory" and m["rows"] == []
    assert "no latency rows" in (m["error"] or "")
    # guards: missing file, traversal, wrong prefix
    assert library_entry("lib:mymodel/absent_file") is None
    assert library_entry("lib:../../etc/passwd") is None
    assert library_entry("lib:mymodel/../other") is None
    assert library_entry("20260925T155514Z") is None  # not a lib id


def test_compare_route_accepts_lib_ids(monkeypatch, tmp_path, library_root):
    """A library baseline and a fabricated PVC run compare through the one
    endpoint (same chat shape)."""
    work = tmp_path / "w"
    monkeypatch.setenv("BENCH_WORK_DIR", str(work))
    _fabricate_runs(
        work,
        [("fresh-run", "chat", {"number_users": "1"}, {"report_md": _CHAT_MD})],
    )
    app = create_app(str(work))
    status, body = _call(app, "GET", "/api/results/compare?ids=lib:mymodel/H200_sglang_hicachex2,fresh-run")
    assert status == 200
    data = json.loads(body)
    assert data["missing"] == []
    assert [s["kind"] for s in data["sections"]] == ["chat"]
    sec = data["sections"][0]
    assert sec["runs"] == ["lib:mymodel/H200_sglang_hicachex2", "fresh-run"]
    labels = [r["label"] for r in sec["rows"]]
    assert any("ctx=32768 users=4" in lb for lb in labels)  # merged union of levels


def test_compare_route_unknown_lib_id_missing(monkeypatch, tmp_path, library_root):
    work = tmp_path / "w"
    monkeypatch.setenv("BENCH_WORK_DIR", str(work))
    _fabricate_runs(work, [("fresh-run", "chat", {"number_users": "1"}, {"report_md": _CHAT_MD})])
    app = create_app(str(work))
    # 1 unknown lib id + 1 valid run: nothing to compare against -> 400
    # (a single surviving run cannot compare), reported loudly
    status, body = _call(app, "GET", "/api/results/compare?ids=lib:mymodel/absent,fresh-run")
    assert status == 400
    assert b"lib:mymodel/absent" in body
    # a VALID lib id + valid run does compare
    status, body = _call(app, "GET", "/api/results/compare?ids=lib:mymodel/H200_sglang_hicachex2,fresh-run")
    assert status == 200
    assert json.loads(body)["missing"] == []


def test_library_endpoint_public(monkeypatch, tmp_path, library_root):
    monkeypatch.setenv("BENCH_API_KEYS", "some-key")
    monkeypatch.setenv("BENCH_WORK_DIR", str(tmp_path / "w"))
    app = create_app(str(tmp_path / "w"))
    status, body = _call(app, "GET", "/api/library")  # no key header
    assert status == 200
    assert json.loads(body)["models"][0]["slug"] == "mymodel"


# ---------------------------------------------------------------------------
# library seeding: image carries results/, PVC gets a one-time copy
# ---------------------------------------------------------------------------


def test_seed_copies_tree_once(tmp_path, monkeypatch):
    """First launch copies the seed tree into <work>/results; later launches
    are a no-op (the PVC copy is authoritative and survives upgrades)."""

    from model_benchmarker.webapp import library as LIB

    seed = tmp_path / "seed"
    (seed / "mymodel").mkdir(parents=True)
    (seed / "mymodel" / "H200.md").write_text(
        "# run\n\nStatus: complete\n\n| ctx | users |\n|---|---|\n| 0 | 1 |\n", encoding="utf-8"
    )
    (seed / "report.html").write_text("<html></html>", encoding="utf-8")
    monkeypatch.delenv(LIB.RESULTS_DIR_ENV, raising=False)
    monkeypatch.setenv(LIB.SEED_DIR_ENV, str(seed))

    work = tmp_path / "work"
    root = LIB.seed_results_into_workdir(work)
    assert root == work / "results"
    assert (root / "mymodel" / "H200.md").is_file()
    assert not (root / "report.html").exists()  # generated reports never seed

    # second call: no-op, the existing PVC copy wins (mutate it to prove it)
    (root / "mymodel" / "H200.md").write_text("PVC copy", encoding="utf-8")
    again = LIB.seed_results_into_workdir(work)
    assert again == root
    assert (again / "mymodel" / "H200.md").read_text(encoding="utf-8") == "PVC copy"


def test_seed_noop_without_seed_dir(tmp_path, monkeypatch):
    from model_benchmarker.webapp import library as LIB

    monkeypatch.delenv(LIB.RESULTS_DIR_ENV, raising=False)
    monkeypatch.delenv(LIB.SEED_DIR_ENV, raising=False)
    work = tmp_path / "work"
    assert LIB.seed_results_into_workdir(work) is None
    assert not (work / "results").exists()


def test_create_app_seeds_and_sets_env(tmp_path, monkeypatch):
    """create_app wires the seed into startup: after building the app with a
    fresh work dir and a seed tree present, BENCH_RESULTS_DIR points at the
    seeded PVC copy and /api/library serves from it."""
    import os
    import shutil as _sh

    from model_benchmarker.webapp import library as LIB
    from model_benchmarker.webapp.app import create_app

    seed = tmp_path / "seed"
    (seed / "mymodel").mkdir(parents=True)
    (seed / "mymodel" / "H200.md").write_text(
        "# run\n\nStatus: complete\n\n| ctx | users | task | failed | TTFT turn1 P50 (ms) | TTFT turn1 P95 (ms) | TTFT turn1 P99 (ms) | TTFT turn1 P100 (ms) | TTFT-post P50 (ms) | TTFT-post P95 (ms) | TTFT-post P99 (ms) | TTFT-post P100 (ms) | tokens/s P50 | tokens/s P95 | tokens/s P99 | tokens/s P100 |\n|:---|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n| 0 | 1 | coding | 0 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 | 1 |\n",
        encoding="utf-8",
    )
    monkeypatch.delenv(LIB.RESULTS_DIR_ENV, raising=False)
    monkeypatch.setenv(LIB.SEED_DIR_ENV, str(seed))
    monkeypatch.setenv("BENCH_WORK_DIR", str(tmp_path / "work"))

    create_app()
    env_after = os.environ.get(LIB.RESULTS_DIR_ENV)
    assert env_after == str(tmp_path / "work" / "results")
    assert (tmp_path / "work" / "results" / "mymodel" / "H200.md").is_file()
    _sh.rmtree(tmp_path / "seed")  # seed must not be needed again


# ---------------------------------------------------------------------------
# measurement store (MB-DB): atoms -> queryable rows, idempotent recording
# ---------------------------------------------------------------------------


def test_store_records_run_and_serves_summary(compare_app):
    status, body = _call(compare_app, "GET", "/api/store")
    assert status == 200
    data = json.loads(body)
    # the compare_app fixture's runs were NOT completed through _supervise, so
    # nothing is auto-recorded yet — the summary is well-formed zeros
    assert data == {"measurements": 0, "models": [], "runs": 0}


def test_store_record_run_atoms_idempotent(compare_app):
    import os

    from model_benchmarker import store as S

    work = os.environ["BENCH_WORK_DIR"]
    from model_benchmarker.webapp.runner import RunManager

    manager = RunManager(Path(work))
    result = manager._record_run_atoms("chat-run-c")
    # _CHAT_MD has 3 table rows (incl. a repeated (0,1,coding) later level);
    # the store keeps every occurrence — dedupe-to-latest is a VIEW concern
    assert result is not None and result[0] == 3, result
    # idempotent: same content -> 0 new
    again = manager._record_run_atoms("chat-run-c")
    assert again == (0, 3)
    # rows carry full provenance
    db = Path(work) / "atoms.db"
    rows = S.query(db, "SELECT * FROM measurements WHERE run_id='chat-run-c' ORDER BY id")
    assert len(rows) == 3
    r = rows[0]
    assert r["kind"] == "chat"
    assert r["model"] is None  # fixture artifact carries no model identity — honest NULL
    assert r["ctx"] == 0 and r["task"] == "coding" and r["ttft_p50_ms"] == 930.6  # FIRST occurrence
    assert r["run_status"] == "success" and r["source"].endswith("report.md")
    # the summary endpoint now reflects the store
    _status, body = _call(compare_app, "GET", "/api/store")
    data = json.loads(body)
    assert data["measurements"] == 3 and data["runs"] == 1


def test_store_dead_run_records_nothing(compare_app):
    import os

    from model_benchmarker.webapp.runner import RunManager

    work = os.environ["BENCH_WORK_DIR"]
    manager = RunManager(Path(work))
    assert manager._record_run_atoms("dead-run-d") is None  # no artifacts, no raise
    assert manager._record_run_atoms("no-such-run") is None  # unknown id


def test_store_query_filters(compare_app):
    import os

    from model_benchmarker.webapp.runner import RunManager

    work = os.environ["BENCH_WORK_DIR"]
    RunManager(Path(work))._record_run_atoms("chat-run-c")
    # the fixture artifact carries no model bullet/param -> model is NULL
    # (honest unknown; _extract_model never invents one) — filter by run_id
    _status, body = _call(compare_app, "GET", "/api/store/query?run_id=chat-run-c")
    data = json.loads(body)
    assert data["total"] == 3 and len(data["rows"]) == 3
    _status, body = _call(compare_app, "GET", "/api/store/query?run_id=chat-run-c&limit=1")
    data = json.loads(body)
    assert len(data["rows"]) == 1 and data["total"] == 3
    _status, body = _call(compare_app, "GET", "/api/store/query?model=nobody")
    assert json.loads(body)["total"] == 0


# ---------------------------------------------------------------------------
# store auto-recording on run completion (MB-DB in the run lifecycle):
# _supervise -> _record_run_atoms -> /api/store, terminal-only, fire-and-forget
# Appended tests only; the fixtures above are the Lead's.
# ---------------------------------------------------------------------------

_CHAT_STORE_MD = """# store test run

- Model: PcaitChat name='demo-model-7b'

Per-level latency / throughput (multiturn):

| ctx | users | task | failed | TTFT turn1 P50 (ms) | TTFT turn1 P95 (ms) | TTFT turn1 P99 (ms) | TTFT turn1 P100 (ms) | TTFT-post P50 (ms) | TTFT-post P95 (ms) | TTFT-post P99 (ms) | TTFT-post P100 (ms) | tokens/s P50 | tokens/s P95 | tokens/s P99 | tokens/s P100 |
|:---|:---|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4096 | 1 | coding | 0 | 100.5 | 110.5 | 120.5 | 130.5 | 50.0 | 60.0 | 70.0 | 80.0 | 300.0 | 290.0 | 280.0 | 270.0 |
| 4096 | 4 | coding | 1 | 200.5 | 210.5 | 220.5 | 230.5 | 90.0 | 95.0 | 99.0 | 105.0 | 150.0 | 140.0 | 130.0 | 120.0 |
"""


def _store_manager(work: Path) -> R.RunManager:
    """A RunManager over the fabricated work dir (same adoption path as create_app)."""
    return R.RunManager(work)


def _supervise_terminal(manager: R.RunManager, run_id: str, status: str = "success") -> None:
    """Drive a fabricated run through the REAL _supervise teardown, synchronously:
    a trivial subprocess plays the bench process (exit 0), then the finally
    block does exactly what production does -- finished_utc, _write_meta,
    and the store recording. Deterministic (no sleeps, no daemon thread)."""
    rec = manager._require(run_id)
    manager._supervise(rec, [sys.executable, "--version"], {})
    assert rec.status == status, rec.status


def test_store_lifecycle_chat_run_records_terminal_artifact(tmp_path):
    """A run driven through _supervise's real teardown lands in the store
    exactly once, with model/ctx/users/task and metric columns read from the
    FINAL report.md (all levels)."""
    from model_benchmarker import store as S

    work = tmp_path / "w"
    _fabricate_runs(
        work,
        [("store-chat-1", "chat", {"number_users": "1,4"}, {"report_md": _CHAT_STORE_MD})],
    )
    manager = _store_manager(work)
    rec_before = manager._require("store-chat-1")
    started_before = rec_before.started_utc
    _supervise_terminal(manager, "store-chat-1")

    # the supervise teardown recorded the run automatically
    data = json.loads(_call(create_app(str(work)), "GET", "/api/store")[1])
    assert data == {"measurements": 2, "models": ["demo-model-7b"], "runs": 1}
    rows = S.query(
        work / "atoms.db",
        "SELECT model, ctx, users, task, failed, ttft_p50_ms, tps_p50, ttft_post_p50_ms, itl_p50_ms, run_status, target_url"
        " FROM measurements WHERE run_id=? ORDER BY users",
        ("store-chat-1",),
    )
    assert len(rows) == 2
    first, second = rows
    # the report's "- Model: ..." header bullet is the model identity
    assert first["model"] == "demo-model-7b"
    assert (first["ctx"], first["users"], first["task"]) == (4096, 1, "coding")
    assert first["failed"] == 0 and first["ttft_p50_ms"] == 100.5 and first["tps_p50"] == 300.0
    assert first["ttft_post_p50_ms"] == 50.0 and first["itl_p50_ms"] is None
    assert second["users"] == 4 and second["failed"] == 1 and second["ttft_p50_ms"] == 200.5
    # terminal status from the record, and provenance from run_meta
    assert all(r["run_status"] == "success" for r in rows)
    assert all(r["target_url"] == "http://m:8000" for r in rows)
    # a re-supervised terminal run records nothing new (idempotent store)
    _supervise_terminal(manager, "store-chat-1")
    data2 = json.loads(_call(create_app(str(work)), "GET", "/api/store")[1])
    assert data2["measurements"] == 2 and data2["runs"] == 1
    # started_utc provenance came from the adopted record
    assert manager._require("store-chat-1").started_utc == started_before


def test_store_supervise_records_endpoint_run_json(tmp_path):
    """Endpoint runs record from run.json's summary rows."""
    from model_benchmarker import store as S

    work = tmp_path / "w"
    _fabricate_runs(
        work,
        [
            (
                "store-ep-1",
                "endpoint",
                {"mode": "rest", "N": "2"},
                {"summary": {"rows": [_ep_row(2, 42.0, 3.0, 6.0, 7.0, gpu_util_mean=55.0)], "knee": None, "notes": []}},
            )
        ],
    )
    manager = _store_manager(work)
    _supervise_terminal(manager, "store-ep-1")

    rows = S.query(
        work / "atoms.db",
        "SELECT kind, users, task, rps, lat_p50_ms, lat_max_ms, gpu_util_mean, mode, source"
        " FROM measurements WHERE run_id=?",
        ("store-ep-1",),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "endpoint" and row["users"] == 2 and row["task"] == "N=2"
    assert row["rps"] == 42.0 and row["lat_p50_ms"] == 3.0 and row["lat_max_ms"] == 7.0
    assert row["gpu_util_mean"] == 55.0 and row["mode"] == "rest" and row["source"].endswith("run.json")


def test_store_recording_failure_never_breaks_supervision(tmp_path, capsys):
    """A store failure (unwritable db path) is fire-and-forget: the run still
    reaches its terminal status and run_meta.json is written; the failure is
    logged, not raised."""
    work = tmp_path / "w"
    _fabricate_runs(
        work,
        [("store-chat-2", "chat", {"number_users": "1"}, {"report_md": _CHAT_STORE_MD})],
    )
    manager = _store_manager(work)
    # make the store path unwritable: a DIRECTORY where atoms.db must go
    db_path = manager._store_path()
    db_path.mkdir()
    _supervise_terminal(manager, "store-chat-2")
    rec = manager._require("store-chat-2")
    assert rec.status == "success" and rec.finished_utc is not None  # supervision unaffected
    assert (work / "runs" / "store-chat-2" / "run_meta.json").is_file()
    assert "recording failed" in capsys.readouterr().err
    # the method itself also refuses to raise when called directly
    assert manager._record_run_atoms("store-chat-2") is None


def test_store_intermediate_state_never_recorded(tmp_path):
    """The store reflects the artifact on disk at COMPLETION time: the only
    record point is _supervise's teardown (AFTER _write_meta). A run whose
    record is still 'running' has recorded nothing."""
    from model_benchmarker import store as S

    work = tmp_path / "w"
    _fabricate_runs(
        work,
        [("store-chat-3", "chat", {"number_users": "1"}, {"report_md": _CHAT_STORE_MD})],
    )
    manager = _store_manager(work)
    # _supervise sets the terminal status BEFORE the finally block; probe the
    # not-yet-recorded window directly: a 'running' record + final artifact
    # on disk would STILL not be recorded, because recording happens only in
    # the teardown. Simulate by clearing the record and re-checking the store
    # is untouched before the supervise call:
    assert S.query(work / "atoms.db", "SELECT * FROM measurements") == []
    _supervise_terminal(manager, "store-chat-3")
    # after the teardown the store has exactly the final rows (both levels)
    assert S.query(work / "atoms.db", "SELECT COUNT(*) AS n FROM measurements")[0]["n"] == 2
    # a second teardown pass adds nothing (idempotent; no double-insert)
    _supervise_terminal(manager, "store-chat-3")
    assert S.query(work / "atoms.db", "SELECT COUNT(*) AS n FROM measurements")[0]["n"] == 2


def test_store_stats_on_fresh_path_returns_zeros(tmp_path):
    """/api/store works before any run has ever completed: stats creates the
    schema on a fresh path and reports well-formed zeros."""
    work = tmp_path / "brand-new"
    assert not (work / "atoms.db").exists()
    app = create_app(str(work))
    status, body = _call(app, "GET", "/api/store")  # no key: public surface
    assert status == 200
    assert json.loads(body) == {"measurements": 0, "models": [], "runs": 0}
    assert (work / "atoms.db").is_file()  # schema created on demand


def test_target_aliases_bridge_serving_and_origin():
    """MB-E suggester fix: the combobox carries the SERVING url, run records
    store the RESOLVED in-cluster origin — target_aliases must make the two
    meet (the resume suggestion silently never fired without this)."""
    from model_benchmarker.webapp.runner import target_aliases

    serving = "https://m.serving.pcai.example.net"
    origin = "http://m.svc.cluster.local"
    rec = target_aliases(origin)  # from /api/results rows
    typed = target_aliases(serving)  # what the user picked
    assert {a.rstrip("/").lower() for a in rec} & {a.rstrip("/").lower() for a in typed}
    # non-serving URLs pass through unchanged (no rewrite invented)
    plain = target_aliases("http://x.example:8000")
    assert "http://x.example:8000" in plain
    # malformed input: empty alias list, never a raise (the JS falls back
    # to [target_url] when the field is absent/empty)
    assert target_aliases("not a url") == []


def test_public_index_carries_target_aliases(compare_app):
    _status, body = _call(compare_app, "GET", "/api/results")
    data = json.loads(body)
    row = next(r for r in data["runs"] if r["run_id"] == "chat-run-c")
    assert isinstance(row["target_aliases"], list) and row["target_aliases"]
    # the resolved origin itself is among the aliases
    assert any(row["target_url"].rstrip("/").lower() == a.rstrip("/").lower() for a in row["target_aliases"])
    # every row carries a model identity field (None when the artifact is
    # anonymous — the UI renders a dash, never a guess)
    assert all("model" in r for r in data["runs"])


# ---------------------------------------------------------------------------
# MB-DB faceted atom browser: whitelisted filters + facets tree
# ---------------------------------------------------------------------------


def test_store_build_filters_whitelists_and_binds():
    from model_benchmarker import store as S

    where, params = S.build_filters({"model": "deepseek-v4.1-flash", "ctx": "0", "users": "4", "hicache": "2"})
    assert where == "model = ? AND ctx = ? AND users = ? AND hicache = ?"
    assert params == ["deepseek-v4.1-flash", 0, 4, 2]  # numerics bound as ints
    # unknown keys ignored, empty = no filter, non-numeric numeric-col ignored
    where2, params2 = S.build_filters({"model": "", "DROP": "x", "ctx": "abc", "kind": "chat"})
    assert where2 == "kind = ?" and params2 == ["chat"]


def test_store_facets_tree_shape(compare_app):
    import os

    from model_benchmarker.webapp.runner import RunManager

    work = os.environ["BENCH_WORK_DIR"]
    RunManager(Path(work))._record_run_atoms("chat-run-c")
    _status, body = _call(compare_app, "GET", "/api/store/facets")
    data = json.loads(body)
    # the fixture artifact carries no model identity -> the facets tree is
    # empty (unknown-model rows are not navigable) but well-formed
    assert data["total"] == 3 and data["models"] == []


def test_store_facets_groups_by_setup(tmp_path, monkeypatch):
    """A store with setup-stamped rows produces model -> setup -> tests."""
    from model_benchmarker import atomize as A
    from model_benchmarker import store as S

    db = tmp_path / "atoms.db"
    md = (
        "| ctx | users | task | failed | TTFT P50/P95/P99/P100 (ms) | tokens/s P50/P95/P99/P100 |\n"
        "|:---|:---|:---|:---|---:|---:|\n"
        "| 0 | 1 | coding | 0 | 100.0 / 110.0 / 120.0 / 130.0 | 300.0 / 290.0 / 280.0 / 270.0 |\n"
        "| 32768 | 32 | coding | 0 | 200.0 / 210.0 / 220.0 / 230.0 | 40.0 / 39.0 / 38.0 / 37.0 |\n"
    )
    atoms = A.atomize_chat_report(md, "results/m1/H200x4_hicachex2.md", {"run_id": "r", "params": {"model": "m1"}})
    setup = {"gpu": "NVIDIA H200 (PCIe)", "gpu_count": 4, "engine": "SGLang", "hicache": 2}
    S.record_atoms(db, atoms, source="results/m1/x.md", setup=setup)
    f = S.facets(db)
    assert f["total"] == 2 and f["models"][0]["model"] == "m1"
    s = f["models"][0]["setups"][0]
    assert s["gpu"] == "NVIDIA H200 (PCIe)" and s["gpu_count"] == 4 and s["hicache"] == 2
    assert s["count"] == 2 and len(s["tests"]) == 2
    tests = {(t["ctx"], t["users"], t["task"]) for t in s["tests"]}
    assert tests == {(0, 1, "coding"), (32768, 32, "coding")}
    # filtering through the same facets the browser uses
    where, params = S.build_filters({"model": "m1", "ctx": "32768", "gpu_count": "4"})
    rows = S.query(db, f"SELECT * FROM measurements WHERE {where}", tuple(params))
    assert len(rows) == 1 and rows[0]["users"] == 32


def test_target_aliases_endpoint_and_js_semantics(compare_app):
    """Adversarial-review BUG 2 regression: the suggester must bridge a
    SERVING-form typed url to LOCAL-origin records. The endpoint exposes the
    server's alias map; the JS matches each alias (normalized)."""
    _status, body = _call(compare_app, "GET", "/api/target-aliases?url=https://m.serving.pcai.example.net")
    data = json.loads(body)
    assert data["aliases"], data
    # the local rewrite of a .serving. url is among the aliases
    assert any("svc.cluster.local" in a for a in data["aliases"]), data["aliases"]
    # JS semantics: any record alias in the typed-alias set (normalized)
    record_aliases = ["http://m.svc.cluster.local:80"]
    typed = {a.rstrip("/").lower() for a in data["aliases"]}
    assert any(a.rstrip("/").lower() in typed for a in record_aliases)
    # bad input -> 400, not 500
    _status, _body = _call(compare_app, "GET", "/api/target-aliases?url=")
    assert _status == 400


def test_store_query_bad_limit_400(compare_app):
    """Adversarial-review BUG 3 regression: limit=abc must be a 400, not a
    500 (the old code raised ValueError out of the handler)."""
    status, _body = _call(compare_app, "GET", "/api/store/query?limit=abc")
    assert status == 400
    status, body = _call(compare_app, "GET", "/api/store/query?limit=10")
    assert status == 200 and json.loads(body)["limit"] == 10


def test_compare_data_carries_model_identity(tmp_path, monkeypatch):
    """The compare cards must name the MODEL (from the artifact's own header
    bullet), not fall back to task tokens — 'coding,creative arrival=closed'
    identifies nothing."""
    from model_benchmarker.webapp.runner import RunManager

    work = tmp_path / "w"
    work.mkdir()
    runs_dir = work / "runs"

    def fabricate(run_id, report_text, params):
        d = runs_dir / run_id
        d.mkdir(parents=True)
        (d / "run_meta.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "kind": "chat",
                    "endpoint": "http://m:8000",
                    "target_url": "http://m:8000",
                    "params": params,
                    "started_utc": "2026-01-01T00:00:00+00:00",
                    "status": "success",
                    "exit_code": 0,
                    "finished_utc": None,
                }
            ),
            encoding="utf-8",
        )
        (d / "report.md").write_text(report_text, encoding="utf-8")

    # 1) artifact with a model bullet -> bullet wins
    bullet_md = "# t\n\n- Model: ChatModel name='deepseek-v4.1-flash'\n\n" + _CHAT_MD.split("\n", 3)[3]
    fabricate("chat-bullet", bullet_md, {"number_users": "1", "model": "ignored-fallback"})
    # 2) no bullet, params.model -> fallback
    fabricate("chat-param", _CHAT_MD, {"number_users": "1", "model": "qwen3-32b"})
    # 3) neither -> honest None
    fabricate("chat-anon", _CHAT_MD, {"number_users": "1"})

    mgr = RunManager(work)
    assert mgr.compare_data("chat-bullet")["model"] == "deepseek-v4.1-flash"
    assert mgr.compare_data("chat-param")["model"] == "qwen3-32b"
    assert mgr.compare_data("chat-anon")["model"] is None


def test_public_index_fuses_same_experiment_partials(tmp_path, monkeypatch):
    """Partial runs of the SAME experiment collapse to one row: the one with
    the most completed rows represents; fused_from lists the absorbed ids.
    Different sweep shapes never fuse; running runs never fuse."""
    from model_benchmarker.webapp.runner import RunManager

    work = tmp_path / "w"
    (work / "runs").mkdir(parents=True)
    sweep = {
        "number_users": "1,4,32,64",
        "context_length": "0,32768",
        "requests_per_user": 5,
        "tasks": "coding,creative",
        "multiturn": True,
        "separate_tasks": True,
    }

    seq = {"n": 0}

    def fabricate(run_id, status, rows):
        seq["n"] += 1
        d = work / "runs" / run_id
        d.mkdir()
        params = dict(sweep)
        if run_id == "r-diff-shape":
            params["tasks"] = "mixed"  # genuinely different experiment
        if run_id == "r-diff-sampling":
            params["temperature"] = 0.7  # different sampling -> different experiment
        (d / "run_meta.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "kind": "chat",
                    "endpoint": "http://ds:8000",
                    "target_url": "http://ds:8000",
                    "params": params,
                    "started_utc": f"2026-01-0{seq['n']}T00:00:00+00:00",
                    "status": status,
                    "exit_code": -15,
                    "finished_utc": None,
                }
            ),
            encoding="utf-8",
        )
        header = "| ctx | users | task | failed | TTFT P50/P95/P99/P100 (ms) | tokens/s P50/P95/P99/P100 |\n|:---|:---|:---|:---|---:|---:|\n"
        body = "".join(
            f"| 0 | {u} | coding | 0 | {100 + i}.0 / 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 / 1.0 |\n"
            for i, u in enumerate([1] * rows)
        )
        (d / "report.md").write_text("# t\n\n" + header + body, encoding="utf-8")

    fabricate("r-2rows", "timeout", 2)  # representative (most rows)
    fabricate("r-1row", "cancelled", 1)
    fabricate("r-0rows", "interrupted", 0)
    fabricate("r-running", "running", 1)  # scanned -> interrupted: fuses
    fabricate("r-diff-shape", "timeout", 5)  # different tasks -> never fuses
    fabricate("r-diff-sampling", "timeout", 4)  # different sampling -> never fuses
    # NOTE: a SCANNED meta with status "running" is crash-recovered to
    # "interrupted" (_scan_workdir, pre-existing) — fusion-eligible by design;
    # the never-fuse-while-running path is asserted via a LIVE record below.

    monkeypatch.setenv("BENCH_WORK_DIR", str(work))
    mgr = RunManager(work)
    from model_benchmarker.webapp.runner import RunRecord

    # live in-memory running record: never fuses
    mgr._runs["r-live-running"] = RunRecord(
        run_id="r-live-running",
        kind="chat",
        endpoint="http://ds:8000",
        target_url="http://ds:8000",
        params=dict(sweep),
        started_utc="2026-01-06T00:00:00+00:00",
        status="running",
    )
    idx = mgr.public_index()
    by_id = {r["run_id"]: r for r in idx}
    assert by_id["r-2rows"]["fused_count"] == 4, by_id.get("r-2rows")
    assert sorted(by_id["r-2rows"]["fused_from"]) == ["r-0rows", "r-1row", "r-running"]
    assert by_id["r-2rows"]["status"] == "timeout"  # group's worst-case status
    assert "fused_from" not in by_id["r-diff-shape"]
    assert "fused_from" not in by_id["r-diff-sampling"]
    assert "fused_from" not in by_id["r-live-running"]
    # no duplicates of the fused members at top level
    assert sum(1 for r in idx if r["run_id"] in ("r-1row", "r-0rows")) == 0
    # fuse=False restores the flat listing
    flat = mgr.public_index(fuse=False)
    assert sum(1 for r in flat if r["run_id"] == "r-1row") == 1


def test_public_index_fuses_resumed_roster_subset(tmp_path, monkeypatch):
    """The engine's own resume contract: number_users/context_length ROSTERS
    are sweep progress, not experiment identity. A resume of a 1,4,32,64 x
    0,32768 sweep launched with roster 32,64 x 32768 must fuse with its
    source (this is the real deepseek-v4.1-flash trio on the deployment)."""
    from model_benchmarker.webapp.runner import RunManager

    work = tmp_path / "w"
    (work / "runs").mkdir(parents=True)
    base = {
        "requests_per_user": "5",
        "tasks": "coding,creative",
        "arrival_mode": "closed",
        "multiturn": True,
        "no_nonce": False,
        "separate_tasks": True,
    }

    def fabricate(run_id, status, users, ctx, rows, started, target):
        d = work / "runs" / run_id
        d.mkdir()
        (d / "run_meta.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "kind": "chat",
                    "endpoint": target,
                    "target_url": target,
                    "params": {**base, "number_users": users, "context_length": ctx},
                    "started_utc": started,
                    "status": status,
                    "exit_code": 0,
                    "finished_utc": None,
                }
            ),
            encoding="utf-8",
        )
        header = "| ctx | users | task | failed | TTFT P50/P95/P99/P100 (ms) | tokens/s P50/P95/P99/P100 |\n|:---|:---|:---|:---|---:|---:|\n"
        body = "".join(
            f"| 0 | {u} | coding | 0 | {100 + i}.0 / 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 / 1.0 |\n"
            for i, u in enumerate([1] * rows)
        )
        text = "# report\n\n- Model: ChatModel name='deepseek-v4.1-flash' usage=local\n\n" + header + body
        (d / "report.md").write_text(text, encoding="utf-8")

    t1 = "http://ds-v1:8000"
    t2 = "http://ds-v2:8000"  # the model redeployed under a new endpoint
    fabricate("ds-failed", "failed", "1,4,32,64", "0,32768", 0, "2026-01-01T00:00:00+00:00", t1)
    fabricate("ds-timeout", "timeout", "1,4,32,64", "0,32768", 13, "2026-01-02T00:00:00+00:00", t2)
    fabricate("ds-resume", "success", "32,64", "32768", 16, "2026-01-03T00:00:00+00:00", t2)

    monkeypatch.setenv("BENCH_WORK_DIR", str(work))
    mgr = RunManager(work)
    idx = mgr.public_index()
    by_id = {r["run_id"]: r for r in idx}
    # the trio is ONE group; the 16-row completed resume represents it and
    # carries the model name for the results table
    assert sorted(by_id["ds-resume"]["fused_from"]) == ["ds-failed", "ds-timeout"], by_id.get("ds-resume")
    assert by_id["ds-resume"]["model"] == "deepseek-v4.1-flash"
    assert sum(1 for r in idx if r["run_id"].startswith("ds-")) == 1


def test_public_index_fuses_by_model_across_targets(tmp_path, monkeypatch):
    """Fusion identity is the MODEL, not the resolved target: a re-run of the
    same experiment against a re-deployed endpoint of the same model (new
    URL, same sweep shape) collapses into one listing whose compare rows are
    the latest measurement per test condition. Different models with the
    same sweep shape on the same URL are DIFFERENT experiments."""
    from model_benchmarker.webapp.runner import RunManager

    work = tmp_path / "w"
    (work / "runs").mkdir(parents=True)
    sweep = {
        "number_users": "1,4",
        "context_length": "0",
        "requests_per_user": 5,
        "tasks": "coding,creative",
        "multiturn": True,
        "separate_tasks": True,
    }
    header = (
        "| ctx | users | task | failed | TTFT P50/P95/P99/P100 (ms) | tokens/s P50/P95/P99/P100 |\n"
        "|:---|:---|:---|:---|---:|---:|\n"
    )

    def fabricate(run_id, status, target, model_bullet, rows, started):
        d = work / "runs" / run_id
        d.mkdir()
        (d / "run_meta.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "kind": "chat",
                    "endpoint": target,
                    "target_url": target,
                    "params": dict(sweep),
                    "started_utc": started,
                    "status": status,
                    "exit_code": 0,
                    "finished_utc": None,
                }
            ),
            encoding="utf-8",
        )
        body = "".join(
            f"| 0 | {u} | coding | 0 | {100.0 + i}.0 / 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 / 1.0 |\n"
            for i, u in enumerate([1] * rows)
        )
        text = "# t\n\n" + (f"- Model: ChatModel name='{model_bullet}'\n\n" if model_bullet else "") + header + body
        (d / "report.md").write_text(text, encoding="utf-8")

    # same model, DIFFERENT targets (v1 endpoint vs the redeployed v2) —
    # fuses by model; the newer attempt carries the most rows so it represents
    fabricate("ds-old", "timeout", "http://deepseek-v1:8000", "deepseek-v4.1-flash", 2, "2026-01-01T00:00:00+00:00")
    fabricate("ds-new", "success", "http://deepseek-v2:8000", "deepseek-v4.1-flash", 4, "2026-01-02T00:00:00+00:00")
    # same target + shape, DIFFERENT model — never fuses with the above
    fabricate("qwen-same-url", "success", "http://deepseek-v2:8000", "qwen3-32b", 3, "2026-01-03T00:00:00+00:00")
    # anonymous runs (no bullet, no params.model) fuse only with themselves
    fabricate("anon-a", "timeout", "http://anon:8000", "", 1, "2026-01-04T00:00:00+00:00")
    fabricate("anon-b", "success", "http://anon:8000", "", 2, "2026-01-05T00:00:00+00:00")

    monkeypatch.setenv("BENCH_WORK_DIR", str(work))
    mgr = RunManager(work)
    idx = mgr.public_index()
    by_id = {r["run_id"]: r for r in idx}

    # the deepseek group fused across both targets; newest/most-complete represents
    assert sorted(by_id["ds-new"]["fused_from"]) == ["ds-old"], by_id.get("ds-new")
    assert by_id["ds-new"]["model"] == "deepseek-v4.1-flash"
    # different model on the same URL stands alone
    assert "fused_from" not in by_id["qwen-same-url"]
    assert by_id["qwen-same-url"]["model"] == "qwen3-32b"
    # anonymous runs keyed by target: they fuse with each other (same URL+shape)
    assert sorted(by_id["anon-b"]["fused_from"]) == ["anon-a"]
    assert by_id["anon-b"]["model"] is None
    assert sum(1 for r in idx if r["run_id"] in ("ds-old", "anon-a")) == 0


def test_public_index_anonymous_run_never_claims_a_model(tmp_path, monkeypatch):
    """Identity honesty under model-keyed fusion: a run whose report names no
    model and carries no params.model is keyed by its resolved target, so it
    fuses only with other runs of the SAME anonymous target and never
    absorbs (or inherits a name from) a run that names itself — a nameless
    artifact must not silently become a named model's entry."""
    from model_benchmarker.webapp.runner import RunManager

    work = tmp_path / "w"
    (work / "runs").mkdir(parents=True)
    sweep = {
        "number_users": "1",
        "context_length": "0",
        "requests_per_user": 5,
        "tasks": "coding",
        "multiturn": True,
        "separate_tasks": True,
    }
    header = (
        "| ctx | users | task | failed | TTFT P50/P95/P99/P100 (ms) | tokens/s P50/P95/P99/P100 |\n"
        "|:---|:---|:---|:---|---:|---:|\n"
    )

    def fabricate(run_id, status, named, rows, started):
        d = work / "runs" / run_id
        d.mkdir()
        (d / "run_meta.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "kind": "chat",
                    "endpoint": "http://ds:8000",
                    "target_url": "http://ds:8000",
                    "params": dict(sweep),
                    "started_utc": started,
                    "status": status,
                    "exit_code": 0,
                    "finished_utc": None,
                }
            ),
            encoding="utf-8",
        )
        body = "".join(
            f"| 0 | 1 | coding | 0 | {100.0 + i}.0 / 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 / 1.0 |\n" for i in range(rows)
        )
        text = "# t\n\n" + ("- Model: ChatModel name='glm-5.3-flash'\n\n" if named else "") + header + body
        (d / "report.md").write_text(text, encoding="utf-8")

    fabricate("rep-anon", "success", False, 3, "2026-01-01T00:00:00+00:00")  # most rows, nameless
    fabricate("src-named", "timeout", True, 1, "2026-01-02T00:00:00+00:00")  # names the model

    monkeypatch.setenv("BENCH_WORK_DIR", str(work))
    mgr = RunManager(work)
    rows = mgr.public_index()
    by_id = {r["run_id"]: r for r in rows}
    # separate entries, each honest: the anonymous one stays anonymous...
    assert by_id["rep-anon"]["model"] is None
    assert "fused_from" not in by_id["rep-anon"]
    # ...and the named one is never absorbed into the anonymous run's entry
    assert by_id["src-named"]["model"] == "glm-5.3-flash"
    assert "fused_from" not in by_id["src-named"]


# ---------------------------------------------------------------------------
# /api/models/search (HF autocomplete) + throughput payload + weight_dtype
# ---------------------------------------------------------------------------


def test_models_search_short_query_short_circuits(gated_app):
    status, body = _call(gated_app, "GET", "/api/models/search?q=a")
    assert status == 200
    assert json.loads(body) == {"models": [], "query": "a"}


def test_models_search_mocked_hub(monkeypatch, gated_app):
    """The endpoint proxies the hub API and maps modelId -> repo (fail-soft)."""
    import model_benchmarker.webapp.app as appmod

    monkeypatch.setattr(
        appmod,
        "_https_fetch_json_url",
        lambda url, timeout=10.0: (
            [
                {"modelId": "Qwen/Qwen3-8B", "downloads": 1000, "likes": 50, "pipeline_tag": "text-generation"},
                {"modelId": "bad"},  # no slash -> filtered
                {"id": "org/only-id", "downloads": 1, "likes": 0},
            ],
            "",
        ),
    )
    status, body = _call(gated_app, "GET", "/api/models/search?q=qwen3&limit=10")
    assert status == 200
    data = json.loads(body)
    assert [m["repo"] for m in data["models"]] == ["Qwen/Qwen3-8B", "org/only-id"]
    assert data["models"][0]["downloads"] == 1000


def test_models_search_offline_fails_soft(monkeypatch, gated_app):
    import model_benchmarker.webapp.app as appmod

    monkeypatch.setattr(appmod, "_https_fetch_json_url", lambda url, timeout=10.0: (None, "connect timeout"))
    status, body = _call(gated_app, "GET", "/api/models/search?q=llama")
    assert status == 200  # never 500s: the combobox degrades to catalog-only
    data = json.loads(body)
    assert data["models"] == [] and "timeout" in (data.get("error") or "")


# ---------------------------------------------------------------------------
# measured benchmarks (replaces the removed throughput roofline)
# ---------------------------------------------------------------------------


def _lib_payload():
    """A scan_library-shaped payload: one real chat run, one memory artifact."""
    return {
        "root": "/tmp/results",
        "models": [
            {
                "slug": "deepseek_v4_flash_0731",
                "name": "Deepseek V4 Flash 0731",
                "setups": [
                    {
                        "id": "lib:deepseek_v4_flash_0731/H200Sx4_hicachex2",
                        "kind": "chat",  # MEASURED -> eligible
                        "file": "H200Sx4_hicachex2.md",
                        "label": "H200Sx4_hicachex2",
                        "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                        "setup": "NVIDIA H200 (PCIe) x4 · HiCache x2",
                        "gpu": "NVIDIA H200 (PCIe)",
                        "gpu_count": 4,
                        "engine": "sglang",
                        "weights": "FP8",
                        "hicache": 2,
                        "replicas": 1,
                        "status": "complete",
                        "rows": [
                            {
                                "label": "ctx=0 users=1 coding",
                                "metrics": {"tokens_p50": 331.1, "ttft_turn1_p50_ms": 930.6},
                            },
                            {
                                "label": "ctx=32768 users=32 mixed",
                                "metrics": {"tokens_p50": 57.6, "ttft_turn1_p50_ms": 1815.3},
                            },
                        ],
                    },
                    {
                        "id": "lib:deepseek_v4_flash_0731/H200PCIEx4_tp4_memory",
                        "kind": "memory",  # the estimator's OWN output -> must never be quoted back
                        "file": "H200PCIEx4_tp4_memory.md",
                        "label": "H200PCIEx4_tp4_memory",
                        "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                        "setup": "NVIDIA H200 (PCIe) x4 (TP4)",
                        "gpu": "NVIDIA H200 (PCIe)",
                        "gpu_count": 4,
                        "status": "complete",
                        "rows": [],
                        "config": {},
                    },
                ],
            },
            {
                "slug": "qwen_38_27b",
                "name": "Qwen 38 27b",
                "setups": [
                    {
                        "id": "lib:qwen_38_27b/H200",
                        "kind": "chat",
                        "file": "H200.md",
                        "label": "H200",
                        "model": "Qwen/Qwen3.8-27B-FP8",
                        "setup": "NVIDIA H200 (PCIe) x4",
                        "gpu": "NVIDIA H200 (PCIe)",
                        "gpu_count": 4,
                        "status": "complete",
                        "rows": [{"label": "ctx=0 users=1 coding", "metrics": {"tokens_p50": 99.0}}],
                    }
                ],
            },
        ],
    }


def test_measured_benchmarks_matched_and_provenanced():
    """A matching model+GPU returns MEASURED rows with provenance."""
    from model_benchmarker.webapp.measured import find_measured

    hits = find_measured("deepseek-ai/DeepSeek-V4-Flash-0731", "NVIDIA H200 SXM", gpu_count=4, library=_lib_payload())
    assert len(hits) == 1  # the memory artifact is excluded
    h = hits[0]
    assert h["file"] == "H200Sx4_hicachex2.md"
    assert h["same_shape"] is True
    assert h["setup"].startswith("NVIDIA H200")
    assert len(h["rows"]) == 2
    assert h["rows"][0]["tokens_p50"] == 331.1
    assert h["rows"][0]["ttft_turn1_p50_ms"] == 930.6
    assert [c["key"] for c in h["columns"]][:2] == ["tokens_p50", "tokens_p95"]


def test_measured_benchmarks_absent_when_nothing_matches():
    """No benchmark for this model -> NO panel (the estimator must not invent
    a throughput number; the removed roofline over-predicted 3-5x)."""
    from model_benchmarker.webapp.measured import find_measured

    assert find_measured("meta-llama/Llama-3-8B", "NVIDIA H200 SXM", 4, library=_lib_payload()) == []
    # right model, wrong GPU family -> still nothing
    assert find_measured("deepseek-ai/DeepSeek-V4-Flash-0731", "NVIDIA B200", 4, library=_lib_payload()) == []


def test_measured_benchmarks_never_quote_memory_artifacts():
    """The estimator's own capacity artifacts are kind='memory' and must never
    be surfaced as measurements (that would be circular)."""
    from model_benchmarker.webapp.measured import find_measured

    hits = find_measured("deepseek-ai/DeepSeek-V4-Flash-0731", "NVIDIA H200 SXM", 4, library=_lib_payload())
    assert all("_memory" not in (h["file"] or "") for h in hits)
    assert all(h["rows"] for h in hits)  # every surfaced setup has real rows


def test_measured_gpu_match_ignores_vendor_word():
    """'NVIDIA H200' vs 'NVIDIA RTX PRO 6000' share only 'nvidia' — that must
    never count as a match (it did, before the GPU-model fix)."""
    from model_benchmarker.webapp.measured import _gpu_matches

    assert _gpu_matches("NVIDIA H200 SXM", "NVIDIA H200 (PCIe)")
    assert _gpu_matches("NVIDIA H200 SXM", "NVIDIA H200 (PCIe)")
    assert not _gpu_matches("NVIDIA H200 SXM", "NVIDIA RTX PRO 6000")
    assert not _gpu_matches("NVIDIA H200 SXM", "B200")


def test_estimate_api_has_measured_benchmarks_and_no_roofline(gated_app, monkeypatch):
    """The payload carries measured_benchmarks (may be empty) and NO
    throughput estimate."""
    import model_benchmarker.webapp.app as appmod

    monkeypatch.setattr(appmod, "find_measured", lambda **kw: [{"file": "x.md", "rows": [], "columns": []}])
    llama = {
        "model_type": "llama",
        "hidden_size": 4096,
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "head_dim": 128,
        "vocab_size": 128256,
        "intermediate_size": 14336,
        "torch_dtype": "bfloat16",
    }
    status, body = _call(
        gated_app,
        "POST",
        "/api/estimate",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"config_json": llama, "gpu": "H200"}).encode(),
    )
    assert status == 200
    data = json.loads(body)
    assert "throughput" not in data  # the roofline is gone
    assert data["measured_benchmarks"] == [{"file": "x.md", "rows": [], "columns": []}]
    # active params survives as an architecture fact in the structure table
    rows = {r[0]: r[1] for r in data["structure"]}
    assert "active params / token" in rows


def test_measured_model_matching_handles_slug_spelling():
    """Model names and results/ slugs disagree about separators AND decimal
    versions ('Qwen3.8-27B-FP8' vs 'qwen_38_27b'). Fingerprint matching
    links those, while never linking genuinely different models."""
    from model_benchmarker.webapp.measured import _slug_matches

    # the spellings that must link
    assert _slug_matches("Qwen/Qwen3.8-27B-FP8", "qwen_38_27b", "")
    assert _slug_matches("deepseek-ai/DeepSeek-V4.1-Flash", "deepseek_v41_flash", "")
    assert _slug_matches("deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek_v4_flash_0731", "")
    assert _slug_matches("zai-org/GLM-5.3-Flash", "glm-5.3-flash", "")
    assert _slug_matches("google/gemma-4-31b-it", "gemma_4_31b", "")
    # the near-misses that must NOT (v4-Flash vs v4.1-Flash is the dangerous one)
    assert not _slug_matches("deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek_v41_flash", "")
    assert not _slug_matches("Qwen/Qwen3-8B", "qwen_38_27b", "")
    assert not _slug_matches("meta-llama/Llama-3-8B", "deepseek_v4_flash_0731", "")


def test_measured_benchmarks_qwen_finds_its_runs():
    """The Qwen3.8-27B slug/spelling case end-to-end: 4 H200 runs found."""
    from model_benchmarker.webapp.measured import find_measured

    lib = {
        "root": "/r",
        "models": [
            {
                "slug": "qwen_38_27b",
                "name": "Qwen 38 27b",
                "setups": [
                    {
                        "id": "lib:qwen_38_27b/H200",
                        "kind": "chat",
                        "file": "H200.md",
                        "label": "H200",
                        "model": "Qwen/Qwen3.8-27B-FP8",
                        "setup": "NVIDIA H200 (PCIe) x4",
                        "gpu": "NVIDIA H200 (PCIe)",
                        "gpu_count": 4,
                        "status": "complete",
                        "rows": [{"label": "ctx=0 users=1 coding", "metrics": {"tokens_p50": 136.4}}],
                    }
                ],
            }
        ],
    }
    hits = find_measured("Qwen/Qwen3.8-27B-FP8", "NVIDIA H200 SXM", gpu_count=4, library=lib)
    assert len(hits) == 1 and hits[0]["file"] == "H200.md"
    assert hits[0]["rows"][0]["tokens_p50"] == 136.4
    # a different model in the same tree is still not matched
    assert find_measured("Qwen/Qwen3-8B", "NVIDIA H200 SXM", 4, library=lib) == []
