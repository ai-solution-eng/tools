"""Canonical conversation reconciliation (ROADMAP §7 phase 1).

Covers, in order:
- the pure semantics: reconcile's four modes, trim_to_budget's determinism
  and its two protected messages, thread_key parity with the toolrelay
  alias it replaced;
- the DB edges (load/save) against a fake that mimics asyncpg's JSONB
  convention (text in, text out);
- the relay wiring through the FULL route: byte-stable extend across a
  handback cycle, reset / escape-hatch / fail-safe behavior observed at the
  upstream wire (what the mock upstream actually received), trimming, and
  the inert feature switch.

No live Postgres — the suite's fake-db style throughout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_mcp, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import canonical, toolrelay
from pcai_llm.routing.router import Endpoint, Router

MODEL = "relay-model"
GRANTS = {("logsearch", "search_logs")}


# ---------------------------------------------------------------------------
# 1. reconcile(): the four modes
# ---------------------------------------------------------------------------


def _turn1() -> list[dict]:
    """A handback-shaped turn: the assistant asked for a CLIENT tool."""
    return [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "run the check"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
            ],
        },
    ]


def test_reconcile_new_adopts_incoming() -> None:
    """No canonical row (first turn of a conversation, or a corrupt row the
    loader treated as absent) -> 'new': adopt incoming, persist."""
    incoming = _turn1()
    msgs, mode = canonical.reconcile(None, incoming)
    assert mode == "new"
    assert msgs == incoming
    msgs, mode = canonical.reconcile([], incoming)
    assert mode == "new" and msgs == incoming


def test_reconcile_extend_on_prefix() -> None:
    """The harness re-sent canonical + the new tail -> 'extend': the upstream
    prompt stays byte-stable for the shared prefix (APC)."""
    canonical_msgs = _turn1()
    incoming = [*canonical_msgs, {"role": "tool", "tool_call_id": "c1", "content": "file.txt"}]
    msgs, mode = canonical.reconcile(canonical_msgs, incoming)
    assert mode == "extend"
    assert msgs == incoming


def test_reconcile_extend_when_identical() -> None:
    """Equal-length identical history is the degenerate prefix — still
    'extend' (an idempotent resend), not a reset."""
    msgs_in = _turn1()
    msgs, mode = canonical.reconcile(msgs_in, [dict(m) for m in msgs_in])
    assert mode == "extend" and msgs == msgs_in


def test_reconcile_reset_on_head_divergence() -> None:
    """First-message mismatch: the client restarted the conversation — the
    client is authoritative on content, so incoming is adopted wholesale."""
    divergent = [{"role": "system", "content": "DIFFERENT"}, *_turn1()[1:]]
    msgs, mode = canonical.reconcile(_turn1(), divergent)
    assert mode == "reset" and msgs == divergent


def test_reconcile_reset_on_mid_chain_divergence() -> None:
    """A mismatch BEFORE canonical is exhausted resets even if a LATER
    message re-converges — prefix identity is all-or-nothing."""
    canonical_msgs = _turn1()
    incoming = [dict(m) for m in canonical_msgs]
    incoming[1]["content"] = "edited by the client"
    incoming.append({"role": "tool", "tool_call_id": "c1", "content": "out"})
    msgs, mode = canonical.reconcile(canonical_msgs, incoming)
    assert mode == "reset" and msgs == incoming


def test_reconcile_trimmed_incoming_shorter_wins_over_hashes() -> None:
    """Client deliberately sent LESS (its own window eviction) -> adopt it;
    checked before the hash walk because a shorter list can never extend a
    longer one — even when the short list is a strict prefix of canonical."""
    canonical_msgs = _turn1()
    shorter = canonical_msgs[:2]
    msgs, mode = canonical.reconcile(canonical_msgs, shorter)
    assert mode == "trimmed_incoming" and msgs == shorter
    # a shorter list that ALSO diverges is still just "sent less"
    msgs, mode = canonical.reconcile(canonical_msgs, [{"role": "user", "content": "fresh start"}])
    assert mode == "trimmed_incoming" and msgs == [{"role": "user", "content": "fresh start"}]


def test_reconcile_is_pure() -> None:
    """Neither input is mutated and the result is a fresh list — the caller
    (and the store) must never be able to alias-edit the client's payload."""
    canonical_msgs = _turn1()
    incoming = [*canonical_msgs, {"role": "user", "content": "next"}]
    canonical_snapshot = json.dumps(canonical_msgs, sort_keys=True)
    incoming_snapshot = json.dumps(incoming, sort_keys=True)
    msgs, _mode = canonical.reconcile(canonical_msgs, incoming)
    msgs.append({"role": "assistant", "content": "mutator"})
    assert json.dumps(canonical_msgs, sort_keys=True) == canonical_snapshot
    assert json.dumps(incoming, sort_keys=True) == incoming_snapshot


# ---------------------------------------------------------------------------
# 2. trim_to_budget(): deterministic; protects the preamble and the last turn
# ---------------------------------------------------------------------------


