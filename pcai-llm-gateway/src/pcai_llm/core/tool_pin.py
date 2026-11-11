"""Tool-definition pinning: a grant remembers the DEFINITION it approved.

WHY THIS EXISTS (2026-11-10, tool-trust hardening): a per-tool grant approves a
NAME. An MCP server can serve that name with one definition while the operator
reviews it, then rewrite `description` or `inputSchema` on a later listing --
the "rug-pull" -- and the ACL still admits the call, because nothing in the
grant chain records WHAT was approved. The model is then offered a tool whose
prose and schema nobody reviewed, and a rewritten description is an instruction
channel straight into the model's context.

THE MECHANISM: fingerprint the definition, remember it the first time it is
seen, and compare every later listing against the remembered digest. A
mismatch is a verdict (CHANGED) the caller decides what to do with -- warn, or
drop the tool from the offered surface (core.toolrelay.pin_catalog) AND refuse
to DISPATCH it (core.toolrelay.pin_dispatch_refusal): the offered surface is
not the executor's gate, because a model can still name the tool from
conversation history. A pinned tool that VANISHES from the listing is refused
too (VANISHED): otherwise "rewrite it, then omit it" would be a free bypass,
because the ACL only ever approved the NAME.

THE CONTRACT (non-negotiable):
  * PURE. No model, no network, no I/O, no config. Deterministic: the same
    definition bytes produce the same digest in every process and every run,
    which is what makes a remembered pin meaningful.
  * CANONICAL ENCODING. Sorted keys, no whitespace variance, description
    normalised for TRAILING whitespace only -- a server re-serialising its
    schema must not read as a rug-pull, while a semantic change always does.
  * IN-MEMORY. Pins live in this process; a restart re-records them. That is
    the honest scope of a memory-resident store (a persistent table is a
    schema change, deliberately outside this change's blast radius).
  * NEVER a dependency. observe() is a dict operation under a lock; callers
    still guard it, because a defence that can break a turn is a liability.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Verdicts. Plain strings (not an Enum) because they ride log lines and,
# later, wire events; a closed vocabulary keeps both readable.
FIRST_SEEN = "first_seen"
MATCH = "match"
CHANGED = "changed"
# A tool that had a LIVE pin and is now ABSENT from the listing (2026-11-10,
# cross-validation P0). The ACL is name-based, so "rewrite the definition,
# then omit the tool from tools/list (or fail the listing)" used to leave the
# dispatch gate with no verdict at all and the rewritten definition ran
# ungated forever. Absence is therefore a verdict of its own.
VANISHED = "vanished"

# Policy modes (the caller's decision, not this module's).
MODES = ("off", "warn", "block")
MODE_OFF, MODE_WARN, MODE_BLOCK = MODES
# Fail-safe middle: a typo must neither silently disable the defence ("off")
# nor block every tool ("block").
DEFAULT_MODE = MODE_WARN
# A typo such as "enforce" coerces to "warn", which SILENTLY disables the
# dispatch gate. Log the coercion once per distinct bad value (a per-result
# warning would be a flood) so the operator can find the typo.
_UNKNOWN_MODE_LOGGED: set[str] = set()

# 16 hex chars = 64 bits. Forging a collision needs ~2^32 crafted definitions
# for a coin-flip, and this is a tripwire, not a signature; the short form
# keeps log lines (and any future audit row) readable.
DIGEST_CHARS = 16


def resolve_mode(value: Any) -> str:
    """Normalise a configured mode; anything unrecognised becomes "warn".

    Same discipline as every other mode knob in the gateway: an unparsable
    value lands on the visible-but-inert setting instead of guessing. The
    coercion is LOGGED once per distinct value (2026-11-10, cross-validation
    P2): a typo like "enforce" must not silently disable the dispatch gate.
    An unset value (None/"") is the documented default, not a typo, so it is
    silent.
    """
    mode = str(value or "").strip().lower()
    if mode in MODES:
        return mode
    raw = "" if value is None else str(value)
    if raw.strip() and raw not in _UNKNOWN_MODE_LOGGED:
        _UNKNOWN_MODE_LOGGED.add(raw)
        log.warning("tool_pin: unknown tool_pin_mode %r — using %r", raw, DEFAULT_MODE)
    return DEFAULT_MODE


def _normalise_description(value: Any) -> str:
    """Trailing whitespace stripped, line endings unified. Nothing else.

    Deliberately NOT a whitespace collapse: a description that reflows its
    prose IS a different definition to a reviewer, while a trailing newline or
    a CRLF/CR difference is serialisation noise (a server changing its JSON
    encoder must not look like an attack).
    """
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).rstrip()


def _canonical(value: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace. PURE.

    `default=str` is a last-resort encoder for a non-JSON value smuggled into
    a schema (an MCP listing is JSON, so this should never fire) -- it keeps
    the fingerprint total instead of raising on the tool path.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _pin_description(value: Any) -> Any:
    """The fingerprint's description slot: normalised text, or a TYPED tag.

    WHY THE TAG (2026-11-10, cross-validation P2): the old slot was
    `_normalise_description(value)`, which str()-folds, so integer 1 and
    string "1" produced the SAME digest -- a server could flip the type of a
    pinned field without the pin moving. MCP says description is a string;
    anything else is already anomalous, so a readable type tag costs nothing
    and keeps the digest total (never raises).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return _normalise_description(value)
    return {"__pcai_non_string__": type(value).__name__, "value": value}


