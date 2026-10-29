"""Header-aware memory floor: `distiller_min_doc_chars` measures the BODY.

WHY (2026-10-20): the floor used to compare `len(doc.strip())` — the WHOLE
document, including the machine-generated `[MEMORY INDEX | ...]` header. That
header's length varies with the project slug and the number of topics, so a
stub with a long topics list carried a 164-char header and cleared a 150-char
floor ON THE HEADER ALONE, with a one-word body ("Greeting").

`memory_body_chars` strips the index line, so the configured number means
"how much memory content is here" — what an operator setting it intends — and
the guard is immune to header length.

The live evidence this closes: the 11 stub memories deleted 2026-10-20 ran
105-128 chars total with bodies of 18-40 chars, while real distilled summaries
run 789+ chars. A 150-char BODY floor rejects every observed stub and no real
summary.
"""

from __future__ import annotations

import pytest

from pcai_llm.core.distiller import (
    GENERIC_MEMORY_INDEX,
    memory_body_chars,
    synthesize_memory_index,
)

# A header as long as a real one with many topics (164 chars measured live).
LONG_HEADER = (
    "[MEMORY INDEX | project: pcai-llm-gateway | type: session-summary | "
    "topics: dsh-plugins, memory-distiller, stub-recurrence, dsh-titler, gateway-fix, memory-hygiene]"
)


# ---------------------------------------------------------------------------
# memory_body_chars
# ---------------------------------------------------------------------------


def test_body_excludes_the_index_header():
    doc = GENERIC_MEMORY_INDEX + "\n\n## What we did\n\nReal content here."
    assert memory_body_chars(doc) == len("## What we did\n\nReal content here.")
    assert memory_body_chars(doc) < len(doc)


def test_body_of_a_title_only_stub_is_tiny():
    """The exact stub shape that polluted the store."""
    doc = synthesize_memory_index("DeepSeek G2 Huge Pages Recall")
    assert memory_body_chars(doc) == len("DeepSeek G2 Huge Pages Recall") == 29
    assert len(doc.strip()) == 112  # the whole-doc number the old floor used


def test_body_is_immune_to_a_long_topics_header():
    """The motivating case: a long header must not satisfy a body floor."""
    doc = LONG_HEADER + "\n\nGreeting"
    assert len(doc.strip()) == 174, "precondition: the whole doc clears 150"
    assert memory_body_chars(doc) == len("Greeting") == 8


def test_body_ignores_a_document_without_a_header():
    """No header -> the whole thing is body (the inline/caller-supplied case)."""
    assert memory_body_chars("just some content") == len("just some content")
    assert memory_body_chars("# Session History\n\n### User\nhi") > 0


def test_body_handles_blank_runs_and_crlf():
    doc = GENERIC_MEMORY_INDEX + "\r\n\r\n\r\ncontent"
    assert memory_body_chars(doc) == len("content")


def test_body_survives_malformed_input():
    """A guard fault must never abort a write."""
    assert memory_body_chars("") == 0
    assert memory_body_chars(GENERIC_MEMORY_INDEX) == 0
    assert memory_body_chars(None) == 0  # type: ignore[arg-type]
    assert memory_body_chars(123) == 0  # type: ignore[arg-type]


def test_body_only_strips_a_LEADING_header():
    """A header-looking line mid-document is content, not scaffolding."""
    doc = "## What we did\n\n[MEMORY INDEX | quoted in the body]"
    assert memory_body_chars(doc) == len(doc)


# ---------------------------------------------------------------------------
# the floor itself (write_memory refuses under it)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_floor_rejects_a_stub_but_keeps_a_real_summary(monkeypatch: pytest.MonkeyPatch):
    """End-to-end: the configured floor gates on body chars, not total chars."""
    from pcai_llm.config import settings
    from pcai_llm.core import memory_write as mw

    written: list[str] = []

    async def mcp_call(args):
        written.append(args["text"])
        return True, "stored"

    monkeypatch.setattr(settings, "distiller_min_doc_chars", 150)

    # A stub: long-ish header, tiny body -> refused, nothing written.
    stub = synthesize_memory_index("DeepSeek G2 Huge Pages Recall")
    ok, detail = await mw.write_memory(None, mcp_call, stub, session_id="s")
    assert ok is False and "floor" in detail
    assert written == []

    # The long-header case that beat the OLD floor -> refused too.
    long_stub = LONG_HEADER + "\n\nGreeting"
    ok2, _ = await mw.write_memory(None, mcp_call, long_stub, session_id="s")
    assert ok2 is False and written == []

    # A real summary passes.
    real = GENERIC_MEMORY_INDEX + "\n\n" + ("## What we did\n" + "x" * 200)
    ok3, _ = await mw.write_memory(None, mcp_call, real, session_id="s")
    assert ok3 is True and len(written) == 1


def test_the_recommended_floor_100_separates_stubs_from_real_cards():
    """Pin the shipped value: 100 must reject every observed stub and keep a
    real one-fact card. Raising it to the old whole-document 150 breaks this.

    MEASURED 2026-10-20 (BODY chars, header excluded):
      - the 11 title stubs deleted that day ran 8-37 chars (max 37)
      - a real one-fact card measures 130
    100 sits +63 above the worst stub and +30 below the real card.
    """
    observed_stubs = [
        "Greeting",
        "Listing Recent Memories",
        "DeepSeek G2 Huge Pages Recall",
        "Fix DSH local model resolution",
        "Distill Williams Deck into PDF",
        "Checking DSH version update",
    ]
    real_card = (
        "[MEMORY INDEX | project: sqlhandler | type: fact-card | topics: sqlhandler, sql, duckdb]"
        "  SQLhandler vs EzPresto benchmark environment on G2 cluster"
        " (pcai-se-ai-application.hst.rdlabs.hpecorp.net), discovered 2026-09-02."
    )

    assert memory_body_chars(real_card) == 130, "the real card's body length is the upper bound"

    for title in observed_stubs:
        body = memory_body_chars(synthesize_memory_index(title))
        assert body < 100, f"{title!r} (body {body}) must be REJECTED at floor 100"

    assert memory_body_chars(real_card) > 100, "the real card must SURVIVE floor 100"


@pytest.mark.asyncio
async def test_floor_zero_disables_the_guard(monkeypatch: pytest.MonkeyPatch):
    """0 (the default) must keep writing anything non-empty."""
    from pcai_llm.config import settings
    from pcai_llm.core import memory_write as mw

    written: list[str] = []

    async def mcp_call(args):
        written.append(args["text"])
        return True, "stored"

    monkeypatch.setattr(settings, "distiller_min_doc_chars", 0)
    ok, _ = await mw.write_memory(None, mcp_call, synthesize_memory_index("Greeting"), session_id="s")
    assert ok is True and len(written) == 1


# ---------------------------------------------------------------------------
# AUDIT 2026-10-29 D-2: the floor reports its own verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_floor_reports_the_floored_verdict(monkeypatch):
    """A floored document is neither a landed write nor a transient failure:
    the verdict channel names it so a caller can tell the three apart (the
    distiller sweep marks on 'suppressed' only)."""
    from pcai_llm.config import settings
    from pcai_llm.core import memory_write as mw

    monkeypatch.setattr(settings, "distiller_min_doc_chars", 250, raising=False)

    async def fake_call(args):
        raise AssertionError("a floored document must not reach the memory server")

    verdict: list[str] = []
    ok, _ = await mw.write_memory(None, fake_call, "## Gotchas", session_id="s", verdict_out=verdict)
    assert ok is False
    assert verdict == [mw.VERDICT_FLOORED] == ["floored"]
