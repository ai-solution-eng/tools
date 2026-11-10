# REMEDIATION 2026-11-10 — AUDIT-2026-11-10, zone-partitioned wave

Companion to [AUDIT-2026-11-10.md](AUDIT-2026-11-10.md). Same day, five phases:
audit → consolidate → remediate (9 disjoint zones) → cross-validate → integrate.

| | |
|---|---|
| **Tree** | branch `main`, HEAD `50bb595`; working tree carries the user's pre-existing uncommitted work |
| **Baseline** | `3408 passed` before the wave |
| **Result** | `3714 passed / 0 failed`, ruff clean, mypy clean (74 modules) — **+306 tests** (305 of them in the new `tests/test_audit_20261110_*.py` files) |
| **Recovery point** | `.scratch/remediation-2026-11-10/` — pre- and post-remediation tarballs, taken before any writer ran |
| **Method** | 9 model-pinned implementers on disjoint write scopes → 3 direction-matched reviewers → Lead adjudication + integration |

---

## 1. Phase 3 — remediation

Nine implementers, one per audit zone, each with an exclusive file list recorded as a write scope on the task board. Every prompt carried the git prohibition verbatim and a timeout-wrapped test rule.

| Zone | Model | Scope | Findings closed |
|---|---|---|---|
| R1 proxy hot path | DeepSeek-V4.1-Flash-1017 | routes_proxy, canonical, context_window, stickiness, response_cache | 3 P1, 6 P2, 3 P3 |
| R2 budgets/routing | DeepSeek-V4.1-Flash-1017 | budgets, costs, model_routing, routing_config, offpeak | 1 P1, 4 P2, 4 P3 |
| R3 MCP relay | DeepSeek-V4.1-Flash-1017 | toolrelay, routes_mcp, arg_policy, federation, toolcompress* | 2 P1, 6 P2, 7 P3 |
| R4 admin API | GLM-5.3-Flash | routes_admin, model_groups, registry, agents | 1 P1, 2 P2, 5 P3 |
| R5 guardrails/chain/helm | GLM-5.3-Flash | guardrails, url_guard, audit, chain_audit, helm/ | 1 P1, 4 P2, 5 P3 |
| R6 admin UI | Qwen3.8-27B | admin.html, index.html, team_leader.html | 4 P2, 6 P3 |
| R7 teams/enroll/distill | DeepSeek-V4.1-Flash-1017 | routes_enroll, routes_team_leader, teams, distiller, routes_distill | 1 P1, 5 P2, 5 P3 |
| R8 identity/keys/auth | GLM-5.3-Flash | auth, identity, keys, secretbox, upstream_auth, model_type_detect | 2 P2, 6 P3 |
| R9 config/app/DB/observability | GLM-5.3-Flash | config, app, schema, metrics, tracing, housekeeping, embedcache | 2 P2, 5 P3 |

**Every finding was addressed.** Nothing was silently dropped; each was reported FIXED, ACCEPTED-RESIDUAL with a written reason, or NOT-REPRODUCED with evidence. One item from the audit was already refuted at consolidation time (the propagate-grants CTE — tested on a live Postgres and correct), and the implementers were told not to change it.

### Notable residuals accepted (with reasons)

- **P2-12 (shadow user rows keyed by email).** Code fixed to key by SUBJECT; pre-fix deployments' email-named rows are **not migrated**. Safe because re-provisioning never revisits an existing subject (the r5 #5 terminal rule), so legacy rows are reachable only via their own subject's live key. Re-homing a shared legacy row is a data decision, not a code one.
- **`uq_api_keys_one_live_sso` ignores expiry.** An *expired* `sso-create` key still occupies the one-live slot. The obvious fix — adding expiry to the partial index — is **impossible**: `now()` is STABLE and PostgreSQL requires index predicates to be IMMUTABLE. The correct fix is application-level (rotate the expired row before binding), which is a design change, not a remediation.
- **`anchor_status` is not rendered in the console.** The API exposes it (fail-closed since wave 1), but `ui/admin.html` has no chain-verify consumer at all; adding one is a feature, not a fix.
- **Audit test-gap #9 remains open** — `SCHEMA_SQL` applied twice against a pre-migration DB with a concurrent writer, on a real Postgres. Several schema gates are proven by reading and by substring pins, not by execution. This is the single highest-value remaining test investment.

## 2. Phase 4 — cross-validation wave 1

Three reviewers, direction-matched (calibrated GLM reviewing the confident DeepSeek work for factual integrity; DeepSeek reviewing the GLM/Qwen work for execution completeness). Reviewers were read-only and probed by **execution** — live module calls, a throwaway Postgres 18, and `helm template` across four values files.

**Wave 1 found 9 defects in already-green code, including 4 P1s — three of them in the fresh fixes themselves.** This is the phase that justified itself.

