"""Telemetry tests against a fake Prometheus HTTP API + aggregation unit tests."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from model_benchmarker.endpoint_benchmarker.telemetry import (
    TelemetryConfig,
    _aggregate_series,
    _promql,
    capture_idle_baseline,
    capture_window,
    subtract_baseline,
)

# ---------------------------------------------------------------------------
# aggregation units
# ---------------------------------------------------------------------------

T0, T1 = 1000.0, 1060.0


def _series(labels, values):
    return {"metric": labels, "values": [[T0 + i * 15, str(v)] for i, v in enumerate(values)]}


def test_promql_selector_interpolation():
    assert _promql("M", "") == "M"
    assert _promql("M", 'exported_namespace="x"') == 'M{exported_namespace="x"}'


def test_aggregate_groups_per_gpu():
    series = [
        _series({"Hostname": "node-a", "gpu": "0", "exported_namespace": "henkia"}, [10, 20, 30]),
        _series({"Hostname": "node-a", "gpu": "1", "exported_namespace": "henkia"}, [50, 60]),
        _series({"Hostname": "node-b", "gpu": "0", "exported_namespace": "other"}, [90]),
    ]
    agg = _aggregate_series(series, T0, T1)
    assert agg["series_count"] == 3
    assert agg["samples"] == 6
    assert len(agg["gpus"]) == 3
    g0 = next(g for g in agg["gpus"] if g["device"] == "0" and g["host"] == "node-a")
    assert g0["mean"] == pytest.approx(20.0)
    assert g0["min"] == 10.0
    assert g0["max"] == 30.0
    assert g0["labels"]["exported_namespace"] == "henkia"
    assert agg["overall"]["mean"] == pytest.approx((20 + 55 + 90) / 3)
    assert agg["overall"]["max"] == 90.0


def test_aggregate_drops_out_of_window_and_nan():
    series = [
        {
            "metric": {"Hostname": "n", "gpu": "0"},
            "values": [[T0 - 100, "5"], [T0, "10"], [float(T0 + 30), "nan"], [T1, "30"], [T1 + 999, "99"]],
        }
    ]
    agg = _aggregate_series(series, T0, T1)
    assert agg["samples"] == 2  # NaN and out-of-window dropped
    g = agg["gpus"][0]
    assert g["mean"] == pytest.approx(20.0)


def test_aggregate_empty():
    agg = _aggregate_series([], T0, T1)
    assert agg == {"series_count": 0, "samples": 0, "gpus": [], "overall": {}}


# ---------------------------------------------------------------------------
# fake prometheus server
# ---------------------------------------------------------------------------


def _make_prom_app(received: dict):
    from aiohttp import web

    async def query_range(request):
        received["queries"].append(dict(request.query))
        q = request.query.get("query", "")
        start = float(request.query["start"])
        end = float(request.query["end"])

        def vals(*pairs):
            """(frac_of_window, value) pairs -> samples inside the requested window."""
            return [[start + (end - start) * f, str(v)] for f, v in pairs]

        if "MISSING" in q:
            return web.json_response({"status": "success", "data": {"result": []}})
        if q.startswith("kube_"):
            return web.json_response(
                {
                    "status": "success",
                    "data": {"result": [{"metric": {"deployment": "rag"}, "values": vals((0.0, 2), (1.0, 4))}]},
                }
            )
        if "DCGM_FI_DEV_GPU_UTIL" in q:
            return web.json_response(
                {
                    "status": "success",
                    "data": {
                        "result": [
                            {
                                "metric": {"Hostname": "node-a", "gpu": "0", "exported_namespace": "henkia"},
                                "values": vals((0.0, 70), (1.0, 90)),
                            },
                            {
                                "metric": {"Hostname": "node-a", "gpu": "1", "exported_namespace": "henkia"},
                                "values": vals((0.0, 10), (1.0, 30)),
                            },
                        ]
                    },
                }
            )
        if "DCGM_FI_DEV_POWER_USAGE" in q:
            return web.json_response(
                {
                    "status": "success",
                    "data": {"result": [{"metric": {"Hostname": "node-a", "gpu": "0"}, "values": vals((0.5, 300))}]},
                }
            )
        return web.json_response({"status": "success", "data": {"result": []}})

    app = web.Application()
    app.router.add_get("/api/v1/query_range", query_range)
    return app


def _run_aiohttp(app, port_q, stop_event):
    from aiohttp import web

    async def main():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port_q.put(runner.addresses[0][1])
        while not stop_event.is_set():
            await asyncio.sleep(0.05)
        await runner.cleanup()

    asyncio.run(main())


@pytest.fixture()
def prom_server():
    received = {"queries": []}
    app = _make_prom_app(received)
    import queue

    port_q: queue.Queue = queue.Queue()
    stop_event = threading.Event()
    thread = threading.Thread(target=_run_aiohttp, args=(app, port_q, stop_event), daemon=True)
    thread.start()
    port = port_q.get(timeout=10)
    yield f"http://127.0.0.1:{port}", received
    stop_event.set()
    thread.join(timeout=5)


def test_capture_window_and_selector(prom_server):
    prom_url, received = prom_server
    config = TelemetryConfig(
        prom_url=prom_url,
        selector='exported_namespace="henkia"',
        gpu_metrics=("DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_POWER_USAGE"),
        extra_queries={"replicas": 'kube_deployment_status_replicas{namespace="henkia"}'},
    )
    t_start, t_end = time.time() - 60, time.time()
    out = asyncio.run(capture_window(config, t_start, t_end))
    util = out["metrics"]["DCGM_FI_DEV_GPU_UTIL"]
    assert util["series_count"] == 2
    assert len(util["gpus"]) == 2
    assert util["overall"]["mean"] == pytest.approx((80 + 20) / 2)
    power = out["metrics"]["DCGM_FI_DEV_POWER_USAGE"]
    assert power["overall"]["mean"] == 300.0
    extra = out["metrics"]["replicas"]
    assert extra["mean"] == pytest.approx(3.0)
    assert extra["max"] == 4.0
    # the selector reached the wire on every GPU metric query
    sent = [q["query"] for q in received["queries"]]
    assert any('DCGM_FI_DEV_GPU_UTIL{exported_namespace="henkia"}' in q for q in sent)
    assert any('DCGM_FI_DEV_POWER_USAGE{exported_namespace="henkia"}' in q for q in sent)
    # window passed through
    assert out["window"]["start_epoch"] == pytest.approx(t_start, abs=1)


def test_capture_window_missing_metric_warns_not_fails(prom_server):
    prom_url, _ = prom_server
    config = TelemetryConfig(prom_url=prom_url, gpu_metrics=("MISSING_METRIC",))
    out = asyncio.run(capture_window(config, T0, T1))
    assert out["metrics"]["MISSING_METRIC"]["series_count"] == 0


def test_subtract_baseline():
    level = {"metrics": {"M": {"overall": {"mean": 80.0}, "gpus": [{"host": "a", "device": "0", "mean": 80.0}]}}}
    baseline = {"metrics": {"M": {"overall": {"mean": 30.0}, "gpus": [{"host": "a", "device": "0", "mean": 30.0}]}}}
    subtract_baseline(level, baseline)
    assert level["metrics"]["M"]["overall"]["mean_minus_idle"] == 50.0
    assert level["metrics"]["M"]["gpus"][0]["mean_minus_idle"] == 50.0


def test_subtract_baseline_floors_at_zero_and_handles_missing():
    level = {"metrics": {"M": {"overall": {"mean": 10.0}, "gpus": [{"host": "a", "device": "0", "mean": 10.0}, {"host": "a", "device": "1", "mean": 99.0}]}}}
    baseline = {"metrics": {"M": {"overall": {"mean": 30.0}, "gpus": [{"host": "a", "device": "0", "mean": 30.0}]}}}
    subtract_baseline(level, baseline)
    assert level["metrics"]["M"]["gpus"][0]["mean_minus_idle"] == 0.0
    assert "mean_minus_idle" not in level["metrics"]["M"]["gpus"][1]  # no baseline entry -> raw only
    assert level["metrics"]["M"]["overall"]["mean_minus_idle"] == 0.0


def test_validate_config():
    assert TelemetryConfig(prom_url="").validate() is None
    with pytest.raises(ValueError, match="no metrics"):
        TelemetryConfig(prom_url="http://p", gpu_metrics=(), extra_queries={}).validate()
    with pytest.raises(ValueError, match="step"):
        TelemetryConfig(prom_url="http://p", step="15x").validate()


def test_idle_baseline_sleeps_then_captures(prom_server):
    prom_url, _ = prom_server
    config = TelemetryConfig(prom_url=prom_url, gpu_metrics=("DCGM_FI_DEV_GPU_UTIL",))
    t0 = time.monotonic()
    baseline = asyncio.run(capture_idle_baseline(config, 1.0))
    assert time.monotonic() - t0 >= 1.0
    assert baseline["kind"] == "idle_baseline"
    assert baseline["metrics"]["DCGM_FI_DEV_GPU_UTIL"]["series_count"] == 2
