"""Reference query pools.

Same pools as the Multimodal RAG benchmark script (generic vs VLM-tripping
wording) plus a loader for ``--queries-file`` (one query per line, ``#``
comments allowed). For a *specific* customer endpoint you should nearly
always pass a real query file — the built-ins exist so the tool is usable
against anything with zero setup.
"""

from __future__ import annotations

import sys

DEFAULT_QUERIES_GENERIC: list[str] = [
    # General knowledge
    "What is machine learning?",
    "Explain how neural networks work",
    "What are the benefits of cloud computing?",
    "What is a transformer architecture?",
    "How does retrieval-augmented generation work?",
    "What is vector similarity search?",
    "Explain the concept of embeddings",
    "What is transfer learning?",
    "Describe the attention mechanism",
    # Technical / code
    "How do I handle errors in Python?",
    "What is a REST API?",
    "Show me examples of data structures",
    "How to optimize database queries",
    "What is containerization?",
    "Explain microservices architecture",
    "How does authentication work?",
    "What are design patterns?",
    # Document / business
    "What are the project requirements?",
    "Summarize the main findings",
    "What are the key metrics?",
    "Describe the deployment process",
    "What are the security policies?",
    "Find information about configuration",
    "What are the best practices?",
    # Short keyword
    "introduction",
    "overview",
    "summary",
    "architecture",
    "performance",
    "security",
    "configuration",
    "installation",
    "troubleshooting",
    "examples",
    # Image / media (for multimodal datasets)
    "The aurora borealis over a snowy mountain",
    "Black and white image of a lake reflecting the trees by its side",
    "A man crouching staring down at the tops of clouds from a mountain",
]

DEFAULT_QUERIES_VLM: list[str] = [
    "Describe the difference between supervised and unsupervised learning",
    "A skyscraper high above the other buildings in a city on a cloudy day",
    "The top of a tower with an antenna on an overcast day",
    "How many products are shown in the image?",
    "What color is the box in the middle of the picture?",
    "What does the label on the packaging say?",
]

DEFAULT_QUERIES: list[str] = DEFAULT_QUERIES_GENERIC + DEFAULT_QUERIES_VLM

QUERY_SETS: dict[str, list[str]] = {
    "generic": DEFAULT_QUERIES_GENERIC,
    "vlm": DEFAULT_QUERIES_VLM,
    "mixed": DEFAULT_QUERIES,
}


def load_queries(queries_file: str | None, query_set: str = "generic") -> tuple[list[str], str]:
    """Return (queries, label). Exits the program on an empty/missing file."""
    if queries_file is None:
        return QUERY_SETS[query_set], query_set
    try:
        with open(queries_file, encoding="utf-8") as f:
            queries = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    except OSError as exc:
        print(f"Error: cannot read queries file {queries_file}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not queries:
        print(f"Error: queries file is empty: {queries_file}", file=sys.stderr)
        raise SystemExit(1)
    return queries, f"custom ({queries_file})"
