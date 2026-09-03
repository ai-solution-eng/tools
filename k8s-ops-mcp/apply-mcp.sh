##
# Specify the domain name below for the MCP server. 
# E.g. pcai-se-ai-application.hst.rdlabs.hpecorp.net
##
export DOMAIN_NAME=""
export NAMESPACE=k8s-mcp-ops
export SUBDOMAIN_NAME=k8s-mcp-2-0-server
export K8S_MCP_BLOCKED_NAMESPACES="kube-system,kube-public,k8s-mcp-ops"
export K8S_MCP_EXEC_ENABLED=true
export K8S_MCP_EXEC_NAMESPACES="*"
export K8S_MCP_EXEC_REQUIRE_LABEL=false
export MCP_HOSTNAME=${SUBDOMAIN_NAME}.${DOMAIN_NAME}


if [ -z "$DOMAIN_NAME" ]; then
  echo "Error: DOMAIN_NAME is not set. Please edit this file and update the domain name." >&2
  exit 1
fi

kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || kubectl create namespace "${NAMESPACE}"


if ! kubectl -n "${NAMESPACE}" get secret k8s-mcp-2-0-apikey >/dev/null 2>&1; then
  openssl rand -hex 32 > /tmp/api-key
  kubectl -n "${NAMESPACE}" create secret generic k8s-mcp-2-0-apikey \
    --from-literal="api-key=$(cat /tmp/api-key)" \
    --dry-run=client -o yaml | kubectl apply -f -
  rm -f /tmp/api-key
fi


envsubst < k8s-mcp-2-0-server.yaml | kubectl apply -f -

kubectl -n $NAMESPACE rollout status deploy/k8s-mcp-2-0-server

echo "This is the bearer token"
kubectl -n ${NAMESPACE} get secret k8s-mcp-2-0-apikey -o jsonpath='{.data.api-key}' | base64 -d
export BEARER_TOKEN=$(kubectl -n ${NAMESPACE} get secret k8s-mcp-2-0-apikey -o jsonpath='{.data.api-key}' | base64 -d)

echo "Use this in opencode:

{
  \"mcpServers\": {
    \"k8s-ops-mcp\": {
      \"type\": \"remote\",
      \"enabled\": true,
      \"url\": \"https://${SUBDOMAIN_NAME}.${DOMAIN_NAME}/mcp\",
      \"headers\": { \"Authorization\": \"Bearer ${BEARER_TOKEN}\" }
    }
  }
}

Or for restricted exec:

{
  \"mcpServers\": {
    \"k8s-ops-mcp\": {
      \"type\": \"remote\",
      \"enabled\": true,
      \"url\": \"https://${SUBDOMAIN_NAME}.${DOMAIN_NAME}/mcp\",
      \"headers\": { \"Authorization\": \"Bearer ${BEARER_TOKEN}\",
      \"X-Exec-Namespaces\": \"comma,separated,list,of,Namespaces,where,it,is,allowed,to,exec\" }
    }
  }
}
"
