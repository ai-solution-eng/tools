"""Context-window discovery and request-fit estimation.

WHY THIS MODULE EXISTS (2026-10-29). An OpenAI-compatible server admits a
request only when `prompt_tokens + max_tokens <= max_model_len`; violate it
and it answers 400 BEFORE sampling. The gateway knew only the second term, and
only as a hand-maintained constant (`settings.max_tokens_ceiling`), so the
first term was frozen at whatever prompt size the operator happened to imagine.

The live defect that forced this: G2 clamps `Qwen3.8-27B` to 196608 against
its 262144 window, i.e. it assumes a prompt <= 65536 tokens. An agent harness
turn is 81515-92912 prompt tokens (measured on the DSH profile's own traffic,
2026-10-29) because the MCP tool catalog dominates it, so every Qwen-served
turn computed 196608 + ~85000 > 262144 and 400'd. The clamp was doing its job;
it was simply solving an inequality with one term missing.

So the gateway learns the term the upstream already publishes: every vLLM and
SGLang `GET /v1/models` card carries `max_model_len` (verified live on all
three G2 chat pools: Qwen 262144, GLM 1048576, Deepseek 1048576). It is read
ONCE per endpoint, lazily, on the forward path where the upstream headers are
already resolved -- no schema change, no second credential path, and a
deployment that changes its window corrects itself on the next probe.

DISCIPLINE (this runs on the request path):
  * discovery is BEST-EFFORT and FAIL-OPEN: an unknown window is `None`,
    which every caller reads as "no opinion" -- exactly today's behaviour;
  * the probe never blocks a request (it is scheduled, not awaited);
  * the estimate must never UNDER-count, because under-counting is what 400s.
    It is deliberately rough in the safe direction: a len//4 text charge plus a
    flat per-image charge, never base64 length (a 1.5MB PNG once scored ~500K
    "tokens" and pinned every vision call to the ceiling -- see
    model_routing._IMAGE_TOKEN_EQUIVALENT for the same lesson).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..routing.router import upstream_client

log = logging.getLogger(__name__)

# Bound on the discovery GET. It runs in the background, so this is a
# "do not leak a task" ceiling rather than a latency budget.
MAX_PROBE_SECONDS = 5.0

# Reserved out of every window: our len//4 estimate disagrees with the served
# model's tokenizer, and the difference is not signed. 1024 tokens is cheap
# against a 262144 window and covers the disagreement on the measured traffic.
SAFETY_MARGIN_TOKENS = 1024

# A completion shorter than this is not worth attempting: at that point the
# model cannot answer, so the candidate must be dropped rather than clamped
# into a truncated reply.
MIN_COMPLETION_TOKENS = 256

# Flat per-image charge, in tokens. Matches model_routing's vision constant so
# the two halves of the gateway agree on what an image costs.
IMAGE_TOKEN_EQUIVALENT = 1600

# base_url -> max_model_len. Keyed by the URL, not the endpoint id: two
# endpoints pointing at one server share a window, and a re-registered
# endpoint (new id, same server) must not lose what we already learned.
_WINDOWS: dict[str, int] = {}
# base_url -> monotonic time the window was LEARNED (audit w6 F1). Distinct
# from _LAST_ATTEMPT ("when we last TRIED"): a learned window older than
# WINDOW_TTL_S is due again, while a failed attempt is throttled. Different
# questions, so the two ages are kept apart.
_WINDOWS_AT: dict[str, float] = {}
# base_urls with a probe in flight, so a burst of requests schedules ONE probe.
_INFLIGHT: set[str] = set()
# base_url -> monotonic time of the last attempt. A FAILED probe (401, no card,
# unreachable) must not be retried on every registry refresh: without this a
# misconfigured endpoint turns a boot-time probe into a permanent polling loop.
_LAST_ATTEMPT: dict[str, float] = {}
# base_url -> monotonic time it ANSWERED without a max_model_len. Separate from
# _LAST_ATTEMPT because the two mean different things: "it failed" (retry
# shortly) versus "it does not publish one" (retry rarely).
_NO_WINDOW: dict[str, float] = {}

# Minimum gap between attempts for one upstream, whatever the outcome.
PROBE_RETRY_SECONDS = 60.0

# An upstream that ANSWERED with a card carrying no max_model_len is not a
# transient failure — it is a fact (the vLLM-SR decision runtime publishes
# family/surfaces instead, and some proxies publish nothing). Retrying it every
# minute is a wasted call and a log line per minute per pod forever: the 3.4.6
# sweep was caught doing exactly that, 8 times a pod in a few minutes. Ask
# again hourly, so an upstream that later starts publishing is still picked up.
NO_WINDOW_RETRY_SECONDS = 3600.0

# How long a LEARNED window is trusted before it is re-probed (audit w6 F1).
# An upstream that changes its max_model_len (a re-deployed model, a window
# shrunk to leave KV room) publishes the new value on the next /v1/models. With
# no TTL the pod kept the OLD window until it restarted, so a stale LARGER
# window over-sized every request on that pool and 400'd — the exact failure
# this module exists to remove. One metadata GET per endpoint per hour is cheap
# and makes an operator change land without a restart.
WINDOW_TTL_S = 3600.0


def _norm(base_url: str) -> str:
    return base_url.rstrip("/")


def cached(base_url: str | None) -> int | None:
    """The known window for this upstream, or None when not learned yet. PURE."""
    if not base_url:
        return None
    return _WINDOWS.get(_norm(base_url))


def remember(base_url: str, limit: Any) -> int | None:
    """Record a window read from a card. Ignores anything unusable.

    bool is rejected for the same reason clamp_max_tokens rejects it: True
    would read as a one-token window and clamp every request to nothing.
    """
    if not base_url or isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return None
    key = _norm(base_url)
    _WINDOWS[key] = limit
    _WINDOWS_AT[key] = time.monotonic()
    return limit


def stale(base_url: str | None) -> bool:
    """Is this upstream's LEARNED window older than WINDOW_TTL_S? PURE.

    False for an upstream with no window: "unknown" is a DIFFERENT state (the
    callers read it as "no opinion") and the sweep collects it separately. A
    window whose learned-at is missing reads STALE — an unmeasurable age must
    not be trusted forever. The -inf default is the repo's documented sentinel
    idiom (core/costs.py:22-27, core/housekeeping.py:41-46): 0.0 would compare
    against host uptime and report a fresh window as ancient on a fresh pod.
    """
    if not base_url:
        return False
    key = _norm(base_url)
    if key not in _WINDOWS:
        return False
    return time.monotonic() - _WINDOWS_AT.get(key, float("-inf")) >= WINDOW_TTL_S


def forget_all() -> None:
    """Drop the cache. Tests only -- production learns and keeps (the TTL is
    what refreshes a learned window in production)."""
    _WINDOWS.clear()
    _WINDOWS_AT.clear()
    _INFLIGHT.clear()
    _LAST_ATTEMPT.clear()
    _NO_WINDOW.clear()


def read_card(card: Any) -> int | None:
    """max_model_len from one /v1/models reply. PURE, never raises.

    Accepts both shapes an OpenAI-compatible listing uses: a `data` array or
    a `models` map. The LARGEST window wins when a server lists several
    models: the gateway routes a virtual model to a whole pool, and refusing to
    serve because one sibling is smaller would be a guess, not a fact.
    """
    if not isinstance(card, dict):
        return None
    rows: list[Any] = []
    data = card.get("data")
    if isinstance(data, list):
        rows = data
    else:
        models = card.get("models")
        if isinstance(models, dict):
            rows = list(models.values())
    best: int | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("max_model_len")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        if best is None or value > best:
            best = value
    return best


async def probe(client: Any, base_url: str, headers: dict[str, str]) -> int | None:
    """GET {base_url}/models and learn its window. NEVER raises.

    `client` is the shared httpx client the forward path already holds, and
    `headers` are the upstream headers already resolved for this request --
    which is the whole reason discovery lives here rather than at registry
    refresh: the credential resolution (by-ref Secret name or Fernet-encrypted
    key) happens on this path and nowhere else, and a second copy of it would
    be a second thing to get wrong.
    """
    if not base_url:
        return None
    key = _norm(base_url)
    known = _WINDOWS.get(key)
    # A FRESH learned window is final; a STALE one is due again (audit w6 F1).
    # The known value stays the fallback for every path below: a failed REFRESH
    # must never regress a known window to "unknown" (that would un-clamp the
    # pool until the next successful probe — the very 400s this module removes).
    if known is not None and not stale(key):
        return known
    if key in _INFLIGHT:
        return known
    # Backoff, applied to EVERY outcome: a 401, a card with no max_model_len
    # and a timeout are all "we do not know", and retrying them on every
    # refresh would turn a misconfiguration into a polling loop.
    #
    # SENTINEL = -inf, NEVER 0.0 (audit w6, P1). time.monotonic() is HOST
    # UPTIME, not wall time, so "now - 0.0 < interval" is TRUE for the first
    # interval of uptime: a 0.0 default silently SKIPPED every probe for the
    # first hour after a pod start, which is the window this module exists to
    # learn. core/costs.py:22-27 documents the same defect and fix ("NEGATIVE
    # INFINITY, not 0.0"); core/housekeeping.py:41-46 uses the None variant.
    # Same idiom here — do not reintroduce a 0.0 default.
    now = time.monotonic()
    if now - _NO_WINDOW.get(key, float("-inf")) < NO_WINDOW_RETRY_SECONDS:
        return known
    if now - _LAST_ATTEMPT.get(key, float("-inf")) < PROBE_RETRY_SECONDS:
        return known
    _LAST_ATTEMPT[key] = now
    _INFLIGHT.add(key)
    try:
        resp = await client.get(f"{key}/models", headers=headers, timeout=MAX_PROBE_SECONDS)
        if getattr(resp, "status_code", 0) != 200:
            # WARNING, not INFO (2026-10-29): an unknown window is no longer
            # cosmetic — it means this pod forwards the client's cap unclamped
            # and 400s on the very requests this module exists to fix. The 3.4.5
            # rollout shipped exactly that: one replica logged this at INFO,
            # kept serving the pre-fix behaviour on half the traffic, and
            # nothing in the logs said so.
            log.warning(
                "context window: %s/models -> %s (window unknown; this pod cannot clamp for it)",
                key,
                getattr(resp, "status_code", "?"),
            )
            return known
        limit = read_card(resp.json())
        if limit is None:
            # A fact, not a failure: remember it so the retry sweep stops
            # asking (see NO_WINDOW_RETRY_SECONDS).
            _NO_WINDOW[key] = time.monotonic()
            log.info(
                "context window: %s advertises no max_model_len (window unknown; not retried for %.0fs)",
                key,
                NO_WINDOW_RETRY_SECONDS,
            )
            return known
        _WINDOWS[key] = limit
        _WINDOWS_AT[key] = time.monotonic()
        log.info("context window: learned max_model_len=%d for %s", limit, key)
        return limit
    except Exception as exc:
        log.warning("context window: probe of %s failed (%s) - keeping the last known window", key, type(exc).__name__)
        return known
    finally:
        _INFLIGHT.discard(key)


def text_tokens(text: str) -> int:
    """Token charge for one text blob. PURE, never raises.

    ASCII at len//4 -- byte-identical to the shipped estimator, so ASCII-only
    traffic does not shift. Every NON-ASCII character costs at least ONE token
    (audit w6 F2): no tokenizer can encode a CJK character or an emoji in a
    quarter of a token, so len//4 under-counted those ~4x and an UNDER-count is
    what 400s (the module docstring's invariant is the opposite direction).

    PUBLIC (audit 2026-11-10 P2-21): the admission/billing estimator in
    routes_proxy is the same charge for the same reason, and it had drifted
    back to a bare len//4 there — TPM simply did not bind for CJK/emoji
    traffic. One implementation, two callers, so the two can never disagree.
    """
    if text.isascii():
        return len(text) // 4
    ascii_chars = 0
    for ch in text:
        if ch < "\x80":
            ascii_chars += 1
    return ascii_chars // 4 + (len(text) - ascii_chars)


def _message_text_tokens(content: Any) -> int:
    """Token charge for one message's TEXT content. PURE, never raises.

    Aggregated per MESSAGE, not per part, so a pure-ASCII message keeps the
    shipped sum-of-lengths // 4 exactly. Non-text parts (image_url, audio,
    file) contribute nothing here: images ride the flat
    IMAGE_TOKEN_EQUIVALENT charge in the caller, never their base64 length.
    """
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    ascii_chars = 0
    non_ascii = 0
    for text in texts:
        if text.isascii():
            ascii_chars += len(text)
            continue
        for ch in text:
            if ch < "\x80":
                ascii_chars += 1
            else:
                non_ascii += 1
    return ascii_chars // 4 + non_ascii


def count_images(body: dict[str, Any]) -> int:
    """Image parts across the whole body. PURE. Mirrors model_routing's count."""
    total = 0
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                iu = part.get("image_url")
                url = iu.get("url") if isinstance(iu, dict) else iu
                if isinstance(url, str) and url:
                    total += 1
    return total


