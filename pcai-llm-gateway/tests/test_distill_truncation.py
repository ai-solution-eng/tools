"""Regression cover for the truncated-distill write (2026-10-23).

SYMPTOM: the memory store held distilled summaries that end mid-document --
one stops right after a Pytest count line (its audit findings never written),
one ends inside a Gotchas bullet ("- **PyMuPDF"), a third ends mid-sentence
("- **Deploy the RW-PVC policy volume"). All three carried
memory_truncated=false and single-chunk layout: the store received exactly
these bytes, so the cut happened UPSTREAM and every write gate passed it.

ROOT CAUSE: routes_distill.llm_once checked finish_reason == "length" ONLY in
the empty-content branch (the reasoning-trace burn). A NON-empty reply that
hit the max_tokens cap was returned as a successful distill, written whole,
and marked distilled.

THE FIX (three layers):
  1. routes_distill.llm_once raises core.distiller.DistillTruncatedError when
     a non-empty reply carries finish_reason="length" (or completion_tokens
     >= max_tokens) and settings.distiller_reject_truncated is on -- except
     on map-stage calls, where partial notes are legitimate intermediates.
  2. core.distiller.run_map_reduce raises the same error when the FINAL
     document is missing any of the four contract sections -- the structural
     net for models/proxies that drop finish_reason.
  3. config.distiller_max_tokens default 2048 -> 4096: the cap only binds
     when hit, so the raise is free; at 2048 it fired on live traffic.
"""

from __future__ import annotations

import pytest

from pcai_llm.core import distiller
from pcai_llm.core.distiller import (
    DistillTruncatedError,
    document_is_complete,
    run_map_reduce,
)

COMPLETE_DOC = (
    "[MEMORY INDEX | project: sqlhandler | type: session-summary | topics: audit]\n"
    "\n"
    "## What we did\n"
    "Audited the repo.\n"
    "\n"
    "## Key decisions & rationale\n"
    "1. Zero-write scope.\n"
    "\n"
    "## Gotchas\n"
    "- ConfigMap mounts are read-only.\n"
    "\n"
    "## How to proceed\n"
    "- Deploy the RW-PVC policy volume.\n"
)


# ---------------------------------------------------------------------------
# document_is_complete -- the structural net
# ---------------------------------------------------------------------------


def test_complete_document_passes():
    assert document_is_complete(COMPLETE_DOC) is True


def test_document_cut_mid_bullet_is_incomplete():
    """The exact live defect: #6 of the 2026-10-23 store review ends here."""
    cut = COMPLETE_DOC[: COMPLETE_DOC.index("- ConfigMap") + len("- **PyMuPDF")]
    assert document_is_complete(cut) is False


def test_document_cut_after_what_we_did_is_incomplete():
    """The exact live defect: #5 of the 2026-10-23 store review ends at the
    Pytest line -- What-we-did present, everything after missing."""
    cut = COMPLETE_DOC[: COMPLETE_DOC.index("## Key decisions")]
    assert document_is_complete(cut) is False


def test_non_string_is_incomplete():
    assert document_is_complete(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_map_reduce -- the final-call guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_final_call_missing_sections_raises_truncated():
    """A final reply missing sections raises DistillTruncatedError (the
    structural net for a model/proxy that dropped finish_reason)."""

    async def llm_once(system, user, max_tokens):
        # No ## Gotchas / ## How to proceed -- the audit-summary shape that
        # shipped truncated to production.
        return (
            "[MEMORY INDEX | project: sqlhandler | type: session-summary | "
            "topics: audit]\n\n## What we did\nRead-only audit. Pytest: "
            "1390 passed / 12 skipped / 0 failed"
        )

    with pytest.raises(DistillTruncatedError):
        await run_map_reduce("transcript body " * 10, llm_once)


@pytest.mark.asyncio
async def test_map_stage_partial_notes_are_not_rejected():
    """Map-stage calls produce partial notes BY DESIGN: a capped map call
    (finish_reason=length, non-empty) must sail through -- the exemption the
    _distill_map_stage marker exists for."""

    calls = []

    async def llm_once(system, user, max_tokens):
        calls.append({"map_stage": bool(getattr(llm_once, "_distill_map_stage", False)), "max_tokens": max_tokens})
        if system == distiller.MAP_SYSTEM_PROMPT:
            return "partial note " + "x" * max_tokens  # capped-map shape
        return COMPLETE_DOC

    doc = await run_map_reduce("word " * 60000, llm_once)  # forces map stage
    assert doc.startswith("[MEMORY INDEX")
    assert calls[0]["map_stage"] is True, "map calls must be marked as map stage"
    assert calls[-1]["map_stage"] is False, "final call must clear the marker"


@pytest.mark.asyncio
async def test_complete_final_document_is_returned():
    async def llm_once(system, user, max_tokens):
        return COMPLETE_DOC

    doc = await run_map_reduce("small transcript", llm_once)
    # run_map_reduce strips the reply; synthesize_memory_index is a no-op here
    assert doc == COMPLETE_DOC.rstrip()
