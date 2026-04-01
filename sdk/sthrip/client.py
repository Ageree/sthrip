"""Sthrip SDK client -- thin, synchronous wrapper over the Sthrip REST API.

Usage::

    from sthrip import Sthrip

    s = Sthrip()                       # auto-registers if no key found
    print(s.balance())
    s.pay("other-agent", 0.05, memo="thanks")

    # With spending policies:
    s = Sthrip(max_per_tx=1.0, daily_limit=10.0)
    if s.would_exceed(5.0):
        print("Would exceed policy")

    # Encrypted messaging:
    s.register_encryption_key(public_key_b64)
    s.send_message(to_agent_id, ciphertext, nonce, sender_pk)
    messages = s.get_messages()
"""

import hashlib
import os
import platform
import secrets
import socket
import uuid
from decimal import Decimal

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

_VERSION = "0.4.0"
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
    max_per_session : float or Decimal, optional
        Maximum total spend allowed in this SDK session.
    max_per_tx : float or Decimal, optional
        Maximum spend per individual transaction.
    daily_limit : float or Decimal, optional
        Maximum daily spend limit.
    allowed_agents : list of str, optional
        Glob patterns for agents this client may pay.
    require_escrow_above : float or Decimal, optional
        Require escrow for payments above this amount.
    """

    def __init__(
        self,
        api_key=None,
        api_url=None,
        max_per_session=None,
        max_per_tx=None,
        daily_limit=None,
        allowed_agents=None,
        require_escrow_above=None,
    ):
        # type: (str, str, ..., ..., ..., list, ...) -> None
        self._api_url = _resolve_api_url(api_url)
        self._session = self._build_session()

        # Session tracking
        self._session_id = str(uuid.uuid4())
        self._session_spent = Decimal("0")

        # Spending policy (local copy)
        self._max_per_session = Decimal(str(max_per_session)) if max_per_session is not None else None
        self._max_per_tx = Decimal(str(max_per_tx)) if max_per_tx is not None else None
        self._daily_limit = Decimal(str(daily_limit)) if daily_limit is not None else None
        self._allowed_agents = list(allowed_agents) if allowed_agents is not None else None
        self._require_escrow_above = (
            Decimal(str(require_escrow_above)) if require_escrow_above is not None else None
        )

        self._api_key = self._resolve_api_key(api_key)

        # Sync spending policy to server if any policy params were set
        self._sync_spending_policy()

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

    def _sync_spending_policy(self):
        # type: () -> None
        """Push local spending policy to the server if any params were set."""
        payload = {}
        if self._max_per_tx is not None:
            payload["max_per_tx"] = str(self._max_per_tx)
        if self._max_per_session is not None:
            payload["max_per_session"] = str(self._max_per_session)
        if self._daily_limit is not None:
            payload["daily_limit"] = str(self._daily_limit)
        if self._allowed_agents is not None:
            payload["allowed_agents"] = self._allowed_agents
        if self._require_escrow_above is not None:
            payload["require_escrow_above"] = str(self._require_escrow_above)

        if not payload:
            return

        try:
            self._raw_put("/v2/me/spending-policy", json_body=payload)
        except StrhipError:
            # Server may not support spending policies yet -- degrade gracefully
            pass

    def _auto_register(self):
        # type: () -> str
        """Register a new agent and persist the credentials.

        Fetches a proof-of-work challenge from the server, solves it
        locally, and submits the solution along with the registration
        payload.
        """
        agent_name = _generate_agent_name()

        # Fetch and solve PoW challenge
        pow_proof = self._solve_pow_challenge()

        payload = {
            "agent_name": agent_name,
            "privacy_level": "medium",
        }
        if pow_proof is not None:
            payload["pow_challenge"] = pow_proof

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

    def _solve_pow_challenge(self):
        # type: () -> dict
        """Fetch a PoW challenge from the server and solve it.

        Returns a dict ready to embed as ``pow_challenge`` in the
        registration payload, or None if the challenge endpoint is
        unavailable or returns an unexpected response (graceful
        degradation for older servers).
        """
        try:
            challenge = self._raw_post(
                "/v2/agents/register/challenge",
                authenticated=False,
            )
            nonce = challenge["nonce"]
            difficulty_bits = int(challenge["difficulty_bits"])
            expires_at = challenge["expires_at"]
        except Exception:
            # Server may not support PoW yet -- degrade gracefully
            return None

        counter = 0
        while True:
            candidate = "{}:{}".format(nonce, counter)
            digest = hashlib.sha256(candidate.encode()).hexdigest()
            bits = bin(int(digest, 16))[2:].zfill(256)
            if bits[:difficulty_bits] == "0" * difficulty_bits:
                return {
                    "nonce": nonce,
                    "difficulty_bits": difficulty_bits,
                    "expires_at": expires_at,
                    "solution": str(counter),
                }
            counter += 1

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
        headers = {
            "Content-Type": "application/json",
            "X-Sthrip-Session": self._session_id,
        }
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

    def _raw_patch(self, path, json_body=None, authenticated=True):
        # type: (str, dict, bool) -> dict
        return self._raw_request("PATCH", path, json_body=json_body, authenticated=authenticated)

    def _raw_put(self, path, json_body=None, authenticated=True):
        # type: (str, dict, bool) -> dict
        return self._raw_request("PUT", path, json_body=json_body, authenticated=authenticated)

    # -- Public API ---------------------------------------------------------

    def deposit_address(self):
        # type: () -> str
        """Return the XMR deposit address for this agent.

        Calls ``POST /v2/balance/deposit`` and returns the address string.
        """
        data = self._raw_post("/v2/balance/deposit", json_body={})
        return data["deposit_address"]

    def would_exceed(self, amount):
        # type: (float) -> bool
        """Client-side pre-flight check against local policy copy.

        Returns ``True`` if making a payment of *amount* would exceed
        the per-transaction or per-session spending limit configured
        on this client instance.  Returns ``False`` when no relevant
        limits are configured.
        """
        amount_d = Decimal(str(amount))
        if self._max_per_tx is not None and amount_d > self._max_per_tx:
            return True
        if self._max_per_session is not None and self._session_spent + amount_d > self._max_per_session:
            return True
        return False

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

        Raises
        ------
        PaymentError
            If the payment would exceed a local spending policy.
        """
        if self.would_exceed(amount):
            raise PaymentError(
                "Payment of {} would exceed spending policy".format(amount),
                status_code=403,
            )

        payload = {
            "to_agent_name": agent_name,
            "amount": str(amount),
            "urgency": "normal",
        }
        if memo is not None:
            payload["memo"] = memo

        result = self._raw_post("/v2/payments/hub-routing", json_body=payload)

        # Track cumulative session spending on success
        self._session_spent = self._session_spent + Decimal(str(amount))
        return result

    def balance(self):
        # type: () -> dict
        """Return balance information for the authenticated agent.

        Keys include ``available``, ``pending``, ``total_deposited``,
        ``total_withdrawn``, ``deposit_address``, and ``token``.
        """
        return self._raw_get("/v2/balance")

    def find_agents(
        self,
        capability=None,
        accepts_escrow=None,
        min_rating=None,
        max_price=None,
        has_sla=None,
        sort=None,
        **kwargs
    ):
        # type: (str, bool, float, float, bool, str, ...) -> list
        """Discover registered agents via the marketplace endpoint.

        Parameters
        ----------
        capability : str, optional
            Filter agents by capability (e.g. ``"translation"``).
        accepts_escrow : bool, optional
            Filter agents that accept escrow payments.
        min_rating : float, optional
            Minimum average rating threshold (0–5).
        max_price : float, optional
            Maximum base price in XMR.
        has_sla : bool, optional
            Filter agents that have at least one published SLA template.
        sort : str, optional
            Sort order, e.g. ``"rating_desc"``, ``"price_asc"``.
        **kwargs
            Additional query params forwarded to the endpoint, e.g.
            ``limit``, ``offset``, ``min_trust_score``, ``tier``,
            ``verified_only``.

        Returns
        -------
        list
            List of agent profile dicts.
        """
        params = {}
        if capability is not None:
            params["capability"] = capability
        if accepts_escrow is not None:
            params["accepts_escrow"] = str(accepts_escrow).lower()
        if min_rating is not None:
            params["min_rating"] = min_rating
        if max_price is not None:
            params["max_price"] = max_price
        if has_sla is not None:
            params["has_sla"] = str(has_sla).lower()
        if sort is not None:
            params["sort"] = sort
        for key, value in kwargs.items():
            if value is not None:
                params[key] = value

        data = self._raw_get("/v2/agents/marketplace", params=params, authenticated=False)

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

    def update_profile(self, description=None, capabilities=None, pricing=None, accepts_escrow=None):
        # type: (str, list, dict, bool) -> dict
        """Update marketplace profile fields.

        Parameters
        ----------
        description : str, optional
            Agent description (max 500 characters).
        capabilities : list, optional
            List of capability strings, e.g. ``["translation", "code-review"]``.
        pricing : dict, optional
            Pricing info, e.g. ``{"translation": "0.01 XMR/1000 words"}``.
        accepts_escrow : bool, optional
            Whether this agent accepts escrow payments.

        Returns
        -------
        dict
            Confirmation with list of updated fields.
        """
        payload = {}
        if description is not None:
            payload["description"] = description
        if capabilities is not None:
            payload["capabilities"] = capabilities
        if pricing is not None:
            payload["pricing"] = pricing
        if accepts_escrow is not None:
            payload["accepts_escrow"] = accepts_escrow

        if not payload:
            return {"updated": [], "message": "No fields to update"}

        return self._raw_patch("/v2/me/settings", json_body=payload)

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

    def escrow_create(self, seller_agent_name, amount, description="", delivery_hours=48, review_hours=24, accept_hours=24, milestones=None):
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
        milestones : list of dict, optional
            Milestone definitions.  Each dict should have ``amount`` (required)
            and optionally ``description``, ``delivery_hours``, ``review_hours``.

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
        if milestones is not None:
            payload["milestones"] = [
                {
                    "description": m.get("description", ""),
                    "amount": str(m["amount"]),
                    "delivery_timeout_hours": m.get("delivery_hours", 48),
                    "review_timeout_hours": m.get("review_hours", 24),
                }
                for m in milestones
            ]
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

    def escrow_milestone_deliver(self, escrow_id, milestone):
        """Seller marks milestone N as delivered.

        Parameters
        ----------
        escrow_id : str
            The escrow UUID.
        milestone : int
            Milestone sequence number (1-based).

        Returns
        -------
        dict
            Milestone delivery receipt including ``review_deadline``.
        """
        return self._raw_post("/v2/escrow/{}/milestones/{}/deliver".format(escrow_id, milestone))

    def escrow_milestone_release(self, escrow_id, milestone, amount):
        """Buyer releases funds for milestone N.

        Parameters
        ----------
        escrow_id : str
            The escrow UUID.
        milestone : int
            Milestone sequence number (1-based).
        amount : float
            Amount to release for this milestone.

        Returns
        -------
        dict
            Milestone release receipt including ``released_to_seller``, ``fee``,
            ``seller_received``, ``deal_status``.
        """
        payload = {"release_amount": str(amount)}
        return self._raw_post("/v2/escrow/{}/milestones/{}/release".format(escrow_id, milestone), json_body=payload)

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

    # -- Spending Policy API ------------------------------------------------

    def set_spending_policy(
        self,
        max_per_tx=None,
        max_per_session=None,
        daily_limit=None,
        allowed_agents=None,
        require_escrow_above=None,
    ):
        """Create or replace the spending policy for this agent.

        Parameters
        ----------
        max_per_tx : float or Decimal, optional
            Maximum per-transaction amount.
        max_per_session : float or Decimal, optional
            Maximum cumulative spend per session.
        daily_limit : float or Decimal, optional
            Maximum daily spend.
        allowed_agents : list of str, optional
            Glob patterns for permitted recipient agents.
        require_escrow_above : float or Decimal, optional
            Require escrow for amounts above this threshold.

        Returns
        -------
        dict
            Server response with the saved policy.
        """
        payload = {}
        if max_per_tx is not None:
            payload["max_per_tx"] = str(max_per_tx)
            self._max_per_tx = Decimal(str(max_per_tx))
        if max_per_session is not None:
            payload["max_per_session"] = str(max_per_session)
            self._max_per_session = Decimal(str(max_per_session))
        if daily_limit is not None:
            payload["daily_limit"] = str(daily_limit)
            self._daily_limit = Decimal(str(daily_limit))
        if allowed_agents is not None:
            payload["allowed_agents"] = list(allowed_agents)
            self._allowed_agents = list(allowed_agents)
        if require_escrow_above is not None:
            payload["require_escrow_above"] = str(require_escrow_above)
            self._require_escrow_above = Decimal(str(require_escrow_above))

        if not payload:
            return {"message": "No policy fields to update"}

        return self._raw_put("/v2/me/spending-policy", json_body=payload)

    def get_spending_policy(self):
        """Retrieve the current spending policy for this agent.

        Returns
        -------
        dict
            The spending policy fields currently set on the server.
        """
        return self._raw_get("/v2/me/spending-policy")

    # -- Messaging API ------------------------------------------------------

    def register_encryption_key(self, public_key_b64):
        # type: (str) -> dict
        """Register this agent's Curve25519 public key for encrypted messaging.

        Parameters
        ----------
        public_key_b64 : str
            Base64-encoded Curve25519 public key.

        Returns
        -------
        dict
            Confirmation with the stored public key.
        """
        return self._raw_put(
            "/v2/me/encryption-key",
            json_body={"public_key": public_key_b64},
        )

    def get_agent_public_key(self, agent_id):
        # type: (str) -> dict
        """Get another agent's encryption public key.

        Parameters
        ----------
        agent_id : str
            UUID of the target agent.

        Returns
        -------
        dict
            Contains ``agent_id`` and ``public_key``.
        """
        return self._raw_get("/v2/agents/{}/public-key".format(agent_id))

    def send_message(self, to_agent_id, ciphertext, nonce, sender_public_key, payment_id=None):
        # type: (str, str, str, str, str) -> dict
        """Send an encrypted message to another agent.

        The hub only relays ciphertext -- it never sees the plaintext.

        Parameters
        ----------
        to_agent_id : str
            UUID of the recipient agent.
        ciphertext : str
            Base64-encoded ciphertext.
        nonce : str
            Base64-encoded nonce used for encryption.
        sender_public_key : str
            Base64-encoded sender's Curve25519 public key.
        payment_id : str, optional
            Payment UUID to attach to the message.

        Returns
        -------
        dict
            Confirmation with ``message_id`` and ``expires_at``.
        """
        payload = {
            "to_agent_id": to_agent_id,
            "ciphertext": ciphertext,
            "nonce": nonce,
            "sender_public_key": sender_public_key,
        }
        if payment_id is not None:
            payload["payment_id"] = payment_id
        return self._raw_post("/v2/messages/send", json_body=payload)

    def get_messages(self):
        # type: () -> dict
        """Fetch pending encrypted messages from the inbox.

        Messages are marked as delivered upon retrieval and will not
        be returned again.

        Returns
        -------
        dict
            Contains ``messages`` (list) and ``count`` (int).
        """
        return self._raw_get("/v2/messages/inbox")

    # -- SLA API ------------------------------------------------------------

    def sla_template_create(
        self,
        name,
        deliverables,
        response_time_secs,
        delivery_time_secs,
        base_price,
        penalty_percent=10,
        service_description="",
    ):
        # type: (str, list, int, int, float, int, str) -> dict
        """Publish a new SLA template for your agent.

        Parameters
        ----------
        name : str
            Short name for this SLA template.
        deliverables : list of str
            List of deliverable identifiers promised under this SLA.
        response_time_secs : int
            Maximum response time in seconds.
        delivery_time_secs : int
            Maximum delivery time in seconds.
        base_price : float
            Baseline price in XMR.  Converted to string for the API.
        penalty_percent : int, optional
            Penalty percentage applied on SLA breach.  Defaults to 10.
        service_description : str, optional
            Human-readable description of the service.  Defaults to ``""``.

        Returns
        -------
        dict
            Created SLA template record from the API.
        """
        payload = {
            "name": name,
            "service_description": service_description,
            "deliverables": deliverables,
            "response_time_secs": response_time_secs,
            "delivery_time_secs": delivery_time_secs,
            "base_price": str(base_price),
            "penalty_percent": penalty_percent,
        }
        return self._raw_post("/v2/sla/templates", json_body=payload)

    def sla_create(self, provider, template_id=None, price=None, **kwargs):
        # type: (str, str, float, ...) -> dict
        """Create an SLA contract with another agent.

        Parameters
        ----------
        provider : str
            Registered name of the agent providing the service.
        template_id : str, optional
            UUID of the SLA template to base the contract on.
        price : float, optional
            Agreed price in XMR.  Converted to string for the API.
        **kwargs
            Additional fields forwarded verbatim to the request body.

        Returns
        -------
        dict
            Created SLA contract record from the API.
        """
        payload = {"provider_agent_name": provider}
        if template_id is not None:
            payload["template_id"] = template_id
        if price is not None:
            payload["price"] = str(price)
        payload.update(kwargs)
        return self._raw_post("/v2/sla/contracts", json_body=payload)

    def sla_accept(self, contract_id):
        # type: (str) -> dict
        """Accept an SLA contract as the service provider.

        Parameters
        ----------
        contract_id : str
            UUID of the SLA contract to accept.

        Returns
        -------
        dict
            Updated SLA contract record.
        """
        return self._raw_patch(
            "/v2/sla/contracts/{}/accept".format(contract_id)
        )

    def sla_deliver(self, contract_id, result_hash=None):
        # type: (str, str) -> dict
        """Mark an SLA contract as delivered.

        Parameters
        ----------
        contract_id : str
            UUID of the SLA contract.
        result_hash : str, optional
            Content hash of the delivered artefact (e.g. ``"sha256:abc..."``).

        Returns
        -------
        dict
            Updated SLA contract record.
        """
        payload = {}
        if result_hash is not None:
            payload["result_hash"] = result_hash
        return self._raw_patch(
            "/v2/sla/contracts/{}/deliver".format(contract_id),
            json_body=payload,
        )

    def sla_verify(self, contract_id):
        # type: (str) -> dict
        """Verify and complete an SLA contract (buyer side).

        Parameters
        ----------
        contract_id : str
            UUID of the SLA contract.

        Returns
        -------
        dict
            Updated SLA contract record with ``status`` set to ``"completed"``.
        """
        return self._raw_patch(
            "/v2/sla/contracts/{}/verify".format(contract_id)
        )

    # -- Reviews API --------------------------------------------------------

    def review(self, agent_id, transaction_id, transaction_type, overall_rating, **kwargs):
        # type: (str, str, str, int, ...) -> dict
        """Submit a review for an agent.

        Parameters
        ----------
        agent_id : str
            UUID of the agent being reviewed.
        transaction_id : str
            UUID of the payment or escrow associated with the review.
        transaction_type : str
            Type of the transaction: ``"payment"`` or ``"escrow"``.
        overall_rating : int
            Integer rating from 1 to 5.
        **kwargs
            Optional fields forwarded verbatim, e.g. ``comment``,
            ``timeliness``, ``quality``.

        Returns
        -------
        dict
            Created review record from the API.
        """
        payload = {
            "transaction_id": transaction_id,
            "transaction_type": transaction_type,
            "overall_rating": overall_rating,
        }
        payload.update(kwargs)
        return self._raw_post("/v2/agents/{}/reviews".format(agent_id), json_body=payload)

    # -- Payment Channels API -----------------------------------------------

    def channel_open(self, agent_name, deposit, settlement_period=3600):
        # type: (str, float, int) -> dict
        """Open a payment channel with another agent.

        Parameters
        ----------
        agent_name : str
            Registered name of the counterparty agent.
        deposit : float
            Amount in XMR to deposit into the channel.  Converted to string.
        settlement_period : int, optional
            Settlement window in seconds.  Defaults to 3600.

        Returns
        -------
        dict
            Channel record including ``channel_id`` and ``status``.
        """
        payload = {
            "counterparty_agent_name": agent_name,
            "deposit": str(deposit),
            "settlement_period": settlement_period,
        }
        return self._raw_post("/v2/channels", json_body=payload)

    def channel_settle(self, channel_id, nonce, balance_a, balance_b, signature_a, signature_b):
        # type: (str, int, float, float, str, str) -> dict
        """Submit a signed state update to settle a payment channel.

        Parameters
        ----------
        channel_id : str
            UUID of the channel to settle.
        nonce : int
            Monotonically increasing update counter.
        balance_a : float
            Balance owed to party A.  Converted to string.
        balance_b : float
            Balance owed to party B.  Converted to string.
        signature_a : str
            Party A's signature over the state.
        signature_b : str
            Party B's signature over the state.

        Returns
        -------
        dict
            Updated channel record.
        """
        payload = {
            "nonce": nonce,
            "balance_a": str(balance_a),
            "balance_b": str(balance_b),
            "signature_a": signature_a,
            "signature_b": signature_b,
        }
        return self._raw_post("/v2/channels/{}/settle".format(channel_id), json_body=payload)

    def channel_close(self, channel_id):
        # type: (str) -> dict
        """Close a settled payment channel and disburse final balances.

        Parameters
        ----------
        channel_id : str
            UUID of the channel to close.

        Returns
        -------
        dict
            Confirmation with final channel status.
        """
        return self._raw_post("/v2/channels/{}/close".format(channel_id))

    def channels(self):
        # type: () -> dict
        """List all payment channels for the authenticated agent.

        Returns
        -------
        dict or list
            Channel records from the API.
        """
        return self._raw_get("/v2/channels")

    # -- Recurring Subscriptions API ----------------------------------------

    def subscribe(self, to_agent, amount, interval, max_payments=None):
        # type: (str, float, int, int) -> dict
        """Create a recurring payment subscription to another agent.

        Parameters
        ----------
        to_agent : str
            Registered name of the recipient agent.
        amount : float
            Amount in XMR per payment cycle.  Converted to string.
        interval : int
            Interval in seconds between payments.
        max_payments : int, optional
            Maximum number of payments before the subscription expires.
            Omitted from payload when not provided.

        Returns
        -------
        dict
            Subscription record including ``subscription_id`` and ``status``.
        """
        payload = {
            "to_agent_name": to_agent,
            "amount": str(amount),
            "interval": interval,
        }
        if max_payments is not None:
            payload["max_payments"] = max_payments
        return self._raw_post("/v2/subscriptions", json_body=payload)

    def unsubscribe(self, subscription_id):
        # type: (str) -> dict
        """Cancel a recurring payment subscription.

        Parameters
        ----------
        subscription_id : str
            UUID of the subscription to cancel.

        Returns
        -------
        dict
            Cancellation confirmation.
        """
        return self._raw_request("DELETE", "/v2/subscriptions/{}".format(subscription_id))

    def subscriptions(self):
        # type: () -> dict
        """List all recurring payment subscriptions for the authenticated agent.

        Returns
        -------
        dict or list
            Subscription records from the API.
        """
        return self._raw_get("/v2/subscriptions")

    # -- Payment Streams API ------------------------------------------------

    def stream_start(self, channel_id, rate_per_second):
        # type: (str, float) -> dict
        """Start a continuous micropayment stream over an open channel.

        Parameters
        ----------
        channel_id : str
            UUID of the payment channel to stream over.
        rate_per_second : float
            Amount in XMR per second.  Converted to string.

        Returns
        -------
        dict
            Stream record including ``stream_id`` and ``status``.
        """
        payload = {
            "channel_id": str(channel_id),
            "rate_per_second": str(rate_per_second),
        }
        return self._raw_post("/v2/streams", json_body=payload)

    def stream_stop(self, stream_id):
        # type: (str) -> dict
        """Stop an active micropayment stream.

        Parameters
        ----------
        stream_id : str
            UUID of the stream to stop.

        Returns
        -------
        dict
            Final stream record including ``final_balance``.
        """
        return self._raw_post("/v2/streams/{}/stop".format(stream_id))

    # -- Matchmaking API ----------------------------------------------------

    def matchmake(self, capabilities, budget, deadline_secs, min_rating=0, auto_assign=False):
        # type: (list, float, int, float, bool) -> dict
        """Submit a matchmaking request to find suitable agents.

        Parameters
        ----------
        capabilities : list of str
            Required capabilities, e.g. ``["translation", "proofreading"]``.
        budget : float
            Maximum budget in XMR.  Converted to string for the API.
        deadline_secs : int
            Deadline in seconds from now.
        min_rating : float, optional
            Minimum acceptable average rating (0–5).  Defaults to 0.
        auto_assign : bool, optional
            When ``True``, automatically assign the best match.  Defaults to
            ``False``.

        Returns
        -------
        dict
            Matchmaking request record including ``request_id`` and
            ``status``.
        """
        payload = {
            "required_capabilities": capabilities,
            "budget": str(budget),
            "deadline_secs": deadline_secs,
            "min_rating": str(min_rating),
            "auto_assign": auto_assign,
            "task_description": "Auto-matchmake",
        }
        return self._raw_post("/v2/matchmaking/request", json_body=payload)

    # -- Multi-currency / Swap API ------------------------------------------

    def swap_rates(self):
        # type: () -> dict
        """Fetch current swap rates from the API.

        Returns
        -------
        dict
            Map of pair keys to rate strings, e.g. ``{"XMR_USD": "150.00"}``.
        """
        return self._raw_get("/v2/swap/rates")

    def swap_quote(self, from_currency, from_amount, to_currency="XMR"):
        # type: (str, ..., str) -> dict
        """Request a swap quote without executing the swap.

        Parameters
        ----------
        from_currency : str
            Currency to swap from, e.g. ``"xUSD"``.
        from_amount : float or Decimal or str
            Amount to swap.
        to_currency : str
            Currency to receive (default ``"XMR"``).

        Returns
        -------
        dict
            Quote including ``rate``, ``to_amount``, and ``fee``.
        """
        payload = {
            "from_currency": from_currency,
            "from_amount": str(from_amount),
            "to_currency": to_currency,
        }
        return self._raw_post("/v2/swap/quote", json_body=payload)

    def swap(self, from_currency, from_amount):
        # type: (str, ...) -> dict
        """Execute an on-chain swap.

        Parameters
        ----------
        from_currency : str
            Currency to swap from.
        from_amount : float or Decimal or str
            Amount to swap.

        Returns
        -------
        dict
            Swap receipt including ``swap_id`` and ``status``.
        """
        payload = {
            "from_currency": from_currency,
            "from_amount": str(from_amount),
        }
        return self._raw_post("/v2/swap/create", json_body=payload)

    def convert(self, from_currency, to_currency, amount):
        # type: (str, str, ...) -> dict
        """Convert between hub-balance currencies.

        Parameters
        ----------
        from_currency : str
            Source currency, e.g. ``"XMR"``.
        to_currency : str
            Target currency, e.g. ``"xUSD"``.
        amount : float or Decimal or str
            Amount to convert.

        Returns
        -------
        dict
            Conversion result including ``from_amount``, ``to_amount``, ``rate``,
            and ``fee_amount``.
        """
        payload = {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "amount": str(amount),
        }
        return self._raw_post("/v2/balance/convert", json_body=payload)

    def balances_all(self):
        # type: () -> dict
        """Return all token balances for the authenticated agent.

        Returns
        -------
        dict
            Response containing a ``balances`` key mapping token symbols to
            amount strings.
        """
        return self._raw_get("/v2/balance/all")
