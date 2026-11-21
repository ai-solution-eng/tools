#!/usr/bin/env bash
# Run the RENDERED recovery payload in a real shell, against fake mounts.
#
# This is the test that was missing: three successive space-check bugs (silent
# skip, wrapped df, heredoc-expanded zero) all passed 'bash -n' and passed a stub
# that never executed the payload. This extracts the payload from the manifest the
# script actually generates and runs it under alpine.
set -uo pipefail
SCRIPT=${1:-/mnt/shared/califra-dsh/tools/openwork/scripts/recover-legacy-shared.sh}
export PATH=/tmp/stub:$PATH
export DOCKER_CONFIG=${DOCKER_CONFIG:-/mnt/shared/califra-dsh/tools/.dsh/docker-config}
pass=0; fail=0
ok() { pass=$((pass+1)); printf 'PASS  %s\n' "$1"; }
no() { fail=$((fail+1)); printf 'FAIL  %s\n      got [%s] want [%s]\n' "$1" "$2" "$3"; }

payload() {
  bash "$SCRIPT" --namespace project-user-francesco-caliva 2>/dev/null \
    | sed -n '/^            - |$/,/^          volumeMounts:$/p' \
    | sed '1d;$d' | sed 's/^              //' \
    | sed 's|^SRC=/legacy$|SRC=/tmp/legacy|; s|^DSTROOT=/personal$|DSTROOT=/tmp/personal|'
}

P=$(payload)
[ -n "$P" ] && ok "payload extracted from the rendered manifest" || no "payload extracted" "empty" "non-empty"
run() { docker run --rm -i alpine:3.20 sh -s 2>&1; }

out=$(printf 'set -e\nmkdir -p /tmp/legacy/sub /tmp/personal\nprintf AAAA > /tmp/legacy/a.txt\nprintf BBBB > /tmp/legacy/sub/b.txt\nchown -R 1000:1000 /tmp/personal\n%s\n' "$P" | run)
case "$out" in *verdict=moved*) ok "fresh copy -> verdict=moved" ;; *) no "fresh copy -> moved" "$out" "verdict=moved" ;; esac
case "$out" in *"owner=1000:1000"*) ok "chowned to the personal owner" ;; *) no "chown" "$out" "owner=1000:1000" ;; esac

swapped=$(printf 'set -e\nmkdir -p /tmp/legacy /tmp/personal\nprintf AAAA > /tmp/legacy/a.txt\nprintf BBBB > /tmp/legacy/b.txt\nchown -R 1000:1000 /tmp/personal\ncp() { /bin/cp "$@"; if [ -f /tmp/personal/shared-legacy.partial/a.txt ]; then t=$(mktemp); /bin/cp /tmp/personal/shared-legacy.partial/a.txt "$t"; /bin/cp /tmp/personal/shared-legacy.partial/b.txt /tmp/personal/shared-legacy.partial/a.txt; /bin/cp "$t" /tmp/personal/shared-legacy.partial/b.txt; rm -f "$t"; fi; }\n%s\n' "$P" | run)
case "$swapped" in *verdict=verify-failed*) ok "same-size swap -> verify-failed" ;; *) no "same-size swap" "$swapped" "verify-failed" ;; esac

rooted=$(printf 'set -e\nmkdir -p /tmp/legacy /tmp/personal\nprintf x > /tmp/legacy/f\nchown 0:0 /tmp/personal\n%s\n' "$P" | run)
case "$rooted" in *verdict=refused-root-owned*) ok "root-owned personal -> refused" ;; *) no "root-owned personal" "$rooted" "refused-root-owned" ;; esac

again=$(printf 'set -e\nmkdir -p /tmp/legacy /tmp/personal\nprintf x > /tmp/legacy/f\nchown -R 1000:1000 /tmp/personal\n%s\n%s\n' "$P" "$P" | run)
case "$again" in *verdict=already-migrated*) ok "re-run -> already-migrated (idempotent)" ;; *) no "re-run idempotent" "$again" "already-migrated" ;; esac

emptyv=$(printf 'set -e\nmkdir -p /tmp/legacy /tmp/personal\nchown -R 1000:1000 /tmp/personal\n%s\n' "$P" | run)
case "$emptyv" in *verdict=legacy-empty*) ok "empty legacy -> legacy-empty" ;; *) no "empty legacy" "$emptyv" "legacy-empty" ;; esac

echo "----"
echo "PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
