"""Automatic endpoint-type detection.

WHY THIS FILE EXISTS (2026-10-23): registering the vLLM-SR decision model meant
typing `model_type: "decision"` by hand, and a mis-typed pool fails SILENTLY in
both directions -- a chat pool typed `decision` disappears from /v1/models and
the ladder, a decision pool typed `chat` accepts a request it can only 500 on.

The detection design was derived from live probes against 8 InferenceServices,
and the tests below pin the two facts that made it necessary:

  * the /v1/models CARD cannot separate chat/embedding/reranker on vLLM or
    SGLang -- all three advertise the same four keys, so the type is simply not
    advertised (only the native decision runtime publishes `surfaces`);
  * a probe must use a VALID payload and the REAL served model id: an empty {}
    body makes every MOUNTED route answer 400, indistinguishable from absent,
    because these servers mount /embeddings and /rerank even on a chat model.

With a real payload the diagonal is clean, and 200 is the only status that
proves a surface is SERVED.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pcai_llm.core import model_type_detect as mtd

# The matrices observed live on 2026-10-23, one per real service.
OBSERVED = {
    "glm-53-flash": ("chat", {"chat": 200, "embedding": 400, "rerank": 400, "decision": 400, "speech": 404}),
    "qwen-38-27b": ("chat", {"chat": 200, "embedding": 400, "rerank": 400, "decision": 404, "speech": 404}),
    "qwen3-vl-embedding-8b": (
        "embedding",
        {"chat": 404, "embedding": 200, "rerank": 200, "decision": 404, "speech": 404},
    ),
    "qwen3-vl-reranker-8b": (
        "reranker",
        {"chat": 404, "embedding": 404, "rerank": 200, "decision": 404, "speech": 404},
    ),
    "vllmsr-d3-turbo-router": (
        "decision",
        {"chat": 404, "embedding": 422, "rerank": 422, "decision": 200, "speech": 404},
    ),
}


@pytest.mark.parametrize("service,expected", sorted(OBSERVED.items()))
def test_classify_reproduces_every_observed_endpoint(service: str, expected: tuple[str, dict[str, Any]]) -> None:
    """Each REAL probe matrix must map to that service's true type."""
    want, evidence = expected
    got = mtd.classify({}, evidence)
    assert got.type == want, f"{service}: want {want}, got {got.type} ({got.reason})"


def test_card_asserted_surfaces_win_without_probing() -> None:
    """The native decision runtime TELLS us what it serves; that is
    authoritative and costs nothing, so no probe is needed to trust it."""
    card = {"data": [{"id": "vllm-sr/d3-turbo", "surfaces": ["decisions"], "family": "decision3"}]}
    assert mtd.classify(card, {}).type == "decision"
    assert "card" in mtd.classify(card, {}).reason


def test_card_family_alone_identifies_a_decision_model() -> None:
    assert mtd.classify({"data": [{"id": "x", "family": "decision3"}]}, {}).type == "decision"


def test_embedding_wins_the_tie_break_against_rerank() -> None:
    """The EMBEDDING server mounts /rerank too (both answer 200), while the
    RERANKER refuses /embeddings. So embeddings=200 must win, and a reranker
    is precisely the case where /rerank answers but /embeddings does not."""
    emb = mtd.classify({}, {"chat": 404, "embedding": 200, "rerank": 200})
    rer = mtd.classify({}, {"chat": 404, "embedding": 404, "rerank": 200})
    assert emb.type == "embedding"
    assert rer.type == "reranker"


def test_a_chat_pool_that_also_answers_embeddings_is_still_chat() -> None:
    """SGLang mounts the embedding route generically; the chat surface is what
    an OpenAI client would actually use, so it wins."""
    assert mtd.classify({}, {"chat": 200, "embedding": 200, "rerank": 200}).type == "chat"


def test_unknown_is_reported_rather_than_guessed() -> None:
    """A wrong guess is the exact failure detection exists to prevent, so a
    matrix with no 200 must come back as an honest empty type."""
    d = mtd.classify({}, {"chat": 500, "embedding": 404, "rerank": 400, "decision": 404})
    assert d.type == ""
    assert "no surface answered 200" in d.reason


def test_a_400_alone_never_proves_a_surface_is_served() -> None:
    """The load-bearing lesson from the live probe: 400/422 only proves the
    route is MOUNTED. An empty body on a chat model returns 400 on
    /embeddings too, so treating non-404 as "served" would type every chat
    pool as an embedding server."""
    assert mtd.classify({}, {"chat": 400, "embedding": 400, "rerank": 400}).type == ""


def test_detect_records_probe_failures_without_raising() -> None:
    """One unreachable surface must not fail the whole detection run -- the
    evidence column records the error so the gap stays visible."""

    async def probe(path: str, payload: dict[str, Any]) -> Any:
        if path == "/chat/completions":
            return 200
        raise ConnectionError("nope")

    d = asyncio.run(mtd.detect({}, probe))
    assert d.type == "chat"
    assert d.evidence["embedding"] == "error:ConnectionError"


def test_detect_probes_every_surface_with_a_valid_payload() -> None:
    """Each probe must carry a REAL body: an empty one cannot distinguish a
    served surface from a merely mounted route."""
    seen: list[tuple[str, dict[str, Any]]] = []

    async def probe(path: str, payload: dict[str, Any]) -> Any:
        seen.append((path, payload))
        return 200 if path == "/embeddings" else 404

    d = asyncio.run(mtd.detect({}, probe))
    assert d.type == "embedding"
    paths = {p for p, _ in seen}
    assert paths == {"/chat/completions", "/embeddings", "/rerank", "/systemone", "/audio/speech"}
    for path, payload in seen:
        assert payload, f"{path} was probed with an empty body"


def test_detect_surfaces_the_served_id_for_the_caller_to_stamp() -> None:
    async def probe(path: str, payload: dict[str, Any]) -> Any:
        return 200 if path == "/chat/completions" else 404

    card = {"data": [{"id": "glm-5.3-flash"}]}
    d = asyncio.run(mtd.detect(card, probe))
    assert d.served_id == "glm-5.3-flash"
