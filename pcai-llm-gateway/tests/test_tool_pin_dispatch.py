"""Tool-definition pinning, part 2: ANNOTATIONS in the pin (GAP 1) and the
DISPATCH-time gate in block mode (GAP 2).

WHY A SEPARATE FILE: tests/test_tool_pin.py pins the fingerprint / store /
offered-surface contract. These tests pin the two behaviours that make the
defence more than a speed bump:

  * GAP 1 — destructiveHint/readOnlyHint are the SECURITY-relevant part of a
    definition (a rug-pull can flip them without touching name, description or
    schema), so the relay catalog must CARRY them and the fingerprint must
    change when ONLY they change.
  * GAP 2 — removing a changed tool from the OFFERED surface does not stop a
    model that names it from conversation history. The dispatch gate re-checks
    the pin when the call ARRIVES and, in "block" mode, returns an ORDINARY
    tool error instead of reaching the executor. "warn"/"off" dispatch exactly
    as before.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest

from pcai_llm.config import settings
from pcai_llm.core import tool_pin, toolrelay

SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string", "description": "query"}},
    "required": ["q"],
}
# The approved annotations. readOnlyHint=True is what makes this tool SAFE to
# call; a rug-pull flips it to destructive.
ANNOTATIONS = {"readOnlyHint": True, "openWorldHint": False}
# The rug-pull: SAME name, SAME description, SAME inputSchema — only the
# annotations (the hints a reviewer relied on) changed.
FLIPPED_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}

APPROVED = [
    {
        "server": "logsearch",
        "tool": "search_logs",
        "description": "search logs",
        "inputSchema": SCHEMA,
        "annotations": ANNOTATIONS,
    }
]
CHANGED = [{**APPROVED[0], "annotations": FLIPPED_ANNOTATIONS}]


def _key():
    from pcai_llm.api.auth import KeyRecord

    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy", "mcp"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


# ---------------------------------------------------------------------------
# GAP 1 — annotations ride the relay catalog into the fingerprint
# ---------------------------------------------------------------------------


def _relay_env(monkeypatch: pytest.MonkeyPatch, tools: list[dict]) -> None:
    """Wire routes_proxy._relay_catalog to ONE enabled server offering `tools`.

    Mirrors tests/test_deny_rows.py::_relay_catalog_env: the catalog fetch is
    stubbed at the routes_mcp seam (the function is imported INSIDE
    _relay_catalog, so the module attribute is what must be patched) and the
    deny rows are empty. Scope grant mode so the fetched catalog IS the grant.
    """
    import pcai_llm.api.routes_mcp as rm
    import pcai_llm.api.routes_proxy as rp
    from pcai_llm.core import registry as registry_mod
    from pcai_llm.mcpf import federation

    async def fake_fetch_tools(_url, _headers, timeout_s=None):
        return [dict(t) for t in tools]

    monkeypatch.setattr(rm, "_fetch_tools", fake_fetch_tools)
    monkeypatch.setattr(rp, "registry", registry_mod)

    class _Reg:
        async def mcp_servers(self):
            return [
                {
                    "name": "logsearch",
                    "url": "https://logsearch.invalid",
                    "headers": {},
                    "auth_secret": None,
                    "auth_token_enc": None,
                    "enabled": True,
                    "tool_policy": "all",
                    "allowed_tools": [],
                    "description": "log search",
                }
            ]

    monkeypatch.setattr(registry_mod, "mcp_servers", _Reg().mcp_servers)

    async def _no_denies(_key_id):
        return set()

    monkeypatch.setattr(federation, "load_denies", _no_denies)
    monkeypatch.setattr(rp.settings, "tool_relay_grant_mode", "scope")
    rp.catalog_cache_clear()


async def test_catalog_carries_annotations_and_an_annotations_only_change_is_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """END TO END (GAP 1): upstream annotations -> _relay_catalog entry ->
    tool_pin.fingerprint -> CHANGED when ONLY the annotations changed.

    The relay used to drop this field, so a server could flip destructiveHint
    (the one hint a reviewer relies on to know whether a call destroys data)
    and the pin would report MATCH: the defence was blind to the field it most
    needed to see.
    """
    import pcai_llm.api.routes_proxy as rp

    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "block")
    key = _key()

    # turn 1: the definition the operator approved
    _relay_env(
        monkeypatch,
        [
            {
                "tool": "search_logs",
                "description": "search logs",
                "inputSchema": SCHEMA,
                "annotations": ANNOTATIONS,
            },
            # a tool that declares NO annotations must not gain an empty dict:
            # absent and empty pin the same definition (fingerprint() says so).
            {"tool": "bare_tool", "description": "no hints", "inputSchema": SCHEMA},
        ],
    )
    approved, _grants = await rp._relay_catalog(key, set())
    by_tool = {t["tool"]: t for t in approved}
    assert by_tool["search_logs"]["annotations"] == ANNOTATIONS
    assert "annotations" not in by_tool["bare_tool"]
    assert [t["tool"] for t in toolrelay.pin_catalog(approved)] == ["search_logs", "bare_tool"]

    # turn 2: SAME name/description/schema, ONLY the annotations flipped
    rp.catalog_cache_clear()
    _relay_env(
        monkeypatch,
        [
            {
                "tool": "search_logs",
                "description": "search logs",
                "inputSchema": SCHEMA,
                "annotations": FLIPPED_ANNOTATIONS,
            }
        ],
    )
    changed, _grants2 = await rp._relay_catalog(key, set())
    entry = changed[0]
    assert entry["annotations"] == FLIPPED_ANNOTATIONS
    # proof the change is ANNOTATIONS-ONLY: the other three pinned fields are
    # byte-identical to the approved listing
    assert entry["description"] == by_tool["search_logs"]["description"]
    assert entry["inputSchema"] == by_tool["search_logs"]["inputSchema"]
    assert tool_pin.fingerprint("search_logs", "search logs", SCHEMA, ANNOTATIONS) != tool_pin.fingerprint(
        "search_logs", "search logs", SCHEMA, FLIPPED_ANNOTATIONS
    )
    # ...and block mode therefore WITHHOLDS it from the offer
    assert [t["tool"] for t in toolrelay.pin_catalog(changed)] == []


# ---------------------------------------------------------------------------
# GAP 2 — dispatch-time gate
# ---------------------------------------------------------------------------


def _run_turn(
    catalog: list[dict],
    *,
    tool_name: str = "logsearch__search_logs",
    grants: set[tuple[str, str]] | None = None,
) -> tuple[bytes, list[tuple[str, str, dict]], list[list[dict]]]:
    """Run ONE relay turn whose FIRST model call names `tool_name`.

    Returns (sse_bytes, executed, model_calls): `executed` is the executor's
    record (empty == the call never reached upstream) and `model_calls` is
    the message list the model saw on each call — the second one carries the
    role=tool result, i.e. exactly what the model would read.
    """
    executed: list[tuple[str, str, dict]] = []
    model_calls: list[list[dict]] = []

    def stream_once(msgs, tools):
        model_calls.append([dict(m) for m in msgs])
        if len(model_calls) == 1:

            async def first():
                yield (
                    "tool_calls",
                    [
                        {
                            "id": "call_pin_1",
                            "type": "function",
                            "function": {"name": tool_name, "arguments": "{}"},
                        }
                    ],
                )

            return first()

        async def answer():
            yield ("content", "done")

        return answer()

    async def execute(server: str, tool: str, args: dict) -> tuple[bool, object]:
        executed.append((server, tool, args))
        return True, {"ok": True}

    async def run() -> list[bytes]:
        return [
            chunk
            async for chunk in toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                grants if grants is not None else {("logsearch", "search_logs")},
                catalog,
                stream_once,
                execute,
                "rid",
                "m",
            )
        ]

    return b"".join(asyncio.run(run())), executed, model_calls


def test_block_mode_refuses_a_changed_tool_at_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GAP 2 REGRESSION: the tool is GONE from the offer, the model names
    it anyway (history replay), and the executor must never be reached."""
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "block")
    # the approved definition is pinned (what the first listing records)
    assert [t["tool"] for t in toolrelay.pin_catalog(APPROVED)] == ["search_logs"]

    text, executed, model_calls = _run_turn(CHANGED)

    assert executed == [], "a rug-pulled tool must NOT reach the executor in block mode"
    # The refusal is an ORDINARY tool error: same tool_call_id, a non-empty
    # string, and it names the tool — so the relay loop keeps iterating (a
    # refusal that broke the loop would be worse than the attack).
    assert len(model_calls) == 2, "the loop must CONTINUE after a refusal"
    result = model_calls[1][-1]
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call_pin_1"
    assert isinstance(result["content"], str) and result["content"].strip()
    assert result["content"].startswith("error:")
    assert "logsearch__search_logs" in result["content"]
    assert "changed since this grant approved it" in result["content"]
    # the model got the refusal, not the (mock) result
    assert '"ok": true' not in result["content"]
    # ...and the wire carries the same story: a tool_error event + a normal
    # final answer (the turn did not die)
    wire = text.decode()
    assert '"event": "tool_error"' in wire
    assert "definition changed since approval" in wire
    assert '"done"' in wire


