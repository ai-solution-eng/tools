"""Control-plane -> data-plane registry loader.

The admin writes rows (model_endpoints, mcp_servers) into Postgres; this
module reads them ONCE per refresh and publishes in-memory snapshots the
hot path consumes lock-free: model endpoints into ``model_router`` (one
Pool per virtual model name) and MCP servers into this module's own cache
for the MCP federation layer. Refresh is whole-snapshot semantics — a row
disabled between refreshes disappears from routing at the next refresh,
never mid-request.

Secrets discipline (fleet convention, the applygate lesson): upstream API
keys are NEVER stored in the database. ``model_endpoints.api_key_secret``
holds the NAME of a Kubernetes Secret that the deployment layer (mounted
files / env / external-secrets) materializes for the proxy to read
by-reference at call time. The registry simply carries the name through.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ..db import db
from ..routing.router import Endpoint
from ..routing.router import router as model_router

log = logging.getLogger(__name__)

# Registry-change hooks (design doc §5 invalidation): fired after every
# successful refresh() — every admin MCP-registry mutation (register /
# update / toggle / POST /admin/registry/refresh) funnels through refresh(),
# so consumers hook HERE to invalidate derived caches instead of reaching
# into routes_admin internals (which would invert the dependency and miss
# the /registry/refresh path). Hook failures are swallowed+logged: a broken
# consumer cache must never fail a registry write.
_registry_change_hooks: list[Callable[[], None]] = []


def on_mcp_registry_change(hook: Callable[[], None]) -> None:
    """Register a synchronous callback fired after every successful refresh().

    The MCP federation listing cache registers its invalidator here at import
    time; hooks must be cheap and non-async (they run inline in the admin
    write path).
    """
    _registry_change_hooks.append(hook)


# Module-level cache of enabled MCP servers, rebuilt wholesale by refresh().
# Shape: [{"id": str, "name": str, "url": str, "headers": dict[str, str]}]
# Read freely by request paths (list assignment is atomic in CPython); the
# only writer is refresh(), which swaps the whole list in one statement so
# readers never observe a half-updated snapshot.
_mcp_servers_cache: list[dict[str, Any]] = []


async def mcp_servers() -> list[dict[str, Any]]:
    """Enabled MCP servers from the last refresh(): name/url + parsed headers.

    Returns a copy of each entry (the ``headers`` dict especially — callers
    mutate per-request, e.g. adding auth headers, and must not write through
    to the cache).
    """
    return [{**server, "headers": dict(server["headers"])} for server in _mcp_servers_cache]


async def refresh() -> None:
    """Reload model endpoints and MCP servers from the control-plane tables.

    Both queries are plain SELECTs; a failure leaves the previous snapshot
    in place (whole-snapshot swap) and re-raises so lifespan/startup wiring
    can decide whether the gateway boots at all. Per-model ``set_endpoints``
    calls REPLACE that model's pool — endpoints deleted or disabled in the
    DB stop routing after the next refresh.
    """
    global _mcp_servers_cache

    # --- model endpoints -> router pools -------------------------------
    endpoint_rows = await db.fetch(
        """
        SELECT id, model_name, base_url, priority, weight, api_key_secret, auth_token_enc, model_type
        FROM model_endpoints WHERE enabled = TRUE
        """
    )
    by_model: dict[str, list[Endpoint]] = {}
    for row in endpoint_rows:
        by_model.setdefault(row["model_name"], []).append(
            Endpoint(
                id=str(row["id"]),
                model_name=row["model_name"],
                base_url=row["base_url"],
                priority=row["priority"],
                weight=row["weight"],
                # K8s Secret NAME (by-ref) — never a literal key; resolved
                # downstream by whatever materializes the Secret.
                api_key_secret=row["api_key_secret"],
                # Fernet-encrypted key (admin panel path) — CIPHERTEXT only
                # in the cache; decrypted per call, never logged.
                auth_token_enc=row["auth_token_enc"],
                model_type=row["model_type"],
            )
        )
    # Whole-snapshot swap (NOT per-model set_endpoints): a model whose rows
    # all disappeared must stop being served — the old code kept its stale
    # pool until pod restart while /v1/models still advertised it.
    model_router.replace_pools(by_model)
    log.info(
        "registry refresh: %d model endpoint(s) across %d model(s)",
        len(endpoint_rows),
        len(by_model),
    )

    # --- MCP servers -> federation cache --------------------------------
    # Order: (priority ASC, name ASC) — lower priority = preferred, the same
    # convention as model_endpoints.priority. This ordering IS the fleet
    # contract for mcp_catalog/tools/list output order AND the flattened-call
    # failover chain.
    server_rows = await db.fetch(
        """
        SELECT id, name, url, headers_json, auth_secret, auth_token_enc, enabled,
               tool_policy, allowed_tools_json, priority, description
        FROM mcp_servers WHERE enabled = TRUE
        ORDER BY priority ASC, name ASC
        """
    )
    cache: list[dict[str, Any]] = []
    for row in server_rows:
        try:
            headers = json.loads(row["headers_json"]) if row["headers_json"] else {}
            if not isinstance(headers, dict):
                # ValueError (not TypeError): this is data validation of a DB
                # column, not a misuse of an API.
                raise ValueError("headers_json must be a JSON object")  # noqa: TRY004
        except (json.JSONDecodeError, ValueError) as exc:
            # One malformed row must not take down federation for the
            # healthy ones: skip it loudly and keep loading.
            log.warning(
                "mcp_server %r has invalid headers_json (%s) — server skipped",
                row["name"],
                exc,
            )
            continue
        try:
            allowed_tools = json.loads(row["allowed_tools_json"] or "[]")
            if not isinstance(allowed_tools, list):
                # ValueError (not TypeError): DB-column data validation
                raise ValueError("allowed_tools_json must be a JSON array")  # noqa: TRY004
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(
                "mcp_server %r has invalid allowed_tools_json (%s) — treating as empty allowlist", row["name"], exc
            )
            allowed_tools = []
        cache.append(
            {
                "id": str(row["id"]),
                "name": row["name"],
                "url": row["url"],
                # Operator-authored one-liner shown to the model in the
                # hybrid tool surface's server directory (2026-10-28).
                # Empty is fine: the directory falls back to name + count.
                "description": row.get("description", "") or "",
                "headers": {str(k): str(v) for k, v in headers.items()},
                # K8s Secret NAME (by-ref) — resolved to a Bearer header at
                # call time via PCAI_LLM_MCP_KEY__<NAME>; never a literal key.
                "auth_secret": row["auth_secret"],
                "auth_token_enc": row["auth_token_enc"],
                "enabled": bool(row["enabled"]),
                "tool_policy": row["tool_policy"] or "allowlist",
                "allowed_tools": [str(t) for t in allowed_tools],
                # Failover/listing order carrier (lower = preferred).
                "priority": int(row["priority"] or 0),
            }
        )
    # Single-statement swap: readers before this see the old snapshot, after
    # it the new one — never a mix.
    _mcp_servers_cache = cache
    log.info("registry refresh: %d MCP server(s) enabled", len(cache))

    # Fire registry-change hooks AFTER the swap: consumers invalidate derived
    # caches against the NEW snapshot, never a torn one. Error-swallowed (see
    # the hook list note).
    for hook in _registry_change_hooks:
        try:
            hook()
        except Exception:
            log.debug("mcp registry-change hook failed", exc_info=True)
