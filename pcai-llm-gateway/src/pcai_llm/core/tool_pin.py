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
  * PURE FINGERPRINT. fingerprint() does no model, no network, no I/O, no
    config. Deterministic: the same definition bytes produce the same digest in
    every process and every run, which is what makes a remembered pin
    meaningful.
  * CANONICAL ENCODING. Sorted keys, no whitespace variance, description
    normalised for TRAILING whitespace only -- a server re-serialising its
    schema must not read as a rug-pull, while a semantic change always does.
  * PERSISTENT (2026-11-23, recommendation B2). Pins are remembered in the
    tool_pins table, so a restart does NOT re-approve the definitions being
    served: a fresh store loads what an earlier process approved BEFORE it
    computes a verdict, so a rewritten definition observed after a restart is
    CHANGED, not FIRST_SEEN. Persistence is BEST-EFFORT (a DB fault degrades to
    the in-memory store this module started as, logging once) and BOUNDED (one
    load per process; last_seen at most once per key per TOUCH_INTERVAL_S).
  * NEVER a dependency. observe() is a dict operation under a lock plus, at
    most, a non-blocking write; callers still guard it, because a defence that
    can break a turn is a liability.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import asyncpg

from ..db import Database, db

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


# --- persistence (2026-11-23, recommendation B2) ---------------------------
#
# WHY: the store below started as process memory, and that was the honest scope
# of a memory-resident pin -- but it is NOT the scope of a control. A restart
# re-approved whatever definitions the MCP servers happened to be serving at that
# moment, so an attacker who rewrites a definition only has to wait for (or
# provoke) a deploy: "block" mode was a tripwire with a one-restart memory. The
# tool_pins table (db/schema.py) is that memory made durable.
#
# THE THREE RULES, and the reason for each:
#   1. LOAD BEFORE THE FIRST VERDICT. A deferred load would be WORSE than no
#      persistence: the first observation after a restart would be FIRST_SEEN and
#      would RECORD the rewritten definition over the approved one, so the
#      rug-pull would approve itself. A fresh store therefore loads on first use.
#   2. WRITE-THROUGH, BOUNDED. FIRST_SEEN persists one upsert (once per tool,
#      ever). MATCH would be a round trip per tool per turn, so last_seen is
#      written at most once per key per TOUCH_INTERVAL_S -- it is observation
#      history for an operator, not a value any decision reads.
#   3. BEST-EFFORT, NEVER A DEPENDENCY. Same posture as traces: a fault degrades
#      to the in-memory store and is logged ONCE per site (a warning per tool per
#      turn would be a flood), and a store whose LOAD failed disables writes --
#      re-recording a digest we could not read first is exactly the overwrite that
#      rule 1 exists to prevent.

# How often a MATCH may refresh last_seen for one (server, tool). A pin is
# compared on every listing and every step; a last_seen that moves by
# milliseconds tells an operator nothing, so the write is coalesced per key.
TOUCH_INTERVAL_S = 300.0

# Bound on the one-shot load (a fresh process, before its first verdict). A
# local/in-cluster PostgreSQL answers the whole table in single-digit
# milliseconds; the bound exists so a wedged connection degrades to in-memory
# instead of holding a relay request open.
LOAD_TIMEOUT_S = 1.0

# The tool_pins statements. The table is the one in db/schema.py.
_SELECT_ALL_SQL = "SELECT server, tool, fingerprint, first_seen FROM tool_pins"
# Upsert, not INSERT: a pin that EXPIRED (ttl_s) or was re-recorded must replace
# the row, and first_seen is the TTL's origin, so both timestamps move.
_SAVE_SQL = """
INSERT INTO tool_pins (server, tool, fingerprint, first_seen, last_seen)
VALUES ($1, $2, $3, now(), now())
ON CONFLICT (server, tool) DO UPDATE
    SET fingerprint = EXCLUDED.fingerprint,
        first_seen  = EXCLUDED.first_seen,
        last_seen   = EXCLUDED.last_seen
"""
# last_seen only, and only while the row still holds the digest just observed: a
# touch must never resurrect a fingerprint that a re-record replaced.
_TOUCH_SQL = "UPDATE tool_pins SET last_seen = now() WHERE server = $1 AND tool = $2 AND fingerprint = $3"
_CLEAR_SQL = "DELETE FROM tool_pins"


