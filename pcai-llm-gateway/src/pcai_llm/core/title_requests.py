"""Session-title request detection (2026-10-17).

WHY THIS MODULE EXISTS

Harnesses fire an auxiliary "session title" model call — DSH does it after
EVERY human prompt (the all-prompts cadence). Each such call is an ordinary
`POST /v1/chat/completions` whose body is a single user message framing the
conversation as JSON:

    Generate the session title from this JSON array of human messages:
    [{"role":"user","content":"..."}]

    Update the session title from this JSON object:
    {"currentTitle":"...","messages":[...]}

Those calls are REAL model requests (they must be served, gated and billed),
but they are NOT conversations. Before this module the gateway's canonical
capture treated each one as a conversation and wrote a canonical row; the
distiller sweep then distilled each row into the user's memory dataset,
producing "session summaries" whose entire content was a title string
("Deepseek calls misrouted to GLM") or the sentence "Session consisted
solely of a title-generation request". That is pure noise in a store whose
contract is a PROJECT MAP, and it cost real model spend per prompt.

The fix is therefore capture-side exclusion, not a prompt change: a title
request is a legitimate request that must simply never become a memory.

TWO INDEPENDENT SIGNALS (defense in depth)

1. An explicit purpose header — authoritative when present. The harness LLM
   core models this as `GenerateOptions.purpose = 'session-title'`, but the
   pi-ai adapter does NOT yet map it onto the wire (verified 2026-10-17:
   `grep purpose packages/llm/llm-pi-ai/src` is empty), so today no client
   actually sends it. The header path is implemented anyway: it is the
   precise, future-proof signal, and it costs nothing to honor.

2. A body fingerprint — the signal that works TODAY. The framings are the
   fixed instruction strings shipped by the harness's title provider.

FALSE-POSITIVE SAFETY (the reason for the structural check)

A fingerprint match on text alone would let a user who PASTES one of those
framings into a chat have their real conversation dropped from memory. So
the framing is never sufficient on its own: the request must ALSO look like
a title call structurally — exactly ONE message, role user, and a payload
after the framing that parses as JSON (an array of message objects, or an
object carrying messages/currentTitle). A real conversation has an assistant
turn or a non-JSON tail and is therefore never matched.

WHY THE CALLERS TREAT A MATCH AS A HARD SKIP

Canonical capture is best-effort bookkeeping (see routes_proxy._capture_*):
a fault there must never break a turn. Title exclusion inherits exactly that
posture — it is a clean no-op, never an error. It is applied at EVERY store
that feeds a distill path (canonical capture, relay reconcile, the
conversations store, and both due-queries), because a single missed store is
enough to keep writing title memories.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

# The fixed instruction framings shipped by the harness title providers.
# Matched case-insensitively against the START of the (stripped) user text.
# Kept as a module tuple so they are greppable and directly testable.
TITLE_FRAMING_PREFIXES: tuple[str, ...] = (
    "Generate the session title from this JSON array of human messages:",
    "Update the session title from this JSON object:",
)

# The explicit purpose header. Header names are case-insensitive at the
# ASGI layer, but a raw dict lookup is not, so callers pass the mapping and
# we scan case-insensitively (cheap: the header set is tiny).
PURPOSE_HEADER = "x-pcai-purpose"
TITLE_PURPOSE = "session-title"


def _header_get(headers: Any, name: str) -> str:
    """Case-insensitive header lookup tolerant of Starlette Headers, a plain
    dict, or None. Returns "" when absent."""
    if not headers:
        return ""
    try:
        # Starlette's Headers already lowercases its lookup key.
        value = headers.get(name)
        if value is not None:
            return str(value)
    except Exception:
        pass
    try:
        for key, value in headers.items():
            if str(key).lower() == name:
                return str(value)
    except Exception:
        pass
    return ""


def has_title_purpose_header(headers: Any) -> bool:
    """True when the request explicitly declares itself a title call.

    Tolerates a comma-joined value (`session-title,compaction`) so a client
    that ever packs several purposes into one header still matches.
    """
    raw = _header_get(headers, PURPOSE_HEADER).strip().lower()
    if not raw:
        return False
    return any(part.strip() == TITLE_PURPOSE for part in raw.split(","))


def _message_text(message: Any) -> str:
    """The text of one message, tolerating the OpenAI shapes.

    Mirrors the tolerance in core.distiller.build_transcript: content may be
    a plain string or a list of parts carrying "text"/"input_text"/
    "output_text". A title call is always a plain string today, but the
    gateway accepts both shapes, so the detector must not be narrower than
    the thing it is classifying.
    """
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _parses_as_title_payload(tail: str) -> bool:
    """Does the text AFTER the framing parse as the title provider's payload?

    Accepts the two shipped shapes:
      * a JSON array of message objects  (first-prompt generation)
      * a JSON object with messages and/or currentTitle  (revision)
    Anything else (including an empty tail, or prose a user typed after the
    framing) is NOT a title payload.
    """
    tail = tail.strip()
    if not tail:
        return False
    try:
        parsed = json.loads(tail)
    except (ValueError, TypeError):
        return False
    if isinstance(parsed, list):
        return all(isinstance(item, dict) for item in parsed)
    if isinstance(parsed, dict):
        return any(key in parsed for key in ("messages", "currentTitle"))
    return False


def matches_title_framing(body: Any) -> bool:
    """Body-only fingerprint. See the module docstring for the safety rules."""
    if not isinstance(body, dict):
        return False
    messages = body.get("messages")
    # Structural precondition: a title call is a SINGLE user message. A real
    # conversation has an assistant turn (or at least more messages), which
    # is what keeps a pasted framing from suppressing genuine memories.
    if not isinstance(messages, list) or len(messages) != 1:
        return False
    message = messages[0]
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    text = _message_text(message).lstrip()
    if not text:
        return False
    lowered = text.lower()
    for prefix in TITLE_FRAMING_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return _parses_as_title_payload(text[len(prefix) :])
    return False


def is_title_request(body: Any, headers: Any = None) -> bool:
    """The one predicate every capture site and due-query must share.

    Header first (authoritative, cheap); the body fingerprint second (the
    signal that actually fires today). Returns False on any malformed input:
    an unclassifiable request is treated as a normal conversation, because
    the cost of dropping a real conversation from memory is far higher than
    the cost of one stray title memory.
    """
    try:
        if has_title_purpose_header(headers):
            return True
        return matches_title_framing(body)
    except Exception:  # pragma: no cover - defensive: never break a capture
        log.debug("title-request detection failed; treating as a conversation", exc_info=True)
        return False


def _user_text(message: Any) -> str:
    """The stripped text of one message, or "" when it is not a user turn."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    return _message_text(message).lstrip()


