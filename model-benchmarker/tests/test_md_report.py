"""Tests for the extended-reporting features of the universal benchmarker:

* ``--md`` markdown report (results/ tree convention, ingested by
  ``results_to_html.py`` via ``parse_rag_table``);
* ``--percentiles`` configurable latency percentiles;
* ``--note`` run annotations stamped into every artifact;
* the error deep-dive (categorization + per-level detail tables);
* ``--from run.json`` re-render without re-running the load.
"""

from __future__ import annotations

import json

# results_to_html.py lives two levels up from the package; import it by path.
import sys
import time
from pathlib import Path

import pytest

from model_benchmarker.endpoint_benchmarker.cli import main
from model_benchmarker.endpoint_benchmarker.report import (
    build_run_payload,
    detect_knee,
    format_scaling_table,
    render_html,
    render_md,
    scaling_rows,
    write_csv,
    write_md,
)
from model_benchmarker.endpoint_benchmarker.stats import BenchmarkStats, categorize_errors
from model_benchmarker.endpoint_benchmarker.sweep import LevelResult

_RESULTS_TO_HTML = Path(__file__).resolve().parents[1] / "src" / "model_benchmarker" / "results_to_html.py"
sys.path.insert(0, str(_RESULTS_TO_HTML.parent))
import results_to_html  # noqa: E402


def _level(n, p99_ms, success_rate=100.0, telemetry=None, errors=None, status_codes=None):
    stats = BenchmarkStats()
    base = p99_ms / 1000.0
    for i in range(20):
        stats.record_success(base * (0.5 + 0.5 * i / 19))
    if errors or status_codes:
        for key, count in (errors or {}).items():
            for _ in range(count):
                stats.record_failure(0.5, key, status=(status_codes or {}).get("code"))
        stats.failed = sum((errors or {}).values())
        stats.success = stats.total - stats.failed
    else:
        stats.failed = int(len(stats.latencies) * (100 - success_rate) / 100)
        stats.success = stats.total - stats.failed
    return LevelResult(
        concurrency=n,
        t_start_epoch=time.time(),
        t_end_epoch=time.time() + 1,
        stats=stats,
        telemetry=telemetry,
    )


def _payload(levels, **kw):
    config = {"mode": "rest", "target": {"url": "http://x", "path": "/search", "method": "GET", "dataset": "ds"}}
    config.update(kw.pop("config", {}))
    return build_run_payload(levels, config, "run-x", "2026-01-01T00:00:00Z", 2.0, None, **kw)


# ---------------------------------------------------------------------------
# Error categorization
# ---------------------------------------------------------------------------


def test_categorize_errors_buckets():
    cats = categorize_errors(
        {
            "HTTP 500: boom": 3,
            "ReadTimeout: timed out": 2,
            "ConnectError: All connection attempts failed": 1,
            "MCP connect: ConnectError: x": 1,
            "Timeout (>120s)": 4,
            "WeirdError: ???": 2,
        }
    )
    assert cats == {"http_error": 3, "timeout": 6, "connection": 2, "other": 2}


def test_categorize_errors_empty():
    assert categorize_errors({}) == {}


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def test_render_md_structure():
    levels = [_level(1, 100), _level(4, 900)]
    payload = _payload(levels, annotations=["warm-cache run", "H200 x4"])
    md = render_md(payload, title="My Setup")
    assert md.startswith("# My Setup")
    assert "Status: complete (last updated" in md
    assert "## Benchmark configuration" in md
    assert "## Benchmark results" in md
    assert "| Note | warm-cache run |" in md
    assert "| Note | H200 x4 |" in md
    assert "| Knee point | N=4" in md
    assert "### Scaling by concurrency level" in md
    # percentile columns present
    assert "p50 (ms)" in md and "p95 (ms)" in md and "p99 (ms)" in md


def test_render_md_error_deep_dive():
    levels = [
        _level(1, 100),
        _level(4, 150, errors={"HTTP 500: boom": 3, "ReadTimeout: timed out": 2}, status_codes={"code": 500}),
    ]
    payload = _payload(levels)
    md = render_md(payload, title="Errors")
    assert "### Errors by category" in md
    assert "| http_error | 3 |" in md
    assert "| timeout | 2 |" in md
    assert "### Error detail (top errors per level)" in md
    assert "HTTP 500: boom" in md
    assert "### HTTP status codes" in md
    assert "| 4 | 500 | 5 |" in md


def test_render_md_clean_run_has_no_error_tables():
    levels = [_level(1, 100)]
    md = render_md(_payload(levels), title="Clean")
    assert "No failed requests in this run." in md
    assert "### Errors by category" not in md


def test_write_md_atomic_and_parsable(tmp_path):
    levels = [_level(1, 100), _level(4, 250)]
    out = tmp_path / "results" / "mymodel" / "H200x1.md"
    payload = _payload(levels, annotations=["n1"])
    write_md(str(out), payload, title="H200x1")
    assert out.exists()
    assert not out.with_name(out.name + ".partial").exists()
    text = out.read_text()
    assert text.startswith("# H200x1")

    # results_to_html.py ingestion: classified as a RAG-style result, config parsed
    rag = results_to_html.parse_rag_table(text)
    assert rag is not None
    assert rag["config"]["Mode"] == "REST"
    assert rag["config"]["Target"] == "http://x"
    assert rag["config"]["Note"] == "n1"
    assert rag["results"]["Knee point"].startswith("N=")
    # and never mistaken for a chat table
    _mode, rows = results_to_html.parse_chat_table(text)
    assert rows == []


