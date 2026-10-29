"""Complexity-driven model routing via pseudo-model names.

Clients name `routing` (ceiling = the profile's best model) or
`routing__<model>` (ceiling = that model) and the gateway serves the
CHEAPEST profile-ladder model the request's complexity actually fits —
never above the ceiling, never below the profile's `routing_min_model`
floor. The names are deliberately NOT registered in model_endpoints: the
chat path recognizes them by shape, so no registry state and no migration
of existing pools is involved.

WHY heuristics-first (v1): a router that calls a model to pick a model
doubles the latency and the cost of every request it classifies, and a
learned router needs LABELED data before it can be trusted with spend.
v1 is a pure, deterministic function of the request body (estimated
prompt tokens, message count, tool-definition bytes, a capped slice of
requested max_tokens) mapped onto the profile's own model ladder via two
configurable cut points (settings.routing_tier1_tokens /
routing_tier2_tokens). No extra model calls, no state, auditable
line-by-line — and wrong in a BOUNDED way: at worst it serves a model one
segment off, which the shadow ledger will measure.

WHY pre-generation only: a chat stream cannot switch models mid-stream —
the choice is made before the first token and stands for the whole
response. (Mid-relay BUDGET exhaustion still walks the profile ordering
exactly as it does for pinned requests today; that is capacity failover,
not complexity re-decision.)

WHY fail-safe to the ceiling: a routing bug must never make an answer
WORSE than the client asked for. The ceiling is the best model the
profile offers (`routing`) or the model the client explicitly named
(`routing__X`); any router error, missing ladder, or doubt serves the
ceiling (or degrades to the pre-routing alias behavior, which serves the
ceiling first). Routing can only DOWNGRADE when the heuristics positively
say the cheap model fits — never as a side effect of a failure.

Rollout (per-profile `routing_policy`): 'off' (default — both pseudo
names behave exactly as they do today, i.e. as unregistered aliases;
zero behavior change until a profile opts in) → 'shadow' (serve exactly
as 'off' would, but compute the routing decision and stamp it on the
ledger row) → 'active' (serve the routed model through the normal
pinned/budget machinery). The usage_ledger routing_* columns written in
shadow mode are the training set for a future learned router: each row
pairs what served (truth) with what the heuristic WOULD have served.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from ..config import settings
from ..core import metrics as _metrics  # decision-routing counters (no-op shims without prometheus_client)
from ..db import db

log = logging.getLogger(__name__)

PSEUDO_ROUTING = "routing"
PSEUDO_PREFIX = "routing__"

# The legacy cascade alias. NOT a pseudo-model (detect_pseudo_model returns
# None for it), so it normally goes straight to the budget cascade. The
# route_default_model flag is what promotes it to routing -- see
# is_routable_name. Kept here rather than imported from core.cascade so this
# module stays import-light (cascade imports it, not the other way round).
DEFAULT_ALIAS = "auto"

# Requested max_tokens feeds the score CAPPED and DISCOUNTED (min(mt, 4096)
# // 8): agents systematically inflate max_tokens (262144 is a normal harness
# config while actual completions run in the hundreds — the same observation
# that _estimate_tokens' capped completion term answers). Uncapped, the term
# would pin every agent request to the ceiling and the router would never
# route anything; dropped entirely, a genuinely huge generation ask would
# carry no signal. 4096//8 = at most +512 score tokens: enough to lift an
# explicit large-completion ask one tier, never enough to dominate the score.
_MAX_TOKENS_CAP = 4096
_MAX_TOKENS_DIVISOR = 8

# Default hysteresis margin for a model SWITCH (probability points).
# Switching discards the incumbent's KV cache and re-prefills the whole
# context; measured ~300x the compute of one decision call for a 76K agent
# payload. A challenger that merely edges the incumbent out must not be able
# to trigger that. 0.10 is deliberately modest: it blocks 0.62-vs-0.60 coin
# flips while leaving a decisive 0.85-vs-0.15 free to switch.
DEFAULT_SWITCH_MARGIN = 0.10

# Flat per-image complexity charge. Vision is genuinely heavier than the same
# text alone, but scoring it by base64 length was a real bug: a 1.5MB PNG once
# contributed ~500K "tokens" and force-routed every vision request to the
# ceiling. A flat charge keeps a vision request modestly above its text
# equivalent without letting payload size choose the tier.
_IMAGE_TOKEN_EQUIVALENT = 800

# Policy lookup TTL (seconds). The profile's routing knobs change on operator
# action, not per request; the costs/offpeak override caches use the same
# shape (in-memory, TTL, stale-on-error).
_POLICY_TTL_S = 60.0
_policy_cache: dict[uuid.UUID, tuple[float, str, str]] = {}
_POLICY_CACHE_MAX = 5000  # crude cap; bulk-clear beats unbounded growth

# One-time WARNING per user for a profile whose routing_min_model is empty
# or absent from the ladder (floor pins to the ceiling — routing engages but
# never downgrades, which is almost never what the operator intended).
_floor_warned: set[uuid.UUID] = set()
_FLOOR_WARNED_MAX = 5000


class RoutingRefused(Exception):
    """A pseudo-model request routing must refuse with a 4xx.

    Raised for `routing__X` whose ceiling is not a registered model or is
    outside the key's allowed_models (allowed_models is a HARD boundary —
    a routing name must never become an ACL bypass). The proxy maps this
    to fastapi.HTTPException; core modules stay web-framework-free.
    """

    def __init__(self, status: int, message: str) -> None:
        self.status, self.message = status, message
        super().__init__(message)


@dataclass
class PseudoModel:
    """A detected pseudo-model request: `routing` (bare) or `routing__X`."""

    kind: str  # "bare" | "named"
    target: str = ""  # the __X ceiling (named only)


def detect_pseudo_model(name: str) -> PseudoModel | None:
    """Recognize the routing pseudo-models by NAME shape. Pure.

    Exact `routing` → bare; `routing__<something>` → named with the suffix
    as the ceiling. A bare `routing__` (empty ceiling) is NOT a pseudo
    model — it rides the plain (unregistered alias) path like any other
    unknown name. Anything else → None (the request is a normal model
    name; routing never engages on it).
    """
    if name == PSEUDO_ROUTING:
        return PseudoModel("bare")
    if name.startswith(PSEUDO_PREFIX):
        target = name[len(PSEUDO_PREFIX) :]
        if target:
            return PseudoModel("named", target)
    return None


def is_routable_name(name: str, route_default: bool) -> bool:
    """Does this request name something the router should consider? PURE.

    True for the two explicit pseudo-models (routing / routing__X) ALWAYS, and
    for the legacy `auto` alias ONLY when the operator has armed
    route_default_model.

    WHY auto is opt-in: `auto` has a documented, long-standing meaning -- the
    legacy profile-order budget cascade, no sticky read, no load ranking, no
    routing. Flipping it to routing by default would silently change what an
    existing client's request does, which is exactly the failure mode the
    auto/auto-demand-aware split was created to avoid. The flag makes the
    promotion explicit and reversible without a redeploy of client config.

    A request that names a real model is NEVER routable: pinning is how a
    client says "I need this specific model".
    """
    if detect_pseudo_model(name) is not None:
        return True
    return bool(route_default) and name == DEFAULT_ALIAS


def _count_images(body: dict[str, Any]) -> int:
    """Number of image parts across all messages. PURE, never raises.

    Counts the OpenAI vision shape ({"type":"image_url", ...}). Mirrors
    decision_routing._content_parts but stays local: this module must not
    import decision_routing (it is the import-light core).
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            # Must agree with decision_routing._content_parts: an image part
            # without a usable url is NOT an image there, so counting it here
            # would make complexity say "image" while the state says
            # has_image=false -- two sources of truth for one fact.
            if isinstance(part, dict) and part.get("type") == "image_url":
                iu = part.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
                if isinstance(url, str) and url:
                    total += 1
    return total


