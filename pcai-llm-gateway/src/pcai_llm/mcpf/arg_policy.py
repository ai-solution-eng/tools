"""Argument-level policy stage for the federated MCP gate chain (P0-1).

WHY THIS EXISTS
---------------
The gateway's authorisation is PAIR-granular ("may this key use
`searxng.fetch_content»?") while the Cohere exfiltration class is
ARGUMENT-granular: a model that wants to reach a host it cannot reach itself
does not need a new grant, it asks a PERMITTED tool to do it and the gateway's
own pod makes the request. Nothing in the ACL chain ever saw the URL. This
stage sees it.

WHERE IT RUNS (both model-driven paths, ONE implementation)
-----------------------------------------------------------
* `api/routes_mcp.py::_gated_mcp_call» - the /mcp surface, between the ACL
  gate and the server tool-policy gate.
* `api/routes_proxy.py::_relay_executor.execute» - the RELAY path, i.e. the
  tool calls a model makes DURING a chat turn (the path that actually carries
  the attack). Same function, same order, same settle status.

THE STRING-CLASSIFICATION RULE (this is the whole false-positive story)
----------------------------------------------------------------------
Tools in this fleet take SQL, YAML manifests, base64 chart payloads, regexes,
workspace paths, namespaces and shell-ish command lines. A blanket "does the
argument text contain 127.0.0.1" scan would refuse half the fleet. So a string
is treated as a DESTINATION only when it is a destination in its ENTIRETY:

1. It carries no whitespace and no control characters. A URL cannot contain
   raw whitespace; SQL, YAML, prose, kubectl command lines and most regexes
   all do. This single rule is what keeps
   `SELECT ... WHERE host='127.0.0.1'», a multi-line manifest containing a
   URL, and `kubectl get pods -n prod» out of the deny path - pinned by
   tests/test_arg_policy.py::test_false_positive_*. The ONE exception is a
   hierarchical URL ("scheme://"): a client percent-encodes a raw space, so
   its wire bytes are the refused whitespace-free request's and a raw space
   must not be the cheaper spelling of "%20" (AUDIT-r5 #1). A drive path
   ("C:\\Program Files"), a command line and prose carry no "//", so they
   keep the exemption.
2. The WHOLE string is an IP literal in some encoding (127.0.0.1, 2130706433,
   0x7f000001, 127.1, 0177.0.0.1) -> range check. An address argument is a
   destination; an address inside a larger string is not classified at all.
3. The string STARTS with a URL scheme (http://, https://, file:, ...) and
   parses -> scheme + host checks. A string that starts with a scheme but
   FAILS to parse is REFUSED, never waved through: "could not parse" is not
   evidence of safety (the posture url_guard design rule 3 demands).
4. Otherwise, only when the ARGUMENT KEY names a destination (url, endpoint,
   host, target, path, ...) do we additionally accept a bare
   `host[:port][/path]» form, so `fetch_content("169.254.169.254/latest/meta-data")»
   is caught while a `namespace» / `pattern» / `sql» argument is not ('paths'
   is a destination key because mm-rag DOWNLOADS it). The same gate covers the
   protocol-relative `//host/path» form, which a client that urljoin()s the
   argument against a base URL resolves to the same host (AUDIT-r5 #1,
   cross-review xv-glm-a); a single leading "/" stays a path reference, a
   host run holding ".." is a parent-relative path and not a host, and a
   bracketed IPv6 literal is decoded with url_guard's IP machinery.

Percent-encoding is decoded once before classification (`%31%32%37.0.0.1» is
127.0.0.1 to a web server); a decode that introduces whitespace drops the
string from destination classification, same rule as above - EXCEPT for a
string that starts with a scheme, where that whitespace can only sit in the
path/query and the host is still the destination. A URL FRAGMENT is never
transmitted, so it is stripped before both tests; otherwise "#%20" decoded to
a trailing space and waved the whole URL through (AUDIT-r5 #1).

WHAT THIS DOES **NOT** DO (state it plainly, do not oversell it)
---------------------------------------------------------------
This blocks *where* a tool may send a request, not *what* it sends. A GET to a
fully allowlisted public host still carries a payload in its path/query - the
article's actual exfiltration. Closing that needs an egress proxy with content
inspection; `scan_payload_signals» only OBSERVES payload shapes (metric, no
refusal). DNS rebinding after the check is the documented url_guard residual.
SATURATION of the bounded resolver is NOT in that fail-open class: "no capacity
to vet this name" refuses (REASON_RESOLVER_SATURATED), because a model that can
wedge the pool must not thereby switch the destination check off.

EXEC-class arguments (command/script/code) are TOKEN-SCANNED, not understood.
A token that is a scheme URL or an address literal (any encoding, percent-
decoded once) is classified; the scan does NOT see:
  * a destination built at RUN time — "echo <b64> | base64 -d | sh",
    "curl $(cat url.txt)", "X=169.254.169.254; curl http://$X/";
  * a bare HOSTNAME token — never DNS-resolved here (a command line is full of
    dotted non-destinations and a 2 s lookup per token is a latency bomb), so
    "curl internal-svc.cluster.local" is NOT refused;
  * a numeric literal shorter than _EXEC_MIN_BARE_LITERAL chars.
A command line with more tokens than _EXEC_MAX_TOKENS is REFUSED
(REASON_TOO_COMPLEX), never partially scanned. Closing the first bullet needs
the egress proxy above: this stage raises the cost of the trivial spelling, it
does not make `exec_in_pod» safe.

POSTURE
-------
`PCAI_LLM_MCP_ARG_POLICY» = enforce (default) | warn | off. Enforce is the
default because the deny-list is a DESTINATION deny-list - it does not stop a
legitimate public web fetch. An unrecognised value fails CLOSED to enforce with
a warning (a typo must never silently disable the stage). Per-tool escape
hatch: `PCAI_LLM_MCP_ARG_POLICY_EXEMPT» (comma-separated `server.tool» globs,
e.g. `searxng.*»). No fleet tool is known to need an exemption today.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from typing import Any
from urllib.parse import unquote, urlparse

from ..config import settings
from ..core import metrics as _metrics
from ..core import url_guard
from . import federation as _federation

log = logging.getLogger(__name__)

# --- Z2 predicates: FAIL CLOSED on every fault ----------------------------
# api/routes_mcp.py and api/routes_proxy.py are Z1's; mcpf/federation.py is
# Z2's, and the seam is pinned by SPEC 3.2.
#
# POSTURE: a security predicate that fails open is not a control (cross-
# validation wave 1, P1 — the earlier "degrade to allowed" wrapper silently
# re-opened sqlhandler.admin_* whenever the predicate faulted, was renamed, or
# was absent from a partial deploy). So:
#   * the symbol is resolved at call time but a MISSING symbol or a raised
#     exception REFUSES with a bounded '*-unavailable' reason — never allows;
#   * the caller-visible message names the control that is unavailable, not
#     the bypass, so an unprivileged caller learns nothing actionable.
# The one exception is the read+egress MODE resolver: its failure resolves to
# the CONFIGURED DEFAULT ('warn'), not to the strictest mode — see the
# asymmetry note in _separate_read_egress_mode (refusing there would close the
# entire tool surface of every dual-class key, which is a fleet outage rather
# than a hardening).
# The two GRANT-level reasons (SPEC 3.2 items 1/4) plus the two
# control-unavailable reasons. Together with the four argument reasons they
# form the CLOSED vocabulary: these are the only values that can ever reach
# the metric's reason label.
ADMIN_TOOL_REASON = "admin-tool"
READ_EGRESS_REASON = "read+egress"  # = federation.MCP_READ_EGRESS_ENFORCE_REASON
READ_EGRESS_WARN_REASON = "read+egress-warn"  # = federation.MCP_READ_EGRESS_WARN_REASON
ADMIN_REFUSAL_UNAVAILABLE = "admin-tool-refusal-unavailable"
READ_EGRESS_UNAVAILABLE = "read-egress-check-unavailable"

_UNAVAILABLE_TEXT = {
    ADMIN_REFUSAL_UNAVAILABLE: (
        "refused: the MCP admin-tool guard is unavailable on this gateway "
        "(its policy predicate is missing or faulted) — admin-control-plane tools "
        "stay closed until it is restored"
    ),
    READ_EGRESS_UNAVAILABLE: (
        "refused: the MCP read+egress separation check is unavailable on this gateway "
        "(its policy predicate is missing or faulted) — calls stay closed until it is restored"
    ),
}


def _in_admin_scope(server: str, tool: str) -> bool | None:
    """Is this call admin-class? True/False from the pure predicate, None when
    even THAT is unavailable (unknown scope -> the caller stays conservative).

    federation.is_admin_only_tool is a pure string test against a module
    constant: no I/O, no fault path, and it is the SAME table the refusal
    predicate reads. It is what lets a fault in the refusal predicate close
    admin verbs WITHOUT closing the rest of the fleet (wave-2 review P2)."""
    fn = getattr(_federation, "is_admin_only_tool", None)
    if fn is None:  # pragma: no cover - only a partial deploy reaches this
        return None
    try:
        return bool(fn(server, tool))
    except Exception:  # pragma: no cover
        return None


def _admin_unavailable_refusal(server: str, tool: str) -> str:
    """The refusal to use when the admin predicate is missing/faulted.

    SCOPED fail-closed: refuse when the call IS admin-class or when the scope
    itself cannot be determined (None), allow when the pure predicate proves
    the tool is not admin-class — a guard fault must never open
    sqlhandler.admin_*, and must never close k8s-ops.get_resource either."""
    if _in_admin_scope(server, tool) is False:
        log.error(
            "mcp arg policy: the admin-tool guard is unavailable, but %s.%s is not an "
            "admin-class tool (pure scope predicate) — allowing this call",
            server,
            tool,
        )
        return ""
    return _UNAVAILABLE_TEXT[ADMIN_REFUSAL_UNAVAILABLE]


def _admin_tool_refusal(server: str, tool: str, key_mcp_admin: bool) -> str:
    """Empty when allowed, else a refusal message. FAILS CLOSED for
    admin-class calls: a missing predicate or a raised exception refuses
    (see the posture note above), scoped by the pure admin-scope predicate."""
    fn = getattr(_federation, "admin_tool_refusal", None)
    if fn is None:  # pragma: no cover - only a partial deploy reaches this
        log.error("mcp arg policy: federation.admin_tool_refusal is MISSING — refusing (fail closed)")
        return _admin_unavailable_refusal(server, tool)
    try:
        return fn(server, tool, key_mcp_admin) or ""
    except Exception:
        log.exception("mcp arg policy: admin_tool_refusal faulted — refusing (fail closed)")
        return _admin_unavailable_refusal(server, tool)


def _read_egress_violation(grants: set[tuple[str, str]]) -> tuple[tuple[str, str], tuple[str, str]] | None:
    """(read_pair, egress_pair) when the grant set holds both classes.

    FAILS CLOSED: a missing/faulted predicate reports the unavailable marker
    (via _read_egress_unavailable) so the caller refuses instead of silently
    skipping the read+egress check."""
    fn = getattr(_federation, "read_egress_violation", None)
    if fn is None:  # pragma: no cover - only a partial deploy reaches this
        log.error("mcp arg policy: federation.read_egress_violation is MISSING — refusing (fail closed)")
        raise RuntimeError(READ_EGRESS_UNAVAILABLE)
    try:
        return fn(set(grants))
    except Exception as exc:
        log.exception("mcp arg policy: read_egress_violation faulted — refusing (fail closed)")
        raise RuntimeError(READ_EGRESS_UNAVAILABLE) from exc


def _read_egress_enforce_reason() -> str:
    """Z2's ENFORCE reason token ('read+egress'). A missing constant falls back
    to the same literal — the token IS the contract, so the fallback cannot
    mislabel anything."""
    return str(getattr(_federation, "MCP_READ_EGRESS_ENFORCE_REASON", READ_EGRESS_REASON))


def _read_egress_warn_reason() -> str:
    """Z2's WARN reason token ('read+egress-warn'). The split matters: a metric
    reading 'blocked' while the call PROCEEDED is a lie, so the warn arm must
    never tick the enforce token."""
    return str(getattr(_federation, "MCP_READ_EGRESS_WARN_REASON", READ_EGRESS_WARN_REASON))


def _separate_read_egress_mode() -> str:
    """The read+egress posture, falling back to the CONFIGURED DEFAULT ('warn').

    THE ASYMMETRY, stated once (wave-2 review P2 — this was over-corrected):
    a fault in the ADMIN predicate refuses ONE capability (admin-control-plane
    verbs, which no model-driven key should ever reach), so failing closed
    there costs nothing legitimate. A fault HERE would refuse the WHOLE tool
    surface of any key holding both a read and an egress grant — including
    k8s-ops.get_resource — i.e. the guard would take the fleet's own agent
    offline during exactly the partial-deploy it exists for. SPEC 3.1 pins
    this knob's default to 'warn' for that reason, so the fault path falls
    back to the configured default with a loud log and lets the call proceed.

    An unrecognised value from a healthy resolver also resolves to 'warn'
    (federation.separate_read_egress_mode's own documented fail-safe)."""
    fn = getattr(_federation, "separate_read_egress_mode", None)
    if fn is None:  # pragma: no cover - only a partial deploy reaches this
        log.error(
            "mcp arg policy: federation.separate_read_egress_mode is MISSING — "
            "using the configured default 'warn' (the read+egress check still RUNS, "
            "it just does not refuse)"
        )
        return "warn"
    try:
        resolved = fn()
    except Exception:  # pragma: no cover
        log.exception(
            "mcp arg policy: separate_read_egress_mode faulted — using the configured "
            "default 'warn' (a refusal here would close the whole tool surface)"
        )
        return "warn"
    return resolved if resolved in ("enforce", "warn", "off") else "warn"


# --- bounds (spec D1: depth 6, 200 strings, 8 KiB per string) --------------
# BUMPED (xv round 2, finding 6): the original bounds refused ORDINARY
# arguments in enforce mode — a depth-8 Kubernetes manifest
# (spec.template.spec.containers[0].resources.limits...) and a 201-element
# batch array are real fleet payloads, and a depth/count refusal is
# unfixable by the model (it cannot flatten a manifest). 12/2000 still
# fail CLOSED above them: padding a denied destination past 2000 benign
# strings costs the attacker 10x more than the old bound, and the bound
# keeps capping the per-call scan work.
MAX_DEPTH = 12
MAX_STRINGS = 2000
# Audit 2026-11-10 P2-32: depth and string count bound the SCAN RESULT, but a
# tree of EMPTY dicts/lists pushes one stack entry per node while incrementing
# neither counter — so neither bound ever tripped and one gated call could burn
# unbounded CPU on the event loop. The node bound caps the WORK; it sits well
# above MAX_STRINGS because a legitimate tree carries container nodes too
# (a 2000-string payload is ~2000+ nodes), and a walk of 20k nodes is still
# microseconds.
MAX_NODES = 20_000
MAX_SCAN_CHARS = 8 * 1024

# --- BOUNDED reason vocabulary ---------------------------------------------
# These four strings are the ONLY values that can reach the
# mcp_policy_blocked_total{reason} label. The caller's URL/host text is never
# a label (cardinality + reflected-input hygiene), and the refusal message
# carries a fixed sentence per reason.
REASON_TOO_COMPLEX = "arguments-too-complex"  # exceeded a scan bound: refused, never partially scanned (audit C-1)
REASON_SCHEME = "url-scheme"  # non-http(s) scheme (file:, gopher:, ...)
REASON_DESTINATION = "url-destination"  # denied range / loopback name / unparseable
REASON_IP_LITERAL = "ip-literal"  # IP-shaped but not decodable
REASON_CLUSTER_HOST = "cluster-host"  # in-cluster name on an egress-class server
REASON_RESOLVER_SATURATED = "resolver-saturated"  # the destination was never vetted: refuse, never allow

REASONS = (
    REASON_TOO_COMPLEX,
    REASON_SCHEME,
    REASON_DESTINATION,
    REASON_IP_LITERAL,
    REASON_CLUSTER_HOST,
    REASON_RESOLVER_SATURATED,
    ADMIN_TOOL_REASON,
    READ_EGRESS_REASON,
    READ_EGRESS_WARN_REASON,
    ADMIN_REFUSAL_UNAVAILABLE,
    READ_EGRESS_UNAVAILABLE,
)

_REASON_TEXT = {
    REASON_TOO_COMPLEX: "the {key} argument tree exceeds the policy scan bounds",
    REASON_SCHEME: "the {key} argument uses a non-http(s) URL scheme",
    REASON_DESTINATION: "the {key} argument targets a denied destination",
    REASON_IP_LITERAL: "the {key} argument is a malformed IP literal",
    REASON_CLUSTER_HOST: "the {key} argument targets an in-cluster host on an egress-class server",
    REASON_RESOLVER_SATURATED: (
        "the {key} argument targets a destination the gateway could not vet (the DNS resolver pool is saturated)"
    ),
}

# Servers whose whole purpose is to reach OUT (a web fetcher/search). Only for
# these do cluster-internal names get denied and DNS resolution run: every
# other fleet server legitimately talks to *.svc.cluster.local.
DEFAULT_EGRESS_SERVERS: tuple[str, ...] = ("searxng",)

# Bound for the argument-time DNS lookup (only egress-class servers, only for
# a name - never for a literal). Shorter than the registration-time timeout:
# this sits on a request path.
_ARG_RESOLVE_TIMEOUT_S = 2.0

# Schemed strings: a scheme is <alpha><alnum+.-*> followed by ':'.
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# The same, but only the AUTHORITY forms: "scheme://host" and the RFC 3986
# network-path reference "//host" (scheme inherited from a base URL). RULE 1
# must not exempt a whitespace-carrying string when that string is a URL whose
# host the tool will contact: the client percent-encodes the space, so the
# bytes on the wire are the refused request's (AUDIT-r5 #1, xv-glm-a).
# Requiring "//" is what keeps "C:\Program Files\x" (drive letter, no
# authority) and prose that merely starts "note: foo bar" in the allowed set.
_URL_WITH_AUTHORITY_RE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:)?//")
# Whitespace/control: the "not a destination" signal (see module docstring).
_NOT_A_TOKEN_RE = re.compile(r"[\s\x00-\x1f\x7f]")
# Bare host[:port][/path?query#frag] - only consulted for destination-ish keys.
_BARE_HOST_RE = re.compile(r"^(?P<host>[A-Za-z0-9._\-]+)(?::(?P<port>\d{1,5}))?(?:[/?#].*)?$", re.DOTALL)
# Argument KEYS that name a destination. Deliberately narrow: 'sql', 'yaml',
# 'manifest', 'content', 'pattern', 'namespace', 'query' are absent on purpose
# - those arguments are payloads, not destinations. 'path'/'paths' ARE keys
# here: a local path never starts with a scheme, so RULE 4 costs nothing, while
# mm-rag's 'paths' is a DOWNLOAD vector (AUDIT-r5 #1 family, xv-glm-a).
_DEST_KEY_RE = re.compile(
    r"(url|uri|endpoint|host|hostname|address|addr|target|base|webhook|callback|link|proxy|peer|dest|origin|referer|site|domain|paths?)",
    re.IGNORECASE,
)
# PAYLOAD argument keys: a scheme-prefixed single token under one of these
# keys is DATA the caller wants stored/executed, not a destination the tool
# will fetch. RULE 3 (the scheme check) is skipped for them.
#
# THE ASYMMETRY, stated once: a payload argument cannot reach a host - the
# tool's own confinement (workspace root, dataset scope, the sandboxed
# executor) bounds what writing it can do - while an argument a tool FEEDS to
# an HTTP client IS the destination. So write_file(content='http://x/'),
# mm-rag add_memory(text='https://...') and dataset_add_documents(texts=[...])
# are legitimate (storing a URL as data); searxng fetch_content(url=...),
# mm-rag dataset_add_documents(paths=['http://...']) (which DOWNLOADS) and
# fetch_content(url=...) stay checked.
#
# DELIBERATELY ABSENT: url, uri, paths, path, source, source_prefix,
# media_url, datasets, endpoint, host, target. 'path' is absent on purpose: a
# LOCAL path never starts with a scheme, so leaving it out costs nothing and
# keeps a URL-valued path argument (a real egress vector) checked. RULE 2
# (a bare IP literal) stays UNCONDITIONAL for every key, payload or not.
#
# ALSO DELIBERATELY ABSENT (audit 2026-11-10 P2-4): command, script, code.
# They were payload keys, which skipped RULE 3 entirely — but a command a
# granted tool EXECUTES is the fleet's strongest egress vector (k8s-ops
# exec_in_pod, workbench run_command), so it must not be exempt from the
# destination rules. They are scanned by _exec_reason instead: the command
# line is split into tokens and each token is classified as a standalone
# destination, which is what catches "curl http://169.254.169.254/x" while
# leaving an ordinary command line ("kubectl get pods -n prod") alone.
# THAT IS A TOKEN SCAN, NOT AN UNDERSTANDING OF THE COMMAND: a destination
# assembled at run time (pipes, base64, $(...), variable expansion), a bare
# hostname token and a sub-threshold numeric literal are NOT caught. The
# module docstring's "WHAT THIS DOES NOT DO" lists those residuals — do not
# read this comment as "commands are destination-checked".
PAYLOAD_ARG_KEYS = frozenset(
    {
        "content",
        "text",
        "texts",
        "body",
        "sql",
        "yaml",
        "manifest",
        "manifest_yaml",
        "pattern",
        "regex",
        "prompt",
        "description",
        "message",
        "note",
        "memory",
        "title",
        "query",
        "label_selector",
    }
)

