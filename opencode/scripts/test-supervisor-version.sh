#!/usr/bin/env bash
# Regression test for binaryVersion() in opencode_supervisor.mjs.
#
# The supervisor logs the resolved opencode/openchamber *versions* so a drifted
# unit is visible without exec'ing into it. npm links bin/<name> into
# ../lib/node_modules/<pkg>/, so realpathSync lands on the package's own bin
# script -- a fixed-depth dirname(dirname()) resolves to the wrong directory and
# the feature silently logs "unknown" in exactly the drifted states it exists for.
#
#   ./scripts/test-supervisor-version.sh
set -u

here=$(cd "$(dirname "$0")/.." && pwd)
template="$here/templates/configmap-preview.yaml"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail + 1)); }

cat > "$work/extract.mjs" <<'JS'
import fs from 'node:fs'
const lines = fs.readFileSync(process.argv[2], 'utf8').split('\n')
const start = lines.findIndex((l) => l.trimStart().startsWith('opencode_supervisor.mjs: |'))
if (start < 0) { console.error('opencode_supervisor.mjs block not found'); process.exit(2) }
const first = lines.slice(start + 1).find((l) => l.trim() !== '')
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

cat > "$work/run.cjs" <<'JS'
const fs = require('node:fs')
const path = require('node:path')
const src = fs.readFileSync(process.argv[2], 'utf8')
const start = src.indexOf('function binaryVersion')
if (start < 0) { console.error('binaryVersion not found'); process.exit(2) }
let i = src.indexOf('{', start), depth = 0, end = -1
for (; i < src.length; i++) {
  if (src[i] === '{') depth++
  else if (src[i] === '}') { depth--; if (depth === 0) { end = i + 1; break } }
}
const fnSrc = src.slice(start, end)
const prefix = path.join(process.argv[3], 'prefix')
for (const [pkg, ver, bin] of [['opencode-ai', '1.19.4', 'opencode'], ['@openchamber/web', '1.24.2', 'openchamber']]) {
  const pkgDir = path.join(prefix, 'lib/node_modules', pkg)
  fs.mkdirSync(path.join(pkgDir, 'bin'), { recursive: true })
  fs.writeFileSync(path.join(pkgDir, 'package.json'), JSON.stringify({ version: ver }))
  fs.writeFileSync(path.join(pkgDir, 'bin', pkg.split('/').pop()), '#!/bin/sh\n')
  fs.mkdirSync(path.join(prefix, 'bin'), { recursive: true })
  const link = path.join(prefix, 'bin', bin)
  fs.rmSync(link, { force: true })
  fs.symlinkSync(path.relative(path.join(prefix, 'bin'), path.join(pkgDir, 'bin', pkg.split('/').pop())), link)
}
const binaryVersion = new Function('fs', 'path', fnSrc + '; return binaryVersion')(fs, path)
console.log(binaryVersion(path.join(prefix, 'bin/opencode'), 'opencode-ai'))
console.log(binaryVersion(path.join(prefix, 'bin/openchamber'), '@openchamber/web'))
console.log(binaryVersion('/nonexistent/opencode', 'opencode-ai'))
JS

node "$work/extract.mjs" "$template" > "$work/supervisor.mjs" || { echo "cannot extract supervisor"; exit 2; }
[ -s "$work/supervisor.mjs" ] || { echo "supervisor extracted empty"; exit 2; }
out=$(node "$work/run.cjs" "$work/supervisor.mjs" "$work") || { echo "binaryVersion test failed to run"; exit 2; }

echo "supervisor version-logging tests"
[ "$(echo "$out" | sed -n 1p)" = "1.19.4" ] && pass "scoped-free package resolves" || bad "opencode -> $(echo "$out" | sed -n 1p)"
[ "$(echo "$out" | sed -n 2p)" = "1.24.2" ] && pass "scoped package resolves" || bad "openchamber -> $(echo "$out" | sed -n 2p)"
[ "$(echo "$out" | sed -n 3p)" = "unknown" ] && pass "missing binary degrades to unknown" || bad "missing -> $(echo "$out" | sed -n 3p)"

echo
if [ "$fail" -eq 0 ]; then echo "all supervisor version tests passed"; exit 0; fi
echo "$fail supervisor version test(s) failed"; exit 1
