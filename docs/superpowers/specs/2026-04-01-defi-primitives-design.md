# Sthrip DeFi Primitives: Design Specification

**Date**: 2026-04-01
**Status**: Draft
**Scope**: Agent-native financial primitives for autonomous AI economies
**Priorities**: Composability, agent autonomy, risk containment, privacy

---

## Design Philosophy

Human DeFi (Uniswap, Aave, Compound) was built for humans with wallets clicking buttons. Agent DeFi is fundamentally different:

1. **API-first, not UI-first** -- agents call endpoints, not click buttons
2. **Reputation replaces identity** -- no KYC, trust scores drive risk parameters
3. **Speed over governance** -- agents make decisions in milliseconds, not governance votes over weeks
4. **Composability is mandatory** -- agents chain primitives: borrow xUSD -> buy compute future -> hedge with option, all in one API call
5. **Hub-held, not on-chain** -- all positions are hub-balance entries with row-level locking, not smart contracts. The hub IS the protocol.

---

## Priority Matrix

| Primitive | Agent Impact (1-10) | Complexity | Revenue Potential | Phase |
|---|---|---|---|---|
| Liquidity Pools (AMM) | 9 | High | High (LP fees + hub fee) | 1 |
| Flash Loans | 10 | Medium | Medium (flat fee per flash) | 1 |
| Overcollateralized Lending | 8 | High | High (interest spread) | 1 |
| Payment Insurance | 7 | Medium | Medium (premiums) | 2 |
| Compute Futures | 9 | Medium | High (settlement fees) | 2 |
| SLA Bonds | 8 | Low | Medium (bond fees) | 2 |
| Revenue Sharing Tokens | 7 | High | High (trade fees) | 3 |
| Perpetual Swaps | 6 | Very High | Very High (funding rates) | 3 |
| Agent Index Funds | 5 | High | Medium (management fee) | 3 |
| Prediction Markets | 6 | Medium | Medium (resolution fees) | 3 |

---

## 1. Agent Liquidity Pools (AMM)

### Why agents need this

Current conversion service (`ConversionService.convert()`) uses a fixed spread (0.5% fee) with fallback rates from CoinGecko. This means:
- Every XMR/xUSD conversion hits an external API
- No price discovery within the ecosystem
- Agents providing services in xUSD must convert at whatever rate the hub dictates
- No way for agents to earn yield on idle capital

An AMM lets agents themselves provide liquidity, earn fees, and get better rates through competition.

### Constant Product AMM (x * y = k)

For agent economies, the classic Uniswap v2 model works well because:
- Simple to implement and reason about
- Predictable pricing with no oracles needed for internal pairs
- Agents can programmatically arbitrage against external rates

For XMR/xUSD specifically, a hybrid model is better: **Curve-style StableSwap for xUSD/xEUR** (both pegged), **constant product for XMR/xUSD** (volatile pair).

### Mathematical Model

**Constant Product (XMR/xUSD):**
```
x * y = k
where x = XMR reserves, y = xUSD reserves

Price of XMR in xUSD = y / x
Price of xUSD in XMR = x / y

For a swap of dx XMR -> xUSD:
  dy = y - k / (x + dx)
  dy = y * dx / (x + dx)             -- simplified
  effective_price = dy / dx
  price_impact = 1 - (effective_price / (y / x))

Fee: 0.3% of input amount (goes to LPs)
Hub fee: 0.05% of input amount (goes to hub revenue)
```

**StableSwap (xUSD/xEUR, amplification factor A):**
```
A * n^n * sum(x_i) + D = A * D * n^n + D^(n+1) / (n^n * prod(x_i))
where n = number of tokens, D = total deposits at equilibrium

For n=2 (xUSD/xEUR):
  2A(x + y) + D = 2AD + D^3 / (4xy)

This gives near-zero slippage when x ~ y (both near peg)
and reverts to constant product behavior when heavily imbalanced.
```

### Data Model

#### Enums (add to `sthrip/db/enums.py`)

```python
class PoolStatus(str, _PyEnum):
    ACTIVE = "active"
    PAUSED = "paused"         # admin pause
    DRAINING = "draining"     # no new deposits, withdrawals only
    CLOSED = "closed"

class PoolType(str, _PyEnum):
    CONSTANT_PRODUCT = "constant_product"   # x*y=k (volatile pairs)
    STABLE_SWAP = "stable_swap"             # Curve-style (pegged pairs)

class LPActionType(str, _PyEnum):
    ADD_LIQUIDITY = "add_liquidity"
    REMOVE_LIQUIDITY = "remove_liquidity"
    SWAP = "swap"
```

#### Models (new file: `sthrip/db/models_defi.py`)

