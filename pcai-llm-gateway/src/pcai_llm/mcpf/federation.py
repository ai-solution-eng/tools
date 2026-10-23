"""MCP federation ACL logic — PURE functions, unit-testable without a DB.

Policy shape: a multi-tenant gateway DENIES BY DEFAULT. A gateway key sees
only the (server, tool) pairs explicitly granted to it by ``allowed=true``
rows in ``mcp_tool_acls``; everything else is invisible and uncallable.
``allowed=false`` rows exist so an operator can DOCUMENT a deny where a
broad grant (or a future wildcard grant) would otherwise apply — under
default-deny they are redundant bookkeeping, not load-bearing policy, and
the pure functions here treat "no grant" and "denied row" identically.
"""

from __future__ import annotations

import logging
from uuid import UUID

from ..db import db

log = logging.getLogger(__name__)

# Live profile grants (ROADMAP §3, option a): when a key's owner profile
# declares 'mcp_grants' presets under a 'live_grants' policy, those grants
# evaluate AT CALL TIME — every key of the profile sees the profile's grant
# set the moment an admin edits it, with per-key mcp_tool_acls rows still
# winning as exceptions. The flag lives in defaults_json
# ({"live_grants": true, "mcp_grants": [[server, tool], ...]}) so profile
# presets stay one JSON document; OFF (absent/false) preserves today's
# stamp-at-create semantics byte-for-byte.

# Default-deny is the DOCUMENTED policy constant: a multi-tenant gateway
# cannot assume any key may call any tool, so absence of an explicit
# allowed=true grant means "denied". Kept as a named constant (instead of
# an implicit `if not grants`) because tests and admin tooling assert the
# POLICY, not its implementation detail.
DEFAULT_DENY = True


def visible_tools(catalog: list[dict], grants: set[tuple[str, str]]) -> list[dict]:
    """Filter a tool catalog down to the (server, tool) pairs the key holds.

    ``catalog`` entries look like {"server": name, "tool": tool_name,
    "description": ...} (produced by the /mcp catalog fetch); ``grants`` is
    the set of granted (server, tool) pairs for the calling key. Order is
    preserved so clients see a stable listing.
    """
    if not grants:
        return []  # default-deny fast path: no grants, nothing visible
    return [entry for entry in catalog if (entry["server"], entry["tool"]) in grants]


def is_allowed(grants: set[tuple[str, str]], server: str, tool: str) -> bool:
    """True iff the key holds an explicit grant for (server, tool).

    Default-deny: unknown server, unknown tool, or no grants at all are all
    simply False — there is no wildcard, no per-server blanket, no fallback.
    """
    return (server, tool) in grants


async def load_grants(key_id: UUID) -> set[tuple[str, str]]:
    """Granted (server, tool) pairs for one key: per-key mcp_tool_acls rows
    PLUS — when the owner's profile runs live grants — the profile's preset
    mcp_grants evaluated at call time. Per-key rows win as exceptions: the
    union adds the key's own grants on top of the profile set, and a
    key-level allowed=FALSE row REMOVES the pair from the profile set
    (default-deny posture: a documented deny must not be resurrected by
    the profile layer).

    Only rows with ``allowed = TRUE`` are grants;
    An allowed=FALSE row never subtracts anything BY ITSELF — it exists to
    document the deny; the deny-exception logic below is what makes it
    load-bearing under live grants.
    """
    rows = await db.fetch(
        "SELECT server, tool FROM mcp_tool_acls WHERE key_id = $1 AND allowed",
        key_id,
    )
    grants = {(row["server"], row["tool"]) for row in rows}
    # Live profile layer (§3a): one extra indexed query; returns None for
    # stamps-only profiles / no profile (today's semantics, byte-for-byte).
    # FAIL-OPEN GUARD: the ACL gate must never 500 because of this layer —
    # any fault here degrades to the key's own rows (v1.4.0 field bug: an
    # asyncpg-uncodable record[] raised for EVERY key and put the whole
    # relay into tool-error retry loops).
    try:
        live = await load_live_profile_grants(key_id)
    except Exception:
        log.exception("federation: live-profile grants lookup failed (key %s) — falling back to key rows", key_id)
        return grants
    if live is None:
        return grants
    profile_pairs, key_denies = live
    return (profile_pairs - key_denies) | grants


