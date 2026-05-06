"""Unit tests for sthrip.services.envelope_crypto (Sprint 3)."""
from __future__ import annotations

import os
import secrets
from decimal import Decimal

import pytest
from cryptography.exceptions import InvalidTag

from sthrip.services.envelope_crypto import (
    PaymentEnvelope,
    _decode_key_material,
    amount_to_bucket,
    decrypt_envelope,
    encrypt_envelope,
    load_hub_kek,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kek() -> bytes:
    return secrets.token_bytes(32)


def _payload() -> dict:
    return {
        "from_agent_id": "11111111-1111-1111-1111-111111111111",
        "to_agent_id": "22222222-2222-2222-2222-222222222222",
        "amount": Decimal("123.450000000000"),
        "description": "test deal",
    }


# ---------------------------------------------------------------------------
# Round-trip / both-keys-required
# ---------------------------------------------------------------------------


def test_envelope_roundtrip():
    hub, op = _kek(), _kek()
    payload = _payload()

    env = encrypt_envelope(payload, hub, op)
    out = decrypt_envelope(env, hub, op)

    assert out["from_agent_id"] == payload["from_agent_id"]
    assert out["to_agent_id"] == payload["to_agent_id"]
    # Decimal serialized as str then read back as str — same precision.
    assert out["amount"] == str(payload["amount"])
    assert out["description"] == payload["description"]


def test_envelope_requires_both_keys():
    hub, op = _kek(), _kek()
    env = encrypt_envelope(_payload(), hub, op)

    bad_op = secrets.token_bytes(32)
    with pytest.raises(InvalidTag):
        decrypt_envelope(env, hub, bad_op)

    bad_hub = secrets.token_bytes(32)
    with pytest.raises(InvalidTag):
        decrypt_envelope(env, bad_hub, op)


def test_envelope_key_length_validation():
    short = b"too short"
    with pytest.raises(ValueError):
        encrypt_envelope(_payload(), short, _kek())
    with pytest.raises(ValueError):
        encrypt_envelope(_payload(), _kek(), short)


# ---------------------------------------------------------------------------
# Schema versioning + serialization
# ---------------------------------------------------------------------------


def test_envelope_schema_version():
    hub, op = _kek(), _kek()
    env = encrypt_envelope(_payload(), hub, op)
    assert env.schema_version == 1

    blob = env.to_bytes()
    rt = PaymentEnvelope.from_bytes(blob)

    assert rt == env
    assert rt.schema_version == 1
    # Decryption still works after wire round-trip.
    out = decrypt_envelope(rt, hub, op)
    assert out["amount"] == str(_payload()["amount"])


def test_envelope_blob_corruption_raises():
    with pytest.raises(ValueError):
        PaymentEnvelope.from_bytes(b"")
    with pytest.raises(ValueError):
        PaymentEnvelope.from_bytes(b"not-msgpack")


def test_envelope_unsupported_schema_version():
    hub, op = _kek(), _kek()
    env = encrypt_envelope(_payload(), hub, op)
    bad = env.with_schema_version(99)
    with pytest.raises(ValueError, match="schema_version"):
        decrypt_envelope(bad, hub, op)


def test_envelope_immutability():
    hub, op = _kek(), _kek()
    env = encrypt_envelope(_payload(), hub, op)
    with pytest.raises(Exception):  # frozen dataclass
        env.nonce = b"x" * 12  # type: ignore[misc]
    # with_schema_version returns a new instance, original unchanged
    new = env.with_schema_version(2)
    assert new is not env
    assert env.schema_version == 1


def test_envelope_tampering_detection():
    hub, op = _kek(), _kek()
    env = encrypt_envelope(_payload(), hub, op)
    # Replace one wrapper with a wrap of a *different* DEK under the same hub_kek.
    fake_dek = secrets.token_bytes(32)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    fake_wrap = nonce + AESGCM(hub).encrypt(nonce, fake_dek, None)

    tampered = PaymentEnvelope(
        nonce=env.nonce,
        ciphertext=env.ciphertext,
        dek_wrap_hub=fake_wrap,
        dek_wrap_op=env.dek_wrap_op,
        schema_version=env.schema_version,
    )
    with pytest.raises(ValueError, match="wrappers disagree"):
        decrypt_envelope(tampered, hub, op)


# ---------------------------------------------------------------------------
# Amount bucketing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount,bucket",
    [
        (Decimal("0"), "0-1 XMR"),
        (Decimal("0.5"), "0-1 XMR"),
        (Decimal("1"), "1-10 XMR"),
        (Decimal("9.99"), "1-10 XMR"),
        (Decimal("10"), "10-100 XMR"),
        (Decimal("99.999"), "10-100 XMR"),
        (Decimal("100"), "100-1k XMR"),
        (Decimal("123.45"), "100-1k XMR"),
        (Decimal("999.999"), "100-1k XMR"),
        (Decimal("1000"), "1k-10k XMR"),
        (Decimal("9999"), "1k-10k XMR"),
        (Decimal("10000"), "10k+ XMR"),
        (Decimal("1000000"), "10k+ XMR"),
        (Decimal("-1"), "negative XMR"),
    ],
)
def test_amount_to_bucket_boundaries(amount, bucket):
    assert amount_to_bucket(amount) == bucket


def test_amount_bucket_coarsened():
    """The bucket label MUST NOT contain a digit-by-digit hint of the amount."""
    bucket = amount_to_bucket(Decimal("123.45"))
    assert "123" not in bucket
    assert "123.45" not in bucket
    assert bucket == "100-1k XMR"


def test_amount_bucket_accepts_non_decimal():
    assert amount_to_bucket(50) == "10-100 XMR"  # int
    assert amount_to_bucket(0.5) == "0-1 XMR"  # float


# ---------------------------------------------------------------------------
# Hub KEK loading
# ---------------------------------------------------------------------------


def test_load_hub_kek_hex(monkeypatch):
    # 32-byte hex (64 chars)
    hex_key = "ab" * 32
    monkeypatch.setenv("STHRIP_HUB_KEK", hex_key)
    load_hub_kek.cache_clear()
    kek = load_hub_kek()
    assert kek == bytes.fromhex(hex_key)
    assert len(kek) == 32


def test_load_hub_kek_base64(monkeypatch):
    import base64
    raw = secrets.token_bytes(32)
    b64 = base64.b64encode(raw).decode()
    monkeypatch.setenv("STHRIP_HUB_KEK", b64)
    load_hub_kek.cache_clear()
    kek = load_hub_kek()
    assert kek == raw


def test_load_hub_kek_missing(monkeypatch):
    monkeypatch.delenv("STHRIP_HUB_KEK", raising=False)
    load_hub_kek.cache_clear()
    with pytest.raises(RuntimeError, match="STHRIP_HUB_KEK"):
        load_hub_kek()


def test_load_hub_kek_malformed(monkeypatch):
    monkeypatch.setenv("STHRIP_HUB_KEK", "not valid hex or b64 long enough!")
    load_hub_kek.cache_clear()
    with pytest.raises((ValueError, RuntimeError)):
        load_hub_kek()


def test_decode_key_material_rejects_short():
    with pytest.raises(ValueError):
        _decode_key_material("aabb")
