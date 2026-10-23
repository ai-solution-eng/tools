#!/usr/bin/env bash
# Wave R1 (r3) — DSH relay-visibility install. Run from the deepseek-harness repo root.
# Read-only-safe: verifies every step before writing; stops at the first mismatch.
set -euo pipefail

KIT="$(cd "$(dirname "$0")" && pwd)"
REPO="${1:-$HOME/Projects/deepseek-harness}"

echo "== target repo: $REPO"

# 0. Pristine baselines must match what this kit was built against.
PI="$REPO/node_modules/.pnpm/@earendil-works+pi-ai@0.85.1_patch_hash=*/node_modules/@earendil-works/pi-ai"
OC="$(echo $PI)/dist/api/openai-completions.js"
LIB="$REPO/packages/llm/llm-pi-ai/lib/index.js"
SRC="$REPO/packages/llm/llm-pi-ai/src/stream.ts"
TESTS="$REPO/packages/llm/llm-pi-ai/tests/stream-relay.spec.ts"

echo "== 1. pi-ai patch file (superset of the previous one; applies to the pristine 0.85.1 base)"
cp "$KIT/work/@earendil-works__pi-ai@0.85.1.patch" "$REPO/patches/@earendil-works__pi-ai@0.85.1.patch"

echo "== 2. live installed dist copy (same edits the patch carries)"
cp "$KIT/work/openai-completions.js" "$OC"

echo "== 3. llm-pi-ai src + compiled lib (git apply; the lib is the compiled form of the src)"
# r3 ships TWO adapter patches: -pristine (from the unmodified upstream files,
# for a fresh clone) and the plain one (r2 → r3, for a tree the r2 kit already
# patched). Try pristine first; fall back to the transition patch.
if git -C "$REPO" apply --check "$KIT/work/llm-pi-ai-relay-render-pristine.patch" 2>/dev/null; then
  echo "   (fresh tree → applying pristine-base patch)"
  git -C "$REPO" apply "$KIT/work/llm-pi-ai-relay-render-pristine.patch"
else
  echo "   (r2-patched tree → applying r2→r3 transition patch)"
  git -C "$REPO" apply "$KIT/work/llm-pi-ai-relay-render.patch"
fi

echo "== 4. relay stream tests"
cp "$KIT/work/tests/stream-relay.spec.ts" "$TESTS"

echo "== 5. sanity: node --check on both dist and lib"
node --check "$OC" && node --check "$LIB" && echo "OK"
echo "== done. Optional rebuild-from-source instead of the lib patch: pnpm run build:lib:host"