```python
class LiquidityPool(Base):
    """AMM liquidity pool for a token pair."""
    __tablename__ = "liquidity_pools"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Pool identity
    token_a = Column(String(10), nullable=False)         # e.g. "XMR"
    token_b = Column(String(10), nullable=False)         # e.g. "xUSD"
    pool_type = Column(SQLEnum(PoolType), nullable=False)
    
    # Reserves (the core AMM state)
    reserve_a = Column(Numeric(30, 12), nullable=False, default=Decimal('0'))
    reserve_b = Column(Numeric(30, 12), nullable=False, default=Decimal('0'))
    
    # For constant product: k = reserve_a * reserve_b
    # Stored for fast validation, recalculated on every state change
    k_last = Column(Numeric(60, 24), nullable=False, default=Decimal('0'))
    
    # For stable_swap: amplification coefficient
    amplification = Column(Integer, default=100)  # A parameter (Curve uses 100-2000)
    
    # LP token tracking
    total_lp_shares = Column(Numeric(30, 12), nullable=False, default=Decimal('0'))
    
    # Fee configuration
    swap_fee_bps = Column(Integer, default=30)    # 30 bps = 0.3% (to LPs)
    hub_fee_bps = Column(Integer, default=5)      # 5 bps = 0.05% (to hub)
    
    # Pool metadata
    status = Column(SQLEnum(PoolStatus), default=PoolStatus.ACTIVE)
    total_volume_a = Column(Numeric(30, 12), default=Decimal('0'))  # cumulative
    total_volume_b = Column(Numeric(30, 12), default=Decimal('0'))
    total_fees_a = Column(Numeric(30, 12), default=Decimal('0'))    # fees earned by LPs
    total_fees_b = Column(Numeric(30, 12), default=Decimal('0'))
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    
    __table_args__ = (
        UniqueConstraint("token_a", "token_b", name="uq_pool_pair"),
        CheckConstraint("reserve_a >= 0", name="ck_pool_reserve_a_non_negative"),
        CheckConstraint("reserve_b >= 0", name="ck_pool_reserve_b_non_negative"),
        CheckConstraint("swap_fee_bps >= 0 AND swap_fee_bps <= 1000",
                        name="ck_pool_swap_fee_range"),
    )


class LPPosition(Base):
    """An agent's liquidity position in a pool."""
    __tablename__ = "lp_positions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pool_id = Column(UUID(as_uuid=True), ForeignKey("liquidity_pools.id"), nullable=False, index=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    # LP shares owned by this agent
    shares = Column(Numeric(30, 12), nullable=False, default=Decimal('0'))
    
    # Tracking for impermanent loss calculation
    deposit_a = Column(Numeric(30, 12), default=Decimal('0'))  # cumulative deposited
    deposit_b = Column(Numeric(30, 12), default=Decimal('0'))
    withdrawn_a = Column(Numeric(30, 12), default=Decimal('0'))
    withdrawn_b = Column(Numeric(30, 12), default=Decimal('0'))
    
    # Value at entry (for IL tracking)
    entry_price_ratio = Column(Numeric(20, 12), nullable=True)  # price of A in terms of B at deposit
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    
    __table_args__ = (
        UniqueConstraint("pool_id", "agent_id", name="uq_lp_position"),
        CheckConstraint("shares >= 0", name="ck_lp_shares_non_negative"),
    )


class PoolAction(Base):
    """Audit trail for all pool operations (swaps, adds, removes)."""
    __tablename__ = "pool_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pool_id = Column(UUID(as_uuid=True), ForeignKey("liquidity_pools.id"), nullable=False, index=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    action_type = Column(SQLEnum(LPActionType), nullable=False)
    
    # Amounts involved
    amount_a = Column(Numeric(30, 12), default=Decimal('0'))
    amount_b = Column(Numeric(30, 12), default=Decimal('0'))
    
    # For swaps: direction and pricing
    swap_in_token = Column(String(10), nullable=True)
    swap_in_amount = Column(Numeric(30, 12), nullable=True)
    swap_out_token = Column(String(10), nullable=True)
    swap_out_amount = Column(Numeric(30, 12), nullable=True)
    swap_fee = Column(Numeric(30, 12), nullable=True)
    hub_fee = Column(Numeric(30, 12), nullable=True)
    effective_price = Column(Numeric(20, 12), nullable=True)
    price_impact_bps = Column(Integer, nullable=True)
    
    # LP shares minted/burned (for add/remove liquidity)
    shares_delta = Column(Numeric(30, 12), nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    
    __table_args__ = (
        Index("ix_pool_actions_pool_created", "pool_id", "created_at"),
        Index("ix_pool_actions_agent_created", "agent_id", "created_at"),
    )
```

### API Design

```
POST   /v2/pools                           -- create pool (admin only initially)
GET    /v2/pools                           -- list all pools with reserves + APY
GET    /v2/pools/{pool_id}                 -- pool details + recent trades

POST   /v2/pools/{pool_id}/swap            -- execute a swap
  Body: { "token_in": "XMR", "amount_in": "1.5", "min_amount_out": "265.0", "slippage_tolerance_bps": 50 }
  Response: { "amount_out": "267.5", "fee": "0.45", "price_impact_bps": 12, "effective_price": "178.33" }

POST   /v2/pools/{pool_id}/add-liquidity   -- deposit tokens proportionally
  Body: { "amount_a": "1.0", "amount_b": "180.0", "min_shares": "0" }
  Response: { "shares_minted": "13.416", "actual_a": "1.0", "actual_b": "178.5" }

POST   /v2/pools/{pool_id}/remove-liquidity -- withdraw by burning LP shares
  Body: { "shares": "13.416", "min_a": "0.9", "min_b": "160.0" }
  Response: { "amount_a": "1.02", "amount_b": "182.1", "shares_burned": "13.416" }

GET    /v2/pools/{pool_id}/quote           -- get swap quote without executing
  Query: ?token_in=XMR&amount_in=1.5
  Response: { "amount_out": "267.5", "fee": "0.45", "price_impact_bps": 12, "expires_in": 10 }

GET    /v2/pools/positions                 -- list agent's LP positions across all pools
GET    /v2/pools/{pool_id}/positions/{agent_id} -- agent's position in specific pool
```

### Service Layer (`sthrip/services/pool_service.py`)

Key methods:
```python
class PoolService:
    def create_pool(db, token_a, token_b, pool_type, initial_a, initial_b, creator_id) -> dict
    def swap(db, pool_id, agent_id, token_in, amount_in, min_amount_out) -> dict
    def add_liquidity(db, pool_id, agent_id, amount_a, amount_b, min_shares) -> dict
    def remove_liquidity(db, pool_id, agent_id, shares, min_a, min_b) -> dict
    def get_quote(db, pool_id, token_in, amount_in) -> dict
    def get_pool_stats(db, pool_id) -> dict  # reserves, volume, APY, IL
```

### Risk Analysis

| Risk | Severity | Mitigation |
|---|---|---|
| Price manipulation (sandwich attacks) | HIGH | Max price impact limit (500 bps), slippage protection required on all swaps |
| LP drain via flash loan | HIGH | Flash loans cannot interact with pools in the same call (reentrancy guard) |
| Impermanent loss | MEDIUM | IL tracking per position, optional IL insurance pool (Phase 2) |
| Low liquidity / high slippage | MEDIUM | Minimum initial liquidity requirement (100 XMR equivalent) |
| Oracle attack on StableSwap | LOW | No external oracle needed; internal rates only |
| Pool token inflation | LOW | Shares calculated via constant product formula, mathematically bounded |

### Integration with Existing Features

- **ConversionService**: Route XMR/xUSD and XMR/xEUR conversions through AMM instead of fixed rates. Fall back to ConversionService if pool has insufficient liquidity.
- **BalanceRepository**: Deduct/credit via existing `balance_repo.deduct()` / `balance_repo.credit()` -- all pool operations go through balance repo.
- **FeeCollector**: Hub fee (0.05%) recorded via `FeeCollection` with `source_type = "amm_swap"`.
- **Webhooks**: `pool.swap`, `pool.add_liquidity`, `pool.remove_liquidity` events.

---

## 2. Agent Lending Protocol

### Why agents need this

AI agents face a fundamental working capital problem:
- Agent A has 100 XMR idle but needs to run for 3 months
- Agent B needs 10 XMR now to pay for GPU compute but will earn 15 XMR over the next week
- Agent C needs 1000 xUSD for a single large transaction but has 10 XMR collateral

Without lending, idle capital sits earning nothing, and agents that need capital cannot get it. This limits the velocity of the agent economy.

### Three Lending Modes

1. **Overcollateralized Lending** -- Lock XMR, borrow xUSD (like Aave/Compound). Safe, no reputation needed.
2. **Reputation-Based Lending** -- Trust score > 80 can borrow with lower collateral. Agent-native.
3. **Flash Loans** -- Borrow and repay in one API call. Zero risk to lender.

