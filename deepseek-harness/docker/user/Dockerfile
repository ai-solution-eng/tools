# syntax=docker/dockerfile:1
# Baked DeepSeek Harness user image.
#
# Everything that used to be installed at user-pod-creation time — the apt
# toolchain, the @deepseek-ai/dsh npm package, uv, ttyd and helm — is baked
# into this image so a fresh user pod reaches Ready in seconds instead of
# minutes. The docker CLI + buildx plugin are baked too so the terminal can
# drive the chart's optional Docker-in-Docker sidecar (`docker:` values; see
# the dockerbuild wrapper below).
#
# Build (canonical invocation, also wrapped by scripts/build-user-image.sh
# which defaults the tag to dsh.version from values.yaml):
#   docker buildx build --platform linux/amd64 \
#     -t ghcr.io/ai-solution-eng/deepseek-harness:0.1.7-rc.2 --push docker/user
#
# IMPORTANT — the chart's runtime install steps remain in place as GUARDED
# FALLBACKS: on this image they no-op (everything they look for is present),
# and on the vanilla node:22-bookworm-slim image they still work. Two rules
# keep the two layers consistent:
#   1. Baked artifacts live under /opt/dsh — NEVER under /var/dsh. The state
#      PVC mounts at /var/dsh and would shadow any image content there. The
#      router's init container points /var/dsh/bin and /var/dsh/data/npm at
#      the baked copies with guarded symlinks on the PVC.
#   2. APT_PACKAGES / DSH_VERSION / TTYD_VERSION here must mirror
#      values.yaml (provisioning.aptPackages, dsh.version) and the ttyd
#      version hardcoded in templates/configmap-router.yaml. The build
#      script reads values.yaml so there is a single source of truth.
#      HELM_VERSION is intentionally NOT mirrored anywhere: helm has no
#      runtime-install fallback (offline packaging tool only), so its pin
#      lives here alone.
FROM node:22-bookworm-slim

ARG DSH_VERSION=0.1.7-rc.2
ARG TTYD_VERSION=1.7.7
# helm: OFFLINE ONLY (package/template/lint) — user pods carry no
# kubeconfig/ServiceAccount, so helm upgrade stays on the deploy host.
# Pinned to the helm 3 line: v4 went stable 2026-09-09 and its packaging
# changes should not precede the deploy pipeline's own v3→v4 upgrade.
ARG HELM_VERSION=3.22.0
ARG APT_PACKAGES="bash ca-certificates curl dnsutils fd-find gh git jq less make nano ncurses-term openssh-client python3 python3-pip python3-venv procps ripgrep tini tmux tree unzip util-linux vim-tiny zip zstd xz-utils lsof"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

LABEL org.opencontainers.image.title="deepseek-harness user unit" \
      org.opencontainers.image.description="Baked user-pod image for the dsh-web-helm chart: apt toolchain + @deepseek-ai/dsh + uv + ttyd + helm + docker CLI/buildx preinstalled" \
      org.opencontainers.image.base.name="node:22-bookworm-slim"

# --- apt toolchain + the /usr/local/bin aliases dsh-startup.sh recreates ---
# Mirrors the dsh-startup.sh toolchain block: same package list, same
# EXTERNALLY-MANAGED removal, same symlinks. With these present, the
# startup's `command -v` guard short-circuits and no apt runs at boot.
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends ${APT_PACKAGES} \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /usr/lib/python*/EXTERNALLY-MANAGED \
 && ln -sf /usr/bin/vi /usr/local/bin/vim \
 && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
 && ln -sf /usr/bin/python3 /usr/local/bin/python \
 && ln -sf /usr/bin/pip3 /usr/local/bin/pip \
 && printf '#!/bin/sh\nexit 0\n' > /usr/local/bin/xdg-open \
 && chmod 0755 /usr/local/bin/xdg-open

# --- @deepseek-ai/dsh under /opt/dsh/npm (image side, version-marked) ---
RUN mkdir -p /opt/dsh/bin /opt/dsh/npm \
 && npm install -g --silent "@deepseek-ai/dsh@${DSH_VERSION}" --prefix /opt/dsh/npm \
 && echo "${DSH_VERSION}" > /opt/dsh/npm/.dsh-version \
 && npm cache clean --force \
 && ln -sf /opt/dsh/npm/bin/dsh /usr/local/bin/dsh

