#!/usr/bin/env bash
# audit-user-dsh-state.sh - READ-ONLY pre-upgrade inventory of every dsh unit.
#
# For each unit: what dsh is ACTUALLY running (not what the annotation claims),
# whether the state PVC's npm tree predates the baked image (i.e. which units
# 0.4.38 will migrate), and which extra global npm CLIs live in that prefix
# (they leave PATH once the prefix becomes the baked alias).
#
#   scripts/audit-user-dsh-state.sh                    # all namespaces
#   scripts/audit-user-dsh-state.sh -n dsh-web-helm    # one namespace
#   scripts/audit-user-dsh-state.sh --save ./pre-upgrade   # also copy the setup
#
# --save copies, per unit, the files that make up a user's setup:
#   cordis.patch.yml, dsh.profile, profiles/web/package.json, skills.txt,
#   env.keys (KEY NAMES ONLY - values are never read into the dump).
# Nothing is written on the PVCs; every command is a read.
set -uo pipefail
KUBECTL="${KUBECTL:-kubectl}"
NS=""; SAVE=""
while [ $# -gt 0 ]; do case "$1" in
  -n|--namespace) NS="${2:-}"; shift 2 ;;
  --save) SAVE="${2:-}"; shift 2 ;;
  -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac; done
[ -z "$SAVE" ] || mkdir -p "$SAVE" || exit 2

namespaces() {
  if [ -n "$NS" ]; then printf '%s\n' "$NS"; else
    "$KUBECTL" get deploy -A -l dsh-user-managed=true \
      -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' | sort -u
  fi
}

printf '%-26s %-32s %-14s %-26s %-12s %s\n' NAMESPACE UNIT RUNNING TREE MARKER EXTRA
for ns in $(namespaces); do
  for unit in $("$KUBECTL" -n "$ns" get deploy -l dsh-user-managed=true -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'); do
    out="$("$KUBECTL" -n "$ns" exec "deploy/$unit" -c dsh-web -- sh -c '
      v=$(/usr/local/bin/dsh --version 2>/dev/null || echo "?")
      if [ -L /var/dsh/data/npm ]; then t="symlink:$(readlink /var/dsh/data/npm)"; else t="REAL-DIR"; fi
      m=$(cat /var/dsh/.dsh-version 2>/dev/null || echo "-")
      x=$(ls /var/dsh/data/npm/bin 2>/dev/null | grep -v "^dsh$" | tr "\n" ",")
      printf "%s|%s|%s|%s" "$v" "$t" "$m" "$x"
    ' 2>/dev/null)" || out="(no dsh-web container)|-|-|-"
    IFS='|' read -r run tree marker extra <<< "$out"
    printf '%-26s %-32s %-14s %-26s %-12s %s\n' "$ns" "$unit" "$run" "$tree" "$marker" "${extra:--}"
    if [ -n "$SAVE" ]; then
      d="$SAVE/${ns}__${unit}"; mkdir -p "$d"
      "$KUBECTL" -n "$ns" exec "deploy/$unit" -c dsh-web -- sh -c 'cat /var/dsh/home/.dsh/profiles/web/cordis.patch.yml 2>/dev/null' > "$d/cordis.patch.yml" 2>/dev/null
      "$KUBECTL" -n "$ns" exec "deploy/$unit" -c dsh-web -- sh -c 'cat /var/dsh/home/.dsh/profiles/web/dsh.profile 2>/dev/null' > "$d/dsh.profile" 2>/dev/null
      "$KUBECTL" -n "$ns" exec "deploy/$unit" -c dsh-web -- sh -c 'cat /var/dsh/home/profiles/web/package.json 2>/dev/null' > "$d/package.json" 2>/dev/null
      "$KUBECTL" -n "$ns" exec "deploy/$unit" -c dsh-web -- sh -c 'ls /var/dsh/home/.dsh/skills 2>/dev/null' > "$d/skills.txt" 2>/dev/null
      "$KUBECTL" -n "$ns" exec "deploy/$unit" -c dsh-web -- sh -c 'grep -o "^[A-Za-z_][A-Za-z0-9_]*=" /var/dsh/home/.dsh/.env 2>/dev/null | tr -d "="' > "$d/env.keys" 2>/dev/null
    fi
  done
done
echo
echo "REAL-DIR  state PVC predates the baked image; 0.4.38 migrates it (old tree kept as npm.pre-*)."
echo "EXTRA     global npm CLIs in the prefix; they leave PATH once the prefix is the baked alias."
[ -z "$SAVE" ] || echo "saved     per-unit setup copies under: $SAVE"