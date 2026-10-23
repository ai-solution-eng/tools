"""Stickiness store + retention + schema tests (load-aware cascade).

Covers the pieces core/cascade.py builds on: the fault-isolated
get/set/drop trio over the model_stickiness table, the housekeeping
retention sweep for it (settings.retention_stickiness_days, 0 = keep
forever), and the schema block's additive/idempotent discipline.

No live Postgres: the repo's monkeypatch-the-module-db pattern (see
tests/test_cascade.py) with an in-memory row table that speaks the real
SQL shape (INSERT ... ON CONFLICT upsert, DELETE, SELECT by PK).
"""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

import pytest

from pcai_llm.config import Settings, settings
from pcai_llm.core import stickiness
from pcai_llm.db.schema import SCHEMA_SQL

KEY = uuid.UUID("11111111-2222-3333-4444-555555555555")
THREAD = "0123456789abcdef"


class StickyFakeDB:
    """model_stickiness in memory: the three statements the module issues."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, str], str] = {}
        self.executed: list[tuple[str, tuple]] = []
        self.fail = False

    async def fetchrow(self, sql: str, *a: Any):
        if self.fail:
            raise RuntimeError("db down")
        if "FROM model_stickiness" in sql:
            model = self.rows.get((a[0], a[1]))
            return {"model": model} if model is not None else None
        return None

    async def execute(self, sql: str, *a: Any):
        if self.fail:
            raise RuntimeError("db down")
        self.executed.append((sql, a))
        if "INSERT INTO model_stickiness" in sql:
            self.rows[(a[0], a[1])] = a[2]  # upsert: last write wins
        elif "DELETE FROM model_stickiness" in sql:
            self.rows.pop((a[0], a[1]), None)
        return "OK"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> StickyFakeDB:
    f = StickyFakeDB()
    monkeypatch.setattr(stickiness, "db", f)
    return f


# ---------------------------------------------------------------------------
# 1. get / set / drop round-trip
# ---------------------------------------------------------------------------


def test_set_then_get_roundtrip(fake: StickyFakeDB) -> None:
    asyncio.run(stickiness.set_sticky(KEY, THREAD, "GLM-5.3-Flash"))
    assert fake.rows[(KEY, THREAD)] == "GLM-5.3-Flash"
    assert asyncio.run(stickiness.get_sticky(KEY, THREAD)) == "GLM-5.3-Flash"


def test_set_is_an_upsert_not_a_second_row(fake: StickyFakeDB) -> None:
    """One row per (key, thread) — a re-pick overwrites (ON CONFLICT), it
    never accumulates rows for one conversation."""
    asyncio.run(stickiness.set_sticky(KEY, THREAD, "GLM-5.3-Flash"))
    asyncio.run(stickiness.set_sticky(KEY, THREAD, "Deepseek-V4.1-Flash"))
    assert fake.rows == {(KEY, THREAD): "Deepseek-V4.1-Flash"}
    assert len([1 for sql, _ in fake.executed if "INSERT INTO model_stickiness" in sql]) == 2
    assert all("ON CONFLICT (key_id, thread_key) DO UPDATE" in sql for sql, _ in fake.executed if "INSERT" in sql)


def test_get_miss_is_none(fake: StickyFakeDB) -> None:
    assert asyncio.run(stickiness.get_sticky(KEY, "never-seen")) is None


def test_get_without_thread_key_is_none(fake: StickyFakeDB) -> None:
    """No thread key = no stickiness machinery at all: the read is a no-op
    (no SQL issued) and the write is skipped — the no-seed path."""
    assert asyncio.run(stickiness.get_sticky(KEY, None)) is None
    assert asyncio.run(stickiness.get_sticky(KEY, "")) is None
    asyncio.run(stickiness.set_sticky(KEY, None, "GLM-5.3-Flash"))
    asyncio.run(stickiness.drop_sticky(KEY, None))
    assert fake.executed == []


def test_set_skips_empty_model(fake: StickyFakeDB) -> None:
    asyncio.run(stickiness.set_sticky(KEY, THREAD, ""))
    assert fake.executed == [] and fake.rows == {}


def test_drop_removes_the_row(fake: StickyFakeDB) -> None:
    asyncio.run(stickiness.set_sticky(KEY, THREAD, "GLM-5.3-Flash"))
    asyncio.run(stickiness.drop_sticky(KEY, THREAD))
    assert fake.rows == {}
    assert asyncio.run(stickiness.get_sticky(KEY, THREAD)) is None


def test_rows_are_per_key_and_per_thread(fake: StickyFakeDB) -> None:
    """The PK is (key_id, thread_key): two keys' conversations never share a
    head, and one key's two conversations stay independent."""
    other_key, other_thread = uuid.uuid4(), "fedcba9876543210"
    asyncio.run(stickiness.set_sticky(KEY, THREAD, "GLM-5.3-Flash"))
    asyncio.run(stickiness.set_sticky(KEY, other_thread, "Deepseek-V4.1-Flash"))
    asyncio.run(stickiness.set_sticky(other_key, THREAD, "Qwen3.8-27B"))
    assert asyncio.run(stickiness.get_sticky(KEY, THREAD)) == "GLM-5.3-Flash"
    assert asyncio.run(stickiness.get_sticky(KEY, other_thread)) == "Deepseek-V4.1-Flash"
    assert asyncio.run(stickiness.get_sticky(other_key, THREAD)) == "Qwen3.8-27B"


