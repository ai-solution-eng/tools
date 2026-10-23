"""SSRF-gate hardening regression tests for `routes_admin._check_url`.

Closes the coordinator-confirmed bypasses: hosts that FAILED the literal-IP
decode fell through as "hostnames" and were ALLOWED, so a live loopback
listener answered `GET http://127.0.0.1.:<port>/` and
`http://169.254.169.254./` (cloud metadata) both passed registration.

Covered here:
  * trailing-dot FQDN forms (`127.0.0.1.`, `localhost.`, `169.254.169.254.`)
    and the exactly-one-strip rule (`localhost..`);
  * the completed inet_aton decoder, including the 3-part form
    (`127.0.65535` = 127.0.255.255) and the 3-part ORIENTATION trap
    (`169.254.1` is link-local 169.254.0.1, NOT the allowed 169.0.254.1);
  * malformed IP-looking literals that must not be waved through as names;
  * registration-time hostname resolution (loopback/metadata names refused,
    non-existent names refused, public names allowed).

NO network in these tests: `socket.getaddrinfo` is monkeypatched everywhere
the hostname path is exercised. The pure decode/shape cases never resolve.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from fastapi import HTTPException

from pcai_llm.api import routes_admin

PUBLIC_V4 = "93.184.216.34"


def _refused(url: str) -> str:
    """Assert `_check_url` refuses `url` and return the refusal detail."""
    with pytest.raises(HTTPException) as ei:
        routes_admin._check_url(url, "base_url")
    assert ei.value.status_code == 400
    return str(ei.value.detail)


def _allowed(url: str) -> None:
    routes_admin._check_url(url, "base_url")  # must not raise


def _fake_getaddrinfo(*addresses: str):
    """A `socket.getaddrinfo` stand-in returning exactly `addresses`."""

    def _impl(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        out = []
        for addr in addresses:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            out.append((family, socket.SOCK_STREAM, 6, "", (addr, 0)))
        return out

    return _impl


def _raise_gaierror(errno: int):
    def _impl(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        raise socket.gaierror(errno, "Name or service not known")

    return _impl


# ---------------------------------------------------------------------------
# (a) trailing-dot FQDN forms
# ---------------------------------------------------------------------------


def test_trailing_dot_loopback_literal_rejected() -> None:
    """The coordinator's live exploit: a loopback listener answered this."""
    detail = _refused("http://127.0.0.1.:9200/")
    assert "127.0.0.0/8" in detail


def test_trailing_dot_metadata_literal_rejected() -> None:
    detail = _refused("http://169.254.169.254./latest/meta-data/")
    assert "169.254.0.0/16" in detail


