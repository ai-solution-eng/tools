"""Self-service MCP management for keyed users (2026-10): a keyed caller
manages THEIR OWN key's MCP credential overrides and tool grants from the
homepage — no admin ticket, no admin key.

Governance envelope (the design invariant this module exists to hold):

* CREDENTIALS (``/v1/mcp/creds``) do not expand what a key can CALL — they
  only change the identity attached to calls the key can already make. The
  write path is the admin path verbatim (same validation, same Fernet
  write-only storage, same reserved-header refusals) keyed to the CALLER's
  own key id: there is no key_id parameter to squat on.
* TOOL GRANTS (``/v1/mcp/selfservice/grants``) can only TIGHTEN, never
  expand (operator decision, 2026-10): a self-grant is accepted only for a
  pair the key ALREADY HOLDS — its held TRUE rows (key rows ∪ the owner
  profile's live-grant presets, folded by federation.load_grants). Under
  the default 'explicit' grant mode that held set IS the callable set, so
  the profile is a hard envelope: a tool the profile withholds is not
  listed, not self-grantable, and a whole withheld server is omitted from
  the panel (the 2026-10-09 SSO poweruser leak — the old scope-mode branch
  folded the entire policy-admitted catalog into the entitlement, letting
  a self-minted mcp-scoped key self-grant applygate/k8s-ops). Under legacy
  scope mode the policy-admitted pairs stay shown and callable exactly as
  the relay admits them at call time (the panel never expanded that — the
  relay did); a grant outside the held set is still 403 there. Deny rows
  are unrestricted within listed servers (a deny only shrinks).

Flag: ``settings.mcp_self_service`` (chart ``settings.mcpSelfService``) —
false hides every route here; the admin surfaces are unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..config import settings
from ..core import registry, secretbox
from ..core.keys import KeyRecord
from ..db import db
from ..mcpf import federation
from . import routes_enroll
from .auth import require_scope

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/mcp", tags=["mcp-selfservice"])

# Every route requires the mcp scope: the surface manages MCP identities
# and grants, so a proxy-only key has no business here (403 same as the
# relay's own gate).
_mcp_key: KeyRecord = Depends(require_scope("mcp"))


def _gate() -> None:
    """Flag gate: 404 (not 403) when the surface is off — an off surface is
    indistinguishable from one that was never deployed."""
    if not settings.mcp_self_service:
        raise HTTPException(404, "not found")


async def _enabled_servers() -> list[dict[str, Any]]:
    """The enabled registry servers, priority-ordered (registry order)."""
    return [s for s in await registry.mcp_servers() if s.get("enabled", True)]


async def _server_tools(target: dict[str, Any]) -> list[dict[str, Any]]:
    """TTL-cached upstream tool list for one server (the SAME cache the
    relay's catalog preflight uses — no second fetch path)."""
    from .routes_mcp import _cached_upstream_tools

    return await _cached_upstream_tools(target)


async def _entitlement_catalog(key: KeyRecord) -> tuple[dict[str, list[dict[str, Any]]], set[tuple[str, str]]]:
    """(per-server catalog rows, entitlement set) for the calling key.

    GOVERNANCE RULE (2026-10 operator decision): self-service can only
    TIGHTEN, never expand. The entitlement set is the pairs the key can
    ALREADY call — its held TRUE rows (key rows ∪ live-profile presets),
    plus, under scope grant mode, the policy-admitted catalog pairs
    (because the relay grants those to an mcp-scoped key by default, so
    "already callable" includes them). A self-grant OUTSIDE that union is
    403: the user turns things OFF (deny rows) and re-enables what they
    hold; they never GAIN access. The catalog lists every enabled server's
    tools for VISIBILITY of the deny surface, but non-entitled pairs are
    not self_grantable.
    """
    # Entitlement (2026-10-09): the HELD set only — profile live presets ∪
    # the key's own ACL rows. The old scope-mode branch folded the whole
    # policy-admitted catalog into the entitlement, which let a self-minted
    # key self-grant tools its profile withheld (the SSO poweruser leak) —
    # self-service can only TIGHTEN the profile envelope, never re-open it.
    held = await federation.load_grants(key.id)  # key TRUE rows ∪ live-profile presets
    catalog: dict[str, list[dict[str, Any]]] = {}
    for target in await _enabled_servers():
        name = target["name"]
        try:
            tools = await _server_tools(target)
        except Exception as exc:
            log.warning("selfservice: tool fetch for %r failed: %s", name, type(exc).__name__)
            catalog[name] = []
            continue
        catalog[name] = [
            {
                "tool": t["tool"],
                "description": t.get("description", ""),
                "policy_ok": federation.server_allows(target, t["tool"]),
            }
            for t in tools
        ]
    return catalog, set(held)


async def _my_cred_names(key_id: Any) -> dict[str, list[str]]:
    """server -> stored override header NAMES for one key (values never
    leave the DB; an undecryptable row lists empty, never 500s)."""
    rows = await db.fetch(
        "SELECT server_name, headers_enc FROM mcp_server_creds WHERE key_id = $1 ORDER BY server_name",
        key_id,
    )
    out: dict[str, list[str]] = {}
    for row in rows:
        names: list[str] = []
        try:
            raw = secretbox.decrypt(row["headers_enc"])
            parsed = json.loads(raw) if raw else {}
            if isinstance(parsed, dict):
                names = sorted(str(n) for n in parsed)
        except Exception:
            log.warning("selfservice: undecryptable creds row key %s/%s", key_id, row["server_name"])
        out[row["server_name"]] = names
    return out


# ---------------------------------------------------------------------------
# credentials — the caller's OWN mcp_server_creds rows
# ---------------------------------------------------------------------------


@router.get("/creds")
async def list_my_creds(key: KeyRecord = _mcp_key) -> list[dict[str, Any]]:
    """Which servers the calling key carries credential overrides for, and
    which header NAMES — never the values (write-only, admin-path parity)."""
    _gate()
    return [{"server": s, "header_names": n} for s, n in (await _my_cred_names(key.id)).items()]


@router.put("/creds/{server}")
async def put_my_cred(
    server: str, body: routes_enroll.McpCredsUpsert, request: Request, key: KeyRecord = _mcp_key
) -> dict[str, Any]:
    """Set/replace the calling key's header overrides for one MCP server.
    Same validation/storage/audit vocabulary as the admin PUT — the only
    difference is that the key id comes from the caller, not the URL."""
    _gate()
    if server != body.server:
        raise HTTPException(400, "URL server must match body.server")
    if not await db.fetchval("SELECT 1 FROM mcp_servers WHERE name = $1", body.server):
        raise HTTPException(404, f"MCP server {body.server!r} not found")
    clean = routes_enroll._validate_creds_headers(body.headers)
    if not secretbox.enabled():
        raise HTTPException(503, "credential storage disabled: PCAI_LLM_SECRETS_MASTER_KEY is not set")
    headers_enc = secretbox.encrypt(json.dumps(clean))
    await db.execute(
        "INSERT INTO mcp_server_creds (key_id, server_name, headers_enc) VALUES ($1, $2, $3) "
        "ON CONFLICT (key_id, server_name) DO UPDATE SET headers_enc = $3, updated_at = now()",
        key.id,
        body.server,
        headers_enc,
    )
    # AUDIT: header NAMES + count only — credential VALUES never enter the
    # trail (same redaction posture as the admin path).
    await routes_enroll._enroll_audit(
        request,
        action="mcp.creds.self",
        resource_type="key",
        resource_id=str(key.id),
        actor_source="key",
        details={"server": body.server, "header_names": sorted(clean), "header_count": len(clean)},
    )
    return {"server": body.server, "header_names": sorted(clean)}


@router.delete("/creds/{server}")
async def delete_my_cred(server: str, request: Request, key: KeyRecord = _mcp_key) -> dict[str, Any]:
    """Clear the calling key's stored overrides for one server."""
    _gate()
    status = await db.execute(
        "DELETE FROM mcp_server_creds WHERE key_id = $1 AND server_name = $2",
        key.id,
        server,
    )
    if status == "DELETE 0":
        raise HTTPException(404, f"no stored credentials for server {server!r}")
    await routes_enroll._enroll_audit(
        request,
        action="mcp.creds.self.delete",
        resource_type="key",
        resource_id=str(key.id),
        actor_source="key",
        details={"server": server},
    )
    return {"deleted": True, "server": server}


# ---------------------------------------------------------------------------
# grants — entitlement-bounded toggles over the caller's own ACL rows
# ---------------------------------------------------------------------------


@router.get("/selfservice")
async def my_mcp_overview(key: KeyRecord = _mcp_key) -> dict[str, Any]:
    """One call powering the homepage dialog: the caller's visible servers,
    each tool's EFFECTIVE call-time grant state, the credential-override
    header names, and the per-pair entitlement bound (what a self-grant may
    still touch).

    TRUTHFULNESS (2026-10-09, user-reported): under scope grant mode the
    relay ADMITS the whole policy-admitted catalog at call time
    (routes_proxy scope merge: (held | catalog) - denies) — a pair the
    dialog showed unchecked was still callable. `granted` now mirrors the
    relay's own admission set exactly; `via_scope` marks pairs admitted by
    the scope merge that no ACL row holds (the checkbox the user never
    asked for — the only brake on those is a deny row, which unchecking
    writes)."""
    _gate()
    catalog, entitled = await _entitlement_catalog(key)
    creds = await _my_cred_names(key.id)
    denies = await federation.load_denies(key.id)
    # Truthful visibility (2026-10-09, the SSO poweruser leak): list ONLY
    # the entitled pairs — held TRUE rows minus the key's deny exceptions,
    # exactly the relay's call-time admission set under the default
    # 'explicit' grant mode. Under legacy scope mode the relay still admits
    # policy-admitted pairs at call time, so those stay shown (granted,
    # marked via_scope) — hiding them there would lie about callability.
    # A pair the key holds NOTHING on is not listed, and a server with no
    # visible pair at all is OMITTED entirely: withheld servers (applygate,
    # k8s-ops…) must not even appear as disabled rows to probe names
    # against. Cred overrides are separate (/creds) — an override on an
    # uncallable server is inert, so it earns no visibility here either.
    scope_mode = settings.tool_relay_grant_mode == "scope" and "mcp" in key.scopes
    effective = entitled - denies
    servers_out = []
    for name, rows in sorted(catalog.items()):
        visible = [
            {
                "tool": r["tool"],
                "description": r["description"],
                "granted": (name, r["tool"]) in effective
                or (scope_mode and r["policy_ok"] and (name, r["tool"]) not in denies),
                "via_scope": scope_mode and r["policy_ok"] and (name, r["tool"]) not in entitled,
                "self_grantable": (name, r["tool"]) in entitled,
            }
            for r in rows
            if (name, r["tool"]) in entitled or (scope_mode and r["policy_ok"] and (name, r["tool"]) not in denies)
        ]
        if not visible:
            # omit the whole server — the withheld-collection case
            continue
        servers_out.append(
            {
                "server": name,
                "cred_header_names": creds.get(name, []),
                "tools": visible,
            }
        )
    return {
        "grant_mode": settings.tool_relay_grant_mode,
        "scope_mode": scope_mode,
        "servers": servers_out,
    }


class SelfGrantToggle(BaseModel):
    """Body for PUT /selfservice/grants — one (server, tool) pair."""

    server: str
    tool: str
    allowed: bool


@router.put("/selfservice/grants")
async def toggle_my_grant(body: SelfGrantToggle, request: Request, key: KeyRecord = _mcp_key) -> dict[str, Any]:
    """Allow/deny ONE (server, tool) pair on the CALLING key's own ACL.

    allowed=true is accepted only for a HELD pair (403 outside — the
    module invariant: the profile envelope cannot be re-opened from the
    panel); allowed=false is unrestricted within listed servers (a deny
    only shrinks)."""
    _gate()
    servers = {s["name"]: s for s in await _enabled_servers()}
    if body.server not in servers:
        raise HTTPException(404, f"MCP server {body.server!r} not found")
    if body.allowed:
        _, entitled = await _entitlement_catalog(key)
        if (body.server, body.tool) not in entitled:
            raise HTTPException(
                403,
                f"tool {body.server}/{body.tool} is outside this key's self-service entitlement "
                "(an admin grant or a policy-admitted catalog pair is required first)",
            )
    else:
        # A deny of a tool the server does not expose would be dead weight
        # and confusing in the listing — verify against the live catalog.
        try:
            tools = {t["tool"] for t in await _server_tools(servers[body.server])}
        except Exception:
            raise HTTPException(503, f"MCP server {body.server!r} tool catalog unavailable — deny not recorded")
        if body.tool not in tools:
            raise HTTPException(404, f"server {body.server!r} does not expose tool {body.tool!r}")
    await db.execute(
        "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (key_id, server, tool) DO UPDATE SET allowed = $4",
        key.id,
        body.server,
        body.tool,
        body.allowed,
    )
    await routes_enroll._enroll_audit(
        request,
        action="mcp.grants.self",
        resource_type="key",
        resource_id=str(key.id),
        actor_source="key",
        details={"server": body.server, "tool": body.tool, "allowed": body.allowed},
    )
    return {"server": body.server, "tool": body.tool, "allowed": body.allowed}
