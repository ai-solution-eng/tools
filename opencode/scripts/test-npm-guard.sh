#!/usr/bin/env bash
# Regression tests for the unit-init npm guard (npmBakedGuardCommand).
#
# The guard is the only thing that decides which opencode/openchamber pair a unit
# runs, and every bug in it so far was invisible from pod status: the unit comes
# up fine while running the wrong binaries. These tests execute the real guard
# (extracted from templates/configmap-router.yaml, paths redirected into a temp
# tree, npm stubbed) and assert on the resulting filesystem state.
#
# No cluster, no registry, no docker. Requires bash and node.
#
#   ./scripts/test-npm-guard.sh
set -u

here=$(cd "$(dirname "$0")/.." && pwd)
template="$here/templates/configmap-router.yaml"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }

# --- extract the guard body from the template ---------------------------------
cat > "$work/extract.mjs" <<'JS'
import fs from 'node:fs'
const line = fs.readFileSync(process.argv[2], 'utf8')
  .split('\n')
  .find((l) => l.includes('npmBakedGuardCommand = '))
if (!line) { console.error('npmBakedGuardCommand not found'); process.exit(2) }
let s = line.slice(line.indexOf('= ') + 2).trim()
if (s.startsWith("'")) s = s.slice(1)
if (s.endsWith("'")) s = s.slice(0, -1)
s = s.split("\\'").join("'")
const i = s.indexOf("sh -c '")
if (i < 0) { console.error('sh -c body not found'); process.exit(2) }
let body = s.slice(i + 7)
if (body.endsWith("'")) body = body.slice(0, -1)
process.stdout.write(body)
JS

guard="$work/guard.sh"
node "$work/extract.mjs" "$template" > "$guard" || { echo "cannot extract the guard"; exit 2; }
sh -n "$guard" || { echo "guard does not parse"; exit 2; }

root="$work/root"
reset() {
  rm -rf "$root"
  mkdir -p "$root"/{opt/opencode/npm/bin,var/opencode/data,var/opencode/home,usr/local/bin,bin}
  cat > "$root/bin/npm" <<'NPM'
#!/usr/bin/env bash
prefix=""; pkg=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) prefix="$2"; shift 2 ;;
    -g|install) shift ;;
    *) pkg="$1"; shift ;;
  esac
