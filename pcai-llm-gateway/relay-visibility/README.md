# Relay visibility — Wave R1 patch kit

Makes gateway-relayed (server-executed) MCP tool calls **visible in DSH** without
executing anything client-side. Three coordinated pieces; DSH-first rollout.

## What you get
- DSH renders `[tool] mm-rag__search_dataset({...args}) → ok (2450ms)` + a ≤300-char
  result preview in the Think panel, for calls the GATEWAY executed in-cluster.
- With the opt-in flag set (see below), the gateway also emits stamped inert
  tool-call deltas (`pcai.relay: true`); the patched pi-ai converts them into the
  tombstones and strips them from the message — DSH can never double-execute.
- Deterministic twin shadowing: a failed catalog fetch no longer silently flips
  whole conversations between handback (cards) and in-cluster (invisible) modes.

## Gateway (apply in THIS repo — ~/Code/HPE/PCAI_LLM — the shipping root).
## internal-projects/pcai-solutions are hardlinks of these files; the patch
## paths (src/pcai_llm/…) already match this root.
    git apply gateway/config.patch gateway/routes_proxy.patch gateway/toolrelay.patch
    cp testrun/test_toolrelay_r1.py tests/
    python3 hardlinker.py   # re-sync the hardlink views after apply
- New settings: `tool_relay_render_deltas` (default **false**),
  `tool_relay_render_index_base` (1000).
- Tombstone lines are enriched (args + bounded preview) for ALL clients; the
  `[tool] name →` prefix tests assert is preserved. 43 upstream tests pass
  unchanged + 5 new (test_toolrelay_r1.py).
- Deltas require BOTH the setting AND request header `X-PCAI-Relay-Deltas: 1`.

## DSH (apply inside ~/Projects/deepseek-harness)
1. Replace the pi-ai patch file (superset of the existing one; verified to apply
   to the pristine 0.85.1 base):
       cp dsh/@earendil-works__pi-ai@0.85.1.wave-r1.patch patches/@earendil-works__pi-ai@0.85.1.patch
2. Apply the adapter source + compiled lib patches:
       git apply dsh/llm-pi-ai-stream-relay-render.patch
       git apply dsh/llm-pi-ai-lib-index-relay-render.patch
   (the lib patch is the pre-built compiled form of the src patch — rerun
   `pnpm run build:lib:host` instead if you prefer building from source)
3. Make DSH opt in — in ~/.dsh/profiles/web/cordis.yml, the
   pcai-model-endpoint-flow provider `headers:` add:
       X-PCAI-Relay-Deltas: '1'
4. Restart DSH.

## Verification
- Gateway: `pytest tests/test_toolrelay.py tests/test_toolrelay_r1.py` (48 passed
  in the kit's testrun/ against the patched module).
- DSH: `tsc` clean on the modified stream.ts (strict, repo paths); patched dist
  and lib pass `node --check`; merged pi-ai patch applies cleanly to pristine.
- Live: run a prompt that triggers an in-cluster MCP call. Expect the enriched
  tombstone in Think; with the flag + header on, same shape rendered from the
  stamped deltas (indistinguishable by design).

## Safety notes
- Stamped deltas are inert by construction: only clients that send the header
  ever receive them, and the pi-ai patch strips them from message content.
- Tombstone previews are bounded (args 200, preview 300 chars) — no unbounded
  reasoning growth (the 2026-09-18 amplification lesson).
- Unstamped/handback behavior is byte-identical to today (guarded by tests).

---
## SUPERSEDED (DSH side) — 2026-09-30 18:0x
The DSH patches in `dsh/` (moved to `dsh-superseded-by-r2/`) are superseded by
`../relay-visibility-r2/`, which fixes three r1 bugs:
1. r1 tombstones always rendered `({})` — pi-ai's parsed args are empty at
   toolcall_start; r2 accumulates raw delta fragments instead.
2. r1 stripped relay blocks BEFORE finishBlock → index shift broke subsequent
   handback toolcall_end indexes; r2 strips after finishBlock.
3. r1's early splice misaligned the replay envelope → BlockAssembler dropped
   replayState on every relay turn (history degraded); r2 keeps indexes stable.
Gateway patches in this kit remain VALID and are already applied to the
shipping tree (see git status). DSH: apply ../relay-visibility-r2/apply-dsh.sh.
