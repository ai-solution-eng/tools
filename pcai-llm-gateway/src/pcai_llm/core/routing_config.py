"""Model-routing runtime configuration: DB-backed, admin-editable.

The env/Settings knobs (config.py) are the DEPLOYMENT defaults; this module
is the OPERATOR override layer the Routing panel writes through, following
the distiller_config pattern exactly: rows, TTL-cached, loaded
degrade-not-fail (a DB blip keeps the last known policy, never resets the
router to defaults mid-flight).

WHY this layer exists at all: core.model_routing v1 is a pure heuristic
function of the request body (see its docstring). The next step is a
DECISION model (vllm-sr / d3-turbo) choosing instead of the heuristics, and
that needs three things the env could not carry: a bounded set of models the
router is allowed to ROUTE TO (spend control), the PROMPT the decision model
receives, and a way to feed live GPU demand into that prompt so the decision
model sees the same signal the off-peak discount bills by. This release
stores the config and renders the prompt; it deliberately does NOT call the
decision model — the config, the allowed set and the rendered prompt can be
reviewed against live shadow-ledger traffic BEFORE a model is allowed to
spend.

Configured here:
  * enabled          - runtime on/off AND-ed with settings.routing_enabled
                       (the env flag still owns the mount; this owns whether
                       routing ACTS).
  * decision_model   - the model that would make the decision ('' =
                       heuristics only — the shipped behavior).
  * allowed_models   - the models the router may route TO; [] = every
                       registered chat model. Deliberately NOT a key ACL: a
                       key's allowed_models stays a hard per-key boundary
                       that no routing decision can widen.
  * prompt           - the decision-model system prompt, template-aware
                       (see render_prompt).
  * inject_gpu_load  - append live GPU load to the rendered prompt.
  * gpu_load_source  - where that load comes from; 'offpeak' is the only
                       source today (the billing evaluator's own samples —
                       one signal, so the prompt and the price can never
                       disagree about how busy the fleet is).
  * max_prompt_chars - hard cap on the RENDERED prompt, so a large GPU
                       snapshot cannot blow the decision model's context.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

_CACHE_TTL_S = 60.0
_cache: dict[str, Any] = {}
_cache_at = 0.0


DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "decision_model": "",
    "allowed_models": [],
    "prompt": "",
    # --- GLOBAL routing arming + ladder (2026-10-28) ----------------------
    # WHY these live here and not only on the profile: routing was armed
    # per-profile (profiles.routing_policy) and its ladder was borrowed from
    # profiles.model_order, which is a CASCADE ordering. Two consequences the
    # operator hit in production:
    #   * the routing panel read decision_mode=active while the profile was
    #     still shadow, so nothing was ever served and the UI gave no hint;
    #   * a profile with an empty model_order made bare `routing` 404
    #     ("routing requires a profile model ordering"), so the feature
    #     silently did not exist for that profile.
    # The GLOBAL value is the fleet default; a profile that explicitly arms
    # itself (shadow/active) still wins, so a site can opt one profile in
    # early. One-way by construction: a profile left at "off" INHERITS, so it
    # cannot force routing off while the fleet default is armed -- if that is
    # ever needed, make profiles.routing_policy nullable and treat NULL as
    # inherit rather than overloading "off".
    "routing_policy": "off",
    # The GLOBAL capability ladder (MOST-CAPABLE FIRST -- the same convention
    # as profiles.model_order, because the router takes order[0] as the
    # CEILING and builds cheap_first as reversed(order). Getting this
    # backwards makes the WEAKEST model the ceiling), used by the router when
    # the calling profile declares no model_order. Empty = no ladder = bare
    # `routing` still 404s, exactly as before.
    "model_order": [],
    # The GLOBAL routing floor: the weakest model the router may serve when a
    # calling profile declares no routing_min_model of its own (R3, audit
    # 2026-10-29: without this knob a fleet-armed heuristic router was pinned
    # to the ceiling and could NEVER downgrade — the feature the panel just
    # enabled was inert for every floorless profile). Most-capable-FIRST name,
    # matching the ladder convention; empty = no global floor (the shipped
    # "no floor -> no downgrade" behaviour, unchanged).
    "routing_min_model": "",
    # ON by default: the shipped prompt has a "## Current GPU load" section that
    # asks the model to weigh load, and with injection off that section renders
    # as the literal string "(gpu load injection is off)" -- the prompt and the
    # text actually sent disagreed on every deployment that never flipped this.
    # Turning it on is the automatic behaviour; an operator who wants the model
    # to ignore load sets it false and the section says so explicitly.
    "inject_gpu_load": True,
    "gpu_load_source": "offpeak",
    "max_prompt_chars": 8000,
    # --- decision-model execution (the LLM-as-router seam) -----------------
    # decision_mode gates whether a CONFIGURED decision model is actually
    # consulted. "off" keeps the shipped heuristics exactly as they are even
    # when decision_model is set (the safe resting state: a model can be
    # registered and pointed at without changing behaviour). "shadow" asks the
    # model and records what it WOULD have served while the heuristics still
    # serve -- zero added latency risk and the labelled data needed to trust it.
    # "active" serves the model choice when it clears min_confidence.
    "decision_mode": "off",
    # Below this the decision model is treated as ABSTAINING and the
    # heuristics serve. 0.60 matches the measured distribution (a clear 2-way
    # split scored 0.9997; an ambiguous 3-way model choice scored 0.43).
    "decision_min_confidence": 0.60,
    # Hysteresis margin for a model SWITCH, in probability points. A challenger
    # must beat the conversation's current model by this much before the router
    # pays the re-prefill. 0 blocks nothing (first-wins); 1 blocks every switch.
    # 0.10 stops a 0.62-vs-0.60 coin flip while leaving a decisive 0.85-vs-0.15
    # free to switch.
    "decision_switch_margin": 0.10,
    # Bounded so a hung decision endpoint cannot park a chat request. The
    # deployed d3-turbo answers a small state in ~790ms.
    "decision_timeout_ms": 2500,
    # --- conversation stickiness (never-downgrade) -------------------------
    # When on, a routed conversation remembers the capability position it was
    # served at and may never move DOWN the ladder mid-conversation. It may
    # still move UP (a "hi" that turns into a hard refactor must be able to
    # escalate), and the stored head is re-validated every turn against the
    # live candidate window / registry / ACL.
    #
    # WHY not a plain lock ("pick once, stay"): with capability routing a
    # conversation that opens with "hi" lands on the cheapest model, and a
    # literal lock would pin a subsequent hard refactor to it -- a quality
    # REGRESSION dressed as a routing improvement. Never-downgrade gets the
    # stability a lock is for (no thrash between turns) without the trap.
    # Default OFF: it changes which model serves, so it is opt-in like every
    # other behaviour-changing knob here.
    "sticky_routing": False,
    # Idle reset: a conversation untouched for this long forgets its floor and
    # re-decides from scratch. Without it a long thread monotonically climbs to
    # the ceiling (every escalation ratchets the floor up and nothing ever
    # brings it back down). 0 = never reset (climb forever).
    "sticky_routing_idle_reset_s": 1800,
    # --- default-model opt-in ---------------------------------------------
    # Whether a request that names NO model (or names the plain auto alias)
    # is offered to the router instead of going straight to the cascade.
    # Default OFF: auto keeps its documented meaning -- the legacy
    # profile-order budget cascade -- so arming routing never silently
    # changes what an existing client request does. This is the "flag" that
    # promotes the default flow to routing; the explicit routing /
    # routing__X names are unaffected by it and always route.
    "route_default_model": False,
    # --- candidate capabilities ------------------------------------------
    # {model: ["text","image",...]} — what each ROUTABLE model can actually
    # accept. Chat endpoints do NOT advertise modality (verified 2026-10-26:
    # deepseek/glm/qwen /v1/models all return only
    # id/object/created/owned_by/root/parent/max_model_len), so this cannot be
    # discovered — the operator declares it. Empty = unknown: the router then
    # makes no capability claims and never excludes a model on modality.
    # When set, the decision model is TOLD which candidates accept images, so
    # a VLM request is not routed to a text-only LLM.
    "model_modalities": {},
    # --- per-model cards (the criteria channel) ---------------------------
    # {model: {"description": "<what this model is for>"}} — one card per
    # ROUTABLE model, rendered into the decision model's CRITERIA at call
    # time. This is where a model's ROLE belongs: criteria is the only channel
    # whose text is bound to an option label when the choice is scored, so the
    # decision model reads "Deepseek: the default for agent work..." attached
    # to the option it describes instead of cross-referencing a role list in
    # the instructions against bare labels (the pre-2026-10-30 shape, which
    # kept every role in one prose block keyed to hardcoded names).
    #
    # Deliberately DATA, not prose in the prompt: the candidate set is
    # per-request (profile ladder, ACL, health, context fit), so a prompt that
    # names three models is wrong on every site whose ladder differs. An empty
    # map is valid — every option then renders as its bare name, which is the
    # pre-existing behaviour — but the panel warns, because a decision model
    # given labels with no descriptions has no basis to discriminate.
    #
    # Descriptions may carry live tokens ({{gpu_util}}, {{low_demand}},
    # {{cost}}, {{modalities}}, {{model}}, {{gpu_line}}); see
    # CRITERIA_PLACEHOLDERS. Tokens are resolved per-model, at call time.
    "model_cards": {},
}

# The ONLY placeholder names render_prompt substitutes. Anything else in
# {{...}} is left VERBATIM (a typo must be visible in the preview, not
# silently eaten) — the console renders this list as the placeholder legend,
# so the legend and the renderer cannot drift.
PLACEHOLDERS = ("models", "model_list", "gpu_load", "gpu_load_json", "decision_model")

# The placeholders a per-model CARD description may use. Separate namespace
# from PLACEHOLDERS on purpose: instruction placeholders describe the SET
# (the model list, the whole load table), card placeholders describe ONE model
# and are resolved against that model's own live row. Keeping them apart is
# what lets a card say "{{gpu_util}} under load" and get its own number
# instead of the fleet's.
CRITERIA_PLACEHOLDERS = ("model", "gpu_util", "low_demand", "gpu_line", "cost", "modalities")

# GPU-load source vocabulary. One value today; the constant exists so the
# routes layer validates against a named set rather than a bare string
# literal (and so a future 'prometheus' source is a one-line addition).
GPU_LOAD_SOURCES = ("offpeak",)

_GPU_INJECTION_OFF = "(gpu load injection is off)"


# TEXT-column type tags (the distiller_config P0, verbatim): asyncpg
# round-trips a Python bool through a TEXT column as "false"/"true", and
# bool("false") is True — an admin's inject_gpu_load=false would read back
# ON, silently inverting the policy. Every stored value carries an explicit
# type tag ("b:", "i:", "s:", "j:") so the decode is exact; untagged legacy
# values read as strings and pass through the per-key coercion fallbacks.
#
# 'j:' is the LIST tag (allowed_models): a JSON array in a TEXT column. It is
# decoded back to a list and NEVER raises — a hand-edited or truncated row
# decodes to an empty list (allow-all) rather than taking the config down,
# because the router's fail-safe direction is "route normally", not "refuse
# to route".
def _encode(value: Any) -> str:
    # bool BEFORE int: bool is an int subclass, and "i:true" would be garbage.
    if isinstance(value, bool):
        return "b:true" if value else "b:false"
    if isinstance(value, int):
        return "i:" + str(value)
    # FLOATS get their own tag. Without it a float falls through to "s:" and
    # reads back as the STRING "0.6" — which silently breaks every numeric
    # comparison against it (decision_min_confidence would compare float <
    # str and raise, or worse, a 0.0 default would win). int before float is
    # deliberate: isinstance(2500, float) is False, so order only matters for
    # bool (handled above).
    if isinstance(value, float):
        return "f:" + repr(value)
    # DICT before the generic str fallback. Without this tag a dict stores as
    # "s:" + str(dict) — a Python repr, not even JSON — and every consumer's
    # isinstance(x, dict) check silently fails, so the feature appears to save
    # and then does nothing. model_modalities is dict-typed, so this is its
    # only working write path.
    if isinstance(value, dict):
        try:
            return "d:" + json.dumps(dict(value), default=str)
        except (TypeError, ValueError):
            return "d:{}"
    if isinstance(value, (list, tuple)):
        # default=str: a list carrying a non-JSON scalar must still encode —
        # the row is operator input, and a refused write would leave the
        # panel showing a value the DB never took.
        return "j:" + json.dumps(list(value), default=str)
    return "s:" + str(value)


def _decode(raw: Any) -> Any:
    if isinstance(raw, (bool, int, list)):
        return raw  # a real typed column (tests' fake stores Python objects)
    text = str(raw)
    if text.startswith("b:"):
        return text[2:] == "true"
    if text.startswith("i:"):
        try:
            return int(text[2:])
        except ValueError:
            return text
    if text.startswith("f:"):
        # A float row. Junk decodes to None, NOT 0.0: for the knobs that use
        # this type, 0.0 is the LEAST safe value (decision_min_confidence 0.0
        # means the decision model never abstains; decision_switch_margin 0.0
        # means hysteresis is off). None lets decision_settings' existing
        # `is not None` fallbacks substitute the measured defaults, so a
        # truncated or hand-edited row degrades to the shipped behaviour
        # instead of silently arming an unsupervised router.
        try:
            return float(text[2:])
        except ValueError:
            return None
    if text.startswith("d:"):
        try:
            decoded = json.loads(text[2:])
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    if text.startswith("j:"):
        try:
            decoded = json.loads(text[2:])
        except (ValueError, TypeError):
            return []
        return decoded if isinstance(decoded, list) else []
    if text.startswith("s:"):
        return text[2:]
    return text  # legacy untagged row: string passthrough


async def _config() -> dict[str, Any]:
    """The merged runtime config (DEFAULTS <- DB overrides), TTL-cached,
    degrade-not-fail.

    DEGRADED is never CACHED as the policy (audit P2-17). The module header
    promises a DB blip "keeps the last known policy, never resets the router to
    defaults"; the fault path used to fall through to _cache = merged, i.e. it
    CACHED the defaults-only merge for a full TTL — exactly the reset it
    promised not to make. Now:
      * warm cache -> the last known policy is returned AND _cache_at is
        stamped, so the outage costs one DB query per TTL instead of one per
        request (the same path used to return the cache without stamping, so
        every get_config() retried the DB);
      * cold cache (boot) -> there is no last known policy to protect, so the
        defaults+env merge is returned WITHOUT being cached: the DB is retried
        rather than the degraded policy being pinned for a TTL.
    """
    global _cache, _cache_at
    now = time.monotonic()
    if _cache and now - _cache_at < _CACHE_TTL_S:
        return _cache
    merged = dict(DEFAULTS)
    # The ENV is the deployment default, the DB row is the OPERATOR override
    # (this module docstring's own layering). Applied between the two so a
    # helm value actually takes effect while the panel can still flip it --
    # without this the env knobs are inert, because DEFAULTS alone is what the
    # merge started from.
    try:
        merged["sticky_routing"] = bool(settings.sticky_routing)
        merged["sticky_routing_idle_reset_s"] = int(settings.sticky_routing_idle_reset_s)
        merged["route_default_model"] = bool(settings.route_default_model)
    except Exception:  # pragma: no cover - settings are static per process
        log.warning("routing config: env defaults unreadable - using module DEFAULTS")
    try:
        rows = await db.fetch("SELECT key, value FROM routing_config")
        for row in rows:
            if row["key"] in merged:
                merged[row["key"]] = _decode(row["value"])
    except Exception as exc:
        log.warning("routing config refresh failed (%s) - keeping last known policy", type(exc).__name__)
        if _cache:
            # KEEP the last known policy — never the degraded merge — and
            # stamp the refresh so an outage is one query per TTL, not one per
            # request (audit P2-17). A successful admin write calls
            # invalidate(), which clears the stamp and re-reads immediately.
            _cache_at = now
            return _cache
        # BOOT: no last known policy exists. Return the defaults+env merge
        # WITHOUT caching it — pinning it for a TTL is the defect this fixes.
        return merged
    _cache = merged
    _cache_at = now
    return merged


async def invalidate() -> None:
    """Force the next read to hit the DB (admin write path).

    Clears BOTH the timestamp and the cached dict (the distiller_config
    reasoning, verbatim): an in-flight reader that assigned _cache after its
    DB read but before this invalidation would otherwise restore the stale
    dict WITH a fresh timestamp, pinning stale admin config for a full TTL."""
    global _cache, _cache_at
    _cache = {}
    _cache_at = 0.0


def defaults() -> dict[str, Any]:
    """A COPY of the defaults (the routes layer reports them alongside the
    merged config so the console can render overrides distinctly). Copied
    because allowed_models is a list — a caller mutating the returned dict
    must not edit the module's DEFAULTS in place."""
    # Lists AND dicts are copied: model_modalities/model_cards are dict-typed,
    # and returning them by reference let a caller mutate the module's defaults
    # (the exact failure this helper exists to prevent for lists).
    return {
        k: (list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v)) for k, v in DEFAULTS.items()
    }


