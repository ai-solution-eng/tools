#!/usr/bin/env bash
# Recover a user's OLD /workspace/shared into /workspace/personal/shared-legacy.
#
# Companion to migrate-shared-to-personal.sh, for the users it CANNOT reach: the
# exec-based script needs a running unit still mounting the legacy claim. Once a
# unit rolls onto the cross-namespace shared claim its /workspace/shared is the
# PLATFORM-WIDE volume and the legacy claim is mounted nowhere - no pod to exec
# into. This does the same copy with a one-off Job (legacy claim read-only,
# personal claim read-write), so it works with the unit down.
#
# usage:
#   scripts/recover-legacy-shared.sh --namespace project-user-foo            # dry run
#   scripts/recover-legacy-shared.sh --namespace project-user-foo --apply    # run it
#
# options:
#   --chart openwork|opencode|deepseek-harness   (default openwork)
#   --legacy-claim NAME     default <release>-shared-pvc
#   --personal-claim NAME   default: the namespace's *-ws-* claim
#   --node NAME             default: the unit pod's node, when one is running
#
# The copy is verified before a marker is written, a second run is a no-op, and
# NOTHING is ever deleted from the legacy claim.
set -uo pipefail

APPLY=0
NS=""
CHART="openwork"
LEGACY=""
PERSONAL=""
NODE=""
KUBECTL=${KUBECTL:-kubectl}

usage() { sed -n '2,22p' "$0"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --namespace|-n) NS=$2; shift 2 ;;
    --chart) CHART=$2; shift 2 ;;
    --legacy-claim) LEGACY=$2; shift 2 ;;
    --personal-claim) PERSONAL=$2; shift 2 ;;
    --node) NODE=$2; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done
[ -n "$NS" ] || { echo "ERROR: --namespace is required" >&2; usage; }
command -v "$KUBECTL" >/dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }

case "$CHART" in
  openwork) RELEASE="openwork-web-helm"; APP="openwork-user"; CLAIM_PREFIX="openwork-" ;;
  opencode) RELEASE="opencode-web-helm"; APP="opencode-user"; CLAIM_PREFIX="opencode-" ;;
  deepseek-harness|dsh) RELEASE="dsh-web-helm"; APP="dsh-user"; CLAIM_PREFIX="dsh-" ;;
  *) echo "ERROR: unknown --chart $CHART" >&2; exit 2 ;;
esac
[ -n "$LEGACY" ] || LEGACY="$RELEASE-shared-pvc"

# F4 (checked first, so the refusal names the real reason): the release namespace
# holds the platform ANCHOR, never a user's legacy copy.
[ -n "$NS" ] && [ "$NS" = "$RELEASE" ] && { echo "ERROR: $NS is the release namespace (platform anchor) - refusing" >&2; exit 1; }

# The namespace holds a workspace claim per chart (dsh-v2-ws-*, openwork-v2-ws-*,
# opencode-v2-ws-*). Matching on '-ws-' alone picks whichever sorts first, i.e.
# the WRONG chart's volume, so filter on this chart's prefix and refuse ambiguity.
if [ -z "$PERSONAL" ]; then
  candidates=$("$KUBECTL" get pvc -n "$NS" -o name 2>/dev/null \
    | sed 's|^persistentvolumeclaim/||' | grep -E "^${CLAIM_PREFIX}[^-]*-ws-" || true)
  count=$(printf '%s' "$candidates" | grep -c . || true)
  if [ "$count" -eq 0 ]; then
    echo "ERROR: no ${CLAIM_PREFIX}*-ws-* claim in $NS (charts in this namespace:)" >&2
    "$KUBECTL" get pvc -n "$NS" -o name 2>/dev/null | sed 's|^persistentvolumeclaim/|  |' | grep -E -- '-ws-' >&2 || true
    exit 1
  fi
  if [ "$count" -gt 1 ]; then
    echo "ERROR: more than one ${CLAIM_PREFIX}*-ws-* claim in $NS; pass --personal-claim:" >&2
    printf '%s\n' "$candidates" | sed 's/^/  /' >&2
    exit 1
  fi
  PERSONAL=$candidates
fi
# Last line of defence: never mount another chart's workspace volume.
case "$PERSONAL" in
  "$CLAIM_PREFIX"*-ws-*) ;;
  *) echo "ERROR: personal claim '$PERSONAL' does not belong to chart $CHART (expected ${CLAIM_PREFIX}*-ws-*)" >&2; exit 1 ;;
esac

case "$LEGACY" in *-xns-pvc) echo "ERROR: $LEGACY is a cross-namespace claim - refusing" >&2; exit 1 ;; esac
anchor_pv=$("$KUBECTL" get pvc "$RELEASE-shared-pvc" -n "$RELEASE" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)
ANCHOR_HANDLE=""
[ -n "$anchor_pv" ] && ANCHOR_HANDLE=$("$KUBECTL" get pv "$anchor_pv" -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null || true)
legacy_pv=$("$KUBECTL" get pvc "$LEGACY" -n "$NS" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)
LEGACY_HANDLE=""
[ -n "$legacy_pv" ] && LEGACY_HANDLE=$("$KUBECTL" get pv "$legacy_pv" -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null || true)
if [ -n "$LEGACY_HANDLE" ] && [ "$LEGACY_HANDLE" = "$ANCHOR_HANDLE" ]; then
  echo "ERROR: $NS/$LEGACY already points at the platform volume - refusing" >&2; exit 1
