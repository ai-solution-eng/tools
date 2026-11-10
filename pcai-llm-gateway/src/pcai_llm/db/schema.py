"""Database schema for the gateway control plane.

Design notes (informed by the AgentBuilder audit findings):
- Budget accounting is LEDGER-BASED: every request writes an immutable
  usage row; balances are derived with SUM over the ledger. No read-modify-
  write "remaining" column = no check-then-act race.
- API keys are stored HASHED (SHA-256). The plaintext is shown once at
  creation and never recoverable.
- MCP ACLs are per-tool (server-level allowlists are trivially bypassed by
  a server that gains one tool).
"""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- profiles MUST precede users: users.profile_id references it, and a
-- forward reference inside one multi-statement script fails on a fresh
-- database ("relation profiles does not exist" — audit finding #5).
CREATE TABLE IF NOT EXISTS profiles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,           -- 'user', 'admin', 'batch'
    -- Budget-driven cascade ordering: first model with headroom serves.
    -- Empty = no cascade (direct model resolution only).
    model_order TEXT[] NOT NULL DEFAULT '{}',
    -- Key-creation presets: {"budgets":[{model,period,limit_tokens}],
    -- "mcp_grants":[[server,tool],...]} — applied when a key is created
    -- with this profile (admin console "profiles as defaults").
    -- Each budgets entry carries exactly ONE limit kind: limit_tokens
    -- (stamped into budgets) OR limit_usd (stamped into cost_budgets);
    -- validated by core.budgets.split_preset_budgets at save AND at every
    -- stamp site, because this column is raw JSON that an older build or a
    -- hand edit can put anything into.
    defaults_json TEXT NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Migration for databases created before defaults_json existed (IF NOT
-- EXISTS keeps it idempotent on every boot).
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS defaults_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    profile_id  UUID REFERENCES profiles(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- Self-mint eligibility (SSO self-mint, admin-governed): TRUE = the user's
-- subject may mint/rotate a key via POST /v1/keys/for-me (the browser
-- self-service door). DEFAULT TRUE on every row — backward compatible:
-- existing users were minting-eligible before the column existed, and the
-- feature's own flag gates stay the primary off switch. The flag is
-- written ONLY by the admin API (PUT /admin/keys/self-mint-eligibility);
-- reads go through routes_identity._self_mint_allowed_for_subject, which
-- resolves subject → user row via the subject binding. NULLs never occur
-- (NOT NULL + server default), so the read is one row lookup.
ALTER TABLE users ADD COLUMN IF NOT EXISTS self_mint_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- SSO self-mint default profile (2026-10-08): the profile the /v1/keys/for-me
-- mint stamps onto every key it issues for THIS tenant (the mint's tenant is
-- tokenShadowTenantId — the SSO population home). NULL = the bare fixed mint
-- grant (proxy+mcp, 60rpm/100k tpm, nothing else) — byte-identical to
-- pre-feature behavior. The mint + rotate transactions stamp the profile's
-- budgets/limits/MCP grants onto the NEW key row (rotate creates a fresh row,
-- so re-stamping is what keeps grants alive across rotation); scopes are NOT
-- overridden (the mint's fixed least-privilege grant stays).
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sso_default_profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL;

-- SSO TIER PROFILES (2026-11-08): the self-mint profile can differ by the
-- caller's IdP groups. The PCAI admin tier is the literal string `admin` in
-- the Keycloak `groups` claim (the platform's own Rego tests
-- `"admin" in groups`); there is NO `member` string — member means "not
-- admin". sso_admin_profile_id is stamped when ANY entry of
-- sso_admin_groups (comma-separated, case-sensitive, whitespace-trimmed)
-- is present in the caller's groups; otherwise sso_default_profile_id
-- applies. Empty sso_admin_groups (the default) DISABLES the tier split:
-- resolution is byte-identical to the single-profile deployment, so an
-- upgrade never re-scopes an existing tenant.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sso_admin_profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sso_admin_groups TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS api_keys (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    key_hash     TEXT NOT NULL UNIQUE,          -- sha256 hex of the plaintext key
    key_prefix   TEXT NOT NULL,                 -- first 10 chars, for UI display
    scopes       TEXT[] NOT NULL DEFAULT '{}',  -- e.g. {proxy,mcp,admin}
    allowed_models TEXT[] NOT NULL DEFAULT '{}', -- empty = all registered models
    rate_limit_rpm  INTEGER NOT NULL DEFAULT 60,
    rate_limit_tpm  INTEGER NOT NULL DEFAULT 100000,
    -- Backpressure (NOT a fallback): max simultaneous in-flight requests.
    -- 0 = unlimited. RPM/TPM stop request-rate bursts; this stops a key
    -- from holding many long-lived streams open at low request rates.
    max_concurrent INTEGER NOT NULL DEFAULT 0,
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
-- mcp_admin (2026-10-26 hardening plan P0-3): the sqlhandler admin_* tools
-- (admin_grants / admin_key_mint / admin_key_revoke / admin_policy_set) are
-- reachable over the federated /mcp surface today for ANY key holding the
-- grant — including a model-driven key. Minting a key mints a credential
-- that outlives the session AND bypasses the gateway ACL entirely, so the
-- MCP surface refuses admin_* unless the key carries this explicit opt-in.
-- DEFAULT FALSE is the control: existing keys do NOT acquire the capability.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS mcp_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- ============================================================================
-- Identity (Phase A of the fleet identity architecture — the identity
-- keystone; review §1.2 DDL verbatim). ALL ADDITIVE / idempotent.
--
-- Subjects are the IDENTITY dimension, orthogonal to users (tenancy/policy
-- holders): api_keys.user_id stays NOT NULL and unchanged; users keep
-- running the budgets/profiles/profiles-as-defaults machinery. A subject is
-- the stable IdP claim a key can be BOUND to at an authenticated moment
-- (enrollment redemption / admin console; SSO-create + token-shadow arrive
-- Phase B), so the ledger, usage explorer, and relay attribution can answer
-- "which human/service made this call" beyond the key id. Existing keys keep
-- working pseudonymously with zero migration: subject_id is NULLABLE and a
-- NULL binding means enforcement is byte-for-byte today's behavior.
--
-- ``idp`` records WHERE the claim came from — 'self-asserted' (the redeemer
-- typed it), 'oauth2-proxy' (SSO headers on the redeem request), 'admin-
-- asserted' (console create), 'keycloak' (Phase B) — and that provenance is
-- surfaced honestly in the UI ("self-asserted" chip): a self-asserted
-- subject is a CLAIM, not a verified identity.
--
-- ``kind`` CHECK: 'user' | 'service' | 'anonymous' (the review vocabulary;
-- Phase A flows only ever write 'user').
-- ============================================================================
CREATE TABLE IF NOT EXISTS subjects (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID REFERENCES tenants(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('user','service','anonymous')),
    idp          TEXT NOT NULL,        -- 'keycloak' | 'oauth2-proxy' | 'admin-asserted'
    subject      TEXT NOT NULL,        -- stable IdP claim (sub, or email fallback)
    display_name TEXT NOT NULL DEFAULT '',
    groups       TEXT[] NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (idp, subject)
);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS subject_id UUID REFERENCES subjects(id) ON DELETE SET NULL;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS bound_via TEXT NOT NULL DEFAULT ''; -- enrollment|sso-create|admin-console|token-shadow
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS bound_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_api_keys_subject ON api_keys(subject_id);
-- ONE LIVE SSO-CREATE KEY PER SUBJECT, enforced BY THE DATABASE (audit
-- P2-13, 2026-11-10): the /v1/keys/for-me mint checks for an existing live
-- key before inserting, but check-then-INSERT is a race two concurrent first
-- mints both win — the double-mint leaves two live keys bound to one
-- subject. The partial unique index makes the loser FAIL instead (the route
-- maps the violation to the same 409 its pre-check raises — the
-- uq_topup_one_pending shape below). GATED creation: a database that already
-- holds duplicates (any pre-fix deployment can) must not fail its whole boot
-- on an uncreatable index — the duplicates are counted first and, when any
-- exist, the index is skipped with a loud WARNING for the operator to
-- deduplicate; the next boot after that creates it. NULL subject_id rows are
-- naturally exempt (unique indexes treat NULLs as distinct).
DO $$
DECLARE
    dup_subjects INTEGER;
BEGIN
    SELECT count(*) INTO dup_subjects FROM (
        SELECT subject_id FROM api_keys
        WHERE bound_via = 'sso-create' AND revoked_at IS NULL AND subject_id IS NOT NULL
        GROUP BY subject_id HAVING count(*) > 1
    ) dup;
    IF dup_subjects > 0 THEN
        RAISE WARNING 'uq_api_keys_one_live_sso NOT created: % subject(s) already hold multiple live sso-create keys — deduplicate them and reboot to enforce one-live-key-per-subject (audit P2-13)', dup_subjects;
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS uq_api_keys_one_live_sso
            ON api_keys(subject_id) WHERE bound_via = 'sso-create' AND revoked_at IS NULL;
    END IF;
END $$;

-- Spend lineage (budget-carry across rotation, 2026-10-09): every rotate
-- sets the NEW key's spend_ancestors = old.spend_ancestors || [old.id], so
-- the budget gates can sum the WHOLE lineage's ledger spend. Without this a
-- rotation zeroed the period-budget counter (spend is DERIVED — SUM over
-- usage_ledger keyed by key_id, and a fresh key_id has no rows), turning
-- "Sign in with SSO" rotation into an in-window budget reset a user could
-- repeat indefinitely. The window predicate (ts >= window_start) is NOT
-- carried — calendars reset normally; only the in-window counter survives.
-- Denormalized array (not a follow-the-chain join) so the sums stay correct
-- even if a predecessor row is later hard-deleted, and so in-flight settles
-- landing on the old id AFTER the rotate commits are still counted.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS spend_ancestors UUID[] NOT NULL DEFAULT '{}';

-- Key group (conversation continuity across rotation, 2026-11-03): the
-- stable identity of "this credential" across rotations. Every rotate copies
-- the OLD key's key_group onto the new row, so a chain A -> B -> C is ONE
-- group even though it is three key ids. Backfill derives the group from the
-- spend_ancestors lineage (its first entry IS the chain root) so rotations
-- that predate this column group correctly; a key with no ancestors is its
-- own root.
--
-- WHY not scope saved conversations by key_id alone: rotation revokes the
-- old row and mints a new id, and the chat console's history is keyed by the
-- credential — so every rotation silently orphaned the user's transcripts
-- ("Conversations aren't saved for the key after rotation", operator report).
-- The group is the narrowest identity that survives a rotation: it does NOT
-- widen visibility across a user's INDEPENDENT keys (those get their own
-- group), which user_id scoping would.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_group UUID;
-- One-shot backfill, GATED on actual state (audit P2-27, 2026-11-10): bare,
-- both statements re-plan on EVERY boot inside the one implicit transaction
-- SCHEMA_SQL runs as — the UPDATE full-scans api_keys to prove no NULLs
-- remain, and SET NOT NULL re-validates with its own scan. The gates turn a
-- steady-state boot into a cheap probe (idx_api_keys_group, created below,
-- indexes NULLs) that touches no rows; the work happens exactly once, on the
-- boot that actually finds un-backfilled rows.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM api_keys WHERE key_group IS NULL) THEN
        UPDATE api_keys SET key_group = COALESCE(spend_ancestors[1], id) WHERE key_group IS NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'api_keys' AND column_name = 'key_group'
                 AND is_nullable = 'YES') THEN
        ALTER TABLE api_keys ALTER COLUMN key_group SET DEFAULT gen_random_uuid();
        ALTER TABLE api_keys ALTER COLUMN key_group SET NOT NULL;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_api_keys_group ON api_keys(key_group);

-- Embedding cache: dedupes identical (model, input) embedding calls.
-- MultimodalRAG and other clients re-embed the same chunks constantly
-- (re-ingestion, re-chunking, repeated queries); a cached hit costs zero
-- upstream GPU work and zero budget. Keyed on sha256(model + normalized
-- input); vectors stored as JSON (portable, no pgvector dependency).
-- input_bytes caps stored size — giant inputs are not worth caching.
CREATE TABLE IF NOT EXISTS embedding_cache (
    cache_key    TEXT PRIMARY KEY,             -- sha256 hex of model+input
    model        TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    embedding    JSONB NOT NULL,               -- the vector (array of floats)
    input_bytes  INTEGER NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_embcache_model ON embedding_cache(model, created_at DESC);

-- Exact-match RESPONSE cache for non-streaming chat (2026-11-02).
-- Distinct from embedding_cache (vectors) and from the upstream PREFIX cache
-- (which only saves re-prefill): a hit here skips the model entirely - zero
-- GPU, zero budget - and settles a free cache_hit ledger row.
-- Keyed on (key_id, model, sha256 of the canonical request), so one tenant's
-- answer can never be served to another, and the body is stored as BYTEA so
-- replay is byte-identical to what the upstream returned.
CREATE TABLE IF NOT EXISTS response_cache (
    key_id        UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    model         TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    response_body BYTEA NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (key_id, model, request_hash)
);
CREATE INDEX IF NOT EXISTS idx_response_cache_expiry ON response_cache(expires_at);

CREATE TABLE IF NOT EXISTS budgets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id      UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    model       TEXT NOT NULL DEFAULT '*',      -- '*' = any model
    period      TEXT NOT NULL CHECK (period IN ('day','week','month')),
    limit_tokens BIGINT NOT NULL CHECK (limit_tokens > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (key_id, model, period)
);
CREATE INDEX IF NOT EXISTS idx_budgets_key ON budgets(key_id);

-- USD budgets (parallel to token budgets): whichever limit hits first stops
-- the key. Cost spend = SUM(usage_ledger.cost_usd) over the same windows.
CREATE TABLE IF NOT EXISTS cost_budgets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id      UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    model       TEXT NOT NULL DEFAULT '*',
    period      TEXT NOT NULL CHECK (period IN ('day','week','month')),
    limit_usd   NUMERIC(12,4) NOT NULL CHECK (limit_usd > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (key_id, model, period)
);
CREATE INDEX IF NOT EXISTS idx_cost_budgets_key ON cost_budgets(key_id);

-- 2026-11-02: quarterly/yearly budget periods. The inline CHECK above cannot
-- be widened on an existing database, so both tables re-state it (idempotent:
-- DROP IF EXISTS then ADD). 'hour' is deliberately NOT added - the rate
-- limiter already owns per-minute windows, and an hourly BUDGET has no
-- consumer.
-- NOT VALID (audit P2-27, 2026-11-10): without it the ADD re-validates EVERY
-- existing row on EVERY boot — a full scan under ACCESS EXCLUSIVE inside the
-- one implicit transaction SCHEMA_SQL runs as. The check is only ever
-- WIDENED, so pre-existing rows satisfy it by construction; NOT VALID keeps
-- the re-add catalog-only (instant), same discipline as ck_ledger_status
-- below (pinned by tests/test_deploy_contract.py). VALIDATE left to the
-- operator if ever desired.
ALTER TABLE budgets DROP CONSTRAINT IF EXISTS budgets_period_check;
ALTER TABLE budgets ADD CONSTRAINT budgets_period_check
    CHECK (period IN ('day','week','month','quarter','year')) NOT VALID;
ALTER TABLE cost_budgets DROP CONSTRAINT IF EXISTS cost_budgets_period_check;
ALTER TABLE cost_budgets ADD CONSTRAINT cost_budgets_period_check
    CHECK (period IN ('day','week','month','quarter','year')) NOT VALID;

CREATE TABLE IF NOT EXISTS usage_ledger (
    -- PARTITIONING NOTE (2026-09-24): created UNPARTITIONED for zero-friction
    -- first boot; at scale this table wants MONTHLY RANGE partitions on ts.
    -- The conversion is a deliberate run-once operation — scripts/partition_ledger.py —
    -- NOT startup DDL: SCHEMA_SQL runs on every boot, and an online
    -- partition conversion rewrites/locks the table; doing that per-boot on a
    -- live ledger is unacceptable. The startup checker (db/__init__.py
    -- _check_ledger_partitioning) warns once the UNPARTITIONED table passes a
    -- row threshold. Partition shape the script builds (and every query here
    -- already assumes): PARTITION BY RANGE (ts), PK (id, ts) — verified: NO
    -- query in this tree addresses ledger rows by bare id (all access is
    -- key_id/ts-filtered or aggregate), so the widened PK is safe; the
    -- (key_id, ts) indexes survive per-partition and the budget gate's SUMs
    -- PRUNE to the current-window partitions (day/week/month windows only
    -- ever touch 1-2 partitions instead of scanning the whole heap).
    id          BIGSERIAL PRIMARY KEY,
    key_id      UUID NOT NULL,
    model       TEXT NOT NULL,
    endpoint    TEXT NOT NULL,                  -- chat|embeddings|mcp:<server>:<tool>
    prompt_tokens     BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens      BIGINT NOT NULL DEFAULT 0,
    -- tokens served from cache: upstream prompt-cache (vLLM cached_tokens)
    -- and full embedding-cache hits (status='cache_hit' rows)
    cached_tokens     BIGINT NOT NULL DEFAULT 0,
    -- USD cost computed at REQUEST time from model_costs (NULL = unpriced)
    cost_usd          NUMERIC(12,6),
    status      TEXT NOT NULL,                  -- ok|error|cache_hit|budget_exceeded|rate_limited|guardrail_blocked
    latency_ms  INTEGER,
    request_id  TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- CHECK constraints (audit finding 20): the status vocabulary was enforced
-- only by writer discipline — a typo'd status silently broke the budget
-- gate's SUM semantics (core.budgets._COSTED_STATUSES: ok/error/
-- guardrail_blocked — a blocked call still spent real upstream tokens) and the PrometheusRule's
-- status alerting. Additive + idempotent (NOT VALID keeps the migration
-- instant even on a huge ledger; existing rows are trusted, new writes
-- are checked — VALIDATE later if desired).
ALTER TABLE usage_ledger DROP CONSTRAINT IF EXISTS ck_ledger_status;
ALTER TABLE usage_ledger ADD CONSTRAINT ck_ledger_status
    CHECK (status IN ('ok','error','cache_hit','budget_exceeded','rate_limited','guardrail_blocked','policy_blocked','denied','routing')) NOT VALID;
-- 'routing' (P10): the decision model's OWN call, settled under the calling
-- key with its real upstream usage. It is PRICED (settle bills it) but kept
-- OUT of budgets._COSTED_STATUSES: the budget SUMs must not charge the user's
-- token/completion envelope for the routing turn — only the USD chargeback
-- sees it. The vocabulary lives here (and in scripts/partition_ledger.py's
-- stamp — pinned identical by tests/test_deploy_contract.py).
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS cached_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12,6);

-- Per-model prices (USD per 1M units — the industry convention for tokens;
-- P10 widens "token" to a unit vocabulary: 'token'|'char'|'second'|'image'|
-- 'request', so a voice model prices per 1M SECONDS of audio, an image model
-- per 1M images, and a flat-fee model per 1M requests). Absent row = unpriced
-- model (cost NULL). Existing rows default to 'token' -> zero behaviour change
-- (the token path never reads the column for its arithmetic).
CREATE TABLE IF NOT EXISTS model_costs (
    model_name        TEXT PRIMARY KEY,
    input_per_m       NUMERIC(12,8) NOT NULL DEFAULT 0,
    output_per_m      NUMERIC(12,8) NOT NULL DEFAULT 0,
    cached_per_m      NUMERIC(12,8) NOT NULL DEFAULT 0,
    -- billing unit the rates are denominated in (P10). 'token' rows price
    -- exactly as always (costs.compute); the unit-aware paths (audio seconds,
    -- images, ...) bill through costs.compute_units, which refuses — and the
    -- guard REPORTS — a row whose unit does not match the unit its route bills.
    unit              TEXT NOT NULL DEFAULT 'token',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- migration from the hours-old per-1k naming (unpriced in production).
-- GUARDED: RENAME has no IF NOT EXISTS — on any database created with (or
-- already migrated to) the per-m names these would crash startup with
-- UndefinedColumnError. Guarded renames are idempotent on every DB state:
-- fresh (per-m from CREATE TABLE), per-1k era (renamed now), already-migrated.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'model_costs' AND column_name = 'input_per_1k') THEN
        ALTER TABLE model_costs RENAME COLUMN input_per_1k TO input_per_m;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'model_costs' AND column_name = 'output_per_1k') THEN
        ALTER TABLE model_costs RENAME COLUMN output_per_1k TO output_per_m;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'model_costs' AND column_name = 'cached_per_1k') THEN
        ALTER TABLE model_costs RENAME COLUMN cached_per_1k TO cached_per_m;
    END IF;
