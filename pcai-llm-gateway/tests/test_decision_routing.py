"""Decision-model routing: the LLM-as-router seam.

WHY THIS FILE EXISTS (2026-10-23): the operator deployed a vLLM-SR decision
model (d3-turbo) and asked for it to drive the gateway complexity router.
The design constraints that shape every test here:

  * a decision model NEVER generates text -- it answers typed questions with
    calibrated probabilities (POST /v1/systemone), so it cannot be wired
    through the chat path;
  * it measured ~790ms per decision on the live deployment, so it must not sit
    unguarded on the request path;
  * its answer is a PROBABILITY, and a real 3-way model choice came back at
    confidence 0.43 -- serving that in preference to a deterministic heuristic
    would make answers worse, which violates model_routing's "never worse than
    what the client asked for" contract.

The contract these tests pin:
  1. every failure mode (transport, timeout, garbage payload, an option the
     ladder never offered, confidence below the floor) returns ok=False and
     the HEURISTICS serve -- a decision model can never fail a request;
  2. a decision can only pick inside the same floor..ceiling window the
     heuristics walk, so it can never escape the ladder or the key ACL;
  3. decision_mode "off" (the default) leaves the heuristic path
     byte-identical, even with a decision model configured.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pcai_llm.core import decision_routing as dr
from pcai_llm.core import model_routing as mr


def _answer(choice: str, confidence: float, probs: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "answers": {
            "answer": {
                "type": "choice",
                "choice": choice,
                "probabilities": probs or {choice: confidence},
                "confidence": confidence,
            }
        }
    }


# ---------------------------------------------------------------------------
# parse_choice -- total by construction
# ---------------------------------------------------------------------------


def test_parse_accepts_a_confident_choice() -> None:
    r = dr.parse_choice(_answer("b", 0.99), ["a", "b"], min_confidence=0.6)
    assert r.ok and r.choice == "b" and r.confidence == pytest.approx(0.99)


def test_parse_refuses_a_choice_the_ladder_never_offered() -> None:
    """A hallucinated option must never be served: the model was given a
    closed set and picked outside it."""
    r = dr.parse_choice(_answer("gpt-9", 0.99), ["a", "b"], min_confidence=0.6)
    assert not r.ok and r.reason == "choice_not_offered"


def test_parse_abstains_below_the_confidence_floor() -> None:
    """The measured shape: a real 3-way model choice returned 0.43. Serving it
    over the heuristic would be a quality regression."""
    r = dr.parse_choice(_answer("b", 0.43), ["a", "b"], min_confidence=0.6)
    assert not r.ok and r.reason == "below_confidence"
    assert r.choice == "b"  # the preference is still recorded for the ledger


@pytest.mark.parametrize(
    "payload,reason",
    [
        ("nope", "bad_payload"),
        ({}, "no_answers"),
        ({"answers": {}}, "no_answer"),
        ({"answers": {"answer": {"type": "choice"}}}, "no_choice"),
        ({"answers": {"answer": {"choice": ""}}}, "no_choice"),
    ],
)
def test_parse_is_total_on_malformed_payloads(payload: Any, reason: str) -> None:
    """Every shape a broken/older endpoint could return degrades to ok=False
    rather than raising: this runs on the request path."""
    r = dr.parse_choice(payload, ["a", "b"], min_confidence=0.6)
    assert not r.ok and r.reason == reason


def test_parse_tolerates_a_junk_probabilities_map() -> None:
    payload = {"answers": {"answer": {"choice": "a", "confidence": 0.9, "probabilities": {"a": "x"}}}}
    r = dr.parse_choice(payload, ["a"], min_confidence=0.6)
    assert r.ok and r.probabilities == {}


# ---------------------------------------------------------------------------
# build_state -- compact, deterministic, bounded
# ---------------------------------------------------------------------------


def test_state_is_a_summary_not_the_conversation() -> None:
    """The endpoint validated_input_tokens is 32768 and the decision costs
    ~790ms; shipping the prompt would blow both. The state carries STRUCTURE."""
    body = {"messages": [{"role": "user", "content": "x" * 100000}], "tools": [{}] * 3, "max_tokens": 4096}
    state = dr.build_state(body, complexity=25000, candidates=["a", "b"])
    assert len(state) < dr.MAX_STATE_CHARS
    assert "x" * 1000 not in state  # the prompt text itself is NOT included
    assert "estimated_prompt_tokens" in state and "tool_count" in state


def test_state_carries_gpu_and_cost_per_candidate() -> None:
    state = dr.build_state(
        {"messages": []},
        complexity=100,
        candidates=["a", "b"],
        gpu={"models": {"a": {"value_pct": 91.5, "active": False}}},
        costs={"a": {"input_per_m": 0.15}},
    )
    assert "91.5%" in state and "low_demand" in state
    assert "0.15" in state


def test_state_never_exceeds_the_budget_even_with_many_candidates() -> None:
    state = dr.build_state(
        {"messages": [], "tools": [{"description": "d" * 200}] * 200},
        complexity=10,
        candidates=[f"m{i}" for i in range(200)],
    )
    assert len(state) <= dr.MAX_STATE_CHARS


# ---------------------------------------------------------------------------
# decide -- the fail-safe wrapper
# ---------------------------------------------------------------------------


def test_decide_returns_ok_on_a_confident_answer() -> None:
    async def caller(state, instructions, candidates, timeout_ms):
        return _answer("a", 0.95)

    r = asyncio.run(dr.decide(caller, state="s", instructions="i", candidates=["a", "b"], min_confidence=0.6))
    assert r.ok and r.choice == "a"


def test_decide_never_raises_on_a_transport_failure() -> None:
    async def caller(state, instructions, candidates, timeout_ms):
        raise ConnectionError("upstream down")

    r = asyncio.run(dr.decide(caller, state="s", instructions="i", candidates=["a", "b"], min_confidence=0.6))
    assert not r.ok and r.reason == "call_failed"


def test_decide_reports_a_timeout_distinctly() -> None:
    async def caller(state, instructions, candidates, timeout_ms):
        raise TimeoutError()

    r = asyncio.run(dr.decide(caller, state="s", instructions="i", candidates=["a", "b"], min_confidence=0.6))
    assert not r.ok and r.reason == "timeout"


def test_decide_with_no_candidates_does_not_call_out() -> None:
    called = False

    async def caller(state, instructions, candidates, timeout_ms):
        nonlocal called
        called = True
        return _answer("a", 1.0)

    r = asyncio.run(dr.decide(caller, state="s", instructions="i", candidates=[], min_confidence=0.6))
    assert not r.ok and r.reason == "no_candidates" and not called


def test_decide_clamps_the_timeout_into_range() -> None:
    seen: list[int] = []

    async def caller(state, instructions, candidates, timeout_ms):
        seen.append(timeout_ms)
        return _answer("a", 0.9)

    asyncio.run(dr.decide(caller, state="s", instructions="i", candidates=["a", "b"], timeout_ms=10**9))
    assert seen == [dr.MAX_TIMEOUT_MS]


# ---------------------------------------------------------------------------
# model_routing integration -- the decision can never escape the ladder
# ---------------------------------------------------------------------------


class _Key:
    user_id = "00000000-0000-0000-0000-000000000001"


def _resolve(decide, order, *, min_model=""):
    """Run the real resolver with an injected decision seam."""

    async def _go():
        return await mr.resolve_routed_model(
            _Key(),
            {"messages": [{"role": "user", "content": "hi"}]},
            "routing__" + order[0],
            set(order),
            [],
            order,
            available=None,
            decide=decide,
        )

    return asyncio.run(_go())


def test_no_decide_callable_leaves_the_heuristic_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default posture: decide=None must be byte-identical to today."""
    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy("active"))
    monkeypatch.setattr(mr.settings, "routing_enabled", True)
    _model, route = _resolve(None, ["big", "small"])
    assert route.policy == "active" and route.reason != ""


