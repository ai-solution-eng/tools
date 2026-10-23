"""Off-peak (demand-shaped) billing evaluator.

WHY: GPU demand is spiky — overnight/weekend windows sit at a fraction of
peak utilization. A tenant burning budget at 3 AM costs the fleet no
foreground capacity, so charging their budget full price wastes headroom.
When enabled, a background evaluator periodically samples an
admin-configurable PromQL (default: avg GPU utilization) through the
REGISTERED prometheus MCP server — in-cluster, behind the same registry +
header resolution every other MCP caller uses — and hysteresis-switches a
low-demand state.

Contract (all fail-safe — the discount must never apply by accident):

* ``fraction()`` is a ZERO-failure sync helper read at every settle moment:
  the configured fraction when the feature is enabled AND the hysteresis
  says low-demand; 1.0 otherwise. A mid-relay flip is honored per iteration
  because each settle reads it fresh.
* ``evaluate_once()`` NEVER raises: missing/disabled registry server,
  connection/timeout/parse failures all land in ``last_error`` with
  ``active=False`` (full price) and a WARNING log. A successful check clears
  the error and stamps the observation.
* Hysteresis band: value < enter_pct -> active; value > exit_pct -> idle;
  between the two -> UNCHANGED (no flapping at the threshold).
* The ledger keeps TRUE tokens/cost (audit); only the BUDGET GATES consume
  fraction-weighted sums (see core.budgets).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from ..db import db
from . import registry

log = logging.getLogger(__name__)

# Metadata-call bound (the catalog/15s pattern): a hung prometheus MCP
# server must not park the evaluator loop, and the loop runs in the
# background anyway — a bounded tick keeps state fresh.
_EVAL_TIMEOUT_S = 15.0

# Utilization history kept per evaluator state for the console's utilization
# panel: (ISO-ts, value) pairs of SUCCESSFUL samples. 240 slots ≈ 2 h at the
# default 30 s cadence — enough shape to see a workday's demand curve without
# growing unbounded.
_HISTORY_MAX = 240

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
_ERROR_TEXT_RE = re.compile(r'parse error|"status":\s*"error"|errorType|Traceback|HTTP [45]\d\d|invalid parameter')


@dataclass
class _State:
    """Module-level evaluator state (the /admin/offpeak snapshot source)."""

    enabled: bool = False  # feature flag as last seen by start()/stop()
    active: bool = False  # hysteresis says LOW-DEMAND right now
    fraction: float = 1.0  # configured fraction while active, else 1.0
    last_value_pct: float | None = None  # last successfully parsed sample
    last_check: str | None = None  # ISO-8601 of the last SUCCESSFUL check
    last_error: str | None = None  # last failure (cleared on success)
    state_since: str | None = None  # when the current active/idle state began
    label: str = "global"  # which evaluator instance this state belongs to
    last_warned_error: str | None = None  # per-state WARNING dedupe
    history: list[tuple[str, float]] = field(default_factory=list)

    def record(self, ts: str, value: float) -> None:
        """Append one successful sample, trimming to the ring size."""
        self.history.append((ts, value))
        if len(self.history) > _HISTORY_MAX:
            del self.history[: len(self.history) - _HISTORY_MAX]

    def history_json(self) -> list[list[Any]]:
        return [[ts, v] for ts, v in self.history]


state = _State()

# Per-model evaluator states: model name -> (promql, state). Built at
# start() from settings.offpeak_model_promql; a model with an entry bills
# by ITS hysteresis (effective_for), everything else by the global state.
_model_evals: dict[str, tuple[str, _State]] = {}

_task: asyncio.Task | None = None

# Hysteresis-band sanity (audit finding 8d): set by start() — False when
# enter_pct >= exit_pct (an inverted band discounts at HIGH demand). While
# False, _apply_state forces full price regardless of the sampled value
# (the evaluator keeps running for observability; billing never discounts).
_band_valid = True


def fraction() -> float:
    """The CURRENT billing fraction — cheap, sync, zero-failure.

    The configured fraction when the feature is enabled AND the evaluator's
    hysteresis state is low-demand; 1.0 in every other case (disabled,
    never-checked, idle, or after any evaluator failure — fail-safe).
    """
    if state.enabled and state.active:
        return state.fraction
    return 1.0


def _clamp_fraction(f: float) -> float:
    """Clamp a configured fraction into (0, 1] (audit finding 8d).

    The discount inputs come from THREE config surfaces (profile REAL column,
    env default, console override) and none carried a CHECK — a fraction of
    0 or negative records full TRUE usage on the ledger while budget gates
    consume ~zero: the full-usage/zero-consumption leak, with demand HIGH.
    0/negative fails safe to FULL PRICE (1.0); >1 clamps to 1.0 (never bill
    more than true usage)."""
    if not (f > 0.0):  # NaN-safe: NaN fails this branch too
        return 1.0
    return float(min(f, 1.0))


def key_fraction(key: Any) -> float:
    """A KEY's configured base fraction (sync, zero-failure).

    Precedence: the key's PROFILE fraction (per-profile commercial knob,
    console-editable, read at authenticate) when set; else the gateway-wide
    env default. Callers pass this into effective() at each settle moment.
    Clamped (finding 8d) — a misconfigured profile fraction can never bill
    zero."""
    pf = getattr(key, "offpeak_fraction", None)
    if pf is not None:
        return _clamp_fraction(float(pf))
    return _clamp_fraction(float(settings.offpeak_fraction))


def effective(base: float) -> float:
    """The fraction a request bills at RIGHT NOW: the key's base fraction
    during an active low-demand window, else 1.0. Fail-safe by construction
    (inactive/disabled/error state -> full price)."""
    if state.enabled and state.active:
        return _clamp_fraction(float(base))
    return 1.0


def model_active(name: str) -> bool | None:
    """PUBLIC read of one model's per-model hysteresis state (load-aware
    cascade head): True = low demand, False = not active/never sampled,
    None = no signal (no eval configured, or the evaluator is off). Pure."""
    ev = _model_evals.get(name)
    return bool(ev[1].active) if ev is not None and ev[1].enabled else None


def effective_for(model: str, base: float) -> float:
    """The fraction a request for THIS MODEL bills at right now: the model's
    own hysteresis state when one is configured
    (settings.offpeak_model_promql), else the global state. Fail-safe by
    construction (inactive/disabled/error -> full price).

    Precedence while the governing evaluator is active: the model's
    console-set ratio override (offpeak_model_config, see set_override)
    first — the panel is the most specific commercial knob for the model —
    then the request's base (profile fraction ?? env default). Both clamped
    (finding 8d)."""
    ev = _model_evals.get(model)
    st = ev[1] if ev else state
    if st.enabled and st.active:
        ov = _overrides.get(model)
        return _clamp_fraction(float(ov)) if ov is not None else _clamp_fraction(float(base))
    return 1.0


# ---------------------------------------------------------------------------
# per-model ratio overrides (console-editable, DB-backed)
# ---------------------------------------------------------------------------
# The Off-peak panel sets a model's pricing ratio; profiles/env remain the
# fallback base for models without one. Cache mirrors the model_costs
# pattern: in-memory, re-read on TTL by the evaluator loop (staleness bound
# for sibling replicas), invalidated on every admin write (instant here),
# loaded at start(). A failed refresh KEEPS the last known ratios — a DB
# blip must never silently reset commercial terms.
_OVERRIDES_TTL = 60.0
_overrides: dict[str, float] = {}
_overrides_loaded_at = 0.0


async def load_overrides(force: bool = False) -> None:
    """Refresh the per-model ratio cache (TTL-bounded; force on write/start)."""
    global _overrides, _overrides_loaded_at
    import time as _t

    if not force and _t.monotonic() - _overrides_loaded_at < _OVERRIDES_TTL:
        return
    try:
        rows = await db.fetch("SELECT model_name, fraction FROM offpeak_model_config")
        _overrides = {r["model_name"]: float(r["fraction"]) for r in rows}
        _overrides_loaded_at = _t.monotonic()
    except Exception as exc:
        log.warning("offpeak: ratio refresh failed (%s) — keeping last known ratios", exc)


async def set_override(model_name: str, fraction: float | None) -> None:
    """Set (fraction) or clear (None) a model's off-peak pricing ratio."""
    if fraction is None:
        await db.execute("DELETE FROM offpeak_model_config WHERE model_name = $1", model_name)
    else:
        await db.execute(
            "INSERT INTO offpeak_model_config (model_name, fraction) VALUES ($1, $2) "
            "ON CONFLICT (model_name) DO UPDATE SET fraction = $2, updated_at = now()",
            model_name,
            float(fraction),
        )
    await load_overrides(force=True)


