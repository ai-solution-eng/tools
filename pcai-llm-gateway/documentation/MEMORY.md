# Memory configuration — keys, datasets, and the distiller

How to give an agent durable long-term memory on this gateway: mint the right
credential, create the right dataset, attach the two together, and prove it
works. This is the **operator/user-facing** walkthrough; the mechanisms behind
it are described in [FEATURES.md](FEATURES.md) § "Session memory distiller"
and § "Per-key MCP credential overrides".

> **The one-sentence version.** Memory is a *credential chain*, not a setting:
> the RAG server mints you a key → that key owns exactly one ★ dataset →
> the gateway stores that key as a header override for the `mm-rag` server →
> the distiller probes with it and writes only if the probe says writable.

---

## 0. The three moving parts

Memory spans **two deployments** and **three identities**. Getting one of them
wrong is the entire failure surface, so name them before touching anything:

| Piece | Lives in | What it is | Where it is stored |
|---|---|---|---|
| **RAG API key** | MultimodalRAG (mm-rag) | Your personal credential. Resolves to *your* registry identity on every MCP/REST call. | Shown once at mint; you paste it into the gateway |
| **★ memory dataset** | MultimodalRAG | The ONE dataset that identity reads/writes as memory, plus its password | Server-side per-identity access store (not in your env) |
| **Header override** | pcai-llm-gateway | `X-API-Key: <your rag key>` attached to *your gateway key*, for the `mm-rag` server only | `mcp_server_creds`, Fernet-encrypted, write-only |

**Why the header override is the whole trick.** The gateway authenticates to
mm-rag with its own *server-level* credential (a fleet token or Secret). If
that were the only credential, every tenant's memories would land in the same
identity. The per-key override is merged **last-wins** over the server-level
auth, and mm-rag's delegation rule (D19) makes `X-API-Key` **outrank** a
co-presented `Authorization: Bearer`. So one request carries both, and the
per-user identity wins. That is what makes multi-tenant memory possible on a
single registered MCP server.

**You do not send the dataset name.** Because the ★ binding resolves
dataset *and* password server-side from the presented key, the gateway only
ever needs the API key. `X-Memory-Dataset` / `X-Dataset-Password` remain
supported (the legacy and enrollment-provisioned path, § 6) but are **not
required** when you own a ★ binding.

---

## 1. Mint yourself a RAG API key

**In the mm-rag UI: open `/access`, find the "Your API key" card, click Mint.**

That card is the SSO self-mint surface (D25). What it gives you: a
long-lived key bound to *your* registry identity that carries exactly your
current access — grants plus selections — and never expires.

```
POST /api/access/mint-key      (no body, or {"rotate": true} to replace)
-> {"status": "ok", "name": "<your identity>", "key": "<FULL KEY>",
    "rotated": false, "created": "...", "message": "..."}
```

> **The key is returned exactly ONCE**, in that response. Every later read
> (`GET /api/access/key`) returns the **masked** form even to its owner —
> key material never appears there. Copy it before you navigate away.

### Preconditions (each has a distinct, honest refusal)

| Condition | Response | Meaning |
|---|---|---|
| `security.accessSelfMint: true` **and** an SSO session | 200 + key | Normal path |
| Self-mint not enabled on the deployment | **409** "SSO self-mint is not enabled on this deployment (RAG_ACCESS_SELF_MINT)." | Ask the operator to set it |
| You are signed in with an **API key**, not SSO | **403** "Key minting is not a power an API key carries (D15/D17)" | Minting requires an SSO proof; a key can never mint a key |
| You are an **admin** | card hidden / 403 | Admins hold the D17 panel instead of self-mint |

**Chart knob:** `security.accessSelfMint` — **default `false`** (opt-in since
the 2026-10-16 P0-1 remediation, because a default-ON mint let a spoofed proxy
identity obtain a durable key). The chart renders
`RAG_ACCESS_SELF_MINT: "true"` only when that value is true. It also requires
`security.accessStore: true` (the default) and an SSO path
(`oidc.enabled` or `security.trustProxyIdentity`) — self-mint does nothing
without them.

