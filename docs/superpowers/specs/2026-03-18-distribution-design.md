# Sthrip Distribution: pip package, discovery, landing

**Date:** 2026-03-18
**Status:** Approved

## Goal

AI agents install `pip install sthrip` and can anonymously send/receive XMR through the Sthrip hub. Developers find the project via landing page, GitHub, or PyPI.

## 1. pip package `sthrip`

### Interface

```python
from sthrip import Sthrip

s = Sthrip()                        # auto-registers on first use
addr = s.deposit_address()          # XMR subaddress for deposits
s.pay("agent-name", 0.5)           # hub-routed payment
s.pay("agent-name", 0.5, memo="for data analysis")
print(s.balance())                  # {"available": "1.5", "pending": "0"}
agents = s.find_agents(capability="translation")
me = s.me()                         # agent profile
```

### Package structure

```
sthrip/              (new top-level SDK package, separate from existing sthrip/ server code)
  __init__.py        re-exports Sthrip class
  client.py          Sthrip class — thin wrapper over REST API
  auth.py            credential storage (~/.sthrip/credentials.json)
  exceptions.py      StrhipError, PaymentError, AuthError, etc.
```

This is a NEW standalone package directory (e.g. `sdk/sthrip/`) with its own `pyproject.toml`, separate from the server codebase. Only dependency: `requests`.

### Behavior

- `Sthrip(api_key=None, api_url=None)` constructor
  - `api_url` defaults to env `STHRIP_API_URL` or `https://sthrip-api-production.up.railway.app`
  - `api_key` defaults to env `STHRIP_API_KEY` or reads from `~/.sthrip/credentials.json`
  - If no key found anywhere, auto-registers a new agent and saves credentials
- Auto-registration uses hostname + random suffix as agent_name
- All methods raise typed exceptions (`PaymentError`, `InsufficientBalance`, `AgentNotFound`)
- Sync-only (no async) for simplicity

### pyproject.toml (sdk/)

```toml
[project]
name = "sthrip"
version = "0.1.0"
description = "Anonymous payments for AI agents"
requires-python = ">=3.8"
dependencies = ["requests>=2.25"]
license = {text = "MIT"}

[project.urls]
Homepage = "https://sthrip.dev"
Documentation = "https://sthrip-api-production.up.railway.app/docs"
Repository = "https://github.com/sthrip/sthrip"
```

## 2. Discovery endpoint

### `GET /.well-known/agent-payments.json`

Added to sthrip-api. Static JSON, no auth required.

```json
{
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
    "agents": "/v2/agents"
  },
  "supported_tokens": ["XMR"],
  "fee_percent": "0.1",
  "min_confirmations": 10,
  "install": "pip install sthrip"
}
```

Implementation: new route in `api/routers/health.py` or dedicated `api/routers/wellknown.py`.

## 3. GitHub repo

Public repo at `github.com/sthrip/sthrip` containing:

- **README.md** with:
  - One-liner description
  - 3-line quickstart code
  - Badges: PyPI version, license, tests
  - Links to landing, API docs, PyPI
  - How it works (brief)
  - Fee structure
- **LICENSE** (MIT)
- **sdk/** directory (the pip package source)

The server code stays in the private repo. Only the SDK is public.

## 4. Landing page (Vercel)

Single-page site at `sthrip.dev` (or similar domain):

- Hero: "Anonymous payments for AI agents"
- Quickstart code snippet (3 lines)
- How it works: register -> deposit -> pay
- Fee: 0.1% per transaction
- Links: pip install, GitHub, API docs
- Built with plain HTML/CSS or lightweight framework

Tech: static HTML deployed on Vercel. No framework needed for one page.

## Implementation order

1. SDK package (`sdk/sthrip/`) — core value
2. `.well-known/agent-payments.json` — quick, deploy with API
3. GitHub repo — push SDK + README
4. Landing page — Vercel deploy

## Out of scope

- Fiat gateway (future)
- MCP registry listing (separate task)
- npm/other language SDKs (future)
- Async client (future, if needed)
