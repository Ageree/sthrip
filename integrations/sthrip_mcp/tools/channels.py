"""Payment channel, subscription, and streaming tools."""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_channel_tools(mcp: FastMCP, client: SthripClient) -> None:
    """Register payment channel, subscription, and streaming tools."""

    @mcp.tool()
    async def channel_open(
        counterparty_agent_name: str,
        deposit_amount: float,
        settle_timeout_hours: int = 24,
    ) -> str:
        """Open a bi-directional payment channel with another agent.

        Payment channels allow fast, off-chain micro-payments between
        two agents. Funds are locked on open, payments flow instantly
        within the channel, and the final balance is settled on close.

        Requires authentication (API key).

        Args:
            counterparty_agent_name: Name of the other agent in the channel.
            deposit_amount: XMR to lock as the channel deposit (your side).
            settle_timeout_hours: Hours for the settlement period when
                closing the channel (default 24). Both parties can
                dispute during this window.

        Returns:
            JSON with channel_id, status, deposit details, counterparty
            info, and settle timeout.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.channel_open(
            counterparty_agent_name=counterparty_agent_name,
            deposit_amount=deposit_amount,
            settle_timeout_hours=settle_timeout_hours,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def channel_settle(
        channel_id: str,
        amount: float,
    ) -> str:
        """Send an off-chain payment through a payment channel.

        Instantly transfers value within an open channel. No on-chain
        transaction is needed until the channel is closed. The channel
        balance is updated immediately for both parties.

        Requires authentication (API key).

        Args:
            channel_id: The payment channel UUID.
            amount: XMR amount to transfer to the counterparty.
                Must not exceed your remaining channel balance.

        Returns:
            JSON with updated channel balances, transfer confirmation,
            and nonce (sequence number).
        """
        require_auth(client.api_key or load_api_key())
        result = await client.channel_settle(
            channel_id=channel_id,
            amount=amount,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def channel_close(channel_id: str) -> str:
        """Close a payment channel and settle the final balance.

        Initiates the closing process. After the settle timeout,
        the final balances are distributed to both parties. Either
        party can close the channel.

        Requires authentication (API key).

        Args:
            channel_id: The payment channel UUID to close.

        Returns:
            JSON with final balances, settlement status, and
            expected payout timestamps.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.channel_close(channel_id)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def subscribe(
        to_agent_name: str,
        amount: float,
        interval_hours: int,
        max_payments: Optional[int] = None,
        memo: Optional[str] = None,
    ) -> str:
        """Create a recurring payment subscription to an agent.

        Automatically sends XMR at a fixed interval. Useful for
        ongoing services like monitoring, data feeds, or API access.
        Payments continue until cancelled or max_payments is reached.

        Requires authentication (API key).

        Args:
            to_agent_name: Recipient agent's name.
            amount: XMR amount per payment cycle.
            interval_hours: Hours between payments (e.g., 24 for daily,
                168 for weekly, 720 for monthly).
            max_payments: Optional cap on total number of payments.
                Subscription auto-cancels after this many payments.
                None for unlimited.
            memo: Optional memo attached to each payment (max 500 chars).

        Returns:
            JSON with subscription_id, status, schedule details, next
            payment timestamp, and total committed amount.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.subscribe(
            to_agent_name=to_agent_name,
            amount=amount,
            interval_hours=interval_hours,
            max_payments=max_payments,
            memo=memo,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def unsubscribe(subscription_id: str) -> str:
        """Cancel a recurring payment subscription.

        Stops all future payments. Already-sent payments are not
        refunded. The subscription enters 'cancelled' status.

        Requires authentication (API key).

        Args:
            subscription_id: The subscription UUID to cancel.

        Returns:
            JSON with cancellation confirmation, total payments made,
            total XMR sent, and final status.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.unsubscribe(subscription_id)
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def stream_start(
        to_agent_name: str,
        rate_per_hour: float,
        max_duration_hours: Optional[float] = None,
        memo: Optional[str] = None,
    ) -> str:
        """Start a continuous payment stream to an agent.

        Streams XMR continuously at a fixed rate per hour. Useful
        for pay-as-you-go services, compute time, or real-time
        data access. The stream runs until stopped or the max
        duration is reached.

        Requires authentication (API key).

        Args:
            to_agent_name: Recipient agent's name.
            rate_per_hour: XMR per hour to stream.
            max_duration_hours: Optional maximum stream duration in
                hours. Stream auto-stops after this time. None for
                unlimited (must be stopped manually).
            memo: Optional memo for the stream (max 500 chars).

        Returns:
            JSON with stream_id, status, rate, start time, estimated
            max cost, and recipient info.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.stream_start(
            to_agent_name=to_agent_name,
            rate_per_hour=rate_per_hour,
            max_duration_hours=max_duration_hours,
            memo=memo,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def stream_stop(stream_id: str) -> str:
        """Stop an active payment stream.

        Halts the continuous payment. The final amount is calculated
        based on elapsed time and the rate. Any excess reserved funds
        are returned to the sender's balance.

        Requires authentication (API key).

        Args:
            stream_id: The stream UUID to stop.

        Returns:
            JSON with final stream details: total elapsed time, total
            XMR streamed, refund amount (if any), and final status.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.stream_stop(stream_id)
        return json.dumps(result, indent=2)
