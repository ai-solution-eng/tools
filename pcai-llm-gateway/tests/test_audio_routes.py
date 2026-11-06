"""Audio passthrough: /v1/audio/speech (TTS) and /v1/audio/transcriptions (ASR).

The routes are transparent byte proxies over the fleet's voice pools: the
request body leaves exactly as it arrived (JSON for speech, multipart for
transcriptions — the audio is never parsed or re-encoded) and the upstream's
status, content-type and body bytes come back the same way. What is asserted
here is the control plane around the call:

* model resolution — JSON body for speech; query > X-PCAI-Model header >
  multipart scan for transcriptions, with a loud 400 when none yields a name;
* the ACL (allowed_models) and the voice pool-type gate (400, not 404);
* the upstream bytes are relayed VERBATIM in both directions;
* ONE ledger row per request carrying the token ESTIMATE (TTS: chars //
  audio_tts_chars_per_token; ASR: body bytes at ~32 kB/s x
  audio_asr_tokens_per_second) under the endpoint labels audio.speech /
  audio.transcriptions, completion 0.

No live Postgres: the module-db monkeypatch pattern from tests/test_proxy.py.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.routing.router import Endpoint, Router

VOICE = "test-voice"
VOICE2 = "test-voice-2"
CHAT = "test-chat"
AUTH = {"Authorization": "Bearer hllm-x"}


class FakeDB:
    """Budget/rate stubs; records every execute() as (sql, args)."""

    def __init__(self) -> None:
        self.settled: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def execute(self, sql: str, *args: Any):
        self.settled.append((sql, args))
        return "INSERT 0 1"

    def ledger_rows(self) -> list[tuple]:
        """Only the usage_ledger INSERTs (settle also issues window updates)."""
        return [args for sql, args in self.settled if "INSERT INTO usage_ledger" in sql]


def _key(allowed_models: list[str] | None = None) -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=allowed_models or [],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


def _multipart_body(model_value: str | None, audio: bytes) -> tuple[bytes, str]:
    """Hand-assembled multipart body (model field optional) + content-type."""
    boundary = "----pcai-test-boundary"
    chunks: list[bytes] = []
    if model_value is not None:
        chunks.append(
            f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="model"\r\n'
            + b"\r\n"
            + model_value.encode()
            + b"\r\n"
        )
    chunks.append(
        f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
        + b"Content-Type: audio/wav\r\n"
        + b"\r\n"
    )
    body = b"".join(chunks) + audio + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Fake auth + fake db + two voice pools and one chat pool (mock upstream)."""
    key = _key()

    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)

    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    monkeypatch.setattr(cascade_mod, "db", fake_db)
    monkeypatch.setattr(costs_mod, "db", fake_db)

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/audio/speech"):
            return httpx2.Response(200, content=b"RIFF-WAV-BYTES", headers={"Content-Type": "audio/wav"})
        return httpx2.Response(200, json={"text": "hello world"})

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(
        VOICE, [Endpoint(id="vp1", model_name=VOICE, base_url="http://fake-a/v1", model_type="voice")]
    )
    mock_router.set_endpoints(
        VOICE2, [Endpoint(id="vp2", model_name=VOICE2, base_url="http://fake-b/v1", model_type="voice")]
    )
    mock_router.set_endpoints(
        CHAT, [Endpoint(id="cp1", model_name=CHAT, base_url="http://fake-c/v1", model_type="chat")]
    )
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    app = FastAPI()
    app.include_router(routes_proxy.router)
    return TestClient(app, raise_server_exceptions=False), fake_db, seen, key


# ---------------------------------------------------------------------------
# /v1/audio/speech (TTS)
# ---------------------------------------------------------------------------


