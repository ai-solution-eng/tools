# Agent-behaviour judge — design

**Status:** proposal, 2026-11-23. Not implemented. Supersedes nothing.
**Related:** `core/agent_eval.py` (the deterministic half, shipped), `core/trace_payloads.py` (the evidence it reads),
`scripts/check_agent_eval.py` (release Gate 4/4), `scripts/agent_eval_report.py` (the reader).

## 1. What already exists, and what it cannot answer

`agent_eval` scores what a trace PROVES without a model: a tool call that errored, a call the grant gate refused,
a repeated identical call, a turn that blew its step budget. That is the cheap half and it is live on G2.

It cannot answer the question an operator actually asks: **was the answer right, and was the path to it defensible?**
"All four tool calls succeeded" is compatible with confidently answering the wrong question. Closing that gap needs a
judge — a model reading what the agent saw and did.

## 2. The design constraint that decides everything

**A judge that is wrong is worse than no judge.** A deterministic scorer that misses a class of failure produces a gap
you can see in the numbers. A judge that hallucinates a verdict produces a NUMBER THAT LOOKS LIKE EVIDENCE, and if a
release gate reads it, it launders a bad change into a green build. Every choice below follows from that.

Corollary: **the judge never gates a release until it has an agreement score against human labels.** Phase order in §6.

## 3. Shape: a tool-using judge, not a transcript prompt

2026 evidence (scout, `.scratch/fleet-standing-2026-11-23/scout-eval-memory.md` E1): a judge that PULLS its own
evidence scores ~0.85 evidence-F1 vs ~0.70 for one-shot transcript reading on long-horizon runs, at ~42% lower cost —
because it stops feeding the whole trace into one window and instead fetches what it needs.

So the judge is an agent over our own tables, with three read-only tools:

| Tool | Reads | Why |
|---|---|---|
| `get_turn(request_id)` | `agent_traces` rows for the turn | the step skeleton (server/tool/ms/status) |
| `get_payload(request_id, iteration)` | `agent_trace_payloads` | what the tool was actually asked and what it returned |
| `get_verdicts(request_id)` | `agent_eval_verdicts` | the deterministic findings, so the judge does not re-derive them |

It returns a fixed schema — `{dimension, severity, claim, evidence_refs[], confidence}` — where **every claim must cite
at least one `request_id`+iteration**. An uncited claim is dropped before storage, not stored at low confidence.

Four dimensions, scored independently (2026 consensus, and they fail differently):
**task completion · trajectory quality · tool use · multi-turn coherence.**

## 4. The injection problem nobody mentions

**The judge reads untrusted tool results.** A tool result is attacker-influenced text; a judge that reads it as
instructions can be steered to produce a clean verdict — by the very agent behaviour it is supposed to catch. This is
the same channel `core/tool_injection.py` screens for the model, and the judge is a model.

Therefore:
1. Payloads handed to the judge pass through `tool_injection.scan` first; findings become part of the judge's context
   as DATA ("this result contains 3 instruction-override shapes"), never stripped silently.
2. The judge's system prompt states that tool output is untrusted reference data. Same rule the playbook block uses.
3. The judge is **read-only by construction**: its tools are SELECTs. It cannot act on a trace, only describe it.
4. A judge run whose payload tripped HIGH severity is flagged `contaminated` and excluded from calibration — otherwise
   the attacker's text teaches the judge what "clean" looks like.

## 5. Where it runs, and what it costs

**Never on the request path.** A judge call is a model call; putting it in the relay would double the cost of every
turn to answer a question nobody asked at request time.

- Runs as a **sweep** (a script, like the housekeeping pattern): score turns older than N minutes, newest first,
  bounded by a per-run budget and a daily cap.
- Budget is a first-class knob: `judge_daily_usd`, `judge_max_turns_per_run`, `judge_min_confidence`.
- Model: the cheapest self-hosted model that clears the agreement floor in §6 — NOT the biggest. The judge's job is
  verification against evidence, which rewards instruction-following over raw capability.
- Sampled: judge a sample of turns, not all. A 10% sample over a week beats 100% for two days followed by silence
  when the budget runs out.

## 6. Rollout — three phases, each a gate on the next

| Phase | Writes verdicts | Gates a release | Exit criterion |
|---|---|---|---|
| 1. **Shadow** | yes (`judge_verdicts`, kind='shadow') | no | runs a week without error; operator reads the report |
| 2. **Calibrated** | yes | no | ≥40 hand-labelled turns; **agreement ≥0.80** per dimension, and the disagreement set reviewed once |
| 3. **Gating** | yes | yes, on the dimensions that cleared 0.80 — and always alongside the deterministic gate, never instead of it | a judge regression is a release blocker |

**Abstention is a first-class outcome.** The judge may answer `insufficient_evidence`; that is a better answer than a
guess, and the rate of it is itself a metric. A judge that never abstains is not calibrated, it is confident.

## 7. Storage

`judge_verdicts` (separate from `agent_eval_verdicts` on purpose — different provenance, different trust): request_id,
dimension, severity, claim, evidence_refs (JSONB), confidence, model, prompt_version, judged_at, contaminated BOOL.
Retention follows `agent_trace_payloads`: the verdict is only as re-readable as the evidence under it.

## 8. Explicit non-goals

- **Not** a training loop. Traces→training stays blocked behind a payload set that covers refusals (A4) and a judge
  that is calibrated; RL on uncalibrated verdicts trains the wrong behaviour.
- **Not** a per-turn scorer. Latency and cost are why this is a sweep.
- **Not** a replacement for Gate 4/4. The deterministic gate is cheap, explainable, and already catches four failure
  classes; the judge adds judgement, it does not remove the floor.
- **Not** multi-judge voting in v1. One calibrated judge with an agreement score beats three uncalibrated ones
  agreeing with each other — same-lineage agreement is not evidence.

## 9. First three commits

1. `judge_verdicts` DDL + `core/judge.py` with the schema validation and the citation requirement (no model call yet;
   a fake judge_fn, like the RAG faithfulness scorer's injectable judge).
2. `scripts/judge_sweep.py` — the sweep, the budget knobs, `--dry` printing the calls it would make.
3. The calibration harness: 40-turn labelled fixture + agreement report per dimension. Phase 2 cannot start without it.