**No SSO on your deployment?** Two supported fallbacks:

- **Admin mints for you** — `/access` → admin panel → *Mint key…* (D17),
  supplying your name and dataset list. Same storage, same semantics.
- **You never mint at all** — use **enrollment-time provisioning** (§ 6),
  where redeeming an invite code creates the dataset *and* stamps the header
  pair onto the fresh gateway key, so no manual step exists.

---

## 2. Create your memory dataset — and star it

The dataset is where memories actually land. Two routes, and **both end with a
★**, which is what makes the whole chain work without sending a dataset name
around.

### The button (recommended — the star is automatic)

On the mm-rag homepage, when you have no memory binding, a panel appears:

> **Give your agent long-term memory** — [Create memory dataset]

That button is not a plain "create". It runs a four-step idempotent flow
(`createMemoryDataset` in the SPA):

1. `POST /api/datasets` — create (a 409 "already exists" is treated as success,
   so a retry resumes rather than fails)
2. `POST /api/datasets/{name}/select` — self-select it for your key
3. `POST /api/access/memory-dataset` `{"dataset": "<name>"}` — **bind the ★**
4. refresh the whoami flags and flip the panel to the bound state

So **using the button makes the star automatic** — you are done at step 2 of
this guide. The suggested name is `<your-identity>-memory`.

> The button is disabled when your key cannot create datasets. The tooltip says
> so; the fix is `security.datasetsCreateAllowed: true` (default) or a per-user
> `can_create_datasets` override on `/access`.

### The manual route (create, then star by hand)

1. **Create** the dataset on the Datasets tab (give it a password if you want
   one — see the caveat below), or `POST /api/datasets`.
2. **Select** it (the checkbox on `/access`) — for a password-protected
   dataset this is where you enter the password **once**; it is saved per
   identity, so later reads and memory writes need no password header.
3. **Star it** — click the ☆ on that dataset's row; it becomes ★.

Only one dataset can hold the ★ at a time; starring another moves it.

### What the ★ actually enforces

`POST /api/access/memory-dataset`:

- **`{"dataset": "<name>"}`** binds it — but only if the dataset is both
  **accessible** (operator ACL or your own selection) *and* **writable**. A
  read-only grant is refused with *"bind a dataset you can write"* (D26). This
  is deliberate: the ★ is your one *writable* memory store.
- **`{}`** (or omitting the field) clears the binding. Clearing always works.
- Requires the access store (`RAG_ACCESS_STORE` / `security.accessStore: true`)
  and a **per-user key**. An admin key cannot bind (admins hold deployment
  authority, not a personal identity) — you will get a 409.

**Password or no password?** Both work.

- **No password** — simplest, and the one-click button's default.
- **With a password** — the ★ still resolves it, *provided* you selected the
  dataset (which saves the password per identity). The password then never
  needs to reach the gateway at all. If instead you hand the gateway
  `X-Memory-Dataset` + `X-Dataset-Password` headers, the password lives in
  `mcp_server_creds` (encrypted, write-only) — that is the enrollment path.

### Resolution order (memorize this; it explains most surprises)

**Dataset** (`_resolve_memory_dataset`):

```
X-Memory-Dataset header  ->  your ★ binding  ->  MEMORY_DATASET env  ->  explicit dataset_name argument
```

The explicit `dataset_name` **argument is last and only applies when nothing
above resolved** — an argument that *disagrees* with a resolved binding is
**refused**, not honoured. That is a prompt-injection guard: a model cannot
retarget memory writes at another identity's store.

> **`RAG_MEMORY_DEFAULT` does not work today.** The chart renders it
> (`security.memoryDefault`) and the mm-rag module docstring lists it as a
> rung between the ★ and `MEMORY_DATASET`, but **no code reads it** — the
> constant `MEMORY_DEFAULT_ENV` is defined and never consulted, so the
> deployment-wide fallback dataset is silently inert. Set `MEMORY_DATASET`
> on the mm-rag container instead, or rely on ★ bindings.

**Password** (`_resolve_memory_password`):

