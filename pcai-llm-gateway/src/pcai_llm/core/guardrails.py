"""Content guardrails: per-model pre-call / post-call text policy engine.

WHY: the gateway fronts models it does not control, for tenants with
different compliance needs. Guardrails are named DB policies attached to
virtual model names (model_guardrails -> model_endpoints.model_name); each
request runs its model's pre_call guards over the user text (mutate or
refuse BEFORE the upstream call) and post_call guards over the assistant
text (mutate or refuse BEFORE the client sees it). Every guardrail yields
exactly ONE event per run — 'pass' included — so the ledger's
guardrail_events column tells the whole audit story next to the cost row.

Contract:

* ENFORCEMENT IS STRICT, DELIVERY IS FAIL-OPEN BY DESIGN. blocklist/length
  blocks raise GuardrailBlocked (pre_call) or return blocked=True
  (post_call); a webhook that is unreachable, erroring or non-JSON
  degrades to pass-with-event — a flaky moderator must never take the
  gateway down, and the failure stays visible in the event.
* Guardrails run in the given order and COMPOSE: each engine sees the
  output of the previous one (mask-then-scan semantics).
* Engines (config shapes on the engine helpers): blocklist (keyword
  deny/mask), regex_pii (email / phone / Luhn-valid credit cards), length
  (character caps), webhook (external moderator), secrets (credential-shaped
  strings: AWS keys, PEM private keys, JWTs, gateway/provider API keys,
  bearer tokens — MASK-ONLY, no reversible mapping).
* Bug fixes vs the reference pipeline this module ports: mask replaces
  EVERY occurrence of EVERY keyword (the reference stopped at the first
  hit); the PHONE pattern refuses to match inside a longer digit run
  (IDs, dates); card detection requires a Luhn-valid, separator-stripped
  digit string (the reference flagged any 13-16 digit run — phone numbers
  included); a webhook mask verdict is applied only when the request has
  exactly ONE non-empty user text part (the reference overwrote every
  user part with the same masked text).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx2

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

GUARDRAIL_MODES = ("pre_call", "post_call")
GUARDRAIL_ENGINES = ("blocklist", "regex_pii", "length", "webhook", "secrets")
MASK = "[REDACTED]"

# --- secrets engine patterns (2026-11-02) ----------------------------------
# Credential-shaped strings that must never leave the gateway or be echoed
# back by a model. Deliberately CONSERVATIVE: each pattern is anchored on a
# vendor prefix or a key header, so ordinary prose is not redacted. The
# engine is MASK-ONLY: no token->original mapping is stored, so a masked
# secret cannot be re-inflated (reversibility needs its own at-rest secret
# store and is a separate decision).
_SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "aws_secret_key": re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+=]{40}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "gateway_key": re.compile(r"\bhllm-[A-Za-z0-9]{16,}\b"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "bearer": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
}
SECRET_KINDS = tuple(_SECRET_PATTERNS)


def _secret_kinds_of(cfg: dict[str, Any]) -> tuple[str, ...]:
    """Which kinds this guardrail watches. Unknown names are ignored; an
    empty/absent list means ALL kinds (the safe default)."""
    raw = cfg.get("kinds")
    if isinstance(raw, list) and raw:
        picked = tuple(str(k) for k in raw if str(k) in _SECRET_PATTERNS)
        return picked or SECRET_KINDS
    return SECRET_KINDS


def _first_secret_hit(texts: list[str], kinds: tuple[str, ...]) -> str | None:
    """The FIRST kind found in any text (for block/flag verdicts), else None."""
    for text in texts:
        for kind in kinds:
            if _SECRET_PATTERNS[kind].search(text):
                return kind
    return None


def _mask_secrets(text: str, kinds: tuple[str, ...]) -> tuple[str, dict[str, int]]:
    """Replace every credential-shaped match with [REDACTED:<kind>]."""
    counts: dict[str, int] = {}
    for kind in kinds:
        pattern = _SECRET_PATTERNS[kind]

        def _sub(match: re.Match[str], _kind: str = kind) -> str:
            counts[_kind] = counts.get(_kind, 0) + 1
            return f"[REDACTED:{_kind}]"

        text = pattern.sub(_sub, text)
    return text, counts


# Audit cap for one run's event list (the ledger's guardrail_events JSONB).
# Enforcement is NEVER capped — only the recorded verdicts are.
_MAX_EVENTS = 50

# AUDIT-r5 #7 (P1 PERF): unbounded '+' on both sides of the @ made any
# no-match text drive quadratic backtracking — every start position
# re-scans the whole run (a 100k-char 'a' prompt, inside the pre_call
# cap, stalled the event loop for ~8.4 s). {1,64}/{1,255} are the RFC
# 5321 local-part and RFC 1035 domain bounds, so real addresses match
# unchanged ('user@localhost' still fails: no dot) while the per-start
# scan is capped and the sweep stays linear.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}")
# (?<!\d) / (?!\d) anchors: a phone must not sit inside a longer digit run
# (the reference pattern flagged suffixes of IDs and dates).
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
# 13-19 digit runs with optional separator characters; Luhn decides which
# candidates are actual card numbers (see _mask_credit_cards). The class
# covers the common card DISPLAY separators: space (incl. NBSP/narrow NBSP),
# dot ("4111.1111.1111.1111") and hyphen — AUDIT-2026-11-10 P1-6: dot- and
# NBSP-separated cards passed the old [ -] class unmasked. The ZERO-WIDTH
# characters (\u200b\u200c\u200d\u2060\ufeff) are IN the class because a prompt
# can embed them inside a card number to defeat any separator regex — and they
# must be matched IN PLACE (cross-val fix: an earlier "strip, scan the cleaned
# copy" version spliced CLEANED offsets into the ORIGINAL string, shifting
# every mask left by the zero-widths before the card and asserting a
# redaction that did not happen).
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ .\-\u00a0\u202f\u200b\u200c\u200d\u2060\ufeff]?){13,19}\b")
# The one-string twin of the candidate separator class (digit-strip before the
# Luhn check and the trailing-separator borrow in _mask_credit_cards).
_CARD_SEPARATORS = " .-\u00a0\u202f\u200b\u200c\u200d\u2060\ufeff"

# Canonical entity order for masking and for detail strings.
_PII_ORDER = ("EMAIL", "PHONE", "CREDIT_CARD")
_PII_PATTERNS: dict[str, re.Pattern[str]] = {"EMAIL": _EMAIL_RE, "PHONE": _PHONE_RE}

_LENGTH_DEFAULTS = {"pre_call": 100_000, "post_call": 200_000}


class GuardrailBlocked(Exception):
    """A guardrail refused the request (pre_call raises, post_call blocks).

    Carries everything a caller needs to log the verdict event without
    re-looking-up the spec: .name, .mode, .detail and .engine.

    .reply_text — when the guard's config carries a non-empty `reply_text`,
    the block is a SOFT block: the proxy settles the same
    `guardrail_blocked` ledger row but returns a normal chat completion
    whose content is this notice instead of a 400 (an agent client keeps
    its thread; the offending turn is not fatal). Empty/absent = the
    documented hard 400.
    """

    def __init__(self, name: str, mode: str, detail: str, engine: str = "", reply_text: str = "") -> None:
        self.name = name
        self.mode = mode
        self.detail = detail
        self.engine = engine
        self.reply_text = reply_text
        super().__init__(f"blocked by guardrail '{name}': {detail}")


@dataclass(frozen=True)
class GuardrailSpec:
    """One enabled guardrail attached to a model (a guardrails DB row)."""

    id: uuid.UUID
    name: str
    mode: str  # 'pre_call' | 'post_call'
    engine: str  # 'blocklist' | 'regex_pii' | 'length' | 'webhook'
    config: dict  # parsed from the row's config_json
    enabled: bool


@dataclass(frozen=True)
class GuardrailEvent:
    """One guardrail's verdict for one run — exactly one per guardrail."""

    guardrail: str
    mode: str
    engine: str
    action: str  # 'pass' | 'masked' | 'blocked'
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "guardrail": self.guardrail,
            "mode": self.mode,
            "engine": self.engine,
            "action": self.action,
            "detail": self.detail,
        }


