"""
Maximum privacy example - all techniques combined
"""

from stealthpay import StealthPay
from stealthpay.privacy import PrivacyConfig, TransactionTiming, PrivacyEnhancer

print("🥷 Maximum Privacy Mode")
print("=" * 50)

# Configure for maximum privacy
config = PrivacyConfig(
    min_mixin=15,           # Large ring size
    max_mixin=25,           # Randomize each time
    timing_strategy=TransactionTiming.NIGHT_TIME,  # Send at 2-4 AM
    auto_rotate_addresses=True,
    max_reuse_count=0,      # Never reuse
    use_decoy_change=True,
    decoy_amount_variance=0.01
)

enhancer = PrivacyEnhancer(config)

# Initialize agent with privacy settings
agent = StealthPay(
    rpc_host="127.0.0.1",
    rpc_port=18082,
    privacy_config=config  # Pass to client
)

print("\n🔒 Privacy Configuration:")
print(f"   Ring size: {config.min_mixin}-{config.max_mixin}")
print(f"   Timing: {config.timing_strategy.value}")
print(f"   Address reuse: Not allowed")
print(f"   Decoy amounts: Enabled")

# Step 1: Churn funds (send to self 3 times)
print("\n🔄 Step 1: Churning funds (breaking chain)")
churn_amounts = enhancer.create_churn_transaction(1.0, rounds=3)
print(f"   Will create {len(churn_amounts)} intermediate transactions")
for i, amt in enumerate(churn_amounts):
    print(f"   Round {i+1}: {amt:.6f} XMR")
    # In production: actually send these with delays

# Step 2: Create fresh stealth address
print("\n🎭 Step 2: Generating fresh stealth address")
stealth = agent.create_stealth_address(purpose="max-privacy-payment")
print(f"   Address: {stealth.address[:30]}...")
print(f"   Index: {stealth.index}")

# Step 3: Calculate optimal delay
print("\n⏱️  Step 3: Calculating broadcast time")
delay_seconds = enhancer.calculate_delay()
broadcast_time = datetime.now() + timedelta(seconds=delay_seconds)
print(f"   Delay: {delay_seconds/3600:.1f} hours")
print(f"   Will broadcast at: {broadcast_time.strftime('%Y-%m-%d %H:%M')}")

# Step 4: Randomize mixin
mixin = enhancer.get_optimal_mixin()
print(f"\n🎲 Step 4: Ring size randomized to {mixin}")

# Step 5: Obfuscate amount slightly
actual_amount = 0.5
obfuscated = enhancer.obfuscate_amount(actual_amount)
print(f"\n💰 Step 5: Amount obfuscation")
print(f"   Intended: {actual_amount} XMR")
print(f"   Actual:   {obfuscated:.8f} XMR")

# Step 6: Add decoy output
print("\n🎪 Step 6: Adding decoy output")
decoy = enhancer.generate_decoy_output()
if decoy:
    print(f"   Decoy amount: {decoy:.6f} XMR to random address")
else:
    print("   No decoy this time (random)")

print("\n✅ Privacy Score:")
from stealthpay.privacy import calculate_privacy_score
score = calculate_privacy_score(
    mixin=mixin,
    timing_variance=delay_seconds/3600,
    address_reuse=False,
    decoy_outputs=1 if decoy else 0
)
print(f"   {score}/100 - {'Excellent' if score > 80 else 'Good' if score > 60 else 'Fair'}")

print("\n⚠️  Note: In production, transaction will be delayed.")
print("   Use agent.scheduler to queue transactions.")
