"""Regression cover for cross-session near-duplicate suppression (2026-10-23).

SYMPTOM: the memory store grew near-duplicate CLUSTERS -- the same session
story distilled multiple times from parallel threads, each generation a
differently-worded near-copy. Live evidence: the sqlhandler deployment story
was distilled FOUR times (sessions 393737a6 12:21, b9fb6796 12:31, 0a81883f
12:34, 292e2fda 12:53 on 2026-10-21), the hugepages runbook 3x, the pricing
PDF 3x. mm-rag folds only at cosine >= 0.995 -- far above independent LLM
paraphrases -- so nothing folded.

ROOT CAUSE: the design comment in core/distill_schedule ("a new distill
REPLACES the picture; RAG near-dedup folds the superseded generations") was
never implemented on the write path; the only gate was a character floor
(default 0). Cross-replica same-instant dupes were fixed by the sweep lease
(2026-10-11/14), but cross-session paraphrases had no guard at all.

THE FIX: write_memory takes an optional mcp_search probe. When set (and
kind=project-summary), it searches the dataset with the new document's own
body and applies token CONTAINMENT against each hit:
  - containment >= distiller_dedup_containment AND a DIFFERENT session_id
      -> suppress the write (near-duplicate cluster prevention);
  - same session_id -> allow (the designed re-distill update for continued
      conversations -- distill_schedule's due predicate re-distills a
      continued session by design);
  - probe failure or no mcp_search -> write through (best-effort; a broken
      dedupe must never block a memory).
"""

from __future__ import annotations

import pytest

from pcai_llm.core.memory_write import _token_containment, write_memory

UUID = "00000000-0000-0000-0000-000000000001"

# LIVE FIXTURE (2026-10-23): the two sqlhandler deployment siblings from the
# store -- session 0a81883f (12:34) and session b9fb6796 (12:31), retrieved
# verbatim during the quality review. Measured containment: 0.805. These are
# what the guard must catch; synthetic paraphrases kept scoring too low
# because real distiller output preserves dense tokens verbatim.
SIBLING_0A = (
    "[MEMORY INDEX | project: sqlhandler | type: session-summary | topics: deployment, pvc, policy-read-only, sso-auth, mcp-fleet, semantic-catalog, kubernetes]\n"
    "## What we did\n"
    "- Diagnosed and triaged multiple post-deployment failures in the "
    "sqlhandler namespace (G2 cluster, kubeconfig ~/SSH/G2/g2_kubeconfig.yaml, "
    "proxy socks5://127.0.0.1:1080): virtual tables not rendering at all, "
    "admin key panel failing to load, cached results regressing.\n"
    "- Built version 3.8.1 after fixes.\n"
    "- Resolved a stuck ContainerCreating pod by deleting the entire "
    "sqlhandler namespace (which also destroyed the PVC).\n"
    "- Recreated secrets post-namespace-delete: s3-credentials (kubectl "
    "apply), mcp-fleet-apikeys.\n"
    "- PVC size: user explicitly rejected 20 Gi, requested 5 Gi.\n"
    "- Hit a FailedMount on the policy-seed volume: configmap "
    "\"sqlhandler-policy\" not found.\n"
    "- Resolved SSO self-mint failure: a hidden X-API-KEY header was "
    "intercepting auth.\n"
    "- User invoked GLM + DeepSeek subagents for a cross-verification "
    "bug-hunt loop.\n"
    "- Attempted semantic-catalog write via curl -X POST - failed because "
    "catalog.yaml was not in the working directory.\n\n"
    "## Key decisions & rationale\n"
    "1. User corrected the problem statement twice.\n"
    "2. PVC at 5 Gi, not 20 Gi.\n"
    "3. Replace ConfigMap with a small RW PVC for policy.\n"
    "4. Group naming must use underscores.\n"
    "5. SSO login != write authorization.\n"
    "6. Bug-hunt with dual-model verification.\n\n"
    "## Gotchas\n"
    "- Namespace deletion destroys PVCs.\n"
    "- ConfigMap mount before ConfigMap exists -> FailedMount loop.\n"
    "- [Errno 30] Read-only file system on /etc/sqlhandler-policy/policy.json.\n"
    "- Hidden X-API-KEY in the PCAI LLM gateway silently broke SSO self-mint.\n"
    "- kubectl apply on a secret created out-of-band warns about missing "
    "annotation.\n"
    "- Semantic-catalog curl failed because catalog.yaml didn't exist at the "
    "invocation path.\n"
    "- Auth env vars block stdio MCP calls.\n\n"
    "## How to proceed\n"
    "- Deploy the RW-PVC policy volume"
)

