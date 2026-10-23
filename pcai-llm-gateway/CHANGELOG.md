# Changelog

Boundaries between the deployed cuts below are reconstructed best-effort from
the audit reports and the tree (the repo's changelog was frozen at 0.1.16
while three release waves shipped); where a cut boundary was uncertain the
feature is listed under the LATER version.

## 2.9.2

- **TPM rate limiting: cached prompt tokens are now measured BEFORE throttling, not
  refunded after the fact — plus two correctness fixes to the cache refund itself.**
  Reported symptom: a key on a 100k TPM limit was throttled while its upstream was
  serving ~97% of the prompt from prefix cache.

  Three defects, all measured on a live deployment:

  1. **The refund was all-or-nothing.** `_refund_window_tokens` issued one conditional
     `UPDATE ... WHERE tokens >= $3`; when the window held FEWER tokens than the
     reported `cached_tokens` the whole refund was discarded rather than applied in
     part. Because `cached_tokens` is the upstream's REAL prompt size while the
     admission charge is a `len//4` estimate, that mismatch was routine — **56.6% of
     refunded tokens were being thrown away** (measured over 24h). The refund is now
     CLAMPED (`GREATEST(tokens - $3, 0)` inside a `FOR UPDATE` CTE): it gives back
     what the window holds, still never goes negative, and still never mints a window
     row. Verified against real Postgres including 40-way concurrency.

  2. **A refused request kept its token charge.** The admission upsert increments
     unconditionally (it must, to be atomic) BEFORE comparing to the limit, and the
     rejection path settled with `cached_tokens=0`, so nothing refunded it. Every 429
     therefore raised the odds of the next one for the rest of the minute. A refusal
     now releases its own token charge (`_release_rejected_charge`); the REQUESTS
     counter is deliberately NOT released — that is the anti-hammer axis.

  3. **The admission gate could not see the cache at all.** The refund above is
     post-hoc: it cannot influence a decision already made, and a long call settles in
     the window AFTER the one it charged (p99 latency 58s against 60s windows). A
     single 800k-token context charged ~808k at admission — 8x a 100k budget — and was
     refused before the upstream ever reported that ~780k of it was prefix-cached. New
     `rate_limit_predict_cached` makes the admission charge use the key's OWN measured
     cache ratio (token-weighted `cached/prompt` over its recent ledger rows, served by
     `idx_ledger_key_ts`, cached in-process for `rate_limit_predict_ttl_s`).

  The prediction is bounded by settle-time reconciliation (`rate_limit_predict_reconcile`):
  `charged + (true_billable - charged) == true_billable` exactly, so it only decides WHEN
  tokens are charged, never HOW MANY. A caller that busts its own cache (a nonce defeats
  the prefix cache) pays the real cost at settle, bounding the bypass to ONE cheap
  admission per window; a key with NO history is charged the full estimate. The cache
  refund and the reconciliation are mutually exclusive — running both would double-count
  the cache (verified: that form nets `prompt - 2*cached`).

  Defaults: `rate_limit_predict_cached` is **OFF** (it changes admission behaviour, so it
  is opted into per site); `rate_limit_predict_reconcile` is ON. With prediction off, the
  charge is byte-identical to 2.9.1. The G2 site values enable prediction.

  Note: raising a key's TPM limit is still the capacity answer for a workload whose
  request rate x context size exceeds the budget — this release fixes the ACCOUNTING,
  not the size of the budget.
## Unreleased

- **Model-routing console panel: a DB-backed operator layer over the router, ready for a
  decision model (`vllm-sr` / `d3-turbo`).** `core.model_routing` has been heuristic-only
  and env-tuned; there was no way to say "the router may use these models, not those",
  no place to keep the prompt a decision model would receive, and no way to feed live GPU
  demand into that prompt. New `core.routing_config` (mirrors `core.distiller_config`:
  60 s TTL cache, degrade-not-fail, one transaction per write) plus
  `GET/PUT /admin/routing/config` and `POST /admin/routing/prompt-preview`, surfaced as a
  new **Routing** tab. It stores `enabled` (AND-ed with `PCAI_LLM_ROUTING_ENABLED` — the
  env flag owns the mount, the row owns whether routing acts), `allowed_models` (the
  router's own ceiling; empty = every registered chat model), `decision_model`, `prompt`,
  `inject_gpu_load`, `gpu_load_source` and `max_prompt_chars`.

  `allowed_models` here is deliberately NOT a key ACL — a key's `allowed_models` stays a
  hard per-key boundary that no routing decision can widen (`routing__X` still 403s on a
  model the key may not use). The two are validated against the registry and reported
  separately so an operator cannot mistake one for the other.

  **GPU-load injection reads the off-peak evaluator, not a second Prometheus client.**
  `{{gpu_load}}` / `{{gpu_load_json}}` render the per-model utilization and hysteresis
  state `core.offpeak` already maintains for the billing discount — one signal, so the
  prompt and the price can never disagree about how busy the fleet is. A model with no
  evaluator renders `no data` rather than a fabricated number; with `inject_gpu_load` off
  both placeholders render `(gpu load injection is off)`.

  The prompt is a **template, not `str.format`**: no `eval`, no attribute access, and an
  unknown `{{placeholder}}` is left VERBATIM so a typo shows up in the preview instead of
  being silently eaten. `max_prompt_chars` truncates the RENDERED prompt so a large
  snapshot cannot blow the decision model's context. The preview route writes nothing (a
  read, no audit row) so an operator can iterate on a prompt without littering the trail.

  `decision_model` is CONFIGURATION ONLY in this release: stored, validated against the
  registry, rendered into the preview — the heuristics still do the routing. That is the
  point: the allowed-model set, the prompt and the load signal can be reviewed against
  live traffic (the shadow ledger) BEFORE a decision model is allowed to spend.

- **`GET /v1/models` now announces what it hid (`X-PCAI-Models-Denied`), and the admin
  plane gets a registry-truth view of the same surface.** Reported symptom: a key could
  not see `Qwen3.8-27B` in the chat console's dropdown, and nothing anywhere said why —
  "the gateway does not serve that model" and "my key is not allowed to use it" looked
  identical from the client side. The listing now stamps `X-PCAI-Models-Denied: <n>` (the
  count of registered models the key's `allowed_models` filtered out; `0` for an allow-all
  key) and the chat console shows a muted hint under the picker when it is non-zero. The
  JSON body is unchanged, so every existing client is byte-identical.

  New `GET /admin/presented-models` is the operator-side counterpart: everything
  `/v1/models` can present — the `auto` / `auto-demand-aware` aliases with their ordering,
  the `routing` / `routing__<model>` pseudo-models, and every registered pool with its
  healthy/total endpoints, `max_tokens` ceiling, off-peak state, demand-aware exclusion and
  routing eligibility — rendered as a refreshable block on the Models tab. It needs no
  proxy-scoped key, so an operator can inspect the advertised surface without
  impersonating one.

- **A profile's `model_order` doubles as an ACL — so a stale id in it silently deleted a
  model from every enrolled key. Now refused, and reported.** The enrollment path derives
  `allowed_models` from the profile ordering (`[m for m in model_order if m in registered]`),
  which is the right least-privilege default but made a renamed or never-registered model id
  vanish from every key minted from that profile — no error, no warning, just a shorter
  dropdown (the live shape: an ordering naming `Qwen/Qwen3.8-27B-FP8` while the registry
  served `Qwen3.8-27B`). `POST /admin/profiles` now refuses an ordering entry that is not a
  registered model, naming it, and `GET /admin/profiles` additively reports `stale_models`
  (ordering entries with no pool) and `acl_effect` (ordering ∩ registry — what an enrolled
  key will actually be allowed) so the console can show the double duty explicitly.

- **Admin console: tabs grouped into four runs separated by a thin green rule, and a new
  Routing tab.** The flat eleven-button strip became `[Keys, Profiles, Enrollment]`,
  `[Models, MCP]`, `[Usage, Traces, Audit, Guardrails]`, `[Off-peak, Memory Distiller,
  Routing]` — identity/keys, the two catalogs, observability, and the background
  GPU-economy features. Buttons stay `nav button[data-tab=...]`, so every existing
  selector, the boot tab-restore and the lazy per-tab loaders are untouched.

- **Dollar-amount budget presets on profiles: a profile's `budgets` entries may now
  carry `limit_usd` as well as `limit_tokens`, so a key minted from a profile can be
  born with a USD cost budget.** The key-level USD budget already existed
  (`cost_budgets.limit_usd`, `POST /admin/keys/{key_id}/budgets`), but the PROFILE
  preset path was token-only — `POST /admin/profiles` accepted just
  `{model, period, limit_tokens}`, so every key created through a profile (admin
  create with `profile_id`, `/v1/enroll` redeem, SSO self-mint) came out with token
  budgets only and a dollar cap had to be applied per key, after the fact. Each
  entry now carries exactly ONE limit kind (`limit_tokens` XOR `limit_usd`, period
  `day|week|month`, positive) and is routed to the matching table: token presets
  into `budgets`, dollar presets into `cost_budgets`. The enrollment redeem and the
  SSO self-mint stamping write BOTH tables inside the mint transaction, so a
  profile-bound code can no longer half-seed a key; `GET /admin/profiles`
  round-trips both kinds. Enforcement is unchanged and shared with the key-level
  budgets — whichever limit hits first stops the key, the USD gate is post-paid
  (`spent >= limit`; an in-flight request's true cost is known only after the
  response, so overshoot is bounded by one response) while token budgets gate
  pre-call on the admission estimate.

  **Caveat, and it bites: a dollar budget is INERT for an unpriced model.** The USD
  spend sum is `SUM(usage_ledger.cost_usd * billing_fraction)`, and `cost_usd` is
  NULL for a model with no `model_costs` price row (Costs tab /
  `POST /admin/costs`) — NULL contributes nothing to the SUM, so the budget sits at
  $0.0000 spent forever and never refuses. Price the model first; a profile that
  presets a dollar budget for a model nobody priced is a preset that silently does
  nothing.

