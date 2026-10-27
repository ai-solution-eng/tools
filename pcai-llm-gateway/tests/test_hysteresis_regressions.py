from __future__ import annotations

import asyncio

from tests.test_hysteresis import _hyst_run


def test_hysteresis_holds_on_an_unvalidated_probability_scale() -> None:
    """A non-0..1 distribution must HOLD, not silently bypass the margin.

    Regression (dual review 2026-10-26): percentages (60/55) made
    `_ch - _in` = 5.0 against a 0..1 margin, so hysteresis was disabled
    entirely -- the opposite of what an unreadable vector should do.
    """
    model, _route = _hyst_run("mid", 0.56, {"mid": 60.0, "small": 55.0}, "small", 0.10)
    assert model == "small"


def test_hysteresis_holds_when_the_incumbent_is_missing_from_the_distribution() -> None:
    """A partial dict is indistinguishable from a truncated one => hold."""
    model, _route = _hyst_run("mid", 0.90, {"mid": 0.90, "big": 0.10}, "small", 0.10)
    assert model == "small"


def test_build_state_is_always_valid_json_under_extreme_pressure() -> None:
    """The size guard must shed FIELDS, never slice the serialized JSON.

    Regression (dual review 2026-10-26): the last resort did
    `json.dumps(...)[:MAX_STATE_CHARS]`, emitting invalid JSON -- and a
    decision model given invalid JSON abstains on EVERY such request.
    """
    import json as _json

    from pcai_llm.core import decision_routing as dr

    cands = [f"model-with-a-very-long-name-{i}-" + "x" * 60 for i in range(200)]
    state = dr.build_state(
        {"messages": [{"role": "user", "content": "x" * 4000}]},
        complexity=99999,
        candidates=cands,
        current_model="m0",
        modalities={c: ["text", "image", "audio"] for c in cands},
    )
    assert len(state) <= dr.MAX_STATE_CHARS
    _json.loads(state)  # must not raise


def test_decide_does_not_double_call_a_five_arg_caller_that_raises() -> None:
    """A TypeError INSIDE a 5-arg caller must not re-run the call.

    Regression (dual review 2026-10-26): catching TypeError to detect a
    legacy 4-arg caller also caught internal TypeErrors, re-running the whole
    call -- two POSTs, and up to 2x timeout on the failure path.
    """
    calls: list[int] = []

    async def five_arg(state, instr, cands, tmo, images=None):
        calls.append(1)
        raise RuntimeError("boom")

    from pcai_llm.core import decision_routing as dr

    result = asyncio.run(
        dr.decide(five_arg, state="s", instructions="i", candidates=["a", "b"])
    )
    assert len(calls) == 1, "a 5-arg caller must be invoked exactly once"
    assert result.ok is False and result.reason == "call_failed"


def test_decide_still_supports_a_legacy_four_arg_caller() -> None:
    async def four_arg(state, instr, cands, tmo):
        return {"answers": {"answer": {"choice": "a", "confidence": 0.9}}}

    from pcai_llm.core import decision_routing as dr

    result = asyncio.run(
        dr.decide(four_arg, state="s", instructions="i", candidates=["a", "b"])
    )
    assert result.ok and result.choice == "a"


def test_image_counters_agree_on_a_malformed_part() -> None:
    """complexity_score and build_state must not disagree about an image."""
    from pcai_llm.core import decision_routing as dr
    from pcai_llm.core import model_routing as mr

    body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]}
    assert mr._count_images(body) == dr.count_images(body) == 0
