#!/usr/bin/env python3
"""Gateway per-request framework-overhead benchmark (in-process ASGI).

WHAT THIS MEASURES
==================
Pure per-request ASGI framework overhead: the cost of walking each stack's
middleware chain, routing, dependency wiring, and response serialisation,
with the network stripped away. Four ASGI apps get the exact same workload:
raw ``scope``/``receive``/``send`` calls from ``--concurrency`` asyncio
worker tasks, timed with ``time.perf_counter_ns`` around the full
``await app(...)``:

  raw-asgi       minimal hand-written ASGI app returning a small JSON body.
                 The floor: what the event loop + one dict + two sends cost.
  starlette-bare bare Starlette app (no FastAPI), one JSONResponse route.
  fastapi        FastAPI app, one GET route, no gateway middleware.
  gateway        the real ``pcai_llm.app.create_app()`` (full middleware
                 stack incl. DurationHeaderMiddleware + FastAPI machinery),
                 hit ONLY on ``/health`` -- by design it does no DB work
                 ("Liveness: process is up; no dependency checks"), so the
                 delta over ``fastapi`` is the gateway's own routing and
                 middleware burden.

No sockets, no TLS, no client libs in mock-compare mode: a socket hop is
~50-200us of kernel + asyncio transport work and would drown the single-to
double-digit microseconds we are trying to see. The receive callable
returns one ``http.request`` then ``http.disconnect``; send consumes
messages; every request is a complete ASGI exchange. Workers yield to the
event loop between requests (never inside the timed window) so
``--concurrency`` is real interleaving, not cosmetic.

WHAT THIS DOES NOT MEASURE
==========================
No TLS handshake cost. No upstream GPU/model latency (no upstream is
called). No Postgres in mock-compare -- the gateway lifespan is
deliberately NEVER run there (pure ASGI calls never trigger it; Starlette
builds the middleware stack lazily on first request and warmup absorbs
that build). No TCP/kernel/transport cost. No serialization of real
prompt-sized bodies. Real end-to-end numbers come from the DEPLOYED
gateway's ``gateway_request_latency_seconds`` histogram (client-inclusive:
TLS, ingress, queueing, DB and upstream included) -- this script is the
complement: the pure framework share of that number.

# not-load-tested-against-production
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any

# Settings is fail-loud at import time: an incomplete database config raises
# before any app code runs. Same placement contract as tests/conftest.py --
# MUST be set before the first pcai_llm import anywhere below.
os.environ.setdefault("PCAI_LLM_DATABASE_URL", "postgresql://bench:bench@localhost:5432/bench")

# pcai_llm is imported lazily inside build_stacks()/_load_gateway_app() --
# NOT at module import -- so --help and argparse errors never pay the
# ~0.6s pcai_llm import cost. The env default above is already in place.
STACKS = ("raw-asgi", "starlette-bare", "fastapi", "gateway")

WARMUP_REQUESTS = 200
SERVER_STARTUP_TIMEOUT_S = 60.0

# ---------------------------------------------------------------- workload --

SCOPE_TEMPLATE: dict[str, Any] = {
    "type": "http",
    "http_version": "1.1",
    "method": "GET",
    "scheme": "http",
    "path": "/health",
    "raw_path": b"/health",
    "query_string": b"",
    "root_path": "",
    "headers": [(b"host", b"localhost")],
    "client": ("127.0.0.1", 12345),
    "server": ("127.0.0.1", 80),
    "asgi": {"version": "3.0", "spec_version": "2.3"},
}


def fresh_scope() -> dict[str, Any]:
    """A fresh scope per request.

    WHY both the dict copy AND the headers-list copy: a real server never
    reuses a scope, and middlewares mutate -- Starlette stores request
    state in the scope dict and MutableHeaders-style helpers may write
    through the headers list. A shallow dict copy alone would still share
    one headers list across every request of a run.
    """
    scope = dict(SCOPE_TEMPLATE)
    scope["headers"] = list(SCOPE_TEMPLATE["headers"])
    return scope


async def _receive():
    """Minimal ASGI receive: one empty http.request, then disconnect."""
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(message):
    """Minimal ASGI send: consumes response messages, does nothing."""
    return


# ------------------------------------------------------- mock-compare mode --

async def _measure_asgi(app: Any, concurrency: int, duration: float) -> dict[str, Any]:
    """Run the load loop against one ASGI app; return latency stats.

    ``concurrency`` worker tasks loop on one event loop. Each request is
    bracketed t0..t1 around ONLY the ``await app(...)``; the cooperative
    ``asyncio.sleep(0)`` between iterations lives OUTSIDE that bracket, so
    it costs harness scheduling, not measured latency -- but without it the
    workers would never yield and "concurrency" would be N serial loops
    (verified empirically: 4 workers x 0.5s ran 2.0s wall without the
    yield). Per-request latency brackets the FULL await: scope handoff
    through middleware to response completion.
    """
    counts: list[int] = [0] * concurrency
    latencies: list[list[int]] = [[] for _ in range(concurrency)]

    async def worker(wid: int) -> None:
        perf = time.perf_counter_ns
        lat = latencies[wid]
        n = 0
        deadline = perf() + int(duration * 1e9)
        while perf() < deadline:
            scope = fresh_scope()
            t0 = perf()
            await app(scope, _receive, _send)
            t1 = perf()
            lat.append(t1 - t0)
            n += 1
            # Hand the loop back so the N workers actually interleave.
            await asyncio.sleep(0)
        counts[wid] = n

    # asyncio.gather raises on worker crash: fail loud, never report a
    # partial stack (fail-fast discipline).
    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    flat = sorted(ns for lst in latencies for ns in lst)
    total = sum(counts)
    elapsed = duration  # workers stop at the deadline; actual drift < 1ms
    us = [x / 1000.0 for x in flat]

    def pct(p: float) -> float:
        # nearest-rank percentile on the sorted list (index 0 = fastest);
        # round() already yields an int here.
        idx = max(0, min(len(us) - 1, round(p / 100.0 * len(us)) - 1))
        return us[idx]

    return {
        "requests": total,
        "rps": total / elapsed,
        "p50": pct(50),
        "p90": pct(90),
        "p99": pct(99),
        "max": us[-1],
    }


def build_stacks() -> list[tuple[str, Any]]:
    """Build the four ASGI callables. Import pcai_llm LAST (env contract)."""
    # (a) raw-asgi: the floor. One JSON write, zero framework.
    async def raw_asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        body = b'{"ok":true}'
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    import starlette.applications
    import starlette.responses
    import starlette.routing

    # (b) starlette-bare: routing + JSONResponse, no FastAPI layer at all.
    async def bare_health(request):
        return starlette.responses.JSONResponse({"status": "ok"})

    starlette_bare = starlette.applications.Starlette(
        routes=[starlette.routing.Route("/health", bare_health, methods=["GET"])],
        lifespan=None,
    )

    import fastapi

    # (c) fastapi: FastAPI's own routing/dependency machinery, no gateway
    # middleware -- isolates "FastAPI tax" from "gateway tax".
    fastapi_app = fastapi.FastAPI()

    @fastapi_app.get("/health")
    async def fastapi_health() -> dict[str, str]:
        return {"status": "ok", "version": "bench"}

    # (d) gateway: the real thing. Import costs ~0.6s and instantiates
    # Settings (env setdefault at module top already ran). create_app()
    # wires the lifespan but building the app does NOT run it -- pure ASGI
    # calls never trigger it, and Starlette builds the middleware stack
    # lazily on the first request, which warmup absorbs. NEVER await
    # app.router.lifespan_context here: that is what would dial Postgres.
    from pcai_llm.app import create_app

    gateway = create_app()

    return [
        ("raw-asgi", raw_asgi),
        ("starlette-bare", starlette_bare),
        ("fastapi", fastapi_app),
        ("gateway", gateway),
    ]


async def run_mock_compare(args: argparse.Namespace) -> int:
    print(f"building stacks (mode={args.mode})...")
    stacks = build_stacks()
    print(
        f"load: concurrency={args.concurrency} duration={args.duration}s "
        f"rps-target={args.rps} (advisory, not enforced) -- in-process ASGI "
        f"calls (no sockets, no TLS)"
    )
    results: dict[str, dict[str, Any]] = {}
    for name, app in stacks:
        # Warmup: first hits build Starlette/FastAPI's middleware stack
        # lazily (apps build router/middleware on first __call__); never
        # let that one-off build pollute the timed window.
        for _ in range(WARMUP_REQUESTS):
            await app(fresh_scope(), _receive, _send)
        results[name] = await _measure_asgi(app, args.concurrency, args.duration)

    print()
    header = (
        f"{'stack':<16}{'reqs':>10}{'req/s':>11}{'p50 us':>10}{'p90 us':>10}"
        f"{'p99 us':>10}{'max us':>10}"
    )
    print(header)
    print("-" * len(header))
    for name in STACKS:
        r = results[name]
        print(
            f"{name:<16}{r['requests']:>10}{r['rps']:>11.0f}{r['p50']:>10.1f}"
            f"{r['p90']:>10.1f}{r['p99']:>10.1f}{r['max']:>10.1f}"
        )
    print()

    gw, raw = results["gateway"], results["raw-asgi"]
    print(
        f"gateway overhead vs raw-asgi: {gw['p50'] / raw['p50']:.1f}x (p50) / "
        f"{gw['p99'] / raw['p99']:.1f}x (p99)"
    )
    return 0


# ------------------------------------------------------------ gateway mode --

def _load_gateway_app():
    from pcai_llm.app import create_app

    return create_app()


async def _read_http_response(reader: asyncio.StreamReader) -> None:
    """Read one full HTTP/1.1 response off a keep-alive socket.

    Handles Content-Length and chunked framing. MUST drain the body: a
    half-read response leaves stale bytes on the socket and every
    subsequent request on that connection desynchronises.
    """
    status_line = await reader.readline()
    if not status_line:
        raise ConnectionResetError("server closed connection")
    headers: dict[bytes, bytes] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        k, _, v = line.partition(b":")
        headers[k.strip().lower()] = v.strip()
    clen = int(headers.get(b"content-length", b"0"))
    if clen:
        await reader.readexactly(clen)
    elif headers.get(b"transfer-encoding", b"").lower() == b"chunked":
        while True:
            size = int((await reader.readline()).split(b";")[0], 16)
            if size == 0:
                await reader.readexactly(2)  # final CRLF
                break
            await reader.readexactly(size + 2)  # chunk body + CRLF


async def _http_worker(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    duration: float,
    latencies_ms: list[float],
) -> None:
    """One persistent-connection worker: raw HTTP/1.1 GETs on one socket.

    Keep-alive: one TCP connect per worker, many requests per connection --
    measures server-side handling + socket round-trip, not TCP handshakes.
    """
    req = b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"
    deadline = time.perf_counter() + duration
    perf = time.perf_counter
    while perf() < deadline:
        t0 = perf()
        writer.write(req)
        await writer.drain()
        await _read_http_response(reader)
        latencies_ms.append((perf() - t0) * 1000.0)
        await asyncio.sleep(0)  # interleave with sibling connections


async def run_gateway_mode(args: argparse.Namespace) -> int:
    """Real uvicorn + sockets; requires a reachable Postgres (lifespan runs).

    The server runs as an asyncio TASK on this loop (uvicorn.Server.serve()
    is a plain coroutine) -- never a thread: a thread blocked on
    run_until_complete(serve()) cannot report `server.started` until the
    server STOPS, which is the classic deadlock this mode must avoid.
    """
    import uvicorn

    app_instance = _load_gateway_app()
    config = uvicorn.Config(
        app_instance, host="127.0.0.1", port=args.port, log_level="warning"
    )
    server = uvicorn.Server(config)

    async def _serve() -> int:
        # Wrap uvicorn's serve() so a lifespan-startup failure (uvicorn calls
        # sys.exit(STARTUP_FAILURE=3)) becomes a task RESULT instead of an
        # exception: asyncio re-raises SystemExit out of the event loop --
        # straight through asyncio.run, bypassing the startup-wait loop
        # below -- which would kill the process with a traceback and no
        # clear message. Converting it here keeps the failure handling in
        # ONE place (the startup check) and the exit-code contract exact.
        try:
            await server.serve()
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
        return 0

    serve_task = asyncio.create_task(_serve(), name="bench-uvicorn")

    # Wait for startup: uvicorn sets server.started after lifespan + bind.
    # A lifespan failure (Postgres unreachable) ends the task with the
    # STARTUP_FAILURE code captured above -- surface that as exit 3, loud,
    # instead of leaking a traceback.
    deadline = time.monotonic() + SERVER_STARTUP_TIMEOUT_S
    serve_exit_code: int | None = None
    while not server.started:
        if serve_task.done():
            serve_exit_code = await serve_task
            break
        if time.monotonic() > deadline:
            break
        await asyncio.sleep(0.05)

    if not server.started:
        cause = f" (uvicorn exit {serve_exit_code})" if serve_exit_code else " (timed out)"
        print(
            "gateway mode: uvicorn startup failed"
            f"{cause}. Lifespan requires a reachable Postgres for db.connect()"
            " / registry refresh -- start Postgres (e.g. bench DSN "
            f"{os.environ['PCAI_LLM_DATABASE_URL']!r}) or check that port "
            f"{args.port} is free, or use --mode mock-compare (in-process, "
            "no DB needed)."
        )
        return 3

    print(
        f"gateway mode: uvicorn serving create_app() on 127.0.0.1:{args.port} "
        f"(lifespan ran: DB connected)"
    )

    latencies_ms: list[float] = []
    conns: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    try:
        conns = [
            await asyncio.open_connection("127.0.0.1", args.port)
            for _ in range(args.concurrency)
        ]
        # Warmup over the SAME worker path so the first request's lazy
        # middleware-stack build and asyncio caching never pollute the
        # timed window; warm samples are discarded (perf-audit hygiene).
        warm: list[float] = []
        await asyncio.gather(
            *(
                _http_worker(r, w, min(0.5, args.duration), warm)
                for (r, w) in conns
            )
        )
        await asyncio.gather(
            *(
                _http_worker(r, w, args.duration, latencies_ms)
                for (r, w) in conns
            )
        )
    except (ConnectionError, OSError) as exc:
        print(f"gateway mode: connection to 127.0.0.1:{args.port} failed: {exc}")
        return 4
    finally:
        # Graceful shutdown: flip the flag, uvicorn finishes in-flight
        # requests, runs lifespan shutdown (pool close), serve() returns.
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(serve_task), timeout=10)
        except (TimeoutError, SystemExit):
            serve_task.cancel()
    if not latencies_ms:
        print("gateway mode: no completed requests -- is the server up?")
        return 4

    ms = sorted(latencies_ms)
    reqs = len(ms)

    def pct(p: float) -> float:
        idx = max(0, min(len(ms) - 1, round(p / 100.0 * len(ms)) - 1))
        return ms[idx]

    rps = reqs / args.duration
    print(f"requests={reqs} req/s={rps:.0f} (raw sockets, keep-alive, {args.concurrency} conns)")
    print(
        f"p50={pct(50):.2f}ms p90={pct(90):.2f}ms p99={pct(99):.2f}ms max={ms[-1]:.2f}ms"
    )
    print("note: these numbers INCLUDE the socket hop and keep-alive framing;")
    print("      compare against mock-compare's gateway row to see transport share.")
    return 0


# ------------------------------------------------------------------- main --

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bench_overhead",
        description="Per-request framework-overhead benchmark for the PCAI LLM gateway.",
    )
    p.add_argument(
        "--mode",
        choices=("mock-compare", "gateway"),
        default="mock-compare",
        help="mock-compare: in-process ASGI (default, no DB). gateway: real "
        "uvicorn with lifespan (needs reachable Postgres).",
    )
    # --rps: the load loop is a saturation loop (each worker fires requests
    # back-to-back for --duration), so the target rate is advisory only -- it
    # is accepted (and echoed in the run header) to keep CLI parity with the
    # deployed-gateway load profiles, but is not enforced. Documented so the
    # flag's presence never misleads a reader into thinking it throttles.
    p.add_argument(
        "--rps",
        type=int,
        default=200,
        help="advisory target req/s (the loop saturates; not a throttle)",
    )
    p.add_argument("--duration", type=float, default=15.0, help="seconds of load per stack")
    p.add_argument("--concurrency", type=int, default=32, help="parallel workers / connections")
    p.add_argument("--port", type=int, default=9292, help="port for --mode gateway")
    args = p.parse_args(argv)
    # Fail fast on nonsense before spending any load time.
    if args.duration <= 0:
        p.error(f"--duration must be > 0, got {args.duration}")
    if args.concurrency < 1:
        p.error(f"--concurrency must be >= 1, got {args.concurrency}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.mode == "gateway":
            return asyncio.run(run_gateway_mode(args))
        return asyncio.run(run_mock_compare(args))
    except KeyboardInterrupt:
        # Ctrl-C is a normal way to stop a bench: discard partial results,
        # never traceback, exit clean.
        print("\ninterrupted -- partial results discarded (fail-fast: no partial report)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
