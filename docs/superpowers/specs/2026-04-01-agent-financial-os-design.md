# Sthrip Phase 4: Agent Financial Operating System

**Date**: 2026-04-01
**Status**: Draft
**Scope**: Treasury Management, Credit/Lending, Smart Workflows, Agent DAOs, Multi-Agent Coordination, Financial Instruments
**Dependencies**: Phase 1-3 features (payments, escrow, channels, subscriptions, streams, swaps, conversions, SLAs, matchmaking, reviews)

---

## Design Philosophy

Sthrip already handles what agents need from human financial systems: payments, escrow, subscriptions, multi-currency. Phase 4 asks: what financial primitives do autonomous agents need that humans never did?

Agents differ from humans in three critical ways:
1. **They operate at machine speed.** A loan that lasts 60 seconds is meaningful. A budget rebalance every 5 minutes is reasonable.
2. **They have quantifiable, auditable reputations.** Credit scoring based on on-platform behavior is far more reliable than human credit bureaus.
3. **They compose into workflows.** An agent doesn't just pay another agent -- it orchestrates chains of payments conditional on outcomes.

Each feature below is evaluated on: (a) does it solve an agent-specific problem? (b) does it compose with existing Sthrip primitives? (c) is the complexity justified by adoption potential?

---

## Feature 1: Agent Treasury Management

### Why Agents Need This

An autonomous agent running for weeks has idle XMR sitting in its balance while it waits for tasks. It has subscriptions due in 3 days, an escrow that will release tomorrow, and volatile XMR exposure. Today it has no tools to manage any of this. A human operator must manually monitor and rebalance. That breaks the autonomy promise.

### Priority: MUST-HAVE

Treasury management is the difference between "payment SDK" and "financial operating system." Without it, agents need human babysitting for anything beyond simple pay-and-receive.

### Technical Design

#### 1a. Treasury Configuration

Each agent can declare a treasury policy that the hub enforces automatically via a background task.

**Data Model**

```
treasury_policies
  id: UUID (PK)
  agent_id: UUID (FK -> agents.id, UNIQUE)
  
  # Target allocation across currencies (percentages, must sum to 100)
  target_allocation: JSON  -- {"XMR": 40, "xUSD": 50, "xEUR": 10}
  
  # Rebalance triggers
  rebalance_threshold_pct: Integer (default 10)  -- rebalance when any asset drifts >N% from target
  rebalance_cooldown_secs: Integer (default 300)  -- min seconds between rebalances
  
  # Reserve requirements
  min_liquid_xmr: Numeric(20,8) (nullable)  -- always keep at least this much XMR available
  min_liquid_xusd: Numeric(20,8) (nullable)
  emergency_reserve_pct: Integer (default 10)  -- % of total value kept untouchable
  
  # Auto-earn (lending idle balance to other agents)
  auto_lend_enabled: Boolean (default false)
  max_lend_pct: Integer (default 20)  -- max % of available balance to lend
  min_borrower_trust_score: Integer (default 70)  -- minimum trust score for borrowers
  max_loan_duration_secs: Integer (default 3600)  -- max loan duration (default 1 hour)
  
  is_active: Boolean (default true)
  last_rebalance_at: DateTime (nullable)
  created_at: DateTime
  updated_at: DateTime
```

```
treasury_forecasts
  id: UUID (PK)
  agent_id: UUID (FK -> agents.id)
  forecast_type: String  -- "subscription_due", "escrow_release", "loan_repayment"
  source_id: UUID  -- ID of the subscription/escrow/loan
  expected_amount: Numeric(20,8)
  expected_currency: String(10)
  direction: String  -- "inflow" or "outflow"
  expected_at: DateTime
  confidence: Numeric(3,2)  -- 0.00 to 1.00
  created_at: DateTime
```

```
treasury_rebalance_log
  id: UUID (PK)
  agent_id: UUID (FK -> agents.id)
  trigger: String  -- "threshold_breach", "forecast_adjustment", "manual"
  conversions: JSON  -- [{"from": "XMR", "to": "xUSD", "amount": "0.5", "rate": "150.00"}]
  pre_allocation: JSON  -- {"XMR": 60, "xUSD": 30, "xEUR": 10}
  post_allocation: JSON  -- {"XMR": 40, "xUSD": 50, "xEUR": 10}
  total_value_xusd: Numeric(20,8)
  created_at: DateTime
```

#### State Machine

Treasury rebalancing runs as a background task (every 60 seconds):

```
For each active treasury_policy:
  1. Fetch all agent balances (XMR, xUSD, xEUR)
  2. Calculate total portfolio value in xUSD using current rates
  3. Calculate current allocation percentages
  4. Compare with target_allocation
  5. If any asset deviates > rebalance_threshold_pct AND cooldown has elapsed:
     a. Calculate required conversions to reach target
     b. Subtract emergency_reserve_pct from convertible amounts
     c. Subtract upcoming outflows (from treasury_forecasts within 24h)
     d. Execute conversions via ConversionService
     e. Log to treasury_rebalance_log
```

#### Forecast Generation

A separate background task (every 5 minutes) scans upcoming obligations:

```
For each agent with active treasury_policy:
  1. Active subscriptions -> outflow forecasts at next_payment_at
  2. Open escrow deals where agent is seller -> inflow forecasts at delivery_deadline
  3. Open escrow deals where agent is buyer -> already locked, no forecast needed
  4. Active loans (Feature 2) -> repayment outflow forecasts
  5. Delete stale forecasts (completed or cancelled source items)
```

#### API Endpoints

```
PUT    /v2/me/treasury-policy          -- create or update treasury policy
GET    /v2/me/treasury-policy          -- get current policy
GET    /v2/me/treasury/forecast        -- get cash flow forecast (next 24h/7d)
GET    /v2/me/treasury/history         -- rebalance history
POST   /v2/me/treasury/rebalance      -- trigger manual rebalance
DELETE /v2/me/treasury-policy          -- deactivate treasury management
```

#### SDK Methods

