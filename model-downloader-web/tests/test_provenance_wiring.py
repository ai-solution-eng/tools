"""Unit tests for the app-side provenance wiring (MD-A1).

Covers the scanner output contract (k8s.py _render placeholder substitution +
storage.py parsing of the scanner's manifest column) without a cluster: the
K8sClient template store is populated directly from the rendered ConfigMap.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from model_downloader.app.k8s import K8sClient
from model_downloader.app.storage import DownloadedModel, merge_models, parse_scan_line

REPO = Path(__file__).resolve().parents[1]
HELM_DIR = REPO / "helm"


def _rendered_templates() -> dict:
    proc = subprocess.run(
        ["helm", "template", "mdtest", ".", "-n", "project-user-x"],
        cwd=HELM_DIR,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    return cm["data"]


@pytest.fixture()
def k8s() -> K8sClient:
    c = K8sClient(template_cm="cm", template_cm_ns="ns")
    data = _rendered_templates()
    # Mirror K8sClient.start()'s ConfigMap-key -> template-name mapping.
    c._templates = {
        "pvc": data["job.yaml"],
        "s3": data.get("job-s3.yaml") or None,
        "debug-job": data.get("debug-job.yaml") or None,
        "scan-job": data.get("scan-job.yaml") or None,
    }
    return c


def test_render_substitutes_provenance_placeholders(k8s: K8sClient):
    manifest = k8s._render(
        "project-user-test",
        "acme/Test-Model",
        "md-acme-test-abc123",
        "hf-token-acme-test-abc123",
        job_id="f00dfeed1234",
        submitted_by="mcp:fp-abc",
    )
    script = manifest["spec"]["template"]["spec"]["containers"][0]["command"][2]
    env = {e["name"]: e.get("value", "") for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["JOB_ID"] == "f00dfeed1234"
    assert env["SUBMITTED_BY"] == "mcp:fp-abc"
    assert env["JOB_NAMESPACE"] == "project-user-test"
    assert env["PROVENANCE_REVISION"] == "main"
    assert "__JOB_ID__" not in script and "__SUBMITTED_BY__" not in script


def test_render_defaults_job_id_to_job_name(k8s: K8sClient):
    manifest = k8s._render(
        "project-user-test",
        "acme/Test-Model",
        "md-acme-test-abc123",
        "hf-token-acme-test-abc123",
    )
    env = {e["name"]: e.get("value", "") for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"]}
    # call sites that don't pass job_id still get a useful identifier
    assert env["JOB_ID"] == "md-acme-test-abc123"
    assert env["SUBMITTED_BY"] == ""


# ---- scanner output parsing ----


def test_parse_scanner_manifest_column_true():
    line = "acme/Test-Model\t1727000000\t/mnt/large-models/models--acme--Test-Model\ttrue"
    m = parse_scan_line(line, "models-pvc", "/mnt/large-models")
    assert m is not None
    assert m.provenance_manifest is True
    assert m.location == "pvc://models-pvc/large-models/models--acme--Test-Model"


def test_parse_scanner_manifest_column_false():
    line = "acme/Test-Model\t1727000000\t/mnt/l/models--acme--Test-Model\tfalse"
    m = parse_scan_line(line, "models-pvc", "/mnt/l")
    assert m is not None
    assert m.provenance_manifest is False


def test_parse_scanner_three_columns_is_unknown():
    # Older chart's scanner emitted three columns — flag must be None, not False.
    line = "acme/Test-Model\t1727000000\t/mnt/l/models--acme--Test-Model"
    m = parse_scan_line(line, "models-pvc", "/mnt/l")
    assert m is not None
    assert m.provenance_manifest is None


def test_parse_scanner_skips_garbage():
    assert parse_scan_line("", "models-pvc", "/mnt/l") is None
    assert parse_scan_line("no-tabs-here", "models-pvc", "/mnt/l") is None
    assert parse_scan_line("\t1727000000", "models-pvc", "/mnt/l") is None
    m = parse_scan_line("acme/M\tNaN\t/mnt/l/models--acme--M", "models-pvc", "/mnt/l")
    # float('NaN') parses as NaN (pre-existing scanner behavior) — not a crash.
    assert m is not None and m.last_modified != m.last_modified  # NaN check


def test_merge_prefers_concrete_provenance_flag():
    known = DownloadedModel(model_name="m", backends=["pvc"], provenance_manifest=True)
    unknown = DownloadedModel(model_name="m", backends=["s3"], provenance_manifest=None, last_modified=1)
    merged = merge_models([unknown], [known])
    assert len(merged) == 1
    assert merged[0].provenance_manifest is True
    assert merged[0].backends == ["s3", "pvc"]  # union preserves first-seen order
    flipped = DownloadedModel(model_name="m", backends=["pvc"], provenance_manifest=False, last_modified=2)
    merged = merge_models([known], [flipped])
    assert merged[0].provenance_manifest is False
