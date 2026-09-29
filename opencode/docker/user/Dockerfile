# Baked OpenCode user image.
#
# Everything that used to be installed at user-pod-creation time — the apt
# toolchain, the opencode-ai npm package, the @openchamber/web npm package,
# uv and ttyd — is baked into this image so a fresh user pod reaches Ready in
# seconds instead of minutes (and the apt stages stop re-running on every
# restart: apt state lives in the container layer, not on the PVCs).
# The Helm CLI is baked as well (a new addition, not a legacy pod-boot
# install) so charts can be packaged/linted from inside the pod.
#
# Build (canonical invocation, also wrapped by scripts/build-user-image.sh
# which reads the versions + apt list from the values file and defaults the
# repo name to ghcr.io/ai-solution-eng/opencode-<opencode>-openchamber-<openchamber>):
#   docker buildx build --platform linux/amd64 \
#     -t ghcr.io/ai-solution-eng/opencode-1.18.11-openchamber-1.17.2:0.0.1 --push docker/user
#
# IMPORTANT — the chart's runtime install steps remain in place as GUARDED
# FALLBACKS: on this image they no-op (everything they look for is present),
# and on the vanilla node:22-bookworm-slim image they still work. Two rules
# keep the two layers consistent:
#   1. Baked artifacts live under /opt/opencode — NEVER under /var/opencode.
#      The state PVC mounts at /var/opencode and would shadow any image
#      content there. The router's init container points /var/opencode/data/npm
#      and /var/opencode/bin/{uv,uvx,ttyd} at the baked copies with guarded
#      symlinks on the PVC.
#   2. APT_PACKAGES / OPENCODE_VERSION / OPENCHAMBER_VERSION / TTYD_VERSION /
#      HELM_VERSION here must mirror values.yaml (provisioning.aptPackages,
#      opencode.version, openchamber.version, provisioning.helmVersion) and
#      the ttyd version hardcoded in templates/configmap-router.yaml. The
#      build script reads values.yaml so there is a single source of truth.
FROM node:22-bookworm-slim

ARG OPENCODE_VERSION=1.18.32
ARG OPENCHAMBER_VERSION=2.0.2
ARG TTYD_VERSION=1.7.7
ARG HELM_VERSION=3.22.0
ARG APT_PACKAGES="bash ca-certificates curl dnsutils fd-find gh git jq less make nano ncurses-term openssh-client python3 python3-pip python3-venv procps ripgrep tini tmux tree unzip util-linux vim-tiny zip zstd xz-utils lsof"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

LABEL org.opencontainers.image.title="opencode user unit" \
     org.opencontainers.image.description="Baked user-pod image for the opencode-web-helm chart: apt toolchain + opencode-ai + @openchamber/web + uv + ttyd + helm preinstalled" \
     org.opencontainers.image.base.name="node:22-bookworm-slim"

# --- apt toolchain + the /usr/local/bin aliases opencode-startup.sh recreates ---
# Mirrors the opencode-startup.sh toolchain block: same package list, same
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

# --- opencode-ai + @openchamber/web under /opt/opencode/npm (image side) ---
# Both UI packages are baked unconditionally so one image serves
# ui.mode: opencode AND ui.mode: openchamber. Version markers let the
# router's init container verify the baked copy matches the requested
# versions before aliasing the state-PVC npm prefix to it.
RUN mkdir -p /opt/opencode/bin /opt/opencode/npm \
     && npm install -g --silent "opencode-ai@${OPENCODE_VERSION}" --prefix /opt/opencode/npm \
     && echo "${OPENCODE_VERSION}" > /opt/opencode/npm/.opencode-version \
     && npm install -g --silent "@openchamber/web@${OPENCHAMBER_VERSION}" --prefix /opt/opencode/npm \
     && echo "${OPENCHAMBER_VERSION}" > /opt/opencode/npm/.openchamber-version \
     && npm cache clean --force \
     && ln -sf /opt/opencode/npm/bin/opencode /usr/local/bin/opencode \
     && ln -sf /opt/opencode/npm/bin/openchamber /usr/local/bin/openchamber

# --- uv + ttyd under /opt/opencode/bin ---
# Same ttyd asset selection as the router's init container (x86_64/aarch64/
# armv7/arm; the fleet is amd64). ttyd version mirrors
# templates/configmap-router.yaml.
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/opt/opencode/bin sh \
     && arch="$(uname -m)" \
     && case "$arch" in \
     x86_64|amd64) ttyd_asset=ttyd.x86_64 ;; \
     aarch64|arm64) ttyd_asset=ttyd.aarch64 ;; \
     armv7l|armhf) ttyd_asset=ttyd.armhf ;; \
     arm*) ttyd_asset=ttyd.arm ;; \
     *) echo "unsupported architecture for ttyd: $arch" >&2; exit 1 ;; \
     esac \
     && curl -fsSL --retry 3 -o /opt/opencode/bin/ttyd \
     "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/${ttyd_asset}" \
     && chmod 0755 /opt/opencode/bin/ttyd \
     && ln -sf /opt/opencode/bin/uv /usr/local/bin/uv \
     && ln -sf /opt/opencode/bin/uvx /usr/local/bin/uvx

# --- helm under /opt/opencode/bin ---
# Official static binaries from get.helm.sh so charts can be packaged,
# linted and templated from inside the pod (helm package / lint / template).
# HELM_VERSION mirrors provisioning.helmVersion in values.yaml. The fleet is
# amd64; arm64 is covered for local arm builds.
RUN arch="$(uname -m)" \
     && case "$arch" in \
     x86_64|amd64) helm_asset=amd64 ;; \
     aarch64|arm64) helm_asset=arm64 ;; \
     *) echo "unsupported architecture for helm: $arch" >&2; exit 1 ;; \
     esac \
     && curl -fsSL --retry 3 -o /tmp/helm.tgz \
     "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${helm_asset}.tar.gz" \
     && tar -xzf /tmp/helm.tgz -C /tmp \
     && install -m 0755 "/tmp/linux-${helm_asset}/helm" /opt/opencode/bin/helm \
     && rm -rf /tmp/helm.tgz "/tmp/linux-${helm_asset}" \
     && ln -sf /opt/opencode/bin/helm /usr/local/bin/helm \
     && /opt/opencode/bin/helm version --short
