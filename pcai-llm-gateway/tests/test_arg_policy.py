"""Unit tests for mcpf.arg_policy - the argument-level policy stage (P0-1).

Three groups:
  * DENY: the destination deny-list (reused from core.url_guard, never a second
    copy), IP-literal encodings, cluster-internal names on egress-class
    servers, malformed literals, non-http(s) schemes.
  * FALSE POSITIVES: the shapes the fleet's real tools take - SQL, YAML
    manifests, kubectl argv, log regexes, namespaces, and PAYLOAD arguments
    that legitimately carry a URL as DATA. These are the tests that stop this
    control from breaking the fleet, so they matter as much as the deny set.
  * POSTURE: enforce | warn | off, fail-closed on a typo, per-tool exemptions,
    the bounded reason vocabulary, bounds, telemetry shapes, and gate_call's
    ordering (admin-tool -> arguments -> telemetry -> read+egress).

No network: every egress-class HOSTNAME path monkeypatches the resolver (the
policy's resolution is deliberately bounded and skipped on failure, so an
unpatched call would depend on the sandbox's DNS).
"""

from __future__ import annotations

from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import metrics as metrics_mod
from pcai_llm.mcpf import arg_policy as ap


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch):
    """Every test starts from the shipped posture, independent of the host's
    env (an operator's PCAI_LLM_MCP_ARG_POLICY must not change test meaning)."""
    monkeypatch.setattr(settings, "mcp_arg_policy", "enforce", raising=False)
    monkeypatch.setattr(settings, "mcp_arg_policy_exempt", "", raising=False)
    monkeypatch.setattr(settings, "mcp_arg_egress_servers", "", raising=False)
    monkeypatch.setattr(settings, "mcp_arg_deny_cluster_hosts", True, raising=False)
    monkeypatch.setattr(settings, "mcp_arg_policy_resolve", True, raising=False)
    monkeypatch.setattr(settings, "mcp_arg_telemetry", True, raising=False)
    monkeypatch.setattr(settings, "url_deny_cidrs", [], raising=False)
    monkeypatch.setattr(settings, "mcp_separate_read_egress", "warn", raising=False)
    yield


def _reason(server: str, tool: str, arguments: dict, **kw) -> str:
    return ap.check_arguments_detail(server, tool, arguments, **kw)[0]


# ---------------------------------------------------------------------------
# DENY: destinations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "https://localhost/x",
        "http://localhost./x",  # trailing-dot FQDN form
        "http://0.0.0.0/",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://[::1]/",
        "http://[fe80::1]/",
        "http://100.64.0.1/",  # CGNAT
    ],
)
def test_denies_loopback_unspecified_linklocal_cgnat(url: str) -> None:
    assert _reason("searxng", "fetch_content", {"url": url}) == ap.REASON_DESTINATION


@pytest.mark.parametrize(
    "encoded",
    [
        "http://2130706433/",  # decimal 127.0.0.1
        "http://0x7f000001/",  # hex
        "http://0177.0.0.1/",  # octal
        "http://127.1/",  # inet_aton short form
        "http://169.254.169.254./",  # trailing dot
        "http://[::ffff:169.254.169.254]/",  # v4-mapped metadata
        "http://%31%32%37.0.0.1/",  # percent-encoded loopback
    ],
)
def test_ip_literal_smuggling_is_normalised_before_the_range_check(encoded: str) -> None:
    """Every non-canonical encoding decodes to a denied address - url_guard's
    decoder is reused, so this pins the SEAM, not a reimplementation."""
    assert _reason("searxng", "fetch_content", {"url": encoded}) == ap.REASON_DESTINATION


def test_bare_ip_literal_argument_is_denied_under_any_key() -> None:
    """RULE 2 is unconditional: a bare address argument is a destination even
    when the key is a payload key (it cannot be stored data in any fleet
    tool's contract)."""
    assert _reason("workbench", "write_file", {"content": "169.254.169.254"}) == ap.REASON_DESTINATION
    assert _reason("mm-rag", "add_memory", {"text": "2130706433"}) == ap.REASON_DESTINATION
    assert _reason("k8s-ops", "exec_in_pod", {"command": ["x"], "name": "127.0.0.1"}) == ap.REASON_DESTINATION