### Interest Rate Model

Utilization-based (like Aave), with a kink:

```
utilization_rate = total_borrows / total_deposits

if utilization_rate <= optimal_utilization (80%):
    borrow_rate = base_rate + (utilization_rate / optimal_utilization) * slope1

if utilization_rate > optimal_utilization:
    borrow_rate = base_rate + slope1 + 
                  ((utilization_rate - optimal_utilization) / (1 - optimal_utilization)) * slope2

Parameters (XMR lending market):
  base_rate = 2% APR
  slope1 = 8% APR        -- gradual increase up to 80% utilization
  slope2 = 100% APR      -- steep increase above 80% (discourages full utilization)
  optimal_utilization = 80%

At 50% utilization: borrow_rate = 2% + (0.5/0.8) * 8% = 7% APR
At 80% utilization: borrow_rate = 2% + 8% = 10% APR
At 90% utilization: borrow_rate = 2% + 8% + (0.1/0.2) * 100% = 60% APR  (!)
```

Supply rate (what lenders earn):
```
supply_rate = borrow_rate * utilization_rate * (1 - reserve_factor)
reserve_factor = 10%  (hub keeps 10% of interest as revenue)
```

### Collateral & Liquidation

```
Loan Health Factor:
  health_factor = (collateral_value * liquidation_threshold) / borrow_value

  XMR collateral: liquidation_threshold = 82.5% (LTV max = 75%)
  Reputation bonus: agents with trust_score >= 80 get +5% LTV (max 80%)

If health_factor < 1.0:
  Position is liquidatable
  Liquidator repays X% of debt, receives collateral at Y% discount
  Liquidation bonus: 5% (liquidator gets 5% more collateral than debt repaid)
  Close factor: 50% (max 50% of debt can be liquidated at once)
```

### Flash Loans

The most agent-native DeFi primitive. Borrow any amount, use it, repay in the same API call.

```
POST /v2/lending/flash-loan
Body: {
    "borrow_token": "xUSD",
    "borrow_amount": "10000",
    "operations": [
        {"action": "swap", "pool_id": "...", "token_in": "xUSD", "amount_in": "10000"},
        {"action": "pay", "to_agent": "compute-provider", "amount": "55", "token": "XMR"},
        {"action": "swap", "pool_id": "...", "token_in": "XMR", "amount_in": "0.5"}
    ]
}

Execution:
1. Hub credits 10000 xUSD to agent's balance (temporary)
2. Execute operations sequentially
3. Hub verifies agent has >= 10000 + fee xUSD at the end
4. If yes: deduct repayment, keep fee. Transaction commits.
5. If no: ENTIRE transaction rolls back. No risk.

Fee: 0.09% flat (9 bps) -- lower than Aave's 0.09% because no gas costs
```

### Data Model

```python
class LendingMarket(Base):
    """A lending market for a single token (e.g., XMR lending market)."""
    __tablename__ = "lending_markets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token = Column(String(10), unique=True, nullable=False)

    # Pool state
    total_deposits = Column(Numeric(30, 12), default=Decimal('0'))
    total_borrows = Column(Numeric(30, 12), default=Decimal('0'))
    total_reserves = Column(Numeric(30, 12), default=Decimal('0'))  # hub revenue from interest

    # Interest rate parameters
    base_rate_bps = Column(Integer, default=200)       # 2% APR
    slope1_bps = Column(Integer, default=800)          # 8% APR
    slope2_bps = Column(Integer, default=10000)        # 100% APR
    optimal_utilization_bps = Column(Integer, default=8000)  # 80%
    reserve_factor_bps = Column(Integer, default=1000)       # 10%

    # Cumulative index (compound interest tracking, like Aave's ray math)
    borrow_index = Column(Numeric(30, 18), default=Decimal('1.0'))
    supply_index = Column(Numeric(30, 18), default=Decimal('1.0'))
    last_update_at = Column(DateTime(timezone=True), default=func.now())

    # Collateral parameters
    ltv_bps = Column(Integer, default=7500)                   # 75% max LTV
    liquidation_threshold_bps = Column(Integer, default=8250)  # 82.5%
    liquidation_bonus_bps = Column(Integer, default=500)       # 5% bonus

    status = Column(SQLEnum(PoolStatus), default=PoolStatus.ACTIVE)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class LendingPosition(Base):
    """An agent's deposit or borrow position in a lending market."""
    __tablename__ = "lending_positions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    market_id = Column(UUID(as_uuid=True), ForeignKey("lending_markets.id"), nullable=False, index=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)

    # Deposits (earning interest)
    deposit_shares = Column(Numeric(30, 18), default=Decimal('0'))  # shares * supply_index = actual balance

    # Borrows (paying interest)
    borrow_shares = Column(Numeric(30, 18), default=Decimal('0'))   # shares * borrow_index = actual debt

    # Collateral locked from other markets
    # Tracked separately: see CollateralLock

    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("market_id", "agent_id", name="uq_lending_position"),
    )


class CollateralLock(Base):
    """Collateral locked against a borrow position."""
    __tablename__ = "collateral_locks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    # What's locked
    locked_token = Column(String(10), nullable=False)    # e.g. "XMR"
    locked_amount = Column(Numeric(30, 12), nullable=False)
    
    # What's borrowed against it
    borrow_market_id = Column(UUID(as_uuid=True), ForeignKey("lending_markets.id"), nullable=False)
    
    # Reputation-based LTV bonus
    reputation_ltv_bonus_bps = Column(Integer, default=0)  # 0-500 bps based on trust score
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("locked_amount > 0", name="ck_collateral_amount_positive"),
        CheckConstraint("reputation_ltv_bonus_bps >= 0 AND reputation_ltv_bonus_bps <= 500",
                        name="ck_reputation_bonus_range"),
    )


class FlashLoan(Base):
    """Record of a flash loan execution."""
    __tablename__ = "flash_loans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    borrow_token = Column(String(10), nullable=False)
    borrow_amount = Column(Numeric(30, 12), nullable=False)
    fee_amount = Column(Numeric(30, 12), nullable=False)
    
    operations = Column(JSON, nullable=False)         # serialized operation list
    operation_count = Column(Integer, nullable=False)
    
    success = Column(Boolean, nullable=False)         # did all ops + repayment succeed?
    error_message = Column(Text, nullable=True)       # if failed, why
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    
    __table_args__ = (
        CheckConstraint("borrow_amount > 0", name="ck_flash_amount_positive"),
    )


class LiquidationEvent(Base):
    """Record of a liquidation execution."""
    __tablename__ = "liquidation_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    liquidator_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    borrower_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    debt_token = Column(String(10), nullable=False)
    debt_repaid = Column(Numeric(30, 12), nullable=False)
    
    collateral_token = Column(String(10), nullable=False)
    collateral_seized = Column(Numeric(30, 12), nullable=False)
    
    liquidation_bonus = Column(Numeric(30, 12), nullable=False)
    health_factor_before = Column(Numeric(10, 6), nullable=False)
    health_factor_after = Column(Numeric(10, 6), nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=func.now())
```

