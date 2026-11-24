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
   and DSN userinfo, plus a bounded SECOND pass that catches a credential
   HARD-WRAPPED across a line break in its short-head shape (2026-11-23; see
   _WRAP_HEAD_RULES for exactly what it fires on and what it still misses). The
   continuation guard was NARROWED the same day: a log level must be followed by
   a space or a colon and a date by `T`/space to count as a stacked-log record,
   so `ERROR-...` and bare `2026-...` continuations are redacted rather than
   exempted.
   It reduces blast radius; it is NOT a guarantee.
3. TRUNCATION IS RECORDED (`prepare`). A payload is capped at
   trace_payload_max_chars and `payload_chars` keeps the length BEFORE the
   cut, so a reader can tell a short result from a truncated one.
4. RETENTION IS OPERATOR-OPTED (`prune`), 0 = keep until manually pruned,
   called by the EXISTING housekeeping sweep (never a second sweeper).

DISCIPLINE: the same one core/traces.py documents — every write here is
BEST-EFFORT and never raises. A broken payload pipeline is a gap in the
quality layer, never a failed request.

COVERAGE (restated 2026-11-23, A4 — all 10 tool-phase sites feed this store):
every tool-phase `_trace_step` call site in core/toolrelay.py now passes what
it has. COUNT CORRECTION: earlier drafts of this paragraph said "13 tool-phase
sites, one fed" — that was a grep artefact (the pattern also matched the
`async def _trace_step` definition). The module has 12 call sites: 10 are
phase="tool" and all 10 are fed, 2 are phase="chat" and correctly carry no
payload (the model-call settle and the mid-loop budget marker are not tool
steps, and the hook below fires only for phase == "tool").

The fed tool sites, and what each one contributes:
  * server-executed MCP execution — arguments + result (the original site);
  * ACL denial — arguments + the refusal text (the headline red-team gap);
  * dispatch-time tool-pin refusal — arguments + the vanished/changed text;
  * unknown tool — the raw argument JSON + the error fed back;
  * invalid-arguments JSON — the MALFORMED argument text verbatim + the error;
  * compressed-result retrieval (pcai__retrieve) — args + slice/unknown-digest;
  * Wave B client-tool dispatch — arguments + the executor's result;
  * client loop-guard denial — the parsed call + the guard's reason;
  * the two handback sites (pure-client batch, twin-rewritten mixed batch) —
    the model's raw argument JSON ONLY: the client executes after the turn, so
    no result exists at trace time and none is invented.
So "captured completely or not at all" (rule 1) now holds across TOOL steps:
what the agent asked for when refused is stored, not just what it did when
allowed. The two chat-phase steps remain payload-free by design.

The coverage is ENFORCED, not just claimed: tests/test_trace_payload_wiring.py
walks the relay's AST and fails unless every tool-phase `_trace_step` site
passes `tool_args` or appears in an explicit, commented allowlist (currently
empty — every site has its arguments in scope). A new unwired tool-phase site
is therefore a test failure, which is how the old 1-of-10 gap survived: the
previous guard only asked for "at least one" fed site.
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
# The (?!\d+(?:[\s\"',;&}\[\]]|$)) guard spares a COUNT from being treated as a
# credential: a config dump says "token: 512" / "api_key: 4096" and redacting
# those mangles the very diagnostics the payload table exists to serve. Added
# 2026-11-23 after cross-review found the digit exception existed only in the
# wrapped-continuation pass while the one-line rules — the common shape — still
# ate it. "password" deliberately keeps no guard: a numeric password is real.
_COUNT_NOT_CREDENTIAL = r"(?!\d+(?:[\s\"',;&}\[\]]|$))"
_API_KEY_KV = re.compile(
    r"(?i)((?:api[_-]?key|apikey)[\"']?\s*[:=]\s*[\"']?)" + _COUNT_NOT_CREDENTIAL + r"([^\s\"',;&}\[\]]+)"
)

# token / access_token / refresh_token / client_secret as a key-value pair.
_SECRET_KV = re.compile(
    r"(?i)((?:client[_-]?secret|secret|refresh[_-]?token|access[_-]?token|token)[\"']?\s*[:=]\s*[\"']?)"
    + _COUNT_NOT_CREDENTIAL
    + r"([^\s\"',;&}\[\]]+)"
)