_LOAD_SQL = """
SELECT g.id, g.name, g.mode, g.engine, g.config_json, g.enabled
FROM guardrails g JOIN model_guardrails mg ON mg.guardrail_id = g.id
WHERE mg.model_name = $1 AND g.enabled
ORDER BY g.name
"""

# Guardrail-spec TTL cache (REMEDIATION P4): the lookup above ran on EVERY
# guarded request — one SELECT per request for config that changes on
# operator action, not per request. Keyed per MODEL NAME (the cache key IS
# the query's $1), value (db_handle, specs, monotonic_expiry). Bounded in
# practice: the key space is the set of guarded virtual model names, an
# operator-curated list of tens — there is no unbounded-growth path (no
# per-request key material). ONLY successful reads are cached: a DB error
# propagates as today (the caller's fail-open posture is unchanged) and
# caches NOTHING, so an outage is never frozen into a stale empty guard set.
#
# The db handle is part of the entry ON PURPOSE: a spec list is only valid
# for the connection pool it was read through, so a swapped handle (the test
# suite's monkeypatched fakes, or a future multi-database split) can never be
# served another handle's specs. `db` is a stable module-level singleton in
# production (db/__init__.py: `db = Database()`), so this never false-misses
# there.
_SPEC_TTL_S = 60.0
_spec_cache: dict[str, tuple[Any, list[GuardrailSpec], float]] = {}


def guardrails_spec_cache_clear(model_name: str | None = None) -> None:
    """Drop the guardrail-spec cache (one model, or all when None).

    MUST be called by every admin-plane guardrail mutation — the admin plane
    writes the guardrails / model_guardrails tables directly, so without an
    explicit clear a fresh attach/edit/delete stays invisible for up to
    _SPEC_TTL_S. Test surfaces call it for isolation. NOTE: this module
    cannot invalidate itself on writes (the mutations live in
    api/routes_admin.py, a layer core must not import); the call-site
    wiring is the routes_admin owner's change (see _spec_cache above).
    """
    if model_name is None:
        _spec_cache.clear()
    else:
        _spec_cache.pop(model_name, None)


async def guardrails_for_model(model_name: str) -> list[GuardrailSpec]:
    """The enabled guardrails attached to a virtual model name, in run
    order (ORDER BY name).

    TTL-cached for _SPEC_TTL_S (key = model name) — see _spec_cache. The
    returned list is the CACHED object: treat it as read-only (the runners
    only read; do not mutate in place).

    DB errors PROPAGATE — the caller decides the fail-open posture (a
    config outage must not silently disable enforcement unseen), and they
    are never cached.
    config_json is tolerated NULL (and empty) -> {}; anything else must
    parse as a JSON object or the error propagates too — engines assume
    dict configs and a silent list/str config would crash later, far from
    the actual misconfiguration.
    """
    now = time.monotonic()
    cached = _spec_cache.get(model_name)
    if cached is not None and cached[0] is db and cached[2] > now:
        return cached[1]
    rows = await db.fetch(_LOAD_SQL, model_name)
    specs: list[GuardrailSpec] = []
    for row in rows:
        raw = row["config_json"]
        config: dict[str, Any] = {}
        if raw:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"guardrail {row['name']!r}: config_json must be a JSON object, got {type(parsed).__name__}"
                )
            config = parsed
        gid = row["id"]
        if not isinstance(gid, uuid.UUID):
            gid = uuid.UUID(str(gid))  # asyncpg yields UUID; fake rows may not
        specs.append(
            GuardrailSpec(
                id=gid,
                name=row["name"],
                mode=row["mode"],
                engine=row["engine"],
                config=config,
                enabled=bool(row["enabled"]),
            )
        )
    # Cache ONLY the successful read (the parse above raised -> nothing stored).
    _spec_cache[model_name] = (db, specs, now + _SPEC_TTL_S)
    return specs


