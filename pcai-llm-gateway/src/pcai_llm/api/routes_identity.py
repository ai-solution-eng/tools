"""GET /v1/identity — the whoami probe the SPA calls BEFORE it has a key.

Brief §Z2 (sso-selfmint-BRIEF.md, ratified 2026-10-06). PUBLIC route BY
DESIGN: the SPA boots without a key and must learn (a) whether the
deployment would let it mint one via POST /v1/keys/for-me (flags.self_mint)
and (b) who the request resolves to once it HAS a key. There is no auth
dependency on the route, and there is nothing here to authorize — the
response only ever describes the CALLING request back to itself.

WHAT it reports (exact keys, no more — the anti-oracle contract):
{"authenticated": bool, "subject": str|null, "idp": str|null,
 "key_prefix": str|null, "scopes": [str], "flags": {"self_mint": bool}}

- ``authenticated`` = a LEDGER KEY anchors this request (the key
  ``require_key`` would admit — bearer key, X-API-Key key, or a validated
  token's shadow key). A trusted browser-header identity alone is
  authenticated=false: enforcement still 401s it until a key exists; the
  probe surfaces its subject so the SPA can show who it is pre-mint.
- ``idp`` comes from the key's subjects binding (oauth2-proxy / keycloak /
  enrollment / self-asserted / admin-asserted — whatever the ledger
  stamped); browser-header and token-only identities have no binding row,
  so idp is null there.
- UNRESOLVED-anonymous (no credential presented, or a credential the
  enforcement path would REFUSE) is EXACTLY {"authenticated": false,
  "subject": null, "idp": null, "key_prefix": null, "scopes": [],
  "flags": {"self_mint": false}} — six top-level keys, one flag, never
  more (the MM RAG anonymous-shape lesson; a refused shape must not become
  a distinguishable oracle). An unbound key resolves authenticated=true
  with its own prefix/scopes and null subject — the caller's own key.
- Fail-closed: resolver/DB/JWKS exceptions → 503. The anonymous shape is
  for RESOLVED-anonymous, never for infrastructure failure.

RESOLUTION (the anti-drift contract): ``resolve_request_identity``
DELEGATES to Z1's shared resolver ``api.auth.resolve_bearer_key`` (the
ONE envelope+precedence resolver require_key and the /mcp wrapper both
run) — the probe literally runs the enforcement resolution, so the two
surfaces cannot drift. Z2-local additions on top, exactly the ones Z1's
helper deliberately leaves to its caller:

- a refusal (HTTPException 401: missing bearer, dead key, dead pair) maps
  to ``(None, <state caller>)`` — the probe describes, it does not
  authorize; the only caller Z1 resolves before refusing (a VALID token
  with no shadow key) rides ``request.state.caller`` and is reported;
- infrastructure failures INSIDE Z1's token ladder arrive wrapped as
  HTTPException(401, "token validation unavailable") — the probe
  re-maps that one detail to 503 (fail-closed; the detail string is the
  stable refusal vocabulary auth.py and routes_mcp share, pinned by
  tests/test_identity_probe.py);
- the key's OWN binding is the reported identity once a key resolved
  (rungs 3-4: ``resolve_caller(key)`` WITHOUT headers — the proxy always
  injects browser headers, which must not shadow the ledger's word);
- the browser-header rung (ladder rung 1) fills in when nothing
  key-shaped resolved — the rung enforcement never reaches (it 401s a
  keyless request first) but the probe exists to show.
"""

from __future__ import annotations

import uuid
from typing import Any

import fastapi
from fastapi import APIRouter, HTTPException, Request

from ..config import settings
from ..core import identity
from ..core.identity import (
    IDP_OAUTH2_PROXY,
    VIA_SSO_CREATE,
    Caller,
    browser_identity_from_headers,
    resolve_caller,
    settings_trust_proxy_identity,
)
from ..core.keys import KeyRecord, authenticate
from . import auth as auth_mod

router = APIRouter(prefix="/v1", tags=["identity"])

_ANONYMOUS_DETAIL = "identity resolution unavailable"
_TOKEN_INFRA_DETAIL = "token validation unavailable"
"""The refusal detail auth.py's token ladder raises for JWKS/IdP
INFRASTRUCTURE failure (its fail-closed posture). The probe re-maps it to
503 — an unavailable IdP must never read as "you are anonymous". Pinned
end-to-end by test_resolver_exception_wrapped_as_503."""


# Self-mint eligibility lookup (wave-1 finding F4): subject → the per-
# subject user its newest live sso-create key binds to → that user's
# admin-governed self_mint_enabled flag. ONE JOIN from the subjects row
# (UNIQUE (idp, subject) — the same anchor the mint flow reads), the same
# conventions routes_enroll's _FOR_ME_* pair uses (bound_via parameter,
# revoked_at IS NULL, newest-wins ordering) but a single statement, so
# the subjects→api_keys→users chain is read atomically-shaped instead of
# three round-trips. Reads identity.db (the identity-ledger pool handle)
# — the same handle every identity binding reader uses.
_SELF_MINT_ELIGIBILITY_SQL = """
SELECT u.self_mint_enabled
FROM subjects s
JOIN api_keys k ON k.subject_id = s.id
    AND k.bound_via = $2
    AND k.revoked_at IS NULL
JOIN users u ON u.id = k.user_id
WHERE s.idp = $1 AND s.subject = $3
ORDER BY k.created_at DESC, k.id DESC
LIMIT 1
"""


