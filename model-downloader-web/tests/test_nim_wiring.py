"""Wiring tests for NIM profile mirroring (source="nim").

Covers the app-side contract: request validation, template rendering, the two
per-submission Secrets, the queue path (no PVC preflight), reconciliation from
Job annotations, and Secret cleanup on watch/remove. No cluster is touched.
"""

import sys
import types

if "psycopg2" not in sys.modules:  # pragma: no cover - environment shim
    _pg = types.ModuleType("psycopg2")
    _pg.__path__ = []
    _pg.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no db in tests"))
    _ext = types.ModuleType("psycopg2.extensions")
    _ext.ISOLATION_LEVEL_AUTOCOMMIT = 0
    _extras = types.ModuleType("psycopg2.extras")
    _extras.Json = lambda value: value
    sys.modules["psycopg2"] = _pg
    sys.modules["psycopg2.extensions"] = _ext
    sys.modules["psycopg2.extras"] = _extras

import asyncio
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="md-nim-")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP", "test-template")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP_NAMESPACE", "test-ns")
os.environ["CATALOG_PATH"] = os.path.join(_TMP, "catalog.json")
os.environ["NIM_ENABLED"] = "true"
os.environ["NIM_BUCKET"] = "nim-profiles"
os.environ["STORAGE_BACKEND"] = "both"

import pytest  # noqa: E402
from kubernetes.client.rest import ApiException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from model_downloader.app import main as md  # noqa: E402
from model_downloader.app.k8s import (  # noqa: E402
    NGC_SECRET_ANNOTATION,
    NIM_BUCKET_ANNOTATION,
    NIM_IMAGE_ANNOTATION,
    NIM_OVERRIDE_ANNOTATION,
    NIM_PROFILES_ANNOTATION,
    SOURCE_ANNOTATION,
    K8sClient,
)
from model_downloader.app.queue import JobQueue, JobRecord  # noqa: E402

IMAGE = "nvcr.io/nim/meta/llama-3.1-8b-instruct:1.0.0"
KEY = "nvapi-test-key"

NIM_TEMPLATE = """
apiVersion: batch/v1
kind: Job
metadata:
  name: __JOB_NAME__
  namespace: __NAMESPACE__
spec:
  template:
    spec:
      containers:
      - name: nim-mirror
        image: "__NIM_IMAGE__"
        args:
        - "mirror"
        - "s3"
        - "-b"
        - "__NIM_BUCKET__"
        - "--manifest-file"
        - "/opt/nim/etc/default/model_manifest.yaml"
        - "--model-cache-path"
        - "/opt/nim/.cache"
        env:
        - name: NGC_API_KEY
          valueFrom:
            secretKeyRef:
              name: __NGC_SECRET__
              key: NGC_API_KEY
"""


# ---------------------------------------------------------------------------
# SubmitRequest
# ---------------------------------------------------------------------------


def _nim(**over):
    kwargs = {"namespace": "project-user-x", "source": "nim", "nim_image": IMAGE, "ngc_api_key": KEY}
    kwargs.update(over)
    return md.SubmitRequest(**kwargs)


def test_hf_submission_unchanged():
    req = md.SubmitRequest(namespace="project-user-x", model_name="org/Repo", hf_token="hf_x")
    assert req.source == "hf"
    assert req.storage == "pvc"
    assert req.model_name == "org/Repo"


def test_hf_requires_model_and_token():
    with pytest.raises(ValidationError):
        md.SubmitRequest(namespace="project-user-x", model_name="", hf_token="hf_x")
    with pytest.raises(ValidationError):
        md.SubmitRequest(namespace="project-user-x", model_name="org/Repo", hf_token="")


def test_nim_forces_s3_and_clears_hf_fields(monkeypatch):
    monkeypatch.setattr(md, "NIM_AVAILABLE", True)
    req = _nim(
        storage="pvc",
        s3_path="s3://other/prefix/",
        cache_root="/mnt/custom",
        chat_template_path="chat.jinja",
        chat_template_contents="body",
        hf_token="hf_x",
    )
    assert req.storage == "s3"
    assert req.s3_path == ""
    assert req.cache_root == ""
    assert req.chat_template_path == ""
    assert req.chat_template_contents == ""
    assert req.hf_token == ""
    assert req.model_name == IMAGE


def test_nim_requires_image_and_key(monkeypatch):
    monkeypatch.setattr(md, "NIM_AVAILABLE", True)
    with pytest.raises(ValidationError):
        _nim(nim_image="")
    with pytest.raises(ValidationError):
        _nim(ngc_api_key="")