END $$;

-- P10 billing unit (see the CREATE TABLE above): every pre-existing row is
-- 'token' by construction, which is exactly what it billed before — zero
-- behaviour change. ADD COLUMN IF NOT EXISTS keeps a second boot (and a DB
-- created with the column already inline) a no-op, same discipline as the
-- usage_ledger evolutions below.
ALTER TABLE model_costs ADD COLUMN IF NOT EXISTS unit TEXT NOT NULL DEFAULT 'token';

-- The unit vocabulary, DO-block-gated (the guarded-rename block above is the
-- file's precedent for DDL that has no IF NOT EXISTS form). TWO hardening
-- rules (cross-validation wave 2, 2026-11-10), the same discipline as every
-- other re-added CHECK in this file (ck_ledger_status, budgets_period_check,
-- guardrails_engine_check):
--   * NOT VALID. SCHEMA_SQL is ONE implicit transaction, so a VALID add
--     full-scans model_costs inside the boot transaction and ONE legacy
--     out-of-vocabulary row (unit='seconds', e.g. written by an older build
--     before this vocabulary existed) would roll the ENTIRE boot DDL back
--     — a crash loop on every replica, no in-product repair. NOT VALID
--     fences new writes immediately; the operator VALIDATEs at leisure.
--   * Gated on the constraint DEFINITION (pg_get_constraintdef), not mere
--     existence: a constraint left by an older vocabulary (the plural
--     'seconds') would otherwise survive boot and reject the new singular
--     vocabulary at write time with a 500. Compare, drop, re-add.
DO $$
DECLARE
    _def TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO _def
      FROM pg_constraint
     WHERE conname = 'ck_model_costs_unit'
       AND conrelid = 'model_costs'::regclass;
    IF _def IS NULL
       OR position('NOT VALID' in _def) = 0
       OR position('token' in _def) = 0
       OR position('char' in _def) = 0
       OR position('second' in _def) = 0
       OR position('image' in _def) = 0
       OR position('request' in _def) = 0
       OR position('seconds' in _def) > 0
    THEN
        IF _def IS NOT NULL THEN
            ALTER TABLE model_costs DROP CONSTRAINT ck_model_costs_unit;
        END IF;
        ALTER TABLE model_costs ADD CONSTRAINT ck_model_costs_unit
            CHECK (unit IN ('token','char','second','image','request')) NOT VALID;
    END IF;
END $$;

-- Per-model off-peak pricing-ratio overrides (console-editable, the Off-peak
-- panel). Absent row = the model bills at its profile's fraction, else the
-- gateway default (settings.offpeak_fraction). Ratio in (0, 1]: 0.5 bills
-- half price during the model's own low-demand window. Runtime-editable (the
-- evaluator caches it; admin writes invalidate) — unlike offpeak_model_promql,
-- which stays env-config because it needs validated PromQL.
CREATE TABLE IF NOT EXISTS offpeak_model_config (
    model_name        TEXT PRIMARY KEY,
    fraction          REAL NOT NULL CHECK (fraction > 0 AND fraction <= 1),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ledger_key_ts ON usage_ledger(key_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_ledger_key_model_ts ON usage_ledger(key_id, model, ts DESC);
-- ts-leading (audit P1-4): the console rollups aggregate over time windows;
-- without a ts-led index every summary/rollup is a full scan + sort of a
-- never-pruned table on the pool the data plane shares.
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON usage_ledger(ts DESC);

CREATE TABLE IF NOT EXISTS rate_windows (
    key_id     UUID NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    requests   INTEGER NOT NULL DEFAULT 0,
    tokens     BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, window_start)
);
-- window_start-leading (perf round 2026-10-16, both explorers confirmed):
-- housekeeping's 60s prune deletes WHERE window_start < now() - '2 minutes';
-- the PK leads with key_id, so that predicate full-scanned a table growing
-- one row per key per minute. Same discipline as idx_ledger_ts above.
CREATE INDEX IF NOT EXISTS idx_rate_windows_start ON rate_windows(window_start);

CREATE TABLE IF NOT EXISTS model_endpoints (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name  TEXT NOT NULL,                  -- virtual name served to consumers
    priority    INTEGER NOT NULL DEFAULT 0,     -- lower = preferred
    weight      INTEGER NOT NULL DEFAULT 1,
    base_url    TEXT NOT NULL,                  -- e.g. http://vllm-svc.ns.svc.cluster.local/v1
    api_key_secret TEXT,                        -- name of K8s Secret holding the upstream key (by-ref)
    -- Fernet-encrypted upstream key (admin panel path); decrypts with
    -- PCAI_LLM_SECRETS_MASTER_KEY. NULL = unauthenticated endpoint.
    auth_token_enc TEXT,
    -- Routing type: chat|embedding|reranker|voice — /v1/chat/completions
    -- only serves 'chat' pools; /v1/embeddings only 'embedding' pools
    -- ("note type": a chat request must never hit an embedding endpoint).
    model_type TEXT NOT NULL DEFAULT 'chat',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (model_name, base_url)
);

CREATE TABLE IF NOT EXISTS mcp_servers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    -- Operator-authored one-liner: what this server is FOR. Rendered to the
    -- model in the hybrid tool surface's server directory (2026-10-28), which
    -- is what lets a model pick a server without a discovery round trip.
    -- Empty is fine (the directory then shows name + tool count).
    description TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL,                  -- e.g. http://applygate-mcp.ns.svc.cluster.local:9102/mcp
    headers_json TEXT NOT NULL DEFAULT '{}',    -- static headers (never secrets inline; by-ref names)
    -- Bearer auth: K8s Secret NAME (by-ref); resolved from env
    -- PCAI_LLM_MCP_KEY__<NAME> at call time — the fleet MCP servers
    -- (applygate et al.) all require their API key even in-cluster.
    auth_secret TEXT,
    -- Fernet-encrypted Bearer token (admin panel path), same master key.
    auth_token_enc TEXT,
    -- Tool policy: 'all' = every tool the server exposes is grantable;
    -- 'allowlist' = only tools in allowed_tools_json are grantable (and
    -- callable, defense-in-depth on top of the per-key ACL pairs).
    tool_policy TEXT NOT NULL DEFAULT 'all',
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS auth_secret TEXT;
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS auth_token_enc TEXT;
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS tool_policy TEXT NOT NULL DEFAULT 'all';
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS allowed_tools_json TEXT NOT NULL DEFAULT '[]';
-- Fleet priority (lower = preferred — the SAME convention as
-- model_endpoints.priority): orders the registry snapshot (hence
-- mcp_catalog output and tools/list) and the flattened-call failover chain.
-- Additive migration: every existing row lands at the neutral default 0 and
-- keeps its previous (name ASC) relative order.
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;
-- 2026-10-28: operator-authored one-liner shown to the model in the hybrid
-- tool surface's server directory. The CREATE TABLE above only runs on a
-- FRESH database, so an existing deployment needs this ALTER or the column
-- never appears and the registry's row.get() silently yields "".
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '';
-- Migration: servers registered while the default was 'allowlist'+EMPTY admit
-- NOTHING (every grant picker offered zero tools — the 'can't add mcp servers'
-- incident). An EMPTY allowlist carries no operator intent, so such rows flip
-- to 'all'; allowlists the admin actually built are preserved.
UPDATE mcp_servers SET tool_policy = 'all'
WHERE allowed_tools_json IN ('[]', '', NULL) AND tool_policy <> 'all';
-- Tool-relay governance, in UNITS — not model rounds (2026-11-03).
-- The relay's per-turn bound used to be a COUNT of model iterations
-- (settings.tool_relay_max_iterations). A count cannot express "this server
-- is expensive": one call to a heavyweight server can cost far more upstream
-- work than ten calls to a cheap one, and a count charged both the same. The
-- bound is therefore a BUDGET denominated in UNITS, where every call charges
-- its server's tool_weight (below) — a 0.1-weight call spends 0.1 units, so a
-- cheap server is ~10x cheaper against the same budget. Fractional on
-- purpose; a budget of 8.0 is "eight neutral calls", not "eight rounds".
--
-- profiles.tool_relay_budget: the per-profile override. NULL = INHERIT
-- settings.tool_relay_max_iterations (read as a unit budget), so every
-- existing profile keeps today's NUMBER until an operator sets one — but the
-- UNIT changed with it: a batch of k parallel calls now costs k, not 1, so a
-- parallel-heavy turn is bounded TIGHTER than before. INTEGER on purpose: an operator types "8", not "8.0"; the fractional
-- side of the arithmetic lives in the weights, not in the budget.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS tool_relay_budget INTEGER;
-- mcp_servers.tool_weight: what ONE call to this server charges, in the same
-- units. REAL (not INTEGER) is the whole point: 0.1 is the useful shape for a
-- cheap server ("a tenth of a neutral call"). 1.0 = neutral, and it is the
-- NOT NULL default, so every pre-existing row charges one unit per call:
-- the old NUMBER, on the new (call) unit. The admin API floors the value at 0.01 — a 0 weight would make a
-- server's calls FREE and let a loop on it run to the wall-clock bound alone.
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS tool_weight REAL NOT NULL DEFAULT 1.0;
ALTER TABLE model_endpoints ADD COLUMN IF NOT EXISTS auth_token_enc TEXT;
ALTER TABLE model_endpoints ADD COLUMN IF NOT EXISTS model_type TEXT NOT NULL DEFAULT 'chat';

CREATE TABLE IF NOT EXISTS mcp_tool_acls (
    key_id      UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    server      TEXT NOT NULL,
    tool        TEXT NOT NULL,
    allowed     BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (key_id, server, tool)
);
CREATE INDEX IF NOT EXISTS idx_acls_key ON mcp_tool_acls(key_id);

-- ============================================================================
-- 0.3.x feature wave — agent traces, saved conversations, self-service
-- enrollment, per-key MCP credential overrides. ALL ADDITIVE: IF NOT EXISTS
-- everywhere, so every database state (fresh, any prior release) applies clean.
-- ============================================================================

-- Agent observability: one row per relay step — one per model iteration
-- (phase 'chat': wall-clock ms + observed usage) and one per tool call
-- (phase 'tool': server/tool/ms/status). Grouped by request_id this is the
-- per-user "what did the agent do and how long did it take" trace.
-- Written BEST-EFFORT: a trace write failure must never fail the request
-- (same discipline as the rate_windows prune).
CREATE TABLE IF NOT EXISTS agent_traces (
    id          BIGSERIAL PRIMARY KEY,
    request_id  TEXT NOT NULL,
    key_id      UUID NOT NULL,
    user_id     UUID,
    model       TEXT NOT NULL DEFAULT '',
    iteration   INT  NOT NULL DEFAULT 0,
    phase       TEXT NOT NULL,                 -- chat | tool
    server      TEXT NOT NULL DEFAULT '',
    tool        TEXT NOT NULL DEFAULT '',
    ms          INT,
    prompt_tokens     BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'ok',    -- ok|error|denied|rate_limited|budget_exceeded|handed_back
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE agent_traces DROP CONSTRAINT IF EXISTS ck_traces_status;
ALTER TABLE agent_traces ADD CONSTRAINT ck_traces_status
    CHECK (status IN ('ok','error','denied','rate_limited','budget_exceeded','handed_back')) NOT VALID;
CREATE INDEX IF NOT EXISTS idx_traces_request ON agent_traces(request_id);
CREATE INDEX IF NOT EXISTS idx_traces_key_ts  ON agent_traces(key_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_traces_user_ts ON agent_traces(user_id, ts DESC);
-- ts-leading (audit A-3, 2026-11-02): the admin/team explorer filters on ts
-- ONLY when no actor filter is given (its since defaults to 30 days) and then
-- groups by request_id. Without this every such page is a sequential scan +
-- hash aggregate over a table that is NEVER pruned by default
-- (retention_trace_days = 0), on the pool the data plane shares. Exactly the
-- idx_ledger_ts shape added for the same reason (audit P1-4).
CREATE INDEX IF NOT EXISTS idx_traces_ts      ON agent_traces(ts DESC);
-- Conversation grouping: a stable fingerprint of (key, first user message).
-- Harness loops (DSH) re-send the accumulated history every step, so one
-- long multi-turn conversation spans MANY request_ids — the thread key is
-- what groups them into one trace conversation.
ALTER TABLE agent_traces ADD COLUMN IF NOT EXISTS thread_id TEXT;
-- Prefix-cache hits reported by the upstream for this step (2026-11-02).
-- The trace table is the "what did the agent do" surface; cached tokens are
-- what makes the conversation's INPUT cost legible (a 98%-cached turn and a
-- cold turn look identical on prompt_tokens alone).
ALTER TABLE agent_traces ADD COLUMN IF NOT EXISTS cached_tokens BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_traces_thread ON agent_traces(thread_id);
-- Conversation READ path (2026-11-02): the explorer picks the N most recent
-- conversations under the actor filters and then reads EVERY row of them.
-- The thread_id-only index above still earns its keep for the UNSCOPED
-- selection (an index-ordered GROUP BY thread_id avoids the sort), but it
-- cannot serve the scoped one: with a key_id (or enrollment) filter the
-- selection is "newest conversations of THIS key", and the fetch that
-- follows is a ts-ordered read per thread. Shape mirrors
-- idx_effective_prompts_thread (effective_prompts: key_id, thread_id, created_at).
CREATE INDEX IF NOT EXISTS idx_traces_key_thread_ts ON agent_traces(key_id, thread_id, ts DESC);

-- Saved conversations (per API key): server-side transcripts so the chat
-- console's left panel survives tabs and devices. The client pushes the
-- full transcript per turn (replace-all — idempotent, no partial state);
-- size caps are enforced app-side (≤200 messages, ≤32KB each).
CREATE TABLE IF NOT EXISTS conversations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id     UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    user_id    UUID,
    -- The KEY GROUP that owns this transcript (rotation-stable; see the
    -- api_keys.key_group migration above). key_id stays as provenance —
    -- "which credential created this row" — and keeps the cascade FK.
    key_group  UUID,
    title      TEXT NOT NULL DEFAULT 'New conversation',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_key ON conversations(key_id, updated_at DESC);
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS key_group UUID;
-- Backfill from the creating key's group; every existing row has a key_id.
-- One-shot, GATED on actual state (audit P2-27, 2026-11-10): this is the
-- TRANSCRIPT table — on the first post-upgrade boot the backfill legitimately
-- rewrites every row, but bare, the statement also re-plans on every boot
-- AFTER that, full-scanning the biggest table in the schema to prove no NULLs
-- remain, inside the one implicit transaction SCHEMA_SQL runs as while it
-- holds ACCESS EXCLUSIVE on ~20 tables (every other pod's ledger inserts and
-- key lookups queue behind it; HPA replays it per new pod). The gate makes a
-- steady-state boot an index probe (idx_conversations_group, created below,
-- indexes NULLs) that touches no rows.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM conversations WHERE key_group IS NULL) THEN
        UPDATE conversations c SET key_group = k.key_group
          FROM api_keys k WHERE c.key_id = k.id AND c.key_group IS NULL;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'conversations' AND column_name = 'key_group'
                 AND is_nullable = 'YES') THEN
        -- SET DEFAULT must precede SET NOT NULL (cross-validation P1,
        -- reproduced on PG 18): NOT NULL-without-default means (a) any row
        -- inserted between the backfill UPDATE's snapshot and this ALTER —
        -- an old-version pod mid rolling-upgrade writes no key_group —
        -- makes the ALTER fail "column contains null values", and
        -- SCHEMA_SQL being one implicit transaction, the WHOLE schema
        -- apply (hence the boot) rolls back; HPA replays the race per new
        -- pod. (b) every pre-upgrade writer that does not name the column
        -- 500s on save until replaced. The server-side default makes the
        -- column self-sufficient for any writer, exactly the api_keys
        -- shape above.
        ALTER TABLE conversations ALTER COLUMN key_group SET DEFAULT gen_random_uuid();
        ALTER TABLE conversations ALTER COLUMN key_group SET NOT NULL;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_conversations_group ON conversations(key_group, updated_at DESC);

