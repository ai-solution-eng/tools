"""Unit tests for core.tooljobs - the detached-tool-call registry.

Two layers, tested separately because they fail differently:

* the PURE layer (states, transitions, TTL clamp, effective state) needs no DB
  and pins the semantics the design doc promises;
* the STORE layer runs against a recording fake so the SQL itself is asserted -
  the atomicity claims (single statement, state predicate, owner filter,
  claim-once) ARE the SQL, so a behavioural fake alone could not catch their
  removal.

The unit suite never touches Postgres (tests/conftest.py), so this mirrors the
db-patching idiom already used by tests/test_admin.py.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pcai_llm.core import tooljobs
from pcai_llm.core.tooljobs import (
    ACTIVE,
    MAX_ACTIVE_PER_KEY,
    MAX_RESULT_BYTES,
    MAX_TTL_S,
    MIN_TTL_S,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_INPUT_REQUIRED,
    STATE_WORKING,
    TERMINAL,
    Job,
    JobStore,
    can_transition,
    clamp_ttl,
    effective_state,
    is_terminal,
    result_available,
)

KEY = uuid.uuid4()
JOB = uuid.uuid4()


def make_job(**over: Any) -> Job:
    """A well-formed Job; override only what a test is about."""
    base: dict[str, Any] = {
        "job_id": JOB,
        "key_id": KEY,
        "server": "sqlhandler",
        "tool": "run_sql",
        "args_sha256": "0" * 64,
        "state": STATE_WORKING,
        "status_message": None,
        "cancel_requested": False,
        "result_claimed_at": None,
        "created_at": None,
        "updated_at": None,
        "expires_at": None,
    }
    base.update(over)
    return Job(**base)


# --- pure layer -------------------------------------------------------------


def test_terminal_vocabulary_is_exactly_the_three() -> None:
    assert set(TERMINAL) == {STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED}
    for state in TERMINAL:
        assert is_terminal(state)
    for state in ACTIVE:
        assert not is_terminal(state)


def test_no_transition_leaves_a_terminal_state() -> None:
    """The invariant that makes a finished job's outcome trustworthy."""
    for done in TERMINAL:
        for target in (*TERMINAL, *ACTIVE):
            assert not can_transition(done, target), f"{done} -> {target} must be refused"


def test_active_transitions_are_allowed() -> None:
    assert can_transition(STATE_WORKING, STATE_COMPLETED)
    assert can_transition(STATE_WORKING, STATE_FAILED)
    assert can_transition(STATE_WORKING, STATE_CANCELLED)
    assert can_transition(STATE_WORKING, STATE_INPUT_REQUIRED)
    assert can_transition(STATE_INPUT_REQUIRED, STATE_WORKING)


def test_unknown_state_is_not_terminal_and_cannot_be_entered() -> None:
    assert not is_terminal("nonsense")
    assert not can_transition(STATE_WORKING, "nonsense")


def test_effective_state_reports_cancel_before_the_worker_notices() -> None:
    """A recorded cancel must answer 'cancelled' immediately: the poller's real
    question is 'will I get a result?', and the answer is no."""
    assert effective_state(make_job(cancel_requested=True)) == STATE_CANCELLED
    assert effective_state(make_job()) == STATE_WORKING


def test_effective_state_never_overrides_a_real_terminal() -> None:
    """A job that COMPLETED just before the cancel was recorded keeps its
    outcome - the flag describes a request, not an outcome."""
    done = make_job(state=STATE_COMPLETED, cancel_requested=True)
    assert effective_state(done) == STATE_COMPLETED


def test_clamp_ttl_bounds() -> None:
    assert clamp_ttl(None) == MAX_TTL_S
    assert clamp_ttl(10) == MIN_TTL_S
    assert clamp_ttl(10**9) == MAX_TTL_S
    assert clamp_ttl(3600) == 3600.0
    assert clamp_ttl("7200") == 7200.0


def test_clamp_ttl_swallows_nan_and_inf() -> None:
    """NaN compares False against both bounds, so a naive clamp passes it
    through and every expiry check answers 'not expired' forever."""
    assert clamp_ttl(float("nan")) == MAX_TTL_S
    assert clamp_ttl(float("inf")) == MAX_TTL_S
    assert clamp_ttl(float("-inf")) == MIN_TTL_S
    assert clamp_ttl("not-a-number") == MAX_TTL_S  # type: ignore[arg-type]


def test_result_available_is_claim_aware() -> None:
    assert result_available(make_job(state=STATE_COMPLETED))
    assert not result_available(make_job(state=STATE_COMPLETED, result_claimed_at="t"))
    assert not result_available(make_job(state=STATE_WORKING))


def test_job_terminal_property() -> None:
    assert make_job(state=STATE_FAILED).terminal
    assert not make_job().terminal


# --- store layer (recording fake) ------------------------------------------


