"""Decision-model routing: the LLM-as-router seam.

WHY this module exists separately from core.model_routing: model_routing is
deliberately import-light and PURE -- a deterministic function of the request
body, no I/O, no extra model calls (see its docstring: "a router that calls a
model to pick a model doubles the latency and the cost of every request it
classifies"). That is still the right DEFAULT. This module adds the other half
of the story: when an operator has actually deployed a decision model
(vLLM-SR d3-turbo), they want it to make the choice, and they want to be able
to measure it before trusting it.

The split keeps both properties:
  * model_routing stays pure -- it gains ONE optional injected callable
    (decide), exactly like its existing `available` predicate seam, so the
    heuristic path is byte-identical when no decision model is configured;
  * everything that touches the network, the wire format, or the operator
    prompt lives HERE, injectable and testable without a live endpoint.

WIRE PROTOCOL (vLLM-SR "System One", verified against the deployed
vllm-sr/d3-turbo on 2026-10-23)::

    POST {base_url}/systemone
    {"model": ..., "state": <str>, "questions": {"answer": {"type": "choice",
     "instructions": <str>, "criteria": {option: description}}}}
    -> {"answers": {"answer": {"type": "choice", "choice": <option>,
                               "probabilities": {option: p}, "confidence": p}},
        "usage": {"input_tokens": n, "output_tokens": 0}}

A decision model NEVER generates text (output_tokens is always 0) and there is
NO /v1/chat/completions on the endpoint -- hence the separate call shape rather
than reusing the chat machinery.

WHY confidence gating rather than trust: d3-turbo returns CALIBRATED
probabilities, and a measured 3-way model choice on a real request returned
confidence 0.43 -- i.e. it genuinely does not always know. Serving a 0.43 guess
in preference to a deterministic heuristic would make answers WORSE, which
violates model_routing's "routing can only downgrade when the cheap model
positively fits, never as a side effect of a failure" contract. Below the
operator floor the heuristics stand; the deferral is recorded so the operator
can see how often the model abstains.

STATE BUDGET: the endpoint advertises validated_input_tokens: 32768 and its
input cap is 262144. The state this module builds is a COMPACT SUMMARY (a few
hundred tokens), never the conversation: a decision model reads the state once,
and shipping a 100k-token prompt would both blow the validated budget and
dominate the ~790ms measured decision latency.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Measured on the deployed d3-turbo (2026-10-23): ~790ms for a small state.
# The default timeout is generous enough for a cold start but bounded so a
# hung endpoint cannot park a chat request -- the caller fail-safe then
# serves the heuristics, which is exactly the pre-existing behaviour.
DEFAULT_TIMEOUT_MS = 2500
MIN_TIMEOUT_MS = 250
MAX_TIMEOUT_MS = 30000

# Below this the model is treated as having ABSTAINED. 0.60 was chosen against
# the measured distribution: a clear 2-way split scored 0.9997, an ambiguous
# 3-way model choice scored 0.43. Operators can move it per deployment.
DEFAULT_MIN_CONFIDENCE = 0.60

# The state is a summary; this is the hard ceiling on its JSON length so a
# pathological tool schema cannot inflate it past the endpoint validated
# input budget (32768 tokens ~ 131k chars; 12000 chars is a safe fraction).
MAX_STATE_CHARS = 12000

# The user's own words are the ONE per-request signal that carries task intent.
# Tools/system prompt/conversation depth are constant for an agent harness (the
# whole fleet sends the same catalog every turn), so they separate nothing --
# measured 2026-10-26: nine wildly different queries (a greeting, a diff
# review, a Raft implementation) all scored 713-722 and the decision model
# returned an identical 0.34 for every one. Sending the text fixed it: 9/9
# correct, confidence 0.75-0.99. Bounded because the endpoint validates 32768
# tokens and a base64 image part would otherwise blow the budget.
MAX_USER_TEXT_CHARS = 4000
# The conversation's ORIGINAL ask is a much shorter signal than a user turn, so
# it gets a tighter cap: it is a classification hint, not a payload.
MAX_USER_TASK_CHARS = 1000

# How many leading images to forward to a vision-capable decision model. Each
# costs real decision latency, and the routing question ("is this request
# about an image?") is answered by the first one; more adds cost without signal.
MAX_DECISION_IMAGES = 2


DECISION_MODES = ("off", "shadow", "active")

# The shipped router prompt: the SAME text as documentation/routing_text.md
# (asserted by tests/test_routing_prompt_default.py). An EMPTY operator prompt
# falls back to this constant, and the previous three-sentence default silently
# replaced the whole operator template on every deployment that had not saved
# one -- with nothing logging which text the model actually received.
DEFAULT_INSTRUCTIONS = """You are the model router for an LLM gateway. Pick the model to serve the next request.

