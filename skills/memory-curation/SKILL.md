---
name: memory-curation
description: Use when a memory store looks bloated or noisy, when recall returns transcript junk instead of facts, before any destructive memory migration or rebuild, for periodic hygiene, or when asked to "remember this" durably. Covers audit, dedupe, prune, rebuild, verify, back up, and recall hygiene. Do NOT use to build session distillation — the session-memory-distiller plugin already does that automatically.
---

# Memory curation (the distiller writes; this skill keeps the store honest)

## Find the store first — never hardcode its name

The RAG server resolves the memory dataset from the **identity**, not this skill: `X-Memory-Dataset` header → the caller's saved ★ binding → `MEMORY_DATASET` env → an explicit `dataset_name` argument, which applies **only when nothing above resolves**. An explicit name that **disagrees** with a resolved binding is **refused** (a prompt-injection guard, not a bug). So **discover the store; do not assume it**:

- `list_memories` with **no** `dataset_name` — its first line names the resolved store (`N memory(ies) in '<name>'`). That is the discovery call; `search_memory` / `add_memory` / `delete_memory` / `forget_session` resolve identically (just omit the argument).
- `get_dataset_info` is the **exception**: its `dataset_name` is required and is *not* binding-resolved — pass the discovered name. It is the source of the live `document_count`.
- Binding: `GET /api/access/selections` returns `memory_dataset`; `set_memory_dataset` sets the ★. Pass `dataset_name` **only** when the user names a *specific* store — a hardcoded name retargets the call or is refused.

## The distiller already exists — do not rebuild it

`@deepseek-ai/dsh-session-memory-distiller` is **live** in the profile's `cordis.patch.yml` (`provider`, `model`, `maxDistillTokens: 2048`, `distillTimeoutMs: 120000`, `rawTranscript: false`, `debounceMs: 45000`). **Cite it by package name, never by path** — the plugin moves, and a stale path is a false claim: `$DSH_PROFILE_DIR/node_modules/@deepseek-ai/<pkg>` is a symlink to the real source (any DSH plugin).

What it already does, with no help from you: on `user/message` | `assistant/message` | `assistant/chunk` | `tool/result` it debounces 45 s per session; on quiet or `session/disposed` it rebuilds the transcript and distills it into `[MEMORY INDEX | project: … | type: session-summary | topics: …]` plus `## What we did` / `## Key decisions & rationale` / `## Gotchas` / `## How to proceed`, then POSTs `add_memory` with `metadata.kind = "project-summary"`. On ANY distill failure it falls back to the raw transcript as `kind: "session_history"` — a session is never lost.

**Why map-reduce:** a DSH session can hold a 1M-token context but the distill model cannot, so long transcripts are split into ~200k-char windows, each compressed by its own call, then reduced. Constants: `MAX_HISTORY_CHARS` 8,000,000 · `MAP_CHUNK_CHARS` 200,000 · `MAX_MAP_CHUNK_CHARS` 700,000 · `MAX_MAP_CHUNKS` 24, adaptive so the WHOLE transcript is covered. **Superseded:** the single-call revision (`MAX_HISTORY_CHARS` 32,000) — describing that is stale.

**Do not write a distill pipeline, a session summarizer, or a "remember this session" script.** This skill owns the other half: audit, dedupe, prune, rebuild, verify, backup, recall hygiene. Changing the distiller itself is a separate job (installer script + `plugin-build/` in the memory-ops directory).

## First check when recall gets noisy

| Symptom | Cause | Action |
|---|---|---|
| Store fills with `session_history` transcript chunks; recall returns mid-sentence fragments | Distiller is falling back on every flush | Confirm `provider` + `model` are both set in the profile. Log line `no provider/model configured — every write will fall back to the raw transcript` is the tell |
| Nothing is written at all | Missing transport config | Distiller logs `disabled — need url + RAG_MEMORY_DATASET/PASSWORD in the dsh process env`; both are set in the DSH env file |
| Near-identical docs from a few sessions | Chunker bug — embedding dedup cannot see it | Text-ratio dedup, not cosine |
| Same content under different session ids | Session forks | Keep the longest fork per cluster (lossless) |

## Count before you touch anything

