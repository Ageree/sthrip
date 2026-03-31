# Implementation Plan: Phase 3c -- Multi-Currency (Cross-Chain Swaps, Stablecoins)

## Goal

Add multi-currency support (Section B from Phase 3 spec): cross-chain atomic swaps between BTC and XMR via HTLC, virtual stablecoin balances (xUSD, xEUR) backed by XMR reserves, currency conversion, and multi-currency payment support. Enables agents that hold BTC to participate and provides stable pricing.

## Architecture

- **Enums**: `SwapStatus` added to `sthrip/db/enums.py`
- **Models**: `SwapOrder`, `CurrencyConversion` in `sthrip/db/models.py`; `AgentBalance` extended with `xusd_balance`, `xeur_balance`
- **Repos**: `swap_repo.py`, `conversion_repo.py` in `sthrip/db/`
- **Services**: `rate_service.py` (exchange rates), `htlc_service.py` (BTC HTLC), `swap_coordinator_service.py`, `conversion_service.py`
- **Router**: `swap.py`, `conversion.py` in `api/routers/`
- **External**: CoinGecko/Kraken API for rates, Redis for rate caching
- **Existing**: Leverages `sthrip/swaps/btc/htlc.py` and `sthrip/swaps/coordinator.py`

## Tech Stack

- Python 3.9, FastAPI, SQLAlchemy 2.0, PostgreSQL (prod), SQLite (tests)
- `aiohttp` for async rate fetching
- Redis for rate caching (60s TTL)
- Existing HTLC infrastructure in `sthrip/swaps/`
- pytest with mocked external APIs

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `sthrip/services/rate_service.py` | Exchange rate fetching (CoinGecko/Kraken), Redis caching |
| `sthrip/services/htlc_service.py` | Hub-side HTLC management for swaps |
| `sthrip/services/swap_coordinator_service.py` | Swap lifecycle orchestration |
| `sthrip/services/conversion_service.py` | XMR <-> xUSD/xEUR conversion logic |
| `sthrip/db/swap_repo.py` | SwapOrder CRUD |
| `sthrip/db/conversion_repo.py` | CurrencyConversion CRUD |
| `api/routers/swap.py` | Swap endpoints |
| `api/routers/conversion.py` | Conversion + multi-currency balance endpoints |
| `api/schemas_swap.py` | Swap Pydantic models |
| `api/schemas_conversion.py` | Conversion Pydantic models |
| `migrations/versions/i0j1k2l3m4n5_multi_currency.py` | Migration |
| `tests/test_rate_service.py` | Rate service tests |
| `tests/test_swap.py` | Swap tests |
| `tests/test_conversion.py` | Conversion tests |
| `tests/test_multi_currency_pay.py` | Multi-currency payment tests |

### Modified Files

| File | Changes |
|------|---------|
| `sthrip/db/enums.py` | Add `SwapStatus` |
| `sthrip/db/models.py` | Add `SwapOrder`, `CurrencyConversion` models |
| `sthrip/db/balance_repo.py` | Support multi-token operations (xUSD, xEUR tokens) |
| `sthrip/db/repository.py` | Re-export new repos |
| `api/main_v2.py` | Register 2 new routers |
| `api/routers/balance.py` | Extend balance response with multi-currency |
| `api/routers/payments.py` | Accept `currency` parameter on pay endpoint |
| `sdk/sthrip/client.py` | Add `swap_*`, `convert` methods |
| `tests/conftest.py` | Add new tables and modules |

---

## Implementation Steps

### Task 1: SwapOrder Model + Repo + Tests

#### Step 1.1: Add SwapStatus enum
- [ ] **File**: `sthrip/db/enums.py`
- [ ] **Action**: Add:
  ```python
  class SwapStatus(str, _PyEnum):
      CREATED = "created"
      LOCKED = "locked"
      COMPLETED = "completed"
      REFUNDED = "refunded"
      EXPIRED = "expired"
  ```
- [ ] **Action**: Add to `__all__`