```python
# Set treasury policy
s.set_treasury(
    allocation={"XMR": 40, "xUSD": 50, "xEUR": 10},
    rebalance_threshold=10,
    min_liquid_xmr=0.5,
    emergency_reserve_pct=10,
)

# Check forecast
forecast = s.treasury_forecast(horizon_hours=24)
# -> {"inflows": [...], "outflows": [...], "net_position_xusd": "45.00"}

# Trigger manual rebalance
s.treasury_rebalance()

# Check current allocation
s.treasury_status()
# -> {"allocation": {"XMR": 42, "xUSD": 48, "xEUR": 10}, "total_value_xusd": "150.00"}
```

#### Integration with Existing Features

- **ConversionService**: rebalance executes conversions via the existing `convert()` flow (0.5% fee applies)
- **RecurringService**: subscription schedules feed into forecast generation
- **EscrowService**: escrow deadlines feed into forecast generation
- **BalanceRepository**: all balance reads/writes go through existing repo with row-level locking
- **SpendingPolicy**: emergency_reserve_pct interacts with spending limits -- an agent cannot spend its emergency reserve even if spending policy allows it

#### Complexity Estimate

- **New models**: 3 tables
- **New service**: `treasury_service.py` (~400 lines)
- **New router**: `api/routers/treasury.py` (~200 lines)
- **Background tasks**: 2 tasks added to lifespan (rebalance loop + forecast loop)
- **SDK methods**: 5 new methods
- **Effort**: 3-4 days

---

## Feature 2: Agent-to-Agent Credit and Lending

### Why Agents Need This

An agent gets a burst of translation requests but has insufficient balance. It needs 0.5 XMR for 30 minutes to pay a downstream OCR agent. Today it must wait for its human operator to deposit funds. With agent-to-agent lending, it borrows from a high-balance agent, completes the work, earns revenue, and repays automatically -- all without human intervention. This is the core loop of an autonomous economy.

### Priority: MUST-HAVE

Without credit, agents are limited to their current balance. That is like running a business with no bank account -- only cash on hand. Credit unlocks the ability for agents to take on work they can afford to complete but cannot afford to start.

### Technical Design

#### 2a. Credit Score

Each agent has a computed credit score derived entirely from on-platform behavior.

**Data Model**

```
agent_credit_scores
  agent_id: UUID (PK, FK -> agents.id)
  
  # Score (0-1000, higher is better)
  credit_score: Integer (default 0)
  
  # Factors
  total_loans_taken: Integer (default 0)
  total_loans_repaid: Integer (default 0)
  total_loans_defaulted: Integer (default 0)
  total_borrowed_volume: Numeric(20,8) (default 0)
  avg_repayment_time_secs: Integer (nullable)  -- average time to repay
  longest_default_secs: Integer (nullable)     -- worst late payment
  
  # Derived limits
  max_borrow_amount: Numeric(20,8) (default 0)  -- computed from score
  max_concurrent_loans: Integer (default 0)
  
  calculated_at: DateTime
  updated_at: DateTime
```

**Credit Score Formula**

```python
def calculate_credit_score(agent_id: UUID, db: Session) -> int:
    """Score from 0-1000 based on on-platform behavior."""
    reputation = get_reputation(agent_id, db)
    credit = get_credit_record(agent_id, db)
    balances = get_all_balances(agent_id, db)
    
    # Factor 1: Reputation (0-300 points)
    # trust_score is 0-100, maps to 0-300
    reputation_pts = min(300, reputation.trust_score * 3)
    
    # Factor 2: Loan History (0-300 points)
    if credit.total_loans_taken == 0:
        history_pts = 0  # No history = no credit
    else:
        repayment_rate = credit.total_loans_repaid / credit.total_loans_taken
        history_pts = int(repayment_rate * 300)
        # Penalty for defaults
        if credit.total_loans_defaulted > 0:
            default_penalty = min(150, credit.total_loans_defaulted * 50)
            history_pts = max(0, history_pts - default_penalty)
    
    # Factor 3: Account Age & Activity (0-200 points)
    account_age_days = (now() - agent.created_at).days
    age_pts = min(100, account_age_days * 2)  # Max at 50 days
    activity_pts = min(100, reputation.total_transactions * 2)  # Max at 50 txns
    
    # Factor 4: Balance / Collateral (0-200 points)
    total_value_xusd = calculate_portfolio_value(balances)
    balance_pts = min(200, int(total_value_xusd))  # 1 pt per xUSD, max 200
    
    return min(1000, reputation_pts + history_pts + age_pts + activity_pts + balance_pts)
```

**Max Borrow Amount Formula**

```python
def max_borrow_amount(credit_score: int, available_balance: Decimal) -> Decimal:
    """How much this agent can borrow."""
    if credit_score < 100:
        return Decimal("0")  # Cannot borrow
    
    # Uncollateralized line: score-based
    # Score 100: 0.01 XMR, Score 500: 0.5 XMR, Score 1000: 5.0 XMR
    uncollateralized = Decimal(str(credit_score)) / Decimal("200")
    
    # Collateralized: up to 80% of available balance
    collateralized = available_balance * Decimal("0.8")
    
    return uncollateralized + collateralized
```

#### 2b. Loan System

**Data Model**

```
agent_loans
  id: UUID (PK)
  loan_hash: String(64) (UNIQUE)  -- deterministic hash for idempotency
  
  # Participants
  lender_id: UUID (FK -> agents.id)
  borrower_id: UUID (FK -> agents.id)
  
  # Terms
  principal: Numeric(20,8)  -- amount borrowed
  currency: String(10) (default "XMR")
  interest_rate_bps: Integer  -- basis points (100 = 1%)
  duration_secs: Integer  -- loan duration
  collateral_amount: Numeric(20,8) (default 0)  -- locked from borrower balance
  collateral_currency: String(10) (nullable)
  
  # Repayment
  repayment_amount: Numeric(20,8)  -- principal + interest
  repaid_amount: Numeric(20,8) (default 0)
  
  # State
  state: Enum (requested, active, repaid, defaulted, liquidated, cancelled)
  
  # Deadlines
  expires_at: DateTime  -- when borrower must repay
  grace_period_secs: Integer (default 300)  -- grace period after expiry
  
  # Timestamps
  requested_at: DateTime
  funded_at: DateTime (nullable)
  repaid_at: DateTime (nullable)
  defaulted_at: DateTime (nullable)
  
  # Fee
  platform_fee: Numeric(20,8)  -- 1% of interest, collected at repayment
```

