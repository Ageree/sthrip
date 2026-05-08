"""sthrip-op-keystore: minimal HTTP service holding the operator KEK.

Deployed as a sibling Railway service to the Sthrip API. The hub never
sees ``KEK_OP`` plaintext; it sends wrapped DEKs to this service which
seals/unseals them under the in-process AES-GCM and returns the result.

Endpoints
---------

- ``POST /wrap``   — accepts ``{"dek_b64": <base64 32-byte DEK>}`` and
  returns ``{"wrapped_b64": <base64 nonce||ciphertext||tag>}``.
- ``POST /unwrap`` — accepts ``{"wrapped_b64": <base64 nonce||ct||tag>}``
  and returns ``{"dek_b64": <base64 plaintext DEK>}``.
- ``GET  /health`` — liveness probe, returns ``{"status": "ok"}``.

Auth
----

All non-health endpoints require ``Authorization: Bearer <AUTH_TOKEN>``.
The hub sets the same token as ``OP_KEYSTORE_AUTH_TOKEN``.

Env
---

- ``KEK_OP_BASE64`` — base64 of the 32-byte operator KEK. Required.
- ``AUTH_TOKEN``    — shared bearer secret. Required.
- ``PORT``          — uvicorn port (default 8000). Railway injects this.

Logging deliberately omits any payload bytes. Body lengths and HTTP
status codes are the only thing logged.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sthrip-op-keystore")

_NONCE_LEN = 12
_DEK_LEN = 32


def _load_kek() -> bytes:
    raw = os.environ.get("KEK_OP_BASE64")
    if not raw:
        raise RuntimeError("KEK_OP_BASE64 env not set")
    try:
        kek = base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("KEK_OP_BASE64 is not valid base64") from exc
    if len(kek) != _DEK_LEN:
        raise RuntimeError(
            f"KEK_OP_BASE64 must decode to exactly {_DEK_LEN} bytes "
            f"(got {len(kek)})"
        )
    return kek


def _load_auth_token() -> str:
    token = os.environ.get("AUTH_TOKEN")
    if not token:
        raise RuntimeError("AUTH_TOKEN env not set")
    return token


_KEK = _load_kek()
_AUTH_TOKEN = _load_auth_token()
_AESGCM = AESGCM(_KEK)


app = FastAPI(title="sthrip-op-keystore", version="1.0.0")


class WrapBody(BaseModel):
    dek_b64: str = Field(..., min_length=1)


class WrapResponse(BaseModel):
    wrapped_b64: str


class UnwrapBody(BaseModel):
    wrapped_b64: str = Field(..., min_length=1)


class UnwrapResponse(BaseModel):
    dek_b64: str


def _verify_auth(authorization: Optional[str]) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization header")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="malformed Authorization header")
    token = authorization[len("Bearer "):]
    # Constant-time compare to deny timing oracles.
    import hmac
    if not hmac.compare_digest(token, _AUTH_TOKEN):
        raise HTTPException(status_code=403, detail="invalid token")


@app.post("/wrap", response_model=WrapResponse)
def wrap(body: WrapBody, authorization: Optional[str] = Header(None)) -> WrapResponse:
    _verify_auth(authorization)
    try:
        dek = base64.b64decode(body.dek_b64)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="dek_b64 not valid base64") from exc
    if len(dek) != _DEK_LEN:
        raise HTTPException(status_code=400, detail=f"DEK must be {_DEK_LEN} bytes")
    nonce = os.urandom(_NONCE_LEN)
    ct = _AESGCM.encrypt(nonce, dek, None)
    wrapped = nonce + ct
    logger.info("wrap ok len=%d", len(wrapped))
    return WrapResponse(wrapped_b64=base64.b64encode(wrapped).decode("ascii"))


@app.post("/unwrap", response_model=UnwrapResponse)
def unwrap(body: UnwrapBody, authorization: Optional[str] = Header(None)) -> UnwrapResponse:
    _verify_auth(authorization)
    try:
        wrapped = base64.b64decode(body.wrapped_b64)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="wrapped_b64 not valid base64") from exc
    if len(wrapped) < _NONCE_LEN + 16:
        raise HTTPException(status_code=400, detail="wrapped blob too short")
    nonce, ct = wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:]
    try:
        dek = _AESGCM.decrypt(nonce, ct, None)
    except Exception as exc:  # noqa: BLE001 — auth-tag failure
        # Do not echo the actual error — it is the only signal an attacker
        # would get for tag-failure side channels.
        logger.warning("unwrap auth-tag failure")
        raise HTTPException(status_code=400, detail="unwrap failed") from exc
    if len(dek) != _DEK_LEN:
        raise HTTPException(status_code=500, detail="unexpected DEK length")
    return UnwrapResponse(dek_b64=base64.b64encode(dek).decode("ascii"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