SIBLING_B9 = (
    "[MEMORY INDEX | project: sqlhandler | type: session-summary | topics: deployment, kubernetes, pvc, policy, read-only-filesystem, sso, api-keys, bug-hunt]\n"
    "## What we did\n"
    "- Diagnosed post-deployment failures in the sqlhandler namespace on G2 "
    "(kubeconfig ~/SSH/G2/g2_kubeconfig.yaml, socks5 proxy).\n"
    "- Built version 3.8.1 after fixes: virtual tables not rendering, admin "
    "key panel failing, cached results regressing on real tables.\n"
    "- Stuck ContainerCreating pod resolved by deleting the sqlhandler "
    "namespace, which also destroyed the PVC.\n"
    "- Recreated s3-credentials and mcp-fleet-apikeys secrets afterwards.\n"
    "- PVC sized 5 Gi per explicit user request (20 Gi rejected).\n"
    "- FailedMount on policy-seed: configmap \"sqlhandler-policy\" not found.\n"
    "- SSO self-mint blocked by a hidden X-API-KEY header in the PCAI LLM "
    "gateway.\n"
    "- GLM + DeepSeek subagent bug-hunt on semantic view RW, admin policy, "
    "key minting.\n\n"
    "## Key decisions & rationale\n"
    "1. User corrected the problem statement twice.\n"
    "2. PVC at 5 Gi not 20 Gi.\n"
    "3. RW PVC replaces the policy ConfigMap.\n"
    "4. Group names need underscores.\n"
    "5. SSO login is not write authorization.\n"
    "6. Dual-model bug-hunt loop.\n\n"
    "## Gotchas\n"
    "- Namespace deletion destroys PVCs.\n"
    "- ConfigMap mount before ConfigMap exists -> FailedMount loop.\n"
    "- [Errno 30] Read-only file system on the policy path.\n"
    "- Hidden X-API-KEY header broke SSO self-mint.\n"
    "- kubectl apply annotation warning.\n"
    "- catalog.yaml missing from CWD broke the curl.\n"
    "- Auth env vars block stdio MCP calls.\n\n"
    "## How to proceed\n"
    "- Deploy the RW-PVC policy volume"
)

# A DIFFERENT story that shares the project and some vocabulary.
# A DIFFERENT story that shares the project and some vocabulary.
DOC_OTHER = (
    "[MEMORY INDEX | project: sqlhandler | type: session-summary | topics: audit]\n"
    "## What we did\n"
    "Read-only audit of the write surfaces: webui, admin keys, policy, OIDC "
    "SSO, saved queries. Checked the identity gate and the mint flow end to "
    "end.\n\n"
    "## Key decisions & rationale\n"
    "1. Zero-write scope for the audit.\n\n"
    "## Gotchas\n"
    "- Stdio MCP cannot carry API-key headers.\n\n"
    "## How to proceed\n"
    "- Re-run after the v3.9 release."
)


def _search_stub(hits: list[dict]) -> callable:
    async def mcp_search(arguments: dict) -> tuple[bool, str]:
        import json

        return True, json.dumps({"results": hits})
    return mcp_search


def _hit(session: str, text: str) -> dict:
    return {
        "session_id": session,
        "memory_kind": "project-summary",
        "text": text,
        "created_at": 1792000000,
    }


@pytest.mark.asyncio
async def test_cross_session_near_duplicate_is_suppressed():
    """The cluster killer: a paraphrase of a DIFFERENT session's memory is
    refused with an explanatory detail."""
    calls: list[dict] = []

    async def mcp_add(args: dict) -> tuple[bool, str]:
        calls.append(args)
        return True, "stored"

    ok, detail = await write_memory(
        UUID, mcp_add, SIBLING_B9, session_id="session-new",
        mcp_search=_search_stub([_hit("session-old", SIBLING_0A)]),
    )
    assert ok is False
    assert "near-duplicate" in detail and "session-old" in detail
    assert calls == [], "suppressed write must not reach add_memory"


