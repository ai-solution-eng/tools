"""Kubernetes client wrapper.

Loads the Job manifest template from a ConfigMap (rendered by the Helm chart),
substitutes per-submission placeholders, then creates an HF-token Secret + the
download Job in the user-specified namespace. Also supports reconciling
previously-created Jobs on startup and reading pod logs. All blocking k8s
calls are pushed to a thread so the FastAPI event loop is not blocked.
"""

import asyncio
import base64
import re
import time
import uuid

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "model-downloader"
JOB_ID_LABEL = "model-downloader/job-id"
MODEL_NAME_ANNOTATION = "model-downloader/model-name"
STORAGE_ANNOTATION = "model-downloader/storage"
S3_PATH_ANNOTATION = "model-downloader/s3-path"
MANAGED_LABEL_SELECTOR = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"


def _sanitize(name: str) -> str:
    """Make a k8s-safe name fragment from a model name like 'org/Repo-Name'."""
    s = name.lower().replace("/", "-")
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "model"


def _parse_s3_path(s3_path: str) -> tuple[str, str]:
    """Split 's3://bucket/prefix/' into (bucket, prefix)."""
    m = re.match(r"^s3://([^/\s]+)(?:/(.*))?$", s3_path.strip().rstrip("/"))
    if not m:
        raise ValueError(f"invalid S3 destination: {s3_path!r}")
    return m.group(1), (m.group(2) or "").strip("/")


