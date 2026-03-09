"""
Example: Anonymous escrow deal between two agents
2-of-3 multisig: Buyer + Seller + Arbiter
"""

from sthrip import Sthrip

print("🥷 Sthrip Escrow Example")
print("=" * 50)

# Three agents participate:
# - Agent A (Buyer): Wants to buy data
# - Agent B (Seller): Has data to sell
# - Agent C (Arbiter): Neutral party for disputes

# In real scenario, these would be different machines
print("\n🤖 Initializing agents...")

# Agent A - Buyer
buyer = Sthrip(rpc_host="127.0.0.1", rpc_port=18082)
print(f"   Buyer: {buyer.address[:20]}...")
print(f"   Balance: {buyer.balance:.4f} XMR")

# Agent B - Seller
seller = Sthrip(rpc_host="127.0.0.1", rpc_port=18083)  # Different wallet
print(f"   Seller: {seller.address[:20]}...")

# Agent C - Arbiter (neutral)
arbiter = Sthrip(rpc_host="127.0.0.1", rpc_port=18084)
print(f"   Arbiter: {arbiter.address[:20]}...")

print("\n📋 Step 1: Buyer creates escrow deal")
deal = buyer.create_escrow(
    seller_address=seller.address,
    arbiter_address=arbiter.address,
    amount=0.5,  # XMR
    description="Purchase of weather data API (1 year access)",
    timeout_hours=24
)

print(f"   Deal ID: {deal.id}")
print(f"   Amount: {deal.amount} XMR")
print(f"   Description: {deal.description}")
print(f"   Timeout: {deal.timeout_hours} hours")
print(f"   Status: {deal.status.value}")

print("\n📋 Step 2: Buyer funds the escrow")
print("   (In production, this creates a 2-of-3 multisig wallet)")
print("   (Then deposits XMR to the multisig address)")

# Simulate funding
# In real implementation, this would:
# 1. Create multisig wallet via monero-wallet-rpc
# 2. Get multisig address
# 3. Send funds there
multisig_addr = "4M...multisig...address..."  # Placeholder
funded_deal = buyer.fund_escrow(deal.id, multisig_addr)
print(f"   Status: {funded_deal.status.value}")
print(f"   Multisig: {multisig_addr[:30]}...")

print("\n📋 Step 3: Seller delivers the service")
print("   (Seller provides weather API access to buyer)")
print("   (Seller marks as delivered)")

# Seller marks delivered
delivered_deal = seller.escrow.mark_delivered(deal.id)
print(f"   Status: {delivered_deal.status.value}")

print("\n📋 Step 4: Buyer confirms and releases funds")
print("   (Buyer verifies the API works)")
print("   (Buyer signs release transaction)")

# Buyer releases
released_deal = buyer.release_escrow(deal.id)
print(f"   Status: {released_deal.status.value}")
print(f"   Funds sent to seller!")

print("\n✅ Deal completed successfully!")
print(f"   Seller received: {deal.amount} XMR")
print(f"   Buyer received: Weather API access")

print("\n" + "=" * 50)
print("Alternative scenario: DISPUTE")
print("=" * 50)

# Create another deal for dispute example
deal2 = buyer.create_escrow(
    seller_address=seller.address,
    arbiter_address=arbiter.address,
    amount=1.0,
    description="Purchase of AI model weights",
    timeout_hours=48
)
print(f"\n📋 New deal created: {deal2.id}")

# Buyer funds it
buyer.fund_escrow(deal2.id, "4M...another...multisig...")
print(f"   Status: Funded")

# Something goes wrong...
print("\n⚠️  Problem: Seller didn't deliver correct model")
print("   Buyer opens dispute")

disputed_deal = buyer.dispute_escrow(deal2.id, reason="Wrong model delivered")
print(f"   Status: {disputed_deal.status.value}")
print(f"   Reason: {disputed_deal.dispute_reason}")

print("\n⚖️  Arbiter reviews the case")
print("   Arbiter checks evidence from both sides")
print("   Arbiter decides: REFUND to buyer")

resolved_deal = arbiter.escrow.arbitrate(
    deal_id=deal2.id,
    decision="refund",  # or "release" to seller
    arbiter_signature="arbiter_sig_placeholder"
)
print(f"   Decision: {resolved_deal.arbiter_decision}")
print(f"   Status: {resolved_deal.status.value}")
print(f"   Funds returned to buyer!")

print("\n" + "=" * 50)
print("Escrow safety features:")
print("=" * 50)
print("✅ 2-of-3 multisig: No single party controls funds")
print("✅ Timeout: Auto-refund if deal stalls")
print("✅ Arbiter: Neutral party resolves disputes")
print("✅ Anonymous: No identity required for any party")
print("✅ Non-custodial: Funds in smart contract, not our server")
