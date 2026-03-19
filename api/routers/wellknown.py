"""Well-known discovery endpoint for agent payment protocol."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["discovery"])

# Frozen discovery payload — returned verbatim on every request.
# Defined as a module-level constant so the dict is never mutated at runtime.
_AGENT_PAYMENTS_DISCOVERY = {
    "service": "sthrip",
    "version": "2.0.0",
    "description": "Anonymous payments for AI agents",
    "api_url": "https://sthrip-api-production.up.railway.app",
    "docs_url": "https://sthrip-api-production.up.railway.app/docs",
    "endpoints": {
        "register": "/v2/agents/register",
        "payments": "/v2/payments/hub-routing",
        "balance": "/v2/balance",
        "deposit": "/v2/balance/deposit",
        "agents": "/v2/agents",
        "escrow": "/v2/escrow",
    },
    "capabilities": [
        "hub-routing",
        "escrow",
        "webhooks",
        "mcp-server",
    ],
    "supported_tokens": ["XMR"],
    "fee_percent": "0.1",
    "min_confirmations": 10,
    "install": "pip install sthrip",
}


@router.get("/.well-known/agent-payments.json", response_model=dict)
async def agent_payments_discovery():
    """Public discovery document for the agent-payments protocol.

    Returns a static JSON manifest that clients and other agents can use
    to discover the Sthrip API capabilities, endpoints, and connection
    details without any authentication.
    """
    return JSONResponse(
        content=_AGENT_PAYMENTS_DISCOVERY,
        media_type="application/json",
    )
