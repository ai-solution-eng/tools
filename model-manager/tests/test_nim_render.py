"""Helm render tests for the NIM profile mirror (MD-NIM, Zone B).

Renders the chart with `helm template` and asserts:
  1. the default render ships a `job-nim.yaml` template key and it is exactly
     one parseable YAML document;
  2. the args are the `mirror s3 -b <bucket> --manifest-file <manifest> --model-cache-path`
     invocation, with the bucket falling back to s3.bucket;
  3. `nim.enabled=false` removes the template key entirely (the app maps a
     missing key to None and refuses NIM submissions);
  4. `nim.bucket` reaches both the Job args and the app's NIM_BUCKET env;
  5. NIM_ENABLED / NIM_BUCKET / NIM_REGION reach the Deployment only when the
     storage backend can actually serve S3 (never with backend: pvc);
  6. `mirror s3` needs no GPU: no nvidia.com/gpu anywhere in the Job;
  7. the MinIO/S3 wiring (endpoint, credentials, path style, regions), the
     NIM cache path and the emptyDir-vs-PVC work volume;
  8. chart-level imagePullSecrets are omitted by default and rendered when set.
"""

import subprocess
from pathlib import Path

import yaml

HELM_DIR = Path(__file__).resolve().parents[1] / "helm"
VALUES_FILE = HELM_DIR / "values.yaml"

NIM_KEY = "job-nim.yaml"


