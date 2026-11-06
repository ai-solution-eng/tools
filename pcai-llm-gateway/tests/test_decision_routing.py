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
import json
from typing import Any, ClassVar

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


def test_state_is_bounded_not_the_whole_conversation() -> None:
    """The endpoint validated_input_tokens is 32768 and the decision costs
    ~790ms; shipping a 100k-char prompt would blow both. The state carries the
    user's LAST turn, capped -- never the full conversation."""
    body = {"messages": [{"role": "user", "content": "x" * 100000}], "tools": [{}] * 3, "max_tokens": 4096}
    state = dr.build_state(body, complexity=25000, candidates=["a", "b"])
    assert len(state) <= dr.MAX_STATE_CHARS
    assert "x" * (dr.MAX_USER_TEXT_CHARS + 10) not in state  # capped
    assert "estimated_prompt_tokens" in state and "tool_count" in state


def test_state_carries_the_user_text() -> None:
    """The user's words are the ONLY per-request signal that carries intent.

    Tools / system prompt / depth are constant for an agent harness. Measured
    2026-10-26: nine queries from a greeting to a Raft implementation all scored
    713-722 and the decision model returned an identical 0.34 for every one.
    """
    state = json.loads(
        dr.build_state(
            {"messages": [{"role": "user", "content": "review this diff for race conditions"}]},
            complexity=700,
            candidates=["a", "b"],
        )
    )
    assert state["request"]["user_message"] == "review this diff for race conditions"


def test_state_uses_the_last_user_turn() -> None:
    """In an agent loop the newest user turn is the task in flight."""
    state = json.loads(
        dr.build_state(
            {
                "messages": [
                    {"role": "user", "content": "first question"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "now do the hard thing"},
                ]
            },
            complexity=700,
            candidates=["a"],
        )
    )
    assert state["request"]["user_message"] == "now do the hard thing"


def test_state_flags_images_and_modalities() -> None:
    """An image request must be VISIBLE to the router, and a VLM candidate must
    be distinguishable from a text-only one -- otherwise a vision request gets
    routed to a text-only LLM."""
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is in this chart?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ]
    }
    state = json.loads(
        dr.build_state(
            body,
            complexity=900,
            candidates=["textonly", "vlm"],
            modalities={"textonly": ["text"], "vlm": ["text", "image"]},
        )
    )
    assert state["request"]["has_image"] is True
    assert state["request"]["image_count"] == 1
    assert state["request"]["user_message"] == "what is in this chart?"
    by_name = {c["model"]: c for c in state["candidates"]}
    assert by_name["textonly"]["modalities"] == ["text"]
    assert "image" in by_name["vlm"]["modalities"]


def test_state_omits_modalities_when_undeclared() -> None:
    """Unknown capability must make NO claim -- not claim text-only, which
    would silently exclude a model that could actually serve."""
    state = json.loads(dr.build_state({"messages": []}, complexity=10, candidates=["a"]))
    assert "modalities" not in state["candidates"][0]


def test_state_text_free_body_has_no_user_message_key() -> None:
    state = json.loads(
        dr.build_state({"messages": [{"role": "system", "content": "s"}]}, complexity=10, candidates=["a"])
    )
    assert "user_message" not in state["request"]


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