@pytest.mark.asyncio
async def test_same_session_near_duplicate_is_allowed():
    """The designed re-distill: same session -> the update writes."""
    calls: list[dict] = []

    async def mcp_add(args: dict) -> tuple[bool, str]:
        calls.append(args)
        return True, "stored"

    ok, _detail = await write_memory(
        UUID, mcp_add, SIBLING_B9, session_id="session-old",
        mcp_search=_search_stub([_hit("session-old", SIBLING_0A)]),
    )
    assert ok is True and calls, "same-session re-distill must write"


@pytest.mark.asyncio
async def test_different_topic_same_project_writes():
    calls: list[dict] = []

    async def mcp_add(args: dict) -> tuple[bool, str]:
        calls.append(args)
        return True, "stored"

    ok, _detail = await write_memory(
        UUID, mcp_add, DOC_OTHER, session_id="session-audit",
        mcp_search=_search_stub([_hit("session-deploy", SIBLING_0A)]),
    )
    assert ok is True and calls


@pytest.mark.asyncio
async def test_probe_failure_writes_through():
    """Best-effort: a broken search probe must never block a memory."""
    calls: list[dict] = []

    async def mcp_add(args: dict) -> tuple[bool, str]:
        calls.append(args)
        return True, "stored"

    async def broken_search(arguments: dict) -> tuple[bool, str]:
        raise RuntimeError("rag search down")

    ok, _detail = await write_memory(
        UUID, mcp_add, SIBLING_B9, session_id="session-new",
        mcp_search=broken_search,
    )
    assert ok is True and calls


@pytest.mark.asyncio
async def test_no_probe_writes_through():
    """Backward compatibility: no mcp_search -> the guard is inert."""
    calls: list[dict] = []

    async def mcp_add(args: dict) -> tuple[bool, str]:
        calls.append(args)
        return True, "stored"

    ok, _ = await write_memory(UUID, mcp_add, SIBLING_B9, session_id="s")
    assert ok is True and calls


@pytest.mark.asyncio
async def test_session_history_kind_never_probed():
    """The fallback kind is exempt: raw transcripts are never deduped."""
    calls: list[dict] = []
    probed: list[bool] = []

    async def mcp_add(args: dict) -> tuple[bool, str]:
        calls.append(args)
        return True, "stored"

    async def probe(arguments: dict) -> tuple[bool, str]:
        probed.append(True)
        return True, "[]"

    ok, _ = await write_memory(
        UUID, mcp_add, "raw transcript", session_id="s", kind="session_history",
        mcp_search=probe,
    )
    assert ok is True and calls and probed == []


# ---------------------------------------------------------------------------
# _token_containment
# ---------------------------------------------------------------------------


def test_containment_of_verbatim_paraphrase_is_high():
    score = _token_containment(SIBLING_B9, SIBLING_0A)
    assert score >= 0.75, f"paraphrase cluster scored {score:.2f}"


def test_containment_of_different_topic_is_low():
    score = _token_containment(DOC_OTHER, SIBLING_0A)
    assert score < 0.75, f"unrelated doc scored {score:.2f}"


def test_containment_survives_chunk_subsets():
    """mm-rag CHUNKS long documents at section boundaries: a stored hit may
    be a strict subset of the prior document. Containment (min-denominator)
    must read high for the DENSE shared chunk, where Jaccard would read the
    subset fraction and miss the duplicate. (The What-we-did block is the
    densest shared region between the live siblings -- measured 0.83 for
    that section pair standalone.)"""
    chunk = SIBLING_0A[: SIBLING_0A.index("## Key decisions")]
    score = _token_containment(SIBLING_B9[: SIBLING_B9.index("## Key decisions")], chunk)
    assert score >= 0.70, f"section-chunk hit scored {score:.2f}"


def test_containment_ignores_the_index_header():
    """Two stubs with identical 164-char headers but different bodies must
    not score as duplicates (the header-immunity lesson from
    test_memory_body_floor)."""
    header = "[MEMORY INDEX | project: x | type: session-summary | topics: a,b,c,d,e,f,g]"
    score = _token_containment(header + "\nalpha", header + "\nbeta")
    assert score < 0.75
