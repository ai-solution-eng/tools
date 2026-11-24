"""Playbook evidence + staleness (B6, 2026-11-23).

Two halves, both tested without a gateway, a database or a network:

* the WRITE half (core.memory_write) stamps ``evidence_ref`` - "request:<id>"
  when the caller passes the originating turn, else "thread:<session>" - into
  the playbook memory metadata, next to the long-standing ``evidence_thread``;
* the READ half (core.playbook) surfaces that reference on the parsed record
  and WITHHOLDS a procedure whose timestamp is older than the operator limit,
  rendering [stale] plus the reason instead of silently dropping it.

Every time-dependent test injects ``now`` (epoch seconds), so the suite has no
clock, no sleeping and no network; the write half is driven through the real
overlay with a stub MCP call that only records the argument dicts.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from pcai_llm.config import settings
from pcai_llm.core import memory_write, playbook

# A fixed epoch (2025-11-25) and a day in seconds: every staleness assertion
# below is relative to them, so nothing here depends on the wall clock.
NOW = 1_764_000_000.0
DAY = 86_400.0
TASK = "recover a stuck pod that will not terminate"


def _iso(epoch: float) -> str:
    """The store ISO-8601 shape (what last_validated / memory_ts carry)."""
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds")


PLAYBOOK_OBJ: dict[str, Any] = {
    "task_signature": "recover a stuck pod",
    "applies_when": ["a pod is Terminating"],
    "steps": ["check finalizers", "delete the pod", "verify it reschedules"],
    "failure_modes": ["forcing deletion before the finalizer clears"],
    "outcome": "success",
}

RECORD: dict[str, Any] = {
    "memory_id": "m1",
    "playbook_key": "k1",
    "task_signature": "recover a stuck pod",
    "applies_when": ["a pod is Terminating"],
    "steps": ["check finalizers", "delete the pod", "verify it reschedules"],
    "failure_modes": ["forcing deletion before the finalizer clears"],
    "outcome": "success",
    "outcome_source": "inferred",
    "success_count": 6,
    "fail_count": 1,
    "score": 7 / 9,
    "last_validated": _iso(NOW - 5 * DAY),
    "evidence_thread": "conv-42",
    "evidence_ref": "request:distill-ab12cd34ef56",
}


def _envelope(rows: list[dict]) -> str:
    """The mm-rag recall_playbooks JSON envelope around these records."""
    return json.dumps({"dataset": "mem", "task": TASK, "count": len(rows), "records": rows})


def _records(rows: list[dict], **kwargs: Any) -> list[dict]:
    """Parsed records for these rows, with the fixed clock injected."""
    return playbook.parse_recall_records(_envelope(rows), now=NOW, **kwargs)


def _block(rows: list[dict], **kwargs: Any) -> str:
    """The rendered block for these rows."""
    return playbook.render_playbook_block(_records(rows, **kwargs))


# ---------------------------------------------------------------------------
# the evidence reference itself (pure helper)
# ---------------------------------------------------------------------------


def test_evidence_ref_prefers_the_request_and_falls_back_to_the_thread() -> None:
    assert playbook.playbook_evidence_ref("conv-42", "distill-abc") == "request:distill-abc"
    assert playbook.playbook_evidence_ref("conv-42") == "thread:conv-42"
    assert playbook.playbook_evidence_ref("conv-42", "   ") == "thread:conv-42"
    assert playbook.playbook_evidence_ref("") == ""
    assert playbook.playbook_evidence_ref("", "") == ""


def test_evidence_ref_is_one_bounded_line() -> None:
    """A caller-supplied id is collapsed and bounded: the metadata value must
    stay a single token, whatever a client sent."""
    assert playbook.playbook_evidence_ref("conv\n42", " rid ") == "request:rid"
    long_ref = playbook.playbook_evidence_ref("x" * 500)
    assert long_ref.startswith("thread:")
    assert len(long_ref) == len("thread:") + playbook.EVIDENCE_REF_MAX_CHARS
    assert "\n" not in long_ref


def test_parsed_record_surfaces_the_evidence_ref_and_thread() -> None:
    (record,) = _records([RECORD])
    assert record["evidence_ref"] == "request:distill-ab12cd34ef56"
    assert record["evidence_thread"] == "conv-42"


def test_parsed_record_without_an_evidence_ref_keeps_the_keys() -> None:
    """A store written by another path answers an empty string, not a missing
    key, so a caller never has to guard the lookup."""
    row = {key: value for key, value in RECORD.items() if not key.startswith("evidence_")}
    (record,) = _records([row])
    assert record["evidence_ref"] == ""
    assert record["evidence_thread"] == ""


def test_a_multiline_stored_reference_is_collapsed() -> None:
    (record,) = _records([{**RECORD, "evidence_ref": "request:abc\ndef"}])
    assert record["evidence_ref"] == "request:abc def"


# ---------------------------------------------------------------------------
# staleness: fresh, stale, undated, disabled
# ---------------------------------------------------------------------------


def test_a_fresh_record_is_injected() -> None:
    block = _block([RECORD], stale_after_days=90)
    assert "1. check finalizers" in block
    assert "[stale]" not in block


def test_a_stale_record_is_withheld_and_labelled() -> None:
    """NOT injected - but not silently dropped either: the block says [stale],
    names the timestamp and the limit, and points at the originating turn."""
    stale = {**RECORD, "last_validated": _iso(NOW - 200 * DAY)}
    block = _block([stale], stale_after_days=90)
    assert "[stale]" in block
    assert "recover a stuck pod" in block
    assert "last_validated 200d old, limit 90d" in block
    assert "steps withheld" in block
    assert "evidence request:distill-ab12cd34ef56" in block
    # THE POINT: no step of a stale procedure reaches the preamble
    assert "check finalizers" not in block
    assert "delete the pod" not in block
    assert "AVOID:" not in block


def test_an_undated_record_is_treated_as_fresh() -> None:
    """FAIL OPEN: no timestamp is not evidence of staleness."""
    row = {key: value for key, value in RECORD.items() if key != "last_validated"}
    block = _block([row], stale_after_days=1)
    assert "1. check finalizers" in block
    assert "[stale]" not in block


def test_zero_or_negative_limit_disables_the_rule() -> None:
    """<= 0 = never expire, exactly like every other retention knob here."""
    ancient = {**RECORD, "last_validated": _iso(NOW - 5000 * DAY)}
    for limit in (0, -1, -365):
        block = _block([ancient], stale_after_days=limit)
        assert "1. check finalizers" in block, limit
        assert "[stale]" not in block, limit


def test_a_record_exactly_at_the_limit_is_fresh() -> None:
    """OLDER than the limit, strictly: 90 days old survives a 90-day rule."""
    row = {**RECORD, "last_validated": _iso(NOW - 90 * DAY)}
    block = _block([row], stale_after_days=90)
    assert "1. check finalizers" in block
    assert "[stale]" not in block


def test_memory_ts_is_used_when_last_validated_is_absent() -> None:
    row = {key: value for key, value in RECORD.items() if key != "last_validated"}
    row["memory_ts"] = _iso(NOW - 200 * DAY)
    block = _block([row], stale_after_days=90)
    assert "[stale]" in block
    assert "memory_ts 200d old" in block
    assert "check finalizers" not in block


def test_last_validated_wins_over_memory_ts() -> None:
    """Preference order is the contract: validation time beats storage time."""
    row = {**RECORD, "last_validated": _iso(NOW - 2 * DAY), "memory_ts": _iso(NOW - 400 * DAY)}
    assert "[stale]" not in _block([row], stale_after_days=90)
    row = {**RECORD, "last_validated": _iso(NOW - 400 * DAY), "memory_ts": _iso(NOW - 2 * DAY)}
    assert "[stale]" in _block([row], stale_after_days=90)


def test_an_unparseable_timestamp_falls_through_to_the_next_field() -> None:
    row = {**RECORD, "last_validated": "not-a-timestamp", "memory_ts": _iso(NOW - 200 * DAY)}
    assert "[stale]" in _block([row], stale_after_days=90)
    # ...and when NOTHING parses, the record is fresh (fail open)
    row = {**RECORD, "last_validated": "not-a-timestamp", "memory_ts": "also-junk"}
    assert "[stale]" not in _block([row], stale_after_days=90)


def test_a_numeric_epoch_timestamp_is_understood() -> None:
    """The store can echo memory_ts as an epoch (mm-rag playbook._parse_ts)."""
    row = {**RECORD, "last_validated": NOW - 200 * DAY}
    block = _block([row], stale_after_days=90)
    assert "[stale]" in block
    assert "200d old" in block


def test_a_future_timestamp_is_fresh() -> None:
    """Clock skew must never hide a procedure."""
    row = {**RECORD, "last_validated": _iso(NOW + 10 * DAY)}
    assert "[stale]" not in _block([row], stale_after_days=90)


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Both sides of the contract read a naive value as UTC."""
    naive = datetime.fromtimestamp(NOW - 200 * DAY, UTC).replace(tzinfo=None).isoformat()
    assert "[stale]" in _block([{**RECORD, "last_validated": naive}], stale_after_days=90)


