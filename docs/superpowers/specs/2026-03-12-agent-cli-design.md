# Sthrip Agent CLI — Design Spec

**Date:** 2026-03-12
**Status:** Draft

## Overview

A machine-readable CLI client for the Sthrip API, enabling AI agents and scripts to register, pay, check balances, and manage their accounts programmatically from the terminal.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Primary consumer | AI agents / scripts | Machine-readable JSON output, meaningful exit codes |
| Credentials storage | `~/.sthrip/credentials.json` | Consistent with MCP server auth flow |
| Default API URL | `https://sthrip-api-production.up.railway.app` | Production-first, overridable via flag/env/config |
| Command scope | Full API surface | All agent-facing endpoints covered |
| Stack | Typer + httpx | Modern, type-hint driven, httpx already in project deps |

## Package Structure

```
sthrip/cli/
├── __init__.py
├── app.py              # Typer app entry point, global flags (--url, --timeout, --debug)
├── client.py           # Sync httpx wrapper — all API calls
├── config.py           # Read/write ~/.sthrip/credentials.json + base_url resolution
├── output.py           # JSON formatting, exit code constants
└── commands/
    ├── __init__.py
    ├── register.py     # sthrip register <name>
    ├── balance.py      # sthrip balance, deposit, withdraw, deposits
    ├── payments.py     # sthrip pay, payment, history
    ├── agents.py       # sthrip agents list/get, leaderboard
    ├── me.py           # sthrip me, me update, rate-limit
    ├── webhooks.py     # sthrip webhooks list/retry
    ├── keys.py         # sthrip rotate-key
    └── health.py       # sthrip health, config show
```

Existing `cli/main.py` (low-level wallet commands) remains untouched.

## Config File

```json
{
  "api_key": "sk_...",
  "base_url": "https://sthrip-api-production.up.railway.app",
  "agent_name": "my-agent"
}
```

**Resolution priority (auth):** `STHRIP_API_KEY` env > `credentials.json.api_key`

**Resolution priority (URL):** `--url` flag > `STHRIP_BASE_URL` env > `credentials.json.base_url` > hardcoded production URL

**Read-merge-write strategy:** Config writes always load existing JSON first, update only changed fields, and write back. This prevents clobbering fields written by MCP server or other tools sharing the same file.

- `sthrip register` saves `api_key` + `agent_name` automatically
- File created with `0600` permissions (owner-only read/write)
- Shared with MCP server (same path, same format)

## HTTP Client (`client.py`)

- Wraps `httpx.Client` (synchronous — CLI does not need async)
- Reads Bearer token from config (respecting priority chain)
- Default timeout: 30s, overridable via `--timeout` global flag
- `--debug` flag logs request/response details to stderr (stdout stays clean for JSON)
- No retry logic — callers (scripts/agents) handle retry policy
- All methods return `dict` (parsed JSON response) or raise `CliError`
- Validates HTTP status codes and maps to appropriate exit codes
- Supports `Idempotency-Key` header via `--idempotency-key` on mutating commands

## Output Format (`output.py`)

All output is JSON. Success goes to stdout, errors to stderr.

**Success:**
```json
{"ok": true, "data": {"balance": "12.5", "pending": "0.0"}}
```

