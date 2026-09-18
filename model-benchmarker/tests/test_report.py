"""Knee detection + report rendering tests (synthetic data)."""

from __future__ import annotations

import json
import time

from model_benchmarker.endpoint_benchmarker.report import (
    build_run_payload,
    detect_knee,
    format_scaling_table,
    render_html,
    scaling_rows,
    write_csv,
)
from model_benchmarker.endpoint_benchmarker.stats import BenchmarkStats
from model_benchmarker.endpoint_benchmarker.sweep import LevelResult


def _level(n, p99_ms, success_rate=100.0, telemetry=None):
    stats = BenchmarkStats()
    # craft latencies so p99 lands near p99_ms
    base = p99_ms / 1000.0
    for i in range(20):
        stats.record_success(base * (0.5 + 0.5 * i / 19))
    stats.failed = int(len(stats.latencies) * (100 - success_rate) / 100)
    stats.success = stats.total - stats.failed
    return LevelResult(
        concurrency=n,
        t_start_epoch=time.time(),
        t_end_epoch=time.time() + 1,
        stats=stats,
        telemetry=telemetry,
    )


def _rows(levels):
    return scaling_rows(levels)


def test_detect_knee_latency_criterion():
    # 1 -> 100ms baseline, 4 -> 110ms (1.1x, fine), 32 -> 150ms (1.5x, fine),
    # 64 -> 900ms (9x, past the 2x factor) -> knee at 64
    levels = [_level(1, 100), _level(4, 110), _level(32, 150), _level(64, 900)]
    knee = detect_knee(_rows(levels), knee_factor=2.0)
    assert knee is not None
    assert knee["criterion"] == "latency"
    assert knee["concurrency"] == 64


def test_detect_knee_error_criterion_takes_priority():
    levels = [_level(1, 100), _level(4, 110, success_rate=95.0)]
    knee = detect_knee(_rows(levels))
    assert knee is not None
    assert knee["criterion"] == "errors"
    assert knee["concurrency"] == 4


def test_detect_knee_none_when_flat():
    levels = [_level(1, 100), _level(4, 105), _level(8, 110)]
    assert detect_knee(_rows(levels), knee_factor=2.0) is None


def test_detect_knee_single_level_none():
    assert detect_knee(_rows([_level(1, 100)])) is None


def _tele(util_mean, samples=6):
    return {
        "metrics": {
            "DCGM_FI_DEV_GPU_UTIL": {
                "series_count": 2,
                "samples": samples,
                "overall": {"mean": util_mean, "min": util_mean - 5, "max": util_mean + 5},
                "gpus": [
                    {
                        "host": "n",
                        "device": "0",
                        "mean": util_mean,
                        "min": util_mean - 5,
                        "max": util_mean + 5,
                        "labels": {},
                    },
                ],
            }
        },
        "window": {"step": "15s"},
    }


def test_scaling_rows_include_gpu_columns():
    levels = [_level(1, 100, telemetry=_tele(30.0)), _level(4, 110, telemetry=_tele(60.0))]
    rows = _rows(levels)
    assert rows[0]["gpu_util_mean"] == 30.0
    assert rows[1]["gpu_util_max"] == 65.0
    assert rows[0]["gpu_samples"] == 6


def test_format_scaling_table_with_and_without_gpu():
    rows_plain = _rows([_level(1, 100), _level(2, 150)])
    table = format_scaling_table(rows_plain)
    assert "GPU_UTIL" not in table
    assert "p99" in table
    rows_gpu = _rows([_level(1, 100, telemetry=_tele(30.0)), _level(2, 150, telemetry=_tele(60.0))])
    table_gpu = format_scaling_table(rows_gpu)
    assert "GPU_UTIL" in table_gpu
    assert "30.0" in table_gpu


def test_render_html_contains_charts_and_knee():
    levels = [_level(1, 100, telemetry=_tele(30.0)), _level(4, 400, telemetry=_tele(70.0))]
    payload = {
        "run_id": "r1",
        "started_utc": "t0",
        "finished_utc": "t1",
        "config": {"mode": "rest", "target": {"url": "http://x", "headers": {"Authorization": "REDACTED"}}},
        "idle_baseline": None,
        "levels": [lv.to_json() for lv in levels],
        "summary": {
            "rows": _rows(levels),
            "knee": {"concurrency": 4, "criterion": "latency", "detail": "p99 4x"},
            "notes": [],
        },
    }
    html = render_html(payload, title="Test report")
    assert "<svg" in html
    assert "Knee point" in html
    assert "REDACTED" in html
    assert "Test report" in html
    assert "N=4" in html  # per-level details


def test_write_csv(tmp_path):
    levels = [_level(1, 100, telemetry=_tele(30.0)), _level(4, 150, telemetry=_tele(60.0))]
    path = tmp_path / "out.csv"
    payload = build_run_payload(levels, {"mode": "rest"}, "run-1", "2026-01-01T00:00:00Z", 2.0, None)
    write_csv(str(path), payload)
    lines = path.read_text().strip().splitlines()
    header = lines[0].split(",")
    assert "concurrency" in header and "gpu_util_mean" in header
    assert len(lines) == 3
    row1 = dict(zip(header, lines[1].split(",")))
    assert row1["concurrency"] == "1"
    assert float(row1["gpu_util_mean"]) == 30.0
    assert json.dumps(row1)  # serializable sanity
