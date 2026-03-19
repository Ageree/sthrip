"""Sthrip OpenAI function-calling integration.

Provides function definitions and a dispatcher for OpenAI's function calling
format, enabling GPT-based agents to make anonymous payments.

Usage::

    import openai
    from integrations.openai_functions import STHRIP_FUNCTIONS, handle_sthrip_function

    response = openai.chat.completions.create(
        model="gpt-4",
        messages=[...],
        tools=STHRIP_FUNCTIONS,
    )

    # When the model calls a Sthrip function:
    tool_call = response.choices[0].message.tool_calls[0]
    result = handle_sthrip_function(
        tool_call.function.name,
        json.loads(tool_call.function.arguments),
    )

Requires: pip install sthrip  (no OpenAI dependency needed for definitions)
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sthrip import Sthrip


# ---------------------------------------------------------------------------
# Function definitions (OpenAI tools format)
# ---------------------------------------------------------------------------

STHRIP_FUNCTIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "sthrip_pay",
            "description": "Send an anonymous XMR (Monero) payment to another AI agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Recipient agent's registered name.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Amount in XMR to send.",
                    },
                    "memo": {
                        "type": "string",
                        "description": "Optional note attached to the payment.",
                    },
                },
                "required": ["agent_name", "amount"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_balance",
            "description": (
                "Check the current XMR balance of the authenticated agent, "
                "including available, pending, and deposit address."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_escrow_create",
            "description": (
                "Create an escrow holding XMR for a seller agent. "
                "Funds are locked until delivery and buyer approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seller_agent_name": {
                        "type": "string",
                        "description": "Seller agent's registered name.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Escrow amount in XMR.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Description of work to be performed.",
                    },
                    "delivery_hours": {
                        "type": "integer",
                        "description": "Hours the seller has to deliver after accepting.",
                        "default": 48,
                    },
                    "review_hours": {
                        "type": "integer",
                        "description": "Hours the buyer has to review after delivery.",
                        "default": 24,
                    },
                    "accept_hours": {
                        "type": "integer",
                        "description": "Hours the seller has to accept the escrow.",
                        "default": 24,
                    },
                },
                "required": ["seller_agent_name", "amount"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_escrow_accept",
            "description": "Accept an incoming escrow as the seller, committing to deliver work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "escrow_id": {
                        "type": "string",
                        "description": "UUID of the escrow to accept.",
                    },
                },
                "required": ["escrow_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_escrow_deliver",
            "description": "Mark escrow work as delivered (seller signals completion).",
            "parameters": {
                "type": "object",
                "properties": {
                    "escrow_id": {
                        "type": "string",
                        "description": "UUID of the escrow to mark as delivered.",
                    },
                },
                "required": ["escrow_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_escrow_release",
            "description": (
                "Release escrowed XMR to the seller. "
                "Set amount to 0 for full refund, or escrow amount for full release."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "escrow_id": {
                        "type": "string",
                        "description": "UUID of the escrow.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Amount to release to seller (0 = full refund).",
                    },
                },
                "required": ["escrow_id", "amount"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_find_agents",
            "description": "Discover AI agents registered on the Sthrip payment network.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max number of agents to return.",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset.",
                        "default": 0,
                    },
                    "min_trust_score": {
                        "type": "number",
                        "description": "Minimum trust score filter.",
                    },
                    "verified_only": {
                        "type": "boolean",
                        "description": "Only return verified agents.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_escrow_list",
            "description": "List escrows the agent is involved in, optionally filtered by role or status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "enum": ["buyer", "seller"],
                        "description": "Filter by role: 'buyer' or 'seller'.",
                    },
                    "status": {
                        "type": "string",
                        "enum": [
                            "created",
                            "accepted",
                            "delivered",
                            "completed",
                            "cancelled",
                            "expired",
                        ],
                        "description": "Filter by escrow status.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results.",
                        "default": 50,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Pagination offset.",
                        "default": 0,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _json_result(data: Any) -> str:
    """Serialize to compact JSON for LLM consumption."""
    return json.dumps(data, default=str)


def handle_sthrip_function(
    name: str,
    args: dict[str, Any],
    api_key: Optional[str] = None,
) -> str:
    """Execute a Sthrip function by name and return a JSON string result.

    Parameters
    ----------
    name : str
        Function name from the tool call (e.g. ``"sthrip_pay"``).
    args : dict
        Parsed arguments from the function call.
    api_key : str, optional
        Explicit API key.  Falls back to env / credentials / auto-register.

    Returns
    -------
    str
        JSON-encoded result or error.
    """
    client = Sthrip(api_key=api_key)

    handlers: dict[str, Any] = {
        "sthrip_pay": lambda: client.pay(
            agent_name=args["agent_name"],
            amount=args["amount"],
            memo=args.get("memo"),
        ),
        "sthrip_balance": lambda: client.balance(),
        "sthrip_escrow_create": lambda: client.escrow_create(
            seller_agent_name=args["seller_agent_name"],
            amount=args["amount"],
            description=args.get("description", ""),
            delivery_hours=args.get("delivery_hours", 48),
            review_hours=args.get("review_hours", 24),
            accept_hours=args.get("accept_hours", 24),
        ),
        "sthrip_escrow_accept": lambda: client.escrow_accept(args["escrow_id"]),
        "sthrip_escrow_deliver": lambda: client.escrow_deliver(args["escrow_id"]),
        "sthrip_escrow_release": lambda: client.escrow_release(
            escrow_id=args["escrow_id"],
            amount=args["amount"],
        ),
        "sthrip_find_agents": lambda: client.find_agents(
            limit=args.get("limit", 20),
            offset=args.get("offset", 0),
            min_trust_score=args.get("min_trust_score"),
            verified_only=args.get("verified_only"),
        ),
        "sthrip_escrow_list": lambda: client.escrow_list(
            role=args.get("role"),
            status=args.get("status"),
            limit=args.get("limit", 50),
            offset=args.get("offset", 0),
        ),
    }

    handler = handlers.get(name)
    if handler is None:
        return _json_result({"error": "Unknown function: {}".format(name)})

    try:
        result = handler()
        return _json_result(result)
    except Exception as exc:
        return _json_result({"error": str(exc)})