#### Step 1.2: Add SwapOrder model
- [ ] **File**: `sthrip/db/models.py`
- [ ] **Action**: Add model:
  ```python
  class SwapOrder(Base):
      __tablename__ = "swap_orders"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      from_currency = Column(String(10), nullable=False)
      from_amount = Column(Numeric(20, 8), nullable=False)
      to_currency = Column(String(10), nullable=False, default="XMR")
      to_amount = Column(Numeric(20, 8), nullable=False)
      exchange_rate = Column(Numeric(20, 8), nullable=False)
      fee_amount = Column(Numeric(20, 8), nullable=False)
      state = Column(SQLEnum(SwapStatus), default=SwapStatus.CREATED)
      htlc_hash = Column(String(64), nullable=False)
      htlc_secret = Column(String(64), nullable=True)
      btc_tx_hash = Column(String(64), nullable=True)
      xmr_tx_hash = Column(String(64), nullable=True)
      lock_expiry = Column(DateTime(timezone=True), nullable=False)
      created_at = Column(DateTime(timezone=True), default=func.now())
      from_agent = relationship("Agent", foreign_keys=[from_agent_id])
  ```
- [ ] **Action**: Add `CurrencyConversion` model:
  ```python
  class CurrencyConversion(Base):
      __tablename__ = "currency_conversions"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      from_currency = Column(String(10), nullable=False)
      from_amount = Column(Numeric(20, 8), nullable=False)
      to_currency = Column(String(10), nullable=False)
      to_amount = Column(Numeric(20, 8), nullable=False)
      rate = Column(Numeric(20, 8), nullable=False)
      fee_amount = Column(Numeric(20, 8), nullable=False)
      created_at = Column(DateTime(timezone=True), default=func.now())
      agent = relationship("Agent", foreign_keys=[agent_id])
  ```

#### Step 1.3: Write swap repo tests (RED)
- [ ] **File**: `tests/test_swap.py`
- [ ] **Action**: Write `TestSwapRepository`:
  - `test_create_swap_order`
  - `test_get_by_id`
  - `test_lock_order` (created -> locked)
  - `test_complete_order` (locked -> completed, stores htlc_secret)
  - `test_refund_order` (locked -> refunded)
  - `test_expire_order` (created -> expired)
  - `test_list_by_agent`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_swap.py::TestSwapRepository -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 1.4: Implement swap repo (GREEN)
- [ ] **File**: `sthrip/db/swap_repo.py`
- [ ] **Action**: Create `SwapRepository`:
  - `create(from_agent_id, from_currency, from_amount, to_currency, to_amount, exchange_rate, fee_amount, htlc_hash, lock_expiry) -> SwapOrder`
  - `get_by_id(swap_id) -> Optional[SwapOrder]`
  - `get_by_id_for_update(swap_id) -> Optional[SwapOrder]`
  - `lock(swap_id, btc_tx_hash) -> int` (created -> locked)
  - `complete(swap_id, htlc_secret, xmr_tx_hash) -> int` (locked -> completed)
  - `refund(swap_id) -> int` (locked -> refunded)
  - `expire(swap_id) -> int` (created -> expired)
  - `list_by_agent(agent_id, limit, offset) -> Tuple[List, int]`
  - `get_expired() -> List[SwapOrder]`