# EXEC argument keys: a value a tool hands to a SHELL/executor rather than to
# an HTTP client. Not payloads (see the P2-4 note above): the whole point of
# these arguments is that something RUNS them, so a destination inside one is
# a live egress vector, not stored data.
_EXEC_ARG_KEYS = frozenset(
    {
        "command",
        "commands",
        "cmd",
        "commandline",
        "command_line",
        "shell",
        "shell_command",
        "script",
        "code",
        "argv",
    }
)
# Token bounds for the exec scan. A command with MORE tokens than this is
# REFUSED (REASON_TOO_COMPLEX), never partially scanned: truncating the scan
# let padding park the real destination past the slice (cross-validation wave
# 1, P1). The bound keeps one gated call's scan work linear and small.
_EXEC_MAX_TOKENS = 64
# A bare token with no '.' or ':' is only decoded as an IP literal when it is
# at least this long. Shorter values are ports/counts ("80", "443", "0") — and
# the literal decoder reads those as 0.0.x.y inside the denied 0.0.0.0/8, so
# classifying them would refuse ordinary commands. 7 chars is above every
# common port/count and below every compact encoding worth catching
# ("2130706433" = 10, "0x7f000001" = 10).
_EXEC_MIN_BARE_LITERAL = 7
# Shell punctuation that can wrap a destination token without being part of
# it: quotes, command substitution, redirections, list separators.
_EXEC_TOKEN_STRIP = "\"'`$(){}[]<>,;|&\\"
# Tokens are split on whitespace AND '=' so "--url=http://x" and "FOO=http://x"
# present their value as its own token.
_EXEC_TOKEN_SPLIT_RE = re.compile(r"[\s=]+")
# A token is only address-shaped (and therefore worth classifying) when it
# carries a '.' or a ':'. Bare numbers are EXCLUDED on purpose: "80", "443",
# "0" and "8080" are ports/counts in ordinary command lines, while the literal
# decoder reads them as 0.0.x.y addresses inside the denied 0.0.0.0/8.
_EXEC_ADDRESS_SHAPE_RE = re.compile(r"[.:]")