### API Design

```
# Markets
GET    /v2/lending/markets                    -- list all markets with rates + utilization
GET    /v2/lending/markets/{token}            -- specific market details

# Deposits (earn interest)
POST   /v2/lending/deposit                    -- deposit tokens to earn interest
  Body: { "token": "XMR", "amount": "10.0" }
POST   /v2/lending/withdraw                   -- withdraw deposited tokens + interest
  Body: { "token": "XMR", "amount": "5.0" }

# Borrowing
POST   /v2/lending/borrow                     -- borrow against collateral
  Body: { "borrow_token": "xUSD", "borrow_amount": "1000", "collateral_token": "XMR", "collateral_amount": "8.0" }
POST   /v2/lending/repay                      -- repay borrowed amount
  Body: { "token": "xUSD", "amount": "500" }

# Collateral
POST   /v2/lending/collateral/add             -- add more collateral to existing position
POST   /v2/lending/collateral/remove          -- remove excess collateral

# Flash loans
POST   /v2/lending/flash-loan                 -- execute flash loan + operations atomically

# Positions
GET    /v2/lending/positions                  -- all my positions (deposits + borrows)
GET    /v2/lending/positions/health            -- health factor across all positions

# Liquidations
POST   /v2/lending/liquidate                  -- liquidate an undercollateralized position
  Body: { "borrower_id": "...", "debt_token": "xUSD", "debt_amount": "500", "collateral_token": "XMR" }
GET    /v2/lending/liquidatable               -- list positions eligible for liquidation
```

### Risk Analysis

| Risk | Severity | Mitigation |
|---|---|---|
| Bank run (all depositors withdraw) | HIGH | Interest rate kink at 80% makes borrowing extremely expensive above threshold |
| Oracle manipulation | HIGH | Use internal AMM pool price, TWAP over 15 min, with bounds check against external rate |
| Flash loan attack on lending | CRITICAL | Flash loans cannot deposit as collateral; operations whitelist enforced |
| Cascading liquidations | HIGH | Close factor = 50% prevents full liquidation; health factor buffer |
| Bad debt (underwater position) | MEDIUM | Reserve factor accumulates protocol-owned buffer; insurance pool (Phase 2) |
| Interest rate model exploit | LOW | Rate parameters are admin-configurable, not governance-voteable |

### Integration with Existing Features

- **BalanceRepository**: Deposits deducted from agent balance, credited to lending market; withdrawals reverse.
- **AgentReputation**: Trust score > 80 unlocks +5% LTV bonus for undercollateralized lending.
- **RateService**: Collateral valuation uses `RateService.get_rate()` for cross-token positions (XMR collateral, xUSD borrow).
- **Escrow**: Escrowed funds cannot be used as collateral (prevents double-pledging).
- **Spending Policies**: Borrow operations respect `max_per_tx` and `daily_limit`.

---

## 3. Payment Insurance

### Why agents need this

Agents operating autonomously face counterparty risk:
- Escrow seller never delivers -> buyer gets refund but wasted time
- Payment channel counterparty goes offline -> funds locked until settlement period
- SLA provider delivers late -> penalty covers some loss but not all
- Cross-chain swap counterparty fails HTLC claim -> funds locked for 30 min

Insurance pools let agents collectively hedge these risks.

### Insurance Model

**Parametric insurance** (auto-payout on measurable events, no claims adjudication):

```
Premium calculation:

  base_premium_rate = 1% of insured amount per month

  Adjustments:
    - Counterparty trust_score > 80: -30% premium
    - Counterparty trust_score < 30: +50% premium
    - Insured amount > 100 XMR: -10% volume discount
    - Agent's own claims history: +20% per past claim (up to +100%)

  Minimum premium: 0.001 XMR

Payout triggers (automatic, no human review):
  - escrow.expired: buyer gets insured_amount (up to escrow amount)
  - sla.breached: consumer gets insured_amount (up to SLA price)
  - channel.disputed: insured party gets guaranteed payout
  - swap.expired: initiator gets refund of insured amount
```

### Data Model

```python
class InsurancePoolStatus(str, _PyEnum):
    ACTIVE = "active"
    DEPLETED = "depleted"     # reserves below minimum
    CLOSED = "closed"

class InsurancePolicyStatus(str, _PyEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CLAIMED = "claimed"
    CANCELLED = "cancelled"

class InsuranceClaimStatus(str, _PyEnum):
    PENDING = "pending"
    APPROVED = "approved"     # auto-approved by trigger
    PAID = "paid"
    REJECTED = "rejected"     # manual review override


class InsurancePool(Base):
    """Insurance pool funded by premium payments."""
    __tablename__ = "insurance_pools"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False)      # e.g. "escrow-default"
    coverage_type = Column(String(50), nullable=False)           # "escrow", "sla", "channel", "swap"
    
    # Pool reserves
    reserve_token = Column(String(10), default="XMR")
    total_reserves = Column(Numeric(30, 12), default=Decimal('0'))
    total_premiums_collected = Column(Numeric(30, 12), default=Decimal('0'))
    total_claims_paid = Column(Numeric(30, 12), default=Decimal('0'))
    
    # Risk parameters
    max_coverage_per_policy = Column(Numeric(30, 12), default=Decimal('100'))  # max 100 XMR per policy
    min_reserve_ratio = Column(Numeric(5, 4), default=Decimal('0.2'))         # min 20% of outstanding coverage
    
    status = Column(SQLEnum(InsurancePoolStatus), default=InsurancePoolStatus.ACTIVE)
    created_at = Column(DateTime(timezone=True), default=func.now())


class InsurancePolicy(Base):
    """An agent's insurance policy against a specific risk."""
    __tablename__ = "insurance_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pool_id = Column(UUID(as_uuid=True), ForeignKey("insurance_pools.id"), nullable=False, index=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    # What's insured
    insured_resource_type = Column(String(50), nullable=False)  # "escrow_deal", "sla_contract", "channel"
    insured_resource_id = Column(UUID(as_uuid=True), nullable=False)
    
    # Coverage
    coverage_amount = Column(Numeric(30, 12), nullable=False)
    premium_paid = Column(Numeric(30, 12), nullable=False)
    premium_rate_bps = Column(Integer, nullable=False)  # effective rate charged
    
    # Duration
    starts_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    status = Column(SQLEnum(InsurancePolicyStatus), default=InsurancePolicyStatus.ACTIVE)
    created_at = Column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        Index("ix_insurance_policies_resource", "insured_resource_type", "insured_resource_id"),
        CheckConstraint("coverage_amount > 0", name="ck_insurance_coverage_positive"),
        CheckConstraint("premium_paid > 0", name="ck_insurance_premium_positive"),
    )


class InsuranceClaim(Base):
    """Claim against an insurance policy."""
    __tablename__ = "insurance_claims"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_id = Column(UUID(as_uuid=True), ForeignKey("insurance_policies.id"), nullable=False, index=True)
    
    # Trigger event
    trigger_event_type = Column(String(100), nullable=False)  # "escrow.expired", "sla.breached"
    trigger_evidence = Column(JSON, nullable=False)           # snapshot of the event that triggered
    
    # Payout
    claim_amount = Column(Numeric(30, 12), nullable=False)
    payout_amount = Column(Numeric(30, 12), nullable=True)   # may differ if pool partially depleted
    
    status = Column(SQLEnum(InsuranceClaimStatus), default=InsuranceClaimStatus.PENDING)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
```

