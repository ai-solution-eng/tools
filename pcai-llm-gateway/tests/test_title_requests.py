"""Session-title request detection and its capture/distill exclusions.

Regression cover for the 2026-10-17 bug: DSH fires an auxiliary session-title
LLM call after EVERY human prompt, and the gateway captured each one as a
conversation, so the distiller wrote one bogus "session summary" per prompt
into the user's memory dataset (whole bodies: "Deepseek calls misrouted to
GLM", or "Session consisted solely of a title-generation request...").
"""

from __future__ import annotations

import json

import pytest

from pcai_llm.core import title_requests

GEN_FRAMING = "Generate the session title from this JSON array of human messages:"
REV_FRAMING = "Update the session title from this JSON object:"


def _gen_body(payload=None) -> dict:
    """The exact shape DSH's title provider sends for a first-prompt title."""
    messages = payload if payload is not None else [{"role": "user", "content": "investigate the routing bug"}]
    return {
        "model": "Qwen3.8-27B",
        "messages": [{"role": "user", "content": GEN_FRAMING + "\n" + json.dumps(messages)}],
    }


def _rev_body() -> dict:
    return {
        "model": "Qwen3.8-27B",
        "messages": [
            {
                "role": "user",
                "content": REV_FRAMING + "\n" + json.dumps({"currentTitle": "Routing bug", "messages": []}),
            }
        ],
    }


class TestBodyFingerprint:
    def test_first_prompt_generation_framing(self):
        assert title_requests.is_title_request(_gen_body()) is True

    def test_revision_framing(self):
        assert title_requests.is_title_request(_rev_body()) is True

    def test_case_insensitive_framing(self):
        body = {
            "messages": [
                {"role": "user", "content": GEN_FRAMING.lower() + "\n" + json.dumps([{"role": "user", "content": "x"}])}
            ]
        }
        assert title_requests.is_title_request(body) is True

    def test_leading_whitespace_tolerated(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": "\n\n   " + GEN_FRAMING + " " + json.dumps([{"role": "user", "content": "x"}]),
                }
            ]
        }
        assert title_requests.is_title_request(body) is True

    def test_content_part_list_shape(self):
        """The gateway accepts part-list content; the detector must not be
        narrower than the payloads it classifies."""
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": GEN_FRAMING + " " + json.dumps([{"role": "user", "content": "x"}])}
                    ],
                }
            ]
        }
        assert title_requests.is_title_request(body) is True


class TestFalsePositiveSafety:
    """A real conversation must NEVER be suppressed. These are the tests that
    protect the user's memories from the fix itself."""

    def test_ordinary_two_message_conversation(self):
        body = {
            "messages": [
                {"role": "user", "content": "Why are DeepSeek calls routed to GLM?"},
                {"role": "assistant", "content": "Let me check the cascade ordering."},
            ]
        }
        assert title_requests.is_title_request(body) is False

    def test_single_user_message_is_not_a_title_request(self):
        """One user message alone is NOT enough — the framing must match."""
        body = {"messages": [{"role": "user", "content": "Please title this session about routing."}]}
        assert title_requests.is_title_request(body) is False

    def test_user_pastes_the_framing_as_prose(self):
        """A user who literally pastes the framing into a chat keeps their
        conversation: the tail is not a JSON title payload."""
        body = {
            "messages": [{"role": "user", "content": GEN_FRAMING + " I want you to explain how this prompt works."}]
        }
        assert title_requests.is_title_request(body) is False

    def test_framing_with_unparseable_tail(self):
        body = {"messages": [{"role": "user", "content": GEN_FRAMING + " {not json"}]}
        assert title_requests.is_title_request(body) is False

    def test_framing_in_a_later_message_is_not_matched(self):
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": GEN_FRAMING + " []"},
            ]
        }
        assert title_requests.is_title_request(body) is False

    def test_framing_with_assistant_turn_present(self):
        body = {
            "messages": [
                {"role": "user", "content": GEN_FRAMING + " " + json.dumps([{"role": "user", "content": "x"}])},
                {"role": "assistant", "content": "Session Title"},
            ]
        }
        assert title_requests.is_title_request(body) is False

    @pytest.mark.parametrize("body", [None, {}, {"messages": "nope"}, {"messages": []}, {"messages": [None]}])
    def test_malformed_bodies_are_not_title_requests(self, body):
        assert title_requests.is_title_request(body) is False


