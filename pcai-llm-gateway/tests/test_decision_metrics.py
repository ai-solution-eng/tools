from __future__ import annotations

from pcai_llm.core import metrics as m


def test_decision_metric_helpers_exist_on_both_paths() -> None:
    """The real metrics and the no-op shims must expose the same surface.

    A shim missing one of these would raise AttributeError inside the
    routing seam -- which the seam's own try/except would swallow, silently
    disabling routing on any deployment without prometheus_client.
    """
    assert callable(m.observe_decision_call)
    assert callable(m.observe_route_switch)
    for name in ("DECISION_CALLS", "DECISION_LATENCY", "ROUTE_SWITCHES"):
        assert hasattr(m, name), name


def test_decision_call_outcome_vocabulary() -> None:
    """Pin the outcome labels the seam actually emits.

    These four are the documented vocabulary (metrics.py) and the seam's
    call sites: served | abstained | error | skipped. `error` was unreachable
    before the 2026-10-26 dual review (timeouts arrived as ok=False and were
    ticked abstained), so it is worth pinning that the split exists.
    """
    for outcome in ("served", "abstained", "error", "skipped"):
        m.observe_decision_call(outcome, 100)


def test_route_switch_outcome_vocabulary() -> None:
    for outcome in ("switched", "held", "same", "cold"):
        m.observe_route_switch(outcome)


def test_decision_call_skips_latency_when_not_measured() -> None:
    """A skipped call made no request, so it must not observe a latency."""
    m.observe_decision_call("skipped")