# Telemetry shapes (D6 - observation only, never a refusal).
# Base64 blob: the base64 alphabet only, after dropping line-wrapping
# newlines (a chart .tgz base64 is often folded). Deliberately NOT allowing
# spaces: prose and SQL ("SELECT 1 SELECT 1 ...") are letters+digits+spaces and
# would otherwise be flagged as blobs, which would make the signal useless.
_B64_ALPHABET_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_LONG_QUERY = 256
_LONG_PATH = 256
_B64_MIN = 512
_TELEMETRY_SCAN_CHARS = 64 * 1024

# Per-tool file: allowlist. file: URLs are DENIED by default; a tool whose
# arguments are genuinely local file references can be admitted here with the
# path prefixes it may name. Empty today ON PURPOSE: no fleet tool takes a
# file:// URL argument (workbench takes workspace-relative paths, mm-rag takes
# dataset-relative paths), so the honest default is deny.
FILE_SCHEME_TOOLS: dict[str, tuple[str, ...]] = {}


# ---------------------------------------------------------------------------
# mode + exemptions
# ---------------------------------------------------------------------------


def policy_mode(explicit: str = "") -> str:
    """Resolve the posture: explicit arg, else PCAI_LLM_MCP_ARG_POLICY.

    Fail CLOSED: an unrecognised value (operator typo, empty env) resolves to
    'enforce' with a warning - never to 'off'. A control that a typo can
    silently disable is not a control."""
    raw = (explicit or getattr(settings, "mcp_arg_policy", "enforce") or "enforce").strip().lower()
    if raw in ("enforce", "warn", "off"):
        return raw
    log.warning("mcp arg policy: unrecognised mode %r - failing closed to 'enforce'", raw)
    return "enforce"


