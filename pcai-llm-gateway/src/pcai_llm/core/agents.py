"""Agent profiles — named tool surfaces exposed as model endpoints (2026-10-29).

WHY THIS EXISTS: exposing every granted MCP tool to every key cost 34,459
prompt tokens per relayed request (core.tool_surface) and made tool SELECTION a
111-way choice for a task that needed four servers. The hybrid surface removed
the token cost; an agent removes the CHOICE.

An agent is a MODEL-NAME entry point. GET /v1/models advertises it next to the
real pools (pcai.type="agent"); a request naming it resolves to the agent model
when one is pinned, else through the profile chat cascade exactly as "auto"
does. Its tool surface is the INTERSECTION of the key grants and the agent
servers — the profile narrows the agent, the agent narrows the profile, and the
KEY remains the hard boundary (an agent can only ever hide tools).

THREE SETS, NOT TWO (the rule that keeps this honest). One grant set feeds three
consumers, and they must NOT be the same set:
  * the OFFERED catalog + the DISPATCH gate -> narrowed by the agent (a model
    must not execute a tool its agent never showed it);
  * the read+egress EXFIL gate (mcpf.arg_policy.gate_call) -> the KEY whole
    set, never narrowed: agents are switchable per request, so narrowing the
    gate input would let one key split read and egress across two agents while
    the conversation itself carries the data between them.

READ FAULTS: for_key raises AgentRegistryUnavailable and the CALLER decides.
The request path FAILS OPEN (loudly) — an agent can never GRANT a tool, only
hide ones the key already holds, so a fault degrades to "no narrowing", i.e.
exactly what any unregistered model name does today, bounded by the same
profile grants. Failing closed there would make every alias request depend on
this read for a feature that only ever subtracts. The 403 ("an agent this key
may not use") is answered ONLY when the read succeeded — a fault must never be
reported as a permission decision.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from ..db import db
from .cascade import DEMAND_AWARE_ALIAS

log = logging.getLogger(__name__)

# Names the resolver owns. An agent must never take one of these: they are
# resolved BEFORE the agent lookup (and by other machinery), so an agent named
# "auto" would be advertised in /v1/models and then never selected.
# DEMAND_AWARE_ALIAS is IMPORTED, not re-typed: the request path builds its
# skip-set from this same constant, so a rename cannot leave the two disagreeing
# (an agent admitted under a stale name would be advertised and unreachable).
RESERVED_NAMES: frozenset[str] = frozenset({"auto", DEMAND_AWARE_ALIAS})


class AgentRegistryUnavailable(RuntimeError):
    """The agent registry could not be read — the caller must NOT fall through."""


@dataclass(frozen=True)
class Agent:
    id: uuid.UUID
    name: str
    description: str
    servers: tuple[str, ...]
    model: str  # '' = profile cascade
    system_prompt: str  # '' = none (injection is phase 2)
    enabled: bool
    # FALSE = this key profile does not offer it. Carried on the SAME row so the
    # request path can tell "not an agent" (fall through to the alias path) from
    # "an agent this key may not use" (403) without a second query — the two
    # outcomes differ by a privilege boundary, so they must never be conflated
    # by a missing lookup.
    offered: bool = True


def _row_to_agent(row: object) -> Agent:
    """One DB row -> Agent. PURE (tolerates asyncpg Records and dict fakes).

    servers is read as a tuple of str: asyncpg hands back a list for TEXT[]; a
    NULL (only possible on a hand-edited row) reads as empty, i.e. a chat-only
    agent — the SAFE direction, since it hides tools rather than revealing them.
    """
    servers = row.get("servers") or ()  # type: ignore[attr-defined]
    return Agent(
        id=row["id"],  # type: ignore[index]
        name=str(row["name"]),  # type: ignore[index]
        description=str(row.get("description") or ""),  # type: ignore[attr-defined]
        servers=tuple(str(s) for s in servers),
        model=str(row.get("model") or ""),  # type: ignore[attr-defined]
        system_prompt=str(row.get("system_prompt") or ""),  # type: ignore[attr-defined]
        enabled=bool(row.get("enabled", True)),  # type: ignore[attr-defined]
        offered=bool(row.get("offered", True)),  # type: ignore[attr-defined]
    )


_FOR_KEY_SQL = """
SELECT a.id, a.name, a.description, a.servers, a.model, a.system_prompt,
       a.enabled,
       (u.profile_id IS NULL OR pa.agent_id IS NOT NULL) AS offered
  FROM agent_profiles a
  LEFT JOIN api_keys k ON k.id = $1
  LEFT JOIN users u ON u.id = k.user_id
  LEFT JOIN profile_agents pa ON pa.agent_id = a.id AND pa.profile_id = u.profile_id
 WHERE a.enabled
 ORDER BY a.name
