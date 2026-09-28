"""Helm render tests for the MD-A1 provenance manifest feature.

Renders the chart with `helm template` and asserts:
  * provenance.enabled (default) -> the downloader Job carries the manifest
    step, the revision env vars, and revision pinning in snapshot_download;
  * provenance.enabled=false -> none of that exists, and the download call is
    unchanged from the pre-provenance behavior (no revision kwarg);
  * the scanner job always emits the manifest-presence column;
  * both embedded python blocks of the enabled render compile.
"""

import subprocess
from pathlib import Path

import yaml

HELM_DIR = Path(__file__).resolve().parents[1] / "helm"


def _render(*sets: str) -> dict:
    """Render the chart; each *sets* element is a `key=value` for --set."""
    cmd = ["helm", "template", "mdtest", ".", "-n", "project-user-x"]
    for s in sets:
        cmd += ["--set", s]
    proc = subprocess.run(cmd, cwd=HELM_DIR, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    return cm["data"]


def _job(data: dict, key: str = "job.yaml") -> str:
    return data[key]


def _embedded_python(script: str, start_marker: str, end_marker: str, dedent: int) -> str:
    lines = script.splitlines()
    s = next(i for i, ln in enumerate(lines) if ln.strip().startswith(start_marker))
    e = next(i for i in range(s + 1, len(lines)) if lines[i].strip() == end_marker)
    body = "\n".join(ln[dedent:] if ln.startswith(" " * dedent) else ln for ln in lines[s + 1 : e])
    compile(body, start_marker, "exec")
    return body


class TestProvenanceEnabled:
    def test_manifest_step_present(self):
        job = _job(_render())
        assert job.count("PROV_EOF") == 2  # heredoc open + close
        assert "manifest.json" in job

    def test_revision_env_and_pinning(self):
        job = _job(_render())
        assert "revision=os.environ['PROVENANCE_REVISION']" in job
        env_block = job.split("env:", 1)[1]
        for name in ("PROVENANCE_REVISION", "JOB_ID", "JOB_NAMESPACE", "SUBMITTED_BY"):
            assert f"- name: {name}" in env_block, name

    def test_provenance_env_gated(self):
        data = _render("provenance.enabled=false")
        job = _job(data)
        for absent in ("PROVENANCE_REVISION", "JOB_ID", "JOB_NAMESPACE", "SUBMITTED_BY", "PROV_EOF"):
            assert absent not in job, absent
        # The download call itself is unchanged from pre-provenance behavior.
        assert "revision=" not in job

    def test_embedded_python_compiles(self):
        job = _job(_render())
        _embedded_python(job, 'python3 -c "', '"', 10)
        _embedded_python(job, "python3 - <<'PROV_EOF'", "PROV_EOF", 10)


class TestRevisionValue:
    def test_custom_revision_propagates(self):
        job = _job(_render("provenance.revision=v2.1"))
        assert '- name: PROVENANCE_REVISION\n          value: "v2.1"' in job
        assert 'PROV_REV="v2.1"' in job

    def test_custom_revision_reaches_snapshot_download(self):
        job = _job(_render("provenance.revision=v2.1"))
        assert "revision=os.environ['PROVENANCE_REVISION']" in job


class TestScannerJob:
    def test_manifest_column(self):
        scan = _job(_render(), "scan-job.yaml")
        assert 'manifest="true"' in scan and 'manifest="false"' in scan
        # Chart >= 1.7: six columns — model, mtime, cachepath, manifest,
        # du bytes (MD-B) and the ModelScan verdict (MD-A2).
        assert "'%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n'" in scan
        assert "du -sb" in scan


class TestOtherTemplatesUnaffected:
    def test_s3_job_has_no_provenance_step(self):
        job = _job(_render(), "job-s3.yaml")
        assert "PROV_EOF" not in job

    def test_debug_job_untouched(self):
        job = _job(_render(), "debug-job.yaml")
        assert "PROVENANCE" not in job
