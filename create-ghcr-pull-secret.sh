#!/usr/bin/env bash
# Create/update a Kubernetes docker-registry secret to pull a PRIVATE container
# image into your cluster. Works for any registry (GitHub Container Registry,
# Docker Hub, a self-hosted registry, etc.).
#
# Run ONCE per namespace that needs to pull the private image.
#
# Usage:
#   tools/create-ghcr-pull-secret.sh --username <USER> --password <TOKEN>
#   tools/create-ghcr-pull-secret.sh --namespace my-ns --username <USER> --password <TOKEN>
#   tools/create-ghcr-pull-secret.sh --server my-registry.example.com --secret my-pull --username ...
set -euo pipefail

NAMESPACE="$(kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}' 2>/dev/null)"
SERVER="ghcr.io"
USERNAME=""
PASSWORD=""
SECRET="ghcr-pull"

usage() {
  echo "Usage: $0 --username <USER> --password <TOKEN> [--namespace <ns>] [--server <registry>] [--secret <name>]"
  echo "  --username  registry username (required)"
  echo "  --password  registry token/PAT (required)"
  echo "  --namespace k8s namespace (default: current context)"
  echo "  --server    registry host (default: ghcr.io)"
  echo "  --secret    secret name (default: ghcr-pull)"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --server)    SERVER="$2";     shift 2 ;;
    --username)  USERNAME="$2";   shift 2 ;;
    --password)  PASSWORD="$2";   shift 2 ;;
    --secret)    SECRET="$2";     shift 2 ;;
    -h|--help)   usage ;;
    *) echo "Unknown argument: $1"; usage ;;
  esac
done

[ -n "$USERNAME" ] || { echo "ERROR: --username is required"; usage; }
[ -n "$PASSWORD" ] || { echo "ERROR: --password is required"; usage; }

args=(create secret docker-registry "$SECRET"
      --docker-server="$SERVER" --docker-username="$USERNAME" --docker-password="$PASSWORD"
      --dry-run=client -o yaml)
[[ -n "$NAMESPACE" ]] && args+=(-n "$NAMESPACE")

kubectl "${args[@]}" | kubectl apply -f -
echo "Created/updated secret '$SECRET'${NAMESPACE:+ in namespace '$NAMESPACE'} for registry '$SERVER'."
echo "In Helm values: imagePullSecrets: [\"$SECRET\"]"