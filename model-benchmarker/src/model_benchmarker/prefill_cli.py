"""CLI: infer prefill throughput from captured SGLang logs (MB-PREFILL).

Reads log lines from a file (one line per record, e.g. a `kubectl logs` dump or
the JSON list produced by a log search) and writes the Markdown report plus an
optional log-format reconstruction.

    python -m model_benchmarker.prefill_cli logs.txt \
        --out results/prefill_inferred/H200x4_32k_logs.md \
        --log-out results/prefill_inferred/H200x4_32k_logs.log \
        --namespace project-user-andrew-bydlon
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .prefill_infer import find_bursts, format_like_log, infer_prefill, parse_prefill_lines, split_runs
from .prefill_report import write_report


def _read_lines(path: Path) -> list[str]:
    """Read log lines from a plain-text dump or a JSON array of records."""
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            loaded = json.loads(stripped)
        except json.JSONDecodeError:
            return text.splitlines()
        if isinstance(loaded, list):
            return [str(x) for x in loaded]
    return text.splitlines()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Infer prefill throughput from SGLang logs.")
    p.add_argument("logs", help="Log dump (plain lines or a JSON array of records).")
    p.add_argument("--out", required=True, help="Markdown report path.")
    p.add_argument("--log-out", default="", help="Optional SGLang-shaped reconstruction path.")
    p.add_argument("--namespace", default="", help="Namespace the logs came from (provenance).")
    p.add_argument("--source", default="", help="Source description (provenance).")
    p.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing report even when this window yields no bursts "
        "(the log buffer may have rolled over since the measurement).",
    )
    args = p.parse_args(argv)

    src = Path(args.logs)
    if not src.is_file():
        print(f"error: no such log file: {src}", file=sys.stderr)
        return 2
    lines = _read_lines(src)
    inferred = infer_prefill(lines)

    source = args.source or f"{src.name}"
    bursts = [(pod["pod"], b) for pod in inferred["pods"] for b in pod.get("bursts", [])]

    # Log buffers roll over.  Re-running against a window that has since aged out
    # would silently replace a real measurement with "no bursts found", so an
    # empty result never overwrites an existing non-empty artifact unless the
    # caller insists with --force.
    existing = Path(args.out)
    if not bursts and existing.is_file() and existing.stat().st_size > 0 and not args.force:
        print(
            f"refusing to overwrite {existing} with an empty result "
            f"(no bursts in this window; pass --force to replace anyway)",
            file=sys.stderr,
        )
        return 3

    dest = write_report(inferred, args.out, source=source, namespace=args.namespace)
    print(f"wrote {dest} ({len(bursts)} burst(s) across {len(inferred['pods'])} pod(s))")

    if args.log_out:
        out_lines: list[str] = []
        steps = parse_prefill_lines(lines)
        for pod in inferred["pods"]:
            pod_steps = [s for s in steps if s.pod == pod["pod"]]
            for b in find_bursts(split_runs(pod_steps)):
                out_lines.append(
                    f"# burst {b.start} -> {b.end}  ({b.tokens:,} tokens, agg {b.aggregate_tps:,.0f} tok/s)"
                )
                out_lines.extend(format_like_log(b))
                out_lines.append("")
        log_path = Path(args.log_out)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(out_lines), encoding="utf-8")
        print(f"wrote {log_path}")

    if not bursts:
        print("note: no prefill bursts found — the log window may be too short or idle.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
