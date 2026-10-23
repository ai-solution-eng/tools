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
    "- Target 300-1200 words. Preserve exact commands, paths, digests, error messages, and config values verbatim."
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
    total = sum(len(line) for line in lines)

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
        lines.append(block)
        total += len(block)
        if total > MAX_HISTORY_CHARS:
            lines.append("\n_[history truncated]_")
            break
    return "\n\n".join(lines)


def split_transcript(transcript: str, max_chars: int) -> list[str]:
    """Split into windows of at most max_chars, preferring block boundaries.

    Direct port of the plugin splitTranscript: a single oversized block (a
    huge tool result) is hard-split rather than dropped; empty windows are
    filtered so the map count is honest.
    """
    blocks = transcript.split("\n\n")
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > max_chars:
            if current != "":
                chunks.append(current)
                current = ""
            for i in range(0, len(block), max_chars):
                chunks.append(block[i : i + max_chars])
            continue
        if len(current) + len(block) + 2 > max_chars:
            chunks.append(current)
            current = block
        else:
            current = block if current == "" else current + "\n\n" + block
    if current != "":
        chunks.append(current)
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


async def run_map_reduce(
    transcript: str,
    llm_once: LlmOnce,
    *,
    max_distill_tokens: int = DEFAULT_DISTILL_TOKENS,
) -> str:
    """Distill one transcript into the final memory document.

    Single-pass for ordinary transcripts; adaptive-window map-reduce above
    MAP_CHUNK_CHARS (window grows so the WHOLE transcript is always covered
    within MAX_MAP_CHUNKS calls, floor MAP_CHUNK_CHARS - the plugin
    adaptive discipline, kept verbatim including the (MAX_MAP_CHUNKS - 1)
    margin for block-boundary slack).

    Raises: whatever llm_once raises (model failure) - the caller owns the
    fallback (write the raw transcript as session_history).
    """
    input_text = transcript
    if len(transcript) > MAP_CHUNK_CHARS:
        window = min(
            MAX_MAP_CHUNK_CHARS,
            max(MAP_CHUNK_CHARS, -(-len(transcript) // (MAX_MAP_CHUNKS - 1))),
        )
        chunks = split_transcript(transcript, window)
        notes: list[str] = []
        for i, chunk in enumerate(chunks):
            note = await llm_once(
                MAP_SYSTEM_PROMPT,
                "Session part " + str(i + 1) + " of " + str(len(chunks)) + ":\n\n" + chunk,
                MAP_DISTILL_TOKENS,
            )
            notes.append("### Part " + str(i + 1) + "\n" + note)
        input_text = "These are sequential notes from one session, part 1 first.\n\n" + "\n\n".join(notes)

    text = await llm_once(
        DISTILL_SYSTEM_PROMPT,
        "Distill this AI-coding-assistant session transcript:\n\n" + input_text,
        max_distill_tokens,
    )
    text = text.strip()
    if not text:
        raise ValueError("distill model produced no text")
    return synthesize_memory_index(text)