# AWS secret access keys (2026-11-23). The generic _SECRET_KV cannot see these:
# "secret" is followed by "_access_key=" so the ["']?\s*[:=] anchor never fires.
_AWS_SECRET = re.compile(
    r"(?i)((?:aws[_-]?)?secret[_-]?access[_-]?key[\"']?\s*[:=]\s*[\"']?)"
    + _COUNT_NOT_CREDENTIAL
    + r"([^\s\"',;&}\[\]]+)"
)

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

# --- hard-wrap continuations (B9, 2026-11-23) ------------------------------
# A credential HARD-WRAPPED across a line break defeats every rule above: they
# are single-line, so a terminal or log wrapping at column 76 turns one 60-char
# bearer token into "Bearer abc" + newline + "defgh..." - the head is under the
# 8-char floor and the tail carries no keyword anchor, and BOTH fragments are
# stored raw (the exact defeat the 2026-11-23 cross-review demonstrated).
#
# This second pass closes the SHORT-HEAD shape WITHOUT re-joining any lines.
# Re-joining is what mangles structured payloads (a JSONL record welded to the
# next one, two CSV rows merged, stacked log lines turned into one sentence),
# so the heuristic NEVER rewrites a line boundary: it redacts the partial value
# at the END of a line and the fragment at the START of the NEXT line, leaving
# both newlines and every other byte in place.
#
# WHAT FIRES (all four must hold):
#   1. the line ends with a credential anchor plus a value that CANNOT be a
#      complete credential for its class - under the 8-char floor for
#      Bearer/Basic, or at most WRAP_KV_FRAGMENT_MAX chars for a key=value
#      class (password/token/secret/api_key/cookie) whose single-line rule has
#      no floor of its own;
#   2. the next line STARTS with a credential-charset run of at least
#      WRAP_TAIL_MIN_CHARS chars;
#   3. head + fragment together are long enough for a real credential (the
#      same per-class floor), so "password=ab" + "12" is left alone;
#   4. the next line does not START like a real new record - a log level
#      followed by a SPACE or a COLON (`ERROR `, `INFO:`), a line that is JUST
#      the level, or an ISO date followed by `T`/space (`2026-11-23T12:00`,
#      `2026-11-23 12:00`). That is a stacked log, not a continuation.
#      NARROWED 2026-11-23 (R4): the first
#      version accepted a log level at a WORD BOUNDARY and a bare `YYYY-MM-DD`,
#      so `ERROR-xyz...` and a line beginning `2026-` both read as records and
#      BOTH credential fragments were stored raw - the exact shapes the
#      cross-review used to defeat the pass. A record whose level ends in a
#      hyphen is not a record shape any logger emits; a credential fragment
#      that happens to begin with a date prefix is far more likely.
# The partial head value and the leading fragment then BOTH become the typed
# marker: "Bearer abc" / "defghijklmnop" -> "Bearer [REDACTED:bearer]" /
# "[REDACTED:bearer]".
#
# WHAT IT STILL DOES NOT CATCH (stated so nobody mistakes it for a guarantee):
#   * a wrap whose head value is ALREADY a full-length credential (a 40-char
#     token wrapped after 20): the single-line rule redacts the head and the
#     tail fragment stays raw, because a long dangling value at EOL is
#     indistinguishable from an ordinary word;
#   * a continuation starting with punctuation ("!x9", "=abc", "..."): the
#     fragment charset is the same one the single-line rules use;
#   * DSN userinfo split across lines (the "@host" anchor is on the other
#     line, so no rule can see a credential there);
#   * a wrap longer than ONE line break (the fragment continues onto a third
#     line), and any credential in a shape the single-line rules miss.
# THE NARROWED RECORD GUARD IS A DELIBERATE TRADE (2026-11-23, R4): a stacked
# log whose level is followed by anything other than a space/colon (`ERROR-`,
# `INFO|`) or whose date has no time after it is now read as a CONTINUATION, so
# such a line can be redacted as a credential fragment. That costs a redacted
# log line in an exotic format; leaving the guard broad cost a stored
# credential in the shape a reviewer actually produced. The credential wins.
# It can still MISFIRE on prose that ends in an anchor word plus a short word
# followed by a long one ("the bearer of" + "goodness"): the per-class joined
# length floor makes that much rarer but cannot remove it, because a wrapped
# short credential and a sentence have the same shape. The marker keeps the key
# ("Bearer", "password=") visible, so the damage is a redacted word, not a
# destroyed record. The sample rate and the retention window remain the real
# controls.

