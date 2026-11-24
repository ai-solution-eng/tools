"""Wiring guard: the payload store must be FED at every tool-phase relay step.

WHY THIS FILE EXISTS (2026-11-23). The quality layer was built in two halves by two
agents: a storage half (traces.record / trace_payloads) and call sites in the relay loop.
Both halves can be green while the feature is dead — the storage is unit-tested, the call
sites exist, and nothing passes the arguments between them. That failure is invisible to
every unit test and silent in production, so it gets a structural test.

Three things are asserted:
  1. STRUCTURAL — EVERY `_trace_step(<iter>, "tool", ...)` site in core/toolrelay.py passes
     `tool_args`, or is listed in PAYLOAD_ALLOWLIST with the reason inline. The first
     version of this guard only asked for "at least one" fed site, so it ratified a 1-of-10
     wiring (A4, 2026-11-23: the refusal gap survived precisely because "at least one" was
     the bar). A new unwired tool-phase site is now a test failure, not a silent hole.
  2. BEHAVIOURAL — driving the REAL relay loop with the payload plumbing live shows the
     refusal's arguments/result reaching trace_payloads.record_step (monkeypatched) for the
     ACL denial, the malformed-arguments refusal, the unknown-tool refusal, the client
     loop-guard denial and a client handback.
  3. traces.record forwards them into the payload store, and pays nothing when sampling is
     off (the default deployment must not regress).

COUNT (2026-11-23, corrected during A4): toolrelay.py has 12 `_trace_step` call sites — 10
with phase="tool" (ALL fed) and 2 with phase="chat" (the model-call settle and the mid-loop
budget marker; they carry no payload BY DESIGN — the hook in traces.record fires only for
phase "tool"). The register's earlier "13 tool-phase sites / 1 fed" was a grep artefact:
the pattern also matched the `async def _trace_step` definition.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from pcai_llm.core import chain_audit, toolrelay, trace_payloads, traces  # noqa: E402
from pcai_llm.core.budgets import GateResult  # noqa: E402

RELAY = SRC / "pcai_llm" / "core" / "toolrelay.py"

# The catalog/grants the behavioural drivers offer. Two tools, one granted in the
# denial tests — enough to tell an ACL refusal from an unknown name.
CATALOG = [
    {"server": "logsearch", "tool": "search_logs", "description": "search"},
    {"server": "applygate", "tool": "plan_apply", "description": "plan"},
]


# ---------------------------------------------------------------------------
# 1. STRUCTURAL GUARD — every tool-phase site is fed, or explicitly excused.
# ---------------------------------------------------------------------------


def _tool_step_calls() -> list[ast.Call]:
    """Every `_trace_step(<iter>, "tool", ...)` call in the relay module."""
    tree = ast.parse(RELAY.read_text(encoding="utf-8"))
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name != "_trace_step":
            continue
        phase = node.args[1] if len(node.args) > 1 else None
        if isinstance(phase, ast.Constant) and phase.value == "tool":
            found.append(node)
    return found


def _kw_value(call: ast.Call, name: str) -> ast.expr | None:
    """The AST value of one keyword argument, or None when the keyword is absent."""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _has_kw(call: ast.Call, name: str) -> bool:
    return _kw_value(call, name) is not None


def _site_signature(call: ast.Call) -> str:
    """A positional-independent identity for one call site (for the allowlist).

    `ast.unparse` of the WHOLE call, so editing an allowlisted site (adding a kwarg,
    changing a value) changes its signature and the entry goes stale — which the test
    below turns into a failure, forcing a re-read of the excuse. Line numbers are
    deliberately NOT part of the key: an unrelated edit above a site must not make an
    allowlist entry point at the wrong place.
    """
    return ast.unparse(call)


# Sites that CANNOT carry a payload, with the reason inline, keyed by
# _site_signature(call). An entry that matches no live call site fails
# test_payload_allowlist_has_no_stale_entries, so an exception cannot outlive the
# code it excuses.
#
# EMPTY ON PURPOSE (2026-11-23, A4): all 10 tool-phase sites have the model's
# arguments in scope, so every one is fed. The mechanism stays because a future
# tool-phase step that legitimately has no arguments needs a documented place to
# say why, instead of the silent hole this guard exists to prevent.
PAYLOAD_ALLOWLIST: dict[str, str] = {}


def test_every_tool_step_passes_payload_arguments() -> None:
    """EVERY tool-phase step must carry the arguments — "at least one" was the bug.

    The payload table exists so a judge/replay/red-team set can see what the model
    asked a tool to do. A tool-phase step that traces the call but stores no arguments
    makes that set systematically wrong (it under-samples refusals), so the guard is
    all-sites-or-excused rather than any-sites-at-all.
    """
    calls = _tool_step_calls()
    assert len(calls) >= 10, (
        f"only {len(calls)} tool-phase _trace_step sites found in toolrelay.py; 10 were wired on "
        "2026-11-23 — the AST extraction has gone blind (did the phase literal move into a "
        "variable?). A blind guard passes vacuously, which is the failure mode it exists for."
    )
    missing: list[str] = []
    for call in calls:
        if _has_kw(call, "tool_args"):
            continue
        if _site_signature(call) in PAYLOAD_ALLOWLIST:
            continue
        missing.append(f"toolrelay.py:{call.lineno}")
    assert not missing, (
        f"tool-phase trace steps with NO tool_args and no allowlist entry: {missing}. "
        "The payload store would silently lose what the model asked for at those sites "
        "(the 1-of-10 gap A4 closed). Pass tool_args=... — the arguments are in scope at "
        "every site today — or add a PAYLOAD_ALLOWLIST entry with the reason inline."
    )


def test_payload_allowlist_has_no_stale_entries() -> None:
    """An excuse must still match a live site; a stale one is a coverage hole in disguise."""
    live = {_site_signature(call) for call in _tool_step_calls()}
    stale = [signature for signature in PAYLOAD_ALLOWLIST if signature not in live]
    assert not stale, (
        f"PAYLOAD_ALLOWLIST entries that match no tool-phase call site: {stale}. The site was "
        "edited, deleted, or became wired — re-read it and delete/refresh the entry."
    )


def test_refusal_sites_carry_both_halves() -> None:
    """A refusal is, by definition, TEXT handed back to the model — store both halves.

    Every tool-phase site whose status is the literal "denied"/"error" is a refusal path,
    so it must carry the arguments that were refused AND the refusal text the model read.
    That pair is exactly what the red-team set was missing before A4 (it showed what the
    agent did when allowed, never what it asked for when refused).
    """
    refusal_calls: list[ast.Call] = []
    for call in _tool_step_calls():
        status = _kw_value(call, "status")
        if isinstance(status, ast.Constant) and status.value in {"denied", "error"}:
            refusal_calls.append(call)
    # loop-guard denial, unknown tool, invalid-arguments JSON, ACL denial, pin refusal.
    assert len(refusal_calls) >= 5, (
        f"only {len(refusal_calls)} literal refusal sites found; 5 were wired on 2026-11-23 "
        "— the extraction has gone blind."
    )
    for call in refusal_calls:
        assert _has_kw(call, "tool_args") and _has_kw(call, "tool_result"), (
            f"refusal site at toolrelay.py:{call.lineno} is missing a payload half: "
            "a refusal row without the refused arguments/refusal text is the gap A4 closed."
        )


def test_trace_step_signature_accepts_payload_kwargs() -> None:
    """The nested _trace_step helper must accept and forward the two kwargs."""
    tree = ast.parse(RELAY.read_text(encoding="utf-8"))
    defs = [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_trace_step"
    ]
    assert defs, "_trace_step not found"
    argnames = {a.arg for a in defs[0].args.args} | {a.arg for a in defs[0].args.kwonlyargs}
    assert {"tool_args", "tool_result"} <= argnames


# ---------------------------------------------------------------------------
# 2. BEHAVIOURAL — the refusal payloads actually reach the store at runtime.
# ---------------------------------------------------------------------------


class _FakeDB:
    """Stands in for the pool so the trace row "lands" and the payload hook is reached.

    Without this the DB raises, record() returns at its best-effort early exit, and the
    behavioural tests below would pass for the wrong reason (no hook reached = no payload =
    "nothing was called"). That vacuous pass is the failure mode this file exists to
    prevent, so the stub is mandatory, not convenience.
    """

    async def execute(self, *_a: object, **_kw: object) -> None:
        return None


async def _noop(*_a: object, **_kw: object) -> None:
    """Swallow one best-effort side write (ledger settle, prompt capture, chain audit)."""


def _drive_relay(
    monkeypatch: pytest.MonkeyPatch,
    script: list[dict],
    grants: set[tuple[str, str]],
    *,
    exec_result: tuple[bool, Any] = (True, "ok"),
    client_tools: list[dict] | None = None,
    client_loop_guard: Any = None,
    client_tool_execute: Any = None,
) -> tuple[str, list[dict[str, Any]], list[tuple[str, str, dict]]]:
    """Run the REAL relay loop with tracing live; capture the payload store's calls.

    Returns (sse_text, captured, executed). `captured` is one entry per
    trace_payloads.record_step() call — the payload store's OWN entry point, monkeypatched
    so the assertion is on the WIRING (does the relay hand the refusal over?) rather than
    on a database. The DB stub keeps traces.record's best-effort INSERT succeeding, which is
    what makes the payload hook reachable at all; the ledger settle, the prompt capture, the
    mid-loop budget gate and the chained audit write are stubbed for the same reason (no
    Postgres in a unit test).
    """
    captured: list[dict[str, Any]] = []

    async def fake_record_step(
        request_id: str,
        iteration: int,
        server: str,
        tool: str,
        *,
        args: Any = None,
        result: Any = None,
        phase: str = "tool",
    ) -> int:
        captured.append(
            {
                "request_id": request_id,
                "iteration": iteration,
                "server": server,
                "tool": tool,
                "args": args,
                "result": result,
                "phase": phase,
            }
        )
        return 1

    async def fake_gate(*_a: object, **_kw: object) -> GateResult:
        return GateResult(True)

    monkeypatch.setattr(trace_payloads, "record_step", fake_record_step)
    monkeypatch.setattr(traces, "db", _FakeDB(), raising=False)
    monkeypatch.setattr(toolrelay, "settle", _noop)
    monkeypatch.setattr(toolrelay, "check_budgets", fake_gate)
    monkeypatch.setattr(toolrelay.effective, "record", _noop)
    monkeypatch.setattr(chain_audit, "write_mcp_call", _noop)

    cursor = {"n": 0}

    def stream_once(msgs: list[dict], tools: list[dict] | None):
        reply = script[min(cursor["n"], len(script) - 1)]
        cursor["n"] += 1

        async def gen():
            if reply.get("content") is not None:
                yield ("content", reply["content"])
            # ONE assembled list, like the routes-layer parser emits (stream_model_call):
            # the relay assigns tool_calls = payload, so a per-call yield would overwrite
            # earlier calls and make a multi-call batch impossible to script.
            if reply.get("tool_calls"):
                yield ("tool_calls", reply["tool_calls"])
            if "usage" in reply:
                yield ("usage", reply["usage"])

        return gen()

    executed: list[tuple[str, str, dict]] = []

    async def execute(server: str, tool: str, args: dict) -> tuple[bool, Any]:
        executed.append((server, tool, args))
        return exec_result

    async def run() -> list[bytes]:
        return [
            chunk
            async for chunk in toolrelay.relay_stream(
                [{"role": "user", "content": "go"}],
                grants,
                CATALOG,
                stream_once,
                execute,
                "payload-wiring-rid",
                "test-model",
                key_id=uuid.uuid4(),
                client_tools=client_tools,
                client_loop_guard=client_loop_guard,
                client_tool_execute=client_tool_execute,
            )
        ]

    text = b"".join(asyncio.run(run())).decode()
    return text, captured, executed


def _tool_rows(captured: list[dict[str, Any]], tool: str) -> list[dict[str, Any]]:
    return [row for row in captured if row["phase"] == "tool" and row["tool"] == tool]


def test_acl_denial_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline refusal: an ungranted tool's ARGUMENTS must reach the payload store.

    Before A4 this step wrote a trace row and nothing else, so a red-team set built from
    the table could never see what the model tried to do when the ACL said no.
    """
    text, captured, executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "applygate__apply_manifest",
                            "arguments": '{"manifest": "api", "namespace": "prod"}',
                        },
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            {"content": "I could not apply it."},
        ],
        grants={("logsearch", "search_logs")},
    )
    assert executed == [], "the ungranted tool must never execute"
    assert '"detail": "denied by ACL"' in text
    rows = _tool_rows(captured, "apply_manifest")
    assert len(rows) == 1, f"expected exactly one ACL-denial payload pair, got {captured!r}"
    assert rows[0]["args"] == {"manifest": "api", "namespace": "prod"}
    assert "not granted" in str(rows[0]["result"])