class FakeDB:
    """Records every statement so the atomicity properties can be asserted."""

    def __init__(self, *, fetchrow: Any = None, execute: Any = "UPDATE 1", fetch: Any = None) -> None:
        self._fetchrow = fetchrow
        self._execute = execute
        self._fetch = fetch
        self.sql: list[tuple[str, list[Any]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sql.append((sql, list(args)))
        return self._fetchrow

    async def execute(self, sql: str, *args: Any) -> Any:
        self.sql.append((sql, list(args)))
        return self._execute

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.sql.append((sql, list(args)))
        return self._fetch

    def statements(self) -> list[str]:
        return [s for s, _ in self.sql]


def row(**over: Any) -> dict[str, Any]:
    base = {
        "job_id": JOB,
        "key_id": KEY,
        "server": "sqlhandler",
        "tool": "run_sql",
        "args_sha256": "0" * 64,
        "state": STATE_WORKING,
        "status_message": None,
        "cancel_requested": False,
        "result_claimed_at": None,
        "created_at": "t0",
        "updated_at": "t0",
        "expires_at": "t1",
    }
    base.update(over)
    return base


@pytest.fixture()
def patched(monkeypatch: pytest.MonkeyPatch):
    def install(**kw: Any) -> FakeDB:
        fake = FakeDB(**kw)
        monkeypatch.setattr(tooljobs, "db", fake)
        return fake

    return install


async def test_create_returns_none_when_admission_is_refused(patched) -> None:
    """Cap overflow is a normal condition, not an exception: the relay turns it
    into a tool error the model can route around."""
    patched(fetchrow=None)
    assert await JobStore().create(key_id=KEY, server="s", tool="t", args_sha256="x") is None


async def test_create_admission_and_insert_are_one_statement(patched) -> None:
    """The per-key cap must be checked INSIDE the insert, or two concurrent
    creates on different replicas both pass the check and overshoot the cap."""
    fake = patched(fetchrow=row())
    job = await JobStore().create(key_id=KEY, server="s", tool="t", args_sha256="x")
    assert job is not None and job.state == STATE_WORKING
    insert = fake.statements()[0]
    assert insert.strip().startswith("INSERT INTO tool_jobs")
    assert "SELECT count(*)" in insert, "admission count must ride the insert"
    assert "$8" in insert, "the cap must be a bound parameter, not interpolated"
    assert MAX_ACTIVE_PER_KEY in fake.sql[0][1]


async def test_get_is_owner_scoped(patched) -> None:
    """A wrong key must be indistinguishable from a bogus id: the key filter is
    in the WHERE clause, so a foreign job is simply not found."""
    fake = patched(fetchrow=None)
    assert await JobStore().get(KEY, JOB) is None
    sql = fake.statements()[0]
    assert "job_id = $1 AND key_id = $2" in sql


async def test_every_update_carries_the_terminal_predicate(patched) -> None:
    """The race arbiter. If this predicate is ever dropped, a late complete()
    can resurrect a cancelled job."""
    fake = patched()
    store = JobStore()
    await store.complete(JOB, "result")
    await store.fail(JOB, "boom")
    await store.request_cancel(KEY, JOB)
    updates = [s for s in fake.statements() if s.strip().startswith("UPDATE")]
    assert len(updates) == 3
    for sql in updates:
        assert "state <> ALL(" in sql, f"terminal guard missing from: {sql[:60]}"


async def test_complete_returns_false_when_the_race_is_lost(patched) -> None:
    patched(execute="UPDATE 0")
    assert await JobStore().complete(JOB, "result") is False


async def test_complete_returns_true_when_it_wins(patched) -> None:
    patched(execute="UPDATE 1")
    assert await JobStore().complete(JOB, "result") is True


async def test_oversized_result_fails_the_job_instead_of_truncating(patched) -> None:
    """A prefix of a JSON body is a corrupt artifact, not a small one - so the
    job fails with a reason rather than storing a truncated result."""
    fake = patched(execute="UPDATE 1")
    huge = "x" * (MAX_RESULT_BYTES + 1)
    assert await JobStore().complete(JOB, huge) is False
    assert any("result not retained" in str(args) for _, args in fake.sql)


async def test_fail_refuses_a_non_terminal_target_state() -> None:
    """fail() with state='working' would silently be a no-op UPDATE; refuse
    loudly instead."""
    with pytest.raises(ValueError):
        await JobStore().fail(JOB, "boom", state=STATE_WORKING)


async def test_claim_result_is_once_only(patched) -> None:
    """A replayed request must not re-inject the same tool result into the
    conversation: the first claim wins, the second sees None."""
    fake = patched(fetchrow={"result_body": "the answer"})
    store = JobStore()
    assert await store.claim_result(KEY, JOB) == "the answer"
    sql = fake.statements()[0]
    assert "result_claimed_at IS NULL" in sql
    assert "state = 'completed'" in sql

    patched(fetchrow=None)
    assert await JobStore().claim_result(KEY, JOB) is None


async def test_claim_result_is_owner_scoped(patched) -> None:
    fake = patched(fetchrow={"result_body": "x"})
    await JobStore().claim_result(KEY, JOB)
    assert "key_id = $2" in fake.statements()[0]


async def test_request_cancel_reports_whether_it_was_recorded(patched) -> None:
    patched(execute="UPDATE 1")
    assert await JobStore().request_cancel(KEY, JOB) is True
    patched(execute="UPDATE 0")  # already terminal
    assert await JobStore().request_cancel(KEY, JOB) is False


async def test_reap_expired_reports_count(patched) -> None:
    patched(fetch=[{"job_id": uuid.uuid4()} for _ in range(3)])
    assert await JobStore().reap_expired() == 3


async def test_list_active_bounds_the_limit(patched) -> None:
    fake = patched(fetch=[])
    await JobStore().list_active(KEY, limit=10_000)
    assert fake.sql[0][1][-1] == MAX_ACTIVE_PER_KEY


def test_affected_parses_asyncpg_status() -> None:
    assert tooljobs._affected("UPDATE 1") == 1
    assert tooljobs._affected("UPDATE 0") == 0
    assert tooljobs._affected(2) == 2
    # Unreadable is -1, NOT 0: 0 would mean "no row matched" and silently drop a
    # legitimate completion.
    assert tooljobs._affected("garbage") == -1
    assert tooljobs._affected("") == -1


def test_ddl_matches_the_state_vocabulary() -> None:
    """The CHECK constraint and the Python vocabulary must not drift."""
    for state in (*ACTIVE, *TERMINAL):
        assert f"'{state}'" in tooljobs.DDL
