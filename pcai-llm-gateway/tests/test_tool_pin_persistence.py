"""Tool-definition pins survive a restart (recommendation B2).

WHAT THIS PROVES: a pin is a CONTROL, not a tripwire with a one-restart
memory. A second PinStore -- a new process, in production -- reads the digest
the first one approved and reports MATCH/CHANGED against it instead of
re-blessing whatever the MCP server is serving now.

HOW: the store talks to persistence through a tiny SYNC backend protocol
(tool_pin.PinBackend), so these tests inject an in-memory fake TABLE (not a
call recorder: a restart test is only meaningful if the second store reads
what the first one wrote). No Postgres, no network -- the unit suite never
touches a live database, which is also why default_backend() is gated on
db.pool being present.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import pytest

from pcai_llm.core import tool_pin
from pcai_llm.db.schema import SCHEMA_SQL

# 16 lowercase hex chars: what fingerprint() always produces.
APPROVED = "0123456789abcdef"
REWRITTEN = "fedcba9876543210"


class FakePinTable:
    """The tool_pins table, in memory: the same operations the DB backend issues."""

    def __init__(self, *, fail_load: bool = False, fail_writes: bool = False) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail_load = fail_load
        self.fail_writes = fail_writes
        self.loads = 0
        self.saves: list[tuple[str, str, str]] = []
        self.touches: list[tuple[str, str, str]] = []
        self.clears = 0

    def seed(self, server: str, tool: str, fingerprint: str, *, age_s: float = 0.0) -> None:
        """Insert a row as if an EARLIER process had approved it."""
        first_seen = time.time() - age_s
        self.rows[(server, tool)] = {"fingerprint": fingerprint, "first_seen": first_seen, "last_seen": first_seen}

    # --- PinBackend ---------------------------------------------------

    def load(self) -> list[tool_pin.PersistedPin]:
        self.loads += 1
        if self.fail_load:
            raise RuntimeError("db down")
        return [
            tool_pin.PersistedPin(
                server=server, tool=tool, fingerprint=row["fingerprint"], first_seen=row["first_seen"]
            )
            for (server, tool), row in self.rows.items()
        ]

    def save(self, server: str, tool: str, fingerprint: str) -> None:
        if self.fail_writes:
            raise RuntimeError("db down")
        self.saves.append((server, tool, fingerprint))
        now = time.time()
        self.rows[(server, tool)] = {"fingerprint": fingerprint, "first_seen": now, "last_seen": now}

    def touch(self, server: str, tool: str, fingerprint: str) -> None:
        if self.fail_writes:
            raise RuntimeError("db down")
        self.touches.append((server, tool, fingerprint))
        row = self.rows.get((server, tool))
        if row is not None and row["fingerprint"] == fingerprint:
            row["last_seen"] = time.time()

    def clear(self) -> None:
        self.clears += 1
        self.rows.clear()


class FakeDb:
    """The two handles tool_pin uses on the db module: the pool gate and fetch()."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, pool: Any = "set", fail: bool = False) -> None:
        self.pool = object() if pool == "set" else None
        self.rows = rows or []
        self.fail = fail
        self.fetches = 0

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetches += 1
        if self.fail:
            raise RuntimeError("db down")
        return list(self.rows)


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Module-level caches are process state: reset them for every test."""
    monkeypatch.setattr(tool_pin, "_FAULT_LOGGED", set())
    monkeypatch.setattr(tool_pin, "_DB_BACKEND", None)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# 1. the restart (the whole point)
# ---------------------------------------------------------------------------


def test_a_restart_matches_the_persisted_approval() -> None:
    """A second store must treat a persisted row as KNOWN: MATCH, not FIRST_SEEN.

    That is the difference between a control and a tripwire: before persistence
    a restart re-approved whatever the server was serving at that moment.
    """
    table = FakePinTable()
    first = tool_pin.PinStore(backend=table)
    assert first.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.FIRST_SEEN
    assert table.saves == [("logsearch", "search_logs", APPROVED)]

    second = tool_pin.PinStore(backend=table)  # a NEW PROCESS, in production
    # The pin is on record before anything is observed this turn, which is what
    # lets the offer pass refuse a tool that "vanished" across a restart.
    assert second.pinned_keys() == [("logsearch", "search_logs")]
    assert second.pinned("logsearch", "search_logs") == APPROVED
    verdict = second.observe("logsearch", "search_logs", APPROVED)
    assert verdict.verdict == tool_pin.MATCH
    assert verdict.previous == APPROVED


def test_a_restart_reports_changed_against_the_persisted_digest() -> None:
    table = FakePinTable()
    tool_pin.PinStore(backend=table).observe("logsearch", "search_logs", APPROVED)
    verdict = tool_pin.PinStore(backend=table).observe("logsearch", "search_logs", REWRITTEN)
    assert verdict.verdict == tool_pin.CHANGED
    assert verdict.previous == APPROVED
    assert verdict.current == REWRITTEN
    assert verdict.changed is True


def test_changed_never_rewrites_the_persisted_approval() -> None:
    """A CHANGED verdict keeps the APPROVED digest on record -- in the table too.

    Re-pinning would let a rug-pull approve itself (change once, then MATCH
    forever), and persisting the rewrite would make it survive the restart that
    is supposed to be the operator re-approval point.
    """
    table = FakePinTable()
    tool_pin.PinStore(backend=table).observe("logsearch", "search_logs", APPROVED)
    table.saves.clear()
    second = tool_pin.PinStore(backend=table)
    assert second.observe("logsearch", "search_logs", REWRITTEN).verdict == tool_pin.CHANGED
    assert table.saves == [] and table.touches == []
    assert table.rows[("logsearch", "search_logs")]["fingerprint"] == APPROVED
    assert second.observe("logsearch", "search_logs", REWRITTEN).verdict == tool_pin.CHANGED


def test_reset_clears_the_persisted_table_too() -> None:
    """An operator re-approval must not be undone by the next restart."""
    table = FakePinTable()
    first = tool_pin.PinStore(backend=table)
    first.observe("logsearch", "search_logs", APPROVED)
    assert table.rows
    first.reset()
    assert table.clears == 1
    assert table.rows == {}
    assert len(first) == 0
    # What the NEXT process sees is the reset, not the retired digest.
    second = tool_pin.PinStore(backend=table)
    assert second.observe("logsearch", "search_logs", REWRITTEN).verdict == tool_pin.FIRST_SEEN


# ---------------------------------------------------------------------------
# 2. TTL semantics across a restart (wall clock -> monotonic age)
# ---------------------------------------------------------------------------


def test_ttl_zero_keeps_a_persisted_pin_trusted() -> None:
    table = FakePinTable()
    table.seed("logsearch", "search_logs", APPROVED, age_s=3600.0)
    store = tool_pin.PinStore(backend=table)  # ttl_s=0: an approval is an approval
    assert store.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.MATCH


def test_ttl_keeps_counting_across_a_restart() -> None:
    """The monotonic clock resets with the process; the AGE of the approval does not.

    A pin recorded an hour ago must already be expired for a store with a
    60-second TTL, not granted a fresh window by the restart.
    """
    table = FakePinTable()
    table.seed("logsearch", "search_logs", APPROVED, age_s=3600.0)
    store = tool_pin.PinStore(ttl_s=60.0, backend=table)
    assert store.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.FIRST_SEEN


def test_a_row_with_an_unreadable_first_seen_is_treated_as_fresh() -> None:
    """A bad timestamp must not silently disable the defence it carries."""
    table = FakePinTable()
    table.rows[("logsearch", "search_logs")] = {"fingerprint": APPROVED, "first_seen": 0.0, "last_seen": 0.0}
    store = tool_pin.PinStore(ttl_s=60.0, backend=table)
    assert store.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.MATCH


# ---------------------------------------------------------------------------
# 3. write-through: bounded, and last_seen on MATCH
# ---------------------------------------------------------------------------


def test_match_refreshes_last_seen_after_a_restart() -> None:
    """Requirement: a MATCH still updates last_seen, and the verdict is unchanged."""
    table = FakePinTable()
    table.seed("logsearch", "search_logs", APPROVED, age_s=30.0)
    before = table.rows[("logsearch", "search_logs")]["last_seen"]
    store = tool_pin.PinStore(backend=table)
    verdict = store.observe("logsearch", "search_logs", APPROVED)
    assert verdict.verdict == tool_pin.MATCH
    assert table.touches == [("logsearch", "search_logs", APPROVED)]
    assert table.rows[("logsearch", "search_logs")]["last_seen"] >= before
    assert table.rows[("logsearch", "search_logs")]["fingerprint"] == APPROVED


def test_last_seen_writes_are_coalesced(monkeypatch: pytest.MonkeyPatch) -> None:
    """MATCH is the hot path: at most one last_seen write per key per window."""
    table = FakePinTable()
    store = tool_pin.PinStore(backend=table)
    assert store.observe("s", "t", APPROVED).verdict == tool_pin.FIRST_SEEN
    assert table.touches == []  # the save already wrote last_seen
    assert store.observe("s", "t", APPROVED).verdict == tool_pin.MATCH
    assert table.touches == []  # inside TOUCH_INTERVAL_S: no round trip
    # The window is a POLICY, not a lost update: with it disabled every MATCH writes.
    monkeypatch.setattr(tool_pin, "TOUCH_INTERVAL_S", 0.0)
    assert store.observe("s", "t", APPROVED).verdict == tool_pin.MATCH
    assert table.touches == [("s", "t", APPROVED)]


def test_the_persisted_table_is_read_once_per_store() -> None:
    """The load is a per-PROCESS cost, not a per-tool (or per-turn) one."""
    table = FakePinTable()
    store = tool_pin.PinStore(backend=table)
    for _ in range(5):
        store.observe("logsearch", "search_logs", APPROVED)
        store.pinned("logsearch", "search_logs")
        store.pinned_keys()
        len(store)
    assert table.loads == 1


def test_concurrent_first_use_loads_once() -> None:
    """The offer pass is per request; two requests racing a cold store must not
    both load (and must not both record a FIRST_SEEN)."""
    table = FakePinTable()
    table.seed("logsearch", "search_logs", APPROVED)
    store = tool_pin.PinStore(backend=table)
    verdicts: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        verdict = store.observe("logsearch", "search_logs", APPROVED)
        with lock:
            verdicts.append(verdict.verdict)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert verdicts == [tool_pin.MATCH] * 8
    assert table.loads == 1


# ---------------------------------------------------------------------------
# 4. best-effort: a DB fault degrades to in-memory and never breaks a verdict
# ---------------------------------------------------------------------------


def test_a_load_fault_degrades_to_memory_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    table = FakePinTable(fail_load=True)
    store = tool_pin.PinStore(backend=table)
    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.tool_pin"):
        assert store.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.FIRST_SEEN
        assert store.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.MATCH
        assert store.observe("applygate", "plan_apply", REWRITTEN).verdict == tool_pin.FIRST_SEEN
    messages = _warnings(caplog)
    assert len(messages) == 1, f"expected exactly one fault warning, got {messages}"
    assert "pin persistence load failed" in messages[0]
    assert "IN MEMORY" in messages[0]
    # A store that could not READ the approvals must not WRITE them: a
    # FIRST_SEEN save would overwrite the very digest the load protects.
    assert table.saves == [] and table.touches == []


def test_a_write_fault_does_not_break_the_verdict(caplog: pytest.LogCaptureFixture) -> None:
    table = FakePinTable(fail_writes=True)
    store = tool_pin.PinStore(backend=table)
    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.tool_pin"):
        assert store.observe("s", "t", APPROVED).verdict == tool_pin.FIRST_SEEN
        assert store.observe("s", "t", APPROVED).verdict == tool_pin.MATCH
    messages = _warnings(caplog)
    assert len(messages) == 1, f"expected exactly one fault warning, got {messages}"
    assert "pin persistence save failed" in messages[0]


def test_unreadable_persisted_rows_are_skipped_not_fatal() -> None:
    rows: list[dict[str, Any]] = [
        {"server": "logsearch", "tool": "search_logs", "fingerprint": APPROVED, "first_seen": time.time()},
        {"server": "applygate"},  # no tool/fingerprint
        {"nonsense": True},
    ]
    pins = tool_pin._rows_to_pins(rows)
    assert [(pin.server, pin.tool, pin.fingerprint) for pin in pins] == [("logsearch", "search_logs", APPROVED)]


# ---------------------------------------------------------------------------
# 5. the production wiring: PinStore() with no backend argument
# ---------------------------------------------------------------------------


def test_default_backend_is_none_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pool = no persistence: the store behaves exactly as it did before B2.

    This is also what keeps the unit suite hermetic -- no connection attempt, no
    pins leaking between tests through a shared table.
    """
    monkeypatch.setattr(tool_pin, "db", FakeDb(pool=None))
    assert tool_pin.default_backend() is None
    store = tool_pin.PinStore()
    assert store.observe("s", "t", APPROVED).verdict == tool_pin.FIRST_SEEN
    assert store.observe("s", "t", APPROVED).verdict == tool_pin.MATCH


