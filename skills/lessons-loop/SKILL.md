---
name: lessons-loop
description: 'Use when building an agent that should learn from its own resolutions — a self-improvement / lesson-distillation loop, a "remember what worked" instruction store, or a domain-adaptive prompt layer. Domain-agnostic: applies to SQL, API calls, shell work, or any tool surface. NOT for session/memory curation (that is memory-curation), and not a substitute for a held-out eval set.'
---

# Lessons loop (self-improving agent, two-dataset guardrail)

## The loop

    resolve ──► distill ──► store(candidate) ──► promote ──► recall(next question)

- **resolve** — the agent answers, running its tool surface unchanged.
- **distill** (outlet, after the turn) — a second LLM decides whether the exchange established a durable
  lesson. Positive evidence only.
- **store** — candidates land in a write-only staging dataset.
- **promote** (separate gated pass) — re-check the evidence shape, skip near-dups, then write curated.
- **recall** (inlet, before the turn) — top-k curated lessons injected as context.

Nothing in the loop is domain-aware. Only the seed corpus and the eval set are domain-specific — see
**Adding a domain**.

## The two-dataset guardrail (the core safety idea)

The risk in every self-improvement loop is the system learning to trust its own output. The guardrail is
**structural, not behavioral**:

| Dataset | Writes | Reads |
|---|---|---|
| `<domain>-lessons` (curated) | promotion gate only | agent recall |
| `<domain>-lessons-candidates` | distill step only | promotion gate |

The agent **never reads candidates**; the distill step **never writes curated**. A bad lesson can therefore
only enter the prompt by passing the promotion gate — and can be demoted by the same gate. Enforce it at
code review: any read of the candidates dataset outside the promotion script, or any write to curated
outside the gate, breaks the property (a candidates dataset in the recall path destroys it entirely).

## Lesson schema (one doc per lesson, flat payload)

    { "text": "<imperative instruction>",
      "kind": "<kind>",
      "trigger": "<class of question this applies to>",
      "tags": [...],
      "status": "candidate | curated | stale",
      "source": "<your producer label>" }

`kind` semantics (adapt the names to your domain): **schema-map** (intent term → table/column/endpoint),
**perf-guard** (bounded IN-list / window / LIMIT / no free-text scans), **resolution-pattern** (how to
resolve a class of question), **fail-fix** (concrete fix for a concrete error).

Write `text` standalone and imperative — a future session with zero context must understand and follow it.

## Distillation gate (poison prevention)

- **Positive evidence only**: the action ran AND the result was sane / human-accepted, OR there is a
  concrete fail→fix. Trivial exchanges, no-tool exchanges, and transient errors emit **NOTHING**. Never
  auto-store an unvalidated resolution.
- The distill prompt must say exactly `NOTHING` for the no-lesson case; treat an unparseable reply as zero
  lessons, not as an error to retry.
- Skip short replies (a minimum-reply-length valve) so trivial exchanges never reach the LLM.
- **Near-dup suppression at ingest is not full coverage.** An embedding-dedup threshold around
  `0.995` misses near-identical text that lands *just under* it: a real store reached 1,044 docs of which
  ~850 were machine-written chunks, the biggest sessions each holding 62–164 near-identical copies of the
  same block. Audit counts by source/kind rather than trusting the threshold.
- Emit 0–3 lessons per exchange at low temperature; the cap stops one resolution becoming ten lessons.

## Promotion gate

The promotion script is the ONLY writer of curated. Per candidate, in order:
1. evidence-shape check → reject;
2. near-dup vs curated (a similarity threshold around `0.90`) → skip + delete;
3. review decision → human y/N, LLM PROMOTE/REJECT, or `--auto` (evidence + near-dup only).

Make the LLM review **fail-closed**: the prompt should say REJECT when uncertain, and a failed review call
must return REJECT. With no review mode passed, treat the candidate as SKIP — nothing promotes by accident.
Run a dry-run first. A scheduled job can run the gate unattended (set `concurrencyPolicy: Forbid` so runs
never overlap).

## Decay — designed, NOT yet closed (state this honestly)

- Curated lessons carry `hits` / `successes` counters; the demote pass acts on a lesson with enough samples
  and a success rate below the floor (schema drift, or a lesson that stopped helping). It never touches an
  untested lesson.
- **Open item:** the recall path is read-only, so nothing increments those counters yet; closing the decay
  loop needs a small document-PATCH endpoint on the store API. Until then the loop accumulates behind the
  gate with **no decay signal** — say exactly that in a status report instead of implying decay is live.
- The schema declares `status: "stale"`, but the demote pass **deletes** the curated doc rather than
  flipping the status — so a demotion is destructive and unreviewable after the fact. **Snapshot the curated
  set before running it for real.**

## Ship the eval BEFORE trusting the loop

- Write a replay harness over a held-out question set: run the agent on each question and score PASS/FAIL,
  by LLM judge or manually.
- Run the baseline BEFORE seeding/promoting and again after each promotion batch; compare PASS rate.
  Without the eval there is no evidence the loop helps — do not tune lessons off vibes.

## Adding a domain

An adapter is just a directory of seed lessons in the same schema with `status: "curated"`, loaded by the
seed script with a domain flag. The mechanism is generic; supply the seed corpus and the eval set.

## Where the seed corpus comes from

A domain that already has a **hand-written rule list** (a system prompt, a runbook, a style guide) is the
output of a manual self-improvement loop: a human watched resolutions and wrote the instructions down.
Automating the "watch resolution, write an instruction" step is what closes the loop — and the questions
those existing rules cover are your eval set.

## Gotchas

- Keep the data surface **stateless + read-only**; the memory lives agent-side, never in the governed data
  server.
- **Watch for kinds the gate accepts but no prompt emits.** If the shape check accepts a `kind` the distill
  prompt never produces (reference case: a fifth `data-shape` kind), either use it deliberately across
  schema + prompt + gate, or reject it — dead config otherwise.
- Recall and distillation are usually separate feature valves (one for the inlet read, one for the outlet
  write) so either half can be disabled independently. Lessons are injected as an extra system message — the
  deployed system prompt is NOT modified.
- Deploying the loop is a release: verify the running version with `release-verify`. Model-pair assumptions
  behind the distill/review LLM are checked with `model-roster-check`.

## Reference instantiation

One implementation (SQL domain) — re-derive locally:

- Home: `MultimodalRAG/openwebui_extension/sql_lessons/` (`DESIGN.md`, `promote.py`,
  `seed_sql_lessons.py`, `sql_lessons.py`, `eval/replay.py`, `deploy/cronjob.yaml`); runs in the OWUI
  extension filter, reusing its memory spine.
- Datasets: `sql-lessons` (curated) / `sql-lessons-candidates`; valves `SQL_LESSONS_ENABLED` (recall) and
  `SQL_LESSONS_DISTILL_ENABLED` (write); `SQL_LESSONS_DISTILL_MIN_REPLY_CHARS` default 200.
- Gate flags: `--dry-run`, `--dup-threshold` (default 0.90), `--review`, `--llm-url/--llm-model`, `--auto`,
  `--demote-stale` (`--min-hits` 3, `--min-success-rate` 0.5). CronJob weekly 03:00,
  `concurrencyPolicy: Forbid`.
- Ingest dedup inherited from the store: `RAG_DEDUP_THRESHOLD` default `0.995`.
- The hand-written rule list that seeded it: the Toromont `agent-system-prompt.md` (resolve serials through
  governed lookup keys — never `ILIKE` on raw serials, bounded key predicates, trailing-12-month windows,
  LIMIT; canonical serial via `equipment_machine` per `toromont_datasets.md`).
