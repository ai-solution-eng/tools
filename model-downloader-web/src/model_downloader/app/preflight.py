"""PVC preflight + per-namespace quota (MD-B).

The app pod never mounts the models PVC, so the two numbers a submit-time
check needs come from different places:

* **capacity** — the PVC's ``status.capacity`` via the k8s API (a read-only
  ``persistentvolumeclaims`` get the ClusterRole grants, chart >= 1.7);
* **used bytes** — the ``du -sb`` pass the PVC scanner Job runs as root on
  the PVC (chart >= 1.7 emits a 5th column with per-cache-dir bytes).

Both numbers are cached with a TTL (the DownloadedModelsCache pattern):
the scanner Job is the expensive source and preflight must not spawn a
second scan per submit. Stale bytes err on the *permissive* side only for
deletions — for submissions a stale "used" can under-count, so preflight
also adds the *pending* bytes of in-flight downloads (running Jobs) on the
same namespace before comparing against free space.

Quota model: ``quota.<namespace>`` bytes map + ``quota.default`` in values.
Usage per namespace is aggregated from the scanner's per-cache-dir bytes
plus the annotation/manifest attribution (custom cache roots sum by owning
namespace, not by fixed root). Enforcement is submit-time, and
``quota.mode`` decides warn-vs-refuse so users can't route around a soft
quota unannounced.

Pure functions here are unit-tested directly; k8s/scanner I/O is injected.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
import time

log = logging.getLogger(__name__)

# HF cache dir name -> repo id: models--<org>--<Repo> (the scanner's first
# column already carries the decoded "org/Repo"; this mirrors it for the
# annotation/manifest attribution paths, which only know the dir name).
_CACHE_DIR_RE = re.compile(r"^models--(.+)--(.+)$")

# Default: a size estimate older than this is treated as unknown.
DEFAULT_SIZE_TTL_SECONDS = 300.0
# Default: storage census (used bytes + per-namespace usage) older than this
# is refreshed (a scanner Job) before enforcing a refusal.
DEFAULT_USAGE_TTL_SECONDS = 120.0


def parse_k8s_quantity(text: str | None) -> int | None:
    """Parse a Kubernetes resource quantity ('500Gi', '1Ti', '262144') to bytes.

    Uses kubernetes.utils.quantity (already a hard app dependency) and
    returns None for anything unparseable so callers can skip rather than
    misreport capacity as 0 (which would refuse every submission).
    """
    if not text:
        return None
    try:
        from kubernetes.utils.quantity import parse_quantity

        return int(parse_quantity(text))
    except Exception:
        log.warning("unparseable k8s quantity %r", text)
        return None


def cache_dir_name(repo_id: str) -> str:
    """``org/Repo`` -> ``models--org--Repo`` (the HF cache dir convention,
    identical to CACHE_ID in the downloader job template)."""
    return "models--" + repo_id.replace("/", "--")


def repo_id_from_cache_dir(cache_dir: str) -> str:
    """``models--org--Repo`` -> ``org/Repo``; '' when the name doesn't match."""
    m = _CACHE_DIR_RE.match(cache_dir or "")
    if not m:
        return ""
    return f"{m.group(1)}/{m.group(2)}"


class PreflightError(Exception):
    """A submission was refused by preflight/quota.

    Carries the human-readable reason (the 4xx detail the UI shows verbatim)
    plus a machine-readable ``code`` for tests/callers.
    """

    def __init__(self, detail: str, code: str = "preflight_refused"):
        super().__init__(detail)
        self.detail = detail
        self.code = code


class QuotaExceededError(PreflightError):
    def __init__(self, detail: str):
        super().__init__(detail, code="quota_exceeded")


class PreflightConfig:
    """Immutable-ish view of the quota/preflight settings (from env)."""

    def __init__(
        self,
        enabled: bool = True,
        mode: str = "refuse",  # refuse | warn | off
        quota_default_bytes: int | None = None,
        quota_namespaces: dict[str, int] | None = None,
        quota_mode: str = "refuse",  # refuse | warn | off
        safety_margin_bytes: int = 0,
    ):
        self.enabled = enabled
        self.mode = mode
        self.quota_default_bytes = quota_default_bytes
        self.quota_namespaces = quota_namespaces or {}
        self.quota_mode = quota_mode
        self.safety_margin_bytes = safety_margin_bytes

    def quota_for(self, namespace: str) -> int | None:
        """Per-namespace byte quota; quota.<ns> overrides quota.default."""
        return self.quota_namespaces.get(namespace, self.quota_default_bytes)


