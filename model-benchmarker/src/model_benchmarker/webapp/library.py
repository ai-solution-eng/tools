"""The results library: the committed results/ tree (git history of serving
setups) surfaced beside the PVC run history on /results.

The webapp's run registry (runner.py) only knows runs launched through the
app (BENCH_WORK_DIR/runs/<id>/). The repo's results/ tree — the committed
per-(model x setup) benchmark artifacts with the filename-encoded setup
(H200_sglang_dflash2_hicachex3_replicasx3.md) — is a DIFFERENT history with
different value: those are the curated baselines. This module scans that
tree with the SAME parsers the HTML report uses (results_to_html) and
normalizes entries into the compare flow's shape, so a fresh PVC run can be
compared against a committed baseline directly.

Library ids are prefixed ``lib:`` (``lib:<model-slug>/<stem>``) and are
read-only by construction — there is no write path here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

LIB_PREFIX = "lib:"
RESULTS_DIR_ENV = "BENCH_RESULTS_DIR"

# setup tokens -> the params_label chips shown in the UI (composed here so
# the endpoint, not the page JS, owns the rendering of library metadata)


def results_root() -> Path | None:
    """The committed results/ tree: BENCH_RESULTS_DIR override, else the
    repo-root results/ beside the package (dev tree). None when absent --
    deployed images carry no results/ tree and the page hides the section.
    An EXPLICIT override that does not exist disables the library (the
    operator turned it off; falling back to the image's copy would ignore
    them)."""
    env = (os.environ.get(RESULTS_DIR_ENV) or "").strip()
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    c = Path(__file__).resolve().parents[3] / "results"
    return c if c.is_dir() else None


def _setup_label(meta: dict[str, Any]) -> str:
    parts: list[str] = []
    if meta.get("gpu"):
        parts.append(str(meta["gpu"]) + (f" x{meta['gpu_count']}" if (meta.get("gpu_count") or 1) > 1 else ""))
    if meta.get("engine"):
        parts.append(str(meta["engine"]))
    if meta.get("mtp"):
        parts.append(str(meta["mtp"]))
    if meta.get("weights"):
        parts.append(str(meta["weights"]))
    if meta.get("hicache"):
        hc = meta["hicache"]
        parts.append("HiCache x" + str(hc) if isinstance(hc, int) else "HiCache")
    if (meta.get("replicas") or 1) > 1:
        parts.append(f"{meta['replicas']} replicas")
    if meta.get("arrival") == "open":
        parts.append("open-loop")
    return " · ".join(parts) if parts else (meta.get("file") or "")


def scan_library(root: Path | None = None) -> dict:
    """Scan the committed results tree into the /api/library payload.

    {"root": str|None, "models": [{slug, name, setups: [...]}, ...]} where
    each setup carries a ``lib:<slug>/<stem>`` id the compare endpoint
    resolves. Unparseable files are skipped (same policy as the HTML
    report); memory-estimate artifacts are listed but not comparable (kind
    "memory", rows []).
    """
    r = root if root is not None else results_root()
    if r is None or not r.is_dir():
        return {"root": None, "models": []}

    from .compare import normalize_chat_rows  # the shared chat-row shape

    _rts = _results_to_html()
    if _rts is None:
        return {"root": str(r), "models": [], "error": "results_to_html parsers unavailable"}
    parse_setup = _rts.parse_setup
    parse_chat_table_full = _rts.parse_chat_table_full
    parse_memory_estimate = _rts.parse_memory_estimate

    models: list[dict[str, Any]] = []
    for mdir in sorted(p for p in r.iterdir() if p.is_dir()):
        slug = mdir.name
        setups: list[dict[str, Any]] = []
        for fp in sorted(mdir.iterdir()):
            if not fp.is_file() or fp.suffix not in (".md", ".txt"):
                continue
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            stem = fp.stem
            mem = parse_memory_estimate(text)
            chat_rows: list[dict] = []
            kind = ""
            if mem is not None:
                kind = "memory"
            else:
                _mode, _rows, _arrival = parse_chat_table_full(text)
                chat_rows = normalize_chat_rows(text)
                if chat_rows:
                    kind = "chat"
            if kind == "":
                continue  # not machine-readable (notes, READMEs)
            meta = parse_setup(stem)
            setups.append(
                {
                    "id": LIB_PREFIX + slug + "/" + stem,
                    "kind": kind,
                    "file": fp.name,
                    "label": stem,
                    "setup": _setup_label(meta),
                    "gpu": meta.get("gpu"),
                    "gpu_count": meta.get("gpu_count") or 1,
                    "engine": meta.get("engine"),
                    "mtp": meta.get("mtp"),
                    "weights": meta.get("weights"),
                    "hicache": meta.get("hicache"),
                    "replicas": meta.get("replicas") or 1,
                    "status": "complete" if "Status: complete" in text else "partial",
                    "rows": chat_rows,
                    "config": mem.get("config", {}) if mem else {},
                }
            )
        if setups:
            models.append({"slug": slug, "name": slug.replace("_", " ").title(), "setups": setups})
    return {"root": str(r), "models": models}


def library_entry(entry_id: str) -> dict | None:
    """One library artifact in the compare flow's runs_data shape (kind,
    params_label, rows) — or None when the id does not resolve."""
    if not entry_id.startswith(LIB_PREFIX):
        return None
    ref = entry_id[len(LIB_PREFIX) :]
    if "/" not in ref or any(part in ("", ".", "..") for part in ref.split("/")):
        return None
    r = results_root()
    if r is None:
        return None
    # scan_library ids carry the artifact STEM; try the writer's suffixes
    path = None
    for ext in (".md", ".txt"):
        candidate = (r / (ref + ext)).resolve()
        if (
            candidate.parent == (r / ref).parent.resolve()
            and str(candidate).startswith(str(r.resolve()))
            and candidate.is_file()
        ):
            path = candidate
            break
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    from .compare import normalize_chat_rows

    _rts = _results_to_html()
    if _rts is None:
        return None
    chat_rows = normalize_chat_rows(text)
    mem = _rts.parse_memory_estimate(text)
    if not chat_rows and mem is None:
        return None
    meta = _rts.parse_setup(path.stem)
    return {
        "run_id": LIB_PREFIX + ref,
        "kind": "chat" if chat_rows else "memory",
        "target_url": f"results/{ref}{path.suffix} (committed baseline)",
        "status": "complete" if "Status: complete" in text else "partial",
        "started_utc": "",
        "params_label": _setup_label(meta) or path.stem,
        "config": mem.get("config", {}) if mem else {},
        "rows": chat_rows,
        "knee": None,
        "error": None if chat_rows else "memory artifact: capacity table only, no latency rows to compare",
    }


def _results_to_html():
    """The results_to_html parser module (lazy import: keeps the webapp
    runtime decoupled and matches compare.py's pattern)."""
    try:
        from importlib import import_module

        return import_module("model_benchmarker.results_to_html")
    except Exception:
        return None
