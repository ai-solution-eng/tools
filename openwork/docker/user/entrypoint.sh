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

exec \${RUN_AS} openwork-server web \
  --host 127.0.0.1 \
  --port \${OPENWORK_PORT:-8787} \
  --workspace /workspace/personal \
  --config "$HOME/.config/openwork/server.json" \
  --approval "\${OPENWORK_APPROVAL_MODE:-auto}" \\
  $([ "\${OPENWORK_WEB_BOOTSTRAP_TOKEN:-1}" = "1" ] || echo --no-bootstrap-token)
