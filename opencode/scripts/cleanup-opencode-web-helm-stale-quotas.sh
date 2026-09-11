#!/bin/sh
# cleanup-opencode-web-helm-stale-quotas.sh
#
# Clears stale VAST quotas left behind by repeated opencode-web-helm installs.
#
# Background:
#   The VAST CSI driver truncates volume/quota names at 64 chars
#   (csi:<namespace>:<pvc-name>:pvc-<uid>), which can cut off the unique PVC-UID
#   suffix. Leftover quotas from previous installs then collide with new
#   CreateVolume calls ("Quota name must be unique per tenant"), leaving PVCs
#   Pending and opencode warm-pool pods unschedulable.
#
# What this script does:
#   1. Reads the VMS endpoint + credentials from the gl4f-csi/gl4f-mgmt secret
#      (works from any node with kubectl access to the cluster).
#   2. Lists BOUND PVCs in the target namespace -> protects their quotas.
#   3. Deletes every VAST quota under 'csi:<namespace>:' that does not belong
#      to a currently Bound PVC (and optionally the legacy 'csi:opencode:*'
#      quotas from a namespace that no longer exists).
#   4. Optionally deletes Pending (unbound) PVCs so the opencode controller
#      recreates them fresh.
#
# SAFETY:
#   - Dry run by default. Nothing is deleted without --apply.
#   - Only quotas whose name starts with the exact target prefixes are touched.
#     Quotas of every other namespace (project-user-*, monitoring, ...) are
#     never matched.
#   - Quotas of Bound PVCs are always protected, including truncated names
#     (matched by the UID fragment embedded in the quota name).
#
# Requirements: sh (POSIX), kubectl, curl, jq, network access to VMS :443.
#
# Usage:
#   ./cleanup-opencode-web-helm-stale-quotas.sh                 # dry run
#   ./cleanup-opencode-web-helm-stale-quotas.sh --apply         # delete
#   ./cleanup-opencode-web-helm-stale-quotas.sh --apply --reset-pvcs
#   ./cleanup-opencode-web-helm-stale-quotas.sh --namespace opencode-web-helm

set -eu

NS=opencode-web-helm
SECRET_NS=gl4f-csi
SECRET=gl4f-mgmt
LEGACY_PREFIX='csi:opencode:'
APPLY=0
RESET_PVCS=0
SKIP_OPENCODE=0

BOUND_FILE=$(mktemp)
QUOTA_FILE=$(mktemp)
STALE_FILE=$(mktemp)
trap 'rm -f "$BOUND_FILE" "$QUOTA_FILE" "$STALE_FILE" 2>/dev/null' EXIT

usage() {
    cat <<EOF
usage: $0 [options]
  --apply            actually delete stale quotas (default: dry run)
  --namespace NS     target k8s namespace (default: opencode-web-helm)
  --reset-pvcs       after cleanup, delete Pending PVCs so they are recreated
  --skip-opencode    do not touch legacy 'csi:opencode:*' quotas
  -h, --help         this help
EOF
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1 ;;
        -n|--namespace) NS="$2"; shift ;;
        --reset-pvcs) RESET_PVCS=1 ;;
        --skip-opencode) SKIP_OPENCODE=1 ;;
        -h|--help) usage ;;
        *) usage ;;
    esac
    shift
done

for c in kubectl curl jq; do
    command -v "$c" >/dev/null 2>&1 || { echo "ERROR: required tool '$c' not found"; exit 1; }
done

# base64 decode that works on GNU coreutils and BSD/macOS
b64d() {
    if base64 -d </dev/null >/dev/null 2>&1; then base64 -d; else base64 -D; fi
}