def _is_title_framed_message(message: Any) -> bool:
    """Is THIS single message a title-call framing with a real payload?"""
    text = _user_text(message)
    if not text:
        return False
    lowered = text.lower()
    for prefix in TITLE_FRAMING_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return _parses_as_title_payload(text[len(prefix) :])
    return False


def is_title_messages(messages: Any) -> bool:
    """Is a STORED message list nothing but accumulated session-title calls?

    The due-queries hold a stored row, not a request body, and a stored row
    is not the single-message shape a live title call has. Harness loops
    (DSH) resend accumulated history, so a thread that saw N title calls
    stores up to 2N messages: [user framing, assistant title] repeated.
    Requiring exactly one message (the request-body rule) therefore MISSED
    every accumulated row - and such a row also clears the distiller's
    `len(messages) < 4` one-shot filter, so it was distilled into a
    title-only stub memory (2026-10-20 leak).

    ROW-LEVEL RULE: every USER message in the row must be a title framing
    with a valid payload. Assistant turns are allowed between them - they
    are the title outputs the harness recorded. That keeps the
    false-positive safety of the single-message rule: a real conversation
    contains at least one user message that is NOT a title framing, and a
    user who pastes a framing and then keeps talking is never matched.
    """
    if not isinstance(messages, list) or not messages:
        return False
    user_turns = 0
    for message in messages:
        if not isinstance(message, dict):
            return False
        role = message.get("role")
        if role == "user":
            user_turns += 1
            if not _is_title_framed_message(message):
                return False
        elif role != "assistant":
            # A tool result / system turn means this row carries real work.
            return False
    return user_turns > 0
