"""Dependency-free OTLP/HTTP tracing: one server span per HTTP request.

WHY hand-rolled: the OpenTelemetry SDK is NOT a dependency of this gateway and
must not become one (the fleet builds images without network access, and the
SDK drags a large transitive tree for what this deployment actually needs — a
server span per HTTP request, exported as OTLP/HTTP JSON). The wire format is
therefore built HERE, on top of the httpx2 client the gateway already ships.
The constraints that shaped it:

1. OFF MEANS FREE. `settings.otel_enabled` defaults to False; when it is off
   `enabled()` is one attribute read, `start_span()` returns a shared
   NoopSpan singleton, and the ASGI middleware is a straight pass-through —
   no span object, no attribute dict, no task, no HTTP client construction.

2. FIRE AND FORGET. `Span.end()` never awaits and never raises: the export is
   an `asyncio.create_task` POST with a short timeout, and EVERY exception is
   swallowed to debug. A collector that is down, slow or misconfigured must
   never add latency to (or fail) a gateway request — observability does not
   get to spend the request path's error budget. `flush()` is the shutdown
   and test drain for the in-flight exports.

3. SAMPLING IS PER-TRACE AND DETERMINISTIC. The decision is
   `sha256(trace_id) < ratio * 2**64`, so the SAME trace id always makes the
   same decision: a child span created with its parent's trace id can never be
   sampled differently from its parent, which is what keeps a partial trace
   coherent instead of a random subset of hops.

4. NO SECRETS, NO HIGH CARDINALITY. Callers pass explicit attributes; the
   app-level span carries method, PATH (never the query string — a query can
   hold an API key, a prompt, or a token) and status/duration only. The
   exporter adds nothing beyond `service.name`.

The wire shape is the OTLP/HTTP JSON protobuf mapping — one resourceSpans
entry per span (one POST per span, which keeps the exporter stateless and is
fine at this span volume):

    POST {otel_endpoint}/v1/traces
    {"resourceSpans":[{"resource":{"attributes":[{"key":"service.name",
      "value":{"stringValue":NAME}}]},"scopeSpans":[{"scope":{"name":
      "pcai_llm"},"spans":[{"traceId":32hex,"spanId":16hex,...}]}]}]}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Mapping
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

# OTLP/HTTP JSON conventions (opentelemetry-proto): span kind 2 = SERVER,
# status code 1 = OK, 2 = ERROR, 0 = UNSET.
_SPAN_KIND_SERVER = 2
_STATUS_OK = 1
_STATUS_ERROR = 2

_TRACES_PATH = "/v1/traces"
_SCOPE_NAME = "pcai_llm"

# Export budget: a collector is in-cluster, so 2s is generous. It bounds the
# background task, NOT the request — nothing waits on it.
_HTTP_TIMEOUT_S = 2.0

# Attribute values are strings on the wire (stringValue). Cap the length so one
# pathological attribute cannot turn into a multi-megabyte POST.
_ATTR_VALUE_LIMIT = 1024

# One shared AsyncClient (connection reuse across exports). Lazily created so
# the disabled path never even imports httpx2.
_CLIENT: Any = None

# Strong references to in-flight export tasks: asyncio only holds a WEAK
# reference to a task, so a fire-and-forget task can be garbage-collected
# mid-flight ("Task was destroyed but it is pending") without this set.
_PENDING: set[asyncio.Task[None]] = set()


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """True when tracing is switched on.

    Deliberately a LIVE read of settings (not a value cached at import): the
    flag is a deployment knob, and tests flip it after import. One attribute
    read is the whole cost of the disabled path.
    """
    return bool(settings.otel_enabled)


def should_sample(trace_id: str, ratio: float | None = None) -> bool:
    """Deterministic per-trace sampling decision.

    Hash-based, so the decision depends ONLY on the trace id (plus the ratio):
    repeated calls with the same trace id always agree, which is what makes a
    child span inherit its parent's sampling without any propagation state.
    ratio is clamped by comparison, not arithmetic: <= 0 drops everything,
    >= 1 keeps everything.
    """
    if ratio is None:
        ratio = settings.otel_sample_ratio
    if ratio >= 1.0:
        return True
    if ratio <= 0.0:
        return False
    digest = hashlib.sha256(trace_id.encode("utf-8", "replace")).digest()
    return int.from_bytes(digest[:8], "big") < ratio * float(1 << 64)


def new_trace_id() -> str:
    """A W3C-shaped trace id: 16 random bytes, 32 lowercase hex chars."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """A W3C-shaped span id: 8 random bytes, 16 lowercase hex chars."""
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# spans
# ---------------------------------------------------------------------------


class Span:
    """A live span. Created by start_span(); ALWAYS closed with end().

    end() is idempotent and never raises: the first call builds the OTLP
    payload and schedules the POST, later calls are no-ops. Attributes given at
    start are merged with the ones given at end (end wins), because status and
    duration are only known when the work finishes.
    """

    __slots__ = (
        "attributes",
        "ended",
        "name",
        "parent_span_id",
        "span_id",
        "start_ns",
        "trace_id",
    )

    def __init__(
        self,
        name: str,
        trace_id: str,
        span_id: str,
        start_ns: int,
        attributes: Mapping[str, Any] | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.start_ns = start_ns
        self.attributes: dict[str, Any] = dict(attributes or {})
        self.parent_span_id = parent_span_id
        self.ended = False

    def end(self, extra_attrs: Mapping[str, Any] | None = None, *, error: bool = False) -> None:
        """Close the span and schedule the export. Never raises, never awaits."""
        if self.ended:
            return
        self.ended = True
        attrs = dict(self.attributes)
        if extra_attrs:
            attrs.update(extra_attrs)
        payload = _payload(
            name=self.name,
            trace_id=self.trace_id,
            span_id=self.span_id,
            start_ns=self.start_ns,
            end_ns=time.time_ns(),
            attributes=attrs,
            error=error,
            parent_span_id=self.parent_span_id,
        )
        try:
            _submit(payload)
        except Exception:  # a span must never break the caller's finally block
            log.debug("tracing: could not schedule span export", exc_info=True)


class NoopSpan(Span):
    """The disabled / unsampled stand-in: a shared singleton that records nothing.

    Subclasses Span so call sites type-check against one return type while the
    disabled path allocates exactly zero objects.
    """

    __slots__ = ()

    def __init__(self) -> None:
        pass

    def end(self, extra_attrs: Mapping[str, Any] | None = None, *, error: bool = False) -> None:
        return None


_NOOP = NoopSpan()


def start_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    start_ns: int | None = None,
) -> Span:
    """Start a span, or return the shared NoopSpan when tracing is off/unsampled.

    trace_id is optional: pass a parent span's trace id to make this a CHILD of
    the same trace (and inherit the parent's deterministic sampling decision).
    parent_span_id records the parent so the backend can NEST the child under
    it; it does not influence sampling — the trace id decides that.
    """
    if not settings.otel_enabled:
        return _NOOP
    tid = trace_id or new_trace_id()
    if not should_sample(tid):
        return _NOOP
    return Span(
        name,
        tid,
        new_span_id(),
        start_ns if start_ns is not None else time.time_ns(),
        attributes,
        parent_span_id=parent_span_id,
    )


# ---------------------------------------------------------------------------
# OTLP/HTTP JSON exporter
# ---------------------------------------------------------------------------


def _attr_value(value: Any) -> dict[str, Any]:
    """Map one attribute value to its OTLP/JSON value object.

    Numeric attributes go on the wire as NUMBERS, not strings: Langfuse reads
    token counts from `intValue` and files a stringified count into its
    metadata catch-all instead of the usage column. bool is tested BEFORE int
    because bool is a subclass of int in Python. Everything else stays
    stringValue — the shape this exporter has always emitted, so existing
    collectors see no change.
    """
    if isinstance(value, bool):
        return {"stringValue": "true" if value else "false"}
    if isinstance(value, int):
        # OTLP/JSON encodes int64 as a string.
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)[:_ATTR_VALUE_LIMIT]}


def _payload(
    *,
    name: str,
    trace_id: str,
    span_id: str,
    start_ns: int,
    end_ns: int,
    attributes: Mapping[str, Any],
    error: bool,
    parent_span_id: str | None = None,
) -> dict[str, Any]:
    """The documented OTLP/HTTP JSON body for ONE span.

    parentSpanId is omitted (not null) when absent: an absent parent makes the
    span the root of its trace, and an explicit null is not valid OTLP.
    """
    span: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": _SPAN_KIND_SERVER,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": [
            {"key": str(key), "value": _attr_value(value)} for key, value in attributes.items()
        ],
        "status": {"code": _STATUS_ERROR if error else _STATUS_OK},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": str(settings.otel_service_name)}}
                    ]
                },
                "scopeSpans": [{"scope": {"name": _SCOPE_NAME}, "spans": [span]}],
            }
        ]
    }


def _get_client() -> Any:
    """The shared exporter client (created on first export, never when off).

    Built through routing.router.upstream_client, NOT httpx2.AsyncClient: the
    L-1 rule (audit 2026-10-29) is that every client talking to an in-cluster
    service pins trust_env=False, so ambient HTTP_PROXY/NO_PROXY can neither
    reroute span exports through a host the gateway does not control nor crash
    construction (a bracketed-IPv6 NO_PROXY raises inside AsyncClient.__init__).
    tests/test_trust_env.py enforces the single-factory rule repo-wide.
    """
    global _CLIENT
    if _CLIENT is None:
        import httpx2

        from ..routing.router import upstream_client

        _CLIENT = upstream_client(timeout=httpx2.Timeout(_HTTP_TIMEOUT_S))
    return _CLIENT


def _traces_url() -> str:
    """{otel_endpoint}/v1/traces, tolerating a trailing slash in the config."""
    return settings.otel_endpoint.rstrip("/") + _TRACES_PATH


def _submit(payload: dict[str, Any]) -> None:
    """Schedule one export as a background task (no-op without a running loop).

    get_running_loop() BEFORE building the coroutine: create_task() would
    otherwise leave an un-awaited coroutine behind on the no-loop path (a
    RuntimeWarning whenever a span is closed outside a loop, e.g. at
    interpreter shutdown).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no running loop (sync context, interpreter shutdown)
        log.debug("tracing: no running event loop; dropping span")
        return
    task = loop.create_task(_export(payload))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


def _export_headers() -> dict[str, str]:
    """Request headers for the OTLP POST: content-type plus configured extras.

    settings.otel_headers follows the OpenTelemetry convention — a
    comma-separated list of Key=Value pairs, the same shape as
    OTEL_EXPORTER_OTLP_HEADERS, e.g.

        Authorization=Basic <base64>,x-langfuse-ingestion-version=4

    Parsed here rather than stored as a dict so the deployment knob stays a
    single env var. An empty setting returns content-type alone, which is
    exactly today's behaviour.
    """
    headers = {"content-type": "application/json"}
    for pair in (settings.otel_headers or "").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


async def _export(payload: dict[str, Any]) -> None:
    """POST one OTLP/JSON batch. EVERY failure is swallowed to debug.

    This runs detached from the request that produced the span, so there is no
    caller to raise to — and no caller that should care: a dropped span is a
    missing data point, never a failed request.
    """
    url = _traces_url()
    try:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        client = _get_client()
        await client.post(
            url,
            content=body,
            headers=_export_headers(),
            timeout=_HTTP_TIMEOUT_S,
        )
    except Exception:
        log.debug("tracing: OTLP export to %s failed", url, exc_info=True)


async def flush(timeout: float = _HTTP_TIMEOUT_S + 1.0) -> None:
    """Await in-flight exports (shutdown + tests). Never raises, never hangs."""
    pending = [task for task in _PENDING if not task.done()]
    if not pending:
        return
    try:
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
    except TimeoutError:
        log.debug("tracing: %d span export(s) still in flight at flush timeout", len(pending))
    except Exception:  # pragma: no cover - a drain must never break shutdown
        log.debug("tracing: flush failed", exc_info=True)


async def aclose() -> None:
    """Close the shared exporter client (shutdown). Never raises."""
    global _CLIENT
    client, _CLIENT = _CLIENT, None
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        log.debug("tracing: exporter client close failed", exc_info=True)
