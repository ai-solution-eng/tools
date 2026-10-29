"""Session distillation: transcript -> structured project-summary memory.

The gateway-side, harness-independent implementation of the session-memory
distiller. Design provenance: the DSH session-memory-distiller plugin
(dsh-plugins/packages/session-memory-distiller), whose prompt, map-reduce
chunking and memory-index contract are ported here VERBATIM (prompt text is
byte-faithful; the plugin stays the reference implementation until the
harness-side copies are retired).

WHY this module is pure: distillation logic (prompt assembly, chunking,
header synthesis) is deliberately separated from every I/O concern (the LLM
call, the RAG write). The caller injects an llm_once callable, so the whole
pipeline is unit-testable without a model endpoint and the LLM path can
evolve (routing, budgets, failover) without touching the prompt contract.
The RAG write lives in core/memory_write.py; the HTTP surface in
api/routes_distill.py.

Distill output contract (the DSH plugin contract, kept for recall
compatibility - existing memories and the 2026-10-02 rebuild index this
shape):

    [MEMORY INDEX | project: <slug> | type: session-summary | topics: ...]
    ## What we did / ## Key decisions & rationale / ## Gotchas / ## How to proceed

Map-reduce: sessions reach megabytes of transcript, far beyond a
single-call context, so long transcripts are split at block boundaries into
windows, each compressed by its own call, and the combined notes are then
reduced into the final structured summary. Short transcripts take the
single-pass path unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

# Prompt version stamped into every memory metadata payload - rebuild and
# audit attribution ("which prompt produced this memory"). Bump on ANY
# prompt change.
PROMPT_VERSION = "dsh-2026-10-09"

# Ported constants (dsh session-memory-distiller). The transcript cap is
# far above anything the conversations store can hold (MAX_MSGS x
# MAX_MSG_CHARS = 6.4M chars) but the bound is kept so an inline-transcript
# caller cannot park the pipeline on unbounded input.
MAX_HISTORY_CHARS = 8_000_000
MAP_CHUNK_CHARS = 200_000
MAX_MAP_CHUNK_CHARS = 700_000
MAX_MAP_CHUNKS = 24
MAP_DISTILL_TOKENS = 700
DEFAULT_DISTILL_TOKENS = 2048

# Fallback kind when distillation fails: a raw session_history write is
# strictly better than losing the session (the DSH plugin contract).
KIND_SUMMARY = "project-summary"
KIND_FALLBACK = "session_history"


class DistillTruncatedError(Exception):
    """The distill reply hit the max_tokens cap mid-document.

    Raised by llm_once (routes_distill) when finish_reason == "length" with
    NON-empty content and settings.distiller_reject_truncated is on. Distinct
    from DistillModelFailure by DESIGN: the caller's failure handling treats
    any Exception the same way (leave the row due / no raw fallback), but
    tests and operators need the cause named, and the map stage must NEVER
    see this for an intermediate part (partial notes are legitimate
    intermediates -- only the final document must be complete).
    """


class DistillBudgetExceeded(Exception):
    """The caller's wall-clock budget lapsed mid-distill (audit D-6).

    Raised BEFORE the next model call, never mid-call, so no spend is
    abandoned half-done: the row simply stays due and the next sweep
    resumes it from scratch. Distinct from DistillUnavailable (no route at
    all) and from a model failure: nothing is wrong with the model, the
    sweep just ran out of its share of the housekeeping loop.
    """


# The four section headers the DISTILL_SYSTEM_PROMPT contract requires. A
# document missing one was cut (or degenerated); memory_body_chars cannot see
# this because a half document still clears any length floor.
REQUIRED_SECTIONS = (
    "## What we did",
    "## Key decisions",
    "## Gotchas",
    "## How to proceed",
)


def _section_headers(doc: str) -> list[str]:
    """The document's own '## ' header LINES, stripped (audit D-8).

    Markdown's own rule for an ATX heading: at most THREE leading spaces.
    Four or more is an indented code block, so a header QUOTED inside a
    fenced/indented example is not a section of this document.
    """
    headers: list[str] = []
    for line in doc.splitlines():
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if indent <= 3 and stripped.startswith("## "):
            headers.append(stripped.strip())
    return headers


def _header_matches(header: str, section: str) -> bool:
    """True when a document header line IS the required section.

    Prefix match, but on the HEADER LINE and bounded at a word break, so
    "## Key decisions & rationale" matches "## Key decisions" while a header
    that merely embeds the words ("## Notes on ## Gotchas") does not.

    COSMETIC VARIANTS ARE FORGIVEN (cross-validation XV-3): a model may
    terminate the header with a colon ("## What we did:") or wrap it in bold
    ("## **What we did**"). Both ARE the required section, and rejecting them
    raised DistillTruncatedError on a COMPLETE document — which re-ran the
    whole map-reduce on every sweep. Only these shapes are forgiven: the
    header must still be the line's opening text.
    """
    if header == section or header.startswith(section + " "):
        return True
    if not header.startswith(section):
        return False
    tail = header[len(section):].replace("*", "").strip()
    return tail == "" or tail.startswith(":")


def document_is_complete(doc: str) -> bool:
    """True when the distilled document carries all four contract sections.

    Belt-and-suspenders for the finish_reason guard: some proxies/models
    drop finish_reason (returning None or "stop" on a capped reply), and a
    document cut mid-section is still detectable structurally. Case-
    sensitive match on the section headers the prompt mandates.
    A missing-header document is not necessarily truncated (a caller-supplied
    inline transcript may legitimately produce a shorter shape), so this is
    the SECOND signal, never the only one.

    LINE-ANCHORED (audit D-8): this used to be `all(s in doc for s in ...)`,
    a plain substring test, so any PROSE that happened to mention a header
    ("...as we wrote under ## Gotchas...") satisfied it and a document with
    no real sections passed the truncation net. Only a line that OPENS with
    the header counts now.
    """
    if not isinstance(doc, str):
        return False
    headers = _section_headers(doc)
    return all(any(_header_matches(h, s) for h in headers) for s in REQUIRED_SECTIONS)


DISTILL_SYSTEM_PROMPT = (
    "You distill AI-coding-assistant session transcripts into durable memory documents for a personal knowledge base.\n"
    "The knowledge base is a PROJECT MAP: what was worked on, how to access it, and how to proceed.\n"
    "Known project slugs: multimodalrag, glm-sglang-serving, mcp-fleet, dsh-opencode-tooling, sqlhandler, benchmarks-perf, mlis-platform, pcai-llm-gateway, model-downloader, deployments, general.\n"
    "\n"
    "OUTPUT FORMAT (markdown, exactly this shape):\n"
    "First line - the memory index header, nothing before it:\n"
    "[MEMORY INDEX | project: <project-slug> | type: session-summary | topics: <comma,separated,topics>]\n"
    "\n"
    "Then these sections:\n"
    "## What we did\n"
    "Concrete work performed: features built, bugs fixed, decisions made, artifacts produced. Keep exact file paths, image digests, version numbers, URLs, and commands - they are the recall value.\n"
    "## Key decisions & rationale\n"
    "Numbered decisions with the WHY. Record user corrections of the assistant verbatim in spirit (what was wrong, what the correction established).\n"
    "## Gotchas\n"
    "Non-obvious traps encountered: symptom -> cause -> fix.\n"
    "## How to proceed\n"
    "Open threads and next steps explicitly left at the end.\n"
    "\n"
    "RULES:\n"
    "- project-slug: pick ONE from the known project slugs listed above when it fits. Cross-project sessions pick the dominant one.\n"
    "- topics: 3-8 lowercase tags for embedding retrieval.\n"
    "- Never invent. If the transcript does not establish something, omit it.\n"
    "- Omit pleasantries, transient debugging dead-ends that resolved themselves, and tool-output noise.\n"
    # SHORTER IS BETTER (operator decision 2026-10-29). The old 300-1200 word
    # target invited documents that ran past the output cap and were then
    # REJECTED whole by document_is_complete() (missing trailing sections), so
    # a rambling summary cost the memory write entirely. A tighter target keeps
    # all four sections inside the cap; verbatim facts are still required, just
    # fewer of them per section.
    "- Target 250-600 words TOTAL, roughly 60-150 words per section. A short,"
    " complete document beats a long one that runs out of room before '## How to proceed'.\n"
    "- Preserve exact commands, paths, digests, error messages, and config values"
    " verbatim - but pick the few that carry recall value, not every one seen."
)

MAP_SYSTEM_PROMPT = (
    "You are given ONE PART of a longer AI-coding-assistant session transcript.\n"
    "Compress it into dense notes for a later merge step. Preserve every durable fact:\n"
    "exact file paths, commands, config values, image digests, version numbers, error\n"
    "messages, decisions and their rationale, and user corrections.\n"
    "Drop pleasantries, resolved dead-ends, and tool-output noise.\n"
    "Write plain markdown bullets, at most 250 words. No preamble, no header, no commentary."
)

# async (system, user_text, max_tokens) -> model output text.
#
# Injected so this module stays I/O-free: the route layer binds it to the
# gateway model routing (budget-settled, endpoint-picked); tests bind a
# stub. Raises on any model failure - the caller decides fallback
# semantics.
LlmOnce = Callable[[str, str, int], Awaitable[str]]


def build_transcript(messages: list[dict[str, Any]], title: str = "") -> str:
    """Render message rows into the distiller transcript document.

    Handles BOTH store shapes: the conversations store (plain
    {role, content} text rows) AND the canonical store's full OpenAI shapes
    (content block arrays, assistant tool_calls, role=tool results) - the
    canonical store is what relayed agents (DSH et al) and, since the
    plain-chat capture, every gateway-using agent converges into. Tool
    calls render compactly (name + arg preview + result preview) so the
    prompt sees the session's ACTIONS, not just its prose.

    Mirrors the DSH plugin buildSessionHistory document grammar (###
    User / ### Assistant / ### Tool blocks, session header).
    """
    _recovered = None
    # DOUBLE-ENCODED ROW RECOVERY (pulse-check 2026-10-11): two production rows
    # arrived as a JSONB *string* (a serialized message array stored as a scalar):
    # asyncpg decodes it as one str, len(messages) counts CHARS, and iterating
    # spreads single characters - the transcript collapsed to the bare header and
    # the row starved the memory feed forever. Recover the payload; unparseable
    # data falls through to the per-element string tolerance below.
    if isinstance(messages, str):
        try:
            parsed = json.loads(messages)
            if isinstance(parsed, list):
                _recovered = parsed
        except (ValueError, TypeError):
            _recovered = [messages]
    elif isinstance(messages, list) and len(messages) > 4 and all(isinstance(m, str) for m in messages):
        # char-spread: a *str merge scattered one payload across the list.
        joined = "".join(cast("list[str]", messages))
        try:
            parsed = json.loads(joined)
            if isinstance(parsed, list):
                _recovered = parsed
        except (ValueError, TypeError):
            pass
    lines = ["# Session History", ""]
    if title:
        lines = ["# Session History - " + title, ""]
    # total = the EXACT length of the document rendered so far, separators
    # included (audit D-9: the cap is enforced against this before every
    # append, so MAX_HISTORY_CHARS is a real bound, not an after-the-fact
    # comparison).
    total = len("\n\n".join(lines))

    def _text_of(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                # type "text" is the chat-completions part; "input_text"/
                # "output_text" are the Responses-API equivalents (a row
                # captured from a Responses-native client collapsed to the
                # bare header under the old type=="text"-only filter — the
                # live pulse-check found threads stranded this way). Any dict
                # carrying a str "text" field renders — future-proof against
                # part-type vocabulary drift. Reasoning summary lists render too.
                if isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
                        continue
                    s = block.get("summary")
                    if isinstance(s, list):
                        for part in s:
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                parts.append(part["text"])
            return "\n".join(parts)
        return ""

    def _neutralize(text: str) -> str:
        """Break transcript-authored STRUCTURE (audit P1-3): a participant
        message whose body carries '### Assistant' or '[MEMORY INDEX' lines
        renders inside the distilled document as if the DISTILLER wrote it,
        and every later recall reads it back as trusted project memory.
        Block-quoting any content line that opens with the document grammar
        keeps it readable while making it inert as structure."""
        out = []
        for line in text.splitlines():
            if line.lstrip().startswith(("#", "[MEMORY INDEX")):
                out.append("> " + line)
            else:
                out.append(line)
        return "\n".join(out)

    def _tool_calls_of(msg: dict[str, Any]) -> str:
        calls = msg.get("tool_calls")
        if not isinstance(calls, list):
            return ""
        rendered: list[str] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            name = str(fn.get("name") or "tool")
            args = fn.get("arguments")
            args_text = args if isinstance(args, str) else json.dumps(args, default=str) if args is not None else ""
            rendered.append(
                f"### Tool call {name}({_neutralize(args_text)[:400]})  …[truncated]"
                if len(args_text) > 400
                else f"### Tool call {name}({_neutralize(args_text)})"
            )
        return "\n\n".join(rendered)

    for msg in _recovered if _recovered is not None else messages:
        # STR-ELEMENT TOLERANCE (pulse-check 2026-10-11): a live canonical row
        # carried 1904 STRING elements among its messages (a client submitted
        # string entries the reconcile path adopted wholesale), rendering the
        # whole transcript to the bare header and starving the memory feed.
        # A string element is a serialized message (parse it) or bare content
        # (render as an anonymous turn) - never a skip: the row must distill
        # whatever it holds.
        if isinstance(msg, str):
            parsed = None
            try:
                candidate = json.loads(msg)
                if isinstance(candidate, dict):
                    parsed = candidate
            except (ValueError, TypeError):
                parsed = None
            if parsed is not None:
                msg = parsed
            else:
                text = msg.strip()
                if text:
                    lines.append("### User\n\n" + _neutralize(text))
                continue
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        content = msg.get("content")
        block = ""
        if role in ("system", "developer"):
            text = _text_of(content)
            if text.strip():
                block = f"### System\n\n{_neutralize(text.strip())[:2000]}"
        elif role in ("user", "assistant"):
            text = _text_of(content)
            if role == "assistant":
                calls_text = _tool_calls_of(msg)
                if text.strip():
                    block = f"### Assistant\n\n{_neutralize(text.strip())}"
                if calls_text:
                    block = (block + "\n\n" if block else "") + calls_text
            elif text.strip():
                block = f"### User\n\n{_neutralize(text.strip())}"
        elif role == "tool":
            text = _text_of(content)
            if text.strip():
                block = f"### Tool result\n\n{_neutralize(text.strip())[:600]}" + (
                    "  …[truncated]" if len(text.strip()) > 600 else ""
                )
        if not block:
            continue
        # CAP CHECKED BEFORE THE APPEND (audit D-9): the old order appended the
        # block and THEN compared, so one oversized block (a 2 MB tool result)
        # pushed the render past MAX_HISTORY_CHARS by its full length and the
        # rest of the transcript was dropped without the bound ever holding.
        # The remaining room is the bound now: a block that does not fit is cut
        # to the space left, and the loop stops after it.
        if total + 2 + len(block) > MAX_HISTORY_CHARS:
            room = max(0, MAX_HISTORY_CHARS - total - 2)
            if room > 0:
                lines.append(block[:room] + "  …[truncated]")
            lines.append("\n_[history truncated]_")
            break
        lines.append(block)
        total += 2 + len(block)
    return "\n\n".join(lines)


def split_transcript(transcript: str, max_chars: int) -> list[str]:
    """Split into windows of at most max_chars, preferring block boundaries.

    Port of the plugin splitTranscript, with the two defects the 2026-10-29
    audit measured (D-7 / F5) fixed:

    * SPLIT AT HALF THE WINDOW, NOT AT THE WHOLE WINDOW. The oversized-block
      test was `len(block) > max_chars`, so a block of 0.6-1.0 windows was
      treated as a normal block and could not share a window with a peer.
    * THE OPEN WINDOW IS FILLED, NOT FLUSHED. The old loop flushed the
      pending window and emitted every hard-split piece as its OWN chunk, so
      the short remainder of every oversized block became a chunk of its own
      and the next block started from scratch. That is the measured defect:
      a 3.24M-char transcript of ~150k blocks produced 12+ chunks where 7
      suffice (audit D-7/F5 - 27 map calls for a transcript whose bound is
      24). The fill cuts each piece to the room actually left, so block tails
      and block heads share a window; only the FINAL window is partial.

    A single oversized block (a huge tool result) is still hard-split rather
    than dropped, empty windows are still filtered so the map count is
    honest, and every chunk still honours max_chars. Block ORDER is preserved
    and no character is dropped or duplicated.
    """
    if max_chars <= 0:
        return [transcript] if transcript else []
    blocks = transcript.split("\n\n")
    chunks: list[str] = []
    current = ""
    # Half a window is the split threshold: a block this big cannot share a
    # window with a peer, so it is cut into pieces that fill the open window.
    split_at = max(1, max_chars // 2)

    def _flush() -> None:
        nonlocal current
        if current != "":
            chunks.append(current)
            current = ""

    for block in blocks:
        if len(block) > split_at:
            while block:
                room = max_chars - (len(current) + 2 if current else 0)
                if room <= 0:
                    _flush()
                    continue
                piece, block = block[:room], block[room:]
                current = piece if current == "" else current + "\n\n" + piece
            continue
        if len(current) + len(block) + 2 > max_chars:
            _flush()
            current = block
        else:
            current = block if current == "" else current + "\n\n" + block
    _flush()
    return [chunk for chunk in chunks if chunk.strip()]


# The header synthesize_memory_index prepends when the model skipped one.
# Named so memory_body_chars and the tests can reference the exact bytes.
GENERIC_MEMORY_INDEX = "[MEMORY INDEX | project: general | type: session-summary | topics: session, misc]"


def synthesize_memory_index(text: str) -> str:
    """Guarantee the [MEMORY INDEX] header contract.

    The index header is the first line of the recall value (the rebuild
    indexes on it); a model that skipped it gets a generic one PREPENDED -
    the body is never rewritten (never invent content the transcript does
    not establish; the project slug degrades to general).
    """
    if text.startswith("[MEMORY INDEX"):
        return text
    return GENERIC_MEMORY_INDEX + "\n\n" + text


def memory_body_chars(doc: str) -> int:
    """Length of a memory document's CONTENT, excluding its index header.

    The counterpart to routes_distill.transcript_body_chars, and the measure
    `settings.distiller_min_doc_chars` uses. WHY it is not len(doc): the
    `[MEMORY INDEX | ...]` line is machine-generated scaffolding whose length
    varies with the project slug and topic count — a stub with a long topics
    list carries a 164-char header and clears a 150-char floor on the header
    alone, with a one-word body. Counting only the content makes the floor
    mean "how much memory is actually here", which is what an operator
    setting the number intends.

    The header is stripped ONLY when it is the document's first line, so a
    document that legitimately opens with other text keeps its full length.
    Everything AFTER the header's closing bracket on that same line is BODY:
    some documents put content there instead of starting a new line, and
    dropping the whole line would measure a real 220-char memory as 0.
    Never raises: a guard fault must never abort a write.
    """
    if not isinstance(doc, str):
        return 0
    try:
        text = doc.lstrip()
        if not text.startswith("[MEMORY INDEX"):
            return len(text.strip())
        # Cut THROUGH the closing bracket of the index header, keeping any
        # content that follows it on the same line.
        close = text.find("]")
        remainder = text[close + 1 :] if close != -1 else text
        return len(remainder.strip())
    except Exception:  # pragma: no cover - defensive: never break a write
        return len(doc.strip())


def map_window(transcript_len: int) -> int:
    """The map-stage window for a transcript of this length (chars).

    Adaptive: MAP_CHUNK_CHARS floor, grown so the WHOLE transcript is
    covered within MAX_MAP_CHUNKS calls, capped at MAX_MAP_CHUNK_CHARS.
    When even the cap cannot fit the transcript inside MAX_MAP_CHUNKS
    windows the chunk count is allowed to exceed the bound - every character
    must reach the model, so coverage beats the call bound (the splitter
    still keeps each window at the cap, which is the cost floor).
    """
    need = -(-transcript_len // max(1, MAX_MAP_CHUNKS - 1))
    return min(MAX_MAP_CHUNK_CHARS, max(MAP_CHUNK_CHARS, need))


def plan_chunks(transcript: str) -> list[str]:
    """The map-stage chunks for a transcript: window + chunk-count bound.

    map_window() bounds the CALL count for a transcript made of one huge
    block; block-boundary packing can still cost a few extra chunks (whole
    blocks do not tile a window evenly), which is how the measured 3.24M
    transcript reached 27 map calls against a bound of 24 (audit D-7/F5).
    Grow the window and re-split until the count honours MAX_MAP_CHUNKS, up
    to the documented per-call cap. If the transcript cannot fit even at the
    cap, COVERAGE WINS: every character must reach the model, so the excess
    chunks are accepted (the splitter still keeps each one at the cap, which
    is the cost floor).
    """
    window = map_window(len(transcript))
    chunks = split_transcript(transcript, window)
    for _ in range(6):
        if len(chunks) <= MAX_MAP_CHUNKS or window >= MAX_MAP_CHUNK_CHARS:
            break
        window = min(MAX_MAP_CHUNK_CHARS, (window * 5) // 4 + 1)
        chunks = split_transcript(transcript, window)
    return chunks


def _check_deadline(deadline: float | None) -> None:
    """Raise DistillBudgetExceeded once the sweep's wall-clock budget is out.

    The housekeeping sweep's per-pass budget used to be checked only BETWEEN
    rows (audit D-6), so one row's 24-30 map calls x 120 s could hold the
    shared loop for ~an hour. The engine now re-checks it before every model
    call, so the overrun is bounded by ONE call instead of one row.
    """
    if deadline is None:
        return
    import time as _time

    if _time.monotonic() > deadline:
        raise DistillBudgetExceeded(
            "distill sweep budget exhausted mid-row - deferring the rest of this transcript"
        )


async def run_map_reduce(
    transcript: str,
    llm_once: LlmOnce,
    *,
    max_distill_tokens: int = DEFAULT_DISTILL_TOKENS,
    deadline: float | None = None,
) -> str:
    """Distill one transcript into the final memory document.

    Single-pass for ordinary transcripts; adaptive-window map-reduce above
    MAP_CHUNK_CHARS (window grows so the WHOLE transcript is always covered
    within MAX_MAP_CHUNKS calls, floor MAP_CHUNK_CHARS, cap
    MAX_MAP_CHUNK_CHARS - see map_window).

    deadline (monotonic, optional): the caller's wall-clock budget. When it
    lapses mid-transcript DistillBudgetExceeded is raised BEFORE the next
    model call, so a scheduled sweep cannot be held by one long row (audit
    D-6). None = unbounded (explicit /v1/distill calls: a human is waiting).

    Raises: whatever llm_once raises (model failure) - the caller owns the
    fallback (write the raw transcript as session_history).
    """
    input_text = transcript
    if len(transcript) > MAP_CHUNK_CHARS:
        chunks = plan_chunks(transcript)
        notes: list[str] = []
        # MAP-STAGE MARKER (2026-10-23): partial notes are legitimate
        # intermediates, so a capped map call must NOT raise
        # DistillTruncatedError (routes_distill checks this attribute).
        # Set for the loop, cleared before the final reduce call.
        llm_once._distill_map_stage = True  # type: ignore[attr-defined]
        try:
            for i, chunk in enumerate(chunks):
                _check_deadline(deadline)
                note = await llm_once(
                    MAP_SYSTEM_PROMPT,
                    "Session part " + str(i + 1) + " of " + str(len(chunks)) + ":\n\n" + chunk,
                    MAP_DISTILL_TOKENS,
                )
                notes.append("### Part " + str(i + 1) + "\n" + note)
        finally:
            llm_once._distill_map_stage = False  # type: ignore[attr-defined]
        input_text = "These are sequential notes from one session, part 1 first.\n\n" + "\n\n".join(notes)

    _check_deadline(deadline)
    text = await llm_once(
        DISTILL_SYSTEM_PROMPT,
        "Distill this AI-coding-assistant session transcript:\n\n" + input_text,
        max_distill_tokens,
    )
    text = text.strip()
    if not text:
        raise ValueError("distill model produced no text")
    final_doc = synthesize_memory_index(text)
    # TRUNCATION GUARD (2026-10-23): the DSH contract document is four
    # sections; a document missing one was cut mid-write even when the
    # upstream finish_reason lied or was dropped by a proxy. Raises
    # DistillTruncatedError so the caller leaves the row due instead of
    # shipping a half document.
    if not document_is_complete(final_doc):
        raise DistillTruncatedError(
            "distilled document is missing required sections "
            + repr(REQUIRED_SECTIONS)
            + " - likely truncated mid-write; raise distiller_max_tokens or retry"
        )
    return final_doc
