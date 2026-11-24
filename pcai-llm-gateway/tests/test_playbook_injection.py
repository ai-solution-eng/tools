"""Pre-task playbook injection: recall -> preamble block, fail-open everywhere.

The feature is the READ half of procedural memory: when
settings.playbook_inject_enabled is true the relay entry recalls the top-k
playbooks for the turn task signature and appends a compact block to the
gateway system prefix (with_system_prefix), so every model call of the turn
starts from the recalled procedures. Everything here is DB-free and
network-free: the mm-rag recall call is stubbed, and the integration tests
drive the REAL relay loop with a scripted model stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pcai_llm.config import settings
from pcai_llm.core import playbook, toolrelay

RECORD: dict[str, Any] = {
    "memory_id": "m1",
    "playbook_key": "k1",
    "task_signature": "recover a stuck pod",
    "steps": ["check finalizers", "delete the pod", "verify it reschedules"],
    "failure_modes": ["forcing deletion before the finalizer clears"],
    "success_count": 6,
    "fail_count": 1,
    "score": 7 / 9,
    "evidence": "proven",
}

TASK = "recover a stuck pod that will not terminate"


def _envelope(records: list[dict], **extra: Any) -> str:
    """The mm-rag recall_playbooks JSON envelope around these records."""
    payload: dict[str, Any] = {
        "dataset": "mem",
        "task": TASK,
        "count": len(records),
        "records": records,
        "conflicts": [],
    }
    payload.update(extra)
    return json.dumps(payload)


class _Recall:
    """Stub recall client: records calls, returns (or raises) a scripted answer."""

    def __init__(self, answer: Any = "") -> None:
        self.answer = answer
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, task: str, k: int) -> str:
        self.calls.append((task, k))
        if isinstance(self.answer, Exception):
            raise self.answer
        if callable(self.answer):
            return str(self.answer(task, k))
        return str(self.answer)


def _block(records: list[dict] | None = None, **kwargs: Any) -> str:
    """The block the helpers render for these records (default: RECORD)."""
    return playbook.render_playbook_block(
        playbook.parse_recall_records(_envelope(records if records is not None else [RECORD])),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# signature derivation (the playbook.py task_signature contract)
# ---------------------------------------------------------------------------


def test_derive_task_signature_collapses_whitespace_to_one_line() -> None:
    assert playbook.derive_task_signature("  recover   a stuck\n pod  ") == "recover a stuck pod"


def test_derive_task_signature_rejects_text_too_short() -> None:
    """Under SIGNATURE_MIN_CHARS there is no task to recall for."""
    assert playbook.derive_task_signature("short") == ""
    assert playbook.derive_task_signature("") == ""
    assert playbook.derive_task_signature(None) == ""  # type: ignore[arg-type]


def test_derive_task_signature_bounds_to_the_field_contract() -> None:
    signature = playbook.derive_task_signature("word " * 200)
    assert len(signature) <= playbook.SIGNATURE_MAX_CHARS
    assert len(signature) >= playbook.SIGNATURE_MIN_CHARS


# ---------------------------------------------------------------------------
# response parsing + success-rate filtering
# ---------------------------------------------------------------------------


def test_success_rate_prefers_store_score_then_recomputes_from_counters() -> None:
    assert playbook.playbook_success_rate({"score": 0.25}) == 0.25
    # no score: the SAME Laplace smoothing the store ranks by
    assert playbook.playbook_success_rate({"success_count": 6, "fail_count": 1}) == 7 / 9
    # untried reads the prior, never 0 or 1
    assert playbook.playbook_success_rate({}) == 0.5
    assert playbook.playbook_success_rate({"score": True}) == 0.5  # a bool is not a rate


def test_parse_records_accepts_envelope_and_bare_list() -> None:
    from_envelope = playbook.parse_recall_records(_envelope([RECORD]))
    from_list = playbook.parse_recall_records(json.dumps([RECORD]))
    assert [r["task_signature"] for r in from_envelope] == ["recover a stuck pod"]
    assert from_envelope == from_list


def test_parse_records_drops_unrenderable_and_malformed_rows() -> None:
    no_steps = {**RECORD, "playbook_key": "k2", "steps": []}
    no_signature = {**RECORD, "playbook_key": "k3", "task_signature": "  "}
    rows = [RECORD, no_steps, no_signature, "not a record", 7]
    assert len(playbook.parse_recall_records(_envelope(rows))) == 1  # type: ignore[arg-type]
    assert playbook.parse_recall_records("not json") == []
    assert playbook.parse_recall_records("") == []
    assert playbook.parse_recall_records("null") == []


def test_parse_records_dedupes_the_same_playbook() -> None:
    """One stored playbook is never injected twice in a turn."""
    dupes = [RECORD, dict(RECORD), {**RECORD, "steps": list(RECORD["steps"])}]
    assert len(playbook.parse_recall_records(_envelope(dupes), k=5)) == 1


def test_parse_records_applies_the_success_rate_floor() -> None:
    weak = {**RECORD, "playbook_key": "weak", "success_count": 1, "fail_count": 5, "score": 2 / 8}
    records = playbook.parse_recall_records(_envelope([RECORD, weak]), min_success_rate=0.5)
    assert len(records) == 1
    assert [r["task_signature"] for r in records] == ["recover a stuck pod"]
    assert playbook.parse_recall_records(_envelope([RECORD]), min_success_rate=0.0) != []
    assert playbook.parse_recall_records(_envelope([RECORD]), min_success_rate=0.99) == []


def test_parse_records_caps_at_k() -> None:
    rows = [{**RECORD, "playbook_key": "k" + str(i), "task_signature": "task number " + str(i)} for i in range(5)]
    assert len(playbook.parse_recall_records(_envelope(rows), k=2)) == 2
    assert playbook.parse_recall_records(_envelope(rows), k=0) == []


# ---------------------------------------------------------------------------
# rendering + the char cap
# ---------------------------------------------------------------------------


def test_render_block_carries_signature_steps_and_failure_modes() -> None:
    block = _block()
    assert block.startswith(playbook.PLAYBOOK_BLOCK_HEADER + "\n")
    assert "recover a stuck pod [6 ok / 1 failed]" in block
    assert "1. check finalizers" in block
    assert "2. delete the pod" in block
    assert "3. verify it reschedules" in block
    assert "AVOID: forcing deletion before the finalizer clears" in block
    assert block.endswith("\n")


def test_render_block_marks_unproven_evidence() -> None:
    fresh = {**RECORD, "success_count": 0, "fail_count": 0, "score": 0.5}
    assert "[unproven]" in _block([fresh])


def test_render_block_empty_store_is_empty() -> None:
    assert playbook.render_playbook_block([]) == ""
    assert playbook.render_playbook_block([{}]) == ""


def test_render_block_caps_and_marks_truncation() -> None:
    rows = [{**RECORD, "playbook_key": "k" + str(i)} for i in range(8)]
    records = playbook.parse_recall_records(_envelope(rows), k=8)
    assert len(records) == 8
    capped = playbook.render_playbook_block(records, max_chars=300)
    assert len(capped) <= 300
    assert playbook.TRUNCATION_MARKER in capped
    full = playbook.render_playbook_block(records, max_chars=100_000)
    assert playbook.TRUNCATION_MARKER not in full
    assert len(full) > 300


def test_render_block_with_no_room_for_the_marker_is_empty() -> None:
    rows = [{**RECORD, "playbook_key": "k" + str(i)} for i in range(8)]
    records = playbook.parse_recall_records(_envelope(rows), k=8)
    assert playbook.render_playbook_block(records, max_chars=5) == ""


# ---------------------------------------------------------------------------
# recall_playbook_block: fail open, always
# ---------------------------------------------------------------------------


def _recall_block(recall: Any, **kwargs: Any) -> str:
    return asyncio.run(playbook.recall_playbook_block(TASK, recall=recall, **kwargs))


def test_recall_block_happy_path() -> None:
    recall = _Recall(_envelope([RECORD]))
    block = _recall_block(recall, k=2, timeout_ms=500)
    assert block == _block()
    assert recall.calls == [(TASK, 2)]


def test_recall_block_empty_store_is_empty() -> None:
    assert _recall_block(_Recall(_envelope([]))) == ""
    assert _recall_block(_Recall("")) == ""
    assert _recall_block(_Recall("not json")) == ""


def test_recall_block_without_a_client_is_empty() -> None:
    assert _recall_block(None) == ""


def test_recall_block_short_task_never_calls_the_store() -> None:
    recall = _Recall(_envelope([RECORD]))
    assert asyncio.run(playbook.recall_playbook_block("tiny", recall=recall)) == ""
    assert recall.calls == []


def test_recall_block_timeout_logs_once_and_fails_open(caplog: Any) -> None:
    async def slow(task: str, k: int) -> str:
        await asyncio.sleep(1.0)
        return _envelope([RECORD])

    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.playbook"):
        block = asyncio.run(playbook.recall_playbook_block(TASK, recall=slow, timeout_ms=20))
    assert block == ""
    warnings = [r for r in caplog.records if r.name == "pcai_llm.core.playbook"]
    assert len(warnings) == 1
    assert "timed out" in warnings[0].getMessage()


def test_recall_block_exception_logs_once_and_fails_open(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.playbook"):
        block = _recall_block(_Recall(RuntimeError("memory store down")))
    assert block == ""
    warnings = [r for r in caplog.records if r.name == "pcai_llm.core.playbook"]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# the relay seam: _with_playbook_block
# ---------------------------------------------------------------------------


def _seam(monkeypatch: Any, recall: Any, **overrides: Any) -> tuple[str, dict[str, int]]:
    """Run the relay seam with the feature ON and a stubbed recall client."""
    monkeypatch.setattr(settings, "playbook_inject_enabled", True)
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    calls = {"client": 0}

    async def _client(_key_id: Any) -> Any:
        calls["client"] += 1
        return recall

    monkeypatch.setattr(toolrelay, "_playbook_recall_client", _client)
    out = asyncio.run(
        toolrelay._with_playbook_block(
            "AGENT PROMPT\n", [{"role": "user", "content": TASK}], request_id="rid", key_id=None
        )
    )
    return out, calls


def test_disabled_path_is_byte_identical(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "playbook_inject_enabled", False)

    async def _must_not_be_built(_key_id: Any) -> Any:
        raise AssertionError("the recall client must not be built when the feature is off")

    monkeypatch.setattr(toolrelay, "_playbook_recall_client", _must_not_be_built)
    prefix = "AGENT PROMPT\nsecond line"
    out = asyncio.run(
        toolrelay._with_playbook_block(prefix, [{"role": "user", "content": TASK}], request_id="rid", key_id=None)
    )
    assert out is prefix  # the very same string: nothing was appended


def test_enabled_path_appends_the_block_after_the_prefix(monkeypatch: Any) -> None:
    recall = _Recall(_envelope([RECORD]))
    out, calls = _seam(monkeypatch, recall)
    assert out == "AGENT PROMPT\n" + _block()
    assert calls["client"] == 1
    assert recall.calls == [(TASK, settings.playbook_inject_k)]


def test_empty_store_returns_the_prefix_untouched(monkeypatch: Any) -> None:
    out, _calls = _seam(monkeypatch, _Recall(_envelope([])))
    assert out == "AGENT PROMPT\n"


def test_no_recall_client_returns_the_prefix_untouched(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "playbook_inject_enabled", True)

    async def _no_server(_key_id: Any) -> Any:
        return None

    monkeypatch.setattr(toolrelay, "_playbook_recall_client", _no_server)
    out = asyncio.run(
        toolrelay._with_playbook_block("P", [{"role": "user", "content": TASK}], request_id="rid", key_id=None)
    )
    assert out == "P"


def test_recall_client_failure_fails_open(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "playbook_inject_enabled", True)

    async def _registry_down(_key_id: Any) -> Any:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(toolrelay, "_playbook_recall_client", _registry_down)
    out = asyncio.run(
        toolrelay._with_playbook_block("P", [{"role": "user", "content": TASK}], request_id="rid", key_id=None)
    )
    assert out == "P"


def test_block_already_in_the_conversation_is_never_injected_twice(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "playbook_inject_enabled", True)

    async def _must_not_be_built(_key_id: Any) -> Any:
        raise AssertionError("a turn already carrying the block must not recall again")

    monkeypatch.setattr(toolrelay, "_playbook_recall_client", _must_not_be_built)
    messages = [{"role": "system", "content": _block()}, {"role": "user", "content": TASK}]
    out = asyncio.run(toolrelay._with_playbook_block("P", messages, request_id="rid", key_id=None))
    assert out == "P"


def test_relay_seam_honours_the_char_cap(monkeypatch: Any) -> None:
    rows = [{**RECORD, "playbook_key": "k" + str(i)} for i in range(6)]
    out, _calls = _seam(monkeypatch, _Recall(_envelope(rows)), playbook_inject_max_chars=120)
    assert out.startswith("AGENT PROMPT\n")
    block = out[len("AGENT PROMPT\n") :]
    assert 0 < len(block) <= 120
    assert playbook.TRUNCATION_MARKER in block


# ---------------------------------------------------------------------------
# the real relay loop: the prefix reaches every model call, once per turn
# ---------------------------------------------------------------------------


def _drive_relay(
    messages: list[dict],
    seen: list[list[dict]],
    *,
    model: list[dict] | None = None,
    grants: set[tuple[str, str]] | None = None,
    exec_results: list[tuple[bool, Any]] | None = None,
    **kwargs: Any,
) -> str:
    """Drive the PRODUCTION relay loop with a scripted model stream.

    `seen` collects the exact message list of every model call, which is
    where the gateway system prefix is observable.
    """
    script = model or [{"content": "done"}]
    counters = {"model": 0, "exec": 0}

    def stream_once(msgs: list[dict], tools: Any) -> Any:
        seen.append([dict(m) for m in msgs])
        response = script[min(counters["model"], len(script) - 1)]
        counters["model"] += 1

        async def raw_chunks() -> Any:
            if response.get("content"):
                chunk = {"choices": [{"delta": {"content": response["content"]}}]}
                yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
            for call in response.get("tool_calls") or []:
                chunk = {"choices": [{"delta": {"tool_calls": [{"index": 0, **call}]}}]}
                yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
            if "usage" in response:
                yield b"data: " + json.dumps({"usage": response["usage"]}).encode() + b"\n\n"

        class _Raw:
            async def aiter_bytes(self) -> Any:
                async for chunk in raw_chunks():
                    yield chunk

        async def gen() -> Any:
            # _Raw is the minimal httpx-shaped double the loop consumes.
            async for event in toolrelay.stream_model_call(_Raw()):  # type: ignore[arg-type]
                yield event

        return gen()

    async def execute(server: str, tool: str, arguments: dict) -> tuple[bool, Any]:
        results = exec_results or [(True, "")]
        result = results[min(counters["exec"], len(results) - 1)]
        counters["exec"] += 1
        return result

    async def run() -> Any:
        return [
            chunk
            async for chunk in toolrelay.relay_stream(
                messages,
                grants or set(),
                [],
                stream_once,
                execute,
                "rid",
                "m1",
                tool_weights={},
                **kwargs,
            )
        ]

    return b"".join(asyncio.run(run())).decode()


def test_relay_model_calls_are_byte_identical_when_disabled(monkeypatch: Any) -> None:
    """Requirement 1: the OFF path changes nothing the model sees."""
    monkeypatch.setattr(settings, "playbook_inject_enabled", False)
    messages = [{"role": "user", "content": TASK}]
    seen: list[list[dict]] = []
    _drive_relay(messages, seen, system_prefix="AGENT PROMPT")
    assert seen == [toolrelay.with_system_prefix(messages, "AGENT PROMPT")]


def test_relay_injects_the_block_into_every_model_call_once_per_turn(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "playbook_inject_enabled", True)
    recall = _Recall(_envelope([RECORD]))

    async def _client(_key_id: Any) -> Any:
        return recall

    monkeypatch.setattr(toolrelay, "_playbook_recall_client", _client)
    arguments = json.dumps({"q": "pod"})
    model: list[dict] = [
        {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "logsearch__search_logs", "arguments": arguments}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
        {"content": "done"},
    ]
    messages = [{"role": "user", "content": TASK}]
    seen: list[list[dict]] = []
    _drive_relay(
        messages,
        seen,
        model=model,
        grants={("logsearch", "search_logs")},
        exec_results=[(True, {"hits": 1})],
        system_prefix="AGENT PROMPT",
    )
    # A prefix with no trailing newline gets a separator, so the operator prompt's
    # last line is never welded onto the injected header (cross-review P2, 2026-11-23).
    expected_prefix = "AGENT PROMPT\n" + _block()
    assert len(seen) == 2, "the tool round must reach a second model call"
    for model_call in seen:
        assert model_call[0] == {"role": "system", "content": expected_prefix}
    assert seen[0][1:] == messages
    assert len(seen[1]) > len(messages), "the tool result rode into the second call"
    # ONE recall for the whole turn: computed once, applied to every model call
    assert recall.calls == [(TASK, settings.playbook_inject_k)]


def test_relay_disabled_path_never_builds_a_recall_client(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "playbook_inject_enabled", False)

    async def _must_not_be_built(_key_id: Any) -> Any:
        raise AssertionError("disabled feature must not touch the memory server")

    monkeypatch.setattr(toolrelay, "_playbook_recall_client", _must_not_be_built)
    seen: list[list[dict]] = []
    text = _drive_relay([{"role": "user", "content": TASK}], seen, system_prefix="AGENT PROMPT")
    assert "done" in text
    assert seen == [toolrelay.with_system_prefix([{"role": "user", "content": TASK}], "AGENT PROMPT")]


# ---------------------------------------------------------------------------
# settings wiring at the seam
# ---------------------------------------------------------------------------


def test_relay_seam_wires_the_success_rate_floor(monkeypatch: Any) -> None:
    out, _calls = _seam(monkeypatch, _Recall(_envelope([RECORD])), playbook_inject_min_success_rate=0.99)
    assert out == "AGENT PROMPT\n", "a record under the operator floor must not be injected"


def test_relay_seam_wires_the_timeout(monkeypatch: Any) -> None:
    async def slow(task: str, k: int) -> str:
        await asyncio.sleep(1.0)
        return _envelope([RECORD])

    out, calls = _seam(monkeypatch, slow, playbook_inject_timeout_ms=20)
    assert out == "AGENT PROMPT\n", "a slow memory store must never hold the turn"
    assert calls["client"] == 1


def test_relay_seam_wires_k(monkeypatch: Any) -> None:
    recall = _Recall(_envelope([RECORD]))
    _seam(monkeypatch, recall, playbook_inject_k=7)
    assert recall.calls == [(TASK, 7)]


# ---------------------------------------------------------------------------
# the mm-rag binder: the distiller MCP path, no new client
# ---------------------------------------------------------------------------


class _FakeSession:
    """Records the MCP tools/call the binder makes; answers with one text block."""

    def __init__(self, calls: dict[str, Any], text: str, is_error: bool = False) -> None:
        self.calls = calls
        self.text = text
        self.is_error = is_error

    async def call_tool(self, name: str, arguments: dict) -> Any:
        self.calls["tool"] = name
        self.calls["arguments"] = arguments
        block = type("Block", (), {"text": self.text})()
        return type("Result", (), {"is_error": self.is_error, "content": [block]})()


def _patch_mcp_path(monkeypatch: Any, calls: dict[str, Any], *, text: str, is_error: bool = False) -> None:
    from pcai_llm.api import routes_mcp
    from pcai_llm.core import enroll_memory

    async def _resolve(server_name: str) -> dict[str, Any]:
        calls["server"] = server_name
        return {"name": server_name, "url": "http://mm-rag.test/mcp", "headers": {}}

    async def _headers(target: dict, key_id: Any = None) -> dict[str, str]:
        calls["key_id"] = key_id
        return {"X-API-Key": "k"}

    async def _session(url: str, headers: dict, operation: Any, timeout_s: float | None = None) -> Any:
        calls["url"] = url
        calls["headers"] = headers
        calls["timeout_s"] = timeout_s
        return await operation(_FakeSession(calls, text, is_error))

    monkeypatch.setattr(enroll_memory, "resolve_memory_server", _resolve)
    monkeypatch.setattr(routes_mcp, "resolve_mcp_headers", _headers)
    monkeypatch.setattr(routes_mcp, "_upstream_session", _session)


def test_recall_client_uses_the_distiller_mcp_path(monkeypatch: Any) -> None:
    calls: dict[str, Any] = {}
    _patch_mcp_path(monkeypatch, calls, text=_envelope([RECORD]))
    recall = asyncio.run(toolrelay._playbook_recall_client(None))
    assert recall is not None
    text = asyncio.run(recall("recover a stuck pod", 2))
    assert text == _envelope([RECORD])
    assert calls["server"] == settings.distiller_memory_server
    assert calls["url"] == "http://mm-rag.test/mcp"
    assert calls["headers"] == {"X-API-Key": "k"}
    assert calls["tool"] == playbook.RECALL_TOOL_DEFAULT
    assert calls["arguments"] == {"task": "recover a stuck pod", "k": 2}
    assert calls["timeout_s"] == settings.playbook_inject_timeout_ms / 1000.0


def test_recall_client_is_none_when_the_memory_server_is_absent(monkeypatch: Any) -> None:
    from pcai_llm.core import enroll_memory

    async def _resolve(_server_name: str) -> None:
        return None

    monkeypatch.setattr(enroll_memory, "resolve_memory_server", _resolve)
    assert asyncio.run(toolrelay._playbook_recall_client(None)) is None


def test_recall_client_reads_an_mcp_error_as_no_recall(monkeypatch: Any) -> None:
    calls: dict[str, Any] = {}
    _patch_mcp_path(monkeypatch, calls, text="refused", is_error=True)
    recall = asyncio.run(toolrelay._playbook_recall_client(None))
    assert recall is not None
    assert asyncio.run(recall("recover a stuck pod", 2)) == ""