async def get_config() -> dict[str, Any]:
    """The merged effective config (DEFAULTS <- DB overrides)."""
    return await _config()


async def set_values(values: dict[str, Any]) -> dict[str, Any]:
    """Admin write: upsert each provided key, clear keys set to None, return
    the merged effective config. Unknown keys are refused by the routes layer
    (this module only stores what it knows).

    ONE transaction (the distiller_config reasoning, verbatim): a multi-key
    PUT that dies midway must not leave a half-applied admin mutation that
    the caller is told succeeded. Bounded acquire: pool exhaustion surfaces
    fast, never queues silently."""
    known = {k: v for k, v in values.items() if k in DEFAULTS}
    if known:
        async with db.acquire_bounded() as conn, conn.transaction():
            for key, value in known.items():
                if value is None:
                    await conn.execute("DELETE FROM routing_config WHERE key = $1", key)
                else:
                    await conn.execute(
                        "INSERT INTO routing_config (key, value) VALUES ($1, $2) "
                        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = now()",
                        key,
                        _encode(value),
                    )
    await invalidate()
    return await _config()


# ---------------------------------------------------------------------------
# the router's model set
# ---------------------------------------------------------------------------


async def decision_settings() -> dict[str, Any]:
    """The decision-model execution knobs, resolved and clamped. NEVER raises.

    Returned as one dict so a caller reads the whole policy from a single
    cached snapshot instead of three separate lookups that could straddle a
    TTL refresh (the same reason offpeak.effective_for reads one state).

    Every value is CLAMPED here rather than at the call site: a hand-edited or
    legacy row must not be able to produce an unbounded timeout or an
    out-of-range confidence floor, and the router must never have to defend
    itself against its own config.
    """
    from . import decision_routing

    cfg = await _config()
    mode = str(cfg.get("decision_mode") or "off").strip().lower()
    if mode not in decision_routing.DECISION_MODES:
        log.warning("routing: unknown decision_mode %r treated as off", mode)
        mode = "off"
    # Coerce through a None-checked local: cfg.get() is Any | None, and a None
    # reaching float()/int() is exactly the legacy-row case the except is for.
    conf_raw = cfg.get("decision_min_confidence")
    try:
        conf = float(conf_raw) if conf_raw is not None else decision_routing.DEFAULT_MIN_CONFIDENCE
    except (TypeError, ValueError):
        conf = decision_routing.DEFAULT_MIN_CONFIDENCE
    conf = min(1.0, max(0.0, conf))
    timeout_raw = cfg.get("decision_timeout_ms")
    try:
        timeout_ms = int(timeout_raw) if timeout_raw is not None else decision_routing.DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        timeout_ms = decision_routing.DEFAULT_TIMEOUT_MS
    timeout_ms = max(decision_routing.MIN_TIMEOUT_MS, min(timeout_ms, decision_routing.MAX_TIMEOUT_MS))
    margin_raw = cfg.get("decision_switch_margin")
    from . import model_routing as _mr

    try:
        margin = float(margin_raw) if margin_raw is not None else _mr.DEFAULT_SWITCH_MARGIN
    except (TypeError, ValueError):
        margin = _mr.DEFAULT_SWITCH_MARGIN
    margin = min(1.0, max(0.0, margin))
    mods_raw = cfg.get("model_modalities")
    modalities: dict[str, list[str]] = {}
    if isinstance(mods_raw, dict):
        for name, mods in mods_raw.items():
            if isinstance(mods, (list, tuple)) and mods:
                modalities[str(name)] = [str(m) for m in mods]
    # Per-model cards, normalised to {name: {"description": str}} with blank
    # entries DROPPED: a half-saved card must read as "no description" (the
    # bare-label fallback), never as an empty description that suppresses it.
    cards_raw = cfg.get("model_cards")
    cards: dict[str, dict[str, str]] = {}
    if isinstance(cards_raw, dict):
        for name, card in cards_raw.items():
            if not isinstance(card, dict):
                continue
            desc = card.get("description")
            if isinstance(desc, str) and desc.strip():
                cards[str(name)] = {"description": desc.strip()}
    return {
        "mode": mode,
        "model": str(cfg.get("decision_model") or ""),
        "min_confidence": conf,
        "switch_margin": margin,
        "timeout_ms": timeout_ms,
        "prompt": str(cfg.get("prompt") or ""),
        "modalities": modalities,
        "cards": cards,
    }


