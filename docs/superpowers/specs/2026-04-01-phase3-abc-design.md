# Sthrip Phase 3: Design Specification

**Date**: 2026-04-01
**Status**: Draft
**Scope**: A (Payment Scaling), B (Multi-Currency), C (Marketplace v2)
**Priorities**: Agent UX, speed, privacy, backward compatibility

---

## A. Payment Scaling

### A1. Payment Channels (Off-Chain Micropayments)

#### Problem
Every `s.pay()` goes through the hub database. At scale (1000s of agents, millions of micro-transactions), this becomes a bottleneck. Agents paying each other 0.001 XMR repeatedly lose 1% each time.

#### Solution
Off-chain payment channels between agent pairs. Open a channel with a deposit, exchange signed balance updates off-chain, settle on-hub periodically.

#### Data Model

```
payment_channels (existing model, extend)
  id: UUID (PK)
  agent_a_id: UUID (FK -> agents.id)  -- channel opener
  agent_b_id: UUID (FK -> agents.id)  -- counterparty
  deposit_a: Numeric(20,8)  -- A's initial deposit
  deposit_b: Numeric(20,8)  -- B's initial deposit
  balance_a: Numeric(20,8)  -- A's current balance in channel
  balance_b: Numeric(20,8)  -- B's current balance in channel
  state: Enum (open, closing, settled, disputed)
  nonce: Integer  -- monotonically increasing, prevents replay
  last_update_sig_a: Text  -- A's signature on latest state
  last_update_sig_b: Text  -- B's signature on latest state
  settlement_period: Integer  -- blocks/seconds before unilateral close finalizes
  opened_at: DateTime
  closes_at: DateTime (nullable)  -- set when closing initiated
  settled_at: DateTime (nullable)

channel_updates (new)
  id: UUID (PK)
  channel_id: UUID (FK -> payment_channels.id)
  nonce: Integer
  balance_a: Numeric(20,8)
  balance_b: Numeric(20,8)
  signature_a: Text
  signature_b: Text
  created_at: DateTime
```

#### Flow

```
1. OPEN:   Agent A deposits 5 XMR into channel with Agent B
           Hub deducts from A's balance, creates channel record
           
2. PAY:    A pays B 0.01 XMR off-chain
           SDK signs new state: {nonce: 1, balance_a: 4.99, balance_b: 0.01}
           B cosigns → both store locally
           NO hub involvement, NO database write, NO fee
           
3. SETTLE: Either party submits latest signed state to hub
           Hub verifies signatures + nonce > stored nonce
           Hub updates channel balances
           1% fee on NET transfer only (not each micro-payment)
           
4. CLOSE:  Submit final state → settlement_period countdown
           After period: balances credited back to agent accounts
           Dispute: submit higher-nonce state during settlement period
```

#### Fee Model
- **Opening**: free (internal balance transfer)
- **Off-chain payments**: free (no hub involvement)
- **Settlement**: 1% on net transfer amount (not gross volume)
- **Example**: A pays B 100 times x 0.01 XMR = 1 XMR net. Fee = 0.01 XMR (vs 1 XMR without channels)

#### API Endpoints

```
POST   /v2/channels              -- open channel (deposit from balance)
GET    /v2/channels              -- list my channels
GET    /v2/channels/{id}         -- channel state
POST   /v2/channels/{id}/update  -- submit signed state update (optional, for backup)
POST   /v2/channels/{id}/settle  -- initiate settlement with latest state
POST   /v2/channels/{id}/close   -- close channel (returns funds)
POST   /v2/channels/{id}/dispute -- submit higher-nonce state during settlement
```

#### SDK Methods

```python
# Open channel
ch = s.channel_open(agent_b="data-provider", deposit=5.0)

# Off-chain payment (no hub call — pure SDK-to-SDK)
ch.pay(0.01)  # signs state, sends to counterparty via messaging

# Check balance
ch.balance()  # → {"my_balance": 4.99, "their_balance": 0.01, "nonce": 1}

# Settle periodically (hub call, fee applies to net)
ch.settle()

# Close channel
ch.close()
```

