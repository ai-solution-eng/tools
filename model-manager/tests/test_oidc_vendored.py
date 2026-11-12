"""Guard against silent drift in the vendored fleet module.

app/oidc_identity.py is a verbatim copy of pcai_utils/oidc_identity.py (the
fleet's shared OIDC verifier). It is vendored rather than hardlinked because
this repo has no ``src/<pkg>/utils/`` tree for the mesh linker to target.

TWO guards, because one alone cannot see both failure modes:

* ``test_vendored_copy_matches_the_pcai_utils_source`` catches UPSTREAM drift —
  the source advancing while this copy stays behind. A pinned hash can never
  see that (the hash still matches the stale copy), which is exactly how the
  2026-11-09 back-pull left this file behind until it was re-synced.
* ``test_vendored_oidc_module_is_unmodified`` pins the vendored bytes, so a
  LOCAL edit is caught even where the fleet checkout is absent (CI).

If either fails, do NOT patch it here: re-copy from pcai_utils (truncate-write
onto the existing inode, never temp-rename) and update the hash below. A local
improvement to the verifier belongs upstream, backported to the fleet — not
forked in this repo.
"""

import hashlib
import os
import pathlib

import pytest

# Written in the `sha256:<hex>` shape ON PURPOSE. secret_scan.sh exempts
# sha256-bound hex runs as image digests but has NO allow-marker path for a bare
# 64-hex literal (the `# secret-scan:allow` marker only applies to its
# secret-named-assignment pass), so a bare digest here fails the mirror gate as
# a credential. The prefix is the documented exemption, not obfuscation.
VENDORED_SHA256 = "sha256:5d6c42c4c1324af04933e2c94991ca82dac8c7f64a9a622bd690864b2cb23784"
PCAI_UTILS_ROOT = pathlib.Path(os.environ.get("PCAI_UTILS_ROOT", "/home/andrew/Code/HPE/pcai_utils"))
VENDORED_PATH = pathlib.Path(__file__).resolve().parents[1] / "src" / "model_manager" / "app" / "oidc_identity.py"


def test_vendored_oidc_module_is_unmodified():
    digest = hashlib.sha256(VENDORED_PATH.read_bytes()).hexdigest()
    assert f"sha256:{digest}" == VENDORED_SHA256, (
        "app/oidc_identity.py was edited locally. Re-copy it from pcai_utils and "
        "update VENDORED_SHA256; send improvements upstream."
    )


@pytest.mark.skipif(
    not (PCAI_UTILS_ROOT / "oidc_identity.py").is_file(),
    reason="pcai_utils checkout not present (CI) — hash pin still applies",
)
def test_vendored_copy_matches_the_pcai_utils_source():
    source = (PCAI_UTILS_ROOT / "oidc_identity.py").read_bytes()
    assert VENDORED_PATH.read_bytes() == source, (
        "app/oidc_identity.py is STALE relative to pcai_utils/oidc_identity.py. "
        "Re-copy it (truncate-write onto the same inode) and update VENDORED_SHA256."
    )


def test_the_gate_uses_the_shared_verifier():
    # The admin gate must verify a token, not trust a header. Checked as SOURCE
    # TEXT rather than by importing main: importing it pulls in psycopg2 and
    # only works when another test module has already stubbed it, which made
    # this test pass or fail depending on collection order.
    import re

    path = pathlib.Path(__file__).resolve().parents[1] / "src" / "model_manager" / "app" / "main.py"
    source = path.read_text()
    match = re.search(r"async def _actor_from_sso.*?(?=\nasync def |\ndef )", source, re.DOTALL)
    assert match, "_actor_from_sso not found"
    body = match.group(0)
    assert "oidc_identity.oidc_enabled()" in body
    assert "_verified_subject" in body
