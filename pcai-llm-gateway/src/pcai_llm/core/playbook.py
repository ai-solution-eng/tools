"""Playbook emission contract for the PCAI-LLM gateway distiller (Feature A).

PURE, STDLIB-ONLY, importable WITHOUT pcai_llm: this module is the single
source of truth for the overlay that teaches the gateway distiller to emit an
OPTIONAL, structured, independently retrievable "## Playbook" section.
apply_playbook_patch.py copies it VERBATIM into the gateway tree as
src/pcai_llm/core/playbook.py, so edit HERE and re-run the patcher - never
edit the copied file.

Design provenance: PLAYBOOK-MEMORY-DESIGN.md (2026-11-10) and the feature
wave contract, section "Feature A". A playbook is a fenced-JSON object under
a "## Playbook" markdown heading appended after the four required sections.
The section is OPTIONAL - the four DSH contract sections stay the only
required ones, so document_is_complete() needs no change - and gated on
session shape WHENEVER the trace carries tool data (assistant tool_calls
blocks or role=tool rows): at least MIN_TOOL_CALLS tool calls and a clean
terminal status. A trace without tool data (e.g. the gateway conversation
store's {seq, role, content, ts} rows) gives the structural gate nothing to
judge, so it stays eligible and the prompt-side gate governs. The outcome
label is INFERRED from trace signals only - "inferred" is never stamped on
a model-supplied outcome; where nothing was inferable the model's claim may
be stored but carries outcome_source "unverified".
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# The prompt version the patched distiller stamps. The overlay bumps the
# gateway's dsh-2026-10-09 to this value (apply_playbook_patch.py, edit 2).
PROMPT_VERSION_BUMP = "dsh-2026-11-10"

# The optional section header the patched prompt may add below the four
# required ones. document_is_complete() checks REQUIRED_SECTIONS only, so a
# document carrying this section still passes completeness UNCHANGED.
OPTIONAL_SECTIONS: tuple[str, ...] = ("## Playbook",)

# The exact JSON keys of a playbook object. validate_playbook REJECTS extra
# keys: an unvalidated extra field would flow into memory metadata unchecked.
PLAYBOOK_KEYS: tuple[str, ...] = (
    "task_signature",
    "applies_when",
    "steps",
    "failure_modes",
    "outcome",
)

OUTCOME_SUCCESS = "success"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_UNKNOWN = "unknown"

# "inferred" is RESERVED for labels derived from trace signals - and only a
# trace that actually carries tool data can yield one. A model-supplied
# outcome may be STORED when the trace is absent or carries no tool data,
# but it is stamped "unverified", NEVER "inferred" (lead ruling F1/F2: a
# self-assessed label wearing "inferred" is the exact failure the field
# exists to prevent).
OUTCOME_SOURCE = "inferred"
OUTCOME_UNVERIFIED = "unverified"

# Playbook JSON shape bounds (contract, section Feature A).
SIGNATURE_MIN_CHARS = 8
SIGNATURE_MAX_CHARS = 200
APPLIES_WHEN_MIN, APPLIES_WHEN_MAX = 1, 8
STEPS_MIN, STEPS_MAX = 1, 15
FAILURE_MODES_MIN, FAILURE_MODES_MAX = 0, 8

# Emission gate (design note section 3, contract section Feature A).
MIN_TOOL_CALLS = 4
# A "denied-tool storm": this many denied tool RESULTS mark the terminal
# state dirty. Below the threshold a couple of denials are ordinary work.
DENIED_STORM_RESULTS = 3
# The same (tool, arguments) pair issued this many times is a loop signal.
REPEATED_CALL_THRESHOLD = 3
# OUTCOME thresholds are BROADER than the gate (design note section 4: the
# gate tolerates a couple of denials / one repeat as ordinary work, but the
# inferred label treats them as abandoned). This split is what makes an
# ELIGIBLE session able to carry an "abandoned" label - the server-side
# override the label exists for.
OUTCOME_DENIED_RESULTS = 1
OUTCOME_REPEATED_CALLS = 2
# Tool-error ratio signals (design note section 4) need a minimum sample.
MIN_RESULTS_FOR_ERROR_RATIO = 4
MAX_TOOL_ERROR_RATIO = 0.5

# Substring markers, matched casefolded. Deliberately TIGHT lists: a false
# positive here only omits an optional section (conservative), but a false
# negative would label a broken session "success".
BUDGET_MARKERS: tuple[str, ...] = ("budget_exceeded", "budget exceeded")
DENIED_MARKERS: tuple[str, ...] = (
    "permission denied",
    "access denied",
    "not allowed",
    "policy denial",
)
RESULT_ERROR_MARKERS: tuple[str, ...] = (
    "traceback (most recent call last",
    "error:",
    "exception",
    "failed:",
    "command failed with",
    "exit code 1",
    "permission denied",
    "access denied",
    "not allowed",
)

# The fence is composed from chr(96) so this SOURCE file stays free of the
# fence character; the RUNTIME strings below still carry real fences.
_FENCE = chr(96) * 3

PLAYBOOK_PROMPT: str = (
    "\n"
    "OPTIONAL PLAYBOOK SECTION (emit ONLY when the session qualifies):\n"
    "- Qualifying session: at least 4 tool calls AND a clean terminal state - no\n"
    "  budget_exceeded, no denied-tool storm, no repeated identical failing call.\n"
    "- Otherwise OMIT the section entirely; a missing playbook is always correct,\n"
    "  a forced or invented one is worse than none.\n"
    "- When it qualifies, append AFTER '## How to proceed' as the LAST section,\n"
    "  exactly this fenced shape and no other keys:\n"
    "## Playbook\n"
    + _FENCE
    + "json\n"
    + '{"task_signature": "<one line, 8-200 chars: the class of task this is for>",\n'
    + ' "applies_when": ["<observable precondition>", ...],\n'
    + ' "steps": ["<imperative, concrete step>", ...],\n'
    + ' "failure_modes": ["<named trap>", ...],\n'
    + ' "outcome": "success" or "abandoned"}\n'
    + _FENCE
    + "\n"
    "- outcome MUST be inferred from trace signals in the transcript (budget_exceeded,\n"
    '  denied tool calls, repeated identical calls, failed commands => "abandoned"),\n'
    "  NEVER from your self-assessment of the work.\n"
    "- Never invent: if the transcript does not establish a reusable procedure, omit\n"
    "  the section.\n"
)


def playbook_key(task_signature: str) -> str:
    """sha256 of the normalized task signature (lowercase hex, 64 chars).

    NORMALIZATION IS A CROSS-REPO CONTRACT: src/multimodal_rag/playbook.py
    (Feature B, the store/recall side) computes the SAME digest with the
    SAME code - lowercase, whitespace-collapsed, stripped - and supersedes
    prior playbooks by this key. Never change one side without the other.
    Empty/None hashes the empty string (Feature B's pinned convention).
    """
    normalized = re.sub(r"\s+", " ", str(task_signature or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_playbook(obj: dict) -> list[str]:
    """Every contract violation of one playbook object; [] == valid.

    NEVER raises - any fault (including a non-dict argument) comes back as a
    one-element error list. Checks: exact key set (extra keys rejected),
    task_signature a single stripped line of 8..200 chars, applies_when 1..8
    non-empty strings, steps 1..15 non-empty strings, failure_modes 0..8
    non-empty strings, outcome exactly "success" or "abandoned".
    """
    try:
        return _validate_playbook(obj)
    except Exception as exc:  # pragma: no cover - defensive: never raise
        return ["validation fault: " + type(exc).__name__]


def _validate_playbook(obj: dict) -> list[str]:
    if not isinstance(obj, dict):
        return ["playbook must be a JSON object"]
    errors: list[str] = []
    keys = set(obj)
    missing = [k for k in PLAYBOOK_KEYS if k not in keys]
    extra = sorted(keys - set(PLAYBOOK_KEYS))
    if missing:
        errors.append("missing keys: " + ", ".join(missing))
    if extra:
        errors.append("unexpected keys: " + ", ".join(extra))

    sig = obj.get("task_signature")
    if isinstance(sig, str):
        if not sig.strip():
            errors.append("task_signature is blank")
        elif sig != sig.strip():
            errors.append("task_signature has leading/trailing whitespace")
        elif not SIGNATURE_MIN_CHARS <= len(sig) <= SIGNATURE_MAX_CHARS:
            errors.append(
                "task_signature must be "
                + str(SIGNATURE_MIN_CHARS)
                + ".."
                + str(SIGNATURE_MAX_CHARS)
                + " chars, got "
                + str(len(sig))
            )
        if "\n" in sig or "\r" in sig:
            errors.append("task_signature must be a single line")
    elif "task_signature" in keys:
        errors.append("task_signature must be a string")

    for field_name, lo, hi in (
        ("applies_when", APPLIES_WHEN_MIN, APPLIES_WHEN_MAX),
        ("steps", STEPS_MIN, STEPS_MAX),
        ("failure_modes", FAILURE_MODES_MIN, FAILURE_MODES_MAX),
    ):
        value = obj.get(field_name)
        if field_name not in keys:
            continue  # a missing key is already reported above
        if not isinstance(value, list):
            errors.append(field_name + " must be a list of strings")
            continue
        if not lo <= len(value) <= hi:
            errors.append(field_name + " must have " + str(lo) + ".." + str(hi) + " entries, got " + str(len(value)))
        for item in value:
            if not isinstance(item, str) or not item.strip():
                errors.append(field_name + " entries must be non-empty strings")
                break

    outcome = obj.get("outcome")
    if "outcome" in keys and outcome not in (OUTCOME_SUCCESS, OUTCOME_ABANDONED):
        errors.append('outcome must be "success" or "abandoned"')
    return errors


def parse_playbook(doc: str) -> dict | None:
    """The FIRST VALID playbook object in a distilled document, or None.

    EVERY "## Playbook" header line is considered (cosmetic bold/colon
    variants forgiven, mirroring the distiller's own _header_matches), each
    with the fenced block directly under it, and the first one that parses
    AND validates wins - a decoy invalid section cannot shadow a valid one
    (lead ruling F3). Returns None - never raises - when there is no
    section at all, no section's fence yields JSON, or none validates.
    """
    if not isinstance(doc, str):
        return None
    try:
        lines = doc.splitlines()
        for start in _playbook_header_indices(lines):
            body = _fenced_body(lines, start)
            if body is None:
                continue
            try:
                obj = json.loads(body)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if validate_playbook(obj):
                continue
            return obj
    except Exception:  # pragma: no cover - defensive: never raise
        return None
    return None


def strip_playbook_section(doc: str) -> str:
    """doc with EVERY Playbook section removed; byte-identical when absent.

    EVERY "## Playbook" section goes - a decoy section must not smuggle
    model-authored text into the stored summary (lead ruling F3). A section
    spans its header line through the line before the next "## " heading,
    or EOF, whichever comes first - one uniform extent, so a well-formed
    section, an unfenced one, and an unclosed-fence decoy all strip the
    same way, and a broken decoy can no longer swallow following REQUIRED
    sections the way a to-EOF rule would. Only the seams around the removed
    spans are touched: each orphaned blank-line run collapses to one
    separator, and the document keeps a single trailing newline iff the
    original had one. A non-string input returns "" (never raises - the
    memory-write path calls this first).
    """
    if not isinstance(doc, str):
        return ""
    try:
        out = doc
        while True:
            lines = out.split("\n")
            start = _playbook_header_index(lines)
            if start is None:
                break
            end = _section_end_line(lines, start)
            before, after = lines[:start], lines[end:]
            while before and after and not before[-1].strip() and not after[0].strip():
                after.pop(0)
            candidate = "\n".join(before + after)
            if candidate == out:  # defensive: no progress - bail, never loop
                break
            out = candidate
        if out == doc:
            return doc
        if not out.strip():
            return ""
        out = out.rstrip()
        if doc.endswith("\n"):
            out += "\n"
        return out
    except Exception:  # pragma: no cover - defensive: never break a write
        return doc


def format_playbook_section(playbook: dict) -> str:
    """The fenced '## Playbook' markdown section for one playbook object.

    The exact bytes the memory-write overlay stores as the playbook memory
    body and the shape parse_playbook parses back. The fence is composed
    from chr(96) so this source file stays free of the fence character.
    """
    fence = chr(96) * 3
    return "## Playbook\n" + fence + "json\n" + json.dumps(playbook, sort_keys=True) + "\n" + fence


# ---------------------------------------------------------------------------
# Emission gate + outcome inference (trace signals over message rows)
# ---------------------------------------------------------------------------


def emission_eligible(messages: list[dict]) -> bool:
    """True when a session qualifies for playbook emission.

    The STRUCTURAL gate applies only to traces that actually carry tool
    data - an assistant tool_calls block or a role=tool row (lead ruling
    F1): at least MIN_TOOL_CALLS tool calls AND a clean terminal status -
    no budget_exceeded marker, no denied-tool storm (DENIED_STORM_RESULTS
    denied results), no repeated identical call (REPEATED_CALL_THRESHOLD
    identical (name, arguments) pairs).

    A trace with NO tool data (e.g. the gateway's conversation store, whose
    rows are {seq, role, content, ts} only) gives the gate NOTHING to
    judge: every structural rule is indeterminate there, so the session
    stays ELIGIBLE and the prompt-side gate governs. False is reserved for
    "tool data exists AND the bar is not met" - never for "I could not
    tell". Never raises.

    Tool calls are counted from assistant tool_calls entries when the
    transcript carries any (the canonical store shape); a transcript with
    only role=tool rows falls back to counting those rows, so both capture
    shapes gate the same way and a call is never counted twice.
    """
    if not isinstance(messages, list):
        return False  # not a trace at all: conservatively ineligible
    try:
        s = _session_signals(messages)
    except Exception:  # pragma: no cover - defensive
        return False
    if not s["has_tool_data"]:
        return True  # indeterminate: nothing to judge - the prompt gate governs
    if s["budget_marker"]:
        return False
    if s["denied_results"] >= DENIED_STORM_RESULTS:
        return False
    if s["max_repeated_call"] >= REPEATED_CALL_THRESHOLD:
        return False
    return s["tool_calls"] >= MIN_TOOL_CALLS


def infer_outcome(messages: list[dict]) -> tuple[str, str]:
    """(outcome, outcome_source) inferred from trace signals; never raises.

    Requires the trace to actually carry tool data (lead ruling F1/F2): a
    trace with NO assistant tool_calls block and no role=tool row has
    nothing to infer from, so the answer is (OUTCOME_UNKNOWN,
    OUTCOME_UNVERIFIED) and the CALLER may store the model's claimed
    outcome stamped "unverified" - a self-assessed label must never wear
    "inferred". With tool data: "abandoned" when any dirty signal fires -
    deliberately BROADER than the emission gate (see OUTCOME_DENIED_RESULTS
    / OUTCOME_REPEATED_CALLS): a budget marker, ANY denied tool result, a
    repeated identical call, or a tool-error ratio above
    MAX_TOOL_ERROR_RATIO with at least MIN_RESULTS_FOR_ERROR_RATIO
    results; otherwise ("success", "inferred") - the label stays advisory
    ranking evidence, not a verified grade.
    """
    if not isinstance(messages, list):
        return (OUTCOME_UNKNOWN, OUTCOME_UNVERIFIED)  # not a trace: nothing inferable
    try:
        s = _session_signals(messages)
    except Exception:  # pragma: no cover - defensive
        return (OUTCOME_UNKNOWN, OUTCOME_UNVERIFIED)
    if not s["has_tool_data"]:
        return (OUTCOME_UNKNOWN, OUTCOME_UNVERIFIED)
    abandoned = bool(s["budget_marker"]) or s["denied_results"] >= OUTCOME_DENIED_RESULTS
    abandoned = abandoned or s["max_repeated_call"] >= OUTCOME_REPEATED_CALLS
    abandoned = abandoned or (
        s["tool_results"] >= MIN_RESULTS_FOR_ERROR_RATIO and s["error_ratio"] > MAX_TOOL_ERROR_RATIO
    )
    return (OUTCOME_ABANDONED if abandoned else OUTCOME_SUCCESS, OUTCOME_SOURCE)


def _session_signals(messages: list[dict]) -> dict[str, Any]:
    """One scan over the transcript rows; the two gate functions share it."""
    entries = _tool_call_entries(messages)
    results = _tool_result_texts(messages)
    call_count = len(entries) if entries else len(results)
    # F1: the structural signals mean something ONLY where tool data exists.
    # The conversation store's rows ({seq, role, content, ts}) carry neither
    # tool_calls blocks nor role=tool rows, so on such traces every rule
    # below is indeterminate: the gate must not block, and the outcome must
    # be "unverified", never "inferred".
    has_tool_data = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        calls = msg.get("tool_calls")
        if (isinstance(calls, list) and bool(calls)) or str(msg.get("role") or "") == "tool":
            has_tool_data = True
            break

    all_text: list[str] = []
    for msg in messages:
        if isinstance(msg, dict):
            all_text.extend(_message_texts(msg))
    joined = "\n".join(all_text).casefold()

    budget = next((m for m in BUDGET_MARKERS if m in joined), None)
    denied = sum(1 for t in results if any(m in t.casefold() for m in DENIED_MARKERS))
    repeats: dict[tuple[str, str], int] = {}
    for entry in entries:
        repeats[entry] = repeats.get(entry, 0) + 1
    errors = sum(1 for t in results if any(m in t.casefold() for m in RESULT_ERROR_MARKERS))
    return {
        "tool_calls": call_count,
        "tool_results": len(results),
        "has_tool_data": has_tool_data,
        "budget_marker": budget,
        "denied_results": denied,
        "max_repeated_call": max(repeats.values()) if repeats else 0,
        "error_results": errors,
        "error_ratio": (errors / len(results)) if results else 0.0,
    }


def _texts(value: Any) -> list[str]:
    """The plain strings of a message content value, across both shapes."""
    out: list[str] = []
    if isinstance(value, str):
        if value:
            out.append(value)
    elif isinstance(value, list):
        for block in value:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    out.append(text)
    return out


def _message_texts(msg: dict) -> list[str]:
    """Everything one message row can say: content plus tool-call text."""
    texts = _texts(msg.get("content"))
    calls = msg.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if isinstance(call, dict):
                fn = call.get("function")
                if isinstance(fn, dict):
                    name = fn.get("name")
                    if isinstance(name, str):
                        texts.append(name)
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        texts.append(args)
                    elif args is not None:
                        texts.append(json.dumps(args, default=str))
    return texts


def _tool_call_entries(messages: list[dict]) -> list[tuple[str, str]]:
    """(name, whitespace-normalized arguments) of every assistant tool call."""
    entries: list[tuple[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        calls = msg.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            fn = fn if isinstance(fn, dict) else {}
            name = str(fn.get("name") or "tool")
            args = fn.get("arguments")
            if isinstance(args, str):
                args_text = args
            elif args is None:
                args_text = ""
            else:
                try:
                    args_text = json.dumps(args, sort_keys=True, default=str)
                except Exception:
                    args_text = str(args)
            entries.append((name, " ".join(args_text.split())))
    return entries


def _tool_result_texts(messages: list[dict]) -> list[str]:
    """Non-empty text of every role=tool result row."""
    texts: list[str] = []
    for msg in messages:
        if isinstance(msg, dict) and str(msg.get("role") or "") == "tool":
            texts.extend(t for t in _texts(msg.get("content")) if t.strip())
    return texts


# ---------------------------------------------------------------------------
# Section parsing internals (header/fence location shared by parse + strip)
# ---------------------------------------------------------------------------

_PLAYBOOK_HEADER_RE = re.compile(r"^#{1,3}\s*\*{0,2}\s*Playbook\*{0,2}\s*:?\s*$")
_FENCE_LINE_RE = re.compile(r"^\s*" + re.escape(_FENCE) + r"[A-Za-z0-9_-]*\s*$")
# The section EXTENT breaks on a "## " heading exactly (the document's
# section grammar; the distiller's own _section_headers uses the same
# prefix) - so h1/h3 lines INSIDE a section do not cut it short and model
# prose under a broken fence cannot survive the strip.
_NEXT_HEADER_RE = re.compile(r"^##\s")


def _playbook_header_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if _PLAYBOOK_HEADER_RE.match(line):
            return i
    return None


def _playbook_header_indices(lines: list[str]) -> list[int]:
    """EVERY Playbook header line index, in document order (lead ruling F3)."""
    return [i for i, line in enumerate(lines) if _PLAYBOOK_HEADER_RE.match(line)]


def _fenced_body(lines: list[str], start: int) -> str | None:
    """The JSON text inside the fence directly under the header, or None."""
    open_idx = None
    for i in range(start + 1, len(lines)):
        if _FENCE_LINE_RE.match(lines[i]):
            open_idx = i
            break
        if lines[i].strip():
            return None  # prose before the fence: not the contract shape
    if open_idx is None:
        return None
    for i in range(open_idx + 1, len(lines)):
        if _FENCE_LINE_RE.match(lines[i]):
            return "\n".join(lines[open_idx + 1 : i])
    return None  # unclosed fence


def _section_end_line(lines: list[str], start: int) -> int:
    """Exclusive end line index of the Playbook section starting at start.

    One uniform extent (lead ruling F3): the section runs to the line
    before the next "## " heading, or EOF. A well-formed section's fence
    closes before that line anyway; an unclosed-fence decoy is capped at
    the next heading instead of swallowing following REQUIRED sections.
    """
    for i in range(start + 1, len(lines)):
        if _NEXT_HEADER_RE.match(lines[i]):
            return i
    return len(lines)


def _next_header_line(lines: list[str], start: int) -> int:
    """The next "## " heading line at or after start+1, or EOF."""
    for i in range(start + 1, len(lines)):
        if _NEXT_HEADER_RE.match(lines[i]):
            return i
    return len(lines)
