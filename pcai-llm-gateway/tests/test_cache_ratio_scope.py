"""Per-model scoping of the admission cache-ratio prediction.

rate_limit_predict_cached discounts the ADMISSION estimate by a measured
prefix-cache ratio. Before 2026-11-02 that ratio was scoped to the KEY, which
blended workloads with opposite cache behaviour: this fleet's chat pools run
94-99.7% prefix-cache hits while embedding/reranker/voice pools sit near zero
(one forward pass per unique input). A chat-heavy key therefore discounted its
embedding calls to ~5% of the estimate, and an embedding-heavy key charged its
first big chat call the FULL estimate - still 429ing on tokens the upstream
would have served from cache.

These tests pin the (key, model) scoping:
* the lookup carries the model predicate and the model argument;
* the in-process ratio cache is keyed per (key, model) - two models must not
  share one cached ratio;
* no model in hand = NO prediction (full estimate), never a blended discount;
* a model with no history charges the full estimate (new key / new model);
* settle reconciliation is unaffected: it still corrects to true billable.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod


class RatioDB:
    """Fake ledger: per-model (cached, prompt) totals for the last-N query."""

    def __init__(self, ratios: dict[str, tuple[float, float]]) -> None:
        self.ratios = ratios
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *a: Any):
        self.calls.append((sql, a))
        cached, prompt = self.ratios.get(str(a[1]), (0.0, 0.0))
        return {"cached": cached, "prompt": prompt}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    budgets_mod._predict_cache.clear()
    monkeypatch.setitem(settings.__dict__, "rate_limit_predict_cached", True)
    monkeypatch.setitem(settings.__dict__, "rate_limit_predict_ttl_s", 60.0)
    monkeypatch.setitem(settings.__dict__, "rate_limit_predict_rows", 50)
    yield
    budgets_mod._predict_cache.clear()


def _install(monkeypatch: pytest.MonkeyPatch, ratios: dict[str, tuple[float, float]]) -> RatioDB:
    fake = RatioDB(ratios)
    monkeypatch.setattr(budgets_mod, "db", fake)
    return fake


def test_lookup_is_scoped_to_one_model(monkeypatch: pytest.MonkeyPatch) -> None:
    key = uuid.uuid4()
    fake = _install(monkeypatch, {"glm": (950.0, 1000.0), "embed": (0.0, 1000.0)})

    ratio = asyncio.run(budgets_mod.measured_cache_ratio(key, "glm"))

    assert ratio == pytest.approx(0.95)
    sql, args = fake.calls[-1]
    assert "model = $2" in sql
    assert args[0] == key and args[1] == "glm"


def test_ratio_cache_is_per_model(monkeypatch: pytest.MonkeyPatch) -> None:
    key = uuid.uuid4()
    fake = _install(monkeypatch, {"glm": (950.0, 1000.0), "embed": (0.0, 1000.0)})

    first = asyncio.run(budgets_mod.measured_cache_ratio(key, "glm"))
    second = asyncio.run(budgets_mod.measured_cache_ratio(key, "glm"))
    assert first == second == pytest.approx(0.95)
    assert len(fake.calls) == 1  # cached, no second query

    embed = asyncio.run(budgets_mod.measured_cache_ratio(key, "embed"))
    assert embed == 0.0
    assert len(fake.calls) == 2  # different model = different lookup


def test_predicted_charge_discounts_only_the_matching_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = uuid.uuid4()
    _install(monkeypatch, {"glm": (950.0, 1000.0), "embed": (0.0, 1000.0)})

    assert asyncio.run(budgets_mod.predicted_charge(key, "glm", 1000)) == 50
    assert asyncio.run(budgets_mod.predicted_charge(key, "embed", 1000)) == 1000


def test_no_model_means_no_prediction(monkeypatch: pytest.MonkeyPatch) -> None:
    key = uuid.uuid4()
    fake = _install(monkeypatch, {"glm": (950.0, 1000.0)})

    assert asyncio.run(budgets_mod.predicted_charge(key, "", 1000)) == 1000
    assert fake.calls == []  # never consults a blended ratio


def test_model_without_history_charges_full(monkeypatch: pytest.MonkeyPatch) -> None:
    key = uuid.uuid4()
    _install(monkeypatch, {"glm": (950.0, 1000.0)})

    assert asyncio.run(budgets_mod.predicted_charge(key, "brand-new", 1000)) == 1000


def test_prediction_off_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    key = uuid.uuid4()
    fake = _install(monkeypatch, {"glm": (950.0, 1000.0)})
    monkeypatch.setitem(settings.__dict__, "rate_limit_predict_cached", False)

    assert asyncio.run(budgets_mod.predicted_charge(key, "glm", 1000)) == 1000
    assert fake.calls == []