async def routing_allowed_models(registered: list[str]) -> list[str]:
    """The models the router may ROUTE TO, in the REGISTRY's order.

    'registered' is the caller's registry snapshot (route handlers pass
    model_router.models(); the hot path would pass its own view) — the
    intersection keeps THAT order rather than the config's, so the rendered
    prompt lists models in the same order the caller's snapshot does.

    NOTE that order is NOT a preference ranking: model_router.models() is
    alphabetically sorted and the router's actual ladder is the PROFILE's
    model_order (core.model_routing). The prompt is a SET description, not a
    priority list — do not read 'first in {{model_list}}' as 'preferred'.

    Empty config -> 'registered' unchanged (allow-all, the shipped behavior).
    An intersection that comes out EMPTY because every configured name is
    stale ALSO returns registered: a config naming models that no longer
    exist must not silently disable the router (the same graceful-degradation
    direction delete_model documents for profile orderings). Never raises.
    """
    try:
        # get_config() (not the private _config()) so this reader goes through
        # the SAME public seam every other consumer uses — one place to fake in
        # tests, and no risk of a future change to get_config's contract
        # silently bypassing the router's ceiling.
        cfg = await get_config()
        configured = cfg.get("allowed_models")
        if not isinstance(configured, list) or not configured:
            return list(registered)
        wanted = {str(m) for m in configured}
        kept = [m for m in registered if m in wanted]
        return kept or list(registered)
    except Exception as exc:  # pragma: no cover - _config already swallows
        log.warning("routing allowed-models lookup failed (%s) - allowing every registered model", type(exc).__name__)
        return list(registered)