def test_block_mode_dispatches_an_unchanged_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: the gate is SELECTIVE — an unchanged definition still runs."""
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "block")
    toolrelay.pin_catalog(APPROVED)

    _text, executed, model_calls = _run_turn(APPROVED)

    assert executed == [("logsearch", "search_logs", {})]
    assert model_calls[1][-1]["content"] == '{"ok": true}'


def test_warn_mode_still_dispatches_a_changed_tool(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """warn keeps today's behaviour exactly: log the change, run the tool."""
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "warn")
    toolrelay.pin_catalog(APPROVED)

    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.toolrelay"):
        _text, executed, model_calls = _run_turn(CHANGED)

    assert executed == [("logsearch", "search_logs", {})], "warn must never block dispatch"
    assert model_calls[1][-1]["content"] == '{"ok": true}'
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "logsearch/search_logs" in messages
    assert "changed since it was pinned" in messages


def test_off_mode_still_dispatches_a_changed_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """off is inert: nothing observed, nothing refused."""
    store = tool_pin.PinStore()
    monkeypatch.setattr(toolrelay, "_PIN_STORE", store)
    monkeypatch.setattr(settings, "tool_pin_mode", "off")
    toolrelay.pin_catalog(APPROVED)
    assert len(store) == 0

    _text, executed, model_calls = _run_turn(CHANGED)

    assert executed == [("logsearch", "search_logs", {})]
    assert model_calls[1][-1]["content"] == '{"ok": true}'


