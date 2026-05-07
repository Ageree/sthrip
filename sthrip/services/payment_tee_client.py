"""HTTP/mTLS client to the GCP TEE payment service.

Phase 3 Sprint 6 — wraps the Sprint 5 TEE service that lives on a GCP
Confidential VM. The Railway proxy (``payment_dispatch``) calls this client
when ``STHRIP_PAYMENT_VIA_TEE=true``.

Design notes
------------

* **HTTP + mTLS, not gRPC** — Lead simplification. mTLS gives us identity
  + encryption without requiring a new wire format. ``httpx`` supports
  client certs via the ``verify``/``cert`` kwargs.

* **Three error shapes** the dispatcher distinguishes:

  - :class:`TEEUnreachableError` — network problem (DNS / TCP / TLS /
    timeout).  Dispatcher falls back to the local handler.
  - :class:`TEEServerError`      — TEE returned 5xx.  Same fall-back path.
  - 4xx response                 — *legitimate* user-facing rejection
    (e.g. insufficient balance).  Dispatcher returns the body unchanged;
    NO fall-back, otherwise we would charge twice on the local side.

* **Idempotency-key passthrough** — the same key the Railway router
  receives is forwarded both in the request body and as an
  ``Idempotency-Key`` header (TEE service reads either).

* **mTLS cert paths** are read from env. In tests we patch the HTTP layer
  so no real certs are required.
"""
from __future__ import annotations

import json as json_lib
import logging
import os
import ssl
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import UUID

import httpx

logger = logging.getLogger("sthrip.tee_client")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TEEUnreachableError(Exception):
    """Network-layer failure reaching the TEE (DNS/TCP/TLS/timeout).

    The dispatcher treats this as a fall-back trigger.
    """


