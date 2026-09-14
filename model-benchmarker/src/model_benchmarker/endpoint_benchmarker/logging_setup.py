"""Logging setup for the benchmarker.

All human output goes through the ``logging`` module (timestamps + levels),
which is the main improvement over the print()-based original: ``-v`` turns
on per-request DEBUG lines, ``--quiet`` silences progress, and ``--log-file``
tees the whole run to disk next to the JSON artifact.
"""

from __future__ import annotations

import logging
import sys

_FMT = "%(asctime)s %(levelname)-7s %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging(verbose: bool = False, quiet: bool = False, log_file: str | None = None) -> None:
    """Configure the root logger once, from CLI flags.

    Levels: DEBUG (per-request lines) < INFO (progress + summaries) <
    WARNING (quiet mode: only warnings/errors/results of consequence —
    the final summary is always printed on stdout via print(), not logging,
    so redirecting stderr never hides results).
    """
    level = logging.DEBUG if verbose else (logging.WARNING if quiet else logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        import os

        parent = os.path.dirname(os.path.abspath(log_file))
        if parent:
            os.makedirs(parent, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=level, format=_FMT, datefmt=_DATEFMT, handlers=handlers, force=True)
    # The MCP SDK / httpx log every wire request at INFO — keep them quiet
    # unless the operator explicitly asked for verbose.
    for noisy in ("httpx", "httpcore", "mcp", "asyncio", "httpx2", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if verbose else logging.WARNING)


log = logging.getLogger("endpoint_benchmarker")
