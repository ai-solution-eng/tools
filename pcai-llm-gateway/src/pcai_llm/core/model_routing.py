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
from dataclasses import dataclass
from typing import Any, TypedDict

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

PSEUDO_ROUTING = "routing"
PSEUDO_PREFIX = "routing__"

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


def complexity_score(body: dict[str, Any]) -> int:
    """Token-denominated complexity score of one chat request. Pure.

    Signals (deliberately cheap — no model calls, no tokenizers):
    * prompt estimate: chars/4 over every message's content — the same
      len//4 heuristic the gates use, so the router and the budget math
      never disagree about what a "token" is. The system prompt is just a
      message and rides along: in RAG/agent traffic it is usually the
      largest single block.
    * tool definitions: serialized JSON bytes // routing_tool_bytes_per_token
      (dense schema JSON tokenizes far worse than prose; the byte term also
      scales with the NUMBER of tools).
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

    tool_tokens = 0
    tools_raw = body.get("tools")
    if tools_raw:
        try:
            tool_bytes = len(json.dumps(tools_raw, default=str).encode("utf-8"))
        except (TypeError, ValueError):  # exotic payloads: approximate, never raise
            tool_bytes = len(str(tools_raw).encode("utf-8", "replace"))
        tool_tokens = tool_bytes // max(int(settings.routing_tool_bytes_per_token), 1)

    turn_tokens = max(len(messages) - 1, 0) * max(int(settings.routing_turn_adder_tokens), 0)

    mt_tokens = 0
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        mt_tokens = min(max_tokens, _MAX_TOKENS_CAP) // _MAX_TOKENS_DIVISOR

    return prompt_est + tool_tokens + turn_tokens + mt_tokens


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
    gets the cheapest AVAILABLE model that still fits; if none does, the
    ceiling (the fail-safe answer).
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
            chosen = ceiling  # nothing in the window can serve → ceiling
    return RoutingChoice(chosen, reason)


async def profile_routing_config(user_id: uuid.UUID) -> tuple[str, str]:
    """(routing_policy, routing_min_model) for the key's profile.

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
    _policy_cache[user_id] = (now, policy, min_model)
    if len(_policy_cache) > _POLICY_CACHE_MAX:
        _policy_cache.clear()
    return policy, min_model


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
    """

    policy: str = ""
    engaged: bool = False
    ceiling: str = ""
    would: str = ""
    served: str = ""
    reason: str = ""
    stamp: bool = False


async def resolve_routed_model(
    key: Any,
    body: dict[str, Any],
    requested: str,
    registered: set[str],
    allowed_models: list[str],
    order: list[str],
    available: Callable[[str], bool] | None = None,
    decide: DecideFn | None = None,
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
    """
    pseudo = detect_pseudo_model(requested)
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

        cheap_first = list(reversed(order))
        ceiling = pseudo.target if pseudo.kind == "named" else order[0]

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
        choice = pick_serving_model(cheap_first, ceiling, min_model, score, available=available)

        # --- decision-model seam (optional; heuristics stand by default) -----
        # The injected `decide` returns None when no decision model is
        # configured OR when it abstained (low confidence, timeout, garbage
        # payload) — in both cases `choice` above is already the correct
        # answer and nothing below changes. A decision that DOES land only ever
        # replaces the model NAME, inside the same ceiling/floor window the
        # heuristics use, so it can never escape the ladder or the key ACL.
        decision: DecisionOutcome | None = None
        if decide is not None:
            # Candidates = the same allowed window the heuristic walks
            # (floor..ceiling, in cheap-first order). Offering the model
            # anything outside that window would let it pick a model the
            # profile/ACL never offered.
            floor_pos = cheap_first.index(min_model) if min_model and min_model in cheap_first else 0
            ceil_pos = cheap_first.index(ceiling) if ceiling in cheap_first else len(cheap_first) - 1
            candidates = cheap_first[min(floor_pos, ceil_pos) : ceil_pos + 1]
            if available is not None:
                candidates = [m for m in candidates if available(m)] or candidates
            if len(candidates) >= 2:
                try:
                    decision = await decide(candidates, body, score)
                except Exception as exc:  # the seam is total; belt and braces
                    log.warning("routing: decision seam failed (%s) - heuristics stand", exc)
                    decision = None
                if decision is not None and decision.ok and decision.choice in candidates:
                    choice = RoutingChoice(decision.choice, f"decision:{decision.confidence:.2f}")

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
            )
        # The upstream must see the model that will actually serve: a pinned
        # request forwards its own model name, and the response `model` field
        # is the upstream's echo — leaving "routing" in the body would make
        # the response report a name nobody served (ledger truth convention).
        body["model"] = choice.model
        return choice.model, RouteDecision(
            policy="active",
            engaged=True,
            ceiling=ceiling,
            served=choice.model,
            reason=choice.reason,
            stamp=True,
        )
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
