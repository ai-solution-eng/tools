#!/usr/bin/env bash
# Build (and optionally push) the baked OpenCode user image.
#
# The image bakes the apt toolchain, opencode-ai, @openchamber/web, uv and
# ttyd so user pods stop installing everything at boot. See
# docker/user/Dockerfile for the design contract (artifacts under
# /opt/opencode, guarded fallbacks).
#
# Usage:
#   scripts/build-user-image.sh                                # build for
#                              # linux/amd64, repo name derived from the
#                              # versions in values.yaml, tag 0.0.1
#   scripts/build-user-image.sh --tag 0.0.2 --push             # build + push
#   scripts/build-user-image.sh --values values-g2.yaml
#
# Sources of truth (kept in sync automatically):
#   - apt package list:   provisioning.aptPackages in the selected values file
#   - opencode version:   opencode.version in the selected values file
#   - openchamber version: openchamber.version in the selected values file
#   - image repo name:    ghcr.io/ai-solution-eng/opencode-<opencode.version>\
#                         -openchamber-<openchamber.version> (override --repo)
#   - image tag:          --tag (default 0.0.1) — the image revision; an app
#                         version bump mints a new repo name, --tag iterates
#                         image-only fixes
#   - ttyd version:       --ttyd-version flag (default 1.7.7; must mirror the
#                         ttydVersion const in templates/configmap-router.yaml)
#   - helm version:       provisioning.helmVersion in the values file (default
#                         3.22.0; --helm-version overrides)
#
# PROXY SUPPORT (behind a corporate proxy / DinD):
#   Nothing proxy-related is stored in this repo. The script reads the standard
#   HTTP_PROXY / HTTPS_PROXY / NO_PROXY (or lowercase) variables FROM THE
#   ENVIRONMENT at run time and forwards them as build-args — the same contract
#   as the `dockerbuild` wrapper baked into the user image (see
#   docker/user/Dockerfile). Values are NEVER echoed, so a proxy URL carrying
#   credentials cannot leak into CI logs or a committed file.
#
#   When a proxy is set the script also adds `--network host`, because inside
#   DinD the daemon's default bridge cannot reach the proxy and Docker's
#   implicit proxy forwarding does not reach RUN steps. Pass --no-network-host
#   to suppress it (e.g. on Docker Desktop/macOS, where it is unsupported).
#
#     export HTTPS_PROXY=http://user:pass@proxy.example:8080
#     export NO_PROXY=.cluster.local,.svc,10.0.0.0/8
#     scripts/build-user-image.sh --values values-g2.yaml --push
#
#   NO_PROXY is always extended with 127.0.0.1, ::1 and localhost so buildx's
#   own gRPC dial to the daemon is never proxied.
set -euo pipefail

cd "$(dirname "$0")/.."

VALUES="values.yaml"
# TAG="0.0.1" The tag is now specified below
TTYD_VERSION="1.7.7"
HELM_VERSION=""
PLATFORM="linux/amd64"
IMAGE_REPO=""
PUSH=0
LOAD=0
# 1 = allow '--network host' when a proxy is configured (default). Set 0 with
# --no-network-host on Docker Desktop/macOS, which does not support it.
NETWORK_HOST=1

usage() {
  # Print the header comment block (everything above 'set -euo pipefail'),
  # so the proxy section is included automatically as the header grows.
  sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --values) VALUES="$2"; shift 2 ;;
    --ttyd-version) TTYD_VERSION="$2"; shift 2 ;;
    --helm-version) HELM_VERSION="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --repo) IMAGE_REPO="$2"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --load) LOAD=1; shift ;;
    --no-network-host) NETWORK_HOST=0; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

if [ ! -f "$VALUES" ]; then
  echo "ERROR: values file not found: $VALUES" >&2
  exit 1
fi

# Resolve the docker CLI (non-interactive shells often have a minimal PATH —
# on macOS Docker Desktop installs to /usr/local/bin/docker).
DOCKER="$(command -v docker || true)"
if [ -z "$DOCKER" ]; then
  for candidate in /usr/local/bin/docker /opt/homebrew/bin/docker "$HOME/.docker/bin/docker"; do
    if [ -x "$candidate" ]; then DOCKER="$candidate"; break; fi
  done
fi
if [ -z "$DOCKER" ]; then
  echo "ERROR: docker CLI not found (install Docker Desktop or add docker to PATH)" >&2
  exit 1
fi
# Credential helpers (docker-credential-*) live next to the CLI and in
# Homebrew — non-interactive PATHs miss them, which breaks registry auth
# during metadata pulls. Export a PATH that can resolve them.
DOCKER_BIN_DIR="$(dirname "$DOCKER")"
export PATH="$DOCKER_BIN_DIR:/usr/local/bin:/opt/homebrew/bin:$PATH"

# Section-aware values parsing: `version:` appears under both `opencode:` and
# `openchamber:` (and aptPackages under `provisioning:`), so grab the first
# two-space-indented key AFTER the section header.
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

APT_PACKAGES="$(grep -E '^  aptPackages:' "$VALUES" | head -1 | sed -e 's/^  aptPackages:[[:space:]]*//' -e 's/^"//' -e 's/"[[:space:]]*$//')"
OPENCODE_VERSION="$(read_section_value opencode version)"
OPENCHAMBER_VERSION="$(read_section_value openchamber version)"
if [ -z "$APT_PACKAGES" ] || [ -z "$OPENCODE_VERSION" ] || [ -z "$OPENCHAMBER_VERSION" ]; then
  echo "ERROR: could not parse provisioning.aptPackages / opencode.version / openchamber.version from $VALUES" >&2
  exit 1