- [ ] **Action**: Register in `sthrip/db/repository.py`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_swap.py::TestSwapRepository -x -v`
- [ ] **Expected**: All tests pass

---

### Task 2: Rate Service (CoinGecko/Kraken API, Redis Cache) + Tests

#### Step 2.1: Write rate service tests (RED)
- [ ] **File**: `tests/test_rate_service.py`
- [ ] **Action**: Write tests:
  - `test_get_rate_btc_xmr` -- returns Decimal rate
  - `test_get_rate_eth_xmr` -- returns Decimal rate
  - `test_get_rate_cached` -- second call returns cached value (no HTTP call)
  - `test_get_rate_cache_expired` -- after 60s, refetches
  - `test_get_rate_api_failure_fallback` -- CoinGecko fails, uses Kraken
  - `test_get_rate_both_fail` -- raises ValueError
  - `test_xmr_usd_rate` -- used for stablecoin conversions
  - `test_slippage_check` -- rejects if rate deviates >2% from quote
- [ ] **Action**: Mock `aiohttp.ClientSession` responses
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_rate_service.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 2.2: Implement rate service (GREEN)
- [ ] **File**: `sthrip/services/rate_service.py`
- [ ] **Action**: Create `RateService`:
  - `__init__(self, redis_url=None)` -- connects to Redis if available, fallback to dict cache
  - `async get_rate(from_currency, to_currency) -> Decimal`:
    1. Check Redis/dict cache (key: `rate:{from}:{to}`, TTL 60s)
    2. If miss: fetch from CoinGecko API (`/simple/price?ids=monero,bitcoin,ethereum&vs_currencies=usd`)
    3. Calculate cross-rate (e.g., BTC/XMR = BTC/USD / XMR/USD)
    4. Cache result
    5. Fallback: try Kraken API if CoinGecko fails
  - `get_rate_sync(from_currency, to_currency) -> Decimal`:
    - Synchronous wrapper for non-async contexts
    - Uses `requests` library instead of `aiohttp`
  - `get_quote(from_currency, from_amount, to_currency) -> dict`:
    1. Get rate
    2. Calculate `to_amount = from_amount * rate`
    3. Calculate `fee = to_amount * 0.01` (1% for swaps, 0.5% for conversions)
    4. Return `{to_amount, rate, fee, expires_in: 300}`
  - `check_slippage(quoted_rate, current_rate, max_slippage=0.02) -> bool`
- [ ] **Why**: Centralized rate fetching with caching and fallback
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_rate_service.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 3: HTLC Service (BTC Side) + Tests

#### Step 3.1: Write HTLC service tests (RED)
- [ ] **File**: `tests/test_swap.py` (append)
- [ ] **Action**: Add `TestHTLCService`:
  - `test_generate_secret_and_hash` -- generates 32-byte secret, SHA256 hash
  - `test_create_htlc` -- creates HTLC script using existing `sthrip/swaps/btc/htlc.py`
  - `test_verify_htlc_claim` -- secret matches hash
  - `test_verify_htlc_claim_invalid` -- wrong secret rejected
  - `test_htlc_timeout_refund` -- after lock_expiry, refund is possible
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_swap.py::TestHTLCService -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 3.2: Implement HTLC service (GREEN)
- [ ] **File**: `sthrip/services/htlc_service.py`
- [ ] **Action**: Create `HTLCService`:
  - `generate_secret() -> tuple[str, str]`: Returns (secret_hex, hash_hex) using `secrets.token_bytes(32)` + SHA256
  - `create_htlc_for_swap(buyer_pubkey, seller_pubkey, hash_hex, lock_time) -> dict`: Wraps existing `sthrip/swaps/btc/htlc.py` functions
  - `verify_secret(secret_hex, hash_hex) -> bool`: SHA256(secret) == hash
  - `is_expired(lock_expiry: datetime) -> bool`: now > lock_expiry
- [ ] **Why**: Thin wrapper making existing HTLC code usable from the swap coordinator
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_swap.py::TestHTLCService -x -v`
- [ ] **Expected**: All tests pass

---

### Task 4: Swap Coordinator Service + Tests