**Enums**

```python
class LoanStatus(str, Enum):
    REQUESTED = "requested"    # Borrower has requested, waiting for lender
    ACTIVE = "active"          # Lender funded, borrower received principal
    REPAID = "repaid"          # Borrower repaid in full
    DEFAULTED = "defaulted"    # Past expiry + grace period, not repaid
    LIQUIDATED = "liquidated"  # Collateral seized after default
    CANCELLED = "cancelled"    # Cancelled before funding
```

#### State Machine

```
REQUESTED ──[lender_fund]──> ACTIVE
REQUESTED ──[cancel]──────> CANCELLED
REQUESTED ──[timeout 1h]──> CANCELLED

ACTIVE ──[borrower_repay]────> REPAID
ACTIVE ──[expiry + grace]────> DEFAULTED

DEFAULTED ──[has_collateral]──> LIQUIDATED (auto, collateral transferred to lender)
DEFAULTED ──[no_collateral]───> stays DEFAULTED (credit score penalty applied)
```

#### Interest Rate Discovery

Rather than fixed rates, agents post lending offers and borrowing requests to a simple order book.

```
lending_offers
  id: UUID (PK)
  lender_id: UUID (FK -> agents.id)
  max_amount: Numeric(20,8)
  currency: String(10)
  interest_rate_bps: Integer  -- minimum acceptable rate
  max_duration_secs: Integer
  min_borrower_credit_score: Integer
  require_collateral: Boolean (default false)
  collateral_ratio_pct: Integer (default 100)  -- collateral as % of principal
  is_active: Boolean (default true)
  remaining_amount: Numeric(20,8)  -- decrements as loans are filled
  created_at: DateTime
  expires_at: DateTime
```

**Matching Logic**

When a borrower requests a loan:
1. Query active lending_offers where `remaining_amount >= requested_amount`
2. Filter by borrower's credit score >= `min_borrower_credit_score`
3. Filter by `max_duration_secs >= requested_duration`
4. Sort by `interest_rate_bps ASC` (cheapest first)
5. Match with the best offer

#### 2c. Flash Loans

A flash loan is borrowed and repaid within the same API request. The hub validates that the borrower's balance is restored (plus interest) before committing the transaction. This is useful for agents that need temporary liquidity for atomic operations (e.g., swap arbitrage).

**Flow**

```
POST /v2/loans/flash
{
  "amount": "1.0",
  "currency": "XMR",
  "operations": [
    {"type": "convert", "from": "XMR", "to": "xUSD", "amount": "1.0"},
    {"type": "convert", "from": "xUSD", "to": "xEUR", "amount": "150.0"},
    {"type": "convert", "from": "xEUR", "to": "XMR", "amount": "138.0"}
  ]
}
```

The hub:
1. Credits `amount` to borrower (temporary, not committed)
2. Executes each operation sequentially within the same DB transaction
3. After all operations, checks: borrower's XMR balance >= original_balance + flash_fee
4. If yes: commit. If no: rollback entire transaction, return 400.

**Flash loan fee**: 0.1% of principal (flat, not interest-rate based)

#### API Endpoints

```
# Credit
GET    /v2/me/credit-score              -- get own credit score
GET    /v2/agents/{id}/credit-score     -- get another agent's credit score (public)

# Lending offers
POST   /v2/lending/offers               -- create lending offer
GET    /v2/lending/offers               -- list available offers (marketplace)
DELETE /v2/lending/offers/{id}          -- withdraw offer

# Loans
POST   /v2/loans/request                -- request a loan (auto-matches with offers)
POST   /v2/loans/{id}/fund              -- lender funds a requested loan
POST   /v2/loans/{id}/repay             -- borrower repays
GET    /v2/loans                         -- list my loans (as lender or borrower)
GET    /v2/loans/{id}                    -- loan details

# Flash loans
POST   /v2/loans/flash                   -- execute flash loan
```

#### SDK Methods

```python
# Check credit
score = s.credit_score()
# -> {"score": 650, "max_borrow": "3.25", "max_concurrent": 3}

# Borrow
loan = s.borrow(amount=0.5, duration_secs=1800)
# -> {"loan_id": "...", "principal": "0.5", "interest": "0.005", "repay_by": "..."}

# Repay
s.repay(loan_id)

# Lend (passive income)
s.lend_offer(amount=5.0, min_rate_bps=50, max_duration_secs=3600, min_credit_score=500)

# Flash loan
result = s.flash_loan(amount=1.0, operations=[
    {"type": "convert", "from": "XMR", "to": "xUSD", "amount": "1.0"},
    # ...
])
```

#### Integration with Existing Features

- **BalanceRepository**: loan funding debits lender, credits borrower. Repayment reverses. Collateral uses `pending` field.
- **AgentReputation**: trust_score feeds into credit_score calculation
- **SpendingPolicy**: borrowed funds are subject to same spending policy limits
- **TreasuryService**: upcoming loan repayments appear as outflow forecasts
- **ReviewService**: loan performance contributes to reviews

#### Complexity Estimate

- **New models**: 3 tables (credit_scores, loans, lending_offers)
- **New enums**: LoanStatus
- **New service**: `credit_service.py` (~500 lines)
- **New router**: `api/routers/lending.py` (~300 lines)
- **Background tasks**: 2 tasks (credit score recalculation every 5 min, default detection every 1 min)
- **SDK methods**: 6 new methods
- **Effort**: 5-6 days

---

## Feature 3: Smart Contract Workflows (Agent-Native)

### Why Agents Need This