def _char_estimate(messages: list[dict]) -> int:
    """The same len//4 heuristic the routes layer uses (content only)."""
    return sum(len(str(m.get("content") or "")) // 4 for m in messages)


def _big_turn(n: int) -> list[dict]:
    """[system, (u_i, a_i) x n, final-user] — a growing harness history."""
    msgs: list[dict] = [{"role": "system", "content": "S"}]
    for i in range(n):
        msgs += [
            {"role": "user", "content": f"u{i}" + "x" * 40},
            {"role": "assistant", "content": f"a{i}" + "y" * 40},
        ]
    msgs.append({"role": "user", "content": "final question" + "z" * 40})
    return msgs


def test_trim_disabled_budget_is_noop() -> None:
    """Budget 0 (the shipped default) and negatives mean 'no trimming'."""
    msgs = _big_turn(6)
    out, dropped = canonical.trim_to_budget(msgs, 0, _char_estimate)
    assert out == msgs and dropped == 0
    out, dropped = canonical.trim_to_budget(msgs, -5, _char_estimate)
    assert out == msgs and dropped == 0


def test_trim_under_budget_is_noop() -> None:
    msgs = _big_turn(2)
    out, dropped = canonical.trim_to_budget(msgs, _char_estimate(msgs) + 1000, _char_estimate)
    assert out == msgs and dropped == 0


def test_trim_drops_oldest_first_keeps_system_and_last() -> None:
    msgs = _big_turn(4)  # [system, u0, a0, u1, a1, u2, a2, u3, a3, final]
    out, dropped = canonical.trim_to_budget(msgs, 60, _char_estimate)
    assert dropped > 0
    assert out[0] == msgs[0], "leading system preamble must survive"
    assert out[-1] == msgs[-1], "the newest message must survive"
    # the survivors are the preamble + a SUFFIX of the original (oldest
    # dropped first — never the middle, never the end): deterministic
    assert out == [msgs[0]] + msgs[1 + dropped :]
    assert _char_estimate(out) <= 60
    # same input + estimator -> same output
    again, again_dropped = canonical.trim_to_budget(msgs, 60, _char_estimate)
    assert again == out and again_dropped == dropped


def test_trim_without_system_preamble_still_keeps_last() -> None:
    msgs = [m for m in _big_turn(4) if m["role"] != "system"]
    out, dropped = canonical.trim_to_budget(msgs, 40, _char_estimate)
    assert out[-1] == msgs[-1]
    assert out == msgs[dropped:]
    assert _char_estimate(out) <= 40


def test_trim_stops_before_unanswering_the_prompt() -> None:
    """When only the protected messages remain and the budget still does not
    fit, trimming stops — an over-budget prompt beats a prompt the model
    cannot act on (system preamble + last message are untouchable)."""
    over = [
        {"role": "system", "content": "S" * 500},
        {"role": "user", "content": "U" * 500},
        {"role": "user", "content": "F" * 500},
    ]
    out, dropped = canonical.trim_to_budget(over, 10, _char_estimate)
    assert out == [over[0], over[2]]  # the middle user turn was droppable
    assert _char_estimate(out) > 10  # still over budget — but answerable
    out, dropped = canonical.trim_to_budget([over[0], over[2]], 10, _char_estimate)
    assert out == [over[0], over[2]] and dropped == 0


# ---------------------------------------------------------------------------
# 3. byte stability across a handback cycle (the APC guarantee)
# ---------------------------------------------------------------------------


def test_byte_stable_prefix_across_handback_cycle() -> None:
    """THE guarantee: request 2 = request 1 + one appended tool result
    reconciles as 'extend', and the upstream-visible prompt for the shared
    prefix is byte-identical — a vLLM/SGLang APC cache hit, not a 200k-token
    re-prefill. Proven THROUGH the JSONB round-trip: what the store persists
    (json text) is what the next turn's hash chain is checked against, so a
    serialization asymmetry would flip the verdict to 'reset'."""
    first = _turn1()
    stored, mode1 = canonical.reconcile(None, first)
    assert mode1 == "new"

    # the asyncpg convention round-trip: save() dumps text, load() parses it
    fake = _StoreFakeDB()
    asyncio.run(canonical.save(fake, "k", "t", stored))
    loaded = asyncio.run(canonical.load(fake, "k", "t"))

    second = [*first, {"role": "tool", "tool_call_id": "c1", "content": "file.txt"}]
    msgs, mode2 = canonical.reconcile(loaded, second)
    assert mode2 == "extend"
    assert json.dumps(msgs[: len(first)], sort_keys=True) == json.dumps(first, sort_keys=True)
    assert json.dumps(msgs, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_save_strips_nul_escapes_poison_proof() -> None:
    """A tool result carrying a literal NUL byte (binary magic, raw-bytes
    echo) serializes to the \\u0000 escape — which Postgres text/jsonb
    REJECTS ('unsupported Unicode escape sequence'), so one poisoned result
    used to freeze the canonical store: the save failed every turn, the
    store never advanced, and reconciliation silently degraded to raw mode
    (the observed pg ERROR spam on a live thread). save() strips NULs at the
    PYTHON-STRING boundary before dumps — the store is unpoisonable
    regardless of what any tool returns; the in-memory message list (what
    the model sees) is untouched."""
    poisoned = [
        {"role": "user", "content": "run it"},
        {"role": "tool", "tool_call_id": "c1", "content": "(<E\x00PNG-ish binary"},
    ]
    fake = _StoreFakeDB()
    asyncio.run(canonical.save(fake, "k", "t", poisoned))
    stored_text = fake.rows[("k", "t")]
    assert "\\u0000" not in stored_text  # PG-safe

    # Round-trips through load() with the NUL removed — the store stays a
    # usable canonical state, and a NUL-free follow-up still reconciles as
    # extend (the hash chain is over the OTHER fields, not the stripped
    # byte; a clean tool result hashes identically either way).
    loaded = asyncio.run(canonical.load(fake, "k", "t"))
    followup = [
        {"role": "user", "content": "run it"},
        {"role": "tool", "tool_call_id": "c1", "content": "(<EPNG-ish binary"},
    ]
    _msgs, mode = canonical.reconcile(loaded, followup)
    assert mode == "extend"


def test_save_never_postprocesses_the_serialized_text() -> None:
    """The first fix (a naive serialized-text replace of '\\u0000') CREATED
    the failure it prevented, caught live on G2 post-deploy: content holding
    the literal TEXT backslash+'u0000' serializes to \\\\u0000 (a LEGAL
    escaped backslash followed by u-0-0-0-0); the replace matched starting
    at the SECOND backslash, splicing a lone backslash onto the next
    character — PG then rejected 'Escape sequence "\\ " is invalid' on
    payloads that had been perfectly legal. Likewise, an escape-shaped
    regex would mangle valid \\\\ pairs. The shipped fix strips NULs from
    the PYTHON strings pre-dumps and never touches the serialized text, so
    every legal payload — including backslash-space, backslash-pipe, and
    literal '\\u0000' text — round-trips BYTE-IDENTICAL (verified against
    PG 16 + asyncpg 0.31: all of these insert fine; only the NUL escape is
    illegal)."""
    legal_cases = [
        "a\\ b",  # backslash-space (the live error's exact sequence)
        "grep \\| pg",  # backslash-pipe (grep-pattern echoes)
        "a\\u0000 b",  # literal backslash + u0000 TEXT (not an escape)
        'win "C:\\Program Files" path',
        "emoji ✓ unicode ünïcødé",
    ]
    for content in legal_cases:
        fake = _StoreFakeDB()
        asyncio.run(canonical.save(fake, "k", "t", [{"role": "user", "content": content}]))
        stored = fake.rows[("k", "t")]
        loaded = asyncio.run(canonical.load(fake, "k", "t"))
        # BYTE-identical round trip — the sanitizer never rewrites legal text
        assert json.loads(stored)[0]["content"] == content
        assert loaded[0]["content"] == content

    # NUL still stripped, everywhere in the tree (nested dicts too)
    fake = _StoreFakeDB()
    asyncio.run(
        canonical.save(
            fake,
            "k",
            "t",
            [
                {"role": "user", "content": [{"type": "text", "text": "x\x00y"}]},
                {"role": "tool", "tool_call_id": "c1", "content": "t\x00ail"},
            ],
        )
    )
    loaded = asyncio.run(canonical.load(fake, "k", "t"))
    assert "\x00" not in fake.rows[("k", "t")]
    assert loaded[0]["content"][0]["text"] == "xy"
    assert loaded[1]["content"] == "tail"


def test_thread_key_matches_toolrelay_alias() -> None:
    """canonical.thread_key is THE definition; toolrelay._thread_key is the
    back-compat alias — they must never disagree (trace threads and store
    rows key off the same value)."""
    cases: list[list[dict]] = [
        _turn1(),
        [{"role": "user", "content": "solo"}],
        [{"role": "user", "content": [{"type": "text", "text": "multimodal"}]}],
        [{"role": "system", "content": "no user message"}],
    ]
    for msgs in cases:
        assert canonical.thread_key(msgs, "k") == toolrelay._thread_key(msgs, "k")
    keyed = [m for m in cases if canonical.thread_key(m, "k") is not None]
    assert len(keyed) == len(cases) - 1  # the no-user-message case fingerprints to None
    for msgs in keyed:
        assert canonical.thread_key(msgs, "k") != canonical.thread_key(msgs, "other")


# ---------------------------------------------------------------------------
# 3b. detect_continuation(): the handback-continuation shape (phase 2A)
# ---------------------------------------------------------------------------


def _assistant_handback(*calls: tuple[str, str, str]) -> dict:
    """An assistant message requesting CLIENT tool calls: (id, name, args)."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": args}} for cid, name, args in calls
        ],
    }


def _tool_result(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_detect_continuation_positive_single_call() -> None:
    """The natural DSH replay: prior history + the assistant tool_calls
    message + its tool result -> a handback continuation."""
    canonical_msgs = _turn1()
    incoming = [
        *canonical_msgs,
        _assistant_handback(("c1", "bash", '{"cmd": "ls"}')),
        _tool_result("c1", "file.txt"),
    ]
    is_cont, info = canonical.detect_continuation(canonical_msgs, incoming)
    assert is_cont is True
    assert info == {"tool_call_ids": ["c1"], "result_count": 1}


def test_detect_continuation_positive_multi_call_batch() -> None:
    """A parallel batch answered by two results: both ids reported."""
    canonical_msgs = _turn1()
    incoming = [
        *canonical_msgs,
        _assistant_handback(("c1", "bash", '{"cmd": "ls"}'), ("c2", "edit", '{"path": "f"}')),
        _tool_result("c1", "file.txt"),
        _tool_result("c2", "ok"),
    ]
    is_cont, info = canonical.detect_continuation(canonical_msgs, incoming)
    assert is_cont is True
    assert info == {"tool_call_ids": ["c1", "c2"], "result_count": 2}


def test_detect_continuation_positive_partial_results() -> None:
    """A parallel batch with only SOME results back is still the client
    answering the handback (documented leniency: detection is advisory —
    full per-id coverage is not required)."""
    canonical_msgs = _turn1()
    incoming = [
        *canonical_msgs,
        _assistant_handback(("c1", "bash", "{}"), ("c2", "bash", "{}")),
        _tool_result("c1", "partial"),
    ]
    is_cont, info = canonical.detect_continuation(canonical_msgs, incoming)
    assert is_cont is True and info["result_count"] == 1


def test_detect_continuation_positive_content_alongside_tool_calls() -> None:
    """An assistant message that BOTH speaks and asks for tools (content +
    tool_calls) still heads a continuation — 'content-only' means no
    tool_calls at all."""
    canonical_msgs = _turn1()
    assistant = _assistant_handback(("c1", "bash", "{}"))
    assistant["content"] = "Let me check the directory."
    incoming = [*canonical_msgs, assistant, _tool_result("c1", "out")]
    is_cont, info = canonical.detect_continuation(canonical_msgs, incoming)
    assert is_cont is True and info["tool_call_ids"] == ["c1"]


def test_detect_continuation_negative_no_canonical_or_no_tail() -> None:
    """No store row, or an idempotent identical resend (no new tail): not a
    continuation — there is nothing being answered."""
    incoming = _turn1()
    assert canonical.detect_continuation(None, incoming)[0] is False
    assert canonical.detect_continuation([], incoming)[0] is False
    assert canonical.detect_continuation(incoming, [dict(m) for m in incoming])[0] is False


def test_detect_continuation_negative_divergent_or_trimmed_prefix() -> None:
    """The extend precondition is re-verified: a diverged (reset) or shorter
    (trimmed_incoming) history is never a continuation."""
    canonical_msgs = _turn1()
    divergent = [dict(m) for m in canonical_msgs]
    divergent[1]["content"] = "edited"
    divergent.append(_tool_result("c1", "out"))
    assert canonical.detect_continuation(canonical_msgs, divergent)[0] is False
    assert canonical.detect_continuation(canonical_msgs, canonical_msgs[:2])[0] is False


def test_detect_continuation_negative_new_user_turn() -> None:
    """A tail that ends in a NEW user message is the client driving a fresh
    turn, not answering the handback."""
    canonical_msgs = _turn1()
    incoming = [
        *canonical_msgs,
        _assistant_handback(("c1", "bash", "{}")),
        _tool_result("c1", "out"),
        {"role": "user", "content": "and now also do this"},
    ]
    assert canonical.detect_continuation(canonical_msgs, incoming)[0] is False


def test_detect_continuation_negative_assistant_content_only_or_misordered() -> None:
    """A content-only assistant tail (the model answered, no tools), a tail
    starting with anything but the tool-calling assistant, and a second
    assistant turn inside the tail all fail the shape."""
    canonical_msgs = _turn1()
    assert (
        canonical.detect_continuation(
            canonical_msgs, [*canonical_msgs, {"role": "assistant", "content": "just answering"}]
        )[0]
        is False
    )
    assert canonical.detect_continuation(canonical_msgs, [*canonical_msgs, _tool_result("c1", "out")])[0] is False
    interleaved = [
        *canonical_msgs,
        _assistant_handback(("c1", "bash", "{}")),
        _tool_result("c1", "out"),
        _assistant_handback(("c9", "bash", "{}")),
        _tool_result("c9", "out"),
    ]
    assert canonical.detect_continuation(canonical_msgs, interleaved)[0] is False


def test_detect_continuation_negative_unanswered_or_foreign_results() -> None:
    """An unanswered handback (assistant tool_calls, zero results) is not a
    continuation; nor is a tool message referencing a foreign id, or a call
    missing its id entirely."""
    canonical_msgs = _turn1()
    assert (
        canonical.detect_continuation(canonical_msgs, [*canonical_msgs, _assistant_handback(("c1", "bash", "{}"))])[0]
        is False
    )
    foreign = [*canonical_msgs, _assistant_handback(("c1", "bash", "{}")), _tool_result("zzz", "out")]
    assert canonical.detect_continuation(canonical_msgs, foreign)[0] is False
    no_id: dict = _assistant_handback(("c1", "bash", "{}"))
    no_id["tool_calls"][0]["id"] = ""  # type: ignore[index]
    assert (
        canonical.detect_continuation(canonical_msgs, [*canonical_msgs, no_id, _tool_result("c1", "out")])[0] is False
    )


def test_detect_continuation_never_mutates_inputs() -> None:
    canonical_msgs = _turn1()
    incoming = [*canonical_msgs, _assistant_handback(("c1", "bash", "{}")), _tool_result("c1", "out")]
    snap_c, snap_i = json.dumps(canonical_msgs), json.dumps(incoming)
    canonical.detect_continuation(canonical_msgs, incoming)
    assert json.dumps(canonical_msgs) == snap_c and json.dumps(incoming) == snap_i


# ---------------------------------------------------------------------------
# 4. DB edges against the asyncpg JSONB convention
# ---------------------------------------------------------------------------


class _StoreFakeDB:
    """Fake db that STORES canonical_conversations rows like Postgres would:
    INSERT ... ON CONFLICT upserts keyed (key_id, thread_id), JSONB read
    back as TEXT. Non-canonical SQL (profiles, rate windows) gets the
    canned answers the gate chain expects."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, str], str] = {}
        # Wave C row shape: pending tail + iterations ride SEPARATE columns —
        # the handback flush UPDATEs them without touching messages.
        self.pending_rows: dict[tuple[Any, str], str] = {}
        self.iterations: dict[tuple[Any, str], int] = {}
        self.queries: list[str] = []
        self.canonical_writes: list[tuple] = []
        self.fail_load = False
        self.fail_save = False

    async def fetchrow(self, sql: str, *args: Any):
        self.queries.append(sql)
        if "canonical_conversations" in sql:
            if self.fail_load:
                raise RuntimeError("db down")
            k = (args[0], args[1])
            if "pending" in sql and "messages" not in sql:
                raw = self.pending_rows.get(k)
                return {"pending": raw} if raw is not None else None
            raw = self.rows.get(k)
            if raw is None:
                return None
            return {"messages": raw, "iterations": self.iterations.get(k, 0)}
        if "profiles" in sql:
            return None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def execute(self, sql: str, *args: Any):
        self.queries.append(sql)
        if "canonical_conversations" in sql:
            if self.fail_save:
                raise RuntimeError("db down")
            k = (args[0], args[1])
            if "SET pending" in sql:
                # the handback flush: pending + iterations only, messages
                # column untouched (the real UPDATE's contract)
                self.pending_rows[k] = args[2]
                self.iterations[k] = args[3]
                self.canonical_writes.append(args)
                return "UPDATE 1"
            self.rows[k] = args[2]
            if len(args) > 3:
                self.iterations[k] = args[3]
            self.canonical_writes.append(args)
            return "INSERT 0 1"
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any):
        self.queries.append(sql)
        return []

    async def fetchval(self, sql: str, *args: Any):
        return None


def test_load_missing_row_returns_none() -> None:
    fake = _StoreFakeDB()
    assert asyncio.run(canonical.load(fake, uuid.uuid4(), "t")) is None


def test_save_then_load_roundtrips_as_jsonb_text() -> None:
    fake = _StoreFakeDB()
    key_id = uuid.uuid4()
    msgs = _turn1()
    asyncio.run(canonical.save(fake, key_id, "t1", msgs))
    # stored as TEXT (the JSONB wire convention), loaded back equal
    assert isinstance(fake.rows[(key_id, "t1")], str)
    assert asyncio.run(canonical.load(fake, key_id, "t1")) == msgs
    # upsert: same PK, newest content wins — one row per conversation
    msgs2 = msgs + [{"role": "tool", "tool_call_id": "c1", "content": "out"}]
    asyncio.run(canonical.save(fake, key_id, "t1", msgs2))
    assert len(fake.rows) == 1
    assert asyncio.run(canonical.load(fake, key_id, "t1")) == msgs2
    # the idempotent-write clause is in the SQL (retries must not duplicate)
    assert "ON CONFLICT (key_id, thread_id)" in fake.queries[-2]


def test_load_tolerates_already_decoded_and_corrupt_rows() -> None:
    """A future asyncpg JSON codec (or a fake returning lists) must work; a
    corrupt row is treated as ABSENT (reconcile as 'new'), never a crash."""
    fake = _StoreFakeDB()
    fake.rows[("k", "decoded")] = json.dumps(_turn1())
    fake.rows[("k", "corrupt")] = "{not json"
    fake.rows[("k", "wrongshape")] = json.dumps({"not": "a list"})
    assert asyncio.run(canonical.load(fake, "k", "decoded")) == _turn1()
    assert asyncio.run(canonical.load(fake, "k", "corrupt")) is None
    assert asyncio.run(canonical.load(fake, "k", "wrongshape")) is None


def test_load_propagates_db_failures() -> None:
    """DB-level failure RAISES from load: degrading is the CALLER's policy
    (the routes layer always degrades — asserted in section 5)."""
    fake = _StoreFakeDB()
    fake.fail_load = True
    with pytest.raises(RuntimeError):
        asyncio.run(canonical.load(fake, "k", "t"))


# ---------------------------------------------------------------------------
# 5. relay wiring: the full route with a fake db + mock upstream
# ---------------------------------------------------------------------------


class WiringFakeDB(_StoreFakeDB):
    """_StoreFakeDB plus ledger-shaped execute accounting (settle rows)."""

    def __init__(self) -> None:
        super().__init__()
        self.settled: list[tuple] = []

    async def execute(self, sql: str, *args: Any):
        if "usage_ledger" in sql:
            self.settled.append(args)
        return await super().execute(sql, *args)


def _sse(chunks: list[dict]) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 1000,
        "rate_limit_tpm": 1_000_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


@pytest.fixture
def wiring_env(monkeypatch: pytest.MonkeyPatch):
    """Relay-route harness (test_relay_settle.py's relay_env, slimmed):
    fake auth/db/registry/MCP plus a mock upstream that RECORDS the request
    bodies it receives — reconciliation verdicts are asserted at the
    upstream wire. Returns (client_factory, WiringFakeDB, calls).

    `make(key, handler=None)` accepts an upstream handler override so a test
    can SCRIPT multi-call flows (e.g. a real handback: call 1 returns
    tool_calls, call 2 returns the answer); the default is the static
    content/usage responder below."""
    fake = WiringFakeDB()

    from pcai_llm.api.routes_proxy import catalog_cache_clear

    catalog_cache_clear()

    def make(key: KeyRecord, handler: Any = None) -> TestClient:
        import pcai_llm.api.auth as auth_mod
        import pcai_llm.core.budgets as budgets_mod
        import pcai_llm.core.cascade as cascade_mod
        import pcai_llm.core.costs as costs_mod
        from pcai_llm.mcpf import federation

        calls: dict[str, Any] = {"bodies": []}

        async def fake_auth(p: str) -> KeyRecord:
            return key

        async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
            return GRANTS

        async def fake_fetch_tools(url: str, headers: dict, timeout_s: float | None = None):
            return [{"tool": "search_logs", "description": "search"}]

        if handler is None:

            def handler(request: httpx2.Request) -> httpx2.Response:  # type: ignore[misc]
                payload = json.loads(request.content)
                calls["bodies"].append(payload)
                if payload.get("stream"):
                    return httpx2.Response(
                        200,
                        content=_sse(
                            [
                                {"choices": [{"delta": {"content": "done"}}]},
                                {"usage": {"prompt_tokens": 3, "completion_tokens": 2}},
                            ]
                        ),
                        headers={"content-type": "text/event-stream"},
                    )
                return httpx2.Response(
                    200,
                    json={
                        "id": "cmpl-1",
                        "object": "chat.completion",
                        "model": MODEL,
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                    },
                )

        monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
        monkeypatch.setattr(budgets_mod, "db", fake)
        monkeypatch.setattr(costs_mod, "db", fake)
        monkeypatch.setattr(cascade_mod, "db", fake)
        monkeypatch.setattr(routes_proxy, "db", fake)  # THE canonical seam
        monkeypatch.setattr(federation, "db", fake)
        monkeypatch.setattr(federation, "load_grants", fake_grants)
        monkeypatch.setattr(routes_mcp, "_fetch_tools", fake_fetch_tools)
        registry_cache: list[dict[str, Any]] = [
            {
                "id": "srv1",
                "name": "logsearch",
                "url": "http://logsearch.svc:8080",
                "headers": {},
                "auth_secret": None,
                "auth_token_enc": None,
                "enabled": True,
                "tool_policy": "all",
                "allowed_tools": [],
            }
        ]
        import pcai_llm.core.registry as registry_mod

        registry_mod._mcp_servers_cache = registry_cache
        r = Router.with_mock(handler)
        r.set_endpoints(MODEL, [Endpoint(id="ep1", model_name=MODEL, base_url="http://fake/v1", model_type="chat")])
        monkeypatch.setattr(routes_proxy, "model_router", r)
        routes_proxy._in_flight.clear()
        routes_proxy._loop_guard.clear()

        app = FastAPI()
        app.include_router(routes_proxy.router)
        # stash calls on the fake so tests reach the upstream wire uniformly
        fake.upstream_calls = calls  # type: ignore[attr-defined]
        return TestClient(app, raise_server_exceptions=False)

    return make, fake


RELAY_HEADERS = {"Authorization": "Bearer x", "X-PCAI-Execute-Tools": "1"}


def _bodies(fake: WiringFakeDB) -> list[dict]:
    return fake.upstream_calls["bodies"]  # type: ignore[attr-defined]


def _sole_row(fake: WiringFakeDB, key_id: uuid.UUID) -> tuple[uuid.UUID, str]:
    entries = [k for k in fake.rows if k[0] == key_id]
    assert len(entries) == 1, f"expected exactly one canonical row for the key, got {entries}"
    return entries[0]  # type: ignore[return-value]


def test_wiring_first_turn_persists_canonical(caplog: pytest.LogCaptureFixture, wiring_env) -> None:
    """Turn 1: no stored row -> mode 'new'; the upstream sees the incoming
    messages unchanged and the canonical row now holds them (INFO-logged)."""
    make, fake = wiring_env
    key = _key()
    msgs = _turn1()
    tc = make(key)
    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": msgs}, headers=RELAY_HEADERS)
    assert r.status_code == 200, r.text
    assert _bodies(fake)[0]["messages"] == msgs
    key_id, thread_id = _sole_row(fake, key.id)
    assert thread_id == canonical.thread_key(msgs, key.id)
    assert json.loads(fake.rows[(key_id, thread_id)]) == msgs
    assert "mode=new" in caplog.text


def test_wiring_handback_cycle_is_byte_stable_extend(caplog: pytest.LogCaptureFixture, wiring_env) -> None:
    """THE wiring guarantee: turn 2 = turn 1 + appended tool result reconciles
    as 'extend' and the upstream receives the full incoming list whose shared
    prefix is byte-identical to turn 1's prompt (APC); the store advances."""
    make, fake = wiring_env
    key = _key()
    tc = make(key)
    first = _turn1()
    r1 = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": first}, headers=RELAY_HEADERS)
    assert r1.status_code == 200, r1.text
    second = [*first, {"role": "tool", "tool_call_id": "c1", "content": "file.txt"}]
    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r2 = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": second}, headers=RELAY_HEADERS)
    assert r2.status_code == 200, r2.text
    assert len(_bodies(fake)) == 2
    seen = _bodies(fake)[1]["messages"]
    assert seen == second, "the upstream must see canonical + tail exactly as the client sent it"
    assert json.dumps(seen[: len(first)], sort_keys=True) == json.dumps(first, sort_keys=True)
    assert "mode=extend" in caplog.text
    key_id, thread_id = _sole_row(fake, key.id)  # still ONE row — same conversation
    assert json.loads(fake.rows[(key_id, thread_id)]) == second


def test_wiring_divergence_resets_and_adopts_incoming(caplog: pytest.LogCaptureFixture, wiring_env) -> None:
    """A client-edited history (same first user message = same thread, but a
    mutated assistant turn) reconciles as 'reset': the upstream sees the
    client's version (client authority on content) and the store follows it.
    Mutating the FIRST USER message instead would change the thread seed —
    a new conversation by the fingerprint's definition, not a reset."""
    make, fake = wiring_env
    key = _key()
    tc = make(key)
    first = _turn1()
    tc.post("/v1/chat/completions", json={"model": MODEL, "messages": first}, headers=RELAY_HEADERS)
    divergent = [dict(m) for m in first]
    divergent[2]["content"] = "edited by the client"
    divergent.append({"role": "tool", "tool_call_id": "c1", "content": "out"})
    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r2 = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": divergent}, headers=RELAY_HEADERS)
    assert r2.status_code == 200, r2.text
    assert _bodies(fake)[1]["messages"] == divergent
    assert "mode=reset" in caplog.text
    key_id, thread_id = _sole_row(fake, key.id)
    assert thread_id == canonical.thread_key(first, key.id), "the thread (seed) must be unchanged"
    assert json.loads(fake.rows[(key_id, thread_id)]) == divergent


def test_wiring_trim_shrinks_upstream_projection_only(
    wiring_env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """With a context budget, the UPSTREAM projection is trimmed (system
    preamble + newest turn kept) while the STORE keeps the untrimmed
    conversation — trimming is a projection, never a rewrite of history."""
    make, fake = wiring_env
    monkeypatch.setattr(settings, "conversation_context_budget_tokens", 30)
    key = _key()
    tc = make(key)
    msgs = _big_turn(4)
    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": msgs}, headers=RELAY_HEADERS)
    assert r.status_code == 200, r.text
    seen = _bodies(fake)[0]["messages"]
    assert len(seen) < len(msgs), "the over-budget projection must shrink"
    assert seen[0] == msgs[0] and seen[-1] == msgs[-1]
    # preamble + a contiguous oldest-first-dropped suffix of the original
    assert seen == [msgs[0]] + msgs[len(msgs) - (len(seen) - 1) :]
    key_id, thread_id = _sole_row(fake, key.id)
    assert json.loads(fake.rows[(key_id, thread_id)]) == msgs, "the store must hold the FULL conversation"
    assert "trimmed" in caplog.text


def test_wiring_raw_header_bypasses_everything(wiring_env, caplog: pytest.LogCaptureFixture) -> None:
    """X-PCAI-Conversation: raw — no read, no write, incoming as-is, even
    when a canonical row exists. For clients that manage their own context."""
    make, fake = wiring_env
    key = _key()
    tc = make(key)
    stored = [{"role": "user", "content": "an older conversation"}]
    fake.rows[(key.id, canonical.thread_key(stored, key.id))] = json.dumps(stored)
    incoming = _turn1()
    headers = {**RELAY_HEADERS, "X-PCAI-Conversation": "raw"}
    before_writes = len(fake.canonical_writes)
    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": incoming}, headers=headers)
    assert r.status_code == 200, r.text
    assert _bodies(fake)[0]["messages"] == incoming
    assert len(fake.canonical_writes) == before_writes, "the escape hatch must not write the store"
    assert fake.rows[(key.id, canonical.thread_key(stored, key.id))] == json.dumps(stored)
    assert "reconciled relay conversation" not in caplog.text


def test_wiring_fail_safe_on_db_failure(wiring_env, caplog: pytest.LogCaptureFixture) -> None:
    """DB down at the canonical load: the relay turn PROCEEDS on the raw
    incoming messages (never fails, never blocks) with a warning."""
    make, fake = wiring_env
    fake.fail_load = True
    key = _key()
    tc = make(key)
    msgs = _turn1()
    with caplog.at_level(logging.WARNING, logger="pcai_llm.api.routes_proxy"):
        r = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": msgs}, headers=RELAY_HEADERS)
    assert r.status_code == 200, r.text
    assert _bodies(fake)[0]["messages"] == msgs
    assert "continuing on raw incoming messages" in caplog.text
    assert fake.settled, "the relay model call must still be settled"


def test_wiring_fail_safe_on_persist_failure(wiring_env, caplog: pytest.LogCaptureFixture) -> None:
    """DB down at the canonical SAVE: same fail-safe — the turn completes on
    the reconciled (== incoming) messages, warning logged."""
    make, fake = wiring_env
    fake.fail_save = True
    key = _key()
    tc = make(key)
    msgs = _turn1()
    with caplog.at_level(logging.WARNING, logger="pcai_llm.api.routes_proxy"):
        r = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": msgs}, headers=RELAY_HEADERS)
    assert r.status_code == 200, r.text
    assert _bodies(fake)[0]["messages"] == msgs
    assert "continuing on raw incoming messages" in caplog.text


def test_wiring_feature_switch_inert(wiring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """canonical_conversations=false = today's behavior byte-for-byte: no
    canonical SQL at all, incoming relayed untouched, store untouched."""
    make, fake = wiring_env
    monkeypatch.setattr(settings, "canonical_conversations", False)
    key = _key()
    tc = make(key)
    stored = [{"role": "user", "content": "an older conversation"}]
    fake.rows[(key.id, canonical.thread_key(stored, key.id))] = json.dumps(stored)
    incoming = _turn1()
    before_writes = len(fake.canonical_writes)
    r = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": incoming}, headers=RELAY_HEADERS)
    assert r.status_code == 200, r.text
    assert _bodies(fake)[0]["messages"] == incoming
    assert not [q for q in fake.queries if "canonical_conversations" in q], (
        "the inert switch must not touch the canonical store"
    )
    assert len(fake.canonical_writes) == before_writes


def test_wiring_reasoning_is_bounded_not_stripped(
    wiring_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reasoning is CAPPED on the way through, not deleted (2026-10-26).

    SUPERSEDES test_wiring_reasoning_stripped_before_reconciliation. That test
    pinned a full strip, which was the fix for the 2026-09-18 relay storm
    (a client re-appending each streamed reasoning block per cycle; one stored
    event reached 6,630 copies of a line, ~401 KB). The strip removed the
    symptom but also the model's own chain of thought: replayed turns became
    "assistant emitted tool calls, no thinking", which teaches the model to
    skip reasoning. Measured: 86.1% of relayed tool-call steps carried no
    reasoning vs 46.1% direct.

    The BOUND is the real fix: growth is now linear in message count and
    capped, so the unbounded storm shape cannot recur.
    """
    make, fake = wiring_env
    key = _key()
    tc = make(key)
    dirty = _turn1()
    long_reasoning = "x" * 5000
    dirty[1]["reasoning_content"] = long_reasoning
    dirty[2]["reasoning"] = "more scratch"
    r = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": dirty}, headers=RELAY_HEADERS)
    assert r.status_code == 200, r.text

    seen = _bodies(fake)[0]["messages"]
    carried = [m for m in seen if m.get("reasoning_content")]
    assert carried, "reasoning must reach the model, not be stripped"
    cap = settings.reasoning_history_max_chars
    for m in carried:
        assert len(m["reasoning_content"]) <= cap + 64  # cap + trim marker
    # The short "reasoning" key rides through intact (under the cap).
    assert any(m.get("reasoning") == "more scratch" for m in seen)

    # The STORE is bounded too: it can never become an unbounded
    # re-injection path (the storm's amplification surface).
    key_id, thread_id = _sole_row(fake, key.id)
    stored = json.loads(fake.rows[(key_id, thread_id)])
    for m in stored:
        for rk in ("reasoning_content", "reasoning"):
            if m.get(rk):
                assert len(m[rk]) <= cap + 64


def test_wiring_reasoning_strip_is_still_available_as_an_escape_hatch(
    wiring_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """reasoning_history_enabled=False restores the pre-2026-10-26 strip
    byte-for-byte — the one-knob rollback if a client re-appends unbounded."""
    monkeypatch.setattr(settings, "reasoning_history_enabled", False)
    make, fake = wiring_env
    key = _key()
    tc = make(key)
    dirty = _turn1()
    dirty[1]["reasoning_content"] = "chain of thought"
    dirty[2]["reasoning"] = "more scratch"
    r = tc.post("/v1/chat/completions", json={"model": MODEL, "messages": dirty}, headers=RELAY_HEADERS)
    assert r.status_code == 200, r.text
    seen = _bodies(fake)[0]["messages"]
    assert all("reasoning_content" not in m and "reasoning" not in m for m in seen)


def test_wiring_non_relay_request_captures_but_never_rewrites(wiring_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """SUPERSEDED 2026-10-09 (automatic memories for every agent): a plain
    chat now CAPTURES fire-and-forget into the canonical store (the distiller
    sweep reads it), but the request itself stays byte-inert — the upstream
    sees exactly what the client sent, and the response is untouched. The
    old assertion (zero canonical SQL on the plain path) is gone; the
    invariant that matters — capture is bookkeeping, never a rewrite — is
    what this test now pins."""
    make, fake = wiring_env
    key = _key()
    tc = make(key)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "plain chat"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "hi"
    # capture happened (best-effort, synchronous within the fake db)
    assert any("canonical_conversations" in q for q in fake.queries)
    # ...and the row records what the client sent (bookkeeping, not a rewrite)
    tid = canonical.thread_key([{"role": "user", "content": "plain chat"}], key.id)
    row = fake.rows.get((key.id, tid))
    assert row is not None


# ---------------------------------------------------------------------------
# 6. handback continuation end-to-end (ROADMAP §7 phase 2A)
# ---------------------------------------------------------------------------

BASH_TOOL = {"type": "function", "function": {"name": "bash", "description": "run a shell command"}}


def _tool_call_sse(call_id: str, name: str, arguments: str) -> bytes:
    """One scripted upstream model call that ends in a client tool call."""
    return _sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ]
                        }
                    }
                ]
            },
            {"usage": {"prompt_tokens": 4, "completion_tokens": 1}},
        ]
    )


def _content_sse(text: str) -> bytes:
    """One scripted upstream model call that answers with final content."""
    return _sse(
        [
            {"choices": [{"delta": {"content": text}}]},
            {"usage": {"prompt_tokens": 4, "completion_tokens": 1}},
        ]
    )


def _scripted_handler(script: list[bytes], bodies: list[dict]) -> Any:
    """An upstream mock that plays `script` one entry per model call (the
    last entry repeats) and records every request body it receives."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            content=script[min(len(bodies) - 1, len(script) - 1)],
            headers={"content-type": "text/event-stream"},
        )

    return handler


def _relay_post(tc: TestClient, messages: list[dict]) -> Any:
    return tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": messages, "tools": [BASH_TOOL]},
        headers=RELAY_HEADERS,
    )


def _relay_post_continue(tc: TestClient, messages: list[dict], thread: str) -> Any:
    """The result-only continuation shape: X-PCAI-Continue + ONLY the
    role=tool results answering the handed-back calls."""
    return tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": messages, "tools": [BASH_TOOL]},
        headers={**RELAY_HEADERS, "X-PCAI-Continue": thread},
    )


def test_wiring_result_only_continuation_roundtrip(caplog: pytest.LogCaptureFixture, wiring_env) -> None:
    """THE X-PCAI-Continue round-trip lock (2026-09-25 live report):

    request 1 [system, user] -> model calls bash -> HANDBACK (flush writes
    the awaiting-client TAIL — assistant tool_calls only, NOT the full
    list — to pending, iterations=1)
    request 2: X-PCAI-Continue: <thread> + messages = [tool result] ONLY
    -> the gateway merges stored + pending + results, the shape check
    PASSES (no duplicated prefix), the relay runs, and the final answer
    streams with the counter resumed.

    Regression: the flush used to persist the FULL relay_msgs list, so the
    merged tail began with the re-appended system message and the shape
    check 400'd on every continuation — the protocol was dead on arrival."""
    make, fake = wiring_env
    key = _key()
    bodies: list[dict] = []
    handler = _scripted_handler(
        [
            _tool_call_sse("c1", "bash", '{"cmd": "ls"}'),  # request 1: handback
            _content_sse("continued answer"),  # request 2 (continuation): final
        ],
        bodies,
    )
    tc = make(key, handler=handler)

    turn1 = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "run the check"},
    ]
    r1 = _relay_post(tc, turn1)
    assert r1.status_code == 200, r1.text
    assert '"finish_reason": "tool_calls"' in r1.text
    # extract the thread ref the handback event carried
    thread = None
    for line in r1.text.splitlines():
        if line.startswith("data:") and '"handback"' in line:
            thread = json.loads(line[5:])["pcai"].get("thread")
    assert thread, f"no handback thread in stream: {r1.text[:400]}"

    # THE FLUSH SHAPE: pending holds ONLY the tail (the assistant tool_calls
    # message), NOT the full list — the merge's precondition. (The relay
    # preserves the assistant message verbatim — content=None here because
    # the scripted SSE carried no content delta.)
    key_id, thread_id = _sole_row(fake, key.id)
    assert thread_id == thread
    pending = json.loads(fake.pending_rows[(key_id, thread_id)])
    assert pending == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
            ],
        }
    ], f"pending must be the awaiting-client TAIL only, got: {pending}"
    assert fake.iterations[(key_id, thread_id)] == 1

    # request 2: result-only continuation
    results_only = [_tool_result("c1", "file listing")]
    r2 = _relay_post_continue(tc, results_only, thread)
    assert r2.status_code == 200, f"result-only continuation must round-trip: {r2.text[:400]}"
    assert "continued answer" in r2.text, "the merged conversation must flow through to the final answer"
    # the upstream saw the FULL merged conversation: canonical + pending + results
    assert len(bodies) == 2
    merged = bodies[1]["messages"]
    assert merged[:2] == turn1, "the stored canonical prefix leads the merged list"
    assert merged[2] == pending[0], "the awaiting assistant tool_calls follows"
    assert merged[3] == results_only[0], "the client's tool result closes the turn"
    # the merged conversation flowed through; the store's counter records
    # the turn's resumed spend (flush carried 1, the continuation ran and
    # answered at iteration 1 — no handback terminal this time, so the
    # value persists from the request-1 flush).
    assert fake.iterations[(key_id, thread_id)] == 1


