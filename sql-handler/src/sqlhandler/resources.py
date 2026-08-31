"""Container-aware resource budgets for the SQL engine.

The engine's DuckDB connections used to run with DuckDB's defaults, which are
derived from the *node's* RAM (80% of ``/proc/meminfo``) — on a big node with
a small pod that is a recipe for OOMKills: one wide scan grows RSS past the
pod's cgroup limit, the kernel kills the process, and every in-process cache
(table list, describe results, open datasets) dies with it.

This module reads the container's *actual* limits from the cgroup hierarchy
(v2 first, then v1) and derives proportional budgets:

- DuckDB ``memory_limit``   = fraction x container memory limit (default 0.6),
                              so DuckDB spills to ``temp_directory`` when a
                              query would otherwise take the pod down.
- DuckDB ``threads``        = floor of the container CPU limit (min 1) —
                              DuckDB otherwise sizes its thread pool from the
                              machine's core count, spawning dozens of threads
                              inside a 1-CPU pod.

Everything here fails open: when the cgroup files are missing (local dev on
Mac/Windows, uncontainerized runs) the budgets come back empty and DuckDB
keeps its defaults, with a single warning logged.

Environment knobs:
  SQLHANDLER_DUCKDB_MEMORY_FRACTION - fraction of the container memory limit
                                      given to DuckDB (default 0.6; 0 disables
                                      the memory cap).
  SQLHANDLER_DUCKDB_TEMP_DIR        - directory DuckDB spills to (default
                                      /tmp/sqlhandler-duckdb-spill; mount an
                                      emptyDir there in k8s so spills don't
                                      fill the container's writable layer).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("sqlhandler.resources")

# cgroup v1's "unlimited" memory value is a page-count sentinel near 2**63;
# anything at or above this is treated as "no limit".
_UNLIMITED_SENTINEL = 1 << 60

_warned_no_cgroup = False


def _read_cgroup_value(*paths: str) -> str | None:
    """Return the first readable, non-empty file's stripped content."""
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read().strip()
            if content:
                return content
        except OSError:
            continue
    return None


def container_memory_bytes() -> int | None:
    """The container's memory limit in bytes, or None when unlimited/unknown.

    Reads cgroup v2 (``memory.max`` — "max" means unlimited) then cgroup v1
    (``memory/memory.limit_in_bytes`` — a huge page-count sentinel means
    unlimited).
    """
    raw = _read_cgroup_value(
        "/sys/fs/cgroup/memory.max",  # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    )
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    if raw == "max" or value <= 0 or value >= _UNLIMITED_SENTINEL:
        return None
    return value


def container_cpu_count() -> float | None:
    """The container's CPU limit as a float, or None when unlimited/unknown.

    cgroup v2 ``cpu.max`` is "quota period" (e.g. "100000 100000" = 1.0 CPU)
    or "max <period>"; cgroup v1 splits that into ``cpu.cfs_quota_us`` /
    ``cpu.cfs_period_us`` (quota -1 = unlimited).
    """
    raw = _read_cgroup_value("/sys/fs/cgroup/cpu.max")
    if raw is not None:
        parts = raw.split()
        if parts and parts[0] == "max":
            return None
        if len(parts) == 2:
            try:
                quota, period = int(parts[0]), int(parts[1])
            except ValueError:
                return None
            if quota > 0 and period > 0:
                return quota / period
        return None
    quota_raw = _read_cgroup_value(
        "/sys/fs/cgroup/cpu/cpu.cfs_quota_us",
    )
    period_raw = _read_cgroup_value(
        "/sys/fs/cgroup/cpu/cpu.cfs_period_us",
    )
    if quota_raw is None or period_raw is None:
        return None
    try:
        quota, period = int(quota_raw), int(period_raw)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def process_rss_bytes() -> int | None:
    """The process's current RSS in bytes, or None when unavailable."""
    raw = _read_cgroup_value("/proc/self/status") or ""
    for line in raw.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1]) * 1024  # VmRSS is reported in kB
                except ValueError:
                    return None
    return None


def _memory_budget_string(num_bytes: int) -> str:
    """Format bytes the way DuckDB's memory_limit expects (e.g. '1.2GiB')."""
    mib = 1024 * 1024
    gib = 1024 * mib
    if num_bytes >= gib:
        return f"{num_bytes / gib:.1f}GiB"
    return f"{num_bytes / mib:.0f}MiB"


def _memory_fraction() -> float | None:
    """The configured DuckDB memory fraction (0 disables the cap)."""
    raw = os.environ.get("SQLHANDLER_DUCKDB_MEMORY_FRACTION", "").strip()
    if not raw:
        return 0.6
    try:
        value = float(raw)
    except ValueError:
        logger.warning("invalid SQLHANDLER_DUCKDB_MEMORY_FRACTION=%r; using 0.6", raw)
        return 0.6
    if value < 0:
        return 0.6
    return value


def duckdb_budget() -> dict[str, object]:
    """Derive the DuckDB knobs from the container's limits.

    Returns a dict with any of the keys ``memory_limit`` ("1.2GiB" style),
    ``threads`` (int >= 1) and ``temp_directory`` (str) — an empty dict when
    the container's limits are undetectable (DuckDB then keeps its defaults).
    Also reports ``container_memory_bytes`` / ``container_cpu_count`` so
    callers can log what the budget was derived from.
    """
    global _warned_no_cgroup
    memory = container_memory_bytes()
    cpu = container_cpu_count()
    if memory is None and cpu is None:
        if not _warned_no_cgroup:
            _warned_no_cgroup = True
            logger.warning(
                "no cgroup limits detected; DuckDB runs with its own defaults "
                "(80% of node RAM) — set a k8s memory limit or "
                "SQLHANDLER_DUCKDB_MEMORY_FRACTION to bound it"
            )
        return {}
    budget: dict[str, object] = {
        "container_memory_bytes": memory,
        "container_cpu_count": cpu,
    }
    fraction = _memory_fraction()
    if memory is not None and fraction > 0:
        budget["memory_limit"] = _memory_budget_string(int(memory * fraction))
    if cpu is not None:
        budget["threads"] = max(1, int(cpu))
    budget["temp_directory"] = os.environ.get(
        "SQLHANDLER_DUCKDB_TEMP_DIR", "/tmp/sqlhandler-duckdb-spill"
    )
    return budget