```
explicit arg  ->  X-Dataset-Password header  ->  saved selection password  ->  RAG_MEMORY_PASSWORD env
```

---

## 3. Attach the RAG key to your gateway key

Now teach the gateway to present *your* RAG key on calls to `mm-rag`. Two
surfaces do the same write; pick by who you are.

### 3a. Self-service (no admin needed)

On the gateway homepage, click **MCP tools** (top bar) → the dialog lists your
servers → find `mm-rag` → enter:

| Field | Value |
|---|---|
| Header name | `X-API-Key` |
| Value | the key you minted in § 1 |

→ **Save cred**. `PUT /v1/mcp/creds/mm-rag` with `{"server":"mm-rag","headers":{"X-API-Key":"..."}}`.

> **Gotcha — the server must already be visible in that dialog.** Under the
> default `explicit` grant mode the dialog lists only servers on which your key
> holds **at least one granted tool**, and **omits a server entirely** when it
> holds none. If `mm-rag` is not shown, the credential form for it is
> unreachable here: ask your admin for a grant (one `mm-rag` tool is enough), or
> use the admin path below. This is by design — withheld servers must not even
> appear as probeable names. (Under the legacy `scope` grant mode a
> policy-admitted pair shows without a held grant, so visibility comes free —
> but `scope` mode is the legacy posture, not the default.)
>
> Note the credential write itself does **not** check tool grants (only that
> the server is registered), so the REST call works even when the dialog hides
> the row — the dialog is a UI visibility rule, not an authorization rule.

### 3b. Admin console

**Admin → Keys → `creds` on the target key** → header rows → Save.
`PUT /admin/keys/{key_id}/mcp-creds` with the same body.

### Rules that apply to both

- **Write-only.** Values are Fernet-encrypted; no endpoint ever returns them.
  `GET` lists header **names** only, so the form always starts empty.
- **The set is replaced wholesale per server.** Saving `{"X-API-Key": "..."}`
  replaces whatever that key had stored for `mm-rag`. To add a second header,
  send both in one call. To clear, use `DELETE`, not an empty PUT (an empty
  PUT is refused with 400).
- **Validation:** names `[A-Za-z0-9-]+`, ≤16 headers, values ≤512 chars.
  Reserved names are refused — the gateway stamps `X-MCP-Caller`,
  `X-MCP-Caller-Subject`, `X-MCP-Caller-Class` *after* the merge
  (attribution-never-authorization), plus hop/routing headers
  (`Host`, `X-Forwarded-*`, `Connection`, …) a caller must not choose.
- **Needs the master key.** `PCAI_LLM_SECRETS_MASTER_KEY` must be set or every
  write 503s (`secretbox.enabled()` is false).
- **Rotation carries it.** `POST /admin/keys/{id}/rotate` copies
  `mcp_server_creds` verbatim (`creds_copied` in the response) — the memory
  identity survives rotation with no re-prompt and no re-encryption.
- **Scope guard.** The override applies to *that key's* calls to *that server*
  only. It grants nothing: credentials are not grants.

### Verify the merge

```
GET /admin/keys/{key_id}/mcp-creds
-> [{"server": "mm-rag", "header_names": ["X-API-Key"]}]
```

Names only — that is the expected answer, not a truncated one.

---

## 4. The distiller: what it needs, and how it decides

The gateway's session distiller turns quiet conversations into a structured
project-summary memory and writes it to your ★ dataset. It is **inert by
construction** for keys that are not attached — that is the property that makes
it safe to leave on fleet-wide.

### Attachment is a CAPABILITY probe, not a config check

Before spending a single model token, the gateway calls the memory server's
probe tool **with your key's own headers**:

```
memory_status()  ->  {memory_available, dataset, writable, unlocked, detail}
```

`attached = memory_available AND writable`

The *backend* answers about *its own caller* (it resolves your identity from
the key you presented). The gateway never guesses a dataset name — it does not
know mm-rag's dataset model and must not. Cached per key for **60 s**; probe
timeout **10 s**; memory writes get **30 s**.

- Not attached → `200 {"ok": false, "inert": true}`, and **nothing runs**.
  A 200 (not an error) keeps fire-and-forget harness shims error-free while
  still letting a caller that cares distinguish the no-op.