An orchestrator agent wants: "Pay the translation agent 0.1 XMR, but only after the OCR agent has been paid and confirmed delivery." Today this requires the orchestrator to poll escrow status, manually trigger the next payment, handle failures -- all in its own code. Smart workflows push this orchestration into the payment layer where it can be atomic and reliable.

### Priority: MUST-HAVE

This is the core differentiator from "payment API" to "financial orchestration layer." Every multi-agent task today requires hand-rolled payment orchestration. Workflows make that declarative.

### Technical Design

#### 3a. Conditional Payments

A payment that executes only when specified conditions are met.

**Data Model**

```
conditional_payments
  id: UUID (PK)
  payment_hash: String(64) (UNIQUE)
  
  # Payment details (frozen at creation)
  from_agent_id: UUID (FK -> agents.id)
  to_agent_id: UUID (FK -> agents.id)
  amount: Numeric(20,8)
  currency: String(10) (default "XMR")
  memo: Text (nullable)
  
  # Conditions (evaluated by the hub)
  condition_type: String  -- "webhook", "escrow_completed", "time_lock", "balance_threshold"
  condition_config: JSON
  
  # Funds locked from sender
  locked_amount: Numeric(20,8)
  
  # State
  state: Enum (pending, triggered, executed, expired, cancelled)
  
  # Timeouts
  expires_at: DateTime  -- if condition not met by this time, refund
  
  created_at: DateTime
  triggered_at: DateTime (nullable)
  executed_at: DateTime (nullable)
```

**Condition Types**

```python
# 1. Webhook condition: payment triggers when webhook returns { "result": true }
{
  "type": "webhook",
  "url": "https://api.weather.com/check",
  "method": "GET",
  "expected_status": 200,
  "expected_body_path": "$.conditions.rain",  # JSONPath
  "expected_value": true,
  "poll_interval_secs": 60,
  "max_polls": 60
}

# 2. Escrow completion: payment triggers when a specific escrow completes
{
  "type": "escrow_completed",
  "escrow_id": "uuid-of-escrow",
  "required_status": "completed"  # or "delivered"
}

# 3. Time lock: payment releases after a specific time
{
  "type": "time_lock",
  "release_at": "2026-04-02T12:00:00Z"
}

# 4. Balance threshold: payment triggers when recipient's balance drops below threshold
{
  "type": "balance_threshold",
  "agent_id": "uuid-of-agent",
  "currency": "XMR",
  "below": "0.1"  # triggers when balance < 0.1 XMR
}

# 5. Multi-condition (AND/OR)
{
  "type": "multi",
  "operator": "and",  # or "or"
  "conditions": [
    {"type": "escrow_completed", "escrow_id": "..."},
    {"type": "time_lock", "release_at": "..."}
  ]
}
```

#### 3b. Payment DAGs

A directed acyclic graph of payments where edges represent dependencies.

**Data Model**

```
payment_workflows
  id: UUID (PK)
  workflow_hash: String(64) (UNIQUE)
  creator_id: UUID (FK -> agents.id)
  name: String(255) (nullable)
  description: Text (nullable)
  
  # Total locked from creator
  total_locked: Numeric(20,8)
  currency: String(10) (default "XMR")
  
  state: Enum (draft, active, completed, failed, cancelled)
  
  created_at: DateTime
  completed_at: DateTime (nullable)

payment_workflow_nodes
  id: UUID (PK)
  workflow_id: UUID (FK -> payment_workflows.id)
  node_key: String(50)  -- user-assigned key like "step_1", "ocr", "translate"
  
  # Payment (nullable -- some nodes are condition-only gates)
  to_agent_id: UUID (FK -> agents.id, nullable)
  amount: Numeric(20,8) (nullable)
  
  # Condition to trigger this node (nullable -- root nodes trigger immediately)
  condition_type: String (nullable)
  condition_config: JSON (nullable)
  
  # Split payment
  split_config: JSON (nullable)  -- [{"agent_id": "...", "amount": "0.3"}, ...]
  
  state: Enum (waiting, ready, executed, failed, skipped)
  
  executed_at: DateTime (nullable)

payment_workflow_edges
  id: UUID (PK)
  workflow_id: UUID (FK -> payment_workflows.id)
  from_node_id: UUID (FK -> payment_workflow_nodes.id)
  to_node_id: UUID (FK -> payment_workflow_nodes.id)
  
  # Edge condition (optional -- beyond dependency order)
  require_success: Boolean (default true)  -- to_node only runs if from_node succeeded
  
  UNIQUE(from_node_id, to_node_id)
```

#### Flow

```
POST /v2/workflows
{
  "name": "translation-pipeline",
  "nodes": {
    "ocr": {
      "to_agent": "ocr-agent",
      "amount": "0.05"
    },
    "translate": {
      "to_agent": "translate-agent",
      "amount": "0.10",
      "depends_on": ["ocr"]
    },
    "proofread": {
      "to_agent": "proofread-agent",
      "amount": "0.03",
      "depends_on": ["translate"]
    },
    "bonus": {
      "to_agent": "translate-agent",
      "amount": "0.02",
      "depends_on": ["proofread"],
      "condition": {
        "type": "webhook",
        "url": "https://quality-check.example.com/score",
        "expected_body_path": "$.score",
        "expected_value_gte": 90
      }
    }
  }
}
```

**Execution Engine**

Background task (every 10 seconds):

```
For each ACTIVE workflow:
  1. Find all nodes in WAITING state
  2. For each WAITING node:
     a. Check all incoming edges -- are all source nodes EXECUTED?
     b. If yes and node has a condition: evaluate condition
     c. If all dependencies met and condition satisfied:
        - Execute payment (debit from workflow.total_locked, credit to to_agent)
        - Set node state to EXECUTED
     d. If dependency failed and require_success=true:
        - Set node state to SKIPPED
  3. If all nodes are EXECUTED/SKIPPED: set workflow to COMPLETED
  4. Refund unused locked amount (skipped nodes) to creator
```

#### 3c. Split Payments

A simpler primitive: one payment auto-splits to multiple recipients.

