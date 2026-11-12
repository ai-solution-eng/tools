#!/usr/bin/env bash
# Regression tests for apply-defaults.mjs (the unit-init OpenChamber profile fixup).
#
# Chart 1.2.25 wrote a literal backslash-n after every profile it touched, so
# settings.json / preferences.json on affected units are invalid JSON. The repair
# must fix them on disk even though they already carry reportUsage:false, and it
# must never overwrite a file it could not read.
#
#   ./scripts/test-apply-defaults.sh
set -u

here=$(cd "$(dirname "$0")/.." && pwd)
template="$here/templates/configmap-project.yaml"
work=$(mktemp -d)
trap 'chmod -R u+rwX "$work" 2>/dev/null; rm -rf "$work"' EXIT

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }

cat > "$work/extract.mjs" <<'JS'
import fs from 'node:fs'
const lines = fs.readFileSync(process.argv[2], 'utf8').split('\n')
const start = lines.findIndex((l) => l.trimStart().startsWith('apply-defaults.mjs: |'))
if (start < 0) { console.error('apply-defaults.mjs block not found'); process.exit(2) }
const first = lines.slice(start + 1).find((l) => l.trim() !== '')
if (!first) { console.error('apply-defaults.mjs block is empty'); process.exit(2) }
const indent = first.match(/^ */)[0].length
const out = []
for (let i = start + 1; i < lines.length; i++) {
  const l = lines[i]
  if (l.trim() === '') { out.push(''); continue }
  if (l.match(/^ */)[0].length < indent) break
  out.push(l.slice(indent))
}
process.stdout.write(out.join('\n'))
JS

script="$work/apply-defaults.mjs"
node "$work/extract.mjs" "$template" > "$script" || { echo "cannot extract apply-defaults.mjs"; exit 2; }
[ -s "$script" ] || { echo "apply-defaults.mjs extracted empty (template layout changed?)"; exit 2; }
node --check "$script" || { echo "apply-defaults.mjs does not parse"; exit 2; }

cfg="$work/config"
fresh() { rm -rf "$cfg"; mkdir -p "$cfg/openchamber"; }
# a profile exactly as chart 1.2.25 leaves it: valid JSON + a literal backslash-n
broken() {
  printf '{\n  "fields": {\n    "reportUsage": { "value": false },\n    "theme": { "value": "%s" }\n  }\n}\\n' "$1" > "$cfg/openchamber/preferences.json"
  printf '{\n  "securityScopedBookmarks": [],\n  "reportUsage": false\n}\\n' > "$cfg/openchamber/settings.json"
}
run() {
  CONFIG_HOME="$cfg" OPENCHAMBER_REPORT_USAGE="${1:-false}" node "$script" > "$work/log" 2>&1
  status=$?
}
valid() { node -e 'JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"))' "$1" 2>/dev/null; }

echo "apply-defaults regression tests"

echo "1. fresh home -> settings.json created with reportUsage:false"
fresh; run false
[ "$status" = 0 ] && pass "exits 0" || bad "exit=$status"
[ -f "$cfg/openchamber/settings.json" ] && valid "$cfg/openchamber/settings.json" \
  && pass "settings.json is valid JSON" || bad "settings.json missing or invalid"

echo "2. 1.2.25-broken profiles -> repaired on disk, user fields kept"
fresh; broken dark; run false
[ "$status" = 0 ] && pass "exits 0" || bad "exit=$status"
valid "$cfg/openchamber/preferences.json" && pass "preferences.json is valid JSON now" || bad "preferences.json still invalid"
valid "$cfg/openchamber/settings.json" && pass "settings.json is valid JSON now" || bad "settings.json still invalid"
grep -q '"dark"' "$cfg/openchamber/preferences.json" && pass "user theme preserved" || bad "user theme lost"
grep -q 'repaired' "$work/log" && pass "repair is logged" || bad "repair not logged"

echo "3. repair still happens when reportUsage enforcement is off"
fresh; broken light; run true
valid "$cfg/openchamber/preferences.json" && pass "preferences.json repaired" || bad "preferences.json still invalid"

echo "4. unreadable settings.json -> left untouched, not replaced"
fresh; broken dark; chmod 000 "$cfg/openchamber/settings.json"
before=$(stat -c %s "$cfg/openchamber/settings.json")
run false
chmod 600 "$cfg/openchamber/settings.json"
after=$(stat -c %s "$cfg/openchamber/settings.json")
[ "$before" = "$after" ] && pass "settings.json not clobbered ($after bytes)" || bad "settings.json was rewritten ($before -> $after bytes)"

echo "5. broken preferences.json without a 'fields' object -> rewritten as valid JSON"
fresh
printf '{\n  "theme": "dark"\n}\\n' > "$cfg/openchamber/preferences.json"
run false
valid "$cfg/openchamber/preferences.json" && pass "preferences.json is valid JSON" || bad "preferences.json still invalid"

echo "6. unparseable profile -> moved aside (never deleted), replaced with valid JSON"
fresh
: > "$cfg/openchamber/settings.json"
printf '{"theme":"dark"}\n' > "$cfg/openchamber/preferences.json"
run false
[ -n "$(ls "$cfg/openchamber"/settings.json.broken-* 2>/dev/null)" ] \
  && pass "settings.json moved aside" || bad "settings.json not quarantined"
valid "$cfg/openchamber/settings.json" && pass "settings.json replaced with valid JSON" || bad "settings.json missing/invalid"
grep -q 'moved unparseable' "$work/log" && pass "quarantine logged" || bad "quarantine not logged"
valid "$cfg/openchamber/preferences.json" && pass "preferences.json still valid" || bad "preferences.json invalid"

echo
if [ "$fail" -eq 0 ]; then echo "all apply-defaults tests passed"; exit 0; fi
echo "$fail apply-defaults test(s) failed"; exit 1
