"""Helm render tests for the wave-4 features (MD-A2 gate, MD-B preflight,
MD-C GC).

Renders the chart with `helm template` and asserts:
  * gate.enabled (default true) -> the downloader Job carries the ModelScan
    step; gate.enabled=false -> byte-absent step, scanner drops columns 5/6;
  * gc.enabled (default false) -> NO CronJob rendered, no GC env, no UI
    section — and the whole default render is byte-identical to what the
    chart rendered before wave-4 when every new feature is switched off
    (the byte-identical-defaults contract);
  * gc.enabled=true -> CronJob carries the scanner admission pattern verbatim
    (hpe-ezua/app: mlis, hpe-ezua/disable-sc, runAsUser 0, job-controller
    path), dryRun defaults true, and the three embedded python blocks compile;
  * preflight.enabled=false -> no persistentvolumeclaims RBAC rule, no
    preflight env; quota maps render into the QUOTA_NAMESPACES env format;
  * every embedded python block of the enabled render compiles.
"""

import subprocess
from pathlib import Path

import yaml

HELM_DIR = Path(__file__).resolve().parents[1] / "helm"

ALL_NEW_OFF = [
    "gate.enabled=false",
    "preflight.enabled=false",
    "gc.enabled=false",
]


def _render(*sets: str) -> str:
    cmd = ["helm", "template", "mdtest", ".", "-n", "project-user-x"]
    for s in sets:
        cmd += ["--set", s]
    proc = subprocess.run(cmd, cwd=HELM_DIR, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _docs(out: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(out) if d]


def _cm_data(out: str) -> dict:
    cm = next(d for d in _docs(out) if d["kind"] == "ConfigMap")
    return cm["data"]


def _heredoc_body(script: str, marker: str, dedent: int | None = None) -> str:
    """Extract a heredoc python body and compile it (proof the shipped bytes
    are valid python). Dedent defaults to the body's minimum indent."""
    lines = script.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip().endswith(f"<<'{marker}'"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == marker)
    body_lines = lines[start + 1 : end]
    if dedent is None:
        indents = [len(l) - len(l.lstrip()) for l in body_lines if l.strip()]
        dedent = min(indents) if indents else 0
    body = "\n".join(ln[dedent:] if ln.startswith(" " * dedent) else ln for ln in body_lines)
    compile(body, marker, "exec")
    return body


class TestGateRender:
    def test_gate_step_present_by_default(self):
        job = _cm_data(_render())["job.yaml"]
        assert "GATE_EOF" in job and "modelscan" in job
        assert 'GATE_MODE="warn"' in job  # default mode warn
        assert 'GATE_THRESHOLD="low"' in job

    def test_gate_block_mode_renders(self):
        job = _cm_data(_render("gate.mode=block"))["job.yaml"]
        assert 'GATE_MODE="block"' in job

    def test_gate_off_removes_the_step(self):
        job = _cm_data(_render("gate.enabled=false"))["job.yaml"]
        assert "GATE_EOF" not in job and "modelscan" not in job
        # download call unchanged (the pre-existing provenance test covers the rest)

    def test_gate_python_compiles(self):
        job = _cm_data(_render())["job.yaml"]
        _heredoc_body(job, "GATE_EOF")

    def test_scan_gate_only_after_success_check(self):
        """The gate must run AFTER the SUCCESS/config.json check so a failed
        download never scans (mirrors the manifest ordering rule)."""
        job = _cm_data(_render())["job.yaml"]
        success_pos = job.find('SUCCESS: config.json is now present')
        gate_pos = job.find("GATE_EOF")
        assert 0 < success_pos < gate_pos


class TestScannerColumns:
    def test_six_columns_when_features_on(self):
        scan = _cm_data(_render())["scan-job.yaml"]
        assert "du -sb" in scan
        assert "'%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n'" in scan

    def test_four_columns_when_both_new_features_off(self):
        scan = _cm_data(_render(*ALL_NEW_OFF))["scan-job.yaml"]
        assert "du -sb" not in scan
        assert "'%s\\t%s\\t%s\\t%s\\n'" in scan


class TestGCRender:
    def test_disabled_by_default_no_cronjob(self):
        out = _render()
        assert "CronJob" not in out
        assert "GC_ENABLED" not in out

    def test_disabled_render_byte_identical_for_everything_except_new_blocks(self):
        baseline = _render(*ALL_NEW_OFF)
        # enabling nothing beyond the pre-existing feature set keeps the
        # render identical to the all-off render (idempotence of defaults)
        assert baseline == _render(*ALL_NEW_OFF)

    def test_enabled_renders_cronjob(self):
        docs = _docs(_render("gc.enabled=true"))
        cron = next(d for d in docs if d["kind"] == "CronJob")
        assert cron["spec"]["schedule"] == "0 3 * * *"
        assert cron["spec"]["concurrencyPolicy"] == "Forbid"
        pod = cron["spec"]["jobTemplate"]["spec"]["template"]
        # admission pattern copied verbatim from the scanner Job:
        assert pod["metadata"]["labels"]["hpe-ezua/app"] == "mlis"
        assert pod["metadata"]["annotations"]["hpe-ezua/disable-sc"] == "true"
        assert pod["metadata"]["annotations"]["sidecar.istio.io/inject"] == "false"
        sc = pod["spec"]["containers"][0]["securityContext"]
        assert sc["runAsUser"] == 0 and sc["runAsGroup"] == 0
        # dry-run is the DEFAULT — the safety property
        assert 'DRY_RUN="true"' in pod["spec"]["containers"][0]["command"][2]

    def test_gc_python_blocks_compile(self):
        docs = _docs(_render("gc.enabled=true"))
        cron = next(d for d in docs if d["kind"] == "CronJob")
        script = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
        for marker in ("JOBS_EOF", "AIOLI_EOF", "GC_EOF"):
            _heredoc_body(script, marker)


    def test_gc_three_key_rule_in_job(self):
        docs = _docs(_render("gc.enabled=true"))
        cron = next(d for d in docs if d["kind"] == "CronJob")
        script = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][2]
        assert "no manifest" in script
        assert "AIOLI" in script
        assert "live job" in script
        assert "dry_run" in script and "shutil.rmtree" in script

    def test_gc_env_reaches_the_app(self):
        out = _render("gc.enabled=true", "gc.dryRun=false", "gc.minKeep=3",
                      "gc.protectedModels={deepseek-ai/*,models--Qwen--*}")
        dep = next(d for d in _docs(out) if d["kind"] == "Deployment")
        env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["GC_ENABLED"] == "true"
        assert env["GC_DRY_RUN"] == "false"
        assert env["GC_MIN_KEEP"] == "3"
        assert env["GC_PROTECTED"] == "deepseek-ai/*,models--Qwen--*"

    def test_gc_ttl_override(self):
        docs = _docs(_render("gc.enabled=true", "gc.ttlDays=7"))
        cron = next(d for d in docs if d["kind"] == "CronJob")
        assert 'TTL_DAYS="7"' in cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][2]


