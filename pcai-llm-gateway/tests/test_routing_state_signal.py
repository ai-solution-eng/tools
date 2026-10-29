"""The routing state must carry the USER'S words, not the harness's clock.

WHY THIS FILE EXISTS (2026-10-28): the decision model was abstaining on 86.6%
of calls in production. The state it received had `user_message` =
"Time sampled while preparing turn 5, step 1: 2026-10-28T17:12:18-04:00..." on
every single request, and `user_task` was absent on all 21 sampled states --
the agent harness prepends a per-STEP runtime line, and both extractors were
returning it verbatim. The router was classifying a timestamp.

Two more defects fell out of the same root cause and are pinned here:
  * the sticky thread fingerprint hashed that clock line too, so every turn of
    one conversation produced a DIFFERENT thread key -- which is why 100% of
    route switches were "cold" and hysteresis could never fire;
  * candidates carried no `cost` even though the prompt tells the model to
    break ties by cost, so that instruction could never fire.
"""

from __future__ import annotations

import json

from pcai_llm.core import canonical, costs
from pcai_llm.core import decision_routing as dr

PREAMBLE = (
    "Time sampled while preparing turn 5, step 1: 2026-10-28T17:12:18-04:00[America/New_York]\n"
    "Browser time zone for this request: America/New_York. Interpret otherwise-unqualified "
    "dates and times in this zone.\n"
    "Elapsed since the preceding model-visible message: 2m 59s."
)


def _body(*user_turns: str) -> dict:
    return {
        "model": "routing",
        "messages": [{"role": "user", "content": t} for t in user_turns],
        "tools": [{"type": "function", "function": {"name": "x"}}],
    }


def test_strip_removes_the_harness_block_and_keeps_the_human_text() -> None:
    text = PREAMBLE + "\n\nPlease fix the routing state, the model is blind."
    stripped = dr.strip_harness_context(text)
    assert stripped == "Please fix the routing state, the model is blind."
    assert "Time sampled" not in stripped


def test_strip_collapses_a_pure_preamble_to_empty() -> None:
    assert dr.strip_harness_context(PREAMBLE) == ""
    assert dr.strip_harness_context("") == ""


def test_strip_keeps_a_human_line_that_merely_mentions_time() -> None:
    # The anchors are line-start/line-end on the KNOWN shapes: a human writing
    # about time sampling keeps their sentence.
    human = "The metric is called Time sampled while preparing turn, rename it."
    assert dr.strip_harness_context(human) == human


def test_extract_user_turn_skips_a_pure_preamble_message() -> None:
    body = _body("Review this diff for correctness.", PREAMBLE)
    text, images = dr.extract_user_turn(body)
    assert text == "Review this diff for correctness."
    assert images == []


def test_extract_user_turn_strips_an_inline_preamble() -> None:
    body = _body(PREAMBLE + "\n\nWhy is the router abstaining?")
    text, _ = dr.extract_user_turn(body)
    assert text == "Why is the router abstaining?"


def test_extract_user_task_returns_the_opening_ask_not_the_clock() -> None:
    body = _body(PREAMBLE + "\n\nPort the chart to PCAI.", PREAMBLE)
    assert dr.extract_user_task(body) == "Port the chart to PCAI."


def test_build_state_carries_the_real_message_and_the_task() -> None:
    body = _body("Port the chart to PCAI.", PREAMBLE + "\n\nNow add the ezua block.")
    state = json.loads(
        dr.build_state(body, complexity=1000, candidates=["GLM-5.3-Flash", "Qwen3.8-27B"])
    )
    request = state["request"]
    assert request["user_message"] == "Now add the ezua block."
    assert request["user_task"] == "Port the chart to PCAI."
    assert "Time sampled" not in json.dumps(state)


def test_build_state_carries_cost_when_the_caller_supplies_it() -> None:
    body = _body("hi")
    state = json.loads(
        dr.build_state(
            body,
            complexity=10,
            candidates=["GLM-5.3-Flash"],
            costs={"GLM-5.3-Flash": "in 0.30 / out 1.20 USD per 1M tokens"},
        )
    )
    assert state["candidates"][0]["cost"] == "in 0.30 / out 1.20 USD per 1M tokens"


def test_thread_key_is_stable_across_turns_that_only_differ_by_the_clock() -> None:
    # The defect: the preamble differs every turn, so hashing it raw gave every
    # turn its own thread and stickiness was structurally impossible.
    first = _body("Port the chart to PCAI.", PREAMBLE.replace("turn 5", "turn 1"))
    second = _body(
        "Port the chart to PCAI.",
        PREAMBLE.replace("turn 5", "turn 2"),
        PREAMBLE.replace("turn 5", "turn 3"),
    )
    assert canonical.thread_key(first["messages"], "key-1") == canonical.thread_key(
        second["messages"], "key-1"
    )


def test_thread_key_still_separates_different_conversations() -> None:
    a = _body("Port the chart to PCAI.", PREAMBLE)
    b = _body("Why is routing abstaining?", PREAMBLE)
    assert canonical.thread_key(a["messages"], "key-1") != canonical.thread_key(
        b["messages"], "key-1"
    )


def test_price_hints_formats_priced_models_and_skips_unpriced(monkeypatch) -> None:
    async def _fake_load():
        return {"GLM-5.3-Flash": {"input_per_m": 0.3, "output_per_m": 1.2, "cached_per_m": 0.06}}

    monkeypatch.setattr(costs, "_load", _fake_load)

    import asyncio

    hints = asyncio.run(costs.price_hints(["GLM-5.3-Flash", "Qwen3.8-27B"]))
    assert hints["GLM-5.3-Flash"] == "in 0.3 / out 1.2 USD per 1M tokens"
    assert "Qwen3.8-27B" not in hints  # unpriced is unknown, not free

