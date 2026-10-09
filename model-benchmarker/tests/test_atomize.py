"""Atomizer tests: past runs decomposed into "this model ran this test"
records (atomize.py), the shared parse path behind the results page's
Test-atoms table, /api/results/atoms, and compare.py's chat rows."""

from __future__ import annotations

import json
from pathlib import Path

from model_benchmarker import atomize as A

# compact format (the current writer's layout)
COMPACT_MD = """# demo

- Model: PcaitChat name='deepseek-v41-flash'
- MODE: arrival=closed
Status: complete (last updated 2026-10-02 10:00:00)

| ctx | users | task | failed | TTFT P50/P95/P99/P100 (ms) | tokens/s P50/P95/P99/P100 |
|:---|:---|:---|:---|---:|---:|
| 0 | 1 | coding | 0 | 930.6 / 930.6 / 930.6 / 930.6 | 331.1 / 318.0 / 317.6 / 317.5 |
| 0 | 32 | coding | 0 | 3090.0 / 3092.0 / 3093.0 / 3094.0 | 39.3 / 34.1 / 33.0 / 32.0 |
"""

# wide legacy layout (committed results tree), multiturn, one repeated test
WIDE_MD = """# demo wide

MODE: multiturn — each user runs 5 turns of one conversation
Status: in progress — 1/3 levels completed

| ctx | users | task | failed | TTFT turn1 P50 (ms) | TTFT turn1 P95 (ms) | TTFT turn1 P99 (ms) | TTFT turn1 P100 (ms) | TTFT-post P50 (ms) | TTFT-post P95 (ms) | TTFT-post P99 (ms) | TTFT-post P100 (ms) | tokens/s P50 | tokens/s P95 | tokens/s P99 | tokens/s P100 |
|:---|:---|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1 | coding | 0 | 900.0 | 900.0 | 900.0 | 900.0 | 510.4 | 847.2 | 882.1 | 890.8 | 300.0 | 298.0 | 297.0 | 296.0 |
| 0 | 1 | coding | 0 | 870.0 | 870.0 | 870.0 | 870.0 | 500.0 | 700.0 | 800.0 | 900.0 | 310.0 | 305.0 | 304.0 | 303.0 |
| 0 | 4 | mixed | 2 | 382.8 | 473.3 | 473.4 | 473.5 | 511.3 | 665.8 | 685.0 | 689.8 | 220.4 | 205.6 | 205.4 | 205.3 |
"""


def test_atomize_compact_shape_and_provenance():
    meta = {
        "run_id": "r1",
        "status": "success",
        "started_utc": "2026-10-02T10:00:00+00:00",
        "target_url": "http://m:8000",
        "params": {"requests_per_user": 5},
    }
    atoms = A.atomize_chat_report(COMPACT_MD, "runs/r1/report.md", meta)
    assert len(atoms) == 2
    a = atoms[0]
    assert a["kind"] == "chat"
    assert a["test_id"] == "ctx=0|users=1|coding|closed"
    assert a["label"] == "ctx=0 users=1 coding"
    assert a["model"] == "deepseek-v41-flash"  # from the - Model: header bullet
    assert a["run_id"] == "r1" and a["status"] == "success"
    assert a["arrival"] == "closed" and a["mode"] == "single"
    assert a["load"] == {"requests_per_user": 5}
    assert a["metrics"]["ttft_ms"] == {"p50": 930.6, "p95": 930.6, "p99": 930.6, "p100": 930.6}
    assert a["metrics"]["tokens_per_s"]["p100"] == 317.5
    assert a["metrics"]["ttft_post_ms"] is None  # group absent in compact single-turn


def test_atomize_wide_multiturn_and_dedupe():
    atoms = A.atomize_chat_report(WIDE_MD, "report.md", {})
    assert len(atoms) == 3
    assert all(a["mode"] == "multiturn" for a in atoms)
    first = next(a for a in atoms if a["occurrence"] == 0)
    assert first["metrics"]["ttft_post_ms"]["p50"] == 510.4
    # latest_occurrences collapses the repeated (ctx, users, task): last wins
    view = A.latest_occurrences(atoms)
    assert len(view) == 2
    kept = next(a for a in view if a["test_id"] == "ctx=0|users=1|coding|closed")
    assert kept["metrics"]["ttft_ms"]["p50"] == 870.0


def test_atom_model_priority_meta_beats_bullet_beats_slug(tmp_path: Path):
    slug = tmp_path / "my-model"
    slug.mkdir()
    f = slug / "setup.md"
    f.write_text(COMPACT_MD, encoding="utf-8")
    # 1) directory slug (no meta, no bullet — strip the bullet first)
    no_bullet = COMPACT_MD.replace("- Model: PcaitChat name='deepseek-v41-flash'\n", "")
    assert A.atomize_chat_report(no_bullet, str(f), {})[0]["model"] == "my-model"
    # 2) header bullet beats the slug
    assert A.atomize_chat_report(COMPACT_MD, str(f), {})[0]["model"] == "deepseek-v41-flash"
    # 3) run_meta params.model beats everything
    meta = {"params": {"model": "from-meta"}}
    assert A.atomize_chat_report(COMPACT_MD, str(f), meta)[0]["model"] == "from-meta"


