"""TTL/GC eviction for the shared model PVC (MD-C).

The GC Job (helm/templates/gc-cronjob.yaml — CronJob-compatible pod template — the exact
scan-job admission pattern: hpe-ezua/app: mlis, hpe-ezua/disable-sc,
runAsUser 0) does the destructive work on the PVC, because the app pod
cannot mount it. This module is the *report* side: it enumerates deletion
candidates from scanner/du output, applies the TTL/LRU line, cross-checks
the two protection signals the app can see (AIOLI packaged_models rows via
a strictly read-only SELECT, and live managed Jobs via their
cache-root/model-name annotations) and produces the dry-run report the UI
shows BEFORE the operator enables deletion (gc.dryRun defaults true).

The three-key AND rule (report + job template must agree — the job template
carries the same logic in its python pass):

    delete ONLY if manifest present AND no AIOLI row AND no live job

because each signal alone is unreliable:
  * ttlSecondsAfterFinished=3600 erases finished-job evidence — "no live
    job" alone would delete a model whose job merely aged out;
  * custom cache roots break exact path equality between AIOLI uris
    (pvc://models-pvc/large-models/<org>/<Model>) and on-disk paths — so the
    AIOLI check matches on the cache-dir name / repo id appearing in the
    uri, never exact paths;
  * MLIS rows may be stale (a model pushed but never served still protects;
    a row removed while the model still serves is the operator's call —
    the manifest key keeps a record so deletions are traceable).
"""

from __future__ import annotations

import fnmatch
import logging
import re
import time

log = logging.getLogger(__name__)

# models--<org>--<Repo> — org/repo segments are alnum, '.', '_', '-'; the
# separator between the two is the first '--' after the prefix (org names do
# not contain '--'; repo names may contain single '-').
_CACHE_DIR_RE = re.compile(r"^models--(.+)--(.+)$")

# Scanner verdict values that mean "scan says unsafe" — informational only
# for GC reports (GC never deletes because of a verdict; that is the A2
# gate's job at download time).
SUSPICIOUS_VERDICTS = {"suspicious", "critical", "high", "medium", "low"}


def repo_id_from_cache_dir(cache_dir: str) -> str:
    """``models--org--Repo`` -> ``org/Repo``; '' when the name doesn't match."""
    m = _CACHE_DIR_RE.match(cache_dir or "")
    if not m:
        return ""
    return f"{m.group(1)}/{m.group(2)}"


def cache_dir_name(repo_id: str) -> str:
    """``org/Repo`` -> ``models--org--Repo``."""
    return "models--" + (repo_id or "").replace("/", "--")


def uri_matches_cache_dir(uri: str, cache_dir: str) -> bool:
    """Does an AIOLI ``uri`` reference the model in *cache_dir*?

    Match is by NAME, never exact path (custom cache roots make paths
    unreliable):

    1. the literal ``models--<org>--<Repo>`` dir name appears in the uri
       (debug shells / custom-root pushes produce such uris);
    2. OR the repo id appears as a ``/<org>/<Repo>`` path segment (the seed
       catalog uri shape: ``pvc://models-pvc/large-models/<org>/<Model>?...``).
       The segment must end at ``/``, ``?`` or end-of-string so
       ``models--deepseek-ai--DeepSeek`` does not match a
       ``.../DeepSeek-V4`` uri.
    """
    if not uri or not cache_dir:
        return False
    if cache_dir in uri:
        return True
    m = _CACHE_DIR_RE.match(cache_dir)
    if not m:
        return False
    org, repo = m.group(1), m.group(2)
    return re.search(r"/" + re.escape(org) + r"/" + re.escape(repo) + r"(/|\?|$)", uri) is not None


def aioli_protected_dirs(uris: list[str], cache_dirs: list[str]) -> set[str]:
    """The subset of *cache_dirs* any AIOLI uri references."""
    protected: set[str] = set()
    for cd in cache_dirs:
        if any(uri_matches_cache_dir(uri, cd) for uri in uris):
            protected.add(cd)
    return protected


