---
name: memory-pulse-check
description: Use when asked whether automatic memory feeding is working, when the memory count seems stuck, when a session should have produced memories but did not, or for a periodic health check of the capture-sweep-distill-write pipeline. Verifies the gateway distiller loop end to end. NOT for deduping or pruning a bloated store (memory-curation), not for fixing the extractor, not for releasing an image (release-verify).
---

# Memory pulse-check (is the automatic feed actually feeding?)

The pipeline has FIVE stages, each with its own silent failure mode:

capture -> sweep select -> attachment gate -> transcript extraction -> add_memory write

A failure at ANY stage looks identical to the user: the count is not moving. Diagnose stage by stage; do not skip to fixes.

## Stage 1 - gates (config + attachment)

1. Deployed env (kubectl get deploy <gw> -n <ns> -o custom-columns=ENV:.spec.template.spec.containers[0].env): must carry DISTILLER_ENABLED=true, DISTILLER_MEMORY_SERVER (registry name), DISTILLER_MEMORY_PROBE_TOOL.
2. The key must be memory-ATTACHED: an mcp_server_creds row for the memory server. No row = the sweep SQL join never selects the key's rows.
3. The memory server must be in the enabled registry snapshot.

## Stage 2 - the sweep is selecting

grep 'distill sweep' on the app container logs. Healthy: 'N conversation rows + M canonical rows due, K attempted'.
- No such lines: housekeeping loop dead, or the offpeak_only gate closed (sweep waits for off-peak).
- due>0 attempted=0: rows gate-skipped or leased by another replica - read the skip lines.
- 'claimed by another replica' lines: normal under HPA scale-out with the 2026-10-11 lease fix.

## Stage 3 - extraction renders (the silent starver)

The classic failure: 'transcript collapsed to N chars from M messages' - the extractor rendered nothing. History: type==text-only filtering missed Responses-native part types (input_text/output_text) and reasoning summaries (fixed 2026-10-11); double-encoded rows (a JSONB string instead of an array, or a char-spread list) collapsed even harder (fixed same day). If a collapse line fires on a CURRENT build, the shape-sample line under it names the surviving gap (roles + content types, no bytes). Fix the extractor; NEVER mark a collapsed row distilled (a marker with no write = permanent silent data loss).

## Stage 4 - writes land (ok=True)

grep 'ok=True detail=' on the app container. Each line = one add_memory write to the CALLER KEY's memory dataset. ok=False: read the detail - registry missing vs RAG 4xx vs timeout. Repeated ok=False on one thread = a stuck row; check the memory server pod.

## Stage 5 - the dataset grows

get_dataset_info(document_count): snapshot, compare an hour later. Healthy under active chat: plus 1..3 docs per distilled session (mm-rag chunks long summaries). Flat count while ok=True lines exist = writes land in a DIFFERENT dataset than the one watched - resolve the key's X-Memory-Dataset stamp (enrolled per-key datasets are not the operator's own store).

## Known-failure catalogue (all hit in production 2026-10-09..11)

| Symptom in logs | Root cause | Status |
|---|---|---|
| collapsed to 17 chars, many messages | part-type filter / double-encoded rows | fixed (extractor hardening + recovery) |
| collapsed + marker written (old builds) | marked-distilled on empty render | fixed (deferral + epoch sentinel) |
| thread distilled twice, summaries seconds apart | HPA scale-out: N pods sweep, no claim | fixed (lease claim upsert) |
| thread re-distilled every turn | no content-hash skip | fixed (distilled_content_hash) |
| rows never due again | marker default now() beat row state | fixed (epoch sentinel on gate-skip INSERT) |
| prune deleted un-distilled rows | prune ignored gate-skip state | fixed (gate_failed_at IS NULL guard) |

When a NEW failure appears, add its row here - the catalogue is the skill's memory.

## Duplicate pairs (dedup belongs to memory-curation)

Pre-lease duplicate summaries (same session, same kind, seconds apart) are FUNCTIONAL duplicates with DIFFERENT wording (independent LLM generations) - text-similarity dedupe will NOT catch them; identify by session + kind + timestamp proximity. Deleting stored memories is a memory-curation operation (it owns id mapping and safety rails) - hand the cleanup there once the lease fix is deployed; do not improvise deletions during a pulse check.

## The 60-second recipe (run in order)

1. get_dataset_info: snapshot document_count.
2. Sweep lines: grep 'distill sweep' - attempted vs due; any collapsed or shape-sample lines?
3. Write lines: count ok=True since the snapshot; ok=False details if any.
4. get_dataset_info again: did the count move by roughly the ok=True count (times chunking)?
5. Verdict: moving = healthy. Flat + ok=True = wrong dataset watched. Flat + no ok=True = extraction or gate stage. No sweep lines = housekeeping or offpeak stage.

Verification truth chain for a JUST-DEPLOYED fix: pod image tag, then in-container __version__ (cat the init file, exec allowlist permitting), then stage 2-4 log lines. A deployed tag is a claim, not evidence (release-verify owns the full chain).

## Boundaries

- Store cleanup, dedupe, prune: memory-curation.
- Image build and rollout verification chain: release-verify.
- The distiller PLUGIN (provider, model, profile): harness-update territory; gateway-side extraction fixes are ordinary repo work.
- Deleting memories during a pulse check: NEVER. Diagnose, then hand to memory-curation.