def test_wiring_result_only_continuation_unknown_thread_and_state(
    wiring_env,
) -> None:
    """The fail-loud contract of the result-only protocol: an unknown
    thread 400s; a thread with NO awaiting handback state 400s (a final-
    answer terminal does not flush pending); garbage thread refs 400."""
    make, _fake = wiring_env
    key = _key()
    bodies: list[dict] = []
    handler = _scripted_handler([_content_sse("plain answer")], bodies)
    tc = make(key, handler=handler)

    # unknown thread
    r = _relay_post_continue(tc, [_tool_result("c1", "x")], "deadbeef" * 4)
    assert r.status_code == 400 and "no server-owned conversation" in r.text

    # known thread but the last turn ended in a FINAL ANSWER (no pending)
    turn1 = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "just answer"},
    ]
    r1 = _relay_post(tc, turn1)
    assert r1.status_code == 200 and '"finish_reason": "stop"' in r1.text
    thread = canonical.thread_key(turn1, key.id)
    r2 = _relay_post_continue(tc, [_tool_result("c1", "x")], thread)
    assert r2.status_code == 400 and "no awaiting handback state" in r2.text


def test_wiring_result_only_continuation_rejects_non_results(
    wiring_env,
) -> None:
    """A client that 'continues' with something other than the handback's
    tool results (e.g. a new user message) fails the shape check LOUD —
    the protocol is for results only; a fresh turn goes through the
    full-history path (and resets the budget there)."""
    make, _fake = wiring_env
    key = _key()
    bodies: list[dict] = []
    handler = _scripted_handler(
        [
            _tool_call_sse("c1", "bash", '{"cmd": "ls"}'),  # handback
            _content_sse("should-never-run"),  # the 400 must preempt this
        ],
        bodies,
    )
    tc = make(key, handler=handler)
    turn1 = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "run the check"},
    ]
    r1 = _relay_post(tc, turn1)
    assert r1.status_code == 200
    thread = None
    for line in r1.text.splitlines():
        if line.startswith("data:") and '"handback"' in line:
            thread = json.loads(line[5:])["pcai"].get("thread")
    assert thread

    # "continuation" carrying a NEW USER message instead of the results
    r2 = _relay_post_continue(tc, [{"role": "user", "content": "actually do this instead"}], thread)
    assert r2.status_code == 400
    assert "not a handback continuation" in r2.text
    assert len(bodies) == 1, "the 400 must fire BEFORE any upstream model call"