#### Step 4.1: Write swap coordinator tests (RED)
- [ ] **File**: `tests/test_swap.py` (append)
- [ ] **Action**: Add `TestSwapCoordinatorService`:
  - `test_create_swap_btc_to_xmr` -- creates swap order, locks XMR in escrow
  - `test_create_swap_xmr_to_btc` -- creates swap order, locks XMR from balance
  - `test_create_swap_invalid_pair` -- rejects unsupported currency pair
  - `test_lock_swap` -- records BTC transaction hash, transitions to locked
  - `test_complete_swap` -- secret revealed, XMR credited, swap completed
  - `test_expire_swap` -- past lock_expiry, refunds both sides
  - `test_swap_fee_calculation` -- 1% fee on to_amount
  - `test_slippage_protection` -- rejects if rate moved >2% since quote
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_swap.py::TestSwapCoordinatorService -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 4.2: Implement swap coordinator (GREEN)
- [ ] **File**: `sthrip/services/swap_coordinator_service.py`
- [ ] **Action**: Create `SwapCoordinatorService`:
  - `create_swap(db, from_agent_id, from_currency, from_amount) -> dict`:
    1. Validate currency pair (BTC/ETH -> XMR only for Phase 1)
    2. Get quote from `RateService`
    3. Generate HTLC secret + hash
    4. Lock XMR amount in escrow (hub holds)
    5. Create SwapOrder record
    6. Return order with BTC address to send to
  - `lock_swap(db, swap_id, btc_tx_hash) -> dict`:
    1. Verify BTC transaction exists (via watcher or manual confirmation)
    2. Transition to locked
  - `complete_swap(db, swap_id, htlc_secret) -> dict`:
    1. Verify secret matches hash
    2. Credit XMR to buyer's balance
    3. Deduct fee
    4. Transition to completed
  - `expire_stale(db) -> int`:
    1. Find orders past lock_expiry in created/locked state
    2. Refund locked amounts
    3. Transition to expired
  - `get_supported_pairs() -> list`
  - `get_rates() -> dict`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_swap.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 5: Swap API Endpoints + Tests

#### Step 5.1: Create swap schemas
- [ ] **File**: `api/schemas_swap.py`
- [ ] **Action**: Create Pydantic models:
  - `SwapRatesResponse` -- dict of pair -> rate
  - `SwapQuoteRequest` -- from_currency (str), from_amount (Decimal, gt 0), to_currency (str, default "XMR")
  - `SwapQuoteResponse` -- to_amount, rate, fee, expires_in
  - `SwapCreateRequest` -- from_currency (str), from_amount (Decimal, gt 0)
  - `SwapCreateResponse` -- swap_id, state, btc_address (nullable), htlc_hash, lock_expiry
  - `SwapClaimRequest` -- secret (str, length 64)
  - `SwapStatusResponse` -- all swap fields

#### Step 5.2: Write swap API tests (RED)
- [ ] **File**: `tests/test_swap.py` (append)
- [ ] **Action**: Add `TestSwapAPI`:
  - `test_get_rates` -- GET `/v2/swap/rates`
  - `test_get_quote` -- POST `/v2/swap/quote`
  - `test_create_swap` -- POST `/v2/swap/create`
  - `test_get_swap_status` -- GET `/v2/swap/{id}`
  - `test_claim_swap` -- POST `/v2/swap/{id}/claim`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_swap.py::TestSwapAPI -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 5.3: Implement swap router (GREEN)
- [ ] **File**: `api/routers/swap.py`
- [ ] **Action**: Create router `APIRouter(prefix="/v2/swap", tags=["swap"])`:
  - `GET /rates` -- current exchange rates (public, cached)
  - `POST /quote` -- get quote (auth required)
  - `POST /create` -- create swap order (auth required)
  - `GET /{id}` -- swap status (auth required, only swap participant)
  - `POST /{id}/claim` -- claim with secret (auth required)
- [ ] **Action**: Register in `api/main_v2.py`
- [ ] **Action**: Update `tests/conftest.py`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_swap.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 6: Virtual Stablecoin Balances (Extend AgentBalance) + Tests

#### Step 6.1: Extend balance system for multi-token
- [ ] **File**: `sthrip/db/models.py`
- [ ] **Action**: The existing `AgentBalance` model already has a `token` column with `UniqueConstraint("agent_id", "token")`. This means the system already supports multiple tokens per agent -- we just need to create rows with `token="xUSD"` and `token="xEUR"`.
- [ ] **Action**: No model change needed -- the design spec's suggestion of adding `xusd_balance` and `xeur_balance` columns is unnecessary since the existing multi-row-per-token design is more flexible.
- [ ] **Why**: Leveraging existing design avoids migration complexity