-- Session distiller state (2026-10): one row per distilled conversation.
-- Mirrors the DSH plugin's session lifecycle: a conversation is distilled
-- when it goes quiet and RE-DISTILLED when it continues (the memory tracks
-- the conversation's latest state). distilled_at is the last distill time;
-- distilled_seq is the MESSAGE COUNT at distill time (all call sites pass
-- msg_count) - a conversation whose current message count EXCEEDS it is
-- stale and due for re-distill. (Do NOT pass a transcript length here; the
-- name says seq, the unit is messages - DeepSeek-V4.1 review note.)
-- housekeeping-driven; the routes surface seeds/refreshes rows on explicit
-- calls so an off-peak schedule and an interactive distill never fight.
-- Distiller runtime config (2026-10): the admin panel's override layer over
-- the env defaults (core.distiller_config DEFAULTS). Key-value rows; a key's
-- presence IS the override, deletion (admin null) returns to the default.
-- Canonical-store distill marker (2026-10): relay-captured conversations
-- (core.canonical, fed automatically by EVERY harness that relays through the
-- gateway - DSH included, zero client changes) distilled by the scheduled
-- sweep. Keyed (key_id, thread_id) like the store row itself; the marker is
-- the "already distilled at this transcript state" record.
CREATE TABLE IF NOT EXISTS canonical_distills (
    key_id              UUID NOT NULL,
    thread_id           TEXT NOT NULL,
    distilled_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- the canonical ROW's updated_at AT distill time (the state snapshot):
    -- the due query compares THIS against the row's CURRENT updated_at, so
    -- a write landing in the same clock tick as the marker can never mark
    -- an updated row as distilled (DeepSeek-V4.1 P1)
    distilled_row_state TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key_id, thread_id)
);