Your pick is a proposal, not a command. The gateway applies its own confidence floor, a switch margin against the model already serving this conversation, and a never-downgrade guard. A low-confidence pick is discarded and a size-based heuristic decides instead -- the request is still served, but it goes wherever prompt length happens to point, regardless of task shape or GPU load. So hedging does not buy a safer route; it buys no route from you at all. Commit to a pick unless the candidates are genuinely interchangeable.

## Candidates
{{model_list}}

Choose only from this set. Never name a model that is not listed. The order above is not a ranking.

## The state you receive
- `request.user_task` -- the conversation's ORIGINAL ask: what this session is for. **When it is present, classify the work from THIS.**
- `request.user_message` -- the most recent user turn (absent when that turn carried no text, e.g. an image-only turn). In an agent loop this is often harness plumbing (a subagent-completion notice, a tool result) rather than a person asking something: read it as what is happening RIGHT NOW, not as what kind of work this is.
- `request.estimated_prompt_tokens`, `message_count`, `tool_count`, `max_tokens`, `stream` -- request shape.
- `request.has_image` / `request.image_count` -- if an image is present, a candidate whose `modalities` list is present and lacks `image` cannot serve it. Never pick one. When no candidate carries `modalities`, ignore this rule.
- `request.current_model` and `request.switch_cost` -- present only when a conversation already has an incumbent: the model currently serving it, and what leaving costs (a switch re-prefills the whole context). Only switch when the capability gain clearly beats that cost.
- `candidates[].cost`, `candidates[].gpu_util`, `candidates[].low_demand` -- per-model price and live load.

## How to choose
1. Image request? First eliminate any candidate whose `modalities` list is present and lacks `image`.
2. Classify the turn from the user's own words first. The words outrank the shape: a long message can be a simple question, a short one can be a hard bug.
3. Apply the roles:
   - **Deepseek-V4.1-Flash-1017** -- the default for agent work. Coding, refactors, multi-file changes, debugging, multi-step plans, tool-driven loops, long context, anything expensive to get wrong. Strong coding default.
   - **GLM-5.3-Flash** -- pick when the deliverable IS judgement: reviewing a diff, verifying a claim, fact-checking, careful summarisation, second opinions, contract and consistency checks.
   - **Qwen3.8-27B** -- pick only for simpler tasks: a greeting, lookups, reformat, classification. Okay for smaller coding projects too. 256k token limit.
4. Break remaining ties by `gpu_util` (do not pile onto a hot model for work a cooler one handles equally well), then by `cost`.

## Current GPU load
{{gpu_load}}"""


@dataclass
class DecisionResult:
    """One decision-model answer, normalised for the router.

    ok is the ONLY field the router needs to branch on: False means "no usable
    answer, use the heuristics" and every failure mode lands there (transport
    error, timeout, malformed payload, a choice outside the offered candidates,
    or confidence below the operator floor). reason names which, so the
    deferral is legible in the ledger instead of silent.
    """

    ok: bool = False
    choice: str = ""
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    reason: str = "not_attempted"
    latency_ms: int = 0


# Above this many context tokens a model switch is a materially expensive
# re-prefill rather than a rounding error. Roughly the point where re-reading
# the context costs more than the decision itself by an order of magnitude.
_SWITCH_COST_HIGH_TOKENS = 8000


def _switch_cost_label(context_tokens: int) -> str:
    """Human-readable cost of leaving the incumbent, for the decision state.

    PURE. Deliberately qualitative ("high"/"moderate"/"low") rather than a
    FLOP count: the model weighs this against a capability gain, and a precise
    number it cannot compare to anything adds tokens without adding judgment.
    """
    if context_tokens >= _SWITCH_COST_HIGH_TOKENS:
        return f"high: switching re-prefills ~{context_tokens} tokens"
    if context_tokens >= _SWITCH_COST_HIGH_TOKENS // 4:
        return f"moderate: switching re-prefills ~{context_tokens} tokens"
    return "low: short context, cheap to re-prefill"


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "no data"


def _content_parts(content: Any) -> tuple[str, list[str]]:
    """Split one message's content into (text, image_data_urls). PURE.

    Handles both shapes an OpenAI-compatible body can carry:
      * a plain string -- text only;
      * a list of parts -- {"type":"text","text":...} and
        {"type":"image_url","image_url":{"url":...}} (the OpenAI vision shape).

    Anything unrecognised contributes nothing rather than raising: a malformed
    content part must cost a routing signal, never a request.
    """
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    texts: list[str] = []
    images: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif ptype == "image_url":
            iu = part.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if isinstance(url, str) and url:
                images.append(url)
    return " ".join(texts), images


def extract_user_turn(body: dict[str, Any]) -> tuple[str, list[str]]:
    """The LAST user message's (text, images). PURE.

    Last, not first: this is "what just happened". In an agent loop that is
    often harness plumbing rather than the task -- measured live, a 3,101-char
    subagent-completion notice -- which is why build_state pairs it with
    extract_user_task()'s opening ask. Falls back to the first user turn when
    no later one exists (a system-prompt-only or assistant-first body still has
    something to say about intent).
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return "", []
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            text, images = _content_parts(msg.get("content"))
            if text or images:
                return text, images
    return "", []