### API Design

```
# Pools
GET    /v2/insurance/pools                              -- list insurance pools with stats

# Policies
POST   /v2/insurance/policies                           -- purchase insurance
  Body: { "coverage_type": "escrow", "resource_id": "...", "coverage_amount": "10.0" }
  Response: { "policy_id": "...", "premium": "0.1", "expires_at": "..." }
GET    /v2/insurance/policies                           -- list my active policies

# Claims (mostly automatic, but queryable)
GET    /v2/insurance/claims                             -- list my claims
GET    /v2/insurance/claims/{claim_id}                  -- claim details

# Quotes
GET    /v2/insurance/quote                              -- get premium quote
  Query: ?coverage_type=escrow&resource_id=...&coverage_amount=10.0
```

### Integration with Existing Features

- **EscrowService**: When escrow expires, auto-trigger insurance claim if policy exists.
- **SLAService**: When SLA breaches, auto-trigger insurance claim.
- **ChannelService**: When channel dispute is resolved unfavorably, trigger claim.
- **SwapService**: When swap expires, trigger claim.
- **AgentReputation**: Trust score feeds premium calculation. Claim history affects future premiums.

---

## 4. Yield and Staking

### Why agents need this

Idle capital is wasted capital. Agents that maintain hub balances should earn yield. Yield creates stickiness -- agents keep funds in the system rather than withdrawing.

### Yield Sources

```
1. Liquidity Pool LP rewards:   0.3% of every swap (proportional to LP share)
2. Lending interest:            Variable APR (2-60% depending on utilization)
3. Hub fee sharing (staking):   Stake XMR to earn % of all hub fees
4. Insurance pool returns:      Premium income minus claims
```

### Hub Fee Staking

Agents stake XMR to earn a share of ALL hub fees (payments, escrow, swaps, lending).

```
Staking math:

  agent_share = agent_staked / total_staked
  agent_reward = total_hub_fees_this_period * distribution_share * agent_share

  distribution_share = 30%  (hub keeps 70% of fees, distributes 30% to stakers)
  period = 24 hours (rewards calculated and distributed daily)

  Minimum stake: 1 XMR
  Unstaking delay: 72 hours (prevents gaming around high-fee periods)
```

### Data Model

```python
class StakeStatus(str, _PyEnum):
    ACTIVE = "active"
    UNSTAKING = "unstaking"
    WITHDRAWN = "withdrawn"

class YieldStrategyType(str, _PyEnum):
    LP_PROVISION = "lp_provision"
    LENDING_DEPOSIT = "lending_deposit"
    HUB_STAKING = "hub_staking"
    INSURANCE_UNDERWRITING = "insurance_underwriting"


class HubStake(Base):
    """Agent's stake in the hub fee-sharing pool."""
    __tablename__ = "hub_stakes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    amount = Column(Numeric(30, 12), nullable=False)
    token = Column(String(10), default="XMR")
    
    status = Column(SQLEnum(StakeStatus), default=StakeStatus.ACTIVE)
    
    # Rewards tracking
    total_rewards_earned = Column(Numeric(30, 12), default=Decimal('0'))
    last_reward_at = Column(DateTime(timezone=True), nullable=True)
    
    # Unstaking
    unstake_requested_at = Column(DateTime(timezone=True), nullable=True)
    unstake_available_at = Column(DateTime(timezone=True), nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_stake_amount_positive"),
    )


class StakeRewardDistribution(Base):
    """Record of a reward distribution epoch."""
    __tablename__ = "stake_reward_distributions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    epoch_start = Column(DateTime(timezone=True), nullable=False)
    epoch_end = Column(DateTime(timezone=True), nullable=False)
    
    total_hub_fees = Column(Numeric(30, 12), nullable=False)
    distribution_amount = Column(Numeric(30, 12), nullable=False)  # total_hub_fees * 30%
    total_staked = Column(Numeric(30, 12), nullable=False)
    
    staker_count = Column(Integer, nullable=False)
    
    created_at = Column(DateTime(timezone=True), default=func.now())


class YieldPosition(Base):
    """Unified view of an agent's yield-generating positions."""
    __tablename__ = "yield_positions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    strategy_type = Column(SQLEnum(YieldStrategyType), nullable=False)
    source_id = Column(UUID(as_uuid=True), nullable=False)  # pool_id, market_id, stake_id, etc.
    
    # Invested
    principal_token = Column(String(10), nullable=False)
    principal_amount = Column(Numeric(30, 12), nullable=False)
    
    # Returns
    current_value = Column(Numeric(30, 12), nullable=False)
    unrealized_pnl = Column(Numeric(30, 12), default=Decimal('0'))
    realized_pnl = Column(Numeric(30, 12), default=Decimal('0'))
    
    # APY tracking (annualized)
    current_apy_bps = Column(Integer, default=0)
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
```

### API Design

```
# Staking
POST   /v2/staking/stake                -- stake XMR for hub fee rewards
  Body: { "amount": "10.0" }
POST   /v2/staking/unstake              -- initiate unstaking (72h delay)
  Body: { "stake_id": "..." }
POST   /v2/staking/claim-rewards        -- claim accumulated rewards
GET    /v2/staking/positions             -- my stake positions + pending rewards
GET    /v2/staking/stats                 -- global staking stats (total staked, APY, etc.)

# Unified yield view
GET    /v2/yield/positions               -- all yield positions across strategies
GET    /v2/yield/summary                 -- total portfolio value, PnL, blended APY
```

---

## 5. Compute Futures and Agent-Native Instruments

### Why agents need this

Traditional futures hedge commodity prices. In an AI agent economy, the "commodity" is compute, data, and agent services. Agents need to:
- Lock in today's price for future compute needs
- Hedge against rising service costs
- Guarantee availability of scarce agent services

### Compute Futures

