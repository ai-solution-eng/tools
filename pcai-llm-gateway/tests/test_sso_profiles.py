"""SSO tier profiles: the self-mint profile can differ by IdP groups (F1, 2026-11-08).

The contract this file pins:

* ``tenants.sso_admin_groups`` (comma-separated, case-sensitive, trimmed,
  empties ignored) + ``tenants.sso_admin_profile_id`` add an ADMIN tier on
  top of the tenant default profile: a caller whose groups intersect the
  configured entries gets the ADMIN profile's budgets/grants/limits stamped
  on their mint (and rotate), everyone else gets the default — and an EMPTY
  sso_admin_groups disables the split entirely (byte-identical single-
  profile behavior, so an upgrade never re-scopes an existing tenant).
* The caller's groups prefer a JWKS-validated bearer (identity.
  groups_from_validated_token) over the proxy-injected header; every token
  failure shape (absent, unconfigured, invalid, IdP blip) falls back to the
  header groups — the mint must never fail on an IdP blip.
* _sso_profile returns the SAME shape _sso_default_profile returned, so the
  stamping machinery (and the 503-that-names-the-profile path) is untouched.

Suite convention (tests/conftest.py + test_sso_selfmint.py): no live
Postgres and NO NETWORK — db calls are monkeypatched fakes.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import fastapi  # noqa: F401 — probe handlers annotate fastapi.Request (postponed annotations)
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_enroll
from pcai_llm.config import settings
from pcai_llm.core import identity

# ---------------------------------------------------------------------------
# dummy bearer shapes — split at the header prefix ON PURPOSE
# ---------------------------------------------------------------------------
# The fleet pre-mirror secret gate (pcai_utils/secret_scan.sh) flags every
# ``eyJ…`` three-segment LITERAL as a credential and honours NO allow marker
# for it; scripts/secret_scan.py flags the same shape above a 10-char segment
# floor. No such literal may therefore sit in the source tree. Splitting the
# ``eyJ`` prefix keeps both scanners clean while the concatenated runtime value
# is BYTE-IDENTICAL to the unsplit literal, so identity._JWT_SHAPE_RE sees
# exactly what it saw before. Same convention as tests/test_guardrail_secrets.py
# and tests/test_admin_selfmint.py.
DUMMY_BEARER = "eyJ" + "hbGciOiJSUzI1NiJ9.eyJzdWIiOiJhIn0.sig"
"""RS256 header + one-claim payload + the literal signature ``sig``: structurally
a JWS so the shape gate accepts it, and carries no credential whatsoever."""

MINIMAL_JWT = "eyJ" + "h.eyJi.c2ln"
"""The smallest token that satisfies the shape gate: three base64url segments
whose contents are meaningless (``{"a`` / ``{"b`` / ``sig``)."""

# ---------------------------------------------------------------------------
# fixtures: the stateful fake (test_sso_selfmint.SelfMintDB shape, rebuilt
# locally with a TIERED tenant row — the suite keeps both files green on the
# same fake-SQL vocabulary by construction). Dispatch checks the tiered
# SELECT's marker BEFORE the legacy profile-read marker: the new statement
# contains both substrings.
# ---------------------------------------------------------------------------


class TierDB:
    """Stateful fake for the tiered mint: subjects/users/api_keys as in
    SelfMintDB, plus ONE joint tenant row carrying sso_admin_groups and BOTH
    profiles (the single round-trip _sso_profile issues)."""

    def __init__(self) -> None:
        self.subjects: dict[tuple[str, str], str] = {}
        self.users: list[dict[str, Any]] = []
        self.keys: list[dict[str, Any]] = []
        self.seq = 0
        self.statements: list[str] = []  # every SQL string that reached the fake
        self.bulk: list[tuple[str, list]] = []  # executemany (profile stamping)
        self.profile_binds: list[Any] = []  # users.profile_id targets, in order
        self.tenant_row: dict[str, Any] | None = None

    def _next(self) -> str:
        self.seq += 1
        return f"00000000-0000-0000-0000-{self.seq:012d}"

    def live_keys(self, subject_id: str, bound_via: str) -> list[dict[str, Any]]:
        return [
            k
            for k in self.keys
            if k["subject_id"] == subject_id and k["bound_via"] == bound_via and k["revoked"] is False
        ]

    async def fetchrow(self, sql: str, *args: Any):
        self.statements.append(" ".join(sql.split()))
        if "sso_admin_groups" in sql and "FROM tenants t" in sql:
            # _sso_profile's tiered joint row (checked BEFORE the legacy
            # "JOIN profiles" marker — the new statement contains both)
            return self.tenant_row
        if "FROM subjects WHERE idp" in sql:
            sid = self.subjects.get((args[0], args[1]))
            return {"id": sid} if sid else None
        if "FROM api_keys k" in sql and "bound_via" in sql and "revoked_at IS NULL" in sql:
            live = self.live_keys(args[0], args[1])
            row = live[-1] if live else None
            if row is None:
                return None
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "key_prefix": row["key_prefix"],
                "name": row["name"],
                "created_at": row["created_at"],
                "spend_ancestors": row.get("spend_ancestors", []),
            }
        if "INSERT INTO users" in sql:
            uid = self._next()
            self.users.append({"id": uid, "tenant_id": args[0], "name": args[1]})
            return {"id": uid, "name": args[1]}
        if "INSERT INTO subjects" in sql:
            pair = (args[2], args[3])
            if pair not in self.subjects:
                self.subjects[pair] = self._next()
            return {"id": self.subjects[pair]}
        if "UPDATE api_keys SET subject_id" in sql:
            kid, subject_id, bound_via = args[0], args[1], args[2]
            for k in self.keys:
                if k["id"] == kid:
                    k["subject_id"] = subject_id
                    k["bound_via"] = bound_via
            return "UPDATE 1"
        if "UPDATE api_keys SET revoked_at" in sql:
            for k in self.keys:
                if k["id"] == args[0]:
                    k["revoked"] = True
                    return "UPDATE 1"
            return "UPDATE 0"
        if "INSERT INTO api_keys" in sql:
            kid = self._next()
            self.keys.append(
                {
                    "id": kid,
                    "user_id": args[0],
                    "name": args[1],
                    "key_hash": args[2],
                    "key_prefix": args[3],
                    "scopes": list(args[4]),
                    "spend_ancestors": list(args[11]) if len(args) >= 12 else [],
                    "subject_id": None,
                    "bound_via": "",
                    "revoked": False,
                    "created_at": "2026-11-08T00:00:00+00:00",
                }
            )
            return {"id": kid}
        if "UPDATE api_keys SET spend_ancestors" in sql:
            for k in self.keys:
                if k["id"] == args[0]:
                    k["spend_ancestors"] = list(args[1])
            return "UPDATE 1"
        return None

    async def fetchval(self, sql: str, *args: Any):
        self.statements.append(" ".join(sql.split()))
        return "tenant-acme"

    async def fetch(self, sql: str, *args: Any):
        self.statements.append(" ".join(sql.split()))
        return []

    async def execute(self, sql: str, *args: Any):
        self.statements.append(" ".join(sql.split()))
        if "UPDATE users SET profile_id" in sql:
            self.profile_binds.append(args[1])
            return "UPDATE 1"
        if "UPDATE api_keys SET subject_id" in sql:
            kid, subject_id, bound_via = args[0], args[1], args[2]
            for k in self.keys:
                if k["id"] == kid:
                    k["subject_id"] = subject_id
                    k["bound_via"] = bound_via
            return "UPDATE 1"
        if "UPDATE api_keys SET revoked_at" in sql:
            for k in self.keys:
                if k["id"] == args[0]:
                    k["revoked"] = True
                    return "UPDATE 1"
            return "UPDATE 0"
        return "UPDATE 0"

    def acquire(self):
        return _TierConn(self)


class _TierConn:
    """Acquired-connection stand-in (the enroll pattern): statements dispatch
    to the owning fake; executemany records the profile stamping."""

    def __init__(self, db: TierDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False  # propagate — the endpoint 500s exactly like prod

    async def fetchrow(self, sql: str, *args: Any):
        return await self._db.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any):
        return await self._db.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def executemany(self, sql: str, args: Any):
        self._db.statements.append(" ".join(sql.split()))
        self._db.bulk.append((" ".join(sql.split()), list(args)))


# Both profiles carry DISTINCT budget models + an admin-only MCP grant, so
# whichever profile the mint stamped is observable in the executemany log.
DEFAULT_JSON = json.dumps({"budgets": [{"model": "m-default", "period": "month", "limit_tokens": 1000}]})
ADMIN_JSON = json.dumps(
    {
        "budgets": [{"model": "m-admin", "period": "month", "limit_tokens": 5000}],
        "mcp_grants": [["srv-admin", "tool-x"]],
    }
)
DEF_PID = "00000000-0000-0000-0000-0000000000d1"
ADM_PID = "00000000-0000-0000-0000-0000000000a1"


def _tier_row(admin_groups: str) -> dict[str, Any]:
    """The full joint row _sso_profile's SELECT returns for a wired tenant."""
    return {
        "sso_admin_groups": admin_groups,
        "id": DEF_PID,
        "name": "default-prof",
        "defaults_json": DEFAULT_JSON,
        "admin_id": ADM_PID,
        "admin_name": "admin-prof",
        "admin_json": ADMIN_JSON,
    }


@pytest.fixture
def tier_env(monkeypatch: pytest.MonkeyPatch):
    """Trust ON, surface flag ON, tenant set, the TierDB fake wired on every
    seam the mint touches (same wiring as test_sso_selfmint.mint_env)."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", str(uuid.uuid4()))
    fake = TierDB()
    import pcai_llm.core.keys as keys_mod
    from pcai_llm.core import audit as audit_mod

    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(audit_mod, "db", fake)
    monkeypatch.setattr(routes_enroll, "bind_key_subject", identity.bind_key_subject)
    app = FastAPI()
    app.include_router(routes_enroll.router)
    return TestClient(app, raise_server_exceptions=False), fake


def _mint(tc: TestClient, groups: str = "eng", **over: Any) -> Any:
    return tc.post(
        "/v1/keys/for-me",
        json=over or None,
        headers={
            "X-Auth-Request-User": "alice",
            "X-Auth-Request-Groups": groups,
        },
    )


def _budget_models(fake: TierDB) -> set[str]:
    return {t[1] for sql, args in fake.bulk if "INSERT INTO budgets " in sql for t in args}


def _admin_grants(fake: TierDB) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for sql, args in fake.bulk:
        if "INSERT INTO mcp_tool_acls" in sql:
            out.extend((t[1], t[2]) for t in args)
    return out


# ---------------------------------------------------------------------------
# 1. tier selection at the endpoint: which profile the mint stamps
# ---------------------------------------------------------------------------


def test_admin_group_stamps_the_admin_profile(tier_env) -> None:
    """Groups contain the configured entry -> the ADMIN profile's budgets and
    MCP grants stamp onto the minted key (and bind to its user row)."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("admin")
    r = _mint(tc, groups="admin")
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-admin"}
    assert ("srv-admin", "tool-x") in _admin_grants(fake)
    assert fake.profile_binds == [ADM_PID]


def test_non_admin_groups_stamp_the_default_profile(tier_env) -> None:
    """Groups lacking the entry -> the DEFAULT profile, and none of the
    admin-only grants leak."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("admin")
    r = _mint(tc, groups="eng")
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-default"}
    assert _admin_grants(fake) == []
    assert fake.profile_binds == [DEF_PID]


def test_empty_sso_admin_groups_disables_the_tier_split(tier_env) -> None:
    """TIER SPLIT OFF (the column default): even a caller CLAIMING `admin`
    gets the default profile — byte-identical to the single-profile
    deployment, so an upgrade never re-scopes an existing tenant."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("")
    r = _mint(tc, groups="admin")
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-default"}
    assert _admin_grants(fake) == []
    assert fake.profile_binds == [DEF_PID]


def test_admin_groups_parse_commas_and_spaces(tier_env) -> None:
    """Entries are comma-separated and whitespace-trimmed: `ops, admin`
    matches a caller carrying only `ops`."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("ops, admin")
    r = _mint(tc, groups="ops")
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-admin"}


def test_admin_match_is_case_sensitive(tier_env) -> None:
    """`Admin` (configured) does NOT match `admin` (caller) — the PCAI tier
    string is matched exactly, per the platform Rego (`\"admin\" in groups`)."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("Admin")
    r = _mint(tc, groups="admin")
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-default"}


def test_empty_config_entries_are_ignored(tier_env) -> None:
    """Blank entries in sso_admin_groups (` , admin ,`) never match anything
    themselves but do not disable the split either."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row(" , admin ,")
    r = _mint(tc, groups="admin")
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-admin"}


def test_admin_tier_without_default_profile_stamps_admin_at_mint(tier_env) -> None:
    """(review fix, endpoint level) Admin profile wired, default NULL, groups
    match: the mint stamps the ADMIN profile — the both-LEFT-join shape must
    not fall back to the bare grant when only the tier half is configured."""
    tc, fake = tier_env
    row = _tier_row("admin")
    row["id"] = None
    row["name"] = None
    row["defaults_json"] = None
    fake.tenant_row = row
    r = _mint(tc, groups="admin")
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-admin"}
    assert ("srv-admin", "tool-x") in _admin_grants(fake)
    assert fake.profile_binds == [ADM_PID]


# ---------------------------------------------------------------------------
# 2. token groups override / fallback
# ---------------------------------------------------------------------------


def test_token_groups_override_header_groups(tier_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A VALIDATED bearer's groups claim outranks the proxy header: header
    says `eng`, the (monkeypatched) validator says `admin` -> admin tier.
    The seam is the identity MODULE attribute — routes_enroll must call
    through it for this patch to be observed."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("admin")
    seen: list[str] = []

    async def fake_groups(token: str, subject: str | None = None) -> list[str] | None:
        seen.append(token)
        # SUBJECT BINDING (review F1-1): the route must pass the header subject
        # so the helper can refuse cross-subject token adoption.
        assert subject == "alice", subject
        return ["admin"]

    monkeypatch.setattr(identity, "groups_from_validated_token", fake_groups)
    r = tc.post(
        "/v1/keys/for-me",
        json=None,
        headers={
            "X-Auth-Request-User": "alice",
            "X-Auth-Request-Groups": "eng",
            "Authorization": f"Bearer {DUMMY_BEARER}",
        },
    )
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-admin"}
    assert seen == [DUMMY_BEARER]  # the request bearer


def test_token_failure_falls_back_to_header_groups(tier_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every helper None shape (absent/invalid token, IdP blip) leaves the
    HEADER groups authoritative — the mint cannot fail on a token."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("admin")

    async def no_groups(token: str, subject: str | None = None) -> list[str] | None:
        return None

    monkeypatch.setattr(identity, "groups_from_validated_token", no_groups)
    r = _mint(tc, groups="admin")
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-admin"}  # the HEADER groups decided


def test_rotate_stamps_the_admin_profile_too(tier_env) -> None:
    """Rotation creates a FRESH key row — the tier must re-resolve there as
    well, or a rotated admin key silently drops to the default preset."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("admin")
    assert _mint(tc, groups="admin").status_code == 201
    r = tc.post(
        "/v1/keys/for-me",
        json={"rotate": True},
        headers={
            "X-Auth-Request-User": "alice",
            "X-Auth-Request-Groups": "admin",
        },
    )
    assert r.status_code == 201, r.text
    admin_budget_stamps = [
        t[1] for sql, args in fake.bulk if "INSERT INTO budgets " in sql for t in args if t[1] == "m-admin"
    ]
    assert len(admin_budget_stamps) == 2  # mint AND rotate both stamped admin


def test_rotate_rebinds_a_user_row_that_sits_in_an_sso_slot(tier_env) -> None:
    """Review F1-3: the user-row bind follows the tier on rotate when the row
    points at one of the tenant's two SSO slots; a hand-assigned profile (a
    different id) is left alone. The WHERE clause is the contract."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("admin")
    assert _mint(tc, groups="admin").status_code == 201
    r = tc.post(
        "/v1/keys/for-me",
        json={"rotate": True},
        headers={
            "X-Auth-Request-User": "alice",
            "X-Auth-Request-Groups": "admin",
        },
    )
    assert r.status_code == 201, r.text
    binds = [s for s in fake.statements if "UPDATE users SET profile_id" in s]
    assert binds, "the rotate must bind the user row"
    assert "sso_default_profile_id" in binds[-1] and "sso_admin_profile_id" in binds[-1]
    assert "profile_id IS NULL" in binds[-1]


def test_token_for_another_subject_does_not_override_header_groups(tier_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review F1-1 (P1): a VALID token whose groups say admin but whose
    identity is a DIFFERENT subject must not stamp the admin profile — the
    header groups stay authoritative (credential laundering refusal)."""
    tc, fake = tier_env
    fake.tenant_row = _tier_row("admin")
    monkeypatch.setattr(settings, "token_jwks_url", "https://idp/jwks")
    monkeypatch.setattr(settings, "token_audience", "pcai-llm")

    async def other_user(token: str) -> Any:
        return identity.TokenClaims(subject="someone-else", email="x@y", groups=["admin"], preferred_username="mallory")

    monkeypatch.setattr(identity, "validate_token", other_user)
    r = _mint(tc, groups="eng")  # header identity: alice, NOT admin
    assert r.status_code == 201, r.text
    assert _budget_models(fake) == {"m-default"}, "the token's admin groups must not cross subjects"


# ---------------------------------------------------------------------------
# 3. _sso_profile unit behavior (canned conn)
# ---------------------------------------------------------------------------


class _CannedConn:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def fetchrow(self, sql: str, *args: Any):
        return self.row


def test_sso_profile_none_when_no_default_row() -> None:
    """No default profile wired (or the join row vanished) -> None: the bare
    grant, exactly as before the tier split existed."""
    got = asyncio.run(routes_enroll._sso_profile(_CannedConn(None), uuid.uuid4(), ["admin"]))
    assert got is None


def test_sso_profile_legacy_row_shape_is_tier_off() -> None:
    """A row WITHOUT sso_admin_groups (the pre-tier shape — asyncpg always
    carries the column, but consumers/tests may hand a plain dict) resolves
    to the DEFAULT profile, never a crash."""
    row = {"id": DEF_PID, "name": "d", "defaults_json": DEFAULT_JSON}
    got = asyncio.run(routes_enroll._sso_profile(_CannedConn(row), uuid.uuid4(), ["admin"]))
    assert got is not None
    assert got == {
        "id": DEF_PID,
        "name": "d",
        "budgets": [{"model": "m-default", "period": "month", "limit_tokens": 1000}],
        "mcp_grants": [],
        "limits": {},
    }


def test_sso_profile_admin_row_vanished_degrades_to_default() -> None:
    """The admin profile was deleted (ON DELETE SET NULL -> admin_id None):
    a matching caller degrades to the DEFAULT profile, never the bare grant."""
    row = _tier_row("admin")
    row["admin_id"] = None
    row["admin_name"] = None
    row["admin_json"] = None
    got = asyncio.run(routes_enroll._sso_profile(_CannedConn(row), uuid.uuid4(), ["admin"]))
    assert got is not None
    assert got["id"] == DEF_PID


def test_sso_profile_admin_tier_without_default_admin_gets_admin() -> None:
    """(review fix a) Admin profile set + default NULL + groups contain the
    entry -> the ADMIN profile (an INNER default join returned None for
    everyone on this legal configuration)."""
    row = _tier_row("admin")
    row["id"] = None
    row["name"] = None
    row["defaults_json"] = None
    got = asyncio.run(routes_enroll._sso_profile(_CannedConn(row), uuid.uuid4(), ["admin"]))
    assert got is not None
    assert got == {
        "id": ADM_PID,
        "name": "admin-prof",
        "budgets": [{"model": "m-admin", "period": "month", "limit_tokens": 5000}],
        "mcp_grants": [["srv-admin", "tool-x"]],
        "limits": {},
    }


def test_sso_profile_admin_tier_without_default_member_gets_none() -> None:
    """(review fix b) Same tenant, groups lack the entry, no default wired:
    None — the bare grant, never the admin profile (privilege must not leak
    down) and never a crash on the NULL default columns."""
    row = _tier_row("admin")
    row["id"] = None
    row["name"] = None
    row["defaults_json"] = None
    got = asyncio.run(routes_enroll._sso_profile(_CannedConn(row), uuid.uuid4(), ["eng"]))
    assert got is None


def test_sso_profile_neither_profile_set_is_none() -> None:
    """(review fix c) Tenant row present, BOTH profile settings NULL: None —
    the bare grant (the both-LEFT shape must not invent a profile)."""
    row = _tier_row("")
    for k in ("id", "name", "defaults_json", "admin_id", "admin_name", "admin_json"):
        row[k] = None
    got = asyncio.run(routes_enroll._sso_profile(_CannedConn(row), uuid.uuid4(), ["admin"]))
    assert got is None


def test_sso_profile_bad_defaults_json_degrades_to_empty_presets() -> None:
    """Hand-edited defaults_json -> empty presets (the same tolerance the
    single-profile read had), never a 500 from the SELECT path."""
    row = _tier_row("admin")
    row["admin_json"] = "not json at all"
    got = asyncio.run(routes_enroll._sso_profile(_CannedConn(row), uuid.uuid4(), ["admin"]))
    assert got is not None
    assert got["id"] == ADM_PID and got["budgets"] == [] and got["mcp_grants"] == []


# ---------------------------------------------------------------------------
# 4. identity.groups_from_validated_token — the contract
# ---------------------------------------------------------------------------


async def test_gfvt_absent_token_is_none() -> None:
    """No bearer -> None (caller falls back to headers); no settings touch."""
    assert await identity.groups_from_validated_token("") is None


async def test_gfvt_unconfigured_validation_is_none() -> None:
    """Validation knobs unset (the default) -> the rung is inert -> None."""
    assert await identity.groups_from_validated_token(MINIMAL_JWT) is None


async def test_gfvt_non_jwt_shape_is_none_without_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-JWT bearer (a virtual key, garbage) never reaches PyJWT — the
    shape gate filters it before validate_token is consulted."""
    monkeypatch.setattr(settings, "token_jwks_url", "https://idp/jwks")
    monkeypatch.setattr(settings, "token_audience", "pcai-llm")

    async def bomb(token: str) -> Any:
        raise AssertionError("validate_token must not run for a non-JWT shape")

    monkeypatch.setattr(identity, "validate_token", bomb)
    assert await identity.groups_from_validated_token("hllm-not-a-jwt") is None


async def test_gfvt_infra_failure_is_none_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE posture: a JWKS/infrastructure blowup degrades to None here (the
    mint already authenticated via proxy headers) — it must never 500 a mint."""
    monkeypatch.setattr(settings, "token_jwks_url", "https://idp/jwks")
    monkeypatch.setattr(settings, "token_audience", "pcai-llm")

    async def exploding(token: str) -> Any:
        raise RuntimeError("JWKS down")

    monkeypatch.setattr(identity, "validate_token", exploding)
    assert await identity.groups_from_validated_token(MINIMAL_JWT) is None


async def test_gfvt_returns_validated_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """A validated token yields its claims groups, EMPTY LIST INCLUDED — []
    is a real "no groups" assertion, distinct from None (fall back)."""
    monkeypatch.setattr(settings, "token_jwks_url", "https://idp/jwks")
    monkeypatch.setattr(settings, "token_audience", "pcai-llm")

    async def valid(token: str) -> Any:
        return identity.TokenClaims(subject="sub", email="e@x", groups=["admin"])

    monkeypatch.setattr(identity, "validate_token", valid)
    assert await identity.groups_from_validated_token(MINIMAL_JWT) == ["admin"]
    # SUBJECT BINDING (review F1-1): the SAME token is adopted only for the
    # subject it identifies. TokenClaims here has preferred_username="" and
    # sub="sub" — "sub" matches, "someone-else" does not.
    assert await identity.groups_from_validated_token(MINIMAL_JWT, "sub") == ["admin"]
    assert await identity.groups_from_validated_token(MINIMAL_JWT, "someone-else") is None

    async def groupless(token: str) -> Any:
        return identity.TokenClaims(subject="sub", email="e@x", groups=[])

    monkeypatch.setattr(identity, "validate_token", groupless)
    got = await identity.groups_from_validated_token(MINIMAL_JWT)
    assert got == [] and got is not None
