"""Deterministic tool-result compression (X-PCAI-Compress-Tools).

What these tests pin, in order of risk:

1. THE CONTRACT — no model, no network, deterministic, fail-open, never
   larger than its input, and idempotent (already-compressed text is never
   re-compressed);
2. EACH SELECTOR KEEPS ITS SIGNAL — error records, ERROR lines, the first
   matches per file, changed lines — and drops the redundancy;
3. THE RETRIEVAL SURFACE — paging, unknown/expired digest, catalog shape.
"""

from __future__ import annotations

import inspect
import json

import pytest

from pcai_llm.core import toolcompress


def _compact_json(items: list[dict]) -> str:
    return json.dumps(items, separators=(",", ":"))


def _records(n: int = 200) -> list[dict]:
    rows = [
        {"id": i, "path": f"/var/log/pod-{i}.log", "level": "info", "msg": "request completed in 12ms"}
        for i in range(n)
    ]
    if n > 137:
        rows[137] = {"id": 137, "path": "/var/log/pod-137.log", "level": "error", "msg": "connection refused"}
    return rows


# --- the contract ----------------------------------------------------------


def test_module_imports_no_model_or_network_client() -> None:
    """The design's non-negotiable: this module is pure. A future 'just use a
    small model for prose' change must fail HERE, not in production."""
    source = inspect.getsource(toolcompress)
    for forbidden in ("httpx", "openai", "anthropic", "aiohttp", "requests"):
        assert forbidden not in source, f"toolcompress must stay model-free (found {forbidden!r})"


def test_compress_is_deterministic() -> None:
    text = _compact_json(_records())
    first = toolcompress.compress_tool_text(text, min_chars=1000)
    second = toolcompress.compress_tool_text(text, min_chars=1000)
    assert first is not None and second is not None
    assert first.text == second.text
    assert first.stats.digest == second.stats.digest


def test_compress_never_grows_and_marks_the_digest() -> None:
    text = _compact_json(_records())
    result = toolcompress.compress_tool_text(text, min_chars=1000)
    assert result is not None
    assert len(result.text) < len(text)
    assert toolcompress.MARKER_SIG in result.text
    assert result.stats.digest in result.text
    assert result.stats.chars_after == len(result.text)
    assert result.stats.ratio > 0.15


def test_already_compressed_text_is_not_recompressed() -> None:
    text = _compact_json(_records())
    once = toolcompress.compress_tool_text(text, min_chars=1000)
    assert once is not None
    assert toolcompress.compress_tool_text(once.text, min_chars=0) is None


def test_small_payload_passes_through() -> None:
    text = _compact_json(_records(5))
    assert toolcompress.compress_tool_text(text, min_chars=4096) is None


def test_unknown_shape_passes_through() -> None:
    prose = "This is a plain prose answer. " * 400
    assert toolcompress.compress_tool_text(prose, min_chars=100) is None


def test_ratio_floor_rejects_marginal_savings() -> None:
    """A payload that shrinks only slightly must NOT be rewritten: the marker
    plus the retrieval risk costs more than the saved characters."""
    items = [{"id": i, "value": "x" * 40} for i in range(60)]
    text = _compact_json(items)
    # Force an implausibly high floor: nothing can clear it.
    assert toolcompress.compress_tool_text(text, min_chars=100, ratio_floor=0.99) is None


# --- selectors -------------------------------------------------------------


def test_large_payload_stays_inside_the_output_budget() -> None:
    """A 1 MB array must not compress to 250 KB and then get head-truncated by
    the relay's hard cap: the compressor's own absolute budget keeps the WHOLE
    result — including its tail — inside the model-facing bound."""
    text = _compact_json(_records(8000))
    result = toolcompress.compress_tool_text(text, min_chars=1000, budget_chars=8000)
    assert result is not None
    assert len(result.text) <= 8000 + 400  # marker allowance
    assert "connection refused" in result.text


def test_payload_that_cannot_fit_the_budget_passes_through() -> None:
    items = [{"id": i, "blob": "x" * 500} for i in range(50)]
    text = _compact_json(items)
    assert toolcompress.compress_tool_text(text, min_chars=1000, budget_chars=1200) is None


def test_json_array_keeps_error_and_boundary_records() -> None:
    text = _compact_json(_records())
    result = toolcompress.compress_tool_text(text, min_chars=1000)
    assert result is not None
    body = result.text.split(toolcompress.MARKER_SIG)[0]
    kept = json.loads(body)
    assert kept[0]["id"] == 0  # boundary head
    assert any(row.get("id") == 199 for row in kept)  # boundary tail
    assert any(row.get("msg") == "connection refused" for row in kept)
    assert "_pcai_omitted_items" in kept[-1]  # the sentinel closes the array
    assert len(kept) < len(_records())


def test_logs_keep_error_lines_and_drop_info_spam() -> None:
    lines = [f"2026-11-04T09:00:{i % 60:02d}Z INFO pod-{i} request completed" for i in range(200)]
    lines[57] = "2026-11-04T09:00:57Z ERROR pod-57 connection refused"
    lines.append("Traceback (most recent call last):")
    lines.append('  File "/app/main.py", line 3, in <module>')
    text = "\n".join(lines)
    result = toolcompress.compress_tool_text(text, min_chars=1000)
    assert result is not None
    assert "connection refused" in result.text
    assert "Traceback" in result.text
    assert result.text.count("request completed") < 10
    assert "lines omitted" in result.text