class TEEServerError(Exception):
    """The TEE returned a 5xx response.

    Same fall-back semantics as :class:`TEEUnreachableError` — the request
    never persisted on the TEE side, so the local handler is safe to run.
    """

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"TEE {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class TEEUserError(Exception):
    """The TEE returned a 4xx with a body the client could not tag.

    Phase 3 Sprint 7 carry-over from Sprint 6.  When the TEE responds 4xx
    with a non-dict JSON body (e.g. a bare ``"insufficient balance"``
    string), we cannot tag ``_status_code`` into the body — there is no
    dict to mutate.  Raising this exception lets the dispatcher surface
    the real status as an HTTPException while still distinguishing
    user-error from server-error (the latter would unsafely fall back).
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"TEE {status_code}: {detail[:200]}")
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Env config
# ---------------------------------------------------------------------------


_ENV_ENDPOINT = "STHRIP_TEE_ENDPOINT"
_ENV_CLIENT_CERT = "STHRIP_TEE_CLIENT_CERT_PATH"
_ENV_CLIENT_KEY = "STHRIP_TEE_CLIENT_KEY_PATH"
_ENV_CA = "STHRIP_TEE_CA_PATH"
_ENV_PROXY_TOKEN = "STHRIP_TEE_PROXY_TOKEN"


def _get_endpoint() -> str:
    endpoint = os.getenv(_ENV_ENDPOINT)
    if not endpoint:
        raise TEEUnreachableError(
            f"{_ENV_ENDPOINT} not set; cannot dispatch to TEE"
        )
    return endpoint.rstrip("/")


def _build_mtls_kwargs() -> Dict[str, Any]:
    """Translate env paths to httpx kwargs.

    Tests patch the HTTP layer, so the cert paths only need to exist in
    production. If any env var is missing we still build a request — the
    caller's mock will short-circuit before TLS is exercised.
    """
    kwargs: Dict[str, Any] = {}
    cert_path = os.getenv(_ENV_CLIENT_CERT)
    key_path = os.getenv(_ENV_CLIENT_KEY)
    ca_path = os.getenv(_ENV_CA)

    if cert_path and key_path:
        kwargs["cert"] = (cert_path, key_path)
    if ca_path:
        kwargs["verify"] = ca_path

    return kwargs


# ---------------------------------------------------------------------------
# Public client API
# ---------------------------------------------------------------------------


def _serialise_envelope(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-safe shape for the TEE request body.

    The TEE Pydantic model (``HubPaymentEnvelope``) accepts UUIDs and
    Decimals natively, but we still convert to str/float so tests can
    assert against a plain dict without importing pydantic.
    """
    out: Dict[str, Any] = {}
    for k, v in envelope.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def dispatch_hub_routing(
    envelope: Dict[str, Any],
    idempotency_key: str,
    timeout_s: float = 30.0,
) -> Dict[str, Any]:
    """POST a hub-routing payment request to the TEE service.

    Parameters
    ----------
    envelope:
        Pre-validated request fields. Must contain at minimum
        ``from_agent_id``, ``to_agent_id``, ``to_agent_name``,
        ``to_xmr_address``, ``amount``, ``urgency``.
    idempotency_key:
        Forwarded both in the body and as ``Idempotency-Key`` header.
    timeout_s:
        httpx timeout for the call. Default 30s.

    Returns
    -------
    dict
        On 2xx: parsed JSON body from the TEE.
        On 4xx: the parsed body, with an extra ``"_status_code"`` key so
        callers can branch on legitimate user errors WITHOUT raising.

    Raises
    ------
    TEEUnreachableError
        Network layer failure or env missing.
    TEEServerError
        TEE returned 5xx.
    """
    endpoint = _get_endpoint()
    body = _serialise_envelope(envelope)
    body["idempotency_key"] = idempotency_key

    headers = {
        "Idempotency-Key": idempotency_key,
        "Content-Type": "application/json",
    }
    proxy_token = os.getenv(_ENV_PROXY_TOKEN)
    if proxy_token:
        headers["X-Proxy-Token"] = proxy_token

    mtls_kwargs = _build_mtls_kwargs()

    url = f"{endpoint}/v2/payments/hub-routing"

    try:
        with httpx.Client(timeout=timeout_s, **mtls_kwargs) as client:
            resp = client.post(url, json=body, headers=headers)
    except (
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.NetworkError,
        ssl.SSLError,
        OSError,
    ) as exc:
        logger.warning("TEE unreachable: %s", exc)
        raise TEEUnreachableError(str(exc)) from exc

    if resp.status_code >= 500:
        logger.error("TEE 5xx %s: %s", resp.status_code, resp.text[:200])
        raise TEEServerError(resp.status_code, resp.text)

    try:
        parsed = resp.json()
    except Exception as exc:  # pragma: no cover — defensive
        logger.error("TEE returned non-JSON body: %s", resp.text[:200])
        raise TEEServerError(resp.status_code, resp.text) from exc

    if 400 <= resp.status_code < 500:
        # Legitimate user-error — caller decides what to do.
        if isinstance(parsed, dict):
            # Tag the status so the dispatcher can detect it without
            # re-parsing.
            return {**parsed, "_status_code": resp.status_code}
        # Non-dict 4xx body (string, list, ...) — we cannot embed
        # `_status_code` so we raise a structured TEEUserError instead of
        # silently returning an untagged value that the dispatcher would
        # then misroute as success (Sprint 6 → Sprint 7 carry-over fix).
        detail = parsed if isinstance(parsed, str) else json_lib.dumps(parsed)[:200]
        logger.warning(
            "TEE %s with non-dict body: %s",
            resp.status_code,
            detail[:120],
        )
        raise TEEUserError(resp.status_code, detail)

    return parsed


def health_check(timeout_s: float = 5.0) -> bool:
    """GET /health — returns True on 200, False otherwise.

    Network failures are swallowed and return False (the caller logs +
    increments the unreachable counter).
    """
    try:
        endpoint = _get_endpoint()
    except TEEUnreachableError:
        return False

    mtls_kwargs = _build_mtls_kwargs()
    url = f"{endpoint}/health"
    try:
        with httpx.Client(timeout=timeout_s, **mtls_kwargs) as client:
            resp = client.get(url)
        return resp.status_code == 200
    except Exception as exc:
        logger.debug("TEE /health failed: %s", exc)
        return False
