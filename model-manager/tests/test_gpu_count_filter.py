"""The catalog's GPU-count window: derivation, defaults, and the UI contract.

The catalog filters configurations by the number of GPUs they reserve.  The
count is DERIVED from resource_limit_gpu / resource_request_gpu (never stored),
the default window is per GPU class, and the page ships both the inputs and the
per-class defaults the browser re-seeds them from.  These tests pin the parts
that break silently: a count that fails to parse (the entry would read as 0 and
drop out of the default window), a class with no default (the inputs would go
blank on a radio click), and the wiring that carries the constants to the
browser.

The default window is a FILTER, not a display preference: H200 and All open on
1-4 GPUs, RTX Pro 6000 and L40S on 1-2, so an 8-GPU H200 configuration and a
0-GPU shared configuration are both outside the initial view by design.  The
tests below assert that this hides something (a default that hides nothing is a
dead control) but never the whole seed (that would be a regression).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from model_manager.app.catalog import (  # noqa: E402
    GPU_COUNT_DEFAULTS,
    TIERS,
    Catalog,
    entry_gpu_count,
    entry_gpus,
)

SEED = json.loads((REPO / "src" / "model_manager" / "app" / "seed_catalog.json").read_text())
TEMPLATES = REPO / "src" / "model_manager" / "app" / "templates"
STATIC = REPO / "src" / "model_manager" / "app" / "static"


def _catalog(entries):
    """Build a Catalog over an explicit entry list (no disk, no seed merge)."""
    cat = Catalog.__new__(Catalog)
    cat.entries = entries
    return cat


def _entry(**overrides):
    base = {
        "catalog_id": "g-1",
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _in_window(entry: dict, window: dict) -> bool:
    """The predicate the page applies: inclusive on both ends, 0 is a count."""
    return window["min"] <= entry_gpu_count(entry) <= window["max"]


# ---- count derivation ----


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"resource_limit_gpu": "4", "resource_request_gpu": "4"}, 4),
        ({"resource_request_gpu": "2"}, 2),
        # limit wins, exactly as entry_gpus() reads it for the row label
        ({"resource_limit_gpu": "4", "resource_request_gpu": "1"}, 4),
        # a JSON number, not a string
        ({"resource_limit_gpu": 8}, 8),
        # 0 is a real count: a shared config that reserves no whole GPU
        ({"resource_request_gpu": "0"}, 0),
        ({}, 0),
        # unparseable / negative degrade to 0 instead of crashing the render
        ({"resource_limit_gpu": "four"}, 0),
        ({"resource_limit_gpu": "-2"}, 0),
        ({"resource_limit_gpu": ""}, 0),
    ],
)
def test_gpu_count_derivation(fields, expected):
    assert entry_gpu_count(_entry(**fields)) == expected


def test_gpu_count_agrees_with_the_display_string():
    """The filter and the "N GPU" label must never disagree on one row."""
    for entry in SEED:
        assert entry_gpu_count(entry) == int(entry_gpus(entry))


def test_every_config_node_carries_a_numeric_count():
    tree = _catalog(SEED).tree()
    nodes = [cfg for lab in tree for model in lab["models"] for cfg in model["configs"]]
    assert len(nodes) == len(SEED)
    for node in nodes:
        assert isinstance(node["gpu_count"], int)
        assert node["gpu_count"] == int(node["gpus"])


# ---- default windows ----


def test_default_windows_match_the_provisioned_nodes():
    """The requested defaults: 1-4 on H200 and All, 1-2 on RTX / L40S."""
    assert GPU_COUNT_DEFAULTS[""] == {"min": 1, "max": 4}
    assert GPU_COUNT_DEFAULTS["h200"] == {"min": 1, "max": 4}
    assert GPU_COUNT_DEFAULTS["rtx-pro-6000"] == {"min": 1, "max": 2}
    assert GPU_COUNT_DEFAULTS["l40s"] == {"min": 1, "max": 2}


def test_every_gpu_class_has_a_default_window():
    """A class with no default leaves the inputs blank on a radio click."""
    assert set(GPU_COUNT_DEFAULTS) == {""} | set(TIERS)


def test_default_windows_are_well_formed():
    for tier, window in GPU_COUNT_DEFAULTS.items():
        assert set(window) == {"min", "max"}, tier
        assert window["min"] >= 0, tier
        assert window["min"] <= window["max"], tier


def test_the_default_window_hides_and_keeps_seed_entries():
    for tier in [""] + list(TIERS):
        window = GPU_COUNT_DEFAULTS[tier]
        entries = [e for e in SEED if not tier or e.get("tier") == tier]
        kept = [e for e in entries if _in_window(e, window)]
        assert kept, f"the default window for {tier!r} hides the whole seed"
        # The 8-GPU H200 config and the 0-GPU shared configs are outside the
        # default view on purpose -- the window has to actually narrow.
        if tier in ("", "h200"):
            assert len(kept) < len(entries), f"the default window for {tier!r} hides nothing"
            assert any(entry_gpu_count(e) == 0 for e in entries)


def test_lowering_the_minimum_reveals_shared_configurations():
    shared = [e for e in SEED if entry_gpu_count(e) == 0]
    assert shared, "seed no longer carries a 0-GPU shared configuration"
    assert not any(_in_window(e, GPU_COUNT_DEFAULTS["h200"]) for e in shared)
    wide = {"min": 0, "max": GPU_COUNT_DEFAULTS["h200"]["max"]}
    assert all(_in_window(e, wide) for e in shared)


def test_raising_the_maximum_reveals_the_eight_gpu_configuration():
    big = [e for e in SEED if entry_gpu_count(e) > GPU_COUNT_DEFAULTS["h200"]["max"]]
    assert big, "seed no longer carries a configuration above the default maximum"
    assert not any(_in_window(e, GPU_COUNT_DEFAULTS["h200"]) for e in big)
    wide = {"min": GPU_COUNT_DEFAULTS["h200"]["min"], "max": 8}
    assert all(_in_window(e, wide) for e in big)


# ---- template / script wiring ----


def test_template_ships_the_min_max_controls():
    html = _read(TEMPLATES / "catalog.html")
    assert 'id="gpu-count-min"' in html
    assert 'id="gpu-count-max"' in html
    assert 'id="gpu-count-reset"' in html
    # Both inputs are seeded from the server constant, never hardcoded here.
    assert "gpu_count_defaults[''].min" in html
    assert "gpu_count_defaults[''].max" in html


def test_every_class_radio_carries_its_default_window():
    """The browser re-seeds the inputs from the radios, so each needs its own.

    The template loops the tier radios, so the SOURCE carries one annotated
    radio for All plus one annotated radio inside the loop; the rendered count
    (one per class) is asserted by the live page test.
    """
    html = _read(TEMPLATES / "catalog.html")
    assert "data-gpu-min=\"{{ gpu_count_defaults[''].min }}\"" in html
    assert "data-gpu-max=\"{{ gpu_count_defaults[''].max }}\"" in html
    assert 'data-gpu-min="{{ gpu_count_defaults[t].min }}"' in html
    assert 'data-gpu-max="{{ gpu_count_defaults[t].max }}"' in html


def test_script_applies_both_bounds_and_shares_them_in_the_url():
    js = _read(STATIC / "catalog.js")
    # Both ends are enforced, inclusively...
    assert "searchState.minGpu !== null && gpus < searchState.minGpu" in js
    assert "searchState.maxGpu !== null && gpus > searchState.maxGpu" in js
    # ...against the server-derived count, with the display string as fallback
    assert "cfg.gpu_count" in js
    # ...re-seeded from the class radio the template annotated
    assert "dataset.gpuMin" in js
    assert "dataset.gpuMax" in js
    # ...and linkable, so a narrowed view is shareable
    assert "'gmin'" in js
    assert "'gmax'" in js


def test_summary_names_the_active_window():
    """The page opens with a filter on, so the count must be reported."""
    js = _read(STATIC / "catalog.js")
    assert "function gpuRangeLabel()" in js
    assert "searchState.minGpu" in js and "searchState.maxGpu" in js


# ---- live app wiring ----


_TMP = tempfile.mkdtemp(prefix="md-gpu-count-")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP", "test-template")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP_NAMESPACE", "test-ns")
# The app seeds its catalog on import; point it at a throwaway path so an
# off-cluster import never touches /mnt.
os.environ.setdefault("CATALOG_PATH", os.path.join(_TMP, "catalog.json"))


def _install_psycopg2_shim() -> None:
    if "psycopg2" in sys.modules:  # pragma: no cover - already provided
        return

    def _no_db(*_args, **_kwargs):
        raise RuntimeError("no db in tests")

    _pg = types.ModuleType("psycopg2")
    _pg.__path__ = []  # mark as a package so psycopg2.extras resolves
    # The stubs are plain ModuleTypes, so mypy has no attribute to check these
    # against; the ignores are the suppression idiom and keep this file clean
    # (the older shims in this suite carry the same three errors unfixed).
    _pg.connect = _no_db  # type: ignore[attr-defined]
    _ext = types.ModuleType("psycopg2.extensions")
    _ext.ISOLATION_LEVEL_AUTOCOMMIT = 0  # type: ignore[attr-defined]
    _extras = types.ModuleType("psycopg2.extras")
    _extras.Json = lambda value: value  # type: ignore[attr-defined]
    sys.modules["psycopg2"] = _pg
    sys.modules["psycopg2.extensions"] = _ext
    sys.modules["psycopg2.extras"] = _extras


@pytest.fixture(scope="module")
def client():
    _install_psycopg2_shim()
    try:
        from fastapi.testclient import TestClient

        from model_manager.app import main as md
    except Exception as exc:  # pragma: no cover - dev box without app deps
        pytest.skip(f"app import unavailable: {exc}")
    return TestClient(md.app)


def test_api_carries_the_defaults_and_a_numeric_count(client):
    data = client.get("/api/catalog").json()
    assert data["gpu_count_defaults"] == GPU_COUNT_DEFAULTS
    nodes = [cfg for lab in data["tree"] for model in lab["models"] for cfg in model["configs"]]
    assert nodes
    for node in nodes:
        assert isinstance(node["gpu_count"], int)
        assert node["gpu_count"] == int(node["gpus"])


def test_catalog_page_renders_the_window_controls(client):
    html = client.get("/catalog").text
    assert 'id="gpu-count-min"' in html
    assert 'id="gpu-count-max"' in html
    # The All view is the initial state, so the inputs open on 1-4.
    assert 'value="1"' in html
    assert 'value="4"' in html
    # The per-class windows ride on the radios for the browser to re-seed from.
    assert 'data-gpu-min="1" data-gpu-max="4"' in html  # All + H200
    assert 'data-gpu-min="1" data-gpu-max="2"' in html  # RTX Pro 6000 + L40S
