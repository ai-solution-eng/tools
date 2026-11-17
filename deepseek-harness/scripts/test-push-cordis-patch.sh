#!/usr/bin/env bash
# Offline regression tests for push_dsh_cordis_patch.sh — NO cluster required.
#
# The script's cluster surface is exactly what a fake kubectl has to answer:
#   get deploy -A -l hpe-ezua/app=…            (release-namespace discovery)
#   get deploy -n NS -l dsh-user-managed=true  (unit inventory)
#   get configmap -n NS NAME -o jsonpath=…     (canonical read / read-back)
#   patch configmap -n NS NAME --type merge …  (canonical write)
#   exec -i -n NS deploy/NAME -c C -- sh -c …  (per-unit push, stdin = patch)
#
# Covered here:
#   * --router-only never execs into a pod (the "canonical write only" contract)
#   * the merge patch carries ONE key and nothing else
#   * idempotency, and read-back verification (including a corrupted read-back)
#   * the staged default never writes the live file; --live backs up first
#   * --from-configmap still reads the ConfigMap (shared resolver refactor)
#   * argument validation (--to-router needs --file, cannot mix with read modes)
#   * chart assertions: the init-only mount, optional volume, CM-first seed
#
#   ./scripts/test-push-cordis-patch.sh
set -u

here=$(cd "$(dirname "$0")/.." && pwd)
script="$here/scripts/push_dsh_cordis_patch.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
state="$work/state"
mkdir -p "$work/bin" "$state"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }

# ------------------------------------------------------------------ fake kubectl
cat > "$work/bin/kubectl" <<'KUBECTL_SHIM'
#!/usr/bin/env bash
# Fake kubectl for test-push-cordis-patch.sh. Only the calls the push script makes.
set -u
STATE="${FAKE_STATE:?}"
UNITS="${FAKE_UNITS:-dsh-user-alice dsh-user-bob}"
CM_FILE="$STATE/cm.yml"

arg_after() { # arg_after <flag> "$@" -> value following the flag
  local want="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      "$want") printf '%s' "${2:-}"; return 0 ;;
      "$want"=*) printf '%s' "${1#*=}"; return 0 ;;
    esac
    shift
  done
  return 1
}

first_positional() { # skips flags and their values
  local i=0 n=$#
  local -a a=("$@")
  while [ "$i" -lt "$n" ]; do
    case "${a[$i]}" in
      -n|--namespace|-o|--output|-l|--selector|-c|--container) i=$((i + 2)) ;;
      -*) i=$((i + 1)) ;;
      *) printf '%s' "${a[$i]}"; return 0 ;;
    esac
  done
  return 1
}

jsonpath_of() {
  local i=0 n=$#
  local -a a=("$@")
  while [ "$i" -lt "$n" ]; do
    case "${a[$i]}" in
      -o|--output) printf '%s' "${a[$((i + 1))]:-}"; return 0 ;;
      -o*) printf '%s' "${a[$i]#-o}"; return 0 ;;
    esac
    i=$((i + 1))
  done
  return 0
}

cmd="${1:-}"; shift || true
case "$cmd" in
  get)
    sub="${1:-}"; shift || true
    case "$sub" in
      deploy)
        # release-namespace discovery: -A with the chart's app label
        if printf '%s' "$*" | grep -q -- '-A'; then
          printf 'dsh-web-helm\n'
          exit 0
        fi
        # unit inventory
        if printf '%s' "$*" | grep -q -- 'dsh-user-managed=true'; then
          for u in $UNITS; do printf '%s|%s||\n' "$u" "${u#dsh-user-}"; done
          exit 0
        fi
        exit 1
        ;;
      configmap)
        if [ "${FAKE_CM_MISSING:-0}" = "1" ] || [ ! -f "$CM_FILE" ]; then
          echo 'Error from server (NotFound): configmaps "x" not found' >&2
          exit 1
        fi
        jp="$(jsonpath_of "$@")"
        case "$jp" in
          *metadata.name*) first_positional "$@" ;;
          *data.cordis*) cat "$CM_FILE" ;;
          *) printf 'apiVersion: v1\nkind: ConfigMap\n' ;;
        esac
        exit 0
        ;;
      namespace)
        exit 1
        ;;
    esac
    exit 1
    ;;
  patch)
    # configmap -n NS NAME --type merge --patch-file F | -p JSON
    pf="$(arg_after --patch-file "$@" || true)"
    pj="$(arg_after -p "$@" || true)"
    n=$(( $(cat "$STATE/patch.count" 2>/dev/null || echo 0) + 1 ))
    printf '%s\n' "$n" > "$STATE/patch.count"
    printf '%s\n' "$*" > "$STATE/patch-$n.args"
    if [ -n "$pf" ]; then
      cp "$pf" "$STATE/patch-$n.json"
    else
      printf '%s' "$pj" > "$STATE/patch-$n.json"
    fi
    if [ "${FAKE_PATCH_FAIL:-0}" = "1" ]; then
      echo 'Error from server (Forbidden): configmaps is forbidden' >&2
      exit 1
    fi
    # Apply data[key] to the fixture so the read-back reflects the write.
    python3 - "$STATE/patch-$n.json" "$CM_FILE" <<'PY'
