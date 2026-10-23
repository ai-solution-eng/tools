"""Admin mutation audit trail — one immutable INSERT per admin mutation.

WHY THIS EXISTS: the usage ledger is the DATA-plane audit artifact (every
model/tool call lands there with key, cost, request_id). The ADMIN plane —
key create/revoke/rotate/patch, budget and cost edits, model/endpoint/
MCP-registry changes, per-key credential overrides, profile and off-peak
edits, enrollment redemption — had no durable record: a compliance question
("who granted tool X, when, from where") was answerable only from ephemeral
logs. ``admin_audit`` (db/schema.py) is that record; this module is the ONLY
writer, so the append-only + redaction contracts live in exactly one place.

CONTRACTS (enforced here, documented in schema.py):

- APPEND-ONLY: this module only ever INSERTs. No UPDATE, no DELETE, no
  pruning — mirroring the usage-ledger retention philosophy. An admin can
  audit themselves but cannot erase the trail.
- NEVER-SECRET: ``details`` payloads pass through ``details_redact()`` at
  write time; keys matching secret-ish patterns collapse to '<redacted>'.
  Credential VALUES (upstream keys, MCP bearer tokens, per-key override
  header values) never reach the audit row — only names and counts.
- NEVER-FATAL (the tradeoff, stated explicitly): an audit-write failure is
  logged loudly (log.exception) but NEVER fails or rolls back the admin
  operation that produced it. The admin operation has already committed its
  real work by the time the audit row is written; failing the HTTP response
  now would report a mutation as failed while it actually succeeded — the
  worse inconsistency (an operator retries, doubling the mutation) versus a
  missing audit line. If the DATABASE is down, the operation itself has
  already failed anyway, so there is nothing to audit and nothing to save.
  The accepted residual risk: an audit row can be lost while the mutation
  succeeded — the loud exception log is the compensating control, and
  callers that need mutation+audit atomicity would need a different design
  (in-transaction audit inserts), deliberately not chosen so an audit
  outage/degradation can never take the admin plane down.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..db import db

# <redacted> is the documented tombstone (schema.py REDACTION rule): a
# human-readable marker that survives JSON round-trips and reads honestly
# in the console ("a secret was here; the trail never stores values").
REDACTED = "<redacted>"

# Secret-ish KEY pattern for details_redact: matched case-insensitively
# against the whole key. Deliberately broad — a false redaction costs
# metadata, a false pass leaks a credential into a permanent, widely-
# readable table. Compound markers (api_key / access_key / private_key /
# auth / bearer) redact; a BARE 'key' segment deliberately does not
# ('key_id' / 'key_name' are the trail's own vocabulary — the schema
# contract keeps that metadata — and a bare-key rule would redact the
# whole trail into uselessness).
_SECRET_KEY_RE = re.compile(
    r"secret|password|passwd|token|credential|cred|auth|cookie|api[_-]?key|access[_-]?key|private[_-]?key|bearer",
    re.IGNORECASE,
)

# Metadata compounds that MENTION a secret-word but carry a NAME, COUNT,
# or presence MARKER — the schema contract keeps these explicitly
# ("credential name / key count" is its example): *_name(s), *_count,
# *_id(s), *_prefix, *_label, *_type(s), *_set/_cleared (auth_token_set
# booleans), *_present, *_enabled, plus the by-ref K8s Secret NAME fields
# (api_key_secret / secret_name / auth_secret hold a POINTER to a Secret,
# never the material itself).
_METADATA_KEY_RE = re.compile(
    r"(?:^|_)(?:names?|count|ids?|prefix|label|types?|set|cleared|present|enabled|"
    r"api_key_secret|secret_name|auth_secret)$",
    re.IGNORECASE,
)

# Token-COUNT compounds (budget/limit metadata): limit_tokens / est_tokens /
# max_tokens are usage quantities, never credential material — redacting
# them would blank every budget-set row. The quantity word binds to the
# '_tokens' tail; a key that merely CONTAINS 'token' (auth_token) still
# redacts.
_TOKEN_COUNT_RE = re.compile(
    r"(?:^|_)(?:limit|max|min|est|rate|num|remaining|total|prompt|completion|cached|seed)s?_tokens$",
    re.IGNORECASE,
)

# Keys whose VALUE MAP is secret material even when individual entry names
# look innocent: the per-key MCP credential override container
# ({"X-Memory-Dataset": ..., "X-Dataset-Password": ...}) — header VALUES
# sent upstream are credentials by definition. `header_names` (a LIST of
# names) is NOT a container — the names are exactly the metadata the
# contract keeps.
_HEADER_CONTAINER_KEYS = {"headers", "override_headers"}


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    if _METADATA_KEY_RE.search(k) or _TOKEN_COUNT_RE.search(k):
        return False
    return bool(_SECRET_KEY_RE.search(k))


def details_redact(payload: Any, _parent_key: str = "") -> Any:
    """Recursively redact secret material from an audit ``details`` payload.

    Rules (schema.py REDACTION rule):
    - a dict KEY matching the secret-ish pattern (secret/token/password/
      credential/auth/api-key/...) has its value replaced by '<redacted>'
      — scalars, lists, and nested dicts alike — EXCEPT the documented
      metadata compounds (_name(s)/_count/_id(s)/_prefix/_type(s)/_set/
      _cleared + by-ref Secret NAME fields + token-count compounds), which
      carry a name/count/marker, never material;
    - inside a headers/override-headers container (the per-key MCP
      credential override shape: {"X-Memory-Dataset": ..., "X-Dataset-
      Password": ...}) EVERY value redacts — header values sent upstream
      are credentials by definition, even when the header name looks
      innocent;
    - lists are walked element-wise; nested dicts recurse;
    - everything else passes through untouched.

    Never raises: a redaction fault must not fail the audit write (worst
    case is an over- or under-redacted payload, never a lost mutation
    response). Redaction is applied at WRITE time (inside audit_action), so
    a caller forgetting to pre-redact is still safe.
    """
    try:
        if isinstance(payload, dict):
            out: dict[str, Any] = {}
            for key, value in payload.items():
                key_str = str(key)
                if key_str.lower() in _HEADER_CONTAINER_KEYS and value is not None:
                    # header NAMES survive (the API returns names too); the
                    # VALUES are credentials — redact element-wise.
                    out[key_str] = {str(k): REDACTED for k in value} if isinstance(value, dict) else REDACTED
                elif _is_secret_key(key_str):
                    out[key_str] = REDACTED
                else:
                    out[key_str] = details_redact(value, key_str)
            return out
        if isinstance(payload, (list, tuple)):
            return [details_redact(item, _parent_key) for item in payload]
        return payload
    except Exception:  # broad-by-design: never fatal (module docstring)
        return REDACTED


def stable_actor_label(secret_material: str) -> str:
    """A stable, non-reversible actor label for a Secret-configured admin key.

    Admin keys are K8s-Secret material (never DB rows), so the trail cannot
    name them by id; it names them by a SHORT SHA-256 PREFIX instead —
    stable across requests with the same key (one actor stays one actor),
    never reversible to the key, and never the full digest (a full hash of
    a low-entropy key is a brute-force oracle; 12 hex chars is plenty to
    distinguish a two-to-five-key deployment).
    """
    return "admin-key:" + hashlib.sha256(secret_material.encode()).hexdigest()[:12]


async def audit_action(
    conn: Any = None,
    *,
    actor_key_id: int | None = None,
    actor_subject: str = "",
    actor_source: str,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
    source_ip: str | None = None,
) -> None:
    """Write ONE admin_audit row. Best-effort BY CONTRACT (module docstring):
    an audit failure logs loudly and never fails the caller's operation.

    ``conn`` is the asyncpg connection to use. Two call shapes are both
    fine and callers pick by semantics:
    - ``None`` (the default and the shape every route uses): the audit row
      commits on THIS module's own pool seam (``audit.db``), AFTER the
      operation's transaction — the operation is already durable when the
      audit runs (see the NEVER-FATAL tradeoff above). A dedicated seam
      (not the caller's ``db`` handle) is deliberate: unit tests fake the
      OPERATION seams (routes_admin.db / core.keys.db) and must keep
      passing un-edited; faking ``audit.db`` is how audit tests observe
      the trail, and in production both resolve to the same pool.
    - a live transaction connection: the audit row joins the operation's
      transaction. Used only where partial-failure analysis says the row
      must not appear for a rolled-back operation.

    ``details`` passes through details_redact() here — callers do not have
    to remember; redaction-at-the-single-writer is the enforceable point.
    """
    payload = details_redact(details or {})
    try:
        target = db if conn is None else conn
        await target.execute(
            """
            INSERT INTO admin_audit
                (actor_key_id, actor_subject, actor_source, action,
                 resource_type, resource_id, details, request_id, source_ip)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            """,
            actor_key_id,
            actor_subject or "",
            actor_source,
            action,
            resource_type,
            resource_id,
            json.dumps(payload, default=str),
            request_id,
            source_ip,
        )
    except Exception:
        # Loud, never fatal (module docstring: the tradeoff in full). The
        # admin operation above this call has already done its real work;
        # raising here would turn a SUCCEEDED mutation into a reported
        # failure — an operator-visible lie that invites a destructive
        # retry. The exception log is the compensating control for the
        # lost row.
        import logging

        logging.getLogger(__name__).exception(
            "admin_audit WRITE FAILED (mutation %s on %s/%s succeeded; audit row lost) — check database health",
            action,
            resource_type,
            resource_id,
        )
