#!/usr/bin/env python3
"""Benchmark time-to-first-token (TTFT) and tokens/s for PCAI chat models.

Runs one model under a range of concurrency levels, mixing long task types
(coding, creative writing, ...), and prints per-task P50 / P95 / P99 / P100
percentiles for both metrics.

Every request gets a unique random nonce prepended to its prompt so the
shared serving cluster cannot reuse prefix/response caches (RadixAttention
etc.) — consecutive requests share no tokens, so measurements reflect real
generation, not cache hits.

With --multiturn, each user instead runs a single growing conversation:
every request (turn) sends the full prior user+assistant history plus the
new task prompt, so the shared prefix *is* reused across a user's turns and
the server's prefix/KV cache (and HiCache tiers) can engage. This measures
TTFT/tokens/s under real chat-style context growth rather than pure cold
prefill.

--no-nonce replaces the per-request random nonce with a fixed shared marker,
so every request (and every user) shares an identical prompt prefix and the
server can reuse its prefix/KV cache across users — isolating the benefit of
cache sharing from cold-prefill cost.

--prewarm sends one shared-prefix request per (ctx, task) before each
concurrency sweep, so the prefix graph is populated ahead of the users
("prefill happens before the users"). Pairs with --no-nonce so the warm
prefix matches what the users send.

Usage:
  # Use a model registered in pcai_models (deepseek in particular):
  python benchmark_chat.py --model_class_name deepseek_v4_flash_280B \
      --remote --number_users 1,2,4,8 --tasks coding,creative

  # Use an arbitrary OpenAI-compatible endpoint:
  python benchmark_chat.py --url "https://<endpoint>" --api_key "<key>" \
      --remote --number_users 1,4,8 --tasks coding

  # A key-less endpoint (no --api_key; no Authorization header is sent):
  python benchmark_chat.py --url "http://localhost:8000" \
      --number_users 1,4,8 --tasks coding

  # A single custom task:
  python benchmark_chat.py --model_class_name deepseek_v4_flash_280B --remote \
      --tasks custom --prompt "Write about X" --max_tokens 1024

Note: without --remote the model targets its *local* in-cluster URL.
Pass --remote to hit the public serving URL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import numpy as np

try:
    from openai import Omit as _OMIT  # openai>=2.0
except ImportError:  # openai 1.x keeps the sentinel in _base_client
    from openai._base_client import Omit as _OMIT  # type: ignore[attr-defined]


def _add_package_to_path() -> None:
    """Put the directory containing the ``model_benchmarker`` package on
    ``sys.path`` so the script runs from anywhere in the repo or the
    hardlinker deployment (which mirrors the repo layout, e.g.
    ``<dest>/src/model_benchmarker/benchmark_chat.py``)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(script_dir, "src"),  # flat: <root>/benchmark_chat.py + <root>/src
        script_dir,  # script run from inside its own directory
        os.path.dirname(script_dir),  # nested: <root>/src/model_benchmarker/ -> <root>/src
    )
    for candidate in candidates:
        if os.path.isdir(os.path.join(candidate, "model_benchmarker")):
            sys.path.insert(0, candidate)
            return


_add_package_to_path()

# The shared PCAI modules log model auto-discovery at WARNING during import;
# keep a clean CLI and let per-request failures surface in our own output.
logging.getLogger().setLevel(logging.ERROR)

DEFAULT_PROMPT = (
    "Write a concise structured essay about the history of computing, "
    "covering the 1950s to the present day, at least twelve paragraphs long."
)

# Fixed prefix used when --no-nonce is given: every request renders the same
# header so all users share an identical prompt prefix the server can cache.
_SHARED_NONCE = "my-shared-benchmark-prefix"

CODING_PROMPT = """[instance {nonce}]
Write a complete, production-grade Python implementation of a thread-safe,
persistent LRU cache with TTL expiry and an optional disk-backed store. The
code must:

1. Include a CacheEntry dataclass holding value, insert timestamp and expiry.
2. Implement get/set/delete/clear with O(1) amortized lookups using an
   OrderedDict or a doubly-linked list.
3. Add a ttl parameter (seconds) to set(); expired entries must be lazily
   purged on access and actively swept by a background thread.
4. Support a persist_to(path) / load(path) pair using a pickled (or JSON)
   snapshot that survives process restarts.
5. Be fully type-hinted, with detailed docstrings, an __all__ list and
   inline comments explaining every non-trivial block.
6. Provide a small unittest suite covering expiry, eviction order, thread
   safety under many writers/readers, and persistence round-trips.

Write the whole module top to bottom in one response, no placeholders and
no "rest omitted" — the full, runnable source.
"""

