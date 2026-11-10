"""OTLP/HTTP tracing contract: the hand-rolled exporter, the app span, sampling.

WHY these tests exist: the gateway ships NO opentelemetry SDK (hard constraint
— no dependency, no network at image build), so the wire format is hand-written
JSON and the only thing between "a span" and "a collector that ingests it" is
the shape pinned here. Equally, tracing is OFF by default and must cost nothing
and break nothing: the failure modes that matter are a disabled path that still
dials out, an exporter that can fail a request, and a query string (API key,
prompt, token) leaking into a collector.

What is pinned:
  (a) disabled  => no HTTP call at all (and a shared no-op span: no allocation);
  (b) enabled   => exactly ONE POST to {endpoint}/v1/traces with the documented
                   OTLP/JSON body (the client is monkeypatched — no network);
  (c) an exporter failure is swallowed and does not poison later spans;
  (d) the query string never reaches span attributes (path only);
  (e) sample ratio 0 exports nothing, 1 exports everything, and the decision is
      a deterministic per-trace hash (same trace id => same decision).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pcai_llm.config import settings
from pcai_llm.core import tracing

# ---------------------------------------------------------------------------
# seams: a recording client (no network) and isolated module state
# ---------------------------------------------------------------------------


class _Response:
    status_code = 200


class FakeClient:
    """Records every POST; optionally raises (the exporter must swallow)."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error
        self.closed = False

    async def post(
        self,
        url: str,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _Response:
        self.calls.append({"url": url, "content": content, "headers": dict(headers or {}), "timeout": timeout})
        if self.error is not None:
            raise self.error
        return _Response()

    async def aclose(self) -> None:
        self.closed = True

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.calls[index]["content"])

    def span(self, index: int = 0) -> dict[str, Any]:
        return self.body(index)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

    def attrs(self, index: int = 0) -> dict[str, str]:
        """Attribute map flattened to text.

        intValue/doubleValue are stringified so the existing assertions read
        the same as before; the WIRE type is asserted separately in
        test_int_attributes_use_int_value.
        """
        out: dict[str, str] = {}
        for a in self.span(index)["attributes"]:
            _kind, raw = next(iter(a["value"].items()))
            out[a["key"]] = str(raw)
        return out


@pytest.fixture(autouse=True)
def _isolated_exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Tracing off, no shared client, no pending tasks — every test starts clean.

    _PENDING is swapped for a fresh set (not cleared): a task left behind by a
    failing test belongs to a loop that is about to close, and flushing it from
    the NEXT test's loop would raise across event loops.
    """
    monkeypatch.setattr(tracing, "_PENDING", set())
    monkeypatch.setattr(tracing, "_CLIENT", None)
    monkeypatch.setattr(settings, "otel_enabled", False)
    monkeypatch.setattr(settings, "otel_sample_ratio", 1.0)
    yield


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(tracing, "_get_client", lambda: client)
    return client


def _enable(monkeypatch: pytest.MonkeyPatch, *, ratio: float = 1.0) -> None:
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_sample_ratio", ratio)


# ---------------------------------------------------------------------------
# ASGI seams for the middleware tests
# ---------------------------------------------------------------------------


def _ok_app(status: int = 200) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return app


async def _drive(middleware: ASGIApp, scope: Scope) -> list[Message]:
    """Run one ASGI call through the middleware, returning what it sent."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return sent


def _scope(method: str = "GET", path: str = "/v1/models", query: bytes = b"") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


# ---------------------------------------------------------------------------
# (a) disabled => no HTTP call, and the span is a shared no-op
# ---------------------------------------------------------------------------


async def test_disabled_exports_nothing(fake: FakeClient) -> None:
    span = tracing.start_span("GET /health", {"http.method": "GET"})
    span.end({"http.status_code": 200})
    await tracing.flush()
    assert tracing.enabled() is False
    assert fake.calls == []


def test_disabled_start_span_is_a_shared_noop() -> None:
    """The disabled path allocates nothing: one singleton, no per-call object."""
    first = tracing.start_span("GET /a", {"http.method": "GET"})
    second = tracing.start_span("GET /b")
    assert isinstance(first, tracing.NoopSpan)
    assert first is second


async def test_disabled_middleware_passes_through_without_exporting(fake: FakeClient) -> None:
    from pcai_llm.app import ServerSpanMiddleware

    sent = await _drive(ServerSpanMiddleware(_ok_app()), _scope(path="/health"))
    await tracing.flush()
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert fake.calls == []


