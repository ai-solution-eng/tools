#!/usr/bin/env bash
# Verify the chart's dsh source patches against a published npm release
# BEFORE spending minutes on an image build.
#
# Why this exists: the user image applies guarded source patches to the dsh
# npm tree (docker/user/Dockerfile). Upstream reorganises those files between
# releases, which breaks the build at step 4/9 after a ~145s build. This
# script downloads the tarballs and re-runs the SAME guards in seconds.
#
#   ./scripts/verify-dsh-patches.sh                 # version from values.yaml
#   ./scripts/verify-dsh-patches.sh 0.2.1-rc.2      # explicit version
#   ./scripts/verify-dsh-patches.sh --values values-g2.yaml
#
# Exit 0 = every guard passes (safe to build). Exit 1 = at least one patch no
# longer applies (fix the Dockerfile patch before building).
set -uo pipefail
cd "$(dirname "$0")/.."

VALUES="values.yaml"
VERSION=""
WORKDIR=""

usage() {
  sed -n '2,16p' "$0"
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --values) VALUES="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    -*) echo "unknown arg: $1" >&2; usage ;;
    *) VERSION="$1"; shift ;;
  esac
done

if [ -z "$VERSION" ]; then
  VERSION="$(grep -E '^  version:' "$VALUES" | head -1 | sed -e 's/^  version:[[:space:]]*//' -e 's/^"//' -e 's/"[[:space:]]*$//')"
fi
if [ -z "$VERSION" ]; then
  echo "ERROR: could not determine dsh.version (pass it as an argument)" >&2
  exit 1
fi

# Temp dir: prefer mktemp, but fall back to a repo-local scratch dir for
# restricted environments (CI sandboxes, locked-down TMPDIR) where mktemp
# cannot create anything.
WORKDIR="$(mktemp -d 2>/dev/null || true)"
if [ -z "$WORKDIR" ] || [ ! -d "$WORKDIR" ]; then
  WORKDIR="$(pwd)/.verify-dsh-patches.$$"
  mkdir -p "$WORKDIR" || { echo "ERROR: cannot create a temp dir" >&2; exit 1; }
fi
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

