"""Model groups — named sets of EQUIVALENT chat models, picked by live load.

WHY: one deployment can serve the same capability from several models
(Deepseek / GLM / Qwen) and several replicas of each. Asking for "any of
them" by hand means the client picks a fixed order, so one model absorbs the
whole conversation load while its siblings idle. A GROUP is a first-class
route target: the request names the group, the gateway picks a MEMBER by live
load, and the conversation is pinned to that member (core.stickiness
``grp:`` scope) so the next turn re-uses it instead of re-rolling.

Contract:

* Members are EQUIVALENT — no priority, no order, no weights in v1. A group
  is the load-balanced SIBLING of the ordered fallback chain
  (core.model_fallbacks): fallback is a FAILURE path, a group is a
  SELECTION path.
* One level only: a member may not itself be a group, so a cycle can never
  be stored (``validate_members`` is the write-path gate).
* NOT a policy boundary: the SELECTED member bills and its
  ``allowed_models`` gate applies. A group never makes a model reachable
  that the key could not reach on its own, and it never bypasses budgets.
* This module owns the DEFINITION plus the two PURE decisions (validate,
  pick). Live load lives in core.load_snapshot, the pin in core.stickiness.
* The registry view is a short-TTL cache of the ``model_groups`` table: a DB
  fault serves the last good snapshot and NEVER fails a request — the same
  fail-open contract every other routing read in this package follows.
"""

from __future__ import annotations

import json
import logging
import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..db import db

log = logging.getLogger(__name__)

# Cache TTL. Groups are admin-edited (rare) and read on every request that
# names one, so a 10s view removes the per-request SELECT while still making
# an edit visible in about one refresh cycle. The admin write path calls
# invalidate() anyway, so the TTL only bounds OTHER replicas' lag.
_TTL_S = 10.0

# Hard cap on a group's width. The pick is power-of-two (two draws), so a
# wide group costs the same per request — the cap is about the admin surface
# and the fallback chain's attempt bound, not the pick.
MAX_MEMBERS = 32

POLICY_LOAD = "load"

# None = never loaded (an EMPTY dict is a valid loaded state: no groups).
_CACHE: dict[str, ModelGroup] | None = None
# Monotonic stamp of the last load ATTEMPT (success or fault): a fault is
# cached for the TTL too, so a database outage cannot turn every request into
# a retry storm — the last good snapshot (or {}) is served instead.
_LOADED_AT: float | None = None
# Generation counter, bumped on every invalidate() (audit 2026-11-10): a
# load() that STARTED before an invalidate() must not publish rows it fetched
# PRE-commit back into the cache — that repopulated the old snapshot for a
# full TTL right after an admin edit. load() captures the generation before
# its fetch and only publishes a snapshot fetched under the SAME generation.
_GEN: int = 0


@dataclass(frozen=True)
class ModelGroup:
    """One named set of equivalent models. Frozen: a snapshot is read-only."""

    name: str
    model_type: str
    policy: str
    members: tuple[str, ...]
    description: str = ""


def _member_names(raw: Any) -> tuple[str, ...]:
    """Member model names from the stored JSONB. PURE, total.

    The column is ``[{model, weight}]`` (schema.py). asyncpg hands JSONB back
    as a STRING unless a codec is registered (none is, in db/__init__.py), so
    both the decoded list and the raw text are accepted — and a plain string
    list is accepted too, because a hand-edited row or an older shape must
    not take the group offline.
    """
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return ()
    if not isinstance(data, (list, tuple)):
        return ()
    out: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model = item.get("model", item.get("name"))
        else:
            model = item
        if not isinstance(model, str):
            continue
        name = model.strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _parse(rows: Any) -> dict[str, ModelGroup]:
    """ModelGroup map from ``model_groups`` rows. PURE, total.

    A row with no usable members is DROPPED (and logged): a group that can
    never select anything must not advertise itself in /v1/models.
    """
    groups: dict[str, ModelGroup] = {}
    for row in rows or []:
        try:
            name = str(row["name"] or "").strip()
        except (KeyError, TypeError, IndexError):
            continue
        if not name:
            continue
        try:
            raw_members = row["members"]
        except (KeyError, TypeError, IndexError):
            raw_members = None
        members = _member_names(raw_members)
        if not members:
            log.warning("model groups: group %r has no usable members — ignored", name)
            continue
        groups[name] = ModelGroup(
            name=name,
            model_type=str(row["model_type"] or "chat"),
            policy=str(row["policy"] or POLICY_LOAD),
            members=members,
            description=str(row["description"] or ""),
        )
    return groups


def _is_fresh() -> bool:
    if _LOADED_AT is None:
        return False
    return (time.monotonic() - _LOADED_AT) < _TTL_S


async def load(force: bool = False) -> dict[str, ModelGroup]:
    """The current group map, from the 10s cache or the database.

    NEVER raises: a DB fault logs and serves the LAST GOOD snapshot ({} when
    there has never been one). The fault is stamped like a success so the
    cache does not become a retry storm during an outage — group routing
    degrades to "no groups" (the caller falls through to normal handling),
    which is strictly better than a request failure.
    """
    global _CACHE, _LOADED_AT
    if not force and _is_fresh():
        return snapshot()
    gen = _GEN
    try:
        rows = await db.fetch("SELECT name, model_type, policy, members, description FROM model_groups ORDER BY name")
    except Exception:
        if _GEN == gen:
            # Only stamp the fault when no invalidate() superseded this
            # attempt — a stale stamp would delay the post-edit reload too.
            _LOADED_AT = time.monotonic()
        log.warning("model groups: load failed — serving the last good snapshot", exc_info=True)
        return snapshot()
    try:
        parsed = _parse(rows)
    except Exception:
        if _GEN == gen:
            _LOADED_AT = time.monotonic()
        log.warning("model groups: unparseable rows — serving the last good snapshot", exc_info=True)
        return snapshot()
    if _GEN != gen:
        # An invalidate() ran while this fetch was in flight: these rows are
        # PRE-commit. Do not publish them — snapshot() is now empty and the
        # next load() re-fetches the committed state.
        return snapshot()
    _CACHE = parsed
    _LOADED_AT = time.monotonic()
    return snapshot()


