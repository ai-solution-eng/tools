#!/usr/bin/env python3
"""Seed the PCAI LLM Gateway registry from the fleet's pcai_models.py + the
live G2 MCP servers.

Models: ONLY currently_deployed=True entries (currently: Qwen3.8-27B —
gemma is NOT deployed; glm-5.3-flash "sproket" excluded per Andrew).
MCP servers: ground truth from the live cluster (namespaces + ports verified
2026-09); ddgs-mcp and rag-mcp have NO services in the cluster -> skipped.

Credentials: each model's MLIS JWT is extracted from pcai_models.py and
stored Fernet-encrypted (panel path) — plaintext never printed. MCP servers
get the fleet key from $MCP_FLEET_KEY (also panel-encrypted).

Usage:
  export ADMIN_KEY=$(kubectl -n pcai-llm get secret pcai-llm-gateway-admin \
      -o jsonpath='{.data.admin-api-keys}' | base64 -d)
  export MCP_FLEET_KEY='<the fleet api-keys value>'
  # optional (if the host is only reachable via your socks proxy):
  export ALL_PROXY=socks5h://127.0.0.1:1080
  python3 scripts/seed_registry.py [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re

import httpx2  # honors HTTPS_PROXY/ALL_PROXY (socks needs httpx2[socks])

MODELS_SRC = os.environ.get("PCAI_MODELS_SRC", "/home/andrew/Code/HPE/pcai_utils/pcai_models.py")
BASE = os.environ.get("GATEWAY_URL", "https://pcai-llm-gateway.pcai-se-ai-application.hst.rdlabs.hpecorp.net")

# PCAI AIE Root CA — the gateway's serving cert is issued by it (and the
# host serves NO chain, so the root must be local). Auto-detected from the
# DSH kit (the hpe-private-ssl-ca / hpe-chain are DIFFERENT CAs — verified
# the AIE root is the one that validates); override with GATEWAY_CA, or set
# GATEWAY_INSECURE=1 to skip verification (not recommended).
_CA_CANDIDATES = [
    os.path.expanduser("~/.dsh/certs/pcai-aie-root-ca.crt"),
    os.path.expanduser("~/.dsh/certs/hpe-chain.pem"),
    os.path.expanduser("~/.dsh/hpe-private-ssl-ca.pem"),
]
DEFAULT_CA = next((p for p in _CA_CANDIDATES if os.path.exists(p)), None)
CA_BUNDLE = os.environ.get("GATEWAY_CA") or (DEFAULT_CA or True)

# (virtual name, model_type, url host prefix) — deployed models ONLY,
# glm-5.3-flash "sproket" deliberately excluded (the glm-5-2-fran block in
# pcai_models.py is francesco's deployment — host prefix picks the right JWT).
# ⚠️ base_url for registered endpoints must be the IN-CLUSTER svc URL: the
# gateway pod's egress goes through the corporate HTTP(S)_PROXY, which cannot
# reach the *.serving.* ingress hostnames (502 "upstream request failed",
# verified 2026-09-19) — and the proxy env exists precisely so the gateway
# can reach MCP servers via their gateway hostnames. Cluster-internal
# .svc.cluster.local traffic bypasses the proxy (NO_PROXY) and works.
DEPLOYED = [
    ("GLM-5.3-Flash", "chat", "glm-53-flash"),
    ("Qwen/Qwen3.8-27B-FP8", "chat", "qwen-38-27b"),
    ("RamManavalan/Qwen3-VL-Embedding-8B-FP8", "embedding", "qwen3-vl-embedding-8b"),
]

# Reference prices, USD per 1M tokens — PUBLIC MARKET rates for parity /
# chargeback-style accounting. Your true internal cost (GPU amortization) is
# almost certainly lower; these are the "what would this cost at list price"
# numbers. Sources: Z.ai official list (GLM-5.3-Flash $0.15/$0.50/$0.03);
# OpenRouter (Qwen3.8-27B $0.15/$1.875; cached ≈ $0.03-0.06).
MODEL_PRICES = {
    "Qwen/Qwen3.8-27B-FP8": {"input_per_m": 0.15, "output_per_m": 1.88, "cached_per_m": 0.03},
    "GLM-5.3-Flash": {"input_per_m": 0.15, "output_per_m": 0.50, "cached_per_m": 0.03},
    "RamManavalan/Qwen3-VL-Embedding-8B-FP8": {"input_per_m": 0.05, "output_per_m": 0.0, "cached_per_m": 0.0},
}

# (name, url, port) — live cluster ground truth (kubectl, 2026-09)
MCP_SERVERS = [
    ("applygate", "applygate-mcp-service.applygate-mcp.svc.cluster.local", 9102),
    ("logsearch", "logsearch-mcp-service.logsearch-mcp.svc.cluster.local", 9101),
    ("workbench", "workbench-mcp-service.workbench-mcp.svc.cluster.local", 9103),
    ("prometheus", "prometheus-mcp-service.prometheus-mcp.svc.cluster.local", 9095),
    ("searxng", "searxng-mcp-service.searxng-mcp.svc.cluster.local", 9090),
    ("sqlhandler", "sqlhandler.sqlhandler.svc.cluster.local", 9097),
    # MultimodalRAG's MCP surface (user-corrected: namespace is mm-rag)
    ("mm-rag", "rag-mcp-server-mcp.mm-rag.svc.cluster.local", 9090),
    # The fleet's governed K8s ops surface (read-only K8S MCP + console)
    ("k8s-ops", "k8s-mcp-service.k8s-mcp-ops.svc.cluster.local", 9090),
]


def extract_jwt(var_hint: str) -> str:
    """Pull the api_key JWT for the model whose entry contains var_hint."""
    src = open(MODELS_SRC).read()
    # find the ChatModel(...) block that contains both the url and the api_key
    blocks = re.findall(r"=(?:\s*)(ChatModel|EmbeddingModel)\(\n(.*?)\n\)", src, re.DOTALL)
    for _, body in blocks:
        if var_hint in body and "api_key" in body:
            m = re.search(r'"(eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"', body)
            if m:
                return m.group(1)
    raise SystemExit(f"FATAL: no JWT found for {var_hint!r} in {MODELS_SRC}")


def call(method: str, path: str, body: dict | None = None) -> dict:
    verify = False if os.environ.get("GATEWAY_INSECURE") == "1" else CA_BUNDLE
    try:
        r = httpx2.request(
            method,
            BASE + path,
            json=body,
            timeout=60,
            verify=verify,
            headers={"Authorization": f"Bearer {os.environ['ADMIN_KEY']}"},
        )
        if r.status_code >= 400:
            print(f"  !! {method} {path} -> {r.status_code}: {r.text[:300]}")
            return {"error": r.text[:300]}
        return r.json()
    except httpx2.HTTPError as e:
        print(f"  !! {method} {path} -> {type(e).__name__}: {e}")
        return {"error": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.environ.get("ADMIN_KEY"):
        raise SystemExit("set ADMIN_KEY first (see header comment)")

    print(f"== Seeding {BASE} ==")

    if args.dry_run:
        for virtual, mtype, host_prefix in DEPLOYED:
            extract_jwt(host_prefix)  # proves the JWT is findable
            print(f"[dry] model {virtual} ({mtype}) + panel token")
        for name, host, port in MCP_SERVERS:
            own = os.environ.get(f"MCP_KEY__{name.upper().replace('-', '_')}")
            src = "own key" if own else ("fleet key" if os.environ.get("MCP_FLEET_KEY") else "NO KEY")
            print(f"[dry] mcp {name} http://{host}:{port}/mcp [{src}]")
        for model_name, price in MODEL_PRICES.items():
            print(f"[dry] price {model_name} -> {price}")
        print("[dry] profile 'user' (cascade over seeded models, 200k/mo)")
        print("dry-run complete (nothing written)")
        return

    # --- tenants / user (idempotent: 409 on duplicate -> resolve via GET) ---
    t = call("POST", "/admin/tenants", {"name": "fleet"})
    tid = t.get("id")
    if not tid:
        tid = next((x["id"] for x in call("GET", "/admin/tenants") if x["name"] == "fleet"), None)
    if not tid:
        raise SystemExit(f"FATAL: could not create or find tenant 'fleet': {t}")
    u = call("POST", f"/admin/tenants/{tid}/users", {"name": "andrew"})
    uid = u.get("id")
    if not uid:
        uid = next((x["id"] for x in call("GET", f"/admin/tenants/{tid}/users") if x["name"] == "andrew"), None)
    if not uid:
        raise SystemExit(f"FATAL: could not create or find user 'andrew': {u}")
    print(f"tenant/user: fleet/andrew ({uid})")

    # --- models (deployed only; sproket excluded) ---
    for virtual, mtype, host_prefix in DEPLOYED:
        jwt = extract_jwt(host_prefix)
        print(f"model: {virtual} ({mtype}) — token extracted, not printed")
        if not args.dry_run:
            # IN-CLUSTER svc URL (see DEPLOYED comment): the pod's proxy egress
            # cannot reach the external serving hostnames.
            svc = f"{host_prefix}.project-user-andrew-bydlon.svc.cluster.local"
            r = call(
                "POST",
                "/admin/models",
                {
                    "model_name": virtual,
                    "model_type": mtype,
                    "endpoints": [
                        {
                            "base_url": f"http://{svc}/v1",
                            "priority": 0,
                            "weight": 1,
                            "api_key": jwt,
                        }
                    ],
                },
            )
            print("  ->", r)

    # --- MCP servers (live cluster; per-server key > fleet key) ---
    fleet = os.environ.get("MCP_FLEET_KEY", "").strip()
    if fleet and ("<" in fleet or len(fleet) < 12):
        # the '<fleet-key>' placeholder got stored once and every fleet server
        # 401'd the gateway — refuse obvious placeholders loudly
        raise SystemExit(
            "FATAL: MCP_FLEET_KEY looks like a placeholder (contains '<' or too "
            "short). The real key is in the cluster Secret the MCP servers use: "
            "kubectl -n applygate-mcp get secret mcp-fleet-apikeys "
            "-o jsonpath='{.data.api-keys}' | base64 -d"
        )
    if not fleet:
        print("!! MCP_FLEET_KEY not set — servers without their own key register WITHOUT credentials")
    for name, host, port in MCP_SERVERS:
        url = f"http://{host}:{port}/mcp"
        # per-server override: MCP_KEY__<NAME_UPPER> (some servers use their
        # own Secret, e.g. k8s-ops -> k8s-mcp-2-0-apikey, NOT the fleet key)
        token = os.environ.get(f"MCP_KEY__{name.upper().replace('-', '_')}", "").strip() or fleet
        if token and ("<" in token or len(token) < 12):
            raise SystemExit(
                f"FATAL: MCP_KEY__{name.upper().replace('-', '_')} looks like a "
                "placeholder — fetch the real value from that server's Secret"
            )
        src = (
            "own key"
            if os.environ.get(f"MCP_KEY__{name.upper().replace('-', '_')}")
            else ("fleet key" if token else "NO KEY")
        )
        print(f"mcp: {name} -> {url} [{src}]")
        if not args.dry_run:
            r = call(
                "POST",
                "/admin/mcp-servers",
                {
                    "name": name,
                    "url": url,
                    **({"auth_token": token} if token else {}),
                    "tool_policy": "all",
                    "enabled": True,
                },
            )
            print("  ->", r)

    # --- reference pricing (public market rates; see MODEL_PRICES) ---
    for model_name, price in MODEL_PRICES.items():
        print(f"price: {model_name} -> {price}")
        if not args.dry_run:
            r = call("POST", "/admin/costs", {"model_name": model_name, **price})
            print("  ->", r)

    # --- sensible default profile (cascade over what exists) ---
    if not args.dry_run:
        r = call(
            "POST",
            "/admin/profiles",
            {
                "name": "user",
                "model_order": [v for v, _, _ in DEPLOYED],
                "budgets": [{"model": "*", "period": "month", "limit_tokens": 200000}],
                "mcp_grants": [],
            },
        )
        print("profile 'user':", r)

    print("done." if not args.dry_run else "dry-run complete (nothing written)")


if __name__ == "__main__":
    main()