# ---------------------------------------------------------------------------
# user-text traversal (the shared pre_call surface)
# ---------------------------------------------------------------------------


def _user_texts(messages: list[dict]) -> list[str]:
    """Every user text part, flattened in message order: str content, or the
    "text" entries of multimodal content-part dicts."""
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    return texts


def concat_user_text(messages: list[dict]) -> str:
    """All user text joined with newlines — what a pre_call guard reads.

    Newline (not '') join: a keyword or PII pattern cannot straddle a part
    boundary, and every part boundary counts toward the length caps.
    """
    return "\n".join(_user_texts(messages))


def rewrite_user_texts(messages: list[dict], fn: Callable[[str], str]) -> list[dict]:
    """Apply fn(text)->str to EVERY user text part, preserving structure.

    Returns a new list; the input is never mutated. Non-user messages pass
    through untouched, and non-text content parts (image_url, audio, ...)
    keep their other keys and identity.
    """
    out: list[dict] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            out.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            out.append({**message, "content": fn(content)})
        elif isinstance(content, list):
            parts = [
                {**part, "text": fn(part["text"])}
                if isinstance(part, dict) and isinstance(part.get("text"), str)
                else part
                for part in content
            ]
            out.append({**message, "content": parts})
        else:
            out.append(message)
    return out


# ---------------------------------------------------------------------------
# engines — each yields exactly ONE verdict per guardrail
# ---------------------------------------------------------------------------


def _cfg_of(guard: GuardrailSpec) -> dict:
    """The guardrail's config, defensively dict-shaped (config_json is a
    TEXT column; hand-built specs may not carry a dict)."""
    return guard.config if isinstance(guard.config, dict) else {}


def _reply_text_of(cfg: dict) -> str:
    """The soft-block notice, if the guard carries one (see
    GuardrailBlocked.reply_text). Non-empty str only — anything else
    (None, non-str, whitespace-only) is the hard 400."""
    raw = cfg.get("reply_text")
    return raw if isinstance(raw, str) and raw.strip() else ""


def _keywords_of(cfg: dict) -> list[str]:
    """Non-empty string keywords, in config order (empty ones ignored)."""
    raw = cfg.get("keywords")
    if not isinstance(raw, (list, tuple)):
        return []
    return [k for k in raw if isinstance(k, str) and k]


def _first_keyword_hit(keywords: list[str], texts: list[str]) -> str | None:
    """First keyword (CONFIG order) matching any text case-insensitively —
    config order keeps deny verdicts deterministic for the audit log."""
    # AUDIT-r5 #3 (round 3): the deny decision must fold exactly like the
    # mask engine — a 'ς' keyword policy must hit 'σ' text, which plain
    # .lower() misses and would PASS what the mask would have redacted.
    lowered = [_fold(t) for t in texts]
    for kw in keywords:
        kw_low = _fold(kw)
        if any(kw_low in t for t in lowered):
            return kw
    return None