def _render_docs(*sets: str) -> list:
    """Render the chart; each *sets* element is a `key=value` for --set."""
    cmd = ["helm", "template", "mdtest", ".", "-n", "project-user-x"]
    for s in sets:
        cmd += ["--set", s]
    proc = subprocess.run(cmd, cwd=HELM_DIR, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def _render(*sets: str) -> dict:
    """The job-template ConfigMap's data map (the app's template keys)."""
    cm = next(d for d in _render_docs(*sets) if d["kind"] == "ConfigMap")
    return cm["data"]


def _deployment_env(*sets: str) -> dict:
    """Map of env name -> value for the app container (valueFrom entries skipped)."""
    dep = next(d for d in _render_docs(*sets) if d["kind"] == "Deployment")
    return {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"] if "value" in e}


def _job(*sets: str) -> dict:
    """The rendered job-nim.yaml, parsed as the single document it must be."""
    docs = list(yaml.safe_load_all(_render(*sets)[NIM_KEY]))
    assert len(docs) == 1, "job-nim.yaml must be exactly one YAML document"
    return docs[0]


def _pod_spec(job: dict) -> dict:
    return job["spec"]["template"]["spec"]


def _container(job: dict) -> dict:
    containers = _pod_spec(job)["containers"]
    assert [c["name"] for c in containers] == ["nim-mirror"]
    return containers[0]


def _env(container: dict) -> dict:
    """Map of env name -> plain value (valueFrom entries are skipped)."""
    return {e["name"]: e.get("value") for e in container["env"] if "value" in e}


def _args(*sets: str) -> list:
    """Job args with the app's bucket substitution applied.

    The chart cannot bake the bucket in: the template carries the literal
    `__NIM_BUCKET__` placeholder and the app replaces it with the Deployment's
    NIM_BUCKET (nim.bucket, default s3.bucket). Mirror that substitution here so
    the assertion covers the real invocation the Job is created with.
    """
    args = _container(_job(*sets))["args"]
    bucket = _deployment_env(*sets)["NIM_BUCKET"]
    return [a.replace("__NIM_BUCKET__", bucket) for a in args]


def _values() -> dict:
    return yaml.safe_load(VALUES_FILE.read_text())


# ---------------------------------------------------------------------------
# 1. the template key exists by default and is one clean YAML document
# ---------------------------------------------------------------------------


class TestTemplateKey:
    def test_default_render_ships_the_nim_key(self):
        assert NIM_KEY in _render()

    def test_key_is_one_yaml_document(self):
        text = _render()[NIM_KEY]
        # helm must have substituted every expression: a leftover {{ would mean
        # a template bug that only shows up when the app creates the Job.
        assert "{{" not in text
        assert "{%" not in text
        docs = list(yaml.safe_load_all(text))
        assert len(docs) == 1
        assert docs[0]["apiVersion"] == "batch/v1"
        assert docs[0]["kind"] == "Job"

    def test_placeholders_are_the_literal_names_the_app_replaces(self):
        text = _render()[NIM_KEY]
        for placeholder in (
            "__JOB_NAME__",
            "__NAMESPACE__",
            "__NIM_IMAGE__",
            "__NGC_SECRET__",
            "__NIM_BUCKET__",
        ):
            assert placeholder in text, placeholder

    def test_manifest_shape_and_admission(self):
        job = _job()
        assert job["spec"]["ttlSecondsAfterFinished"] == _values()["nim"]["ttlSecondsAfterFinished"]
        assert job["spec"]["backoffLimit"] == _values()["nim"]["backoffLimit"]

        template = job["spec"]["template"]
        # Admission copied from the downloader Jobs: the platform's
        # protect-models-pvc policy keys off these.
        assert template["metadata"]["annotations"]["sidecar.istio.io/inject"] == "false"
        assert template["metadata"]["annotations"]["hpe-ezua/disable-sc"] == "true"
        assert template["metadata"]["labels"]["hpe-ezua/app"] == "mlis"
        assert template["metadata"]["labels"]["model-manager/source"] == "nim"
        assert job["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "model-manager"
        assert job["metadata"]["labels"]["model-manager/source"] == "nim"

        pod = _pod_spec(job)
        assert pod["restartPolicy"] == "OnFailure"
        container = _container(job)
        assert container["image"] == "__NIM_IMAGE__"
        assert container["imagePullPolicy"] == "IfNotPresent"
        assert container["securityContext"]["runAsUser"] == 0
        assert container["securityContext"]["runAsGroup"] == 0

    def test_disable_security_context_is_configurable(self):
        annotations = _job("nim.disableSecurityContext=false")["spec"]["template"]["metadata"]["annotations"]
        assert annotations["hpe-ezua/disable-sc"] == "false"

    def test_node_selector_propagates(self):
        pod = _pod_spec(_job("nim.nodeSelector.disktype=fast"))
        assert pod["nodeSelector"] == {"disktype": "fast"}


# ---------------------------------------------------------------------------
# 2. the args are the mirror s3 invocation
# ---------------------------------------------------------------------------


class TestArgs:
    def test_template_carries_the_bucket_placeholder(self):
        args = _container(_job())["args"]
        assert args[2] == "-b"
        assert args[3] == "__NIM_BUCKET__"

    def test_args_are_the_mirror_invocation(self):
        args = _args()
        assert args[:2] == ["mirror", "s3"]
        assert args[2] == "-b"
        # nim.bucket is empty by default -> s3.bucket is the fallback.
        assert args[3] == "mlis-models"
        assert args[4] == "--manifest-file"
        assert args[5] == "/opt/nim/etc/default/model_manifest.yaml"
        assert args[6:] == ["--model-cache-path", "/opt/nim/.cache"]
        assert len(args) == 8

    def test_manifest_path_is_configurable(self):
        args = _container(_job("nim.manifestPath=/opt/nim/etc/custom.yaml"))["args"]
        assert args[args.index("--manifest-file") + 1] == "/opt/nim/etc/custom.yaml"


# ---------------------------------------------------------------------------
# 3. nim.enabled=false removes the key
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_disabled_removes_the_key(self):
        assert NIM_KEY not in _render("nim.enabled=false")

    def test_disabled_removes_the_app_env(self):
        env = _deployment_env("nim.enabled=false")
        for name in ("NIM_ENABLED", "NIM_BUCKET", "NIM_REGION"):
            assert name not in env


# ---------------------------------------------------------------------------
# 4. nim.bucket reaches the args and the deployment env
# ---------------------------------------------------------------------------


class TestBucketOverride:
    def test_bucket_override_reaches_args(self):
        args = _args("nim.bucket=nim-profiles")
        assert args[3] == "nim-profiles"

    def test_bucket_override_reaches_the_deployment_env(self):
        assert _deployment_env("nim.bucket=nim-profiles")["NIM_BUCKET"] == "nim-profiles"


# ---------------------------------------------------------------------------
# 5. the app env is gated on an S3-capable backend
# ---------------------------------------------------------------------------


class TestDeploymentEnv:
    def test_env_present_by_default(self):
        env = _deployment_env()
        assert env["NIM_ENABLED"] == "true"
        assert env["NIM_BUCKET"] == "mlis-models"
        assert env["NIM_REGION"] == "us-east-1"

    def test_region_is_configurable(self):
        assert _deployment_env("nim.region=eu-west-2")["NIM_REGION"] == "eu-west-2"

    def test_env_absent_for_pvc_backend(self):
        # backend: pvc has no bucket to mirror into; the app must see NIM as
        # unavailable (no NIM_ENABLED, no bucket).
        env = _deployment_env("storage.backend=pvc")
        for name in ("NIM_ENABLED", "NIM_BUCKET", "NIM_REGION"):
            assert name not in env


# ---------------------------------------------------------------------------
# 6. mirror s3 needs no GPU
# ---------------------------------------------------------------------------


class TestNoGpu:
    def test_job_requests_no_gpu(self):
        text = _render()[NIM_KEY]
        assert "nvidia.com/gpu" not in text
        container = _container(_job())
        resources = container["resources"]
        assert "nvidia.com/gpu" not in (resources.get("limits") or {})
        assert "nvidia.com/gpu" not in (resources.get("requests") or {})
        assert "nvidia.com/gpu" not in yaml.safe_dump(_job())


# ---------------------------------------------------------------------------
# 7. MinIO / S3-compatible wiring, cache path, work volume
# ---------------------------------------------------------------------------


class TestMinioWiring:
    def test_aws_env_matches_the_s3_values(self):
        s3 = _values()["s3"]
        env = _env(_container(_job()))
        assert env["AWS_ENDPOINT_URL"] == s3["endpointUrl"]
        assert env["AWS_ACCESS_KEY_ID"] == s3["accessKeyId"]
        assert env["AWS_SECRET_ACCESS_KEY"] == s3["secretAccessKey"]
        assert env["AWS_S3_USE_PATH_STYLE"] == "true"
        assert env["AWS_REGION"] == "us-east-1"
        assert env["AWS_DEFAULT_REGION"] == "us-east-1"

    def test_credentials_and_endpoint_come_from_the_s3_values(self):
        s3 = _values()["s3"]
        env = _env(
            _container(
                _job(
                    "s3.accessKeyId=minio-key",
                    "s3.secretAccessKey=minio-secret",
                    "s3.endpointUrl=http://minio.example:9000",
                )
            )
        )
        assert env["AWS_ACCESS_KEY_ID"] == "minio-key"
        assert env["AWS_SECRET_ACCESS_KEY"] == "minio-secret"
        assert env["AWS_ENDPOINT_URL"] == "http://minio.example:9000"
        # Sanity: the fixtures above are test-only values, not the site keys.
        assert env["AWS_ACCESS_KEY_ID"] != s3["accessKeyId"]

    def test_path_style_can_be_turned_off(self):
        assert _env(_container(_job("nim.pathStyle=false")))["AWS_S3_USE_PATH_STYLE"] == "false"

    def test_nim_cache_path_and_mount(self):
        container = _container(_job())
        assert _env(container)["NIM_CACHE_PATH"] == "/opt/nim/.cache"
        mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
        assert mounts == {"nim-cache": "/opt/nim/.cache"}

    def test_cache_volume_is_a_sized_emptydir(self):
        volumes = {v["name"]: v for v in _pod_spec(_job())["volumes"]}
        assert volumes["nim-cache"]["emptyDir"]["sizeLimit"] == _values()["nim"]["workSize"]
        assert "persistentVolumeClaim" not in volumes["nim-cache"]

    def test_work_size_is_configurable(self):
        volumes = {v["name"]: v for v in _pod_spec(_job("nim.workSize=5Gi"))["volumes"]}
        assert volumes["nim-cache"]["emptyDir"]["sizeLimit"] == "5Gi"

    def test_work_pvc_name_switches_the_volume(self):
        volumes = {v["name"]: v for v in _pod_spec(_job("nim.workPvcName=nim-work-pvc"))["volumes"]}
        assert volumes["nim-cache"]["persistentVolumeClaim"]["claimName"] == "nim-work-pvc"
        assert "emptyDir" not in volumes["nim-cache"]

    def test_proxy_block_is_wired_only_when_set(self):
        assert not [k for k in _env(_container(_job())) if "proxy" in k.lower()]
        env = _env(_container(_job("proxy.noProxy=localhost", "proxy.https=squid:3128", "proxy.http=squid:3128")))
        assert env["no_proxy"] == "localhost" and env["NO_PROXY"] == "localhost"
        assert env["https_proxy"] == "squid:3128" and env["HTTPS_PROXY"] == "squid:3128"
        assert env["http_proxy"] == "squid:3128" and env["HTTP_PROXY"] == "squid:3128"


# ---------------------------------------------------------------------------
# 8. chart-level imagePullSecrets
# ---------------------------------------------------------------------------


class TestImagePullSecrets:
    def test_absent_by_default(self):
        assert "imagePullSecrets" not in _pod_spec(_job())

    def test_present_when_configured(self):
        pod = _pod_spec(_job("nim.imagePullSecrets[0]=ngc-secret"))
        assert pod["imagePullSecrets"] == [{"name": "ngc-secret"}]

    def test_multiple_secrets_render_in_order(self):
        pod = _pod_spec(_job("nim.imagePullSecrets[0]=ngc-secret", "nim.imagePullSecrets[1]=local-regcred"))
        assert [s["name"] for s in pod["imagePullSecrets"]] == ["ngc-secret", "local-regcred"]
