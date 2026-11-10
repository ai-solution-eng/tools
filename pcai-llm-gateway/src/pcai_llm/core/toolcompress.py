"""Deterministic, model-free compression of model-facing tool results.

WHY THIS EXISTS (2026-11-04 design review): a relayed tool result is
re-prefilled on every later iteration of the same turn, so one oversized
JSON dump or log file is paid for repeatedly. The gateway already caps
results at `RELAY_TOOL_RESULT_CAP_CHARS`, but a cap is blunt head-truncation:
it keeps the beginning and throws away the end, which is often where the
error is. This module replaces the blunt cap with *selection*: it keeps the
signal (error items, boundary context, changed lines, high-relevance
matches) and drops the redundancy, per content kind.

THE CONTRACT (non-negotiable):
  * NO model, NO network, NO ML. Pure functions of the input text only.
    That is what lets it run in the request path: microseconds, no key, no
    second billable request, and fully deterministic — the same bytes in
    produce the same bytes out, which is what keeps the provider's prefix
    cache warm across turns. A non-deterministic transform would invalidate
    the cache on every request and cost more than it saves.
  * FAIL OPEN. Unknown shape, failed parse, or a saving below the ratio
    floor returns None and the caller forwards the original text untouched.
  * REVERSIBLE. The caller stores the original under `digest_of(text)` and
    the marker tells the model how to fetch it back via `pcai__retrieve`.
    Compression without a retrieval path is a lossy cap with extra steps,
    so the two ship together.

WHAT IT IS NOT: a summarizer. It cannot say "the tests failed because the
migration ran twice" — that needs generation, and generation belongs in a
subagent where it can be reviewed and retried, not in the gateway's request
path. The gateway compresses structure; agents summarize meaning.

THREAT NOTE: a tool result may EMBED the marker signature. That only
suppresses its own compression (the substring check below) — a resource
effect, never a correctness one — and a forged digest can only make the
model ask `pcai__retrieve` for it. The lookup is key-scoped (a digest
resolves only for the key that produced it), so a forged digest returns the
explicit unknown-digest answer rather than another tenant's data.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

# Bump when a compressor's output changes. The marker carries the version so
# a mixed-version fleet is diagnosable, and the routes layer logs it.
COMPRESSOR_VERSION = "1"

# Synthetic retrieval surface. The model sees `pcai__retrieve` next to
# `logsearch__search_logs`; the relay intercepts it before MCP dispatch
# (it is not a granted tool and never leaves the process).
RETRIEVE_SERVER = "pcai"
RETRIEVE_TOOL = "retrieve"
# Deliberately BELOW the relay's model-facing cap (32,000 in toolrelay): the
# returned page is header + body + paging footer, so a body sized at the cap
# would push the footer past it and the relay's cap would amputate exactly
# the "call again with offset=..." hint the model is told to follow. 30,000
# leaves ~2,000 for the two notices.
RETRIEVE_MAX_CHARS = 30_000

MODES = ("off", "on", "shadow")

# Marker signature: recognised so already-compressed text is never
# re-compressed (idempotence) and so operators can grep a log for it.
MARKER_SIG = "[... pcai compressed "

_LEVEL_RE = re.compile(r"(?i)\b(ERROR|CRITICAL|FATAL|PANIC|WARN(?:ING)?|EXCEPTION|SEVERE)\b")
# Detection is broader than retention: a log whose lines are almost all INFO
# is still a log (and exactly the kind worth compressing), while the
# compressor keeps only the WARN+ lines.
_ANY_LEVEL_RE = re.compile(
    r"(?i)\b(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|CRITICAL|FATAL|PANIC|EXCEPTION|SEVERE)\b"
)
_TRACE_RE = re.compile(r"^(?:\s+(?:at |File \"|Traceback)|\.\.\.)")
_SUMMARY_RE = re.compile(r"(?i)\b(summary|totals?|passed|failed|skipped|elapsed|took|duration)\b")
_ERROR_RE = re.compile(
    r"(?i)\b(error|failed|failure|exception|fatal|panic|timeout|timed out|denied|refused|traceback|critical)\b"
)
_SEARCH_RE = re.compile(r"^(?P<path>[^\s:][^:]*):(?P<line>\d+):(?P<text>.*)$")
_DIFF_HDR_RE = re.compile(r"^(diff --git |--- |\+\+\+ |index |new file mode|deleted file mode)")
_DIFF_HUNK_RE = re.compile(r"^@@")

# Absolute ceiling on a compressed candidate. Must stay comfortably under
# the relay's model-facing cap (RELAY_TOOL_RESULT_CAP_CHARS = 32,000 in
# core/toolrelay) so a rewrite is never head-truncated afterwards; the test
# suite pins the headroom. A candidate that cannot fit passes through.
DEFAULT_BUDGET_CHARS = 24_000

# INPUT ceiling (audit 2026-11-10 P2-35). Compression runs SYNCHRONOUSLY on the
# event loop, and the json.loads/json.dumps of the candidate is the one
# unbounded cost in it: a multi-MB tool result must never be parsed at all.
# ABSOLUTE, not a multiple of budget_chars: budget_chars is a caller knob (a
# caller may ask for an 8,000-char result out of a 700 KB payload — that
# compression is exactly what this module is for), while the ceiling has to
# bound the PARSE. 1 MB matches the store's own default retention cap
# (config.tool_compress_max_store_chars): an original bigger than that can
# never be retained, so a rewrite would be discarded as "not_stored" anyway
# and the parse would be pure waste. A candidate over the ceiling PASSES
# THROUGH — the pre-feature behaviour, never a worse outcome.
MAX_INPUT_CHARS = 1_000_000

# Detection floors. Below these a payload is small enough that the marker
# costs more attention than the omission saves.
_MIN_JSON_ITEMS = 4
_MIN_LINES = 12


@dataclass(frozen=True)
class CompressStats:
    """Accounting for one compressed result (logged + emitted on the wire)."""

    kind: str
    digest: str
    chars_before: int
    chars_after: int

    @property
    def ratio(self) -> float:
        return 0.0 if self.chars_before <= 0 else 1.0 - (self.chars_after / self.chars_before)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "digest": self.digest,
            "before": self.chars_before,
            "after": self.chars_after,
            "version": COMPRESSOR_VERSION,
        }


@dataclass(frozen=True)
class CompressResult:
    """A rewritten tool result plus the accounting the caller stores/logs."""

    text: str
    stats: CompressStats


def resolve_mode(header_value: str | None, default: str) -> str:
    """Resolve the per-request mode from the header, falling back to the
    deployment default. Unknown header values are OFF, never a 400: a typo
    must not fail a turn, and the safe direction is uncompressed."""
    fallback = default.strip().lower() if isinstance(default, str) else "off"
    if fallback not in MODES:
        fallback = "off"
    if header_value is None or not str(header_value).strip():
        return fallback
    value = str(header_value).strip().lower()
    if value in ("1", "on", "true", "yes"):
        return "on"
    if value in ("0", "off", "false", "no"):
        return "off"
    if value == "shadow":
        return "shadow"
    return "off"


def digest_of(text: str) -> str:
    """Stable content address for the original text (the retrieval key).

    Keyed on the ORIGINAL, not the compressed form: the client re-sends the
    original every turn, so the digest — and therefore the marker — is
    identical across turns. A digest of the compressed form would churn.
    """
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def detect_kind(text: str) -> str | None:
    """Classify a tool result into a compressor kind, or None to pass through.

    Deliberately conservative: a false positive rewrites data the model
    needed, a false negative only costs the savings. Order matters — diffs
    contain '---' lines, search output can contain the word ERROR, and JSON
    detection must win before the line-based kinds.
    """
    if not text or len(text) < 64:
        return None
    stripped = text.lstrip()
    if stripped.startswith("[") and len(stripped) > 100:
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and len(parsed) >= _MIN_JSON_ITEMS:
            return "json_array"

    lines = text.splitlines()
    sample = [ln for ln in lines[:40] if ln.strip()]
    if len(sample) >= 5 and all(_looks_like_json_object(ln) for ln in sample[:5]):
        return "ndjson"
    if any(_DIFF_HDR_RE.match(ln) for ln in lines[:40]) and any(_DIFF_HUNK_RE.match(ln) for ln in lines[:200]):
        return "diff"
    search_hits = sum(1 for ln in sample if _search_match(ln) is not None)
    if search_hits >= max(5, len(sample) // 2):
        return "search"
    if len(lines) >= _MIN_LINES and sum(1 for ln in lines if _ANY_LEVEL_RE.search(ln)) >= 3:
        return "logs"
    return None


def _search_match(line: str) -> re.Match[str] | None:
    """A search-result line (`path:line:text`), or None.

    The bare regex is too generous: an ISO log line
    (`2026-11-04T09:00:00Z INFO ...`) also fits `x:y:z`. Requiring a
    path-shaped first token — no leading digit, and a slash or a dot — is
    what keeps grep detection from claiming every timestamped log.
    """
    match = _SEARCH_RE.match(line)
    if match is None:
        return None
    path = match.group("path")
    if not path or path[0].isdigit() or ("/" not in path and "." not in path):
        return None
    return match


def _looks_like_json_object(line: str) -> bool:
    line = line.strip()
    if not (line.startswith("{") and line.endswith("}")):
        return False
    try:
        return isinstance(json.loads(line), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def _is_error_item(item: Any) -> bool:
    """Whether one JSON/NDJSON record carries an error signal.

    Checks the conventional severity fields first, then any string value:
    the point is to keep the records a human would open, without a keyword
    list tuned to one tool's schema.
    """
    if isinstance(item, str):
        return bool(_ERROR_RE.search(item))
    if isinstance(item, dict):
        for key in ("level", "severity", "status", "result", "outcome", "type", "kind", "error"):
            value = item.get(key)
            if isinstance(value, str) and _ERROR_RE.search(value):
                return True
        return any(isinstance(v, str) and _ERROR_RE.search(v) for v in item.values())
    return False


def _marker(kind: str, before: int, after: int, digest: str) -> str:
    omitted = max(0, before - after)
    pct = 0 if before <= 0 else round(100.0 * omitted / before)
    return (
        "\n\n"
        + MARKER_SIG
        + f"{kind}: {omitted} of {before} chars omitted ({pct}%); digest={digest}. "
        + 'Full result: pcai__retrieve with {"digest": "'
        + digest
        + '"}. ...]'
        + "\n"
    )


def compress_tool_text(
    text: str,
    *,
    min_chars: int = 4096,
    ratio_floor: float = 0.15,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    max_input_chars: int = 0,
) -> CompressResult | None:
    """Compress one model-facing tool result, or return None to pass through.

    The ratio floor is measured on the FINAL text including the marker: a
    result that saves 8% but pays a 200-char marker to do it is not worth
    the model's attention or the retrieval risk.

    `budget_chars` is an ABSOLUTE ceiling on the candidate, deliberately
    below the relay's hard cap: a 1 MB payload compressed to 25% is still
    250 KB, and the cap would then head-truncate it — discarding the tail
    (and any late error record) exactly the way this module exists to avoid.
    A candidate that cannot fit the budget returns None (pass through), so
    the outcome is never worse than the pre-feature cap.

    `max_input_chars` is the INPUT ceiling (0 = MAX_INPUT_CHARS): a candidate
    above it is never parsed. The relay passes its store cap, because a
    rewrite it cannot retain is discarded.
    """
    if not isinstance(text, str) or len(text) < max(64, min_chars):
        return None
    if len(text) > (max_input_chars if max_input_chars > 0 else MAX_INPUT_CHARS):
        return None
    if MARKER_SIG in text:
        return None
    kind = detect_kind(text)
    if kind is None:
        return None
    digest = digest_of(text)
    compressor = _COMPRESSORS.get(kind)
    if compressor is None:
        return None
    budget = max(1200, budget_chars)
    try:
        candidate = compressor(text, digest, budget)
    except Exception:  # a compressor bug must never break a relay turn
        return None
    if not candidate or len(candidate) > budget:
        return None
    final = candidate + _marker(kind, len(text), len(candidate), digest)
    if len(final) >= len(text):
        return None
    if len(final) > len(text) * (1.0 - max(0.0, min(0.9, ratio_floor))):
        return None
    return CompressResult(
        text=final,
        stats=CompressStats(kind=kind, digest=digest, chars_before=len(text), chars_after=len(final)),
    )


# ---------------------------------------------------------------------------
# JSON arrays (MCP results: list of records)
# ---------------------------------------------------------------------------


def _compress_json_array(text: str, digest: str, budget_chars: int) -> str | None:
    try:
        items = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(items, list) or len(items) < _MIN_JSON_ITEMS:
        return None
    compact = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    # Fast path: whitespace alone may clear the floor on a pretty-printed
    # payload. It still gets the uniform marker from the caller (the marker
    # is also the idempotence check and the retrieval contract), and the
    # omitted chars it reports are, honestly, whitespace.
    if len(compact) <= min(len(text) * 0.8, budget_chars):
        return compact

    n = len(items)
    keep = [False] * n
    for index in (0, 1, n - 2, n - 1):
        keep[index] = True
    for index, item in enumerate(items):
        if _is_error_item(item):
            keep[index] = True

    budget = min(max(1200, int(len(text) * 0.25)), budget_chars)
    average = max(1, len(compact) // n)
    # Reserve room for the sentinel object and the separators: sizing the
    # subset at exactly `budget` would overshoot by the sentinel and fail the
    # caller's ceiling, turning a compressible payload into a pass-through.
    max_items = max(6, min(n, max(1, (budget - 256) // average)))
    kept = sum(keep)
    if kept < max_items:
        # Deterministic stride fill: evenly spaced records across the omitted
        # middle, never random (determinism is the cache contract).
        remaining = max_items - kept
        candidates = n - kept
        stride = max(1, candidates // max(1, remaining))
        for index in range(n):
            if kept >= max_items:
                break
            if not keep[index] and index % stride == 0:
                keep[index] = True
                kept += 1

    subset = [item for index, item in enumerate(items) if keep[index]]
    omitted = n - len(subset)
    if omitted <= 0:
        return None
    subset.append({"_pcai_omitted_items": omitted, "_pcai_digest": digest})
    return json.dumps(subset, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# NDJSON (one JSON object per line: streaming tool output)
# ---------------------------------------------------------------------------


def _compress_ndjson(text: str, _digest: str, _budget_chars: int) -> str | None:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < _MIN_LINES:
        return None
    parsed: list[Any] = []
    for line in lines:
        try:
            parsed.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            return None
    keep = [False] * len(lines)
    for index in range(min(2, len(lines))):
        keep[index] = True
    for index in range(max(0, len(lines) - 2), len(lines)):
        keep[index] = True
    for index, item in enumerate(parsed):
        if _is_error_item(item):
            keep[index] = True
    if sum(keep) >= len(lines) * 0.6:
        return None
    out: list[str] = []
    run = 0
    for index, line in enumerate(lines):
        if keep[index]:
            if run:
                out.append(f"[... {run} lines omitted ...]")
                run = 0
            out.append(line)
        else:
            run += 1
    if run:
        out.append(f"[... {run} lines omitted ...]")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Logs (build/test output)
# ---------------------------------------------------------------------------


def _compress_logs(text: str, _digest: str, _budget_chars: int) -> str | None:
    lines = text.splitlines()
    if len(lines) < _MIN_LINES:
        return None
    keep = [False] * len(lines)
    for index, line in enumerate(lines):
        if _LEVEL_RE.search(line) or _TRACE_RE.match(line) or _SUMMARY_RE.search(line):
            keep[index] = True
    for index in range(min(3, len(lines))):
        keep[index] = True
    for index in range(max(0, len(lines) - 3), len(lines)):
        keep[index] = True
    kept = sum(keep)
    if kept >= len(lines) * 0.6 or kept == 0:
        return None
    out: list[str] = []
    run = 0
    for index, line in enumerate(lines):
        if keep[index]:
            if run:
                out.append(f"[... {run} lines omitted ...]")
                run = 0
            out.append(line)
        else:
            run += 1
    if run:
        out.append(f"[... {run} lines omitted ...]")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Search results (grep / ripgrep)
# ---------------------------------------------------------------------------


def _compress_search(text: str, _digest: str, _budget_chars: int) -> str | None:
    lines = text.splitlines()
    per_path: dict[str, int] = {}
    out: list[str] = []
    omitted = 0
    kept = 0
    for line in lines:
        match = _search_match(line)
        if match is None:
            out.append(line)
            continue
        path = match.group("path")
        count = per_path.get(path, 0)
        is_signal = bool(_ERROR_RE.search(match.group("text")))
        # An ERROR/FATAL/DENIED match is kept UNCONDITIONALLY, even past the
        # 60-match cap (audit P3): the cap exists to bound the tail of ordinary
        # matches, and dropping the one line that says "permission denied" --
        # the reason the search is being read at all -- is the failure mode
        # this module exists to prevent. (A very noisy result can therefore
        # exceed the caller's budget and pass through uncompressed, which is
        # the documented fail-open.)
        if is_signal or (kept < 60 and count < 2):
            per_path[path] = count + 1
            out.append(line)
            kept += 1
        else:
            per_path[path] = count + 1
            omitted += 1
    if omitted == 0 or kept == 0:
        return None
    out.append(f"[... {omitted} matches omitted; {len(per_path)} files represented ...]")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Unified diffs
# ---------------------------------------------------------------------------


def _compress_diff(text: str, _digest: str, _budget_chars: int) -> str | None:
    lines = text.splitlines()
    out: list[str] = []
    # omitted = the CURRENT run of context lines (flushed as a sentinel);
    # total_omitted = everything dropped, which is what decides whether this
    # diff was compressed at all. They must be separate counters (audit
    # P2-23): the flush resets `omitted` to 0, so the old `omitted == 0`
    # test asked "was the LAST line omitted?" -- a diff ending on "+new"
    # (the normal case) returned None and was never compressed, while the
    # same diff ending on a context line compressed.
    omitted = 0
    total_omitted = 0
    for line in lines:
        if _DIFF_HDR_RE.match(line) or _DIFF_HUNK_RE.match(line) or line.startswith(("+", "-")):
            if omitted:
                out.append(f"[... {omitted} context lines omitted ...]")
                omitted = 0
            out.append(line)
        else:
            omitted += 1
            total_omitted += 1
    if omitted:
        out.append(f"[... {omitted} context lines omitted ...]")
    if not out or total_omitted == 0:
        return None
    return "\n".join(out)


_COMPRESSORS = {
    "json_array": _compress_json_array,
    "ndjson": _compress_ndjson,
    "logs": _compress_logs,
    "search": _compress_search,
    "diff": _compress_diff,
}


# ---------------------------------------------------------------------------
# Retrieval surface
# ---------------------------------------------------------------------------


def retrieve_catalog_entry() -> dict[str, Any]:
    """The catalog entry for the synthetic `pcai__retrieve` tool.

    Offered only while compression is ON for the request, so a client that
    never opts in never sees a tool it cannot use. Same shape as an MCP
    catalog row (tools_payload reads server/tool/description/inputSchema).
    """
    return {
        "server": RETRIEVE_SERVER,
        "tool": RETRIEVE_TOOL,
        "description": (
            "Fetch the full text of a tool result the gateway compressed. "
            "Compressed results carry a marker with a digest; pass that digest here. "
            "Returns a character window so a very large result can be paged with offset/limit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "digest": {"type": "string", "description": "Digest from the compression marker."},
                "offset": {"type": "integer", "description": "Start character (default 0)."},
                "limit": {"type": "integer", "description": f"Max characters (default/cap {RETRIEVE_MAX_CHARS})."},
            },
            "required": ["digest"],
        },
    }


def retrieve_slice(text: str, *, offset: Any = 0, limit: Any = RETRIEVE_MAX_CHARS) -> str:
    """A bounded character window of a stored original, with paging hints.

    Char (code point) slicing, not bytes: the store round-trips UTF-8 text,
    and a byte offset would split a multi-byte character and corrupt the
    model's view. Same discipline as the harness's own spill notices.
    """
    total = len(text)
    try:
        start = max(0, int(offset))
    except (TypeError, ValueError):
        start = 0
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = RETRIEVE_MAX_CHARS
    size = max(1, min(size, RETRIEVE_MAX_CHARS))
    start = min(start, total)
    end = min(total, start + size)
    header = f"[pcai retrieve: chars {start}..{end} of {total}]\n"
    body = text[start:end]
    footer = ""
    if end < total:
        footer = f"\n[... {total - end} chars remain; call pcai__retrieve again with offset={end} ...]"
    return header + body + footer


def retrieve_unknown_digest(digest: str) -> str:
    """The model-facing answer when a digest is unknown or expired.

    Explicit about the cause: an expired entry and a typo look identical from
    the model's side, and guessing "it was probably truncated" invites a
    confabulated reconstruction.
    """
    return (
        f"error: no stored result for digest {digest!r} — it may have expired "
        "(the gateway keeps compressed originals for a bounded window) or the digest was mistyped. "
        "Re-run the original tool call if you still need the data."
    )
