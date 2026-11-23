# Routing descriptions — the decision model's per-model cards

> **What this file is.** The `model_cards` text for every routable model, written
> for the decision model that picks the serving model
> (`core/routing_config.py` → `render_criteria`). These blocks are not
> marketing copy: each one is the per-option description bound to that option's
> label in the CRITERIA channel, and it is the only channel whose text the
> scoring model reads next to the option it describes
> ([API.md §4.6](API.md)). Paste a block into the admin console's routing card,
> or send the JSON at the end of this file to `PUT /admin/routing/config`.
>
> **Rules the text must obey** (from the shipped router prompt,
> `core/decision_routing.py`, `DEFAULT_INSTRUCTIONS`):
>
> 1. **Claim a role.** An option that says nothing about what it is FOR carries
>    no role claim — the model treats it as unknown and never picks it.
> 2. **State both directions.** Rule 3 tells the model to prefer the *weakest*
>    option that genuinely fits, so a card needs what the model is for *and*
>    what it must not take. A category left unclaimed is only weakly matched;
>    a stated "not for" is decisive.
> 3. **Never name a sibling.** The candidate set is per-request (profile ladder,
>    ACL, health, context fit), so a card that names another model is wrong on
>    every site whose ladder differs.
> 4. **Do not restate load, cost or modalities.** `render_criteria` appends
>    those machine facts automatically — and skips them only when the card uses
>    the token itself (`{{gpu_util}}`, `{{cost}}`, `{{modalities}}`, …), so
>    using a token just suppresses the standard phrasing.
> 5. **Keep it short.** Every character rides in *every* decision call. Hard cap
>    is 4000 chars per card (`_CARD_DESCRIPTION_MAX`, `api/routes_admin.py`).

## Roster and division of labour

| Model | Role | Takes | Yields |
|---|---|---|---|
| `Deepseek-V4.1-Flash-1017` | **The builder** — agentic execution | implementation, refactors, debugging, tool loops | review, judgment |
| `Inkling-2-Small` | **The reviewer** — accuracy and knowledge | review, audit, hard reasoning over documents | building, agentic loops |
| `Qwen3.8-27B` | **Light work** | short tasks, cheap long inputs | anything expensive to get wrong |
| `GLM-5.3-Flash` | **Retired** — removed from the ladder and the model list | — | — |

All three deployed models accept images, so **no card mentions modality**: the fact is appended
automatically for every option, so it discriminates nothing. Spend the words on
role instead.

## Deepseek-V4.1-Flash-1017

```text
The builder: the default for agent work — coding, refactors, multi-file changes,
debugging, multi-step plans and tool-driven loops that have to run to completion.
The strongest option at executing in a terminal and at agentic software work.
Do not use it for review, audit or hard judgment over documents — a more accurate
model is worth it — and not for light chat or single lookups.
```

**Why.** It owns the agentic-execution column outright: AutomationBench-AA 67–68%
vs 46% for Inkling, Terminal-Bench 4.0 29% (reasoning build) and higher for the
non-reasoning build against Inkling's 13%, Terminal-Bench-Science 10–13% vs 6%,
GDPval-AA v2.1 Elo 1746 (top). It also wins Strategy & Ops (49/48 vs 43).