def test_malformed_arguments_refusal_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MALFORMED argument text is stored VERBATIM (a parsed {} would hide the defect)."""
    text, captured, _executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "logsearch__search_logs", "arguments": '{"q": "gpu"'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            {"content": "Re-issuing."},
        ],
        grants={("logsearch", "search_logs")},
    )
    assert "not valid JSON" in text
    rows = _tool_rows(captured, "search_logs")
    assert len(rows) == 1, f"expected one invalid-arguments payload pair, got {captured!r}"
    assert rows[0]["args"] == '{"q": "gpu"'
    assert "not valid JSON" in str(rows[0]["result"])


def test_unknown_tool_refusal_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hallucinated tool name is a refusal too — its arguments still reach the store."""
    text, captured, _executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "totally_unknown_tool", "arguments": '{"x": 1}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            {"content": "Sorry."},
        ],
        grants={("logsearch", "search_logs")},
    )
    # The refusal TEXT is fed to the model in-loop (a role=tool row), not streamed as a
    # client event — the client sees the pcai tool_error event instead.
    assert '"detail": "not an MCP tool name"' in text
    rows = _tool_rows(captured, "totally_unknown_tool")
    assert len(rows) == 1, f"expected one unknown-tool payload pair, got {captured!r}"
    assert rows[0]["args"] == '{"x": 1}'
    assert "unknown tool" in str(rows[0]["result"])