echo "==> Reading VMS credentials from secret $SECRET_NS/$SECRET"
VMS_HOST=$(kubectl -n "$SECRET_NS" get secret "$SECRET" -o jsonpath='{.data.endpoint}' | b64d | tr -d '[:space:]')
VMS_USER=$(kubectl -n "$SECRET_NS" get secret "$SECRET" -o jsonpath='{.data.username}' | b64d)
VMS_PASS=$(kubectl -n "$SECRET_NS" get secret "$SECRET" -o jsonpath='{.data.password}' | b64d)
case "$VMS_HOST" in https://*|http://*) VMS_HOST=${VMS_HOST#*://} ;; esac
VMS_HOST=${VMS_HOST%%/*}
[ -n "$VMS_HOST" ] || { echo "ERROR: secret has no 'endpoint' key"; exit 1; }
[ -n "$VMS_USER" ] || { echo "ERROR: secret has no 'username' key"; exit 1; }

echo "==> Authenticating to VMS at https://$VMS_HOST"
TOKEN=$(curl -k -s -X POST "https://$VMS_HOST/api/token/" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$VMS_USER\",\"password\":\"$VMS_PASS\"}" | jq -r '.access // empty')
[ -n "$TOKEN" ] || { echo "ERROR: VMS authentication failed"; exit 1; }

fetch_quotas() {
    # Handles both response shapes: plain array (older VMS) and {data_list: [...]} (newer)
    curl -k -s "https://$VMS_HOST/api/quotas/?limit=1000" -H "Authorization: Bearer $TOKEN" \
        | jq -r 'if type=="object" and has("data_list") then (.data_list | .[])
                 elif type=="array" then .[]
                 else empty end
                 | [.id, .name] | @tsv'
}

echo
echo "==> PVCs in namespace $NS:"
kubectl -n "$NS" get pvc -o custom-columns=NAME:.metadata.name,PHASE:.status.phase,VOLUMENAME:.spec.volumeName 2>/dev/null || \
    echo "  (namespace not found or no PVCs)"

# BOUND PVCs only -> their quotas are LIVE and must be kept
kubectl -n "$NS" get pvc -o json 2>/dev/null \
    | jq -r '.items[]
             | select(.spec.volumeName != null and .spec.volumeName != "")
             | "\(.metadata.name)\t\(.metadata.uid)"' > "$BOUND_FILE" || true

echo "==> Protected (Bound) PVC count: $(awk 'END{print NR}' "$BOUND_FILE" 2>/dev/null || echo 0)"

fetch_quotas > "$QUOTA_FILE"
[ -s "$QUOTA_FILE" ] || { echo "ERROR: VMS returned no quotas (auth ok? API version?)"; exit 1; }

# A quota is LIVE only if its pvc-name is currently Bound AND the UID fragment
# embedded in the quota name is a prefix of that PVC's UID. Everything else
# under the target prefix is stale.
awk -F'\t' -v ns="$NS" '
    FILENAME == ARGV[1] { if (NF >= 2) bound[$1] = substr($2, 1, 10); next }
    {
        name = $2
        if (index(name, "csi:" ns ":") != 1) next
        rest = name
        sub(/^csi:[^:]*:/, "", rest)
        pvc = rest;     sub(/:pvc-.*$/, "", pvc)
        uidpart = rest; sub(/^[^:]*:pvc-/, "", uidpart)
        keep = 0
        if (pvc in bound && uidpart != "" && index(bound[pvc], uidpart) == 1) keep = 1
        if (keep == 0) print $1 "\t" name
    }
' "$BOUND_FILE" "$QUOTA_FILE" > "$STALE_FILE"

# Legacy 'csi:opencode:*' quotas (namespace 'opencode' no longer exists)
if [ "$SKIP_OPENCODE" != "1" ] && [ "$NS" != "opencode" ]; then
    awk -F'\t' '$2 ~ /^csi:opencode:/ { print $1 "\t" $2 }' "$QUOTA_FILE" >> "$STALE_FILE"
fi

N=$(awk 'END{print NR}' "$STALE_FILE")
if [ "$N" = "0" ]; then
    echo "==> No stale quotas found. Nothing to do."
    exit 0
fi

echo
echo "==> Stale quota(s) to delete ($N):"
awk -F'\t' '{ printf "  %4s  %s\n", $1, $2 }' "$STALE_FILE"
echo

if [ "$APPLY" != "1" ]; then
    echo "DRY RUN: nothing was deleted. Re-run with --apply to delete."
    if [ "$RESET_PVCS" = "1" ]; then
        PENDING=$(kubectl -n "$NS" get pvc -o json 2>/dev/null \
            | jq -r '.items[] | select(.spec.volumeName == null or .spec.volumeName == "") | .metadata.name' || true)
        [ -n "$PENDING" ] && { echo "DRY RUN: would also delete Pending PVCs:"; echo "$PENDING"; }
    fi
    exit 0
fi

echo "==> Deleting stale quotas:"
awk -F'\t' '{print $1}' "$STALE_FILE" | while IFS= read -r id; do
    [ -n "$id" ] || continue
    code=$(curl -k -s -o /dev/null -w '%{http_code}' \
        -X DELETE "https://$VMS_HOST/api/quotas/$id/" -H "Authorization: Bearer $TOKEN")
    echo "  id=$id -> HTTP $code"
done

if [ "$RESET_PVCS" = "1" ]; then
    PENDING=$(kubectl -n "$NS" get pvc -o json 2>/dev/null \
        | jq -r '.items[] | select(.spec.volumeName == null or .spec.volumeName == "") | .metadata.name' || true)
    if [ -n "$PENDING" ]; then
        echo
        echo "==> Deleting Pending PVCs (opencode controller recreates them):"
        echo "$PENDING"
        echo "$PENDING" | xargs kubectl -n "$NS" delete pvc
    fi
fi

echo
echo "==> Remaining quotas under 'csi:$NS:' (should be only live/Bound ones):"
fetch_quotas | awk -F'\t' -v ns="$NS" 'index($2, "csi:" ns ":") == 1 { printf "  %s\n", $2 }'

echo
echo "==> Current status:"
kubectl -n "$NS" get pvc 2>/dev/null || true
kubectl -n "$NS" get pods 2>/dev/null || true
echo
echo "Done. PVCs bind on the next provisioner retry (~60s)."
echo "Watch with: kubectl -n $NS get pvc,pods -w"
