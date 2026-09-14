"""Concurrency sweep: run the load generator at each concurrency level with
per-level wall-clock windows, and join GPU telemetry onto those windows.

Protocol (worked out for hosted-trial scaling runs — see README):

* each level starts with ``warmup_rounds`` × N unmeasured concurrent
  requests (populate server caches, open connection pools);
* the measured load then runs for ``duration`` seconds; the telemetry
  window is exactly this measured phase ([t_start, t_end], epoch seconds);
* between levels the harness settles for ``settle`` seconds (GPU
  utilization decays asynchronously after load stops; averaging through the
  decay would understate the busy level), and the next level's telemetry
  fetch is overlapped with that sleep.

Single-level runs (``-N`` without ``--sweep``) go through the same engine
with one level and no settle — they get the same per-level window and
therefore the same optional GPU snapshot.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from .mcp_driver import ResolvedTool, mcp_call_once, run_user_mcp
from .rest_driver import run_user_rest
from .stats import BenchmarkStats
from .targets import McpTarget, RestTarget
from .telemetry import TelemetryConfig, capture_window, subtract_baseline

log = logging.getLogger("endpoint_benchmarker")

SCRAPE_INTERVAL_WARN_S = 45.0  # ~3x a 15s DCGM scrape interval


@dataclass
class LevelResult:
    concurrency: int
    t_start_epoch: float
    t_end_epoch: float
    stats: BenchmarkStats
    telemetry: dict | None = None
    config: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        out = {
            "concurrency": self.concurrency,
            "t_start_epoch": self.t_start_epoch,
            "t_end_epoch": self.t_end_epoch,
            "client_stats": self.stats.summary(),
            "config": self.config,
        }
        if self.telemetry is not None:
            out["telemetry"] = self.telemetry
        return out


class ProgressReporter:
    """Periodic per-level progress table (log-based; header reprinted every 20 rows)."""

    def __init__(self, interval: float = 5.0) -> None:
        self.interval = max(1.0, interval)
        self._rows_since_header = 0

    async def report(self, label: str, stats: BenchmarkStats) -> None:
        fmt = "  {:>8}  {:>9}  {:>9}  {:>9}  {:>12}"
        while True:
            await asyncio.sleep(self.interval)
            elapsed = time.monotonic() - stats.start_time
            if self._rows_since_header % 20 == 0:
                log.info(fmt.format("elapsed", "requests", "success", "failed", "rate (req/s)") + f"   [{label}]")
                log.info(fmt.format("-" * 8, "-" * 9, "-" * 9, "-" * 9, "-" * 12))
            rate = f"{stats.total / elapsed:.1f}" if elapsed > 0 else "starting"
            log.info(fmt.format(f"{elapsed:.0f}s", stats.total, stats.success, stats.failed, rate))
            self._rows_since_header += 1


async def _gather_with_progress(tasks: list, stats: BenchmarkStats, label: str, interval: float) -> None:
    reporter = ProgressReporter(interval)
    progress_task = asyncio.create_task(reporter.report(label, stats))
    try:
        await asyncio.gather(*tasks)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Warm-up rounds (unmeasured; one round = N concurrent requests)
# ---------------------------------------------------------------------------


async def _warmup_rest(client: httpx.AsyncClient, target: RestTarget, queries: list[str], n: int, call_timeout: float) -> None:
    async def one(i: int) -> None:
        query = random.Random(1_000_003 + i).choice(queries)
        url, params, body = target.build_request(query)
        try:
            await client.request(target.method, url, params=params, headers=target.headers, content=body, timeout=call_timeout)
        except Exception as exc:
            log.debug("warm-up request failed (ignored): %s", exc)

    await asyncio.gather(*(one(i) for i in range(n)))


async def _warmup_mcp(target: McpTarget, resolved: ResolvedTool, queries: list[str], n: int, call_timeout: float) -> None:
    async def one(i: int) -> None:
        query = random.Random(2_000_003 + i).choice(queries)
        await mcp_call_once(target, resolved, query, call_timeout)

    await asyncio.gather(*(one(i) for i in range(n)))


# ---------------------------------------------------------------------------
# Sweep engine
# ---------------------------------------------------------------------------


async def run_sweep(
    levels: list[int],
    *,
    duration: float,
    ramp_up: float,
    warmup_rounds: int,
    settle: float,
    call_timeout: float,
    seed: int | None,
    progress_interval: float,
    queries: list[str],
    rest_target: RestTarget | None = None,
    mcp_target: McpTarget | None = None,
    resolved: ResolvedTool | None = None,
    telemetry_config: TelemetryConfig | None = None,
    baseline: dict | None = None,
) -> list[LevelResult]:
    """Run every concurrency level sequentially; return per-level results."""
    assert (rest_target is None) != (mcp_target is None), "exactly one target kind"
    mode = "rest" if rest_target is not None else "mcp"
    results: list[LevelResult] = []

    if telemetry_config and telemetry_config.prom_url and duration < SCRAPE_INTERVAL_WARN_S:
        log.warning(
            "level duration %.0fs is shorter than ~3 Prometheus scrape intervals — "
            "the per-level GPU average will rest on very few samples; "
            "use --duration 120 or more for a trustworthy scaling curve",
            duration,
        )

    for idx, n_users in enumerate(levels, start=1):
        label = f"level {idx}/{len(levels)} N={n_users}"
        log.info("=== %s: launching %d concurrent users (%s mode) ===", label, n_users, mode)
        ramp_step = ramp_up / n_users if n_users > 0 else 0
        stats = BenchmarkStats()

        if mode == "rest":
            # Pool sized to the level: ModelBenchmarker's connection-pool warning,
            # made structural — every level gets a pool that fits it.
            limits = httpx.Limits(max_connections=n_users * 2 + 10, max_keepalive_connections=n_users)
            async with httpx.AsyncClient(
                limits=limits,
                http2=rest_target.http2,
                verify=not rest_target.insecure,
            ) as client:
                if warmup_rounds > 0:
                    log.info(
                        "%s: warm-up %d round(s) -> %d unmeasured request(s)",
                        label,
                        warmup_rounds,
                        warmup_rounds * n_users,
                    )
                    for _ in range(warmup_rounds):
                        await _warmup_rest(client, rest_target, queries, n_users, call_timeout)
                tasks = [
                    run_user_rest(
                        user_id=i,
                        client=client,
                        target=rest_target,
                        queries=queries,
                        duration=duration,
                        ramp_delay=i * ramp_step,
                        stats=stats,
                        call_timeout=call_timeout,
                        seed=None if seed is None else seed * 100_003 + i,
                    )
                    for i in range(n_users)
                ]
                stats.start_time = time.monotonic()
                t_start_epoch = time.time()
                await _gather_with_progress(tasks, stats, label, progress_interval)
                stats.end_time = time.monotonic()
                t_end_epoch = time.time()
        else:
            if warmup_rounds > 0:
                log.info(
                    "%s: warm-up %d round(s) -> %d unmeasured session(s)",
                    label,
                    warmup_rounds,
                    warmup_rounds * n_users,
                )
                for _ in range(warmup_rounds):
                    await _warmup_mcp(mcp_target, resolved, queries, n_users, call_timeout)
            tasks = [
                run_user_mcp(
                    user_id=i,
                    target=mcp_target,
                    resolved=resolved,
                    queries=queries,
                    duration=duration,
                    ramp_delay=i * ramp_step,
                    stats=stats,
                    call_timeout=call_timeout,
                    seed=None if seed is None else seed * 100_003 + i,
                )
                for i in range(n_users)
            ]
            stats.start_time = time.monotonic()
            t_start_epoch = time.time()
            await _gather_with_progress(tasks, stats, label, progress_interval)
            stats.end_time = time.monotonic()
            t_end_epoch = time.time()

        s = stats.summary()
        log.info(
            "%s done: %d reqs, %.1f%% ok, %.1f req/s, p50 %.0fms, p95 %.0fms, p99 %.0fms",
            label,
            s["total_requests"],
            s["success_rate_pct"],
            s["response_rate_rps"],
            s["latency_ms"]["median"],
            s["latency_ms"]["p95"],
            s["latency_ms"]["p99"],
        )
        if s["failed"]:
            top_errors = list(s["errors"].items())[:3]
            log.warning(
                "%s: %d failure(s), top: %s%s",
                label,
                s["failed"],
                top_errors,
                " ..." if len(s["errors"]) > 3 else "",
            )

        level = LevelResult(
            concurrency=n_users,
            t_start_epoch=t_start_epoch,
            t_end_epoch=t_end_epoch,
            stats=stats,
            config={
                "duration_s": duration,
                "ramp_up_s": ramp_up,
                "warmup_rounds": warmup_rounds,
                "call_timeout_s": call_timeout,
            },
        )
        results.append(level)

        # -- telemetry fetch (overlapped with the settle sleep) -------------
        last = idx == len(levels)
        if telemetry_config and telemetry_config.prom_url:
            capture = capture_window(telemetry_config, t_start_epoch, t_end_epoch)
            if baseline:
                capture = _apply_baseline(capture, baseline)
            if not last and settle > 0:
                log.info("%s: settling %.0fs before next level (GPU util decays asynchronously)", label, settle)
                settle_task = asyncio.create_task(asyncio.sleep(settle))
                level.telemetry = await capture
                await settle_task
            else:
                level.telemetry = await capture
        elif not last and settle > 0:
            log.info("%s: settling %.0fs before next level", label, settle)
            await asyncio.sleep(settle)

    return results


def _apply_baseline(capture_coro, baseline: dict):
    """Wrap the capture coroutine so baseline subtraction runs on its result."""

    async def _run():
        telemetry = await capture_coro
        subtract_baseline(telemetry, baseline)
        return telemetry

    return _run()
