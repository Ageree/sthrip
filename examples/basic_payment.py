"""
Basic example: Agent sending anonymous payment to another agent
"""

from sthrip import Sthrip

# Initialize agent wallet
# Make sure monero-wallet-rpc is running:
# monero-wallet-rpc --wallet-file my_agent --password "" --rpc-bind-port 18082

print("🥷 Initializing Sthrip agent...")
agent = Sthrip(
    rpc_host="127.0.0.1",
    rpc_port=18082,
    # rpc_user="optional",
    # rpc_pass="optional"
)

# Check balance
info = agent.get_info()
print(f"💰 Balance: {info.balance:.4f} XMR")
print(f"📍 Primary address: {info.address}")

# Create stealth address for receiving payment
# This creates a one-time address that can't be linked to your wallet
stealth = agent.create_stealth_address(
    label="payment-for-data",
    purpose="buying-research-data"
)
print(f"\n🎭 Stealth address generated: {stealth.address}")
print(f"   Index: {stealth.index}")
print(f"   Purpose: {stealth.label}")
print("\n   Share this address with the buyer.")
print("   It can only be used once and reveals nothing about your wallet.")

# Example: Send payment (uncomment when ready)
# recipient_address = "44...recipient...stealth...address"
# payment = agent.pay(
#     to_address=recipient_address,
#     amount=0.05,  # XMR
#     memo="Payment for weather API access"
# )
# print(f"\n💸 Payment sent!")
# print(f"   TX Hash: {payment.tx_hash}")
# print(f"   Amount: {payment.amount} XMR")
# print(f"   Fee: {payment.fee:.6f} XMR")
# print(f"   Status: {payment.status.value}")

# Get recent payments
print("\n📜 Recent payments:")
payments = agent.get_payments(limit=5)
for p in payments:
    direction = "📥" if p.from_address else "📤"
    status = "✅" if p.is_confirmed else "⏳"
    print(f"   {direction} {status} {p.amount:.4f} XMR - {p.timestamp.strftime('%Y-%m-%d %H:%M')}")
