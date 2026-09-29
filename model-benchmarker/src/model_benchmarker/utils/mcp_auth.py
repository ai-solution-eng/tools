"""Shared API-key middleware for the MCP fleet (the K8S-MCP pattern, extracted).

NOT an auth server — a pure-ASGI middleware class each server wraps its own
app with. No extra deployment, no network hop, no central point of failure;
keys stay in each server's own Kubernetes Secret and are re-read PER REQUEST,
so a Secret rotation reaches a running pod without a restart.

One-address wiring (fleet decision, 2026-09):

* ``MCP_API_KEYS`` is the UNIVERSAL env var — every fleet server accepts it.
* Per-server names (``K8S_MCP_API_KEY``, ``APPLYGATE_API_KEYS``,
  ``WORKBENCH_API_KEYS``, ...) keep working: the key sets are UNIONED, all
  comparisons constant-time (``hmac.compare_digest``). Comma-separated keys
  within one var are the rotation mechanism — append the new key, move
  clients over, drop the old one, zero downtime.
* No keys configured → auth is OPEN (local development mode); call
  ``warn_if_open`` in ``main()`` to scream about it once at startup. Whether
  that is acceptable per server is a CHART decision: mandatory servers wire
  the env from an operator-created Secret unconditionally (the pod fails
  loud with CreateContainerConfigError until the Secret exists); optional
  servers wire it only when the chart values point at one.

Scope per server (fleet decision, 2026-09):

* MANDATORY auth: k8s-mcp, applygate, logsearch, workbench (execution or
  cluster-mutation surfaces).
* OPTIONAL auth: RAG-MCP, SQLhandler, searxng (read/search surfaces fronted
  by the gateway).

Caller attribution (Wave 6, Item A1 — attribution-never-authorization):

* ``capture_caller`` resolves WHO is calling for the audit trail: the
  matched key's sha256 FINGERPRINT (never the key), the ASGI client
  host:port, an optional per-request registry name (``<SERVER>_CLIENTS``),
  and — only when the direct peer is on the trusted-CIDR list — the peer's
  own ``X-MCP-Caller`` claim as ``via``. A trusted peer is infrastructure
  (the gateway); an untrusted peer's header is IGNORED, not honored —
  fail-closed. Attribution never unlocks anything anywhere: it names the
  caller for audit, it is not a credential.
* ``CALLER_CONTEXT`` / ``current_caller`` are the request-scoped slot the
  capture middleware sets and the audit writer reads.

This module is hardlinked into the fleet by pcai_utils machinery; keep it
dependency-free (stdlib only) and import-agnostic (no relative imports, no
sibling-module imports) so every consumer can import it from wherever its
tree places it.
"""

import hashlib
import hmac
import ipaddress
import os
from contextvars import ContextVar
from dataclasses import dataclass

UNIVERSAL_API_KEYS_ENV = "MCP_API_KEYS"

UNAUTHORIZED_BODY = b'{"error": "unauthorized: missing or invalid API key"}'

DEFAULT_PUBLIC_PATHS = ("/health", "/healthz")

DEFAULT_TRUSTED_CIDRS_ENV = "MCP_CALLER_TRUSTED_CIDRS"

#: Maximum accepted length of an ``X-MCP-Caller`` value before sanitization
#: (the cap is applied to the RAW header so a 1 MiB header cannot reach the
#: sanitization step at all).
CALLER_HEADER_MAX_LENGTH = 256

#: Sanitized ``via`` values are capped again AFTER CR/LF stripping — audit
#: JSON must stay one physical line no matter what a peer sends.
CALLER_VIA_MAX_LENGTH = 200


def configured_keys(env_names=("MCP_API_KEYS",)):
    """Union of comma-separated keys from the given env vars (order kept,
    duplicates dropped). Env is read on every call — rotation without restart."""
    keys: list = []
    for name in env_names:
        raw = os.environ.get(name, "")
        for k in raw.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


def presented_keys(scope):
    """Candidate keys from raw ASGI headers (names must already be lowercase).

    Accepts ``Authorization: Bearer <key>`` and ``X-API-Key: <key>``; both are
    collected so clients can use whichever header their MCP client exposes.
    """
    candidates = []
    for name, value in scope.get("headers", []):
        lowered = name.lower()
        if lowered == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                candidates.append(token.strip())
        elif lowered == b"x-api-key":
            candidates.append(value.decode("latin-1").strip())
    return candidates


def presented_keys_with_source(scope):
    """``[(key, header_name), …]`` — the :func:`presented_keys` candidates
    paired with the lowercased header that presented each one.

    The pairing drives the D19 delegation precedence in
    ``clients_registry.resolve_presented``: a key presented via
    ``X-API-Key`` outranks a co-forwarded ``Authorization: Bearer`` token
    (a gateway's own platform/admin token), so per-key delegation to a
    registry identity is possible at all.  Only ``Bearer`` Authorization
    headers count; Basic/Negotiate are ignored.
    """
    pairs = []
    for name, value in scope.get("headers", []):
        lowered = name.lower()
        if lowered == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                pairs.append((token.strip(), "authorization"))
        elif lowered == b"x-api-key":
            token = value.decode("latin-1").strip()
            if token:
                pairs.append((token, "x-api-key"))
    return pairs


