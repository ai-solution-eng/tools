---
name: audit-wave
description: Use when running a multi-agent audit, a zone-partitioned remediation wave, or any large parallel-agent change to a codebase — and when deciding whether a big change is safe to release. Implements the 2026-10-02 reference arc (5-subagent audit → consolidated report → zone-partitioned remediation → 2 cross-validation waves → perf wave → same-day release). Do NOT use for a single-file or single-zone change — use cross-validate alone for that.
---

# audit-wave (multi-agent audit → remediation → release)

## The arc (reference run: a large service repo, v5.3.2, 2026-10-02)

`audit → consolidate → remediate (zone-partitioned) → cross-validate ×2 → perf → release`, all in one day.
Result: **2×P0 + 11×P1** found; final **974 passed / 1 skipped / 0 failed** (from a **842 passed / 6 failed / 1 skipped** baseline).

Do not skip a phase to save time. Each phase's cost was paid back by defects the previous phase could not see.

| Phase | Agents | Output | Gate to leave |
|---|---|---|---|
| 1 Audit | 5 subagents + lead | per-area findings | every claim spot-checked |
| 2 Consolidate | lead | one report (`documentation/AUDIT-<date>.md`) | findings severity-ranked, evidence quoted |
| 3 Remediate | N implementers, disjoint zones | fixes + tests | zone diff reviewed vs HEAD |
| 4 Cross-validate | 2 waves, direction-matched | defects in the fresh fixes | findings fixed or recorded as accepted residuals |
| 5 Release | lead | green suite, tag | `release-verify` |

## Phase 1 — audit

