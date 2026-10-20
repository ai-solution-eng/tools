---
name: model-roster-check
description: Use when an ensemble cell is upgraded, replaced, or re-postured, when a new model pair is being adopted for cross-validation, or when the cross-validate direction table looks stale (reviews agreeing too easily, a model's failures no longer match its recorded posture). Keeps a direction-matched ensemble honest and anti-correlated. NOT for routine reviews — use cross-validate for those.
---

# Model roster check (keep the ensemble anti-correlated)

## Source of truth

- The ensemble **method** lives in a method doc (reference: `MODEL_CROSS_VALIDATION.md`). **Update ONLY its
  "current instantiation" section when the roster changes** — the rest is model-agnostic.
- A runnable implementation may accompany it (reference: `ensemble_flow.py`, with `flow` / `review` /
  `escalate` subcommands and a `--dry` mode that prints the calls without sending).
- Re-sync the `cross-validate` skill's direction table whenever the instantiation section changes — it
  duplicates those conclusions.

## Diagnose from per-row data, never the composite

Read **per-benchmark rows**, not a composite index — the composite hides the posture you need.

| Row | What it tells you |
|---|---|
| Hallucination / non-hallucination rate | **Calibration posture.** Low non-hallucination % = confident asserter; high = abstainer. |
| Agentic tool rows (terminal/SaaS/workflow) vs knowledge rows (documents, recall, no-tools) | **Where training concentrated.** Big spread = environment-trained scalpel; flat rows = generalist. |

**Anti-correlation test (the adoption gate):** their *failure* rows must be each other's *strong* rows. If
both fail the same rows they are one model twice — **skip the ensemble and use the machine layer harder**
(more dry-runs, logs, tests). Same-lineage or same-posture pairs agree falsely; agreement between them is
not evidence.

## Adopting a new pair

1. Read each candidate's per-row data; classify posture and training concentration.
2. Run the anti-correlation test. Fail → do not adopt that pair for that domain.
3. Derive the direction table: who reviews whom, and for what failure mode.
4. Record the date, the source rows, and the effort level the posture was measured at.
5. Update the `cross-validate` skill's direction table to match.

## After a cell changes

1. Re-derive the direction table from fresh per-row data.
2. Re-run the anti-correlation test. Fail → retire the ensemble for that domain; do not "try it anyway".
3. Rewrite the instantiation section **only**; note the date and the source rows.
4. Re-check the effort dials — a version bump can move the posture, and posture is recorded **at a given
   effort**, not intrinsically.

## Effort-dial surface

- Models with a numeric effort knob change posture with the dial. Record which effort reviews run at; a
  review verdict at high effort is not comparable to one at low.
- Reviews run low–mid — overthinking a diff review is pure cost.

## Known failure mode — empty ≠ refusal

A model with thinking enabled and a **small `max_tokens`** budget can return **empty content** with the
budget spent on the reasoning trace. Empty content = retry with more budget or lower effort, **not** a
refusal and not "the model had nothing to say".

## Reference instantiation (2026-10-03)

One deployment's pair — re-derive locally:

| | DS V4.1-Flash | GLM-5.3-Flash |
|---|---|---|
| Posture | Confident asserter (non-hallucination 4%, dead last; knowledge accuracy 46% #2-open) | Calibrated abstainer (non-hallucination 72%; shallower agentic execution) |
| Home turf | Terminal/SaaS/code agents (AutomationBench 69% #3 overall, best open) | kube RCA (ITBench 51% vs DS 47%); chat-shaped knowledge |
| Role | Primary for coding/terminal/agentic | Primary for k8s RCA + reviewer of DS's facts |

- Method doc: `/home/andrew/Code/HPE/ModelDeploymentApproaches/MODEL_CROSS_VALIDATION.md`; implementation
  `ensemble_flow.py` (25 for reviews, 50 for DS primaries).
- Providers: `pcai-model-endpoint-flow` — enumerate with `list_subagent_models`.
- Auth/TLS: endpoints in `ModelBenchmarker/src/model_benchmarker/utils/pcai_models.py`;
  `REMOTE_CA_BUNDLE=$DSH_HOME/hpe-private-ssl-ca.pem` (without it the CLI disables verification and warns
  on stderr).

## Known duplication hazard

A skill can exist as **two copies** (e.g. a loaded `~/.dsh/skills/<name>/SKILL.md` and a repo copy under a
project's skills directory) with **no installer or sync script**. One will drift — and in this deployment it
did: the repo copy of `cross-validate` went stale against the loaded copy, and the repo copy is on a
**read-only mount**, so it could not be re-synced from a session. **Pin one canonical source** (the loaded
`~/.dsh/skills/` copy is what agents actually read) and make the other a pointer or symlink; until then,
diff them before trusting either.
