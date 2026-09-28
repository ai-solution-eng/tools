"""Comparison model for the results page's side-by-side run view.

compare.py holds the PURE logic: normalized run summaries (produced by
``RunManager.compare_data`` in runner.py, which owns the path safety) are
merged here into sections of merged rows with per-run cells and delta
columns against the first selected run (the reference).

The response shape:

    {
      "runs": [per-run dicts: run_id/kind/target_url/status/started_utc/
               params_label/config/rows/knee/error],
      "sections": [
        {"kind": "endpoint"|"chat",
         "reference_run": "<run_id>",
         "columns": ["rps", "lat_p50_ms", ...],
         "runs": [<run_id>...],          # section's runs, reference first
         "rows": [{"label": "N=4",
                   "cells": {"rps": {"<run_id>": 12.3, ...}, ...},
                   "delta_vs_reference": {"rps": {"<run_id>": 25.0}, ...}}]}
      ],
      "missing": ["<id>", ...]
    }

Delta semantics: percent change vs the reference run's value for the same
label, signed so that a POSITIVE number is always an improvement per the
metric's DIRECTIONS ("higher is better" / "lower is better"). Ties / absent
values / non-numeric metrics produce no delta entry. "info"-direction
metrics (gpu_util_mean) never get deltas.
"""

from __future__ import annotations

from typing import Any, Final

# Which way is "better" per metric. "higher"/"lower" flip the sign of the
# displayed delta; "info" metrics are descriptive and get no delta.
DIRECTIONS: Final[dict[str, str]] = {
    "rps": "higher",
    "tokens_p50": "higher",
    "ttft_turn1_p50_ms": "lower",
    "ttft_post_p50_ms": "lower",
    "lat_p50_ms": "lower",
    "lat_p99_ms": "lower",
    "lat_max_ms": "lower",
    "failed": "lower",
    "success_rate_pct": "higher",
    "gpu_util_mean": "info",
}

# Metrics that may legitimately be absent per row (dash in the UI).
DELTA_SKIP_DIRECTIONS: Final[frozenset[str]] = frozenset({"info"})

MAX_COMPARE_RUNS: Final[int] = 4


def params_label(kind: str, params: dict) -> str:
    """Short one-line label: mode+sweep/N for endpoint runs, users/contexts
    for chat runs. Derived from run_meta params (already redacted at write
    time; no header values are ever read)."""
    p = params or {}

    def _list(key: str) -> str:
        v = p.get(key)
        if v in (None, ""):
            return ""
        if isinstance(v, (list, tuple)):
            return ",".join(str(x) for x in v)
        return str(v).strip()

    if kind == "chat":
        users = _list("number_users")
        ctx = _list("context_length")
        tasks = str(p.get("tasks") or "").strip()
        bits = []
        if users:
            bits.append(f"users={users}")
        if ctx:
            bits.append(f"ctx={ctx}")
        if tasks:
            bits.append(tasks)
        arrival = str(p.get("arrival_mode") or "").strip()
        if arrival:
            bits.append(f"arrival={arrival}")
        return " ".join(bits) if bits else kind
    mode = str(p.get("mode") or "rest").strip()
    sweep = _list("sweep")
    n = _list("N")
    if sweep:
        return f"{mode} sweep={sweep}"
    if n:
        return f"{mode} N={n}"
    return mode


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _pct_delta(new: Any, ref: Any) -> float | None:
    """Percent change of ``new`` vs ``ref``, or None when not computable."""
    a, b = _num(new), _num(ref)
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / abs(b) * 100.0, 1)


def _signed_delta(new: Any, ref: Any, direction: str) -> float | None:
    """Delta signed so POSITIVE always means an improvement: for a
    lower-is-better metric, a latency drop (new < ref) flips to positive.
    Returns None for non-numeric/absent/zero-reference pairs."""
    d = _pct_delta(new, ref)
    if d is None:
        return None
    return -d if direction == "lower" else d


def normalize_endpoint_rows(run_json: dict) -> list[dict]:
    """run.json summary.rows -> normalized comparison rows.

    Row shape: {"label": "N=<concurrency>", "metrics": {...}}. Every lat_*
    percentile the run configured is normalized to a ``lat_pNN`` metric key
    (lat_p50_ms, lat_p99_ms, lat_p99.9_ms — the run's own percentile set).
    """
    rows_out: list[dict] = []
    summary = run_json.get("summary") if isinstance(run_json, dict) else None
    raw_rows = summary.get("rows") if isinstance(summary, dict) else None
    if not isinstance(raw_rows, list):
        return rows_out
    for r in raw_rows:
        if not isinstance(r, dict):
            continue
        conc = r.get("concurrency")
        metrics: dict[str, Any] = {}
        for k, v in r.items():
            if k in ("concurrency", "total", "window_s", "avg_results", "avg_bytes"):
                continue  # identity/diagnostic fields, not comparison columns
            if k.startswith("lat_") and k not in ("lat_min", "lat_mean"):
                # lat_p50 -> lat_p50_ms (min/mean stay out; percentiles only)
                metrics[k + "_ms"] = _num(v)
            elif k in DIRECTIONS:
                metrics[k] = _num(v)
        rows_out.append({"label": f"N={conc}", "metrics": metrics})
    return rows_out


