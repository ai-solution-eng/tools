"""Regression tests for the two bugs that made manual deletion a no-op.

1. `manual-delete-job.yaml` was never mapped in K8sClient.start(), so every
   commit died with "template is missing from the ConfigMap" even though the
   chart shipped it.
2. The delete Job annotated itself with the scan root as its cache-root, and
   live_job_protects() treats that as pinning every directory underneath — so
   the Job hard-blocked its own targets and a finished Job blocked every later
   preview for jobTtlSeconds.
"""

import asyncio

import pytest

from model_manager.app.k8s import (
    CACHE_ROOT_ANNOTATION,
    DELETE_MANAGED_BY_VALUE,
    K8sClient,
    parse_job_templates,
)

MIN_JOB = """
apiVersion: batch/v1
kind: Job
metadata:
  name: __JOB_NAME__
  namespace: __NAMESPACE__
spec:
  ttlSecondsAfterFinished: __JOB_TTL__
  activeDeadlineSeconds: __JOB_DEADLINE__
  template:
    spec:
      serviceAccountName: app-sa
      containers:
      - name: c
        image: __IMAGE__
        env:
        - name: MD_TARGETS_B64
          value: "__TARGETS_B64__"
        - name: MD_ACTOR
          value: "__ACTOR__"
        - name: MD_SCAN_ROOT
          value: "__SCAN_ROOT__"
"""


def cm_data(**over):
    data = {
        "job.yaml": "pvc",
        "job-s3.yaml": "s3",
        "debug-job.yaml": "debug",
        "scan-job.yaml": "scan",
        "manual-delete-job.yaml": MIN_JOB,
        "job-nim.yaml": "nim",
    }
    data.update(over)
    return data


# ---------------------------------------------------------------------------
# bug 1: the template must be mapped
# ---------------------------------------------------------------------------


def test_manual_delete_template_is_mapped():
    templates = parse_job_templates(cm_data())
    assert templates["manual-delete-job"] == MIN_JOB


def test_every_optional_template_is_mapped():
    # The regression was "a caller reads a slot nothing fills". Pin all slots.
    templates = parse_job_templates(cm_data())
    assert set(templates) == {"pvc", "s3", "debug-job", "scan-job", "manual-delete-job", "nim"}


def test_missing_optional_template_maps_to_none():
    templates = parse_job_templates(cm_data(**{"manual-delete-job.yaml": ""}))
    assert templates["manual-delete-job"] is None


def test_missing_nim_template_maps_to_none():
    # A chart older than the NIM feature simply has no job-nim.yaml; the app
    # must map it to None (and refuse at submit time), not crash on start.
    templates = parse_job_templates(cm_data(**{"job-nim.yaml": ""}))
    assert templates["nim"] is None


def test_job_yaml_is_required():
    with pytest.raises(RuntimeError):
        parse_job_templates({"scan-job.yaml": "x"})


# ---------------------------------------------------------------------------
# bug 2: the delete Job must not pin the tree
# ---------------------------------------------------------------------------


class _FakeBatch:
    def __init__(self):
        self.body = None

    def create_namespaced_job(self, namespace, body):
        self.body = body


def _create(**over):
    client = K8sClient("cm", "ns")
    client._templates = {"manual-delete-job": MIN_JOB}
    batch = _FakeBatch()
    client._batch = batch
    kwargs = {
        "namespace": "model-downloader",
        "pvc_name": "models-pvc",
        "scan_root": "/mnt/large-models",
        "image": "ghcr.io/x/y:v1",
        "job_id": "abc123",
        "targets_b64": "W10=",
        "actor": "andrew",
        "job_ttl": 86400,
        "job_deadline": 3600,
    }
    kwargs.update(over)
    name = asyncio.run(client.create_delete_job(**kwargs))
    return name, batch.body


def test_delete_job_has_no_cache_root_annotation():
    _, body = _create()
    annotations = body["metadata"].get("annotations") or {}
    assert CACHE_ROOT_ANNOTATION not in annotations


def test_delete_job_records_the_actor():
    _, body = _create()
    assert body["metadata"]["annotations"]["model-manager/delete-actor"] == "andrew"


def test_delete_job_carries_its_own_managed_by_value():
    name, body = _create()
    assert name == "mm-delete-abc123"
    labels = body["metadata"]["labels"]
    assert labels["app.kubernetes.io/managed-by"] == DELETE_MANAGED_BY_VALUE


def test_placeholders_are_substituted():
    _, body = _create()
    container = body["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["MD_TARGETS_B64"] == "W10="
    assert env["MD_ACTOR"] == "andrew"
    assert env["MD_SCAN_ROOT"] == "/mnt/large-models"
    assert container["image"] == "ghcr.io/x/y:v1"
    assert body["spec"]["ttlSecondsAfterFinished"] == 86400
    assert body["spec"]["activeDeadlineSeconds"] == 3600


def test_missing_template_raises_a_clear_error():
    client = K8sClient("cm", "ns")
    client._templates = {}
    client._batch = _FakeBatch()
    with pytest.raises(RuntimeError, match="manual-delete-job template is missing"):
        asyncio.run(
            client.create_delete_job(
                namespace="ns",
                pvc_name="pvc",
                scan_root="/mnt/x",
                image="i",
                job_id="j",
                targets_b64="W10=",
                actor="a",
                job_ttl=1,
            )
        )


# ---------------------------------------------------------------------------
# bug 3: a client with no template ConfigMap must still start
# ---------------------------------------------------------------------------


class _ExplodingCore:
    def read_namespaced_config_map(self, **kwargs):  # pragma: no cover - asserted
        raise AssertionError("must not read a ConfigMap when template_cm is empty")


def test_start_without_template_name_reads_no_configmap(monkeypatch):
    # The in-pod delete Job builds K8sClient(template_cm="") and calls start();
    # an unconditional ConfigMap read raised and killed the Job before any
    # verdict ran.
    import kubernetes.config as kubeconfig

    import model_manager.app.k8s as k8smod

    monkeypatch.setattr(k8smod.client, "ApiClient", lambda *a, **k: object())
    monkeypatch.setattr(k8smod.client, "BatchV1Api", lambda *a, **k: object())
    monkeypatch.setattr(k8smod.client, "CoreV1Api", lambda *a, **k: _ExplodingCore())
    monkeypatch.setattr(kubeconfig, "load_incluster_config", lambda: None)

    client = k8smod.K8sClient("", "model-downloader")
    asyncio.run(client.start())
    assert client._templates == {}
    assert client.manual_delete_job_available is False