| Severity | Finding | Why the green suite missed it |
|---|---|---|
| **P1** | **P1-4 fix defeated by multiplicity.** The admission charge was *popped* by the first settle, but the relay settles once per iteration under one `request_id` — so iterations 2..N refunded unclamped. Measured: charge 1,000, refunds `[1000, 59000, 69000]` = **129× the charge**, TPM off for the rest of the minute. | Every test settled exactly once. |
| **P1** | **P1-6 card fix masked at CLEANED offsets.** Zero-width chars were stripped into a shorter `cleaned` string, but offsets were spliced into the original — the mask landed on unrelated text and digits survived, *while the audit event said "masked"*. `'\u200b'*16 + card` returned the **entire PAN**. | The test asserted `'4111' not in masked` — true for `'[REDACTED]111'`. |
| **P1** | **Exec-argument scan truncated at 64 tokens** — ~96 chars of `A=1 ` padding pushed the metadata URL past the slice and it was ALLOWED, contradicting the module's own "bounds are a refusal, not a truncation" invariant. | No padded-command test. |
| **P1** | **`conversations.key_group SET NOT NULL` with no DEFAULT.** A row inserted between the backfill UPDATE and the ALTER (old pod mid rolling-upgrade) fails the NOT NULL validation → the whole implicit schema transaction rolls back → **boot abort**, re-armed per HPA pod. Reproduced with a two-session PG race. | Schema gates pinned by substring greps. |
| P2 | `stream_once` pick-None yielded an error event the relay does not fail over on — a breaker-cooled primary with healthy fallbacks produced an answerless turn. | No test with a cooled primary + healthy fallback. |
| P2 | The metadata cost read was unguarded — a `model_costs` fault 500s a response the upstream was already paid for. | No fault-injection on that path. |
| P2 | The tail anchor was defeatable by **deleting one row** — `verify()` still answered `ok:true` with `anchored:false`. | The test pinned `ok is True` for the delete-the-anchor attack — it locked the bypass in. |
| P2 | The `url_guard` DNS fix turned a thread leak into a **global SSRF-check outage**: 4 shared workers + uncancellable `getaddrinfo` = saturation, and saturation was fail-open. | No saturation test. |
| P3 | `guardrails_engine_check` re-added without `NOT VALID`; `/admin/costs` array→object broke the console during a rolling upgrade; a mangled operator command in `values.g2.yaml`; over-masking of dotted decimal prose. | — |

All were fixed and re-verified; the suite went 3649 → 3682. The reviewers also flagged **trivially-passing tests** — several new tests would still pass with their fix reverted — and those were strengthened (notably the zero-width test now asserts no digit survives, and the anchor test was inverted to fail closed).

## 2b. Cross-validation wave 2 — the fixes' own fixes

Wave 2 reviewed (a) the wave-1 repairs, because fast repairs under review pressure are where new defects are born, and (b) zone P10. Two reviewers, again direction-matched and probing by execution.

**Wave-1 repairs: 4 of 6 fully clean on first inspection; 3 residual gaps found and fixed.**

| Sev | Finding | Fix |
|---|---|---|
| P2 | The card gate's dot rule **fused `<digit>.<PAN>`** and leaked the whole PAN — the widened separator class let the leftmost greedy candidate swallow a touching decimal, and `finditer` is non-overlapping so the inner PAN was never retried. Dotted Amex 4-6-5 / Diners 4-6-4 also leaked. | Dotted spans now accept 4/5/6 grouping; a rejected dotted candidate re-scans its **tail** and masks from the first 4+ group. Verified: fused decimal, dotted Amex/Diners, 4×4 and zero-width all mask; decimal prose untouched. |
| P2 | A **wiped** audit trail verified `ok:true` as `'empty-trail'` even when the surviving anchor row proved rows had existed — the `last_id == 0` early-return fired before the anchor was consulted, so bulk delete re-opened the attack class wave 1 had just closed. | The anchor is now read *before* that branch: `last_id == 0` + an anchor row ⇒ `ok:false, kind='trail-wiped'` (forged MAC included — a forged anchor still proves a tail existed). `'empty-trail'` stays green only for a genuinely fresh deployment. |
| P3 | `worker.start()` failure permanently leaked a resolver slot (the release lived inside the thread body), so eight failures saturated the resolver for the process lifetime. | Construction + start wrapped in `try/except BaseException`: release and re-raise. |

**Zone P10: four P1s found and fixed.**

