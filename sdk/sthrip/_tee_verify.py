"""SDK-side TEE attestation verifier (Phase 3 Sprint 7).

Pure helpers used by :class:`sthrip.client.Sthrip` when ``verify_tee=True``.
Kept in a small dedicated module so the (large) ``client.py`` stays
focused on HTTP method bindings.

Verification convention
-----------------------
Mirrors :mod:`gcp.payment_tee_deploy.attestation_service` exactly:

* canonical JSON over ``{"quote_b64", "image_hash_sha256", "timestamp",
  "mode"}`` with sorted keys + compact separators,
* Ed25519 detached signature in ``signature_b64`` over those bytes,
* base64-encoded 32-byte verify key in env ``STHRIP_TEE_ATTESTATION_PUBKEY``.
"""
from __future__ import annotations

import base64
import json
from typing import Mapping, Sequence


_REQUIRED_FIELDS = ("quote_b64", "image_hash_sha256", "timestamp", "signature_b64", "mode")


def _canonical_for_signing(payload):
    # type: (Mapping[str, object]) -> str
    """Return the canonical JSON string used by both signer and verifier."""
    safe = {k: v for k, v in payload.items() if k != "signature_b64"}
    return json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)


def has_required_fields(payload):
    # type: (Mapping[str, object]) -> bool
    if not isinstance(payload, Mapping):
        return False
    return all(field in payload for field in _REQUIRED_FIELDS)


def verify_signature(payload, pubkey_b64):
    # type: (Mapping[str, object], str) -> bool
    """Detached Ed25519 verification over the canonical bytes.

    Returns ``False`` for any structural / cryptographic error — never
    raises (matches the canary verifier convention).
    """
    try:
        from nacl.exceptions import BadSignatureError, CryptoError
        from nacl.signing import VerifyKey
    except ImportError:
        # PyNaCl is a hard SDK dep, but be defensive in case it's missing.
        return False

    sig_b64 = payload.get("signature_b64") if isinstance(payload, Mapping) else None
    if not isinstance(sig_b64, str) or not sig_b64:
        return False

    try:
        message = _canonical_for_signing(payload).encode("utf-8")
        sig = base64.b64decode(sig_b64, validate=True)
        vk = VerifyKey(base64.b64decode(pubkey_b64, validate=True))
        vk.verify(message, sig)
        return True
    except BadSignatureError:
        return False
    except (CryptoError, ValueError, TypeError):
        return False
