"""Custom API documentation endpoints.

Serves branded Redoc at /docs, Swagger UI at /docs/playground,
and OpenAPI schema at /openapi.json — available in all environments.
"""

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse


# ═══════════════════════════════════════════════════════════════════════════════
# OPENAPI SCHEMA ENRICHMENT
# ═══════════════════════════════════════════════════════════════════════════════

_DESCRIPTION = """\
# Sthrip API

**Production-ready anonymous payments for AI Agents** powered by Monero.

Sthrip enables AI agents to send and receive private payments without
revealing their identity or transaction history.

---

## Getting Started

### 1. Register your agent

```
POST /v2/agents/register
{
  "agent_name": "my-agent",
  "webhook_url": "https://example.com/webhook"
}
```

Save the `api_key` from the response — it is shown only once.

### 2. Authenticate

Include your API key as a Bearer token in all requests:

```
Authorization: Bearer <your-api-key>
```

### 3. Check your balance

```
GET /v2/balance
Authorization: Bearer <your-api-key>
```

### 4. Deposit XMR

```
POST /v2/balance/deposit
Authorization: Bearer <your-api-key>
```

In **onchain mode**, you'll receive a deposit address.
In **ledger mode** (testing), specify an amount to auto-credit.

### 5. Send a payment

```
POST /v2/payments/hub-routing
Authorization: Bearer <your-api-key>
{
  "to_agent_name": "recipient-agent",
  "amount": 0.1,
  "memo": "Payment for service"
}
```

### 6. MCP Server (for AI tool use)

Sthrip ships an MCP server for seamless integration with AI agents:

```bash
# Install and run
PYTHONPATH=. python -m integrations.sthrip_mcp

# Or with SSE transport
PYTHONPATH=. python -m integrations.sthrip_mcp --sse
```

The MCP server provides 12 tools: discovery (3), registration (3),
payments (3), and balance (3).

---

## Authentication

All authenticated endpoints require a **Bearer token**:

```
Authorization: Bearer <api-key>
```

API keys are generated during agent registration and shown only once.
Use `POST /v2/me/rotate-key` to generate a new key.

## Rate Limits

Rate limits are enforced per agent tier:

| Tier | Requests/min | Burst |
|------|-------------|-------|
| free | 30 | 10 |
| verified | 60 | 20 |
| premium | 120 | 40 |
| enterprise | 300 | 100 |

Rate limit headers are included in all responses:
- `X-RateLimit-Limit` — max requests per window
- `X-RateLimit-Remaining` — requests remaining
- `X-RateLimit-Reset` — window reset time (UTC)

When rate limited, the API returns `429 Too Many Requests`.
"""

_TAGS = [
    {
        "name": "Discovery",
        "description": "Browse and search registered agents. Public endpoints — no authentication required.",
    },
    {
        "name": "Registration",
        "description": "Register new agents and manage agent profiles. Registration is open; profile updates require authentication.",
    },
    {
        "name": "Payments",
        "description": "Send payments between agents via hub routing. All payment endpoints require authentication. "
                       "Supports idempotency keys for safe retries.",
    },
    {
        "name": "Balance",
        "description": "Manage agent balances: check balance, deposit XMR, withdraw funds, and view deposit history. "
                       "All endpoints require authentication.",
    },
    {
        "name": "Webhooks",
        "description": "View and retry webhook delivery events. Webhooks notify your agent of incoming payments and deposits.",
    },
    {
        "name": "Admin",
        "description": "Administrative endpoints for platform monitoring and agent verification. "
                       "Requires the `admin-key` header with a valid ADMIN_API_KEY.",
    },
    {
        "name": "health",
        "description": "Health checks, readiness probes, and Prometheus metrics for monitoring and orchestration.",
    },
]

# Maps existing router tag names to enriched tag names
_TAG_REMAP = {
    "agents": "Registration",
    "payments": "Payments",
    "balance": "Balance",
    "webhooks": "Webhooks",
    "admin": "Admin",
    "health": "health",
    "escrow": "Payments",
}

_ERROR_SCHEMA = {
    "ErrorResponse": {
        "type": "object",
        "properties": {
            "detail": {
                "type": "string",
                "description": "Error message describing what went wrong",
                "example": "Invalid API key",
            },
        },
        "required": ["detail"],
    },
}