class TestHeader:
    def test_header_alone_is_authoritative(self):
        """The header wins even when the body looks nothing like a title call:
        it is the precise signal a future adapter will send."""
        body = {"messages": [{"role": "user", "content": "a real question"}]}
        assert title_requests.is_title_request(body, {"x-pcai-purpose": "session-title"}) is True

    def test_header_case_insensitive_name_and_value(self):
        body = {"messages": [{"role": "user", "content": "q"}]}
        assert title_requests.is_title_request(body, {"X-PCAI-Purpose": "Session-Title"}) is True

    def test_header_multi_value_tolerated(self):
        body = {"messages": [{"role": "user", "content": "q"}]}
        assert title_requests.is_title_request(body, {"x-pcai-purpose": "compaction, session-title"}) is True

    def test_unrelated_header_value_ignored(self):
        body = {"messages": [{"role": "user", "content": "q"}]}
        assert title_requests.is_title_request(body, {"x-pcai-purpose": "compaction"}) is False

    def test_no_headers_at_all(self):
        assert title_requests.is_title_request(_gen_body(), None) is True


class TestStoredMessageList:
    """The due-query guard holds a stored message list, not a request body."""

    def test_stored_title_row_detected(self):
        # the store holds the message list as written by the capture path
        assert title_requests.is_title_messages(_gen_body()["messages"]) is True

    def test_stored_real_conversation_kept(self):
        stored = [
            {"role": "user", "content": "Why are DeepSeek calls routed to GLM?"},
            {"role": "assistant", "content": "Because the cascade ordering..."},
        ]
        assert title_requests.is_title_messages(stored) is False


# ---------------------------------------------------------------------------
# Minimum-length floor on a distilled memory body (2026-10-17)
# ---------------------------------------------------------------------------


