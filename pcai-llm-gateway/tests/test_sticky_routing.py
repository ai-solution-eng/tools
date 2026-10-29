"""Never-downgrade sticky routing + route_default_model flag.

Covers the pieces the operator asked for (2026-10-23):

  * apply_never_downgrade is PURE and total: it holds a conversation at or
    above the capability it was already served at, allows escalation, and
    ignores a stale head that left the window/registry/ACL.
  * is_routable_name promotes the legacy 'auto' alias ONLY when the
    route_default_model flag is armed -- arming routing must never silently
    change what an existing client's request does.
  * the routing seam persists a floor ONLY in the branch that actually serves
    (active), never in shadow (which harvests a decision without serving it).
  * stickiness.make_writer / get_sticky_row are fail-open: a DB fault costs
    the floor, never a request.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from pcai_llm.core import model_routing as mr
from pcai_llm.core import stickiness

QWEN = "Qwen3.8-27B"
GLM = "GLM-5.3-Flash"
DS = "Deepseek-V4.1-Flash-1017"
WINDOW = [QWEN, GLM, DS]  # cheap-first: index IS capability
KEY = uuid.UUID("11111111-2222-3333-4444-555555555555")
THREAD = "0123456789abcdef"


# ---------------------------------------------------------------------------
# apply_never_downgrade — the floor
# ---------------------------------------------------------------------------


def test_no_head_leaves_choice_untouched() -> None:
    c = mr.RoutingChoice(QWEN, "prompt_tokens<tier1")
    assert mr.apply_never_downgrade(c, WINDOW, None) is c


def test_same_model_passes_through() -> None:
    c = mr.RoutingChoice(GLM, "heuristic")
    assert mr.apply_never_downgrade(c, WINDOW, GLM).model == GLM


def test_downgrade_is_blocked() -> None:
    """The whole point: a thread served at GLM may not fall back to Qwen."""
    c = mr.RoutingChoice(QWEN, "prompt_tokens<tier1")
    out = mr.apply_never_downgrade(c, WINDOW, GLM)
    assert out.model == GLM
    assert out.reason.startswith("sticky_floor")


def test_escalation_is_allowed() -> None:
    """A 'hi' that becomes a hard refactor MUST be able to climb."""
    c = mr.RoutingChoice(DS, "prompt_tokens>=tier2")
    out = mr.apply_never_downgrade(c, WINDOW, QWEN)
    assert out.model == DS


def test_ceiling_head_holds_against_every_lower_pick() -> None:
    for pick in (QWEN, GLM):
        c = mr.RoutingChoice(pick, "x")
        assert mr.apply_never_downgrade(c, WINDOW, DS).model == DS


def test_stale_head_outside_window_is_ignored() -> None:
    """A head that left the ladder/registry/ACL must not be resurrected."""
    c = mr.RoutingChoice(QWEN, "x")
    assert mr.apply_never_downgrade(c, WINDOW, "Retired-Model").model == QWEN


def test_choice_outside_window_is_ignored_not_raised() -> None:
    """Total by construction: an off-window pick cannot raise."""
    c = mr.RoutingChoice("Not-In-Window", "x")
    assert mr.apply_never_downgrade(c, WINDOW, GLM).model == "Not-In-Window"


def test_empty_candidates_is_inert() -> None:
    c = mr.RoutingChoice(QWEN, "x")
    assert mr.apply_never_downgrade(c, [], GLM).model == QWEN


# ---------------------------------------------------------------------------
# is_routable_name — the route_default_model flag
# ---------------------------------------------------------------------------


def test_pseudo_names_always_route() -> None:
    assert mr.is_routable_name("routing", False)
    assert mr.is_routable_name("routing__" + GLM, False)
    assert mr.is_routable_name("routing", True)


def test_auto_is_opt_in_only() -> None:
    """THE flag's contract: auto keeps its cascade meaning until armed."""
    assert mr.is_routable_name("auto", False) is False
    assert mr.is_routable_name("auto", True) is True


def test_real_models_never_route() -> None:
    for name in (GLM, DS, QWEN):
        assert mr.is_routable_name(name, True) is False


def test_demand_aware_alias_is_not_hijacked() -> None:
    """auto-demand-aware has its own load-aware path; routing must not take it."""
    assert mr.is_routable_name("auto-demand-aware", True) is False


# ---------------------------------------------------------------------------
# the routing seam: persist ONLY when serving
# ---------------------------------------------------------------------------


class _Key:
    def __init__(self) -> None:
        self.user_id = KEY
        self.allowed_models: list[str] = []


async def _run_resolve(monkeypatch, *, policy: str, body_model: str, writes: list):
    """Drive resolve_routed_model with the profile policy pinned."""

    async def _fake_policy(user_id):
        return policy, QWEN

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    return await mr.resolve_routed_model(
        _Key(),
        {"model": body_model, "messages": [{"role": "user", "content": "hi"}]},
        body_model,
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        sticky_floor=QWEN,
        sticky_write=writes.append,
    )


@pytest.mark.asyncio
async def test_active_persists_the_served_model(monkeypatch) -> None:
    writes: list[str] = []
    model, ctx = await _run_resolve(monkeypatch, policy="active", body_model="routing", writes=writes)
    assert ctx.policy == "active"
    assert writes == [model], "the SERVED model must be persisted as the new floor"


