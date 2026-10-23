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

DECISION_MODES = ("off", "shadow", "active")

DEFAULT_INSTRUCTIONS = (
    "You are the model router for an LLM gateway. Given the request shape and "
    "the candidate models, choose the single model that should serve this "
    "request. Prefer the cheapest model that can handle the complexity, and "
    "consider current GPU load: a model already under heavy load should not be "
    "chosen for a request a less-loaded model can serve."
)


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


def _pct(value: Any) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "no data"


def build_state(
    body: dict[str, Any],
    *,
    complexity: int,
    candidates: list[str],
    gpu: dict[str, Any] | None = None,
    costs: dict[str, Any] | None = None,
) -> str:
    """A compact, JSON-encoded description of the request and the candidates.

    PURE (no I/O), so a test and the live path build byte-identical states.

    Deliberately NOT the conversation. A decision model reads the state once
    and answers a probability per option; the signals that actually separate
    "cheap model fits" from "needs the ceiling" are structural -- how big the
    prompt is, how many tools are declared, how deep the conversation runs, and
    how loaded each candidate is. The full prompt adds latency and prompt-
    injection surface without adding a signal the complexity score lacks.

    Every field is derived from data the gateway already computed for the
    request, so this cannot disagree with the admission/budget math.
    """
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    state: dict[str, Any] = {
        "request": {
            "estimated_prompt_tokens": int(complexity),
            "message_count": len(messages) if isinstance(messages, list) else 0,
            "tool_count": len(tools) if isinstance(tools, list) else 0,
            "max_tokens": body.get("max_tokens"),
            "stream": bool(body.get("stream")),
        },
        "candidates": [],
    }
    gpu_models = (gpu or {}).get("models") if isinstance(gpu, dict) else None
    if not isinstance(gpu_models, dict):
        gpu_models = {}
    for name in candidates:
        entry: dict[str, Any] = {"model": name}
        if isinstance(costs, dict) and name in costs:
            entry["cost"] = costs[name]
        g = gpu_models.get(name)
        if isinstance(g, dict):
            entry["gpu_util"] = _pct(g.get("value_pct"))
            entry["low_demand"] = bool(g.get("active"))
        state["candidates"].append(entry)

    text = json.dumps(state, sort_keys=True, default=str)
    if len(text) > MAX_STATE_CHARS:
        # Truncate the CANDIDATE list rather than the JSON mid-object: a
        # decision model given invalid JSON would abstain every time.
        keep = max(2, len(state["candidates"]) // 2)
        state["candidates"] = state["candidates"][:keep]
        text = json.dumps(state, sort_keys=True, default=str)[:MAX_STATE_CHARS]
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


DecideCaller = Callable[[str, str, list[str], int], Awaitable[Any]]
"""state, instructions, candidates, timeout_ms -> the raw System One payload.

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
    try:
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
