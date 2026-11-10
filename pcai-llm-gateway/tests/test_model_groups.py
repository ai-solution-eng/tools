"""Model groups: the definition, the load pick, the snapshot and the grp: pin.

WHY these tests exist: a group is a SELECTION path, so the two failures that
would be silent are (1) the stored DEFINITION — a member that is a group,
unknown, duplicated or absent makes the group unservable, and the write path
must reject it — and (2) the PICK — power-of-two over live load, where a
metrics-less member must be neither starved nor flooded. Both are pure, so
they are pinned directly (with a seeded RNG for the draws).

The load snapshot is pinned with a fake collector: min queue per member, a
STALE snapshot reading as no-data, and fail-open behaviour when the collector
is missing or broken (it is added by a parallel change, so "missing" is a
real state). The grp: pin is pinned against an in-memory model_stickiness
table (tests/test_session_affinity.py's pattern) to prove it cannot collide
with the ep: rows.

No live Postgres and no live Prometheus.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
import uuid
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import endpoint_metrics as em
from pcai_llm.core import load_snapshot as ls
from pcai_llm.core import model_groups as mg
from pcai_llm.core import stickiness


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from a cold group cache and a cold load snapshot."""
    monkeypatch.setattr(mg, "_CACHE", None)
    monkeypatch.setattr(mg, "_LOADED_AT", None)
    monkeypatch.setattr(ls, "_SCORES", {})
    monkeypatch.setattr(ls, "_KEY_SCORES", {})
    monkeypatch.setattr(ls, "_LAST_OK", None)
    monkeypatch.setattr(ls, "_WARNED_MISSING", False)
    monkeypatch.setattr(ls, "_TASK", None)
    monkeypatch.setattr(ls, "_STOP", None)


# --- fixtures / fakes ------------------------------------------------------


class _FakeDB:
    """The model_groups read, counting calls so the cache is observable."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.calls = 0

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM model_groups" in sql
        self.calls += 1
        if self.fail:
            raise RuntimeError("db down")
        return self.rows


def _row(
    name: str,
    members: Any,
    *,
    model_type: str = "chat",
    policy: str = "load",
    description: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "model_type": model_type,
        "policy": policy,
        "members": members,
        "description": description,
    }


def _group(*members: str) -> mg.ModelGroup:
    return mg.ModelGroup(name="g", model_type="chat", policy="load", members=members)


# --- load / snapshot -------------------------------------------------------


def test_load_parses_jsonb_members_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncpg hands JSONB back as TEXT (no codec is registered): parse it."""
    fake = _FakeDB([_row("balanced", '["GLM-5.3-Flash", "Deepseek-V4.1"]', description="tier")])
    monkeypatch.setattr(mg, "db", fake)

    got = asyncio.run(mg.load())

    assert list(got) == ["balanced"]
    group = got["balanced"]
    assert group.members == ("GLM-5.3-Flash", "Deepseek-V4.1")
    assert group.model_type == "chat"
    assert group.policy == "load"
    assert group.description == "tier"
    assert mg.is_group("balanced") is True
    assert mg.member_models("balanced") == ("GLM-5.3-Flash", "Deepseek-V4.1")
    assert mg.group_for("balanced") == group

    # Second read inside the TTL: served from the cache, no second query.
    assert asyncio.run(mg.load()) == got
    assert fake.calls == 1


def test_load_accepts_decoded_dict_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """The schema stores [{model, weight}]; a decoded list must work too."""
    fake = _FakeDB([_row("g", [{"model": "a", "weight": 1}, {"model": "b", "weight": 1}])])
    monkeypatch.setattr(mg, "db", fake)

    got = asyncio.run(mg.load())

    assert got["g"].members == ("a", "b")


def test_load_dedupes_members_in_stored_order(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB([_row("g", '["b", "a", "b"]')])
    monkeypatch.setattr(mg, "db", fake)

    assert asyncio.run(mg.load())["g"].members == ("b", "a")


def test_load_drops_a_group_with_no_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """A group that can never select anything must not be advertised."""
    fake = _FakeDB([_row("empty", "[]"), _row("broken", "not json")])
    monkeypatch.setattr(mg, "db", fake)

    assert asyncio.run(mg.load()) == {}


def test_load_force_bypasses_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB([_row("g", '["a"]')])
    monkeypatch.setattr(mg, "db", fake)

    asyncio.run(mg.load())
    asyncio.run(mg.load(force=True))

    assert fake.calls == 2


def test_load_fault_serves_the_last_good_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB([_row("g", '["a"]')])
    monkeypatch.setattr(mg, "db", fake)
    good = asyncio.run(mg.load())

    fake.fail = True
    again = asyncio.run(mg.load(force=True))

    assert again == good
    assert mg.is_group("g") is True


def test_load_fault_with_no_snapshot_is_empty_not_an_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mg, "db", _FakeDB(fail=True))

    assert asyncio.run(mg.load()) == {}
    assert mg.is_group("g") is False
    assert mg.member_models("g") == ()