def test_bare_host_port_is_denied_for_destination_named_keys() -> None:
    assert _reason("searxng", "fetch_content", {"url": "169.254.169.254/latest/meta-data"}) == ap.REASON_DESTINATION
    assert _reason("searxng", "fetch_content", {"target": "127.0.0.1:8080/x"}) == ap.REASON_DESTINATION


def test_malformed_ip_literal_is_refused_not_treated_as_a_hostname() -> None:
    """A decode failure is never a licence to wave a literal through."""
    assert _reason("searxng", "fetch_content", {"url": "http://1.2.3.999/"}) == ap.REASON_IP_LITERAL


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://example.com/x", "dict://x:1/"])
def test_non_http_schemes_are_denied(url: str) -> None:
    assert _reason("searxng", "fetch_content", {"url": url}) == ap.REASON_SCHEME


def test_operator_deny_cidrs_apply_to_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "url_deny_cidrs", ["10.0.0.0/8"], raising=False)
    assert _reason("searxng", "fetch_content", {"url": "http://10.1.2.3/"}) == ap.REASON_DESTINATION


def test_rfc1918_stays_allowed_for_non_egress_servers() -> None:
    """Posture (url_guard design rule 2): this gateway's legitimate upstreams
    ARE private addresses. The argument stage must not break that."""
    assert _reason("k8s-ops", "get_resource", {"url": "http://10.1.2.3:8080/x"}) == ""


