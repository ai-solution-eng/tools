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
-- gate's `status IN ('ok','error')` SUM semantics and the PrometheusRule's
-- status alerting. Additive + idempotent (NOT VALID keeps the migration
-- instant even on a huge ledger; existing rows are trusted, new writes
-- are checked — VALIDATE later if desired).
ALTER TABLE usage_ledger DROP CONSTRAINT IF EXISTS ck_ledger_status;
ALTER TABLE usage_ledger ADD CONSTRAINT ck_ledger_status
    CHECK (status IN ('ok','error','cache_hit','budget_exceeded','rate_limited','guardrail_blocked','policy_blocked','denied')) NOT VALID;
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS cached_tokens BIGINT NOT NULL DEFAULT 0;
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(12,6);

-- Per-model prices (USD per 1M tokens — the industry convention). Absent
-- row = unpriced model (cost NULL).
CREATE TABLE IF NOT EXISTS model_costs (
    model_name        TEXT PRIMARY KEY,
    input_per_m       NUMERIC(12,8) NOT NULL DEFAULT 0,
    output_per_m      NUMERIC(12,8) NOT NULL DEFAULT 0,
    cached_per_m      NUMERIC(12,8) NOT NULL DEFAULT 0,
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
-- Conversation grouping: a stable fingerprint of (key, first user message).
-- Harness loops (DSH) re-send the accumulated history every step, so one
-- long multi-turn conversation spans MANY request_ids — the thread key is
-- what groups them into one trace conversation.
ALTER TABLE agent_traces ADD COLUMN IF NOT EXISTS thread_id TEXT;
CREATE INDEX IF NOT EXISTS idx_traces_thread ON agent_traces(thread_id);

-- Saved conversations (per API key): server-side transcripts so the chat
-- console's left panel survives tabs and devices. The client pushes the
-- full transcript per turn (replace-all — idempotent, no partial state);
-- size caps are enforced app-side (≤200 messages, ≤32KB each).
CREATE TABLE IF NOT EXISTS conversations (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_id     UUID NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    user_id    UUID,
    title      TEXT NOT NULL DEFAULT 'New conversation',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_key ON conversations(key_id, updated_at DESC);

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
    max_uses   INT  NOT NULL DEFAULT 0,        -- 0 = unlimited
    uses       INT  NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
    model         TEXT NOT NULL DEFAULT '',     -- '' = profile cascade; else a pinned model
    system_prompt TEXT NOT NULL DEFAULT '',     -- prepended system message (phase 2)
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agent_profiles_enabled ON agent_profiles(enabled, name);

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
"""
