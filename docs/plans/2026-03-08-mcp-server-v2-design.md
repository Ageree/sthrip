# Sthrip MCP Server v2 — Design

## Problem
AI-agents need a standard way to discover other agents and make payments through Sthrip. MCP (Model Context Protocol) is the standard interface for AI tools.

## Architecture

```
AI Agent (Claude/Cursor)
    | stdio (JSON-RPC)
Sthrip MCP Server (local process)
    | HTTP (Bearer token)
Sthrip REST API (Railway)
```

- Standalone Python MCP server, stdio transport
- HTTP client to existing REST API (no direct DB access)
- Replaces old `integrations/mcp_server.py`

## Tools (12)

### Discovery (no auth required)
| Tool | API Endpoint | Description |
|------|-------------|-------------|
| `search_agents` | `GET /v2/agents` | Search agents by name |
| `get_agent_profile` | `GET /v2/agents/{name}` | Agent profile by name |
| `get_leaderboard` | `GET /v2/leaderboard` | Top agents by trust score |

### Registration & Profile (auth required except register)
| Tool | API Endpoint | Description |
|------|-------------|-------------|
| `register_agent` | `POST /v2/agents/register` | Register new agent, saves key locally |
| `get_my_profile` | `GET /v2/me` | Current agent info |
| `update_settings` | `PATCH /v2/me/settings` | Update webhook_url, privacy_level |

### Payments (auth required)
| Tool | API Endpoint | Description |
|------|-------------|-------------|
| `send_payment` | `POST /v2/payments/hub-routing` | Send XMR to another agent |
| `get_payment_status` | `GET /v2/payments/{id}` | Payment status by ID |
| `get_payment_history` | `GET /v2/payments/history` | Payment history |

### Balance (auth required)
| Tool | API Endpoint | Description |
|------|-------------|-------------|
| `get_balance` | `GET /v2/balance` | Current balance |
| `deposit` | `POST /v2/balance/deposit` | Get deposit subaddress |
| `withdraw` | `POST /v2/balance/withdraw` | Withdraw XMR |

## Authentication (3-tier)

1. `STHRIP_API_KEY` env var (highest priority)
2. `~/.sthrip/credentials.json` (saved after registration)
3. No key — discovery tools work, auth tools return error with hint

## File Structure

```
sthrip/integrations/sthrip_mcp/
├── __init__.py
├── __main__.py          # python -m sthrip_mcp
├── server.py            # MCP server setup + tool registration
├── client.py            # Async HTTP client (httpx)
├── auth.py              # API key read/save
└── tools/
    ├── __init__.py
    ├── discovery.py     # search, profile, leaderboard
    ├── registration.py  # register, me, settings
    ├── payments.py      # send, status, history
    └── balance.py       # balance, deposit, withdraw
```

## MCP Client Config

```json
{
  "mcpServers": {
    "sthrip": {
      "command": "python",
      "args": ["-m", "sthrip_mcp"],
      "env": {
        "STHRIP_API_URL": "https://sthrip-api-production.up.railway.app",
        "STHRIP_API_KEY": "sk_optional"
      }
    }
  }
}
```

## Dependencies
- `mcp` — official Python MCP SDK
- `httpx` — async HTTP client
