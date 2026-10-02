"""CLI: deterministic memory estimation for a model + GPU deployment shape.

Examples::

    # one scenario, GPU from the free-text name
    python -m model_benchmarker.memory_model \\
        --model /mnt/models/deepseek-ai/DeepSeek-V4-Flash-0731 \\
        --gpu "H200" --gpus 4 --tp 4 --kv-dtype fp8_e4m3 --context 32768

    # TP sweep x context grid, markdown artifact into the results tree
    python -m model_benchmarker.memory_model --model Qwen/Qwen3-8B \\
        --gpu "RTX Pro 6000" --gpus 1 --grid --output results/qwen/memory.md

The model's config.json is the structure source (local dir or HF repo id);
serving parameters come from flags, or from the seed deployment catalog
when ``--catalog-id`` matches an entry (tp / kv dtype / mem-fraction /
gpu count).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from .configs import load_config_json
from .dtypes import dtype_label
from .estimate import (
    DEFAULT_OVERHEAD_GIB,
    EstimateRequest,
    HicacheSpec,
    SpeculativeSpec,
    resolve_dtype_weight,
    run_estimate,
    summary_line,
)
from .gpus import DEFAULT_GPU, resolve_gpu
from .report import render_markdown

DEFAULT_CONTEXTS = (4096, 16384, 65536, 262144, 1048576)

EPILOG = """\
examples:
  # H200 x4, TP4, fp8 KV — the seed-catalog DeepSeek setup
  %(prog)s --model /mnt/models/deepseek-ai/DeepSeek-V4-Flash-0731 \\
      --gpu H200 --gpus 4 --tp 4 --kv-dtype fp8_e4m3 --mem-fraction 0.8

  # serve args straight from a seed-catalog deployment entry
  %(prog)s --model deepseek-v4-flash-0731 --catalog-id seed-h200-deepseek

  # TP sweep with a context grid, artifact for the HTML report
  %(prog)s --model Qwen/Qwen3-8B --gpu "RTX Pro 6000" --gpus 1 --grid \\
      --output results/Qwen/memory.md
"""


def _fmt_gib(b: float | None) -> str:
    return f"{b / 1024**3:.1f} GiB" if b is not None else "?"


def _fmt_m(b: float | None) -> str:
    return f"{b / 1e6:.2f}M" if b is not None else "?"


def _hicache_from_args(args: argparse.Namespace) -> HicacheSpec:
    """HicacheSpec from the --hicache* flags (all off when no mode given)."""
    mode = (args.hicache or "").strip().lower()
    if mode not in ("ratio", "size"):
        return HicacheSpec.off()
    rep: bool | None = None
    if args.hicache_tp_sharded and args.hicache_tp_replicated:
        print(
            "warning: --hicache-tp-sharded and --hicache-tp-replicated are mutually exclusive; using auto",
            file=sys.stderr,
        )
    elif args.hicache_tp_sharded:
        rep = False
    elif args.hicache_tp_replicated:
        rep = True
    return HicacheSpec(
        l2_mode=mode,
        l2_ratio=args.hicache_ratio or 0.0,
        l2_gib=args.hicache_size or 0.0,
        l3_gib=args.hicache_l3 or 0.0,
        l2_tp_replicated=rep,
    )


def _hicache_config_rows(spec: HicacheSpec, res) -> list[tuple[str, str]]:
    """``Memory configuration`` rows describing the cache-tier setup."""
    rows: list[tuple[str, str]] = []
    if spec.l2_mode == "ratio":
        rows.append(("hicache L2", f"ratio {spec.l2_ratio:g}x device pool"))
    elif spec.l2_mode == "size":
        rows.append(("hicache L2", f"{spec.l2_gib:g} GiB host RAM per replica"))
    if spec.l3_gib:
        rows.append(("hicache L3", f"{spec.l3_gib:g} GiB backing tier per replica"))
    if spec.active:
        rep = spec.l2_tp_replicated
        layout = "auto" if rep is None else ("replicated per TP rank" if rep else "sharded across TP ranks")
        rows.append(("hicache L2 layout", layout))
    if res.tiers.l2_tokens is not None:
        rows.append(("hicache L2 tokens", _fmt_m(res.tiers.l2_tokens)))
    if res.tiers.l3_tokens is not None:
        rows.append(("hicache L3 tokens", _fmt_m(res.tiers.l3_tokens)))
    if res.tiers.total_tokens is not None:
        rows.append(("addressable tokens (all tiers)", _fmt_m(res.tiers.total_tokens)))
    return rows


def _catalog_entry(catalog_path: Path | None, catalog_id: str | None, model_ref: str):
    """Find a catalog entry by id, or by name substring of the model ref."""
    if not (catalog_path and catalog_path.exists()):
        return None
    try:
        data = json.loads(catalog_path.read_text())
    except (OSError, ValueError):
        return None
    entries = data if isinstance(data, list) else []
    if catalog_id:
        for e in entries:
            if e.get("catalog_id") == catalog_id:
                return e
        return None
    low = model_ref.lower().rsplit("/", 1)[-1]
    for e in entries:
        name = str(e.get("name", "")).lower()
        if low and (low in name or name in low):
            return e
    return None


def _args_from_catalog(entry: dict, args: argparse.Namespace) -> None:
    """Fill unset serving args from a catalog entry's launch arguments."""
    a = entry.get("arguments") or []

    def opt(flag: str) -> str | None:
        for i, x in enumerate(a):
            if x == flag and i + 1 < len(a):
                return a[i + 1]
        return None

    if args.gpu_count is None:
        try:
            args.gpu_count = int(entry.get("resource_request_gpu") or 0) or None
        except (TypeError, ValueError):
            pass
    if args.tp is None:
        tp = opt("--tp-size")
        args.tp = int(tp) if tp and tp.isdigit() else args.gpu_count
    if args.pp is None:
        pp = opt("--pp-size")
        args.pp = int(pp) if pp and pp.isdigit() else 1
    if args.kv_dtype is None:
        args.kv_dtype = opt("--kv-cache-dtype")
    if args.mem_fraction is None:
        mf = opt("--mem-fraction-static")
        try:
            args.mem_fraction = float(mf) if mf else None
        except ValueError:
            pass


