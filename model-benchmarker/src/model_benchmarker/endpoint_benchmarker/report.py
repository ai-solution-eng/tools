"""Run summaries and artifacts: console scaling table, knee-point detection,
JSON run artifact (secrets redacted), CSV, and a self-contained HTML report.

The HTML report has no external CSS/JS — it can be attached to an email or
dropped into a trial report as-is (same rule as ModelBenchmarker's
``results_to_html`` output).
"""

from __future__ import annotations

import html
import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .stats import DEFAULT_PERCENTILES, categorize_errors, pct_label
from .sweep import LevelResult
from .targets import redact_secrets

log = logging.getLogger("endpoint_benchmarker")

PRIMARY_GPU_METRIC = "DCGM_FI_DEV_GPU_UTIL"

_PALETTE = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2", "#be185d", "#4d7c0f"]


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def scaling_rows(results: list[LevelResult], percentiles: Sequence[float] | None = None) -> list[dict]:
    rows = []
    for r in results:
        s = r.stats.summary(percentiles) if percentiles else r.stats.summary()
        row = {
            "concurrency": r.concurrency,
            "total": s["total_requests"],
            "success_rate_pct": s["success_rate_pct"],
            "rps": s["response_rate_rps"],
            "rps_per_user": round(s["response_rate_rps"] / r.concurrency, 2) if r.concurrency else 0.0,
            **{f"lat_{k}": v for k, v in s["latency_ms"].items()},
            "avg_results": s["avg_results_per_query"],
            "avg_bytes": s["avg_response_bytes"],
            "window_s": (r.t_end_epoch - r.t_start_epoch),
        }
        tele = r.telemetry or {}
        util = (tele.get("metrics") or {}).get(PRIMARY_GPU_METRIC) or {}
        overall = util.get("overall") or {}
        if overall:
            row["gpu_util_mean"] = overall.get("mean")
            row["gpu_util_mean_minus_idle"] = overall.get("mean_minus_idle")
            row["gpu_util_max"] = overall.get("max")
            row["gpu_samples"] = util.get("samples", 0)
        rows.append(row)
    return rows


def _percentile_cols(rows: list[dict]) -> list[str]:
    """The ``lat_p<N>`` keys present in the rows, ordered by percentile.

    Rows are self-describing, so every renderer (console table, CSV,
    markdown, HTML) shows exactly the percentiles the run was configured
    with, in a stable order. Fractional percentiles (``lat_p99.9``) sort
    after their integer neighbors (``lat_p99``).
    """
    import re

    num = re.compile(r"^\d+(\.\d+)?$")
    cols = {c for r in rows for c in r if c.startswith("lat_p") and num.match(c[len("lat_p") :])}
    return sorted(cols, key=lambda c: float(c[len("lat_p") :]))


def _knee_latency_key(rows: list[dict]) -> str | None:
    """Preferred tail-latency key for knee detection: p99 if measured, else
    the highest configured percentile."""
    if any("lat_p99" in r for r in rows):
        return "lat_p99"
    cols = _percentile_cols(rows)
    return cols[-1] if cols else None


def detect_knee(rows: list[dict], knee_factor: float = 2.0) -> dict | None:
    """First level where the service visibly stops scaling gracefully.

    Criterion 1: any level with error rate > 1% (success_rate < 99).
    Criterion 2: tail latency (p99, or the highest configured percentile)
    >= knee_factor × the best (minimum) tail across levels. Needs at least
    two levels with data; otherwise None.
    """
    valid = [r for r in rows if r["total"] > 0]
    if len(valid) < 2:
        return None
    for r in valid:
        if r["success_rate_pct"] < 99.0:
            return {
                "concurrency": r["concurrency"],
                "criterion": "errors",
                "detail": f"success rate fell to {r['success_rate_pct']}% at N={r['concurrency']}",
            }
    lat_key = _knee_latency_key(valid)
    if lat_key is None:
        return None
    tails = [r[lat_key] for r in valid if r.get(lat_key, 0) > 0]
    if not tails:
        return None
    best = min(tails)
    for r in valid:
        tail = r.get(lat_key, 0)
        if tail > 0 and tail >= knee_factor * best:
            return {
                "concurrency": r["concurrency"],
                "criterion": "latency",
                "detail": f"{lat_key[4:]} {tail:.0f}ms is {tail / best:.1f}x the best level "
                f"({best:.0f}ms) at N={r['concurrency']} (factor {knee_factor})",
            }
    return None


