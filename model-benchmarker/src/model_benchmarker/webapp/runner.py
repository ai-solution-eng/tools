"""Benchmark run management for the webapp: target validation, parameter
validation, argv building and the subprocess run registry.

Security posture (the webapp is a load generator behind an API-key gate):

* Targets are FREE TEXT from the user (any http(s) URL -- the point of the
  universal benchmarker), validated to scheme+host and rejected when they
  embed credentials (use the API-key field / headers instead, so the value
  is redacted from artifacts).
* ONE benchmark per endpoint at a time -- THE contention rule: a second run
  against the same endpoint ORIGIN is refused (HTTP 409) while one is in
  flight, because concurrent sweeps against the same backend would
  contaminate each other's measurements (and load it unpredictably).
* Every client-supplied value is validated against a whitelist and handed
  to the bench CLIs as argv elements (subprocess list form, never a shell).
* Target API keys travel per-process env (PCAI_API_KEY) or headers, never
  argv, and are redacted from everything the API serves back.

The bench engines themselves are unchanged: a run is exactly the documented
CLI invocation (python -m model_benchmarker.benchmark_chat /
...endpoint_benchmarker) writing its artifacts into a per-run directory.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ENDPOINT_KEYS_ENV = "BENCH_ENDPOINT_API_KEYS"  # JSON object: target URL -> key
TARGET_SUGGESTIONS_ENV = "BENCH_TARGET_SUGGESTIONS"  # JSON list: curated targets for the combobox
CATALOG_PATH_ENV = "BENCH_CATALOG_PATH"  # optional: explicit seed_catalog.json location
PROM_URL_ENV = "BENCH_PROM_URL"
MAX_CONCURRENT_ENV = "BENCH_MAX_CONCURRENT_RUNS"
MAX_RUN_SECONDS_ENV = "BENCH_MAX_RUN_SECONDS"

KINDS = ("chat", "endpoint")

# limits: one place, so the API 400 messages and the values.yaml comments agree
MAX_LEVELS = 8
MAX_USERS = 512
MAX_QUERIES = 500
MAX_HEADERS = 16
MAX_NOTES = 5
DEFAULT_RUN_TIMEOUT_S = 7200.0
DEFAULT_MAX_CONCURRENT = 2

_INT_RE = re.compile(r"^[0-9]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_ARG_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]+$")
_PCT_RE = re.compile(r"^[0-9]+(\.[0-9]+)?(,[0-9]+(\.[0-9]+)?)*$")
_GOODPUT_RE = re.compile(r"^[A-Za-z0-9<>=,._ \-]{0,200}$")
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9_=\"',.(){}~|!<>=+\- ]{1,300}$")

CHAT_TASKS = ("coding", "creative", "mixed", "custom")


class BenchError(Exception):
    """Client-facing validation error -> HTTP 400."""


class BusyEndpointError(BenchError):
    """The endpoint already has a run in flight -> HTTP 409."""

    def __init__(self, endpoint: str, run_id: str) -> None:
        super().__init__(
            f"endpoint {endpoint} already has a benchmark running ({run_id}) -- one benchmark per endpoint at a time"
        )
        self.endpoint = endpoint
        self.run_id = run_id


class MaxRunsError(BenchError):
    """The deployment-wide concurrent-run cap is reached -> HTTP 429."""


# ---------------------------------------------------------------------------
# target validation (free text from the user) + operator config
# ---------------------------------------------------------------------------

TARGET_MAX_LEN = 2048


def validate_target(url: str) -> str:
    """Validate a free-text benchmark target. Returns the cleaned URL.

    Rules: http(s) only; a hostname required; no embedded credentials (they
    would land UNREDACTED in artifacts -- the API-key field and headers are
    the supported, redacted channels); length-capped; no whitespace or
    control characters anywhere in the URL.
    """
    u = str(url or "").strip()
    if not u:
        raise BenchError("target URL is required")
    if len(u) > TARGET_MAX_LEN:
        raise BenchError(f"target URL is limited to {TARGET_MAX_LEN} characters")
    if any(ch.isspace() or ord(ch) < 32 for ch in u):
        raise BenchError("target URL contains whitespace or control characters")
    parsed = urllib.parse.urlsplit(u)
    if parsed.scheme not in ("http", "https"):
        raise BenchError("target URL must start with http:// or https://")
    if not parsed.hostname:
        raise BenchError("target URL needs a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise BenchError(
            "target URL must not embed credentials (user:pass@host) -- put them "
            "in the API-key field or headers so they are redacted from artifacts"
        )
    return u


def origin_of(url: str) -> str:
    """The contention identity of a target: scheme://host:port (lowercased
    host, explicit default port). Two paths on the same server contend for
    the same backend -- the lock is per ORIGIN, not per full URL."""
    parsed = urllib.parse.urlsplit(validate_target(url))
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    host = (parsed.hostname or "").lower()
    return f"{parsed.scheme}://{host}:{port}"


@dataclass(frozen=True)
class TargetSuggestion:
    name: str
    url: str
    kind: str  # chat | endpoint | any


def load_target_suggestions() -> list[TargetSuggestion]:
    """Operator-curated targets for the UI's searchable combobox (from the
    chart's bench.suggestions). ADVICE ONLY -- a user can always type any
    http(s) URL; suggestions never restrict what can be benchmarked. Invalid
    entries are skipped loudly and never block the page."""
    raw = (os.environ.get(TARGET_SUGGESTIONS_ENV) or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        print(
            f"[webapp] ERROR: {TARGET_SUGGESTIONS_ENV} is not valid JSON -- suggestions ignored",
            file=sys.stderr,
            flush=True,
        )
        return []
    if not isinstance(data, list):
        print(
            f"[webapp] ERROR: {TARGET_SUGGESTIONS_ENV} must be a JSON list -- suggestions ignored",
            file=sys.stderr,
            flush=True,
        )
        return []
    out: list[TargetSuggestion] = []
    for item in data:
        if not isinstance(item, dict) or not item.get("name") or not item.get("url"):
            print(
                f"[webapp] ERROR: {TARGET_SUGGESTIONS_ENV} entry needs name+url: {item!r} -- skipped",
                file=sys.stderr,
                flush=True,
            )
            continue
        name = str(item["name"]).strip()
        url = str(item["url"]).strip()
        kind = str(item.get("kind") or "any").strip().lower()
        try:
            validate_target(url)
        except BenchError as exc:
            print(
                f"[webapp] ERROR: {TARGET_SUGGESTIONS_ENV} entry {name!r}: {exc} -- skipped",
                file=sys.stderr,
                flush=True,
            )
            continue
        if not re.match(r"^[A-Za-z0-9_. ()/-]{1,64}$", name):
            print(
                f"[webapp] ERROR: {TARGET_SUGGESTIONS_ENV} entry {name!r}: invalid name -- skipped",
                file=sys.stderr,
                flush=True,
            )
            continue
        out.append(TargetSuggestion(name=name, url=url, kind=kind if kind in ("chat", "endpoint", "any") else "any"))
    return out


def convert_remote_url_to_local(path: str) -> str:
    """Rewrite a remote PCAI serving URL to its in-cluster form -- the exact
    rule of utils/pcai_model_classes.ChatModel._convert_remote_url_to_local:
    https->http, and only URLs containing the '.serving.' marker are
    rewritten (anything else passes through unchanged, never mangled)."""
    new_path = path.replace("https", "http")
    marker = new_path.find(".serving.")
    if marker == -1:
        return new_path
    return new_path[:marker] + ".svc.cluster.local"


def apply_target_usage(url: str, params: dict) -> str:
    """Resolve the LOCAL/REMOTE toggle (the pcai-model-classes behavior, one
    press in the UI): usage=local rewrites '.serving.' URLs to their
    in-cluster .svc.cluster.local form; usage=remote (default) keeps the
    URL the user gave. Non-serving URLs are returned unchanged either way."""
    usage = str(params.get("target_usage") or params.get("model_usage") or "remote").strip().lower()
    if usage == "local":
        return convert_remote_url_to_local(url)
    return url


def endpoint_api_keys() -> dict[str, str]:
    """Target API keys: JSON object of exact target URL -> key (from a
    Secret). When a run's target matches exactly, the key is injected into
    the bench subprocess (PCAI_API_KEY / Bearer) -- never served back."""
    raw = (os.environ.get(ENDPOINT_KEYS_ENV) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        print(
            f"[webapp] ERROR: {ENDPOINT_KEYS_ENV} is not valid JSON -- endpoint keys ignored",
            file=sys.stderr,
            flush=True,
        )
        return {}
    if not isinstance(data, dict):
        print(
            f"[webapp] ERROR: {ENDPOINT_KEYS_ENV} must be a JSON object -- endpoint keys ignored",
            file=sys.stderr,
            flush=True,
        )
        return {}
    return {str(k).rstrip("/"): str(v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def _int_in(params: dict, key: str, lo: int, hi: int) -> int | None:
    v = params.get(key)
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BenchError(f"{key} must be an integer") from None
    if not lo <= n <= hi:
        raise BenchError(f"{key} must be between {lo} and {hi}")
    return n


def _float_in(params: dict, key: str, lo: float, hi: float) -> float | None:
    v = params.get(key)
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise BenchError(f"{key} must be a number") from None
    if not lo <= x <= hi:
        raise BenchError(f"{key} must be between {lo} and {hi}")
    return x


def _one_of(params: dict, key: str, choices: tuple[str, ...]) -> str | None:
    v = params.get(key)
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    if s not in choices:
        raise BenchError(f"{key} must be one of: {', '.join(choices)}")
    return s


def _int_list(params: dict, key: str, lo: int, hi: int) -> list[int] | None:
    v = params.get(key)
    if v is None or str(v).strip() == "":
        return None
    parts = [p.strip() for p in str(v).split(",") if p.strip()]
    if not parts or len(parts) > MAX_LEVELS:
        raise BenchError(f"{key} must be up to {MAX_LEVELS} comma-separated integers")
    out = []
    for p in parts:
        if not _INT_RE.match(p):
            raise BenchError(f"{key} must be comma-separated integers (got {p!r})")
        n = int(p)
        if not lo <= n <= hi:
            raise BenchError(f"{key} values must be between {lo} and {hi} (got {n})")
        out.append(n)
    return out


def _kv_dict(params: dict, key: str, value_max: int = 8192, name_re: re.Pattern[str] | None = None) -> dict[str, str]:
    v = params.get(key)
    if not v:
        return {}
    if not isinstance(v, dict):
        raise BenchError(f"{key} must be an object of name: value pairs")
    if len(v) > MAX_HEADERS:
        raise BenchError(f"{key} allows at most {MAX_HEADERS} entries")
    name_pattern = name_re or _HEADER_RE
    out: dict[str, str] = {}
    for k, val in v.items():
        k = str(k).strip()
        if not name_pattern.match(k):
            raise BenchError(f"{key}: invalid name {k!r}")
        sval = str(val)
        if len(sval) > value_max or any(c in sval for c in "\r\n\x00"):
            raise BenchError(f"{key}.{k}: value too long or contains control characters")
        out[k] = sval
    return out


def _match_str(params: dict, key: str, pattern: re.Pattern[str], label: str, cap: int = 300) -> str | None:
    v = params.get(key)
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    if len(s) > cap or not pattern.match(s):
        raise BenchError(f"{key}: {label}")
    return s


def _capped_str(params: dict, key: str, cap: int) -> str | None:
    v = params.get(key)
    if v is None or v == "":
        return None
    s = str(v)
    if len(s) > cap:
        raise BenchError(f"{key} is limited to {cap} characters")
    return s.strip()


def _str_list(params: dict, key: str, item_cap: int, count_cap: int) -> list[str] | None:
    v = params.get(key)
    if v is None or v == "":
        return None
    if not isinstance(v, list):
        raise BenchError(f"{key} must be a list of strings")
    if len(v) > count_cap:
        raise BenchError(f"{key} allows at most {count_cap} entries")
    out = []
    for item in v:
        s = str(item)
        if not s.strip() or len(s) > item_cap:
            raise BenchError(f"{key}: entries must be non-empty and under {item_cap} characters")
        out.append(s)
    return out


def _flag(params: dict, key: str) -> bool:
    v = params.get(key)
    return bool(v) and v not in ("false", "0", "no")


def _redact(params: dict) -> dict:
    """Copy of params safe to serve back: header values and keys masked."""
    out: dict[str, Any] = dict(params)
    if isinstance(out.get("headers"), dict):
        out["headers"] = {
            k: (v[:3] + "...(redacted)" if len(v) > 3 else "(redacted)") for k, v in out["headers"].items()
        }
    if isinstance(out.get("args"), dict):
        out["args"] = {
            k: ("...(redacted)" if "key" in k.lower() or "token" in k.lower() else v) for k, v in out["args"].items()
        }
    out.pop("api_key", None)
    return out


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# compare-view helpers (pure: redacted params -> config subset; the params
# LABEL lives in webapp/compare.py::params_label -- one tested implementation).
# The config subset is built here because RunManager owns what may leave the
# run directory (row PARSING/merging lives in webapp/compare.py, imported
# lazily inside compare_data so a parser change can never break run launching)
# ---------------------------------------------------------------------------

_SENSITIVE_KEY_PARTS = ("key", "token", "secret", "password", "authorization", "credential")


def _flatten_config_subset(params: dict) -> dict[str, Any]:
    """Flat key/value subset of a run's params, safe for a public payload:
    scalars and short lists only; dict values collapse to entry counts; long
    strings are truncated; header/arg maps are dropped entirely (values were
    redacted at write time, but the subset should not echo them at all)."""
    out: dict[str, Any] = {}
    for k in sorted(params or {}):
        kl = str(k).lower()
        if kl in ("headers", "args"):
            continue
        if any(s in kl for s in _SENSITIVE_KEY_PARTS):
            continue  # api_key etc: name itself stays out of the subset
        v = params[k]
        if isinstance(v, dict):
            if v:
                out[k] = f"{len(v)} entries"
            continue
        if isinstance(v, list):
            if not v:
                continue
            if len(v) <= 8 and all(isinstance(x, (str, int, float, bool)) for x in v):
                joined = ",".join(str(x) for x in v)
                out[k] = joined if len(joined) <= 120 else f"{len(v)} entries"
            else:
                out[k] = f"{len(v)} entries"
            continue
        if v in (None, ""):
            continue
        s = str(v)
        if len(s) > 120:
            out[k] = s[:117] + "..."
            continue
        out[k] = v
    return out


def _endpoint_params_label(params: dict) -> str:
    """Delegate to compare.params_label (one tested implementation)."""
    from .compare import params_label

    return params_label("endpoint", params)


def _chat_params_label(params: dict) -> str:
    """Delegate to compare.params_label (one tested implementation)."""
    from .compare import params_label

    return params_label("chat", params)


def _package_pythonpath() -> str:
    """PYTHONPATH entry that makes model_benchmarker importable in the bench
    subprocess: the package's parent (src/ in a dev tree, site-packages when
    pip-installed). Prepended to any inherited PYTHONPATH."""
    parent = str(Path(__file__).resolve().parents[2])
    existing = os.environ.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if parent not in parts:
        parts.insert(0, parent)
    return os.pathsep.join(parts)


# ---------------------------------------------------------------------------
# argv builders (pure: validated params -> argv + per-process env extras)
# ---------------------------------------------------------------------------


def build_chat_argv(
    target_url: str, params: dict, run_dir: Path, prom_url: str | None = None
) -> tuple[list[str], dict[str, str], dict]:
    """Validate chat-bench params and build the benchmark_chat invocation."""
    del prom_url  # chat bench has no telemetry join (yet)
    original_url = target_url
    target_url = apply_target_usage(target_url, params)
    argv = [
        sys.executable,
        "-m",
        "model_benchmarker.benchmark_chat",
        "--url",
        target_url,
        # per-request progress prints run on the event loop and distort
        # TTFT/ITL when stdout is a pipe (the module's own --quiet guidance)
        "--quiet",
        "--output",
        str(run_dir / "report.md"),
    ]
    env: dict[str, str] = {}

    api_key = _capped_str(params, "api_key", 4096) or ""
    if not api_key:
        api_key = endpoint_api_keys().get(original_url.rstrip("/"), "")
    if api_key:
        env["PCAI_API_KEY"] = api_key  # per-process env, never argv

    users = _int_list(params, "number_users", 1, MAX_USERS) or [1]
    argv += ["--number_users", ",".join(str(u) for u in users)]

    rpu = _int_in(params, "requests_per_user", 1, 100)
    if rpu is not None:
        argv += ["--requests_per_user", str(rpu)]

    contexts = _int_list(params, "context_length", 0, 1_000_000)
    if contexts:
        argv += ["--context_length", ",".join(str(c) for c in contexts)]

    tasks_raw = params.get("tasks")
    if tasks_raw:
        tasks = [t.strip().lower() for t in str(tasks_raw).split(",") if t.strip()]
        if not tasks or len(tasks) > 4:
            raise BenchError("tasks: choose 1-4 of coding, creative, mixed, custom")
        for t in tasks:
            if t not in CHAT_TASKS or not _TOKEN_RE.match(t):
                raise BenchError(f"unknown task {t!r} -- available: {', '.join(CHAT_TASKS)}")
        argv += ["--tasks", ",".join(tasks)]

    arrival = _one_of(params, "arrival_mode", ("closed", "open"))
    if arrival:
        argv += ["--arrival_mode", arrival]
    if arrival == "open":
        rate = _float_in(params, "request_rate", 0.01, 1000.0)
        if rate is None:
            raise BenchError("request_rate is required with arrival_mode=open")
        argv += ["--request_rate", str(rate)]
        level = _float_in(params, "level_duration", 1.0, 3600.0)
        if level is not None:
            argv += ["--level_duration", str(level)]
        burst = _float_in(params, "burstiness", 0.01, 100.0)
        if burst is not None:
            argv += ["--burstiness", str(burst)]

    goodput = _capped_str(params, "goodput", 200)
    if goodput:
        if not _GOODPUT_RE.match(goodput):
            raise BenchError("goodput: use e.g. 'ttft<=2000,tpot<=50'")
        argv += ["--goodput", goodput]

    max_tokens = _int_in(params, "max_tokens", 1, 65536)
    if max_tokens is not None:
        argv += ["--max_tokens", str(max_tokens)]
    temperature = _float_in(params, "temperature", 0.0, 2.0)
    if temperature is not None:
        argv += ["--temperature", str(temperature)]
    top_p = _float_in(params, "top_p", 0.0, 1.0)
    if top_p is not None:
        argv += ["--top_p", str(top_p)]

    thinking = _one_of(params, "thinking", ("enable", "disable"))
    if thinking == "enable":
        argv.append("--enable_thinking")
    elif thinking == "disable":
        argv.append("--disable_thinking")
    tlevel = _one_of(params, "thinking_level", ("off", "low", "medium", "high", "x-high"))
    if tlevel:
        argv += ["--thinking_level", tlevel]

    if _flag(params, "multiturn"):
        argv.append("--multiturn")
    if _flag(params, "no_nonce"):
        argv.append("--no-nonce")
    if _flag(params, "prewarm"):
        argv.append("--prewarm")
    if _flag(params, "separate_tasks"):
        argv.append("--separate_tasks")

    prompt = _capped_str(params, "prompt", 20000)
    if prompt:
        argv += ["--prompt", prompt]

    seed = _int_in(params, "seed", 0, 2**31 - 1)
    if seed is not None:
        argv += ["--seed", str(seed)]

    return argv, env, _redact(params)


def build_endpoint_argv(
    target_url: str, params: dict, run_dir: Path, prom_url: str | None = None
) -> tuple[list[str], dict[str, str], dict]:
    """Validate universal-bench params and build the endpoint_benchmarker run."""
    mode = _one_of(params, "mode", ("rest", "mcp")) or "rest"
    original_url = target_url
    target_url = apply_target_usage(target_url, params)
    argv = [
        sys.executable,
        "-m",
        "model_benchmarker.endpoint_benchmarker",
        "--mode",
        mode,
        "--url",
        target_url,
        "--output",
        str(run_dir / "run.json"),
        "--csv",
        str(run_dir / "run.csv"),
        "--md",
        str(run_dir / "report.md"),
        "--html",
        str(run_dir / "report.html"),
    ]
    env: dict[str, str] = {}

    headers = _kv_dict(params, "headers")
    api_key = _capped_str(params, "api_key", 4096) or endpoint_api_keys().get(original_url.rstrip("/"), "")
    if api_key and not any(h.lower() == "authorization" for h in headers):
        headers["Authorization"] = "Bearer " + api_key
    for k, v in headers.items():
        argv += ["--header", f"{k}={v}"]

    if _flag(params, "insecure"):
        argv.append("--insecure")

    dataset = _capped_str(params, "dataset", 128)
    if dataset:
        if not _TOKEN_RE.match(dataset):
            raise BenchError("dataset: unexpected characters")
        argv += ["--dataset", dataset]

    if mode == "rest":
        method = _one_of(params, "method", ("get", "post", "put", "patch", "delete", "head"))
        if method:
            argv += ["--method", method.upper()]
        req_path = _capped_str(params, "path", 300)
        if req_path:
            if not req_path.startswith("/"):
                raise BenchError("path must start with '/'")
            argv += ["--path", req_path]
        query_param = _capped_str(params, "query_param", 64)
        if query_param is not None:
            argv += ["--query-param", query_param]
        for k, v in _kv_dict(params, "params").items():
            argv += ["--param", f"{k}={v}"]
        body = _capped_str(params, "body", 20000)
        if body:
            probe = body.replace("{query}", "x").replace("{dataset}", "ds")
            try:
                json.loads(probe)
            except ValueError:
                raise BenchError("body must be a JSON template (checked with {query}/{dataset} filled)") from None
            argv += ["--body", body]
        health = _capped_str(params, "health_path", 200)
        if health is not None:
            argv += ["--health-path", health]
    else:
        transport = _one_of(params, "transport", ("streamable-http", "sse"))
        if transport:
            argv += ["--transport", transport]
        tool = _capped_str(params, "tool", 128)
        if tool:
            if not _TOKEN_RE.match(tool):
                raise BenchError("tool: unexpected characters")
            argv += ["--tool", tool]
        for k, v in _kv_dict(params, "args", name_re=_ARG_NAME_RE).items():
            argv += ["--arg", f"{k}={v}"]
        query_arg = _capped_str(params, "query_arg", 64)
        if query_arg:
            if not _TOKEN_RE.match(query_arg):
                raise BenchError("query_arg: unexpected characters")
            argv += ["--query-arg", query_arg]

    n = _int_in(params, "N", 1, MAX_USERS)
    sweep = _int_list(params, "sweep", 1, MAX_USERS)
    if sweep:
        argv += ["--sweep", ",".join(str(x) for x in sweep)]
    elif n is not None:
        argv += ["-N", str(n)]

    duration = _float_in(params, "duration", 0.1, 3600.0)
    if duration is not None:
        argv += ["--duration", str(duration)]
    ramp = _float_in(params, "ramp_up", 0.0, 600.0)
    if ramp is not None:
        argv += ["--ramp-up", str(ramp)]
    warmup = _int_in(params, "warmup_rounds", 0, 10)
    if warmup is not None:
        argv += ["--warmup-rounds", str(warmup)]
    settle = _float_in(params, "settle", 0.0, 600.0)
    if settle is not None:
        argv += ["--settle", str(settle)]
    call_timeout = _float_in(params, "call_timeout", 1.0, 1800.0)
    if call_timeout is not None:
        argv += ["--call-timeout", str(call_timeout)]
    seed = _int_in(params, "seed", 0, 2**31 - 1)
    if seed is not None:
        argv += ["--seed", str(seed)]

    queries = _str_list(params, "queries", 4000, MAX_QUERIES)
    if queries:
        qfile = run_dir / "queries.txt"
        qfile.write_text("\n".join(queries) + "\n", encoding="utf-8")
        argv += ["--queries-file", str(qfile)]
    query_set = _one_of(params, "query_set", ("generic", "vlm", "mixed"))
    if query_set:
        argv += ["--query-set", query_set]

    # Telemetry: the Prometheus URL is OPERATOR-CONFIGURED (env) -- a client
    # never points the server at an arbitrary scrape target. The selector
    # (which GPUs to attribute) is the user-facing knob.
    if prom_url:
        argv += ["--prom-url", prom_url]
        selector = _match_str(params, "prom_selector", _SELECTOR_RE, "invalid PromQL selector characters")
        if selector:
            argv += ["--prom-selector", selector]
        step = _capped_str(params, "prom_step", 16)
        if step and _INT_RE.match(step.rstrip("smh")):
            argv += ["--prom-step", step]
        baseline = _float_in(params, "baseline_duration", 0.0, 600.0)
        if baseline:
            argv += ["--baseline-duration", str(baseline)]
        for name, promql in _kv_dict(params, "extra_range_queries", value_max=300).items():
            if not _TOKEN_RE.match(name):
                raise BenchError(f"extra_range_queries: invalid name {name!r}")
            argv += ["--extra-range-query", f"{name}={promql}"]

    percentiles = _capped_str(params, "percentiles", 64)
    if percentiles:
        if not _PCT_RE.match(percentiles):
            raise BenchError("percentiles must be e.g. 50,95,99")
        argv += ["--percentiles", percentiles]
    knee = _float_in(params, "knee_factor", 1.0, 100.0)
    if knee is not None:
        argv += ["--knee-factor", str(knee)]
    notes = _str_list(params, "notes", 200, MAX_NOTES) or []
    for note in notes:
        argv += ["--note", note.replace("\n", " ")]

    return argv, env, _redact(params)


BUILDERS = {"chat": build_chat_argv, "endpoint": build_endpoint_argv}


# ---------------------------------------------------------------------------
# run registry
# ---------------------------------------------------------------------------


@dataclass
class RunRecord:
    run_id: str
    kind: str
    endpoint: str  # the contention ORIGIN (scheme://host:port)
    target_url: str
    params: dict
    started_utc: str
    status: str = "running"  # running|success|failed|cancelled|timeout|interrupted
    exit_code: int | None = None
    finished_utc: str | None = None
    pid: int | None = None
    cancel: bool = False
    deadline: float = 0.0
    meta: dict = field(default_factory=dict)


META_FIELDS = (
    "run_id",
    "kind",
    "endpoint",
    "target_url",
    "params",
    "started_utc",
    "status",
    "exit_code",
    "finished_utc",
)


class RunManager:
    """Owns every run: subprocess supervision, per-origin single-flight,
    artifact + log access, and crash-safe metadata in the run directory."""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = Path(work_dir)
        self.runs_dir = self.work_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, RunRecord] = {}
        self._busy: dict[str, str] = {}
        self._lock = threading.Lock()
        self._scan_workdir()

    # -- persistence ---------------------------------------------------------

    def _write_meta(self, rec: RunRecord) -> None:
        meta = {k: getattr(rec, k) for k in META_FIELDS}
        target = self.runs_dir / rec.run_id / "run_meta.json"
        tmp = target.with_name("run_meta.json.partial")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, target)

    def _scan_workdir(self) -> None:
        """Adopt run directories written by previous processes (the in-memory
        registry is per-process by design; the artifacts on the PVC are the
        record). A 'running' entry from a dead process becomes 'interrupted'."""
        for meta_path in sorted(self.runs_dir.glob("*/run_meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rec = RunRecord(
                run_id=str(meta.get("run_id") or meta_path.parent.name),
                kind=str(meta.get("kind") or "endpoint"),
                endpoint=str(meta.get("endpoint") or "?"),
                target_url=str(meta.get("target_url") or ""),
                params=meta.get("params") or {},
                started_utc=str(meta.get("started_utc") or ""),
                status=str(meta.get("status") or "failed"),
                exit_code=meta.get("exit_code"),
                finished_utc=meta.get("finished_utc"),
            )
            if rec.status == "running":
                rec.status = "interrupted"
                self._write_meta(rec)
            self._runs[rec.run_id] = rec

    # -- lifecycle -------------------------------------------------------------

    def _active_count(self) -> int:
        return sum(1 for r in self._runs.values() if r.status == "running")

    def launch(
        self,
        kind: str,
        target_url: str,
        params: dict,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_run_seconds: float = DEFAULT_RUN_TIMEOUT_S,
    ) -> RunRecord:
        if kind not in KINDS:
            raise BenchError(f"kind must be one of: {', '.join(KINDS)}")
        url = validate_target(target_url)
        # the contention identity follows the RESOLVED target: a local
        # (.svc.cluster.local) rewrite is a DIFFERENT backend from the remote
        # serving URL, so they never contend with each other
        origin = origin_of(apply_target_usage(url, params))
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        prom_url = (os.environ.get(PROM_URL_ENV) or "").strip() or None
        argv, env_extra, redacted = BUILDERS[kind](url, dict(params), run_dir, prom_url)
        # the record shows what actually gets benchmarked: the resolved URL
        # (local/remote toggle applied); the user's original input stays in
        # params.target_url_input for reference.
        resolved_url = argv[argv.index("--url") + 1]
        redacted = {**redacted, "target_url_input": url}

        with self._lock:
            busy = self._busy.get(origin)
            if busy:
                shutil.rmtree(run_dir, ignore_errors=True)
                raise BusyEndpointError(origin, busy)
            if self._active_count() >= max_concurrent:
                shutil.rmtree(run_dir, ignore_errors=True)
                raise MaxRunsError(
                    f"the deployment allows {max_concurrent} concurrent benchmark(s); try again when one finishes"
                )
            rec = RunRecord(
                run_id=run_id,
                kind=kind,
                endpoint=origin,
                target_url=resolved_url,
                # note the resolved usage in the record (redacted-safe)
                params={**redacted, "target_usage": str(params.get("target_usage") or "remote")},
                started_utc=_now(),
                deadline=time.monotonic() + max_run_seconds,
            )
            rec.meta = {"work_dir": str(run_dir)}
            self._runs[run_id] = rec
            self._busy[origin] = run_id

        self._write_meta(rec)
        threading.Thread(
            target=self._supervise, args=(rec, argv, env_extra), daemon=True, name=f"bench-{run_id}"
        ).start()
        return rec

    def _supervise(self, rec: RunRecord, argv: list[str], env_extra: dict[str, str]) -> None:
        run_dir = self.runs_dir / rec.run_id
        log_path = run_dir / "run.log"
        try:
            with open(log_path, "wb") as logf:
                proc = subprocess.Popen(
                    argv,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env={**os.environ, **env_extra, "PYTHONPATH": _package_pythonpath()},
                    cwd=run_dir,
                )
                rec.pid = proc.pid
                while True:
                    try:
                        rc = proc.wait(timeout=2.0)
                        rec.exit_code = rc
                        rec.status = "success" if rc == 0 else "failed"
                        break
                    except subprocess.TimeoutExpired:
                        if rec.cancel or time.monotonic() > rec.deadline:
                            proc.terminate()
                            try:
                                proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait(timeout=10)
                            rec.exit_code = proc.returncode if proc.returncode is not None else -1
                            rec.status = "cancelled" if rec.cancel else "timeout"
                            break
        except Exception as exc:  # launch failure (missing module, bad cwd...)
            rec.status = "failed"
            rec.exit_code = -1
            try:
                log_path.write_text(f"launch error: {type(exc).__name__}: {exc}\n", encoding="utf-8")
            except OSError:
                pass
        finally:
            rec.finished_utc = _now()
            with self._lock:
                if self._busy.get(rec.endpoint) == rec.run_id:
                    del self._busy[rec.endpoint]
            self._write_meta(rec)

    def cancel(self, run_id: str) -> RunRecord:
        rec = self._require(run_id)
        if rec.status != "running":
            raise BenchError(f"run {run_id} is not running (status: {rec.status})")
        rec.cancel = True
        return rec

    # -- reads --------------------------------------------------------------

    def _require(self, run_id: str) -> RunRecord:
        rec = self._runs.get(run_id)
        if rec is None:
            raise KeyError(run_id)
        return rec

    def get(self, run_id: str) -> dict:
        rec = self._require(run_id)
        out = {k: getattr(rec, k) for k in META_FIELDS}
        out["artifacts"] = self.artifacts(run_id)
        out["log_tail"] = self.log_tail(run_id)
        return out

    def list_runs(self, limit: int = 100) -> list[dict]:
        recs = sorted(self._runs.values(), key=lambda r: r.started_utc, reverse=True)[:limit]
        return [{k: getattr(r, k) for k in META_FIELDS} for r in recs]

    def log_tail(self, run_id: str, max_lines: int = 200) -> str:
        log_path = self.runs_dir / run_id / "run.log"
        if not log_path.is_file():
            return ""
        try:
            with open(log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 16384))
                data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(data.splitlines()[-max_lines:])

    def artifacts(self, run_id: str) -> list[dict]:
        run_dir = self.runs_dir / run_id
        if not run_dir.is_dir():
            return []
        out = []
        for p in sorted(run_dir.iterdir()):
            if p.name == "run_meta.json" or not p.is_file():
                continue
            out.append({"name": p.name, "bytes": p.stat().st_size})
        return out

    def artifact_path(self, run_id: str, name: str) -> Path:
        """Validated path to one artifact -- no traversal, no dotfiles."""
        if "/" in name or "\\" in name or name.startswith(".") or name == "run_meta.json":
            raise BenchError("invalid artifact name")
        if not _TOKEN_RE.match(name):
            raise BenchError("invalid artifact name")
        run_dir = (self.runs_dir / run_id).resolve()
        if run_dir.parent != self.runs_dir.resolve():
            raise BenchError("invalid run id")
        target = run_dir / name
        if not target.is_file():
            raise BenchError("no such artifact")
        return target

    def busy_endpoints(self) -> dict[str, str]:
        return dict(self._busy)

    def public_index(self, limit: int = 200) -> list[dict]:
        """Public-safe run index for the results page: no params (they can
        carry redacted-but-still-sensitive header names), no log content.
        From the PVC-resident metadata, newest first."""
        out = []
        for r in sorted(self._runs.values(), key=lambda x: x.started_utc, reverse=True)[:limit]:
            out.append(
                {
                    "run_id": r.run_id,
                    "kind": r.kind,
                    "target_url": r.target_url,
                    "status": r.status,
                    "started_utc": r.started_utc,
                    "finished_utc": r.finished_utc,
                    "artifacts": self.artifacts(r.run_id),
                    "n": len(self.artifacts(r.run_id)),
                }
            )
        return out

    def compare_data(self, run_id: str) -> dict:
        """Normalized summary for the results page's compare view.

        Read-only and public-safe like the rest of the /results surface:
        kind + status + timestamps from run_meta.json, a short params_label
        and a flattened config subset (re-masked defensively -- params were
        already redacted at write time), plus machine-readable rows parsed
        from run.json (endpoint) or report.md (chat). An interrupted run with
        neither artifact returns rows=[] + an "error" reason instead.

        Path safety stays with the RunManager: the run id is resolved inside
        self.runs_dir exactly like artifact_path (no traversal). A missing
        run_meta means the id is unknown -> KeyError, like get().
        """
        rec = self._require(run_id)
        run_dir = (self.runs_dir / rec.run_id).resolve()
        if run_dir.parent != self.runs_dir.resolve():
            raise BenchError("invalid run id")

        # params from the on-disk record (survives restarts), re-redacted for
        # defense in depth: the params_label and config subset must never
        # carry header values or key material even if a meta file predates
        # the write-time redaction
        params = rec.params if isinstance(rec.params, dict) else {}
        redacted = _redact(params)
        config = _flatten_config_subset(redacted)
        if rec.kind == "endpoint":
            label = _endpoint_params_label(redacted)
        else:
            label = _chat_params_label(redacted)

        out: dict[str, Any] = {
            "run_id": rec.run_id,
            "kind": rec.kind,
            "target_url": rec.target_url,
            "status": rec.status,
            "started_utc": rec.started_utc,
            "params_label": label,
            "config": config,
            "rows": [],
            "knee": None,
            "error": None,
        }

        if rec.kind == "endpoint":
            parsed = self._read_json_artifact(run_id, "run.json")
            if parsed is None:
                out["error"] = "no machine-readable summary"
                return out
            from . import compare as _cmp

            out["rows"] = _cmp.normalize_endpoint_rows(parsed)
            summary = parsed.get("summary") if isinstance(parsed, dict) else None
            knee = summary.get("knee") if isinstance(summary, dict) else None
            out["knee"] = knee if isinstance(knee, dict) else None
            return out

        if rec.kind == "chat":
            text = self._read_text_artifact(run_id, "report.md")
            if text is None:
                out["error"] = "no machine-readable summary"
                return out
            from . import compare as _cmp

            out["rows"] = _cmp.normalize_chat_rows(text)
            return out

        out["error"] = f"unknown kind {rec.kind!r}"
        return out

    # -- compare view: artifact readers (validated like artifact_path) --------

    def _read_json_artifact(self, run_id: str, name: str) -> Any:
        try:
            path = self.artifact_path(run_id, name)
        except BenchError:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _read_text_artifact(self, run_id: str, name: str) -> str | None:
        try:
            path = self.artifact_path(run_id, name)
        except BenchError:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None


def manager_from_env(work_dir: str | None = None) -> RunManager:
    wd = Path(work_dir or os.environ.get("BENCH_WORK_DIR") or "/data")
    # Cache HF config.json fetches (the memory page's catalog entries) on the
    # work dir (the PVC): writable where ~/.cache may not be, and persistent.
    if not os.environ.get("HF_HOME"):
        try:
            hf = wd / ".hf-cache"
            hf.mkdir(parents=True, exist_ok=True)
            os.environ["HF_HOME"] = str(hf)
        except OSError:
            pass  # unwritable work dir: huggingface_hub falls back to defaults
    return RunManager(wd)