CREATIVE_PROMPT = """[instance {nonce}]
Write a long, original, literary short story of roughly 1,500-2,000 words
titled "The Lighthouse at the Edge of the Map". Requirements:

1. A lonely lighthouse keeper on an island that does not appear on any chart
   discovers a door in the cliff that opens once a generation.
2. Use vivid sensory detail and at least one extended metaphor running
   through the whole piece.
3. Show, don't tell, through concrete scenes, dialogue and inner monologue;
   no summary narration.
4. Build to a clear emotional turning point, and end with an ambiguous but
   satisfying final line.
5. Write in the third person, past tense, with a distinct narrative voice.

Give me the complete story — every paragraph, no outline, no summary, no
truncation."""


@dataclass
class Task:
    name: str
    prompt: str
    max_tokens: int
    temperature: float
    top_p: float = 1.0


def _strip_instance_line(prompt: str) -> str:
    """Drop the leading '[instance {nonce}]' line so prompts can be nested."""
    head, sep, rest = prompt.partition("\n")
    return rest if sep and head.startswith("[instance") else prompt


# A single request that must complete BOTH a coding and a creative-writing
# task in one response (code + prose in one generation).
MIXED_PROMPT = (
    "[instance {nonce}]\n"
    "Complete BOTH of the following two tasks in a single response, in two "
    "clearly separated sections. Do not skip either part.\n\n"
    "========== PART A - CODING ==========\n"
    + _strip_instance_line(CODING_PROMPT)
    + "\n\n========== PART B - CREATIVE WRITING ==========\n"
    + _strip_instance_line(CREATIVE_PROMPT)
)


TASK_REGISTRY: dict[str, Task] = {
    "coding": Task(
        name="coding",
        prompt=CODING_PROMPT,
        max_tokens=8192,
        temperature=1.0,
        top_p=0.95,
    ),
    "creative": Task(
        name="creative",
        prompt=CREATIVE_PROMPT,
        max_tokens=16384,
        temperature=1.0,
    ),
    "mixed": Task(
        name="mixed",
        prompt=MIXED_PROMPT,
        max_tokens=24576,
        temperature=1.0,
    ),
}


