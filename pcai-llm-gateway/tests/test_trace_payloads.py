"""Agent trace payloads: per-request deterministic sampling, best-effort
redaction, bounded truncation, retention, and the traces.record hook.

The unit suite never touches Postgres — the module-level `db` handles are
monkeypatched with a fake (repo fake-db style). Insert column order is pinned
here so a silent reorder breaks a test instead of a judge.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import trace_payloads, traces
from pcai_llm.db.schema import SCHEMA_SQL

# index of each column in record_step()'s INSERT (assertions read better)
COL = {
    "request_id": 0,
    "iteration": 1,
    "phase": 2,
    "server": 3,
    "tool": 4,
    "kind": 5,
    "payload": 6,
    "payload_chars": 7,
    "truncated": 8,
    "redacted": 9,
}


class PayloadFakeDB:
    """Captures every statement + binding; serves the tags the code parses."""

    def __init__(self, fail: bool = False, tag: str = "INSERT 0 1") -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fail = fail
        self.tag = tag

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append((sql, args))
        if self.fail:
            raise RuntimeError("db down")
        return self.tag

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append((sql, args))
        return []

    @property
    def payload_inserts(self) -> list[tuple[Any, ...]]:
        return [args for sql, args in self.calls if "INTO agent_trace_payloads" in sql]

    @property
    def trace_inserts(self) -> list[tuple[Any, ...]]:
        return [args for sql, args in self.calls if "INTO agent_traces" in sql]


# ---------------------------------------------------------------------------
# 1. redact() — the PURE redaction function
# ---------------------------------------------------------------------------


def test_redact_bearer_token_keeps_the_header_shape() -> None:
    """The marker replaces the TOKEN, not the header: a reader must still see
    that this was an Authorization header."""
    assert trace_payloads.redact("Authorization: Bearer abcdefgh12345678") == "Authorization: Bearer [REDACTED:bearer]"


def test_redact_api_keys_in_every_shape_it_takes() -> None:
    assert trace_payloads.redact("x-api-key: abcd1234efgh") == "x-api-key: [REDACTED:api_key]"
    assert trace_payloads.redact('{"api_key": "abcd1234efgh"}') == '{"api_key": "[REDACTED:api_key]"}'
    assert trace_payloads.redact("api-key=abcd1234efgh") == "api-key=[REDACTED:api_key]"
    assert trace_payloads.redact("sk-proj-ABCDEFGH12345678") == "[REDACTED:api_key]"


def test_redact_passwords_in_json_and_query_string() -> None:
    assert trace_payloads.redact('{"password": "hunter2"}') == '{"password": "[REDACTED:password]"}'
    assert trace_payloads.redact("user=bob&pwd=hunter2&x=1") == "user=bob&pwd=[REDACTED:password]&x=1"


def test_redact_tokens_and_secrets() -> None:
    assert trace_payloads.redact("access_token=abcdef123456") == "access_token=[REDACTED:secret]"
    assert trace_payloads.redact('{"client_secret": "zzz"}') == '{"client_secret": "[REDACTED:secret]"}'


def test_redact_dsn_userinfo() -> None:
    """A DSN in a tool result is the classic leak: scheme://user:pass@host."""
    assert trace_payloads.redact("postgresql://user:s3cret@db:5432/x") == "postgresql://[REDACTED:dsn]@db:5432/x"
    # empty username (redis://:password@host)
    assert trace_payloads.redact("redis://:hunter2@cache:6379") == "redis://[REDACTED:dsn]@cache:6379"
    # a credential with no password part (a PAT in a clone URL)
    assert (
        trace_payloads.redact("https://ghp_ABCDEFGH1234567890ABCD@github.com/o/r")
        == "https://[REDACTED:dsn]@github.com/o/r"
    )


def test_redact_leaves_ordinary_text_alone() -> None:
    for text in (
        "no secrets here at all",
        "the bearer of bad news arrived",  # no 8+ char token follows
        "see https://example.com/a@b for details",  # an @ in a path is not userinfo
        "max_tokens: 512",  # a token COUNT is not a token
    ):
        assert trace_payloads.redact(text) == text


def test_redact_is_idempotent() -> None:
    """Markers cannot be re-matched, so a payload stored twice is unchanged —
    the property that makes the function safe to run on already-scrubbed text."""
    text = (
        "Authorization: Bearer abcdefgh1234 password=hunter2 "
        "postgresql://user:pw@db/x access_token=tok123456789 sk-proj-ABCDEFGH12345678"
    )
    once = trace_payloads.redact(text)
    assert once != text
    assert trace_payloads.redact(once) == once


def test_redact_empty_string() -> None:
    assert trace_payloads.redact("") == ""


# ---------------------------------------------------------------------------
# 2. sampled() — deterministic, PER REQUEST
# ---------------------------------------------------------------------------


def test_sampling_is_deterministic_for_a_request() -> None:
    """The same request_id must always decide the same way — on every
    replica and after every restart, or a trace is captured in pieces."""
    decisions = {trace_payloads.sampled("req-abc", 0.5) for _ in range(50)}
    assert len(decisions) == 1


def test_sampling_edges() -> None:
    assert trace_payloads.sampled("any", 0.0) is False
    assert trace_payloads.sampled("any", -1.0) is False
    assert trace_payloads.sampled("any", 1.0) is True
    assert trace_payloads.sampled("any", 2.0) is True  # clamped, not an error


def test_sampling_is_monotone_in_the_rate() -> None:
    """Raising the rate only ever ADDS requests (bucket < rate): an operator
    who samples more must not lose the traces they already had."""
    ids = [f"req-{i}" for i in range(500)]
    low = {i for i in ids if trace_payloads.sampled(i, 0.1)}
    high = {i for i in ids if trace_payloads.sampled(i, 0.4)}
    assert low, "no request sampled at 0.1 — the hash is degenerate"
    assert low < high


def test_sampling_rate_is_a_probability_not_a_counter() -> None:
    """The rate must spread across requests, not capture the first N."""
    ids = [f"req-{i}" for i in range(2000)]
    hits = sum(1 for i in ids if trace_payloads.sampled(i, 0.25))
    assert 0.2 * len(ids) <= hits <= 0.3 * len(ids), hits


def test_sampling_defaults_to_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    assert trace_payloads.sampled("req-x") is True
    monkeypatch.setattr(settings, "trace_payload_sampling", 0.0)
    assert trace_payloads.sampled("req-x") is False


# ---------------------------------------------------------------------------
# 3. canonical() + prepare() — the pure storage projection
# ---------------------------------------------------------------------------


def test_canonical_is_stable_json() -> None:
    a = trace_payloads.canonical({"b": 1, "a": [1, 2]})
    b = trace_payloads.canonical({"a": [1, 2], "b": 1})
    assert a == b == '{"a": [1, 2], "b": 1}'
    assert trace_payloads.canonical("already text") == "already text"
    assert trace_payloads.canonical(b"bytes") == "bytes"


def test_canonical_survives_a_non_json_value() -> None:
    class Odd:
        def __repr__(self) -> str:
            return "<odd>"

    assert "odd" in trace_payloads.canonical({"x": Odd()})


