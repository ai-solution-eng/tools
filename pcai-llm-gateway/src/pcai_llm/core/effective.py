"""Effective-prompt capture (ROADMAP §7 phase 2 wave C): what the model ACTUALLY saw.

THE TRANSPARENCY PROBLEM (Andrew): the relay path now transforms what the
upstream model receives — the inbound reasoning strip (0.8.1: thinking is
per-turn scratch, streamed out for review, never sent back in), canonical
conversation reconciliation, and the optional trim-to-budget context bound
(0.8.2). When debugging a bad answer, "the conversation the user reads is
not the conversation the model received": the client's transcript and the
wire prompt diverge, and the divergence is exactly where bugs hide.

This module is the capture half of wave C: a PURE, bounded projection of
one model call's effective prompt — the final `msgs` list at the capture
choke point (post-strip, post-reconciliation, pre-upstream) — plus a
bounded summary of the offered tools. Everything here is deliberately
bounded, because a transparency artifact that can rival the prompt itself
in size is just a second transcript store:

- per-message: canonical JSON (sort_keys=True, default=str), hard-truncated
  at `effective_prompt_message_cap` chars with an explicit "…[truncated N
  chars]" marker (the truncation is always honest about what it cut);
- rolling tail window: only the LAST `effective_prompt_tail_messages`
  messages are kept; omitted history is counted by a "_meta" entry, never
  silently dropped;
- whole-payload cap: the assembled JSON is hard-sliced at
  `effective_prompt_total_cap` chars with an end marker;
- tools: NEVER full schemas — one summary line per tool (name, description
  front, schema byte count). Schemas are the densest, least readable part
  of a wire prompt; their SIZE is the debug signal, not their text.

The sibling `record()` persists one row per REAL model call via the DB
handle passed in — strictly observational, best-effort: ANY failure logs a
warning and moves on, never touching the call that produced the prompt.
The caller (toolrelay) invokes it AFTER the upstream call completes, so
capture cannot even delay the wire: a broken capture pipeline is a silent
absence in the admin view, never a broken relay turn.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

# The explicit truncation markers (…) — grep-able, so a reader of a captured
# row can tell an honest bound from a complete payload at a glance.
MSG_TRUNC_SUFFIX = "…[truncated {n} chars]"
TOTAL_TRUNC_SUFFIX = " …[effective-prompt preview truncated, {n} chars total]"
META_EARLIER = "…[{n} earlier messages omitted]"

# Opportunistic prune piggyback: at most once per this interval process-wide
# (module-level timestamp below). The capture write is the heartbeat — a
# busy relay prunes every minute, an idle gateway never wakes for it.
_PRUNE_EVERY_S = 60.0
_last_prune = 0.0


def _canon(value: Any) -> str:
    """Canonical JSON text for a message/payload: sort_keys makes the
    preview independent of dict insertion order (the same message parsed
    twice — from the client's JSON and from the store's — previews
    identically), default=str survives non-JSON-native values instead of
    raising. Same serialization discipline as canonical.message_hash."""
    return json.dumps(value, sort_keys=True, default=str)


def _bounded(text: str, cap: int) -> str:
    """One truncation rule, applied consistently: pass through under the
    cap; over it, slice and APPEND the explicit marker with the true cut
    count — the reader must always be able to tell how much was dropped."""
    if len(text) <= cap:
        return text
    return text[:cap] + MSG_TRUNC_SUFFIX.format(n=len(text) - cap)


def preview_messages(
    msgs: list[dict],
    tools: list[dict] | None,
    message_cap: int | None = None,
    tail_messages: int | None = None,
    total_cap: int | None = None,
) -> dict[str, str]:
    """The bounded effective-prompt preview for ONE model call.

    Returns {"messages": <json text>, "tools": <json text>} — the exact
    shapes the effective_prompts row stores. Pure function: never mutates
    `msgs`/`tools`, and the input list is the caller's OWN copy of what
    goes upstream, so previewing cannot perturb the wire.

    Boundedness rules (every cap from config, with knobs so an operator can
    trade row size for fidelity):
    - per message: {"role": role, "preview": canonical JSON truncated to
      `message_cap` chars with an explicit marker};
    - rolling window: only the LAST `tail_messages` messages; when any were
      omitted, a leading {"role": "_meta", "preview": …} entry counts them —
      the preview states its own incompleteness;
    - whole payload: the assembled JSON hard-slices at `total_cap` with an
      end marker — worst-case row size is a constant.
    - tools: [{"name", "description": front 200 chars, "schema_bytes"}] when
      non-empty; empty string otherwise (no tool noise on toolless calls).
    """
    message_cap = settings.effective_prompt_message_cap if message_cap is None else message_cap
    tail_messages = settings.effective_prompt_tail_messages if tail_messages is None else tail_messages
    total_cap = settings.effective_prompt_total_cap if total_cap is None else total_cap

    entries: list[dict[str, str]] = []
    src = list(msgs or [])
    if tail_messages > 0 and len(src) > tail_messages:
        omitted = len(src) - tail_messages
        entries.append({"role": "_meta", "preview": META_EARLIER.format(n=omitted)})
        src = src[-tail_messages:]
    for m in src:
        role = m.get("role", "") if isinstance(m, dict) else ""
        entries.append({"role": str(role), "preview": _bounded(_canon(m), message_cap)})

    messages_json = _canon(entries)
    if total_cap > 0 and len(messages_json) > total_cap:
        messages_json = messages_json[:total_cap] + TOTAL_TRUNC_SUFFIX.format(n=len(messages_json))

    tools_json = ""
    if tools:
        summary = [
            {
                "name": t.get("function", {}).get("name", ""),
                "description": _bounded(str(t.get("function", {}).get("description", "")), 200),
                "schema_bytes": len(_canon(t)),
            }
            for t in tools
        ]
        tools_json = _canon(summary)
    return {"messages": messages_json, "tools": tools_json}


def _prune_sql() -> str | None:
    """The retention DELETE, or None when retention is off (0 = keep
    forever). Separated so the caller's fake-DB tests can assert on the
    exact SQL + interval."""
    retain_s = settings.effective_prompt_retain_s
    if retain_s <= 0:
        return None
    return "DELETE FROM effective_prompts WHERE created_at < now() - make_interval(secs => $1)"


async def record(
    key_id: Any,
    thread_id: str | None,
    request_id: str,
    iteration: int,
    attempt: int,
    model: str,
    msgs: list[dict],
    tools: list[dict] | None,
) -> None:
    """Persist ONE captured model call. BEST-EFFORT, strictly observational:
    never raises — a failed capture is a warning in the log and a gap in the
    admin view, never an effect on the relay turn (the capture row is the
    debug artifact; the model call is the product).

    The module-level `db` handle is read at call time (the traces.record
    pattern — tests monkeypatch `pcai_llm.core.effective.db`; callers never
    thread a handle).

    `attempt` (1-based) distinguishes a bounded-call TIMEOUT retry from its
    first attempt — every real upstream call is captured, including retries
    and the wrap-up pass; aborted-never-started attempts have no caller and
    therefore no row.
    """
    global _last_prune
    if not settings.effective_prompt_capture:
        return
    try:
        preview = preview_messages(msgs, tools)
        await db.execute(
            """
            INSERT INTO effective_prompts
                (key_id, thread_id, request_id, iteration, attempt, model, messages, tools)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            key_id,
            thread_id,
            request_id,
            int(iteration),
            int(attempt),
            model,
            preview["messages"],
            preview["tools"],
        )
    except Exception:
        log.warning(
            "effective-prompt capture write failed (request_id=%s iteration=%s) — continuing",
            request_id,
            iteration,
            exc_info=True,
        )
        return
    # Retention prune: piggybacked on the successful capture write, at most
    # once per _PRUNE_EVERY_S process-wide (monotonic clock, immune to wall
    # clock jumps). Prune errors never surface beyond a warning — losing a
    # prune cycle only delays the cleanup.
    prune_sql = _prune_sql()
    if prune_sql is None:
        return
    now = time.monotonic()
    if now - _last_prune < _PRUNE_EVERY_S:
        return
    _last_prune = now
    try:
        await db.execute(prune_sql, settings.effective_prompt_retain_s)
    except Exception:
        log.warning("effective_prompts retention prune failed — continuing", exc_info=True)


async def recent(
    key_id: Any,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest-first captured rows for the admin read surface.

    key_id is mandatory (rows are keyed per key; the admin surface filters
    by key and optionally thread). Non-positive limits are clamped to 1 by
    the route layer — this function trusts its caller's clamp, mirroring
    traces.recent's posture.
    """
    if thread_id is not None:
        rows = await db.fetch(
            """
            SELECT id, key_id, thread_id, request_id, iteration, attempt, model,
                   messages, tools, created_at
            FROM effective_prompts
            WHERE key_id = $1 AND thread_id = $2
            ORDER BY created_at DESC, id DESC
            LIMIT $3
            """,
            key_id,
            thread_id,
            limit,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, key_id, thread_id, request_id, iteration, attempt, model,
                   messages, tools, created_at
            FROM effective_prompts
            WHERE key_id = $1
            ORDER BY created_at DESC, id DESC
            LIMIT $2
            """,
            key_id,
            limit,
        )
    return [dict(r) for r in rows]
