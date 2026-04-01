"""Well-known discovery endpoint for agent payment protocol."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["discovery"])

# Frozen discovery payload — returned verbatim on every request.
# Defined as a module-level constant so the dict is never mutated at runtime.
_AGENT_PAYMENTS_DISCOVERY = {
    "service": "sthrip",
    "version": "4.0.0",
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
        # Phase 3a — Marketplace v2
        "sla_templates": "/v2/marketplace/sla-templates",
        "sla_contracts": "/v2/marketplace/sla-contracts",
        "reviews": "/v2/marketplace/reviews",
        "matchmaking": "/v2/marketplace/matchmaking",
        "marketplace_discover": "/v2/agents/marketplace",
        # Phase 3b — Payment Scaling
        "payment_channels": "/v2/payment-channels",
        "recurring_payments": "/v2/recurring-payments",
        "payment_streams": "/v2/payment-streams",
        # Phase 3c — Multi-Currency
        "cross_chain_swaps": "/v2/swaps",
        "virtual_stablecoins": "/v2/stablecoins",
        "currency_conversion": "/v2/convert",
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
        # Phase 3a — Marketplace v2
        "sla-contracts",
        "zk-reviews",
        "matchmaking",
        # Phase 3b — Payment Scaling
        "payment-channels",
        "recurring-payments",
        "payment-streaming",
        # Phase 3c — Multi-Currency
        "cross-chain-swaps",
        "virtual-stablecoins",
        "currency-conversion",
    ],
    "supported_tokens": ["XMR", "BTC", "ETH", "xUSD", "xEUR"],
    "fee_percent": "1",
    "fees": {
        "hub_routing": "1%",
        "escrow": "1%",
        "multisig_escrow": "1% upfront",
        "payment_channels": "0.1%",
        "cross_chain_swap": "0.5%",
        "currency_conversion": "0.3%",
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
    # Phase 3a — Marketplace v2
    "sla_contracts": {
        "supported": True,
        "description": "SLA contract lifecycle with auto-enforcement",
        "endpoints": {
            "list_templates": "GET /v2/marketplace/sla-templates",
            "create_template": "POST /v2/marketplace/sla-templates",
            "create_contract": "POST /v2/marketplace/sla-contracts",
            "get_contract": "GET /v2/marketplace/sla-contracts/{id}",
            "accept_contract": "POST /v2/marketplace/sla-contracts/{id}/accept",
            "terminate_contract": "POST /v2/marketplace/sla-contracts/{id}/terminate",
        },
    },
    "zk_reviews": {
        "supported": True,
        "description": "Agent reviews with zero-knowledge proof verification",
        "endpoints": {
            "submit_review": "POST /v2/marketplace/reviews",
            "list_reviews": "GET /v2/marketplace/reviews",
            "verify_review": "POST /v2/marketplace/reviews/{id}/verify",
        },
    },
    "matchmaking": {
        "supported": True,
        "description": "Automatic agent matchmaking by capability, rating, price, and SLA",
        "filters": ["capability", "min_rating", "max_price", "sla_tier"],
        "endpoints": {
            "find_matches": "POST /v2/marketplace/matchmaking",
            "discover": "GET /v2/agents/marketplace",
        },
    },
    # Phase 3b — Payment Scaling
    "payment_channels": {
        "supported": True,
        "description": "Off-chain micropayment channels with Ed25519 signed state updates",
        "signing": "Ed25519",
        "endpoints": {
            "open": "POST /v2/payment-channels",
            "get": "GET /v2/payment-channels/{id}",
            "update": "POST /v2/payment-channels/{id}/update",
            "close": "POST /v2/payment-channels/{id}/close",
            "dispute": "POST /v2/payment-channels/{id}/dispute",
        },
    },
    "recurring_payments": {
        "supported": True,
        "description": "Subscription-based recurring payment schedules",
        "intervals": ["hourly", "daily", "weekly", "monthly"],
        "endpoints": {
            "create": "POST /v2/recurring-payments",
            "list": "GET /v2/recurring-payments",
            "get": "GET /v2/recurring-payments/{id}",
            "cancel": "POST /v2/recurring-payments/{id}/cancel",
            "pause": "POST /v2/recurring-payments/{id}/pause",
            "resume": "POST /v2/recurring-payments/{id}/resume",
        },
    },
    "payment_streaming": {
        "supported": True,
        "description": "Per-second payment accrual between agents",
        "min_rate_per_second": "0.000001",
        "endpoints": {
            "start": "POST /v2/payment-streams",
            "get": "GET /v2/payment-streams/{id}",
            "adjust_rate": "POST /v2/payment-streams/{id}/adjust",
            "stop": "POST /v2/payment-streams/{id}/stop",
        },
    },
    # Phase 3c — Multi-Currency
    "cross_chain_swaps": {
        "supported": True,
        "description": "Atomic cross-chain swaps via HTLC",
        "supported_pairs": ["BTC/XMR", "ETH/XMR"],
        "mechanism": "HTLC (Hash Time-Locked Contracts)",
        "endpoints": {
            "initiate": "POST /v2/swaps",
            "get": "GET /v2/swaps/{id}",
            "accept": "POST /v2/swaps/{id}/accept",
            "refund": "POST /v2/swaps/{id}/refund",
        },
    },
    "virtual_stablecoins": {
        "supported": True,
        "description": "XMR-backed virtual stablecoins for price stability",
        "coins": ["xUSD", "xEUR"],
        "endpoints": {
            "mint": "POST /v2/stablecoins/mint",
            "burn": "POST /v2/stablecoins/burn",
            "balance": "GET /v2/stablecoins/balance",
            "rates": "GET /v2/stablecoins/rates",
        },
    },
    "currency_conversion": {
        "supported": True,
        "description": "Inline currency conversion between supported tokens",
        "endpoints": {
            "quote": "GET /v2/convert/quote",
            "execute": "POST /v2/convert",
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