import json, os, sys
patch = json.load(open(sys.argv[1]))
content = patch['data']['cordis.patch.yml']
if os.environ.get('FAKE_PATCH_CORRUPT') == '1':
    content = content + '# corrupted by the shim\n'
open(sys.argv[2], 'w').write(content)
PY
    printf 'configmap/%s patched\n' "$(first_positional "$@" || true)"
    exit 0
    ;;
  exec)
    n=$(( $(cat "$STATE/exec.count" 2>/dev/null || echo 0) + 1 ))
    printf '%s\n' "$n" > "$STATE/exec.count"
    printf '%s\n' "$*" > "$STATE/exec-$n.args"
    cat > "$STATE/exec-$n.stdin"
    exit 0
    ;;
esac
exit 1
KUBECTL_SHIM
chmod +x "$work/bin/kubectl"

# ----------------------------------------------------------------------- helpers
reset_state() {
  rm -rf "$state"
  mkdir -p "$state"
  cat > "$work/patch.yml" <<'PATCH'
- insert:
    - id: probe-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: streamable-http
        serverName: probe
        url: http://example.invalid:9200/mcp
PATCH
  # an OLD canonical copy, so --to-router has something to change
  printf '# old canonical copy\n- id: legacy\n  disabled: true\n' > "$state/cm.yml"
}

count_of() { # count_of <file> -> integer
  if [ -f "$1" ]; then cat "$1"; else echo 0; fi
}

run_push() { # run_push <args…>; stdout+stderr -> $work/out, exit code -> $work/rc
  KUBECTL="$work/bin/kubectl" FAKE_STATE="$state" NAMESPACE=dsh-web-helm \
    bash "$script" "$@" > "$work/out" 2>&1
  echo $? > "$work/rc"
}

expect_rc() { # expect_rc <label> <expected>
  local got; got="$(cat "$work/rc")"
  if [ "$got" = "$2" ]; then pass "$1"; else bad "$1 (rc=$got, expected $2)"; sed 's/^/       /' "$work/out" | head -12; fi
}

expect_contains() { # expect_contains <label> <pattern> [file]
  local f="${3:-$work/out}"
  if grep -q -- "$2" "$f"; then pass "$1"; else bad "$1 (no match for: $2)"; sed 's/^/       /' "$f" | head -12; fi
}

expect_not_contains() { # expect_not_contains <label> <pattern>
  if grep -q -- "$2" "$work/out"; then bad "$1 (unexpected match: $2)"; else pass "$1"; fi
}