class K8sClient:
    def __init__(self, template_cm: str, template_cm_ns: str):
        self.template_cm = template_cm
        self.template_cm_ns = template_cm_ns
        self._template: str | None = None
        self._batch: client.BatchV1Api | None = None
        self._core: client.CoreV1Api | None = None

    @property
    def batch(self) -> client.BatchV1Api:
        if self._batch is None:
            raise RuntimeError("K8sClient not started")
        return self._batch

    @property
    def core(self) -> client.CoreV1Api:
        if self._core is None:
            raise RuntimeError("K8sClient not started")
        return self._core

    async def start(self) -> None:
        try:
            from kubernetes.config import load_incluster_config

            load_incluster_config()
        except Exception:
            from kubernetes.config import load_kube_config

            load_kube_config()
        api_client = client.ApiClient()
        self._batch = client.BatchV1Api(api_client)
        self._core = client.CoreV1Api(api_client)

        cm = await asyncio.to_thread(
            self.core.read_namespaced_config_map,
            name=self.template_cm,
            namespace=self.template_cm_ns,
        )
        if not cm.data or "job.yaml" not in cm.data:
            raise RuntimeError(f"ConfigMap {self.template_cm}/{self.template_cm_ns} missing 'job.yaml' key")
        self._templates = {}
        self._templates["pvc"] = cm.data["job.yaml"]
        self._templates["s3"] = cm.data.get("job-s3.yaml") or None
        self._template = self._templates["pvc"]

    def _render(
        self,
        namespace: str,
        model_name: str,
        job_name: str,
        secret_name: str,
        storage: str = "pvc",
        s3_path: str = "",
        chat_template_path: str = "",
        chat_template_contents: str = "",
    ) -> dict:
        text = self._templates.get(storage)
        if text is None:
            raise RuntimeError(
                f"storage backend '{storage}' requested but template 'job-{storage}.yaml' "
                "is missing from the ConfigMap; check the helm chart version"
            )
        text = text.replace("__NAMESPACE__", namespace)
        text = text.replace("__MODEL_NAME__", model_name)
        text = text.replace("__JOB_NAME__", job_name)
        text = text.replace("__HF_TOKEN_SECRET__", secret_name)
        text = text.replace(
            "__CHAT_TEMPLATE_B64__", base64.b64encode(chat_template_contents.encode("utf-8")).decode("ascii")
        )
        text = text.replace("__CHAT_TEMPLATE_PATH__", chat_template_path)
        norm_s3 = ""
        if storage == "s3":
            bucket, prefix = _parse_s3_path(s3_path)
            text = text.replace("__S3_BUCKET__", bucket)
            text = text.replace("__S3_PREFIX__", prefix)
            norm_s3 = f"s3://{bucket}" + (f"/{prefix}" if prefix else "")
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        if len(docs) != 1:
            raise RuntimeError(f"job template rendered to {len(docs)} docs, expected exactly 1")
        manifest = docs[0]
        meta = manifest.setdefault("metadata", {})
        meta.setdefault("labels", {})[MANAGED_BY_LABEL] = MANAGED_BY_VALUE
        meta.setdefault("annotations", {})[MODEL_NAME_ANNOTATION] = model_name
        meta.setdefault("annotations", {})[STORAGE_ANNOTATION] = storage
        if norm_s3:
            meta.setdefault("annotations", {})[S3_PATH_ANNOTATION] = norm_s3
        return manifest

    async def create_job(
        self,
        namespace: str,
        model_name: str,
        hf_token: str,
        job_id: str,
        storage: str = "pvc",
        s3_path: str = "",
        chat_template_path: str = "",
        chat_template_contents: str = "",
    ) -> tuple[str, str]:
        """Create the HF token Secret + the download Job. Returns (secret_name, job_name)."""
        base = _sanitize(model_name)
        suffix = uuid.uuid4().hex[:6]
        secret_name = f"hf-token-{base}-{suffix}"[:63].rstrip("-")
        job_name = f"md-{base}-{suffix}"[:63].rstrip("-")

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=namespace,
                labels={
                    MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                    JOB_ID_LABEL: job_id,
                },
            ),
            string_data={"HF_TOKEN": hf_token},
            type="Opaque",
        )
        try:
            await asyncio.to_thread(self.core.create_namespaced_secret, namespace=namespace, body=secret)
        except ApiException as e:
            if e.status != 409:
                raise

        manifest = self._render(
            namespace,
            model_name,
            job_name,
            secret_name,
            storage=storage,
            s3_path=s3_path,
            chat_template_path=chat_template_path,
            chat_template_contents=chat_template_contents,
        )
        meta = manifest.setdefault("metadata", {})
        meta.setdefault("labels", {})[JOB_ID_LABEL] = job_id
        meta.setdefault("annotations", {})[MODEL_NAME_ANNOTATION] = model_name
        await asyncio.to_thread(self.batch.create_namespaced_job, namespace=namespace, body=manifest)
        return secret_name, job_name

    @staticmethod
    def _parse_job_status(job) -> tuple[str, str, float | None]:
        """Return (status, error, finished_epoch) from a V1Job."""
        for c in (job.status.conditions if job.status else None) or []:
            if c.status == "True" and c.type in ("Complete", "Failed"):
                finished = None
                if job.status.completion_time:
                    finished = job.status.completion_time.timestamp()
                if c.type == "Complete":
                    return "succeeded", "", finished
                return "failed", f"failed: {c.reason or ''} {c.message or ''}", finished
        return "running", "", None

    async def list_managed_jobs(self) -> list[dict]:
        """List all Jobs we created (cluster-wide) so we can reconcile on startup."""
        jobs = await asyncio.to_thread(
            self.batch.list_job_for_all_namespaces,
            label_selector=MANAGED_LABEL_SELECTOR,
        )
        result = []
        for job in jobs.items:
            labels = job.metadata.labels or {}
            annotations = job.metadata.annotations or {}
            status, error, finished = self._parse_job_status(job)
            created = job.metadata.creation_timestamp.timestamp() if job.metadata.creation_timestamp else time.time()
            result.append(
                {
                    "id": labels.get(JOB_ID_LABEL, job.metadata.name),
                    "namespace": job.metadata.namespace,
                    "job_name": job.metadata.name,
                    "model_name": annotations.get(MODEL_NAME_ANNOTATION, ""),
                    "storage": annotations.get(STORAGE_ANNOTATION, "pvc"),
                    "s3_path": annotations.get(S3_PATH_ANNOTATION, ""),
                    "status": status,
                    "error": error,
                    "created_at": created,
                    "finished_at": finished,
                }
            )
        return result

    async def wait_for_job(
        self, namespace: str, job_name: str, timeout: int = 15000, poll: int = 5
    ) -> tuple[bool, str]:
        """Poll the Job until Complete/Failed or timeout. Returns (success, message)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                job = await asyncio.to_thread(
                    self.batch.read_namespaced_job_status,
                    name=job_name,
                    namespace=namespace,
                )
            except ApiException as e:
                if e.status == 404:
                    return False, "job disappeared"
                raise
            for c in job.status.conditions or []:
                if c.status == "True" and c.type in ("Complete", "Failed"):
                    if c.type == "Complete":
                        return True, "succeeded"
                    return False, f"failed: {c.reason or ''} {c.message or ''}"
            await asyncio.sleep(poll)
        return False, "timeout waiting for job to finish"

    async def get_job_logs(self, namespace: str, job_name: str, container: str = "downloader") -> str:
        """Read logs from the first pod belonging to a Job."""
        pods = await asyncio.to_thread(
            self.core.list_namespaced_pod,
            namespace=namespace,
            label_selector=f"job-name={job_name}",
        )
        if not pods.items:
            return "(no pods found for job; it may have been cleaned up by TTL)"
        pod = pods.items[0]
        # If the pod isn't running yet, say so
        phase = pod.status.phase.lower() if pod.status and pod.status.phase else "unknown"
        if phase not in ("running", "succeeded"):
            return f"(pod is {phase}; logs not available until it starts running)"
        pod_name = pod.metadata.name
        try:
            logs = await asyncio.to_thread(
                self.core.read_namespaced_pod_log,
                name=pod_name,
                namespace=namespace,
                container=container,
            )
        except ApiException as e:
            body = e.body or ""
            if isinstance(body, bytes):
                body = body.decode(errors="replace")
            return f"(error reading logs: {e.reason} {body[:300]})".strip()
        return logs or "(empty log output)"

    async def read_secret(self, namespace: str, name: str) -> dict:
        """Read a Secret's string data (base64-decoded)."""
        import base64

        secret = await asyncio.to_thread(self.core.read_namespaced_secret, name=name, namespace=namespace)
        return {k: base64.b64decode(v).decode(errors="replace") for k, v in (secret.data or {}).items()}

    async def delete_secret(self, namespace: str, secret_name: str) -> None:
        try:
            await asyncio.to_thread(
                self.core.delete_namespaced_secret,
                name=secret_name,
                namespace=namespace,
            )
        except ApiException as e:
            if e.status != 404:
                raise

    async def delete_job(self, namespace: str, job_name: str) -> None:
        try:
            await asyncio.to_thread(
                self.batch.delete_namespaced_job,
                name=job_name,
                namespace=namespace,
                propagation_policy="Background",
            )
        except ApiException as e:
            if e.status != 404:
                raise

    async def check_progress(self, namespace: str, job_name: str) -> dict:
        """Check download progress by reading pod logs."""
        logs = await self.get_job_logs(namespace, job_name)
        return {"logs": logs}
