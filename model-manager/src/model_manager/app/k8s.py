"""Kubernetes client wrapper.

Loads the Job manifest template from a ConfigMap (rendered by the Helm chart),
substitutes per-submission placeholders, then creates an HF-token Secret + the
download Job in the user-specified namespace. Also supports reconciling
previously-created Jobs on startup and reading pod logs. All blocking k8s
calls are pushed to a thread so the FastAPI event loop is not blocked.
"""

import asyncio
import base64
import json
import logging
import re
import time
import uuid

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

log = logging.getLogger(__name__)

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"

# ---------------------------------------------------------------------------
# Identifier naming (Model Downloader -> Model Manager)
# ---------------------------------------------------------------------------
# Everything written from now on uses the Model Manager name. The legacy
# "model-downloader" values are DEPRECATED but still ACCEPTED on read, and the
# live-job selectors match both generations, so Jobs, Secrets and provenance
# manifests written by earlier releases keep reconciling. A later release drops
# the legacy names.
MANAGED_BY_VALUE = "model-manager"
LEGACY_MANAGED_BY_VALUE = "model-downloader"
# Debug shells use their own managed-by value (they are Jobs, not download
# Jobs) so list_managed_jobs() never picks them into the Jobs table.
DEBUG_MANAGED_BY_VALUE = "model-manager-debug"
LEGACY_DEBUG_MANAGED_BY_VALUE = "model-downloader-debug"
# PVC scanner Jobs get the same treatment: a distinct managed-by value so the
# Jobs table (and reconcile) only ever sees real download Jobs.
SCAN_MANAGED_BY_VALUE = "model-manager-scan"
LEGACY_SCAN_MANAGED_BY_VALUE = "model-downloader-scan"
# Manual-delete Jobs are Jobs too, and they MUST be visible to the GC/live-job
# cross-check: while one is running it pins the cache root it is deleting, so a
# concurrent GC run cannot delete underneath it. That means adding this value to
# the live-job selectors, not hiding it like debug/scan are hidden from the
# Jobs table.
DELETE_MANAGED_BY_VALUE = "model-manager-delete"
LEGACY_DELETE_MANAGED_BY_VALUE = "model-downloader-delete"
# Every managed-by value the GC/live-job cross-check must see (old and new).
LIVE_JOB_MANAGED_BY_VALUES = (
    MANAGED_BY_VALUE,
    LEGACY_MANAGED_BY_VALUE,
    DEBUG_MANAGED_BY_VALUE,
    LEGACY_DEBUG_MANAGED_BY_VALUE,
    SCAN_MANAGED_BY_VALUE,
    LEGACY_SCAN_MANAGED_BY_VALUE,
    DELETE_MANAGED_BY_VALUE,
    LEGACY_DELETE_MANAGED_BY_VALUE,
)