def complexity_score(body: dict[str, Any]) -> int:
    """Token-denominated complexity score of one chat request. Pure.

    Signals (deliberately cheap — no model calls, no tokenizers):
    * prompt estimate: chars/4 over every message's content — the same
      len//4 heuristic the gates use, so the router and the budget math
      never disagree about what a "token" is. The system prompt is just a
      message and rides along: in RAG/agent traffic it is usually the
      largest single block.
    * tool definitions: DELIBERATELY NOT COUNTED (see the tool_tokens=0 note
      in the body). An agent harness ships the same catalog every turn, so
      tool bytes are a constant that inflates every request toward the
      ceiling without separating easy from hard.
    * images: a FLAT _IMAGE_TOKEN_EQUIVALENT per image part, never a
      base64-length term (which once scored a 1.5MB PNG at ~500K tokens).
    * conversation depth: routing_turn_adder_tokens per message beyond the
      first (state demands capability even when this turn is short).
    * requested max_tokens: capped + discounted (see the constants above).
    """
    messages = body.get("messages") or []
    prompt_est = 0
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        # Text-only estimate (audit GLM-review nit): the old len(str(content))
        # stringified multimodal content-part lists, so a 1.5MB base64 image
        # inflated the complexity score ~500K "tokens" and force-routed vision
        # requests to tier 2. routes_proxy._message_text_tokens is the
        # source of truth, but this module must stay import-light (no
        # routes_proxy import here — it imports this module), so the tiny
        # len//4 heuristic is replicated; the two MUST keep agreeing (this
        # module's own docstring invariant). Non-text parts contribute 0,
        # exactly like the admission estimator.
        if isinstance(content, str):
            prompt_est += len(content) // 4
        elif isinstance(content, list):
            prompt_est += sum(
                len(part["text"]) // 4
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )

    # TOOLS ARE DELIBERATELY NOT COUNTED (2026-10-26).
    # An agent harness ships the SAME tool catalog every turn, so tool bytes are
    # a constant, not a per-request signal. Measured: a 28-tool catalog added a
    # fixed ~199 tokens to every request while the queries spanned a greeting to
    # a Raft implementation -- a constant cannot separate easy from hard, it
    # only inflates every request toward the ceiling. It broke the decision
    # model identically: with tool_count=28 it chose the strongest model even
    # at 50 prompt tokens.
    # tool_count still rides in the decision STATE (a model may weigh it); it
    # just no longer drives the deterministic score.
    tool_tokens = 0

    turn_tokens = max(len(messages) - 1, 0) * max(int(settings.routing_turn_adder_tokens), 0)

    mt_tokens = 0
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        mt_tokens = min(max_tokens, _MAX_TOKENS_CAP) // _MAX_TOKENS_DIVISOR

    # Images are a real complexity signal but must NOT be scored by base64
    # length (a 1.5MB PNG once scored ~500K "tokens" and force-routed every
    # vision call to the ceiling). A flat per-image charge puts a vision request
    # modestly above its text equivalent without letting payload size pick the
    # tier.
    image_tokens = _count_images(body) * _IMAGE_TOKEN_EQUIVALENT

    return prompt_est + tool_tokens + turn_tokens + mt_tokens + image_tokens


def _cut_points() -> tuple[int, int]:
    """The two tier cut points, guaranteed monotonic (t1 < t2).

    A misconfigured env (tier2 <= tier1) would otherwise make every
    non-trivial request tier 2 by accident; clamping keeps the mapping
    deterministic and the failure mode boring.
    """
    t1 = max(int(settings.routing_tier1_tokens), 1)
    t2 = max(int(settings.routing_tier2_tokens), t1 + 1)
    return t1, t2


@dataclass
class RoutingChoice:
    """The pure router's answer: which ladder model serves, and why."""

    model: str
    reason: str


