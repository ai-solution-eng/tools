---
name: audit-quickstart
description: 'Use when the user asks for a "security/perf/bug audit", "agent teams search", or any multi-model review of a repo and you have already decided to run one — the condensed phase-1 spawn recipe: zone partition, model pins, the verbatim prompt block, and the exact pre-spawn checklist. NOT for the full audit→remediate→release arc (use audit-wave), for spawn mechanics (subagent-teams/ptc-team-runtime), or for the collect loop itself (team-collect-loop).'
---

# audit-quickstart — phase-1 audit wave, the 10-minute setup

A condensed, executable front half of `audit-wave` phase 1. When the engagement will
continue into remediation/release, load `audit-wave` in full — this is the fast path
for "find the problems now".

## Pre-spawn checklist (all six, in order — each maps to a real failure)

1. **Recon the repo layout** — module list + line counts (`wc -l`), so zones are
   real partitions, not guesses.
2. **`git status --porcelain` + `git diff --stat HEAD`** — uncommitted work is
   NORMAL. Record the dirty-file count; every prompt carries the git prohibition
   regardless of read-only status (guard-uncommitted-work).
3. **`tools.list_agents({})`** — verify the team surface is mounted at all.
4. **`tools.list_subagent_models({})`** — STRING return; then
   `{provider: "<id>"}` for models; then `{provider, model}` for reasoning efforts.
5. **`__schema("spawn_teammate")`** and `__schema` on every board tool before first use.
6. **Create the roster task** on the shared board (`team_task_create`): zone map,
   model pins, dirty-file count, "lead spot-checks every claim".

## Zone partition (breadth/depth split)

- **Breadth zones → calibrated/abstaining cell** (GLM-5.3-Flash): auth/identity,
  policy/sharing, server/webui/API surface. Breadth finds surfaces.
- **Depth zones → environment-trained agentic cell** (DeepSeek-V4.1-Flash): the
  biggest/correctness-critical modules, write paths, caches. Depth finds mechanisms.
- Disjoint file lists per zone; say "read only these" explicitly.
- 3-5 auditors is the sweet spot: every report gets actually read and spot-checked.

## The prompt block (paste verbatim into every auditor prompt)

```
NEVER run git checkout/restore/clean/stash — the user has uncommitted work.
Timeout-wrap every test run.

YOU ARE READ-ONLY: do not edit, create, or delete ANY file. Do not run tests or
anything that writes. Only read files and run read-only git commands.

REPORT FORMAT (final message): for each finding:
[P0|P1|P2|P3] <one-line title> | file:line | quoted source evidence (paste the
actual lines) | why it is a problem | concrete fix suggestion.
P0 = exploitable/data-loss; P1 = likely defect or real perf cliff;
P2 = latent bug/smell; P3 = hardening/nit.
End with "SOLID (verified, keep)" and "TEST GAPS" lists.
Quote evidence — never paraphrase without file:line. Unsure = mark HYPOTHESIS.
```

Rationale per line: the git prohibition is verbatim because "read-only for you"
alone does NOT stop a write (2026-10-02 incident); "quoted evidence" is what makes
lead spot-checking (below) possible; HYPOTHESIS marking prevents over-claims from
infecting the consolidated report.

## After spawn

1. Verify each spawn's `member.model` echo shows the pinned model, never `auto`.
2. `list_agents` must show every member addressable — record targets on the board.
3. Collect with the chunked-wait loop (`team-collect-loop`).
4. **Spot-check every claim before consolidation.** Reproduce by execution where
   possible. Correct severity freely but inline. Expect ~1-2 over-claims per wave
   (reference run: 26/26 held but 2 corrected — archive-bomb chain was false,
   credential-in-tarball claim was false).

## Anti-patterns

- Auditor prompt without the verbatim git prohibition "because it's read-only".
- Both cells assigned the same KIND of zone (symmetric coverage wastes the ensemble).
- Believing a finding without file:line evidence attached.
- Skipping the roster task on the board — zone ownership is what prevents two
  agents from wandering the same files.