# ---------------------------------------------------------------------------
# GPU load: the off-peak evaluator's own signal (never a second Prometheus
# client)
# ---------------------------------------------------------------------------


async def gpu_load_snapshot() -> dict[str, Any]:
    """Live per-model GPU demand, shaped for prompt rendering and the panel.

    Built from core.offpeak.models_snapshot() — the SAME evaluator the billing
    discount reads, so the prompt and the price can never disagree about how
    busy the fleet is. A second Prometheus client here would be a second
    truth. (The GLOBAL evaluator's state — offpeak.state — is deliberately not
    folded in: the per-model snapshot is what a routing decision is made
    against, and the fleet-wide average is the dilution trap the per-model
    evaluators exist to escape.)

    available=False when the feature is off in env OR the evaluator has no
    per-model target at all (no offpeak_model_promql configured) — the panel
    says so instead of pretending zero demand. NEVER raises: a snapshot fault
    degrades to available=False, because a prompt-rendering request must
    never fail for want of the signal.
    """
    empty: dict[str, Any] = {"available": False, "source": "offpeak", "models": {}}
    try:
        from . import offpeak  # deferred: keeps this module import-light

        if not settings.offpeak_enabled:
            return empty
        snapshot = offpeak.models_snapshot()
        if not snapshot:
            return empty
        models: dict[str, Any] = {}
        for name, st in snapshot.items():
            models[name] = {
                "value_pct": st.get("last_value_pct"),
                "active": st.get("active"),
                "fraction": st.get("fraction"),
                "last_check": st.get("last_check"),
                "last_error": st.get("last_error"),
                "promql": st.get("promql") or "",
            }
        return {"available": True, "source": "offpeak", "models": models}
    except Exception as exc:
        log.warning("gpu load snapshot unavailable (%s)", type(exc).__name__)
        return empty


