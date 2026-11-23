"""Deterministic, model-free agent scoring: the five verdicts, their
precedence, the thresholds, and the gated write.

The unit suite never touches Postgres — the module-level `db` handle is
monkeypatched with a fake (repo fake-db style). `score()` is pure, so most of
this file needs no fake at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import pytest

from pcai_llm.config import Settings, settings
from pcai_llm.core import agent_eval
from pcai_llm.db.schema import SCHEMA_SQL


def _chat(iteration: int = 0, status: str = "ok") -> dict[str, Any]:
    return {"iteration": iteration, "phase": "chat", "server": "", "tool": "", "status": status}


def _tool(
    iteration: int = 0,
    server: str = "logsearch",
    tool: str = "search_logs",
    status: str = "ok",
) -> dict[str, Any]:
    return {"iteration": iteration, "phase": "tool", "server": server, "tool": tool, "status": status}


def _verdicts(rows: list[dict[str, Any]], **kwargs: Any) -> list[str]:
    return [v.verdict for v in agent_eval.score(rows, **kwargs)]


class EvalFakeDB:
    """Captures statements; serves canned rows or raises on demand."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError("db down")
        return list(self.rows)

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError("db down")
        return "INSERT 0 1"


# ---------------------------------------------------------------------------
# 1. the five verdicts
# ---------------------------------------------------------------------------


def test_clean_turn_is_a_positive_statement() -> None:
    """A clean turn yields exactly one `clean` row — never an empty list, so a
    caller can never confuse "no findings" with "not scored"."""
    rows = [_chat(0), _tool(0), _chat(1), _tool(1, server="applygate", tool="plan_apply")]
    verdicts = agent_eval.score(rows)
    assert [v.verdict for v in verdicts] == ["clean"]
    assert verdicts[0].verdict == agent_eval.CLEAN
    assert "no findings" in verdicts[0].reason
    detail = json.loads(verdicts[0].detail)
    assert detail == {"steps": 4, "tool_calls": 2}


def test_tool_error_flags_a_failed_call() -> None:
    rows = [_chat(0), _tool(0, status="error")]
    verdicts = agent_eval.score(rows)
    assert [v.verdict for v in verdicts] == ["tool_error"]
    assert "1 tool call(s) did not succeed" == verdicts[0].reason
    assert json.loads(verdicts[0].detail) == {
        "by_status": {"error": 1},
        "calls": {"logsearch__search_logs": 1},
        "count": 1,
    }


def test_gateway_refusals_are_tool_errors_too() -> None:
    """rate_limited / budget_exceeded: the tool did not run, so the call did
    not succeed. An unknown status is equally loud (fail-loud on new vocab)."""
    rows = [
        _tool(0, server="a", tool="t1", status="rate_limited"),
        _tool(1, server="b", tool="t2", status="budget_exceeded"),
        _tool(2, server="c", tool="t3", status="some_future_status"),
    ]
    verdicts = agent_eval.score(rows)
    assert [v.verdict for v in verdicts] == ["tool_error"]
    assert json.loads(verdicts[0].detail)["by_status"] == {
        "budget_exceeded": 1,
        "rate_limited": 1,
        "some_future_status": 1,
    }


def test_handed_back_calls_are_not_tool_errors() -> None:
    """A handback is a CLIENT-executed call (the DSH harness loop) — the
    fleet's primary traffic. Flagging it would make every DSH turn unclean."""
    rows = [_chat(0), _tool(0, server="client", tool="read", status="handed_back")]
    assert _verdicts(rows) == ["clean"]


def test_denied_is_not_granted_and_never_also_a_tool_error() -> None:
    rows = [_chat(0), _tool(0, status="denied")]
    verdicts = agent_eval.score(rows)
    assert [v.verdict for v in verdicts] == ["not_granted"]
    assert "denied by the grant gate" in verdicts[0].reason
    assert json.loads(verdicts[0].detail) == {"calls": {"logsearch__search_logs": 1}, "count": 1}


def test_loop_fires_at_the_threshold() -> None:
    rows = [_chat(0), _tool(0), _chat(1), _tool(1), _chat(2), _tool(2)]
    assert _verdicts(rows, loop_threshold=3) == ["loop"]
    assert _verdicts(rows, loop_threshold=4) == ["clean"]
    loop = agent_eval.score(rows, loop_threshold=3)[0]
    assert "repeated >= 3 times" in loop.reason
    assert json.loads(loop.detail) == {"calls": {"logsearch__search_logs": 3}, "threshold": 3}


def test_loop_does_not_combine_different_tools() -> None:
    """Three calls to three different tools is work, not a loop."""
    rows = [
        _tool(0, server="logsearch", tool="search_logs"),
        _tool(1, server="logsearch", tool="search_logs_v2"),
        _tool(2, server="k8s-ops", tool="pods"),
    ]
    assert _verdicts(rows, loop_threshold=3) == ["clean"]