def _fake_policy(policy: str, min_model: str = ""):
    async def _inner(user_id):
        return policy, min_model

    return _inner


def test_a_decision_inside_the_window_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy("active"))
    monkeypatch.setattr(mr.settings, "routing_enabled", True)

    async def decide(candidates, body, score):
        return mr.DecisionOutcome(ok=True, choice="small", confidence=0.9, reason="decision")

    model, route = _resolve(decide, ["big", "small"])
    assert model == "small"
    assert route.served == "small" and route.reason.startswith("decision:")


def test_an_abstaining_decision_leaves_the_heuristic_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy("active"))
    monkeypatch.setattr(mr.settings, "routing_enabled", True)

    async def decide(candidates, body, score):
        return mr.DecisionOutcome(ok=False, choice="small", confidence=0.4, reason="below_confidence")

    _model, route = _resolve(decide, ["big", "small"])
    assert not route.reason.startswith("decision:")


def test_a_decision_naming_a_model_outside_the_window_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ACL/ceiling guarantee: the decision model is offered a CLOSED set
    and an answer outside it must be discarded, not served."""
    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy("active", min_model="small"))
    monkeypatch.setattr(mr.settings, "routing_enabled", True)

    async def decide(candidates, body, score):
        assert "outside" not in candidates
        return mr.DecisionOutcome(ok=True, choice="outside", confidence=1.0, reason="decision")

    _model, route = _resolve(decide, ["big", "small"])
    assert not route.reason.startswith("decision:")


def test_a_raising_decide_seam_cannot_fail_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy("active"))
    monkeypatch.setattr(mr.settings, "routing_enabled", True)

    async def decide(candidates, body, score):
        raise RuntimeError("seam exploded")

    _model, route = _resolve(decide, ["big", "small"])
    assert route.policy == "active"  # still served, heuristics stand