@dataclass
class RequestResult:
    success: bool
    task: str = ""
    turn: int = 0
    ttft_s: float = 0.0
    tokens: int = 0
    tokens_per_s: float = 0.0
    error: str = ""
    response_text: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--model_class_name",
        default="",
        help="Name of a model variable in model_benchmarker/utils/pcai_models.py "
        "(imported lazily, only when this flag is used; that module is excluded "
        "from the hardlink deployment, so --url and --api_key are required "
        "there). If omitted, --url is required and --api_key is optional "
        "(endpoints that don't require auth can omit it).",
    )
    parser.add_argument("--url", default="", help="OpenAI-compatible base URL (root, no /v1).")
    parser.add_argument(
        "--api_key",
        default="",
        help="Bearer token for the endpoint. Optional: omit for endpoints "
        "that don't require auth (no Authorization header is then sent). "
        "Prefer --api_key_file or the PCAI_API_KEY env var so the secret "
        "stays out of shell history and the process list.",
    )
    parser.add_argument(
        "--api_key_file",
        default="",
        help="Read the bearer token from this file (first line, stripped). "
        "Ignored when --api_key is given; falls back to $PCAI_API_KEY.",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Call .remote() on the model so the public serving URL is used.",
    )
    parser.add_argument(
        "--number_users",
        type=str,
        default="1",
        help="Comma-separated list of concurrency levels (e.g. 1,2,4,8).",
    )
    parser.add_argument(
        "--requests_per_user",
        type=int,
        default=1,
        help="Sequential requests each concurrent user performs, cycling through tasks (default: 1).",
    )
    parser.add_argument(
        "--no-nonce",
        action="store_true",
        help="Replace the per-request random nonce with a fixed shared marker so "
        "every request shares an identical prompt prefix and the server can reuse "
        "its prefix/KV cache across users (isolates cache-sharing benefit from "
        "cold-prefill cost).",
    )
    parser.add_argument(
        "--prewarm",
        action="store_true",
        help="Send one shared-prefix request per (ctx, task) before each "
        "concurrency sweep so the prefix graph is populated ahead of the users. "
        "Pairs with --no-nonce so the warm prefix matches the users' requests.",
    )
    parser.add_argument(
        "--multiturn",
        action="store_true",
        help="Treat each user's requests as turns of ONE growing conversation "
        "instead of independent requests: every turn replays the full prior "
        "user+assistant history and appends a fresh task prompt, so the shared "
        "prefix is reused and the server's prefix/KV cache (and HiCache tiers) "
        "can engage. Turn order still cycles through --tasks.",
    )
    parser.add_argument(
        "--separate_tasks",
        action="store_true",
        help="Run each task type in its own (ctx, users) pass instead of mixing "
        "them: for every (ctx, users, task) triple, all users run "
        "--requests_per_user turns of just that task back-to-back. Gives every "
        "task a clean turn-1 (prefill) vs turns 2+ (prefix reuse) comparison "
        "without cross-task interference.",
    )
    parser.add_argument(
        "--tasks",
        default="coding,creative",
        help="Comma-separated task types to mix (default: coding,creative). "
        "Available: " + ", ".join(sorted(TASK_REGISTRY)) + ", custom. "
        "'mixed' sends one request that must complete both the coding and "
        "creative tasks at once; 'custom' uses --prompt / --max_tokens / "
        "--temperature.",
    )
    parser.add_argument(
        "--max_tokens",
        type=int,
        default=None,
        help="Override max output tokens for every task (default: per-task).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="Override sampling temperature for every task (default: per-task).",
    )
    parser.add_argument(
        "--top_p",
        type=float,
        default=None,
        help="Override top_p for every task (default: per-task; coding=0.95, else 1.0).",
    )
    parser.add_argument(
        "--context_length",
        type=str,
        default="0",
        help="Comma-separated list of prefill lengths to sweep: each level "
        "pads every prompt to approx that many input tokens (4 chars/token "
        "approximation). 0 disables padding (default). E.g. "
        "8192,32768,65536 for a prefill sweep.",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt used for the 'custom' task.")
    parser.add_argument(
        "--enable_thinking",
        action="store_true",
        help="Force Qwen3-style reasoning on via chat_template_kwargs.enable_thinking=True "
        "(it is on by default; useful if the server default was changed).",
    )
    parser.add_argument(
        "--disable_thinking",
        action="store_true",
        help="Force chat_template_kwargs.enable_thinking=False so the model emits content "
        "tokens directly (no reasoning_content deltas).",
    )
    parser.add_argument(
        "--thinking_budget",
        type=int,
        default=None,
        help="Cap reasoning tokens via chat_template_kwargs.thinking_token_budget. Must be "
        "smaller than the task's max_tokens or the model may spend the whole budget on "
        "reasoning and never emit content (TTFT then reads 0).",
    )
    parser.add_argument(
        "--thinking_level",
        choices=sorted(THINKING_BUDGET_LEVELS),
        default=None,
        help="Preset reasoning budget: off=0, low=1024, medium=4096, high=16384, x-high=32768. "
        "Overrides --thinking_budget. Default (no flag) leaves the server default.",
    )
    parser.add_argument(
        "--debug_stream",
        action="store_true",
        help="Print the first delta's field names/values and per-request content vs "
        "reasoning chunk counts so you can see what the endpoint actually streams "
        "(helps diagnose TTFT=0 when content arrives in a non-`content` field).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the per-request progress lines (keep prewarm notices, "
        "level previews and the final table). Recommended when stdout is "
        "piped/redirected: per-request print() runs on the event loop and a "
        "blocked stdout stall can distort TTFT/tokens-s.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Write the final summary table to this file (in addition to stdout). "
        "E.g. --output results/qwen_38_27b/H200x4.txt.",
    )
    return parser.parse_args()


def resolve_api_key(args: argparse.Namespace) -> str:
    """Resolve the bearer token for --url mode without putting secrets on the
    command line.  Priority: --api_key > --api_key_file > $PCAI_API_KEY."""
    if args.api_key:
        return args.api_key
    if args.api_key_file:
        try:
            key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"--api_key_file: cannot read {args.api_key_file!r}: {exc}") from exc
        if key:
            return key
    return os.environ.get("PCAI_API_KEY", "")


def resolve_tasks(args: argparse.Namespace) -> list[Task]:
    names = [t.strip() for t in args.tasks.split(",") if t.strip()]
    if not names:
        raise SystemExit("--tasks must contain at least one task type")

    tasks: list[Task] = []
    for name in names:
        if name == "custom":
            tasks.append(
                Task(
                    name="custom",
                    prompt=args.prompt,
                    max_tokens=args.max_tokens or 512,
                    temperature=args.temperature if args.temperature is not None else 1.0,
                    top_p=args.top_p if args.top_p is not None else 1.0,
                )
            )
        elif name in TASK_REGISTRY:
            base = TASK_REGISTRY[name]
            tasks.append(
                Task(
                    name=base.name,
                    prompt=base.prompt,
                    max_tokens=args.max_tokens or base.max_tokens,
                    temperature=args.temperature if args.temperature is not None else base.temperature,
                    top_p=args.top_p if args.top_p is not None else base.top_p,
                )
            )
        else:
            raise SystemExit(f"Unknown task {name!r}. Available: {', '.join(sorted(TASK_REGISTRY))}, custom")
    return tasks


