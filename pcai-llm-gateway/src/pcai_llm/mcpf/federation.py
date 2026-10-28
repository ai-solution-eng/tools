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



# ---------------------------------------------------------------------------
# Server-class taxonomy + grant-profile presets (P0-3, hardening 2026-10-26)
# ---------------------------------------------------------------------------
#
# The default-deny pair machinery above answers "may this key call THIS
# (server, tool) pair?" — it never says anything about WHAT a server is.
# The least-privilege wave needs the second axis: every fleet MCP server
# is classified into exactly one class-set, from ONE constant (the table
# below; nothing else in the gateway may restate it), and the four named
# profile presets are derived from it.
#
#   read   — observation surfaces; cannot change cluster or data state
#   egress — tools that can reach OUT (the exfiltration channel class)
#   mutate — tools that change state, but cannot reach out
#   admin  — credential/authorization control-plane verbs (sqlhandler
#            admin_*); additionally double-gated by admin_tool_refusal()
#            so NO grant alone can call them
#
# The rule that matters most (plan P0-3): no key should hold BOTH a read
# grant and an egress grant — that combination lets a key read data and
# then hand it to a tool that can send. read_egress_violation() is the
# pure predicate the MCP gate chain runs on the key's grant set.
#
# Names are the live registry's virtual names (scripts/seed_registry.py
# MCP_SERVERS + the seaborn analytics server). A server NOT in this table
# belongs to no class: it is excluded from every preset and from both
# violation classes — a deliberately conservative unknown (classifying an
# unclassified server requires editing this table, which is reviewable).

MCP_SERVER_CLASSES: dict[str, frozenset[str]] = {
    "k8s-ops": frozenset({"read"}),
    "prometheus": frozenset({"read"}),
    "logsearch": frozenset({"read"}),
    # sqlhandler carries BOTH classes deliberately: the read tools power the
    # observe preset, and its admin_* verbs are the admin class. The admin
    # verbs are DOUBLE-gated (admin_tool_refusal below) so the read grant
    # never implies them.
    "sqlhandler": frozenset({"read", "admin"}),
    # mm-rag is {read, mutate} (wave-1 review P1-2): its surface mixes
    # read/search/memory-recall tools with hard writes (dataset_add_documents,
    # dataset_delete_documents, add/delete_memory, create/end_short_term_memory,
    # set_memory_dataset). Class alone must never mean "safe" — the WRITE-VERB
    # rule below does the per-tool cut; the dual class keeps that honest.
    "mm-rag": frozenset({"read", "mutate"}),
    "seaborn": frozenset({"read"}),
    "searxng": frozenset({"egress"}),
    "applygate": frozenset({"mutate"}),
    "ezapp-deploy": frozenset({"mutate"}),
    "workbench": frozenset({"mutate"}),
}

# Admin-only verb prefix (SPEC §3.2). The prefix is only meaningful on the
# admin-class server — a tool merely NAMED "admin_*" elsewhere is not
# admin-only (see is_admin_only_tool).
MCP_ADMIN_ONLY_TOOL_PREFIXES = ("admin_",)