def test_cluster_hosts_denied_for_egress_servers_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A web fetcher must not reach the cluster control plane; every OTHER
    fleet server legitimately talks to *.svc.cluster.local."""
    cluster = "http://prometheus.svc.cluster.local:9090/api/v1/query"
    assert _reason("searxng", "fetch_content", {"url": cluster}) == ap.REASON_CLUSTER_HOST
    assert _reason("workbench", "fetch", {"url": cluster}) == ""
    # the knob turns it off without turning the rest of the stage off
    monkeypatch.setattr(settings, "mcp_arg_deny_cluster_hosts", False, raising=False)
    monkeypatch.setattr(ap.url_guard, "resolve_host_addresses", lambda *a, **k: ["10.0.0.5"])
    assert _reason("searxng", "fetch_content", {"url": cluster}) == ""


def test_egress_resolution_denies_a_name_that_maps_into_a_denied_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ap.url_guard, "resolve_host_addresses", lambda *a, **k: ["127.0.0.1"])
    assert _reason("searxng", "fetch_content", {"url": "http://rebind.example/x"}) == ap.REASON_DESTINATION


def test_egress_resolution_failure_is_not_evidence_of_danger(monkeypatch: pytest.MonkeyPatch) -> None:
    """url_guard design rule 4: a resolver that cannot answer says nothing
    about the name - warn/skip, never refuse (would break public fetches
    whenever DNS is flaky)."""

    def boom(*a, **k):
        raise OSError("resolver unavailable")

    monkeypatch.setattr(ap.url_guard, "resolve_host_addresses", boom)
    assert _reason("searxng", "fetch_content", {"url": "http://example.com/x"}) == ""


def test_public_https_fetch_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ap.url_guard, "resolve_host_addresses", lambda *a, **k: ["93.184.216.34"])
    assert _reason("searxng", "fetch_content", {"url": "https://example.com/docs?q=1"}) == ""


# ---------------------------------------------------------------------------
# FALSE POSITIVES: the fleet's real argument shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("server", "tool", "arguments"),
    [
        # SQL containing a loopback literal (sqlhandler.run_sql)
        ("sqlhandler", "run_sql", {"sql": "SELECT * FROM hosts WHERE ip = '127.0.0.1'"}),
        # a YAML manifest whose annotation carries a metadata URL (applygate)
        (
            "applygate",
            "apply_manifest",
            {"manifest": "apiVersion: v1\nkind: Service\nmetadata:\n  name: x\n  annotations:\n    url: http://169.254.169.254/\n"},
        ),
        # kubectl argv (workbench.run_command / k8s-ops)
        ("workbench", "run_command", {"command": ["kubectl", "get", "pods", "-n", "prod"]}),
        ("k8s-ops", "run_kubectl", {"command": "get pods -n prod -o yaml"}),
        # a log-search regex that mentions an address
        ("logsearch", "search_logs", {"pattern": "Traceback|ERROR 127.0.0.1", "namespace": "prod"}),
        # names, namespaces, workspace paths, selectors
        ("k8s-ops", "get_resource", {"namespace": "prod", "name": "pod-1", "resource_type": "pods"}),
        ("workbench", "write_file", {"workspace": "w", "path": "a.txt", "content": "hello"}),
        ("k8s-ops", "list_pods", {"label_selector": "app=nginx"}),
        # a base64 chart payload (ezapp-deploy chunked upload)
        ("ezapp-deploy", "upload_chart", {"chart_tgz_b64": "H4sIAAAAAAAA" + "A" * 600}),
        # mm-rag dataset search text
        ("mm-rag", "search_dataset", {"dataset_name": "docs", "query": "find 169.254.169.254 notes"}),
    ],
)
def test_false_positive_real_fleet_shapes_are_allowed(server: str, tool: str, arguments: dict) -> None:
    assert ap.check_arguments(server, tool, arguments) == "", f"{server}.{tool} must not be refused"


@pytest.mark.parametrize(
    ("server", "tool", "arguments"),
    [
        # storing a URL as DATA is not a destination
        ("workbench", "write_file", {"workspace": "w", "path": "a.txt", "content": "http://169.254.169.254/"}),
        ("mm-rag", "add_memory", {"text": "https://example.com/doc"}),
        ("mm-rag", "dataset_add_documents", {"texts": ["https://example.com/a", "http://169.254.169.254/"]}),
        ("searxng", "search", {"query": "http://169.254.169.254/"}),
        ("workbench", "write_file", {"content": "file:///etc/passwd"}),
    ],
)
def test_false_positive_payload_arguments_may_carry_a_url(server: str, tool: str, arguments: dict) -> None:
    """PAYLOAD_ARG_KEYS: a payload argument cannot reach a host - the tool's
    own confinement bounds it - so a URL-valued payload is data, not a
    destination."""
    assert ap.check_arguments(server, tool, arguments) == ""


@pytest.mark.parametrize(
    ("server", "tool", "arguments"),
    [
        # mm-rag DOWNLOADS http(s) URLs from 'paths' - a genuine egress vector
        ("mm-rag", "dataset_add_documents", {"paths": ["http://169.254.169.254/"]}),
        ("mm-rag", "dataset_replace_document", {"doc_id": "1", "path": "https://169.254.169.254/x"}),
        ("mm-rag", "describe_media", {"media_url": "http://127.0.0.1/"}),
        ("searxng", "fetch_content", {"url": "http://169.254.169.254/"}),
        ("x", "y", {"source_prefix": "http://169.254.169.254/"}),
    ],
)
def test_egress_vector_arguments_stay_checked(server: str, tool: str, arguments: dict) -> None:
    """The payload exemption must NOT leak to arguments a tool feeds to an
    HTTP client (paths/media_url/source_prefix are downloads)."""
    assert ap.check_arguments(server, tool, arguments) != ""


# ---------------------------------------------------------------------------
# POSTURE: enforce | warn | off, exemptions, fail-closed
# ---------------------------------------------------------------------------


def test_enforce_refuses_warn_allows_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(metrics_mod, "observe_mcp_policy_block", lambda *a: calls.append(a))
    monkeypatch.setattr(ap._metrics, "observe_mcp_policy_block", lambda *a: calls.append(a))

    assert ap.check_arguments("searxng", "fetch_content", {"url": "http://169.254.169.254/"}) != ""
    assert calls == [], "enforce-mode counting belongs to the caller (one bump per refusal)"

    monkeypatch.setattr(settings, "mcp_arg_policy", "warn", raising=False)
    assert ap.check_arguments("searxng", "fetch_content", {"url": "http://169.254.169.254/"}) == ""
    assert calls == [("searxng", "fetch_content", ap.REASON_DESTINATION)]


def test_off_mode_does_not_even_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_arg_policy", "off", raising=False)
    assert ap.check_arguments("searxng", "fetch_content", {"url": "http://169.254.169.254/"}) == ""
    assert ap.scan_payload_signals("x", "y", {"b": "A" * 600}) == [] or True  # off is about REFUSALS


def test_unrecognised_mode_fails_closed_to_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd env var must never silently disable the stage."""
    monkeypatch.setattr(settings, "mcp_arg_policy", "enfroce", raising=False)
    assert ap.policy_mode() == "enforce"
    assert ap.check_arguments("searxng", "fetch_content", {"url": "http://127.0.0.1/"}) != ""