def extract_user_task(body: dict[str, Any]) -> str:
    """The FIRST user message's text: what the conversation is ABOUT. PURE.

    The last user turn is not the task in an agent harness. Measured on live
    traffic (2026-10-27): the last `role: "user"` message was a 3,101-char
    subagent-completion notice inside a 116k-token, 178-message conversation,
    so the router was classifying harness plumbing and correctly answering
    "no preference" on every turn. The opening user message is the one that
    says what the session is for; the last one says what just happened.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "user":
            text, _images = _content_parts(msg.get("content"))
            if text:
                return text
    return ""


def count_images(body: dict[str, Any]) -> int:
    """How many image parts the WHOLE body carries (all messages). PURE.

    Used for the complexity bump: unlike the routing hint (last turn only),
    complexity should reflect every image the model must encode.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return 0
    total = 0
    for msg in messages:
        if isinstance(msg, dict):
            total += len(_content_parts(msg.get("content"))[1])
    return total


def build_state(
    body: dict[str, Any],
    *,
    complexity: int,
    candidates: list[str],
    gpu: dict[str, Any] | None = None,
    costs: dict[str, Any] | None = None,
    modalities: dict[str, list[str]] | None = None,
    current_model: str | None = None,
) -> str:
    """A compact, JSON-encoded description of the request and the candidates.

    PURE (no I/O), so a test and the live path build byte-identical states.

    Carries the user's own words (the opening task AND the latest turn) -- and
    nothing else from the conversation.
    That single field is what makes the router work at all: tools, system
    prompt and conversation depth are CONSTANT for an agent harness (the same
    catalog ships every turn), so they carry no per-request signal, while the
    query is the only thing that varies. See MAX_USER_TEXT_CHARS for the
    measurement that forced this.

    The full conversation is still deliberately excluded: it would add latency
    and prompt-injection surface for no signal the last user turn lacks.

    modalities (optional) maps candidate -> ["text","image",...] so the model
    can avoid picking a text-only LLM for an image request. When omitted, no
    capability claims are made and the model decides on the other signals.

    current_model (optional) is the model this CONVERSATION is already served
    by. Switching costs a full re-prefill of the context on the new model --
    measured at ~300x the compute of one decision call for a 76K agent payload
    -- so the model must know what it would be switching FROM to weigh that.
    Without it the model has no concept of an incumbent and treats every turn
    as a cold start.
    """
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    user_text, _user_images = extract_user_turn(body)
    user_task = extract_user_task(body)
    total_images = count_images(body)
    request_block: dict[str, Any] = {
        "estimated_prompt_tokens": int(complexity),
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "max_tokens": body.get("max_tokens"),
        "stream": bool(body.get("stream")),
    }
    # The routing signal. Empty string is omitted rather than sent as "" so a
    # text-free body reads as "no user turn" instead of "an empty question".
    if user_text:
        request_block["user_message"] = user_text[:MAX_USER_TEXT_CHARS]
    # The conversation's ORIGINAL ask, sent only when it differs from the last
    # turn -- a single-shot request already carries it as user_message and must
    # not pay for it twice. This is the field that classifies an agent session:
    # user_message says what just happened, user_task says what we are doing.
    if user_task and user_task != user_text:
        request_block["user_task"] = user_task[:MAX_USER_TASK_CHARS]
    # Modality signals: has_image is the one that must gate VLM choice;
    # image_count separates "one screenshot" from "a 40-frame dump".
    request_block["has_image"] = total_images > 0
    if total_images:
        request_block["image_count"] = total_images
    # Incumbent + what leaving it would cost. Estimated_prompt_tokens IS the
    # context the new model would have to re-read, so the same number serves
    # both fields -- no extra accounting, and the two can never disagree.
    if current_model:
        request_block["current_model"] = current_model
        request_block["switch_cost"] = _switch_cost_label(int(complexity))
    state: dict[str, Any] = {
        "request": request_block,
        "candidates": [],
    }
    gpu_models = (gpu or {}).get("models") if isinstance(gpu, dict) else None
    if not isinstance(gpu_models, dict):
        gpu_models = {}
    for name in candidates:
        entry: dict[str, Any] = {"model": name}
        if isinstance(costs, dict) and name in costs:
            entry["cost"] = costs[name]
        # Capability claim, only when the caller actually knows it: a VLM
        # candidate is offered to image requests, a text-only one is visibly
        # unsuitable rather than silently chosen.
        if isinstance(modalities, dict):
            mods = modalities.get(name)
            if isinstance(mods, (list, tuple)) and mods:
                entry["modalities"] = [str(m) for m in mods]
        g = gpu_models.get(name)
        if isinstance(g, dict):
            entry["gpu_util"] = _pct(g.get("value_pct"))
            entry["low_demand"] = bool(g.get("active"))
        state["candidates"].append(entry)

    text = json.dumps(state, sort_keys=True, default=str)
    if len(text) > MAX_STATE_CHARS:
        # Shed signal in the order that costs the LEAST routing quality:
        # costs first (a tie-breaker), then the candidate list. The user's
        # text is never shed -- without it the whole call is a constant.
        if any("cost" in c for c in state["candidates"]):
            for c in state["candidates"]:
                c.pop("cost", None)
            text = json.dumps(state, sort_keys=True, default=str)
        if len(text) > MAX_STATE_CHARS:
            # Truncate the CANDIDATE list rather than the JSON mid-object: a
            # decision model given invalid JSON would abstain every time.
            keep = max(2, len(state["candidates"]) // 2)
            state["candidates"] = state["candidates"][:keep]
            text = json.dumps(state, sort_keys=True, default=str)
        # Last resorts, in order. NEVER slice the serialized JSON: a raw
        # character cut emits invalid JSON and the decision model abstains on
        # every such request, forever (dual review 2026-10-26 -- the previous
        # `[:MAX_STATE_CHARS]` did exactly the thing the comment above forbids).
        # Shrink FIELDS and re-serialize until it fits; if it still does not,
        # return the smallest VALID document rather than a truncated one.
        if len(text) > MAX_STATE_CHARS and "user_message" in state["request"]:
            overflow = len(text) - MAX_STATE_CHARS
            msg = state["request"]["user_message"]
            keep_chars = max(200, len(msg) - overflow)
            state["request"]["user_message"] = msg[:keep_chars]
            text = json.dumps(state, sort_keys=True, default=str)
        while len(text) > MAX_STATE_CHARS and len(state["candidates"]) > 1:
            # Drop the LAST candidate (cheapest-to-lose end of the window) and
            # re-serialize; the JSON stays valid at every step.
            state["candidates"].pop()
            text = json.dumps(state, sort_keys=True, default=str)
        if len(text) > MAX_STATE_CHARS:
            # Single candidate still too big: strip the optional decorations
            # rather than the structure. A minimal valid document beats an
            # informative invalid one.
            for cand in state["candidates"]:
                cand.pop("modalities", None)
                cand.pop("cost", None)
            state["request"].pop("user_message", None)
            text = json.dumps(state, sort_keys=True, default=str)
    return text


def parse_choice(payload: Any, candidates: list[str], *, min_confidence: float) -> DecisionResult:
    """Normalise one System One response into a DecisionResult. PURE.

    Total by construction: every malformed shape returns ok=False with a reason
    rather than raising, because this sits on the request path and
    model_routing's contract is that routing NEVER fails a request.
    """
    if not isinstance(payload, dict):
        return DecisionResult(reason="bad_payload")
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        return DecisionResult(reason="no_answers")
    answer = answers.get("answer")
    if not isinstance(answer, dict):
        return DecisionResult(reason="no_answer")
    choice = answer.get("choice")
    if not isinstance(choice, str) or not choice:
        return DecisionResult(reason="no_choice")
    # A choice outside the offered set is a hallucinated option: refuse it
    # rather than serve a model the ladder never offered.
    if candidates and choice not in candidates:
        return DecisionResult(choice=choice, reason="choice_not_offered")

    probs_raw = answer.get("probabilities")
    probabilities: dict[str, float] = {}
    if isinstance(probs_raw, dict):
        for k, v in probs_raw.items():
            try:
                probabilities[str(k)] = float(v)
            except (TypeError, ValueError):
                continue

    try:
        confidence = float(answer.get("confidence", probabilities.get(choice, 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence < min_confidence:
        return DecisionResult(
            choice=choice,
            confidence=confidence,
            probabilities=probabilities,
            reason="below_confidence",
        )
    return DecisionResult(
        ok=True,
        choice=choice,
        confidence=confidence,
        probabilities=probabilities,
        reason="decision",
    )


DecideCaller = Callable[[str, str, list[str], int, list[str]], Awaitable[Any]]
"""state, instructions, candidates, timeout_ms, images -> raw System One payload.

images is a list of image data URLs (possibly empty) forwarded to a
vision-capable decision model so it can SEE the request, not just count it.
A caller that cannot carry images must accept and ignore the argument.


Injected by the caller (routes_proxy) so this module never imports the HTTP
client, the registry, or the credential resolution -- the same seam discipline
core.distiller uses for llm_once.
"""


async def decide(
    caller: DecideCaller,
    *,
    state: str,
    instructions: str,
    candidates: list[str],
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    images: list[str] | None = None,
) -> DecisionResult:
    """Ask the decision model which candidate should serve. NEVER raises.

    Any failure -- no caller, transport error, timeout, garbage payload -- comes
    back as ok=False so the router serves the heuristics. That is the whole
    contract: an optional feature must not be able to fail a request, and a
    decision model that is down must cost a downgrade opportunity, never an
    answer.
    """
    if not candidates:
        return DecisionResult(reason="no_candidates")
    bounded = max(MIN_TIMEOUT_MS, min(int(timeout_ms or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS))
    t0 = time.monotonic()
    # Resolve ARITY ONCE, up front, instead of catching TypeError.
    # Catching TypeError could not tell "this caller predates the images
    # parameter" from "this caller raised TypeError internally AFTER its
    # POST" -- and the second case re-ran the whole call, hitting the
    # decision endpoint twice and doubling the worst-case latency on the
    # failure path (dual review 2026-10-26). Inspecting the signature is
    # cheap, happens before any I/O, and cannot double-call.
    _takes_images = True
    try:
        import inspect

        _params = inspect.signature(caller).parameters
        _takes_images = len(_params) >= 5 or any(
            p.kind is inspect.Parameter.VAR_POSITIONAL for p in _params.values()
        )
    except (TypeError, ValueError):
        # Unintrospectable callable (C function / partial): assume modern.
        _takes_images = True
    try:
        if _takes_images:
            payload = await caller(state, instructions, list(candidates), bounded, list(images or []))
        else:
            # A caller written against the pre-images 4-arg signature. Dropping
            # the image hint must cost the hint, never the decision.
            payload = await caller(state, instructions, list(candidates), bounded)
    except TimeoutError:
        return DecisionResult(reason="timeout", latency_ms=int((time.monotonic() - t0) * 1000))
    except Exception as exc:
        log.warning("decision routing: call failed (%s) - heuristics stand", type(exc).__name__)
        return DecisionResult(reason="call_failed", latency_ms=int((time.monotonic() - t0) * 1000))
    result = parse_choice(payload, candidates, min_confidence=min_confidence)
    result.latency_ms = int((time.monotonic() - t0) * 1000)
    if not result.ok:
        log.info(
            "decision routing: %s (choice=%r confidence=%.3f) - heuristics stand",
            result.reason,
            result.choice,
            result.confidence,
        )
    return result