# WRITE-VERB rule (wave-1 review P1-2): server CLASS alone cannot decide
# read-only-ness — tool NAMES must. A tool whose name starts with one of
# these prefixes, or whose full name is in the exact set, CHANGES STATE
# (writes data, mutates memory, executes code, deletes) and is excluded
# from every preset that does not admit the mutate class — regardless of
# the server's class. This keeps 'observe' honest against REAL catalogs:
# mm-rag carries delete/add/set/forget verbs under a mostly-read surface,
# sqlhandler query_save/query_delete persist, k8s-ops exec_in_pod executes
# inside containers. Case-insensitive; the exact-name set covers verbs a
# prefix cannot express. ONE constant — presets derive their safety from
# it and the tests pin real registered tool names against it.
MCP_WRITE_TOOL_PREFIXES = (
    "add_", "create_", "delete_", "replace_", "set_", "save_", "forget_",
    "end_", "unlock_", "deselect_", "exec_", "run_", "apply_", "write_",
    "remove_", "upload_", "cancel_", "revoke_", "restore_", "patch_",
)
# Verb SEGMENTS (wave-1 review P1-2 follow-up): several registered tools
# carry the write verb in the MIDDLE of the name — mm-rag
# dataset_delete_documents / dataset_add_documents / dataset_replace_document,
# sqlhandler query_save / query_delete / query_cancel. A leading-prefix rule
# misses all of them, so the rule ALSO matches when any underscore-delimited
# segment of the name EQUALS a write verb (segment equality, not substring —
# "dataset" is one segment and must not match "set"). Cost of the broader
# rule: plan_apply is excluded from observe too — a server-side dry-run is
# still the mutate server's surface, and under-provision is the safe
# direction.
# Full-name exact entries (case-insensitive): verbs the prefix/segment rules
# cannot express. plan_delete is the applygate destructive verb; delete_chart
# is ALSO caught by "delete_" and pinned for legibility.
MCP_WRITE_TOOL_NAMES = frozenset({
    "plan_delete",
    "delete_chart",
})

# READ-ONLY exceptions to the WRITE-VERB rule (wave-2 review): these (server,
# tool) pairs are named with a write-shaped verb but are read-only BY
# CONTRACT — sqlhandler.run_sql is SELECT-only (sqlguard.assert_mcp_readonly,
# 'Execute a read-only SQL query ... SELECT-only') and k8s-ops.run_kubectl is
# annotated read_only_hint: true in the live catalog. Kept as an exact PAIR
# set (not a name set) deliberately: the exception is about what THAT server
# guarantees for THAT tool, and a same-named tool elsewhere gets no benefit
# of the doubt. If either server ever widens the verb's behavior, remove the
# pair here — the tests pin observe CONTAINS these two.
READ_ONLY_RUN_TOOLS = frozenset({
    ("sqlhandler", "run_sql"),
    ("k8s-ops", "run_kubectl"),
})

MCP_WRITE_TOOL_SEGMENTS = frozenset({
    "add", "create", "delete", "replace", "set", "save", "forget", "end",
    "unlock", "deselect", "exec", "run", "apply", "write", "remove",
    "upload", "cancel", "revoke", "restore", "patch",
})

# The (only) server whose admin_* verbs mint/revoke credentials and edit
# authorization policy. Pinned as a named constant so the predicate, the
# presets and any future caller can never drift apart.
ADMIN_ONLY_SERVER = "sqlhandler"

# Derived class views — the ONE constant is the table above; these are its
# projections (named constants because the SPEC pins these names for the
# Z1 gate chain).
EGRESS_SERVERS = frozenset(s for s, c in MCP_SERVER_CLASSES.items() if "egress" in c)
READ_SERVERS = frozenset(s for s, c in MCP_SERVER_CLASSES.items() if "read" in c)

# Four named grant-profile presets (plan P0-3 table): for each preset, the
# server CLASSES the preset admits. Resolution against concrete (server,
# tool) pairs happens at STAMP time against the live per-server catalogs
# (preset_catalog_pairs + preset_pairs below), so a server that gains a new
# tool does not silently join a stamped profile until the operator re-stamps.
MCP_PROFILE_PRESETS: dict[str, frozenset[str]] = {
    # cannot change state; deliberately EXCLUDES admin verbs (the prefix rule
    # in preset_pairs keeps admin_* out of every read stamp)
    "observe": frozenset({"read"}),
    # the exfiltration channel, isolated: egress and nothing else
    "egress": frozenset({"egress"}),
    # changes state, no egress, no reads
    "mutate": frozenset({"mutate"}),
    # sqlhandler admin_* ONLY — and even a key of an admin-stamped profile
    # is refused at call time unless the KEY itself carries mcp_admin=TRUE
    # (belt to the suspenders: a grant alone never re-arms admin verbs)
    "admin": frozenset({"admin"}),
}