def test_wiring_handback_continuation_prefix_stability_and_persistence(
    caplog: pytest.LogCaptureFixture, wiring_env
) -> None:
    """THE phase-2A integration test, three requests of one conversation:

    request 1 [system, user]              -> model calls bash  -> HANDBACK
    request 2 [.., assistant c1, tool c1] -> model calls bash  -> HANDBACK (continuation)
    request 3 [.., assistant c2, tool c2] -> model answers     -> final

    Locks the APC invariant at the upstream wire: each request's prompt is
    the PREVIOUS request's prompt EXTENDED — byte-identical prefix, only
    the handback's assistant tool_calls + role=tool results appended — and
    locks that the canonical store advances with every continuation so the
    NEXT continuation still reconciles cleanly. The continuation linkage is
    observable: the routes layer logs it and request 2's handback event
    carries "continuation": true (request 1's does not — it STARTED the
    handback)."""
    make, fake = wiring_env
    key = _key()
    bodies: list[dict] = []
    tc = make(
        key,
        handler=_scripted_handler(
            [
                _tool_call_sse("c1", "bash", '{"cmd": "ls"}'),
                _tool_call_sse("c2", "bash", '{"cmd": "cat file.txt"}'),
                _content_sse("all done"),
            ],
            bodies,
        ),
    )

    turn1 = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "run the check"},
    ]
    assistant1 = _assistant_handback(("c1", "bash", '{"cmd": "ls"}'))
    result1 = _tool_result("c1", "file.txt")
    assistant2 = _assistant_handback(("c2", "bash", '{"cmd": "cat file.txt"}'))
    result2 = _tool_result("c2", "file contents")
    conv2 = [*turn1, assistant1, result1]
    conv3 = [*conv2, assistant2, result2]

    with (
        caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"),
        caplog.at_level(logging.INFO, logger="pcai_llm.core.toolrelay"),
    ):
        r1 = _relay_post(tc, turn1)
        assert r1.status_code == 200, r1.text
        # request 1 starts the handback — no continuation linkage anywhere
        assert '"finish_reason": "tool_calls"' in r1.text
        assert '"continuation"' not in r1.text
        assert "handback-continuation" not in caplog.text
        caplog.clear()

        r2 = _relay_post(tc, conv2)
        assert r2.status_code == 200, r2.text
        # request 2 answers the handback: continuation-flagged handback
        assert '"finish_reason": "tool_calls"' in r2.text
        assert '"continuation": true' in r2.text, r2.text
        assert "handback-continuation" in caplog.text
        assert "handback continuation thread=" in caplog.text

        r3 = _relay_post(tc, conv3)
        assert r3.status_code == 200, r3.text
        assert "all done" in r3.text, "the continuation must flow through to the final answer"
        # the store caught up across BOTH continuations: r2 AND r3 were each
        # detected as handback continuations (r3's detection requires r2's
        # tool-result tail to have landed in the store — a stale row would
        # make its tail multi-assistant and fail the shape)
        assert caplog.text.count("handback-continuation") == 2

    # --- THE STABLE-PREFIX LOCK (what makes vLLM/SGLang APC hit) -----------
    assert len(bodies) == 3
    assert bodies[0]["messages"] == turn1
    assert bodies[1]["messages"] == conv2, (
        "the continuation request must reach the upstream exactly as the "
        "client sent it: canonical + [assistant tool_calls, tool result]"
    )
    assert json.dumps(bodies[1]["messages"][: len(turn1)], sort_keys=True) == json.dumps(turn1, sort_keys=True)
    assert json.dumps(bodies[2]["messages"][: len(bodies[1]["messages"])], sort_keys=True) == json.dumps(
        bodies[1]["messages"], sort_keys=True
    ), "request 3's prompt must be request 2's prompt EXTENDED — byte-identical prefix, tail only appended"
    assert bodies[2]["messages"] == conv3

    # --- persistence after continuation -----------------------------------
    key_id, thread_id = _sole_row(fake, key.id)
    assert thread_id == canonical.thread_key(turn1, key.id), "one conversation = one thread/row"
    assert json.loads(fake.rows[(key_id, thread_id)]) == conv3, (
        "the store must hold the FULL extended conversation — the tool-result "
        "tail lands in the store so the next continuation reconciles cleanly"
    )