def test_the_parsed_record_carries_the_staleness_verdict() -> None:
    stale = {**RECORD, "last_validated": _iso(NOW - 200 * DAY)}
    fresh, withheld = _records([RECORD, stale], stale_after_days=90)
    assert fresh["stale"] is False
    assert withheld["stale"] is True
    assert withheld["stale_days"] == 200
    assert withheld["stale_after_days"] == 90
    assert withheld["stale_field"] == "last_validated"


# ---------------------------------------------------------------------------
# staleness does not let withheld records crowd out (or flood) the block
# ---------------------------------------------------------------------------


def test_withheld_records_do_not_crowd_out_fresh_ones() -> None:
    """The k budget belongs to the INJECTABLE records: a stale store entry must
    not cost a fresh procedure its place in the preamble."""
    rows = [
        {
            **RECORD,
            "playbook_key": "s" + str(i),
            "task_signature": "stale task " + str(i),
            "last_validated": _iso(NOW - 200 * DAY),
        }
        for i in range(3)
    ] + [{**RECORD, "playbook_key": "f" + str(i), "task_signature": "fresh task " + str(i)} for i in range(2)]
    records = _records(rows, k=3, stale_after_days=90)
    fresh = [r for r in records if not r["stale"]]
    withheld = [r for r in records if r["stale"]]
    assert [r["task_signature"] for r in fresh] == ["fresh task 0", "fresh task 1"]
    assert len(withheld) == 3
    block = playbook.render_playbook_block(records)
    assert block.count("[stale]") == 3
    assert "1. check finalizers" in block


