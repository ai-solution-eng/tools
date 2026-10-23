"""Server-side tool-use relay: guards, ACL enforcement inside the loop,
ledger settle per hop, and the SSE event stream."""

from __future__ import annotations

import json
from typing import Any

from pcai_llm.core import toolrelay
from pcai_llm.core.toolrelay import RelayGuard, parse_tool_name, tools_payload

CATALOG = [
    {"server": "logsearch", "tool": "search_logs", "description": "search"},
    {"server": "applygate", "tool": "plan_apply", "description": "plan"},
]


# --- pure pieces ---


def test_tools_payload_uses_double_underscore() -> None:
    tools = tools_payload(CATALOG)
    assert tools[0]["function"]["name"] == "logsearch__search_logs"
    assert tools[1]["function"]["name"] == "applygate__plan_apply"


def test_parse_tool_name() -> None:
    assert parse_tool_name("logsearch__search_logs") == ("logsearch", "search_logs")
    assert parse_tool_name("no_separator") is None
    assert parse_tool_name("__bad") is None


def test_alias_granted_gateway_name_resolves_harness_prefix() -> None:
    """The name-duality fix: a model call under the harness's own connection
    prefix (opencode registers the gateway as mcp__pcai-llm-gateway__...) that
    suffix-matches a GRANTED gateway tool resolves to that (server, tool)."""
    catalog = [{"server": "mm-rag", "tool": "search_memory", "description": "d"}]
    assert toolrelay.alias_granted_gateway_name("mm-rag__search_memory", catalog) == ("mm-rag", "search_memory")
    assert toolrelay.alias_granted_gateway_name("mcp__pcai-llm-gateway__mm-rag__search_memory", catalog) == (
        "mm-rag",
        "search_memory",
    )
    # one-hop client convention still aliases (parse already strips mcp__, but
    # a bare mcp__logsearch__search_logs call must resolve, not error)
    assert toolrelay.alias_granted_gateway_name("mcp__mm-rag__search_memory", catalog) == ("mm-rag", "search_memory")


def test_alias_granted_gateway_name_never_guesses() -> None:
    """Only UNIQUE full-name matches alias: a name matching no grant, or a
    bare tool name (substring luck must not hijack a call), stays None — the
    unknown-tool path. Server-qualified tails disambiguate (a__search and
    b__search are different full names), but a name whose tails collapse to
    SEVERAL different grants is ambiguous and aliases nothing."""
    catalog = [
        {"server": "mm-rag", "tool": "search_memory", "description": "d"},
        {"server": "mm-rag", "tool": "get_dataset_info", "description": "d"},
    ]
    assert toolrelay.alias_granted_gateway_name("ghost__x", catalog) is None
    assert toolrelay.alias_granted_gateway_name("search_memory", catalog) is None  # bare tool: no full-name match
    # same TOOL name on two servers: mcp__conn__search's left-trimmed tails
    # collapse ambiguously — but the server-qualified spelling still resolves
    dual = [
        {"server": "a", "tool": "search", "description": "d"},
        {"server": "b", "tool": "search", "description": "d"},
    ]
    assert toolrelay.alias_granted_gateway_name("mcp__conn__a__search", dual) == ("a", "search")
    assert toolrelay.alias_granted_gateway_name("mcp__conn__b__search", dual) == ("b", "search")


def test_guard_max_iterations() -> None:
    g = RelayGuard(max_iterations=2, wall_clock_s=60)
    assert g.check(0, [{"function": {"name": "a__b", "arguments": "{}"}}]) == ""
    assert g.check(2, [{"function": {"name": "a__b", "arguments": "{}"}}]) != ""


def test_guard_loop_detection() -> None:
    g = RelayGuard(max_iterations=10, wall_clock_s=60)
    tc = [{"function": {"name": "a__b", "arguments": '{"x":1}'}}]
    assert g.check(0, tc) == ""
    assert "loop detected" in g.check(1, tc)  # identical call repeated
    # different args -> not a loop
    g2 = RelayGuard(10, 60)
    assert g2.check(0, tc) == ""
    assert g2.check(1, [{"function": {"name": "a__b", "arguments": '{"x":2}'}}]) == ""


def test_guard_wall_clock() -> None:
    import time

    g = RelayGuard(max_iterations=10, wall_clock_s=0.0)
    time.sleep(0.01)
    assert "wall-clock" in g.check(0, [])


# --- full loop (async, mocked model + executor) ---


def _stream_relay(model_side_effects, exec_side_effects, **kwargs):
    """Drive relay_stream (the PRODUCTION loop — run_relay was deleted as a
    drifted duplicate, audit finding 20) with scripted SSE model responses
    + executor results; returns the full SSE text + executor call count."""
    calls = {"exec": 0}
    idx = {"n": 0}  # PER-RELAY model-call counter (a stream_once-local reset
    # would replay the first effect on every iteration)

    def stream_once(msgs, tools):
        call_idx = idx["n"]
        idx["n"] += 1
        r = model_side_effects[min(call_idx, len(model_side_effects) - 1)]

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
        r = exec_side_effects[min(calls["exec"], len(exec_side_effects) - 1)]
        calls["exec"] += 1
        return r

    import asyncio

    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                kwargs.pop("messages", [{"role": "user", "content": "go"}]),
                kwargs.pop("grants"),
                kwargs.pop("catalog", CATALOG),
                stream_once,
                execute,
                "rid",
                kwargs.pop("model", "m1"),
                **kwargs,
            )
        ]

    text = b"".join(asyncio.run(run())).decode()
    return text, calls


