#!/usr/bin/env python3
"""Convert the text-based benchmark outputs in results/ into a single,
self-contained HTML report.

Each .txt under results/<model>/ is the --output of benchmark_chat.py (or a
RAG scale-run).  The filename encodes the serving setup (GPU, engine, MTP /
speculative-decoding config, HiCache ratio, replicas, ...).  The report:

  - groups everything by model and lets you filter by model / setup
  - shows, per setup, the full serving metadata (model, GPU, # GPUs, MTP
    config, engine, HiCache, replicas, obsolete marker, source file)
  - has a clickable "PCAI deployment config" expander linking the run to the
    matching Model-Downloader catalog entry (image, serving args, resources)
  - renders every benchmark table as HTML
  - lets you pick 2+ setups and compare them side-by-side on the same
    (ctx, users, task) workload

The output HTML is fully self-contained (no external CSS/JS), so you can
share the file by itself - no one needs the repo or the .txt files.

Usage:
  python3 results_to_html.py                 # <repo-root>/results (two levels above this script)
  python3 results_to_html.py --results path  # explicit results dir
  python3 results_to_html.py --catalog path/seed_catalog.json
  python3 results_to_html.py --output out.html
  python3 results_to_html.py --title "Chat benchmarks"
  python3 results_to_html.py --results_label "tools/model-benchmarker/results/" \
      --catalog_label "tools/model-downloader/.../seed_catalog.json"  # display-only path labels
  python3 results_to_html.py --open          # open in browser
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Display names / model slug -> readable name
# --------------------------------------------------------------------------
MODEL_NAMES = {
    "deepseek_v4_flash_0731": "DeepSeek-V4-Flash-0731",
    "qwen_38_27b": "Qwen3.8-27B",
    "gemma_4_31b": "Gemma-4-31B",
    "glm_52_753b": "GLM-5.2-753B",
    "glm-5.3-flash": "GLM-5.3-Flash",
    "RAG": "RAG (Multimodal Retrieval)",
}

# GPU token (as it appears in the filename) -> (display name, hw tier)
# NOTE: all existing benchmarked H200s are PCIe-based, so "H200S" is shown
# as PCIe too (the "S" is just part of the old filename convention).  An
# SXM part is not used anywhere yet.
GPU_TOKENS = [
    ("H200S", "NVIDIA H200 (PCIe)", "h200"),
    ("H200", "NVIDIA H200 (PCIe)", "h200"),
    ("H100S", "NVIDIA H100 (SXM)", "h100"),
    ("H100", "NVIDIA H100", "h100"),
    ("H800", "NVIDIA H800", "h800"),
    ("A100S", "NVIDIA A100 (SXM)", "a100"),
    ("A100", "NVIDIA A100", "a100"),
    ("L40S", "NVIDIA L40S", "l40s"),
    ("RTX_PRO_6000", "NVIDIA RTX PRO 6000", "rtx-pro-6000"),
    ("RTXPRO6000", "NVIDIA RTX PRO 6000", "rtx-pro-6000"),
    ("RTX_PRO_4000", "NVIDIA RTX PRO 4000", "rtx-pro-4000"),
    ("RTXPRO4000", "NVIDIA RTX PRO 4000", "rtx-pro-4000"),
    ("RTX6000", "NVIDIA RTX 6000 Ada", "rtx-pro-6000"),
    ("T4", "NVIDIA T4", "t4"),
]

# MTP / speculative-decoding config tokens -> readable label
MTP_LABELS = {
    "dflash2": "DFlash2",
    "dflash": "DFlash",
    "dspark": "DSPark",
    "dsp": "DSPark",
    "eagle": "EAGLE",
    "eagle3": "EAGLE-3",
    "mtp": "MTP",
    "mtp2": "MTP-2",
    "disabled": "Disabled",
}

# Engine tokens
ENGINE_TOKENS = {"sglang": "SGLang", "vllm": "vLLM"}

# Weight-precision tokens -> readable label (nvfp4 = Blackwell FP4 quant)
WEIGHT_LABELS = {
    "nvfp4": "NVFP4",
    "fp8": "FP8",
    "fp8_e4m3": "FP8",
    "bf16": "BF16",
    "fp16": "FP16",
}

# Tokens to ignore when building the "extra info" notes
_IGNORED_TOKENS = {"old", "hicache", "fp8", "fp8_e4m3", "bf16", "fp16"}

# --------------------------------------------------------------------------
# .txt parsing
# --------------------------------------------------------------------------


def parse_chat_table(text: str) -> tuple[str, list[dict]]:
    """Parse a benchmark_chat.py --output table.

    Returns (mode, rows).  mode is "multiturn", "single" or "unknown"
    ("multiturn" is surfaced per setup as ``multiturn: True`` by the caller).
    Each row: {ctx, users, task, failed, ttft:[..4], post:[..4] or None,
    tokens:[..4]}.
    """
    lines = [ln.rstrip("\n") for ln in text.splitlines()]
    mode = "unknown"
    joined = "\n".join(lines)
    if "multiturn" in joined and "TTFT turn1" in joined:
        mode = "multiturn"
    elif "TTFT (ms)" in joined or "tokens/s" in joined:
        mode = "single"

    rows: list[dict] = []
    for ln in lines:
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 2:
            continue
        head = parts[0].split()
        if len(head) < 4 or not head[0].isdigit() or not head[1].isdigit() or not head[3].isdigit():
            continue
        if head[2] == "task":
            continue
        try:
            ctx = int(head[0])
            users = int(head[1])
            task = head[2]
            failed = int(head[3])
        except ValueError:
            continue
        segs = []
        ok = True
        for seg in parts[1:]:
            toks = seg.split()
            if len(toks) != 4:
                ok = False
                break
            try:
                segs.append([float(t) for t in toks])
            except ValueError:
                ok = False
                break
        if not ok:
            continue
        if len(segs) == 2:
            ttft, tokens = segs
            ttft_post = None
        elif len(segs) == 3:
            ttft, ttft_post, tokens = segs
        else:
            continue
        rows.append(
            {
                "ctx": ctx,
                "users": users,
                "task": task,
                "failed": failed,
                "ttft": ttft,
                "ttft_post": ttft_post,
                "tokens": tokens,
            }
        )
    return mode, rows


def parse_rag_table(text: str) -> dict | None:
    """Scan a RAG scale-benchmark output into a small summary dict."""
    if "BENCHMARK CONFIGURATION" not in text and "BENCHMARK RESULTS" not in text:
        return None
    summary: dict = {"config": {}, "results": {}}
    section = None
    for ln in text.splitlines():
        ln = ln.strip()
        if "BENCHMARK CONFIGURATION" in ln:
            section = "config"
            continue
        if "BENCHMARK RESULTS" in ln:
            section = "results"
            continue
        if not ln or "=====" in ln or "---" in ln or ln.startswith("("):
            continue
        if section and ":" in ln:
            k, _, v = ln.partition(":")
            summary[section][k.strip()] = v.strip()
    if not summary["results"]:
        return None
    return summary


# --------------------------------------------------------------------------
# Filename -> setup metadata
# --------------------------------------------------------------------------


def parse_setup(stem: str) -> dict:
    """Infer the serving setup from a result filename stem."""
    meta = {
        "gpu": None,
        "tier": None,
        "gpu_count": 1,
        "engine": None,
        "mtp": None,
        "weights": None,
        "hicache": None,
        "replicas": 1,
        "obsolete": False,
        "hints": [],
    }
    if stem.startswith("OLD_"):
        meta["obsolete"] = True
        stem = stem[len("OLD_") :]

    tokens = stem.split("_")

    def parse_gpu_token(tok: str) -> None:
        for name, disp, tier in GPU_TOKENS:
            if tok.startswith(name):
                rest = tok[len(name) :]
                meta["gpu"] = disp
                meta["tier"] = tier
                if rest.startswith("x") and rest[1:].isdigit():
                    meta["gpu_count"] = int(rest[1:])
                elif rest.isdigit():
                    meta["gpu_count"] = int(rest)
                return
        meta["hints"].append(tok)

    for tok in tokens:
        low = tok.lower()
        if low in ENGINE_TOKENS:
            meta["engine"] = ENGINE_TOKENS[low]
            continue
        if low in MTP_LABELS:
            meta["mtp"] = MTP_LABELS[low]
            continue
        if low == "hicache":
            meta["hicache"] = "on"
            continue
        m = re.fullmatch(r"hicachex(\d+)", low)
        if m:
            meta["hicache"] = int(m.group(1))
            continue
        m = re.fullmatch(r"replicasx(\d+)", low)
        if m:
            meta["replicas"] = int(m.group(1))
            continue
        if low in WEIGHT_LABELS:
            meta["weights"] = WEIGHT_LABELS[low]
            continue
        if low in _IGNORED_TOKENS:
            continue
        if re.fullmatch(r"x\d+", low):
            continue
        if low == "old":
            meta["obsolete"] = True
            continue
        parse_gpu_token(tok)
    if meta["gpu"] is None and meta["hints"]:
        meta["gpu"] = meta["hints"][-1].upper()
    return meta


# --------------------------------------------------------------------------
# PCAI deployment catalog matching
# --------------------------------------------------------------------------


def discover_catalog() -> Path | None:
    """Locate the Model-Downloader seed_catalog.json near this repo."""
    here = Path(__file__).resolve()
    for root in [here] + list(here.parents):
        for cand in (
            root / "ModelDownloader" / "src" / "model_downloader" / "app" / "seed_catalog.json",
            root
            / "pcai-solutions"
            / "tools"
            / "model-downloader-web"
            / "src"
            / "model_downloader"
            / "app"
            / "seed_catalog.json",
        ):
            if cand.exists():
                return cand
    return None


def load_catalog(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _cat_spec(entry: dict) -> str | None:
    args = [str(a) for a in entry.get("arguments", [])]
    for i, a in enumerate(args):
        if a == "--speculative-algorithm" and i + 1 < len(args):
            return args[i + 1].upper()
        if a == "--speculative-config" and i + 1 < len(args):
            return args[i + 1]
    return None


def _cat_hicache(entry: dict) -> int | None:
    args = [str(a) for a in entry.get("arguments", [])]
    for i, a in enumerate(args):
        if a == "--hicache-ratio" and i + 1 < len(args):
            try:
                return int(float(args[i + 1]))
            except ValueError:
                return None
    if "--enable-hierarchical-cache" in args:
        return "on"
    return None


def _cat_image(entry: dict) -> str:
    return str(entry.get("image", ""))


def match_catalog(model_slug: str, meta: dict, catalog: list[dict]) -> dict | None:
    """Best-effort match of a setup against the deployment catalog."""
    if not catalog:
        return None
    slug_low = model_slug.lower()
    gpu_low = (meta.get("gpu") or "").lower()
    tier = meta.get("tier") or ""
    key_map = {
        "deepseek_v4_flash_0731": "deepseek-v4-flash-0731",
        "qwen_38_27b": "qwen3.8-27b",
        "gemma_4_31b": "gemma-4-31b",
    }
    key = key_map.get(model_slug)
    candidates = []
    for e in catalog:
        name = str(e.get("name", "")).lower()
        cid = str(e.get("catalog_id", "")).lower()
        if not (slug_low in name or slug_low.replace("_", "-") in name):
            if not (key and (key in name or key in cid)):
                continue
        score = 0
        etier = str(e.get("tier", ""))
        if tier and tier == etier:
            score += 12
        elif gpu_low and (gpu_low in etier or etier in gpu_low):
            score += 10
        elif gpu_low and ("h200" in etier and "h200" in gpu_low):
            score += 10
        eng = (meta.get("engine") or "").lower()
        img = _cat_image(e).lower()
        if eng and eng in img:
            score += 8
        spec = _cat_spec(e)
        if meta.get("mtp"):
            mtp = meta["mtp"].lower()
            if spec and mtp in spec.lower():
                score += 12
        else:
            if spec in (None, "", "none"):
                score += 3
        hc = _cat_hicache(e)
        if meta.get("hicache") is not None:
            want = meta["hicache"]
            if hc == want or (hc == "on" and want == "on"):
                score += 6
            elif want == "on" and hc is not None:
                score += 3
        else:
            if hc in (None, ""):
                score += 3
        # Weight-format preference: when the run's filename declares a weight
        # precision (nvfp4/fp8/bf16/fp16), prefer the catalog entry that
        # matches it so FP8 vs NVFP4 variants don't get conflated.
        wf = meta.get("weights")
        if wf:
            wlow = wf.lower()
            hay = " ".join(
                (
                    str(e.get("name", "")),
                    str((e.get("arguments") or ["", ""])[0]),
                    str(e.get("uri", "")),
                )
            ).lower()
            score += 6 if wlow in hay else -6
        candidates.append((score, e))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    best = dict(candidates[0][1])
    # keep the payload lean: drop huge / redundant catalog fields
    for _k in ("chat_template_contents", "uri", "metadata", "registry", "project", "environment", "version"):
        best.pop(_k, None)
    best["_match_score"] = candidates[0][0]
    return best


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=None, help="Path to results/ dir (default: beside this script).")
    ap.add_argument("--catalog", default=None, help="Path to Model-Downloader seed_catalog.json (auto-discovered).")
    ap.add_argument("--output", default=None, help="Output HTML path (default: results/benchmark_report.html).")
    ap.add_argument("--title", default="PCAI Model Benchmarks", help="Page title.")
    ap.add_argument("--open", action="store_true", help="Open the report in the default browser.")
    ap.add_argument(
        "--results_label",
        default="pcai-solutions/tools/model-benchmarker/results/",
        help="How to display the results directory in the report (default: pcai-solutions/tools path).",
    )
    ap.add_argument(
        "--catalog_label",
        default="pcai-solutions/tools/model-downloader-web/src/model_downloader/app/seed_catalog.json",
        help="How to display the catalog path in the report (default: pcai-solutions/tools path).",
    )
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    results_dir = Path(args.results) if args.results else (script_dir.parent.parent / "results")
    results_dir = results_dir.expanduser().resolve()
    if not results_dir.is_dir():
        print(f"error: results dir not found: {results_dir}", file=sys.stderr)
        return 2

    catalog_path = Path(args.catalog).expanduser() if args.catalog else None
    if catalog_path is None:
        catalog_path = discover_catalog()
    catalog = load_catalog(catalog_path)

    models = []
    for m in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        slug = m.name
        files = sorted(p for p in m.glob("*.txt") if p.is_file())
        if not files:
            continue
        display = MODEL_NAMES.get(slug, slug.replace("_", " ").title())
        setups = []
        for fp in files:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                print(f"warn: cannot read {fp}: {e}", file=sys.stderr)
                continue
            stem = fp.stem
            rag = parse_rag_table(text)
            mode, rows = parse_chat_table(text)
            if not rows and rag is None:
                continue
            meta = parse_setup(stem)
            if rag is not None and not rows:
                # RAG scale-benchmark: not a chat-table result
                meta = {
                    "gpu": None,
                    "tier": None,
                    "gpu_count": 1,
                    "engine": None,
                    "mtp": None,
                    "weights": None,
                    "hicache": None,
                    "replicas": 1,
                    "obsolete": False,
                    "hints": [],
                }
                mode = "rag"
            cat_entry = match_catalog(slug, meta, catalog)
            if meta.get("mtp") is None and cat_entry:
                spec = _cat_spec(cat_entry)
                if spec:
                    s = str(spec).upper()
                    for k, lab in (("DFLASH", "DFlash2"), ("DSPARK", "DSPark"), ("EAGLE", "EAGLE"), ("MTP", "MTP")):
                        if k in s:
                            meta["mtp"] = lab
                            break
                else:
                    # catalog entry present but no speculative decoding configured
                    meta["mtp"] = "Disabled"
            elif meta.get("mtp") is None:
                # no catalog matched: keep it explicit that nothing was configured
                meta["mtp"] = "Disabled"
            # Derive the serving engine from the catalog when the filename
            # doesn't say (e.g. "H200Sx4_hicachex2.txt" is served by SGLang
            # per its catalog entry, not by vLLM).
            if meta.get("engine") is None and cat_entry:
                img = _cat_image(cat_entry).lower()
                if "sglang" in img:
                    meta["engine"] = "SGLang"
                elif "vllm" in img:
                    meta["engine"] = "vLLM"
            setup = {
                "slug": slug,
                "file": fp.name,
                "mtime": _fmt_mtime(fp),
                "meta": meta,
                "mode": mode,
                "rows": rows,
                "multiturn": mode == "multiturn",
                "catalog": cat_entry,
                "catalog_source": None,  # filled in relative form below
            }
            if rag is not None:
                setup["rag"] = rag
            setups.append(setup)
        if setups:
            models.append({"slug": slug, "name": display, "setups": setups})

    # Split into chat-model results (shown under "Models") and RAG
    # scale-benchmarks (shown in a separate "RAG" tab) so RAG does not
    # appear as a model in the "All models" view.
    chat_models: list[dict] = []
    rag_models: list[dict] = []
    for m in models:
        chat = [s for s in m["setups"] if "rag" not in s]
        rag = [s for s in m["setups"] if "rag" in s]
        if chat:
            chat_models.append({"slug": m["slug"], "name": m["name"], "setups": chat})
        if rag:
            rag_models.append({"slug": m["slug"], "name": m["name"], "setups": rag})

    output = Path(args.output) if args.output else (results_dir / "benchmark_report.html")
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    # Display paths are presented in the pcai-solutions/tools/ layout
    # (hardcoded by default, override with the *_label flags).
    results_label = args.results_label
    catalog_label = args.catalog_label if catalog_path else None

    for m in chat_models + rag_models:
        for s in m["setups"]:
            if s.get("catalog_source") is None and catalog_label:
                s["catalog_source"] = catalog_label

    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "results_dir": results_label,
        "catalog": catalog_label,
        "model_count": len(chat_models),
        "setup_count": sum(len(m["setups"]) for m in chat_models),
        "rag_count": sum(len(m["setups"]) for m in rag_models),
        "models": chat_models,
        "rag_models": rag_models,
    }
    output.write_text(render_html(data, args.title), encoding="utf-8")
    print(f"Report written to {output}")
    print(f"  models: {data['model_count']}   setups: {data['setup_count']}")
    print(f"  catalog: {data['catalog'] or 'none (metadata inferred from filenames)'}")
    if args.open:
        # No shell interpolation: --output is operator-supplied and must not
        # be able to inject commands via quoting.
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", str(output)], check=False)
            else:
                subprocess.Popen(
                    ["xdg-open", str(output)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except OSError as exc:
            print(f"warn: could not open browser: {exc}", file=sys.stderr)
    return 0


def _fmt_mtime(fp: Path) -> str:
    try:
        t = fp.stat().st_mtime
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    except OSError:
        return ""


# --------------------------------------------------------------------------
# HTML rendering (delegated to _html_template so this file stays light)
# --------------------------------------------------------------------------


def render_html(data: dict, title: str) -> str:
    try:
        from ._html_template import build_html  # type: ignore[import-not-found]
    except ImportError:  # running as a plain script
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _html_template import build_html  # type: ignore[import-not-found]

    return build_html(data, title)


if __name__ == "__main__":
    raise SystemExit(main())
