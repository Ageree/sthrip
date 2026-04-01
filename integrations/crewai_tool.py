"""Sthrip CrewAI integration -- tool set for autonomous AI crews to make payments.

Usage::

    from crewai import Agent
    from integrations.crewai_tool import get_sthrip_crew_tools

    tools = get_sthrip_crew_tools()
    agent = Agent(
        role="Treasurer",
        tools=tools,
        ...
    )

Requires: pip install crewai sthrip
"""

from __future__ import annotations

import json
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

try:
    from crewai.tools import BaseTool
except ImportError:
    raise ImportError(
        "crewai is required for this integration. "
        "Install it with: pip install crewai"
    )

from sthrip import Sthrip


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_client(api_key: Optional[str] = None) -> Sthrip:
    """Return a new Sthrip client (auto-registers if no credentials found)."""
    return Sthrip(api_key=api_key)


def _json_result(data: Any) -> str:
    """Serialize *data* to a compact JSON string for LLM consumption."""
    return json.dumps(data, default=str)


# ---------------------------------------------------------------------------
# Input schemas
# ---------------------------------------------------------------------------

class PayInput(BaseModel):
    agent_name: str = Field(description="Recipient agent's registered name")
    amount: float = Field(description="Amount in XMR to send")
    memo: Optional[str] = Field(default=None, description="Optional note for the payment")


class BalanceInput(BaseModel):
    """No parameters required."""


class EscrowCreateInput(BaseModel):
    seller_agent_name: str = Field(description="Seller agent's registered name")
    amount: float = Field(description="Escrow amount in XMR")
    description: str = Field(default="", description="Description of work to be performed")
    delivery_hours: int = Field(default=48, description="Hours seller has to deliver")
    review_hours: int = Field(default=24, description="Hours buyer has to review")
    accept_hours: int = Field(default=24, description="Hours seller has to accept")


class EscrowIdInput(BaseModel):
    escrow_id: str = Field(description="UUID of the escrow")


class EscrowReleaseInput(BaseModel):
    escrow_id: str = Field(description="UUID of the escrow")
    amount: float = Field(description="Amount to release to seller (0 = full refund)")


class FindAgentsInput(BaseModel):
    limit: int = Field(default=20, description="Max number of agents to return")
    offset: int = Field(default=0, description="Pagination offset")
    min_trust_score: Optional[float] = Field(default=None, description="Minimum trust score filter")
    verified_only: Optional[bool] = Field(default=None, description="Only return verified agents")


# -- Phase 3a: SLA, Reviews, Matchmaking --

class SlaTemplateCreateInput(BaseModel):
    name: str = Field(description="Short name for this SLA template")
    deliverables: list[str] = Field(description="List of deliverable identifiers promised under this SLA")
    response_time_secs: int = Field(description="Maximum response time in seconds")
    delivery_time_secs: int = Field(description="Maximum delivery time in seconds")
    base_price: float = Field(description="Baseline price in XMR")
    penalty_percent: int = Field(default=10, description="Penalty percentage on SLA breach")
    service_description: str = Field(default="", description="Human-readable description of the service")


class SlaCreateInput(BaseModel):
    provider: str = Field(description="Registered name of the provider agent")
    template_id: Optional[str] = Field(default=None, description="UUID of the SLA template to base the contract on")
    price: Optional[float] = Field(default=None, description="Agreed price in XMR")


class ReviewAgentInput(BaseModel):
    agent_id: str = Field(description="UUID of the agent being reviewed")
    transaction_id: str = Field(description="UUID of the payment or escrow associated with the review")
    transaction_type: str = Field(description="Type of transaction: 'payment' or 'escrow'")
    overall_rating: int = Field(description="Integer rating from 1 to 5")
    comment: Optional[str] = Field(default=None, description="Optional text comment")


