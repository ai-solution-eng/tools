"""Upstream wire compatibility: request-body rewrites every backend accepts.

WHY THIS MODULE EXISTS (2026-10-29). OpenAI added the `developer` role as a
higher-priority `system`. SGLang's Qwen templates know neither form of that
tolerance, and they reject it twice, in two different ways:

    role: developer            -> 400 "Unexpected message role."
    two system messages        -> 400 "System message must be at the beginning."

Both were measured live, and both cost real requests: the first killed a
session-title call (which is how "(model error 400)" became a session name),
and the second killed every routed turn whose history carried a developer
event, because the naive fix — rewrite `developer` to `system` in place —
produces `[system, system, user]`, which is exactly the second shape.

WHY THE GATEWAY AND NOT THE CLIENT. A harness can only avoid the role when it
knows which upstream will serve the request — and behind `routing` it does not.
The gateway is the one component that knows the model it is about to call, and
a SINGLE leading system message is accepted by every backend this proxy fronts
(vLLM, SGLang, and OpenAI's own API alike). Folding system-level instructions
into one block is lossless for them: `developer` means "system, higher
priority", and none of these backends ranks the two differently.

CONTRACT (hot path): PURE, never raises, and ZERO-COPY for the shapes that are
already valid — the common case is one leading `system` message, which is
returned untouched after a single pass over the message list.
"""

from __future__ import annotations

from typing import Any

# Roles that carry system-level instructions upstream.
_SYSTEM_ROLES = ("system", "developer")

# How merged instruction blocks are separated. A blank line keeps them legible
# to the model and to anyone reading a captured body.
SEPARATOR = chr(10) + chr(10)


def _text_of(message: dict[str, Any]) -> str:
    """Flatten one message's content to text. PURE, never raises.

    A system-level message is instructions, so non-text parts (an image in a
    system turn, which no client this proxy serves emits) contribute nothing
    rather than being re-emitted into a shape the upstream may reject.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return SEPARATOR.join(parts)


def normalize_system_messages(body: dict[str, Any]) -> dict[str, Any]:
    """Fold every `system`/`developer` message into ONE leading system message.

    Returns the SAME object when the body is already valid (no system-level
    message, or exactly one and it leads), and a NEW dict otherwise; the
    caller's body is never mutated, because the ledger settle and the admission
    estimate read it afterwards.

    Order is preserved within the merged block, so a developer event that
    arrived mid-conversation still precedes the instructions that followed it.
    Other roles keep their relative order and position after the leading block.
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return body

    system_idx = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") in _SYSTEM_ROLES]
    if not system_idx:
        return body
    # Already the one shape every upstream accepts: a single leading system.
    if system_idx == [0] and messages[0].get("role") == "system":
        return body

    merged = SEPARATOR.join(filter(None, (_text_of(messages[i]) for i in system_idx)))
    rest = [m for i, m in enumerate(messages) if i not in set(system_idx)]
    rewritten: list[Any] = [{"role": "system", "content": merged}] if merged else []
    rewritten.extend(rest)
    return {**body, "messages": rewritten}
