"""Memory write contract: distilled document -> the key RAG memory dataset.

The gateway single write shape into the fleet RAG server (mm-rag), shared
by every harness once the distiller surface is live. Replaces the three
harness-side MCP write implementations (DSH plugin, opencode agent, OWUI
filter) with ONE credential and protocol owner.

CREDENTIAL CONTRACT (core.enroll_memory): a keyed caller carries its memory
dataset pair {X-Memory-Dataset, X-Dataset-Password} stamped into
mcp_server_creds at enrollment redemption. The MCP call is executed with
routes_mcp.resolve_mcp_headers - the SAME merge the relay and /mcp use
(server-level auth first, per-key overrides LAST-WINS) - so a key without
the pair writes unauthenticated and RAG refuses; no fallback URL, no env
credential, no second secret path (the SSRF surface enroll_memory closed
stays closed). The registry row is the ONLY source of the server URL
(settings.enroll_memory_server names it).

PROTOCOL: MCP tools/call add_memory on the registered memory server - the
same tool the harnesses call today. Metadata carries kind (project-summary
| session_history), session_id (the conversation id or a caller-supplied
session label), source, and prompt_version for rebuild/audit attribution.

I/O-FREE DISCIPLINE (mirrors core.distiller): this module owns the DECISION
shape (arguments, kinds, the session-history degrade) and takes the MCP
call as an injected callable; the routes layer binds it to _upstream_session.
Returns (ok, detail) - it never raises: memory writes are best-effort
enrichment, the same discipline as core.traces. The caller decides what
the user sees.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import settings
from .distiller import KIND_FALLBACK, KIND_SUMMARY, PROMPT_VERSION, memory_body_chars

log = logging.getLogger(__name__)

SOURCE_LABEL = "gateway:distiller"

# Bounded wait for the add_memory call: a memory write is background-
# quality enrichment, not a model-paced tool call - a hung RAG server must
# not park a distill request for the full 600s upstream timeout (the
# metadata-bound pattern the MCP catalogs use).
WRITE_TIMEOUT_S = 30.0

McpCall = Callable[[dict[str, Any]], Awaitable[tuple[bool, str]]]
"""async (add_memory_arguments) -> (ok, detail).

Injected by the caller: the routes layer binds the registered memory
server + resolve_mcp_headers merge + _upstream_session; tests bind a stub.
Raises on transport failure - caught HERE, never by the route.
"""


def build_add_memory_arguments(
    doc: str,
    session_id: str,
    source: str,
    kind: str,
) -> dict[str, Any]:
    """The add_memory tool arguments (the DSH plugin buildAddMemoryBody
    shape, minus the JSON-RPC envelope - the MCP SDK session builds that).
    """
    return {
        "text": doc,
        "metadata": {
            "kind": kind,
            "session_id": session_id,
            "source": source,
            "prompt_version": PROMPT_VERSION,
        },
    }


async def write_memory(
    key_id: uuid.UUID | None,
    mcp_call: McpCall,
    doc: str,
    *,
    session_id: str,
    kind: str = KIND_SUMMARY,
    source: str = SOURCE_LABEL,
    fallback_doc: str = "",
    fallback_session_id: str = "",
) -> tuple[bool, str]:
    """Write ONE memory document to the calling key memory dataset.

    On a failure of the summary write the caller may pass fallback_doc (the
    raw transcript): a session_history write is strictly better than losing
    the session, so the failed summary degrades to the fallback kind with
    the raw document - the DSH plugin contract, kept verbatim. The fallback
    fires on ANY summary-write failure (transport, auth refusal, upstream
    4xx) and is attempted once.

    Returns (ok, detail): detail is a short human string for logs and the
    route response - never the document itself (already in the store),
    never credential material.
    """
    if not doc or not doc.strip():
        return False, "empty document"
    # MINIMUM LENGTH FLOOR (2026-10-17). Deliberately a *small* guard, not a
    # junk filter: a distill that produced only section headers or a stub is
    # caught here, but a contentless-yet-long summary is NOT — the distill
    # template makes those 789+ chars regardless (see config
    # distiller_min_doc_chars for the measurement). Junk of that shape needs
    # semantic detection (core.title_requests), not a length gate.
    #
    # A refused write is NOT a distilled marker: the caller only marks the row
    # on ok=True, so the row stays due and retries — nothing is lost, and a
    # later sweep that produces a real summary still writes it.
    #
    # Deliberately returns BEFORE the fallback block below: a stub summary is
    # not a MODEL failure, so degrading to the raw transcript here would trade
    # one short document for 100+ transcript chunks — strictly worse.
    #
    # MEASURED ON THE BODY, NOT THE WHOLE DOCUMENT (2026-10-20). The floor used
    # to be len(doc), which COUNTED the machine-generated `[MEMORY INDEX | ...]`
    # header: a stub with a long topics list carried a 164-char header and
    # cleared a 150-char floor on the header alone, with a one-word body.
    # memory_body_chars strips the header, so the number means "how much memory
    # is actually here" — the operator's intent — and the guard is immune to
    # header length.
    min_chars = settings.distiller_min_doc_chars
    body_chars = memory_body_chars(doc)
    if min_chars > 0 and body_chars < min_chars:
        log.info(
            "memory write skipped: body is %d chars (%d with header), under the %d-char floor (key=%s)",
            body_chars,
            len(doc.strip()),
            min_chars,
            key_id,
        )
        return False, f"document body under the {min_chars}-char floor"

    try:
        ok, detail = await mcp_call(build_add_memory_arguments(doc, session_id, source, kind))
        if ok:
            return True, detail
    except Exception as exc:
        log.warning("memory write failed (key=%s): %s", key_id, type(exc).__name__, exc_info=True)
        ok, detail = False, "memory write failed: " + type(exc).__name__

    if fallback_doc and fallback_doc.strip():
        try:
            ok2, detail2 = await mcp_call(
                build_add_memory_arguments(
                    fallback_doc,
                    fallback_session_id or session_id,
                    source,
                    KIND_FALLBACK,
                )
            )
            if ok2:
                return True, "summary failed; wrote raw transcript as session_history"
            detail = detail + "; fallback: " + detail2
        except Exception as exc:
            log.warning("memory fallback write failed (key=%s)", key_id, exc_info=True)
            detail = detail + "; fallback failed: " + type(exc).__name__
    return ok, detail