class MatchmakeInput(BaseModel):
    capabilities: list[str] = Field(description="Required capabilities, e.g. ['translation', 'proofreading']")
    budget: float = Field(description="Maximum budget in XMR")
    deadline_secs: int = Field(description="Deadline in seconds from now")
    min_rating: float = Field(default=0, description="Minimum acceptable average rating (0-5)")
    auto_assign: bool = Field(default=False, description="Automatically assign best match when True")


# -- Phase 3b: Channels, Subscriptions, Streams --

class ChannelOpenInput(BaseModel):
    agent_name: str = Field(description="Registered name of the counterparty agent")
    deposit: float = Field(description="Amount in XMR to deposit into the channel")
    settlement_period: int = Field(default=3600, description="Settlement window in seconds")


class SubscribeInput(BaseModel):
    to_agent: str = Field(description="Registered name of the recipient agent")
    amount: float = Field(description="Amount in XMR per payment cycle")
    interval: int = Field(description="Interval in seconds between payments")
    max_payments: Optional[int] = Field(default=None, description="Maximum number of payments before expiry")


class StreamStartInput(BaseModel):
    channel_id: str = Field(description="UUID of the payment channel to stream over")
    rate_per_second: float = Field(description="Amount in XMR per second")


# -- Phase 3c: Swap & Conversion --

class SwapRatesInput(BaseModel):
    """No parameters required."""


class SwapInput(BaseModel):
    from_currency: str = Field(description="Currency to swap from, e.g. 'xUSD'")
    from_amount: float = Field(description="Amount to swap")


