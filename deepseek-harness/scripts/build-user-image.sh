#!/usr/bin/env bash
# Build (and optionally push) the baked DeepSeek Harness user image.
#
# The image bakes the apt toolchain, @deepseek-ai/dsh, uv and ttyd so user
# pods stop installing everything at boot. See docker/user/Dockerfile for
# the design contract (artifacts under /opt/dsh, guarded fallbacks).
#
# Usage:
#   scripts/build-user-image.sh                       # build for linux/amd64,
#                                                     # tag = dsh.version from values
#   scripts/build-user-image.sh --tag 0.1.6-alpha.2 --push   # build + push
#   scripts/build-user-image.sh --values values-g2.yaml
#
# Sources of truth (kept in sync automatically):
#   - apt package list:  provisioning.aptPackages in the selected values file
#   - dsh npm version:   dsh.version in the selected values file — ALSO the
#                        default image tag (tag == baked dsh version, so a
#                        dsh.version bump and an image rebuild stay coupled)
#   - ttyd version:      --ttyd-version flag (default 1.7.7; must mirror the
#                        ttydVersion const in templates/configmap-router.yaml)
set -euo pipefail

cd "$(dirname "$0")/.."

VALUES="values.yaml"
TAG=""
TTYD_VERSION="1.7.7"
PLATFORM="linux/amd64"
IMAGE_REPO="ghcr.io/ai-solution-eng/deepseek-harness"
PUSH=0
LOAD=0

usage() {
  sed -n '2,20p' "$0"
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --values) VALUES="$2"; shift 2 ;;
    --ttyd-version) TTYD_VERSION="$2"; shift 2 ;;
    --platform) PLATFORM="$2"; shift 2 ;;
    --repo) IMAGE_REPO="$2"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --load) LOAD=1; shift ;;
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

# provisioning.aptPackages — the single source of truth for the apt list
# (quoted single-line string in values.yaml).
APT_PACKAGES="$(grep -E '^  aptPackages:' "$VALUES" | head -1 | sed -e 's/^  aptPackages:[[:space:]]*//' -e 's/^"//' -e 's/"[[:space:]]*$//')"
if [ -z "$APT_PACKAGES" ]; then
  echo "ERROR: could not parse provisioning.aptPackages from $VALUES" >&2
  exit 1
fi

# dsh.version — npm package version baked into the image, and the default
# image tag (tag == baked dsh version keeps rebuilds and version bumps coupled).
DSH_VERSION="$(grep -E '^  version:' "$VALUES" | head -1 | sed -e 's/^  version:[[:space:]]*//' -e 's/^"//' -e 's/"[[:space:]]*$//')"
if [ -z "$DSH_VERSION" ]; then
  echo "ERROR: could not parse dsh.version from $VALUES" >&2
  exit 1
fi
TAG="${TAG:-$DSH_VERSION}"

echo "==> values:        $VALUES"
echo "==> dsh.version:   $DSH_VERSION"
echo "==> ttyd version:  $TTYD_VERSION"
echo "==> apt packages:  $APT_PACKAGES"
echo "==> image:         ${IMAGE_REPO}:${TAG} (${PLATFORM})"
echo

BUILD_ARGS=(
  --platform "$PLATFORM"
  --build-arg "DSH_VERSION=${DSH_VERSION}"
  --build-arg "TTYD_VERSION=${TTYD_VERSION}"
  --build-arg "APT_PACKAGES=${APT_PACKAGES}"
  -t "${IMAGE_REPO}:${TAG}"
)
[ "$PUSH" = 1 ] && BUILD_ARGS+=(--push)
[ "$LOAD" = 1 ] && BUILD_ARGS+=(--load)

"$DOCKER" buildx build "${BUILD_ARGS[@]}" docker/user

if [ "$PUSH" != 1 ]; then
  echo
  echo "==> Built ${IMAGE_REPO}:${TAG} locally. Push with:"
  echo "    docker buildx build --platform ${PLATFORM} -t ${IMAGE_REPO}:${TAG} --push docker/user"
  echo "    (or rerun this script with --push)"
fi
