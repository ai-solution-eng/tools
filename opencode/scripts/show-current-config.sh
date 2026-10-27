#!/usr/bin/env bash
# Print the CURRENT config as rendered from values.yaml (the canonical copy in
# the release's project-config ConfigMap) — the thing to hand to the team, or
# to diff against what a user actually has.
#
# Read-only: touches no pod, changes nothing.
#
#   scripts/show-current-config.sh                 # print it
#   scripts/show-current-config.sh --out FILE      # write it to FILE
#   scripts/show-current-config.sh --namespace NS  # non-default namespace
if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${NAMESPACE:-opencode-web-helm}"
CONFIGMAP="${CONFIGMAP_NAME:-opencode-web-helm-project-config}"
KEY="${CONFIG_KEY:-opencode.json}"
OUT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --out)
      shift
      OUT="${1:-}"
      if [ -z "$OUT" ]; then echo "error: --out requires a path" >&2; exit 1; fi
      shift
      ;;
    --namespace|-n)
      shift
      NAMESPACE="${1:-}"
      if [ -z "$NAMESPACE" ]; then echo "error: --namespace requires a value" >&2; exit 1; fi
      shift
      ;;
    --help|-h)
      sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

# The ConfigMap data key contains dots (e.g. cordis.patch.yml); escape them
# for the jsonpath expression.
JSONPATH_KEY="$(printf '%s' "$KEY" | awk '{gsub(/\./,"\\."); print}')"

if ! BODY="$("$KUBECTL" get configmap -n "$NAMESPACE" "$CONFIGMAP" \
      -o jsonpath="{.data.$JSONPATH_KEY}" 2>&1)"; then
  echo "error: kubectl failed reading ConfigMap '$CONFIGMAP' in '$NAMESPACE': $BODY" >&2
  echo "       (has the chart been upgraded? see templates/configmap-project.yaml)" >&2
  exit 1
fi
if [ -z "$BODY" ]; then
  echo "error: ConfigMap '$CONFIGMAP' has no '$KEY' key (or it is empty)" >&2
  exit 1
fi

if [ -n "$OUT" ]; then
  printf '%s\n' "$BODY" > "$OUT"
  echo "wrote $NAMESPACE/$CONFIGMAP:$KEY -> $OUT" >&2
else
  printf '%s\n' "$BODY"
fi
