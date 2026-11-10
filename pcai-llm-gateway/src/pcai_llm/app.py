"""FastAPI application factory: lifespan wiring, health/readiness, metrics.

CORS is deliberately absent: this is a server-to-server gateway plus an
operator UI served behind the same origin via Istio — cross-origin browser
traffic is not a supported path, so no CORS surface is exposed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
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
from .api.routes_team_leader import router as team_leader_router
from .config import settings
from .core import costs, health_probe, housekeeping, offpeak, registry, tracing
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


# CSP frame-ancestors token vocabulary (audit P3, 2026-11-10): an
# operator origin must be an exact scheme://host[:port][/path] token with
# NO quotes, semicolons, spaces or commas — CSP directives are
# space-separated inside one header, so any of those in an "origin" would
# be header-shaping, not naming. A non-matching token is DROPPED, not
# escaped: an unparseable origin can only shrink the allow-list back
# toward 'self', the safe direction.
_FRAME_ANCESTORS_TOKEN_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9._~%-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%!$&()*=:@%-]*)?$"
)


def _csp_frame_ancestors() -> str:
    """frame-ancestors value for the console CSP: 'self' plus any exact
    origins the operator allow-listed (settings.csp_frame_ancestors,
    comma-separated). Empty setting = 'self' only — the audit's fix for the
    previous 'self' https: default that let any https origin frame the
    admin/leader consoles."""
    tokens = []
    for raw in settings.csp_frame_ancestors.split(","):
        origin = raw.strip()
        if origin and _FRAME_ANCESTORS_TOKEN_RE.fullmatch(origin):
            tokens.append(origin)
    return "'self'" + ("" if not tokens else " " + " ".join(tokens))


class ServerSpanMiddleware:
    """One OpenTelemetry server span per HTTP request (pure ASGI, free when off).

    WHY pure ASGI: the same reason as DurationHeaderMiddleware below — this
    wraps every request, including SSE streams and the /mcp mount, and must not
    reintroduce Starlette's BaseHTTPMiddleware task/memory-stream layer.

    WHY the body is never read: a tracing layer that buffers the request body
    would break streaming uploads and cost memory on the 32 MB max_body_bytes
    path. The span carries routing/status facts only — method, PATH, status,
    duration.

    WHY the path and not the URL: scope["path"] structurally excludes the query
    string (that is scope["query_string"]). A query can carry an API key, a
    prompt, or a token; it must never reach a collector.

    Disabled (the default) this is one boolean read and a straight pass-through:
    no span object, no attribute dict, no task — see core.tracing.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Two independent entry conditions: a non-HTTP ASGI scope, or tracing off.
        if scope["type"] != "http" or not tracing.enabled():
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "/")
        span = tracing.start_span(f"{method} {path}", {"http.method": method, "http.target": path})
        started = time.monotonic()
        status_code = 0
        failed = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # A raising app has no response status: record it as an error span,
            # then let the app's exception handlers / the server deal with it.
            failed = True
            raise
        finally:
            # end() is a no-op for an unsampled span and never raises; on a
            # client disconnect (CancelledError) it still runs, so the span is
            # closed with whatever status the response reached.
            span.end(
                {
                    "http.status_code": status_code,
                    "duration_ms": f"{(time.monotonic() - started) * 1000:.3f}",
                },
                error=failed or status_code >= 500,
            )


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
    # Unpriced-model WARNING (audit 2026-11-10): a model with no model_costs
    # row ledgers cost NULL, which every USD budget gate's SUM ignores — the
    # gate is silently inert for that model (cost_fallback_per_m below is
    # the billing answer). One best-effort WARNING at startup names them
    # all; a cost-table fault must never block startup.
    try:
        # P10: hand the SAME guard the registry's model_type view, so the
        # boot-time warning also names unit MISCONFIGURATIONS (a priced model
        # whose model_costs.unit cannot match the unit its route bills — the
        # rows would ledger NULL instead of a wrong-rate bill). Injected as a
        # module attribute rather than a second parameter so the pinned
        # one-line guard call below keeps its shape
        # (tests/test_audit_20261110_ops.py); core.costs stays import-light.
        def _pool_types(name: str) -> set[str]:
            pool = model_router.pool(name)
            return {str(ep.model_type) for ep in pool.endpoints} if pool is not None else set()

        costs.types_lookup = _pool_types
        await costs.warn_unpriced(model_router.models())
    except Exception:
        log.debug("startup: unpriced-model check skipped", exc_info=True)
    # Model groups (F3): load the group map once so the very first request can
    # classify a group name, then start the load-snapshot loop that feeds the
    # member pick (queue depth per member, refreshed off the request path).
    # Both are best-effort and gated on the flag: a group feature must never
    # block startup, and with the flag off (the default) this block is one
    # boolean read.
    if settings.model_groups_enabled:
        try:
            from .core import model_groups as _model_groups

            await _model_groups.load(force=True)
        except Exception:
            log.warning("model groups: initial load failed — the TTL refresh will retry", exc_info=True)
        try:
            from .core import load_snapshot as _load_snapshot

            _load_snapshot.start()
        except Exception:
            log.warning("model groups: load snapshot did not start — picks fall back to random", exc_info=True)
    # Active health probe (GOAL A): passive availability (breaker-not-cooling,
    # traffic-fed) leaves an idle-but-broken model reading healthy forever.
    # The probe gives Pool.healthy() a traffic-independent signal (GET
    # {base_url}/models every interval). Inert (no task) when the flag is off
    # or the interval is <= 0; never raises into the lifespan (start/stop are
    # defensive by contract).
    await health_probe.start()
    if settings.offpeak_enabled:
        await offpeak.start()
    # Housekeeping sweep (audit finding 16): rate_windows + operator-opted
    # retention on a 60s timer — previously piggybacked on registry.refresh
    # (boot + admin writes only), so an idle-admin deployment grew
    # rate_windows for the process lifetime. Own loop, error-swallowed.
    await housekeeping.start()
    # Cross-replica convergence (2026-10-30): the registry snapshot is
    # process-local, so a mutation served by ONE replica left its siblings
    # stale until they restarted (console "test" 404s, relay "unknown
    # server"). The watcher polls a DB watermark and refreshes when it moves.
    # Started AFTER the boot refresh; its first tick (one cadence later) may do
    # one redundant refresh because no watermark is recorded yet — harmless, and
    # seeding one at boot would be WORSE: a write landing between the boot
    # refresh and the seed would be recorded as already-seen.
    await registry.start_watcher()
    try:
        yield
    finally:
        # Before the pool closes: the watcher's watermark query needs a
        # connection, and a cancel mid-query must not outlive db.close().
        await registry.stop_watcher()
        # Model groups: stop the load-snapshot loop during teardown so it
        # cannot outlive the app (its refresh reads Prometheus through
        # endpoint_metrics; stop() is a no-op when it never started).
        if settings.model_groups_enabled:
            try:
                from .core import load_snapshot as _load_snapshot

                await _load_snapshot.stop()
            except Exception:
                log.warning("model groups: load snapshot stop failed", exc_info=True)
        await health_probe.stop()
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
        # Tracing: drain in-flight OTLP exports, then close the exporter client.
        # Both are no-ops when tracing is off (no client, no pending task) and
        # neither can raise, so shutdown ordering is otherwise unchanged.
        await tracing.flush()
        await tracing.aclose()
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
    # Native /rerank spelling (no /v1) for clients that strip the prefix,
    # e.g. the RAG pipeline's MultiModalReranker.
    app.include_router(routes_proxy.rerank_alias_router)
    app.include_router(routes_admin.router)
    app.include_router(conversations_router)
    if settings.distiller_enabled:
        # session distiller: unmounted entirely when off (the routes_mcp_self pattern - an off surface 404s like a route that never existed); even when on, per-key attachment gates make it inert for keys the backend reports unattached (routes_distill.memory_attached)
        app.include_router(distill_router)
    app.include_router(enroll_router)
    app.include_router(enroll_admin_router)
    app.include_router(identity_router)
    # Team leader tier: the enrollment-scoped mini control plane. Its gate is
    # its OWN dependency (routes_team_leader.require_team_leader — a hash
    # match against enrollments.team_leader_key_hash), deliberately NOT the
    # admin dependency: a leader key must never reach /admin, and an admin
    # key is not a leader key. An enrollment with no leader key simply has no
    # valid credential here, so the surface 401s — there is nothing to mount
    # conditionally and no flag to turn on.
    app.include_router(team_leader_router)
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

    # OpenTelemetry server spans: ONE span per HTTP request (class docstring
    # has the WHY). Registered BEFORE the duration middleware so that one stays
    # the OUTERMOST layer — its position is pinned by
    # tests/test_backend_hardening.py, and the span loses nothing one layer in:
    # the only work outside it is the duration header stamp. Free when
    # PCAI_LLM_OTEL_ENABLED is false (one boolean read + pass-through).
    app.add_middleware(ServerSpanMiddleware)

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
            # - frame-ancestors: 'self' plus the operator's exact allow-list
            #   (_csp_frame_ancestors / settings.csp_frame_ancestors). The
            #   pages REMAIN deliberately iframe-safe (sessionStorage
            #   wrappers exist FOR iframe-blocked storage) — the change is
            #   WHO may frame them: the old 'self' https: default let any
            #   https origin frame the consoles, a clickjacking surface on
            #   destructive admin actions (audit P3, 2026-11-10). A site
            #   that embeds the console opts in by naming those exact
            #   origins; everyone else gets 'self'. Script execution stays
            #   same-origin; a framing parent cannot read the page, only
            #   display it.
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'self' data: https:; "
                f"frame-ancestors {_csp_frame_ancestors()}; "
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

        # Team leader console: the enrollment-scoped sibling of /admin. Same
        # static-page discipline (no data in the page; it prompts for the
        # leader key, keeps it in sessionStorage and sends it as a Bearer on
        # every fetch to /team_leader/*). Read in its OWN try: a slim install
        # that ships without this page must still serve / and /admin — the
        # outer guard already tolerates a missing ui/ directory, but a single
        # missing FILE must not take the other consoles down with it.
        try:
            team_leader_html = (resources.files("pcai_llm") / "ui" / "team_leader.html").read_text(encoding="utf-8")

            @app.get("/team_leader", include_in_schema=False)
            async def team_leader_console(request: Request) -> Response:
                """Team leader console (keys / advertised / usage / traces)."""
                return _ui_response(team_leader_html, request)
        except FileNotFoundError:
            log.warning("ui/team_leader.html not present; /team_leader console disabled")
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

    # --- SSO sign-out (browser path; public by design) ---------------------
    # WHY the gateway owns this path: the chat/admin consoles link "Sign out",
    # and behind the platform edge chain the oauth2-proxy session is a
    # DOMAIN-WIDE HttpOnly cookie that the page's JS cannot clear. Before this
    # route the link went to /oauth2/sign_out, which (on any deployment whose
    # VirtualService catch-all sends / to the gateway) reached FastAPI and
    # rendered {"detail":"Not Found"} — the cookie survived, so the user came
    # back "still signed in". That is the whole bug: a dead end that LOOKED
    # like a sign-out.
    #
    # Two shapes, both a redirect (never a 404):
    #   * PCAI_LLM_SSO_SIGN_OUT_URL set -> 302 to the proxy's own sign-out
    #     (cross-host is fine: the cookie is set on the parent domain, so the
    #     proxy can clear it and bounce back through rd);
    #   * unset -> 302 to "/?signout=local", which the console reads to say
    #     "signed out on this device; the platform session is still active"
    #     instead of pretending it ended. Honest beats silent.
    # A chart VirtualService route for /oauth2/ (ezua.virtualService.ssoProxy)
    # supersedes this handler entirely by sending the path to the proxy
    # directly — same-origin, no config needed. This is the fallback.
    @app.get("/oauth2/sign_out", include_in_schema=False)
    async def sso_sign_out(rd: str = "/") -> RedirectResponse:
        """End the browser session: proxy sign-out when configured, else an
        honest local-only redirect. Never a 404 (see the block comment)."""
        # rd is a RETURN path, so it must stay on this origin: a relative path
        # only (no scheme, no protocol-relative //host). Anything else would
        # make this endpoint an open redirect, which is a phishing primitive
        # on a page that holds bearer keys in sessionStorage.
        target = rd if rd.startswith("/") and not rd.startswith("//") else "/"
        configured = settings.sso_sign_out_url.strip()
        if not configured:
            return RedirectResponse("/?signout=local", status_code=302)
        # Preserve an existing query on the configured URL (some proxies mount
        # it with a required param) instead of assuming there is none.
        joiner = "&" if "?" in configured else "?"
        return RedirectResponse(f"{configured}{joiner}rd={quote(target, safe='')}", status_code=302)

    if settings.metrics_enabled:
        # Import-guarded per the fleet's additive-metrics convention: an
        # optional dependency must never take the whole gateway down.
        try:
            from prometheus_client import make_asgi_app

            app.mount("/metrics", make_asgi_app())
        except ImportError:
            log.warning("prometheus_client not installed; /metrics disabled")

    def _json_safe(value: Any) -> Any:
        """Replace non-finite floats so a 422 body can always be serialized.

        WHY (found in the 2026-10-30 cross-model review): pydantic echoes the
        offending INPUT back inside a validation error, and Starlette's
        JSONResponse is allow_nan=False. A body carrying NaN/Infinity — which
        Python's json module happily parses — therefore produced a 500 while
        BUILDING the 422, on every constrained float field in the API: the
        refusal was right and the response was unbuildable. Stringify only the
        non-finite values (the error type already says what was wrong); the
        payload shape is otherwise FastAPI's own.
        """
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, dict):
            return {k: _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(v) for v in value]
        return value

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """The default 422 body, made always-serializable (see _json_safe)."""
        return JSONResponse(status_code=422, content={"detail": _json_safe(exc.errors())})

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