def estimate_context_tokens(body: dict[str, Any]) -> int:
    """How many prompt tokens this request will cost, roughly. PURE, never raises.

    DIFFERENT QUESTION FROM model_routing.estimate_prompt_tokens, deliberately:
    that one scores COMPLEXITY and therefore excludes the tool catalog, because
    a catalog is constant across an agent's turns and cannot separate easy work
    from hard. For FITTING a window the catalog is the opposite -- it is the
    bulk of the prompt (a 28-tool MCP surface is tens of thousands of tokens),
    and excluding it is precisely how a 65536-token headroom assumption died.

    Charges: ASCII text at len//4, NON-ASCII characters at >= 1 token each (an
    under-count is what 400s — see text_tokens), tool schemas at the same
    serialized charge, a flat per-image charge, and a small per-message
    overhead for role/framing tokens.
    """
    total = 0
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            total += _message_text_tokens(msg.get("content")) + 4
            # Tool-call arguments ride outside content on assistant turns.
            calls = msg.get("tool_calls")
            if isinstance(calls, list) and calls:
                try:
                    total += text_tokens(json.dumps(calls, default=str))
                except (TypeError, ValueError):
                    total += 64 * len(calls)
    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        try:
            total += text_tokens(json.dumps(tools, default=str))
        except (TypeError, ValueError):
            total += 256 * len(tools)
    total += count_images(body) * IMAGE_TOKEN_EQUIVALENT
    return max(total, 0)


