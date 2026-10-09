#!/usr/bin/env python3
"""Sequential chat-benchmark runner for the ModelBenchmarker webapp (MB-SEQ).

Launches chat benchmarks ONE AT A TIME against the same endpoint, waiting for
each to finish before starting the next, so no two sweeps ever contend for the
backend's compute and TTFT/TPS numbers stay clean. The webapp already refuses
overlapping runs per endpoint (BusyEndpointError -> 409, keyed on the resolved
origin); this client's job is only to serialize launches and to stop on
failure unless told otherwise.

Usage:
  model-benchmarker-seq <plan.json> [options]

Plan format (JSON; "runs" is ordered, items are POST /api/runs bodies):
  {
    "runs": [
      {
        "name": "ctx0",                          # optional label, shown in output
        "target": "http://deepseek-...svc.cluster.local:80",
        "params": {                              # same params the chat launcher
          "number_users": "1,4,32,64",           #   page collects: number_users,
          "requests_per_user": 5,                #   requests_per_user, tasks,
          "context_length": "0",                 #   arrival_mode, multiturn,
          "tasks": "coding,creative",            #   no_nonce, prewarm,
          "separate_tasks": true,                #   separate_tasks, max_tokens,
          "model": "deepseek-v41-flash"          #   temperature, api_key, ...
        }
      },
      { "name": "ctx32k", "target": "http://...", "params": { ... } }
    ],
    "settle_seconds": 30                         # optional, default 0
  }

Why split + sequence instead of one big sweep:
  * Each run's wall clock stays far under the webapp's BENCH_MAX_RUN_SECONDS
    hard stop (default 7200 s), so a long matrix cannot die at the 2 h
    SIGTERM with status "timeout".
  * Partial results are preserved either way (report.md is rewritten after
    every level), but per-run reports keep levels grouped the way you'd want
    to compare them (per context length / per task).
  * A failed or timed-out run stops the sequence immediately (use --keep-going
    to override) so you never benchmark against a saturated or broken server.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_POLL_SECONDS = 10.0
DEFAULT_SETTLE_SECONDS = 0.0
TERMINAL = {"success", "failed", "cancelled", "timeout", "interrupted"}

USAGE = "model-benchmarker-seq <plan.json> [--api-key KEY | MB_API_KEY env]"


def _request(
    method: str,
    url: str,
    api_key: str,
    body: dict | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict | list | str]:
    payload = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("X-API-Key", api_key)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # unreachable server: no HTTP status to report, fail the caller's
        # launch path cleanly (polling retries instead — see poll_until_done)
        raise ConnectionError(f"{method} {url} unreachable: {type(exc).__name__}: {exc}") from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = raw
    return status, parsed


def _fail_detail(parsed: dict | list | str) -> str:
    if isinstance(parsed, dict):
        return str(parsed.get("detail") or parsed)
    return str(parsed)


def load_plan(path: str) -> dict:
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read plan: {exc}") from None
    except ValueError as exc:
        raise SystemExit(f"plan is not valid JSON: {exc}") from None
    if not isinstance(plan, dict) or not isinstance(plan.get("runs"), list) or not plan["runs"]:
        raise SystemExit("plan must be a JSON object with a non-empty 'runs' array")
    for i, entry in enumerate(plan["runs"]):
        if not isinstance(entry, dict) or not entry.get("target") or not isinstance(entry.get("params"), dict):
            raise SystemExit(
                f"runs[{i}] must be an object with 'target' (string) and 'params' (object) -- "
                "the same params the /benchmark/chat page collects"
            )
    return plan


def launch_one(base_url: str, entry: dict, api_key: str) -> str:
    """POST one run; exits with a self-describing message on 409 (busy)."""
    url = f"{base_url.rstrip('/')}/api/runs"
    body = {"kind": "chat", "target": entry["target"], "params": entry["params"]}
    try:
        status, detail = _request("POST", url, api_key, body=body)
    except ConnectionError as exc:
        raise SystemExit(f"webapp unreachable: {exc}\n  check --base-url (and that the app pod is up).") from None
    if status == 409:
        raise SystemExit(
            f"endpoint busy: {_fail_detail(detail)}\n"
            "  wait for the in-flight run to finish (or cancel it), then re-run the sequencer."
        )
    if status != 202:
        raise SystemExit(f"launch HTTP {status}: {_fail_detail(detail)}")
    run_id = detail.get("run_id") if isinstance(detail, dict) else None
    if not run_id:
        raise SystemExit(f"launch response missing run_id: {detail!r}")
    return str(run_id)


def poll_until_done(
    base_url: str,
    run_id: str,
    api_key: str,
    poll_seconds: float,
    label: str,
) -> tuple[str, dict]:
    """Poll GET /api/runs/{run_id} until a terminal status.

    Transient network errors never kill a live run: they are logged and
    retried until the run reports a terminal status (Ctrl-C exits).
    """
    url = f"{base_url.rstrip('/')}/api/runs/{run_id}"
    while True:
        try:
            status, detail = _request("GET", url, api_key)
            if status == 200 and isinstance(detail, dict):
                state = str(detail.get("status") or "running")
                if state in TERMINAL:
                    return state, detail
            elif status != 200:
                print(f"  [{label}] poll HTTP {status}: {_fail_detail(detail)}", flush=True)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  [{label}] poll retry ({type(exc).__name__}: {exc})", flush=True)
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="model-benchmarker-seq",
        description="Sequential chat-benchmark runner for the ModelBenchmarker webapp",
    )
    parser.add_argument("plan", help="JSON plan file (see module docstring)")
    parser.add_argument(
        "--base-url", default="http://localhost:8000", help="webapp base URL (default http://localhost:8000)"
    )
    parser.add_argument("--api-key", default="", help="webapp API key (falls back to $MB_API_KEY)")
    parser.add_argument(
        "--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="status poll interval (default 10)"
    )
    parser.add_argument(
        "--settle-seconds", type=float, default=None, help="override the plan's settle_seconds (sleep between runs)"
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="continue with the next run after a failure instead of stopping the sequence",
    )
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("MB_API_KEY", "")
    plan = load_plan(args.plan)
    runs: list[dict] = plan["runs"]
    settle = float(plan.get("settle_seconds") or 0.0)
    if args.settle_seconds is not None:
        settle = args.settle_seconds

    print(f"Sequencing {len(runs)} run(s) against {args.base_url} (settle {settle:g}s between runs)")
    failures: list[str] = []
    for i, entry in enumerate(runs, start=1):
        label = str(entry.get("name") or f"run{i}")
        print(f"\n=== [{i}/{len(runs)}] {label} ===", flush=True)
        try:
            run_id = launch_one(args.base_url, entry, api_key)
        except SystemExit as exc:
            print(f"ERROR: {exc}")
            failures.append(label)
            if not args.keep_going:
                print("stopping sequence (--keep-going to continue past failures)")
                raise SystemExit(1) from None
            continue
        print(f"  launched {run_id}; polling every {args.poll_seconds:g}s...", flush=True)
        state, rec = poll_until_done(args.base_url, run_id, api_key, args.poll_seconds, label)
        print(f"  finished: status={state} exit_code={rec.get('exit_code')}", flush=True)
        if state != "success":
            failures.append(label)
            if not args.keep_going:
                print("stopping sequence (--keep-going to continue past failures)")
                raise SystemExit(1) from None
        if i < len(runs) and settle > 0:
            print(f"  settling {settle:g}s...", flush=True)
            time.sleep(settle)

    if failures:
        print(f"\nSequence finished WITH FAILURES: {', '.join(failures)}")
        raise SystemExit(1)
    print("\nSequence finished: all runs succeeded")


if __name__ == "__main__":
    main()
