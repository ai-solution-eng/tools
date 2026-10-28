"""Differential test: the fast masking path must equal the sequential loop.

WHY: _mask_keywords was an O(keywords x text) per-keyword re.subn loop — 50
keywords over a 1.5 MB prompt is 50 full regex passes and ~340-600 ms of
synchronous event-loop stall. It now takes a single span pass when that is
PROVABLY equivalent, and otherwise runs the original loop unchanged.

This file pins the property that matters: for ANY input the public function
returns exactly what the original loop returned. The reference below is a
verbatim copy of the pre-optimisation body and is the oracle.
"""

from __future__ import annotations

import random
import re

import pytest

from pcai_llm.core.guardrails import (
    MASK,
    _mask_by_spans,
    _mask_keywords,
    _simple_keywords,
)


def reference_mask(text: str, keywords: list[str]) -> tuple[str, int]:
    """The ORIGINAL implementation, kept as the oracle. Do not 'fix' this."""
    count = 0
    for kw in keywords:
        text, n = re.subn(re.escape(kw), MASK, text, flags=re.IGNORECASE)
        count += n
    return text, count


# Keyword sets as shipped by deployments / used across the test suite.
SHIPPED_SETS = [
    ["alpha", "beta"],
    ["c++"],
    ["secret"],
    ["x"],
    ["email", "phone", "ssn"],
    ["Project X", "codename"],
]

# One keyword is a substring of another, or occurs inside MASK, or is empty:
# the loop's ordering is observable, so these MUST take the fallback.
INTERACTING_SETS = [
    ["bc", "abc"],
    ["abc", "bc"],
    ["X", "bX"],
    ["a", "ab"],
    ["redacted", "act"],
    ["", "alpha"],
    ["RED", "REDACTED"],
]

CORPUS = [
    "",
    "abc",
    "abcd",
    "aXbXc",
    "ab",
    "The C++ and c++ here",
    "case CASE Case",
    "a.b.c",
    "abcabc",
    "nothing to see",
    "ALPHA and beta and ALPHA",
    "x marks the x",
    "[REDACTED] already masked",
    "secret secret secret",
    "unicode: caf\u00e9 \u4f60\u597d \U0001f600 secret",
    "newlines\nand\ttabs\rand secret\n",
    "c++ / c++ / C++",
]


@pytest.mark.parametrize("keywords", SHIPPED_SETS + INTERACTING_SETS)
@pytest.mark.parametrize("text", CORPUS)
def test_optimised_matches_reference(text: str, keywords: list[str]) -> None:
    assert _mask_keywords(text, keywords) == reference_mask(text, keywords)


def test_empty_keyword_list_is_a_noop() -> None:
    assert _mask_keywords("anything", []) == ("anything", 0)


def test_shipped_sets_take_the_fast_path() -> None:
    """A regression here means the optimisation silently stopped applying."""
    for kws in SHIPPED_SETS:
        assert _simple_keywords(tuple(kws)) is not None, kws


def test_interacting_sets_take_the_fallback() -> None:
    """These sets are order-sensitive; they MUST use the sequential loop."""
    for kws in INTERACTING_SETS:
        assert _simple_keywords(tuple(kws)) is None, kws


def test_duplicate_keywords_collapse_and_stay_exact() -> None:
    kws = ["alpha", "alpha", "beta"]
    assert _simple_keywords(tuple(kws)) == ("alpha", "beta")
    assert _mask_keywords("alpha beta", kws) == reference_mask("alpha beta", kws)


def test_case_variant_duplicates_collapse_and_stay_exact() -> None:
    """["c++","C++"] are the same keyword under IGNORECASE; masking twice is
    idempotent, so collapsing them to one pass is exact."""
    kws = ["c++", "C++"]
    assert _simple_keywords(tuple(kws)) == ("c++",)
    assert _mask_keywords("The C++ and c++ here", kws) == reference_mask("The C++ and c++ here", kws)


def test_overlapping_occurrences_fall_back_and_stay_exact() -> None:
    """No keyword is a substring of another, but they OVERLAP in the text:
    masking "abc" then "bcd" on "abcd" leaves "[REDACTED]d", which one
    combined pass cannot reproduce — so the span pass must decline."""
    kws = ["abc", "bcd"]
    assert _simple_keywords(tuple(kws)) is not None
    assert _mask_by_spans("abcd", _simple_keywords(tuple(kws))) is None
    assert _mask_keywords("abcd", kws) == reference_mask("abcd", kws) == ("[REDACTED]d", 1)


def test_validation_is_cached() -> None:
    kws = ("alpha", "beta")
    assert _simple_keywords(kws) is _simple_keywords(kws)


def test_known_divergence_is_avoided() -> None:
    """The exact case the analysis found: a naive alternation masks MORE."""
    naive = re.compile("|".join(re.escape(k) for k in ["bc", "abc"]), re.IGNORECASE)
    assert naive.subn(MASK, "abc")[0] != reference_mask("abc", ["bc", "abc"])[0]
    # ... but the public function is still identical to the loop.
    assert _mask_keywords("abc", ["bc", "abc"]) == reference_mask("abc", ["bc", "abc"])


def test_fuzz_equivalence() -> None:
    """Random text x random (often overlapping) keyword sets."""
    rng = random.Random(20261027)
    alphabet = "abcABC +.\u00e9"
    for _ in range(600):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        kws = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 3))) for _ in range(rng.randint(0, 5))]
        assert _mask_keywords(text, kws) == reference_mask(text, kws), (text, kws)
