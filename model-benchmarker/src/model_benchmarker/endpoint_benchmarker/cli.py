"""CLI entry point and run orchestration."""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
from datetime import UTC, datetime

import httpx

from . import __version__
from .logging_setup import log, setup_logging
from .mcp_driver import ResolvedTool
from .queries import load_queries
from .report import build_run_payload, print_results, write_artifacts
from .stats import DEFAULT_PERCENTILES, err_key
from .sweep import run_sweep
from .targets import McpTarget, RestTarget, TargetError, parse_kv
from .telemetry import DEFAULT_GPU_METRICS, TelemetryConfig, capture_idle_baseline

try:  # py3.11+
    from builtins import BaseExceptionGroup
except ImportError:  # py3.10 — no exception groups in the stdlib
    BaseExceptionGroup = ()  # type: ignore[assignment, misc]  # rebinds a stdlib type name on old pythons


def _add_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--mode",
        choices=["rest", "mcp"],
        default="rest",
        help="Target kind: 'rest' (any HTTP endpoint) or 'mcp' (any MCP server over "
        "streamable-http or SSE). Default: rest.",
    )
    p.add_argument(
        "--url",
        default="http://localhost:8000",
        help="REST mode: base URL of the API. MCP mode: the MCP endpoint URL — this is the "
        "ONLY URL an MCP target needs (connectivity and tool discovery happen over MCP).",
    )
    p.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Request header, repeatable (e.g. --header 'Authorization=Bearer ...'). "
        "Applies to REST requests or the MCP HTTP transport. Values are redacted in saved artifacts.",
    )
    p.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification.")
    # REST-specific
    p.add_argument("--method", default="GET", help="REST HTTP method (default GET).")
    p.add_argument(
        "--path",
        default="/api/datasets/{dataset}/search",
        help="REST path template; may contain {dataset}. Default keeps the MM RAG search "
        "endpoint so the original benchmark's REST mode carries over unchanged.",
    )
    p.add_argument(
        "--query-param",
        default="q",
        help="REST query-string parameter receiving each sampled query; pass an empty string "
        "to disable (body-only endpoints). Default: q.",
    )
    p.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra REST query parameter, repeatable (e.g. --param top_k=10 --param use_reranker=false).",
    )
    p.add_argument(
        "--body",
        default=None,
        help='REST JSON body template with a "{query}" placeholder placed inside quotes; '
        'e.g. \'{"query": "{query}", "top_k": 10}\'.',
    )
    p.add_argument("--body-file", default=None, help="Read the REST JSON body template from a file.")
    p.add_argument(
        "--health-path",
        default="/healthz",
        help="REST health probe path appended to --url ('' to skip). A failed probe aborts; "
        "an HTTP error status only warns (custom APIs may not expose /healthz). Default: /healthz.",
    )
    p.add_argument(
        "--dataset",
        default=None,
        help="REST: fills {dataset} in --path (auto-discovered from /api/datasets if omitted and "
        "needed). MCP: fills a dataset-ish required tool argument (dataset_name/dataset/collection/...).",
    )
    # MCP-specific
    p.add_argument(
        "--tool",
        default=None,
        help="MCP tool to call (auto-selected if the server exposes one obvious search-like tool).",
    )
    p.add_argument(
        "--list-tools",
        action="store_true",
        help="MCP: list the server's tools (with inferred query argument) and exit.",
    )
    p.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Fixed MCP tool argument, repeatable; values are JSON-parsed (--arg top_k=10 --arg use_reranker=true).",
    )
    p.add_argument(
        "--query-arg",
        default=None,
        help="MCP tool argument receiving the sampled query (default: auto-detected from the "
        "tool's input schema — 'query', 'q', 'prompt', 'text', ... or the first required string).",
    )
    p.add_argument(
        "--transport",
        choices=["streamable-http", "sse"],
        default="streamable-http",
        help="MCP transport (default: streamable-http).",
    )


