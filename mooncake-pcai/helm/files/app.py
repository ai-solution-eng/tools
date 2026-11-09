#!/usr/bin/env python3
"""Mooncake KV-store dashboard (read-only).

Data sources, all plain HTTP from inside the cluster:
  * Mooncake master  :9003  -> /metrics/summary (pool counters), /get_all_segments
  * Kubernetes API          -> pods that carry a MOONCAKE_MASTER env var
  * each such pod   :8080   -> SGLang /metrics (per-replica cache state)

Refresh model: a background thread re-reads every REFRESH_INTERVAL_SECONDS
(default 1800); the HTTP handlers serve the cached snapshot instantly, and
POST /api/refresh forces an immediate re-read.

Stdlib only - no pip installs, no custom image, no writable filesystem.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


def _env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url, timeout, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _sa_token():
    with open(os.path.join(SA_DIR, "token"), "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _sa_namespace():
    try:
        with open(os.path.join(SA_DIR, "namespace"), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "default"


def k8s_get_json(path, timeout):
    ctx = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
    req = urllib.request.Request(
        "https://kubernetes.default.svc" + path,
        headers={"Authorization": "Bearer " + _sa_token(), "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


_SIZE_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12,
               "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}

_STORE_RE = re.compile(
    r"Storage:\s*([0-9.]+)\s*([KMGTP]?i?B)\s*/\s*([0-9.]+)\s*([KMGTP]?i?B)\s*\(([0-9.]+)%\)",
    re.IGNORECASE,
)


def _size(value, unit):
    return int(float(value) * _SIZE_UNITS.get(unit.upper(), 1))


_QUANTITY_UNITS = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15,
                   "KI": 1024, "MI": 1024**2, "GI": 1024**3, "TI": 1024**4, "PI": 1024**5}


def _quantity(text):
    """Parse a Kubernetes resource quantity such as "350Gi" into bytes."""
    match = re.match(r"^([0-9.]+)s*([KMGTP]i?)?$", str(text or "").strip())
    if not match:
        return 0
    return int(float(match.group(1)) * _QUANTITY_UNITS.get((match.group(2) or "").upper(), 1))


_STORE_PVC_RE = re.compile(r"^ssd-.*-store-[0-9]+$")


def store_disk_capacity(timeout):
    """Sum the store PVC claims.

    The master advertises a disk capacity that is not the PVC quota (it reports
    6.00 TB against three 350Gi claims), so the claims are the only honest
    denominator for the capacity tier. Returns None when RBAC forbids it - the
    caller falls back to the master's figure and says so.
    """
    namespace = _sa_namespace()
    payload = k8s_get_json(
        "/api/v1/namespaces/%s/persistentvolumeclaims" % namespace, timeout)
    total = 0
    claims = 0
    for item in payload.get("items", []):
        meta = item.get("metadata") or {}
        if not _STORE_PVC_RE.search(meta.get("name") or ""):
            continue
        spec = (item.get("spec") or {}).get("resources") or {}
        capacity = ((item.get("status") or {}).get("capacity") or {}).get("storage") \
            or (spec.get("requests") or {}).get("storage")
        total += _quantity(capacity)
        claims += 1
    return (total, claims) if total else None


def parse_store_summary(text):
    out = {
        "raw_summary": text,
        "capacity_bytes": None,
        "used_bytes": None,
        "used_pct": None,
        "keys": None,
        "clients": None,
        "evictions": None,
        "alloc_failures": None,
        "ssd_bytes": None,
    }
    match = _STORE_RE.search(text)
    if match:
        out["used_bytes"] = _size(match.group(1), match.group(2))
        out["capacity_bytes"] = _size(match.group(3), match.group(4))
        out["used_pct"] = float(match.group(5))
    # Must anchor on "SSD Storage": the line also carries "NVMe-oF SSD: 0 B",
    # which the looser pattern matched first, so the tier always read 0 B.
    ssd = re.search(r"SSD Storage:\s*([0-9.]+)\s*([KMGTP]?i?B)\s*/\s*([0-9.]+)\s*([KMGTP]?i?B)",
                    text, re.IGNORECASE)
    if ssd:
        out["ssd_bytes"] = _size(ssd.group(1), ssd.group(2))
        out["ssd_reported_capacity_bytes"] = _size(ssd.group(3), ssd.group(4))
    for field, pattern in (
        ("keys", r"Keys:\s*([0-9]+)"),
        ("clients", r"Clients:\s*([0-9]+)"),
    ):
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            out[field] = int(found.group(1))
    # 0.3.x prints "Eviction: Success/Attempts=<ok>/<total>" and "AllocFail=<n>";
    # the older "Eviction: <n>" / "AllocFail: <n>" form is kept as a fallback.
    for field, patterns in (
        ("evictions", (r"Eviction:\s*Success/Attempts=([0-9]+)",
                       r"Eviction[^:\n]*:\s*([0-9]+)")),
        ("alloc_failures", (r"AllocFail=([0-9]+)",
                            r"AllocFail[^:\n]*:\s*([0-9]+)")),
    ):
        for pattern in patterns:
            found = re.search(pattern, text, re.IGNORECASE)
            if found:
                out[field] = int(found.group(1))
                break
    return out


def _json_chunks(text):
    """Parse one or more concatenated JSON values.

    The master answers /get_all_segments in whatever shape it likes: one
    document, one object per line, or several documents back to back. A plain
    json.loads() fails on the last two with "Extra data".
    """
    out = []
    decoder = json.JSONDecoder()
    idx, size = 0, len(text)
    while idx < size:
        while idx < size and text[idx] in " \t\r\n,":
            idx += 1
        if idx >= size:
            break
        try:
            value, end = decoder.raw_decode(text, idx)
        except ValueError:
            break
        out.append(value)
        idx = end
    return out


def parse_segments(payload):
    """Best-effort: the segment payload shape is not part of the public API.

    0.3.x answers /get_all_segments as *plain text*, one "host:port" per line -
    not JSON at all. That fell through both parsers and silently produced an
    empty list, so the Segments tile always read 0.
    """
    chunks = []
    if isinstance(payload, (list, dict)):
        chunks = [payload]
    else:
        text = (payload or "").strip()
        if text:
            try:
                chunks = [json.loads(text)]
            except ValueError:
                chunks = _json_chunks(text)
                # A "host:port" line parses as a JSON *number* prefix (10.224),
                # so a truthiness check is not enough - require a real object.
                if not any(isinstance(c, (dict, list)) for c in chunks):
                    lines = [line.strip() for line in text.splitlines() if line.strip()]
                    if lines:
                        return [{"id": None, "host": line, "size_bytes": None}
                                for line in lines]
    items = []
    for chunk in chunks:
        if isinstance(chunk, list):
            items.extend(chunk)
        elif isinstance(chunk, dict):
            found = None
            for key in ("segments", "data", "items", "result", "segment_list"):
                if isinstance(chunk.get(key), list):
                    found = chunk[key]
                    break
            if found is not None:
                # "segments": {id: {...}} is as likely as a list.
                items.extend(found.values() if isinstance(found, dict) else found)
            elif any(isinstance(v, dict) for v in chunk.values()):
                items.extend(v for v in chunk.values() if isinstance(v, dict))
            else:
                items.append(chunk)
    out = []
    for item in items:
        if not isinstance(item, dict):
            out.append({"id": None, "host": None, "size_bytes": None})
            continue
        size = None
        for key in ("size", "size_bytes", "capacity", "total_size", "segment_size"):
            if isinstance(item.get(key), (int, float)):
                size = int(item[key])
                break
        out.append({
            "id": item.get("id") or item.get("segment_id") or item.get("name"),
            "host": item.get("host") or item.get("hostname") or item.get("address"),
            "size_bytes": size,
        })
    return out


def parse_prometheus(text):
    metrics = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            name, value = line.rsplit(" ", 1)
        except ValueError:
            continue
        try:
            metrics[name.split("{", 1)[0]] = float(value)
        except ValueError:
            continue
    return metrics


def parse_prometheus_labeled(text):
    """{metric_name: [({label: value}, sample), ...]}

    parse_prometheus() strips labels, so every mode of a labelled counter
    collapses onto one key and only the last line survives. The HiCache tier
    breakdown needs the labels, hence this second view.
    """
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            head, raw = line.rsplit(" ", 1)
            sample = float(raw)
        except ValueError:
            continue
        labels = {}
        if "{" in head:
            name, rest = head.split("{", 1)
            for part in rest.rstrip("}").split(","):
                if "=" in part:
                    key, value = part.split("=", 1)
                    labels[key.strip()] = value.strip().strip('"')
        else:
            name = head
        out.setdefault(name, []).append((labels, sample))
    return out


SGLANG_KEYS = (
    "sglang:cache_hit_rate",
    "sglang:token_usage",
    "sglang:kv_used_tokens",
    "sglang:kv_evictable_tokens",
    "sglang:max_total_num_tokens",
    "sglang:num_requests_total",
)


_STORE_POD_RE = re.compile(r"-store-\d+$")


def _is_store_pod(meta, exclude_re):
    """The store pods are Mooncake clients too, but they are not models.

    They carry MOONCAKE_MASTER like a serving replica does, so without this
    they show up in the Models table as a permanent 404 (they serve the debug
    HTTP API, not SGLang /metrics).
    """
    labels = meta.get("labels") or {}
    app_label = labels.get("app") or ""
    name = meta.get("name") or ""
    if app_label.endswith("-store"):
        return True
    if _STORE_POD_RE.search(name):
        return True
    if exclude_re is not None and exclude_re.search(name):
        return True
    return False


def discover_clients(namespaces, timeout, exclude_re=None):
    """Return (dict of model name to pods, list of errors)."""
    groups = {}
    errors = []
    for namespace in namespaces:
        try:
            payload = k8s_get_json("/api/v1/namespaces/%s/pods" % namespace, timeout)
        except Exception as exc:
            errors.append("pods/%s: %s" % (namespace, exc))
            continue
        for pod in payload.get("items", []):
            spec = pod.get("spec") or {}
            names = []
            for container in spec.get("containers") or []:
                for item in container.get("env") or []:
                    names.append(item.get("name"))
            if "MOONCAKE_MASTER" not in names:
                continue
            meta = pod.get("metadata") or {}
            if _is_store_pod(meta, exclude_re):
                continue
            label = (meta.get("labels") or {}).get("serving.kserve.io/inferenceservice")
            name = label or (meta.get("name") or "").split("-predictor")[0] or "unknown"
            groups.setdefault(name, []).append(pod)
    return groups, errors


def _replica_from_metrics(pod_label, node, metrics_text):
    entry = {
        "pod": pod_label,
        "node": node,
        "reachable": True,
        "error": None,
        "metrics": {},
    }
    metrics = parse_prometheus(metrics_text)
    entry["metrics"] = {k: v for k, v in metrics.items() if k.startswith("sglang:")}
    for key in SGLANG_KEYS:
        entry[key.split(":", 1)[1]] = entry["metrics"].get(key)
    # The table reads kv_pool_tokens; the metric is max_total_num_tokens.
    # Publish it under both names so the two stay in step.
    entry["kv_pool_tokens"] = entry["metrics"].get("sglang:max_total_num_tokens")

    # HiCache tier breakdown: where the prompt tokens were actually served from.
    # This is what tells you whether the Mooncake tier is earning its keep -
    # cache_hit_rate alone is dominated by L1 and hides an idle L3.
    labeled = parse_prometheus_labeled(metrics_text)

    def _sum(name, **want):
        total = 0.0
        for labels, sample in labeled.get(name, []):
            if all(labels.get(k) == v for k, v in want.items()):
                total += sample
        return total

    def _pct(part, whole):
        return (part / whole) if whole else None

    def _max(name, **want):
        # Pool *sizes* are per-rank, not additive: summing them across a TP group
        # would invent capacity that no single rank has.
        vals = [sample for labels, sample in labeled.get(name, [])
                if all(labels.get(k) == v for k, v in want.items())]
        return max(vals) if vals else None

    input_tokens = _sum("sglang:prefill_effective_tokens_total", mode="input")
    l1 = _sum("sglang:prefill_effective_tokens_total", mode="device_hit")
    l2 = _sum("sglang:prefill_effective_tokens_total", mode="host_hit")
    l3 = _sum("sglang:prefill_effective_tokens_total", mode="storage_hit")
    entry["input_tokens"] = input_tokens or None
    entry["l1_tokens"], entry["l2_tokens"], entry["l3_tokens"] = l1, l2, l3
    entry["l1_pct"] = _pct(l1, input_tokens)
    entry["l2_pct"] = _pct(l2, input_tokens)
    entry["l3_pct"] = _pct(l3, input_tokens)
    entry["l3_write_tokens"] = _sum("sglang:backuped_tokens_total",
                                    storage_backend="mooncake")
    # The HiCache host pool (L2) - the tier that sits between GPU KV and the
    # shared store. Showing its size next to its hit rate is what makes an
    # over-provisioned ratio visible.
    entry["host_pool_tokens"] = _max("sglang:hicache_host_total_tokens")
    entry["host_used_tokens"] = _max("sglang:hicache_host_used_tokens")
    # Cumulative tokens the GPU cache has served. Pairs with the instantaneous
    # occupancy so an idle 0% still shows the cache is doing work over time.
    entry["cached_tokens"] = _sum("sglang:cached_tokens_total", cache_source="device")
    # kv_used_tokens counts only tokens held by RUNNING requests - it is 0
    # between requests. Residency is kv_evictable_tokens (prefixes the radix
    # cache keeps) plus those in-flight tokens.
    entry["kv_evictable_tokens"] = _max("sglang:kv_evictable_tokens")
    return entry


def scrape_replica(pod, port, timeout):
    meta = pod.get("metadata") or {}
    status = pod.get("status") or {}
    spec = pod.get("spec") or {}
    pod_name = meta.get("name")
    node = spec.get("nodeName")
    ip = status.get("podIP")
    if not ip:
        entry = {"pod": pod_name, "node": node, "reachable": False,
                 "error": "no podIP (pending or terminating)", "metrics": {}}
        for key in SGLANG_KEYS:
            entry[key.split(":", 1)[1]] = None
        return entry
    try:
        text = http_get("http://%s:%d/metrics" % (ip, int(port)), timeout)
    except Exception as exc:
        entry = {"pod": pod_name, "node": node, "reachable": False,
                 "error": str(exc), "metrics": {}}
        for key in SGLANG_KEYS:
            entry[key.split(":", 1)[1]] = None
        return entry
    return _replica_from_metrics(pod_name, node, text)


def scrape_endpoint(endpoint, timeout):
    """STATIC_MODELS mode: scrape a URL directly, no Kubernetes API."""
    try:
        text = http_get(endpoint.rstrip("/") + "/metrics", timeout)
    except Exception as exc:
        entry = {"pod": endpoint, "node": None, "reachable": False,
                 "error": str(exc), "metrics": {}}
        for key in SGLANG_KEYS:
            entry[key.split(":", 1)[1]] = None
        return entry
    return _replica_from_metrics(endpoint, None, text)


_LOCK = threading.Lock()
_SNAPSHOT = {"data": None, "at": 0.0}


def refresh(config):
    timeout = config["timeout"]
    errors = []
    master = config["master_url"].rstrip("/")

    store = parse_store_summary("")
    store["reachable"] = False
    try:
        store = parse_store_summary(http_get(master + "/metrics/summary", timeout))
        store["reachable"] = True
    except Exception as exc:
        errors.append("store summary: %s" % exc)

    try:
        raw = http_get(master + "/get_all_segments", timeout)
        store["segments_detail"] = parse_segments(raw)
    except Exception as exc:
        store["segments_detail"] = []
        errors.append("segments: %s" % exc)

    if store.get("clients") is None and store.get("segments_detail"):
        store["clients"] = len(store["segments_detail"])
    store["segments"] = len(store.get("segments_detail") or [])

    # Disk denominator: the PVC claims, not the master's own figure.
    try:
        claims = store_disk_capacity(timeout)
    except Exception as exc:
        claims = None
        errors.append("pvc capacity: %s" % exc)
    if claims:
        store["disk_capacity_bytes"], store["disk_claims"] = claims
        store["disk_capacity_source"] = "pvc"
    else:
        store["disk_capacity_bytes"] = store.get("ssd_reported_capacity_bytes")
        store["disk_claims"] = 0
        store["disk_capacity_source"] = "master" if store["disk_capacity_bytes"] else None

    models = []
    if config["static_models"]:
        for item in config["static_models"]:
            name = item.get("name") or item.get("endpoint")
            replica = scrape_endpoint(item.get("endpoint", ""), timeout)
            models.append({"name": name, "namespace": None, "replica_count": 1, "replicas": [replica]})
    else:
        groups, discovery_errors = discover_clients(config["namespaces"], timeout, config.get("exclude_re"))
        errors.extend(discovery_errors)
        for name in sorted(groups):
            pods = groups[name]
            replicas = [scrape_replica(pod, config["metrics_port"], timeout) for pod in pods]
            namespace = ((pods[0].get("metadata") or {}).get("namespace")) if pods else None
            models.append({
                "name": name,
                "namespace": namespace,
                "replica_count": len(replicas),
                "replicas": replicas,
            })

    now = time.time()
    data = {
        "generated_at": _utcnow(),
        "age_seconds": 0,
        "refresh_interval_seconds": config["interval"],
        "next_refresh_in_seconds": config["interval"],
        "store": store,
        "models": models,
        "errors": errors,
    }
    with _LOCK:
        _SNAPSHOT["data"] = data
        _SNAPSHOT["at"] = now
    return data


def _empty_snapshot(config):
    return {
        "generated_at": _utcnow(),
        "age_seconds": 0,
        "refresh_interval_seconds": config["interval"],
        "next_refresh_in_seconds": 0,
        "loading": True,
        "store": {
            "reachable": False, "raw_summary": "",
            "capacity_bytes": None, "used_bytes": None, "used_pct": None,
            "keys": None, "clients": None, "segments": None,
            "evictions": None, "alloc_failures": None, "ssd_bytes": None,
            "disk_capacity_bytes": None, "disk_claims": 0, "disk_capacity_source": None,
            "segments_detail": [],
        },
        "models": [],
        "errors": [],
    }


def snapshot(config):
    with _LOCK:
        data = _SNAPSHOT["data"]
        at = _SNAPSHOT["at"]
    if data is None:
        # Never block a request on the first read. The background thread fills
        # this within a few seconds and the page polls again.
        return _empty_snapshot(config)
    age = max(0, int(time.time() - at))
    data["age_seconds"] = age
    data["next_refresh_in_seconds"] = max(0, config["interval"] - age)
    data["loading"] = False
    return data


def refresher(config, stop_event):
    while not stop_event.is_set():
        had_errors = True
        try:
            data = refresh(config)
            had_errors = bool(data.get("errors"))
        except Exception:
            pass
        # Retry quickly after a failed read instead of waiting a full interval:
        # a rollout restarts the UI and the master together, and the first read
        # usually races the Service endpoints.
        stop_event.wait(15 if had_errors else config["interval"])


PAGE = """<!doctype html>
<html lang="en" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  color-scheme: light;
  --green:#01a982; --green-strong:#007a5e; --green-soft:#e6f6f1;
  --bg:#f5f5f7; --panel:#ffffff; --ink:#1a1a1a; --muted:#6b7280; --line:#e4e4e8;
  --bar-bg:#ececf0; --danger:#c0392b; --danger-soft:#fdecea; --shadow:0 1px 2px rgba(0,0,0,.06);
  --active:#d97706;
}
@media (prefers-color-scheme: dark){
  :root[data-theme="auto"]{
    color-scheme: dark;
    --green:#2ed3a8; --green-strong:#4be3b8; --green-soft:#102b25;
    --bg:#0e0e10; --panel:#18181b; --ink:#f2f2f2; --muted:#9aa0a6; --line:#2b2b30;
    --bar-bg:#2a2a30; --danger:#ff9a90; --danger-soft:#3a1d1a; --shadow:none;
    --active:#f0a55c;
  }
}
:root[data-theme="dark"]{
  color-scheme: dark;
  --green:#2ed3a8; --green-strong:#4be3b8; --green-soft:#102b25;
  --bg:#0e0e10; --panel:#18181b; --ink:#f2f2f2; --muted:#9aa0a6; --line:#2b2b30;
  --bar-bg:#2a2a30; --danger:#ff9a90; --danger-soft:#3a1d1a; --shadow:none;
  --active:#f0a55c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
header{display:flex;align-items:center;gap:18px;padding:9px 24px;min-height:64px;background:var(--panel);border-bottom:1px solid var(--line);border-top:3px solid var(--green)}
h1{font-size:16px;font-weight:600;margin:0;letter-spacing:.01em}
h1 span{color:var(--muted);font-weight:400;margin-left:10px;font-size:13px}
.brand{display:flex;align-items:center;gap:14px;min-width:0}
.hpe-logo{height:34px;width:auto;flex-shrink:0;display:block}
.brandtext{display:flex;flex-direction:column;line-height:1.15;min-width:0}
.brand-name{font-size:9.5px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.16em}
.brand-title{font-size:15px;font-weight:600;color:var(--ink);margin-top:1px}
.brand-sub{font-size:12px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.spacer{flex:1}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{background:transparent;color:var(--muted);border:0;padding:6px 11px;font-size:12.5px;cursor:pointer}
.seg button.on{background:var(--green-soft);color:var(--green-strong);font-weight:600}
.btn{background:var(--green);color:#fff;border:0;border-radius:8px;padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}
.btn:disabled{opacity:.55;cursor:default}
main{max-width:1180px;margin:0 auto;padding:22px 24px 40px;display:grid;gap:18px}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:18px 20px}
h2{font-size:11.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);margin:0 0 16px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:16px}
.stat .k{color:var(--muted);font-size:12px;margin-bottom:4px}
.stat .v{font-size:22px;font-weight:600;letter-spacing:-.01em}
.stat .v small{font-size:13px;font-weight:400;color:var(--muted);margin-left:4px}
.tierblock{margin-top:20px}
.tierhead{display:flex;justify-content:space-between;align-items:baseline;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.tierhead em{font-style:normal;text-transform:none;letter-spacing:0;margin-left:7px;opacity:.8}
.tiernum{font-size:12px;color:var(--ink);letter-spacing:0;text-transform:none;font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--bar-bg);border-radius:3px;overflow:hidden;margin-top:7px}
.bar>i{display:block;height:100%;background:var(--green);width:0}
table{width:100%;border-collapse:collapse;font-size:13px}
th{ text-align:left;color:var(--muted);font-weight:500;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;padding:0 12px 10px 0;border-bottom:1px solid var(--line)}
td{padding:11px 12px 11px 0;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:0}
td.mono{font-variant-numeric:tabular-nums}
td.pod{max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.legend{color:var(--muted);font-size:12px;margin:-6px 0 16px}
.mcard{border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px}
.mcard:last-child{margin-bottom:0}
.mhead{display:flex;align-items:baseline;gap:10px}
.mhead strong{font-size:15px}
.mcount{color:var(--muted);font-size:12px}
.replica{margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}
.replica:first-of-type{margin-top:10px;padding-top:0;border-top:0}
.repline{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.podname{color:var(--muted);font-size:12px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nodename{color:var(--muted);font-size:12px}
.tier{display:grid;grid-template-columns:130px minmax(60px,1fr) 230px 88px;align-items:center;gap:16px;margin:9px 0}
.tlabel{font-size:12.5px;color:var(--ink)}
.tlabel span{display:block;font-size:10px;color:var(--muted);letter-spacing:.04em;text-transform:uppercase;margin-top:1px}
.tbar{height:6px;background:var(--bar-bg);border-radius:3px;overflow:hidden;display:flex}
.tbar>i{display:block;height:100%;background:var(--green);flex:0 0 auto}
.tbar>i.active{background:var(--active)}
.tlabel b.act{color:var(--active);font-weight:600}
.tval{font-size:12px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
.ttraffic{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.thit{font-size:12px;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
.thit.dim{color:var(--muted);font-weight:400}
.src{margin-top:14px;font-size:12px;color:var(--muted)}
.src b{color:var(--ink);font-weight:600}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600;background:var(--green-soft);color:var(--green-strong)}
.chip.bad{background:var(--danger-soft);color:var(--danger)}
.empty{color:var(--muted);font-size:13px;padding:6px 0}
.note{margin-top:14px;color:var(--muted);font-size:12px}
#errors{display:none;border-color:var(--danger)}
#errors h2{color:var(--danger)}
#errlist{color:var(--danger);font-size:12.5px;word-break:break-word}
</style>
</head>
<body>
<header>
  <div class="brand">
    <svg class="hpe-logo" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 432 288">
      <path fill="#01a982" d="M126.617,109.327v15.861h55.218v-15.861H126.617z M178.384,121.737h-48.316v-8.959h48.316V121.737z"/>
      <path fill="var(--ink)" d="M136.668,158.173v1.93h-7.843v4.849h7.132v1.878h-7.132v5.279h7.843v1.929h-10.051v-15.864H136.668z
        M142.123,174.037h-2.157v-11.676h2.03v2.184c0.837-1.473,2.183-2.438,3.909-2.438c2.716,0,4.315,1.776,4.315,4.442v7.487h-2.158
        V166.6c0-1.547-0.812-2.462-2.614-2.462c-1.548,0-2.868,1.041-3.325,2.438V174.037z M159.051,174.291c-2.919,0-4.086-1.271-4.086-3.579
        v-6.446h-2.462v-1.904h2.462v-3.02h2.157v3.02h3.325v1.904h-3.325v6.244c0,1.396,0.787,1.775,2.133,1.775c0.354,0,0.71-0.049,1.015-0.152
        h0.177v1.904C160.117,174.163,159.659,174.291,159.051,174.291z M168.541,174.291c-3.883,0-6.345-2.284-6.345-5.965
        c0-3.655,2.285-6.219,5.635-6.219c3.451,0,5.203,2.312,5.203,5.737v1.143h-8.604c0.305,2.282,1.929,3.35,4.137,3.35
        c1.371,0,2.412-0.229,3.427-0.964h0.228v1.853C171.182,174.012,169.836,174.291,168.541,174.291z M164.456,167.285h6.472
        c-0.077-2.131-1.193-3.273-3.071-3.273S164.735,165.383,164.456,167.285z M182.474,164.569c-0.305-0.15-0.762-0.254-1.243-0.254
        c-1.372,0-2.463,0.967-2.919,2.538v7.184h-2.157v-11.676h2.03v2.514c0.686-1.904,1.929-2.691,3.325-2.691
        c0.507,0,0.964,0.077,1.142,0.178v2.208H182.474z M186.965,172.133v6.193h-2.157v-15.965h2.03v2.006
        c0.686-1.194,2.03-2.26,3.832-2.26c3.528,0,5.381,2.792,5.381,5.965c0,3.249-1.854,6.091-5.381,6.091
        C188.996,174.163,187.676,173.25,186.965,172.133z M193.843,168.072c0-2.36-1.319-3.935-3.68-3.935c-1.395,0-2.512,0.737-3.197,2.006
        v4.011c0.686,1.243,1.802,2.005,3.197,2.005C192.523,172.159,193.843,170.534,193.843,168.072z M205.619,164.569
        c-0.305-0.15-0.762-0.254-1.244-0.254c-1.371,0-2.462,0.967-2.918,2.538v7.184h-2.157v-11.676h2.03v2.514
        c0.685-1.904,1.929-2.691,3.324-2.691c0.508,0,0.965,0.077,1.143,0.178v2.208H205.619z M209.297,161.194
        c-0.813,0-1.548-0.686-1.548-1.523c0-0.837,0.735-1.498,1.548-1.498c0.837,0,1.548,0.661,1.548,1.498
        C210.845,160.509,210.135,161.194,209.297,161.194z M210.363,174.037h-2.157v-11.676h2.157V174.037z M213.865,171.27h0.204
        c0.964,0.787,2.386,1.193,3.73,1.193c1.269,0,2.386-0.481,2.386-1.521c0-1.041-0.939-1.271-2.791-1.878
        c-1.65-0.56-3.504-1.144-3.504-3.352c0-2.31,1.878-3.604,4.291-3.604c1.37,0,2.589,0.279,3.603,0.965v1.98h-0.228
        c-0.914-0.686-2.005-1.117-3.351-1.117c-1.37,0-2.132,0.609-2.132,1.473c0,0.964,0.812,1.219,2.716,1.801
        c1.699,0.56,3.579,1.219,3.579,3.427c0,2.412-1.981,3.655-4.543,3.655c-1.549,0-3.097-0.38-3.96-1.065V171.27z
        M231.147,174.291c-3.883,0-6.345-2.284-6.345-5.965c0-3.655,2.284-6.219,5.635-6.219c3.453,0,5.204,2.312,5.204,5.737v1.143h-8.605
        c0.305,2.282,1.929,3.35,4.139,3.35c1.369,0,2.41-0.229,3.426-0.964h0.228v1.853C233.787,174.012,232.442,174.291,231.147,174.291z
        M227.062,167.285h6.473c-0.076-2.131-1.193-3.273-3.071-3.273C228.585,164.012,227.341,165.383,227.062,167.285z M139.942,135.543
        v15.863h-3.452v-6.574h-6.421v6.574h-3.451v-15.863h3.451v6.269h6.421v-6.269H139.942z M149.306,151.686c-4.366,0-6.701-2.361-6.701-6.168
        c0-3.807,2.436-6.32,5.964-6.32c3.807,0,5.711,2.386,5.711,5.965v1.344h-8.122c0.253,1.956,1.802,2.462,3.324,2.462
        c1.372,0,2.437-0.177,3.555-0.784h0.38v2.486C152.402,151.406,150.778,151.686,149.306,151.686z M146.133,144.325h4.747
        c-0.102-1.777-0.863-2.564-2.259-2.564C147.428,141.761,146.361,142.32,146.133,144.325z M158.339,151.406l-2.488-11.522v-0.406h3.198
        l1.346,7.259l0.33,2.334l0.457-2.334l1.624-7.259h3.63l1.6,7.259l0.482,2.334l0.381-2.334l1.345-7.259h3.046v0.406l-2.513,11.522h-4.417
        l-1.421-6.37l-0.406-2.386l-0.406,2.386l-1.396,6.37H158.339z M179.378,151.686c-2.538,0-3.807-1.167-3.807-3.884v-12.258h3.452v12.03
        c0,0.838,0.355,1.117,1.167,1.117c0.254,0,0.661-0.076,0.914-0.151h0.051v2.867C180.774,151.534,180.038,151.686,179.378,151.686z
        M188.87,151.686c-4.366,0-6.701-2.361-6.701-6.168c0-3.807,2.437-6.32,5.965-6.32c3.808,0,5.71,2.386,5.71,5.965v1.344h-8.122
        c0.254,1.956,1.802,2.462,3.325,2.462c1.37,0,2.437-0.177,3.553-0.784h0.381v2.486C191.965,151.406,190.341,151.686,188.87,151.686z
        M185.696,144.325h4.746c-0.101-1.777-0.862-2.564-2.258-2.564C186.991,141.761,185.925,142.32,185.696,144.325z M205.542,147.066
        v-4.747h-5.051v4.594c0,1.219,0.406,1.776,1.549,1.776c0.33,0,0.761-0.102,0.989-0.202h0.331v2.842
        c-0.458,0.152-1.143,0.355-2.158,0.355c-2.918,0-4.162-1.396-4.162-4.619v-4.747h-2.056v-2.842h2.056v-2.894h3.452v2.894h5.051
        v-2.894h3.452v2.894h2.868v2.842h-2.868v4.594c0,1.219,0.406,1.776,1.548,1.776c0.33,0,0.762-0.102,0.99-0.202h0.33v2.842
        c-0.457,0.152-1.142,0.355-2.157,0.355C206.787,151.686,205.542,150.289,205.542,147.066z M222.011,146.507v4.899h-3.425v-15.863h6.167
        c3.503,0,5.812,1.929,5.812,5.507c0,3.527-2.31,5.456-5.812,5.456H222.011z M224.475,138.462h-2.464v5.127h2.464
        c1.749,0,2.588-1.041,2.588-2.538C227.062,139.502,226.224,138.462,224.475,138.462z M239.345,149.63
        c-0.71,1.32-2.005,1.904-3.604,1.904c-2.208,0-4.036-1.271-4.036-3.631c0-2.359,1.599-3.73,4.62-3.73c0.989,0,1.979,0.101,2.919,0.279
        v-0.686c0-1.269-0.889-1.827-2.666-1.827c-1.32,0-2.411,0.279-3.324,0.735h-0.382v-2.589c0.915-0.481,2.539-0.888,4.112-0.888
        c3.732,0,5.661,1.701,5.661,4.645v7.563h-3.301V149.63z M239.244,147.676v-1.523c-0.585-0.177-1.345-0.228-1.956-0.228
        c-1.395,0-2.106,0.533-2.106,1.623c0,1.091,0.712,1.522,1.751,1.522C237.976,149.07,238.913,148.564,239.244,147.676z
        M245.18,145.468c0-3.858,2.716-6.27,6.246-6.27c1.445,0,2.562,0.331,3.323,0.838v2.995h-0.355c-0.735-0.558-1.547-0.812-2.588-0.812
        c-1.702,0-3.071,1.192-3.071,3.249c0,2.03,1.369,3.197,3.071,3.197c1.041,0,1.853-0.254,2.588-0.812h0.355v2.995
        c-0.761,0.506-1.878,0.837-3.323,0.837C247.896,151.686,245.18,149.3,245.18,145.468z M260.864,145.619v5.787h-3.451v-15.863h3.451
        v9.035l0.812-1.217l3.173-3.883h3.655v0.406l-4.416,5l4.416,6.115v0.407h-3.883l-2.945-4.366L260.864,145.619z M276.953,149.63
        c-0.71,1.32-2.005,1.904-3.604,1.904c-2.208,0-4.036-1.271-4.036-3.631c0-2.359,1.599-3.73,4.62-3.73c0.989,0,1.979,0.101,2.919,0.279
        v-0.686c0-1.269-0.889-1.827-2.665-1.827c-1.319,0-2.412,0.279-3.324,0.735h-0.381v-2.589c0.913-0.481,2.537-0.888,4.11-0.888
        c3.732,0,5.661,1.701,5.661,4.645v7.563h-3.301V149.63z M276.853,147.676v-1.523c-0.585-0.177-1.345-0.228-1.954-0.228
        c-1.396,0-2.106,0.533-2.106,1.623c0,1.091,0.71,1.522,1.751,1.522S276.521,148.564,276.853,147.676z M286.698,141.889
        c0.636-1.777,1.98-2.539,3.35-2.539c0.459,0,0.839,0.127,0.991,0.203v3.274h-0.355c-0.354-0.177-0.71-0.28-1.193-0.28
        c-1.371,0-2.36,0.635-2.64,1.98v6.878h-3.451v-11.929h3.299V141.889z M301.013,149.731c-0.787,1.243-2.032,1.954-3.579,1.954
        c-3.249,0-5.254-2.615-5.254-6.218c0-3.63,2.005-6.27,5.254-6.27c1.446,0,2.64,0.635,3.427,1.726v-5.38h3.451v15.863h-3.299V149.731z
        M300.86,147.523v-4.163c-0.635-0.914-1.498-1.346-2.487-1.346c-1.573,0-2.639,1.218-2.639,3.453c0,2.208,1.065,3.4,2.639,3.4
        C299.362,148.868,300.226,148.437,300.86,147.523z"/>
    </svg>
    <div class="brandtext">
      <div class="brand-name">Hewlett Packard Enterprise</div>
      <div class="brand-title">__TITLE__</div>
      <div class="brand-sub" id="meta">loading</div>
    </div>
  </div>
  <div class="spacer"></div>
  <div class="seg" role="group" aria-label="Theme">
    <button data-theme-btn="auto">Auto</button>
    <button data-theme-btn="light">Light</button>
    <button data-theme-btn="dark">Dark</button>
  </div>
  <button class="btn" id="refresh">Refresh</button>
</header>
<main>
  <section>
    <h2>Store</h2>
    <div class="grid" id="store"></div>
    <div class="tierblock">
      <div class="tierhead"><span>RAM<em>hot tier</em></span><span class="tiernum" id="ramnum"></span></div>
      <div class="bar"><i id="bar"></i></div>
    </div>
    <div class="tierblock">
      <div class="tierhead"><span>Disk<em>capacity tier</em></span><span class="tiernum" id="disknum"></span></div>
      <div class="bar"><i id="bar2"></i></div>
    </div>
    <div class="note" id="storenote"></div>
  </section>
  <section>
    <h2>Models</h2>
    <p class="legend"><b>L1</b> (GPU memory) and <b>L2</b> (pod RAM) are fixed-size pools, so they show occupancy. <b>L3</b> is the shared Mooncake store, which has no per-replica capacity — it shows traffic: what was written, and how much was ever read back.</p>
    <div id="models" class="empty">Collecting the first sample...</div>
  </section>
  <section id="errors">
    <h2>Warnings</h2>
    <div id="errlist"></div>
  </section>
</main>
<script>
var THEME_KEY = "mooncake-theme";
function applyTheme(t){
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
  var b = document.querySelectorAll("[data-theme-btn]");
  for (var i = 0; i < b.length; i++) {
    if (b[i].getAttribute("data-theme-btn") === t) { b[i].classList.add("on"); } else { b[i].classList.remove("on"); }
  }
}
var saved = "auto";
try { saved = localStorage.getItem(THEME_KEY) || "auto"; } catch (e) {}
applyTheme(saved);
var seg = document.querySelectorAll("[data-theme-btn]");
for (var j = 0; j < seg.length; j++) {
  seg[j].onclick = function(){ applyTheme(this.getAttribute("data-theme-btn")); };
}
function esc(s){ if (s === null || s === undefined) return "-"; return String(s).replace(/[&<>]/g, function(c){ return c === "&" ? "&amp;" : (c === "<" ? "&lt;" : "&gt;"); }); }
function bytes(n){
  if (n === null || n === undefined) return "-";
  var u = ["B","KB","MB","GB","TB","PB"], i = 0, v = n;
  while (v >= 1000 && i < u.length - 1) { v = v / 1000; i++; }
  return (i === 0 ? v : v.toFixed(2)) + " " + u[i];
}
function num(n){ if (n === null || n === undefined) return "-"; return Number(n).toLocaleString(); }
function pct(n){ if (n === null || n === undefined) return "-"; return (Number(n) * 100).toFixed(1) + "%"; }
function tile(k, v, unit){
  return '<div class="stat"><div class="k">' + esc(k) + '</div><div class="v">' + v + (unit ? '<small>' + esc(unit) + '</small>' : "") + '</div></div>';
}
function tierRow(label, sub, used, total, kind, hit, opts){
  opts = opts || {};
  var frac = (used !== null && used !== undefined && total) ? Math.min(1, used / total) : 0;
  var shown = (frac * 100).toFixed(1) + "%";
  var value = kind === "served"
    ? num(used) + " of " + num(total) + " read back \u00b7 " + shown
    : num(used) + " of " + num(total) + " tok \u00b7 " + shown;
  var hasHit = hit !== null && hit !== undefined;

  var subHtml = esc(sub);
  if (opts.active !== null && opts.active !== undefined) {
    subHtml += ' \u00b7 <b class="act">' + num(opts.active) + ' in flight</b>';
  }

  var cells = '<div class="tlabel">' + esc(label) + '<span>' + subHtml + '</span></div>';

  if (opts.traffic) {
    // No bar here on purpose: L3 has no capacity of its own, so drawing a
    // fill would imply one. It is a traffic counter, not an occupancy gauge.
    cells += '<div class="ttraffic" style="grid-column:span 2">'
      + '\u2191 ' + num(opts.traffic.written) + ' tokens written'
      + ' &nbsp;\u00b7&nbsp; \u2193 ' + num(opts.traffic.read) + ' read back</div>';
  } else {
    var bar;
    if (opts.parts) {
      bar = '<div class="tbar">';
      for (var p = 0; p < opts.parts.length; p++) {
        var w = total ? Math.min(100, (opts.parts[p].value / total) * 100) : 0;
        bar += '<i' + (opts.parts[p].cls ? ' class="' + opts.parts[p].cls + '"' : '')
          + ' style="width:' + w.toFixed(2) + '%"></i>';
      }
      bar += '</div>';
    } else {
      bar = '<div class="tbar"><i style="width:' + (frac * 100).toFixed(1) + '%"></i></div>';
    }
    cells += bar + '<div class="tval">' + value + '</div>';
  }

  cells += '<div class="thit' + (hasHit ? '' : ' dim') + '">' + (hasHit ? pct(hit) + ' hit' : '-') + '</div>';
  return '<div class="tier">' + cells + '</div>';
}
function render(d){
  var s = d.store || {};
  var meta = document.getElementById("meta");
  if (d.loading) { meta.textContent = "collecting first sample"; }
  else { meta.textContent = "updated " + d.age_seconds + "s ago \u00b7 next read in " + d.next_refresh_in_seconds + "s \u00b7 every " + d.refresh_interval_seconds + "s"; }
  document.getElementById("store").innerHTML =
    tile("Clients", num(s.clients)) +
    tile("Segments", num(s.segments)) +
    tile("Keys", num(s.keys)) +
    tile("Evictions", num(s.evictions)) +
    tile("Alloc failures", num(s.alloc_failures));

  document.getElementById("ramnum").textContent =
    (s.used_bytes === null || s.used_bytes === undefined ? "-" : bytes(s.used_bytes))
    + " of " + (s.capacity_bytes === null || s.capacity_bytes === undefined ? "-" : bytes(s.capacity_bytes))
    + (s.used_pct === null || s.used_pct === undefined ? "" : "  \u00b7  " + s.used_pct.toFixed(1) + "%");
  document.getElementById("bar").style.width = Math.min(100, Math.max(0, s.used_pct || 0)) + "%";

  var diskCap = s.disk_capacity_bytes;
  var diskUsed = s.ssd_bytes;
  var diskPct = (diskCap && diskUsed !== null && diskUsed !== undefined) ? (diskUsed / diskCap) * 100 : null;
  var diskSrc = s.disk_capacity_source === "pvc"
    ? "  \u00b7  " + s.disk_claims + " PVCs"
    : (s.disk_capacity_source === "master" ? "  \u00b7  capacity reported by master" : "");
  document.getElementById("disknum").textContent =
    (diskUsed === null || diskUsed === undefined ? "-" : bytes(diskUsed))
    + " of " + (diskCap ? bytes(diskCap) : "-")
    + (diskPct === null ? "" : "  \u00b7  " + diskPct.toFixed(1) + "%")
    + diskSrc;
  document.getElementById("bar2").style.width = Math.min(100, Math.max(0, diskPct || 0)) + "%";
  document.getElementById("storenote").textContent = s.reachable ? "" : "Master unreachable from the dashboard pod.";
  var models = d.models || [];
  var cards = "";
  for (var i = 0; i < models.length; i++) {
    var m = models[i];
    var reps = m.replicas || [];
    var body = "";
    for (var r = 0; r < reps.length; r++) {
      var x = reps[r];
      var chip = x.reachable
        ? '<span class="chip">up</span>'
        : '<span class="chip bad">' + esc(x.error || "down") + '</span>';
      body += '<div class="replica">'
        + '<div class="repline">'
        + '<span class="podname" title="' + esc(x.pod) + '">' + esc(x.pod) + '</span>'
        + '<span class="nodename">' + esc(x.node) + '</span>'
        + chip
        + '</div>'
        + tierRow("GPU KV", "L1",
                  (x.kv_evictable_tokens || 0) + (x.kv_used_tokens || 0),
                  x.kv_pool_tokens, "tokens", x.l1_pct,
                  {active: x.kv_used_tokens,
                   parts: [{value: x.kv_evictable_tokens || 0, cls: ""},
                           {value: x.kv_used_tokens || 0, cls: "active"}]})
        + tierRow("Host RAM", "L2 \u00b7 pod memory", x.host_used_tokens, x.host_pool_tokens, "tokens", x.l2_pct)
        + tierRow("Mooncake", "L3 \u00b7 shared store, traffic not capacity", 0, 0, "traffic", x.l3_pct,
                  {traffic: {written: x.l3_write_tokens, read: x.l3_tokens}})
        + '<div class="src">' + num(x.input_tokens) + ' prompt tokens \u00b7 <b>'
        + pct((x.l1_pct || 0) + (x.l2_pct || 0) + (x.l3_pct || 0)) + ' served from cache</b></div>'
        + '</div>';
    }
    cards += '<div class="mcard">'
      + '<div class="mhead"><strong>' + esc(m.name) + '</strong>'
      + '<span class="mcount">' + reps.length + (reps.length === 1 ? ' replica' : ' replicas') + '</span></div>'
      + body
      + '</div>';
  }
  var host = document.getElementById("models");
  if (cards) {
    host.className = "";
    host.innerHTML = cards;
  } else {
    host.className = "empty";
    host.textContent = d.loading ? "Collecting the first sample..." : "No Mooncake clients yet. A pod appears here once it carries a MOONCAKE_MASTER env var and its /metrics is reachable.";
  }
  var e = d.errors || [];
  var box = document.getElementById("errors");
  if (e.length) { box.style.display = "block"; document.getElementById("errlist").textContent = e.join(" \u00b7 "); }
  else { box.style.display = "none"; }
}
function get(url, ms){
  var ctl = new AbortController();
  var timer = setTimeout(function(){ ctl.abort(); }, ms);
  return fetch(url, {signal: ctl.signal, headers: {"Accept": "application/json"}}).then(function(r){ clearTimeout(timer); return r.json(); });
}
function fail(){
  document.getElementById("meta").textContent = "reconnecting to the dashboard backend...";
  setTimeout(load, 5000);
}
function load(){ get("/api/stats", 20000).then(render).catch(fail); }
document.getElementById("refresh").onclick = function(){
  var b = this; b.disabled = true;
  get("/api/refresh", 30000).then(render).catch(fail).then(function(){ b.disabled = false; });
};
setInterval(load, 30000);
load();
</script>
</body></html>"""


def _json_safe(value):
    """Replace non-finite floats with null before encoding.

    json.dumps emits bare NaN / Infinity for these, which is *not* valid JSON.
    Python reads it back happily, every browser's JSON.parse rejects it - so a
    single NaN metric (SGLang exposes e.g. sglang:fwd_occupancy as NaN before
    any forward pass) used to make the whole /api/stats payload unparseable
    and the dashboard sat on "reconnecting to the dashboard backend...".
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


class Handler(BaseHTTPRequestHandler):
    config = None

    def log_message(self, fmt, *args):
        return

    def _send(self, status, body, content_type):
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, data, status=200):
        self._send(status, json.dumps(_json_safe(data)), "application/json")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.replace("__TITLE__", self.config["title"]), "text/html; charset=utf-8")
        elif self.path.startswith("/api/stats"):
            self._json(snapshot(self.config))
        elif self.path.startswith("/api/refresh"):
            # The Refresh button issues a GET (see the fetch in PAGE). This used
            # to exist only on do_POST, so the button 404'd and the page kept
            # showing its first snapshot until the 1800 s timer fired.
            self._json(refresh(self.config))
        elif self.path.startswith("/healthz"):
            self._send(200, "ok", "text/plain")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path.startswith("/api/refresh"):
            self._json(refresh(self.config))
        else:
            self._send(404, "not found", "text/plain")


def build_config():
    namespaces = _env("DISCOVERY_NAMESPACES", "")
    if namespaces:
        namespace_list = [item.strip() for item in namespaces.split(",") if item.strip()]
    else:
        namespace_list = [_sa_namespace()]
    static_models = None
    raw = _env("STATIC_MODELS", "")
    if raw:
        static_models = json.loads(raw)
    return {
        "title": _env("UI_TITLE", "Mooncake KV store"),
        "master_url": _env("MOONCAKE_MASTER_URL", "http://mooncake-master:9003"),
        "interval": int(_env("REFRESH_INTERVAL_SECONDS", "1800")),
        "timeout": float(_env("REQUEST_TIMEOUT_SECONDS", "3")),
        "namespaces": namespace_list,
        "metrics_port": int(_env("MODEL_METRICS_PORT", "8080")),
        "static_models": static_models,
        "exclude_re": re.compile(_env("DISCOVERY_EXCLUDE")) if _env("DISCOVERY_EXCLUDE") else None,
        "port": int(_env("PORT", "8080")),
    }


def selftest_data():
    return {
        "generated_at": _utcnow(),
        "age_seconds": 12,
        "refresh_interval_seconds": 1800,
        "next_refresh_in_seconds": 1788,
        "store": {
            "reachable": True,
            "raw_summary": "Master Metrics: Storage: 12.34 GB / 150.00 GB (8.2%) | Keys: 12345 | Clients: 3",
            "capacity_bytes": 150 * 10**9,
            "used_bytes": int(12.34 * 10**9),
            "used_pct": 8.2,
            "keys": 12345,
            "clients": 3,
            "segments": 3,
            "evictions": 0,
            "alloc_failures": 0,
            "segments_detail": [{"id": "seg-0", "host": "10.0.0.5", "size_bytes": 50 * 10**9}],
        },
        "models": [
            {
                "name": "deepseek-v4.1-flash",
                "namespace": "project-user-andrew-bydlon",
                "replica_count": 2,
                "replicas": [
                    {"pod": "dsv41-a", "node": "scs04", "reachable": True, "error": None,
                     "kv_pool_tokens": 17060352, "kv_used_tokens": 120000, "token_usage": 0.17,
                     "cache_hit_rate": 0.993, "input_tokens": 434199,
                     "l1_tokens": 346752, "l2_tokens": 0, "l3_tokens": 12000,
                     "l1_pct": 0.799, "l2_pct": 0.0, "l3_pct": 0.028,
                     "l3_write_tokens": 447040,
                     "host_pool_tokens": 4090816, "host_used_tokens": 0, "cached_tokens": 346752,
                     "kv_evictable_tokens": 2780000,
                     "metrics": {"sglang:cache_hit_rate": 0.993}},
                    {"pod": "dsv41-b", "node": "scs05", "reachable": True, "error": None,
                     "kv_pool_tokens": 17060352, "kv_used_tokens": 1200000, "token_usage": 0.07,
                     "cache_hit_rate": 0.911, "input_tokens": 980000,
                     "l1_tokens": 600000, "l2_tokens": 90000, "l3_tokens": 210000,
                     "l1_pct": 0.612, "l2_pct": 0.092, "l3_pct": 0.214,
                     "l3_write_tokens": 1012000,
                     "host_pool_tokens": 4090816, "host_used_tokens": 1902336, "cached_tokens": 610400,
                     "kv_evictable_tokens": 900000,
                     "metrics": {"sglang:cache_hit_rate": 0.911}},
                ],
            }
        ],
        "errors": [],
    }


def main():
    parser = argparse.ArgumentParser(description="Mooncake KV-store dashboard")
    parser.add_argument("--selftest", action="store_true", help="print synthetic /api/stats JSON and exit")
    parser.add_argument("--selftest-html", action="store_true", help="print the rendered page and exit")
    args = parser.parse_args()

    config = build_config()
    if args.selftest:
        print(json.dumps(selftest_data(), indent=2))
        return 0
    if args.selftest_html:
        print(PAGE.replace("__TITLE__", config["title"]))
        return 0

    stop_event = threading.Event()
    thread = threading.Thread(target=refresher, args=(config, stop_event), daemon=True)
    thread.start()

    Handler.config = config
    server = ThreadingHTTPServer(("0.0.0.0", config["port"]), Handler)
    print("mooncake dashboard listening on :%d (refresh every %ds)" % (config["port"], config["interval"]), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