def build_model(args: argparse.Namespace):
    """Resolve the model instance from pcai_models or from url+api_key.

    ``pcai_models`` is imported lazily — and only when ``--model_class_name``
    is given — so the script stays runnable from the hardlinked copy of the
    repo, where ``pcai_models.py`` is excluded by ``hardlink_config.json``.
    """
    if args.model_class_name:
        try:
            from model_benchmarker.utils import pcai_models
        except ImportError as exc:
            raise SystemExit(
                f"--model_class_name={args.model_class_name!r} requires the "
                "model_benchmarker/utils/pcai_models.py module, which is not "
                f"available here ({exc}). It is excluded from the hardlink "
                "deployment; use --url and --api_key instead."
            ) from exc

        model = getattr(pcai_models, args.model_class_name, None)
        if model is None:
            available = ", ".join(pcai_models.__all__)
            raise SystemExit(f"Unknown --model_class_name={args.model_class_name!r}. Available: {available}")
    else:
        if not args.url:
            raise SystemExit("--url is required when --model_class_name is not given.")
        from model_benchmarker.utils.pcai_model_classes import ChatModel

        # api_key is optional: a key-less endpoint gets an empty key, and the
        # OpenAI client is built with credential enforcement relaxed so no
        # Authorization header is sent (see stream_once / _openai_client_kwargs).
        # Resolution order: --api_key > --api_key_file > $PCAI_API_KEY.
        model = ChatModel(url_remote=args.url, api_key=resolve_api_key(args))

    if args.remote:
        model.remote()

    if not model.model_name:
        model.model_name = model._discover_model_name() or ""
        if not model.model_name:
            print("Warning: could not auto-discover the model name; using an empty model id.")
    return model


THINKING_BUDGET_LEVELS: dict[str, int] = {
    "off": 0,
    "low": 1024,
    "medium": 4096,
    "high": 16384,
    "x-high": 32768,
}

FILLER_BLOCK = (
    "This is auxiliary context text included solely to extend the input "
    "length for benchmarking purposes; it is not part of the task and "
    "should be ignored. The sentence is repeated many times until the "
    "target context size is reached. "
)


def build_chat_template_kwargs(args: argparse.Namespace) -> dict | None:
    """Resolve per-request reasoning overrides (Qwen3-style chat_template_kwargs).

    Returns None when no override is requested, so the server default stands.
    """
    kwargs: dict = {}
    if args.disable_thinking:
        kwargs["enable_thinking"] = False
    elif args.enable_thinking:
        kwargs["enable_thinking"] = True

    budget = args.thinking_level if args.thinking_level is not None else args.thinking_budget
    if budget is not None:
        if isinstance(budget, str):
            budget = THINKING_BUDGET_LEVELS[budget]
        kwargs["thinking_token_budget"] = int(budget)

    return {"chat_template_kwargs": kwargs} if kwargs else None