# ---------------------------------------------------------------------------
# prompt rendering (PURE — the preview and the future decision call share it)
# ---------------------------------------------------------------------------


def _gpu_summary(state: dict[str, Any]) -> str:
    """One model's demand, without the name: '42.1% util (low demand, 0.5x)'.

    Shared by the {{gpu_load}} block and the per-card {{gpu_line}} token so the
    two can never phrase the same reading differently. A model the evaluator
    has never sampled renders 'no data' — a fabricated 0% would read as
    "idle, send it traffic", the exact wrong inference.
    """
    value = state.get("value_pct")
    if value is None:
        return "no data"
    try:
        float(value)
    except (TypeError, ValueError):
        return "no data"
    # active is None when the evaluator has a utilisation sample but no
    # hysteresis state yet (or was never sampled). Rendering that as "high
    # demand" invents a load claim the data does not support -- the same class
    # of lie as a fabricated 0%.
    active = state.get("active")
    demand = "demand unknown" if active is None else ("low demand" if active else "high demand")
    fraction = state.get("fraction")
    suffix = f", {fraction}x" if isinstance(fraction, (int, float)) else ""
    return f"{float(value):.1f}% util ({demand}{suffix})"


def _gpu_line(name: str, state: dict[str, Any]) -> str:
    """One model's demand line: '- <name>: 42.1% util (low demand, 0.5x)'."""
    return f"- {name}: {_gpu_summary(state)}"