def completion_cap(
    *,
    context_limit: int | None,
    est_prompt: int,
    requested: Any,
    ceiling: int | None = None,
) -> int | None:
    """The largest completion this attempt may ask for. PURE.

    None means "no opinion -- send the client's value unchanged", which is the
    case whenever the window is unknown. The result is the tightest of:
      * the client's own cap (a clamp never RAISES what was asked for),
      * the operator's ceiling (a cost policy, still honoured),
      * what is left of the window after the prompt and the safety margin.
    A window remainder below MIN_COMPLETION_TOKENS is NOT offered as a
    candidate: it is no opinion, not a truncated reply (same floor fits()
    uses).
    """
    candidates: list[int] = []
    if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
        candidates.append(requested)
    if isinstance(ceiling, int) and not isinstance(ceiling, bool) and ceiling > 0:
        candidates.append(ceiling)
    if context_limit is not None and context_limit > 0:
        headroom = context_limit - max(est_prompt, 0) - SAFETY_MARGIN_TOKENS
        if headroom <= 0:
            # The PROMPT alone does not fit. No completion cap can rescue this,
            # and clamping to zero would turn a clear upstream 400 into an
            # empty answer -- so express "no opinion" and let the caller's
            # candidate gate / failover handle it.
            return None
        if headroom < MIN_COMPLETION_TOKENS:
            # SAME DECISION ONE STEP EARLIER (audit 2026-11-10 P2-26). A
            # headroom below the minimum viable reply is not a fit: clamping
            # to it TRUNCATES the answer mid-sentence instead of failing over
            # to a candidate with a bigger window. fits() already drops such a
            # model from the ladder at exactly this floor, and the two
            # predicates must agree — the floor used to be read ONLY by
            # fits(), so the cap happily returned 7 tokens here.
            return None
        candidates.append(headroom)
    if not candidates:
        return None
    return min(candidates)


