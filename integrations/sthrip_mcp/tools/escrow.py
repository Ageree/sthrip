"""Escrow tools — create, accept, deliver, release, cancel, get, list."""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_escrow_tools(mcp: FastMCP, client: SthripClient) -> None:
    """Register escrow tools on the MCP server."""

    @mcp.tool()
    async def escrow_create(
        seller_agent_name: str,
        amount: float,
        description: str,
        accept_timeout_hours: int = 24,
        delivery_timeout_hours: int = 48,
        review_timeout_hours: int = 24,
    ) -> str:
        """Create a new escrow deal as a buyer.

        Funds are locked from the buyer's balance until the deal
        is released, cancelled, or expires. The seller must accept
        the deal before the accept timeout.

        Requires authentication (API key).

        Args:
            seller_agent_name: The seller agent's name.
            amount: Amount in XMR to escrow (locked from buyer balance).
            description: Description of the deal (max 1000 chars).
            accept_timeout_hours: Hours for seller to accept (default 24).
            delivery_timeout_hours: Hours for seller to deliver after accepting (default 48).
            review_timeout_hours: Hours for buyer to review after delivery (default 24).

        Returns:
            JSON with escrow_id, status, amount, timeouts, and participant details.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.escrow_create(
            seller_agent_name=seller_agent_name,
            amount=amount,
            description=description,
            accept_timeout_hours=accept_timeout_hours,
            delivery_timeout_hours=delivery_timeout_hours,
            review_timeout_hours=review_timeout_hours,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def escrow_accept(escrow_id: str) -> str:
        """Accept an escrow deal as the seller.

        Once accepted, the seller commits to delivering the agreed
        goods or services within the delivery timeout.

        Requires authentication (API key).

        Args:
            escrow_id: The escrow UUID to accept.

        Returns:
            JSON with updated escrow status and delivery deadline.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.escrow_accept(escrow_id)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def escrow_deliver(escrow_id: str) -> str:
        """Mark an escrow as delivered by the seller.

        Signals that the seller has fulfilled their obligation.
        The buyer then has the review timeout period to release
        funds or raise a dispute.

        Requires authentication (API key).

        Args:
            escrow_id: The escrow UUID to mark as delivered.

        Returns:
            JSON with updated escrow status and review deadline.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.escrow_deliver(escrow_id)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def escrow_release(escrow_id: str, release_amount: float) -> str:
        """Release escrowed funds to the seller (buyer action).

        The buyer confirms satisfaction and releases XMR to the
        seller. Partial releases are supported — release_amount
        can be less than the escrowed amount.

        Requires authentication (API key).

        Args:
            escrow_id: The escrow UUID to release.
            release_amount: Amount in XMR to release to the seller.

        Returns:
            JSON with release confirmation, amounts, and final escrow status.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.escrow_release(
            escrow_id=escrow_id,
            release_amount=release_amount,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def escrow_cancel(escrow_id: str) -> str:
        """Cancel an escrow before the seller accepts (buyer action).

        Only works if the escrow is still in 'pending' status
        (seller has not yet accepted). Locked funds are returned
        to the buyer's balance.

        Requires authentication (API key).

        Args:
            escrow_id: The escrow UUID to cancel.

        Returns:
            JSON with cancellation confirmation and refund details.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.escrow_cancel(escrow_id)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def escrow_get(escrow_id: str) -> str:
        """Get details of a specific escrow deal.

        Requires authentication (API key). Only participants
        (buyer or seller) can view escrow details.

        Args:
            escrow_id: The escrow UUID to look up.

        Returns:
            JSON with full escrow details: status, amounts, participants,
            timeouts, and timestamps.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.escrow_get(escrow_id)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def escrow_list(
        role: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List escrow deals for the current agent.

        Requires authentication (API key).

        Args:
            role: Filter by role: 'buyer', 'seller', or 'all' (default: all).
            status: Filter by escrow status (e.g., 'pending', 'accepted', 'delivered', 'released', 'cancelled').
            limit: Maximum results (default 50).
            offset: Pagination offset.

        Returns:
            JSON list of escrow deals with summary details.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.escrow_list(
            role=role,
            status=status,
            limit=limit,
            offset=offset,
        )
        return json.dumps(result, indent=2)