def test_prom_samples_finds_a_counter_by_its_total_name() -> None:
    # The bug this pins: prometheus_client names a Counter's FAMILY without the
    # _total suffix, so matching the family alone reported zeros for every
    # counter and the panel looked empty rather than broken.
    from prometheus_client import Counter

    from pcai_llm.api.routes_admin import _prom_samples

    counter = Counter("zz_routing_stats_probe_total", "probe", ["outcome"])
    counter.labels(outcome="served").inc(2)
    samples = _prom_samples("zz_routing_stats_probe_total")
    assert any(
        s.name == "zz_routing_stats_probe_total" and s.labels.get("outcome") == "served"
        for s in samples
    )
    assert _prom_samples("zz_does_not_exist_total") == []


def test_strip_keeps_human_text_on_the_same_line_as_the_preamble() -> None:
    # The preamble is stripped as a PREFIX: a one-line message must keep its
    # human half, which a whole-line delete would have thrown away.
    one_line = (
        "Time sampled while preparing turn 5, step 1: 2026-10-28T17:12:18-04:00"
        "[America/New_York] Fix the bug in routes_proxy."
    )
    assert dr.strip_harness_context(one_line) == "Fix the bug in routes_proxy."


def test_strip_does_not_eat_a_human_line_that_opens_like_a_harness_line() -> None:
    # Case-SENSITIVE on purpose: IGNORECASE ate questions that merely started
    # with the same words.
    human = "Browser time zone for this request: is that a real header?"
    assert dr.strip_harness_context(human) == human
    human2 = "Current runtime context. Please explain what that means."
    assert dr.strip_harness_context(human2) == human2


def test_thread_key_is_stable_for_preamble_only_conversations() -> None:
    # A preamble-only conversation has no human words to hash. Hashing the raw
    # seed would hash the CLOCK and re-split the thread every turn -- defeating
    # the fix at exactly the turn-1/turn-2 boundary.
    first = _body(PREAMBLE.replace("turn 5", "turn 1"))
    second = _body(PREAMBLE.replace("turn 5", "turn 2"))
    assert canonical.thread_key(first["messages"], "key-1") == canonical.thread_key(
        second["messages"], "key-1"
    )


def test_thread_key_is_stable_for_multimodal_list_content() -> None:
    # List-shaped content is flattened BEFORE stripping; json.dumps put the
    # text behind quotes where the line anchor could never match.
    def body(turn: str) -> dict:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PREAMBLE.replace("turn 5", turn) + "\n\nLook at this."},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    ],
                }
            ]
        }

    assert canonical.thread_key(body("turn 1")["messages"], "key-1") == canonical.thread_key(
        body("turn 2")["messages"], "key-1"
    )


def test_hist_quantile_interpolates_instead_of_returning_bucket_bounds() -> None:
    from pcai_llm.api.routes_admin import _hist_quantile

    # 90 samples <=10ms, 10 samples <=5000ms: the naive upper-bound version
    # reported 5000 for p95.
    buckets = [(10.0, 90.0), (5000.0, 100.0)]
    p95 = _hist_quantile(buckets, 100.0, 0.95)
    assert p95 is not None
    assert 10.0 < p95 < 5000.0
    assert _hist_quantile([], 0.0, 0.5) is None


def test_strip_covers_every_browser_time_zone_variant() -> None:
    # request-zone.ts emits three shapes; only the "resolved" one was matched,
    # so the "unavailable" form (live in this fleet) survived as user_message.
    resolved = "Browser time zone for this request: America/New_York. Interpret otherwise-unqualified dates and times in this zone."
    unavailable = "Browser time zone for this request: unavailable. Ask the user to clarify otherwise-unqualified dates and times."
    mixed = 'Browser time zone for this request: mixed ["America/New_York","Europe/London"]. Ask the user to clarify otherwise-unqualified dates and times.'
    for variant in (resolved, unavailable, mixed):
        assert dr.strip_harness_context(variant) == "", variant


def test_strip_handles_a_bracket_less_timestamp() -> None:
    # The bracket used to be required by the prefix pattern, so a bracket-less
    # timestamp left the WHOLE preamble in the state -- the original defect.
    line = "Time sampled while preparing turn 5, step 1: 2026-10-28T17:12:18-04:00 Fix the bug."
    assert dr.strip_harness_context(line) == "Fix the bug."


def test_thread_key_finds_real_words_in_a_later_message() -> None:
    # The lazy scan must keep walking past preamble-only messages, not stop at
    # the first one it sees.
    body = _body(PREAMBLE, PREAMBLE.replace("turn 5", "turn 6"), "Port the chart.")
    key = canonical.thread_key(body["messages"], "key-1")
    assert key is not None
    assert key == canonical.thread_key(_body("Port the chart.")["messages"], "key-1")


def test_thread_key_is_stable_for_image_only_conversations() -> None:
    def body(turn: str) -> dict:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PREAMBLE.replace("turn 5", turn)},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                    ],
                }
            ]
        }

    assert canonical.thread_key(body("turn 1")["messages"], "k") == canonical.thread_key(
        body("turn 2")["messages"], "k"
    )


def test_price_hints_keeps_sub_cent_rates_significant(monkeypatch) -> None:
    async def _fake_load():
        return {"tiny": {"input_per_m": 0.0005, "output_per_m": 0.002, "cached_per_m": 0.0}}

    monkeypatch.setattr(costs, "_load", _fake_load)

    import asyncio

    hints = asyncio.run(costs.price_hints(["tiny"]))
    assert hints["tiny"] == "in 0.0005 / out 0.002 USD per 1M tokens"
    assert "0.00 " not in hints["tiny"]

