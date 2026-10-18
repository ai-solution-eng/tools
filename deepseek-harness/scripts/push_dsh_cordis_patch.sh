#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${NAMESPACE:-dsh-web-helm}"
CONTAINER="${CONTAINER:-dsh-web}"
TARGET="/var/dsh/home/.dsh/profiles/web/cordis.patch.yml"
# Pre-push backup: the CURRENT per-user patch is copied to $BACKUP (same
# directory) before anything overwrites it, so an administrator push can
# never destroy the user's existing patch layer. Rolling single backup:
# each push replaces the previous backup with the then-current file.
BACKUP="/var/dsh/home/.dsh/profiles/web/cordis.patch-backup.yml"
# cp -p preserves the original ownership/mode; the guard makes the backup a
# no-op for units that have no patch file yet. If the backup fails the
# &&-chain stops and the overwrite never runs (fail-closed). The in-pod
# supervisor watches this file and YAML-validates it before restarting dsh
# on change (DSH_WATCH_CONFIG_FILES), so a broken patch cannot crash-loop.
PUSH_CMD="mkdir -p /var/dsh/home/.dsh/profiles/web && if [ -f $TARGET ]; then cp -p $TARGET $BACKUP; fi && cat > $TARGET"

usage() {
  cat <<EOF
usage: push-dsh-cordis-patch.sh --file cordis.patch.yml [--apply] [--namespace NS]

Updates /var/dsh/home/.dsh/profiles/web/cordis.patch.yml (the dsh web
profile's patch layer: MCP integrations, session content search,
title-service caps) in every dsh-web user environment (all deployments
labeled dsh-user-managed=true, warm-pool units included). The in-pod
supervisor hot-reloads the change (YAML-validated first), so no pod
restart is needed. Before overwriting, the current file is backed up to
$BACKUP (a previous backup is replaced by the newest pre-push state).

  --file PATH          (required) push this local YAML patch file into every pod
  --apply              actually run
  (default)            dry-run preview only
  --namespace NS       target namespace (default: $NAMESPACE)
  --help               this help

env overrides: KUBECTL (default: kubectl), NAMESPACE, CONTAINER (default: $CONTAINER)
EOF
}

APPLY=0
FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --dry-run)
      APPLY=0
      shift
      ;;
    --file)
      shift
      FILE="${1:-}"
      if [ -z "$FILE" ]; then
        echo "error: --file requires a path" >&2
        exit 1
      fi
      shift
      ;;
    --namespace|-n)
      shift
      NAMESPACE="${1:-}"
      if [ -z "$NAMESPACE" ]; then
        echo "error: --namespace requires a value" >&2
        exit 1
      fi
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# The cordis patch has no in-pod defaults mount (the chart seeds it from the
# dshCordisPatch value at first boot only), so the file to push is mandatory.
if [ -z "$FILE" ]; then
  echo "error: --file is required (pass the cordis.patch.yml to push)" >&2
  usage
  exit 1
fi

if [ ! -f "$FILE" ]; then
  echo "error: file not found: $FILE" >&2
  exit 1
fi
if [ ! -s "$FILE" ]; then
  echo "error: file is empty: $FILE" >&2
  exit 1
fi
if command -v python3 >/dev/null 2>&1; then
  if ! python3 -c "import sys; import yaml; yaml.safe_load(open(sys.argv[1]))" "$FILE" >/dev/null 2>&1; then
    echo "warning: $FILE is not valid YAML (or PyYAML is unavailable); pushing anyway — the in-pod supervisor validates and would reject a broken patch" >&2
  fi
fi

if ! DEPLOYMENTS="$("$KUBECTL" get deploy -n "$NAMESPACE" -l dsh-user-managed=true -o jsonpath='{.items[*].metadata.name}' 2>&1)"; then
  echo "error: kubectl failed: $DEPLOYMENTS" >&2
  exit 1
fi

if [ -z "$DEPLOYMENTS" ]; then
  echo "no user deployments found in namespace '$NAMESPACE' (label selector: dsh-user-managed=true)"
  exit 0
fi

COUNT=$(printf '%s\n' $DEPLOYMENTS | wc -l | tr -d ' ')

if [ "$APPLY" -eq 0 ]; then
  echo "DRY RUN - nothing executed"
  echo "mode: push local file '$FILE' -> $TARGET"
  echo "backup: current file -> $BACKUP (kept before each overwrite)"
  echo "namespace: $NAMESPACE | container: $CONTAINER | deployments: $COUNT"
  for name in $DEPLOYMENTS; do
    echo " - $name"
  done
  echo "re-run with --apply to execute"
  exit 0
fi

echo "updating $COUNT deployment(s) in namespace '$NAMESPACE'"
FAILED_COUNT=0
FAILED_LIST=""
i=0
for name in $DEPLOYMENTS; do
  i=$((i + 1))
  if "$KUBECTL" exec -i -n "$NAMESPACE" "deploy/$name" -c "$CONTAINER" -- sh -c "$PUSH_CMD" < "$FILE" >/dev/null 2>&1; then
    echo "[$i/$COUNT] $name: pushed (previous patch backed up)"
  else
    echo "[$i/$COUNT] $name: FAILED" >&2
    FAILED_COUNT=$((FAILED_COUNT + 1))
    FAILED_LIST="$FAILED_LIST $name"
  fi
done

if [ "$FAILED_COUNT" -gt 0 ]; then
  echo "done with $FAILED_COUNT failure(s):$FAILED_LIST" >&2
  exit 1
fi
echo "done: $COUNT deployment(s) updated (dsh hot-reloads the patch; users may need to refresh their browser)"