@dataclass(frozen=True)
class Caller:
    """Resolved identity of one request's caller — audit-grade, non-secret.

    ``key_fp`` is ``sha256:<12 hex>`` of the MATCHED configured key (or None
    when anonymous/unmatched — never a would-be intruder's fingerprint);
    ``client`` is the ASGI client host:port when known.

    ``name`` is the per-request registry name (``<SERVER>_CLIENTS`` ->
    ``name:key``) for the matched key — the human/service identity an
    operator assigned. ``via`` is the caller-claim relayed by a TRUSTED
    proxy peer in ``X-MCP-Caller`` (e.g. ``<subject>@gateway``) — None for
    every direct/untrusted caller. ``via`` is metadata about WHO a trusted
    intermediary says is on the other end; it is never consulted for any
    authorization decision (attribution-never-authorization).

    ``as_dict`` includes ``name``/``via`` ONLY when set, so audit lines for
    deployments that do not configure the registry (or relay nothing)
    serialize BYTE-IDENTICAL to the pre-attribution 2-key shape
    ``{"key_fp": ..., "client": ...}`` — every existing reader keeps working.
    """

    key_fp: str | None
    client: str | None
    name: str | None = None
    via: str | None = None

    def as_dict(self) -> dict:
        out = {"key_fp": self.key_fp, "client": self.client}
        if self.name:
            out["name"] = self.name
        if self.via:
            out["via"] = self.via
        return out


#: The request-scoped caller slot — set by each server's outermost capture
#: middleware per request; read by the audit writer (``current_caller``).
CALLER_CONTEXT: ContextVar = ContextVar("mcp_caller", default=None)


def current_caller() -> Caller | None:
    """The caller captured for the CURRENT request (None outside one)."""
    return CALLER_CONTEXT.get()


def _client_str(scope) -> str | None:
    client = scope.get("client")
    return f"{client[0]}:{client[1]}" if client else None


def _sanitize_via(raw: str) -> str | None:
    """Make a caller-claim safe for single-line JSONL audit: strip CR/LF and
    control characters (header injection / log-forgery), then cap the
    length. An empty result sanitizes to None."""
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch not in "\r\n")
    cleaned = cleaned.strip()
    return cleaned[:CALLER_VIA_MAX_LENGTH] or None


def _trusted_peer(scope, trusted_cidrs_env: str) -> bool:
    """True only when the DIRECT peer address falls inside one of the
    trusted CIDRs. An unset/empty env is fail-CLOSED: no peer is trusted,
    so ``via`` is never honored (CIDRs are a deployment-tuned trust claim —
    behind an Istio sidecar scope["client"] is the sidecar, so tune the list
    per deployment; the default posture ignores the header everywhere)."""
    raw = (os.environ.get(trusted_cidrs_env) or "").strip()
    if not raw:
        return False
    client = scope.get("client")
    if not client or not client[0]:
        return False
    try:
        peer = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            if peer in ipaddress.ip_network(chunk, strict=False):
                return True
        except ValueError:
            continue  # a malformed CIDR in operator config is skipped, never trusted
    return False


