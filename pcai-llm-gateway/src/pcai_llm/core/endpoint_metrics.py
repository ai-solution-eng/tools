"""Live per-endpoint throughput and load, read from the inference servers' own metrics.

WHY this is not core.offpeak: that module answers ONE question -- "is demand
low enough to discount?" -- with one number per model and a hysteresis state
per target, and its value feeds BILLING (active -> fraction -> price). This
module answers "what is each endpoint doing right now" with several numbers
per pod and NO state at all. Sharing a map between them would be a defect in
both directions: pointing offpeak at a tokens/s query reprices traffic, and
pointing this panel at the discount decision reports a billing posture as if
it were load.

WHERE the numbers come from: the inference server's own Prometheus metrics
(KServe/MLIS pods are scraped by mlis-podmonitor in this fleet). TWO engines
are covered, because this fleet runs both: vLLM for the vision/audio/embedding
pods and SGLang for the chat models. Nothing here is inferred from the
gateway's own traffic, so the panel shows the engine's view, not a proxy.

WHAT "prefill vs decode" CAN and CANNOT be: vLLM runs CHUNKED prefill, so a
single iteration can carry both phases; there is no per-request phase gauge to
read. The honest representation is (a) the two token rates, split by phase,
and (b) the share of request TIME spent in prefill over the window. A binary
"prefill / decode" badge would be a lie for most of a busy engine's life.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

# Query window for the rate() calls. 300s is 10x a 30s scrape: stable enough
# that a single slow scrape does not read as a traffic collapse, short enough
# that "now" still means now. The panel labels it, so the operator is never
# guessing what "t/s" is averaged over.
DEFAULT_WINDOW_S = 300
MIN_WINDOW_S = 60
MAX_WINDOW_S = 3600

# Per-query timeout. The whole snapshot is 7 concurrent queries; a hung
# prometheus must cost the panel, never a request (this route is read-only,
# but it runs in the same process as the data plane).
_QUERY_TIMEOUT_S = 10.0

# {window} is substituted with e.g. "300s".
#
# sum by (namespace, pod, model_name): pod is the identity, namespace keeps
# two same-named pods in different namespaces apart, and model_name is carried
# through because it is the only label that says WHICH model a pod serves
# (it is a filesystem path, see _model_key).
# EVERY query is a regex over BOTH engines' metric families. This fleet runs
# vLLM for the vision/audio/embedding pods and SGLang for the CHAT models
# (verified live 2026-10-30: Qwen3.8-27B-FP8, glm-5.3-flash and
# deepseek-v4.1-flash export sglang:*, not vllm:*). A vLLM-only selector
# rendered a panel with every chat model missing -- the exact models the panel
# exists for -- while still looking perfectly healthy.
#
# One selector per logical metric also means ONE request per metric: querying
# each family separately would double the calls on a 10s poll for no gain.
#
# sum by (namespace, pod, instance, model_name) collapses SGLang's is_streaming=true|false
# split into one row per pod (the two counters are disjoint request classes, so
# summing them is the total, not a double count).
QUERIES: dict[str, str] = {
    "prefill_tps": (
        "sum by (namespace, pod, instance, model_name) "
        '(rate({__name__=~"vllm:prompt_tokens_total|sglang:prompt_tokens_total"}[{window}]))'
    ),
    "decode_tps": (
        "sum by (namespace, pod, instance, model_name) "
        '(rate({__name__=~"vllm:generation_tokens_total|sglang:generation_tokens_total"}[{window}]))'
    ),
    "running": (
        "sum by (namespace, pod, instance, model_name) "
        '({__name__=~"vllm:num_requests_running|sglang:num_running_reqs"})'
    ),
    "waiting": (
        'sum by (namespace, pod, instance, model_name) ({__name__=~"vllm:num_requests_waiting|sglang:num_queue_reqs"})'
    ),
    # Both are a 0..1 cache occupancy ratio: vLLM calls it kv_cache_usage_perc,
    # SGLang calls it token_usage.
    "kv_cache_pct": 'sum by (namespace, pod, instance) ({__name__=~"vllm:kv_cache_usage_perc|sglang:token_usage"}) * 100',
    # CACHE HIT RATE. This is what makes the prompt-token rate readable:
    # prompt_tokens_total counts EVERY prompt token submitted, INCLUDING
    # prefix-cache hits that never reach the GPU. Measured on this fleet
    # (2026-10-30): one prefill batch was "#new-token: 1682, #cached-token:
    # 35840" -- 96% cached -- while prompt_tokens_total reported the full
    # 37 522. Without this column a cache-heavy workload looks like an
    # impossible amount of prefill work. SGLang only; vLLM rows read "—".
    "cache_hit_pct": 'sum by (namespace, pod, instance) ({__name__="sglang:cache_hit_rate"}) * 100',
    # ABSOLUTE tokens held in the KV pool. A percentage of a ~12M-token pool
    # sits at 0.x% for a normal workload, so the fraction alone cannot say
    # whether that is 3 conversations or 300. SGLang only (vLLM exports the
    # ratio but no used-token gauge); absent rows simply omit the tooltip.
    "used_tokens": 'sum by (namespace, pod, instance) ({__name__="sglang:num_used_tokens"})',
    # Prefill/decode TIME histograms are vLLM-only. SGLang exports no
    # equivalent split, so those rows show "—" rather than a made-up ratio.
    "prefill_s": 'sum by (namespace, pod, instance) (rate({__name__="vllm:request_prefill_time_seconds_sum"}[{window}]))',
    "decode_s": 'sum by (namespace, pod, instance) (rate({__name__="vllm:request_decode_time_seconds_sum"}[{window}]))',
}

# models--Qwen--Qwen3-VL-Reranker-8B/snapshots/<sha>  ->  Qwen/Qwen3-VL-Reranker-8B
_MODEL_PATH_RE = re.compile(r"models--([^/]+)")
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _model_key(raw: str) -> str:
    """Normalise a model path or a pool name to a comparable key. PURE.

    "Qwen3-VL-Reranker-8B", "Qwen/Qwen3-VL-Reranker-8B" and
    ".../models--Qwen--Qwen3-VL-Reranker-8B/snapshots/abc" all collapse to
    "qwen3vlreranker8b", which is what lets the panel attribute a pod to the
    gateway's pool without a hand-maintained mapping.
    """
    text = str(raw or "")
    m = _MODEL_PATH_RE.search(text)
    if m:
        text = m.group(1).replace("--", "/")
    # Compare on the LAST path segment: a pod's model path carries the org
    # ("Qwen/Qwen3-VL-Reranker-8B") while a pool is usually named for the model
    # alone ("Qwen3-VL-Reranker-8B"). Keeping the org would make every such
    # pair miss and every row read "unmapped".
    text = text.rstrip("/").rsplit("/", 1)[-1]
    return _NON_ALNUM.sub("", text.lower())


def _series_key(metric: dict[str, Any]) -> str:
    """namespace/(pod or instance) -- the identity two queries are merged on.

    Every query groups by BOTH pod and instance for exactly this reason: a
    metric family that does not carry a pod label (SGLang's label set differs
    from vLLM's) must still merge with the families that do, instead of landing
    in a second row keyed by an empty pod name.
    """
    ident = metric.get("pod") or metric.get("instance") or ""
    return str(metric.get("namespace") or "") + "/" + str(ident)


# The MCP prometheus tool RENDERS its answer as a table, one series per line:
#
#   {model_name=/mnt/models/models--Qwen--X/snapshots/abc,namespace=ns,pod=p1} = 4.4983 (at 1793369254.388)
#
# It does NOT hand back the Prometheus JSON envelope. Verified against the live
# server on 2026-10-30: a parser that only understood JSON returned [] for
# every query, which the panel renders as "no endpoints reporting" -- a silent
# lie that looks like an idle fleet. Both shapes are accepted, because a
# different deployment (or a tool swap) may return the envelope instead.
_TABLE_ROW_RE = re.compile(r"^\s*\{(?P<labels>[^}]*)\}\s*=\s*(?P<value>\S+)")
# The rendered table separates labels with ", " (comma AND space), so the
# lookahead must allow whitespace -- otherwise the second label's key is glued
# to the first label's value ("namespace=project-user, pod=x").
_LABEL_SPLIT_RE = re.compile(r",(?=\s*[A-Za-z_][A-Za-z0-9_]*=)")


def _parse_table(text: str) -> list[tuple[dict[str, Any], float]]:
    """(labels, value) from the rendered table. PURE, total."""
    out: list[tuple[dict[str, Any], float]] = []
    for line in text.splitlines():
        m = _TABLE_ROW_RE.match(line)
        if not m:
            continue
        labels: dict[str, Any] = {}
        for pair in _LABEL_SPLIT_RE.split(m.group("labels")):
            if "=" not in pair:
                continue
            key, _, value = pair.partition("=")
            labels[key.strip()] = value.strip().strip('"')
        try:
            number = float(m.group("value"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        out.append((labels, number))
    return out


def prom_vector(result: Any) -> list[tuple[dict[str, Any], float]]:
    """(labels, value) pairs from one prometheus MCP tool result. PURE, total.

    Accepts BOTH the rendered table (what this fleet's tool actually returns)
    and the Prometheus JSON envelope. Anything unparseable or error-shaped
    yields [] -- a metrics panel that raises is worse than one that says
    "no data", and this runs in the gateway's own process.
    """
    from .offpeak import _collect_texts  # one text-extraction rule, shared

    out: list[tuple[dict[str, Any], float]] = []
    for text in _collect_texts(result):
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("status") not in (None, "success"):
                continue
            data = parsed.get("data")
            if not isinstance(data, dict):
                continue
            rows = data.get("result")
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                metric = row.get("metric")
                value = row.get("value")
                if not isinstance(metric, dict) or not isinstance(value, list) or len(value) < 2:
                    continue
                try:
                    number = float(value[1])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    out.append((metric, number))
            continue
        out.extend(_parse_table(text))
    return out


def _attribute(row: dict[str, Any], pools: dict[str, list[str]] | None) -> str:
    """The gateway pool a pod belongs to, or "" when nothing matches. PURE.

    Two joins, strongest first:
      1. the endpoint address -- 'instance' is host:port and a pool registered
         by IP matches exactly;
      2. the normalised model key -- the pod's model path vs the pool name.
    A pod that matches neither is NOT dropped: an unattributed row is the
    whole point of the panel when a pool is misnamed.
    """
    if not pools:
        return ""
    instance = str(row.get("instance") or "")
    if instance:
        host = instance.rsplit(":", 1)[0]
        for name, urls in pools.items():
            for url in urls:
                if not url:
                    continue
                if instance in url or (host and host in url):
                    return name
    key = _model_key(str(row.get("model_name") or ""))
    if key:
        for name in pools:
            if _model_key(name) == key:
                return name
        for name in pools:
            nk = _model_key(name)
            if nk and (nk in key or key in nk):
                return name
    return ""


async def snapshot(window_s: int = DEFAULT_WINDOW_S, pools: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """One read of every endpoint's throughput/load. NEVER raises.

    pools (optional) maps the gateway's pool name -> endpoint base URLs; it is
    how a pod gets attributed to a model. Callers pass the registry view; the
    module deliberately does not import the router (this file must stay
    importable from a read-only admin route).
    """
    from . import offpeak

    window = max(MIN_WINDOW_S, min(int(window_s or DEFAULT_WINDOW_S), MAX_WINDOW_S))
    empty = {
        "available": False,
        "source": f"mcp:{settings.offpeak_mcp_server}/{settings.offpeak_tool}",
        "window_s": window,
        "endpoints": [],
        "error": "",
    }
    try:
        url, headers = await offpeak.prometheus_target()
    except Exception as exc:
        return {**empty, "error": str(exc) or type(exc).__name__}

    async def one(name: str, promql: str) -> tuple[str, list[tuple[dict[str, Any], float]]]:
        try:
            result = await asyncio.wait_for(
                offpeak._call_tool(url, headers, settings.offpeak_tool, {"query": promql}, _QUERY_TIMEOUT_S),
                timeout=_QUERY_TIMEOUT_S,
            )
        except Exception as exc:
            log.warning("endpoint metrics: %s query failed (%s)", name, type(exc).__name__)
            return name, []
        return name, prom_vector(result)

    results = await asyncio.gather(
        *(one(name, q.replace("{window}", f"{window}s")) for name, q in QUERIES.items()),
        return_exceptions=False,
    )
    by_query = dict(results)

    rows: dict[str, dict[str, Any]] = {}
    for query, series in by_query.items():
        for metric, value in series:
            key = _series_key(metric)
            if key == "/":
                continue
            row = rows.setdefault(
                key,
                {
                    "namespace": str(metric.get("namespace") or ""),
                    "pod": str(metric.get("pod") or ""),
                    "instance": str(metric.get("instance") or ""),
                    "model_name": str(metric.get("model_name") or ""),
                    "pool": "",
                    "prefill_tps": None,
                    "decode_tps": None,
                    "running": None,
                    "waiting": None,
                    "kv_cache_pct": None,
                    "cache_hit_pct": None,
                    "used_tokens": None,
                    "prefill_share": None,
                    "idle": False,
                },
            )
            if metric.get("instance") and not row["instance"]:
                row["instance"] = str(metric["instance"])
            if metric.get("model_name") and not row["model_name"]:
                row["model_name"] = str(metric["model_name"])
            row[query] = value

    endpoints: list[dict[str, Any]] = []
    for row in rows.values():
        prefill_s = row.get("prefill_s") or 0.0
        decode_s = row.get("decode_s") or 0.0
        total_s = prefill_s + decode_s
        row["prefill_share"] = round(prefill_s / total_s, 3) if total_s > 0 else None
        # A pod with no traffic in the window is IDLE, not broken: rate() of a
        # flat counter is 0, and a panel that renders "0 t/s" for an idle
        # fleet looks like a fault report.
        row["idle"] = (
            not (row.get("running") or 0) and not (row.get("waiting") or 0) and (row.get("decode_tps") or 0) < 0.01
        )
        row["pool"] = _attribute(row, pools)
        endpoints.append(row)

    # Biggest talkers first: the panel exists to answer "who is busy".
    endpoints.sort(key=lambda r: (-(r.get("decode_tps") or 0), -(r.get("prefill_tps") or 0), r.get("pod") or ""))
    return {
        "available": True,
        "source": f"mcp:{settings.offpeak_mcp_server}/{settings.offpeak_tool}",
        "window_s": window,
        "endpoints": endpoints,
        "error": "",
    }