# A dangling head value longer than this is treated as COMPLETE - the
# single-line rules have already had their chance at it.
WRAP_SCHEME_FRAGMENT_MAX = 7  # one below the 8-char floor _BEARER/_BASIC use
WRAP_KV_FRAGMENT_MAX = 5
# A continuation shorter than this is not worth redacting and is far more
# likely to be the first word of ordinary prose.
WRAP_TAIL_MIN_CHARS = 3

# Cheap pre-filter: the pass only runs at all on text that mentions an anchor.
_WRAP_HINT_RE = re.compile(r"(?i)(?:bearer|basic|password|passwd|pwd|token|secret|api[_-]?key|apikey|cookie|sk-)")

# The next line leading credential-charset run: the wrapped fragment.
_WRAP_TAIL_RE = re.compile(r"^(?P<frag>[A-Za-z0-9\-._~+/]{" + str(WRAP_TAIL_MIN_CHARS) + r",})")

# A continuation that STARTS like a REAL record is not a fragment (rule 4).
# NARROWED 2026-11-23 (R4, cross-review): the old pattern accepted a log level
# at a WORD BOUNDARY (`\b` matches between "ERROR" and "-") and a bare
# `YYYY-MM-DD` prefix, so "Bearer abc" + newline + "ERROR-xyz..." and a line
# beginning "2026-..." were both read as stacked records and BOTH fragments were
# stored RAW. A real record follows its level with a SPACE or a COLON (or is the
# whole line) and its date with `T` or a space; those are the only shapes
# honoured here.
_RECORD_START_RE = re.compile(
    r"(?i)^(?:\d{4}-\d{2}-\d{2}[T ]|(?:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|FATAL|CRITICAL)(?:[ :]|$))"
)

_WRAP_SCHEME_VALUE = r"[A-Za-z0-9\-._~+/]{1," + str(WRAP_SCHEME_FRAGMENT_MAX) + "}"
_WRAP_KV_VALUE = r"[^\s\"\',;&}\[\]]{1," + str(WRAP_KV_FRAGMENT_MAX) + "}"