def live_job_protects(entry: dict, jobs: list[dict]) -> dict | None:
    """The first live job that pins this cache entry, or None.

    A job protects an entry when:
      * its cache_root annotation names the entry's parent dir (the job's
        resolved CACHE_ROOT — exact match while the job exists), OR
      * its model-name annotation decodes to the same repo id AND its own
        cache root would place the model there (default root
        ``<cache_root>/<cache id>``) — catches jobs whose annotation set is
        partially populated (e.g. reconciled S3->PVC mixed states).

    Any live managed job counts — downloader, debug shell, or scanner: a
    debug shell on the root is use. *status* is deliberately ignored: a
    Completed downloader job is deleted by TTL within the hour, but while it
    exists its pod may still be writing — never delete underneath it.
    """
    entry_parent = (entry.get("cachepath") or "").rsplit("/", 1)[0]
    entry_name = entry.get("model_name") or ""
    for job in jobs or []:
        root = (job.get("cache_root") or "").rstrip("/")
        if root and entry_parent and (entry_parent == root or entry_parent.startswith(root + "/")):
            return job
        if (
            entry_name
            and (job.get("model_name") or "") == entry_name
            # Name match alone is weaker (two namespaces can host the same
            # model); accept it only when the job has no cache root
            # annotation at all (older charts) — otherwise the root match
            # above is the decider.
            and not root
        ):
            return job
    return None


def is_expired(entry: dict, *, ttl_days: float | None, now: float | None = None) -> bool:
    """TTL check: cache dir mtime older than ttl_days.

    ``None`` (no TTL line configured) and ``<= 0`` (explicitly disabled)
    both mean "never expires" — the report side must agree with the job
    template, where ttlDays=0 disables the TTL line.
    """
    if not ttl_days or ttl_days <= 0:
        return False
    now = now if now is not None else time.time()
    mtime = entry.get("mtime") or 0.0
    if not mtime:
        # An unparseable mtime must never look expired.
        return False
    return (now - mtime) > ttl_days * 86400.0


def glob_protected(patterns: list[str], name: str) -> bool:
    """True when *name* (model id or cache-dir name) matches a protect glob."""
    return any(fnmatch.fnmatch(name, pat) for pat in patterns or [])


