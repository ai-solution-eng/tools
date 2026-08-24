"""FastAPI service that serves the HF Model Downloader UI and Job API."""

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator, model_validator

from .catalog import TIER_LABELS, TIERS, Catalog
from .db import AioliDB
from .k8s import K8sClient
from .queue import JobQueue

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

k8s_client = K8sClient(template_cm=JOB_TEMPLATE_CM, template_cm_ns=JOB_TEMPLATE_CM_NS)
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