"""


async def for_key(key_id: uuid.UUID) -> list[Agent]:
    """Every ENABLED agent, each flagged with whether THIS key may use it.

    One indexed query, and deliberately the ONE function the request path and
    GET /v1/models both call: the listing and the resolver cannot disagree about
    what a key may use, which is the honesty contract the presented-models panel
    is built on (routes_admin.presented_models).

    A key with NO profile is offered EVERYTHING — the repo empty-ACL convention
    (api_keys.allowed_models empty = all models). Without it an admin/CLI key
    could never exercise an agent, and the alternative (invent a profile per
    operator) buys no security: the key own grants still bound every tool the
    agent could reach.
    """
    try:
        rows = await db.fetch(_FOR_KEY_SQL, key_id)
    except Exception as exc:  # fail CLOSED — see the module docstring
        log.exception("agents: registry read failed for key %s", key_id)
        raise AgentRegistryUnavailable("agent registry unavailable") from exc
    return [_row_to_agent(r) for r in rows or []]


async def list_all() -> list[Agent]:
    """Every agent (admin view — the offered flag is meaningless here).

    Fail-soft: an admin listing fault must not 500 the console. It reports an
    empty registry, which is the truth the operator can act on.
    """
    try:
        rows = await db.fetch(
            "SELECT id, name, description, servers, model, system_prompt, enabled "
            "FROM agent_profiles ORDER BY name"
        )
    except Exception:
        log.warning("agents: admin listing failed", exc_info=True)
        return []
    return [_row_to_agent(r) for r in rows or []]


def pick(agents: list[Agent], name: str) -> Agent | None:
    """The agent a request NAMED, or None when the name is not an agent. PURE.

    Exact match: model ids in this fleet are case-sensitive strings
    (GLM-5.3-Flash, Qwen3.8-27B), and an agent id is a model id.
    """
    for a in agents:
        if a.name == name:
            return a
    return None


def near_miss(agents: list[Agent], name: str) -> Agent | None:
    """The agent whose name differs from this one only by CASE. PURE.

    A model id is exact — this fleet ids are case-sensitive strings
    (GLM-5.3-Flash, Qwen3.8-27B) — so "admin-agent" is NOT "Admin-Agent". But a
    client that lowercases ids, or a human typing the name into a provider
    config, would otherwise fall through to the ordinary alias path and be
    served the profile FULL tool surface: exactly the widening an agent exists
    to prevent. The caller refuses by name instead of serving something else.
    """
    folded = name.casefold()
    for a in agents:
        if a.name != name and a.name.casefold() == folded:
            return a
    return None

def narrow_grants(grants: set[tuple[str, str]], servers: tuple[str, ...] | None) -> set[tuple[str, str]]:
    """Grants narrowed to an agent servers. None = no agent, unchanged. PURE.

    Used for the OFFERED surface and the DISPATCH gate only — never for the
    read+egress gate, which must keep the key whole set (module docstring).
    """
    if servers is None:
        return grants
    keep = set(servers)
    return {pair for pair in grants if pair[0] in keep}


def narrow_catalog(catalog: list[dict], servers: tuple[str, ...] | None) -> list[dict]:
    """A tool catalog narrowed to an agent servers. None = no agent. PURE.

    The hybrid surface derives its server DIRECTORY from this list
    (core.tool_surface.meta_tool_spec), so narrowing here is what makes the
    model discovery tool show exactly the profile-and-agent intersection.
    """
    if servers is None:
        return catalog
    keep = set(servers)
    return [t for t in catalog if str(t.get("server") or "") in keep]