- **Minimum-length floor on a distilled memory body (`distiller_min_doc_chars`,
  default 0 = off) — an optional second net, not the primary defence.**
  Proposed as "require ~250 chars before writing to RAG". Measured on the live
  store at **MEMORY level — 600 memories reconstructed from 1,723 stored chunk
  documents** (mm-rag splits one memory across several chunks, so chunk counts
  are NOT document counts): a floor of 150 / 200 / 250 drops **2 memories
  (0.3%)**, 300 drops 3, 500 drops 6. Both of the two under 250 were title
  artifacts ("Agent Swarm Short-Term Memory"; "Título de sesión de pruebas
  unitarias ST-MEM" with a header bolted on), so **a 250 floor would have been
  safe here** — it would have removed junk and nothing else.

  Correction worth recording: an earlier revision of this entry claimed a 250
  floor would "delete 58 real fact-cards", and cited a 220-char "SQLhandler vs
  EzPresto …" card as a genuine short memory at risk. **Both claims were wrong.**
  The 58 came from counting 220-char CHUNK FRAGMENTS as complete short cards, and
  the EzPresto card was exactly such a fragment — the opening slice of a 2,221-char
  memory, ending on a dangling colon. It reads like a title because it is a
  truncated lead-in. The real defect that produced it is mm-rag's chunker emitting
  the opening slice as a separate document (55 such redundant lead-in chunks in
  the corpus), which no length floor touches.

  The default therefore stays **0**, but for the honest reason: the floor is
  nearly REDUNDANT with the title guard below — the only two memories it would
  catch are the ones that guard now prevents at capture time. 150 is the safe
  value if a second net is wanted. The refusal returns BEFORE the raw-transcript
  fallback (a stub is not a model failure; degrading to the raw transcript would
  trade one short doc for 100+ chunks) and does not mark the row distilled, so it
  stays due and retries.

- **Session-title requests are no longer captured or distilled into memory
  (2026-10-17): DSH fires an auxiliary "session title" model call after EVERY
  human prompt, and the gateway treated each one as a conversation.** Each title
  call is an ordinary `POST /v1/chat/completions` whose body is a single user
  message framing the conversation as JSON ("Generate the session title from this
  JSON array of human messages: …" / "Update the session title from this JSON
  object: …"). The canonical capture wrote a row for every one; the distill sweep
  then turned each row into a bogus "session summary" in the user's memory
  dataset — whole bodies like "Deepseek calls misrouted to GLM", or "Session
  consisted solely of a title-generation request". One junk memory per prompt,
  plus the model spend to distill it, in a store whose contract is a project map.
  New `core/title_requests.py` owns the detection with TWO independent signals:
  the `X-PCAI-Purpose: session-title` header (authoritative; the harness LLM core
  models this as `GenerateOptions.purpose`, but the pi-ai adapter does not yet
  map it onto the wire, so the header path ships forward-compatible), and the
  body fingerprint (the signal that fires today). **False-positive safety is
  structural, not textual**: a framing match alone is never enough — the request
  must also be exactly ONE user message whose post-framing tail parses as the
  title provider's JSON payload, so a user who PASTES a framing into a real chat
  keeps their conversation in memory. Enforced at every store that feeds a
  distill path: both capture sites in `routes_proxy` (plain-chat capture and the
  relay reconcile — DSH's title calls engage the relay, so one guard alone would
  have left the leak open) and `distill_schedule._exclude_title_rows` for rows
  ALREADY stored, which are dropped from the due set without being marked
  distilled (a narrowed detector re-eligibilizes them rather than burying them).
  The due query over-fetches (4x, capped at 400) before the Python filter, so
  discarded rows cannot starve newer eligible ones — the head-of-line starvation
  bug this module documents twice. The `conversations` due path deliberately gets
  NO guard: title calls never write that store, and a comment says so. Knob:
  `canonical_skip_title_requests` (default true; false restores pre-fix
  behavior). 23 new tests pin both signals, the malformed-body tolerance, and the
  false-positive guards (ordinary conversation, pasted framing, framing in a
  later message, framing plus an assistant turn). Suite 1493 passed; ruff + mypy
  clean.

- **Distiller feed-stall hotfix (2026-10-14 pulse-check): the automatic memory feed
  stopped cluster-wide because the v2.5.0 image shipped a SELECT for a column no
  migration created.** `due_canonical_conversations` reads and
  `mark_canonical_distilled` writes `canonical_distills.distilled_content_hash`
  (the unchanged-content-skip marker, 2026-10-11), but `schema.py` never added the
  column — so the due query raised `column d.distilled_content_hash does not exist`
  on EVERY sweep, and because the handler swallowed it at `log.debug` the sweep
  looked healthy while returning an empty due set. The only trace was the Postgres
  error log. Live: ~2h of zero writes, 41+ identical PG errors, and the memory
  dataset flat at 802 documents. Two sibling defects in the same build, both found
  while validating the fix: (1) `_claim_canonical_rows` stamped
  `-infinity()::timestamptz` — `infinity` is a Postgres LITERAL, not a function,
  and `::` binds tighter than unary minus, so the whole claim INSERT raised; its
  fail-open handler returned every row, silently disabling the cross-replica lease
  and letting two pods distill the same thread ~0.5s apart (live duplicate pairs,
  sessions ccdaeaaea9ede679 / 35959da49f2a2dbd / 064c4d6c18d21251); (2)
  `mark_canonical_distilled`'s None branch named 5 target columns and supplied 4
  values. Fixes: the missing migration, `'-infinity'::timestamptz`, the value-list
  arity, and — the meta-fix — the two due-sweep handlers raised from `log.debug`
  to `log.warning`, since the DEBUG swallow is what made a total outage invisible.
  Five STATIC regression tests pin the bug class (statement text vs SCHEMA_SQL
  agreement, valid infinity literal, non-silent claim failure, INSERT arity via a
  balanced-paren parser, column declaration); the arity test was caught VACUOUS by
  adversarial review and rewritten until a reintroduced bug makes it fail. Verified
  live: column present, sweep writing again (3-11 distills/min), `andrew-memory`
  802 -> 932+ documents. NOTE: the schema heal was applied to the running DB out of
  band; the code fixes still require a v2.5.1 image cut + rollout.

- **Cache-discounted period token budgets (budget_cached_exempt_fraction, default 0.9): upstream prompt-cache hits count at one-tenth weight against day/week/month token budgets.** The same problem the TPM refund fixes, on the slower axis: the ledger-derived token spend summed FULL total_tokens, so a ~500k-token context re-sent every turn burned its (mostly cache-served) prompt against the period cap and hard-limited the conversation's length no matter how large the budget. The token-budget SUM now discounts the row's cached_tokens (billable = (total - cached*frac) * billing_fraction, GREATEST/LEAST-guarded against poisoned rows); the ADMISSION estimate is discounted by the same factor (the settle brings ledger truth either way, bounding the residual overshoot by the concurrent in-flight requests' remainder — the same trade the post-paid USD gate accepts). The USD-budget branch is byte-identical: costs.compute already prices cached tokens at cached_per_m, and discounting them in the gate too would double-count the exemption. TRUE tokens stay on the ledger row and in the usage-summary rollups; only budget CONSUMPTION changes. fraction: 0.9 = one-tenth weight (default) · 1.0 = fully exempt · 0.0 = legacy full count (also restores the byte-identical legacy SUM shape the shape-asserting tests pin).

- **Admin console: the session-memory distiller's summarizer model is now a dropdown of the registered router pools** (the same /admin/models source the Models tab renders), replacing freeform text — a typo used to ship a model name the distill engine only refused at distill time (DistillUnavailable on the sweep). The empty option keeps the "(calling key's own routing)" semantics (null clears the override back to the env default, save body unchanged); loadModels() re-paints the select so pools registered after the panel first painted still appear; and a configured model that is NOT currently registered still renders, marked "(not registered)" — saving the panel never silently rewrites an override the operator set.

- **Cache-aware TPM (rate_limit_exclude_cached_tokens, default ON): upstream prompt-cache hits no longer consume the key's tokens-per-minute window.** The admission gate charges the len//4 estimate — it cannot know how much of the prompt the upstream prefix cache (vLLM/SGLang prompt_tokens_details.cached_tokens) served — so a high-cache-hit deployment (a ~500k-token context re-sent every turn) 429'd instantly despite the cache doing the prefill. At settle time the real usage is known: the gateway now refunds the reported cached_tokens from the key's CURRENT minute window (conditional decrement clamped at 0 — never a UPSERT, so a refund can never mint window tokens or credit a future window, keeping the denial-of-wallet fix intact). Window-rollover between admission and settle drops the refund (it ages out with the pruned window) and is counted separately on the new gateway_tpm_cache_refund_tokens_total{outcome=refunded|discarded} counter so refunds+discards reconcile against the ledger's cached_tokens column. status='cache_hit' rows (the embeddings cache) are EXCLUDED: that path settles a hit without ever running the admission gate, so there is no charge to refund — crediting one would be a rate-limit bypass. Kill switch: PCAI_LLM_RATE_LIMIT_EXCLUDE_CACHED_TOKENS=false restores the legacy byte-identical settle. With the feature on, a 1M TPM limit is genuinely attainable for cached-heavy agent loops (1M of BILLABLE tokens per minute, not 1M counted-with-cache). Period token budgets are deliberately UNCHANGED — they bill the full prompt_tokens by design (cache hits still cost the tenant real context).

- **Distill extractor hardening (pulse-check 2026-10-11): the automatic memory feed
  was silently starving** - relay-captured canonical rows whose messages carry
  Responses-native part types (input_text/output_text) or reasoning summary lists
  collapsed to the bare 17-char transcript header under the old type=="text"-only
  filter, and every such row deferred forever (bounded backoff, no marker - the
  2026-10-10 fix kept the row alive but nothing ever rendered). build_transcript
  now renders ANY content block carrying a str text field (plus reasoning summary
  lists), so part-type vocabulary drift cannot strand a row again. A shape
  diagnostic logs roles/content-types (no bytes) if a collapse EVER fires again.
  Live confirmation: sweep attempted 3 rows, all collapsed, 0 memories written in
  ~30h despite active conversations.

- **`POST /v1/responses` now answers a deliberate `501` (`responses_api_unsupported`)**
  instead of a bare 404: the OpenAI Responses API is not implemented, and the two
  blockers are documented in the refusal itself (SGLang upstream gaps — array-form
  input validation (sglang#17853) and custom function tools (sglang#13292) — plus the
  gateway's chat-completions-shaped relay machinery). An Agents-SDK client gets a
  machine-readable pointer to `/v1/chat/completions`; the refusal fires before auth so
  the shape is identical for every client. Responses facade = planned translation
  project.

- **Distiller reliability knobs (2026-10-10): `distillerGateBackoffS` (skip
  backoff) and `distillerRawFallback` (raw-transcript fallback on a failed
  distill)** — the stall fix + the recall fix. Skip backoff (default 900s):
  a row the engine skips (memory gate down, transcript collapsed) leaves the
  oldest-first due window for this long before retry, so a broken memory
  dependency can no longer pin the window and starve newer rows — NOT a
  distilled marker, the row returns to the due set when the window lapses.
  Raw fallback (default `true`, the historical DSH contract) can be turned
  OFF: mm-rag chunks a raw transcript (measured: 126 chunks from one
  session), so one failed distill could land 100+ transcript fragments that
  dilute recall; with it `false` the row stays due and retries — nothing is
  lost (the conversation remains in `canonical_conversations`) and the
  memory store only ever receives distilled summaries. Chart values
  `settings.distillerGateBackoffS` / `settings.distillerRawFallback`
  (→ `PCAI_LLM_DISTILLER_GATE_BACKOFF_S` / `PCAI_LLM_DISTILLER_RAW_FALLBACK`);
  the shipped G2 example sets `distillerRawFallback: false`.
- **Distilled-only canonical retention — `retentionCanonicalDays`**
  (2026-10-10): the third retention knob, for `canonical_conversations` —
  the distiller's INPUT store (full message history as JSONB; the biggest
  table on a busy gateway, pruned by nothing else). DISTILLED-ONLY: a row is
  deleted only once a `canonical_distills` marker proves a memory was
  written for it, so un-distilled conversations are never lost. Default 0 =
  disabled, the same opt-in contract as `retentionTraceDays` /
  `retentionConversationDays` (the usage LEDGER is never pruned either way).
  Chart value `settings.retentionCanonicalDays`
  (→ `PCAI_LLM_RETENTION_CANONICAL_DAYS`); documented in DEPLOYMENT.md and
  mirrored in both shipped example values with a keep-forever default.

- **SSO self-mint least-privilege enforced — `toolRelayGrantMode` defaults to
  `explicit`, the self-service panel shows only the profile's envelope**
  (2026-10-09, the SSO poweruser leak): an SSO-self-minted key (scopes fixed
  `proxy+mcp`) could call — and self-grant from the homepage panel — EVERY
  policy-admitted tool of every enabled server (applygate, k8s-ops…), because
  the default `scope` grant mode re-adds the whole admitted catalog over the
  profile's grant presets at call time, and the panel folded that same union
  into its self-grant entitlement. Three-sided fix: (1) the shipped default
  grant mode is now `explicit` — a key's callable set is exactly its held
  pairs (profile live-grant presets ∪ own ACL rows, `federation.load_grants`;
  deny rows still win), so the profile IS the envelope and a withheld server is
  invisible and uncallable no matter what scopes the key carries (`scope`
  remains opt-in legacy for deployments that grant mcp scope deliberately —
  its panel rendering stays truthful, marked `via_scope`); (2) the MCP
  self-service panel lists ONLY entitled pairs and OMITS a whole server the
  key holds nothing on — withheld servers no longer appear, even disabled;
  (3) the self-grant entitlement is the HELD set only under every mode — a
  toggle outside it 403s. Regression tests pin the omission, the default, and
  the held-only toggle; all five helm values files + DEPLOYMENT.md updated.

- **Session memory distiller — the harness-independent memory pipeline**
  (2026-10-09): one canonical transcript→memory pipeline in the gateway
  replaces the three per-harness distillers (DSH session-memory-distiller
  plugin, opencode session-remember agent, OWUI filter distill step).
  `POST /v1/distill` (+ `/v1/distill/conversations/{id}`) with the DSH
  plugin's prompt and map-reduce ported verbatim; attachment-gated inert
  no-op for keys without the memory pair; per-key ledger settle + trace;
  housekeeping-scheduled re-distill on conversation continuation
  (off-peak-only scheduling admin-configurable via the Memory Distiller
  panel and `GET/PUT /admin/distiller/config`); harness shims shrink to a
  single unconditional call.
- **Soft guardrail blocks — `reply_text` turns a pre_call block into a
  normal assistant turn** (2026-10-09): agent clients that treat a 400 as
  fatal (DSH killed the whole thread on the first politics question) now
  have a non-fatal option. A pre_call guardrail whose config carries a
  non-empty `reply_text` (any engine; admin-validated as a string; absent/
  empty = the documented hard 400) returns the notice as a NORMAL 200 chat
  completion — `chatcmpl-guarded-<request_id>`, `finish_reason: "stop"`,
  zero usage; `stream:true` gets a single-shot SSE answer — plus an
  `X-PCAI-Guardrail-Blocked: <name>` response header for clients that
  want to machine-detect the block and purge the offending turn from
  their history. The ledger row is byte-identical to the hard path
  (`status='guardrail_blocked'`, verdict event, zero tokens) — only the
  CLIENT surface changed, so API consumers relying on the 400 contract
  are unaffected unless an admin opts a guardrail in. Note the gateway
  cannot purge for the client (stateless — the blocked text rides back in
  `messages` next turn): without header handling the notice re-fires
  every turn. Tests: proxy wiring (JSON + SSE, ledger parity) in
  test_guardrails_proxy.py, exception carriage in test_guardrails_core.py,
  admin validation in test_guardrails_admin.py.

- **Budget spend survives key rotation — the rotation budget-reset bypass
  closed** (2026-10-09): period budgets are DERIVED (SUM over
  `usage_ledger` keyed by `key_id`), so every rotate minted a fresh key_id
  with zero ledger rows and reset the in-window counter to 0% — and since
  SSO self-mint hands rotation to the USER (default-eligible,
  unlimited-successive-rotates), a budgeted user could rotate at 99% of a
  monthly cap into a fresh full cap, same calendar window, indefinitely.
  Fix: `api_keys.spend_ancestors UUID[]` — every rotate (admin and SSO
  self-mint) sets the new key's lineage to `old.spend_ancestors ||
  [old.id]`, and both budget-gate SUMs and the console's budget_status
  join ledger rows over `key_id = ANY(spend_ancestors || id)`. Window
  predicates unchanged: the in-window COUNTER carries, calendars still
  reset normally. Rotation also carries the budget LIMIT rows server-side
  for the first time (token + USD; `budgets_copied` in the rotate
  response/audit) — before this a raw-API rotate left the new key with NO
  budget rows (unlimited) and the console's client-side re-apply was the
  only thing restoring limits; that re-POST stays as an idempotent no-op.
  Fresh mints still start at zero (correct); RPM/TPM still reset (1-minute
  burst windows — carrying them is complexity for ~zero gain). Tests:
  lineage accumulation + budget-limit carry pinned in test_admin.py and
  test_sso_selfmint.py.
- **SSO self-mint default profile — per tenant, admin-configurable from
  the console** (the mint's grants stop being hardcoded): new
  `tenants.sso_default_profile_id` (NULL = the bare fixed mint grant,
  byte-identical to pre-feature). Admin configures it in the console's
  **Enroll tab** ("SSO self-mint default profile" card: tenant + profile
  dropdown + Save → `PUT /admin/tenants/{id}/sso-default-profile`) or via
  the API. The mint AND rotate stamp the profile's budgets, MCP grants,
  and limit presets onto the fresh key (rotate re-stamps — a rotated key
  is a new row; without this, grants would die at first rotation) while
  scopes stay the fixed least-privilege grant. A deleted profile degrades
  to the bare grant (FK ON DELETE SET NULL); the fresh-mint bind moved
  into the mint transaction so binding + stamping commit as one. 4 new
  tests in test_admin_selfmint.py (29 total passing).
- **Identity probe: subject-spelling parity with the mint on a chained
  edge** (G2, 2026-10-08): behind the SSO edge the proxy injects BOTH a
  Bearer id_token AND the browser headers; the probe let the token rung
  outrank the browser rung and reported the IdP's ``sub`` (a UUID on G2)
  while the MINT binds the header subject
  (X-Auth-Request-Preferred-Username) — the chip showed
  `1d2fee26-…` where the minted key anchors `andrew-bydlon`, and
  pre-mint eligibility ran against an anchor no key would bind. The
  probe now prefers the browser-header rung whenever the request carries
  header identity under trust (the exact shape the mint trusts); the
  token rung stays authoritative with headers absent or trust off (pure
  token/shadow-key callers unchanged; attribution in auth.py/X-MCP-Caller
  untouched — reporting only). 3 new tests in test_identity_probe.py pin
  headers+token → header spelling, token-only → sub, trust-off → sub.
- **First-boot PG-connect race fixed two ways** (seen on every G2 rollout
  since the namespace went meshed: one `ConnectionRefusedError` crash +
  kubelet restart per pod, always self-healing, always on first boot):
  (1) chart — new `deployment.holdApplicationUntilProxyStarts` (default
  true) renders the Istio pod annotation
  `proxy.istio.io/config: {"holdApplicationUntilProxyStarts": true}`,
  holding the app until the sidecar finishes starting (a no-op on
  unmeshed namespaces); (2) app — `db._make_pool` absorbs transient
  `OSError`/`asyncpg.PostgresError` during pool creation with a bounded
  3-attempt / short-backoff hold before the (unchanged) fail-loud exit —
  a wrong DSN still crashes fast, the boot race just no longer counts as
  one. Tests: tests/test_db_pool_retry.py (absorbed refusal, bounded
  fail-loud, non-transient error class).
- **Identity probe: `flags.self_mint` now mirrors the mint's tenant
  precondition** (found live on G2 2026-10-08): `POST /v1/keys/for-me`
  503s when `PCAI_LLM_TOKEN_SHADOW_TENANT_ID` is unset/malformed (a key
  needs a tenancy holder; the mint refuses to invent a tenant), but the
  probe's `self_mint` ignored that term — so the SPA showed the
  "Sign in with SSO" card where the click 503'd, and its non-201/409
  handler hid the card silently (by design, no banner): the user watched
  the mint affordance vanish on click. The probe now parses the tenant
  exactly as the mint does (empty/non-UUID ⇒ not mintable); 3 new tests
  in tests/test_identity_probe.py pin no-tenant, bad-tenant, and the
  not-over-blocked configured case. Deployment posture unchanged: a
  correctly configured tenant keeps `self_mint: true`.
- **Runbook/values corrected: `tokenShadowTenantId` is REQUIRED for the
  mint flow** — the runbook's step-4 block and `helm/local/values.g2.yaml`
  previously described it as optional ("needed only for token-only
  callers"); that was true only for shadow keys, not the browser mint.
  The failure signature (chip green, card present, vanishes on click)
  is documented in SSO-ROLLOUT-G2.md step 4.

## 2.1.1 — 2026-10-07

- **Chat page: a failed SSO mint now clears the boot hint's SSO clause** —
  the 403/401-mid-mint path hid the "Sign in with SSO" card but left the
  boot hint naming the vanished button (the disclosure invariant's second
  enforcement site, now pinned in tests/test_ui.py). Verified live in the
  served v2.1.1 UI.
- **Chart: AuthorizationPolicy relief rules rebuilt from the G2 rollout's
  live findings (2026-10-07)** — two rules the namespace-scoped design
  could not express, both verified live: (1) `allowMetricsScrape` is now
  an ANY-SOURCE rule scoped to `/metrics` + `/metrics/` on the service
  port — kube-prometheus scrapes raw POD IPs, and raw-IP outbound is
  envoy passthrough (PLAINTEXT, no peer identity) even from a MESHED
  scraper (prometheus-0 was given a sidecar and `up` stayed 0), so a
  `namespaces: […]` matcher can never admit it; (2) new
  `allowInClusterApi` (default false, SITE call) admits ANY source to the
  traffic port EXCEPT the identity-bearing paths — in-cluster direct
  consumers (DSH, OpenWebUI, opencode-web, the `clearwing` agent) were
  403-denied by the first policy render. The identity paths
  (`/v1/identity`, `/v1/keys/for-me` — the only routes whose resolver
  consumes the browser-header rung) stay reachable ONLY from the edge
  namespace, which is the policy's actual job: with `trustProxyIdentity`
  on, an in-cluster pod must never deliver spoofed `X-Auth-Request-*`
  headers to a mint. ⚠️ These chart changes are UNCOMMITTED and already
  LIVE on G2 (release revision 4 was deployed from the working tree) —
  commit before any clean-tree re-deploy or the rules silently revert.

## 2.1.0 — 2026-10-07

- **SSO self-mint (the MM RAG D25 pattern, complete in the gateway)** —
  `GET /v1/identity` (public whoami probe: exact 6-key anonymous anti-oracle
  shape, 503 fail-closed), `GET/POST /v1/keys/for-me` (one key per browser
  identity, 409 unless `{"rotate": true}`, crash-safe rotate = create+revoke
  in ONE transaction reusing the subject's user row), the `X-API-Key`
  envelope (D19 de-escalation precedence: a valid envelope outranks the
  co-forwarded proxy Bearer; a dead envelope is never rescued by it; REST
  and `/mcp` share ONE resolver so they cannot drift), admin-governed
  eligibility (`users.self_mint_enabled` + `GET/PUT
  /admin/keys/self-mint-eligibility`, default-eligible first mint), the
  chat page's "Sign in with SSO" card (mint/rotate/reveal-once) and the
  MM RAG-style identity chip, D24-complete sign-out, browser rung reading
  the platform proxy's actual header spellings
  (`X-Auth-Request-Preferred-Username`/`-Groups`). 145 new tests.
  INERT until the coupled rollout in DEPLOYMENT.md ("SSO self-mint
  rollout") lands: edge chain + sidecar/policy + the two flags —
  flipping flags before the chain enforces trusts spoofable headers.
- **Chat page: the empty header pill fixed** — the `#budget-strip` button
  carried an INLINE `display:flex`, which overrides the `hidden` attribute's
  UA `[hidden]{display:none}`, so the strip rendered as an EMPTY pill
  between "usage" and the connection status until its first content (the
  ghost element in the 2026-10-07 screenshot). The flex layout moved into
  the `#budget-strip` CSS rule behind an explicit `#budget-strip[hidden]`
  guard; audited both UIs for the same conflict class (zero remain).
- **Chat page: the identity chip is identity disclosure, not a mint
  affordance** — the green "Signed in as <subject> · oidc" chip now renders
  whenever the boot probe resolves a subject, regardless of
  `flags.self_mint` (the MM RAG banner parity: you can SEE you're on SSO
  before you can MINT); `authenticated` no longer gates it (behind the edge
  chain a credential-less probe resolves the subject before any key
  exists). Only the mint card and the boot hint's SSO clause track
  `flags.self_mint` — the hint never names an affordance that isn't there.

## 2.0.2 — 2026-10-04

- **Per-model `max_tokens` ceiling (`PCAI_LLM_MAX_TOKENS_CEILING`)** — the
  gateway never forwards a `max_tokens` a model cannot satisfy. Live defect:
  the `auto` entry sends 262144 while Qwen3.8-27B's context is 262144, so
  `max_tokens + prompt > context` was rejected with an API-level 400 BEFORE
  sampling (~86% of Qwen calls failing; the load-aware head kept choosing it).
  A JSON map (virtual model -> ceiling) silently LOWERS a client's larger
  value on every upstream path — non-streaming, streaming, and the relay
  WIRE body (the relay's `max_tokens=` param is not the body that 400s) —
  and never raises a smaller one. Empty map = no cap, byte-identical render.
  G2 sets `Qwen3.8-27B: 196608` (192k leaves prompt headroom).

- **Load-aware head is now its own alias: `auto-demand-aware`** — the
  feature previously engaged on EVERY alias, silently changing what the
  existing `auto` meant for every client. Now: `auto` is the legacy pure
  profile-order cascade (byte-identical to pre-feature: no sticky read/write,
  no pool lookup, no counter tick) and `auto-demand-aware` is the SAME walk
  with the off-peak load pick + per-conversation stickiness engaged. Both are
  advertised in `/v1/models` under the identical condition, `auto` first and
  unchanged in shape; the new entry carries `pcai.demand_aware: true`.
  Opt-in per client, and rollback is a name switch with no redeploy.

- **Health gate on the load-aware ranking** — idle-because-BROKEN no longer
  ranks best. The head now ranks only candidates whose pool has an available
  endpoint (`Pool.healthy()`, the same predicate `_routing_available` and
  `_next_fallback_candidate` use), demotes unhealthy ones behind the healthy
  (never dropping them — the budget walk may still legitimately use them),
  drops+re-picks a sticky head that goes unhealthy, and leaves the candidate
  list untouched when NOTHING is healthy. Live shape it fixes: Qwen idle at
  0.00% GPU util while 400-ing — every new conversation headed to it.
  Metric vocabulary gains `unhealthy` for the exclusion.

## 2.0.0 — 2026-10-04

- **Console: pricing table stops printing 8-decimal ledger artifacts ("can we
  not do 6+ float decimals for cost :D")** — `model_costs` columns are
  `NUMERIC(12,8)` and asyncpg crosses them over as Decimal strings, so the
  Usage tab's pricing table rendered raw wire values: `0.30000000`,
  `1.20000000`, and Postgres zero as scientific `0E-8` (the unpriced-embedder
  row). The three price cells now go through a `fmtPrice` helper (beside
  `fmtUSD`/`fmtInt`, same Decimal-string gotcha documented): numbers render
  trimmed — `0.3`, `1.2`, `0.006`, `0.05`, `0` — and a hypothetical
  sub-nano price falls back to 8dp trim instead of scientific notation.
  Display-only; stored precision, the `0.000001` editor step, and the
  Decimal-settle math are untouched.
- **Console: per-model fallback chain editor (Models tab → `fallbacks`)** —
  the admin-curated failover chains (core.model_fallbacks, `PUT
  /admin/models/{name}/fallbacks`) were API-only; the console now opens a
  dialog per model: checkbox list of the other registered models (rank order
  = DOM order), a free-text escape hatch for names outside the snapshot, the
  current chain echoed as `a → b` walk-order text, and the same replace-all
  PUT the API enforces (server-side validation: registration, self-fallback
  refusal, dedupe, `PCAI_LLM_MODEL_FALLBACK_MAX` cap). Also fixes a latent
  styling bug the dialog exposed: `button.primary` (the edit-key dialog's
  save button, since the key-UX wave) had no CSS and rendered as a default
  gray browser button — it now matches `button.btn`.
- **Console polish: the Models/MCP registration forms stop clipping off the
  window (the 2026-10-02 "unprofessional" report)** — the endpoint-pool form
  sized its five inputs in fixed px inside a plain flex row and packed the
  field docs into one dense paragraph, so the card overflowed the viewport and
  the help text ran past the right edge mid-word. The endpoint row is now a
  bordered CSS-grid card (URL gets the room; API key / Secret / Priority /
  Weight / remove-button keep their own lanes; `minmax()` columns shrink
  safely and wrap to two columns under 760px), the remove button carries a
  real title, added rows render identically to the seeded one (same
  placeholder, same grid), and the dense prose became a structured
  `.form-hint` list (one bullet per concept, dashed-rule separated). The MCP
  registration form gets the same hint treatment and loses its fixed widths;
  `.card { overflow-x: auto }` backstops the remaining legacy fixed-px forms
  so no card can push the page wider than the window again.
- **Relay cycle breaks: per-cycle Think panels, per-call tool cards, no more
  stale answer text (DSH 2026-10-02 report)** — on a relayed multi-cycle turn
  the entire agentic loop streamed into ONE assistant message with a single
  `reasoning_content` accumulator, so DSH rendered one ever-growing Think
  panel at the top of the message (tool tombstones + every cycle's thinking
  welded together) while the already-final intermediate narration below it sat
  stale until the real answer arrived, and each executed tool call appeared
  only as a 200/300-char-capped text tombstone inside that thinking stream.
  Three coordinated changes, all gated behind the existing Wave R1 opt-ins
  (`X-PCAI-Relay-Deltas: 1` + `X-PCAI-Stream-Events`; clients without them
  get byte-for-byte today's stream):
  - **Gateway** (`toolrelay`): the `[tool] …` reasoning tombstones are
    suppressed when the client renders stamped inert deltas — a rendering
    client already receives the call as a native card plus the
    `tool_call`/`tool_error` event payload (full args, ≤4000-char preview),
    and the duplicated tombstone text was exactly what glued the tool section
    into the following cycle's thinking block. New `_reasoning_trace()` helper
    returns `b""` when `render_deltas` is on; the four tombstone call sites
    route through it. Clients without the deltas keep the tombstone (it is
    their only view of the call).
  - **pi-ai provider** (`relay-visibility-r2` dist edits): on a `pcai.thinking`
    event whose `iteration` increases, the open thinking/text blocks are
    finished mid-stream and their accumulators cleared — the next cycle's
    reasoning/content opens FRESH blocks at its true stream position, giving
    DSH its natural Think → text → tool → new Think rhythm inside one message.
    Relay tool telemetry now also finalizes the display-only call block at its
    stream position; a finalize-once guard keeps the end-of-stream
    `finishBlock` loop from duplicating the card.
  - **Harness adapter** (`relay-visibility-r2` patch): one synthetic reasoning
    tombstone PER relay call — opened and closed at the call's stream position,
    carrying the full streamed arguments (≤2000 chars) and the gateway's result
    preview (≤4000 chars) instead of r2's single ever-growing 200/300-capped
    block. Suppression semantics unchanged: relay calls NEVER reach the
    harness as tool-call blocks (the agent loop executes every tool-call block
    in the assembled message; a relay call has no client-side result).
  - Tests: gateway `tests/test_toolrelay_r1.py` still green (5/5); harness
    `packages/llm/llm-pi-ai/tests/stream-relay.spec.ts` rewritten for r3
    (12 tests, green) plus the package's offline suites (130 green). The pi-ai
    patch file regenerates from true-pristine and applies clean (`--fuzz=0`,
    byte-identical output); `apply-lib-edits.mjs`/`apply-dist-edits.mjs`
    reproduce the deployed dist/lib byte-for-byte.
- **Console: the key MCP-grants dialog is now a per-collection checkbox tree
  (the keys ▸ MCP view)** — the old dialog loaded ONE server's tools at a
  time behind a dropdown, while the save endpoint
  (`PUT /admin/keys/{id}/mcp-grants`) is REPLACE-ALL across every server:
  collecting only the displayed server's checkboxes meant saving after
  editing one collection silently wiped the key's grants on every other
  collection (a real footgun for multi-collection keys — the 2026-10-01
  jeff-admin applygate-off edit exposed it; that one went through the API
  with a jq filter instead). The dialog now reuses the Profiles-tab
  components (`fetchGrantCatalogs` + `renderGrantTree`): one bordered card
  per enabled server — the OUTER NEST is the source collection — with a
  `[✓] server (all tools)` select-all (indeterminate mid-state, also applied
  on initial render), per-tool checkboxes, live `n/m tools` counters, a
  **`✕ disable all`** button that unticks the whole collection in one step
  (both header controls operate on the entire collection at once, each in a
  single press), and collapsible per-server tool lists, all inside one scroll
  pane. The tree renders the UNION of live catalogs and collections the key
  already holds grants on: a collection whose `list_tools` failed (upstream
  down / timeout) or that was disabled in the registry after grants were
  stamped still lists its held tools (flagged "not offered live") with a
  working disable-all — turning a collection off for a key never depends on
  the upstream answering, and held pairs are preserved on save unless
  explicitly unticked. The pill row under the tree mirrors the live pair set.
  Disable one collection for one key = press that card's `✕ disable all`,
  Save. Close-without-save restores the loaded pill set (no PUT ever happens
  off Save).
- **Tool relay: server-executed tool calls become visible to rendering clients
  (Wave R1)** — closes the "invisible tool call" class of reports (a DSH turn
  showed only `[tool] mm-rag__search_dataset → ok (2450ms)` inside the model's
  reasoning, with no call, no output, and inconsistent cards-vs-tombstones
  across reruns). Three coordinated changes, all backward-compatible:
  - **Deterministic twin shadowing** (routes_proxy): a failed relay-catalog
    fetch used to `continue`, silently shrinking the offered toolset for one
    request — a shadowed client MCP twin then un-shadowed (handback → client
    card) on that request and re-shadowed on the next, flipping whole
    conversations between execution modes run to run. Failed first fetches are
    now negative-cached for the catalog TTL, so the offer is stable within the
    window and refreshes retry normally.
  - **Enriched reasoning tombstones** (toolrelay): the `[tool] <server>__<tool>`
    reasoning trace now carries the (collapsed, ≤200-char) call arguments and a
    whitespace-collapsed, ≤300-char result preview (`↳` line) for every
    in-cluster execution — success, error, and ACL-denial alike. The
    `[tool] <name> →` prefix tests pattern-match is preserved; the telemetry
    `pcai` tool_call/tool_error events gained a `id` field (call id) so
    rendering clients can join deltas to previews.
  - **Stamped inert render deltas** (opt-in, default OFF): new settings
    `tool_relay_render_deltas` (false) and `tool_relay_render_index_base`
    (1000). When enabled AND the client sends `X-PCAI-Relay-Deltas: 1`, the
    relay re-emits each in-cluster tool call as a native OpenAI tool_call
    delta stamped `pcai.relay: true` — display contract: clients that
    understand the stamp render it as a completed card and never execute it;
    the delta index space starts at the base so a stamp-blind client can never
    merge rendered calls with handback deltas. Distinct from
    `relay_stream_wire_tools` (which stays OFF — the 2026-09-21 double-
    execution lesson): these deltas are gated per-request on the header, so an
    unknowing client never receives them. The DSH-side consumer lives in the
    deepseek-harness repo (patched pi-ai provider + adapter; kit:
    relay-visibility-r2/, applies via apply-dsh.sh).
  - Tests: `tests/test_toolrelay_r1.py` (5 new: default-off byte-identity,
    stamped emission, handback exclusion, tombstone args+preview, denial id);
    full suite green (842 passed).
- **Admin console Usage tab: chargeback totals group with commas + page-wide
  ↻ refresh at the top**: the Totals row rendered bare digit strings
  (4427047307) while Billed tokens grouped — SUM(bigint) yields Postgres
  `numeric`, asyncpg decodes that as `Decimal`, and the JSON layer
  stringifies it, so the browser's `String.toLocaleString()` was a silent
  no-op. The `/admin/usage/summary` queries now cast the token sums
  `::bigint` (real JSON numbers on the wire, exact far past any plausible
  token count), and every integer cell on the tab (requests, cache hits,
  token columns in all three rollup tables + the ledger rows) formats
  through a new `fmtInt` helper that coerces with `+v` first — comma
  grouping is wire-shape-independent from here on. The Usage tab's ↻
  refresh button moved to the TOP of the tab (beside the Totals heading,
  above everything it reloads) and now re-runs the exact loader set tab
  activation does — pricing editor included, so one click refreshes the
  whole page instead of just the ledger it used to hover over. Backend
  wire-contract test pins the `::bigint` casts
  (`test_usage_summary_wire_contract_json_numbers`); the formatter is
  behavior-tested under node (`test_console_usage_integer_formatting_node`).
- **Admin console: page refresh keeps the operator's tab + Usage refresh
  button**: the markup hardcodes Keys as the active tab, so every browser
  reload (F5) stranded the admin back on Keys — refreshing the Usage ledger
  took tab-click + re-filter. Tab switching now runs through ONE shared
  `activateTab()` (nav clicks and boot restore, so a restored tab lazy-loads
  exactly like a click), the last tab is remembered in the SAME iframe-safe
  `sessionStorage` store as the admin key (fresh sessions still open on
  Keys; invalid/renamed values repair to Keys), and restore runs inside the
  auto-connect path after the key input is populated (restored-tab loaders
  fire real fetches). The Usage tab gains a `↻ refresh` button (parity with
  Traces/Audit/Off-peak) and its ledger user-filter also persists across
  reloads (pendingUser seed honored by the first fetch, which races ahead of
  connect()'s keys load). Client-only — no API/schema changes. Test:
  `test_console_tab_restore_and_usage_refresh`.
- **Uvicorn connection knobs (12-factor) + framework-overhead bench harness
  (perf wave 2)**: two workstreams, one conclusion.
  (1) `PCAI_LLM_KEEP_ALIVE_TIMEOUT_S` (default 75) — uvicorn's
  `timeout_keep_alive`; the 5s default makes pooled consumers (agents,
  OpenAI SDKs) re-TLS-handshake constantly at scale, 75s matches common
  LB/proxy idle windows. `PCAI_LLM_LIMIT_CONCURRENCY` (default 0 = UNSET,
  uvicorn unbounded) — a hard cap on connections+tasks that sheds load with
  503, the same fail-fast discipline as DB-pool exhaustion; the key is
  OMITTED at 0 because uvicorn treats None as unlimited and an explicit 0
  as "shed everything". Wired config.py → main.py → chart env →
  values.yaml → DEPLOYMENT.md table; 4 new tests (env parsing + a
  uvicorn.run recorder asserting keep-alive always passes and the cap is
  key-absent at 0). ADDITIVE: default render unchanged.
  (2) `scripts/bench_overhead.py` — in-process ASGI benchmark isolating
  pure per-request framework overhead across four stacks (raw ASGI / bare
  Starlette / plain FastAPI / the real create_app() on /health), same
  workload, fresh-scope discipline (headers list copied too), warmup
  absorbing lazy middleware builds, no lifespan (no DB). Measured on the
  dev box (stable across runs): raw-asgi p50 ≈ 0.5µs, starlette-bare ≈ 8µs,
  fastapi ≈ 30µs, gateway ≈ 100µs (p99 ≈ 127µs) — i.e. the gateway's own
  stack (DurationHeaderMiddleware + MCP mount + route table on top of
  FastAPI) costs ~100µs CPU per request. Gateway mode (real uvicorn +
  keep-alive sockets) measured ≈ 1.3ms p50 at loopback — transport costs
  ~1.2ms, the framework ~0.1ms, and upstream GPU TTFT hundreds of ms:
  the evidence that gateway-side micro-optimization is finished and the
  remaining levers are routing intelligence (per-endpoint TTFT-weighted
  pool selection, ROADMAP §4's shape) and ops tuning. Concurrency
  correctness note: workers interleave via asyncio.sleep(0) OUTSIDE the
  timed bracket (awaiting a never-suspending coroutine does not yield —
  N workers would run serially); a first draft had this bug, the shipped
  harness verifies interleaving by construction.

- **HorizontalPodAutoscaler (ADDITIVE, off by default) + g2 example enabled**:
  `helm/templates/hpa.yaml` renders an autoscaling/v2 HPA on the Deployment
  when `autoscaling.enabled` (default false — default render byte-identical,
  same convention as ServiceMonitor/PrometheusRule/ingress). CPU-utilization
  target (default 70% of the REQUEST — the container carries `250m`), min 2 /
  max 6, deliberately asymmetric behavior: scale-UP at most doubles per
  minute (a fresh pod needs DB-pool warm-up + first registry refresh before
  it is useful), scale-DOWN ≤25%/minute behind a 300s stabilization window
  (no thrash on bursty load). Interplay fixed in `deployment.yaml`:
  `spec.replicas` is OMITTED when autoscaling is on (a present replicas
  field would pin the scale and fight the HPA every reconcile).
  `helm/values-examples/values.g2.yaml` enables it for the SE G2 site
  (prerequisites VERIFIED against the live release, `pcai-llm` namespace:
  metrics-server serves `metrics.k8s.io` pod metrics on this unit, CPU
  request present, HPA CRD available) with `maxReplicas: 4` — the
  connection math: every replica carries its own pool, so worst-case
  Postgres demand is maxReplicas × dbPoolMax (4 × 25 = 100) = the embedded
  Postgres image default exactly. Supporting knob:
  `database.embedded.maxConnections` renders `postgres -c
  max_connections=N` on the PG StatefulSet (unset keeps the image default
  100 and the default render byte-identical; the live release raises it to
  200 — headroom for PG's own sessions + future maxReplicas growth;
  CHANGING it restarts the StatefulSet, a brief /ready fail-closed 503
  window, no data loss). DEPLOYMENT.md § Sizing documents the full
  escalation order (dbPoolMax → limits → replicaCount → autoscaling) and
  the pool math. minReplicas: 2 additionally buys rolling-upgrade
  availability — at 1 replica every re-roll drops capacity to zero
  mid-upgrade.

- **Pure-ASGI duration middleware + uvloop/httptools (perf wave, gateway
  overhead reduction)**: two hot-path changes, both contract-preserving.
  (1) The `X-PCAI-Duration-Ms` stamp moved off `@app.middleware("http")` —
  Starlette's `BaseHTTPMiddleware` wraps EVERY request (SSE streams and the
  /mcp mount included) in a task + memory-stream layer with known streaming
  and cancellation edge cases — into `DurationHeaderMiddleware`, a ~15-line
  pure-ASGI wrapper registered FIRST (outermost) that adds nothing per
  request but the header on `http.response.start` and passes
  lifespan/websocket scopes through untouched. Header contract unchanged
  (same header, same server-time semantics, still stamped before the body
  streams); the existing `/health` test now rides three new tests (outermost
  pure-ASGI registration pinned, stamped-before-stream-body on a raw ASGI
  streaming app, non-HTTP scope pass-through). (2) `uvloop>=0.22` +
  `httptools>=0.8` join the dependencies: uvicorn's `loop="auto"` /
  `http="auto"` pick them up with zero code change (verified:
  HttpToolsProtocol + uvloop under Config.load), giving libuv's event loop
  and the httptools parser on the proxy path — a CPU-per-request/p99-under-
  burst play, NOT a TTFT play (upstream GPU inference dominates latency by
  3+ orders of magnitude). cp314 manylinux wheels verified; no compiler in
  the image path. docker/requirements.lock refreshed (uvloop==0.22.1,
  httptools==0.8.0, no other drift — diffed against pip freeze).

- **X-PCAI-Continue result-only protocol fixed — it 400'd on EVERY handback
  (2026-09-25 live report)**: the handback flush persisted the relay's FULL
  final message list as the awaiting-client `pending` tail, but the
  continuation merge computes `stored + pending + results` and shape-checks
  that against `stored` — the duplicated `[system, user]` prefix made the
  merged tail begin with a system message, so `detect_continuation` always
  failed ("results are not a handback continuation…"). The flush now reads
  the stored canonical row, slices the tail BEYOND it (the assistant
  tool_calls message + in-loop-executed results), and persists that — and
  skips loudly (never guesses the boundary) when the stored row is missing
  or is no longer a prefix of the final list (a racing request moved the
  store). Wiring tests lock the full round-trip (flush tail shape, merge
  fidelity at the upstream wire, counter resume) plus the fail-loud
  contracts (unknown thread, no awaiting state, non-results tail 400
  BEFORE any upstream call).

- **Relay iteration budget is PER-TURN (2026-09-25 live report)**: wave C's
  cross-request loop accounting made `tool_relay_max_iterations` (16)
  bound the WHOLE CONVERSATION — the counter resumed across every
  handback-continuation request and never reset, so a long tool-heavy
  discussion exhausted the budget mid-discussion ("max tool iterations
  (16) reached" firing repeatedly; a new question inherits a spent
  budget). The counter now resumes ONLY while requests keep answering
  handbacks (the continuation shape `_reconcile_relay_messages` already
  detects); any fresh user message — reset/new/trimmed_incoming mode, or
  an extend whose tail is not purely the handback's results — starts the
  guard at 0. The cap is back to bounding ONE turn's relay loop, which is
  where the runaway protection actually lives (wall clock, identical-call
  loop detection, and the per-call ledger gates still bound every
  request). Wiring tests lock both sides: a new turn resets AND a
  continuation resumes.

- **1.4.1 field fix (§3a live-grants resolver)**: the shipped v1.4.0 resolver
  aggregated an anonymous `record[]` (`array_agg((server, tool))`) — asyncpg
  has NO codec for the record type, so the query raised at the
  type-description phase for EVERY key, EVERY request; the ACL gate and
  relay catalog inherit the failure → every tool call errored and the
  harness drove retry loops (observed live post-deploy). Deny pairs now
  ride chr(31)-delimited TEXT elements (native text[] codec), the lazy
  import is corrected (mcpf has no enroll_memory — it lives in core), and
  the live layer is wrapped FAIL-OPEN: any resolver fault logs and
  degrades to the key's own stamped grants instead of taking tool calls
  down. Regression test pins the asyncpg-safe SQL shape + the fail-open
  behavior.

- **Relay wire hygiene — tool results no longer leak into the visible answer
  (2026-09-24 live report)**: two emission defects fixed. (1) On a MIXED
  handback batch, the executed MCP results' "[tool result] … {json}"
  summaries were re-emitted as `delta.content` chunks — OpenAI-spec
  clients (DSH) render those VERBATIM into the main chat, so a 347k-char
  memory-search result blob landed in the visible answer. The chat console
  never parsed them; the full results already reach the model via the
  role=tool rows, the console via the tool_call pcai events, the audit via
  the traces — the wire echo is gone (only the gated iteration event
  remains). (2) The guard-halt injection ("Tool use halted: …") was a
  role=user message — a NEW USER BUBBLE in every chat client and a user
  turn the canonical store records that the user never wrote; both halt
  sites now inject role=tool (same self-correction signal, honest shape).
- **§7 Wave C: cross-request loop accounting + the result-only continuation
  protocol** (the last open §7 arc): the relay's iteration guard budget now
  RESUMES across a conversation's requests — each handback terminal
  persists the loop's iteration count (new `canonical_conversations.
  iterations` column) and the next request's guard starts from there, so
  cycling handback → result → handback can no longer reset the cap per
  request. Plus the protocol itself: a client that read the handback
  event's new `thread` ref answers with `X-PCAI-Continue: <thread>` +
  messages = ONLY the new role=tool results — the gateway stitches the
  stored canonical list + the awaiting-client tail (new `pending`
  column, written at the handback terminal; the canonical messages column
  is deliberately untouched so full-history clients keep byte-stable
  extend/APC semantics) + the client's results, verifies the handback
  shape, and relays. No more full-transcript re-send (the measured 214k
  input-token turn). Fail-loud 400/503 on any mismatch; the
  X-PCAI-Conversation: raw escape and canonical_conversations=false keep
  today's behavior; the flag-off path writes no canonical SQL at all.
- **§3a LIVE profile MCP grants (the explicit trust-model decision)**: a
  profile may now declare `live_grants: true` — its preset mcp_grants
  then evaluate AT CALL TIME for every key of the profile (per-key
  mcp_tool_acls rows win as exceptions; a key-level allowed=FALSE row
  removes the pair from the profile set), so tightening a sensitive
  server re-scopes RUNNING keys on their next call with zero console
  clicks. Off (default) = stamps-at-create, byte-for-byte today.
  Console: Profiles form toggle; the grant read path gains one indexed
  query that short-circuits for stamps-only profiles.
- **stream_pcai_events DEFAULT FLIP (consumer inventory closed)**: new
  keys are spec-clean by default (FALSE) at every minting surface —
  unknown OpenAI-compatible clients (opencode, clearwing class) never see
  the non-spec {"pcai": …} lines unless an operator opts in. The
  inventory's two event-PARSING consumers opt in explicitly: the console's
  create form posts true, and enrollment (the DSH key path) passes True.
  Rotation keeps copying the old key's stored value verbatim; existing
  keys are unchanged (ALTER DEFAULT touches only future inserts).
- **Keys alphabetized by name**: `list_keys` reads `ORDER BY k.name`
  (was newest-first; same-name ties stay newest-first — a rotation reissues
  under the SAME name), so the console's keys table and the key filter
  dropdown scan in name order. The keys table now leads **Name / User** and
  moves the `key_prefix` column third (was Key first) — the name is the
  identifying field at a glance.
- **Transparent ledger partitioning path** (perf-structural wave): the
  usage ledger is created unpartitioned (zero behavior change — budgets,
  ledger truth, and every query are byte-identical today). At scale it
  converts to MONTHLY RANGE partitions via a run-once operator script
  (`scripts/partition_ledger.py --apply`, never startup DDL — an online
  conversion locks/rewrites the table, unacceptable per-boot): PK widens
  to (id, ts), monthly partitions + DEFAULT catch-all, single
  ACCESS EXCLUSIVE transaction with row-count parity post-check and an
  optional `--keep-legacy` belt-and-braces mode. Verified END-TO-END
  against a live Postgres (seed → apply → budget-gate query shape →
  settle-shaped INSERT → far-future row → DEFAULT catch → cross-partition
  aggregate → plan pruning), which caught and fixed four real bugs
  (nested BEGIN, legacy index-name collision, missing id-sequence default
  on the partitioned parent, asyncpg's bytes relkind false-negative).
  The budget gate's windowed SUMs PRUNE (day/week/month windows touch 1-2
  partitions instead of one growing heap — the gate gets FASTER with age);
  verified `Subplans Removed: 6` on a 1-month window query. A startup
  checker warns once the unpartitioned table passes 50M estimated rows.
- **Retention policies** (ROADMAP §5 shipped): `agent_traces` and
  `conversations` (messages follow via ON DELETE CASCADE) gain operator
  TTL prunes — `PCAI_LLM_RETENTION_TRACE_DAYS` /
  `PCAI_LLM_RETENTION_CONVERSATION_DAYS`, **both default 0 = disabled**
  (nothing is deleted until the operator opts in, the roadmap's own
  contract), piggybacking the registry-refresh housekeeping hook
  (error-swallowed). The usage LEDGER is never pruned regardless — it is
  the audit/budget artifact (pinned by test).
- **Caller-aware `GET /v1/models`** (ROADMAP §6, documented intent now
  shipped): every registered entry carries `pcai.type` (the pool's
  endpoint model_type — chat|embedding|reranker|voice) and
  `pcai.primary: true` on the FIRST cascade-ordered model the calling key
  can actually reach. The chat console has rendered both fields since the
  meta-chip wave — this lights them up for every OpenAI-compatible
  consumer with zero UI change. Unprofiled keys: types ride, no primary.
- **Upstream CA bundle knob**: `PCAI_LLM_UPSTREAM_CA_BUNDLE` (chart
  `settings.upstreamCaBundle`, default empty = system trust store) — the
  AgentBuilder lesson applied: when a site's PCAI ingress presents a
  private CA, "verify against the SITE CA" is now the easy path. Applied
  to ALL four upstream clients (model router, MCP federation, admin
  probe, enrollment memory provisioning). Verification stays
  UNCONDITIONAL — there is deliberately no verify=False knob.

- **Per-key `stream_pcai_events` + `X-PCAI-Stream-Events` header**
  (RFC-PER-KEY-STREAM-EVENTS, F. Caliva 2026-09-23): the relay's in-stream
  `{"pcai": …}` SSE telemetry events are not valid OpenAI chunks (no
  `choices`/`error`) — strict validating clients (opencode
  @ai-sdk/openai-compatible, clearwing openai-py ≥ 2.45) abort the stream
  on the first one. New per-key flag (default **TRUE** — deploy is a no-op
  for every existing key; console/DSH keep today's bytes) plus a
  per-request `X-PCAI-Stream-Events: 0|1` override (same three-state shape
  as `X-PCAI-Execute-Tools` over `relay_default`). A key flipped OFF gets
  a byte-clean spec-compliant stream: chunks, `reasoning_content`, the
  standard `include_usage` chunk, the `{"error": …}` envelope, `[DONE]`,
  and keepalive comments — everything a compliant client can parse; the
  telemetry it couldn't parse is suppressed via a single `_pcai()` gate
  over ALL 20 emission sites (purity pinned by a dedicated test).
  Console: create/edit toggles + a `no-events` pill on flipped keys.
  Telemetry decoupled from client protocol: the
  `gateway_relay_iterations` metric now counts iterations in the relay
  generator's `finally` (was: wire-format parsing, which would have
  undercounted on OFF keys).
- **Admin mutation audit trail** (compliance wave): new append-only
  `admin_audit` table + `core/audit.py` single-writer (`audit_action`
  never fails the operation; `details_redact()` enforces the no-secret-
  material rule). All 27 mutating admin routes + all 6 enroll-surface
  routes instrumented (33 actions: key/model/budget/MCP/profile/guardrail/
  off-peak/enrollment create-patch-delete-revoke). New admin-gated
  `GET /admin/audit` (filters, paging, CSV export) and a console Audit
  tab. The usage ledger stays the data-plane artifact; this is the
  control-plane twin.
- **Custom Prometheus metrics** (observability wave): `/metrics` previously
  exported only process metrics. New `core/metrics.py` (single import
  point, no-op shims when prometheus_client is absent) exporting 10
  series: `gateway_requests_total{model,status,kind}` (status = ledger
  vocabulary verbatim; bumped INSIDE `settle()` so metric and ledger row
  can never disagree; NO per-key label — cardinality/tenant-leak, per-key
  questions live in the ledger), `gateway_request_latency_seconds`,
  `gateway_ttft_seconds`, `gateway_budget_rejections_total{tokens|usd}`,
  `gateway_rate_limited_total{rpm|tpm|concurrency}`,
  `gateway_breaker_open{endpoint}` + `gateway_breaker_trips_total`,
  `gateway_relay_iterations{model}`, `gateway_mcp_acl_denied_total{server}`,
  `gateway_budget_warning_total{80|95}`. New guarded
  `helm/templates/prometheusrule.yaml` (default ruleset: error ratio,
  breaker-open, budget-rejection spike; `rules:` replaces wholesale) —
  `metrics.prometheusRule.enabled` default false; hosted-trial example
  enables it.
- **Chart network isolation** (network wave; user decision: g2 OFF,
  hosted-trial ON): new guarded `networkpolicy.yaml` (default-deny ingress
  from the EzUA ingress gateway + same-namespace + kubelet probes;
  egress DNS/embedded-PG/in-cluster CIDRs) and `authorizationpolicy.yaml`
  (Istio ALLOW from `istio-system` + same namespace). Both default-off —
  default and g2 renders are byte-identical; the hosted-trial example
  enables both. The `PCAI_LLM_TRUST_PROXY_IDENTITY` coupling is documented
  at the values (the flag alone is NOT the control). Kubelet-probe
  caveats + `excludeInboundPorts` relief valve in values comments and
  DEPLOYMENT.md § Network isolation.

- **Enrollment-time per-user memory datasets** (review §2; DECISIONS
  "APPROVED w/ refinement — opt-in at redemption, not always-provision" —
  the last remaining build-plan item): `POST /v1/enroll` gains
  `memory_opt_in` (`bool | null`; null = the redeemer's profile preset
  `"memory": {"provision": …}`, else the deployment default
  `PCAI_LLM_ENROLL_MEMORY_DEFAULT`, false). When opted in, the gateway
  provisions a private RAG dataset `mem-<tenant>-<user>-<key6>` on the
  REGISTERED memory MCP server (`PCAI_LLM_ENROLL_MEMORY_SERVER`, default
  `mm-rag` — URL only ever from that admin-registered registry row, no
  free-form URL env; auth reuses the same server-level credential
  resolution every MCP call uses) and stamps `X-Memory-Dataset` /
  `X-Dataset-Password` onto the fresh key's per-server credential row
  (the existing write-only Fernet path — merged into THAT key's relay
  calls to THAT server only; rotation copies it like every creds row).
  The generated password rides the redemption response ONCE. Provisioning
  failure NEVER fails the redemption (key created first; the response's
  `memory` block reports `{provisioned, dataset?, password?, error?}`);
  a 409 on the dataset name gets a bounded suffix retry
  (`PCAI_LLM_ENROLL_MEMORY_NAME_RETRIES`, default 3) instead of a blind
  adopt; with no `PCAI_LLM_SECRETS_MASTER_KEY` nothing is even created.
  The gateway PROVISIONS and BINDS, never deletes (deletion stays
  RAG-side; the gateway's only delete is clearing its own creds row).
  Redeem-page checkbox on the chat UI; profile preset editable in the
  console's Profiles form (stored in `defaults_json`); chart knobs
  rendered (`settings.enrollMemory*`).
- **Relay-attribution companions** (`X-MCP-Caller-Subject` /
  `X-MCP-Caller-Class`): the SQLhandler policy slice reads subject identity
  from `X-MCP-Caller-Subject` (+ optional `-Class`), not the plain
  `X-MCP-Caller` the gateway stamped — relayed calls resolved to
  key-fingerprint identity downstream, so per-subject policies never
  applied to gateway traffic. `resolve_mcp_headers` now additionally
  stamps `X-MCP-Caller-Subject: <subject>` (bare, no `@gateway` suffix) +
  `X-MCP-Caller-Class: <subject_kind>` in the SAME gated (`settings.
  mcp_caller_attribution`), post-override-merge block — ONLY when a real
  subject resolved (bound key / lookup hit); an unbound (key-fp) key emits
  neither companion (a pseudonymous call must not advertise a subject), and
  stale pre-reservation tenant values for the new names are dropped at the
  merge like `X-MCP-Caller` itself. All three names are now reserved at the
  `PUT /admin/keys/{id}/mcp-creds` write path (400). `X-MCP-Caller` is
  unchanged (still suffixed `@gateway`, still stamped on every keyed call).
- **Client tool-server dispatch — chart wiring** (ROADMAP §7 wave B2
  enablement): `PCAI_LLM_CLIENT_TOOL_DISPATCH` /
  `PCAI_LLM_CLIENT_TOOL_DISPATCH_TIMEOUT_S` now render from
  `settings.clientToolDispatch` (default false) /
  `settings.clientToolDispatchTimeoutS` (default 120.0) in the chart —
  previously the env vars had no values lever at all. When the flag is on,
  keys may register their local tools (`POST /v1/dispatch/register`) and the
  relay merges `client__<tool>` into the model toolset per key (fail-safe:
  keys without a live offer see byte-for-byte today's handback behavior;
  routes 404 while the flag is off). Site enablement lives in
  `helm/local/values.g2.yaml` (G2 set true for the DSH tool-server plugin).
- **Router extras — per-model fallback chains** (review §4a): admin-curated
  failover chains per virtual model, independent of the profile ordering,
  closing the "pinned request ends answerless" gap. `model_fallbacks` table
  (additive/idempotent) + `GET/PUT /admin/models/{name}/fallbacks`
  (validation: registered models, no self-fallback, dedup, chain capped at
  `PCAI_LLM_MODEL_FALLBACK_MAX` default 2). The chain walks when the
  serving model cannot pick (`pool.pick()==None` — previously a hard 503)
  or fails with an upstream transport error / 5xx post `pool.report`
  (previously a bare relayed 5xx). EVERY hop re-gates at walk time
  (registered + key-allowed + healthy chat pool + fresh `check_budgets`
  headroom — an exhausted candidate settles its own `budget_exceeded` row
  and is skipped; a fallback is NEVER a budget bypass) and settles under
  its ACTUAL model (the ledger row names the model that really served —
  the same per-candidate rule the MCP failover applies). Plain streaming:
  failover only BEFORE the first byte (transport failure / 5xx error body
  — mid-stream death is exactly today's behavior); the relay's mid-loop
  budget failover is unchanged. `X-PCAI-Fallback` (existing header, now
  also on the non-relay paths) names the model failed over FROM.
- **Router extras — router metadata in responses** (review §4b): headers
  ALWAYS (no transparency cost): `X-PCAI-Served-Model` (the post-cascade/
  post-routing model that actually answered) and `X-PCAI-Endpoint` (the
  endpoint id), via the single `_relay_headers` choke point on every chat
  response (JSON, plain stream, relay stream). Body `"pcai": {served_model,
  endpoint, cost_usd?, cached_tokens?}` ONLY on `X-PCAI-Metadata: 1` AND
  non-streaming — the module is a declared transparent proxy; body mutation
  is opt-in. An upstream that already emits a `pcai` key is never collided.
- **Router extras — zero-completion insurance** (review §4c,
  `PCAI_LLM_ZERO_COMPLETION_RETRY`, default OFF): when a chat call answers
  2xx with EMPTY assistant text (string or content-parts shape),
  `completion_tokens==0` and `finish_reason=='stop'`, retry the identical
  call EXACTLY once. Both attempts settle under the SHARED request_id (the
  first was real GPU spend); the retry re-runs guardrails on its own
  answer. Applies to the non-streaming JSON path and the relay's final
  answer (the relay owns assembly, so the retry is a bounded internal
  re-roll before anything reaches the client); NEVER plain streaming —
  buffering breaks transparency.

- **Identity Phase B: token validation at the gateway** (the fleet identity
  architecture, review §1.1 rung 2 + DECISIONS "OAuth REVISION (final)").
  A JWT-shaped bearer on `/v1` + `/mcp` is validated against the configured
  Keycloak IdP (PyJWT — already in the lock file, no new dependency): JWKS
  signature check (TTL-cached, refresh-once on an unknown kid), issuer +
  audience pinned, sub/email/groups extracted. THE FALL-THROUGH RULE: an
  invalid/expired token is never a hard 401 when the same bearer also
  authenticates as a virtual key (this is what keeps the ~30-minute token
  TTL invisible to headless key holders); it IS a 401 when the token was
  the only credential. A valid token authorizes nothing by itself — the
  gateway never issues, refreshes, or stores tokens. Inert until
  `PCAI_LLM_TOKEN_ISSUER` + `PCAI_LLM_TOKEN_JWKS_URL` +
  `PCAI_LLM_TOKEN_AUDIENCE` are all set (flag-off byte-identity).
- **Token shadow-keys** (`PCAI_LLM_TOKEN_SHADOW_KEYS`, default off): a
  first-seen VALID token subject lazily provisions user + ledger key
  (`bound_via='token-shadow'`, idp='keycloak', scopes proxy+mcp, limits
  from config) so the usage ledger keeps its key_id anchor; lookup via
  UNIQUE (idp, subject) → newest non-revoked shadow key. Off = tokens
  validate but never provision (the anonymous-fp posture).
- **`POST /v1/keys/for-me`** (`PCAI_LLM_SSO_KEY_CREATE_ENABLED`, default
  off; requires `PCAI_LLM_TRUST_PROXY_IDENTITY`): a trusted browser
  identity mints a key bound to its OWN subject (`sso-create`) — no admin
  escalation, scopes fixed, grants stay admin-controlled. 403 when trust
  is off.
- **Admin gate second path** (`PCAI_LLM_ADMIN_GROUPS`, default empty):
  a trusted browser identity in one of the configured groups may call the
  admin API. Empty (the default) = today's bearer-only gate, byte-for-byte;
  a rejected bearer never falls through to the group path, and 503-on-
  empty-admin-config still wins.

- **Key rotation carries per-server MCP credential overrides** (the
  "re-enter my memory dataset + password after every bump" fix): rotation
  minted a new key_id and copied the federation grants (the 2026-09-18
  fix) — but NOT the `mcp_server_creds` rows, so every rotated/re-created
  key silently lost its upstream identity headers (the memory-MCP
  `X-Memory-Dataset` / `X-Dataset-Password` pattern) and the user had to
  re-enter them in the console. The release-iteration workflow (bump the
  image tag → rotate/recreate the key) hit this every single time; each
  user's credentials are per-key, so every user re-entered theirs. Rotation
  now copies the override rows in the same one-statement INSERT..SELECT as
  the grants (verbatim ciphertext — the Fernet master key decrypts it, not
  the key credential, so no re-encryption is needed), placed after
  create/before revoke under the same over-provisioning partial-failure
  contract. The response reports `creds_copied`; the console's rotate
  dialog names both carried-over counts. Enroll-created keys are unaffected
  (they are new identities by design — their profile presets stamp budgets
  + grants, not personal credentials).

- **Canonical conversations are NUL-poison-proof** (the "reconciliation
  failed … \u0000 cannot be converted to text" freeze, caught live on the G2
  gateway): a tool result carrying a literal NUL byte (binary magic, a
  raw-bytes echo) serializes to the `\u0000` JSON escape, which Postgres
  text/jsonb REJECTS — so `canonical.save` failed, the store never advanced,
  and EVERY subsequent turn of that thread re-failed while reconciliation
  silently degraded to raw mode (unbounded prompt growth, lost prefix-cache
  hits). `save` strips NULs from the PYTHON strings before `json.dumps` (the
  model-facing wire path is untouched); the store is unpoisonable regardless
  of what any tool returns. Method note: a serialized-TEXT replace or
  escape-shaped regex is the WRONG tool here — both corrupt legal payloads
  (the first fix's replace spliced a lone backslash from a literal
  `\u0000`-text substring onto the next character, producing the "Escape
  sequence `\ ` is invalid" rejection seen live post-deploy on content that
  had been perfectly legal; verified against PG 16 + asyncpg 0.31 that every
  non-NUL `json.dumps` output inserts into jsonb verbatim). Pre-dumps string
  sanitization is the only correct boundary.

- **Relay iteration cap raised 8 → 16** (`toolRelayMaxIterations`): the old
  cap counted MODEL rounds, not tool calls, and a serial research turn
  (search, then a fetch per page, then synthesize) burns ~1 round per call —
  an opencode research turn tripped `max tool iterations (8) reached`
  mid-research. The other guards are unchanged and still bound the true
  worst case: wall clock (clamped under the ingress timeout), identical-call
  loop detection, and the per-call budget/ledger gates. Docs updated
  (API.md also had a stale "wall clock 240 s"; the deployed value is 900).

- **Tool relay: harness-prefixed MCP tool names no longer produce
  `Error: unknown tool` storms** (the name-duality fix, observed live in an
  opencode session): a harness that registers the gateway's OWN /mcp
  endpoint under its connection prefix offers every tool under TWO
  spellings — `mcp__pcai-llm-gateway__mm-rag__search_memory` (the harness's)
  and `mm-rag__search_memory` (the relay's offer) — because
  `shadow_client_mcp`'s one-hop twin split matched nothing on the
  double-prefixed name. The model imitated the bare spelling from the
  relay's own injected `[tool result] …` summaries, and every harness-side
  execution of it failed. Three-part fix in `core.toolrelay`:
  (1) twin matching is SUFFIX-based over `__` tails, so the harness's
  double-prefixed tool is deduped from the offer and mixed-batch handbacks
  rewrite to it correctly; (2) a model call arriving under either spelling
  ALIASES to the granted in-cluster twin (unique suffix match against the
  granted catalog — never a guess; the grant gate re-checks the resolved
  pair), on both the streaming and loop relay paths, instead of parse-None
  or a bogus-server ACL denial; (3) unknown/denied gateway-shaped calls
  append the granted-tool list so the model self-corrects in one step.
- **Panel: Traces tab renders one stacked table per conversation** (the
  "14 cards for one thread" verbosity): the old per-request card + a
  collapsible `step table (N)` summary made a multi-request conversation
  read as N boxed cards with N repeated table headers. Requests in a
  thread now render as turn-header rows (double rule above, per-user
  shade) inside ONE table, their step rows stacked back-to-back under a
  single header; single-request threads drop the turn row entirely. Pure
  client-side re-render of the same `/admin/traces` payload — no API or
  DB change.
- **Usage-detail passthrough reaches the PLAIN chat paths** (the "cached
  tokens invisible" accounting gap): the relay settled
  `usage.prompt_tokens_details.cached_tokens` since 0.9.0
  (`toolrelay._cached_tokens`), but `_stream_chat` and `_proxy_json` —
  every request WITHOUT `X-PCAI-Execute-Tools` — still flattened the
  details away, so a deployment with a high vLLM prefix-cache hit rate
  ledgered `cached_tokens=0` on all its direct traffic. `_stream_chat`
  now ADDS `stream_options: {"include_usage": true}` to the upstream body
  (clients that sent their own keep theirs; an upstream that rejects
  stream_options is retried once without it — the same negotiation
  `stream_once` applies), `_StreamLedger` extracts the cached count, and
  `_proxy_json` reads the same detail from the non-streaming usage block;
  both settle it into the row. Absent/odd details stay 0 — no fabricated
  facts. Note: ledger rows written BEFORE this fix are immutable history —
  the numbers only become trustworthy from the first post-deploy request
  onward.
- **Panel: Cached column on the By-key chargeback table** (Usage tab): the
  backend's `/admin/usage/summary` by_key rollup has always returned
  `cached_tokens` — the UI simply never rendered it. One column added
  (header + `loadUsage` row builder), colspan 7 → 8; matches the Totals
  and By-model tables that already showed the cached split.
- **Panel: show/hide toggles on every password field** (admin key, MCP
  server key, model endpoint keys, MCP credential-override values): a
  client-side-only `armPasswordToggles` helper (armed at boot, dialog
  render, and both dynamic row builders) appends a show/hide button after
  each `input[type=password]` — typing 40-char secrets against masked dots
  was the pain. The write-only credential contract is untouched: no
  endpoint returns stored values; the toggle only unmasks the field being
  typed into.
- **Complexity-driven model routing** (pseudo-models `routing` /
  `routing__X`): a deterministic complexity score (est. prompt tokens +
  tool-definition bytes + turns + a capped max_tokens slice) picks the
  CHEAPEST model on the key's profile ladder that fits — never above the
  named ceiling (`routing` = the ordering's best model, `routing__X` = X,
  hard-ACL-checked), never below the profile's `routing_min_model` floor.
  Per-profile `routing_policy` gates it: `off` (default — the names keep
  today's alias behavior byte-for-byte) → `shadow` (serve as `off`, stamp
  the would-serve model on the ledger — the labeled data a learned router
  trains on) → `active` (serve the routed model through the normal
  pinned/budget machinery; admission failover treats it as PINNED so a 429
  walk never promotes above the ceiling). `X-PCAI-Route` + the ledger
  `routing_*` columns make every decision auditable; any router error
  fails safe to the ceiling, never to a worse answer.
- **Key rotation in one call** (`POST /admin/keys/{key_id}/rotate`): mints
  a new key with the SAME name and attributes copied verbatim from the old
  row, then revokes the old one (create-first-then-revoke — a partial
  failure leaves both keys live, never strands clients on 401s); the 201
  returns the plaintext exactly once. Fail closed: 404 unknown, 409
  already-revoked (refused before any write). Console: one shared
  show-once reveal at create + rotate with one-click copy, the keys list
  stays prefix-only, and the old client-side reissue flow (renamed copy,
  old key kept alive) is removed.
- **MCP flat per-tool surface + fleet priority**: `/mcp` tools/list now
  re-exposes the calling key's granted upstream tools as individual
  `{server}__{tool}` entries (upstream inputSchemas + annotations,
  ACL-filtered per request — zero-grant servers contribute nothing, not
  even their URLs) with the two gateway tools kept first as the compat
  surface; every flattened call funnels through the SAME gate chain (one
  choke point, no second enforcement path). Additive `mcp_servers.priority`
  (lower = preferred) orders listing output and the NEW flattened-call
  failover (transport failures retry the next eligible server, ≤2 retries;
  explicit `mcp_call` never fails over — a deliberate asymmetry); per-server
  listing cache (60 s TTL, invalidated on registry changes, stale-on-error).
  Kill switch: `PCAI_LLM_MCP_FLAT_TOOLS=0`.
- **Upstream inputSchemas were silently dropped**: `_fetch_tools` read
  `tool.inputSchema` while this MCP SDK generation exposes snake_case, so
  catalog listings shipped no schemas at all; now reads `input_schema`
  (camelCase as fallback) and captures annotations too.
- **Bounded relay retry-once**: a timed-out bounded model call (prefill
  queueing under agent fan-out is the usual trigger) retries ONCE per
  relay turn when the remaining wall-clock budget supports it (≥ 10 s) —
  the retry spends only REMAINING budget, so the ingress clean-close
  guarantee holds; a second timeout falls through to the unchanged clean
  close. Each attempt settles its own ledger row. Knob:
  `PCAI_LLM_TOOL_RELAY_CALL_RETRIES` (default 1; 0 = the old behavior).
- **Chat-console "tools · MCPs" checkbox fixed**: the checkbox was
  stretched by a `header input {width:260px}` descendant selector (looked
  detached from its label); input-width rules are scoped to text inputs,
  the checkbox is themed with `accent-color`, and labels are explicit.
  Behavior/persistence unchanged.
- **Conversation-grouped traces** (the "everything is 1 or 2 steps" fix):
  every relay step now carries a `thread_id` — a stable fingerprint of
  (key, first user message); harness loops re-send the accumulated history
  each step, so the first user message is the conversation's stable seed.
  One long multi-turn DSH session collapses into ONE grouped trace event
  (summary: model chain — a failover reads `GLM-5.3-Flash → Qwen3.8-27B`
  — request count, step count, handbacks, time span) with the per-request
  cards nested inside; unthreaded rows group per-request as before.
  `agent_traces.thread_id` + index (ALTER-IF-NOT-EXISTS, zero migration);
  the grouped read projects it with `MIN()` — one query, unchanged shape.
- **Mid-relay budget failover now covers PINNED requests** (the "GLM budget
  dies mid-turn → flow into Qwen" fix): the relay's fallback chain was built
  only for alias/auto requests — a pinned model exhausting its budget
  MID-STREAM halted the turn answerless ("tool use halted: budget exceeded
  mid-relay…"), even though admission itself promotes a pinned request whose
  budget is already exhausted to the ordering. The profile ordering now
  rides `Resolution.order` (fetched anyway — zero extra queries) and the
  relay chains the ordering AFTER the serving model
  (`_relay_fallback_tail`, filtered to registered ∧ allowed; fresh budget
  gates re-check each candidate). Admission semantics unchanged: pinned
  requests still never RATE-fail-over.
- **`auto` is a first-class model in `/v1/models`** when the key's profile
  has a model ordering: a synthetic entry (`pcai.type: "cascade"`,
  `pcai.order: [...]`) precedes the registered models — requests naming it
  resolve through the ordering (first candidate with budget headroom). The
  chat console's model dropdown picks it up with no special-casing and
  shows the admin's fallback order as the option tooltip; suppressed when
  no ordering exists or no ordered candidate is callable (registered ∧
  allowed), since auto would 404 at send time.

## 0.7.1 — 2026-09-18

- **Off-peak utilization panel + per-model pricing ratio** (the console):
  every attached evaluator (`settings.offpeakModelPromql` — e.g. GLM on
  scs04 GPUs 4-7, Qwen on scs05) now renders as a utilization card — live
  value, ACTIVE/normal/error state, and an SVG sparkline of the last ~2 h
  of successful samples with the enter/exit thresholds drawn as dashed
  lines — plus a **ratio editor**: `PUT /admin/offpeak/models/{model}/fraction`
  (new `offpeak_model_config` table, cached like `model_costs` — TTL refresh
  in the evaluator loop, instant invalidation on write, last-known ratios
  kept through a DB blip). Precedence while the model's governing evaluator
  is ACTIVE: panel override → profile fraction → gateway default; inactive
  always bills 1.0. The ledger keeps TRUE usage (fraction stamps per settle),
  so edits are forward-looking commercial terms only. The global GPU row
  gained the same sparkline; the 30s auto-refresh skips a repaint while an
  admin is mid-edit in a ratio input (caret + value survive).
- **At-a-glance budget strip on the chat homepage** (the usage dialog existed
  but was click-gated — users only found it after a 429 taught them it
  existed): the moment a key connects, the header shows the TIGHTEST budget
  burn-down (highest fill % leads: `model period  $1.234 / $10.00` + bar, or
  tokens) plus the current-minute request window, refreshed after every turn
  so the burn stays honest. Click-through opens the full usage dialog
  (all budgets, rate window, recent outcomes). Same `/v1/usage` endpoint —
  no new surface; best-effort (a failed refresh just hides the strip, never
  blocks the chat).
- **Near-transparent model failover**: the mid-relay failover's user-visible
  mention shrank to one compact line (`[relay] failing over to {model}`) —
  the verbose budget prose ("X budget exhausted mid-task (reason) — failing
  over…") is gone from the reading flow. The WHY was never lost: the
  `budget_exceeded` ledger row + the trace card still record which model ran
  out and the gate reason, and the response's `model` field reports what
  actually serves (so the harness's own model-changed note keeps working).
  Per-fallback lines compacted to `{model} unavailable — trying next`.
- **Tool-relay wall clock raised 240s → 600s + an ingress-timeout clamp** (the
  "model error: relay model call timed out" fix): harness-relay turns (big
  prefill, one generation emitting a large parallel tool-call batch)
  legitimately cross 4 minutes, so the old default killed real work instead
  of protecting it. Primary fix: `toolRelayWallClockS: 600` (config default
  and every chart values file). Guardrail: `RelayGuard` now clamps its
  deadline to `upstream_timeout_s − tool_relay_ingress_headroom_s` (new
  setting, default 30s) — a raised budget can never race the Istio
  virtualService 600s timeout, so a burned budget still closes the stream
  CLEANLY (guard event + error chunk + [DONE]) instead of dying to a bare
  504 the client can't parse.
- **HPE favicon on the chat UI and admin console**: both pages carry an
  inline SVG favicon (`<link rel="icon">` as a data URI — no new route, no
  new packaged assets, nothing for a path-prefixing ingress to break). The
  mark is the 2025 HPE lettermark (official stroke geometry: the H·P·E
  letterforms with the notched E) rendered white on the HPE-green `#01a982`
  tile — the console's existing accent — so it reads on light and dark tab
  strips alike.
- **Thinking no longer renders twice on tool-relay turns** (base chat): the
  relay streams every reasoning payload on BOTH channels — `pcai.thinking`
  events (rendered inside the per-iteration Step panels) and raw
  `reasoning_content` deltas (rendered in a standalone Thinking box) — so a
  relayed turn showed the same text twice. The chat now treats the deltas as
  redundant the moment relay thinking is seen for the turn; relay-off turns
  (tools unchecked, no grants, plain reasoning models) keep the standalone
  box. Server surface unchanged — external OpenAI-compatible clients still
  get `reasoning_content`.
- **Scroll lock released during generation** (base chat): the view was
  yanked to the bottom on every paint batch, making it impossible to scroll
  up mid-stream. Follow-the-stream is now pinned-bottom-only: scrolling up
  releases it, returning to the bottom re-engages it, and sending a message
  re-engages explicitly.
- **Admin console user filters actually contain the users**: `GET /admin/keys`
  projected `user_name` but not `user_id`, while the console derives the
  ledger/trace user dropdowns from exactly that join (`user_id + user_name`
  — the documented contract in `knownUsers()`), so the dropdowns sat at
  "all users" unless traces happened to be loaded. `list_keys` now returns
  `user_id` (additive; admin-gated projection, hashes still never leave the
  server), and the dropdowns fill from the key list on every boot.
- **Ghost prices are removable** (the "renamed model leaves its old price
  row forever" gap): new `DELETE /admin/costs/{model_name}` +
  `costs.delete_cost` (404 when not priced), a remove button on every row
  of the pricing table, and the route listed in `/admin/help`. The
  `{model_name:path}` converter carries slash-containing names
  ("Qwen/Qwen3.8-27B-FP8") — the same trap DELETE /models documents, where
  a plain path param 404'd every slash-named pool. Ledger history stays
  put: costs were computed AND STORED per row at request time, so removal
  only affects future requests (they ledger NULL = unpriced).

## 0.6.0 — 2026-09-17

Content guardrails, OpenAI-idiomatic MCP opt-in with approval passthrough,
client-handback tracing, and per-model off-peak evaluators.

- **Per-model off-peak evaluators reach the chart** (the "billing discount
  during peak" fix): `settings.offpeak_model_promql` — per-model PromQL +
  hysteresis, built for exactly the dilution failure — was UNREACHABLE
  through the chart. Now `settings.offpeakModelPromql` (JSON map of model
  name → scoped PromQL) renders to `PCAI_LLM_OFFPEAK_MODEL_PROMQL`
  (omitted when unset — default render byte-identical). Live case: GLM
  serving at ~53-90% on scs04 GPUs 4-7 while the 16-GPU fleet average sat
  at 19.6% — below the 20% enter threshold — so the discount flipped ON
  during peak. The console Off-peak card now renders the per-model
  evaluator states (`models`) with an explanatory empty-state, and
  values-examples/values.g2.yaml ships the validated scoped query
  (DCGM has no pod label — scope by Hostname + device).
- **Client-handback tracing** (the "every DSH request is 1 step" fix): when
  the relay's model turn calls tools the CLIENT offered — a harness running
  its own tool loop — the handback now writes agent-trace rows
  (`phase='tool'`, `server='client'`, `status='handed_back'`, `ms` NULL;
  loop-guard refusals trace as `denied`). Previously only in-cluster
  executions traced, so every harness-side turn degenerated to
  "1 steps · tools 0ms" and the waterfall hid what the agent asked to do.
  `totals` gains `handed_back`; the console card shows
  `N handed to client` and renders handback rows as informational (dim),
  not errors.
- **Content guardrails** (ported from the hpe-llm-gateway design,
  reimplemented on the ledger): named per-model pre_call/post_call policies
  — keyword blocklist (block|mask), regex PII masking (email/phone/
  Luhn-valid cards), length caps, external webhook moderation (fail-open,
  verdict visible). pre_call blocks refuse BEFORE the gates (no rate/budget
  consumption, no slot); post_call blocks refuse the answer with the REAL
  upstream spend ledgered (`status='guardrail_blocked'`); masks rewrite
  request/response content. Streaming buffers only when post_call guards
  are attached — pass replays byte-identical, masked resynthesizes the
  final chunk, blocked emits a terminal SSE error. Every verdict (pass
  included) lands in `usage_ledger.guardrail_events` JSONB; relay-path
  verdicts go on a 0-token `chat:guardrail` marker row. Admin CRUD +
  per-model attachment (`/admin/guardrails`, console Guardrails tab);
  webhook URLs pass the same SSRF gate as endpoints and MCP servers.
  Reference bugs fixed in the port: mask-every-occurrence, phone patterns
  not matching inside digit runs, Luhn-gated card detection, unambiguous
  webhook mask application.
- **MCP request-body opt-in + approval passthrough** (OpenAI-idiomatic):
  `tools:[{"type":"mcp","server"?…,"require_approval"?…}]` engages the
  in-cluster relay per request (default "never", narrowed to `server`),
  while `"always"` exposes the granted catalog (with upstream
  `inputSchema`s) as plain OpenAI function tools and returns the model's
  `tool_calls` UNTOUCHED for client-side/human-approved execution. mcp
  entries are stripped before the upstream call; an explicit
  `X-PCAI-Execute-Tools: 0` against a body execution request is a loud 400
  (never a silent ignore).
- **Relay catalog scope-mode fix**: the catalog fan-out iterated only
  EXPLICITLY granted servers, so a scope-mode mcp key without explicit ACL
  rows relayed with an EMPTY toolset — silently contradicting the
  documented scope semantics ("every policy-admitted tool of enabled
  servers"). Scope mode now sweeps all enabled (or requested) servers.
- **Streaming tool-event payloads**: relay `tool_call`/`tool_error` SSE
  events now carry the parsed `args` and a truncated `result` preview, so
  the chat console expands each tool step in place (no second fetch to see
  what a tool did). The model still receives the full result via the relayed
  messages — the preview is wire-only.
- **Per-key relay default** (`api_keys.relay_default`, console Keys ▸ edit):
  three-state resolution per request — the `X-PCAI-Execute-Tools` header
  always wins (`1` forces on, `0` opts out), an ABSENT header falls back to
  the key's default. Off-cluster consumers (sproket) relay in-cluster with
  zero client config while local harnesses keep their own tools — flipped
  live per key, no restart.
- **Key edit dialog** (`PATCH /admin/keys/{id}`): edit name, scopes,
  `allowed_models`, RPM/TPM, `max_concurrent`, expiry (explicit null clears)
  and the relay default IN PLACE — keys are read from Postgres per request,
  so edits are live immediately. No reissue, no client migration, no new
  plaintext; hash/prefix are never editable (rotate = reissue).
- **Per-profile off-peak fraction** (`profiles.offpeak_fraction`): the
  commercial knob the env default can't be — different tenants bill at
  different discounts during the same low-demand window. Read via the
  profile JOIN at authenticate time, console-editable, live on the next
  request; NULL = inherit the gateway default.
- **Off-peak parse hardening**: a tool result with `isError=true` and
  error-shaped text are rejected BEFORE number-parsing (the evaluator once
  parsed "400" out of "Prometheus returned HTTP 400" and billed on a status
  code); default PromQL corrected to
  `avg(avg_over_time(DCGM_FI_DEV_GPU_UTIL[5m]))`.
- **Console polish**: grant-tree restyle with quote-safe CSS selectors (a
  `'` in a server/tool name no longer throws querySelector and silently
  kills the tree); `Tokens (in + out)` trace-column label;
  [DEPLOYMENT.md](documentation/DEPLOYMENT.md) env-table refresh.

## 0.4.1 — 2026-09-16

Console polish on the 0.4.0 observability surfaces.

- **Endpoint status dots + test buttons**: the Models and MCP tabs render
  live health dots (healthy / cooling / disabled, from `GET /admin/endpoints`)
  and a `test` button per row — model pools probe
  (`POST /admin/models/{name}/probe`), MCP servers answer a real `list_tools`
  (`GET /admin/mcp-servers/{name}/tools`). A dead replica is discovered by
  the operator, not by end users.

## 0.4.0 — 2026-09-16

The observability + self-service wave, plus the usability-audit console
batch.

- **Agent traces**: one `agent_traces` row per relay step (phase `chat` per
  model iteration with wall-clock ms + observed usage; phase `tool` per tool
  call with server/tool/ms/status), grouped by `request_id` — the per-user
  "what did the agent DO and how long did each hop take" timeline behind
  `GET /admin/traces`. Writes are best-effort: a trace failure never fails a
  request the ledger already settled.
- **Saved conversations per key** (`/v1/conversations` CRUD): server-side
  transcripts behind the same bearer as the proxy; every query filters by
  `key_id`, so foreign ids are a plain 404 (no existence oracle). The client
  pushes the full transcript per turn (replace-all, one transaction); caps
  enforced app-side (≤200 messages, ≤32 KB each).
- **Self-service enrollment codes**: admin mints a code bound to
  tenant + profile (`POST /admin/enrollments`, plaintext shown once, hash
  stored); anyone holding it redeems at the PUBLIC `POST /v1/enroll` for a
  personal key with the profile's budgets/cascade/grants applied at creation
  in one transaction. Brute force is answered by 32-byte code entropy,
  a generic rejection message (no per-cause oracle) and a per-IP redeem
  throttle (10/h → 429).
- **Per-key MCP credential overrides** (`mcp_server_creds`): Fernet-
  encrypted header JSON per (key, server) — the memory-MCP pattern
  (`X-Memory-Dataset` / `X-Dataset-Password`) without per-user server
  registrations. Write-only via `/admin/keys/{id}/mcp-creds` (values never
  return); merged AFTER server-level auth, last-wins, on the /mcp and relay
  call paths.
- **Off-peak (demand-shaped) billing**: a background evaluator samples a
  configurable PromQL through the registered Prometheus MCP server and
  hysteresis-switches a low-demand state (enter below enter_pct, exit only
  above exit_pct). Ledger rows keep TRUE tokens/cost stamped with the
  `billing_fraction` in effect; budget gates consume the fraction-weighted
  sums. ANY evaluator failure bills full price (fail-safe). Snapshot at
  `GET /admin/offpeak`; console Off-peak tab.
- **Console UX batch** (usability audit): quote-safe `esc()` closes the
  admin-console XSS (a registered server's tool name could break out of a
  quoted attribute and steal the admin key); every mutating button is
  double-click guarded (a second click on "Create key" used to overwrite the
  shown-once plaintext); the budget editor posts the UNION of existing +
  new budgets (a single-row "add" no longer silently deletes the rest);
  the unreachable rotation flow is wired back onto the keys table; the
  remembered admin key actually reconnects (store mismatch); aria-live
  regions + labeled inputs; responsive layout; chat gets New chat, per-turn
  model refresh and human error messages instead of raw status codes.

## 0.3.1 — 2026-09-16

- **Crash-loop schema idempotency fix**: startup migrations no longer crash
  on any database state — the `model_costs` per-1k→per-1M column renames are
  guarded by an `information_schema` existence check (a bare `RENAME` on an
  already-migrated database raised `UndefinedColumnError` and pod-restart-
  looped), and the empty-allowlist MCP rows flip to `tool_policy='all'`
  idempotently.

## 0.3.0 — 2026-09-16

The security + performance audit program, delivered as one hardening wave.

- **Relay ledger settle** (the budget hole): every relay model call settles
  real usage (`stream_options: {"include_usage": true}`, estimate fallback)
  with a mid-loop budget re-gate — previously the relay loop's model calls
  bypassed token AND USD budgets entirely.
- **Slot leaks closed**: the concurrency cap no longer bricks a key — the
  relay "no MCP grants" 400 runs before the gate, every post-gate raise
  releases the in-flight slot, and client disconnects settle + release
  inside a shielded scope.
- **Admin console XSS fixed** (quote-safe `esc()`) and `GET /admin/help`
  admin-gated (it enumerated the surface — and the Secret recipe — to
  anyone on the public hostname).
- **Metadata-call timeouts**: MCP catalog fetches (federation, relay, admin)
  are bounded at 15 s — one hung MCP server no longer freezes a request for
  the full 600 s upstream timeout.
- **Relay policy gates**: the relay executor enforces the same per-server
  tool policy and RPM window as `/mcp` — the automated path is no longer a
  policy bypass.
- **Chunked-body cap**: request bodies are read through a bounded
  accumulator (a chunked body with no Content-Length used to buffer
  unbounded into memory), 413 at `maxBodyBytes`.
- **SSRF guard on admin-registered URLs**: loopback, link-local/metadata
  (169.254.169.254) and operator-extra CIDRs (`PCAI_LLM_URL_DENY_CIDRS`)
  are refused; RFC1918 + `*.svc.cluster.local` stay allowed (in-cluster
  upstreams ARE private here).
- **Key PATCH** (`PATCH /admin/keys/{id}`): edit limits/scopes/expiry in
  place instead of reissue-and-migrate for a one-field change.
- **Endpoint health surface**: `GET /admin/endpoints` joins the DB registry
  with the live in-memory breaker state (last error, cooldown remaining);
  `POST /admin/models/{name}/probe` is a 5 s upstream check; breaker state
  now SURVIVES registry refreshes (a console save during an outage no longer
  hands a dead endpoint a fresh strike budget).
- **Ledger explorer**: `GET /admin/usage` gains filters (key/model/status/
  since/until), an actor join (key/user/tenant names), GROUP BY rollups
  (key|model|tenant|day) and CSV export.
- **Pool env wiring + gate consolidation**: `PCAI_LLM_DB_POOL_MIN/MAX/
  ACQUIRE_TIMEOUT_S` replace the hardcoded 2/10 pool (bounded acquire fails
  fast with 503 `database busy` instead of queueing); the budget gate is two
  aggregate queries instead of an N+1 chain, and the cascade's already-
  computed gate is reused on the pinned path.
- **Rate-limit headers**: `X-RateLimit-Remaining-Requests/-Tokens` on
  admitted calls (SDK clients back off natively); an opportunistic
  `rate_windows` prune (the per-minute counter table grew forever).

## 0.2.0 — 2026-09-16

The first post-0.1.16 feature cut.

- **USD cost budgets** (`cost_budgets`): parallel shape to token budgets
  (`limit_usd`), enforced pre-call alongside them — whichever limit hits
  first stops the key. Cost spend is `SUM(cost_usd)` over the same windows.
- **Model pricing** (`model_costs`, `POST/GET /admin/costs`): per-model USD
  prices per 1M tokens (input/output/cached); every ledger row computes and
  stores `cost_usd` at request time — repricing never rewrites history.
- **Model CRUD** (`DELETE /admin/models/{name}`, replace-per-model pool
  semantics with pruning of endpoints not in the payload, `model_type`
  routing: a chat request must never hit an embedding endpoint).
- **Profiles as dynamic presets** (`profiles.defaults_json`): a profile
  carries preset budgets + MCP grants stamped onto every key created with
  it, and the console profile editor offers every saved profile as a
  "start from" template — presets are data, not code.
- **Budget-status completeness**: `budget_status` emits BOTH budget kinds
  (`kind: tokens|usd`), so the console and `/v1/usage` see what the gate
  enforces — and key rotation no longer silently drops USD budgets.

## 0.1.16 — 2026-09-16

The first *feature-complete* release label (re-cut over the original 0.1.16:
same tag, newer content — mutable-tag caveat, `pullPolicy: Always` covers it).

Everything through the cache/cost accounting work:

- **Gateway core**: OpenAI-compatible `/v1` proxy (chat streaming, embeddings)
  over endpoint pools (priority tiers, weighted split, `weight: 0` = standby,
  circuit breaker); virtual model names; profile-driven **budget cascade**
  (first model with headroom serves; pinned requests never cascade)
- **Virtual keys**: SHA-256-hashed, scoped, per-model allowlists, RPM/TPM,
  concurrency caps, expiry/revocation, re-issue (rotation) flow
- **Budgets**: day/week/month per key per model, ledger-derived (no
  check-then-act races); cache-hit rows excluded from spend
- **Usage ledger**: input/output/cached token split, per-request USD cost
  (per-model price table, computed at request time), cache-hit rows visible
  but free; `/admin/usage/summary` rollup
- **MCP federation**: default-deny per-tool ACLs, per-server tool policy
  (`all` / `allowlist` + checklist editor), fleet-key + per-server +
  panel-encrypted credential modes, live tool catalogs
- **Tool relay** (`X-PCAI-Execute-Tools`): the model's tool calls executed
  in-cluster; thinking + per-iteration events streamed to the chat UI;
  guards: max iterations, wall clock, loop detection
- **Console**: admin panel (keys / profiles / MCP / models / usage / costs),
  chat UI with thinking + tool dropdowns; in-page error strip
- **Ops**: embedded-Postgres mode (Secret-based auth), Kyverno vendor labels,
  values-only PCAI deployment, pullPolicy/rollout-on-upgrade knobs

## 0.1.15 — 2026-09-16

Console stabilization round (error strip, diagnostics, export fixes).

## 0.1.13 / 0.1.14 — 2026-09-16

Grant-tree redesign, profile-editor fixes, k8s-ops registration.

## 0.1.12 and earlier — 2026-09-15/16

First deployable releases: proxy + keys + budgets + MCP federation + admin
API + chat UI, through the audit hardening (5 found bugs fixed, live-Postgres
schema verification) and the streaming/tool-relay features.
