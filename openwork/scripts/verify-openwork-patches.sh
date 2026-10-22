#!/usr/bin/env bash
# Verify a built OpenWork unit image matches the chart's contract
# (REQUIREMENTS.md §7, modelled on the dsh chart's verify-dsh-patches.sh).
# Usage: scripts/verify-openwork-patches.sh <image> [platform]
set -euo pipefail
IMAGE="${1:?usage: verify-openwork-patches.sh <image> [platform]}"
PLATFORM="${2:-linux/amd64}"

docker run --rm --platform "$PLATFORM" --entrypoint /bin/bash "$IMAGE" -c '
  set -e
  command -v openwork-server >/dev/null || { echo "FAIL: openwork-server missing"; exit 1; }
  command -v opencode >/dev/null || { echo "FAIL: opencode engine missing"; exit 1; }
  echo "openwork-server: $(openwork-server --version 2>/dev/null | head -1)"
  echo "opencode:        $(opencode --version 2>/dev/null | head -1)"
  echo "pinned server:   $(cat /opt/openwork-server-version)"
  echo "pinned engine:   $(cat /opt/openwork-opencode-version)"
  if find /usr/local/lib/node_modules -maxdepth 3 \( -name "ee" -o -name "den*" \) 2>/dev/null | grep -q .; then
    echo "FAIL: ee/ artifacts present"; exit 1
  fi
  if grep -rIl openchamber /usr/local/bin /opt/openwork* 2>/dev/null | grep -q .; then
    echo "FAIL: openchamber artifact present"; exit 1
  fi
  echo VERIFY-OK
'
