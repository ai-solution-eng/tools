#!/usr/bin/env bash
# Run recover-legacy-shared.sh over EVERY SSO user that still holds the legacy
# shared claim — the fleet-wide form of the single-user recovery.
#
# usage:
#   scripts/recover-legacy-shared-all.sh                 # dry run: list targets
#   scripts/recover-legacy-shared-all.sh --apply         # run them one by one
#
# options:
#   --chart openwork|opencode|deepseek-harness   (default openwork)
#   --namespace-prefix P    default project-user-
#
# Deliberately a driver, not a second copy of the Job: recover-legacy-shared.sh
# owns the manifest, the verification and the idempotency. The RELEASE namespace
# is always excluded — there the legacy claim IS the platform anchor and copying
# it into a user's personal folder would be exactly wrong.
set -uo pipefail

APPLY=0
CHART="opencode"
NS_PREFIX="project-user-"
SELF_DIR=$(cd "$(dirname "$0")" && pwd)
KUBECTL=${KUBECTL:-kubectl}

usage() { sed -n '2,17p' "$0"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --chart) CHART=$2; shift 2 ;;
    --namespace-prefix) NS_PREFIX=$2; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

case "$CHART" in
  openwork) RELEASE="openwork-web-helm" ;;
  opencode) RELEASE="opencode-web-helm" ;;
  deepseek-harness|dsh) RELEASE="dsh-web-helm" ;;
  *) echo "ERROR: unknown --chart $CHART" >&2; exit 2 ;;
esac
LEGACY=$RELEASE-shared-pvc
RELEASE_NS=$RELEASE

command -v "$KUBECTL" >/dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }
RECOVER=$SELF_DIR/recover-legacy-shared.sh
[ -x "$RECOVER" ] || { echo "ERROR: $RECOVER not found/executable" >&2; exit 1; }

# Every namespace holding the legacy claim, minus the release namespace.
# Ask for the namespace and name as explicit columns. Parsing `-o name` assumed
# it prints `persistentvolumeclaim/<ns>/<name>`; some kubectl builds omit the
# namespace, which made every row fail the match and reported "no targets" —
# a false clean fleet.
# F13: a failed discovery (RBAC, expired credentials, unreachable API) must not
# read as "no namespace holds the claim" — that is a false clean fleet.
if ! RAW=$("$KUBECTL" get pvc -A --no-headers \
     -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name 2>/dev/null); then
  echo "ERROR: could not list PVCs cluster-wide (RBAC? expired credentials?)." >&2
  echo "       Refusing to report an empty fleet." >&2
  exit 1
fi
NS_LIST=$(printf '%s\n' "$RAW" \
  | awk -v c="$LEGACY" -v p="$NS_PREFIX" -v r="$RELEASE_NS" \
      '$2==c && index($1,p)==1 && $1!=r {print $1}' \
  | sort -u)

echo "==> mode:    $([ "$APPLY" = 1 ] && echo APPLY || echo 'DRY RUN (pass --apply to run)')"
echo "==> chart:   $CHART"
echo "==> claim:   $LEGACY (read-only)"
echo "==> exclude: $RELEASE_NS (platform anchor)"
if [ -z "$NS_LIST" ]; then
  echo "==> targets: none — no $NS_PREFIX* namespace holds $LEGACY"
  exit 0
fi
echo "==> targets: $(printf '%s' "$NS_LIST" | grep -c .) namespace(s)"
echo

failed=0
ok=0
for ns in $NS_LIST; do
  echo "=============== $ns"
  if [ "$APPLY" = 1 ]; then
    "$RECOVER" --namespace "$ns" --chart "$CHART" --apply
  else
    "$RECOVER" --namespace "$ns" --chart "$CHART"
  fi
  rc=$?
  if [ "$rc" -eq 0 ]; then ok=$((ok + 1)); else failed=$((failed + 1)); fi
done

echo
echo "=============== summary"
echo "  ok:     $ok"
echo "  failed: $failed"
[ "$APPLY" = 1 ] || echo "  (dry run: nothing was created)"
[ "$failed" -eq 0 ] || exit 1
exit 0