def _key_fingerprint(key: str) -> str:
    """sha256 of the MATCHED configured key, first 12 hex chars — the fleet
    caller-fingerprint convention (defined here so every consumer hashes
    IDENTICALLY; the raw key never enters the result)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _registry_names(clients_env: str | None) -> dict:
    """Parse ``<clients_env>`` (``name:key;name:key;...``) into {key: name},
    re-read PER REQUEST so a Secret rotation reaches a running pod. Malformed
    entries are refused loudly and skipped — the registry can never widen
    authentication (it only NAMES keys that configured_keys already matched;
    an unparseable registry degrades attribution to fp-only, never auth)."""
    names: dict = {}
    if not clients_env:
        return names
    raw = (os.environ.get(clients_env) or "").strip()
    if not raw:
        return names
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            import sys

            print(
                f"[mcp_auth] WARNING: invalid client entry {chunk!r} in {clients_env} "
                "(expected name:key) — entry skipped (registry only NAMES callers, "
                "it never authenticates them)",
                file=sys.stderr,
                flush=True,
            )
            continue
        name, key = parts[0].strip(), parts[1].strip()
        if key not in names:
            names[key] = name
    return names


def capture_caller(
    scope,
    env_names=("MCP_API_KEYS",),
    clients_env: str | None = None,
    trusted_cidrs_env: str = DEFAULT_TRUSTED_CIDRS_ENV,
) -> Caller:
    """Resolve WHO is calling — the one resolver servers' capture middlewares
    call. Same inputs the auth middleware sees; attribution only, never a
    gate (keep capture OUT of ApiKeyAuthMiddleware.__call__: the middleware
    short-circuits non-protected paths, which would silently change
    console-path audit shapes; each server keeps its thin outermost wrapper).

    - ``key_fp``: constant-time match of the presented keys against
      ``configured_keys(env_names)`` -> ``sha256:<12 hex>`` of the MATCHED
      configured key. Unmatched/anonymous callers get None (never a
      would-be intruder's fingerprint).
    - ``client``: ASGI client host:port when known.
    - ``name``: registry name from ``clients_env`` (``name:key;...``) for the
      matched key — only when ``clients_env`` is configured AND the key is
      registered. ``hmac.compare_digest`` semantics over the registry keys.
    - ``via``: the peer's ``X-MCP-Caller`` claim, sanitized (CR/LF stripped,
      ≤200 chars), ONLY when the direct peer IP is inside
      ``trusted_cidrs_env`` (default ``MCP_CALLER_TRUSTED_CIDRS``). Empty or
      unset CIDRs ⇒ ``via`` is ALWAYS None — fail-closed. http scopes only.
    """
    if scope.get("type") != "http":
        return Caller(key_fp=None, client=_client_str(scope))
    matched: str | None = None
    keys = configured_keys(env_names)
    if keys:
        for candidate in presented_keys(scope):
            for valid in keys:
                if hmac.compare_digest(candidate.encode("utf-8"), valid.encode("utf-8")):
                    matched = valid
                    break
            if matched:
                break
    key_fp = ("sha256:" + _key_fingerprint(matched)) if matched else None
    name = None
    if matched and clients_env:
        registry = _registry_names(clients_env)
        for reg_key, reg_name in registry.items():
            if hmac.compare_digest(matched.encode("utf-8"), reg_key.encode("utf-8")):
                name = reg_name
                break
    via = None
    if _trusted_peer(scope, trusted_cidrs_env):
        for hname, hvalue in scope.get("headers", []):
            if hname.lower() == b"x-mcp-caller":
                via = _sanitize_via(hvalue.decode("latin-1")[:CALLER_HEADER_MAX_LENGTH])
                break
    return Caller(key_fp=key_fp, client=_client_str(scope), name=name, via=via)


class ApiKeyAuthMiddleware:
    """Pure-ASGI middleware enforcing an API key on the protected paths.

    Parameters
    ----------
    app:
        The ASGI app to wrap (usually the server's assembled Starlette app —
        wrapping in the builder, not in ``main()``, so every consumer of the
        builder gets the authenticated app).
    env_names:
        Env vars holding comma-separated valid keys. ALWAYS include
        ``UNIVERSAL_API_KEYS_ENV`` first; per-server names are aliases.
    protected:
        Optional ``callable(path) -> bool`` naming the paths that REQUIRE a
        key. When given it wins over ``public_paths`` (use it for
        allowlist-style servers like applygate, where ONLY /mcp is
        protected and everything else is public).
    public_paths:
        Paths that never require a key (default: the k8s probes). Used when
        ``protected`` is not given (denylist-style servers like workbench:
        everything except the probes is protected).
    """

    def __init__(self, app, env_names=("MCP_API_KEYS",), protected=None, public_paths=None):
        self.app = app
        self._env_names = tuple(env_names) or (UNIVERSAL_API_KEYS_ENV,)
        self._protected = protected
        self._public = frozenset(public_paths if public_paths is not None else DEFAULT_PUBLIC_PATHS)

    @property
    def routes(self):
        """Pass-through so callers/tests can introspect the wrapped app."""
        return self.app.routes

    def _needs_auth(self, path: str) -> bool:
        if self._protected is not None:
            return self._protected(path)
        return path not in self._public

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._needs_auth(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        keys = configured_keys(self._env_names)
        if not keys:
            await self.app(scope, receive, send)  # auth disabled (dev mode)
            return
        for candidate in presented_keys(scope):
            for valid in keys:
                if hmac.compare_digest(candidate.encode("utf-8"), valid.encode("utf-8")):
                    await self.app(scope, receive, send)
                    return
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(UNAUTHORIZED_BODY)).encode("ascii")),
            (b"www-authenticate", b"Bearer"),
        ]
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})


def warn_if_open(server_label: str, env_names=("MCP_API_KEYS",)) -> bool:
    """Loud one-time startup warning when no keys are configured.

    Returns True when auth is OPEN — call it in the server's HTTP-mode
    ``main()`` only (stdio dev use never needed auth).
    """
    if configured_keys(env_names):
        return False
    names = ", ".join(env_names)
    line = "=" * 72
    print(line)
    print(f"WARNING: none of [{names}] is set — {server_label}'s HTTP endpoints are OPEN")
    print("(local development mode). Configure keys from a Secret before any")
    print("shared or gateway-exposed deployment.")
    print(line)
    return True