fi

# Helm CLI version: --helm-version flag wins, else provisioning.helmVersion
# from the values file, else the script default.
if [ -z "$HELM_VERSION" ]; then
  HELM_VERSION="$(read_section_value provisioning helmVersion)"
fi
HELM_VERSION="${HELM_VERSION:-3.22.0}"

# IMAGE_REPO="${IMAGE_REPO:-ghcr.io/ai-solution-eng/opencode-${OPENCODE_VERSION}-openchamber-${OPENCHAMBER_VERSION}}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/ai-solution-eng/opencode-openchamber}"
TAG="${TAG:-${OPENCODE_VERSION}-${OPENCHAMBER_VERSION}}-dind"

echo "==> values:          $VALUES"
echo "==> opencode.version:   $OPENCODE_VERSION"
echo "==> openchamber.version: $OPENCHAMBER_VERSION"
echo "==> ttyd version:    $TTYD_VERSION"
echo "==> helm version:    $HELM_VERSION"
echo "==> apt packages:    $APT_PACKAGES"
echo "==> image:           ${IMAGE_REPO}:${TAG} (${PLATFORM})"

# --- Proxy (environment only; never stored in the repo) ---------------------
# Mirrors the 'dockerbuild' wrapper baked into the user image so a build from
# this script behaves the same as a build from inside a pod. The VALUES ARE
# NEVER ECHOED: a proxy URL may carry credentials (http://user:pass@host) and
# this script's output routinely lands in CI logs and chat transcripts.
PROXY_HTTP="${HTTP_PROXY:-${http_proxy:-}}"
PROXY_HTTPS="${HTTPS_PROXY:-${https_proxy:-}}"
PROXY_BYPASS="${NO_PROXY:-${no_proxy:-}}"
# Remember which variables the CALLER actually set, so the status line does not
# claim NO_PROXY came "from environment" when the script merely synthesised it.
PROXY_FROM_ENV=""
[ -n "$PROXY_HTTP" ] && PROXY_FROM_ENV="${PROXY_FROM_ENV}HTTP_PROXY "
[ -n "$PROXY_HTTPS" ] && PROXY_FROM_ENV="${PROXY_FROM_ENV}HTTPS_PROXY "
[ -n "$PROXY_BYPASS" ] && PROXY_FROM_ENV="${PROXY_FROM_ENV}NO_PROXY "

PROXY_ARGS=()
if [ -n "$PROXY_HTTP" ] || [ -n "$PROXY_HTTPS" ]; then
  # buildx dials the daemon itself; that dial must never go through the proxy.
  for _extra in 127.0.0.1 ::1 localhost; do
    case ",$PROXY_BYPASS," in
      *",$_extra,"*) ;;
      *) PROXY_BYPASS="${PROXY_BYPASS:+$PROXY_BYPASS,}$_extra" ;;
    esac
  done
  if [ -n "$PROXY_HTTP" ]; then
    PROXY_ARGS+=(--build-arg "HTTP_PROXY=$PROXY_HTTP" --build-arg "http_proxy=$PROXY_HTTP")
  fi
  if [ -n "$PROXY_HTTPS" ]; then
    PROXY_ARGS+=(--build-arg "HTTPS_PROXY=$PROXY_HTTPS" --build-arg "https_proxy=$PROXY_HTTPS")
  fi
  PROXY_ARGS+=(--build-arg "NO_PROXY=$PROXY_BYPASS" --build-arg "no_proxy=$PROXY_BYPASS")
  if [ "$NETWORK_HOST" = 1 ]; then
    PROXY_ARGS+=(--network host)
  fi
  # Report only WHICH variables the caller set — never their values.
  echo "==> proxy:           enabled from environment (${PROXY_FROM_ENV% }) — values not printed"
  if [ -z "${NO_PROXY:-${no_proxy:-}}" ]; then
    echo "                     NO_PROXY was unset; forwarding only the loopback bypass list"
  fi
  if [ "$NETWORK_HOST" = 1 ]; then
    echo "                     --network host added (DinD: bridge cannot reach the proxy)"
  else
    echo "                     --network host NOT added (--no-network-host)"
  fi
else
  echo "==> proxy:           none (HTTP_PROXY/HTTPS_PROXY unset) — direct build"
fi
echo

BUILD_ARGS=(
  --platform "$PLATFORM"
  --build-arg "OPENCODE_VERSION=${OPENCODE_VERSION}"
  --build-arg "OPENCHAMBER_VERSION=${OPENCHAMBER_VERSION}"
  --build-arg "TTYD_VERSION=${TTYD_VERSION}"
  --build-arg "HELM_VERSION=${HELM_VERSION}"
  --build-arg "APT_PACKAGES=${APT_PACKAGES}"
  -t "${IMAGE_REPO}:${TAG}"
)
[ "${#PROXY_ARGS[@]}" -gt 0 ] && BUILD_ARGS+=("${PROXY_ARGS[@]}")
[ "$PUSH" = 1 ] && BUILD_ARGS+=(--push)
[ "$LOAD" = 1 ] && BUILD_ARGS+=(--load)

"$DOCKER" buildx build "${BUILD_ARGS[@]}" docker/user

if [ "$PUSH" != 1 ]; then
  echo
  echo "==> Built ${IMAGE_REPO}:${TAG} locally. Push with:"
  # Point at the script rather than printing a raw docker command: the raw form
  # would silently omit the proxy build-args above, and a proxy URL with
  # credentials must not be echoed into logs.
  echo "    scripts/build-user-image.sh --values ${VALUES} --push"
  echo "    (same versions/apt list; re-runs the build and pushes it)"
fi