def fingerprint(name: Any, description: Any, input_schema: Any, annotations: Any = None) -> str:
    """16-hex-char digest of ONE tool definition. PURE and deterministic.

    Stable to dict key order at every depth (sort_keys is recursive) and to
    trailing-whitespace-only edits of the description. `input_schema` and
    `annotations` of None normalise to {} so a server that omits an empty
    object and one that sends it explicitly pin the SAME definition -- both
    mean "nothing declared".
    """
    payload = {
        "name": "" if name is None else str(name),
        "description": _pin_description(description),
        "inputSchema": {} if input_schema is None else input_schema,
        "annotations": {} if annotations is None else annotations,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:DIGEST_CHARS]


@dataclass(frozen=True)
class PinVerdict:
    """What ONE observation of a tool definition meant.

    `previous` is the fingerprint the store had on record: empty for
    FIRST_SEEN, the same digest for MATCH, and the APPROVED digest for CHANGED
    (the one the operator's grant was made against). `current` is what was
    just observed.
    """

    verdict: str
    previous: str = ""
    current: str = ""

    @property
    def changed(self) -> bool:
        return self.verdict == CHANGED

    @property
    def vanished(self) -> bool:
        return self.verdict == VANISHED


class PinStore:
    """In-memory, thread-safe (server, tool) -> fingerprint store.

    TTL: `ttl_s` (seconds; 0 = never) is measured from when a fingerprint was
    RECORDED, not from its last observation. A continuously-observed tool
    therefore re-approves after the TTL, which is the point of a TTL -- a pin
    that could only expire on silence would never expire in a busy process.
    An expired entry is dropped and the next observation is FIRST_SEEN again.
    """

    def __init__(self, ttl_s: float = 0.0) -> None:
        self._lock = threading.Lock()
        # (server, tool) -> (fingerprint, recorded_at: time.monotonic())
        self._pins: dict[tuple[str, str], tuple[str, float]] = {}
        self.ttl_s = ttl_s

    def observe(self, server: str, tool: str, fp: str) -> PinVerdict:
        """Record/compare ONE observation. The ONLY mutator. Thread-safe."""
        key = (str(server), str(tool))
        now = time.monotonic()
        with self._lock:
            recorded = self._pins.get(key)
            if recorded is not None and self._expired(recorded[1], now):
                del self._pins[key]
                recorded = None
            if recorded is None:
                self._pins[key] = (str(fp), now)
                return PinVerdict(FIRST_SEEN, "", str(fp))
            if recorded[0] == fp:
                return PinVerdict(MATCH, recorded[0], str(fp))
            # CHANGED: the APPROVED fingerprint STAYS on record. Re-pinning
            # here would let a rug-pull approve itself on the next listing
            # (change once, then MATCH forever) -- exactly the bypass this
            # store exists to prevent. An operator re-approves by restarting
            # the process (or via reset()), never by the server changing again.
            return PinVerdict(CHANGED, recorded[0], str(fp))

    def pinned(self, server: str, tool: str) -> str:
        """The fingerprint on record for one tool, or "" (also when expired)."""
        with self._lock:
            recorded = self._pins.get((str(server), str(tool)))
            if recorded is None or self._expired(recorded[1], time.monotonic()):
                return ""
            return recorded[0]

    def pinned_keys(self) -> list[tuple[str, str]]:
        """Every (server, tool) with a LIVE pin. Snapshot; expired entries drop.

        Used by the offer pass to notice a pinned tool that is ABSENT from the
        listing it should have appeared in (the vanished-tool bypass).
        """
        now = time.monotonic()
        with self._lock:
            return [key for key, recorded in self._pins.items() if not self._expired(recorded[1], now)]

    def reset(self) -> None:
        """Forget every pin (tests, and an operator-initiated re-approval)."""
        with self._lock:
            self._pins.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._pins)

    def _expired(self, recorded_at: float, now: float) -> bool:
        ttl = self.ttl_s
        return ttl > 0 and (now - recorded_at) > ttl


