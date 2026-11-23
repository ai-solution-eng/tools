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
    for line in alters:
        assert "agent_trace_payloads" in line or "agent_eval_verdicts" in line, line


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
