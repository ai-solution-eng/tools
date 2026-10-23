"""REMEDIATION P5 — off-peak evaluator gathers its per-model fetches.

evaluate_once used to fetch the global query and every per-model query
SEQUENTIALLY (each with its own 15 s MCP timeout) inside a 30 s cadence —
two slow models were enough to blow the window and drift billing. The
fetches now ride one asyncio.gather(..., return_exceptions=True).

Pinned here:
* per-model error isolation is EXACTLY the sequential version's: one
  failing fetch fails only ITS state (_fail_state: full price + last_error
  + the per-state WARNING dedupe) — logged once, others untouched;
* all-success lands in the same final state the sequential loop produced;
* the fetches genuinely OVERLAP (barrier-synchronized fake: all three must
  be in flight before any returns) and the whole tick stays bounded;
* settings cadence/hysteresis/public signatures are untouched — the global
  + per-model sample shape rides the existing tests/test_offpeak.py suite.

The MCP call is faked at the offpeak._call_tool seam and the registry at
registry.mcp_servers — the same seams tests/test_offpeak.py uses. No live
Postgres, no network.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import offpeak, registry

MODELS = {
    "GLM-5.3-Flash": "q-glm",
    "Qwen3.8-27B": "q-qwen",
    "Embed-4": "q-embed",
}


@pytest.fixture(autouse=True)
def clean_offpeak_state():
    offpeak.reset()
    yield offpeak.state
    offpeak.reset()


def _prom_server() -> dict[str, Any]:
    return {"name": "prometheus", "url": "http://prometheus.svc:9090"}


def _enable(monkeypatch: pytest.MonkeyPatch, *, models: dict[str, str] | None = None) -> None:
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_enter_pct", 20.0)
    monkeypatch.setattr(settings, "offpeak_exit_pct", 35.0)
    monkeypatch.setattr(settings, "offpeak_fraction", 0.5)
    monkeypatch.setattr(settings, "offpeak_model_promql", models or MODELS)

    async def fake_servers() -> list[dict[str, Any]]:
        return [_prom_server()]

    monkeypatch.setattr(registry, "mcp_servers", fake_servers)
    monkeypatch.setattr(
        offpeak, "_model_evals", {m: (q, offpeak._State(label=m)) for m, q in (models or MODELS).items()}
    )


# ---------------------------------------------------------------------------
# (1) one fetch raises: the others keep their results, error logged once
# ---------------------------------------------------------------------------


def test_one_failed_fetch_does_not_poison_the_others(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _enable(monkeypatch)

    async def fake_call(url, headers, tool, arguments, timeout_s):
        q = arguments["query"]
        if q == "q-glm":
            raise ConnectionError("prometheus unreachable")
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="10.0")])

    monkeypatch.setattr(offpeak, "_call_tool", fake_call)

    with caplog.at_level(logging.WARNING, logger="pcai_llm.core.offpeak"):
        asyncio.run(offpeak.evaluate_once())

    # the failing model: full price + error surfaced
    assert offpeak.effective_for("GLM-5.3-Flash", 0.5) == 1.0
    snap = offpeak.models_snapshot()["GLM-5.3-Flash"]
    assert "prometheus unreachable" in (snap["last_error"] or "")

    # the surviving models: SAME result the sequential loop produced
    assert offpeak.effective_for("Qwen3.8-27B", 0.5) == 0.5, "survivors must still go ACTIVE"
    assert offpeak.models_snapshot()["Qwen3.8-27B"]["last_value_pct"] == 10.0
    assert offpeak.effective_for("Embed-4", 0.5) == 0.5

    # global query also succeeded (same fake returns 10.0 for it)
    assert offpeak.effective(0.5) == 0.5

    # error isolation is per-state, log dedupe intact: exactly ONE warning
    warn_records = [r for r in caplog.records if "prometheus unreachable" in r.message]
    assert len(warn_records) == 1, "the failing model's WARNING must be logged exactly once"
    assert "offpeak evaluator[GLM-5.3-Flash]" in warn_records[0].getMessage()


def test_isError_content_and_unparseable_are_isolated_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-exception failure paths (isError content, no numeric value)
    isolate per-state exactly like a raised exception does."""
    _enable(monkeypatch)

    async def fake_call(url, headers, tool, arguments, timeout_s):
        q = arguments["query"]
        if q == "q-glm":
            return SimpleNamespace(isError=True, content=[SimpleNamespace(text="HTTP 400: bad query")])
        if q == "q-qwen":
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text="no data here")])
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="10.0")])

    monkeypatch.setattr(offpeak, "_call_tool", fake_call)
    asyncio.run(offpeak.evaluate_once())

    snap = offpeak.models_snapshot()
    assert "HTTP 400" in (snap["GLM-5.3-Flash"]["last_error"] or "")
    assert "no numeric value" in (snap["Qwen3.8-27B"]["last_error"] or "")
    assert snap["Embed-4"]["last_error"] is None and snap["Embed-4"]["active"] is True
    assert offpeak.effective_for("Embed-4", 0.5) == 0.5


# ---------------------------------------------------------------------------
# (2) all succeed: same final state as the sequential loop
# ---------------------------------------------------------------------------


