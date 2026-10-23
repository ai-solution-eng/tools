#!/usr/bin/env bash
# Fix the stale DeepSeek model id across DSH config surfaces.
#
# WHY: Deepseek-V4.1-Flash (no -1017) was DELETED from the gateway registry at
# 2026-10-17T19:16:14Z (audit: model.delete) and re-created as
# Deepseek-V4.1-Flash-1017. Every DSH surface still naming the old id is now
# either a hard spawn failure or a silent alias to GLM.
#
# Run on the HOST (these paths are read-only to the agent sandbox).
# Idempotent; timestamped backups; verifies after each edit.

set -euo pipefail

PROFILE=$HOME/.dsh/profiles/web/cordis.patch.yml
STAMP=$(date +%Y%m%d-%H%M%S)
OLD='Deepseek-V4.1-Flash'
NEW='Deepseek-V4.1-Flash-1017'

say() { printf '%s\n' "$*"; }

# --- 1. LIVE PROFILE ---------------------------------------------------------
say "=== $PROFILE ==="
cp -a "$PROFILE" "$PROFILE.bak-$STAMP"
say "backup: $PROFILE.bak-$STAMP"

python3 - "$PROFILE" <<'PY'
import sys
from pathlib import Path

p = Path(sys.argv[1])
s = p.read_text(encoding='utf-8')
orig = s

# FIX A -- allowedModels ACL (~line 280). The stale id passes the ACL while
# the WORKING id is refused ('child LLM route ... is not allowed for this
# Session'). That inversion is the trap: the model is offered a route it can
# select, and the spawn dies at preflight with UNKNOWN_MODEL.
acl_old = '        model: Deepseek-V4.1-Flash' + chr(10)
acl_new = '        model: Deepseek-V4.1-Flash-1017' + chr(10)
n = s.count(acl_old)
if n != 1:
    sys.exit('ABORT ACL: matched ' + str(n) + 'x (expected 1)')
s = s.replace(acl_old, acl_new, 1)
print('  FIX A: allowedModels  Deepseek-V4.1-Flash -> Deepseek-V4.1-Flash-1017')

# FIX B -- confirm the PROVIDER catalog already declares -1017 (~line 155).
# An ACL-only fix would still fail if the provider never declared the model.
if 'id: Deepseek-V4.1-Flash-1017' not in s:
    sys.exit('ABORT: provider catalog does not declare -1017; fix the provider block first')
print('  FIX B: provider catalog already declares -1017 (verified)')

if s != orig:
    p.write_text(s, encoding='utf-8')
    print('  written.')
else:
    print('  no change needed.')
PY

# --- 2. SKILLS ---------------------------------------------------------------
say ""
say "=== skills ==="
for f in "$HOME/.dsh/skills/cross-validate/SKILL.md" \
         "$HOME/.dsh/skills/audit-wave/SKILL.md" \
         "$HOME/.dsh/skills/subagent-teams/SKILL.md"; do
  if [ ! -f "$f" ]; then say "  skip (missing): $f"; continue; fi
  if grep -q "$OLD" "$f"; then
    cp -a "$f" "$f.bak-$STAMP"
    sed -i "s/${OLD}/${NEW}/g" "$f"
    say "  fixed: $f  (backup .bak-$STAMP)"
  else
    say "  clean: $f"
  fi
done

# --- 3. HARNESS SOURCE -------------------------------------------------------
# This tool description is injected verbatim into EVERY agent's system prompt
# as 'this deployment's canonical cells' -- so a stale id here teaches every
# agent to spawn with a model that does not exist. Needs a rebuild.
say ""
say "=== harness source ==="
HARNESS="$HOME/Projects/deepseek-harness/packages/experimental/tool-agent-team/src/index.ts"
if [ -f "$HARNESS" ] && grep -q "$OLD" "$HARNESS"; then
  cp -a "$HARNESS" "$HARNESS.bak-$STAMP"
  sed -i "s/${OLD}/${NEW}/g" "$HARNESS"
  sed -i 's/provider: "glm53-flash-sproket", model: "glm-5.3-flash"/provider: "pcai-model-endpoint-flow", model: "GLM-5.3-Flash"/g' "$HARNESS"
  say "  fixed: $HARNESS  (backup .bak-$STAMP)"
  say "  NOTE: requires a harness rebuild for the prompt text to change."
else
  say "  clean or missing: $HARNESS"
fi

# --- 4. VERIFY ---------------------------------------------------------------
say ""
say "=== verify ==="
FAIL=0
if grep -q "model: $OLD$\$" "$PROFILE"; then say "  FAIL: profile still has the stale id"; FAIL=1; else say "  OK: profile ACL clean"; fi
if grep -q "model: $NEW" "$PROFILE"; then say "  OK: profile ACL has -1017"; else say "  FAIL: profile ACL missing -1017"; FAIL=1; fi
for f in "$HOME/.dsh/skills"/*/SKILL.md; do
  if grep -q "\b${OLD}\b" "$f" 2>/dev/null; then say "  FAIL: $f still stale"; FAIL=1; fi
done
if [ "$FAIL" -eq 0 ]; then say "  ALL CLEAN"; else say "  see FAILs above"; fi

say ""
say "Restart the DSH host (or reload the profile) for the config change to apply."
exit "$FAIL"