def _load_model_evals() -> None:
    """(Re)build the per-model evaluator states from settings. Invalid
    entries are skipped with a warning — a typo in one model's query must
    not disable per-model billing for the rest."""
    raw = settings.offpeak_model_promql or {}
    rebuilt: dict[str, tuple[str, _State]] = {}
    for name, promql in raw.items():
        if not isinstance(promql, str) or not promql.strip():
            log.warning("offpeak: ignoring model %r — empty/non-string promql", name)
            continue
        st = _State()
        st.label = name
        rebuilt[name] = (promql, st)
    _model_evals.clear()
    _model_evals.update(rebuilt)


def models_snapshot() -> dict[str, dict[str, Any]]:
    """Per-model evaluator states for the /admin/offpeak snapshot — the
    utilization panel's data source (value, thresholds context via the
    route, state, and the recent-sample history for the sparkline)."""
    return {
        name: {
            "active": st.active,
            "fraction": st.fraction,
            "fraction_override": _overrides.get(name),
            "last_value_pct": st.last_value_pct,
            "last_check": st.last_check,
            "last_error": st.last_error,
            "state_since": st.state_since,
            "promql": promql,
            "history": st.history_json(),
        }
        for name, (promql, st) in sorted(_model_evals.items())
    }


# ---------------------------------------------------------------------------
# result parsing — FIRST float out of an MCP CallToolResult
# ---------------------------------------------------------------------------