def test_withheld_markers_are_bounded() -> None:
    """A store full of stale procedures cannot fill the preamble with markers."""
    rows = [
        {
            **RECORD,
            "playbook_key": "s" + str(i),
            "task_signature": "stale task " + str(i),
            "last_validated": _iso(NOW - 200 * DAY),
        }
        for i in range(6)
    ]
    records = _records(rows, k=10, stale_after_days=90)
    assert len(records) == playbook.STALE_MARKER_MAX
    assert all(r["stale"] for r in records)


def test_a_store_of_only_stale_records_still_renders_the_marker() -> None:
    """No procedure is injected, but the operator still learns WHY."""
    stale = {**RECORD, "last_validated": _iso(NOW - 400 * DAY)}
    block = _block([stale], stale_after_days=90)
    assert block.startswith(playbook.PLAYBOOK_BLOCK_HEADER + "\n")
    assert "[stale]" in block
    assert block.endswith("\n")


def test_a_stale_record_without_an_evidence_ref_says_so_plainly() -> None:
    row = {**RECORD, "last_validated": _iso(NOW - 200 * DAY), "evidence_ref": ""}
    block = _block([row], stale_after_days=90)
    assert "[stale]" in block
    assert "steps withheld)" in block
    assert "evidence" not in block


# ---------------------------------------------------------------------------
# recall_playbook_block threads the operator limit (the relay binding)
# ---------------------------------------------------------------------------