def _resolve(decide, order, *, min_model="", requested=None, registered=None):
    """Run the real resolver with an injected decision seam.

    requested / registered default to the historical shape (the ladder head is
    the named ceiling); an OFF-ladder ceiling is what audit w6's floor pin
    needs, so both are overridable.
    """
    target = requested or ("routing__" + order[0])
    regs = set(order) if registered is None else set(registered)

    async def _go():
        return await mr.resolve_routed_model(
            _Key(),
            {"messages": [{"role": "user", "content": "hi"}]},
            target,
            regs,
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


def test_strip_harness_context_removes_system_reminder_blocks() -> None:
    """A <system-reminder> block must not reach the router as "the user turn".

    Measured live 2026-10-28: the decision state carried
    "<system-reminder>\nA skill is a reusable set of task-speci..." and the
    model answered at confidence 0.10-0.29; the same question with clean text
    scored 0.74. Everything below the 0.60 floor is discarded, so the block
    was silently handing every decision to the size heuristic.
    """
    from pcai_llm.core.decision_routing import strip_harness_context

    dirty = "<system-reminder>\nA skill is a reusable set.\n</system-reminder>\nTesting. 1,2,3."
    assert strip_harness_context(dirty) == "Testing. 1,2,3."
    # Entirely plumbing -> empty, so the caller can tell "no user words this
    # turn" from an empty message.
    assert strip_harness_context("<system-reminder>only plumbing</system-reminder>") == ""
    # Case-insensitive and attribute-bearing openers are the same block.
    assert strip_harness_context('<SYSTEM-REMINDER type="x">noise</SYSTEM-REMINDER>hi') == "hi"


# ---------------------------------------------------------------------------
# R1 (audit 2026-10-29): NaN/inf decision confidence bypassed the abstain
# floor. Only `confidence < min_confidence` was tested, and float("nan") <
# x is False for every x -- so a misbehaving endpoint returning bare NaN
# (json.loads parses it) or +inf sailed past the operator's ONLY distrust
# lever. Post-fix: a non-finite confidence is not a strong answer, it is NO
# answer -- abstain with reason "nonfinite_confidence".
# ---------------------------------------------------------------------------


def test_r1_nan_confidence_abstains() -> None:
    """The audit's exact repro: confidence "NaN" served an unsupervised pick."""
    payload = _answer("b", float("nan"))
    r = dr.parse_choice(payload, ["a", "b"], min_confidence=0.6)
    assert not r.ok, "NaN confidence must not pass the floor"
    assert r.reason == "nonfinite_confidence"


def test_r1_inf_confidence_abstains() -> None:
    payload = _answer("b", float("inf"))
    r = dr.parse_choice(payload, ["a", "b"], min_confidence=0.6)
    assert not r.ok and r.reason == "nonfinite_confidence"


def test_r1_bare_json_nan_literal_abstains() -> None:
    """json.loads accepts BARE NaN/Infinity -- no exotic encoding needed."""
    raw = json.loads('{"answers":{"answer":{"choice":"b","confidence":NaN,"probabilities":{"b":1.0}}}}')
    r = dr.parse_choice(raw, ["a", "b"], min_confidence=0.6)
    assert not r.ok and r.reason == "nonfinite_confidence"


def test_r1_junk_string_confidence_still_abstains() -> None:
    """The pre-existing junk path (float("abc") -> 0.0) is unchanged."""
    raw = {"answers": {"answer": {"choice": "b", "confidence": "not-a-number"}}}
    r = dr.parse_choice(raw, ["a", "b"], min_confidence=0.6)
    assert not r.ok


def test_r1_finite_confidence_still_served() -> None:
    """The fix must not over-reject: a real answer still lands."""
    r = dr.parse_choice(_answer("b", 0.99), ["a", "b"], min_confidence=0.6)
    assert r.ok and r.choice == "b"


def test_r1_negative_inf_abstains() -> None:
    r = dr.parse_choice(_answer("b", float("-inf")), ["a", "b"], min_confidence=0.6)
    assert not r.ok and r.reason == "nonfinite_confidence"


# ---------------------------------------------------------------------------
# the WIRE contract: cards -> criteria (2026-10-30)
# ---------------------------------------------------------------------------
# The decision model is told what each candidate is FOR through the criteria
# channel -- the option description it scores against. Nothing pinned that the
# configured cards actually reach the wire, so a save could have looked fine in
# the panel while every call shipped bare labels.


class _FakeResponse:
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.content = b""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(_answer("b", 0.99))


def _caller_env(
    monkeypatch: pytest.MonkeyPatch,
    cards: dict[str, Any],
    modalities: dict[str, Any] | None = None,
    prompt: str = "",
):
    from pcai_llm.api import routes_proxy
    from pcai_llm.core import routing_config

    client = _FakeClient()

    class _Ep:
        base_url = "http://decision.test/v1"
        api_key_secret = None
        auth_token_enc = None

    class _Pool:
        # ClassVar: a shared fixture, not per-instance state (ruff RUF012).
        endpoints: ClassVar[list[Any]] = [_Ep()]

        def pick(self) -> Any:
            return self.endpoints[0]

        async def report(self, ep: Any, ok: bool, error: str = "") -> None:
            """The real Pool's breaker seam (audit w6 wired the decision call
            into it); this stub only has to satisfy the interface."""

    monkeypatch.setattr(routes_proxy.model_router, "pool", lambda name: _Pool())
    # Router.client is a read-only property (it asserts the router started), so
    # it is replaced on the CLASS, not on the instance.
    monkeypatch.setattr(type(routes_proxy.model_router), "client", property(lambda self: client))
    monkeypatch.setattr(routes_proxy, "_upstream_headers", lambda ep, rid: {})

    async def _cfg() -> dict[str, Any]:
        return {"inject_gpu_load": True}

    async def _gpu() -> dict[str, Any]:
        return {
            "available": True,
            "inject": True,
            "models": {
                "a": {"value_pct": 1.5, "active": True},
                "b": {"value_pct": 88.0, "active": False},
            },
        }

    async def _prices(models: list[str]) -> dict[str, str]:
        return {"b": "in 0.1 / out 0.2 USD per 1M tokens"}

    monkeypatch.setattr(routing_config, "get_config", _cfg)
    monkeypatch.setattr(routing_config, "gpu_load_snapshot", _gpu)
    from pcai_llm.core import costs

    monkeypatch.setattr(costs, "price_hints", _prices)

    policy = {"cards": cards, "modalities": modalities or {}, "prompt": prompt}
    caller = routes_proxy._decision_caller("decision-model", policy)
    assert caller is not None
    return caller, client


def test_criteria_on_the_wire_come_from_the_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = {
        "a": {"description": "light work, load {{gpu_util}} ({{low_demand}})"},
        "b": {"description": "hard work, cost {{cost}}"},
    }
    caller, client = _caller_env(monkeypatch, cards, prompt="policy")
    payload = asyncio.run(caller("{}", "policy", ["a", "b"], 2500, []))
    sent = client.calls[0]["json"]
    assert payload["answers"]["answer"]["choice"] == "b"
    # each option carries ITS OWN live row, not the fleet's. A card that writes
    # a token itself is not given the appended duplicate; the facts it did NOT
    # write are appended.
    assert sent["questions"]["answer"]["criteria"] == {
        "a": "light work, load 1.5% (low demand)",
        "b": "hard work, cost in 0.1 / out 0.2 USD per 1M tokens | gpu 88.0% (high demand)",
    }
    # the operator prompt rides as instructions, unrendered (it has no placeholders)
    assert sent["questions"]["answer"]["instructions"] == "policy"


def test_instructions_default_to_the_shipped_policy_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty operator prompt falls back to the SHIPPED text, which is the
    model-agnostic policy -- the roles are in the criteria, never in here."""
    caller, client = _caller_env(monkeypatch, {})
    asyncio.run(caller("{}", "", ["a", "b"], 2500, []))
    sent = client.calls[0]["json"]
    assert sent["questions"]["answer"]["instructions"] == dr.DEFAULT_INSTRUCTIONS
    assert "{{" not in sent["questions"]["answer"]["instructions"]


def test_criteria_without_cards_still_carry_the_live_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """No cards must still tell the model the load -- that is the whole point
    of the facts living in the criteria channel rather than the state."""
    caller, client = _caller_env(monkeypatch, {})
    asyncio.run(caller("{}", "policy", ["a", "b"], 2500, []))
    crit = client.calls[0]["json"]["questions"]["answer"]["criteria"]
    assert crit["a"].startswith("a | ")
    assert "gpu 1.5% (low demand)" in crit["a"]
    assert "gpu 88.0% (high demand)" in crit["b"]


def test_modalities_reach_the_criteria(monkeypatch: pytest.MonkeyPatch) -> None:
    caller, client = _caller_env(
        monkeypatch,
        {"a": {"description": "reads {{modalities}}"}},
        modalities={"a": ["text", "image"]},
    )
    asyncio.run(caller("{}", "policy", ["a", "b"], 2500, []))
    sent = client.calls[0]["json"]
    assert sent["questions"]["answer"]["criteria"]["a"].startswith("reads text, image")


def test_the_decision_window_is_floored_when_the_named_ceiling_is_off_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 (audit w6): a spend control the decision channel used to bypass.

    _window's off-ladder-ceiling branch returned the WHOLE ladder and dropped
    routing_min_model, so under decision_mode=active a request naming
    routing__<off-ladder-model> could be SERVED by a model weaker than the
    operator's floor. The heuristic channel on the identical request pins to
    the named ceiling, so the floor must only remove rungs BELOW it -- never
    make the request unservable.
    """
    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy("active", min_model="mid"))
    monkeypatch.setattr(mr.settings, "routing_enabled", True)

    offered: list[str] = []

    async def decide(candidates, body, score):
        offered.extend(candidates)
        return mr.DecisionOutcome(ok=True, choice="small", confidence=0.9, reason="decision")

    model, _route = _resolve(
        decide,
        ["big", "mid", "small"],  # most-capable first: cheap_first = [small, mid, big]
        requested="routing__offladder",
        registered=["big", "mid", "small", "offladder"],
    )

    assert "small" not in offered, f"a model below routing_min_model was offered: {offered}"
    assert model != "small", "the decision model must not serve below the operator's floor"
    assert model == "offladder", "the named ceiling is still the heuristic's answer for this request"