def render_prompt(task: Task, context_length: int = 0, no_nonce: bool = False) -> str:
    """Render the request prompt, optionally padded to ``context_length`` tokens.

    By default a unique random nonce is prepended so no two requests share any
    prefix tokens — this defeats server-side prefix/KV caching (vLLM
    RadixAttention, SGLang prefix cache, response caches keyed on exact input)
    across the whole benchmark run, so measurements reflect real generation,
    not cache hits.  When ``no_nonce`` is True, a fixed shared marker is used
    instead so *all* requests share an identical prefix (server cache reuse).
    When ``context_length > 0``, neutral filler text is inserted between the
    nonce header and the task body so the request is padded to ~``context_length``
    input tokens.
    """
    prompt = task.prompt
    nonce = _SHARED_NONCE if no_nonce else secrets.token_hex(6)
    if "{nonce}" in prompt:
        # str.replace, not str.format: custom prompts routinely contain literal
        # braces (dict/JSON examples) that would make .format() raise KeyError.
        prompt = prompt.replace("{nonce}", nonce)
    else:
        prompt = f"[{nonce}]\n{prompt}"

    if context_length > 0:
        target_chars = context_length * 4
        padding = target_chars - len(prompt)
        if padding > 0:
            filler = (FILLER_BLOCK * (padding // len(FILLER_BLOCK) + 1))[:padding]
            nl = prompt.find("\n")
            cut = nl + 1 if nl != -1 else 0
            prompt = prompt[:cut] + filler + "\n" + prompt[cut:]
    return prompt


def _debug_first_delta(tag: str, delta, debug_stream: bool) -> None:
    """Debug print the first text-bearing delta (the chunk that set TTFT)."""
    if not debug_stream:
        return
    extra = getattr(delta, "model_extra", None) or getattr(delta, "__pydantic_extra__", None) or {}
    combined = {**delta.model_dump(), **extra}
    text_fields = {k: v for k, v in combined.items() if isinstance(v, str) and v}
    print(f"  [debug] first token via {tag}: keys={sorted(combined.keys())}")
    print(f"  [debug]   non-empty text fields: {text_fields}")


async def stream_once(
    model,
    task: Task,
    context_length: int = 0,
    extra_body: dict | None = None,
    debug_stream: bool = False,
    messages: list[dict] | None = None,
    no_nonce: bool = False,
) -> RequestResult:
    """One streamed completion: returns TTFT and generation tokens/s.

    ``first_token_at`` is set on the first chunk carrying *any* text delta
    (``content`` or ``reasoning_content``), since thinking models stream
    reasoning tokens before any answer content.

    ``messages`` optionally overrides the default single ``user`` message
    with a full conversation (used for multiturn requests); the assistant
    text streamed back is returned in ``response_text`` so it can be
    appended to the history for the next turn.  ``response_text`` carries
    only ``content`` deltas — reasoning tokens are counted but NOT replayed
    into the conversation history, matching what real chat clients resend.
    """
    t0 = time.perf_counter()
    first_token_at: float | None = None
    content_deltas = 0
    reasoning_deltas = 0
    usage_tokens: int | None = None
    content_parts: list[str] = []
    try:
        if messages is None:
            messages = [{"role": "user", "content": render_prompt(task, context_length, no_nonce)}]
        kwargs: dict = {
            "model": model.model_name,
            "messages": messages,
            "max_tokens": task.max_tokens,
            "temperature": task.temperature,
            "top_p": task.top_p,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if not model.api_key:
            # Key-less endpoint: openai>=2 requires the Authorization header to
            # be *explicitly omitted* per request (its _validate_headers raises
            # otherwise); the Omit sentinel also guarantees no empty Bearer
            # header is sent. Harmless no-op on openai 1.x.
            kwargs["extra_headers"] = {"Authorization": _OMIT()}
        stream = await model.async_client.chat.completions.create(**kwargs)
        async for chunk in stream:
            now = time.perf_counter()
            if getattr(chunk, "usage", None) is not None and chunk.usage.completion_tokens is not None:
                usage_tokens = chunk.usage.completion_tokens
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta is not None:
                    content = getattr(delta, "content", None)
                    reasoning = getattr(delta, "reasoning_content", None)
                    if content:
                        if first_token_at is None:
                            first_token_at = now
                            _debug_first_delta("content", delta, debug_stream)
                        content_deltas += 1
                        content_parts.append(content)
                    elif reasoning:
                        if first_token_at is None:
                            first_token_at = now
                            _debug_first_delta("reasoning_content", delta, debug_stream)
                        reasoning_deltas += 1
                    elif first_token_at is None:
                        # Some Qwen3 servers stream text under other delta
                        # fields (e.g. `reasoning`), which the SDK keeps in
                        # model_extra. Treat any non-empty, non-role string
                        # delta as a token so TTFT never falls back to 0 on
                        # an unknown field name.
                        extra = getattr(delta, "model_extra", None) or getattr(delta, "__pydantic_extra__", None) or {}
                        combined = {**delta.model_dump(), **extra}
                        if any(isinstance(v, str) and v for k, v in combined.items() if k != "role"):
                            first_token_at = now
                            _debug_first_delta("model_extra", delta, debug_stream)
    except Exception as exc:
        return RequestResult(success=False, task=task.name, error=str(exc))

    t_end = time.perf_counter()
    if first_token_at is None:
        first_token_at = t0

    tokens = usage_tokens if usage_tokens is not None else (content_deltas + reasoning_deltas)
    total_time = t_end - t0
    gen_time = t_end - first_token_at
    if gen_time <= 0 or gen_time < 0.05 * total_time:
        # Whole response arrived in one burst (short generations): the
        # first token and stream end are ~simultaneous, so throughput is
        # meaningless — fall back to the full request time.
        gen_time = total_time
    tokens_per_s = tokens / gen_time if gen_time > 0 else 0.0
    if debug_stream:
        print(
            f"  [debug] content_chunks={content_deltas} reasoning_chunks={reasoning_deltas} "
            f"usage_tokens={usage_tokens} first_token={(first_token_at - t0) * 1000:.0f}ms"
        )
    return RequestResult(
        success=True,
        task=task.name,
        ttft_s=first_token_at - t0,
        tokens=tokens,
        tokens_per_s=tokens_per_s,
        response_text="".join(content_parts),
    )


async def run_concurrency(
    model,
    tasks: list[Task],
    n_users: int,
    requests_per_user: int,
    context_length: int = 0,
    extra_body: dict | None = None,
    debug_stream: bool = False,
    multiturn: bool = False,
    no_nonce: bool = False,
    quiet: bool = False,
) -> list[RequestResult]:
    sem = asyncio.Semaphore(n_users)

    async def _user(uid: int) -> list[RequestResult]:
        results: list[RequestResult] = []
        rng = random.Random(uid)  # deterministic per-user seed; reproducible
        history: list[dict] = []
        for i in range(requests_per_user):
            # Each round (len(tasks) requests) uses a freshly shuffled
            # permutation, so every task appears exactly once per round but
            # the order is randomized per user.  Also keeps the concurrency
            # level an even mix of all tasks at any instant.
            if i % len(tasks) == 0:
                round_order = list(tasks)
                rng.shuffle(round_order)
            task = round_order[i % len(tasks)]
            if multiturn:
                # Carry the whole conversation forward: all prior user +
                # assistant turns are replayed, and only the newest user
                # prompt gets a fresh nonce, so the shared prefix *is*
                # reused by the server's prefix/KV cache across turns.
                #
                # context_length padding is applied only to turn 1 (the
                # "prefill" turn); later turns append just the new task
                # prompt so the measured TTFT reflects reuse of that prefix.
                pad = context_length if i == 0 else 0
                messages: list[dict] | None = history + [
                    {"role": "user", "content": render_prompt(task, pad, no_nonce)}
                ]
            else:
                messages = None
            async with sem:
                res = await stream_once(
                    model,
                    task,
                    context_length,
                    extra_body,
                    debug_stream,
                    messages=messages,
                    no_nonce=no_nonce,
                )
            res.turn = i + 1
            results.append(res)
            if multiturn and messages is not None:
                if res.success:
                    history = messages + [{"role": "assistant", "content": res.response_text}]
                else:
                    # Keep the conversation going even if a turn failed so the
                    # user's remaining turns stay aligned with the round order.
                    history = messages
            if not quiet:
                # print() is a blocking write on the event-loop thread and this
                # lock serializes every user; a backpressured stdout (pipe, slow
                # SSH pane) would stall chunk processing for ALL in-flight
                # streams and distort TTFT/tokens-s. --quiet skips it entirely.
                async with _print_lock:
                    if res.success:
                        print(
                            f"  [N={n_users} user={uid} req={i + 1}/{requests_per_user}] "
                            f"task={task.name} -> TTFT={res.ttft_s * 1000:.0f}ms, "
                            f"{res.tokens} tok, {res.tokens_per_s:.1f} tok/s"
                        )
                    else:
                        print(
                            f"  [N={n_users} user={uid} req={i + 1}/{requests_per_user}] "
                            f"task={task.name} FAILED: {res.error}"
                        )
        return results

    grouped = await asyncio.gather(*(_user(i) for i in range(n_users)))
    return [r for g in grouped for r in g]


def stats(values: list[float], inverted: bool = False) -> dict[str, float]:
    """Percentile summary. For tokens/s, ``inverted`` reports the "slow" tail:
    higher t/s is better, so P100 is the *slowest* measurement and P95/P99
    are the t/s values that 95%/99% of requests met or exceeded."""
    arr = np.asarray(values, dtype=float)
    qmap = {"P50": 50, "P95": 5, "P99": 1, "P100": 0} if inverted else {"P50": 50, "P95": 95, "P99": 99, "P100": 100}
    return {p: float(np.percentile(arr, qmap[p])) for p in PCTS}


async def prewarm(
    model,
    tasks: list[Task],
    context_length: int,
    extra_body: dict | None,
    debug_stream: bool,
) -> None:
    """Populate the server's prefix cache ahead of a concurrency sweep.

    Sends one warm-up request per task using the *fixed* shared prefix (no
    random nonce), so the prefix graph is resident before the users fire.
    Only helps when the subsequent requests share that exact prefix — i.e.
    when the run uses --no-nonce; otherwise the warm prefix is never matched.
    """
    print(f"Prewarming ctx={context_length} cache (one shared-prefix request per task)...")
    for task in tasks:
        messages = [{"role": "user", "content": render_prompt(task, context_length, no_nonce=True)}]
        res = await stream_once(
            model,
            task,
            context_length,
            extra_body,
            debug_stream,
            messages=messages,
            no_nonce=True,
        )
        if res.success:
            print(f"  warm {task.name}: TTFT={res.ttft_s * 1000:.0f}ms, {res.tokens} tok")
        else:
            print(f"  warm {task.name} FAILED: {res.error}")


PCTS = ("P50", "P95", "P99", "P100")
_print_lock = asyncio.Lock()
_CELL = 10


def _pct_header() -> str:
    return "".join(f"{p:>{_CELL}}" for p in PCTS)


def print_table(
    rows: list[tuple[int, int, str, int, dict[str, float], dict[str, float] | None, dict[str, float]]],
    *,
    file: TextIO | None = None,
    multiturn: bool = False,
) -> None:
    """One row per (ctx, users, task): TTFT percentiles for turn 1 and for
    turns 2+ ('TTFT-post', the prefix-reuse turns) plus tokens/s percentiles,
    side by side under a single header, separated by vertical rules, with a
    dotted line whenever ctx or users changes.  A 'failed' column reports
    per-row request failures.  A None TTFT-post renders dashes (no turn 2+
    to aggregate, or not in multiturn mode)."""
    ttft_label = "TTFT turn1 (ms)" if multiturn else "TTFT (ms)"
    post_label = "TTFT-post (ms)"
    title_row = (
        f"{'':>8} {'':>6} {'':>10} {'':>6}    |    "
        f"{ttft_label:^{len(_pct_header())}}    |    "
        f"{post_label:^{len(_pct_header())}}    |    "
        f"{'tokens/s':^{len(_pct_header())}}"
    )
    pct_row = (
        f"{'ctx':>8} {'users':>6} {'task':>10} {'failed':>6}    |    "
        f"{_pct_header()}    |    {_pct_header()}    |    {_pct_header()}"
    )
    print(title_row, file=file)
    print(pct_row, file=file)
    sep = "-" * len(pct_row)
    print(sep, file=file)
    prev_key = None
    for ctx, n_users, task, n_fail, ttft, ttft_post, tps in rows:
        key = (ctx, n_users)
        if prev_key is not None and key != prev_key:
            print(sep, file=file)
        prev_key = key
        blocks = []
        for s in (ttft, ttft_post, tps):
            if s is None:
                blocks.append("".join(f"{'-':>{_CELL}}" for _ in PCTS))
            else:
                blocks.append("".join(f"{s[p]:>{_CELL}.1f}" for p in PCTS))
        line = f"{ctx:>8} {n_users:>6} {task:>10} {n_fail:>6}    |    " + "    |    ".join(blocks)
        print(line, file=file)


async def _run_level(
    model,
    tasks: list[Task],
    n_users: int,
    ctx: int,
    args: argparse.Namespace,
    rows: list[tuple[int, int, str, int, dict[str, float], dict[str, float] | None, dict[str, float]]],
) -> None:
    """Run one (ctx, users) level: concurrency of ``n_users``, each doing
    ``requests_per_user`` requests over the given ``tasks``, then append the
    per-task summary rows to ``rows``."""
    print(f"Running ctx={ctx} N={n_users} users (requests_per_user={args.requests_per_user})...")
    results = await run_concurrency(
        model,
        tasks,
        n_users,
        args.requests_per_user,
        ctx,
        args.extra_body,
        args.debug_stream,
        multiturn=args.multiturn,
        no_nonce=args.no_nonce,
        quiet=args.quiet,
    )
    successes = [r for r in results if r.success]
    failures = len(results) - len(successes)
    if not successes:
        print(f"  all {len(results)} requests failed — skipping level.")
        print(f"  first error: {results[0].error if results else 'n/a'}")
        return
    level_rows: list[tuple[int, int, str, int, dict[str, float], dict[str, float] | None, dict[str, float]]] = []
    for task in tasks:
        group = [r for r in successes if r.task == task.name]
        if not group:
            continue
        group_fail = sum(1 for r in results if r.task == task.name and not r.success)
        ttfts = [r.ttft_s * 1000.0 for r in group]
        tpss = [r.tokens_per_s for r in group]
        if args.multiturn:
            # Split TTFT by turn depth: turn 1 pays the full prefill;
            # turns 2+ reuse the shared prefix, so their TTFT shows the
            # warm-path (prefix/KV cache) latency.
            turn1 = [r.ttft_s * 1000.0 for r in group if r.turn == 1]
            post = [r.ttft_s * 1000.0 for r in group if r.turn >= 2]
            ttft_post = stats(post) if post else None
            if turn1:
                ttfts = turn1
        else:
            ttft_post = None
        level_rows.append(
            (
                ctx,
                n_users,
                task.name,
                group_fail,
                stats(ttfts),
                ttft_post,
                stats(tpss, inverted=True),
            )
        )
    if failures:
        print(f"  {failures}/{len(results)} requests failed (excluded from stats)")
    if level_rows:
        print(f"\n--- preview ctx={ctx} users={n_users} ---")
        print_table(level_rows, multiturn=args.multiturn)
        print()
    rows.extend(level_rows)


async def main() -> None:
    args = parse_args()
    if args.requests_per_user < 1:
        raise SystemExit("--requests_per_user must be >= 1")
    try:
        user_levels = [int(x) for x in args.number_users.split(",") if x.strip()]
    except ValueError:
        raise SystemExit("--number_users must be a comma-separated list of ints (e.g. 1,2,4,8)")
    if not user_levels:
        raise SystemExit("--number_users must contain at least one integer")
    try:
        ctx_levels = [int(x) for x in args.context_length.split(",") if x.strip()]
    except ValueError:
        raise SystemExit("--context_length must be a comma-separated list of ints (e.g. 8192,32768,65536)")
    if not ctx_levels or any(c < 0 for c in ctx_levels):
        raise SystemExit("--context_length must be a comma-separated list of non-negative ints")

    tasks = resolve_tasks(args)
    model = build_model(args)
    # Warn when the requested concurrency exceeds the client connection pool:
    # excess requests queue INSIDE the measured TTFT window, so the top level
    # silently measures "pool_max concurrent streams + queueing" instead of N.
    try:
        from model_benchmarker.utils.pcai_model_classes import model_pool_max_connections

        pool_max = model_pool_max_connections()
        if max(user_levels) > pool_max:
            print(
                f"WARNING: max --number_users ({max(user_levels)}) exceeds the httpx "
                f"connection-pool limit ({pool_max}; MODEL_POOL_MAX_CONNECTIONS). Requests "
                "beyond the limit queue client-side and inflate measured TTFT — raise the "
                "env var to at least your max concurrency."
            )
    except ImportError:
        pass
    extra_body = build_chat_template_kwargs(args)
    args.extra_body = extra_body
    task_desc = ", ".join(f"{t.name}(max_tokens={t.max_tokens})" for t in tasks)
    print(
        f"Model: {model.__class__.__name__} name={model.model_name!r} "
        f"usage={model.model_usage} tasks=[{task_desc}] "
        f"requests_per_user={args.requests_per_user} "
        f"context_lengths={ctx_levels}"
    )
    if args.multiturn:
        print(
            f"MODE: multiturn — each user runs {args.requests_per_user} turns of one "
            "conversation (shared prefix reused across turns; server prefix/KV cache engages)."
        )
    if args.no_nonce:
        print("MODE: no-nonce — every request uses a fixed shared prefix (server prefix cache reuse enabled).")
    if args.prewarm:
        print(
            "MODE: prewarm — one shared-prefix warm-up request per (ctx, task) before each sweep "
            "(prefix graph populated ahead of the users)."
        )
    if extra_body:
        print(f"extra_body: {extra_body}")
    print(
        "tokens/s = completion_tokens / time-from-first-token-to-stream-end "
        "(TTFT excluded); completion_tokens from stream usage, else counted "
        "content chunks.\n"
    )

    rows: list[tuple[int, int, str, int, dict[str, float], dict[str, float] | None, dict[str, float]]] = []
    for ctx in ctx_levels:
        if args.prewarm:
            await prewarm(model, tasks, ctx, args.extra_body, args.debug_stream)
            print()
        for n_users in user_levels:
            if args.separate_tasks:
                # One dedicated (ctx, users, task) pass per task: all N users
                # run their turns on just this task back-to-back, so each task
                # gets a clean turn-1 (prefill) vs turns 2+ (prefix reuse) split.
                for task in tasks:
                    await _run_level(
                        model,
                        [task],
                        n_users,
                        ctx,
                        args,
                        rows,
                    )
            else:
                await _run_level(
                    model,
                    tasks,
                    n_users,
                    ctx,
                    args,
                    rows,
                )

    if not rows:
        raise SystemExit("No successful requests; nothing to report.")

    if args.multiturn:
        summary_head = "\nPer-level latency / throughput (multiturn: shared prefix reused across each user's turns):"
    elif args.no_nonce:
        summary_head = (
            "\nPer-level latency / throughput (no-nonce: all requests share one fixed prefix, "
            "server prefix cache can reuse):"
        )
    else:
        summary_head = "\nPer-level latency / throughput (unique-nonce requests, no cache reuse):"
    summary_note = "TTFT (ms): P50/P95/P99/P100 ascending;  tokens/s: higher is better so percentiles are inverted (P100 = slowest)."
    if args.multiturn:
        summary_note += (
            "\nTTFT turn1 = first request (full prefill);  TTFT-post = turns 2+ "
            "(shared prefix reused — shows prefix-cache benefit)."
        )
    print(summary_head)
    print(summary_note)
    print_table(rows, multiturn=args.multiturn)
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(summary_head + "\n")
            fh.write(summary_note + "\n")
            print_table(rows, file=fh, multiturn=args.multiturn)
        print(f"Table written to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