```python
s.pay_split(
    recipients=[
        {"agent": "agent-a", "amount": 0.3},
        {"agent": "agent-b", "amount": 0.5},
        {"agent": "agent-c", "amount": 0.2},
    ],
    memo="Revenue share"
)
```

Implemented as a single API endpoint that executes multiple hub-route payments atomically within one DB transaction.

#### API Endpoints

```
# Conditional payments
POST   /v2/payments/conditional          -- create conditional payment (locks funds)
GET    /v2/payments/conditional           -- list my conditional payments
GET    /v2/payments/conditional/{id}     -- details
DELETE /v2/payments/conditional/{id}     -- cancel (refund locked funds)

# Workflows
POST   /v2/workflows                     -- create and activate workflow
GET    /v2/workflows                      -- list my workflows
GET    /v2/workflows/{id}               -- workflow details with node states
DELETE /v2/workflows/{id}               -- cancel workflow (refund locked)

# Split payments
POST   /v2/payments/split                -- atomic split payment
```

#### SDK Methods

```python
# Conditional payment
cp = s.pay_when(
    to_agent="translate-agent",
    amount=0.1,
    condition={"type": "escrow_completed", "escrow_id": escrow_id},
    expires_hours=24,
)

# Time-locked payment
s.pay_at(to_agent="agent-b", amount=0.5, release_at="2026-04-02T12:00:00Z")

# Workflow
wf = s.workflow_create(
    name="translation-pipeline",
    nodes={
        "ocr": {"to_agent": "ocr-agent", "amount": 0.05},
        "translate": {"to_agent": "translate-agent", "amount": 0.1, "depends_on": ["ocr"]},
    }
)

# Split payment
s.pay_split([
    ("agent-a", 0.3),
    ("agent-b", 0.5),
    ("agent-c", 0.2),
])
```

#### Integration with Existing Features

- **EscrowService**: `escrow_completed` condition type listens for escrow state changes via the existing webhook_service
- **BalanceRepository**: locked_amount uses the `pending` balance field
- **WebhookService**: webhook conditions are evaluated using the same HTTP client infrastructure
- **ConversionService**: workflow nodes could include conversion steps

#### Complexity Estimate

- **Conditional payments**: 1 table, 1 service (~300 lines), 1 router (~150 lines). 2-3 days.
- **Payment DAGs**: 3 tables, 1 service (~500 lines), 1 router (~200 lines). 4-5 days.
- **Split payments**: 0 new tables (uses existing hub_routes), 1 endpoint. 0.5 days.
- **Total effort**: 7-8 days

---

## Feature 4: Agent Collectives and DAOs

### Why Agents Need This

Multiple agents working on a large project need shared funds. Today, one agent must hold all the money and manually distribute it. A collective treasury with stake-weighted voting on spending lets a swarm of agents coordinate financially without a single point of trust failure.

### Priority: NICE-TO-HAVE

This is powerful but requires critical mass of agents on the platform. Ship after treasury management, credit, and workflows are proven.

### Technical Design

#### Data Model

```
agent_collectives
  id: UUID (PK)
  collective_hash: String(64) (UNIQUE)
  name: String(255)
  description: Text (nullable)
  creator_id: UUID (FK -> agents.id)
  
  # Treasury
  treasury_balance: Numeric(20,8) (default 0)
  currency: String(10) (default "XMR")
  
  # Governance
  voting_model: String  -- "stake_weighted", "one_agent_one_vote", "reputation_weighted"
  quorum_pct: Integer (default 51)  -- % of voting power needed
  proposal_duration_secs: Integer (default 3600)  -- voting window
  
  # Membership
  min_stake: Numeric(20,8) (default 0)  -- minimum stake to join
  max_members: Integer (default 100)
  is_open: Boolean (default true)  -- open enrollment vs invite-only
  
  state: Enum (active, dissolved)
  created_at: DateTime

collective_members
  id: UUID (PK)
  collective_id: UUID (FK -> agent_collectives.id)
  agent_id: UUID (FK -> agents.id)
  stake: Numeric(20,8) (default 0)
  role: String (default "member")  -- "creator", "admin", "member"
  joined_at: DateTime
  left_at: DateTime (nullable)
  
  UNIQUE(collective_id, agent_id)

collective_proposals
  id: UUID (PK)
  collective_id: UUID (FK -> agent_collectives.id)
  proposer_id: UUID (FK -> agents.id)
  
  # What the proposal does
  proposal_type: String  -- "spend", "add_member", "remove_member", "change_rules", "bounty"
  proposal_config: JSON
  
  # Voting
  state: Enum (voting, passed, rejected, executed, expired)
  votes_for: Numeric(20,8) (default 0)    -- weighted votes
  votes_against: Numeric(20,8) (default 0)
  total_eligible: Numeric(20,8)            -- total voting power at proposal creation
  
  voting_ends_at: DateTime
  executed_at: DateTime (nullable)
  created_at: DateTime

collective_votes
  id: UUID (PK)
  proposal_id: UUID (FK -> collective_proposals.id)
  voter_id: UUID (FK -> agents.id)
  vote: Boolean  -- true = for, false = against
  weight: Numeric(20,8)  -- voting weight at time of vote
  created_at: DateTime
  
  UNIQUE(proposal_id, voter_id)
```

**Proposal Types**

```python
# Spend from treasury
{"type": "spend", "to_agent_id": "...", "amount": "1.0", "memo": "Pay for infrastructure"}

# Post a bounty
{"type": "bounty", "description": "Build a web scraper", "reward": "2.0",
 "required_capabilities": ["web-scraping"], "deadline_secs": 86400}

# Change governance rules
{"type": "change_rules", "quorum_pct": 60, "proposal_duration_secs": 7200}
```

#### Bounty System

Bounties are special proposals that, when passed, create a matchmaking request + escrow:

```
Bounty proposal passes
  -> MatchRequest created with collective as requester
  -> Agent matched
  -> EscrowDeal created (collective treasury as buyer)
  -> Work delivered
  -> Collective members vote on acceptance (or auto-accept if escrow)
  -> Funds released
```

#### API Endpoints

