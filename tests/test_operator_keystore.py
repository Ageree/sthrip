"""Unit tests for sthrip.services.operator_keystore (Sprint 3)."""
from __future__ import annotations

import secrets

import pytest

from sthrip.services.operator_keystore import (
    RemoteKeystore,
    StubKeystore,
    get_keystore,
)


def test_keystore_stub_mode_round_trip():
    """Stub keystore wraps a 32-byte DEK and unwraps it back identity."""
    ks = StubKeystore()
    dek = secrets.token_bytes(32)
    wrapped = ks.wrap_dek(dek)
    assert wrapped != dek  # actually encrypted
    out = ks.unwrap_dek(wrapped)
    assert out == dek


def test_keystore_stub_mode_via_get_keystore(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_MODE", "stub")
    get_keystore.cache_clear()
    ks = get_keystore()
    assert isinstance(ks, StubKeystore)


def test_keystore_stub_rejects_wrong_dek_length():
    ks = StubKeystore()
    with pytest.raises(ValueError):
        ks.wrap_dek(b"too-short")


def test_keystore_stub_rejects_corrupt_wrapper():
    ks = StubKeystore()
    with pytest.raises(ValueError):
        ks.unwrap_dek(b"x" * 5)


def test_keystore_stub_kek_for_envelope_is_32_bytes():
    ks = StubKeystore()
    kek = ks.get_kek_for_envelope()
    assert len(kek) == 32


def test_remote_keystore_raises_until_sprint_4():
    rk = RemoteKeystore()
    with pytest.raises(NotImplementedError):
        rk.wrap_dek(secrets.token_bytes(32))
    with pytest.raises(NotImplementedError):
        rk.unwrap_dek(b"x" * 32)
    with pytest.raises(NotImplementedError):
        rk.get_kek_for_envelope()


def test_get_keystore_remote_mode(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_MODE", "remote")
    get_keystore.cache_clear()
    ks = get_keystore()
    assert isinstance(ks, RemoteKeystore)
    # And calling raises.
    with pytest.raises(NotImplementedError):
        ks.unwrap_dek(b"x" * 32)


def test_get_keystore_invalid_mode(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_MODE", "bogus")
    get_keystore.cache_clear()
    with pytest.raises(RuntimeError, match="OP_KEYSTORE_MODE"):
        get_keystore()


def test_get_keystore_default_is_stub(monkeypatch):
    monkeypatch.delenv("OP_KEYSTORE_MODE", raising=False)
    get_keystore.cache_clear()
    ks = get_keystore()
    assert isinstance(ks, StubKeystore)
