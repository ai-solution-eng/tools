#!/usr/bin/env bash
# Sanity-test a baked opencode user image locally (no cluster needed).
#
#   scripts/test-user-image.sh [image] [values-file]
#
# Verifies, inside the image itself (default platform linux/amd64):
#   1. the baked artifacts exist and match opencode.version /
#      openchamber.version / provisioning.helmVersion from the values file
#   2. replaying the router's REAL init guards (extracted from
#      templates/configmap-router.yaml) against an EMPTY simulated state PVC
#      takes the baked path — no npm install, no downloads, PVC aliases created
#   3. an EXISTING simulated PVC (own npm copy, matching versions) is respected
#   4. a version-mismatched PVC falls back to the runtime npm install
set -euo pipefail

cd "$(dirname "$0")/.."

PLATFORM="linux/amd64"
VALUES="${2:-values.yaml}"

# Section-aware values parsing (mirrors build-user-image.sh).
read_section_value() {
  local section="$1" key="$2"
  awk -v section="$section" -v key="$key" '
    $0 ~ "^" section ":" { insec = 1; next }
    insec && /^[^ ]/   { insec = 0 }
    insec && $0 ~ "^  " key ":" {
      sub("^  " key ":[[:space:]]*", "")
      gsub(/^"/, ""); gsub(/"[[:space:]]*$/, "")
      print; exit
    }
  ' "$VALUES"
}
OPENCODE_VERSION="$(read_section_value opencode version)"
OPENCHAMBER_VERSION="$(read_section_value openchamber version)"
[ -n "$OPENCODE_VERSION" ] && [ -n "$OPENCHAMBER_VERSION" ] || {
  echo "ERROR: could not parse opencode.version / openchamber.version from $VALUES" >&2
  exit 1
}
HELM_VERSION="$(read_section_value provisioning helmVersion)"
HELM_VERSION="${HELM_VERSION:-3.22.0}"
# Default image repo/tag follow the build script's convention:
# repo encodes both app versions, tag is the image revision (0.0.1).
IMAGE="${1:-ghcr.io/ai-solution-eng/opencode-${OPENCODE_VERSION}-openchamber-${OPENCHAMBER_VERSION}:0.0.1}"

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

echo "==> image: $IMAGE ($PLATFORM), opencode=$OPENCODE_VERSION openchamber=$OPENCHAMBER_VERSION helm=$HELM_VERSION"

# --- Extract the router's init guard commands from the chart source ---------
# (avoids drift: the test replays exactly what production runs).
GUARDS="$("$NODE" -e '
const fs = require("fs")
const src = fs.readFileSync("templates/configmap-router.yaml", "utf8")
const pats = ["/opt/opencode/npm/.opencode-version", "astral.sh/uv/install.sh", "ttyd/releases/download", "for b in uv uvx ttyd"]
for (let line of src.split("\n")) {
  if (!pats.some(p => line.includes(p))) continue
  line = line.trim()
  line = line.replace(/,\s*$/, "")                       // trailing JS comma
  // Allow `const NAME = <literal>` assignments (the npm baked guard).
  const assign = line.match(/^const [a-zA-Z0-9_]+ = (.*)$/)
  if (assign) line = assign[1]
  // Decode the JS string literal (double- or single-quoted).
  if (line.startsWith("\"")) {
    try { line = JSON.parse(line) } catch (e) { console.error("WARN: JSON.parse failed: " + line.slice(0, 60)); continue }
  } else if (line.startsWith("\x27")) {
    line = line.replace(/^\x27/, "").replace(/\x27$/, "")
    line = line.split("\\\x27").join("\x27")
  } else { console.error("WARN: unrecognized literal: " + line.slice(0, 60)); continue }
  // Unwrap a $RUN_AS_USER sh -c \x27...\x27 wrapper (platform-integration form).
  const m = line.match(/^\$RUN_AS_USER sh -c \x27(.*)\x27( \|\| true)?$/)
  if (m) line = m[1].split("\\\x27").join("\x27")
  console.log(line)
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
"$DOCKER" run --rm --platform "$PLATFORM" -e HELM_VERSION="$HELM_VERSION" "$IMAGE" bash -ec '
  echo "  opencode marker:  $(cat /opt/opencode/npm/.opencode-version)"
  echo "  openchamber marker: $(cat /opt/opencode/npm/.openchamber-version)"
  echo "  opencode bin:     $(command -v opencode)"
  echo "  openchamber bin:  $(command -v openchamber)"
  echo "  uv:               $(command -v uv) ($(uv --version 2>/dev/null | head -1))"
  echo "  ttyd:             $(command -v ttyd) ($(/opt/opencode/bin/ttyd --version 2>/dev/null | head -1))"
  echo "  helm:             $(command -v helm) ($(helm version --short 2>/dev/null | head -1))"
  case "$(helm version --short 2>/dev/null)" in "v${HELM_VERSION}"*) ;; *) echo "  FAIL: helm version mismatch (want v${HELM_VERSION})"; exit 1 ;; esac
  for c in bash curl git script tmux jq make python3 pip3 rg gh vim fd unzip zstd; do
    command -v "$c" >/dev/null || { echo "  MISSING: $c"; exit 1; }
  done
  echo "  toolchain:    all present"
