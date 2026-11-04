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

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from ..config import settings
from ..db import db
from ..routing.router import Endpoint
from ..routing.router import router as model_router
from . import context_window, upstream_auth

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

    # --- learn each upstream's CONTEXT WINDOW (2026-10-29) ----------------
    # Every vLLM/SGLang /v1/models card publishes max_model_len, and without it
    # the gateway cannot solve the admission inequality
    # (prompt + max_tokens <= window) — the Qwen 400 that produced
    # "(model error 400)" as an assistant answer. Learned HERE rather than on
    # the request path because the window is a property of the ENDPOINT set and
    # because a transparent proxy must not add an upstream call per request.
    # Concurrent, bounded, backoff-gated (context_window.PROBE_RETRY_SECONDS),
    # and FAIL-OPEN: discovery can only ever add knowledge.
    try:
        entries = [
            (ep.base_url, upstream_auth.upstream_headers(ep)) for endpoints in by_model.values() for ep in endpoints
        ]
        await context_window.refresh_windows(entries)
    except Exception as exc:
        log.warning("registry refresh: context-window discovery skipped (%s)", exc)

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


# ---------------------------------------------------------------------------
# Cross-replica convergence (2026-10-30)
# ---------------------------------------------------------------------------
# WHY: the snapshot above is PROCESS-LOCAL, and refresh() fires only at boot
# and after a mutation THIS process served. On an HPA'd deployment
# (minReplicas 2) that made a newly registered MCP server visible to the
# replica that accepted the registration and invisible to its sibling until
# that pod restarted: the console's "test" button 404'd "not in the enabled
# registry" on roughly half the clicks, and the relay answered "unknown
# server" for the same server on the stale pod. Same failure mode as the
# 2026-10-29 context-window incident (housekeeping._context_window_sweep): a
# fact stored in the DB, cached per process, invalidated by an event only ONE
# process observes.
#
# FIX: a WATERMARK poll. Rows in mcp_servers/model_endpoints carry updated_at
# (bumped by a DB trigger, so out-of-band psql edits count too); the watcher
# re-reads the watermark on a short cadence and refreshes ONLY when it moved.
# Cheap (one aggregate over two tiny tables per tick), portable (no
# LISTEN/NOTIFY — that needs a dedicated connection and is silently dropped
# behind a transaction-mode pooler), and idempotent (refresh is a whole-
# snapshot swap, so a redundant pass costs two SELECTs, never correctness).
#
# TWO callers, deliberately:
#   * the watcher loop bounds the DATA plane's staleness to registry_watch_s;
#   * ensure_fresh() bounds the ADMIN CONSOLE's staleness to
#     registry_admin_fresh_s — the console is where a stale read is most
#     confusing, because the server row renders while its own "test" 404s.
#
# Neither replaces the local refresh after a committed mutation: that one must
# stay immediate, because the operator's very next request may land here. The
# watcher is what covers the pods that did NOT serve the write.

# (mcp rows, newest mcp write, endpoint rows, newest endpoint write).
# COUNT catches DELETEs; MAX(updated_at) catches INSERTs and UPDATEs. The
# timestamp values stay whatever asyncpg returns (datetime | None) — they are
# only ever compared for equality, so a clock step is detected too (the pair
# merely has to CHANGE, not increase). The one miss is two writes stamped on the
# identical microsecond; the next registry write moves the watermark again.
# (That is why the trigger uses clock_timestamp(), not now(): now() is
# transaction-start time and ties far more easily.)
Watermark = tuple[int, Any, int, Any]

_WATERMARK_SQL = """
SELECT (SELECT count(*) FROM mcp_servers) AS mcp_count,
       (SELECT max(updated_at) FROM mcp_servers) AS mcp_ts,
       (SELECT count(*) FROM model_endpoints) AS ep_count,
       (SELECT max(updated_at) FROM model_endpoints) AS ep_ts
"""

_last_watermark: Watermark | None = None
_last_check = 0.0
_refreshing = False
_watch_task: asyncio.Task[None] | None = None
_watch_failures = 0


