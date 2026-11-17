#!/usr/bin/env bash
# Offline tests for push_openwork_json_config.sh — NO cluster, NO real kubectl.
#
# A fake kubectl shim is put first on PATH. It implements exactly the calls the
# push script makes:
#   get deploy -n NS -l ... -o jsonpath=...        -> fixed inventory
#   get configmap -n NS NAME -o jsonpath=...       -> current key value
#   patch cm NAME -n NS --type merge --patch-file  -> records the JSON and
#                                                     applies it to a fixture
#   exec [-i] -n NS deploy/X -c C -- sh -c SCRIPT  -> records the script and
#                                                     the stdin payload
# so every assertion below is about what the script WOULD do.
#
# Usage: scripts/test-push-config.sh          (exit 0 = all pass or SKIP)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PUSH="$SCRIPT_DIR/push_openwork_json_config.sh"

# Never let the caller's environment perturb the script under test.
unset KUBECTL NAMESPACE CONTAINER TARGET_NAME CONFIGMAP_NAME RELEASE_NAME RELEASE_NAMESPACE 2>/dev/null || true

if [ ! -f "$PUSH" ]; then
  echo "FAIL: push script not found: $PUSH" >&2
  exit 1
fi
if [ ! -x "$PUSH" ]; then
  echo "FAIL: push script is not executable: $PUSH" >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "SKIP: python3 is required (merge-patch JSON + the kubectl shim's fixtures)"
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/openwork-push-test.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

BIN="$WORK/bin"
SHIM="$WORK/shim"
mkdir -p "$BIN" "$SHIM/stdin"

# ----------------------------------------------------------------- the shim
cat > "$BIN/kubectl" <<'SHIM_EOF'
#!/usr/bin/env bash
# Fake kubectl for push_openwork_json_config.sh tests. Only what it calls.
set -uo pipefail
S="${SHIM_DIR:?SHIM_DIR not set}"
mkdir -p "$S/stdin"
printf '%s\n' "$*" >> "$S/argv.log"

cmd="${1:-}"
[ $# -gt 0 ] && shift

cm_value() {  # $1 = jsonpath -> the value, no trailing newline added
  python3 - "$S/cm.json" "$1" <<'PY'
import json, re, sys
cm_path, jp = sys.argv[1], sys.argv[2]
m = re.search(r"\.data\.(.+?)\}$", jp)
key = m.group(1).replace("\\.", ".") if m else ""
with open(cm_path, encoding="utf-8") as fh:
    data = json.load(fh)["data"]
sys.stdout.write(data.get(key, ""))
PY
}

case "$cmd" in
  get)
    sub="${1:-}"
    [ $# -gt 0 ] && shift
    case "$sub" in
      deploy)
        cat "$S/deploy_inventory.txt"
        ;;
      configmap)
        jp=""
        for a in "$@"; do
          case "$a" in
            jsonpath=*) jp="${a#jsonpath=}" ;;
          esac
        done
        if [ -f "$S/patched.marker" ] && [ -f "$S/tamper" ]; then
          printf 'TAMPERED-READBACK\n'
        else
          cm_value "$jp"
        fi
        ;;
      namespace)
        exit 0
        ;;
      *)
        echo "shim: unsupported get '$sub'" >&2
        exit 1
        ;;
    esac
    ;;
  patch)
    pfile=""
    pinline=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --patch-file) pfile="${2:-}"; shift 2 ;;
        -p|--patch)   pinline="${2:-}"; shift 2 ;;
        *) shift ;;
      esac
    done
    if [ -n "$pfile" ]; then
      if [ -n "${SHIM_NO_PATCH_FILE:-}" ]; then
        echo "error: unknown flag: --patch-file" >&2
        exit 1
      fi
      cp "$pfile" "$S/patch.body.json"
    else
      printf '%s' "$pinline" > "$S/patch.body.json"
    fi
    printf '%s\n' "$(cat "$S/patch.body.json")" >> "$S/patch.log"
    python3 - "$S/cm.json" "$S/patch.body.json" <<'PY'