def test_nim_disabled_is_rejected(monkeypatch):
    monkeypatch.setattr(md, "NIM_AVAILABLE", False)
    with pytest.raises(ValidationError):
        _nim()


def test_nim_profiles_normalized_and_deduped(monkeypatch):
    monkeypatch.setattr(md, "NIM_AVAILABLE", True)
    req = _nim(nim_profiles="abc, def  abc")
    assert req.nim_profiles == "abc def"


def test_nim_profiles_reject_bad_id(monkeypatch):
    monkeypatch.setattr(md, "NIM_AVAILABLE", True)
    with pytest.raises(ValidationError):
        _nim(nim_profiles="abc bad!")


def test_nim_image_requires_tag_or_digest(monkeypatch):
    monkeypatch.setattr(md, "NIM_AVAILABLE", True)
    with pytest.raises(ValidationError):
        _nim(nim_image="nvcr.io/nim/meta/llama-3.1-8b-instruct")


def test_ngc_key_rejects_whitespace(monkeypatch):
    monkeypatch.setattr(md, "NIM_AVAILABLE", True)
    with pytest.raises(ValidationError):
        _nim(ngc_api_key="nvapi abc")


def test_page_context_exposes_nim_when_available(monkeypatch):
    monkeypatch.setattr(md, "NIM_AVAILABLE", True)
    monkeypatch.setattr(md, "NIM_BUCKET", "nim-profiles")
    monkeypatch.setattr(md, "NIM_REGION", "us-east-1")
    ctx = asyncio.run(md._page_context(None))
    assert ctx["nim_enabled"] is True
    assert ctx["nim_bucket"] == "nim-profiles"
    assert ctx["nim_override"] == "s3://nim-profiles"


# ---------------------------------------------------------------------------
# K8sClient rendering / secret creation
# ---------------------------------------------------------------------------


def _client():
    client = K8sClient("cm", "ns")
    client._templates = {"nim": NIM_TEMPLATE}
    return client


def test_render_nim_substitutes_and_appends_profiles():
    manifest = _client()._render_nim(
        "project-user-x", IMAGE, "md-nim-x", "ngc-secret", "nim-profiles", profiles="abc def", job_id="job123"
    )
    assert manifest["metadata"]["name"] == "md-nim-x"
    assert manifest["metadata"]["namespace"] == "project-user-x"
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == IMAGE
    assert container["args"][-3:] == ["--profiles", "abc", "def"]
    ann = manifest["metadata"]["annotations"]
    assert ann[SOURCE_ANNOTATION] == "nim"
    assert ann[NIM_IMAGE_ANNOTATION] == IMAGE
    assert ann[NIM_PROFILES_ANNOTATION] == "abc def"
    assert ann[NIM_BUCKET_ANNOTATION] == "nim-profiles"
    assert ann[NIM_OVERRIDE_ANNOTATION] == "s3://nim-profiles"
    assert ann[NGC_SECRET_ANNOTATION] == "ngc-secret"


def test_render_nim_omits_profiles_when_empty():
    manifest = _client()._render_nim("ns", IMAGE, "job", "s", "b")
    args = manifest["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--profiles" not in args
    assert NIM_PROFILES_ANNOTATION not in manifest["metadata"]["annotations"]


def test_render_nim_missing_template_is_a_clear_error():
    client = K8sClient("cm", "ns")
    client._templates = {}
    with pytest.raises(RuntimeError, match="job-nim.yaml|nim job template"):
        client._render_nim("ns", IMAGE, "job", "s", "b")


class _FakeCore:
    def __init__(self, fail_secret_at=0):
        self.secrets = []
        self.fail_secret_at = fail_secret_at

    def create_namespaced_secret(self, namespace, body):
        if self.fail_secret_at and len(self.secrets) + 1 == self.fail_secret_at:
            raise ApiException(status=403, reason="denied")
        self.secrets.append(body)


class _FakeBatch:
    def __init__(self, fail=False):
        self.body = None
        self.fail = fail

    def create_namespaced_job(self, namespace, body):
        if self.fail:
            raise ApiException(status=422, reason="denied")
        self.body = body


def test_create_nim_job_creates_both_secrets_and_pull_secret():
    client = _client()
    core, batch = _FakeCore(), _FakeBatch()
    client._core, client._batch = core, batch
    ngc, pull, job = asyncio.run(client.create_nim_job("ns", IMAGE, "abc", "job1", KEY, "nim-profiles"))
    assert job.startswith("md-nim-") and ngc.startswith("ngc-api-") and pull.startswith("ngc-pull-")
    assert len(core.secrets) == 2
    types_ = {s.type for s in core.secrets}
    assert types_ == {"Opaque", "kubernetes.io/dockerconfigjson"}
    assert batch.body["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": pull}]
    assert batch.body["metadata"]["labels"]["model-downloader/job-id"] == "job1"
    assert batch.body["metadata"]["annotations"]["model-downloader/nim-pull-secret"] == pull