- Probe errors read as **not available** (fail-safe inert), never raise.

So a correct § 1–3 chain shows up here as
`memory_available: true, writable: true`, with `dataset` naming your ★.

### Runtime gates (both must be true)

| Gate | Where | Default |
|---|---|---|
| `PCAI_LLM_DISTILLER_ENABLED` (`settings.distillerEnabled`) | env / chart | **false** — the routes are *unmounted* (404) |
| `enabled` | Admin UI → Memory Distiller (DB-backed) | true |

Env false = the surface does not exist. Env true + admin toggle off = 503
"distiller paused by admin". The admin panel also tunes the summarizer model,
off-peak-only scheduling, the quiet window and the output cap — **no redeploy
needed** for those.

### Chart keys

```yaml
settings:
  distillerEnabled: true              # PCAI_LLM_DISTILLER_ENABLED — master switch
  distillerMemoryServer: "mm-rag"     # _DISTILLER_MEMORY_SERVER — registry name
  distillerMemoryTool: "add_memory"   # _DISTILLER_MEMORY_TOOL
  distillerMemoryProbeTool: "memory_status"  # _DISTILLER_MEMORY_PROBE_TOOL
  distillerGateBackoffS: 900          # _GATE_BACKOFF_S — skip backoff (0 = retry every pass)
  distillerRawFallback: false         # _RAW_FALLBACK — see below
  distillerMinDocChars: 100           # _MIN_DOC_CHARS — body floor (0 = off)
```

**`distillerRawFallback` deserves a decision, not a default.** On a model
failure *after* the gates passed:

- `true` (app default, the historical DSH contract) — write the raw transcript
  as `session_history` so a session is never lost. **Cost:** mm-rag chunks a raw
  transcript (measured: 126 fragments from one session), which dilutes recall.
- `false` (the shipped G2 example) — leave the row due and retry. Nothing is
  lost because `canonical_conversations` still holds the conversation, and the
  memory store only ever receives distilled summaries.

### Verify end to end

```bash
python3 scripts/check_memory_feed.py            # one-shot
python3 scripts/check_memory_feed.py --wait 600 # sample, wait, re-sample
```

It checks, in order: the deployed image tag + distiller env gates → the
Postgres error log for distiller SQL faults → that the sweep is advancing
(newest `canonical_distills` marker) → that the dataset `document_count` is
**moving between two samples**. Exit 0 = feed healthy.

> **A moving count is the only evidence.** The 2026-10-14 outage was found by a
> human noticing a flat count hours after rollout — logs were clean (the fault
> was swallowed at `log.debug`) and `/health` was 200 throughout. Pods Ready
> and a tag live are *claims*.

---

## 5. Using a different memory MCP (portability contract)

The distiller is **not mm-rag-specific**. Any backend satisfying this contract
integrates by changing chart values — no code:

1. **Registry row.** Register the server normally (`POST /admin/mcp-servers`);
   the URL comes from the registry and is SSRF-validated at registration. There
   is deliberately **no free-form memory-URL env** — that surface stays closed.
2. **A write tool** that accepts `{text, metadata}` — `distillerMemoryTool`.
   The gateway sends `metadata: {kind, session_id, source, prompt_version}`;
   `kind` is `project-summary` or `session_history`. Extra metadata is the
   backend's business.
3. **A capability probe tool** — `distillerMemoryProbeTool` — taking no
   arguments and returning JSON with at least
   `{memory_available: bool, writable: bool}` (optionally `dataset`,
   `unlocked`, `detail`). It must answer **about its own caller**, resolved
   from the presented credential; the gateway never passes an identity.
4. **Per-user identity by request header.** The backend must let a per-key
   header override select the caller's identity (the mm-rag pattern:
   `X-API-Key`, or a legacy `X-Memory-Dataset`/`X-Dataset-Password` pair).
   Without this, every tenant shares one memory store — the thing the override
   exists to prevent.

Set `distillerMemoryServer` / `distillerMemoryTool` / `distillerMemoryProbeTool`
to your backend — **and `enrollMemoryServer` too**, for the reason in the first
gap below.

