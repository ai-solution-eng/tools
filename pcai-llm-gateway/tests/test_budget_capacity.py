"""model_capacity — per-key, per-model budget capacity for model_list.

The operator's question is "for a given api key, which models still have token
capacity RIGHT NOW?" — answered by one call that must stay TWO aggregate
queries no matter how many models are listed (the naive shape, check_budgets
per model, would be 2*len(models) lineage-scoped ledger SUMs from a UI
endpoint, against the pool the data plane shares).

Semantics pinned here are the GATE's semantics (check_budgets), because
visibility that disagrees with enforcement is worse than no visibility:

* a budget row applies when row["model"] == m or row["model"] == "*";
* token exhaustion is spent >= limit — the gate admits while spent + est <=
  limit, so one token of headroom still admits — but the ADMISSION ESTIMATE is
  charged exactly as the gate charges it (est * (1 - budget_cached_exempt_fraction));
* usd exhaustion is spent_usd >= limit_usd (post-paid, equality is exhausted);
* NO matching row -> UNLIMITED (ok, remaining None) — this mirrors
  check_budgets' any_rows-is-False short-circuit. A key with no budget rows is
  uncapped, not exhausted, and this is the overwhelmingly common case;
* several matching rows -> remaining is the MIN of the token rows' headroom
  and the model is blocked if ANY matching row is exhausted;
* a budget lookup FAULT fails OPEN (every model unlimited + a warning): a DB
  blip must never make models disappear from routing.

DB seam: _budget_sums is stubbed for the matching-semantics tests — that is
the SAME helper check_budgets runs, so the parity asserted here is parity with
the gate, not with a display copy of the math. Plus ONE test that drives the
REAL _budget_sums through a counting fake db to prove the query count does not
scale with the model list.

TEAM ENVELOPE (2026-11-03): the gate's verdict is the per-key verdict OVERLAID
by the enrollment's envelope. The cases at the bottom pin that overlay:
pooled caps, derived per-model caps, the overflow swallow (uncommitted
headroom), the team warn band, and fail-open on a team aggregate fault.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import pytest

from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import teams as teams_mod


def _tok(model: str, period: str, limit: int, spent: int) -> dict[str, Any]:
    """One token-budget row exactly as _budget_sums emits it."""
    return {"model": model, "period": period, "limit_tokens": limit, "spent": float(spent)}


def _usd(model: str, period: str, limit_usd: float, spent_usd: float) -> dict[str, Any]:
    """One USD-budget row exactly as _budget_sums emits it."""
    return {"model": model, "period": period, "limit_usd": limit_usd, "spent": float(spent_usd)}


class SumsStub:
    """Replaces core.budgets._budget_sums (the gate's own spend source).

    Keyed by TABLE exactly like the real helper: the token family answers from
    token_rows, the cost family from usd_rows. The production signature is
    kept so a call-site drift fails loudly here.
    """

    def __init__(self) -> None:
        self.token_rows: list[dict[str, Any]] = []
        self.usd_rows: list[dict[str, Any]] = []
        self.calls: list[tuple[str, uuid.UUID]] = []
        self.raises = False

    async def __call__(
        self, table: str, limit_col: str, sum_col: str, key_id: uuid.UUID, model: str | None
    ) -> list[dict[str, Any]]:
        self.calls.append((table, key_id))
        if self.raises:
            raise RuntimeError("budget lookup unavailable")
        return list(self.token_rows if table == "budgets" else self.usd_rows)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> SumsStub:
    s = SumsStub()

    async def _rows_exist(key_id: uuid.UUID, model: str | None) -> bool:
        return bool(s.token_rows or s.usd_rows)

    monkeypatch.setattr(budgets_mod, "_budget_sums", s)
    # The catalog probe is a separate seam: answer it from the same rows so
    # the tests never touch the (absent) database handle.
    monkeypatch.setattr(budgets_mod, "_budget_rows_exist", _rows_exist)
    return s


# ---------------------------------------------------------------------------
# (1) no budget rows at all: UNLIMITED, never "exhausted"
# ---------------------------------------------------------------------------


def test_no_budget_rows_means_unlimited_for_every_model(stub: SumsStub) -> None:
    stub.token_rows = []
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b", "m-c"]))
    assert set(out) == {"m-a", "m-b", "m-c"}
    for m in ("m-a", "m-b", "m-c"):
        assert out[m] == {"ok": True, "remaining_tokens": None, "blocked_by": "", "warn": 0}


def test_budget_for_a_different_model_does_not_cap_this_one(stub: SumsStub) -> None:
    # An exact-model row for m-other is NOT a matching row for m-a: m-a stays
    # uncapped (None), while m-other reports its headroom.
    stub.token_rows = [_tok("m-other", "month", 1000, 100)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-other"]))
    assert out["m-a"] == {"ok": True, "remaining_tokens": None, "blocked_by": "", "warn": 0}
    assert out["m-other"]["ok"] is True
    assert out["m-other"]["remaining_tokens"] == 900


# ---------------------------------------------------------------------------
# (2) exact-model token budget: headroom / exhaustion / the admission boundary
# ---------------------------------------------------------------------------


def test_exact_model_token_budget_with_headroom(stub: SumsStub) -> None:
    stub.token_rows = [_tok("m-a", "month", 1000, 250)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True
    assert out["m-a"]["remaining_tokens"] == 750
    assert out["m-a"]["blocked_by"] == ""
    assert out["m-a"]["warn"] == 0


def test_exact_model_token_budget_exhausted(stub: SumsStub) -> None:
    stub.token_rows = [_tok("m-a", "month", 1000, 1000)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["remaining_tokens"] == 0
    assert out["m-a"]["blocked_by"]  # non-empty human reason
    assert "month" in out["m-a"]["blocked_by"]


def test_overspent_budget_floors_remaining_at_zero(stub: SumsStub) -> None:
    # Concurrent in-flight requests can overshoot; remaining is never negative.
    stub.token_rows = [_tok("m-a", "day", 1000, 1400)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["remaining_tokens"] == 0


def test_one_token_of_headroom_still_admits(stub: SumsStub) -> None:
    # GATE parity: check_budgets refuses only when spent + est > limit, so
    # spent == limit - 1 admits (est 0) and must report ok with 1 token left.
    stub.token_rows = [_tok("m-a", "month", 1000, 999)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    # warn 95 rides along (99.9% consumed) — the gate reports the same band.
    assert out["m-a"] == {"ok": True, "remaining_tokens": 1, "blocked_by": "", "warn": 95}


# ---------------------------------------------------------------------------
# (3) wildcard rows apply to every model
# ---------------------------------------------------------------------------


def test_wildcard_budget_applies_to_every_model(stub: SumsStub) -> None:
    stub.token_rows = [_tok("*", "month", 5000, 1000)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b", "m-c"]))
    for m in ("m-a", "m-b", "m-c"):
        assert out[m]["ok"] is True
        assert out[m]["remaining_tokens"] == 4000


def test_wildcard_exhausted_blocks_every_model(stub: SumsStub) -> None:
    stub.token_rows = [_tok("*", "day", 1000, 1000)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"]))
    for m in ("m-a", "m-b"):
        assert out[m]["ok"] is False
        assert out[m]["remaining_tokens"] == 0


# ---------------------------------------------------------------------------
# (4) several matching rows: remaining is the MIN, any exhaustion blocks
# ---------------------------------------------------------------------------


def test_wildcard_plus_exact_model_reports_min_remaining(stub: SumsStub) -> None:
    # '*' month cap leaves 4000; the m-a day cap leaves 300 -> 300 is what
    # actually runs out first. m-b has only the wildcard -> 4000.
    stub.token_rows = [
        _tok("*", "month", 5000, 1000),
        _tok("m-a", "day", 1000, 700),
    ]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"]))
    assert out["m-a"]["ok"] is True
    assert out["m-a"]["remaining_tokens"] == 300
    assert out["m-b"]["ok"] is True
    assert out["m-b"]["remaining_tokens"] == 4000


def test_any_exhausted_matching_row_blocks_the_model(stub: SumsStub) -> None:
    # The wildcard still has room, but the m-a day cap is spent: ANY
    # exhausted matching row refuses the model (check_budgets returns on the
    # first exhausted row it meets).
    stub.token_rows = [
        _tok("*", "month", 5000, 1000),
        _tok("m-a", "day", 1000, 1000),
    ]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"]))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["remaining_tokens"] == 0
    assert out["m-a"]["blocked_by"]
    assert out["m-b"]["ok"] is True
    assert out["m-b"]["remaining_tokens"] == 4000


# ---------------------------------------------------------------------------
# (5) USD budgets block independently of token headroom
# ---------------------------------------------------------------------------


def test_usd_budget_exhausted_blocks_despite_token_headroom(stub: SumsStub) -> None:
    stub.token_rows = [_tok("m-a", "month", 1000, 100)]  # plenty of tokens left
    stub.usd_rows = [_usd("m-a", "month", 10.0, 10.0)]  # dollar cap spent (post-paid equality)
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["remaining_tokens"] == 900  # token headroom still reported
    assert "cost budget" in out["m-a"]["blocked_by"]


def test_usd_budget_with_room_does_not_block(stub: SumsStub) -> None:
    stub.usd_rows = [_usd("*", "day", 10.0, 2.5)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    # A USD-only key carries no TOKEN budget -> remaining_tokens stays None.
    assert out["m-a"] == {"ok": True, "remaining_tokens": None, "blocked_by": "", "warn": 0}


# ---------------------------------------------------------------------------
# (6) the ADMISSION ESTIMATE is charged exactly as the gate charges it
# ---------------------------------------------------------------------------


def test_est_tokens_blocks_a_model_the_request_would_overshoot(stub: SumsStub) -> None:
    # 5,000 tokens left, a 100,000-token request: at the deployed 0.9 cached
    # fraction the gate charges 10,000 -> refused. Capacity must agree, or the
    # router picks the model and admission 429s.
    stub.token_rows = [_tok("m-a", "month", 10_000, 5_000)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"], 100_000))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["blocked_by"]
    # The SAME row admits the default (est 0) listing read: "has headroom".
    out0 = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out0["m-a"]["ok"] is True


def test_est_tokens_does_not_conjure_a_budget(stub: SumsStub) -> None:
    # No rows at all: uncapped regardless of the request size.
    stub.token_rows = []
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"], 1_000_000))
    assert out["m-a"]["ok"] is True


# ---------------------------------------------------------------------------
# (7) warn bands mirror the gate's 80/95 thresholds
# ---------------------------------------------------------------------------


def test_warn_band_from_token_rows(stub: SumsStub) -> None:
    stub.token_rows = [_tok("m-a", "month", 1000, 850)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True and out["m-a"]["warn"] == 80

    stub.token_rows = [_tok("m-a", "month", 1000, 960)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True and out["m-a"]["warn"] == 95


def test_warn_band_from_usd_rows(stub: SumsStub) -> None:
    stub.usd_rows = [_usd("m-a", "month", 10.0, 9.6)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True and out["m-a"]["warn"] == 95


# ---------------------------------------------------------------------------
# (8) fail-open on a budget lookup fault
# ---------------------------------------------------------------------------


def test_budget_sums_failure_is_fail_open(stub: SumsStub, caplog: pytest.LogCaptureFixture) -> None:
    # A row must exist, or the fast path answers "unlimited" without ever
    # reaching the aggregate this test is about.
    stub.token_rows = [_tok("*", "month", 1000, 1)]
    stub.raises = True
    with caplog.at_level(logging.WARNING, logger=budgets_mod.log.name):
        out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"]))
    # Every model present and unlimited: a lookup fault must never make
    # models disappear from routing.
    assert set(out) == {"m-a", "m-b"}
    for m in ("m-a", "m-b"):
        assert out[m] == {"ok": True, "remaining_tokens": None, "blocked_by": "", "warn": 0}
    assert "model_capacity" in caplog.text


def test_empty_model_list_returns_empty_mapping(stub: SumsStub) -> None:
    stub.token_rows = [_tok("*", "month", 1000, 1000)]
    assert asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), [])) == {}


# ---------------------------------------------------------------------------
# (9) the perf contract: one _budget_sums call per family, not per model
# ---------------------------------------------------------------------------


def test_budget_sums_called_once_per_family_regardless_of_model_count(stub: SumsStub) -> None:
    stub.token_rows = [_tok("*", "month", 10_000, 1_000)]
    models = [f"m-{i}" for i in range(25)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), models))
    assert len(out) == 25
    assert len(stub.calls) == 2, f"expected ONE call per budget family, got {len(stub.calls)}"


# ---------------------------------------------------------------------------
# (10) end-to-end through the REAL _budget_sums: constant query count
# ---------------------------------------------------------------------------


class _CountingDB:
    """Counting fake for the real _budget_sums path (repo fake-DB style:
    classify by the same SQL substrings the production fakes match).

    fetchrow is the ONE lineage PK lookup _budget_sums does per call
    ("SELECT spend_ancestors FROM api_keys"): constant size, no ledger
    touch. Answering None = no rotation lineage, which is the normal key.
    """

    def __init__(self) -> None:
        self.token_rows: list[dict[str, Any]] = []
        self.usd_rows: list[dict[str, Any]] = []
        self.fetch_count = 0
        self.fetchrow_count = 0

    async def fetchrow(self, sql: str, *a: Any):
        # Implicit None = no rotation lineage, which is the normal key; the
        # real _budget_sums reads a missing row as "no ancestors".
        self.fetchrow_count += 1

    async def fetch(self, sql: str, *a: Any):
        self.fetch_count += 1
        if "FROM budgets" in sql:
            return list(self.token_rows)
        if "FROM cost_budgets" in sql:
            return list(self.usd_rows)
        return []


def test_real_budget_sums_path_is_constant_queries_for_many_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _CountingDB()
    fake.token_rows = [{"model": "*", "period": "month", "limit_tokens": 1000, "spent": 250}]
    fake.usd_rows = [{"model": "m-a", "period": "day", "limit_usd": 5.0, "spent": 1.0}]
    monkeypatch.setattr(budgets_mod, "db", fake)

    # The contract is NOT a magic query count — it is that the count is
    # INDEPENDENT of len(models). _budget_sums costs one ledger aggregate per
    # family plus one lineage PK lookup per family; the same constant for 2
    # models and for 40. That is the whole point of delegating: 40 models must
    # not cost 40 budget lookups.
    small = [f"m-{i}" for i in range(2)]
    out_small = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), small))
    counts_small = (fake.fetch_count, fake.fetchrow_count)
    assert counts_small[0] == 2, f"expected 2 aggregates, got {counts_small[0]}"

    fake.fetch_count = fake.fetchrow_count = 0
    large = [f"m-{i}" for i in range(40)]
    out_large = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), large))
    counts_large = (fake.fetch_count, fake.fetchrow_count)

    assert counts_large == counts_small, (
        f"query count must not scale with model count: {len(small)} models -> {counts_small}, "
        f"{len(large)} models -> {counts_large}"
    )
    for m in small:
        assert out_small[m]["ok"] is True
        assert out_small[m]["remaining_tokens"] == 750
    for m in large:
        assert out_large[m]["ok"] is True
        assert out_large[m]["remaining_tokens"] == 750


# ---------------------------------------------------------------------------
# (11) TEAM ENVELOPE — the gate's second layer, mirrored
# ---------------------------------------------------------------------------


def _enrollment(
    *,
    tokens: int | None = None,
    usd: float | None = None,
    period: str = "month",
    overflow: bool = False,
    caps: tuple[teams_mod.TeamModelCap, ...] = (),
) -> teams_mod.TeamEnrollment:
    return teams_mod.TeamEnrollment(
        id=uuid.uuid4(),
        label="team",
        tenant_id=uuid.uuid4(),
        profile_id=None,
        budget_tokens=tokens,
        budget_usd=usd,
        budget_period=period,
        overflow_enabled=overflow,
        model_caps=caps,
    )


class TeamStub:
    """The four team primitives model_capacity consumes, plus the lookup."""

    def __init__(self) -> None:
        self.enrollment: teams_mod.TeamEnrollment | None = None
        self.spend: tuple[int, float] = (0, 0.0)
        self.cap_spends: dict[tuple[str, str], tuple[int, float]] = {}
        self.available: tuple[int | None, float | None] = (None, None)
        self.scope_available: tuple[int | None, float | None] = (None, None)
        self.raises = False


@pytest.fixture
def team(monkeypatch: pytest.MonkeyPatch) -> TeamStub:
    t = TeamStub()

    async def _for_key(key_id: uuid.UUID):
        return t.enrollment

    async def _team_spend(enrollment_id: uuid.UUID, period: str):
        if t.raises:
            raise RuntimeError("team aggregate unavailable")
        return t.spend

    async def _team_cap_spend(enrollment_id: uuid.UUID, scopes: list[tuple[str, str]]):
        if t.raises:
            raise RuntimeError("team aggregate unavailable")
        return {scope: t.cap_spends.get(scope, (0, 0.0)) for scope in scopes}

    async def _team_available(enrollment_id: uuid.UUID, period: str):
        return t.available

    async def _scope_available(enrollment_id: uuid.UUID, model: str, period: str, cap_tokens, cap_usd):
        return t.scope_available

    monkeypatch.setattr(budgets_mod, "_team_for_key", _for_key)
    monkeypatch.setattr(teams_mod, "team_spend", _team_spend)
    monkeypatch.setattr(teams_mod, "team_cap_spend", _team_cap_spend)
    monkeypatch.setattr(teams_mod, "team_available", _team_available)
    monkeypatch.setattr(teams_mod, "team_scope_available", _scope_available)
    return t


def test_team_pooled_exhaustion_blocks_the_model(stub: SumsStub, team: TeamStub) -> None:
    team.enrollment = _enrollment(tokens=100)
    team.spend = (100, 0.0)
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert "team" in out["m-a"]["blocked_by"]
    assert out["m-a"]["team"]["ok"] is False
    assert "team" in out["m-a"]["team"]["blocked_by"]


def test_team_pooled_headroom_admits_and_carries_the_band(stub: SumsStub, team: TeamStub) -> None:
    team.enrollment = _enrollment(tokens=10_000)
    team.spend = (9_500, 0.0)  # 95% of the TEAM pool
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True
    assert out["m-a"]["warn"] == 95, "team overhead must surface 'about to run out'"
    assert out["m-a"]["team"] == {"period": "month", "ok": True, "warn": 95}


def test_team_per_model_cap_blocks_only_that_model(stub: SumsStub, team: TeamStub) -> None:
    team.enrollment = _enrollment(
        caps=(teams_mod.TeamModelCap(model="m-a", period="month", limit_tokens=1000, limit_usd=None),)
    )
    team.cap_spends = {("m-a", "month"): (1000, 0.0)}
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"]))
    assert out["m-a"]["ok"] is False
    assert "m-a" in out["m-a"]["blocked_by"]
    assert out["m-b"]["ok"] is True  # no cap names it, no pooled cap configured


def test_team_overflow_rescues_a_per_key_refusal(stub: SumsStub, team: TeamStub) -> None:
    # Own allocation spent, but the team pool has UNCOMMITTED headroom and the
    # enrollment opted into overflow: the gate admits, so capacity must too.
    stub.token_rows = [_tok("m-a", "month", 1000, 1000)]
    team.enrollment = _enrollment(tokens=10_000, overflow=True)
    team.spend = (0, 0.0)
    team.available = (5_000, None)
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True
    assert out["m-a"]["blocked_by"] == ""


def test_team_overflow_without_headroom_stands(stub: SumsStub, team: TeamStub) -> None:
    stub.token_rows = [_tok("m-a", "month", 1000, 1000)]
    team.enrollment = _enrollment(tokens=10_000, overflow=True)
    team.spend = (0, 0.0)
    team.available = (0, None)  # every token committed to teammates
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert "fully committed" in out["m-a"]["blocked_by"]


def test_team_overflow_off_keeps_the_per_key_refusal(stub: SumsStub, team: TeamStub) -> None:
    stub.token_rows = [_tok("m-a", "month", 1000, 1000)]
    team.enrollment = _enrollment(tokens=10_000, overflow=False)
    team.spend = (0, 0.0)
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["blocked_by"] == "month budget 1000 exceeded (spent 1000)"


def test_team_envelope_is_not_charged_the_estimate(stub: SumsStub, team: TeamStub) -> None:
    # POST-PAID, exactly like the gate: a huge estimate must not move the team
    # verdict (the pool is shared by concurrent teammates).
    team.enrollment = _enrollment(tokens=1000)
    team.spend = (990, 0.0)
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"], 1_000_000))
    assert out["m-a"]["ok"] is True
    assert out["m-a"]["warn"] == 95


def test_team_aggregate_fault_fails_open_to_the_own_verdict(
    stub: SumsStub, team: TeamStub, caplog: pytest.LogCaptureFixture
) -> None:
    stub.token_rows = [_tok("m-a", "month", 1000, 250)]
    team.enrollment = _enrollment(tokens=10_000)
    team.raises = True
    with caplog.at_level(logging.WARNING, logger=budgets_mod.log.name):
        out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True
    assert "team" not in out["m-a"], "an unevaluated envelope must not invent a verdict"
    assert "team spend lookup failed" in caplog.text


def test_team_capped_key_with_no_own_rows_still_takes_the_aggregate_path(stub: SumsStub, team: TeamStub) -> None:
    # The FAST PATH probe only sees OWN rows; a team-capped key must not be
    # short-circuited to "unlimited".
    stub.token_rows = []
    team.enrollment = _enrollment(tokens=100)
    team.spend = (100, 0.0)
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert "team" in out["m-a"]["blocked_by"]


def test_team_per_model_overflow_rescues_only_with_uncommitted_headroom(stub: SumsStub, team: TeamStub) -> None:
    """The DERIVED-envelope twin of the pooled overflow case.

    A per-model cap (N x preset) is its own pool: the per-key refusal is
    swallowed only when THAT pool has uncommitted headroom. This exercises the
    per-model overflow branch (and its scope cache) rather than the pooled one.
    """
    stub.token_rows = [_tok("m-a", "month", 1000, 1000)]  # own allocation spent
    team.enrollment = _enrollment(
        caps=(teams_mod.TeamModelCap(model="m-a", period="month", limit_tokens=10_000, limit_usd=None),),
        overflow=True,
    )
    team.cap_spends = {("m-a", "month"): (0, 0.0)}
    team.scope_available = (500, None)  # pool has room
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True
    assert out["m-a"]["blocked_by"] == ""

    team.scope_available = (0, None)  # every token committed to teammates
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert "fully committed" in out["m-a"]["blocked_by"]
