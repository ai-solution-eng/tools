"""Shared pytest fixtures.

The unit suite avoids a live Postgres: db calls are monkeypatched. The
federation ACL functions are pure by design (see mcpf/federation.py) and
need no fixtures at all.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

# Settings requires a database_url at import time (fail-loud config). Unit
# tests never touch Postgres — set a dummy before any pcai_llm import.
os.environ.setdefault("PCAI_LLM_DATABASE_URL", "postgresql://test:test@localhost:5432/test")


@pytest.fixture(scope="session")
def event_loop() -> Any:
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