@dataclass(frozen=True)
class PersistedPin:
    """ONE tool_pins row, as the store consumes it."""

    server: str
    tool: str
    fingerprint: str
    # WALL-CLOCK epoch seconds of the first observation. The in-memory store
    # measures TTL from a monotonic clock that a restart resets, so a row's
    # first_seen is converted to an AGE at load time instead (see _absorb).
    first_seen: float


class PinBackend(Protocol):
    """The persistence seam. SYNC by contract, and that is not an oversight.

    observe() is called from the relay's SYNCHRONOUS offer/dispatch passes
    (core.toolrelay.pin_catalog_with_verdicts), so the store's own API cannot
    become async without re-plumbing the relay. A backend therefore hides the
    asynchrony: _DbPinBackend reads on a helper thread and writes
    fire-and-forget onto the caller's loop.

    Implementations must be safe to call from any thread and should not raise
    on the hot path: the store guards every call (a fault degrades to memory),
    but a backend raising on every observation would log a fault per tool.
    """

    def load(self) -> list[PersistedPin]:
        """Every pin on record. The ONE call that may block."""
        ...

    def save(self, server: str, tool: str, fingerprint: str) -> None:
        """Persist a NEW pin (FIRST_SEEN), replacing any row for that key."""
        ...

    def touch(self, server: str, tool: str, fingerprint: str) -> None:
        """Refresh last_seen for a MATCH (guarded on the digest)."""
        ...

    def clear(self) -> None:
        """Forget every pin on record (operator-initiated re-approval)."""
        ...


# Fault sites already reported. A set (not a bool) so a broken LOAD does not
# silence a broken WRITE: they are different operator problems.
_FAULT_LOGGED: set[str] = set()


def _note_fault(where: str, exc: BaseException) -> None:
    """Report a persistence fault ONCE per site, then stay quiet.

    WHY ONCE: pinning runs per listed tool on every relay request; a broken
    database would otherwise emit a warning per tool per turn. WHY AT ALL: the
    operator has to know the control degraded to a one-restart memory.
    """
    if where in _FAULT_LOGGED:
        log.debug("tool_pin: pin persistence %s failed again (already degraded): %r", where, exc)
        return
    _FAULT_LOGGED.add(where)
    log.warning(
        "tool_pin: pin persistence %s failed - tool-definition pins stay IN MEMORY for this process"
        " (a restart re-approves the served definitions): %r",
        where,
        exc,
        exc_info=exc,
    )


def _epoch(value: Any) -> float:
    """Wall-clock epoch seconds from a TIMESTAMPTZ value (0.0 when unusable).

    asyncpg hands back an AWARE datetime for TIMESTAMPTZ, so .timestamp() is
    absolute. A naive value (a fake row, hand SQL) is read as local time, which
    is only ever used to AGE a pin: a skew there costs at most one TTL window,
    never a wrong verdict.
    """
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _row_to_pin(row: Any) -> PersistedPin | None:
    """One result row -> PersistedPin, or None when the row is unreadable.

    A malformed row must not take the whole load down: the other rows are still
    approvals worth honouring, so it is skipped and logged at DEBUG.
    """
    try:
        return PersistedPin(
            server=str(row["server"]),
            tool=str(row["tool"]),
            fingerprint=str(row["fingerprint"]),
            first_seen=_epoch(row["first_seen"]),
        )
    except Exception:
        log.debug("tool_pin: skipping an unreadable tool_pins row: %r", row, exc_info=True)
        return None


def _rows_to_pins(rows: Any) -> list[PersistedPin]:
    """Convert result rows, dropping the unreadable ones (see _row_to_pin)."""
    return [pin for pin in (_row_to_pin(row) for row in rows or ()) if pin is not None]


# Fire-and-forget write tasks. Strong references are REQUIRED: asyncio keeps
# only weak ones, so an unreferenced task can be garbage-collected mid-flight
# (the "Task was destroyed but it is pending" class of bug).
_WRITE_TASKS: set[asyncio.Task[Any]] = set()


def _write_done(task: asyncio.Task[Any]) -> None:
    """Log a failed background write once; never raise into the loop."""
    _WRITE_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _note_fault("write", exc)