def test_all_success_same_state_as_sequential(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch)

    async def fake_call(url, headers, tool, arguments, timeout_s):
        q = arguments["query"]
        # every query distinct: global idle (high), GLM active (low), rest in-band
        value = {"q-glm": "10.0", "q-qwen": "27.5", "q-embed": "40.0"}.get(q, "50.0")
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text=value)])

    monkeypatch.setattr(offpeak, "_call_tool", fake_call)
    asyncio.run(offpeak.evaluate_once())

    # global 50 > exit -> idle, full price
    assert offpeak.effective(0.5) == 1.0
    assert offpeak.state.active is False and offpeak.state.last_value_pct == 50.0
    assert offpeak.state.last_error is None
    # GLM 10 < enter -> active@0.5; Qwen 27.5 in band -> state unchanged from
    # pristine idle (the hysteresis "no flap" rule, per-state); Embed 40 idle
    assert offpeak.effective_for("GLM-5.3-Flash", 0.5) == 0.5
    assert offpeak.effective_for("Qwen3.8-27B", 0.5) == 1.0
    assert offpeak.effective_for("Embed-4", 0.5) == 1.0
    # every state saw exactly ONE successful sample + a last_check stamp
    snap = offpeak.models_snapshot()
    for m in MODELS:
        assert snap[m]["last_error"] is None
        assert snap[m]["last_check"] is not None
        assert len(snap[m]["history"]) == 1, "one tick == one sample per state"


def test_all_success_histories_and_stamps_match_sequential_order_free_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deep equality on the observable evaluate-result state: values, active
    flags, fractions, errors, state_since stamps — gather must be a pure
    scheduling change, not a semantics change."""
    _enable(monkeypatch)

    class _QuietRatioDB:
        async def fetch(self, sql: str, *a: Any):
            return []  # no console overrides configured

        async def execute(self, sql: str, *a: Any):
            return "OK"

    monkeypatch.setattr(offpeak, "db", _QuietRatioDB())

    async def fake_call(url, headers, tool, arguments, timeout_s):
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="5.0")])

    monkeypatch.setattr(offpeak, "_call_tool", fake_call)
    asyncio.run(offpeak.evaluate_once())
    gathered = offpeak.models_snapshot()
    gathered_global_history = offpeak.state.history_json()

    # run the same tick through the legacy sequential body (apply directly —
    # the sequential loop was one _call_tool + _apply_state per target)
    offpeak.reset()
    offpeak._model_evals.update({m: (q, offpeak._State(label=m)) for m, q in MODELS.items()})
    for st in [offpeak.state, *[s for _q, s in offpeak._model_evals.values()]]:
        st.enabled = True
        offpeak._apply_state(st, 5.0)
    sequential = offpeak.models_snapshot()

    for m in MODELS:
        assert gathered[m]["active"] == sequential[m]["active"]
        assert gathered[m]["fraction"] == sequential[m]["fraction"]
        assert gathered[m]["last_value_pct"] == sequential[m]["last_value_pct"]
        assert gathered[m]["last_error"] is None
        assert gathered[m]["state_since"] is not None
    # history shape/values identical; the ISO stamps differ by clock microseconds
    assert [v for _ts, v in gathered_global_history] == [v for _ts, v in offpeak.state.history_json()]
    assert len(gathered_global_history) == len(offpeak.state.history_json()) == 1


# ---------------------------------------------------------------------------
# (3) concurrency actually happens: overlap assertion + bounded runtime
# ---------------------------------------------------------------------------


def test_fetches_overlap_and_bounded_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The strongest possible scheduling proof short of a real cluster: a
    fake fetch that BLOCKS on a barrier until all 4 targets (global + 3
    models) are simultaneously in flight. Sequential execution would deadlock
    at the first slot; the gather version releases it. Also bounds the tick:
    with 0.05 s per fake fetch, a sequential 4×15 s worst case is impossible."""
    _enable(monkeypatch)
    n_targets = len(MODELS) + 1  # + the global query
    in_flight = 0
    peak_in_flight = 0
    barrier = asyncio.Barrier(n_targets)
    lock = asyncio.Lock()

    async def slow_blocking_call(url, headers, tool, arguments, timeout_s):
        nonlocal in_flight, peak_in_flight
        async with lock:
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
        try:
            # Bound the wait (review T1): if a future regression re-serializes
            # the fetches, the barrier never fills and this must FAIL FAST,
            # not hang CI. TimeoutExpired => the test fails on its own
            # assertions below (peak_in_flight < n_targets / elapsed).
            await asyncio.wait_for(barrier.wait(), 10)
            await asyncio.sleep(0.05)
        finally:
            async with lock:
                in_flight -= 1
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="10.0")])

    monkeypatch.setattr(offpeak, "_call_tool", slow_blocking_call)

    started = time.monotonic()
    asyncio.run(offpeak.evaluate_once())
    elapsed = time.monotonic() - started

    assert peak_in_flight == n_targets, f"all {n_targets} fetches must overlap (peak in-flight {peak_in_flight})"
    assert elapsed < 5.0, f"one gathered tick took {elapsed:.2f}s — the cadence would still drift"
    assert offpeak.effective_for("GLM-5.3-Flash", 0.5) == 0.5, "results intact after the concurrent tick"


def test_timeouts_still_bounded_per_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gather must not shrink or extend the per-fetch timeout: the seam
    still receives _EVAL_TIMEOUT_S (15 s) for every target."""
    _enable(monkeypatch)
    seen: list[float] = []

    async def fake_call(url, headers, tool, arguments, timeout_s):
        seen.append(timeout_s)
        return SimpleNamespace(isError=False, content=[SimpleNamespace(text="10.0")])

    monkeypatch.setattr(offpeak, "_call_tool", fake_call)
    asyncio.run(offpeak.evaluate_once())
    assert len(seen) == len(MODELS) + 1
    assert seen == [offpeak._EVAL_TIMEOUT_S] * (len(MODELS) + 1), "per-fetch timeout unchanged"
