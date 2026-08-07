#!/usr/bin/env python3
"""Benchmark time-to-first-token (TTFT) and tokens/s for PCAI chat models.

Runs one model under a range of concurrency levels, mixing long task types
(coding, creative writing, ...), and prints per-task stats — average /
p95 / p99 / max for both metrics.

Every request gets a unique random nonce prepended to its prompt so the
shared serving cluster cannot reuse prefix/response caches (RadixAttention
etc.) — consecutive requests share no tokens, so measurements reflect real
generation, not cache hits.

Usage:
  # Use a model registered in pcai_models (deepseek in particular):
  python benchmark_chat.py --model_class_name deepseek_v4_flash_280B \
      --remote --number_users 1,2,4,8 --tasks coding,creative

  # Use an arbitrary OpenAI-compatible endpoint:
  python benchmark_chat.py --url "https://<endpoint>" --api_key "<key>" \
      --remote --number_users 1,4,8 --tasks coding

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

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

# The shared PCAI modules log model auto-discovery at WARNING during import;
# keep a clean CLI and let per-request failures surface in our own output.
logging.getLogger().setLevel(logging.ERROR)

DEFAULT_PROMPT = (
    "Write a concise structured essay about the history of computing, "
    "covering the 1950s to the present day, at least twelve paragraphs long."
)

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
    ttft_s: float = 0.0
    tokens: int = 0
    tokens_per_s: float = 0.0
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--model_class_name",
        default="",
        help="Name of a model variable in model_benchmarker/utils/pcai_models.py. "
        "If omitted, --url and --api_key are required.",
    )
    parser.add_argument("--url", default="", help="OpenAI-compatible base URL (root, no /v1).")
    parser.add_argument("--api_key", default="", help="Bearer token for the endpoint.")
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
    return parser.parse_args()


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
    """Resolve the model instance from pcai_models or from url+api_key."""
    if args.model_class_name:
        from model_benchmarker.utils import pcai_models

        model = getattr(pcai_models, args.model_class_name, None)
        if model is None:
            available = ", ".join(pcai_models.__all__)
            raise SystemExit(f"Unknown --model_class_name={args.model_class_name!r}. Available: {available}")
    else:
        if not args.url or not args.api_key:
            raise SystemExit("--url and --api_key are required when --model_class_name is not given.")
        from model_benchmarker.utils.pcai_model_classes import ChatModel

        model = ChatModel(url_remote=args.url, api_key=args.api_key)

    if args.remote:
        model.remote()

    if not model.model_name:
        model.model_name = model._discover_model_name() or ""
        if not model.model_name:
            print("Warning: could not auto-discover the model name; using an empty model id.")
    return model


FILLER_BLOCK = (
    "This is auxiliary context text included solely to extend the input "
    "length for benchmarking purposes; it is not part of the task and "
    "should be ignored. The sentence is repeated many times until the "
    "target context size is reached. "
)


def render_prompt(task: Task, context_length: int = 0) -> str:
    """Insert a unique nonce so no two requests share any prefix tokens.

    This defeats server-side prefix/KV caching (vLLM RadixAttention,
    SGLang prefix cache, response caches keyed on exact input) across the
    whole benchmark run.  When ``context_length > 0``, neutral filler text
    is inserted between the nonce header and the task body so the request
    is padded to ~``context_length`` input tokens.
    """
    prompt = task.prompt
    nonce = secrets.token_hex(6)
    if "{nonce}" in prompt:
        prompt = prompt.format(nonce=nonce)
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


async def stream_once(model, task: Task, context_length: int = 0) -> RequestResult:
    """One streamed completion: returns TTFT and generation tokens/s."""
    t0 = time.perf_counter()
    first_token_at: float | None = None
    content_deltas = 0
    usage_tokens: int | None = None
    try:
        stream = await model.async_client.chat.completions.create(
            model=model.model_name,
            messages=[{"role": "user", "content": render_prompt(task, context_length)}],
            max_tokens=task.max_tokens,
            temperature=task.temperature,
            top_p=task.top_p,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            now = time.perf_counter()
            if getattr(chunk, "usage", None) is not None and chunk.usage.completion_tokens is not None:
                usage_tokens = chunk.usage.completion_tokens
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    if first_token_at is None:
                        first_token_at = now
                    content_deltas += 1
    except Exception as exc:
        return RequestResult(success=False, task=task.name, error=str(exc))

    t_end = time.perf_counter()
    if first_token_at is None:
        first_token_at = t0

    tokens = usage_tokens if usage_tokens is not None else content_deltas
    total_time = t_end - t0
    gen_time = t_end - first_token_at
    if gen_time <= 0 or gen_time < 0.05 * total_time:
        # Whole response arrived in one burst (short generations): the
        # first token and stream end are ~simultaneous, so throughput is
        # meaningless — fall back to the full request time.
        gen_time = total_time
    tokens_per_s = tokens / gen_time if gen_time > 0 else 0.0
    return RequestResult(
        success=True,
        task=task.name,
        ttft_s=first_token_at - t0,
        tokens=tokens,
        tokens_per_s=tokens_per_s,
    )


async def run_concurrency(
    model, tasks: list[Task], n_users: int, requests_per_user: int, context_length: int = 0
) -> list[RequestResult]:
    sem = asyncio.Semaphore(n_users)

    async def _user(uid: int) -> list[RequestResult]:
        results: list[RequestResult] = []
        rng = random.Random(uid)  # deterministic per-user seed; reproducible
        for i in range(requests_per_user):
            # Each round (len(tasks) requests) uses a freshly shuffled
            # permutation, so every task appears exactly once per round but
            # the order is randomized per user.  Also keeps the concurrency
            # level an even mix of all tasks at any instant.
            if i % len(tasks) == 0:
                round_order = list(tasks)
                rng.shuffle(round_order)
            task = round_order[i % len(tasks)]
            async with sem:
                res = await stream_once(model, task, context_length)
            results.append(res)
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


PCTS = ("P50", "P95", "P99", "P100")
_print_lock = asyncio.Lock()
_CELL = 10


def _pct_header() -> str:
    return "".join(f"{p:>{_CELL}}" for p in PCTS)


def print_table(rows: list[tuple[int, int, str, int, dict[str, float], dict[str, float]]]) -> None:
    """One row per (ctx, users, task): TTFT percentiles and tokens/s
    percentiles side by side under a single header, separated by vertical
    rules, with a dotted line whenever ctx or users changes.  A 'failed'
    column reports per-row request failures."""
    title_row = (
        f"{'':>8} {'':>6} {'':>10} {'':>6}    |    "
        f"{'TTFT (ms)':^{len(_pct_header())}}    |    "
        f"{'tokens/s':^{len(_pct_header())}}"
    )
    pct_row = f"{'ctx':>8} {'users':>6} {'task':>10} {'failed':>6}    |    {_pct_header()}    |    {_pct_header()}"
    print(title_row)
    print(pct_row)
    sep = "-" * len(pct_row)
    print(sep)
    prev_key = None
    for ctx, n_users, task, n_fail, ttft, tps in rows:
        key = (ctx, n_users)
        if prev_key is not None and key != prev_key:
            print(sep)
        prev_key = key
        line = f"{ctx:>8} {n_users:>6} {task:>10} {n_fail:>6}    |"
        for i, s in enumerate((ttft, tps)):
            line += "    " + "".join(f"{s[p]:>{_CELL}.1f}" for p in PCTS)
            if i == 0:
                line += "    |"
        print(line)


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
    task_desc = ", ".join(f"{t.name}(max_tokens={t.max_tokens})" for t in tasks)
    print(
        f"Model: {model.__class__.__name__} name={model.model_name!r} "
        f"usage={model.model_usage} tasks=[{task_desc}] "
        f"requests_per_user={args.requests_per_user} "
        f"context_lengths={ctx_levels}"
    )
    print(
        "tokens/s = completion_tokens / time-from-first-token-to-stream-end "
        "(TTFT excluded); completion_tokens from stream usage, else counted "
        "content chunks.\n"
    )

    rows: list[tuple[int, int, str, int, dict[str, float], dict[str, float]]] = []
    for ctx in ctx_levels:
        for n_users in user_levels:
            print(f"Running ctx={ctx} N={n_users} users (requests_per_user={args.requests_per_user})...")
            results = await run_concurrency(model, tasks, n_users, args.requests_per_user, ctx)
            successes = [r for r in results if r.success]
            failures = len(results) - len(successes)
            if not successes:
                print(f"  all {len(results)} requests failed — skipping level.")
                print(f"  first error: {results[0].error if results else 'n/a'}")
                continue
            for task in tasks:
                group = [r for r in successes if r.task == task.name]
                if not group:
                    continue
                group_fail = sum(1 for r in results if r.task == task.name and not r.success)
                ttfts = [r.ttft_s * 1000.0 for r in group]
                tpss = [r.tokens_per_s for r in group]
                rows.append(
                    (
                        ctx,
                        n_users,
                        task.name,
                        group_fail,
                        stats(ttfts),
                        stats(tpss, inverted=True),
                    )
                )
            if failures:
                print(f"  {failures}/{len(results)} requests failed (excluded from stats)")

    if not rows:
        raise SystemExit("No successful requests; nothing to report.")

    print("\nPer-level latency / throughput (unique-nonce requests, no cache reuse):")
    print(
        "TTFT (ms): P50/P95/P99/P100 ascending;  tokens/s: higher is better so percentiles are inverted (P100 = slowest)."
    )
    print_table(rows)


if __name__ == "__main__":
    asyncio.run(main())
