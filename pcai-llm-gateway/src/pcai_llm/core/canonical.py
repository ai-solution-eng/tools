"""Canonical conversation reconciliation (ROADMAP §7 — server-owned conversations).

Phase 1 of the inversion "the model endpoint has the conversation; the
client's machine is just where some of its tools live". Today a relay
HANDBACK (a client-side tool like a harness's bash/edit) ends the stream
with finish_reason=tool_calls, and the harness re-sends the ENTIRE
accumulated conversation as a new request (a real DSH relay turn measured
214k input tokens). This module makes the gateway the OWNER of the
conversation record without requiring any client change:

- every relayed turn is fingerprinted (thread_key) and reconciled
  (reconcile) against the stored canonical message list;
- the verdict is one of four modes — "new" (no store row yet), "extend"
  (incoming = canonical + appended tail), "reset" (incoming diverges: the
  CLIENT is authoritative on content, so we adopt it wholesale), and
  "trimmed_incoming" (the client deliberately sent LESS than canonical);
- in every mode the incoming list is what the upstream sees — the store is
  bookkeeping, never a rewrite. That is what makes the feature fail-safe
  by construction: a bug here can at worst mislabel a turn, never change
  what the model is asked.

WHY a store at all, when phase 1 always adopts incoming: (1) the
extend/reset verdict is the observability substrate for the roadmap's
"one conversation = one continuous trace" and for phase 2's result-only
continuation protocol (client sends "result of call X" + session ref
INSTEAD of the full transcript — only possible once the gateway holds the
canonical list); (2) the extend verdict PROVES the byte-stable prefix that
vLLM/SGLang APC needs (a client re-sending an identical prefix hits the
prompt cache; divergence collapses hit rates — now visible per turn).

Phase 2A rides the same substrate with detect_continuation(): the client
still re-sends the full transcript, but when the new tail is exactly
"assistant tool_calls + their role=tool results" the request is recognized
as a handback CONTINUATION and the relay loop's identity (trace linkage)
carries across the request boundary — see toolrelay.relay_stream.

DB shape lives at the edges (load/save take the db handle as an argument):
every pure decision here is unit-testable without the app, and the routes
layer stays the only place that decides WHETHER reconciliation runs
(master switch, X-PCAI-Conversation: raw escape hatch).

JSONB convention: asyncpg returns JSONB as str (no codec is registered on
the pool) and accepts json.dumps text on insert — the same convention
embedcache/budgets already use. load() tolerates both str and
already-decoded input so fakes (and a future codec) need no shim.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# Reconcile modes (reconcile()'s second return value). Kept as literals in
# the code so grep finds every decision site; documented in reconcile().
MODE_EXTEND = "extend"
MODE_RESET = "reset"
MODE_NEW = "new"
MODE_TRIMMED_INCOMING = "trimmed_incoming"


def _strip_nuls(obj: Any) -> Any:
    """Recursively drop NUL characters from every string in a message-tree.

    PG's json/jsonb parser rejects the \\u0000 escape ("unsupported Unicode
    escape sequence") — the ONE character json.dumps can emit that Postgres
    refuses. Stripping BEFORE serialization (never on the serialized text:
    a post-dumps replace would corrupt legal payloads containing literal
    backslash+'u0000' text, splicing a lone backslash onto the next char and
    creating the very 'Escape sequence "\\ " is invalid' failure it tried to
    prevent — caught live on G2). Every other character json.dumps emits is
    a legal escape Postgres accepts.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: _strip_nuls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nuls(v) for v in obj]
    return obj


def thread_key(msgs: list[dict], key_id: Any) -> str | None:
    """Stable conversation fingerprint: hash of the key and the FIRST user
    message.

    Harness loops (DSH) re-send the ACCUMULATED history on every step, so
    the first user message is the conversation's stable seed — every
    relayed request of one conversation lands on one fingerprint, and a
    long multi-turn session reads as ONE thread in the trace console and
    ONE row here instead of N disconnected per-request records. The system
    preamble is deliberately excluded (clients vary it between turns);
    list-shaped content (multimodal parts) is canonicalized through
    json.dumps. Moved here from toolrelay (which keeps a back-compat
    alias) so trace grouping and conversation reconciliation can never
    drift apart — a fingerprint that disagreed between the two would split
    one conversation across trace threads and store rows.
    """
    seed = None
    for m in msgs or []:
        if isinstance(m, dict) and m.get("role") == "user":
            seed = m.get("content")
            break
    if seed is None:
        return None
    if not isinstance(seed, str):
        try:
            seed = json.dumps(seed, sort_keys=True, default=str)
        except (TypeError, ValueError):
            seed = str(seed)
    return hashlib.sha256(f"{key_id}|{seed}".encode()).hexdigest()[:16]


