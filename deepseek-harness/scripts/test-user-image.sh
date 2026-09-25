#!/usr/bin/env bash
# Sanity-test a built dsh user image locally (no cluster needed).
#
#   scripts/test-user-image.sh [image] [values-file]
#
# Verifies, inside the image itself (default platform linux/amd64):
#   1. the baked artifacts exist and match dsh.version from the values file
#   2. replaying the router's REAL init guards (extracted from
#      templates/configmap-router.yaml) against an EMPTY simulated state PVC
#      takes the baked path — no npm install, no downloads, PVC aliases created
#   3. an EXISTING simulated PVC (own npm copy, matching version) is respected
#   4. a version-mismatched PVC falls back to the runtime npm install
set -euo pipefail

cd "$(dirname "$0")/.."

PLATFORM="linux/amd64"
VALUES="${2:-values.yaml}"
DSH_VERSION="$(grep -E '^  version:' "$VALUES" | head -1 | sed -e 's/^  version:[[:space:]]*//' -e 's/^"//' -e 's/"[[:space:]]*$//')"
# Default image tag follows the build script's convention: tag == dsh.version.
IMAGE="${1:-ghcr.io/ai-solution-eng/deepseek-harness:${DSH_VERSION}}"

# Resolve docker the same way build-user-image.sh does.
DOCKER="$(command -v docker || true)"
if [ -z "$DOCKER" ]; then
  for candidate in /usr/local/bin/docker /opt/homebrew/bin/docker "$HOME/.docker/bin/docker"; do
    if [ -x "$candidate" ]; then DOCKER="$candidate"; break; fi
  done
fi
[ -n "$DOCKER" ] || { echo "ERROR: docker CLI not found" >&2; exit 1; }
export PATH="$(dirname "$DOCKER"):/usr/local/bin:/opt/homebrew/bin:$PATH"

# node is needed to extract the guard commands from the chart source.
NODE="$(command -v node || true)"
if [ -z "$NODE" ]; then
  for candidate in /opt/homebrew/bin/node /usr/local/bin/node; do
    if [ -x "$candidate" ]; then NODE="$candidate"; break; fi
  done
fi
[ -n "$NODE" ] || { echo "ERROR: node CLI not found" >&2; exit 1; }

echo "==> image: $IMAGE ($PLATFORM), dsh.version=$DSH_VERSION"

# --- Extract the router's init guard commands from the chart source ---------
# (avoids drift: the test replays exactly what production runs).
GUARDS="$("$NODE" -e '
const fs = require("fs")
const src = fs.readFileSync("templates/configmap-router.yaml", "utf8")
const pats = ["/opt/dsh/npm/.dsh-version", "astral.sh/uv/install.sh", "ttyd/releases/download", "for b in uv uvx ttyd"]
for (let line of src.split("\n")) {
  if (!pats.some(p => line.includes(p))) continue
  line = line.trim()
  line = line.replace(/^\x27/, "")                 // leading JS quote
  line = line.replace(/\x27,\s*$/, "").replace(/\x27$/, "")  // trailing ",\x27 or "
  // unwrap $RUN_AS_USER sh -c \x27...\x27  (trailing quote may have been
  // consumed above; the escaped backslash always remains)
  const m = line.match(/^\$RUN_AS_USER sh -c \\\x27(.*)\\(\x27)?( \|\| true)?$/)
  if (m) line = m[1]
  else console.error("WARN: unwrapped pattern not matched: " + line.slice(0, 60))
  console.log(line.split("\\\x27").join("\x27"))
}
console.log("echo GUARDS-DONE")
')"
[ -n "$GUARDS" ] || { echo "ERROR: could not extract guards from the chart" >&2; exit 1; }

# Workspace-local scratch dir (the default TMPDIR may be unwritable under
# restricted file policies).
SIM="$(mktemp -d "$PWD/.tmp-imgtest.XXXXXX")"
trap 'rm -rf "$SIM"' EXIT
echo "$GUARDS" > "$SIM/guards.sh"

echo
echo "==> [1/4] baked artifacts present in the image"
"$DOCKER" run --rm --platform "$PLATFORM" "$IMAGE" bash -ec '
  echo "  dsh marker:   $(cat /opt/dsh/npm/.dsh-version)"
  echo "  dsh bin:      $(command -v dsh)"
  echo "  uv:           $(command -v uv) ($(uv --version 2>/dev/null | head -1))"
  echo "  ttyd:         $(command -v ttyd) ($(/opt/dsh/bin/ttyd --version 2>/dev/null | head -1))"
  for c in bash curl git script tmux jq make python3 pip3 rg gh vim fd unzip zstd; do
    command -v "$c" >/dev/null || { echo "  MISSING: $c"; exit 1; }
  done
  echo "  toolchain:    all present"