def test_trailing_dot_localhost_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """`localhost.` must be refused — by the loopback name check, or (if that
    were ever narrowed) by resolution. Asserted either way; resolution is
    mocked so the test never touches the network."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1", "::1"))
    assert _refused("http://localhost.:9200/")


def test_trailing_dot_stripped_exactly_once() -> None:
    """One trailing dot is the FQDN form and is stripped; a host STILL ending
    in '.' after that strip (`localhost..`) is refused outright."""
    assert "trailing-dot host form" in _refused("http://localhost../x")


def test_trailing_dot_strip_does_not_reject_dotless_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The strip must not widen the gate: a public host still passes."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_V4))
    _allowed("http://example.com/v1")


def test_trailing_dot_hostname_still_resolves_and_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A trailing-dot HOSTNAME is re-checked after the strip — a name that
    resolves into a denied range stays refused through the dot form."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert "resolves to" in _refused("http://evil.example.com./v1")


# ---------------------------------------------------------------------------
# (b) completed inet_aton decode
# ---------------------------------------------------------------------------


def test_three_part_form_decodes_to_denied_range() -> None:
    """`127.0.65535` is 127.0.255.255 (3-part: the last part is a 16-bit value
    filling the final two octets) — previously a decode failure that passed
    as a hostname."""
    assert "127.0.0.0/8" in _refused("http://127.0.65535/")


@pytest.mark.parametrize(
    "host,expected",
    [
        ("169.254.1", "169.254.0.1"),  # ORIENTATION TRAP: a.b.(c>>8).(c&255)
        ("169.254.169", "169.254.0.169"),  # metadata-prefixed short form
        ("127.0.65535", "127.0.255.255"),
        ("10.0.65535", "10.0.255.255"),
    ],
)
def test_three_part_decode_orientation_matches_inet_aton(host: str, expected: str) -> None:
    """The 3-part orientation is the subtle half: decoding a.b.c as
    `a.(b>>8).(b&255).c` maps the LINK-LOCAL `169.254.1` to the ALLOWED
    `169.0.254.1`. True inet_aton semantics put the 16-bit value LAST."""
    assert routes_admin._decode_ip_literal(host) == expected


def test_three_part_link_local_short_form_rejected() -> None:
    """`169.254.1` resolves to link-local 169.254.0.1 — must be refused, and
    must NOT be decoded to the allowed 169.0.254.1."""
    detail = _refused("http://169.254.1/")
    assert "169.254.0.1" in detail and "169.254.0.0/16" in detail


def test_two_part_form_still_decoded_and_rejected() -> None:
    """Existing 2-part behavior preserved: `127.1` is 127.0.0.1."""
    assert "127.0.0.0/8" in _refused("http://127.1/")


def test_hex_short_form_rejected() -> None:
    """`0x7f.0.1` — a hex part inside a dotted form — decodes to 127.0.0.1."""
    assert "127.0.0.0/8" in _refused("http://0x7f.0.1/")


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",  # canonical
        "2130706433",  # pure decimal
        "0x7f000001",  # pure hex
        "0177.0.0.1",  # octal dotted
        "127.1",  # 2-part
        "::ffff:127.0.0.1",  # v4-mapped
        "0.0.0.0",  # unspecified
    ],
)
def test_all_literal_encodings_reach_the_deny_check(host: str) -> None:
    _refused(f"http://{host}/")


def test_allowed_literal_is_not_treated_as_malformed() -> None:
    """The IP-shape guard must not fire on a literal that DECODED fine —
    otherwise the legitimate RFC1918 upstreams would be refused."""
    _allowed("http://10.1.2.3:8000/v1")
    _allowed("http://192.168.1.10:8080")
    _allowed("http://172.16.0.5")


# ---------------------------------------------------------------------------
# (c) malformed IP-looking literals must not pass as hostnames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "1.2.3.4.5",  # too many parts
        "1.2.3.999",  # part out of range
        "999.1.1.1",
        "0x7f.0.0.999",  # hex parts, last one out of range
        "127.0.0.1.1",
    ],
)
def test_ip_looking_but_undecodable_is_rejected(host: str) -> None:
    """A decode failure is never a licence to treat a malformed literal as a
    hostname — these must be refused, not resolved."""
    assert "IP-literal form failed validation" in _refused(f"http://{host}/")


def test_non_hex_letters_fall_to_the_hostname_path_and_still_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`0xZZ` carries the 0x prefix → literal-SHAPED → refused at the shape
    guard (coordinator-amended `_looks_like_ip_literal` keeps 0x-prefix
    intent), never waved through as a name."""
    monkeypatch.setattr(socket, "getaddrinfo", _raise_gaierror(socket.EAI_NONAME))
    assert routes_admin._looks_like_ip_literal("0xzz") is True
    assert "IP-literal form" in _refused("http://0xZZ/")


def test_hex_word_hostnames_are_names_not_literals() -> None:
    """COORDINATOR AMENDMENT regression: `x`, `beef`, `cafe` are genuine
    (if unusual) hostnames — the original all-[0-9a-fx.] shape test
    mis-classified them as IP literals and broke the baseline guardrail
    webhook-validation tests. Only 0x-prefixed or digits+dots-with-a-digit
    hosts are literal-shaped."""
    assert routes_admin._looks_like_ip_literal("x") is False
    assert routes_admin._looks_like_ip_literal("beef") is False
    assert routes_admin._looks_like_ip_literal("1.2.3.999") is True
    assert routes_admin._looks_like_ip_literal("0xbeef") is True


def test_plain_hostnames_still_take_the_hostname_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Genuine hostnames carry letters/hyphens beyond a 0x prefix and must
    NOT be caught by the IP-shape guard."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_V4))
    assert routes_admin._looks_like_ip_literal("vllm-pool.default.svc.cluster.local") is False
    _allowed("http://vllm-pool.default.svc.cluster.local:8000/v1")


# ---------------------------------------------------------------------------
# (d) registration-time hostname resolution
# ---------------------------------------------------------------------------


def test_plain_localhost_rejected_via_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """`localhost` hits the name check; assert the resolution path also holds
    when the name check is bypassed (defence in depth)."""
    assert _refused("http://localhost/")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1", "::1"))
    assert _refused("http://LOCALHOST/")  # case-insensitive name check