def is_exempt(server: str, tool: str) -> bool:
    """True when PCAI_LLM_MCP_ARG_POLICY_EXEMPT names this (server, tool).

    Patterns are fnmatch globs matched against 'server.tool' (so 'searxng.*'
    exempts a whole server). Case-sensitive: server and tool names are."""
    patterns = getattr(settings, "mcp_arg_policy_exempt", None) or []
    if isinstance(patterns, str):  # defensive: a comma-separated env string
        patterns = [p for p in patterns.split(",") if p.strip()]
    pair = f"{server}.{tool}"
    for pattern in patterns:
        pat = str(pattern).strip()
        if pat and fnmatch.fnmatchcase(pair, pat):
            return True
    return False


def is_egress_server(server: str) -> bool:
    """True when the server is egress-class (it reaches OUT on the caller's
    behalf): cluster-internal names are denied and DNS is resolved."""
    patterns = getattr(settings, "mcp_arg_egress_servers", None)
    if not patterns:
        patterns = DEFAULT_EGRESS_SERVERS
    if isinstance(patterns, str):
        patterns = [p for p in patterns.split(",") if p.strip()]
    return any(fnmatch.fnmatchcase(server, str(p).strip()) for p in patterns if str(p).strip())


# ---------------------------------------------------------------------------
# the bounded recursive walk
# ---------------------------------------------------------------------------


class _ScanResult(list[tuple[str, str]]):
    """iter_string_arguments' return: the scanned strings plus an overflow
    flag. Audit C-1 — the scan bounds fail CLOSED: an argument tree beyond
    either bound is refused by the gate, never partially scanned."""

    overflowed: bool = False


