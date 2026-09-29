"""Unit tests for the wave-4 scanner output contract (storage.py).

Chart >= 1.7 scanner emits six columns: model, mtime, cachepath, manifest,
du-bytes (MD-B) and the ModelScan verdict (MD-A2). Older scanners emit 3 or
4 — the new fields must stay None/"" there (never fabricated). Extraction
from a live `helm template` render pins the parsing to the shipped bytes.
"""

import subprocess
from pathlib import Path

import yaml

from model_downloader.app.storage import DownloadedModel, merge_models, parse_scan_line

REPO = Path(__file__).resolve().parents[1]
HELM_DIR = REPO / "helm"


def _rendered_scan_script() -> str:
    """The scan-job.yaml shell script from a live render."""
    proc = subprocess.run(
        ["helm", "template", "mdtest", ".", "-n", "project-user-x", "--show-only", "templates/configmap-job-template.yaml"],
        cwd=HELM_DIR,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    return cm["data"]["scan-job.yaml"]


class TestSixColumnParsing:
    def test_full_row(self):
        line = "acme/Big\t1727000000\t/mnt/large-models/models--acme--Big\ttrue\t174982693888\tclean"
        m = parse_scan_line(line, "models-pvc", "/mnt/large-models")
        assert m is not None
        assert m.model_name == "acme/Big"
        assert m.provenance_manifest is True
        assert m.cachepath == "/mnt/large-models/models--acme--Big"
        assert m.bytes == 174982693888
        assert m.scanned == "clean"
        assert m.location == "pvc://models-pvc/large-models/models--acme--Big"

    def test_unscanned_verdict_empty(self):
        line = "acme/Old\t1727000000\t/mnt/l/models--acme--Old\tfalse\t512\t"
        m = parse_scan_line(line, "models-pvc", "/mnt/l")
        assert m is not None and m.bytes == 512 and m.scanned == ""

    def test_garbage_bytes_col_is_none(self):
        line = "acme/X\t1727000000\t/mnt/l/models--acme--X\ttrue\tNaN\tclean"
        m = parse_scan_line(line, "models-pvc", "/mnt/l")
        assert m is not None and m.bytes is None

    def test_backward_compat_three_columns(self):
        line = "acme/Old\t1727000000\t/mnt/l/models--acme--Old"
        m = parse_scan_line(line, "models-pvc", "/mnt/l")
        assert m is not None
        assert m.provenance_manifest is None
        assert m.bytes is None
        assert m.scanned == ""
        assert m.cachepath == "/mnt/l/models--acme--Old"

    def test_backward_compat_four_columns(self):
        line = "acme/Old\t1727000000\t/mnt/l/models--acme--Old\ttrue"
        m = parse_scan_line(line, "models-pvc", "/mnt/l")
        assert m is not None
        assert m.provenance_manifest is True
        assert m.bytes is None and m.scanned == ""


class TestRenderedScannerContract:
    def test_rendered_script_emits_six_columns(self):
        script = _rendered_scan_script()
        assert "du -sb" in script
        assert "'%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n'" in script
        assert 'MODELSCAN_VERDICT_FILE' in script

    def test_rendered_verdict_reader_handles_bad_manifest(self):
        """The scanner's embedded verdict reader must never fail a scan on a
        malformed manifest — run the exact rendered reader code."""
        script = _rendered_scan_script()
        start = script.find("scanned=$(MODELSCAN_VERDICT_FILE=")
        assert start >= 0
        # pull the python -c payload between the quotes
        seg = script[start:]
        q1 = seg.find("'") + 1
        q2 = seg.find("'", q1)
        payload = seg[q1:q2]
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "manifest.json")
            Path(bad).write_text("{not json")
            proc = subprocess.run(
                ["python3", "-c", payload],
                env={**os.environ, "MODELSCAN_VERDICT_FILE": bad},
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0
            assert proc.stdout.strip() == ""
            good = os.path.join(tmp, "m2.json")
            Path(good).write_text('{"scan": {"verdict": "suspicious"}}')
            proc = subprocess.run(
                ["python3", "-c", payload],
                env={**os.environ, "MODELSCAN_VERDICT_FILE": good},
                capture_output=True,
                text=True,
            )
            assert proc.stdout.strip() == "suspicious"


class TestMergeCarriesNewFields:
    def test_merge_keeps_newest_bytes_and_scanned(self):
        fresh = DownloadedModel(model_name="m", backends=["pvc"], last_modified=10, bytes=500, scanned="clean", cachepath="/p")
        stale = DownloadedModel(model_name="m", backends=["s3"], last_modified=1)
        merged = merge_models([stale], [fresh])
        assert merged[0].bytes == 500 and merged[0].scanned == "clean" and merged[0].cachepath == "/p"
        # a None never shadows a concrete value
        unknown = DownloadedModel(model_name="m", backends=["pvc"], last_modified=99)
        merged = merge_models([fresh], [unknown])
        assert merged[0].bytes == 500 and merged[0].scanned == "clean"

    def test_to_dict_carries_new_fields(self):
        d = DownloadedModel(model_name="m", bytes=1, scanned="clean", cachepath="/p").to_dict()
        assert d["bytes"] == 1 and d["scanned"] == "clean" and d["cachepath"] == "/p"