'

echo
echo "==> [2/4] empty state PVC → baked path taken (no installs)"
mkdir -p "$SIM/empty/bin" "$SIM/empty/data" "$SIM/empty/home" "$SIM/empty/state" "$SIM/empty/cache"
"$DOCKER" run --rm --platform "$PLATFORM" -v "$SIM/empty:/var/opencode" \
  -e OPENCODE_VERSION="$OPENCODE_VERSION" -e OPENCHAMBER_VERSION="$OPENCHAMBER_VERSION" \
  -e TTYD_VERSION=1.7.7 -e RUN_AS_USER= \
  -v "$SIM/guards.sh:/guards.sh:ro" "$IMAGE" bash -ec '
  sh /guards.sh >/dev/null 2>&1
  [ -L /var/opencode/data/npm ] || { echo "FAIL: /var/opencode/data/npm is not aliased to the baked prefix"; exit 1; }
  [ "$(readlink /var/opencode/data/npm)" = "/opt/opencode/npm" ] || { echo "FAIL: wrong alias target"; exit 1; }
  [ -x /var/opencode/bin/ttyd ] || { echo "FAIL: /var/opencode/bin/ttyd missing"; exit 1; }
  [ -x /var/opencode/bin/uv ] || { echo "FAIL: /var/opencode/bin/uv missing"; exit 1; }
  [ ! -e /var/opencode/.opencode-version ] || { echo "FAIL: runtime install wrote the PVC (.opencode-version)"; exit 1; }
  [ ! -e /var/opencode/.openchamber-version ] || { echo "FAIL: runtime install wrote the PVC (.openchamber-version)"; exit 1; }
  echo "  PASS: PVC aliases in place, nothing installed"
'

echo
echo "==> [3/4] existing PVC with matching versions → respected, not clobbered"
mkdir -p "$SIM/owned/data/npm/bin" "$SIM/owned/bin" "$SIM/owned/home"
printf '#!/bin/sh\necho pvc-opencode\n' > "$SIM/owned/data/npm/bin/opencode"
printf '#!/bin/sh\necho pvc-openchamber\n' > "$SIM/owned/data/npm/bin/openchamber"
chmod 0755 "$SIM/owned/data/npm/bin/opencode" "$SIM/owned/data/npm/bin/openchamber"
echo "$OPENCODE_VERSION" > "$SIM/owned/.opencode-version"
echo "$OPENCHAMBER_VERSION" > "$SIM/owned/.openchamber-version"
"$DOCKER" run --rm --platform "$PLATFORM" -v "$SIM/owned:/var/opencode" \
  -e OPENCODE_VERSION="$OPENCODE_VERSION" -e OPENCHAMBER_VERSION="$OPENCHAMBER_VERSION" \
  -e TTYD_VERSION=1.7.7 -e RUN_AS_USER= \
  -v "$SIM/guards.sh:/guards.sh:ro" "$IMAGE" bash -ec '
  sh /guards.sh >/dev/null 2>&1
  [ -d /var/opencode/data/npm ] && [ ! -L /var/opencode/data/npm ] || { echo "FAIL: existing PVC npm dir was clobbered"; exit 1; }
  [ -x /var/opencode/data/npm/bin/opencode ] || { echo "FAIL: PVC opencode removed"; exit 1; }
  echo "  PASS: PVC copy untouched (version-consistent)"
'

echo
echo "==> [4/4] version mismatch → falls back to runtime npm install"
mkdir -p "$SIM/stale/data/npm/bin" "$SIM/stale/bin" "$SIM/stale/home"
printf '#!/bin/sh\necho pvc-opencode\n' > "$SIM/stale/data/npm/bin/opencode"
chmod 0755 "$SIM/stale/data/npm/bin/opencode"
echo "0.0.0-old" > "$SIM/stale/.opencode-version"
printf '#!/bin/sh\necho "NPM-STUB $*" >> /tmp/npm-stub.log\n' > "$SIM/npm-stub"
chmod 0755 "$SIM/npm-stub"
"$DOCKER" run --rm --platform "$PLATFORM" -v "$SIM/stale:/var/opencode" \
  -v "$SIM/npm-stub:/usr/local/bin/npm:ro" \
  -e OPENCODE_VERSION="9.9.9-test" -e OPENCHAMBER_VERSION="$OPENCHAMBER_VERSION" \
  -e TTYD_VERSION=1.7.7 -e RUN_AS_USER= \
  -v "$SIM/guards.sh:/guards.sh:ro" "$IMAGE" bash -ec '
  sh /guards.sh >/dev/null 2>&1 || true
  grep -q "install -g opencode-ai@9.9.9-test --prefix /var/opencode/data/npm" /tmp/npm-stub.log \
    || { echo "FAIL: runtime npm fallback NOT invoked on version mismatch"; cat /tmp/npm-stub.log; exit 1; }
  echo "  PASS: fallback npm install invoked with the right version/prefix"
'

echo
echo "ALL SCENARIOS PASSED — image $IMAGE behaves per the baked-image contract."