def test_atom_model_never_from_runs_dir(tmp_path: Path):
    """runs/<id>/report.md without a model bullet/model param stays unknown:
    the run id is not a model name."""
    runs = tmp_path / "runs" / "20261002T160051Z-ab"
    runs.mkdir(parents=True)
    (runs / "report.md").write_text(
        COMPACT_MD.replace("- Model: PcaitChat name='deepseek-v41-flash'\n", ""), encoding="utf-8"
    )
    (runs / "run_meta.json").write_text(json.dumps({"run_id": "20261002T160051Z-ab"}), encoding="utf-8")
    atoms, _notes = A.atomize_paths([str(tmp_path / "runs")])
    assert atoms
    assert all(a["model"] is None for a in atoms)


def test_atom_to_compare_metrics_matches_legacy_shape():
    atoms = A.latest_occurrences(A.atomize_chat_report(COMPACT_MD, "report.md", {}))
    m = A.atom_to_compare_metrics(atoms[0])
    assert m == {
        "ttft_turn1_p50_ms": 930.6,
        "ttft_turn1_p95_ms": 930.6,
        "ttft_post_p50_ms": None,
        "ttft_post_p95_ms": None,
        "tokens_p50": 331.1,
        "tokens_p95": 318.0,
        "failed": 0.0,
    }


def test_compare_normalize_chat_rows_via_atom_layer():
    """The compare flow's chat rows come from the SAME decomposition (one
    parse path, three consumers) — identical output to the old dedicated
    parser, including last-occurrence-wins."""
    from model_benchmarker.webapp.compare import normalize_chat_rows

    rows = normalize_chat_rows(WIDE_MD)
    assert [r["label"] for r in rows] == ["ctx=0 users=1 coding", "ctx=0 users=4 mixed"]
    assert rows[0]["metrics"]["ttft_turn1_p50_ms"] == 870.0  # LAST of the pair


def test_endpoint_atoms_from_run_json():
    from model_benchmarker.webapp.compare import normalize_endpoint_rows

    run_json = {
        "summary": {
            "rows": [
                {
                    "concurrency": 4,
                    "rps": 42.0,
                    "lat_p50": 8.0,
                    "lat_p99": 12.0,
                    "lat_p95": 11.0,
                    "lat_max": 20.0,
                    "failed": 0,
                    "total": 100,
                    "lat_min": 1.0,
                    "lat_mean": 8.0,
                    "avg_results": 1.0,
                    "avg_bytes": 128.0,
                    "window_s": 60.0,
                    "success_rate_pct": 99.0,
                }
            ],
            "knee": None,
            "notes": [],
        }
    }
    atoms = A.atomize_endpoint_run(run_json, "runs/ep/run.json", {"run_id": "ep", "params": {"mode": "mcp"}})
    assert len(atoms) == 1
    a = atoms[0]
    assert a["kind"] == "endpoint" and a["mode"] == "mcp"
    assert a["test_id"] == "mcp|N=4"
    assert a["metrics"]["rps"] == 42.0
    assert a["metrics"]["lat_p50_ms"] == 8.0  # compare.py's key renaming
    assert normalize_endpoint_rows(run_json)[0]["metrics"] == a["metrics"]


def test_atoms_markdown_renders_and_escapes():
    atoms = A.latest_occurrences(A.atomize_chat_report(COMPACT_MD, "report.md", {}))
    md = A.atoms_markdown(atoms, "t")
    assert "| deepseek-v41-flash |" in md
    assert "930.6" in md
    # a model name containing | cannot break the table
    atoms[0]["model"] = "weird|name"
    safe = next(ln for ln in A.atoms_markdown(atoms, "t").splitlines() if ln.startswith("| weird"))
    assert "weird\\|name" in safe


def test_atomize_paths_reports_skips(tmp_path: Path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "prose.md").write_text("no tables here\n", encoding="utf-8")
    atoms, notes = A.atomize_paths([str(tmp_path / "runs")])
    assert atoms == []
    assert any("no chat table rows" in n for n in notes)


def test_atomic_writes_leave_no_partial(tmp_path: Path, monkeypatch):
    out = tmp_path / "sub" / "atoms.jsonl"
    A._atomic_write(out, "{}\n")
    assert out.read_text() == "{}\n"
    assert not out.with_name("atoms.jsonl.partial").exists()
    assert not list(out.parent.glob("*.partial"))


def test_extract_model_synth_path_yields_nothing():
    """Adversarial-review BUG 6 regression: a bare relative path that does
    NOT exist on disk must yield None (the CWD's parent is not a model)."""
    from pathlib import Path

    from model_benchmarker import atomize as A

    assert A._extract_model("no bullets", {}, Path("x.md")) is None
    # and a real file in a slug dir still resolves (the guard must not
    # over-block): build one
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path as P

        slug = P(td) / "mymodel"
        slug.mkdir()
        f = slug / "setup.md"
        f.write_text("# t\n", encoding="utf-8")
        assert A._extract_model("no bullets", {}, f) == "mymodel"