def test_loop_ignores_client_handbacks() -> None:
    """A harness turn reads five files: five (client, read) rows. The gateway
    did not execute them, so repetition there is normal work."""
    rows = [_tool(i, server="client", tool="read", status="handed_back") for i in range(5)]
    assert _verdicts(rows, loop_threshold=3) == ["clean"]


def test_loop_counts_denied_repeats() -> None:
    """Asking for the same ungranted tool three times IS a loop."""
    rows = [_tool(i, status="denied") for i in range(3)]
    assert _verdicts(rows, loop_threshold=3) == ["not_granted", "loop"]


def test_step_budget_fires_past_the_budget() -> None:
    rows = [_chat(i) for i in range(6)]
    assert _verdicts(rows, max_steps=5) == ["step_budget"]
    assert _verdicts(rows, max_steps=6) == ["clean"]
    verdict = agent_eval.score(rows, max_steps=5)[0]
    assert json.loads(verdict.detail) == {"max_steps": 5, "rounds": 6}


def test_step_budget_counts_distinct_iterations_not_the_absolute_number() -> None:
    """A continued turn starts at iterations_prior > 0: max(iteration)+1 would
    charge it for rounds it never ran."""
    rows = [_chat(10), _tool(10), _chat(11), _tool(11), _chat(12)]
    assert _verdicts(rows, max_steps=3) == ["clean"]
    assert _verdicts(rows, max_steps=2) == ["step_budget"]


def test_thresholds_default_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "agent_eval_max_steps", 2)
    monkeypatch.setattr(settings, "agent_eval_loop_threshold", 2)
    rows = [_tool(0), _tool(1), _chat(2)]
    assert _verdicts(rows) == ["loop", "step_budget"]


def test_zero_threshold_disables_the_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fleet-wide knob convention: 0 = off, NOT "flag everything". A gate
    must not fail every turn because an operator zeroed a knob."""
    rows = [_chat(i) for i in range(50)] + [_tool(i) for i in range(10)]
    assert _verdicts(rows, max_steps=0, loop_threshold=0) == ["clean"]
    monkeypatch.setattr(settings, "agent_eval_max_steps", 0)
    monkeypatch.setattr(settings, "agent_eval_loop_threshold", 0)
    assert _verdicts(rows) == ["clean"]


# ---------------------------------------------------------------------------
# 2. the verdict SET: precedence, ordering, exclusivity
# ---------------------------------------------------------------------------


def test_verdicts_are_ordered_and_compose() -> None:
    """A bad turn can be several things at once; the order is the module
    vocabulary order so two runs (and two replicas) agree."""
    rows = [
        _chat(0),
        _tool(0, status="denied"),
        _tool(1, status="error"),
        _tool(2),
        _tool(2),
        _tool(2),
        _chat(3),
        _chat(4),
        _chat(5),
    ]
    verdicts = agent_eval.score(rows, max_steps=4, loop_threshold=3)
    assert [v.verdict for v in verdicts] == ["not_granted", "tool_error", "loop", "step_budget"]
    assert [v.verdict for v in verdicts] == [v for v in agent_eval.VERDICTS if v != agent_eval.CLEAN]


def test_clean_is_never_emitted_alongside_another_verdict() -> None:
    rows = [_chat(0), _tool(0, status="error")]
    for kwargs in ({}, {"max_steps": 1}, {"loop_threshold": 1}):
        names = _verdicts(rows, **kwargs)
        assert agent_eval.CLEAN not in names


def test_detail_is_canonical_json() -> None:
    """Byte-identical evidence for identical rows: a diff on the verdict table
    is a behaviour change, never a dict-ordering artifact."""
    rows = [_tool(0, status="error", server="b", tool="t"), _tool(1, status="error", server="a", tool="t")]
    first = agent_eval.score(rows)[0].detail
    second = agent_eval.score(list(reversed(rows)))[0].detail
    assert first == second
    assert json.loads(first)["calls"] == {"a__t": 1, "b__t": 1}


def test_score_accepts_record_like_rows() -> None:
    """asyncpg Records expose .get — the scorer must not require dicts, so a
    script can pass the query result straight through."""

    class RecordLike:
        def __init__(self, **fields: Any) -> None:
            self._fields = fields

        def get(self, key: str, default: Any = None) -> Any:
            return self._fields.get(key, default)

    rows = [
        RecordLike(iteration=0, phase="chat", server="", tool="", status="ok"),
        RecordLike(iteration=0, phase="tool", server="s", tool="t", status="error"),
    ]
    assert [v.verdict for v in agent_eval.score(rows)] == ["tool_error"]


def test_score_tolerates_empty_rows() -> None:
    """A request with no traced steps is clean-by-vacuity, not a crash."""
    assert _verdicts([]) == ["clean"]


# ---------------------------------------------------------------------------
# 3. record() / evaluate_turn() — the gated write
# ---------------------------------------------------------------------------


def test_record_does_not_write_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """agent_eval_enabled gates the WRITE, not the analysis: a dry run still
    returns the verdicts."""
    monkeypatch.setattr(settings, "agent_eval_enabled", False)
    fake = EvalFakeDB()
    monkeypatch.setattr(agent_eval, "db", fake)
    verdicts = asyncio.run(agent_eval.record("req-1", [_tool(0, status="error")]))
    assert [v.verdict for v in verdicts] == ["tool_error"]
    assert fake.calls == []


def test_record_writes_one_row_per_verdict_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "agent_eval_enabled", True)
    fake = EvalFakeDB()
    monkeypatch.setattr(agent_eval, "db", fake)
    rows = [_tool(0, status="denied"), _tool(1, status="error")]
    verdicts = asyncio.run(agent_eval.record("req-2", rows))
    assert [v.verdict for v in verdicts] == ["not_granted", "tool_error"]
    assert len(fake.calls) == 2
    for (sql, args), verdict in zip(fake.calls, verdicts, strict=True):
        assert "INSERT INTO agent_eval_verdicts" in sql
        assert "request_id, verdict, reason, detail" in sql
        assert args == ("req-2", verdict.verdict, verdict.reason, verdict.detail)


def test_record_swallows_a_write_failure(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The core.traces.record posture: a verdict-table fault is a WARNING,
    never an exception on the caller's path."""
    monkeypatch.setattr(settings, "agent_eval_enabled", True)
    fake = EvalFakeDB(fail=True)
    monkeypatch.setattr(agent_eval, "db", fake)
    with caplog.at_level(logging.WARNING):
        verdicts = asyncio.run(agent_eval.record("req-3", [_tool(0, status="error")]))
    assert [v.verdict for v in verdicts] == ["tool_error"]
    assert "agent eval verdict write failed" in caplog.text