### Two known gaps in the current implementation

Documented because they bite an operator who renames servers, and because the
code comments claim otherwise:

- **Probe and write can resolve *different* servers.** The probe gate uses
  `settings.distiller_memory_server`; the write path
  (`routes_distill._mcp_call_for`) uses `settings.enroll_memory_server`.
  Both default to `mm-rag`, so the default deployment is correct — but if you
  point `distillerMemoryServer` at a non-default backend **you must also set
  `enrollMemoryServer` to the same name**, or the gate will probe one server
  and the write will target another.
- **The "tools/list fallback" described in `config.py` does not exist.**
  That comment says a backend without the probe tool "degrades to tools/list
  presence checking". No such fallback is implemented: a backend lacking the
  probe tool fails the probe, and the distiller stays inert. If you cannot
  supply a probe tool, this integration path is closed today.

---

## 6. Let the gateway do it for you (enrollment provisioning)

If you would rather not mint, create, and paste by hand: an enrollment
redemption can provision the whole chain.

- The redeem request takes `memory_opt_in` (`bool | null`). `null`/omitted
  falls back to the profile preset (`"memory": {"provision": true}`), then to
  the deployment default `PCAI_LLM_ENROLL_MEMORY_DEFAULT` (**false**).
  Precedence: request > profile > deployment.
- The gateway creates `mem-<tenant>-<user>-<key6>` on the registered server,
  then stamps `X-Memory-Dataset` + `X-Dataset-Password` into that key's
  `mcp_server_creds` row — the same write-only path as § 3, so **the dataset
  name and password ride the headers** rather than a ★ binding.
- **Never fatal, never deleting.** The key is created first (create → provision
  → stamp), so every failure mode reports `memory: {provisioned: false, error}`
  and still returns a live key. The gateway provisions and binds; deleting a
  dataset stays a RAG-side operation.
- Requires the server row to be registered + enabled and
  `PCAI_LLM_SECRETS_MASTER_KEY` to be set.

If you use this path you do **not** need § 1 or § 2.

> **Retargeting a provisioned key.** The `X-Memory-Dataset` header is checked
> *before* the ★, so starring a different dataset will **not** move a
> provisioned key's memory — its stamped header keeps winning. To retarget it,
> update or `DELETE` that header override (§ 3), or clear the `X-Memory-Dataset`
> entry and let the ★ take over.

---

## 7. Other memory clients

The same RAG key works for harness-side memory, which is independent of the
gateway distiller (both write to the same ★ store):

- **DSH session-memory distiller** — `@deepseek-ai/dsh-session-memory-distiller`
  in the profile's `cordis.patch.yml`. Needs `provider` + `model` set, and the
  transport config (`url` + `RAG_MEMORY_DATASET`/`RAG_MEMORY_PASSWORD` in the
  DSH process env, or a saved ★ binding). Without provider+model every write
  falls back to the raw transcript.
- **opencode** — one `rag` connection, bearer-authenticated
  (`MM_RAG_BEARER_TOKEN` = your minted key); dataset and password resolve
  server-side from the ★. Optional `RAG_MEMORY_DATASET` pins the header.
- **Open WebUI** — a filter using the RAG REST API; per-user dataset and
  password are HMAC-derived from the SSO identity, not stored in valves.

> **A key-only client needs no memory env vars at all.** Once the ★ exists, the
> only thing the client supplies is the key. That is the point of the binding.

---

## 8. Troubleshooting

Work top-down; each row is one link in the chain.