def test_md_percentiles_flow_through(tmp_path):
    levels = [_level(1, 100), _level(2, 150)]
    payload = _payload(levels, percentiles=(50.0, 90.0, 99.9))
    md = render_md(payload, title="Pcts")
    assert "p90 (ms)" in md
    assert "p99.9 (ms)" in md
    assert "p95 (ms)" not in md
    assert "| Percentiles | p50, p90, p99.9 |" in md
    table = format_scaling_table(payload["summary"]["rows"])
    assert "p90" in table and "p95" not in table
    out = tmp_path / "p.md"
    write_md(str(out), payload, title="Pcts")
    assert "p90 (ms)" in out.read_text()


# ---------------------------------------------------------------------------
# Percentiles + knee interplay
# ---------------------------------------------------------------------------


def test_knee_uses_p99_when_present():
    levels = [_level(1, 100), _level(4, 900)]
    knee = detect_knee(scaling_rows(levels), knee_factor=2.0)
    assert knee is not None and knee["criterion"] == "latency" and knee["concurrency"] == 4


def test_knee_falls_back_to_highest_percentile():
    levels = [_level(1, 100), _level(4, 900)]
    rows = scaling_rows(levels, percentiles=(50.0, 90.0))  # no p99 key
    knee = detect_knee(rows, knee_factor=2.0)
    assert knee is not None and "p90" in knee["detail"]


# ---------------------------------------------------------------------------
# Annotations in HTML + CSV
# ---------------------------------------------------------------------------


def test_annotations_in_html():
    levels = [_level(1, 100)]
    payload = _payload(levels, annotations=["annotated run"])
    html = render_html(payload, title="T")
    assert "annotated run" in html


def test_csv_from_payload_with_custom_percentiles(tmp_path):
    levels = [_level(1, 100), _level(2, 150)]
    payload = _payload(levels, percentiles=(50.0, 90.0))
    path = tmp_path / "o.csv"
    write_csv(str(path), payload)
    header = path.read_text().splitlines()[0].split(",")
    assert "lat_p90_ms" in header
    assert "lat_p95_ms" not in header


# ---------------------------------------------------------------------------
# End-to-end: run + --from re-render (real local REST server)
# ---------------------------------------------------------------------------


def test_end_to_end_run_and_rerender(rest_server, tmp_path, capsys):
    out_json = tmp_path / "run.json"
    out_md = tmp_path / "report.md"
    rc = main(
        [
            "--url",
            rest_server["base_url"],
            "-N",
            "2",
            "--duration",
            "0.3",
            "--ramp-up",
            "0",
            "--health-path",
            "",
            "--output",
            str(out_json),
            "--md",
            str(out_md),
            "--note",
            "e2e annotation",
        ]
    )
    assert rc == 0
    assert out_json.exists() and out_md.exists()
    md_text = out_md.read_text()
    assert "e2e annotation" in md_text
    md_text  # noqa: B018 — (used below via re-render assertions)

    # --from re-render: new knee factor + csv + html from the saved JSON only
    out_md2 = tmp_path / "report2.md"
    out_csv = tmp_path / "report2.csv"
    out_html = tmp_path / "report2.html"
    rc = main(
        [
            "--from",
            str(out_json),
            "--knee-factor",
            "1.1",
            "--md",
            str(out_md2),
            "--csv",
            str(out_csv),
            "--html",
            str(out_html),
        ]
    )
    assert rc == 0
    assert out_md2.exists() and out_csv.exists() and out_html.exists()
    payload = json.loads(out_json.read_text())
    rows = payload["summary"]["rows"]
    assert len(rows) == 1
    # a different knee factor must not crash and must produce a re-rendered file
    assert "Status: complete" in out_md2.read_text()
    assert "," in out_csv.read_text()  # csv has content


def test_end_to_end_custom_percentiles(rest_server, tmp_path):
    out_md = tmp_path / "pct.md"
    rc = main(
        [
            "--url",
            rest_server["base_url"],
            "-N",
            "2",
            "--duration",
            "0.2",
            "--ramp-up",
            "0",
            "--health-path",
            "",
            "--percentiles",
            "50,90",
            "--md",
            str(out_md),
        ]
    )
    assert rc == 0
    text = out_md.read_text()
    assert "p90 (ms)" in text
    assert "p95 (ms)" not in text


def test_rerender_missing_file_is_clean_error(tmp_path):
    rc = main(["--from", str(tmp_path / "nope.json"), "--md", str(tmp_path / "x.md")])
    assert rc == 2


@pytest.mark.parametrize(
    "bad",
    ["0", "101", "abc", "50,,70", "-5"],
)
def test_bad_percentiles_rejected(rest_server, tmp_path, bad):
    rc = main(
        [
            "--url",
            rest_server["base_url"],
            "-N",
            "1",
            "--duration",
            "0.1",
            "--ramp-up",
            "0",
            "--health-path",
            "",
            "--percentiles",
            bad,
        ]
    )
    assert rc == 2