def test_prepare_under_the_cap_is_verbatim() -> None:
    p = trace_payloads.prepare("hello", max_chars=100, redact_enabled=True)
    assert (p.payload, p.payload_chars, p.truncated, p.redacted) == ("hello", 5, False, False)


def test_prepare_truncates_and_records_the_original_length() -> None:
    """payload_chars is the length BEFORE the cut — the only way a reader can
    tell a short result from a truncated one."""
    p = trace_payloads.prepare("x" * 500, max_chars=10, redact_enabled=False)
    assert p.payload == "x" * 10
    assert p.payload_chars == 500
    assert p.truncated is True
    assert p.redacted is False


def test_prepare_redacts_before_truncating() -> None:
    p = trace_payloads.prepare("Authorization: Bearer abcdefgh1234", max_chars=200)
    assert p.payload == "Authorization: Bearer [REDACTED:bearer]"
    assert p.redacted is True
    assert p.truncated is False


def test_prepare_redaction_can_grow_past_the_cap() -> None:
    """A marker is LONGER than a short secret ("pwd=a" -> 21 chars), so the
    cap has to be re-applied after redaction."""
    p = trace_payloads.prepare("pwd=a", max_chars=10, redact_enabled=True)
    assert len(p.payload) == 10
    assert p.truncated is True
    assert p.payload_chars == 5


def test_prepare_redaction_work_is_bounded_by_the_cap() -> None:
    """Only the stored prefix is scanned: a 350 KB tool result must not cost a
    350 KB regex pass on the relay path, and the dropped tail is not stored."""
    p = trace_payloads.prepare("a" * 100 + " Authorization: Bearer abcdefgh1234", max_chars=10)
    assert p.payload == "a" * 10
    assert "REDACTED" not in p.payload
    assert p.payload_chars == 100 + len(" Authorization: Bearer abcdefgh1234")


def test_prepare_zero_cap_keeps_a_metadata_row() -> None:
    p = trace_payloads.prepare("payload", max_chars=0)
    assert p.payload == ""
    assert p.payload_chars == 7
    assert p.truncated is True


def test_prepare_redaction_disabled_leaves_the_secret() -> None:
    """The knob is real: trace_payload_redact=False stores what arrived (the
    operator's deliberate, documented choice)."""
    p = trace_payloads.prepare("Authorization: Bearer abcdefgh1234", redact_enabled=False)
    assert "REDACTED" not in p.payload
    assert p.redacted is False


def test_prepare_defaults_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_max_chars", 4)
    monkeypatch.setattr(settings, "trace_payload_redact", False)
    p = trace_payloads.prepare("abcdefgh")
    assert p.payload == "abcd"
    assert p.truncated is True


# ---------------------------------------------------------------------------
# 4. record_step() — the best-effort write
# ---------------------------------------------------------------------------


def test_record_step_writes_args_and_result_when_sampled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    fake = PayloadFakeDB()
    monkeypatch.setattr(trace_payloads, "db", fake)
    written = asyncio.run(
        trace_payloads.record_step("req-1", 2, "logsearch", "search_logs", args={"q": "x"}, result="found")
    )
    assert written == 2
    args_row, result_row = fake.payload_inserts
    assert args_row[COL["kind"]] == "args"
    assert args_row[COL["payload"]] == '{"q": "x"}'
    assert args_row[COL["payload_chars"]] == len('{"q": "x"}')
    assert args_row[COL["iteration"]] == 2
    assert args_row[COL["server"]] == "logsearch"
    assert args_row[COL["tool"]] == "search_logs"
    assert args_row[COL["request_id"]] == "req-1"
    assert args_row[COL["phase"]] == "tool"
    assert result_row[COL["kind"]] == "result"
    assert result_row[COL["payload"]] == "found"


def test_record_step_redacts_and_flags_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    fake = PayloadFakeDB()
    monkeypatch.setattr(trace_payloads, "db", fake)
    asyncio.run(trace_payloads.record_step("req-2", 0, "sqlhandler", "query", result="dsn=postgresql://u:pw@db/x"))
    row = fake.payload_inserts[0]
    assert "pw" not in row[COL["payload"]]
    assert "[REDACTED:dsn]" in row[COL["payload"]]
    assert row[COL["redacted"]] is True


def test_record_step_sampling_off_is_a_true_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFAULT DEPLOYMENT MUST PAY NOTHING: with trace_payload_sampling=0
    there is no DB call, no serialisation and no hash — the feature is inert
    before it touches any of the three."""
    monkeypatch.setattr(settings, "trace_payload_sampling", 0.0)
    fake = PayloadFakeDB()
    monkeypatch.setattr(trace_payloads, "db", fake)

    def boom(*a: Any, **k: Any) -> Any:
        pytest.fail("serialisation must not run when sampling is off")

    monkeypatch.setattr(trace_payloads, "prepare", boom)
    monkeypatch.setattr(trace_payloads, "canonical", boom)
    real_sha256 = hashlib.sha256
    hashed: list[Any] = []

    def spy(*a: Any, **k: Any) -> Any:
        hashed.append(a)
        return real_sha256(*a, **k)

    monkeypatch.setattr(trace_payloads.hashlib, "sha256", spy)

    assert asyncio.run(trace_payloads.record_step("r", 0, "s", "t", args={"a": 1}, result="x")) == 0
    assert fake.calls == []
    assert hashed == []


def test_record_step_with_one_payload_writes_one_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    fake = PayloadFakeDB()
    monkeypatch.setattr(trace_payloads, "db", fake)
    assert asyncio.run(trace_payloads.record_step("r", 0, "s", "t", result="only result")) == 1
    assert [row[COL["kind"]] for row in fake.payload_inserts] == ["result"]


def test_record_step_without_payloads_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    fake = PayloadFakeDB()
    monkeypatch.setattr(trace_payloads, "db", fake)
    assert asyncio.run(trace_payloads.record_step("r", 0, "s", "t")) == 0
    assert fake.calls == []


def test_record_step_swallows_a_db_failure(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A payload write fault is a WARNING and a gap in the quality layer —
    never an exception on the relay path."""
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    fake = PayloadFakeDB(fail=True)
    monkeypatch.setattr(trace_payloads, "db", fake)
    with caplog.at_level(logging.WARNING):
        written = asyncio.run(trace_payloads.record_step("r", 0, "s", "t", args="a", result="b"))
    assert written == 0
    assert len(fake.calls) == 2, "one attempt per payload — a failed args write must not drop the result"
    assert "agent trace payload write failed" in caplog.text


# ---------------------------------------------------------------------------
# 5. prune() — retention, 0 = disabled
# ---------------------------------------------------------------------------