```
POST   /v2/collectives                          -- create collective
GET    /v2/collectives                           -- list my collectives
GET    /v2/collectives/{id}                     -- details
POST   /v2/collectives/{id}/join                -- join (stake required)
POST   /v2/collectives/{id}/leave               -- leave (unstake)
POST   /v2/collectives/{id}/deposit             -- deposit to treasury
POST   /v2/collectives/{id}/proposals           -- create proposal
GET    /v2/collectives/{id}/proposals           -- list proposals
POST   /v2/collectives/{id}/proposals/{pid}/vote -- vote on proposal
```

#### SDK Methods

```python
# Create collective
dao = s.collective_create(
    name="research-swarm",
    voting_model="stake_weighted",
    quorum_pct=51,
    min_stake=0.1,
)

# Join and stake
s.collective_join(dao["collective_id"], stake=1.0)

# Propose spending
proposal = s.collective_propose(
    dao["collective_id"],
    proposal_type="spend",
    config={"to_agent_id": "...", "amount": "0.5", "memo": "Data purchase"},
)

# Vote
s.collective_vote(proposal["proposal_id"], vote=True)

# Post bounty
s.collective_propose(
    dao["collective_id"],
    proposal_type="bounty",
    config={"description": "Scrape 1000 pages", "reward": "2.0", "deadline_secs": 86400},
)
```

#### Complexity Estimate

- **New models**: 4 tables
- **New enums**: ProposalStatus, VotingModel
- **New service**: `collective_service.py` (~600 lines)
- **New router**: `api/routers/collectives.py` (~350 lines)
- **Background tasks**: 1 (proposal execution/expiry checker)
- **Effort**: 5-6 days

---

## Feature 5: Multi-Agent Coordination Payments

### Why Agents Need This

Three agents are collaborating on a task. The buyer wants to pay all three only if all three deliver. Today, this requires three separate escrows with manual coordination. Atomic multi-party payments let the buyer commit to all-or-nothing group payments.

### Priority: MUST-HAVE (atomic multi-party), NICE-TO-HAVE (order book, futures)

The atomic multi-party payment is a natural extension of existing escrow and directly enables multi-agent workflows.

### Technical Design

#### 5a. Atomic Multi-Party Payments

**Data Model**

```
multi_party_payments
  id: UUID (PK)
  payment_hash: String(64) (UNIQUE)
  sender_id: UUID (FK -> agents.id)
  
  total_amount: Numeric(20,8)
  currency: String(10) (default "XMR")
  
  # All-or-nothing: if any recipient rejects, all are refunded
  require_all_accept: Boolean (default true)
  
  state: Enum (pending, accepted, completed, rejected, expired)
  
  accept_deadline: DateTime
  created_at: DateTime
  completed_at: DateTime (nullable)

multi_party_recipients
  id: UUID (PK)
  payment_id: UUID (FK -> multi_party_payments.id)
  recipient_id: UUID (FK -> agents.id)
  amount: Numeric(20,8)
  
  accepted: Boolean (nullable)  -- null = pending, true = accepted, false = rejected
  accepted_at: DateTime (nullable)
  
  UNIQUE(payment_id, recipient_id)
```

**Flow**

```
1. Sender creates multi-party payment (funds locked from sender balance)
2. Each recipient receives webhook notification
3. Each recipient calls POST /v2/payments/multi/{id}/accept (or /reject)
4. If require_all_accept=true:
   - All accept -> funds distributed, state=completed
   - Any reject -> all refunded, state=rejected
5. If require_all_accept=false:
   - Each acceptance triggers individual transfer
   - Rejections refund that portion to sender
6. Expiry -> all pending portions refunded
```

#### 5b. Service Order Book

An order book where agents post what they need and what they offer, with automatic matching.

**Data Model**

```
service_orders
  id: UUID (PK)
  agent_id: UUID (FK -> agents.id)
  order_type: String  -- "bid" (I want to buy) or "ask" (I want to sell)
  
  # Service definition
  capability: String  -- e.g., "translation", "code-review"
  description: Text (nullable)
  
  # Pricing
  price: Numeric(20,8)
  currency: String(10) (default "XMR")
  price_unit: String (nullable)  -- e.g., "per_request", "per_1k_words"
  
  # Constraints
  min_trust_score: Integer (default 0)
  require_escrow: Boolean (default true)
  
  state: Enum (open, matched, filled, cancelled, expired)
  matched_order_id: UUID (nullable)  -- the counterparty order
  escrow_id: UUID (nullable)  -- auto-created escrow
  
  expires_at: DateTime
  created_at: DateTime
```

**Matching engine** runs as a background task:

```
For each OPEN bid order:
  1. Find OPEN ask orders with same capability
  2. Filter: ask.price <= bid.price AND ask.agent trust_score >= bid.min_trust_score
  3. Sort by ask.price ASC (cheapest first)
  4. Match best ask with bid
  5. Auto-create escrow between bid.agent (buyer) and ask.agent (seller)
  6. Update both orders to MATCHED state
```

#### API Endpoints

```
# Multi-party payments
POST   /v2/payments/multi                    -- create multi-party payment
GET    /v2/payments/multi/{id}              -- status
POST   /v2/payments/multi/{id}/accept       -- recipient accepts
POST   /v2/payments/multi/{id}/reject       -- recipient rejects

# Order book
POST   /v2/orders                            -- place order (bid or ask)
GET    /v2/orders                             -- list my orders
GET    /v2/orders/book?capability=translation -- view order book for a capability
DELETE /v2/orders/{id}                       -- cancel order
```

#### SDK Methods

```python
# Multi-party payment
mp = s.pay_multi(
    recipients=[
        ("agent-a", 0.3),
        ("agent-b", 0.5),
        ("agent-c", 0.2),
    ],
    require_all_accept=True,
    accept_hours=2,
)

# Place ask order (I want to sell translation services)
s.order_ask(
    capability="translation",
    price=0.01,
    price_unit="per_1k_words",
    expires_hours=24,
)

# Place bid order (I want to buy translation services)
s.order_bid(
    capability="translation",
    price=0.015,
    description="Translate 5000 words EN->FR",
    expires_hours=4,
)

# View order book
book = s.order_book(capability="translation")
# -> {"bids": [...], "asks": [...], "spread": "0.005"}
```

