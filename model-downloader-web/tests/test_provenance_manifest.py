"""Unit tests for the MD-A1 provenance manifest logic.

The manifest builder lives verbatim inside the helm-rendered job script
(helm/templates/configmap-job-template.yaml). These tests extract that exact
block from a live `helm template` render, so the code under test is the code
that actually ships — then executes it against a synthetic HF cache layout
(blobs/ + snapshots/<sha>/ symlinked, refs/, model card, dangling link) and
asserts on the manifest.json it produces:

  * revision pinning: refs/<requested> -> commit; commit-sha pins resolve via
    the snapshot dir only when the requested ref is commit-like and matches;
    unknown refs are recorded, never silently adopted; "" defaults to main;
  * per-file sha256 + size with streamed reads (symlinks followed, dangling
    links skipped);
  * lfs_sha256 / lfs_sha256_verified only when Hub metadata is available,
    matching and mismatching;
  * license from config.json with model-card front-matter fallback;
  * idempotency: a second run (backoffLimit retry) overwrites cleanly, leaves
    no .partial behind, and never fails.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
HELM_DIR = REPO / "helm"
WORK = Path(__file__).resolve().parent / "work"

CID = "models--acme--Test-Model"
REPO_ID = "acme/Test-Model"


def _prov_block() -> str:
    """Extract the provenance python block from a live helm render."""
    proc = subprocess.run(
        ["helm", "template", "mdtest", ".", "-n", "project-user-x"],
        cwd=HELM_DIR,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    script = cm["data"]["job.yaml"].splitlines()
    start = next(i for i, ln in enumerate(script) if ln.strip().startswith("python3 - <<'PROV_EOF'"))
    end = next(i for i in range(start + 1, len(script)) if script[i].strip() == "PROV_EOF")
    body = "\n".join(ln[10:] if ln.startswith(" " * 10) else ln for ln in script[start + 1 : end])
    compile(body, "prov_block", "exec")
    return body


PROV = _prov_block()


def _build_cache(root: Path, commit: str) -> Path:
    """A realistic HF cache: blobs/ + snapshots/<sha>/ of symlinks + refs/."""
    shutil.rmtree(root, ignore_errors=True)
    snap = root / CID / "snapshots" / commit
    blobs = root / CID / "blobs"
    (root / CID / "refs").mkdir(parents=True)
    blobs.mkdir(parents=True)
    snap.mkdir(parents=True)

    weights = blobs / "lfsblob"
    weights.write_bytes(b"W" * (2 * 1024 * 1024 + 7))
    (snap / "model.safetensors").symlink_to(weights)

    (snap / "config.json").write_text(json.dumps({"license": "llama3.2"}))
    (snap / "tokenizer.json").write_text("{}")
    (snap / "README.md").write_text("---\nlicense: other\n---\n# Model\n")
    (snap / "dangling.bin").symlink_to(blobs / "missing-blob")

    (root / CID / "refs" / "main").write_text(commit + "\n")
    return snap


def _run(env: dict, script: str = PROV) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent),
    )


def _manifest(root: Path) -> dict:
    return json.loads((root / CID / "manifest.json").read_text())


def _env(root: Path, snap: Path, **over) -> dict:
    base = {
        "CACHE_ROOT": str(root),
        "CACHE_ID": CID,
        "SNAP_DIR": str(snap),
        "MODEL_NAME": REPO_ID,
        "PROVENANCE_REVISION": "main",
        "JOB_ID": "testjob123",
        "JOB_NAMESPACE": "project-user-test",
        "SUBMITTED_BY": "",
    }
    base.update(over)
    return base


def test_manifest_fields_and_files():
    commit = hashlib.sha256(b"fixture").hexdigest()
    root = WORK / "t_fields"
    snap = _build_cache(root, commit)
    proc = _run(_env(root, snap))
    assert proc.returncode == 0, proc.stderr
    m = _manifest(root)

    assert m["schema"] == "model-downloader.provenance/1.0"
    assert m["repo"] == REPO_ID
    assert m["revision"] == commit and m["revision_requested"] == "main" and m["revision_resolved"] is True
    assert m["license"] == "llama3.2"
    assert m["job_id"] == "testjob123"
    assert m["namespace"] == "project-user-test"
    assert m["submitted_by"] == ""  # no identity yet — field present, empty
    assert m["hub_metadata_fetched"] is False

    files = {f["path"]: f for f in m["files"]}
    assert set(files) == {"model.safetensors", "config.json", "tokenizer.json", "README.md"}
    weights_sha = hashlib.sha256((root / CID / "blobs" / "lfsblob").read_bytes()).hexdigest()
    assert files["model.safetensors"]["sha256"] == weights_sha  # symlink followed
    assert files["model.safetensors"]["size"] == 2 * 1024 * 1024 + 7
    assert files["config.json"]["sha256"] == hashlib.sha256((snap / "config.json").read_bytes()).hexdigest()
    assert "dangling.bin" not in files  # dangling symlink skipped
    assert all("lfs_sha256" not in f for f in m["files"])  # no Hub metadata -> no claim


def test_idempotent_rerun():
    commit = hashlib.sha256(b"rerun").hexdigest()
    root = WORK / "t_rerun"
    snap = _build_cache(root, commit)
    env = _env(root, snap)
    assert _run(env).returncode == 0
    m1 = _manifest(root)
    assert _run(env).returncode == 0  # backoffLimit retry: blobs already on disk
    m2 = _manifest(root)
    assert m1["files"] == m2["files"]
    assert m1["revision"] == m2["revision"]
    assert not (root / CID / "manifest.json.partial").exists()


def test_lfs_cross_check_match_and_mismatch():
    commit = hashlib.sha256(b"lfs").hexdigest()
    root = WORK / "t_lfs"
    snap = _build_cache(root, commit)
    weights_sha = hashlib.sha256((root / CID / "blobs" / "lfsblob").read_bytes()).hexdigest()
    script_with_hub = PROV.replace(
        "lfs_shas = {}",
        f"lfs_shas = {{'model.safetensors': {weights_sha!r}}}",
    ).replace("hub_metadata_fetched = False", "hub_metadata_fetched = True")
    assert _run(_env(root, snap), script_with_hub).returncode == 0
    entry = {f["path"]: f for f in _manifest(root)["files"]}["model.safetensors"]
    assert entry["lfs_sha256"] == weights_sha
    assert entry["lfs_sha256_verified"] is True
    assert "lfs_sha256" not in {f["path"]: f for f in _manifest(root)["files"]}["config.json"]

    bad = PROV.replace(
        "lfs_shas = {}",
        "lfs_shas = {'model.safetensors': " + repr("0" * 64) + "}",
    ).replace("hub_metadata_fetched = False", "hub_metadata_fetched = True")
    assert _run(_env(root, snap), bad).returncode == 0
    entry = {f["path"]: f for f in _manifest(root)["files"]}["model.safetensors"]
    assert entry["lfs_sha256_verified"] is False


def test_license_card_fallback():
    commit = hashlib.sha256(b"card").hexdigest()
    root = WORK / "t_card"
    snap = _build_cache(root, commit)
    (snap / "config.json").write_text(json.dumps({"architectures": ["LlamaForCausalLM"]}))
    assert _run(_env(root, snap)).returncode == 0
    assert _manifest(root)["license"] == "other"


def test_revision_pinning_variants():
    commit = hashlib.sha256(b"pin").hexdigest()
    root = WORK / "t_pin"
    snap = _build_cache(root, commit)
    (root / CID / "refs" / "v2.1").write_text("tagged789\n")
    snap40 = root / CID / "snapshots" / ("b" * 40)
    snap40.mkdir(parents=True)
    (snap40 / "config.json").write_text("{}")

    m = _manifest(root) if _run(_env(root, snap)).returncode == 0 else None
    assert m["revision"] == commit and m["revision_resolved"] is True  # main -> refs

    m = _manifest(root) if _run(_env(root, snap, PROVENANCE_REVISION="v2.1")).returncode == 0 else None
    assert m["revision"] == "tagged789" and m["revision_resolved"] is True  # tag -> refs

    m = _manifest(root) if _run(_env(root, snap, PROVENANCE_REVISION=commit)).returncode == 0 else None
    assert m["revision"] == commit and m["revision_resolved"] is True  # sha pin, no refs entry

    m = _manifest(root) if _run(_env(root, snap40, PROVENANCE_REVISION="b" * 40)).returncode == 0 else None
    assert m["revision"] == "b" * 40 and m["revision_resolved"] is True  # 40-hex pin

    m = _manifest(root) if _run(_env(root, snap, PROVENANCE_REVISION="")).returncode == 0 else None
    assert m["revision_requested"] == "main" and m["revision_resolved"] is True  # "" -> main

    m = _manifest(root) if _run(_env(root, snap, PROVENANCE_REVISION="does-not-exist")).returncode == 0 else None
    assert m["revision"] == "does-not-exist" and m["revision_resolved"] is False  # never adopted
