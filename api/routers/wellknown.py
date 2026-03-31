"""Well-known discovery endpoint for agent payment protocol."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["discovery"])

# Frozen discovery payload — returned verbatim on every request.
# Defined as a module-level constant so the dict is never mutated at runtime.
_AGENT_PAYMENTS_DISCOVERY = {
    "service": "sthrip",
    "version": "3.0.0",
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
        "spending_policy": "/v2/me/spending-policy",
        "messages": "/v2/messages",
        "reputation": "/v2/me/reputation-proof",
        "webhooks": "/v2/webhook-endpoints",
    },
    "capabilities": [
        "hub-routing",
        "escrow",
        "multisig-escrow",
        "webhooks",
        "mcp-server",
        "spending-policies",
        "encrypted-messaging",
        "zk-reputation",
        "pow-registration",
    ],
    "supported_tokens": ["XMR"],
    "fee_percent": "1",
    "fees": {
        "hub_routing": "1%",
        "escrow": "1%",
        "multisig_escrow": "1% upfront",
    },
    "min_confirmations": 10,
    "install": "pip install sthrip",
    "spending_policies": {
        "supported": True,
        "endpoints": {
            "set": "PUT /v2/me/spending-policy",
            "get": "GET /v2/me/spending-policy",
        },
    },
    "encrypted_messaging": {
        "supported": True,
        "protocol": "NaCl Box (Curve25519 + XSalsa20-Poly1305)",
        "max_message_size": 65536,
        "message_ttl_hours": 24,
        "endpoints": {
            "register_key": "PUT /v2/me/encryption-key",
            "get_key": "GET /v2/agents/{id}/public-key",
            "send": "POST /v2/messages/send",
            "inbox": "GET /v2/messages/inbox",
        },
    },
    "multisig_escrow": {
        "supported": True,
        "type": "2-of-3 Monero multisig",
        "fee": "1% upfront",
    },
    "pow_registration": {
        "supported": True,
        "algorithm": "sha256",
        "difficulty_bits": 20,
        "endpoint": "POST /v2/agents/register/challenge",
    },
    "zk_reputation": {
        "supported": True,
        "endpoints": {
            "generate": "POST /v2/me/reputation-proof",
            "verify": "POST /v2/verify-reputation",
        },
    },
    "webhook_registration": {
        "supported": True,
        "max_per_agent": 10,
        "signing": "Standard Webhooks (HMAC-SHA256)",
        "endpoints": {
            "register": "POST /v2/webhook-endpoints",
            "list": "GET /v2/webhook-endpoints",
            "rotate": "POST /v2/webhook-endpoints/{id}/rotate",
        },
    },
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