class TestMinDocCharsFloor:
    """A SMALL guard, not a junk filter.

    Measured on a 1,723-document store: distilled summaries come from a fixed
    4-section template, so even a summary of a contentless session runs 789+
    chars (p10 1,618; median 2,569). A length floor therefore CANNOT catch the
    title-artifact junk it was proposed for — it catches only degenerate
    stubs (a distill that returned section headers and nothing else). These
    tests pin that honest scope so nobody later "fixes" junk with a bigger
    number and silently deletes the 100-200-char one-fact cards.
    """

    @pytest.mark.asyncio
    async def test_floor_off_by_default_writes_short_doc(self, monkeypatch):
        from pcai_llm.config import settings
        from pcai_llm.core import memory_write as mw

        monkeypatch.setattr(settings, "distiller_min_doc_chars", 0, raising=False)
        calls: list[dict] = []

        async def fake_call(args):
            calls.append(args)
            return True, "ok"

        ok, _detail = await mw.write_memory(None, fake_call, "tiny", session_id="s")
        assert ok is True
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_floor_blocks_stub_and_writes_nothing(self, monkeypatch):
        from pcai_llm.config import settings
        from pcai_llm.core import memory_write as mw

        monkeypatch.setattr(settings, "distiller_min_doc_chars", 250, raising=False)
        calls: list[dict] = []

        async def fake_call(args):
            calls.append(args)
            return True, "ok"

        ok, detail = await mw.write_memory(None, fake_call, "## Gotchas", session_id="s")
        assert ok is False
        assert "floor" in detail
        assert calls == [], "a floored document must not reach the memory server"

    @pytest.mark.asyncio
    async def test_floor_does_not_trigger_the_raw_transcript_fallback(self, monkeypatch):
        """A stub is not a MODEL failure: degrading to the raw transcript would
        trade one short document for 100+ chunks (strictly worse)."""
        from pcai_llm.config import settings
        from pcai_llm.core import memory_write as mw

        monkeypatch.setattr(settings, "distiller_min_doc_chars", 250, raising=False)
        calls: list[dict] = []

        async def fake_call(args):
            calls.append(args)
            return True, "ok"

        ok, _ = await mw.write_memory(None, fake_call, "stub", session_id="s", fallback_doc="x" * 5000)
        assert ok is False
        assert calls == [], "the fallback must NOT fire for a floored document"

    @pytest.mark.asyncio
    async def test_a_250_floor_blocks_the_chunk_fragment_that_looks_like_a_title(self, monkeypatch):
        """A 250 floor is SAFE here - this pins what it actually catches.

        An earlier version of this test asserted the opposite: that this
        220-char document was a "genuine fact card at risk", and that a 250
        floor would delete 58 such cards. That was wrong, and the correction
        is worth keeping executable.

        The text below is the OPENING CHUNK of a 2,221-char memory that mm-rag
        split across two documents. It ends on a dangling colon because it is a
        truncated lead-in, not a fact - which is why it reads like a title. At
        memory level the store had only TWO documents under 250 chars, and both
        were title artifacts, so a 250 floor drops junk and nothing else.
        """
        from pcai_llm.config import settings
        from pcai_llm.core import memory_write as mw

        fragment = (
            "[MEMORY INDEX | project: sqlhandler | type: fact-card | topics: sqlhandler, sql, duckdb]"
            "  SQLhandler vs EzPresto benchmark environment on G2 cluster"
            " (pcai-se-ai-application.hst.rdlabs.hpecorp.net), discovered 2026-09-02:"
        )
        assert len(fragment) < 250
        assert fragment.rstrip().endswith(":"), "the tell: a dangling lead-in"

        calls: list[dict] = []

        async def fake_call(args):
            calls.append(args)
            return True, "ok"

        monkeypatch.setattr(settings, "distiller_min_doc_chars", 250, raising=False)
        ok, _ = await mw.write_memory(None, fake_call, fragment, session_id="s")
        assert ok is False, "a 250 floor correctly rejects the truncated fragment"
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_complete_memory_passes_a_250_floor(self, monkeypatch):
        """The memory this fragment came from is 2,221 chars and sails through.

        NOTE (2026-10-20): the floor now measures the BODY (index header
        stripped), so its unit shifted down by the header's ~85 chars. This
        document is padded long enough to clear any plausible floor; the
        boundary cases live in tests/test_memory_body_floor.py.
        """
        from pcai_llm.config import settings
        from pcai_llm.core import memory_write as mw

        whole = (
            "[MEMORY INDEX | project: sqlhandler | type: fact-card | topics: sqlhandler, sql, duckdb]"
            "  SQLhandler vs EzPresto benchmark environment on G2 cluster"
            " (pcai-se-ai-application.hst.rdlabs.hpecorp.net), discovered 2026-09-02:"
            " - Data we created: MinIO bucket test-parquet (minio.minio.svc.cluster.local)"
        ) + (" padding body text so this is a complete document." * 30)
        assert len(whole) > 250

        calls: list[dict] = []

        async def fake_call(args):
            calls.append(args)
            return True, "ok"

        monkeypatch.setattr(settings, "distiller_min_doc_chars", 250, raising=False)
        ok, _ = await mw.write_memory(None, fake_call, whole, session_id="s")
        assert ok is True
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_conservative_floor_passes_real_fact_cards(self, monkeypatch):
        """At the RECOMMENDED 100 (BODY chars) a real one-fact card still lands.

        MEASURED 2026-10-20: this card is 220 chars total but only 130 of BODY
        once its 88-char index header is excluded. That is exactly why the
        recommended floor is 100 and NOT the 150 quoted when the measurement
        counted the whole document — 150 body chars would reject this card.
        """
        from pcai_llm.config import settings
        from pcai_llm.core import memory_write as mw

        monkeypatch.setattr(settings, "distiller_min_doc_chars", 100, raising=False)
        card = (
            "[MEMORY INDEX | project: sqlhandler | type: fact-card | topics: sqlhandler, sql, duckdb]"
            "  SQLhandler vs EzPresto benchmark environment on G2 cluster"
            " (pcai-se-ai-application.hst.rdlabs.hpecorp.net), discovered 2026-09-02."
        )
        calls: list[dict] = []

        async def fake_call(args):
            calls.append(args)
            return True, "ok"

        ok, _ = await mw.write_memory(None, fake_call, card, session_id="s")
        assert ok is True
        assert len(calls) == 1
