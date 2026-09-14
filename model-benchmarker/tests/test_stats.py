from __future__ import annotations

import pytest

from model_benchmarker.endpoint_benchmarker.stats import BenchmarkStats, err_key, percentile


def test_percentile_basic():
    vals = sorted(float(x) for x in range(1, 101))  # 1..100
    assert percentile(vals, 50) == pytest.approx(50.5)
    assert percentile(vals, 0) == 1.0
    assert percentile(vals, 100) == 100.0


def test_percentile_small_inputs():
    assert percentile([], 95) == 0.0
    assert percentile([2.0], 95) == 2.0
    assert percentile([1.0, 3.0], 50) == pytest.approx(2.0)


def test_record_success_and_failure():
    stats = BenchmarkStats()
    stats.record_success(0.1, n_results=3, n_bytes=100, status=200)
    stats.record_success(0.3, n_results=1, n_bytes=50, status=200)
    stats.record_failure(0.2, "HTTP 500: boom", status=500)
    s = stats.summary()
    assert s["total_requests"] == 3
    assert s["successful"] == 2
    assert s["failed"] == 1
    assert s["success_rate_pct"] == pytest.approx(66.67, abs=0.01)
    assert s["avg_results_per_query"] == 2.0
    assert s["avg_response_bytes"] == 75.0
    assert s["status_codes"] == {200: 2, 500: 1}
    assert s["errors"] == {"HTTP 500: boom": 1}
    assert s["latency_ms"]["min"] == 100.0
    assert s["latency_ms"]["max"] == 300.0


def test_error_map_bounded():
    stats = BenchmarkStats()
    for i in range(100):
        stats.record_failure(0.01, f"error-{i}")
    assert len(stats.errors) <= 33  # 32 distinct + the "(+more)" marker
    assert any("more" in k for k in stats.errors)


def test_err_key_includes_message_and_truncates():
    class WeirdError(Exception):
        pass

    key = err_key(WeirdError("x" * 500))
    assert key.startswith("WeirdError:")
    assert len(key) < 150
    assert err_key(Exception()) == "Exception"


def test_summary_empty():
    s = BenchmarkStats().summary()
    assert s["total_requests"] == 0
    assert s["success_rate_pct"] == 0.0
    assert s["response_rate_rps"] == 0.0
    assert s["latency_ms"]["p99"] == 0.0
