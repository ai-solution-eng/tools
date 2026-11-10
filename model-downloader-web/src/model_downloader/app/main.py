"""FastAPI service that serves the HF Model Downloader UI and Job API."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, field_validator, model_validator

from . import oidc_identity
from .catalog import TIER_INFO, TIER_LABELS, TIERS, Catalog
from .db import AioliDB
from .gc import (
    VERDICT_BLOCK,
    VERDICT_DELETABLE,
    VERDICT_SOFT_BLOCK,
    build_gc_plan,
    drop_delete_jobs,
    evaluate_manual_targets,
    parse_aioli_rows,
)
from .k8s import K8sClient
from .preflight import (
    PreflightConfig,
    PreflightError,
    PreflightService,
    parse_k8s_quantity,
)
from .queue import JobQueue
from .storage import DownloadedModelsCache
from .versioning import config_fingerprint, normalize_project

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
# Prefill for the form's S3-destination input (rendered hidden unless s3 is
# the default backend — and hidden inputs are still submitted with the form).
# Must be "" when no bucket is configured: a literal "s3:///" prefill would
# ride along on every PVC-backend submit and be rejected by the s3_path
# validator, which demands a bucket after the scheme.
S3_DEFAULT_PATH = (f"s3://{S3_BUCKET}/" + (f"{S3_PREFIX}/" if S3_PREFIX else "")) if S3_BUCKET else ""
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
# Project recorded on a pushed packaged model that names none (chart:
# catalog.defaultProject).  Empty = leave it blank, the historical behaviour.
# The target is MLIS's "project" filter: rows with an empty project only show
# under "All projects".  The value is a project NAME: a PCAI project-* namespace
# is accepted and its project- prefix is stripped before the push (see
# versioning.strip_project_prefix).
CATALOG_DEFAULT_PROJECT = os.environ.get("CATALOG_DEFAULT_PROJECT", "").strip()
# "Refresh from GitHub" button: raw URL of the catalog JSON on GitHub
# (e.g. https://raw.githubusercontent.com/<org>/<repo>/<branch>/seed_catalog.json).
CATALOG_GITHUB_URL = os.environ.get("CATALOG_GITHUB_URL", "")
# TLS verification for that fetch. Env-var driven: the chart wires
# CATALOG_GITHUB_VERIFY_TLS from catalog.githubVerifyTls, which defaults to
# verify-on; behind a corporate MITM proxy with an untrusted cert a site
# sets it false explicitly.
CATALOG_GITHUB_VERIFY_TLS = os.environ.get("CATALOG_GITHUB_VERIFY_TLS", "true").strip().lower() == "true"
# Namespace dropdown search on the front page: only namespaces with this
# prefix are offered (empty prefix offers every namespace).
NAMESPACE_PREFIX = os.environ.get("NAMESPACE_PREFIX", "project-user-")
AIOLI_DB_HOST = os.environ.get("AIOLI_DB_HOST", "aioli-db-service-hpe-mlis.mlis.svc.cluster.local")
AIOLI_DB_PORT = int(os.environ.get("AIOLI_DB_PORT", "5432"))
AIOLI_DB_NAME = os.environ.get("AIOLI_DB_NAME", "aioli")
AIOLI_DB_USER = os.environ.get("AIOLI_DB_USER", "postgres")
AIOLI_DB_SECRET_NAME = os.environ.get("AIOLI_DB_SECRET_NAME", "aioli-db-password")
AIOLI_DB_SECRET_NS = os.environ.get("AIOLI_DB_SECRET_NS", "mlis")
AIOLI_DB_SECRET_KEY = os.environ.get("AIOLI_DB_SECRET_KEY", "password")

# ---- MD-B preflight + per-namespace quota --------------------------------
# PREFLIGHT_MODE: refuse (default) | warn | off. warn reports the same
# decisions in the response but never refuses; off skips the check entirely.
PREFLIGHT_ENABLED = os.environ.get("PREFLIGHT_ENABLED", "true").strip().lower() == "true"
PREFLIGHT_MODE = os.environ.get("PREFLIGHT_MODE", "refuse")
# Bytes map: "ns1:536870912000,ns2:1099511627776" + a default. Values accept
# plain integers or k8s quantities ("500Gi") for readability.
QUOTA_DEFAULT = os.environ.get("QUOTA_DEFAULT", "")
QUOTA_NAMESPACES = os.environ.get("QUOTA_NAMESPACES", "")
QUOTA_MODE = os.environ.get("QUOTA_MODE", "refuse")  # refuse | warn | off
# Safety margin subtracted from free space before the fit check (headroom for
# growth of in-flight writes the du pass cannot see yet).
PREFLIGHT_SAFETY_MARGIN = int(os.environ.get("PREFLIGHT_SAFETY_MARGIN", "0"))
# TTL for the cached storage census / size estimates (seconds).
PREFLIGHT_USAGE_TTL = int(os.environ.get("PREFLIGHT_USAGE_TTL", "120"))
PREFLIGHT_SIZE_TTL = int(os.environ.get("PREFLIGHT_SIZE_TTL", "300"))

# ---- MD-C TTL/GC -----------------------------------------------------------
# The GC deletion itself runs in the CronJob's pod (it needs the PVC); the
# app exposes the dry-run report (gc.dryRun defaults true — the report lands
# in the UI BEFORE any deletion is ever enabled) and the settings the job
# reads from its own env. GC_REPORT_TTL mirrors the single-flight cache
# pattern of the downloaded-models listing.
GC_ENABLED = os.environ.get("GC_ENABLED", "false").strip().lower() == "true"
GC_TTL_DAYS = float(os.environ.get("GC_TTL_DAYS", "30"))
GC_DRY_RUN = os.environ.get("GC_DRY_RUN", "true").strip().lower() == "true"
GC_PROTECTED = [p for p in os.environ.get("GC_PROTECTED", "").split(",") if p.strip()]
GC_MIN_KEEP = int(os.environ.get("GC_MIN_KEEP", "0"))
GC_REPORT_TTL = int(os.environ.get("GC_REPORT_TTL", "120"))

# ---- Manual deletion (admin-initiated) --------------------------------------
# A different feature from GC: GC is scheduled, TTL-driven and unattended; this
# is an admin selecting exact directories now. It shares gc.py's protection
# signals and nothing else. Disabled by default; the route handlers are always
# registered (decorators run at import) but every one of them refuses with 404
# (page) or 400 (API) when the feature is off, so there is nothing to
# authenticate and nothing to reach.
MANUAL_DELETE_ENABLED = os.environ.get("MANUAL_DELETE_ENABLED", "false").strip().lower() == "true"
MANUAL_DELETE_MAX_TARGETS = int(os.environ.get("MANUAL_DELETE_MAX_TARGETS", "20"))
MANUAL_DELETE_JOB_TTL = int(os.environ.get("MANUAL_DELETE_JOB_TTL", "86400"))
# Hard cap on one delete Job. Generous on purpose: removing tens of GiB over
# network storage takes minutes, and killing the process mid-rmtree is worse
# than waiting. It exists to catch a pod that never schedules.
MANUAL_DELETE_JOB_DEADLINE = int(os.environ.get("MANUAL_DELETE_JOB_DEADLINE", "3600"))
# No fallback to the scanner image on purpose: the Job runs
# "python3 -m app.delete_targets" and imports the kubernetes + psycopg2
# clients, none of which the scanner image carries (see §9.1 of the design
# note). The chart sets this to the app image; an empty value is a
# misconfiguration the commit endpoint refuses rather than guesses at.
MANUAL_DELETE_IMAGE = os.environ.get("MANUAL_DELETE_IMAGE", "")
MANUAL_DELETE_AUTH_MODE = (os.environ.get("MANUAL_DELETE_AUTH_MODE", "sso") or "sso").strip().lower()
MANUAL_DELETE_ADMIN_GROUPS = [
    g for g in os.environ.get("MANUAL_DELETE_ADMIN_GROUPS", "").split(",") if g.strip()
]
MANUAL_DELETE_ADMINS = [a for a in os.environ.get("MANUAL_DELETE_ADMINS", "").split(",") if a.strip()]
MANUAL_DELETE_SECRET_NAME = os.environ.get("MANUAL_DELETE_SECRET_NAME", "model-delete-key")
MANUAL_DELETE_SECRET_NAMESPACE = os.environ.get("MANUAL_DELETE_SECRET_NAMESPACE", APP_NAMESPACE)
MANUAL_DELETE_SECRET_KEY = os.environ.get("MANUAL_DELETE_SECRET_KEY", "token")
# Header names the PCAI oauth2-proxy injects (verified against the live
# oauth2-proxy alpha config). X-Forwarded-User is NOT among them.
SSO_USER_HEADERS = ("X-Auth-Request-Preferred-Username", "kubeflow-userid")
SSO_GROUP_HEADERS = ("X-Auth-Request-Groups", "kubeflow-groups")
# How long a preview's pinned target set stays valid. Must exceed the scanner
# Job timeout (300s) because the clock starts when the preview finishes, not
# when it starts.
MANUAL_DELETE_VERDICT_TTL = int(os.environ.get("MANUAL_DELETE_VERDICT_TTL", "900"))
# Cap on concurrent pinned previews; each one can hold a resolved target list.
_MANUAL_DELETE_MAX_PREVIEWS = 32

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Content-hash every static asset once at startup so cache-busting query strings
# change whenever a file changes. The old templates hardcoded "?v=0.7.2", which
# never changed across releases, so browsers kept running stale JS (e.g. the one
# that predates the custom download-location field) after an upgrade — silently
# dropping cache_root from submissions. A per-file hash guarantees the browser
# refetches the asset on the next deploy.
ASSET_NAMES = ("app.js", "style.css", "catalog.js", "admin_delete.js", "favicon.jpg")


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
    secret_name=AIOLI_DB_SECRET_NAME,  # secret-scan:allow (k8s Secret NAME constant, not a credential)
    secret_ns=AIOLI_DB_SECRET_NS,
    secret_key=AIOLI_DB_SECRET_KEY,
    default_project=CATALOG_DEFAULT_PROJECT,
)
downloaded_cache = DownloadedModelsCache(pvc_refresh_interval=PVC_REFRESH_INTERVAL)


def _parse_quota_map(spec: str) -> dict[str, int]:
    """``ns:500Gi,ns2:1099511627776`` -> {ns: bytes}.

    Values may be integers or k8s quantities; bad entries are logged and
    skipped (a typo must not zero a namespace's quota into unlimited-by-
    accident — skipping falls back to quota.default, which is the stricter
    default posture).
    """
    from .preflight import parse_k8s_quantity

    quotas: dict[str, int] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        ns, sep, value = part.partition(":")
        ns = ns.strip()
        value = value.strip()
        if not sep or not ns or not value:
            log.warning("ignoring malformed quota entry %r (want namespace:bytes)", part)
            continue
        parsed = int(value) if value.isdigit() else parse_k8s_quantity(value)
        if parsed is None:
            log.warning("ignoring quota entry with unparseable size %r", part)
            continue
        quotas[ns] = parsed
    return quotas


preflight_service = PreflightService(
    PreflightConfig(
        enabled=PREFLIGHT_ENABLED,
        mode=PREFLIGHT_MODE,
        quota_default_bytes=parse_k8s_quantity(QUOTA_DEFAULT) if QUOTA_DEFAULT else None,
        quota_namespaces=_parse_quota_map(QUOTA_NAMESPACES),
        quota_mode=QUOTA_MODE,
        safety_margin_bytes=PREFLIGHT_SAFETY_MARGIN,
    ),
    k8s=k8s_client,
    usage_ttl=PREFLIGHT_USAGE_TTL,
    size_ttl=PREFLIGHT_SIZE_TTL,
    scan_image=PVC_SCAN_IMAGE,
)

# ---- MD-C GC dry-run report cache (single-flight, DownloadedModelsCache
# pattern): concurrent requests share one build; ?force=1 rebuilds debounced.
gc_report_cache: dict = {"report": None, "built_at": 0.0}
_gc_lock: asyncio.Lock | None = None


async def _gc_lock_or_create() -> asyncio.Lock:
    global _gc_lock
    if _gc_lock is None:
        _gc_lock = asyncio.Lock()
    return _gc_lock


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
        # "s3://" / "s3:///" — scheme but no bucket — is the degenerate prefill
        # older builds rendered when no S3 bucket is configured. It carries no
        # information: treat it as empty (storage='s3' then gets the clearer
        # "s3_path is required" error from the model validator below).
        if v in ("s3://", "s3:///"):
            return ""
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


def _download_scan_status() -> str:
    """Status sentence(s) for the "Downloaded models" section, reading exactly:

      * S3 configured    -> "s3 automatic scanning enabled."
      * PVC auto scan on -> "pvc automatic scanning enabled at <N> second cadence."
      * neither          -> "No automatic scanning enabled."

    Both sentences are shown when both sources are enabled.
    """
    parts: list[str] = []
    if S3_BUCKET:
        parts.append("s3 automatic scanning enabled.")
    if PVC_SCAN_ENABLED:
        parts.append(f"pvc automatic scanning enabled at {PVC_REFRESH_INTERVAL} second cadence.")
    return " ".join(parts) if parts else "No automatic scanning enabled."


async def _page_context(request: Request | None = None) -> dict:
    # The namespace field defaults to the caller's own project namespace when
    # SSO tells us who they are and that namespace exists.
    default_namespace = await _resolve_user_namespace(request) if request is not None else DEFAULT_NAMESPACE
    return {
        "max_concurrency": MAX_CONCURRENCY,
        "default_namespace": default_namespace,
        "app_namespace": APP_NAMESPACE,
        "tier_labels": TIER_LABELS,
        "tier_info": TIER_INFO,
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
        "download_scan_status": _download_scan_status(),
        "debug_pods_enabled": DEBUG_POD_ENABLED and k8s_client.debug_pod_available,
        "debug_pod_image": DEBUG_POD_IMAGE,
        "catalog_github_url": CATALOG_GITHUB_URL,
        "gc_enabled": GC_ENABLED,
        "gc_ttl_days": GC_TTL_DAYS,
        "gc_dry_run": GC_DRY_RUN,
        "assets": STATIC_HASHES,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context=await _page_context(request))


@app.get("/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request):
    return templates.TemplateResponse(request=request, name="catalog.html", context=await _page_context(request))


@app.post("/api/jobs")
async def submit_job(req: SubmitRequest):
    # MD-B: preflight/quota runs inside queue.submit before Job creation.
    # A refusal raises PreflightError/QuotaExceededError with the exact
    # bytes-needed vs bytes-free / used-vs-quota reason — surfaced as a 4xx
    # detail the UI shows verbatim in the submit error path. Warn decisions
    # ride along in the response so the UI can display them non-fatally.
    try:
        record = await queue.submit(
            req.namespace,
            req.model_name,
            req.hf_token,
            storage=req.storage,
            s3_path=req.s3_path,
            chat_template_path=req.chat_template_path,
            chat_template_contents=req.chat_template_contents,
            cache_root=req.cache_root,
            preflight=preflight_service,
        )
    except PreflightError as e:
        # QuotaExceededError subclasses PreflightError; both carry the exact
        # human-readable refusal (bytes-needed vs bytes-free / used vs quota).
        raise HTTPException(422, e.detail) from e
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


@app.get("/api/namespaces")
async def list_namespaces():
    """Namespaces for the front-page dropdown search (NAMESPACE_PREFIX-filtered).

    RBAC denial (the pod's service account lacks namespaces/list) surfaces as
    a clean 403 detail — the UI falls back to typing a namespace manually.
    """
    try:
        names = await k8s_client.list_namespaces()
    except ApiException as e:
        raise HTTPException(e.status or 500, _api_error_detail(e)) from e
    return {"namespaces": sorted(n for n in names if n.startswith(NAMESPACE_PREFIX)), "prefix": NAMESPACE_PREFIX}


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
        return {"models": [], "scan_status": _download_scan_status()}

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
        # The clean status sentence the UI shows above the table (same text as
        # the page render — one source of truth: _download_scan_status()).
        "scan_status": _download_scan_status(),
        # Why each source contributed what it did — so an empty table is
        # explainable (scan ran and found 0 vs skipped vs error). The UI logs
        # this to the console rather than rendering it.
        "scan": dict(downloaded_cache.last_status),
    }


# ---- MD-C TTL/GC dry-run report -------------------------------------------------


def _require_gc() -> None:
    if not GC_ENABLED:
        raise HTTPException(400, "gc is disabled (gc.enabled=false)")


async def _build_gc_report(force: bool = False) -> dict:
    """The GC dry-run report (no deletion — deletion runs in the GC Job).

    Pipeline (gc.py): scanner/du enumeration -> TTL/LRU line -> AIOLI
    packaged_models cross-check (strictly read-only SELECT) -> live managed
    Job cross-check (annotations) -> three-key AND verdict per cache dir.
    Cached single-flight with a TTL; ?force=1 rebuilds (debounced by the
    same interval, matching the downloaded-listing pattern).

    The report lands in the UI FIRST — gc.dryRun=true is the chart default
    and the GC Job refuses to delete until an operator flips it — so this
    endpoint never performs or triggers a deletion itself.
    """
    lock = await _gc_lock_or_create()
    async with lock:
        fresh = (time.time() - gc_report_cache["built_at"]) < GC_REPORT_TTL
        if gc_report_cache["report"] is not None and fresh and not force:
            return gc_report_cache["report"]

        scan_root = f"/mnt/{PVC_SUBPATH}"
        entries: list[dict] = []
        scan_error = ""
        if PVC_SCAN_IMAGE and PVC_NAME:
            from .storage import scan_pvc_via_job

            models, err = await scan_pvc_via_job(
                k8s_client,
                namespace=DEFAULT_NAMESPACE,
                pvc_name=PVC_NAME,
                scan_root=scan_root,
                image=PVC_SCAN_IMAGE,
                timeout=300,
                mount_path="/mnt/",
            )
            if err:
                scan_error = err
            for m in models:
                entries.append(
                    {
                        "model_name": m.model_name,
                        "cachepath": m.cachepath or "",
                        "mtime": m.last_modified,
                        "bytes": m.bytes,
                        "manifest": m.provenance_manifest,
                        "scanned": m.scanned,
                    }
                )
        else:
            scan_error = "not configured (pvc or pvcScanImage missing)"

        aioli_uris: list[str] = []
        aioli_error = ""
        try:
            aioli_uris = parse_aioli_rows(await aioli_db.fetch_packaged_uris())
        except Exception as e:
            aioli_error = f"{type(e).__name__}: {e}"
            log.warning("GC AIOLI cross-check failed: %s — treating ALL dirs as AIOLI-protected", e)
            # Fail-safe: an unreachable AIOLI must not unprotect anything.
            # Mark every entry as aioli-protected by injecting a catch-all.
            aioli_uris = ["*AIOLI-UNAVAILABLE*"]

        live_jobs: list[dict] = []
        try:
            live_jobs = await k8s_client.list_managed_jobs_by_annotation()
        except Exception as e:
            log.warning("GC live-job cross-check failed: %s", e)

        if aioli_error:
            plan = build_gc_plan(
                entries,
                ttl_days=GC_TTL_DAYS,
                aioli_uris=[],
                live_jobs=live_jobs,
                protected_models=GC_PROTECTED,
                min_keep=GC_MIN_KEEP,
            )
            # Override: without a usable AIOLI read, nothing is deletable.
            for row in plan["rows"]:
                if row["action"] == "delete":
                    row["action"] = "keep"
                    row["reason"] = "AIOLI cross-check unavailable — fail-safe keeps everything"
            plan["summary"]["delete"] = 0
            plan["summary"]["aioli_unavailable"] = True
        else:
            plan = build_gc_plan(
                entries,
                ttl_days=GC_TTL_DAYS,
                aioli_uris=aioli_uris,
                live_jobs=live_jobs,
                protected_models=GC_PROTECTED,
                min_keep=GC_MIN_KEEP,
            )

        report = {
            "generated_at": plan["generated_at"],
            "ttl_days": GC_TTL_DAYS,
            "dry_run": GC_DRY_RUN,
            "enabled": GC_ENABLED,
            "protected_models": GC_PROTECTED,
            "min_keep": GC_MIN_KEEP,
            "scan_error": scan_error,
            "aioli_error": aioli_error,
            "aioli_model_count": len(aioli_uris),
            "live_job_count": len(live_jobs),
            "rows": plan["rows"],
            "summary": plan["summary"],
        }
        gc_report_cache["report"] = report
        gc_report_cache["built_at"] = time.time()
        return report


@app.get("/api/gc/report")
async def gc_report(request: Request):
    """Dry-run GC report for the UI (cached; ?force=1 rebuilds)."""
    _require_gc()
    force = (request.query_params.get("force") or "") in ("1", "true", "yes")
    return await _build_gc_report(force=force)


@app.get("/api/gc/config")
async def gc_config():
    """The GC settings the UI renders (no cluster calls)."""
    return {
        "enabled": GC_ENABLED,
        "ttl_days": GC_TTL_DAYS,
        "dry_run": GC_DRY_RUN,
        "protected_models": GC_PROTECTED,
        "min_keep": GC_MIN_KEEP,
    }


# ---- Manual deletion (admin-initiated) -------------------------------------
#
# Two gates, deliberately independent (see documentation/manual-delete-design.md):
#
#   * auth    -- WHO may delete. An admin identity from the PCAI SSO gate, or a
#                shared key on installs without oauth2-proxy. Never a normal
#                user session.
#   * verdict -- MAY this be deleted. gc.evaluate_manual_targets(), the same
#                logic the preview shows and the Job re-runs in-pod.
#
# The isolation rule is the load-bearing one: the client may only name
# cachepaths that the CURRENT scan returned. Anything else is rejected, never
# normalised or repaired. The Job then receives exactly the resolved set.

# Secret read once per process: rotation of a destructive-action credential
# should require a restart anyway, and re-reading per request would be an API
# call per keystroke.
_delete_key_cache: dict[str, str] = {}
# Pinned previews: verdict_id -> {actor, created, expires_at, rows, targets}.
# In-memory and therefore single-replica (deployment.yaml pins replicas: 1).
_manual_delete_previews: dict[str, dict] = {}
_preview_lock: asyncio.Lock | None = None


class DeletionPreviewRequest(BaseModel):
    targets: list[str] = []


class DeletionCommitRequest(BaseModel):
    verdict_id: str = ""
    confirm: str = ""
    overrides: list[str] = []


def _require_manual_delete() -> None:
    if not MANUAL_DELETE_ENABLED:
        # House convention for a disabled feature: 400 + detail, like _require_gc.
        raise HTTPException(400, "manual delete is disabled (manualDelete.enabled=false)")


async def _expected_delete_key() -> str:
    """The configured shared key, or "" when the feature is unusable."""
    if "value" in _delete_key_cache:
        return _delete_key_cache["value"]
    value = ""
    if MANUAL_DELETE_SECRET_NAME:
        try:
            data = await k8s_client.read_secret(MANUAL_DELETE_SECRET_NAMESPACE, MANUAL_DELETE_SECRET_NAME)
            value = (data.get(MANUAL_DELETE_SECRET_KEY) or "").strip()
        except ApiException as e:
            if e.status != 404:
                log.warning("manual delete: could not read Secret %s/%s: %s", MANUAL_DELETE_SECRET_NAMESPACE, MANUAL_DELETE_SECRET_NAME, e)
            value = ""
        except Exception as e:  # pragma: no cover - defensive
            log.warning("manual delete: Secret read failed: %s", e)
            value = ""
    if not value:
        # Absent/empty must NEVER mean "no gate": leave it empty so every
        # comparison below fails, and say so in the response. Deliberately NOT
        # cached: a transient API failure must not turn into a permanent
        # lockout until restart, and a key created after startup should start
        # working without one.
        log.warning("manual delete: no usable delete key configured — all requests will be refused")
        return value
    _delete_key_cache["value"] = value
    return value


def _sso_subject(request: Request) -> str:
    """The caller's SSO identity, or "" when no identity header is present.

    oauth2-proxy injects X-Auth-Request-Preferred-Username (claim
    preferred_username) and kubeflow-userid on this cluster; X-Forwarded-User is
    NOT among them. The value is the same string the fleet's OIDC verifier
    resolves (pcai_utils/oidc_identity.py, D21) — its token fixtures use
    andrew-bydlon for both sub and preferred_username.
    """
    for header in SSO_USER_HEADERS:
        candidate = (request.headers.get(header) or "").strip()
        if candidate:
            return candidate
    return ""


# Namespace list cache: one API call per minute rather than one per page render.
_namespace_cache: dict = {"at": 0.0, "names": frozenset()}
_NAMESPACE_CACHE_TTL = 60.0


async def _namespace_names() -> frozenset:
    now = time.time()
    if _namespace_cache["names"] and (now - _namespace_cache["at"]) < _NAMESPACE_CACHE_TTL:
        return _namespace_cache["names"]
    try:
        names = await k8s_client.list_namespaces()
    except Exception as e:
        # Best effort: a denied or failed list must not break page rendering.
        log.debug("namespace list unavailable for the form default: %s", e)
        return _namespace_cache["names"] or frozenset()
    _namespace_cache["at"] = now
    _namespace_cache["names"] = frozenset(names)
    return _namespace_cache["names"]


# Per-namespace "does models-pvc live here" cache, same 60s TTL.
_pvc_ns_cache: dict[str, tuple[float, bool]] = {}


async def _namespace_serves_models_pvc(namespace: str) -> bool:
    """True when PVC_NAME exists in *namespace*.

    A PVC is namespace-scoped, so a download or delete Job created anywhere else
    waits forever on "persistentvolumeclaim not found". A namespace is only a
    valid form default if it actually serves the model volume — otherwise the
    prefill would hand the user a guaranteed-hanging Job.
    """
    now = time.time()
    cached = _pvc_ns_cache.get(namespace)
    if cached and (now - cached[0]) < _NAMESPACE_CACHE_TTL:
        return cached[1]
    ok = False
    try:
        # A successful read (even with unknown capacity) proves the PVC exists;
        # a 404 or an RBAC denial both mean "do not default here".
        await k8s_client.read_pvc_capacity(namespace, PVC_NAME)
        ok = True
    except Exception as e:
        log.debug("models PVC not readable in %s: %s", namespace, e)
    _pvc_ns_cache[namespace] = (now, ok)
    return ok


async def _resolve_user_namespace(request: Request) -> str:
    """project-user-<preferred_username> when that namespace exists, else the default.

    The convention is PCAI's: a user owns project-user-<username>, and
    preferred_username is the claim oauth2-proxy forwards. That is a naming
    convention, not an authorization fact, so it is used ONLY to pre-fill the
    form and is verified against the namespace list the app can already read.
    It also requires that namespace to actually hold the model PVC, because a
    Job elsewhere cannot mount it. Anything unexpected — no identity header, no
    such namespace, no PVC there, a failed list — falls back to the configured
    default rather than guessing.
    """
    subject = _sso_subject(request)
    if not subject:
        return DEFAULT_NAMESPACE
    candidate = f"{NAMESPACE_PREFIX}{subject}"
    if candidate in await _namespace_names() and await _namespace_serves_models_pvc(candidate):
        return candidate
    return DEFAULT_NAMESPACE


async def _verified_subject(request: Request) -> str:
    """The subject of a VERIFIED OIDC bearer token, or "".

    oauth2-proxy injects `Authorization: Bearer <id_token>` alongside the
    identity headers, so the app can verify the token instead of trusting a
    header any pod in the cluster could forge. Verification is the fleet's
    shared pcai_utils module (vendored here): RS256 only, JWKS-cached,
    fail-closed, and it never logs token material.

    The JWKS fetch is blocking, so it runs in a worker thread; the module caches
    keys (and negative-caches failures), so this is cheap after the first call.
    """
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return ""
    token = header[7:].strip()
    if not token:
        return ""
    claims = await asyncio.to_thread(oidc_identity.verify_and_decode, token)
    if not claims:
        return ""
    name = str(claims.get(oidc_identity.DEFAULT_IDENTITY_CLAIM) or claims.get("sub") or "").strip()
    return name


async def _actor_from_sso(request: Request) -> str:
    """The caller's identity, or a 403 that names the misconfiguration."""
    if oidc_identity.oidc_enabled():
        # A verified token is REQUIRED when an IdP is configured: a bare header
        # is forgeable by anything that can reach the Service directly.
        user = await _verified_subject(request)
        if not user:
            raise HTTPException(
                403,
                "SSO token missing or not verifiable — admin actions require a valid "
                "OIDC bearer token (set oidc.issuer/audience, or manualDelete.auth.mode=secret)",
            )
    else:
        # No IdP configured: fall back to the proxy-injected header. Weaker by
        # construction — the Service must not be reachable except via the ingress.
        user = _sso_subject(request)
    if not user:
        raise HTTPException(
            403,
            "no identity header on this request — this deployment is not behind the "
            "PCAI oauth2-proxy gate; set manualDelete.auth.mode=secret",
        )
    if not MANUAL_DELETE_ADMINS and not MANUAL_DELETE_ADMIN_GROUPS:
        raise HTTPException(
            403,
            "no admins configured — set manualDelete.auth.admins or manualDelete.auth.adminGroups",
        )
    if user in MANUAL_DELETE_ADMINS:
        return user
    groups: set[str] = set()
    for header in SSO_GROUP_HEADERS:
        raw = request.headers.get(header) or ""
        for part in raw.replace(",", " ").split():
            part = part.strip()
            if part:
                groups.add(part)
    if groups & set(MANUAL_DELETE_ADMIN_GROUPS):
        return user
    raise HTTPException(403, f"admin required: {user} is not in an admin group")


async def _delete_actor(request: Request) -> str:
    """Resolve the acting admin, or raise. Never returns for a denial."""
    _require_manual_delete()
    if MANUAL_DELETE_AUTH_MODE == "secret":
        presented = (request.headers.get("X-Delete-Key") or "").strip()
        if not presented:
            raise HTTPException(401, "missing X-Delete-Key header")
        expected = await _expected_delete_key()
        if not expected:
            raise HTTPException(403, "deletion is not configured on this deployment (delete key Secret missing or empty)")
        # compare_digest on bytes: the "" == "" case above is already excluded
        # by the empty check, and a constant-time compare avoids a timing oracle.
        if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(403, "invalid delete key")
        # Fingerprint, never the key, so two keys are distinguishable in audit.
        return "shared-key:" + hashlib.sha256(expected.encode("utf-8")).hexdigest()[:8]
    return await _actor_from_sso(request)


async def _collect_deletion_inputs() -> tuple[list[dict], dict]:
    """Scan once, then the two cross-checks, each with an availability flag.

    Only rows with a cachepath are deletion candidates: a job-history or S3 row
    has no on-disk directory, so it cannot be named as a target.
    """
    scan_root = f"/mnt/{PVC_SUBPATH}"
    models = await downloaded_cache.get_models(
        queue.list_jobs(),
        pvc_enabled=True,
        pvc_namespace=DEFAULT_NAMESPACE,
        pvc_name=PVC_NAME,
        pvc_scan_root=scan_root,
        pvc_image=PVC_SCAN_IMAGE,
        pvc_mount_path="/mnt/",
        k8s=k8s_client,
        s3_bucket="",  # S3 eviction is out of scope: PVC rows only
        s3_prefix="",
        s3_endpoint="",
        s3_access_key="",
        s3_secret_key="",
        # force=False on purpose: this shares the DownloadedModelsCache with
        # /api/downloaded, so the admin page reuses the scan the main page (or a
        # previous preview) already paid for, and only spawns a scanner Job when
        # the cached list is older than PVC_REFRESH_INTERVAL. The safety
        # property does not depend on the preview being seconds old — the Job
        # re-validates every path in-pod before removing it.
        force=False,
    )
    entries = [
        {
            "model_name": m.model_name,
            "cachepath": m.cachepath or "",
            "mtime": m.last_modified,
            "bytes": m.bytes,
            "manifest": m.provenance_manifest,
            "scanned": m.scanned,
        }
        for m in models
        if m.cachepath
    ]

    # Surface why an empty or all-blocked list happened: an empty table with no
    # explanation is the failure mode this app keeps having to fix.
    scan_error = ""
    pvc_status = str((downloaded_cache.last_status or {}).get("pvc") or "")
    if pvc_status.startswith("error"):
        scan_error = pvc_status

    aioli_uris: list[str] = []
    aioli_available = True
    aioli_error = ""
    try:
        aioli_uris = parse_aioli_rows(await aioli_db.fetch_packaged_uris())
    except Exception as e:
        aioli_available = False
        aioli_error = f"{type(e).__name__}: {e}"
        log.warning("manual delete: AIOLI cross-check failed (%s) — refusing all targets", e)

    live_jobs: list[dict] = []
    jobs_available = True
    try:
        live_jobs = drop_delete_jobs(await k8s_client.list_managed_jobs_by_annotation())
    except Exception as e:
        # NOTE: _build_gc_report fails OPEN here (it proceeds with an empty
        # list). The manual path must fail CLOSED, like the GC CronJob: a
        # deletion we cannot prove is safe is not performed.
        jobs_available = False
        log.warning("manual delete: live-jobs cross-check failed (%s) — refusing all targets", e)

    return entries, {
        "aioli_uris": aioli_uris,
        "aioli_available": aioli_available,
        "aioli_error": aioli_error,
        "live_jobs": live_jobs,
        "jobs_available": jobs_available,
        "scan_error": scan_error,
        "scan_root": scan_root,
    }


def _prune_previews() -> None:
    now = time.time()
    for key in [k for k, v in _manual_delete_previews.items() if v["expires_at"] <= now]:
        _manual_delete_previews.pop(key, None)
    while len(_manual_delete_previews) > _MANUAL_DELETE_MAX_PREVIEWS:
        oldest = min(_manual_delete_previews, key=lambda k: _manual_delete_previews[k]["created"])
        _manual_delete_previews.pop(oldest, None)


@app.get("/admin/delete", response_class=HTMLResponse)
async def admin_delete_page(request: Request):
    """The admin deletion page. The route does not exist when disabled."""
    if not MANUAL_DELETE_ENABLED:
        raise HTTPException(404)
    context = await _page_context(request)
    context.update(
        {
            "md_auth_mode": MANUAL_DELETE_AUTH_MODE,
            "md_max_targets": MANUAL_DELETE_MAX_TARGETS,
        }
    )
    return templates.TemplateResponse(request=request, name="admin_delete.html", context=context)


@app.post("/api/admin/deletions/preview")
async def preview_deletions(req: DeletionPreviewRequest, request: Request):
    """Resolve a selection to an exact target set and return per-row verdicts.

    Nothing is deleted here, and the returned verdict_id pins the resolved set
    so the commit call acts on exactly what the operator reviewed.
    """
    actor = await _delete_actor(request)
    # Dedupe before anything else: the same directory named twice must not be
    # resolved, listed, or deleted twice.
    req.targets = list(dict.fromkeys(req.targets))
    # Two modes on purpose:
    #   * no targets -> LIST mode. Every candidate on the scan root with its
    #     verdict, so the page can render the picker. No verdict_id is issued:
    #     a pin requires an explicit selection.
    #   * targets    -> PIN mode. The selection is resolved and stored, and the
    #     commit call may only act on that stored set.
    list_mode = not req.targets
    if len(req.targets) > MANUAL_DELETE_MAX_TARGETS:
        raise HTTPException(422, f"too many targets ({len(req.targets)} > {MANUAL_DELETE_MAX_TARGETS})")

    entries, ctx = await _collect_deletion_inputs()
    by_path = {e["cachepath"]: e for e in entries}
    unknown = [t for t in req.targets if t not in by_path]
    if unknown:
        # The isolation rule: only directories the CURRENT scan returned may be
        # named. A path we did not scan is refused, never repaired.
        raise HTTPException(
            422,
            f"{len(unknown)} target(s) are not in the current scan: {unknown[:3]}"
            + (" …" if len(unknown) > 3 else ""),
        )

    selected = list(by_path.values()) if list_mode else [by_path[t] for t in req.targets]
    rows = evaluate_manual_targets(
        selected,
        aioli_uris=ctx["aioli_uris"],
        live_jobs=ctx["live_jobs"],
        protected_models=GC_PROTECTED,
        aioli_available=ctx["aioli_available"],
        jobs_available=ctx["jobs_available"],
    )

    verdict_id = ""
    global _preview_lock
    if _preview_lock is None:
        _preview_lock = asyncio.Lock()
    if not list_mode:
        async with _preview_lock:
            _prune_previews()
            verdict_id = uuid.uuid4().hex
            _manual_delete_previews[verdict_id] = {
                "actor": actor,
                "created": time.time(),
                "expires_at": time.time() + MANUAL_DELETE_VERDICT_TTL,
                "rows": rows,
                "scan_root": ctx["scan_root"],
            }

    summary = {
        "deletable": sum(1 for r in rows if r["verdict"] == VERDICT_DELETABLE),
        "soft_block": sum(1 for r in rows if r["verdict"] == VERDICT_SOFT_BLOCK),
        "block": sum(1 for r in rows if r["verdict"] == VERDICT_BLOCK),
        "bytes": sum(int(r["bytes"] or 0) for r in rows if r["verdict"] != VERDICT_BLOCK),
    }
    return {
        "verdict_id": verdict_id,
        "expires_at": int(time.time() + MANUAL_DELETE_VERDICT_TTL) if verdict_id else 0,
        "rows": rows,
        "summary": summary,
        "scan_root": ctx["scan_root"],
        "max_targets": MANUAL_DELETE_MAX_TARGETS,
        # Empty/blocked results must be explainable in the UI, like the GC
        # report's fail-safe banner.
        "scan_error": ctx["scan_error"],
        "aioli_error": ctx["aioli_error"],
        # How old the shared scan is, so the page can say "reused" instead of
        # looking like it is doing work it is not.
        "scan_age_seconds": downloaded_cache.pvc_scan_age,
    }


@app.post("/api/admin/deletions")
async def commit_deletions(req: DeletionCommitRequest, request: Request):
    """Create the Job that deletes exactly the pinned target set."""
    actor = await _delete_actor(request)
    if req.confirm != "DELETE":
        raise HTTPException(422, "confirmation text must be exactly DELETE")

    global _preview_lock
    if _preview_lock is None:
        _preview_lock = asyncio.Lock()
    async with _preview_lock:
        _prune_previews()
        pinned = _manual_delete_previews.get(req.verdict_id)
    if not pinned:
        raise HTTPException(409, "preview expired or unknown — re-run the preview")
    if pinned["actor"] != actor:
        # A verdict_id is bound to the admin who produced it.
        raise HTTPException(403, "this preview belongs to a different caller")

    rows = pinned["rows"]
    by_path = {r["cachepath"]: r for r in rows}
    overrides: set[str] = set()
    for path in req.overrides:
        row = by_path.get(path)
        if row is None:
            raise HTTPException(422, f"override for a target that is not in this preview: {path}")
        if row["verdict"] == VERDICT_BLOCK:
            raise HTTPException(422, f"cannot override a hard block: {path} ({'; '.join(row['blocks'])})")
        if row["verdict"] == VERDICT_SOFT_BLOCK:
            overrides.add(path)

    chosen = [
        {"cachepath": r["cachepath"], "override": r["cachepath"] in overrides}
        for r in rows
        if r["verdict"] == VERDICT_DELETABLE or (r["verdict"] == VERDICT_SOFT_BLOCK and r["cachepath"] in overrides)
    ]
    if not chosen:
        raise HTTPException(409, "no deletable targets in this preview")
    if len(chosen) > MANUAL_DELETE_MAX_TARGETS:
        raise HTTPException(422, f"too many targets ({len(chosen)} > {MANUAL_DELETE_MAX_TARGETS})")

    # Consume the preview: one verdict_id authorises exactly ONE Job. Without
    # this, a replay inside the TTL would spawn duplicate delete Jobs against
    # directories the first one already removed.
    async with _preview_lock:
        if _manual_delete_previews.pop(req.verdict_id, None) is None:
            raise HTTPException(409, "preview already used — re-run the preview")

    # Re-verify against FRESH state immediately before creating the Job. The
    # Job itself no longer re-checks protections (it runs in the PVC's
    # namespace with the default ServiceAccount and therefore has no cluster
    # RBAC, exactly like a download Job) — so this is where the guarantee
    # lives, and it must not trust verdicts the operator may have been looking
    # at for minutes.
    fresh_entries, ctx = await _collect_deletion_inputs()
    fresh_by_path = {e["cachepath"]: e for e in fresh_entries}
    still_present = [c for c in chosen if c["cachepath"] in fresh_by_path]
    vanished = [c["cachepath"] for c in chosen if c["cachepath"] not in fresh_by_path]
    recheck = evaluate_manual_targets(
        [fresh_by_path[c["cachepath"]] for c in still_present],
        aioli_uris=ctx["aioli_uris"],
        live_jobs=ctx["live_jobs"],
        protected_models=GC_PROTECTED,
        aioli_available=ctx["aioli_available"],
        jobs_available=ctx["jobs_available"],
    )
    admitted: list[dict] = []
    refused: list[str] = []
    for row in recheck:
        path = row["cachepath"]
        if row["verdict"] == VERDICT_DELETABLE:
            admitted.append({"cachepath": path, "override": path in overrides, "verdict": row["verdict"]})
        elif row["verdict"] == VERDICT_SOFT_BLOCK and path in overrides:
            admitted.append({"cachepath": path, "override": True, "verdict": row["verdict"]})
        else:
            refused.append(f"{path}: {'; '.join(row['blocks']) or row['verdict']}")
    if not admitted:
        detail = "; ".join((refused + [f"{p}: no longer present" for p in vanished])[:5])
        raise HTTPException(409, f"nothing is still deletable — re-run the preview ({detail})")
    chosen = admitted

    payload = base64.b64encode(json.dumps(chosen).encode("utf-8")).decode("ascii")
    try:
        job_name = await k8s_client.create_delete_job(
            # The namespace that OWNS models-pvc — the same one the scan Job
            # mounts it from and the download Jobs are created in. PVCs are
            # namespace-scoped: naming the app's namespace here left the pod
            # Pending forever with "persistentvolumeclaim not found".
            namespace=DEFAULT_NAMESPACE,
            pvc_name=PVC_NAME,
            scan_root=pinned["scan_root"],
            image=MANUAL_DELETE_IMAGE,
            job_id=uuid.uuid4().hex[:8],
            targets_b64=payload,
            actor=actor,
            job_ttl=MANUAL_DELETE_JOB_TTL,
            job_deadline=MANUAL_DELETE_JOB_DEADLINE,
            mount_path="/mnt/",
        )
    except Exception as e:
        # Nothing was created, so restore the pin: forcing the operator to
        # re-run a preview that can take a 300s scanner Job, over a transient
        # API error, is a worse trade than letting them retry the commit.
        async with _preview_lock:
            _manual_delete_previews.setdefault(req.verdict_id, pinned)
        raise HTTPException(500, f"could not create the delete Job: {e}") from e

    # The listing must stop showing what is about to disappear, and the GC
    # report must not serve a stale verdict for the next two minutes.
    downloaded_cache._last_pvc_scan = 0.0
    gc_report_cache["report"] = None
    gc_report_cache["built_at"] = 0.0

    log.info("manual delete: %s queued %d target(s) in %s", actor, len(chosen), job_name)
    return {
        "job_name": job_name,
        "targets": [c["cachepath"] for c in chosen],
        "overrides": sorted(overrides),
        "job_ttl_seconds": MANUAL_DELETE_JOB_TTL,
    }


@app.get("/api/admin/deletions/{job_name}")
async def deletion_status(job_name: str, request: Request):
    """Status + logs for a delete Job (pod logs, not a queue JobRecord)."""
    await _delete_actor(request)
    try:
        logs = await k8s_client.get_job_logs(APP_NAMESPACE, job_name)
    except ApiException as e:
        if e.status == 404:
            raise HTTPException(404, "job not found") from e
        raise HTTPException(e.status or 500, _api_error_detail(e)) from e
    return {"job_name": job_name, "logs": logs}


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
    # "tiers" stays: the Download page (app.js) and the flat-table fallback
    # still key off it.  "tree" is the lab > model > configuration grouping the
    # catalog page renders, derived server-side so the grouping rules are
    # unit-testable and identical for every client.
    return {
        "tiers": catalog.list_by_tier(),
        "tier_labels": TIER_LABELS,
        "tier_info": TIER_INFO,
        "tree": catalog.tree(),
    }


@app.post("/api/catalog")
async def add_catalog_entry(req: Request):
    entry = await req.json()
    if not entry.get("name") or not entry.get("image"):
        raise HTTPException(400, "name and image are required")
    return catalog.add(entry)


@app.post("/api/catalog/batch")
async def add_catalog_entries(req: Request):
    """Add one entry (JSON object) or many (JSON array) from the direct-JSON textarea.

    Each entry needs name and image.  Entries are deduplicated by catalog_id and
    by name + deployment configuration (versioning.config_fingerprint), so
    re-pasting the same JSON does not create duplicates while a different
    configuration of the same model still can.  Returns per-entry results for
    the UI.
    """
    entries = await req.json()
    if isinstance(entries, dict):
        entries = [entries]  # single {MODEL_CONFIGURATION}
    if not isinstance(entries, list):
        raise HTTPException(400, "expected a JSON object or a JSON array of entries")
    # Identity is name + deployment configuration, not name + version: the
    # catalog stores version 1 for every entry (MLIS assigns the real version at
    # push time), so keying on (name, version) would let the same configuration
    # in twice under two versions while refusing a genuinely different
    # configuration of the same model (vLLM vs SGLang on one GPU class).
    present_ids = {e.get("catalog_id") for e in catalog.all()}
    present_configs = {(e.get("name"), config_fingerprint(e)) for e in catalog.all()}
    added = skipped = 0
    results = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else ""
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("image"):
            skipped += 1
            results.append({"status": "skipped", "name": name or "", "detail": "name and image are required"})
            continue
        if (entry.get("catalog_id") and entry["catalog_id"] in present_ids) or (
            name,
            config_fingerprint(entry),
        ) in present_configs:
            skipped += 1
            results.append(
                {"status": "skipped", "name": name, "detail": "already in catalog (same name and configuration)"}
            )
            continue
        e = catalog.add(entry)
        added += 1
        present_configs.add((e["name"], config_fingerprint(e)))
        results.append({"status": "added", "name": e["name"], "catalog_id": e["catalog_id"]})
    return {"added": added, "skipped": skipped, "results": results}


@app.delete("/api/catalog/{catalog_id}")
async def remove_catalog_entry(catalog_id: str):
    if not catalog.remove(catalog_id):
        raise HTTPException(404, "catalog entry not found")
    return {"ok": True}


def _fetch_github_catalog(url: str) -> bytes:
    """Blocking fetch of the catalog JSON from GitHub.

    MUST run via asyncio.to_thread: network I/O on the event loop freezes the
    whole app (every endpoint, incl. /api/healthz) for as long as the
    connection hangs — on clusters without direct egress the connect/DNS
    black-holes, probes starve, and kubelet kills the pod. (This exact bug
    restarted the g2 pod on every refresh click before 1.4.1.)
    """
    if CATALOG_GITHUB_VERIFY_TLS:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(url, timeout=30, context=ctx) as resp:
        return resp.read()


@app.post("/api/catalog/refresh")
async def refresh_catalog_from_github():
    """Fetch the latest catalog JSON from GitHub and merge it into the PVC catalog.

    The URL comes from CATALOG_GITHUB_URL (Helm: catalog.githubUrl).  Merge
    rules match the on-start seed merge: add new entries, keep user edits,
    don't resurrect removed ones.  Returns per-source counters for the UI.
    """
    if not CATALOG_GITHUB_URL:
        raise HTTPException(503, "CATALOG_GITHUB_URL is not configured")
    try:
        body = await asyncio.to_thread(_fetch_github_catalog, CATALOG_GITHUB_URL)
        entries = json.loads(body.decode("utf-8"))
        return catalog.merge_entries(entries, source="github")
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"GitHub returned HTTP {e.code} for {CATALOG_GITHUB_URL}") from e
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as e:
        raise HTTPException(502, f"could not reach GitHub: {e}") from e
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise HTTPException(502, f"invalid catalog JSON from GitHub: {e}") from e


