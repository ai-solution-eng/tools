"""Sweep-engine tests (client-side only; no Prometheus)."""

from __future__ import annotations

import asyncio
import json

from model_benchmarker.endpoint_benchmarker.cli import main
from model_benchmarker.endpoint_benchmarker.queries import load_queries
from model_benchmarker.endpoint_benchmarker.report import build_run_payload
from model_benchmarker.endpoint_benchmarker.sweep import run_sweep
from model_benchmarker.endpoint_benchmarker.targets import RestTarget


def test_sweep_levels_independent_and_ordered(rest_server):
    target = RestTarget(base_url=rest_server["base_url"], dataset="ds-a", health_path="")
    target.validate()
    queries = load_queries(None, "generic")[0]
    levels = asyncio.run(
        run_sweep(
            [2, 4],
            duration=0.5,
            ramp_up=0.0,
            warmup_rounds=0,
            settle=0.2,
            call_timeout=10.0,
            seed=11,
            progress_interval=0.5,
            queries=queries,
            rest_target=target,
        )
    )
    assert [lv.concurrency for lv in levels] == [2, 4]
    # separate stats per level
    assert levels[0].stats is not levels[1].stats
    # wall-clock windows recorded and non-overlapping-ish (level 2 starts after level 1 ends)
    assert levels[0].t_start_epoch <= levels[0].t_end_epoch
    assert levels[1].t_start_epoch >= levels[0].t_end_epoch
    assert all(lv.stats.total > 0 for lv in levels)


def test_sweep_warmup_unmeasured(rest_server):
    state = rest_server["state"]
    state["searches"].clear()
    target = RestTarget(base_url=rest_server["base_url"], dataset="ds-a", health_path="")
    target.validate()
    queries = ["warmq"]
    levels = asyncio.run(
        run_sweep(
            [2],
            duration=0.4,
            ramp_up=0.0,
            warmup_rounds=2,  # 2 rounds x 2 users = 4 warm requests
            settle=0.0,
            call_timeout=10.0,
            seed=None,
            progress_interval=0.5,
            queries=queries,
            rest_target=target,
        )
    )
    measured = levels[0].stats.total
    seen = len(state["searches"])
    assert measured > 0
    assert seen >= measured  # warm-up requests hit the server too but are not counted


def test_sweep_json_payload_shape(rest_server, tmp_path):
    target = RestTarget(base_url=rest_server["base_url"], dataset="ds-a", health_path="")
    target.validate()
    queries = load_queries(None, "generic")[0]
    levels = asyncio.run(
        run_sweep(
            [1, 2],
            duration=0.4,
            ramp_up=0.0,
            warmup_rounds=0,
            settle=0.1,
            call_timeout=10.0,
            seed=None,
            progress_interval=0.5,
            queries=queries,
            rest_target=target,
        )
    )
    payload = build_run_payload(
        levels, {"mode": "rest", "target": target.describe()}, "run-x", "2026-01-01T00:00:00+00:00", 2.0, None
    )
    assert set(payload) >= {"run_id", "config", "levels", "summary"}
    assert len(payload["levels"]) == 2
    level0 = payload["levels"][0]
    assert {"concurrency", "t_start_epoch", "t_end_epoch", "client_stats"} <= set(level0)
    row = payload["summary"]["rows"][0]
    assert {"concurrency", "total", "rps", "rps_per_user", "lat_p99"} <= set(row)
    # single level pair with flat latencies -> knee may be None; structure holds
    assert "knee" in payload["summary"]
    # redaction flows through the payload config
    secret_target = RestTarget(
        base_url=rest_server["base_url"], dataset="ds-a", health_path="", headers={"X-Api-Key": "hidden"}
    )
    payload2 = build_run_payload(levels, {"target": secret_target.describe()}, "run-y", "t", 2.0, None)
    assert payload2["config"]["target"]["headers"]["X-Api-Key"] == "REDACTED"


def test_cli_sweep_without_prometheus(rest_server, tmp_path, capsys):
    out = tmp_path / "sweep.json"
    rc = main(
        [
            "--url",
            rest_server["base_url"],
            "--dataset",
            "ds-a",
            "--health-path",
            "",  # generic endpoint, no /healthz
            "--sweep",
            "1,2",
            "--duration",
            "0.5",
            "--settle",
            "0.2",
            "--warmup-rounds",
            "0",
            "--ramp-up",
            "0",
            "--progress-interval",
            "1",
            "--output",
            str(out),
            "--quiet",
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text())
    assert [lv["concurrency"] for lv in payload["levels"]] == [1, 2]
    assert payload["config"]["telemetry"] is None
