"""Regression cover for the title-stub leak (2026-10-20).

SYMPTOM: the memory store accumulated ~18 memories whose entire body is a
session TITLE, each stamped with the generic
`[MEMORY INDEX | project: general | type: session-summary | topics: session, misc]`
header (e.g. "Greeting", "Updating dsh package version"). They add no recall
value and dilute embedding search.

TWO INDEPENDENT DEFECTS produce them; both are pinned here.

DEFECT 1 - the transcript collapse guard measures the WRONG length.
    build_transcript prepends `# Session History - <title>`. A conversation
    with zero extractable MESSAGE text therefore still renders to 28-48
    chars and sails past MIN_TRANSCRIPT_CHARS (20). The distill model gets a
    "transcript" that contains nothing but a title, correctly answers with
    that title, emits no [MEMORY INDEX] header (there is nothing to
    summarize), and synthesize_memory_index prepends the generic one - the
    exact stub bytes. The guard must count BODY text, not header text.

DEFECT 2 - the title detector is structural on a SINGLE message.
    is_title_messages/matches_title_framing require len(messages) == 1.
    A canonical row that accumulated TWO title calls (4 messages: user
    framing, assistant title, user framing, assistant title) is not
    recognized, and it clears the `len(messages) < 4` one-shot filter - so
    it IS distilled. The transcript then contains nothing but title-call
    frames, and the model answers with a title.
"""

from __future__ import annotations

import json

from pcai_llm.core import title_requests
from pcai_llm.core.distiller import build_transcript, synthesize_memory_index

GEN_FRAMING = "Generate the session title from this JSON array of human messages:"
REV_FRAMING = "Update the session title from this JSON object:"

STUB_INDEX = "[MEMORY INDEX | project: general | type: session-summary | topics: session, misc]"


def _gen_msg(text: str = "investigate the routing bug") -> dict:
    return {
        "role": "user",
        "content": GEN_FRAMING + "\n" + json.dumps([{"role": "user", "content": text}]),
    }


def _rev_msg(title: str = "Routing bug") -> dict:
    return {
        "role": "user",
        "content": REV_FRAMING + "\n" + json.dumps({"currentTitle": title, "messages": []}),
    }


# ---------------------------------------------------------------------------
# DEFECT 1 - header length must not satisfy the collapse guard
# ---------------------------------------------------------------------------


def test_body_text_ignores_the_session_header():
    """The helper the collapse guard must use: BODY chars, not header chars."""
    from pcai_llm.api.routes_distill import transcript_body_chars

    # A title and nothing else: the header is long, the body is empty.
    assert transcript_body_chars(build_transcript([], "Updating dsh package version")) == 0
    assert transcript_body_chars(build_transcript([], "Greeting")) == 0
    # The canonical path (no title) is already caught today; keep it caught.
    assert transcript_body_chars(build_transcript([])) == 0
    # A real conversation has body text.
    real = build_transcript([{"role": "user", "content": "fix the retry loop"}], "Retry bug")
    assert transcript_body_chars(real) > 0


def test_title_only_conversation_is_not_distillable():
    """A title-only transcript must fail the MIN_TRANSCRIPT_CHARS gate.

    This is the end-to-end statement of defect 1: the gate is what stands
    between a degenerate row and a stub memory.
    """
    from pcai_llm.api.routes_distill import MIN_TRANSCRIPT_CHARS, transcript_body_chars

    for title in ("Greeting", "Updating dsh package version", "Checking dsh version update"):
        transcript = build_transcript([], title)
        # The raw length passes the old gate (that is the bug) ...
        assert len(transcript.strip()) >= MIN_TRANSCRIPT_CHARS
        # ... but the body length must not.
        assert transcript_body_chars(transcript) < MIN_TRANSCRIPT_CHARS


def test_caller_supplied_header_only_transcript_is_rejected():
    """The INLINE /distill surface takes a caller-supplied transcript.

    A harness renders its own transcript with the same `# Session History -
    <title>` opening line, so the raw-length gate would admit a header + a
    title and reproduce the stub on that path too. transcript_body_chars
    strips the header only when line 0 IS the header, so real content is
    never eaten.
    """
    from pcai_llm.api.routes_distill import MIN_TRANSCRIPT_CHARS, transcript_body_chars

    # Exactly what a harness shim would POST for a title-only session.
    shim = "# Session History - Updating dsh package version"
    assert len(shim) >= MIN_TRANSCRIPT_CHARS, "raw length would have passed the old gate"
    assert transcript_body_chars(shim) < MIN_TRANSCRIPT_CHARS

    # A real caller transcript passes untouched, header or not.
    assert transcript_body_chars("# Session History - Retry bug\n\n### User\nfix the loop") >= MIN_TRANSCRIPT_CHARS
    assert transcript_body_chars("### User\nfix the loop in the retry path") >= MIN_TRANSCRIPT_CHARS


def test_body_chars_survives_unstructured_shapes():
    """Tolerance: never raise on a malformed render (guard must not crash a sweep)."""
    from pcai_llm.api.routes_distill import transcript_body_chars

    assert transcript_body_chars("") == 0
    assert transcript_body_chars("# Session History") == 0
    assert transcript_body_chars("no header at all") > 0


