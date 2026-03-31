# Sthrip - Anonymous Payments for AI Agents

[![PyPI version](https://badge.fury.io/py/sthrip.svg)](https://badge.fury.io/py/sthrip)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Sthrip enables AI agents to make anonymous, censorship-resistant payments using Monero. Perfect for agent-to-agent transactions, escrow deals, and micropayments.

## 🚀 Quick Start

```bash
pip install sthrip
```

```python
from sthrip import Sthrip

# Connect to your Monero wallet
agent = Sthrip.from_env()

# Check balance
info = agent.get_info()
print(f"Balance: {info.balance} XMR")

# Send anonymous payment
tx = agent.pay(
    to_address="44...",
    amount=0.1,
    memo="Payment for data analysis"
)
print(f"Sent: {tx.tx_hash}")
```

## 💡 Key Features

- **Zero-Knowledge Payments** - Sender, receiver, and amount are hidden
- **No KYC Required** - Perfect for autonomous agents
- **Stealth Addresses** - One-time addresses for each transaction
- **Escrow Support** - 2-of-3 multisig for secure deals
- **Payment Channels** - Instant, off-chain micropayments
- **P2P + Hub Routing** - Free direct payments or fast routed payments

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Agent A                                                    │
│  ┌──────────────┐      ┌──────────────┐                     │
│  │   Sthrip │──────▶│   Monero     │                     │
│  │   Client     │      │   Network    │                     │
│  └──────────────┘      └──────────────┘                     │
└─────────────────────────────────────────────────────────────┘
         │                              │
         │ P2P (free, 0%)               │
         │ or                           │
         │ Hub Routing (1%, instant)    │
         ▼                              ▼
┌─────────────────────────────────────────────────────────────┐
│  Agent B                                                    │
│  ┌──────────────┐      ┌──────────────┐                     │
│  │   Sthrip │◀─────│   Monero     │                     │
│  │   Client     │      │   Network    │                     │
│  └──────────────┘      └──────────────┘                     │
└─────────────────────────────────────────────────────────────┘
```

## 📦 Installation

### Basic Installation

```bash
pip install sthrip
```

### With MCP Support (for Claude/Cursor)

```bash
pip install sthrip[mcp]
```

### Development Installation

```bash
git clone https://github.com/sthrip/sthrip.git
cd sthrip
pip install -e ".[dev]"
```

## 🔧 Configuration

Set environment variables:

```bash
# Monero Wallet RPC
export MONERO_RPC_HOST=127.0.0.1
export MONERO_RPC_PORT=18082
export MONERO_RPC_USER=your_username
export MONERO_RPC_PASS=your_password

# Database (PostgreSQL)
export DATABASE_URL=postgresql://user:pass@localhost/sthrip

# Redis (for rate limiting)
export REDIS_URL=redis://localhost:6379/0

# API Server
export PORT=8000
export ADMIN_API_KEY=your_secret_key
```

## 📖 Usage

### P2P Payment (Free)

```python
from sthrip import Sthrip

agent = Sthrip.from_env()

# Send payment directly (free, but normal confirmation time)
tx = agent.pay(
    to_address="44ABC...",
    amount=0.5,
    memo="Payment for service"
)

# Wait for confirmation
confirmed = agent.wait_for_confirmation(tx.tx_hash)
```

### Hub Routing (1% fee, instant)

```python
# Use hub routing for instant confirmation
from sthrip.services.fee_collector import get_fee_collector

collector = get_fee_collector()
route = collector.create_hub_route(
    from_agent_id="your-agent-id",
    to_agent_id="recipient-agent-id",
    amount=1.0
)

# Confirmed instantly (hub takes the risk)
result = collector.confirm_hub_route(route["payment_id"])
```

### Escrow (1% fee)

```python
# Create 2-of-3 escrow deal
escrow = agent.create_escrow(
    seller_address="44SELLER...",
    arbiter_address="44ARBITER...",  # Optional
    amount=10.0,
    description="Smart contract audit",
    timeout_hours=72
)

# Fund escrow
agent.fund_escrow(escrow.id, multisig_address)

# Release when work is done
agent.release_escrow(escrow.id)
```

### Payment Channels

```python
# Open channel for frequent payments
channel = agent.open_channel(
    counterparty_address="44PARTNER...",
    capacity=5.0  # XMR to lock
)

# Instant off-chain payments
state = agent.channel_pay(channel.id, amount=0.01)

# Close when done
agent.close_channel(channel.id)
```

### Stealth Addresses

```python
# Create one-time address for receiving
stealth = agent.create_stealth_address(purpose="api-payment")
print(f"Pay me: {stealth.address}")  # Use once then discard
```

## 🌐 API Server

Start the REST API:

```bash
sthrip-api
# or
python -m sthrip.api.main_v2
```

### Register Agent

```bash
curl -X POST http://localhost:8000/v2/agents/register \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "my-agent",
    "webhook_url": "https://my-agent.com/webhooks",
    "xmr_address": "44..."
  }'
```

Response:
```json
{
  "agent_id": "uuid",
  "agent_name": "my-agent",
  "tier": "free",
  "api_key": "sk_...",  // SAVE THIS!
  "created_at": "2024-01-01T00:00:00"
}
```

### Send Payment

```bash
curl -X POST http://localhost:8000/v2/payments/send \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "to_address": "44...",
    "amount": 0.1,
    "memo": "Payment"
  }'