# The span pass (str.find) must search the SAME folded form the loop's
# re.IGNORECASE subn matches against — and the fold must not change any
# length, or the spans would point off the original text. str.lower()
# misses the literal-equivalence pairs re adds on top of it (CPython's
# re._casefix table: final/regular sigma, micro/mu, theta variants, the
# Cyrillic te-tail set, ...);
#   * U+0130's lower is TWO chars ('i' + combining dot), so sre equates
#     it to 'i' through its expansion handling instead of the table; and
#   * str.lower() applies the CONTEXTUAL Greek final-sigma rule: a 'Σ'
#     before a non-cased char lowers to 'ς', not 'σ' — the fold image of
#     'Σ' would depend on its NEIGHBOURS, while re.IGNORECASE matches
#     case per character (round 5: a keyword 'σ' missed a word-final
#     'Σ' that str.lower() had turned into 'ς').
# The table therefore folds every character to a FIXED class member:
# one bulk-guarded scan collects every 1:1 lowercase pair (the cased
# 'A' after each char keeps the final-sigma rule from firing while
# scanning; the one 2-char lower U+0130 is excluded and unioned
# explicitly), the interpreter's _casefix classes are unioned in on
# top, and every class collapses onto its smallest member that is its
# own lowercase. Because sigma-form characters all map AWAY from 'Σ',
# no 'Σ' survives the translate step and the trailing .lower() in
# _fold can never fire the contextual rule — it only serves as the
# plain-case folder should the table build degrade (no interpreter
# table), where the len guards at both call sites then decline
# anything that lengthens to the exact sequential loop.
def _build_fold_trans() -> list[str | None]:
    # getattr, not "from re import _casefix": the module attribute is private
    # and absent from typeshed, so a direct import fails type-checking while
    # getattr degrades cleanly on an interpreter that does not carry it.
    import re as _re

    _casefix = getattr(_re, "_casefix", None)
    extra: dict[int, tuple[int, ...]] = {}
    if _casefix is not None:
        raw = getattr(_casefix, "_EXTRA_CASES", None) or {}
        for k, v in raw.items():
            try:
                extra[int(k)] = tuple(int(x) for x in (v if isinstance(v, (tuple, list)) else (v,)))
            except (TypeError, ValueError):
                continue
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        # setdefault BOTH endpoints: a case partner (upper form, say 'S')
        # may be otherwise unmentioned, and find's path-halving needs
        # every stored value to be a key.
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    def mention(cp: int) -> None:
        parent.setdefault(cp, cp)
        lo = chr(cp).lower()
        if len(lo) == 1:  # U+0130's 2-char lower must not poison the union
            union(cp, ord(lo))
        # AUDIT-r5 #3 (round 4): upper forms must land in the class too —
        # re matches a char against its upper trivially, so a class that
        # excludes the upper form splits.
        up = chr(cp).upper()
        if len(up) == 1:
            union(cp, ord(up))

    mention(0x130)
    union(0x130, 0x69)  # sre's expansion equivalence: 'İ' matches 'i'
    for k, members in extra.items():
        mention(k)
        for cp in members:
            mention(cp)
        for cp in members:
            union(k, cp)
    # Every 1:1 lowercase pair is likewise re-equivalent. Collect them in
    # ONE bulk pass so import stays cheap: the cased guard letter after
    # each char keeps the contextual final-sigma rule from firing (a
    # word-final capital sigma must still map to 'σ' here, not 'ς'), and
    # U+0130 — the one 2-char lower, which would desynchronise the
    # pairing — is excluded and already unioned above. If a FUTURE char
    # grows a 2-char lower, the length check skips this pass and only
    # the class table plus plain .lower() remain (conceded degradation,
    # see _fold).
    chars = "".join(chr(cp) + "A" for cp in range(0x11000) if not 0xD800 <= cp <= 0xDFFF and cp != 0x130)
    lowered = chars.lower()
    if len(lowered) == len(chars):
        for i in range(0, len(chars), 2):
            if chars[i] != lowered[i]:
                union(ord(chars[i]), ord(lowered[i]))
    classes: dict[int, list[int]] = {}
    for cp in parent:
        classes.setdefault(find(cp), []).append(cp)
    trans: dict[int, str] = {}
    for group in classes.values():
        # canonical = smallest member that is its OWN lowercase, so no
        # class image can be re-contextualised by the trailing .lower()
        own_lower = [m for m in group if chr(m) == chr(m).lower()]
        canon = min(own_lower) if own_lower else min(group)
        for cp in group:
            if cp != canon:
                trans[cp] = chr(canon)
    # list, not dict: str.translate indexes a sequence in C, and the
    # fold sits on the per-request masking path. Identity-filled — a
    # None entry would DELETE the character, not leave it unchanged.
    table: list[str | None] = [chr(i) for i in range(0x11000)]
    for cp, ch in trans.items():
        table[cp] = ch
    return table


_FOLD_TRANS = _build_fold_trans()


def _fold(s: str) -> str:
    """Length-preserving, CONTEXT-FREE fold reproducing re.IGNORECASE's
    literal equivalence for THIS interpreter.

    str.lower() is unusable directly: it misses re's extra
    literal-equivalence pairs (re._casefix: sigma variants, micro/mu,
    ...), it LENGTHENS U+0130 (-> 'i' + combining dot) — shifting find()
    spans off the original text — and its contextual Greek final-sigma
    rule makes the image of 'Σ' depend on the NEXT character, while
    re.IGNORECASE matches case per character. The translate table maps
    every character to a fixed member of its re-equivalence class (see
    _build_fold_trans), which keeps len(_fold(s)) == len(s) — offsets
    stay valid — removes every 'Σ' so the trailing .lower() can never
    fire the contextual rule, and degrades to plain .lower() if the
    table build found no interpreter table; the len guards at both call
    sites then decline any lengthening codepoint to the exact
    sequential loop."""
    return s.translate(_FOLD_TRANS).lower()


@lru_cache(maxsize=64)
def _simple_keywords(keywords: tuple[str, ...]) -> tuple[str, ...] | None:
    """Deduped, re-folded (IGNORECASE-exact) set when a single span pass
    is provably equivalent to the sequential loop; None when it is not.

    The loop masks keyword 1 everywhere, then keyword 2 over the ALREADY
    masked text, so the two diverge whenever one keyword can interact with
    another or with the mask token. Three exclusions make one pass exact:

    * a keyword that is a substring of another keyword (e.g. ["bc","abc"]
      on "abc": the loop yields "a[REDACTED]", one leftmost pass yields
      "[REDACTED]");
    * a keyword that occurs inside MASK (the loop can match a later keyword
      inside a token an earlier one just inserted);
    * an empty keyword (the loop's behaviour there is degenerate).

    The returned keywords are the FOLDED forms — the span pass searches
    text folded the same way (_fold), so find() sees what the loop's
    re.subn sees.

    Cached per keyword tuple, so a set is validated once, not per request.
    Exact duplicates collapse (masking a keyword twice is idempotent, and
    the loop's second pass counts zero hits)."""
    mask_low = _fold(MASK)
    seen: list[str] = []
    for kw in keywords:
        kw_low = _fold(kw)
        if len(kw_low) != len(kw):
            # AUDIT-r5 #3 (round 2): a keyword the fold cannot keep
            # length-preserving cannot drive span search — take the loop.
            return None
        if not kw_low or kw_low in mask_low:
            return None
        for other in keywords:
            other_low = _fold(other)
            if other_low != kw_low and kw_low in other_low:
                return None
        if kw_low not in seen:
            seen.append(kw_low)
    return tuple(seen)


