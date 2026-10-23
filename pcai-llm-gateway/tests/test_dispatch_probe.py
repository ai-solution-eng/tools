"""Regression: `?wait_s=nan` hung the long-poll forever (audit finding 11 —
NaN survives both min/max clamps, a NaN deadline never satisfies
`monotonic() >= deadline`), and `ttl_s: NaN` minted a never-live offer whose
JSONResponse carried bare `NaN` (invalid RFC 8259). Both now 400."""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import auth as auth_mod
from pcai_llm.api import routes_dispatch
from pcai_llm.config import settings
from pcai_llm.core.keys import KeyRecord


def _key() -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="probe",
        key_prefix="hllm-probe",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=600,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


@pytest.fixture
def tc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "client_tool_dispatch", True)
    k = _key()

    async def pinned_auth(p: str) -> KeyRecord:
        return k

    monkeypatch.setattr(auth_mod, "authenticate", pinned_auth)
    app = FastAPI()
    app.include_router(routes_dispatch.router)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, k


def test_wait_s_nan_400(tc) -> None:
    c, _key = tc
    r = c.get("/v1/dispatch/poll?wait_s=nan", headers={"Authorization": "Bearer x"})
    assert r.status_code == 400, r.text
    # NOTE: inf clamps to MAX_POLL_WAIT_S (60s) — pre-existing clamp
    # semantics, not part of the NaN fix; not asserted here (would take 60s).


def test_ttl_s_nan_400(tc) -> None:
    c, _key = tc
    r = c.post(
        "/v1/dispatch/register",
        json={"tools": [{"name": "bash", "description": "x"}], "ttl_s": "nan"},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 400, r.text
    r = c.post(
        "/v1/dispatch/register",
        json={"tools": [{"name": "bash", "description": "x"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200, r.text