**What changed.** The old card ended with *"anything expensive to get wrong"* —
that is precisely the review/judgment slot Inkling is now claiming, and two
options claiming one role is the one thing the criteria channel cannot
disambiguate. The phrase moved to Inkling's card, stated as an accuracy claim.
"Long context" was dropped because it no longer discriminates: Deepseek and
Inkling both advertise 1M (Inkling's window was verified on 2026-11-23 from its
pool's `/v1/models` card, `max_model_len: 1048576`), and Qwen's 256k is the one
window worth stating.

## Inkling-2-Small

```text
High-accuracy reviewer: reviewing code, diffs and designs for correctness,
security and risk; checking work another model produced; hard reasoning over long
documents, spreadsheets, transcripts and reports; finance, legal, medical and
scientific analysis. The accuracy pick — reach for it when being wrong is
expensive and speed is not the constraint.
Not for building: no writing, refactoring, debugging or multi-file repo work, and
no agentic terminal loops that must run to completion. Review here, implement
elsewhere.
```

**Why "high-accuracy reviewer" is defensible.** It is the most accurate model in
this roster on knowledge-heavy judgment, and — the part that matters for review —
it is the second-least likely to invent a finding:

| Benchmark | Inkling | Best rival |
|---|---|---|
| HLE-Diamond (reasoning & knowledge) | **29%** | 25% |
| GDP.pdf (professional document reasoning) | **25%** | 17% |
| AA-Briefcase v1.1 | **1527** (top; rival value clipped) | 1449 |
| Legal / Finance / Engineering / Economics indexes | **42 / 44 / 39 / 43** (all top) | 38 / 42 / 38 / 38 |
| AA-AnalystAgent (spreadsheets & documents) | **51%** | 25% |
| EnterpriseOps-Gym-AA | **47%** | 33% |
| CritPt v2 hard subset | **top** (value clipped) | 28% |
| AA-Omniscience non-hallucination | **73%** (2nd) | 75% |
| FrontierCode-AA 2.0 (agentic SWE) | **25%** (2nd) | 28% |

FrontierCode is the closest thing to a code benchmark where it is strong — 25%,
ahead of the mid-tier models, just behind the top reasoning build. Combined with
73% non-hallucination, "review code for correctness and risk" is the right
reading.

**Context.** 1M, verified 2026-11-23 from the pool's own `/v1/models` card
(`thinkingmachines/inkling-2-small`, `max_model_len: 1048576`) — which retires
the UNVERIFIED flag in the roster research. It is not a discriminator against
Deepseek (also 1M), so the card does not claim it; the router already drops
candidates whose prompt cannot fit.

**Caveat, stated honestly.** Artificial Analysis has no code-*review* benchmark;
FrontierCode measures agentic SWE (fixing repos), not reviewing them. The reviewer
role is an inference from reasoning scores + non-hallucination + document
analysis, not a measured result. It is a good inference, not evidence.

**What changed.** The previous card was a document/prose specialist that
explicitly excluded code and claimed to be text-only. Both are now wrong: code
review is in scope, and the model accepts images. The exclusion flipped from
"not for code" to "not for *building* code", which is the distinction that
matters — its agentic-execution numbers are the weakest in the roster
(AutomationBench 46%, last; Terminal-Bench 4.0 13%), so it must never be handed a
loop that has to run to completion.

## Qwen3.8-27B

```text
Choose for light work: greetings, lookups, reformatting, classification,
extraction, one-line fixes, and short questions with obvious answers. 256k
context, so it also takes very long inputs cheaply.
Not for multi-file changes, debugging, review or anything expensive to get wrong.
```

**Why.** Mostly unchanged. "Small single-file edits / fine for smaller coding
projects" was dropped: rule 3 prefers the weakest option that fits, so a card
that claims coding is how real coding ends up on the cheapest model. The 256k
context claim is kept because it is a genuine capability fact the router can use
when a long input fits.

## GLM-5.3-Flash (retired — removed from the roster, kept for the record)

```text
Fast mid-tier generalist for agentic business and workflow tasks: SaaS-style
multi-step operations and terminal work, at low cost. Weak on hard reasoning,
long-document analysis and factual accuracy — send those elsewhere.
```

**Why it read that way.** The numbers support exactly that positioning: solid
agentic throughput (AutomationBench 60%, Terminal-Bench 4.0 ~38%,
GDPval Elo 1648) and weak knowledge columns (HLE-Diamond 18%, GDP.pdf 14%,
MLCRA 12%, AA-Briefcase 1449).

**Why dropping it for Inkling is coherent.** GLM's niche — mid-tier agentic
execution — is already owned by Deepseek, which beats it on the same axes
(67–68% vs 60%, 29%+ vs ~38% is the one place GLM led). Its *knowledge* slot was
the empty one, and Inkling dominates it there on every index (Legal 42 vs 35,
Finance 44 vs 39, Engineering 39 vs 34, HLE 29 vs 18, GDP.pdf 25 vs 14). The swap
traded a redundant second execution option for a first-choice reviewer. The one
thing given up is GLM's AutomationBench edge (60% vs 46%) — covered by Deepseek
at 67–68%.

## Ladder position decides whether the reviewer is reachable

Fleet capability order (most capable first, GLM's rung removed):

```text
Deepseek-V4.1-Flash-1017  >  Inkling-2-Small  >  Qwen3.8-27B
```

`core/model_routing.py` reverses `model_order` into a cheap-first candidate
list, and `apply_never_downgrade` holds a conversation **at or above the
capability position it was already served at**:

```text
cheap_first = [ Qwen3.8-27B , Inkling-2-Small , Deepseek-V4.1-Flash-1017 ]
                   index 0        index 1              index 2
```

**In this order a review asked mid-thread cannot reach Inkling.** A coding
thread already served by Deepseek is held at or above index 2, so the floor
refuses the downgrade and the review turn is served by Deepseek anyway. The
reviewer fires only from a **cold** conversation: a new thread, a review
delegated as its own request/subagent call, or a request naming
`routing__Inkling-2-Small`.

### If a profile flips Inkling to the top

Putting Inkling at `model_order[0]` makes it the router's ceiling, so review
turns can escalate to it. That is the point — but `model_order[0]` is doing two
other jobs at the same time, and both of them change:

1. **It is the cascade's first attempt.** `model_order` is the chat cascade
   ordering as well as the model ACL (`core/cascade.py`), so with routing off —
   or on any request that does not go through the router — Inkling becomes the
   profile's *default serving model*, not just its reviewer.
2. **It is where the heuristic pins.** With no `routing_min_model`, the
   heuristic's floor defaults to the ceiling: `pick_serving_model` sets
   `floor_pos = ceil_pos` when the floor is empty, span 0, and every complexity
   tier then maps to the ceiling. So whenever the decision model abstains, sits
   in `shadow`, or is unset, **every routed request in that profile is served by
   Inkling**.

Neither is a defect; it is what "ceiling" means in this code. The consequence is
that a flip is only safe for profiles whose traffic is review-shaped. In a
build-shaped profile the decision model is the only thing that sends work back
down to Deepseek, and it can only do that while it is answering above the
confidence floor.

One trap in either arrangement: an allowlisted model kept *off* the ladder is
appended at the **cheap end** by `expand_candidates` (sorted by name), so it is
positioned as the cheapest model and the router will try to downgrade light work
onto it. Keep Inkling **on** the ladder in every profile that should reach it.

**Recommended posture.** Keep the fleet flow as declared and get reviews to
Inkling by cold start; a delegated review subagent is the cleanest route, because
it is its own conversation and takes no sticky floor. Flip Inkling to the top only
in profiles that are review-shaped, keep `routing_policy=active` there so the
heuristic never becomes the decider, and accept that the sticky floor then holds
each thread on Inkling afterwards.

## Apply

```json
{
  "model_cards": {
    "Deepseek-V4.1-Flash-1017": {
      "description": "The builder: the default for agent work — coding, refactors, multi-file changes, debugging, multi-step plans and tool-driven loops that have to run to completion. The strongest option at executing in a terminal and at agentic software work. Do not use it for review, audit or hard judgment over documents — a more accurate model is worth it — and not for light chat or single lookups."
    },
    "Inkling-2-Small": {
      "description": "High-accuracy reviewer: reviewing code, diffs and designs for correctness, security and risk; checking work another model produced; hard reasoning over long documents, spreadsheets, transcripts and reports; finance, legal, medical and scientific analysis. The accuracy pick — reach for it when being wrong is expensive and speed is not the constraint. Not for building: no writing, refactoring, debugging or multi-file repo work, and no agentic terminal loops that must run to completion. Review here, implement elsewhere."
    },
    "Qwen3.8-27B": {
      "description": "Choose for light work: greetings, lookups, reformatting, classification, extraction, one-line fixes, and short questions with obvious answers. 256k context, so it also takes very long inputs cheaply. Not for multi-file changes, debugging, review or anything expensive to get wrong."
    }
  },
  "model_modalities": {
    "Deepseek-V4.1-Flash-1017": ["text", "image"],
    "Inkling-2-Small": ["text", "image"],
    "Qwen3.8-27B": ["text", "image"]
  }
}
```

Check the rendered result before saving: `POST /admin/routing/prompt-preview`
with the same `model_cards` writes nothing and shows the exact criteria text the
decision model would receive, including the auto-appended machine facts.