def test_wiring_extend_with_new_user_turn_is_not_a_continuation(caplog: pytest.LogCaptureFixture, wiring_env) -> None:
    """Extend WITHOUT the handback tail shape (the client appended a fresh
    user turn after the tool result) reconciles as extend but must NOT be
    flagged as a handback continuation — no linkage log, no event flag."""
    make, fake = wiring_env
    key = _key()
    bodies: list[dict] = []
    tc = make(
        key,
        handler=_scripted_handler(
            [
                _tool_call_sse("c1", "bash", '{"cmd": "ls"}'),
                _content_sse("answered the follow-up"),
            ],
            bodies,
        ),
    )
    turn1 = [
        {"role": "system", "content": "You are a helpful agent."},
        {"role": "user", "content": "run the check"},
    ]
    conv2 = [
        *turn1,
        _assistant_handback(("c1", "bash", '{"cmd": "ls"}')),
        _tool_result("c1", "file.txt"),
        {"role": "user", "content": "and now summarize"},
    ]
    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r1 = _relay_post(tc, turn1)
        assert r1.status_code == 200, r1.text
        caplog.clear()
        r2 = _relay_post(tc, conv2)
        assert r2.status_code == 200, r2.text
    assert "mode=extend" in caplog.text
    assert "handback-continuation" not in caplog.text
    assert '"continuation"' not in r2.text
    key_id, thread_id = _sole_row(fake, key.id)
    assert json.loads(fake.rows[(key_id, thread_id)]) == conv2


