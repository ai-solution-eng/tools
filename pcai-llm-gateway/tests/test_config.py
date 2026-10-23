"""Config + entrypoint wiring tests for the uvicorn server knobs:

PCAI_LLM_KEEP_ALIVE_TIMEOUT_S / PCAI_LLM_LIMIT_CONCURRENCY must parse
through pydantic-settings (12-factor: every knob is an env var) and reach
uvicorn.run() — the keep-alive timeout ALWAYS, the concurrency cap only
when > 0 (uvicorn treats None as unlimited; passing 0 would be wrong).
"""

from __future__ import annotations

from typing import Any

import pytest


def test_keep_alive_timeout_default_and_override(monkeypatch):
    monkeypatch.setenv("PCAI_LLM_KEEP_ALIVE_TIMEOUT_S", "120")
    from pcai_llm.config import Settings

    s = Settings()
    assert s.keep_alive_timeout_s == 120


def test_limit_concurrency_default_zero_and_override(monkeypatch):
    from pcai_llm.config import Settings

    assert Settings().limit_concurrency == 0
    monkeypatch.setenv("PCAI_LLM_LIMIT_CONCURRENCY", "500")
    assert Settings().limit_concurrency == 500


def test_main_passes_keep_alive_and_omits_zero_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest.py sets PCAI_LLM_DATABASE_URL before any pcai_llm import —
    # import main (and its settings) inside the test, never at module time.
    import pcai_llm.main as main_mod

    # main() reads the module-level settings SINGLETON, which may have been
    # constructed while an earlier test's env overrides were active — pin
    # the values under test explicitly (monkeypatch restores them after).
    monkeypatch.setattr(main_mod.settings, "keep_alive_timeout_s", 75)
    monkeypatch.setattr(main_mod.settings, "limit_concurrency", 0)

    calls: list[dict[str, Any]] = []

    def recorder(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("uvicorn.run", recorder)
    # No-op the app factory: main() must wire KNOBS, not compose the app.
    monkeypatch.setattr(main_mod, "create_app", lambda: object())
    main_mod.main()
    # Default keep-alive always passes through ...
    assert calls[0]["timeout_keep_alive"] == 75
    # ... and a 0 cap is OMITTED (key absent — uvicorn's unbounded default).
    assert "limit_concurrency" not in calls[0]


def test_main_passes_concurrency_cap_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.main as main_mod

    # Pin the singleton (same ordering caveat as the default-path test):
    # a positive cap must reach uvicorn.run() as limit_concurrency.
    monkeypatch.setattr(main_mod.settings, "keep_alive_timeout_s", 75)
    monkeypatch.setattr(main_mod.settings, "limit_concurrency", 500)

    calls: list[dict[str, Any]] = []

    def recorder(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("uvicorn.run", recorder)
    monkeypatch.setattr(main_mod, "create_app", lambda: object())
    main_mod.main()
    assert calls[0]["timeout_keep_alive"] == 75
    assert calls[0]["limit_concurrency"] == 500