def iter_string_arguments(
    arguments: Any,
    *,
    max_depth: int = MAX_DEPTH,
    max_strings: int = MAX_STRINGS,
    max_nodes: int = MAX_NODES,
) -> _ScanResult:
    """Every string VALUE in arguments as (nearest-key-hint, value).

    Bounded four ways: depth (MAX_DEPTH), string count (MAX_STRINGS), visited
    node count (MAX_NODES) and, per string, MAX_SCAN_CHARS applied by the
    classifier. Iterative, so a hostile deeply-nested payload cannot blow the
    Python stack. Keys are hints only - they name the argument for the refusal
    message and for the destination-ish bare-host rule; a key is never scanned
    as a value.

    FAIL-CLOSED (audit C-1): the bounds are a REFUSAL TRIGGER, not a
    truncation. This walk used to stop silently at the 200th string and skip
    depth-exceeded subtrees, so padding the arguments with benign values
    parked a denied destination past the scan in a stage whose own posture is
    "a security predicate that fails open is not a control". Exceeding ANY
    bound now marks .overflowed (the scan still returns what it saw); the
    gate refuses with REASON_TOO_COMPLEX instead of trusting a partial scan.
    The walk remains O(nodes) and iterative - the bound exists to cap the
    work per call, and the tree is already fully materialised by the body
    reader, so refusing costs nothing."""
    out = _ScanResult()
    stack: list[tuple[Any, str, int]] = [(arguments, "", 0)]
    overflow = False
    nodes = 0
    while stack:
        node, hint, depth = stack.pop()
        # Audit P2-32: the NODE bound. Depth and string count never fire on a
        # tree of empty containers, so this is the counter that caps the work.
        nodes += 1
        if nodes > max_nodes:
            overflow = True
            break
        if depth > max_depth:
            overflow = True
            continue
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if len(out) < max_strings:
                        out.append((str(key), value))
                    else:
                        overflow = True
                elif isinstance(value, (dict, list, tuple)):
                    stack.append((value, str(key), depth + 1))
        elif isinstance(node, (list, tuple)):
            for item in node:
                if isinstance(item, str):
                    if len(out) < max_strings:
                        out.append((hint, item))
                    else:
                        overflow = True
                elif isinstance(item, (dict, list, tuple)):
                    stack.append((item, hint, depth + 1))
    out.overflowed = overflow
    return out


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def _refusal_message(reason: str, key: str) -> str:
    """Bounded refusal text: a fixed sentence per reason + a short, sanitized
    argument name. The caller's URL/host text is NEVER echoed (it would land
    in the caller's context and in logs as reflected input)."""
    label = re.sub(r"[^A-Za-z0-9_.\-]", "_", (key or "argument"))[:40] or "argument"
    return f"refused by the MCP argument policy ({reason}): " + _REASON_TEXT[reason].format(key=label)


def _resolved_host_reason(host: str) -> str:
    """Deny reason when an egress-class HOSTNAME resolves into a denied range.

    Resolution FAILURE is not evidence of danger (url_guard design rule 4): a
    resolver that cannot answer says nothing about the name, so we log and
    allow - the call-time controls (follow_redirects=False) remain.

    RESOLVER SATURATION IS NOT A RESOLUTION FAILURE and must NOT take that
    fail-open path (audit follow-up, guardrails resolver work): the bounded
    pool answering "I have no capacity" says nothing about the NAME but a lot
    about the check — the destination was never vetted, and a model that wedges
    the pool could otherwise switch the SSRF gate off for its own next call.
    url_guard.ResolverSaturated is deliberately not a gaierror/OSError, so it
    is re-raised HERE, before the broad degradation handler, and turned into a
    bounded refusal at check_arguments_detail."""
    try:
        addresses = url_guard.resolve_host_addresses(host, timeout_s=_ARG_RESOLVE_TIMEOUT_S)
    except url_guard.ResolverSaturated:
        raise
    except Exception as exc:  # gaierror, OSError, UnicodeError, timeout
        log.debug("mcp arg policy: could not resolve %r (%s) - resolution check skipped", host, exc)
        return ""
    for address in addresses:
        if url_guard.denied_ip_reason(address, extra_cidrs=settings.url_deny_cidrs):
            return REASON_DESTINATION
    return ""


def _host_reason(server: str, host: str) -> str:
    """Deny reason for one HOST (parsed out of a URL, or a bare host
    argument), else ''.

    Reuses core.url_guard predicates verbatim - this module must never grow a
    second deny-list (spec D1)."""
    h = (host or "").strip().lower()
    if not h:
        return REASON_DESTINATION  # a URL with no host is unparseable: refuse
    # Trailing-dot FQDN forms are the same endpoint as their dotless form;
    # strip EXACTLY one dot (a host still ending in '.' is refused) - the
    # routes_admin._check_url hardening, applied to arguments too.
    if h.endswith("."):
        stripped = h[:-1]
        if stripped.endswith("."):
            return REASON_DESTINATION
        h = stripped
    if h == "localhost" or h.endswith(".localhost"):
        return REASON_DESTINATION
    if url_guard.denied_ip_reason(h, extra_cidrs=settings.url_deny_cidrs):
        return REASON_DESTINATION
    # An ALLOWED address literal (RFC1918 stays allowed - this gateway's
    # legitimate upstreams ARE private) needs no further check.
    if url_guard.decode_ip_literal(h) is not None:
        return ""
    # IP-SHAPED but undecodable: a malformed literal is refused, never treated
    # as a hostname (the exact bypass url_guard closes).
    if url_guard.looks_like_ip_literal(h):
        return REASON_IP_LITERAL
    # --- hostname path ---
    if is_egress_server(server):
        if getattr(settings, "mcp_arg_deny_cluster_hosts", True) and url_guard.is_cluster_host(h):
            return REASON_CLUSTER_HOST
        if getattr(settings, "mcp_arg_policy_resolve", True):
            return _resolved_host_reason(h)
    return ""


def _url_reason(server: str, tool: str, text: str, scheme: str) -> str:
    """Deny reason for a string that STARTS with a scheme, else ''."""
    if scheme in ("http", "https"):
        try:
            parsed = urlparse(text)
        except ValueError:
            return REASON_DESTINATION
        # AUDIT w6 F3 (the WHATWG parse differential): this parser takes the
        # host after the LAST "@" while a WHATWG-grade client ends the
        # authority at the first "/" or backslash — "http://127.0.0.1\@evil.com/"
        # parsed here as host "evil.com" (allowed, DNS-checked) while such a
        # client connects to 127.0.0.1. Conservative rule, stated once: an
        # authority carrying USERINFO at all, or a BACKSLASH anywhere, is
        # refused — legitimate MCP destinations never carry credentials in
        # the URL.
        if "@" in parsed.netloc or "\\" in parsed.netloc:
            return REASON_DESTINATION
        return _host_reason(server, parsed.hostname or "")
    if scheme == "file":
        # file: is denied unless the tool is explicitly known file-taking AND
        # the path sits inside one of its allowed prefixes (both empty today).
        allowed = FILE_SCHEME_TOOLS.get(tool) or FILE_SCHEME_TOOLS.get(f"{server}.{tool}")
        if allowed:
            try:
                path = urlparse(text).path or ""
            except ValueError:
                path = ""
            if path and any(path.startswith(prefix) for prefix in allowed):
                return ""
        return REASON_SCHEME
    return REASON_SCHEME


