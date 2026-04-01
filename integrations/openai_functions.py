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
    # -- Phase 3a: SLA, Reviews, Matchmaking --
    {
        "type": "function",
        "function": {
            "name": "sthrip_sla_template_create",
            "description": (
                "Create an SLA service template defining deliverables, response times, "
                "delivery times, base price, and penalty terms."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short name for this SLA template.",
                    },
                    "deliverables": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of deliverable identifiers promised under this SLA.",
                    },
                    "response_time_secs": {
                        "type": "integer",
                        "description": "Maximum response time in seconds.",
                    },
                    "delivery_time_secs": {
                        "type": "integer",
                        "description": "Maximum delivery time in seconds.",
                    },
                    "base_price": {
                        "type": "number",
                        "description": "Baseline price in XMR.",
                    },
                    "penalty_percent": {
                        "type": "integer",
                        "description": "Penalty percentage on SLA breach.",
                        "default": 10,
                    },
                    "service_description": {
                        "type": "string",
                        "description": "Human-readable description of the service.",
                    },
                },
                "required": [
                    "name",
                    "deliverables",
                    "response_time_secs",
                    "delivery_time_secs",
                    "base_price",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_sla_create",
            "description": (
                "Create an SLA contract with a provider agent, optionally referencing "
                "an existing SLA template and agreed price."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Registered name of the provider agent.",
                    },
                    "template_id": {
                        "type": "string",
                        "description": "UUID of the SLA template to base the contract on.",
                    },
                    "price": {
                        "type": "number",
                        "description": "Agreed price in XMR.",
                    },
                },
                "required": ["provider"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_review_agent",
            "description": (
                "Submit a review for an agent based on a completed payment or escrow. "
                "Rating is 1-5."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "UUID of the agent being reviewed.",
                    },
                    "transaction_id": {
                        "type": "string",
                        "description": "UUID of the payment or escrow associated with the review.",
                    },
                    "transaction_type": {
                        "type": "string",
                        "enum": ["payment", "escrow"],
                        "description": "Type of transaction: 'payment' or 'escrow'.",
                    },
                    "overall_rating": {
                        "type": "integer",
                        "description": "Integer rating from 1 to 5.",
                        "minimum": 1,
                        "maximum": 5,
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional text comment.",
                    },
                },
                "required": [
                    "agent_id",
                    "transaction_id",
                    "transaction_type",
                    "overall_rating",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_matchmake",
            "description": (
                "Submit a matchmaking request to automatically find the best agent "
                "for a task given required capabilities, budget, and deadline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required capabilities, e.g. ['translation', 'proofreading'].",
                    },
                    "budget": {
                        "type": "number",
                        "description": "Maximum budget in XMR.",
                    },
                    "deadline_secs": {
                        "type": "integer",
                        "description": "Deadline in seconds from now.",
                    },
                    "min_rating": {
                        "type": "number",
                        "description": "Minimum acceptable average rating (0-5).",
                        "default": 0,
                    },
                    "auto_assign": {
                        "type": "boolean",
                        "description": "Automatically assign best match when true.",
                        "default": False,
                    },
                },
                "required": ["capabilities", "budget", "deadline_secs"],
                "additionalProperties": False,
            },
        },
    },
    # -- Phase 3b: Channels, Subscriptions, Streams --
    {
        "type": "function",
        "function": {
            "name": "sthrip_channel_open",
            "description": (
                "Open a bidirectional payment channel with another agent by depositing XMR. "
                "Enables high-frequency micropayments without on-chain fees."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Registered name of the counterparty agent.",
                    },
                    "deposit": {
                        "type": "number",
                        "description": "Amount in XMR to deposit into the channel.",
                    },
                    "settlement_period": {
                        "type": "integer",
                        "description": "Settlement window in seconds.",
                        "default": 3600,
                    },
                },
                "required": ["agent_name", "deposit"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_subscribe",
            "description": (
                "Set up a recurring XMR payment to another agent at a fixed interval. "
                "Optionally limit the total number of payments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "Registered name of the recipient agent.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Amount in XMR per payment cycle.",
                    },
                    "interval": {
                        "type": "integer",
                        "description": "Interval in seconds between payments.",
                    },
                    "max_payments": {
                        "type": "integer",
                        "description": "Maximum number of payments before expiry.",
                    },
                },
                "required": ["to_agent", "amount", "interval"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_stream_start",
            "description": (
                "Start streaming XMR payments at a specified rate per second "
                "over an existing payment channel."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {
                        "type": "string",
                        "description": "UUID of the payment channel to stream over.",
                    },
                    "rate_per_second": {
                        "type": "number",
                        "description": "Amount in XMR per second.",
                    },
                },
                "required": ["channel_id", "rate_per_second"],
                "additionalProperties": False,
            },
        },
    },
    # -- Phase 3c: Swap & Conversion --
    {
        "type": "function",
        "function": {
            "name": "sthrip_swap_rates",
            "description": "Get current exchange rates for all supported swap pairs (e.g. XMR/USD, XMR/EUR).",
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
            "name": "sthrip_swap",
            "description": "Execute an on-chain swap from one currency to XMR.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_currency": {
                        "type": "string",
                        "description": "Currency to swap from, e.g. 'xUSD'.",
                    },
                    "from_amount": {
                        "type": "number",
                        "description": "Amount to swap.",
                    },
                },
                "required": ["from_currency", "from_amount"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sthrip_convert",
            "description": (
                "Convert between hub-balance currencies such as XMR, xUSD, and xEUR. "
                "Returns the converted amount, rate, and fee."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_currency": {
                        "type": "string",
                        "description": "Source currency, e.g. 'XMR'.",
                    },
                    "to_currency": {
                        "type": "string",
                        "description": "Target currency, e.g. 'xUSD'.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "Amount to convert.",
                    },
                },
                "required": ["from_currency", "to_currency", "amount"],
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
        # Phase 3a: SLA, Reviews, Matchmaking
        "sthrip_sla_template_create": lambda: client.sla_template_create(
            name=args["name"],
            deliverables=args["deliverables"],
            response_time_secs=args["response_time_secs"],
            delivery_time_secs=args["delivery_time_secs"],
            base_price=args["base_price"],
            penalty_percent=args.get("penalty_percent", 10),
            service_description=args.get("service_description", ""),
        ),
        "sthrip_sla_create": lambda: client.sla_create(
            provider=args["provider"],
            template_id=args.get("template_id"),
            price=args.get("price"),
        ),
        "sthrip_review_agent": lambda: client.review(
            agent_id=args["agent_id"],
            transaction_id=args["transaction_id"],
            transaction_type=args["transaction_type"],
            overall_rating=args["overall_rating"],
            **({"comment": args["comment"]} if args.get("comment") else {}),
        ),
        "sthrip_matchmake": lambda: client.matchmake(
            capabilities=args["capabilities"],
            budget=args["budget"],
            deadline_secs=args["deadline_secs"],
            min_rating=args.get("min_rating", 0),
            auto_assign=args.get("auto_assign", False),
        ),
        # Phase 3b: Channels, Subscriptions, Streams
        "sthrip_channel_open": lambda: client.channel_open(
            agent_name=args["agent_name"],
            deposit=args["deposit"],
            settlement_period=args.get("settlement_period", 3600),
        ),
        "sthrip_subscribe": lambda: client.subscribe(
            to_agent=args["to_agent"],
            amount=args["amount"],
            interval=args["interval"],
            max_payments=args.get("max_payments"),
        ),
        "sthrip_stream_start": lambda: client.stream_start(
            channel_id=args["channel_id"],
            rate_per_second=args["rate_per_second"],
        ),
        # Phase 3c: Swap & Conversion
        "sthrip_swap_rates": lambda: client.swap_rates(),
        "sthrip_swap": lambda: client.swap(
            from_currency=args["from_currency"],
            from_amount=args["from_amount"],
        ),
        "sthrip_convert": lambda: client.convert(
            from_currency=args["from_currency"],
            to_currency=args["to_currency"],
            amount=args["amount"],
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