def message_hash(message: Any) -> str:
    """Stable per-message hash over the message's canonical JSON
    (sort_keys; default=str for non-JSON-native values).

    WHY a hash chain instead of deep equality: the extend check only needs
    to know "is canonical a PREFIX of incoming", and comparing N short
    fixed-size digests lets the common case (identical prefix) short-circuit
    on the first mismatch without materializing/normalizing whole messages.
    sort_keys makes the hash independent of dict insertion order (the same
    message parsed twice from JSON must hash identically) — exactly the
    byte-stability the APC argument depends on.
    """
    return hashlib.sha256(json.dumps(message, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def prefix_hashes(canon: list[dict], incoming: list[dict]) -> list[int] | None:
    """Indices where the two lists' hashes DISAGREE, walking only the canonical
    prefix (perf, audit P2-3): one shared walk for reconcile + continuation
    detection instead of two. Returns None the moment a mismatch is found
    (the caller's mode is then reset, and no continuation is possible) —
    a full pass means byte-identical prefix."""
    limit = min(len(canon), len(incoming))
    for idx in range(limit):
        if message_hash(canon[idx]) != message_hash(incoming[idx]):
            return [idx]
    return None


def reconcile(
    canonical: list[dict] | None,
    incoming: list[dict],
    prefix_verdict: list[int] | None = None,
) -> tuple[list[dict], str]:
    """Reconcile the client's re-sent history against the stored canonical
    list. Returns (messages_to_use, mode).

    Mode semantics (the whole feature in four words):

    - "new" — no canonical row (or an empty/unusable one): the gateway has
      never seen this conversation; adopt incoming and persist it.
    - "extend" — canonical is a PREFIX of incoming (its hash chain matches
      incoming's first len(canonical) hashes): the client re-sent the same
      history plus the new tail. Adopting INCOMING keeps the upstream
      prompt byte-stable across cycles — the shared prefix the upstream
      already prefilled is bit-identical, so vLLM/SGLang APC turns each
      handback cycle into a mostly cache hit instead of a full re-prefill.
    - "reset" — a hash mismatch BEFORE canonical is exhausted: the client
      edited/compacted/branched its history. The client is authoritative
      on CONTENT (we never re-inject messages it chose to drop — that would
      fight its own context management); adopt incoming wholesale, persist
      it, and report the reset so callers can log/trace it.
    - "trimmed_incoming" — incoming is SHORTER than canonical: the client
      deliberately sent less (its own window eviction). Same authority
      rule as reset; checked before the hash walk because a shorter list
      can never extend a longer one.

    Phase 1 deliberately returns incoming in EVERY mode: reconciliation is
    detection + persistence, never a rewrite. Inputs are never mutated and
    the returned list is a fresh copy.
    """
    msgs = list(incoming or [])
    if not canonical:
        return msgs, MODE_NEW
    if len(msgs) < len(canonical):
        return msgs, MODE_TRIMMED_INCOMING
    mismatch = prefix_hashes(canonical, msgs) if prefix_verdict is None else prefix_verdict
    if mismatch is not None:
        return msgs, MODE_RESET
    return msgs, MODE_EXTEND


def detect_continuation(
    canonical: list[dict] | None,
    incoming: list[dict],
    prefix_verdict: list[int] | None = None,
) -> tuple[bool, dict]:
    """Is this relayed request a HANDBACK CONTINUATION of the stored
    conversation? Returns (is_continuation, info).

    Phase 2A of the §7 inversion. The client (a harness like DSH) is
    UNCHANGEABLE — after the relay hands back (finish_reason=tool_calls) it
    executes its own tools and re-sends the full accumulated history with
    the assistant tool_calls message and the role=tool results APPENDED. That
    natural shape is detectable, and detecting it is what lets the next wave
    treat the follow-up request as a continuation of the relay loop rather
    than a fresh conversation.

    A request IS a handback continuation when ALL of these hold:

    - a canonical row exists (the gateway has seen this conversation);
    - the canonical list is a byte-identical PREFIX of incoming — the same
      precondition reconcile() reports as mode "extend" (re-verified here so
      the function is self-contained; the hash walk is cheap);
    - the new tail (incoming beyond the canonical prefix) consists ONLY of
      ONE leading assistant message carrying a non-empty `tool_calls` list,
      followed by one or more role="tool" messages whose tool_call_id is one
      of those calls' ids. Anything else in the tail — a new user message, an
      assistant content-only message, a second assistant turn, a tool result
      referencing a foreign id — means the client is doing something other
      than answering the handback, and this is NOT a continuation.

    Deliberately NOT required: that every tool_call id got a result. A
    partially-answered parallel batch is still the client answering the
    handback; detection is advisory (a trace/log linkage flag), never a
    rewrite, so leniency here cannot corrupt the conversation — the same
    fail-safe posture as reconcile (a mislabel can at worst mislabel).

    Returns info {"tool_call_ids": [...], "result_count": int} on a positive
    (the ids the handback asked the client to answer, how many results came
    back) and the same keys empty on a negative. Inputs are never mutated.
    """
    empty: dict = {"tool_call_ids": [], "result_count": 0}
    msgs = incoming if isinstance(incoming, list) else []
    canon = canonical if isinstance(canonical, list) else None
    if not canon or len(msgs) <= len(canon):
        return False, empty
    mismatch = prefix_hashes(canon, msgs) if prefix_verdict is None else prefix_verdict
    if mismatch is not None:
        return False, empty
    tail = msgs[len(canon) :]
    head = tail[0]
    if not isinstance(head, dict) or head.get("role") != "assistant":
        return False, empty
    tool_calls = head.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return False, empty
    call_ids: list[str] = []
    for tc in tool_calls:
        tc_id = tc.get("id") if isinstance(tc, dict) else None
        if not tc_id:
            return False, empty
        call_ids.append(str(tc_id))
    results = tail[1:]
    if not results:
        return False, empty  # the handback was never answered
    id_set = set(call_ids)
    for m in results:
        if not isinstance(m, dict) or m.get("role") != "tool" or m.get("tool_call_id") not in id_set:
            return False, empty
    return True, {"tool_call_ids": call_ids, "result_count": len(results)}


def trim_to_budget(
    messages: list[dict],
    budget_tokens: int,
    estimate: Callable[[list[dict]], int],
) -> tuple[list[dict], int]:
    """Bound the projected message list to an estimated-token budget.

    The roadmap's "don't send too much" invariant, enforced at the one
    choke point that already decides what the model sees per call: when
    budget_tokens > 0 and estimate(messages) exceeds it, the OLDEST
    messages are dropped one at a time until the estimate fits — the model
    needs the recent turns and the newest tool results, never the oldest
    history. Deterministic by construction: same input list + estimator =>
    same output.

    Two messages are ALWAYS kept: a leading system message (index 0 — the
    preamble is the cheapest tokens on the wire and losing it changes the
    model's behavior far out of proportion) and the LAST message (dropping
    the newest turn would ask the model to answer a question it cannot
    see). When only those protected messages remain and the budget still
    does not fit, we stop and return what is left — over-budget is better
    than an unanswerable prompt. budget_tokens <= 0 means "no trimming"
    (the knob's off position) and returns the list untouched.

    The trimmed list is a PROJECTION for this call only — callers persist
    the untrimmed reconciled conversation, so trimming never rewrites
    history and the next turn's prefix-stability check is unaffected.
    Returns (trimmed_messages, dropped_count).
    """
    if budget_tokens <= 0 or not messages:
        return list(messages), 0
    trimmed = list(messages)
    dropped = 0
    while estimate(trimmed) > budget_tokens:
        first_droppable = 1 if isinstance(trimmed[0], dict) and trimmed[0].get("role") == "system" else 0
        if first_droppable > len(trimmed) - 2:
            # Nothing droppable left: only the protected preamble (if any)
            # and the last message remain. Stop rather than send a prompt
            # the model cannot act on.
            break
        trimmed.pop(first_droppable)
        dropped += 1
    return trimmed, dropped


async def load_row(db: Any, key_id: Any, thread_id: str) -> tuple[list[dict] | None, int]:
    """Fetch (messages, iterations) for (key_id, thread_id).

    'iterations' is the per-turn loop-accounting counter (2026-09-25,
    supersedes the wave-C per-conversation accounting): the relay iterations
    the CURRENT TURN's prior handback-continuation requests already spent —
    the routes layer zeroes it on any fresh user turn, so
    tool_relay_max_iterations bounds one turn, not the whole conversation
    (0 on any uncertainty — a missing/legacy/corrupt row never invents
    budget). Messages follow load()'s fail-safe shape.
    """
    row = await db.fetchrow(
        "SELECT messages, iterations FROM canonical_conversations WHERE key_id = $1 AND thread_id = $2",
        key_id,
        thread_id,
    )
    if row is None:
        return None, 0
    try:
        iters = max(0, int(row["iterations"] or 0))
    except (KeyError, TypeError, ValueError):
        # Legacy row / fake without the column: 0 (never invents budget)
        iters = 0
    return _messages_from_row(row, thread_id), iters


def _messages_from_row(row: Any, thread_id: str) -> list[dict] | None:
    """Decode the stored JSONB list (tolerates str/decoded/corrupt)."""
    raw = row["messages"]
    if isinstance(raw, str):  # asyncpg JSONB arrives as text (no codec)
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("canonical: corrupt JSONB row for thread %s — treating as absent", thread_id)
            return None
    return raw if isinstance(raw, list) else None


async def load(db: Any, key_id: Any, thread_id: str) -> list[dict] | None:
    """Fetch the stored canonical message list for (key_id, thread_id).

    Returns None when no row exists (the caller reconciles as "new") or
    when the stored payload is not a usable list (a corrupt/foreign row is
    treated as absent — fail-safe: the caller then adopts incoming, never
    crashes). DB-level failures RAISE: whether a database hiccup may pass
    through or must degrade to raw incoming is a caller-policy decision
    (the routes layer always picks the degradation), not this module's.
    """
    messages, _iterations = await load_row(db, key_id, thread_id)
    return messages


async def save_pending(db: Any, key_id: Any, thread_id: str, tail: list[dict], iterations: int) -> None:
    """Write the AWAITING-CLIENT handback state (the relay loop's appended
    tail: the assistant tool_calls message + any in-loop-executed tool
    results) plus the cross-request iteration counter — WITHOUT touching
    the canonical messages column.

    WHY a separate column: the canonical list is CLIENT-authoritative (a
    full-history re-send reconciles against it byte-for-byte; the relay's
    internal reconstruction of the assistant message need not hash-match
    the client's). The pending tail is the SERVER-side substrate the
    result-only continuation reads: the client answers with the role=tool
    results alone and the gateway stitches canonical + pending + results.
    Cleared (NULL) when the continuation is consumed.
    """
    payload = json.dumps(_strip_nuls(tail), default=str)
    await db.execute(
        """
        UPDATE canonical_conversations
           SET pending = $3, iterations = $4, updated_at = now()
         WHERE key_id = $1 AND thread_id = $2
        """,
        key_id,
        thread_id,
        payload,
        max(0, int(iterations or 0)),
    )


async def load_pending(db: Any, key_id: Any, thread_id: str) -> list[dict] | None:
    """The awaiting-client tail saved by the last handback (None = none)."""
    row = await db.fetchrow(
        "SELECT pending FROM canonical_conversations WHERE key_id = $1 AND thread_id = $2",
        key_id,
        thread_id,
    )
    if row is None or row["pending"] is None:
        return None
    raw = row["pending"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return raw if isinstance(raw, list) else None


async def save(
    db: Any,
    key_id: Any,
    thread_id: str,
    messages: list[dict],
    iterations: int = 0,
) -> None:
    """Upsert the canonical message list for (key_id, thread_id).

    One row per (key, conversation) — last-writer-wins, which is exactly
    the reconciliation contract: every mode adopts the incoming list, so
    the newest successfully reconciled turn IS the canonical state. The
    upsert (not delete+insert) keeps the write idempotent under retries.

    NUL bytes are stripped before serialization: a JSON string may carry
    the \\u0000 escape, but Postgres text/jsonb REJECTS it ("unsupported
    Unicode escape sequence") — one binary tool result (an image/PDF magic,
    a raw-bytes echo) used to poison the save, and because the save then
    never advanced, EVERY subsequent turn of that thread re-failed (the
    observed pg ERROR spam) while the store silently froze at its last good
    state. Stripping at the PYTHON-STRING boundary (never on the serialized
    text — see _strip_nuls) keeps the store unpoisonable regardless of what
    any tool returns; the model-facing wire path is untouched. Every
    non-NUL payload json.dumps produces inserts into jsonb verbatim
    (verified against PG 16 + asyncpg 0.31: backslash-space, backslash-pipe,
    literal \\u0000 text all parse fine).
    """
    payload = json.dumps(_strip_nuls(messages), default=str)
    await db.execute(
        """
        INSERT INTO canonical_conversations (key_id, thread_id, messages, iterations, updated_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (key_id, thread_id)
        DO UPDATE SET messages = $3, iterations = $4, updated_at = now()
        """,
        key_id,
        thread_id,
        payload,
        max(0, int(iterations or 0)),
    )