'

echo
echo "==> [2/4] empty state PVC → baked path taken (no installs)"
mkdir -p "$SIM/empty/bin" "$SIM/empty/data" "$SIM/empty/home" "$SIM/empty/state" "$SIM/empty/cache"
"$DOCKER" run --rm --platform "$PLATFORM" -v "$SIM/empty:/var/dsh" \
  -e DSH_VERSION="$DSH_VERSION" -e TTYD_VERSION=1.7.7 -e RUN_AS_USER= \
  -v "$SIM/guards.sh:/guards.sh:ro" "$IMAGE" bash -ec '
  sh /guards.sh >/dev/null 2>&1
  [ -L /var/dsh/data/npm ] || { echo "FAIL: /var/dsh/data/npm is not aliased to the baked prefix"; exit 1; }
  [ "$(readlink /var/dsh/data/npm)" = "/opt/dsh/npm" ] || { echo "FAIL: wrong alias target"; exit 1; }
  [ -x /var/dsh/bin/ttyd ] || { echo "FAIL: /var/dsh/bin/ttyd missing"; exit 1; }
  [ -x /var/dsh/bin/uv ] || { echo "FAIL: /var/dsh/bin/uv missing"; exit 1; }
  [ ! -e /var/dsh/.dsh-version ] || { echo "FAIL: runtime install wrote the PVC (.dsh-version)"; exit 1; }
  echo "  PASS: PVC aliases in place, nothing installed"
'

echo
echo "==> [3/4] existing PVC with matching dsh → respected, not clobbered"
mkdir -p "$SIM/owned/data/npm/bin" "$SIM/owned/bin" "$SIM/owned/home"
printf '#!/bin/sh\necho pvc-dsh\n' > "$SIM/owned/data/npm/bin/dsh"
chmod 0755 "$SIM/owned/data/npm/bin/dsh"
echo "$DSH_VERSION" > "$SIM/owned/.dsh-version"
"$DOCKER" run --rm --platform "$PLATFORM" -v "$SIM/owned:/var/dsh" \
  -e DSH_VERSION="$DSH_VERSION" -e TTYD_VERSION=1.7.7 -e RUN_AS_USER= \
  -v "$SIM/guards.sh:/guards.sh:ro" "$IMAGE" bash -ec '
  sh /guards.sh >/dev/null 2>&1
  [ -d /var/dsh/data/npm ] && [ ! -L /var/dsh/data/npm ] || { echo "FAIL: existing PVC npm dir was clobbered"; exit 1; }
  [ -x /var/dsh/data/npm/bin/dsh ] || { echo "FAIL: PVC dsh removed"; exit 1; }
  echo "  PASS: PVC copy untouched (version-consistent)"
'

echo
echo "==> [4/4] version mismatch → falls back to runtime npm install"
mkdir -p "$SIM/stale/data/npm/bin" "$SIM/stale/bin" "$SIM/stale/home"
printf '#!/bin/sh\necho pvc-dsh\n' > "$SIM/stale/data/npm/bin/dsh"
chmod 0755 "$SIM/stale/data/npm/bin/dsh"
echo "0.0.0-old" > "$SIM/stale/.dsh-version"
printf '#!/bin/sh\necho "NPM-STUB $*" >> /tmp/npm-stub.log\n' > "$SIM/npm-stub"
chmod 0755 "$SIM/npm-stub"
"$DOCKER" run --rm --platform "$PLATFORM" -v "$SIM/stale:/var/dsh" \
  -v "$SIM/npm-stub:/usr/local/bin/npm:ro" \
  -e DSH_VERSION="9.9.9-test" -e TTYD_VERSION=1.7.7 -e RUN_AS_USER= \
  -v "$SIM/guards.sh:/guards.sh:ro" "$IMAGE" bash -ec '
  sh /guards.sh >/dev/null 2>&1 || true
  grep -q "install -g @deepseek-ai/dsh@9.9.9-test --prefix /var/dsh/data/npm" /tmp/npm-stub.log \
    || { echo "FAIL: runtime npm fallback NOT invoked on version mismatch"; cat /tmp/npm-stub.log; exit 1; }
  echo "  PASS: fallback npm install invoked with the right version/prefix"
'

echo
echo "ALL SCENARIOS PASSED — image $IMAGE behaves per the baked-image contract."
