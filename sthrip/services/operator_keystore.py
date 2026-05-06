"""Operator keystore facade — Sprint 3 stub, Sprint 4b real HTTP client.

Per Lead decision Q1, the operator KEK lives on a separate Railway service
``sthrip-op-keystore`` that never touches ``DATABASE_URL`` and has independent
ACLs. The hub never sees ``KEK_OP`` plaintext; it sends wrapped DEKs to the
keystore service which decrypts under its private key and returns the DEK.

For Sprint 3 (dual-write phase) the real service is not yet deployed, so we
ship a `StubKeystore` that uses an internal AES-GCM with a fixed test KEK.
This keeps the wire-format identical (encrypted blobs in the column) so the
Sprint 4 cutover is a swap-and-go.

Selection is via env var ``OP_KEYSTORE_MODE``:

- ``stub`` (default for Sprint 3, also for tests): in-process AES-GCM stub.
- ``remote`` (Sprint 4b cutover): proxies to ``sthrip-op-keystore``
  via httpx. Requires ``OP_KEYSTORE_AUTH_TOKEN`` env (shared bearer
  secret). URL is ``OP_KEYSTORE_URL`` (default
  ``http://sthrip-op-keystore.railway.internal:8000``). Timeout 5 s.

The stub's KEK is hard-coded as a 32-byte literal — it is **not a secret**.
The threat model assumes any attacker who reaches Sprint 3 dual-write data
also reaches the stub KEK. Sprint 4b replaces this with a network round-trip
to the operator-only service.

Note on ``get_kek_for_envelope``: by design the hub never sees ``KEK_OP``
plaintext in remote mode, so the remote implementation raises a clear
``RuntimeError`` instructing operators to migrate the writer/reader to the
``wrap_dek``/``unwrap_dek`` API once the real keystore is online. The stub
keeps the legacy ``get_kek_for_envelope`` path so existing Sprint 3 dual-write
code continues to work in tests and pre-cutover production.
"""
from __future__ import annotations

import base64
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


#: Default URL for the operator keystore service inside the Railway private
#: network. Override with ``OP_KEYSTORE_URL`` for local testing or alternate
#: deployments.
_DEFAULT_REMOTE_URL = "http://sthrip-op-keystore.railway.internal:8000"

#: HTTP timeout for keystore round-trips. Should be small — the keystore is
#: a colocated AES-GCM service, not a database.
_REMOTE_TIMEOUT_SECONDS = 5.0


class RemoteKeystore:
    """Sprint 4b real keystore client — POSTs wrapped DEKs to the
    sthrip-op-keystore service over the Railway private network.

    Auth: shared bearer secret in the ``Authorization`` header. The same
    token is set on both ends as ``OP_KEYSTORE_AUTH_TOKEN`` (hub) and
    ``AUTH_TOKEN`` (keystore).

    URL: ``OP_KEYSTORE_URL`` (default ``sthrip-op-keystore.railway.internal:8000``).

    The hub never sees ``KEK_OP`` plaintext; ``get_kek_for_envelope`` raises
    a clear ``RuntimeError``. Code paths that still rely on it must be
    rewired to ``wrap_dek``/``unwrap_dek`` before flipping
    ``OP_KEYSTORE_MODE=remote`` in production.
    """

    def __init__(self) -> None:
        # Imported here so the module is importable even in environments
        # that lack httpx (e.g. minimal alembic-only containers).
        import httpx

        self._url = os.environ.get("OP_KEYSTORE_URL", _DEFAULT_REMOTE_URL).rstrip("/")
        token = os.environ.get("OP_KEYSTORE_AUTH_TOKEN")
        if not token:
            raise RuntimeError(
                "OP_KEYSTORE_AUTH_TOKEN env not set; required when "
                "OP_KEYSTORE_MODE=remote. Set the same shared secret on "
                "both the API service and the sthrip-op-keystore service."
            )
        self._token = token
        self._client = httpx.Client(
            timeout=_REMOTE_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}"},
        )

    # -- HTTP helpers --

    def _post(self, path: str, json_body: dict) -> dict:
        url = f"{self._url}{path}"
        try:
            resp = self._client.post(url, json=json_body)
        except Exception as exc:  # noqa: BLE001 — wrap into clear runtime error
            raise RuntimeError(
                f"sthrip-op-keystore unreachable at {url}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code != 200:
            # Truncate body to avoid leaking key material in logs.
            body = resp.text[:200]
            raise RuntimeError(
                f"sthrip-op-keystore {path} returned HTTP {resp.status_code}: {body}"
            )
        return resp.json()

    # -- Public interface --

    def wrap_dek(self, dek: bytes) -> bytes:
        if len(dek) != _DEK_LEN:
            raise ValueError(f"DEK must be exactly {_DEK_LEN} bytes")
        body = {"dek_b64": base64.b64encode(dek).decode("ascii")}
        result = self._post("/wrap", body)
        wrapped_b64 = result.get("wrapped_b64")
        if not isinstance(wrapped_b64, str):
            raise RuntimeError("sthrip-op-keystore /wrap response missing wrapped_b64")
        return base64.b64decode(wrapped_b64)

    def unwrap_dek(self, wrapped: bytes) -> bytes:
        if len(wrapped) < _NONCE_LEN + 16:
            raise ValueError("wrapped DEK blob too short")
        body = {"wrapped_b64": base64.b64encode(wrapped).decode("ascii")}
        result = self._post("/unwrap", body)
        dek_b64 = result.get("dek_b64")
        if not isinstance(dek_b64, str):
            raise RuntimeError("sthrip-op-keystore /unwrap response missing dek_b64")
        dek = base64.b64decode(dek_b64)
        if len(dek) != _DEK_LEN:
            raise RuntimeError(
                f"sthrip-op-keystore returned DEK of wrong length: {len(dek)}"
            )
        return dek

    def get_kek_for_envelope(self) -> bytes:
        raise RuntimeError(
            "RemoteKeystore.get_kek_for_envelope is unsupported by design — "
            "the hub never sees KEK_OP plaintext in remote mode. "
            "Migrate the caller to wrap_dek/unwrap_dek before enabling "
            "OP_KEYSTORE_MODE=remote."
        )


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