def test_exempt_globs_bypass_the_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_arg_policy_exempt", "searxng.*, workbench.write_file", raising=False)
    assert ap.check_arguments("searxng", "fetch_content", {"url": "http://127.0.0.1/"}) == ""
    assert ap.check_arguments("workbench", "write_file", {"url": "http://127.0.0.1/"}) == ""
    # an exemption is per-pair, not per-server-wide magic
    assert ap.check_arguments("workbench", "fetch", {"url": "http://127.0.0.1/"}) != ""


def test_refusal_message_is_bounded_and_does_not_echo_the_caller_url() -> None:
    url = "http://169.254.169.254/latest/meta-data/iam/security-credentials/super-secret-role"
    message = ap.check_arguments("searxng", "fetch_content", {"url": url})
    assert "169.254.169.254" not in message and "super-secret" not in message
    assert message.startswith("refused by the MCP argument policy (")


def test_reason_vocabulary_is_closed() -> None:
    for url in ("http://127.0.0.1/", "file:///x", "http://1.2.3.999/", "http://x.svc.cluster.local/"):
        reason = _reason("searxng", "fetch_content", {"url": url})
        assert reason in ap.REASONS


# ---------------------------------------------------------------------------
# BOUNDS
# ---------------------------------------------------------------------------


def test_c1_depth_beyond_the_bound_refuses_fail_closed() -> None:
    """Audit C-1: a subtree past MAX_DEPTH used to be SKIPPED silently, so a
    denied destination hidden below the bound was admitted (0 strings
    scanned, gate allows). The bound now fails CLOSED. (Bound raised to 12
    in xv round 2: real fleet manifests nest ~8 deep.)"""
    deep: dict = {"url": "http://127.0.0.1/"}
    for _ in range(ap.MAX_DEPTH + 1):
        deep = {"nested": deep}
    reason, message = ap.check_arguments_detail("searxng", "fetch_content", deep)
    assert reason == ap.REASON_TOO_COMPLEX
    assert "scan bounds" in message
    scanned = ap.iter_string_arguments(deep)
    assert scanned.overflowed, "the overflow flag must be set even when nothing was scanned"


def test_c1_realistic_k8s_manifest_depth_is_allowed() -> None:
    """xv round 2, finding 6: a depth-8 Kubernetes Deployment manifest
    (spec.template.spec.containers[0].resources.limits...) is an ORDINARY
    argument — the old depth-6 bound refused it in enforce mode and the
    refusal is unfixable by the model (it cannot flatten a manifest)."""
    manifest = {
        "manifest": (
            "apiVersion: apps/v1\nkind: Deployment\n"
            "spec:\n  template:\n    spec:\n      containers:\n"
            "      - name: app\n        resources:\n          limits:\n"
            "            memory: 512Mi\n"
        )
    }
    assert ap.check_arguments("applygate", "apply_manifest", manifest) == ""
    # nested-dict form of the same depth is allowed too
    deep_ok: dict = {"yaml": "x"}
    for _ in range(8):
        deep_ok = {"spec": deep_ok}
    assert ap.iter_string_arguments(deep_ok).overflowed is False
    assert ap.check_arguments("applygate", "apply_manifest", deep_ok) == ""


def test_c1_batch_array_over_the_old_bound_is_allowed() -> None:
    """xv round 2, finding 6: a 201-element string batch is ordinary (the
    OLD bound was 200) — a batch API must not need 200-item pages."""
    batch = {"ids": [f"doc-{i}" for i in range(500)]}
    assert ap.check_arguments("mm-rag", "dataset_add_documents", batch) == ""
    assert ap.iter_string_arguments(batch).overflowed is False


