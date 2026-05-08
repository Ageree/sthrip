"""Envelope encryption for the payment-graph privacy hardening (Sprint 3).

Layer 1 — per-row DEK (Data Encryption Key): random 32 bytes.
Layer 2 — payload (`{from_id, to_id, amount, description}`) encrypted AES-256-GCM
          under that DEK.
Layer 3 — DEK encrypted *twice*: once with the hub's KEK and once with the
          operator's KEK. Reading the payload requires unwrapping with **both**
          keys, so neither the hub nor the operator can recover the graph
          unilaterally.

Sprint 3 ships this module in dual-write mode — every payment-graph row writes
its envelope alongside the existing plaintext FK columns. Sprint 4 cuts reads
over and drops the plaintext.

The serialized envelope format is msgpack with a `schema_version` field for
forward compatibility. Versioning lets Sprint 7 KEK rotation rewrap rows
without changing the layout.

Public API
----------
``PaymentEnvelope``     — frozen dataclass holding the wire-level fields.
``encrypt_envelope``    — payload + (hub_kek, op_kek) → PaymentEnvelope
``decrypt_envelope``    — PaymentEnvelope + (hub_kek, op_kek) → payload
``amount_to_bucket``    — Decimal → coarse log-bucket label
``load_hub_kek``        — read STHRIP_HUB_KEK from env (hex or base64), 32 bytes.

The keys are *secrets* — never log them, never pass them through audit logs.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import lru_cache
from typing import Any, Dict, Optional

import msgpack
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Length of the AES-256-GCM nonce (recommended IV size).
_NONCE_LEN = 12
#: Length of the DEK and any KEK accepted by this module.
_KEK_LEN = 32
#: Current envelope schema version. Bump on layout changes.
_SCHEMA_VERSION = 1

#: Required payload fields. Description is optional but always serialized
#: (None passes through msgpack as nil).
_PAYLOAD_KEYS = ("from_agent_id", "to_agent_id", "amount", "description")


# ---------------------------------------------------------------------------
# Envelope dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentEnvelope:
    """Immutable wire-level representation of an encrypted payment-graph row.

    Wrapped DEK fields are stored as ``nonce || ciphertext`` (AES-GCM auth tag
    appended by the library). Storing them concatenated keeps the layout
    schema-stable across rotations.
    """

    nonce: bytes
    ciphertext: bytes
    dek_wrap_hub: bytes
    dek_wrap_op: bytes
    schema_version: int = _SCHEMA_VERSION

    # ── Serialization ────────────────────────────────────────────────────

    def to_bytes(self) -> bytes:
        """Pack the envelope into a stable bytes blob (msgpack)."""
        packed: Dict[str, Any] = {
            "v": int(self.schema_version),
            "n": bytes(self.nonce),
            "c": bytes(self.ciphertext),
            "wh": bytes(self.dek_wrap_hub),
            "wo": bytes(self.dek_wrap_op),
        }
        return msgpack.packb(packed, use_bin_type=True)

    @classmethod
    def from_bytes(cls, blob: bytes) -> "PaymentEnvelope":
        """Unpack a stored envelope blob. Raises ValueError on corruption."""
        if not blob:
            raise ValueError("empty envelope blob")
        try:
            unpacked = msgpack.unpackb(blob, raw=False)
        except (msgpack.exceptions.ExtraData, msgpack.exceptions.UnpackException, ValueError) as exc:
            raise ValueError(f"corrupt envelope blob: {exc}") from exc
        if not isinstance(unpacked, dict):
            raise ValueError("envelope blob is not a dict")
        try:
            return cls(
                nonce=unpacked["n"],
                ciphertext=unpacked["c"],
                dek_wrap_hub=unpacked["wh"],
                dek_wrap_op=unpacked["wo"],
                schema_version=int(unpacked["v"]),
            )
        except KeyError as exc:
            raise ValueError(f"envelope blob missing field {exc}") from exc

    def with_schema_version(self, version: int) -> "PaymentEnvelope":
        """Return a copy with a different schema_version (immutable)."""
        return replace(self, schema_version=int(version))


# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------


def _decode_key_material(value: str) -> bytes:
    """Decode hex or base64 key material to raw bytes.

    Tolerates surrounding whitespace. Tries hex first (must be exactly
    ``2 * _KEK_LEN`` chars), falls back to base64.
    """
    stripped = value.strip()
    if len(stripped) == _KEK_LEN * 2:
        try:
            decoded = bytes.fromhex(stripped)
            if len(decoded) == _KEK_LEN:
                return decoded
        except ValueError:
            pass
    # Try base64 (urlsafe + standard)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded = decoder(stripped)
            if len(decoded) == _KEK_LEN:
                return decoded
        except (binascii.Error, ValueError):
            continue
    raise ValueError(
        f"hub KEK material must decode to exactly {_KEK_LEN} bytes "
        "(hex or base64)"
    )


@lru_cache(maxsize=1)
def load_hub_kek() -> bytes:
    """Load the hub KEK from env var ``STHRIP_HUB_KEK``.

    Cached for the process lifetime. Tests that override the env var must
    call ``load_hub_kek.cache_clear()`` from their fixtures.

    Raises:
        RuntimeError: if the env var is missing or malformed.
    """
    raw = os.environ.get("STHRIP_HUB_KEK")
    if not raw:
        raise RuntimeError(
            "STHRIP_HUB_KEK is not set. Sthrip refuses to write a payment-graph "
            "envelope without a configured hub KEK. Set the env var to 32 bytes "
            "of hex or base64 key material."
        )
    return _decode_key_material(raw)


# ---------------------------------------------------------------------------
# Payload encoding
# ---------------------------------------------------------------------------


def _normalize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the payload to JSON-stable types.

    UUIDs → str; Decimals → str (full precision preserved); None → None.
    Unknown keys are dropped silently to keep the envelope schema strict.
    """
    out: Dict[str, Any] = {}
    for key in _PAYLOAD_KEYS:
        if key not in payload:
            out[key] = None
            continue
        value = payload[key]
        if value is None:
            out[key] = None
        elif isinstance(value, Decimal):
            out[key] = str(value)
        elif isinstance(value, (bytes, bytearray)):
            out[key] = bytes(value).hex()
        else:
            out[key] = str(value)
    return out