def test_relay_executes_tool_and_returns_final() -> None:
    model = [
        {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "logsearch__search_logs", "arguments": '{"q":"gpu"}'}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
        {"content": "found 3 results"},
    ]
    text, _calls = _stream_relay(
        model,
        [(True, {"hits": 3})],
        grants={("logsearch", "search_logs")},
    )
    # the tool result was fed back and the final answer streamed
    assert '"event": "tool_call"' in text and '"ok": true' in text
    assert "found 3 results" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_denies_ungranted_tool_inside_loop() -> None:
    model = [
        {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "applygate__apply_manifest", "arguments": "{}"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
        {"content": "ok, skipping"},
    ]
    text, calls = _stream_relay(
        model,
        [(True, "should never run")],
        grants={("logsearch", "search_logs")},  # applygate NOT granted
    )
    assert calls["exec"] == 0, "ungranted tool must NOT execute"
    assert '"detail": "denied by ACL"' in text
    assert "ok, skipping" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_aborts_on_loop_and_asks_model_to_wrap_up() -> None:
    model = [
        {
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "logsearch__search_logs", "arguments": '{"q":"same"}'}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
        {
            "content": None,
            "tool_calls": [{"id": "c2", "function": {"name": "logsearch__search_logs", "arguments": '{"q":"same"}'}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },  # identical -> loop
        {"content": "final wrap-up"},
    ]
    text, _ = _stream_relay(model, [(True, "r")], grants={("logsearch", "search_logs")})
    assert '"event": "guard"' in text and "loop detected" in text
    # protocol compliance (audit finding 13): the wrap-up answer is ONE
    # chunk carrying THE terminal finish_reason — not per-delta stops
    assert '"finish_reason": "stop"' in text
    assert text.count('"finish_reason": "stop"') == 1
    assert "final wrap-up" in text
    assert text.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# streaming machinery
# ---------------------------------------------------------------------------


def _sse(chunks: list[dict]) -> Any:
    async def gen():
        for c in chunks:
            yield b"data: " + json.dumps(c).encode() + b"\n\n"

    return gen()


def test_parse_sse_handles_split_lines() -> None:
    """Line reassembly across chunk boundaries. The pre-filter (perf audit)
    applies to REASSEMBLED lines: the first fragment carries no marker, the
    completed line does — it must still parse (regression guard for the
    filter running per-chunk by mistake)."""
    import asyncio

    async def raw():
        yield b'data: {"a":'
        yield b'1, "usage": {"total_tokens": 5}}\n'
        yield b'data: {"a": 2, "choices": [{"delta": {"content": "x"}}]}\n\n'
        yield b"data: [DONE]\n"

    async def run():
        return [o async for o in toolrelay.parse_sse_payloads(raw())]

    assert asyncio.run(run()) == [
        {"a": 1, "usage": {"total_tokens": 5}},
        {"a": 2, "choices": [{"delta": {"content": "x"}}]},
    ]


def test_parse_sse_prefilter_skips_noop_lines() -> None:
    """Perf fix: lines carrying none of the fields the relay reads (empty
    keepalive deltas, comments) skip decode+json.loads entirely; every
    marker-bearing line still parses."""
    import asyncio

    parsed: list[dict] = []

    async def raw():
        yield b": ping\n\n"
        yield b'data: {"choices":[{"delta":{},"finish_reason":null}]}\n\n'
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b'data: {"usage": {"prompt_tokens": 1, "completion_tokens": 2}}\n\n'

    async def run():
        return [o async for o in toolrelay.parse_sse_payloads(raw())]

    parsed = asyncio.run(run())
    assert parsed == [
        {"choices": [{"delta": {"content": "hi"}}]},
        {"usage": {"prompt_tokens": 1, "completion_tokens": 2}},
    ]


def test_stream_model_call_assembles_fragmented_tool_calls() -> None:
    """vLLM streams tool_calls as argument FRAGMENTS across deltas — the
    assembler must reconstruct them (regression for the streaming relay)."""
    import asyncio

    chunks = [
        {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "type": "function",
                                "function": {"name": "logsearch__search_logs", "arguments": '{"q":'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"gpu"}'}}]}}]},
        {
            "choices": [{"delta": {}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4},
        },
    ]

    class R:
        async def aiter_bytes(self):
            for c in chunks:
                yield b"data: " + json.dumps(c).encode() + b"\n\n"

    async def run():
        return [e async for e in toolrelay.stream_model_call(R())]

    events = asyncio.run(run())
    kinds = [k for k, _ in events]
    assert kinds == ["reasoning", "usage", "tool_calls"]
    tc = dict(events)["tool_calls"][0]
    assert tc["id"] == "c1"
    assert tc["function"]["name"] == "logsearch__search_logs"
    assert json.loads(tc["function"]["arguments"]) == {"q": "gpu"}


def test_relay_stream_emits_thinking_and_final() -> None:
    import asyncio

    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1
        if calls["n"] == 1:

            async def gen1():
                class R:
                    async def aiter_bytes(self):
                        yield b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n'
                        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"logsearch__search_logs","arguments":"{}"}}]}}]}\n\n'

                resp = R()
                async for ev in toolrelay.stream_model_call(resp):
                    yield ev

            return gen1()

        async def gen2():
            class R:
                async def aiter_bytes(self):
                    yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'

            resp = R()
            async for ev in toolrelay.stream_model_call(resp):
                yield ev

        return gen2()

    async def execute(server, tool, args):
        return True, {"ok": 1}

    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                CATALOG,
                stream_once,
                execute,
                "rid",
                "m",
            )
        ]

    out = asyncio.run(run())
    text = b"".join(out).decode()
    assert '"thinking"' in text and '"iteration": 0' in text
    assert '"event": "tool_call"' in text.replace("tool_call", "tool_call") or '"tool_call"' in text
    assert "done" in text and "[DONE]" in text
    # mixed-mode visibility: thinking AND tool steps dual-emit as native
    # reasoning_content deltas so OpenAI-compatible clients render the loop
    assert "reasoning_content" in text
    assert "[tool] logsearch__search_logs" in text


def test_relay_stream_merges_client_tools() -> None:
    """The caller's own tools ride along UNDER the MCP catalog at every model
    call — the relay must not silently strip the harness's tools."""
    import asyncio

    seen: list[list[dict] | None] = []

    def stream_once(msgs, tools):
        seen.append(tools)

        async def gen():
            yield ("content", "ok")
            yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

        return gen()

    async def execute(server, tool, args):
        return True, {}

    client_tools = [{"type": "function", "function": {"name": "bash"}}]
    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=client_tools,
            )
        )
    )
    assert b"[DONE]" in b"".join(out)
    names = [t["function"]["name"] for t in (seen[0] or [])]
    assert "bash" in names