JOB_ID_LABEL = "model-manager/job-id"
LEGACY_JOB_ID_LABEL = "model-downloader/job-id"
DEBUG_POD_LABEL = "model-manager/debug-pod"
K8S_JOB_NAME_LABEL = "batch.kubernetes.io/job-name"
K8S_JOB_NAME_LABEL_LEGACY = "job-name"
MODEL_NAME_ANNOTATION = "model-manager/model-name"
LEGACY_MODEL_NAME_ANNOTATION = "model-downloader/model-name"
STORAGE_ANNOTATION = "model-manager/storage"
LEGACY_STORAGE_ANNOTATION = "model-downloader/storage"
S3_PATH_ANNOTATION = "model-manager/s3-path"
LEGACY_S3_PATH_ANNOTATION = "model-downloader/s3-path"
CACHE_ROOT_ANNOTATION = "model-manager/cache-root"
LEGACY_CACHE_ROOT_ANNOTATION = "model-downloader/cache-root"
# Source discriminator: hf (default when absent, for pre-NIM jobs) | nim.
SOURCE_ANNOTATION = "model-manager/source"
LEGACY_SOURCE_ANNOTATION = "model-downloader/source"
NIM_IMAGE_ANNOTATION = "model-manager/nim-image"
LEGACY_NIM_IMAGE_ANNOTATION = "model-downloader/nim-image"
NIM_PROFILES_ANNOTATION = "model-manager/nim-profiles"
LEGACY_NIM_PROFILES_ANNOTATION = "model-downloader/nim-profiles"
NIM_BUCKET_ANNOTATION = "model-manager/nim-bucket"
LEGACY_NIM_BUCKET_ANNOTATION = "model-downloader/nim-bucket"
NIM_OVERRIDE_ANNOTATION = "model-manager/nim-override"
LEGACY_NIM_OVERRIDE_ANNOTATION = "model-downloader/nim-override"
# These three values are Kubernetes annotation NAMES, not credentials; the
# fleet scanner's ASSIGN pass flags the Secret-suffixed identifiers.
NGC_SECRET_ANNOTATION = "model-manager/ngc-secret"  # secret-scan:allow (k8s annotation NAME)
LEGACY_NGC_SECRET_ANNOTATION = "model-downloader/ngc-secret"  # secret-scan:allow (k8s annotation NAME)
NIM_PULL_SECRET_ANNOTATION = "model-manager/nim-pull-secret"  # secret-scan:allow (k8s annotation NAME)
LEGACY_NIM_PULL_SECRET_ANNOTATION = "model-downloader/nim-pull-secret"  # secret-scan:allow (k8s annotation NAME)
HF_SECRET_ANNOTATION = "model-manager/hf-secret"  # secret-scan:allow (k8s annotation NAME)
LEGACY_HF_SECRET_ANNOTATION = "model-downloader/hf-secret"  # secret-scan:allow (k8s annotation NAME)
DEBUG_JOB_NAME_ANNOTATION = "model-manager/debug-job-name"
LEGACY_DEBUG_JOB_NAME_ANNOTATION = "model-downloader/debug-job-name"
SCAN_LABEL = "model-manager/scan"
DELETE_LABEL = "model-manager/delete"
DELETE_ACTOR_ANNOTATION = "model-manager/delete-actor"

# Selectors match BOTH the canonical and the deprecated managed-by values.
MANAGED_LABEL_SELECTOR = f"{MANAGED_BY_LABEL} in ({MANAGED_BY_VALUE},{LEGACY_MANAGED_BY_VALUE})"
DEBUG_LABEL_SELECTOR = f"{MANAGED_BY_LABEL} in ({DEBUG_MANAGED_BY_VALUE},{LEGACY_DEBUG_MANAGED_BY_VALUE})"
LIVE_JOB_LABEL_SELECTOR = f"{MANAGED_BY_LABEL} in ({','.join(LIVE_JOB_MANAGED_BY_VALUES)})"


def read_annotation(annotations: dict, key: str, legacy_key: str, default: str = "") -> str:
    """Read a canonical annotation, falling back to its deprecated legacy key."""
    value = annotations.get(key)
    if value is None:
        value = annotations.get(legacy_key)
    return default if value is None else value


def read_label(labels: dict, key: str, legacy_key: str, default: str = "") -> str:
    """Read a canonical label, falling back to its deprecated legacy key."""
    value = labels.get(key)
    if value is None:
        value = labels.get(legacy_key)
    return default if value is None else value


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


