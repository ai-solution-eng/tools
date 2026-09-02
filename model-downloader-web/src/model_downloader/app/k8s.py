"""Kubernetes client wrapper.

Loads the Job manifest template from a ConfigMap (rendered by the Helm chart),
substitutes per-submission placeholders, then creates an HF-token Secret + the
download Job in the user-specified namespace. Also supports reconciling
previously-created Jobs on startup and reading pod logs. All blocking k8s
calls are pushed to a thread so the FastAPI event loop is not blocked.
"""

import asyncio
import base64
import logging
import re
import time
import uuid

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

log = logging.getLogger(__name__)

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "model-downloader"
# Debug shells use their own managed-by value (they are Jobs, not download
# Jobs) so list_managed_jobs() never picks them into the Jobs table.
DEBUG_MANAGED_BY_VALUE = "model-downloader-debug"
# PVC scanner Jobs get the same treatment: a distinct managed-by value so the
# Jobs table (and reconcile) only ever sees real download Jobs.
SCAN_MANAGED_BY_VALUE = "model-downloader-scan"
JOB_ID_LABEL = "model-downloader/job-id"
DEBUG_POD_LABEL = "model-downloader/debug-pod"
K8S_JOB_NAME_LABEL = "batch.kubernetes.io/job-name"
K8S_JOB_NAME_LABEL_LEGACY = "job-name"
MODEL_NAME_ANNOTATION = "model-downloader/model-name"
STORAGE_ANNOTATION = "model-downloader/storage"
S3_PATH_ANNOTATION = "model-downloader/s3-path"
CACHE_ROOT_ANNOTATION = "model-downloader/cache-root"
DEBUG_JOB_NAME_ANNOTATION = "model-downloader/debug-job-name"
MANAGED_LABEL_SELECTOR = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
DEBUG_LABEL_SELECTOR = f"{MANAGED_BY_LABEL}={DEBUG_MANAGED_BY_VALUE}"


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
    def __init__(self, template_cm: str, template_cm_ns: str, default_debug_image: str = ""):
        self.template_cm = template_cm
        self.template_cm_ns = template_cm_ns
        self.default_debug_image = default_debug_image
        self.debug_pod_available = False
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
        # Optional: the debug-job template only exists in chart >= 1.2.1. When
        # missing, the UI hides the debug section and the API returns a hint.
        self._templates["debug-job"] = cm.data.get("debug-job.yaml") or None
        self.debug_pod_available = self._templates["debug-job"] is not None
        # Optional: scan-job template for listing models on PVC.
        self._templates["scan-job"] = cm.data.get("scan-job.yaml") or None
        self.scan_job_available = self._templates["scan-job"] is not None

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
        cache_root: str = "",
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
        # Resolve the cache root here so the job template stays simple. When the
        # user leaves it blank we default to /mnt/large-models/<model>.
        resolved_cache_root = cache_root or f"/mnt/large-models/{model_name}"
        text = text.replace("__CACHE_ROOT__", resolved_cache_root)
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
        # Always record the resolved PVC destination (default or custom) so the
        # exact cache root a job used is inspectable, even when the user left
        # the custom field blank. S3 jobs stream straight to the bucket and
        # ignore CACHE_ROOT, so they keep the old optional-annotation behavior.
        if storage == "pvc":
            meta.setdefault("annotations", {})[CACHE_ROOT_ANNOTATION] = resolved_cache_root
        elif cache_root:
            meta.setdefault("annotations", {})[CACHE_ROOT_ANNOTATION] = cache_root
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
        cache_root: str = "",
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
            cache_root=cache_root,
        )
        meta = manifest.setdefault("metadata", {})
        meta.setdefault("labels", {})[JOB_ID_LABEL] = job_id
        meta.setdefault("annotations", {})[MODEL_NAME_ANNOTATION] = model_name
        await asyncio.to_thread(self.batch.create_namespaced_job, namespace=namespace, body=manifest)
        return secret_name, job_name

    # ---- Debug shell (UI "Launch debug pod") — implemented as a Job ----

    def _render_debug_job(self, namespace: str, job_name: str, secret_name: str, image: str) -> dict:
        text = self._templates.get("debug-job")
        if not text:
            raise RuntimeError(
                "debug-job template is missing from the job-template ConfigMap; "
                "upgrade the chart (helm upgrade) to get the debug pod feature"
            )
        resolved_image = image or self.default_debug_image
        if not resolved_image:
            raise RuntimeError("no debug pod image configured (set debugPod.image in values)")
        text = text.replace("__NAMESPACE__", namespace)
        text = text.replace("__JOB_NAME__", job_name)
        text = text.replace("__HF_TOKEN_SECRET__", secret_name)
        text = text.replace("__IMAGE__", resolved_image)
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        if len(docs) != 1:
            raise RuntimeError(f"debug-job template rendered to {len(docs)} docs, expected exactly 1")
        return docs[0]

    async def create_debug_job(
        self, namespace: str, hf_token: str = "", image: str = ""
    ) -> tuple[str, str]:
        """Create the (optional) HF-token Secret + a long-running debug Job.

        Returns (job_name, secret_name). Deliberately a Job, not a bare Pod:
        the Job's Pod is created by the kube-system job-controller — the same
        admission path as the downloader Jobs — so the platformwide
        protect-models-pvc Kyverno policy admits it. A Pod created directly by
        this ServiceAccount is denied for setting the 'hpe-ezua/app: mlis'
        label (prevent-unauthorized-create-with-mlis).
        """
        suffix = uuid.uuid4().hex[:6]
        job_name = f"md-debug-{suffix}"
        secret_name = f"hf-token-debug-{suffix}"

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=secret_name,
                namespace=namespace,
                labels={
                    MANAGED_BY_LABEL: DEBUG_MANAGED_BY_VALUE,
                    DEBUG_POD_LABEL: "true",
                },
                annotations={DEBUG_JOB_NAME_ANNOTATION: job_name},
            ),
            # An empty token keeps the env var present but harmless; the debug
            # shell is mainly for inspecting the PVC.
            string_data={"HF_TOKEN": hf_token or ""},
            type="Opaque",
        )
        try:
            await asyncio.to_thread(self.core.create_namespaced_secret, namespace=namespace, body=secret)
        except ApiException as e:
            if e.status != 409:
                raise

        manifest = self._render_debug_job(namespace, job_name, secret_name, image)
        try:
            await asyncio.to_thread(self.batch.create_namespaced_job, namespace=namespace, body=manifest)
        except ApiException:
            # Job admission failed (e.g. kyverno) — don't leave the token Secret behind.
            try:
                await self.delete_secret(namespace, secret_name)
            except Exception:
                pass
            raise
        return job_name, secret_name

    async def list_debug_pods(self) -> list[dict]:
        """List debug-shell pods we created (cluster-wide) so the UI can show them."""
        pods = await asyncio.to_thread(
            self.core.list_pod_for_all_namespaces,
            label_selector=DEBUG_LABEL_SELECTOR,
        )
        result = []
        for pod in pods.items:
            meta = pod.metadata
            status = pod.status
            labels = meta.labels or {}
            image = ""
            if pod.spec and pod.spec.containers:
                image = pod.spec.containers[0].image or ""
            created = meta.creation_timestamp.timestamp() if meta.creation_timestamp else None
            result.append(
                {
                    "name": meta.name,
                    "namespace": meta.namespace,
                    "job_name": labels.get(K8S_JOB_NAME_LABEL) or labels.get(K8S_JOB_NAME_LABEL_LEGACY) or "",
                    "phase": (status.phase if status else "") or "unknown",
                    "node": (pod.spec.node_name if pod.spec else "") or "",
                    "image": image,
                    "created_at": created,
                }
            )
        return result

    async def delete_debug_job(self, namespace: str, job_name: str) -> None:
        """Delete a debug Job (its Pod goes with it) and its HF-token Secret.

        404s are ignored so deleting an already-cleaned-up job is a no-op.
        """
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
        secrets = await asyncio.to_thread(
            self.core.list_namespaced_secret,
            namespace=namespace,
            label_selector=DEBUG_LABEL_SELECTOR,
        )
        for secret in secrets.items:
            if (secret.metadata.annotations or {}).get(DEBUG_JOB_NAME_ANNOTATION) == job_name:
                await self.delete_secret(namespace, secret.metadata.name)

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
                    "cache_root": annotations.get(CACHE_ROOT_ANNOTATION, ""),
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

    @staticmethod
    def _decode_log(resp) -> str:
        """Decode a pod-log response across kubernetes-client versions.

        Older clients (<=31) preload the body into ``str``; newer ones (32+)
        hand back raw ``bytes`` (which ``str()`` would turn into a ``b'...'``
        repr — the bug that made a perfectly good scan parse as zero models).
        ``_preload_content=False`` + explicit decode is deterministic on all
        versions.
        """
        data = getattr(resp, "data", resp)
        if isinstance(data, (bytes, bytearray)):
            return bytes(data).decode("utf-8", "replace")
        return data if isinstance(data, str) else str(data)

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
            resp = await asyncio.to_thread(
                self.core.read_namespaced_pod_log,
                name=pod_name,
                namespace=namespace,
                container=container,
                _preload_content=False,
            )
            logs = self._decode_log(resp)
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

    # ---- PVC scan job (short-lived, read-only) ----

    async def create_scan_job(
        self,
        namespace: str,
        pvc_name: str,
        scan_root: str,
        image: str,
        job_id: str,
        mount_path: str = "/mnt/",
    ) -> str:
        """Create a short-lived Job that scans the PVC for model directories.

        Returns the job name.  The job is designed to finish quickly (a few
        seconds) and is cleaned up by the caller after reading its logs.
        """
        text = self._templates.get("scan-job")
        if not text:
            raise RuntimeError(
                "scan-job template is missing from the job-template ConfigMap; "
                "upgrade the chart (helm upgrade) to enable PVC scanning"
            )
        job_name = f"md-scan-{job_id}"[:63].rstrip("-")
        text = text.replace("__NAMESPACE__", namespace)
        text = text.replace("__JOB_NAME__", job_name)
        text = text.replace("__PVC_NAME__", pvc_name)
        text = text.replace("__MOUNT_PATH__", mount_path)
        text = text.replace("__SCAN_ROOT__", scan_root)
        text = text.replace("__IMAGE__", image)

        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        if len(docs) != 1:
            raise RuntimeError(
                f"scan-job template rendered to {len(docs)} docs, expected exactly 1"
            )
        manifest = docs[0]
        meta = manifest.setdefault("metadata", {})
        meta.setdefault("labels", {})[MANAGED_BY_LABEL] = SCAN_MANAGED_BY_VALUE
        meta.setdefault("labels", {})["model-downloader/scan"] = "true"

        await asyncio.to_thread(
            self.batch.create_namespaced_job,
            namespace=namespace,
            body=manifest,
        )
        return job_name

    async def get_scan_results(
        self, namespace: str, job_name: str, timeout: int = 30, poll: int = 2
    ) -> tuple[str, str]:
        """Wait for a scanner Job to finish and return (status, stdout).

        Status is "complete", "failed", or "timeout" so callers can tell the
        user why a scan produced nothing instead of returning a silent empty.
        """
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
                    return "failed", "scan job disappeared before completing"
                raise
            for c in job.status.conditions or []:
                if c.status == "True":
                    if c.type == "Complete":
                        return "complete", await self._read_scan_logs(namespace, job_name)
                    if c.type == "Failed":
                        return "failed", await self._read_scan_logs(namespace, job_name)
            await asyncio.sleep(poll)
        return "timeout", ""

    async def _read_scan_logs(self, namespace: str, job_name: str) -> str:
        """Read stdout from the scanner pod.

        Every branch is logged — a scan that yields nothing must be
        distinguishable (no pod found vs wrong phase vs read error) instead of
        silently returning an empty string.
        """
        try:
            pods = await asyncio.to_thread(
                self.core.list_namespaced_pod,
                namespace=namespace,
                label_selector=f"job-name={job_name}",
            )
            if not pods.items:
                log.warning("scan %s: no pod found with label job-name=%s", job_name, job_name)
                return ""
            pod = pods.items[0]
            if pod.status.phase not in ("Running", "Succeeded"):
                log.warning("scan %s: pod %s phase %s; logs not readable", job_name, pod.metadata.name, pod.status.phase)
                return ""
            logs = await asyncio.to_thread(
                self.core.read_namespaced_pod_log,
                name=pod.metadata.name,
                namespace=namespace,
                container="scanner",
                _preload_content=False,
            )
            logs = self._decode_log(logs)
            log.info("scan %s: read %d bytes from pod %s", job_name, len(logs), pod.metadata.name)
            return logs or ""
        except Exception as e:
            log.warning("scan %s: log read failed: %s", job_name, e)
            return ""