def test_client_loop_guard_denial_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client loop guard's refusal carries the PARSED call plus the guard's reason."""

    def guard(name: str, args: dict) -> str | None:
        return f"loop detected: {name} repeated with identical arguments"

    _text, captured, _executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            {"content": "Stopping."},
        ],
        grants=set(),
        client_tools=[{"type": "function", "function": {"name": "bash", "description": "run"}}],
        client_loop_guard=guard,
    )
    rows = _tool_rows(captured, "bash")
    assert len(rows) == 1, f"expected one loop-guard payload pair, got {captured!r}"
    assert rows[0]["server"] == "client"
    assert rows[0]["args"] == {"command": "ls"}
    assert "loop detected" in str(rows[0]["result"])


def test_client_handback_payload_reaches_the_store_without_a_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handback stores the arguments and NO result — the client has not run it yet.

    Inventing a placeholder result here would be exactly the lie the payload table must not
    tell, so `result is None` is part of the contract being asserted.
    """
    text, captured, executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls -la"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        ],
        grants=set(),
        client_tools=[{"type": "function", "function": {"name": "bash", "description": "run"}}],
    )
    assert executed == [], "a handback is executed by the CLIENT, not in-cluster"
    assert '"event": "handback"' in text
    rows = _tool_rows(captured, "bash")
    assert len(rows) == 1, f"expected one handback payload entry, got {captured!r}"
    assert rows[0]["server"] == "client"
    assert rows[0]["args"] == '{"command": "ls -la"}'
    assert rows[0]["result"] is None


