"""FastAPI service that serves the HF Model Downloader UI and Job API."""

import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, field_validator, model_validator

from .catalog import TIER_INFO, TIER_LABELS, TIERS, Catalog
from .db import AioliDB
from .gc import build_gc_plan, parse_aioli_rows
from .k8s import K8sClient
from .preflight import (
    PreflightConfig,
    PreflightError,
    PreflightService,
    parse_k8s_quantity,
)
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
# Prefill for the form's S3-destination input (rendered hidden unless s3 is
# the default backend — and hidden inputs are still submitted with the form).
# Must be "" when no bucket is configured: a literal "s3:///" prefill would
# ride along on every PVC-backend submit and be rejected by the s3_path
# validator, which demands a bucket after the scheme.
S3_DEFAULT_PATH = (
    f"s3://{S3_BUCKET}/" + (f"{S3_PREFIX}/" if S3_PREFIX else "")
) if S3_BUCKET else ""
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
# "Refresh from GitHub" button: raw URL of the catalog JSON on GitHub
# (e.g. https://raw.githubusercontent.com/<org>/<repo>/<branch>/seed_catalog.json).
CATALOG_GITHUB_URL = os.environ.get("CATALOG_GITHUB_URL", "")
# TLS verification for that fetch. On HPE clusters the Zscaler MITM proxy
# presents an untrusted cert, so hpe_proxies-style deployments verify=off
# (chart wires this from the same skipTls logic the downloader Jobs use).
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

log = logging.getLogger(__name__)

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


def _page_context() -> dict:
    return {
        "max_concurrency": MAX_CONCURRENCY,
        "default_namespace": DEFAULT_NAMESPACE,
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
    return templates.TemplateResponse(request=request, name="index.html", context=_page_context())


@app.get("/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request):
    return templates.TemplateResponse(request=request, name="catalog.html", context=_page_context())


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
    return {"tiers": catalog.list_by_tier(), "tier_labels": TIER_LABELS, "tier_info": TIER_INFO}


@app.post("/api/catalog")
async def add_catalog_entry(req: Request):
    entry = await req.json()
    if not entry.get("name") or not entry.get("image"):
        raise HTTPException(400, "name and image are required")
    return catalog.add(entry)


@app.post("/api/catalog/batch")
async def add_catalog_entries(req: Request):
    """Add one entry (JSON object) or many (JSON array) from the direct-JSON textarea.

    Each entry needs name and image.  Entries are deduplicated: a catalog_id
    already present (or a name+version pair already in the catalog) is skipped,
    so re-pasting the same JSON doesn't create duplicates.  Returns per-entry
    results for the UI.
    """
    entries = await req.json()
    if isinstance(entries, dict):
        entries = [entries]  # single {MODEL_CONFIGURATION}
    if not isinstance(entries, list):
        raise HTTPException(400, "expected a JSON object or a JSON array of entries")
    present_ids = {e.get("catalog_id") for e in catalog.all()}
    present_pairs = {(e.get("name"), e.get("version")) for e in catalog.all()}
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
            entry.get("version"),
        ) in present_pairs:
            skipped += 1
            results.append({"status": "skipped", "name": name, "detail": "already in catalog"})
            continue
        e = catalog.add(entry)
        added += 1
        present_pairs.add((e["name"], e.get("version")))
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
async def push_models(req: Request):
    """Push a JSON object or a JSON array of model configs into the AIOLI packaged_models table.

    Duplicate (name, version) entries are skipped.  Returns per-config results.
    """
    configs = await req.json()
    if isinstance(configs, dict):
        configs = [configs]  # single {MODEL_CONFIGURATION} — same as a 1-element array
    if not isinstance(configs, list):
        raise HTTPException(400, "expected a JSON object or a JSON array of model configs")
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