def test_relay_stream_hands_back_client_tool_calls() -> None:
    """A mixed batch (client tool + MCP tool in one model turn) runs the MCP
    half IN-LOOP and hands back the client half, with the MCP results
    injected as content so they survive into the client's replayed history —
    the old drop-the-MCP-half design silently ate those calls."""
    import asyncio

    executed: list[tuple[str, str]] = []

    def stream_once(msgs, tools):
        async def gen():
            yield ("content", "editing the file now")
            yield (
                "tool_calls",
                [
                    {"id": "c1", "type": "function", "function": {"name": "edit", "arguments": '{"path": "x"}'}},
                    {"id": "c2", "type": "function", "function": {"name": "logsearch__search_logs", "arguments": "{}"}},
                ],
            )

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {"found": "stuff"}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=[{"type": "function", "function": {"name": "edit"}}],
            )
        )
    )
    text = b"".join(out).decode()
    assert executed == [("logsearch", "search_logs")]  # MCP half executed in-loop
    assert '"handback"' in text and '"name": "edit"' in text
    assert '"delta": {"content": "editing the file now"}' in text  # narration streamed live
    assert "[tool result] logsearch__search_logs" in text  # result survives the handback
    assert '{\\"found\\": \\"stuff\\"}' in text  # result payload (JSON-escaped inside the SSE frame)
    assert '"finish_reason": "tool_calls"' in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_stream_streams_content_live() -> None:
    """The final answer must stream piece-by-piece as the model writes it —
    not arrive as one post-generation blob ('I typically start reading the
    output when it comes')."""
    import asyncio

    def stream_once(msgs, tools):
        async def gen():
            yield ("content", "part one. ")
            yield ("content", "part two. ")
            yield ("content", "part three.")
            yield ("usage", {"prompt_tokens": 1, "completion_tokens": 3})

        return gen()

    async def execute(server, tool, args):
        return True, {}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    text = b"".join(out).decode()
    content_deltas = [line for line in text.splitlines() if '"delta": {"content": "part' in line]
    assert len(content_deltas) == 3  # three live pieces, not one blob
    assert '"finish_reason": "stop"' in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_stream_stuck_model_call_closes_cleanly(monkeypatch) -> None:
    """A queued/wedged upstream must NOT park the relay for the full 600s
    upstream timeout: the per-call budget (wall-clock guard) fires, the client
    gets a guard event + visible error chunk + [DONE] — never a silent dead
    socket (the 'relay model appears to hang' incident)."""
    import asyncio
    import time as _time

    from pcai_llm.config import settings as _settings

    monkeypatch.setattr(_settings, "tool_relay_wall_clock_s", 0.05)  # budget floor is 1.0s

    def stream_once(msgs, tools):
        async def gen():
            await asyncio.sleep(30)  # wedged upstream: never yields
            yield ("content", "unreachable")

        return gen()

    async def execute(server, tool, args):
        return True, {}

    started = _time.monotonic()
    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    elapsed = _time.monotonic() - started
    text = b"".join(out).decode()
    assert elapsed < 10, f"relay parked {elapsed:.1f}s on a wedged upstream"
    assert '"event": "guard"' in text and "timed out" in text
    assert "relay model call timed out" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_guard_clamps_to_ingress_timeout(monkeypatch) -> None:
    """Guardrail for the 600s default: a wall clock at/above the Istio
    virtualService timeout (600s) would make a burned budget RACE the ingress
    504 — the client gets an unparseable gateway error instead of the relay's
    clean guard event + error chunk + [DONE]. RelayGuard must clamp its
    deadline to upstream_timeout_s minus headroom, whatever the env says."""
    import time as _time

    from pcai_llm.config import settings as _settings

    monkeypatch.setattr(_settings, "tool_relay_wall_clock_s", 99999.0)
    g = toolrelay.RelayGuard(8, _settings.tool_relay_wall_clock_s)
    assert g.deadline - _time.monotonic() <= (
        _settings.upstream_timeout_s - _settings.tool_relay_ingress_headroom_s + 0.1
    )
    # small values pass through untouched (the clamp is a CEILING only)
    monkeypatch.setattr(_settings, "tool_relay_wall_clock_s", 5.0)
    g2 = toolrelay.RelayGuard(8, 5.0)
    assert g2.deadline - _time.monotonic() <= 5.1


def test_relay_stream_wedged_upstream_closes_inside_ingress_budget(monkeypatch) -> None:
    """The 600s-default variant of the wedge test: even with the raised
    budget, a wedged upstream must hit the (clamped) guard and close the
    stream cleanly well inside the ingress timeout — guard event, error
    chunk, [DONE]."""
    import asyncio
    import time as _time

    from pcai_llm.config import settings as _settings

    monkeypatch.setattr(_settings, "tool_relay_wall_clock_s", 600.0)
    monkeypatch.setattr(_settings, "upstream_timeout_s", 2.0)  # clamp -> ~1.7s guard
    monkeypatch.setattr(_settings, "tool_relay_ingress_headroom_s", 0.3)

    def stream_once(msgs, tools):
        async def gen():
            await asyncio.sleep(60)  # wedged: outlives the clamped budget
            yield ("content", "unreachable")

        return gen()

    async def execute(server, tool, args):
        return True, {}

    started = _time.monotonic()
    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    elapsed = _time.monotonic() - started
    text = b"".join(out).decode()
    assert elapsed < _settings.upstream_timeout_s, f"relay parked {elapsed:.1f}s past the ingress clamp"
    assert "relay model call timed out" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_stream_emits_keepalive_during_slow_call(monkeypatch) -> None:
    """While a model call streams slowly, SSE comment keepalives must flow so
    proxy/ingress idle timers don't reap the connection."""
    import asyncio

    monkeypatch.setattr(toolrelay, "KEEPALIVE_S", 0.05)

    def stream_once(msgs, tools):
        async def gen():
            await asyncio.sleep(0.3)  # silent longer than one keepalive slice
            yield ("content", "late")
            yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

        return gen()

    async def execute(server, tool, args):
        return True, {}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    text = b"".join(out).decode()
    assert ": keepalive" in text
    assert "late" in text and "[DONE]" in text