#### Step 6.2: Write multi-token balance tests (RED)
- [ ] **File**: `tests/test_conversion.py`
- [ ] **Action**: Write `TestMultiTokenBalance`:
  - `test_create_xusd_balance` -- `get_or_create(agent_id, "xUSD")` creates new row
  - `test_create_xeur_balance` -- same for xEUR
  - `test_deposit_xusd` -- adds to available xUSD balance
  - `test_deduct_xusd` -- deducts from xUSD balance
  - `test_balance_isolation` -- XMR and xUSD balances are independent
  - `test_balance_non_negative` -- check constraint prevents negative xUSD
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_conversion.py::TestMultiTokenBalance -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests should pass (existing repo supports this) -- verify no issues

#### Step 6.3: Verify existing balance repo handles multi-token (GREEN)
- [ ] **File**: `sthrip/db/balance_repo.py`
- [ ] **Action**: Verify `get_or_create`, `_get_for_update`, `deposit`, `deduct` all accept `token` parameter correctly. They do -- the existing code filters on `(agent_id, token)`.
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_conversion.py::TestMultiTokenBalance -x -v`
- [ ] **Expected**: All tests pass

---

### Task 7: Currency Conversion Service + API + Tests

#### Step 7.1: Write conversion repo tests (RED)
- [ ] **File**: `tests/test_conversion.py` (append)
- [ ] **Action**: Add `TestConversionRepository`:
  - `test_create_conversion_record`
  - `test_list_by_agent`
- [ ] **Test command**: fails initially

#### Step 7.2: Implement conversion repo (GREEN)
- [ ] **File**: `sthrip/db/conversion_repo.py`
- [ ] **Action**: Create `ConversionRepository`:
  - `create(agent_id, from_currency, from_amount, to_currency, to_amount, rate, fee_amount) -> CurrencyConversion`
  - `list_by_agent(agent_id, limit, offset) -> Tuple[List, int]`
- [ ] **Action**: Register in `sthrip/db/repository.py`

#### Step 7.3: Write conversion service tests (RED)
- [ ] **File**: `tests/test_conversion.py` (append)
- [ ] **Action**: Add `TestConversionService`:
  - `test_convert_xmr_to_xusd` -- deducts XMR, credits xUSD at market rate minus 0.5% fee
  - `test_convert_xusd_to_xmr` -- deducts xUSD, credits XMR
  - `test_convert_insufficient_balance` -- raises ValueError
  - `test_convert_invalid_pair` -- rejects unsupported pairs
  - `test_conversion_fee` -- 0.5% fee on converted amount
  - `test_conversion_records_history` -- CurrencyConversion record created

#### Step 7.4: Implement conversion service (GREEN)
- [ ] **File**: `sthrip/services/conversion_service.py`
- [ ] **Action**: Create `ConversionService`:
  - `convert(db, agent_id, from_currency, to_currency, amount) -> dict`:
    1. Get rate from `RateService` (XMR/USD for xUSD conversions)
    2. Calculate `to_amount = amount * rate`
    3. Calculate `fee = to_amount * 0.005` (0.5%)
    4. `net_to_amount = to_amount - fee`
    5. Deduct `amount` from source balance (`BalanceRepository.deduct(agent_id, from_currency)`)
    6. Credit `net_to_amount` to target balance (`BalanceRepository.deposit(agent_id, to_currency)`)
    7. Record fee via `FeeCollection`
    8. Record conversion history
    9. Return result dict
  - `get_supported_currencies() -> list` -- returns ["XMR", "xUSD", "xEUR"]

#### Step 7.5: Create conversion schemas + router
- [ ] **File**: `api/schemas_conversion.py`
- [ ] **Action**: Create:
  - `ConvertRequest` -- from_currency (str), to_currency (str), amount (Decimal, gt 0)
  - `ConvertResponse` -- from_currency, from_amount, to_currency, to_amount, rate, fee
  - `MultiCurrencyBalanceResponse` -- xmr (dict), xusd (dict), xeur (dict)
- [ ] **File**: `api/routers/conversion.py`
- [ ] **Action**: Create router `APIRouter(prefix="/v2", tags=["conversion"])`:
  - `POST /balance/convert` -- convert between currencies
  - `GET /balance` -- extend existing to return all currency balances (or add `/balance/all`)
- [ ] **Action**: Register in `api/main_v2.py`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_conversion.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 8: Multi-Currency Payment Support (Extend Pay Endpoint) + Tests

#### Step 8.1: Write multi-currency payment tests (RED)
- [ ] **File**: `tests/test_multi_currency_pay.py`
- [ ] **Action**: Write tests:
  - `test_pay_in_xusd` -- `POST /v2/payments/hub-routing` with `currency=xUSD`
  - `test_pay_xusd_insufficient` -- xUSD balance too low
  - `test_pay_xusd_to_xmr_agent` -- auto-converts at receiver side
  - `test_pay_default_xmr` -- existing behavior unchanged when no currency specified
  - `test_subscription_in_xusd` -- recurring payment with `currency=xUSD`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_multi_currency_pay.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 8.2: Extend payment endpoint (GREEN)
- [ ] **File**: `api/schemas.py`
- [ ] **Action**: Add `currency` field to `HubPaymentRequest`:
  ```python
  currency: str = Field(default="XMR", pattern=r"^(XMR|xUSD|xEUR)$")
  ```
- [ ] **File**: `api/routers/payments.py`
- [ ] **Action**: When `currency != "XMR"`:
  1. Deduct from sender's `{currency}` balance
  2. Credit to receiver's `{currency}` balance
  3. Fee calculated in the payment currency
- [ ] **File**: `sthrip/services/recurring_service.py` (if implemented in Phase 3b)
- [ ] **Action**: Support `currency` field on RecurringPayment
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_multi_currency_pay.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 9: SDK Methods + Tests

#### Step 9.1: Write SDK tests (RED)
- [ ] **File**: `tests/test_sdk_multi_currency.py`
- [ ] **Action**: Write tests:
  - `test_swap_rates` -- calls `GET /v2/swap/rates`
  - `test_swap_quote` -- calls `POST /v2/swap/quote`
  - `test_swap_create` -- calls `POST /v2/swap/create`
  - `test_convert` -- calls `POST /v2/balance/convert`
  - `test_pay_with_currency` -- calls hub-routing with currency param
  - `test_balance_multi_currency` -- calls `GET /v2/balance` shows all currencies

#### Step 9.2: Implement SDK methods (GREEN)
- [ ] **File**: `sdk/sthrip/client.py`
- [ ] **Action**: Add methods:
  ```python
  def swap_rates(self):
      return self._raw_get("/v2/swap/rates", authenticated=False)

  def swap_quote(self, from_currency, from_amount):
      payload = {"from_currency": from_currency, "from_amount": str(from_amount)}
      return self._raw_post("/v2/swap/quote", json_body=payload)

  def swap(self, from_currency, from_amount):
      payload = {"from_currency": from_currency, "from_amount": str(from_amount)}
      return self._raw_post("/v2/swap/create", json_body=payload)

  def convert(self, from_currency, to_currency, amount):
      payload = {"from_currency": from_currency, "to_currency": to_currency, "amount": str(amount)}
      return self._raw_post("/v2/balance/convert", json_body=payload)
  ```
- [ ] **Action**: Extend `pay` method to accept `currency` kwarg:
  ```python
  def pay(self, agent_name, amount, memo=None, currency="XMR"):
      # ... existing validation ...
      payload = {"to_agent_name": agent_name, "amount": str(amount), "urgency": "normal"}
      if currency != "XMR":
          payload["currency"] = currency
      if memo is not None:
          payload["memo"] = memo
      # ... rest unchanged ...
  ```
- [ ] **Action**: Extend `subscribe` to accept `currency` kwarg
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sdk_multi_currency.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 10: Alembic Migration

#### Step 10.1: Create migration
- [ ] **File**: `migrations/versions/i0j1k2l3m4n5_multi_currency.py`
- [ ] **Action**: Migration for:
  - Create `swapstatus` enum (PostgreSQL only, with `DO $$ BEGIN ... EXCEPTION WHEN duplicate_object`)
  - Create `swap_orders` table
  - Create `currency_conversions` table
  - No changes to `agent_balances` (existing multi-row design handles xUSD/xEUR)
- [ ] **Why**: Minimal, idempotent migration
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -c "from migrations.versions.i0j1k2l3m4n5_multi_currency import upgrade; print('OK')"`
- [ ] **Expected**: Prints `OK`

