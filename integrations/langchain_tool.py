"""Sthrip LangChain integration -- tool set for AI agents to make anonymous payments.

Usage::

    from integrations.langchain_tool import get_sthrip_tools
    tools = get_sthrip_tools()  # pass to your LangChain agent

Requires: pip install langchain-core sthrip
"""

from __future__ import annotations

import json
from typing import Any, Optional, Type

from pydantic import BaseModel, Field

try:
    from langchain_core.tools import BaseTool
except ImportError:
    raise ImportError(
        "langchain-core is required for this integration. "
        "Install it with: pip install langchain-core"
    )

from sthrip import Sthrip


# ---------------------------------------------------------------------------
# Shared client factory
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


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class StrhipPayTool(BaseTool):
    """Send an anonymous XMR payment to another AI agent."""

    name: str = "sthrip_pay"
    description: str = (
        "Send XMR (Monero) to another registered AI agent. "
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
    """Check the current XMR balance of the authenticated agent."""

    name: str = "sthrip_balance"
    description: str = (
        "Check your current wallet balance including available, pending, "
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
    """Create a new escrow as buyer -- funds are locked until work is delivered."""

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
    """Accept an incoming escrow as the seller."""

    name: str = "sthrip_escrow_accept"
    description: str = "Accept an escrow contract as the seller, committing to deliver."
    args_schema: Type[BaseModel] = EscrowIdInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, escrow_id: str) -> str:
        try:
            result = self.client.escrow_accept(escrow_id)
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipEscrowDeliverTool(BaseTool):
    """Mark an escrow as delivered (seller signals work is complete)."""

    name: str = "sthrip_escrow_deliver"
    description: str = "Mark escrow work as delivered. The buyer can then review and release funds."
    args_schema: Type[BaseModel] = EscrowIdInput
    client: Sthrip = Field(default_factory=_make_client)

    def _run(self, escrow_id: str) -> str:
        try:
            result = self.client.escrow_deliver(escrow_id)
            return _json_result(result)
        except Exception as exc:
            return _json_result({"error": str(exc)})


class StrhipEscrowReleaseTool(BaseTool):
    """Release escrow funds to the seller (buyer approves delivery)."""

    name: str = "sthrip_escrow_release"
    description: str = (
        "Release escrowed XMR to the seller. "
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
    """Discover other AI agents registered on the Sthrip network."""

    name: str = "sthrip_find_agents"
    description: str = "Search for registered AI agents on the Sthrip payment network."
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


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

def get_sthrip_tools(api_key: Optional[str] = None) -> list[BaseTool]:
    """Return all Sthrip tools wired to a shared client instance.

    Parameters
    ----------
    api_key : str, optional
        Explicit API key.  Falls back to env / credentials / auto-register.
    """
    client = _make_client(api_key)
    return [
        StrhipPayTool(client=client),
        StrhipBalanceTool(client=client),
        StrhipEscrowCreateTool(client=client),
        StrhipEscrowAcceptTool(client=client),
        StrhipEscrowDeliverTool(client=client),
        StrhipEscrowReleaseTool(client=client),
        StrhipFindAgentsTool(client=client),
    ]