def test_relay_stream_timeout_retries_then_succeeds(monkeypatch) -> None:
    """Retry feature: a bounded model call that expired on timeout (the
    prefill-queueing case) gets ONE bounded retry per turn instead of killing
    the turn — a retry guard event announces it, keepalives flow during the
    retry attempt, and its answer completes the turn. Each attempt settles
    separately (the timed-out attempt keeps its error model_usage), and no
    attempt runs after the retry succeeded."""
    import asyncio

    from pcai_llm.config import settings as _settings

    monkeypatch.setattr(_settings, "tool_relay_call_retries", 1)
    monkeypatch.setattr(toolrelay, "KEEPALIVE_S", 0.05)

    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1
        if calls["n"] == 1:

            async def gen1():
                await asyncio.sleep(0.01)  # dies inside the first keepalive slice
                raise TimeoutError("model call exceeded its 570s budget")
                yield ("content", "")  # pragma: no cover — makes this a generator

            return gen1()

        async def gen2():
            await asyncio.sleep(0.3)  # silent longer than one keepalive slice
            yield ("content", "the real answer")
            yield ("usage", {"prompt_tokens": 5, "completion_tokens": 7})

        return gen2()

    async def execute(server, tool, args):
        return True, {}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    text = b"".join(out).decode()
    assert calls["n"] == 2, "exactly one retry; no attempt after the success"
    assert '"event": "guard", "detail": "model call timed out' in text
    assert "retrying (1 of 1 retries left" in text and "s budget)" in text
    assert text.count('"event": "model_usage"') == 2  # per attempt: error settle + retry settle
    assert ": keepalive" in text  # attempt 1 died pre-slice, so these rode the retry
    assert "the real answer" in text
    assert "(model error: relay model call timed out)" not in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_stream_retry_timeout_falls_through_to_clean_close(monkeypatch) -> None:
    """The retry is bounded: if the RETRY also expires, the stream falls
    through to today's clean close — guard event carrying the retry's own
    timeout, visible error chunk, [DONE] — and never attempts a third call."""
    import asyncio

    from pcai_llm.config import settings as _settings

    monkeypatch.setattr(_settings, "tool_relay_call_retries", 1)
    monkeypatch.setattr(toolrelay, "KEEPALIVE_S", 0.05)

    calls = {"n": 0}

    def stream_once(msgs, tools):
        n = calls["n"] = calls["n"] + 1

        async def gen():
            await asyncio.sleep(0.01)
            raise TimeoutError(f"model call exceeded its {n} budget")
            yield ("content", "")  # pragma: no cover — makes this a generator

        return gen()

    async def execute(server, tool, args):
        return True, {}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    text = b"".join(out).decode()
    assert calls["n"] == 2, "one retry, never a third attempt"
    assert "retrying (1 of 1 retries left" in text
    assert '"event": "guard", "detail": "model call exceeded its 2 budget"' in text
    assert "(model error: relay model call timed out)" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_stream_no_retry_when_wall_clock_exhausted(monkeypatch) -> None:
    """A timeout with the wall clock already burned gets NO retry (a doomed
    attempt against a still-queued endpoint is worse than failing now):
    straight to today's clean close."""
    import asyncio
    import time as _time

    from pcai_llm.config import settings as _settings

    monkeypatch.setattr(_settings, "tool_relay_call_retries", 1)
    monkeypatch.setattr(_settings, "tool_relay_wall_clock_s", 0.05)  # guard deadline ~now

    def stream_once(msgs, tools):
        async def gen():
            await asyncio.sleep(30)  # wedged upstream: never yields
            yield ("content", "unreachable")

        return gen()

    async def execute(server, tool, args):
        return True, {}

    started = _time.monotonic()
    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    elapsed = _time.monotonic() - started
    text = b"".join(out).decode()
    assert elapsed < 10, f"relay parked {elapsed:.1f}s on a wedged upstream"
    assert "retrying" not in text
    assert "(model error: relay model call timed out)" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_stream_call_retries_zero_is_today_behavior(monkeypatch) -> None:
    """tool_relay_call_retries=0 = pre-retry behavior byte-for-byte: the
    first bounded-call timeout closes the stream immediately — no retry
    event, no second model call."""
    import asyncio

    from pcai_llm.config import settings as _settings

    monkeypatch.setattr(_settings, "tool_relay_call_retries", 0)
    monkeypatch.setattr(toolrelay, "KEEPALIVE_S", 0.05)

    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1

        async def gen():
            await asyncio.sleep(0.01)
            raise TimeoutError("model call exceeded its 570s budget")
            yield ("content", "")  # pragma: no cover — makes this a generator

        return gen()

    async def execute(server, tool, args):
        return True, {}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    text = b"".join(out).decode()
    assert calls["n"] == 1
    assert "retrying" not in text
    assert '"event": "guard", "detail": "model call exceeded its 570s budget"' in text
    assert "(model error: relay model call timed out)" in text
    assert text.rstrip().endswith("data: [DONE]")


def test_relay_stream_hands_back_mcp_prefixed_client_tools() -> None:
    """DSH/opencode expose their OWN MCP servers as mcp__<server>__<tool>.
    parse_tool_name strips that prefix, so the name parses as gateway-shaped —
    handback must test offered-name membership FIRST or the client's MCP
    servers are hijacked into the gateway ACL and denied in-loop (live
    incident: 'denied: k8s-hardened-g2-mcp/triage is not granted' instead of
    local execution)."""
    import asyncio

    executed: list[tuple[str, str]] = []

    def stream_once(msgs, tools):
        async def gen():
            yield (
                "tool_calls",
                [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "mcp__k8s-hardened-g2-mcp__triage", "arguments": "{}"},
                    }
                ],
            )

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=[
                    {"type": "function", "function": {"name": "mcp__k8s-hardened-g2-mcp__triage"}},
                ],
            )
        )
    )
    text = b"".join(out).decode()
    assert executed == []  # never touched the gateway executor
    assert '"handback"' in text
    assert "mcp__k8s-hardened-g2-mcp__triage" in text
    assert "denied" not in text  # the ACL path must NOT fire for offered tools
    assert '"finish_reason": "tool_calls"' in text
    assert text.rstrip().endswith("data: [DONE]")