# ---------------------------------------------------------------------------
# Pure preflight math (unit-tested directly, no I/O)
# ---------------------------------------------------------------------------


def decide_disk_preflight(
    *,
    estimate_bytes: int | None,
    capacity_bytes: int | None,
    used_bytes: int | None,
    pending_bytes: int = 0,
    safety_margin_bytes: int = 0,
) -> tuple[str, str]:
    """Disk-space decision: ('ok'|'refuse'|'warn', reason).

    Refusal needs *both* a known estimate and known free space — anything
    unknown degrades to warn-only (or plain ok when there is nothing to warn
    about). Refused reasons name bytes-needed vs bytes-free, the detail the
    submit endpoint returns verbatim.
    """
    if estimate_bytes is None:
        return "ok", "model size unknown — no disk preflight possible (download proceeds)"
    if capacity_bytes is None or used_bytes is None:
        return (
            "warn",
            f"model size known ({estimate_bytes} bytes) but PVC capacity/usage unknown — preflight skipped",
        )
    free = capacity_bytes - used_bytes - pending_bytes - safety_margin_bytes
    if estimate_bytes > free:
        margin_txt = f" - margin {safety_margin_bytes}" if safety_margin_bytes else ""
        return (
            "refuse",
            (
                f"insufficient PVC space: model needs {estimate_bytes} bytes, only {max(free, 0)} bytes free "
                f"(capacity {capacity_bytes} - used {used_bytes} - pending {pending_bytes}{margin_txt}). "
                "Delete unused models or contact your admin."
            ),
        )
    return "ok", f"preflight ok: {estimate_bytes} bytes needed, {free} bytes free"


def decide_quota(
    *,
    namespace: str,
    estimate_bytes: int | None,
    usage_bytes: int | None,
    quota_bytes: int | None,
) -> tuple[str, str]:
    """Quota decision: ('ok'|'refuse'|'warn'|'off', reason).

    ``off`` = no quota configured for this namespace. Refusal names
    bytes-used + bytes-requested vs the configured limit.
    """
    if quota_bytes is None:
        return "off", "no quota configured"
    if usage_bytes is None:
        return (
            "warn",
            f"quota {quota_bytes} bytes configured but current usage unknown — quota check skipped",
        )
    requested = estimate_bytes or 0
    projected = usage_bytes + requested
    if projected > quota_bytes:
        return (
            "refuse",
            f"namespace quota exceeded: {usage_bytes} bytes used + {requested} requested > {quota_bytes} quota for namespace '{namespace}'",
        )
    return "ok", f"quota ok: {projected} of {quota_bytes} bytes projected for '{namespace}'"


