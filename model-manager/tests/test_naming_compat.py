"""Identifier deprecation contract (Model Downloader -> Model Manager).

The canonical cluster identifiers are `model-manager/*`; the legacy
`model-downloader/*` names are deprecated but must keep working on read, and
the live-job selectors must match both generations. These tests pin that
contract without a cluster: the read helpers, the selector strings, and the
reconcile path fed a legacy Job object.
"""

import asyncio
from types import SimpleNamespace

from model_manager.app import k8s
from model_manager.app.gc import drop_delete_jobs


def test_managed_by_selectors_match_both_generations():
    assert "model-manager" in k8s.MANAGED_LABEL_SELECTOR
    assert "model-downloader" in k8s.MANAGED_LABEL_SELECTOR
    assert "model-manager-debug" in k8s.DEBUG_LABEL_SELECTOR
    assert "model-downloader-debug" in k8s.DEBUG_LABEL_SELECTOR
    for value in k8s.LIVE_JOB_MANAGED_BY_VALUES:
        assert value in k8s.LIVE_JOB_LABEL_SELECTOR


def test_live_job_values_cover_every_managed_by_generation():
    assert set(k8s.LIVE_JOB_MANAGED_BY_VALUES) == {
        "model-manager",
        "model-downloader",
        "model-manager-debug",
        "model-downloader-debug",
        "model-manager-scan",
        "model-downloader-scan",
        "model-manager-delete",
        "model-downloader-delete",
    }


def test_read_annotation_prefers_canonical_and_falls_back():
    key, legacy = k8s.MODEL_NAME_ANNOTATION, k8s.LEGACY_MODEL_NAME_ANNOTATION
    assert k8s.read_annotation({key: "new", legacy: "old"}, key, legacy) == "new"
    assert k8s.read_annotation({legacy: "old"}, key, legacy) == "old"
    assert k8s.read_annotation({}, key, legacy) == ""
    assert k8s.read_annotation({}, key, legacy, "default") == "default"


def test_read_label_prefers_canonical_and_falls_back():
    key, legacy = k8s.JOB_ID_LABEL, k8s.LEGACY_JOB_ID_LABEL
    assert k8s.read_label({key: "new", legacy: "old"}, key, legacy) == "new"
    assert k8s.read_label({legacy: "old"}, key, legacy) == "old"
    assert k8s.read_label({}, key, legacy, "fallback") == "fallback"


class _FakeBatch:
    def __init__(self, job):
        self._job = job
        self.selector = None

    def list_job_for_all_namespaces(self, label_selector=None):
        self.selector = label_selector
        return SimpleNamespace(items=[self._job])


def _legacy_job():
    meta = SimpleNamespace(
        name="md-legacy-abc123",
        namespace="project-user-x",
        labels={
            "app.kubernetes.io/managed-by": "model-downloader",
            "model-downloader/job-id": "legacy-job-1",
        },
        annotations={
            "model-downloader/model-name": "acme/Legacy-Model",
            "model-downloader/storage": "pvc",
            "model-downloader/cache-root": "/mnt/large-models/acme/legacy",
            "model-downloader/source": "hf",
            "model-downloader/hf-secret": "hf-token-legacy",
        },
        creation_timestamp=SimpleNamespace(timestamp=lambda: 1.0),
    )
    return SimpleNamespace(metadata=meta, status=None)


def test_list_managed_jobs_reconciles_a_legacy_job():
    client = k8s.K8sClient("", "ns")
    batch = _FakeBatch(_legacy_job())
    client._batch = batch

    (row,) = asyncio.run(client.list_managed_jobs())

    assert batch.selector == k8s.MANAGED_LABEL_SELECTOR
    assert row["id"] == "legacy-job-1"
    assert row["model_name"] == "acme/Legacy-Model"
    assert row["cache_root"] == "/mnt/large-models/acme/legacy"
    assert row["source"] == "hf"
    assert row["hf_secret_name"] == "hf-token-legacy"


def test_drop_delete_jobs_accepts_an_explicit_single_value():
    jobs = [
        {"managed_by": "model-manager-delete"},
        {"managed_by": "model-downloader-delete"},
        {"managed_by": "model-manager"},
    ]
    assert drop_delete_jobs(jobs) == [{"managed_by": "model-manager"}]
    assert drop_delete_jobs(jobs, managed_by="model-downloader-delete") == [
        {"managed_by": "model-manager-delete"},
        {"managed_by": "model-manager"},
    ]
