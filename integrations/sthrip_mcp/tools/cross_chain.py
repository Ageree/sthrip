"""Cross-chain tools — swap rates, quotes, swaps, conversions, multi-currency balances."""

import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from ..auth import load_api_key, require_auth
from ..client import SthripClient


def register_cross_chain_tools(mcp: FastMCP, client: SthripClient) -> None:
    """Register cross-chain swap and multi-currency tools."""

    @mcp.tool()
    async def swap_rates(
        from_currency: str = "XMR",
        to_currency: Optional[str] = None,
    ) -> str:
        """Get current exchange rates for cross-chain swaps.

        Returns live rates from integrated DEX aggregators. Rates
        are indicative and may vary at execution time. No
        authentication required.

        Args:
            from_currency: Source currency ticker (default 'XMR').
                Supported: XMR, BTC, ETH, USDT, USDC, SOL.
            to_currency: Target currency ticker. If None, returns
                rates for all supported pairs from the source currency.

        Returns:
            JSON with exchange rates, spread, 24h change, and
            available liquidity for each pair.
        """
        result = await client.swap_rates(
            from_currency=from_currency,
            to_currency=to_currency,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def swap_quote(
        from_currency: str,
        to_currency: str,
        amount: float,
    ) -> str:
        """Get a binding quote for a cross-chain swap.

        Returns a firm quote valid for 60 seconds. The quote
        includes exact output amount, fees, and slippage. No
        authentication required for quotes.

        Args:
            from_currency: Source currency ticker (e.g., 'XMR').
            to_currency: Target currency ticker (e.g., 'BTC').
            amount: Amount of source currency to swap.

        Returns:
            JSON with quote_id, input amount, output amount, rate,
            fee breakdown, estimated time, and expiry timestamp.
            Use the quote_id to execute the swap.
        """
        result = await client.swap_quote(
            from_currency=from_currency,
            to_currency=to_currency,
            amount=amount,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def swap(
        from_currency: str,
        to_currency: str,
        amount: float,
        max_slippage_bps: int = 50,
    ) -> str:
        """Execute a cross-chain atomic swap.

        Swaps between supported currencies using atomic swap
        protocols. Funds are deducted from the source currency
        balance and credited to the target currency balance.

        Requires authentication (API key).

        Args:
            from_currency: Source currency ticker (e.g., 'XMR').
            to_currency: Target currency ticker (e.g., 'BTC').
            amount: Amount of source currency to swap.
            max_slippage_bps: Maximum acceptable slippage in basis
                points (default 50 = 0.5%). Swap fails if slippage
                exceeds this limit.

        Returns:
            JSON with swap_id, input/output amounts, actual rate,
            fees, status, and settlement timestamps.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.swap(
            from_currency=from_currency,
            to_currency=to_currency,
            amount=amount,
            max_slippage_bps=max_slippage_bps,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def convert(
        from_currency: str,
        to_currency: str,
        amount: float,
    ) -> str:
        """Convert between currencies within the Sthrip hub.

        Instant conversion at current market rates. Faster than
        atomic swaps because it uses hub-held liquidity pools.
        Lower fees but requires trusting the hub.

        Requires authentication (API key).

        Args:
            from_currency: Source currency ticker (e.g., 'XMR').
            to_currency: Target currency ticker (e.g., 'USDT').
            amount: Amount of source currency to convert.

        Returns:
            JSON with conversion_id, input/output amounts, rate,
            fee, and updated balances.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.convert(
            from_currency=from_currency,
            to_currency=to_currency,
            amount=amount,
        )
        return json.dumps(result, indent=2)

    @mcp.tool()
    async def balances_all() -> str:
        """Get balances for all supported currencies.

        Returns the agent's balance across all currency types,
        including XMR, BTC, ETH, USDT, USDC, and SOL. Each
        balance shows available, pending, and locked amounts.

        Requires authentication (API key).

        Returns:
            JSON dict keyed by currency ticker, each with available,
            pending, locked, and total fields. Also includes a
            total_value_xmr field showing the aggregate portfolio
            value denominated in XMR.
        """
        require_auth(client.api_key or load_api_key())
        result = await client.balances_all()
        return json.dumps(result, indent=2)