# --- dsh-web-helm multi-user patch: settings mirror --------------------------
# dsh web assumes a laptop deployment: the browser-side settings mirror only
# syncs with the host for "loopback" pages — its persistence is chosen as
#   persistence = connection.isLoopback ? "host" : "memory"
# where (dsh-client-connection/lib/client.js)
#   isLoopback = transport?.ownsHost === true || pageLocation === void 0
#              || isLoopbackHostname(pageLocation.hostname)
# and ownsHost is only ever true inside the desktop shell. On ANY multi-user
# deployment (public domain through the in-pod nginx + router) the mirror
# stays memory-only and every Settings surface fails with "settings are
# unavailable in this browser" (observed live on the Models page, 0.1.6-alpha.2).
# In THIS chart the UI origin is already session-gated three layers deep
# (router session cookie, in-pod validator, dsh launch cookie), so the origin
# IS the trusted origin: force the connection's isLoopback true. GUARDED —
# the build fails if upstream changes the expression, so the patch can never
# silently stop applying.
# 2026-09-25 (0.1.7-rc.2 prep): resolve the package path dynamically. The
# expression is UNCHANGED in 0.1.7-rc.2 (verified against unpkg), but the
# 0.1.7 line pins every @deepseek-ai dep to one exact version, so npm may
# hoist dsh-client-connection to the tree root instead of nesting it under
# @deepseek-ai/dsh/node_modules (the 0.1.6-alpha.2 layout the old hardcoded
# path assumed). Guards unchanged: a missing file or a changed upstream
# expression still fails the build loudly.
RUN PKG="$(find /opt/dsh/npm/lib/node_modules -path '*@deepseek-ai/dsh-client-connection/lib/client.js' -print -quit)" \
 && test -n "$PKG" \
 && grep -Fq "isLoopback: transport?.ownsHost === true || pageLocation === void 0 || isLoopbackHostname(pageLocation.hostname)" "$PKG" \
 && sed -i "s#isLoopback: transport?.ownsHost === true || pageLocation === void 0 || isLoopbackHostname(pageLocation.hostname)#isLoopback: true /* dsh-web-helm: session-gated multi-user UI origin */#" "$PKG" \
 && grep -Fq "isLoopback: true /* dsh-web-helm: session-gated multi-user UI origin */" "$PKG"

# --- uv + ttyd under /opt/dsh/bin ---
# Same ttyd asset selection as the router's init container (x86_64/aarch64;
# the g2 fleet is amd64). ttyd version mirrors templates/configmap-router.yaml.
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/opt/dsh/bin sh \
 && arch="$(uname -m)" \
 && case "$arch" in \
      x86_64|amd64) ttyd_asset=ttyd.x86_64 ;; \
      aarch64|arm64) ttyd_asset=ttyd.aarch64 ;; \
      *) echo "unsupported architecture for ttyd: $arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL --retry 3 -o /opt/dsh/bin/ttyd \
      "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/${ttyd_asset}" \
 && chmod 0755 /opt/dsh/bin/ttyd \
 && ln -sf /opt/dsh/bin/uv /usr/local/bin/uv \
 && ln -sf /opt/dsh/bin/uvx /usr/local/bin/uvx

# --- helm under /opt/dsh/bin (chart packaging from the pod terminal) ---------
# OFFLINE scope only: `helm package` / `helm template` / `helm lint`. User
# pods carry no kubeconfig/ServiceAccount (only the router and the cleanup
# hook do), so there is nothing for `helm upgrade` to authenticate with —
# deploys stay on the deploy host. Pinned to the helm 3 line (see the
# HELM_VERSION ARG comment). Same guarded asset selection as ttyd above
# (the g2 fleet is amd64).
RUN arch="$(uname -m)" \
 && case "$arch" in \
      x86_64|amd64) helm_asset=linux-amd64 ;; \
      aarch64|arm64) helm_asset=linux-arm64 ;; \
      *) echo "unsupported architecture for helm: $arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL --retry 3 -o /tmp/helm.tgz \
      "https://get.helm.sh/helm-v${HELM_VERSION}-${helm_asset}.tar.gz" \
 && tar -xzf /tmp/helm.tgz -C /tmp \
 && install -m 0755 "/tmp/${helm_asset}/helm" /opt/dsh/bin/helm \
 && rm -rf /tmp/helm.tgz "/tmp/${helm_asset}" \
 && ln -sf /opt/dsh/bin/helm /usr/local/bin/helm

