"""FastAPI application factory: lifespan wiring, health/readiness, metrics.

CORS is deliberately absent: this is a server-to-server gateway plus an
operator UI served behind the same origin via Istio — cross-origin browser
traffic is not a supported path, so no CORS surface is exposed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .api import routes_admin, routes_proxy
from .api.routes_admin import warn_short_admin_keys
from .api.routes_conversations import router as conversations_router
from .api.routes_distill import router as distill_router
from .api.routes_effective import router as effective_router
from .api.routes_enroll import admin_router as enroll_admin_router
from .api.routes_enroll import router as enroll_router
from .api.routes_identity import router as identity_router
from .api.routes_mcp import mount as mount_mcp
from .config import settings
from .core import housekeeping, offpeak, registry
from .db import PoolUnavailable, db
from .routing.router import router as model_router

log = logging.getLogger(__name__)


class DurationHeaderMiddleware:
    """Pure-ASGI middleware stamping true SERVER-side handling duration.

    WHY: the console's [slow] telemetry measured client WALL time —
    fetch() start to headers — which includes browser queuing and
    VPN/ingress connection setup. A burst of parallel admin GETs made the
    last few report ~3s despite ~1ms handlers ("[slow] GET /offpeak took
    3219ms" on a handler that touches a module dict). With this header
    the console warns on SERVER time, so the banner means what it says.

    Pure non-blocking math — two monotonic reads; no lock, no I/O. On
    streaming responses the header is stamped on http.response.start, i.e.
    before the body streams (unchanged from the BaseHTTPMiddleware version).

    WHY pure ASGI (not @app.middleware("http")): that decorator wraps every
    request — including SSE streams and the /mcp mount — in Starlette's
    BaseHTTPMiddleware, an extra task + memory-stream layer with known
    streaming-path overhead (and cancellation edge cases). This class adds
    nothing per-request but the header itself.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pass-through for lifespan/websocket
            await self.app(scope, receive, send)
            return
        started = time.monotonic()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.append("X-PCAI-Duration-Ms", str(int((time.monotonic() - started) * 1000)))
            await send(message)

        await self.app(scope, receive, send_wrapper)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bring up DB pool -> router client -> registry caches; reverse on shutdown.

    Order matters: db.connect() must succeed before anything else because the
    registry refresh reads model_endpoints/mcp_servers rows; the router client
    (httpx2 pool) must exist before the registry hands endpoints to proxies;
    the registry refresh runs last so the very first request is routable.
    The off-peak billing evaluator (when enabled) starts AFTER the registry
    refresh — it resolves its MCP server from that snapshot — and is stopped
    before the pool closes (guarded: only ever started when the flag is on,
    and stop() is a no-op when nothing is running).
    """
    warn_short_admin_keys()
    await db.connect()
    # router.start() may not exist in every router build — call it only if
    # present so app wiring survives sibling-module evolution.
    if hasattr(model_router, "start"):
        await model_router.start()
    await registry.refresh()
    if settings.offpeak_enabled:
        await offpeak.start()
    # Housekeeping sweep (audit finding 16): rate_windows + operator-opted
    # retention on a 60s timer — previously piggybacked on registry.refresh
    # (boot + admin writes only), so an idle-admin deployment grew
    # rate_windows for the process lifetime. Own loop, error-swallowed.
    await housekeeping.start()
    try:
        yield
    finally:
        await housekeeping.stop()
        if settings.offpeak_enabled:
            await offpeak.stop()
        if hasattr(model_router, "close"):
            await model_router.close()
        # F6 (cross-review 2026-10-16): flush detached sticky writes before
        # the pool closes — a shutdown that lands between the head pick and
        # the upsert would silently drop the pick (next turn re-picks, but
        # the write is cheap and already in flight). Guarded import: the
        # cascade module owns the task set.
        try:
            from .core.cascade import drain_sticky_writes

            await asyncio.wait_for(drain_sticky_writes(), timeout=5.0)
        except Exception:  # shutdown must never hang on a pick write
            log.warning("sticky-write drain at shutdown did not complete cleanly", exc_info=True)
        await db.close()


def create_app() -> FastAPI:
    """Build the gateway app (no CORS, no globals mutation — pure factory)."""
    app = FastAPI(
        title="PCAI LLM Gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,  # control-plane surface stays minimal; no swagger on prod pods
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(routes_proxy.router)
    app.include_router(routes_admin.router)
    app.include_router(conversations_router)
    if settings.distiller_enabled:
        # session distiller: unmounted entirely when off (the routes_mcp_self pattern - an off surface 404s like a route that never existed); even when on, per-key attachment gates make it inert for keys without the memory pair (routes_distill.memory_attached)
        app.include_router(distill_router)
    app.include_router(enroll_router)
    app.include_router(enroll_admin_router)
    app.include_router(identity_router)
    # effective-prompt review (wave C): the admin gate lives in
    # routes_admin.require_admin, imported — not copied — by this router.
    app.include_router(effective_router)
    if settings.mcp_self_service:
        # Self-service MCP management (2026-10): keyed users manage their
        # OWN key's MCP credential overrides and entitlement-bounded tool
        # grants. Import-guarded AND mount-guarded on the flag (the
        # routes_dispatch pattern): flag off = surface unmounted, not 404.
        from .api import routes_mcp_self

        app.include_router(routes_mcp_self.router)
    if settings.client_tool_dispatch:
        # Client tool-server dispatch surface (ROADMAP §7 phase 2 wave B2):
        # the offer/poll/result/offer-introspection routes the off-cluster
        # client long-polls to serve its local tools. Import-guarded AND
        # mount-guarded on the flag: flag off (the default) leaves the
        # surface unmounted and unimported — no route exists to even 401.
        from .api import routes_dispatch

        app.include_router(routes_dispatch.router)
    mount_mcp(app)  # MCP federation mounts its own routes/middleware

    # Pure-ASGI duration stamping (class docstring has the WHY). Registered
    # FIRST via add_middleware so it is the OUTERMOST layer: the header
    # measures the full stack, exactly as the decorator version did.
    app.add_middleware(DurationHeaderMiddleware)

    # --- chat UI (single static page; the browser talks to /v1 directly) ---
    # Import-guarded like /metrics: the UI ships in the wheel (package data)
    # but a slim install without it must still serve the API.
    try:
        from importlib import resources

        ui_index = resources.files("pcai_llm") / "ui" / "index.html"
        ui_html = ui_index.read_text(encoding="utf-8")

        # no-cache: the UIs are tiny and iterate fast — a stale cached page
        # (old JS, broken buttons) is far worse than a revalidation request.
        #
        # CSP (perf/sec round 2026-10-16, GLM explorer): defense-in-depth for
        # the innerHTML sinks (58 in admin.html, 21 in index.html) — the pages
        # carry admin bearer keys in sessionStorage, so an XSS slip is key
        # theft. Both pages are inline-script single pages with no third-party
        # origins, so 'unsafe-inline' is required for now (a nonce migration
        # is the later step); img-src keeps data: for the base64 favicon.
        # ETag: the bytes are read once at import, so a strong tag is free —
        # 304 revalidation preserves the no-cache semantics while skipping
        # the ~176 KB admin payload on unchanged consoles.
        UI_HEADERS = {
            "Cache-Control": "no-cache, must-revalidate",
            # CSP details (cross-review 2026-10-16, F3/F4):
            # - img-src allows https: — the chat console renders markdown
            #   ![](https://...) images by design (charset-restricted, escaped,
            #   lazy, no referrer); scripts/styles stay locked to 'self'.
            # - frame-ancestors 'self' https: — both pages are deliberately
            #   iframe-safe (sessionStorage wrappers exist FOR iframe-blocked
            #   storage), so 'none' would break console embedding. Script
            #   execution stays same-origin; a framing https: parent cannot
            #   read the page, only display it.
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'self' data: https:; "
                "frame-ancestors 'self' https:; "
                "base-uri 'self'; "
                "form-action 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
        }

        def _ui_response(html: str, request: Request) -> Response:
            """UI page with CSP/ETag headers and real 304 revalidation.

            F2 (cross-review 2026-10-16): Starlette's Response does NO
            conditional-request handling — an ETag alone never produces a
            304, so the payload was resent every time. Compare If-None-Match
            here (weak-comparison: strip any W/ prefix and quotes) and return
            an empty 304 carrying the same ETag when it matches."""
            etag = '"' + hashlib.sha256(html.encode("utf-8")).hexdigest() + '"'
            inm = request.headers.get("if-none-match", "")
            candidates = [v.strip().removeprefix("W/").strip('"') for v in inm.split(",")] if inm else []
            if etag.strip('"') in candidates:
                return Response(status_code=304, headers={**UI_HEADERS, "ETag": etag})
            headers = dict(UI_HEADERS)
            headers["ETag"] = etag
            return Response(html, media_type="text/html", headers=headers)

        @app.get("/", include_in_schema=False)
        async def ui_root(request: Request) -> Response:
            """Serve the chat UI at / (behind the Istio gateway this is the
            first thing a user hits)."""
            return _ui_response(ui_html, request)

        @app.get("/ui", include_in_schema=False)
        async def ui_alias(request: Request) -> Response:
            """Alias for / — bookmarkable, keeps the root free for future use."""
            return _ui_response(ui_html, request)

        # Admin console: same static-page pattern. The page itself holds NO
        # data — it prompts for the admin key (kept in sessionStorage, sent
        # as a Bearer header on every fetch; never a cookie, so no CSRF
        # surface) and talks to the /admin API. JSON discovery moved to
        # /admin/help so the browser gets the console at the natural path.
        admin_html = (resources.files("pcai_llm") / "ui" / "admin.html").read_text(encoding="utf-8")

        @app.get("/admin", include_in_schema=False)
        async def admin_console(request: Request) -> Response:
            """Admin console (keys / profiles / MCP / usage / models)."""
            return _ui_response(admin_html, request)
    except (ModuleNotFoundError, FileNotFoundError):
        log.warning("UI assets not present; / and /admin console disabled (API-only install)")

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness: process is up; no dependency checks (k8s probes use /ready)."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        """Readiness: a real DB round-trip; 503 (not 5xx-crash) so the Service
        keeps the pod out of rotation without restarting it."""
        try:
            ok = await db.fetchval("SELECT 1")
        except Exception:
            log.exception("readiness probe: database unreachable")
            raise HTTPException(503, "database unreachable") from None
        if ok is None:
            raise HTTPException(503, "database unreachable")
        return {"status": "ready"}

    if settings.metrics_enabled:
        # Import-guarded per the fleet's additive-metrics convention: an
        # optional dependency must never take the whole gateway down.
        try:
            from prometheus_client import make_asgi_app

            app.mount("/metrics", make_asgi_app())
        except ImportError:
            log.warning("prometheus_client not installed; /metrics disabled")

    @app.exception_handler(PoolUnavailable)
    async def pool_unavailable(request: Request, exc: PoolUnavailable) -> JSONResponse:
        """Bounded DB-pool acquisition ran out (perf audit): fail FAST with
        503 so the load balancer sheds load, instead of queueing every
        request behind a saturated pool. Opaque body, request id attached."""
        log.error("database pool unavailable on %s %s", request.method, request.url.path)
        request_id = getattr(request.state, "request_id", None) or request.headers.get("x-request-id", "")
        return JSONResponse(
            status_code=503,
            content={"error": "database busy, retry shortly", "request_id": request_id},
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        """Catch-all: log the traceback server-side, return an opaque 500.

        Exception payloads can carry SQL fragments, URLs, or secrets — none of
        that leaves the process. request_id lets an operator correlate the log
        line with the client-visible response.
        """
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        request_id = getattr(request.state, "request_id", None) or request.headers.get("x-request-id", "")
        return JSONResponse(
            status_code=500,
            content={"error": "internal error", "request_id": request_id},
        )

    return app
