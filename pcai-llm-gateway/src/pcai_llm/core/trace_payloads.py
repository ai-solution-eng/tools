"""Agent trace payloads: what the agent SAW (sampled tool args + results).

WHAT: `agent_traces` (core/traces.py) records what an agent DID — one row per
model iteration and per tool call, with status, latency and cost — but never
WHAT IT SAW: the arguments a tool was called with and the result that came
back. Without those there is no replay, no judge, no red-team set and no
training data, so the quality layer and the traces-to-training loop are both
blocked on this table. This module is the storage half.

THE FOUR RULES, each its own testable function:

1. SAMPLING IS PER REQUEST, NOT PER ROW (`sampled`). A request is captured
   COMPLETELY or not at all. A per-row coin flip would keep the arguments of
   a call and drop its result, which is a trace no judge can read. The
   decision is a hash of the request_id, so it is stable across replicas and
   restarts and monotone in the rate: a request sampled at 0.1 is still
   sampled at 0.5.
2. REDACTION IS A PURE FUNCTION (`redact`). Best-effort pattern matching over
   bearer/basic tokens, api keys, aws secret keys, session cookies, passwords
   and DSN userinfo. It reduces blast radius; it is NOT a guarantee — a
   credential HARD-WRAPPED across a line break is stored raw (see redact()'s
   docstring for the demonstrated defeat).
3. TRUNCATION IS RECORDED (`prepare`). A payload is capped at
   trace_payload_max_chars and `payload_chars` keeps the length BEFORE the
   cut, so a reader can tell a short result from a truncated one.
4. RETENTION IS OPERATOR-OPTED (`prune`), 0 = keep until manually pruned,
   called by the EXISTING housekeeping sweep (never a second sweeper).

DISCIPLINE: the same one core/traces.py documents — every write here is
BEST-EFFORT and never raises. A broken payload pipeline is a gap in the
quality layer, never a failed request.

COVERAGE (stated precisely, 2026-11-23 cross-review): rule 1 holds for the
calls that reach this module, and today that is the SERVER-EXECUTED MCP path —
the one `_trace_step` call site in the relay that has both the arguments and
the result in scope. The other tool-phase trace steps (ACL denial, pin
refusal, unknown tool, invalid arguments, client dispatch, handbacks) write a
trace row and NO payload, because they do not go through this hook. So a
red-team set built from this table systematically under-samples REFUSALS: it
shows what the agent did when allowed, not what it asked for when refused.
Widening the hook to those sites is the obvious next step and is deliberately
NOT done here — each site needs its own review, and a half-wired payload
pipeline that silently drops refusals is worse than one that says so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

# Salted so the bucket for a request_id is stable but cannot be computed from
# another component's hash of the same id (and is versioned, so changing the
# sampling scheme is an explicit act with an observable seam).
_SAMPLE_SALT = "pcai-llm/trace-payload/v1"

# The typed marker every redaction leaves behind: a reader can see WHAT class
# of secret was removed without seeing it.
REDACT_MARKER = "[REDACTED:{kind}]"

# --- redaction rules -------------------------------------------------------
# Each entry is (compiled pattern, replacement). The replacement keeps the KEY
# (so the payload still says "this was an Authorization header") and replaces
# only the secret. Every value class below excludes [ and ], which is what
# makes redact() IDEMPOTENT: an existing marker can never be re-matched.

# Authorization: Bearer <token>. The token charset is RFC 6750 tchar-ish; a
# value shorter than 8 chars is not a usable credential and is left alone so
# prose ("bearer of good news") is not mangled.
_BEARER = re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9\-._~+/]{8,}=*)")

# Authorization: Basic <base64>. Added 2026-11-23 (cross-review P2): Basic is the
# DEFAULT auth of half the tool ecosystem and was passing through untouched.
_BASIC = re.compile(r"(?i)\b(basic)(\s+)([A-Za-z0-9+/]{8,}={0,2})")

# scheme://user:pass@host. The userinfo is replaced wholesale: a username in a
# DSN is a credential-shaped identifier too, and a marker that keeps the
# username invites "half the secret is still there". The username may be empty
# (redis://:password@host) and the password may itself contain a colon.
_DSN_USERPASS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@:\[\]]*:[^\s/@\[\]]+@")

# scheme://<long-token>@host — a credential with no password part (a PAT in a
# clone URL). The 16-char floor keeps an ordinary "https://host/path@user" from
# being mangled: a real PAT is far longer, a path fragment is not.
_DSN_TOKEN = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[A-Za-z0-9_.\-]{16,}@")

# OpenAI-style bare keys: sk-..., sk-proj-.... No key= prefix to anchor on, so
# the shape is the anchor.
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}")

# api_key / api-key / apikey / x-api-key as a key-value pair (JSON, header,
# query string). "x-api-key" matches at its api-key tail, leaving the "x-"
# prefix in place — the shape still reads correctly.
_API_KEY_KV = re.compile(r"(?i)((?:api[_-]?key|apikey)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&}\[\]]+)")

# token / access_token / refresh_token / client_secret as a key-value pair.
_SECRET_KV = re.compile(
    r"(?i)((?:client[_-]?secret|secret|refresh[_-]?token|access[_-]?token|token)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"',;&}\[\]]+)"
)

# AWS secret access keys (2026-11-23). The generic _SECRET_KV cannot see these:
# "secret" is followed by "_access_key=" so the ["']?\s*[:=] anchor never fires.
_AWS_SECRET = re.compile(r"(?i)((?:aws[_-]?)?secret[_-]?access[_-]?key[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&}\[\]]+)")

# Session cookies (2026-11-23): the credential class most often echoed by an HTTP
# tool result. Anchored on the cookie header AND a session-ish name, so an ordinary
# "cookie: oatmeal" is not mangled.
_COOKIE = re.compile(
    r"(?i)((?:set-)?cookie\b[^\n=]{0,80}?(?:session|sess|sid|auth|token|jwt)[A-Za-z0-9_-]*=)([^;\s\"']{8,})"
)

# password / passwd / pwd as a key-value pair. Short values are redacted too:
# "pw=abc" is a credential, not a word.
_PASSWORD_KV = re.compile(r"(?i)((?:password|passwd|pwd)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&}\[\]]+)")

# ORDER MATTERS: bearer before the generic key/value rules (a bearer token is
# never also a "token=" value), and the DSN rule before the generic ones (a
# DSN carries no "password=" keyword, but its password may contain one).
_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_BEARER, r"\1\2" + REDACT_MARKER.format(kind="bearer")),
    (_BASIC, r"\1\2" + REDACT_MARKER.format(kind="basic")),
    (_DSN_USERPASS, r"\1" + REDACT_MARKER.format(kind="dsn") + "@"),
    (_DSN_TOKEN, r"\1" + REDACT_MARKER.format(kind="dsn") + "@"),
    (_SK_TOKEN, REDACT_MARKER.format(kind="api_key")),
    (_API_KEY_KV, r"\1" + REDACT_MARKER.format(kind="api_key")),
    (_SECRET_KV, r"\1" + REDACT_MARKER.format(kind="secret")),
    (_AWS_SECRET, r"\1" + REDACT_MARKER.format(kind="aws_secret")),
    (_COOKIE, r"\1" + REDACT_MARKER.format(kind="cookie")),
    (_PASSWORD_KV, r"\1" + REDACT_MARKER.format(kind="password")),
)

_INSERT_SQL = """
INSERT INTO agent_trace_payloads
    (request_id, iteration, phase, server, tool, kind, payload, payload_chars,
     truncated, redacted)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""