def render_prompt(
    template: str,
    *,
    gpu: dict[str, Any],
    models: list[str],
    decision_model: str = "",
) -> str:
    """Substitute the KNOWN placeholders in 'template'; leave everything else
    untouched. PURE — no I/O, no config read, so the preview route and
    (later) the decision call render byte-identically.

    Deliberately NOT str.format and NOT eval: the template is operator text
    that may legitimately contain braces, JSON examples, or attribute-ish
    dotted paths, and a format string would either explode on them or — far
    worse with eval — execute them. Only the five PLACEHOLDERS are
    substituted, by exact {{name}} token; any other {{...}} stays VERBATIM so
    a typo shows up in the preview instead of being silently eaten.

    Truncation is the CALLER's job (the routes layer applies
    max_prompt_chars): this function stays a pure string transform whose
    output the caller can measure.
    """
    text = str(template or "")
    gpu_models = gpu.get("models") if isinstance(gpu, dict) else None
    if not isinstance(gpu_models, dict):
        gpu_models = {}

    # 'inject' is the config DECISION (the caller may have overridden it
    # in-memory for a preview); 'available' is the DATA question. The caller
    # folds both into 'inject' so a preview of inject_gpu_load=false shows
    # the off-marker even on a site whose evaluator has live samples.
    injecting = bool(gpu.get("inject", gpu.get("available"))) if isinstance(gpu, dict) else False

    if injecting and gpu_models:
        gpu_lines = "\n".join(_gpu_line(name, st) for name, st in sorted(gpu_models.items()) if isinstance(st, dict))
    elif injecting:
        # Injection asked for, evaluator has nothing: name the models we DO
        # know about rather than emitting an empty block the decision model
        # would read as "no GPUs at all".
        gpu_lines = "\n".join(f"- {m}: no data" for m in models) or "- no data"
    else:
        gpu_lines = _GPU_INJECTION_OFF
    gpu_json = json.dumps(gpu_models, sort_keys=True) if injecting else json.dumps(_GPU_INJECTION_OFF)

    substitutions = {
        "models": ", ".join(models),
        "model_list": "\n".join(f"- {m}" for m in models),
        "gpu_load": gpu_lines,
        "gpu_load_json": gpu_json,
        "decision_model": str(decision_model or ""),
    }
    for name in PLACEHOLDERS:
        text = text.replace("{{" + name + "}}", substitutions[name])
    return text


