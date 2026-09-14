#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${NAMESPACE:-opencode-web-helm}"
CONTAINER="${CONTAINER:-opencode-web}"
TARGET="/var/opencode/home/.config/opencode/opencode.json"
DEFAULTS="/opt/opencode-defaults/opencode.json"
PULL_CMD="mkdir -p /var/opencode/home/.config/opencode && cp $DEFAULTS $TARGET"
PUSH_CMD="mkdir -p /var/opencode/home/.config/opencode && cat > $TARGET"

usage() {
  cat <<EOF
usage: push-config.sh [--apply] [--file config.json] [--namespace NS]

Updates ~/.config/opencode/opencode.json in every opencode-web user environment
(all deployments labeled opencode-user-managed=true, warm-pool units included).
The in-pod supervisor hot-reloads the change, so no pod restart is needed.

  (default)            dry-run preview only
  --apply              actually run
  --file PATH          push this local JSON file into every pod
                       (default: copy the ConfigMap-mounted $DEFAULTS,
                       i.e. run a helm upgrade of opencodeConfig first)
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

if [ -n "$FILE" ]; then
  if [ ! -f "$FILE" ]; then
    echo "error: file not found: $FILE" >&2
    exit 1
  fi
  if [ ! -s "$FILE" ]; then
    echo "error: file is empty: $FILE" >&2
    exit 1
  fi
  if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$FILE" >/dev/null 2>&1; then
      echo "warning: $FILE is not valid JSON, pushing anyway" >&2
    fi
  fi
fi

if ! DEPLOYMENTS="$("$KUBECTL" get deploy -n "$NAMESPACE" -l opencode-user-managed=true -o jsonpath='{.items[*].metadata.name}' 2>&1)"; then
  echo "error: kubectl failed: $DEPLOYMENTS" >&2
  exit 1
fi

if [ -z "$DEPLOYMENTS" ]; then
  echo "no user deployments found in namespace '$NAMESPACE' (label selector: opencode-user-managed=true)"
  exit 0
fi

COUNT=$(printf '%s\n' $DEPLOYMENTS | wc -l | tr -d ' ')

if [ "$APPLY" -eq 0 ]; then
  echo "DRY RUN - nothing executed"
  if [ -n "$FILE" ]; then
    echo "mode: push local file '$FILE' -> $TARGET"
  else
    echo "mode: copy in-pod defaults ($DEFAULTS -> $TARGET)"
    echo "note: run a helm upgrade of opencodeConfig first so the mounted ConfigMap is current"
  fi
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
  if [ -n "$FILE" ]; then
    if "$KUBECTL" exec -i -n "$NAMESPACE" "deploy/$name" -c "$CONTAINER" -- sh -c "$PUSH_CMD" < "$FILE" >/dev/null 2>&1; then
      echo "[$i/$COUNT] $name: pushed"
    else
      echo "[$i/$COUNT] $name: FAILED" >&2
      FAILED_COUNT=$((FAILED_COUNT + 1))
      FAILED_LIST="$FAILED_LIST $name"
    fi
  else
    if "$KUBECTL" exec -n "$NAMESPACE" "deploy/$name" -c "$CONTAINER" -- sh -c "$PULL_CMD" >/dev/null 2>&1; then
      echo "[$i/$COUNT] $name: updated"
    else
      echo "[$i/$COUNT] $name: FAILED" >&2
      FAILED_COUNT=$((FAILED_COUNT + 1))
      FAILED_LIST="$FAILED_LIST $name"
    fi
  fi
done

if [ "$FAILED_COUNT" -gt 0 ]; then
  echo "done with $FAILED_COUNT failure(s):$FAILED_LIST" >&2
  exit 1
fi
echo "done: $COUNT deployment(s) updated (opencode hot-reloads the config; users may need to refresh their browser)"