def pick_serving_model(
    cheap_first: list[str],
    ceiling: str,
    floor_model: str,
    score: int,
    available: Callable[[str], bool] | None = None,
) -> RoutingChoice:
    """Map the complexity score onto the cheap-first ladder. PURE.

    Ladder: `cheap_first` is the profile's model_order REVERSED (cheapest
    first); `ceiling` caps the walk (its own position, or the model itself
    when it is not on the ladder — a ceiling off the ladder can't be
    downgraded from, so it serves as-is); `floor_model` (the profile's
    routing_min_model) sets the cheapest allowed position, defaulting to
    the ceiling (no downgrade) when empty or unknown — with the floor
    clamped to never sit above the ceiling ("never above the ceiling" is
    absolute, even against a contradictory floor config).

    Score → position via the two cut points: < tier1 serves the floor
    (cheapest allowed), < tier2 the middle of the allowed window, >= tier2
    the ceiling. The mapped position IS the start of the request's tier
    segment — the cheapest model whose complexity tier the request fits.

    `available` (optional predicate: registered + key-allowed + healthy
    chat pool, built by the caller): when the mapped model can't serve,
    walk UP toward the ceiling — a request that needs tier-N capability
    gets the cheapest AVAILABLE model that still fits. When even the
    ceiling is unavailable, walk DOWN the window to the cheapest available
    model that fits the tier (the audit R-F1 defect: the ceiling used to be
    returned unchecked, so an ACL that excluded the ladder head 403'd every
    routed request). When NO window member can serve, return the
    nothing-available sentinel (model='', reason='nothing_available') — the
    CALLER turns that into the policy answer (active: RoutingRefused(503);
    shadow: legacy degrade — shadow must never fail a request 'off' would
    have served). Availability is a hard fact and always wins over the
    floor; what happens when it is exhausted is policy.
    """
    t1, t2 = _cut_points()
    if ceiling not in cheap_first:
        # Ceiling off the ladder (e.g. routing__X pointing at a registered
        # model the profile never ordered): no ladder → no downgrade → the
        # ceiling itself is the only correct answer.
        return RoutingChoice(ceiling, "no_ladder")
    ceil_pos = cheap_first.index(ceiling)
    if floor_model and floor_model in cheap_first:
        floor_pos = cheap_first.index(floor_model)
    else:
        floor_pos = ceil_pos  # no usable floor → no downgrade (operator warned upstream)
    floor_pos = min(floor_pos, ceil_pos)
    span = ceil_pos - floor_pos

    if score < t1:
        pos, reason = floor_pos, "prompt_tokens<tier1"
    elif score < t2:
        # XV-4 (cross-validation): a span==0 special case here was DEAD CODE —
        # floor+(0+1)//2 == floor == ceil, byte-identical to the plain
        # formula on the whole score grid (proved by A/B against 8a4a329).
        pos, reason = floor_pos + (span + 1) // 2, "prompt_tokens<tier2"
    else:
        pos, reason = ceil_pos, "prompt_tokens>=tier2"

    chosen = cheap_first[pos]
    if available is not None and not available(chosen):
        for p in range(pos + 1, ceil_pos + 1):
            if available(cheap_first[p]):
                chosen = cheap_first[p]
                break
        else:
            # R-F1 (audit 2026-10-29): the ceiling was returned UNCHECKED. With
            # no floor (floor_pos == ceil_pos) the pick IS the ceiling and this
            # walk can never leave it, so a key whose ACL excludes the ladder
            # head was served a model admission then 403'd on EVERY routed
            # request — the feature was unusable, not degraded. Validate the
            # final choice like any other: walk DOWN the window to the
            # cheapest available model that still fits the tier (the floor is
            # a downgrade policy, availability is a hard fact). When NOTHING
            # in the window can serve, SIGNAL (model='', reason
            # 'nothing_available') rather than raise: whether that becomes a
            # refusal (active) or a legacy degrade (shadow, which must never
            # fail a request 'off' would have served) is a POLICY answer only
            # the caller can make.
            for p in range(ceil_pos - 1, floor_pos - 1, -1):
                if available(cheap_first[p]):
                    chosen = cheap_first[p]
                    break
            else:
                return RoutingChoice("", "nothing_available")
    return RoutingChoice(chosen, reason)


def _window(cheap_first: list[str], ceiling: str, min_model: str) -> list[str]:
    """The candidate window (floor..ceiling, cheap-first) the router may pick
    from. PURE.

    Extracted so the decision-model seam and the never-downgrade floor build
    the SAME window: the sticky floor measures capability POSITION inside this
    list, so it must be the same list the router was offered -- otherwise a
    conversation could be held at a model the router was never allowed to
    consider. That is the invariant this helper protects; it does NOT claim the
    window matches the heuristic's pick (see the floor note below).
    """
    if ceiling in cheap_first:
        ceil_pos = cheap_first.index(ceiling)
    else:
        # A ceiling OUTSIDE the window (e.g. routing__Qwen3.8-27B when Qwen is
        # not on the profile ladder). Before this it silently fell back to the
        # window end, so the named ceiling was never offered and the decision
        # model could not pick it -- the alias promised a ceiling it could not
        # honour (defect fix, 2026-10-23). Append it as the most capable
        # position: the operator named it explicitly, so it outranks the
        # ladder for THIS request only. The window is returned, not mutated.
        return list(cheap_first) + [ceiling]
    # No configured floor -> position 0, i.e. the WHOLE ladder. This is the
    # decision seam's long-standing behaviour and it is deliberately NOT the
    # heuristic's rule: pick_serving_model pins to the ceiling when no floor is
    # set ("no downgrade"), while the decision model is allowed to consider the
    # full ladder. The two differ by design, and tests/test_decision_routing.py
    # pins the decision side. Do not "harmonise" them here -- it silently
    # changes which models a configured decision model may pick.
    floor_pos = cheap_first.index(min_model) if min_model and min_model in cheap_first else 0
    return cheap_first[min(floor_pos, ceil_pos) : ceil_pos + 1]


def expand_candidates(
    cheap_first: list[str],
    routable_models: list[str] | None,
    available: Callable[[str], bool] | None = None,
) -> list[str]:
    """The router candidate list, CHEAP-FIRST, widened by the operator allowlist.

    PURE. Two inputs with deliberately different jobs:
      * `cheap_first` (the profile ladder, reversed) supplies CAPABILITY ORDER —
        index 0 is the cheapest model, the last entry the most capable.
      * `routable_models` (the console allowlist) supplies ELIGIBILITY — which
        models the operator has signed off for routing at all.

    Before this the ladder was BOTH, so a ticked model was unreachable unless it
    also sat in `model_order`, while the console claimed a bound it never
    applied (defect, 2026-10-23).

    SEMANTICS — the allowlist BOUNDS, it does not merely add:
      * empty/None -> the whole ladder (the documented "none checked = every
        chat model is routable");
      * non-empty  -> ONLY the allowlisted models, i.e. the operator's set is
        authoritative. A model on the ladder but NOT ticked is excluded: the
        panel says this set bounds what the router may pick, and a bound that
        only ever widened would be a lie.

    ORDER: ladder members keep their ladder position (capability order the admin
    signed off). Allowlisted models NOT on the ladder are appended at the CHEAP
    end, sorted by name. WHY cheap: an off-ladder model has no declared
    capability position, and the router only downgrades when a cheaper model
    positively fits, so an undeclared model must not outrank one the ladder
    placed. WHY name-sorted: a load-ordered window would offer the same request
    different options on different turns.

    `available` (registered + key-ACL + healthy, the caller predicate) filters
    the additions only. It never REMOVES a ladder model: the ladder is the
    profile contract and the existing walk already handles an unservable ladder
    entry by stepping up to the ceiling.
    """
    if not routable_models:
        return list(cheap_first)
    wanted = {str(m) for m in routable_models}
    kept = [m for m in cheap_first if m in wanted]
    extra = sorted(wanted - set(cheap_first))
    if available is not None:
        extra = [m for m in extra if available(m)]
    # All ladder models filtered out (every tick was stale or every ticked model
    # is off-ladder) -> fall back to the additions, then to the ladder, so a
    # mis-ticked panel cannot empty the candidate set and 404 the request.
    return (extra + kept) or extra or list(cheap_first)