-- ATTACHMENT-GATE BACKOFF (2026-10-09 production stall). A row whose key
-- fails the memory attachment gate is deliberately NOT marked distilled
-- (the marker means "a write landed"), but an unmarked row is re-selected
-- OLDEST-FIRST every pass -- so when the gate itself is broken (empty
-- registry snapshot: resolve_memory_server -> None for EVERY key) the same
-- LIMIT-20 window is retried forever and nothing newer is ever reached.
-- This column records the last gate skip so the due query can suppress that
-- row for a bounded window, letting the queue advance. NULL = never gate
-- skipped (the normal case). Cleared on a successful distill.
ALTER TABLE canonical_distills ADD COLUMN IF NOT EXISTS gate_failed_at TIMESTAMPTZ;

-- UNCHANGED-CONTENT SKIP MARKER (2026-10-11 feature; migration added 2026-10-14
-- after a production stall). The sha256 of the transcript the last distill
-- CONSUMED: a continued conversation that re-renders byte-identical text
-- refreshes its marker and skips the map-reduce instead of re-distilling.
-- The v2.5.0 image shipped the READ and WRITE of this column WITHOUT this
-- migration, so distill_schedule.due_canonical_conversations raised
-- "column d.distilled_content_hash does not exist" on EVERY sweep -- swallowed
-- at log.debug, so the only trace was the Postgres error log, and the
-- automatic memory feed stopped cluster-wide (see CHANGELOG 2026-10-14).
ALTER TABLE canonical_distills ADD COLUMN IF NOT EXISTS distilled_content_hash TEXT;

CREATE TABLE IF NOT EXISTS distiller_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,  -- scalars only (model str / bools / ints)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Model-routing operator config (the Routing panel; core.routing_config).
-- Same kv shape as distiller_config above and the same discipline: the env
-- flags are deployment defaults, these rows are the operator override layer,
-- one row per knob, absent row = default. WHY a table and not env: the
-- allowed-model set and the decision-model PROMPT are things an operator must
-- be able to iterate on against live shadow-ledger traffic without a redeploy
-- — and the prompt is operator prose, not a value a chart should carry.
-- value stays TEXT (not JSONB) because it is a tagged scalar per row ("b:"/
-- "i:"/"s:" tags, plus "j:" for the one list — allowed_models): the tags are
-- what stop asyncpg's bool-through-TEXT round-trip from inverting a flag
-- (see core.routing_config._encode).
CREATE TABLE IF NOT EXISTS routing_config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS conversation_distills (
    conversation_id UUID PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    key_id          UUID NOT NULL,
    distilled_seq   INT  NOT NULL,
    distilled_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Attachment-gate backoff, same rationale as canonical_distills.gate_failed_at
-- (2026-10-09): a gate-skipped conversation must not pin the oldest-first
-- LIMIT window while the gate is broken.
ALTER TABLE conversation_distills ADD COLUMN IF NOT EXISTS gate_failed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS conversation_messages (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq             INT  NOT NULL,
    role            TEXT NOT NULL,             -- user | assistant
    content         TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, seq)
);

