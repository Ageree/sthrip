"""Sthrip SDK client -- thin, synchronous wrapper over the Sthrip REST API.

Usage::

    from sthrip import Sthrip

    s = Sthrip()                       # auto-registers if no key found
    print(s.balance())
    s.pay("other-agent", 0.05, memo="thanks")
"""

import os
import platform
import secrets
import socket

import requests

from .auth import load_credentials, save_credentials
from .exceptions import (
    AgentNotFound,
    AuthError,
    InsufficientBalance,
    NetworkError,
    PaymentError,
    RateLimitError,
    StrhipError,
)

_VERSION = "0.2.0"
_USER_AGENT = "sthrip-sdk/{}".format(_VERSION)
_DEFAULT_API_URL = "https://sthrip-api-production.up.railway.app"
_REQUEST_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_agent_name():
    # type: () -> str
    """Build a human-readable agent name from the hostname + random suffix."""
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        host = "agent"
    # Keep only alphanumeric/underscore/hyphen characters.
    safe = "".join(ch for ch in host if ch.isalnum() or ch in ("_", "-"))
    if not safe:
        safe = "agent"
    suffix = secrets.token_hex(4)
    return "{}-{}".format(safe, suffix)


def _resolve_api_url(explicit):
    # type: (str) -> str
    """Return the API base URL without a trailing slash."""
    url = explicit or os.environ.get("STHRIP_API_URL", "") or _DEFAULT_API_URL
    return url.rstrip("/")