def apply_never_downgrade(
    choice: RoutingChoice,
    candidates: list[str],
    sticky_head: str | None,
) -> RoutingChoice:
    """Hold a conversation at or ABOVE the capability it was already served at.

    PURE -- no I/O, no config read; the caller supplies the stored head. Returns
    the choice to serve: either the choice passed in (moving up, or no head),
    or a RoutingChoice naming the sticky head (refusing to move down).

    WHY a floor and not a lock: the operator sketch was "pick once, stay", but
    with capability routing that pins a conversation that OPENED on the
    cheapest model. A thread starting with "hi" (-> Qwen) that then asks for a
    hard refactor would stay on Qwen -- a quality REGRESSION, not a routing
    improvement. Never-downgrade keeps the stability a lock buys (no thrash
    between turns) while still allowing escalation, the only direction that
    can rescue a conversation whose difficulty rose.

    Position is CAPABILITY, not preference: candidates is the caller cheap-first
    (floor..ceiling) window, so a HIGHER index is a more capable model. The
    caller availability-filters the window first (see resolve_routed_model): a
    floor must be measured against what can actually serve.

    Total and fail-open, in the direction that cannot make an answer worse:
      * no head, empty candidates, or a head NOT in the current window (it left
        the ladder, the registry, or the key ACL) -> choice unchanged. A stale
        preference must never resurrect a model the key may no longer use; the
        caller re-persists whatever this returns.
      * head at or above the new pick -> choice (moving up is allowed, and
        staying put is the same model).
      * new pick BELOW the head -> the head, with a reason naming the floor.
    """
    if not sticky_head or not candidates:
        return choice
    if sticky_head not in candidates:
        # Not offered this turn: the ladder moved, the model was deregistered,
        # or the key ACL narrowed. Ignore it rather than forcing it in.
        return choice
    try:
        head_pos = candidates.index(sticky_head)
        pick_pos = candidates.index(choice.model)
    except ValueError:
        # choice.model outside the window (a decision-model pick is validated
        # against it by the caller; this is belt-and-braces).
        return choice
    if pick_pos < head_pos:
        return RoutingChoice(sticky_head, "sticky_floor(was:" + choice.reason + ")")
    return choice


async def profile_routing_config(user_id: uuid.UUID) -> tuple[str, str]:
    """(routing_policy, routing_min_model) for the key's profile.

    The second element is the EFFECTIVE floor: the profile's own
    routing_min_model when set, else the GLOBAL routing_config floor
    (routing_min_model — R3, audit 2026-10-29: the global-arming path had
    no floor knob, so a fleet-armed router could never downgrade). The
    profile value always wins when present.

    TTL-cached per user (the profile knobs change on operator action, not
    per request — same staleness trade as the costs/offpeak caches).
    Fail-safe: any lookup failure returns ('off', '') UNGACHED, so a DB
    blip degrades exactly one request to legacy behavior instead of
    pinning a stale policy for a minute; an unknown policy VALUE also
    reads 'off' (the admin console is the only writer, but a hand-edited
    row must not arm routing by surprise).
    """
    now = time.monotonic()
    cached = _policy_cache.get(user_id)
    if cached is not None and now - cached[0] < _POLICY_TTL_S:
        return cached[1], cached[2]
    try:
        row = await db.fetchrow(
            """
            SELECT p.routing_policy, p.routing_min_model
            FROM users u
            JOIN profiles p ON p.id = u.profile_id
            WHERE u.id = $1
            """,
            user_id,
        )
    except Exception as exc:
        log.warning("routing: profile policy lookup failed (fail-safe 'off'): %s", exc)
        return "off", ""
    policy = (row["routing_policy"] if row else None) or "off"
    min_model = (row["routing_min_model"] if row else None) or ""
    if policy not in ("off", "shadow", "active"):
        log.warning("routing: unknown routing_policy %r treated as 'off'", policy)
        policy = "off"
    # GLOBAL INHERITANCE (2026-10-28). Routing is a fleet-level feature: the
    # operator arms it once in the routing panel (routing_config.routing_policy)
    # instead of editing every profile. A profile that explicitly arms itself
    # (shadow/active) still wins, so early opt-in per profile keeps working.
    # This is what makes the panel honest: before, the panel could read
    # decision_mode=active while every profile sat at the 'off' default, and
    # nothing was served with no visible cause.
    if policy == "off":
        policy = await global_policy()
    if not min_model:
        # R3: inherit the GLOBAL floor when the profile declares none. Read
        # OUTSIDE the policy-inheritance branch so a shadow/active profile
        # without a floor still downgrades — and read through the SAME cached
        # seam as global_policy, so a routing-config DB blip cannot turn into
        # a per-request outage here.
        min_model = await global_min_model()
    _policy_cache[user_id] = (now, policy, min_model)
    if len(_policy_cache) > _POLICY_CACHE_MAX:
        _policy_cache.clear()
    return policy, min_model


async def global_policy() -> str:
    """The fleet-level routing arming from routing_config. NEVER raises. PURE-ish (one cached read).

    Fail-safe is 'off': an unreadable config must not arm routing by accident.
    """
    try:
        from . import routing_config as _rc

        cfg = await _rc.get_config()
        val = str(cfg.get("routing_policy") or "off").strip().lower()
        if val in ("off", "shadow", "active"):
            return val
        log.warning("routing: unknown global routing_policy %r treated as 'off'", val)
    except Exception as exc:
        log.warning("routing: global policy lookup failed (fail-safe 'off'): %s", exc)
    return "off"


def clear_policy_cache() -> None:
    """Drop every cached profile routing policy (admin-write hook).

    P3 (audit 2026-10-29): _policy_cache had NO invalidator, so a floor or
    policy change made in the console lagged up to _POLICY_TTL_S (60s) per
    user in the same process. routing_config.set_values() already invalidates
    its own cache; this is the matching seam for the profile knobs. Bounded
    dict, cleared wholesale on overflow — wholesale clear is the same
    discipline the cache itself uses.
    """
    _policy_cache.clear()


async def global_min_model() -> str:
    """The fleet-level routing floor (weakest allowed model) from routing_config.

    R3 (audit 2026-10-29): the 2026-10-28 global-arming path added a ladder
    but no floor, so a fleet-armed heuristic router was pinned to the ceiling
    and could never downgrade — the feature it enabled was inert. NEVER
    raises; '' means no global floor (pinned to the ceiling, the shipped
    behaviour).
    """
    try:
        from . import routing_config as _rc

        cfg = await _rc.get_config()
        return str(cfg.get("routing_min_model") or "").strip()
    except Exception as exc:
        log.warning("routing: global min-model lookup failed (fail-safe: no floor): %s", exc)
        return ""


async def global_ladder() -> list[str]:
    """The fleet-level capability ladder (MOST-CAPABLE FIRST) from routing_config.

    Used when the calling profile declares no model_order. WHY this exists:
    the router used to borrow profiles.model_order -- a CASCADE ordering -- as
    its ladder, so a profile with an empty order made bare `routing` 404 and
    the feature silently did not exist for that profile. NEVER raises; an
    empty list means "no ladder", which preserves the old 404 exactly.
    """
    try:
        from . import routing_config as _rc

        cfg = await _rc.get_config()
        raw = cfg.get("model_order")
        if isinstance(raw, list):
            return [str(m) for m in raw if str(m).strip()]
    except Exception as exc:
        log.warning("routing: global ladder lookup failed (fail-open: no ladder): %s", exc)
    return []


