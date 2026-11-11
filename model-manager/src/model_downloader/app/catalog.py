"""PVC-backed model catalog with GPU-tier grouping.

The catalog is a JSON file on a small dedicated PVC.  If the file is
missing it is seeded from seed_catalog.json bundled in the image.  On
every start, seed entries not yet present (and not explicitly removed)
are merged in, so new seed entries ship with an image upgrade without
overwriting user edits or resurrecting removed entries.  Each entry is a
full packaged_models config plus a catalog_id (uuid or seed-* name) and
a tier (h200 | rtx-pro-6000 | l40s).  An entry may also carry an optional
catalog-only ``eval`` block recording the measured quality of a quantised
variant (see the EVAL section below): it is validated when the entry enters
the catalog, surfaced as an "unvalidated" badge when absent on a quantised
entry, and never pushed to MLIS.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime
from pathlib import Path

SEED_FILE = Path(__file__).parent / "seed_catalog.json"
TIERS = ["h200", "rtx-pro-6000", "l40s"]
TIER_LABELS = {"h200": "H200", "rtx-pro-6000": "RTX Pro 6000", "l40s": "L40S"}

# ---- Lab / model / configuration derivation for the hierarchical catalog ----
#
# The catalog is authored as a FLAT list of configurations: one entry per
# (model, GPU, serving framework, quant, cache mode) combination.  The UI
# presents it as a three-level tree -- lab > model > configuration -- so the
# grouping below is pure derivation: it adds no fields to the stored entries
# and is recomputed on every request.  That keeps the on-disk catalog (and the
# Push-to-MLIS payload) byte-identical to what users authored.
#
# Lab is keyed off the MODEL FAMILY, deliberately NOT off the org segment of
# the URI.  The org is often a quantizer or a personal re-upload rather than
# the lab: Inferact/Qwen3.8-27B-NVFP4 and
# RamManavalan/Qwen3-VL-Embedding-8B-FP8 are Qwen models, and
# RedHatAI/gemma-4-31B-it-FP8-block is Google Gemma.  Grouping by org would
# scatter one lab across several "labs" and put two labs under one.  Curated
# prefixes fix that; anything unmatched falls back to the org so a newly added
# model is never dropped or misfiled.
LAB_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("qwen",), "Qwen"),
    (("glm",), "Z.ai"),
    (("deepseek",), "DeepSeek"),
    (("gemma",), "Google"),
    (("cohere",), "Cohere"),
    (("whisper",), "OpenAI"),
    (("fishaudio", "s2-"), "Fish Audio"),
    (("d3-turbo", "vllm-sr"), "vLLM Project"),
)

# Trailing tokens stripped when reducing a model path to its base-model key,
# so every quant / engine / cache / placement variant of one model lands under
# a single model node.  The strip loop repeats until stable, which is what
# handles chains like -FP8-block.
#
# "it" is intentionally absent: it is part of real model names
# (gemma-4-31B-it), not a variant suffix -- stripping it would merge the
# instruction-tuned Gemma with an unrelated base model.
VARIANT_TOKENS = (
    "fp8",
    "nvfp4",
    "fp4",
    "bf16",
    "fp16",
    "int8",
    "int4",
    "awq",
    "gptq",
    "gguf",
    "block",
    "dynamic",
    "e4m3",
    "e5m2",
    "vllm",
    "sglang",
    "hicache",
    "hicachex3",
    "nohicache",
    "shared",
    "dedicated",
    "replicas",
)
_VARIANT_RE = re.compile(
    r"-?(?:" + "|".join(re.escape(t) for t in VARIANT_TOKENS) + r")(?:x\d+)?$",
    re.IGNORECASE,
)

# Weight-quantization levels, matched ONLY against the model path and the entry
# name.  Deliberately not matched against arguments: a vLLM config carrying
# --kv-cache-dtype fp8 is FP8 in its KV CACHE, not in its weights, and
# labelling it "FP8" would misdescribe the model.
QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])(NVFP4|FP8|FP4|BF16|FP16|INT8|INT4|AWQ|GPTQ|GGUF)(?![A-Za-z0-9])",
    re.IGNORECASE,
)

FRAMEWORKS: tuple[tuple[str, str], ...] = (
    ("semantic-router", "vLLM-SR"),
    ("vllmaudio", "vLLM-Audio"),
    ("sglang", "SGLang"),
    ("vllm", "vLLM"),
)


def extract_model_id(entry: dict) -> str:
    """Model path from the entry URI, mirroring the JS helper.

    pvc://pvc-name/prefix/org/Model -> org/Model; s3://bucket/key -> key.
    Falls back to the first org/name-shaped argument, which is how an entry
    without a URI names its model.
    """
    uri = entry.get("uri") or ""
    m = re.match(r"^pvc://[^/?]+/[^/]+/([^?]+)", uri)
    if m:
        return m.group(1).rstrip("/")
    m = re.match(r"^s3://[^/?]+/([^?]+)", uri)
    if m:
        # Strip any ?query the way the pvc branch does: an s3 URI can carry
        # the same ?containerPath-style suffix, and leaving it in would put
        # "model?x=1" in the model path and split one model into two nodes.
        return m.group(1).rstrip("/")
    for arg in entry.get("arguments") or []:
        if isinstance(arg, str) and re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", arg):
            return arg
    return ""


def model_display_name(entry: dict) -> str:
    """Human-facing model name: the URI model path without the org segment."""
    model_id = extract_model_id(entry)
    if model_id:
        return model_id.rsplit("/", 1)[-1]
    return str(entry.get("name") or "")


def _strip_variants(name: str) -> str:
    """Drop trailing variant tokens, keeping the original casing."""
    key = re.sub(r"[\s_]+", "-", name)
    previous = None
    while previous != key:
        previous = key
        key = _VARIANT_RE.sub("", key)
    return key.strip("-")


def model_key(entry: dict) -> str:
    """Stable grouping key for the model node (variant suffixes stripped)."""
    key = _strip_variants(model_display_name(entry) or str(entry.get("name") or ""))
    return key.lower() or str(entry.get("catalog_id") or "")


def model_node_name(entry: dict) -> str:
    """Display name for the model node: base model, variants removed.

    The node represents the MODEL, not one packaging of it, so its label must
    not carry a quant/engine suffix -- otherwise "Qwen3.8-27B-FP8" would sit
    above configs that include an NVFP4 build.  Falls back to the raw name
    when stripping would leave nothing (a model genuinely named "FP8").
    """
    raw = model_display_name(entry) or str(entry.get("name") or "")
    stripped = _strip_variants(raw)
    return stripped or raw


def entry_quant(entry: dict) -> str:
    """Weight-quant level of the model, or "" when the entry does not say."""
    blob = model_display_name(entry) + " " + str(entry.get("name") or "")
    found = QUANT_RE.search(blob)
    return found.group(1).upper() if found else ""


def entry_framework(entry: dict) -> str:
    image = str(entry.get("image") or "").lower()
    for needle, label in FRAMEWORKS:
        if needle in image:
            return label
    return "Other"


def entry_hicache(entry: dict) -> bool:
    """True when the serving args actually enable the hierarchical cache.

    Derived from the arguments rather than caching_enabled because that field
    is a broad "model caching is on" flag set on most entries, including ones
    that never enable HiCache.
    """
    for arg in entry.get("arguments") or []:
        text = str(arg).lower()
        if "hierarchical" in text or "hicache" in text:
            return True
    return False


def entry_hicache_storage_backend(entry: dict) -> str:
    """L3 (storage) backend named by --hicache-storage-backend, or "".

    HiCache L2 lives in host memory and needs no L3 backend at all, so this is
    deliberately separate from entry_hicache(): the backend is what tells two
    otherwise identical HiCache configurations apart (kernel/CPU pool vs the
    shared Mooncake store).
    """
    args = [str(arg).strip() for arg in entry.get("arguments") or []]
    for i, arg in enumerate(args):
        lowered = arg.lower()
        if lowered == "--hicache-storage-backend" and i + 1 < len(args):
            return args[i + 1].lower()
        if lowered.startswith("--hicache-storage-backend="):
            return arg.split("=", 1)[1].strip().lower()
    return ""


def entry_mooncake(entry: dict) -> bool:
    """True when the HiCache L3 tier is the Mooncake store.

    Either signal is enough: the storage-backend argument is what SGLang
    actually reads, and the MOONCAKE_* client environment is what a hand-added
    entry (or an entry copied from a live ISVC) is likely to carry.
    """
    if entry_hicache_storage_backend(entry) == "mooncake":
        return True
    # An env-only signal is trusted only when HiCache is actually enabled:
    # MOONCAKE_* vars on an entry with no hierarchical cache are inert, and
    # labelling that entry "Mooncake" (plus the RDMA caveat) would be a lie.
    if not entry_hicache(entry):
        return False
    environment = entry.get("environment") or {}
    return any(str(key).upper().startswith("MOONCAKE_") for key in environment)


# Catalog-only caveat attached to Mooncake configurations.  DERIVED, never
# stored: the seed merge only ADDS entries, so a caveat authored into
# seed_catalog.json would never reach a catalog that already holds the entry --
# and it would ride into the Push-to-MLIS payload.  Deriving it from the
# arguments makes it appear for existing catalogs and keeps the pushed config
# byte-identical to what the user authored.
MOONCAKE_RDMA_CAVEAT: dict = {
    "id": "mooncake-rdma",
    "chip": "RDMA setup",
    "title": "Mooncake needs a patch script for RDMA",
    "body": (
        "The catalog cannot express an RDMA client: the serving pod needs "
        "/dev/infiniband, IPC_LOCK and its own MOONCAKE_LOCAL_HOSTNAME/HOST_ID. "
        "Run scripts/patch-model-mooncake.sh against the deployed ISVC before "
        "you rely on the L3 tier. Without it Mooncake runs over TCP "
        "(MOONCAKE_PROTOCOL=tcp): functional, but not zero-copy -- and it "
        "falls back silently."
    ),
    "commands": [
        {
            "label": "enable RDMA",
            "command": "scripts/patch-model-mooncake.sh --isvc <isvc> --protocol rdma",
        },
        {
            "label": "verify transport",
            "command": ('kubectl -n mooncake logs mooncake-store-0 | grep -iE "tcp_transport|memcpy|infiniband"'),
        },
    ],
}


def entry_caveats(entry: dict) -> list[dict]:
    """Operational caveats the UI shows for a configuration (never pushed).

    A caveat is something the catalog cannot express in the config itself, so
    the user has to do it by hand after deploy.  Returned as a copy so callers
    cannot mutate the module constant.
    """
    if not entry_mooncake(entry):
        return []
    return [
        dict(
            MOONCAKE_RDMA_CAVEAT,
            commands=[dict(command) for command in MOONCAKE_RDMA_CAVEAT["commands"]],
        )
    ]


# ---------------------------------------------------------------------------
# Optional quality evaluation (eval) block
# ---------------------------------------------------------------------------
#
# Catalog entries carry deployment facts (uri, image, args, env, resources,
# format, tier) — nothing in them says whether a quantised variant
# (FP8/NVFP4/...) is actually as good as the bf16 original, so an entry
# without a score is silently treated as equivalent.  The optional ``eval``
# object closes that gap:
#
#     "eval": {
#         "suite": "mmlu",                # required: which benchmark
#         "score": 84.2,                  # required: finite number, 0..100
#         "baseline_bf16": 86.0,          # optional: bf16 reference on the suite
#         "run_id": "20261028T120000Z",   # optional: the run that measured it
#         "date": "2026-10-28"            # optional: ISO date of the measurement
#     }
#
# The block is stored verbatim and is CATALOG-ONLY: it never rides into the
# Push-to-MLIS payload (db.COLUMNS has no "eval" column) and is not part of
# the configuration fingerprint (versioning.FINGERPRINT_COLUMNS), so adding
# one to an entry changes neither its MLIS identity nor its version.
#
# Full-precision entries (BF16/FP16) ARE the reference a quantised variant is
# judged against — they need no eval — so only a QUANTISED entry without one
# is "unvalidated" (badge in the UI, warning on push).


def _eval_score_ok(value, label: str, name: str) -> None:
    """Sanity-check one score-shaped eval field: a finite number in 0..100
    (both the 0-1 fraction and the 0-100 percent conventions are legal
    scores).  Raises ValueError naming the entry and the field."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(  # noqa: TRY004 - the catalog's validation idiom is ValueError (see validate_eval)
            f"{name!r}: eval.{label} must be a number, got {value!r}"
        )
    if not math.isfinite(value):
        raise ValueError(f"{name!r}: eval.{label} must be a finite number, got {value!r}")
    if not 0 <= value <= 100:
        raise ValueError(f"{name!r}: eval.{label} must be in 0..100, got {value!r}")