@pytest.mark.asyncio
async def test_shadow_persists_nothing(monkeypatch) -> None:
    """Shadow harvests a decision without serving it, so there is no floor to write."""
    writes: list[str] = []
    _, ctx = await _run_resolve(monkeypatch, policy="shadow", body_model="routing", writes=writes)
    assert ctx.policy == "shadow"
    assert writes == []


@pytest.mark.asyncio
async def test_floor_holds_in_active_mode(monkeypatch) -> None:
    """A trivial 'hi' would heuristically pick the floor; the floor is GLM here."""
    writes: list[str] = []
    model, _ = await _run_resolve(monkeypatch, policy="active", body_model="routing", writes=writes)
    assert model == QWEN  # heuristic tier1 pick == the floor passed in
    # Now prove a floor ABOVE the heuristic pick is honoured:
    writes2: list[str] = []
    m2, _ = await mr.resolve_routed_model(
        _Key(),
        {"model": "routing", "messages": [{"role": "user", "content": "hi"}]},
        "routing",
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        sticky_floor=GLM,
        sticky_write=writes2.append,
    )
    assert m2 == GLM, "a conversation served at GLM may not drop to Qwen"
    assert writes2 == [GLM]


# ---------------------------------------------------------------------------
# stickiness helpers — fail-open
# ---------------------------------------------------------------------------


def test_make_writer_none_without_thread() -> None:
    assert stickiness.make_writer(KEY, None) is None


@pytest.mark.asyncio
async def test_make_writer_persists(monkeypatch) -> None:
    seen: list[tuple] = []

    async def _fake_set(k, t, m):
        seen.append((k, t, m))

    monkeypatch.setattr(stickiness, "set_sticky", _fake_set)
    w = stickiness.make_writer(KEY, THREAD)
    assert w is not None
    w(GLM)
    await asyncio.sleep(0)  # let the scheduled task run
    assert seen == [(KEY, THREAD, GLM)]


@pytest.mark.asyncio
async def test_xv3_deferred_writer_persists_only_the_committed_model(monkeypatch) -> None:
    """XV-3 (audit 2026-10-29 P3): the routing pick happens BEFORE the budget
    gates and admission failover, so an eager write records a model that may
    never serve and never-downgrade then holds the NEXT turn at it.

    The deferred writer must persist NOTHING until the caller commits, and must
    then persist the model the caller names — never the pick it was handed.
    """
    seen: list[tuple] = []

    async def _fake_set(k, t, m):
        seen.append((k, t, m))

    monkeypatch.setattr(stickiness, "set_sticky", _fake_set)
    w = stickiness.make_writer(KEY, THREAD, defer=True)
    assert w is not None
    finisher = w(GLM)  # the router's PICK ...
    assert callable(finisher), "a deferring writer must hand back a finisher"
    await asyncio.sleep(0)
    assert seen == [], "nothing may be persisted at pick time"

    finisher(QWEN)  # ... but admission served QWEN
    await asyncio.sleep(0)
    assert seen == [(KEY, THREAD, QWEN)], "the floor records what actually served"


@pytest.mark.asyncio
async def test_xv3_deferred_writer_falls_back_to_the_pick(monkeypatch) -> None:
    """A finisher called with an empty model keeps the pick rather than
    persisting an empty floor (which would be a silently dead sticky row)."""
    seen: list[tuple] = []

    async def _fake_set(k, t, m):
        seen.append((k, t, m))

    monkeypatch.setattr(stickiness, "set_sticky", _fake_set)
    w = stickiness.make_writer(KEY, THREAD, defer=True)
    assert w is not None
    finisher = w(GLM)
    assert callable(finisher)
    finisher("")
    await asyncio.sleep(0)
    assert seen == [(KEY, THREAD, GLM)]


@pytest.mark.asyncio
async def test_xv3_eager_writer_is_unchanged(monkeypatch) -> None:
    """The default stays eager: every pre-existing caller is byte-identical."""
    seen: list[tuple] = []

    async def _fake_set(k, t, m):
        seen.append((k, t, m))

    monkeypatch.setattr(stickiness, "set_sticky", _fake_set)
    w = stickiness.make_writer(KEY, THREAD)
    assert w is not None
    assert w(GLM) is None, "an eager writer returns nothing"
    await asyncio.sleep(0)
    assert seen == [(KEY, THREAD, GLM)]


@pytest.mark.asyncio
async def test_xv3_incumbent_state_wires_a_deferring_writer(monkeypatch) -> None:
    """_routing_incumbent_state must hand the router the DEFERRING writer, or
    the commit never happens and the floor is silently never written."""
    from pcai_llm.api import routes_proxy

    async def _cfg():
        return {"sticky_routing": True}

    monkeypatch.setattr("pcai_llm.core.routing_config.get_config", _cfg)

    async def _row(*a, **k):
        return None

    monkeypatch.setattr(stickiness, "get_sticky_row", _row)
    key = _Key()
    key.id = KEY
    _inc, _floor, writer = await routes_proxy._routing_incumbent_state(
        key, {"messages": [{"role": "user", "content": "hi"}]}
    )
    assert writer is not None, "a conversation fingerprint must always get a writer"
    assert callable(writer(GLM)), "and that writer must defer, not persist"


@pytest.mark.asyncio
async def test_get_sticky_row_never_raises(monkeypatch) -> None:
    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(stickiness.db, "fetchrow", _boom)
    assert await stickiness.get_sticky_row(KEY, THREAD) is None