DecideFn = Callable[[list[str], dict[str, Any], int], Awaitable["DecisionOutcome"]]
"""candidates, request body, complexity -> a decision outcome.

Injected by the caller (routes_proxy) and implemented by core.decision_routing:
this module stays import-light and I/O-free, so the heuristic path keeps its
"pure function of the request body" contract exactly. When no decision model is
configured the caller passes None and every line below is byte-identical to the
pre-decision-model behaviour.
"""


@dataclass
class DecisionOutcome:
    """What the decision model returned, flattened for the router.

    A tiny local shape rather than importing decision_routing: this module must
    not depend on the wire layer. ok=False means "no usable answer" and the
    heuristics stand.
    """

    ok: bool = False
    choice: str = ""
    confidence: float = 0.0
    reason: str = ""
    # The FULL per-option distribution from the decision model (option -> p).
    # Carried so the ledger can record how CLOSE a decision was, not just the
    # winner score: a 0.62 winner over a 0.60 runner-up is a coin flip that
    # happened to clear the floor, while a 0.99 winner is a real signal. Without
    # this the operator can only see the winner and cannot tune the floor
    # against real data (2026-10-23).
    probabilities: dict[str, float] = field(default_factory=dict)
    # Wall time the seam spent on this call, in ms. Carried so the caller can
    # record the user-facing cost of routing without re-measuring it: the
    # decision is on the request's critical path, so this IS added latency.
    # None = not measured (no call was made); the metrics helper refuses to
    # observe None so a non-event cannot drag the histogram mean down.
    latency_ms: int | None = None


@dataclass
class RouteDecision:
    """What routing decided for one request — header + ledger source.

    policy   the PROFILE's policy as looked up ('' = no profile lookup
             happened, 'off'/'shadow'/'active').
    engaged  master switch on AND the policy participates (shadow/active).
             False with a shadow/active policy ⇒ the header reads "off"
             (the profile opted in but the fleet switch is down).
    ceiling  the never-exceed model for this request.
    would    shadow: the router's pick (what active WOULD have served).
    served   active: the router's pick (the actual served model is decided
             downstream by the budget gates and rides the ledger `model`).
    reason   the deciding cut, e.g. 'prompt_tokens<tier1' / 'direct'.
    stamp    write the routing_* columns on this request's primary ledger
             row (pseudo-model requests under shadow/active only — direct
             requests never consulted the router, so there is nothing
             honest to stamp).
    sticky_finish deferred stickiness commit: finisher(actual_model).
    """

    policy: str = ""
    engaged: bool = False
    ceiling: str = ""
    would: str = ""
    served: str = ""
    reason: str = ""
    stamp: bool = False
    # Deferred stickiness commit (P3, audit 2026-10-29): when the caller's
    # sticky_write supports deferral it returns a finisher here instead of
    # persisting at pick time; the caller invokes it ONCE, with the model the
    # budget gates actually settled on, so the sticky row can never name a
    # model that never served. None = the write (if any) already happened
    # (legacy eager writers keep working unchanged).
    sticky_finish: Callable[[str], None] | None = None
    # option -> probability from the decision model, when one answered. Empty
    # when the heuristics served or the model abstained with no distribution.
    probabilities: dict[str, float] = field(default_factory=dict)


