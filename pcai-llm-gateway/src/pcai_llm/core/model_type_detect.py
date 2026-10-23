"""Automatic endpoint-type detection: what IS this model?

WHY THIS EXISTS (2026-10-23): registering the vLLM-SR decision model meant
telling the gateway `model_type: "decision"` by hand. Get that wrong and the
failure is silent in both directions — a chat pool typed `decision` vanishes
from /v1/models and the ladder, and a decision pool typed `chat` accepts a chat
request it can only 500 on. The operator should not have to know the taxonomy.

WHAT THE EVIDENCE SAYS (probed live against 8 InferenceServices):

1. The /v1/models CARD is NOT enough. vLLM and SGLang both advertise exactly
   `{id, max_model_len, object, owned_by, root/parent}` — identical for a chat,
   an embedding and a reranker pool. There is no type field to read:
       qwen-38-27b        (chat)      owned_by=sglang, same 4 keys
       qwen3-vl-embedding (embedding) owned_by=vllm,   same shape
       qwen3-vl-reranker  (reranker)  owned_by=vllm,   same shape
   The native vLLM-SR runtime is the exception: its card carries
   `family`, `surfaces` and `question_types`, which identify a decision model
   outright without probing anything.

2. A CAPABILITY PROBE is reliable — but only with a VALID payload and the REAL
   served model id. An empty `{}` body makes every MOUNTED route answer 400,
   which is indistinguishable from "route absent", and these servers mount
   /embeddings and /rerank generically even on a chat model. With a real
   payload the diagonal is clean:

       service                     chat embed rerank decision   true type
       glm-53-flash                 200   400    400      400    chat
       qwen-38-27b                  200   400    400      404    chat
       qwen3-vl-embedding-8b        404   200    200      404    embedding
       qwen3-vl-reranker-8b         404   404    200      404    reranker
       vllmsr-d3-turbo-router       404   422    422      200    decision

   So 200 = "this endpoint SERVES that surface"; anything else = it does not.

3. The embedding/reranker pair needs one tie-break rule: the EMBEDDING server
   mounts /rerank too (both answer 200), while the RERANKER refuses /embeddings
   (404). "embeddings 200" therefore wins, and a reranker is the case where
   /rerank answers but /embeddings does not.

DESIGN STANCE: detection returns a TYPE plus the EVIDENCE that produced it,
and returns "unknown" rather than guessing. A mis-typed pool is silent in both
directions, so a confident wrong answer is worse than an honest "I could not
tell" — the operator keeps the final say, and the probe output shows them why.

COST: a probe sends one minimal request per surface (a 1-token chat, a
short embedding, two tiny documents, a yes/no decision). That is real upstream
GPU work, which is why this is an EXPLICIT admin action and never runs on a
registry refresh.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# The surfaces a probe can exercise, in the order we report them.
SURFACES = ("chat", "embedding", "rerank", "decision", "speech")

# Minimal VALID payloads. Deliberately tiny: detection must not cost a real
# generation, and the answer only needs "did this surface serve it at all".
PROBE_PAYLOADS: dict[str, tuple[str, dict[str, Any]]] = {
    "chat": ("/chat/completions", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}),
    "embedding": ("/embeddings", {"input": "probe"}),
    "rerank": ("/rerank", {"query": "probe", "documents": ["a", "b"]}),
    "decision": (
        "/systemone",
        {"state": "probe", "questions": {"q": {"type": "noul", "instructions": "Is this text?"}}},
    ),
    "speech": ("/audio/speech", {"input": "probe", "voice": "default"}),
}


@dataclass
class Detection:
    """One endpoint's detected type plus the evidence for it.

    `type` is a MODEL_TYPES value or "" (unknown). `evidence` maps each probed
    surface to the HTTP status it answered, so an operator can see WHY the
    verdict came out as it did instead of trusting a bare label.
    """

    type: str = ""
    served_id: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str = "not_probed"


def classify(card: dict[str, Any], evidence: dict[str, Any]) -> Detection:
    """Decide the type from the advertised card and the probe statuses. PURE.

    Card first: a native vLLM-SR endpoint advertises `surfaces`/`family`, which
    is authoritative and costs nothing. Otherwise read the probe matrix.

    Total by construction — a malformed card or a missing surface simply falls
    through to the next rule and finally to unknown.
    """
    served = ""
    if isinstance(card, dict):
        data = card.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            served = str(data[0].get("id") or "")

    def ok(surface: str) -> bool:
        return evidence.get(surface) == 200

    # 1. Card-asserted capability (the native decision runtime). Authoritative:
    #    the model TELLS us what it serves, so no probe is needed to trust it.
    if isinstance(card, dict):
        data = card.get("data")
        entry = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        surfaces = entry.get("surfaces")
        if isinstance(surfaces, list) and "decisions" in surfaces:
            return Detection("decision", served, evidence, "card:surfaces=decisions")
        fam = str(entry.get("family") or "")
        if fam.startswith("decision"):
            return Detection("decision", served, evidence, f"card:family={fam}")

    # 2. Probe matrix. A 200 is the only thing that proves a surface is SERVED;
    #    a 400/422 only proves the route is mounted (these servers mount
    #    /embeddings and /rerank even on a chat model).
    if ok("decision"):
        return Detection("decision", served, evidence, "probe:/systemone=200")
    if ok("speech"):
        return Detection("voice", served, evidence, "probe:/audio/speech=200")
    if ok("chat"):
        # A chat pool that also answers /embeddings is still a chat pool: the
        # chat surface is the one an OpenAI client would use, and SGLang
        # mounts the embedding route generically.
        return Detection("chat", served, evidence, "probe:/chat/completions=200")
    if ok("embedding"):
        # Tie-break: the EMBEDDING server mounts /rerank too, so embeddings
        # answering 200 wins; a pure reranker refuses /embeddings.
        return Detection("embedding", served, evidence, "probe:/embeddings=200")
    if ok("rerank"):
        return Detection("reranker", served, evidence, "probe:/rerank=200")

    return Detection("", served, evidence, "no surface answered 200")


ProbeFn = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""surface -> (path, payload) -> HTTP status (or an exception)."""


async def detect(card: dict[str, Any], probe: ProbeFn, *, surfaces: tuple[str, ...] = SURFACES) -> Detection:
    """Probe the surfaces and classify. NEVER raises.

    A probe failure is recorded as an error string for that surface and
    treated as "not served" — a detection run must not fail because one
    surface timed out, and the evidence column makes the gap visible.
    """
    evidence: dict[str, Any] = {}
    for surface in surfaces:
        path, payload = PROBE_PAYLOADS[surface]
        try:
            evidence[surface] = await probe(path, payload)
        except Exception as exc:
            evidence[surface] = f"error:{type(exc).__name__}"
            log.info("type-detect: %s probe failed (%s)", surface, type(exc).__name__)
    return classify(card, evidence)
