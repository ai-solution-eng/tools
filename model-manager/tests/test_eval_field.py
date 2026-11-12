"""Tests for the optional ``eval`` block on catalog entries (EVAL).

Catalog entries carry deployment facts only — the eval block is the one
record that a quantised variant (FP8/NVFP4/...) was actually measured
against its bf16 original.  These tests pin the four things that matter:

* backward compatibility — entries without an eval block load and behave
  exactly as before (the seed catalog ships none);
* a valid block parses and is stored verbatim, and clears the badge;
* malformed blocks are REJECTED with a clear error at the entry boundary
  (Catalog.add / merge_entries), never silently ignored;
* the unvalidated-quant badge logic — quantised without eval is badged,
  unvalidated BF16/FP16 (the reference itself) is NOT.

Plus the MLIS push warning: pushing an unvalidated quantised variant logs a
warning naming the model (it never blocks the push).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "model_manager"))

from app.catalog import (  # noqa: E402
    Catalog,
    entry_eval,
    entry_quant,
    entry_unvalidated,
    validate_eval,
)

SEED = json.loads((REPO / "src" / "model_manager" / "app" / "seed_catalog.json").read_text())

VALID_EVAL = {
    "suite": "mmlu",
    "score": 84.2,
    "baseline_bf16": 86.0,
    "run_id": "20261028T120000Z",
    "date": "2026-10-28",
}


def _entry(**overrides):
    base = {
        "catalog_id": "t-eval-1",
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


def _catalog(entries):
    """Build a Catalog over an explicit entry list (no disk, no seed merge)."""
    cat = Catalog.__new__(Catalog)
    cat.entries = entries
    cat._removed_seed_ids = set()
    return cat


def _config_node(entry):
    return Catalog._config_nodes([entry])[0]


# ---------------------------------------------------------------------------
# Backward compatibility: no eval block == exactly today's behaviour
# ---------------------------------------------------------------------------


def test_seed_entries_have_no_eval_and_load_unchanged():
    assert all(e.get("eval") is None for e in SEED)
    for entry in SEED:
        assert entry_eval(entry) is None


def test_fresh_catalog_still_seeds_every_entry(tmp_path):
    cat = Catalog(str(tmp_path / "catalog.json"))
    assert len(cat.entries) == len(SEED)
    assert all(entry_eval(e) is None for e in cat.entries)


def test_add_without_eval_invents_nothing(tmp_path):
    entry = _entry()
    del entry["catalog_id"]
    cat = Catalog(str(tmp_path / "catalog.json"))
    added = cat.add(entry)
    assert "eval" not in added  # not invented, not defaulted
    assert entry_eval(added) is None


# ---------------------------------------------------------------------------
# A valid block parses and is stored verbatim
# ---------------------------------------------------------------------------


def test_valid_eval_full_block(tmp_path):
    entry = _entry(eval=dict(VALID_EVAL))
    cat = Catalog(str(tmp_path / "catalog.json"))
    added = cat.add(entry)
    assert added["eval"] == VALID_EVAL  # verbatim
    assert entry_eval(added) == VALID_EVAL
    assert entry_unvalidated(added) is False  # quantised, now validated
    node = _config_node(added)
    assert node["eval"] == VALID_EVAL
    assert node["unvalidated"] is False


def test_valid_eval_minimal_fraction_score(tmp_path):
    # The 0-1 fraction convention is a legal score; unknown extra keys are
    # forward-compatible.
    minimal = {"suite": "mmlu", "score": 0.842, "extra_future_key": 1}
    entry = _entry(eval=dict(minimal))
    cat = Catalog(str(tmp_path / "catalog.json"))
    added = cat.add(entry)
    assert added["eval"] == minimal
    assert entry_unvalidated(added) is False


def test_eval_on_non_quantised_entry_never_badged():
    # An entry whose quant cannot be determined is not claimed quantised, so
    # the eval block (or its absence) never earns the badge.
    entry = _entry(
        name="Mystery-Model",
        uri="pvc://models-pvc/large-models/Org/Mystery-Model?containerPath=/mnt/models",
    )
    assert entry_quant(entry) == ""
    assert entry_unvalidated(entry) is False
    entry["eval"] = dict(VALID_EVAL)
    assert entry_unvalidated(entry) is False


# ---------------------------------------------------------------------------
# Malformed blocks are rejected with a clear error, never silently ignored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "mmlu=84",  # not an object
        {},  # no suite
        {"suite": ""},  # empty suite
        {"suite": "mmlu"},  # no score
        {"suite": "mmlu", "score": "84.2"},  # string score
        {"suite": "mmlu", "score": -1},  # negative
        {"suite": "mmlu", "score": 1e9},  # out of range
        {"suite": "mmlu", "score": float("inf")},  # not finite
        {"suite": "mmlu", "score": True},  # bool is not a score
        {"suite": "mmlu", "score": 1.0, "baseline_bf16": -5},  # bad baseline
        {"suite": "mmlu", "score": 1.0, "run_id": 42},  # run_id must be a string
        {"suite": "mmlu", "score": 1.0, "date": "October 2026"},  # unparseable date
        {"suite": "mmlu", "score": 1.0, "date": 20261028},  # date must be a string
    ],
)
@pytest.mark.parametrize("name", [""], ids=["named"])
def test_validate_eval_rejects_malformed(bad, name):
    with pytest.raises(ValueError) as excinfo:
        validate_eval(bad, name)
    msg = str(excinfo.value)
    assert "eval" in msg  # every error names the eval block
    if isinstance(bad, dict):
        # A FIELD-level error must name the offending field. A non-dict block
        # has no field to name yet, so "eval must be an object" is the whole
        # correct message there — requiring a field name for it was wrong.
        assert "suite" in msg or "score" in msg or "baseline" in msg or "run_id" in msg or "date" in msg


def test_validate_eval_accepts_none_and_valid():
    validate_eval(None, "x")  # absent is always valid
    validate_eval(dict(VALID_EVAL), "x")


def test_add_rejects_malformed_eval(tmp_path):
    cat = Catalog(str(tmp_path / "catalog.json"))
    with pytest.raises(ValueError, match="eval.date"):
        cat.add(_entry(eval={"suite": "mmlu", "score": 1.0, "date": "soon"}))


def test_merge_entries_rejects_malformed_eval_and_adds_nothing():
    cat = _catalog([_entry(name="existing")])
    with pytest.raises(ValueError, match="eval.score"):
        cat.merge_entries(
            [_entry(catalog_id="new-bad", name="bad", eval={"suite": "mmlu"})],
            source="github",
        )
    assert len(cat.entries) == 1  # nothing added before the rejection


# ---------------------------------------------------------------------------
# Unvalidated-quant badge logic
# ---------------------------------------------------------------------------


def test_quantised_without_eval_is_unvalidated():
    assert entry_quant(_entry()) == "FP8"  # the fixture is genuinely quantised
    assert entry_unvalidated(_entry()) is True
    nvfp4 = _entry(
        name="Gemma-4-31B-it-NVFP4",
        uri="pvc://models-pvc/large-models/Gemma/Gemma-4-31B-it-NVFP4?containerPath=/mnt/models",
    )
    assert entry_quant(nvfp4) == "NVFP4"
    assert entry_unvalidated(nvfp4) is True


def test_unvalidated_bf16_is_not_badged():
    # The reference itself must never carry the badge, with or without eval.
    bf16 = _entry(
        name="Qwen3.8-27B-BF16",
        uri="pvc://models-pvc/large-models/Qwen/Qwen3.8-27B?containerPath=/mnt/models",
    )
    assert entry_quant(bf16) == "BF16"
    assert entry_unvalidated(bf16) is False
    bf16["eval"] = dict(VALID_EVAL)
    assert entry_unvalidated(bf16) is False


def test_fp16_is_not_badged():
    fp16 = _entry(
        name="Qwen3.8-27B-FP16",
        uri="pvc://models-pvc/large-models/Qwen/Qwen3.8-27B?containerPath=/mnt/models",
    )
    assert entry_quant(fp16) == "FP16"
    assert entry_unvalidated(fp16) is False


def test_malformed_eval_is_treated_as_absent_by_the_badge():
    # The derivation never raises: a rogue block cannot crash the tree render
    # or the push path (rejection happened at the entry boundary).
    rogue = _entry(eval={"suite": "mmlu", "score": "no idea"})
    assert entry_eval(rogue) is None
    assert entry_unvalidated(rogue) is True


def test_config_node_carries_eval_and_badge_flag():
    node = _config_node(_entry())
    assert node["eval"] is None
    assert node["unvalidated"] is True
    node = _config_node(_entry(eval=dict(VALID_EVAL)))
    assert node["eval"] == VALID_EVAL
    assert node["unvalidated"] is False


# ---------------------------------------------------------------------------
# MLIS push warning
# ---------------------------------------------------------------------------


def _import_main(tmp_path, monkeypatch):
    """Import app.main the way the wiring tests do (psycopg2 shim + env)."""
    if "model_manager" not in sys.modules:
        try:
            import model_manager  # noqa: F401
        except ModuleNotFoundError:
            sys.path.insert(0, str(REPO / "src"))
    import types

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
    monkeypatch.setenv("CATALOG_PATH", str(tmp_path / "push-test-catalog.json"))
    monkeypatch.setenv("STORAGE_BACKEND", "pvc")
    from model_manager.app import main as md

    return md


def test_push_warns_only_on_unvalidated_quant(monkeypatch, tmp_path, caplog):
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

    fp8_bad = {
        "name": "qwen-fp8",
        "image": "vllm/vllm-openai:latest",
        "uri": "pvc://m/l/Qwen/Qwen3.8-27B-FP8?containerPath=/mnt/models",
    }
    fp8_good = dict(fp8_bad, name="qwen-fp8-eval", eval={"suite": "mmlu", "score": 84.0})
    bf16_plain = {
        "name": "qwen-bf16",
        "image": "vllm/vllm-openai:latest",
        "uri": "pvc://m/l/Qwen/Qwen3.8-27B?containerPath=/mnt/models",
    }

    with caplog.at_level(logging.WARNING, logger="model_manager.app.main"):
        out = asyncio.run(md.push_models(_Req([fp8_bad, fp8_good, bf16_plain])))

    # the warning never blocks: all three still push
    assert out["pushed"] == 3
    assert [c["name"] for c in pushed["configs"]] == ["qwen-fp8", "qwen-fp8-eval", "qwen-bf16"]
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING and "UNVALIDATED" in r.message]
    assert len(warnings) == 1  # only the quantised entry without eval
    assert "qwen-fp8" in warnings[0]  # names the model
    assert "FP8" in warnings[0]  # and its quant level