class _DbPinBackend:
    """The gateway's tool_pins backend: one blocking read, fire-and-forget writes.

    THE READ (load) must finish BEFORE the first verdict (see the block comment
    above) and observe() is synchronous, so it cannot be awaited on the caller's
    loop. It runs on a short-lived helper thread that opens its OWN connection:
    the shared pool is bound to the app's event loop, so a pool fetch from
    another thread raises "attached to a different loop", while a standalone
    asyncpg.connect() is loop-local and safe there. Cost: one connect plus one
    SELECT, ONCE per process (a handful of milliseconds in-cluster).

    THE WRITES must never block the caller: they are scheduled as tasks on the
    caller's loop and use the shared pool (no extra connection, no thread). A
    write that cannot be scheduled (no running loop -- a synchronous test) is
    dropped, which is the in-memory behaviour, not an error.

    A store whose load fails disables writes, so this class never has to guess
    whether it has read the approval it might otherwise overwrite.
    """

    def load(self) -> list[PersistedPin]:
        """Read every pin; raise on fault or timeout (the caller degrades)."""
        pins: list[PersistedPin] = []
        fault: BaseException | None = None

        def _target() -> None:
            nonlocal pins, fault
            try:
                pins = asyncio.run(self._aload())
            except BaseException as exc:  # re-raised to the caller (load() contract)
                fault = exc

        worker = threading.Thread(target=_target, name="tool-pin-load", daemon=True)
        worker.start()
        worker.join(LOAD_TIMEOUT_S)
        if worker.is_alive():
            # Daemon thread: it finishes (or dies) on its own; the process must
            # not wait for a wedged connection.
            raise TimeoutError(f"tool pin load did not finish within {LOAD_TIMEOUT_S}s")
        if fault is not None:
            raise fault
        return pins

    @staticmethod
    async def _aload() -> list[PersistedPin]:
        """Read every pin on a THROWAWAY connection (the caller's loop is busy).

        The DSN comes from Database._connect_kwargs() rather than being resolved
        again here: it is the one place that validates URL-mode vs discrete-mode
        configuration (fail-loud), and a second copy would drift.
        """
        conn = await asyncpg.connect(timeout=LOAD_TIMEOUT_S, **Database._connect_kwargs())
        try:
            return _rows_to_pins(await conn.fetch(_SELECT_ALL_SQL))
        finally:
            await conn.close()

    def save(self, server: str, tool: str, fingerprint: str) -> None:
        self._write(_SAVE_SQL, server, tool, fingerprint)

    def touch(self, server: str, tool: str, fingerprint: str) -> None:
        self._write(_TOUCH_SQL, server, tool, fingerprint)

    def clear(self) -> None:
        self._write(_CLEAR_SQL)

    @staticmethod
    def _write(sql: str, *args: Any) -> None:
        """Schedule ONE statement on the caller's loop; never block, never raise."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.debug("tool_pin: pin write dropped - no running event loop (in-memory pinning)")
            return
        try:
            task = loop.create_task(db.execute(sql, *args))
        except Exception as exc:  # pragma: no cover - a loop closed under us
            _note_fault("write", exc)
            return
        _WRITE_TASKS.add(task)
        task.add_done_callback(_write_done)


_DB_BACKEND: PinBackend | None = None
_BACKEND_LOCK = threading.Lock()


def default_backend() -> PinBackend | None:
    """The process-wide DB backend, or None when this process has no database.

    GATED ON db.pool, not on a settings knob, deliberately:
      * the gateway cannot serve a request without its database (db.connect() is
        fail-loud at startup), so "a pool exists" is exactly "this is the real
        gateway" -- persistence is a CONTROL, and a control must not have a
        switch an operator can silently leave off;
      * the unit suite runs with db.pool None, so PinStore() in a test stays
        byte-identical to the pre-persistence store: no connection attempt, no
        schema dependency, and no pins leaking between tests through a table.
    The positive result is cached (one backend per process); a None is NOT, so a
    store constructed after db.connect() still resolves one. (A store that
    already LOADED while the pool was absent stays memory-only for its LIFETIME
    and awarm() cannot rescue it — awarm() returns early for an already-loaded
    store, which is the opposite of what an earlier version of this comment
    claimed (corrected 2026-11-23 after cross-review). Restart the process, or
    build a fresh store after the pool exists.)
    """
    global _DB_BACKEND
    if _DB_BACKEND is not None:
        return _DB_BACKEND
    if getattr(db, "pool", None) is None:
        return None
    with _BACKEND_LOCK:
        if _DB_BACKEND is None:
            _DB_BACKEND = _DbPinBackend()
        return _DB_BACKEND


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
    """Thread-safe (server, tool) -> fingerprint store, durable when it can be.

    TTL: `ttl_s` (seconds; 0 = never) is measured from when a fingerprint was
    RECORDED, not from its last observation. A continuously-observed tool
    therefore re-approves after the TTL, which is the point of a TTL -- a pin
    that could only expire on silence would never expire in a busy process.
    An expired entry is dropped and the next observation is FIRST_SEEN again.

    PERSISTENCE (2026-11-23, recommendation B2): a store loads the tool_pins
    table on FIRST USE, so a pin an EARLIER process approved is a MATCH, not a
    FIRST_SEEN -- a restart no longer re-approves the definitions being served.
    It writes through on first observation and (bounded) on MATCH, and clears
    the table on reset(). Pass `backend` to inject one (tests); omit it for the
    process-wide DB backend, which is None when this process has no database
    (the unit suite, or a store used before startup) -- the store then behaves
    exactly as the in-memory store it started as.
    """

    def __init__(self, ttl_s: float = 0.0, backend: PinBackend | None = None) -> None:
        self._lock = threading.Lock()
        # Serialises the ONE load. It cannot be _lock: a backend read is I/O and
        # must not be held against every observation in the process.
        self._load_lock = threading.Lock()
        # (server, tool) -> (fingerprint, recorded_at: time.monotonic())
        self._pins: dict[tuple[str, str], tuple[str, float]] = {}
        # (server, tool) -> monotonic time of that key's last last_seen write.
        self._touched: dict[tuple[str, str], float] = {}
        self.ttl_s = ttl_s
        self._backend = backend
        self._loaded = False
        self._persist_ok = True

    # --- persistence ------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load persisted pins ONCE, before the first verdict. A bool when done."""
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            backend = self._backend if self._backend is not None else default_backend()
            rows: list[PersistedPin] = []
            if backend is not None:
                try:
                    rows = backend.load()
                except Exception as exc:
                    # A store that could not READ the approvals must not WRITE:
                    # a FIRST_SEEN save would overwrite the very digest the failed
                    # load was supposed to protect (rule 1 in the block comment).
                    self._persist_ok = False
                    _note_fault("load", exc)
            if backend is None:
                self._persist_ok = False
            self._backend = backend
            self._absorb(rows)
            self._loaded = True

    def _absorb(self, rows: list[PersistedPin]) -> None:
        """Merge persisted pins into memory, converting first_seen to an AGE.

        The monotonic clock restarts with the process, so a row's first_seen is
        turned into "how long ago was this approved" and the in-memory
        recorded_at is back-dated by that age: the TTL then keeps counting across
        restarts instead of restarting with them.

        An observation this process already made WINS over the row (it is newer).
        That cannot normally happen -- the load precedes every observation -- but
        the merge must not depend on that ordering.
        """
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            for row in rows:
                key = (str(row.server), str(row.tool))
                if key in self._pins:
                    continue
                first_seen = float(row.first_seen or 0.0)
                # An unreadable (0.0) first_seen means "age unknown": treat the
                # pin as fresh rather than as infinitely old, so a bad row cannot
                # silently disable the defence it carries.
                # max(): clock skew, or a row from the future.
                age = max(now_wall - first_seen, 0.0) if first_seen > 0.0 else 0.0
                self._pins[key] = (str(row.fingerprint), now_mono - age)

    def _persist_save(self, key: tuple[str, str], fingerprint: str) -> None:
        """Write a FIRST_SEEN through to the backend (best-effort)."""
        backend = self._backend
        if backend is None or not self._persist_ok:
            return
        try:
            backend.save(key[0], key[1], fingerprint)
        except Exception as exc:
            _note_fault("save", exc)

    def _persist_touch(self, key: tuple[str, str], fingerprint: str) -> None:
        """Refresh last_seen for a MATCH (best-effort; bounded by the caller)."""
        backend = self._backend
        if backend is None or not self._persist_ok:
            return
        try:
            backend.touch(key[0], key[1], fingerprint)
        except Exception as exc:
            _note_fault("touch", exc)

    async def awarm(self) -> None:
        """Load persisted pins on the CURRENT loop (pool-backed). OPTIONAL.

        The synchronous path (_ensure_loaded) pays one blocking round trip on the
        first relay request of a process. A gateway that awaits this after
        db.connect() -- in app.py's lifespan, next to the housekeeping start --
        pays it at boot instead. WIRED (2026-11-23): app.py's lifespan awaits
        this immediately after db.connect(), so the deployed gateway loads at
        boot and the sync path is only the fallback for a caller that skipped
        it. Both produce the same store.

        Idempotent and best-effort: a fault degrades to in-memory exactly like
        the sync path, and an already-loaded store returns immediately.
        """
        if self._loaded:
            return
        if self._backend is not None:
            # An injected backend owns the read; there is no pool to consult.
            self._ensure_loaded()
            return
        rows: list[PersistedPin] = []
        fault: Exception | None = None
        try:
            rows = _rows_to_pins(await db.fetch(_SELECT_ALL_SQL))
        except Exception as exc:
            fault = exc
        with self._load_lock:
            if self._loaded:
                return
            backend = default_backend()
            if backend is None or fault is not None:
                self._persist_ok = False
            if fault is not None:
                _note_fault("load", fault)
            self._backend = backend
            self._absorb(rows)
            self._loaded = True

    # --- observation ------------------------------------------------------

    def observe(self, server: str, tool: str, fp: str) -> PinVerdict:
        """Record/compare ONE observation. The ONLY mutator. Thread-safe."""
        key = (str(server), str(tool))
        current = str(fp)
        self._ensure_loaded()
        now = time.monotonic()
        write = ""
        with self._lock:
            recorded = self._pins.get(key)
            if recorded is not None and self._expired(recorded[1], now):
                del self._pins[key]
                recorded = None
            if recorded is None:
                self._pins[key] = (current, now)
                # The save IS this key's last_seen write, so a MATCH right after
                # it must not pay a second one.
                self._touched[key] = now
                verdict = PinVerdict(FIRST_SEEN, "", current)
                write = "save"
            elif recorded[0] == current:
                verdict = PinVerdict(MATCH, recorded[0], current)
                last = self._touched.get(key)
                if last is None or (now - last) >= TOUCH_INTERVAL_S:
                    self._touched[key] = now
                    write = "touch"
            else:
                # CHANGED: the APPROVED fingerprint STAYS on record. Re-pinning
                # here would let a rug-pull approve itself on the next listing
                # (change once, then MATCH forever) -- exactly the bypass this
                # store exists to prevent. An operator re-approves by restarting
                # the process (or via reset()), never by the server changing again.
                verdict = PinVerdict(CHANGED, recorded[0], current)
        # Persistence happens OUTSIDE the lock: a backend call (a DB round trip in
        # production) must never serialise every observation in the process
        # behind it. The store's own state is already consistent.
        if write == "save":
            self._persist_save(key, current)
        elif write == "touch":
            self._persist_touch(key, current)
        return verdict

    def pinned(self, server: str, tool: str) -> str:
        """The fingerprint on record for one tool, or "" (also when expired)."""
        self._ensure_loaded()
        with self._lock:
            recorded = self._pins.get((str(server), str(tool)))
            if recorded is None or self._expired(recorded[1], time.monotonic()):
                return ""
            return recorded[0]

    def pinned_keys(self) -> list[tuple[str, str]]:
        """Every (server, tool) with a LIVE pin. Snapshot; expired entries drop.

        Used by the offer pass to notice a pinned tool that is ABSENT from the
        listing it should have appeared in (the vanished-tool bypass). With
        persistence this also lists pins an EARLIER process approved, which is
        the point: "rewrite it, then omit it, then wait for a restart" stops
        being a bypass.
        """
        self._ensure_loaded()
        now = time.monotonic()
        with self._lock:
            return [key for key, recorded in self._pins.items() if not self._expired(recorded[1], now)]

    def reset(self) -> None:
        """Forget every pin (tests, and an operator-initiated re-approval).

        With persistence this clears the TABLE too: a re-approval that only
        survived until the next restart would resurrect the digest the operator
        just retired.

        DEGRADED MODE IS REFUSED, LOUDLY (2026-11-23, cross-review): when the
        process could not READ the approvals (a boot-time load fault — the very
        scenario this persistence exists for), clearing the table would destroy
        records it never saw. So the clear is skipped and a WARNING is logged;
        the in-memory reset still takes effect. The operator must know the table
        was not cleared, because otherwise their re-approval is silently inert
        until the next healthy boot.
        """
        with self._lock:
            self._pins.clear()
            self._touched.clear()
        backend = self._backend if self._backend is not None else default_backend()
        if backend is None:
            return
        if not self._persist_ok:
            log.warning(
                "tool pin reset did NOT clear the tool_pins table: persistence is degraded "
                "(the boot load failed), so the stored approvals were never read. The "
                "in-memory reset took effect; the table still holds the retired digests and "
                "they will be adopted on the next healthy boot — clear the table by hand if "
                "that is not what you want.",
            )
            return
        try:
            backend.clear()
        except Exception as exc:
            _note_fault("clear", exc)

    def __len__(self) -> int:
        self._ensure_loaded()
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