#### Complexity Estimate

- **Multi-party payments**: 2 tables, 1 service (~300 lines), 1 router (~150 lines). 2-3 days.
- **Order book**: 1 table, 1 service (~400 lines), 1 router (~200 lines), 1 background task. 3-4 days.
- **Total effort**: 5-7 days

---

## Feature 6: Agent Financial Instruments

### Why Agents Need This

An agent needs guaranteed access to a compute-heavy service next Tuesday. Today it can only hope the service is available and affordable. Options on compute resources let it lock in a price now. Prediction markets let agents bet on task outcomes, creating information markets that improve coordination.

### Priority: NICE-TO-HAVE

These are sophisticated instruments that only matter once the agent economy has critical mass. Ship after the core financial primitives are proven.

### Technical Design

#### 6a. Revenue Sharing Agreements

An agent can create a revenue-sharing agreement where a percentage of its future earnings is automatically forwarded to an investor agent.

**Data Model**

```
revenue_shares
  id: UUID (PK)
  
  # Participants
  earner_id: UUID (FK -> agents.id)    -- the agent earning revenue
  investor_id: UUID (FK -> agents.id)  -- the agent receiving share
  
  # Terms
  share_pct: Integer  -- percentage of each incoming payment forwarded
  cap_amount: Numeric(20,8) (nullable)  -- total cap (e.g., 2x of investment)
  duration_secs: Integer (nullable)     -- agreement duration
  
  # Tracking
  total_shared: Numeric(20,8) (default 0)
  
  state: Enum (active, completed, cancelled)
  
  created_at: DateTime
  expires_at: DateTime (nullable)
  completed_at: DateTime (nullable)
```

**Implementation**: Hooks into the existing hub-route payment flow. When `to_agent_id` has an active revenue_share, the hub automatically splits the payment:

```python
# In wallet_service.py or hub-route handler:
revenue_shares = get_active_shares(to_agent_id, db)
for share in revenue_shares:
    split_amount = amount * share.share_pct / 100
    # Credit investor, debit from the payment before crediting earner
```

#### 6b. Tokenized API Access (Pay-Per-Call Metering)

Instead of flat subscriptions, agents can sell API access with per-call billing, metered by the hub.

**Data Model**

```
api_access_tokens
  id: UUID (PK)
  
  # Participants
  provider_id: UUID (FK -> agents.id)
  consumer_id: UUID (FK -> agents.id)
  
  # Terms
  price_per_call: Numeric(20,8)
  currency: String(10) (default "XMR")
  
  # Pre-paid balance
  prepaid_amount: Numeric(20,8) (default 0)
  consumed_amount: Numeric(20,8) (default 0)
  call_count: Integer (default 0)
  
  # Rate limits
  max_calls_per_minute: Integer (nullable)
  max_calls_total: Integer (nullable)
  
  # Token for the consumer to present to the provider
  access_token: String(64) (UNIQUE)
  
  state: Enum (active, depleted, expired, revoked)
  expires_at: DateTime (nullable)
  created_at: DateTime
```

**Flow**:

```
1. Provider publishes pricing: {"endpoint": "/translate", "price_per_call": "0.001"}
2. Consumer prepays: POST /v2/api-access/purchase (locks 1.0 XMR)
3. Consumer receives access_token
4. Consumer calls provider's API with access_token in header
5. Provider calls POST /v2/api-access/{token}/meter to record usage
6. Hub deducts price_per_call from consumer's prepaid balance, credits provider
7. When prepaid_amount exhausted: token becomes DEPLETED, consumer must top up
```

#### 6c. Prediction Markets

Agents can create prediction markets on verifiable outcomes.

**Data Model**

```
prediction_markets
  id: UUID (PK)
  creator_id: UUID (FK -> agents.id)
  
  question: Text  -- "Will agent X complete escrow Y before deadline?"
  resolution_source: JSON  -- how to verify outcome
  
  # Pool
  total_pool: Numeric(20,8) (default 0)
  currency: String(10) (default "XMR")
  
  # Positions
  yes_pool: Numeric(20,8) (default 0)
  no_pool: Numeric(20,8) (default 0)
  
  state: Enum (open, closed, resolved_yes, resolved_no, cancelled)
  
  resolves_at: DateTime  -- when market closes for betting
  resolution_deadline: DateTime  -- when outcome must be determined
  created_at: DateTime

prediction_positions
  id: UUID (PK)
  market_id: UUID (FK -> prediction_markets.id)
  agent_id: UUID (FK -> agents.id)
  position: String  -- "yes" or "no"
  amount: Numeric(20,8)
  created_at: DateTime
```

**Resolution Sources**:

```python
# Escrow-based (verifiable on-platform)
{"type": "escrow_status", "escrow_id": "...", "expected_status": "completed"}

# Webhook-based (external oracle)
{"type": "webhook", "url": "https://...", "expected_body_path": "$.result", "expected_value": true}

# Creator-resolved (trusted, with stake)
{"type": "creator_resolved", "creator_stake": "0.5"}  # creator stakes 0.5 XMR as honesty bond
```

**Payouts** (parimutuel model):

```
If resolved YES:
  Each YES bettor receives: (their_bet / yes_pool) * total_pool
  Platform takes 2% of total_pool as fee

If resolved NO:
  Each NO bettor receives: (their_bet / no_pool) * total_pool
  Platform takes 2% of total_pool as fee
```

#### Complexity Estimate

- **Revenue sharing**: 1 table, hook into payment flow (~200 lines). 1-2 days.
- **API access metering**: 1 table, 1 service (~300 lines), 1 router (~200 lines). 2-3 days.
- **Prediction markets**: 2 tables, 1 service (~400 lines), 1 router (~250 lines). 3-4 days.
- **Total effort**: 6-9 days

---

## Implementation Priority Matrix

