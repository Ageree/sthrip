"""Tests for Ed25519 channel state signing.

Tests cover: keypair generation, signing, verification (valid, tampered, wrong key).
"""

import base64
import pytest

from sthrip.services.channel_signing import (
    generate_channel_keypair,
    sign_channel_state,
    verify_channel_state,
)


@pytest.mark.unit
def test_generate_keypair():
    pub, priv = generate_channel_keypair()
    assert isinstance(pub, str)
    assert isinstance(priv, str)
    # Both must be valid base64 — raises if not
    base64.b64decode(pub)
    base64.b64decode(priv)


@pytest.mark.unit
def test_generate_keypair_returns_distinct_keys():
    pub, priv = generate_channel_keypair()
    assert pub != priv


@pytest.mark.unit
def test_generate_keypair_different_each_call():
    pub1, _ = generate_channel_keypair()
    pub2, _ = generate_channel_keypair()
    assert pub1 != pub2


@pytest.mark.unit
def test_sign_state():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "channel-123", 1, "4.99", "0.01")
    assert isinstance(sig, str)
    # Must be valid base64
    base64.b64decode(sig)


@pytest.mark.unit
def test_sign_state_returns_non_empty_signature():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "channel-123", 1, "4.99", "0.01")
    assert len(sig) > 0


@pytest.mark.unit
def test_verify_valid_signature():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "channel-123", 1, "4.99", "0.01")
    assert verify_channel_state(pub, sig, "channel-123", 1, "4.99", "0.01") is True


@pytest.mark.unit
def test_verify_invalid_signature_tampered_nonce():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "channel-123", 1, "4.99", "0.01")
    # Tamper: different nonce
    assert verify_channel_state(pub, sig, "channel-123", 2, "4.99", "0.01") is False


@pytest.mark.unit
def test_verify_invalid_signature_tampered_channel_id():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "channel-123", 1, "4.99", "0.01")
    assert verify_channel_state(pub, sig, "channel-999", 1, "4.99", "0.01") is False


@pytest.mark.unit
def test_verify_invalid_signature_tampered_balance_a():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "channel-123", 1, "4.99", "0.01")
    assert verify_channel_state(pub, sig, "channel-123", 1, "9.99", "0.01") is False


@pytest.mark.unit
def test_verify_invalid_signature_tampered_balance_b():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "channel-123", 1, "4.99", "0.01")
    assert verify_channel_state(pub, sig, "channel-123", 1, "4.99", "1.00") is False


@pytest.mark.unit
def test_verify_wrong_key():
    pub1, priv1 = generate_channel_keypair()
    pub2, _priv2 = generate_channel_keypair()
    sig = sign_channel_state(priv1, "ch-1", 1, "5.0", "0.0")
    assert verify_channel_state(pub2, sig, "ch-1", 1, "5.0", "0.0") is False


@pytest.mark.unit
def test_state_message_format():
    """Canonical format channel_id:nonce:balance_a:balance_b round-trips."""
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "my-channel", 42, "1.5", "3.5")
    assert verify_channel_state(pub, sig, "my-channel", 42, "1.5", "3.5") is True


@pytest.mark.unit
def test_verify_returns_bool_true():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "ch", 0, "0.0", "0.0")
    result = verify_channel_state(pub, sig, "ch", 0, "0.0", "0.0")
    assert result is True


@pytest.mark.unit
def test_verify_returns_bool_false():
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "ch", 0, "0.0", "0.0")
    result = verify_channel_state(pub, sig, "ch", 1, "0.0", "0.0")
    assert result is False


@pytest.mark.unit
def test_sign_nonce_zero():
    """Nonce of zero is a valid boundary value."""
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "ch", 0, "10.0", "0.0")
    assert verify_channel_state(pub, sig, "ch", 0, "10.0", "0.0") is True


@pytest.mark.unit
def test_sign_large_nonce():
    """Large nonce boundary value."""
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "ch", 2**31 - 1, "1.0", "1.0")
    assert verify_channel_state(pub, sig, "ch", 2**31 - 1, "1.0", "1.0") is True


@pytest.mark.unit
def test_sign_zero_balances():
    """Both balances zero is a valid state."""
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "ch", 1, "0.0", "0.0")
    assert verify_channel_state(pub, sig, "ch", 1, "0.0", "0.0") is True


@pytest.mark.unit
def test_sign_channel_id_with_special_characters():
    """Channel IDs may contain hyphens and alphanumerics."""
    pub, priv = generate_channel_keypair()
    channel_id = "agent-a_to_agent-b-2026"
    sig = sign_channel_state(priv, channel_id, 5, "2.5", "7.5")
    assert verify_channel_state(pub, sig, channel_id, 5, "2.5", "7.5") is True


@pytest.mark.unit
def test_verify_corrupted_signature():
    """Corrupted (but base64-valid) signature must return False."""
    pub, priv = generate_channel_keypair()
    sig = sign_channel_state(priv, "ch", 1, "1.0", "1.0")
    # Flip the last character of the base64 string
    corrupted = sig[:-4] + "AAAA"
    result = verify_channel_state(pub, corrupted, "ch", 1, "1.0", "1.0")
    assert result is False


@pytest.mark.unit
def test_different_states_produce_different_signatures():
    """Two distinct states must yield different signatures."""
    pub, priv = generate_channel_keypair()
    sig1 = sign_channel_state(priv, "ch", 1, "4.0", "6.0")
    sig2 = sign_channel_state(priv, "ch", 2, "4.0", "6.0")
    assert sig1 != sig2


@pytest.mark.unit
def test_same_state_produces_deterministic_signature():
    """Ed25519 signing is deterministic — same inputs yield same signature."""
    pub, priv = generate_channel_keypair()
    sig1 = sign_channel_state(priv, "ch", 1, "4.0", "6.0")
    sig2 = sign_channel_state(priv, "ch", 1, "4.0", "6.0")
    assert sig1 == sig2
