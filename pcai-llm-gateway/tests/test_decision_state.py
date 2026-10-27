"""The decision state must carry the conversation's TASK, not just its last turn.

Live traffic (2026-10-27) showed the last `role: "user"` message in an agent
harness is usually harness plumbing -- a subagent-completion notice inside a
116k-token conversation -- so the router classified nothing and abstained on
every turn. `user_task` carries the opening ask so the model has something real
to classify.
"""
from pcai_llm.core import decision_routing as dr


def _body(*user_turns: str) -> dict:
    messages = [{"role": "system", "content": "you are an agent"}]
    for i, text in enumerate(user_turns):
        if i:
            messages.append({"role": "assistant", "content": "ok"})
        messages.append({"role": "user", "content": text})
    return {"model": "routing", "messages": messages}


def test_first_and_last_turn_are_both_sent() -> None:
    state = dr.build_state(_body("port the chart to PCAI", "Background subagent finished."), complexity=100, candidates=["A", "B"])
    assert '"user_task": "port the chart to PCAI"' in state
    assert '"user_message": "Background subagent finished."' in state


def test_single_turn_is_not_duplicated() -> None:
    state = dr.build_state(_body("Hello friend."), complexity=0, candidates=["A", "B"])
    assert '"user_message": "Hello friend."' in state
    assert "user_task" not in state


def test_identical_first_and_last_turn_is_not_duplicated() -> None:
    state = dr.build_state(_body("same text"), complexity=0, candidates=["A", "B"])
    assert "user_task" not in state


def test_task_is_capped() -> None:
    long_task = "x" * (dr.MAX_USER_TASK_CHARS + 500)
    state = dr.build_state(_body(long_task, "now"), complexity=0, candidates=["A", "B"])
    assert '"user_task": "' + ("x" * dr.MAX_USER_TASK_CHARS) + '"' in state


def test_extract_user_task_returns_empty_without_a_user_turn() -> None:
    assert dr.extract_user_task({"messages": [{"role": "system", "content": "s"}]}) == ""
    assert dr.extract_user_task({}) == ""