fi
# F3: key the marker on the VOLUME, so a marker left by the migrate script or by
# another chart cannot suppress this recovery.
LEGACY_ID=$(printf '%s' "${LEGACY_HANDLE:-$LEGACY}" | tr -c 'a-zA-Z0-9.-' '_' | cut -c1-40)

if [ -z "$NODE" ]; then
  # Skip the router's transient clear-* Job pods: they carry the same app label.
  NODE=$("$KUBECTL" get pods -n "$NS" -l "app=$APP" -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.nodeName}{"\n"}{end}' 2>/dev/null \
    | grep -v -- '-clear-' | awk 'NF>1 {print $2; exit}' || true)
fi
# F2: a unit pod that exists but is unscheduled would deadlock the Job on
# Multi-Attach. Fail closed instead of silently creating an unpinned Job.
UNIT_PODS=$("$KUBECTL" get pods -n "$NS" -l "app=$APP" -o name 2>/dev/null | grep -v -- '-clear-' || true)
if [ -n "$UNIT_PODS" ] && [ -z "$NODE" ]; then
  echo "ERROR: a unit pod exists in $NS but has no node yet (rollout/pending)." >&2
  echo "       Re-run once it is scheduled, or pass --node explicitly." >&2
  exit 1
fi
NODE_SHOWN=$NODE
[ -n "$NODE_SHOWN" ] || NODE_SHOWN="<none: the personal claim is not attached to a running unit>"

