"""Measured-benchmark lookup for the memory estimator.

The estimator answers CAPACITY questions deterministically (does it fit, how
many concurrent N-token requests) and deliberately does NOT predict
tokens/second: a first-order roofline over-predicted this fleet's measured
decode by 3-5x across batch sizes, and a wrong throughput figure is worse
than none in a tool people size deployments with.

What it does instead: when the committed results library holds a REAL
benchmark run for the same model + GPU shape, surface those MEASURED numbers
verbatim with their provenance (artifact path, setup label, context/user
levels). No benchmark -> no panel. Nothing here is inferred, interpolated,
or extrapolated: every number shown was actually measured.

Memory-estimate artifacts are excluded by construction -- they are the
estimator's own output (kind "memory"), so quoting them back would be
circular.
"""

from __future__ import annotations

import re
from typing import Any

from ..memory_model.throughput import active_params_per_token

# The measured columns worth surfacing, in reading order. Keys match
# atomize.atom_to_compare_metrics (the results page's row-metric shape).
_METRIC_COLUMNS: tuple[tuple[str, str], ...] = (
    ("tokens_p50", "tokens/s P50"),
    ("tokens_p95", "tokens/s P95"),
    ("ttft_turn1_p50_ms", "TTFT turn1 P50 (ms)"),
    ("ttft_post_p50_ms", "TTFT-post P50 (ms)"),
)


def _model_tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens of a model name/repo (used for GPU
    names, which are canonical on both sides)."""
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def _fingerprint(text: str) -> str:
    """Separator-free identity string for a model name.

    Names and directory slugs disagree about separators AND decimal versions:
    'Qwen3.8-27B-FP8' vs 'qwen_38_27b', 'DeepSeek-V4.1-Flash' vs
    'deepseek_v41_flash'. Stripping every non-alphanumeric character makes
    both spellings collapse to one comparable string ('qwen3827bfp8' contains
    'qwen3827b'), while genuinely different models stay distinct
    ('deepseekv4flash' is NOT a substring of 'deepseekv41flash').
    """
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _slug_matches(model_ref: str, slug: str, artifact_model: str) -> bool:
    """True when a library entry plausibly IS this model.

    Conservative on purpose — a wrong match would show another model's
    measurements. Compares separator-free fingerprints, requiring one to
    CONTAIN the other (so 'Qwen3.8-27B-FP8' matches the 'qwen_38_27b' slug,
    while 'DeepSeek-V4-Flash' never matches 'deepseek_v41_flash').
    """
    want = _fingerprint(model_ref.rsplit("/", 1)[-1])
    if not want:
        return False
    for cand in (slug, artifact_model):
        have = _fingerprint(cand)
        if not have:
            continue
        if want in have or have in want:
            return True
    return False


# Vendor/filler words carry no discriminating power: 'NVIDIA H200' and
# 'NVIDIA RTX PRO 6000' share only 'nvidia', which must NEVER count as a match.
_GPU_VENDOR_WORDS = frozenset({"nvidia", "amd", "gpu", "the", "series", "smx", "sxm", "pcie"})


def _gpu_core_tokens(name: str) -> set[str]:
    """The DISCRIMINATING tokens of a GPU name: model digits/letters
    ('h200', '6000', 'rtx', 'mi325x'), minus vendor and interface filler."""
    return {t for t in _model_tokens(name) if t not in _GPU_VENDOR_WORDS}


def _gpu_matches(gpu_name: str, entry_gpu: str | None) -> bool:
    """Match on the GPU MODEL, never on the vendor.

    The library's ``gpu`` value comes from parse_setup, which already
    normalizes the filename's legacy markers ('H200Sx4' -> 'NVIDIA H200
    (PCIe)'), so both sides are canonical names: 'NVIDIA H200 SXM' vs
    'NVIDIA H200 (PCIe)' compare on the model token 'h200' and match.
    'NVIDIA RTX PRO 6000' shares only the vendor word 'nvidia' and must NOT
    match — a loose token-intersection test wrongly matched these and would
    have surfaced an RTX PRO 6000 benchmark for an H200 query.
    """
    if not entry_gpu:
        return False
    want = _gpu_core_tokens(gpu_name)
    have = _gpu_core_tokens(str(entry_gpu))
    if not want or not have:
        return False
    # subset over the discriminating tokens: {h200} vs {h200} matches;
    # {h200} vs {rtx, pro, 6000} does not
    smaller, larger = (want, have) if len(want) <= len(have) else (have, want)
    return smaller <= larger


def find_measured(
    model_ref: str,
    gpu_name: str,
    gpu_count: int = 0,
    tp: int = 0,
    library: dict | None = None,
) -> list[dict]:
    """Measured benchmark setups for this model + GPU, most relevant first.

    ``library`` lets callers/tests inject a scan_library() payload; None means
    scan the live results tree. Returns [] when nothing matches (the caller
    then shows no throughput panel at all). Each entry carries the measured
    rows plus provenance so the reader can open the artifact.
    """
    if library is None:
        try:
            from .library import scan_library

            library = scan_library()
        except Exception:  # pragma: no cover — the panel is optional
            return []
    if not isinstance(library, dict):
        return []

    out: list[dict] = []
    for model in library.get("models") or []:
        slug = str(model.get("slug") or "")
        for setup in model.get("setups") or []:
            if setup.get("kind") != "chat":  # measured runs only
                continue
            rows = setup.get("rows") or []
            if not rows:
                continue
            artifact_model = str(setup.get("model") or "")
            if not _slug_matches(model_ref, slug, artifact_model):
                continue
            if gpu_name and not _gpu_matches(gpu_name, setup.get("gpu")):
                continue
            # exact-shape preference: same GPU count / TP when the setup says
            same_gpus = bool(gpu_count) and int(setup.get("gpu_count") or 0) == int(gpu_count)
            out.append(
                {
                    "id": setup.get("id"),
                    "file": setup.get("file"),
                    "label": setup.get("label"),
                    "setup": setup.get("setup"),
                    "model": artifact_model or slug,
                    "gpu": setup.get("gpu"),
                    "gpu_count": setup.get("gpu_count"),
                    "engine": setup.get("engine"),
                    "weights": setup.get("weights"),
                    "hicache": setup.get("hicache"),
                    "replicas": setup.get("replicas"),
                    "status": setup.get("status"),
                    "same_shape": same_gpus,
                    "rows": _slim_rows(rows),
                    "columns": [{"key": k, "label": lbl} for k, lbl in _METRIC_COLUMNS],
                }
            )
    # exact shape first, then most context/user coverage
    out.sort(key=lambda e: (not e["same_shape"], -len(e["rows"])))
    return out


def _slim_rows(rows: list[dict]) -> list[dict]:
    """Keep only the measured columns worth showing (drop nulls)."""
    slim: list[dict] = []
    for r in rows:
        metrics = r.get("metrics") or {}
        keep = {k: metrics.get(k) for k, _lbl in _METRIC_COLUMNS if metrics.get(k) is not None}
        if not keep:
            continue
        slim.append({"label": r.get("label"), **keep})
    return slim


def active_params_summary(cfg: Any) -> dict | None:
    """The ACTIVE parameter count per token (architecture fact, not a
    prediction): attention + dense MLPs + the routed top-k experts + shared
    experts + embeddings. Shown as model structure; carries no throughput."""
    try:
        params, note = active_params_per_token(cfg)
    except Exception:  # pragma: no cover — structural extra, never fatal
        return None
    if not params:
        return None
    return {"params": params, "params_b": round(params / 1e9, 1), "note": note}