def build_notes(results: list[LevelResult], telemetry_step: str | None) -> list[str]:
    notes: list[str] = []
    if telemetry_step:
        step_s = _step_to_seconds(telemetry_step)
        for r in results:
            tele = r.telemetry or {}
            util = (tele.get("metrics") or {}).get(PRIMARY_GPU_METRIC) or {}
            samples = util.get("samples", 0)
            if util and samples and step_s and samples < 3 * max(1, len(util.get("gpus", [1]))):
                notes.append(
                    f"N={r.concurrency}: only {samples} GPU samples in the window "
                    f"(window {r.t_end_epoch - r.t_start_epoch:.0f}s, step {telemetry_step}) — widen --duration"
                )
    return notes


def _step_to_seconds(step: str) -> float | None:
    table = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if step and step[-1] in table and step[:-1].replace(".", "", 1).isdigit():
        return float(step[:-1]) * table[step[-1]]
    return None


def format_scaling_table(rows: list[dict]) -> str:
    p_cols = _percentile_cols(rows)
    has_gpu = any("gpu_util_mean" in r for r in rows)
    has_idle = any(r.get("gpu_util_mean_minus_idle") is not None for r in rows)
    header = f"  {'N':>5}  {'reqs':>7}  {'ok%':>7}  {'rps':>8}  {'rps/user':>8}"
    header += "".join(f"  {c[4:]:>9}" for c in p_cols)  # lat_p99 -> 'p99'
    header += f"  {'max ms':>9}"
    if has_gpu:
        header += f"  {'GPU_UTIL':>9}"
        if has_idle:
            header += f"  {'−idle':>7}"
    lines = [header, "  " + "-" * (len(header) - 2)]
    for r in rows:
        line = (
            f"  {r['concurrency']:>5}  {r['total']:>7}  {r['success_rate_pct']:>7.1f}  {r['rps']:>8.2f}  "
            f"{r['rps_per_user']:>8.2f}"
        )
        line += "".join(f"  {_fmt_opt(r.get(c), 9, 1)}" for c in p_cols)
        line += f"  {_fmt_opt(r.get('lat_max'), 9, 0)}"
        if has_gpu:
            line += f"  {_fmt_opt(r.get('gpu_util_mean'), 9, 1)}"
            if has_idle:
                line += f"  {_fmt_opt(r.get('gpu_util_mean_minus_idle'), 7, 1)}"
        lines.append(line)
    return "\n".join(lines)


def _fmt_opt(value, width: int, prec: int = 1) -> str:
    return f"{value:>{width}.{prec}f}" if isinstance(value, (int, float)) else " " * (width - 2) + "n/a"


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def build_run_payload(
    results: list[LevelResult],
    config: dict,
    run_id: str,
    started_utc: str,
    knee_factor: float,
    baseline: dict | None,
    percentiles: Sequence[float] | None = None,
    annotations: Sequence[str] | None = None,
) -> dict:
    percentiles = tuple(percentiles) if percentiles else DEFAULT_PERCENTILES
    config = dict(config)
    config["percentiles"] = [pct_label(p) for p in percentiles]
    if annotations:
        config["annotations"] = list(annotations)
    rows = scaling_rows(results, percentiles)
    knee = detect_knee(rows, knee_factor)
    tele_cfg = config.get("telemetry") or {}
    payload = {
        "run_id": run_id,
        "tool": "endpoint-benchmarker",
        "started_utc": started_utc,
        "finished_utc": datetime.now(UTC).isoformat(),
        "config": redact_secrets(config),
        "idle_baseline": baseline,
        "levels": [r.to_json() for r in results],
        "summary": {
            "rows": rows,
            "knee": knee,
            "notes": build_notes(results, tele_cfg.get("step")),
        },
    }
    return payload


def print_results(payload: dict) -> None:
    rows = payload["summary"]["rows"]
    knee = payload["summary"]["knee"]
    print()
    print("=" * 72)
    print("  BENCHMARK RESULTS (per concurrency level)")
    print("=" * 72)
    print(format_scaling_table(rows))
    if knee:
        print()
        print(f"  Knee point: N={knee['concurrency']} ({knee['detail']})")
    else:
        print()
        print("  Knee point: none observed across the tested concurrency levels")
    for note in payload["summary"]["notes"]:
        print(f"  note: {note}")
    print("=" * 72)


