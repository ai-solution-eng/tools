"""Screening of MCP tool RESULTS for injected instructions.

WHY THIS EXISTS (2026-11-10, tool-trust hardening): the gateway's guardrails
cover user text BEFORE a call and assistant text AFTER one, but a tool result
is a third channel -- attacker-controlled text (a document, a web page, a log
line, a memory record) that a server hands back and the relay appends to the
model's context as trusted `role=tool` content. The gateway is the only
component that sees every result before it re-enters the model, so this is
where that channel is screened.

THE CONTRACT (non-negotiable):
  * PURE and MODEL-FREE. Regexes and an entropy count over the text; no
    network, no key, no second billable call. Runs in the request path.
  * CONSERVATIVE. A false positive costs a line of annotation (or one refused
    result); a false negative costs a compromised turn. Severity is HIGH only
    for a clear instruction-override or role/control-token spoof; the
    exfiltration and encoded-payload shapes are LOW (suggestive, not proof).
  * NEVER raises: scan() takes any value and always returns a ScanResult.
  * THE CALLER OWNS THE POLICY. render() applies a mode to the text the model
    sees; logging is the relay's job, not this module's.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import unicodedata
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Policy modes. "annotate" is the middle ground between logging a finding and
# withholding the result: the model is TOLD what the gateway saw.
MODES = ("off", "warn", "block", "annotate")
MODE_OFF, MODE_WARN, MODE_BLOCK, MODE_ANNOTATE = MODES
# Fail-safe middle: an unparsable value neither disables the screen ("off")
# nor starts withholding every result ("block").
DEFAULT_MODE = MODE_WARN
# A typo coerces to "warn" (log only). Report the coercion once per distinct
# bad value so an operator can find it without a per-result warning flood.
_UNKNOWN_MODE_LOGGED: set[str] = set()

SEVERITY_NONE = "none"
SEVERITY_LOW = "low"
SEVERITY_HIGH = "high"
_SEVERITY_RANK = {SEVERITY_NONE: 0, SEVERITY_LOW: 1, SEVERITY_HIGH: 2}

FAMILY_OVERRIDE = "instruction_override"
FAMILY_ROLE = "role_spoof"
FAMILY_EXFIL = "exfiltration"
FAMILY_ENCODED = "encoded_payload"
# Tool text that imitates the gateway's OWN annotate header (2026-11-10,
# cross-validation P1): a hand-written "[pcai:tool-result-screen ...]" line
# otherwise scans clean, so the model sees two identical-format provenance
# headers and cannot tell which one the gateway wrote.
FAMILY_PROVENANCE = "provenance_spoof"

# --- normalisation for matching --------------------------------------------
# An attacker splits or disguises the phrase: Cyrillic homoglyphs ("igno" with
# a Cyrillic i), zero-width spaces ("ig<ZWSP>nore"), soft hyphens, or
# newline-split words ("ignore\nall\nprevious\ninstructions"). Matching runs on
# a NORMALISED surface -- NFKC, Unicode format characters (Cf: zero-width,
# bidi controls, soft hyphen, BOM) stripped, common Cyrillic/Greek lookalikes
# folded to ASCII, and the phrase gaps allowed to cross whitespace -- while
# every REASON quotes the ORIGINAL span (see _orig_snippet), so a trace shows
# what the server actually sent.
_HOMOGLYPHS = str.maketrans(
    {
        # Cyrillic lower / upper
        "а": "a",
        "в": "b",
        "е": "e",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "с": "c",
        "т": "t",
        "у": "y",
        "х": "x",
        "ѕ": "s",
        "і": "i",
        "ј": "j",
        "һ": "h",
        "ԁ": "d",
        "ӏ": "l",
        "ɡ": "g",
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "У": "Y",
        "Х": "X",
        "Ѕ": "S",
        "І": "I",
        "Ј": "J",
        "Һ": "H",
        "Ӏ": "I",
        # Greek lower / upper
        "α": "a",
        "ε": "e",
        "ι": "i",
        "κ": "k",
        "ν": "v",
        "ο": "o",
        "ρ": "p",
        "τ": "t",
        "υ": "u",
        "χ": "x",
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Υ": "Y",
        "Χ": "X",
        # Latin lookalikes
        "ı": "i",
        "ȷ": "j",
        "ɑ": "a",
        "ɩ": "i",
    }
)
# Fast path: ordinary printable ASCII (plus the whitespace tool output carries)
# needs no per-character work -- the overwhelming majority of results.
_NEEDS_NORMALISING_RE = re.compile(r"[^\x20-\x7e\n\r\t]")


def _match_surface(text: str) -> tuple[str, list[int]]:
    """(normalised match surface, original index per normalised char). PURE.

    Per-character NFKC plus homoglyph folding plus Cf stripping. The parallel
    origin list maps every normalised character back to the index it came
    from, which is what lets a reason quote the ORIGINAL text. An EMPTY origin
    means the surface is byte-identical to the original (the ASCII fast path),
    so a matched span can be quoted directly from it -- no 32k-index list is
    allocated for the overwhelming majority of results.
    """
    if _NEEDS_NORMALISING_RE.search(text) is None:
        return text, []
    out: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        if unicodedata.category(char) == "Cf":
            continue  # zero-width / bidi control / soft hyphen / BOM
        folded = unicodedata.normalize("NFKC", char).translate(_HOMOGLYPHS)
        for folded_char in folded:
            if unicodedata.category(folded_char) == "Cf":
                continue
            out.append(folded_char)
            origin.append(index)
    return "".join(out), origin


def _orig_snippet(match: re.Match[str], original: str, origin: list[int], cap: int = 60) -> str:
    """The matched span quoted from the ORIGINAL text. PURE.

    Matching happens on the normalised surface; this maps the span back through
    origin, so the reason shows what the server actually sent (homoglyphs,
    zero-widths and all) instead of the folded rendering.
    """
    if not origin or match.start() >= len(origin):
        return _snippet(match.group(0), cap)
    start = origin[match.start()]
    last = min(match.end() - 1, len(origin) - 1)
    end = origin[last] + 1 if match.end() > match.start() else start
    return _snippet(original[start:end], cap)


# --- instruction override ---------------------------------------------------
# "<verb> ... <target> ... <instruction noun>" inside ONE clause. The bounded,
# dot-excluding gaps keep a verb and a noun that merely share a SENTENCE from
# reading as an override -- but they deliberately CROSS newlines: a
# newline-split "ignore\nall\nprevious\ninstructions" was a verified evasion,
# and the dot bound is what keeps unrelated log lines apart.
_OVERRIDE_RE = re.compile(
    r"(?i)\b(?:ignore|disregard|forget|override|bypass|set\s+aside)\b[^.]{0,80}?\b"
    r"(?:previous|prior|above|earlier|preceding|foregoing|original|all|any|your|the)\b[^.]{0,80}?\b"
    r"(?:instructions?|prompts?|rules?|directions?|directives?|guidance|guardrails?|constraints?|polic(?:y|ies))\b"
)
# "disregard the above" carries no instruction noun; it is its own idiom.
_ABOVE_RE = re.compile(
    r"(?i)\b(?:ignore|disregard|forget|override|bypass)\b[^.]{0,24}?\b"
    r"(?:the\s+above|above|preceding\s+(?:text|messages?|context)|prior\s+context|everything\s+(?:above|before|prior))\b"
)
# A role reassignment is an override even without the word "instruction".
_ROLE_ASSIGN_RE = re.compile(r"(?i)\byou\s+are\s+now\b")
# "new system prompt" / "prompt override": a replacement prompt, not an edit
# request. Requiring "prompt" keeps "the new instructions are in the README"
# (a benign doc sentence) out.
_NEW_PROMPT_RE = re.compile(
    r"(?i)\b(?:(?:new|updated|revised|replacement|override)\s+(?:system\s+)?prompt|(?:system\s+)?prompt\s+override)\b"
)

# --- role / control-token spoofing -----------------------------------------
# Chat-template control tokens: no legitimate tool RESULT carries these. The
# generic shape covers <|im_start|>, <|endoftext|>, <|eot_id|>,
# <|start_header_id|> and every other spelling.
_CONTROL_TOKEN_RE = re.compile(r"<\|[A-Za-z_][A-Za-z0-9_]{1,24}\|>")
_CHAT_TEMPLATE_RE = re.compile(r"\[/?INST\]|<<\s*/?\s*SYS\s*>>|<</?s>|\[/?SYSTEM\]")
# A blank-line-separated role label is the classic hand-built transcript
# boundary ("...\n\nsystem: you are ..."). Mid-sentence "system:" does not
# match, so ordinary prose about system: fields stays quiet.
_ROLE_LABEL_RE = re.compile(r"(?im)\n[ \t]*\n[ \t]*(?:system|assistant|developer|human)[ \t]*:")
# Alpaca-style delimiters. ONLY the bare heading line matches -- a markdown
# "### Instructions" heading (the common benign shape) does not.
_PROMPT_TAG_RE = re.compile(r"(?im)^[ \t]*#{2,4}[ \t]*(?:instruction|response|system|assistant|human)[ \t]*:?[ \t]*$")

# --- provenance forgery -----------------------------------------------------
# ANNOTATE prepends a gateway-authored header. Tool text carrying the same
# pattern is a hand-written forgery: the model would otherwise see two
# identical-format headers and could not tell which one the gateway wrote
# (2026-11-10, cross-validation P1). ANY occurrence inside the BODY is a
# finding, whatever values it claims. The REAL header carries a per-result
# random token (annotation_header) that the body cannot predict.
_PROVENANCE_RE = re.compile(r"\[pcai:tool-result-screen\b[^\]\n]{0,400}\]")

# --- exfiltration -----------------------------------------------------------
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"'`]+|\bwww\.[^\s<>\"'`]+")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CRED_RE = re.compile(
    r"(?i)\b(?:api[\s_-]?key|access[\s_-]?key|secret[\s_-]?key|client[\s_-]?secret|password|passwd"
    r"|passphrase|credentials?|authorization|bearer|private[\s_-]?key|session[\s_-]?cookie"
    r"|aws_secret_access_key)\b"
    r"|-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"
)
_SEND_RE = re.compile(
    r"(?i)\b(?:send|post|upload|transmit|exfiltrate|forward|email|leak)\b[^.\n]{0,80}?\b(?:to|at|into)\b"
)

# --- encoded payloads -------------------------------------------------------
# A 120+ char run of the base64 alphabet is rare in prose (spaces are not in
# the class) and rare in identifiers (so are '+' and '='); the entropy floor
# then rejects long low-entropy runs (padded zeros, repeated filler).
_B64_RE = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")
_HEX_RE = re.compile(r"\b(?:[0-9a-fA-F]{2}){40,}\b")
_B64_MIN_ENTROPY = 4.5
_HEX_MIN_ENTROPY = 3.0


@dataclass(frozen=True)
class Hit:
    """ONE finding: which family, why (human-readable), how serious."""

    family: str
    reason: str
    severity: str


@dataclass(frozen=True)
class ScanResult:
    """Every finding for one text, plus the aggregate severity."""

    hits: tuple[Hit, ...] = ()
    severity: str = SEVERITY_NONE

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(hit.reason for hit in self.hits)

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(_unique_families(self))


def combine(*results: ScanResult) -> ScanResult:
    """Merge several screened FORMS of one result: worst severity, hits unioned.

    WHY (2026-11-10, cross-validation P1): the relay used to screen only the
    JSON envelope the model receives, where a real newline rides as the two
    characters backslash-n; the role-label detector anchors on a real blank
    line, so the same injected text scored HIGH raw and NONE inside the
    envelope. The relay now screens the UNWRAPPED text block too and merges
    the two with this: the worst severity wins and duplicate findings collapse.
    """
    hits: list[Hit] = []
    for result in results:
        for hit in result.hits:
            if hit not in hits:
                hits.append(hit)
    severity = SEVERITY_NONE
    for result in results:
        if _SEVERITY_RANK.get(result.severity, 0) > _SEVERITY_RANK[severity]:
            severity = result.severity
    return ScanResult(hits=tuple(hits), severity=severity)


def resolve_mode(value: Any) -> str:
    """Normalise a configured screen mode; anything unrecognised is "warn".

    The coercion is LOGGED once per distinct value (2026-11-10, cross-
    validation P2): a typo must not silently downgrade the screen to log-only.
    An unset value (None/"") is the documented default, not a typo.
    """
    mode = str(value or "").strip().lower()
    if mode in MODES:
        return mode
    raw = "" if value is None else str(value)
    if raw.strip() and raw not in _UNKNOWN_MODE_LOGGED:
        _UNKNOWN_MODE_LOGGED.add(raw)
        log.warning("tool_injection: unknown tool_injection_screen %r — using %r", raw, DEFAULT_MODE)
    return DEFAULT_MODE


def _unique_families(result: ScanResult) -> list[str]:
    out: list[str] = []
    for hit in result.hits:
        if hit.family not in out:
            out.append(hit.family)
    return out


def _snippet(text: str, cap: int = 60) -> str:
    """One-line, hard-capped excerpt for a reason string. PURE."""
    return " ".join(text.split())[:cap]


def _entropy(text: str) -> float:
    """Shannon entropy in bits/char. PURE."""
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    total = len(text)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _scan_override(body: str, original: str, origin: list[int]) -> list[Hit]:
    out: list[Hit] = []
    for label, pattern in (
        ("instruction-override phrase", _OVERRIDE_RE),
        ("instruction-override phrase", _ABOVE_RE),
        ("role reassignment", _ROLE_ASSIGN_RE),
        ("replacement prompt", _NEW_PROMPT_RE),
    ):
        match = pattern.search(body)
        if match is not None:
            out.append(Hit(FAMILY_OVERRIDE, f"{label}: {_orig_snippet(match, original, origin)!r}", SEVERITY_HIGH))
    return out


def _scan_role_spoof(body: str, original: str, origin: list[int]) -> list[Hit]:
    out: list[Hit] = []
    for label, pattern in (
        ("chat-template control token", _CONTROL_TOKEN_RE),
        ("chat-template delimiter", _CHAT_TEMPLATE_RE),
        ("transcript role label", _ROLE_LABEL_RE),
        ("prompt-format tag", _PROMPT_TAG_RE),
    ):
        match = pattern.search(body)
        if match is not None:
            out.append(Hit(FAMILY_ROLE, f"{label}: {_orig_snippet(match, original, origin)!r}", SEVERITY_HIGH))
    return out


def _scan_provenance(body: str, original: str, origin: list[int]) -> list[Hit]:
    match = _PROVENANCE_RE.search(body)
    if match is None:
        return []
    return [
        Hit(
            FAMILY_PROVENANCE,
            f"forged gateway provenance header: {_orig_snippet(match, original, origin)!r}",
            SEVERITY_HIGH,
        )
    ]


def _scan_exfiltration(body: str, original: str, origin: list[int]) -> list[Hit]:
    url = _URL_RE.search(body)
    ip = _IPV4_RE.search(body)
    email = _EMAIL_RE.search(body)
    cred = _CRED_RE.search(body)
    send = _SEND_RE.search(body)
    destination = url if url is not None else ip
    out: list[Hit] = []
    if destination is not None and cred is not None:
        out.append(
            Hit(
                FAMILY_EXFIL,
                f"credential-shaped token beside a network destination"
                f" ({_orig_snippet(cred, original, origin)!r} +"
                f" {_orig_snippet(destination, original, origin)!r})",
                SEVERITY_LOW,
            )
        )
    if send is not None and (destination is not None or email is not None):
        target = destination if destination is not None else email
        assert target is not None  # the branch condition guarantees it
        out.append(
            Hit(
                FAMILY_EXFIL,
                f"instruction to send data to an external destination:"
                f" {_orig_snippet(send, original, origin)!r} ->"
                f" {_orig_snippet(target, original, origin)!r}",
                SEVERITY_LOW,
            )
        )
    elif send is not None and cred is not None:
        out.append(
            Hit(
                FAMILY_EXFIL,
                f"instruction to send credential-shaped data: {_orig_snippet(send, original, origin)!r}",
                SEVERITY_LOW,
            )
        )
    return out


def _scan_encoded(body: str) -> list[Hit]:
    for match in _B64_RE.finditer(body):
        blob = match.group(0)
        if (
            any(ch.isdigit() for ch in blob)
            and any(ch.isupper() for ch in blob)
            and any(ch.islower() for ch in blob)
            and _entropy(blob) >= _B64_MIN_ENTROPY
        ):
            return [Hit(FAMILY_ENCODED, f"high-entropy base64-looking blob ({len(blob)} chars)", SEVERITY_LOW)]
    for match in _HEX_RE.finditer(body):
        blob = match.group(0)
        if any(ch.isdigit() for ch in blob) and any(ch.isalpha() for ch in blob) and _entropy(blob) >= _HEX_MIN_ENTROPY:
            return [Hit(FAMILY_ENCODED, f"high-entropy hex blob ({len(blob)} chars)", SEVERITY_LOW)]
    return []


def scan(text: Any) -> ScanResult:
    """Screen ONE tool result. PURE, total, never raises.

    Returns every finding plus the aggregate severity: HIGH when any finding is
    high (a clear override, role spoof or provenance forgery), else LOW, else
    "none". Matching runs on a NORMALISED surface (see _match_surface) so
    unicode and newline evasions cannot hide a phrase; reasons quote the
    original span.
    """
    body = text if isinstance(text, str) else ("" if text is None else str(text))
    hits: list[Hit] = []
    if body:
        match_body, origin = _match_surface(body)
        if match_body:
            hits.extend(_scan_override(match_body, body, origin))
            hits.extend(_scan_role_spoof(match_body, body, origin))
            hits.extend(_scan_provenance(match_body, body, origin))
            hits.extend(_scan_exfiltration(match_body, body, origin))
            hits.extend(_scan_encoded(match_body))
    severity = SEVERITY_NONE
    for hit in hits:
        if _SEVERITY_RANK.get(hit.severity, 0) > _SEVERITY_RANK[severity]:
            severity = hit.severity
    return ScanResult(hits=tuple(hits), severity=severity)


def annotation_header(server: str, tool: str, result: ScanResult, token: str = "") -> str:
    """The one-line machine-readable provenance header for ANNOTATE. PURE.

    token is a PER-RESULT nonce: the tool text cannot predict it, so a
    hand-written copy of this header (the cross-validation P1 forgery) can
    never carry the real value. render() supplies a fresh one; an explicit
    token keeps the function deterministic for tests and audit replay.
    """
    families = ",".join(_unique_families(result)) or "-"
    nonce = token or secrets.token_hex(8)
    return (
        f"[pcai:tool-result-screen server={server} tool={tool}"
        f" severity={result.severity} hits={len(result.hits)} families={families}"
        f" token={nonce}]"
    )


def blocked_text(server: str, tool: str, result: ScanResult) -> str:
    """The refusal body for BLOCK. PURE and always non-empty.

    Names the server/tool and the finding families so the model can tell a
    policy refusal from a server failure, and keeps the role=tool result a
    valid, non-empty string (the tool call itself still succeeded).
    """
    families = ", ".join(_unique_families(result)) or "unspecified"
    return (
        f"error: the gateway withheld this tool result from {server}__{tool}:"
        f" its text matched the tool-result injection screen ({len(result.hits)} finding(s): {families})."
        " The tool call itself succeeded; request a narrower output if the content is genuinely needed."
    )


def render(text: Any, result: ScanResult, *, server: str, tool: str, mode: Any) -> str:
    """The text the MODEL sees for one result under `mode`. PURE.

    off / warn -> the text unchanged (warn's logging belongs to the caller);
    annotate   -> the header line PREPENDED, the content untouched;
    block      -> the refusal INSTEAD of the content.
    A clean result (severity "none") is never annotated or blocked: there is
    nothing to report, and the modes exist to describe FINDINGS.

    NOT byte-deterministic in ANNOTATE by design: the header carries a fresh
    random token per result (the provenance-forgery fix). Every other branch
    is pure.
    """
    body = text if isinstance(text, str) else ("" if text is None else str(text))
    resolved = resolve_mode(mode)
    if resolved in (MODE_OFF, MODE_WARN) or result.severity == SEVERITY_NONE:
        return body
    if resolved == MODE_ANNOTATE:
        return annotation_header(server, tool, result, token=secrets.token_hex(8)) + "\n" + body
    return blocked_text(server, tool, result)