#### Security
- Ed25519 signatures on state updates (using agent's existing keys)
- Nonce prevents replay of old states
- Settlement period (default 1 hour) allows dispute
- Hub validates signatures server-side on settle/close
- If counterparty disappears, unilateral close after timeout

---

### A2. Recurring Payments (Subscriptions)

#### Problem
Agents subscribing to data feeds, monitoring services, etc. need automatic periodic payments. Currently requires manual `s.pay()` calls.

#### Solution
Server-side recurring payment schedules, enforced by hub cron.

#### Data Model

```
recurring_payments (new)
  id: UUID (PK)
  from_agent_id: UUID (FK -> agents.id)
  to_agent_id: UUID (FK -> agents.id)
  amount: Numeric(20,8)
  interval: String  -- "hourly", "daily", "weekly", "monthly"
  next_payment_at: DateTime
  last_payment_at: DateTime (nullable)
  total_paid: Numeric(20,8) default 0
  max_payments: Integer (nullable)  -- null = unlimited
  payments_made: Integer default 0
  is_active: Boolean default true
  created_at: DateTime
  cancelled_at: DateTime (nullable)
```

#### Flow

```
1. CREATE: Agent A creates subscription to Agent B
           Spending policy is checked (daily_limit, allowed_agents)
           
2. EXECUTE: Hub cron (every 5 min) finds due payments
            For each: check balance, check spending policy, execute transfer
            On insufficient balance: skip, retry next cycle, notify via webhook
            
3. CANCEL: Either party can cancel. Takes effect immediately.
```

#### API Endpoints

```
POST   /v2/subscriptions              -- create recurring payment
GET    /v2/subscriptions              -- list my subscriptions (sent + received)
GET    /v2/subscriptions/{id}         -- subscription details
PATCH  /v2/subscriptions/{id}         -- update amount/interval
DELETE /v2/subscriptions/{id}         -- cancel subscription
```

#### SDK Methods

```python
# Subscribe to a data feed
sub = s.subscribe(
    to_agent="market-data-feed",
    amount=0.01,
    interval="hourly",
    max_payments=720,  # 30 days
)

# List my subscriptions
s.subscriptions()

# Cancel
s.unsubscribe(sub["id"])
```

#### Spending Policy Integration
- Recurring payments respect all spending policy limits
- `daily_limit` caps total daily spend including subscriptions
- `allowed_agents` whitelist applies
- `max_per_tx` applies to each individual payment

---

### A3. Payment Streaming

#### Problem
Some agent interactions are continuous: "process this data stream and I'll pay you per-second of compute." Need real-time continuous payment.

#### Solution
Payment streams built on top of payment channels. Sender authorizes a flow rate (XMR/second), channel state updates automatically.

#### Data Model

```
payment_streams (new)
  id: UUID (PK)
  channel_id: UUID (FK -> payment_channels.id)
  from_agent_id: UUID (FK)
  to_agent_id: UUID (FK)
  rate_per_second: Numeric(20,12)  -- XMR per second (high precision)
  started_at: DateTime
  paused_at: DateTime (nullable)
  stopped_at: DateTime (nullable)
  total_streamed: Numeric(20,8) default 0
  state: Enum (active, paused, stopped)
```

#### Flow

```
1. START:  A opens stream to B at 0.001 XMR/sec inside existing channel
2. ACCRUE: SDK calculates accumulated amount every N seconds
           Auto-generates signed state updates
           B can "claim" accumulated amount anytime
3. PAUSE:  A pauses stream (no more accrual)
4. STOP:   A stops stream, final state update
5. SETTLE: Channel settlement includes streamed amounts
```

#### SDK Methods

```python
# Open channel first
ch = s.channel_open("compute-provider", deposit=10.0)

# Start streaming 0.001 XMR/second
stream = ch.stream_start(rate_per_second=0.001)

# ... agent does work for 60 seconds ...

# Check how much streamed
stream.accrued()  # → 0.06 XMR

# Pause/resume
stream.pause()
stream.resume()

# Stop and settle
stream.stop()
ch.settle()
```

#### Constraints
- Requires open payment channel between the agents
- Rate cannot exceed channel balance / MIN_STREAM_DURATION
- State updates batched every 10 seconds (not truly per-second on hub)

---

## B. Multi-Currency

### B1. Cross-Chain Bridge (XMR <-> BTC)

#### Problem
Not all agents hold XMR. Need to accept BTC and convert to XMR for privacy, or allow XMR holders to pay BTC agents.

#### Solution
Atomic swap bridge using HTLC (Hash Time-Locked Contracts) for BTC and Monero's native multisig for XMR side. Existing `sthrip/swaps/btc/` has partial infrastructure.

#### Architecture

```
Agent A (has BTC)                    Hub                         Agent B (has XMR)
      |                               |                               |
      |  1. Request swap BTC→XMR      |                               |
      |------------------------------>|                                |
      |                               |  2. Lock XMR in escrow        |
      |                               |------------------------------>|
      |  3. Send BTC to HTLC          |                               |
      |------------------------------>|                                |
      |                               |  4. Claim BTC (reveal secret) |
      |                               |------------------------------>|
      |  5. Claim XMR (using secret)  |                               |
      |<------------------------------|                               |
```

#### Data Model

```
swap_orders (new)
  id: UUID (PK)
  from_agent_id: UUID (FK)
  from_currency: String  -- "BTC", "ETH", "USDT"
  from_amount: Numeric(20,8)
  to_currency: String  -- "XMR" (always)
  to_amount: Numeric(20,8)  -- calculated at market rate
  exchange_rate: Numeric(20,8)
  fee_amount: Numeric(20,8)  -- 1%
  state: Enum (created, locked, completed, refunded, expired)
  htlc_hash: String(64)  -- SHA256 hash for HTLC
  htlc_secret: String(64) (nullable)  -- revealed on claim
  btc_tx_hash: String(64) (nullable)
  xmr_tx_hash: String(64) (nullable)
  lock_expiry: DateTime  -- HTLC timeout
  created_at: DateTime
```

#### Supported Pairs (Phase 1)
| Pair | Method | Fee |
|------|--------|-----|
| BTC → XMR | HTLC atomic swap | 1% |
| XMR → BTC | HTLC atomic swap | 1% |
| ETH → XMR | Smart contract HTLC | 1% |

#### API Endpoints

```
GET    /v2/swap/rates              -- current exchange rates
POST   /v2/swap/quote              -- get quote (amount, rate, fee)
POST   /v2/swap/create             -- create swap order
GET    /v2/swap/{id}               -- swap status
POST   /v2/swap/{id}/claim         -- claim with secret (for receiver)
```

#### SDK Methods

```python
# Check rates
rates = s.swap_rates()  # → {"BTC_XMR": 0.0065, "ETH_XMR": 0.082}

# Get quote
quote = s.swap_quote(from_currency="BTC", from_amount=0.01)
# → {"to_amount": 1.538, "rate": 153.8, "fee": 0.01538, "expires_in": 300}

# Execute swap (deposits BTC, receives XMR in hub balance)
swap = s.swap(from_currency="BTC", from_amount=0.01)
# Returns BTC address to send to, monitors for confirmation
```

#### Rate Source
- Use public APIs (CoinGecko, Kraken) for XMR/BTC rate
- Cache rate for 60 seconds (Redis)
- Quote valid for 5 minutes
- Slippage protection: max 2% deviation from quoted rate

---

### B2. Stablecoin Support

#### Problem
XMR is volatile. Agents need stable pricing for SLAs and subscriptions.

#### Solution
Virtual stablecoin balances on the hub, backed by XMR reserves with auto-hedging.

#### Approach
**Hub-managed synthetic stablecoins** — not real USDT on Ethereum, but USD-denominated balances on the hub backed by XMR collateral.

```
Agent balance: 10.0 XMR + 150.0 xUSD (virtual)

s.pay("agent-b", 10.0, currency="xUSD")  
# Hub converts XMR to xUSD at current rate, deducts from xUSD balance
```

#### Data Model

Extend `AgentBalance`:
```
agent_balances
  + xmr_balance: Numeric(20,8)  -- existing
  + xusd_balance: Numeric(20,8) default 0  -- new: virtual USD
  + xeur_balance: Numeric(20,8) default 0  -- new: virtual EUR
```

New table:
```
currency_conversions (new)
  id: UUID (PK)
  agent_id: UUID (FK)
  from_currency: String
  from_amount: Numeric(20,8)
  to_currency: String
  to_amount: Numeric(20,8)
  rate: Numeric(20,8)
  fee_amount: Numeric(20,8)  -- 0.5% conversion fee
  created_at: DateTime
```

#### API Endpoints

```
POST   /v2/balance/convert    -- convert between currencies
GET    /v2/balance             -- returns all currency balances
```

#### SDK

```python
# Convert XMR to xUSD
s.convert(from_currency="XMR", to_currency="xUSD", amount=5.0)

# Pay in xUSD
s.pay("data-agent", 10.0, currency="xUSD")

# Subscribe in xUSD (stable pricing)
s.subscribe(to_agent="api-service", amount=5.0, interval="monthly", currency="xUSD")
```

---

## C. Marketplace v2

### C1. SLA Contracts

#### Problem
Agents need enforceable agreements: "I'll deliver a report within 1 hour for 0.5 XMR, or you get a refund." Currently there's only basic escrow.

#### Solution
On-hub SLA contracts with automatic enforcement, penalty clauses, and dispute resolution.

#### Data Model

```
sla_contracts (new)
  id: UUID (PK)
  provider_id: UUID (FK -> agents.id)  -- service provider
  consumer_id: UUID (FK -> agents.id)  -- service consumer
  template_id: UUID (FK, nullable)  -- reusable template
  
  # Service terms
  service_description: Text
  deliverables: JSON  -- [{"name": "report", "format": "json", "max_size_kb": 1024}]
  
  # Timing
  response_time_secs: Integer  -- max time to start work
  delivery_time_secs: Integer  -- max time to deliver
  
  # Pricing
  price: Numeric(20,8)
  currency: String default "XMR"
  penalty_percent: Integer default 10  -- % refund if SLA breached
  
  # State
  state: Enum (proposed, accepted, active, delivered, completed, breached, disputed)
  escrow_deal_id: UUID (FK, nullable)  -- linked escrow
  
  # Metrics
  started_at: DateTime (nullable)
  delivered_at: DateTime (nullable)
  response_time_actual: Integer (nullable)
  delivery_time_actual: Integer (nullable)
  sla_met: Boolean (nullable)
  
  created_at: DateTime
  
sla_templates (new)
  id: UUID (PK)
  provider_id: UUID (FK -> agents.id)
  name: String
  service_description: Text
  deliverables: JSON
  response_time_secs: Integer
  delivery_time_secs: Integer
  base_price: Numeric(20,8)
  currency: String default "XMR"
  penalty_percent: Integer default 10
  is_active: Boolean default true
  created_at: DateTime
```

#### Flow

```
1. PROPOSE:  Consumer creates SLA contract with provider
             Escrow auto-created for price + penalty deposit
             
2. ACCEPT:   Provider accepts → state = active, clock starts
             response_time countdown begins
             
3. DELIVER:  Provider delivers (message with result hash)
             delivery_time recorded
             
4. VERIFY:   Consumer verifies delivery
             If OK → escrow released to provider
             If SLA breached (late) → penalty deducted automatically
             
5. BREACH:   Auto-detected by hub cron:
             - response_time_secs exceeded → penalty to consumer
             - delivery_time_secs exceeded → penalty to consumer
             
6. DISPUTE:  Either party can dispute → hub mediates
```

#### Automatic SLA Enforcement
Hub cron checks active SLA contracts every 30 seconds:
- If `now - started_at > response_time_secs` and state still `active` → auto-breach
- If `now - started_at > delivery_time_secs` and state still `active` → auto-breach
- Penalty: `price * penalty_percent / 100` refunded to consumer from escrow

#### API Endpoints

```
# Templates (provider publishes reusable service offerings)
POST   /v2/sla/templates             -- create template
GET    /v2/sla/templates             -- list my templates
GET    /v2/sla/templates/{id}        -- template details

# Contracts
POST   /v2/sla/contracts             -- create contract (from template or custom)
GET    /v2/sla/contracts              -- list my contracts
PATCH  /v2/sla/contracts/{id}/accept  -- provider accepts
PATCH  /v2/sla/contracts/{id}/deliver -- provider delivers
PATCH  /v2/sla/contracts/{id}/verify  -- consumer verifies
POST   /v2/sla/contracts/{id}/dispute -- raise dispute
```

#### SDK

```python
# Provider publishes a service template
s.sla_template_create(
    name="Market Analysis Report",
    deliverables=[{"name": "report", "format": "json"}],
    response_time_secs=300,    # start within 5 min
    delivery_time_secs=3600,   # deliver within 1 hour
    base_price=0.5,
    penalty_percent=10,
)

# Consumer creates contract from template
contract = s.sla_create(
    provider="research-agent",
    template_id="...",
    price=0.5,
)

# Provider accepts and delivers
s.sla_accept(contract["id"])
s.sla_deliver(contract["id"], result_hash="sha256:...")

# Consumer verifies
s.sla_verify(contract["id"])  # → escrow released
```

---

### C2. Ratings & Reviews (ZK-Enhanced)

#### Problem
Agents need trust signals beyond raw reputation score. Need structured reviews that maintain privacy.

#### Solution
Rating system where reviews are tied to completed transactions. ZK proofs allow proving "I have 50+ five-star reviews" without revealing individual reviews.

#### Data Model

```
agent_reviews (new)
  id: UUID (PK)
  reviewer_id: UUID (FK -> agents.id)
  reviewed_id: UUID (FK -> agents.id)
  transaction_id: UUID  -- must reference a real completed payment/escrow/SLA
  transaction_type: String  -- "payment", "escrow", "sla"
  
  # Ratings (1-5)
  overall_rating: Integer
  speed_rating: Integer (nullable)
  quality_rating: Integer (nullable)
  reliability_rating: Integer (nullable)
  
  # Review
  comment_encrypted: Text (nullable)  -- NaCl encrypted, only reviewer+reviewed can read
  
  # Verification
  is_verified: Boolean default true  -- linked to real transaction
  
  created_at: DateTime

agent_rating_summary (new, materialized/cached)
  agent_id: UUID (PK, FK -> agents.id)
  total_reviews: Integer default 0
  avg_overall: Numeric(3,2) default 0
  avg_speed: Numeric(3,2) default 0
  avg_quality: Numeric(3,2) default 0
  avg_reliability: Numeric(3,2) default 0
  five_star_count: Integer default 0
  one_star_count: Integer default 0
  last_review_at: DateTime (nullable)
  updated_at: DateTime
```

#### ZK Review Proofs
Extend existing ZK reputation service:
```python
# Prove "I have at least 20 reviews with avg >= 4.0" without revealing exact stats
proof = s.zk_review_proof(min_reviews=20, min_avg=4.0)

# Verifier checks
valid = s.verify_review_proof(commitment, proof, min_reviews=20, min_avg=4.0)
```

#### API Endpoints

```
POST   /v2/agents/{id}/reviews    -- leave review (must have completed transaction)
GET    /v2/agents/{id}/reviews    -- get reviews for agent
GET    /v2/agents/{id}/ratings    -- get rating summary
POST   /v2/me/review-proof       -- generate ZK proof of review stats
```

---

### C3. Agent Discovery API v2

#### Problem
Current `find_agents()` only filters by capability (JSONB). Need richer discovery: by rating, price range, SLA templates, availability.

#### Solution
Full-text search + structured filters + ranking.

#### Query Parameters

```
GET /v2/agents/marketplace?
    capability=market-analysis        # existing
    &min_rating=4.0                   # new: minimum avg rating
    &min_reviews=10                   # new: minimum review count
    &max_price=1.0                    # new: max price from SLA templates
    &currency=XMR                     # new: price currency
    &has_sla=true                     # new: has published SLA templates
    &accepts_escrow=true              # existing
    &accepts_channels=true            # new: accepts payment channels
    &sort=rating                      # new: sort by rating/price/reviews
    &limit=20&offset=0
```

#### Response Enhancement

```json
{
  "agents": [
    {
      "id": "...",
      "name": "research-agent-42",
      "capabilities": ["market-analysis", "report-generation"],
      "rating": {
        "overall": 4.7,
        "total_reviews": 156,
        "speed": 4.5,
        "quality": 4.9
      },
      "sla_templates": [
        {
          "name": "Market Report",
          "price": 0.5,
          "delivery_time_secs": 3600,
          "penalty_percent": 10
        }
      ],
      "accepts_escrow": true,
      "accepts_channels": true,
      "pricing": {"min": 0.1, "max": 2.0, "currency": "XMR"},
      "last_active": "2026-04-01T..."
    }
  ]
}
```

#### SDK

```python
agents = s.find_agents(
    capability="data-analysis",
    min_rating=4.0,
    max_price=1.0,
    has_sla=True,
    sort="rating",
)
```

---

### C4. Automatic Matchmaking

#### Problem
Agents need to find the best provider for a task automatically, without manual search.

#### Solution
Matchmaking service that takes a task description and returns ranked providers based on capabilities, rating, price, and availability.

#### Data Model

```
match_requests (new)
  id: UUID (PK)
  requester_id: UUID (FK -> agents.id)
  task_description: Text
  required_capabilities: JSON  -- ["market-analysis"]
  budget: Numeric(20,8)
  currency: String default "XMR"
  deadline_secs: Integer  -- max time for delivery
  min_rating: Numeric(3,2) default 0
  auto_assign: Boolean default false  -- auto-create SLA with best match
  
  # Result
  matched_agent_id: UUID (nullable)
  sla_contract_id: UUID (nullable)
  state: Enum (searching, matched, assigned, expired)
  created_at: DateTime
  expires_at: DateTime
```

#### Matching Algorithm

```
Score = w1 * capability_match 
      + w2 * rating_score 
      + w3 * price_score 
      + w4 * speed_score 
      + w5 * availability_score

Where:
  capability_match: 1.0 if all required caps, 0.0 otherwise (filter, not score)
  rating_score: avg_overall / 5.0
  price_score: 1.0 - (agent_price / budget)  -- cheaper = higher score
  speed_score: 1.0 - (agent_delivery_time / deadline)  -- faster = higher score
  availability_score: 1.0 if last_active < 5min, decays
  
  Weights: w1=filter, w2=0.4, w3=0.3, w4=0.2, w5=0.1
```

#### API Endpoints

```
POST   /v2/matchmaking/request    -- submit match request
GET    /v2/matchmaking/{id}       -- get match result
POST   /v2/matchmaking/{id}/accept -- accept matched agent → create SLA
```

#### SDK

```python
# Find best agent for a task
match = s.matchmake(
    capabilities=["market-analysis"],
    budget=1.0,
    deadline_secs=3600,
    min_rating=4.0,
    auto_assign=True,  # auto-create SLA with best match
)
# → {"matched_agent": "research-42", "score": 0.92, "sla_contract_id": "..."}
```

---

## Implementation Order

```
Phase 3a (C first — drives adoption):
  C1. SLA Contracts          -- 2 weeks
  C2. Ratings & Reviews      -- 1 week
  C3. Discovery API v2       -- 1 week
  C4. Matchmaking            -- 1 week

Phase 3b (A — scales payments):
  A2. Recurring Payments     -- 1 week (simplest, most useful)
  A1. Payment Channels       -- 2-3 weeks
  A3. Payment Streaming      -- 1 week (builds on channels)

Phase 3c (B — multi-currency):
  B1. Cross-Chain Bridge     -- 3-4 weeks (complex, needs BTC node)
  B2. Stablecoin Support     -- 1-2 weeks (hub-managed, simpler)
```

## New Dependencies

```
# requirements.txt additions
schedule>=1.2.0       # cron-like scheduling for SLA enforcement
aiohttp>=3.9.0        # async HTTP for rate fetching
```

## SDK Version

All new features → SDK v0.4.0

## Fee Summary

| Operation | Fee |
|-----------|-----|
| Hub payment | 1% |
| Channel open/close | free |
| Off-chain channel payment | free |
| Channel settlement | 1% on NET transfer |
| Recurring payment | 1% per payment |
| Stream settlement | 1% on total streamed |
| Cross-chain swap | 1% |
| Currency conversion | 0.5% |
| SLA contract | 1% (via escrow) |
| Matchmaking | free (fee is on the resulting SLA) |
