#!/usr/bin/env bash
# deploy_unitresources_fix.sh — ship the "every unit container declares
# requests+limits" fix (dsh 0.4.46 / opencode 1.2.36 / openwork 0.1.35).
#
# WHY: on a cluster whose project-user-* namespaces carry a ResourceQuota with
# limits.cpu/limits.memory, Kubernetes rejects the whole unit ReplicaSet if ANY
# container omits them — init containers included. The router-generated pod had
# 'resources: {}' on build-ca-bundle, init-<app>-storage, <app>-preview-proxy and
# <app>-auth, so SSO units never got a pod and the router timed out after 300 s.
#
# SAFETY CONTRACT (nothing here deletes user data):
#   * the chart upload only ADDS a chart version to ChartMuseum;
#   * the EzAppConfig apply is an in-place helm UPGRADE of the same release
#     (spec.name unchanged), so PVCs, workspaces and sessions are preserved;
#   * keepPvcOnDelete stays true; no helm uninstall, no PVC delete, no
#     storage.nameVersion bump, no --force/--recreate;
#   * the RUNTIME_REV bump only makes the router re-PATCH each unit Deployment
#     (Recreate strategy) — pods are replaced, their PVCs are not.
#
# USAGE
#   export EZAPP_DEPLOY_KEY=...            # bearer key for the ezapp-deploy MCP
#   export EZAPP_DEPLOY_URL=https://...    # its base URL (no /mcp suffix)
#   ./scripts/deploy_unitresources_fix.sh              # dry run: shows the plan
#   ./scripts/deploy_unitresources_fix.sh --apply      # upload + apply
#
#   # If EZAPP_DEPLOY_URL is not reachable from this host, use a port-forward:
#   kubectl -n ezapp-deploy-mcp port-forward svc/ezapp-deploy-service 19090:9090 &
#   export EZAPP_DEPLOY_URL=http://127.0.0.1:19090
set -eo pipefail

# repo root: this script lives in <repo>/deepseek-harness/scripts/
here=$(cd "$(dirname "$0")/../.." && pwd)
apply=0
if [ $# -gt 0 ] && [ "$1" = "--apply" ]; then apply=1; fi

echo "== 1. chart render check =="
check_chart() { # dir release version
  local dir=$1 rel=$2 ver=$3 n
  if ! helm template verify "$here/$dir" -n "$rel" --set ezua.domainName=example.com >/tmp/_r.yaml 2>/tmp/_r.err; then
    printf '   FAIL %s: %s\n' "$rel" "$(tail -2 /tmp/_r.err)"; exit 1
  fi
  n=$(grep -c 'resources: unitResources\.' /tmp/_r.yaml || true)
  if [ "$n" -ge 4 ]; then
    printf '   ok   %s %s renders, %s containers declare resources\n' "$rel" "$ver" "$n"
  else
    printf '   FAIL %s: only %s resource refs\n' "$rel" "$n"; exit 1
  fi
}
check_chart deepseek-harness dsh-web-helm 0.4.46
check_chart opencode opencode-web-helm 1.2.36
check_chart openwork openwork-web-helm 0.1.35

echo "== 2. packages =="
show_pkg() { # dir name version
  local tgz="$here/$1/$2-$3.tgz"
  [ -f "$tgz" ] || helm package "$here/$1" -d "$here/$1" >/dev/null
  printf '   %s (%s bytes, sha256 %s)\n' "$tgz" "$(wc -c <"$tgz" | tr -d ' ')" "$(sha256sum "$tgz" | cut -d' ' -f1)"
}
show_pkg deepseek-harness dsh-web-helm 0.4.46
show_pkg opencode opencode-web-helm 1.2.36
show_pkg openwork openwork-web-helm 0.1.35

if [ "$apply" -eq 0 ]; then
  echo "== DRY RUN =="
  echo "   would upload the three packages to \$EZAPP_DEPLOY_URL/upload, then"
  echo "   kubectl apply -f the three EzAppConfig CRs (in-place upgrade):"
  echo "     ezappconfig-dsh-web-helm-0.4.46.yaml"
  echo "     ezappconfig-opencode-web-helm-1.2.36.yaml"
  echo "     ezappconfig-openwork-web-helm-0.1.35.yaml"
  echo "   re-run with --apply"
  exit 0
fi

if [ -z "$EZAPP_DEPLOY_KEY" ] || [ -z "$EZAPP_DEPLOY_URL" ]; then
  echo "error: export EZAPP_DEPLOY_KEY and EZAPP_DEPLOY_URL first" >&2
  exit 1
fi

echo "== 3. upload to ChartMuseum =="
upload_pkg() { # dir name version
  local tgz="$here/$1/$2-$3.tgz" code
  code=$(curl -sS -m 300 -o /tmp/_up.json -w '%{http_code}' \
    -H "Authorization: Bearer $EZAPP_DEPLOY_KEY" \
    --data-binary @"$tgz" "$EZAPP_DEPLOY_URL/upload" || true)
  case "$code" in
    200|201) printf '   ok   %s-%s uploaded\n' "$2" "$3" ;;
    409)     printf '   ok   %s-%s already present\n' "$2" "$3" ;;
    *)       printf '   FAIL %s-%s upload http=%s: %s\n' "$2" "$3" "$code" "$(head -c 300 /tmp/_up.json)"; exit 1 ;;
  esac
}
upload_pkg deepseek-harness dsh-web-helm 0.4.46
upload_pkg opencode opencode-web-helm 1.2.36
upload_pkg openwork openwork-web-helm 0.1.35

echo "== 4. apply the CRs (in-place upgrade of the SAME release) =="
kubectl apply -f "$here/ezappconfig-dsh-web-helm-0.4.46.yaml"
kubectl apply -f "$here/ezappconfig-opencode-web-helm-1.2.36.yaml"
kubectl apply -f "$here/ezappconfig-openwork-web-helm-0.1.35.yaml"

echo "== 5. watch =="
cat <<'EOS'
   kubectl get ezappconfig | grep -E '0.4.46|1.2.36|0.1.35'
   # units re-stamp on the user's next resolve; verify the fix landed:
   kubectl -n project-user-<user> get deploy -o jsonpath='{range .spec.template.spec.initContainers[*]}{.name}{" "}{.resources}{"\n"}{end}'
   # expect NON-EMPTY resources on every line; a new pod appears within a minute.
   # PVCs must be untouched (same UID/volumeName):
   kubectl -n project-user-<user> get pvc
EOS