class ConvertInput(BaseModel):
    from_currency: str = Field(description="Source currency, e.g. 'XMR'")
    to_currency: str = Field(description="Target currency, e.g. 'xUSD'")
    amount: float = Field(description="Amount to convert")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class StrhipPayTool(BaseTool):
    name: str = "sthrip_pay"
    description: str = (
        "Send an anonymous XMR (Monero) payment to another registered AI agent. "
        "The payment is private and untraceable."
    )
    args_schema: Type[BaseModel] = PayInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, agent_name: str, amount: float, memo: Optional[str] = None) -> str:
        try:
            result = self.client.pay(agent_name, amount, memo=memo)
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipBalanceTool(BaseTool):
    name: str = "sthrip_balance"
    description: str = (
        "Check your current XMR wallet balance including available, pending, "
        "and deposit address."
    )
    args_schema: Type[BaseModel] = BalanceInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self) -> str:
        try:
            result = self.client.balance()
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipEscrowCreateTool(BaseTool):
    name: str = "sthrip_escrow_create"
    description: str = (
        "Create an escrow holding XMR for a seller agent. "
        "Funds are locked until delivery and buyer approval."
    )
    args_schema: Type[BaseModel] = EscrowCreateInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(
        self,
        seller_agent_name: str,
        amount: float,
        description: str = "",
        delivery_hours: int = 48,
        review_hours: int = 24,
        accept_hours: int = 24,
    ) -> str:
        try:
            result = self.client.escrow_create(
                seller_agent_name=seller_agent_name,
                amount=amount,
                description=description,
                delivery_hours=delivery_hours,
                review_hours=review_hours,
                accept_hours=accept_hours,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipEscrowAcceptTool(BaseTool):
    name: str = "sthrip_escrow_accept"
    description: str = "Accept an incoming escrow as the seller, committing to deliver work."
    args_schema: Type[BaseModel] = EscrowIdInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, escrow_id: str) -> str:
        try:
            result = self.client.escrow_accept(escrow_id)
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipEscrowDeliverTool(BaseTool):
    name: str = "sthrip_escrow_deliver"
    description: str = (
        "Mark escrow work as delivered. The buyer can then review and release funds."
    )
    args_schema: Type[BaseModel] = EscrowIdInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, escrow_id: str) -> str:
        try:
            result = self.client.escrow_deliver(escrow_id)
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipEscrowReleaseTool(BaseTool):
    name: str = "sthrip_escrow_release"
    description: str = (
        "Release escrowed XMR to the seller after delivery. "
        "Set amount to 0 for full refund, or the escrow amount for full release."
    )
    args_schema: Type[BaseModel] = EscrowReleaseInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, escrow_id: str, amount: float) -> str:
        try:
            result = self.client.escrow_release(escrow_id, amount)
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipFindAgentsTool(BaseTool):
    name: str = "sthrip_find_agents"
    description: str = "Discover AI agents registered on the Sthrip payment network."
    args_schema: Type[BaseModel] = FindAgentsInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(
        self,
        limit: int = 20,
        offset: int = 0,
        min_trust_score: Optional[float] = None,
        verified_only: Optional[bool] = None,
    ) -> str:
        try:
            result = self.client.find_agents(
                limit=limit,
                offset=offset,
                min_trust_score=min_trust_score,
                verified_only=verified_only,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


# -- Phase 3a: SLA, Reviews, Matchmaking tools --------------------------------


class StrhipSlaTemplateCreateTool(BaseTool):
    name: str = "sthrip_sla_template_create"
    description: str = (
        "Create an SLA service template defining deliverables, response times, "
        "delivery times, base price, and penalty terms."
    )
    args_schema: Type[BaseModel] = SlaTemplateCreateInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(
        self,
        name: str,
        deliverables: list[str],
        response_time_secs: int,
        delivery_time_secs: int,
        base_price: float,
        penalty_percent: int = 10,
        service_description: str = "",
    ) -> str:
        try:
            result = self.client.sla_template_create(
                name=name,
                deliverables=deliverables,
                response_time_secs=response_time_secs,
                delivery_time_secs=delivery_time_secs,
                base_price=base_price,
                penalty_percent=penalty_percent,
                service_description=service_description,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipSlaCreateTool(BaseTool):
    name: str = "sthrip_sla_create"
    description: str = (
        "Create an SLA contract with a provider agent, optionally referencing "
        "an existing SLA template and agreed price."
    )
    args_schema: Type[BaseModel] = SlaCreateInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(
        self,
        provider: str,
        template_id: Optional[str] = None,
        price: Optional[float] = None,
    ) -> str:
        try:
            result = self.client.sla_create(
                provider=provider,
                template_id=template_id,
                price=price,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipReviewAgentTool(BaseTool):
    name: str = "sthrip_review_agent"
    description: str = (
        "Submit a review for an agent based on a completed payment or escrow. "
        "Rating is 1-5."
    )
    args_schema: Type[BaseModel] = ReviewAgentInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(
        self,
        agent_id: str,
        transaction_id: str,
        transaction_type: str,
        overall_rating: int,
        comment: Optional[str] = None,
    ) -> str:
        try:
            kwargs: dict[str, Any] = {}
            if comment is not None:
                kwargs["comment"] = comment
            result = self.client.review(
                agent_id=agent_id,
                transaction_id=transaction_id,
                transaction_type=transaction_type,
                overall_rating=overall_rating,
                **kwargs,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipMatchmakeTool(BaseTool):
    name: str = "sthrip_matchmake"
    description: str = (
        "Submit a matchmaking request to automatically find the best agent "
        "for a task given required capabilities, budget, and deadline."
    )
    args_schema: Type[BaseModel] = MatchmakeInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(
        self,
        capabilities: list[str],
        budget: float,
        deadline_secs: int,
        min_rating: float = 0,
        auto_assign: bool = False,
    ) -> str:
        try:
            result = self.client.matchmake(
                capabilities=capabilities,
                budget=budget,
                deadline_secs=deadline_secs,
                min_rating=min_rating,
                auto_assign=auto_assign,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


# -- Phase 3b: Channels, Subscriptions, Streams tools -------------------------


class StrhipChannelOpenTool(BaseTool):
    name: str = "sthrip_channel_open"
    description: str = (
        "Open a bidirectional payment channel with another agent by depositing XMR. "
        "Enables high-frequency micropayments without on-chain fees."
    )
    args_schema: Type[BaseModel] = ChannelOpenInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(
        self,
        agent_name: str,
        deposit: float,
        settlement_period: int = 3600,
    ) -> str:
        try:
            result = self.client.channel_open(
                agent_name=agent_name,
                deposit=deposit,
                settlement_period=settlement_period,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipSubscribeTool(BaseTool):
    name: str = "sthrip_subscribe"
    description: str = (
        "Set up a recurring XMR payment to another agent at a fixed interval. "
        "Optionally limit the total number of payments."
    )
    args_schema: Type[BaseModel] = SubscribeInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(
        self,
        to_agent: str,
        amount: float,
        interval: int,
        max_payments: Optional[int] = None,
    ) -> str:
        try:
            result = self.client.subscribe(
                to_agent=to_agent,
                amount=amount,
                interval=interval,
                max_payments=max_payments,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipStreamStartTool(BaseTool):
    name: str = "sthrip_stream_start"
    description: str = (
        "Start streaming XMR payments at a specified rate per second over "
        "an existing payment channel."
    )
    args_schema: Type[BaseModel] = StreamStartInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, channel_id: str, rate_per_second: float) -> str:
        try:
            result = self.client.stream_start(
                channel_id=channel_id,
                rate_per_second=rate_per_second,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


# -- Phase 3c: Swap & Conversion tools ----------------------------------------


class StrhipSwapRatesTool(BaseTool):
    name: str = "sthrip_swap_rates"
    description: str = "Get current exchange rates for all supported swap pairs (e.g. XMR/USD, XMR/EUR)."
    args_schema: Type[BaseModel] = SwapRatesInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self) -> str:
        try:
            result = self.client.swap_rates()
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipSwapTool(BaseTool):
    name: str = "sthrip_swap"
    description: str = "Execute an on-chain swap from one currency to XMR."
    args_schema: Type[BaseModel] = SwapInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, from_currency: str, from_amount: float) -> str:
        try:
            result = self.client.swap(
                from_currency=from_currency,
                from_amount=from_amount,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipConvertTool(BaseTool):
    name: str = "sthrip_convert"
    description: str = (
        "Convert between hub-balance currencies such as XMR, xUSD, and xEUR. "
        "Returns the converted amount, rate, and fee."
    )
    args_schema: Type[BaseModel] = ConvertInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, from_currency: str, to_currency: str, amount: float) -> str:
        try:
            result = self.client.convert(
                from_currency=from_currency,
                to_currency=to_currency,
                amount=amount,
            )
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

def get_sthrip_crew_tools(api_key: Optional[str] = None) -> list[BaseTool]:
    """Return all Sthrip tools wired to a shared client instance.

    Parameters
    ----------
    api_key : str, optional
        Explicit API key.  Falls back to env / credentials / auto-register.
    """
    client = _make_client(api_key)
    return [
        # Core
        StrhipPayTool(client=client),
        StrhipBalanceTool(client=client),
        # Escrow
        StrhipEscrowCreateTool(client=client),
        StrhipEscrowAcceptTool(client=client),
        StrhipEscrowDeliverTool(client=client),
        StrhipEscrowReleaseTool(client=client),
        # Discovery
        StrhipFindAgentsTool(client=client),
        # Phase 3a: SLA, Reviews, Matchmaking
        StrhipSlaTemplateCreateTool(client=client),
        StrhipSlaCreateTool(client=client),
        StrhipReviewAgentTool(client=client),
        StrhipMatchmakeTool(client=client),
        # Phase 3b: Channels, Subscriptions, Streams
        StrhipChannelOpenTool(client=client),
        StrhipSubscribeTool(client=client),
        StrhipStreamStartTool(client=client),
        # Phase 3c: Swap & Conversion
        StrhipSwapRatesTool(client=client),
        StrhipSwapTool(client=client),
        StrhipConvertTool(client=client),
    ]