async def _self_mint_allowed_for_subject(subject: str) -> bool:
    """THE single self-mint ELIGIBILITY decision point — the probe's
    ``flags.self_mint`` requires it AND the mint does too
    (routes_enroll._self_mint_allowed delegates here — the ONE eligibility
    lookup both surfaces consult, so a blocked subject sees a consistent
    "not allowed" on the probe AND at the mint, and the console toggle
    governs them together).

    CONTRACT: z3's admin eligibility model WRITES the governing data
    (users.self_mint_enabled, via PUT /admin/keys/self-mint-eligibility —
    db/schema.py migration, default TRUE); this function READS it and is
    the only lookup the mint-eligibility surfaces share.

    RESOLUTION (subject → user → flag): the subject's newest non-revoked
    ``sso-create`` key identifies the per-subject user row the mint flow
    itself creates (an SSO-created key's user IS the per-subject user —
    the same row the admin console's subject-selector write targets), so
    the write and this read always land on the same flag. Subjects with
    NO such key are DEFAULT-ELIGIBLE (True): an unknown subject must be
    able to mint its FIRST key — eligibility governs the second mint
    onward. A user row missing / a NULL flag reads True the same way
    (NOT NULL + server default make NULL unreachable in practice; an
    un-migrated DB therefore behaves exactly as before this lookup
    existed — flag-off byte-identity holds).

    FAIL-CLOSED (raise-don't-False, the wave-1 agreement): DB exceptions
    PROPAGATE — the callers' handlers map them (probe → 503, for-me →
    503, both pinned by tests). An unavailable eligibility store must
    never read as "not eligible": a silent False would deny every user
    during an outage with a refusal that lies about the cause. A False
    RETURN is the only "blocked" signal, and it means the lookup
    SUCCEEDED and said no.

    ``subject`` may be '' (no browser identity resolved) — no subject row
    can match, so the default-eligible branch answers; the hook is
    consulted only for the eligibility question, never as an identity
    assertion.
    """
    if not subject:
        return True
    row = await identity.db.fetchrow(_SELF_MINT_ELIGIBILITY_SQL, IDP_OAUTH2_PROXY, VIA_SSO_CREATE, subject)
    if row is None:
        return True  # no bound key/user → default-eligible (first mint)
    flag = row["self_mint_enabled"]
    return True if flag is None else bool(flag)


async def resolve_request_identity(request: Request) -> tuple[KeyRecord | None, Caller | None]:
    """The brief §Z2 helper contract, delegating to Z1's shared resolver:
    ``(key, caller)`` for the calling request under the SAME resolution
    the enforcement path performs (module docstring).

    ``(None, None)`` = RESOLVED-anonymous (nothing presented, or every
    presented credential refused). Raises ``HTTPException(503)`` on
    infrastructure failure (JWKS via Z1's wrapped refusal; DB/JWKS
    failures elsewhere propagate raw — the route handler maps both).
    """
    key: KeyRecord | None
    token_caller: Caller | None
    try:
        key, raw_caller = await auth_mod.resolve_bearer_key(
            request,
            authenticate=authenticate,
            headers=request.headers,
        )
        token_caller = raw_caller if isinstance(raw_caller, Caller) else None
    except fastapi.HTTPException as exc:
        if exc.status_code == 401 and (exc.detail or "") == _TOKEN_INFRA_DETAIL:
            # Z1's token ladder wraps JWKS/IdP infrastructure failure in
            # this exact refusal — on a PUBLIC probe that must stay
            # fail-closed: 503, never the anonymous shape.
            raise HTTPException(503, _ANONYMOUS_DETAIL) from None
        # A credential refusal (missing bearer / dead key / dead pair —
        # the enforcement 401): nothing anchored. The one caller Z1
        # resolves BEFORE refusing (valid token, no shadow key) rides
        # request.state.caller — surface it below like any token caller.
        key, token_caller = None, getattr(request.state, "caller", None)

    if key is not None:
        # The key's OWN binding is the reported identity (rungs 3-4).
        # Headers deliberately NOT passed — require_key never consults the
        # browser rung once a key resolved, and the proxy always injects
        # browser headers, which would otherwise shadow the ledger's word.
        caller = await resolve_caller(key)
    elif token_caller is not None and not (
        settings_trust_proxy_identity() and browser_identity_from_headers(dict(request.headers)) is not None
    ):
        # ⚠️ SUBJECT-SPELLING PARITY (2026-10-08, G2): behind the chained
        # edge the proxy injects BOTH a Bearer id_token AND the browser
        # headers — the token rung validates and reports the IdP's ``sub``
        # (a UUID on G2), while the MINT binds the HEADER subject
        # (X-Auth-Request-Preferred-Username = the human username, the MM
        # RAG convention). A probe that let the token rung outrank the
        # browser rung reported two spellings for one human (the chip
        # showed the UUID pre-mint while the mint bound the friendly
        # name) and computed self-mint eligibility against an anchor no
        # key will ever bind. On a chained edge the browser-header
        # spelling IS the identity the mint will create — the probe must
        # describe THAT (same request shape, same trust gate the mint
        # applies). The token rung stays authoritative whenever headers
        # are absent (pure token/API callers — the shadow-key surface) or
        # trust is off (headers untrusted → token only). Attribution
        # elsewhere (auth.py rungs, X-MCP-Caller) is untouched — this is
        # the probe's REPORTING precedence only.
        caller = token_caller
    elif settings_trust_proxy_identity():
        # Nothing key-shaped resolved and no dominating token caller: the
        # browser-header rung (ladder rung 1, trust-gated inside
        # resolve_caller as well).
        caller = await resolve_caller(None, headers=dict(request.headers))
    else:
        caller = None
    return key, caller