# ts-leading by design: the retention prune must never seq-scan this table
# (same reasoning as idx_traces_ts in db/schema.py).
_PRUNE_SQL = "DELETE FROM agent_trace_payloads WHERE ts < now() - make_interval(days => $1)"


@dataclass(frozen=True)
class Prepared:
    """One payload, ready to store: the exact values the row carries.

    `payload_chars` is the length of the payload AS IT ARRIVED (before
    redaction and before truncation), so it is the honest size of what the
    agent saw. `payload` is the bounded, scrubbed text actually stored.
    """

    payload: str
    payload_chars: int
    truncated: bool
    redacted: bool


def redact(text: str) -> str:
    """Scrub credential-shaped substrings from `text`. PURE, best-effort.

    Recognised: `Authorization: Bearer <token>`, `Authorization: Basic <b64>`,
    bare `sk-...` api keys, `api_key`/`x-api-key` pairs, `token`/`secret` pairs,
    `aws_secret_access_key`, session cookies (`set-cookie: session=...`),
    `password`/`pwd` pairs, and DSN userinfo (`scheme://user:pass@host`). Each
    becomes a TYPED marker (`[REDACTED:bearer]`, `[REDACTED:api_key]`, ...) so a
    reader can see what class of secret was removed.

    THIS IS NOT A GUARANTEE. It is pattern matching: a secret in an
    unrecognised shape (a bespoke header, a split/encoded token, a credential
    inside free prose) passes through untouched. It reduces the blast radius
    of storing payloads at all; it does not make the table safe to publish.
    The sample rate and the retention window are the real controls.

    KNOWN DEFEAT, demonstrated 2026-11-23: a credential HARD-WRAPPED across a
    line break defeats every rule whose value charset is a single line. A
    terminal or log that wraps at column 76 turns one 60-char bearer token into
    "Bearer abc" + newline + "defgh..." — the head is under the 8-char floor and
    the tail carries no keyword anchor, so BOTH fragments are stored raw. The
    same is true of "password=hun" + newline + "ter2". Re-joining continuations
    before matching is deliberately NOT done here: it would need a tokenizer
    guess about which newline was a wrap and which was a record boundary, and a
    wrong guess mangles structured payloads (JSONL, CSV, stacked log lines).
    Treat wrapped secrets as OUT OF SCOPE for redaction and let the retention
    window bound the exposure.

    Idempotent: redact(redact(x)) == redact(x), because every value class
    excludes the brackets a marker is made of.
    """
    if not text:
        return text
    for pattern, replacement in _REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