def parse_job_templates(data: dict) -> dict:
    """Map the job-template ConfigMap's data keys to the client's template slots.

    Every optional template must be listed HERE as well as used by its caller:
    adding a feature that reads self._templates["<key>"] without adding the key
    here fails at runtime with "template is missing from the ConfigMap" even
    though the chart shipped it. That is exactly what happened to
    manual-delete-job.yaml, so this mapping is a pure function with a test.
    """
    if not data or "job.yaml" not in data:
        raise RuntimeError("ConfigMap missing 'job.yaml' key")
    return {
        "pvc": data["job.yaml"],
        "s3": data.get("job-s3.yaml") or None,
        # Optional: debug-job only exists in chart >= 1.2.1; the UI hides the
        # section and the API returns a hint when it is absent.
        "debug-job": data.get("debug-job.yaml") or None,
        "scan-job": data.get("scan-job.yaml") or None,
        "manual-delete-job": data.get("manual-delete-job.yaml") or None,
        "nim": data.get("job-nim.yaml") or None,
    }


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

        # A client that only needs the API (the in-pod delete Job reads the
        # AIOLI Secret and lists Jobs) names no template ConfigMap. Reading one
        # unconditionally with an empty name raises, which would have killed the
        # delete Job before it evaluated a single target.
        self._templates = {}
        self._template = None
        self.debug_pod_available = False
        self.scan_job_available = False
        self.manual_delete_job_available = False
        self.nim_job_available = False
        if not self.template_cm:
            return
        cm = await asyncio.to_thread(
            self.core.read_namespaced_config_map,
            name=self.template_cm,
            namespace=self.template_cm_ns,
        )
        if not cm.data or "job.yaml" not in cm.data:
            raise RuntimeError(f"ConfigMap {self.template_cm}/{self.template_cm_ns} missing 'job.yaml' key")
        self._templates = parse_job_templates(cm.data)
        self._template = self._templates["pvc"]
        self.debug_pod_available = self._templates["debug-job"] is not None
        self.scan_job_available = self._templates["scan-job"] is not None
        self.manual_delete_job_available = self._templates["manual-delete-job"] is not None
        self.nim_job_available = self._templates["nim"] is not None

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
        job_id: str = "",
        submitted_by: str = "",
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
        # Provenance manifest inputs (MD-A1). Older charts without the
        # provenance step don't carry the placeholders, so replacing them is
        # harmless there.
        text = text.replace("__JOB_ID__", job_id or job_name)
        text = text.replace("__SUBMITTED_BY__", submitted_by)
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
        # Source discriminator + the token Secret name, so a reconciled job can
        # clean up its own Secret after an app restart (old jobs have neither
        # annotation; they default to hf and leak exactly as they did before).
        meta.setdefault("annotations", {})[SOURCE_ANNOTATION] = "hf"
        meta.setdefault("annotations", {})[HF_SECRET_ANNOTATION] = secret_name
        return manifest

    def _render_nim(
        self,
        namespace: str,
        image: str,
        job_name: str,
        ngc_secret: str,
        bucket: str,
        profiles: str = "",
        job_id: str = "",
    ) -> dict:
        """Render the NIM profile-mirror Job.

        The template ships the fixed mirror-s3 args; profile ids are appended
        here so the template stays a static YAML document.
        """
        text = self._templates.get("nim")
        if not text:
            raise RuntimeError(
                "nim job template is missing from the job-template ConfigMap; "
                "upgrade the chart (helm upgrade) to get NIM profile mirroring"
            )
        text = text.replace("__NAMESPACE__", namespace)
        text = text.replace("__NIM_IMAGE__", image)
        text = text.replace("__JOB_NAME__", job_name)
        text = text.replace("__NGC_SECRET__", ngc_secret)
        text = text.replace("__NIM_BUCKET__", bucket)
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        if len(docs) != 1:
            raise RuntimeError(f"nim job template rendered to {len(docs)} docs, expected exactly 1")
        manifest = docs[0]
        if profiles:
            try:
                args = manifest["spec"]["template"]["spec"]["containers"][0]["args"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError("nim job template has no container args list") from exc
            args += ["--profiles", *profiles.split()]
        meta = manifest.setdefault("metadata", {})
        meta.setdefault("labels", {})[MANAGED_BY_LABEL] = MANAGED_BY_VALUE
        meta.setdefault("labels", {})[SOURCE_ANNOTATION] = "nim"
        annotations = meta.setdefault("annotations", {})
        annotations[MODEL_NAME_ANNOTATION] = image
        annotations[SOURCE_ANNOTATION] = "nim"
        annotations[NIM_IMAGE_ANNOTATION] = image
        if profiles:
            annotations[NIM_PROFILES_ANNOTATION] = profiles
        annotations[NIM_BUCKET_ANNOTATION] = bucket
        annotations[NIM_OVERRIDE_ANNOTATION] = f"s3://{bucket}"
        annotations[NGC_SECRET_ANNOTATION] = ngc_secret
        if job_id:
            meta.setdefault("labels", {})[JOB_ID_LABEL] = job_id
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
        submitted_by: str = "",
    ) -> tuple[str, str]:
        """Create the HF token Secret + the download Job. Returns (secret_name, job_name)."""
        base = _sanitize(model_name)
        suffix = uuid.uuid4().hex[:6]
        secret_name = f"hf-token-{base}-{suffix}"[:63].rstrip("-")
        job_name = f"mm-{base}-{suffix}"[:63].rstrip("-")

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
            job_id=job_id,
            submitted_by=submitted_by,
        )
        meta = manifest.setdefault("metadata", {})
        meta.setdefault("labels", {})[JOB_ID_LABEL] = job_id
        meta.setdefault("annotations", {})[MODEL_NAME_ANNOTATION] = model_name
        await asyncio.to_thread(self.batch.create_namespaced_job, namespace=namespace, body=manifest)
        return secret_name, job_name

    async def create_nim_job(
        self,
        namespace: str,
        image: str,
        profiles: str,
        job_id: str,
        ngc_api_key: str,
        bucket: str,
    ) -> tuple[str, str, str]:
        """Create the NGC key Secret (+ nvcr.io pull Secret) and the mirror Job.

        Returns (ngc_secret_name, pull_secret_name, job_name). pull_secret_name
        is "" when the image is not pulled from nvcr.io; the chart's own
        nim.imagePullSecrets cover private/local registries.
        """
        base = _sanitize(image)[:40].rstrip("-") or "nim"
        suffix = uuid.uuid4().hex[:6]
        ngc_secret = f"ngc-api-{base}-{suffix}"[:63].rstrip("-")
        job_name = f"mm-nim-{base}-{suffix}"[:63].rstrip("-")
        pull_secret = ""
        if image.startswith("nvcr.io/"):
            pull_secret = f"ngc-pull-{base}-{suffix}"[:63].rstrip("-")

        ngc = client.V1Secret(
            metadata=client.V1ObjectMeta(
                name=ngc_secret,
                namespace=namespace,
                labels={MANAGED_BY_LABEL: MANAGED_BY_VALUE, JOB_ID_LABEL: job_id},
            ),
            string_data={"NGC_API_KEY": ngc_api_key},
            type="Opaque",
        )
        try:
            await asyncio.to_thread(self.core.create_namespaced_secret, namespace=namespace, body=ngc)
        except ApiException as e:
            if e.status != 409:
                raise

        if pull_secret:
            auth = base64.b64encode(f"$oauthtoken:{ngc_api_key}".encode()).decode("ascii")
            dockerconfig = json.dumps(
                {"auths": {"nvcr.io": {"username": "$oauthtoken", "password": ngc_api_key, "auth": auth}}}
            )
            pull = client.V1Secret(
                metadata=client.V1ObjectMeta(
                    name=pull_secret,
                    namespace=namespace,
                    labels={MANAGED_BY_LABEL: MANAGED_BY_VALUE, JOB_ID_LABEL: job_id},
                ),
                string_data={".dockerconfigjson": dockerconfig},
                type="kubernetes.io/dockerconfigjson",
            )
            try:
                await asyncio.to_thread(self.core.create_namespaced_secret, namespace=namespace, body=pull)
            except ApiException as e:
                if e.status != 409:
                    await self._delete_secret_quiet(namespace, ngc_secret)
                    raise

        try:
            manifest = self._render_nim(
                namespace, image, job_name, ngc_secret, bucket, profiles=profiles, job_id=job_id
            )
            if pull_secret:
                pod_spec = manifest["spec"]["template"]["spec"]
                pod_spec.setdefault("imagePullSecrets", []).append({"name": pull_secret})
                # Annotate it too: after an app restart the reconciler cleans
                # Secrets from annotations, and a leaked pull Secret carries
                # the NGC key.
                manifest["metadata"].setdefault("annotations", {})[NIM_PULL_SECRET_ANNOTATION] = pull_secret
            await asyncio.to_thread(self.batch.create_namespaced_job, namespace=namespace, body=manifest)
        except Exception:
            # Template render errors and admission denials must not leave the
            # NGC key (or the pull credential) behind.
            await self._delete_secret_quiet(namespace, ngc_secret)
            if pull_secret:
                await self._delete_secret_quiet(namespace, pull_secret)
            raise
        return ngc_secret, pull_secret, job_name

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

    async def create_debug_job(self, namespace: str, hf_token: str = "", image: str = "") -> tuple[str, str]:
        """Create the (optional) HF-token Secret + a long-running debug Job.

        Returns (job_name, secret_name). Deliberately a Job, not a bare Pod:
        the Job's Pod is created by the kube-system job-controller — the same
        admission path as the downloader Jobs — so the platformwide
        protect-models-pvc Kyverno policy admits it. A Pod created directly by
        this ServiceAccount is denied for setting the 'hpe-ezua/app: mlis'
        label (prevent-unauthorized-create-with-mlis).
        """
        suffix = uuid.uuid4().hex[:6]
        job_name = f"mm-debug-{suffix}"
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
            secret_annotations = secret.metadata.annotations or {}
            if (
                read_annotation(secret_annotations, DEBUG_JOB_NAME_ANNOTATION, LEGACY_DEBUG_JOB_NAME_ANNOTATION)
                == job_name
            ):
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
                    "id": read_label(labels, JOB_ID_LABEL, LEGACY_JOB_ID_LABEL, job.metadata.name),
                    "namespace": job.metadata.namespace,
                    "job_name": job.metadata.name,
                    "model_name": read_annotation(annotations, MODEL_NAME_ANNOTATION, LEGACY_MODEL_NAME_ANNOTATION),
                    "storage": read_annotation(annotations, STORAGE_ANNOTATION, LEGACY_STORAGE_ANNOTATION, "pvc"),
                    "s3_path": read_annotation(annotations, S3_PATH_ANNOTATION, LEGACY_S3_PATH_ANNOTATION),
                    "cache_root": read_annotation(annotations, CACHE_ROOT_ANNOTATION, LEGACY_CACHE_ROOT_ANNOTATION),
                    # NIM profile mirrors carry source="nim" plus their own
                    # attribution; pre-NIM jobs have no source annotation and
                    # must keep reconciling as hf.
                    "source": read_annotation(annotations, SOURCE_ANNOTATION, LEGACY_SOURCE_ANNOTATION, "hf"),
                    "nim_image": read_annotation(annotations, NIM_IMAGE_ANNOTATION, LEGACY_NIM_IMAGE_ANNOTATION),
                    "nim_profiles": read_annotation(
                        annotations, NIM_PROFILES_ANNOTATION, LEGACY_NIM_PROFILES_ANNOTATION
                    ),
                    "nim_bucket": read_annotation(annotations, NIM_BUCKET_ANNOTATION, LEGACY_NIM_BUCKET_ANNOTATION),
                    "nim_override": read_annotation(
                        annotations, NIM_OVERRIDE_ANNOTATION, LEGACY_NIM_OVERRIDE_ANNOTATION
                    ),
                    "ngc_secret_name": read_annotation(
                        annotations, NGC_SECRET_ANNOTATION, LEGACY_NGC_SECRET_ANNOTATION
                    ),
                    "nim_pull_secret_name": read_annotation(
                        annotations, NIM_PULL_SECRET_ANNOTATION, LEGACY_NIM_PULL_SECRET_ANNOTATION
                    ),
                    "hf_secret_name": read_annotation(annotations, HF_SECRET_ANNOTATION, LEGACY_HF_SECRET_ANNOTATION),
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

    async def _delete_secret_quiet(self, namespace: str, name: str) -> None:
        """Best-effort Secret deletion for rollback paths (never masks the cause)."""
        try:
            await self.delete_secret(namespace, name)
        except Exception:
            pass

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
        job_name = f"mm-scan-{job_id}"[:63].rstrip("-")
        text = text.replace("__NAMESPACE__", namespace)
        text = text.replace("__JOB_NAME__", job_name)
        text = text.replace("__PVC_NAME__", pvc_name)
        text = text.replace("__MOUNT_PATH__", mount_path)
        text = text.replace("__SCAN_ROOT__", scan_root)
        text = text.replace("__IMAGE__", image)

        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        if len(docs) != 1:
            raise RuntimeError(f"scan-job template rendered to {len(docs)} docs, expected exactly 1")
        manifest = docs[0]
        meta = manifest.setdefault("metadata", {})
        meta.setdefault("labels", {})[MANAGED_BY_LABEL] = SCAN_MANAGED_BY_VALUE
        meta.setdefault("labels", {})[SCAN_LABEL] = "true"

        await asyncio.to_thread(
            self.batch.create_namespaced_job,
            namespace=namespace,
            body=manifest,
        )
        return job_name

    async def create_delete_job(
        self,
        namespace: str,
        pvc_name: str,
        scan_root: str,
        image: str,
        job_id: str,
        targets_b64: str,
        actor: str,
        job_ttl: int,
        job_deadline: int = 3600,
        mount_path: str = "/mnt/",
    ) -> str:
        """Create the Job that performs an admin's manual deletion.

        Returns the job name. The Job runs the app image (it imports the same
        gc.evaluate_manual_targets the preview used) and deletes exactly the
        directories handed to it in targets_b64 -- base64-encoded JSON, because
        template substitution here is a literal str.replace and raw JSON inside
        a double-quoted YAML env value breaks on a quote or a newline.

        Two deliberate differences from create_scan_job:

        * the PVC is mounted READ-WRITE -- this Job exists to remove data;
        * the Job carries a cache-root annotation equal to the scan root, so
          live_job_protects() pins the whole tree while it runs and a
          concurrent GC pass cannot delete underneath it.

        The Job deliberately carries no ServiceAccount and no AIOLI env: it runs
        in the namespace that OWNS the model PVC (where the app's SA does not
        exist), and the AIOLI / live-job protection checks happen in the app at
        commit time instead. That is the same trust model as a download Job.
        """
        text = self._templates.get("manual-delete-job")
        if not text:
            raise RuntimeError(
                "manual-delete-job template is missing from the job-template "
                "ConfigMap; upgrade the chart (helm upgrade) to enable manual deletion"
            )
        job_name = f"mm-delete-{job_id}"[:63].rstrip("-")
        subs = {
            "__NAMESPACE__": namespace,
            "__JOB_NAME__": job_name,
            "__PVC_NAME__": pvc_name,
            "__MOUNT_PATH__": mount_path,
            "__SCAN_ROOT__": scan_root,
            "__IMAGE__": image,
            "__TARGETS_B64__": targets_b64,
            "__ACTOR__": actor,
            "__JOB_TTL__": str(int(job_ttl)),
            "__JOB_DEADLINE__": str(int(job_deadline)),
        }
        for placeholder, value in subs.items():
            text = text.replace(placeholder, value)

        docs = [d for d in yaml.safe_load_all(text) if d is not None]
        if len(docs) != 1:
            raise RuntimeError(f"manual-delete-job template rendered to {len(docs)} docs, expected exactly 1")
        manifest = docs[0]
        meta = manifest.setdefault("metadata", {})
        meta.setdefault("labels", {})[MANAGED_BY_LABEL] = DELETE_MANAGED_BY_VALUE
        meta.setdefault("labels", {})[DELETE_LABEL] = "true"
        annotations = meta.setdefault("annotations", {})
        # NO cache-root annotation, deliberately. live_job_protects() treats any
        # managed job whose cache-root is a prefix of an entry's parent as
        # pinning it — so annotating this Job with the scan root made it protect
        # EVERY target from ITSELF (nothing could ever be deleted) and kept every
        # later preview blocked for the whole jobTtlSeconds (a day). The Job
        # needs no tree pin: it re-validates each path in-pod, and a concurrent
        # GC removing a directory first just fails structural validation and
        # aborts the run, which is safe.
        annotations[DELETE_ACTOR_ANNOTATION] = actor

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
                log.warning(
                    "scan %s: pod %s phase %s; logs not readable", job_name, pod.metadata.name, pod.status.phase
                )
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

    # ---- Namespace dropdown search (front page) ----

    async def list_namespaces(self) -> list[str]:
        """List every cluster namespace name.

        Callers apply the namespace-prefix filter.  An RBAC denial propagates
        to the API layer, which surfaces it as a clean 403 detail.
        """
        ns = await asyncio.to_thread(self.core.list_namespace)
        return [item.metadata.name for item in ns.items]

    # ---- PVC read (MD-B preflight) ----

    async def read_pvc_capacity(self, namespace: str, pvc_name: str) -> int | None:
        """A PVC's status.capacity in bytes (None when unknown/unreadable).

        Read-only `persistentvolumeclaims` get on the ClusterRole (MD-B).
        The app pod cannot mount the models PVC, so capacity comes from the
        API while used-bytes come from the scanner's du pass (preflight.py).
        """
        from .preflight import parse_k8s_quantity

        pvc = await asyncio.to_thread(
            self.core.read_namespaced_persistent_volume_claim,
            name=pvc_name,
            namespace=namespace,
        )
        capacity = (pvc.status.capacity or {}).get("storage") if pvc.status else None
        return parse_k8s_quantity(capacity)

    # ---- GC live-job cross-check (MD-C) ----

    async def list_managed_jobs_by_annotation(self) -> list[dict]:
        """Every live managed Job (all managed-by values) as an attribution row.

        The GC three-key rule cross-checks live Jobs because ttlSecondsAfterFinished
        erases finished-job evidence: a cache dir with no Job *right now* may
        still serve. Includes scan/debug managed-by values on purpose — a
        running scanner or debug shell on a cache root is evidence of use.
        """
        jobs = await asyncio.to_thread(
            self.batch.list_job_for_all_namespaces,
            label_selector=LIVE_JOB_LABEL_SELECTOR,
        )
        result = []
        for job in jobs.items:
            meta = job.metadata
            annotations = meta.annotations or {}
            labels = meta.labels or {}
            status, _error, finished = self._parse_job_status(job)
            result.append(
                {
                    "namespace": meta.namespace,
                    "job_name": meta.name,
                    "model_name": read_annotation(annotations, MODEL_NAME_ANNOTATION, LEGACY_MODEL_NAME_ANNOTATION),
                    "storage": read_annotation(annotations, STORAGE_ANNOTATION, LEGACY_STORAGE_ANNOTATION, "pvc"),
                    "cache_root": read_annotation(annotations, CACHE_ROOT_ANNOTATION, LEGACY_CACHE_ROOT_ANNOTATION),
                    "managed_by": labels.get(MANAGED_BY_LABEL, ""),
                    "status": status,
                    "created_at": meta.creation_timestamp.timestamp() if meta.creation_timestamp else None,
                    "finished_at": finished,
                }
            )
        return result
