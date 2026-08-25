#!/usr/bin/env python3
"""Concurrency load test for a SQLhandler (or any streamable-HTTP) MCP server.

Loads N concurrent tools/call requests and reports wall time, throughput (qps)
and p50/p95 per-call latency per concurrency level. Stdlib only.

Examples:
  python bench/concurrency.py --url http://127.0.0.1:9097/mcp --tool run_sql \
      --queries "SELECT * FROM work_order_header;SELECT * FROM work_order_note_recent" \
      --levels 1,2,4,8 --warmup 4

  python bench/concurrency.py --url https://sqlhandler.<domain>/mcp --bearer "$TOKEN" \
      --tool describe_table --tables work_order_header,work_order_note_recent
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.request

DEFAULT_QUERIES = [
    "SELECT COUNT(*) AS c FROM work_order_header",
    "SELECT COUNT(*) AS c FROM work_order_note_recent",
    "SELECT COUNT(*) AS c FROM work_order_labour",
    "SELECT COUNT(*) AS c FROM work_order_parts",
]


def build_jobs(args):
    """Return a pool of (tool_name, arguments) jobs."""
    if args.tool == "describe_table":
        tables = [t.strip() for t in (args.tables or "").split(",") if t.strip()]
        if not tables:
            raise SystemExit("describe_table requires --tables")
        return [("describe_table", {"table": t}) for t in tables]
    if args.tool == "scan_table":
        tables = [t.strip() for t in (args.tables or "").split(",") if t.strip()]
        if not tables:
            raise SystemExit("scan_table requires --tables")
        return [("scan_table", {"table": t, "limit": args.limit}) for t in tables]
    queries = [q.strip() for q in (args.queries or "").split(";") if q.strip()] or DEFAULT_QUERIES
    return [("run_sql", {"sql": q}) for q in queries]


def call(url, bearer, job, timeout):
    """One tools/call request; returns (seconds, ok)."""
    name, args = job
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 0, "method": "tools/call",
         "params": {"name": name, "arguments": args}}
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=body, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            dt = time.monotonic() - t0
            res = data.get("result") or {}
            ok = bool(res.get("content")) and "error" not in data
            return dt, ok
    except Exception as exc:
        return time.monotonic() - t0, "ERR:" + str(exc)[:80]


def run_level(url, bearer, jobs, level, reps, timeout):
    """Run N concurrent calls for each batch; report stats."""
    all_ms = []
    ok = 0
    walls = []
    for _ in range(reps):
        batch = [jobs[i % len(jobs)] for i in range(level)]
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=level) as ex:
            results = list(ex.map(lambda j: call(url, bearer, j, timeout), batch))
        walls.append(time.monotonic() - t0)
        all_ms.extend(r[0] * 1000 for r in results)
        ok += sum(1 for r in results if r[1] is True)
    total_calls = len(all_ms)
    total_time = sum(walls)
    ms = sorted(all_ms)
    pct = lambda q: ms[min(len(ms) - 1, int(len(ms) * q))]
    return {
        "level": level,
        "calls": total_calls,
        "ok": ok,
        "wall_ms": round((sum(walls) / len(walls)) * 1000) if walls else 0,
        "qps": round(total_calls / total_time, 2) if total_time else 0.0,
        "p50_ms": round(pct(0.5)),
        "p95_ms": round(pct(0.95)),
        "min_ms": round(ms[0]) if ms else 0,
        "max_ms": round(ms[-1]) if ms else 0,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="MCP streamable-http endpoint")
    parser.add_argument("--bearer", default=os.environ.get("MCP_BEARER_TOKEN", ""), help="Bearer token")
    parser.add_argument("--tool", default="run_sql", choices=["run_sql", "scan_table", "describe_table"])
    parser.add_argument("--queries", default="", help=";-separated SQL (run_sql only)")
    parser.add_argument("--tables", default="", help="comma-separated table names (describe/scan)")
    parser.add_argument("--levels", default="1,2,4,8", help="comma-separated concurrency levels")
    parser.add_argument("--warmup", type=int, default=4, help="sequential runs per job")
    parser.add_argument("--reps", type=int, default=1, help="batches per level")
    parser.add_argument("--timeout", type=int, default=300, help="per-request timeout seconds")
    parser.add_argument("--limit", type=int, default=50, help="scan_table row limit")
    args = parser.parse_args(argv)

    jobs = build_jobs(args)
    print(f"endpoint: {args.url}   tool: {args.tool}   pool: {len(jobs)} job(s)", file=sys.stderr)
    if args.warmup > 0:
        print(f"warming {args.warmup} run(s) per job...", file=sys.stderr)
        for _ in range(args.warmup):
            for j in jobs:
                call(args.url, args.bearer, j, args.timeout)

    print()
    print("| concurrency | calls | ok | wall | qps | p50 | p95 | min | max |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for level in [int(x) for x in args.levels.split(",") if x.strip()]:
        st = run_level(args.url, args.bearer, jobs, level, args.reps, args.timeout)
        print(f"| {st['level']} | {st['calls']} | {st['ok']} | {st['wall_ms']} ms | {st['qps']} | {st['p50_ms']} ms | {st['p95_ms']} ms | {st['min_ms']} ms | {st['max_ms']} ms |")
    return 0


if __name__ == "__main__":
    sys.exit(main())