def test_shadow_client_mcp_drops_unambiguous_twins() -> None:
    """A client MCP tool whose tool name matches exactly one granted gateway
    tool is shadowed (dropped from the OFFER) — the model calls the in-cluster
    variant and skips the client's VPN hop. The reverse map enables
    protocol-true handback rewrites on mixed batches."""
    client_tools = [
        {"type": "function", "function": {"name": "mcp__logsearch__search_logs"}},
        {"type": "function", "function": {"name": "bash"}},  # plain: never shadows
        {"type": "function", "function": {"name": "mcp__workbench__write_file"}},  # no twin
    ]
    catalog = [
        {"server": "logsearch", "tool": "search_logs", "description": "d"},
        {"server": "logsearch", "tool": "get_pod_logs", "description": "d"},
    ]
    kept, reverse = toolrelay.shadow_client_mcp(client_tools, catalog)
    names = [t["function"]["name"] for t in kept]
    assert "mcp__logsearch__search_logs" not in names  # shadowed — cloud twin offered instead
    assert "bash" in names and "mcp__workbench__write_file" in names
    assert reverse == {"logsearch__search_logs": "mcp__logsearch__search_logs"}


def test_shadow_client_mcp_drops_double_prefixed_gateway_twins() -> None:
    """opencode wraps the gateway's own /mcp under a connection prefix:
    mcp__pcai-llm-gateway__<server>__<tool>. The one-hop split saw tool name
    '<server>__<tool>' and matched NOTHING, so the model was offered BOTH
    spellings of the same tool and history replay taught it the bare one the
    harness refuses — the live 'unknown tool mm-rag__search_memory' storm.
    Suffix matching dedupes the offer AND populates the mixed-batch rewrite."""
    client_tools = [
        {"type": "function", "function": {"name": "mcp__pcai-llm-gateway__mm-rag__search_memory"}},
        {"type": "function", "function": {"name": "bash"}},  # plain: never shadows
        {"type": "function", "function": {"name": "mcp__other-svc__get_dataset_info"}},  # not a gateway tail
    ]
    catalog = [{"server": "mm-rag", "tool": "search_memory", "description": "d"}]
    kept, reverse = toolrelay.shadow_client_mcp(client_tools, catalog)
    names = [t["function"]["name"] for t in kept]
    assert "mcp__pcai-llm-gateway__mm-rag__search_memory" not in names  # shadowed — suffix twin
    assert "bash" in names and "mcp__other-svc__get_dataset_info" in names
    assert reverse == {"mm-rag__search_memory": "mcp__pcai-llm-gateway__mm-rag__search_memory"}


def test_shadow_client_mcp_keeps_ambiguous_and_respects_kill_switch(monkeypatch) -> None:
    from pcai_llm.config import settings as _settings

    client_tools = [{"type": "function", "function": {"name": "mcp__x__search_logs"}}]
    ambiguous = [
        {"server": "logsearch", "tool": "search_logs", "description": "d"},
        {"server": "other", "tool": "search_logs", "description": "d"},
    ]
    # two gateway servers own the same tool name — not unambiguous, keep client
    kept, reverse = toolrelay.shadow_client_mcp(client_tools, ambiguous)
    assert kept == client_tools and reverse == {}
    # kill switch: shadowing off → the client variant stays offered
    monkeypatch.setattr(_settings, "tool_relay_shadow_mcp", False)
    kept, reverse = toolrelay.shadow_client_mcp(client_tools, [{"server": "logsearch", "tool": "search_logs"}])
    assert kept == client_tools and reverse == {}


def test_relay_stream_mixed_batch_rewrites_twins_for_client_execution() -> None:
    """Mixed batch (client tool + cloud tool with a client twin): NOTHING
    executes in-cluster — the cloud name is REWRITTEN to the client's twin and
    the whole batch hands back, so the client executes everything and the
    replayed history pairs every call with a proper role=tool result. (The
    execute+content-inject design made the history lie — observed live as an
    infinite re-issue loop.)"""
    import asyncio

    executed: list[tuple[str, str]] = []

    def stream_once(msgs, tools):
        async def gen():
            yield (
                "tool_calls",
                [
                    {"id": "c1", "type": "function", "function": {"name": "edit", "arguments": '{"path": "x"}'}},
                    {"id": "c2", "type": "function", "function": {"name": "logsearch__search_logs", "arguments": "{}"}},
                ],
            )

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {"found": "stuff"}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=[
                    {"type": "function", "function": {"name": "edit"}},
                    {"type": "function", "function": {"name": "mcp__logsearch__search_logs"}},
                ],
            )
        )
    )
    text = b"".join(out).decode()
    assert executed == []  # nothing ran in-cluster — the client executes both
    assert '"handback"' in text
    assert '"name": "edit"' in text
    assert '"name": "mcp__logsearch__search_logs"' in text  # rewritten to the client's twin
    assert "[tool result]" not in text  # no in-loop execution, nothing to inject
    assert '"finish_reason": "tool_calls"' in text
    assert text.rstrip().endswith("data: [DONE]")


def test_loop_guard_blocks_identical_calls_across_requests(monkeypatch) -> None:
    """The relay's in-loop detection is per-request; the handback moved part
    of the loop into the HARNESS (each handback = a new request). Identical
    (server, tool, args) calls >=5x in 10 minutes must be refused without
    executing — the cross-request circuit breaker."""
    from pcai_llm.api import routes_proxy

    monkeypatch.setattr(routes_proxy, "_loop_guard", {})
    kid = "key-1"
    args = {"q": "same"}
    for _ in range(routes_proxy._LOOP_GUARD_MAX_HITS):
        assert routes_proxy.loop_guard_check(kid, "logsearch", "search_logs", args) is None
    reason = routes_proxy.loop_guard_check(kid, "logsearch", "search_logs", args)
    assert reason is not None and "loop guard" in reason and "logsearch/search_logs" in reason
    # different args → different fingerprint → allowed
    assert routes_proxy.loop_guard_check(kid, "logsearch", "search_logs", {"q": "different"}) is None
    # different key → allowed
    assert routes_proxy.loop_guard_check("key-2", "logsearch", "search_logs", args) is None


