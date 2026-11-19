"""Cross-side contract: the relay wire shape as the DSH harness consumes it.

DSH's relay port (`packages/llm/llm-pi-ai/src/stream.ts` plus the
`@earendil-works/pi-ai` dist patch) reads exactly four things out of this
stream. Each test below pins one of them, so a rename on EITHER side breaks a
test here instead of silently degrading every relayed card.

  1. a render delta whose TOP-LEVEL object carries `{"pcai": {"relay": true}}`
     -- the patch tests `chunk.pcai.relay === true` on the parsed chunk, so a
     nested stamp would never be seen and the call would render as executable;
  2. that delta's `choices[0].delta.tool_calls[i].index` -- pi-ai keys its
     `toolCallBlocksByIndex` map by it and never clears that map for the
     response, so the index space MUST stay monotone across relay iterations
     (audit 2026-10-26 F-1: a per-iteration reset fused two calls into one card
     with invalid JSON while the gateway had really executed both);
  3. the call `id` + `function.name`/`function.arguments` used to build the card;
  4. a later telemetry event `{"pcai": {"event": "tool_call", "id", "ok", "ms",
     "result"}}` joined back to the card by id.

The join in (4) is what turns a card from "executed server-side" into
"ok (175ms) <preview>", so the field names are part of the contract.
"""

import json

import test_toolrelay_r1 as r1

MODEL_ONE = [
    {
        "content": None,
        "tool_calls": [
            {"id": "c1", "function": {"name": "logsearch__search_logs", "arguments": '{"q":"gpu"}'}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    },
    {"content": "done"},
]

MODEL_TWO_ITERATIONS = [
    {
        "content": None,
        "tool_calls": [
            {"id": "c1", "function": {"name": "logsearch__search_logs", "arguments": '{"q":"a"}'}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    },
    {
        "content": None,
        "tool_calls": [
            {"id": "c2", "function": {"name": "logsearch__search_logs", "arguments": '{"q":"b"}'}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    },
    {"content": "done"},
]

GRANTS = {("logsearch", "search_logs")}


def _chunks(text: str) -> list[dict]:
    """Every parsed `data:` payload, in wire order."""
    out: list[dict] = []
    for line in text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            out.append(json.loads(line[len("data: "):]))
        except json.JSONDecodeError:
            continue
    return out


def _render_deltas(text: str) -> list[dict]:
    return [c for c in _chunks(text) if (c.get("pcai") or {}).get("relay") is True]


def _telemetry(text: str) -> list[dict]:
    return [
        c["pcai"]
        for c in _chunks(text)
        if isinstance(c.get("pcai"), dict) and "relay" not in c["pcai"] and c["pcai"].get("event") == "tool_call"
    ]


def test_stamp_is_top_level_never_nested() -> None:
    """DSH reads `chunk.pcai.relay`; a stamp nested under choices/delta is
    invisible to it and the call would be treated as executable."""
    text, _ = r1._drive(MODEL_ONE, [(True, {"hits": 1})], grants=GRANTS, render_deltas=True)
    deltas = _render_deltas(text)
    assert len(deltas) == 1
    chunk = deltas[0]
    assert chunk["pcai"] == {"relay": True}
    # and it is NOT reachable from inside the delta body
    assert "pcai" not in chunk["choices"][0]
    assert "pcai" not in chunk["choices"][0]["delta"]


def test_render_delta_is_wire_compatible_with_handback() -> None:
    """A stamp-blind client must be able to parse this as an ordinary
    tool_call delta (that is the compatibility claim in the docstring)."""
    text, _ = r1._drive(MODEL_ONE, [(True, {"hits": 1})], grants=GRANTS, render_deltas=True)
    chunk = _render_deltas(text)[0]
    assert chunk["object"] == "chat.completion.chunk"
    tc = chunk["choices"][0]["delta"]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["id"] == "c1"
    assert tc["function"]["name"] == "logsearch__search_logs"
    assert json.loads(tc["function"]["arguments"]) == {"q": "gpu"}


def test_render_index_is_monotone_across_iterations() -> None:
    """F-1 guard. pi-ai's block map is keyed by this index and is never
    cleared for the response, so iteration 2 must continue the space rather
    than restart at the base."""
    text, calls = r1._drive(
        MODEL_TWO_ITERATIONS,
        [(True, {"hits": 1}), (True, {"hits": 2})],
        grants=GRANTS,
        render_deltas=True,
        render_index_base=1000,
    )
    assert calls["exec"] == 2
    indexes = [
        c["choices"][0]["delta"]["tool_calls"][0]["index"] for c in _render_deltas(text)
    ]
    assert indexes == [1000, 1001], f"index space reset between iterations: {indexes}"


def test_telemetry_carries_the_fields_dsh_reads() -> None:
    """event/id/ok/ms/result are each read by the pi-ai patch."""
    text, _ = r1._drive(MODEL_ONE, [(True, {"hits": 1})], grants=GRANTS, render_deltas=True)
    events = _telemetry(text)
    assert len(events) == 1
    ev = events[0]
    assert ev["event"] == "tool_call"
    assert ev["id"] == "c1"
    assert ev["ok"] is True
    assert isinstance(ev["ms"], int)
    assert isinstance(ev["result"], str)


def test_error_outcome_keeps_tool_call_event_with_ok_false() -> None:
    """A failed relayed call must still arrive as `event: "tool_call"` with
    ok=false -- that is the arm the patch maps to an error card. If it moved to
    a different event name with a `detail` field, the patch's error arm expects
    `detail`, and a mismatch would blank the card body."""
    text, _ = r1._drive(
        MODEL_ONE, [(False, {"error": "boom"})], grants=GRANTS, render_deltas=True
    )
    events = _telemetry(text)
    assert len(events) == 1
    assert events[0]["ok"] is False
    assert isinstance(events[0]["result"], str)


def test_every_render_delta_joins_to_a_telemetry_event() -> None:
    """The card only settles to a real outcome when the telemetry id matches
    the render id. An orphan render delta degrades to 'executed server-side'."""
    text, _ = r1._drive(
        MODEL_TWO_ITERATIONS,
        [(True, {"hits": 1}), (True, {"hits": 2})],
        grants=GRANTS,
        render_deltas=True,
    )
    render_ids = [c["choices"][0]["delta"]["tool_calls"][0]["id"] for c in _render_deltas(text)]
    event_ids = [e["id"] for e in _telemetry(text)]
    assert render_ids == ["c1", "c2"]
    assert sorted(render_ids) == sorted(event_ids)


def test_render_deltas_are_inert_client_still_executes_once() -> None:
    """Display-only: the gateway executes, the stamp tells the client not to.
    Exactly one execution is the 2026-09-21 double-execution guard."""
    text, calls = r1._drive(MODEL_ONE, [(True, {"hits": 1})], grants=GRANTS, render_deltas=True)
    assert calls["exec"] == 1
    assert len(_render_deltas(text)) == 1