def test_invalidate_drops_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeDB([_row("g", '["a"]')])
    monkeypatch.setattr(mg, "db", fake)
    asyncio.run(mg.load())

    asyncio.run(mg.invalidate())

    assert mg.snapshot() == {}
    assert mg.is_group("g") is False
    asyncio.run(mg.load())
    assert fake.calls == 2


def test_snapshot_is_a_copy_of_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mg, "db", _FakeDB([_row("g", '["a"]')]))
    asyncio.run(mg.load())

    view = mg.snapshot()
    view.clear()

    assert mg.is_group("g") is True


# --- validate_members ------------------------------------------------------

_ALL = {"a", "b", "c"}
_GROUPS = {"g1": _group("a")}


def test_validate_members_accepts_a_clean_set() -> None:
    assert mg.validate_members(["a", "b"], _ALL, _GROUPS) is None


def test_validate_members_trims_whitespace() -> None:
    assert mg.validate_members(["  a  ", "b"], _ALL, _GROUPS) is None


def test_validate_members_rejects_empty() -> None:
    assert mg.validate_members([], _ALL, _GROUPS) == "a group needs at least one member"
    assert mg.validate_members(["", "   "], _ALL, _GROUPS) == "a group needs at least one member"


def test_validate_members_rejects_unknown_model() -> None:
    assert mg.validate_members(["a", "ghost"], _ALL, _GROUPS) == "unknown model: ghost"


def test_validate_members_rejects_a_group_member() -> None:
    """One level only: a member may not be a group (no cycles can be stored)."""
    assert mg.validate_members(["g1"], _ALL, _GROUPS) == "member 'g1' is a group (groups are one level)"


def test_validate_members_rejects_duplicates() -> None:
    assert mg.validate_members(["a", "a"], _ALL, _GROUPS) == "duplicate member: a"


def test_validate_members_rejects_too_many() -> None:
    wide = [f"m{i}" for i in range(mg.MAX_MEMBERS + 1)]

    assert mg.validate_members(wide, set(wide), {}) == f"too many members ({mg.MAX_MEMBERS + 1} > {mg.MAX_MEMBERS})"


def test_validate_members_accepts_exactly_the_cap() -> None:
    wide = [f"m{i}" for i in range(mg.MAX_MEMBERS)]

    assert mg.validate_members(wide, set(wide), {}) is None


# --- pick_member -----------------------------------------------------------


def test_pick_member_returns_the_only_eligible_member() -> None:
    assert mg.pick_member(_group("a"), None, lambda _m: None) == "a"


def test_pick_member_filters_by_allowed_models() -> None:
    group = _group("a", "b")
    scores = {"a": 0.0, "b": 9.0}

    assert mg.pick_member(group, {"b"}, scores.get) == "b"


def test_pick_member_returns_none_when_the_acl_excludes_everyone() -> None:
    assert mg.pick_member(_group("a", "b"), {"c"}, lambda _m: 0.0) is None


def test_pick_member_is_deterministic_for_a_seeded_rng() -> None:
    group = _group("a", "b", "c")
    scores = {"a": 1.0, "b": 2.0, "c": 3.0}

    random.seed(1234)
    first = mg.pick_member(group, None, scores.get)
    random.seed(1234)
    second = mg.pick_member(group, None, scores.get)

    assert first == second


def test_pick_member_prefers_the_lower_score() -> None:
    group = _group("idle", "busy")
    scores = {"idle": 0.0, "busy": 10.0}

    picks = [mg.pick_member(group, None, scores.get) for _ in range(20)]

    assert picks == ["idle"] * 20


def test_pick_member_unknown_score_is_a_neutral_rank_over_the_observed_range() -> None:
    """A metrics-less member draws in [min(known), max(known)] -- "average
    load". Over an observed 0..10 range it must win SOME draws and lose some:
    neither a -inf-like sentinel (which would soak the group) nor +inf (which
    would starve it)."""
    group = _group("idle", "busy", "blind")
    scores = {"idle": 0.0, "busy": 10.0}

    random.seed(7)
    picks = [mg.pick_member(group, None, scores.get) for _ in range(400)]

    assert "blind" in picks, "an unknown member must win some draws"
    assert picks.count("blind") < len(picks), "an unknown member must lose some draws"


