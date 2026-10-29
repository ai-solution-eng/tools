"""model_capacity — per-key, per-model budget headroom for model_list.

The operator's question is "for a given api key, which models still have token
capacity RIGHT NOW?" — answered by one call that must stay TWO aggregate
queries no matter how many models are listed (the naive shape, check_budgets
per model, would be 2*len(models) lineage-scoped ledger SUMs from a UI
endpoint, against the pool the data plane shares).

Semantics pinned here are the GATE's semantics (check_budgets), because
visibility that disagrees with enforcement is worse than no visibility:

* a budget row applies when row["model"] == m or row["model"] == "*";
* token exhaustion is spent >= limit (the gate admits while spent + est <=
  limit, so one token of headroom still admits — it must not read "blocked");
* usd exhaustion is spent_usd >= limit_usd (post-paid, equality is exhausted);
* NO matching row -> UNLIMITED (ok, remaining None) — this mirrors
  check_budgets' any_rows-is-False short-circuit. A key with no budget rows is
  uncapped, not exhausted, and this is the overwhelmingly common case;
* several matching rows -> remaining is the MIN of the token rows' headroom
  and the model is blocked if ANY matching row is exhausted;
* a budget lookup FAULT fails OPEN (every model unlimited + a warning): a DB
  blip must never make models disappear from routing.

DB seam: budget_status is stubbed for the matching-semantics tests (that is
the function under test's only dependency), plus ONE test that drives the
REAL budget_status through a counting fake db to prove the two-query cost.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import pytest

from pcai_llm.core import budgets as budgets_mod


def _tok(model: str, period: str, limit: int, spent: int) -> dict[str, Any]:
    """One token-budget row exactly as budget_status emits it."""
    return {
        "kind": "tokens",
        "model": model,
        "period": period,
        "limit": limit,
        "spent": spent,
        "window_start": "2026-10-01T00:00:00+00:00",
    }


def _usd(model: str, period: str, limit_usd: float, spent_usd: float) -> dict[str, Any]:
    """One USD-budget row exactly as budget_status emits it."""
    return {
        "kind": "usd",
        "model": model,
        "period": period,
        "limit_usd": limit_usd,
        "spent_usd": spent_usd,
        "window_start": "2026-10-01T00:00:00+00:00",
    }


class StatusStub:
    """Replaces core.budgets.budget_status; records every call."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[uuid.UUID] = []
        self.raises = False

    async def __call__(self, key_id: uuid.UUID) -> list[dict[str, Any]]:
        self.calls.append(key_id)
        if self.raises:
            raise RuntimeError("budget lookup unavailable")
        return list(self.rows)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StatusStub:
    s = StatusStub()
    monkeypatch.setattr(budgets_mod, "budget_status", s)
    return s


# ---------------------------------------------------------------------------
# (1) no budget rows at all: UNLIMITED, never "exhausted"
# ---------------------------------------------------------------------------


def test_no_budget_rows_means_unlimited_for_every_model(stub: StatusStub) -> None:
    stub.rows = []
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b", "m-c"]))
    assert set(out) == {"m-a", "m-b", "m-c"}
    for m in ("m-a", "m-b", "m-c"):
        assert out[m] == {"ok": True, "remaining_tokens": None, "blocked_by": ""}


