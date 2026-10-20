---
name: guard-uncommitted-work
description: Use when about to spawn ANY agent or subagent that can write files, when integrating a stopped/killed/partial agent's work, or when a cleanup step is about to run a git state operation. Protects the user's uncommitted work from agent rollback.
---

# guard-uncommitted-work

## The prohibition (standing rule — non-negotiable)

**Never run `git checkout` / `git restore` / `git clean` / `git stash` in the user's repos. Ask first.**
This holds for every agent you spawn, every script you write, and every "let me just reset this" instinct.

Uncommitted work is **NORMAL**, not a mess to clean up. Reference incident (2026-10-02): one target repo carried **~111 uncommitted files / ~14.3k insertions** awaiting review. A "clean tree" is never the default assumption — verify with `git status` before you believe one.

## Before you spawn a writable agent — every time

Add this line **verbatim** to the prompt:

> **"Never run git checkout/restore/clean/stash — the user has uncommitted work. Timeout-wrap every test run."**

| Agent capability | Required in prompt |
|---|---|
| Read-only (audit/review) | "Read-only for you" **AND** the git prohibition |
| Writes files | the git prohibition + **disjoint write zone** + timeout-wrapped tests |
| Runs tests / long jobs | the git prohibition + `timeout 120 …` |

**"Read-only for you" is NOT sufficient** — and this is the exact gap that destroyed the work. Reference incident (2026-10-02): the audit prompts said "read-only for you", but the **implementers had write access and no git prohibition**. State the git prohibition explicitly even when the agent has no write scope; a prompt-only restriction does not constrain a tool.

## The incident (why this skill exists)

Reference incident (2026-10-02): a subagent stopped mid-task (killed during a long test run). An attempted rollback **wiped ~1,200 lines of the user's uncommitted work** (the watched-state machinery, ~37 references). It was recovered IN FULL from the hardlink twin — then the agent's small, session-documented layer was re-applied by hand. Recovery was luck plus a mirror; the guard is cheaper.

## Integrating a stopped / partial agent

Do all four before believing its zone:

1. `git status` + `git diff --stat` in the repo — see the real working-tree shape first.
2. **Diff its zone vs HEAD** — never assume its edits are complete or self-consistent.
3. **Check mtimes** for writes still in flight (a live process may still be editing).
4. Re-run its tests yourself, timeout-wrapped. A stopped agent's "all green" is not a result.

A stopped agent leaves **inconsistent** state, not merely incomplete state — in the reference incident a killed run left one module inconsistent mid-file.

## Recovery from the twin

A repo family may be mirrored by a **twin** — a second working copy produced by the family's hardlinker. Resolve it from the repo's own `hardlink_config.json`: its `source` key names the source repo, its `dest` key names the mirror. **Check that config before assuming no mirror exists.** Hardlinking can be detached (git ops and sync daemons silently re-inode files), so treat the twin as a copy to read from, not a guaranteed live view.

The twin is a real recovery source **even when the hardlinking is currently detached** — it holds a separate copy of the tree, so a file wiped from the source is often still present there.

**The twin has its OWN git history — it is not a bare file copy.** It is tracked inside a *different* repo (no nested `.git`; `git -C <twin> rev-parse --show-toplevel` resolves to that outer repo; reference: ~531 tracked files, real commits). So `git -C <twin> log -p -- <path>` and `git -C <twin> show <rev>:<path>` DO work and are your fastest way to recover a pre-agent version. The twin's working tree carries its own untracked entries, and **its HEAD is not the source repo's HEAD** — never assume the two repos share a revision.

Recovery = **restore the file from the twin, then re-apply the agent's small, documented layer by hand.** Re-apply carefully: hand-re-application is where collisions are born (a hand-re-applied prune guard produced a P0 basename collision in the reference run).

**Caveat — recovery yes, build no.** The twin's ignore rules can exclude files the build needs, so a twin can never be assumed to produce a working image. Reference: the build/version scripts and the credential file are **absent** from the twin while present in the source repo (the credential file is gitignored and lives only in the source working tree). Verify per repo before trusting a twin as a build source.

## Environment gotchas

- **A file-sync daemon can race file writes** — conflict-copy litter (`*sync-conflict*`, e.g. `.pyc` files under `__pycache__`) is the tell. Check for it when files "change between checks".
- **`/tmp` does not persist between bash tool calls** — every verification must be one self-contained call; never stage a file in `/tmp` and read it in a later call.
- Do **not** sync or write to the mirror from a session — the maintainer manages the hardlinking. Editing files under the source repo only is the correct scope.
- **Never run a git state operation to "fix" link or drift state** — the repair is the linker, not a reset (see `fleet-drift`).

## Handoff

Spawning writable agents at scale? Read `audit-wave` for zone partitioning and the stopped-agent integration rules. Doing the review itself? `cross-validate`. Link/drift state in question? `fleet-drift`.

## Reference instantiation

One deployment's concrete values (2026-10-02) — re-derive locally, never hardcode:

- Source repo `<repo>` = `/home/andrew/Code/HPE/MultimodalRAG/`; mirror `<twin>` = `/home/andrew/Code/HPE/pcai-solutions/foundational-workflows/multimodal-rag/`, per that repo's `hardlink_config.json` (`source` / `dest`, `on_conflict: overwrite`).
- The twin is tracked inside the `foundational-workflows` repo (no nested `.git`); the twin subdir carries ~531 tracked files, and its HEAD is not the source repo's HEAD.
- Hardlink state at the reference deployment (checked 2026-10-06): the twin files were **detached** from the source (different inodes, source nlink 1) — the twin was serving as an independent copy, not a live hardlink view.
- Absent from the twin, present in the source repo: `automation.sh`, `bump_version.sh`, `prune_charts.py`. Absent from both trees at repo root: `pcai_models.py` — gitignored, present only as `src/multimodal_rag/utils/pcai_models.py` in the source working tree.
- The wiped work was the watched-state machinery (~37 references) in the dataset-manager module.
- Sync daemon in the reference deployment: Syncthing.
