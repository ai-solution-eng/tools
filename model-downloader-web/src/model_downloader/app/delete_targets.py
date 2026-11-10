"""In-pod execution of an admin's manual deletion.

Runs inside a Job created by the app: python3 -m app.delete_targets, from the
image's WORKDIR /app, so the package is `app`.

This module is deliberately as simple as a download Job. It mounts the model
PVC, validates the paths it was given structurally, writes a tombstone, and
removes them. It makes NO Kubernetes API calls and NO database calls — which is
what lets the Job run in the namespace that owns the PVC with the default
ServiceAccount, exactly like the download and scan Jobs.

The protection checks (AIOLI registration, live Jobs, protectedModels globs)
happen in the app at preview time AND again at commit time, immediately before
this Job is created. That is the same trust model as the download path: the app
decides, the pod executes. Do not add cluster-API calls back here — doing so
requires a ServiceAccount that does not exist in the PVC's namespace, and the
pod then sits Pending forever with "serviceaccount not found".

The isolation rule is the reason this file exists in this shape: it is handed an
exact list of directories and deletes exactly those, after validating each one.
It never walks the tree, never globs, never expands a pattern, and never builds
a path from the payload.

Exit codes:

* 0 — every named target was deleted (or had already vanished and was recorded);
* 1 — bad payload, a structural validation failure (nothing deleted), or a
  delete that failed. A fully-refused run is non-zero on purpose so the Job
  shows Failed.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import time

from .gc import repo_id_from_cache_dir

CACHE_PREFIX = "models--"
DELETED_DIR = "_deleted"


def _err(message: str) -> None:
    print(f"MD-DELETE ERROR: {message}", file=sys.stderr, flush=True)


def _out(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------------------
# payload + structural validation
# ---------------------------------------------------------------------------


def decode_targets(raw: str) -> list[dict]:
    """Decode MD_TARGETS_B64 into [{"cachepath", "override", "verdict"}].

    Duplicates are collapsed: the same directory named twice must not be
    deleted twice (the second rmtree would fail and pollute the audit trail).
    """
    if not raw:
        raise ValueError("MD_TARGETS_B64 is empty")
    payload = json.loads(base64.b64decode(raw.encode("ascii")).decode("utf-8"))
    if not isinstance(payload, list):
        raise TypeError("targets payload is not a list")
    out: list[dict] = []
    seen: set[str] = set()
    for item in payload:
        if isinstance(item, str):
            cachepath, override, verdict = item, False, "deletable"
        elif isinstance(item, dict):
            cachepath = item.get("cachepath") or ""
            override = bool(item.get("override"))
            verdict = str(item.get("verdict") or "deletable")
        else:
            raise TypeError(f"target entry is neither a string nor an object: {item!r}")
        cachepath = str(cachepath)
        if not cachepath:
            raise ValueError("target entry has an empty cachepath")
        if cachepath in seen:
            continue
        seen.add(cachepath)
        out.append({"cachepath": cachepath, "override": override, "verdict": verdict})
    if not out:
        raise ValueError("targets payload is empty")
    return out


def validate_target(path: str, scan_root: str) -> str:
    """Return "" when *path* is safe to delete, else the reason it is not.

    Everything here is a refusal:

    * must be absolute;
    * must not be a symlink (rmtree on a link's target would escape the root);
    * must be a directory;
    * its basename must be a models--<org>--<repo> cache dir;
    * it must not be the scan root itself;
    * its REAL path must sit strictly under the scan root.
    """
    if not path:
        return "empty path"
    if not os.path.isabs(path):
        return "not an absolute path"
    if os.path.islink(path):
        return "is a symlink"
    if not os.path.isdir(path):
        return "is not a directory"
    base = os.path.basename(path.rstrip("/"))
    if not base.startswith(CACHE_PREFIX) or base.count("--") < 2:
        return f"basename {base!r} is not a models--<org>--<repo> cache dir"
    real_root = os.path.realpath(scan_root).rstrip("/")
    real_path = os.path.realpath(path).rstrip("/")
    if real_path == real_root:
        return "is the scan root itself"
    if not real_path.startswith(real_root + "/"):
        return f"resolves outside the scan root ({real_root})"
    return ""


def build_entries(targets: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Scanner-shaped rows for the tombstone, from the real directories.

    Returns (entries, vanished). A target that passed validation but cannot be
    stat'd any more (something removed it in between) is reported in *vanished*
    rather than dropped: every named target must end up either deleted or
    explicitly skipped, or the audit trail lies by omission.
    """
    entries: list[dict] = []
    vanished: list[tuple[str, str]] = []
    for target in targets:
        path = target["cachepath"]
        base = os.path.basename(path.rstrip("/"))
        try:
            stat = os.stat(path)
        except OSError as e:
            vanished.append((path, f"disappeared after validation ({e})"))
            continue
        manifest_path = os.path.join(path, "manifest.json")
        entries.append(
            {
                "model_name": repo_id_from_cache_dir(base) or base,
                "cachepath": path,
                "mtime": stat.st_mtime,
                "bytes": None,
                "manifest": os.path.isfile(manifest_path),
                "scanned": _scanned_verdict(manifest_path),
                "override": bool(target.get("override")),
                "verdict": target.get("verdict") or "deletable",
            }
        )
    return entries, vanished


def _scanned_verdict(manifest_path: str) -> str:
    try:
        with open(manifest_path) as fh:
            data = json.load(fh)
    except Exception:
        return ""
    scan = data.get("scan") if isinstance(data, dict) else None
    return str((scan or {}).get("verdict", "")) if isinstance(scan, dict) else ""


# ---------------------------------------------------------------------------
# audit record
# ---------------------------------------------------------------------------


def tombstone(row: dict, *, actor: str, job: str, override: bool, verdict: str = "deletable") -> dict:
    """The audit record, written BEFORE the directory disappears.

    The provenance manifest lives inside the cache dir, so deleting the dir
    destroys the only ownership record. Copying it here is what keeps the
    deletion traceable at all.
    """
    path = row["cachepath"]
    manifest = None
    manifest_path = os.path.join(path, "manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path) as fh:
                manifest = json.load(fh)
        except Exception:
            manifest = None
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": actor,
        "job": job,
        "path": path,
        "model": row.get("model_name") or "",
        "bytes": row.get("bytes"),
        "verdict": verdict,
        "override": bool(override),
        "manifest": manifest,
    }


def write_tombstone_file(scan_root: str, payload: dict) -> None:
    """Best-effort durable copy under <scan_root>/_deleted/.

    _deleted/ does not match the scanner's models--*--* glob and holds only
    .json files, so it is invisible to both the listing and the GC pass.
    A failure here is logged and swallowed: the stdout line is the primary
    record and the Job's logs outlive the deletion (jobTtlSeconds).
    """
    try:
        directory = os.path.join(scan_root, DELETED_DIR)
        os.makedirs(directory, exist_ok=True)
        name = f"{payload['ts'].replace(':', '')}-{os.path.basename(payload['path'])}-{payload['job']}.json"
        with open(os.path.join(directory, name), "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except Exception as e:  # pragma: no cover - best effort by design
        _err(f"tombstone file write failed (stdout record stands): {e}")


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def rename_for_deletion(path: str) -> str:
    """Hide the directory from the scanner, then delete the hidden one.

    The scanner finds models--*--* ANYWHERE under the root, so a partially
    deleted cache dir would still be listed as a model with missing files. A
    rename to .deleting-<name> no longer matches that glob: the model vanishes
    from the listing the instant deletion starts, and a run killed mid-rmtree
    leaves an obviously-junk directory instead of a corrupt-looking model.

    Rename is atomic within one directory, so there is no window where both
    names exist. If a previous attempt left the hidden name behind, a timestamp
    suffix keeps this attempt from clobbering it.
    """
    parent = os.path.dirname(path)
    base = os.path.basename(path.rstrip("/"))
    pending = os.path.join(parent, f".deleting-{base}")
    if os.path.exists(pending):
        pending = os.path.join(parent, f".deleting-{base}-{int(time.time())}")
    os.rename(path, pending)
    return pending


def run(
    *,
    targets: list[dict],
    scan_root: str,
    actor: str,
    job: str,
    remove=shutil.rmtree,
    write_tombstone: bool = True,
) -> int:
    """Validate, record, delete. Returns the process exit code."""
    if not scan_root:
        _err("MD_SCAN_ROOT is not set")
        return 1
    if not targets:
        _err("no targets")
        return 1

    # Structural validation FIRST: one bad path aborts everything, because a bad
    # path means the payload is not what we think it is.
    problems = [(t["cachepath"], validate_target(t["cachepath"], scan_root)) for t in targets]
    problems = [(path, reason) for path, reason in problems if reason]
    if problems:
        for path, reason in problems:
            _err(f"refusing the whole run — {path}: {reason}")
        return 1

    entries, vanished = build_entries(targets)
    for path, reason in vanished:
        _out(f"SKIP {path} — {reason}")
    if not entries:
        _err("no readable targets after validation")
        return 1

    deleted = 0
    for row in entries:
        path = row["cachepath"]
        payload = tombstone(
            row,
            actor=actor,
            job=job,
            override=row.get("override", False),
            verdict=row.get("verdict", "deletable"),
        )
        _out(json.dumps(payload, sort_keys=True))
        if write_tombstone:
            write_tombstone_file(scan_root, payload)
        try:
            remove(rename_for_deletion(path))
        except Exception as e:
            _err(f"{path}: delete failed ({e})")
            continue
        deleted += 1
        _out(f"DELETED {path}")

    _out(
        f"MD-DELETE DONE: {deleted} deleted, {len(vanished)} skipped, "
        f"{len(targets)} requested"
    )
    return 0 if deleted == len(entries) else 1


def main() -> int:
    try:
        targets = decode_targets(os.environ.get("MD_TARGETS_B64", ""))
    except Exception as e:
        _err(f"bad targets payload: {e}")
        return 1
    return run(
        targets=targets,
        scan_root=os.environ.get("MD_SCAN_ROOT", ""),
        actor=os.environ.get("MD_ACTOR", "unknown"),
        job=os.environ.get("MD_JOB_NAME", "unknown"),
    )


if __name__ == "__main__":
    sys.exit(main())