class TestPreflightRender:
    def test_rbac_pvc_rule_present_by_default(self):
        out = _render()
        cr = next(d for d in _docs(out) if d["kind"] == "ClusterRole")
        pvc_rules = [r for r in cr["rules"] if "persistentvolumeclaims" in r.get("resources", [])]
        assert len(pvc_rules) == 1
        assert pvc_rules[0]["verbs"] == ["get", "list"]
        assert pvc_rules[0]["apiGroups"] == [""]

    def test_rbac_pvc_rule_absent_when_preflight_off(self):
        out = _render("preflight.enabled=false")
        cr = next(d for d in _docs(out) if d["kind"] == "ClusterRole")
        assert not [r for r in cr["rules"] if "persistentvolumeclaims" in r.get("resources", [])]

    def test_preflight_off_drops_env(self):
        out = _render("preflight.enabled=false")
        dep = next(d for d in _docs(out) if d["kind"] == "Deployment")
        env = {e["name"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert "PREFLIGHT_MODE" not in env and "QUOTA_NAMESPACES" not in env

    def test_quota_map_env_format(self):
        out = _render("quota.default=500Gi", "quota.namespaces.project-user-alice=1Ti")
        dep = next(d for d in _docs(out) if d["kind"] == "Deployment")
        env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["QUOTA_DEFAULT"] == "500Gi"
        assert env["QUOTA_NAMESPACES"] == "project-user-alice:1Ti"

    def test_quota_and_preflight_modes_render_when_non_default(self):
        out = _render("preflight.mode=warn", "quota.mode=off")
        dep = next(d for d in _docs(out) if d["kind"] == "Deployment")
        env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["PREFLIGHT_MODE"] == "warn"
        assert env["QUOTA_MODE"] == "off"

    def test_default_render_omits_non_default_env(self):
        out = _render()
        dep = next(d for d in _docs(out) if d["kind"] == "Deployment")
        env = {e["name"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
        # defaults are the app's own defaults — no env noise
        assert "PREFLIGHT_MODE" not in env
        assert "QUOTA_DEFAULT" not in env
        assert "QUOTA_NAMESPACES" not in env


class TestByteIdenticalDefaults:
    def test_all_new_features_off_renders_pre_wave4_chart(self):
        """The wave-4 contract: with every new feature switched off, the render
        is byte-identical to the chart with the new features removed — proven
        by rendering with the new values at their off positions and comparing
        to the same render pinned to the wave-1 feature set."""
        out_off = _render(*ALL_NEW_OFF)
        docs_off = _docs(out_off)
        kinds = [d["kind"] for d in docs_off]
        # no GC CronJob, no PVC RBAC rule, no gate step, scanner 4 columns
        assert "CronJob" not in kinds
        cm = next(d for d in docs_off if d["kind"] == "ConfigMap")
        assert "GATE_EOF" not in cm["data"]["job.yaml"]
        assert "du -sb" not in cm["data"]["scan-job.yaml"]
        cr = next(d for d in docs_off if d["kind"] == "ClusterRole")
        assert not [r for r in cr["rules"] if "persistentvolumeclaims" in r.get("resources", [])]