# --- docker CLI + buildx plugin under /opt/dsh/bin (DinD client) -------------
# The chart can add a docker-in-docker SIDECAR to user pods (values `docker:`);
# this client is the terminal half: it talks to the sidecar over
# DOCKER_HOST=tcp://127.0.0.1:2375 (set by the chart). Static binaries from
# download.docker.com — there is NO Debian `docker-cli` package ("Unable to
# locate package docker-cli"; the distro `docker.io` would drag the daemon
# in). Versions pinned to the clearwing-proven pair (client 27.5.1 against
# the docker:28-dind daemon). Plugin path: the CLI scans
# /usr/local/libexec/docker/cli-plugins (system path, not $HOME — the state
# PVC would shadow it); the artifact itself lives under /opt/dsh per the
# baked-artifacts rule.
ARG DOCKER_VERSION=27.5.1
ARG BUILDX_VERSION=0.37.1
RUN arch="$(uname -m)" \
 && case "$arch" in \
      x86_64|amd64) docker_arch=x86_64 ;; \
      aarch64|arm64) docker_arch=aarch64 ;; \
      *) echo "unsupported architecture for docker: $arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL --retry 3 \
      "https://download.docker.com/linux/static/stable/${docker_arch}/docker-${DOCKER_VERSION}.tgz" \
      | tar -xz -C /tmp docker/docker \
 && install -m 0755 /tmp/docker/docker /opt/dsh/bin/docker \
 && rm -rf /tmp/docker \
 && ln -sf /opt/dsh/bin/docker /usr/local/bin/docker \
 && mkdir -p /usr/local/libexec/docker/cli-plugins \
 && case "$docker_arch" in \
      x86_64) buildx_asset=buildx-v${BUILDX_VERSION}.linux-amd64 ;; \
      aarch64) buildx_asset=buildx-v${BUILDX_VERSION}.linux-arm64 ;; \
    esac \
 && curl -fsSL --retry 3 \
      "https://github.com/docker/buildx/releases/download/v${BUILDX_VERSION}/${buildx_asset}" \
      -o /opt/dsh/bin/docker-buildx \
 && chmod 0755 /opt/dsh/bin/docker-buildx \
 && ln -sf /opt/dsh/bin/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx \
 && /opt/dsh/bin/docker --version \
 && /opt/dsh/bin/docker buildx version

# Belt-and-braces: route plain `docker build` in the terminal through
# BuildKit too (CLI 27 defaults to it; plugin discovery uses the system
# /usr/local/libexec/docker/cli-plugins path, independent of HOME).
ENV DOCKER_BUILDKIT=1

# --- dockerbuild: DinD-aware buildx wrapper ----------------------------------
# On proxied clusters two adjustments are REQUIRED for `docker build` inside
# DinD (proven live on the g2 cluster via clearwing + container-builder-mcp):
#   1. --network host — the daemon's default bridge network cannot reach the
#      corporate proxy; sharing the pod network makes proxy-dependent RUN
#      steps (apt/pip/curl) work.
#   2. explicit proxy build-args — Docker's implicit forwarding of the
#      HTTP_PROXY family does NOT reach the RUN steps' environment inside
#      DinD (observed: pip Errno 101 / TLS failures; daemon-level pulls kept
#      working because the daemon env carries the proxy).
# NO_PROXY additionally gains 127.0.0.1,::1,localhost so buildx's own gRPC
# dial to the daemon is never proxied. Without a proxy configured the
# wrapper is a plain passthrough to `docker buildx build`.
COPY <<'EOF' /opt/dsh/bin/dockerbuild
#!/usr/bin/env bash
# DinD-aware `docker buildx build` wrapper (see the Dockerfile header for the
# why). Usage: dockerbuild [buildx args] <context-dir>
set -euo pipefail
args=()
http="${HTTP_PROXY:-${http_proxy:-}}"
https="${HTTPS_PROXY:-${https_proxy:-}}"
if [ -n "$http" ] || [ -n "$https" ]; then
  np="${NO_PROXY:-${no_proxy:-}}"
  for extra in 127.0.0.1 ::1 localhost; do
    case ",${np}," in
      *",${extra},"*) ;;                          # already listed
      *) np="${np:+${np},}${extra}" ;;
    esac
  done
  if [ -n "$http" ]; then
    args+=(--build-arg "HTTP_PROXY=${http}" --build-arg "http_proxy=${http}")
  fi
  if [ -n "$https" ]; then
    args+=(--build-arg "HTTPS_PROXY=${https}" --build-arg "https_proxy=${https}")
  fi
  args+=(--build-arg "NO_PROXY=${np}" --build-arg "no_proxy=${np}")
  args+=(--network host)
fi
exec docker buildx build "${args[@]}" "$@"
EOF
RUN chmod 0755 /opt/dsh/bin/dockerbuild \
 && ln -sf /opt/dsh/bin/dockerbuild /usr/local/bin/dockerbuild
