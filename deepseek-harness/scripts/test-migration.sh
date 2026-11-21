#!/usr/bin/env bash
# Offline test of the migration payload against two scratch dirs standing in for
# the personal and shared volumes. Extracts the heredocs from the real script so
# the tested text is the shipped text.
set -uo pipefail
SCRIPT=${1:?usage: test-migration.sh <script>}
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT

extract() {  # extract() <DRY|APPLY>
  awk -v want="$1" '
    $0 ~ ("^" want "_PAYLOAD=\\$\\(cat <<.SNIP.$") { grab=1; next }
    grab && /^SNIP$/ { exit }
    grab { print }
  ' "$SCRIPT"
}

# Rewrite the two hardcoded mount points to the scratch dirs.
rewrite() { sed -e "s#/workspace/shared#$T/shared#g" -e "s#/workspace/personal#$T/personal#g"; }

DRY=$(extract DRY | rewrite)
APPLY=$(extract APPLY | rewrite)
[ -n "$DRY" ] || { echo "FAIL: could not extract DRY payload"; exit 1; }
[ -n "$APPLY" ] || { echo "FAIL: could not extract APPLY payload"; exit 1; }

pass=0; fail=0
ok()   { pass=$((pass+1)); echo "PASS  $1"; }
bad()  { fail=$((fail+1)); echo "FAIL  $1"; [ -n "${2:-}" ] && echo "      $2"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got [$2] want [$3]"; fi; }

fresh() { rm -rf "$T/shared" "$T/personal"; mkdir -p "$T/shared" "$T/personal"; }

# 1. empty shared
fresh
check "empty shared -> verdict=empty" "$(sh -c "$DRY" | sed -n 's/verdict=\([a-z-]*\).*/\1/p')" "empty"

# 2. populated: files, nested dirs, dotfile, empty dir, symlink
fresh
mkdir -p "$T/shared/sub/deep" "$T/shared/empty-dir"
printf 'alpha\n' > "$T/shared/a.txt"
printf 'beta\n'  > "$T/shared/sub/b.txt"
printf 'gamma\n' > "$T/shared/sub/deep/c.txt"
printf 'hidden\n' > "$T/shared/.dotfile"
ln -s a.txt "$T/shared/link-to-a"
filebytes() { find "$1" -mindepth 1 -type f -printf '%s\n' | awk '{s+=$1} END{printf "%d", s+0}'; }
shape() { ( cd "$1" && find . -mindepth 1 -printf '%y %P\n' | sort ); }
before_n=$(find "$T/shared" -mindepth 1 | wc -l | tr -d ' ')
before_b=$(filebytes "$T/shared")
before_s=$(shape "$T/shared")
check "populated -> dry run says ready" "$(sh -c "$DRY" | sed -n 's/verdict=\([a-z-]*\).*/\1/p')" "ready"
check "dry run moves nothing" "$(find "$T/shared" -mindepth 1 | wc -l | tr -d ' ')" "$before_n"

out=$(sh -c "$APPLY"); rc=$?
check "apply exits 0" "$rc" "0"
check "apply verdict=moved" "$(printf '%s' "$out" | sed -n 's/verdict=\([a-z-]*\).*/\1/p')" "moved"
check "source is now empty" "$(find "$T/shared" -mindepth 1 | wc -l | tr -d ' ')" "0"
check "destination entry count matches" "$(find "$T/personal/shared-legacy" -mindepth 1 | wc -l | tr -d ' ')" "$before_n"
check "destination file bytes match" "$(filebytes "$T/personal/shared-legacy")" "$before_b"
check "destination tree shape matches" "$(shape "$T/personal/shared-legacy")" "$before_s"
check "content survived" "$(cat "$T/personal/shared-legacy/sub/deep/c.txt")" "gamma"
check "dotfile survived" "$(cat "$T/personal/shared-legacy/.dotfile")" "hidden"
check "symlink preserved as a link" "$(test -L "$T/personal/shared-legacy/link-to-a" && echo yes || echo no)" "yes"
check "empty dir preserved" "$(test -d "$T/personal/shared-legacy/empty-dir" && echo yes || echo no)" "yes"
check "marker written" "$(test -f "$T/personal/.shared-legacy-migrated" && echo yes || echo no)" "yes"

# 3. re-run is a no-op
check "re-run -> already-migrated" "$(sh -c "$DRY" | sed -n 's/verdict=\([a-z-]*\).*/\1/p')" "already-migrated"

# 4. pre-existing destination is refused, source untouched
fresh
printf 'x\n' > "$T/shared/x.txt"
mkdir -p "$T/personal/shared-legacy"; printf 'old\n' > "$T/personal/shared-legacy/old.txt"
out=$(sh -c "$APPLY"); rc=$?
check "existing dest -> apply refuses (exit 9)" "$rc" "9"
check "existing dest -> verdict=dest-exists" "$(printf '%s' "$out" | sed -n 's/verdict=\([a-z-]*\).*/\1/p')" "dest-exists"
check "existing dest -> source untouched" "$(cat "$T/shared/x.txt")" "x"
check "existing dest -> dest untouched" "$(cat "$T/personal/shared-legacy/old.txt")" "old"

# --- F18 regression: a copy that swaps two equal-size files must be CAUGHT ------
# DeepSeek proved that the old shape+total-bytes signature let this through and
# then \`find -delete\` destroyed the source. The signature now carries per-path
# sizes; this test fails loudly if that regresses.
fresh
printf 'AAAA' > "$T/shared/a.txt"
printf 'BBBB' > "$T/shared/b.txt"
swapdir=$(mktemp -d)
cat > "$swapdir/cp" <<'SWAP'
#!/bin/sh
/bin/cp "$@" || exit 1
DEST=""
for a in "$@"; do case "$a" in */shared-legacy|*/shared-legacy/) DEST=$a ;; esac; done
if [ -n "$DEST" ] && [ -f "$DEST/a.txt" ] && [ -f "$DEST/b.txt" ]; then
  tmp=$(mktemp); /bin/cp "$DEST/a.txt" "$tmp"
  /bin/cp "$DEST/b.txt" "$DEST/a.txt"; /bin/cp "$tmp" "$DEST/b.txt"; rm -f "$tmp"
fi
SWAP
chmod +x "$swapdir/cp"
out=$(PATH="$swapdir:$PATH" sh -c "$APPLY"); rc=$?
check "size-preserving swap -> verify-failed" "$(printf '%s' "$out" | sed -n 's/verdict=\([a-z-]*\).*/\1/p')" "verify-failed"
check "size-preserving swap -> non-zero exit" "$([ "$rc" -ne 0 ] && echo yes || echo no)" "yes"
check "size-preserving swap -> SOURCE NOT DELETED" "$(cat "$T/shared/a.txt")$(cat "$T/shared/b.txt")" "AAAABBBB"
check "size-preserving swap -> no marker written" "$(test -f "$T/personal/.shared-legacy-migrated" && echo yes || echo no)" "no"
rm -rf "$swapdir"

# 5. no shared mount at all
fresh; rmdir "$T/shared"
check "missing shared mount -> no-shared-mount" "$(sh -c "$DRY" | sed -n 's/verdict=\([a-z-]*\).*/\1/p')" "no-shared-mount"

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
