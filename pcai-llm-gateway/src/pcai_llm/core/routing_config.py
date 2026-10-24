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
    "inject_gpu_load": False,
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
}

# The ONLY placeholder names render_prompt substitutes. Anything else in
# {{...}} is left VERBATIM (a typo must be visible in the preview, not
# silently eaten) — the console renders this list as the placeholder legend,
# so the legend and the renderer cannot drift.
PLACEHOLDERS = ("models", "model_list", "gpu_load", "gpu_load_json", "decision_model")

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
        # A float row. Junk degrades to 0.0 (the safest numeric floor) rather
        # than the raw string: every consumer of these knobs does arithmetic.
        try:
            return float(text[2:])
        except ValueError:
            return 0.0
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
    degrade-not-fail."""
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
            return _cache
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
    return {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}


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
    return {
        "mode": mode,
        "model": str(cfg.get("decision_model") or ""),
        "min_confidence": conf,
        "timeout_ms": timeout_ms,
        "prompt": str(cfg.get("prompt") or ""),
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


def _gpu_line(name: str, state: dict[str, Any]) -> str:
    """One model's demand line: '- <name>: 42.1% util (low demand, 0.5x)'.

    The demand word and the fraction come from the evaluator's hysteresis
    state, so a reader can see WHY the price is what it is. A model the
    evaluator has never sampled renders 'no data' — a fabricated 0% would
    read as "idle, send it traffic", the exact wrong inference.
    """
    value = state.get("value_pct")
    if value is None:
        return f"- {name}: no data"
    demand = "low demand" if state.get("active") else "high demand"
    fraction = state.get("fraction")
    suffix = f", {fraction}x" if isinstance(fraction, (int, float)) else ""
    return f"- {name}: {float(value):.1f}% util ({demand}{suffix})"


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
