"""Entrypoint: run the gateway under uvicorn (console script `pcai-llm-gateway`)."""

from __future__ import annotations

from typing import Any

import uvicorn

from .app import create_app
from .config import settings


def main() -> None:
    """Start uvicorn with the composed app; host/port/log level and the
    uvicorn connection knobs (``timeout_keep_alive`` always, and
    ``limit_concurrency`` only when a cap is set) come from settings.

    keep_alive_timeout_s / limit_concurrency are 12-factor env vars
    (PCAI_LLM_KEEP_ALIVE_TIMEOUT_S / PCAI_LLM_LIMIT_CONCURRENCY): 0 for
    limit_concurrency means "no cap" — uvicorn treats None as unlimited and
    an explicit 0 as "shed everything", so the key is OMITTED at 0
    (byte-identical default render: the no-cap path never reaches uvicorn).
    """
    kwargs: dict[str, Any] = {
        "timeout_keep_alive": settings.keep_alive_timeout_s,
    }
    if settings.limit_concurrency > 0:
        # Only a POSITIVE cap is passed through: 0 means disabled (unbounded,
        # uvicorn's default), and passing 0 to uvicorn would be wrong.
        kwargs["limit_concurrency"] = settings.limit_concurrency
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        **kwargs,
    )


if __name__ == "__main__":
    main()