@pytest.mark.asyncio
async def test_get_sticky_row_returns_model_and_age(monkeypatch) -> None:
    async def _row(sql, *a):
        return {"model": GLM, "age_s": 12.5}

    monkeypatch.setattr(stickiness.db, "fetchrow", _row)
    assert await stickiness.get_sticky_row(KEY, THREAD) == (GLM, 12.5)


# ---------------------------------------------------------------------------
# config defaults
# ---------------------------------------------------------------------------


def test_new_keys_default_to_off() -> None:
    from pcai_llm.core.routing_config import DEFAULTS

    assert DEFAULTS["sticky_routing"] is False
    assert DEFAULTS["route_default_model"] is False
    assert DEFAULTS["sticky_routing_idle_reset_s"] == 1800


# ---------------------------------------------------------------------------
# env <-> DB layering (the helm wiring is only real if something reads it)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_default_reaches_config_and_db_still_wins(monkeypatch) -> None:
    """The env is the deployment DEFAULT, the DB row the OPERATOR override.

    Regression: the three new knobs were first wired into helm + Settings only,
    where nothing read them -- _config() merged DEFAULTS <- DB, so a helm value
    was silently inert. This pins the middle layer.
    """
    from pcai_llm.config import settings
    from pcai_llm.core import routing_config as rc

    class _Empty:
        async def fetch(self, sql, *a):
            return []

    monkeypatch.setattr(rc, "db", _Empty())
    monkeypatch.setattr(settings, "sticky_routing", True)
    monkeypatch.setattr(settings, "route_default_model", True)
    await rc.invalidate()
    cfg = await rc.get_config()
    assert cfg["sticky_routing"] is True, "env must act as the deployment default"
    assert cfg["route_default_model"] is True

    class _Override:
        async def fetch(self, sql, *a):
            return [{"key": "sticky_routing", "value": "b:false"}]

    monkeypatch.setattr(rc, "db", _Override())
    await rc.invalidate()
    assert (await rc.get_config())["sticky_routing"] is False, "DB must beat env"
    await rc.invalidate()


# ---------------------------------------------------------------------------
# CROSS-VALIDATION REGRESSIONS (GLM review, 2026-10-23)
# Each of these FAILED before the fix; they exist so the bug cannot come back.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_thread_bootstraps_a_floor(monkeypatch) -> None:
    """BUG 1: the persist gate must be the WRITER, not a pre-existing floor.

    Before the fix, sticky_write was only invoked when sticky_floor was already
    non-None -- so a brand-new conversation never wrote a row, and the next turn
    again had no floor. The feature could never engage on real traffic.
    """

    async def _fake_policy(user_id):
        return "active", QWEN

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    writes: list[str] = []
    model, ctx = await mr.resolve_routed_model(
        _Key(),
        {"model": "routing", "messages": [{"role": "user", "content": "hi"}]},
        "routing",
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        sticky_floor=None,  # <-- fresh conversation
        sticky_write=writes.append,
    )
    assert ctx.policy == "active"
    assert writes == [model], "a fresh thread MUST persist its first floor"


@pytest.mark.asyncio
async def test_auto_does_not_route_unless_armed(monkeypatch) -> None:
    """C3: 'auto' keeps its documented cascade meaning by default."""

    async def _fake_policy(user_id):
        return "active", QWEN

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    body = {"model": "auto", "messages": [{"role": "user", "content": "x" * 40000}]}
    model, ctx = await mr.resolve_routed_model(
        _Key(),
        body,
        "auto",
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        route_default=False,
    )
    assert model == "auto", "unarmed auto must ride the cascade unchanged"
    assert ctx.reason == "direct"


@pytest.mark.asyncio
async def test_auto_routes_when_armed(monkeypatch) -> None:
    """C3: arming route_default_model must actually engage the router."""

    async def _fake_policy(user_id):
        return "active", QWEN

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    body = {"model": "auto", "messages": [{"role": "user", "content": "x" * 40000}]}
    model, ctx = await mr.resolve_routed_model(
        _Key(),
        body,
        "auto",
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        route_default=True,
    )
    assert model == DS, "a tier2-sized prompt must escalate to the ceiling"
    assert ctx.reason != "direct", "armed auto must produce a real routing decision"


def test_window_without_a_floor_offers_the_whole_ladder() -> None:
    """The decision window deliberately DIFFERS from the heuristic's pick.

    With no routing_min_model, pick_serving_model pins to the ceiling (no
    downgrade) while _window offers the full ladder, so a configured decision
    model may pick any of it. Cross-validation flagged the two as inconsistent;
    it is a real design divergence but PRE-EXISTING and pinned by
    tests/test_decision_routing.py, so harmonising it here would silently change
    which models a decision model may pick. This test pins the intent.
    """
    window = mr._window([QWEN, GLM, DS], DS, "")
    assert window == [QWEN, GLM, DS], "no floor -> full ladder for the decision model"

    # With a floor set, the window starts there (the capability-order invariant
    # the sticky floor depends on).
    assert mr._window([QWEN, GLM, DS], DS, GLM) == [GLM, DS]


def test_pending_task_set_is_bounded() -> None:
    """BUG 3: _PENDING_MAX was declared but never enforced."""
    import asyncio as _aio

    async def _drive() -> None:
        async def _noop(*a, **k):
            return None

        stickiness.set_sticky = _noop  # type: ignore[assignment]
        stickiness._PENDING.clear()
        w = stickiness.make_writer(KEY, THREAD)
        assert w is not None
        for _ in range(stickiness._PENDING_MAX + 50):
            w(GLM)
        await _aio.sleep(0)
        assert len(stickiness._PENDING) <= stickiness._PENDING_MAX

    _aio.run(_drive())


