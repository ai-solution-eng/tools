"""Endpoint-pool router with health/cooldown circuit breaker.

A virtual model name maps to N physical endpoints (vLLM/SGLang stacks,
quantization variants...). Selection: enabled + cooled-down endpoints,
sorted by (priority, weight) with weighted-random among equal priority.
This is the on-prem feature LiteLLM treats as an afterthought — the fleet's
own benchmarker shows 4x TTFT spread across cache regimes/quantizations,
so pool choice is a real lever.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2

from ..config import settings
from ..core import metrics as _metrics  # breaker gauge/counters (no-op shims w/o prometheus_client)


def upstream_client(**kwargs: Any) -> httpx2.AsyncClient:
    """The ONE constructor for every client that talks to an UPSTREAM
    service (model pools, admin probes, MCP sessions, enrollment RAG).

    L-1 (audit 2026-10-29): httpx2 defaults to trust_env=True, so ambient
    proxy environment leaks into these clients two ways —

    * a bracketed-IPv6 NO_PROXY entry (``[::1]``) makes URLPattern() raise
      httpx2.InvalidURL inside AsyncClient.__init__ — and in Router.start()
      that is the app lifespan, so the POD CRASH-LOOPS, not a 500;
    * a valid HTTP_PROXY silently reroutes every upstream call (prompts,
      API keys, completions) through a host this gateway does not control.

    Upstream targets are in-cluster, admin-registered URLs: proxies and
    other ambient env must never apply. trust_env is pinned False here and
    EVERY construction site must build through this factory (one place to
    audit). verify/limits/timeout policy stays with the caller as before.
    """
    kwargs["trust_env"] = False
    return httpx2.AsyncClient(**kwargs)


@dataclass
class Endpoint:
    id: str
    model_name: str
    base_url: str
    priority: int = 0
    weight: int = 1
    api_key_secret: str | None = None  # K8s Secret name (resolved by-ref, never inline)
    auth_token_enc: str | None = None  # Fernet ciphertext (admin panel path)
    model_type: str = "chat"  # chat|embedding|reranker|voice

    # runtime breaker state
    consecutive_failures: int = 0
    cooldown_until: float = 0.0

    # passive health surface (gaps P5, stamped by Pool.report; WALL-CLOCK
    # epoch seconds — unlike cooldown_until, these are for admin display)
    last_error: str = ""
    last_ok_at: float = 0.0
    last_fail_at: float = 0.0


@dataclass
class Pool:
    endpoints: list[Endpoint] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def healthy(self) -> list[Endpoint]:
        now = time.monotonic()
        return [e for e in self.endpoints if e.cooldown_until < now]

    def pick(self) -> Endpoint | None:
        """Weighted pick within the best (lowest) healthy priority tier.

        Semantics: priority orders tiers (failover); weight splits traffic
        WITHIN a tier. weight 0 = standby — it receives NO load-balanced
        traffic (it still serves via failover when nothing else in the tier
        is healthy). If every endpoint in the tier has weight 0, the tier is
        served in deterministic list order (strict priority order, no
        randomization) — "all weights zero = follow the order they were
        registered in".
        """
        candidates = self.healthy()
        if not candidates:
            return None
        best_priority = min(e.priority for e in candidates)
        tier = [e for e in candidates if e.priority == best_priority]
        if len(tier) == 1:
            return tier[0]
        weighted = [e for e in tier if e.weight > 0]
        if not weighted:
            return tier[0]  # all standby -> deterministic order
        return random.choices(weighted, weights=[e.weight for e in weighted], k=1)[0]

    async def report(self, ep: Endpoint, ok: bool, error: str = "") -> None:
        """Record one call outcome against the endpoint's breaker.

        Also stamps the passive health surface (last_ok_at/last_fail_at/
        last_error) so GET /admin/endpoints can show WHY an endpoint is
        cooling without a prober. `error` is a short reason (exception type
        name / upstream status) — never a full traceback or URL dump.
        """
        async with self._lock:
            if ok:
                ep.consecutive_failures = 0
                ep.last_ok_at = time.time()
                ep.last_error = ""
                # Gauge-only recovery signal: a success while a cooldown is
                # still pending does NOT shorten the cooldown (rotation
                # semantics unchanged — the endpoint stays out of healthy()
                # until cooldown_until passes); it only closes the Prometheus
                # gauge early so the alert doesn't fire for the full window.
                if ep.cooldown_until:
                    _metrics.breaker_recover(ep.id)
                return
            ep.consecutive_failures += 1
            ep.last_fail_at = time.time()
            if error:
                ep.last_error = error
            if ep.consecutive_failures >= settings.endpoint_failure_threshold:
                ep.cooldown_until = time.monotonic() + settings.endpoint_cooldown_s
                ep.consecutive_failures = 0
                _metrics.breaker_trip(ep.id)


class Router:
    """Holds one Pool per virtual model name."""

    def __init__(self) -> None:
        self._pools: dict[str, Pool] = {}
        self._client: httpx2.AsyncClient | None = None

    async def start(self) -> None:
        # Upstream TLS: verification is UNCONDITIONAL (config.py note — there
        # is deliberately no verify=False knob). A site with a private CA
        # points PCAI_LLM_UPSTREAM_CA_BUNDLE at a mounted bundle; empty means
        # httpx's default trust store.
        verify: str | bool = settings.upstream_ca_bundle or True
        # Pool limits (perf audit, 2026-10): the default httpx pool is 100
        # connections / 20 keepalive with a ~5s keepalive expiry — shared
        # across EVERY upstream (chat, embeddings, MCP relay). Each in-flight
        # SSE stream holds one connection for its whole completion, so past
        # 100 concurrent streams the 101st queues INSIDE the client with no
        # upstream-visible signal. upstream_pool_max_connections=0 keeps the
        # httpx defaults (byte-identical legacy behavior); keepalive_expiry
        # is matched to the uvicorn keep-alive (75s) so pooled consumers
        # don't re-handshake mid-stream.
        if settings.upstream_pool_max_connections > 0:
            self._client = upstream_client(
                timeout=httpx2.Timeout(settings.upstream_timeout_s, connect=10.0),
                verify=verify,
                limits=httpx2.Limits(
                    max_connections=settings.upstream_pool_max_connections,
                    max_keepalive_connections=settings.upstream_pool_max_keepalive,
                    keepalive_expiry=settings.upstream_pool_keepalive_expiry_s,
                ),
            )
        else:
            # byte-identical legacy path (no limits kwarg = httpx defaults)
            self._client = upstream_client(
                timeout=httpx2.Timeout(settings.upstream_timeout_s, connect=10.0), verify=verify
            )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    def set_endpoints(self, model_name: str, endpoints: list[Endpoint]) -> None:
        self._pools[model_name] = Pool(endpoints=endpoints)

    def replace_pools(self, pools: dict[str, list[Endpoint]]) -> None:
        """Whole-snapshot swap used by registry.refresh(): models absent from
        the snapshot are REMOVED (set_endpoints alone would serve stale pools
        for models disabled/removed in the DB until pod restart).

        Breaker state (consecutive_failures, cooldown_until) is CARRIED
        across the swap by endpoint id (perf audit): the old behavior reset
        every failure counter on each refresh, so a console save during an
        upstream outage handed a dead endpoint a fresh strike budget and
        kept it in rotation (each hit = a 10 s connect timeout for clients).
        """
        preserved: dict[str, tuple[int, float]] = {}
        for pool in self._pools.values():
            for e in pool.endpoints:
                if e.consecutive_failures or e.cooldown_until:
                    preserved[e.id] = (e.consecutive_failures, e.cooldown_until)
                elif e.id:
                    # An endpoint that refreshed with NO breaker state is
                    # healthy — clear a stale open gauge left by a previous
                    # generation of the same endpoint id (registry refresh).
                    _metrics.breaker_recover(e.id)
        new_pools: dict[str, Pool] = {}
        for m, eps in pools.items():
            for e in eps:
                state = preserved.get(e.id)
                if state is not None:
                    e.consecutive_failures, e.cooldown_until = state
            new_pools[m] = Pool(endpoints=eps)
        self._pools = new_pools

    def all_endpoints(self) -> list[Endpoint]:
        """Every live endpoint across all pools (admin health surface —
        breaker state is in-memory, so the admin view joins DB rows to
        these by endpoint id)."""
        return [e for pool in self._pools.values() for e in pool.endpoints]

    def models(self) -> list[str]:
        return sorted(self._pools)

    def pool(self, model_name: str) -> Pool | None:
        return self._pools.get(model_name)

    @property
    def client(self) -> httpx2.AsyncClient:
        assert self._client is not None, "router not started"
        return self._client

    @classmethod
    def with_mock(cls, handler) -> Router:
        """Test seam: a Router whose httpx2 client routes through `handler`.

        Production code never calls this; it exists so integration tests can
        inject an in-process upstream without monkeypatching private state.
        """
        r = cls()
        # L-1: through the shared factory — the seam must exercise the SAME
        # trust posture production traffic gets (ambient proxy env ignored).
        r._client = upstream_client(transport=httpx2.MockTransport(handler))
        return r


router = Router()
