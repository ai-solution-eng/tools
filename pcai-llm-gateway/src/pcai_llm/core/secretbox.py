"""Fernet-at-rest credential storage (the AgentBuilder fleet pattern).

Endpoints' API keys are stored ENCRYPTED in Postgres (never plaintext, never
in values files) under a master key supplied via env:

    PCAI_LLM_SECRETS_MASTER_KEY  (from a K8s Secret by reference)

The master key may be ANY non-empty string — it is SHA-256-derived into the
Fernet key, so `openssl rand -hex 32` output works as-is. Rotation invalidates
every stored credential (they must be re-saved) — by design: a rotated master
key must not silently keep decrypting old material.

When the master key is ABSENT, credential storage is DISABLED: writes are
rejected (the admin API answers 503-style guidance) and lookups degrade to
unauthenticated (the upstream 401s visibly — same rule as the by-ref path).
"""

from __future__ import annotations

import base64
import hashlib
import logging

from ..config import settings

log = logging.getLogger(__name__)

_fernet = None
_warned = False


def _get():
    global _fernet, _warned
    if _fernet is None:
        master = settings.secrets_master_key.strip()
        if not master:
            if not _warned:
                log.warning(
                    "PCAI_LLM_SECRETS_MASTER_KEY not set — stored credentials "
                    "disabled (existing by-ref Secret names still work)"
                )
                _warned = True
            return None
        derived = base64.urlsafe_b64encode(hashlib.sha256(master.encode()).digest())
        from cryptography.fernet import Fernet

        _fernet = Fernet(derived)
    return _fernet


def enabled() -> bool:
    """True when credential storage is usable (master key present)."""
    return _get() is not None


def encrypt(plaintext: str) -> str:
    f = _get()
    if f is None:
        raise RuntimeError("credential storage disabled: PCAI_LLM_SECRETS_MASTER_KEY not set")
    return f.encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str | None:
    """Decrypt a stored credential; None on any failure (missing master key,
    wrong key after rotation, corrupt row). Callers degrade to unauthenticated
    — the upstream 401 keeps the failure visible."""
    f = _get()
    if f is None:
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except Exception:
        log.error("stored credential undecryptable (master key rotated?) — re-save it from the admin panel")
        return None
