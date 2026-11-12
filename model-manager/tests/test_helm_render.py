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


def _render_docs(*sets: str) -> list:
    """Render the chart; return every parsed document (Deployment and friends)."""
    cmd = ["helm", "template", "mdtest", ".", "-n", "project-user-x"]
    for s in sets:
        cmd += ["--set", s]
    proc = subprocess.run(cmd, cwd=HELM_DIR, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _deployment_env(docs: list) -> dict:
    """Map of env name -> value for the app container (valueFrom entries skipped)."""
    dep = next(d for d in docs if d["kind"] == "Deployment")
    return {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}


class TestManualDeleteDisabled:
    def test_no_template_key(self):
        # Default values: manualDelete.enabled=false.
        data = _render()
        assert "manual-delete-job.yaml" not in data

    def test_deployment_has_no_manual_delete_env(self):
        env = _deployment_env(_render_docs())
        assert not any(k.startswith("MANUAL_DELETE_") for k in env)


class TestManualDeleteEnabled:
    def test_template_key_present(self):
        data = _render("manualDelete.enabled=true")
        assert "manual-delete-job.yaml" in data

    def test_placeholders_admission_and_shape(self):
        job = _job(_render("manualDelete.enabled=true"), "manual-delete-job.yaml")
        for ph in (
            "__JOB_NAME__",
            "__NAMESPACE__",
            "__PVC_NAME__",
            "__MOUNT_PATH__",
            "__SCAN_ROOT__",
            "__IMAGE__",
            "__TARGETS_B64__",
            "__ACTOR__",
            "__JOB_TTL__",
        ):
            assert ph in job, ph
        # Admission copied verbatim from the GC CronJob / scanner Job.
        assert "hpe-ezua/app: mlis" in job
        assert 'hpe-ezua/disable-sc: "true"' in job
        assert 'sidecar.istio.io/inject: "false"' in job
        assert "runAsUser: 0" in job
        assert "runAsGroup: 0" in job
        assert "backoffLimit: 0" in job
        assert "restartPolicy: Never" in job
        assert 'command: ["python3", "-m", "app.delete_targets"]' in job

    def test_mount_is_readwrite(self):
        job = _job(_render("manualDelete.enabled=true"), "manual-delete-job.yaml")
        # No readOnly flag anywhere: the delete Job writes tombstones and
        # removes dirs on the PVC. The scanner Job is the only readOnly mount.
        assert "readOnly" not in job
        assert "claimName: __PVC_NAME__" in job

    def test_service_account_is_omitted(self):
        # The delete Job runs in the namespace that OWNS models-pvc, where the
        # app's ServiceAccount does not exist. Naming one there leaves the pod
        # Pending forever ("serviceaccount not found"), so the template must
        # omit it and use the namespace default — same as the download and scan
        # Jobs, which need no cluster RBAC either.
        import yaml

        text = _job(_render("manualDelete.enabled=true"), "manual-delete-job.yaml")
        spec = yaml.safe_load(text)
        assert spec["spec"]["template"]["spec"].get("serviceAccountName") is None

    def test_no_aioli_or_rbac_env(self):
        # The AIOLI / live-job checks moved to commit time in the app; the pod
        # must not need cluster RBAC or the MLIS password.
        data = _render("manualDelete.enabled=true")
        job = data["manual-delete-job.yaml"]
        assert "AIOLI_DB" not in job
        assert "GC_PROTECTED" not in job

    def test_image_defaults_to_app_image_not_scan_image(self):
        data = _render("manualDelete.enabled=true")
        docs = _render_docs("manualDelete.enabled=true")
        dep = next(d for d in docs if d["kind"] == "Deployment")
        app_image = dep["spec"]["template"]["spec"]["containers"][0]["image"]
        env = _deployment_env(docs)
        # manualDelete.image empty by default -> the APP image (it ships
        # app.delete_targets + the kubernetes client), never the scanner.
        assert env["MANUAL_DELETE_IMAGE"] == app_image
        job = data["manual-delete-job.yaml"]
        assert "image: __IMAGE__" in job
        assert "basic-ubuntu" not in job

    def test_custom_image_propagates(self):
        sets = ("manualDelete.enabled=true", "manualDelete.image=ghcr.io/x/delete:v9")
        env = _deployment_env(_render_docs(*sets))
        assert env["MANUAL_DELETE_IMAGE"] == "ghcr.io/x/delete:v9"

    def test_ttl_env_matches_job_ttl_seconds(self):
        sets = ("manualDelete.enabled=true", "manualDelete.jobTtlSeconds=3600")
        job = _job(_render(*sets), "manual-delete-job.yaml")
        assert "ttlSecondsAfterFinished: __JOB_TTL__" in job
        env = _deployment_env(_render_docs(*sets))
        assert env["MANUAL_DELETE_JOB_TTL"] == "3600"

    def test_managed_by_labels(self):
        job = _job(_render("manualDelete.enabled=true"), "manual-delete-job.yaml")
        assert "app.kubernetes.io/managed-by: model-manager-delete" in job
        assert 'model-manager/delete: "true"' in job

    def test_targets_b64_is_plain_env_value(self):
        job = _job(_render("manualDelete.enabled=true"), "manual-delete-job.yaml")
        assert "- name: MD_TARGETS_B64" in job
        # Quoted plain env value placeholder — raw JSON is never spliced in.
        assert 'value: "__TARGETS_B64__"' in job

    def test_secret_namespace_defaults_to_release(self):
        # Rendered with -n project-user-x, manualDelete.auth.secret.namespace
        # empty -> the release namespace.
        env = _deployment_env(_render_docs("manualDelete.enabled=true"))
        assert env["MANUAL_DELETE_SECRET_NAMESPACE"] == "project-user-x"

    def test_secret_namespace_override(self):
        env = _deployment_env(_render_docs("manualDelete.enabled=true", "manualDelete.auth.secret.namespace=mlis"))
        assert env["MANUAL_DELETE_SECRET_NAMESPACE"] == "mlis"

    def test_admin_group_defaults_to_pcai_admin(self):
        # PCAI's admin tier IS membership in the Keycloak group "admin": the
        # EzUA policy server grants admin on `"admin" in input.groups`
        # (ezuser-system/ez-policy-server-rego-cm), and oauth2-proxy forwards
        # the `groups` claim as X-Auth-Request-Groups. Shipping it as the
        # default means every AIE Admin can use the page with no per-person
        # values edit — and `admins` stays a union with it, not a replacement.
        env = _deployment_env(_render_docs("manualDelete.enabled=true"))
        assert env["MANUAL_DELETE_ADMIN_GROUPS"] == "admin"
        assert env["MANUAL_DELETE_ADMINS"] == ""
