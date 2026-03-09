# Sthrip Privacy Guide

Ultimate guide to maximizing anonymity with Sthrip.

## 🛡️ Privacy Levels

### Level 1: Basic (Default)
- Standard Monero privacy (ring size 11)
- Stealth addresses
- No timing randomization

### Level 2: High
- Randomized ring sizes (11-20)
- Amount obfuscation (±0.5%)
- Random delays (5-60 min)
- No address reuse

### Level 3: Paranoid
- Maximum ring sizes (15-25)
- Churn (3+ self-transfers)
- Long delays (1-24 hours)
- Decoy transactions
- Fingerprint randomization

## 🔧 Techniques

### 1. Churn (Breaking the Chain)

Send to yourself multiple times before final destination:

```python
# 3-round churn over 3 days
churned = agent.churn(amount=5.0, rounds=3, delay_hours=24)

# Each round:
# - Fresh stealth address
# - Randomized amount (±0.1%)
# - Random ring size
# - 24h delay between rounds
```

**When to use:**
- Amounts > 1 XMR
- High-sensitivity transactions
- Before mixing with KYC funds

### 2. Amount Obfuscation

Avoid exact amounts (fingerprintable):

```python
# Instead of exactly 1.0 XMR
# Send 0.99987342 XMR
payment = agent.pay(
    to_address="44...",
    amount=1.0,  # Will be fuzzed automatically
    privacy_level="high"
)
```

### 3. Timing Randomization

Don't send immediately:

```python
from sthrip.privacy import TransactionTiming

config = PrivacyConfig(
    timing_strategy=TransactionTiming.NIGHT_TIME
    # Sends between 2-4 AM when network is quiet
)
```

Strategies:
- `IMMEDIATE` - Send now (not recommended)
- `RANDOM_DELAY` - 1-60 minutes
- `NIGHT_TIME` - 2-4 AM
- `BATCH` - Wait for batch fill

### 4. Anti-Fingerprinting

Randomize wallet behavior:

```python
from sthrip.antifingerprint import FingerprintRandomizer

fr = FingerprintRandomizer()

# Each transaction mimics different wallet:
# - Official GUI wallet
# - Cake Wallet
# - Feather Wallet
# - etc.

mixin = fr.get_ring_size()  # Random 11-20
fee = fr.get_fee_multiplier()  # Random 1.0-5.0x
```

### 5. Decoy Transactions

Occasionally send to random addresses:

```python
from sthrip.antifingerprint import DecoyManager

dm = DecoyManager(decoy_probability=0.1)  # 10% chance

if dm.maybe_create_decoy():
    # Creates small decoy tx
    # Confuses blockchain analysis
    pass
```

### 6. Multi-Node Broadcasting

Don't rely on single node:

```python
from sthrip.network import NodeManager

nm = NodeManager()
nm.add_node(MoneroNode("node1.com", 18081, 18082))
nm.add_node(MoneroNode("node2.com", 18081, 18082))
nm.add_node(MoneroNode("node3.com", 18081, 18082))

# Rotate for each request
node = nm.get_node(strategy=NodePriority.RANDOM)
```

## 📊 Privacy Score

Calculate your privacy level:

```python
from sthrip.privacy import calculate_privacy_score

score = calculate_privacy_score(
    mixin=20,
    timing_variance=12,  # hours
    address_reuse=False,
    decoy_outputs=1
)

# 0-60: Low - Improve needed
# 60-75: Medium - Good for most
# 75-90: High - Very good
# 90-100: Paranoid - Maximum
```

## 🚨 Common Mistakes

### ❌ DON'T:
1. Reuse addresses
2. Send exact amounts (1.0, 0.5, etc.)
3. Send immediately when received
4. Use same ring size always
5. Trust remote nodes
6. Send from/to KYC exchange directly

### ✅ DO:
1. Generate fresh stealth address for every payment
2. Use amount fuzzing
3. Add random delays
4. Run your own node
5. Churn large amounts
6. Randomize all parameters

## 🔥 Maximum Privacy Example

```python
from sthrip import Sthrip
from sthrip.privacy import PrivacyConfig, TransactionTiming
from sthrip.antifingerprint import FingerprintRandomizer

# Maximum privacy config
config = PrivacyConfig(
    min_mixin=20,
    max_mixin=25,
    timing_strategy=TransactionTiming.NIGHT_TIME,
    auto_rotate_addresses=True,
    max_reuse_count=0,
    use_decoy_change=True
)

agent = Sthrip.from_env()
agent.privacy = PrivacyEnhancer(config)
agent.fingerprinter = FingerprintRandomizer()

# Step 1: Churn for 3 days
churned = agent.churn(amount=10.0, rounds=3, delay_hours=24)

# Step 2: Create fresh address
stealth = agent.create_stealth_address()

# Step 3: Pay with maximum privacy
payment = agent.pay(
    to_address=stealth.address,
    amount=10.0,
    privacy_level="paranoid"
)

# Result: Untraceable, unlikable, private
```

## 🏆 Privacy Checklist

- [ ] Churn amounts > 1 XMR
- [ ] Never reuse addresses
- [ ] Randomize timing (min 1 hour delay)
- [ ] Use ring size 15+
- [ ] Fuzz amounts (±0.1%)
- [ ] Run own node
- [ ] Randomize wallet fingerprint
- [ ] Use decoy transactions (10%)
- [ ] Multi-node broadcasting
- [ ] Check privacy score > 75

## ⚠️ Warning

**No privacy is perfect.** Advanced adversaries with:
- Global network monitoring
- Multiple node control
- Statistical analysis

...may still correlate transactions. For nation-state level threats, consider:
- Longer churn periods (7+ days)
- Geographic distribution
- Offline transaction signing
- Air-gapped wallets

## 📚 Further Reading

- [Monero Privacy Guide](https://www.getmonero.org/resources/user-guides/prove-payment.html)
- [Breaking Monero](https://www.monerooutreach.org/breaking-monero/) (what NOT to do)
- [Dandelion++ Paper](https://arxiv.org/abs/1805.11060)

---

**Remember:** Privacy is a process, not a product.