class _Recall:
    """Stub recall client: records calls, returns the scripted envelope."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, task: str, k: int) -> str:
        self.calls.append((task, k))
        return self.answer


def test_recall_playbook_block_threads_the_staleness_limit() -> None:
    row = {**RECORD, "last_validated": _iso(NOW - 200 * DAY)}
    recall = _Recall(_envelope([row]))
    block = asyncio.run(playbook.recall_playbook_block(TASK, recall=recall, stale_after_days=90))
    assert "[stale]" in block
    assert "1. check finalizers" not in block


def test_recall_playbook_block_defaults_to_never_expire() -> None:
    """No limit passed = today behaviour: an undated-by-rule store still injects."""
    row = {**RECORD, "last_validated": _iso(NOW - 5000 * DAY)}
    recall = _Recall(_envelope([row]))
    block = asyncio.run(playbook.recall_playbook_block(TASK, recall=recall))
    assert "[stale]" not in block
    assert "1. check finalizers" in block


def test_the_operator_setting_is_the_90_day_default() -> None:
    """The config knob this feature reads (config.py is the Lead zone; this
    pins the contract the relay binds into recall_playbook_block)."""
    assert settings.playbook_stale_after_days == 90


# ---------------------------------------------------------------------------
# the WRITE half: the evidence reference lands in the memory metadata
# ---------------------------------------------------------------------------


def _doc() -> str:
    """A distilled document carrying a valid Playbook section."""
    body = "## Session summary\n" + ("the operator debugged a stuck pod and verified the reschedule " * 4)
    return body + "\n\n" + playbook.format_playbook_section(PLAYBOOK_OBJ) + "\n"


class _Mcp:
    """Stub add_memory MCP call: records every argument dict it is handed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, args: dict[str, Any]) -> tuple[bool, str]:
        self.calls.append(args)
        return True, "ok"

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if str(call.get("metadata", {}).get("kind") or "") == kind]


def _write(**kwargs: Any) -> tuple[bool, str, _Mcp]:
    """Write the fixture document through the real overlay with a stub MCP."""
    mcp = _Mcp()
    ok, detail = asyncio.run(memory_write.write_memory(None, mcp, _doc(), session_id="conv-42", **kwargs))
    return ok, detail, mcp


def test_written_playbook_metadata_carries_the_evidence_ref() -> None:
    ok, _detail, mcp = _write(request_id="distill-ab12cd34ef56")
    assert ok is True
    (written,) = mcp.of_kind("playbook")
    metadata = written["metadata"]
    assert metadata["evidence_ref"] == "request:distill-ab12cd34ef56"
    assert metadata["evidence_thread"] == "conv-42"
    # the write half still stamps a real validation timestamp (the staleness
    # input for every future recall)
    assert datetime.fromisoformat(str(metadata["last_validated"])).tzinfo is not None


def test_written_playbook_reference_falls_back_to_the_thread() -> None:
    """A caller that cannot supply a request id still gets a usable anchor."""
    _ok, _detail, mcp = _write()
    (written,) = mcp.of_kind("playbook")
    assert written["metadata"]["evidence_ref"] == "thread:conv-42"


def test_evidence_ref_round_trips_from_write_to_parsed_record() -> None:
    """write -> store metadata -> recall envelope -> parsed record, unchanged."""
    _ok, _detail, mcp = _write(request_id="distill-ab12cd34ef56")
    (written,) = mcp.of_kind("playbook")
    records = _records([dict(written["metadata"])])
    assert records[0]["evidence_ref"] == "request:distill-ab12cd34ef56"
    assert records[0]["evidence_thread"] == "conv-42"


def test_a_just_written_playbook_is_fresh() -> None:
    """End to end: the timestamp the write half stamps is what the read half
    judges, so a fresh emission is never withheld."""
    _ok, _detail, mcp = _write(request_id="distill-ab12")
    (written,) = mcp.of_kind("playbook")
    records = playbook.parse_recall_records(_envelope([dict(written["metadata"])]), stale_after_days=90)
    assert [record["stale"] for record in records] == [False]


def test_the_summary_write_is_unchanged_by_the_evidence_ref() -> None:
    """The overlay never changes the summary write: same doc, same kind, and
    the summary body has the Playbook section stripped."""
    ok, detail, mcp = _write(request_id="distill-ab12")
    assert ok is True
    assert detail == "ok"
    (summary,) = mcp.of_kind("project-summary")
    assert "## Playbook" not in summary["text"]
    assert "the operator debugged a stuck pod" in summary["text"]
    assert len(mcp.of_kind("playbook")) == 1