def is_admin_only_tool(server: str, tool: str) -> bool:
    """True iff (server, tool) is an admin-class verb: sqlhandler + a tool
    named under MCP_ADMIN_ONLY_TOOL_PREFIXES. Everything else — including a
    similarly-named tool on any other server — is not.

    Case-insensitive (wave-1 review P3): a registry entry named "SQLHandler"
    or "Admin_Grants" must not slip the gate. Not exploitable today — the
    pair ACL grants exact strings and sqlhandler's dispatch is exact-match —
    but the predicate must not depend on that."""
    return (
        server.lower() == ADMIN_ONLY_SERVER
        and tool.lower().startswith(MCP_ADMIN_ONLY_TOOL_PREFIXES)
    )


def is_write_tool(tool: str) -> bool:
    """True iff the tool NAME is a state-changing verb under the WRITE-VERB
    rule — independent of any server class. Case-insensitive (a registry
    entry "Delete_Documents" is still a delete). Three clauses, cheapest
    first: exact full name, leading prefix, underscore-SEGMENT equality
    (catches the registered mid-name verbs like dataset_delete_documents
    and query_save without ever substring-matching inside a segment)."""
    name = tool.lower()
    if name in MCP_WRITE_TOOL_NAMES or name.startswith(MCP_WRITE_TOOL_PREFIXES):
        return True
    return any(seg in MCP_WRITE_TOOL_SEGMENTS for seg in name.split("_"))


def admin_tool_refusal(server: str, tool: str, key_mcp_admin: bool) -> str:
    """Empty string when the call may proceed; a refusal message when the
    tool is admin-only and the calling key lacks the mcp_admin opt-in.

    The message names the CONTROL (the mcp_admin flag), not the shape of the
    bypass path — an unprivileged caller learns only that the call was
    refused and who can authorize it. Z1's gate chain settles the refusal
    under ledger status 'policy_blocked' with metric reason 'admin-tool'."""
    if not is_admin_only_tool(server, tool) or key_mcp_admin:
        return ""
    return (
        f"refused: {server}.{tool} is an admin-control-plane tool; MCP-surface "
        "calls require a key explicitly flagged mcp_admin=TRUE (default FALSE) "
        "— mint such a key out-of-band via the admin console, never from a model"
    )


def read_egress_violation(
    grants: set[tuple[str, str]],
) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """(read_pair, egress_pair) when ONE grant set holds both classes; None
    otherwise (empty sets and single-class sets are the honest no-op cases).

    Deterministic pick: the alphabetically-first granted pair of each class,
    so a given grant set always reports the SAME pair and two alerts for one
    key compare equal. Z1's gate chain calls this per request under
    PCAI_LLM_MCP_SEPARATE_READ_EGRESS with the KEY'S WHOLE grant set, and
    that is the intended posture (wave-1 review P2-3a, stated in the docs):
    in enforce mode EVERY call from a both-classes key refuses — the key is
    mis-scoped, not just this call — so the rollout order is SPLIT THE KEYS
    FIRST (preset stamps / per-key grant edits), THEN flip. Warn-mode ticks
    use MCP_READ_EGRESS_WARN_REASON ("read+egress-warn"), never the enforce
    reason, because the series' name says blocked and warn does not block."""
    read_pairs = sorted(g for g in grants if g[0] in READ_SERVERS)
    egress_pairs = sorted(g for g in grants if g[0] in EGRESS_SERVERS)
    if not read_pairs or not egress_pairs:
        return None
    return (read_pairs[0], egress_pairs[0])


SEPARATE_READ_EGRESS_MODES = ("enforce", "warn", "off")

# Metric reason vocabulary for the read+egress gate (wave-1 review P2-3b).
# The series gateway_mcp_policy_blocked_total means BLOCKED; a warn-mode hit
# proceeds, so it must tick the DISTINCT reason "read+egress-warn" — never
# "read+egress", or the metric lies about what happened. Z1's gate chain
# imports these constants; an operator alerting on enforcement uses
# {reason="read+egress"} exactly.
MCP_READ_EGRESS_ENFORCE_REASON = "read+egress"
MCP_READ_EGRESS_WARN_REASON = "read+egress-warn"