# ---------------------------------------------------------------------------
# 2. fault isolation (the fail-open discipline: a stickiness outage must
#    NEVER break admission)
# ---------------------------------------------------------------------------


def test_get_fault_returns_none_and_logs(fake: StickyFakeDB, caplog: pytest.LogCaptureFixture) -> None:
    fake.fail = True
    with caplog.at_level("WARNING", logger="pcai_llm.core.stickiness"):
        assert asyncio.run(stickiness.get_sticky(KEY, THREAD)) is None
    assert any("read failed" in r.message for r in caplog.records)


def test_set_fault_is_swallowed(fake: StickyFakeDB, caplog: pytest.LogCaptureFixture) -> None:
    """A failed write is best-effort: it logs and returns — the request it
    belongs to is already resolved; the next turn simply re-picks."""
    fake.fail = True
    with caplog.at_level("WARNING", logger="pcai_llm.core.stickiness"):
        asyncio.run(stickiness.set_sticky(KEY, THREAD, "GLM-5.3-Flash"))  # must NOT raise
    assert any("write failed" in r.message for r in caplog.records)


def test_drop_fault_is_swallowed(fake: StickyFakeDB, caplog: pytest.LogCaptureFixture) -> None:
    fake.fail = True
    with caplog.at_level("WARNING", logger="pcai_llm.core.stickiness"):
        asyncio.run(stickiness.drop_sticky(KEY, THREAD))  # must NOT raise
    assert any("drop failed" in r.message for r in caplog.records)


def test_missing_table_degrades_like_any_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment whose DB predates the table (or a pool outage) resolves
    as "no sticky head" — never as an exception out of admission."""

    class MissingTable:
        async def fetchrow(self, sql: str, *a: Any):
            raise RuntimeError('relation "model_stickiness" does not exist')

        async def execute(self, sql: str, *a: Any):
            raise RuntimeError('relation "model_stickiness" does not exist')

    monkeypatch.setattr(stickiness, "db", MissingTable())
    assert asyncio.run(stickiness.get_sticky(KEY, THREAD)) is None
    asyncio.run(stickiness.set_sticky(KEY, THREAD, "m"))  # must NOT raise
    asyncio.run(stickiness.drop_sticky(KEY, THREAD))  # must NOT raise


# ---------------------------------------------------------------------------
# 3. observability: INFO line per decision + the counter
# ---------------------------------------------------------------------------


def test_observe_head_pick_logs_and_counts(caplog: pytest.LogCaptureFixture) -> None:
    from pcai_llm.core import metrics

    def count(picked: str, reason: str) -> float:
        for fam in metrics.CASCADE_HEAD_PICKS.collect():
            for s in fam.samples:
                if s.labels == {"picked": picked, "reason": reason}:
                    return float(s.value)
        return 0.0

    before = count("GLM-5.3-Flash", "load")
    with caplog.at_level("INFO", logger="pcai_llm.core.stickiness"):
        stickiness.observe_head_pick("GLM-5.3-Flash", "load", THREAD, "GLM-5.3-Flash=0,Deepseek=1")
    assert any(
        "head pick model=GLM-5.3-Flash reason=load" in r.message
        and "thread=01234567" in r.message  # 8-char prefix, not the whole key
        and "GLM-5.3-Flash=0,Deepseek=1" in r.message
        for r in caplog.records
    )
    assert count("GLM-5.3-Flash", "load") == before + 1


def test_observe_head_pick_reason_vocabulary() -> None:
    """The reasons the cascade module emits are exactly the counter's own
    vocabulary constant (one place defines the contract)."""
    from pcai_llm.core import metrics

    assert set(metrics.CASCADE_PICK_REASONS) == {"sticky", "load", "default", "unhealthy"}


# ---------------------------------------------------------------------------
# 4. retention: housekeeping prunes model_stickiness
# ---------------------------------------------------------------------------


class PruneFakeDB:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.args: list[tuple] = []

    async def execute(self, sql: str, *a: Any):
        self.executed.append(sql)
        self.args.append(a)
        return "DELETE 3"


def test_housekeeping_prunes_stickiness_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """model_stickiness rides the SAME retention table list as agent_traces
    and conversations — cutoff on updated_at, make_interval(days => $1)."""
    from pcai_llm.core import housekeeping

    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_stickiness_days", 14)
    asyncio.run(housekeeping._sweep_once())

    stmt = next(s for s in fake.executed if "DELETE FROM model_stickiness" in s)
    assert "updated_at < now() - make_interval(days => $1)" in stmt
    idx = fake.executed.index(stmt)
    assert fake.args[idx] == (14,)


def test_housekeeping_zero_days_keeps_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 = keep forever: the prune is skipped entirely (the same contract as
    the other retention knobs)."""
    from pcai_llm.core import housekeeping

    fake = PruneFakeDB()
    monkeypatch.setattr(housekeeping, "db", fake)
    monkeypatch.setattr(settings, "retention_stickiness_days", 0)
    asyncio.run(housekeeping._sweep_once())
    assert not any("DELETE FROM model_stickiness" in s for s in fake.executed)


