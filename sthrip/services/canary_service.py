"""Warrant canary service (Phase 2 Sprint 1).

Daily Ed25519-signed declaration that the operator has not received a
subpoena, gag order, NSL, or compromise demand since the previous canary.
The cron job at 03:05 UTC re-signs the canary with a fresh ``signed_at``;
absence of a fresh canary (>= 48h old) is itself the signal — the
``GET /.well-known/canary.txt`` endpoint returns 503 in that case.

Why dedicated key
-----------------
The canary is signed under ``CANARY_SIGNING_KEY`` rather than reusing
``WEBHOOK_ENCRYPTION_KEY`` or ``AUDIT_HMAC_KEY``. Lead decision (see
``.harness/phase2-money-and-tee/lead-decisions.md``): compromise of any
one key must not cascade into another subsystem.

Why detached signature
----------------------
Detached Ed25519 signature over canonical JSON allows the verifier to
reconstruct the message bytes without trusting the signer's
serialisation. Canonical JSON = sorted keys, compact separators, str
fallback — same convention used elsewhere in the project (see
``sthrip/services/audit_logger.py::_canonical_json``).

Staleness
---------
``get_current_canary()`` returns ``None`` when the latest stored canary
was signed more than 48 hours ago. The endpoint then renders 503 instead
of leaking a stale signature that could be misread as "everything is
fine". 48h gives the cron one day of slack on top of its 24h cadence.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from nacl.exceptions import BadSignatureError, CryptoError
from nacl.signing import SigningKey, VerifyKey
from sqlalchemy.orm import Session

from sthrip.db.models import CanaryState

logger = logging.getLogger("sthrip.canary")

# Single-row pattern. All canary writes upsert into id=1.
_CANARY_ROW_ID = 1

# Staleness window: a canary older than this is considered "missing" and
# the endpoint serves 503. The cron writes daily, so 48h gives one day of
# slack for missed runs (network blip, container restart, etc).
_STALENESS_HOURS = 48

# Fixed message. Operators replace this entire payload when responding to
# legal pressure (the canary "dies"); they never amend the message field
# while continuing to sign.
_CANARY_STATUS = "no_subpoena_no_compromise"
_CANARY_MESSAGE = (
    "As of the date below, Sthrip's operator has not received a subpoena, "
    "national security letter, gag order, or any demand to compromise the "
    "integrity of the platform. Absence of a fresh signed canary should be "
    "interpreted as a signal."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(obj: dict) -> str:
    """Deterministic JSON serialisation (sorted keys, compact, str fallback)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _decode_signing_key(b64: str) -> SigningKey:
    """Decode a base64-encoded 32-byte seed into a PyNaCl SigningKey.

    Caller is responsible for catching ValueError on malformed input.
    """
    if not b64:
        raise ValueError("CANARY_SIGNING_KEY is not configured")
    raw = base64.b64decode(b64, validate=True)
    if len(raw) != 32:
        raise ValueError(
            f"CANARY_SIGNING_KEY must decode to 32 bytes, got {len(raw)}"
        )
    return SigningKey(raw)


def derive_public_key_b64(private_key_b64: str) -> str:
    """Return the base64-encoded Ed25519 public key for the given seed.

    Callers (verifiers, the well-known endpoint) use the public key to
    check signatures without the secret half.
    """
    sk = _decode_signing_key(private_key_b64)
    return base64.b64encode(sk.verify_key.encode()).decode()


def generate_canary_payload(now: Optional[datetime] = None) -> dict:
    """Build the canonical canary payload (unsigned).

    The same field ordering and formatting must be used by signers and
    verifiers — the ``_canonical_json`` helper guarantees that regardless
    of dict construction order.
    """
    when = now if now is not None else _utc_now()
    return {
        "date": when.strftime("%Y-%m-%d"),
        "status": _CANARY_STATUS,
        "message": _CANARY_MESSAGE,
        "last_signed_at": when.isoformat(),
    }


