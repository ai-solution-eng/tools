"""Markdown rendering for memory-estimate artifacts.

The artifact follows the results-tree convention (H1, ``Status:`` line,
``##`` sections with 2-cell ``| key | value |`` tables) plus a wide
``## Capacity grid`` table that the chat-row parser deliberately rejects
(non-numeric first cell). A ``tool: memory-estimate`` config row is what
``results_to_html.py`` uses to route the file into the Memory tab.
"""

from __future__ import annotations

from typing import Any


def _esc_cell(v: str) -> str:
    """Escape pipes so a value can never add table columns."""
    return str(v).replace("|", "\\|")


def _kv_table(rows: list[tuple[str, str]]) -> list[str]:
    out = ["| Key | Value |", "|---|---|"]
    out += [f"| {_esc_cell(k)} | {_esc_cell(v)} |" for k, v in rows]
    return out


def render_markdown(p: dict[str, Any]) -> str:
    """Render the payload built by the CLI into the artifact markdown."""
    lines: list[str] = [
        f"# Memory estimate: {p['model']}",
        "",
        f"Generated {p['generated']}",
        "",
    ]
    if p.get("status"):
        lines += [f"Status: {p['status']}", ""]

    lines += ["## Memory configuration", ""]
    lines += _kv_table(p["config"])
    lines += [""]

    lines += ["## Model structure", ""]
    lines += _kv_table(p["structure"])
    lines += [""]

    if p.get("scenarios"):
        lines += ["## Deployment scenarios", ""]
        lines += _kv_table([(s["label"], s["verdict"]) for s in p["scenarios"]])
        lines += [""]

    grid = p.get("grid") or {}
    if grid.get("rows"):
        lines += ["## Capacity grid", ""]
        lines += [
            ("Concurrent requests by context length (cell = max concurrent N-token requests; blank = does not fit):"),
            "",
        ]
        header = ["Deployment", "KV pool (tokens)"] + [
            f"{int(c) // 1024}k" if int(c) % 1024 == 0 else str(c) for c in grid["contexts"]
        ]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for row in grid["rows"]:
            cells = [row["label"], row.get("kv", "-")] + [str(c) if c is not None else "-" for c in row["cells"]]
            lines.append("| " + " | ".join(_esc_cell(c) for c in cells) + " |")
        lines += [""]

    warns = p.get("warnings") or []
    if warns:
        lines += ["## Warnings & notes", ""]
        lines += [f"- {w}" for w in warns]
        lines += [""]

    lines += ["## Notes", ""]
    lines += [
        (
            "- KV per-token cost is the conservative paged-pool footprint: every token in every "
            "KV layer. Sliding-window reuse and sparse selection only free memory at runtime."
        ),
        (
            "- Weights use the checkpoint's actual parameter count (all MoE experts are resident "
            "even though only a few route per token)."
        ),
        (
            "- Activation/CUDA-graph overhead is the `overhead` knob above; calibrate against a "
            "live server's reported KV pool when available."
        ),
    ]
    return "\n".join(lines) + "\n"
