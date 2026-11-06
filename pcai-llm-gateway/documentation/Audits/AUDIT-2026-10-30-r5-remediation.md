# AUDIT r5 — verification & remediation record (2026-10-30)

**Source audit:** `.scratch/audit-r5/AUDIT-r5.md` (41 findings, 2026-10-29, GLM ⇄ DeepSeek).
**This wave:** verify every claim against the working tree, fix the P0/P1 set, cross-validate the fixes.
**Team:** Lead (machine layer + adjudication) · 6 implementers · 2 P2 verifiers · 2 opposite-family reviewers
(model-pinned: DeepSeek-V4.1-Flash-1017 and GLM-5.3-Flash; reviewers always the *other* family from the author).

---

## 1. Verdict on the audit's own claims

| Claim | Verdict |
|---|---|
| Artifacts live in `.scratch/audit-r5/` | **True.** 22 reports + 40 proofs, mtimes preserved from the 2026-10-29 14:48 run; the directory entered `.scratch/` at 2026-10-30 17:24 (ctime). `.scratch/` is gitignored — "everything is in the repo" means the working tree, not git. |
| "The 2,381-test suite could not run" | **False on this host.** The tree at the audit's own state runs **2721 tests in ~32 s**. Every EXEC claim was therefore re-checkable here, and every fix below is pinned by a test that fails without it. |
| "506 citations → 503 exact, 0 fabricated" | **Weaker than it sounds.** The machine check proves only that the cited file exists and the line range is in bounds — not that the quoted code is at those lines. Re-ran it: 1036 citations across all 22 files, 1032 resolve; the 4 misses are basename-resolution artifacts (`db/__init__.py`), as the report itself says. Content spot-checks of every P0/P1 citation by the Lead: all matched. |
| 13 P0/P1 findings | **12 live** on the current tree. **#12** (NaN budget door) was already fixed after the audit (`BudgetSpec.limit_usd = Field(..., allow_inf_nan=False)`); a wire-level pin was added. |
| 28 P2 findings (#14–#41) | **All live** except #19 (partially fixed), #36 (severity P3 — the audit's own §5 contradicts its §2), #39 ("permanently firing" overstated: an empty numerator yields no sample; the missing `> 0.10` is real), #40 (fixed mid-verification). |
| #15 "chart-default ON" | **Wrong for the base chart** — `helm/values.yaml` ships `distillerEnabled: false`; the distiller is ON only in the g2 site overlay. Mechanism unaffected where it runs. |
| §4 regression table: "5 [x] items not in tree" | **3 verified absent** (conversations cap, MCP-allowlist migration marker, `expires_at` past-date rejection). The grace period **is** present but too short (the report says "half"); the HPA×pool item is not a REMEDIATION `[x]` item at all. |
| 3 refutations | **All sound.** mm-rag IS in `READ_SERVERS` (`{read, mutate}`); `hub.dispatch`'s only caller always passes a finite timeout; `model_type` is per-model and the admin write path rejects mixed-type pools. |

Line drift since 2026-10-29 was significant (`routes_proxy.py` +119, `routing_config.py` +29, `routes_admin.py` +25…+85); every citation was re-located by function, never by offset.

---

## 2. Fixed (13 P0/P1 + 1 P2, each with a pin)

| # | Sev | Defect | Fix |
|---|---|---|---|
| 1 | **P0** | One percent-encoded whitespace disabled the whole MCP destination policy | Fragment stripped before classification (raw + `%23`); decoded-whitespace exemption only for non-URL payloads; RULE 1's raw-whitespace exemption no longer covers `scheme://`; new protocol-relative rule (`//host`, bracketed IPv6); `path(s)` are destination keys; parent-relative paths stay allowed. `mcpf/arg_policy.py` |
| 2 | P1 | Masked replay leaked via dict `pcai.args`, `n>1` choices, split reads | Type-blind payload classification (`json.dumps`), every choice judged + rewritten, line cursor across reads. `api/routes_proxy.py` |
| 3 | P1 | Keyword mask applied `lower()` offsets to the original text | Length-preserving fold (`İ/ı/ſ` + the interpreter's full `re.IGNORECASE` casefix table, union-found at import), used by the span pass, the keyword validator AND the deny-side matcher. `core/guardrails.py` |
| 4 | P1 | Admin-group check preferred the header the proxy never sets | Proxy spelling first; the forwarded spelling is consulted **only** behind the new `trustForwardedGroups` flag (default false — the groupless case was still a self-grant). `core/identity.py`, `config.py`, helm |
| 5 | P1 | Revoking/expiring a shadow key self-healed into a fresh unbudgeted key | A gate-less probe distinguishes ABSENT from REFUSED; a revoked/expired row is terminal. `core/identity.py` |
| 6 | P1 | AuthorizationPolicy rule (ii) admitted the whole namespace to /admin + mints | Rule (ii) now carries `from`+`to` ANDed; both (ii) and (iv) exclude all four surfaces **and their subtrees** (`/admin/*` — a bare `/admin` is an exact match and left the API admitted). `helm/templates/authorizationpolicy.yaml` |
| 7 | P1 | EMAIL regex quadratic (8.4 s stall at 100k) | Bounded local/domain (`{1,64}`/`{1,255}`): 11.5 ms, semantics pinned. `core/guardrails.py` |
| 8 | P1 | Streamed chat billed the global off-peak fraction | `effective_for(self._model, …)`; zero global `effective()` calls left in `src`. `api/routes_proxy.py` |
| 9 | P1 | A guardrail block spent money no cost column or budget gate saw | One `_COSTED_STATUSES` set drives the price branch and **all five** SUM aggregates (per-key gate, console rollup, team envelope, team allocation). `core/budgets.py`, `core/teams.py` |
| 10 | P1 | Routing panel's `enabled` kill switch was inert | `global_enabled()` (TTL-cached, fail-open) ANDed into the gate and `engaged`; `/v1/models` advertisement follows it. `core/model_routing.py`, `api/routes_proxy.py` |
| 11 | P1 | Disconnect during post-call guardrails lost the ledger row and leaked the slot | `except BaseException` with shielded `finish()` + `release_slot()`, then re-raise. `api/routes_proxy.py` |
| 12 | P1 | `limit_usd: NaN` reached the DB | **Already fixed** post-audit; wire-level pin added (raw `NaN`/`Infinity` → 422, zero writes). `api/routes_admin.py`, tests |
| 13 | P1 | The documented ledger-partition runbook could not run | `routing_probabilities` added (20 cols); `usage_ledger_pkey` + `idx_ledger_ts` renamed/recreated; `ck_ledger_status` restamped **after** the copy (NOT VALID does not protect subsequent INSERTs). `scripts/partition_ledger.py` |
| 40 | P2 | 3.12-only syntax under `requires-python >=3.11` | `TypeVar` spelling + a repo-wide 3.11 AST-parse test. `api/routes_mcp.py` |

New/updated tests: **+84** (2721 → 2805). Every pin was demonstrated to fail against the pre-fix logic.

---

## 3. Cross-validation (the part that earned its cost)

Three rounds, each reviewer the opposite model family from the author. **Eight defects were found in fixes that were already green (2 P1, 5 P2), plus one missing pin:**

1. **P1** — the header *swap* left the groupless case spoofable (a user with no groups assertion self-granted admin via `X-Forwarded-Groups`) → `trustForwardedGroups` flag.
2. **P1** — `notPaths: "/admin"` is an exact match; the whole `/admin/*` API stayed admitted → 8-entry subtree list.
3. **P2** — the mask fold covered only `İ`; 50 further `re.IGNORECASE` classes still under-masked (`σ`/`ς`) and the deny-side matcher disagreed with the masker → full casefix fold.
4. **P2** — declining on `İ` re-armed the 340-600 ms stall (one prepended character) → fold instead of decline.
5. **P2** — protocol-relative `//169.254.169.254/x` bypassed the destination policy → RULE 1b.
6. **P2** — `paths?` as a destination key refused parent-relative local paths (`../up/x.txt`) → `_parent_relative` guard.
7. **P2** — the partition script stamped the CHECK *before* the copy, so a retired status would abort the migration → moved after.
8. **P2** — the fold was not a closure over UPPERCASE forms (`fold("Σ") != fold("σ")`), and behind that, `str.lower()`'s contextual Greek Final_Sigma rule made `Σ`'s image neighbour-dependent. Final design: union the casefix classes + `İ↔i` + per-member upper/lower, then every 1:1 lowercase pair collected with a cased guard letter (so the context rule cannot fire), canonical = a member that is its own lowercase, and an identity-filled translate list. Verified here exhaustively: 0 split classes, 0 length violations over 0..0x10FFFF, 0 fuzz mismatches in 4000 random cases.
9. **P3** — the "missing table degrades safely" safety net had no pin → pinned.

Each was reproduced by execution before being fixed, and each fix carries a pin.

---

## 4. Gates

```
pytest ............ 2802 passed in 31.9 s
ruff check ........ All checks passed  (src/pcai_llm + tests)
ruff format ....... 169 files already formatted
mypy src .......... Success: no issues found in 65 source files
mypy tests ........ Success: no issues found in 103 source files
```

Note for the next runner: `mypy tests/` from the repo root trips a mypy module-mapping artifact
("source file found twice under different module names") because `tests/` has no `__init__.py` and the
CWD is on the path. Run it from outside the repo (`cd /tmp && mypy --config-file <repo>/mypy.ini <repo>/tests`)
or add `--explicit-package-bases`; the shared `mypy.ini` is a fleet file and was deliberately not edited.

---

## 5. Verified live, deliberately NOT fixed (tracked)

P2/P3, all still open on this tree: #14 lease 300 s < row budget 900 s · #15 `VERDICT_FLOORED` never marked · #16 shared row-#1 exemption · #17 distiller settles at 1.0 · #18 off-ladder allowlist still serves the unticked head · #19 active-mode ceiling overspend (shadow half fixed) · #20 zero-completion retry unsettled · #21 streamed completion booked prompt-only · #22 relay TPM undercount · #23 relayed rows never stamped · #24 failure not cached · #25 non-ASCII dot spellings · #26 mm-rag is not an egress class · #27 stale `model_order` mints allow-all · #28 unknown-kid JWKS flood · #29 token rung without issuer pinning · #30 concurrent first-sighting 401 · #31 rotating an expired key · #32 chain-audit misclassification · #33 unpaginated `/admin/keys` · #34 probe before the SSRF gate · #35 boot-time allowlist repair · #36 legacy distiller_config decode · #37 unconditional master-key secretKeyRef · #38 ingress catch-all routes /admin · #39 PrometheusRule operator + labels · #41 cold-cache defaults cached 60 s.

Also unfixed and outside this wave: the known-open 4xx→circuit-breaker defect (2026-10-28 §4), the
`/v1/keys/for-me` per-subject throttle (REMEDIATION `[ ]`), the conversations-surface cap, the MCP
allowlist migration marker, `expires_at` past-date rejection, and the HPA × `dbPoolMax` guard.
