"""Wiring guard: the payload store must actually be FED at runtime.

WHY THIS FILE EXISTS (2026-11-23). The quality layer was built in two halves by two
agents: a storage half (traces.record / trace_payloads) and a call site in the relay
loop. Both halves can be green while the feature is dead — the storage is unit-tested,
the call site exists, and nothing passes the arguments between them. That failure is
invisible to every unit test and silent in production, so it gets a structural test.

Two things are asserted:
  1. the executed-tool trace step in core/toolrelay.py passes tool_args AND tool_result
     (a future refactor that drops one of them fails here, not in a week);
  2. traces.record forwards them into the payload store, and pays nothing when sampling
     is off (the default deployment must not regress).
"""

from __future__ import annotations

import ast
import sys
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from pcai_llm.core import trace_payloads, traces  # noqa: E402

RELAY = SRC / "pcai_llm" / "core" / "toolrelay.py"


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


def test_executed_tool_step_passes_payload_arguments() -> None:
    """At least one tool step must carry the arguments and the result."""
    calls = _tool_step_calls()
    assert calls, "no _trace_step(<iter>, 'tool', ...) call found in toolrelay.py"

    def kw(call: ast.Call, name: str) -> bool:
        return any(k.arg == name for k in call.keywords)

    fed = [c for c in calls if kw(c, "tool_args") and kw(c, "tool_result")]
    assert fed, (
        "no executed-tool trace step passes tool_args+tool_result: the payload store is "
        "built but never fed (agent_trace_payloads would stay empty forever)"
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


class _FakeDB:
    """Stands in for the pool so the trace row 'lands' and the hook is reached.

    Without this the DB raises, record() returns at its best-effort early exit, and
    BOTH of the tests below would pass for the wrong reason (no hook reached = no
    payload = 'nothing was called'). That vacuous pass is the failure mode this
    file exists to prevent, so the stub is mandatory, not convenience.
    """

    async def execute(self, *_a: object, **_kw: object) -> None:
        return None


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