REGISTRY="https://registry.npmjs.org"
FAILED=0
CHECKED=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; CHECKED=$((CHECKED + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=$((FAILED + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$*"; }

# fetch <pkg> <version> <destdir> — download and extract one npm tarball.
fetch() {
  local pkg="$1" ver="$2" dest="$3"
  local url="$REGISTRY/$(echo "$pkg" | sed 's#/#%2F#')/-/$(basename "$pkg")-$ver.tgz"
  mkdir -p "$dest"
  if ! curl -fsSL --retry 2 --max-time 120 -o "$dest/pkg.tgz" "$url"; then
    return 1
  fi
  tar -xzf "$dest/pkg.tgz" -C "$dest" || return 1
  return 0
}

say "==> verifying dsh source patches for @deepseek-ai/dsh@${VERSION}"
say ""

# ---------------------------------------------------------------- root pkg --
if ! fetch "@deepseek-ai/dsh" "$VERSION" "$WORKDIR/dsh"; then
  say "ERROR: could not download @deepseek-ai/dsh@${VERSION} from npm" >&2
  exit 1
fi
ROOT="$WORKDIR/dsh/package"
say "==> resolved @deepseek-ai/dsh@${VERSION}"

# Node engine constraint: dsh itself declares none, but commander@15 requires
# >=22.12.0 and the image pins node:22.23.3-bookworm-slim. Report the floor so
# a future dependency bump that outgrows the pinned Node is caught here.
CMDR="$(python3 -c "
import json,sys
d=json.load(open('$ROOT/package.json'))
print(d.get('dependencies',{}).get('commander',''))
")"
say "==> commander requirement: ${CMDR:-<none>}"
NODE_FLOOR="$(python3 - "$CMDR" <<'PY'
import re, sys
spec = sys.argv[1] if len(sys.argv) > 1 else ''
m = re.search(r'(\d+)\.(\d+)\.(\d+)', spec)
print('%s.%s.%s' % m.groups() if m else '')
PY
)"
PINNED_NODE="$(grep -E '^FROM node:[0-9]' docker/user/Dockerfile | head -1 | sed -E 's/^FROM node:([0-9.]+)-.*/\1/')"
if [ -n "$NODE_FLOOR" ] && [ -n "$PINNED_NODE" ]; then
  if python3 -c "
import sys
def t(v): return tuple(int(x) for x in v.split('.')[:3])
sys.exit(0 if t('$PINNED_NODE') >= t('$NODE_FLOOR') else 1)"; then
    ok "node ${PINNED_NODE} satisfies commander >= ${NODE_FLOOR}"
  else
    bad "node ${PINNED_NODE} < commander requirement ${NODE_FLOOR} — bump the FROM line"
  fi
else
  skip "node floor undetermined (commander '${CMDR:-none}', pinned '${PINNED_NODE:-none}')"
fi

# --------------------------------------------------- transitive patch target --
# The isLoopback patch targets @deepseek-ai/dsh-client-connection, pulled in by
# @deepseek-ai/dsh-web-app. Resolve it the way the image's node_modules tree
# would: fetch the holder at the SAME version and read its dependency spec.
HOLDER="@deepseek-ai/dsh-web-app"
say ""
say "==> resolving $HOLDER -> @deepseek-ai/dsh-client-connection"
if ! fetch "$HOLDER" "$VERSION" "$WORKDIR/holder"; then
  bad "could not download ${HOLDER}@${VERSION}"
else
  CC_SPEC="$(python3 -c "
import json
d=json.load(open('$WORKDIR/holder/package/package.json'))
print(d.get('dependencies',{}).get('@deepseek-ai/dsh-client-connection',''))
")"
  say "    spec: ${CC_SPEC:-<not a dependency>}"
  CC_VER="$(python3 - "$CC_SPEC" <<'PY'
import re, sys
spec = sys.argv[1] if len(sys.argv) > 1 else ''
# npm ranges are ~x / ^x / exact; dsh pins siblings to one exact version
m = re.search(r'(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)', spec)
print(m.group(1) if m else '')
PY
)"
  if [ -z "$CC_VER" ]; then
    bad "cannot resolve a concrete version from '${CC_SPEC}'"
  elif ! fetch "@deepseek-ai/dsh-client-connection" "$CC_VER" "$WORKDIR/cc"; then
    bad "could not download @deepseek-ai/dsh-client-connection@${CC_VER}"
  else
    say "    version: ${CC_VER}"

    # ---- PATCH 1: isLoopback forced true (multi-user UI origin) -----------
    # Mirrors the Dockerfile RUN step exactly: any lib/client*.js that still
    # contains the upstream expression (0.2.1 split client.js into
    # client.browser.js + client.node.js).
    EXPR='isLoopback: transport?.ownsHost === true || pageLocation === void 0 || isLoopbackHostname(pageLocation.hostname)'
    MARKER='isLoopback: true /* dsh-web-helm: session-gated multi-user UI origin */'
    # Discover candidates with a glob (no find, no redirections) so this works
    # in locked-down shells where /dev/null itself is unwritable.
    CANDIDATES=""
    for f in "$WORKDIR/cc/package/lib"/client*.js; do
      [ -f "$f" ] && CANDIDATES="$CANDIDATES $f"
    done
    CANDIDATES="${CANDIDATES# }"
    if [ -z "$CANDIDATES" ]; then
      bad "patch 1 (isLoopback): no lib/client*.js in dsh-client-connection@${CC_VER}"
    else
      TARGET=""
      for f in $CANDIDATES; do
        if grep -Fq "$EXPR" "$f"; then TARGET="$f"; break; fi
      done
      if [ -z "$TARGET" ]; then
        bad "patch 1 (isLoopback): expression NOT found in: $(printf '%s ' $CANDIDATES)"
        say "       upstream changed the expression — update the Dockerfile patch"
      else
        ok "patch 1 (isLoopback): expression found in $(basename "$TARGET")"
        # Prove the substitution is unambiguous (exactly one occurrence) and
        # that the result is still the intended marker.
        COUNT="$(grep -Fo "$EXPR" "$TARGET" | wc -l | tr -d ' ')"
        if [ "$COUNT" = "1" ]; then
          ok "patch 1 (isLoopback): exactly 1 occurrence — sed substitution is unambiguous"
        else
          bad "patch 1 (isLoopback): ${COUNT} occurrences — sed would only rewrite the first"
        fi
      fi
    fi
  fi
fi

# --------------------------------------------------------------- summary ----
say ""
if [ "$FAILED" -eq 0 ]; then
  say "==> all ${CHECKED} guard(s) passed for dsh ${VERSION} — safe to build the image"
  exit 0
fi
say "==> ${FAILED} guard(s) FAILED for dsh ${VERSION} — fix docker/user/Dockerfile before building" >&2
exit 1