```

### Discover Agents

```bash
# Find verified agents with high trust score
curl "http://localhost:8000/v2/agents?min_trust_score=80&verified_only=true"
```

## 🤖 MCP Server (for Claude/Cursor)

Sthrip works as an MCP tool server for AI assistants:

```bash
# Install with MCP support
pip install sthrip[mcp]

# Run MCP server
sthrip-mcp
```

Available tools:
- `send_payment` - Send XMR payment
- `get_balance` - Check wallet balance
- `create_stealth_address` - Generate receiving address
- `create_escrow` - Create escrow deal

### Configure Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sthrip": {
      "command": "sthrip-mcp",
      "env": {
        "MONERO_RPC_HOST": "127.0.0.1",
        "MONERO_RPC_PORT": "18082"
      }
    }
  }
}
```

## 💰 Fee Structure

| Service | Fee | When to Use |
|---------|-----|-------------|
| P2P Direct | 0% | Trust recipient, normal speed OK |
| Hub Routing | 1% | Need instant confirmation |
| Escrow | 1% | Large amounts, need protection |
| API Calls | $0.001 | Reputation checks |
| Verified Badge | $29/month | Build trust |

## 🏛️ Production Deployment

### Docker Compose

```yaml
version: '3.8'
services:
  api:
    image: sthrip/api:latest
    ports:
      - "8000:8000"
    environment:
      - DATABASE_URL=postgresql://user:pass@db/sthrip
      - REDIS_URL=redis://redis:6379/0
    depends_on:
      - db
      - redis
      - monero-wallet-rpc

  db:
    image: postgres:15
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: pass
      POSTGRES_DB: sthrip
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine

  monero-wallet-rpc:
    image: ghcr.io/sethforprivacy/simple-monero-wallet-rpc:latest
    volumes:
      - monero_wallets:/home/monero/wallets

volumes:
  postgres_data:
  monero_wallets:
```

### Health Checks

```bash
curl http://localhost:8000/health
```

Response:
```json
{
  "status": "healthy",
  "version": "2.0.0",
  "timestamp": "2024-01-01T00:00:00Z",
  "checks": {
    "database": {"healthy": true},
    "redis": {"healthy": true},
    "wallet_rpc": {"healthy": true}
  }
}
```

## 🔒 Security

- **Non-custodial** - We never store private keys
- **Zero-knowledge** - Monero hides transaction details
- **Rate limiting** - Redis-based per-agent limits
- **HMAC webhooks** - Signed webhook deliveries

## 📚 Documentation

- [Full Documentation](https://docs.sthrip.io)
- [API Reference](https://docs.sthrip.io/api)
- [Python SDK](https://docs.sthrip.io/python)
- [TypeScript SDK](https://docs.sthrip.io/typescript)

## 🤝 Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.

## 🔗 Links

- [Website](https://sthrip.io)
- [Documentation](https://docs.sthrip.io)
- [GitHub](https://github.com/sthrip/sthrip)
- [Discord](https://discord.gg/sthrip)
- [Twitter](https://twitter.com/sthrip)
