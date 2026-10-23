"""API layer: auth dependencies and data-plane proxy routes."""

from __future__ import annotations

from .auth import require_key, require_scope
from .routes_proxy import router as proxy_router

__all__ = ["proxy_router", "require_key", "require_scope"]