# ---------------------------------------------------------------------------
# per-model cards -> the decision model's CRITERIA
# ---------------------------------------------------------------------------


def _criteria_tokens(
    name: str,
    *,
    gpu: dict[str, Any] | None,
    costs: dict[str, Any] | None,
    modalities: dict[str, list[str]] | None,
) -> dict[str, str]:
    """One model's live values, keyed by the token names a card may use. PURE.

    Every token resolves against THIS model's own row, which is the whole
    point of the criteria channel: "{{gpu_util}}" in Deepseek's card is
    Deepseek's number, never the fleet's.

    A model the evaluator has no reading for renders "no data" rather than a
    zero, and injection-off renders the off-marker for the load tokens — an
    operator who turned load injection OFF must not still get live numbers
    through a card, which is the bug class the instructions path already fixed.
    """
    gpu_models = (gpu or {}).get("models") if isinstance(gpu, dict) else None
    state = gpu_models.get(name) if isinstance(gpu_models, dict) else None
    if not isinstance(state, dict):
        state = {}
    injecting = bool(gpu.get("inject", gpu.get("available"))) if isinstance(gpu, dict) else False
    if not injecting:
        gpu_util = low_demand = gpu_line = _GPU_INJECTION_OFF
    else:
        try:
            gpu_util = "no data" if state.get("value_pct") is None else f"{float(state['value_pct']):.1f}%"
        except (TypeError, ValueError):
            # A hand-edited / half-populated snapshot must degrade to "no data"
            # rather than 500 the preview route: this runs on a READ path too.
            gpu_util = "no data"
        # NO READING means no demand claim. A never-sampled evaluator's state is
        # value_pct=None AND active=False (the dataclass default), so keying the
        # demand word off "active" alone reported "high demand" for a model
        # nobody measured -- the same lie as a fabricated 0%. value_pct is the
        # presence test; "active" only means something once there is a sample.
        if state.get("value_pct") is None or gpu_util == "no data":
            low_demand = "no data"
        elif state.get("active") is None:
            low_demand = "demand unknown"
        else:
            low_demand = "low demand" if state.get("active") else "high demand"
        gpu_line = _gpu_summary(state)
    cost = (costs or {}).get(name) if isinstance(costs, dict) else None
    mods = (modalities or {}).get(name) if isinstance(modalities, dict) else None
    return {
        "model": str(name),
        "gpu_util": gpu_util,
        "low_demand": low_demand,
        "gpu_line": gpu_line,
        "cost": str(cost) if cost else "no data",
        # Only a real sequence is joinable: a hand-edited map can hold a
        # scalar, and a bare string would be split into characters.
        "modalities": (", ".join(str(m) for m in mods) if isinstance(mods, (list, tuple)) and mods else "not declared"),
    }


