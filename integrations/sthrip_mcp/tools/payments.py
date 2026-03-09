"""Payment tools — send, check status, view history."""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_payment_tools(mcp: FastMCP, client: SthripClient) -> None:
    """Register payment tools on the MCP server."""

    @mcp.tool()
    async def send_payment(
        to_agent_name: str,
        amount: float,
        memo: Optional[str] = None,
        urgency: str = "normal",
    ) -> str:
        """Send XMR to another agent via hub routing.

        Requires authentication (API key).

        Args:
            to_agent_name: Recipient agent's name.
            amount: Amount in XMR (max 10,000).
            memo: Optional private memo (max 500 chars).
            urgency: 'normal' or 'urgent' (urgent has higher fee).

        Returns:
            JSON with payment_id, status, amount, fee, and recipient info.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.send_payment(
            to_agent_name=to_agent_name,
            amount=amount,
            memo=memo,
            urgency=urgency,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def get_payment_status(payment_id: str) -> str:
        """Check the status of a specific payment.

        Requires authentication (API key).

        Args:
            payment_id: The payment UUID to look up.

        Returns:
            JSON with payment details: status, amount, fee, timestamps.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.get_payment_status(payment_id)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def get_payment_history(
        direction: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Get payment transaction history.

        Requires authentication (API key).

        Args:
            direction: Filter by 'in' (received) or 'out' (sent). None for all.
            limit: Maximum results (default 20).
            offset: Pagination offset.

        Returns:
            JSON list of payment transactions.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.get_payment_history(
            direction=direction,
            limit=limit,
            offset=offset,
        )
        return json.dumps(result, indent=2)