def test_relay_stream_offer_shadows_but_handback_still_accepts_old_name() -> None:
    """Shadowing changes only the OFFER: the model is shown the in-cluster
    twin, but a history-imitating call under the client's original name still
    handbacks (correct, just slower) — never a denial."""
    import asyncio

    offered: list[list[dict] | None] = []
    executed: list[tuple[str, str]] = []

    calls = {"n": 0}

    def stream_once(msgs, tools):
        offered.append(tools)
        calls["n"] += 1

        async def gen():
            if calls["n"] == 1:  # model takes the bait: cloud twin
                yield (
                    "tool_calls",
                    [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "logsearch__search_logs", "arguments": "{}"},
                        }
                    ],
                )
            else:  # next turn replays history: model imitates the OLD name
                yield (
                    "tool_calls",
                    [
                        {
                            "id": "c2",
                            "type": "function",
                            "function": {"name": "mcp__logsearch__search_logs", "arguments": "{}"},
                        }
                    ],
                )

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {}

    out1 = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=[{"type": "function", "function": {"name": "mcp__logsearch__search_logs"}}],
            )
        )
    )
    # turn 1: twin offered (client variant shadowed); the model called the
    # CLOUD name in a PURE cloud batch → executes IN-CLUSTER (the shadowing
    # payoff — no VPN hop, and the loop guard sees the call)
    offered_names = [t["function"]["name"] for t in (offered[0] or [])]
    assert "logsearch__search_logs" in offered_names
    assert "mcp__logsearch__search_logs" not in offered_names
    text1 = b"".join(out1).decode()
    assert executed == [("logsearch", "search_logs")]
    assert b'"event": "tool_call"' in b"".join(out1)
    # iteration 1 yields the OLD-name call (scripted) → that one handbacks
    assert '"handback"' in text1 and '"name": "mcp__logsearch__search_logs"' in text1
    assert "denied" not in text1
    assert text1.rstrip().endswith("data: [DONE]")

    # turn 2 (simulated): old name NOT offered anymore — but still handbacks
    out2 = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=[{"type": "function", "function": {"name": "mcp__logsearch__search_logs"}}],
            )
        )
    )
    text2 = b"".join(out2).decode()
    assert executed == [
        ("logsearch", "search_logs")
    ]  # unchanged since phase 1 — old-name call handbacks, never executes
    assert '"handback"' in text2 and "mcp__logsearch__search_logs" in text2
    assert "denied" not in text2


def test_relay_stream_pure_cloud_batch_executes_incluster_despite_twins() -> None:
    """Regression (found live): a batch containing ONLY cloud-named calls with
    client twins must execute IN-CLUSTER — rewriting it into a handback sent
    every shadowed call over the client's VPN, starved the executor, and the
    loop guard never armed. Twin-rewrite is for MIXED batches only."""
    import asyncio

    executed: list[tuple[str, str]] = []

    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1

        async def gen():
            if calls["n"] == 1:
                yield (
                    "tool_calls",
                    [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "logsearch__search_logs", "arguments": "{}"},
                        }
                    ],
                )
            else:
                yield ("content", "done")
                yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {"ok": 1}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=[{"type": "function", "function": {"name": "mcp__logsearch__search_logs"}}],
            )
        )
    )
    text = b"".join(out).decode()
    assert executed == [("logsearch", "search_logs")]  # executed IN-CLUSTER
    assert '"event": "tool_call"' in text
    assert '"handback"' not in text  # pure cloud batch — no handback
    assert "done" in text and text.rstrip().endswith("data: [DONE]")


def test_relay_stream_aliases_harness_prefixed_ghost_call() -> None:
    """The name-duality failure (observed live): a conversation replayed after
    the offer-merge teaches the model the harness's own prefixed spelling —
    it calls mcp__pcai-llm-gateway__mm-rag__search_memory, which parses to a
    bogus server 'mcp' and errored 'unknown tool'. A call whose suffix
    matches a GRANTED gateway tool now executes the in-cluster twin instead
    (still grant-gated) — the model gets its result, the harness gets no
    unknown-tool error."""
    import asyncio

    executed: list[tuple[str, str]] = []

    def stream_once(msgs, tools):
        async def gen():
            yield (
                "tool_calls",
                [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "mcp__pcai-llm-gateway__mm-rag__search_memory", "arguments": '{"q": "x"}'},
                    }
                ],
            )

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {"hits": 1}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("mm-rag", "search_memory")},
                [{"server": "mm-rag", "tool": "search_memory", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    text = b"".join(out).decode()
    assert executed == [("mm-rag", "search_memory")]  # aliased to the granted twin
    assert '"event": "tool_call"' in text
    assert "unknown tool" not in text


def test_relay_stream_unknown_gateway_shaped_call_hints_granted_names() -> None:
    """A gateway-SHAPED call that matches NO granted tool denies with the
    granted-name list appended — the model can self-correct in one step
    instead of re-trying spellings blind. (ghost__tool parses to
    (ghost, tool); the alias leaves it; the ACL gate refuses with the hint.)"""
    import asyncio

    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1

        async def gen():
            if calls["n"] == 1:
                yield (
                    "tool_calls",
                    [{"id": "c1", "type": "function", "function": {"name": "ghost__tool", "arguments": "{}"}}],
                )
            else:
                yield ("content", "giving up")
                yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

        return gen()

    async def execute(server, tool, args):
        return True, {}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    text = b"".join(out).decode()
    # the SSE wire carries the short pcai event; the GRANTED hint rides the
    # role=tool message the next model call consumes — drive one more turn to
    # see the hint in the history the loop replays upstream.
    assert '"tool_error"' in text and '"denied by ACL"' in text

    seen: list[list[dict]] = []
    calls2 = {"n": 0}

    def stream_once2(msgs, tools):
        seen.append(list(msgs))
        calls2["n"] += 1

        async def gen():
            if calls2["n"] == 1:
                yield (
                    "tool_calls",
                    [{"id": "c1", "type": "function", "function": {"name": "ghost__tool", "arguments": "{}"}}],
                )
            else:
                yield ("content", "giving up")
                yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

        return gen()

    asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once2,
                execute,
                "rid",
                "m",
            )
        )
    )
    history = [m for m in seen[1] if m.get("role") == "tool"]
    assert history and "granted tools: logsearch__search_logs" in history[0]["content"]