def _mask_by_spans(text: str, lowered_keywords: tuple[str, ...]) -> tuple[str, int] | None:
    """One pass over the folded text; None when occurrences OVERLAP or
    the fold fails to preserve the text's length (see the guard below).

    Overlap is the remaining way one pass could differ from the loop:
    masking "abc" then "bcd" on "abcd" leaves "[REDACTED]d", while a single
    combined pass would swallow the "bcd" too. It is detected and the caller
    falls back. str.find is the C memchr loop, so the cost is one lowercase
    pass plus one scan per keyword instead of one regex pass per keyword."""
    lowered = _fold(text)
    if len(lowered) != len(text):
        # AUDIT-r5 #3 safety net: a codepoint the fold does not already
        # map that .lower() would LENGTHEN shifts every span off the
        # original text — 20 of them before a secret shipped it verbatim
        # while redacting an unrelated tail. Decline; the caller's
        # sequential re.subn loop is exact under any case expansion.
        # (Today no such codepoint exists — see _fold.)
        return None
    spans: list[tuple[int, int]] = []
    for kw in lowered_keywords:
        start = lowered.find(kw)
        width = len(kw)
        while start != -1:
            spans.append((start, start + width))
            start = lowered.find(kw, start + 1)
    if not spans:
        return text, 0
    spans.sort()
    previous_end = -1
    for start, end in spans:
        if start < previous_end:
            return None
        previous_end = end
    parts: list[str] = []
    last = 0
    for start, end in spans:
        parts.append(text[last:start])
        parts.append(MASK)
        last = end
    parts.append(text[last:])
    return "".join(parts), len(spans)


def _mask_keywords(text: str, keywords: list[str]) -> tuple[str, int]:
    """Replace EVERY occurrence of EVERY keyword, case-insensitively (BUG
    FIX vs the reference, which masked only the first hit). re.escape keeps
    literal keywords like 'c++' literal.

    PERF: the loop below is O(keywords x text) — 50 keywords over a 1.5 MB
    prompt is 50 full regex passes, measured at 340-600 ms of synchronous
    event-loop stall, the worst latency cliff in the gateway. When the set
    is provably order-insensitive (_simple_keywords) and its occurrences do
    not overlap (and the fold preserves the text's length), one span pass
    produces byte-identical output for roughly 17x less time. Every other
    case runs the original loop unchanged, so
    behaviour is identical for all inputs."""
    if not keywords:
        return text, 0
    simple = _simple_keywords(tuple(keywords))
    if simple is not None:
        fast = _mask_by_spans(text, simple)
        if fast is not None:
            return fast
    count = 0
    for kw in keywords:
        text, n = re.subn(re.escape(kw), MASK, text, flags=re.IGNORECASE)
        count += n
    return text, count


def _rewrite_masking(messages: list[dict], keywords: list[str]) -> tuple[list[dict], int]:
    """Mask every keyword across every user text part; total replacements."""
    total = 0

    def mask(text: str) -> str:
        nonlocal total
        text, n = _mask_keywords(text, keywords)
        total += n
        return text

    return rewrite_user_texts(messages, mask), total