def usage_by_namespace(
    scan_entries: list[dict],
    *,
    live_jobs: list[dict],
    default_root_prefix: str = "/mnt/large-models",
) -> dict[str, int]:
    """Aggregate per-namespace usage (bytes) from scanner output + attribution.

    *scan_entries* are parsed scanner rows: dicts with ``cachepath``
    (absolute path of the models-- dir), ``bytes`` (du total), and
    ``model_name``. Attribution, most-reliable-wins:

    1. **Live managed jobs** — a Job whose ``cache_root`` annotation matches
       the entry's cachepath (exact, while the Job exists) attributes the
       entry to the Job's namespace. Same ``model_name`` + different root
       still attributes by root, so a second namespace's copy is distinct.
    2. **Job history by model+root** — same matching against in-memory
       finished jobs (caller merges those into *live_jobs*).
    3. **Default-root fallback** — a path under the default root prefix
       ``/mnt/large-models/<model>`` belongs to whichever namespace's job
       history claims that model name (best-effort; scanner output alone
       cannot know the namespace, so the caller's job list decides).

    Everything unattributable (path outside any known root, no matching job)
    is summed under the ``"_unattributed"`` key — reported, never silently
    dropped, and counted against no namespace's quota.
    """
    usage: dict[str, int] = {}
    # Index: exact cache-root -> namespace, and model_name -> namespace(s).
    root_to_ns: dict[str, str] = {}
    model_to_ns: dict[str, str] = {}
    for job in live_jobs or []:
        ns = job.get("namespace") or ""
        if not ns:
            continue
        root = (job.get("cache_root") or "").rstrip("/")
        if root:
            # First writer wins; two namespaces sharing one custom root is a
            # conflict the three-key GC rule already keeps destructive.
            root_to_ns.setdefault(root, ns)
        name = job.get("model_name") or ""
        if name:
            model_to_ns.setdefault(name, ns)

    for entry in scan_entries or []:
        raw = entry.get("bytes")
        if raw is None:
            continue
        try:
            nbytes = int(raw)
        except (TypeError, ValueError):
            continue
        cachepath = (entry.get("cachepath") or "").rstrip("/")
        ns = root_to_ns.get(cachepath)
        if ns is None:
            # cache dir conventionally sits under <cache_root>/<cache-id>;
            # also try the parent of the models-- dir as the job's root.
            parent = cachepath.rsplit("/", 1)[0] if "/" in cachepath else ""
            ns = root_to_ns.get(parent)
        if ns is None:
            model = entry.get("model_name") or ""
            ns = model_to_ns.get(model)
            if ns is None and cachepath.startswith(default_root_prefix):
                # Default-root path with no job evidence: attribute to the
                # namespace that the caller's job history last claimed the
                # model under — else unattributed.
                ns = "_unattributed"
        if ns is None:
            ns = "_unattributed"
        usage[ns] = usage.get(ns, 0) + nbytes
    return usage


# ---------------------------------------------------------------------------
# Hub size estimate (best effort)
# ---------------------------------------------------------------------------


def fetch_model_size_sync(repo_id: str, *, verify_tls: bool = True, timeout: int = 20) -> int | None:
    """Total advertised size (bytes) of a HF repo via the Hub REST API.

    ``GET https://huggingface.co/api/models/<repo>?blobs=true`` — siblings[]
    carry per-file ``size``. Returns None on any failure (the preflight then
    degrades to warn-only). Runs through urllib so the pod's proxy env applies
    exactly like the catalog GitHub fetch; TLS follows the same verify flag.
    """
    import json as _json
    import ssl as _ssl
    import urllib.request as _ur

    url = f"https://huggingface.co/api/models/{repo_id}?blobs=true"
    try:
        if verify_tls:
            with _ur.urlopen(url, timeout=timeout) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
        else:
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            with _ur.urlopen(url, timeout=timeout, context=ctx) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("model size fetch failed for %s: %s", repo_id, e)
        return None
    total = 0
    seen_any = False
    for sib in data.get("siblings") or []:
        size = sib.get("size")
        if size is None:
            continue
        try:
            total += int(size)
            seen_any = True
        except (TypeError, ValueError):
            continue
    return total if seen_any else None


# ---------------------------------------------------------------------------
# Cached preflight service (app-side)
# ---------------------------------------------------------------------------