@router.get("/identity")
async def whoami(request: Request) -> dict[str, Any]:
    """The whoami probe. PUBLIC — no auth dependency (the SPA calls it
    before it has a key); the response describes only the calling request.

    ``flags.self_mint`` is the exact precondition under which
    POST /v1/keys/for-me would succeed: the trust flag on AND
    ``sso_key_create_enabled`` AND this request carries a browser-header
    identity (ladder rung 1, read with the same pure helper the mint
    route uses — so the two surfaces cannot disagree) AND the subject
    passes ``_self_mint_allowed_for_subject`` (the shared eligibility
    hook — admin-governed once z3's model lands, default-True until then).
    """
    headers = dict(request.headers)
    browser = browser_identity_from_headers(headers) is not None
    try:
        key, caller = await resolve_request_identity(request)
        binding_idp: str | None = None
        if key is not None:
            # The ledger's word on provenance: the key's subjects binding
            # carries the idp (oauth2-proxy / keycloak / enrollment /
            # self-asserted / admin-asserted). One cheap re-read of the row
            # resolve_caller already consulted for KIND_KEY_BOUND callers —
            # a probe is rare (SPA boot, card clicks) and correctness beats
            # the extra SELECT. DB failure here is the same 503 class.
            binding = await identity._fetch_key_binding(key.id)
            binding_idp = str(binding["idp"]) if binding is not None else None
        resolved_subject = caller.subject if caller is not None and caller.subject is not None else ""
        # ⚠️ TENANT PARITY (2026-10-08, found live on G2): the mint route
        # (routes_enroll POST /v1/keys/for-me) refuses with 503 when
        # PCAI_LLM_TOKEN_SHADOW_TENANT_ID is unset/malformed — the key needs
        # a tenancy holder (users.user_id NOT NULL) and the mint refuses to
        # invent one. A probe that ignored the tenant offered a card whose
        # click 503'd — and the SPA hides the card on ANY non-201/409 mint
        # response (ssoHideAndClose: silent, no banner), so the user saw the
        # mint affordance vanish on click. The mint ONLY lives where this
        # probe says it does: mirror the mint's tenant precondition exactly
        # (empty OR non-UUID → not mintable — uuid.UUID is the mint's own
        # parse at routes_enroll.py).
        try:
            uuid.UUID(settings.token_shadow_tenant_id or "")
            tenant_configured = True
        except ValueError:
            tenant_configured = False
        self_mint = (
            settings_trust_proxy_identity()
            and bool(settings.sso_key_create_enabled)
            and browser
            and tenant_configured
            and await _self_mint_allowed_for_subject(resolved_subject)
        )
    except HTTPException:
        raise
    except Exception:
        # Fail-closed: a broken identity ledger is a 503, never a silent
        # anonymous shape (the anonymous shape is for RESOLVED-anonymous).
        # The eligibility hook lives inside this try on purpose: when it
        # grows a real DB lookup, a broken ledger must 503 — never read as
        # "ineligible" (the same fail-closed posture as resolution).
        raise HTTPException(503, _ANONYMOUS_DETAIL) from None

    if key is None and caller is None:
        # RESOLVED-anonymous: the exact anti-oracle shape — six keys, one
        # nested flag, never more.
        return {
            "authenticated": False,
            "subject": None,
            "idp": None,
            "key_prefix": None,
            "scopes": [],
            "flags": {"self_mint": self_mint},
        }
    return {
        "authenticated": key is not None,
        "subject": caller.subject if caller is not None else None,
        "idp": binding_idp,
        "key_prefix": key.key_prefix if key is not None else None,
        "scopes": list(key.scopes) if key is not None else [],
        "flags": {"self_mint": self_mint},
    }
