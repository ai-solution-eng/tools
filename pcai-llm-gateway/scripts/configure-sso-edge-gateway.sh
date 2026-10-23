#!/bin/bash
# configure-sso-edge-gateway.sh — step 3 of the SSO self-mint rollout
# (documentation/DEPLOYMENT.md § "SSO self-mint rollout (coupled, ordered)"
# and documentation/SSO-ROLLOUT-G2.md): chain the gateway's host through
# the PLATFORM oauth2-proxy at the ezaf-gateway, with the exact path
# classification, and verify it — the PCAI_LLM flavor of the fleet's
# configure-oidc-rag.sh / configure-oidc-sql.sh.
#
# ── WHY THIS SCRIPT IS DIFFERENT FROM RAG/SQL ─────────────────────────
# RAG and SQL run APP-SIDE OIDC: their charts embed an oauth2-proxy leg
# and each app registers its own /oauth/oidc/callback in the shared `ua`
# client. The gateway deliberately does NOT run app-side OIDC (DECISIONS
# "OAuth REVISION": OAuth terminates at the gateway; the gateway never
# issues, refreshes, or stores tokens). Its browser SSO is delivered by
# the PLATFORM oauth2-proxy as an envoy ext_authz endpoint:
#
#   verified live 2026-10-07 (ns oauth2-proxy, deploy oauth2-proxy):
#     --cookie-domain   =.pcai-se-ai-application.hst.rdlabs.hpecorp.net
#     --whitelist-domain=.pcai-se-ai-application.hst.rdlabs.hpecorp.net
#     --redirect-url    =https://oauth2-proxy.<domain>/oauth2/callback
#     alpha-config (cm oauth2-proxy-alpha): injectResponseHeaders =
#       Authorization: "Bearer <id_token>", X-Auth-Request-User (claim
#       user), X-Auth-Request-Groups, X-Auth-Request-Preferred-Username,
#       X-Refresh-Token; provider oidc-istio, clientID ua, issuer
#       https://keycloak.<domain>/realms/UA.
#
# Consequences (each verified, none needs doing again):
#   * NO Keycloak change for this host — the OAuth callback is the
#     PROXY'S OWN hostname and is registered once; cookie/whitelist
#     domains are already wildcards over the whole domain.
#   * NO per-app secret, client, or cookie config — the `ua` client
#     secret lives in the platform proxy's own config.
#   * 2026-10-08 CORRECTION (live-verified on G2): chaining is NOT an
#     EnvoyFilter — the only two envoyfilters in istio-system are platform
#     plumbing (local ratelimit + x-api-routes strip). The per-host chain is
#     an AuthorizationPolicy in istio-system: action CUSTOM, provider
#     oauth2-proxy, selector istio:ingressgateway, host+path-scoped in
#     rules[].to; UNMATCHED requests fall through unchained (that IS the
#     skip-auth half). Clone source: dsh-web-helm-auth-policy. The applied
#     pcai-llm manifest: .scratch/pcai-llm-edge-authz.yaml →
#     istio-system/pcai-llm-gateway-edge-chain. --attach lists these.
#
# ── PATH CLASSIFICATION (documentation/SSO-ROLLOUT-G2.md — LOAD-BEARING) ──
#   CHAINED (the proxy auths + injects headers):
#     /            (browser shell)          /ui
#     /v1/identity (whoami — identity IS the injected headers)
#     /v1/keys/for-me (mint — same)
#   SKIP-AUTH (the app's own bearer/X-API-Key is the control):
#     /v1/* (all other)   /mcp/*   /admin   /health   /ready   /metrics
#   ⚠️ NO blanket 302 (the MM RAG lesson): key-based REST clients must
#   reach the app without a browser session.
#
# ── MODES ──────────────────────────────────────────────────────────────
#   --verify (default)  read-only pre-flight + the behavioral chain
#                       oracle (the curl triple); safe anywhere, any time
#   --attach            locate + print the existing chained-host
#                       EnvoyFilter (RBAC permitting) and exit; the
#                       pcai-llm clone is generated from it once seen
#
# Run from a terminal with the cluster KUBECONFIG (the chaining objects
# live in istio-system; a read-only SA sees only the verify half).
# Requirements: kubectl, curl. NOT required: jq (no Keycloak API calls).

set -euo pipefail

GATEWAY_NS="${GATEWAY_NS:-pcai-llm}"
POLICY_NAME="${POLICY_NAME:-pcai-llm-gateway-authz}"
VS_NAME="${VS_NAME:-pcai-llm-gateway-vs}"
KUBECTL="${KUBECTL:-kubectl}"
CURL_HOST="${CURL_HOST:-}"   # override the probed host if needed

mode="${1:---verify}"

kc() { $KUBECTL "$@"; }

# ── shared pre-flight ──────────────────────────────────────────────────
# kubectl is OPTIONAL: with it, the release-state checks run; without it
# (no kubeconfig / no RBAC), the script degrades to the curl-triple oracle
# against CURL_HOST (or DOMAIN_NAME) — pure HTTP, works from anywhere.
DOMAIN_NAME="${DOMAIN_NAME:-$(kc get cm ezapp-manager-parameters -n ezapp-system \
  -o jsonpath='{..DOMAIN_NAME}' 2>/dev/null || true)}"