async def load_denies(key_id: UUID) -> set[tuple[str, str]]:
    """Denied (server, tool) pairs for one key: the key's allowed=FALSE
    mcp_tool_acls rows, verbatim.

    The subtract side of the relay scope-merge (routes_proxy): under the
    DEFAULT tool_relay_grant_mode="scope" the merge re-adds the whole
    policy-admitted catalog AFTER the grant filter, so a documented deny
    row is the ONLY thing that can hold a pair back — without this read
    the console's "disable" for an mcp-scoped key is a silent no-op.
    Under live grants load_grants already folds the same rows in as
    profile exceptions; this read is for the scope path, where FALSE rows
    are load-bearing policy rather than bookkeeping.

    Error-isolation mirrors load_grants: the ACL gate must never 500
    because of this layer — any fault here degrades to "no denies"
    (fail-OPEN for visibility, the same posture as the live-profile
    guard: the relay still grants nothing the key lacks a TRUE row or
    scope for).
    """
    try:
        rows = await db.fetch(
            "SELECT server, tool FROM mcp_tool_acls WHERE key_id = $1 AND NOT allowed",
            key_id,
        )
    except Exception:
        log.exception("federation: deny-rows lookup failed (key %s) — treating as no denies", key_id)
        return set()
    return {(row["server"], row["tool"]) for row in rows}


async def load_live_profile_grants(key_id: UUID) -> tuple[set[tuple[str, str]], set[tuple[str, str]]] | None:
    """(profile_grant_pairs, key_deny_pairs) when the key's owner profile
    runs LIVE grants, else None (stamps-only profile or no profile).

    One indexed query both ways (api_keys PK, mcp_tool_acls key index):
    the key's allowed=FALSE rows arrive in the SAME pass, so a key-level
    deny stays an exception the profile cannot override. defaults_json is
    read AT CALL TIME — an admin profile edit is visible to running keys
    on their next call, which is the whole point of option (a).
    """
    # fetch() (not fetchrow): every fake in the test suite implements the
    # row-list shape, and the JOIN returns at most one row (api_keys PK).
    # NOTE: the deny pairs arrive as 'server<US>tool' TEXT elements — an
    # anonymous record[] (array_agg((a, b))) has NO asyncpg codec and would
    # fail every call at the type-description phase (v1.4.0 field bug).
    rows = await db.fetch(
        """
        SELECT p.defaults_json,
               (SELECT array_agg(a.server || chr(31) || a.tool)
                  FROM mcp_tool_acls a
                 WHERE a.key_id = $1 AND a.allowed = FALSE) AS key_denies
          FROM api_keys k
          JOIN users u ON u.id = k.user_id
          JOIN profiles p ON p.id = u.profile_id
         WHERE k.id = $1
        """,
        key_id,
    )
    row = rows[0] if rows else None
    if row is None:
        return None
    from ..core.enroll_memory import parse_profile_defaults  # mcpf has no enroll_memory (v1.4.0 field bug #2)

    defaults = parse_profile_defaults(row["defaults_json"])
    if not defaults.get("live_grants"):
        return None
    pairs = {
        (str(g[0]), str(g[1]))
        for g in (defaults.get("mcp_grants") or [])
        if isinstance(g, (list, tuple)) and len(g) == 2
    }
    denies: set[tuple[str, str]] = set()
    for item in row["key_denies"] or []:
        # TEXT elements: 'server<US>tool' — split back into the pair
        try:
            server, _, tool = str(item).partition(chr(31))
            if server:
                denies.add((server, tool))
        except (TypeError, ValueError):
            continue
    return pairs, denies


def server_allows(target: dict, tool: str) -> bool:
    """Per-server tool policy, checked at CALL time (defense-in-depth on top
    of the per-key ACL pairs): 'all' exposes everything the server lists;
    'allowlist' only the explicitly allowed tools. Unknown policy -> deny
    (fail closed, same posture as the key ACLs)."""
    if target.get("tool_policy", "allowlist") == "all":
        return True
    return tool in (target.get("allowed_tools") or [])
