# Wave R1 additions: stamped inert render deltas + enriched [tool] tombstones.
# Appended to the upstream test file's coverage; self-contained harness reuse
# via the same _stream_relay shape.

import asyncio
import json

from pcai_llm.core import toolrelay
from pcai_llm.config import settings


def _drive(model, exec_effects, **kwargs):
    """Same harness shape as test_toolrelay._stream_relay: scripted model
    SSE responses + executor results -> full SSE text."""
    calls = {"exec": 0}
    idx = {"n": 0}

    def stream_once(msgs, tools):
        call_idx = idx["n"]
        idx["n"] += 1
        r = model[min(call_idx, len(model) - 1)]

        async def raw_chunks():
            chunks = []
            if r.get("content"):
                chunks.append({"choices": [{"delta": {"content": r["content"]}}]})
            for tc in r.get("tool_calls") or []:
                chunks.append({"choices": [{"delta": {"tool_calls": [{"index": 0, **tc}]}}]})
            if "usage" in r:
                chunks.append({"usage": r["usage"]})
            for c in chunks:
                yield b"data: " + json.dumps(c).encode() + b"\n\n"

        class R:
            async def aiter_bytes(self):
                async for b in raw_chunks():
                    yield b

        async def gen():
            async for ev in toolrelay.stream_model_call(R()):
                yield ev

        return gen()

    async def execute(server, tool, args):
        r = exec_effects[min(calls["exec"], len(exec_effects) - 1)]
        calls["exec"] += 1
        return r

    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                kwargs.pop("messages", [{"role": "user", "content": "go"}]),
                kwargs.pop("grants"),
                kwargs.pop("catalog", [{"server": "logsearch", "tool": "search_logs", "description": "d"}]),
                stream_once,
                execute,
                "rid",
                kwargs.pop("model", "m1"),
                **kwargs,
            )
        ]

    text = b"".join(asyncio.run(run())).decode()
    return text, calls


def _reasoning_lines(text: str) -> list[str]:
    """Decode every reasoning_content delta out of the raw SSE text."""
    out: list[str] = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            obj = json.loads(line[len("data: "):])
        except json.JSONDecodeError:
            continue
        for ch in obj.get("choices") or []:
            rc = (ch.get("delta") or {}).get("reasoning_content")
            if rc:
                out.append(rc)
    return out


MODEL = [
    {
        "content": None,
        "tool_calls": [{"id": "c1", "function": {"name": "logsearch__search_logs", "arguments": '{"q":"gpu cluster"}'}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    },
    {"content": "found it"},
]


def test_render_deltas_off_by_default_no_stamped_chunks() -> None:
    text, calls = _drive(MODEL, [(True, {"hits": 1})], grants={("logsearch", "search_logs")})
    assert calls["exec"] == 1
    assert '"pcai": {"relay": true}' not in text
    # enriched tombstone still present, prefix intact
    reasoning = "".join(_reasoning_lines(text))
    assert "[tool] logsearch__search_logs" in reasoning
    assert "→ ok (" in reasoning


def test_render_deltas_emit_stamped_chunks_when_on() -> None:
    text, calls = _drive(
        MODEL,
        [(True, {"hits": 1})],
        grants={("logsearch", "search_logs")},
        render_deltas=True,
        render_index_base=1000,
    )
    assert calls["exec"] == 1  # executed exactly once — stamped deltas are inert
    assert '"pcai": {"relay": true}' in text
    # the stamped delta carries the FULL model-facing name + args
    assert '"name": "logsearch__search_logs"' in text
    assert '"index": 1000' in text
    # and the final finish_reason is still the turn's single stop
    assert text.count('"finish_reason": "stop"') == 1


def test_render_deltas_exclude_handback_calls() -> None:
    """A client-offered tool (handback) must NOT get a stamped delta —
    its native deltas stay executable and unstamped."""
    client_tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
    model = [
        {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": '{"cmd":"ls"}'}}]},
        {"content": "done"},
    ]
    text, _ = _drive(
        model,
        [],
        grants=set(),
        catalog=[],
        client_tools=client_tools,
        render_deltas=True,
    )
    assert '"pcai": {"relay": true}' not in text
    assert '"finish_reason": "tool_calls"' in text


def test_tombstone_carries_args_and_preview() -> None:
    text, _ = _drive(MODEL, [(True, "x" * 5000)], grants={("logsearch", "search_logs")})
    reasoning = "".join(_reasoning_lines(text))
    # collapsed args present
    assert '[tool] logsearch__search_logs({"q": "gpu cluster"}) → ok' in reasoning
    # bounded preview line present (collapse + cap)
    assert "↳ " in reasoning


def test_tombstone_denied_has_no_args_leak_of_full_grants() -> None:
    model = [
        {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "secret__tool", "arguments": '{"a":1}'}}],
        },
        {"content": "fine"},
    ]
    text, calls = _drive(model, [], grants=set(), catalog=[])
    assert calls["exec"] == 0
    reasoning = "".join(_reasoning_lines(text))
    assert "[tool] secret__tool" in reasoning and "denied by ACL" in reasoning
    assert '"event": "tool_error"' in text
    assert '"id": "c1"' in text  # call id rides the error event for renderer join
