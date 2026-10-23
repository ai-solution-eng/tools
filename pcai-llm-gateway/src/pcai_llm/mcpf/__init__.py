"""MCP federation package: gateway-level /mcp surface + ACL logic.

Exports the pure ACL functions (tests import from here) and the routes
module that mounts the streamable-http /mcp ASGI app.
"""

from __future__ import annotations

from .federation import DEFAULT_DENY, is_allowed, load_grants, visible_tools

__all__ = ["DEFAULT_DENY", "is_allowed", "load_grants", "visible_tools"]