def sign_canary(payload: dict, signing_key_b64: str) -> dict:
    """Attach a base64-encoded Ed25519 detached signature to ``payload``.

    Returns a NEW dict (never mutates the input). Signature is over the
    canonical JSON of the payload.
    """
    sk = _decode_signing_key(signing_key_b64)
    message = _canonical_json(payload).encode("utf-8")
    sig = sk.sign(message).signature
    signed = dict(payload)
    signed["signature_b64"] = base64.b64encode(sig).decode()
    return signed


def verify_canary(
    signed_payload: dict,
    public_key_b64: str,
) -> bool:
    """Verify a signed canary payload against a public key.

    Returns False on any decoding/verification error — never raises.
    Strips ``signature_b64`` before recomputing the canonical JSON.
    """
    if not isinstance(signed_payload, dict):
        return False
    sig_b64 = signed_payload.get("signature_b64")
    if not isinstance(sig_b64, str) or not sig_b64:
        return False
    try:
        unsigned = {k: v for k, v in signed_payload.items() if k != "signature_b64"}
        message = _canonical_json(unsigned).encode("utf-8")
        sig = base64.b64decode(sig_b64, validate=True)
        verify_key = VerifyKey(base64.b64decode(public_key_b64, validate=True))
        verify_key.verify(message, sig)
        return True
    except BadSignatureError:
        logger.debug("canary signature rejected: BadSignatureError")
        return False
    except (CryptoError, ValueError, TypeError) as e:
        logger.debug("canary verify input rejected: %s", type(e).__name__)
        return False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def publish_daily_canary(
    *,
    db: Session,
    signing_key_b64: str,
    now: Optional[datetime] = None,
) -> dict:
    """Cron entry point: generate, sign, persist (upsert id=1).

    Returns the signed payload that was persisted (caller may log a hash
    or expose to operators). Raises ValueError when the signing key is
    not configured — the operator must explicitly opt in by setting
    ``CANARY_SIGNING_KEY``.
    """
    when = now if now is not None else _utc_now()
    payload = generate_canary_payload(when)
    signed = sign_canary(payload, signing_key_b64)

    payload_json = _canonical_json(payload)
    sig_b64 = signed["signature_b64"]

    existing = db.query(CanaryState).filter(CanaryState.id == _CANARY_ROW_ID).one_or_none()
    if existing is None:
        row = CanaryState(
            id=_CANARY_ROW_ID,
            signed_at=when,
            payload_json=payload_json,
            signature_b64=sig_b64,
        )
        db.add(row)
    else:
        existing.signed_at = when
        existing.payload_json = payload_json
        existing.signature_b64 = sig_b64

    db.flush()
    logger.info("canary published at %s", when.isoformat())
    return signed


def get_current_canary(
    *,
    db: Session,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Read the latest stored canary; return None if stale (>48h) or missing.

    The endpoint converts ``None`` → 503 and a non-None payload → 200.
    The check uses naive UTC compare (SQLite stores naive datetimes); we
    normalise both sides to naive UTC so the comparison is robust.
    """
    row = db.query(CanaryState).filter(CanaryState.id == _CANARY_ROW_ID).one_or_none()
    if row is None:
        return None

    base = now if now is not None else _utc_now()
    if base.tzinfo is None:
        base_naive = base
    else:
        base_naive = base.astimezone(timezone.utc).replace(tzinfo=None)

    signed_at = row.signed_at
    if signed_at is None:
        return None
    if signed_at.tzinfo is not None:
        signed_at_naive = signed_at.astimezone(timezone.utc).replace(tzinfo=None)
    else:
        signed_at_naive = signed_at

    age = base_naive - signed_at_naive
    if age > timedelta(hours=_STALENESS_HOURS):
        logger.warning(
            "canary stale: signed_at=%s age=%s",
            signed_at_naive.isoformat(),
            age,
        )
        return None

    try:
        payload = json.loads(row.payload_json)
    except (TypeError, ValueError):
        logger.error("canary payload_json corrupt; treating as stale")
        return None

    payload["signature_b64"] = row.signature_b64
    return payload