def test_the_unit_suite_has_no_pool_so_pins_stay_in_memory() -> None:
    """The gate that keeps the rest of the suite off a live table."""
    from pcai_llm.db import db as real_db

    assert real_db.pool is None
    assert tool_pin.default_backend() is None


def test_a_store_without_a_backend_argument_uses_the_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production wiring: toolrelay._PIN_STORE = tool_pin.PinStore()."""
    table = FakePinTable()
    monkeypatch.setattr(tool_pin, "db", FakeDb())
    monkeypatch.setattr(tool_pin, "_DbPinBackend", lambda: table)
    first = tool_pin.PinStore()
    assert first.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.FIRST_SEEN
    assert table.saves == [("logsearch", "search_logs", APPROVED)]
    second = tool_pin.PinStore()
    assert second.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.MATCH


async def test_awarm_reads_the_pool_before_the_first_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OPTIONAL non-blocking warm-up: same store state, loaded at boot."""
    table = FakePinTable()
    fake_db = FakeDb(
        rows=[
            {
                "server": "logsearch",
                "tool": "search_logs",
                "fingerprint": APPROVED,
                "first_seen": time.time() - 10.0,
            }
        ]
    )
    monkeypatch.setattr(tool_pin, "db", fake_db)
    monkeypatch.setattr(tool_pin, "_DbPinBackend", lambda: table)
    store = tool_pin.PinStore()
    await store.awarm()
    assert fake_db.fetches == 1
    assert store.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.MATCH
    await store.awarm()  # idempotent: an already-loaded store does not re-read
    assert fake_db.fetches == 1
    # Writes still flow after a warm-up (the backend was resolved, not skipped).
    assert store.observe("applygate", "plan_apply", REWRITTEN).verdict == tool_pin.FIRST_SEEN
    assert table.saves == [("applygate", "plan_apply", REWRITTEN)]


