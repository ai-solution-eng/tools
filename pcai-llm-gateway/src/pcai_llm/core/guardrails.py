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
  (character caps), webhook (external moderator).
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
from typing import Any

import httpx2

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

GUARDRAIL_MODES = ("pre_call", "post_call")
GUARDRAIL_ENGINES = ("blocklist", "regex_pii", "length", "webhook")
MASK = "[REDACTED]"

# Audit cap for one run's event list (the ledger's guardrail_events JSONB).
# Enforcement is NEVER capped — only the recorded verdicts are.
_MAX_EVENTS = 50

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# (?<!\d) / (?!\d) anchors: a phone must not sit inside a longer digit run
# (the reference pattern flagged suffixes of IDs and dates).
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
# 13-19 digit runs with optional ' '/'-' separators; Luhn decides which
# candidates are actual card numbers (see _mask_credit_cards).
_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

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
    lowered = [t.lower() for t in texts]
    for kw in keywords:
        kw_low = kw.lower()
        if any(kw_low in t for t in lowered):
            return kw
    return None


def _mask_keywords(text: str, keywords: list[str]) -> tuple[str, int]:
    """Replace EVERY occurrence of EVERY keyword, case-insensitively (BUG
    FIX vs the reference, which masked only the first hit). re.escape keeps
    literal keywords like 'c++' literal."""
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


def _mask_credit_cards(text: str) -> tuple[str, int]:
    """Mask only Luhn-valid candidates. The candidate regex can borrow ONE
    trailing separator into its span (greedy ``[ -]?`` before the closing
    \\b); the mask replaces the card digits and leaves that separator, so
    the following word does not fuse to the mask."""
    pieces: list[str] = []
    pos = 0
    count = 0
    for match in _CARD_CANDIDATE_RE.finditer(text):
        digits = match.group(0).replace(" ", "").replace("-", "")
        if not _luhn_ok(digits):
            continue
        end = match.end()
        if text[end - 1] in " -":
            end -= 1
        pieces.append(text[pos : match.start()])
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
    '"EMAILx2, CREDIT_CARDx1" — hit entities only, canonical order.'
    return ", ".join(f"{entity}x{counts[entity]}" for entity in _PII_ORDER if counts.get(entity))


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
        else:
            log.warning("guardrails: skipping %r — unknown engine %r", guard.name, guard.engine)
    return text, _cap(events), False
