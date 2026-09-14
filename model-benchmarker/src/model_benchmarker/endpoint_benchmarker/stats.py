"""Collected metrics for one benchmark level.

Generalizes ``MultimodalRAG/tests/benchmark.py``'s ``BenchmarkStats``:

* percentile math no longer crashes on runs with fewer than two samples
  (``statistics.quantiles`` raises ``StatisticsError`` for n<2 — the original
  had that latent bug for any single-request run);
* error keys carry a bounded snippet of the actual message
  (``ConnectError: All connection attempts failed``) instead of just the
  exception class name, and the error map is capped so one pathological
  failure mode cannot grow unbounded;
* successful-response sizes are recorded (avg response bytes is a cheap
  sanity check that the endpoint is actually returning content — e.g. an
  empty-result regression shows up as avg_bytes collapsing).
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

MAX_DISTINCT_ERRORS = 32
ERROR_SNIPPET_LEN = 120

DEFAULT_PERCENTILES: tuple[float, ...] = (50.0, 95.0, 99.0)


def pct_label(p: float) -> str:
    """Canonical percentile label: integral values as ``p50``/``p95``,
    fractional as ``p99.9`` (no truncation — 99.9 must never collide with 99)."""
    p = float(p)
    return f"p{int(p)}" if p.is_integer() else f"p{p:g}"

# Error categories for the deep-dive report. Mapping is by the error key
# shape the drivers produce: ``HTTP <code>: ...`` (REST non-2xx),
# ``<ExceptionType>: <snippet>`` (httpx/MCP exception), ``Timeout (>Ns)``
# and ``MCP connect: ...`` (MCP driver shortcuts).
ERROR_CATEGORIES: tuple[str, ...] = ("http_error", "timeout", "connection", "other")


def percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated percentile of an already-sorted list.

    Works for any number of samples (>= 1), unlike ``statistics.quantiles``
    which requires at least two.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def err_key(exc: BaseException) -> str:
    """Bounded, informative error key: ``TypeName: first 120 chars of message``."""
    msg = str(exc).strip().replace("\n", " ")[:ERROR_SNIPPET_LEN]
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def categorize_errors(errors: dict[str, int]) -> dict[str, int]:
    """Bucket the bounded error keys into a few reportable categories.

    Keys look like ``HTTP 500: boom`` (REST non-2xx), ``ReadTimeout: ...`` /
    ``ConnectError: ...`` (httpx exceptions), ``Timeout (>120s)`` and
    ``MCP connect: ...`` (MCP driver shortcuts). Anything unrecognised falls
    into ``other``. Returned in ERROR_CATEGORIES order, zero counts dropped.
    """
    out: dict[str, int] = {}
    for key, count in errors.items():
        if key.startswith("HTTP "):
            cat = "http_error"
        else:
            k = key.lower()
            if "timeout" in k:
                cat = "timeout"
            elif "connect" in k:
                cat = "connection"
            else:
                cat = "other"
        out[cat] = out.get(cat, 0) + count
    return {c: out[c] for c in ERROR_CATEGORIES if out.get(c)}


@dataclass
class BenchmarkStats:
    """Aggregated metrics for one concurrency level (or one whole run)."""

    total: int = 0
    success: int = 0
    failed: int = 0
    latencies: list[float] = field(default_factory=list)
    result_counts: list[int] = field(default_factory=list)
    response_bytes: list[int] = field(default_factory=list)
    status_codes: dict[int, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    start_time: float = 0.0  # time.monotonic()
    end_time: float = 0.0

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

    @property
    def response_rate(self) -> float:
        return self.total / self.duration if self.duration > 0 else 0.0

    @property
    def success_rate(self) -> float:
        return (self.success / self.total * 100) if self.total > 0 else 0.0

    # -- recording -----------------------------------------------------------

    def record_success(self, latency: float, n_results: int = 1, n_bytes: int = 0, status: int | None = None) -> None:
        self.total += 1
        self.success += 1
        self.latencies.append(latency)
        self.result_counts.append(n_results)
        self.response_bytes.append(n_bytes)
        if status is not None:
            self.status_codes[status] = self.status_codes.get(status, 0) + 1

    def record_failure(self, latency: float, key: str, status: int | None = None) -> None:
        self.total += 1
        self.failed += 1
        self.latencies.append(latency)
        if status is not None:
            self.status_codes[status] = self.status_codes.get(status, 0) + 1
        if len(self.errors) < MAX_DISTINCT_ERRORS or key in self.errors:
            self.errors[key] = self.errors.get(key, 0) + 1
        elif "(+more)" not in self.errors:
            self.errors["(+more distinct errors omitted)"] = 1

    # -- summary -------------------------------------------------------------

    def summary(self, percentiles: Sequence[float] = DEFAULT_PERCENTILES) -> dict:
        lat = sorted(self.latencies)
        latency: dict[str, float] = {
            "min": round(min(lat) * 1000, 2) if lat else 0.0,
            "mean": round(statistics.mean(lat) * 1000, 2) if lat else 0.0,
        }
        for p in percentiles:
            latency[pct_label(p)] = round(percentile(lat, p) * 1000, 2) if lat else 0.0
        if any(float(p) == 50.0 for p in percentiles):
            latency["median"] = latency["p50"]  # back-compat alias
        latency["max"] = round(max(lat) * 1000, 2) if lat else 0.0
        return {
            "total_requests": self.total,
            "successful": self.success,
            "failed": self.failed,
            "success_rate_pct": round(self.success_rate, 2),
            "duration_s": round(self.duration, 2),
            "response_rate_rps": round(self.response_rate, 2),
            "latency_ms": latency,
            "avg_results_per_query": round(statistics.mean(self.result_counts), 2) if self.result_counts else 0.0,
            "avg_response_bytes": round(statistics.mean(self.response_bytes), 1) if self.response_bytes else 0.0,
            "status_codes": dict(sorted(self.status_codes.items())),
            "errors": dict(sorted(self.errors.items(), key=lambda kv: -kv[1])),
        }