# ---------------------------------------------------------------------------
# WIRING GUARD (GLM review round 2, 2026-10-23)
# A unit test that supplies a kwarg directly cannot catch the proxy FORGETTING
# to forward it -- which is exactly how the C3 fix shipped as dead code. This
# asserts on the call site's source, the same way tests/test_admin_ui.py pins
# UI contracts.
# ---------------------------------------------------------------------------


def test_proxy_forwards_route_default_to_the_router() -> None:
    """route_default_model is inert unless routes_proxy forwards it."""
    import inspect

    from pcai_llm.api import routes_proxy

    src = inspect.getsource(routes_proxy)
    assert "route_default=_rdef," in src, (
        "resolve_routed_model must receive route_default -- without it the "
        "route_default_model flag is dead code and 'auto' never routes"
    )
    # and the flag must actually be READ from the routing config
    assert "route_default_model" in src


def test_proxy_forwards_sticky_arguments() -> None:
    """The floor/writer seam must reach the router too."""
    import inspect

    from pcai_llm.api import routes_proxy

    src = inspect.getsource(routes_proxy)
    assert "sticky_floor=_sticky_floor," in src
    assert "sticky_write=_sticky_write," in src


# ---------------------------------------------------------------------------
# CROSS-FEATURE NAMESPACING (operator question, 2026-10-23)
# model_stickiness is shared by the cascade's load-aware HEAD and routing's
# never-downgrade FLOOR, and both key on the same conversation fingerprint.
# Without a namespace, a cascade head silently became routing's floor.
# ---------------------------------------------------------------------------


def test_routing_and_cascade_keys_never_collide() -> None:
    """Same conversation -> two DIFFERENT rows."""
    conv = "abc123def456abcd"
    routing_key = stickiness.scoped_thread_key(conv, stickiness.SCOPE_ROUTING)
    cascade_key = conv  # the cascade path uses the bare fingerprint
    assert routing_key != cascade_key
    assert routing_key == stickiness.SCOPE_ROUTING + conv


def test_scoped_thread_key_preserves_none() -> None:
    """No fingerprint -> no stickiness, for both scopes."""
    assert stickiness.scoped_thread_key(None, stickiness.SCOPE_ROUTING) is None
    assert stickiness.scoped_thread_key("", stickiness.SCOPE_ROUTING) is None


def test_cascade_head_cannot_become_a_routing_floor() -> None:
    """The regression: a GLM cascade head must NOT hold a routed turn at GLM.

    Reproduces the pre-fix leak: both features used the raw fingerprint, so the
    cascade's stored head was read back as routing's capability floor and every
    later routed turn was pinned to it regardless of the request.
    """
    conv = "sameconv12345678"
    store: dict[str, str] = {conv: GLM}  # cascade wrote its head at the raw key
    routing_key = stickiness.scoped_thread_key(conv, stickiness.SCOPE_ROUTING)
    assert store.get(routing_key) is None, "routing must not see the cascade head"

    # With no floor, a trivial request routes to the cheap model as normal.
    out = mr.apply_never_downgrade(
        mr.RoutingChoice(QWEN, "prompt_tokens<tier1"), [QWEN, GLM, DS], store.get(routing_key)
    )
    assert out.model == QWEN, "routing must not be pinned by the cascade's head"


# ---------------------------------------------------------------------------
# PANEL CONTROLS (operator ask, 2026-10-23)
# The three settings had no UI at all: RoutingConfigPatch omitted them, so they
# could only be set via helm or a direct DB write. These pin the wiring end to
# end -- HTML control, load path, save path, and the PATCH contract.
# ---------------------------------------------------------------------------


def _admin_html() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "src/pcai_llm/ui/admin.html").read_text()


def test_panel_renders_the_three_controls() -> None:
    s = _admin_html()
    assert 'id="rt-routedefault"' in s, "route_default_model needs a checkbox"
    assert 'id="rt-sticky"' in s, "sticky_routing needs a checkbox"
    assert 'id="rt-stickyidle"' in s, "idle reset needs a number input"


def test_panel_loads_saved_values_into_the_controls() -> None:
    s = _admin_html()
    assert "$('rt-routedefault').checked = c.route_default_model === true;" in s
    assert "$('rt-sticky').checked = c.sticky_routing === true;" in s
    assert "$('rt-stickyidle').value = c.sticky_routing_idle_reset_s" in s


def test_panel_saves_the_three_controls() -> None:
    s = _admin_html()
    assert "route_default_model: $('rt-routedefault').checked," in s
    assert "sticky_routing: $('rt-sticky').checked," in s
    assert "sticky_routing_idle_reset_s: $('rt-stickyidle').value" in s


def test_patch_model_accepts_the_three_keys() -> None:
    """A control whose key the PATCH model rejects would 400 on save."""
    from pcai_llm.api.routes_admin import RoutingConfigPatch

    p = RoutingConfigPatch(route_default_model=True, sticky_routing=True, sticky_routing_idle_reset_s=900)
    assert p.model_dump(exclude_unset=True) == {
        "route_default_model": True,
        "sticky_routing": True,
        "sticky_routing_idle_reset_s": 900,
    }


def test_patch_model_bounds_the_idle_reset() -> None:
    from pydantic import ValidationError

    from pcai_llm.api.routes_admin import RoutingConfigPatch

    for bad in (-1, 86401):
        with pytest.raises(ValidationError):
            RoutingConfigPatch(sticky_routing_idle_reset_s=bad)