def separate_read_egress_mode(raw: str | None = None) -> str:
    """Resolve PCAI_LLM_MCP_SEPARATE_READ_EGRESS to one of
    SEPARATE_READ_EGRESS_MODES. None/empty -> the Settings default ("warn");
    an unrecognized value -> "warn" — fail-safe (a typo'd env var must not
    silently disable the check) and never boot-fatal."""
    from ..config import settings  # deferred: federation has no config import at module load

    value = (raw if raw is not None else settings.mcp_separate_read_egress) or ""
    # .strip().lower() before the membership test (wave-1 review P2-2):
    # "ENFORCE" / " enforce" must NOT silently degrade to warn — that would
    # be the exact not-enforcing failure the fail-safe default exists to
    # avoid. Only a genuinely unrecognized value falls back to warn.
    value = value.strip().lower()
    return value if value in SEPARATE_READ_EGRESS_MODES else "warn"


def preset_catalog_pairs(
    catalogs: dict[str, list[dict]],
) -> set[tuple[str, str]]:
    """Flatten per-server tool catalogs ({"tool": name, ...} entries — the
    shape routes_mcp._fetch_tools returns) into (server, tool) pairs.
    Servers missing from the dict contribute nothing (stamp-time catalog
    gaps are REPORTED by the caller, never guessed at here)."""
    pairs: set[tuple[str, str]] = set()
    for server, tools in catalogs.items():
        for entry in tools or []:
            tool = entry.get("tool") if isinstance(entry, dict) else None
            if tool:
                pairs.add((server, tool))
    return pairs


def preset_pairs(
    preset: str,
    catalog: set[tuple[str, str]] | list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Resolve a named preset (MCP_PROFILE_PRESETS) against a live (server,
    tool) catalog: the catalog pairs whose server carries one of the
    preset's classes, with two corrective rules applied AFTER the class
    match so the taxonomy's edge cannot leak:
      1. an admin_* tool never enters a NON-admin preset (a read-class
         server that also carries admin verbs — sqlhandler — must not hand
         them to an observe key via a stamp);
      2. an admin preset admits ONLY admin_* verbs (never the plain read
         tools of the admin-class server);
      3. a WRITE verb (is_write_tool) never enters a preset that does not
         admit the mutate class — class is not proof of read-only-ness, the
         tool name is (wave-1 review P1-2: the observe stamp previously
         granted mm-rag dataset_delete_documents, sqlhandler query_save,
         k8s-ops exec_in_pod, and ten more state-changing verbs).

    Unknown preset -> ValueError: a typo must be a loud 400, not an empty
    grant list that reads as success."""
    try:
        classes = MCP_PROFILE_PRESETS[preset]
    except KeyError:
        raise ValueError(
            f"unknown mcp grant preset {preset!r}; known: {sorted(MCP_PROFILE_PRESETS)}"
        ) from None
    out: set[tuple[str, str]] = set()
    for server, tool in catalog:
        if MCP_SERVER_CLASSES.get(server, frozenset()) & classes:
            if "admin" not in classes and tool.startswith(MCP_ADMIN_ONLY_TOOL_PREFIXES):
                continue  # rule 1: admin verbs stay out of every non-admin stamp
            if "admin" in classes and not tool.startswith(MCP_ADMIN_ONLY_TOOL_PREFIXES):
                continue  # rule 2: the admin stamp is admin verbs ONLY
            if (
                "mutate" not in classes
                and is_write_tool(tool)
                and (server, tool) not in READ_ONLY_RUN_TOOLS
            ):
                # rule 3: a non-mutate stamp never carries a write verb —
                # EXCEPT the documented read-only run tools above (wave-2:
                # rule 3 as shipped had cut sqlhandler.run_sql and
                # k8s-ops.run_kubectl, leaving observe able to list tables
                # but not select a row / read the cluster)
                continue
            out.add((server, tool))
    return sorted(out)