def test_search_keeps_first_matches_per_file() -> None:
    lines = []
    for i in range(80):
        lines.append(f"src/module_{i % 4}.py:{i}:match number {i}")
    text = "\n".join(lines)
    result = toolcompress.compress_tool_text(text, min_chars=500)
    assert result is not None
    assert "matches omitted" in result.text
    for module in range(4):
        assert result.text.count(f"src/module_{module}.py:") <= 2


def test_diff_drops_context_keeps_changes() -> None:
    lines = ["diff --git a/x.py b/x.py", "index 111..222 100644", "--- a/x.py", "+++ b/x.py", "@@ -1,6 +1,6 @@"]
    for i in range(40):
        lines.append(f" context line {i}")
        if i == 20:
            lines.append("+added line")
            lines.append("-removed line")
    text = "\n".join(lines)
    result = toolcompress.compress_tool_text(text, min_chars=500)
    assert result is not None
    assert "+added line" in result.text and "-removed line" in result.text
    assert "context lines omitted" in result.text
    assert text.count("context line") > result.text.count("context line")


def test_ndjson_keeps_error_records() -> None:
    lines = [json.dumps({"i": i, "level": "info", "msg": "ok"}) for i in range(120)]
    lines[71] = json.dumps({"i": 71, "level": "error", "msg": "timeout"})
    text = "\n".join(lines)
    result = toolcompress.compress_tool_text(text, min_chars=500)
    assert result is not None
    assert '"timeout"' in result.text
    assert "lines omitted" in result.text


# --- mode resolution -------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "default", "expected"),
    [
        (None, "off", "off"),
        (None, "on", "on"),
        ("", "on", "on"),
        ("1", "off", "on"),
        ("on", "off", "on"),
        ("true", "off", "on"),
        ("0", "on", "off"),
        ("off", "on", "off"),
        ("shadow", "off", "shadow"),
        ("SHADOW", "off", "shadow"),
        ("banana", "on", "off"),  # unknown header = OFF, never a 400
        (None, "banana", "off"),  # unknown deployment default = OFF
    ],
)
def test_resolve_mode(header: str | None, default: str, expected: str) -> None:
    assert toolcompress.resolve_mode(header, default) == expected


# --- retrieval surface -----------------------------------------------------


def test_retrieve_slice_pages_by_characters() -> None:
    text = "".join(chr(0x4E00 + (i % 100)) for i in range(1000))  # CJK: 1 char, 3 UTF-8 bytes
    first = toolcompress.retrieve_slice(text, offset=0, limit=400)
    assert first.startswith("[pcai retrieve: chars 0..400 of 1000]")
    assert "600 chars remain" in first
    second = toolcompress.retrieve_slice(text, offset=400, limit=400)
    assert second.startswith("[pcai retrieve: chars 400..800 of 1000]")
    assert text[0:400] in first and text[400:800] in second


def test_retrieve_slice_clamps_hostile_arguments() -> None:
    text = "abcdef"
    # A hostile limit floors at ONE character, never "everything".
    assert "chars 0..1 of 6" in toolcompress.retrieve_slice(text, offset=-5, limit=0)
    assert "chars 6..6 of 6" in toolcompress.retrieve_slice(text, offset=999, limit=10)
    assert "abcdef" in toolcompress.retrieve_slice(text, offset="nonsense", limit="nonsense")


def test_retrieve_unknown_digest_is_explicit() -> None:
    message = toolcompress.retrieve_unknown_digest("deadbeef")
    assert "deadbeef" in message
    assert "expired" in message


def test_retrieve_catalog_entry_shape() -> None:
    entry = toolcompress.retrieve_catalog_entry()
    assert entry["server"] == toolcompress.RETRIEVE_SERVER
    assert entry["tool"] == toolcompress.RETRIEVE_TOOL
    assert entry["inputSchema"]["required"] == ["digest"]
    assert "digest" in entry["inputSchema"]["properties"]


# --- the budget/cap invariant (review finding 1+2) -------------------------


def test_retrieval_page_fits_under_the_relay_cap() -> None:
    """A FULL page must keep its paging footer: the relay caps the final
    string at 32,000 chars, so a body sized at that cap would amputate
    exactly the "call again with offset=..." hint the model is told to
    follow."""
    from pcai_llm.core.toolrelay import RELAY_TOOL_RESULT_CAP_CHARS

    page = toolcompress.retrieve_slice("x" * 100_000, offset=0, limit=toolcompress.RETRIEVE_MAX_CHARS)
    assert len(page) <= RELAY_TOOL_RESULT_CAP_CHARS
    assert "call pcai__retrieve again with offset=" in page


def test_default_budget_stays_under_the_relay_cap() -> None:
    """The default budget is not a magic number: it must leave headroom for
    the marker under the relay's hard cap, or every rewrite would be
    head-truncated after the fact."""
    from pcai_llm.core.toolrelay import RELAY_TOOL_RESULT_CAP_CHARS

    assert toolcompress.DEFAULT_BUDGET_CHARS + 1_000 < RELAY_TOOL_RESULT_CAP_CHARS
    text = _compact_json(_records(8000))
    result = toolcompress.compress_tool_text(text, min_chars=1000)  # DEFAULT budget
    assert result is not None
    assert len(result.text) <= RELAY_TOOL_RESULT_CAP_CHARS


def test_forged_marker_passes_through() -> None:
    """A tool result embedding the marker signature suppresses its OWN
    compression (resource-only; the digest lookup is key-scoped, so a forged
    digest cannot reach another key's data)."""
    text = _compact_json(_records()) + "\n[... pcai compressed json_array: 1 of 2 chars omitted ...]"
    assert toolcompress.compress_tool_text(text, min_chars=100) is None