def dispatch_refusal(server: Any, tool: Any, verdict: PinVerdict) -> str:
    """The tool-result text that REFUSES one dispatch. PURE and non-empty.

    WHY (dispatch gate, 2026-11-10): removing a changed tool from the OFFERED
    surface is not a gate. A model that names it from CONVERSATION HISTORY (a
    replayed tool_calls turn, a definition it read before the rewrite) never
    consults the offer, so block mode alone was a speed bump. The relay
    re-checks the pin when the call ARRIVES and returns this text instead of
    dispatching upstream.

    SHAPE: an ORDINARY tool error. The caller appends it as a normal
    role=tool result (same tool_call_id, a non-empty string starting
    "error:"), so the relay loop keeps iterating and the model can
    self-correct -- a refusal that broke the loop would be worse than the
    attack. It names <server>__<tool> and BOTH digests, so an operator
    reading a trace can tell WHICH definition changed and what it changed
    from, and it says the definition changed SINCE APPROVAL (not that the
    call was malformed) so the model does not simply retry the same call.
    """
    return (
        f"error: the gateway refused to run {server}__{tool}: its definition changed since this"
        f" grant approved it (approved fingerprint {verdict.previous or 'unknown'}, current"
        f" {verdict.current or 'unknown'}), so the call was NOT executed."
        " Ask the operator to re-approve the tool, or use a different tool."
    )


def vanished_refusal(server: Any, tool: Any, approved: str) -> str:
    """The tool-result text that REFUSES a dispatch whose tool VANISHED.

    WHY (2026-11-10, cross-validation P0): the ACL is NAME-based and the pin
    was only consulted for tools PRESENT in the listing. A server could pin a
    harmless definition, rewrite it, then OMIT the tool from tools/list (or
    fail the listing): no verdict was recorded, the dispatch gate found no
    entry, and the rewritten definition ran ungated forever. A tool the store
    still holds a live pin for, but which is absent from the listing, is
    therefore refused in block mode -- the definition the grant approved is
    no longer on offer, so the NAME must not be executable. A tool that was
    NEVER pinned still fails open (first run / restart).

    SHAPE: an ordinary tool error, like dispatch_refusal, so the relay loop
    keeps iterating and the model can self-correct.
    """
    return (
        f"error: the gateway refused to run {server}__{tool}: it vanished from the listing since"
        f" this grant approved it (approved fingerprint {approved or 'unknown'}), so the call was"
        " NOT executed. The tool may have been renamed, hidden, or its server's listing may have"
        " failed. Ask the operator to re-approve the tool, or use a different tool."
    )