def test_relay_stream_aliases_double_prefixed_ungranted_parse() -> None:
    """The actual live shape: mcp__pcai-llm-gateway__mm-rag__search_memory
    PARSES (single-strip yields a bogus 'pcai-llm-gateway' server) and would
    deny by ACL. The alias must also fire on the parsed-but-ungranted path —
    the suffix resolves to the granted twin, which then executes."""
    import asyncio

    executed: list[tuple[str, str]] = []

    def stream_once(msgs, tools):
        async def gen():
            yield (
                "tool_calls",
                [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "mcp__pcai-llm-gateway__logsearch__search_logs", "arguments": "{}"},
                    }
                ],
            )

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {"ok": 1}

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
            )
        )
    )
    text = b"".join(out).decode()
    assert executed == [("logsearch", "search_logs")]
    assert "denied" not in text and "unknown tool" not in text


def test_relay_stream_client_loop_guard_blocks_handback_loop() -> None:
    """Handback-path calls never reach the executor's loop guard — a harness
    loop repeating the same client call was invisible. The client-side guard
    refuses the fingerprint and feeds the refusal to the model IN-LOOP (a
    self-correction signal), so the turn ends with an answer, not another
    blind handback."""
    import asyncio

    executed: list[tuple[str, str]] = []
    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1

        async def gen():
            if calls["n"] == 1:
                yield (
                    "tool_calls",
                    [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "same"}'}}],
                )
            else:
                yield ("content", "wrapping up")
                yield ("usage", {"prompt_tokens": 1, "completion_tokens": 1})

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {}

    def guard(name: str, args: dict) -> str | None:
        if name == "bash":
            return "loop guard: identical call bash made 6x in 10m — change approach"
        return None

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                set(),
                [],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=[{"type": "function", "function": {"name": "bash"}}],
                client_loop_guard=guard,
            )
        )
    )
    text = b"".join(out).decode()
    assert executed == []  # refused in-loop — never handed back
    assert '"event": "guard"' in text and "loop guard" in text
    assert "wrapping up" in text  # the model self-corrected and answered
    assert '"finish_reason": "tool_calls"' not in text  # no handback happened
    assert text.rstrip().endswith("data: [DONE]")


async def _collect(agen):
    return [c async for c in agen]


def test_relay_stream_mid_loop_budget_failover(monkeypatch) -> None:
    """Mid-relay budget exhaustion fails over to the next cascade model and
    the conversation CONTINUES — the client sees the handoff as a reasoning
    line, settles stamp the fallback's name, and the turn still answers."""
    import asyncio
    import uuid as _uuid

    from pcai_llm.core.budgets import GateResult

    executed: list[tuple[str, str]] = []
    settled: list[tuple] = []
    calls = {"n": 0}

    async def fake_settle(*a, **k):
        settled.append(a)

    async def fake_trace(*a, **k):
        return None

    async def fake_budgets(key_id, model, est):
        if model == "primary-model":
            return GateResult(False, "month budget exceeded")
        return GateResult(True)

    monkeypatch.setattr(toolrelay, "settle", fake_settle)
    monkeypatch.setattr(toolrelay.traces, "record", fake_trace)
    monkeypatch.setattr(toolrelay, "check_budgets", fake_budgets)

    def stream_once(msgs, tools, pool_for=None):
        calls["n"] += 1

        async def gen():
            if calls["n"] == 1:
                yield (
                    "tool_calls",
                    [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "logsearch__search_logs", "arguments": "{}"},
                        }
                    ],
                )
            else:
                yield ("content", "recovered on the fallback model")
                yield ("usage", {"prompt_tokens": 5, "completion_tokens": 3})

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {"ok": 1}

    def fb_stream(msgs, tools):
        return stream_once(msgs, tools)

    out = asyncio.run(
        _collect(
            toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "primary-model",
                key_id=_uuid.uuid4(),
                fallbacks=[("fallback-model", fb_stream)],
            )
        )
    )
    text = b"".join(out).decode()
    assert executed == [("logsearch", "search_logs")]
    assert "failing over to fallback-model" in text
    # near-transparent handoff: the WHY (budget prose, gate reasons) lives in
    # the ledger/trace, not in the user-visible reasoning text
    assert "budget exhausted mid-task" not in text
    assert "recovered on the fallback model" in text
    assert any(len(a) > 2 and a[1] == "fallback-model" for a in settled)  # settles stamp the fallback
    assert any(len(a) > 2 and a[1] == "primary-model" for a in settled)  # the exhaustion marker too
    assert text.rstrip().endswith("data: [DONE]")


# ---------------------------------------------------------------------------
# wire-visibility deltas (relay_stream_wire_tools): default OFF — the DSH
# harness EXECUTES native tool_call deltas instead of rendering them, so a
# pure-cloud batch's informational deltas became double execution / phantom
# "unknown tool" errors fed back to the model (live 2026-09-21, trace
# 948c1228). Handback deltas are a DIFFERENT emission path (unconditional)
# and must be untouched by the flag.
# ---------------------------------------------------------------------------