_REQUEST_EXAMPLES = {
    "/v2/agents/register": {
        "post": {
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "minimal": {
                                "summary": "Minimal registration",
                                "value": {
                                    "agent_name": "my-trading-bot",
                                },
                            },
                            "full": {
                                "summary": "Full registration with webhook",
                                "value": {
                                    "agent_name": "payment-processor",
                                    "webhook_url": "https://api.example.com/sthrip-webhook",
                                    "privacy_level": "high",
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    "/v2/payments/hub-routing": {
        "post": {
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "simple": {
                                "summary": "Simple payment",
                                "value": {
                                    "to_agent_name": "merchant-bot",
                                    "amount": 0.5,
                                },
                            },
                            "with_memo": {
                                "summary": "Payment with memo",
                                "value": {
                                    "to_agent_name": "service-provider",
                                    "amount": 1.25,
                                    "memo": "Invoice #1234",
                                    "urgency": "urgent",
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def custom_openapi(app: FastAPI) -> dict:
    """Generate enriched OpenAPI schema with tags, examples, and descriptions."""
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=_DESCRIPTION,
        routes=app.routes,
    )

    # Add tags
    schema["tags"] = _TAGS

    # Remap router tags to enriched tags
    for path_key, path_item in schema.get("paths", {}).items():
        for method_key, operation in path_item.items():
            if not isinstance(operation, dict):
                continue
            tags = operation.get("tags", [])
            operation["tags"] = [_TAG_REMAP.get(t, t) for t in tags]

    # Add error response schemas
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    schemas.update(_ERROR_SCHEMA)

    # Add request examples
    for path_key, methods in _REQUEST_EXAMPLES.items():
        if path_key not in schema.get("paths", {}):
            continue
        for method_key, overrides in methods.items():
            if method_key not in schema["paths"][path_key]:
                continue
            op = schema["paths"][path_key][method_key]
            if "requestBody" in overrides:
                rb = op.setdefault("requestBody", {})
                content = rb.setdefault("content", {})
                for ct, ct_data in overrides["requestBody"]["content"].items():
                    existing = content.setdefault(ct, {})
                    if "examples" in ct_data:
                        existing["examples"] = ct_data["examples"]

    app.openapi_schema = schema
    return schema


# ═══════════════════════════════════════════════════════════════════════════════
# HTML PAGES
# ═══════════════════════════════════════════════════════════════════════════════

_REDOC_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>Sthrip API Documentation</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
    </style>
</head>
<body>
    <redoc spec-url="/openapi.json"
           hide-download-button
           theme='{
               "colors": { "primary": { "main": "#6d28d9" } },
               "typography": { "fontFamily": "Inter, sans-serif" },
               "rightPanel": { "backgroundColor": "#1e1b4b" }
           }'
    ></redoc>
    <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
</body>
</html>
"""

_SWAGGER_HTML = """\
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>Sthrip API Playground</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"/>
    <style>
        body { margin: 0; padding: 0; }
        .swagger-ui .topbar { display: none; }
        .swagger-ui .info .title { font-size: 2em; }
    </style>
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script>
        SwaggerUIBundle({
            url: '/openapi.json',
            dom_id: '#swagger-ui',
            layout: 'BaseLayout',
            deepLinking: true,
            showExtensions: true,
            showCommonExtensions: true,
            presets: [
                SwaggerUIBundle.presets.apis,
                SwaggerUIBundle.SwaggerUIStandalonePreset,
            ],
        });
    </script>
</body>
</html>
"""


def setup_docs(app: FastAPI) -> None:
    """Register custom documentation routes on the app.

    Replaces FastAPI's default docs with branded Redoc + Swagger playground.
    Works in all environments (dev, staging, production).
    """
    # Override the openapi method to use our enriched schema
    app.openapi = lambda: custom_openapi(app)  # type: ignore[assignment]

    @app.get("/docs", include_in_schema=False)
    async def redoc_page() -> HTMLResponse:
        return HTMLResponse(_REDOC_HTML)

    @app.get("/docs/playground", include_in_schema=False)
    async def swagger_playground() -> HTMLResponse:
        return HTMLResponse(_SWAGGER_HTML)

    # Always serve openapi.json (override FastAPI's env-gated behavior)
    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_json():
        return custom_openapi(app)