def test_patch_model_does_not_clear_omitted_keys() -> None:
    """exclude_unset discipline: an omitted key must not become a clear."""
    from pcai_llm.api.routes_admin import RoutingConfigPatch

    assert RoutingConfigPatch().model_dump(exclude_unset=True) == {}
    # ...but an explicit False IS a real choice and must survive.
    assert RoutingConfigPatch(sticky_routing=False).model_dump(exclude_unset=True) == {"sticky_routing": False}


# ---------------------------------------------------------------------------
# ALLOWLIST AS ELIGIBILITY (operator ask, 2026-10-23)
# The console checklist claimed to bound what the router could pick while the
# request path never read it. The ladder is CAPABILITY ORDER; the allowlist is
# ELIGIBILITY. These pin both jobs.
# ---------------------------------------------------------------------------


def test_expand_without_allowlist_is_the_ladder() -> None:
    assert mr.expand_candidates([GLM, DS], None) == [GLM, DS]
    assert mr.expand_candidates([GLM, DS], []) == [GLM, DS]


def test_allowlist_makes_an_off_ladder_model_reachable() -> None:
    """Qwen is not in model_order; ticking it must put it in the window."""
    out = mr.expand_candidates([GLM, DS], [QWEN, GLM, DS])
    assert QWEN in out
    # off-ladder models go to the CHEAP end (no declared capability position)
    assert out[0] == QWEN


def test_allowlist_bounds_and_does_not_merely_widen() -> None:
    """A model on the ladder but NOT ticked must be excluded."""
    assert mr.expand_candidates([GLM, DS], [GLM]) == [GLM]
    assert mr.expand_candidates([GLM, DS], [QWEN]) == [QWEN]


def test_allowlist_preserves_ladder_capability_order() -> None:
    """Ladder members keep their relative order; additions sort by name."""
    out = mr.expand_candidates([GLM, DS], [GLM, DS, QWEN])
    assert out == [QWEN, GLM, DS]
    # and the ladder order is intact among themselves
    assert out.index(GLM) < out.index(DS)


def test_availability_filters_additions_only() -> None:
    out = mr.expand_candidates([GLM, DS], [QWEN, GLM, DS], available=lambda m: m != QWEN)
    assert QWEN not in out
    assert out == [GLM, DS]


def test_stale_allowlist_never_empties_the_candidate_set() -> None:
    """A panel naming only vanished models must not 404 every request."""
    assert mr.expand_candidates([GLM, DS], ["Retired-Model"]) == ["Retired-Model"]


def test_off_ladder_ceiling_is_offered() -> None:
    """routing__Qwen3.8-27B could not reach Qwen before this fix."""
    assert mr._window([GLM, DS], QWEN, "") == [GLM, DS, QWEN]
    # an on-ladder ceiling is unchanged
    assert mr._window([GLM, DS], DS, "") == [GLM, DS]


@pytest.mark.asyncio
async def test_router_offers_the_allowlisted_off_ladder_model(monkeypatch) -> None:
    """End to end through resolve_routed_model: the decide() seam sees Qwen."""

    async def _fake_policy(user_id):
        return "active", ""

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    seen: list[list[str]] = []

    async def decide(candidates, body, score):
        seen.append(list(candidates))
        return mr.DecisionOutcome(ok=True, choice=QWEN, confidence=0.9, reason="decision")

    model, ctx = await mr.resolve_routed_model(
        _Key(),
        {"model": "routing", "messages": [{"role": "user", "content": "hi"}]},
        "routing",
        {QWEN, GLM, DS},
        [],
        [DS, GLM],  # ladder does NOT contain Qwen
        available=None,
        decide=decide,
        routable_models=[QWEN, GLM, DS],
    )
    assert seen and QWEN in seen[0], "the decision model must be offered Qwen"
    assert model == QWEN, "and its pick must be served"
    assert ctx.policy == "active"


def test_ledger_stamp_records_the_full_distribution() -> None:
    """routing_reason carries only the winner; the distribution explains it."""
    route = mr.RouteDecision(
        policy="shadow",
        engaged=True,
        ceiling=DS,
        would=GLM,
        reason="decision:0.62",
        stamp=True,
        probabilities={GLM: 0.62, DS: 0.38},
    )
    stamp = mr.ledger_stamp(route, actual_model=DS)
    assert stamp["routing_probabilities"] == '{"Deepseek-V4.1-Flash-1017":0.38,"GLM-5.3-Flash":0.62}'


def test_ledger_stamp_probabilities_empty_when_no_decision() -> None:
    route = mr.RouteDecision(
        policy="shadow", engaged=True, ceiling=DS, would=DS, reason="prompt_tokens<tier1", stamp=True
    )
    assert mr.ledger_stamp(route, actual_model=DS)["routing_probabilities"] == ""


def test_schema_has_the_probabilities_column() -> None:
    from pcai_llm.db.schema import SCHEMA_SQL

    assert "routing_probabilities" in SCHEMA_SQL


# ---------------------------------------------------------------------------
# AUDIT 2026-10-29 (zone routing) regressions
# ---------------------------------------------------------------------------


