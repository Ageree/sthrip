"""Operator keystore facade — Sprint 3 stub, Sprint 4 real service.

Per Lead decision Q1, the operator KEK lives on a separate Railway service
``sthrip-op-keystore`` that never touches ``DATABASE_URL`` and has independent
ACLs. The hub never sees ``KEK_OP`` plaintext; it sends wrapped DEKs to the
keystore service which decrypts under its private key and returns the DEK.

For Sprint 3 (dual-write phase) the real service is not yet deployed, so we
ship a `StubKeystore` that uses an internal AES-GCM with a fixed test KEK.
This keeps the wire-format identical (encrypted blobs in the column) so the
Sprint 4 cutover is a swap-and-go.

Selection is via env var ``OP_KEYSTORE_MODE``:
- ``stub`` (default for Sprint 3, also for tests): in-process AES-GCM stub
- ``remote`` (Sprint 4): proxies to ``sthrip-op-keystore.railway.internal``;
  raises ``NotImplementedError`` until Sprint 4.

The stub's KEK is hard-coded as a 32-byte literal — it is **not a secret**.
The threat model assumes any attacker who reaches Sprint 3 dual-write data
also reaches the stub KEK. Sprint 4 replaces this with a network round-trip
to the operator-only service.
"""
from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

#: Length of the DEK the keystore wraps/unwraps.
_DEK_LEN = 32
#: Nonce length for AES-GCM.
_NONCE_LEN = 12

#: Stub-mode KEK — exactly 32 bytes. Not a secret; documented as such.
#: Sprint 4 wires this to the real keystore service over the private network.
_STUB_OP_KEK: bytes = b"sthrip-stub-op-kek-32-byte-len!!"

assert len(_STUB_OP_KEK) == _DEK_LEN, "stub OP KEK must be exactly 32 bytes"


class OperatorKeystoreInterface(Protocol):
    """Minimal facade — wrap a fresh DEK, unwrap a stored wrapper."""

    def wrap_dek(self, dek: bytes) -> bytes:  # pragma: no cover - interface
        ...

    def unwrap_dek(self, wrapped: bytes) -> bytes:  # pragma: no cover - interface
        ...

    def get_kek_for_envelope(self) -> bytes:
        """Return the raw OP KEK for direct AES-GCM seal in envelope_crypto.

        Sprint 3 stub returns the hard-coded constant; Sprint 4 remote will
        raise (the remote keystore performs the wrap server-side and returns
        only the wrapped blob — the hub never sees the raw KEK).
        """


class StubKeystore:
    """In-process keystore for Sprint 3.

    AES-GCM with a fixed 32-byte test KEK. Wrap/unwrap round-trips a DEK to
    itself, identity-equivalent. The fixed KEK is **not** a secret.
    """

    _KEK = _STUB_OP_KEK

    def wrap_dek(self, dek: bytes) -> bytes:
        if len(dek) != _DEK_LEN:
            raise ValueError(f"DEK must be exactly {_DEK_LEN} bytes")
        nonce = secrets.token_bytes(_NONCE_LEN)
        ct = AESGCM(self._KEK).encrypt(nonce, dek, None)
        return nonce + ct

    def unwrap_dek(self, wrapped: bytes) -> bytes:
        if len(wrapped) < _NONCE_LEN + 16:
            raise ValueError("wrapped DEK blob too short")
        nonce, ct = wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:]
        dek = AESGCM(self._KEK).decrypt(nonce, ct, None)
        if len(dek) != _DEK_LEN:
            raise ValueError("unwrapped blob is not a valid DEK length")
        return dek

    def get_kek_for_envelope(self) -> bytes:
        return self._KEK


class RemoteKeystore:
    """Sprint 4 placeholder. Constructing it is fine; calling raises."""

    _ERR = (
        "RemoteKeystore is the Sprint 4 cutover. Until "
        "sthrip-op-keystore.railway.internal is deployed and reachable, "
        "set OP_KEYSTORE_MODE=stub."
    )

    def wrap_dek(self, dek: bytes) -> bytes:  # noqa: ARG002
        raise NotImplementedError(self._ERR)

    def unwrap_dek(self, wrapped: bytes) -> bytes:  # noqa: ARG002
        raise NotImplementedError(self._ERR)

    def get_kek_for_envelope(self) -> bytes:
        raise NotImplementedError(self._ERR)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


_VALID_MODES = {"stub", "remote"}


def _resolve_mode() -> str:
    mode = (os.environ.get("OP_KEYSTORE_MODE") or "stub").strip().lower()
    if mode not in _VALID_MODES:
        raise RuntimeError(
            f"OP_KEYSTORE_MODE={mode!r} is invalid; expected one of {sorted(_VALID_MODES)}"
        )
    return mode


@lru_cache(maxsize=1)
def get_keystore() -> OperatorKeystoreInterface:
    """Return the configured keystore. Cached for the process lifetime.

    Tests that toggle ``OP_KEYSTORE_MODE`` must call ``get_keystore.cache_clear()``
    in their fixture teardown.
    """
    mode = _resolve_mode()
    if mode == "stub":
        logger.debug("OperatorKeystore: using stub mode (Sprint 3)")
        return StubKeystore()
    logger.warning("OperatorKeystore: remote mode selected — Sprint 4 placeholder")
    return RemoteKeystore()