async def test_non_http_scopes_are_passed_through(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """Lifespan/websocket scopes must never become server spans."""
    from pcai_llm.app import ServerSpanMiddleware

    _enable(monkeypatch)
    seen: list[str] = []

    async def lifespan_app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    await ServerSpanMiddleware(lifespan_app)({"type": "lifespan", "asgi": {"version": "3.0"}}, _receive, _send)
    await tracing.flush()
    assert seen == ["lifespan"]
    assert fake.calls == []


async def _receive() -> Message:  # pragma: no cover - never called by a lifespan app
    return {"type": "lifespan.startup"}


async def _send(message: Message) -> None:  # pragma: no cover - never called
    return None


# ---------------------------------------------------------------------------
# (b) enabled => exactly one POST with the documented OTLP/JSON shape
# ---------------------------------------------------------------------------


async def test_enabled_posts_one_span_with_the_documented_shape(
    monkeypatch: pytest.MonkeyPatch, fake: FakeClient
) -> None:
    _enable(monkeypatch)
    span = tracing.start_span("GET /v1/models", {"http.method": "GET", "http.target": "/v1/models"})
    span.end({"http.status_code": 200, "duration_ms": "12.500"})
    await tracing.flush()

    assert len(fake.calls) == 1, f"expected exactly one POST, got {len(fake.calls)}"
    call = fake.calls[0]
    assert call["url"] == "http://otel-collector:4318/v1/traces"
    assert call["headers"]["content-type"] == "application/json"
    assert call["timeout"] == tracing._HTTP_TIMEOUT_S, "the export must ride a SHORT timeout"

    body = fake.body()
    assert list(body) == ["resourceSpans"]
    resource_span = body["resourceSpans"][0]
    assert resource_span["resource"]["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "pcai-llm-gateway"}}
    ]
    scope_span = resource_span["scopeSpans"][0]
    assert scope_span["scope"] == {"name": "pcai_llm"}

    assert len(scope_span["spans"]) == 1
    span_doc = scope_span["spans"][0]
    assert set(span_doc) == {
        "traceId",
        "spanId",
        "name",
        "kind",
        "startTimeUnixNano",
        "endTimeUnixNano",
        "attributes",
        "status",
    }
    assert re.fullmatch(r"[0-9a-f]{32}", span_doc["traceId"]), "traceId must be 32 lowercase hex chars"
    assert re.fullmatch(r"[0-9a-f]{16}", span_doc["spanId"]), "spanId must be 16 lowercase hex chars"
    assert span_doc["name"] == "GET /v1/models"
    assert span_doc["kind"] == 2, "kind 2 is SPAN_KIND_SERVER"
    assert isinstance(span_doc["startTimeUnixNano"], str) and span_doc["startTimeUnixNano"].isdigit()
    assert isinstance(span_doc["endTimeUnixNano"], str) and span_doc["endTimeUnixNano"].isdigit()
    assert int(span_doc["endTimeUnixNano"]) >= int(span_doc["startTimeUnixNano"])
    assert span_doc["status"] == {"code": 1}, "code 1 is STATUS_CODE_OK"
    assert fake.attrs() == {
        "http.method": "GET",
        "http.target": "/v1/models",
        "http.status_code": "200",
        "duration_ms": "12.500",
    }
    # Attribute VALUES are TYPED on the wire: an int rides as intValue so a
    # backend reads it as a number (Langfuse's usage columns), a string stays
    # stringValue. Nothing else is emitted.
    by_key = {attr["key"]: attr["value"] for attr in span_doc["attributes"]}
    assert by_key["http.status_code"] == {"intValue": "200"}, "int -> intValue"
    assert by_key["http.method"] == {"stringValue": "GET"}, "str -> stringValue"
    assert by_key["duration_ms"] == {"stringValue": "12.500"}, "str -> stringValue"


async def test_int_attributes_use_int_value(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """Token counts must be numbers, not strings, or Langfuse files them in
    metadata instead of the usage column."""
    _enable(monkeypatch)
    tracing.start_span(
        "chat",
        {"gen_ai.usage.input_tokens": 123, "gen_ai.usage.output_tokens": 0, "langfuse.trace.name": "chat"},
    ).end()
    await tracing.flush()
    by_key = {a["key"]: a["value"] for a in fake.span()["attributes"]}
    assert by_key["gen_ai.usage.input_tokens"] == {"intValue": "123"}
    assert by_key["gen_ai.usage.output_tokens"] == {"intValue": "0"}
    assert by_key["langfuse.trace.name"] == {"stringValue": "chat"}


async def test_error_flag_sets_status_code_2(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    _enable(monkeypatch)
    tracing.start_span("GET /boom").end(error=True)
    await tracing.flush()
    assert fake.span()["status"] == {"code": 2}, "code 2 is STATUS_CODE_ERROR"


async def test_endpoint_and_service_name_come_from_settings(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    _enable(monkeypatch)
    monkeypatch.setattr(settings, "otel_endpoint", "http://collector.example:4318/")
    monkeypatch.setattr(settings, "otel_service_name", "gw-test")
    tracing.start_span("GET /health").end()
    await tracing.flush()
    assert fake.calls[0]["url"] == "http://collector.example:4318/v1/traces"
    assert fake.body()["resourceSpans"][0]["resource"]["attributes"][0]["value"]["stringValue"] == "gw-test"


async def test_end_is_idempotent(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """A double end() (a finally plus an explicit close) must not double-export."""
    _enable(monkeypatch)
    span = tracing.start_span("GET /health")
    span.end({"http.status_code": 200})
    span.end({"http.status_code": 500})
    await tracing.flush()
    assert len(fake.calls) == 1
    assert fake.attrs()["http.status_code"] == "200"


async def test_bool_attributes_are_lowercase_strings(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    _enable(monkeypatch)
    tracing.start_span("GET /health", {"flag": True}).end()
    await tracing.flush()
    assert fake.attrs()["flag"] == "true"


# ---------------------------------------------------------------------------
# (c) exporter failures are swallowed
# ---------------------------------------------------------------------------


async def test_export_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """A dead collector must never surface in the request path (or the loop)."""
    _enable(monkeypatch)
    fake.error = RuntimeError("collector unreachable")
    tracing.start_span("GET /health").end()
    await tracing.flush()  # must not raise
    assert len(fake.calls) == 1, "the POST was attempted"


async def test_export_failure_does_not_poison_later_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    client = FakeClient(error=RuntimeError("boom"))
    monkeypatch.setattr(tracing, "_get_client", lambda: client)
    tracing.start_span("GET /one").end()
    await tracing.flush()
    client.error = None  # collector comes back
    tracing.start_span("GET /two").end()
    await tracing.flush()
    assert len(client.calls) == 2
    assert client.span(1)["name"] == "GET /two"


def test_end_without_a_running_loop_is_swallowed(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """A span closed in a sync context (interpreter shutdown, a sync test) must
    not raise just because there is no loop to schedule the export on."""
    _enable(monkeypatch)
    tracing.start_span("sync-context").end()  # no running loop here
    assert fake.calls == []


async def test_flush_and_aclose_are_noops_when_nothing_is_pending() -> None:
    await tracing.flush()  # no pending exports
    await tracing.aclose()  # no client
    assert tracing._CLIENT is None


async def test_aclose_closes_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    monkeypatch.setattr(tracing, "_CLIENT", client)
    await tracing.aclose()
    assert client.closed is True
    assert tracing._CLIENT is None


# ---------------------------------------------------------------------------
# (d) the query string never reaches the attributes
# ---------------------------------------------------------------------------


async def test_query_string_never_reaches_the_exporter(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """A query can carry an API key or a prompt: the span records the PATH only.

    Driven at the middleware with a real query_string, then the WHOLE exported
    body is scanned — the guarantee must hold everywhere in the payload, not
    just on the attribute we happen to name.
    """
    from pcai_llm.app import ServerSpanMiddleware

    _enable(monkeypatch)
    secret = "hllm-SECRET-KEY"
    sent = await _drive(
        ServerSpanMiddleware(_ok_app(200)),
        _scope(method="POST", path="/v1/chat/completions", query=f"api_key={secret}&stream=true".encode()),
    )
    await tracing.flush()

    assert sent[0]["status"] == 200
    raw = fake.calls[0]["content"].decode()
    assert secret not in raw, "the query string leaked into the span payload"
    assert "api_key" not in raw and "stream=true" not in raw
    attrs = fake.attrs()
    assert attrs["http.target"] == "/v1/chat/completions"
    assert attrs["http.method"] == "POST"


async def test_server_span_records_status_and_duration(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    from pcai_llm.app import ServerSpanMiddleware

    _enable(monkeypatch)
    await _drive(ServerSpanMiddleware(_ok_app(201)), _scope(method="PUT", path="/admin/keys"))
    await tracing.flush()

    span_doc = fake.span()
    assert span_doc["name"] == "PUT /admin/keys"
    assert span_doc["status"] == {"code": 1}
    attrs = fake.attrs()
    assert attrs["http.status_code"] == "201"
    assert float(attrs["duration_ms"]) >= 0.0


async def test_server_span_marks_5xx_as_error(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    from pcai_llm.app import ServerSpanMiddleware

    _enable(monkeypatch)
    await _drive(ServerSpanMiddleware(_ok_app(503)), _scope(path="/ready"))
    await tracing.flush()
    assert fake.span()["status"] == {"code": 2}
    assert fake.attrs()["http.status_code"] == "503"


async def test_server_span_marks_a_raising_app_as_error(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    """The exception still propagates (handlers above need it); the span closes."""
    from pcai_llm.app import ServerSpanMiddleware

    _enable(monkeypatch)

    async def boom(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("handler exploded")

    with pytest.raises(RuntimeError, match="handler exploded"):
        await _drive(ServerSpanMiddleware(boom), _scope(path="/v1/chat/completions"))
    await tracing.flush()
    assert fake.span()["status"] == {"code": 2}
    assert fake.attrs()["http.status_code"] == "0", "no response start was sent"


async def test_unsampled_middleware_span_is_a_noop(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> None:
    from pcai_llm.app import ServerSpanMiddleware

    _enable(monkeypatch, ratio=0.0)
    await _drive(ServerSpanMiddleware(_ok_app()), _scope(path="/health"))
    await tracing.flush()
    assert fake.calls == []


# ---------------------------------------------------------------------------
# (e) sampling: ratio 0 => none, ratio 1 => all, hash-deterministic per trace
# ---------------------------------------------------------------------------


def test_ratio_zero_samples_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_sample_ratio", 0.0)
    assert tracing.should_sample(tracing.new_trace_id()) is False
    assert isinstance(tracing.start_span("GET /x"), tracing.NoopSpan)


def test_ratio_one_samples_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "otel_enabled", True)
    monkeypatch.setattr(settings, "otel_sample_ratio", 1.0)
    for _ in range(50):
        span = tracing.start_span("GET /x")
        assert type(span) is tracing.Span, "ratio 1 must never drop a span"
        assert tracing.should_sample(span.trace_id) is True


async def test_ratio_zero_exports_nothing_and_one_exports_all(
    monkeypatch: pytest.MonkeyPatch, fake: FakeClient
) -> None:
    _enable(monkeypatch, ratio=0.0)
    for _ in range(5):
        tracing.start_span("GET /x").end()
    await tracing.flush()
    assert fake.calls == []

    _enable(monkeypatch, ratio=1.0)
    for _ in range(5):
        tracing.start_span("GET /x").end()
    await tracing.flush()
    assert len(fake.calls) == 5


def test_sampling_is_deterministic_per_trace_id() -> None:
    """The SAME trace id always makes the same decision — that is what lets a
    child span inherit its parent's sampling without propagation state."""
    trace_id = "0123456789abcdef0123456789abcdef"
    decisions = {tracing.should_sample(trace_id, 0.5) for _ in range(25)}
    assert len(decisions) == 1


def test_sampling_ratio_is_a_fraction_not_a_coin_flip() -> None:
    """A hash-based ratio must actually sample ~ratio of traces, not all/none."""
    ids = [f"{i:032x}" for i in range(1000)]
    sampled = sum(1 for trace_id in ids if tracing.should_sample(trace_id, 0.25))
    fraction = sampled / len(ids)
    assert 0.18 <= fraction <= 0.32, f"ratio 0.25 sampled {fraction:.3f} of 1000 fixed traces"


def test_child_span_shares_the_parent_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)
    parent = tracing.start_span("parent")
    child = tracing.start_span("child", {"hop": "model"}, trace_id=parent.trace_id)
    assert child.trace_id == parent.trace_id
    assert child.span_id != parent.span_id
    assert type(child) is tracing.Span


# ---------------------------------------------------------------------------
# app wiring
# ---------------------------------------------------------------------------


def test_app_registers_the_server_span_middleware() -> None:
    """The app actually mounts it — and the duration middleware stays OUTERMOST
    (its position is pinned by tests/test_backend_hardening.py; the span sits
    directly inside it, losing only the header stamp from its window)."""
    from pcai_llm.app import DurationHeaderMiddleware, ServerSpanMiddleware, create_app

    registered = [m.cls for m in create_app().user_middleware]
    assert ServerSpanMiddleware in registered
    assert registered[0] is DurationHeaderMiddleware
    assert registered[1] is ServerSpanMiddleware


async def test_no_event_loop_task_is_created_when_disabled(fake: FakeClient) -> None:
    """Zero measurable work when off: no task is even scheduled."""
    tracing.start_span("GET /health").end()
    assert tracing._PENDING == set()
    assert fake.calls == []