class PreflightService:
    """Caches capacity + used-bytes + per-namespace usage, TTL'd separately.

    Single-flight like DownloadedModelsCache: concurrent submits share one
    refresh. ``record_usage_delta`` folds finished/started downloads into the
    cached usage immediately (the next scanner refresh re-derives it), so a
    burst of submissions cannot slip past the quota between scans.
    """

    def __init__(
        self,
        config: PreflightConfig,
        k8s=None,
        *,
        usage_ttl: float = DEFAULT_USAGE_TTL_SECONDS,
        size_ttl: float = DEFAULT_SIZE_TTL_SECONDS,
        scan_image: str = "",
    ):
        self.config = config
        self.k8s = k8s
        self.usage_ttl = usage_ttl
        self.size_ttl = size_ttl
        # The scanner image the usage census reuses (same short-lived Job
        # plumbing as the downloaded-models listing); set at wiring time.
        self.scan_image = scan_image
        self._capacity: int | None = None
        self._capacity_at: float = 0.0
        self._used: int | None = None
        self._used_at: float = 0.0
        self._usage_by_ns: dict[str, int] = {}
        self._usage_at: float = 0.0
        # repo -> (size, fetched_at)
        self._sizes: dict[str, tuple[int, float]] = {}
        self._pending: dict[str, int] = {}  # namespace -> in-flight bytes
        self._delta: dict[str, int] = {}  # namespace -> usage drift since last scan
        self._lock: asyncio.Lock | None = None

    async def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ---- capacity -------------------------------------------------------

    async def get_capacity(self, namespace: str, pvc_name: str) -> int | None:
        """PVC status.capacity in bytes, TTL-cached. None = unknown."""
        if time.time() - self._capacity_at <= self.usage_ttl and self._capacity is not None:
            return self._capacity
        if self.k8s is None:
            return self._capacity
        try:
            cap = await self.k8s.read_pvc_capacity(namespace, pvc_name)
        except Exception as e:
            log.warning("PVC capacity read failed (%s/%s): %s", namespace, pvc_name, e)
            cap = None
        if cap is not None:
            self._capacity = cap
            self._capacity_at = time.time()
        return self._capacity

    # ---- used bytes / per-namespace usage ---------------------------------

    async def get_usage(
        self,
        *,
        namespace: str,
        pvc_name: str,
        scan_root: str,
        scan_image: str,
        mount_path: str = "/mnt/",
        jobs: list | None = None,
        force: bool = False,
    ) -> tuple[int | None, dict[str, int]]:
        """(total used bytes on the scan root, usage per namespace).

        Fresh only when older than the TTL or when a caller forces; otherwise
        cached. Jobs (live + finished history) drive attribution per
        usage_by_namespace(); the per-namespace delta of submissions started
        since the last scan is added on top (see record_usage_delta).
        """
        lock = await self._get_lock()
        async with lock:
            stale = (time.time() - self._usage_at) > self.usage_ttl
            if force or stale or not self._usage_at:
                entries = await self._run_usage_scan(
                    namespace=namespace,
                    pvc_name=pvc_name,
                    scan_root=scan_root,
                    scan_image=scan_image,
                    mount_path=mount_path,
                )
                if entries is not None:
                    self._usage_by_ns = usage_by_namespace(
                        entries, live_jobs=jobs or [], default_root_prefix=self._default_root(scan_root)
                    )
                    self._used = sum(self._usage_by_ns.values())
                    self._usage_at = time.time()
                    self._delta = {}
            merged = dict(self._usage_by_ns)
            drift = self._delta.get(namespace, 0)
            if drift:
                merged[namespace] = merged.get(namespace, 0) + drift
            return self._used, merged

    def _default_root(self, scan_root: str) -> str:
        return scan_root.rstrip("/")

    async def _run_usage_scan(self, *, namespace, pvc_name, scan_root, scan_image, mount_path):
        """Run the du-enabled scanner Job and return parsed entries, or None.

        Delegates to the storage layer's scanner plumbing; a failed scan
        returns None (caller keeps serving the last-known numbers rather than
        zeroing usage — that would silently un-charge a namespace).
        """
        if self.k8s is None or not pvc_name or not scan_image:
            return None
        from .storage import scan_pvc_via_job

        try:
            models, err = await scan_pvc_via_job(
                self.k8s,
                namespace=namespace,
                pvc_name=pvc_name,
                scan_root=scan_root,
                image=scan_image,
                timeout=120,
                mount_path=mount_path,
            )
        except Exception as e:
            log.warning("usage scan failed: %s", e)
            return None
        if err:
            log.warning("usage scan error: %s", err)
            return None
        # DownloadedModel carries everything the aggregation needs (location
        # is the pvc:// form of cachepath; use its cachepath field when set).
        entries: list[dict] = []
        for m in models:
            entries.append(
                {
                    "model_name": m.model_name,
                    "cachepath": getattr(m, "cachepath", "") or _pvc_location_to_path(m.location),
                    "bytes": getattr(m, "bytes", None),
                }
            )
        return entries

    # ---- size estimates ---------------------------------------------------

    async def get_model_size(self, repo_id: str, *, verify_tls: bool = True) -> int | None:
        """Cached Hub size estimate; None = unknown (warn-only preflight)."""
        cached = self._sizes.get(repo_id)
        if cached and (time.time() - cached[1]) <= self.size_ttl:
            return cached[0]
        size = await asyncio.to_thread(fetch_model_size_sync, repo_id, verify_tls=verify_tls)
        if size is not None:
            self._sizes[repo_id] = (size, time.time())
        return size

    # ---- bookkeeping between scans -----------------------------------------

    def record_usage_delta(self, namespace: str, bytes_delta: int) -> None:
        """Fold a submission's estimated bytes into cached usage immediately.

        Positive when a download starts (charged), negative when a GC run or
        deletion frees space. Persisted across the next scanner refresh,
        which re-derives usage from disk and resets the delta.
        """
        if bytes_delta:
            self._delta[namespace] = self._delta.get(namespace, 0) + bytes_delta

    def record_pending(self, namespace: str, bytes_delta: int) -> None:
        """Track in-flight estimated bytes for the disk-preflight math."""
        if bytes_delta:
            self._pending[namespace] = self._pending.get(namespace, 0) + bytes_delta

    def pending_for(self, namespace: str) -> int:
        return self._pending.get(namespace, 0)

    # ---- the submit-time gate ----------------------------------------------

    async def check(
        self,
        *,
        namespace: str,
        repo_id: str,
        estimate_bytes: int | None,
        pvc_name: str,
        scan_root: str,
        scan_image: str,
        mount_path: str = "/mnt/",
        jobs: list | None = None,
        verify_tls: bool = True,
    ) -> dict:
        """Run preflight + quota for one submission. Returns a report dict.

        Raises PreflightError (disk) or QuotaExceededError (quota) in refuse
        mode; returns the decision details either way otherwise. The report
        shape is stable and tested: {disk: {decision, reason, ...},
        quota: {decision, reason, ...}, estimate_bytes, capacity_bytes,
        used_bytes}.
        """
        cfg = self.config
        report: dict = {
            "estimate_bytes": estimate_bytes,
            "capacity_bytes": None,
            "used_bytes": None,
            "disk": {"decision": "off", "reason": "preflight disabled"},
            "quota": {"decision": "off", "reason": "quota disabled"},
        }
        if not cfg.enabled or cfg.mode == "off":
            return report

        if estimate_bytes is None:
            estimate_bytes = await self.get_model_size(repo_id, verify_tls=verify_tls)
            report["estimate_bytes"] = estimate_bytes

        capacity = await self.get_capacity(namespace, pvc_name)
        used, usage = await self.get_usage(
            namespace=namespace,
            pvc_name=pvc_name,
            scan_root=scan_root,
            scan_image=scan_image,
            mount_path=mount_path,
            jobs=jobs,
        )
        report["capacity_bytes"] = capacity
        report["used_bytes"] = used

        disk_decision, disk_reason = decide_disk_preflight(
            estimate_bytes=estimate_bytes,
            capacity_bytes=capacity,
            used_bytes=used,
            pending_bytes=self.pending_for(namespace),
            safety_margin_bytes=cfg.safety_margin_bytes,
        )
        report["disk"] = {"decision": disk_decision, "reason": disk_reason}
        if disk_decision == "refuse" and cfg.mode == "refuse":
            raise PreflightError(disk_reason)

        quota_bytes = cfg.quota_for(namespace)
        q_decision, q_reason = decide_quota(
            namespace=namespace,
            estimate_bytes=estimate_bytes,
            usage_bytes=usage.get(namespace),
            quota_bytes=quota_bytes,
        )
        report["quota"] = {"decision": q_decision, "reason": q_reason, "quota_bytes": quota_bytes}
        if q_decision == "refuse" and cfg.quota_mode == "refuse":
            raise QuotaExceededError(q_reason)
        return report


def _pvc_location_to_path(location: str) -> str:
    """``pvc://models-pvc/large-models/x`` -> ``/large-models/x``.

    The scanner's cachepath column is the authoritative absolute path; this
    is the fallback for older scanners (which carry location only).
    """
    m = re.match(r"^pvc://[^/]+(/.*)$", location or "")
    if not m:
        return ""
    path = m.group(1)
    return path if path.startswith("/") else "/" + path


def glob_protected(patterns: list[str], name: str) -> bool:
    """True when *name* matches any glob *pattern* (fnmatch semantics).

    Used by GC's protected list; lives here so both the GC job template and
    the report side share one tested implementation.
    """
    return any(fnmatch.fnmatch(name, pat) for pat in patterns or [])


def human_bytes(n: int | None) -> str:
    """Human-readable byte count for UI/refusal messages."""
    if n is None:
        return "unknown"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"