def validate_eval(eval_block, name: str = "") -> None:
    """Validate an entry's optional eval block, raising ValueError (naming the
    entry and the offending field) when malformed.  Absent/None is always
    valid — the field is optional.

    This is the catalog's validation idiom: a plain ValueError that the API
    layer turns into a 4xx/502 with a clear message (catalog add/batch and
    the GitHub refresh both surface it).  Unknown extra keys are allowed
    (forward compatibility with future eval sub-fields); the known keys are
    enforced.
    """
    if eval_block is None:
        return
    if not isinstance(eval_block, dict):
        raise ValueError(f"{name!r}: eval must be an object, got {eval_block!r}")  # noqa: TRY004
    suite = eval_block.get("suite")
    if not isinstance(suite, str) or not suite.strip():
        raise ValueError(f"{name!r}: eval.suite is required (non-empty string)")
    score = eval_block.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError(f"{name!r}: eval.score is required (finite number in 0..100)")  # noqa: TRY004
    _eval_score_ok(score, "score", name)
    # baseline_bf16 is OPTIONAL (the documented shape is float|None): only
    # validate it when the entry actually carries one. Passing the absent
    # key's None through the score check rejected every entry that omitted
    # the baseline — which is most of them.
    baseline = eval_block.get("baseline_bf16")
    if baseline is not None:
        _eval_score_ok(baseline, "baseline_bf16", name)
    run_id = eval_block.get("run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise ValueError(f"{name!r}: eval.run_id must be a string when present, got {run_id!r}")
    date = eval_block.get("date")
    if date is not None:
        if not isinstance(date, str):
            raise ValueError(f"{name!r}: eval.date must be an ISO date (YYYY-MM-DD) string, got {date!r}")
        try:
            # Format validation only: a date literal has no timezone, so
            # there is nothing to localise (DTZ007 is about aware instants).
            datetime.strptime(date, "%Y-%m-%d")  # noqa: DTZ007
        except ValueError:
            raise ValueError(f"{name!r}: eval.date {date!r} is not a valid ISO date (YYYY-MM-DD)") from None


def entry_eval(entry: dict) -> dict | None:
    """The entry's eval block, or None when absent or malformed.

    Defensive by design, like every other entry_* derivation: a bad block is
    treated as absent here so a rogue entry can never crash the tree render
    or the push path — REJECTION happens at the entry boundary
    (validate_eval in Catalog.add / merge_entries), not at render time.
    """
    eval_block = entry.get("eval")
    if not isinstance(eval_block, dict):
        return None
    suite = eval_block.get("suite")
    score = eval_block.get("score")
    if not isinstance(suite, str) or not suite.strip():
        return None
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
        return None
    return eval_block


# Weight levels that ARE the model itself (full precision) — the reference a
# quantised variant is judged against.  Everything else entry_quant() can
# detect is a quantised/derived variant.
_FULL_PRECISION_QUANTS = frozenset({"BF16", "FP16"})


def entry_unvalidated(entry: dict) -> bool:
    """True when the entry is a QUANTISED variant with no recorded eval.

    A full-precision entry (BF16/FP16) is the baseline itself, and an entry
    whose quant cannot be determined ("") is not claimed to be quantised —
    neither earns the badge.  This drives both the catalog UI's
    "unvalidated" badge and the MLIS push warning.
    """
    quant = entry_quant(entry)
    if not quant or quant in _FULL_PRECISION_QUANTS:
        return False
    return entry_eval(entry) is None


def entry_shared_gpu(entry: dict) -> bool:
    """True when the config is meant to share a GPU with other models."""
    tags = str((entry.get("metadata") or {}).get("tags") or "").lower()
    name = str(entry.get("name") or "").lower()
    return "shared" in name or "shared_gpu" in tags


def entry_lab(entry: dict) -> str:
    """Lab that publishes the model family (falls back to the URI org)."""
    family = model_key(entry)
    for prefixes, label in LAB_PREFIXES:
        if any(family.startswith(p) for p in prefixes):
            return label
    model_id = extract_model_id(entry)
    if "/" in model_id:
        org = model_id.split("/", 1)[0]
        return org.replace("-", " ").replace("_", " ").title()
    return "Other"


def entry_gpus(entry: dict) -> str:
    for field in ("resource_limit_gpu", "resource_request_gpu"):
        value = entry.get(field)
        if value not in (None, ""):
            return str(value)
    return "0"


def entry_publisher(entry: dict) -> str:
    """Org segment of the model path (the repo the weights came from).

    Shown only when two configurations of the same model are otherwise
    indistinguishable: the catalog really does carry the same model from two
    different publishers (e.g. a Firworks FP8 quant and a RedHatAI
    compressed-tensors quant of gemma-4-26B-A4B-it, same served-model-name),
    and without the publisher the two leaves would read identically.
    """
    model_id = extract_model_id(entry)
    return model_id.split("/", 1)[0] if "/" in model_id else ""


def config_label(entry: dict, *, with_publisher: bool = False) -> str:
    """Compact one-line label for a configuration leaf.

    Carries the dimensions a user actually chooses between -- GPU class,
    serving framework, quantization, GPU count, HiCache, its L3 backend
    (Mooncake), shared-GPU -- so two leaves under one model node are
    distinguishable without opening either.
    """
    tier = str(entry.get("tier") or "")
    parts = [TIER_LABELS.get(tier, tier or "\u2014"), entry_framework(entry)]
    quant = entry_quant(entry)
    if quant:
        parts.append(quant)
    parts.append(entry_gpus(entry) + " GPU")
    if entry_hicache(entry):
        parts.append("HiCache")
    # L3 tier: without it a Mooncake config and the L2-only config it was
    # cloned from render the same label, and the user cannot tell which one
    # carries the RDMA caveat.
    if entry_mooncake(entry):
        parts.append("Mooncake")
    if entry_shared_gpu(entry):
        parts.append("Shared")
    if with_publisher:
        publisher = entry_publisher(entry)
        if publisher:
            parts.append(publisher)
    return " \u00b7 ".join(parts)


# Per-GPU specs shown in the catalog tier headers: the quantization levels the
# card supports and its VRAM. NVFP4 needs Blackwell tensor cores, so only the
# RTX Pro 6000 lists it; H200 (Hopper) and L40S (Ada) do FP8.
TIER_INFO = {
    "h200": {"quants": ["FP8"], "vram_gb": 141},
    "rtx-pro-6000": {"quants": ["FP8", "NVFP4"], "vram_gb": 96},
    "l40s": {"quants": ["FP8"], "vram_gb": 48},
}


class Catalog:
    def __init__(self, path: str):
        self.path = Path(path)
        self.removed_path = self.path.with_name("removed_seeds.json")
        self.entries: list[dict] = []
        self._removed_seed_ids: set[str] = set()
        self._seed_ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        self._removed_seed_ids = self._read_removed_seeds()
        if self.path.exists():
            self.entries = json.loads(self.path.read_text())
        else:
            self.entries = []
        changed = self._normalize_versions()
        changed = self._merge_seed() or changed
        if changed or not self.path.exists():
            self._save()

    def _normalize_versions(self) -> bool:
        """Force every entry's version to 1 (MLIS assigns the real one at push).

        Entries pulled from a local MLIS arrive carrying that instance's
        version (v9 for DeepSeek, v8 for GLM), which made the same model a
        different version on every unit.  The version is an MLIS-side sequence
        now (versioning.plan_push), so the catalog stores 1 and the push result
        reports what was actually assigned.  Returns True when anything
        changed, so the caller saves the normalised catalog.
        """
        changed = False
        for entry in self.entries:
            if entry.get("version") != 1:
                entry["version"] = 1
                changed = True
        return changed

    def _read_removed_seeds(self) -> set[str]:
        if self.removed_path.exists():
            return set(json.loads(self.removed_path.read_text()))
        return set()

    def _write_removed_seeds(self) -> None:
        self.removed_path.parent.mkdir(parents=True, exist_ok=True)
        self.removed_path.write_text(json.dumps(sorted(self._removed_seed_ids), indent=2))

    def _merge_seed(self) -> bool:
        if not SEED_FILE.exists():
            return False
        seed_entries = json.loads(SEED_FILE.read_text())
        self._seed_ids = {e.get("catalog_id") for e in seed_entries if e.get("catalog_id")}
        return bool(self.merge_entries(seed_entries, source="seed")["added"])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2))

    def merge_entries(self, entries: list[dict], source: str = "seed") -> dict:
        """Merge external catalog entries (seed file or GitHub refresh).

        Same rules as the on-start seed merge: add only entries whose
        catalog_id is not present and not explicitly removed, so a refresh
        ships new entries without overwriting user edits or resurrecting
        removed ones.  Returns counters for the API response.
        """
        if not isinstance(entries, list):
            raise TypeError("catalog payload must be a JSON array of entries")
        present = {e.get("catalog_id") for e in self.entries}
        added = skipped = removed = 0
        for entry in entries:
            if not isinstance(entry, dict):
                skipped += 1
                continue
            cid = entry.get("catalog_id")
            if not cid:
                skipped += 1
                continue
            if cid in present:
                skipped += 1
            elif cid in self._removed_seed_ids:
                removed += 1
            else:
                # EVAL: a bad block in a seed/GitHub payload is a LOUD startup
                # failure (seed) or a 502 "invalid catalog JSON from GitHub" —
                # never a silently skipped entry.
                validate_eval(entry.get("eval"), str(entry.get("name") or ""))
                # Same normalisation as the on-load pass: a seed file or a
                # GitHub refresh may carry an inherited MLIS version.
                entry = dict(entry)
                entry["version"] = 1
                self.entries.append(entry)
                present.add(cid)
                added += 1
        if added:
            self._save()
        return {"source": source, "added": added, "skipped": skipped, "removed": removed}

    def all(self) -> list[dict]:
        return list(self.entries)

    def list_by_tier(self) -> dict[str, list[dict]]:
        by: dict[str, list[dict]] = {t: [] for t in TIERS}
        for e in self.entries:
            tier = e.get("tier", "")
            by.setdefault(tier, []).append(e)
        for tier, entries in by.items():
            entries.sort(key=lambda e: (e.get("name", "").lower(), e.get("catalog_id", "")))
        return by

    def tree(self) -> list[dict]:
        """Group entries into lab > model > configuration nodes.

        Returns a list of labs (alphabetical, "Other" last) each holding its
        models (alphabetical) each holding its configuration entries sorted by
        the displayed leaf label.  Every entry appears exactly once: entries
        that fail to derive a lab or model key fall into the "Other" lab /
        their own single-config model node rather than being dropped.
        """
        labs: dict[str, dict[str, list[dict]]] = {}
        for entry in self.entries:
            lab = entry_lab(entry)
            key = model_key(entry) or str(entry.get("catalog_id") or "")
            labs.setdefault(lab, {}).setdefault(key, []).append(entry)

        out: list[dict] = []
        for lab in sorted(labs, key=lambda name: (name == "Other", name.lower())):
            models = []
            for key in sorted(labs[lab]):
                entries = sorted(
                    labs[lab][key],
                    key=lambda e: (config_label(e).lower(), str(e.get("catalog_id") or "")),
                )
                models.append(
                    {
                        "key": key,
                        "name": model_node_name(entries[0]) or key,
                        "configs": self._config_nodes(entries),
                    }
                )
            out.append(
                {
                    "name": lab,
                    "model_count": len(models),
                    "config_count": sum(len(m["configs"]) for m in models),
                    "models": models,
                }
            )
        return out

    @staticmethod
    def _config_nodes(entries: list[dict]) -> list[dict]:
        """Build the configuration leaves for one model node.

        Leaves are labelled by their distinguishing dimensions.  When two of
        them still collide (the catalog genuinely carries the same model from
        two publishers), the publisher is appended to BOTH labels -- so a user
        can always tell two leaves apart, and a label never changes just
        because a sibling was added.
        """
        labels = [config_label(e) for e in entries]
        counts: dict[str, int] = {}
        for label in labels:
            counts[label] = counts.get(label, 0) + 1
        nodes = []
        for entry, label in zip(entries, labels):
            if counts[label] > 1:
                label = config_label(entry, with_publisher=True)
            nodes.append(
                {
                    "label": label,
                    "framework": entry_framework(entry),
                    "quant": entry_quant(entry),
                    "hicache": entry_hicache(entry),
                    "mooncake": entry_mooncake(entry),
                    "caveats": entry_caveats(entry),
                    # EVAL: the stored block (None when absent) plus the
                    # derived "unvalidated" flag the UI badges on.
                    "eval": entry_eval(entry),
                    "unvalidated": entry_unvalidated(entry),
                    "shared_gpu": entry_shared_gpu(entry),
                    "gpus": entry_gpus(entry),
                    "publisher": entry_publisher(entry),
                    "model_id": extract_model_id(entry),
                    "entry": entry,
                }
            )
        return nodes

    def add(self, entry: dict) -> dict:
        entry = dict(entry)
        # EVAL: reject a malformed block at the entry boundary (clear error
        # naming the entry and field) instead of storing it and discovering
        # the problem at render/push time.
        validate_eval(entry.get("eval"), str(entry.get("name") or ""))
        entry["catalog_id"] = uuid.uuid4().hex[:12]
        if entry.get("tier") not in TIERS:
            entry["tier"] = TIERS[0]
        # MLIS assigns the real version at push time (versioning.plan_push);
        # storing an authored one here is what made one model v9 on one unit
        # and v1 on another.
        entry["version"] = 1
        if entry.get("metadata") is None:
            entry["metadata"] = {"tags": "", "modelCategory": "other"}
        if entry.get("environment") is None:
            entry["environment"] = {}
        if entry.get("arguments") is None:
            entry["arguments"] = []
        if entry.get("project") is None:
            entry["project"] = ""
        if entry.get("caching_enabled") is None:
            entry["caching_enabled"] = False
        if entry.get("registry") is None:
            entry["registry"] = None
        self.entries.append(entry)
        self._save()
        return entry

    def remove(self, catalog_id: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.get("catalog_id") != catalog_id]
        if len(self.entries) < before:
            if catalog_id in self._seed_ids:
                self._removed_seed_ids.add(catalog_id)
                self._write_removed_seeds()
            self._save()
            return True
        return False