def test_executed_mcp_tool_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ORIGINAL fed site still carries both halves after the A4 rewiring."""
    _text, captured, executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "logsearch__search_logs", "arguments": '{"q": "gpu"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            {"content": "Found three hits."},
        ],
        grants={("logsearch", "search_logs")},
        exec_result=(True, "hit-1, hit-2, hit-3"),
    )
    assert executed == [("logsearch", "search_logs", {"q": "gpu"})]
    rows = _tool_rows(captured, "search_logs")
    assert len(rows) == 1, f"expected one executed-tool payload pair, got {captured!r}"
    assert rows[0]["args"] == {"q": "gpu"}
    assert rows[0]["result"] == "hit-1, hit-2, hit-3"


def test_pin_refusal_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch-time pin gate: a changed definition refuses the call — and the call is stored."""
    from pcai_llm.core import tool_pin

    verdicts = {("logsearch", "search_logs"): tool_pin.PinVerdict(tool_pin.CHANGED, "digest-old", "digest-new")}
    monkeypatch.setattr(toolrelay, "pin_catalog_with_verdicts", lambda catalog, grants=None: (catalog, verdicts))
    monkeypatch.setattr(toolrelay.settings, "tool_pin_mode", "block")
    text, captured, executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "logsearch__search_logs", "arguments": '{"q": "gpu"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            {"content": "The definition changed; not retrying."},
        ],
        grants={("logsearch", "search_logs")},
    )
    assert executed == [], "a pin-refused call must never reach the executor"
    assert '"detail": "definition changed since approval"' in text
    rows = _tool_rows(captured, "search_logs")
    assert len(rows) == 1, f"expected one pin-refusal payload pair, got {captured!r}"
    assert rows[0]["args"] == {"q": "gpu"}
    assert "definition changed" in str(rows[0]["result"])