def test_budget_for_a_different_model_does_not_cap_this_one(stub: StatusStub) -> None:
    # An exact-model row for m-other is NOT a matching row for m-a: m-a stays
    # uncapped (None), while m-other reports its headroom.
    stub.rows = [_tok("m-other", "month", 1000, 100)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-other"]))
    assert out["m-a"] == {"ok": True, "remaining_tokens": None, "blocked_by": ""}
    assert out["m-other"]["ok"] is True
    assert out["m-other"]["remaining_tokens"] == 900


# ---------------------------------------------------------------------------
# (2) exact-model token budget: headroom / exhaustion / the admission boundary
# ---------------------------------------------------------------------------


def test_exact_model_token_budget_with_headroom(stub: StatusStub) -> None:
    stub.rows = [_tok("m-a", "month", 1000, 250)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is True
    assert out["m-a"]["remaining_tokens"] == 750
    assert out["m-a"]["blocked_by"] == ""


def test_exact_model_token_budget_exhausted(stub: StatusStub) -> None:
    stub.rows = [_tok("m-a", "month", 1000, 1000)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["remaining_tokens"] == 0
    assert out["m-a"]["blocked_by"]  # non-empty human reason
    assert "month" in out["m-a"]["blocked_by"]


def test_overspent_budget_floors_remaining_at_zero(stub: StatusStub) -> None:
    # Concurrent in-flight requests can overshoot; remaining is never negative.
    stub.rows = [_tok("m-a", "day", 1000, 1400)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["remaining_tokens"] == 0


def test_one_token_of_headroom_still_admits(stub: StatusStub) -> None:
    # GATE parity: check_budgets refuses only when spent + est > limit, so
    # spent == limit - 1 admits and must report ok with 1 token left.
    stub.rows = [_tok("m-a", "month", 1000, 999)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"] == {"ok": True, "remaining_tokens": 1, "blocked_by": ""}


# ---------------------------------------------------------------------------
# (3) wildcard rows apply to every model
# ---------------------------------------------------------------------------


def test_wildcard_budget_applies_to_every_model(stub: StatusStub) -> None:
    stub.rows = [_tok("*", "month", 5000, 1000)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b", "m-c"]))
    for m in ("m-a", "m-b", "m-c"):
        assert out[m]["ok"] is True
        assert out[m]["remaining_tokens"] == 4000


def test_wildcard_exhausted_blocks_every_model(stub: StatusStub) -> None:
    stub.rows = [_tok("*", "day", 1000, 1000)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"]))
    for m in ("m-a", "m-b"):
        assert out[m]["ok"] is False
        assert out[m]["remaining_tokens"] == 0


# ---------------------------------------------------------------------------
# (4) several matching rows: remaining is the MIN, any exhaustion blocks
# ---------------------------------------------------------------------------


def test_wildcard_plus_exact_model_reports_min_remaining(stub: StatusStub) -> None:
    # '*' month cap leaves 4000; the m-a day cap leaves 300 -> 300 is what
    # actually runs out first. m-b has only the wildcard -> 4000.
    stub.rows = [
        _tok("*", "month", 5000, 1000),
        _tok("m-a", "day", 1000, 700),
    ]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"]))
    assert out["m-a"]["ok"] is True
    assert out["m-a"]["remaining_tokens"] == 300
    assert out["m-b"]["ok"] is True
    assert out["m-b"]["remaining_tokens"] == 4000


def test_any_exhausted_matching_row_blocks_the_model(stub: StatusStub) -> None:
    # The wildcard still has room, but the m-a day cap is spent: ANY
    # exhausted matching row refuses the model (check_budgets returns on the
    # first exhausted row it meets).
    stub.rows = [
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


def test_usd_budget_exhausted_blocks_despite_token_headroom(stub: StatusStub) -> None:
    stub.rows = [
        _tok("m-a", "month", 1000, 100),  # plenty of tokens left
        _usd("m-a", "month", 10.0, 10.0),  # dollar cap spent (post-paid equality)
    ]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    assert out["m-a"]["ok"] is False
    assert out["m-a"]["remaining_tokens"] == 900  # token headroom still reported
    assert "cost budget" in out["m-a"]["blocked_by"]


def test_usd_budget_with_room_does_not_block(stub: StatusStub) -> None:
    stub.rows = [_usd("*", "day", 10.0, 2.5)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a"]))
    # A USD-only key carries no TOKEN budget -> remaining_tokens stays None.
    assert out["m-a"] == {"ok": True, "remaining_tokens": None, "blocked_by": ""}


# ---------------------------------------------------------------------------
# (6) fail-open on a budget lookup fault
# ---------------------------------------------------------------------------


def test_budget_status_failure_is_fail_open(stub: StatusStub, caplog: pytest.LogCaptureFixture) -> None:
    stub.raises = True
    with caplog.at_level(logging.WARNING, logger=budgets_mod.log.name):
        out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), ["m-a", "m-b"]))
    # Every model present and unlimited: a lookup fault must never make
    # models disappear from routing.
    assert set(out) == {"m-a", "m-b"}
    for m in ("m-a", "m-b"):
        assert out[m] == {"ok": True, "remaining_tokens": None, "blocked_by": ""}
    assert "model_capacity" in caplog.text


def test_empty_model_list_returns_empty_mapping(stub: StatusStub) -> None:
    stub.rows = [_tok("*", "month", 1000, 1000)]
    assert asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), [])) == {}


# ---------------------------------------------------------------------------
# (7) the perf contract: budget_status is called EXACTLY ONCE
# ---------------------------------------------------------------------------


def test_budget_status_called_once_regardless_of_model_count(stub: StatusStub) -> None:
    stub.rows = [_tok("*", "month", 10_000, 1_000)]
    models = [f"m-{i}" for i in range(25)]
    out = asyncio.run(budgets_mod.model_capacity(uuid.uuid4(), models))
    assert len(out) == 25
    assert len(stub.calls) == 1, f"expected ONE budget_status call, got {len(stub.calls)}"


# ---------------------------------------------------------------------------
# (8) end-to-end through the REAL budget_status: two aggregate queries total
# ---------------------------------------------------------------------------


class _CountingDB:
    """Counting fake for the real budget_status path (repo fake-DB style:
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


def test_real_budget_status_path_is_two_queries_for_many_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _CountingDB()
    fake.token_rows = [{"model": "*", "period": "month", "limit_tokens": 1000, "spent": 250}]
    fake.usd_rows = [{"model": "m-a", "period": "day", "limit_usd": 5.0, "spent": 1.0}]
    monkeypatch.setattr(budgets_mod, "db", fake)

    # The contract is NOT a magic query count — it is that the count is
    # INDEPENDENT of len(models). budget_status costs two ledger aggregates
    # (one per budget family) plus one lineage PK lookup per family; the same
    # constant for 2 models and for 40. That is the whole point of
    # delegating: 40 models must not cost 40 budget lookups.
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