def render_criteria(
    cards: dict[str, Any] | None,
    models: list[str],
    *,
    gpu: dict[str, Any] | None = None,
    costs: dict[str, Any] | None = None,
    modalities: dict[str, list[str]] | None = None,
) -> dict[str, str]:
    """Build the decision model's {option: description} map from the cards. PURE.

    One description per ROUTABLE model, in the caller's order. A model with no
    card description renders as its bare NAME — the pre-cards behaviour — so an
    unconfigured site is byte-identical to what it served before this existed
    and never inherits prose about models it does not run.

    Tokens are substituted per model; an UNKNOWN {{...}} stays VERBATIM, the
    same discipline as render_prompt, so a typo is visible in the panel
    preview instead of being silently eaten.
    """
    safe_cards = cards if isinstance(cards, dict) else {}
    out: dict[str, str] = {}
    for name in models:
        card = safe_cards.get(name)
        raw_text = ""
        if isinstance(card, dict):
            raw = card.get("description")
            if isinstance(raw, str):
                raw_text = raw.strip()
        text = raw_text
        tokens = _criteria_tokens(name, gpu=gpu, costs=costs, modalities=modalities)
        for token, value in tokens.items():
            text = text.replace("{{" + token + "}}", value)
        # The MACHINE FACTS are appended whether or not a card exists.
        #
        # This is the fix for the one real regression of the cards change: the
        # request state stopped carrying modalities/gpu_util/cost (they belong
        # to the criteria channel now), so a site that never wrote a card would
        # have handed the decision model bare labels AND no capability, load or
        # price -- strictly less than before -- while the prompt still told it
        # to weigh those. Facts are appended here, exactly once, in the channel
        # that is bound to the option; a card that already uses the token is
        # left alone so nothing is stated twice.
        facts: list[str] = []
        uses_load = "{{gpu_util}}" in raw_text or "{{gpu_line}}" in raw_text
        # Injection OFF appends nothing: the tokens render the off-marker
        # sentence, and "gpu (gpu load injection is off) ((gpu load injection
        # is off))" is noise. The operator's own switch is the message.
        if not uses_load and tokens["gpu_util"] not in ("no data", _GPU_INJECTION_OFF):
            facts.append(f"gpu {tokens['gpu_util']} ({tokens['low_demand']})")
        if "{{cost}}" not in raw_text and tokens["cost"] != "no data":
            facts.append(f"cost {tokens['cost']}")
        if "{{modalities}}" not in raw_text and tokens["modalities"] != "not declared":
            facts.append(f"modalities {tokens['modalities']}")
        base = text or str(name)
        out[name] = base + (" | " + " | ".join(facts) if facts else "")
    return out


def describe_coverage(cards: dict[str, Any] | None, models: list[str]) -> dict[str, Any]:
    """How many routable models carry a description. PURE.

    The panel and the stats endpoint both need the same answer to "is the
    decision model being told anything?", and neither may compute it
    differently — a router that has silently lost every description is the
    failure this number exists to make visible.
    """
    safe_cards = cards if isinstance(cards, dict) else {}
    described = [
        name
        for name in models
        if isinstance(safe_cards.get(name), dict)
        and isinstance(safe_cards[name].get("description"), str)
        and safe_cards[name]["description"].strip()
    ]
    return {
        "models": len(models),
        "described": len(described),
        "undescribed": [n for n in models if n not in described],
    }