def _add_load_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-N", "--N", dest="N", type=int, default=10, help="Concurrent simulated users (default 10).")
    p.add_argument(
        "--sweep",
        default=None,
        help="Comma-separated concurrency levels to run sequentially (e.g. 1,4,32,64). "
        "Overrides -N. Each level gets warm-up, its own timed window and GPU telemetry.",
    )
    p.add_argument("--duration", type=float, default=30.0, help="Seconds of load per level (default 30).")
    p.add_argument("--ramp-up", type=float, default=5.0, help="Ramp-up stagger across users in seconds (default 5).")
    p.add_argument(
        "--warmup-rounds",
        type=int,
        default=1,
        help="Unmeasured warm-up rounds per level (one round = N concurrent requests). 0 disables. Default 1.",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=30.0,
        help="Settle gap between sweep levels in seconds — GPU utilization decays asynchronously "
        "after load stops, and averaging through the decay understates the busy level (default 30).",
    )
    p.add_argument(
        "--call-timeout",
        type=float,
        default=120.0,
        help="Per-request timeout in seconds (default 120). Timed-out calls count as failures; "
        "this also keeps sweeps terminating near --duration when the server queues.",
    )
    p.add_argument("--seed", type=int, default=None, help="RNG seed for query sampling (default: derived per user).")
    p.add_argument("--queries-file", default=None, help="One query per line; '#' comments allowed.")
    p.add_argument(
        "--query-set",
        choices=["generic", "vlm", "mixed"],
        default="generic",
        help="Built-in query pool when --queries-file is not given (default generic).",
    )


def _add_prom_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--prom-url",
        default=os.environ.get("PROM_URL"),
        help="Prometheus base URL for GPU telemetry (or env PROM_URL). Omit to benchmark "
        "latency-only. In sweep mode the join is what turns latency curves into GPU scaling curves.",
    )
    p.add_argument(
        "--prom-selector",
        default="",
        help="Extra PromQL label matcher appended to every GPU metric, e.g. "
        "'exported_namespace=\"henkia\"' (DCGM k8s pod-mapping) or 'Hostname=\"pcai-se-scs04\"'.",
    )
    p.add_argument("--prom-step", default="15s", help="query_range step (default 15s ≈ DCGM scrape interval).")
    p.add_argument(
        "--gpu-metrics",
        default=",".join(DEFAULT_GPU_METRICS),
        help="Comma-separated DCGM metric names to average per GPU per level "
        "(default: GPU_UTIL, GR_ENGINE_ACTIVE, FB_USED, MEM_COPY_UTIL, POWER_USAGE).",
    )
    p.add_argument(
        "--extra-range-query",
        action="append",
        default=[],
        metavar="NAME=PROMQL",
        help="Additional range query aggregated per level (e.g. replica counts from kube-state-metrics), repeatable.",
    )
    p.add_argument(
        "--baseline-duration",
        type=float,
        default=0.0,
        help="Capture an idle GPU baseline for this many seconds before the sweep and report "
        "baseline-subtracted means — the contamination check for shared GPU nodes (default 0 = off; "
        "use 60-120 on shared clusters).",
    )
    p.add_argument(
        "--knee-factor",
        type=float,
        default=2.0,
        help="Knee-point detection factor: p99 latency at the knee >= factor x best level's p99 "
        "(or success rate < 99%%). Default 2.0.",
    )