def test_pick_member_unknown_score_ties_on_a_flat_observed_range() -> None:
    """min == max: the neutral rank IS that value, so the draw ties and the
    first sampled member wins -- the unknown is not systematically favoured."""
    group = _group("a", "b", "blind")
    scores = {"a": 5.0, "b": 5.0}

    random.seed(7)
    picks = [mg.pick_member(group, None, scores.get) for _ in range(400)]

    assert set(picks) == {"a", "b", "blind"}
    assert 0 < picks.count("blind") < len(picks)


def test_pick_member_unknown_score_does_not_soak_a_busy_member() -> None:
    """Regression for the old [0, max] draw: it beat a busy member EVERY time."""
    random.seed(7)
    picks = [mg.pick_member(_group("busy", "blind"), None, lambda m: 5.0 if m == "busy" else None) for _ in range(200)]

    assert "busy" in picks, "a known-busy member must keep its share"
    assert "blind" in picks, "an unknown member must still get picked"


def test_pick_member_all_unknown_still_picks() -> None:
    """No observed scores at all: no range to be neutral about, even spread."""
    random.seed(7)
    picks = [mg.pick_member(_group("a", "b"), None, lambda _m: None) for _ in range(200)]

    assert set(picks) == {"a", "b"}


def test_pick_member_tie_keeps_the_first_sampled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mg.random, "sample", lambda population, k: ["a", "b"])

    assert mg.pick_member(_group("a", "b"), None, lambda _m: 0.0) == "a"


def test_pick_member_treats_a_raising_score_as_unknown() -> None:
    def boom(_member: str) -> float | None:
        raise RuntimeError("metrics down")

    random.seed(3)

    assert mg.pick_member(_group("a", "b"), None, boom) in {"a", "b"}


def test_pick_member_ignores_a_non_finite_score() -> None:
    """inf/NaN is not a score: the member reads as UNKNOWN (neutral rank), not
    as a starving +inf sentinel that can never win."""
    group = _group("inf", "idle")
    scores = {"inf": math.inf, "idle": 0.0}

    random.seed(11)
    picks = [mg.pick_member(group, None, scores.get) for _ in range(200)]

    assert set(picks) == {"inf", "idle"}


# --- load_snapshot ---------------------------------------------------------