| Symptom | Likely link | Fix |
|---|---|---|
| `/access` shows no "Your API key" card | self-mint off, or admin, or API-key session | `security.accessSelfMint: true` + SSO; else admin mints (§ 1) |
| Mint returns **409** | self-mint disabled | operator knob above |
| Mint returns **403** | you are signed in with a key, not SSO | sign in via SSO — a key can never mint a key |
| Star click refused: *"bind a dataset you can write"* | read-only grant | get an rw grant, or bind a dataset you own |
| Star click refused: *"not accessible … select it first"* | dataset not in your world | select it (checkbox) first |
| `/v1/distill` returns `{ok:false, inert:true}` | not attached | run the probe (§ 4); check the header row exists |
| Probe says `memory_available: true, writable: false` | ro grant on the ★ | rw grant |
| Probe says `memory_available: false` | no ★ and no header dataset | star a dataset (§ 2) |
| Memory goes to the wrong dataset / a deleted one | stale `X-Memory-Dataset` header beats the ★ | update or DELETE the header override (§ 3) |
| `security.memoryDefault` set but ignored | `RAG_MEMORY_DEFAULT` is never read | use `MEMORY_DATASET` on the mm-rag container, or ★ bindings |
| Probe `detail` says *"not in the enabled registry"* | server name/row | register + enable `mm-rag`; check `distillerMemoryServer` |
| Probe `detail` says *"memory probe failed: …"* | transport/auth | check the credential, and see the fail-closed note below |
| Distiller 404s | env flag off | `distillerEnabled: true` (restart) |
| Distiller 503s | admin toggle off | Admin UI → Memory Distiller → enabled |
| Count flat, `ok=True` lines present | watching the wrong dataset | resolve which dataset the *key* uses — an enrolled key's store is not yours |
| Count flat, no `ok=True` lines | gate or extraction | see the stage-by-stage recipe in `check_memory_feed.py` |

### The fail-closed trap (worth its own paragraph)

An **unknown or revoked** `X-API-Key` does **not** fall back to the gateway's
server-level token — mm-rag resolves no identity and answers **401**. That is
correct security posture, and it means a stale key looks like a *transport*
failure in the distiller logs (`memory probe failed`), not an auth one.

Rotating your RAG key therefore requires re-saving the header override (§ 3) —
**unless** the gateway key itself is rotated, in which case
`mcp_server_creds` rides along automatically. Two different rotations; only
the RAG-side one needs manual work.

---

## 9. Quick reference

```
# --- mm-rag side ---------------------------------------------------------
POST /api/access/mint-key                      -> key (shown once)
GET  /api/access/key                           -> masked view of your key
POST /api/access/memory-dataset {"dataset": X} -> bind ★
POST /api/access/memory-dataset {}             -> clear ★
GET  /api/access/selections                    -> {selections, memory_dataset}
POST /api/datasets                             -> create
POST /api/datasets/{name}/select {"password"}  -> select (+ save password)

# --- gateway side --------------------------------------------------------
PUT    /v1/mcp/creds/mm-rag            {"server","headers"}   # self-service
GET    /v1/mcp/creds                                          # names only
DELETE /v1/mcp/creds/mm-rag
PUT    /admin/keys/{id}/mcp-creds      {"server","headers"}   # admin
POST   /admin/keys/{id}/rotate                                # creds_copied
POST   /v1/distill                     {"transcript","session_id"?}
POST   /v1/distill/conversations/{id}
GET/PUT /admin/distiller/config
```

| Concern | Value |
|---|---|
| Probe tool contract | no args → `{memory_available, writable, dataset?, unlocked?, detail?}` |
| Probe TTL / timeout | 60 s / 10 s |
| Memory write timeout | 30 s |
| Attachment rule | `memory_available AND writable` |
| Inert response | `200 {"ok": false, "inert": true}` |
| Override merge order | server-level auth → per-key overrides (**last wins**) |
| Delegation precedence | `X-API-Key` outranks co-presented `Bearer` |
| Memory kinds | `project-summary`, `session_history` |

---

## See also

- [FEATURES.md](FEATURES.md) § Session memory distiller — the pipeline internals
- [FEATURES.md](FEATURES.md) § Per-key MCP credential overrides — storage and merge
- [FEATURES.md](FEATURES.md) § Enrollment-time memory datasets (opt-in at redemption) — the automatic path
- [DEPLOYMENT.md](DEPLOYMENT.md) § Underlying detail — the full env table
- [API.md](API.md) § 2.6 Session distiller — request/response shapes
- [VERIFICATION.md](changelogs/VERIFICATION.md) — smoke tests for enrollment memory