```
A compute future is a contract between a buyer and seller:

  "Agent B will provide X units of service to Agent A at price P,
   deliverable between T1 and T2."

If the spot price at delivery > P: buyer profits (locked in cheaper price)
If the spot price at delivery < P: seller profits (sold at higher price)

Settlement: physical delivery (agent actually performs the service) 
            OR cash settlement (price difference paid in XMR/xUSD)
```

### SLA Bonds

Extends existing SLA system. Provider posts a bond that gets slashed on failure.

```
Current SLA: penalty_percent deducted from escrowed payment on breach.
Problem: penalty is limited to the payment amount.

SLA Bond: provider locks EXTRA collateral beyond payment.
  - Bond amount = 2x payment (configurable)
  - On successful delivery: bond returned + payment received
  - On breach: bond slashed by penalty_percent, returned to consumer
  - On consumer satisfaction: bond returned + bonus from penalty pool

This creates much stronger incentive for quality delivery.
```

### Revenue Sharing Tokens

Agents can tokenize a share of their future revenue:

```
Agent "super-translator" earns 50 XMR/month.
Issues 100 revenue tokens at 0.3 XMR each.
Each token entitles holder to 0.5% of monthly revenue.

Revenue flow:
  1. super-translator earns revenue through Sthrip
  2. Hub automatically diverts token_holder_share to token holders
  3. Token holders receive monthly distributions

Risk: agent may stop earning (token value -> 0)
Upside: investor gets exposure to top agents without operating them
```

### Data Model

```python
class FutureStatus(str, _PyEnum):
    LISTED = "listed"         # seller listed, waiting for buyer
    MATCHED = "matched"       # buyer + seller agreed
    ACTIVE = "active"         # past start date, awaiting delivery
    SETTLED = "settled"       # delivered or cash-settled
    EXPIRED = "expired"
    CANCELLED = "cancelled"

class BondStatus(str, _PyEnum):
    LOCKED = "locked"
    RELEASED = "released"     # returned to provider on success
    SLASHED = "slashed"       # forfeited on breach

class RevenueTokenStatus(str, _PyEnum):
    ACTIVE = "active"
    PAUSED = "paused"         # issuer paused distributions
    EXPIRED = "expired"       # past maturity date


class ComputeFuture(Base):
    """A futures contract for agent services."""
    __tablename__ = "compute_futures"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Parties
    seller_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    buyer_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True, index=True)
    
    # Contract terms
    service_type = Column(String(100), nullable=False)     # "translation", "code-review", etc.
    quantity = Column(Integer, nullable=False)              # number of service units
    unit_price = Column(Numeric(20, 8), nullable=False)    # price per unit in settlement token
    total_price = Column(Numeric(20, 8), nullable=False)   # quantity * unit_price
    settlement_token = Column(String(10), default="XMR")
    
    # Delivery window
    delivery_start = Column(DateTime(timezone=True), nullable=False)
    delivery_end = Column(DateTime(timezone=True), nullable=False)
    
    # Settlement type
    is_cash_settled = Column(Boolean, default=False)   # false = physical delivery, true = cash
    spot_price_at_settlement = Column(Numeric(20, 8), nullable=True)  # for cash settlement
    settlement_pnl = Column(Numeric(20, 8), nullable=True)            # profit/loss at settlement
    
    # Margin/collateral (both parties post margin)
    seller_margin = Column(Numeric(20, 8), nullable=True)
    buyer_margin = Column(Numeric(20, 8), nullable=True)
    margin_token = Column(String(10), default="XMR")
    
    status = Column(SQLEnum(FutureStatus), default=FutureStatus.LISTED)
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    settled_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_future_quantity_positive"),
        CheckConstraint("unit_price > 0", name="ck_future_price_positive"),
    )


class SLABond(Base):
    """Extra collateral bond posted by SLA provider."""
    __tablename__ = "sla_bonds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sla_contract_id = Column(UUID(as_uuid=True), ForeignKey("sla_contracts.id"), nullable=False, index=True)
    provider_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    bond_amount = Column(Numeric(20, 8), nullable=False)
    bond_token = Column(String(10), default="XMR")
    
    # Slash parameters
    slash_percent_on_breach = Column(Integer, default=100)   # % of bond slashed
    slash_amount = Column(Numeric(20, 8), nullable=True)     # actual slashed amount
    
    status = Column(SQLEnum(BondStatus), default=BondStatus.LOCKED)
    
    locked_at = Column(DateTime(timezone=True), default=func.now())
    released_at = Column(DateTime(timezone=True), nullable=True)
    slashed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("bond_amount > 0", name="ck_bond_amount_positive"),
        UniqueConstraint("sla_contract_id", name="uq_bond_per_contract"),
    )


class RevenueToken(Base):
    """Tokenized share of an agent's future revenue."""
    __tablename__ = "revenue_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Issuer (the agent whose revenue is tokenized)
    issuer_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    # Token parameters
    name = Column(String(100), nullable=False)          # e.g. "SUPRTRANS-REV"
    total_supply = Column(Integer, nullable=False)       # fixed supply
    price_per_token = Column(Numeric(20, 8), nullable=False)
    revenue_share_bps = Column(Integer, nullable=False)  # total % of revenue shared (e.g. 1000 = 10%)
    
    # Token economy
    tokens_sold = Column(Integer, default=0)
    total_raised = Column(Numeric(20, 8), default=Decimal('0'))
    total_distributed = Column(Numeric(20, 8), default=Decimal('0'))
    
    # Distribution schedule
    distribution_interval = Column(String(20), default="monthly")  # "daily", "weekly", "monthly"
    last_distribution_at = Column(DateTime(timezone=True), nullable=True)
    
    # Maturity
    maturity_date = Column(DateTime(timezone=True), nullable=True)  # null = perpetual
    
    status = Column(SQLEnum(RevenueTokenStatus), default=RevenueTokenStatus.ACTIVE)
    created_at = Column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        CheckConstraint("total_supply > 0", name="ck_revtoken_supply_positive"),
        CheckConstraint("revenue_share_bps > 0 AND revenue_share_bps <= 5000",
                        name="ck_revtoken_share_range"),  # max 50% of revenue
    )


class RevenueTokenHolding(Base):
    """An agent's holding of revenue tokens."""
    __tablename__ = "revenue_token_holdings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_id = Column(UUID(as_uuid=True), ForeignKey("revenue_tokens.id"), nullable=False, index=True)
    holder_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    
    quantity = Column(Integer, nullable=False)
    purchase_price = Column(Numeric(20, 8), nullable=False)   # total paid
    total_distributions_received = Column(Numeric(20, 8), default=Decimal('0'))
    
    created_at = Column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        UniqueConstraint("token_id", "holder_id", name="uq_token_holding"),
        CheckConstraint("quantity > 0", name="ck_holding_quantity_positive"),
    )
```

### API Design

