"""Embedding cache — dedupe identical (model, input) embedding calls.

WHY: MultimodalRAG and similar clients re-embed the same content constantly
(re-ingestion, re-chunking, repeated queries, multi-tenant overlap). The
gateway sits between the RAG app and the embedding endpoints, so the cache
works via a base_url swap with zero client changes. A cache hit costs zero
upstream GPU work and zero token budget — which effectively stretches every
key's allocation for read-dominated workloads.

Storage: Postgres JSONB, keyed on sha256(model + "\x00" + input). JSON over
pgvector keeps the dependency surface zero and vectors are small (1k floats
≈ 20KB JSON); if scale ever demands it, pgvector is a drop-in migration.

Only inputs up to settings.embedding_cache_max_input_bytes are stored —
caching giant payloads buys nothing and bloats the table.
"""

from __future__ import annotations

import hashlib
import json
import logging

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)


def cache_key(model: str, input_str: str) -> str:
    """Deterministic cache key: model + NUL separator + exact input.

    The NUL separator prevents ('ab','c') colliding with ('a','bc').
    Exact-input matching (no normalization) — embedding endpoints treat
    even whitespace as semantically meaningful, so "same" must mean
    byte-identical.
    """
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"\x00")
    h.update(input_str.encode())
    return h.hexdigest()


async def lookup(model: str, input_str: str) -> list[float] | None:
    """Return the cached vector, or None on miss (and on any cache error —
    a broken cache must degrade to a normal upstream call, never fail the
    request)."""
    if not settings.embedding_cache_enabled:
        return None
    try:
        row = await db.fetchval(
            "SELECT embedding FROM embedding_cache WHERE cache_key = $1",
            cache_key(model, input_str),
        )
        if row is None:
            return None
        return json.loads(row) if isinstance(row, str) else list(row)
    except Exception:
        log.exception("embedding cache lookup failed; degrading to upstream")
        return None


async def store(model: str, input_str: str, embedding: list[float]) -> None:
    """Best-effort write; failures log and continue (cache is an
    optimization, not a requirement)."""
    if not settings.embedding_cache_enabled:
        return
    if len(input_str.encode()) > settings.embedding_cache_max_input_bytes:
        return
    try:
        await db.execute(
            """
            INSERT INTO embedding_cache (cache_key, model, input_sha256, embedding, input_bytes)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (cache_key) DO NOTHING
            """,
            cache_key(model, input_str),
            model,
            hashlib.sha256(input_str.encode()).hexdigest(),
            json.dumps(embedding),
            len(input_str.encode()),
        )
    except Exception:
        log.exception("embedding cache store failed; continuing without cache")


def extract_embedding(response_json: dict) -> list[float] | None:
    """Pull the first vector out of an OpenAI-format embeddings response."""
    data = response_json.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        emb = data[0].get("embedding")
        if isinstance(emb, list):
            return [float(x) for x in emb]
    return None
