#!/bin/sh
# strip-pvc-owner-refs.sh — one-off migration for the PVC-survival contract
# (dsh-web-helm >= 0.4.13).
#
# WHY THIS EXISTS:
#   Older chart versions created the user data PVCs (per-user ws/st claims,
#   warm-pool claims, per-namespace shared mirror) WITH an ownerReference to
#   the per-user anchor ConfigMap (dsh-anchor-<slug>).
#   Deleting that anchor — an admin user-delete, an accidental deletion, or
#   the anchor cascading during teardown — therefore GC-deleted the PVCs and,
#   because the storage class reclaim policy is Delete, the underlying PVs
#   and VAST volumes: the user lost all workspace/state data.
#
#   New chart versions create these PVCs WITHOUT ownerReferences (they are
#   never deleted by the app; purge = manual `kubectl delete pvc`). But
#   PVCs that already exist keep their legacy ownerReferences — this script
#   strips them so the GC can never cascade user data PVCs again.
#
# WHAT IT DOES:
#   Merge-patches `metadata.ownerReferences: null` on every PVC labeled
#   dsh-user-managed=true in
#   the release namespace and in every unit namespace that hosts such a PVC
#   (discovered cluster-wide from the label; registry not required).
#
# SAFETY:
#   - Dry run by default: prints every PVC it WOULD patch. Nothing changes
#     without --apply.
#   - Platform-owned PVCs (ezprojects.hpe.com/* labels: user-pvc,
#     kubeflow-shared-pvc, models-pvc, notebook workspaces, ...) are NEVER
#     touched — they do not carry the chart label, and a belt-and-braces
#     check refuses any PVC with an ezprojects.hpe.com/* label.
#   - Removing an ownerReference never restarts or disturbs the pods; the
#     PVC stays Bound to the same PV the whole time.
#
# Requirements: sh (POSIX), kubectl.
#
# Usage:
#   ./strip-pvc-owner-refs.sh                     # dry run, both charts' labels
#   ./strip-pvc-owner-refs.sh --apply             # patch for real
#   ./strip-pvc-owner-refs.sh --label opencode-user-managed=true --apply
#   ./strip-pvc-owner-refs.sh --label dsh-user-managed=true --apply
set -eu

APPLY=0
LABEL=''

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --label) LABEL="$2"; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
  shift
done

if [ -z "$LABEL" ]; then
  # Default: handle both apps in one run (a namespace may host either/both).
  LABELS='opencode-user-managed=true dsh-user-managed=true'
else
  LABELS="$LABEL"
fi

patched=0
skipped_platform=0
for lbl in $LABELS; do
  echo "=== scanning PVCs labeled $lbl ==="
  # Cluster-wide label scan; fall back to per-namespace listing if -A is
  # restricted by cluster policy.
  PVC_LIST=$(kubectl get pvc -A -l "$lbl" -o json 2>/dev/null || echo '')
  if [ -z "$PVC_LIST" ]; then
    echo "  WARN: cluster-wide PVC list unavailable (RBAC/policy);" \
         "run per namespace: kubectl get pvc -n <ns> -l $lbl"
    continue
  fi
  echo "$PVC_LIST" | command grep -o '"name": "[^"]*"' >/dev/null 2>&1 || continue
  echo "$PVC_LIST" | python3 -c '
import json,sys
data=json.load(sys.stdin)
for item in data.get("items",[]):
    ns=item["metadata"]["namespace"]; name=item["metadata"]["name"]
    labels=item["metadata"].get("labels",{}) or {}
    if any(l.startswith("ezprojects.hpe.com/") for l in labels):
        print(f"SKIP-PLATFORM\t{ns}\t{name}"); continue
    owner=item["metadata"].get("ownerReferences") or []
    print(("HAS-OWNERREF" if owner else "CLEAN") + f"\t{ns}\t{name}")
' | while IFS="$(printf '\t')" read -r state ns name; do
    case "$state" in
      SKIP-PLATFORM)
        echo "  [$ns] $name: platform-owned, skipping (never touched)"
        ;;
      CLEAN)
        echo "  [$ns] $name: no ownerReferences, nothing to do"
        ;;
      HAS-OWNERREF)
        if [ "$APPLY" = "1" ]; then
          if kubectl patch pvc "$name" -n "$ns" --type=merge -p '{"metadata":{"ownerReferences":null}}' >/dev/null 2>&1; then
            echo "  [$ns] $name: ownerReferences stripped ✔"
          else
            echo "  [$ns] $name: PATCH FAILED — inspect manually" >&2
          fi
        else
          echo "  [$ns] $name: HAS ownerReferences — WOULD strip (dry run)"
        fi
        ;;
    esac
  done
done

if [ "$APPLY" = "1" ]; then
  echo "=== done (applied) ==="
else
  echo "=== dry run complete — re-run with --apply to patch ==="
fi
