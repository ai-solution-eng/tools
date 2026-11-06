"""Token shadow-keys under audit r5 #5 (2026-10-30): the ABSENT/REFUSED
split in resolve_shadow_key.

resolve_shadow_key read the None from find_shadow_key as "first sighting" in
EVERY case, so an EXPIRED or console-REVOKED shadow row self-healed into a
fresh key on the next sighting — and _provision_shadow_key's INSERT carries
no expires_at and an allow-all allowed_models, so the healed key was
non-expiring and unbudgeted: the "console revocation is the off switch"
docstring promise, inverted. These pins hold the split: a refused row is
TERMINAL (None, no INSERT); only a genuinely absent subject provisions,
exactly once.

Fake fidelity note (the phaseb ShadowDB/ExpiryShadowDB precedent): the fake
serves the newest shadow row REGARDLESS of revoked/expired state — the real
table keeps the row; the mapper's gate and resolve's _SHADOW_KEY_ANY_SQL
probe are what read it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import identity

_TENANT = "22222222-2222-2222-2222-222222222222"


class HonestShadowDB:
    """Stateful fake modeling subjects UNIQUE (idp, subject), users, and
    api_keys for the shadow flow — serving refused rows honestly (see the
    module docstring). Every provisioned key is recorded, so INSERT counts
    are assertable."""

    def __init__(self) -> None:
        self.subjects: dict[tuple[str, str], str] = {}  # (idp, subject) -> id
        self.users: list[dict[str, Any]] = []
        self.keys: list[dict[str, Any]] = []
        self.seq = 0

    def _next(self) -> str:
        self.seq += 1
        return f"00000000-0000-0000-0000-{self.seq:012d}"

    async def fetchrow(self, sql: str, *args: Any):
        # find_shadow_key AND the r5 probe share this statement shape
        # ("FROM api_keys k" + "bound_via"): serve the newest row regardless
        # of state — refusal is the mapper's/probe caller's job, not ours.
        if "FROM api_keys k" in sql and "bound_via" in sql:
            sid = self.subjects.get((args[0], args[1]))
            rows = [k for k in self.keys if k["subject_id"] == sid and k["bound_via"] == args[2]]
            if not rows:
                return None
            newest = rows[-1]
            return {
                "id": newest["id"],
                "user_id": newest["user_id"],
                "name": newest["name"],
                "key_prefix": "hllm-XX",
                "scopes": newest["scopes"],
                "allowed_models": [],
                "rate_limit_rpm": 60,
                "rate_limit_tpm": 100_000,
                "max_concurrent": 0,
                "expires_at": newest.get("expires_at"),
                "revoked_at": datetime.now(UTC) if newest.get("revoked") else None,
                "relay_default": False,
                "stream_pcai_events": False,
                "offpeak_fraction": None,
            }
        if "FROM subjects WHERE idp" in sql:
            sid = self.subjects.get((args[0], args[1]))
            return {"id": sid} if sid else None
        if "SELECT id FROM users WHERE tenant_id" in sql:
            for u in self.users:
                if u["tenant_id"] == args[0] and u["name"] == args[1]:
                    return {"id": u["id"]}
            return None
        if "INSERT INTO users" in sql:
            uid = self._next()
            self.users.append({"id": uid, "tenant_id": args[0], "name": args[1]})
            return {"id": uid, "name": args[1]}
        if "INSERT INTO subjects" in sql:
            pair = (args[1], args[2])  # the shadow provisioner's arg shape
            if pair not in self.subjects:
                self.subjects[pair] = self._next()
            return {"id": self.subjects[pair]}
        if "INSERT INTO api_keys" in sql:
            kid = self._next()
            self.keys.append(
                {
                    "id": kid,
                    "user_id": args[0],
                    "name": args[1],
                    "scopes": args[4],
                    "subject_id": args[8],
                    "bound_via": args[9],
                    "revoked": False,
                }
            )
            return {"id": kid}
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


@pytest.fixture(autouse=True)
def _shadow_env(monkeypatch: pytest.MonkeyPatch):
    """A valid tenant baseline (shadow_keys_enabled needs flag AND tenant);
    each test flips the flag itself."""
    monkeypatch.setattr(settings, "token_shadow_tenant_id", _TENANT)
    monkeypatch.setattr(settings, "token_shadow_keys", False)


def test_r5_revoked_shadow_row_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Console-REVOKED row: the next sighting gets None and NO new key —
    revocation is the off switch, not a re-provision trigger (the pre-fix
    behavior minted a fresh non-expiring, unbudgeted key per sighting)."""
    fake = HonestShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    first = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert first is not None
    fake.keys[0]["revoked"] = True
    assert asyncio.run(identity.find_shadow_key("user-alice-1")) is None
    assert asyncio.run(identity.resolve_shadow_key("user-alice-1", None)) is None
    assert len(fake.keys) == 1, "a revoked row must not re-provision"


def test_r5_expired_shadow_row_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """EXPIRED row: the same terminal rule — the expired row must not be
    self-healed into a fresh key whose INSERT carries no expires_at."""
    fake = HonestShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    first = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert first is not None
    fake.keys[0]["expires_at"] = datetime(2020, 1, 1, tzinfo=UTC)
    assert asyncio.run(identity.find_shadow_key("user-alice-1")) is None
    assert asyncio.run(identity.resolve_shadow_key("user-alice-1", None)) is None
    assert len(fake.keys) == 1, "an expired row must not re-provision"


def test_r5_genuinely_absent_provisions_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Genuinely ABSENT subject + flag on: provisions exactly ONE key (the
    split must not chill first provisioning), and the second sighting reuses
    it instead of inserting again."""
    fake = HonestShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    first = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert first is not None
    assert first.scopes == ["proxy", "mcp"] and first.name == "token-shadow"
    assert len(fake.keys) == 1 and len(fake.users) == 1
    again = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert again is not None and again.id == first.id
    assert len(fake.keys) == 1, "the second sighting must reuse, not re-provision"
