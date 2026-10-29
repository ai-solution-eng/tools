"""Upstream wire compatibility — the system-message normalizer.

LIVE DEFECT (2026-10-29), in two acts:

  1. A DSH title call pinned to Qwen3.8-27B came back
     `400 {"message":"Unexpected message role."}` and the gateway rendered it as
     the session title "(model error 400)". Cause: OpenAI's `developer` role,
     which SGLang's Qwen templates predate.
  2. Rewriting `developer` to `system` IN PLACE fixed that and immediately
     broke the next shape: `[system, system, user]` answers
     `400 {"message":"System message must be at the beginning."}` — so every
     routed turn whose history carried a developer event still died, now with
     a different message. Verified against the live endpoint: one leading
     system succeeds, two systems fail, and the merged single system succeeds.

Contract under test:
  * every `system`/`developer` message folds into ONE leading system message,
    contents joined in order;
  * an already-valid body (no system message, or exactly one and it leads) is
    returned as the SAME object — zero-copy, because this runs on every
    proxied request;
  * other roles keep their order and position after the leading block;
  * the caller's body is never mutated (the ledger settle reads it afterwards).
"""

from __future__ import annotations

from typing import Any

from pcai_llm.core.wire_compat import normalize_system_messages


def _body(*messages: dict[str, Any], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"model": "Qwen3.8-27B", "max_tokens": 2048, "messages": list(messages)}
    body.update(extra)
    return body


def _m(role: str, text: str) -> dict[str, Any]:
    return {"role": role, "content": text}


def test_developer_is_folded_into_the_leading_system() -> None:
    """The shape that 400'd with 'System message must be at the beginning'."""
    body = _body(_m("system", "sys"), _m("developer", "dev"), _m("user", "hi"))
    out = normalize_system_messages(body)
    assert [m["role"] for m in out["messages"]] == ["system", "user"]
    assert out["messages"][0]["content"] == "sys\n\ndev"
    assert out["messages"][1]["content"] == "hi"
    assert len(body["messages"]) == 3  # caller untouched


def test_developer_only_body_becomes_a_leading_system() -> None:
    """'Unexpected message role' — the original failure."""
    out = normalize_system_messages(_body(_m("developer", "dev"), _m("user", "hi")))
    assert [m["role"] for m in out["messages"]] == ["system", "user"]
    assert out["messages"][0]["content"] == "dev"


def test_mid_conversation_developer_is_lifted_to_the_front() -> None:
    body = _body(_m("system", "sys"), _m("user", "hi"), _m("developer", "later"), _m("assistant", "yo"))
    out = normalize_system_messages(body)
    assert [m["role"] for m in out["messages"]] == ["system", "user", "assistant"]
    assert out["messages"][0]["content"] == "sys\n\nlater"


def test_multiple_systems_merge_in_order() -> None:
    out = normalize_system_messages(_body(_m("system", "a"), _m("system", "b"), _m("user", "hi")))
    assert out["messages"][0]["content"] == "a\n\nb"


def test_zero_copy_for_already_valid_bodies() -> None:
    leading = _body(_m("system", "sys"), _m("user", "hi"))
    assert normalize_system_messages(leading) is leading
    no_system = _body(_m("user", "hi"))
    assert normalize_system_messages(no_system) is no_system


def test_never_raises_on_malformed_bodies() -> None:
    for body in ({}, {"messages": None}, {"messages": []}, {"messages": "nope"}, {"messages": [None, 1, "x"]}):
        assert normalize_system_messages(body) is body


def test_part_list_system_content_is_flattened() -> None:
    body = _body(
        {"role": "system", "content": [{"type": "text", "text": "part one"}]},
        {"role": "developer", "content": [{"type": "text", "text": "part two"}]},
        _m("user", "hi"),
    )
    out = normalize_system_messages(body)
    assert out["messages"][0]["content"] == "part one\n\npart two"