def _map_error(status_code, detail):
    # type: (int, str) -> StrhipError
    """Map an HTTP status + detail string to the most specific exception."""
    lower = detail.lower()

    if status_code == 429:
        return RateLimitError(detail, status_code)

    if status_code in (401, 403):
        return AuthError(detail, status_code)

    if status_code == 404 and "agent" in lower:
        return AgentNotFound(detail, status_code)

    if "insufficient" in lower or "not enough" in lower:
        return InsufficientBalance(detail, status_code)

    if status_code >= 400:
        return PaymentError(detail, status_code)

    return StrhipError(detail, status_code)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Sthrip(object):
    """Synchronous client for the Sthrip anonymous-payments API.

    Parameters
    ----------
    api_key : str, optional
        Bearer token.  Resolution order: *api_key* argument, then
        ``STHRIP_API_KEY`` env var, then ``~/.sthrip/credentials.json``,
        and finally auto-registration.
    api_url : str, optional
        Base URL of the Sthrip API.  Defaults to the env var
        ``STHRIP_API_URL`` or the production endpoint.
    """

    def __init__(self, api_key=None, api_url=None):
        # type: (str, str) -> None
        self._api_url = _resolve_api_url(api_url)
        self._session = self._build_session()
        self._api_key = self._resolve_api_key(api_key)

    # -- key resolution -----------------------------------------------------

    def _resolve_api_key(self, explicit):
        # type: (str) -> str
        """Walk the credential chain and return a usable API key."""
        # 1. Explicit parameter
        if explicit:
            return explicit

        # 2. Environment variable
        env_key = os.environ.get("STHRIP_API_KEY", "")
        if env_key:
            return env_key

        # 3. Credential file
        creds = load_credentials()
        if creds is not None:
            return creds["api_key"]

        # 4. Auto-register
        return self._auto_register()

    def _auto_register(self):
        # type: () -> str
        """Register a new agent and persist the credentials."""
        agent_name = _generate_agent_name()
        payload = {"agent_name": agent_name, "privacy_level": "medium"}

        data = self._raw_post(
            "/v2/agents/register",
            json_body=payload,
            authenticated=False,
        )

        save_credentials(
            api_key=data["api_key"],
            agent_id=data["agent_id"],
            agent_name=data["agent_name"],
            api_url=self._api_url,
        )

        return data["api_key"]

    # -- HTTP layer ---------------------------------------------------------

    def _build_session(self):
        # type: () -> requests.Session
        session = requests.Session()
        session.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        })
        return session

    def _headers(self, authenticated):
        # type: (bool) -> dict
        """Return a *new* headers dict -- never mutates the session."""
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer {}".format(self._api_key)
        return headers

    def _handle_response(self, response):
        # type: (requests.Response) -> dict
        """Raise a typed exception on non-2xx or return the parsed JSON."""
        if response.ok:
            return response.json()

        # Try to pull the structured detail from the API error envelope.
        try:
            body = response.json()
            detail = body.get("detail", response.text)
        except (ValueError, AttributeError):
            detail = response.text

        raise _map_error(response.status_code, str(detail))

    def _raw_request(self, method, path, json_body=None, params=None, authenticated=True):
        # type: (str, str, dict, dict, bool) -> dict
        url = "{}{}".format(self._api_url, path)
        headers = self._headers(authenticated)

        try:
            response = self._session.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.ConnectionError as exc:
            raise NetworkError("Connection failed: {}".format(exc))
        except requests.Timeout as exc:
            raise NetworkError("Request timed out: {}".format(exc))
        except requests.RequestException as exc:
            raise NetworkError("Request error: {}".format(exc))

        return self._handle_response(response)

    def _raw_get(self, path, params=None, authenticated=True):
        # type: (str, dict, bool) -> dict
        return self._raw_request("GET", path, params=params, authenticated=authenticated)

    def _raw_post(self, path, json_body=None, authenticated=True):
        # type: (str, dict, bool) -> dict
        return self._raw_request("POST", path, json_body=json_body, authenticated=authenticated)

    # -- Public API ---------------------------------------------------------

    def deposit_address(self):
        # type: () -> str
        """Return the XMR deposit address for this agent.

        Calls ``POST /v2/balance/deposit`` and returns the address string.
        """
        data = self._raw_post("/v2/balance/deposit", json_body={})
        return data["deposit_address"]

    def pay(self, agent_name, amount, memo=None):
        # type: (str, float, str) -> dict
        """Send a hub-routed payment to *agent_name*.

        Parameters
        ----------
        agent_name : str
            Recipient agent's registered name.
        amount : float
            Amount in XMR.  Converted to string for the API.
        memo : str, optional
            Human-readable note attached to the payment.

        Returns
        -------
        dict
            Full payment receipt from the API.
        """
        payload = {
            "to_agent_name": agent_name,
            "amount": str(amount),
            "urgency": "normal",
        }
        if memo is not None:
            payload["memo"] = memo

        return self._raw_post("/v2/payments/hub-routing", json_body=payload)

    def balance(self):
        # type: () -> dict
        """Return balance information for the authenticated agent.

        Keys include ``available``, ``pending``, ``total_deposited``,
        ``total_withdrawn``, ``deposit_address``, and ``token``.
        """
        return self._raw_get("/v2/balance")

    def find_agents(self, capability=None, **kwargs):
        # type: (str, ...) -> list
        """Discover registered agents.

        Parameters
        ----------
        capability : str, optional
            Currently reserved for future filtering.
        **kwargs
            Additional query params forwarded to ``GET /v2/agents``, e.g.
            ``limit``, ``offset``, ``min_trust_score``, ``tier``,
            ``verified_only``.

        Returns
        -------
        list
            List of agent profile dicts.
        """
        params = {}
        for key, value in kwargs.items():
            if value is not None:
                params[key] = value

        data = self._raw_get("/v2/agents", params=params, authenticated=False)

        # The endpoint may return the list directly or inside an envelope.
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "agents" in data:
            return data["agents"]
        return data

    def me(self):
        # type: () -> dict
        """Return the profile of the currently authenticated agent."""
        return self._raw_get("/v2/me")

    def withdraw(self, amount, address):
        # type: (float, str) -> dict
        """Withdraw XMR to an external Monero address.

        Parameters
        ----------
        amount : float
            Amount in XMR.  Converted to string for the API.
        address : str
            Destination Monero address.

        Returns
        -------
        dict
            Withdrawal receipt including ``tx_hash``, ``amount``, ``fee``.
        """
        payload = {
            "amount": str(amount),
            "address": address,
        }
        return self._raw_post("/v2/balance/withdraw", json_body=payload)

    def payment_history(self, direction=None, limit=50):
        # type: (str, int) -> list
        """Retrieve payment history.

        Parameters
        ----------
        direction : str, optional
            ``"in"`` for received, ``"out"`` for sent, or ``None`` for both.
        limit : int
            Maximum number of records (default 50).

        Returns
        -------
        list
            List of payment record dicts.
        """
        params = {"limit": limit}
        if direction is not None:
            params["direction"] = direction

        data = self._raw_get("/v2/payments/history", params=params)

        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "payments" in data:
            return data["payments"]
        return data

    # -- Escrow API ---------------------------------------------------------

    def escrow_create(self, seller_agent_name, amount, description="", delivery_hours=48, review_hours=24, accept_hours=24):
        """Create a new escrow.

        Parameters
        ----------
        seller_agent_name : str
            The seller agent's registered name.
        amount : float
            Escrow amount in XMR.
        description : str
            Description of work to be performed.
        delivery_hours : int
            Hours seller has to deliver after accepting.
        review_hours : int
            Hours buyer has to review after delivery.
        accept_hours : int
            Hours seller has to accept the escrow.

        Returns
        -------
        dict
            Escrow creation receipt.
        """
        payload = {
            "seller_agent_name": seller_agent_name,
            "amount": str(amount),
            "description": description,
            "accept_timeout_hours": accept_hours,
            "delivery_timeout_hours": delivery_hours,
            "review_timeout_hours": review_hours,
        }
        return self._raw_post("/v2/escrow", json_body=payload)

    def escrow_accept(self, escrow_id):
        """Accept an escrow as seller."""
        return self._raw_post("/v2/escrow/{}/accept".format(escrow_id))

    def escrow_deliver(self, escrow_id):
        """Mark escrow work as delivered (seller)."""
        return self._raw_post("/v2/escrow/{}/deliver".format(escrow_id))

    def escrow_release(self, escrow_id, amount):
        """Release escrow funds to seller (buyer).

        Parameters
        ----------
        escrow_id : str
            The escrow UUID.
        amount : float
            Amount to release to seller (0 = full refund, escrow amount = full release).
        """
        payload = {"release_amount": str(amount)}
        return self._raw_post("/v2/escrow/{}/release".format(escrow_id), json_body=payload)

    def escrow_cancel(self, escrow_id):
        """Cancel escrow before seller accepts (buyer)."""
        return self._raw_post("/v2/escrow/{}/cancel".format(escrow_id))

    def escrow_get(self, escrow_id):
        """Get escrow details."""
        return self._raw_get("/v2/escrow/{}".format(escrow_id))

    def escrow_list(self, role=None, status=None, limit=50, offset=0):
        """List escrows.

        Parameters
        ----------
        role : str, optional
            Filter by role: "buyer", "seller", or None for all.
        status : str, optional
            Filter by status: "created", "accepted", "delivered", "completed", "cancelled", "expired".
        limit : int
            Max results.
        offset : int
            Pagination offset.
        """
        params = {"limit": limit, "offset": offset}
        if role is not None:
            params["role"] = role
        if status is not None:
            params["status"] = status
        return self._raw_get("/v2/escrow", params=params)
