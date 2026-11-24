"""ONE attribute contract for every tool span the gateway emits.

WHY this module exists: two call sites assembled the same attribute set by hand
— the relay executor (core/toolrelay._emit_tool_span) and the /mcp tools/call
span (api/routes_mcp). Adding a field meant remembering both, and the two had
already drifted: mcp.surface existed only on /mcp spans, pcai.iteration and the
argument digest only on relay spans, so a query that worked on one surface
silently half-worked on the other.

The keys live here; call sites pass facts. This module is pure and stdlib-only —
no settings, no I/O, no span creation — so each caller keeps its own
start_span/parent logic and nesting behaviour is unchanged.

Canonical keys, by the question they answer:
  what ran     mcp.server, mcp.tool, mcp.tool.requested (only when it differs),
               mcp.surface (which surface asked), tool.status, duration_ms
  who          user.id (UUID, joins usage_ledger/agent_traces), pcai.key (the
               readable key name)
  which turn   langfuse.session.id, pcai.iteration, pcai.request_id (joins the
               ledger and agent_traces rows)
  what was asked, without the content
               pcai.args.sha256 (16 hex), pcai.args.bytes
  why          pcai.handback.reason (only on handed-back calls)
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

TRACE_NAME = "tool"


def args_fingerprint(args: Any) -> tuple[str, int] | None:
    """(sha256[:16], byte length) for one call's arguments, or None.

    Arguments are never exported — they can carry user data. A digest plus a
    size is enough to spot identical calls and to match the console's bounded
    tool_args preview without copying content into the tracing backend.
    """
    try:
        raw = args if isinstance(args, str) else json.dumps(args, sort_keys=True, separators=(",", ":"))
        body = raw.encode("utf-8", "replace")
        return hashlib.sha256(body).hexdigest()[:16], len(body)
    except Exception:
        return None


def tool_span_attrs(
    *,
    tool: str,
    status: str,
    server: str | None = None,
    ms: int | None = None,
    requested: str | None = None,
    surface: str | None = None,
    user_id: Any = None,
    key_name: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    iteration: int | None = None,
    args: Any = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Attributes for ONE tool span. A fact that is not known is omitted, never
    faked — an absent user.id means "not attributable", not "unknown user"."""
    attrs: dict[str, Any] = {
        "langfuse.observation.type": "span",
        "langfuse.trace.name": TRACE_NAME,
        "mcp.tool": tool,
        "tool.status": status,
    }
    if server:
        attrs["mcp.server"] = server
    if ms is not None:
        attrs["duration_ms"] = int(ms)
    if requested and requested != tool:
        attrs["mcp.tool.requested"] = requested
    if surface:
        attrs["mcp.surface"] = surface
    if user_id:
        attrs["user.id"] = str(user_id)
    if key_name:
        attrs["pcai.key"] = str(key_name)
    if session_id:
        attrs["langfuse.session.id"] = str(session_id)
    if request_id:
        attrs["pcai.request_id"] = str(request_id)
    if iteration is not None:
        attrs["pcai.iteration"] = int(iteration)
    if reason:
        attrs["pcai.handback.reason"] = str(reason)
    if args is not None:
        fingerprint = args_fingerprint(args)
        if fingerprint:
            attrs["pcai.args.sha256"], attrs["pcai.args.bytes"] = fingerprint
    return attrs