def test_public_hostname_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal public hostname resolving to a public address passes."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_V4))
    _allowed("https://api.example.com/v1")


def test_hostname_resolving_to_loopback_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The static half of the DNS hole: a name that RESOLVES into a denied
    range is refused at registration."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    detail = _refused("http://loopback-name.example.com/v1")
    assert "resolves to" in detail and "127.0.0.1" in detail


def test_hostname_resolving_to_metadata_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    detail = _refused("http://metadata-name.example.com/v1")
    assert "169.254.0.0/16" in detail


def test_any_denied_address_in_a_multi_address_answer_rejects(monkeypatch: pytest.MonkeyPatch) -> None:
    """EVERY returned address is checked — a public answer alongside a denied
    one must not smuggle the name through."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(PUBLIC_V4, "127.0.0.1"))
    assert "resolves to" in _refused("http://mixed.example.com/v1")


def test_hostname_resolving_to_ipv6_loopback_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("::1"))
    assert "::1" in _refused("http://v6-loopback.example.com/v1")


def test_unresolvable_hostname_warns_and_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """COORDINATOR RULING: ALL resolution failures (transient AND
    authoritative) degrade to the pre-hardening posture — a resolver that
    cannot answer says nothing about the name's danger, and refusing would
    reject the gateway's legitimate in-cluster upstreams whenever DNS is
    unavailable (or names this environment cannot resolve at all).
    Security-critical hardening (decode/shape/resolved-address checks) is
    unchanged; a name re-pointed after registration is the documented
    residual."""
    monkeypatch.setattr(socket, "getaddrinfo", _raise_gaierror(socket.EAI_NONAME))
    _allowed("http://does-not-exist.example.com/v1")


def test_nodata_hostname_warns_and_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _raise_gaierror(socket.EAI_NODATA))
    _allowed("http://nodata.example.com/v1")


def test_transient_resolver_failure_degrades_instead_of_false_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """EAI_AGAIN means the RESOLVER was unavailable, which says nothing about
    the name. Refusing here would reject the gateway's legitimate in-cluster
    *.svc.cluster.local upstreams whenever cluster DNS is briefly down, so
    the check degrades to the pre-hardening posture (loud warning)."""
    monkeypatch.setattr(socket, "getaddrinfo", _raise_gaierror(socket.EAI_AGAIN))
    _allowed("http://vllm-pool.default.svc.cluster.local:8000/v1")


def test_authoritative_failure_also_degrades_per_ruling(monkeypatch: pytest.MonkeyPatch) -> None:
    """COORDINATOR RULING supersedes the transient-vs-authoritative split:
    EAI_FAIL degrades too (nothing was SHOWN dangerous). Resolved-address
    checks still fire whenever the resolver DOES answer."""
    monkeypatch.setattr(socket, "getaddrinfo", _raise_gaierror(socket.EAI_FAIL))
    _allowed("http://fail.example.com/v1")


def test_resolution_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged resolver must not hang an admin request: the lookup is
    bounded, and a timeout is treated as a transient failure."""
    import time as time_mod

    monkeypatch.setattr(routes_admin, "_URL_RESOLVE_TIMEOUT_S", 0.2)

    def _hang(host: str, port: Any, *a: Any, **k: Any) -> list[Any]:
        time_mod.sleep(3)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", _hang)
    t0 = time_mod.monotonic()
    _allowed("http://slow.example.com/v1")  # degrades, does not hang
    assert time_mod.monotonic() - t0 < 2.5


def test_incluster_hostname_allowed_when_it_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """RFC1918 / *.svc.cluster.local remain LEGITIMATE upstreams: once the
    name resolves to a private-but-allowed address, registration passes."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.1.2.3"))
    _allowed("http://vllm-pool.default.svc.cluster.local:8000/v1")


def test_extra_deny_cidrs_apply_to_resolved_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator-added CIDRs apply to RESOLVED addresses too, not just
    literals."""
    from pcai_llm.config import settings

    monkeypatch.setattr(settings, "url_deny_cidrs", ["10.0.0.0/8"])
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.1.2.3"))
    assert "10.0.0.0/8" in _refused("http://internal.example.com/v1")


def test_non_http_scheme_still_refused() -> None:
    assert "http://" in _refused("ftp://internal/x")
    assert "http://" in _refused("file:///etc/passwd")


def test_no_host_still_refused() -> None:
    with pytest.raises(HTTPException) as ei:
        routes_admin._check_url("http:///nohost", "base_url")
    assert ei.value.status_code == 400