async def _watermark() -> Watermark | None:
    """Read the registry watermark, or None when it cannot be established.

    None is a SKIP signal, never an error to propagate: a database that predates
    the updated_at columns (or a test double with no such columns) must degrade
    to "no opinion" rather than break the admin plane or log every tick. The
    caller keeps the snapshot it already has — exactly the behaviour this
    module had before it grew a watcher.
    """
    try:
        row = await db.fetchrow(_WATERMARK_SQL)
        if row is None:
            return None
        data = dict(row)
        return (int(data["mcp_count"]), data["mcp_ts"], int(data["ep_count"]), data["ep_ts"])
    except Exception:
        log.debug("registry watch: watermark unavailable", exc_info=True)
        return None


async def refresh_if_changed() -> bool:
    """Refresh when the DB watermark moved since the last refresh. True if it did.

    The watermark is read BEFORE the refresh and stored as-is. Storing the
    POST-refresh value would swallow any write that landed between the two: the
    next tick would then compare against a watermark that already includes a
    change the snapshot does not. Storing the pre-refresh value can only cost
    one redundant refresh, never a missed one.

    A refresh already in flight makes this a no-op (return False) rather than a
    second concurrent whole-snapshot swap: the in-flight one is reading the same
    rows, and the next tick re-reads the watermark anyway.

    The in-flight flag is taken BEFORE the first await, deliberately. Checking it
    after the watermark read leaves an await hole: a watcher tick and an admin
    console read can both pass the check before either sets it, and the OLDER of
    the two swaps can then land last — a stale snapshot behind a watermark that
    already advanced past it, uncorrected until the next registry write.
    """
    global _last_watermark, _refreshing
    if _refreshing:
        return False
    _refreshing = True
    try:
        current = await _watermark()
        if current is None or current == _last_watermark:
            return False
        await refresh()
        _last_watermark = current
    finally:
        _refreshing = False
    log.info(
        "registry watch: watermark moved (%d MCP server row(s), %d endpoint row(s)) — snapshot refreshed",
        current[0],
        current[2],
    )
    return True


async def ensure_fresh() -> None:
    """Refresh on demand when the watermark moved (admin console reads).

    Rate-limited to one watermark check per settings.registry_admin_fresh_s, so
    a console burst (the server list plus every "test" click) costs at most one
    extra query per interval. FAIL-OPEN BY CONTRACT: freshness is an
    optimization, so every failure leaves the current snapshot in place — this
    must never turn an admin GET into a 500, and must never be why a request is
    slow. The check-then-set below is deliberately lock-free: two requests
    racing it can only run one redundant refresh, while a module-level
    asyncio.Lock would bind this import-time module to one event loop.
    """
    global _last_check
    interval = settings.registry_admin_fresh_s
    if interval <= 0:
        return
    now = time.monotonic()
    if now - _last_check < interval:
        return
    _last_check = now
    try:
        await refresh_if_changed()
    except Exception:
        log.warning(
            "registry watch: on-demand refresh failed — keeping the current snapshot",
            exc_info=True,
        )


async def _watch_loop() -> None:
    global _watch_failures
    while True:
        await asyncio.sleep(settings.registry_watch_s)
        try:
            await refresh_if_changed()
            _watch_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            _watch_failures += 1
            # A 5 s cadence with a traceback every tick would bury the log
            # during a database outage: full detail on the first failure and
            # once a minute after that.
            log.warning(
                "registry watch: refresh failed (%d consecutive) — keeping the current snapshot",
                _watch_failures,
                exc_info=_watch_failures == 1 or _watch_failures % 12 == 0,
            )


async def start_watcher() -> None:
    """Start the cross-replica watermark poll (idempotent).

    Sleeping FIRST, then checking, is deliberate: the boot refresh() already
    ran, so an immediate tick would only re-read a watermark nothing has moved.
    """
    global _watch_task
    if settings.registry_watch_s <= 0:
        log.info("registry watch disabled (registry_watch_s <= 0) — single-replica assumption")
        return
    if _watch_task is not None and not _watch_task.done():
        return
    _watch_task = asyncio.create_task(_watch_loop(), name="registry-watch")
    log.info("registry watch started (cadence %.1fs)", settings.registry_watch_s)


async def stop_watcher() -> None:
    """Cancel the watcher (idempotent; safe when it was never started)."""
    global _watch_task
    task, _watch_task = _watch_task, None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