def _key_base(key: str) -> str:
    """The bare argument name, lowercased: 'X-Media-URL'/'media_url' ->
    'media_url'. PAYLOAD_ARG_KEYS is matched on this, so a casing or
    separator variant cannot slip past the payload rule."""
    return re.sub(r"[^a-z0-9]+", "_", (key or "").strip().lower()).strip("_")


def _parent_relative(host_run: str) -> bool:
    """True when a _BARE_HOST_RE host run is really a PARENT-RELATIVE path.

    The regex stops the host at the first "/", so "../up/x.txt" yields the
    host run ".."; _host_reason's trailing-dot branch (written for the FQDN
    "example.com." case) then strips one dot and refuses the residual ".".
    A DNS name never contains an empty label, so a run holding ".." is not a
    host and the caller must fall through to allowed: wave 1 accepted these
    paths and an enforce-mode refusal of "../x" is a false positive the model
    cannot spell its way out of (wave-2 review of the "paths?" delta)."""
    return ".." in host_run


def _exec_reason(server: str, tool: str, text: str) -> str:
    """Deny reason for one EXEC-class argument (a command / script / code), else ''.

    Audit 2026-11-10 P2-4: command/script/code used to be payload keys, so the
    destination rules never saw them — and a command a granted tool EXECUTES
    (k8s-ops exec_in_pod, workbench run_command) is the fleet's strongest
    egress vector. The classifier only ever classifies a WHOLE string, so the
    command line is split into tokens and each ADDRESS-SHAPED token is
    classified by the same rules (RULE 2 for an IP literal, RULE 3 for a
    scheme URL). An ordinary command line ("kubectl get pods -n prod",
    "grep -r foo .") carries no such token and stays allowed.

    BOUNDS ARE A REFUSAL, NOT A TRUNCATION (cross-validation wave 1, P1). The
    scan used to take the first _EXEC_MAX_TOKENS tokens and allow the rest, so
    ~96 characters of "A=1 " padding parked the real destination past the
    slice and the call was allowed. A command line with more tokens than the
    bound is now REFUSED with REASON_TOO_COMPLEX — the posture
    iter_string_arguments already states for its own bounds.

    Each candidate token is percent-DECODED once before classification, the
    same contract as the main path (and what curl/a WHATWG client does):
    without it "http://%31%32%37.0.0.1/" parsed as an unresolvable NAME and
    was allowed, while the client connects to 127.0.0.1.

    Deliberately NOT done here: a bare NAME token is never bare-host checked
    and never DNS-resolved. A command line is full of dotted non-destinations
    ("--field-selector=metadata.name=x", file names), and a 2 s resolution per
    token on a request path is a latency bomb for no gain. SHORT bare numbers
    are exempt too ("80", "443", "0" are ports/counts, and the literal decoder
    reads them as 0.0.x.y inside the denied 0.0.0.0/8), but a bare
    numeric/hex literal of at least _EXEC_MIN_BARE_LITERAL chars IS decoded
    and range-checked: "2130706433" and "0x7f000001" are both 127.0.0.1.

    The residual gaps (pipes/base64/subshell constructions, bare hostnames,
    sub-threshold numerics) are listed in the module docstring's "WHAT THIS
    DOES NOT DO" — this scan raises the cost of the trivial spelling, it does
    not make exec_in_pod safe.
    """
    tokens = _EXEC_TOKEN_SPLIT_RE.split(text)
    if len(tokens) > _EXEC_MAX_TOKENS:
        # FAIL CLOSED: scanning a prefix would let padding hide the destination.
        return REASON_TOO_COMPLEX
    for token in tokens:
        candidate = token.strip(_EXEC_TOKEN_STRIP)
        if not candidate:
            continue
        # Percent-decode ONCE (the main path's contract): the client decodes,
        # so "%31%32%37.0.0.1" must classify as 127.0.0.1, not as a name.
        candidate = unquote(candidate) if "%" in candidate else candidate
        if not candidate:
            continue
        scheme = _SCHEME_RE.match(candidate)
        if scheme:
            reason = _url_reason(server, tool, candidate, scheme.group(1).lower())
            if reason:
                return reason
            continue
        if _EXEC_ADDRESS_SHAPE_RE.search(candidate):
            if candidate.startswith("[") and "]" in candidate:
                literal = candidate[1 : candidate.index("]")]
                if literal and url_guard.denied_ip_reason(literal, extra_cidrs=settings.url_deny_cidrs):
                    return REASON_DESTINATION
                continue
            bare = _BARE_HOST_RE.match(candidate)
            host = bare.group("host") if bare else ""
            if not host or _parent_relative(host):
                continue
            if not (url_guard.looks_like_ip_literal(host) or url_guard.decode_ip_literal(host) is not None):
                continue  # a NAME: never DNS-resolved from a command line
            reason = _host_reason(server, host)
            if reason:
                return reason
            continue
        # No "." and no ":" — still an address literal in a COMPACT encoding
        # ("2130706433", "0x7f000001"). Short values stay exempt: they are
        # ports/counts far more often than addresses.
        if len(candidate) < _EXEC_MIN_BARE_LITERAL:
            continue
        if not (url_guard.looks_like_ip_literal(candidate) or url_guard.decode_ip_literal(candidate) is not None):
            continue
        if url_guard.denied_ip_reason(candidate, extra_cidrs=settings.url_deny_cidrs):
            return REASON_DESTINATION
        if url_guard.decode_ip_literal(candidate) is None:
            return REASON_IP_LITERAL
    return ""