def sampled(request_id: str, rate: float | None = None) -> bool:
    """Is THIS REQUEST captured? Deterministic in (request_id, rate).

    The sampling decision is per REQUEST, never per row: every payload of a
    sampled turn is stored, so a judge reads a complete trace instead of a
    random subset of one. The bucket is a hash of the request_id, so the same
    request decides the same way on every replica and after every restart,
    and raising the rate only ever ADDS requests (bucket < rate is monotone).

    rate <= 0 disables capture (the shipped default); rate >= 1 captures
    everything.
    """
    if rate is None:
        rate = settings.trace_payload_sampling
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(f"{_SAMPLE_SALT}:{request_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < rate


def canonical(value: Any) -> str:
    """The payload as text: strings pass through, everything else is JSON.

    sort_keys makes the stored text independent of dict insertion order (the
    same arguments captured twice serialise identically), default=str keeps a
    non-JSON-native value from raising on the hot path. Mirrors
    core.effective._canon.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - default=str covers it
        return str(value)


def prepare(text: str, *, max_chars: int | None = None, redact_enabled: bool | None = None) -> Prepared:
    """Bound + scrub ONE payload for storage. PURE (no settings mutation, no I/O).

    Order is redact-then-truncate, and `payload_chars` is measured on the
    text as it arrived:
      * redaction runs on at most `max_chars` of input, because nothing past
        the cap is ever stored and a 350 KB tool result must not cost a
        350 KB regex scan on the relay path;
      * truncation runs again afterwards, because a marker can be LONGER than
        the secret it replaces ("pw=a" -> "[REDACTED:password]"), so redaction
        can push the text back over the cap.

    `truncated` is TRUE whenever any byte was dropped, and `payload` is a
    pure prefix of the (scrubbed) payload — the flag and `payload_chars` are
    what tell a reader the text was cut, so no marker is spliced into the
    stored text itself. max_chars = 0 keeps a metadata-only row.
    """
    if max_chars is None:
        max_chars = settings.trace_payload_max_chars
    if redact_enabled is None:
        redact_enabled = settings.trace_payload_redact
    max_chars = max(0, int(max_chars))
    payload_chars = len(text)
    head = text if payload_chars <= max_chars else text[:max_chars]
    stored = redact(head) if redact_enabled else head
    redacted = stored != head
    truncated = payload_chars > max_chars or len(stored) > max_chars
    if len(stored) > max_chars:
        stored = stored[:max_chars]
    return Prepared(payload=stored, payload_chars=payload_chars, truncated=truncated, redacted=redacted)


async def record_step(
    request_id: str,
    iteration: int,
    server: str,
    tool: str,
    *,
    args: Any = None,
    result: Any = None,
    phase: str = "tool",
) -> int:
    """Write the captured args/result rows for ONE relay step. BEST-EFFORT.

    Returns the number of rows written (0 when this request is not sampled,
    when both payloads are None, or when every write failed). Never raises:
    a payload write fault is a warning in the log and a gap in the quality
    layer, exactly the core.traces.record contract.

    Called from traces.record() once a tool step's trace row has landed, so a
    payload row can never be an orphan of a trace row that does not exist.
    """
    if not sampled(request_id):
        return 0
    written = 0
    for kind, value in (("args", args), ("result", result)):
        if value is None:
            continue
        try:
            prepared = prepare(canonical(value))
            await db.execute(
                _INSERT_SQL,
                request_id,
                int(iteration),
                phase,
                server,
                tool,
                kind,
                prepared.payload,
                prepared.payload_chars,
                prepared.truncated,
                prepared.redacted,
            )
            written += 1
        except Exception:
            log.warning(
                "agent trace payload write failed (request_id=%s iteration=%s kind=%s) — continuing",
                request_id,
                iteration,
                kind,
                exc_info=True,
            )
    return written


async def prune(days: int | None = None) -> int:
    """Delete payloads older than `days`; returns rows reclaimed.

    0 (or negative) disables reclamation — the operator contract shared with
    every other retention knob, and the reason this table ships with a 7-day
    default instead of "keep forever": it holds tool arguments and results,
    the most sensitive rows the gateway stores. Called by the EXISTING
    housekeeping sweep (core/housekeeping.py), never by a second loop.

    Raises on a DB fault; the housekeeping step is error-swallowed like every
    other one there (the embedcache.prune / toolcompress_store.prune shape).
    """
    if days is None:
        days = settings.trace_payload_retention_days
    if days <= 0:
        return 0
    tag = await db.execute(_PRUNE_SQL, int(days))
    try:
        return int(str(tag).split()[-1])
    except (ValueError, IndexError):
        return 0
