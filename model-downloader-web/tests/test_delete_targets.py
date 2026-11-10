"""Tests for the in-pod delete module (app/delete_targets.py).

This file removes bytes from the PVC, so the tests are weighted toward REFUSAL:
every structural escape hatch must result in no rmtree call at all.

The module makes no cluster or database calls by design (that is what lets the
Job run in the PVC's namespace with the default ServiceAccount), so there is
nothing here to fake but the filesystem and the remover.
"""

import base64
import json
import os
import shutil

import pytest

from model_downloader.app.delete_targets import (
    build_entries,
    decode_targets,
    run,
    tombstone,
    validate_target,
)


def encode(items):
    return base64.b64encode(json.dumps(items).encode()).decode()


def make_model(root, name="models--acme--Alpha", manifest=True):
    path = os.path.join(str(root), name)
    os.makedirs(path, exist_ok=True)
    if manifest:
        with open(os.path.join(path, "manifest.json"), "w") as fh:
            json.dump({"schema": "model-downloader.provenance/1.0", "scan": {"verdict": "clean"}}, fh)
    return path


# ---------------------------------------------------------------------------
# payload decoding
# ---------------------------------------------------------------------------


def test_decode_accepts_objects_and_strings():
    raw = encode([{"cachepath": "/a/models--x--Y", "override": True, "verdict": "soft_block"}, "/a/models--x--Z"])
    assert decode_targets(raw) == [
        {"cachepath": "/a/models--x--Y", "override": True, "verdict": "soft_block"},
        {"cachepath": "/a/models--x--Z", "override": False, "verdict": "deletable"},
    ]


def test_decode_dedupes_repeated_paths():
    raw = encode(["/a/models--x--Y", "/a/models--x--Y"])
    assert decode_targets(raw) == [{"cachepath": "/a/models--x--Y", "override": False, "verdict": "deletable"}]


@pytest.mark.parametrize(
    "raw",
    ["", encode([]), encode({"not": "a list"}), encode([{"nope": 1}]), encode([""])],
)
def test_decode_rejects_bad_payloads(raw):
    with pytest.raises((ValueError, TypeError)):
        decode_targets(raw)


# ---------------------------------------------------------------------------
# structural validation — the isolation rule
# ---------------------------------------------------------------------------


def test_valid_target(tmp_path):
    assert validate_target(make_model(tmp_path), str(tmp_path)) == ""


def test_rejects_relative_path(tmp_path):
    assert "absolute" in validate_target("models--a--B", str(tmp_path))


def test_rejects_symlink(tmp_path):
    real = make_model(tmp_path, "models--acme--Real")
    link = os.path.join(str(tmp_path), "models--acme--Link")
    os.symlink(real, link)
    assert "symlink" in validate_target(link, str(tmp_path))


def test_rejects_non_directory(tmp_path):
    path = os.path.join(str(tmp_path), "models--acme--File")
    open(path, "w").close()
    assert "not a directory" in validate_target(path, str(tmp_path))


def test_rejects_bad_basename(tmp_path):
    path = os.path.join(str(tmp_path), "just-a-dir")
    os.makedirs(path)
    assert "cache dir" in validate_target(path, str(tmp_path))


def test_rejects_the_scan_root_itself(tmp_path):
    root = os.path.join(str(tmp_path), "models--acme--Root")
    os.makedirs(root)
    assert "scan root" in validate_target(root, root)


def test_rejects_outside_the_scan_root(tmp_path):
    root = os.path.join(str(tmp_path), "root")
    os.makedirs(root)
    outside = make_model(tmp_path, "models--acme--Outside")
    assert "outside the scan root" in validate_target(outside, root)


def test_rejects_traversal(tmp_path):
    root = os.path.join(str(tmp_path), "root")
    os.makedirs(os.path.join(root, "models--a--B"))
    escape = os.path.join(root, "models--a--B", "..", "..", "models--a--B")
    assert validate_target(escape, root)


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)


