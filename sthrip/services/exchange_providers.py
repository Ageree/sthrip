"""
Exchange provider clients for cross-chain swaps.

Provides an ExchangeProvider protocol with two concrete implementations:
  - ChangeNowProvider  — primary (ChangeNOW REST API v1)
  - SideShiftProvider  — fallback (SideShift.ai REST API)

Environment variables:
  CHANGENOW_API_KEY       — required for ChangeNowProvider
  SIDESHIFT_AFFILIATE_ID  — optional affiliate ID for SideShiftProvider
"""

import logging
import os
from typing import Optional, Protocol, runtime_checkable

import httpx

logger = logging.getLogger("sthrip.exchange_providers")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHANGENOW_BASE = "https://api.changenow.io/v2"
_SIDESHIFT_BASE = "https://sideshift.ai/api/v2"

_HTTP_TIMEOUT_SECONDS: float = 15.0

# Status values normalised to a common vocabulary.
# Providers map their own statuses to these strings.
STATUS_WAITING = "waiting"
STATUS_CONFIRMING = "confirming"
STATUS_EXCHANGING = "exchanging"
STATUS_SENDING = "sending"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

_TERMINAL_STATUSES = frozenset({STATUS_FINISHED, STATUS_FAILED, STATUS_EXPIRED})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ExchangeProviderError(RuntimeError):
    """Raised when an exchange provider returns an error or is unreachable."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ExchangeProvider(Protocol):
    """Duck-typed interface for exchange provider clients."""

    def create_order(
        self,
        from_currency: str,
        from_amount: str,
        to_currency: str,
        to_address: str,
    ) -> dict:
        """Create a swap order with the exchange.

        Returns a dict with keys:
          - external_order_id: str  — provider's order ID
          - deposit_address: str    — address to send source funds
          - expected_amount: str    — expected source amount (may differ from from_amount)
          - provider: str           — "changenow" or "sideshift"

        Raises ExchangeProviderError on failure.
        """
        ...

    def get_order_status(self, external_order_id: str) -> dict:
        """Fetch the current status of an existing order.

        Returns a dict with keys:
          - external_order_id: str
          - status: str  — one of: waiting, confirming, exchanging, sending, finished, failed, expired
          - to_amount: str | None  — amount sent (only when finished)
          - provider: str

        Raises ExchangeProviderError on failure.
        """
        ...


# ---------------------------------------------------------------------------
# ChangeNOW provider
# ---------------------------------------------------------------------------


class ChangeNowProvider:
    """ChangeNOW REST API v1 client.

    API docs: https://documenter.getpostman.com/view/8180765/SVfTPnM3
    """

    _PROVIDER_NAME = "changenow"

    # Map ChangeNOW statuses to our normalised vocabulary.
    _STATUS_MAP = {
        "new": STATUS_WAITING,
        "waiting": STATUS_WAITING,
        "confirming": STATUS_CONFIRMING,
        "exchanging": STATUS_EXCHANGING,
        "sending": STATUS_SENDING,
        "finished": STATUS_FINISHED,
        "failed": STATUS_FAILED,
        "refunded": STATUS_FAILED,
        "expired": STATUS_EXPIRED,
        "verifying": STATUS_CONFIRMING,
        "hold": STATUS_CONFIRMING,
    }

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._api_key = api_key or os.environ.get("CHANGENOW_API_KEY", "")
        if not self._api_key:
            logger.warning(
                "ChangeNowProvider: CHANGENOW_API_KEY not set — provider will fail"
            )

    def create_order(
        self,
        from_currency: str,
        from_amount: str,
        to_currency: str,
        to_address: str,
    ) -> dict:
        """Create a standard-flow exchange order on ChangeNOW v2.

        POST /v2/exchange
        """
        if not self._api_key:
            raise ExchangeProviderError("CHANGENOW_API_KEY is not configured")

        url = f"{_CHANGENOW_BASE}/exchange"
        payload = {
            "fromCurrency": from_currency.lower(),
            "toCurrency": to_currency.lower(),
            "fromAmount": from_amount,
            "address": to_address,
            "flow": "standard",
        }
        headers = {"x-changenow-api-key": self._api_key}

        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise ExchangeProviderError(
                f"ChangeNOW: network error creating order: {exc}"
            ) from exc

        if response.status_code != 200:
            raise ExchangeProviderError(
                f"ChangeNOW: create_order returned HTTP {response.status_code}: {response.text[:200]}"
            )

        data = response.json()
        order_id = data.get("id") or data.get("transactionId")
        deposit_address = data.get("payinAddress") or data.get("payin_address")
        expected_amount = str(data.get("fromAmount") or data.get("amount") or from_amount)

        if not order_id or not deposit_address:
            raise ExchangeProviderError(
                f"ChangeNOW: unexpected response shape: {data}"
            )

        return {
            "external_order_id": order_id,
            "deposit_address": deposit_address,
            "expected_amount": expected_amount,
            "provider": self._PROVIDER_NAME,
        }

    def get_order_status(self, external_order_id: str) -> dict:
        """Fetch current order status from ChangeNOW v2.

        GET /v2/exchange/by-id?id={id}
        """
        if not self._api_key:
            raise ExchangeProviderError("CHANGENOW_API_KEY is not configured")

        url = f"{_CHANGENOW_BASE}/exchange/by-id"
        params = {"id": external_order_id}
        headers = {"x-changenow-api-key": self._api_key}

        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                response = client.get(url, params=params, headers=headers)
        except httpx.RequestError as exc:
            raise ExchangeProviderError(
                f"ChangeNOW: network error fetching status: {exc}"
            ) from exc

        if response.status_code != 200:
            raise ExchangeProviderError(
                f"ChangeNOW: get_order_status returned HTTP {response.status_code}: {response.text[:200]}"
            )

        data = response.json()
        raw_status = data.get("status", "")
        normalised = self._STATUS_MAP.get(raw_status, STATUS_WAITING)
        raw = data.get("amountTo")
        if raw is None:
            raw = data.get("toAmount")
        to_amount = str(raw) if raw is not None else None

        return {
            "external_order_id": external_order_id,
            "status": normalised,
            "to_amount": to_amount,
            "provider": self._PROVIDER_NAME,
        }


# ---------------------------------------------------------------------------
# SideShift provider (fallback)
# ---------------------------------------------------------------------------


class SideShiftProvider:
    """SideShift.ai REST API v2 client.

    API docs: https://sideshift.ai/api/v2
    """

    _PROVIDER_NAME = "sideshift"

    # Map SideShift statuses to our normalised vocabulary.
    _STATUS_MAP = {
        "waiting": STATUS_WAITING,
        "pending": STATUS_WAITING,
        "processing": STATUS_CONFIRMING,
        "review": STATUS_CONFIRMING,
        "settled": STATUS_FINISHED,
        "complete": STATUS_FINISHED,
        "refunding": STATUS_FAILED,
        "refunded": STATUS_FAILED,
        "failed": STATUS_FAILED,
        "expired": STATUS_EXPIRED,
    }

    def __init__(self, affiliate_id: Optional[str] = None) -> None:
        self._affiliate_id = affiliate_id or os.environ.get(
            "SIDESHIFT_AFFILIATE_ID", ""
        )

    def create_order(
        self,
        from_currency: str,
        from_amount: str,
        to_currency: str,
        to_address: str,
    ) -> dict:
        """Create a variable-rate shift order on SideShift.

        POST /v2/shifts/variable
        """
        url = f"{_SIDESHIFT_BASE}/shifts/variable"
        payload = {
            "depositCoin": from_currency.upper(),
            "settleCoin": to_currency.upper(),
            "settleAddress": to_address,
            "depositAmount": from_amount,
        }
        if self._affiliate_id:
            payload["affiliateId"] = self._affiliate_id

        headers: dict = {}
        if self._affiliate_id:
            headers["x-sideshift-secret"] = self._affiliate_id

        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                response = client.post(
                    url,
                    json=payload,
                    headers=headers,
                )
        except httpx.RequestError as exc:
            raise ExchangeProviderError(
                f"SideShift: network error creating order: {exc}"
            ) from exc

        if response.status_code not in (200, 201):
            raise ExchangeProviderError(
                f"SideShift: create_order returned HTTP {response.status_code}: {response.text}"
            )

        data = response.json()
        order_id = data.get("id")
        deposit_address = data.get("depositAddress")
        expected_amount = str(data.get("depositAmount", from_amount))

        if not order_id or not deposit_address:
            raise ExchangeProviderError(
                f"SideShift: unexpected response shape: {data}"
            )

        return {
            "external_order_id": order_id,
            "deposit_address": deposit_address,
            "expected_amount": expected_amount,
            "provider": self._PROVIDER_NAME,
        }

    def get_order_status(self, external_order_id: str) -> dict:
        """Fetch current shift status from SideShift.

        GET /v2/shifts/{id}
        """
        url = f"{_SIDESHIFT_BASE}/shifts/{external_order_id}"

        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
                response = client.get(url)
        except httpx.RequestError as exc:
            raise ExchangeProviderError(
                f"SideShift: network error fetching status: {exc}"
            ) from exc

        if response.status_code != 200:
            raise ExchangeProviderError(
                f"SideShift: get_order_status returned HTTP {response.status_code}: {response.text}"
            )

        data = response.json()
        raw_status = data.get("status", "")
        normalised = self._STATUS_MAP.get(raw_status, STATUS_WAITING)
        raw = data.get("settleAmount")
        if raw is None:
            raw = data.get("settledAmount")
        to_amount = str(raw) if raw is not None else None

        return {
            "external_order_id": external_order_id,
            "status": normalised,
            "to_amount": to_amount,
            "provider": self._PROVIDER_NAME,
        }


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def get_provider_chain() -> list:
    """Return ordered list of providers: [ChangeNow, SideShift].

    ChangeNOW is primary; SideShift is the fallback.
    """
    return [
        ChangeNowProvider(),
        SideShiftProvider(),
    ]


def create_order_with_fallback(
    from_currency: str,
    from_amount: str,
    to_currency: str,
    to_address: str,
) -> dict:
    """Try ChangeNOW first; fall back to SideShift on failure.

    Returns the first successful result.
    Raises ExchangeProviderError if all providers fail.
    """
    providers = get_provider_chain()
    last_error: Optional[ExchangeProviderError] = None

    for provider in providers:
        try:
            result = provider.create_order(from_currency, from_amount, to_currency, to_address)
            logger.info(
                "exchange order created via %s: %s",
                result["provider"],
                result["external_order_id"],
            )
            return result
        except ExchangeProviderError as exc:
            logger.warning(
                "Provider %s failed: %s — trying next",
                getattr(provider, "_PROVIDER_NAME", type(provider).__name__),
                exc,
            )
            last_error = exc

    raise ExchangeProviderError(
        f"All exchange providers failed. Last error: {last_error}"
    )
