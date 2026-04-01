"""Async HTTP client for Sthrip REST API."""

import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx


@dataclass(frozen=True)
class ApiError:
    """Immutable API error details."""

    status_code: int
    message: str


class SthripApiError(Exception):
    """Raised when the Sthrip API returns an error."""

    def __init__(self, error: ApiError) -> None:
        self.error = error
        super().__init__(f"API error {error.status_code}: {error.message}")


_ERROR_MESSAGES = {
    401: "Authentication required. Use 'register_agent' or set STHRIP_API_KEY.",
    403: "Access forbidden. Your account may be disabled.",
    404: "Resource not found.",
    429: "Rate limit exceeded. Try again later.",
}


def _build_headers(api_key: Optional[str]) -> Dict[str, str]:
    """Build request headers with optional auth."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _idempotency_headers() -> Dict[str, str]:
    """Generate idempotency key header for mutation requests."""
    return {"Idempotency-Key": str(uuid.uuid4())}


async def _handle_response(response: httpx.Response) -> Dict[str, Any]:
    """Parse response or raise ApiError."""
    if response.is_success:
        return response.json()

    message = _ERROR_MESSAGES.get(response.status_code)
    if not message:
        try:
            body = response.json()
            message = body.get("detail", body.get("error", response.text))
        except (json.JSONDecodeError, ValueError):
            message = response.text or f"HTTP {response.status_code}"

    raise SthripApiError(ApiError(response.status_code, message))


class SthripClient:
    """Async HTTP client wrapping the Sthrip v2 API.

    All methods return raw dict responses from the API.
    The client never mutates its own state after construction.
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._shared_client: Optional[httpx.AsyncClient] = None

    @property
    def api_key(self) -> Optional[str]:
        return self._api_key

    def with_api_key(self, api_key: str) -> "SthripClient":
        """Return a new client with the given API key (immutable update)."""
        return SthripClient(self._base_url, api_key, self._timeout)

    def _client(self) -> httpx.AsyncClient:
        """Get or create a shared httpx client for connection reuse."""
        if self._shared_client is None or self._shared_client.is_closed:
            self._shared_client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=_build_headers(self._api_key),
                timeout=self._timeout,
            )
        return self._shared_client

    async def close(self) -> None:
        """Close the shared HTTP client."""
        if self._shared_client and not self._shared_client.is_closed:
            await self._shared_client.aclose()

    # --- Discovery (no auth required) ---

    async def search_agents(
        self,
        query: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if query:
            params["query"] = query
        resp = await self._client().get("/v2/agents", params=params)
        return await _handle_response(resp)

    async def get_agent_profile(self, agent_name: str) -> Dict[str, Any]:
        resp = await self._client().get(f"/v2/agents/{agent_name}")
        return await _handle_response(resp)

    async def get_leaderboard(self, limit: int = 10) -> Dict[str, Any]:
        resp = await self._client().get("/v2/leaderboard", params={"limit": limit})
        return await _handle_response(resp)

    # --- Registration & Profile (auth required except register) ---

    async def register_agent(
        self,
        agent_name: str,
        privacy_level: str = "medium",
        webhook_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "agent_name": agent_name,
            "privacy_level": privacy_level,
        }
        if webhook_url:
            body["webhook_url"] = webhook_url
        resp = await self._client().post(
            "/v2/agents/register",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def get_me(self) -> Dict[str, Any]:
        resp = await self._client().get("/v2/me")
        return await _handle_response(resp)

    async def update_settings(
        self,
        webhook_url: Optional[str] = None,
        privacy_level: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if webhook_url is not None:
            body["webhook_url"] = webhook_url
        if privacy_level is not None:
            body["privacy_level"] = privacy_level
        resp = await self._client().patch("/v2/me/settings", json=body)
        return await _handle_response(resp)

    # --- Payments (auth required) ---

    async def send_payment(
        self,
        to_agent_name: str,
        amount: float,
        memo: Optional[str] = None,
        urgency: str = "normal",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "to_agent_name": to_agent_name,
            "amount": amount,
            "urgency": urgency,
        }
        if memo:
            body["memo"] = memo
        resp = await self._client().post(
            "/v2/payments/hub-routing",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def get_payment_status(self, payment_id: str) -> Dict[str, Any]:
        resp = await self._client().get(f"/v2/payments/{payment_id}")
        return await _handle_response(resp)

    async def get_payment_history(
        self,
        direction: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if direction:
            params["direction"] = direction
        resp = await self._client().get("/v2/payments/history", params=params)
        return await _handle_response(resp)

    # --- Balance (auth required) ---

    async def get_balance(self) -> Dict[str, Any]:
        resp = await self._client().get("/v2/balance")
        return await _handle_response(resp)

    async def deposit(self) -> Dict[str, Any]:
        resp = await self._client().post(
            "/v2/balance/deposit",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def withdraw(
        self,
        amount: float,
        address: str,
    ) -> Dict[str, Any]:
        resp = await self._client().post(
            "/v2/balance/withdraw",
            json={"amount": amount, "address": address},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    # --- Escrow (auth required) ---

    async def escrow_create(
        self,
        seller_agent_name: str,
        amount: float,
        description: str,
        accept_timeout_hours: int = 24,
        delivery_timeout_hours: int = 48,
        review_timeout_hours: int = 24,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "seller_agent_name": seller_agent_name,
            "amount": amount,
            "description": description,
            "accept_timeout_hours": accept_timeout_hours,
            "delivery_timeout_hours": delivery_timeout_hours,
            "review_timeout_hours": review_timeout_hours,
        }
        resp = await self._client().post(
            "/v2/escrow",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def escrow_accept(self, escrow_id: str) -> Dict[str, Any]:
        resp = await self._client().post(
            f"/v2/escrow/{escrow_id}/accept",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def escrow_deliver(self, escrow_id: str) -> Dict[str, Any]:
        resp = await self._client().post(
            f"/v2/escrow/{escrow_id}/deliver",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def escrow_release(
        self,
        escrow_id: str,
        release_amount: float,
    ) -> Dict[str, Any]:
        resp = await self._client().post(
            f"/v2/escrow/{escrow_id}/release",
            json={"release_amount": release_amount},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def escrow_cancel(self, escrow_id: str) -> Dict[str, Any]:
        resp = await self._client().post(
            f"/v2/escrow/{escrow_id}/cancel",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def escrow_get(self, escrow_id: str) -> Dict[str, Any]:
        resp = await self._client().get(f"/v2/escrow/{escrow_id}")
        return await _handle_response(resp)

    async def escrow_list(
        self,
        role: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if role:
            params["role"] = role
        if status:
            params["status"] = status
        resp = await self._client().get("/v2/escrow", params=params)
        return await _handle_response(resp)

    # --- Spending Policy (auth required) ---

    async def set_spending_policy(
        self,
        max_per_tx: Optional[float] = None,
        max_daily: Optional[float] = None,
        allowed_recipients: Optional[list] = None,
        require_confirmation_above: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if max_per_tx is not None:
            body["max_per_tx"] = max_per_tx
        if max_daily is not None:
            body["max_daily"] = max_daily
        if allowed_recipients is not None:
            body["allowed_recipients"] = allowed_recipients
        if require_confirmation_above is not None:
            body["require_confirmation_above"] = require_confirmation_above
        resp = await self._client().put("/v2/me/spending-policy", json=body)
        return await _handle_response(resp)

    async def get_spending_policy(self) -> Dict[str, Any]:
        resp = await self._client().get("/v2/me/spending-policy")
        return await _handle_response(resp)

    # --- Encrypted Messaging (auth required) ---

    async def register_encryption_key(
        self,
        public_key: str,
    ) -> Dict[str, Any]:
        resp = await self._client().put(
            "/v2/me/encryption-key",
            json={"public_key": public_key},
        )
        return await _handle_response(resp)

    async def get_agent_public_key(
        self,
        agent_id: str,
    ) -> Dict[str, Any]:
        resp = await self._client().get(f"/v2/agents/{agent_id}/public-key")
        return await _handle_response(resp)

    async def send_message(
        self,
        to_agent_id: str,
        ciphertext: str,
        nonce: str,
        ephemeral_public_key: str,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "to_agent_id": to_agent_id,
            "ciphertext": ciphertext,
            "nonce": nonce,
            "ephemeral_public_key": ephemeral_public_key,
        }
        resp = await self._client().post(
            "/v2/messages/send",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def get_messages(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        resp = await self._client().get("/v2/messages/inbox", params=params)
        return await _handle_response(resp)

    # --- ZK Reputation (auth required for generate, public for verify) ---

    async def generate_reputation_proof(
        self,
        claim_type: str,
        threshold: Optional[float] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"claim_type": claim_type}
        if threshold is not None:
            body["threshold"] = threshold
        resp = await self._client().post(
            "/v2/me/reputation-proof",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def verify_reputation_proof(
        self,
        proof: str,
        claim_type: str,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "proof": proof,
            "claim_type": claim_type,
        }
        if agent_id is not None:
            body["agent_id"] = agent_id
        resp = await self._client().post("/v2/verify-reputation", json=body)
        return await _handle_response(resp)

    # --- PoW Registration (no auth required) ---

    async def get_pow_challenge(self) -> Dict[str, Any]:
        resp = await self._client().post(
            "/v2/agents/register/challenge",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    # --- SLA & Marketplace (Phase 3a) ---

    async def sla_template_create(
        self,
        name: str,
        description: str,
        metrics: Dict[str, Any],
        penalty_basis_points: int = 100,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "metrics": metrics,
            "penalty_basis_points": penalty_basis_points,
        }
        resp = await self._client().post(
            "/v2/sla/templates",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def sla_create(
        self,
        template_id: str,
        provider_agent_name: str,
        amount: float,
        duration_hours: int,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "template_id": template_id,
            "provider_agent_name": provider_agent_name,
            "amount": amount,
            "duration_hours": duration_hours,
        }
        if parameters is not None:
            body["parameters"] = parameters
        resp = await self._client().post(
            "/v2/sla",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def sla_accept(self, sla_id: str) -> Dict[str, Any]:
        resp = await self._client().post(
            f"/v2/sla/{sla_id}/accept",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def sla_deliver(
        self,
        sla_id: str,
        proof: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if proof is not None:
            body["proof"] = proof
        resp = await self._client().post(
            f"/v2/sla/{sla_id}/deliver",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def sla_verify(
        self,
        sla_id: str,
        accepted: bool,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"accepted": accepted}
        if reason is not None:
            body["reason"] = reason
        resp = await self._client().post(
            f"/v2/sla/{sla_id}/verify",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def review_agent(
        self,
        agent_name: str,
        rating: int,
        comment: Optional[str] = None,
        escrow_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "agent_name": agent_name,
            "rating": rating,
        }
        if comment is not None:
            body["comment"] = comment
        if escrow_id is not None:
            body["escrow_id"] = escrow_id
        resp = await self._client().post(
            "/v2/reviews",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def matchmake(
        self,
        capability: str,
        max_budget: Optional[float] = None,
        min_trust_score: Optional[float] = None,
        limit: int = 10,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"capability": capability, "limit": limit}
        if max_budget is not None:
            params["max_budget"] = max_budget
        if min_trust_score is not None:
            params["min_trust_score"] = min_trust_score
        resp = await self._client().get("/v2/matchmake", params=params)
        return await _handle_response(resp)

    # --- Payment Channels (Phase 3b) ---

    async def channel_open(
        self,
        counterparty_agent_name: str,
        deposit_amount: float,
        settle_timeout_hours: int = 24,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "counterparty_agent_name": counterparty_agent_name,
            "deposit_amount": deposit_amount,
            "settle_timeout_hours": settle_timeout_hours,
        }
        resp = await self._client().post(
            "/v2/channels",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def channel_settle(
        self,
        channel_id: str,
        amount: float,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"amount": amount}
        resp = await self._client().post(
            f"/v2/channels/{channel_id}/settle",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def channel_close(self, channel_id: str) -> Dict[str, Any]:
        resp = await self._client().post(
            f"/v2/channels/{channel_id}/close",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def subscribe(
        self,
        to_agent_name: str,
        amount: float,
        interval_hours: int,
        max_payments: Optional[int] = None,
        memo: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "to_agent_name": to_agent_name,
            "amount": amount,
            "interval_hours": interval_hours,
        }
        if max_payments is not None:
            body["max_payments"] = max_payments
        if memo is not None:
            body["memo"] = memo
        resp = await self._client().post(
            "/v2/subscriptions",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def unsubscribe(self, subscription_id: str) -> Dict[str, Any]:
        resp = await self._client().post(
            f"/v2/subscriptions/{subscription_id}/cancel",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def stream_start(
        self,
        to_agent_name: str,
        rate_per_hour: float,
        max_duration_hours: Optional[float] = None,
        memo: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "to_agent_name": to_agent_name,
            "rate_per_hour": rate_per_hour,
        }
        if max_duration_hours is not None:
            body["max_duration_hours"] = max_duration_hours
        if memo is not None:
            body["memo"] = memo
        resp = await self._client().post(
            "/v2/streams",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def stream_stop(self, stream_id: str) -> Dict[str, Any]:
        resp = await self._client().post(
            f"/v2/streams/{stream_id}/stop",
            json={},
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    # --- Cross-chain (Phase 3c) ---

    async def swap_rates(
        self,
        from_currency: str = "XMR",
        to_currency: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"from": from_currency}
        if to_currency is not None:
            params["to"] = to_currency
        resp = await self._client().get("/v2/swap/rates", params=params)
        return await _handle_response(resp)

    async def swap_quote(
        self,
        from_currency: str,
        to_currency: str,
        amount: float,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "from": from_currency,
            "to": to_currency,
            "amount": amount,
        }
        resp = await self._client().get("/v2/swap/quote", params=params)
        return await _handle_response(resp)

    async def swap(
        self,
        from_currency: str,
        to_currency: str,
        amount: float,
        max_slippage_bps: int = 50,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "amount": amount,
            "max_slippage_bps": max_slippage_bps,
        }
        resp = await self._client().post(
            "/v2/swap",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def convert(
        self,
        from_currency: str,
        to_currency: str,
        amount: float,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "from_currency": from_currency,
            "to_currency": to_currency,
            "amount": amount,
        }
        resp = await self._client().post(
            "/v2/convert",
            json=body,
            headers=_idempotency_headers(),
        )
        return await _handle_response(resp)

    async def balances_all(self) -> Dict[str, Any]:
        resp = await self._client().get("/v2/balance/all")
        return await _handle_response(resp)
