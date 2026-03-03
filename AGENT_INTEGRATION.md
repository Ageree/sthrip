# Agent Integration Guide

Guide for integrating StealthPay into AI Agents.

## Quick Start for Agents

### 1. Python SDK (Recommended)

```python
from stealthpay import StealthPay

# Initialize
agent = StealthPay.from_env()

# Check balance
info = agent.get_info()
print(f"Balance: {info.balance} XMR")

# Send payment
payment = agent.pay(
    to_address="44...seller...",
    amount=0.01,
    memo="Payment for API access"
)
```

### 2. REST API (Any Language)

```bash
# Register agent
curl -X POST http://localhost:8000/agents/register \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "my-agent", "privacy_level": "high"}'

# Response: {"api_key": "sk_...", "address": "44..."}

# Send payment
curl -X POST http://localhost:8000/payments/send \
  -H "Authorization: Bearer sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "to_address": "44...",
    "amount": 0.01,
    "memo": "API payment"
  }'
```

### 3. LangChain Integration

```python
from stealthpay import StealthPay
from stealthpay.integrations.langchain import get_stealthpay_tools
from langchain.agents import initialize_agent

# Setup
stealthpay = StealthPay.from_env()
tools = get_stealthpay_tools(stealthpay)

# Create agent
agent = initialize_agent(tools, llm, agent="zero-shot-react-description")

# Agent can now:
# - Check balance
# - Send payments
# - Create addresses
agent.run("Send 0.01 XMR to 44... for data")
```

### 4. MCP Server (Claude/Cursor)

Add to Claude Desktop config:

```json
{
  "mcpServers": {
    "stealthpay": {
      "command": "python",
      "args": ["/path/to/stealthpay/integrations/mcp_server.py"],
      "env": {
        "MONERO_RPC_HOST": "127.0.0.1",
        "MONERO_RPC_PORT": "18082"
      }
    }
  }
}
```

Then ask Claude: "Send 0.01 XMR to address 44..."

## Common Agent Patterns

### Pattern 1: Service Provider Agent

Agent sells data/services:

```python
class ServiceAgent:
    def __init__(self):
        self.stealthpay = StealthPay.from_env()
        self.price = 0.001
    
    def handle_request(self, service):
        # 1. Create payment request
        stealth = self.stealthpay.create_stealth_address()
        
        return {
            "price": self.price,
            "pay_to": stealth.address,
            "instructions": f"Send {self.price} XMR"
        }
    
    def deliver(self, request_id):
        # 2. Check payment
        if self.check_payment(request_id):
            # 3. Deliver service
            return {"data": "..."}
```

### Pattern 2: Consumer Agent

Agent buys services:

```python
class ConsumerAgent:
    def buy_service(self, seller_url, service):
        # 1. Discover price
        catalog = self.discover(seller_url)
        
        # 2. Get payment address
        request = self.create_request(seller_url, service)
        
        # 3. Send payment
        payment = self.stealthpay.pay(
            request['pay_to'],
            request['price']
        )
        
        # 4. Wait & receive
        return self.wait_for_delivery(request['id'])
```

### Pattern 3: Escrow Agent

Agent acts as trusted arbiter:

```python
class ArbiterAgent:
    def create_escrow(self, buyer, seller, amount):
        return self.stealthpay.create_escrow(
            seller_address=seller,
            arbiter_address=self.address,
            amount=amount,
            description="Escrow deal"
        )
    
    def resolve_dispute(self, escrow_id, decision):
        # "release" to seller or "refund" to buyer
        return self.stealthpay.escrow.arbitrate(
            escrow_id, decision, signature
        )
```

## CLI Usage

```bash
# Check balance
stealthpay balance

# Send payment
stealthpay send 44... 0.01 --memo "API payment"

# Create address
stealthpay address create --purpose "service-payment"

# Churn for privacy
stealthpay churn 1.0 --rounds 3

# Create escrow
stealthpay escrow create 44seller... 44arbiter... 0.5 "Description"
```

## Security Best Practices

1. **Never hardcode credentials** - Use environment variables
2. **Secure the wallet RPC** - Bind to localhost only
3. **Use high privacy mode** for sensitive transactions
4. **Churn large amounts** before spending
5. **Monitor balance** - Set up alerts

## Environment Variables

```bash
# Required
MONERO_RPC_HOST=127.0.0.1
MONERO_RPC_PORT=18082

# Optional auth
MONERO_RPC_USER=agent
MONERO_RPC_PASS=secret

# For API
STEALTHPAY_API_KEY=sk_...
```

## Docker Deployment

```yaml
version: '3'
services:
  monero:
    image: monero-wallet-rpc
    volumes:
      - ./wallet:/wallet
    
  stealthpay-api:
    image: stealthpay/sdk-python
    ports:
      - "8000:8000"
    environment:
      - MONERO_RPC_HOST=monero
      - MONERO_RPC_PORT=18082
```

## Troubleshooting

**Connection refused:**
- Check monero-wallet-rpc is running
- Verify MONERO_RPC_HOST/PORT

**Insufficient funds:**
- Check balance: `stealthpay balance`
- Wait for unlock (10 confirmations)

**Payment not confirming:**
- Normal: 20 minutes for 10 confirmations
- Check: `stealthpay history`

## Support

- Docs: https://docs.stealthpay.io
- Discord: https://discord.gg/stealthpay
- Issues: GitHub Issues