def normalize_chat_rows(report_md: str) -> list[dict]:
    """report.md -> normalized comparison rows, one per distinct
    (ctx, users, task) tuple.

    Chat sweeps repeat the same (ctx, users, task) across levels; later rows
    in file order are later levels, so the LAST occurrence of each tuple wins.
    """
    # imported lazily: keeps compare.py importable standalone and the webapp
    # runtime resilient if results_to_html ever grows heavy deps
    import importlib

    parser = importlib.import_module("model_benchmarker.results_to_html")
    _mode, rows, _arrival = parser.parse_chat_table_full(report_md or "")

    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for r in rows:
        ttft = r.get("ttft") or []
        tokens = r.get("tokens") or []
        if not ttft or not tokens:
            continue
        ttft_post = r.get("ttft_post") or []
        key = (r.get("ctx"), r.get("users"), r.get("task"))
        metrics = {
            "ttft_turn1_p50_ms": _num(ttft[0]) if len(ttft) > 0 else None,
            "ttft_turn1_p95_ms": _num(ttft[1]) if len(ttft) > 1 else None,
            "ttft_post_p50_ms": _num(ttft_post[0]) if len(ttft_post) > 0 else None,
            "ttft_post_p95_ms": _num(ttft_post[1]) if len(ttft_post) > 1 else None,
            "tokens_p50": _num(tokens[0]) if len(tokens) > 0 else None,
            "tokens_p95": _num(tokens[1]) if len(tokens) > 1 else None,
            "failed": _num(r.get("failed")),
        }
        row = {"label": f"ctx={key[0]} users={key[1]} {key[2]}", "metrics": metrics}
        if key in seen:
            seen[key]["metrics"] = metrics  # later levels overwrite earlier ones
        else:
            seen[key] = row
            out.append(row)
    return out


def _columns_for(rows: list[dict]) -> list[str]:
    """Union of metric keys across a kind's rows, first-appearance ordered
    with the DIRECTIONS-known columns leading (stable column order)."""
    known = [k for k in DIRECTIONS if any(k in r["metrics"] for r in rows)]
    unknown: list[str] = []
    for r in rows:
        for k in r["metrics"]:
            if k not in known and k not in unknown:
                unknown.append(k)
    return known + unknown


def _reference_id(runs_data: list[dict]) -> str | None:
    return runs_data[0]["run_id"] if runs_data else None


def build_comparison(runs_data: list[dict]) -> dict:
    """Merge normalized per-run dicts into the comparison payload.

    Rows with the same label are aligned across runs; the union of labels is
    ordered by first appearance (reference run first, then each later run's
    new labels in its own file order). Same-label same-kind runs of DIFFERENT
    kinds never mix — sections are grouped per kind, each with its own
    reference (the first selected run OF that kind) and its own columns.
    """
    present = [r for r in runs_data if r.get("run_id")]
    sections: list[dict] = []
    for kind in ("endpoint", "chat"):
        kind_runs = [r for r in present if r.get("kind") == kind and r.get("rows")]
        if not kind_runs:
            continue
        ref_id = _reference_id(kind_runs)
        if ref_id is None:  # unreachable: kind_runs is non-empty here
            continue
        run_ids = [r["run_id"] for r in kind_runs]
        # union of labels, first appearance across the runs in selection order
        labels: list[str] = []
        for r in kind_runs:
            for row in r["rows"]:
                lab = row["label"]
                if lab not in labels:
                    labels.append(lab)
        by_label: dict[str, dict[str, dict]] = {}
        for r in kind_runs:
            for row in r["rows"]:
                by_label.setdefault(row["label"], {}).setdefault(r["run_id"], row["metrics"])
        all_rows = []
        for lab in labels:
            per_run = by_label[lab]
            cells: dict[str, dict] = {}
            deltas: dict[str, dict] = {}
            for rid in run_ids:
                metrics = per_run.get(rid) or {}
                for metric, value in metrics.items():
                    cells.setdefault(metric, {})[rid] = value
            # columns: union across runs for this label — computed at the
            # section level below; here only delta computation
            for metric in cells:
                direction = DIRECTIONS.get(metric)
                if direction in DELTA_SKIP_DIRECTIONS or direction is None:
                    continue
                ref_val = (per_run.get(ref_id) or {}).get(metric)
                if ref_val is None:
                    continue
                for rid in run_ids:
                    if rid == ref_id:
                        continue
                    d = _signed_delta((per_run.get(rid) or {}).get(metric), ref_val, direction)
                    if d is not None:
                        deltas.setdefault(metric, {})[rid] = d
            all_rows.append({"label": lab, "cells": cells, "delta_vs_reference": deltas})
        columns = _columns_for([row for r in kind_runs for row in r["rows"]])
        sections.append(
            {
                "kind": kind,
                "reference_run": ref_id,
                "runs": run_ids,
                "columns": columns,
                "rows": all_rows,
            }
        )
    return {"runs": runs_data, "sections": sections, "missing": []}


def missing_ids(requested: list[str], runs_data: list[dict]) -> list[str]:
    """Requested ids that resolved to no run on disk (skipped, reported)."""
    found = {r.get("run_id") for r in runs_data}
    return [rid for rid in requested if rid not in found]


def finalize_comparison(runs_data: list[dict], requested: list[str]) -> dict:
    """build_comparison + the missing-id list (the route's single entry
    point). Duplicate requested ids collapse to their first position."""
    seen: set[str] = set()
    unique: list[str] = []
    for rid in requested:
        if rid not in seen:
            seen.add(rid)
            unique.append(rid)
    out = build_comparison(runs_data)
    out["missing"] = missing_ids(unique, runs_data)
    return out