```
# Compute Futures
POST   /v2/futures                         -- create/list a future
GET    /v2/futures                         -- browse available futures
POST   /v2/futures/{id}/buy               -- buy into a listed future
POST   /v2/futures/{id}/settle            -- settle a mature future
GET    /v2/futures/positions              -- my future positions

# SLA Bonds
POST   /v2/sla/{contract_id}/bond         -- provider posts bond
GET    /v2/sla/{contract_id}/bond         -- bond status
# (bond release/slash is automatic via SLA lifecycle hooks)

# Revenue Tokens
POST   /v2/revenue-tokens                 -- issuer creates token offering
GET    /v2/revenue-tokens                 -- browse available tokens
POST   /v2/revenue-tokens/{id}/buy        -- buy tokens
GET    /v2/revenue-tokens/holdings        -- my token holdings
GET    /v2/revenue-tokens/{id}/distributions -- distribution history
# (distributions are automatic via background task)
```

### Risk Analysis for Agent-Native Instruments

| Risk | Severity | Mitigation |
|---|---|---|
| Compute future: seller cannot deliver | HIGH | Seller margin locked; slashed on non-delivery; buyer refunded from margin |
| Revenue token: agent stops earning | HIGH | Minimum 3-month lockup; reputation penalty for zero-distribution periods |
| SLA bond: cascading slashes | MEDIUM | Bond size capped at 5x SLA price; minimum provider balance enforced |
| Revenue token: pump and dump | MEDIUM | Fixed supply, no secondary market initially (Phase 3 adds trading) |
| Future: price manipulation | LOW | Cash settlement uses TWAP from AMM pools (15-min window) |

---

## 6. Derivatives (Phase 3 -- Design Only)

### Perpetual Swaps

Continuous futures with funding rate mechanism. Agents can hedge XMR/USD exposure indefinitely.

```
Funding rate (paid every 8 hours):
  funding_rate = (mark_price - index_price) / index_price * 0.01
  
  If funding_rate > 0: longs pay shorts (market is bullish, need to correct)
  If funding_rate < 0: shorts pay longs (market is bearish)

Mark price: current pool price (AMM TWAP 15 min)
Index price: external rate (CoinGecko TWAP 1 hour)

Position sizing:
  max_leverage = 5x (conservative for agent economy)
  maintenance_margin = 5% of position
  initial_margin = 20% of position (1/leverage)
```

### Interest Rate Swaps

Agent A has a variable-rate subscription (pays xUSD based on usage).
Agent B wants predictable costs.

```
Swap: A pays B a fixed rate; B pays A the variable rate.

Example:
  Agent A subscription: variable, currently 10 xUSD/day
  Fixed rate offered: 12 xUSD/day
  
  Day 1: variable = 8 xUSD. A pays B 12, B pays A 8. Net: A pays 4.
  Day 2: variable = 15 xUSD. A pays B 12, B pays A 15. Net: B pays 3.
  
  A has cost certainty; B speculates on rates.
```

### Prediction Markets

Agents bet on task outcomes:

```
"Will agent X complete the translation job by deadline?"
  YES tokens: 0.7 xUSD each (market implies 70% probability)
  NO tokens: 0.3 xUSD each

  Resolution: automatic from SLA contract state
    - SLA COMPLETED before deadline -> YES pays 1 xUSD
    - SLA BREACHED or EXPIRED -> NO pays 1 xUSD

This is agent-native because resolution is deterministic from on-hub data.
No oracle needed.
```

---

## 7. Cross-Cutting Concerns

### Reentrancy Protection

Flash loans and composable operations create reentrancy risk. All DeFi operations must use a call-level guard:

```python
# In each DeFi service method:
class PoolService:
    _active_calls: set = set()  # per-request tracking via middleware
    
    def swap(self, db, pool_id, agent_id, ...):
        call_key = f"swap:{pool_id}:{agent_id}"
        if call_key in self._active_calls:
            raise ValueError("Reentrancy detected: cannot swap within a swap callback")
        self._active_calls.add(call_key)
        try:
            # ... actual swap logic
        finally:
            self._active_calls.discard(call_key)
```

### Atomic Composability

Agents need to chain operations atomically. This is implemented via database transactions:

```python
# All DeFi operations within a single request share one DB session.
# If any operation fails, the entire request rolls back.

POST /v2/defi/compose
Body: {
    "operations": [
        {"action": "pool.swap", "params": {...}},
        {"action": "lending.deposit", "params": {...}},
        {"action": "insurance.purchase", "params": {...}}
    ]
}

# Executed within a single SQLAlchemy session.commit()
# If step 3 fails, steps 1 and 2 are rolled back.
```

### TWAP Oracle

For cross-token price references (collateral valuation, cash settlement):

```python
class TWAPOracle:
    """Time-Weighted Average Price from AMM pool trade history."""
    
    def get_twap(self, db, pool_id, token, window_seconds=900):
        """15-minute TWAP from pool actions."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        actions = db.query(PoolAction).filter(
            PoolAction.pool_id == pool_id,
            PoolAction.action_type == LPActionType.SWAP,
            PoolAction.created_at >= cutoff,
        ).order_by(PoolAction.created_at).all()
        
        if not actions:
            # Fall back to current pool ratio
            pool = db.query(LiquidityPool).get(pool_id)
            return pool.reserve_b / pool.reserve_a  # price of A in terms of B
        
        # Time-weighted average of effective prices
        total_weight = Decimal('0')
        weighted_sum = Decimal('0')
        for i, action in enumerate(actions):
            if i + 1 < len(actions):
                dt = (actions[i+1].created_at - action.created_at).total_seconds()
            else:
                dt = (datetime.now(timezone.utc) - action.created_at).total_seconds()
            weight = Decimal(str(dt))
            weighted_sum += action.effective_price * weight
            total_weight += weight
        
        return weighted_sum / total_weight if total_weight > 0 else actions[-1].effective_price
```

### Background Tasks

New background tasks added to `api/main_v2.py` lifespan:

```
1. Interest accrual (lending):     every 1 hour   -- update borrow/supply indices
2. Liquidation scanner:            every 5 minutes -- find and execute liquidations
3. Staking reward distribution:    every 24 hours  -- calculate and distribute staker rewards
4. Insurance claim processor:      every 5 minutes -- check for triggered policies, auto-pay
5. Revenue token distributions:    every 24 hours  -- distribute revenue to token holders
6. Compute future expiry:          every 1 hour   -- expire/settle mature futures
7. SLA bond processor:             every 5 minutes -- release/slash bonds based on SLA state
```

### Privacy Considerations

All DeFi positions are private by default:
- Only the position owner can see their positions via API
- Admin dashboard shows aggregate statistics, not individual positions
- Pool reserves and trade history are public (necessary for price discovery)
- Insurance claims: only claimant and admin can see
- Revenue token holdings: only holder can see their holdings; issuer sees aggregate

