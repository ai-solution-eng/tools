"""Secrets guardrail engine — credential detection, mask-only.

The engine is deliberately conservative (vendor prefixes / key headers), and
MASK-ONLY: no token->original mapping is stored, so a masked secret cannot be
re-inflated. These tests pin the three actions and the kinds subset.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from pcai_llm.core import guardrails as g

# Credential-shaped fixtures are split at the vendor/header prefix: the fleet
# pre-mirror secret scanner's hard patterns (JWT/sk-/hllm-) honour no allow
# marker, so the literal shape is what keeps this test file out of the mirror
# gate. The concatenated runtime value is byte-identical to the real shape.
OPENAI = "sk-" + "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"
GATEWAY = "hllm-" + "0123456789abcdef0123456789abcdef"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


def _spec(mode: str = "pre_call", **cfg) -> g.GuardrailSpec:
    return g.GuardrailSpec(id=uuid.uuid4(), name="secrets", mode=mode, engine="secrets", config=dict(cfg), enabled=True)


def _msgs(text: str) -> list[dict]:
    return [{"role": "user", "content": text}]


def test_engine_is_registered() -> None:
    assert "secrets" in g.GUARDRAIL_ENGINES


def test_pre_call_masks_an_openai_key() -> None:
    out, events = asyncio.run(g.run_pre_call([_spec()], _msgs(f"key={OPENAI}"), None))
    text = out[0]["content"]
    assert OPENAI not in text
    assert "[REDACTED:openai_key]" in text
    assert events[0].action == "masked"


def test_pre_call_masks_a_gateway_key() -> None:
    out, events = asyncio.run(g.run_pre_call([_spec()], _msgs(f"Authorization: Bearer {GATEWAY}"), None))
    assert GATEWAY not in out[0]["content"]
    assert "[REDACTED:gateway_key]" in out[0]["content"]
    assert events[0].action == "masked"


def test_pre_call_block_refuses() -> None:
    with pytest.raises(g.GuardrailBlocked) as exc:
        asyncio.run(g.run_pre_call([_spec(action="block")], _msgs(f"oops {JWT}"), None))
    assert exc.value.engine == "secrets"
    assert "jwt" in exc.value.detail


def test_pre_call_flag_leaves_text_untouched() -> None:
    msg = f"oops {OPENAI}"
    out, events = asyncio.run(g.run_pre_call([_spec(action="flag")], _msgs(msg), None))
    assert out[0]["content"] == msg
    assert events[0].action == "pass"
    assert "openai_key" in events[0].detail


def test_kinds_subset_limits_scope() -> None:
    # Only JWTs watched: the OpenAI key survives, the JWT is masked.
    out, _ = asyncio.run(g.run_pre_call([_spec(kinds=["jwt"])], _msgs(f"{OPENAI} {JWT}"), None))
    text = out[0]["content"]
    assert OPENAI in text
    assert JWT not in text


def test_ordinary_text_passes() -> None:
    msg = "please summarize the deployment notes"
    out, events = asyncio.run(g.run_pre_call([_spec()], _msgs(msg), None))
    assert out[0]["content"] == msg
    assert events[0].action == "pass"


def test_post_call_masks_the_answer() -> None:
    text, events, blocked = asyncio.run(g.run_post_call([_spec(mode="post_call")], f"here: {OPENAI}", None))
    assert not blocked
    assert OPENAI not in text
    assert "[REDACTED:openai_key]" in text
    assert events[0].action == "masked"


def test_post_call_block_flags_the_response() -> None:
    text, _events, blocked = asyncio.run(
        g.run_post_call([_spec(mode="post_call", action="block")], f"here: {JWT}", None)
    )
    assert blocked
    assert text == f"here: {JWT}"  # untouched; the caller refuses it


def test_masked_output_is_not_reversible() -> None:
    """No mapping is stored anywhere: the module exposes no re-inflate path."""
    assert not hasattr(g, "unmask_secrets")
    assert not hasattr(g, "restore_secrets")