import json, sys
cm_path, patch_path = sys.argv[1], sys.argv[2]
with open(cm_path, encoding="utf-8") as fh:
    cm = json.load(fh)
with open(patch_path, encoding="utf-8") as fh:
    patch = json.load(fh)
for k, v in patch.get("data", {}).items():
    cm["data"][k] = v
with open(cm_path, "w", encoding="utf-8") as fh:
    json.dump(cm, fh, indent=2)
PY
    : > "$S/patched.marker"
    echo "configmap/pushed patched"
    ;;
  exec)
    interactive=0
    while [ $# -gt 0 ]; do
      case "$1" in
        -i|--stdin) interactive=1; shift ;;
        -n|--namespace) shift 2 ;;
        -c|--container) shift 2 ;;
        *) break ;;
      esac
    done
    target="${1:-}"
    [ $# -gt 0 ] && shift
    while [ $# -gt 0 ] && [ "$1" != "--" ]; do shift; done
    [ $# -gt 0 ] && shift
    script="$*"
    n=$(cat "$S/exec.log" 2>/dev/null | wc -l)
    n=$((n + 1))
    printf '%s|%s|%s\n' "$target" "$script" "$interactive" >> "$S/exec.log"
    if [ "$interactive" -eq 1 ]; then
      cat > "$S/stdin/$n.bin"
    fi
    exit 0
    ;;
  *)
    echo "shim: unsupported command '$cmd'" >&2
    exit 1
    ;;
esac
exit 0
SHIM_EOF
chmod +x "$BIN/kubectl"

# ------------------------------------------------------------------ fixtures
cat > "$SHIM/cm.initial.json" <<'JSON'
{
  "apiVersion": "v1",
  "kind": "ConfigMap",
  "metadata": {"name": "openwork-web-helm-project-config"},
  "data": {
    "openwork-server.json": "{\"server\":{\"port\":8787}}\n",
    "opencode.json": "{\"legacy\":true}\n",
    "skill-demo.md": "# demo skill\n",
    "agent-reviewer.md": "reviewer agent\n"
  }
}
JSON

cat > "$SHIM/deploy_inventory.txt" <<'INV'
openwork-user-alice|alice|alice|
openwork-user-bob|bob|bob|
openwork-dind-carol|carol|carol|true
INV

cat > "$WORK/payload.json" <<'JSON'
{
  "model": "deepseek-v4",
  "providers": {"pcai": {"baseURL": "http://127.0.0.1:4000"}}
}
JSON

LIVE='/var/openwork/home/.config/opencode/opencode.json'
BACKUP='/var/openwork/home/.config/opencode/opencode-backup.json'

# ------------------------------------------------------------------- helpers
PASS=0
FAIL=0
ok()  { printf 'ok   %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf 'FAIL %s\n' "$1"; FAIL=$((FAIL + 1)); }

RC=0
OUT=""
ERR=""

run_push() {
  RC=0
  OUT="$(SHIM_DIR="$SHIM" PATH="$BIN:$PATH" KUBECTL=kubectl "$PUSH" "$@" 2>"$WORK/stderr")" || RC=$?
  ERR="$(cat "$WORK/stderr")"
  printf '=== push %s (rc=%s) ===\n%s\n--- stderr ---\n%s\n' "$*" "$RC" "$OUT" "$ERR" > "$WORK/last.txt"
}

reset_state() {
  cp "$SHIM/cm.initial.json" "$SHIM/cm.json"
  rm -f "$SHIM/patch.log" "$SHIM/exec.log" "$SHIM/argv.log" \
        "$SHIM/patch.body.json" "$SHIM/patched.marker" "$SHIM/tamper"
  rm -rf "$SHIM/stdin"
  mkdir -p "$SHIM/stdin"
}

patch_count() { if [ -f "$SHIM/patch.log" ]; then wc -l < "$SHIM/patch.log" | tr -d ' '; else echo 0; fi; }
exec_count()  { if [ -f "$SHIM/exec.log" ]; then wc -l < "$SHIM/exec.log" | tr -d ' '; else echo 0; fi; }

assert_rc() {
  if [ "$RC" -eq "$2" ]; then ok "$1"; else bad "$1 (rc=$RC, want $2)"; sed 's/^/     | /' "$WORK/last.txt"; fi
}
assert_contains() {
  case "$1" in
    *"$2"*) ok "$3" ;;
    *) bad "$3 — missing: $2"; printf '%s\n' "$1" | sed 's/^/     | /' ;;
  esac
}
assert_eq() {
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (got '$2', want '$3')"; sed 's/^/     | /' "$WORK/last.txt"; fi
}

# cm_key_is KEY FILE -> 0 when the fixture's data[KEY] is byte-identical to FILE
cm_key_is() {
  python3 - "$SHIM/cm.json" "$1" "$2" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    cm = json.load(fh)
got = cm["data"].get(sys.argv[2])
with open(sys.argv[3], encoding="utf-8") as fh:
    want = fh.read()
sys.exit(0 if got == want else 1)
PY
}

echo "== push script: $PUSH"
echo "== shim dir:   $SHIM"
echo

# ---------------------------------------------------------------------- T1
# 1. dry run executes nothing and describes BOTH phases separately.
reset_state
run_push --to-router --file "$WORK/payload.json"
assert_rc "T1 dry-run --to-router --file exits 0" 0
assert_contains "$OUT" "configmap: openwork-web-helm-project-config" "T1 dry-run names the canonical ConfigMap"
assert_contains "$OUT" "namespace: openwork-web-helm" "T1 dry-run names the RELEASE namespace"
assert_contains "$OUT" "data key:  opencode.json" "T1 dry-run names the single data key"
assert_contains "$OUT" "sha256:" "T1 dry-run prints the payload sha256"
assert_contains "$OUT" "would merge-patch opencode.json (dry run - nothing executed)" "T1 dry-run describes the canonical write"
assert_contains "$OUT" "phase 2/2 - fan-out to user units" "T1 dry-run describes the fan-out separately"
assert_contains "$OUT" " - openwork-user-alice" "T1 dry-run lists the units"
assert_eq "T1 dry-run performed no patch" "$(patch_count)" "0"
assert_eq "T1 dry-run performed no exec" "$(exec_count)" "0"
if python3 - "$SHIM/cm.json" <<'PY'
import json, sys
cm = json.load(open(sys.argv[1], encoding="utf-8"))
sys.exit(0 if cm["data"]["opencode.json"] == '{"legacy":true}\n' else 1)
PY
then ok "T1 dry-run left the canonical ConfigMap untouched"; else bad "T1 dry-run modified the ConfigMap"; fi
echo

# ---------------------------------------------------------------------- T2
# 2. --to-router --router-only --apply patches the CM, ZERO exec calls.
reset_state
run_push --to-router --router-only --file "$WORK/payload.json" --apply
assert_rc "T2 --to-router --router-only --apply exits 0" 0
assert_eq "T2 patched the ConfigMap once" "$(patch_count)" "1"
assert_eq "T2 performed ZERO exec calls" "$(exec_count)" "0"
assert_contains "$OUT" "readback verified byte-identical" "T2 verified the readback"
assert_contains "$OUT" "no pod contacted (--router-only)" "T2 reports that no pod was contacted"
if cm_key_is opencode.json "$WORK/payload.json"; then ok "T2 the canonical key now holds the payload"; else bad "T2 canonical key mismatch"; fi
echo

# ---------------------------------------------------------------------- T3
# 3. --to-router --file X --apply = canonical write THEN one exec per unit.
reset_state
run_push --to-router --file "$WORK/payload.json" --apply
assert_rc "T3 --to-router --file --apply exits 0" 0
assert_eq "T3 patched the ConfigMap once" "$(patch_count)" "1"
assert_eq "T3 exec'd once per unit (broker skipped)" "$(exec_count)" "2"
assert_contains "$OUT" "phase 1/2 - canonical ConfigMap (--to-router)" "T3 reports phase 1"
assert_contains "$OUT" "phase 2/2 - fan-out to user units" "T3 reports phase 2"
if python3 - "$SHIM/exec.log" <<'PY'
import sys
rows = [l.rstrip("\n").split("|", 2) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
assert len(rows) == 2, rows
for tgt, script, inter in rows:
    assert tgt.startswith("deploy/openwork-user-"), tgt
    assert "opencode-update.json" in script, script
    assert inter == "1", inter
PY
then ok "T3 every exec wrote the STAGED sibling with stdin"; else bad "T3 exec payload/filename wrong"; sed 's/^/     | /' "$SHIM/exec.log"; fi
if python3 - "$SHIM/stdin" "$WORK/payload.json" <<'PY'
import os, sys
d = sys.argv[1]
with open(sys.argv[2], "rb") as fh:
    payload = fh.read()
names = sorted(n for n in os.listdir(d) if n.endswith(".bin"))
assert len(names) == 2, names
for n in names:
    with open(os.path.join(d, n), "rb") as fh:
        assert fh.read() == payload, n
PY
then ok "T3 stdin to every pod is byte-identical to --file"; else bad "T3 stdin payload differs from --file"; fi
echo

# ---------------------------------------------------------------------- T4
# 4a. the staged default NEVER writes the live filename.
reset_state
run_push --file "$WORK/payload.json" --apply
assert_rc "T4a staged push exits 0" 0
assert_eq "T4a exec'd once per unit" "$(exec_count)" "2"
assert_contains "$OUT" "staged (opencode-update.json)" "T4a reports the staged filename"
if python3 - "$SHIM/exec.log" "$LIVE" "$BACKUP" <<'PY'
import sys
rows = [l.rstrip("\n").split("|", 2) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
live, backup = sys.argv[2], sys.argv[3]
for tgt, script, inter in rows:
    assert "opencode-update.json" in script, script
    assert backup not in script, "staged push must not take a backup: " + script
    assert "mv -f" not in script, "staged push must not mv anything: " + script
    assert ("cat > " + live) not in script, "staged push must not write the live file: " + script
PY
then ok "T4a staged push never touches the live file (no backup, no mv)"; else bad "T4a staged push touched the live file"; sed 's/^/     | /' "$SHIM/exec.log"; fi

# 4b. --live: backup FIRST, then temp file, then atomic mv.
reset_state
run_push --file "$WORK/payload.json" --live --apply
assert_rc "T4b --live --apply exits 0" 0
assert_eq "T4b exec'd once per unit" "$(exec_count)" "2"
assert_contains "$ERR" "!!! WARNING: --live TARGETS THE LIVE CONFIG" "T4b prints the loud live warning"
assert_contains "$ERR" "openwork-user-alice" "T4b the warning lists the units"
assert_contains "$OUT" "LIVE config replaced (backup: opencode-backup.json)" "T4b reports the live replacement"
if python3 - "$SHIM/exec.log" "$LIVE" "$BACKUP" <<'PY'
import sys
rows = [l.rstrip("\n").split("|", 2) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
live, backup = sys.argv[2], sys.argv[3]
assert len(rows) == 2, rows
for tgt, script, inter in rows:
    b = script.find("cp -p %s %s" % (live, backup))
    w = script.find("cat > %s.tmp" % live)
    m = script.find("mv -f %s.tmp %s" % (live, live))
    assert b != -1, "no rolling backup: " + script
    assert w != -1, "no temp-file write: " + script
    assert m != -1, "no atomic mv: " + script
    assert b < w < m, "backup must come BEFORE the live write: " + script
    assert ("cat > " + live + " ") not in script, "live file truncated in place: " + script
PY
then ok "T4b --live backs up first, writes a temp file, then mv (never in place)"; else bad "T4b --live ordering/atomicity wrong"; sed 's/^/     | /' "$SHIM/exec.log"; fi

# 4c. --live without --apply writes nothing.
reset_state
run_push --file "$WORK/payload.json" --live
assert_rc "T4c --live dry-run exits 0" 0
assert_eq "T4c --live dry-run performed no exec" "$(exec_count)" "0"
assert_contains "$ERR" "!!! WARNING: --live TARGETS THE LIVE CONFIG" "T4c --live dry-run still warns"
assert_contains "$OUT" "OVERWRITES THE LIVE CONFIG" "T4c --live dry-run marks the target"
echo

# ---------------------------------------------------------------------- T5
# 5. the merge patch carries ONLY our key; sibling keys survive untouched.
reset_state
run_push --to-router --router-only --file "$WORK/payload.json" --apply
assert_rc "T5 canonical write exits 0" 0
if python3 - "$SHIM/patch.log" <<'PY'
import json, sys
lines = [l for l in open(sys.argv[1], encoding="utf-8").read().split("\n") if l.strip()]
assert len(lines) == 1, lines
patch = json.loads(lines[0])
assert set(patch.keys()) == {"data"}, patch.keys()
assert set(patch["data"].keys()) == {"opencode.json"}, patch["data"].keys()
PY
then ok "T5 merge patch JSON contains ONLY data['opencode.json']"; else bad "T5 merge patch JSON has extra keys"; sed 's/^/     | /' "$SHIM/patch.log"; fi
if python3 - "$SHIM/cm.initial.json" "$SHIM/cm.json" <<'PY'
import json, sys
a = json.load(open(sys.argv[1], encoding="utf-8"))["data"]
b = json.load(open(sys.argv[2], encoding="utf-8"))["data"]
for k in ("openwork-server.json", "skill-demo.md", "agent-reviewer.md"):
    assert a[k] == b[k], "sibling key %s changed" % k
assert set(a) == set(b), (set(a), set(b))
PY
then ok "T5 sibling keys (server json, skill-*, agent-*) survived unchanged"; else bad "T5 a sibling key was touched"; fi
echo

# ---------------------------------------------------------------------- T6
# 6. a readback mismatch fails the run (and blocks the fan-out).
reset_state
: > "$SHIM/tamper"
run_push --to-router --file "$WORK/payload.json" --apply
if [ "$RC" -ne 0 ]; then ok "T6 readback mismatch exits non-zero (rc=$RC)"; else bad "T6 readback mismatch did NOT fail the run"; fi
assert_contains "$ERR" "READBACK MISMATCH" "T6 readback mismatch is reported clearly"
assert_eq "T6 no fan-out after a failed canonical write" "$(exec_count)" "0"
echo

# ---------------------------------------------------------------------- T7
# 7. --to-router without --file is an error.
reset_state
run_push --to-router --apply
if [ "$RC" -ne 0 ]; then ok "T7 --to-router without --file exits non-zero (rc=$RC)"; else bad "T7 --to-router without --file exited 0"; fi
assert_contains "$ERR" "--to-router requires --file" "T7 says --to-router requires --file"
assert_eq "T7 nothing patched" "$(patch_count)" "0"
assert_eq "T7 nothing exec'd" "$(exec_count)" "0"
echo

# ---------------------------------------------------------------------- T8
# 8. idempotency: a second identical canonical write patches nothing.
reset_state
run_push --to-router --router-only --file "$WORK/payload.json" --apply
run_push --to-router --router-only --file "$WORK/payload.json" --apply
assert_rc "T8 second identical --to-router exits 0" 0
assert_eq "T8 second run patched nothing (idempotent)" "$(patch_count)" "1"
assert_contains "$OUT" "already up to date" "T8 reports 'already up to date'"
echo

# ---------------------------------------------------------------------- T9
# 9. kubectl without --patch-file: the -p fallback still applies the patch.
reset_state
SHIM_NO_PATCH_FILE=1
run_push --to-router --router-only --file "$WORK/payload.json" --apply
unset SHIM_NO_PATCH_FILE
assert_rc "T9 -p fallback exits 0" 0
assert_eq "T9 -p fallback patched once" "$(patch_count)" "1"
assert_contains "$OUT" "falling back to -p" "T9 reports the -p fallback"
echo

# --------------------------------------------------------------------- T10
# 10. flag misuse is rejected.
reset_state
run_push --router-only --file "$WORK/payload.json" --apply
if [ "$RC" -ne 0 ]; then ok "T10a --router-only without --to-router exits non-zero"; else bad "T10a --router-only alone exited 0"; fi
assert_contains "$ERR" "--router-only requires --to-router" "T10a says --router-only requires --to-router"
reset_state
run_push --to-router --file "$WORK/payload.json" --from-configmap --apply
if [ "$RC" -ne 0 ]; then ok "T10b --to-router + --from-configmap exits non-zero"; else bad "T10b mutually exclusive sources accepted"; fi
assert_contains "$ERR" "mutually exclusive" "T10b says the sources are mutually exclusive"
echo

# --------------------------------------------------------------------- T11
# 11. the canonical CM is written in the RELEASE namespace, the fan-out in the
#     per-user one (the project-user-* trap).
reset_state
run_push --to-router --namespace project-user-alice --file "$WORK/payload.json" --apply
assert_rc "T11 --namespace project-user-alice --to-router exits 0" 0
if python3 - "$SHIM/argv.log" <<'PY'
import sys
lines = [l for l in open(sys.argv[1], encoding="utf-8").read().split("\n") if l.strip()]
patches = [l for l in lines if l.startswith("patch ")]
execs = [l for l in lines if l.startswith("exec ")]
assert patches, lines
assert all("-n openwork-web-helm" in l for l in patches), patches
assert execs and all("-n project-user-alice" in l for l in execs), execs
PY
then ok "T11 CM patched in openwork-web-helm, pods exec'd in project-user-alice"; else bad "T11 namespace split wrong"; sed 's/^/     | /' "$SHIM/argv.log"; fi
echo

# --------------------------------------------------------------------- T12
# 12. TARGET_NAME=opencode.json still works but is deprecated.
reset_state
RC=0
OUT="$(SHIM_DIR="$SHIM" PATH="$BIN:$PATH" KUBECTL=kubectl TARGET_NAME=opencode.json \
       "$PUSH" --file "$WORK/payload.json" 2>"$WORK/stderr")" || RC=$?
ERR="$(cat "$WORK/stderr")"
assert_rc "T12 TARGET_NAME=opencode.json (dry-run) exits 0" 0
assert_contains "$ERR" "DEPRECATED" "T12 warns that TARGET_NAME=opencode.json is deprecated"
assert_contains "$OUT" "OVERWRITES THE LIVE CONFIG" "T12 TARGET_NAME=opencode.json still selects the live file"
echo

# --------------------------------------------------------------------- help
HELP="$("$PUSH" --help 2>&1)"
assert_contains "$HELP" "--to-router" "help documents --to-router"
assert_contains "$HELP" "--router-only" "help documents --router-only"
assert_contains "$HELP" "--live" "help documents --live"
assert_contains "$HELP" "per-key merge patch" "help documents the merge-patch semantics"
echo

# ------------------------------------------------------------------ summary
echo "-------------------------------------------"
echo "passed: $PASS   failed: $FAIL"
if [ "$FAIL" -ne 0 ]; then
  exit 1
fi
echo "ALL TESTS PASSED"
exit 0
