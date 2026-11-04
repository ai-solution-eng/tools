"""The shipped router prompt must BE the operator template, and must stay model-agnostic.

An empty routing-config `prompt` falls back to
`decision_routing.DEFAULT_INSTRUCTIONS`, so this constant IS the production
prompt on any deployment that never saved one. A three-sentence default once
silently replaced the whole operator template, and nothing logged which text
the decision model actually received.

2026-10-30: the prompt stopped naming models. Per-model ROLE text moved to the
CRITERIA channel (routing_config.model_cards -> render_criteria), because the
candidate set is per-request (profile ladder, ACL, health, context fit) while a
prose role list is static -- a prompt naming three models is wrong on every
site whose ladder differs, and it made the decision model map prose onto bare
labels across two channels. These tests pin the new contract: the default
prompt is POLICY, the cards are the FACTS.
"""

from pathlib import Path

from pcai_llm.core import decision_routing

# The operator template lives with the other routing-prompt material
# (documentation/example_routing_prompts/ — moved there by the 2026-10-30 docs
# reorg; it used to sit at documentation/routing_text.md, which is why this
# guard went FileNotFound and took the release gate down with it). It is not an
# "example": its text IS DEFAULT_INSTRUCTIONS, the prompt every deployment that
# never saved one actually runs. routing_text_v2.md is a DRAFT and is
# deliberately not pinned here.
TEMPLATE = Path(__file__).resolve().parents[1] / "documentation" / "example_routing_prompts" / "routing_text.md"


def test_default_instructions_match_the_documented_template() -> None:
    # Fail with the reason, not a bare FileNotFoundError: this assertion is
    # what stops a silent prompt rewrite from shipping (the 3-sentence
    # regression), so a moved file must say "update TEMPLATE", not look like
    # a broken test.
    assert TEMPLATE.is_file(), f"the operator template moved — update TEMPLATE in this test (looked for {TEMPLATE})"
    documented = TEMPLATE.read_text(encoding="utf-8").rstrip("\n")
    assert decision_routing.DEFAULT_INSTRUCTIONS.rstrip("\n") == documented


def test_default_instructions_name_no_model() -> None:
    """The candidate set is per-request, so the prompt cannot name a candidate.

    This is the hardcoding guard: a model named here is a model the decision
    model may believe exists on a site whose ladder never offers it (and a
    model missing here has no description at all).
    """
    body = decision_routing.DEFAULT_INSTRUCTIONS.lower()
    for name in ("qwen", "glm", "deepseek", "d3-turbo", "vllm-sr", "llama", "mistral"):
        assert name not in body, f"the shipped prompt must not name {name!r}"
    # and it must not smuggle a per-model token in either: the tokens resolve
    # against ONE model, so they belong to the cards, not to the policy.
    from pcai_llm.core import routing_config

    for token in routing_config.CRITERIA_PLACEHOLDERS:
        assert "{{" + token + "}}" not in body, f"policy must not use the card token {token!r}"


def test_default_instructions_carry_no_placeholders() -> None:
    """Roles and live numbers belong to the CARDS, not the instructions.

    An instruction placeholder describes the SET (the model list, the whole
    load table) and had to be kept in sync with the criteria by hand. The
    cards resolve per model, so the default prompt needs none of them; the
    placeholders stay SUPPORTED for operator templates that still use them.
    """
    assert "{{" not in decision_routing.DEFAULT_INSTRUCTIONS
    # ...but the renderer still honours them for operator prompts.
    from pcai_llm.core import routing_config

    assert "model_list" in routing_config.PLACEHOLDERS
    assert "gpu_load" in routing_config.PLACEHOLDERS


def test_default_instructions_point_at_the_option_descriptions() -> None:
    """The prompt must tell the model WHERE the per-model facts are.

    With the roles gone from the instructions, the only place a model's role
    can come from is its own option description. A prompt that does not say so
    leaves the decision model with bare labels -- the failure mode measured on
    2026-10-30 (0/15 commits when the roles were removed and not relocated).
    """
    assert "option" in decision_routing.DEFAULT_INSTRUCTIONS.lower()
    assert "description" in decision_routing.DEFAULT_INSTRUCTIONS.lower()


def test_default_instructions_do_not_hardcode_the_confidence_floor() -> None:
    # The floor is operator-configurable (routing_config DEFAULTS
    # decision_min_confidence); a literal here would drift from the real knob.
    assert "0.60" not in decision_routing.DEFAULT_INSTRUCTIONS


def test_rule_one_prefers_the_current_turn_over_the_opening_ask() -> None:
    """The opening ask must not pin a whole conversation's classification.

    Found live (2026-10-30): a chat that began "Hello friend." never escalated,
    because the prompt said user_task OUTRANKS user_message -- and user_task is
    the first user message. Every later turn, including a hard one, was
    classified from the greeting. The rule exists for AGENT loops, where the
    last user turn is often harness plumbing; it must not override a real
    question sitting in front of the model.
    """
    body = decision_routing.DEFAULT_INSTRUCTIONS
    assert "outranks" not in body, "user_task must not outrank the current turn"
    assert "is the turn being served" in body
    # ...and the fallback it was protecting is still stated
    assert "tool result" in body and "subagent notice" in body
    # the failure case, named so a future edit cannot quietly reintroduce it
    assert "opened with" in body and "two turns later" in body