def _catalog_model_ref(entry: dict) -> str | None:
    """Derive an HF repo id (or local path) for a catalog entry.

    The serve arguments name the model (e.g. ``sglang serve
    deepseek-ai/DeepSeek-V4-Flash-0731``); the PVC URI carries the same
    path. Prefer the args (they are the repo id actually served).
    """
    a = entry.get("arguments") or []
    for i, x in enumerate(a):
        if x in ("serve", "run") and i + 1 < len(a) and "/" in a[i + 1]:
            return str(a[i + 1])
    uri = str(entry.get("uri") or "")
    if "pvc://" in uri:
        tail = uri.split("pvc://", 1)[1].split("?")[0]
        # .../large-models/<org>/<name> -> <org>/<name>
        parts = [p for p in tail.split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
    return None


def build_payload(args: argparse.Namespace) -> tuple[dict, int]:
    """Estimate + assemble the artifact payload. Returns (payload, exit_code)."""
    catalog = args.catalog
    entry = _catalog_entry(catalog, args.catalog_id, args.model) if (args.catalog_id or catalog) else None

    model_ref = args.model
    if entry and "/" not in model_ref and not Path(model_ref).expanduser().is_dir():
        # a deployment name needs its underlying model repo from the catalog
        repo = _catalog_model_ref(entry)
        if repo:
            model_ref = repo

    try:
        cfg, source = load_config_json(model_ref)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return {}, 2

    gpu = resolve_gpu(args.gpu) if args.gpu else None
    if gpu is None and not args.gpu_vram:
        gpu = resolve_gpu("H200")
        if gpu is None:
            gpu = DEFAULT_GPU
    if args.gpu_vram:
        from .gpus import GpuSpec

        gpu = GpuSpec(name=args.gpu or "custom", vram_gib=float(args.gpu_vram))
    if gpu is None:  # unreachable in practice: DEFAULT_GPU fallback above
        gpu = DEFAULT_GPU

    gpus = args.gpu_count if args.gpu_count is not None else 1
    tp = args.tp if args.tp is not None else gpus
    pp = args.pp if args.pp is not None else 1
    kv_dtype = args.kv_dtype
    mem_fraction = args.mem_fraction if args.mem_fraction is not None else 0.9
    contexts = sorted({int(c) for c in (args.context or DEFAULT_CONTEXTS)})

    if entry:
        _args_from_catalog(entry, args)
        gpus = args.gpu_count or gpus
        tp = args.tp or tp
        pp = args.pp or pp
        kv_dtype = args.kv_dtype
        mem_fraction = args.mem_fraction if args.mem_fraction is not None else mem_fraction
        if not args.gpu:
            gpu = resolve_gpu(str(entry.get("tier") or "")) or gpu

    # weights dtype: explicit flag > quantization_config/torch_dtype from config
    weight_dtype = resolve_dtype_weight(cfg, args.weight_dtype)
    spec_args = SpeculativeSpec(
        algorithm=(
            getattr(args, "speculative_algorithm", "") or ("MTP" if getattr(args, "speculative", 0) else "")
        ).upper(),
        draft_tokens=getattr(args, "draft_tokens", 0) or 0,
        draft_model=getattr(args, "draft_model", "") or "",
        layers=getattr(args, "speculative", 0) or 0,
    )
    kv_label_str = dtype_label(kv_dtype) if kv_dtype else "bf16 (auto)"

    req = EstimateRequest(
        cfg=cfg,
        gpu=gpu,
        gpu_count=gpus,
        tp_size=tp,
        pp_size=pp,
        weight_dtype=weight_dtype,
        kv_dtype=kv_dtype,
        mem_fraction=mem_fraction,
        overhead_gib=args.overhead,
        context=contexts[-1],
        speculative=spec_args,
        moe_runner=getattr(args, "moe_runner", "") or None,
        hicache=_hicache_from_args(args),
        ep_size=args.ep,
    )
    res = run_estimate(req)
    warnings = list(res.warnings)
    exit_code = 0

    # ---- structure section ----
    kvd = res.details["kv"]
    wd = res.details["weights"]
    params_b = (wd.get("params") or 0) / 1e9
    structure: list[tuple[str, str]] = [
        ("model_type", cfg.model_type or "?"),
        ("parameters", f"{params_b:.1f}B" if params_b else "unknown (config incomplete)"),
        ("weights", f"{_fmt_gib(res.weights_bytes)} @ {dtype_label(weight_dtype) or '?'}"),
        ("attention", str(kvd.get("arch", "?"))),
        ("kv formula", str(kvd.get("formula", "?"))),
        ("kv per token", f"{res.kv_bytes_per_token / 1024:.1f} KiB @ {dtype_label(kv_dtype)}"),
        ("kv per layer", f"{kvd.get('per_layer_bytes', 0) / 1024:.1f} KiB (whole-model stream)"),
        ("layers", str(kvd.get("layer_mix").label()) if kvd.get("layer_mix") else "?"),
        ("config source", source),
    ]
    if cfg.num_nextn_predict_layers:
        structure.append(("MTP layers (checkpoint)", str(cfg.num_nextn_predict_layers)))

    hicache = _hicache_from_args(args)

    # ---- scenarios: the requested one + TP variants ----
    scenarios: list[dict] = []
    tps = [tp]
    if args.grid:
        tps = [t for t in (1, 2, 4, 8, 16) if t <= gpus]
        if tp not in tps:
            tps.append(tp)
            tps.sort()
    gpu_counts = sorted({gpus, *args.grid_gpus}) if args.grid_gpus else [gpus]
    for g in gpu_counts:
        for t in [x for x in tps if x <= g]:
            r = run_estimate(
                EstimateRequest(
                    cfg=cfg,
                    gpu=gpu,
                    gpu_count=g,
                    tp_size=t,
                    pp_size=pp,
                    weight_dtype=weight_dtype,
                    kv_dtype=kv_dtype,
                    mem_fraction=mem_fraction,
                    overhead_gib=args.overhead,
                    context=contexts[-1],
                    speculative=spec_args,
                    hicache=hicache,
                    ep_size=args.ep,
                )
            )
            warnings.extend(w for w in r.warnings if w not in warnings)
            scenarios.append(
                {
                    "label": f"{gpu.name} x{g} TP{t}" + (f" PP{pp}" if pp > 1 else ""),
                    "verdict": summary_line(r),
                    "fits": r.fits,
                    "kv_tokens": r.kv_tokens_total,
                    "_res": r,
                }
            )

    # ---- capacity grid ----
    grid_rows: list[dict] = []
    primary = scenarios[0]["_res"] if scenarios else res
    for sc in scenarios:
        r = sc["_res"]
        cells = []
        for c in contexts:
            cells.append(int(r.kv_tokens_total // c) if r.fits and r.kv_tokens_total else None)
        grid_rows.append({"label": sc["label"], "kv": _fmt_m(r.kv_tokens_total) if r.fits else "-", "cells": cells})
    grid_data = {"contexts": contexts, "rows": grid_rows}

    status = "ok" if res.fits else ("does not fit" if res.fits is False else "unknown weights size")
    primary_kv = primary.kv_tokens_total
    if primary.fits and primary_kv is not None:
        status += f" | KV pool {primary_kv / 1e6:.2f}M tokens | {primary.concurrency or 0} x {contexts[-1] // 1024}k"
    elif res.fits is None:
        status = "weights size unknown (set --weight-dtype)"

    payload = {
        "model": args.model.rsplit("/", 1)[-1] or args.model,
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "status": status,
        "config": [
            ("tool", "memory-estimate"),
            ("model", args.model),
            ("gpu", f"{gpu.name} x{gpus} (TP{tp}" + (f", PP{pp}" if pp > 1 else "") + ")"),
            ("weight dtype", dtype_label(weight_dtype) or "?"),
            ("kv dtype", kv_label_str),
            ("mem fraction", f"{mem_fraction:g}"),
            *([("expert parallel", str(args.ep))] if args.ep > 1 else []),
            ("overhead", f"{args.overhead:g} GiB/GPU"),
            *(_hicache_config_rows(hicache, res)),
            ("max context", f"{contexts[-1]:,}"),
        ],
        "structure": structure,
        "scenarios": [{"label": s["label"], "verdict": s["verdict"]} for s in scenarios],
        "grid": grid_data,
        "warnings": warnings,
        "fits": res.fits,
        "kv_tokens_total": primary_kv,
        "concurrency": primary.concurrency,
    }
    return payload, exit_code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="memory-estimate",
        description="Estimate LLM serving memory: does it fit, KV token capacity, concurrent requests.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--model", help="model dir (with config.json) or HF repo id (required unless --calculator)")
    ap.add_argument("--gpu", help='GPU name, free text (e.g. "H200 PCIe", "RTX Pro 6000")')
    ap.add_argument("--gpu-vram", type=float, help="override VRAM in GiB (unknown GPUs)")
    ap.add_argument("--gpus", dest="gpu_count", type=int, help="number of GPUs (default 1)")
    ap.add_argument("--tp", type=int, help="tensor-parallel size (default: all GPUs)")
    ap.add_argument("--pp", type=int, help="pipeline-parallel size (default 1; weights stage-split x PP, "
                                            "KV pool unchanged — the GLM-5.2 TP4/PP2 shape)")
    ap.add_argument("--weight-dtype", help="weights dtype (default: from quantization_config/torch_dtype)")
    ap.add_argument("--kv-dtype", help="KV cache dtype (fp16/bf16 default; fp8_e4m3 etc.)")
    ap.add_argument("--mem-fraction", type=float, help="engine mem-fraction-static (default 0.9)")
    ap.add_argument(
        "--overhead",
        type=float,
        default=DEFAULT_OVERHEAD_GIB,
        help=f"per-GPU activation/CUDA overhead in GiB (default {DEFAULT_OVERHEAD_GIB})",
    )
    ap.add_argument("--context", type=int, action="append", help="context length(s) for the grid (repeatable)")
    ap.add_argument("--grid", action="store_true", help="sweep TP sizes (1,2,4,... <= GPUs)")
    ap.add_argument(
        "--grid-gpus",
        type=lambda s: [int(x) for x in s.split(",")],
        help="with --grid: also sweep these GPU counts (e.g. 1,2,4,8)",
    )
    ap.add_argument(
        "--speculative",
        type=int,
        default=0,
        metavar="N",
        help="N MTP/draft layers active (extra KV per token) -- MTP-family "
        "(DSPARK/EAGLE/NEXTN/MTP). For a separate draft model use "
        "--speculative-algorithm DFLASH --draft-model <repo>.",
    )
    ap.add_argument(
        "--speculative-algorithm",
        default="",
        metavar="ALGO",
        help="speculative family: DFLASH | EAGLE | NEXTN | DSPARK | MTP "
        "(empty = off; with N>0 and no algorithm, MTP is assumed)",
    )
    ap.add_argument(
        "--draft-model",
        default="",
        metavar="REPO",
        help="DFLASH draft model repo id / path (priced separately: weights + KV)",
    )
    ap.add_argument(
        "--draft-tokens",
        type=int,
        default=0,
        metavar="N",
        help="speculative-num-draft-tokens (the acceptance window; display + cap)",
    )
    ap.add_argument(
        "--moe-runner",
        default="",
        metavar="BACKEND",
        help="the deployment's --moe-runner-backend (flashinfer_mxfp4/marlin "
        "store routed experts at 4-bit -- the PCAI small-GPU MoE shape)",
    )
    ap.add_argument(
        "--ep",
        type=int,
        default=1,
        metavar="N",
        help="expert-parallel size: MoE experts shard EP-ways, attention stays TP "
        "(e.g. --gpus 8 --tp 4 --ep 8 for MoE across 2 NVLink groups)",
    )
    ap.add_argument(
        "--hicache",
        choices=("ratio", "size"),
        metavar="MODE",
        help="hierarchical KV cache (SGLang HiCache): 'ratio' sizes the L2 host "
        "tier from --hicache-ratio, 'size' from --hicache-size (GiB)",
    )
    ap.add_argument(
        "--hicache-ratio",
        type=float,
        default=0.0,
        metavar="X",
        help="L2 = X x the device KV pool, per replica (with --hicache ratio)",
    )
    ap.add_argument(
        "--hicache-size",
        type=float,
        default=0.0,
        metavar="GIB",
        help="L2 host RAM per replica in GiB (with --hicache size)",
    )
    ap.add_argument(
        "--hicache-l3",
        type=float,
        default=0.0,
        metavar="GIB",
        help="L3 backing-tier (NVMe/object store) budget per replica in GiB; "
        "capped at the L2 pool (a larger L3 is never filled)",
    )
    ap.add_argument(
        "--hicache-tp-replicated",
        action="store_true",
        help="L2 holds the FULL per-token stream on every TP rank (forced; the "
        "auto rule already does this for MQA/MLA families)",
    )
    ap.add_argument(
        "--hicache-tp-sharded",
        action="store_true",
        help="L2 is partitioned across TP ranks (forced; the auto rule shards for standard GQA/MHA families)",
    )
    ap.add_argument("--catalog", type=Path, help="seed_catalog.json path (serving args from a matched entry)")
    ap.add_argument("--catalog-id", help="exact catalog_id to take serving args from (implies --catalog discovery)")
    ap.add_argument("--output", type=Path, help="write the markdown artifact here")
    ap.add_argument("--json", action="store_true", help="print the payload as JSON instead of markdown")
    ap.add_argument(
        "--calculator",
        action="store_true",
        help="generate the standalone HTML memory calculator (results/memory_calculator.html) and exit",
    )
    ap.add_argument(
        "--add-model",
        action="append",
        default=[],
        metavar="REF",
        help="with --calculator: extra HF repo id / model dir to embed (repeatable)",
    )
    ap.add_argument(
        "--no-fetch",
        action="store_true",
        help="with --calculator: do not touch the network (HF cache / built-in shapes only)",
    )
    args = ap.parse_args(argv)

    if not args.model and not args.calculator:
        ap.error("--model is required (or use --calculator to generate the HTML page)")

    if args.calculator:
        from .html import write_calculator

        out, notes = write_calculator(output=args.output, extra_refs=args.add_model, allow_fetch=not args.no_fetch)
        n = sum(1 for m in json.loads(out.read_text().split("const MODELS = ")[1].split(";\n")[0]) if m.get("params"))
        print(f"Memory calculator written to {out}")
        print(f"  models embedded with params: {n}")
        for note in notes:
            print(f"  note: {note}")
        return 0

    if not args.catalog:
        # default: the Model-Downloader catalog next to this repo
        try:
            from model_benchmarker.results_to_html import discover_catalog

            args.catalog = discover_catalog()
        except Exception:
            args.catalog = None

    payload, code = build_payload(args)
    if not payload:
        return code

    if args.json:
        printable = {k: v for k, v in payload.items() if k != "grid"}
        printable["grid"] = payload["grid"]
        print(json.dumps(printable, indent=2, default=str))
        return code

    md = render_markdown(payload)
    if args.output:
        out = args.output.expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"Memory estimate written to {out}")
    print(md)
    return code


if __name__ == "__main__":
    sys.exit(main())