def test_create_nim_job_skips_pull_secret_for_local_registry():
    client = _client()
    core, batch = _FakeCore(), _FakeBatch()
    client._core, client._batch = core, batch
    _ngc, pull, _job = asyncio.run(
        client.create_nim_job("ns", "registry.local/nim/llama:1.0", "", "job2", KEY, "nim-profiles")
    )
    assert pull == ""
    assert len(core.secrets) == 1
    assert "imagePullSecrets" not in batch.body["spec"]["template"]["spec"]


def test_create_nim_job_rolls_back_secrets_when_job_is_denied():
    client = _client()
    core, batch = _FakeCore(), _FakeBatch(fail=True)
    client._core, client._batch = core, batch
    deleted = []

    async def fake_delete(namespace, name):
        deleted.append(name)

    client.delete_secret = fake_delete  # type: ignore[assignment]
    with pytest.raises(ApiException):
        asyncio.run(client.create_nim_job("ns", IMAGE, "", "job3", KEY, "nim-profiles"))
    assert len(deleted) == 2


# ---------------------------------------------------------------------------
# Queue path
# ---------------------------------------------------------------------------


class _FakeK8s:
    def __init__(self, managed=None):
        self.created = None
        self.deleted = []
        self.managed = managed or []
        self.deleted_jobs = []

    async def create_nim_job(self, namespace, image, profiles, job_id, ngc_api_key, bucket):
        self.created = {
            "namespace": namespace,
            "image": image,
            "profiles": profiles,
            "job_id": job_id,
            "bucket": bucket,
        }
        return "ngc-secret", "pull-secret", "md-nim-job"

    async def wait_for_job(self, namespace, job_name):
        return True, "succeeded"

    async def delete_secret(self, namespace, name):
        self.deleted.append(name)

    async def delete_job(self, namespace, job_name):
        self.deleted_jobs.append(job_name)

    async def list_managed_jobs(self):
        return self.managed


class _ExplodingPreflight:
    async def check(self, **kwargs):
        raise AssertionError("NIM submissions must not run the PVC preflight")


def _queue(k8s):
    return JobQueue(max_concurrency=1, k8s=k8s, pvc_name="models-pvc", nim_bucket="nim-profiles")


async def _settle(queue, job_id, tries=200):
    for _ in range(tries):
        record = queue.get(job_id)
        if record.status != "queued":
            return record
        await asyncio.sleep(0.01)
    raise AssertionError("queue task did not settle")


def test_queue_nim_submit_bypasses_preflight_and_records_override():
    async def run():
        k8s = _FakeK8s()
        queue = _queue(k8s)
        await queue.start()
        record = await queue.submit(
            "ns",
            "",
            "",
            source="nim",
            nim_image=IMAGE,
            nim_profiles="abc",
            ngc_api_key=KEY,
            preflight=_ExplodingPreflight(),
        )
        assert record.storage == "s3"
        assert record.source == "nim"
        assert record.model_name == IMAGE
        assert record.output_url == "s3://nim-profiles"
        assert record.nim_override == "s3://nim-profiles"
        done = await _settle(queue, record.id)
        assert done.status == "succeeded"
        assert k8s.created["image"] == IMAGE
        assert k8s.created["profiles"] == "abc"
        assert k8s.created["bucket"] == "nim-profiles"
        assert sorted(k8s.deleted) == ["ngc-secret", "pull-secret"]

    asyncio.run(run())