def test_speech_relays_body_bytes_verbatim(env) -> None:
    tc, fake_db, seen, _ = env
    body = json.dumps({"model": VOICE, "input": "hello world", "voice": "alloy", "response_format": "wav"}).encode()
    r = tc.post(
        "/v1/audio/speech",
        content=body,
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 200, r.text
    # Upstream got EXACTLY the client's bytes (and its content-type).
    assert seen[-1].content == body, "request body must be forwarded byte-for-byte"
    assert seen[-1].url.path.endswith("/audio/speech")
    assert seen[-1].headers["content-type"] == "application/json"
    # The response is the upstream's bytes + content-type, untouched.
    assert r.content == b"RIFF-WAV-BYTES"
    assert r.headers["content-type"] == "audio/wav"
    # ONE ledger row, the estimate (11 chars // 4), completion 0, label audio.speech.
    rows = fake_db.ledger_rows()
    assert len(rows) == 1, f"expected exactly one settle, got {rows}"
    row = rows[0]
    assert row[1] == VOICE  # model
    assert row[2] == "audio.speech"  # endpoint label
    assert row[3] == len("hello world") // settings.audio_tts_chars_per_token  # prompt estimate
    assert row[4] == 0  # completion
    assert row[8] == "ok"  # status


def test_speech_missing_model_400(env) -> None:
    tc, fake_db, _, _ = env
    r = tc.post(
        "/v1/audio/speech",
        content=json.dumps({"input": "hi"}).encode(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert fake_db.ledger_rows() == []  # rejected before admission: no row


def test_speech_not_json_400(env) -> None:
    tc, fake_db, _, _ = env
    r = tc.post(
        "/v1/audio/speech",
        content=b"not json at all",
        headers={**AUTH, "Content-Type": "text/plain"},
    )
    assert r.status_code == 400
    assert fake_db.ledger_rows() == []


def test_speech_wrong_pool_type_400(env) -> None:
    tc, fake_db, _, _ = env
    r = tc.post(
        "/v1/audio/speech",
        content=json.dumps({"model": CHAT, "input": "hi"}).encode(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "not voice" in r.text
    assert fake_db.ledger_rows() == []


def test_speech_unregistered_model_404(env) -> None:
    tc, _, _, _ = env
    r = tc.post(
        "/v1/audio/speech",
        content=json.dumps({"model": "ghost", "input": "hi"}).encode(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /v1/audio/transcriptions (ASR)
# ---------------------------------------------------------------------------


def _asr_post(tc: TestClient, body: bytes, ctype: str, **extra: Any) -> Any:
    headers = dict(AUTH)
    headers["Content-Type"] = ctype
    headers.update({k: v for k, v in extra.items() if k != "model_query"})
    params = {"model": extra["model_query"]} if "model_query" in extra else None
    return tc.post("/v1/audio/transcriptions", content=body, headers=headers, params=params)


def test_transcriptions_model_from_query(env) -> None:
    tc, fake_db, seen, _ = env
    audio = bytes(range(256)) * 128  # binary-ish, 32 KiB
    body, ctype = _multipart_body(None, audio)
    r = _asr_post(tc, body, ctype, model_query=VOICE)
    assert r.status_code == 200, r.text
    assert r.json()["text"] == "hello world"
    # Verbatim in both directions.
    assert seen[-1].content == body
    assert seen[-1].url.path.endswith("/audio/transcriptions")
    assert seen[-1].headers["content-type"] == ctype
    # Ledger: the duration estimate at 32 kB/s x tokens/sec, label audio.transcriptions.
    rows = fake_db.ledger_rows()
    assert len(rows) == 1
    row = rows[0]
    expected = max(1, int(max(1, len(body) / 32000) * settings.audio_asr_tokens_per_second))
    assert row[1] == VOICE
    assert row[2] == "audio.transcriptions"
    assert row[3] == expected
    assert row[4] == 0
    assert row[8] == "ok"


def test_transcriptions_model_from_header(env) -> None:
    tc, fake_db, _, _ = env
    body, ctype = _multipart_body(None, b"\x00" * 32000)
    r = tc.post(
        "/v1/audio/transcriptions",
        content=body,
        headers={**AUTH, "Content-Type": ctype, "X-PCAI-Model": VOICE},
    )
    assert r.status_code == 200, r.text
    assert fake_db.ledger_rows()[0][1] == VOICE


def test_transcriptions_model_from_multipart_field(env) -> None:
    tc, fake_db, seen, _ = env
    body, ctype = _multipart_body(VOICE, b"\x00" * 32000)
    r = _asr_post(tc, body, ctype)  # no query, no header: the multipart scan must find it
    assert r.status_code == 200, r.text
    assert fake_db.ledger_rows()[0][1] == VOICE
    # The WHOLE body (model field included) is what gets forwarded.
    assert seen[-1].content == body


def test_transcriptions_query_wins_over_header(env) -> None:
    tc, fake_db, seen, _ = env
    body, ctype = _multipart_body(VOICE2, b"\x00" * 32000)  # multipart says VOICE2
    r = tc.post(
        "/v1/audio/transcriptions",
        content=body,
        headers={**AUTH, "Content-Type": ctype, "X-PCAI-Model": VOICE},
        params={"model": VOICE2},
    )
    assert r.status_code == 200, r.text
    # query > header: VOICE2's pool (fake-b) served.
    assert seen[-1].url.host == "fake-b"
    assert fake_db.ledger_rows()[0][1] == VOICE2


def test_transcriptions_missing_model_400(env) -> None:
    tc, fake_db, _, _ = env
    body, ctype = _multipart_body(None, b"\x00" * 32000)
    r = _asr_post(tc, body, ctype)  # no query, no header, no multipart field
    assert r.status_code == 400
    assert "model" in r.text.lower()
    assert fake_db.ledger_rows() == []


def test_transcriptions_wrong_pool_type_400(env) -> None:
    tc, fake_db, _, _ = env
    body, ctype = _multipart_body(CHAT, b"\x00" * 32000)
    r = _asr_post(tc, body, ctype)
    assert r.status_code == 400
    assert "not voice" in r.text
    assert fake_db.ledger_rows() == []


def test_transcriptions_acl_enforced(env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, fake_db, _, _ = env
    # A key allowed ONLY test-voice-2 must be refused for test-voice (403,
    # before admission: no ledger row, no upstream call).
    restricted = _key(allowed_models=[VOICE2])
    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return restricted

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    body, ctype = _multipart_body(VOICE, b"\x00" * 32000)
    r = _asr_post(tc, body, ctype)
    assert r.status_code == 403
    assert fake_db.ledger_rows() == []
