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


def upstream_headers(ep: Endpoint, request_id: str = "") -> dict[str, str]:
    """Build upstream headers: request id (when given) + bearer key if resolvable.

    Credential resolution ORDER (first hit wins):
    1. `auth_token_enc` — Fernet ciphertext from the admin panel, decrypted
       per call with PCAI_LLM_SECRETS_MASTER_KEY (never logged, never cached
       beyond the call).
    2. `api_key_secret` — BY-REFERENCE: a K8s Secret name whose contents the
       chart injects as env PCAI_LLM_UPSTREAM_KEY__<NAME_NORMALIZED>.
    Neither → unauthenticated call with a warning (the pool may still serve
    public models; the upstream 401 keeps misconfig visible).
    """
    headers: dict[str, str] = {"X-Request-ID": request_id} if request_id else {}
    if ep.auth_token_enc:
        token = secretbox.decrypt(ep.auth_token_enc)
        if token:
            headers["Authorization"] = f"Bearer {token}"
            return headers
        log.error(
            "endpoint %s has a stored credential but it is undecryptable "
            "(master key missing/rotated) — calling unauthenticated",
            ep.id,
        )
    if ep.api_key_secret:
        env_name = "PCAI_LLM_UPSTREAM_KEY__" + ep.api_key_secret.upper().replace("-", "_")
        secret = os.environ.get(env_name)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        else:
            log.warning(
                "upstream key secret %r not found as env var %s; forwarding without Authorization header",
                ep.api_key_secret,
                env_name,
            )
    return headers
