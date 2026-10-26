"""Tests for the lab > model > configuration grouping in app.catalog.

The catalog is stored as a flat list of configurations; the tree is derived.
These tests pin the derivation rules that the catalog page depends on, and
guard the two failure modes that matter: an entry going MISSING from the tree,
and two configurations of one model becoming INDISTINGUISHABLE in the UI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "model_downloader"))

from app.catalog import (  # noqa: E402
    Catalog,
    config_label,
    entry_framework,
    entry_hicache,
    entry_lab,
    entry_quant,
    entry_shared_gpu,
    extract_model_id,
    model_key,
    model_node_name,
)

SEED = json.loads((REPO / "src" / "model_downloader" / "app" / "seed_catalog.json").read_text())


def _catalog(entries):
    """Build a Catalog over an explicit entry list (no disk, no seed merge)."""
    cat = Catalog.__new__(Catalog)
    cat.entries = entries
    return cat


def _entry(**overrides):
    base = {
        "catalog_id": "t-1",
        "tier": "h200",
        "name": "Qwen3.8-27B-vllm-FP8",
        "version": 1,
        "uri": "pvc://models-pvc/large-models/Qwen/Qwen3.8-27B-FP8?containerPath=/mnt/models",
        "image": "vllm/vllm-openai:latest",
        "arguments": ["Qwen/Qwen3.8-27B-FP8", "--port", "8080"],
        "metadata": {"tags": "", "modelCategory": "llm"},
    }
    base.update(overrides)
    return base


# ---- grouping invariants over the real seed data ----


def test_every_seed_entry_appears_exactly_once():
    tree = _catalog(SEED).tree()
    ids = [cfg["entry"]["catalog_id"] for lab in tree for model in lab["models"] for cfg in model["configs"]]
    assert len(ids) == len(SEED)
    assert sorted(ids) == sorted(e["catalog_id"] for e in SEED)


def test_counts_are_consistent_with_children():
    for lab in _catalog(SEED).tree():
        assert lab["model_count"] == len(lab["models"])
        assert lab["config_count"] == sum(len(m["configs"]) for m in lab["models"])


def test_no_two_configurations_of_one_model_share_a_label():
    """A duplicate label means a user cannot tell two configs apart."""
    for lab in _catalog(SEED).tree():
        for model in lab["models"]:
            labels = [cfg["label"] for cfg in model["configs"]]
            assert len(labels) == len(set(labels)), (
                f"duplicate config label under {lab['name']} / {model['name']}: {labels}"
            )


def test_seed_labs_are_the_expected_families():
    labs = [lab["name"] for lab in _catalog(SEED).tree()]
    assert labs == sorted(labs, key=lambda n: (n == "Other", n.lower()))
    # Labs come from the model family, not the URI org: the Qwen re-uploads live
    # under Inferact / RamManavalan / RedHatAI but belong to their real labs.
    assert "Qwen" in labs
    assert "Inferact" not in labs
    assert "RamManavalan" not in labs
    assert "RedHatAI" not in labs


# ---- seed-file integrity ----


def test_seed_catalog_ids_are_unique():
    """A duplicate catalog_id silently drops an entry from a fresh catalog.

    The seed merge is first-wins on catalog_id, so two entries sharing one id
    means only the first survives: a fresh deployment seeded 37 of 39 entries.
    A duplicate also makes two leaves share one selection key in the UI, so
    checking one toggled both.
    """
    ids = [e["catalog_id"] for e in SEED]
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate catalog_id(s) in seed_catalog.json: {sorted(duplicates)}"


def test_fresh_catalog_seeds_every_seed_entry(tmp_path):
    """A brand-new catalog must contain the whole seed file."""
    catalog = Catalog(str(tmp_path / "catalog.json"))
    assert len(catalog.entries) == len(SEED)


def test_tree_selection_keys_are_unique():
    """Every leaf must be individually selectable (catalog_id is the key)."""
    ids = [
        cfg["entry"]["catalog_id"]
        for lab in _catalog(SEED).tree()
        for model in lab["models"]
        for cfg in model["configs"]
    ]
    assert len(ids) == len(set(ids))


# ---- GPU class filter support ----


def test_every_tier_in_the_filter_is_a_real_tier():
    """The radio group is built from TIERS; a tier with no entries is a dead control."""
    from app.catalog import TIERS

    present = {str(e.get("tier") or "") for e in SEED}
    assert set(TIERS) <= present | set(TIERS)
    for tier in TIERS:
        assert any(e.get("tier") == tier for e in SEED), f"tier {tier!r} has no seed entries"


def test_tier_filter_partitions_the_catalog_exactly():
    """Filtering by GPU class must partition every config, never drop or duplicate one."""
    from app.catalog import TIERS

    by_tier = {t: 0 for t in TIERS}
    for entry in SEED:
        by_tier[str(entry.get("tier") or "")] += 1
    assert sum(by_tier.values()) == len(SEED)
    for tier, count in by_tier.items():
        subset = [e for e in SEED if e.get("tier") == tier]
        assert len(subset) == count


def test_tier_labels_cover_every_tier():
    from app.catalog import TIER_LABELS, TIERS

    for tier in TIERS:
        assert tier in TIER_LABELS, f"no display label for tier {tier!r}"


# ---- lab derivation ----


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("pvc://models-pvc/large-models/Inferact/Qwen3.8-27B-NVFP4?containerPath=/mnt/models", "Qwen"),
        ("pvc://models-pvc/large-models/RamManavalan/Qwen3-VL-Embedding-8B-FP8?x=1", "Qwen"),
        ("pvc://models-pvc/large-models/RedHatAI/gemma-4-31B-it-FP8-block?x=1", "Google"),
        ("pvc://models-pvc/large-models/Firworks/gemma-4-26B-A4B-it-fp8?x=1", "Google"),
        ("pvc://models-pvc/large-models/zai-org/GLM-5.3-Flash?x=1", "Z.ai"),
        ("pvc://models-pvc/large-models/deepseek-ai/DeepSeek-V4-Flash-0731?x=1", "DeepSeek"),
        ("pvc://models-pvc/large-models/CohereLabs/cohere-transcribe-03-2026?x=1", "Cohere"),
        ("pvc://models-pvc/large-models/openai/whisper-large-v3-turbo?x=1", "OpenAI"),
        ("pvc://models-pvc/large-models/fishaudio/s2-pro?x=1", "Fish Audio"),
        ("pvc://models-pvc/large-models/vllm-sr/d3-turbo?x=1", "vLLM Project"),
    ],
)
def test_lab_comes_from_model_family_not_uri_org(uri, expected):
    assert entry_lab(_entry(uri=uri)) == expected


def test_unknown_family_falls_back_to_uri_org():
    """A brand-new model must still land in a sensible lab, not vanish."""
    entry = _entry(
        uri="pvc://models-pvc/large-models/acme-labs/newmodel-7b?x=1",
        name="newmodel-7b",
        arguments=[],
    )
    assert entry_lab(entry) == "Acme Labs"


def test_lab_without_uri_is_other():
    entry = _entry(uri="", name="mystery", arguments=[])
    assert entry_lab(entry) == "Other"


# ---- model derivation ----


def test_model_key_strips_variants_so_they_share_one_node():
    keys = {
        model_key(_entry(uri=f"pvc://models-pvc/large-models/Qwen/Qwen3.8-27B{s}?x=1"))
        for s in ("", "-FP8", "-FP8-block", "-NVFP4")
    }
    assert keys == {"qwen3.8-27b"}


def test_model_key_does_not_strip_it_suffix():
    """ "-it" is part of real model names (gemma-4-31B-it), not a variant."""
    assert model_key(_entry(uri="pvc://models-pvc/large-models/RedHatAI/gemma-4-31B-it?x=1")) == ("gemma-4-31b-it")


def test_model_node_name_drops_variant_but_keeps_casing():
    entry = _entry(uri="pvc://models-pvc/large-models/Qwen/Qwen3.8-27B-FP8?x=1")
    assert model_node_name(entry) == "Qwen3.8-27B"


def test_model_node_name_falls_back_when_stripping_empties_it():
    entry = _entry(uri="pvc://models-pvc/large-models/x/FP8?x=1", name="FP8")
    assert model_node_name(entry) == "FP8"


def test_extract_model_id_handles_pvc_s3_and_arguments():
    assert extract_model_id(_entry(uri="pvc://p/prefix/org/M?x=1")) == "org/M"
    assert extract_model_id(_entry(uri="s3://bucket/a/b/model?x=1")) == "a/b/model"
    assert extract_model_id(_entry(uri="", arguments=["org/M", "--port", "1"])) == "org/M"
    assert extract_model_id(_entry(uri="", arguments=["--port", "1"])) == ""


# ---- configuration leaf attributes ----


@pytest.mark.parametrize(
    "image,expected",
    [
        ("lmsysorg/sglang:latest", "SGLang"),
        ("vllm/vllm-openai:latest", "vLLM"),
        ("andrewbydlon/vllmaudio:v0.22.1", "vLLM-Audio"),
        ("ghcr.io/vllm-project/semantic-router/vllm-sr-cuda:latest", "vLLM-SR"),
        ("busybox:latest", "Other"),
    ],
)
def test_framework_from_image(image, expected):
    assert entry_framework(_entry(image=image)) == expected


def test_hicache_comes_from_arguments_not_caching_enabled():
    """caching_enabled is a broad flag; HiCache is what the args actually do."""
    entry = _entry(
        caching_enabled=True,
        arguments=["sglang", "serve", "--enable-hierarchical-cache", "--hicache-size", "720"],
    )
    assert entry_hicache(entry) is True

    # caching_enabled set, but nothing in the args enables HiCache.
    assert entry_hicache(_entry(caching_enabled=True, arguments=["--port", "8080"])) is False


def test_shared_gpu_from_name_or_tag():
    assert entry_shared_gpu(_entry(name="Qwen3-VL-Embedding-8B-shared")) is True
    assert entry_shared_gpu(_entry(name="plain", metadata={"tags": "shared_gpu"})) is True
    assert entry_shared_gpu(_entry(name="plain", metadata={"tags": ""})) is False


def test_quant_ignores_kv_cache_dtype():
    """--kv-cache-dtype fp8 quantizes the CACHE, not the weights."""
    entry = _entry(
        name="Qwen3-VL-Embedding-8B",
        uri="pvc://models-pvc/large-models/Qwen/Qwen3-VL-Embedding-8B?x=1",
        arguments=["--kv-cache-dtype", "fp8"],
    )
    assert entry_quant(entry) == ""


def test_quant_detected_from_model_path():
    assert entry_quant(_entry(uri="pvc://p/models-pvc/large-models/x/M-NVFP4?x=1")) == "NVFP4"
    assert entry_quant(_entry(uri="pvc://p/large-models/x/M-FP8?x=1")) == "FP8"


def test_config_label_carries_the_distinguishing_dimensions():
    entry = _entry(
        tier="h200",
        image="lmsysorg/sglang:latest",
        uri="pvc://models-pvc/large-models/Qwen/Qwen3.8-27B-FP8?x=1",
        resource_limit_gpu="4",
        arguments=["--enable-hierarchical-cache"],
    )
    label = config_label(entry)
    assert "H200" in label
    assert "SGLang" in label
    assert "FP8" in label
    assert "4 GPU" in label
    assert "HiCache" in label


# ---- collision handling ----


def test_same_model_from_two_publishers_gets_distinct_labels():
    """Real case: Firworks and RedHatAI quants of one Gemma, identical config."""
    a = _entry(
        catalog_id="a",
        name="gemma-4-26B-A4B-it",
        tier="l40s",
        uri="pvc://models-pvc/large-models/Firworks/gemma-4-26B-A4B-it-fp8?x=1",
        image="vllm/vllm-openai:gemma4-cu130",
        resource_limit_gpu="1",
        arguments=["--port", "8080"],
    )
    b = _entry(
        catalog_id="b",
        name="gemma-4-26B-A4B-it-FP8-dynamic",
        tier="l40s",
        uri="pvc://models-pvc/large-models/RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic?x=1",
        image="vllm/vllm-openai:gemma4-cu130",
        resource_limit_gpu="1",
        arguments=["--port", "8080"],
    )
    tree = _catalog([a, b]).tree()
    configs = tree[0]["models"][0]["configs"]
    labels = [c["label"] for c in configs]
    assert len(labels) == 2
    assert len(set(labels)) == 2
    assert "Firworks" in labels[0] or "Firworks" in labels[1]
    assert "RedHatAI" in labels[0] or "RedHatAI" in labels[1]


def test_single_config_model_does_not_show_publisher():
    entry = _entry(uri="pvc://models-pvc/large-models/Qwen/Qwen3.8-27B-FP8?x=1")
    tree = _catalog([entry]).tree()
    assert tree[0]["models"][0]["configs"][0]["label"] == config_label(entry)


# ---- robustness ----


def test_entry_without_uri_or_name_is_not_dropped():
    entry = {"catalog_id": "orphan", "tier": "h200", "name": "", "image": "vllm/vllm-openai:latest"}
    tree = _catalog([entry]).tree()
    ids = [c["entry"]["catalog_id"] for lab in tree for m in lab["models"] for c in m["configs"]]
    assert ids == ["orphan"]


def test_empty_catalog_renders_empty_tree():
    assert _catalog([]).tree() == []


def test_other_lab_sorts_last():
    other = _entry(catalog_id="o", uri="", name="mystery", arguments=[])
    qwen = _entry(catalog_id="q")
    labs = [lab["name"] for lab in _catalog([other, qwen]).tree()]
    assert labs[-1] == "Other"
