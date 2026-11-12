#!/usr/bin/env bash
# OpenWork unit entrypoint (openwork-web-helm chart).
# Ported from the opencode chart's startup script semantics, incl. the tr211
# lesson: skip setpriv entirely when admission already forced the tenant uid.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH

# --- platform identity (setpriv under the tenant uid/gid when root) ---
RUN_AS=""
if [ -n "\${USER_INFO_UID:-}" ] && [ "\${USER_INFO_UID}" != "0" ] && [ "$(id -u)" = "0" ]; then
  getent group "$USER_INFO_GID" >/dev/null 2>&1 || groupadd -g "$USER_INFO_GID" openwork-tenant
  getent passwd "$USER_INFO_UID" >/dev/null 2>&1 || useradd -u "$USER_INFO_UID" -g "$USER_INFO_GID" -m -d "$HOME" openwork-tenant
  chown -R "$USER_INFO_UID:$USER_INFO_GID" /var/openwork /workspace 2>/dev/null || true
  RUN_AS="setpriv --reuid=$USER_INFO_UID --regid=$USER_INFO_GID --init-groups --"
elif [ -n "\${USER_INFO_UID:-}" ] && [ "$(id -u)" = "$USER_INFO_UID" ]; then
  : # admission already forced the tenant uid — skip setpriv
fi

# --- workspace/config seeding: merge, never clobber ---
mkdir -p "$HOME/.config/opencode" "$HOME/.config/openwork" "$OPENWORK_DATA_DIR" /workspace/personal /workspace/shared
[ -f "$HOME/.config/opencode/opencode.json" ] || cp /opt/openwork-defaults/opencode.json "$HOME/.config/opencode/opencode.json" 2>/dev/null || true
[ -f "$HOME/.config/openwork/server.json" ]  || cp /opt/openwork-defaults/openwork-server.json "$HOME/.config/openwork/server.json" 2>/dev/null || true
[ -d "$HOME/.config/opencode/skill" ] || mkdir -p "$HOME/.config/opencode/skill"
for d in /workspace/shared/_org/skills/*/ ; do
  [ -d "$d" ] || continue; n="$(basename "$d")"
  [ -e "$HOME/.config/opencode/skill/$n" ] || ln -s "$d" "$HOME/.config/opencode/skill/$n"
done

# --- workspace precedence: the seeded server.json is authoritative ---
# openwork-server's `web` command AUTO-PUSHES process.cwd() as a workspace when
# the --workspace flag list is empty AND OPENWORK_WORKSPACES is unset. That cwd
# fallback (/workspace, the PARENT of personal/ and shared/) would defeat the
# seeded config, so suppress it instead of passing --workspace: a whitespace-only
# OPENWORK_WORKSPACES makes parseList() return [] (no env-injected paths) while
# the variable stays DEFINED, which suppresses the cwd push and lets
# fileConfig.workspaces win — keeping the per-workspace NAMES (personal /
# shared / platform-*) that the path-only CLI/env routes cannot preserve.
# opencode_supervisor.mjs does exactly the same for chart-deployed units.
#
# Two guards, both mirroring the supervisor:
#  - never clobber an operator-supplied OPENWORK_WORKSPACES (docker run -e ...);
#  - only suppress when server.json actually DECLARES workspaces — on the bare
#    image (no seeded config, or an empty workspaces array) suppressing the cwd
#    push would leave the UI with ZERO workspaces instead of the cwd fallback.
if [ -z "${OPENWORK_WORKSPACES:-}" ] && [ -f "$HOME/.config/openwork/server.json" ]; then
  # node is guaranteed in this image; a missing/broken runtime just means "no
  # declared workspaces", never an aborted entrypoint (set -e safe: it is an
  # `if` condition, not a bare command).
  if node -e 'const fs = require("fs"); let ok = false; try { const w = JSON.parse(fs.readFileSync(process.argv[1], "utf8")).workspaces; ok = Array.isArray(w) && w.length > 0 } catch {} process.exit(ok ? 0 : 1)' "$HOME/.config/openwork/server.json" 2>/dev/null; then
    export OPENWORK_WORKSPACES=' '
  fi
fi

exec \${RUN_AS} openwork-server web \
  --host 127.0.0.1 \
  --port \${OPENWORK_PORT:-8787} \
  --config "$HOME/.config/openwork/server.json" \
  --approval "\${OPENWORK_APPROVAL_MODE:-auto}" \
  $([ "\${OPENWORK_WEB_BOOTSTRAP_TOKEN:-1}" = "1" ] || echo --no-bootstrap-token)
