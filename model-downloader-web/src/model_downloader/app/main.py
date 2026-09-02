"""FastAPI service that serves the HF Model Downloader UI and Job API."""

import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, field_validator, model_validator

from .catalog import TIER_LABELS, TIERS, Catalog
from .db import AioliDB
from .k8s import K8sClient
from .queue import JobQueue
from .storage import DownloadedModelsCache

APP_NAMESPACE = os.environ.get("POD_NAMESPACE", "default")
DEFAULT_NAMESPACE = os.environ.get("DEFAULT_NAMESPACE", "project-user-andrew-bydlon")
JOB_TEMPLATE_CM = os.environ["JOB_TEMPLATE_CONFIGMAP"]
JOB_TEMPLATE_CM_NS = os.environ.get("JOB_TEMPLATE_CONFIGMAP_NAMESPACE", APP_NAMESPACE)
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "4"))
PVC_NAME = os.environ.get("PVC_NAME", "models-pvc")
CONTAINER_PATH = os.environ.get("CONTAINER_PATH", "/mnt/models")
PVC_SUBPATH = os.environ.get("PVC_SUBPATH", "large-models")
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "both")  # pvc | s3 | both
STORAGE_DEFAULT = os.environ.get("STORAGE_DEFAULT", "pvc")  # pvc | s3
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", "")
S3_DEFAULT_PATH = f"s3://{S3_BUCKET}/" + (f"{S3_PREFIX}/" if S3_PREFIX else "")
# Debug pod ("Launch debug pod" in the UI). Requires chart >= 1.2.0, which
# renders the debug-pod.yaml template into the job-template ConfigMap.
DEBUG_POD_ENABLED = os.environ.get("DEBUG_POD_ENABLED", "true").strip().lower() == "true"
DEBUG_POD_IMAGE = os.environ.get("DEBUG_POD_IMAGE", "")

# Downloaded-models listing configuration
DOWNLOAD_LIST_ENABLED = os.environ.get("DOWNLOAD_LIST_ENABLED", "true").strip().lower() == "true"
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY", "")
PVC_SCAN_IMAGE = os.environ.get("PVC_SCAN_IMAGE", DEBUG_POD_IMAGE)
PVC_SCAN_ENABLED = os.environ.get("PVC_SCAN_ENABLED", "false").strip().lower() == "true"
PVC_REFRESH_INTERVAL = int(os.environ.get("PVC_REFRESH_INTERVAL", "60"))

CATALOG_PATH = os.environ.get("CATALOG_PATH", "/mnt/catalog/catalog.json")
AIOLI_DB_HOST = os.environ.get("AIOLI_DB_HOST", "aioli-db-service-hpe-mlis.mlis.svc.cluster.local")
AIOLI_DB_PORT = int(os.environ.get("AIOLI_DB_PORT", "5432"))
AIOLI_DB_NAME = os.environ.get("AIOLI_DB_NAME", "aioli")
AIOLI_DB_USER = os.environ.get("AIOLI_DB_USER", "postgres")
AIOLI_DB_SECRET_NAME = os.environ.get("AIOLI_DB_SECRET_NAME", "aioli-db-password")
AIOLI_DB_SECRET_NS = os.environ.get("AIOLI_DB_SECRET_NS", "mlis")
AIOLI_DB_SECRET_KEY = os.environ.get("AIOLI_DB_SECRET_KEY", "password")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Content-hash every static asset once at startup so cache-busting query strings
# change whenever a file changes. The old templates hardcoded "?v=0.7.2", which
# never changed across releases, so browsers kept running stale JS (e.g. the one
# that predates the custom download-location field) after an upgrade — silently
# dropping cache_root from submissions. A per-file hash guarantees the browser
# refetches the asset on the next deploy.
ASSET_NAMES = ("app.js", "style.css", "catalog.js", "favicon.jpg")


def _static_hashes() -> dict[str, str]:
    hashes = {}
    for name in ASSET_NAMES:
        path = BASE_DIR / "static" / name
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:10] if path.is_file() else "0"
    return hashes


STATIC_HASHES = _static_hashes()