# ---- Push to MLIS ----


@app.post("/api/push")
async def push_models(req: Request, project: str = ""):
    """Push a JSON object or a JSON array of model configs into the AIOLI packaged_models table.

    MLIS assigns the version: v1 for a name it has never seen, max(version)+1
    when the name exists with a different configuration, and a skip (not a new
    version) when the configuration is identical.  Any version in the posted
    JSON is ignored.  Returns per-config results, each carrying the version it
    landed as and whether that version was bumped.

    The optional "project" query parameter files the pushed models under that
    project (the UI sends the namespace picked next to the push button; the
    PCAI "project-" namespace prefix is stripped, because MLIS files by project
    name).  It only fills in configs that name no project of their own; a
    config's own project always wins, and the chart default
    (catalog.defaultProject) applies when neither is set.
    """
    try:
        project = normalize_project(project)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    configs = await req.json()
    if isinstance(configs, dict):
        configs = [configs]  # single {MODEL_CONFIGURATION} — same as a 1-element array
    if not isinstance(configs, list):
        raise HTTPException(400, "expected a JSON object or a JSON array of model configs")
    results = await aioli_db.push_batch(configs, project=project)
    pushed = sum(1 for r in results if r["status"] == "pushed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors = sum(1 for r in results if r["status"] == "error")
    return {"results": results, "pushed": pushed, "skipped": skipped, "errors": errors}


app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)