def test_evaluate_turn_reads_the_trace_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """evaluate_turn owns the READ so a caller cannot score a different row
    set than the one stored."""
    monkeypatch.setattr(settings, "agent_eval_enabled", False)
    rows = [_chat(0), _tool(0, status="error")]
    fake = EvalFakeDB(rows=rows)
    monkeypatch.setattr(agent_eval, "db", fake)
    verdicts = asyncio.run(agent_eval.evaluate_turn("req-4"))
    assert [v.verdict for v in verdicts] == ["tool_error"]
    sql, args = fake.calls[0]
    assert "FROM agent_traces" in sql
    assert "WHERE request_id = $1" in sql
    assert args == ("req-4",)


def test_evaluate_turn_read_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate that reported "clean" because the query failed would be worse
    than a gate that failed loudly: the READ is allowed to raise."""
    fake = EvalFakeDB(fail=True)
    monkeypatch.setattr(agent_eval, "db", fake)
    with pytest.raises(RuntimeError, match="db down"):
        asyncio.run(agent_eval.evaluate_turn("req-5"))


# ---------------------------------------------------------------------------
# 4. schema + settings: the table the scorer writes, and the shipped default
# ---------------------------------------------------------------------------


def test_schema_verdict_table_columns() -> None:
    m = re.search(r"CREATE TABLE IF NOT EXISTS agent_eval_verdicts \(([^;]+)\)", SCHEMA_SQL, re.DOTALL)
    assert m, "agent_eval_verdicts CREATE TABLE missing from SCHEMA_SQL"
    flat = " ".join(m.group(1).split())
    for fragment in (
        "request_id TEXT NOT NULL",
        "verdict TEXT NOT NULL",
        "reason TEXT NOT NULL DEFAULT ''",
        "detail TEXT NOT NULL DEFAULT ''",
        "ts TIMESTAMPTZ NOT NULL DEFAULT now()",
    ):
        assert fragment in flat, f"missing from agent_eval_verdicts DDL: {fragment!r}"


def test_schema_verdict_vocabulary_matches_the_scorer() -> None:
    """The CHECK and the Python vocabulary are the same set — a verdict the
    scorer can emit must never be rejected by the database."""
    for verdict in agent_eval.VERDICTS:
        assert f"''{verdict}''" not in SCHEMA_SQL
        assert f"'{verdict}'" in SCHEMA_SQL, verdict


def test_schema_verdict_indexes() -> None:
    assert "CREATE INDEX IF NOT EXISTS idx_eval_verdicts_request ON agent_eval_verdicts(request_id)" in SCHEMA_SQL
    assert (
        "CREATE INDEX IF NOT EXISTS idx_eval_verdicts_verdict_ts ON agent_eval_verdicts(verdict, ts DESC)" in SCHEMA_SQL
    )


def test_shipped_defaults_are_off() -> None:
    """The scorer writes on the hot path: it must ship OFF, with the documented
    thresholds, until an operator opts in."""
    s = Settings()
    assert s.agent_eval_enabled is False
    assert s.agent_eval_max_steps == 40
    assert s.agent_eval_loop_threshold == 3
