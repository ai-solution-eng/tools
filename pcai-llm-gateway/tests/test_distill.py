"""Pins for the SCHEDULED conversations-distill verdict handling (audit A-1).

The conversation-store sweep and its canonical sibling must treat the two
PERMANENT memory-write verdicts as verdicts, not failures:

  * SUPPRESSED (near-duplicate) — the store already holds the memory this
    document duplicates, so the row is claimed exactly like a success;
  * FLOORED (body under the length floor) — nothing was written, so this is
    NOT a distilled marker; the row backs off and stays due.

Before the fix both left the row due with NO marker and NO backoff, so the
full ~25-call map-reduce was re-spent on every lease lapse (~300 s) for as
long as the row stayed due. A TRANSIENT failure must keep the old behaviour
(leave the row due, no backoff) — pinned too, so the fix cannot over-mark.

The historical home for the distill-engine tests is tests/test_distiller.py;
this file is the zone-owned pin for the 2026-11-02 fix.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

import pcai_llm.api.routes_distill as rd
from pcai_llm.core import distill_schedule as ds
from pcai_llm.core import keys as keys_mod
from pcai_llm.core import memory_write as mw

CID, KID = uuid.uuid4(), uuid.uuid4()

MSGS = [
    {"role": "user", "content": "explain the ledger please " * 8},
    {"role": "assistant", "content": "the ledger stores every row " * 8},
    {"role": "user", "content": "and the budgets? " * 8},
    {"role": "assistant", "content": "the budgets sum the ledger " * 8},
]


def _install(monkeypatch: pytest.MonkeyPatch, verdict: str) -> tuple[list[Any], list[Any]]:
    """Bind the engine's collaborators and return (marked, skipped) sinks."""
    marked: list[Any] = []
    skipped: list[Any] = []

    async def mark_distilled(cid: Any, kid: Any, seq: int, *a: Any, **k: Any) -> None:
        marked.append((cid, seq))

    async def mark_gate_skip(cid: Any, kid: Any) -> None:
        skipped.append(cid)

    class Key:
        id = KID
        user_id = uuid.uuid4()
        allowed_models: list[str] | None = None
        key_prefix = "hllm-XX"
        name = "k"

    async def load_key_record(kid: Any) -> Any:
        return Key()

    async def attached(key: Any, request_id: str) -> bool:
        return True

    async def conv_get(kid: Any, cid: Any) -> dict[str, Any]:
        return {"id": cid, "messages": MSGS, "title": "t"}

    async def max_tokens() -> int:
        return 1024

    async def map_reduce(transcript: str, llm_once: Any, **kw: Any) -> str:
        return "a distilled summary document " * 20

    async def mcp_call_for(key: Any) -> Any:
        return lambda arguments: (True, "ok")

    async def write_memory(*a: Any, **k: Any) -> tuple[bool, str]:
        # Mirror the real contract: exactly ONE verdict is appended.
        out = k.get("verdict_out")
        if out is not None:
            out.append(verdict)
        return False, verdict + " verdict"

    monkeypatch.setattr(ds, "mark_distilled", mark_distilled)
    monkeypatch.setattr(ds, "mark_gate_skip", mark_gate_skip)
    monkeypatch.setattr(keys_mod, "load_key_record", load_key_record)
    monkeypatch.setattr(rd, "memory_attached", attached)
    monkeypatch.setattr(rd.conversations, "get", conv_get)
    monkeypatch.setattr(rd.distiller_config, "max_tokens", max_tokens)
    monkeypatch.setattr(rd, "run_map_reduce", map_reduce)
    monkeypatch.setattr(rd, "_llm_once_for", lambda key, request_id: lambda s, u, m: "x")
    monkeypatch.setattr(rd, "_mcp_call_for", mcp_call_for)
    monkeypatch.setattr(mw, "write_memory", write_memory)
    return marked, skipped


def _row() -> dict[str, Any]:
    return {"conversation_id": CID, "key_id": KID, "msg_count": 4, "title": "t", "gate_failed_at": None}


def test_suppressed_verdict_claims_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A near-duplicate is PERMANENT: re-distilling produces the same
    near-copy, so the row must be claimed instead of re-spending forever."""
    marked, skipped = _install(monkeypatch, mw.VERDICT_SUPPRESSED)
    wrote = asyncio.run(rd.distill_due_conversation(_row()))
    assert wrote is False, "nothing was written, so the sweep's write counter stays honest"
    assert marked == [(CID, 4)], "the suppressed row is marked with the current message count"
    assert skipped == [], "no backoff: the marker is the stronger claim"


def test_floored_verdict_backs_off_and_never_marks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stub body is NOT a write: no distilled marker (a later sweep must
    still be able to write a real summary), but the row must leave the
    oldest-first window so the map-reduce is not re-spent every pass."""
    marked, skipped = _install(monkeypatch, mw.VERDICT_FLOORED)
    wrote = asyncio.run(rd.distill_due_conversation(_row()))
    assert wrote is False
    assert marked == [], "a body that was not written must never look distilled"
    assert skipped == [CID], "the row backs off instead of being retried every sweep"


def test_transient_failure_still_leaves_the_row_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """The original contract, kept: a FAILED write is transient — the next
    sweep retries it, so it gets neither a marker nor a backoff."""
    marked, skipped = _install(monkeypatch, mw.VERDICT_FAILED)
    wrote = asyncio.run(rd.distill_due_conversation(_row()))
    assert wrote is False
    assert marked == [] and skipped == [], "a transient RAG outage must not be claimed or suppressed"