expect_eq() { # expect_eq <label> <expected> <actual>
  if [ "$2" = "$3" ]; then pass "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

# ============================================================== push-script tests
echo "push script"

reset_state
run_push --to-router --file "$work/patch.yml" --router-only
expect_rc "router-only dry run exits 0" 0
expect_contains "dry run says it would patch" "canonical: would patch"
expect_eq "dry run performs no patch" 0 "$(count_of "$state/patch.count")"
expect_eq "dry run performs no exec" 0 "$(count_of "$state/exec.count")"

reset_state
run_push --to-router --file "$work/patch.yml" --router-only --apply
expect_rc "router-only apply exits 0" 0
expect_contains "canonical write is verified" "verified by read-back"
expect_eq "canonical write happened once" 1 "$(count_of "$state/patch.count")"
expect_eq "router-only never execs into a pod" 0 "$(count_of "$state/exec.count")"
if cmp -s "$work/patch.yml" "$state/cm.yml"; then pass "canonical copy now equals the local file"; else bad "canonical copy differs from the local file"; fi
expect_contains "router-only tells you the next step" "--from-configmap --apply"

# the merge patch must carry exactly one key
python3 - "$state/patch-1.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
ok = list(p.keys()) == ['data'] and list(p['data'].keys()) == ['cordis.patch.yml']
print('  ok   merge patch carries data[cordis.patch.yml] only' if ok else '  FAIL merge patch shape: %r' % (p,))
sys.exit(0 if ok else 1)
PY
[ $? -eq 0 ] || fail=$((fail + 1))

# idempotency
run_push --to-router --file "$work/patch.yml" --router-only --apply
expect_rc "second identical publish exits 0" 0
expect_contains "second publish is a no-op" "already up to date"
expect_eq "second publish does not patch again" 1 "$(count_of "$state/patch.count")"

# canonical + fan-out in one run
reset_state
run_push --to-router --file "$work/patch.yml" --apply
expect_rc "combined publish+push exits 0" 0
expect_eq "combined run patches the ConfigMap" 1 "$(count_of "$state/patch.count")"
expect_eq "combined run pushes to both units" 2 "$(count_of "$state/exec.count")"
expect_contains "combined run stages, not live" "cordis.patch-update.yml"
expect_not_contains "combined run never writes the live file" "cat > /var/dsh/home/.dsh/profiles/web/cordis.patch.yml"
if cmp -s "$work/patch.yml" "$state/exec-1.stdin"; then pass "unit receives the local file verbatim"; else bad "unit payload differs from the local file"; fi

# staged-only fan-out (no canonical write)
reset_state
run_push --file "$work/patch.yml" --apply
expect_rc "staged push exits 0" 0
expect_eq "staged push leaves the ConfigMap alone" 0 "$(count_of "$state/patch.count")"
expect_eq "staged push reaches both units" 2 "$(count_of "$state/exec.count")"
expect_contains "staged push uses the -update sibling" "cordis.patch-update.yml"
expect_not_contains "staged push never writes the live file" "cat > /var/dsh/home/.dsh/profiles/web/cordis.patch.yml"

# --live: backup first, then the live write
reset_state
run_push --live --file "$work/patch.yml" --apply
expect_rc "--live push exits 0" 0
expect_contains "--live writes the live file" "cat > /var/dsh/home/.dsh/profiles/web/cordis.patch.yml" "$state/exec-1.args"
expect_contains "--live takes a backup" "cp -p /var/dsh/home/.dsh/profiles/web/cordis.patch.yml /var/dsh/home/.dsh/profiles/web/cordis.patch-backup.yml" "$state/exec-1.args"
python3 - "$state/exec-1.args" <<'PY'
import sys
cmd = open(sys.argv[1]).read()
b = cmd.find('cordis.patch-backup.yml')
w = cmd.find('cat > /var/dsh/home/.dsh/profiles/web/cordis.patch.yml')
print('  ok   backup precedes the live write' if 0 <= b < w else '  FAIL backup ordering (backup@%d, write@%d)' % (b, w))
sys.exit(0 if 0 <= b < w else 1)
PY
[ $? -eq 0 ] || fail=$((fail + 1))
expect_contains "--live warns it is destructive" "WARNING: --live rewrites the LIVE patch"

# legacy TARGET_NAME still works, but warns and still backs up
reset_state
TARGET_NAME=cordis.patch.yml KUBECTL="$work/bin/kubectl" FAKE_STATE="$state" NAMESPACE=dsh-web-helm \
  bash "$script" --file "$work/patch.yml" --apply > "$work/out" 2>&1
echo $? > "$work/rc"
expect_rc "legacy TARGET_NAME exits 0" 0
expect_contains "legacy TARGET_NAME warns" "warning: TARGET_NAME=cordis.patch.yml overwrites the LIVE patch"
expect_contains "legacy TARGET_NAME still backs up" "cordis.patch-backup.yml"

# --from-configmap still reads the ConfigMap (shared resolver refactor)
reset_state
printf -- '- id: from-configmap\n  disabled: true\n' > "$state/cm.yml"
run_push --from-configmap --apply
expect_rc "--from-configmap exits 0" 0
expect_contains "--from-configmap reports its source" "source: --from-configmap"
expect_eq "--from-configmap reaches both units" 2 "$(count_of "$state/exec.count")"
python3 - "$state/exec-1.stdin" <<'PY'
import sys
got = open(sys.argv[1]).read()
want = '- id: from-configmap\n  disabled: true\n'
print('  ok   --from-configmap payload matches the ConfigMap' if got.rstrip('\n') == want.rstrip('\n') else '  FAIL payload: %r' % got)
sys.exit(0 if got.rstrip('\n') == want.rstrip('\n') else 1)
PY
[ $? -eq 0 ] || fail=$((fail + 1))

# read-back verification catches a corrupted write
reset_state
FAKE_PATCH_CORRUPT=1 KUBECTL="$work/bin/kubectl" FAKE_STATE="$state" NAMESPACE=dsh-web-helm \
  bash "$script" --to-router --file "$work/patch.yml" --router-only --apply > "$work/out" 2>&1
echo $? > "$work/rc"
expect_rc "corrupted write fails the run" 1
expect_contains "corrupted write is reported" "verification FAILED"

# argument validation
reset_state
run_push --to-router --router-only --apply
expect_rc "--to-router without --file fails" 1
expect_contains "--to-router explains what it needs" "requires --file"

reset_state
run_push --to-router --from-configmap --apply
expect_rc "--to-router + --from-configmap fails" 1
expect_contains "the mix is explained" "cannot be combined with --from-configmap"

reset_state
FAKE_CM_MISSING=1 KUBECTL="$work/bin/kubectl" FAKE_STATE="$state" NAMESPACE=dsh-web-helm \
  bash "$script" --to-router --file "$work/patch.yml" --router-only --apply > "$work/out" 2>&1
echo $? > "$work/rc"
expect_rc "missing ConfigMap fails the run" 1
expect_contains "missing ConfigMap is explained" "not found in:"

# The YAML pre-check is best-effort BY DESIGN: it warns on invalid YAML, and says
# so when PyYAML is unavailable (the in-pod supervisor is the real gate). Either
# message proves the pre-check ran; silence would prove it did not.
reset_state
printf 'this: [is: not: valid: yaml\n' > "$work/bad.yml"
run_push --to-router --file "$work/bad.yml" --router-only --apply
if grep -q -e "not valid YAML" -e "pre-check skipped" "$work/out"; then
  pass "YAML pre-check ran and reported its outcome"
else
  bad "YAML pre-check produced no verdict"
  sed 's/^/       /' "$work/out" | head -8
fi

# =============================================================== chart assertions
echo "chart"
if ! command -v helm >/dev/null 2>&1; then
  echo "  SKIP helm not available (chart assertions skipped)"
else
  cat > "$work/vals.yaml" <<'VALS'
ezua:
  domainName: example.com
  virtualService:
    endpoint: dsh.example.com
    istioGateway: istio-system/ezaf-gateway
dshCordisPatch: |
  - id: probe-a
    disabled: true
VALS
  cat > "$work/vals-empty.yaml" <<'VALS'
ezua:
  domainName: example.com
  virtualService:
    endpoint: dsh.example.com
    istioGateway: istio-system/ezaf-gateway
dshCordisPatch: ""
VALS
  if timeout 120 helm template t "$here" -n dsh-web-helm -f "$work/vals.yaml" > "$work/render.yaml" 2>"$work/render.err"; then
    pass "helm template renders the chart"
    grep -q "mountPath: '/opt/dsh-patch', readOnly: true" "$work/render.yaml" \
      && pass "canonical patch is mounted read-only at /opt/dsh-patch" \
      || bad "canonical patch mount missing"
    grep -q "name: 't-project-config', optional: true" "$work/render.yaml" \
      && pass "patch volume is optional (a missing mirror cannot block a pod)" \
      || bad "patch volume is not optional"
    grep -q "'t-project-config'," "$work/render.yaml" \
      && pass "patch ConfigMap is in the platform-namespace mirror list" \
      || bad "patch ConfigMap is not mirrored for platform units"
    seed="$(grep -m1 'if \[ -s /opt/dsh-patch/cordis.patch.yml \]' "$work/render.yaml" || true)"
    if [ -n "$seed" ]; then
      case "$seed" in
        *'elif [ -n "'*) pass "seed prefers the ConfigMap, falls back to the baked value" ;;
        *) bad "seed does not fall back to the baked value" ;;
      esac
    else
      bad "seed does not try the ConfigMap first"
    fi
    printf '%s' "$seed" | grep -q 'if \[ ! -f /var/dsh/home/.dsh/profiles/web/cordis.patch.yml \]' \
      && pass "seed stays strictly seed-once (existing patch never overwritten)" \
      || bad "seed lost its [ ! -f ] guard"
    awk '/name: .dsh-web./{c=1} c&&/opt\/dsh-patch/{print "MOUNTED-IN-APP"; exit}' "$work/render.yaml" | grep -q MOUNTED-IN-APP \
      && bad "the patch ConfigMap is mounted in the app container" \
      || pass "the patch ConfigMap is init-only"
  else
    bad "helm template failed: $(tail -3 "$work/render.err")"
  fi
  if timeout 120 helm template t2 "$here" -n dsh-web-helm -f "$work/vals-empty.yaml" > "$work/render-empty.yaml" 2>&1; then
    grep -q 'elif \[ -n "" \]' "$work/render-empty.yaml" \
      && pass "empty patch value creates no patch file" \
      || bad "empty patch value renders an unconditional seed"
  else
    bad "helm template with an empty patch failed"
  fi
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "all tests passed"
  exit 0
fi
echo "$fail test(s) FAILED"
exit 1
