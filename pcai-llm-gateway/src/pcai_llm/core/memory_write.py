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

# WRITE VERDICTS (audit D-2). write_memory answers (ok, detail); a caller that
# has to act differently on a SUPPRESSED write than on a FAILED one (the
# distiller sweep: a near-duplicate is a permanent condition, so the row must
# be marked instead of re-running a 25-call map-reduce every pass forever)
# passes verdict_out and reads these back. Kept out of the return tuple on
# purpose: the tuple is the long-standing (ok, detail) contract every caller
# and test already speaks, and widening it would break all of them.
VERDICT_SUPPRESSED = "suppressed"
VERDICT_FLOORED = "floored"
VERDICT_WRITTEN = "written"
VERDICT_FAILED = "failed"

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
    mcp_search: McpCall | None = None,
    verdict_out: list[str] | None = None,
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

    verdict_out (optional): when passed, exactly one of VERDICT_WRITTEN /
    VERDICT_FLOORED / VERDICT_SUPPRESSED / VERDICT_FAILED is appended, so a
    caller can tell a PERMANENT refusal (a near-duplicate that will be
    produced again by every retry) from a TRANSIENT failure (which must leave
    the row due). The (ok, detail) tuple is unchanged.
    """

    def _verdict(v: str) -> None:
        if verdict_out is not None:
            verdict_out.append(v)

    if not doc or not doc.strip():
        _verdict(VERDICT_FAILED)
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
        _verdict(VERDICT_FLOORED)
        return False, f"document body under the {min_chars}-char floor"

    # NEAR-DUPLICATE SUPPRESSION (2026-10-23; see config
    # distiller_dedup_superseded for the full rationale). Content-based:
    # search with the new document's own body, then token-containment
    # against each hit. Cross-session near-dup -> suppressed; same-session
    # near-dup -> allowed (the designed re-distill update). Every failure
    # mode writes through: a broken dedupe probe must never block a memory.
    if mcp_search is not None and kind == KIND_SUMMARY and session_id:
        try:
            verdict = await _near_duplicate_verdict(mcp_search, doc, session_id)
        except Exception as exc:
            log.warning(
                "memory dedup probe failed (key=%s, session=%s): %s - writing anyway",
                key_id, session_id[:16], type(exc).__name__,
            )
            verdict = None
        if verdict is not None:
            hit_session, score = verdict
            if hit_session != session_id:
                log.info(
                    "memory dedup: suppressed near-duplicate write "
                    "(session=%s matches prior session=%s, containment=%.2f)",
                    session_id[:16], hit_session[:16], score,
                )
                _verdict(VERDICT_SUPPRESSED)
                return False, (
                    "near-duplicate of an existing memory from session "
                    + hit_session[:16]
                    + f" (containment {score:.2f}); write suppressed"
                )

    try:
        ok, detail = await mcp_call(build_add_memory_arguments(doc, session_id, source, kind))
        if ok:
            _verdict(VERDICT_WRITTEN)
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
                _verdict(VERDICT_WRITTEN)
                return True, "summary failed; wrote raw transcript as session_history"
            detail = detail + "; fallback: " + detail2
        except Exception as exc:
            log.warning("memory fallback write failed (key=%s)", key_id, exc_info=True)
            detail = detail + "; fallback failed: " + type(exc).__name__
    _verdict(VERDICT_WRITTEN if ok else VERDICT_FAILED)
    return ok, detail



async def _near_duplicate_verdict(
    mcp_search: McpCall,
    doc: str,
    session_id: str,
) -> tuple[str, float] | None:
    """(prior_session_id, containment) for the closest stored near-dup, or None.

    Probe: search_memory with the new document's own body as the query (a
    paraphrase of the same session ranks top-1 for its own text). Hits are
    filtered to project-summary metadata and scored with token containment.
    Uses ONLY the real mm-rag MCP surface; search results carry text and
    metadata but NO point id (documented limitation), so this guard can
    suppress but never delete -- the delete-half of "replaces the picture"
    needs mm-rag to expose memory_id on search results (follow-up).
    """
    import json as _json

    from .distiller import memory_body_chars

    body = memory_body_chars(doc)
    query = " ".join(doc.split())[-600:] if body < 200 else " ".join(doc.split())[:600]
    ok, detail = await mcp_search({"query": query, "top_k": 5})
    if not ok or not isinstance(detail, str) or not detail.strip():
        return None
    try:
        payload = _json.loads(detail)
    except Exception:
        return None
    rows: list[Any] = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("results"), list):
        rows = payload["results"]
    best: tuple[str, float] | None = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        kind = str(r.get("memory_kind") or r.get("kind") or "")
        if kind and kind != KIND_SUMMARY:
            continue
        hit_session = str(r.get("session_id") or "")
        if not hit_session or hit_session == session_id:
            continue  # same-session handled by the re-distill lifecycle
        hit_text = str(r.get("text") or r.get("content") or "")
        if not hit_text.strip():
            continue
        score = _token_containment(doc, hit_text)
        if best is None or score > best[1]:
            best = (hit_session, score)
    if best is None:
        return None
    from ..config import settings

    if best[1] < settings.distiller_dedup_containment:
        return None
    return best


def _token_containment(new_doc: str, hit_text: str) -> float:
    """|A ∩ B| / min(|A|, |B|) over lowercase word tokens of the two bodies.

    Containment (not Jaccard) because mm-rag CHUNKS long documents: a hit
    may be one chunk of the prior document, a strict subset -- containment
    still reads ~1.0 where Jaccard would read |chunk|/|doc| and miss the
    duplicate. Both sides drop the machine-generated index header first.
    """
    import re

    def toks(s: str) -> set[str]:
        # Body TEXT tokens: strip the machine-generated index header exactly
        # the way core.distiller.memory_body_chars does, but keep the string
        # (that helper returns a LENGTH -- calling .lower() on it was the
        # AttributeError mypy caught pre-merge).
        text = s.lstrip()
        if text.startswith("[MEMORY INDEX"):
            close = text.find("]")
            text = text[close + 1:] if close != -1 else text
        return set(re.findall(r"[a-z0-9_.:/-]+", text.lower()))

    a, b = toks(new_doc), toks(hit_text)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))
