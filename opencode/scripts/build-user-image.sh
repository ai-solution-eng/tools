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

usage() {
  sed -n '2,27p' "$0"
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
TAG="${TAG:-${OPENCODE_VERSION}-${OPENCHAMBER_VERSION}}"

echo "==> values:          $VALUES"
echo "==> opencode.version:   $OPENCODE_VERSION"
echo "==> openchamber.version: $OPENCHAMBER_VERSION"
echo "==> ttyd version:    $TTYD_VERSION"
echo "==> helm version:    $HELM_VERSION"
echo "==> apt packages:    $APT_PACKAGES"
echo "==> image:           ${IMAGE_REPO}:${TAG} (${PLATFORM})"
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
[ "$PUSH" = 1 ] && BUILD_ARGS+=(--push)
[ "$LOAD" = 1 ] && BUILD_ARGS+=(--load)

"$DOCKER" buildx build "${BUILD_ARGS[@]}" docker/user

if [ "$PUSH" != 1 ]; then
  echo
  echo "==> Built ${IMAGE_REPO}:${TAG} locally. Push with:"
  echo "    docker buildx build --platform ${PLATFORM} -t ${IMAGE_REPO}:${TAG} --push docker/user"
  echo "    (or rerun this script with --push)"
fi
