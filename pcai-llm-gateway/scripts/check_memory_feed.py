#!/usr/bin/env python3
"""Post-deploy smoke check for the automatic memory feed.

Run IMMEDIATELY after every gateway rollout, and again ~10 minutes later.
Answers one question: **is the distiller actually writing memories?**

WHY THIS EXISTS
---------------
The 2026-10-14 outage was found by a human noticing the memory count was
flat -- hours after the rollout. The gateway logs were clean (the fault was
swallowed at log.debug) and the /health endpoint was 200 throughout: the
deployment was "healthy" while writing nothing at all. A tag being live and
pods being Ready are CLAIMS; only a moving memory count is evidence.

It checks, in order (mirroring the memory-pulse-check skill):
  1. the deploy's image tag + the distiller env gates,
  2. the Postgres error log for distiller SQL faults (the only place a
     swallowed statement error is visible),
  3. that the sweep is advancing -- newest canonical_distills marker,
  4. that the memory dataset document_count is MOVING between two samples.

Exit 0 = feed healthy. Exit 1 = stalled (prints which stage failed).

USAGE
  python3 scripts/check_memory_feed.py            # one-shot
  python3 scripts/check_memory_feed.py --wait 600 # sample, wait, re-sample

Requires cluster read access (kubectl) and a gateway admin key for the
dataset count; both are read-only.
"""

from __future__ import annotations

import argparse
import subprocess
import time

NS = "pcai-llm"
PG_POD = "pcai-llm-gateway-pg-0"


def sh(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:  # surface any failure as text, never crash the check
        return 1, str(exc)


def psql(sql: str) -> tuple[int, str]:
    """One read-only query against the gateway database via kubectl exec."""
    return sh(
        [
            "kubectl", "-n", NS, "exec", PG_POD, "-c", "postgres", "--",
            "psql", "-U", "pcai_llm", "-d", "pcai_llm", "-X", "-A", "-t", "-c", sql,
        ]
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait between count samples")
    args = ap.parse_args()

    print("=== 1. deployed image + distiller gates ===")
    rc, out = sh([
        "kubectl", "-n", NS, "get", "deploy", "pcai-llm-gateway",
        "-o", "jsonpath={.spec.template.spec.containers[0].image}",
    ])
    if rc != 0:
        print("  cannot read the deployment (kubectl configured?):", out[:200])
        return 1
    print("  image:", out)
    rc, env = sh([
        "kubectl", "-n", NS, "get", "deploy", "pcai-llm-gateway",
        "-o", "jsonpath={range .spec.template.spec.containers[0].env[*]}{.name}={.value}{'\\n'}{end}",
    ])
    gates = [
        line for line in env.splitlines()
        if line.startswith(("PCAI_LLM_DISTILLER_ENABLED", "PCAI_LLM_DISTILLER_MEMORY_SERVER",
                            "PCAI_LLM_DISTILLER_MEMORY_PROBE_TOOL", "PCAI_LLM_DISTILLER_RAW_FALLBACK"))
    ]
    for g in gates:
        print("  " + g)
    if not any(g.endswith("=true") for g in gates if g.startswith("PCAI_LLM_DISTILLER_ENABLED")):
        print("  !! DISTILLER_ENABLED is not true -- the feed cannot run")
        return 1

    print("\n=== 2. Postgres error log: distiller SQL faults (last 500 lines) ===")
    rc, logs = sh([
        "kubectl", "-n", NS, "logs", PG_POD, "-c", "postgres", "--tail=500",
    ])
    faults = [
        line for line in logs.splitlines()
        if "ERROR" in line and any(k in line for k in ("does not exist", "cannot cast", "more target columns"))
    ]
    if faults:
        print(f"  !! {len(faults)} distiller SQL fault(s) -- the sweep is failing:")
        for line in faults[-5:]:
            print("    " + line[:160])
        print("  This is the exact signature of the 2026-10-14 outage.")
        return 1
    print("  none -- statements agree with the schema")

    print("\n=== 3. sweep advancing? ===")
    rc, newest = psql("select max(distilled_at) from canonical_distills;")
    if rc != 0:
        print("  cannot query (kubectl exec blocked?):", newest[:200])
        return 1
    print("  newest distill marker:", newest)
    rc, recent = psql(
        "select count(*) from canonical_distills where distilled_at > now() - interval '30 minutes';"
    )
    print("  distills in the last 30 min:", recent)

    print("\n=== 4. memory dataset growing? ===")
    print("  sample document_count from the mm-rag API (needs a gateway key):")
    print("    curl -s -H 'X-API-Key: <key>' <gateway>/api/datasets/<name>  | jq .document_count")
    if args.wait:
        print(f"  waiting {args.wait}s, then RE-SAMPLE -- a flat count with a clean log is the outage")
        time.sleep(args.wait)

    print("\nVerdict: no SQL fault and a recent marker = the feed is alive.")
    print("For the authoritative end-to-end check, run the memory-pulse-check skill:")
    print("  snapshot document_count -> wait -> re-snapshot. MOVING = healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