def test_client_dispatch_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave B: a client__<tool> call executed over the reverse channel stores args + result."""
    dispatched: list[tuple[str, str, dict]] = []

    async def dispatch(server: str, tool: str, args: dict) -> tuple[bool, Any]:
        dispatched.append((server, tool, args))
        return True, "total 4"

    _text, captured, executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "client__bash", "arguments": '{"command": "ls"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            {"content": "Done."},
        ],
        grants=set(),
        client_tool_execute=dispatch,
    )
    assert executed == [], "a client__ call must not run through the in-cluster executor"
    assert dispatched == [("client", "bash", {"command": "ls"})]
    rows = _tool_rows(captured, "bash")
    assert len(rows) == 1, f"expected one client-dispatch payload pair, got {captured!r}"
    assert rows[0]["server"] == "client"
    assert rows[0]["args"] == {"command": "ls"}
    assert rows[0]["result"] == "total 4"


def test_retrieve_refusal_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """pcai__retrieve: the digest the model asked for + the unknown-digest refusal."""
    from pcai_llm.core import toolcompress_store

    async def no_stored_result(*_a: object, **_kw: object) -> None:
        return None

    monkeypatch.setattr(toolcompress_store, "load", no_stored_result)
    _text, captured, _executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "pcai__retrieve", "arguments": '{"digest": "deadbeefcafe"}'},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
            {"content": "Re-running the original call."},
        ],
        grants=set(),
    )
    rows = _tool_rows(captured, "retrieve")
    assert len(rows) == 1, f"expected one retrieve payload pair, got {captured!r}"
    assert rows[0]["args"] == {"digest": "deadbeefcafe"}
    assert "no stored result for digest" in str(rows[0]["result"])


def test_mixed_batch_handback_payload_reaches_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mixed batch: the cloud call executes in-cluster, the client call is handed back WITH args."""
    text, captured, executed = _drive_relay(
        monkeypatch,
        [
            {
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    },
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "logsearch__search_logs", "arguments": '{"q": "gpu"}'},
                    },
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            }
        ],
        grants={("logsearch", "search_logs")},
        client_tools=[{"type": "function", "function": {"name": "bash", "description": "run"}}],
    )
    assert executed == [("logsearch", "search_logs", {"q": "gpu"})], "the cloud half runs in-cluster"
    assert '"event": "handback"' in text
    rows = _tool_rows(captured, "bash")
    assert len(rows) == 1, f"expected one mixed-batch handback payload entry, got {captured!r}"
    assert rows[0]["server"] == "client"
    assert rows[0]["args"] == '{"command": "ls"}'
    assert rows[0]["result"] is None