| Sev | Finding | Why the green suite missed it |
|---|---|---|
| **P1** | **A voice model priced `unit='second'` was billed through the TOKEN arithmetic.** `'second_estimate'` is not in `costs.UNITS`, so the row fell to `compute()` and applied the per-*second* rate as a per-*token* rate — measured **3.0× over-billed** for ASR, 3.75× for TTS. This is the exact wrong-rate bill the feature exists to prevent, and the code's own warning claimed it was impossible. | The estimate path had no test with a non-token stored unit. |
| **P1** | **`?duration=` was unbounded** and beat the upstream's measured duration: `0.001` ⇒ a free 3.6 MB ASR, `99999999` ⇒ a **$1,500** immutable ledger row. | No bound was ever asserted. |
| **P1** | **`billable_units ≥ 1e12` overflowed `NUMERIC(18,6)`** — asyncpg raised, the INSERT never ran, and the immutable billing row was **lost** for a request the upstream had already served. | No oversized-quantity test. |
| **P1** | **`ck_model_costs_unit` was added VALID**, so one stale `unit` value aborted the whole boot transaction on every replica, permanently. | Schema gates were pinned by substring, never executed. |

Fixes: the effective unit is derived from the tag and gated on the **stored** unit (so `token` keeps legacy arithmetic byte-identically while a repriced voice model bills real seconds, and an un-convertible char estimate ledgers NULL rather than a fabricated conversion); the hint is clamped into the documented compression envelope with the upstream duration taking precedence; both the quantity and the computed cost are validated against their column ceilings before the INSERT (row never lost); the CHECK is `NOT VALID` **and** gated on `pg_get_constraintdef` so a stale-vocabulary constraint is replaced rather than surviving to 500 writes. Plus the summary's routing-row contamination, the cross-unit `SUM(billable_units)`, and the misleading MISCONFIGURED warning.

**Verified by execution after the fix:** `SCHEMA_SQL` applied three times on a fresh Postgres 18 ⇒ OK/OK/OK, `ck_model_costs_unit convalidated=False` (NOT VALID), and — the decisive one — a database holding a stale `unit='seconds'` row now **re-applies clean without aborting the boot**, leaving the row visible for the operator to correct.

## 3. Phase 5 — integration (Lead)

- **25 pre-existing tests were failing** at the end of the implementer phase. Every one was adjudicated by reading the assertion and the new code: 24 pinned superseded behaviour (raw session key vs hashed, fail-open deny rows, JWT-shaped fakes that cannot exist in production, the email-collision user row, ungated model-group guards, the `enroll.redeem`→`enroll.sso_mint` rename, the body-wide MutationObserver, `banner(esc(...))` now that banner escapes by default, the batched team-listing fixtures). They were updated to the corrected contract, each with a comment recording *why*. One was a genuine test bug: a `';'`-splitting statement counter that shreds `DO $$ … $$` blocks.
- **4 mypy errors** the wave introduced were fixed: a SQL placeholder string shadowing a `_SpendLineage` parameter, an annotated dict immediately subscripted into a tuple, and a `bool | None` classifier assigned to a `bool`-narrowed variable.
- **Cross-zone comment corrections** that the implementers flagged but could not reach: the stale "default `tool_relay_grant_mode='scope'`" claims (the default is `explicit`), the stale "`load_denies` is fail-open-isolated" claim (it now fails closed), and `enroll.sso_mint` registered in `AUDIT_ACTIONS_DOC` and the console's action filter.
- **Estimator parity:** `toolrelay._message_text_tokens` now charges through `context_window.text_tokens`, so the admission path and its documented twin cannot diverge on non-ASCII again.
- **`documentation/API.md`** updated for the `GET /admin/costs` object shape and the planned unit column.

## 4. What is verified, not assumed

- Every P1 in the audit was reproduced before being fixed (Postgres for the `GROUP BY` 500 and the CTE refutation; live function calls for the card mask and the diff compressor).
- Every P1 in wave 1 was reproduced by the reviewer, and the three fix-integrity ones were re-reproduced by the Lead.
- `git status` before and after: the wave touched only `src/`, `tests/`, `helm/` and `documentation/`; no git state operation was run by any agent; the recovery snapshot was never touched by an agent.

## 5. Zone P10 — unit-aware pricing (owner request, same day)

The audit's §7 finding — *one pricing mechanism, every model type forced through it as a token count* — implemented as a tenth zone.