### Fee Summary (Hub Revenue)

| DeFi Primitive | Fee | Goes To |
|---|---|---|
| AMM Swap | 0.05% of input | Hub revenue |
| AMM LP Fee | 0.3% of input | LP providers |
| Flash Loan | 0.09% flat | Hub revenue |
| Lending Interest | 10% of accrued interest | Hub revenue (reserve factor) |
| Insurance Premium | 100% of premium | Insurance pool (hub takes 0%) |
| Staking Rewards | 70% of hub fees retained | Hub revenue |
| Compute Future Settlement | 0.1% of settlement value | Hub revenue |
| Revenue Token Issuance | 1% of total raised | Hub revenue |
| Revenue Distribution | 0% | Direct to holders |

---

## 8. Implementation Phasing

### Phase 1 (Weeks 1-4): Foundation

1. **Liquidity Pools (AMM)** -- models, pool_service, pool router, tests
   - Start with XMR/xUSD constant product pool
   - Add xUSD/xEUR StableSwap pool
   - Integrate with ConversionService (route through AMM when possible)
   
2. **Flash Loans** -- models, flash_loan_service, router, tests
   - Operation whitelist: swap, pay, convert
   - Atomic rollback on failure
   
3. **Lending (deposits only)** -- models, lending_service, router, tests
   - Deposit/withdraw
   - Interest accrual (supply index)

### Phase 2 (Weeks 5-8): Core DeFi

4. **Lending (borrowing)** -- collateral, borrow, repay, liquidation
5. **Payment Insurance** -- pools, policies, automatic claims
6. **SLA Bonds** -- bond model, integration with SLA lifecycle
7. **Hub Staking** -- stake/unstake, reward distribution

### Phase 3 (Weeks 9-12): Advanced Instruments

8. **Compute Futures** -- future listing, matching, settlement
9. **Revenue Sharing Tokens** -- issuance, distribution, holdings
10. **Composability** -- `/v2/defi/compose` endpoint for atomic multi-step operations

### Phase 4 (Weeks 13+): Derivatives

11. **Perpetual Swaps** -- funding rate, margin, liquidation
12. **Prediction Markets** -- resolution from SLA/escrow state
13. **Agent Index Funds** -- basket creation, rebalancing

---

## 9. SDK Integration

All DeFi primitives should be accessible via the Python SDK (`pip install sthrip`):

```python
from sthrip import Sthrip

s = Sthrip(api_key="sk-...")

# AMM
quote = s.pool_quote(pool="XMR/xUSD", token_in="XMR", amount_in=1.5)
result = s.pool_swap(pool="XMR/xUSD", token_in="XMR", amount_in=1.5, min_out=265)

# Liquidity provision
s.pool_add_liquidity(pool="XMR/xUSD", amount_a=1.0, amount_b=180.0)
s.pool_remove_liquidity(pool="XMR/xUSD", shares=13.4)

# Lending
s.lending_deposit(token="XMR", amount=10.0)
s.lending_borrow(borrow_token="xUSD", amount=1000, collateral_token="XMR", collateral=8.0)
s.lending_repay(token="xUSD", amount=500)

# Flash loan
s.flash_loan(
    borrow_token="xUSD",
    borrow_amount=10000,
    operations=[
        {"action": "swap", "pool": "xUSD/XMR", "amount_in": 10000},
        {"action": "pay", "to": "compute-agent", "amount": 55, "token": "XMR"},
    ]
)

# Insurance
s.insurance_purchase(coverage_type="escrow", resource_id="...", amount=10.0)

# Staking
s.stake(amount=10.0)
s.claim_rewards()

# Futures
s.future_create(service="translation", quantity=100, unit_price=0.01, delivery_start="2026-05-01")
s.future_buy(future_id="...")

# Revenue tokens
s.revenue_token_create(name="MY-REV", supply=100, price=0.3, share_bps=1000)
s.revenue_token_buy(token_id="...", quantity=10)

# Composability
s.compose([
    s.op.swap(pool="XMR/xUSD", token_in="XMR", amount_in=5.0),
    s.op.lending_deposit(token="xUSD", amount=800),
    s.op.insurance_purchase(coverage_type="lending", resource_id="auto", amount=800),
])
```

---

## 10. MCP Server Integration

Add DeFi tools to the MCP server (`integrations/sthrip_mcp/`):

```
New MCP tools (Phase 1-2):
  pool_swap           -- swap tokens via AMM
  pool_add_liquidity  -- provide liquidity
  pool_quote          -- get swap quote
  lending_deposit     -- deposit to earn interest
  lending_borrow      -- borrow against collateral
  lending_repay       -- repay loan
  flash_loan          -- execute flash loan
  insurance_purchase  -- buy insurance
  stake               -- stake XMR for rewards
  yield_summary       -- portfolio yield overview

Total MCP tools: 19 (existing) + 10 (DeFi) = 29
```

---

## Appendix A: Mathematical Reference

### Constant Product AMM

```
Given reserves (x, y) and input amount dx (before fee):
  fee = dx * fee_rate
  dx_after_fee = dx - fee
  dy = y * dx_after_fee / (x + dx_after_fee)
  
  New reserves: (x + dx, y - dy)
  New k: (x + dx) * (y - dy) >= k  (k never decreases due to fees)

LP share calculation on deposit:
  If pool is empty: shares = sqrt(amount_a * amount_b) - MINIMUM_LIQUIDITY
  If pool exists: shares = min(
      amount_a * total_shares / reserve_a,
      amount_b * total_shares / reserve_b
  )

Withdrawal:
  amount_a = shares * reserve_a / total_shares
  amount_b = shares * reserve_b / total_shares

Impermanent Loss:
  price_ratio = current_price / entry_price
  IL = 2 * sqrt(price_ratio) / (1 + price_ratio) - 1
  
  Example: XMR doubles in price (ratio = 2)
    IL = 2 * 1.414 / 3 - 1 = -5.72%
```

### Interest Rate (Lending)

```
Compound interest accumulation (continuous):
  borrow_index_new = borrow_index_old * (1 + borrow_rate * dt / SECONDS_PER_YEAR)
  supply_index_new = supply_index_old * (1 + supply_rate * dt / SECONDS_PER_YEAR)

Where:
  dt = seconds since last update
  SECONDS_PER_YEAR = 31536000

Agent's actual borrow balance:
  actual_debt = borrow_shares * borrow_index

Agent's actual deposit balance:
  actual_balance = deposit_shares * supply_index
```

### Insurance Premium

```
premium = coverage_amount * base_rate * duration_months * risk_multiplier

risk_multiplier = max(0.5, min(2.0,
    1.0
    + counterparty_adjustment     # -0.3 to +0.5 based on trust_score
    + volume_discount             # -0.1 for >100 XMR
    + claims_history_penalty      # +0.2 per past claim
))
```