def _collector(rows: list[dict[str, Any]]):
    async def _fake(pools: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        return rows

    return _fake


def _refresh(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(em, "load_rows", _collector(rows), raising=False)
    asyncio.run(ls._refresh_once())


def test_snapshot_scores_the_min_queue_per_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A member is as available as its LEAST-loaded replica."""
    _refresh(
        monkeypatch,
        [
            {"pool": "m1", "queue": 5.0, "model_name": ""},
            {"pool": "m1", "queue": 2.0, "model_name": ""},
            {"pool": "m2", "queue": 9.0, "model_name": ""},
        ],
    )

    assert ls.is_fresh() is True
    assert ls.member_score("m1") == 2.0
    assert ls.member_score("m2") == 9.0
    assert ls.member_score("m3") is None


def test_snapshot_falls_back_to_the_model_key_for_unattributed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _refresh(
        monkeypatch,
        [{"pool": "", "queue": 3.0, "model_name": "/mnt/models/models--Qwen--Qwen3.8-27B/snapshots/abc"}],
    )

    assert ls.member_score("Qwen3.8-27B") == 3.0


def test_a_row_without_a_queue_scores_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Present but unsampled is an IDLE pod, not a missing one."""
    _refresh(monkeypatch, [{"pool": "m1", "queue": None, "model_name": ""}])

    assert ls.member_score("m1") == 0.0


def test_never_refreshed_has_no_age_and_no_scores() -> None:
    assert math.isinf(ls.age_s())
    assert ls.is_fresh() is False
    assert ls.member_score("m1") is None


def test_stale_snapshot_reads_as_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_group_load_stale_s", 30.0)
    _refresh(monkeypatch, [{"pool": "m1", "queue": 1.0}])
    assert ls.member_score("m1") == 1.0

    monkeypatch.setattr(ls, "_LAST_OK", time.monotonic() - 31.0)

    assert ls.is_fresh() is False
    assert ls.member_score("m1") is None


def test_refresh_is_fail_open_when_the_collector_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """endpoint_metrics.load_rows is added by a parallel change: absent = no data."""
    monkeypatch.setattr(em, "load_rows", None, raising=False)

    asyncio.run(ls._refresh_once())

    assert ls.is_fresh() is False
    assert ls.member_score("m1") is None


def test_refresh_is_fail_open_when_the_collector_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(pools: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("prometheus down")

    monkeypatch.setattr(em, "load_rows", boom, raising=False)

    asyncio.run(ls._refresh_once())

    assert ls.is_fresh() is False


def test_a_failed_refresh_keeps_the_previous_view(monkeypatch: pytest.MonkeyPatch) -> None:
    _refresh(monkeypatch, [{"pool": "m1", "queue": 4.0}])

    async def boom(pools: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("prometheus down")

    monkeypatch.setattr(em, "load_rows", boom, raising=False)
    asyncio.run(ls._refresh_once())

    assert ls.member_score("m1") == 4.0


def test_start_is_a_noop_when_the_feature_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_groups_enabled", False)

    async def scenario() -> None:
        ls.start()
        assert ls._TASK is None

    asyncio.run(scenario())


def test_start_is_idempotent_and_stop_clears_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_groups_enabled", True)
    monkeypatch.setattr(settings, "model_group_load_refresh_s", 0.01)  # clamped to the 0.1s floor
    calls: list[Any] = []

    async def fake(pools: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        calls.append(pools)
        return [{"pool": "m1", "queue": 0.0, "model_name": ""}]

    monkeypatch.setattr(em, "load_rows", fake, raising=False)

    async def scenario() -> None:
        ls.start()
        task = ls._TASK
        assert task is not None
        ls.start()
        assert ls._TASK is task
        await asyncio.sleep(0.05)
        await ls.stop()
        assert ls._TASK is None

    asyncio.run(scenario())

    assert calls, "the background task never refreshed"


def test_model_key_matches_the_metrics_join_rule() -> None:
    for raw in (
        "/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/snapshots/abc",
        "Qwen/Qwen3-VL-Reranker-8B",
        "Qwen3-VL-Reranker-8B",
    ):
        assert ls._model_key(raw) == em._model_key(raw) == "qwen3vlreranker8b"


# --- grp: pin scope --------------------------------------------------------


class _StickyDB:
    """In-memory model_stickiness table speaking the real SQL shapes."""

    def __init__(self) -> None:
        self.rows: dict[tuple[Any, str], tuple[str, float]] = {}
        self.fail = False

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM model_stickiness" in sql
        if self.fail:
            raise RuntimeError("db down")
        row = self.rows.get((args[0], args[1]))
        if row is None:
            return None
        return {"model": row[0], "age_s": row[1]}

    async def execute(self, sql: str, *args: Any) -> str:
        if self.fail:
            raise RuntimeError("db down")
        if "INSERT INTO model_stickiness" in sql:
            self.rows[(args[0], args[1])] = (args[2], 0.0)
            return "OK"
        if "DELETE FROM model_stickiness" in sql:
            self.rows.pop((args[0], args[1]), None)
            return "OK"
        raise AssertionError(f"unexpected SQL: {sql}")


KEY = uuid.uuid4()
SESSION = "sess-1"


def test_group_and_endpoint_pins_never_share_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _StickyDB()
    monkeypatch.setattr(stickiness, "db", fake)

    asyncio.run(stickiness.set_endpoint_pin(KEY, SESSION, "ep-1"))
    asyncio.run(stickiness.set_group_pin(KEY, SESSION, "GLM-5.3-Flash"))

    assert asyncio.run(stickiness.get_endpoint_pin(KEY, SESSION, 0)) == "ep-1"
    assert asyncio.run(stickiness.get_group_pin(KEY, SESSION, 0)) == "GLM-5.3-Flash"
    assert set(fake.rows) == {(KEY, "ep:sess-1"), (KEY, "grp:sess-1")}


def test_group_pin_expires_on_its_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _StickyDB()
    monkeypatch.setattr(stickiness, "db", fake)
    asyncio.run(stickiness.set_group_pin(KEY, SESSION, "m1"))

    fake.rows[(KEY, "grp:sess-1")] = ("m1", 120.0)

    assert asyncio.run(stickiness.get_group_pin(KEY, SESSION, 60)) is None
    assert (KEY, "grp:sess-1") not in fake.rows, "an expired pin must be dropped, not left behind"


def test_group_pin_drop_forgets_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _StickyDB()
    monkeypatch.setattr(stickiness, "db", fake)
    asyncio.run(stickiness.set_group_pin(KEY, SESSION, "m1"))

    asyncio.run(stickiness.drop_group_pin(KEY, SESSION))

    assert asyncio.run(stickiness.get_group_pin(KEY, SESSION, 0)) is None


def test_group_pin_never_raises_on_a_db_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _StickyDB()
    fake.fail = True
    monkeypatch.setattr(stickiness, "db", fake)

    assert asyncio.run(stickiness.get_group_pin(KEY, SESSION, 60)) is None
    asyncio.run(stickiness.set_group_pin(KEY, SESSION, "m1"))
    asyncio.run(stickiness.drop_group_pin(KEY, SESSION))


def test_group_pin_without_a_session_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stickiness, "db", _StickyDB())

    assert asyncio.run(stickiness.get_group_pin(KEY, None, 60)) is None
    asyncio.run(stickiness.set_group_pin(KEY, None, "m1"))