- **Schema:** `model_costs.unit` (`token|char|second|image|request`, default `token`) + `usage_ledger.billable_units`/`unit_kind`. Additive and idempotent; every existing row keeps billing exactly as before. `ck_ledger_status` gains `routing`; `scripts/partition_ledger.py` updated to match (the deploy-contract parity pin requires it).
- **Pricing:** `costs.compute()` untouched (chat is byte-identical). New `compute_units(model, units, unit)` treats `input_per_m` as USD per 1M **units** and returns `None` on a unit mismatch rather than billing the wrong rate. Lead-verified: 1M in/out chat = `18.000000`; 100 s of TTS at 10 USD/1M s = `0.001000`; a chat model billed in seconds → `None` + a named WARNING; an unpriced model → `None`.
- **Routes:** TTS bills a **real** duration when the upstream reports one (`X-Audio-Duration` or a sample-count/sample-rate pair), else the legacy char proxy tagged `char_estimate`. ASR takes `?duration=` > `X-PCAI-Duration` > upstream duration, else the byte proxy tagged `second_estimate`; the byte divisor is now `audio_asr_bytes_per_second` (documented as raw 16 kHz mono — compressed formats under-bill, so send the hint). Embeddings/rerank keep token estimates tagged `token_estimate`.
- **Routing model:** `_decision_caller` now settles its own call under the calling key with status `routing`, gated by `routing_charge_enabled`. `routing` is deliberately **not** in `_COSTED_STATUSES` (Lead-verified) — it is priced and visible in chargeback but consumes no token/completion budget.
- **Admin/UI:** unit on the cost editor + a Unit column; usage projections expose `billable_units`/`unit_kind`; the startup guard now also reports a unit/route mismatch as MISCONFIGURED.

Deliberate decisions recorded: estimate rows keep the legacy token arithmetic and are only *tagged* (billing a char proxy through a unit rate would silently un-bill every existing voice model); `compute_units` has no fallback-rate path (`cost_fallback_per_m` is per-token — applying it to seconds would fabricate a conversion); no per-image/per-request route wiring yet (no such surface exists).

## 6. Real-Postgres verification (closes audit test-gap #9 for the fresh-DB case)

The suite has no live-Postgres fixture, which is why the `GROUP BY` 500 and the `key_group` boot abort both survived a green run. As a Lead-level verification, `SCHEMA_SQL` was applied **three times in a row to a throwaway Postgres 18 cluster** (initdb, unix socket, no extensions assumed):

```
APPLY #1: OK
APPLY #2: OK
APPLY #3: OK
usage_ledger new cols: ['billable_units', 'unit_kind']
model_costs.unit default: 'token'::text
ck_model_costs_unit convalidated: True
conversations.key_group default=gen_random_uuid() nullable=NO
api_keys.key_group     default=gen_random_uuid() nullable=NO
```

That is execution evidence for: schema idempotency on repeated boot; the P10 unit column + default; the P1 `key_group` DEFAULT-before-NOT-NULL repair on **both** tables; and the absence of any unguarded `ALTER`/`ADD CONSTRAINT` that would re-scan. It does **not** cover the pre-migration-DB-with-a-concurrent-writer case (that was reproduced separately during wave 1 and fixed) — that remains the right thing to encode as a CI test.

## 7. Closing the opens

**`tests/test_postgres_schema.py` now exists** — a throwaway-cluster fixture (initdb + pg_ctl on a unix socket under `tmp_path`, skipped cleanly when the PostgreSQL binaries are absent) with nine tests:

1. `SCHEMA_SQL` applies three times on a fresh cluster; the P10 columns and the `key_group` DEFAULT are asserted present.
2. A **stale out-of-vocabulary `unit` row does not abort the boot**, and the CHECK is asserted `NOT VALID`.
3–7. Every `group_by` mode's **actual generated SQL is prepared against a real server** (captured from the route with a recording seam, then `conn.prepare()`). This is the P1-1 class, executed.
8. `settle()` writes a row with the unit columns, and an out-of-range `billable_units` **does not lose the row**.
9. The propagate-grants CTE is pinned as *correct* so nobody "fixes" the audit's one refuted hypothesis.

**It paid for itself on the first run.** It immediately found a live regression the green suite could not see: the P10 edit had dropped the comma after `AS cost_usd` in the usage rollup's SELECT list, so `GET /admin/usage?group_by=…` was a syntax error — **the very endpoint P1-1 had just repaired**. Fixed; all nine pass.

Also closed in this pass: `anchor_status` is now rendered on the Audit tab (a verify button that treats `anchor-mismatch`/`anchor-invalid`/`trail-wiped` as failures and never polls); the embeddings/rerank token estimators now charge through `context_window.text_tokens` like their chat twin, so non-ASCII cannot under-count on those surfaces; `listing_cache_clear()` bumps a generation so a fetch that started before a registry edit cannot publish its pre-edit snapshot; and DEPLOYMENT.md documents that rotating the master key invalidates the tail anchors until the next chained write.

## 8. Remaining work

1. **`uq_api_keys_one_live_sso` ignores expiry** — an expired `sso-create` key still occupies the one-live slot. **Cannot** be fixed in the index (`now()` is not IMMUTABLE); needs an application-level rotate-before-bind. *Owner: the maintainer (accepted).*
2. A `unit_mismatch` banner in the console (the API already reports it), and per-image/per-request billing once a route exists to bill.
3. Cosmetic: the `housekeeping` module docstring still says prunes "piggyback the registry-refresh hook"; `routes_mcp`'s rate gate passes no `request_id` (harmless today — MCP settles 0 tokens).