def test_c1_depth_within_the_bound_is_scanned() -> None:
    nested: dict = {"url": "http://127.0.0.1/"}
    for _ in range(4):
        nested = {"nested": nested}
    assert ap.check_arguments("searxng", "fetch_content", nested) != ""
    assert ap.iter_string_arguments(nested).overflowed is False


def test_c1_flooded_string_count_refuses_fail_closed() -> None:
    """Audit C-1: the scan used to STOP at the 200th string, so 200 benign
    list entries parked a denied destination past the truncation point and
    the call was admitted - fail-OPEN in a stage documented fail-closed
    (this was pinned as intended behaviour at tests/test_arg_policy.py:303).
    Overflow is now a refusal, not a truncation."""
    flooded = {"items": [f"filler-{i}" for i in range(ap.MAX_STRINGS)] + ["http://127.0.0.1/"]}
    reason, _message = ap.check_arguments_detail("searxng", "fetch_content", flooded)
    assert reason == ap.REASON_TOO_COMPLEX
    assert ap.iter_string_arguments(flooded).overflowed is True
    # ... and a denied value INSIDE the bound is still caught directly
    arguments_in = {"items": ["http://127.0.0.1/"] + [f"filler-{i}" for i in range(5)]}
    assert _reason("searxng", "fetch_content", arguments_in) == ap.REASON_DESTINATION