# Head rules: (pattern over the END of a line, marker kind, minimum joined
# length for a real credential). The named value group is what gets replaced.
_WRAP_HEAD_RULES: tuple[tuple[re.Pattern[str], str, int], ...] = (
    # Scheme-auth classes use a HIGHER joined floor (12): their head value is a
    # dangling word far more often than a credential fragment ("the bearer of"
    # + "goodness" is prose), and a real wrapped bearer token is far longer.
    (re.compile(r"(?i)\bbearer\s+(?P<value>" + _WRAP_SCHEME_VALUE + r")=*$"), "bearer", 12),
    (re.compile(r"(?i)\bbasic\s+(?P<value>" + _WRAP_SCHEME_VALUE + r")={0,2}$"), "basic", 12),
    (re.compile(r"\bsk-(?P<value>[A-Za-z0-9_\-]{1,9})$"), "api_key", 10),
    (
        re.compile(r"(?i)(?:api[_-]?key|apikey)[\"\']?\s*[:=]\s*[\"\']?(?P<value>" + _WRAP_KV_VALUE + r")$"),
        "api_key",
        8,
    ),
    (
        re.compile(
            r"(?i)(?:client[_-]?secret|secret|refresh[_-]?token|access[_-]?token|token)"
            r"[\"\']?\s*[:=]\s*[\"\']?(?P<value>" + _WRAP_KV_VALUE + r")$"
        ),
        "secret",
        8,
    ),
    (
        re.compile(
            r"(?i)(?:aws[_-]?)?secret[_-]?access[_-]?key[\"\']?\s*[:=]\s*[\"\']?"
            r"(?P<value>" + _WRAP_KV_VALUE + r")$"
        ),
        "aws_secret",
        20,
    ),
    (
        re.compile(
            r"(?i)(?:set-)?cookie\b[^\n=]{0,80}?(?:session|sess|sid|auth|token|jwt)"
            r"[A-Za-z0-9_-]*=(?P<value>[^;\s\"\']{1," + str(WRAP_KV_FRAGMENT_MAX) + r"})$"
        ),
        "cookie",
        8,
    ),
    (
        re.compile(r"(?i)(?:password|passwd|pwd)[\"\']?\s*[:=]\s*[\"\']?(?P<value>" + _WRAP_KV_VALUE + r")$"),
        "password",
        6,
    ),
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


def _redact_wrapped(text: str) -> str:
    """Redact the CONTINUATION of a credential hard-wrapped across a line break.

    One bounded pass over the line PAIRS; the block comment above
    _WRAP_HEAD_RULES states exactly what fires and what it still misses. Line
    boundaries are preserved - a rewrite never moves text between lines -
    which is what keeps JSONL records, CSV rows and stacked logs intact. Never
    raises: a redactor must not break a payload, so any fault returns the
    input unchanged.
    """
    if "\n" not in text or _WRAP_HINT_RE.search(text) is None:
        return text
    try:
        lines = text.split("\n")
        changed = False
        for index in range(len(lines) - 1):
            line = lines[index]
            head = line.removesuffix("\r")
            if _WRAP_HINT_RE.search(head) is None:
                continue
            continuation = lines[index + 1]
            fragment_match = _WRAP_TAIL_RE.match(continuation)
            if fragment_match is None or _RECORD_START_RE.match(continuation) is not None:
                continue
            fragment = fragment_match.group("frag")
            for pattern, kind, min_joined in _WRAP_HEAD_RULES:
                match = pattern.search(head)
                if match is None:
                    continue
                value = match.group("value")
                if value.isdigit() and kind != "password":
                    # A dangling ALL-DIGITS value under token/secret/api_key
                    # is a COUNT, not a truncated credential ("token: 512",
                    # "tokens: 4096" in a config dump). A numeric password is
                    # real, so that one class keeps the rule.
                    continue
                if len(value) + len(fragment) < min_joined:
                    continue
                marker = REDACT_MARKER.format(kind=kind)
                lines[index] = head[: match.start("value")] + marker + head[match.end("value") :]
                if line.endswith("\r"):
                    lines[index] += "\r"
                lines[index + 1] = marker + continuation[fragment_match.end("frag") :]
                changed = True
                break
        return "\n".join(lines) if changed else text
    except Exception:  # pragma: no cover - a redactor never breaks a payload
        return text


def redact(text: str) -> str:
    """Scrub credential-shaped substrings from `text`. PURE, best-effort.

    Recognised: `Authorization: Bearer <token>`, `Authorization: Basic <b64>`,
    bare `sk-...` api keys, `api_key`/`x-api-key` pairs, `token`/`secret` pairs,
    `aws_secret_access_key`, session cookies (`set-cookie: session=...`),
    `password`/`pwd` pairs, and DSN userinfo (`scheme://user:pass@host`). Each
    becomes a TYPED marker (`[REDACTED:bearer]`, `[REDACTED:api_key]`, ...) so a
    reader can see what class of secret was removed.

    HARD-WRAPPED CREDENTIALS (B9, 2026-11-23): a credential broken across a
    line break is now caught in its SHORT-HEAD shape. A terminal or log that
    wraps at column 76 turns one 60-char bearer token into "Bearer abc" +
    newline + "defgh..." - the head is under the 8-char floor and the tail
    carries no keyword anchor - and the second pass (_redact_wrapped) redacts
    BOTH fragments: "Bearer [REDACTED:bearer]" + newline + "[REDACTED:bearer]".
    It does that WITHOUT re-joining lines: the line boundary is preserved, so
    JSONL records, CSV rows and stacked log lines keep their shape. A next line
    counts as a stacked-log RECORD (and is left alone) only when it starts like
    a real one - a log level followed by a SPACE or a COLON (or alone on the
    line), or an ISO date followed by `T`/space; `ERROR-...` and a bare `2026-...` are treated as
    credential fragments since 2026-11-23 (R4), because they were the shapes
    that left both halves of a wrapped credential raw. The exact shapes that
    fire, and the ones that still do not, are listed in the block comment above
    _WRAP_HEAD_RULES - read it before relying on this.

    THIS IS NOT A GUARANTEE. It is pattern matching: a secret in an
    unrecognised shape (a bespoke header, an encoded token, a credential
    inside free prose, a wrap whose head already looks like a complete
    credential, a continuation starting with punctuation) passes through
    untouched. It reduces the blast radius of storing payloads at all; it does
    not make the table safe to publish. The sample rate and the retention
    window are the real controls.

    Idempotent: redact(redact(x)) == redact(x), because every value class
    excludes the brackets a marker is made of.
    """
    if not text:
        return text
    text = _redact_wrapped(text)
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