def _collect_texts(result: Any) -> list[str]:
    """Flatten an MCP CallToolResult (or a bare str/dict from tests) into
    candidate text blobs, in order."""
    if result is None:
        return []
    if isinstance(result, str):
        return [result]
    blocks = getattr(result, "content", None)
    if not isinstance(blocks, (list, tuple)):
        blocks = [result]
    texts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            texts.append(text)
        elif isinstance(block, (dict, list, tuple, int, float, bool)):
            texts.append(json.dumps(block, default=str))
        else:
            texts.append(str(block))
    return texts


def _num(value: Any) -> float | None:
    """Parse a scalar as float (strings via the first numeric token)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _NUM_RE.search(value)
        if m:
            try:
                return float(m.group(0))
            except ValueError:  # pragma: no cover - regex guarantees a number
                return None
    return None


def _extract(obj: Any) -> float | None:
    """Pull the sample value out of a parsed JSON payload.

    Prometheus instant-query shape awareness: a vector sample is the PAIR
    [epoch_ts, "value"] — the sample VALUE is the pair's LAST slot (a naive
    "first number in the text" would read the epoch timestamp). Otherwise
    descend into the conventional keys first (value/result/data), then scan.
    """
    if isinstance(obj, bool):
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, str):
        return _num(obj)
    if isinstance(obj, dict):
        pair = obj.get("value")
        if isinstance(pair, (list, tuple)) and pair:
            tail = _extract(pair[-1])
            if tail is not None:
                return tail
        for key in ("result", "data", "metric"):
            if key in obj:
                found = _extract(obj[key])
                if found is not None:
                    return found
        for value in obj.values():
            found = _extract(value)
            if found is not None:
                return found
        return None
    if isinstance(obj, (list, tuple)):
        # Prometheus [ts, value] sample pair: the value rides last.
        if len(obj) == 2:
            head = _num(obj[0])
            tail = _num(obj[1])
            if head is not None and head >= 1e9 and tail is not None:
                return tail
        for value in obj:
            found = _extract(value)
            if found is not None:
                return found
    return None


def first_float(result: Any) -> float | None:
    """FIRST float from the tool result content; None when nothing parses.

    JSON text (Prometheus vector/scalar payloads) is parsed structurally;
    plain text falls back to the first numeric token — EXCEPT error-shaped
    text (a 400's status code is not a GPU utilization).
    """
    for text in _collect_texts(result):
        if _ERROR_TEXT_RE.search(text):
            continue  # error payload — its numbers are status codes, not data
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None:
            found = _extract(parsed)
            if found is not None:
                return found
        m = _NUM_RE.search(text)
        if m:
            try:
                return float(m.group(0))
            except ValueError:  # pragma: no cover - regex guarantees a number
                continue
    return None


# ---------------------------------------------------------------------------
# the MCP call (one seam tests can fake) + one evaluation tick
# ---------------------------------------------------------------------------


async def _call_tool(
    url: str,
    headers: dict[str, str],
    tool: str,
    arguments: dict[str, Any],
    timeout_s: float,
) -> Any:
    """One MCP tool call in a fresh streamable session — the same per-call
    client + metadata-timeout pattern routes_mcp uses (15s: a hung
    prometheus must not park the evaluator; the session/opener is the
    monkeypatch seam for tests)."""
    from mcp import ClientSession

    from ..api.routes_mcp import _upstream_session  # lazy: avoids import cycles

    async def do(session: ClientSession) -> Any:
        return await session.call_tool(tool, arguments)

    return await _upstream_session(url, headers, do, timeout_s=timeout_s)


_last_warned_error: str | None = None  # dedupe: WARN per DISTINCT error only


def _fail_state(st: _State, message: str) -> None:
    """Fail-safe ONE evaluator state: idle + full price, error surfaced,
    WARNING logged per DISTINCT signature (per-state dedupe — a tick every
    check_s must not spam an identical WARNING ~2880x/day)."""
    st.active = False
    st.fraction = 1.0
    st.last_error = message
    if message != st.last_warned_error:
        log.warning("offpeak evaluator[%s]: %s", st.label, message)
        st.last_warned_error = message
    else:
        log.debug("offpeak evaluator[%s] (repeat): %s", st.label, message)


def _fail(message: str) -> None:
    """Global-state wrapper for _fail_state (compat)."""
    _fail_state(state, message)


async def _eval_one(name: str, promql: str, st: _State, url: str, headers: dict[str, str]) -> None:
    """One target's fetch + parse + state switch — the gather slot body.

    The per-target semantics are byte-for-byte the sequential loop's:
    isError content, unparseable results, and transport failures all fail
    safe via _fail_state (that target only — full price + last_error + the
    per-state WARNING dedupe); a good sample rides _apply_state (hysteresis,
    override, history) and logs the same INFO line. Raises nothing a caller
    outside the evaluator could distinguish: every Exception is normalized
    to a _fail_state call, so the gather's return_exceptions slots stay
    None in every ordinary path."""
    try:
        result = await _call_tool(
            url,
            headers,
            settings.offpeak_tool,
            {"query": promql},
            _EVAL_TIMEOUT_S,
        )
        # MCP tools may report failure as CONTENT with isError=True (the
        # prometheus server wraps upstream HTTP 4xx/5xx this way). Treating
        # that content as data once made first_float parse "400" out of
        # "Prometheus returned HTTP 400" and bill on an HTTP status code.
        if getattr(result, "isError", False):
            _fail_state(st, "; ".join(_collect_texts(result))[:300] or "tool reported isError")
            return
        value = first_float(result)
        if value is None:
            _fail_state(st, f"no numeric value parsed from {settings.offpeak_tool} result")
            return
        override = _overrides.get(name) if name else None
        _apply_state(st, value, override)
        label = f"offpeak evaluator[{name}]" if name else "offpeak evaluator"
        log.info("%s: value=%.2f active=%s fraction=%.2f", label, value, st.active, st.fraction)
    except Exception as exc:
        _fail_state(st, str(exc) or type(exc).__name__)


def _apply_state(st: _State, value: float, override: float | None = None) -> None:
    """Stamp a successful sample through ONE state's hysteresis switch.

    ``override`` is the model's console-set ratio (model states only): the
    state's display fraction reflects what the model would ACTUALLY bill at
    while active, so the panel never shows a number the ledger contradicts.

    An INVERTED band (finding 8d) forces full price regardless of the
    sample — the evaluator's observability surface stays live, billing
    never discounts on a misconfigured band."""
    prev_active = st.active
    if _band_valid:
        if value < settings.offpeak_enter_pct:
            st.active = True
        elif value > settings.offpeak_exit_pct:
            st.active = False
    else:
        st.active = False
    # between enter and exit: hysteresis band — state UNCHANGED (no flap)
    base = override if override is not None else settings.offpeak_fraction
    st.fraction = _clamp_fraction(base) if st.active else 1.0
    st.last_value_pct = value
    st.last_check = datetime.now(UTC).isoformat()
    st.last_error = None
    st.last_warned_error = None  # recovery: a future failure re-warns
    st.record(st.last_check, value)
    if st.active != prev_active or st.state_since is None:
        st.state_since = st.last_check


def _apply(value: float) -> None:
    """Global-state wrapper for _apply_state (compat)."""
    _apply_state(state, value)


async def evaluate_once() -> None:
    """One evaluation tick. NEVER raises.

    Resolves the configured server ONCE, then samples the global query AND
    every per-model query (settings.offpeak_model_promql) — each through its
    own hysteresis state. A failure in one query fails ONLY that state
    (full price + last_error); a server-resolution failure fails them all.
    Per-model queries exist because the global GPU average dilutes a busy
    model's utilization with every other GPU in the cluster (live: GLM's 4
    GPUs at ~90% averaged down to ~22% by 12 idle ones).
    """
    targets: list[tuple[str, str, _State]] = [("", settings.offpeak_promql, state)]
    for name, (promql, mst) in sorted(_model_evals.items()):
        targets.append((name, promql, mst))

    if not settings.offpeak_enabled:
        for _name, _promql, st in targets:
            st.enabled = False
            st.active = False
            st.fraction = 1.0
            st.last_error = None
        return
    for _name, _promql, st in targets:
        st.enabled = True

    # TTL-bounded refresh of the console-set ratios (no-op within the TTL):
    # sibling replicas converge within a cadence, the writing pod instantly.
    await load_overrides()

    try:
        servers = {s["name"]: s for s in await registry.mcp_servers()}
        target = servers.get(settings.offpeak_mcp_server)
        if target is None:
            for _name, _promql, st in targets:
                _fail_state(st, f"MCP server {settings.offpeak_mcp_server!r} is not in the enabled registry")
            return
        from ..api.routes_mcp import resolve_mcp_headers  # lazy: avoids import cycles

        headers = await resolve_mcp_headers(target, key_id=None)  # server-level only
    except Exception as exc:
        for _name, _promql, st in targets:
            _fail_state(st, str(exc) or type(exc).__name__)
        return

    # REMEDIATION P5: the per-target fetches run CONCURRENTLY (asyncio.gather,
    # return_exceptions=True) instead of sequentially — N models can no longer
    # serialise N × _EVAL_TIMEOUT_S inside the 30 s cadence. Error isolation is
    # identical to the sequential version: every failure lands in that target's
    # _State via _fail_state (log dedupe per state), and other targets keep
    # their results. Per-fetch timeout unchanged. _call_tool builds a FRESH
    # client per call (_upstream_session), so the concurrent calls share no
    # connection state — gather-safe by construction.
    results = await asyncio.gather(
        *(_eval_one(name, promql, st, target["url"], headers) for name, promql, st in targets),
        return_exceptions=True,
    )
    # Belt-and-braces for a return_exceptions leak (a non-Exception BaseException
    # would surface here): every target either landed in its own state or —
    # should one somehow not — fails safe to full price. A leaked slot FAILS
    # THAT STATE SAFE (it does not propagate): the tick's zero-failure contract
    # holds even if a gather slot raised something _eval_one's own try/except
    # could not normalize. (CancelledError of the gather itself still
    # propagates through await — only slot-level leaks land here.)
    for (_name, _promql, st), slot_result in zip(targets, results, strict=True):
        if isinstance(slot_result, BaseException):
            _fail_state(st, str(slot_result) or type(slot_result).__name__)


# ---------------------------------------------------------------------------
# background loop (lifespan-owned)
# ---------------------------------------------------------------------------


async def _loop() -> None:
    """Evaluate, sleep check_s, repeat. evaluate_once swallows its own
    failures, but the wrapper catches anyway so the loop can never die —
    a dead evaluator would silently pin billing at whichever state it
    died in."""
    while True:
        try:
            await evaluate_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive belt-and-braces
            log.exception("offpeak evaluator loop tick failed unexpectedly")
        await asyncio.sleep(max(settings.offpeak_check_s, 0.1))


async def start() -> None:
    """Start the background evaluator (idempotent)."""
    global _task
    if _task is not None and not _task.done():
        return
    # Hysteresis-band validation (audit finding 8d): enter >= exit makes
    # nearly EVERY value "active" (the band inverts) — the discount would
    # apply at high demand, the exact accident the fail-safe design forbids.
    # Refuse to arm the discount: force fraction 1.0 for the run and warn
    # loudly (the feature stays up for observability; billing stays full).
    global _band_valid
    _band_valid = settings.offpeak_enter_pct < settings.offpeak_exit_pct
    if not _band_valid:
        log.warning(
            "offpeak: offpeak_enter_pct (%.1f) >= offpeak_exit_pct (%.1f) — the hysteresis band is "
            "inverted and would discount at HIGH demand; billing is FORCED FULL PRICE until the "
            "band is fixed (per-model overrides still clamp to [0,1])",
            settings.offpeak_enter_pct,
            settings.offpeak_exit_pct,
        )
    _load_model_evals()
    await load_overrides(force=True)  # console-set ratios live from the first tick
    state.enabled = True
    if not _band_valid:
        state.fraction = 1.0
    _task = asyncio.create_task(_loop(), name="offpeak-evaluator")
    log.info(
        "offpeak evaluator started: server=%r tool=%r promql=%r enter=%.1f exit=%.1f fraction=%.2f check_s=%.1f",
        settings.offpeak_mcp_server,
        settings.offpeak_tool,
        settings.offpeak_promql,
        settings.offpeak_enter_pct,
        settings.offpeak_exit_pct,
        settings.offpeak_fraction,
        settings.offpeak_check_s,
    )


async def stop() -> None:
    """Cancel the evaluator and drop back to full price (idempotent)."""
    global _task, _band_valid
    task = _task
    _task = None
    _band_valid = True
    state.enabled = False
    state.active = False
    state.fraction = 1.0
    # Per-model states too (audit finding: stop() only reset the global
    # state — per-model evaluators kept enabled=True/active=<last>, so
    # effective_for kept discounting after a "stop")
    for _promql, mst in _model_evals.values():
        mst.enabled = False
        mst.active = False
        mst.fraction = 1.0
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def reset() -> None:
    """Test seam: back to pristine module state (no task, full price)."""
    global _task, state, _overrides, _overrides_loaded_at, _band_valid
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    state = _State()
    _overrides = {}
    _overrides_loaded_at = 0.0
    _band_valid = True
    for _promql, mst in _model_evals.values():
        mst.enabled = False
        mst.active = False
        mst.fraction = 1.0