**Error:**
```json
{"ok": false, "error": "Agent not found", "code": 1}
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | API error (4xx/5xx) |
| 2 | Authentication error (401/403 or missing credentials) |
| 3 | Network error (connection refused, timeout) |
| 4 | Validation error (bad arguments) |

## Commands

### Global Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | (see priority chain) | Override API base URL |
| `--timeout` | 30 | Request timeout in seconds |
| `--debug` | false | Log HTTP request/response details to stderr |

### Registration

| Command | API Endpoint | Notes |
|---------|-------------|-------|
| `sthrip register <name> [--webhook-url URL] [--privacy LEVEL]` | `POST /v2/agents/register` | Saves credentials to `~/.sthrip/credentials.json` on success (read-merge-write). Outputs full registration response including one-time `api_key` and `webhook_secret`. |

### Balance & Deposits

| Command | API Endpoint | Notes |
|---------|-------------|-------|
| `sthrip balance` | `GET /v2/balance` | Returns available, pending, total, deposit_address |
| `sthrip deposit [--amount AMOUNT] [--idempotency-key KEY]` | `POST /v2/balance/deposit` | Onchain: returns deposit address. Ledger: credits immediately (amount required). |
| `sthrip withdraw <address> <amount> [--idempotency-key KEY]` | `POST /v2/balance/withdraw` | Validates Monero address format client-side before sending |
| `sthrip deposits` | `GET /v2/balance/deposits` | List deposit transactions |

### Payments

| Command | API Endpoint | Notes |
|---------|-------------|-------|
| `sthrip pay <agent_name> <amount> [--memo TEXT] [--urgent] [--idempotency-key KEY]` | `POST /v2/payments/hub-routing` | Hub-routed payment to another agent |
| `sthrip payment <payment_id>` | `GET /v2/payments/{payment_id}` | Look up payment status by ID |
| `sthrip history [--limit N] [--offset N] [--direction in\|out]` | `GET /v2/payments/history` | Payment history with pagination and filters |

### Agent Discovery

| Command | API Endpoint | Notes |
|---------|-------------|-------|
| `sthrip agents list [--verified] [--tier TIER] [--min-trust-score N] [--limit N] [--offset N]` | `GET /v2/agents` | Discover agents with filters and pagination |
| `sthrip agents get <name>` | `GET /v2/agents/{agent_name}` | Public agent profile by agent_name (not UUID) |
| `sthrip leaderboard` | `GET /v2/leaderboard` | Top agents by trust score |

### Self-Management

| Command | API Endpoint | Notes |
|---------|-------------|-------|
| `sthrip me` | `GET /v2/me` | Current agent info |
| `sthrip me update [--webhook-url URL] [--privacy LEVEL]` | `PATCH /v2/me/settings` | Update agent settings |
| `sthrip rotate-key` | `POST /v2/me/rotate-key` | Rotates API key, updates `credentials.json` automatically (read-merge-write) |
| `sthrip rate-limit` | `GET /v2/me/rate-limit` | Current rate limit status |

### Webhooks

| Command | API Endpoint | Notes |
|---------|-------------|-------|
| `sthrip webhooks list` | `GET /v2/webhooks/events` | Recent webhook events |
| `sthrip webhooks retry <event_id>` | `POST /v2/webhooks/events/{id}/retry` | Retry failed webhook delivery |

### Diagnostics

| Command | API Endpoint | Notes |
|---------|-------------|-------|
| `sthrip health` | `GET /health` | API health check — useful as pre-flight check in scripts |
| `sthrip config show` | (local) | Print resolved config: base_url, credentials file path, api_key source (env/file/none). Does not expose the full key. |

## Entry Point

```toml
# In future pyproject.toml or setup.cfg
[project.scripts]
sthrip = "sthrip.cli.app:app"
```

Existing `cli/main.py` has no installed entry point (run as `python cli/main.py`), so `sthrip` is safe to claim.

## Testing Strategy

- **Unit tests:** Mock httpx with `respx`, test config read/write (including merge), output formatting, argument parsing
- **Integration tests:** Full command invocation via Typer's `CliRunner`, mock API responses
- **Coverage target:** 80%+
- **Test location:** `tests/test_cli.py` initially, split to `tests/cli/` if needed

## Dependencies

New production dependencies:
- `typer>=0.9.0,<1.0` — CLI framework (Python 3.9 compatible)

New dev dependencies:
- `respx` — httpx mock library for tests

(httpx already in requirements.txt)

## Out of Scope

- Interactive/TUI mode
- Color/table output (machine-readable only)
- Shell completion generation (can add later)
- Admin commands (separate concern)
- Direct wallet operations (handled by existing `cli/main.py`)
