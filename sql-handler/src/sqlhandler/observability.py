"""Ops observability: Prometheus metrics + structured query audit log.

Dependency-free (Prometheus text exposition 0.0.4, JSONL audit file), so the
server keeps zero extra packages. Everything is best-effort: a metrics or
audit failure must never affect a query.

Metrics (rendered at GET /metrics):

  sqlhandler_queries_total{outcome}        counter (ok | error | timeout | cancelled)
  sqlhandler_query_duration_seconds        histogram (per-query wall time)
  sqlhandler_query_rows_total              counter (rows returned, ok queries)
  sqlhandler_cache_hits_total{cache}       counter (describe | profile | dataset)
  sqlhandler_cache_misses_total{cache}     counter
  sqlhandler_tables                        gauge  (current table count)
  sqlhandler_process_rss_bytes             gauge
  sqlhandler_container_memory_limit_bytes  gauge (0 when unlimited)

Audit log (SQLHANDLER_AUDIT_LOG=path): one JSON line per query outcome —
{"ts", "event": "query", "sql", "state", "duration_ms", "n_rows", "error"} —
so every SQL executed against the lake is reviewable (SIEM-friendly).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time

logger = logging.getLogger("sqlhandler.observability")

_PROMPT_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Histogram buckets (seconds) — spanning the sub-second cache-hit path to
# multi-minute lake scans.
_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300)


class _Counter:
    """A labeled set of monotonic counters (thread-safe)."""

    def __init__(self, name: str, help_text: str, label: str):
        self.name = name
        self.help = help_text
        self.label = label
        self._values: dict[str, float] = {}
        self._lock = threading.Lock()

    def inc(self, label: str, amount: float = 1) -> None:
        with self._lock:
            self._values[label] = self._values.get(label, 0) + amount

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)


class _Histogram:
    """A fixed-bucket cumulative histogram (thread-safe, per-label)."""

    def __init__(self, name: str, help_text: str, buckets: tuple[float, ...]):
        self.name = name
        self.help = help_text
        self.buckets = buckets
        self._counts: dict[str, list[int]] = {}  # label -> per-bucket cumulative
        self._sums: dict[str, float] = {}
        self._totals: dict[str, int] = {}
        self._lock = threading.Lock()

    def observe(self, label: str, value: float) -> None:
        with self._lock:
            counts = self._counts.setdefault(label, [0] * len(self.buckets))
            for i, b in enumerate(self.buckets):
                if value <= b:
                    counts[i] += 1
            self._sums[label] = self._sums.get(label, 0.0) + value
            self._totals[label] = self._totals.get(label, 0) + 1

    def snapshot(self, label: str) -> tuple[list[int], float, int]:
        with self._lock:
            return (
                list(self._counts.get(label, [0] * len(self.buckets))),
                self._sums.get(label, 0.0),
                self._totals.get(label, 0),
            )


class Metrics:
    """Registry of the server's counters/histograms/gauges + text rendering."""

    def __init__(self):
        self.queries = _Counter(
            "sqlhandler_queries_total", "Queries executed, by outcome.", "outcome"
        )
        self.duration = _Histogram(
            "sqlhandler_query_duration_seconds", "Query wall time in seconds.", _DURATION_BUCKETS
        )
        self.rows = _Counter("sqlhandler_query_rows_total", "Rows returned by ok queries.", "table")

    def record_query(self, outcome: str, duration_s: float, n_rows: int | None, table: str = "") -> None:
        """One query outcome (called from the engine's record path)."""
        try:
            self.queries.inc(outcome)
            self.duration.observe(outcome, max(duration_s, 0.0))
            if outcome == "ok" and n_rows is not None:
                self.rows.inc(table or "unknown", n_rows)
        except Exception:
            logger.debug("metrics record failed", exc_info=True)

    def render(self, engine=None) -> str:
        """Prometheus text exposition (engine gauges included when given)."""
        lines: list[str] = []

        def emit(name: str, help_text: str, typ: str, series: list[tuple[str, str]]) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {typ}")
            for labels, value in series:
                lines.append(f"{name}{labels} {value}")

        q = self.queries.snapshot()
        emit(
            self.queries.name,
            self.queries.help,
            "counter",
            [(f'{{outcome="{lbl}"}}', val) for lbl, val in sorted(q.items())],
        )
        for outcome in sorted(q):
            counts, total_sum, total_n = self.duration.snapshot(outcome)
            cumulative = 0
            for i, bucket in enumerate(self.duration.buckets):
                cumulative = counts[i]  # counts are cumulative by construction
                lines.append(
                    f'{self.duration.name}_bucket{{outcome="{outcome}",le="{bucket}"}} {cumulative}'
                )
            lines.append(f'{self.duration.name}_bucket{{outcome="{outcome}",le="+Inf"}} {total_n}')
            emit_count = f"{self.duration.name}_sum{{outcome=\"{outcome}\"}} {round(total_sum, 6)}"
            lines.append(emit_count)
            lines.append(f'{self.duration.name}_count{{outcome="{outcome}"}} {total_n}')
        r = self.rows.snapshot()
        if r:
            emit(
                self.rows.name,
                self.rows.help,
                "counter",
                [(f'{{table="{lbl}"}}', val) for lbl, val in sorted(r.items())],
            )
        if engine is not None:
            try:
                stats = engine.cache_stats()
                for cache in ("describe", "profile", "dataset"):
                    hits = stats.get(f"{cache}_hits", 0)
                    misses = stats.get(f"{cache}_misses", 0)
                    lines.append("# HELP sqlhandler_cache_hits_total Cache hits by cache type.")
                    lines.append("# TYPE sqlhandler_cache_hits_total counter")
                    lines.append(f'sqlhandler_cache_hits_total{{cache="{cache}"}} {hits}')
                    lines.append("# TYPE sqlhandler_cache_misses_total counter")
                    lines.append(f'sqlhandler_cache_misses_total{{cache="{cache}"}} {misses}')
                tables = engine.list_tables()
                lines.append("# HELP sqlhandler_tables Tables currently exposed.")
                lines.append("# TYPE sqlhandler_tables gauge")
                lines.append(f"sqlhandler_tables {len(tables)}")
                rss = stats.get("process_rss_bytes")
                lines.append("# HELP sqlhandler_process_rss_bytes Process resident memory.")
                lines.append("# TYPE sqlhandler_process_rss_bytes gauge")
                lines.append(f"sqlhandler_process_rss_bytes {rss or 0}")
                mem = stats.get("container_memory_bytes")
                lines.append("# HELP sqlhandler_container_memory_limit_bytes Container memory limit.")
                lines.append("# TYPE sqlhandler_container_memory_limit_bytes gauge")
                lines.append(f"sqlhandler_container_memory_limit_bytes {mem or 0}")
            except Exception:
                logger.debug("engine gauges unavailable for /metrics", exc_info=True)
        return "\n".join(lines) + "\n"


metrics = Metrics()  # process-wide registry


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------


def audit_log_path() -> str:
    """Configured audit JSONL path ('' = audit logging off)."""
    return os.environ.get("SQLHANDLER_AUDIT_LOG", "").strip()


def audit_query(sql: str, state: str, duration_ms: float | None, n_rows: int | None, error: str | None) -> None:
    """Append one query outcome to the audit JSONL (best-effort, never raises)."""
    path = audit_log_path()
    if not path:
        return
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "query",
        "sql": sql[:2000],
        "state": state,
        "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        "n_rows": n_rows,
        "error": error[:500] if error else None,
    }
    try:
        line = json.dumps(record, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        logger.debug("audit log write failed", exc_info=True)


def token_matches(provided: str, expected: str) -> bool:
    """Constant-time comparison of a provided API token against the expected one.

    Accepts both ``Bearer <token>`` (Authorization header) and a bare token
    (X-API-Token header).
    """
    if not expected:
        return True
    provided = (provided or "").strip()
    if provided.lower().startswith("bearer "):
        provided = provided[7:].strip()
    return hmac.compare_digest(provided, expected)