async def _resolve_with(monkeypatch, *, policy_pair, routable=None, order=None, registered=None,
                        body_model="routing", body=None, available=None, sticky_floor=None,
                        sticky_write=None, routing_enabled=True):
    """Drive resolve_routed_model with the profile policy pinned to policy_pair.

    policy_pair=None keeps the REAL profile_routing_config (the test must
    then monkeypatch model_routing.db and clear the policy cache itself).
    """
    if policy_pair is not None:
        async def _fake_policy(_user_id):
            return policy_pair

        monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    monkeypatch.setattr(mr.settings, "routing_enabled", routing_enabled)
    return await mr.resolve_routed_model(
        _Key(),
        body or {"model": body_model, "messages": [{"role": "user", "content": "hi"}]},
        body_model,
        registered if registered is not None else {QWEN, GLM, DS},
        [],
        order if order is not None else [DS, GLM, QWEN],
        available=available,
        decide=None,
        sticky_floor=sticky_floor,
        sticky_write=sticky_write,
        routable_models=routable,
    )


async def test_r2_bare_routing_never_serves_an_unticked_head(monkeypatch) -> None:
    """R2 (audit 2026-10-29): the allowlist BOUNDS bare `routing` too.

    Pre-fix: ceiling = order[0] = DS while routable=[GLM, Qwen] -- expand_candidates
    dropped DS, the no_ladder branch served the unticked head anyway, and the shadow
    ledger stamped it as training truth.
    """
    model, ctx = await _resolve_with(
        monkeypatch, policy_pair=("active", ""), routable=[GLM], body={"model": "routing", "messages": [{"role": "user", "content": "x" * 40000}]}
    )
    assert model != DS, "the operator unticked DS; routing must never serve it"
    assert model == GLM
    assert ctx.ceiling == GLM


async def test_r2_shadow_also_respects_the_allowlist_ceiling(monkeypatch) -> None:
    """Shadow must not stamp the excluded head as would-serve training data."""
    _model, ctx = await _resolve_with(
        monkeypatch, policy_pair=("shadow", ""), routable=[GLM], body={"model": "routing", "messages": [{"role": "user", "content": "x" * 40000}]}
    )
    assert ctx.ceiling == GLM
    assert ctx.would != DS


async def test_r2_stale_allowlist_falls_back_to_ladder_head(monkeypatch) -> None:
    """A fully-stale tick list must not 404 the request (graceful degradation)."""
    model, ctx = await _resolve_with(monkeypatch, policy_pair=("active", ""), routable=["Retired-Model"])
    assert model == DS  # nothing survived expand_candidates -> the ladder stands
    assert ctx.ceiling == DS


async def test_r3_global_floor_downgrades_when_profile_has_none(monkeypatch) -> None:
    """R3 (audit 2026-10-29): a fleet-armed router must be able to downgrade.

    Pre-fix the global-arming path had no floor knob: profile floor '' pinned
    every small request to the ceiling (DS) forever. Drives the REAL
    profile_routing_config (a PolicyDB row with an empty floor) so the global
    inheritance seam is exercised, not bypassed by a fake policy.
    """

    class _ProfileDB:
        async def fetchrow(self, sql: str, *args: object):
            return {"routing_policy": "active", "routing_min_model": ""}

    monkeypatch.setattr(mr, "db", _ProfileDB())
    mr._policy_cache.clear()  # the real lookup TTL-caches per user

    async def _fake_gmin():
        return GLM

    monkeypatch.setattr(mr, "global_min_model", _fake_gmin)
    model, ctx = await _resolve_with(monkeypatch, policy_pair=None)
    mr._policy_cache.clear()
    assert model == GLM, "the GLOBAL floor must let the router downgrade off the ceiling"
    assert ctx.reason == "prompt_tokens<tier1"


async def test_r3_profile_floor_wins_over_global_floor(monkeypatch) -> None:
    async def _fake_gmin():
        return GLM

    monkeypatch.setattr(mr, "global_min_model", _fake_gmin)
    model, _ctx = await _resolve_with(monkeypatch, policy_pair=("active", QWEN))
    assert model == QWEN, "the profile's own floor is authoritative"


async def test_r3_global_floor_read_failure_fails_safe_to_ceiling(monkeypatch) -> None:
    """A routing-config blip must not invent a floor (fail-safe: no downgrade)."""
    async def _boom():
        raise RuntimeError("config db down")

    monkeypatch.setattr(mr, "global_min_model", _boom)
    model, _ctx = await _resolve_with(monkeypatch, policy_pair=("active", ""))
    assert model == DS


async def test_r4_named_ceiling_off_ladder_honours_profile_floor(monkeypatch) -> None:
    """R4 (audit 2026-10-29): routing__X below the operator's floor is demoted.

    Pre-fix the no_ladder branch served X pinned even when the profile floor
    sat ABOVE it on the global ladder.
    """
    async def _fake_ladder():
        return [DS, GLM, QWEN]  # most-capable first

    monkeypatch.setattr(mr, "global_ladder", _fake_ladder)
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", GLM),
        order=[DS, GLM],  # Qwen is NOT on the profile ladder
        body_model=f"routing__{QWEN}",
    )
    assert model == GLM, "naming a below-floor model must not step under the floor"
    assert ctx.ceiling == QWEN  # the ASKED ceiling is recorded honestly
    assert ctx.reason == "prompt_tokens<tier1"


async def test_r4_named_ceiling_at_or_above_floor_stays_pinned(monkeypatch) -> None:
    async def _fake_ladder():
        return [DS, GLM, QWEN]

    monkeypatch.setattr(mr, "global_ladder", _fake_ladder)
    model, _ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", GLM),
        order=[DS, GLM],
        body_model=f"routing__{DS}",
        body={"model": f"routing__{DS}", "messages": [{"role": "user", "content": "x" * 40000}]},
    )
    assert model == DS, "a ceiling at/above the floor keeps the shipped pin"


