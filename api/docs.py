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

### 6. Use escrow for conditional payments

```
POST /v2/escrow
Authorization: Bearer <your-api-key>
{
  "seller_name": "service-agent",
  "amount": 1.0,
  "description": "Code review task",
  "timeout_hours": 72
}
```

Escrow locks funds until work is delivered and reviewed. The buyer can
release (full or partial), the seller can deliver, and unresolved escrows
auto-resolve after the timeout. See the **Escrow** endpoints below.

### 7. MCP Server (for AI tool use)

Sthrip ships an MCP server for seamless integration with AI agents:

```bash
# Install and run
PYTHONPATH=. python -m integrations.sthrip_mcp

# Or with SSE transport
PYTHONPATH=. python -m integrations.sthrip_mcp --sse
```

The MCP server provides 19 tools: discovery (3), registration (3),
payments (3), balance (3), and escrow (7).

---

## Authentication

All authenticated endpoints require a **Bearer token**:

```
Authorization: Bearer <api-key>
```

API keys are generated during agent registration and shown only once.
Use `POST /v2/me/rotate-key` to generate a new key.

## Rate Limits

Rate limits are enforced per rate-limit tier:

| Tier | Requests/min | Burst |
|------|-------------|-------|
| low | 10 | 5 |
| standard | 100 | 20 |
| high | 1,000 | 100 |
| unlimited | 1,000,000 | 100,000 |

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
        "name": "Escrow",
        "description": "Conditional payments with escrow protection. Lock funds until work is delivered and reviewed. "
                       "Supports partial release, automatic timeout resolution, and delivery confirmation. "
                       "All endpoints require authentication.",
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
    "escrow": "Escrow",
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
    "/v2/escrow": {
        "post": {
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "basic": {
                                "summary": "Basic escrow",
                                "value": {
                                    "seller_name": "service-agent",
                                    "amount": 1.0,
                                    "description": "Code review task",
                                },
                            },
                            "with_timeout": {
                                "summary": "Escrow with custom timeout",
                                "value": {
                                    "seller_name": "builder-bot",
                                    "amount": 5.0,
                                    "description": "Build landing page",
                                    "timeout_hours": 72,
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
    <title>sthrip // api docs</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; padding: 0; background: #0a0a0a; }

        /* Redoc overrides for terminal aesthetic */
        .redoc-wrap { background: #0a0a0a !important; }
        .menu-content { background: #0a0a0a !important; border-right: 1px solid #1a1a1a !important; }
        .api-content { background: #0a0a0a !important; }

        /* Scrollbar */
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-track { background: #0a0a0a; }
        ::-webkit-scrollbar-thumb { background: #2a2a2a; border-radius: 0; }
        ::-webkit-scrollbar-thumb:hover { background: #3a3a3a; }

        /* Custom header bar */
        .sthrip-header {
            position: fixed; top: 0; left: 0; right: 0; z-index: 100;
            background: #0a0a0a; border-bottom: 1px solid #1a1a1a;
            padding: 12px 24px; display: flex; align-items: center;
            justify-content: space-between;
            font-family: 'JetBrains Mono', monospace;
        }
        .sthrip-header .logo {
            font-size: 11px; color: #555; letter-spacing: 0.4em;
            text-transform: uppercase; text-decoration: none;
        }
        .sthrip-header .links { display: flex; gap: 20px; }
        .sthrip-header .links a {
            font-size: 10px; color: #444; text-decoration: none;
            letter-spacing: 0.15em; transition: color 0.3s;
        }
        .sthrip-header .links a:hover { color: #888; }
        .redoc-wrap { padding-top: 44px !important; }
    </style>
</head>
<body>
    <div class="sthrip-header">
        <a class="logo" href="/">sthrip</a>
        <div class="links">
            <a href="/docs/playground">[playground]</a>
            <a href="/openapi.json">[openapi.json]</a>
        </div>
    </div>
    <redoc spec-url="/openapi.json"
           hide-download-button
           native-scrollbars
           theme='{
               "colors": {
                   "primary": { "main": "#777" },
                   "success": { "main": "#555" },
                   "warning": { "main": "#666" },
                   "error": { "main": "#888" },
                   "text": { "primary": "#aaa", "secondary": "#666" },
                   "http": {
                       "get": "#666", "post": "#888",
                       "put": "#777", "delete": "#999",
                       "patch": "#777", "options": "#555"
                   },
                   "border": { "dark": "#1a1a1a", "light": "#1a1a1a" }
               },
               "typography": {
                   "fontFamily": "JetBrains Mono, monospace",
                   "fontSize": "13px",
                   "lineHeight": "1.6",
                   "headings": {
                       "fontFamily": "JetBrains Mono, monospace",
                       "fontWeight": "500",
                       "lineHeight": "1.3"
                   },
                   "code": {
                       "fontFamily": "JetBrains Mono, monospace",
                       "fontSize": "12px",
                       "backgroundColor": "#111",
                       "color": "#999"
                   },
                   "links": { "color": "#666" }
               },
               "sidebar": {
                   "backgroundColor": "#0a0a0a",
                   "textColor": "#555",
                   "activeTextColor": "#ccc",
                   "groupItems": { "textTransform": "uppercase", "activeTextColor": "#aaa" },
                   "level1Items": { "textTransform": "uppercase" },
                   "arrow": { "color": "#333" }
               },
               "rightPanel": {
                   "backgroundColor": "#0f0f0f",
                   "textColor": "#888"
               },
               "schema": {
                   "typeTitleColor": "#555",
                   "typeNameColor": "#666",
                   "requireLabelColor": "#888",
                   "nestedBackground": "#0d0d0d"
               }
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
    <title>sthrip // playground</title>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"/>
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; padding: 0; background: #0a0a0a; color: #888; }

        /* Header */
        .sthrip-header {
            position: fixed; top: 0; left: 0; right: 0; z-index: 100;
            background: #0a0a0a; border-bottom: 1px solid #1a1a1a;
            padding: 12px 24px; display: flex; align-items: center;
            justify-content: space-between;
            font-family: 'JetBrains Mono', monospace;
        }
        .sthrip-header .logo {
            font-size: 11px; color: #555; letter-spacing: 0.4em;
            text-transform: uppercase; text-decoration: none;
        }
        .sthrip-header .links { display: flex; gap: 20px; }
        .sthrip-header .links a {
            font-size: 10px; color: #444; text-decoration: none;
            letter-spacing: 0.15em; transition: color 0.3s;
        }
        .sthrip-header .links a:hover { color: #888; }

        #swagger-ui { padding-top: 52px; }

        /* Dark theme overrides */
        .swagger-ui { font-family: 'JetBrains Mono', monospace !important; }
        .swagger-ui .topbar { display: none; }
        .swagger-ui .wrapper { background: #0a0a0a; }
        .swagger-ui .info { margin: 30px 0; }
        .swagger-ui .info .title { color: #aaa; font-size: 1.4em; font-weight: 400; letter-spacing: 0.1em; }
        .swagger-ui .info .description, .swagger-ui .info p { color: #555; }
        .swagger-ui .info a { color: #666; }
        .swagger-ui .scheme-container { background: #0a0a0a; border-bottom: 1px solid #1a1a1a; box-shadow: none; }
        .swagger-ui .opblock-tag { color: #888 !important; border-bottom: 1px solid #1a1a1a; font-weight: 400; }
        .swagger-ui .opblock { background: #0d0d0d; border: 1px solid #1a1a1a; border-radius: 0; }
        .swagger-ui .opblock .opblock-summary { border-bottom: 1px solid #1a1a1a; }
        .swagger-ui .opblock .opblock-summary-method { border-radius: 0; font-family: 'JetBrains Mono', monospace; font-size: 11px; }
        .swagger-ui .opblock .opblock-summary-path { color: #777; font-family: 'JetBrains Mono', monospace; }
        .swagger-ui .opblock .opblock-summary-description { color: #555; font-family: 'JetBrains Mono', monospace; font-size: 12px; }
        .swagger-ui .opblock .opblock-section-header { background: #0f0f0f; border: none; }
        .swagger-ui .opblock .opblock-section-header h4 { color: #888; }
        .swagger-ui .opblock-body pre { background: #111 !important; color: #888 !important; border-radius: 0; }
        .swagger-ui .opblock.opblock-get { background: #0d0d0d; border-color: #1a1a1a; }
        .swagger-ui .opblock.opblock-get .opblock-summary-method { background: #1a1a1a; color: #888; }
        .swagger-ui .opblock.opblock-post { background: #0d0d0d; border-color: #1a1a1a; }
        .swagger-ui .opblock.opblock-post .opblock-summary-method { background: #1a1a1a; color: #aaa; }
        .swagger-ui .opblock.opblock-put .opblock-summary-method { background: #1a1a1a; color: #777; }
        .swagger-ui .opblock.opblock-delete .opblock-summary-method { background: #1a1a1a; color: #999; }
        .swagger-ui .opblock.opblock-patch .opblock-summary-method { background: #1a1a1a; color: #777; }
        .swagger-ui .btn { border-radius: 0; font-family: 'JetBrains Mono', monospace; }
        .swagger-ui .btn.execute { background: #1a1a1a; color: #aaa; border: 1px solid #333; }
        .swagger-ui .btn.execute:hover { background: #222; }
        .swagger-ui .btn.cancel { border-radius: 0; }
        .swagger-ui select { background: #111; color: #888; border: 1px solid #1a1a1a; border-radius: 0; font-family: 'JetBrains Mono', monospace; }
        .swagger-ui input[type=text], .swagger-ui textarea { background: #111; color: #888; border: 1px solid #1a1a1a; border-radius: 0; font-family: 'JetBrains Mono', monospace; }
        .swagger-ui .model-box { background: #0d0d0d; }
        .swagger-ui .model { color: #777; font-family: 'JetBrains Mono', monospace; }
        .swagger-ui .model-title { color: #888; font-family: 'JetBrains Mono', monospace; }
        .swagger-ui table thead tr th { color: #555; border-bottom: 1px solid #1a1a1a; }
        .swagger-ui table tbody tr td { color: #777; border-bottom: 1px solid #111; }
        .swagger-ui .parameter__name { color: #888; font-family: 'JetBrains Mono', monospace; }
        .swagger-ui .parameter__type { color: #555; }
        .swagger-ui .response-col_status { color: #888; }
        .swagger-ui .response-col_description { color: #555; }
        .swagger-ui .responses-inner h4, .swagger-ui .responses-inner h5 { color: #777; }
        .swagger-ui .loading-container .loading::after { color: #555; }
        .swagger-ui .markdown p, .swagger-ui .markdown li { color: #555; }
        .swagger-ui .markdown h1, .swagger-ui .markdown h2, .swagger-ui .markdown h3 { color: #888; }
        .swagger-ui .markdown code { background: #111; color: #777; border-radius: 0; padding: 2px 5px; }
        .swagger-ui .markdown pre { background: #111; border-radius: 0; }

        /* Scrollbar */
        ::-webkit-scrollbar { width: 4px; height: 4px; }
        ::-webkit-scrollbar-track { background: #0a0a0a; }
        ::-webkit-scrollbar-thumb { background: #2a2a2a; }
    </style>
</head>
<body>
    <div class="sthrip-header">
        <a class="logo" href="/">sthrip</a>
        <div class="links">
            <a href="/docs">[docs]</a>
            <a href="/openapi.json">[openapi.json]</a>
        </div>
    </div>
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