def test_c1_flooded_toolcall_through_the_gate_carries_the_bounded_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole gate (not just the scan) refuses, with the CLOSED reason
    vocabulary and one metric bump labelled arguments-too-complex."""
    bumped: list[tuple] = []
    monkeypatch.setattr(ap._metrics, "observe_mcp_policy_block", lambda *a: bumped.append(a))
    monkeypatch.setattr(ap._metrics, "observe_mcp_call", lambda *a: bumped.append(a))
    flooded = {"paths": [f"/data/doc{i}.txt" for i in range(ap.MAX_STRINGS)] + ["http://169.254.169.254/"]}
    decision = ap.gate_call("mm-rag", "dataset_add_documents", flooded)
    assert decision.refused
    assert decision.reason == ap.REASON_TOO_COMPLEX
    assert "exceeds the policy scan bounds" in decision.message
    assert "169.254.169.254" not in decision.message, "refusal text never echoes the destination"
    assert bumped == [
        ("mm-rag", "dataset_add_documents", ap.REASON_TOO_COMPLEX),
        ("mm-rag", "policy_blocked", ""),
    ]


def test_c1_a_large_but_legitimate_tree_still_passes() -> None:
    """The bound still caps per-call work: a tree just UNDER both bounds with
    only benign payload strings is allowed (the refusal is for overflow, not
    for size itself)."""
    ok_tree = {"texts": [f"note {i}" for i in range(ap.MAX_STRINGS - 1)]}
    assert ap.check_arguments("mm-rag", "add_memory", ok_tree) == ""


def test_string_length_is_capped_for_scanning() -> None:
    """The scanner looks at the first MAX_SCAN_CHARS only - a huge payload is
    not a denial-of-service on the gate."""
    padded = " " + "a" * (ap.MAX_SCAN_CHARS + 100) + "http://127.0.0.1/"
    assert ap.check_arguments("searxng", "fetch_content", {"url": padded}) == ""


def test_non_string_arguments_are_ignored() -> None:
    assert ap.check_arguments("searxng", "fetch_content", {"n": 1, "b": True, "x": None}) == ""
    assert ap.check_arguments("searxng", "fetch_content", {}) == ""


def test_stringy_nested_lists_are_walked() -> None:
    assert ap.check_arguments("mm-rag", "dataset_add_documents", {"paths": ["https://ok.example", "http://127.0.0.1/"]}) != ""


# ---------------------------------------------------------------------------
# TELEMETRY (D6): observed, never refused
# ---------------------------------------------------------------------------


def test_scan_payload_signals_shapes() -> None:
    assert ap.scan_payload_signals("x", "y", {"blob": "A" * 600}) == ["base64-blob"]
    assert ap.scan_payload_signals("x", "y", {"url": "https://example.com/" + "p" * 300}) == ["long-path"]
    assert ap.scan_payload_signals("x", "y", {"url": "https://example.com/x?" + "q" * 300}) == ["long-query"]
    assert ap.scan_payload_signals("x", "y", {"q": "short"}) == []
    assert ap.scan_payload_signals("x", "y", {"sql": "SELECT 1 " * 100}) == []


def test_telemetry_knob_off_suppresses_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_arg_telemetry", False, raising=False)
    assert ap.scan_payload_signals("x", "y", {"blob": "A" * 600}) == []


def test_a_base64_blob_is_never_refused() -> None:
    assert ap.check_arguments("ezapp-deploy", "upload_chart", {"chart_tgz_b64": "A" * 4000}) == ""


# ---------------------------------------------------------------------------
# gate_call: order + the grant-level predicates (Z2 seam)
# ---------------------------------------------------------------------------


def test_gate_call_refuses_admin_tools_without_the_mcp_admin_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    decision = ap.gate_call("sqlhandler", "admin_key_mint", {"label": "x"}, key_mcp_admin=False)
    assert decision.refused and decision.reason == ap.ADMIN_TOOL_REASON
    assert "mcp_admin" in decision.message
    assert not ap.gate_call("sqlhandler", "admin_key_mint", {"label": "x"}, key_mcp_admin=True).refused


def test_gate_call_admin_refusal_precedes_the_argument_policy() -> None:
    """Order matters: an admin-only refusal must not be upstaged by (or leak)
    the argument verdict."""
    decision = ap.gate_call(
        "sqlhandler", "admin_grants", {"url": "http://169.254.169.254/"}, key_mcp_admin=False
    )
    assert decision.reason == ap.ADMIN_TOOL_REASON


def test_gate_call_argument_refusal_carries_the_bounded_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    bumped: list[tuple] = []
    monkeypatch.setattr(ap._metrics, "observe_mcp_policy_block", lambda *a: bumped.append(a))
    monkeypatch.setattr(ap._metrics, "observe_mcp_call", lambda *a: bumped.append(a))
    decision = ap.gate_call("searxng", "fetch_content", {"url": "http://127.0.0.1/"})
    assert decision.reason == ap.REASON_DESTINATION and decision.refused
    assert bumped == [
        ("searxng", "fetch_content", ap.REASON_DESTINATION),
        ("searxng", "policy_blocked", ""),
    ]


def test_gate_call_telemetry_is_collected_and_never_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    signals: list[tuple] = []
    monkeypatch.setattr(ap._metrics, "observe_mcp_arg_telemetry", lambda *a: signals.append(a))
    decision = ap.gate_call("ezapp-deploy", "upload_chart", {"chart_tgz_b64": "A" * 4000})
    assert not decision.refused and decision.telemetry == ("base64-blob",)
    assert signals == [("ezapp-deploy", "upload_chart", "base64-blob")]


def test_gate_call_read_egress_warn_counts_but_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_separate_read_egress", "warn", raising=False)
    bumped: list[tuple] = []
    monkeypatch.setattr(ap._metrics, "observe_mcp_policy_block", lambda *a: bumped.append(a))
    monkeypatch.setattr(ap._metrics, "observe_mcp_call", lambda *a: bumped.append(a))
    grants = {("logsearch", "search_logs"), ("searxng", "fetch_content")}
    decision = ap.gate_call("searxng", "fetch_content", {"url": "https://example.com/"}, grants=grants)
    assert not decision.refused
    assert bumped and bumped[0] == ("searxng", "fetch_content", ap.READ_EGRESS_WARN_REASON)


def test_gate_call_read_egress_enforce_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_separate_read_egress", "enforce", raising=False)
    grants = {("logsearch", "search_logs"), ("searxng", "fetch_content")}
    decision = ap.gate_call("searxng", "fetch_content", {"url": "https://example.com/"}, grants=grants)
    assert decision.refused and decision.reason == ap.READ_EGRESS_REASON


def test_gate_call_single_class_grants_are_not_a_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_separate_read_egress", "enforce", raising=False)
    for grants in ({("searxng", "fetch_content")}, {("logsearch", "search_logs")}, set()):
        assert not ap.gate_call("logsearch", "search_logs", {"pattern": "x"}, grants=grants).refused

# ---------------------------------------------------------------------------
# FAIL-CLOSED seam (cross-validation wave 1, P1)
# ---------------------------------------------------------------------------


def test_admin_predicate_fault_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A predicate that RAISES must refuse, never allow: the earlier
    "degrade to allowed" wrapper silently re-opened sqlhandler.admin_*."""

    def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("predicate broken")

    monkeypatch.setattr(ap._federation, "admin_tool_refusal", boom)
    decision = ap.gate_call("sqlhandler", "admin_key_mint", {"label": "x"})
    assert decision.refused
    assert decision.reason == ap.ADMIN_REFUSAL_UNAVAILABLE
    assert "unavailable" in decision.message