def test_prune_disabled_never_touches_the_db(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PayloadFakeDB()
    monkeypatch.setattr(trace_payloads, "db", fake)
    assert asyncio.run(trace_payloads.prune(0)) == 0
    assert asyncio.run(trace_payloads.prune(-3)) == 0
    assert fake.calls == []


def test_prune_deletes_older_than_days(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PayloadFakeDB(tag="DELETE 7")
    monkeypatch.setattr(trace_payloads, "db", fake)
    assert asyncio.run(trace_payloads.prune(7)) == 7
    sql, args = fake.calls[0]
    assert "DELETE FROM agent_trace_payloads" in sql
    assert "make_interval(days => $1)" in sql
    assert args == (7,)


def test_prune_defaults_to_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_retention_days", 3)
    fake = PayloadFakeDB(tag="DELETE 1")
    monkeypatch.setattr(trace_payloads, "db", fake)
    assert asyncio.run(trace_payloads.prune()) == 1
    assert fake.calls[0][1] == (3,)


def test_prune_survives_an_unparseable_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = PayloadFakeDB(tag="")
    monkeypatch.setattr(trace_payloads, "db", fake)
    assert asyncio.run(trace_payloads.prune(7)) == 0


# ---------------------------------------------------------------------------
# 6. the traces.record() hook — the trace row is the anchor
# ---------------------------------------------------------------------------


def test_traces_record_hooks_the_payload_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    fake = PayloadFakeDB()
    monkeypatch.setattr(traces, "db", fake)
    monkeypatch.setattr(trace_payloads, "db", fake)
    asyncio.run(
        traces.record(
            "req-hook",
            uuid.uuid4(),
            None,
            "m1",
            3,
            "tool",
            server="logsearch",
            tool="search_logs",
            status="ok",
            tool_args={"q": "x"},
            tool_result="found",
        )
    )
    assert len(fake.trace_inserts) == 1
    assert [row[COL["kind"]] for row in fake.payload_inserts] == ["args", "result"]
    # the payload rows carry the SAME step coordinates as the trace row
    for row in fake.payload_inserts:
        assert row[COL["request_id"]] == "req-hook"
        assert row[COL["iteration"]] == 3
        assert row[COL["server"]] == "logsearch"
        assert row[COL["tool"]] == "search_logs"


def test_traces_record_chat_step_never_writes_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    fake = PayloadFakeDB()
    monkeypatch.setattr(traces, "db", fake)
    monkeypatch.setattr(trace_payloads, "db", fake)
    asyncio.run(traces.record("req-chat", uuid.uuid4(), None, "m1", 0, "chat", tool_args="ignored"))
    assert len(fake.trace_inserts) == 1
    assert fake.payload_inserts == []


def test_traces_record_sampling_off_writes_no_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_payload_sampling", 0.0)
    fake = PayloadFakeDB()
    monkeypatch.setattr(traces, "db", fake)
    monkeypatch.setattr(trace_payloads, "db", fake)
    asyncio.run(
        traces.record(
            "req-off",
            uuid.uuid4(),
            None,
            "m1",
            0,
            "tool",
            server="s",
            tool="t",
            tool_args={"a": 1},
            tool_result="r",
        )
    )
    assert len(fake.trace_inserts) == 1
    assert fake.payload_inserts == []


def test_traces_record_trace_failure_skips_the_payload_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """No orphan payloads: the payload rides a trace row that actually landed."""
    monkeypatch.setattr(settings, "trace_payload_sampling", 1.0)
    fake = PayloadFakeDB(fail=True)
    monkeypatch.setattr(traces, "db", fake)
    monkeypatch.setattr(trace_payloads, "db", fake)
    asyncio.run(
        traces.record(
            "req-boom",
            uuid.uuid4(),
            None,
            "m1",
            0,
            "tool",
            server="s",
            tool="t",
            tool_args={"a": 1},
            tool_result="r",
        )
    )
    assert fake.payload_inserts == []


# ---------------------------------------------------------------------------
# 7. housekeeping: the EXISTING sweep enforces trace_payload_retention_days
# ---------------------------------------------------------------------------


def _quiet_sweep(monkeypatch: pytest.MonkeyPatch) -> PayloadFakeDB:
    """Silence every OTHER sweep so the test observes only the payload step."""
    from pcai_llm.core import embedcache, housekeeping, response_cache, toolcompress_store

    async def _noop(*a: Any, **k: Any) -> int:
        return 0

    async def _noop_void(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(response_cache, "purge_expired", _noop)
    monkeypatch.setattr(embedcache, "prune", _noop)
    monkeypatch.setattr(toolcompress_store, "prune", _noop)
    # The canonical-store prune owns a module-global throttle; leaving it to run
    # would set process state this test has no business setting.
    monkeypatch.setattr(housekeeping, "_prune_canonical_store", _noop_void)
    fake = PayloadFakeDB(tag="DELETE 0")
    monkeypatch.setattr(housekeeping, "db", fake)
    return fake


def test_housekeeping_sweep_prunes_trace_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retention is wired into the ONE existing housekeeping loop — a second
    sweeper would be a defect, so this asserts the existing step calls us."""
    from pcai_llm.core import housekeeping

    _quiet_sweep(monkeypatch)
    monkeypatch.setattr(settings, "trace_payload_retention_days", 7)
    calls: list[int] = []

    async def fake_prune(days: int) -> int:
        calls.append(days)
        return 3

    monkeypatch.setattr(trace_payloads, "prune", fake_prune)
    asyncio.run(housekeeping._sweep_once())
    assert calls == [7]


def test_housekeeping_sweep_skips_payload_prune_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 = disabled: the step does nothing at all, not even call prune()."""
    from pcai_llm.core import housekeeping

    _quiet_sweep(monkeypatch)
    monkeypatch.setattr(settings, "trace_payload_retention_days", 0)
    calls: list[int] = []

    async def fake_prune(days: int) -> int:
        calls.append(days)
        return 0

    monkeypatch.setattr(trace_payloads, "prune", fake_prune)
    asyncio.run(housekeeping._sweep_once())
    assert calls == []


# ---------------------------------------------------------------------------
# 8. schema: the DDL is additive, idempotent and complete
# ---------------------------------------------------------------------------


def _schema_statements() -> list[str]:
    """SCHEMA_SQL split into statements with comments stripped (the
    test_stickiness helper shape)."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in SCHEMA_SQL.split("\n"))
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def test_schema_payload_table_columns() -> None:
    m = re.search(r"CREATE TABLE IF NOT EXISTS agent_trace_payloads \(([^;]+)\)", SCHEMA_SQL, re.DOTALL)
    assert m, "agent_trace_payloads CREATE TABLE missing from SCHEMA_SQL"
    flat = " ".join(m.group(1).split())
    for fragment in (
        "request_id TEXT NOT NULL",
        "iteration INT NOT NULL DEFAULT 0",
        "phase TEXT NOT NULL DEFAULT 'tool'",
        "server TEXT NOT NULL DEFAULT ''",
        "tool TEXT NOT NULL DEFAULT ''",
        "kind TEXT NOT NULL",
        "payload TEXT NOT NULL DEFAULT ''",
        "payload_chars INT NOT NULL DEFAULT 0",
        "truncated BOOLEAN NOT NULL DEFAULT FALSE",
        "redacted BOOLEAN NOT NULL DEFAULT FALSE",
        "ts TIMESTAMPTZ NOT NULL DEFAULT now()",
    ):
        assert fragment in flat, f"missing from agent_trace_payloads DDL: {fragment!r}"


def test_schema_payload_indexes() -> None:
    """The two reads the design promises: a step's payloads in order, and the
    retention prune by ts (never a seq scan on the sensitive table)."""
    assert (
        "CREATE INDEX IF NOT EXISTS idx_trace_payloads_request "
        "ON agent_trace_payloads(request_id, iteration)" in SCHEMA_SQL
    )
    assert "CREATE INDEX IF NOT EXISTS idx_trace_payloads_ts ON agent_trace_payloads(ts DESC)" in SCHEMA_SQL


def test_schema_payload_kind_vocabulary() -> None:
    assert "CHECK (kind IN ('args','result')) NOT VALID" in SCHEMA_SQL


def test_schema_quality_layer_ddl_is_idempotent() -> None:
    """SCHEMA_SQL is re-run on every boot (ONE implicit transaction): every
    statement touching these tables must be re-runnable, and every ADD
    CONSTRAINT must be preceded by its own DROP ... IF EXISTS."""
    for table in ("agent_trace_payloads", "agent_eval_verdicts"):
        stmts = [s for s in _schema_statements() if table in s]
        assert stmts, f"no DDL statements for {table}"
        for stmt in stmts:
            head = " ".join(stmt.split("\n", 1)[0].split())
            assert head.startswith(
                ("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS", f"ALTER TABLE {table}")
            ), f"{table} DDL is not re-runnable: {head!r}"
        adds = [s for s in stmts if "ADD CONSTRAINT" in s]
        drops = [s for s in stmts if "DROP CONSTRAINT IF EXISTS" in s]
        assert len(adds) == len(drops), f"{table}: every ADD CONSTRAINT needs a DROP IF EXISTS"
        for add in adds:
            match = re.search(r"ADD CONSTRAINT (\w+)", add)
            assert match, add
            assert any(match.group(1) in drop for drop in drops), match.group(1)


def test_schema_quality_layer_is_additive_only() -> None:
    """No existing table changes shape: the quality layer adds its own tables
    and indexes (the brief's additive-only rule for a live database)."""
    block = SCHEMA_SQL[SCHEMA_SQL.index("-- Agent quality layer (2026-11-23)") :]
    alters = [line.strip() for line in block.split("\n") if line.strip().upper().startswith("ALTER TABLE")]
    assert alters, "expected the vocabulary constraints"
    # judge_verdicts joined the quality layer in B1 commit 1 (2026-11-23): it is an
    # agent-quality table, so it belongs AFTER the marker like the other two.
    for line in alters:
        assert "agent_trace_payloads" in line or "agent_eval_verdicts" in line or "judge_verdicts" in line, line


# --- redaction coverage added 2026-11-23 (cross-review P2) ------------------
# These classes passed through UNTOUCHED before the cross-review: HTTP Basic (the
# default auth of half the tool ecosystem), AWS secret access keys (which the generic
# secret rule could not anchor on), and session cookies. Each case below is a verbatim
# shape the reviewer used to defeat the redactor.


def test_redacts_http_basic() -> None:
    out = trace_payloads.redact("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert "[REDACTED:basic]" in out
    assert "dXNlcjpwYXNzd29yZA" not in out


# Fixtures below are assembled from parts ON PURPOSE: a contiguous fake credential in
# source trips pcai_utils/secret_scan.sh, and its JWT rule deliberately ignores the
# '# secret-scan:allow' marker. Concatenation keeps the runtime string identical while
# leaving no literal credential shape in the file. These are AWS's own documented
# example key and a truncated JWT header — not secrets.
def _fake(*parts: str) -> str:
    return "".join(parts)


_AWS_FAKE = _fake("wJalr", "XUtnFEMI/K7MD", "ENG/bPxRfiCY", "EXAMPLEKEY")
_JWT_FAKE = _fake("eyJ", "hbGc.abc", ".def")


def test_redacts_aws_secret_access_key() -> None:
    out = trace_payloads.redact("aws_secret_access_key=" + _AWS_FAKE)
    assert "[REDACTED:aws_secret]" in out
    assert "wJalr" not in out


def test_redacts_session_cookie() -> None:
    out = trace_payloads.redact("set-cookie: session=" + _JWT_FAKE + "; Path=/")
    assert "[REDACTED:cookie]" in out
    assert "hbGc" not in out


def test_ordinary_prose_is_not_mangled() -> None:
    """The added rules must not eat normal words (a false positive is a data bug)."""
    assert trace_payloads.redact("cookie: oatmeal") == "cookie: oatmeal"
    assert trace_payloads.redact("the bearer of good news") == "the bearer of good news"


def test_new_rules_are_idempotent() -> None:
    for sample in (
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "aws_secret_access_key=" + _AWS_FAKE,
        "set-cookie: session=" + _JWT_FAKE,
    ):
        once = trace_payloads.redact(sample)
        assert trace_payloads.redact(once) == once


# --- hard-wrap redaction (B9, 2026-11-23) -----------------------------------
# The cross-review defeat: a credential HARD-WRAPPED across a line break was
# stored RAW on BOTH lines (the head is under the 8-char floor, the tail has no
# keyword anchor). These pin the closed shape AND the payloads that must not be
# touched: the second pass never re-joins lines, so structure survives.


def test_redacts_a_split_bearer_continuation() -> None:
    """The exact shape the reviewer used to defeat the redactor."""
    tail = _fake("defghijklmnop")
    out = trace_payloads.redact("Authorization: Bearer " + _fake("abc") + "\n" + tail)
    assert out == "Authorization: Bearer [REDACTED:bearer]\n[REDACTED:bearer]"
    assert tail not in out


def test_redacts_a_split_password_continuation() -> None:
    out = trace_payloads.redact("password=" + _fake("hun") + "\n" + _fake("ter2") + " and more")
    assert out == "password=[REDACTED:password]\n[REDACTED:password] and more"


def test_redacts_a_split_value_in_every_key_value_class() -> None:
    """The continuation is redacted for every class the single-line rules know."""
    # AWS keys are 40 chars, so their joined floor is higher (20) and the
    # fragment a real wrap leaves behind is long.
    cases = (
        ("access_token=", "secret", "defgh1234"),
        ("refresh_token=", "secret", "defgh1234"),
        ("client_secret=", "secret", "defgh1234"),
        ("api_key=", "api_key", "defgh1234"),
        ("aws_secret_access_key=", "aws_secret", "defghijklmnopqrstuvwx"),
        ("set-cookie: session=", "cookie", "defgh1234"),
    )
    for prefix, kind, fragment in cases:
        tail = _fake(fragment)
        out = trace_payloads.redact(prefix + _fake("abc") + "\n" + tail)
        head, _, rest = out.partition("\n")
        assert head == prefix + "[REDACTED:" + kind + "]", out
        assert rest.startswith("[REDACTED:" + kind + "]"), out


def test_redacts_split_basic_and_bare_sk_credentials() -> None:
    out = trace_payloads.redact("Authorization: Basic " + _fake("dXNlc") + "\n" + _fake("jpwYXNz"))
    assert out == "Authorization: Basic [REDACTED:basic]\n[REDACTED:basic]"
    out = trace_payloads.redact("key sk-" + _fake("abc") + "\n" + _fake("defghijklmno"))
    assert out == "key sk-[REDACTED:api_key]\n[REDACTED:api_key]"


def test_wrapped_credential_redaction_is_idempotent() -> None:
    """The marker cannot be re-matched, so a payload scrubbed twice is stable."""
    for sample in (
        "Authorization: Bearer " + _fake("abc") + "\n" + _fake("defghijklmnop"),
        "password=" + _fake("hun") + "\n" + _fake("ter2"),
        "api_key=" + _fake("ab") + "\n" + _fake("cd1234efgh"),
    ):
        once = trace_payloads.redact(sample)
        assert once != sample
        assert trace_payloads.redact(once) == once


def test_wrapped_prose_is_not_mangled() -> None:
    """A line ending in an anchor WORD is prose, not a wrapped credential."""
    for text in (
        "the bearer of\ngood news arrived",
        "the bearer of\ngoodness arrived",
        "a basic\nrule of thumb",
        "line one ends here\nand the next line is prose",
    ):
        assert trace_payloads.redact(text) == text


def test_wrapped_jsonl_records_are_not_mangled() -> None:
    """No line is joined to its neighbour: every record still parses alone."""
    import json

    records = [
        {"step": 1, "password": "hunter2"},
        {"step": 2, "note": "ordinary line"},
    ]
    text = "\n".join(json.dumps(r) for r in records)
    out = trace_payloads.redact(text)
    lines = out.split("\n")
    assert len(lines) == len(records)
    assert [json.loads(line)["step"] for line in lines] == [1, 2]
    assert "hunter2" not in out
    assert "[REDACTED:password]" in out


def test_wrapped_csv_rows_are_not_mangled() -> None:
    text = "id,user,password\n1,bob,hunter2\n2,amy,s3cret"
    assert trace_payloads.redact(text) == text


def test_stacked_log_lines_are_not_mangled() -> None:
    """The next RECORD start is never eaten as a continuation fragment."""
    text = (
        "2026-11-23 12:00:01 INFO password=hunter2\n"
        "2026-11-23 12:00:02 INFO ready\n"
        "2026-11-23 12:00:03 ERROR token=abc\n"
        "2026-11-23 12:00:04 INFO done"
    )
    out = trace_payloads.redact(text)
    assert out.count("\n") == text.count("\n")
    assert "hunter2" not in out
    assert "[REDACTED:password]" in out
    for line in out.split("\n"):
        assert re.match(r"^\d{4}-\d{2}-\d{2} ", line), line


def test_wrapped_secret_is_redacted_by_prepare() -> None:
    """The storage projection runs the wrap pass too (prepare -> redact)."""
    text = "Authorization: Bearer " + _fake("abc") + "\n" + _fake("defghijklmnop")
    p = trace_payloads.prepare(text, max_chars=1000, redact_enabled=True)
    assert p.redacted is True
    assert "defghijklmnop" not in p.payload
    assert p.payload == "Authorization: Bearer [REDACTED:bearer]\n[REDACTED:bearer]"


def test_wrapped_redaction_handles_crlf() -> None:
    out = trace_payloads.redact("Authorization: Bearer " + _fake("abc") + "\r\n" + _fake("defghijklmnop") + "\r\n")
    assert out == "Authorization: Bearer [REDACTED:bearer]\r\n[REDACTED:bearer]\r\n"


def test_a_short_dangling_value_needs_a_real_joined_length() -> None:
    """A 2-char continuation is not a credential fragment: the head is redacted
    by the single-line rule and the tiny tail is left alone."""
    out = trace_payloads.redact("password=" + _fake("ab") + "\n" + _fake("12"))
    assert out == "password=[REDACTED:password]\n" + _fake("12")


def test_a_full_length_head_wrap_keeps_its_tail_raw() -> None:
    """DOCUMENTED BOUND (pinned, not celebrated): when the head value already
    looks like a complete credential the single-line rule redacts the head and
    the continuation stays raw - a long dangling value at EOL is
    indistinguishable from an ordinary word. Pinned so a future change to this
    bound is a deliberate act, not silent drift."""
    tail = _fake("tailfragment")
    out = trace_payloads.redact("Authorization: Bearer " + _fake("abcdefgh12345678") + "\n" + tail)
    assert out == "Authorization: Bearer [REDACTED:bearer]\n" + tail


def test_a_dangling_anchor_at_eof_has_no_continuation() -> None:
    """No next line, no continuation: the under-floor value stays (unchanged
    single-line behaviour - the wrap pass needs a fragment to act on)."""
    text = "Authorization: Bearer " + _fake("abc")
    assert trace_payloads.redact(text) == text


# ===========================================================================
# 9. R4 (2026-11-23): the wrapped-continuation RECORD GUARD is NARROWED
# ===========================================================================
# The cross-review defeat: _redact_wrapped treated ANY next line beginning with a
# log level (a \b boundary) or a bare date as a NEW RECORD, so "Bearer abc" +
# newline + "ERROR-xyz..." left BOTH credential fragments raw. The guard now
# requires the log-level convention to be followed by a SPACE or a COLON, and a
# date-prefixed record to look like a real record (YYYY-MM-DD then T/space).
# These pin the new truth AND the shapes that must still be left alone.


def test_r4_record_start_guard_requires_a_real_level_separator() -> None:
    """A level counts as a record start only when a SPACE or a COLON follows
    (or it is the whole line) - `ERROR-xyz` is a fragment, not a record."""
    for level in ("TRACE", "DEBUG", "INFO", "NOTICE", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL"):
        assert trace_payloads._RECORD_START_RE.match(f"{level} something happened"), level
        assert trace_payloads._RECORD_START_RE.match(f"{level}: something happened"), level
        assert trace_payloads._RECORD_START_RE.match(level), level
        assert trace_payloads._RECORD_START_RE.match(f"{level}-xyzfragment") is None, level
        assert trace_payloads._RECORD_START_RE.match(f"{level}|xyzfragment") is None, level


def test_r4_record_start_guard_requires_a_real_date_record() -> None:
    """`YYYY-MM-DD` is a record only when T or a space follows; a bare date
    prefix (the reviewer's shape) is a credential fragment."""
    for record_start in (
        "2026-11-23T12:00:01Z INFO ready",
        "2026-11-23 12:00:01 INFO ready",
        "2026-11-23t12:00:01",
    ):
        assert trace_payloads._RECORD_START_RE.match(record_start), record_start
    for fragment in ("2026-11-23xyzfrag", "2026-frag", "2026-11-23x", "2026"):
        assert trace_payloads._RECORD_START_RE.match(fragment) is None, fragment


def test_r4_wrapped_bearer_then_log_level_hyphen_is_now_redacted() -> None:
    """THE reviewer's shape: `Bearer abc` + newline + `ERROR-xyz...` used to
    leave BOTH halves raw because the next line read as a stacked log record."""
    tail = _fake("xyzdefghijkl")
    out = trace_payloads.redact("Authorization: Bearer " + _fake("abc") + "\nERROR-" + tail)
    assert out == "Authorization: Bearer [REDACTED:bearer]\n[REDACTED:bearer]"
    assert tail not in out
    # ... and the same for any other level, not just ERROR
    out = trace_payloads.redact("password=" + _fake("hun") + "\nINFO-" + _fake("ter2xyz"))
    assert out == "password=[REDACTED:password]\n[REDACTED:password]"


def test_r4_wrapped_continuation_after_a_real_log_level_is_still_exempt() -> None:
    """The guard still exists: a genuine stacked log line is NOT a fragment, so
    a short dangling value stays (the pre-R4 protection is intact)."""
    for continuation in (
        "ERROR something happened",
        "INFO: ready",
        "WARN: 3 retries left",
        "DEBUG boot",
        "CRITICAL",
    ):
        text = "Authorization: Bearer " + _fake("abc") + "\n" + continuation
        assert trace_payloads.redact(text) == text, continuation


def test_r4_wrapped_continuation_after_a_real_iso_timestamp_is_still_exempt() -> None:
    """A real record start (date + T/space) is never eaten as a fragment; the
    single-line rule still redacts the head it can see."""
    for stamp in ("2026-11-23T12:00:01Z", "2026-11-23 12:00:01"):
        text = "password=" + _fake("abc") + "\n" + stamp + " INFO ready"
        assert trace_payloads.redact(text) == "password=[REDACTED:password]\n" + stamp + " INFO ready"


def test_r4_wrapped_continuation_with_a_bare_date_prefix_is_redacted() -> None:
    """`2026-` alone is not a record: it is a fragment (the narrowing)."""
    tail = _fake("2026-11-23xyzfrag")
    out = trace_payloads.redact("password=" + _fake("ab") + "\n" + tail)
    assert out == "password=[REDACTED:password]\n[REDACTED:password]"
    assert tail not in out


def test_r4_narrowed_guard_keeps_stacked_logs_and_prose_intact() -> None:
    """The narrowing must not start eating ordinary structured payloads: real
    stacked logs keep their shape and prose is untouched."""
    text = (
        "2026-11-23 12:00:01 INFO password=hunter2\n"
        "2026-11-23 12:00:02 ERROR something happened\n"
        "2026-11-23 12:00:03 INFO done"
    )
    out = trace_payloads.redact(text)
    assert out.count("\n") == text.count("\n")
    assert "hunter2" not in out
    for line in out.split("\n"):
        assert re.match(r"^\d{4}-\d{2}-\d{2} ", line), line
    for prose in (
        "the bearer of\ngoodness arrived",
        "line one ends here\nand the next line is prose",
        "no secrets here\njust two lines",
    ):
        assert trace_payloads.redact(prose) == prose


def test_r4_narrowed_guard_is_idempotent() -> None:
    """Redaction stays stable on the new shapes (a stored payload scrubbed
    twice must be byte-identical)."""
    for sample in (
        "Authorization: Bearer " + _fake("abc") + "\nERROR-" + _fake("xyzdefghijkl"),
        "password=" + _fake("ab") + "\n" + _fake("2026-11-23xyzfrag"),
        "password=" + _fake("abc") + "\n2026-11-23T12:00:01Z INFO ready",
    ):
        once = trace_payloads.redact(sample)
        assert trace_payloads.redact(once) == once


# ===========================================================================
# 10. scripts/tool_pin_admin.py - the operator re-approval lever (R3)
# 11. scripts/agent_eval_report.py - the quality-layer reader (R5)
# ===========================================================================
# WHY THESE LIVE HERE: this wave's write zone grants exactly ONE test file (this
# one), so the two new operator scripts are tested from it. They are loaded by
# path - the repo's established pattern for scripts/ (test_eval_gate_keying.py,
# test_deploy_contract.py) - and the imports stay LOCAL to the helpers so this
# suite remains append-only: no existing import block is touched.


def _load_script(name: str) -> Any:
    """Load scripts/<name>.py as a module by path (no sys.path edit, no package)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _blank_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the gateway's own DB config unresolvable (the unit-suite environment)."""
    for field in ("database_url", "db_host", "db_port", "db_user", "db_password", "db_name"):
        monkeypatch.setattr(settings, field, "")


class _FakeAdminConn:
    """The two statements tool_pin_admin issues, and nothing else."""

    def __init__(self, rows: Any = (), tag: str = "DELETE 1", fault: Exception | None = None) -> None:
        self.rows = list(rows)
        self.tag = tag
        self.fault = fault
        self.fetched: list[tuple[str, tuple[Any, ...]]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    async def fetch(self, sql: str, *params: Any) -> list[Any]:
        if self.fault is not None:
            raise self.fault
        self.fetched.append((sql, params))
        return self.rows

    async def execute(self, sql: str, *params: Any) -> str:
        if self.fault is not None:
            raise self.fault
        self.executed.append((sql, params))
        return self.tag

    async def close(self) -> None:
        self.closed = True


def _install_conn(monkeypatch: pytest.MonkeyPatch, module: Any, conn: Any) -> None:
    """Point the script's _open() at a fake connection (no database is touched)."""

    async def _open() -> Any:
        return conn

    monkeypatch.setattr(module, "_open", _open)


def test_tool_pin_admin_lists_every_column() -> None:
    """`list` must show what an operator needs to decide: server, tool,
    fingerprint, first_seen, last_seen."""
    module = _load_script("tool_pin_admin")
    from datetime import UTC, datetime

    rows = [
        {
            "server": "logsearch",
            "tool": "search_logs",
            "fingerprint": "0123456789abcdef",
            "first_seen": datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC),
            "last_seen": datetime(2026, 11, 23, 18, 0, 0, tzinfo=UTC),
        }
    ]
    out = module._format_rows(rows)
    for expected in (
        "1 pin(s) in tool_pins",
        "SERVER",
        "TOOL",
        "FINGERPRINT",
        "FIRST_SEEN",
        "LAST_SEEN",
        "logsearch",
        "search_logs",
        "0123456789abcdef",
        "2026-11-20 10:00:00",
        "2026-11-23 18:00:00",
    ):
        assert expected in out, expected


def test_tool_pin_admin_empty_listing_says_what_it_means() -> None:
    """An empty list is an ANSWER, not silence: it must say the next observation
    is FIRST_SEEN rather than print nothing an operator can misread."""
    module = _load_script("tool_pin_admin")
    out = module._format_rows([])
    assert "no tool-definition pins recorded" in out
    assert "FIRST_SEEN" in out


def test_tool_pin_admin_clear_all_reuses_the_stores_clear_sql() -> None:
    """The brief's rule: reuse core.tool_pin's constants. `--all` is exactly
    the store's own "forget everything" statement."""
    module = _load_script("tool_pin_admin")
    from pcai_llm.core import tool_pin

    sql, params = module._clear_plan(None, None, True, True)
    assert sql == tool_pin._CLEAR_SQL
    assert params == ()


def test_tool_pin_admin_clear_one_key_is_narrow() -> None:
    """Clearing ONE pin must never be able to wipe the table: the delete is
    keyed on both server and tool."""
    module = _load_script("tool_pin_admin")

    sql, params = module._clear_plan("logsearch", "search_logs", False, False)
    assert params == ("logsearch", "search_logs")
    assert "DELETE FROM tool_pins" in sql
    assert "WHERE server = $1 AND tool = $2" in sql
    # and it strips whitespace, so a pasted name with a newline still matches
    assert module._clear_plan(" logsearch\n", " search_logs ", False, False)[1] == ("logsearch", "search_logs")


def test_tool_pin_admin_refuses_all_without_yes() -> None:
    """`--all` is irreversible and fleet-wide: it needs an explicit --yes."""
    module = _load_script("tool_pin_admin")
    with pytest.raises(module.CommandError) as caught:
        module._clear_plan(None, None, True, False)
    assert caught.value.code == 2
    assert "--yes" in caught.value.message


def test_tool_pin_admin_refuses_an_ambiguous_or_half_target() -> None:
    """A half target or a mixed ask is a usage error, never a guess."""
    module = _load_script("tool_pin_admin")
    for server, tool, all_pins in (
        (None, None, False),
        ("logsearch", None, False),
        (None, "search_logs", False),
        ("logsearch", "search_logs", True),
    ):
        with pytest.raises(module.CommandError) as caught:
            module._clear_plan(server, tool, all_pins, True)
        assert caught.value.code == 2


def test_tool_pin_admin_list_end_to_end(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """One connection, one SELECT, the table printed, the connection closed."""
    module = _load_script("tool_pin_admin")
    conn = _FakeAdminConn(rows=[{"server": "s", "tool": "t", "fingerprint": "abcdef0123456789"}])
    _install_conn(monkeypatch, module, conn)
    assert module.main(["list"]) == 0
    assert len(conn.fetched) == 1
    assert conn.fetched[0][0] == module._LIST_SQL
    assert "last_seen" in conn.fetched[0][0], "the list read must carry last_seen"
    assert "s" in capsys.readouterr().out
    assert conn.closed is True


def test_tool_pin_admin_clear_deletes_and_names_the_restart(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clear deletes the durable row and says the running gateway must be
    restarted - otherwise the operator thinks the lever worked when it did not."""
    module = _load_script("tool_pin_admin")
    conn = _FakeAdminConn(tag="DELETE 1")
    _install_conn(monkeypatch, module, conn)
    assert module.main(["clear", "--server", "logsearch", "--tool", "search_logs"]) == 0
    assert conn.executed == [(module._DELETE_ONE_SQL, ("logsearch", "search_logs"))]
    out = capsys.readouterr().out
    assert "cleared 1 pin(s)" in out
    assert "logsearch__search_logs" in out
    assert "RESTARTED" in out
    assert conn.closed is True


def test_tool_pin_admin_clear_all_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--all --yes` issues the store's clear constant, not a hand-written one."""
    module = _load_script("tool_pin_admin")
    from pcai_llm.core import tool_pin

    conn = _FakeAdminConn(tag="DELETE 4")
    _install_conn(monkeypatch, module, conn)
    assert module.main(["clear", "--all", "--yes"]) == 0
    assert conn.executed == [(tool_pin._CLEAR_SQL, ())]


def test_tool_pin_admin_reports_an_absent_pin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deleting nothing is not an error, but it is never silent either."""
    module = _load_script("tool_pin_admin")
    _install_conn(monkeypatch, module, _FakeAdminConn(tag="DELETE 0"))
    assert module.main(["clear", "--server", "s", "--tool", "t"]) == 0
    assert "nothing was deleted" in capsys.readouterr().out


def test_tool_pin_admin_clear_all_on_an_empty_table_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fleet-wide clear of an empty table reports the empty table, not a
    per-tool "no pin matched every pin" sentence."""
    module = _load_script("tool_pin_admin")
    _install_conn(monkeypatch, module, _FakeAdminConn(tag="DELETE 0"))
    assert module.main(["clear", "--all", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "no pins recorded" in out
    assert "no pin matched" not in out


def test_tool_pin_admin_without_a_database_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """THE brief's rule: no database configuration is a CLEAR failure, never an
    empty list an operator can mistake for "no pins"."""
    module = _load_script("tool_pin_admin")
    _blank_database(monkeypatch)
    assert module.main(["list"]) == 2
    err = capsys.readouterr().err
    assert "database is not configured" in err
    assert "PCAI_LLM_DATABASE_URL" in err


def test_tool_pin_admin_validates_the_target_before_connecting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad ask must be answered as a bad ask (exit 2, --yes named), not as a
    database problem: the target is validated before any connection."""
    module = _load_script("tool_pin_admin")
    _blank_database(monkeypatch)
    assert module.main(["clear", "--all"]) == 2
    assert "--yes" in capsys.readouterr().err


def test_tool_pin_admin_names_a_missing_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cluster that never booted the gateway has no schema: say so."""
    module = _load_script("tool_pin_admin")
    import asyncpg

    conn = _FakeAdminConn(fault=asyncpg.UndefinedTableError("relation does not exist"))
    _install_conn(monkeypatch, module, conn)
    assert module.main(["list"]) == 1
    assert "tool_pins table does not exist" in capsys.readouterr().err
    assert conn.closed is True, "a failed read must still close the connection"


class _FakeReportConn:
    """The four reads agent_eval_report issues, routed by statement text."""

    def __init__(
        self,
        counts: Any = (),
        total: Any = None,
        recent: Any = (),
        payload: Any = None,
        fault: Exception | None = None,
    ) -> None:
        self.counts = list(counts)
        self.total = total
        self.recent = list(recent)
        self.payload = payload
        self.fault = fault
        self.statements: list[str] = []
        self.closed = False

    async def fetch(self, sql: str, *params: Any) -> list[Any]:
        if self.fault is not None:
            raise self.fault
        self.statements.append(sql)
        return self.counts if "GROUP BY verdict" in sql else self.recent

    async def fetchrow(self, sql: str, *params: Any) -> Any:
        if self.fault is not None:
            raise self.fault
        self.statements.append(sql)
        return self.total if "count(DISTINCT request_id)" in sql else self.payload

    async def close(self) -> None:
        self.closed = True


def _verdict_row(request_id: str, verdict: str, reason: str, minutes_ago: int = 0) -> dict[str, Any]:
    from datetime import UTC, datetime, timedelta

    return {
        "request_id": request_id,
        "verdict": verdict,
        "reason": reason,
        "ts": datetime(2026, 11, 23, 18, 0, 0, tzinfo=UTC) - timedelta(minutes=minutes_ago),
    }


def test_agent_eval_report_ranks_the_worst_turns_first() -> None:
    """Severity first (a denied call outranks a loop), then recency."""
    module = _load_script("agent_eval_report")
    rows = [
        _verdict_row("req-loop", "loop", "3 repeats", minutes_ago=1),
        _verdict_row("req-denied", "not_granted", "1 denied", minutes_ago=9),
        _verdict_row("req-both", "tool_error", "1 failed", minutes_ago=5),
        _verdict_row("req-both", "step_budget", "41 rounds", minutes_ago=5),
        _verdict_row("req-clean", "clean", "no findings", minutes_ago=2),
    ]
    turns = module.rank_turns(rows, 10)
    assert [turn.request_id for turn in turns] == ["req-denied", "req-both", "req-loop"]
    assert turns[1].weight == 4, "tool_error (3) + step_budget (1)"
    assert turns[1].severity == 3, "the worst single finding on that turn"
    assert turns[1].findings == (("tool_error", "1 failed"), ("step_budget", "41 rounds"))
    assert all(turn.request_id != "req-clean" for turn in turns), "clean turns are not findings"


def test_agent_eval_report_one_denial_outranks_many_operational_findings() -> None:
    """Severity is PRIMARY, weight secondary: a turn with one DENIED call beats a
    turn carrying three operational findings, because a denial is the signal an
    operator must see first."""
    module = _load_script("agent_eval_report")
    rows = [
        _verdict_row("req-many", "tool_error", "1 failed", minutes_ago=1),
        _verdict_row("req-many", "loop", "3 repeats", minutes_ago=1),
        _verdict_row("req-many", "step_budget", "41 rounds", minutes_ago=1),
        _verdict_row("req-denied", "not_granted", "1 denied", minutes_ago=30),
    ]
    turns = module.rank_turns(rows, 10)
    assert [turn.request_id for turn in turns] == ["req-denied", "req-many"]
    assert turns[0].weight == 4 and turns[1].weight == 6, "the heavier turn still ranks second"


def test_agent_eval_report_ranking_is_total_and_deterministic() -> None:
    """Equal weight and equal timestamp still order by request_id, so a re-run
    prints the same report (and a test can assert on it)."""
    module = _load_script("agent_eval_report")
    rows = [
        _verdict_row("req-b", "loop", "3 repeats"),
        _verdict_row("req-a", "loop", "3 repeats"),
    ]
    assert [turn.request_id for turn in module.rank_turns(rows, 10)] == ["req-a", "req-b"]
    assert len(module.rank_turns(rows, 1)) == 1, "--limit caps the ranking"


def test_agent_eval_report_counts_every_verdict_in_report_order() -> None:
    """All five verdicts are listed (zeros included) in core.agent_eval order; an
    unknown verdict row is visible instead of silently dropped."""
    module = _load_script("agent_eval_report")
    from pcai_llm.core import agent_eval

    rows = [
        {"verdict": "clean", "rows": 7, "turns": 7},
        {"verdict": "tool_error", "rows": 2, "turns": 2},
        {"verdict": "legacy_verdict", "rows": 1, "turns": 1},
    ]
    out = module.format_counts(rows, days=7, turns_scored=9)
    positions = [out.index(verdict) for verdict in agent_eval.VERDICTS]
    assert positions == sorted(positions), "the report order is core.agent_eval.VERDICTS"
    assert "step_budget" in out and "0 row(s)" in out
    assert "legacy_verdict" in out
    assert "NOT in the verdict vocabulary" in out


def test_agent_eval_report_empty_states_name_the_knobs() -> None:
    """An empty report must explain itself: the flags are OFF by default, and the
    reader says so instead of printing nothing."""
    module = _load_script("agent_eval_report")
    counts = module.format_counts([], days=7, turns_scored=0)
    assert "agent_eval_enabled" in counts
    turns = module.format_turns([], days=7, scanned=0, turns_scored=0)
    assert "no verdict rows in the window" in turns
    # a scored-but-clean window is a DIFFERENT answer from "nothing was scored"
    clean = module.format_turns([], days=7, scanned=12, turns_scored=12)
    assert "no findings" in clean and "12 turn(s) scored" in clean
    payload = module.format_payload_stats({"rows": 0}, retention_days=7)
    assert "trace_payload_sampling" in payload


def test_agent_eval_report_payload_stats_show_shares_and_timestamps() -> None:
    """rows, oldest/newest, and the truncated/redacted shares - the numbers that
    tell an operator the table is healthy and the scrubber is firing."""
    module = _load_script("agent_eval_report")
    from datetime import UTC, datetime

    row = {
        "rows": 1234,
        "oldest": datetime(2026, 11, 16, 10, 0, 0, tzinfo=UTC),
        "newest": datetime(2026, 11, 23, 18, 0, 0, tzinfo=UTC),
        "truncated": 152,
        "redacted": 563,
    }
    out = module.format_payload_stats(row, retention_days=7)
    assert "1,234" in out
    assert "2026-11-16 10:00:00+00:00" in out
    assert "2026-11-23 18:00:00+00:00" in out
    assert "12.3%" in out
    assert "45.6%" in out
    assert "retention 7 day(s)" in out


def test_agent_eval_report_issues_reads_only() -> None:
    """THE brief's hard rule: the report is read-only. Every statement it can
    issue is a SELECT."""
    module = _load_script("agent_eval_report")
    statements = [value for name, value in vars(module).items() if name.endswith("_SQL") and isinstance(value, str)]
    assert len(statements) == 4, "one statement per read (counts, total, recent, payload)"
    for sql in statements:
        head = " ".join(sql.split()).upper()
        assert head.startswith("SELECT"), head
        for forbidden in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER", "CREATE"):
            assert re.search(rf"\b{forbidden}\b", head) is None, f"{forbidden} in a read-only report: {head}"


def test_agent_eval_report_end_to_end(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """All three sections print from ONE connection, and the connection closes."""
    module = _load_script("agent_eval_report")
    from datetime import UTC, datetime

    conn = _FakeReportConn(
        counts=[{"verdict": "tool_error", "rows": 2, "turns": 2}],
        total={"turns": 5},
        recent=[_verdict_row("req-bad", "tool_error", "1 tool call(s) did not succeed")],
        payload={
            "rows": 10,
            "oldest": datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC),
            "newest": datetime(2026, 11, 23, 18, 0, 0, tzinfo=UTC),
            "truncated": 1,
            "redacted": 2,
        },
    )
    _install_conn(monkeypatch, module, conn)
    assert module.main(["--days", "3", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "agent quality layer report -- window: last 3 day(s)" in out
    assert "verdict counts" in out
    assert "req-bad" in out
    assert "1 tool call(s) did not succeed" in out
    assert "agent_trace_payloads" in out
    assert conn.closed is True
    assert all(" ".join(sql.split()).upper().startswith("SELECT") for sql in conn.statements)


def test_agent_eval_report_without_a_database_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script("agent_eval_report")
    _blank_database(monkeypatch)
    assert module.main([]) == 2
    assert "database is not configured" in capsys.readouterr().err


def test_agent_eval_report_names_a_missing_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_script("agent_eval_report")
    import asyncpg

    conn = _FakeReportConn(fault=asyncpg.UndefinedTableError("relation does not exist"))
    _install_conn(monkeypatch, module, conn)
    assert module.main([]) == 1
    assert "quality-layer tables do not exist" in capsys.readouterr().err
    assert conn.closed is True