def snapshot() -> dict[str, ModelGroup]:
    """The last loaded group map (sync; {} before the first successful load).

    A shallow COPY: callers are on the request path and must not be able to
    mutate the cache by accident (the values are frozen dataclasses).
    """
    if _CACHE is None:
        return {}
    return dict(_CACHE)


def is_group(name: str) -> bool:
    """True when ``name`` is a configured group. PURE."""
    if not name or _CACHE is None:
        return False
    return name in _CACHE


def group_for(name: str) -> ModelGroup | None:
    """The group definition, or None for a plain model. PURE."""
    if not name or _CACHE is None:
        return None
    return _CACHE.get(name)


def member_models(name: str) -> tuple[str, ...]:
    """A group's member models in stored order (() for a plain model). PURE."""
    group = group_for(name)
    return group.members if group is not None else ()


async def invalidate() -> None:
    """Drop the cache so the next load() reads the table.

    Deliberately does NOT reload: the admin write path calls this right after
    its INSERT/DELETE, and an eager reload inside the same transaction would
    cache the PRE-commit row for a full TTL. Dropping is always correct; the
    next request pays one SELECT. The generation bump also neutralises a
    load() already in flight (it fetched pre-commit rows and must not publish
    them — audit 2026-11-10).
    """
    global _CACHE, _LOADED_AT, _GEN
    _GEN += 1
    _CACHE = None
    _LOADED_AT = None


def validate_members(
    members: list[str],
    all_models: set[str],
    groups: dict[str, ModelGroup],
) -> str | None:
    """Why these members are invalid, or None when they are fine. PURE.

    The caller owns the model TYPE check (a chat group must not carry an
    embedding member): this module only knows names, not the registry's type
    map. Everything else that can corrupt the stored definition is checked
    here, so no write path can persist a group that the pick cannot serve.
    """
    cleaned: list[str] = []
    for raw in members:
        name = str(raw or "").strip()
        if name:
            cleaned.append(name)
    if not cleaned:
        return "a group needs at least one member"
    if len(cleaned) > MAX_MEMBERS:
        return f"too many members ({len(cleaned)} > {MAX_MEMBERS})"
    seen: set[str] = set()
    for name in cleaned:
        if name in seen:
            return f"duplicate member: {name}"
        seen.add(name)
        if name in groups:
            return f"member {name!r} is a group (groups are one level)"
        if name not in all_models:
            return f"unknown model: {name}"
    return None


def _score_of(score: Callable[[str], float | None], member: str) -> float | None:
    """One member's score, with a broken/absent collector reading as UNKNOWN."""
    try:
        value = score(member)
    except Exception:
        log.debug("model groups: score(%r) raised — treated as unknown", member, exc_info=True)
        return None
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def pick_member(
    group: ModelGroup,
    allowed: set[str] | None,
    score: Callable[[str], float | None],
) -> str | None:
    """Power-of-two choice of one member. PURE apart from the RNG.

    ``allowed`` is the key's ACL (None = unrestricted): a group can never
    widen what the key may reach, so the filter runs BEFORE any draw.

    Two DISTINCT eligible members are sampled and the lower score wins. An
    UNKNOWN score (no metrics for that model) is a uniform draw over the
    OBSERVED range [min(known), max(known)] — a NEUTRAL rank, not a sentinel:
    the metrics-less member reads as "average load", so it neither beats a
    busy member on every draw (the old [0, max] range did, letting one
    unmonitored model soak the group's traffic) nor loses to an idle one on
    every draw. A single observed value makes the draw that value (a tie).
    With NO observed scores there is no range to be neutral about, so the
    draw is uniform over [0, 1] — the pick is then an even spread. A tie
    keeps the FIRST sampled member, so a healthy fleet with equal load still
    spreads deterministically instead of flapping.
    """
    eligible = [m for m in group.members if allowed is None or m in allowed]
    if not eligible:
        return None
    if len(eligible) == 1:
        return eligible[0]

    values: dict[str, float | None] = {m: _score_of(score, m) for m in eligible}
    known = [v for v in values.values() if v is not None]
    # NEUTRAL RANK over the OBSERVED range, not [0, max]: an unmonitored
    # member must read as "average load". Drawing below every observed score
    # made it beat a busy member essentially always, so one metrics-less
    # model soaked the group's traffic. A single observed value makes the
    # draw that value (a tie); with nothing observed there is no range to be
    # neutral about, so the draw is uniform over [0, 1].
    low, high = (min(known), max(known)) if known else (0.0, 1.0)

    def _draw(member: str) -> float:
        value = values[member]
        if value is not None:
            return value
        return random.uniform(low, high)

    first, second = random.sample(eligible, 2)
    return second if _draw(second) < _draw(first) else first