async def test_r4_unmeasurable_floor_keeps_pinned_ceiling(monkeypatch) -> None:
    """Floor not on the global ladder -> cannot compare -> fail-safe: stay pinned."""
    async def _fake_ladder():
        return [DS, GLM, QWEN]

    monkeypatch.setattr(mr, "global_ladder", _fake_ladder)
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", "Ghost-Model"),
        order=[DS, GLM],
        body_model=f"routing__{QWEN}",
    )
    assert model == QWEN and ctx.reason == "no_ladder"


async def test_r4_no_ladder_profile_shadow_stays_byte_identical(monkeypatch) -> None:
    """Shadow serves exactly as 'off' would: the pseudo name rides the alias path."""
    async def _fake_ladder():
        return [DS, GLM, QWEN]

    monkeypatch.setattr(mr, "global_ladder", _fake_ladder)
    body = {"model": f"routing__{QWEN}", "messages": [{"role": "user", "content": "hi"}]}
    model, ctx = await _resolve_with(monkeypatch, policy_pair=("shadow", GLM), order=[], body_model=f"routing__{QWEN}", body=body)
    assert model == QWEN, "shadow must not serve a floor it did not serve under 'off'"
    assert ctx.stamp is False


def test_p3_clear_policy_cache_drops_every_user() -> None:
    """P3 (audit 2026-10-29): the per-user policy cache had NO invalidator."""
    mr._policy_cache[KEY] = (mr.time.monotonic(), "active", GLM)
    try:
        mr.clear_policy_cache()
        assert KEY not in mr._policy_cache
    finally:
        mr._policy_cache.clear()


def test_p3_admin_profile_put_calls_the_invalidator() -> None:
    """WIRING GUARD: the cache is worthless if no admin write path clears it.

    Source assertion, same discipline as the file's other wiring guards: the
    call site must reference the seam added with the cache.
    """
    from pathlib import Path

    src = Path("src/pcai_llm/api/routes_admin.py").read_text()
    assert "clear_policy_cache()" in src, "profile writes must drop the router policy cache"
    # the GLOBAL routing-config write is the kill-switch path: it must drop the
    # cache too, or a panel save lags per user for up to the 60s TTL
    put_start = src.index('async def put_routing_config')
    put_end = src.index('@router.get', put_start)
    assert "clear_policy_cache()" in src[put_start:put_end], (
        "PUT /admin/routing/config must drop the router policy cache (kill-switch lag)"
    )


async def test_p3_sticky_write_deferral_protocol(monkeypatch) -> None:
    """P3 (audit 2026-10-29): the floor was persisted at PICK time, before
    admission could overrule the pick. New-style writers return a finisher;
    the router must surface it on the decision instead of persisting eagerly,
    so the caller can commit the model that REALLY served.
    """
    picks: list[str] = []

    def _deferring_writer(model: str):
        picks.append(model)
        return lambda actual: picks.append(f"commit:{actual}")

    model, ctx = await _resolve_with(
        monkeypatch, policy_pair=("active", QWEN), sticky_write=_deferring_writer
    )
    assert picks == [model], "the pick is recorded, but NOT yet committed"
    assert ctx.sticky_finish is not None, "the finisher must ride out on the decision"
    ctx.sticky_finish(GLM)
    assert picks == [model, f"commit:{GLM}"], "the caller commits the ACTUAL serving model"


async def test_p3_sticky_write_eager_writer_still_works(monkeypatch) -> None:
    """Legacy writers (returning None) keep the old eager behaviour."""
    writes: list[str] = []
    _model, ctx = await _resolve_with(monkeypatch, policy_pair=("active", QWEN), sticky_write=writes.append)
    assert writes and ctx.sticky_finish is None


async def test_p3_sticky_floor_is_availability_aware(monkeypatch) -> None:
    """P3 (audit 2026-10-29): the floor was measured in an availability-blind
    window, pinning conversations to a cooling model. An unavailable head
    must be ignored (the documented stale-head contract), not enforced.
    """
    body = {"model": "routing", "messages": [{"role": "user", "content": "x" * 40000}]}
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", QWEN),
        body=body,
        sticky_floor=GLM,  # the conversation was served at GLM...
        available=lambda m: m != GLM,  # ...which is cooling NOW
    )
    assert model == DS, "a cooling sticky head must not hold the conversation"
    assert ctx.reason != "sticky_floor*"


async def test_p3_sticky_floor_still_holds_when_head_is_available(monkeypatch) -> None:
    """The availability filter must not weaken the floor when the head is fine."""
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", QWEN),
        sticky_floor=GLM,
        available=lambda m: True,
    )
    assert model == GLM
    assert ctx.reason.startswith("sticky_floor")


# ---------------------------------------------------------------------------
# R-F1 + XV-1/XV-2/XV-5 (cross-validation): the nothing-can-serve sentinel is
# a TRANSIENT fact (available() conflates policy ACL, breaker health and
# budget capacity), so BOTH policies degrade to legacy — the router itself
# never fails a request over availability, and admission/failover keeps its
# recovery paths. Shadow additionally still HARVESTS the decision.
# ---------------------------------------------------------------------------


