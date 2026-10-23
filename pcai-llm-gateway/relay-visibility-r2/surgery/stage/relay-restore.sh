#!/bin/bash
# Wave R1 relay-render restore hook (surgery mode).
# Re-applies the relay-visibility patches to the DSH install tree after a
# pod restart / init-container reinstall wipes /opt/dsh. Idempotent: skips
# when the install tree already carries the patch. Version-guarded: only
# patches DSH 0.2.0-rc.1 (byte-baseline verified 2026-10-01); any other
# version is left untouched and the mismatch is logged.
set -u
VER="$(cat /opt/dsh/npm/.dsh-version 2>/dev/null || echo unknown)"
STAGE=/var/dsh/data/relay-r2
OC=/opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js
LIB=/opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js
if [ "$VER" != "0.2.0-rc.1" ]; then
  echo "[relay-restore] DSH version ${VER} != 0.2.0-rc.1; skipping relay patch (stale kit)"
  exit 0
fi
if [ ! -f "$STAGE/openai-completions.js" ] || [ ! -f "$STAGE/lib-index.js" ]; then
  echo "[relay-restore] staged files missing; nothing to do"
  exit 0
fi
if grep -q "pcaiRelay" "$OC" 2>/dev/null && grep -q "pcaiRelay" "$LIB" 2>/dev/null; then
  exit 0  # already patched
fi
if grep -q "pcaiRelay" "$OC" 2>/dev/null || grep -q "pcaiRelay" "$LIB" 2>/dev/null; then
  echo "[relay-restore] PARTIAL patch state; skipping to avoid a mixed install"
  exit 1
fi
cp "$STAGE/openai-completions.js" "$OC" && cp "$STAGE/lib-index.js" "$LIB" \
  && echo "[relay-restore] relay patches applied (DSH ${VER})"