def write_json(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    log.info("Wrote JSON artifact: %s", path)


def write_csv(path: str, payload: dict) -> None:
    """Write the per-level CSV from a run payload dict.

    Payload-driven (not LevelResult-driven) so ``--from run.json`` can
    re-render a CSV without re-running the load.
    """
    rows = payload["summary"]["rows"]
    levels = payload["levels"]
    # GPU columns: overall mean per metric + per-GPU mean for the primary metric.
    gpu_cols: list[str] = []
    metric_names: list[str] = []
    for level in levels:
        metrics = (level.get("telemetry") or {}).get("metrics") or {}
        for name, agg in metrics.items():
            if not isinstance(agg, dict) or agg.get("overall") is None:
                continue
            col = f"gpu_{name}_mean"
            if col not in gpu_cols and name != PRIMARY_GPU_METRIC:
                gpu_cols.append(col)
                metric_names.append(name)
        util = metrics.get(PRIMARY_GPU_METRIC) or {}
        for g in util.get("gpus", []):
            col = f"gpu_util[{g['host']}:{g['device']}]"
            if col not in gpu_cols:
                gpu_cols.append(col)

    p_cols = _percentile_cols(rows)
    base_cols = (
        ["concurrency", "total", "success", "failed", "success_rate_pct", "duration_s", "rps", "rps_per_user"]
        + [f"{c}_ms" for c in ["lat_min", "lat_mean", *p_cols, "lat_max"]]
        + ["avg_results", "avg_bytes", "gpu_util_mean", "gpu_util_mean_minus_idle", "gpu_util_max"]
    )
    all_cols = base_cols + gpu_cols
    lines = [",".join(all_cols)]
    for row, level in zip(rows, levels):
        s = level["client_stats"]
        metrics = (level.get("telemetry") or {}).get("metrics") or {}
        values: dict[str, object] = {
            "concurrency": row["concurrency"],
            "total": row["total"],
            "success": s.get("successful", ""),
            "failed": s.get("failed", ""),
            "success_rate_pct": row["success_rate_pct"],
            "duration_s": s.get("duration_s", ""),
            "rps": row["rps"],
            "rps_per_user": row["rps_per_user"],
            "lat_min_ms": row.get("lat_min", ""),
            "lat_mean_ms": row.get("lat_mean", ""),
            "lat_max_ms": row.get("lat_max", ""),
            "avg_results": row.get("avg_results", ""),
            "avg_bytes": row.get("avg_bytes", ""),
            "gpu_util_mean": row.get("gpu_util_mean", ""),
            "gpu_util_mean_minus_idle": row.get("gpu_util_mean_minus_idle", ""),
            "gpu_util_max": row.get("gpu_util_max", ""),
        }
        for c in p_cols:
            values[f"{c}_ms"] = row.get(c, "")
        for col in gpu_cols:
            values[col] = ""
        for name in metric_names:
            agg = metrics.get(name) or {}
            overall = agg.get("overall") or {}
            values[f"gpu_{name}_mean"] = overall.get("mean", "")
        util = metrics.get(PRIMARY_GPU_METRIC) or {}
        for g in util.get("gpus", []):
            values[f"gpu_util[{g['host']}:{g['device']}]"] = g.get("mean", "")
        lines.append(",".join(str(values.get(c, "")) for c in all_cols))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log.info("Wrote CSV artifact: %s", path)


# ---------------------------------------------------------------------------
# HTML report (self-contained)
# ---------------------------------------------------------------------------


def _esc(value) -> str:
    return html.escape(str(value))


def _svg_chart(
    title: str,
    x_values: list[float],
    series: dict[str, list[float | None]],
    x_label: str = "concurrency",
    y_label: str = "",
    width: int = 460,
    height: int = 240,
) -> str:
    """Minimal dependency-free SVG line chart with axes + legend."""
    pad_l, pad_b, pad_t, pad_r = 46, 30, 22, 12
    ys_all = [v for vals in series.values() for v in vals if isinstance(v, (int, float))]
    if not x_values or not ys_all:
        return ""
    y_min, y_max = min(ys_all), max(ys_all)
    if y_max == y_min:
        y_max = y_min + 1
    x_min, x_max = min(x_values), max(x_values)
    if x_max == x_min:
        x_max = x_min + 1

    def X(v):
        return pad_l + (v - x_min) / (x_max - x_min) * (width - pad_l - pad_r)

    def Y(v):
        return pad_t + (1 - (v - y_min) / (y_max - y_min)) * (height - pad_t - pad_b)

    parts = [f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">']
    parts.append(f'<rect width="{width}" height="{height}" fill="white"/>')
    parts.append(f'<text x="{pad_l}" y="14" font-size="12" font-weight="bold" fill="#111">{_esc(title)}</text>')
    # gridlines + y ticks
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        yv = y_min + frac * (y_max - y_min)
        y = Y(yv)
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(
            f'<text x="{pad_l - 4}" y="{y + 3:.1f}" font-size="9" fill="#555" text-anchor="end">{yv:.4g}</text>'
        )
    # x ticks (first / last / middle)
    for xv in sorted({x_min, (x_min + x_max) / 2, x_max}):
        parts.append(
            f'<text x="{X(xv):.1f}" y="{height - pad_b + 12}" font-size="9" fill="#555" text-anchor="middle">'
            f"{xv:.4g}</text>"
        )
    parts.append(
        f'<text x="{(pad_l + width - pad_r) / 2:.0f}" y="{height - 2}" font-size="9" fill="#777" text-anchor="middle">'
        f"{_esc(x_label)}</text>"
    )
    for i, (name, vals) in enumerate(series.items()):
        color = _PALETTE[i % len(_PALETTE)]
        pts = []
        for xv, val in zip(x_values, vals):
            if isinstance(val, (int, float)):
                pts.append(f"{X(xv):.1f},{Y(val):.1f}")
        if pts:
            parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="2"/>')
        parts.append(
            f'<rect x="{pad_l + i * 110}" y="{height - 14}" width="9" height="3" fill="{color}"/>'
            f'<text x="{pad_l + i * 110 + 13}" y="{height - 9}" font-size="9" fill="#333">{_esc(name)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _level_detail_rows(level_json: dict) -> str:
    util = (level_json.get("telemetry") or {}).get("metrics", {}).get(PRIMARY_GPU_METRIC) or {}
    gpus = util.get("gpus", [])
    if not gpus:
        return ""
    rows = ["<tr><th>host</th><th>device</th><th>mean</th><th>mean−idle</th><th>max</th><th>labels</th></tr>"]
    for g in gpus:
        rows.append(
            f"<tr><td>{_esc(g.get('host'))}</td><td>{_esc(g.get('device'))}</td>"
            f"<td>{g.get('mean', '')}</td><td>{g.get('mean_minus_idle', '')}</td><td>{g.get('max', '')}</td>"
            f"<td>{_esc(json.dumps(g.get('labels', {})))}</td></tr>"
        )
    return "<table>" + "".join(rows) + "</table>"


def render_html(payload: dict, title: str = "Endpoint benchmark report") -> str:
    rows = payload["summary"]["rows"]
    knee = payload["summary"]["knee"]
    conc = [r["concurrency"] for r in rows]

    lat_series = {
        "p50": [r["lat_median"] for r in rows],
        "p95": [r["lat_p95"] for r in rows],
        "p99": [r["lat_p99"] for r in rows],
    }
    charts = [
        _svg_chart("Latency (ms) vs concurrency", conc, lat_series),
        _svg_chart("Throughput (req/s) vs concurrency", conc, {"req/s": [r["rps"] for r in rows]}),
        _svg_chart(
            "Per-user throughput (req/s per user) vs concurrency",
            conc,
            {"req/s/user": [r["rps_per_user"] for r in rows]},
        ),
    ]
    # one chart per GPU metric (overall mean; plus −idle line when a baseline exists)
    metric_names: list[str] = []
    for level in payload["levels"]:
        for name, agg in (level.get("telemetry") or {}).get("metrics", {}).items():
            if isinstance(agg, dict) and agg.get("overall") and name not in metric_names:
                metric_names.append(name)
    for name in metric_names:
        series: dict[str, list[float | None]] = {}
        series[name] = [
            ((l.get("telemetry") or {}).get("metrics", {}).get(name) or {}).get("overall", {}).get("mean")
            for l in payload["levels"]
        ]
        if any(r.get("gpu_util_mean_minus_idle") is not None for r in rows) and name == PRIMARY_GPU_METRIC:
            series[f"{name} −idle"] = [r.get("gpu_util_mean_minus_idle") for r in rows]
        charts.append(_svg_chart(f"{name} (overall mean) vs concurrency", conc, series))

    knee_html = (
        f'<div class="knee">Knee point: <b>N={knee["concurrency"]}</b> — {_esc(knee["detail"])}</div>'
        if knee
        else '<div class="knee">Knee point: none observed across the tested concurrency levels.</div>'
    )
    notes = "".join(f"<li>{_esc(n)}</li>" for n in payload["summary"]["notes"])
    annotations = payload["config"].get("annotations") or []
    annotations_html = "".join(f"<li>{_esc(a)}</li>" for a in annotations)

    level_sections = []
    for level in payload["levels"]:
        s = level["client_stats"]
        lat = s["latency_ms"]
        tele = level.get("telemetry") or {}
        tele_summary = "no telemetry"
        if tele:
            util_overall = (tele.get("metrics", {}).get(PRIMARY_GPU_METRIC) or {}).get("overall") or {}
            tele_summary = (
                f"GPU_UTIL overall mean {util_overall.get('mean', 'n/a')} (max {util_overall.get('max', 'n/a')})"
            )
        # Error deep-dive: per-level category rollup + top raw errors.
        errs = s.get("errors") or {}
        cats = categorize_errors(errs)
        total_failed = s.get("failed", 0)
        cat_rows = "".join(
            f"<tr><td>{_esc(c)}</td><td>{n}</td><td>{(n / total_failed * 100):.0f}%</td></tr>" for c, n in cats.items()
        )
        cat_table = (
            f"<p><b>Errors by category</b> ({total_failed} failed)</p>"
            f"<table><tr><th>category</th><th>count</th><th>share</th></tr>{cat_rows}</table>"
            if cats
            else "<p>No failed requests at this level.</p>"
        )
        top_errs = "".join(f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>" for k, v in list(errs.items())[:8])
        err_table = (
            f"<p><b>Top error keys</b> (bounded snippets)</p><table><tr><th>error</th><th>count</th></tr>{top_errs}</table>"
            if top_errs
            else ""
        )
        status = s.get("status_codes") or {}
        status_rows = "".join(f"<tr><td>{code}</td><td>{n}</td></tr>" for code, n in sorted(status.items()))
        status_table = (
            f"<p><b>HTTP status codes</b></p><table><tr><th>status</th><th>count</th></tr>{status_rows}</table>"
            if status_rows
            else ""
        )
        tail_keys = [k for k in ("p99", "p95") if k in lat] or [k for k in lat if k.startswith("p")]
        tail_summary = ", ".join(f"{k} {lat[k]:.0f}ms" for k in tail_keys[:2]) or "no latency data"
        level_sections.append(
            f"<details><summary>N={level['concurrency']} — "
            f"{s['total_requests']} reqs, {s['success_rate_pct']}% ok, "
            f"{tail_summary} — {tele_summary}</summary>"
            f"<pre>window: {datetime.fromtimestamp(level['t_start_epoch'], UTC).isoformat()} → "
            f"{datetime.fromtimestamp(level['t_end_epoch'], UTC).isoformat()}</pre>"
            f"{cat_table}{err_table}{status_table}{_level_detail_rows(level)}</details>"
        )

    # Run-wide error rollup across all levels (the deep-dive headline table).
    run_cats: dict[str, int] = {}
    worst_level: dict[str, int] = {}
    for level in payload["levels"]:
        s = level["client_stats"]
        for cat, n in categorize_errors(s.get("errors") or {}).items():
            run_cats[cat] = run_cats.get(cat, 0) + n
            worst_level[cat] = max(worst_level.get(cat, 0), level["concurrency"])
    run_cat_rows = "".join(
        f"<tr><td>{_esc(c)}</td><td>{n}</td><td>N≤{worst_level.get(c, '-')}</td></tr>" for c, n in run_cats.items()
    )
    error_section = (
        f"<h2>Errors by category (whole run)</h2>"
        f"<table><tr><th>category</th><th>count</th><th>worst level</th></tr>{run_cat_rows}</table>"
        if run_cats
        else ""
    )

    cfg = json.dumps(payload["config"], indent=2)
    baseline = payload.get("idle_baseline")
    baseline_html = ""
    if baseline:
        util = (baseline.get("metrics") or {}).get(PRIMARY_GPU_METRIC) or {}
        baseline_html = (
            f"<h3>Idle baseline ({baseline.get('duration_s', 0):.0f}s before the sweep)</h3>"
            f"<p>GPU_UTIL overall mean {util.get('overall', {}).get('mean', 'n/a')} across "
            f"{util.get('series_count', 0)} GPU series — subtracted from level means as <code>mean−idle</code>.</p>"
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{_esc(title)}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 24px; color: #111; }}
h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 28px; }} h3 {{ font-size: 13px; }}
table {{ border-collapse: collapse; font-size: 12px; margin: 8px 0; }}
th, td {{ border: 1px solid #d1d5db; padding: 3px 8px; text-align: right; }}
th:nth-child(1), td:nth-child(1) {{ text-align: left; }}
th {{ background: #f3f4f6; }}
pre {{ background: #f8f8f8; padding: 8px; font-size: 11px; overflow-x: auto; }}
.knee {{ margin: 12px 0; padding: 10px 12px; background: #fef3c7; border: 1px solid #f59e0b; border-radius: 6px; font-size: 13px; }}
.charts {{ display: flex; flex-wrap: wrap; gap: 12px; }}
details {{ margin: 6px 0; font-size: 12px; }}
summary {{ cursor: pointer; }}
.meta {{ color: #555; font-size: 12px; }}
</style></head><body>
<h1>{_esc(title)}</h1>
<p class="meta">run {_esc(payload["run_id"])} · started {_esc(payload["started_utc"])} ·
finished {_esc(payload["finished_utc"])}</p>
{f'<ul class="meta"><b>Notes:</b> {annotations_html}</ul>' if annotations_html else ""}
{knee_html}
{f'<ul class="meta">{notes}</ul>' if notes else ""}
<h2>Scaling summary</h2>
<pre>{_esc(format_scaling_table(rows))}</pre>
<h2>Charts</h2>
<div class="charts">{"".join(c for c in charts if c)}</div>
{error_section}
{baseline_html}
<h2>Per-level details</h2>
{"".join(level_sections) or "<p>No levels recorded.</p>"}
<h2>Run configuration (secrets redacted)</h2>
<pre>{_esc(cfg)}</pre>
</body></html>"""


def write_html(path: str, payload: dict, title: str = "Endpoint benchmark report") -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(payload, title))
    log.info("Wrote HTML report: %s", path)


# ---------------------------------------------------------------------------
# Markdown report (the results/ tree artifact)
# ---------------------------------------------------------------------------
#
# Written in the ModelBenchmarker results-tree convention (H1 title, Status:
# line, ``## Benchmark configuration`` / ``## Benchmark results`` sections)
# so ``results_to_html.py`` ingests it unchanged: its ``parse_rag_table``
# reads the two-cell ``| Metric | Value |`` rows of those two sections and
# classifies the file as a RAG-style (non-chat) result. The wide per-level
# tables are deliberately multi-column — the 2-cell parser skips them, and
# the chat-row parser requires exactly 12/16 numeric columns with an int in
# position 3, which these tables never produce (rps sits in position 3).


def _md_table(headers: list[str], rows: list[list[object]], align: str | None = None) -> str:
    """A GitHub-flavored Markdown table. ``align``: 'l' or 'r' per column."""
    if align is None:
        align = "r"  # numeric-first tables: right-align everything but col 0
    head = "| " + " | ".join(headers) + " |"
    sep = "|" + "|".join((":" if a == "l" else "") + "---" for a in align) + "|"
    body = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return f"{head}\n{sep}\n{body}"


def _md_kv_table(pairs: list[tuple[str, object]]) -> str:
    """Two-cell ``| Metric | Value |`` table — the shape parse_rag_table reads."""
    return _md_table(["Metric", "Value"], [[k, v] for k, v in pairs], align="ll")


def _fmt_num(value, prec: int = 2) -> str:
    return f"{value:.{prec}f}" if isinstance(value, (int, float)) else "–"


def render_md(payload: dict, title: str = "Endpoint benchmark report") -> str:
    rows = payload["summary"]["rows"]
    knee = payload["summary"].get("knee")
    cfg = payload["config"]
    target = cfg.get("target") or {}
    load = cfg.get("load") or {}
    tele = cfg.get("telemetry") or {}
    annotations = cfg.get("annotations") or []
    p_cols = _percentile_cols(rows)

    config_pairs: list[tuple[str, object]] = [
        ("Mode", str(cfg.get("mode", "?")).upper()),
        ("Target", target.get("url", "n/a")),
    ]
    if cfg.get("mode") == "rest":
        config_pairs += [("Method", target.get("method", "GET"))]
    else:
        config_pairs += [
            ("Transport", target.get("transport", "streamable-http")),
            ("Tool", target.get("tool") or "(auto)"),
        ]
    config_pairs += [
        ("Dataset", target.get("dataset") or "(auto)"),
        (
            "Query pool",
            f"{load.get('queries', {}).get('count', '?')} queries ({load.get('queries', {}).get('source', '?')})",
        ),
        ("Concurrency levels", ", ".join(str(n) for n in load.get("levels", []))),
        ("Duration per level", f"{load.get('duration_s', '?')} s"),
        ("Ramp-up", f"{load.get('ramp_up_s', '?')} s"),
        ("Warm-up rounds", load.get("warmup_rounds", "?")),
        ("Settle", f"{load.get('settle_s', '?')} s"),
        ("Call timeout", f"{load.get('call_timeout_s', '?')} s"),
        ("Percentiles", ", ".join(cfg.get("percentiles", [])) or "p50, p95, p99"),
    ]
    if tele:
        config_pairs += [
            ("Prometheus", tele.get("prom_url", "")),
            ("Prom selector", tele.get("selector") or "(none)"),
            ("Prom step", tele.get("step", "15s")),
            (
                "Idle baseline",
                f"{tele['baseline_duration_s']} s" if tele.get("baseline_duration_s") else "off",
            ),
            ("GPU metrics", ", ".join(tele.get("gpu_metrics", []))),
        ]
    config_pairs += [
        ("Seed", load.get("seed") if load.get("seed") is not None else "(per-user)"),
        ("TLS verify", "ON" if not target.get("insecure") else "OFF (--insecure)"),
        ("Tool version", cfg.get("tool_version", "?")),
        ("Run", payload.get("run_id", "?")),
    ]
    for note in annotations:
        config_pairs.append(("Note", note))

    total_reqs = sum(r["total"] for r in rows)
    total_failed = sum((l.get("client_stats") or {}).get("failed", 0) for l in payload["levels"])
    best_rps_row = max(rows, key=lambda r: r["rps"]) if rows else None
    summary_pairs: list[tuple[str, object]] = [
        ("Levels run", len(rows)),
        ("Total requests", total_reqs),
        ("Failed requests", total_failed),
        (
            "Overall success rate",
            f"{(100 * (total_reqs - total_failed) / total_reqs):.2f}%" if total_reqs else "n/a",
        ),
    ]
    if best_rps_row:
        summary_pairs.append(("Best throughput", f"{best_rps_row['rps']:.1f} req/s at N={best_rps_row['concurrency']}"))
    summary_pairs.append(
        (
            "Knee point",
            f"N={knee['concurrency']} — {knee['detail']}" if knee else "none observed across the tested levels",
        )
    )

    parts = [
        f"# {title}",
        "",
        f"Status: complete (last updated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')})",
        "",
        f"Universal endpoint benchmark (endpoint-benchmarker {cfg.get('tool_version', '?')}). "
        "Run id: " + str(payload.get("run_id", "?")) + ".",
        "",
        "## Benchmark configuration",
        "",
        _md_kv_table(config_pairs),
        "",
        "## Benchmark results",
        "",
        _md_kv_table(summary_pairs),
        "",
        "### Scaling by concurrency level",
        "",
    ]

    headers = ["N", "reqs", "ok%", "rps", "rps/user"] + [f"{c[4:]} (ms)" for c in p_cols] + ["max (ms)"]
    headers += ["avg results", "avg bytes", "window s"]
    has_gpu = any("gpu_util_mean" in r for r in rows)
    if has_gpu:
        headers += ["GPU_UTIL", "GPU_UTIL −idle", "gpu samples"]
    table_rows = []
    for r in rows:
        cells = [r["concurrency"], r["total"], r["success_rate_pct"], r["rps"], r["rps_per_user"]]
        cells += [_fmt_num(r.get(c)) for c in p_cols]
        cells += [
            _fmt_num(r.get("lat_max"), 0),
            _fmt_num(r.get("avg_results")),
            _fmt_num(r.get("avg_bytes"), 1),
            _fmt_num(r.get("window_s"), 1),
        ]
        if has_gpu:
            cells += [
                _fmt_num(r.get("gpu_util_mean"), 1),
                _fmt_num(r.get("gpu_util_mean_minus_idle"), 1)
                if r.get("gpu_util_mean_minus_idle") is not None
                else "–",
                r.get("gpu_samples", "–"),
            ]
        table_rows.append(cells)
    parts += [_md_table(headers, table_rows, align="l" + "r" * (len(headers) - 1)), ""]

    # -- Error deep-dive ------------------------------------------------------
    run_cats: dict[str, int] = {}
    worst_level: dict[str, int] = {}
    for level in payload["levels"]:
        s = level.get("client_stats") or {}
        for cat, n in categorize_errors(s.get("errors") or {}).items():
            run_cats[cat] = run_cats.get(cat, 0) + n
            worst_level[cat] = max(worst_level.get(cat, 0), level["concurrency"])
    if run_cats:
        parts += [
            "### Errors by category",
            "",
            _md_table(
                ["Category", "Total", "Worst level"],
                [[c, n, f"N≤{worst_level.get(c, '-')}"] for c, n in run_cats.items()],
                align="lrr",
            ),
            "",
            "### Error detail (top errors per level)",
            "",
        ]
        err_rows = []
        for level in payload["levels"]:
            errs = (level.get("client_stats") or {}).get("errors") or {}
            for key, n in list(errs.items())[:6]:
                err_rows.append([level["concurrency"], key, n])
            if len(errs) > 6:
                err_rows.append([level["concurrency"], f"(+{len(errs) - 6} more distinct errors)", ""])
        parts += [_md_table(["N", "Error", "Count"], err_rows, align="llr"), ""]
    else:
        parts += ["### Errors", "", "No failed requests in this run.", ""]

    # -- HTTP status codes ----------------------------------------------------
    status_rows = [
        [level["concurrency"], code, n]
        for level in payload["levels"]
        for code, n in sorted(((level.get("client_stats") or {}).get("status_codes") or {}).items())
    ]
    if status_rows:
        parts += ["### HTTP status codes", "", _md_table(["N", "Status", "Count"], status_rows, align="lrr"), ""]

    # -- GPU telemetry per level ----------------------------------------------
    gpu_rows = []
    for level in payload["levels"]:
        metrics = (level.get("telemetry") or {}).get("metrics") or {}
        for name, agg in metrics.items():
            if not isinstance(agg, dict) or not agg.get("overall"):
                continue
            overall = agg["overall"]
            gpu_rows.append(
                [
                    level["concurrency"],
                    name,
                    _fmt_num(overall.get("mean"), 1),
                    _fmt_num(overall.get("mean_minus_idle"), 1) if overall.get("mean_minus_idle") is not None else "–",
                    _fmt_num(overall.get("max"), 1),
                    len(agg.get("gpus", [])),
                    agg.get("samples", "–"),
                ]
            )
    if gpu_rows:
        parts += [
            "### GPU telemetry per level",
            "",
            _md_table(["N", "Metric", "Mean", "Mean − idle", "Max", "GPUs", "Samples"], gpu_rows, align="llrrrrr"),
            "",
        ]
        baseline = payload.get("idle_baseline")
        if baseline:
            util = (baseline.get("metrics") or {}).get(PRIMARY_GPU_METRIC) or {}
            baseline_note = (
                f"Idle baseline ({baseline.get('duration_s', 0):.0f}s): GPU_UTIL mean "
                f"{_fmt_num((util.get('overall') or {}).get('mean'), 1)} across "
                f"{util.get('series_count', 0)} GPU series — subtracted as 'Mean − idle'."
            )
            parts += [baseline_note, ""]

    if payload["summary"].get("notes"):
        parts += ["## Notes", ""]
        parts += [f"- note: {n}" for n in payload["summary"]["notes"]]
        parts.append("")
    return "\n".join(parts)


def write_md(path: str, payload: dict, title: str | None = None) -> None:
    """Write the markdown report atomically (``.partial`` + ``os.replace``),
    same durability rule as benchmark_chat.py's ``--output`` writer: a run
    killed mid-write can never leave a truncated file behind."""
    out_path = Path(path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if title is None:
        title = out_path.stem
    tmp_path = out_path.with_name(out_path.name + ".partial")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(render_md(payload, title=title))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, out_path)
    log.info("Wrote markdown report: %s", out_path)


def write_artifacts(
    payload: dict,
    *,
    json_path: str | None = None,
    csv_path: str | None = None,
    html_path: str | None = None,
    md_path: str | None = None,
    title: str | None = None,
) -> None:
    """Write any subset of the four artifacts from one payload."""
    if json_path:
        _ensure_parent(json_path)
        write_json(json_path, payload)
    if csv_path:
        _ensure_parent(csv_path)
        write_csv(csv_path, payload)
    if html_path:
        _ensure_parent(html_path)
        write_html(html_path, payload, title=title or "Endpoint benchmark report")
    if md_path:
        # Markdown H1 defaults to the file stem — the results/ tree convention
        # (matches benchmark_chat.py's --output files).
        write_md(md_path, payload, title=None)