# ---------------------------------------------------------------------------
# DEFECT 2 - accumulated title-call rows must be detected
# ---------------------------------------------------------------------------


def test_single_title_call_still_detected():
    """The existing 1-message detection must not regress."""
    assert title_requests.is_title_messages([_gen_msg()])
    assert title_requests.is_title_messages([_rev_msg()])


def test_two_accumulated_title_calls_are_detected():
    """A row holding TWO title exchanges is still nothing but title calls.

    Shape observed live: the harness resends accumulated history, so a
    thread that saw two title calls stores 4 messages.
    """
    row = [
        _gen_msg(),
        {"role": "assistant", "content": "Updating dsh package version"},
        _rev_msg("Updating dsh package version"),
        {"role": "assistant", "content": "Updating dsh package version"},
    ]
    assert len(row) >= 4, "the one-shot filter would let this row through"
    assert title_requests.is_title_messages(row) is True


def test_many_accumulated_title_calls_are_detected():
    row = []
    for i in range(3):
        row.append(_gen_msg(f"message {i}"))
        row.append({"role": "assistant", "content": f"Title {i}"})
    assert title_requests.is_title_messages(row) is True


def test_real_conversation_with_a_pasted_framing_is_not_a_title_row():
    """FALSE-POSITIVE SAFETY must survive the widened detector.

    A user who pastes a title framing into a real chat, then keeps talking,
    has a genuine conversation. The row must NOT be excluded.
    """
    row = [
        _gen_msg(),
        {"role": "assistant", "content": "Routing bug"},
        {"role": "user", "content": "no, actually fix the pagination bug in the admin UI"},
        {"role": "assistant", "content": "Looking at the pagination code now."},
    ]
    assert title_requests.is_title_messages(row) is False


def test_real_conversation_is_not_a_title_row():
    row = [
        {"role": "user", "content": "why is the distill sweep writing title stubs?"},
        {"role": "assistant", "content": "Because the collapse guard counts header chars."},
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "Done."},
    ]
    assert title_requests.is_title_messages(row) is False


def test_malformed_rows_are_kept_not_dropped():
    """Unclassifiable input is KEPT (a real conversation is dearer than a stray)."""
    assert title_requests.is_title_messages([]) is False
    assert title_requests.is_title_messages(None) is False
    assert title_requests.is_title_messages([{"role": "user", "content": ""}]) is False
    assert title_requests.is_title_messages(["not a dict"]) is False


# ---------------------------------------------------------------------------
# the stub shape itself, so the fingerprint is documented in code
# ---------------------------------------------------------------------------


def test_stub_bytes_are_recognizable():
    """Pin the exact stub shape the store accumulated."""
    stub = synthesize_memory_index("Updating dsh package version")
    assert stub.startswith(STUB_INDEX)
    body = stub[len(STUB_INDEX) :].strip()
    assert body == "Updating dsh package version"
    assert "## " not in stub, "a real summary always carries sections"


# ---------------------------------------------------------------------------
# AUDIT 2026-10-29 B-F1 (P1): decode the JSONB payload at the boundary
# ---------------------------------------------------------------------------


def test_f1_jsonb_string_payload_is_decoded_before_classification():
    """asyncpg returns a jsonb column as a STRING (no JSONB codec is
    registered on the pool). The title detector starts with
    "if not isinstance(messages, list): return False", so on the REAL row
    shape it returned False for every row and the exclusion was inert.

    Pre-fix: is_title_messages(<the str>) -> False for the title row below.
    """
    import json

    from pcai_llm.core.distill_schedule import decode_jsonb_messages

    row = [
        _gen_msg("investigate the routing bug"),
        {"role": "assistant", "content": "Routing bug"},
        _rev_msg("Routing bug"),
        {"role": "assistant", "content": "Routing bug"},
    ]
    payload = json.dumps(row)
    assert title_requests.is_title_messages(payload) is False  # the inert path
    decoded = decode_jsonb_messages(payload)
    assert isinstance(decoded, list)
    assert title_requests.is_title_messages(decoded) is True


def test_f1_decode_leaves_a_real_conversation_alone():
    import json

    from pcai_llm.core.distill_schedule import decode_jsonb_messages

    row = [
        {"role": "user", "content": "why is the distill sweep writing title stubs?"},
        {"role": "assistant", "content": "Because the guard counts header chars."},
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "Done."},
    ]
    assert decode_jsonb_messages(json.dumps(row)) == row
    assert title_requests.is_title_messages(decode_jsonb_messages(json.dumps(row))) is False


def test_f1_decode_is_tolerant_of_malformed_payloads():
    """A payload that is not a JSON array is returned UNCHANGED: a malformed
    row must never be silently replaced, and build_transcript has its own
    recovery for the double-encoded shapes."""
    from pcai_llm.core.distill_schedule import decode_jsonb_messages

    assert decode_jsonb_messages("not json at all") == "not json at all"
    assert decode_jsonb_messages('{"role": "user"}') == '{"role": "user"}'
    assert decode_jsonb_messages([{"role": "user"}]) == [{"role": "user"}]
    assert decode_jsonb_messages(None) is None
