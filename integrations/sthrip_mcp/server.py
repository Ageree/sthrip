"""MCP server setup — creates FastMCP instance, registers all 46 tools."""

import os

from mcp.server.fastmcp import FastMCP

from .auth import load_api_key
from .client import SthripClient
from .tools.balance import register_balance_tools
from .tools.channels import register_channel_tools
from .tools.cross_chain import register_cross_chain_tools
from .tools.discovery import register_discovery_tools
from .tools.escrow import register_escrow_tools
from .tools.messaging import register_messaging_tools
from .tools.payments import register_payment_tools
from .tools.pow import register_pow_tools
from .tools.registration import register_registration_tools
from .tools.reputation import register_reputation_tools
from .tools.sla import register_sla_tools
from .tools.spending_policy import register_spending_policy_tools

DEFAULT_API_URL = "https://sthrip-api-production.up.railway.app"


def create_server() -> FastMCP:
    """Create and configure the Sthrip MCP server.

    Reads configuration from environment:
      - STHRIP_API_URL: API base URL (default: production)
      - STHRIP_API_KEY: optional API key (also checked in ~/.sthrip/)
    """
    api_url = os.environ.get("STHRIP_API_URL", DEFAULT_API_URL)
    api_key = load_api_key()

    client = SthripClient(base_url=api_url, api_key=api_key)

    mcp = FastMCP(
        "Sthrip",
        instructions=(
            "Sthrip enables AI agents to discover other agents, "
            "make anonymous XMR payments, create escrow deals, "
            "exchange encrypted messages, set spending policies, "
            "generate ZK reputation proofs, manage SLA contracts, "
            "open payment channels, stream payments, subscribe to "
            "services, and swap between currencies. "
            "Discovery, matchmaking, rates, and verification tools "
            "work without authentication. "
            "Payment, balance, escrow, SLA, channel, subscription, "
            "streaming, swap, messaging, spending policy, and "
            "reputation tools require an API key "
            "(set STHRIP_API_KEY or use the register_agent tool)."
        ),
    )

    register_discovery_tools(mcp, client)
    register_registration_tools(mcp, client)
    register_payment_tools(mcp, client)
    register_balance_tools(mcp, client)
    register_escrow_tools(mcp, client)
    register_spending_policy_tools(mcp, client)
    register_messaging_tools(mcp, client)
    register_reputation_tools(mcp, client)
    register_pow_tools(mcp, client)
    register_sla_tools(mcp, client)
    register_channel_tools(mcp, client)
    register_cross_chain_tools(mcp, client)

    return mcp
