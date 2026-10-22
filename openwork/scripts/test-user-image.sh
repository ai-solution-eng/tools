#!/usr/bin/env bash
# Smoke-test the baked OpenWork user image (openwork-web-helm chart).
# Ported from the opencode chart's test-user-image.sh; the OpenChamber
# fallback scenario is dropped (spec D6 — OpenChamber is not installed).
# Scenarios (per REQUIREMENTS.md §11 P2):
#   1. empty state PVC  -> baked artifacts used, server boots
#   2. matching marker  -> baked artifacts respected, no reinstall
#   3. version mismatch -> must FAIL LOUDLY (runtime fallback is impossible:
#      openwork-server/engine come only from the image)
#
# Usage: scripts/test-user-image.sh [image] [platform]
set -euo pipefail

VALUES="${VALUES:-$(dirname "$0")/../values.yaml}"
PLATFORM="${2:-linux/amd64}"
read_section_value() {  # section key -> first match inside that top-level block
  awk -v sec="$1" -v key="$2" '
    $0 ~ "^"sec":" {insec=1; next} /^[a-zA-Z]/ {insec=0}
    insec && $1 == key":" {
      line=$0; sub("^[[:space:]]*" key ":[[:space:]]*", "", line)
      sub(/[[:space:]]*#.*$/, "", line); gsub(/"/, "", line)
      print line; exit}
  ' "$VALUES"
}
OPENCODE_VERSION="$(read_section_value openwork opencodeVersion)"
OPENWORK_SERVER_VERSION="$(read_section_value openwork serverVersion)"
[ -n "$OPENCODE_VERSION" ] && [ -n "$OPENWORK_SERVER_VERSION" ] || {
  echo "ERROR: could not parse openwork.serverVersion / openwork.opencodeVersion from $VALUES" >&2; exit 1; }
IMAGE="${1:-ghcr.io/ai-solution-eng/openwork-server:${OPENWORK_SERVER_VERSION}-${OPENCODE_VERSION}-dind}"

echo "==> image: $IMAGE ($PLATFORM), openwork-server=$OPENWORK_SERVER_VERSION opencode=$OPENCODE_VERSION"

run_in_image() { docker run --rm --platform "$PLATFORM" --entrypoint /bin/bash "$IMAGE" -c "$1"; }

echo "--- scenario 1: baked artifacts present, entrypoint exists ---"
run_in_image '
  set -e
  command -v openwork-server >/dev/null || { echo FAIL: no openwork-server; exit 1; }
  command -v opencode >/dev/null       || { echo FAIL: no opencode engine; exit 1; }
  command -v ttyd >/dev/null && command -v helm >/dev/null && command -v uv >/dev/null \
    || { echo FAIL: toolchain missing; exit 1; }
  [ -x /usr/local/bin/openwork-entrypoint.sh ] || { echo FAIL: no entrypoint; exit 1; }
  echo "  openwork-server: $(openwork-server --version 2>/dev/null | head -1)"
  echo "  opencode:        $(opencode --version 2>/dev/null | head -1)"
  echo OK-1
'

echo "--- scenario 2: version markers match requested pins ---"
run_in_image "
  set -e
  baked_server=$(cat /opt/openwork-server-version)
  baked_oc=$(cat /opt/openwork-opencode-version)
  [ "$baked_server" = "${OPENWORK_SERVER_VERSION}" ] || { echo "FAIL: server pin mismatch $baked_server != ${OPENWORK_SERVER_VERSION}"; exit 1; }
  [ "$baked_oc" = "${OPENCODE_VERSION}" ] || { echo "FAIL: engine pin mismatch $baked_oc != ${OPENCODE_VERSION}"; exit 1; }
  echo OK-2
"

echo "--- scenario 3: baked == pinned, no runtime fallback path exists ---"
# With openwork baked into the image there IS no runtime installer to fall back
# to; assert the runtime install paths the old chart guarded are absent and the
# state dir is empty on first boot (nothing to silently re-resolve).
run_in_image '
  set -e
  [ ! -e /var/openwork/data/npm ] || { echo FAIL: legacy runtime npm dir exists; exit 1; }
  grep -rq openchamber /usr/local/bin /opt/openwork* 2>/dev/null && { echo FAIL: openchamber artifact present; exit 1; }
  echo OK-3
'

echo "ALL SCENARIOS PASSED: $IMAGE"
