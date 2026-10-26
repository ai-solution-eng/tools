#!/usr/bin/env bash
# Preflight / record / verify helper for a dsh-web-helm upgrade.
#
# WHY THIS EXISTS
#   User data lives on PVCs (dsh-<ver>-ws-<slug>, dsh-<ver>-st-<slug>, and the
#   release shared PVC). Those survive `helm uninstall` only because
#   storage.keepPvcOnDelete=true (the default) makes the pre-delete hook LIST
#   them instead of deleting them. This script checks that contract, records
#   what must survive, and verifies it afterwards.
#
#   Note on uninstall+install: when the PVCs survive, the router's
#   ensurePersistentVolumeClaim() finds them and returns without creating
#   anything, so the SAME volumes are reused — no new storage is provisioned.
#   The re-create (and the VAST quota-collision risk that comes with it) only
#   happens if a PVC is actually deleted first: keepPvcOnDelete=false, a manual
#   `kubectl delete pvc`, or a storage.nameVersion bump.
#
# WHAT THE DUMP IS AND IS NOT
#   The PVs backing these claims use reclaimPolicy: Delete, so deleting a PVC
#   destroys its PV and its data. The YAML this script writes is therefore a
#   RECORD (names, UIDs, volumeName, sizes, ages) for audit and comparison —
#   it is NOT a restorable backup of file contents. Re-binding works only
#   while the PV still exists, which under Delete means the PVC was never
#   removed. For real recoverability you need VolumeSnapshot support from the
#   storage driver (not assumed here).
#
# USAGE
#   scripts/pre-upgrade-check.sh                 # preflight + record (dry)
#   scripts/pre-upgrade-check.sh --out DIR       # also write the manifest
#   scripts/pre-upgrade-check.sh --verify DIR    # after the upgrade: compare
#   scripts/pre-upgrade-check.sh --namespace NS  # non-default namespace
#
# Exit codes: 0 ok, 1 a blocker was found, 2 usage/environment error.
if [ -z "${BASH_VERSION:-}" ]; then
  if command -v bash >/dev/null 2>&1; then exec bash "$0" "$@"; fi
  echo "error: this script requires bash" >&2; exit 2
fi
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${NAMESPACE:-dsh-web-helm}"
RELEASE="${RELEASE:-dsh-web-helm}"
MODE="check"
OUTDIR=""

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --out)      shift; OUTDIR="${1:-}"; [ -n "$OUTDIR" ] || { echo "error: --out needs a dir" >&2; exit 2; }; shift ;;
    --verify)   shift; MODE="verify"; OUTDIR="${1:-}"; [ -n "$OUTDIR" ] || { echo "error: --verify needs the dir from the earlier run" >&2; exit 2; }; shift ;;
    --namespace|-n) shift; NAMESPACE="${1:-}"; [ -n "$NAMESPACE" ] || { echo "error: --namespace needs a value" >&2; exit 2; }; shift ;;
    --help|-h)  usage; exit 0 ;;
    *) echo "error: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

BLOCKERS=0
blocker() { echo "  BLOCKER: $*" >&2; BLOCKERS=$((BLOCKERS + 1)); }
ok()      { echo "  ok: $*"; }
warn()    { echo "  WARN: $*" >&2; }

# ---------------------------------------------------------------- verify mode
if [ "$MODE" = "verify" ]; then
  MAN="$OUTDIR/pvc-manifest.tsv"
  [ -f "$MAN" ] || { echo "error: $MAN not found (run --out DIR before the upgrade)" >&2; exit 2; }
  echo "==> verifying PVCs against $MAN"
  MISSING=0; CHANGED=0
  while IFS=$'\t' read -r ns name uid vol phase; do
    [ "$ns" = "NAMESPACE" ] && continue
    cur_uid="$("$KUBECTL" get pvc "$name" -n "$ns" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
    cur_vol="$("$KUBECTL" get pvc "$name" -n "$ns" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"
    cur_ph="$("$KUBECTL" get pvc "$name" -n "$ns" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [ -z "$cur_uid" ]; then
      echo "  MISSING: $ns/$name (existed before, gone now)" >&2; MISSING=$((MISSING + 1))
    elif [ "$cur_uid" != "$uid" ]; then
      echo "  REPLACED: $ns/$name UID $uid -> $cur_uid (NEW EMPTY VOLUME)" >&2; CHANGED=$((CHANGED + 1))
    elif [ -n "$vol" ] && [ "$cur_vol" != "$vol" ]; then
      echo "  REBOUND: $ns/$name volume $vol -> $cur_vol (data not carried over)" >&2; CHANGED=$((CHANGED + 1))
    elif [ "$cur_ph" != "Bound" ]; then
      echo "  NOT BOUND: $ns/$name phase=$cur_ph" >&2; CHANGED=$((CHANGED + 1))
    else
      echo "  ok: $ns/$name unchanged (uid + volumeName match, Bound)"
    fi
  done < "$MAN"
  echo
  if [ "$MISSING" -gt 0 ] || [ "$CHANGED" -gt 0 ]; then
    echo "RESULT: $MISSING missing, $CHANGED replaced/rebound — user data did NOT come through intact" >&2
    exit 1
  fi
  echo "RESULT: every recorded PVC survived with the same UID and volume — data carried through"
  exit 0
