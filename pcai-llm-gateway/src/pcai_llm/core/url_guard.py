"""Shared outbound-destination guard - the fleet's ONE SSRF deny-list.

WHY this module exists (2026-10-26 hardening plan, P0-1): the deny-list below
was born in api/routes_admin.py as the *registration-time* gate on
admin-supplied upstream URLs (model endpoint base_url, MCP server url, webhook
url). It never ran on MODEL-supplied arguments - and the federated /mcp
surface is exactly where a model hands a URL to a permitted tool and lets the
gateway's own pod make the request (the Cohere exfiltration class: the gateway
is a strong ACL boundary, but its authorisation is PAIR-granular while the
attack is ARGUMENT-granular).

So the predicates move here, unchanged, and two consumers share them:

* api/routes_admin.py - registration-time validation (behaviour preserved
  byte-for-byte: the private names are re-exported there as aliases, and
  _resolve_host_addresses stays a thin wrapper so the existing monkeypatch of
  routes_admin._URL_RESOLVE_TIMEOUT_S keeps working);
* mcpf/arg_policy.py - the new argument-level policy stage in the MCP gate
  chain.

DESIGN RULES (do not "improve" these without reading the call sites):

1. NO FastAPI imports. This is policy, not transport: the caller decides
   whether a refusal becomes an HTTPException, a ToolError, or a log line.
2. POSTURE IS AMENDED FOR IN-CLUSTER REALITY. RFC1918 stays ALLOWED: this
   gateway's legitimate upstreams ARE private services. Hard-denied:
   loopback, unspecified, link-local/metadata, CGNAT, plus operator CIDRs.
3. NON-CANONICAL IP ENCODINGS ARE DECODED, never waved through as hostnames -
   decimal/hex/octal/short forms all reach the range check.
4. RESOLUTION FAILURE IS NOT EVIDENCE OF DANGER. A resolver that cannot
   answer says nothing about the name; callers warn and skip (the documented
   rebinding-after-registration residual), never refuse on EAI_AGAIN.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time

# No import cycle: core.metrics imports only logging/prometheus_client at
# module level (its db import is lazy, inside _pool_stat) — and this module's
# design rule bans FastAPI imports, not core.telemetry ones.
from . import metrics as _metrics

log = logging.getLogger(__name__)

# Bounded registration/argument-time DNS lookup: a wedged resolver must not
# hang a request. A lookup that does not finish in time is a resolution
# FAILURE, which warns and skips the resolved-address check (nothing was
# SHOWN dangerous) rather than refusing.
RESOLVE_TIMEOUT_S = 5.0

# P3 (audit 2026-11-10): the lookup used to spawn a fresh daemon thread per
# call — a model-supplied URL that times out DNS leaked one thread PER call
# with no memory of the failure, so a repeated bad host leaked unboundedly.
#
# CROSS-VAL AMENDMENT (still P3, audit 2026-11-10): the first fix swapped the
# threads for a shared ThreadPoolExecutor(max_workers=4) — which turned the
# leak into a GLOBAL OUTAGE. CPython cannot cancel a wedged getaddrinfo, so
# four blackholed names occupied ALL FOUR workers and every later lookup
# queue-timed-out into the fail-open "warn and skip" path: a model could
# disable destination validation for ALL hosts for the resolver window — the
# exact class the SSRF gate exists to stop.
#
# Final shape: PER-CALL daemon threads (one wedged lookup can never starve
# another host's check) with the number of concurrently RUNNING lookups
# bounded by a semaphore (a model cannot spawn unbounded threads either),
# plus the short per-host NEGATIVE cache (a host that failed resolution
# answers from the cache for the TTL without new work). Saturation is a
# DISTINCT, fail-CLOSED outcome (ResolverSaturated), not EAI_AGAIN.
_RESOLVE_MAX_CONCURRENT = 8
_resolve_slots = threading.BoundedSemaphore(_RESOLVE_MAX_CONCURRENT)
_NEGATIVE_TTL_S = 30.0
_NEGATIVE_CACHE_MAX = 256
_negative_cache: dict[str, float] = {}
_negative_lock = threading.Lock()


class ResolverSaturated(RuntimeError):
    """The bounded resolver already carries its full complement of in-flight
    lookups; this request's check was NOT performed.

    Deliberately NOT a socket.gaierror/OSError subclass: the fail-open rule
    ("a resolver that cannot answer says nothing about the name") applies to
    RESOLUTION failures. Capacity exhaustion is a different fact — callers
    that catch only the narrow resolution-failure tuple propagate this and
    REFUSE, so four-plus wedged names cannot silently switch the SSRF gate
    off. The metric for this event is tracked with the audit remediation."""


def _negative_hit(host: str) -> bool:
    """True when host failed resolution recently enough to skip re-trying."""
    now = time.monotonic()
    with _negative_lock:
        deadline = _negative_cache.get(host)
        if deadline is not None and deadline > now:
            return True
        _negative_cache.pop(host, None)
        if deadline is None and len(_negative_cache) >= _NEGATIVE_CACHE_MAX:
            # Bound the cache: drop the soonest-expiring entries first.
            for key, _when in sorted(_negative_cache.items(), key=lambda kv: kv[1])[: len(_negative_cache) // 2]:
                del _negative_cache[key]
        return False


def _negative_remember(host: str) -> None:
    with _negative_lock:
        if len(_negative_cache) >= _NEGATIVE_CACHE_MAX:
            for key, _when in sorted(_negative_cache.items(), key=lambda kv: kv[1])[: len(_negative_cache) // 2]:
                del _negative_cache[key]
        _negative_cache[host] = time.monotonic() + _NEGATIVE_TTL_S


# Ranges denied UNCONDITIONALLY. See design rule 2.
CORE_DENIED_CIDRS: tuple[str, ...] = (
    "127.0.0.0/8",  # IPv4 loopback
    "0.0.0.0/8",  # unspecified: routes to localhost on Linux
    "::1/128",  # IPv6 loopback
    "::/128",  # unspecified v6
    "169.254.0.0/16",  # link-local incl. cloud metadata (169.254.169.254)
    "fe80::/10",  # IPv6 link-local
    "100.64.0.0/10",  # carrier-grade NAT (shared address space)
)

# Cluster-internal name suffixes. NOT denied by the core range check (they are
# hostnames, and they legitimately resolve to RFC1918 services this gateway
# serves); callers that must not reach the cluster control plane from a
# tool-driven request opt in explicitly (see arg_policy egress posture).
CLUSTER_SUFFIXES: tuple[str, ...] = (
    ".svc",
    ".svc.cluster",
    ".svc.cluster.local",
    ".cluster.local",
)


def resolve_host_addresses(host: str, timeout_s: float | None = None) -> list[str]:
    """Resolve host via getaddrinfo (ALL families) with a short timeout.

    Returns the distinct address strings, or raises socket.gaierror when the
    name does not resolve. A stuck resolver must not hang the caller: the
    lookup runs on a PER-CALL daemon thread (one wedged lookup can never
    starve another host's check — the shared-pool detour was a global-outage
    bug, see the module comment) with a bounded wait, and a timeout is
    surfaced as an EAI_AGAIN gaierror. The number of concurrently RUNNING
    lookups is bounded: when the bound is exhausted, ResolverSaturated is
    raised — a DISTINCT fail-closed outcome, not the fail-open gaierror. A
    host whose resolution recently failed answers from the negative cache
    without new work. Resolution failures stay fail-open at the documented
    call sites (warn and skip — see api.routes_admin._check_url).
    """
    if _negative_hit(host):
        raise socket.gaierror(socket.EAI_AGAIN, f"resolution of {host!r} failed recently (negative cache)")
    timeout = RESOLVE_TIMEOUT_S if timeout_s is None else timeout_s
    if not _resolve_slots.acquire(blocking=False):
        log.warning(
            "url_guard: resolver saturated (%d in-flight lookups) — destination check for %r NOT performed",
            _RESOLVE_MAX_CONCURRENT,
            host,
        )
        # The dashboard surface for the log line above: saturation is a
        # fail-closed refusal an operator must be able to see and alert on.
        _metrics.observe_url_guard_resolver_saturated()
        raise ResolverSaturated(f"url resolver saturated: {_RESOLVE_MAX_CONCURRENT} lookups already in flight")
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def _work() -> None:
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception as exc:  # re-raised on the caller's thread
            errors.append(exc)
            return
        finally:
            # The slot covers the LOOKUP — a wedged getaddrinfo holds it for
            # the OS resolver's natural duration, a finished one frees it
            # before the (cheap) parse.
            _resolve_slots.release()
        seen: list[str] = []
        for info in infos:
            addr = info[4][0]
            if not isinstance(addr, str):  # getaddrinfo types it str | int (port slot)
                continue
            if addr not in seen:
                seen.append(addr)
        results.append(seen)

    # P3 (cross-val wave 2): the slot is released inside _work's finally, which
    # only runs if the thread actually STARTS. A "can't start new thread"
    # RuntimeError from start() used to propagate with the slot still held —
    # one permanent leak per failure, saturating the resolver (every later
    # lookup -> ResolverSaturated -> global SSRF-check refusal). Release the
    # slot on ANY construction/start failure and re-raise unchanged.
    try:
        worker = threading.Thread(target=_work, name="pcai-url-check-resolve", daemon=True)
        worker.start()
    except BaseException:
        _resolve_slots.release()
        raise
    worker.join(timeout)
    if worker.is_alive():
        # The daemon thread finishes (and frees its slot) on its own; the
        # failure is remembered so a repeated bad host spawns nothing new.
        _negative_remember(host)
        raise socket.gaierror(socket.EAI_AGAIN, f"resolution of {host!r} timed out after {timeout}s") from None
    if errors:
        _negative_remember(host)
        raise errors[0]
    return results[0] if results else []


def looks_like_ip_literal(host: str) -> bool:
    """True when host is shaped like an IP literal and therefore must never
    be waved through as a hostname when its decode fails.

    COORDINATOR AMENDMENT (guardrail-validation regression): the original
    charset test (all of [0-9a-fA-Fx.]) classified ANY short alnum string as
    an IP shape - "x" (the baseline webhook-validation hostname), "beef",
    "dead", "cafe" are all "IP-shaped" under it, so legitimate hostname
    registrations 400'd with a misleading "IP-literal form failed
    validation". Literal INTENT is what matters:
      * 0x prefix (hex intent: 0x7f, 0xZZ) - literal-shaped, whatever follows;
      * otherwise: digits and dots ONLY with at least one digit
        (127.0.0.1.1.1, 1.2.3.999, 9.9.9 are malformed literals;
        x, beef, vllm are names).
    Hex-letter hostnames (beef.example.com has dots+letters -> name;
    bare beef -> name; beef with a 0x prefix -> literal) keep working.
    """
    if not host:
        return False
    if host.lower().startswith("0x"):
        return True
    return set(host) <= set("0123456789.") and any(c.isdigit() for c in host)


def decode_ip_literal(host: str) -> str | None:
    """Canonical textual address for host when it is an address literal in
    ANY encoding the OS resolver/inet_aton accepts, else None.

    Covers: canonical v4/v6, pure-integer (2130706433), 0x-hex
    (0x7f000001), and the inet_aton short forms - 2-part a.b (b is a
    24-bit value filling the last three octets) and 3-part a.b.c (a and b
    are octets, c is a 16-bit value filling the last two) - plus octal
    dotted forms (0177.0.0.1). The 3-part orientation is the subtle one:
    169.254.1 is 169.254.0.1 (LINK-LOCAL/metadata) - decoding it as
    169.0.254.1 instead would hand back an allowed address, so each part is
    range-checked in its own position. Returning None for a malformed literal
    is what lets the caller refuse it rather than fall through as a hostname."""
    if not host:
        return None
    if ":" in host:
        # IPv6 literal (urlparse already stripped the brackets): ipaddress is
        # the authority. A colon-bearing non-address is a name, not a
        # malformed literal.
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
        return host
    try:
        if host.isdigit() or (host.lower().startswith("0x") and "." not in host):
            # Pure-integer or 0x-hex forms encode a 32-bit IPv4 address.
            n = int(host, 0)
            return str(ipaddress.IPv4Address(n)) if 0 <= n <= 0xFFFFFFFF else None
        # Short forms ('127.1', octal '0177.0.0.1'): parse each part.
        # Leading-zero parts are OCTAL in BSD/inet_aton semantics
        # (int(p, 0) rejects '0177' outright) - decode them as octal
        # explicitly so the old resolver behavior cannot smuggle a
        # loopback address past the gate.
        parts = host.split(".")
        if not 1 <= len(parts) <= 4:
            return None
        ints = []
        for p in parts:
            if p.startswith("0") and len(p) > 1 and p.isdigit():
                ints.append(int(p, 8))
            else:
                ints.append(int(p, 0))  # ValueError on non-numeric
        if len(ints) == 4 and all(0 <= p <= 0xFF for p in ints):
            return ".".join(str(p) for p in ints)
        if len(ints) == 3 and 0 <= ints[0] <= 0xFF and 0 <= ints[1] <= 0xFF and 0 <= ints[2] <= 0xFFFF:
            return f"{ints[0]}.{ints[1]}.{(ints[2] >> 8) & 0xFF}.{ints[2] & 0xFF}"
        if len(ints) == 2 and 0 <= ints[0] <= 0xFF and 0 <= ints[1] <= 0xFFFFFF:
            return f"{ints[0]}.{(ints[1] >> 16) & 0xFF}.{(ints[1] >> 8) & 0xFF}.{ints[1] & 0xFF}"
        return None
    except (ValueError, OverflowError):
        return None  # malformed literal (caller refuses) or a hostname


# P3 (audit 2026-11-10): the core deny list was re-parsed with ip_network()
# per ENTRY on EVERY denied_ip_reason() call — per URL, per tool call. It is
# static: parse it once at module level.
_CORE_DENIED_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network(cidr) for cidr in CORE_DENIED_CIDRS
]
# extra_cidrs changes only when an operator reconfigures: cache the parsed
# list keyed by the exact tuple and rebuild only when it differs. (A malformed
# entry is logged once per rebuild instead of once per call.)
_extra_nets_cache: tuple[tuple[str, ...], list[ipaddress.IPv4Network | ipaddress.IPv6Network]] | None = None


def _denied_nets(extra_cidrs: tuple[str, ...] | list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    global _extra_nets_cache
    key = tuple(extra_cidrs)
    cached = _extra_nets_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    nets = list(_CORE_DENIED_NETS)
    for cidr in key:
        try:
            nets.append(ipaddress.ip_network(cidr))
        except ValueError:
            log.warning("ignoring malformed deny-CIDR entry %r", cidr)
    _extra_nets_cache = (key, nets)
    return nets


def denied_ip_reason(host: str, extra_cidrs: tuple[str, ...] | list[str] = ()) -> str:
    """Reason string when host is an IP literal inside a denied range
    (loopback / unspecified / link-local-metadata / CGNAT / operator extra-deny),
    else ''.

    Audit hardening (finding 4): (a) non-canonical IP encodings - decimal
    (2130706433), hex (0x7f000001), shortened (127.1), octal (0177.0.0.1) -
    fail ip_address parsing and USED to pass as plain hostnames while the OS
    resolver maps every one of them to 127.0.0.1; they are now decoded and
    checked. (b) The unspecified addresses (0.0.0.0, ::) and IPv4-mapped
    loopback/metadata (::ffff:127.0.0.1, ::ffff:169.254.169.254) parse as
    addresses but sat OUTSIDE every deny CIDR - on Linux they connect to the
    local pod, bypassing the loopback hard-deny; v4-mapped addresses are
    normalized before the range check and the unspecified ranges are denied
    outright.

    extra_cidrs: operator-added ranges. Callers pass settings.url_deny_cidrs
    (the registration gate's default) or an argument-policy override; a
    malformed entry is logged and ignored, never fatal. The parsed networks
    are built once at module level and rebuilt only when extra_cidrs changes
    (P3, audit 2026-11-10 — the list used to be re-parsed on every call)."""
    decoded = decode_ip_literal(host)
    if decoded is None:
        return ""  # not an address literal - the caller handles it as a host
    addr = ipaddress.ip_address(decoded)
    # Normalize IPv4-mapped IPv6 to the embedded IPv4 (the deny lists are
    # v4-shaped; ::ffff:127.0.0.1 must hit 127.0.0.0/8).
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    for net in _denied_nets(extra_cidrs):
        if addr in net:
            return f"address {addr} is inside denied range {net}"
    return ""


def is_cluster_host(host: str) -> bool:
    """True when host is (or ends with) an in-cluster name suffix.

    Deliberately a NAME test, not a range test: RFC1918 addresses stay
    allowed (design rule 2), so the only reliable cluster signal is the
    Kubernetes DNS convention. localhost is handled by the core check.
    """
    if not host:
        return False
    h = host.lower().rstrip(".")
    return any(h == suf.lstrip(".") or h.endswith(suf) for suf in CLUSTER_SUFFIXES)
