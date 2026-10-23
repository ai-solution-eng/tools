"""Enrollment-time per-user memory datasets (review §2; DECISIONS "APPROVED
w/ refinement — opt-in at redemption, not always-provision").

The per-key MCP credential override (mcp_server_creds) exists so each tenant
can address its own memory dataset on the fleet's RAG server — but until now
an admin created the dataset by hand and pasted the X-Memory-Dataset /
X-Dataset-Password headers into the key's creds dialog. This module closes
the loop: at enrollment REDEMPTION (an already-authenticated moment — the
code is the credential), an opted-in redeemer gets a private dataset
provisioned on the registered RAG server and the credential pair stamped
onto their fresh key automatically.

CONTRACT (all six points from the review, restated as invariants):

* OPT-IN AT REDEMPTION: the redeem request's ``memory_opt_in`` field decides
  — None falls back to the deployment default
  (PCAI_LLM_ENROLL_MEMORY_DEFAULT); a profile bound to the enrollment code
  may force provisioning with ``"memory": {"provision": true}`` in its
  defaults_json (profile beats deployment default, request beats profile:
  the human at the keyboard stays the last word).
* AUTHENTICATED CALL PATH, NO FREE-FORM URL: the RAG server row comes from
  the mcp_servers REGISTRY snapshot (core.registry) by
  PCAI_LLM_ENROLL_MEMORY_SERVER name — the URL is admin-registered and
  was _check_url-validated at registration time (loopback/metadata denied,
  private in-cluster allowed). A free-form RAG URL env would re-open the
  SSRF surface the registry gate closed; none is read, deliberately. Auth
  reuses the SAME server-level credential resolution every other MCP call
  uses (build_mcp_headers: Fernet auth_token_enc first, then the by-ref
  K8s Secret env PCAI_LLM_MCP_KEY__<NAME>) — the gateway already holds
  this credential for the federated RAG server; no new secret plumbing.
* NAMING: ``mem-<tenant>-<user>-<key6>`` where key6 = the first 6 hex of
  the key_id — human-readable, and globally unique in RAG even across two
  "Alice"s (enrollment suffixes user names, but RAG dataset names must be
  globally unique; the key fragment is the disambiguator). RAG validates
  ``[A-Za-z0-9._-]`` after whitespace→underscore, so tenant/user names are
  sanitized to that grammar here.
* BIND, DON'T DISPLAY: on success the pair {X-Memory-Dataset,
  X-Dataset-Password} is stamped into mcp_server_creds (key_id, server)
  via the EXISTING write-only Fernet path — values are never returned by
  any read endpoint and merge into that key's relay calls to that server
  LAST-WINS after server-level auth. The generated password rides the
  redemption response ONCE (the user may want direct RAG access; the relay
  path works without it).
* DELETION OWNERSHIP: the gateway PROVISIONS and BINDS, never deletes.
  RAG's DELETE /api/datasets/{name} requires the dataset password, which
  the gateway only ever stores — so deletion stays a RAG-side operation.
  The gateway's only "delete" is clearing its own creds row (orphaning the
  dataset — visible in RAG's dataset list, harmless). This module must not
  grow a second dataset admin UI.
* PROVISIONING FAILURE NEVER FAILS THE REDEMPTION: the key is created
  FIRST (create → provision → stamp, the over-provision-on-crash ordering
  rotate_key uses — a crash can only leave a live key without memory,
  never a provisioned dataset without a live key). Every failure path
  returns ``memory: {provisioned: False, error: ...}`` and the key.

Scope guard: the stamp lands on the single (key_id, server) creds row —
resolve_mcp_headers injects the pair ONLY for that key's calls to that
server. Rotation copies mcp_server_creds rows verbatim (routes_admin
rotate_key — the fixed 2026-09 behavior), so the identity survives
rotation without re-encryption.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from secrets import token_urlsafe as _token_urlsafe
from typing import Any

import httpx2

from ..config import settings
from ..db import db
from . import registry, secretbox

log = logging.getLogger(__name__)

# Bounded wait for the provisioning POST: the redeem request holds the
# response hostage for this long at most; a metadata-bound (the 15 s
# catalog pattern) not the 600 s model timeout — enrollment is interactive.
_PROVISION_TIMEOUT_S = 15.0

# RAG's dataset-name grammar (dataset_manager._validate_name): must start
# alphanumeric, then [A-Za-z0-9._-]. RAG itself only maps whitespace→_;
# every other rejection is a 400 — so the gateway sanitizes tenant/user
# name fragments itself and keeps the whole name inside the grammar.
_DS_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
# Fragment cap: mem- (4) + 60 + 1 + 60 + 1 + 6 = 132 — the whole name is
# bounded BY CONSTRUCTION (no outer slice that could eat the key6 tail;
# the key fragment is the uniqueness and must always survive intact).
_DS_FRAG_MAX = 60

MEMORY_HEADER_DATASET = "X-Memory-Dataset"
MEMORY_HEADER_PASSWORD = "X-Dataset-Password"


def dataset_name_for(tenant_name: str, user_name: str, key_id: uuid.UUID) -> str:
    """``mem-<tenant>-<user>-<key6>`` — the globally-unique naming contract.

    Runs of characters outside RAG's grammar collapse to '_'; an empty
    fragment degrades to a placeholder so the name shape
    `mem-…-…-<key6>` always holds; the key6 hex suffix is the real
    uniqueness carrier (two "Alice"s disambiguate by key). Each readable
    fragment is capped at _DS_FRAG_MAX so the key6 tail can never be cut.
    """

    def frag(value: str, placeholder: str) -> str:
        cleaned = _DS_NAME_RE.sub("_", value.strip())[:_DS_FRAG_MAX].strip("._-")
        return cleaned or placeholder

    return f"mem-{frag(tenant_name, 't')}-{frag(user_name, 'u')}-{key_id.hex[:6]}"


def memory_opt_in_requested(
    request_opt_in: bool | None,
    profile_defaults: dict[str, Any] | None,
) -> bool:
    """Resolve the redeem request's opt-in tri-state against the profile
    preset and the deployment default.

    Precedence (explicit human intent wins over generic defaults):
    1. request ``memory_opt_in`` is True/False → THAT.
    2. None (absent) → the enrollment's profile ``"memory": {"provision":
       …}`` preset when the profile declares one (profile beats deployment
       default — an operator who preset the profile opted that cohort in).
    3. no profile preset → PCAI_LLM_ENROLL_MEMORY_DEFAULT.
    Malformed profile JSON shapes degrade to "not declared" (provisioning
    is opt-in; a typo must never silently provision).
    """
    if request_opt_in is not None:
        return bool(request_opt_in)
    if isinstance(profile_defaults, dict):
        memory = profile_defaults.get("memory")
        if isinstance(memory, dict):
            provision = memory.get("provision")
            if isinstance(provision, bool):
                return provision
    return settings.enroll_memory_default


async def resolve_memory_server(server_name: str) -> dict[str, Any] | None:
    """The enabled registry row for the memory server (or None).

    Resolved from the SAME snapshot the federation layer serves — so an
    admin-disabled mm-rag immediately stops provisioning (and its absence
    is reported, never guessed). The URL is the registry row's: already
    _check_url-validated at registration (routes_admin upsert_mcp_server);
    this function deliberately does NOT accept or consult any URL env.
    """
    for server in await registry.mcp_servers():
        if server.get("name") == server_name:
            return server
    return None


def _api_base_url(mcp_url: str) -> str:
    """REST origin of the RAG deployment from its registered MCP URL.

    The registry row points at the MCP surface (…/mcp or a bare origin);
    the dataset-create endpoint lives on the REST API (POST /api/datasets).
    Deployment shape (MultimodalRAG helm): both containers share the pod
    behind their own Services, and the ezua VirtualService routes `/mcp`
    to the MCP service and everything else to the API service — so
    stripping a trailing `/mcp` from the registered URL lands on the API
    surface through the same host. A registered URL with any other path is
    used as-is (the operator registered what they registered; only the
    known MCP suffix is normalized).
    """
    base = mcp_url.strip()
    if base.lower().endswith("/mcp"):
        base = base[: -len("/mcp")]
    return base.rstrip("/")


async def create_memory_dataset(
    server: dict[str, Any],
    dataset: str,
    dataset_password: str,
) -> tuple[bool, str]:
    """POST {name, dataset_password} to the RAG REST API with the server's own
    credential resolution. Returns (ok, error) — never raises.

    Error strings are OPAQUE TYPE NAMES for transport failures (the
    probe_model convention: connection refused / timeout / TLS must not
    leak host internals into a response any redeemer can read) but carry
    the upstream STATUS for HTTP rejections (a 409 vs a 401 vs a 400 is
    exactly what an operator debugging provisioning needs; the bodies of
    those responses are RAG's own validation messages, no gateway secret).

    409 handling lives with the CALLER (bounded suffix retry — the caller
    owns the name sequence); this function reports the 409 verbatim.
    """
    from ..api.routes_mcp import build_mcp_headers  # lazy: avoids import cycles

    headers = build_mcp_headers(
        server.get("headers") or {},
        server.get("auth_secret"),
        server.get("auth_token_enc"),
    )
    headers["Content-Type"] = "application/json"
    url = f"{_api_base_url(str(server.get('url') or ''))}/api/datasets"
    try:
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(_PROVISION_TIMEOUT_S, connect=5.0),
            follow_redirects=False,
            # Same upstream-TLS contract as the model router (config.py):
            # verification unconditional; site CA bundle when configured.
            verify=settings.upstream_ca_bundle or True,
        ) as client:
            resp = await client.post(url, json={"name": dataset, "password": dataset_password}, headers=headers)
    except (
        httpx2.HTTPError,
        httpx2.InvalidURL,
        ValueError,
    ) as exc:  # InvalidURL is NOT an HTTPError subclass (audit P2-4, verified at runtime)
        return False, type(exc).__name__
    if resp.status_code == 201:
        return True, ""
    return False, f"HTTP {resp.status_code}"


async def provision_memory_for_key(
    key_id: uuid.UUID,
    tenant_name: str,
    user_name: str,
    profile_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The full provisioning step for ONE fresh key. NEVER raises.

    Returns the redemption response's ``memory`` block:
      {provisioned: True, dataset, dataset_password} on success (the dataset_password is
      shown ONCE here — the same shown-once discipline as the key itself),
      {provisioned: False, error} on any failure (key already live).
    The Fernet stamp is the LAST step: a crash before it leaves an
    orphaned RAG dataset (harmless, visible in RAG's list) but never a
    stamped credential pointing at a dataset that was never created.
    """
    if not secretbox.enabled():
        return {
            "provisioned": False,
            "error": "credential storage disabled: PCAI_LLM_SECRETS_MASTER_KEY is not set",
        }
    server = await resolve_memory_server(settings.enroll_memory_server)
    if server is None:
        return {
            "provisioned": False,
            "error": f"memory server {settings.enroll_memory_server!r} is not in the enabled registry",
        }

    # 32 bytes of entropy in the urlsafe alphabet (fits the header-value
    # grammar; RAG treats it opaquely). NOT the enrollment-code generator —
    # a different secret with a different lifetime.
    dataset_password = _token_urlsafe(24)
    dataset = dataset_name_for(tenant_name, user_name, key_id)
    ok, error = await create_memory_dataset(server, dataset, dataset_password)
    if not ok and error == "HTTP 409":
        # Bounded suffix retry (review: adopt-by-rename, never adopt-silently
        # — we cannot prove a 409 name is OURS without the dataset_password, so a
        # taken name gets a fresh suffix instead of a blind reuse).
        for attempt in range(2, settings.enroll_memory_name_retries + 2):
            candidate = f"{dataset}-{attempt}"
            ok, error = await create_memory_dataset(server, candidate, dataset_password)
            if ok or error != "HTTP 409":
                dataset = candidate
                break
    if not ok:
        return {"provisioned": False, "error": error}
    try:
        headers_enc = secretbox.encrypt(
            json.dumps({MEMORY_HEADER_DATASET: dataset, MEMORY_HEADER_PASSWORD: dataset_password})
        )
        await db.execute(
            "INSERT INTO mcp_server_creds (key_id, server_name, headers_enc) VALUES ($1, $2, $3) "
            "ON CONFLICT (key_id, server_name) DO UPDATE SET headers_enc = $3, updated_at = now()",
            key_id,
            settings.enroll_memory_server,
            headers_enc,
        )
    except Exception as exc:  # the stamp is enrichment; the key is live (broad by design)
        log.warning(
            "enroll memory: dataset %r created for key %s but the credential stamp failed (%s) — "
            "the dataset exists; re-stamp via PUT /admin/keys/{id}/mcp-creds",
            dataset,
            key_id,
            type(exc).__name__,
        )
        return {"provisioned": False, "dataset": dataset, "error": f"credential stamp failed: {type(exc).__name__}"}
    return {"provisioned": True, "dataset": dataset, "password": dataset_password}


def parse_profile_defaults(defaults_json: str | None) -> dict[str, Any]:
    """defaults_json TEXT → dict ({} on every failure) — the same tolerance
    _apply_profile_defaults uses; extracted so the memory decision and the
    preset application read ONE parse, not two divergent ones."""
    if not defaults_json:
        return {}
    try:
        parsed = json.loads(defaults_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
