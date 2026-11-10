"""Relay integration for X-PCAI-Compress-Tools.

Drives toolrelay.relay_stream directly (the test_clientside pattern): scripted
model calls, a fake MCP executor, and an in-memory stand-in for the Postgres
retrieval store. The relay imports the store lazily, so patching the module's
functions covers both the write (compress) and the read (retrieve) paths.

What this proves, in order of risk:
1. ON rewrites the model-facing result, keeps the error signal, and makes the
   original retrievable under the digest the marker advertises;
2. SHADOW reports the saving and rewrites nothing;
3. OFF is byte-for-byte the pre-feature behavior (no marker, no tool, no
   event);
4. the synthetic pcai__retrieve resolves a stored digest and is explicit
   about an unknown one;
5. a store failure fails the COMPRESSION, never the turn.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from pcai_llm.core import toolcompress, toolcompress_store, toolrelay

KEY = uuid.uuid4()
USER = uuid.uuid4()
CATALOG = [{"server": "logsearch", "tool": "search_logs", "description": "search"}]
GRANTS = {("logsearch", "search_logs")}


def _records(n: int = 300) -> list[dict]:
    rows = [
        {"id": i, "path": f"/var/log/pod-{i}.log", "level": "info", "msg": "request completed in 12ms"}
        for i in range(n)
    ]
    rows[137] = {"id": 137, "path": "/var/log/pod-137.log", "level": "error", "msg": "connection refused"}
    return rows


BIG_JSON = json.dumps(_records(), separators=(",", ":"))


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
    }


def _scripted(calls: list[list[dict]]) -> tuple[Any, dict[str, Any]]:
    seen: dict[str, Any] = {"n": 0, "msgs": [], "tools": []}

    def stream_once(msgs: list[dict], tools: list[dict] | None = None) -> Any:
        seen["msgs"].append([dict(m) for m in msgs])
        seen["tools"].append(tools)
        index = seen["n"]
        seen["n"] += 1
        chunks = calls[min(index, len(calls) - 1)]

        async def gen() -> Any:
            for chunk in chunks:
                if "tool_calls" in chunk:
                    yield ("tool_calls", chunk["tool_calls"])
                else:
                    yield ("content", chunk.get("content", ""))
            yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

        return gen()

    return stream_once, seen


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[uuid.UUID, str], str]:
    data: dict[tuple[uuid.UUID, str], str] = {}

    async def save(key_id: uuid.UUID, digest: str, body: str) -> None:
        data[(key_id, digest)] = body

    async def load(key_id: uuid.UUID, digest: str) -> str | None:
        return data.get((key_id, digest))

    monkeypatch.setattr(toolcompress_store, "save", save)
    monkeypatch.setattr(toolcompress_store, "load", load)
    return data


def _run(
    monkeypatch: pytest.MonkeyPatch,
    script: list[list[dict]],
    execute: Any,
    *,
    mode: str = "on",
    key_id: uuid.UUID | None = KEY,
) -> tuple[str, dict[str, Any]]:
    async def ok_gate(*_a: Any, **_k: Any) -> Any:
        class _Gate:
            ok = True

        return _Gate()

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    # No Postgres in unit tests: the loop's trace/settle/gate writes are
    # best-effort in production and no-ops here.
    monkeypatch.setattr(toolrelay, "check_budgets", ok_gate)
    monkeypatch.setattr(toolrelay, "settle", noop)
    monkeypatch.setattr(toolrelay.traces, "record", noop)

    stream_once, seen = _scripted(script)

    async def collect() -> list[bytes]:
        return [
            chunk
            async for chunk in toolrelay.relay_stream(
                [{"role": "user", "content": "find the failure"}],
                GRANTS,
                CATALOG,
                stream_once,
                execute,
                "req-compress",
                "test-model",
                key_id=key_id,
                compress_mode=mode,
            )
        ]

    chunks = asyncio.run(collect())
    return b"".join(chunks).decode(), seen


async def _execute_big(_server: str, _tool: str, _args: dict) -> tuple[bool, Any]:
    return True, BIG_JSON


# The PRODUCTION shape: routes_proxy.execute returns the MCP content-block
# LIST, not a bare string. A single-text-block result serializes to a
# one-line, one-item JSON array — below every detector's item/line floor —
# which is how the feature shipped unable to compress the traffic it was
# written for. These fixtures exist so the shape can never silently regress.
BIG_LOG = "\n".join(
    [f"2026-11-05T09:00:{i:02d}Z INFO worker-{i} tick" for i in range(80)]
    + ["2026-11-05T09:01:00Z ERROR upstream refused the connection"]
    + [f"2026-11-05T09:01:{i:02d}Z INFO worker-{i} tick" for i in range(80)]
)


def _blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


async def _execute_blocks(_server: str, _tool: str, _args: dict) -> tuple[bool, Any]:
    return True, _blocks(BIG_LOG)


def _tool_messages(seen: dict[str, Any], call_index: int) -> list[dict]:
    return [m for m in seen["msgs"][call_index] if m.get("role") == "tool"]


def test_on_rewrites_result_and_offers_retrieve(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    script = [[_tool_call("c1", "logsearch__search_logs", {"q": "refused"})], [{"content": "done"}]]
    text, seen = _run(monkeypatch, script, _execute_big, mode="on")

    tool_msgs = _tool_messages(seen, 1)
    assert len(tool_msgs) == 1
    content = tool_msgs[0]["content"]
    assert toolcompress.MARKER_SIG in content
    assert "connection refused" in content  # the signal survives
    assert len(content) < len(BIG_JSON)
    assert content != BIG_JSON

    # The original is retrievable under the advertised digest.
    digest = toolcompress.digest_of(BIG_JSON)
    assert store[(KEY, digest)] == BIG_JSON

    # Wire event + the synthetic tool is offered.
    assert '"event": "compress"' in text
    names = [t["function"]["name"] for t in seen["tools"][0]]
    assert "pcai__retrieve" in names


def test_mcp_content_block_result_is_compressed(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    """The production MCP shape must reach the compressor as TEXT.

    Regression (2026-11-05): execute() returns [{"type": "text", "text": ...}],
    and serialized that is a one-line, one-item JSON array. Every detector is
    count-based, so every in-cluster MCP result passed through and was then
    head-truncated at 32k — the blunt cap this feature exists to replace.
    """
    script = [[_tool_call("c1", "logsearch__search_logs", {"q": "refused"})], [{"content": "done"}]]
    text, seen = _run(monkeypatch, script, _execute_blocks, mode="on")

    content = _tool_messages(seen, 1)[0]["content"]
    assert toolcompress.MARKER_SIG in content
    assert "upstream refused the connection" in content  # the signal survives
    assert content != json.dumps(_blocks(BIG_LOG))  # the envelope was unwrapped
    assert len(content) < len(BIG_LOG)

    # The digest addresses the UNWRAPPED original: retrieval hands back the
    # log the model asked for, not a JSON envelope around it.
    digest = toolcompress.digest_of(BIG_LOG)
    assert store[(KEY, digest)] == BIG_LOG
    assert '"event": "compress"' in text


def test_single_text_block_below_the_floor_keeps_the_envelope(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    """Unwrapping is a compression DECISION, not a reformat.

    A block result that does not clear the floor must stay byte-for-byte the
    pre-fix serialized envelope — the model-facing shape only changes when a
    rewrite the model actually receives replaces it.
    """
    small = _blocks("2026-11-05T09:00:00Z INFO tiny\n")

    async def _execute_small(_server: str, _tool: str, _args: dict) -> tuple[bool, Any]:
        return True, small

    script = [[_tool_call("c1", "logsearch__search_logs", {"q": "x"})], [{"content": "done"}]]
    _text, seen = _run(monkeypatch, script, _execute_small, mode="on")

    assert _tool_messages(seen, 1)[0]["content"] == json.dumps(small)
    assert store == {}


def test_shadow_reports_but_rewrites_nothing(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    script = [[_tool_call("c1", "logsearch__search_logs", {"q": "refused"})], [{"content": "done"}]]
    text, seen = _run(monkeypatch, script, _execute_big, mode="shadow")

    content = _tool_messages(seen, 1)[0]["content"]
    assert content == BIG_JSON  # untouched
    assert toolcompress.MARKER_SIG not in content
    assert store == {}  # nothing stored: nothing was rewritten
    assert '"event": "compress"' in text  # the measurement still happens
    names = [t["function"]["name"] for t in seen["tools"][0]]
    assert "pcai__retrieve" not in names


def test_off_is_byte_for_byte_unchanged(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    script = [[_tool_call("c1", "logsearch__search_logs", {"q": "refused"})], [{"content": "done"}]]
    text, seen = _run(monkeypatch, script, _execute_big, mode="off")

    content = _tool_messages(seen, 1)[0]["content"]
    assert content == BIG_JSON
    assert '"event": "compress"' not in text
    names = [t["function"]["name"] for t in seen["tools"][0]]
    assert "pcai__retrieve" not in names


def test_retrieve_returns_the_stored_original(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    store[(KEY, "abc123")] = "the full original text"
    script = [[_tool_call("c1", "pcai__retrieve", {"digest": "abc123"})], [{"content": "done"}]]
    _text, seen = _run(monkeypatch, script, _execute_big, mode="on")

    content = _tool_messages(seen, 1)[0]["content"]
    assert "[pcai retrieve: chars 0..22 of 22]" in content
    assert "the full original text" in content


def test_retrieve_unknown_digest_is_explicit(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    script = [[_tool_call("c1", "pcai__retrieve", {"digest": "nope"})], [{"content": "done"}]]
    _text, seen = _run(monkeypatch, script, _execute_big, mode="on")

    content = _tool_messages(seen, 1)[0]["content"]
    assert "no stored result" in content
    assert "nope" in content


def test_store_failure_fails_compression_not_the_turn(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    async def broken_save(_key_id: uuid.UUID, _digest: str, _body: str) -> None:
        raise RuntimeError("postgres down")

    monkeypatch.setattr(toolcompress_store, "save", broken_save)
    script = [[_tool_call("c1", "logsearch__search_logs", {"q": "refused"})], [{"content": "done"}]]
    text, seen = _run(monkeypatch, script, _execute_big, mode="on")

    content = _tool_messages(seen, 1)[0]["content"]
    assert content == BIG_JSON  # original, untouched: no marker to nowhere
    assert toolcompress.MARKER_SIG not in content
    assert '"stored": false' in text  # the failure is visible on the wire
    assert "done" in text  # the turn completed


def test_keyless_run_never_rewrites(monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]) -> None:
    """key_id=None is the unit-test/legacy path: no store identity means no
    safe rewrite (a marker without a retrievable original is the exact
    failure mode the store exists to prevent) AND no pcai__retrieve offer
    (a tool that can never resolve)."""
    script = [[_tool_call("c1", "logsearch__search_logs", {"q": "refused"})], [{"content": "done"}]]
    _text, seen = _run(monkeypatch, script, _execute_big, mode="on", key_id=None)

    content = _tool_messages(seen, 1)[0]["content"]
    assert content == BIG_JSON
    names = [t["function"]["name"] for t in seen["tools"][0]]
    assert "pcai__retrieve" not in names


def test_cross_key_retrieval_is_denied(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    """The security core of the store: a digest resolves only for the key
    that produced it. KEY_B asking for KEY_A's digest gets the explicit
    unknown-digest answer, never the text."""
    store[(KEY, "abc123")] = "key A's private output"
    other_key = uuid.uuid4()
    script = [[_tool_call("c1", "pcai__retrieve", {"digest": "abc123"})], [{"content": "done"}]]
    _text, seen = _run(monkeypatch, script, _execute_big, mode="on", key_id=other_key)

    content = _tool_messages(seen, 1)[0]["content"]
    assert "no stored result" in content
    assert "key A's private output" not in content


def test_off_turn_retrieval_still_resolves(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    """A history that names pcai__retrieve from an earlier ON turn must still
    resolve when the current turn is OFF — the model followed the marker's
    instruction; answering "denied" would punish it for being right."""
    store[(KEY, "abc123")] = "the full original text"
    script = [[_tool_call("c1", "pcai__retrieve", {"digest": "abc123"})], [{"content": "done"}]]
    _text, seen = _run(monkeypatch, script, _execute_big, mode="off")

    content = _tool_messages(seen, 1)[0]["content"]
    assert "the full original text" in content


def test_blank_digest_is_an_explicit_error(
    monkeypatch: pytest.MonkeyPatch, store: dict[tuple[uuid.UUID, str], str]
) -> None:
    script = [[_tool_call("c1", "pcai__retrieve", {})], [{"content": "done"}]]
    _text, seen = _run(monkeypatch, script, _execute_big, mode="on")

    content = _tool_messages(seen, 1)[0]["content"]
    assert "requires a digest" in content