def _wire_probe_env(monkeypatch, *, flag: bool):
    """One model turn whose ONLY call is a granted in-cluster tool; the
    executor records and succeeds; the follow-up model call answers."""

    monkeypatch.setattr(toolrelay.settings, "relay_stream_wire_tools", flag)
    executed: list[tuple[str, str]] = []

    def stream_once(msgs, tools):
        async def gen():
            yield (
                "tool_calls",
                [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "logsearch__search_logs", "arguments": '{"q": "x"}'},
                    }
                ],
            )

        return gen()

    async def execute(server, tool, args):
        executed.append((server, tool))
        return True, {"found": "stuff"}

    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
            )
        ]

    return run, executed


def test_relay_wire_deltas_off_by_default_pure_cloud(monkeypatch) -> None:
    """Flag OFF (the new default): a pure-cloud batch must NOT re-emit the
    model's tool_call as native deltas — the relay's pcai tool_call event and
    the reasoning-text trace carry visibility instead."""
    import asyncio

    run, executed = _wire_probe_env(monkeypatch, flag=False)
    text = b"".join(asyncio.run(run())).decode()
    assert executed == [("logsearch", "search_logs")]
    # the native re-emission is absent…
    assert '"function": {"name": "logsearch__search_logs", "arguments": "{\\"q\\": \\"x\\"}"}' not in text
    # …but visibility survives via the pcai event + reasoning trace
    assert '"event": "tool_call"' in text
    assert "[tool] logsearch__search_logs" in text
    assert '"finish_reason": "tool_calls"' not in text


def test_relay_wire_deltas_on_restores_native_emission(monkeypatch) -> None:
    """Flag ON (legacy): the native delta re-emission is present, for clients
    that render deltas without executing them."""
    import asyncio

    run, executed = _wire_probe_env(monkeypatch, flag=True)
    text = b"".join(asyncio.run(run())).decode()
    assert executed == [("logsearch", "search_logs")]
    assert '"function": {"name": "logsearch__search_logs"' in text
    assert '"finish_reason": "tool_calls"' not in text  # still no finish_reason mid-loop


def test_relay_handback_deltas_unconditional(monkeypatch) -> None:
    """Handback deltas are NOT gated by the flag: with the flag OFF, a mixed
    batch still hands the client calls back as native deltas — that is the
    client's own execution path, not informational."""
    import asyncio

    monkeypatch.setattr(toolrelay.settings, "relay_stream_wire_tools", False)

    def stream_once(msgs, tools):
        async def gen():
            yield (
                "tool_calls",
                [
                    {"id": "c1", "type": "function", "function": {"name": "edit", "arguments": '{"path": "x"}'}},
                    {"id": "c2", "type": "function", "function": {"name": "logsearch__search_logs", "arguments": "{}"}},
                ],
            )

        return gen()

    async def execute(server, tool, args):
        return True, {"found": "stuff"}

    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                [{"server": "logsearch", "tool": "search_logs", "description": "d"}],
                stream_once,
                execute,
                "rid",
                "m",
                client_tools=[{"type": "function", "function": {"name": "edit"}}],
            )
        ]

    text = b"".join(asyncio.run(run())).decode()
    assert '"name": "edit"' in text  # client half handed back as native delta
    assert '"finish_reason": "tool_calls"' in text


# ---------------------------------------------------------------------------
# stream_pcai_events (RFC-PER-KEY-STREAM-EVENTS) — OFF-mode stream purity
# ---------------------------------------------------------------------------


def test_relay_stream_off_mode_is_spec_clean() -> None:
    """stream_pcai_events=False yields ZERO {"pcai" lines across thinking,
    tool execution, and the final answer — every non-spec telemetry line is
    suppressed while every spec-clean byte survives: chat.completion.chunk
    deltas, reasoning_content, keepalive comments, [DONE]. This is the
    contract strict validating clients (opencode, clearwing) depend on."""
    import asyncio

    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1
        if calls["n"] == 1:

            async def gen1():
                class R:
                    async def aiter_bytes(self):
                        yield b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n'
                        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"logsearch__search_logs","arguments":"{}"}}]}}]}\n\n'

                resp = R()
                async for ev in toolrelay.stream_model_call(resp):
                    yield ev

            return gen1()

        async def gen2():
            class R:
                async def aiter_bytes(self):
                    yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'

            resp = R()
            async for ev in toolrelay.stream_model_call(resp):
                yield ev

        return gen2()

    async def execute(server, tool, args):
        return True, {"ok": 1}

    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                CATALOG,
                stream_once,
                execute,
                "rid",
                "m",
                stream_pcai_events=False,
            )
        ]

    text = b"".join(asyncio.run(run())).decode()
    # the RFC's mitigation test: the non-spec marker NEVER appears
    assert '{"pcai"' not in text
    assert '"pcai"' not in text
    # spec-clean channels all intact
    assert "reasoning_content" in text  # thinking rides the compliant delta
    assert "done" in text and "[DONE]" in text
    assert "chat.completion.chunk" in text


def test_relay_stream_default_on_keeps_bytes() -> None:
    """Default ON = byte-identical to today: telemetry events still flow
    (console/DSH contract unchanged)."""
    import asyncio

    calls = {"n": 0}

    def stream_once(msgs, tools):
        calls["n"] += 1
        if calls["n"] == 1:

            async def gen1():
                class R:
                    async def aiter_bytes(self):
                        yield b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n'
                        yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c","function":{"name":"logsearch__search_logs","arguments":"{}"}}]}}]}\n\n'

                resp = R()
                async for ev in toolrelay.stream_model_call(resp):
                    yield ev

            return gen1()

        async def gen2():
            class R:
                async def aiter_bytes(self):
                    yield b'data: {"choices":[{"delta":{"content":"done"}}]}\n\n'

            resp = R()
            async for ev in toolrelay.stream_model_call(resp):
                yield ev

        return gen2()

    async def execute(server, tool, args):
        return True, {"ok": 1}

    async def run():
        return [
            c
            async for c in toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                {("logsearch", "search_logs")},
                CATALOG,
                stream_once,
                execute,
                "rid",
                "m",
            )
        ]

    text = b"".join(asyncio.run(run())).decode()
    assert '"pcai"' in text  # today's bytes, unchanged
    assert '"thinking"' in text and '"tool_call"' in text
    assert "done" in text and "[DONE]" in text