def _add_output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--output", default=None, help="Write the full JSON run artifact (levels + telemetry + summary).")
    p.add_argument("--csv", default=None, help="Write a per-level CSV (client stats + GPU means).")
    p.add_argument(
        "--md",
        "--markdown",
        dest="md",
        default=None,
        help="Write the easy-to-read Markdown report (results/ tree convention — "
        "Status line, benchmark configuration, per-level scaling, error deep-dive, "
        "GPU telemetry). Ingested by results_to_html.py unchanged.",
    )
    p.add_argument(
        "--html",
        default=None,
        help="Write a self-contained HTML report (charts + tables; no external assets).",
    )
    p.add_argument("--log-file", default=None, help="Tee the full log to this file.")
    p.add_argument(
        "--note",
        action="append",
        default=[],
        metavar="TEXT",
        help="Free-text annotation stamped into every artifact (markdown config table, "
        "HTML header, JSON config) — e.g. serving config, cache policy, GPU type. Repeatable.",
    )
    p.add_argument(
        "--percentiles",
        default=None,
        metavar="P1,P2,...",
        help="Comma-separated latency percentiles to report (default 50,95,99). "
        "E.g. --percentiles 50,90,99. Every table (console, CSV, markdown, HTML) adapts.",
    )
    p.add_argument(
        "--from",
        dest="from_json",
        default=None,
        metavar="RUN.JSON",
        help="Re-render artifacts (--md/--html/--csv) from a previously saved JSON run "
        "artifact without re-running the load. Knee detection is re-applied with the "
        "current --knee-factor.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging (per-request lines).")
    p.add_argument("--quiet", action="store_true", help="WARNING logging only (progress table suppressed).")
    p.add_argument(
        "--progress-interval", type=float, default=5.0, help="Progress table interval in seconds (default 5)."
    )
    p.add_argument(
        "--http2",
        action="store_true",
        help="REST mode: use HTTP/2 (requires the 'h2' package). MCP transports negotiate their own.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="endpoint-benchmarker",
        description=(
            "Benchmark ANY REST endpoint or MCP server with N concurrent users looping over a "
            "reference query pool, and — with --sweep plus a Prometheus URL — produce a GPU "
            "scaling curve (per-level client stats joined with per-GPU DCGM telemetry). "
            "Generalizes MultimodalRAG/tests/benchmark.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # MM RAG REST parity: 50 users, 30s, auto-discovered dataset
  endpoint-benchmarker --url http://localhost:8000 --dataset my-ds -N 50

  # Any REST endpoint: POST JSON body, auth header, custom pool
  endpoint-benchmarker --url https://their-rag.example.com --mode rest \\
      --method POST --path /api/v1/answer \\
      --body '{"question": "{query}", "top_k": 5}' \\
      --header 'Authorization=Bearer eyJ...' \\
      --queries-file their_queries.txt -N 32 --duration 120

  # Any MCP server — ONE url, tool auto-discovered, args from the schema:
  endpoint-benchmarker --mode mcp --url https://rag.example.com/mcp \\
      --dataset their-dataset -N 32 --duration 120

  # MCP server, explicit tool + args:
  endpoint-benchmarker --mode mcp --url https://rag.example.com/mcp \\
      --tool search_dataset --arg top_k=10 --arg use_reranker=true -N 8

  # The hosted-trial GPU scaling run (Henkia-style): sweep 1->4->32->64 with
  # DCGM telemetry averaged over each level's window, namespaced to their pods
  endpoint-benchmarker --mode mcp --url https://rag.example.com/mcp \\
      --dataset their-dataset --sweep 1,4,32,64 --duration 180 --settle 30 \\
      --prom-url http://prometheus:9090 \\
      --prom-selector 'exported_namespace="their-ns"' \\
      --baseline-duration 120 \\
      --output scaling.json --csv scaling.csv --md report.md --html report.html \\
      --note 'warm-cache run' --percentiles 50,90,99

  # Re-render a saved run without re-running the load (e.g. new knee factor):
  endpoint-benchmarker --from scaling.json --md report.md --knee-factor 1.5
""",
    )
    _add_target_args(parser)
    _add_load_args(parser)
    _add_prom_args(parser)
    _add_output_args(parser)
    return parser


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def _print_config(
    args: argparse.Namespace, target_desc: dict, n_queries: int, query_label: str, levels: list[int]
) -> None:
    prom = args.prom_url or "(none — latency-only)"
    print()
    print("=" * 72)
    print("  ENDPOINT BENCHMARK CONFIGURATION")
    print("=" * 72)
    print(f"  Mode:           {args.mode.upper()}")
    print(f"  Target:         {target_desc.get('url')}")
    if args.mode == "rest":
        print(f"  Method:         {args.method}  (query param: '{args.query_param or '-'}')")
        if args.body:
            print(f"  Body template:  {args.body[:70]}{'…' if len(args.body) > 70 else ''}")
    else:
        print(f"  Transport:      {args.transport}")
        print(f"  Tool:           {target_desc.get('tool') or '(auto)'}")
    print(f"  Dataset:        {args.dataset or '(auto)'}")
    print(f"  Levels (N):     {','.join(map(str, levels))}")
    print(f"  Duration/level: {args.duration}s   ramp-up {args.ramp_up}s   warm-up {args.warmup_rounds} round(s)")
    print(f"  Settle:         {args.settle}s   call timeout {args.call_timeout}s")
    print(f"  Queries:        {n_queries} ({query_label})")
    print(f"  Prometheus:     {prom}")
    if args.prom_url:
        print(
            f"  Selector:       {args.prom_selector or '(none)'}   step {args.prom_step}   baseline {args.baseline_duration}s"
        )
    print(f"  TLS verify:     {'OFF (--insecure)' if args.insecure else 'ON'}")
    print("=" * 72)
    print()


async def _rest_health_check(target: RestTarget) -> None:
    """Connection errors abort; an HTTP error status only warns (generic APIs
    may not expose /healthz at all)."""
    if not target.health_path:
        log.info("Skipping REST health check (--health-path '')")
        return
    url = target.base_url.rstrip("/") + target.health_path
    log.info("Health check %s ...", url)
    try:
        async with httpx.AsyncClient(verify=not target.insecure, timeout=10) as client:
            resp = await client.get(url, headers=target.headers)
    except Exception as exc:
        print(f"Error: health check failed — {err_key(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc
    if 200 <= resp.status_code < 300:
        log.info("Health check OK")
    else:
        log.warning(
            "Health endpoint returned HTTP %s — continuing (pass --health-path '' to silence)", resp.status_code
        )


async def _discover_dataset(target: RestTarget) -> None:
    """mm-script parity: auto-discover the first dataset when {dataset} needs a value."""
    url = target.base_url.rstrip("/") + "/api/datasets"
    log.info("Discovering datasets at %s ...", url)
    try:
        async with httpx.AsyncClient(verify=not target.insecure, timeout=10) as client:
            resp = await client.get(url, headers=target.headers)
            resp.raise_for_status()
            names = [ds["name"] for ds in resp.json().get("datasets", [])]
    except Exception as exc:
        raise TargetError(
            f"--dataset is required but could not be auto-discovered from {url} ({err_key(exc)})"
        ) from exc
    if not names:
        raise TargetError(f"no datasets available on {url}; create one or pass --dataset explicitly")
    target.dataset = names[0]
    log.info("Auto-discovered dataset: '%s'", target.dataset)


def _parse_percentiles(spec: str | None) -> tuple[float, ...]:
    """Parse ``--percentiles 50,90,99`` into a validated tuple of floats."""
    if not spec:
        return DEFAULT_PERCENTILES
    parts = str(spec).split(",")
    if not spec.strip() or any(not part.strip() for part in parts):
        raise TargetError("--percentiles must be a comma-separated list of numbers (e.g. 50,90,99)")
    try:
        values = tuple(float(part.strip()) for part in parts)
    except ValueError:
        raise TargetError("--percentiles must be a comma-separated list of numbers (e.g. 50,90,99)") from None
    if any(not (0 < p < 100) for p in values):
        raise TargetError("--percentiles values must be between 0 and 100 (exclusive)")
    return tuple(sorted(set(values)))


def _rerender_from(args: argparse.Namespace) -> int:
    """``--from run.json``: rebuild md/html/csv artifacts from a saved run
    artifact — no load is generated. The knee point is re-detected from the
    stored per-level rows with the current --knee-factor, so a saved run can
    be re-read under a different sensitivity without re-running."""
    import json

    from .report import detect_knee, write_artifacts

    with open(args.from_json, encoding="utf-8") as f:
        payload = json.load(f)
    rows = (payload.get("summary") or {}).get("rows") or []
    if rows and any("lat_p99" in r or any(c.startswith("lat_p") for c in r) for r in rows):
        recomputed = detect_knee(rows, args.knee_factor)
        payload.setdefault("summary", {})["knee"] = recomputed
    title = f"Endpoint benchmark — {(payload.get('config') or {}).get('target', {}).get('url', '')}"
    if not (args.md or args.html or args.csv or args.output):
        print("Error: --from needs at least one of --md/--html/--csv/--output to write", file=sys.stderr)
        return 2
    write_artifacts(payload, csv_path=args.csv, html_path=args.html, md_path=args.md, title=title)
    # --output rewrites the JSON (useful to stamp the recomputed knee back).
    if args.output:
        import os

        from .report import write_json

        parent = os.path.dirname(os.path.abspath(args.output))
        if parent:
            os.makedirs(parent, exist_ok=True)
        write_json(args.output, payload)
    return 0


async def run(args: argparse.Namespace) -> int:
    started_utc = datetime.now(UTC).isoformat()
    log.info("endpoint-benchmarker %s on Python %s", __version__, platform.python_version())
    percentiles = _parse_percentiles(args.percentiles)

    headers = parse_kv(args.header, "--header")

    # -- build target (shape checks that don't need the network run later) --
    if args.mode == "rest":
        rest_target = RestTarget(
            base_url=args.url,
            path_template=args.path,
            method=args.method.upper(),
            query_param=args.query_param,
            params=parse_kv(args.param, "--param"),
            body_template=args.body,
            headers=headers,
            dataset=args.dataset,
            health_path=args.health_path,
            http2=args.http2,
            insecure=args.insecure,
        )
        if args.body_file:
            with open(args.body_file, encoding="utf-8") as f:
                rest_target.body_template = f.read()
        target: RestTarget | McpTarget = rest_target
    else:
        target = McpTarget(
            url=args.url,
            transport=args.transport,
            tool_name=args.tool,
            args=parse_kv(args.arg, "--arg"),
            query_arg=args.query_arg,
            dataset=args.dataset,
            headers=headers,
            insecure=args.insecure,
        )

    # -- queries -------------------------------------------------------------
    queries, query_label = load_queries(args.queries_file, args.query_set)

    # -- mode-specific pre-flight -------------------------------------------
    from .mcp_driver import resolve_mcp_target

    resolved: ResolvedTool | None = None
    if args.mode == "rest":
        assert isinstance(target, RestTarget)  # narrowed: constructed as RestTarget when mode == "rest"
        if target.health_path:
            await _rest_health_check(target)
        # Auto-discovery (mm parity) must run BEFORE validation, which checks
        # that every {dataset} placeholder has a value.
        if "{dataset}" in target.path_template and not target.dataset:
            await _discover_dataset(target)
        target.validate()
    else:
        assert isinstance(target, McpTarget)  # narrowed: constructed as McpTarget otherwise
        result = await resolve_mcp_target(target, list_only=args.list_tools)
        if args.list_tools:
            return 0
        assert result is not None  # resolve_mcp_target returns None only on the --list-tools path
        resolved = result[0]
        # Record what was actually used in the printed config + artifacts.
        target.tool_name = resolved.tool_name
        target.query_arg = resolved.query_arg

    target_desc = target.describe()
    levels = _resolve_levels(args)
    _print_config(args, target_desc, len(queries), query_label, levels)

    # -- telemetry -------------------------------------------------------------
    telemetry_config: TelemetryConfig | None = None
    if args.prom_url:
        extra = parse_kv(args.extra_range_query, "--extra-range-query")
        telemetry_config = TelemetryConfig(
            prom_url=args.prom_url,
            selector=args.prom_selector,
            step=args.prom_step,
            gpu_metrics=tuple(m.strip() for m in args.gpu_metrics.split(",") if m.strip()),
            extra_queries=extra,
            baseline_duration=args.baseline_duration,
            insecure=args.insecure,
        )
        try:
            telemetry_config.validate()
        except ValueError as exc:
            raise TargetError(str(exc)) from exc
    elif args.sweep:
        log.info("No --prom-url given — running a latency-only scaling curve (no GPU telemetry)")

    baseline = None
    if telemetry_config and telemetry_config.baseline_duration > 0:
        baseline = await capture_idle_baseline(telemetry_config, telemetry_config.baseline_duration)

    # -- sweep -------------------------------------------------------------------
    results = await run_sweep(
        levels,
        duration=args.duration,
        ramp_up=args.ramp_up,
        warmup_rounds=args.warmup_rounds,
        settle=args.settle,
        call_timeout=args.call_timeout,
        seed=args.seed,
        progress_interval=args.progress_interval,
        queries=queries,
        rest_target=target if isinstance(target, RestTarget) else None,
        mcp_target=target if isinstance(target, McpTarget) else None,
        resolved=resolved,
        telemetry_config=telemetry_config,
        baseline=baseline,
    )

    # -- summarize + artifacts -----------------------------------------------------
    run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
    config = {
        "mode": args.mode,
        "target": target_desc,
        "load": {
            "levels": levels,
            "duration_s": args.duration,
            "ramp_up_s": args.ramp_up,
            "warmup_rounds": args.warmup_rounds,
            "settle_s": args.settle,
            "call_timeout_s": args.call_timeout,
            "seed": args.seed,
            "queries": {"count": len(queries), "source": query_label},
        },
        "telemetry": (
            {
                "prom_url": telemetry_config.prom_url,
                "selector": telemetry_config.selector,
                "step": telemetry_config.step,
                "gpu_metrics": list(telemetry_config.gpu_metrics),
                "extra_queries": telemetry_config.extra_queries,
                "baseline_duration_s": telemetry_config.baseline_duration,
            }
            if telemetry_config
            else None
        ),
        "tool_version": __version__,
    }
    payload = build_run_payload(
        results,
        config,
        run_id,
        started_utc,
        args.knee_factor,
        baseline,
        percentiles=percentiles,
        annotations=args.note,
    )
    print_results(payload)

    if not (args.output or args.csv or args.html or args.md):
        log.info("No artifact paths given — pass --md/--html/--csv/--output to save the run")
    write_artifacts(
        payload,
        json_path=args.output,
        csv_path=args.csv,
        html_path=args.html,
        md_path=args.md,
        title=f"Endpoint benchmark — {target_desc.get('url', '')}",
    )

    total_success = sum(r.stats.success for r in results)
    return 0 if total_success > 0 else 1


def _resolve_levels(args: argparse.Namespace) -> list[int]:
    if not args.sweep:
        return [args.N]
    try:
        levels = [int(x) for x in str(args.sweep).split(",") if x.strip()]
    except ValueError:
        raise TargetError("--sweep must be a comma-separated list of ints (e.g. 1,4,32,64)") from None
    if not levels or any(n < 1 for n in levels):
        raise TargetError("--sweep levels must be integers >= 1")
    if levels != sorted(levels):
        log.info("Note: --sweep levels are not ascending; running in the order given")
    return levels


def _first_exception(group) -> BaseException:
    """Flatten an ExceptionGroup (anyio TaskGroups wrap everything) to its first leaf."""
    stack = [group]
    while stack:
        item = stack.pop(0)
        if isinstance(item, BaseExceptionGroup):
            stack.extend(item.exceptions)
        else:
            return item
    return group


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.from_json:
        # Re-render mode: no target/load args are required or used.
        setup_logging(args.verbose, args.quiet, args.log_file)
        try:
            return _rerender_from(args)
        except FileNotFoundError as exc:
            print(f"Error: run artifact not found — {exc.filename}", file=sys.stderr)
            return 2
        except Exception as exc:
            if args.verbose:
                raise
            print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    if args.N < 1:
        parser.error("-N must be at least 1")
    if args.duration < 0.1:
        parser.error("--duration must be >= 0.1s")
    if args.warmup_rounds < 0:
        parser.error("--warmup-rounds must be >= 0")
    if args.settle < 0:
        parser.error("--settle must be >= 0")
    if args.call_timeout < 0.1:
        parser.error("--call-timeout must be >= 0.1s")

    setup_logging(args.verbose, args.quiet, args.log_file)
    try:
        return asyncio.run(run(args))
    except TargetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except BaseExceptionGroup as exc:
        first = _first_exception(exc)
        print(f"Error: {type(first).__name__}: {first}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        if args.verbose:
            raise
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        log.debug("Traceback:", exc_info=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
