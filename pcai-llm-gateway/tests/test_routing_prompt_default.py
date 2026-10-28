"""The shipped router prompt must BE the operator template, not a stub.

An empty routing-config `prompt` falls back to
`decision_routing.DEFAULT_INSTRUCTIONS`, so this constant IS the production
prompt on any deployment that never saved one. A three-sentence default once
silently replaced the whole operator template, and nothing logged which text
the decision model actually received.
"""

from pathlib import Path

from pcai_llm.core import decision_routing

TEMPLATE = Path(__file__).resolve().parents[1] / "documentation" / "routing_text.md"


def test_default_instructions_match_the_documented_template() -> None:
    documented = TEMPLATE.read_text(encoding="utf-8").rstrip("\n")
    assert decision_routing.DEFAULT_INSTRUCTIONS.rstrip("\n") == documented


def test_default_instructions_keep_the_placeholders() -> None:
    assert "{{model_list}}" in decision_routing.DEFAULT_INSTRUCTIONS
    assert "{{gpu_load}}" in decision_routing.DEFAULT_INSTRUCTIONS


def test_default_instructions_do_not_hardcode_the_confidence_floor() -> None:
    # The floor is operator-configurable (routing_config DEFAULTS
    # decision_min_confidence); a literal here would drift from the real knob.
    assert "0.60" not in decision_routing.DEFAULT_INSTRUCTIONS