- **`list_memories` returns an ARBITRARY scroll sample, sorted by `memory_ts` afterward — it is NOT the true newest-N.** (`mcp_server.py`: `dm.scroll_documents(...)` then `rows.sort(key=_ts, reverse=True)`.) Never reason about counts or ordering from it; use it only to harvest `memory_id` values.
- Live count: `get_dataset_info` → `document_count` — **re-read it, never reuse this number**; it moves as the distiller writes.
- Full corpus: `GET /api/datasets/{name}/export` → a `.tar.gz` with `meta.json` (password hash stripped), `documents.jsonl` (`{"id","payload"}` NDJSON), and `files/`. Header `X-RAG-Api-Key`; dataset routes also need `X-Dataset-Password`. Sweep scripts read `documents.jsonl` — the only view with true generation, chunk-count, and text-ratio data.

## Back up before any destructive step

Non-negotiable — the pre-Aug-19 memory loss is the precedent. A rebuild with no export is data loss.

1. Export first (the tar.gz above). Keep the parsed corpus too — the memory-ops directory holds the curated cards, distilled per-project summaries, gap sessions, `fact-cards.json`, and the plan JSONs.
2. Restore: `POST /api/admin/datasets/import` (multipart `file`, or JSON `{"s3_uri": ...}`). **The export carries no vectors — a restore re-embeds**, so it is not a byte-identical rollback.
3. Run every script WITHOUT `--apply` first and diff its plan against the export.

## The three bloat root causes (reference incident, 2026-09-09: 1,044 docs, only ~196 curated)

1. **Sliding-window chunker bug.** During the Sep-5 mass flush the chunker advanced its cursor ~48 chars against a ~29.5k-char window; the three biggest sessions each stored 62–164 near-identical copies of the same block while declaring 3,126 / 1,168 / 809 chunks. Text ratio 0.9997–0.9999, but embedding cosine landed **just under 0.995** — ingest dedup passed them all.
2. **Mass re-flush after a harness/plugin update** — ~600 chunks in 10 minutes (Sep 5 16:06–16:16 UTC); count went 429 (Sep 3) → 1,043 (Sep 9).
3. **Session forks** — each fork gets a new session id and flushes its inherited transcript, creating cross-session duplicates. A fork's transcript is a superset of the parent's kept content, so **keeping the longest fork per cluster is lossless.**

After that sweep: 648 docs = 196 curated + 452 `session_history` across 300 sessions, zero cross-session near-dups; remaining 0.9+ pairs are within-session header/prompt repeats — benign, leave them.

## Dedup rules

- Ingest gate is cosine ≥ `RAG_DEDUP_THRESHOLD` (default `0.995`) — `add_memory` auto-skips near-dups, so a re-add can be a silent no-op. Always read `stored_ids`; never assume the count landed.
- **Cosine alone misses root cause #1.** Compare TEXT: normalize whitespace, then `difflib.SequenceMatcher` ratio ≥ ~0.999 on the body, grouped by `session_id` + `memory_ts` generation. Within a session keep the newest generation; across fork clusters keep the longest.
- `delete_memory` is **explicit-IDs-only by design** — you cannot delete what you have not listed. Split memories need every chunk id. Delete in a loop: list → match → delete → repeat until nothing is found.

## Tooling (the deployment's memory-ops directory)

Locate it before citing it (reference locations in the fenced section); the script NAMES are the recall value. **Sweep/analysis** — `analyze2.py` (generation, zombie, and near-dup analysis over `documents.jsonl`), `measure.py` (stored vs declared chunk counts per session), `verify_copies.py` (per-chunk text-ratio evidence). **Rebuild**:

| Script | What it does |
|---|---|
| `rebuild-via-gateway.py` | list all memory_ids → delete all → re-add the normalized set. Destructive; run from the operator's host terminal (it needs the admin key, not the MCP path) |
| `purge-transcripts.py` | purge transcript-fragment memories (dry-run default; `--apply` to delete) |
| `dedup-sweep.py` | content-duplicate memories (same body, different headers/sessions); keeps the newest. Idempotent |
| `sweep-untagged.py` | delete untagged docs only when a tagged twin exists; untagged docs with no twin are kept |
| `renormalize.py` | delete old-format docs and re-ingest the normalized set (needs the RAG API key + memory password; REST via `kubectl port-forward` to bypass the gateway) |
| `final-cleanup.py` | full clear-and-re-add cycle for a multi-generation store (dry-run default; `--apply`) |