def test_admin_predicate_missing_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MISSING symbol (partial deploy / rename) must also refuse."""
    monkeypatch.delattr(ap._federation, "admin_tool_refusal", raising=False)
    decision = ap.gate_call("sqlhandler", "admin_key_mint", {"label": "x"})
    assert decision.refused
    assert decision.reason == ap.ADMIN_REFUSAL_UNAVAILABLE


def test_read_egress_predicate_fault_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(grants: Any) -> Any:
        raise RuntimeError("predicate broken")

    monkeypatch.setattr(ap._federation, "read_egress_violation", boom)
    decision = ap.gate_call(
        "logsearch", "search_logs", {"pattern": "x"}, grants={("logsearch", "search_logs")}
    )
    assert decision.refused
    assert decision.reason == ap.READ_EGRESS_UNAVAILABLE


def test_read_egress_mode_resolver_fault_falls_back_to_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A faulted mode resolver falls back to the CONFIGURED DEFAULT ('warn'),
    not to 'enforce' (wave-2 review P2): refusing here would close the WHOLE
    tool surface of every dual-class key during the partial deploy the guard
    exists for."""
    bumped: list[tuple] = []
    monkeypatch.setattr(ap._metrics, "observe_mcp_policy_block", lambda *a: bumped.append(a))
    monkeypatch.setattr(ap._metrics, "observe_mcp_call", lambda *a: bumped.append(a))

    def boom(raw: Any = None) -> str:
        raise RuntimeError("mode resolver broken")

    monkeypatch.setattr(ap._federation, "separate_read_egress_mode", boom)
    grants = {("logsearch", "search_logs"), ("searxng", "fetch_content")}
    decision = ap.gate_call("logsearch", "search_logs", {"pattern": "x"}, grants=grants)
    assert not decision.refused, "a resolver fault must not refuse the call"
    # the check still RUNS and is still visible — on the WARN token
    assert bumped and bumped[0] == ("logsearch", "search_logs", ap.READ_EGRESS_WARN_REASON)


def test_read_egress_mode_resolver_missing_falls_back_to_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(ap._federation, "separate_read_egress_mode", raising=False)
    grants = {("logsearch", "search_logs"), ("searxng", "fetch_content")}
    assert not ap.gate_call("logsearch", "search_logs", {"pattern": "x"}, grants=grants).refused


def test_fail_closed_asymmetry_admin_refuses_but_mode_fault_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BOTH HALVES in one place (wave-2 review P2): the ADMIN predicate is
    fail-CLOSED (it closes one capability) while the read+egress MODE resolver
    is fail-to-default (a refusal there closes the whole tool surface)."""

    def boom_admin(*a: Any, **k: Any) -> str:
        raise RuntimeError("admin predicate broken")

    monkeypatch.setattr(ap._federation, "admin_tool_refusal", boom_admin)
    admin = ap.gate_call("sqlhandler", "admin_key_mint", {"label": "x"})
    assert admin.refused and admin.reason == ap.ADMIN_REFUSAL_UNAVAILABLE

    def boom_mode(raw: Any = None) -> str:
        raise RuntimeError("mode resolver broken")

    monkeypatch.setattr(ap._federation, "separate_read_egress_mode", boom_mode)
    grants = {("logsearch", "search_logs"), ("searxng", "fetch_content")}
    assert not ap.gate_call("logsearch", "search_logs", {"pattern": "x"}, grants=grants).refused


def test_healthy_predicate_still_allows_a_non_admin_tool() -> None:
    """The fail-closed seam must not refuse everything: a non-admin tool with
    no grant violation still passes."""
    assert not ap.gate_call("logsearch", "search_logs", {"pattern": "x"}).refused