# ---------------------------------------------------------------------------
# Per-turn loop budget (2026-09-25): the relay's iteration counter resumes
# ONLY across a handback continuation; any fresh user turn resets it to 0.
# ---------------------------------------------------------------------------


def test_wiring_new_user_turn_resets_iteration_budget(caplog: pytest.LogCaptureFixture, wiring_env) -> None:
    """THE per-turn reset lock: a conversation that already burned a relay
    round in turn 1 starts turn 2 (a NEW user message appended to the
    history) with the guard at 0 — the store's carried counter never leaks
    into a fresh turn.

    Script: turn 1 handbacks after round 1 (the client answers, the store
    carries iterations=1); turn 2's single model call answers. With the
    cap at 1, a carried counter would abort turn 2's call BEFORE the model
    is even asked; the reset lets it run."""
    make, fake = wiring_env
    key = _key()
    bodies: list[dict] = []
    handler = _scripted_handler(
        [
            _tool_call_sse("c1", "bash", '{"cmd": "step1"}'),  # turn 1: handback, store iterations=1
            _content_sse("fresh turn answered"),  # turn 2 round 1: must RUN
        ],
        bodies,
    )
    from pcai_llm.config import settings as _settings

    monkey = pytest.MonkeyPatch()
    monkey.setattr(_settings, "tool_relay_max_iterations", 1)
    try:
        tc = make(key, handler=handler)

        turn1 = [
            {"role": "system", "content": "You are a helpful agent."},
            {"role": "user", "content": "do the research"},
        ]
        conv2 = [
            *turn1,
            _assistant_handback(("c1", "bash", '{"cmd": "step1"}')),
            _tool_result("c1", "out1"),
        ]
        # THE TURN BOUNDARY: a new user message on the same conversation.
        turn2 = [*conv2, {"role": "user", "content": "different question entirely"}]

        r1 = _relay_post(tc, turn1)
        assert r1.status_code == 200, r1.text
        assert '"finish_reason": "tool_calls"' in r1.text
        key_id, thread_id = _sole_row(fake, key.id)
        assert fake.iterations[(key_id, thread_id)] == 1, "turn 1's handback flush carried the counter"

        r2 = _relay_post(tc, turn2)
        assert r2.status_code == 200, r2.text
        assert "max tool iterations" not in r2.text, (
            "a fresh user turn must reset the iteration budget — the guard "
            "ran with the prior turn's counter and aborted before the model call"
        )
        assert "fresh turn answered" in r2.text, "the reset turn's model call must flow through"
        # the reconciliation extended the prefix but did NOT detect a
        # handback continuation — the new user message breaks the shape.
        assert "handback-continuation" not in r2.text
        key_id, thread_id = _sole_row(fake, key.id)
        assert fake.iterations[(key_id, thread_id)] == 0, "turn 2's reconcile-time baseline restarted at 0"
    finally:
        monkey.undo()