async def resolve_routed_model(
    key: Any,
    body: dict[str, Any],
    requested: str,
    registered: set[str],
    allowed_models: list[str],
    order: list[str],
    available: Callable[[str], bool] | None = None,
    decide: DecideFn | None = None,
    sticky_floor: str | None = None,
    sticky_write: Callable[[str], None] | None = None,
    route_default: bool = False,
    routable_models: list[str] | None = None,
    incumbent: str | None = None,
    switch_margin: float = 0.0,
) -> tuple[str, RouteDecision]:
    """Resolve one (pseudo-)model request to the model name to serve next.

    Returns (model_for_resolution, RouteDecision). The returned name is
    what the caller feeds into the NORMAL cascade resolution — routing
    replaces the NAME, never the machinery: budget gates, admission
    failover, and the mid-relay walk all behave exactly as they do for a
    client-pinned (or alias) request of that name.

    NEVER raises except RoutingRefused (a deliberate 4xx); every other
    failure degrades to legacy behavior (serve the request as it arrived)
    — an optional feature must not fail requests (fail-safe defaults).

    `order` is the profile's model_order as fetched by the caller
    (cascade.profile_order_for_key — reused, not re-queried). Empty order
    ⇒ the no-ladder rules: bare `routing` is a 404 with a pointed message,
    `routing__X` serves X with plain pinned semantics (nothing to
    downgrade without a ladder).

    `sticky_floor` / `sticky_write` are the conversation-stickiness seam
    (never-downgrade). BOTH are optional and default to None, so a caller
    that does not opt in keeps this function byte-identical to the
    pre-stickiness behaviour. The caller owns the STORAGE (this module stays
    I/O-free): `sticky_floor` is the head model this conversation was last
    served at, and `sticky_write` is invoked with the model actually chosen
    so the caller can persist it. A write is only attempted when a floor was
    supplied at all (the feature is on) and the request actually routed.
    """
    pseudo = detect_pseudo_model(requested)
    # route_default_model promotes the legacy 'auto' alias into a BARE routing
    # request: without this the flag only changed whether the caller fetched an
    # order, while 'auto' still fell into the direct branch below and returned
    # unchanged -- an inert flag that also emitted a false "active" route
    # header (caught by cross-validation 2026-10-23).
    if pseudo is None and is_routable_name(requested, route_default):
        pseudo = PseudoModel("bare")
    if pseudo is None:
        # Direct request: no routing math. The policy still rides along so
        # the response header can declare the profile's routing state
        # (spec: the header appears on every chat response of an opted-in
        # profile, not only on routing-named ones).
        policy, _ = await profile_routing_config(key.user_id)
        if policy in ("shadow", "active"):
            return requested, RouteDecision(
                policy=policy,
                engaged=bool(settings.routing_enabled),
                ceiling=requested,
                would=requested,
                served=requested,
                reason="direct",
            )
        return requested, RouteDecision()

    try:
        if not order:
            # No ladder to walk: bare `routing` cannot resolve (the caller
            # gets a clear 404 instead of the generic alias message), and
            # routing__X degenerates to serving X pinned — the normal
            # resolution path provides the pinned/forbidden/404 semantics.
            if pseudo.kind == "bare":
                raise RoutingRefused(404, "routing requires a profile model ordering")
            # The upstream must see the name that will actually serve: the
            # response `model` field is the upstream's echo, and a body still
            # saying "routing__X" would make the response report a name
            # nobody served (ledger truth convention).
            body["model"] = pseudo.target
            # XV-6 (cross-validation): an R4 floor-demotion here was
            # UNREACHABLE — routes_proxy refills `order` from
            # global_ladder() before calling this when the profile declares
            # none, so an empty `order` implies an empty global ladder and
            # the demotion could never fire. Deleted rather than kept as a
            # dead branch with a test proving nothing; the pinned serve below
            # IS the contract (pinned by test_unprofiled_named_serves_pinned
            # and test_xv6_empty_order_named_ceiling_serves_pinned_under_active).
            return pseudo.target, RouteDecision()

        policy, min_model = await profile_routing_config(key.user_id)
        if policy == "off" or not settings.routing_enabled:
            # 'off' — or a fleet switch-down on an opted-in profile — is
            # byte-for-byte today's behavior: BOTH pseudo names are plain
            # unregistered aliases that cascade through model_order. The
            # decision (policy, engaged) still rides out for the header.
            return requested, RouteDecision(policy=policy, engaged=False)

        # Routing engages (shadow or active, master on). routing__X carries
        # a hard boundary: allowed_models is an ACL, and a pseudo name must
        # not launder a model the key cannot use.
        if pseudo.kind == "named":
            target = pseudo.target
            if target not in registered:
                raise RoutingRefused(404, f"routing ceiling {target!r} is not a registered model")
            if allowed_models and target not in allowed_models:
                raise RoutingRefused(403, f"key is not allowed to use model {target!r} (routing ceiling)")

        # The ladder supplies CAPABILITY ORDER; the console allowlist supplies
        # ELIGIBILITY. Widening here is what makes a ticked model reachable even
        # when it is absent from model_order (defect fix, 2026-10-23: the
        # allowlist was console-only and bounded nothing on the request path).
        # available() is applied only to the ADDED models inside
        # expand_candidates -- the ladder keeps its own upstream walk semantics.
        cheap_first = expand_candidates(list(reversed(order)), routable_models, available)
        # R2 (audit 2026-10-29): BOTH ceiling forms go through the operator
        # allowlist. The named form already checked allowed_models above, but
        # the BARE form used to take order[0] raw — so bare `routing` under an
        # 'active' policy served the ladder head the operator UNTICKED in the
        # routing panel, and the shadow ledger trained on it. When the ladder
        # head is unticked, the ceiling is the most capable TICKED model; an
        # entirely stale allowlist falls back to the ladder head (a mis-ticked
        # panel must not 404 the request — the same graceful degradation
        # expand_candidates applies).
        if pseudo.kind == "named":
            ceiling = pseudo.target
        else:
            ceiling = order[0]
            if routable_models and ceiling not in {str(m) for m in routable_models}:
                for cand in order:
                    if cand in {str(m) for m in routable_models}:
                        ceiling = cand
                        break

        # R4 (audit 2026-10-29): a named ceiling the profile ladder cannot rank
        # used to skip routing_min_model entirely (pick_serving_model's
        # no_ladder branch pins to it). When BOTH the floor and the target sit
        # on the GLOBAL routing ladder, a target demonstrably BELOW the floor
        # is demoted to the floor: the floor is an operator spend control and
        # naming a model must not step under it. At or above the floor (or
        # unmeasurable — either name missing from the global ladder) keeps the
        # shipped pin-to-ceiling semantics; 'off' never reaches here.
        # The ASKED ceiling stays on RouteDecision.ceiling (the honest
        # never-exceed for this request); only the router's working window is
        # demoted.
        eff_ceiling = ceiling
        if (
            pseudo.kind == "named"
            and ceiling not in cheap_first
            and min_model
            # XV-1 (cross-validation): the demotion target must be LEGAL and
            # SERVABLE for THIS request before it is adopted. cheap_first is
            # expand_candidates' output — the operator's routing allowlist
            # already bounded it — and available() is the key ACL + health +
            # capacity closure. Skipping either check served an
            # allowlist-excluded floor (spend-control bypass) or 503'd a
            # pinned request the key was entitled to. An unusable floor keeps
            # the ASKED ceiling: the pinned semantics the client asked for.
            and min_model in cheap_first
            and (available is None or available(min_model))
            and (g_order := await global_ladder())
            and min_model in g_order
            and pseudo.target in g_order
            and g_order.index(pseudo.target) > g_order.index(min_model)
        ):
            eff_ceiling = min_model

        floor_usable = bool(min_model) and min_model in cheap_first
        if not floor_usable and key.user_id not in _floor_warned:
            # Floor unusable → pinned to the ceiling (no downgrade). One
            # WARNING per user per process: this is a profile config gap,
            # not a per-request event, and the operator needs to see it
            # exactly once (a mirror of the offpeak per-distinct-error
            # dedupe discipline — a tick every request must not spam).
            log.warning(
                "routing: profile for user %s has no usable routing_min_model (%r) —"
                " floor pinned to the ceiling; routing will not downgrade until one is set",
                key.user_id,
                min_model,
            )
            _floor_warned.add(key.user_id)
            if len(_floor_warned) > _FLOOR_WARNED_MAX:
                _floor_warned.clear()

        score = complexity_score(body)
        choice = pick_serving_model(cheap_first, eff_ceiling, min_model, score, available=available)
        if not choice.model:
            # XV-2 (cross-validation): the sentinel fires on TRANSIENT
            # unavailability too — `available` conflates policy (key ACL),
            # health (breaker) and budget (model_capacity). Escalating it to a
            # 503 preempted admission failover and the fallback chain, and
            # turned a budget 429 into a 503. Degrade to legacy in BOTH
            # policies: serve `requested` and let admission do its job — the
            # baseline outcome for the all-unavailable window, and strictly
            # better wherever the walk-down found a servable rung. Policy
            # exclusions are refused where they are KNOWN (the named-ceiling
            # ACL check above, a deliberate 4xx); the router itself never
            # fails a request over availability.
            # XV-5 (cross-validation): shadow must still HARVEST the decision —
            # a blank RouteDecision dropped the X-PCAI-Route header and the
            # ledger row precisely when an operator needs to see why nothing
            # was picked. would='' is honest ("no pick"); the header falls
            # back to the actually-served model.
            if policy == "shadow":
                return requested, RouteDecision(
                    policy="shadow",
                    engaged=True,
                    ceiling=eff_ceiling,
                    would="",
                    reason="nothing_available",
                    stamp=True,
                )
            return requested, RouteDecision()

        # --- decision-model seam (optional; heuristics stand by default) -----
        # The injected `decide` returns None when no decision model is
        # configured OR when it abstained (low confidence, timeout, garbage
        # payload) — in both cases `choice` above is already the correct
        # answer and nothing below changes. A decision that DOES land only ever
        # replaces the model NAME, inside the same ceiling/floor window the
        # heuristics use, so it can never escape the ladder or the key ACL.
        decision: DecisionOutcome | None = None
        probs: dict[str, float] = {}
        if decide is not None:
            # Candidates = the same allowed window the heuristic walks
            # (floor..ceiling, in cheap-first order). Offering the model
            # anything outside that window would let it pick a model the
            # profile/ACL never offered.
            candidates = _window(cheap_first, eff_ceiling, min_model)
            if available is not None:
                _avail = [m for m in candidates if available(m)]
                if not _avail:
                    # Every candidate in the window is unavailable right now.
                    # Offering them anyway invites a pick that admission then
                    # refuses -- a guaranteed 503 -- so skip the model and let
                    # the heuristic's ceiling walk serve this turn.
                    log.info("routing: no available candidate in window - decision skipped")
                candidates = _avail
            if len(candidates) >= 2:
                try:
                    decision = await decide(candidates, body, score)
                except Exception as exc:  # the seam is total; belt and braces
                    log.warning("routing: decision seam failed (%s) - heuristics stand", exc)
                    decision = None
                    _metrics.observe_decision_call("error")
            else:
                # Fewer than two candidates means there is nothing to choose
                # between: the window collapsed (e.g. the sticky floor reached
                # the ceiling). No call is made -- recording it keeps
                # `skipped` legible instead of silently shrinking the sample.
                _metrics.observe_decision_call("skipped")
            if decision is not None and decision.ok and decision.choice in candidates:
                # --- switch hysteresis ---------------------------------
                # A decision that LANDS is not automatically a switch: when
                # the conversation already has an incumbent, a challenger
                # must beat it by a real margin. Switching discards the
                # incumbent's KV cache and re-prefills the whole context --
                # measured at ~300x one decision call for a 76K agent
                # payload -- so paying that for a 0.62-vs-0.60 coin flip is
                # a net loss. The margin is drawn from the SAME probability
                # distribution the model already returns, so this needs no
                # new signal and no new call.
                #
                # Applies ONLY when an incumbent exists and the challenger
                # differs: a cold start (no incumbent) always takes the
                # model's pick, and agreeing with the incumbent is free.
                _held = False
                if incumbent and decision.choice != incumbent:
                    _p = decision.probabilities or {}
                    # Only trust the distribution when it is a real probability
                    # vector. The endpoint is expected to return 0..1, but an
                    # unvalidated scale (e.g. percentages) would make
                    # `_ch - _in` large against a 0..1 margin and silently
                    # DISABLE hysteresis entirely. A truncated/partial dict is
                    # equally untrustworthy: a missing incumbent key looked
                    # identical to "the model considered and rejected it".
                    _usable = bool(_p) and all(
                        isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0 for v in _p.values()
                    )
                    # Default the comparison operands so the log line below is
                    # always well-formed (an unreadable distribution never sets
                    # them).
                    _ch = float(decision.confidence)
                    _in = 0.0
                    if _usable and decision.choice in _p and incumbent in _p:
                        _ch, _in = float(_p[decision.choice]), float(_p[incumbent])
                        _margin_ok = (_ch - _in) >= switch_margin
                    else:
                        # Unreadable distribution (wrong scale, partial, or the
                        # incumbent missing). The margin cannot be computed in
                        # comparable units, so HOLD: an unreadable vector must
                        # never authorise a switch it cannot justify. Falling
                        # back to `confidence - 0.0` was wrong -- the winner's
                        # confidence alone clears a 0.10 margin almost always,
                        # which silently disabled hysteresis in exactly the
                        # case it was meant to guard.
                        _margin_ok = False
                    if not _margin_ok:
                        _held = True
                        log.info(
                            "routing: switch held by hysteresis (incumbent=%s %.3f vs %s %.3f,"
                            " margin %.2f) - staying on the incumbent",
                            incumbent,
                            _in,
                            decision.choice,
                            _ch,
                            switch_margin,
                        )
                if not _held:
                    choice = RoutingChoice(decision.choice, f"decision:{decision.confidence:.2f}")
                elif incumbent in candidates:
                    # A HELD switch must actually stay on the incumbent. Leaving
                    # `choice` as the heuristic pick made the hold a lie: with the
                    # never-downgrade floor permitting escalation, an incumbent
                    # GLM + heuristic DS + held Qwen challenger served DS -- the
                    # switch the margin exists to prevent -- while logging
                    # "staying on the incumbent". Guarded by `in candidates` so an
                    # unvalidated incumbent (a raw sticky row) can never escape the
                    # window; the never-downgrade floor below re-checks it anyway.
                    choice = RoutingChoice(incumbent, "hysteresis:held")
                # else: the incumbent is outside this request's candidate window
                # (ACL/health/ceiling change), so there is nothing to stay on and
                # the heuristic pick stands -- the honest fallback.
                probs = dict(decision.probabilities or {})
                # Observability. Two honesty rules the dual review (2026-10-26)
                # forced, both about not reporting a decision that did not happen:
                #   * `held` must mean the hold was APPLIED. When the incumbent is
                #     outside the candidate window there is nothing to stay on and
                #     the heuristic pick stands, so a switch can still occur --
                #     ticking `held` there reported the margin blocking a switch
                #     that actually happened.
                #   * `served` must mean the model's choice is what SERVES. This
                #     point is upstream of the never-downgrade floor, so a floor
                #     override could still replace the pick; the caller re-labels
                #     it from the final reason (see DECISION_CALLS below).
                _hold_applied = _held and incumbent in candidates
                if _hold_applied:
                    _metrics.observe_route_switch("held")
                elif _held:
                    # Margin fired but could not be applied: the incumbent is not
                    # servable this turn, so this IS a switch.
                    _metrics.observe_route_switch("switched")
                elif incumbent is None:
                    _metrics.observe_route_switch("cold")
                elif decision.choice == incumbent:
                    _metrics.observe_route_switch("same")
                elif policy == "shadow":
                    # Shadow never serves the pick, so nothing is re-prefilled;
                    # counting it as `switched` inflates the go/no-go number an
                    # operator uses to decide whether to arm `active`.
                    _metrics.observe_route_switch("shadow_would")
                else:
                    _metrics.observe_route_switch("switched")
                _metrics.observe_decision_call("served" if not _hold_applied else "held", decision.latency_ms)
            elif decision is not None:
                # The call RETURNED but was not served. Split the two very
                # different causes: an endpoint that timed out or failed is an
                # OUTAGE, not a confidence problem, and conflating them is what
                # makes an operator mis-tune the floor during an outage.
                # decide() never raises (it maps transport/timeout to ok=False),
                # so THIS branch -- not the except above -- is where real
                # failures land.
                probs = dict(decision.probabilities or {})
                if decision.reason in ("timeout", "call_failed"):
                    _metrics.observe_decision_call("error", decision.latency_ms)
                else:
                    _metrics.observe_decision_call("abstained", decision.latency_ms)

        # --- never-downgrade floor (optional; sticky_floor is None when off) --
        # Applied AFTER the decision-model seam so the floor constrains BOTH
        # the heuristic and the model's pick: a decision model that tries to
        # move a conversation down the ladder is held at the floor exactly
        # like the heuristic is. `candidates` is the cheap-first window built
        # above; when no decision ran (fewer than 2 candidates, or no decision
        # model) it is rebuilt here so the floor still has a capability order
        # to measure against. Both are pure list operations -- no I/O.
        if sticky_floor:
            floor_candidates = candidates if decide is not None else _window(cheap_first, eff_ceiling, min_model)
            # P3 (audit 2026-10-29): the floor was measured in an
            # AVAILABILITY-BLIND window, so a conversation could be pinned to
            # a model that is cooling right now — the opposite of what
            # cascade._reorder_head does for the pinned path (which steps the
            # head to a servable model). Filter the SAME window by the
            # caller's availability predicate: an unavailable sticky head
            # drops OUT of the window, apply_never_downgrade then ignores it
            # (its documented contract: "a stale preference must never
            # resurrect a model the key may no longer use") and the choice
            # stands. Fail-safe: when availability filtering would EMPTY the
            # window, keep the unfiltered one — an all-unavailable fleet is a
            # capacity problem for admission to report, not for the floor to
            # fake.
            if available is not None and floor_candidates:
                _servable = [m for m in floor_candidates if available(m)]
                if _servable:
                    floor_candidates = _servable
            choice = apply_never_downgrade(choice, floor_candidates, sticky_floor)

        if policy == "shadow":
            # Serve EXACTLY as 'off' would (the pseudo name rides the alias
            # path downstream); only the decision is harvested.
            return requested, RouteDecision(
                policy="shadow",
                engaged=True,
                ceiling=ceiling,
                would=choice.model,
                reason=choice.reason,
                stamp=True,
                probabilities=probs,
            )
        # The upstream must see the model that will actually serve: a pinned
        # request forwards its own model name, and the response `model` field
        # is the upstream's echo — leaving "routing" in the body would make
        # the response report a name nobody served (ledger truth convention).
        body["model"] = choice.model
        # Persist the capability floor for this conversation (never-downgrade).
        # ONLY here, in the branch that actually SERVES a routed model: shadow
        # harvests a decision without serving it, so writing a floor there
        # would constrain a later request against a model that never served.
        # Best-effort and swallowed -- the caller's writer is fail-open by
        # contract, and a persistence fault must cost stickiness, never a
        # request.
        # Gate on the WRITER (feature on), NOT on an existing floor: a fresh
        # conversation has no floor yet, and requiring one here meant the row
        # was never written, so the next turn again had no floor -- the feature
        # could never bootstrap (caught by cross-validation 2026-10-23).
        # P3 (audit 2026-10-29): the floor was persisted HERE, at pick time —
        # before admission/breaker promotion could move the request, so the
        # sticky row could name a model that never actually served and the
        # next turn's never-downgrade floor held the conversation there.
        # The caller knows the real serving model only after the gates run,
        # so the actual write is RE-DIRECTED through the deferred writer
        # seam the caller supplies: sticky_write(model) returns either None
        # (legacy eager writer — write now) or a zero-arg finisher that the
        # caller invokes once admission settles. The ledger truth
        # convention applies to stickiness too: record what billed.
        _finish = None
        if sticky_write is not None:
            try:
                _finish = sticky_write(choice.model)
            except Exception as exc:  # pragma: no cover - writer is fail-open
                log.warning("routing: sticky write failed (%s) - floor not persisted", type(exc).__name__)
        active_decision = RouteDecision(
            policy="active",
            engaged=True,
            ceiling=ceiling,
            served=choice.model,
            reason=choice.reason,
            stamp=True,
            probabilities=probs,
        )
        if _finish is not None:
            active_decision.sticky_finish = _finish
        return choice.model, active_decision
    except RoutingRefused:
        raise
    except Exception as exc:
        # Any doubt → legacy behavior. For `routing` the alias path serves
        # the ceiling (model_order[0]) first anyway, so a router bug costs
        # a downgrade opportunity — never an answer quality regression.
        log.warning("routing: decision failed (legacy behavior): %s", exc)
        return requested, RouteDecision()