def build_gc_plan(
    entries: list[dict],
    *,
    ttl_days: float | None,
    aioli_uris: list[str],
    live_jobs: list[dict],
    protected_models: list[str] | None = None,
    min_keep: int = 0,
    now: float | None = None,
) -> dict:
    """The full candidate evaluation: report rows + summary counters.

    *entries* — parsed scanner rows: model_name, cachepath, mtime, bytes,
    manifest (bool|None), scanned (str).
    *aioli_uris* — every packaged_models.uri (read-only SELECT result).
    *live_jobs* — annotation rows from list_managed_jobs_by_annotation().
    *protected_models* — value globs (gc.protectedModels) matched against
    the model id or cache-dir name.
    *min_keep* — LRU floor: never delete if fewer than this many cache dirs
    would remain (0 = no LRU line).

    Every row carries the exact three-key verdict so the dry-run report can
    show WHY each dir is kept or would be deleted. Deletion itself happens
    in the GC Job; the app only ever reports.
    """
    now = now if now is not None else time.time()
    cache_dirs = [e.get("cachepath", "").rsplit("/", 1)[-1] for e in entries]
    aioli_hits = aioli_protected_dirs(aioli_uris, cache_dirs)
    protected_globs = list(protected_models or [])

    rows: list[dict] = []
    delete_count = keep_count = 0
    delete_bytes = 0
    for e in entries:
        name = e.get("model_name") or ""
        cachepath = e.get("cachepath") or ""
        cd = cachepath.rsplit("/", 1)[-1] if cachepath else ""
        manifest_present = e.get("manifest")
        aioli_hit = cd in aioli_hits if cd else False
        job = live_job_protects(e, live_jobs)
        glob_hit = glob_protected(protected_globs, name) or (cd and glob_protected(protected_globs, cd))
        expired = is_expired(e, ttl_days=ttl_days, now=now)

        blocks: list[str] = []
        if not manifest_present:
            blocks.append("no manifest")
        if aioli_hit:
            blocks.append("in AIOLI packaged_models")
        if job:
            blocks.append(f"live job {job.get('namespace', '')}/{job.get('job_name', '')}")
        if glob_hit:
            blocks.append("matches protectedModels glob")
        if expired and not blocks:
            rows.append(
                {
                    "model_name": name,
                    "cachepath": cachepath,
                    "bytes": e.get("bytes"),
                    "mtime": e.get("mtime"),
                    "age_days": round((now - (e.get("mtime") or now)) / 86400.0, 1),
                    "manifest": manifest_present,
                    "scanned": e.get("scanned") or "",
                    "action": "delete",
                    "reason": f"TTL {ttl_days}d expired; manifest present; no AIOLI row; no live job",
                }
            )
            delete_count += 1
            if e.get("bytes"):
                delete_bytes += int(e["bytes"])
        else:
            reason = "; ".join(blocks) if blocks else ("no TTL line configured" if not ttl_days else "below TTL line")
            rows.append(
                {
                    "model_name": name,
                    "cachepath": cachepath,
                    "bytes": e.get("bytes"),
                    "mtime": e.get("mtime"),
                    "age_days": round((now - (e.get("mtime") or now)) / 86400.0, 1) if e.get("mtime") else None,
                    "manifest": manifest_present,
                    "scanned": e.get("scanned") or "",
                    "action": "keep",
                    "reason": reason,
                }
            )
            keep_count += 1

    # LRU floor: when a min_keep line is configured, protect the newest
    # min_keep delete-candidates (largest mtime first).
    if min_keep > 0 and delete_count > 0:
        delete_rows = [r for r in rows if r["action"] == "delete"]
        delete_rows.sort(key=lambda r: r.get("mtime") or 0.0, reverse=True)
        keep_set = {r["cachepath"] for r in delete_rows[:min_keep]}
        for r in rows:
            if r["action"] == "delete" and r["cachepath"] in keep_set:
                r["action"] = "keep"
                r["reason"] = f"LRU min_keep={min_keep} (newest kept)"
                delete_count -= 1
                keep_count += 1
                if r.get("bytes"):
                    delete_bytes -= int(r["bytes"])

    return {
        "generated_at": now,
        "ttl_days": ttl_days,
        "rows": rows,
        "summary": {
            "total": len(rows),
            "delete": delete_count,
            "keep": keep_count,
            "delete_bytes": delete_bytes,
            "aioli_protected": len(aioli_hits),
            "live_job_protected": sum(1 for r in rows if "live job" in (r.get("reason") or "")),
            "glob_protected": sum(1 for r in rows if "protectedModels" in (r.get("reason") or "")),
            "no_manifest": sum(1 for r in rows if "no manifest" in (r.get("reason") or "")),
        },
    }


def parse_aioli_rows(rows: list[tuple] | list[dict]) -> list[str]:
    """Flatten a read-only packaged_models SELECT into a uri list.

    Accepts either tuples ``(name, uri)`` or dicts with a ``uri`` key;
    tolerates NULL uris. The SELECT is built by the caller (db.py) with no
    write capability anywhere on the connection.
    """
    uris: list[str] = []
    for row in rows or []:
        if isinstance(row, dict):
            uri = row.get("uri")
        elif isinstance(row, (tuple, list)) and len(row) >= 2:
            uri = row[1]
        else:
            uri = None
        if isinstance(uri, str) and uri.strip():
            uris.append(uri.strip())
    return uris
