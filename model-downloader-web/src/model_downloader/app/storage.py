"""Storage scanning utilities for the "Downloaded models" listing.

Combines data from three sources into one deduplicated list:
  1. Succeeded job history (in-memory, tracked by the queue).
  2. A live scan of the S3 bucket (boto3 listing) when configured.
  3. A short-lived scanner Job that lists model directories on the shared
     PVC (the app pod itself does not mount the models PVC).

A model counts as "downloaded" when a ``config.json`` is present in its
directory (HF cache layout: ``<model>/models--org--repo/snapshots/<rev>/
config.json`` or the flat layout ``<model>/config.json``).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class DownloadedModel:
    """A model that exists on at least one storage backend."""

    model_name: str
    backends: list[str] = field(default_factory=list)
    location: str = ""
    last_modified: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "backends": self.backends,
            "location": self.location,
            "last_modified": self.last_modified,
        }


# ---------------------------------------------------------------------------
# Merge / deduplicate
# ---------------------------------------------------------------------------


def merge_models(*model_lists: list) -> list:
    """Merge several lists of DownloadedModel, deduplicating by model_name.

    For each model name the entry with the most recent ``last_modified`` wins
    for the timestamp and location, while backends are unioned.
    """
    merged: dict[str, DownloadedModel] = {}
    for models in model_lists:
        for m in models:
            key = m.model_name
            if key in merged:
                existing = merged[key]
                for b in m.backends:
                    if b not in existing.backends:
                        existing.backends.append(b)
                if m.last_modified > existing.last_modified:
                    existing.last_modified = m.last_modified
                    existing.location = m.location
            else:
                merged[key] = DownloadedModel(
                    model_name=m.model_name,
                    backends=list(m.backends),
                    location=m.location,
                    last_modified=m.last_modified,
                )
    return sorted(merged.values(), key=lambda m: m.last_modified, reverse=True)


# ---------------------------------------------------------------------------
# Source 1: in-memory job history
# ---------------------------------------------------------------------------


def models_from_job_history(jobs: list) -> list:
    """Extract downloaded models from succeeded JobRecord objects."""
    models = []
    for j in jobs:
        if j.status != "succeeded":
            continue
        model_name = getattr(j, "model_name", "") or ""
        if not model_name:
            continue
        backend = getattr(j, "storage", "pvc") or "pvc"
        location = getattr(j, "pvc_url", "") or getattr(j, "s3_path", "") or ""
        models.append(
            DownloadedModel(
                model_name=model_name,
                backends=[backend],
                location=location,
                last_modified=getattr(j, "finished_at", 0) or 0.0,
            )
        )
    return models


# ---------------------------------------------------------------------------
# Source 2: S3 scan
# ---------------------------------------------------------------------------


def scan_s3(
    bucket: str,
    prefix: str,
    endpoint_url: str = "",
    access_key: str = "",
    secret_key: str = "",
) -> tuple[list, str]:
    """List model directories in an S3-compatible bucket.

    A model is considered present when a ``config.json`` object exists
    underneath ``<prefix>/<org>/<Model>/``. Returns ``(models, error)`` where
    *error* is an empty string on success, so callers can surface why a scan
    contributed nothing instead of failing silently.
    """
    if not bucket:
        return [], "no bucket configured"
    try:
        import boto3
    except ImportError:
        log.warning("boto3 not installed; S3 scan skipped")
        return [], "boto3 not installed"

    kwargs: dict = {}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key

    try:
        s3 = boto3.client("s3", **kwargs)
    except Exception as e:
        log.error("Failed to create S3 client: %s", e)
        return [], f"client error: {e}"

    prefix_stripped = prefix.rstrip("/") + "/" if prefix else ""
    models: dict[str, DownloadedModel] = {}

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix_stripped):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith("config.json"):
                    continue
                relative = key[len(prefix_stripped):] if prefix_stripped else key
                parts = relative.split("/")
                # The S3 job uploads the repo's own files (flat — no models--
                # cache dirs) to <prefix>/<org>/<Model>/<file>. The model id is
                # therefore the two segments after the prefix; anything else
                # (stray files, wrong depth) is skipped rather than mislabeled.
                if len(parts) < 3 or not parts[0] or not parts[1]:
                    continue
                model_name = f"{parts[0]}/{parts[1]}"
                last_mod = obj.get("LastModified")
                mtime = last_mod.timestamp() if last_mod else 0.0
                location = f"s3://{bucket}/{key}"

                if model_name not in models or mtime > models[model_name].last_modified:
                    models[model_name] = DownloadedModel(
                        model_name=model_name,
                        backends=["s3"],
                        location=location,
                        last_modified=mtime,
                    )
        return list(models.values()), ""
    except Exception as e:
        log.error("S3 scan failed: %s", e)
        return [], f"list error: {e}"


# ---------------------------------------------------------------------------
# Source 3: PVC scan via a short-lived k8s Job
# ---------------------------------------------------------------------------


async def scan_pvc_via_job(
    k8s,
    namespace: str,
    pvc_name: str,
    scan_root: str,
    image: str,
    timeout: int = 60,
    mount_path: str = "/mnt/",
) -> list:
    """Scan a PVC for downloaded models by creating a short-lived k8s Job.

    The Job mounts the PVC read-only at ``mount_path`` and lists model
    directories (under ``scan_root``) that contain a ``config.json`` file.
    Results are returned as ``model_name<TAB>mtime`` lines read from the Job's
    logs.
    """
    import uuid

    if not pvc_name or not scan_root or not image:
        return [], "not configured (pvc or pvcScanImage missing)"

    job_id = uuid.uuid4().hex[:8]

    try:
        job_name = await k8s.create_scan_job(
            namespace=namespace,
            pvc_name=pvc_name,
            scan_root=scan_root,
            image=image,
            job_id=job_id,
            mount_path=mount_path,
        )
    except Exception as e:
        log.error("Failed to create scan job: %s", e)
        return [], f"create failed: {e}"

    try:
        status, output = await k8s.get_scan_results(namespace, job_name, timeout=timeout)
    except Exception as e:
        log.error("Failed to get scan results: %s", e)
        return [], f"status check failed: {e}"
    # NOTE: the Job is intentionally NOT deleted here on empty/failed scans —
    # ttlSecondsAfterFinished keeps the pod around briefly so its logs remain
    # inspectable. Successful scans are deleted after parsing (below).

    if status != "complete":
        reason = {"timeout": f"timed out after {timeout}s", "failed": "scan job failed"}.get(status, status)
        detail = (output or "").strip().splitlines()[-1] if (output or "").strip() else ""
        # Keep the Job (TTL cleans it up) so `kubectl logs` still works for debugging.
        return [], reason + (f": {detail}" if detail else "")

    sub = scan_root.lstrip("/")
    if sub.startswith("mnt/"):
        sub = sub[len("mnt/"):]
    models = []
    for line in (output or "").strip().splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        model_name = parts[0].strip()
        try:
            mtime = float(parts[1])
        except ValueError:
            mtime = 0.0
        # Third field (when present): absolute path of the models-- cache dir
        # — the precise on-disk location, which may differ from
        # <scan_root>/<model> when the user chose a custom cache root.
        cachepath = parts[2].strip() if len(parts) > 2 else ""
        if cachepath:
            rel = cachepath.lstrip("/")
            if rel.startswith("mnt/"):
                rel = rel[len("mnt/"):]
            location = f"pvc://{pvc_name}/{rel}"
        else:
            location = f"pvc://{pvc_name}/{sub}/{model_name}"
        if model_name:
            models.append(
                DownloadedModel(
                    model_name=model_name,
                    backends=["pvc"],
                    location=location,
                    last_modified=mtime,
                )
            )
    # Evidence + diagnosability: a completed scan with zero lines is the one
    # case that used to be indistinguishable from a broken read. Log it, and
    # KEEP the Job (ttlSecondsAfterFinished cleans it up) so its pod logs
    # remain inspectable with `kubectl logs`.
    if not models:
        preview = (output or "")[:200].replace("\n", " | ")
        log.warning(
            "PVC scan %s completed with no model lines (raw output %d bytes)%s",
            job_name, len(output or ""), f" — preview: {preview!r}" if preview else "",
        )
    else:
        log.info("PVC scan %s: %d model(s)", job_name, len(models))
        try:
            await k8s.delete_job(namespace, job_name)
        except Exception:
            pass
    return models, ""


# ---------------------------------------------------------------------------
# Orchestration + time-based cache
# ---------------------------------------------------------------------------


class DownloadedModelsCache:
    """Cache for the "Downloaded models" listing, tuned to minimise calls.

    Job history is in-memory and always fresh. The S3 listing and the PVC
    scanner Job are cached server-side: automatic polls re-run them only after
    their refresh interval — and the PVC scan only when opted in
    (``pvc_enabled``). An explicit force (the "Rescan storage" button)
    refreshes on demand regardless, subject to a short debounce
    (_FORCE_MIN_INTERVAL), and concurrent callers share one in-flight scan
    instead of spawning duplicate work.
    """

    # Don't re-run a scan more often than this even if the client sends
    # ?force=1 repeatedly.
    _FORCE_MIN_INTERVAL: float = 10.0

    def __init__(self, pvc_refresh_interval: float = 60.0, s3_refresh_interval: float = 60.0):
        self.pvc_refresh_interval = pvc_refresh_interval
        self.s3_refresh_interval = s3_refresh_interval
        self._last_pvc_scan: float = 0.0
        self._last_s3_scan: float = 0.0
        self._pvc_models: list = []
        self._s3_models: list = []
        self._lock: Optional[asyncio.Lock] = None
        # Diagnostics from the most recent get_models() call, surfaced by the
        # API so the UI can explain an empty result instead of failing silently.
        self.last_status: dict = {}

    async def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _pvc_stale(self) -> bool:
        return (time.time() - self._last_pvc_scan) > self.pvc_refresh_interval

    def _s3_stale(self) -> bool:
        return (time.time() - self._last_s3_scan) > self.s3_refresh_interval

    def _pvc_force_allowed(self) -> bool:
        """True if enough time has passed since the last scan to honour a force."""
        return (time.time() - self._last_pvc_scan) > self._FORCE_MIN_INTERVAL

    def _s3_force_allowed(self) -> bool:
        return (time.time() - self._last_s3_scan) > self._FORCE_MIN_INTERVAL

    async def get_models(
        self,
        jobs: list,
        *,
        pvc_enabled: bool = False,
        s3_enabled: bool = True,
        pvc_namespace: str = "",
        pvc_name: str = "",
        pvc_scan_root: str = "",
        pvc_image: str = "",
        pvc_mount_path: str = "/mnt/",
        k8s=None,
        s3_bucket: str = "",
        s3_prefix: str = "",
        s3_endpoint: str = "",
        s3_access_key: str = "",
        s3_secret_key: str = "",
        force: bool = False,
    ) -> list:
        """Return a deduplicated list of downloaded models from all sources.

        Job history is always fresh (in-memory, zero calls). The S3 listing and
        the PVC scanner Job are cached: automatic polls refresh them only after
        their refresh interval, and the PVC scan only when opted in
        (``pvc_enabled``). An explicit *force* (the Rescan button) refreshes
        both on demand regardless, subject to the _FORCE_MIN_INTERVAL debounce;
        concurrent callers share one in-flight scan instead of spawning
        duplicate work.
        """
        lock = await self._get_lock()
        async with lock:
            # Per-source status for the UI, so "nothing appeared" is always
            # explainable: ran-and-found-0 vs skipped vs error.
            status: dict = {}

            # Job history is in-memory: always fresh, zero external calls.
            job_models = models_from_job_history(jobs)
            status["jobs"] = len(job_models)

            # S3 listing – cached. Automatic polls refresh only after the
            # interval (and only when s3_enabled); a force refreshes on demand,
            # debounced the same way as the PVC scan.
            if not (s3_bucket or s3_enabled or force):
                s3_status = "off"
            elif not s3_bucket:
                s3_status = "no bucket configured"
            elif self._s3_stale() or (force and self._s3_force_allowed()):
                self._s3_models, err = await asyncio.to_thread(
                    scan_s3,
                    s3_bucket,
                    s3_prefix,
                    s3_endpoint,
                    s3_access_key,
                    s3_secret_key,
                )
                self._last_s3_scan = time.time()
                s3_status = f"ok ({len(self._s3_models)} models)" if not err else f"error: {err}"
            else:
                s3_status = "cached (scanned recently)"
            status["s3"] = s3_status
            s3_models = self._s3_models

            # PVC scanner Job – the expensive source. Automatic polls scan only
            # when opted in (pvc_enabled); a force (the "Rescan storage" button)
            # scans on demand either way. Both paths share the single-flight
            # lock and the debounce, so rapid/concurrent requests reuse one Job.
            if not (pvc_enabled or force):
                pvc_status = (
                    f"cached ({len(self._pvc_models)} models — click Rescan storage to refresh)"
                    if self._pvc_models
                    else "off (click Rescan storage to scan now)"
                )
            elif not (pvc_name and pvc_scan_root and k8s and pvc_image):
                pvc_status = "not configured (pvc or pvcScanImage missing)"
            elif self._pvc_stale() or (force and self._pvc_force_allowed()):
                self._pvc_models, err = await scan_pvc_via_job(
                    k8s,
                    namespace=pvc_namespace,
                    pvc_name=pvc_name,
                    scan_root=pvc_scan_root,
                    image=pvc_image,
                    mount_path=pvc_mount_path,
                )
                self._last_pvc_scan = time.time()
                pvc_status = f"ok ({len(self._pvc_models)} models)" if not err else f"error: {err}"
            else:
                pvc_status = "cached (scanned recently)"
            status["pvc"] = pvc_status
            self.last_status = status

            return merge_models(job_models, s3_models, self._pvc_models)
