"""MLIS packaged-model versioning: configuration fingerprints and version rules.

The catalog authors a *configuration*; MLIS owns the *version*.  A push lands
as v1 when the name is new on that MLIS instance, and as max(version)+1 when
the name already exists with a different configuration.  Re-pushing an
unchanged configuration is reported as skipped rather than versioned up, so a
catalog refreshed from GitHub -- or the same config pushed twice by two
operators -- does not grow versions that differ only in a typo fix.

Why this exists: catalog entries are sometimes pulled from a local MLIS and
carry whatever version that instance had (v9 for DeepSeek, v8 for GLM), so a
fresh unit would package the same model under a different version than every
other unit.  The catalog now always stores version 1 and this module decides
the real version at push time.

Kept out of db.py so the rules are unit-testable without psycopg2.
"""

from __future__ import annotations

import hashlib
import json
import re

# Columns that define WHAT GETS DEPLOYED.  The fingerprint is built from
# exactly these, so an unchanged configuration is recognised as a duplicate
# while any change to the deployment (uri, image, args, env, resources, GPU
# type, format, project) is a new version.  Deliberately excluded: name (the
# key being versioned), version (assigned here), description and metadata
# (cosmetic -- fixing a description typo must not mint a new version).
FINGERPRINT_COLUMNS = (
    "uri",
    "image",
    "registry",
    "arguments",
    "environment",
    "resource_request_cpu",
    "resource_request_memory",
    "resource_request_gpu",
    "resource_limit_cpu",
    "resource_limit_memory",
    "resource_limit_gpu",
    "resource_gpu_type",
    "model_format",
    "caching_enabled",
    "project",
)


def _canonical(value):
    """Normalise a column value so a catalog payload and a DB row compare equal.

    psycopg2 hands back text[] as a list and jsonb as a dict, while the pushed
    payload may carry tuples or the same data with a different scalar type
    (resources are strings in the catalog but may be integers in the DB).  Both
    sides are reduced to the same JSON-able form; scalars go through str(), so
    a bool stays "True"/"False" on both sides and 4 matches "4".
    """
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    if value is None:
        return ""
    return str(value)


# Columns whose "empty" value is a container rather than a string.  A raw
# /api/push payload that omits one of these inserts SQL NULL, while the catalog
# path always sends [] / {} (Catalog.add defaults them) -- both mean "nothing
# configured", so they must fingerprint the same or a config that crossed
# representations would mint one spurious version before stabilising.
_EMPTY_CONTAINER: dict[str, object] = {"arguments": [], "environment": {}}


# ---- the project field ----
#
# A packaged model can be filed under a project.  In PCAI a project IS a
# Kubernetes namespace, so the value is a namespace name.  Resolution order is
# explicit entry > the project picked for this push > the deployment default
# (chart: catalog.defaultProject); unset stays "" -- which is what MLIS shows
# only under "All projects".
PROJECT_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def normalize_project(value: str) -> str:
    """Strip and validate a project/namespace string.  "" is valid (unset)."""
    project = (value or "").strip()
    if project and (len(project) > 63 or not PROJECT_RE.match(project)):
        raise ValueError(
            "project must be a Kubernetes namespace name: lowercase letters, digits and '-', "
            "starting and ending with an alphanumeric, at most 63 characters"
        )
    return project


def resolve_project(cfg: dict, push_project: str = "", default_project: str = "") -> str:
    """Project to record for a config: explicit entry > per-push > chart default."""
    explicit = str(cfg.get("project") or "").strip()
    if explicit:
        return explicit
    return (push_project or "").strip() or (default_project or "").strip()


def config_fingerprint(cfg: dict) -> str:
    """Stable digest of a config's deployment-defining fields.

    Two configs with the same fingerprint are the same deployment: pushing one
    when the other is already in MLIS is a no-op, not a new version.
    """
    payload = {}
    for col in FINGERPRINT_COLUMNS:
        value = cfg.get(col)
        if value is None and col in _EMPTY_CONTAINER:
            value = _EMPTY_CONTAINER[col]
        payload[col] = _canonical(value)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def plan_push(existing: list[dict], cfg: dict) -> dict:
    """Decide which version a config lands as, given the rows MLIS already has.

    existing is every row MLIS holds for this name, each a dict with
    "version" plus the FINGERPRINT_COLUMNS (that is exactly what db.py
    selects).  Returns {version, status, detail, bumped} where status is
    "pushed" (insert this version) or "skipped" (identical config already
    present).  The config's own "version" field is ignored: the catalog
    always stores 1 and MLIS owns the sequence.
    """
    fingerprint = config_fingerprint(cfg)
    for row in existing:
        if config_fingerprint(row) == fingerprint:
            version = int(row.get("version") or 1)
            return {
                "version": version,
                "status": "skipped",
                "detail": f"already exists in MLIS as v{version} (identical configuration)",
                "bumped": False,
            }
    if not existing:
        return {"version": 1, "status": "pushed", "detail": "", "bumped": False}
    latest = max(int(row.get("version") or 1) for row in existing)
    return {
        "version": latest + 1,
        "status": "pushed",
        "detail": f"v{latest} already exists for this name with a different configuration",
        "bumped": True,
    }