fi

# ------------------------------------------------------------- preflight mode
echo "==> dsh-web-helm pre-upgrade preflight (namespace: $NAMESPACE)"
echo
echo "-- 1. PVC-survival contract --"
if ! RAW="$("$KUBECTL" get deploy "$RELEASE" -n "$NAMESPACE" \
      -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="KEEP_PVC_ON_DELETE")]}{.value}{end}' 2>&1)"; then
  blocker "cannot read deployment $RELEASE/$NAMESPACE: $RAW"
elif [ "$RAW" = "true" ]; then
  ok "KEEP_PVC_ON_DELETE=true (uninstall keeps user PVCs)"
elif [ -z "$RAW" ]; then
  warn "KEEP_PVC_ON_DELETE not set on the live deployment; the chart default is true."
  warn "The value is baked in at install time — confirm your values set storage.keepPvcOnDelete: true"
else
  blocker "KEEP_PVC_ON_DELETE=$RAW — an uninstall WOULD DELETE user PVCs. Set storage.keepPvcOnDelete: true before upgrading."
fi
echo
echo "-- 2. storage.nameVersion (must NOT change across an upgrade) --"
CURRENT_NV="$("$KUBECTL" get pvc -n "$NAMESPACE" -l dsh-user-managed=true -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [ -n "$CURRENT_NV" ]; then
  PREFIX="$(printf '%s' "$CURRENT_NV" | sed -E 's/^dsh-(v[0-9]+)-.*/\1/')"
  ok "existing per-user PVCs use nameVersion prefix: $PREFIX"
  echo "     (keep storage.nameVersion=$PREFIX; a bump mints FRESH EMPTY volumes and orphans the old data)"
else
  warn "no dsh-user-managed PVCs found in $NAMESPACE — nothing to protect here"
fi
echo
echo "-- 3. inventory of PVCs that must survive --"
NSLIST="$NAMESPACE"
# unit namespaces (platform Option B) that hold dsh-managed PVCs
for ns in $("$KUBECTL" get pvc -A -l dsh-user-managed=true -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' 2>/dev/null | sort -u); do
  case " $NSLIST " in *" $ns "*) ;; *) NSLIST="$NSLIST $ns" ;; esac
done
COUNT=0
for ns in $NSLIST; do
  n="$("$KUBECTL" get pvc -n "$ns" -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | grep -c . || true)"
  [ "${n:-0}" -gt 0 ] && echo "  $ns: $n PVC(s)" && COUNT=$((COUNT + n))
done
echo "  total: $COUNT"
echo
echo "-- 4. hazards --"
echo "  SAFE (no PVC is re-created, so no VAST CreateVolume call):"
echo "    - 'helm upgrade'."
echo "    - uninstall+install while keepPvcOnDelete=true: the router's"
echo "      ensurePersistentVolumeClaim() does GET-then-return when the claim exists,"
echo "      so existing volumes are reused as-is. The release shared PVC is also"
echo "      protected by helm.sh/resource-policy: keep."
echo "  RISKY (PVC deleted, then re-created -> new VAST quota needed):"
echo "    - keepPvcOnDelete=false, or a manual 'kubectl delete pvc', or a"
echo "      storage.nameVersion bump. The VAST quota name is"
echo "      csi:<ns>:<pvc>:pvc-<uid> truncated at 64 chars, which cuts off the UID,"
echo "      so a re-create reuses the SAME quota name and can fail with"
echo "      'Quota name must be unique per tenant', leaving the PVC Pending."
echo "      Remedy: scripts/cleanup-dsh-web-helm-stale-quotas.sh (dry-run first)."
echo "  - The PVs backing these claims use reclaimPolicy: Delete — deleting a PVC destroys its data."
echo "  - This script records the PVCs; it does NOT back up file contents."

if [ -n "$OUTDIR" ]; then
  mkdir -p "$OUTDIR"
  MAN="$OUTDIR/pvc-manifest.tsv"
  printf 'NAMESPACE\tNAME\tUID\tVOLUMENAME\tPHASE\n' > "$MAN"
  for ns in $NSLIST; do
    "$KUBECTL" get pvc -n "$ns" -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.spec.volumeName}{"\t"}{.status.phase}{"\n"}{end}' 2>/dev/null >> "$MAN" || true
  done
  for ns in $NSLIST; do
    "$KUBECTL" get pvc -n "$ns" -o yaml 2>/dev/null | sed -n '/^apiVersion: v1/,$p' >> "$OUTDIR/pvc-raw.yaml" || true
  done
  echo "wrote $MAN ($(( $(wc -l < "$MAN") - 1 )) rows)"
  echo "wrote $OUTDIR/pvc-raw.yaml"
  echo "NOTE: a record, not a backup — see the header comment."
fi

echo
if [ "$BLOCKERS" -gt 0 ]; then
  echo "RESULT: $BLOCKERS blocker(s) — do NOT uninstall until fixed." >&2
  exit 1
fi
echo "RESULT: preflight passed. Safe to run 'helm upgrade'. If you must uninstall+install, re-run with --out and then --verify afterwards."