Reuse instead of regenerating: the master index, the curated cards, `fact-cards.json`, and the plan JSONs (`reingest-plan-*.json`, `store-fixpoint.json`, `preserve-docs.json`).

## PROJECT MAP principle (settled 2026-10-02 — do not re-litigate)

The dataset is a **PROJECT MAP** — *what we worked on / how to access it / how to proceed* — built from distilled per-project summaries + atomic fact cards + a WHO/themes card. **Not raw transcripts.** Transcripts stay local (`$DSH_HOME/sessions/`) or in backup exports. Chunking: 8192 tokens default.

## Recall hygiene (what a good memory looks like)

- Every doc opens with `[MEMORY INDEX | project: <slug> | type: <type> | topics: a,b,c]` — the header is embedded, so topic queries hit it directly. Keep the slug one of the known project slugs.
- One fact per card; 3–8 lowercase topics; exact paths, digests, versions, error strings verbatim.
- Metadata `kind` values observed: `project-summary`, `fact-card`, `methodology`, `correction`, `note`; `session_history` is machine-written only (the distiller's fallback path).

## Verify after any migration

1. `get_dataset_info` `document_count` matches the plan (accounting for auto-skipped dupes).
2. Run 5–7 probe queries through `search_memory`; the right document must land at #1 (the reference rebuild scored 7/7 at rrf 1.0 — hold that bar). Then re-export and re-run the dup analysis: zero cross-session dups.

## Gotchas

- Gateway tools are **four-part**: `mcp__<gateway>__<rag-server>__list_memories`. Flat two-part names are refused as unknown tools. The reference deployment's alias is in the fenced section.
- `/tmp` does not persist between bash tool calls — make every verification one self-contained call.
- Never run `git checkout` / `restore` / `clean` / `stash` anywhere (see `guard-uncommitted-work`).
- `forget_session(session_id)` deletes ONLY that session's `session_history` docs — the tool for a session with sensitive material. It never touches curated memories.
- The store is typically password-protected (`has_password: true`); dataset routes need `X-Dataset-Password`. In-store deletion is irreversible; the export is the only recovery, and it restores by re-embedding.
- `lessons-loop` owns the **sql-lessons / sql-lessons-candidates** promotion gate — a different store and lifecycle; do not conflate it with memory-store curation. Before a destructive rebuild, apply `guard-uncommitted-work` to anything on disk and run `cross-validate` on the delete/keep plan when large.

## Reference instantiation (one deployment's values — re-derive locally)

Resolve your own equivalents (see PORTABILITY.md) before using any of these.

- Memory dataset `andrew-memory` (★ binding; `RAG_MEMORY_DATASET` in the DSH env file) — discover, never hardcode it.
- Gateway `pcai-llm-gateway`, RAG server `mm-rag` → `mcp__pcai-llm-gateway__mm-rag__list_memories`.
- Profile `~/.dsh/profiles/web` (`$DSH_PROFILE_DIR`); the distiller resolves via `$DSH_PROFILE_DIR/node_modules/@deepseek-ai/dsh-session-memory-distiller` → `~/Projects/dsh-plugins/packages/session-memory-distiller`. The old `~/Projects/deepseek-harness/packages/memory/session-memory-distiller` path no longer exists.
- Distiller config: `provider: pcai-model-endpoint-flow`, `model: Qwen3.8-27B`.
- Memory-ops dirs: `~/Projects/.memory-sweep/` (sweep/analysis) and `~/Code/HPE/MultimodalRAG/.memory-rebuild/` (rebuild; also `install-mapreduce.fish` + `plugin-build/`).
- Rebuild key `PCAI_LLM_ADMIN_KEY`; `renormalize.py` also wants `RAG_API_KEY` + `RAG_MEMORY_PASSWORD`.
- Parsed corpus (2026-10-04): `curated/` 570 md, `distilled/` 39 md, `dsh-gap/` 81 md, `fact-cards.json`. 2026-10-02 rebuild scored 7/7 probes at rrf 1.0.