def test_reconcile_maps_nim_annotations():
    managed = [
        {
            "id": "j1",
            "namespace": "ns",
            "job_name": "md-nim-job",
            "model_name": IMAGE,
            "storage": "s3",
            "s3_path": "",
            "cache_root": "",
            "source": "nim",
            "nim_image": IMAGE,
            "nim_profiles": "abc",
            "nim_bucket": "nim-profiles",
            "nim_override": "s3://nim-profiles",
            "ngc_secret_name": "ngc-secret",
            "nim_pull_secret_name": "pull-secret",
            "hf_secret_name": "",
            "status": "running",
            "error": "",
            "created_at": 1.0,
            "finished_at": None,
        }
    ]

    async def run():
        queue = _queue(_FakeK8s(managed=managed))
        await queue.reconcile()
        rec = queue.get("j1")
        assert rec.source == "nim"
        assert rec.output_url == "s3://nim-profiles"
        assert rec.pvc_url == ""
        assert rec.ngc_secret_name == "ngc-secret"
        assert rec.nim_pull_secret_name == "pull-secret"

    asyncio.run(run())


def test_watch_cleans_up_reconciled_secrets():
    k8s = _FakeK8s()
    record = JobRecord(
        id="j1",
        namespace="ns",
        model_name=IMAGE,
        status="running",
        k8s_job_name="job",
        source="nim",
        ngc_secret_name="ngc-secret",
        nim_pull_secret_name="pull-secret",
    )

    async def run():
        await _queue(k8s)._watch(record)

    asyncio.run(run())
    assert record.status == "succeeded"
    assert sorted(k8s.deleted) == ["ngc-secret", "pull-secret"]


def test_remove_deletes_nim_secrets():
    k8s = _FakeK8s()
    queue = _queue(k8s)
    record = JobRecord(
        id="j1",
        namespace="ns",
        model_name=IMAGE,
        status="succeeded",
        k8s_job_name="job",
        source="nim",
        ngc_secret_name="ngc-secret",
        nim_pull_secret_name="pull-secret",
    )
    queue.jobs["j1"] = record
    assert asyncio.run(queue.remove("j1")) is None
    assert k8s.deleted_jobs == ["job"]
    assert sorted(k8s.deleted) == ["ngc-secret", "pull-secret"]


def test_real_chart_template_renders_through_app():
    """End-to-end: the chart's actual job-nim.yaml renders through _render_nim.

    This is the seam the two zones meet at: Zone B ships a static YAML doc with
    five placeholders; Zone A must substitute all of them, keep the fixed args,
    and append --profiles. A minimal fake template cannot catch a placeholder
    rename in the chart, so use the real render.
    """
    import subprocess
    from pathlib import Path

    import yaml

    helm = Path(__file__).resolve().parents[1] / "helm"
    proc = subprocess.run(
        ["helm", "template", "mdtest", ".", "-n", "project-user-x"],
        cwd=helm,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    cm = next(d for d in docs if d["kind"] == "ConfigMap")
    assert "job-nim.yaml" in cm["data"]

    client = K8sClient("cm", "ns")
    client._templates = {"nim": cm["data"]["job-nim.yaml"]}
    manifest = client._render_nim("project-user-x", IMAGE, "md-nim-real", "ngc-secret", "mlis-models", profiles="abc")
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == IMAGE
    assert container["args"][:8] == [
        "mirror",
        "s3",
        "-b",
        "mlis-models",
        "--manifest-file",
        "/opt/nim/etc/default/model_manifest.yaml",
        "--model-cache-path",
        "/opt/nim/.cache",
    ]
    assert container["args"][-2:] == ["--profiles", "abc"]
    rendered = yaml.safe_dump(manifest)
    assert "__" not in rendered, "unsubstituted placeholder left in the NIM Job"
    assert manifest["metadata"]["labels"]["model-downloader/source"] == "nim"


def test_reconcile_cleans_finished_job_secrets():
    """A Job that finished while the app was down still owns annotated Secrets."""
    managed = [
        {
            "id": "j-done",
            "namespace": "ns",
            "job_name": "md-nim-done",
            "model_name": IMAGE,
            "storage": "s3",
            "s3_path": "",
            "cache_root": "",
            "source": "nim",
            "nim_image": IMAGE,
            "nim_profiles": "",
            "nim_bucket": "nim-profiles",
            "nim_override": "s3://nim-profiles",
            "ngc_secret_name": "ngc-secret",
            "nim_pull_secret_name": "pull-secret",
            "hf_secret_name": "",
            "status": "succeeded",
            "error": "",
            "created_at": 1.0,
            "finished_at": 2.0,
        }
    ]

    async def run():
        k8s = _FakeK8s(managed=managed)
        queue = _queue(k8s)
        await queue.reconcile()
        assert sorted(k8s.deleted) == ["ngc-secret", "pull-secret"]

    asyncio.run(run())