for c in "$LEGACY" "$PERSONAL"; do
  phase=$("$KUBECTL" get pvc "$c" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [ "$phase" = "Bound" ] || { echo "ERROR: claim $NS/$c is not Bound (phase='$phase')" >&2; exit 1; }
done

# F5: the chart belongs in the name - Job templates are immutable, so two charts
# sharing a namespace would otherwise collide and the second could never run.
# Kubernetes copies the Job name into the pod template's job-name LABEL, which is
# capped at 63 chars, so a long namespace is rejected at admission. Cap the name.
JOB="shared-legacy-recover-$CHART-$(echo "$NS" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9-' '-')"
JOB=$(printf '%s' "$JOB" | cut -c1-63 | sed 's/-*$//')
[ "$(printf '%s' "$JOB" | wc -c)" -le 63 ] || { echo "ERROR: computed Job name too long: $JOB" >&2; exit 1; }
NODE_LINE=""
[ -n "$NODE" ] && NODE_LINE="      nodeName: $NODE"

echo "==> mode:      $([ "$APPLY" = 1 ] && echo APPLY || echo 'DRY RUN (pass --apply to create the Job)')"
echo "==> chart:     $CHART"
echo "==> namespace: $NS"
echo "==> legacy:    $LEGACY (read-only, never modified)"
echo "==> personal:  $PERSONAL"
echo "==> node:      $NODE_SHOWN"
echo "==> job:       $JOB"
echo

MANIFEST=$(cat <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB
  namespace: $NS
  labels:
    app: shared-legacy-recover
    recover-chart: $CHART
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 900
  ttlSecondsAfterFinished: 604800
  template:
    metadata:
      labels:
        app: shared-legacy-recover
        recover-chart: $CHART
      annotations:
        # Without this the namespace's Istio injection adds a sidecar that never
        # exits, the pod sits 1/2, and the Job never reaches Completed.
        sidecar.istio.io/inject: "false"
    spec:
      restartPolicy: Never
$NODE_LINE
      securityContext:
        runAsUser: 0
        runAsGroup: 0
      containers:
        - name: migrate
          image: alpine:3.20
          imagePullPolicy: IfNotPresent
          # F8: project-user-* namespaces carry a ResourceQuota that requires
          # requests AND limits on every container, or the pod is rejected at
          # admission and the Job silently never runs.
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits:   {cpu: "1", memory: 512Mi}
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -eu
              SRC=/legacy
              DSTROOT=/personal
              DST=\$DSTROOT/shared-legacy
              PART=\$DSTROOT/shared-legacy.partial
              MARKER=\$DSTROOT/.shared-legacy-migrated.$LEGACY_ID
              if [ ! -d "\$SRC" ]; then echo "verdict=no-legacy-mount"; exit 9; fi
              if [ -f "\$MARKER" ]; then echo "verdict=already-migrated"; exit 0; fi
              n=\$(find "\$SRC" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')
              if [ "\$n" -eq 0 ]; then echo "verdict=legacy-empty"; exit 0; fi
              if [ -e "\$DST" ]; then echo "verdict=dest-exists"; exit 9; fi
              bytes=\$(find "\$SRC" -type f -exec stat -c %s {} \; | awk '{s+=\$1} END{printf "%d", s+0}')
              # stat -f cannot wrap; df -k wraps a long csi.vastdata.com:/... source
              # onto two lines, so tail -1 read the Use% column instead of free blocks.
              set -- \$(stat -f -c '%a %S' "\$DSTROOT" 2>/dev/null || echo "0 0")
              avail_kb=\$(( (\${1:-0} * \${2:-0}) / 1024 ))
              case "\$avail_kb" in ''|*[!0-9]*) echo "verdict=no-df avail_kb=\$avail_kb"; exit 9 ;; esac
              need_kb=\$(( (bytes + 1023) / 1024 ))
              if [ "\$need_kb" -gt "\$avail_kb" ]; then echo "verdict=no-space need_kb=\$need_kb avail_kb=\$avail_kb"; exit 9; fi
              # F7: a root-owned personal root would produce a copy the user cannot
              # use, marked done and never repaired.
              owner=\$(stat -c %u:%g "\$DSTROOT")
              if [ "\$owner" = "0:0" ]; then echo "verdict=refused-root-owned owner=0:0"; exit 9; fi
              # F17: copy into a .partial dir and rename only after verification, so an
              # interrupted run never leaves a half-written shared-legacy that blocks
              # every later run with dest-exists.
              rm -rf "\$PART"
              mkdir -p "\$PART"
              cp -a "\$SRC"/. "\$PART"/
              # busybox find has no -printf: verify by content hash plus a path list.
              src_h=\$( cd "\$SRC" && find . -mindepth 1 -type f -exec sha256sum {} \; | sort )
              dst_h=\$( cd "\$PART" && find . -mindepth 1 -type f -exec sha256sum {} \; | sort )
              src_t=\$( cd "\$SRC" && find . -mindepth 1 | sort )
              dst_t=\$( cd "\$PART" && find . -mindepth 1 | sort )
              if [ "\$src_h" != "\$dst_h" ] || [ "\$src_t" != "\$dst_t" ]; then
                rm -rf "\$PART"
                echo "verdict=verify-failed source=untouched partial=removed"
                exit 9
              fi
              mv "\$PART" "\$DST"
              chown -R "\$owner" "\$DST"
              files=\$(printf '%s' "\$src_h" | grep -c . || true)
              printf 'migrated=%s\nsource=%s\nfiles=%s\nbytes=%s\n' "\$(date -Is)" "$LEGACY_ID" "\$files" "\$bytes" > "\$MARKER"
              echo "verdict=moved files=\$files bytes=\$bytes owner=\$owner"
          volumeMounts:
            - name: legacy
              mountPath: /legacy
              readOnly: true
            - name: personal
              mountPath: /personal
      volumes:
        - name: legacy
          persistentVolumeClaim:
            claimName: $LEGACY
            readOnly: true
        - name: personal
          persistentVolumeClaim:
            claimName: $PERSONAL
YAML
)

if [ "$APPLY" != 1 ]; then
  printf '%s\n' "$MANIFEST"
  echo "----"
  echo "dry run: nothing created. Re-run with --apply."
  exit 0
fi

# A stale Job (same deterministic name) blocks re-apply: clear it first.
"$KUBECTL" delete job "$JOB" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
printf '%s\n' "$MANIFEST" | "$KUBECTL" apply -f - || { echo "ERROR: could not create job/$JOB" >&2; exit 1; }
echo
echo "==> waiting for the Job (up to 15 min)..."
"$KUBECTL" wait -n "$NS" --for=condition=complete "job/$JOB" --timeout=900s >/dev/null 2>&1 || true
echo "--- logs ---"
OUT=$("$KUBECTL" logs -n "$NS" "job/$JOB" --tail=-1 2>/dev/null || true)
printf '%s\n' "$OUT"
echo "-------------"

# F1: a missing verdict is a FAILURE. The old code ended with `|| true` and
# exited 0, so a quota-rejected, unschedulable, OOMKilled or evicted pod — none of
# which ever prints a verdict — looked exactly like success.
COND=$("$KUBECTL" get job "$JOB" -n "$NS" -o jsonpath='{.status.conditions[?(@.type=="Complete")].status}' 2>/dev/null || true)
if [ "$COND" != "True" ]; then
  echo "FAIL: job/$JOB did not complete. Diagnostics:" >&2
  "$KUBECTL" describe job "$JOB" -n "$NS" 2>&1 | sed -n '/Conditions:/,/^Events:/p' | head -20 >&2
  "$KUBECTL" get events -n "$NS" --sort-by=.lastTimestamp 2>/dev/null | tail -5 >&2
  exit 1
fi
case "$OUT" in
  *"verdict=moved"*|*"verdict=already-migrated"*|*"verdict=legacy-empty"*) ;;
  *) echo "FAIL: job/$JOB completed but reported no success verdict (see logs above)" >&2; exit 1 ;;
esac
echo "(Job kept 7 days for its logs: kubectl -n $NS delete job/$JOB to remove it sooner)"
