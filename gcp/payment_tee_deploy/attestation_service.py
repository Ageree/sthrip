"""TEE-side remote attestation service (Phase 3 Sprint 7).

Collects an AMD SEV-SNP attestation report (or a deterministic stub when
the SEV-SNP guest device is absent — useful for tests and local
development), signs the canonical payload with a hub-static Ed25519 key,
and serves the result at ``GET /.well-known/attestation.json``.

Why dedicated key
-----------------
Per the canary precedent (Phase 2 Sprint 1), each signing-domain gets
its own key so a compromise of one subsystem does not cascade into
another.  ``STHRIP_TEE_ATTESTATION_KEY`` (Ed25519 base64 seed) is unique
to attestation.

Stub mode
---------
We try to import a SEV-SNP helper library (``python_sev_snp``) lazily.
If it is absent (the typical test/CI case), we generate a deterministic
placeholder report tagged ``mode="snp_stub"``.  Production deploys MUST
verify ``mode == "snp_real"`` before pinning the image hash.

Image hash
----------
``STHRIP_TEE_IMAGE_HASH`` is set at deploy time (the operator captures
the digest from ``docker push`` and pipes it into the VM's metadata).
The attestation echoes that hash so the SDK can compare against the
operator-pinned ``KNOWN_GOOD_HASHES`` list.

Design notes
------------
* The signed payload is the JSON canonicalisation of the four fields
  ``quote_b64``, ``image_hash_sha256``, ``timestamp``, ``mode`` —
  ``signature_b64`` is appended after signing so verifiers MUST strip it
  before recomputing the canonical bytes (same convention as the canary).
* The module never raises on missing env at import time; it raises only
  when the endpoint is actually invoked.  This lets ``payment_service``
  import the module unconditionally.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

from nacl.signing import SigningKey

logger = logging.getLogger("sthrip.tee.attestation")


_ENV_KEY = "STHRIP_TEE_ATTESTATION_KEY"
_ENV_IMAGE_HASH = "STHRIP_TEE_IMAGE_HASH"
_DEFAULT_IMAGE_HASH = "sha256:unknown-image-set-STHRIP_TEE_IMAGE_HASH"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AttestationConfigError(RuntimeError):
    """The signing key or image hash is missing / malformed."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_for_signing(payload: Dict[str, object]) -> str:
    """Canonical JSON used for both signing and verification.

    Same convention as ``sthrip.services.canary_service._canonical_json``:
    sorted keys, compact separators, ``str`` fallback for unknown types.
    The ``signature_b64`` field is intentionally excluded.
    """
    safe = {k: v for k, v in payload.items() if k != "signature_b64"}
    return json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str)


def _decode_signing_key(b64: str) -> SigningKey:
    raw = base64.b64decode(b64, validate=True)
    if len(raw) != 32:
        raise AttestationConfigError(
            f"{_ENV_KEY} must decode to 32 bytes, got {len(raw)}"
        )
    return SigningKey(raw)


# ---------------------------------------------------------------------------
# Quote collection
# ---------------------------------------------------------------------------


def _try_collect_real_snp_quote(image_hash: str) -> Optional[bytes]:
    """Attempt to read a real SEV-SNP attestation quote.

    Returns ``None`` when the SEV-SNP guest device or helper library is
    unavailable.  We deliberately do NOT touch ``/dev/sev-guest`` here —
    in test/CI environments the device is absent, and we don't want
    permission errors to bubble.  A future hardening step adds a
    ``python_sev_snp``-style import path.
    """
    try:
        # Import is intentionally inside the function so tests do not need
        # the dependency installed.  If the library exists at runtime in
        # production, this branch is taken.
        import python_sev_snp  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        # The library API varies by version; keep this defensive.  If the
        # call signature changes, we fall through to stub mode rather than
        # crashing the TEE service at boot.
        report_data = hashlib.sha256(image_hash.encode("utf-8")).digest()
        return python_sev_snp.get_report(report_data=report_data)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("python_sev_snp.get_report failed: %s", exc)
        return None


def _build_stub_quote(image_hash: str) -> bytes:
    """Deterministic placeholder quote keyed off the image hash.

    Tagged ``mode="snp_stub"`` so the SDK side and operator runbooks
    can refuse to pin against a stub-mode attestation.
    """
    digest = hashlib.sha256(b"sthrip-tee-stub|" + image_hash.encode("utf-8")).digest()
    # Mimic the AMD report length of 1184 bytes by repeating; this is
    # only structural — a real verifier would reject it.
    return (digest * 37)[:1184]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_attestation() -> Dict[str, object]:
    """Build, sign, and return the attestation payload.

    Raises
    ------
    AttestationConfigError
        Signing key missing or malformed.
    """
    signing_key_b64 = os.getenv(_ENV_KEY)
    if not signing_key_b64:
        raise AttestationConfigError(
            f"{_ENV_KEY} is not set; cannot sign attestation"
        )

    image_hash = os.getenv(_ENV_IMAGE_HASH, _DEFAULT_IMAGE_HASH)

    real_quote = _try_collect_real_snp_quote(image_hash)
    if real_quote is not None:
        quote_bytes = real_quote
        mode = "snp_real"
    else:
        quote_bytes = _build_stub_quote(image_hash)
        mode = "snp_stub"

    payload: Dict[str, object] = {
        "quote_b64": base64.b64encode(quote_bytes).decode(),
        "image_hash_sha256": image_hash,
        "timestamp": _utc_now_iso(),
        "mode": mode,
    }

    sk = _decode_signing_key(signing_key_b64)
    sig = sk.sign(_canonical_for_signing(payload).encode("utf-8")).signature
    return {**payload, "signature_b64": base64.b64encode(sig).decode()}


def verify_attestation_signature(payload: Dict[str, object], pubkey_b64: str) -> bool:
    """Verify an attestation payload signature with the operator-pinned pubkey.

    Used by the SDK and by operators running an offline check.  Returns
    ``False`` on any structural / cryptographic problem.  Never raises.
    """
    from nacl.exceptions import BadSignatureError, CryptoError
    from nacl.signing import VerifyKey

    if not isinstance(payload, dict):
        return False
    sig_b64 = payload.get("signature_b64")
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
    except (CryptoError, ValueError, TypeError) as exc:
        logger.debug("attestation verify rejected: %s", type(exc).__name__)
        return False