def fits(*, context_limit: int | None, est_prompt: int, min_completion: int = MIN_COMPLETION_TOKENS) -> bool:
    """Can this model serve this prompt at all? PURE.

    True when the window is unknown: an unlearned window must never remove a
    model from the ladder (that would be a routing decision made from a
    missing fact).
    """
    if context_limit is None or context_limit <= 0:
        return True
    return context_limit - est_prompt - SAFETY_MARGIN_TOKENS >= min_completion


async def refresh_windows(entries: list[tuple[str, dict[str, str]]]) -> int:
    """Learn every UNKNOWN or STALE window in one bounded pass. NEVER raises.

    Called by registry.refresh() -- at boot and after any committed admin
    mutation -- NOT from the request path. That placement is deliberate on two
    counts: a probe is an EXTRA UPSTREAM CALL, and a transparent proxy must not
    make one per request (it also turned every call-counting test into a false
    failure); and the window is a property of the ENDPOINT, so it should be
    learned when the endpoint set changes, not discovered by whichever request
    happens to arrive first.

    Entries are §(base_url, headers)§ pairs; headers come from
    core.upstream_auth, i.e. the same credential the forward path uses. Probes
    run CONCURRENTLY (they are independent) and are bounded by
    MAX_PROBE_SECONDS each. Unknown-but-not-due upstreams are skipped silently:
    returning 0 is the normal steady state.

    Returns the number of upstreams considered (0 = nothing to learn).
    """
    todo: list[tuple[str, dict[str, str]]] = []
    now = time.monotonic()
    for base_url, headers in entries:
        if not base_url:
            continue
        key = _norm(base_url)
        if key in _INFLIGHT:
            continue
        # A FRESH learned window is settled; a STALE one is due again (audit
        # w6 F1). This pass is the only thing that ever re-probes a KNOWN
        # upstream, so without this the TTL could never take effect.
        if key in _WINDOWS and not stale(key):
            continue
        # An upstream that answered "no max_model_len" is settled for an hour:
        # filtering it here (not only inside probe) also avoids building and
        # tearing down a client for a pass that can do no work.
        # -inf default, never 0.0 — see probe()'s SENTINEL note (audit w6).
        if now - _NO_WINDOW.get(key, float("-inf")) < NO_WINDOW_RETRY_SECONDS:
            continue
        todo.append((key, headers))
    if not todo:
        return 0
    client = upstream_client()
    try:
        await asyncio.gather(
            *(probe(client, key, headers) for key, headers in todo),
            return_exceptions=True,
        )
    finally:
        try:
            await client.aclose()
        except Exception:
            pass
    return len(todo)