def test_no_pin_at_dispatch_time_dispatches_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """RESTART: the in-memory store is empty, so the first listing pins the
    definition it sees (FIRST_SEEN) — that is not a change, and the call runs."""
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "block")

    _text, executed, model_calls = _run_turn(APPROVED)

    assert executed == [("logsearch", "search_logs", {})]
    assert model_calls[1][-1]["content"] == '{"ok": true}'


def test_tool_that_vanished_after_pinning_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE P0 BYPASS (cross-validation 2026-11-10): pin the approved
    definition, rewrite it, then OMIT the tool from the listing (or fail the
    listing). The ACL is name-based, so with no verdict the rewritten
    definition used to run ungated forever. Block mode must refuse."""
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "block")
    # turn 1: the approved definition is pinned by the offer pass
    assert [t["tool"] for t in toolrelay.pin_catalog(APPROVED)] == ["search_logs"]

    # turn 2: the server omits the tool (empty/failed listing)
    text, executed, model_calls = _run_turn([])

    assert executed == [], "a vanished pinned tool must NOT reach the executor in block mode"
    assert len(model_calls) == 2, "the loop must CONTINUE after a refusal"
    result = model_calls[1][-1]
    assert result["role"] == "tool"
    assert isinstance(result["content"], str) and result["content"].startswith("error:")
    assert "logsearch__search_logs" in result["content"]
    assert "vanished from the listing since this grant approved it" in result["content"]
    wire = text.decode()
    assert '"event": "tool_error"' in wire
    assert "vanished from the listing since approval" in wire
    assert '"done"' in wire


def test_tool_never_pinned_absent_from_the_listing_still_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: the refusal needs a LIVE pin. A tool that was NEVER pinned
    (first run, restart, a listing that never carried it) must still fail
    OPEN -- a pinning defence must not break a turn."""
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "block")

    _text, executed, model_calls = _run_turn([])

    assert executed == [("logsearch", "search_logs", {})]
    assert model_calls[1][-1]["content"] == '{"ok": true}'


# ---------------------------------------------------------------------------
# the pieces the gate is built from (pure, so they are cheap to pin)
# ---------------------------------------------------------------------------


