"""End-to-end bootstrap of the routing incumbent (2026-10-26).

The unit tests exercise resolve_routed_model with an incumbent passed IN.
Nothing tested the code that PRODUCES one -- routes_proxy's incumbent read and
its sticky writer. That gap is why the same defect shipped twice: the writer
sat inside the sticky_routing gate, so _incumbent was permanently None and
hysteresis was inert in the default deployment, while every test stayed green.

These tests drive the REAL _routing_incumbent_state helper. Verified by
mutation: re-gating the writer makes test_writer_is_built_with_sticky_routing_off
fail.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from pcai_llm.api import routes_proxy as rp
from pcai_llm.core import model_routing as mr
from pcai_llm.core import routing_config as rc
from pcai_llm.core import stickiness as st


class _Key:
    user_id = "00000000-0000-0000-0000-000000000001"
    id = "00000000-0000-0000-0000-000000000002"
    allowed_models: ClassVar[list[str]] = []


def _install(monkeypatch, *, sticky_on: bool):
    """Fake config + in-memory model_stickiness, keyed as the real one is."""
    rows: dict[tuple, str] = {}

    async def _cfg():
        return {**rc.DEFAULTS, "sticky_routing": sticky_on}

    monkeypatch.setattr(rc, "get_config", _cfg)

    async def _get(key_id, thread_key):
        if not thread_key:
            return None
        model = rows.get((key_id, thread_key))
        return (model, 0.0) if model else None

    async def _drop(key_id, thread_key):
        rows.pop((key_id, thread_key), None)

    monkeypatch.setattr(st, "get_sticky_row", _get)
    monkeypatch.setattr(st, "drop_sticky", _drop)

    def _writer(key_id, thread_key):
        if not thread_key:
            return None

        def _w(model):
            rows[(key_id, thread_key)] = model

        return _w

    monkeypatch.setattr(st, "make_writer", _writer)
    return rows


def _body():
    return {"messages": [{"role": "user", "content": "hi"}]}


def test_writer_is_built_with_sticky_routing_off(monkeypatch) -> None:
    """The DEFAULT config must still produce a writer.

    Regression (dual review 2026-10-26, HIGH, shipped twice): the writer was
    created inside `if sticky_routing:`, which defaults to False, so no
    routing-scoped row was ever written and the incumbent could never exist.
    """
    _install(monkeypatch, sticky_on=False)
    incumbent, floor, writer = asyncio.run(rp._routing_incumbent_state(_Key(), _body()))
    assert writer is not None, "writer must exist with sticky_routing OFF"
    assert incumbent is None and floor is None, "nothing stored yet"


def _real_thread() -> str:
    """The key the helper actually computes for _body()."""
    from pcai_llm.core import canonical

    return st.scoped_thread_key(
        canonical.thread_key(_body()["messages"], _Key.id), st.SCOPE_ROUTING
    )


def test_floor_stays_opt_in_but_writer_does_not(monkeypatch) -> None:
    """sticky_routing gates the FLOOR only -- never the writer."""
    thread = _real_thread()

    rows = _install(monkeypatch, sticky_on=False)
    rows[(_Key.id, thread)] = "mid"
    incumbent, floor, writer = asyncio.run(rp._routing_incumbent_state(_Key(), _body()))
    assert incumbent == "mid" and floor is None, "floor must stay off"
    assert writer is not None

    rows2 = _install(monkeypatch, sticky_on=True)
    rows2[(_Key.id, thread)] = "mid"
    incumbent2, floor2, writer2 = asyncio.run(rp._routing_incumbent_state(_Key(), _body()))
    assert incumbent2 == "mid" and floor2 == "mid", "floor on when opted in"
    assert writer2 is not None


def test_turn_one_persists_the_model_turn_two_reads_it(monkeypatch) -> None:
    """The loop the feature depends on: serve -> persist -> read -> hold."""
    rows = _install(monkeypatch, sticky_on=False)
    order = ["big", "mid", "small"]

    async def _fake_policy(_u):
        return "active", "small"

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    monkeypatch.setattr(mr.settings, "routing_enabled", True)

    _inc, _floor, writer = asyncio.run(rp._routing_incumbent_state(_Key(), _body()))

    async def _decide1(_c, _b, _s):
        return mr.DecisionOutcome(ok=True, choice="mid", confidence=0.90,
                                  reason="decision", probabilities={"mid": 0.90, "small": 0.10})

    async def _turn1():
        return await mr.resolve_routed_model(
            _Key(), _body(), "routing", set(order), [], order,
            available=None, decide=_decide1, incumbent=None,
            switch_margin=0.10, sticky_write=writer)

    served1, _ = asyncio.run(_turn1())
    assert served1 == "mid"

    # the writer must have persisted it under the REAL thread key
    thread = _real_thread()
    assert rows.get((_Key.id, thread)) == "mid", f"turn 1 must persist; rows={rows}"

    # turn 2 reads the incumbent back through the REAL helper
    incumbent, _f, _w = asyncio.run(rp._routing_incumbent_state(_Key(), _body()))
    assert incumbent == "mid", "turn 2 must read what turn 1 wrote"

    async def _decide2(_c, _b, _s):
        return mr.DecisionOutcome(ok=True, choice="big", confidence=0.56,
                                  reason="decision", probabilities={"big": 0.56, "mid": 0.55})

    async def _turn2():
        return await mr.resolve_routed_model(
            _Key(), _body(), "routing", set(order), [], order,
            available=None, decide=_decide2, incumbent=incumbent,
            switch_margin=0.10, sticky_write=writer)

    served2, route2 = asyncio.run(_turn2())
    assert served2 == "mid", "a marginal challenger must be held"
    assert route2.reason == "hysteresis:held"