async def test_awarm_fault_degrades_to_memory(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake_db = FakeDb(fail=True)
    monkeypatch.setattr(tool_pin, "db", fake_db)
    table = FakePinTable()
    monkeypatch.setattr(tool_pin, "_DbPinBackend", lambda: table)
    store = tool_pin.PinStore()
    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.tool_pin"):
        await store.awarm()
        assert store.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.FIRST_SEEN
    messages = _warnings(caplog)
    assert len(messages) == 1 and "pin persistence load failed" in messages[0]
    assert table.saves == []


async def test_awarm_with_an_injected_backend_delegates_to_it() -> None:
    table = FakePinTable()
    table.seed("logsearch", "search_logs", APPROVED)
    store = tool_pin.PinStore(backend=table)
    await store.awarm()
    assert table.loads == 1
    assert store.observe("logsearch", "search_logs", APPROVED).verdict == tool_pin.MATCH


# ---------------------------------------------------------------------------
# 6. the schema the store reads and writes
# ---------------------------------------------------------------------------


def _ddl() -> str:
    match = re.search(r"CREATE TABLE IF NOT EXISTS tool_pins \(([^;]+)\)", SCHEMA_SQL, re.DOTALL)
    assert match, "tool_pins CREATE TABLE missing from SCHEMA_SQL"
    return match.group(1)


def test_schema_has_the_pin_table() -> None:
    body = _ddl()
    for fragment in (
        "server      TEXT NOT NULL",
        "tool        TEXT NOT NULL",
        "fingerprint TEXT NOT NULL",
        "first_seen  TIMESTAMPTZ NOT NULL DEFAULT now()",
        "last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()",
        "PRIMARY KEY (server, tool)",
    ):
        assert fragment in body, f"missing from tool_pins DDL: {fragment!r}"


def test_schema_pin_constraint_is_not_valid() -> None:
    """NOT VALID is the file-wide discipline: a bad legacy row must stay visible
    instead of aborting the whole implicit schema transaction (the boot)."""
    assert "ALTER TABLE tool_pins DROP CONSTRAINT IF EXISTS ck_tool_pins_shape" in SCHEMA_SQL
    assert "CHECK (server <> '' AND tool <> '' AND fingerprint ~ '^[0-9a-f]{16}$') NOT VALID" in SCHEMA_SQL


def test_schema_pin_ddl_is_idempotent() -> None:
    """SCHEMA_SQL runs on EVERY boot: no statement may fail the second time."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in SCHEMA_SQL.split("\n"))
    statements = [s.strip() for s in no_comments.split(";") if "tool_pins" in s]
    assert len(statements) == 3, f"expected 3 tool_pins statements, got {len(statements)}: {statements}"
    heads = [" ".join(s.split("\n", 1)[0].split()) for s in statements]
    assert heads[0].startswith("CREATE TABLE IF NOT EXISTS")
    assert heads[1].startswith("ALTER TABLE tool_pins DROP CONSTRAINT IF EXISTS")
    # ADD CONSTRAINT is idempotent BECAUSE the DROP above it runs first; the
    # constraint is added NOT VALID so a stale row cannot abort the boot.
    assert heads[2].startswith("ALTER TABLE tool_pins ADD CONSTRAINT")
    assert "NOT VALID" in statements[2]