SKIPPED_KUBECONFIG=0
if [ -z "$DOMAIN_NAME" ]; then
  if [ -z "$CURL_HOST" ]; then
    echo "FAILED: no cluster access and no CURL_HOST/DOMAIN_NAME — nothing to probe."
    echo
    echo "  fish:   export KUBECONFIG=(realpath ~/SSH/G2/g2_kubeconfig.yaml)"
    echo "          export https_proxy=socks5://127.0.0.1:1080"
    echo "          ./scripts/configure-sso-edge-gateway.sh"
    echo
    echo "  bash:   export KUBECONFIG=\$HOME/SSH/G2/g2_kubeconfig.yaml https_proxy=socks5://127.0.0.1:1080"
    echo
    echo "  Cluster-free oracle: ./scripts/configure-sso-edge-gateway.sh --verify \\"
    echo "    with CURL_HOST=pcai-llm-gateway.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
    exit 1
  fi
  SKIPPED_KUBECONFIG=1
  echo "⚠️ kubectl unavailable — release-state checks skipped; running the chain oracle only"
fi
HOST="pcai-llm-gateway.${DOMAIN_NAME}"
[ -n "$CURL_HOST" ] && HOST="$CURL_HOST"
echo "host: ${HOST}"

echo
echo "── 1. release state (steps 1+2 must already be live) ──────────────"
if [ "$SKIPPED_KUBECONFIG" = "1" ]; then
  echo "  (skipped — no cluster access in this context)"
else
VS_HOST=$(kc get virtualservice "$VS_NAME" -n "$GATEWAY_NS" \
  -o jsonpath='{.spec.hosts[0]}' 2>/dev/null || true)
[ "$VS_HOST" = "$HOST" ] && echo "  ok: VS $VS_NAME serves $HOST" \
  || { echo "  FAIL: VS mismatch (got '${VS_HOST:-none}') — deploy first"; exit 1; }

POLICY=$(kc get authorizationpolicy "$POLICY_NAME" -n "$GATEWAY_NS" -o yaml 2>/dev/null || true)
echo "$POLICY" | grep -q 'paths:' && \
  echo "  ok: AuthorizationPolicy carries the metrics + in-cluster relief rules" \
  || echo "  ⚠️ policy lacks the path-scoped relief rules — the 2026-10-07 chart fixes are NOT applied (metrics + in-cluster consumers would be denied)"
fi

echo
echo "── 2. the behavioral chain oracle (the curl triple) ───────────────"
probe() { curl -sk --max-time 8 -o /dev/null -w '%{http_code}' "$1"; }
CODE_ROOT=$(probe "https://${HOST}/")
CODE_MODELS=$(probe "https://${HOST}/v1/models")
CODE_IDENTITY=$(probe "https://${HOST}/v1/identity")
echo "  /            → ${CODE_ROOT}"
echo "  /v1/models   → ${CODE_MODELS}"
echo "  /v1/identity → ${CODE_IDENTITY}"

echo
echo "── 3. interpretation ───────────────────────────────────────────────"
if [ "$CODE_ROOT" = "200" ] && [ "$CODE_IDENTITY" = "200" ]; then
  echo "  NOT CHAINED (steps 1+2 state): browser paths hit the app directly."
  echo "  → step 3 (the edge-chain AuthorizationPolicy) is the remaining work —"
  echo "    .scratch/pcai-llm-edge-authz.yaml is the applied shape (clone of"
  echo "    dsh-web-helm-auth-policy). GOTCHA: policies propagate over xDS —"
  echo "    wait ~1 min and re-verify before concluding NOT CHAINED (2026-10-08:""the first post-apply verify raced propagation and still read 200/401/200)."
elif [ "$CODE_ROOT" = "302" ] && [ "$CODE_IDENTITY" = "302" ] && [ "$CODE_MODELS" = "401" ]; then
  echo "  ✅ CHAINED CORRECTLY: browser+identity paths redirect to Keycloak,"
  echo "     /v1/models answers 401 FROM THE APP (bearer control intact)."
  echo "  → step 4 (the flags) may now be applied; then the browser sweep (step 5)."
elif [ "$CODE_MODELS" = "302" ]; then
  echo "  ❌ BLANKET 302 — the MM RAG failure mode: API paths are being chained."
  echo "  → fix the AuthorizationPolicy's path classification BEFORE the flags."
elif [ "$CODE_ROOT" = "503" ]; then
  echo "  gateway unreachable (roll in progress? pods Ready?)"
else
  echo "  UNRECOGNIZED state (${CODE_ROOT}/${CODE_MODELS}/${CODE_IDENTITY}) — inspect manually."
fi

if [ "$mode" = "--attach" ]; then
  echo
  echo "── attach: the per-host edge-chain AuthorizationPolicy (RBAC-gated) ──"
  echo "  existing CUSTOM policies on the ingress gateway (the clone source is"
  echo "  a chained host — e.g. dsh-web-helm-auth-policy):"
  kc get authorizationpolicy -n istio-system \
    -o custom-columns='NAME:.metadata.name,ACTION:.spec.action,HOSTS:.spec.rules[*].to[*].operation.hosts[*]' 2>/dev/null \
    | sed 's/^/    /' \
    || echo "    (RBAC denied from this identity — run under a full kubeconfig)"
  echo
  echo "  NOT applying anything: generate the pcai-llm policy from the clone"
  echo "  source's shape with this host's path classification — the applied"
  echo "  manifest is .scratch/pcai-llm-edge-authz.yaml."
fi
