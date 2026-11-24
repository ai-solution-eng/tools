"""Structural guards for the two Lead integration seams (wave 2, 2026-11-23).

WHY THIS FILE EXISTS. Two one-line integrations are the operational point of their
features, and neither has a behavioural test: drop either line and the whole suite
stays green while the documented behaviour silently reverts.

  1. app.py's lifespan awaits tool_pin's warm load. Without it the deployed gateway
     pays the pin load on its FIRST relay request instead of at boot — and, worse,
     the docstring's promise ("pins survive a restart") is what an operator relies
     on when they flip tool_pin_mode to block.
  2. toolrelay.py passes settings.playbook_stale_after_days into
     recall_playbook_block. It is the ONLY path by which the operator's staleness
     limit reaches the recall, and the module default (None) means NEVER EXPIRE —
     so a dropped kwarg turns a documented 90-day trust floor into "trust forever",
     silently.

These are AST guards, not behaviour tests, for the same reason the payload/eval
wiring guards are: the failure mode is the line disappearing, not the line being
wrong. Verified in wave 1 that this pattern catches exactly that.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

APP = SRC / "pcai_llm" / "app.py"
RELAY = SRC / "pcai_llm" / "core" / "toolrelay.py"


def test_lifespan_awaits_the_pin_warm_load() -> None:
    """app.py must await the pin store's warm load, or pins load on first request."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "awarm"
    ]
    assert calls, (
        "app.py no longer awaits _PIN_STORE.awarm(): the pin store would load on the "
        "first relay request instead of at boot, and the restart-survival promise "
        "would rest on that first request happening"
    )


def test_pin_warm_load_is_wrapped_so_it_cannot_block_boot() -> None:
    """The warm load is best-effort: it must sit inside a try, not at module scope."""
    src = APP.read_text(encoding="utf-8")
    tree = ast.parse(src)
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if any(
            isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "awarm"
            for sub in ast.walk(node)
        ):
            guarded = True
    assert guarded, "the awarm() call is not inside a try: a pin-load fault could block startup"


def test_relay_passes_the_staleness_limit_to_recall() -> None:
    """toolrelay must pass stale_after_days, or the 90-day floor silently means 'never'."""
    tree = ast.parse(RELAY.read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "recall_playbook_block"
    ]
    assert calls, "recall_playbook_block is not called from toolrelay.py"
    assert any(kw.arg == "stale_after_days" for c in calls for kw in c.keywords), (
        "recall_playbook_block is called WITHOUT stale_after_days: playbook_stale_after_days "
        "would be declared-but-never-read and every stale procedure would be injected"
    )


def test_counts_are_not_redacted_as_credentials() -> None:
    """A config dump's 'token: 512' must survive redaction (cross-review, 2026-11-23).

    The digit exception used to exist only in the wrapped-continuation pass, so the
    common one-line shape mangled the very diagnostics the payload table exists for.
    """
    from pcai_llm.core.trace_payloads import redact

    for text in ("token: 512", "tokens: 4096", "api_key: 4096"):
        assert "[REDACTED" not in redact(text), f"a COUNT was redacted as a credential: {text!r}"
    # ...but a real credential of the same shape still goes.
    assert "[REDACTED:secret]" in redact("token: abc123")
    # ...and a numeric PASSWORD stays redacted: a numeric password is real.
    assert "[REDACTED:password]" in redact("password: 512")


# ---------------------------------------------------------------------------
# B8: the approval gate must cover the RELAY, not just /mcp.
# ---------------------------------------------------------------------------
# Phase 1 originally gated only _gated_mcp_call. The relay is the path an AGENT
# uses, so gating /mcp alone left the model-driven path ungated and the observe
# data incomplete — a scoping error the phase-1 report surfaced. These guards make
# a future refactor that drops the relay gate fail here rather than silently.

PROXY = SRC / "pcai_llm" / "api" / "routes_proxy.py"


def test_relay_executor_calls_the_approval_gate() -> None:
    """The relay's execute() must consult approval.gate_call, or agents bypass it."""
    tree = ast.parse(PROXY.read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "gate_call"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "approval"
    ]
    assert calls, (
        "routes_proxy does not call approval.gate_call: the RELAY path would be "
        "ungated, so a destructive tool invoked by an agent would bypass the gate"
    )


def test_approval_gate_precedes_credential_build_in_the_relay() -> None:
    """A refusal must happen before credentials are built or a session opens."""
    src = PROXY.read_text(encoding="utf-8")
    gate = src.index("approval.gate_call(")
    headers = src.index("resolve_mcp_headers(target")
    assert gate < headers, (
        "approval.gate_call appears AFTER resolve_mcp_headers in routes_proxy: a refused "
        "call would build credentials for the server it refuses to reach"
    )


# ---------------------------------------------------------------------------
# The relay approval-refusal path must use attributes that EXIST on GateDecision.
# ---------------------------------------------------------------------------
# 2026-11-23: the relay wiring returned `approval_decision.message` where the
# dataclass has `.reason`. Dormant in off/warn (refused is never True there) and an
# AttributeError the moment enforce is switched on — on the path an agent uses.
# ruff cannot see it and no test covered that branch; mypy caught it, which is why
# automation.sh now runs mypy. This guard pins the specific attribute contract so a
# future copy-paste from arg_policy's `.message` fails here instead of in enforce.


def test_relay_approval_refusal_uses_real_gate_decision_attributes() -> None:
    from pcai_llm.core.approval import GateDecision

    tree = ast.parse(PROXY.read_text(encoding="utf-8"))
    attrs = {
        n.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "approval_decision"
    }
    assert attrs, "routes_proxy never inspects an approval_decision — is the relay gated?"
    real = set(vars(GateDecision()).keys())
    unknown = attrs - real
    assert not unknown, (
        f"routes_proxy reads approval_decision.{sorted(unknown)} but GateDecision has "
        f"only {sorted(real)} — an AttributeError waiting for enforce mode"
    )
