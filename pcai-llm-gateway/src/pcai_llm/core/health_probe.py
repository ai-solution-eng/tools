"""Active endpoint health probe (GOAL A).

WHY IT EXISTS: Pool.healthy() is fed exclusively by traffic that happens to
arrive — passive availability through the breaker. An endpoint whose upstream
rejects every call (a 400 on every request) is a live defect: with no traffic
its breaker never sees a failure, so the idle-but-broken model reads healthy
forever and keeps ranking as free/idle. This probe adds a traffic-independent
signal: every interval it GETs {base_url}/models on each endpoint of every
chat|embedding|reranker pool and reports the verdict into the pool
(Pool.report_probe), where Pool.healthy() excludes probe_ok=False endpoints.

DESIGN CONTRACT:
  * ONE background asyncio task, started from the app lifespan, cancelled on
    shutdown, never raising into it (every fault inside the loop is logged and
    swallowed — a probe bug must not crash the gateway).
  * CREDENTIALS: the probe presents the endpoint's OWN credential through
    core.upstream_auth — the same resolution the forward path uses.
  * VERDICTS (_classify): 2xx = ok; a transport error or 5xx = fail;
    401/403 = OK (the server answered, so it is alive — our credential is the
    problem, and availability is not authorization); any OTHER 4xx = UNKNOWN
    (our probe request was wrong — path shape, method, a server with no
    /models card — so the endpoint's state is left untouched).
  * HYSTERESIS: probe_ok flips to False only after
    settings.health_probe_failures_to_trip CONSECUTIVE failures, the same
    posture the breaker gives real traffic.
  * NEVER GATES A POOL: Pool.healthy() ignores the probe entirely when it
    would exclude every endpoint (see its NEVER-EMPTY INVARIANT).

  2026-11-03 INCIDENT — WHY THESE RULES EXIST: the first shipped version was
  auth-less and treated every non-2xx as a failure. An upstream that gates
  /models answered 401, the whole fleet was marked probe-failed, every pool
  went empty and every chat request answered "503 no healthy upstream".
  * FAIL-OPEN: an endpoint that has never been probed (probe_ok is None) — a
    cold pod before the first tick, a probe-disabled deployment, or a probe
    that died — stays HEALTHY. The probe degrades an endpoint, it never
    gates one.
  * Log ONLY transitions (ok->fail, fail->ok): a broken endpoint is marked
    once per interval, and a WARNING line per interval would spam the log for
    the life of the outage.
  * interval <= 0 (or the flag off) = inert no-op: no task is created at all.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx2

from ..config import settings
from ..routing.router import Endpoint, Pool
from ..routing.router import router as model_router
from . import upstream_auth

log = logging.getLogger(__name__)

# The model types the probe covers. NOTE the codebase spelling: routes_admin
# registers "reranker" (MODEL_TYPES = chat|embedding|reranker|voice|decision);
# "reranking" appears in no registry row. Voice pools are excluded on purpose
# (a TTS/ASR /models card is not a liveness signal the chat/embedding gates
# consume), and so is "decision" (not an OpenAI-compatible endpoint shape).
_PROBED_MODEL_TYPES = ("chat", "embedding", "reranker")

# Concurrency cap on one sweep: a fleet sweep fires N upstream GETs at once;
# the cap keeps a large registry from presenting as a small DoS to its own
# model servers (8 concurrent /models GETs is already well past a real sweep
# on the current fleet size).
_SWEEP_CONCURRENCY = 8


def _classify(status: int) -> tuple[bool | None, str]:
    """Probe verdict from a /models status code. PURE, total.

    Returns (ok, error) where ok is:
      * True  — the endpoint answered and is usable (2xx).
      * False — the endpoint is NOT usable: 5xx, or a transport fault handled
        by the caller.
      * None  — UNKNOWN: our PROBE request was rejected in a way that says
        nothing about the model. The caller must leave the endpoint's state
        untouched.

    WHY THE 4xx SPLIT (2026-11-03 INCIDENT, fleet-wide 503): the probe first
    treated EVERY non-2xx as a failure. An upstream that auth-gates /models
    then marked the entire fleet probe-failed, Pool.healthy() emptied every
    pool, and every chat request answered "503 no healthy upstream".

    * 401/403 — the server ANSWERED, so it is alive; our credential is the
      problem. Availability is not authorization: the forward path surfaces
      the 401 honestly. Counting it as downtime hides the real fault behind a
      fleet outage.
    * any other 4xx (404/405/400/422) — the PROBE is wrong (path shape,
      method, a server with no /models card). Our own misconfiguration must
      never remove a healthy model from rotation.
    """
    if 200 <= status < 300:
        return True, ""
    if status in (401, 403):
        return True, f"auth-gated /models ({status})"
    if 500 <= status < 600:
        return False, f"upstream status {status}"
    return None, f"probe unsupported ({status})"


_task: asyncio.Task | None = None


async def run_once(client: httpx2.AsyncClient | None = None, router=None) -> None:
    """Probe every probed endpoint once and report each verdict into its pool.

    Test seam: inject `client` (e.g. an httpx2.MockTransport client) and/or
    `router` (a fresh Router with hand-built pools); both default to the live
    app singletons. Never raises — a fault on one endpoint is recorded as a
    fail for that endpoint only, and an unexpected fault anywhere in the sweep
    is logged and swallowed (the caller's loop must survive it).
    """
    r = router if router is not None else model_router
    if client is None:
        client = r.client

    # Snapshot (pool, endpoint) pairs first: a concurrent registry refresh
    # (replace_pools) swaps the pool dict mid-sweep, and the snapshot keeps
    # this one against that (reporting into a just-replaced pool is still
    # safe — report_probe only stamps the endpoint object).
    targets: list[tuple[Pool, Endpoint]] = []
    for name in r.models():
        pool = r.pool(name)
        if pool is None:
            continue
        for ep in pool.endpoints:
            if ep.model_type in _PROBED_MODEL_TYPES and ep.base_url:
                targets.append((pool, ep))
    if not targets:
        return

    sem = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def _probe_one(pool: Pool, ep: Endpoint) -> None:
        async with sem:
            prev_ok = ep.probe_ok
            prev_error = ep.probe_error
            # _classify returns bool | None (None = "our probe was rejected,
            # which says nothing about the model"), so the variable must carry
            # that union — the except arm alone would narrow it to bool.
            ok: bool | None
            error: str
            try:
                resp = await client.get(
                    ep.base_url.rstrip("/") + "/models",
                    # CREDENTIALS (2026-11-03 INCIDENT): present the SAME
                    # credential the forward path presents. The probe shipped
                    # auth-less, so an upstream that gates /models answered
                    # 401 and the whole fleet read probe-failed. quiet=True:
                    # the shared resolver logs per call, and a sweep every
                    # 30 s must not turn a missing credential into a flood.
                    headers=upstream_auth.upstream_headers(ep, "", quiet=True),
                    timeout=settings.health_probe_timeout_s,
                )
            except httpx2.HTTPError as exc:
                ok, error = False, type(exc).__name__
            else:
                ok, error = _classify(resp.status_code)
                if ok is None:
                    # UNKNOWN: our probe was rejected, which says nothing about
                    # the model. Record the reason for the admin surface and
                    # leave probe_ok EXACTLY as it was.
                    ep.probe_at = time.time()
                    if prev_error != error:
                        log.warning(
                            "health probe: endpoint %s (model %s) %s — probe state left unchanged",
                            ep.id,
                            ep.model_name,
                            error,
                        )
                    ep.probe_error = error
                    return
            pool.report_probe(ep, ok, error if not ok else "")
            # Log ONLY transitions: prev_ok None = never probed (the first
            # tick after boot logs nothing — no noise on a healthy fleet),
            # and a repeated fail/ok is the same state, not an event.
            if prev_ok is None and not ok:
                # FIRST-TICK SILENCE was a real defect (2026-11-03): the whole
                # fleet went probe-failed on the first sweep and the log said
                # nothing, because "None -> fail" was not counted as a
                # "transition". A cold start that fails the probe is the
                # loudest case there is.
                log.warning(
                    "health probe: endpoint %s (model %s) UNREACHABLE on the first probe: %s",
                    ep.id,
                    ep.model_name,
                    error,
                )
            elif prev_ok is not None and prev_ok != ok:
                if ok:
                    log.info(
                        "health probe: endpoint %s (model %s) recovered: %s -> ok",
                        ep.id,
                        ep.model_name,
                        prev_error or "fail",
                    )
                else:
                    log.warning(
                        "health probe: endpoint %s (model %s) degraded: ok -> %s",
                        ep.id,
                        ep.model_name,
                        error,
                    )

    try:
        await asyncio.gather(*(_probe_one(pool, ep) for pool, ep in targets))
    except Exception:
        # Defense in depth: _probe_one already swallows HTTP-level faults; this
        # guard covers the unexpected (a semaphore bug, a client closed mid-
        # sweep) so the loop below never has to handle it.
        log.warning("health probe: sweep aborted", exc_info=True)


async def _loop() -> None:
    # interval captured at START: settings are process-lifetime (env-rendered
    # by the chart), so re-reading per tick would change cadence mid-flight
    # with no way for the operator to have set it.
    interval = settings.health_probe_interval_s
    while True:
        # Sleep FIRST: a cold pod's first registry refresh just finished, and
        # the first tick 30 s later is when the fleet learns anything — no
        # point adding N /models GETs to the boot burst.
        try:
            await asyncio.sleep(interval)
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # the loop never dies; shutdown is via cancel()
            log.warning("health probe: iteration failed", exc_info=True)


async def start() -> None:
    """Start the probe loop (idempotent; called from the app lifespan).

    Inert no-op when the flag is off or the interval is <= 0: no task is
    created, and every endpoint's probe_ok stays None (fail-open healthy).
    """
    global _task
    if not settings.health_probe_enabled or settings.health_probe_interval_s <= 0:
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="health-probe")
    log.info("health probe started (interval %.1fs)", settings.health_probe_interval_s)


async def stop() -> None:
    """Cancel the loop (idempotent, never raises — shutdown must survive it)."""
    global _task
    task = _task
    _task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        log.warning("health probe: stop did not complete cleanly", exc_info=True)


def reset() -> None:
    """Test seam (same contract as housekeeping.reset)."""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None


__all__ = ["reset", "run_once", "start", "stop"]
