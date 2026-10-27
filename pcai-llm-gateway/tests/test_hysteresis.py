from __future__ import annotations

import asyncio
from typing import ClassVar

# ---------------------------------------------------------------------------
# switch hysteresis (2026-10-26) -- a challenger must beat the incumbent
# ---------------------------------------------------------------------------

def _hyst_run(choice, conf, probs, incumbent, margin, order=("big", "mid", "small"), text="hi"):
    """Resolve one routed request with an injected decision + incumbent."""
    from pcai_llm.core import model_routing as _mr

    class _K:
        user_id = "00000000-0000-0000-0000-000000000001"
        allowed_models: ClassVar[list[str]] = []

    async def _fake_policy(_u):
        return "active", "small"

    async def _decide(_c, _b, _s):
        return _mr.DecisionOutcome(
            ok=True, choice=choice, confidence=conf, reason="decision", probabilities=probs
        )

    async def _go():
        return await _mr.resolve_routed_model(
            _K(),
            {"messages": [{"role": "user", "content": text}]},
            "routing",
            set(order),
            [],
            list(order),
            available=None,
            decide=_decide,
            incumbent=incumbent,
            switch_margin=margin,
        )

    orig = _mr.profile_routing_config
    _mr.profile_routing_config = _fake_policy
    try:
        return asyncio.run(_go())
    finally:
        _mr.profile_routing_config = orig


def test_hysteresis_holds_a_marginal_challenger() -> None:
    """A 0.60-vs-0.55 challenger must not pay the re-prefill of a switch."""
    model, _route = _hyst_run("mid", 0.60, {"mid": 0.60, "small": 0.55, "big": 0.05}, "small", 0.10)
    assert model == "small"


def test_hysteresis_allows_a_decisive_challenger() -> None:
    model, route = _hyst_run("mid", 0.85, {"mid": 0.85, "small": 0.10, "big": 0.05}, "small", 0.10)
    assert model == "mid"
    assert route.reason.startswith("decision:")


def test_hysteresis_hold_serves_the_incumbent_not_the_heuristic() -> None:
    """The hold must ACTUALLY stay on the incumbent.

    Regression (dual review 2026-10-26): leaving `choice` as the heuristic pick
    meant a held switch could still serve a different model -- the never-
    downgrade floor permits ESCALATION, so incumbent=small + heuristic=big +
    held challenger served big while the log claimed 'staying on the
    incumbent'. The re-prefill the margin exists to prevent was paid anyway.
    """
    model, route = _hyst_run(
        "mid", 0.60, {"mid": 0.60, "small": 0.55, "big": 0.05}, "small", 0.10,
        text="x" * 40000,  # huge body => heuristic picks the ceiling
    )
    assert model == "small", "a held switch must serve the incumbent"
    assert route.reason == "hysteresis:held"


def test_hysteresis_cold_start_takes_the_pick() -> None:
    """No incumbent => nothing to protect; the model's pick serves."""
    model, _route = _hyst_run("big", 0.42, {"big": 0.42, "small": 0.40}, None, 0.10)
    assert model == "big"


def test_hysteresis_agreeing_with_the_incumbent_is_free() -> None:
    model, _route = _hyst_run("small", 0.42, {"small": 0.42, "big": 0.40}, "small", 0.10)
    assert model == "small"


def test_hysteresis_holds_when_incumbent_absent_from_distribution() -> None:
    """A missing incumbent key => HOLD, not a free switch.

    Reversed by the dual review (2026-10-26). The original reading -- 'the
    model judged the incumbent out of contention, so any alternative wins' --
    cannot be distinguished in code from a TRUNCATED or partial probability
    dict, and the failure mode of guessing wrong is an expensive re-prefill.
    An unreadable distribution must never authorise a switch.
    """
    model, _route = _hyst_run("big", 0.90, {"big": 0.90, "mid": 0.10}, "small", 0.10)
    assert model == "small"


def test_hysteresis_margin_zero_allows_a_marginal_switch() -> None:
    model, _route = _hyst_run("mid", 0.56, {"mid": 0.56, "small": 0.55}, "small", 0.0)
    assert model == "mid"


# ---------------------------------------------------------------------------
# routing_config: the new knobs must survive the store
# ---------------------------------------------------------------------------


def test_model_modalities_roundtrips_through_the_store() -> None:
    """A dict-typed config value must survive encode->decode.

    Regression (dual review 2026-10-26): _encode had no dict tag, so a
    modalities map stored as a Python-repr 's:' string and decision_settings
    silently dropped it -- the feature could not be saved at all.
    """
    from pcai_llm.core import routing_config as rc

    value = {"Qwen3.8-27B": ["text", "image"], "GLM-5.3-Flash": ["text"]}
    assert rc._decode(rc._encode(value)) == value


def test_junk_float_decodes_to_none_not_zero() -> None:
    """Junk must degrade to the measured default, never to 0.0.

    0.0 is the LEAST safe value for both float knobs: confidence floor 0.0
    means the decision model never abstains, switch margin 0.0 disables
    hysteresis. None lets decision_settings substitute the shipped defaults.
    """
    from pcai_llm.core import routing_config as rc

    assert rc._decode("f:not-a-number") is None


def test_decision_settings_exposes_switch_margin_and_modalities() -> None:
    from pcai_llm.core import routing_config as rc

    settings = asyncio.run(rc.decision_settings())
    assert "switch_margin" in settings
    assert 0.0 <= settings["switch_margin"] <= 1.0
    assert isinstance(settings.get("modalities"), dict)
