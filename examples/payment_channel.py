"""
Example: Payment channel for instant micropayments
Perfect for high-frequency agent interactions
"""

from stealthpay import StealthPay

print("⚡ Payment Channel Example")
print("=" * 50)

# Agent A: API provider (收取费用)
# Agent B: API consumer (付费调用)

print("\n🤖 Setting up agents...")
provider = StealthPay(rpc_host="127.0.0.1", rpc_port=18082)
consumer = StealthPay(rpc_host="127.0.0.1", rpc_port=18083)

print(f"   Provider: {provider.address[:20]}...")
print(f"   Consumer: {consumer.address[:20]}...")
print(f"   Consumer balance: {consumer.balance:.4f} XMR")

print("\n📡 Step 1: Open payment channel")
print("   Consumer locks 1 XMR for API calls")
print("   (1 on-chain transaction, ~20 min confirmation)")

channel = consumer.open_channel(
    counterparty_address=provider.address,
    capacity=1.0,  # XMR
    their_capacity=0.0
)

print(f"   Channel ID: {channel.id}")
print(f"   Capacity: {channel.capacity} XMR")
print(f"   Status: {channel.status.value}")

# Provider accepts
provider.channels.accept_channel(channel.id)
channel = consumer.channels.fund_channel(channel.id, "funding_tx_123")
print(f"   Status after funding: {channel.status.value}")

print("\n⚡ Step 2: Make instant payments (off-chain)")
print("   No blockchain fees! Instant confirmation!")
print("   " + "-" * 40)

# Simulate 1000 API calls
api_calls = [
    ("weather", 0.001),
    ("news", 0.002),
    ("crypto_price", 0.0005),
    ("translation", 0.003),
    ("sentiment", 0.0015),
]

for i in range(10):  # Just 10 for demo
    service, price = api_calls[i % len(api_calls)]
    
    # Consumer pays provider through channel
    new_state = consumer.channel_pay(channel.id, price)
    
    consumer_balance = consumer.channels.get_balance(channel.id, consumer.address)
    provider_balance = channel.capacity - consumer_balance
    
    print(f"   Call #{i+1}: {service} (-{price} XMR)")
    print(f"      Consumer: {consumer_balance:.4f} | Provider: {provider_balance:.4f}")

print("   " + "-" * 40)
print(f"\n   Total API calls: 10")
print(f"   Total paid: {1.0 - consumer_balance:.4f} XMR")
print(f"   On-chain transactions: 0 ⚡")
print(f"   Fees paid: 0 XMR ⚡")

print("\n📡 Step 3: Close channel")
print("   Final balances written to blockchain (1 tx)")

closed = consumer.close_channel(channel.id, cooperative=True)
final = consumer.channels.finalize_close(channel.id, "closing_tx_456")

print(f"   Status: {final.status.value}")
print(f"   Consumer gets back: ~{consumer_balance:.4f} XMR")
print(f"   Provider receives: ~{provider_balance:.4f} XMR")

print("\n" + "=" * 50)
print("COMPARISON: Without channel vs With channel")
print("=" * 50)

print("\n❌ Without channel (on-chain every time):")
print("   10 API calls = 10 transactions")
print("   Time: 10 × 20 min = 200 minutes")
print("   Fees: 10 × $0.01 = $0.10")
print("   Privacy: All transactions public")

print("\n✅ With payment channel:")
print("   10 API calls = 0 on-chain (off-chain)")
print("   Time: Instant (< 1 second)")
print("   Fees: $0 (off-chain)")
print("   Settlement: 2 transactions total")
print("   Privacy: Only open/close visible")

print("\n" + "=" * 50)
print("Real world scenario: High-frequency trading agents")
print("=" * 50)

print("\n📊 Trading Agent A sends 10,000 price updates/sec to Agent B")
print("   Each update: $0.0001 (0.1 cent)")
print("   ")
print("   Without channel:")
print("     Impossible - blockchain can't handle 10k TPS")
print("     Even 1 TPS = $8.64/day in fees")
print("   ")
print("   With channel:")
print("     10k updates/sec ✓ (just updating local state)")
print("     Settlement every hour: 1 on-chain tx")
print("     Total fees: $0.01/day")

print("\n💡 Key benefits:")
print("   ✅ Unlimited TPS off-chain")
print("   ✅ Zero fees for micropayments")
print("   ✅ Instant settlement")
print("   ✅ Better privacy")
print("   ✅ Only 2 on-chain transactions total")