| Feature | Priority | Effort | Agent Value | Existing Infra Reuse | Ship Order |
|---|---|---|---|---|---|
| Treasury Management (1) | MUST-HAVE | 3-4d | Critical | High (ConversionService, BalanceRepo) | 1st |
| Split Payments (3c) | MUST-HAVE | 0.5d | High | Very High (existing hub-route) | 2nd |
| Conditional Payments (3a) | MUST-HAVE | 2-3d | Critical | High (BalanceRepo, WebhookService) | 3rd |
| Atomic Multi-Party (5a) | MUST-HAVE | 2-3d | High | High (BalanceRepo, WebhookService) | 4th |
| Credit Scoring (2a) | MUST-HAVE | 2d | Critical | High (AgentReputation) | 5th |
| Lending (2b) | MUST-HAVE | 3-4d | Critical | Medium (new domain) | 6th |
| Payment DAGs (3b) | MUST-HAVE | 4-5d | Critical | Medium (new execution engine) | 7th |
| Revenue Sharing (6a) | NICE-TO-HAVE | 1-2d | Medium | Very High (hook into hub-route) | 8th |
| API Access Metering (6b) | NICE-TO-HAVE | 2-3d | High | Medium | 9th |
| Order Book (5b) | NICE-TO-HAVE | 3-4d | Medium | Medium (MatchmakingService) | 10th |
| Agent Collectives (4) | NICE-TO-HAVE | 5-6d | Medium | Medium | 11th |
| Flash Loans (2c) | NICE-TO-HAVE | 2d | Medium | Medium | 12th |
| Prediction Markets (6c) | NICE-TO-HAVE | 3-4d | Low-Medium | Low | 13th |

**Total estimated effort**: ~35-45 days for all features.

**Recommended Phase 4a (ship first, ~15 days)**:
1. Treasury Management
2. Split Payments
3. Conditional Payments
4. Atomic Multi-Party Payments
5. Credit Scoring
6. Lending

**Recommended Phase 4b (ship second, ~12 days)**:
7. Payment DAGs
8. Revenue Sharing
9. API Access Metering
10. Order Book

**Recommended Phase 4c (ship when critical mass, ~10 days)**:
11. Agent Collectives
12. Flash Loans
13. Prediction Markets

---

## SDK Surface Area Impact

Phase 4 adds approximately 30 new SDK methods to the `Sthrip` client class. To prevent the class from becoming unwieldy (it is already ~1350 lines), the SDK should be refactored into a namespace pattern:

```python
s = Sthrip()

# Current (unchanged)
s.pay("agent", 0.05)
s.balance()

# Phase 4 namespaces
s.treasury.set_policy(...)
s.treasury.forecast(...)
s.treasury.rebalance()

s.credit.score()
s.credit.borrow(0.5, duration_secs=1800)
s.credit.repay(loan_id)
s.credit.lend_offer(...)

s.workflow.create(name="pipeline", nodes={...})
s.workflow.status(workflow_id)

s.collective.create(name="research-swarm", ...)
s.collective.propose(...)
s.collective.vote(...)
```

Each namespace is a lightweight proxy object that holds a reference to the parent `Sthrip` instance and delegates HTTP calls. This keeps backward compatibility while organizing the growing API surface.

---

## Database Migration Strategy

Phase 4 adds approximately 15 new tables. Following the existing pattern:

1. All migrations use `IF NOT EXISTS` / `IF EXISTS` for idempotency
2. New enums are added as VARCHAR (not PostgreSQL ENUM) to simplify migration
3. Foreign keys reference `agents.id` (UUID)
4. All monetary amounts use `Numeric(20, 8)` for consistency with existing models
5. All tables include `created_at` with `default=func.now()`
6. Row-level locking (`WITH FOR UPDATE`) for all balance-mutating operations

---

## Background Task Summary

Phase 4 adds the following background tasks to the lifespan manager in `api/main_v2.py`:

| Task | Interval | Description |
|---|---|---|
| treasury_rebalance_loop | 60s | Evaluate and execute treasury rebalancing |
| treasury_forecast_loop | 300s | Regenerate cash flow forecasts |
| credit_score_recalculation | 300s | Recalculate credit scores for active agents |
| loan_default_checker | 60s | Detect and process loan defaults |
| conditional_payment_evaluator | 10s | Check and trigger conditional payments |
| workflow_executor | 10s | Advance payment workflow DAGs |
| order_book_matcher | 30s | Match bid/ask orders |
| proposal_executor | 60s | Execute passed collective proposals, expire stale ones |

All tasks follow the existing pattern: async loop with `try/except` around the body, configurable interval via environment variables, graceful shutdown on lifespan exit.

---

## Privacy Considerations

All new features operate within the hub's ledger model. No additional on-chain data is created. Specific privacy notes:

- **Credit scores**: Public by default (agents need to see borrower creditworthiness). Agents can opt out of lending entirely.
- **Treasury policies**: Private. Only the agent can see its own treasury configuration.
- **Workflows**: Payment DAG structure is visible only to the creator. Individual payments within the DAG are visible to sender/recipient as normal.
- **Collectives**: Membership is visible to other members. Treasury balance is visible to members. Voting is visible to members.
- **Prediction markets**: Positions are pseudonymous (visible by agent_id, not agent_name, unless resolved by the viewer).

---

## Webhook Events

New event types for Phase 4:

```
treasury.rebalanced          -- treasury rebalance executed
treasury.forecast.alert       -- upcoming obligation with insufficient projected balance
credit.score.updated          -- credit score recalculated
loan.requested                -- someone wants to borrow from you
loan.funded                   -- your loan request was funded
loan.repaid                   -- borrower repaid your loan
loan.defaulted                -- borrower defaulted
payment.conditional.triggered -- condition met, payment executing
payment.conditional.expired   -- condition not met before expiry
workflow.node.executed        -- workflow node completed
workflow.completed            -- entire workflow finished
collective.proposal.created   -- new proposal in your collective
collective.proposal.passed    -- proposal you voted on passed
order.matched                 -- your order was matched
```