def _luhn_ok(digits: str) -> bool:
    """Luhn (mod-10) checksum over a BARE digit string (no separators).

    Card masking only fires on Luhn-valid candidates, so 16-digit order
    numbers and phone-shaped digit runs stop being flagged as cards."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:  # every second digit from the right is doubled
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _plausible_card_span(span: str) -> bool:
    """False when a Luhn-valid candidate span is ORDINARY NUMERIC PROSE.

    Cross-val P3 (audit 2026-11-10): the widened separator class also matches
    dotted/spacey decimal text — "values 1.5 2.5 ... 13.5" concatenates to a
    Luhn-valid 13-19 digit run (~1/10 of such lists), so masking it would
    destroy content and raise a false CREDIT_CARD event. A span is masked
    only when it LOOKS like card display:
      * any zero-width separator — obfuscation intent by construction;
      * a dot anywhere — only accepted with 4-DIGIT grouping (the dotted
        display form "4111.1111.1111.1111"), never decimal points;
      * otherwise (spaces / hyphens / NBSP) — rejected only when EVERY digit
        group is shorter than 4 (range/decimal lists); bare 13-19 digit runs
        and card groupings up to Amex's 4-6-5 stay accepted as before.
    """
    if any(ch in span for ch in "\u200b\u200c\u200d\u2060\ufeff"):
        return True
    groups = re.findall(r"\d+", span)
    if not groups:
        return False
    if "." in span:
        # P2 (cross-val wave 2): the dotted DISPLAY shapes are not all 4-digit
        # groups — Amex is 4-6-5 and Diners 4-6-4 — so the old "every group is
        # exactly 4" rule leaked both. A dotted span whose groups are all
        # card-shaped (4/6/5) is card display, not a decimal.
        return len(groups) >= 3 and all(4 <= len(g) <= 6 for g in groups)
    return any(len(g) >= 4 for g in groups)


def _strip_card_separators(span: str) -> str:
    """The BARE digits of a candidate span (every separator class member gone)."""
    digits = span
    for sep in _CARD_SEPARATORS:
        digits = digits.replace(sep, "")
    return digits


def _dotted_card_tail_offset(span: str) -> int | None:
    """Offset within a REJECTED dotted candidate where a card-shaped TAIL starts.

    P2 (cross-val wave 2, audit 2026-11-10): the candidate regex's greedy
    separator class lets the LEFTMOST match swallow a decimal that touches a
    card — "score 5.4111 1111 1111 1111" is ONE 17-digit candidate. The whole
    span then fails the Luhn/plausibility gate, and because finditer matches
    are NON-OVERLAPPING the inner 16-digit PAN is never retried: the full PAN
    survived, for every leading digit. The same greedy fusion breaks the
    dotted Amex (4-6-5) and dotted Diners (4-6-4) displays.

    This re-scan recovers the card that STARTS at the first digit group long
    enough to begin one (>= 4 digits), dropping a fused decimal's integer part
    ("5.") while keeping the card intact. A tail is accepted when it is
    Luhn-valid AND card-shaped, or — for a display split into card-shaped
    groups with at most one short group (the dotted 4-4-4-2 Diners form) —
    when its digit count is still a PAN length. Decimal prose has no group of
    4+ digits, so it yields None and is left byte-identical.
    """
    groups = list(re.finditer(r"\d+", span))
    start = next((m for m in groups if len(m.group(0)) >= 4), None)
    if start is None:
        return None
    tail_groups = [m.group(0) for m in groups if m.start() >= start.start()]
    tail_digits = _strip_card_separators(span[start.start() :])
    if not 13 <= len(tail_digits) <= 19 or len(tail_groups) < 3:
        return None
    if all(4 <= len(g) <= 6 for g in tail_groups) and _luhn_ok(tail_digits):
        return start.start()
    if sum(1 for g in tail_groups if len(g) < 4) <= 1:
        return start.start()
    return None


def _mask_credit_cards(text: str) -> tuple[str, int]:
    """Mask only Luhn-valid candidates. The candidate regex can borrow ONE
    trailing separator into its span (greedy separator class before the
    closing \\b); the mask replaces the card digits and leaves that
    separator, so the following word does not fuse to the mask.

    The scan runs over the ORIGINAL text and the match span IS the splice
    span (P1-6, cross-val fix): the zero-width characters a prompt embeds
    inside a card number are members of the separator class, matched in
    place — never stripped into a cleaned copy whose offsets would not line
    up with the string being masked. A Luhn hit is further gated by
    _plausible_card_span so ordinary numeric prose is not destroyed."""
    pieces: list[str] = []
    pos = 0
    count = 0
    for match in _CARD_CANDIDATE_RE.finditer(text):
        # Strip EVERY separator class member before the Luhn check — the
        # display formats differ only in the separator, never in the digits.
        span = match.group(0)
        start = match.start()
        if not _luhn_ok(_strip_card_separators(span)) or not _plausible_card_span(span):
            # P2 (cross-val wave 2): a dotted candidate the gate rejected may
            # have fused a decimal onto a real PAN (or split an Amex/Diners
            # display). Recover the card-shaped TAIL and splice the mask from
            # its first digit — never the whole fused span, so the decimal's
            # integer part survives. Non-overlapping finditer would otherwise
            # never retry the PAN that lives inside the rejected span.
            if "." not in span:
                continue
            offset = _dotted_card_tail_offset(span)
            if offset is None:
                continue
            start += offset
        end = match.end()
        if text[end - 1] in _CARD_SEPARATORS:
            end -= 1
        pieces.append(text[pos:start])
        pieces.append(MASK)
        pos = end
        count += 1
    pieces.append(text[pos:])
    return "".join(pieces), count


def _mask_pii(text: str, entities: set[str] | None) -> tuple[str, dict[str, int]]:
    """One pass of every enabled entity over ONE text part; entities None
    (empty/missing config) means all three. Canonical EMAIL, PHONE,
    CREDIT_CARD order — EMAIL first, so a phone-shaped local part is
    already masked before the phone pattern runs."""
    counts: dict[str, int] = {}
    for entity in _PII_ORDER:
        if entities is not None and entity not in entities:
            continue
        if entity == "CREDIT_CARD":
            text, count = _mask_credit_cards(text)
        else:
            text, count = _PII_PATTERNS[entity].subn(MASK, text)
        if count:
            counts[entity] = count
    return text, counts


def _entities_of(cfg: dict) -> set[str] | None:
    """The configured PII entity subset; None = all three (missing/empty)."""
    raw = cfg.get("entities")
    if isinstance(raw, (list, tuple)) and raw:
        return {str(e).upper() for e in raw}
    return None


def _detail_of(counts: dict[str, int]) -> str:
    """ "EMAILx2, CREDIT_CARDx1" — hit entities only, canonical order.

    P2-7 (audit 2026-11-10): the secrets engine keys its counts by SECRET
    KIND ("aws_access_key", "jwt", ...) — keys that are not in _PII_ORDER —
    so its events always carried an EMPTY detail. The kinds are appended in
    their declaration order (SECRET_KINDS); any key outside both vocabularies
    is appended sorted, so an engine that adds a new count axis can never
    silently emit an empty detail again."""
    parts = [f"{entity}x{counts[entity]}" for entity in _PII_ORDER if counts.get(entity)]
    parts += [f"{kind}x{counts[kind]}" for kind in SECRET_KINDS if counts.get(kind)]
    extra = sorted(k for k in counts if counts[k] and k not in _PII_ORDER and k not in SECRET_KINDS)
    parts += [f"{k}x{counts[k]}" for k in extra]
    return ", ".join(parts)


def _length_limit(guard: GuardrailSpec, cfg: dict) -> int:
    """max_chars with engine defaults (100k in / 200k out); a junk value
    falls back to the default rather than crashing the relay."""
    default = _LENGTH_DEFAULTS[guard.mode]
    raw = cfg.get("max_chars", default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        log.warning("guardrail %r: invalid max_chars %r — using default %d", guard.name, raw, default)
        return default


async def _webhook_verdict(http: httpx2.AsyncClient, guard: GuardrailSpec, text: str) -> tuple[str, str]:
    """One moderator round-trip, ALWAYS fail-open.

    Returns (kind, payload): kind 'block' | 'mask' | 'pass'; payload is the
    block detail for 'block' and the masked text for 'mask'. Transport
    errors (any Exception — httpx2.HTTPError, a malformed URL, ...), HTTP
    >= 400 and non-JSON bodies all degrade to ('pass', 'webhook error:'):
    the gateway must not go down with its moderator, but the failure must
    be visible in the event.
    """
    cfg = _cfg_of(guard)
    url = cfg.get("url")
    if not url or not isinstance(url, str):
        return "pass", "no url configured"
    headers = {"Content-Type": "application/json"}
    api_key_env = cfg.get("api_key_env")
    if isinstance(api_key_env, str) and api_key_env:
        key = os.environ.get(api_key_env)
        if key:  # unset/empty secret means "no auth", never a hard stop
            headers["Authorization"] = f"Bearer {key}"
    timeout = settings.guardrail_webhook_timeout_s
    raw_timeout = cfg.get("timeout_s")
    if raw_timeout is not None:
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            log.warning(
                "guardrail %r: invalid timeout_s %r — using default %.1fs",
                guard.name,
                raw_timeout,
                timeout,
            )
    # P3 (audit 2026-11-10): clamp the per-guardrail override to a sane band —
    # timeout_s: 999999 used to pin one request for ~11 days against a hung
    # moderator. Lower bound keeps a zero/negative junk value meaningful; the
    # upper bound is the operator's own default (at least 30s) so a site with
    # a deliberately slow moderator is not clamped below what it configured.
    timeout = max(0.5, min(timeout, max(settings.guardrail_webhook_timeout_s, 30.0)))
    payload = {"text": text, "guardrail": guard.name}
    try:
        response = await http.post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as exc:  # fail-open by design (see docstring)
        return "pass", f"webhook error: {exc}"[:200]
    if response.status_code >= 400:
        return "pass", f"webhook error: HTTP {response.status_code}"[:200]
    try:
        data = response.json()
    except ValueError:
        return "pass", "webhook error: non-JSON response"[:200]
    if not isinstance(data, dict):
        return "pass", ""
    verdict = data.get("verdict")
    if verdict == "block":
        reason = data.get("reason")
        return "block", str(reason)[:200] if reason else "webhook blocked"
    if verdict == "mask" and isinstance(data.get("text"), str):
        return "mask", data["text"]
    return "pass", ""


def _apply_webhook_mask(messages: list[dict], masked: str) -> tuple[list[dict], bool]:
    """Apply a webhook's masked text — but ONLY when unambiguous.

    BUG FIX vs the reference (it overwrote EVERY user part with the same
    masked text): with exactly ONE non-empty user text part the mask target
    is unambiguous and that part is replaced surgically; any other shape is
    left alone — guessing which part to overwrite would destroy sibling
    content. Returns (messages, applied?)."""
    texts = _user_texts(messages)
    non_empty = [t for t in texts if t]
    if len(non_empty) != 1:
        return messages, False
    index = texts.index(non_empty[0])
    seen = 0

    def replace(text: str) -> str:
        nonlocal seen
        at = seen
        seen += 1
        return masked if at == index else text

    return rewrite_user_texts(messages, replace), True


def _cap(events: list[GuardrailEvent]) -> list[GuardrailEvent]:
    """Audit cap: enforcement ran in full; only recorded verdicts are cut."""
    if len(events) > _MAX_EVENTS:
        log.warning(
            "guardrails: %d verdict(s) dropped at the %d-event audit cap",
            len(events) - _MAX_EVENTS,
            _MAX_EVENTS,
        )
        return events[:_MAX_EVENTS]
    return events


def _event(guard: GuardrailSpec, engine: str, action: str, detail: str = "") -> GuardrailEvent:
    """The verdict event for one guardrail's run (mode rides the spec —
    the runners only dispatch guards whose mode matches)."""
    return GuardrailEvent(guard.name, guard.mode, engine, action, detail)


# ---------------------------------------------------------------------------
# runners — the proxy wiring calls exactly these two
# ---------------------------------------------------------------------------


async def run_pre_call(
    guards: list[GuardrailSpec], messages: list[dict], http: httpx2.AsyncClient
) -> tuple[list[dict], list[GuardrailEvent]]:
    """Run the pre_call guards over the request messages, in the given
    order.

    Returns (possibly-rewritten messages, events). Raises GuardrailBlocked
    on the first block — later guards do not run, the request is already
    refused. The exception carries .engine so the caller can log the full
    verdict event.
    """
    events: list[GuardrailEvent] = []
    current = messages
    for guard in guards:
        if guard.mode != "pre_call":
            continue
        cfg = _cfg_of(guard)
        if guard.engine == "blocklist":
            hit = _first_keyword_hit(_keywords_of(cfg), _user_texts(current))
            if hit is None:
                events.append(_event(guard, "blocklist", "pass"))
            elif cfg.get("action", "block") == "mask":
                current, n = _rewrite_masking(current, _keywords_of(cfg))
                events.append(_event(guard, "blocklist", "masked", f"masked {n} occurrence(s)"))
            else:
                detail = f"blocked keyword '{hit}'"
                events.append(_event(guard, "blocklist", "blocked", detail))
                raise GuardrailBlocked(
                    guard.name, "pre_call", detail, engine="blocklist", reply_text=_reply_text_of(cfg)
                )
        elif guard.engine == "regex_pii":
            requested = _entities_of(cfg)
            totals: dict[str, int] = {}

            def mask_part(text: str) -> str:
                new_text, counts = _mask_pii(text, requested)
                for entity, count in counts.items():
                    totals[entity] = totals.get(entity, 0) + count
                return new_text

            current = rewrite_user_texts(current, mask_part)
            action = "masked" if totals else "pass"
            events.append(_event(guard, "regex_pii", action, _detail_of(totals)))
        elif guard.engine == "length":
            limit = _length_limit(guard, cfg)
            n = len(concat_user_text(current))
            if n > limit:
                detail = f"input length {n} exceeds max_chars {limit}"
                events.append(_event(guard, "length", "blocked", detail))
                raise GuardrailBlocked(guard.name, "pre_call", detail, engine="length", reply_text=_reply_text_of(cfg))
            events.append(_event(guard, "length", "pass"))
        elif guard.engine == "webhook":
            kind, payload = await _webhook_verdict(http, guard, concat_user_text(current))
            if kind == "block":
                events.append(_event(guard, "webhook", "blocked", payload))
                raise GuardrailBlocked(
                    guard.name, "pre_call", payload, engine="webhook", reply_text=_reply_text_of(cfg)
                )
            if kind == "mask":
                current, applied = _apply_webhook_mask(current, payload)
                if applied:
                    detail = "webhook returned masked text"
                    events.append(_event(guard, "webhook", "masked", detail))
                else:
                    detail = "webhook mask skipped: multiple user text parts"
                    events.append(_event(guard, "webhook", "pass", detail))
            else:
                events.append(_event(guard, "webhook", "pass", payload))
        elif guard.engine == "secrets":
            kinds = _secret_kinds_of(cfg)
            action = str(cfg.get("action", "mask")).lower()
            if action == "block":
                found = _first_secret_hit(_user_texts(current), kinds)
                if found is not None:
                    detail = f"blocked {found} credential"
                    events.append(_event(guard, "secrets", "blocked", detail))
                    raise GuardrailBlocked(
                        guard.name, "pre_call", detail, engine="secrets", reply_text=_reply_text_of(cfg)
                    )
                events.append(_event(guard, "secrets", "pass"))
            elif action == "flag":
                found = _first_secret_hit(_user_texts(current), kinds)
                events.append(_event(guard, "secrets", "pass", f"flagged {found}" if found else ""))
            else:
                secret_totals: dict[str, int] = {}

                def mask_secrets_part(text: str) -> str:
                    new_text, counts = _mask_secrets(text, kinds)
                    for kind, count in counts.items():
                        secret_totals[kind] = secret_totals.get(kind, 0) + count
                    return new_text

                current = rewrite_user_texts(current, mask_secrets_part)
                action_name = "masked" if secret_totals else "pass"
                events.append(_event(guard, "secrets", action_name, _detail_of(secret_totals)))
        else:
            # Unreachable via the DDL CHECK constraint; a hand-built spec
            # must not silently pass or block — skip loudly, yield nothing.
            log.warning("guardrails: skipping %r — unknown engine %r", guard.name, guard.engine)
    return current, _cap(events)


async def run_post_call(
    guards: list[GuardrailSpec], text: str, http: httpx2.AsyncClient
) -> tuple[str, list[GuardrailEvent], bool]:
    """Run the post_call guards over the assistant text, in the given
    order.

    Returns (possibly-rewritten text, events, blocked?). The FIRST block
    stops the run — the response is refused as-is, mirroring the pre_call
    raise-and-stop."""
    events: list[GuardrailEvent] = []
    for guard in guards:
        if guard.mode != "post_call":
            continue
        cfg = _cfg_of(guard)
        if guard.engine == "blocklist":
            keywords = _keywords_of(cfg)
            hit = _first_keyword_hit(keywords, [text])
            if hit is None:
                events.append(_event(guard, "blocklist", "pass"))
            elif cfg.get("action", "block") == "mask":
                text, n = _mask_keywords(text, keywords)
                events.append(_event(guard, "blocklist", "masked", f"masked {n} occurrence(s)"))
            else:
                detail = f"blocked keyword '{hit}'"
                events.append(_event(guard, "blocklist", "blocked", detail))
                return text, _cap(events), True
        elif guard.engine == "regex_pii":
            text, counts = _mask_pii(text, _entities_of(cfg))
            action = "masked" if counts else "pass"
            events.append(_event(guard, "regex_pii", action, _detail_of(counts)))
        elif guard.engine == "length":
            limit = _length_limit(guard, cfg)
            if len(text) > limit:
                detail = f"input length {len(text)} exceeds max_chars {limit}"
                events.append(_event(guard, "length", "blocked", detail))
                return text, _cap(events), True
            events.append(_event(guard, "length", "pass"))
        elif guard.engine == "webhook":
            kind, payload = await _webhook_verdict(http, guard, text)
            if kind == "block":
                events.append(_event(guard, "webhook", "blocked", payload))
                return text, _cap(events), True
            if kind == "mask":
                # post_call is a single string — the mask is always applied.
                text = payload
                detail = "webhook returned masked text"
                events.append(_event(guard, "webhook", "masked", detail))
            else:
                events.append(_event(guard, "webhook", "pass", payload))
        elif guard.engine == "secrets":
            kinds = _secret_kinds_of(cfg)
            action = str(cfg.get("action", "mask")).lower()
            if action == "block":
                found = _first_secret_hit([text], kinds)
                if found is not None:
                    detail = f"blocked {found} credential in the answer"
                    events.append(_event(guard, "secrets", "blocked", detail))
                    return text, _cap(events), True
                events.append(_event(guard, "secrets", "pass"))
            elif action == "flag":
                found = _first_secret_hit([text], kinds)
                events.append(_event(guard, "secrets", "pass", f"flagged {found}" if found else ""))
            else:
                text, counts = _mask_secrets(text, kinds)
                action_name = "masked" if counts else "pass"
                events.append(_event(guard, "secrets", action_name, _detail_of(counts)))
        else:
            log.warning("guardrails: skipping %r — unknown engine %r", guard.name, guard.engine)
    return text, _cap(events), False
