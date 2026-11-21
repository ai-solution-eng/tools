#!/usr/bin/env bash
# Stage 1 of the cross-namespace shared workspace (see .dsh/xns-shared/DESIGN.md):
# move every unit's /workspace/shared content into /workspace/personal/shared-legacy
# so the shared volume can be swapped for one platform-wide volume without
# orphaning anything, and the old per-namespace shared PVCs can be retired.
#
# Why this shape:
#   - personal and shared are DIFFERENT volumes, so this is a copy + delete, not
#     a rename. The copy is verified (entry count + apparent bytes) before a
#     single source file is removed.
#   - it runs inside the unit pod, as the unit's own uid, so the moved files stay
#     owned by the user. A Job or an exec-as-root would leave root-owned files
#     the user cannot edit afterwards.
#   - idempotent: a marker on the personal volume makes a re-run a no-op.
#
# usage:
#   scripts/migrate-shared-to-personal.sh                 # DRY RUN, every unit
#   scripts/migrate-shared-to-personal.sh --apply         # do it
#   scripts/migrate-shared-to-personal.sh --slug foo --apply
#   scripts/migrate-shared-to-personal.sh --namespace project-user-foo --apply
#
# Sibling charts: the payload is chart-agnostic (all three mount /workspace/personal
# and /workspace/shared), only the pod selector differs:
#   deepseek-harness  app=dsh-user,dsh-user-managed=true        (default)
#   opencode          app=opencode-user,opencode-user-managed=true
#   openwork          app=openwork-user,openwork-user-managed=true
# Pass it with --selector.
#
# exits: 0 all good, 1 kubectl problem, 2 usage, 3 at least one unit skipped
set -uo pipefail

APPLY=0
ALLOW_SHARED_NS=0
SLUG=""
NS=""
NS_PREFIX=""
KUBECTL=${KUBECTL:-kubectl}
SELECTOR="app=dsh-user,dsh-user-managed=true"

usage() { sed -n '2,26p' "$0"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --allow-shared-namespace) ALLOW_SHARED_NS=1; shift ;;
    --slug) SLUG=${2:-}; shift 2 ;;
    --namespace|-n) NS=${2:-}; shift 2 ;;
    --selector) SELECTOR=${2:-}; shift 2 ;;
    --chart)
      case "${2:-}" in
        deepseek-harness|dsh) SELECTOR="app=dsh-user,dsh-user-managed=true" ;;
        opencode) SELECTOR="app=opencode-user,opencode-user-managed=true" ;;
        openwork) SELECTOR="app=openwork-user,openwork-user-managed=true" ;;
        *) echo "unknown --chart ${2:-} (deepseek-harness|opencode|openwork)" >&2; exit 2 ;;
      esac
      shift 2 ;;
    --namespace-prefix) NS_PREFIX=${2:-}; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

command -v "$KUBECTL" >/dev/null 2>&1 || { echo "ERROR: kubectl not found" >&2; exit 1; }

