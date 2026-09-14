"""Prometheus GPU telemetry, joined onto each concurrency level's wall-clock window.

Implemented against the Prometheus **HTTP API** (``/api/v1/query_range``),
not by driving the Prometheus MCP server from a script — the MCP server is
for interactive verification while a human is in the loop; an automated
harness needs a plain HTTP client and no session state.

For each level the harness records ``t_start``/``t_end`` (epoch seconds) and
this module pulls each configured metric over exactly that window and
aggregates **per GPU** (grouped by the DCGM identity labels).  With the DCGM
exporter's k8s pod-mapping enabled, series carry ``exported_namespace`` /
``exported_container`` labels, so ``--prom-selector 'exported_namespace="henkia"'``
attributes utilization to the customer's pods specifically.

Caveats this module surfaces rather than hides:

* A level shorter than ~3 scrape intervals yields a garbage average — the
  sweep warns and the report annotates sample counts.
* On shared GPU nodes other tenants' work contaminates the numbers; an idle
  baseline (``--baseline-duration``) is captured before the sweep and both
  raw and baseline-subtracted means are reported.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

import httpx

from .stats import err_key

log = logging.getLogger("endpoint_benchmarker")

DEFAULT_GPU_METRICS = (
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_PROF_GR_ENGINE_ACTIVE",
    "DCGM_FI_DEV_FB_USED",
    "DCGM_FI_DEV_MEM_COPY_UTIL",
    "DCGM_FI_DEV_POWER_USAGE",
)

# Labels that identify a physical GPU (grouping key, in priority order) and
# labels kept for display/attribution.
_GPU_ID_KEYS = ("Hostname", "hostname", "instance")
_DEVICE_KEYS = ("gpu", "device", "UUID", "uuid")
_DISPLAY_KEYS = ("exported_namespace", "exported_container", "pod", "modelName", "device", "UUID")


@dataclass
class TelemetryConfig:
    prom_url: str
    selector: str = ""  # extra label matcher, e.g. 'exported_namespace="henkia"'
    step: str = "15s"
    gpu_metrics: tuple[str, ...] = DEFAULT_GPU_METRICS
    extra_queries: dict[str, str] = field(default_factory=dict)  # name -> full PromQL
    baseline_duration: float = 0.0  # idle capture seconds before the sweep (0 = off)
    timeout: float = 30.0
    insecure: bool = False

    def validate(self) -> None:
        if not self.prom_url:
            return
        if not self.gpu_metrics and not self.extra_queries:
            raise ValueError("telemetry enabled but no metrics configured")
        if self.step and self.step[-1].isalpha() and self.step[-1] not in "smhd":
            raise ValueError(f"invalid --prom-step {self.step!r} (use e.g. 15s, 30s, 1m)")


def _promql(metric: str, selector: str) -> str:
    return f"{metric}{{{selector}}}" if selector else metric


def _gpu_key(labels: dict) -> tuple[str, str]:
    host = next((labels[k] for k in _GPU_ID_KEYS if labels.get(k)), "?")
    device = next((labels[k] for k in _DEVICE_KEYS if labels.get(k)), "?")
    return (str(host), str(device))


def _display_labels(labels: dict) -> dict:
    return {k: labels[k] for k in _DISPLAY_KEYS if labels.get(k) not in (None, "")}


def _aggregate_series(series: list[dict], t_start: float, t_end: float) -> dict:
    """Aggregate ``query_range`` result series within [t_start, t_end].

    Returns ``{"series_count": n, "samples": n, "gpus": [...], "overall": {...}}``
    where each gpu entry carries its identity labels plus per-value
    mean/min/max over the window (raw values, not rates — DCGM gauges).
    """
    gpus: dict[tuple[str, str], dict] = {}
    total_samples = 0
    for s in series:
        labels = s.get("metric", {})
        values = []
        for ts, val in s.get("values", []):
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue  # unparseable marker
            if not math.isfinite(v):
                continue  # NaN / +Inf staleness markers
            if t_start - 1 <= float(ts) <= t_end + 1:
                values.append(v)
        if not values:
            continue
        total_samples += len(values)
        key = _gpu_key(labels)
        entry = gpus.setdefault(
            key,
            {"labels": _display_labels(labels), "samples": 0},
        )
        entry["samples"] += len(values)
        entry.setdefault("mean", []).append(sum(values) / len(values))
        entry.setdefault("min", []).append(min(values))
        entry.setdefault("max", []).append(max(values))

    gpu_list = []
    for (host, device), entry in sorted(gpus.items()):
        gpu_list.append(
            {
                "host": host,
                "device": device,
                "labels": entry["labels"],
                "samples": entry["samples"],
                "mean": round(sum(entry["mean"]) / len(entry["mean"]), 2),
                "min": round(min(entry["min"]), 2),
                "max": round(max(entry["max"]), 2),
            }
        )

    overall = {}
    if gpu_list:
        means = [g["mean"] for g in gpu_list]
        overall = {
            "mean": round(sum(means) / len(means), 2),
            "min": round(min(g["min"] for g in gpu_list), 2),
            "max": round(max(g["max"] for g in gpu_list), 2),
        }
    return {"series_count": len(series), "samples": total_samples, "gpus": gpu_list, "overall": overall}


def _aggregate_scalar(series: list[dict], t_start: float, t_end: float) -> dict:
    """Aggregate extra (non per-GPU) queries: mean/min/max across all series+samples."""
    values = []
    for s in series:
        for ts, val in s.get("values", []):
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(v):
                continue
            if t_start - 1 <= float(ts) <= t_end + 1:
                values.append(v)
    if not values:
        return {"series_count": len(series), "samples": 0}
    return {
        "series_count": len(series),
        "samples": len(values),
        "mean": round(sum(values) / len(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


async def query_range(
    client: httpx.AsyncClient,
    prom_url: str,
    promql: str,
    t_start: float,
    t_end: float,
    step: str,
    timeout: float = 30.0,
) -> list[dict]:
    resp = await client.get(
        f"{prom_url.rstrip('/')}/api/v1/query_range",
        params={"query": promql, "start": f"{t_start:.3f}", "end": f"{t_end:.3f}", "step": step},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "success":
        raise RuntimeError(f"prometheus query failed: {payload.get('errorType')}: {payload.get('error')}")
    return payload.get("data", {}).get("result", [])


async def capture_window(
    config: TelemetryConfig,
    t_start: float,
    t_end: float,
) -> dict:
    """Fetch + aggregate all configured metrics for one [t_start, t_end] window."""
    out: dict = {"prom_url": config.prom_url, "window": {"start_epoch": t_start, "end_epoch": t_end, "step": config.step}}
    async with httpx.AsyncClient(verify=not config.insecure) as client:
        per_metric: dict[str, dict] = {}
        for metric in config.gpu_metrics:
            promql = _promql(metric, config.selector)
            try:
                series = await query_range(client, config.prom_url, promql, t_start, t_end, config.step, config.timeout)
                agg = _aggregate_series(series, t_start, t_end)
                per_metric[metric] = agg
                if agg["series_count"] == 0:
                    log.warning("telemetry: no series for %s (check --prom-selector / scrape config)", promql)
            except Exception as exc:
                log.warning("telemetry: query failed for %s: %s", metric, err_key(exc))
                per_metric[metric] = {"error": err_key(exc)}
        for name, promql in config.extra_queries.items():
            try:
                series = await query_range(client, config.prom_url, promql, t_start, t_end, config.step, config.timeout)
                per_metric[name] = _aggregate_scalar(series, t_start, t_end)
            except Exception as exc:
                log.warning("telemetry: extra query '%s' failed: %s", name, err_key(exc))
                per_metric[name] = {"error": err_key(exc)}
    out["metrics"] = per_metric
    n_gpus = max(
        (m.get("series_count", 0) for m in per_metric.values() if isinstance(m, dict)),
        default=0,
    )
    out["series_count"] = n_gpus
    return out


async def capture_idle_baseline(config: TelemetryConfig, duration: float) -> dict:
    """Sleep *duration* seconds (true idle window), then capture it."""
    log.info("Capturing %.0fs idle GPU baseline (shared-node contamination check) ...", duration)
    await asyncio.sleep(duration)
    t_end = time.time()
    baseline = await capture_window(config, t_end - duration, t_end)
    baseline["kind"] = "idle_baseline"
    baseline["duration_s"] = duration
    log.info(
        "Idle baseline: %d GPU series, GPU_UTIL mean %s",
        baseline.get("metrics", {}).get(DEFAULT_GPU_METRICS[0], {}).get("series_count", 0),
        baseline.get("metrics", {}).get(DEFAULT_GPU_METRICS[0], {}).get("overall", {}).get("mean", "n/a"),
    )
    return baseline


def _gpu_identity(host: str, device: str) -> tuple[str, str]:
    return (str(host), str(device))


def subtract_baseline(level_telemetry: dict, baseline: dict) -> None:
    """Add ``minus_idle`` (raw - idle, floored at 0) to each per-GPU metric entry.

    Mutates *level_telemetry* in place.  Matching is per (host, device); GPUs
    present under load but absent from the baseline keep raw values only.
    """
    baseline_metrics = baseline.get("metrics", {}) if baseline else {}
    level_metrics = level_telemetry.get("metrics", {})
    for metric, agg in level_metrics.items():
        base_agg = baseline_metrics.get(metric) or {}
        if not isinstance(agg, dict) or "gpus" not in agg or not isinstance(base_agg, dict):
            continue
        idle_by_key = {_gpu_identity(g.get("host", "?"), g.get("device", "?")): g for g in base_agg.get("gpus", [])}
        for gpu in agg.get("gpus", []):
            idle = idle_by_key.get(_gpu_identity(gpu.get("host", "?"), gpu.get("device", "?")))
            if not idle or "mean" not in idle:
                continue
            gpu["mean_minus_idle"] = round(max(0.0, gpu["mean"] - idle["mean"]), 2)
        base_overall = base_agg.get("overall") or {}
        if base_overall.get("mean") is not None and agg.get("overall"):
            agg["overall"]["mean_minus_idle"] = round(
                max(0.0, agg["overall"]["mean"] - base_overall["mean"]), 2
            )