def _encode_payload(payload: Dict[str, Any]) -> bytes:
    return json.dumps(_normalize_payload(payload), separators=(",", ":")).encode("utf-8")


def _decode_payload(blob: bytes) -> Dict[str, Any]:
    decoded = json.loads(blob.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("payload did not decode to a dict")
    return decoded


# ---------------------------------------------------------------------------
# Encrypt / decrypt
# ---------------------------------------------------------------------------


def _aes_seal(key: bytes, plaintext: bytes, *, aad: Optional[bytes] = None) -> bytes:
    """AES-GCM seal that returns ``nonce || ciphertext`` concatenated."""
    if len(key) != _KEK_LEN:
        raise ValueError(f"AES key must be {_KEK_LEN} bytes")
    nonce = secrets.token_bytes(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce + ct


def _aes_open(key: bytes, blob: bytes, *, aad: Optional[bytes] = None) -> bytes:
    """Reverse of ``_aes_seal``. Raises on auth-tag mismatch (cryptography
    surfaces this as ``InvalidTag``)."""
    if len(key) != _KEK_LEN:
        raise ValueError(f"AES key must be {_KEK_LEN} bytes")
    if len(blob) < _NONCE_LEN + 16:
        raise ValueError("ciphertext blob too short to contain nonce + tag")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, aad)


def encrypt_envelope(
    payload: Dict[str, Any],
    hub_kek: bytes,
    op_kek: bytes,
) -> PaymentEnvelope:
    """Encrypt a payment-graph payload into a sealed envelope.

    The DEK is fresh per call (32 random bytes). The payload bytes are sealed
    under the DEK; the DEK itself is then sealed twice — once with the hub
    KEK, once with the operator KEK.
    """
    if len(hub_kek) != _KEK_LEN:
        raise ValueError(f"hub_kek must be {_KEK_LEN} bytes")
    if len(op_kek) != _KEK_LEN:
        raise ValueError(f"op_kek must be {_KEK_LEN} bytes")

    dek = secrets.token_bytes(_KEK_LEN)
    payload_blob = _encode_payload(payload)

    # Seal the payload with the DEK.
    nonce = secrets.token_bytes(_NONCE_LEN)
    ciphertext = AESGCM(dek).encrypt(nonce, payload_blob, None)

    # Seal the DEK twice.
    dek_wrap_hub = _aes_seal(hub_kek, dek)
    dek_wrap_op = _aes_seal(op_kek, dek)

    return PaymentEnvelope(
        nonce=nonce,
        ciphertext=ciphertext,
        dek_wrap_hub=dek_wrap_hub,
        dek_wrap_op=dek_wrap_op,
        schema_version=_SCHEMA_VERSION,
    )


def decrypt_envelope(
    envelope: PaymentEnvelope,
    hub_kek: bytes,
    op_kek: bytes,
) -> Dict[str, Any]:
    """Recover the plaintext payload. Both KEKs are required.

    If either KEK is wrong, ``cryptography.exceptions.InvalidTag`` propagates
    out of the AES-GCM ``decrypt`` call.
    """
    if envelope.schema_version != _SCHEMA_VERSION:
        raise ValueError(
            f"unsupported envelope schema_version={envelope.schema_version}; "
            f"this module supports v{_SCHEMA_VERSION}"
        )
    if len(hub_kek) != _KEK_LEN:
        raise ValueError(f"hub_kek must be {_KEK_LEN} bytes")
    if len(op_kek) != _KEK_LEN:
        raise ValueError(f"op_kek must be {_KEK_LEN} bytes")

    # Unwrap the DEK from one wrapper, then sanity-check it from the other.
    # We use both wrappers because either-key compromise must not be enough.
    dek_via_hub = _aes_open(hub_kek, envelope.dek_wrap_hub)
    dek_via_op = _aes_open(op_kek, envelope.dek_wrap_op)
    if dek_via_hub != dek_via_op:
        # Tamper detection: somebody replaced one wrapper but not the other.
        raise ValueError("envelope DEK wrappers disagree — possible tampering")

    payload_blob = AESGCM(dek_via_hub).decrypt(envelope.nonce, envelope.ciphertext, None)
    return _decode_payload(payload_blob)


# ---------------------------------------------------------------------------
# Amount bucketing
# ---------------------------------------------------------------------------

#: Log-scale buckets for accounting / cron aggregation. Boundaries chosen so
#: typical XMR amounts land cleanly. The bucket label is intentionally coarse
#: — a leak target should not learn the exact amount even if the row's plaintext
#: amount column is dropped (Sprint 4) but the bucket survives.
_BUCKETS = (
    (Decimal("0"), Decimal("1"), "0-1 XMR"),
    (Decimal("1"), Decimal("10"), "1-10 XMR"),
    (Decimal("10"), Decimal("100"), "10-100 XMR"),
    (Decimal("100"), Decimal("1000"), "100-1k XMR"),
    (Decimal("1000"), Decimal("10000"), "1k-10k XMR"),
)
_BUCKET_OVERFLOW = "10k+ XMR"
_BUCKET_NEGATIVE = "negative XMR"


def amount_to_bucket(amount: Decimal) -> str:
    """Map a precise amount to a coarse log-scale bucket label.

    The label MUST NOT contain a digit-by-digit hint of the original amount;
    this is enforced by tests.
    """
    if amount is None:
        return _BUCKET_OVERFLOW  # Defensive: never leak "unknown" as exact.
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    if amount < Decimal("0"):
        return _BUCKET_NEGATIVE
    for lo, hi, label in _BUCKETS:
        if lo <= amount < hi:
            return label
    return _BUCKET_OVERFLOW