# ---------------------------------------------------------------- payload ----
# Runs INSIDE the pod. POSIX sh is enough.
DRY_PAYLOAD=$(cat <<'SNIP'
set -eu
SRC=/workspace/shared
DSTROOT=/workspace/personal
DST=$DSTROOT/shared-legacy
MARKER=$DSTROOT/.shared-legacy-migrated
if [ ! -d "$SRC" ]; then echo "verdict=no-shared-mount"; exit 0; fi
if [ -f "$MARKER" ]; then echo "verdict=already-migrated"; exit 0; fi
entries=$(find "$SRC" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')
bytes=$(du -sb "$SRC" 2>/dev/null | cut -f1)
avail=$(df -B1 --output=avail "$DSTROOT" | tail -1 | tr -d ' ')
existing=0
[ -d "$DST" ] && existing=$(find "$DST" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')
if [ "$entries" -eq 0 ]; then echo "verdict=empty entries=0 bytes=$bytes avail=$avail"; exit 0; fi
if [ "$existing" -gt 0 ]; then echo "verdict=dest-exists entries=$entries bytes=$bytes avail=$avail dest_entries=$existing"; exit 0; fi
if [ "$bytes" -gt "$avail" ]; then echo "verdict=no-space entries=$entries bytes=$bytes avail=$avail"; exit 0; fi
echo "verdict=ready entries=$entries bytes=$bytes avail=$avail"
SNIP
)

APPLY_PAYLOAD=$(cat <<'SNIP'
set -eu
SRC=/workspace/shared
DSTROOT=/workspace/personal
DST=$DSTROOT/shared-legacy
MARKER=$DSTROOT/.shared-legacy-migrated
if [ ! -d "$SRC" ]; then echo "verdict=no-shared-mount"; exit 0; fi
if [ -f "$MARKER" ]; then echo "verdict=already-migrated"; exit 0; fi
entries=$(find "$SRC" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')
if [ "$entries" -eq 0 ]; then echo "verdict=empty entries=0"; exit 0; fi
if [ -d "$DST" ] && [ -n "$(ls -A "$DST" 2>/dev/null)" ]; then echo "verdict=dest-exists"; exit 9; fi
bytes=$(du -sb "$SRC" | cut -f1)
avail=$(df -B1 --output=avail "$DSTROOT" | tail -1 | tr -d ' ')
if [ "$bytes" -gt "$avail" ]; then echo "verdict=no-space bytes=$bytes avail=$avail"; exit 9; fi

mkdir -p "$DST"
cp -a "$SRC"/. "$DST"/

# Verify BEFORE removing anything. Directory st_size is filesystem-dependent — a
# correct copy of a tree legitimately reports different directory sizes — so
# compare what must be identical: the tree shape, the regular-file byte total,
# and every symlink target.
# Verification must be CONTENT-BLIND-PROOF: a shape+size signature still let a
# same-size swap (a.txt<->b.txt) pass, after which `find -delete` destroyed the
# source. Files are therefore compared by sha256, not by size.
tree_sig() { ( cd "$1" && find . -mindepth 1 -printf '%y %P\n' | sort ); }
tree_hash() { ( cd "$1" && find . -mindepth 1 -type f -print0 | sort -z | xargs -0 -r sha256sum | sort ); }
file_bytes() { find "$1" -mindepth 1 -type f -printf '%s\n' | awk '{s+=$1} END{printf "%d", s+0}'; }
link_targets() { ( cd "$1" && find . -mindepth 1 -type l -printf '%P -> %l\n' | sort ); }

src_sig=$(tree_sig "$SRC"); dst_sig=$(tree_sig "$DST")
src_h=$(tree_hash "$SRC"); dst_h=$(tree_hash "$DST")
src_b=$(file_bytes "$SRC"); dst_b=$(file_bytes "$DST")
src_l=$(link_targets "$SRC"); dst_l=$(link_targets "$DST")
if [ "$src_sig" != "$dst_sig" ] || [ "$src_h" != "$dst_h" ] || [ "$src_b" != "$dst_b" ] || [ "$src_l" != "$dst_l" ]; then
  echo "verdict=verify-failed shape=$([ "$src_sig" = "$dst_sig" ] && echo ok || echo differs) content=$([ "$src_h" = "$dst_h" ] && echo ok || echo differs) bytes=${src_b}/${dst_b} links=$([ "$src_l" = "$dst_l" ] && echo ok || echo differs)"
  exit 9
fi

# Source is intact and fully copied: clear it, leaving the mount point itself.
find "$SRC" -mindepth 1 -delete
printf 'migrated=%s\nentries=%s\nbytes=%s\n' "$(date -Is)" "$entries" "$src_b" > "$MARKER"
echo "verdict=moved entries=$entries bytes=$src_b"
SNIP
)

# ------------------------------------------------------------- per-pod run ---
# Drop to the volume owner's uid inside the container: the unit process runs as
# root and setpriv's the services, so a plain exec would leave root-owned files.
run_in_pod() {
  local ns="$1" pod="$2" payload="$3"
  printf '%s' "$payload" | "$KUBECTL" exec -i -n "$ns" "$pod" -- sh -c '
    p=/workspace/personal
    u=$(stat -c %u "$p" 2>/dev/null || echo 0)
    g=$(stat -c %g "$p" 2>/dev/null || echo 0)
    if [ "$u" != "0" ] && [ "$(id -u)" = "0" ]; then
      exec setpriv --reuid="$u" --regid="$g" --clear-groups sh -s
    fi
    exec sh -s
  ' 2>&1
}

echo "==> mode:      $([ "$APPLY" = 1 ] && echo APPLY || echo 'DRY RUN (pass --apply to change anything)')"
echo "==> selector:  $SELECTOR"
[ -n "$SLUG" ] && echo "==> slug:      $SLUG"
[ -n "$NS" ] && echo "==> namespace: $NS"
echo

# Every unit pod, across the release namespace AND the project-user-* ones.
jsonpath='jsonpath={range .items[*]}{.metadata.namespace}{" "}{.metadata.name}{" "}{.status.phase}{"\n"}{end}'
if [ -n "$NS" ]; then
  pods=$("$KUBECTL" get pods -n "$NS" -l "$SELECTOR" -o "$jsonpath")
else
  pods=$("$KUBECTL" get pods -A -l "$SELECTOR" -o "$jsonpath")
fi || { echo "ERROR: kubectl get pods failed" >&2; exit 1; }

if [ -z "$pods" ]; then echo "no unit pods matched"; exit 0; fi

# C8 guard, fail-closed: units in the RELEASE namespace all mount ONE shared
# claim, so the "shared folder" is a single volume — running the migration for
# several of them would copy that one volume into the first user's personal
# space and then delete it for everyone else. Refuse rather than guess.
if [ "$APPLY" = 1 ] && [ "$ALLOW_SHARED_NS" != 1 ]; then
  # Count CONSUMERS OF THE CLAIM (deployments whose pod template mounts a
  # *-shared-pvc claim), not live pods: a unit that is quota-rejected, scaled to
  # zero or evicted has no pod but still shares the volume, and counting pods
  # missed exactly the case this guard exists for.
  dupes=""
  for ns in $(printf '%s\n' "$pods" | awk 'NF {print $1}' | sort -u); do
    n=$("$KUBECTL" get deploy -n "$ns" -o jsonpath='{range .items[*]}{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}{"\n"}{end}' 2>/dev/null \
      | awk '/-shared-pvc$/ {c++} END {print c+0}')
    [ "${n:-0}" -gt 1 ] && dupes="$dupes$ns ($n units mounting a shared claim)
"
  done
  if [ -n "$dupes" ]; then
    echo "REFUSING TO APPLY: these namespaces hold more than one unit, so those units share" >&2
    echo "one shared claim. Migrating them would move the same volume into the first user's" >&2
    echo "personal space and delete it for the rest." >&2
    printf '  %s\n' "$dupes" >&2
    echo "Pick ONE unit per such namespace with --slug, or pass --allow-shared-namespace" >&2
    echo "if you really mean to migrate that volume once." >&2
    exit 2
  fi
fi

printf '%-42s %-34s %-10s %s\n' NAMESPACE POD BYTES RESULT
skipped=0
while read -r ns pod phase; do
  [ -z "${pod:-}" ] && continue
  if [ -n "$SLUG" ] && [ "$pod" != *"$SLUG"* ]; then continue; fi
  if [ -n "$NS_PREFIX" ] && [ "$ns" != "$NS_PREFIX"* ]; then continue; fi
  # HARD GUARD: a pod already mounting the cross-namespace claim is looking at the
  # PLATFORM-WIDE shared volume. Migrating it would move everyone's data into this
  # one user's personal folder and then delete it from the shared volume.
  shared_claim=$("$KUBECTL" get pod -n "$ns" "$pod" -o jsonpath='{.spec.volumes[?(@.name=="shared-workspace")].persistentVolumeClaim.claimName}' 2>/dev/null || true)
  case "$shared_claim" in
    *-xns-pvc)
      printf '%-42s %-34s %-10s %s\n' "$ns" "$pod" "-" "skip: already on the shared claim ($shared_claim)"
      continue ;;
  esac
  if [ "$phase" != "Running" ]; then
    printf '%-42s %-34s %-10s %s\n' "$ns" "$pod" "-" "skip: pod is $phase"
    skipped=$((skipped + 1))
    continue
  fi
  if [ "$APPLY" = 1 ]; then out=$(run_in_pod "$ns" "$pod" "$APPLY_PAYLOAD"); else out=$(run_in_pod "$ns" "$pod" "$DRY_PAYLOAD"); fi
  verdict=$(printf '%s' "$out" | sed -n 's/.*verdict=\([a-z-]*\).*/\1/p' | tail -1)
  bytes=$(printf '%s' "$out" | sed -n 's/.*bytes=\([0-9]*\).*/\1/p' | tail -1)
  case "${verdict:-error}" in
    ready|moved) ;;
    empty|already-migrated|no-shared-mount) ;;
    *) skipped=$((skipped + 1)); verdict="ATTENTION:$verdict" ;;
  esac
  printf '%-42s %-34s %-10s %s\n' "$ns" "$pod" "${bytes:-?}" "$verdict"
  case "${verdict:-}" in ATTENTION:*) printf '    %s\n' "$out" ;; esac
done <<< "$pods"

echo
if [ "$APPLY" != 1 ]; then
  echo "dry run: nothing was moved. Re-run with --apply."
fi
[ "$skipped" -eq 0 ] || { echo "$skipped unit(s) need attention (see above)." >&2; exit 3; }
exit 0
