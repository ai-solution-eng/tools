"""End-to-end REST driver + CLI tests against the local aiohttp fixture."""

from __future__ import annotations

import asyncio
import json

from model_benchmarker.endpoint_benchmarker.cli import main
from model_benchmarker.endpoint_benchmarker.queries import load_queries
from model_benchmarker.endpoint_benchmarker.sweep import run_sweep
from model_benchmarker.endpoint_benchmarker.targets import RestTarget


def _queries():
    return load_queries(None, "generic")[0]


def test_rest_run_against_local_server(rest_server):
    base = rest_server["base_url"]
    state = rest_server["state"]
    target = RestTarget(
        base_url=base,
        dataset="ds-a",
        params={"top_k": 5},
        headers={"X-Marker": "bench"},
    )
    target.validate()
    results = asyncio.run(
        run_sweep(
            [3],
            duration=1.0,
            ramp_up=0.0,
            warmup_rounds=0,
            settle=0.0,
            call_timeout=10.0,
            seed=7,
            progress_interval=0.5,
            queries=_queries(),
            rest_target=target,
        )
    )
    assert len(results) == 1
    level = results[0]
    assert level.stats.success == level.stats.total
    assert level.stats.total > 0
    s = level.stats.summary()
    assert s["latency_ms"]["p99"] > 0
    assert s["avg_results_per_query"] > 0
    # the server actually saw our shape: dataset path, q param, marker header
    assert state["searches"], "server saw no requests"
    hit = state["searches"][0]
    assert hit["dataset"] == "ds-a"
    assert hit["params"]["top_k"] == "5"
    assert hit["headers"]["X-Marker"] == "bench"
    assert any(hit["q"] == q for q in _queries())


def test_rest_post_body_endpoint(rest_server):
    base = rest_server["base_url"]
    state = rest_server["state"]
    target = RestTarget(
        base_url=base,
        path_template="/echo",
        method="POST",
        query_param="",
        body_template='{"question": "{query}", "top_k": 3}',
    )
    target.validate()
    results = asyncio.run(
        run_sweep(
            [2],
            duration=0.8,
            ramp_up=0.0,
            warmup_rounds=1,
            settle=0.0,
            call_timeout=10.0,
            seed=None,
            progress_interval=0.5,
            queries=["q one", "q two"],
            rest_target=target,
        )
    )
    assert results[0].stats.success > 0
    assert state["echoes"], "POST body never reached the server"
    bodies = [json.loads(e["body"])["question"] for e in state["echoes"]]
    assert any(b in ("q one", "q two") for b in bodies)


def test_rest_failures_counted(rest_server):
    base = rest_server["base_url"]
    target = RestTarget(base_url=base, path_template="/fail", query_param="q", health_path="")
    target.validate()
    results = asyncio.run(
        run_sweep(
            [2],
            duration=0.6,
            ramp_up=0.0,
            warmup_rounds=0,
            settle=0.0,
            call_timeout=10.0,
            seed=None,
            progress_interval=0.5,
            queries=["x"],
            rest_target=target,
        )
    )
    s = results[0].stats.summary()
    assert s["failed"] == s["total_requests"]
    assert s["successful"] == 0
    assert any("HTTP 500" in k for k in s["errors"])


def test_cli_rest_full_run_with_artifacts(rest_server, tmp_path, capsys):
    base = rest_server["base_url"]
    out = tmp_path / "results.json"
    csv = tmp_path / "results.csv"
    html = tmp_path / "report.html"
    rc = main(
        [
            "--url",
            base,
            "--dataset",
            "ds-b",
            "-N",
            "2",
            "--duration",
            "0.8",
            "--warmup-rounds",
            "0",
            "--ramp-up",
            "0",
            "--progress-interval",
            "1",
            "--param",
            "top_k=2",
            "--header",
            "Authorization=Bearer sekrit-token",
            "--output",
            str(out),
            "--csv",
            str(csv),
            "--html",
            str(html),
            "--quiet",
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["levels"][0]["client_stats"]["successful"] > 0
    assert payload["levels"][0]["t_end_epoch"] >= payload["levels"][0]["t_start_epoch"]
    # secrets redacted in the artifact
    assert payload["config"]["target"]["headers"]["Authorization"] == "REDACTED"
    # summary rows present
    assert payload["summary"]["rows"][0]["concurrency"] == 2
    # csv + html well-formed
    csv_text = csv.read_text()
    header = csv_text.splitlines()[0]
    assert "concurrency" in header and "rps" in header and "rps_per_user" in header
    assert len(csv_text.strip().splitlines()) == 2  # header + one level row
    html_text = html.read_text()
    assert "<svg" in html_text
    assert "sekrit-token" not in html_text
    assert "REDACTED" in html_text


def test_cli_rest_auto_discovers_dataset(rest_server, capsys):
    base = rest_server["base_url"]
    rc = main(
        [
            "--url",
            base,
            "-N",
            "1",
            "--duration",
            "0.5",
            "--warmup-rounds",
            "0",
            "--ramp-up",
            "0",
            "--progress-interval",
            "1",
            "--quiet",
        ]
    )
    assert rc == 0
    # ds-a is the first dataset the fixture advertises
    assert rest_server["state"]["searches"][0]["dataset"] == "ds-a"


def test_cli_rest_bad_target_exits_cleanly(rest_server):
    rc = main(
        [
            "--url",
            rest_server["base_url"],
            "--path",
            "/api/datasets/{dataset}/search",  # needs --dataset, none given and discovery works...
            # force the failure differently: body on GET
            "--body",
            '{"q": "{query}"}',
            "-N",
            "1",
            "--duration",
            "0.5",
        ]
    )
    assert rc == 2  # TargetError -> clean exit code 2, no traceback