class RoutingLedgerStamp(TypedDict, total=False):
    """The routing_* kwargs ledger_stamp() hands to settle().

    total=False is load-bearing TWICE: an empty dict stays a valid stamp
    ("routing never engaged" — spreading it adds NO kwargs, so settle keeps
    the columns' '' defaults instead of faux stamps), and a TypedDict (not
    dict[str, str]) lets mypy prove the **spread into settle() cannot
    supply any of its positional/int-typed parameters — the plain-dict
    spread was flagged arg-type at both routes_proxy call sites.
    """

    routing_policy: str
    routing_ceiling: str
    routing_served: str
    routing_reason: str
    routing_probabilities: str


def ledger_stamp(route: RouteDecision | None, actual_model: str) -> RoutingLedgerStamp:
    """The routing_* kwargs for settle() on this request's primary row.

    Empty dict = stamp nothing (no routing in effect, or a direct request
    that never consulted the router). 'shadow' stamps would-serve while
    the `model` column keeps the actually-served (ceiling-side) model;
    'active' stamps the ACTUAL model (the budget gates may have promoted
    the request elsewhere after the router picked — the ledger records
    what really billed, never the router's intention).
    """
    if route is None or not route.stamp:
        return {}
    served = route.would if route.policy == "shadow" else (actual_model or route.served)
    return {
        "routing_policy": route.policy,
        "routing_ceiling": route.ceiling,
        "routing_served": served,
        "routing_reason": route.reason,
        # Sorted so the same distribution always serializes identically (the
        # ledger is compared across rows when tuning the floor). Empty when no
        # decision model answered -- the column default, not a faux stamp.
        "routing_probabilities": (
            json.dumps(dict(sorted(route.probabilities.items())), separators=(",", ":")) if route.probabilities else ""
        ),
    }


def route_header(route: RouteDecision | None, actual_model: str = "") -> str | None:
    """The X-PCAI-Route value for one chat response (None = no header).

    Shapes (precedent: X-PCAI-Billing — the response announces the billing/
    routing state in effect for THIS response):
      shadow ceiling=<c> would=<w> reason=<r>
      active ceiling=<c> served=<s> reason=<r>
      off                                  (opted-in profile, master switch off)
    `actual_model` (the post-gate serving model) wins over the router's
    pick in the active form: the header must not claim a model the budget
    gates then overruled.
    """
    if route is None or route.policy not in ("shadow", "active"):
        return None
    if not route.engaged:
        return "off"
    if route.policy == "shadow":
        return f"shadow ceiling={route.ceiling} would={route.would or actual_model} reason={route.reason}"
    return f"active ceiling={route.ceiling} served={actual_model or route.served} reason={route.reason}"
