"""Upstream credential resolution — the ONE place a bearer key is attached.

WHY IT IS ITS OWN MODULE (2026-10-29): two callers need it now. The forward
path has always needed it (routes_proxy._upstream_headers), and context-window
discovery needs it too, because the `/v1/models` card that publishes
`max_model_len` sits behind the same auth. A second copy of the resolution
order would be a second thing to get wrong — and the failure mode is silent
(an unauthenticated probe just 401s and the window stays unknown), which is
exactly the kind of bug that survives review.

Core owns it, api and registry both import it, and nothing here imports either.
"""

from __future__ import annotations

import logging
import os

from ..routing.router import Endpoint
from . import secretbox

log = logging.getLogger(__name__)

_normalisation_warned: set[str] = set()
"""Once-per-process stamps for lossy Secret-name normalisations (see
_env_name). Not quiet-gated ON PURPOSE: it fires at most once per name per
process — config-smell diagnostics, not the per-sweep credential noise the
quiet contract exists for."""


def _env_name(secret_name: str) -> str:
    """The env var a by-ref Secret name resolves to. LOSSY by history (audit
    P3, 2026-11-10): '-' and '_' both fold to '_', so 'a-b' and 'a_b' alias
    one env var and one credential. A hyphenated name warns once per process
    (folding silently would make two endpoints share a credential with no
    trace in the logs)."""
    env_name = "PCAI_LLM_UPSTREAM_KEY__" + secret_name.upper().replace("-", "_")
    if "-" in secret_name and secret_name not in _normalisation_warned:
        _normalisation_warned.add(secret_name)
        log.warning(
            "upstream key secret %r normalises lossily to env %s — an underscore "
            "spelling of the same name would alias it (prefer underscore spellings)",
            secret_name,
            env_name,
        )
    return env_name


def upstream_headers(ep: Endpoint, request_id: str = "", quiet: bool = False) -> dict[str, str]:
    """Build upstream headers: request id (when given) + bearer key if resolvable.

    Credential resolution ORDER (first hit wins):
    1. `auth_token_enc` — Fernet ciphertext from the admin panel, decrypted
       per call with PCAI_LLM_SECRETS_MASTER_KEY (never logged, never cached
       beyond the call).
    2. `api_key_secret` — BY-REFERENCE: a K8s Secret name whose contents the
       chart injects as env PCAI_LLM_UPSTREAM_KEY__<NAME_NORMALIZED>. The
       normalisation is LOSSY (audit P3, 2026-11-10): upper-casing and
       mapping '-' → '_' means 'a-b' and 'a_b' resolve to the SAME env var,
       so two endpoints configured with either spelling share one credential
       silently. A hyphenated name logs a once-per-process warning; prefer
       underscore spellings (a registry-level refusal belongs to the admin
       write path, outside this module).
    If the STORED credential is undecryptable, the by-ref Secret is STILL
    tried and may succeed — the call is unauthenticated only when the
    fall-through produced nothing (audit P3, 2026-11-10: the diagnostics now
    log the ACTUAL outcome, not the assumed one). Neither credential →
    unauthenticated call (the pool may still serve public models; the
    upstream 401 keeps misconfig visible).

    `quiet=True` suppresses those two diagnostics for a BACKGROUND caller that
    repeats on a timer (core.health_probe sweeps every 30 s): the same missing
    credential would otherwise log N lines per sweep, for the life of the
    deployment. The forward path never passes it — a real request must keep
    shouting about an unauthenticated call.
    """
    headers: dict[str, str] = {"X-Request-ID": request_id} if request_id else {}
    if ep.auth_token_enc:
        # quiet threads THROUGH to decrypt (audit P3, 2026-11-10): the
        # sweeper passes quiet=True, and secretbox used to ERROR-log every
        # failed call underneath this gate regardless.
        token = secretbox.decrypt(ep.auth_token_enc, quiet=quiet)
        if token:
            headers["Authorization"] = f"Bearer {token}"
            return headers
        if not quiet:
            # The by-ref Secret below may still succeed — this must not
            # claim 'calling unauthenticated' before the fall-through has
            # spoken (the same audit): each diagnostic logs its actual
            # outcome, never the assumed one.
            log.error(
                "endpoint %s has a stored credential but it is undecryptable "
                "(master key missing/rotated) — falling through to the by-ref Secret",
                ep.id,
            )
    if ep.api_key_secret:
        env_name = _env_name(ep.api_key_secret)
        secret = os.environ.get(env_name)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        elif not quiet:
            log.warning(
                "upstream key secret %r not found as env var %s; forwarding without Authorization header",
                ep.api_key_secret,
                env_name,
            )
    return headers