---

## Testing Strategy

- **Unit tests**: Rate caching, HTLC secret/hash, fee calculations, conversion math
- **Integration tests**: Full swap lifecycle, conversion flow, multi-currency payments
- **SDK tests**: Method signatures, request construction
- **Mocking**: `aiohttp.ClientSession` for rate APIs, Bitcoin RPC for HTLC verification
- **Target coverage**: 85%+ on new code

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Exchange rate manipulation | Critical | Max 2% slippage from quoted rate; rate cached per 60s |
| HTLC secret leaked before BTC confirmed | Critical | Hub only reveals XMR after BTC confirmation (6 blocks) |
| Rate API downtime | High | Dual-source (CoinGecko + Kraken); fallback to last cached rate |
| Virtual stablecoin depegging | Medium | Hub maintains XMR reserves >= sum(xUSD) / XMR_USD_rate |
| Swap timeout race | Medium | Lock expiry has 15-min buffer; status-guarded UPDATEs |
| Conversion rounding errors | Low | Numeric(20,8) precision; all arithmetic uses Decimal |

## Success Criteria

- [ ] Exchange rates fetched from CoinGecko/Kraken with 60s Redis cache
- [ ] Swap quote valid for 5 minutes with 2% slippage protection
- [ ] BTC -> XMR swap lifecycle works (create -> lock -> claim)
- [ ] XMR -> BTC swap lifecycle works
- [ ] Stale swaps auto-expire and refund
- [ ] xUSD and xEUR virtual balances work (deposit, deduct, transfer)
- [ ] XMR <-> xUSD conversion works with 0.5% fee
- [ ] Payments can be made in xUSD/xEUR
- [ ] SDK has swap_rates, swap_quote, swap, convert methods
- [ ] All tests pass (80%+ coverage)
- [ ] Migration is idempotent

---

## Summary of All Three Plans

| Plan | Feature | Tasks | New Files | New Tests |
|------|---------|-------|-----------|-----------|
| 3a | Marketplace v2 | 11 | 20 | ~65 |
| 3b | Payment Scaling | 10 | 18 | ~55 |
| 3c | Multi-Currency | 10 | 15 | ~45 |

**Implementation order**: 3a first (drives adoption), then 3b (scales payments), then 3c (multi-currency, most complex due to external dependencies).

**Key files referenced across all three plans**:
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/db/models.py` -- all new models
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/db/enums.py` -- all new enums
- `/Users/saveliy/Documents/Agent Payments/sthrip/sthrip/db/repository.py` -- re-export facade
- `/Users/saveliy/Documents/Agent Payments/sthrip/api/main_v2.py` -- router registration + background tasks
- `/Users/saveliy/Documents/Agent Payments/sthrip/tests/conftest.py` -- test table + module lists
- `/Users/saveliy/Documents/Agent Payments/sthrip/sdk/sthrip/client.py` -- SDK methods
- `/Users/saveliy/Documents/Agent Payments/sthrip/docs/superpowers/specs/2026-04-01-phase3-abc-design.md` -- source design spec