async def test_rf1_shadow_never_refuses_on_an_unavailable_window(monkeypatch) -> None:
    """Shadow + a fully-unavailable window: served as 'off' would serve it.

    The intermediate design raised RoutingRefused(503) from the router itself,
    failing a request the legacy alias path would have served — shadow is
    decision-harvest only.
    """
    body = {"model": "routing", "messages": [{"role": "user", "content": "hi"}]}
    model, _ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("shadow", ""),
        body=body,
        available=lambda m: False,  # nothing in the window can serve
    )
    assert model == "routing", "shadow must ride the legacy alias path, unchanged"


async def test_rf1_shadow_still_harvests_on_an_unavailable_window(monkeypatch) -> None:
    """XV-5: the blank RouteDecision dropped header + ledger row exactly when
    an operator most needs to see why nothing was picked. Shadow keeps its
    stamp with would='' (no pick) and reason nothing_available.
    """
    body = {"model": "routing", "messages": [{"role": "user", "content": "hi"}]}
    _model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("shadow", ""),
        body=body,
        available=lambda m: False,
    )
    assert ctx.policy == "shadow" and ctx.engaged is True
    assert ctx.stamp is True
    assert ctx.would == "" and ctx.reason == "nothing_available"
    assert ctx.ceiling == DS
    # the header shape survives: would falls back to the served model
    assert "shadow" in (mr.route_header(ctx, "routing") or "")


async def test_xv2_transient_unavailability_degrades_not_503(monkeypatch) -> None:
    """XV-2: active + every model unavailable (breaker open / budget 429)
    degrades to legacy — the request is served as-is and admission/failover
    keep their recovery paths; the router does NOT preempt them with a 503.
    """
    body = {"model": "routing", "messages": [{"role": "user", "content": "hi"}]}
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", ""),
        body=body,
        available=lambda m: False,
    )
    assert model == "routing", "transient unavailability must degrade, not refuse"
    assert ctx.policy == "" and ctx.stamp is False, "nothing harvested: routing did not run"


# ---------------------------------------------------------------------------
# XV-1 (cross-validation): the R4 floor demotion adopts the floor only when it
# is LEGAL (inside the operator allowlist via cheap_first) and SERVABLE
# (available(): key ACL + health + capacity) for THIS request. Otherwise the
# ASKED ceiling stays pinned — baseline semantics.
# ---------------------------------------------------------------------------


async def test_xv1_floor_demotion_requires_a_servable_floor(monkeypatch) -> None:
    """Case A: floor excluded by the key's ACL -> keep the pinned ceiling.

    Pre-XV1-fix this demoted to the ACL-excluded floor and pick_serving_model's
    no_ladder branch returned it unchecked: a SERVABLE pinned request 503'd
    (baseline served Qwen).
    """
    async def _fake_ladder():
        return [DS, GLM, QWEN]

    monkeypatch.setattr(mr, "global_ladder", _fake_ladder)
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", GLM),
        order=[DS, GLM],  # Qwen NOT on the profile ladder
        body_model=f"routing__{QWEN}",
        available=lambda m: m != GLM,  # the key may not use the floor
    )
    assert model == QWEN, "an unusable floor must keep the pinned ceiling (baseline parity)"
    assert ctx.reason == "no_ladder"


async def test_xv1_demotion_never_serves_an_allowlist_excluded_floor(monkeypatch) -> None:
    """Case B: floor not in the operator's routing allowlist -> no demotion.

    Pre-XV1-fix the router SERVED the excluded GLM (spend-control bypass:
    expand_candidates' 'a bound that only ever widened would be a lie').
    """
    async def _fake_ladder():
        return [DS, GLM, QWEN]

    monkeypatch.setattr(mr, "global_ladder", _fake_ladder)
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", GLM),
        order=[DS, GLM],
        body_model=f"routing__{QWEN}",
        routable=[DS],  # the operator ticked ONLY DS
        available=None,
    )
    assert model == QWEN, "the demoted floor must not escape the routing allowlist"
    assert ctx.reason == "no_ladder"


async def test_xv1_legal_servable_floor_still_demotes(monkeypatch) -> None:
    """The XV-1 guard must not break the R4 fix: a legal, servable floor still
    holds against a below-floor named ceiling.
    """
    async def _fake_ladder():
        return [DS, GLM, QWEN]

    monkeypatch.setattr(mr, "global_ladder", _fake_ladder)
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", GLM),
        order=[DS, GLM],
        body_model=f"routing__{QWEN}",
        available=lambda m: True,  # the floor is servable for this key
    )
    assert model == GLM
    assert ctx.reason == "prompt_tokens<tier1"


# ---------------------------------------------------------------------------
# XV-6 (cross-validation): routes_proxy refills `order` from global_ladder()
# before resolve_routed_model, so an empty order reaching this function means
# an empty GLOBAL ladder. The contract that actually holds: the named ceiling
# serves pinned under ANY engaged policy (the intermediate design demoted it;
# that branch was unreachable and deleted).
# ---------------------------------------------------------------------------


async def test_xv6_empty_order_named_ceiling_serves_pinned_under_active(monkeypatch) -> None:
    body = {"model": f"routing__{QWEN}", "messages": [{"role": "user", "content": "hi"}]}
    model, ctx = await _resolve_with(
        monkeypatch,
        policy_pair=("active", GLM),
        order=[],  # no profile ladder AND (via routes_proxy's refill) no global ladder
        body_model=f"routing__{QWEN}",
        body=body,
    )
    assert model == QWEN, "empty global ladder -> pinned serve, exactly as 'off' resolves"
    assert ctx.stamp is False