- **Split by breadth vs depth.** In the reference run, 5 subagents: one model family took breadth (API/identity, input processors, helm/deployment); a second took depth (MCP tool surface, data-core perf/correctness). Breadth finds surfaces; depth finds mechanisms.
- **Model-pin each subagent by `provider` + `model` together.** Pinning one alone is not a pin. **Resolve the ids with `list_subagent_models` before spawning** — registered provider ids drift between deployments and a stale id silently routes to the wrong cell, so check, never assume. (Reference deployment's ids: see below.)
- **Audits are read-only for the auditor — say so explicitly in the prompt.** Pair it with the git prohibition: "read-only for you" alone does NOT stop a write (see `guard-uncommitted-work`).
- **Spot-check EVERY subagent claim before believing it.** In the reference run **26/26 lead spot-checks held, but 2 over-claims were corrected**:
  - a claimed `tar.gz` archive-bomb exploit chain was **false** — the extractor's dispatch already caps it;
  - the repo's gitignored credential module was claimed to ship in tarballs — it is **gitignored, and NOT in the tarballs** (both verified; the file is named in the reference section).
- **An audit finding is a hypothesis until the lead reproduces it.** Reproduce by execution where you can; the P0s in the reference run were confirmed by running the exploit, not by reading the code.
- Correct severity freely — but flag the correction inline so the report shows both the claim and the verdict.

## Phase 2 — consolidate

- Write ONE report. Per finding: area, claim, **quoted source evidence** (file:line), severity, fix. The reference report's structure — P0 / P1 table / P2 condensed / P3 selected / perf quick-wins / test gaps / what's solid — is worth copying.
- Keep a **"what's solid (verified, keep)"** section. It stops the remediation wave from "fixing" hardened code, and it is evidence the audit actually read the codebase.
- Keep a **"test gaps (why the big ones survived)"** section. It is the cheapest part of the report and it is what stops the same class recurring.
- Cost model: the expensive resource is **human adjudication time** — minimize how often the human must read two full outputs. Consolidate before they are asked to look.

## Phase 3 — remediation

- **Zone-partitioned implementers** — each agent gets a disjoint write zone, and the zone is **recorded on the task** (write scope), not just in the prompt. Overlapping zones are how parallel agents overwrite each other.
- **EVERY implementer prompt must contain, verbatim:**
  > "Never run git checkout/restore/clean/stash — the user has uncommitted work."
  plus an instruction to **timeout-wrap test runs** (`timeout 120 pytest …`).
- **A stopped agent's partial state must be diff-checked before integrating.** Diff its zone vs HEAD and check mtimes for ongoing writes. Never assume its edits are complete or self-consistent — in the reference run a subagent killed during a long test run left its main module inconsistent.
- **Never run a git state operation as a "cleanup" before integrating.** That is exactly the move that destroyed the work — see the incident below.

### The incident (why the two rules above are not optional)

Reference incident (2026-10-02): a subagent stopped mid-task (killed during a long test run). An attempted rollback **wiped ~1,200 lines of the user's uncommitted work** (the watched-state machinery, ~37 references). It was recovered IN FULL from the hardlink twin, then the agent's small, session-documented layer was re-applied by hand. The gap that allowed it: the audit prompts said "read-only for you", but the **implementers had write access and no git prohibition**. Read `guard-uncommitted-work` before spawning anything that can write.

## Phase 4 — cross-validation waves

- **Direction-matched** (see `cross-validate`): each model reviews the other's + the lead's work — matched to the reviewer's characteristic failure mode. Two waves, because **the first wave reviews the audit fixes and the second reviews the wave-1 fixes plus the perf work** — fresh code is where the new defects are.
- **Two waves caught 19 defects in already-green code** (9 in wave 1, 10 in wave 2). Green tests are not evidence of correctness; they are evidence of coverage, and coverage is what the audit just told you is thin.
- **Name the failure shapes to look for** — these are real catches, and each generalizes:
  - **P0 basename collision** in a hand-re-applied prune guard (one sync spanning two prefixes with the same object name) → **key by `tmp_path`, not basename**. Hand-re-application is where collisions are born.
  - **500-regression:** `SelectionDenied` is a `PermissionError`, **not** a `ValueError` → both the REST (→403) and MCP (→ToolError) handlers needed a new `except` clause. Check the exception's real base class before writing the handler.
  - **`registry_configured()` missed two multi-user shapes** (the access-store flag alone; the trust-proxy-identity flag alone). Enumerate the shapes, then test each — a config predicate is a matrix, not a boolean.
  - **Body-cap bypass via `application/json-patch+json`.** The gate was an exact `== "application/json"` match, but the framework's own detection is **subtype-aware** (`subtype == "json" or subtype.endswith("+json")`) and calls `request.body()` for every Body-typed route regardless of content type. Measured with cap=1 MiB / 8 MiB fed: the subtype pulled the full 8 MiB at up to ~34 MiB peak allocation vs ~4.6 MiB for honest `application/json` (reported as 32× amplification; 15 Body-typed endpoints in scope). **Fix: match the framework's own rule — refuse on the declared length for every non-multipart body, and keep the counting-receive swap scoped.** When you gate on a framework's parse rule, copy the framework's exact predicate.
  - **`_prefix_of` used `rstrip("/")`** → prefix `s3://b/a/` also matched sibling `s3://b/ab/…`. Strip only the exact delimiter; prefix matching is boundary arithmetic.
  - **An empty-listing prune guard stranded legitimately-emptied prefixes forever** — the guard must refuse only when the listing produced *nothing at all* while stored sources exist.
- **Require the reviewer to probe at runtime, not just read.** The wave-2 report measured middleware order from the app's own middleware list, fed real oversized bodies, and confirmed the ~1× cap memory bound on a real uvicorn (+3 MiB RSS against a declared 300 MiB body). It also confirmed the fixes that mattered — say what held, not only what broke.
- **Accept residuals explicitly.** Non-cancellable executor futures, the ⌈N/C⌉×timeout hang bound, the `.part` leak — documented as accepted, not silently dropped.

## Test/CI reliability (the audit's own blind spot)

- **Module-global caches in the core package must be handle-scoped**, or fake backends cross-contaminate between tests.
- **Fakes must key ACL rows by the real PK** (with `allowed` as a VALUE) — otherwise whole PK-bug classes are invisible.
- **A test barrier needs `wait_for`** (or re-sequencing), or regressions hang CI instead of failing.
- **Hung vs erroring tests:** a test that patches a module attribute fails with `AttributeError` at `getattr` when the source function is missing — an error cascade under `-x` **looks like a hang**. Run `pytest -o faulthandler_timeout=60` to get real stack dumps before diagnosing a "hang".
- Stale render-test assertions are normal test drift on every version bump — fix once, don't chase them per release.

## Phase 5 — release

Hand off to `release-verify` for the build/deploy/verify path. Two rules that bite here: the project's build script builds from the **working tree** (uncommitted changes ship; a run fired before edits land bakes the old code), and a pushed tag is **poisoned** — always version-bump.

## Anti-patterns

- Treating the audit as done when the report is written — the report is phase 2 of 5.
- Parallel implementers without disjoint zones, or zones not recorded on the task.
- Integrating a stopped agent's zone without a diff.
- One cross-validation wave. The second wave is where the fixes' own defects surface.
- Declaring green tests a verdict. 19 defects were found in already-green code.

## Reference instantiation

One deployment's concrete values (2026-10-02) — re-derive locally, never hardcode:

- Target repo: `/home/andrew/Code/HPE/MultimodalRAG/`, version `v5.3.2`; report at `documentation/AUDIT-2026-10-02.md`.
- Subagent provider ids (verified 2026-10-04 via `list_subagent_models`): `pcai-model-endpoint-flow` (models `Deepseek-V4.1-Flash-1017`, `GLM-5.3-Flash`, `Qwen3.8-27B`, `auto`). These differ from the `pcai-ds` / `pcai-glm` shorthands used elsewhere — those are model *names*, not spawnable provider ids.
- Breadth/depth split: GLM-5.3-Flash took breadth (API/identity, processors, helm); DeepSeek-V4.1-Flash took depth (MCP tool surface, data core).
- Over-claim #2's file: `src/multimodal_rag/utils/pcai_models.py` (gitignored; absent from the shipped tarballs).
- Build script named in Phase 5: `automation.sh`.
