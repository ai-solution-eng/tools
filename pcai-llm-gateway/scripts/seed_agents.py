#!/usr/bin/env python3
"""Install the gateway's built-in AGENT PRESETS into a live control plane.

WHY A SCRIPT AND NOT A ROUTE OR A MIGRATION. An agent profile is config, and
this fleet seeds config through the admin API (the same posture as
scripts/seed_registry.py) — no boot-time writes, no rows baked into a
migration, and a deployment that never runs this keeps exactly the agents it
already has. The definitions themselves live in
`pcai_llm.core.agent_presets` so they are reviewed, versioned and tested as
code; this script only delivers them.

WHAT IT WRITES. `POST /admin/agents` (an UPSERT by name), so re-running after
a preset update is the upgrade path and re-running with no change is a no-op.
Nothing else is touched unless `--offer-to-profile` is given, which appends
the agent to a profile's selection (the union — it never removes one).

USAGE
  export ADMIN_KEY=$(kubectl -n pcai-llm get secret pcai-llm-gateway-admin \
      -o jsonpath='{.data.admin-api-keys}' | base64 -d)
  python3 scripts/seed_agents.py --dry-run            # what would change
  python3 scripts/seed_agents.py                      # install every preset
  python3 scripts/seed_agents.py --only prometheus-sweep
  python3 scripts/seed_agents.py --offer-to-profile user

  # host only reachable through the socks proxy:
  export ALL_PROXY=socks5h://127.0.0.1:1080
  # custom CA / skip verification:
  export GATEWAY_CA=/path/to/ca.pem   # or GATEWAY_INSECURE=1

VALIDATION BEFORE WRITE. Server names are checked against the live registry
(the admin route refuses unknown ones anyway, with the same message). Tool
names are checked against each server's LIVE `list_tools` when the server
answers — a typo in a narrowed list would otherwise install silently and hide
the tool it meant to name (narrowing can only subtract, so a wrong name is not
an error at request time). An unreachable server downgrades that check to a
warning: installing config must not depend on every MCP server being up.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx2

from pcai_llm.core.agent_presets import MAX_SYSTEM_PROMPT_CHARS, PRESETS, AgentPreset

# Same default + CA discovery as scripts/seed_registry.py (kept in step with
# it deliberately: two seeding entry points must not disagree about how the
# gateway is reached).
DEFAULT_GATEWAY = "https://pcai-llm-gateway.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
_CA_CANDIDATES = [
    os.path.expanduser("~/.dsh/certs/pcai-aie-root-ca.crt"),
    os.path.expanduser("~/.dsh/certs/hpe-chain.pem"),
    os.path.expanduser("~/.dsh/hpe-private-ssl-ca.pem"),
]
_DEFAULT_CA = next((p for p in _CA_CANDIDATES if os.path.exists(p)), None)


class Gateway:
    """The two calls this script needs, with the fleet's auth/CA conventions."""

    def __init__(self, base: str, admin_key: str, *, ca: Any = None, insecure: bool = False) -> None:
        self.base = base.rstrip("/")
        self._headers = {"Authorization": f"Bearer {admin_key}"}
        self._verify: Any = False if insecure else (ca or _DEFAULT_CA or True)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            r = httpx2.request(
                method,
                self.base + path,
                json=body,
                timeout=60,
                verify=self._verify,
                headers=self._headers,
            )
        except httpx2.HTTPError as exc:
            raise SystemExit(f"FATAL: {method} {path} -> {type(exc).__name__}: {exc}") from None
        if r.status_code >= 400:
            # 300 chars is the seed_registry convention: enough to read the
            # refusal, short enough not to bury the next line.
            raise SystemExit(f"FATAL: {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None


def _live_servers(gw: Gateway) -> set[str]:
    rows = gw.call("GET", "/admin/mcp-servers") or []
    return {str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("name")}


def _live_tools(gw: Gateway, server: str) -> dict[str, bool] | None:
    """The server's live catalog as {tool: admitted by the server's policy},
    or None when the server cannot be reached.

    None is NOT an empty dict: "the server is down" must skip the check, while
    "the server answered and the tool is not in it" is a real refusal. The
    ADMITTED flag matters too: a catalog entry the server's OWN allowlist
    denies would install fine and then be denied at call time — dead config
    that reads, in the preset, as a working sweep.
    """
    try:
        payload = gw.call("GET", f"/admin/mcp-servers/{server}/tools")
    except SystemExit as exc:
        print(f"  !! {server}: tool catalog unavailable ({exc}); skipping the name check")
        return None
    entries = (payload or {}).get("tools") or []
    out: dict[str, bool] = {}
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("tool") or entry.get("name")
            admitted = entry.get("allowed", True)
        else:
            name, admitted = entry, True
        if name:
            out[str(name)] = bool(admitted)
    return out or None


def validate(preset: AgentPreset, live_servers: set[str], gw: Gateway | None, *, force: bool) -> list[str]:
    """Every reason this preset must NOT be installed. Empty = installable."""
    problems: list[str] = []
    if len(preset.system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
        problems.append(
            f"system_prompt is {len(preset.system_prompt)} chars — the admin API caps it at {MAX_SYSTEM_PROMPT_CHARS}"
        )
    if len(preset.description) > 400:
        problems.append(f"description is {len(preset.description)} chars — the admin API caps it at 400")
    missing = [s for s in preset.servers if s not in live_servers]
    if missing:
        problems.append(f"servers not in the live registry: {missing} (registered: {sorted(live_servers)})")
    if not problems and gw is not None:
        for server, wanted in preset.tools.items():
            if server not in preset.servers:
                problems.append(f"tools name server {server!r} that is not in servers")
                continue
            live = _live_tools(gw, server)
            if live is None:
                continue
            unknown = [t for t in wanted if t not in live]
            if unknown:
                problems.append(
                    f"{server}: tool names not exposed by the live server: {unknown} (available: {sorted(live)})"
                )
            denied = [t for t in wanted if live.get(t) is False]
            if denied:
                admitted = sorted(name for name, ok in live.items() if ok)
                problems.append(
                    f"{server}: tool names the server's own policy does not admit: {denied} (admits: {admitted})"
                )
    if problems and force:
        for p in problems:
            print(f"  !! {p} (ignored: --force)")
        return []
    return problems


def offer_to_profile(gw: Gateway, preset: AgentPreset, profile: str) -> None:
    """Append (never remove) the agent in one profile's selection.

    Read-modify-write on purpose: POST /admin/profiles MERGES an omitted field
    and REPLACES a present one, so the only safe way to add without clearing
    is to read the current list and post the union. A concurrent edit between
    the read and the write loses that edit's agent — acceptable for a CLI
    action the operator just requested, and the reason this is opt-in.
    """
    profiles = gw.call("GET", "/admin/profiles") or []
    current = next((p for p in profiles if str(p.get("name")) == profile), None)
    if current is None:
        raise SystemExit(f"FATAL: profile {profile!r} does not exist (have: {[p.get('name') for p in profiles]})")
    wanted = sorted(set(current.get("agents") or []) | {preset.name})
    gw.call("POST", "/admin/profiles", {"name": profile, "agents": wanted})
    print(f"  -> profile {profile!r} now offers: {wanted}")


def json_summary(preset: AgentPreset) -> str:
    """The install line WITHOUT the prompt body (the operator asked for a
    summary, not six kilobytes of prose in their terminal)."""
    body = preset.upsert_body()
    return (
        f"name={body['name']} servers={body['servers']}"
        f" tools={ {s: len(t) for s, t in body['tools'].items()} }"
        f" model={body['model']!r} prompt={len(body['system_prompt'])}ch"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Install built-in agent presets into a live gateway.")
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_URL") or DEFAULT_GATEWAY)
    ap.add_argument("--only", action="append", metavar="NAME", help="install only this preset (repeatable)")
    ap.add_argument("--offer-to-profile", action="append", metavar="PROFILE", help="also offer it to this profile")
    ap.add_argument("--list", action="store_true", help="list the shipped presets and exit")
    ap.add_argument("--dry-run", action="store_true", help="validate and print; write nothing")
    ap.add_argument("--force", action="store_true", help="install despite validation problems")
    args = ap.parse_args()

    if args.list:
        for preset in PRESETS.values():
            print(
                f"{preset.name}: servers={list(preset.servers)}"
                f" tools={ {s: len(t) for s, t in preset.tools.items()} }"
                f" prompt={len(preset.system_prompt)}ch v{preset.prompt_version}"
            )
        return 0

    selected = list(PRESETS.values())
    if args.only:
        unknown = [n for n in args.only if n not in PRESETS]
        if unknown:
            raise SystemExit(f"FATAL: unknown preset(s) {unknown}; shipped: {sorted(PRESETS)}")
        selected = [PRESETS[n] for n in args.only]

    admin_key = os.environ.get("ADMIN_KEY", "").strip()
    if not admin_key:
        raise SystemExit("FATAL: set ADMIN_KEY first (see the header comment for the kubectl one-liner)")

    gw = Gateway(
        args.gateway,
        admin_key,
        ca=os.environ.get("GATEWAY_CA"),
        insecure=os.environ.get("GATEWAY_INSECURE") == "1",
    )
    print(f"== Agent presets -> {gw.base} ==")
    live_servers = _live_servers(gw)

    failed = False
    for preset in selected:
        print(
            f"[{preset.name}] {len(preset.servers)} servers, prompt {len(preset.system_prompt)} chars, v{preset.prompt_version}"
        )
        # Validate against the LIVE catalogs even in --dry-run: a dry run whose
        # whole point is "would this install cleanly" must not skip the check
        # that catches the typo this validation exists for.
        problems = validate(preset, live_servers, gw, force=args.force)
        if problems:
            failed = True
            for p in problems:
                print(f"  !! {p}")
            print("  -> SKIPPED (fix, or re-run with --force to install anyway)")
            continue
        if args.dry_run:
            print(f"  [dry] POST /admin/agents {json_summary(preset)}")
            continue
        row = gw.call("POST", "/admin/agents", preset.upsert_body()) or {}
        print(
            f"  -> installed: servers={row.get('servers')}"
            f" tools={ {s: len(t) for s, t in (row.get('tools') or {}).items()} }"
            f" prompt={len(str(row.get('system_prompt') or ''))}ch"
        )
        for profile in args.offer_to_profile or []:
            offer_to_profile(gw, preset, profile)

    if args.dry_run:
        print("dry-run complete (nothing written)")
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