def check_string(server: str, tool: str, key: str, value: str) -> str:
    """Deny reason for ONE argument string, else '' (the classification rule
    from the module docstring, executable)."""
    if not isinstance(value, str) or not value:
        return ""
    text = value.strip()[:MAX_SCAN_CHARS]
    # EXEC-class arguments (a command/script/code the tool will RUN) are
    # scanned as a TOKEN LIST first: RULE 1 below exempts any string carrying
    # whitespace, which is every real command line (audit P2-4).
    if _key_base(key) in _EXEC_ARG_KEYS:
        return _exec_reason(server, tool, text)
    # A URL fragment is NEVER transmitted, so it cannot change where the tool
    # sends the request - drop it BEFORE classification. Otherwise a fragment
    # is a free bypass of every rule below: "#%20" (or a raw space inside the
    # fragment) makes RULE 1 see whitespace and return ALLOWED for the exact
    # request whose fragment-less twin is refused (AUDIT-r5 #1).
    text = text.split("#", 1)[0]
    if not text:
        return ""
    # RULE 1: a destination is a single token. Whitespace/control characters
    # mean SQL / YAML / prose / a command line / a multi-line regex - not a
    # destination. This is the primary false-positive guard. The ONE exception
    # is a hierarchical URL: the client percent-encodes a raw space, so
    # "http://denied/a b" puts the same bytes on the wire as the refused
    # ".../a%20b" (AUDIT-r5 #1).
    if _NOT_A_TOKEN_RE.search(text) and not _URL_WITH_AUTHORITY_RE.match(text):
        return ""
    # RULE 1b: a protocol-relative reference ("//host/path", the RFC 3986
    # network-path form) IS a destination for any tool that urljoin()s the
    # argument against a base URL: urljoin("https://ok/", "//169.254.169.254/x")
    # keeps the scheme and takes the DENIED host. A single leading "/" is a
    # path reference and stays allowed, and the bare-host key gate is RULE 4's,
    # so a payload key is untouched (AUDIT-r5 #1, cross-review xv-glm-a).
    if text.startswith("//") and _DEST_KEY_RE.search(key or ""):
        rest = text[2:]
        # AUDIT w6 F3: the SAME authority rule as the scheme branch — the
        # authority of a network-path reference ends at the first "/" and a
        # WHATWG client also ends it at a backslash, so userinfo or a
        # backslash here is the same parse differential.
        authority = rest.split("/", 1)[0]
        if "@" in authority or "\\" in authority:
            return REASON_DESTINATION
        if rest.startswith("["):
            # A bracketed IPv6 literal: _BARE_HOST_RE's host class has no
            # brackets, so the address goes through the same url_guard decode
            # RULE 2 uses. An unterminated "[" is not a host and falls through.
            end = rest.find("]")
            literal = rest[1:end] if end > 0 else ""
            if literal and url_guard.denied_ip_reason(literal, extra_cidrs=settings.url_deny_cidrs):
                return REASON_DESTINATION
        else:
            bare = _BARE_HOST_RE.match(rest)
            if bare and not _parent_relative(bare.group("host")):
                return _host_reason(server, bare.group("host"))
    # Percent-encoding is decoded ONCE before classification (a web server
    # decodes it too). A percent-encoded '#' is the same fragment delimiter,
    # so the fragment is dropped after the decode as well (AUDIT-r5 #1).
    decoded = unquote(text) if "%" in text else text
    decoded = decoded.split("#", 1)[0]
    if not decoded:
        return ""
    # A decode that introduces whitespace means the payload was not a
    # destination token after all - UNLESS the caller's string STARTS with a
    # scheme, i.e. it IS a URL: the fragment is gone and RULE 1 already proved
    # the raw text carries no whitespace, so that whitespace can only sit in
    # the path/query while the host stays the destination. Exempting
    # URL-shaped strings was the AUDIT-r5 #1 bypass ("?q=%20", "/a%20b",
    # "gopher://127.0.0.1:6379/_x%20").
    if _NOT_A_TOKEN_RE.search(decoded) and not _SCHEME_RE.match(text):
        return ""
    # RULE 2: the WHOLE string is an IP literal in ANY encoding.
    if url_guard.looks_like_ip_literal(decoded) or url_guard.decode_ip_literal(decoded) is not None:
        if url_guard.denied_ip_reason(decoded, extra_cidrs=settings.url_deny_cidrs):
            return REASON_DESTINATION
        if url_guard.decode_ip_literal(decoded) is None:
            return REASON_IP_LITERAL
        return ""
    # RULE 3: URL-shaped (starts with a scheme) - for DESTINATION arguments.
    # A PAYLOAD key (content/text/sql/manifest/...) is data the caller wants
    # STORED, never a host the tool will fetch, so a URL-valued payload is
    # allowed (see PAYLOAD_ARG_KEYS). RULE 2 above already ran, so a bare IP
    # literal is refused under ANY key, and an EXEC-class key was already
    # scanned token-by-token by _exec_reason above.
    scheme_match = _SCHEME_RE.match(decoded)
    if scheme_match and _key_base(key) not in PAYLOAD_ARG_KEYS:
        return _url_reason(server, tool, decoded, scheme_match.group(1).lower())
    # RULE 4: bare host[:port][/path] - only for destination-named keys.
    if _DEST_KEY_RE.search(key or ""):
        bare = _BARE_HOST_RE.match(decoded)
        if bare and not _parent_relative(bare.group("host")):
            return _host_reason(server, bare.group("host"))
    return ""


# ---------------------------------------------------------------------------
# public surface (pinned by SPEC 3.2)
# ---------------------------------------------------------------------------


def check_arguments_detail(
    server: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    mode: str = "",
) -> tuple[str, str]:
    """(reason, refusal_message) - ('', '') when the call may proceed.

    The reason is the BOUNDED label the gate chain hands to
    observe_mcp_policy_block; the message is what the caller sees. Split out
    of check_arguments (whose pinned signature returns only the message) so
    the metric never has to parse a human sentence.

    In 'warn' mode a hit is logged and counted HERE and ('', '') is returned -
    the call proceeds (the pinned warn contract). In 'enforce' mode the CALLER
    counts the refusal (one metric bump per refusal, not two)."""
    resolved = policy_mode(mode)
    if resolved == "off":
        return "", ""
    if not isinstance(arguments, dict) or not arguments:
        return "", ""
    if is_exempt(server, tool):
        return "", ""
    scanned = iter_string_arguments(arguments)
    if scanned.overflowed:
        # Audit C-1: a tree past the depth/string bounds is REFUSED, not
        # partially scanned — a denied destination parked past the old
        # 200-string truncation point was silently admitted.
        reason = REASON_TOO_COMPLEX
        message = (
            "refused by the MCP argument policy (" + REASON_TOO_COMPLEX + "): "
            "the argument tree exceeds the policy scan bounds"
        )
        if resolved == "warn":
            _metrics.observe_mcp_policy_block(server, tool, reason)
            log.warning("mcp arg policy (warn, not refused): %s/%s %s", server, tool, message)
            return "", ""
        return reason, message
    for key, value in scanned:
        try:
            reason = check_string(server, tool, key, value)
        except url_guard.ResolverSaturated:
            # FAIL CLOSED (audit follow-up): the resolver pool refused to vet
            # this destination, so the check did NOT happen. url_guard raises a
            # distinct type precisely so this stage can tell "no capacity" from
            # "the name does not resolve" (the latter stays fail-open above).
            # Mapped to a bounded reason here — the gate chain turns a refusal
            # MESSAGE into a policy_blocked settle; a raised exception would
            # escape the stage as a 500 instead.
            reason = REASON_RESOLVER_SATURATED
        if not reason:
            continue
        message = _refusal_message(reason, key)
        if resolved == "warn":
            _metrics.observe_mcp_policy_block(server, tool, reason)
            log.warning("mcp arg policy (warn, not refused): %s/%s %s", server, tool, message)
            return "", ""
        return reason, message
    return "", ""


def check_arguments(server: str, tool: str, arguments: dict[str, Any], *, mode: str = "") -> str:
    """Return '' when the call may proceed, else a REFUSAL MESSAGE.

    mode: enforce | warn | off ('' -> settings.mcp_arg_policy).
    A warn-mode hit still returns '' (it logs + counts only)."""
    return check_arguments_detail(server, tool, arguments, mode=mode)[1]


