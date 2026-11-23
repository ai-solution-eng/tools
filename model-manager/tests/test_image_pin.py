"""Tests for image pinning and the optional ``engine`` provenance block (ENGINE).

A catalog row names an image to pull, but a floating tag (``:latest``,
``:stable``) does not say WHICH build that is: the same row deploys different
inference engines on different days.  These tests pin down four things:

* image_is_pinned / entry_pinned -- digest and version-tag references count as
  pinned, floating aliases and untagged references do not, and a registry port
  is never mistaken for a tag separator;
* the seed catalog is fully pinned apart from an explicit, reasoned allowlist,
  so adding a new floating entry is a decision rather than an accident;
* the engine block is stored verbatim, validated at the entry boundary
  (Catalog.add / merge_entries), and surfaced on the config node;
* the engine block is OUTSIDE the MLIS configuration fingerprint -- recording
  provenance must never mint an MLIS version on its own; changing the image is
  what does that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "model_manager"))

from app.catalog import (  # noqa: E402
    Catalog,
    entry_engine,
    entry_pinned,
    image_is_pinned,
    validate_engine,
)
from app.versioning import FINGERPRINT_COLUMNS, config_fingerprint  # noqa: E402

SEED = json.loads((REPO / "src" / "model_manager" / "app" / "seed_catalog.json").read_text())

PINNED_IMAGE = "lmsysorg/sglang:v0.5.23-cu130"
SGLANG_DIGEST = "sha256:f832b5c39559c9caa900b22ad37c76fcce66470676855c42052102c537812f1f"
VALID_ENGINE = {
    "version": "v0.5.23-cu130",
    "digest": SGLANG_DIGEST,
    "validated": "2026-11-02",
    "source": "live-pod",
}

# Floating tags that are DELIBERATE, not oversights.  This set is a decision
# record, not a backlog: an entry lands here only when someone decided that
# tracking a moving tag is better than freezing a version, and the reason is
# recorded on the entry's own notes.  The "unpinned" badge stays visible on
# them by design -- it is telling the truth, not flagging a defect.
# Empty as of 2026-11-20. The two d3-turbo entries were the only deliberate
# floaters: upstream semantic-router was very new and moving fast, so a version
# pin would have gone stale faster than it could be validated (decision
# 2026-11-19).
#
# That trade-off no longer applies. Both entries now point at
# ghcr.io/ai-solution-eng/d3-turbo-router:v0.2.0, a wrapper we own that freezes
# the upstream base by commit tag AND platform digest. Pinning is now cheaper
# than floating, so the allowlist is empty by design -- not because nothing
# floats, but because nothing needs to.
KNOWN_UNPINNED: set[str] = set()


def _entry(**overrides):
    base = {
        "catalog_id": "t-pin-1",
        "tier": "h200",
        "name": "Qwen3.8-27B-sglang-FP8",
        "version": 1,
        "uri": "pvc://models-pvc/large-models/Qwen/Qwen3.8-27B-FP8?containerPath=/mnt/models",
        "image": PINNED_IMAGE,
        "arguments": ["Qwen/Qwen3.8-27B-FP8", "--port", "8080"],
        "metadata": {"tags": "", "modelCategory": "llm"},
    }
    base.update(overrides)
    return base


def _catalog(entries):
    """Build a Catalog over an explicit entry list (no disk, no seed merge)."""
    cat = Catalog.__new__(Catalog)
    cat.entries = entries
    cat._removed_seed_ids = set()
    return cat


def _config_node(entry):
    return Catalog._config_nodes([entry])[0]


# ---------------------------------------------------------------------------
# image_is_pinned: what counts as a pin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "image",
    [
        "lmsysorg/sglang:v0.5.23-cu130",
        "vllm/vllm-openai:v0.23.0-cu129",
        "andrewbydlon/vllmaudio:omni-v24.0",
        "vllm/vllm-openai:gemma4-cu130",
        "ghcr.io/local-inference-lab/vllm:release-10152026",
        f"lmsysorg/sglang@{SGLANG_DIGEST}",
        "host:5000/team/model:v1.2.3",  # registry port is not the tag
    ],
)
def test_version_tags_and_digests_are_pinned(image):
    assert image_is_pinned(image) is True


@pytest.mark.parametrize(
    "image",
    [
        "lmsysorg/sglang:latest",
        "vllm/vllm-openai:stable",
        "team/model:edge",
        "team/model:nightly",
        "team/model",  # untagged implies :latest
        "host:5000/team/model",  # port, still untagged
        "",
        "   ",
    ],
)
def test_floating_and_untagged_references_are_not_pinned(image):
    assert image_is_pinned(image) is False


def test_entry_pinned_reads_the_image_field():
    assert entry_pinned(_entry()) is True
    assert entry_pinned(_entry(image="vllm/vllm-openai:latest")) is False
    assert entry_pinned({"name": "no-image"}) is False


# ---------------------------------------------------------------------------
# The seed catalog: pinned, apart from the explicit backlog
# ---------------------------------------------------------------------------


def test_seed_catalog_is_pinned_except_the_allowlist():
    floating = {e["catalog_id"] for e in SEED if not entry_pinned(e)}
    assert floating == KNOWN_UNPINNED, (
        "a seed entry changed its pinning state; if this is a new floating entry, "
        "pin it or add it to KNOWN_UNPINNED with the reason"
    )


def test_allowlist_entries_still_exist_and_still_float():
    by_id = {e["catalog_id"]: e for e in SEED}
    for catalog_id in KNOWN_UNPINNED:
        assert catalog_id in by_id, f"stale allowlist entry {catalog_id!r}"
        assert entry_pinned(by_id[catalog_id]) is False, f"{catalog_id!r} is now pinned -- drop it from KNOWN_UNPINNED"


def test_pinned_sglang_entries_record_engine_provenance():
    pinned = [e for e in SEED if e.get("image") == PINNED_IMAGE]
    assert pinned, "expected the sglang entries to be pinned"
    for entry in pinned:
        engine = entry_engine(entry)
        assert engine is not None, f"{entry['catalog_id']!r}: pinned without an engine block"
        assert engine["version"] == "v0.5.23-cu130"
        assert engine["digest"] == SGLANG_DIGEST
        # validated is either an ISO date (boot-verified) or empty (not yet)
        assert engine["validated"] == "" or len(engine["validated"]) == 10


# ---------------------------------------------------------------------------
# validate_engine: the entry boundary
# ---------------------------------------------------------------------------


def test_validate_engine_accepts_none_and_valid_blocks():
    validate_engine(None)
    validate_engine(dict(VALID_ENGINE))
    # an unvalidated pin records empty strings, not None
    validate_engine({"version": "v0.5.23-cu130", "digest": SGLANG_DIGEST, "validated": "", "source": "unvalidated"})
    # unknown extra keys are forward-compatible
    validate_engine(dict(VALID_ENGINE, future_key=1))


@pytest.mark.parametrize(
    "block",
    [
        "not-an-object",
        ["nope"],
        {"version": 5},  # wrong type
        {"digest": "f832b5c3"},  # not a sha256: digest
        {"digest": "sha512:abc"},  # wrong algorithm
        {"validated": "02-11-2026"},  # not ISO
    ],
)
def test_validate_engine_rejects_malformed(block):
    with pytest.raises(ValueError):
        validate_engine(block, "Qwen3.8-27B")


def test_validate_engine_names_the_entry():
    with pytest.raises(ValueError, match="Qwen3.8-27B"):
        validate_engine({"validated": "yesterday"}, "Qwen3.8-27B")


# ---------------------------------------------------------------------------
# Catalog.add / merge_entries: stored verbatim, rejected when malformed
# ---------------------------------------------------------------------------


def test_add_stores_engine_verbatim(tmp_path):
    cat = Catalog(str(tmp_path / "catalog.json"))
    added = cat.add(_entry(engine=dict(VALID_ENGINE)))
    assert added["engine"] == VALID_ENGINE
    assert entry_engine(added) == VALID_ENGINE
    node = _config_node(added)
    assert node["engine"] == VALID_ENGINE
    assert node["pinned"] is True


def test_add_without_engine_invents_nothing(tmp_path):
    cat = Catalog(str(tmp_path / "catalog.json"))
    added = cat.add(_entry())
    assert "engine" not in added  # not invented, not defaulted
    assert entry_engine(added) is None


def test_add_rejects_malformed_engine(tmp_path):
    cat = Catalog(str(tmp_path / "catalog.json"))
    before = len(cat.entries)
    with pytest.raises(ValueError, match="engine.digest"):
        cat.add(_entry(engine={"digest": "not-a-digest"}))
    assert len(cat.entries) == before  # nothing half-added


def test_merge_entries_rejects_malformed_engine_and_adds_nothing():
    cat = _catalog([])
    with pytest.raises(ValueError, match="engine"):
        cat.merge_entries([_entry(engine={"version": 7})], source="test")
    assert cat.entries == []


def test_config_node_flags_unpinned():
    node = _config_node(_entry(image="vllm/vllm-openai:latest"))
    assert node["pinned"] is False
    assert node["engine"] is None


# ---------------------------------------------------------------------------
# The split that makes this safe: image is fingerprinted, provenance is not
# ---------------------------------------------------------------------------


def test_engine_block_is_outside_the_mlis_fingerprint():
    assert "engine" not in FINGERPRINT_COLUMNS
    base = _entry()
    with_engine = _entry(engine=dict(VALID_ENGINE))
    assert config_fingerprint(base) == config_fingerprint(with_engine)


def test_changing_the_image_does_mint_a_new_mlis_version():
    assert "image" in FINGERPRINT_COLUMNS
    floating = _entry(image="lmsysorg/sglang:latest")
    pinned = _entry(image=PINNED_IMAGE)
    assert config_fingerprint(floating) != config_fingerprint(pinned)


# ---------------------------------------------------------------------------
# The push path warns (never blocks) on an unpinned image
# ---------------------------------------------------------------------------


def _import_main(tmp_path, monkeypatch):
    if "psycopg2" not in sys.modules:  # pragma: no cover - environment shim
        _pg = types.ModuleType("psycopg2")
        _pg.__path__ = []
        _pg.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no db in tests"))
        _ext = types.ModuleType("psycopg2.extensions")
        _ext.ISOLATION_LEVEL_AUTOCOMMIT = 0
        _extras = types.ModuleType("psycopg2.extras")
        _extras.Json = lambda value: value
        sys.modules["psycopg2"] = _pg
        sys.modules["psycopg2.extensions"] = _ext
        sys.modules["psycopg2.extras"] = _extras
    monkeypatch.setenv("JOB_TEMPLATE_CONFIGMAP", "test-template")
    monkeypatch.setenv("JOB_TEMPLATE_CONFIGMAP_NAMESPACE", "test-ns")
    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "pin-test-catalog.json"))
    monkeypatch.setenv("STORAGE_BACKEND", "pvc")
    from model_manager.app import main as md

    return md


def test_push_warns_only_on_unpinned_images(monkeypatch, tmp_path, caplog):
    md = _import_main(tmp_path, monkeypatch)

    pushed = {}

    class _DB:
        async def push_batch(self, configs, project=""):
            pushed["configs"] = configs
            return [
                {"status": "pushed", "name": c.get("name"), "version": 1, "bumped": False, "detail": ""}
                for c in configs
            ]

    monkeypatch.setattr(md, "aioli_db", _DB())

    class _Req:
        def __init__(self, payload):
            self._p = payload

        async def json(self):
            return self._p

    unpinned = {"name": "qwen-floating", "image": "vllm/vllm-openai:latest"}
    pinned = {"name": "qwen-pinned", "image": PINNED_IMAGE}
    digested = {"name": "qwen-digest", "image": f"lmsysorg/sglang@{SGLANG_DIGEST}"}

    with caplog.at_level(logging.WARNING, logger="model_manager.app.main"):
        out = asyncio.run(md.push_models(_Req([unpinned, pinned, digested])))

    # the warning never blocks: all three still push
    assert out["pushed"] == 3
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING and "UNPINNED" in r.message]
    assert len(warnings) == 1
    assert "qwen-floating" in warnings[0]
    assert "vllm/vllm-openai:latest" in warnings[0]