k8s_client = K8sClient(
    template_cm=JOB_TEMPLATE_CM,
    template_cm_ns=JOB_TEMPLATE_CM_NS,
    default_debug_image=DEBUG_POD_IMAGE,
)
queue = JobQueue(
    max_concurrency=MAX_CONCURRENCY,
    k8s=k8s_client,
    pvc_name=PVC_NAME,
    container_path=CONTAINER_PATH,
    pvc_subpath=PVC_SUBPATH,
    s3_bucket=S3_BUCKET,
    s3_prefix=S3_PREFIX,
)
catalog = Catalog(CATALOG_PATH)
aioli_db = AioliDB(
    k8s=k8s_client,
    host=AIOLI_DB_HOST,
    port=AIOLI_DB_PORT,
    dbname=AIOLI_DB_NAME,
    user=AIOLI_DB_USER,
    secret_name=AIOLI_DB_SECRET_NAME,
    secret_ns=AIOLI_DB_SECRET_NS,
    secret_key=AIOLI_DB_SECRET_KEY,
)
downloaded_cache = DownloadedModelsCache(pvc_refresh_interval=PVC_REFRESH_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await k8s_client.start()
    await queue.start()
    await queue.reconcile()
    yield


app = FastAPI(title="HF Model Downloader", lifespan=lifespan)


class SubmitRequest(BaseModel):
    namespace: str
    model_name: str
    hf_token: str
    storage: str = "pvc"
    s3_path: str = ""
    chat_template_path: str = ""
    chat_template_contents: str = ""
    cache_root: str = ""

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", v):
            raise ValueError("invalid namespace")
        return v

    @field_validator("model_name")
    @classmethod
    def _valid_model(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$", v):
            raise ValueError("model_name must be 'org/Repo-Name'")
        return v

    @field_validator("storage")
    @classmethod
    def _valid_storage(cls, v: str) -> str:
        if v not in ("pvc", "s3"):
            raise ValueError("storage must be 'pvc' or 's3'")
        return v

    @field_validator("s3_path")
    @classmethod
    def _valid_s3_path(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^s3://[^/\s]+", v):
            raise ValueError("s3_path must start with 's3://<bucket>'")
        return v

    @field_validator("cache_root")
    @classmethod
    def _valid_cache_root(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v:
            return ""
        if not v.startswith("/") or ".." in v.split("/"):
            raise ValueError("cache_root must be an absolute path and must not contain '..'")
        return v

    @model_validator(mode="after")
    def _check_s3_path(self):
        if self.storage == "s3" and not self.s3_path:
            raise ValueError("s3_path is required when storage is 's3'")
        return self

    @model_validator(mode="after")
    def _check_chat_template(self):
        if bool(self.chat_template_path) != bool(self.chat_template_contents):
            raise ValueError("chat template path and contents must be provided together")
        return self


class DebugPodRequest(BaseModel):
    namespace: str
    hf_token: str = ""
    image: str = ""  # empty => chart default (debugPod.image)

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", v):
            raise ValueError("invalid namespace")
        return v

    @field_validator("image")
    @classmethod
    def _valid_image(cls, v: str) -> str:
        v = v.strip()
        if v and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-/:@]*$", v):
            raise ValueError("image must look like [registry/]repo[:tag|@digest]")
        return v


def _storage_allowed(backend: str) -> list[str]:
    if backend == "s3":
        return ["s3"]
    if backend == "pvc":
        return ["pvc"]
    return ["pvc", "s3"]


def _storage_default() -> str:
    default = STORAGE_DEFAULT
    allowed = _storage_allowed(STORAGE_BACKEND)
    return default if default in allowed else allowed[0]


def _page_context() -> dict:
    return {
        "max_concurrency": MAX_CONCURRENCY,
        "default_namespace": DEFAULT_NAMESPACE,
        "app_namespace": APP_NAMESPACE,
        "tier_labels": TIER_LABELS,
        "tiers": TIERS,
        "storage_backend": STORAGE_BACKEND,
        "storage_default": _storage_default(),
        "storage_options": _storage_allowed(STORAGE_BACKEND),
        "s3_bucket": S3_BUCKET,
        "s3_prefix": S3_PREFIX,
        "s3_default_path": S3_DEFAULT_PATH,
        # Job history always populates the table, so the section only depends
        # on the master switch; PVC/S3 scans enrich it when enabled/configured.
        "download_list_enabled": DOWNLOAD_LIST_ENABLED,
        "pvc_scan_enabled": PVC_SCAN_ENABLED,
        "debug_pods_enabled": DEBUG_POD_ENABLED and k8s_client.debug_pod_available,
        "debug_pod_image": DEBUG_POD_IMAGE,
        "assets": STATIC_HASHES,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context=_page_context())


@app.get("/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request):
    return templates.TemplateResponse(request=request, name="catalog.html", context=_page_context())


@app.post("/api/jobs")
async def submit_job(req: SubmitRequest):
    record = await queue.submit(
        req.namespace,
        req.model_name,
        req.hf_token,
        storage=req.storage,
        s3_path=req.s3_path,
        chat_template_path=req.chat_template_path,
        chat_template_contents=req.chat_template_contents,
        cache_root=req.cache_root,
    )
    return {"id": record.id, "status": record.status, "storage": record.storage}


@app.get("/api/jobs")
async def list_jobs():
    return [r.to_dict() for r in queue.list_jobs()]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    r = queue.get(job_id)
    if not r:
        raise HTTPException(status_code=404, detail="job not found")
    return r.to_dict()


@app.get("/api/jobs/{job_id}/logs")
async def get_job_logs(job_id: str):
    r = queue.get(job_id)
    if not r:
        raise HTTPException(status_code=404, detail="job not found")
    logs = await queue.get_logs(job_id)
    return {"logs": logs}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    err = await queue.remove(job_id)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/progress")
async def get_job_progress(job_id: str):
    r = queue.get(job_id)
    if not r:
        raise HTTPException(status_code=404, detail="job not found")
    return await queue.get_progress(job_id)


@app.get("/api/healthz")
async def healthz():
    return {"ok": True, "max_concurrency": MAX_CONCURRENCY}


# ---- Downloaded models listing ----


@app.get("/api/downloaded")
async def list_downloaded(request: Request):
    """Return a deduplicated list of models on storage.

    Job history is always included (in-memory). The S3 listing and the PVC
    scanner Job are cached server-side: automatic polls refresh them only
    after their interval — and the PVC scan only when PVC_SCAN_ENABLED is
    set — while ``?force=1`` (the "Rescan storage" button) refreshes both on
    demand. With the PVC scan disabled this endpoint makes no k8s calls at
    all; with no bucket configured it makes no S3 calls either.
    """
    force = (request.query_params.get("force") or "") in ("1", "true", "yes")
    if not DOWNLOAD_LIST_ENABLED:
        return {"models": []}

    # The scanner Job mounts the PVC at /mnt/ and scans the subpath where
    # downloader Jobs write (matches the __CACHE_ROOT__ default in job.yaml).
    # Automatic PVC scanning is opt-in (PVC_SCAN_ENABLED); an explicit
    # ?force=1 ("Rescan storage") scans on demand regardless — the cache
    # applies its debounce so this stays one scanner Job at a time.
    scan_root = f"/mnt/{PVC_SUBPATH}"
    models = await downloaded_cache.get_models(
        queue.list_jobs(),
        pvc_enabled=PVC_SCAN_ENABLED,
        pvc_namespace=DEFAULT_NAMESPACE,
        pvc_name=PVC_NAME,
        pvc_scan_root=scan_root,
        pvc_image=PVC_SCAN_IMAGE,
        pvc_mount_path="/mnt/",
        k8s=k8s_client,
        s3_bucket=S3_BUCKET,
        s3_prefix=S3_PREFIX,
        s3_endpoint=S3_ENDPOINT_URL,
        s3_access_key=S3_ACCESS_KEY_ID,
        s3_secret_key=S3_SECRET_ACCESS_KEY,
        force=force,
    )
    return {
        "models": [m.to_dict() for m in models],
        # Why each source contributed what it did — so an empty table is
        # explainable (scan ran and found 0 vs skipped vs error).
        "scan": dict(downloaded_cache.last_status),
    }


# ---- Debug pod ----


def _require_debug_pods() -> None:
    if not DEBUG_POD_ENABLED:
        raise HTTPException(400, "debug pods are disabled (debugPod.enabled=false)")
    if not k8s_client.debug_pod_available:
        raise HTTPException(
            400,
            "debug-job template missing from the job-template ConfigMap; "
            "upgrade the chart (helm upgrade) to enable debug pods",
        )


def _api_error_detail(e: ApiException) -> str:
    """Human-readable message from a k8s API error (RBAC 403, kyverno denials...).

    The apiserver puts the admission/RBAC explanation in the Status body's
    'message' field — that is what the user needs to see in the UI.
    """
    status = e.status or 500
    detail = e.reason or "kubernetes API error"
    body = e.body
    if isinstance(body, bytes | bytearray):
        body = body.decode(errors="replace")
    if body:
        try:
            msg = json.loads(body).get("message")
            if msg:
                detail = str(msg)
        except (ValueError, AttributeError):
            if isinstance(body, str) and body.strip():
                detail = body.strip()[:500]
    return f"k8s API returned {status}: {detail}"


@app.post("/api/debug-pods")
async def launch_debug_pod(req: DebugPodRequest):
    _require_debug_pods()
    try:
        job_name, _secret_name = await k8s_client.create_debug_job(
            req.namespace,
            hf_token=req.hf_token.strip(),
            image=req.image.strip(),
        )
    except ApiException as e:
        # Job creation denied (RBAC / kyverno admission) — surface the real
        # reason instead of an opaque 500.
        raise HTTPException(e.status or 500, _api_error_detail(e))
    # The pod belongs to the job-controller and gets a generated name, so the
    # exec hint resolves it via the job-name label.
    pod_selector = f"-l job-name={job_name}"
    return {
        "name": job_name,
        "namespace": req.namespace,
        "kubectl": (
            f"kubectl exec -it $(kubectl get pods -n {req.namespace} {pod_selector} "
            f"-o jsonpath='{{.items[0].metadata.name}}') -- bash"
        ),
    }


@app.get("/api/debug-pods")
async def list_debug_pods():
    _require_debug_pods()
    return await k8s_client.list_debug_pods()


@app.delete("/api/debug-pods/{namespace}/{pod_name}")
async def delete_debug_pod(namespace: str, pod_name: str):
    _require_debug_pods()
    try:
        await k8s_client.delete_debug_job(namespace, pod_name)
    except ApiException as e:
        raise HTTPException(e.status or 500, _api_error_detail(e))
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return JSON for unhandled errors instead of Starlette's plain-text 500.

    The UI parses every API response as JSON; a text/plain 'Internal Server
    Error' made fetch's r.json() throw an opaque SyntaxError and hid the real
    problem. The exception is still re-raised by the server middleware, so
    tracebacks keep landing in the pod logs.
    """
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


# ---- Model catalog ----


@app.get("/api/catalog")
async def get_catalog():
    return {"tiers": catalog.list_by_tier(), "tier_labels": TIER_LABELS}


@app.post("/api/catalog")
async def add_catalog_entry(req: Request):
    entry = await req.json()
    if not entry.get("name") or not entry.get("image"):
        raise HTTPException(400, "name and image are required")
    return catalog.add(entry)


@app.delete("/api/catalog/{catalog_id}")
async def remove_catalog_entry(catalog_id: str):
    if not catalog.remove(catalog_id):
        raise HTTPException(404, "catalog entry not found")
    return {"ok": True}


# ---- Push to MLIS ----


@app.post("/api/push")
async def push_models(req: Request):
    """Push a JSON array of model configs into the AIOLI packaged_models table.

    Duplicate (name, version) entries are skipped.  Returns per-config results.
    """
    configs = await req.json()
    if not isinstance(configs, list):
        raise HTTPException(400, "expected a JSON array of model configs")
    results = await aioli_db.push_batch(configs)
    pushed = sum(1 for r in results if r["status"] == "pushed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors = sum(1 for r in results if r["status"] == "error")
    return {"results": results, "pushed": pushed, "skipped": skipped, "errors": errors}


app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)