def test_wiring_handback_continuation_resumes_budget(caplog: pytest.LogCaptureFixture, wiring_env) -> None:
    """The complementary lock: WITHIN one turn, a handback continuation
    still RESUMES the counter — the per-turn budget is not per-request.

    Cap 2. Request 1: a pure in-cluster MCP round (c1 executes, the loop
    continues) then a MIXED batch (c2 executes + c3 hands back) — the
    handback terminal flushes iterations=2. The continuation request's
    first model call is round 3 of the turn: under per-request semantics
    the guard would see 0 and run to a FIFTH scripted call; with the
    resume the guard trips at the cap and the toolless wrap-up answers."""
    make, fake = wiring_env
    key = _key()
    bodies: list[dict] = []
    handler = _scripted_handler(
        [
            _tool_call_sse(
                "c1", "logsearch__search_logs", '{"q": "round one"}'
            ),  # r1 round 0: pure MCP, loop continues
            _sse(  # r1 round 1: MIXED batch — c2 executes in-loop, c3 hands back
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "c2",
                                            "type": "function",
                                            "function": {
                                                "name": "logsearch__search_logs",
                                                "arguments": '{"q": "round two"}',
                                            },
                                        },
                                        {
                                            "index": 1,
                                            "id": "c3",
                                            "type": "function",
                                            "function": {"name": "bash", "arguments": '{"cmd": "step2"}'},
                                        },
                                    ]
                                }
                            }
                        ]
                    },
                    {"usage": {"prompt_tokens": 4, "completion_tokens": 1}},
                ]
            ),
            _sse(  # r2 round 2 (the continuation's first call): mixed again — the cap fires here
                [
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "c4",
                                            "type": "function",
                                            "function": {"name": "logsearch__search_logs", "arguments": "{}"},
                                        },
                                        {
                                            "index": 1,
                                            "id": "c5",
                                            "type": "function",
                                            "function": {"name": "bash", "arguments": '{"cmd": "step3"}'},
                                        },
                                    ]
                                }
                            }
                        ]
                    },
                    {"usage": {"prompt_tokens": 4, "completion_tokens": 1}},
                ]
            ),
            _content_sse("WRAPUP"),  # the guard's toolless wrap-up call
            _content_sse("should-never-run"),  # a per-request reset would reach this
        ],
        bodies,
    )

    # make the in-cluster MCP call EXECUTE (relay_env's fake session shape):
    # without this the wiring fixture's logsearch would fail DNS and muddy
    # the script (an error result still advances the iteration — noise).
    import pcai_llm.api.routes_mcp as routes_mcp_mod

    class FakeBlock:
        def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
            return {"type": "text", "text": "ok"}

    class FakeResult:
        def __init__(self) -> None:
            self.is_error = False
            self.content = [FakeBlock()]

    async def fake_upstream_session(url, headers, op, timeout_s=None):
        return FakeResult()

    from pcai_llm.config import settings as _settings

    monkey = pytest.MonkeyPatch()
    monkey.setattr(_settings, "tool_relay_max_iterations", 2)
    monkey.setattr(routes_mcp_mod, "_upstream_session", fake_upstream_session)
    try:
        tc = make(key, handler=handler)
        turn1 = [
            {"role": "system", "content": "You are a helpful agent."},
            {"role": "user", "content": "do the research"},
        ]
        # the harness's literal echo of request 1's handback: it NEVER saw
        # the in-loop c1/c2 MCP rounds (they executed inside the relay and
        # streamed only as reasoning summaries), so its history carries only
        # the handed-back c3 call + its result.
        conv2: list[dict[str, Any]] = [
            *turn1,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c3", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "step2"}'}},
                ],
            },
            _tool_result("c3", "out2"),
        ]

        r1 = _relay_post(tc, turn1)
        assert r1.status_code == 200, r1.text
        assert '"finish_reason": "tool_calls"' in r1.text, "request 1 must end in the mixed handback"
        key_id, thread_id = _sole_row(fake, key.id)
        assert fake.iterations[(key_id, thread_id)] == 2, (
            "request 1 spent two in-cluster rounds (c1 + c2) — the flush records 2"
        )

        r2 = _relay_post(tc, conv2)
        assert r2.status_code == 200, r2.text
        # the continuation RESUMED (the guard fired at the cap on its first
        # round) and the wrap-up produced the answer — the turn is bounded.
        assert "max tool iterations (2) reached" in r2.text, (
            "the continuation must RESUME the turn's counter — if the cap "
            "never fires the counter restarted and the per-turn bound is a lie"
        )
        assert "WRAPUP" in r2.text
        assert "should-never-run" not in r2.text
        assert len(bodies) == 4, "exactly the 3 model rounds + wrap-up — the loop stopped at the cap"
        # the durable counter records the turn's true spend
        assert fake.iterations[(key_id, thread_id)] == 2
    finally:
        monkey.undo()