def test_run_hides_the_directory_before_deleting_it(tmp_path):
    # The scanner matches models--*--* anywhere under the root, so a partially
    # deleted dir would still be listed as a model. The dir is renamed to a
    # .deleting- sibling first, which the glob no longer matches.
    path = make_model(tmp_path)
    rec = Recorder()
    code = run(targets=[{"cachepath": path, "override": False}], scan_root=str(tmp_path), actor="andrew", job="md-delete-1", remove=rec, write_tombstone=False)
    assert code == 0
    assert len(rec.calls) == 1
    pending = rec.calls[0]
    assert os.path.basename(pending) == ".deleting-models--acme--Alpha"
    assert os.path.dirname(pending) == os.path.dirname(path)
    assert not os.path.exists(path)


def test_run_aborts_on_structural_failure(tmp_path):
    path = make_model(tmp_path)
    link = os.path.join(str(tmp_path), "models--acme--Link")
    os.symlink(path, link)
    rec = Recorder()
    code = run(
        targets=[{"cachepath": path, "override": False}, {"cachepath": link, "override": False}],
        scan_root=str(tmp_path), actor="a", job="j", remove=rec, write_tombstone=False,
    )
    assert code == 1
    assert rec.calls == []  # the good target is NOT deleted either


def test_run_aborts_without_a_scan_root(tmp_path):
    rec = Recorder()
    assert run(targets=[{"cachepath": "/x/models--a--B"}], scan_root="", actor="a", job="j", remove=rec) == 1
    assert rec.calls == []


def test_run_records_a_vanished_target_as_a_skip(tmp_path):
    path = make_model(tmp_path)
    shutil.rmtree(path)
    rec = Recorder()
    code = run(targets=[{"cachepath": path}], scan_root=str(tmp_path), actor="a", job="j", remove=rec, write_tombstone=False)
    assert code == 1  # nothing deleted -> non-zero
    assert rec.calls == []


def test_run_really_removes_the_directory(tmp_path):
    path = make_model(tmp_path)
    assert os.path.isdir(path)
    run(targets=[{"cachepath": path}], scan_root=str(tmp_path), actor="a", job="j", write_tombstone=False)
    assert not os.path.exists(path)


def test_run_fails_when_a_delete_fails(tmp_path):
    path = make_model(tmp_path)

    def boom(_):
        raise OSError("nope")

    assert run(targets=[{"cachepath": path}], scan_root=str(tmp_path), actor="a", job="j", remove=boom, write_tombstone=False) == 1


# ---------------------------------------------------------------------------
# tombstone
# ---------------------------------------------------------------------------


def test_tombstone_copies_the_manifest(tmp_path):
    path = make_model(tmp_path)
    (entry,) = build_entries([{"cachepath": path, "override": True, "verdict": "soft_block"}])[0]
    payload = tombstone(entry, actor="andrew", job="md-delete-1", override=True, verdict="soft_block")
    assert payload["actor"] == "andrew"
    assert payload["path"] == path
    assert payload["manifest"]["schema"] == "model-downloader.provenance/1.0"
    assert payload["override"] is True
    assert payload["verdict"] == "soft_block"


def test_tombstone_survives_a_missing_manifest(tmp_path):
    path = make_model(tmp_path, manifest=False)
    (entry,) = build_entries([{"cachepath": path}])[0]
    payload = tombstone(entry, actor="a", job="j", override=False)
    assert payload["manifest"] is None


def test_build_entries_reads_the_scan_verdict(tmp_path):
    path = make_model(tmp_path)
    entries, vanished = build_entries([{"cachepath": path, "override": False, "verdict": "deletable"}])
    (entry,) = entries
    assert entry["model_name"] == "acme/Alpha"
    assert entry["manifest"] is True
    assert entry["scanned"] == "clean"
    assert vanished == []
