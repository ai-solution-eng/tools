# syntax=docker/dockerfile:1
# Baked OpenWork user image (openwork-web-helm chart).
#
# Ported from the opencode chart's user image (see REQUIREMENTS.md §3 and
# values.yaml.opencode-reference for provenance). Same philosophy: everything
# that used to be installed at user-pod-creation time is baked here so a fresh
# user pod reaches Ready in seconds. Baked artifacts live under /opt/openwork —
# NEVER under /var/openwork (the state PVC mounts there and would shadow them).
#
# What is baked:
#   - apt toolchain + uv + ttyd + helm + docker CLI/buildx (carried over from
#     the opencode image's toolchain block, same package list)
#   - openwork-server (npm, MIT apps/packages only — ZERO ee/ code, spec D4)
#     The published package bundles the built web UI; no source build needed.
#   - the pinned OpenCode engine binary at /usr/local/bin/opencode
#     (spec D5: OPENWORK_OPENCODE_BIN — the server manages the engine; baking
#     it avoids the first-boot GitHub download on egress-restricted PCAI units)
#
# OpenChamber is intentionally NOT installed (spec D6: removed entirely).
#
# Build (wrapped by scripts/build-user-image.sh which reads the versions from
# values.yaml — single source of truth):
#   docker buildx build --platform linux/amd64 \
#     -t ghcr.io/ai-solution-eng/openwork-server:<serverVersion>-<opencodeVersion>-dind \
#     --push docker/user
#
# IMPORTANT — version pins here MUST mirror values.yaml:
#   OPENWORK_SERVER_VERSION <-> openwork.serverVersion
#   OPENCODE_VERSION        <-> openwork.opencodeVersion
#   APT_PACKAGES            <-> provisioning.aptPackages
#   TTYD_VERSION            <-> the version hardcoded in templates/configmap-router.yaml

FROM node:22-bookworm-slim

# Proxy build-args (declared no-op by default; the build script sets them
# explicitly when the calling shell carries proxy env — see
# scripts/build-user-image.sh; the g2-proven pattern from the sibling chart).
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY=""
ARG http_proxy=""
ARG https_proxy=""
ARG no_proxy=""

ARG OPENWORK_SERVER_VERSION=0.18.59
ARG OPENCODE_VERSION=1.18.30
ARG TARGETARCH

# --- runtime deps (tini for signal handling; ripgrep for the engine) ---
RUN apt-get update -qq \
     && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
          bash ca-certificates curl dnsutils fd-find gh git jq less make nano ncurses-term \
          openssh-client python3 python3-pip python3-venv procps ripgrep tini tmux tree unzip \
          util-linux vim-tiny zip zstd xz-utils lsof \
     && rm -rf /var/lib/apt/lists/* \
     && rm -f /usr/lib/python*/EXTERNALLY-MANAGED \
     && ln -sf /usr/bin/vi /usr/local/bin/vim \
     && ln -sf /usr/bin/fdfind /usr/local/bin/fd \
     && ln -sf /usr/bin/python3 /usr/local/bin/python \
     && ln -sf /usr/bin/pip3 /usr/local/bin/pip

# --- openwork-server (MIT; bundles the web UI + OpenCode plugin deps) ---
RUN npm install -g --no-audit --no-fund "openwork-server@${OPENWORK_SERVER_VERSION}" \
     && chmod 0755 /usr/local/lib/node_modules/openwork-server/dist/bin/openwork-server \
     && echo "${OPENWORK_SERVER_VERSION}" > /opt/openwork-server-version \
     && openwork-server --version 2>/dev/null | head -1 || true

# --- OpenCode engine, baked via npm (sibling chart's proven path) ---
# Installed under /opt/openwork/npm like the opencode chart does (npm registry
# egress is proven through the corporate proxy; GitHub release downloads are
# not). The bin is symlinked to /usr/local/bin/opencode where
# OPENWORK_OPENCODE_BIN expects it. No first-boot download happens at runtime.
RUN npm install -g --no-audit --no-fund "opencode-ai@${OPENCODE_VERSION}" --prefix /opt/openwork/npm \
     && ln -sf /opt/openwork/npm/bin/opencode /usr/local/bin/opencode \
     && echo "${OPENCODE_VERSION}" > /opt/openwork-opencode-version \
     && opencode --version

# --- uv + ttyd + helm + docker CLI (carried over from the opencode image) ---
ARG TTYD_VERSION=1.7.7
ARG HELM_VERSION=3.22.0
RUN set -eux; \
    curl -fsSL https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/opt/openwork/bin sh -s -- \
     && ln -sf /opt/openwork/bin/uv /usr/local/bin/uv \
     && ln -sf /opt/openwork/bin/uvx /usr/local/bin/uvx; \
    mkdir -p /opt/openwork/bin; \
    curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
      -o /opt/openwork/bin/ttyd && chmod 0755 /opt/openwork/bin/ttyd \
      && ln -sf /opt/openwork/bin/ttyd /usr/local/bin/ttyd; \
    curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
      | tar -xz -C /tmp && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm && rm -rf /tmp/linux-amd64

# --- non-root default identity; state lands on the chart's PVCs ---
# node:22-bookworm-slim already ships a uid/gid 1000 pair ("node"); rename it
# to openwork rather than creating a conflicting one. HOME moves to
# /var/openwork/home (on the state PVC at runtime; image default here).
RUN usermod -l openwork -d /var/openwork/home -m node 2>/dev/null || true \
     && groupmod -n openwork node 2>/dev/null || true \
     && usermod -d /var/openwork/home openwork 2>/dev/null || true \
     && mkdir -p /var/openwork/home \
     && chown -R 1000:1000 /var/openwork/home

ENV HOME=/var/openwork/home \
    XDG_CONFIG_HOME=/var/openwork/home/.config \
    XDG_DATA_HOME=/var/openwork/home/.local/share \
    XDG_CACHE_HOME=/var/openwork/home/.cache \
    OPENWORK_DATA_DIR=/var/openwork/data/openwork-server \
    OPENWORK_OPENCODE_BIN=/usr/local/bin/opencode \
    OPENWORK_NO_UPDATE_CHECK=1 \
    OPENCODE_CONFIG_DIR=/var/openwork/home/.config/opencode \
    OPENCODE_DB=/var/openwork/data/opencode/opencode.db \
    NPM_CONFIG_AUDIT=false

RUN mkdir -p /var/openwork/home /var/openwork/data/opencode /workspace \
     && chown -R 1000:1000 /var/openwork /workspace

EXPOSE 8787
ENTRYPOINT ["/usr/bin/tini","--","/usr/local/bin/openwork-entrypoint.sh"]
COPY entrypoint.sh /usr/local/bin/openwork-entrypoint.sh
