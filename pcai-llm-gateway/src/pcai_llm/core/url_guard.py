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

log = logging.getLogger(__name__)

# Bounded registration/argument-time DNS lookup: a wedged resolver must not
# hang a request. A lookup that does not finish in time is a resolution
# FAILURE, which warns and skips the resolved-address check (nothing was
# SHOWN dangerous) rather than refusing.
RESOLVE_TIMEOUT_S = 5.0

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
    lookup runs on a daemon thread with a bounded join, and a timeout is
    surfaced as an EAI_AGAIN gaierror. Callers treat every failure class the
    same way - warn and skip the check (see api.routes_admin._check_url).
    """
    timeout = RESOLVE_TIMEOUT_S if timeout_s is None else timeout_s
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def _work() -> None:
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception as exc:  # re-raised on the caller's thread
            errors.append(exc)
            return
        seen: list[str] = []
        for info in infos:
            addr = info[4][0]
            if not isinstance(addr, str):  # getaddrinfo types it str | int (port slot)
                continue
            if addr not in seen:
                seen.append(addr)
        results.append(seen)

    worker = threading.Thread(target=_work, name="pcai-url-check-resolve", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise socket.gaierror(socket.EAI_AGAIN, f"resolution of {host!r} timed out after {timeout}s")
    if errors:
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
    malformed entry is logged and ignored, never fatal."""
    decoded = decode_ip_literal(host)
    if decoded is None:
        return ""  # not an address literal - the caller handles it as a host
    addr = ipaddress.ip_address(decoded)
    # Normalize IPv4-mapped IPv6 to the embedded IPv4 (the deny lists are
    # v4-shaped; ::ffff:127.0.0.1 must hit 127.0.0.0/8).
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    denied: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
        ipaddress.ip_network(cidr) for cidr in CORE_DENIED_CIDRS
    ]
    for cidr in extra_cidrs:
        try:
            denied.append(ipaddress.ip_network(cidr))
        except ValueError:
            log.warning("ignoring malformed deny-CIDR entry %r", cidr)
    for net in denied:
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