def test_pin_catalog_with_verdicts_exposes_the_changed_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch gate reads the verdict map — it must report CHANGED for
    the tool the offer dropped (that tool is ABSENT from the offered list, so
    the map is the only place the relay can still see it)."""
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "block")

    kept, verdicts = toolrelay.pin_catalog_with_verdicts(APPROVED)
    assert [t["tool"] for t in kept] == ["search_logs"]
    assert verdicts[("logsearch", "search_logs")].verdict == tool_pin.FIRST_SEEN

    kept2, verdicts2 = toolrelay.pin_catalog_with_verdicts(CHANGED)
    assert kept2 == []
    changed_verdict = verdicts2[("logsearch", "search_logs")]
    assert changed_verdict.changed is True
    assert changed_verdict.previous != changed_verdict.current


def test_pin_dispatch_refusal_only_fires_for_block_and_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    verdicts = {("s", "t"): tool_pin.PinVerdict(tool_pin.CHANGED, "aaaa", "bbbb")}
    monkeypatch.setattr(settings, "tool_pin_mode", "block")
    assert toolrelay.pin_dispatch_refusal("s", "t", verdicts) is not None
    # no observation for this tool AND no live pin -> dispatch (fail open)
    assert toolrelay.pin_dispatch_refusal("s", "other", verdicts) is None
    # a MATCH is not a change
    matched = {("s", "t"): tool_pin.PinVerdict(tool_pin.MATCH, "aaaa", "aaaa")}
    assert toolrelay.pin_dispatch_refusal("s", "t", matched) is None
    # a VANISHED verdict refuses, even with no observed definition
    vanished = {("s", "gone"): tool_pin.PinVerdict(tool_pin.VANISHED, "approved00000000", "")}
    refusal = toolrelay.pin_dispatch_refusal("s", "gone", vanished)
    assert refusal is not None
    assert "vanished from the listing since this grant approved it" in refusal
    for mode in ("warn", "off"):
        monkeypatch.setattr(settings, "tool_pin_mode", mode)
        assert toolrelay.pin_dispatch_refusal("s", "t", verdicts) is None


def test_pin_dispatch_refusal_refuses_a_live_pin_absent_from_the_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate's store fallback: when the listing carried NO entry for a tool
    the store still has a live pin for, that is a disappearance -- not an
    unobserved first run -- so block mode refuses."""
    store = tool_pin.PinStore()
    store.observe("logsearch", "search_logs", "approved00000000")
    monkeypatch.setattr(toolrelay, "_PIN_STORE", store)
    monkeypatch.setattr(settings, "tool_pin_mode", "block")

    refusal = toolrelay.pin_dispatch_refusal("logsearch", "search_logs", {})
    assert refusal is not None
    assert "vanished from the listing since this grant approved it" in refusal
    assert "approved00000000" in refusal
    # an EXPIRED pin is not a live pin (the store's documented TTL re-approval)
    store.ttl_s = 0.0
    store.reset()
    assert toolrelay.pin_dispatch_refusal("logsearch", "search_logs", {}) is None
    # warn/off never refuse a vanished tool either
    store.observe("logsearch", "search_logs", "approved00000000")
    for mode in ("warn", "off"):
        monkeypatch.setattr(settings, "tool_pin_mode", mode)
        assert toolrelay.pin_dispatch_refusal("logsearch", "search_logs", {}) is None


def test_pin_catalog_with_verdicts_surfaces_a_vanished_tool(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """The OFFER pass must say so too: a pinned tool absent from the listing it
    belongs in is logged, and block mode records a VANISHED verdict so the
    dispatch gate refuses without a second store lookup."""
    monkeypatch.setattr(toolrelay, "_PIN_STORE", tool_pin.PinStore())
    monkeypatch.setattr(settings, "tool_pin_mode", "block")
    assert [t["tool"] for t in toolrelay.pin_catalog(APPROVED)] == ["search_logs"]

    with caplog.at_level(logging.ERROR, logger="pcai_llm.core.toolrelay"):
        kept, verdicts = toolrelay.pin_catalog_with_verdicts([], grants={("logsearch", "search_logs")})

    assert kept == []
    verdict = verdicts[("logsearch", "search_logs")]
    assert verdict.vanished is True
    assert verdict.previous
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "VANISHED" in messages
    # a pin outside this turn's grants is another key's grant, not a
    # disappearance (the store is process-wide)
    kept2, verdicts2 = toolrelay.pin_catalog_with_verdicts([], grants={("applygate", "plan_apply")})
    assert kept2 == []
    assert verdicts2 == {}


def test_dispatch_refusal_text_names_the_tool_and_both_digests() -> None:
    """The refusal must be self-explanatory in a trace: WHICH tool, WHICH
    definition it was approved as, WHAT it changed to."""
    verdict = tool_pin.PinVerdict(tool_pin.CHANGED, "approved00000000", "rugpull000000000")
    text = tool_pin.dispatch_refusal("logsearch", "search_logs", verdict)
    assert text.startswith("error:")
    assert "logsearch__search_logs" in text
    assert "approved00000000" in text
    assert "rugpull000000000" in text
    assert "changed since this grant approved it" in text
    # never claims the call was malformed: the model must not retry it blindly
    assert "NOT executed" in text