def test_housekeeping_stickiness_prune_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.core import housekeeping

    class Boom(PruneFakeDB):
        async def execute(self, sql: str, *a: Any):
            if "model_stickiness" in sql:
                raise RuntimeError("db hiccup")
            return await super().execute(sql, *a)

    monkeypatch.setattr(housekeeping, "db", Boom())
    monkeypatch.setattr(settings, "retention_stickiness_days", 14)
    asyncio.run(housekeeping._sweep_once())  # must NOT raise


def test_retention_default_is_14_days() -> None:
    """The knob ships ON at 14 days (sticky rows are pure routing state) —
    the brief's default. 0 is the operator's explicit keep-forever."""
    assert Settings().retention_stickiness_days == 14


def test_load_aware_cascade_defaults_off() -> None:
    """The feature gate ships OFF (the backwards-compat gate): every
    deployment keeps the pre-feature cascade until an operator opts in."""
    assert Settings().load_aware_cascade is False


def test_retention_stickiness_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PCAI_LLM_RETENTION_STICKINESS_DAYS", "30")
    from pcai_llm.config import Settings as S

    assert S().retention_stickiness_days == 30


def test_load_aware_cascade_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PCAI_LLM_LOAD_AWARE_CASCADE", "1")
    from pcai_llm.config import Settings as S

    assert S().load_aware_cascade is True


# ---------------------------------------------------------------------------
# 5. schema: additive + idempotent
# ---------------------------------------------------------------------------


def test_schema_model_stickiness_ddl() -> None:
    """The brief's DDL, verbatim: PK (key_id, thread_key), model NOT NULL,
    updated_at defaulted to now()."""
    m = re.search(r"CREATE TABLE IF NOT EXISTS model_stickiness \(([^;]+)\)", SCHEMA_SQL, re.DOTALL)
    assert m, "model_stickiness CREATE TABLE missing from SCHEMA_SQL"
    body = m.group(1)
    for fragment in (
        "key_id     UUID NOT NULL",
        "thread_key TEXT NOT NULL",
        "model      TEXT NOT NULL",
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "PRIMARY KEY (key_id, thread_key)",
    ):
        assert fragment in body, f"missing from model_stickiness DDL: {fragment!r}"


def test_schema_model_stickiness_is_idempotent() -> None:
    """Re-running SCHEMA_SQL on every boot is legal: every statement that
    touches model_stickiness is IF NOT EXISTS-guarded (the same discipline
    every migration in this file follows)."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in SCHEMA_SQL.split("\n"))
    stmts = [s.strip() for s in no_comments.split(";") if s.strip()]
    sticky_stmts = [s for s in stmts if "model_stickiness" in s]
    # 2 statements: CREATE TABLE + the updated_at index.
    assert len(sticky_stmts) == 2, f"expected 2 model_stickiness DDL statements, got {len(sticky_stmts)}"
    for s in sticky_stmts:
        head = " ".join(s.split("\n", 1)[0].split())
        assert head.startswith(("CREATE TABLE IF NOT EXISTS", "CREATE INDEX IF NOT EXISTS")), (
            f"model_stickiness DDL is not IF NOT EXISTS-guarded: {head!r}"
        )


def test_schema_model_stickiness_is_additive_only() -> None:
    """ADDITIVE: the block creates its own table/index and ALTERs nothing —
    no existing table's shape changes (the brief's additive-only rule)."""
    block = SCHEMA_SQL[
        SCHEMA_SQL.index("CREATE TABLE IF NOT EXISTS model_stickiness") : SCHEMA_SQL.index(
            "CREATE INDEX IF NOT EXISTS idx_model_stickiness_updated"
        )
        + 120
    ]
    for line in block.split("\n"):
        stripped = line.strip()
        assert not stripped.upper().startswith("ALTER TABLE"), f"unexpected ALTER in the sticky block: {stripped!r}"
        assert not stripped.upper().startswith("DROP "), f"unexpected DROP in the sticky block: {stripped!r}"


def test_schema_model_stickiness_index_on_updated_at() -> None:
    """The retention sweep deletes on updated_at — the index keeps that
    prune off a seq scan as the table grows."""
    assert "CREATE INDEX IF NOT EXISTS idx_model_stickiness_updated ON model_stickiness(updated_at)" in SCHEMA_SQL


def test_schema_model_stickiness_has_no_fk() -> None:
    """No FK to api_keys: the usage_ledger / canonical_conversations
    convention — key_id alone, so a dangling row after a key delete is
    inert (and ON CONFLICT upserts keep writes idempotent)."""
    m = re.search(r"CREATE TABLE IF NOT EXISTS model_stickiness \(([^;]+)\)", SCHEMA_SQL, re.DOTALL)
    assert m and "REFERENCES" not in m.group(1)