-- Compressed tool-result originals (X-PCAI-Compress-Tools, 2026-11-04).
-- The model sees a compressed result plus a marker; this table holds the
-- exact original so the model can call pcai__retrieve and page it back.
-- KEY-SCOPED by primary key: a digest resolves only for the key that
-- produced it (a globally addressable store would be a cross-tenant read
-- primitive). Expiry is housekeeping's job (tool_compress_store_ttl_s),
-- never the lookup's: a row that outlives its TTL is still correct to
-- serve, just overdue for reclamation.
CREATE TABLE IF NOT EXISTS tool_compress_store (
    key_id     UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    digest     TEXT NOT NULL,
    body       TEXT NOT NULL,
    chars      INT  NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key_id, digest)
);
CREATE INDEX IF NOT EXISTS tool_compress_store_created_at
    ON tool_compress_store (created_at);

-- Self-service key enrollment (invite codes): the admin mints a code bound
-- to tenant+profile; anyone holding it redeems at POST /v1/enroll for a
-- personal key with the profile's budgets/cascade/grants applied. Codes are
-- stored HASHED (same discipline as api_keys — plaintext shown once).
CREATE TABLE IF NOT EXISTS enrollments (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code_hash  TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL DEFAULT '',
    tenant_id  UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    profile_id UUID REFERENCES profiles(id) ON DELETE SET NULL,
    max_uses   INT  NOT NULL DEFAULT 1,        -- strictly positive; 0 is refused (2026-10-30)
    uses       INT  NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- Team leader tier (2026-10-30). An enrollment code is a TEAM INVITE: every
-- key redeemed from it carries api_keys.enrollment_id, which groups those keys
-- into one budget ENVELOPE (a HARD cap on the team's SUM) and gives the
-- enrollment a scoped control plane at /team_leader over exactly its own keys,
-- usage and traces. ALL ADDITIVE / idempotent, same discipline as every
-- migration above.
-- ============================================================================

-- Which enrollment (team) a key belongs to. ON DELETE SET NULL, never CASCADE:
-- deleting an enrollment must not delete the keys its teammates are still
-- holding (usage_ledger references them, and an orphaned key would be a
-- 500-shaped hole in the data plane) — the keys simply become un-grouped,
-- which is exactly the pre-feature behavior. NULL = outside any team, which is
-- every key that predates this migration.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS enrollment_id UUID REFERENCES enrollments(id) ON DELETE SET NULL;
-- Serves the team console's "keys in this enrollment" list and team_spend's
-- per-enrollment ledger JOIN (both filter api_keys BY enrollment_id).
CREATE INDEX IF NOT EXISTS idx_api_keys_enrollment ON api_keys(enrollment_id);

-- The team budget envelope. NULL = that cap is NOT set; an enrollment with
-- both NULL enforces nothing (its keys behave exactly as before — the grouping
-- is then reporting-only). budget_period is validated APP-SIDE against
-- core.teams.TEAM_PERIODS (day|week|month) rather than by a CHECK constraint,
-- matching this file's tolerance for values written by older builds and raw
-- JSON presets: a bad value normalizes to 'month' at READ time, whereas a
-- CHECK would fail the whole multi-statement schema apply on a live database.
-- 2026-10-30: max_uses is STRICTLY POSITIVE — 0 no longer means "unlimited"
-- (it is EXHAUSTED, and the redeem predicate fails closed on it). The CREATE
-- TABLE default only applies to a fresh database, so an existing deployment
-- needs this ALTER to stop minting unlimited codes by omission.
ALTER TABLE enrollments ALTER COLUMN max_uses SET DEFAULT 1;

ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS budget_tokens BIGINT;          -- NULL = no team token cap
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS budget_usd NUMERIC(12,4);      -- NULL = no team USD cap
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS budget_period TEXT NOT NULL DEFAULT 'month';  -- day|week|month
-- TRUE (the default) = the UNALLOCATED remainder of the envelope is a shared
-- overflow pool: a key may exceed its own allocation while the team still has
-- headroom. FALSE = each key is hard-capped at its own allocation (the
-- envelope still caps the team). Default TRUE keeps an existing team usable
-- without minting a per-key allocation for every teammate.
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS overflow_enabled BOOLEAN NOT NULL DEFAULT TRUE;

-- The team leader key: a control-plane credential scoped to ONE enrollment
-- (the analog of the admin key, not of an api_key). HASH-ONLY, exactly like
-- api_keys.key_hash and enrollments.code_hash — the plaintext is returned once
-- at mint/rotate and is never stored, logged or projected. prefix +
-- created_at exist so the console can show WHICH credential is live;
-- revoked_at (not a row delete) is the kill switch core.teams.authenticate_leader
-- enforces in SQL, so a revoke keeps its audit trail (prefix/created_at stay).
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS team_leader_key_hash TEXT;
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS team_leader_key_prefix TEXT NOT NULL DEFAULT '';
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS team_leader_key_created_at TIMESTAMPTZ;
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS team_leader_key_revoked_at TIMESTAMPTZ;
-- WHEN the leader credential was last used. created_at answers "when was it
-- minted", never "is it still in use" — and a stale-credential review (rotate
-- it? revoke it? hand the console to someone else?) is exactly that second
-- question. Stamped BEST-EFFORT on a successful leader auth by
-- core.teams.stamp_leader_used, throttled to one write per hour per enrollment
-- (the console polls; a row per request would be pure write amplification).
-- NULL = never used since it was minted, which is itself the signal.
ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS team_leader_key_last_used_at TIMESTAMPTZ;
-- One live leader credential per hash, enforced BY THE DATABASE: a duplicate
-- (a copied row, a buggy rotate that reuses a hash) must fail the UPDATE
-- instead of silently authorizing the wrong team's console. Partial index
-- because the column is NULL for every enrollment without a leader key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_enrollments_leader_hash
    ON enrollments(team_leader_key_hash) WHERE team_leader_key_hash IS NOT NULL;

-- BUDGET TOP-UP REQUESTS (team leader tier): the enrollment CAP is the admin's
-- alone, but a leader who has hit "available 0" otherwise has no way to ask for
-- more except out-of-band. This is the ask/approve QUEUE: the leader files one
-- open request, an admin approves (which raises the cap) or denies.
--
-- WHY a table and not a flag on enrollments: the request is an EVENT with a
-- resolution — who asked, when, what was approved versus what was requested,
-- and who resolved it. Overwriting a column would lose exactly the audit story
-- an approval trail exists to keep, and a rejected ask is information too.
--
-- requested_* are NULLABLE PER KIND: "another 5M tokens" and "another $20" are
-- different asks (the envelope is per kind), so a request carries either or
-- both, and the approver may OVERRIDE the amounts. No CHECK constraint on the
-- pair — an all-NULL row is rejected app-side (the request body needs at least
-- one amount), matching how budget_period is validated in code, not in DDL.
--
-- status: 'pending' | 'approved' | 'denied'. A partial-ish index on status
-- serves the console's "pending" card (the only unbounded-growth query shape);
-- the per-enrollment index serves the leader's own history.
CREATE TABLE IF NOT EXISTS enrollment_topup_requests (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    enrollment_id    UUID NOT NULL REFERENCES enrollments(id) ON DELETE CASCADE,
    requested_tokens BIGINT,          -- NULL = not asking for tokens
    requested_usd    NUMERIC(12,4),   -- NULL = not asking for USD
    note             TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    resolved_by      TEXT NOT NULL DEFAULT ''   -- admin actor label ('' = unresolved)
);
CREATE INDEX IF NOT EXISTS idx_topup_status ON enrollment_topup_requests(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_topup_enrollment ON enrollment_topup_requests(enrollment_id, created_at DESC);
-- ONE OPEN ASK PER TEAM, enforced BY THE DATABASE. The route checks for a
-- pending row before inserting, but check-then-INSERT is a race: two concurrent
-- POSTs (a double-click on a slow link) both see "none pending" and both insert,
-- and the admin then approves two asks that were meant to be one. The partial
-- unique index makes the loser fail instead — the route maps the violation to
-- the same 409 the pre-check raises, so the caller sees one contract either way.
CREATE UNIQUE INDEX IF NOT EXISTS uq_topup_one_pending
    ON enrollment_topup_requests(enrollment_id) WHERE status = 'pending';

-- Per-key MCP credential overrides (the memory-MCP pattern: identity via
-- request headers like X-Memory-Dataset / X-Dataset-Password). Fernet-
-- encrypted JSON {header: value}; resolved at call time and merged AFTER
-- server-level auth; grants stay a separate concern. Write-only via the
-- admin API — plaintext never returns.
CREATE TABLE IF NOT EXISTS mcp_server_creds (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id      UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    server_name TEXT NOT NULL,
    headers_enc TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (key_id, server_name)
);

-- Off-peak billing: the fraction the LOW-DEMAND evaluator was in when the
-- request settled (1.0 = normal). The ledger keeps TRUE tokens/cost for
-- audit; the BUDGET GATES consume tokens*fraction / cost*fraction, so a
-- 0.5 off-peak window makes every budget last twice as long. Idempotent.
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS billing_fraction REAL NOT NULL DEFAULT 1.0;

-- Per-key tool-relay DEFAULT: when TRUE, MCP tool calls relay in-cluster
-- WITHOUT the client sending X-PCAI-Execute-Tools (an off-cluster consumer
-- like sproket NEEDS this — the cluster's MCPs are external to it, and the
-- relay is how it reaches them). The per-request header still overrides:
-- '1' forces on, '0' opts out. Idempotent.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS relay_default BOOLEAN NOT NULL DEFAULT FALSE;

-- Per-key `stream_pcai_events` (RFC-PER-KEY-STREAM-EVENTS, 2026-09-23): the
-- gateway's in-stream `{"pcai": …}` SSE events are NOT valid OpenAI chunks
-- (no `choices`/`error`) — strict validating clients (opencode
-- @ai-sdk/openai-compatible, clearwing openai-py >= 2.45) abort the stream
-- on the first one. TRUE (default) keeps today's bytes for every existing
-- key (console, DSH — the lenient-parser consumers); a key flipped OFF gets
-- a byte-clean spec-compliant stream: chunks + reasoning_content + the
-- standard include_usage chunk + {"error": …} + [DONE] — nothing a
-- compliant client can parse is lost. Per-request override header
-- X-PCAI-Stream-Events: 0|1 (same three-state shape as the relay toggle
-- above). Mirrors relay_default end-to-end. Idempotent.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS stream_pcai_events BOOLEAN NOT NULL DEFAULT TRUE;
-- DEFAULT-FLIP (consumer inventory 2026-09-24): new keys are spec-clean
-- (FALSE) unless the minting surface opts in — the console's create form
-- posts true, enrollment (the DSH key path) passes True explicitly.
-- ALTER DEFAULT touches only future inserts that omit the column; every
-- existing key keeps its stored value (no mid-flight stream change).
ALTER TABLE api_keys ALTER COLUMN stream_pcai_events SET DEFAULT FALSE;

-- Off-peak billing fraction, PER PROFILE (the commercial knob; the
-- evaluator's env config is the infra knob that decides WHEN low-demand is
-- active). NULL = inherit the gateway default (PCAI_LLM_OFFPEAK_FRACTION
-- while a low-demand window is active, else 1.0). Console-editable.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS offpeak_fraction REAL;

-- Content guardrails: named policies attached per virtual model name
-- (model_endpoints.model_name). Guardrails run pre_call (mutate/block the
-- request messages) and post_call (mutate/block the assistant text); every
-- run writes its verdicts to usage_ledger.guardrail_events. Fail-open by
-- design: a broken guardrail/webhook degrades to pass-with-event, never to
-- a gateway outage.
CREATE TABLE IF NOT EXISTS guardrails (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    mode        TEXT NOT NULL CHECK (mode IN ('pre_call','post_call')),
    engine      TEXT NOT NULL CHECK (engine IN ('blocklist','regex_pii','length','webhook')),
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2026-11-02: the secrets engine joined the framework. CREATE TABLE IF NOT
-- EXISTS cannot widen an existing CHECK, so the constraint is re-stated for
-- databases created before this wave (DROP IF EXISTS + ADD is idempotent).
-- NOT VALID (cross-validation 2026-11-10): a VALID re-add scans every row
-- under ACCESS EXCLUSIVE on EVERY boot inside the same implicit transaction
-- as the rest of SCHEMA_SQL — the budgets_period_check pair above got the
-- same fix. NOT VALID is the right POSTURE for these re-adds generally, not
-- just a speed trick: the unconditional DROP+ADD would silently reset an
-- operator's manual VALIDATE CONSTRANT on the next boot anyway, so keeping
-- the re-add NOT VALID states the honest contract (existing rows trusted;
-- new writes checked; VALIDATE is the operator's, and it sticks only until
-- this file widens the vocabulary again).
ALTER TABLE guardrails DROP CONSTRAINT IF EXISTS guardrails_engine_check;
ALTER TABLE guardrails ADD CONSTRAINT guardrails_engine_check
    CHECK (engine IN ('blocklist','regex_pii','length','webhook','secrets')) NOT VALID;

CREATE TABLE IF NOT EXISTS model_guardrails (
    model_name   TEXT NOT NULL,
    guardrail_id UUID NOT NULL REFERENCES guardrails(id) ON DELETE CASCADE,
    PRIMARY KEY (model_name, guardrail_id)
);
CREATE INDEX IF NOT EXISTS idx_model_guardrails_model ON model_guardrails(model_name);

-- Verdict log: one JSON array of {guardrail,mode,engine,action,detail} per
-- settled request that ran guardrails (NULL = none ran). Additive column on
-- the immutable ledger — audit keeps the full story next to the cost row.
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS guardrail_events JSONB;

-- ============================================================================
-- Complexity-driven model routing (pseudo-models `routing` / `routing__X`).
-- ALL ADDITIVE / idempotent, same discipline as every migration above.
-- ============================================================================

-- Per-profile opt-in (core.model_routing): 'off' (default — routing and
-- routing__X stay unregistered aliases, byte-for-byte today's behavior),
-- 'shadow' (serve as off, stamp what the router WOULD have served), and
-- 'active' (serve the routed model through the normal pinned machinery).
-- routing_min_model is the cheap-first-ladder FLOOR: the router never
-- serves a model cheaper than it; empty = floor pinned to the ceiling
-- (no downgrade) until the operator picks one.
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS routing_policy TEXT NOT NULL DEFAULT 'off';
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS routing_min_model TEXT NOT NULL DEFAULT '';

-- Ledger routing columns: the decision audit that shadow mode exists to
-- harvest (the labeled (request → would-serve vs actually-served) pairs a
-- future learned router trains on). routing_policy '' = routing was not in
-- effect for the row; the `model` column keeps the ACTUALLY-served model
-- in every case (ledger truth is never displaced by routing metadata).
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS routing_policy TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS routing_ceiling TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS routing_served TEXT NOT NULL DEFAULT '';
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS routing_reason TEXT NOT NULL DEFAULT '';
-- The decision model's FULL per-option distribution (JSON object, '' when none).
-- routing_reason carries only the WINNER score, which cannot answer "how close
-- was this?" -- a 0.62 winner over a 0.60 runner-up is a coin flip that happened
-- to clear the floor. Without the distribution an operator cannot tune the
-- confidence floor against real traffic (2026-10-23).
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS routing_probabilities TEXT NOT NULL DEFAULT '';
-- P10 unit-aware billing: billable_units is the QUANTITY the row billed in the
-- unit its cost was computed from (seconds of audio, characters of a TTS
-- script, images, requests) and unit_kind names that unit — with an
-- '_estimate' suffix when the number is a PROXY rather than a measurement
-- ('char_estimate' = the TTS char heuristic, 'second_estimate' = the ASR byte
-- proxy, 'token_estimate' = the len//4 family). NULL on every legacy row and
-- on rows with REAL measured usage (chat's upstream-reported tokens): a NULL
-- unit_kind means "priced exactly as always". Both appended LAST — the
-- partition script's COLUMNS block (positional INSERT..SELECT * copy) is
-- pinned to this physical order by tests/test_deploy_contract.py.
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS billable_units NUMERIC(18,6);
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS unit_kind TEXT;

-- ============================================================================
-- Server-owned conversations (ROADMAP §7, phase 1: canonical reconciliation).
-- ALL ADDITIVE / idempotent, same discipline as every migration above.
-- ============================================================================

-- The gateway's authoritative message list per (key, conversation) for the
-- relay path: one row per conversation (PK = key_id + thread fingerprint),
-- the full OpenAI-shaped message list as JSONB, last reconciled turn wins.
-- Phase 1 only DETECTS the conversation shape per turn (extend / reset /
-- new / trimmed_incoming — see core.canonical.reconcile) and persists what
-- the client already re-sends; phase 2's result-only continuation protocol
-- ("result of call X" + session ref instead of the 214k-token full resend)
-- reads this row as the conversation the client no longer has to carry.
-- Deliberately a SEPARATE table from console-shaped conversations/
-- conversation_messages (role + TEXT content, chat-UI transcripts) — do not
-- merge them. No FK to api_keys: like usage_ledger, the row is keyed by the
-- id alone and reconciliation is best-effort (an orphaned row after a key
-- delete is inert; ON CONFLICT upserts keep writes idempotent).
CREATE TABLE IF NOT EXISTS canonical_conversations (
    key_id     UUID NOT NULL,
    thread_id  TEXT NOT NULL,            -- _thread_key fingerprint
    messages   JSONB NOT NULL,           -- the canonical message list (full OpenAI shapes)
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key_id, thread_id)
);

-- The distill sweep orders by updated_at every tick (audit P2-2): without
-- this index that is a full scan + sort of the biggest table on a busy
-- gateway. Additive; supports the due-window ORDER BY ... LIMIT 20.
CREATE INDEX IF NOT EXISTS idx_canonical_conversations_updated ON canonical_conversations(updated_at);

-- Wave C cross-request loop accounting (ROADMAP §7 phase 2): the relay
-- iterations the conversation's loop already spent across ALL prior
-- requests. A handback continuation resumes THIS counter (the relay's
-- guard budget starts there), so cycling handback -> result -> handback
-- can no longer reset the iteration cap per request. 0 = legacy row
-- (created before this column existed) - never invents budget. Additive
-- and idempotent like every migration in this file.
ALTER TABLE canonical_conversations ADD COLUMN IF NOT EXISTS iterations INTEGER NOT NULL DEFAULT 0;

-- The AWAITING-CLIENT handback state (wave C result-only continuation):
-- the relay loop's appended tail (assistant tool_calls + in-loop-executed
-- results) saved at the handback terminal, read by X-PCAI-Continue, and
-- cleared when consumed. The canonical messages column is deliberately
-- NOT touched by the flush: a full-history client's re-send must keep
-- reconciling byte-stable (the relay's internal assistant reconstruction
-- need not hash-match the client's message).
ALTER TABLE canonical_conversations ADD COLUMN IF NOT EXISTS pending JSONB;

-- ============================================================================
-- Effective-prompt capture (ROADMAP §7 phase 2 wave C: transparency).
-- ALL ADDITIVE / idempotent, same discipline as every migration above.
-- ============================================================================

-- One row per REAL upstream model call on the relay path: the bounded
-- preview of the EFFECTIVE prompt — the exact message list the call sent
-- upstream (post reasoning-strip, post canonical reconciliation, post
-- context-budget trim) — plus a bounded tools summary. WHY a second store
-- next to canonical_conversations: the canonical store holds the
-- conversation BETWEEN calls (one row, last turn wins); this one freezes
-- WHAT EACH CALL SAW (retry attempts included), which is the artifact a
-- "the model answered badly" investigation needs. Messages/tools are TEXT
-- (the bounded JSON the capture wrote — already truncated at the config
-- caps), never re-parsed server-side. Written BEST-EFFORT strictly AFTER
-- the upstream call: a capture fault must never touch the relay turn.
-- No FK to api_keys (usage_ledger convention: key_id alone; retention
-- prunes rows independently of key lifecycle).
CREATE TABLE IF NOT EXISTS effective_prompts (
    id          BIGSERIAL PRIMARY KEY,
    key_id      UUID NOT NULL,
    thread_id   TEXT,
    request_id  TEXT NOT NULL,
    iteration   INT  NOT NULL,
    attempt     INT  NOT NULL DEFAULT 1,    -- 1 = first try; N = bounded-call retry N-1
    model       TEXT NOT NULL,
    messages    TEXT NOT NULL,              -- bounded JSON preview (see core.effective)
    tools       TEXT NOT NULL DEFAULT '',   -- bounded JSON tools summary ('' = toolless call)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_effective_prompts_thread ON effective_prompts(key_id, thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_effective_prompts_created ON effective_prompts(created_at);

-- ============================================================================
-- Per-model fallback chains (router extras, review §4a). ALL ADDITIVE.
-- ============================================================================
-- Admin-curated failover chains INDEPENDENT of the profile ordering: a PINNED
-- request whose model cannot serve (pool.pick()==None — every endpoint
-- cooling/absent) or fails with an upstream 5xx / transport error walks the
-- chain instead of ending answerless. Each candidate is re-gated at WALK time
-- (registered + key-allowed + healthy chat pool + check_budgets headroom) —
-- the chain is a preference list, never a policy bypass; budgets are checked
-- fresh per attempt and every attempt settles under its ACTUAL model.
-- `rank` orders the walk (lower first, the model_endpoints.priority
-- convention). UNIQUE (model_name, fallback_model): one chain entry per
-- pair — duplicates are a console double-tick, not two attempts.
-- No FK to model_endpoints.model_name (that column is not keyed): the admin
-- API validates names against the live registry at write time, and a chain
-- row naming a since-deleted model is inert at walk time (the candidate is
-- skipped as unregistered) — the same graceful-degradation discipline
-- delete_model documents for profile orderings.
CREATE TABLE IF NOT EXISTS model_fallbacks (
    model_name     TEXT NOT NULL,          -- the primary (pinned) model
    rank           INTEGER NOT NULL DEFAULT 0,   -- lower = tried first
    fallback_model TEXT NOT NULL,          -- the candidate to try
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (model_name, fallback_model)
);
CREATE INDEX IF NOT EXISTS idx_model_fallbacks_model ON model_fallbacks(model_name, rank);

-- ============================================================================
-- Admin mutation audit trail (append-only). WHY: the usage_ledger is the
-- DATA-plane audit artifact — every model/tool call is ledgered with its
-- key, cost, and request_id — but the ADMIN plane (key create/revoke/patch,
-- budget + cost edits, model/endpoint/MCP registry changes, credential
-- overrides, profile/off-peak edits, enrollment redemption) had NO record
-- beyond ephemeral log lines. A compliance question ("who granted tool X,
-- when, and from where") was unanswerable from durable state. This table is
-- that record: one immutable row per successful admin mutation.
--
-- APPEND-ONLY BY CONTRACT: there is deliberately NO UPDATE or DELETE path
-- anywhere in the codebase and no pruning job — the same
-- never-pruned/never-rewritten philosophy as the usage ledger (history is
-- the point; an admin cannot erase the trace of what they did, which is
-- exactly the property the trail exists to guarantee). A mutated row would
-- be a broken audit trail, so nothing may grow one.
--
-- SECRET MATERIAL IS FORBIDDEN IN ``details`` (REDACTION RULE): the details
-- JSONB must never carry credential VALUES — upstream API keys, MCP bearer
-- tokens, per-key credential override header values (e.g. the memory-MCP
-- X-Dataset-Password), enrollment code plaintexts, or any key material.
-- core.audit.details_redact() enforces this recursively at write time
-- (keys matching secret/token/password/key/credential patterns →
-- '<redacted>'); what MAY be stored is metadata: credential NAMES, header
-- NAMES, key/row counts, URLs, scopes, the prior state of a flag. Hashes
-- (SHA-256 of stored keys/codes) are also never stored here — the admin
-- audit answers "who changed what", and the referenced table already holds
-- the hash.
--
-- ACTOR SHAPE: admin keys are operator-provisioned via a K8s Secret (never
-- DB rows), so the actor is recorded three ways: actor_key_id stays NULL
-- for them; actor_subject carries the caller's stable identity when one is
-- resolvable (SSO browser identity, enrolled subject) and a stable digest
-- label for a Secret bearer key; actor_source is the credential KIND that
-- authenticated the request ('admin-key' | 'proxy-identity' | 'token' |
-- 'enroll'). Enrollment redemptions are unauthenticated by design (the
-- code IS the credential), so they audit with actor_subject = the created
-- key's subject/claim when bound, else the key id — attribution is
-- best-effort and never refused for want of a subject.
--
-- NO FK on actor_key_id BY DESIGN: api_keys rows are the only mutable id
-- space referenced and the trail must survive a key's full lifecycle —
-- but more importantly api_keys.id is a UUID while actor_key_id is
-- BIGINT-shaped in the spec, and (decisively) a revocation or a hard
-- delete of a key row must never cascade into or block the audit trail
-- (revoke-preserves-history: the ledger rows of a revoked key stay
-- attributable, and the audit trail follows the same rule — a dangling
-- actor reference reads as "the key existed once", which is true history,
-- not corruption). Typed BIGINT (not UUID) so a future numeric actor
-- space (e.g. an internal operator table) joins without a migration.
-- ============================================================================
CREATE TABLE IF NOT EXISTS admin_audit (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_key_id   BIGINT,                    -- numeric actor id when one exists (NULL = Secret admin key / unnumbered)
    actor_subject  TEXT NOT NULL DEFAULT '',  -- stable identity claim ('' = anonymous/unresolvable)
    actor_source   TEXT NOT NULL,             -- 'admin-key' | 'proxy-identity' | 'token' | 'enroll'
    action         TEXT NOT NULL,             -- 'key.create', 'key.patch', 'budget.set', ...
    resource_type  TEXT NOT NULL,             -- 'key' | 'budget' | 'model' | 'mcp_server' | 'profile' | ...
    resource_id    TEXT NOT NULL,             -- the row id / name the action landed on (TEXT: ids are mixed UUID/name)
    details        JSONB NOT NULL DEFAULT '{}',  -- REDACTED metadata (see the block above) — never secret values
    request_id     TEXT,                      -- X-Request-Id / handler correlation id (NULL = none supplied)
    source_ip      TEXT                       -- best-effort client address (XFF-aware); NULL when indeterminate
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_ts ON admin_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_actor ON admin_audit(actor_key_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_admin_audit_resource ON admin_audit(resource_type, resource_id, ts DESC);

-- ============================================================================
-- Hash-chained audit (2026-10-26 hardening plan P2-2)
-- ============================================================================
-- applygate's audit file is hash-chained; the GATEWAY's own record of who
-- asked for what was not. For the exfiltration attack class the model-side
-- record is the one that matters, and it was the weaker trail: an operator
-- with DB access could edit or delete rows and nothing would notice.
--
-- Two trails get chained:
--   * admin_audit    - control plane (key minting, grants, registry edits).
--   * mcp_call_audit - the MCP tool calls themselves (server/tool/argument
--     DIGEST/outcome), i.e. the 'which model asked for what' record. Its
--     volume is tool-call-rate, not chat-rate, so per-row chaining is
--     affordable there in a way it is NOT on usage_ledger (whose INSERT arg
--     tuple is positional and whose write is on the hot path - deliberately
--     out of scope for this wave; see documentation/HARDENING-2026-10-26.md).
--
-- Chain shape: row_hash = sha256(prev_hash || '|' || canonical row text).
-- The first row's prev_hash is ''. Verification walks id ASC and reports the
-- first row whose prev_hash differs from the previous row's row_hash
-- (tamper, deletion, or a concurrent-write fork).
ALTER TABLE admin_audit ADD COLUMN IF NOT EXISTS prev_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE admin_audit ADD COLUMN IF NOT EXISTS row_hash  TEXT NOT NULL DEFAULT '';
-- details_sha256: the chain covers the REDACTED details payload by DIGEST,
-- computed in Python at write time. Chaining over the JSONB column itself
-- would depend on jsonb's text rendering (key order, spacing) matching the
-- verifier's, which is not a contract worth betting the trail on.
ALTER TABLE admin_audit ADD COLUMN IF NOT EXISTS details_sha256 TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_admin_audit_row_hash ON admin_audit(row_hash);

CREATE TABLE IF NOT EXISTS mcp_call_audit (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_id      UUID,                       -- calling virtual key (no FK: ledger convention)
    server      TEXT NOT NULL,
    tool        TEXT NOT NULL,
    -- DIGEST, never the arguments: the trail must answer 'what was asked'
    -- without becoming a second copy of the payload (and without storing
    -- whatever the model may have been exfiltrating).
    args_sha256 TEXT NOT NULL DEFAULT '',
    args_bytes  INTEGER NOT NULL DEFAULT 0,
    outcome     TEXT NOT NULL,              -- ok|error|denied|policy_blocked
    model       TEXT NOT NULL DEFAULT '',   -- serving model when known ('' = unattributed)
    request_id  TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',   -- bounded reason token / short error class
    prev_hash   TEXT NOT NULL DEFAULT '',
    row_hash    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mcp_call_audit_ts ON mcp_call_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_call_audit_key ON mcp_call_audit(key_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_call_audit_tool ON mcp_call_audit(server, tool, ts DESC);

-- FORK GUARD (wave-2 review P2-3): prev_hash is read in the inserting statement,
-- so two concurrent writers can chain from the SAME prev - a FORK, not a tamper.
-- A partial unique index makes the second writer FAIL instead, and the writer
-- retries (it re-reads prev on the next attempt). Without this the verifier is
-- permanently red on a busy gateway, i.e. the tamper detector is red from birth
-- and cannot be distinguished from a real edit. Partial: the empty prev (the
-- first row, and every row written by the degraded unchained path) is exempt.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_call_audit_prev
    ON mcp_call_audit(prev_hash) WHERE prev_hash <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_audit_prev
    ON admin_audit(prev_hash) WHERE prev_hash <> '';

-- EXTERNAL TAIL ANCHOR (audit P2-5): the chain above is tamper-EVIDENT only
-- against an attacker who cannot recompute the tail — an attacker with DB
-- write access who rewrites the last rows and every hash after them passes
-- verify as ok. The anchor is the off-chain counterweight: core.chain_audit
-- periodically stamps the trail's tail (last_id, last_row_hash) here under
-- an HMAC keyed from the master key (an env SECRET, never in this database),
-- so a re-tailed trail no longer matches the anchored tail. Verbatim the
-- ANCHOR_DDL in core.chain_audit (that module also keeps its per-process
-- lazy ensure, so a boot against a pre-migration schema still works).
-- Idempotent: CREATE TABLE IF NOT EXISTS only, no ALTER — safe per boot.
CREATE TABLE IF NOT EXISTS audit_chain_anchor (
    table_name TEXT PRIMARY KEY,   -- which trail: admin_audit | mcp_call_audit
    last_id BIGINT NOT NULL,
    last_row_hash TEXT NOT NULL,
    mac TEXT NOT NULL,
    anchored_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- Model groups (2026-11-08) — named sets of EQUIVALENT chat models.
--
-- WHY: a deployment can serve the same capability from several models
-- (Deepseek / GLM / Qwen) and several replicas of each. A group is a
-- first-class route target: members are chosen by LIVE LOAD (not a fixed
-- order), then the conversation is pinned to the chosen member's endpoint.
-- It is usable anywhere a model name is accepted (direct request, fallback
-- chain, /v1/models). This is the load-balanced sibling of the ORDERED
-- fallback chain: fallback is a failure path, a group is a selection path.
--
-- members is JSONB [{model, weight}] in registry model-name spelling. ONE
-- LEVEL ONLY: a member may not itself be a group (validated at the write
-- path, so a cycle can never be stored). policy 'load' today; 'weighted'
-- is reserved. model_type keeps a group from serving chat with an
-- embedding member (mirrors the pool type gate).
--
-- NOT a budget or ACL boundary: the SELECTED member bills and its
-- allowed_models gate applies (a group never makes a model reachable that
-- the key could not reach on its own).
CREATE TABLE IF NOT EXISTS model_groups (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL UNIQUE,
    model_type  TEXT NOT NULL DEFAULT 'chat',
    policy      TEXT NOT NULL DEFAULT 'load',
    members     JSONB NOT NULL DEFAULT '[]',
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================================
-- Load-aware cascade stickiness (auto entry, off-peak-driven). ADDITIVE /
-- idempotent, same discipline as every migration above.
-- ============================================================================
-- One row per (key, conversation thread): the head model the `auto` cascade
-- picked when the conversation STARTED, so every later turn of that
-- conversation reorders to the SAME head ("if it went to GLM, stay with
-- GLM") instead of re-rolling per request. Postgres-backed (NOT in-process)
-- so the choice survives a replica switch: sibling pods read the same row,
-- which is what makes the pick replica-safe.
--
-- ``thread_key`` is core.canonical.thread_key(msgs, key_id) — a 16-hex
-- fingerprint of (key, first user message); a request with no user-message
-- seed has no thread and never touches this table (no stickiness possible
-- or attempted). Rows are pure ROUTING state: a row whose model left the
-- profile ordering / the key's allowed_models / the registry is DROPPED on
-- sight by core.stickiness (the allowed/registered set is authoritative,
-- never the stored preference), and housekeeping prunes rows older than
-- retention_stickiness_days (default 14; 0 = keep forever).
--
-- No FK to api_keys: same convention as usage_ledger / canonical_conversations
-- (key_id alone) — a dangling row after a key delete is inert, and
-- ON CONFLICT upserts keep writes idempotent across replicas.
CREATE TABLE IF NOT EXISTS model_stickiness (
    key_id     UUID NOT NULL,
    thread_key TEXT NOT NULL,
    model      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (key_id, thread_key)
);
CREATE INDEX IF NOT EXISTS idx_model_stickiness_updated ON model_stickiness(updated_at);
-- CROSS-REPLICA SWEEP LEASE (2026-10-11 duplication fix): the distill sweep runs
-- in EVERY gateway pod (HPA scale-out = N concurrent sweepers), and two pods
-- selecting the same due row distill it twice - the live pulse-check caught
-- every thread carrying TWO near-identical summaries 3s apart. A row is claimed
-- at selection time: the sweeper takes a short lease; a second sweeper skips
-- leased rows until the lease lapses (a crashed holder self-heals).
ALTER TABLE canonical_distills ADD COLUMN IF NOT EXISTS sweep_lease TEXT;
ALTER TABLE canonical_distills ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE conversation_distills ADD COLUMN IF NOT EXISTS sweep_lease TEXT;
ALTER TABLE conversation_distills ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;

-- ============================================================================
-- Agent profiles (2026-10-29) — named tool surfaces exposed as model endpoints.
-- ADDITIVE / idempotent, same discipline as every migration above.
-- ============================================================================
-- WHY: exposing all ~111 granted MCP tools to every key cost 34,459 prompt
-- tokens per relayed request and made tool SELECTION a 111-way choice for a
-- task that needed four servers. The hybrid surface (core.tool_surface) removed
-- the token cost; an agent removes the CHOICE — it is a named bundle of servers
-- a request opts into by naming it as its model.
--
-- SHAPE: an agent is a MODEL-NAME entry point (GET /v1/models advertises it
-- alongside the real pools, pcai.type=agent). A request naming it resolves to
-- the 'model' column when set, else through the profile's chat cascade exactly
-- as 'auto' does, and its tool surface is the INTERSECTION of the key's grants
-- and 'servers'.
--
-- SERVER-LEVEL, NOT (server, tool) PAIRS — deliberately, and NOT a repeat of
-- the mistake the mcp_servers.tool_policy comment warns about. That warning is
-- about a table that CREATES grants: a server-level allowlist there is bypassed
-- by the server gaining a tool. An agent creates nothing: it is a FILTER over
-- grants the key already holds (mcp_tool_acls / live profile grants) plus the
-- per-server policy (mcpf.federation.server_allows), which stay the boundary.
-- A server that gains a tool still grants that tool to nobody new; the agent
-- merely does not hide it from a key that already holds it. That is why the
-- admin UI can offer 'pick servers' without weakening anything.
--
-- 'model' '' = cascade (the agent picks tools + prompt, not the model).
-- 'system_prompt' '' = none (the injection seam is phase 2; the column ships
-- now so an agent authored today keeps its prompt when that lands).
CREATE TABLE IF NOT EXISTS agent_profiles (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL UNIQUE,         -- the model id clients request
    description   TEXT NOT NULL DEFAULT '',     -- what this agent is FOR (shown in /v1/models)
    servers       TEXT[] NOT NULL DEFAULT '{}', -- MCP server names this agent may see
    -- Per-server TOOL narrowing, JSON object {server: [tool, ...]} (TEXT, not
    -- JSONB: the pool registers no JSONB codec, so a jsonb column arrives as a
    -- raw string anyway — same shape as profiles.defaults_json). A server
    -- ABSENT from this object is WHOLE-SERVER (every tool it exposes) — the
    -- pre-2026-11-04 meaning, so existing rows keep working untouched. Present
    -- = only those tools. Narrowing can only HIDE: the agent's surface is
    -- still intersected with the key's grants at request time.
    tools         TEXT NOT NULL DEFAULT '{}',
    model         TEXT NOT NULL DEFAULT '',     -- '' = profile cascade; else a pinned model
    system_prompt TEXT NOT NULL DEFAULT '',     -- prepended system message (phase 2)
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_profiles_enabled ON agent_profiles(enabled, name);
-- 2026-11-04: agents gained per-server tool narrowing. ADD COLUMN IF NOT
-- EXISTS keeps this idempotent for deployments that already have the table.
ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS tools TEXT NOT NULL DEFAULT '{}';

-- Which profiles OFFER which agents. A key inherits its USER's profile, so this
-- join is the whole authorization story: the profile is the envelope (what this
-- class of caller may ever reach) and the agent is the role inside it.
-- Deliberately its OWN table rather than a profiles.defaults_json key:
-- create_profile rebuilds defaults_json from the request body, so a binding
-- stored there is silently deleted by any older console POST (the same
-- destructive-by-omission hazard live_grants already has).
-- NO row for a profile = it offers no agents. A key with NO profile offers
-- every ENABLED agent — the repo's empty-ACL convention (api_keys.allowed_models
-- empty = all models), which is what keeps an admin/CLI key usable without
-- inventing a profile for it.
CREATE TABLE IF NOT EXISTS profile_agents (
    profile_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    agent_id   UUID NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE,
    PRIMARY KEY (profile_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_profile_agents_agent ON profile_agents(agent_id);

-- ============================================================================
-- Cross-replica registry convergence (2026-10-30) — modification stamps.
-- ADDITIVE / idempotent, same discipline as every migration above.
-- ============================================================================
-- WHY: the gateway caches mcp_servers + model_endpoints in PROCESS memory and
-- reloaded that snapshot only at boot and after a mutation THIS process served.
-- On an HPA'd deployment (minReplicas 2) a server registered on one replica was
-- therefore invisible to its sibling until that pod restarted — the console's
-- "test" button 404'd "not in the enabled registry" on roughly half the clicks
-- and the relay answered "unknown server" on the stale pod. core.registry now
-- polls these stamps and refreshes the snapshot when they move.
--
-- WHY A TRIGGER rather than `updated_at = now()` in each write statement: the
-- manual POST /admin/registry/refresh exists precisely because operators make
-- OUT-OF-BAND changes (psql, seeding scripts, tooling) that bypass the admin
-- API. Those edits must converge across replicas too, and only the database can
-- see them. The stamp therefore belongs to the ROW, not to a code path.
ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE model_endpoints ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- clock_timestamp(), NOT now(): now() is TRANSACTION-START time, so two
-- concurrent admin writes would carry the same stamp (the later one could not
-- move the MAX and its replica would never see it). clock_timestamp() is the
-- moment the row actually changed. The residual is a literal microsecond tie
-- between two writes — the next registry write moves the watermark again, so a
-- tie costs one delayed convergence, never a permanently invisible row.
CREATE OR REPLACE FUNCTION pcai_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $pcai$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$pcai$;

-- CREATE TRIGGER has no IF NOT EXISTS, and a bare DROP + CREATE is NOT atomic
-- against a sibling replica booting at the same moment: MEASURED on a throwaway
-- Postgres (5/5 rounds), the loser's whole schema apply rolls back on
-- "trigger already exists" — and because SCHEMA_SQL is one implicit
-- transaction, that pod crash-loops once before healing. The DO block's
-- duplicate_object handler makes the second creator a no-op; the trigger is
-- byte-identical either way, so there is nothing to reconcile.
DROP TRIGGER IF EXISTS mcp_servers_touch_updated_at ON mcp_servers;
DO $pcai$
BEGIN
    CREATE TRIGGER mcp_servers_touch_updated_at
        BEFORE UPDATE ON mcp_servers
        FOR EACH ROW EXECUTE FUNCTION pcai_touch_updated_at();
EXCEPTION WHEN duplicate_object THEN
    NULL;
END;
$pcai$;

DROP TRIGGER IF EXISTS model_endpoints_touch_updated_at ON model_endpoints;
DO $pcai$
BEGIN
    CREATE TRIGGER model_endpoints_touch_updated_at
        BEFORE UPDATE ON model_endpoints
        FOR EACH ROW EXECUTE FUNCTION pcai_touch_updated_at();
EXCEPTION WHEN duplicate_object THEN
    NULL;
END;
$pcai$;
"""
