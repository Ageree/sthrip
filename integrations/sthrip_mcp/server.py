"""MCP server setup — creates FastMCP instance, registers all tools."""

import os

from mcp.server.fastmcp import FastMCP

from .auth import load_api_key
from .client import SthripClient
from .tools.balance import register_balance_tools
from .tools.discovery import register_discovery_tools
from .tools.payments import register_payment_tools
from .tools.registration import register_registration_tools

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
            "Sthrip enables AI agents to discover other agents and "
            "make anonymous XMR payments. Discovery tools work without "
            "authentication. Payment and balance tools require an API key "
            "(set STHRIP_API_KEY or use the register_agent tool)."
        ),
    )

    register_discovery_tools(mcp, client)
    register_registration_tools(mcp, client)
    register_payment_tools(mcp, client)
    register_balance_tools(mcp, client)

    return mcp