# ---------------------------------------------------------------------------
# 3. The storage half: traces.record forwards the payload, sampling-off is free.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_forwards_payload_to_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """traces.record must hand the payload to trace_payloads for a tool phase."""
    seen: list[tuple[object, object]] = []

    async def fake_record_step(*_a: object, **kw: object) -> int:
        seen.append((kw.get("args"), kw.get("result")))
        return 1

    monkeypatch.setattr(traces, "db", _FakeDB(), raising=False)
    monkeypatch.setattr(trace_payloads, "record_step", fake_record_step)

    await traces.record(
        request_id="wiring-test-1",
        key_id=uuid.uuid4(),
        user_id=None,
        model="test-model",
        iteration=1,
        phase="tool",
        server="k8s-ops",
        tool="list_pods",
        status="ok",
        tool_args={"namespace": "default"},
        tool_result="pod-a Running",
    )
    assert seen, "traces.record did not forward the payload to trace_payloads.record_step"
    assert seen[0][0] == {"namespace": "default"}
    assert seen[0][1] == "pod-a Running"


@pytest.mark.asyncio
async def test_sampling_off_costs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The DEFAULT deployment must pay nothing: no DB write when sampling is 0.

    The gate lives inside trace_payloads.record_step (not at the call site), so this
    exercises the REAL function with the real sampling logic and counts DB traffic.
    """
    from pcai_llm.config import Settings

    db_calls: list[tuple[object, ...]] = []

    class _CountingDB:
        async def execute(self, *a: object, **_kw: object) -> None:
            db_calls.append(a)

    monkeypatch.setattr(trace_payloads, "db", _CountingDB(), raising=False)
    monkeypatch.setattr(trace_payloads, "settings", Settings(trace_payload_sampling=0.0), raising=False)

    written = await trace_payloads.record_step(
        "wiring-test-2", 1, "k8s-ops", "list_pods", args={"namespace": "default"}, result="pod-a Running"
    )
    assert written == 0, "record_step wrote rows while sampling was disabled"
    assert not db_calls, "payload store hit the DB with sampling disabled (default deployment pays cost)"


def test_sampling_is_per_request_and_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same request_id must decide the same way every time (replica/restart safe).

    A per-ROW sampler would capture a random subset of one turn, which no judge can
    read; a non-deterministic one would store a different subset on each replica.
    """
    decisions = [trace_payloads.sampled("stable-request-id", 0.5) for _ in range(50)]
    assert len(set(decisions)) == 1, "sampling decision is not deterministic for one request_id"
    # and the rate is monotone: a request captured at r stays captured at r' > r
    for rid in (f"req-{i}" for i in range(200)):
        if trace_payloads.sampled(rid, 0.3):
            assert trace_payloads.sampled(rid, 0.9)


# ---------------------------------------------------------------------------
# The SAME guard for the eval half (added 2026-11-23 after cross-review P1).
# ---------------------------------------------------------------------------
# `agent_eval` shipped with a config flag, a DDL, unit tests and NO call site: flipping
# agent_eval_enabled wrote nothing, forever. The payload half had this guard from the
# start; the eval half did not, and that asymmetry is exactly how the no-op survived a
# green suite. Any future refactor that drops the call site fails here.

PROXY = SRC / "pcai_llm" / "api" / "routes_proxy.py"


def test_agent_eval_is_called_from_the_request_path() -> None:
    """agent_eval.evaluate_turn must be invoked by the proxy, not merely importable."""
    tree = ast.parse(PROXY.read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "evaluate_turn"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "agent_eval"
    ]
    assert calls, (
        "agent_eval.evaluate_turn has no call site in routes_proxy.py: the scorer would "
        "be dead code and agent_eval_enabled=true would write zero verdict rows"
    )


def test_agent_eval_call_is_guarded_by_the_flag() -> None:
    """The hook must be gated on settings.agent_eval_enabled, not run unconditionally."""
    src = PROXY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if "agent_eval_enabled" not in test_src:
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "evaluate_turn":
                guarded = True
    assert guarded, "the evaluate_turn call is not guarded by settings.agent_eval_enabled"
