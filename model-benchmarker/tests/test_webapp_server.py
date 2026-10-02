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