def scan_payload_signals(server: str, tool: str, arguments: dict[str, Any]) -> list[str]:
    """Payload-shaped signals for TELEMETRY ONLY: base64-blob | long-query |
    long-path (SPEC D6).

    Never a refusal: a legitimate large payload (a chart tarball, a document
    upload) has exactly these shapes, and the plan itself says the payload
    axis needs an egress proxy rather than a heuristic. Gated by
    PCAI_LLM_MCP_ARG_TELEMETRY (default on)."""
    if not getattr(settings, "mcp_arg_telemetry", True):
        return []
    if not isinstance(arguments, dict) or not arguments:
        return []
    signals: list[str] = []
    for _key, value in iter_string_arguments(arguments):
        text = value.strip()
        if not text:
            continue
        head = text[:_TELEMETRY_SCAN_CHARS]
        if len(text) > _B64_MIN and _B64_ALPHABET_RE.match(head.replace("\r", "").replace("\n", "")):
            if "base64-blob" not in signals:
                signals.append("base64-blob")
            continue
        if _NOT_A_TOKEN_RE.search(head):
            continue
        if not _SCHEME_RE.match(head):
            continue
        try:
            parsed = urlparse(head)
        except ValueError:
            continue
        if len(parsed.query) > _LONG_QUERY and "long-query" not in signals:
            signals.append("long-query")
        if len(parsed.path) > _LONG_PATH and "long-path" not in signals:
            signals.append("long-path")
    return signals


# ---------------------------------------------------------------------------
# the gate-chain entry point (ONE call per path, both paths)
# ---------------------------------------------------------------------------


class PolicyDecision:
    """The gate chain's verdict for one call. Truthy refusal via .refused.

    reason    - bounded metric label (never caller text)
    message   - the refusal text handed to the caller, or ''
    telemetry - payload signals observed (never a refusal; D6)
    """

    __slots__ = ("message", "reason", "telemetry")

    def __init__(self, reason: str = "", message: str = "", telemetry: tuple[str, ...] = ()) -> None:
        self.reason = reason
        self.message = message
        self.telemetry = telemetry

    @property
    def refused(self) -> bool:
        return bool(self.message)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PolicyDecision(refused={self.refused!r}, reason={self.reason!r}, telemetry={self.telemetry!r})"


def _count_block(server: str, tool: str, reason: str) -> None:
    """One refusal, two bounded counters: the policy-block counter (labelled
    by the bounded reason) and the per-server call-outcome counter that makes
    the unattributed share alertable (P2-1)."""
    _metrics.observe_mcp_policy_block(server, tool, reason)
    _metrics.observe_mcp_call(server, "policy_blocked", "")


def gate_call(
    server: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    key_mcp_admin: bool = False,
    grants: set[tuple[str, str]] | None = None,
) -> PolicyDecision:
    """Run the whole argument/grant-level policy stage for ONE call.

    ORDER IS THE CONTRACT (SPEC 3.2 items 1-4) and it is the same on both
    paths - /mcp and the relay:
      1. admin_tool_refusal   (Z2 predicate) -> refuse (reason 'admin-tool')
      2. check_arguments      (this module)  -> refuse (its own bounded reason)
      3. scan_payload_signals (this module)  -> telemetry only, never refuses
      4. read_egress_violation(Z2 predicate) -> enforce: refuse; warn: count+log

    Callers must have already applied the key ACL: an ungranted tool must
    never learn policy details, so this stage runs AFTER the ACL gate.

    FAIL-CLOSED SEAM (cross-validation wave 1, P1): every Z2 predicate is
    resolved at call time, and a MISSING symbol or a FAULT inside one REFUSES
    with its own bounded reason ('admin-tool-refusal-unavailable' /
    'read-egress-check-unavailable'). A control that degrades to "allowed"
    when its predicate breaks is not a control — that wrapper would have
    re-opened sqlhandler.admin_* on any partial deploy.
    """
    # --- 1. admin-control-plane tools never ride the model-driven surface ---
    refusal = _admin_tool_refusal(server, tool, bool(key_mcp_admin))
    if refusal:
        # The unavailable-guard refusals are distinguishable from a policy
        # decision: an operator alerting on them sees a broken control, not a
        # caller doing something wrong.
        reason = (
            ADMIN_REFUSAL_UNAVAILABLE if refusal == _UNAVAILABLE_TEXT[ADMIN_REFUSAL_UNAVAILABLE] else ADMIN_TOOL_REASON
        )
        _count_block(server, tool, reason)
        return PolicyDecision(reason=reason, message=refusal)

    # --- 2. argument-level destination policy ---
    reason, message = check_arguments_detail(server, tool, arguments)
    if message:
        _count_block(server, tool, reason)
        return PolicyDecision(reason=reason, message=message)

    # --- 3. payload-shaped telemetry (D6): observe, never refuse ---
    signals = tuple(scan_payload_signals(server, tool, arguments))
    for signal in signals:
        _metrics.observe_mcp_arg_telemetry(server, tool, signal)

    # --- 4. read+egress separation (the article's attack combination) ---
    try:
        violation = _read_egress_violation(grants) if grants is not None else None
    except RuntimeError:
        # The predicate is missing or faulted: refuse rather than skip the
        # check (fail closed — see the posture note above).
        _count_block(server, tool, READ_EGRESS_UNAVAILABLE)
        return PolicyDecision(
            reason=READ_EGRESS_UNAVAILABLE,
            message=_UNAVAILABLE_TEXT[READ_EGRESS_UNAVAILABLE],
        )
    if violation is not None:
        read_pair, egress_pair = violation
        detail = f"{read_pair[0]}.{read_pair[1]} + {egress_pair[0]}.{egress_pair[1]}"
        mode = _separate_read_egress_mode()
        if mode == "enforce":
            enforce_reason = _read_egress_enforce_reason()
            _count_block(server, tool, enforce_reason)
            return PolicyDecision(
                reason=enforce_reason,
                message=(
                    "refused by the MCP grant policy (read+egress): this key holds BOTH a read "
                    f"grant ({read_pair[0]}.{read_pair[1]}) and an egress grant "
                    f"({egress_pair[0]}.{egress_pair[1]}); split them across keys "
                    "(PCAI_LLM_MCP_SEPARATE_READ_EGRESS)"
                ),
            )
        if mode == "warn":
            # DISTINCT reason for the non-refusal (wave-1 review P2-3b): the
            # call PROCEEDS here, so ticking the enforce token would make the
            # metric claim a block that never happened.
            _count_block(server, tool, _read_egress_warn_reason())
            log.warning(
                "mcp grant policy (warn, not refused): key holds BOTH classes (%s) — "
                "the read-then-egress combination is the exfiltration pattern",
                detail,
            )
    return PolicyDecision(telemetry=signals)