done
mkdir -p "$prefix" 2>/dev/null || true
[ "${NPM_EXIT:-0}" = "0" ] || { echo "npm: EACCES simulated" >&2; exit 1; }
if [ -n "${NPM_FAIL_PKG:-}" ]; then case "$pkg" in *"$NPM_FAIL_PKG"*) echo "npm: simulated failure for $pkg" >&2; exit 1 ;; esac; fi
name="$(echo "$pkg" | sed -e 's|^@[^/]*/||' -e 's|@.*||')"
[ "$name" = "opencode-ai" ] && name=opencode
[ "$name" = "web" ] && name=openchamber
mkdir -p "$prefix/bin"
printf '#!/usr/bin/env bash\necho %s\n' "$pkg" > "$prefix/bin/$name"
chmod +x "$prefix/bin/$name"
NPM
  chmod +x "$root/bin/npm"
  printf '#!/usr/bin/env bash\necho baked-opencode\n' > "$root/opt/opencode/npm/bin/opencode"
  printf '#!/usr/bin/env bash\necho baked-openchamber\n' > "$root/opt/opencode/npm/bin/openchamber"
  chmod +x "$root"/opt/opencode/npm/bin/*
}
baked() {  # $1=opencode marker  $2=openchamber marker
  echo "$1" > "$root/opt/opencode/npm/.opencode-version"
  echo "$2" > "$root/opt/opencode/npm/.openchamber-version"
}
real_prefix() {  # $1=opencode bin output  $2=openchamber bin output
  mkdir -p "$root/var/opencode/data/npm/bin"
  printf '#!/usr/bin/env bash\necho %s\n' "$1" > "$root/var/opencode/data/npm/bin/opencode"
  printf '#!/usr/bin/env bash\necho %s\n' "$2" > "$root/var/opencode/data/npm/bin/openchamber"
  chmod +x "$root/var/opencode/data/npm/bin/"*
}
run_guard() {  # $1=OPENCODE_VERSION $2=OPENCHAMBER_VERSION $3=OPENCHAMBER_INSTALL
  sed -e "s|/opt/opencode/npm|$root/opt/opencode/npm|g" \
      -e "s|/var/opencode/data/npm|$root/var/opencode/data/npm|g" \
      -e "s|/var/opencode/.opencode-version|$root/var/opencode/.opencode-version|g" \
      -e "s|/var/opencode/.openchamber-version|$root/var/opencode/.openchamber-version|g" \
      -e "s|/var/opencode/home|$root/var/opencode/home|g" \
      -e "s|/usr/local/bin/|$root/usr/local/bin/|g" "$guard" > "$work/guard.rendered.sh"
  PATH="$root/bin:$PATH" OPENCODE_VERSION="$1" OPENCHAMBER_VERSION="$2" \
    OPENCHAMBER_INSTALL="$3" RUN_AS_USER="" sh "$work/guard.rendered.sh" \
    > "$work/guard.log" 2>&1
  status=$?
}
state() {
  p="$root/var/opencode/data/npm"
  if [ -L "$p" ]; then echo alias
  elif [ -d "$p" ]; then echo real
  else echo missing; fi
}
opencode_runs() { "$root/var/opencode/data/npm/bin/opencode" 2>/dev/null | head -1; }

echo "npm guard regression tests"

echo "1. fresh PVC + image bakes the configured pair -> alias to the baked pair"
reset; baked 1.19.4 1.24.2; run_guard 1.19.4 1.24.2 true
[ "$status" = 0 ] && pass "guard exits 0" || bad "guard exit=$status"
[ "$(state)" = alias ] && pass "state is an alias" || bad "state is $(state)"
[ "$(opencode_runs)" = baked-opencode ] && pass "runs the baked pair" || bad "runs $(opencode_runs)"

echo "2. alias + image falls behind -> real prefix, installs the configured pair"
reset; baked 1.19.4 1.24.2; run_guard 1.19.4 1.24.2 true
baked 1.18.11 1.17.2; run_guard 1.19.3 1.24.2 true
[ "$(state)" = real ] && pass "alias replaced by a real prefix" || bad "state is $(state)"
[ "$(opencode_runs)" = "opencode-ai@1.19.3" ] && pass "runs the configured opencode" || bad "runs $(opencode_runs)"

echo "3. same, but the registry is unreachable -> alias restored, unit stays runnable"
reset; baked 1.19.4 1.24.2; run_guard 1.19.4 1.24.2 true
baked 1.18.11 1.17.2; NPM_EXIT=1 run_guard 1.19.3 1.24.2 true
[ "$status" = 0 ] && pass "guard exits 0 (does not abort the init)" || bad "guard exit=$status"
[ "$(state)" = alias ] && pass "alias restored" || bad "state is $(state)"
[ "$(opencode_runs)" = baked-opencode ] && pass "unit still has a runnable binary" || bad "runs $(opencode_runs)"
grep -q 'install failed; leaving the existing binary in place' "$work/guard.log" \
  && pass "failure is logged" || bad "failure is silent"

echo "4. real stale prefix + image catches up -> upgraded in place"
reset; baked 1.19.4 1.24.2; real_prefix stale-1.19.3 stale-1.24.2
echo 1.19.3 > "$root/var/opencode/.opencode-version"; echo 1.24.2 > "$root/var/opencode/.openchamber-version"
run_guard 1.19.4 1.24.2 true
[ "$(opencode_runs)" = "opencode-ai@1.19.4" ] && pass "upgraded to the configured pair" || bad "runs $(opencode_runs)"

echo "5. real prefix already at the configured pair -> no reinstall"
reset; baked 1.19.4 1.24.2; real_prefix current-1.19.4 current-1.24.2
echo 1.19.4 > "$root/var/opencode/.opencode-version"; echo 1.24.2 > "$root/var/opencode/.openchamber-version"
run_guard 1.19.4 1.24.2 true
[ "$(opencode_runs)" = "current-1.19.4" ] && pass "left alone" || bad "runs $(opencode_runs)"
[ ! -s "$work/guard.log" ] && pass "no install attempted" || bad "unexpected log: $(tr '\n' '|' < "$work/guard.log")"

echo "6. non-chamber unit -> opencode only, no openchamber install"
reset; baked 1.19.4 1.24.2; run_guard 1.19.4 1.24.2 true
baked 1.18.11 1.17.2; run_guard 1.19.3 1.24.2 ""
[ "$(opencode_runs)" = "opencode-ai@1.19.3" ] && pass "opencode installed" || bad "runs $(opencode_runs)"
[ ! -e "$root/var/opencode/data/npm/bin/openchamber" ] \
  && pass "openchamber untouched" || bad "openchamber was installed"

echo "7. partially-failed install leaves a real prefix dir -> reset to the alias, unit runnable"
reset; baked 1.19.4 1.24.2; mkdir -p "$root/var/opencode/data/npm"
echo 1.19.3 > "$root/var/opencode/.opencode-version"; echo 1.24.2 > "$root/var/opencode/.openchamber-version"
NPM_EXIT=1 run_guard 1.19.4 1.24.2 true
[ "$status" = 0 ] && pass "guard exits 0" || bad "guard exit=$status"
[ "$(state)" = alias ] && pass "broken prefix replaced by the alias" || bad "state is $(state)"
[ "$(opencode_runs)" = baked-opencode ] && pass "unit runs the baked binary" || bad "runs $(opencode_runs)"
grep -q 'using the image copy' "$work/guard.log" \
  && pass "reset is logged" || bad "reset not logged"

echo "8. stale real prefix + registry dead + image bakes the configured pair -> converge onto the image"
reset; baked 1.19.4 1.24.2; real_prefix stale-1.19.3 stale-1.24.2
echo 1.19.3 > "$root/var/opencode/.opencode-version"; echo 1.24.2 > "$root/var/opencode/.openchamber-version"
NPM_EXIT=1 run_guard 1.19.4 1.24.2 true
[ "$status" = 0 ] && pass "guard exits 0" || bad "guard exit=$status"
[ "$(state)" = alias ] && pass "converged onto the image" || bad "state is $(state)"
[ "$(opencode_runs)" = baked-opencode ] && pass "runs the configured (baked) pair" || bad "runs $(opencode_runs)"
grep -q 'not the configured pair' "$work/guard.log" && pass "convergence logged" || bad "convergence not logged"

echo "9. only one install succeeds -> no mixed pair"
reset; baked 1.19.4 1.24.2; run_guard 1.19.4 1.24.2 true   # start from the alias
rm -f "$root/var/opencode/data/npm"                          # force the install branch
NPM_FAIL_PKG=web run_guard 1.19.4 1.24.2 true
[ "$(state)" = alias ] && pass "pair made atomic on the image" || bad "state is $(state)"
[ "$(opencode_runs)" = baked-opencode ] && pass "opencode from the image" || bad "runs $(opencode_runs)"

echo
if [ "$fail" -eq 0 ]; then echo "all npm guard tests passed"; exit 0; fi
echo "$fail npm guard test(s) failed"; exit 1
