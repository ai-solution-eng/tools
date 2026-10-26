"""Render inferred prefill throughput as a results artifact (MB-PREFILL).

Takes the JSON produced by prefill_infer.infer_prefill and writes a Markdown
report in the same shape as a benchmark_chat run file: a configuration bullet
list, a Status line, then the measurement table.  The measurement itself comes
from the serving engine own accounting (see prefill_infer), not from
client-side TTFT, so the provenance says so explicitly rather than implying a
benchmark_chat sweep produced these numbers.
"""

from __future__ import annotations

from pathlib import Path


def render_report(inferred: dict, *, source: str, namespace: str) -> str:
    """Render one inferred-prefill Markdown report."""
    cov = inferred.get("covered") or {}
    lines = [
        "# Prefill throughput (inferred from serving logs)",
        "",
        f"- Source: `{source}` (SGLang `Prefill batch` lines, `kserve-container`)",
        f"- Namespace: `{namespace}`",
        f"- Log window covered: {cov.get('first')} -> {cov.get('last')}",
        "- MODE: inference - values are the engine own `input throughput (token/s)`",
        "  and token counters, NOT client-measured TTFT.",
        "- Aggregate tok/s = sum(#new-token) / wall-clock span of the burst.",
        "- Steady tok/s excludes each burst first chunk (HiCache load, not prefill).",
        "",
        "`#new-token` counts only newly-computed tokens; tokens served from the prefix",
        "cache (`#cached-token`) are attended over but not recomputed, so they do not",
        "appear in the rate.  A burst is a continuous drain of a queued backlog",
        "(#pending-token >= 100k) - i.e. the engine actually had work batched.",
        "",
        "`#pending-token` is the GLOBAL backlog, not a per-request counter, so the log",
        "does not reveal how many requests were in flight or their individual sizes.",
        "The `~32k reqs` column is a scale figure (tokens / 32768), not a count.",
        "",
    ]
    pods = inferred.get("pods") or []
    bursts = [(p["pod"], b) for p in pods for b in p.get("bursts", [])]
    if not bursts:
        lines += ["Status: no prefill bursts found in this log window.", ""]
        return "\n".join(lines)

    lines += [
        "| pod | window | tokens | chunks | chunk size | wall (s) | aggregate tok/s | steady tok/s | ~32k reqs |",
        "|:---|:---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for pod, b in bursts:
        short = pod.split("-predictor")[0]
        lines.append(
            f"| {short} | {b['start'][11:]} -> {b['end'][11:]} | {b['tokens']:,} | "
            f"{b['chunks']} | {b['chunk_size']:,} | {b['wall_seconds']:.0f} | "
            f"{b['aggregate_tps']:,.0f} | {b['steady_tps']:,.0f} | {b['equivalent_32k_requests']:.1f} |"
        )
    lines += ["", "## Coverage", ""]
    for p in pods:
        short = p["pod"].split("-predictor")[0]
        lines.append(
            f"- `{short}`: {p['steps']} prefill steps, {len(p.get('bursts', []))} burst(s), {p['first']} -> {p['last']}"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(inferred: dict, dest: str | Path, *, source: str, namespace: str) -> Path:
    """Render and write the report atomically; returns the written path."""
    path = Path(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_report(inferred, source=source, namespace=namespace)
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path
