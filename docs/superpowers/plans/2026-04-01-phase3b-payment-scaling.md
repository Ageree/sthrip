# Implementation Plan: Phase 3b -- Payment Scaling (Channels, Subscriptions, Streams)

## Goal

Scale the payment infrastructure (Section A from Phase 3 spec): off-chain payment channels for free micropayments, server-side recurring payments (subscriptions), and real-time payment streaming built on channels. Reduces fees for high-frequency agent interactions by 100x.

## Architecture

- **Enums**: `RecurringInterval`, `StreamStatus` added to `sthrip/db/enums.py`; `ChannelStatus` extended with `settled`
- **Models**: `ChannelUpdate`, `RecurringPayment`, `PaymentStream` in `sthrip/db/models.py`; `PaymentChannel` extended with new fields
- **Repos**: `channel_repo.py` extended, `recurring_repo.py`, `stream_repo.py` new
- **Services**: `channel_service.py`, `recurring_service.py`, `stream_service.py` in `sthrip/services/`
- **Routers**: `channels.py`, `subscriptions.py`, `streams.py` in `api/routers/`
- **Background task**: Recurring payment cron (5-min), stream accrual check
- **SDK**: New methods on `Sthrip` class
- **Crypto**: Ed25519 signing utilities for off-chain state

## Tech Stack

- Python 3.9, FastAPI, SQLAlchemy 2.0, PostgreSQL (prod), SQLite (tests)
- Ed25519 signatures via `PyNaCl` (already available for messaging crypto)
- pytest for TDD

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `sthrip/services/channel_service.py` | Channel open/close/settle/dispute logic |
| `sthrip/services/channel_signing.py` | Ed25519 state signing + verification |
| `sthrip/services/recurring_service.py` | Subscription execution, spending policy checks |
| `sthrip/services/stream_service.py` | Stream lifecycle, accrual calculation |
| `sthrip/db/recurring_repo.py` | RecurringPayment CRUD |
| `sthrip/db/stream_repo.py` | PaymentStream CRUD |
| `api/routers/channels.py` | Channel endpoints |
| `api/routers/subscriptions.py` | Subscription endpoints |
| `api/routers/streams.py` | Stream endpoints |
| `api/schemas_channels.py` | Channel Pydantic models |
| `api/schemas_subscriptions.py` | Subscription Pydantic models |
| `api/schemas_streams.py` | Stream Pydantic models |
| `migrations/versions/h9i0j1k2l3m4_payment_scaling.py` | Migration |
| `tests/test_channel_service.py` | Channel service unit tests |
| `tests/test_channel_signing.py` | Ed25519 signing tests |
| `tests/test_channel_api.py` | Channel API integration tests |
| `tests/test_recurring.py` | Subscription tests |
| `tests/test_streams.py` | Stream tests |

### Modified Files

| File | Changes |
|------|---------|
| `sthrip/db/enums.py` | Add `RecurringInterval`, `StreamStatus`; extend `ChannelStatus` |
| `sthrip/db/models.py` | Extend `PaymentChannel`, add `ChannelUpdate`, `RecurringPayment`, `PaymentStream` |
| `sthrip/db/channel_repo.py` | Add new methods for Phase 3 channel ops |
| `sthrip/db/repository.py` | Re-export new repos |
| `api/main_v2.py` | Register 3 routers, add recurring payment cron |
| `sdk/sthrip/client.py` | Add channel/subscription/stream methods |
| `tests/conftest.py` | Add new tables and modules |

---

## Implementation Steps

### Task 1: Extend PaymentChannel Model + ChannelUpdate Model + Tests

#### Step 1.1: Add new enums
- [ ] **File**: `sthrip/db/enums.py`
- [ ] **Action**: Add `settled` value to `ChannelStatus`:
  ```python
  class ChannelStatus(str, _PyEnum):
      PENDING = "pending"
      OPEN = "open"
      CLOSING = "closing"
      SETTLED = "settled"  # new
      CLOSED = "closed"
      DISPUTED = "disputed"
  ```
- [ ] **Action**: Add `RecurringInterval` enum:
  ```python
  class RecurringInterval(str, _PyEnum):
      HOURLY = "hourly"
      DAILY = "daily"
      WEEKLY = "weekly"
      MONTHLY = "monthly"
  ```
- [ ] **Action**: Add `StreamStatus` enum:
  ```python
  class StreamStatus(str, _PyEnum):
      ACTIVE = "active"
      PAUSED = "paused"
      STOPPED = "stopped"
  ```
- [ ] **Action**: Add all to `__all__`

#### Step 1.2: Extend PaymentChannel model
- [ ] **File**: `sthrip/db/models.py`
- [ ] **Action**: Add new columns to `PaymentChannel`:
  ```python
  deposit_a = Column(Numeric(20, 8), default=Decimal('0'))
  deposit_b = Column(Numeric(20, 8), default=Decimal('0'))
  balance_a = Column(Numeric(20, 8), default=Decimal('0'))
  balance_b = Column(Numeric(20, 8), default=Decimal('0'))
  nonce = Column(Integer, default=0)
  last_update_sig_a = Column(Text, nullable=True)
  last_update_sig_b = Column(Text, nullable=True)
  settlement_period = Column(Integer, default=3600)  # seconds, default 1 hour
  settled_at = Column(DateTime(timezone=True), nullable=True)
  ```
- [ ] **Action**: Add `ChannelUpdate` model:
  ```python
  class ChannelUpdate(Base):
      __tablename__ = "channel_updates"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      channel_id = Column(UUID(as_uuid=True), ForeignKey("payment_channels.id", ondelete="CASCADE"), nullable=False, index=True)
      nonce = Column(Integer, nullable=False)
      balance_a = Column(Numeric(20, 8), nullable=False)
      balance_b = Column(Numeric(20, 8), nullable=False)
      signature_a = Column(Text, nullable=True)
      signature_b = Column(Text, nullable=True)
      created_at = Column(DateTime(timezone=True), default=func.now())
      channel = relationship("PaymentChannel")
      __table_args__ = (
          UniqueConstraint("channel_id", "nonce", name="uq_channel_update_nonce"),
      )
  ```
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -c "from sthrip.db.models import ChannelUpdate; print('OK')"`
- [ ] **Expected**: Prints `OK`

#### Step 1.3: Write channel repo tests (RED)
- [ ] **File**: `tests/test_channel_service.py`
- [ ] **Action**: Write `TestChannelRepoExtended`:
  - `test_open_channel_with_deposits` -- creates channel with deposit_a, deposit_b
  - `test_submit_state_update` -- stores ChannelUpdate record
  - `test_get_latest_update` -- returns highest nonce update
  - `test_initiate_settlement` -- transitions to `closing`, sets `closes_at`
  - `test_settle_channel` -- transitions to `settled`
  - `test_dispute_channel` -- transitions to `disputed` during settlement
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_channel_service.py::TestChannelRepoExtended -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 1.4: Extend channel repo (GREEN)
- [ ] **File**: `sthrip/db/channel_repo.py`
- [ ] **Action**: Add new methods:
  - `open_with_deposit(channel_hash, agent_a_id, agent_b_id, deposit_a, deposit_b, settlement_period) -> PaymentChannel`:
    - Sets `balance_a = deposit_a`, `balance_b = deposit_b`, `capacity = deposit_a + deposit_b`, `status = OPEN`, `nonce = 0`
  - `submit_update(channel_id, nonce, balance_a, balance_b, signature_a, signature_b) -> ChannelUpdate`
  - `get_latest_update(channel_id) -> Optional[ChannelUpdate]`
  - `initiate_settlement(channel_id, nonce, balance_a, balance_b, sig_a, sig_b) -> int`:
    - Transitions `open` -> `closing`, sets `closes_at = now + settlement_period`
    - Updates `balance_a`, `balance_b`, `nonce` on channel
  - `settle(channel_id) -> int` (closing -> settled)
  - `finalize_close(channel_id) -> int` (settled -> closed, sets `closed_at`)
  - `dispute(channel_id, nonce, balance_a, balance_b, sig_a, sig_b) -> int`:
    - Only during `closing` state, requires higher nonce
  - `get_channels_ready_to_settle() -> List[PaymentChannel]`:
    - `closing` channels where `closes_at <= now`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_channel_service.py::TestChannelRepoExtended -x -v`
- [ ] **Expected**: All tests pass

---

### Task 2: Channel Service (Open/Close/Settle/Dispute) + Tests

#### Step 2.1: Write channel service tests (RED)
- [ ] **File**: `tests/test_channel_service.py` (append)
- [ ] **Action**: Add `TestChannelService`:
  - `test_open_channel` -- deducts from agent A balance, creates channel
  - `test_open_channel_insufficient_balance` -- raises ValueError
  - `test_open_channel_self` -- raises ValueError
  - `test_settle_valid_signatures` -- verifies signatures, applies net fee
  - `test_settle_invalid_signature` -- raises ValueError
  - `test_settle_fee_calculation` -- 1% on net transfer only
  - `test_close_after_settlement` -- credits balances back
  - `test_dispute_higher_nonce` -- replaces state during settlement
  - `test_dispute_lower_nonce_rejected` -- raises ValueError
  - `test_unilateral_close_timeout` -- after settlement period, auto-close
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_channel_service.py::TestChannelService -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 2.2: Implement channel service (GREEN)
- [ ] **File**: `sthrip/services/channel_service.py`
- [ ] **Action**: Create `ChannelService`:
  - `open_channel(db, agent_a_id, agent_b_id, deposit_a, deposit_b=0, settlement_period=3600) -> dict`:
    1. Validate agents exist and differ
    2. Deduct `deposit_a` from agent A balance
    3. Deduct `deposit_b` from agent B balance (if any)
    4. Create channel via repo
    5. Log audit event
    6. Return channel dict
  - `submit_update(db, channel_id, agent_id, nonce, balance_a, balance_b, signature_a, signature_b) -> dict`:
    1. Verify channel exists and agent is participant
    2. Verify nonce > channel.nonce
    3. Verify `balance_a + balance_b == deposit_a + deposit_b` (conservation)
    4. Verify signatures (both present)
    5. Store update, update channel state
  - `settle(db, channel_id, agent_id, nonce, balance_a, balance_b, sig_a, sig_b) -> dict`:
    1. Verify signatures
    2. Calculate net transfer: `net_a = balance_a - deposit_a`
    3. Fee = `abs(net_transfer) * 0.01` (1% on net)
    4. Initiate settlement (closing state)
    5. Return settlement preview
  - `close(db, channel_id, agent_id) -> dict`:
    1. Verify channel is `settled` or `closing` past settlement period
    2. Credit balances back to agents
    3. Record fee collection
    4. Finalize close
  - `dispute(db, channel_id, agent_id, nonce, balance_a, balance_b, sig_a, sig_b) -> dict`:
    1. Verify channel is `closing`
    2. Verify nonce > channel.nonce
    3. Verify signatures
    4. Update channel state with new balances
  - `auto_settle_expired(db) -> int`:
    1. Find closing channels past settlement period
    2. Auto-close each
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_channel_service.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 3: Channel API Endpoints + Tests

#### Step 3.1: Create channel schemas
- [ ] **File**: `api/schemas_channels.py`
- [ ] **Action**: Create Pydantic models:
  - `ChannelOpenRequest` -- counterparty_agent_name (str), deposit (Decimal, gt 0, le 10000), settlement_period (int, 60-86400, default 3600)
  - `ChannelStateUpdateRequest` -- nonce (int, gt 0), balance_a (Decimal, ge 0), balance_b (Decimal, ge 0), signature_a (str), signature_b (str)
  - `ChannelSettleRequest` -- nonce (int), balance_a (Decimal), balance_b (Decimal), signature_a (str), signature_b (str)
  - `ChannelDisputeRequest` -- same as settle
  - `ChannelResponse` -- all channel fields
  - `ChannelListResponse` -- items list + pagination

#### Step 3.2: Write channel API tests (RED)
- [ ] **File**: `tests/test_channel_api.py`
- [ ] **Action**: Write integration tests:
  - `test_open_channel_201`
  - `test_open_channel_insufficient_balance`
  - `test_list_channels`
  - `test_get_channel`
  - `test_submit_update`
  - `test_settle_channel`
  - `test_close_channel`
  - `test_dispute_channel`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_channel_api.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 3.3: Implement channel router (GREEN)
- [ ] **File**: `api/routers/channels.py`
- [ ] **Action**: Create router `APIRouter(prefix="/v2/channels", tags=["channels"])`:
  - `POST /` -- open channel
  - `GET /` -- list my channels
  - `GET /{id}` -- get channel state
  - `POST /{id}/update` -- submit signed state update (backup)
  - `POST /{id}/settle` -- initiate settlement
  - `POST /{id}/close` -- close channel
  - `POST /{id}/dispute` -- dispute during settlement
- [ ] **Action**: Register in `api/main_v2.py`
- [ ] **Action**: Update `tests/conftest.py`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_channel_api.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 4: Off-Chain State Signing (Ed25519) + Tests

#### Step 4.1: Write signing tests (RED)
- [ ] **File**: `tests/test_channel_signing.py`
- [ ] **Action**: Write tests:
  - `test_sign_state` -- signs a channel state, returns base64 signature
  - `test_verify_valid_signature` -- verifies with correct public key
  - `test_verify_invalid_signature` -- rejects tampered signature
  - `test_verify_wrong_key` -- rejects with wrong public key
  - `test_state_message_format` -- canonical message format: `channel_id:nonce:balance_a:balance_b`
  - `test_generate_keypair` -- generates Ed25519 keypair
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_channel_signing.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 4.2: Implement signing (GREEN)
- [ ] **File**: `sthrip/services/channel_signing.py`
- [ ] **Action**: Create signing utilities:
  - `generate_channel_keypair() -> tuple[str, str]`:
    - Returns (public_key_b64, private_key_b64)
    - Uses `nacl.signing.SigningKey.generate()`
  - `sign_channel_state(private_key_b64, channel_id, nonce, balance_a, balance_b) -> str`:
    - Canonical message: `f"{channel_id}:{nonce}:{balance_a}:{balance_b}"`
    - Signs with Ed25519
    - Returns base64 signature
  - `verify_channel_state(public_key_b64, signature_b64, channel_id, nonce, balance_a, balance_b) -> bool`:
    - Verifies Ed25519 signature
    - Returns True/False
- [ ] **Why**: Cryptographic integrity for off-chain state
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_channel_signing.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 5: Recurring Payments Model + Service + Cron + Tests

#### Step 5.1: Add RecurringPayment model
- [ ] **File**: `sthrip/db/models.py`
- [ ] **Action**: Add model:
  ```python
  class RecurringPayment(Base):
      __tablename__ = "recurring_payments"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      amount = Column(Numeric(20, 8), nullable=False)
      interval = Column(SQLEnum(RecurringInterval), nullable=False)
      next_payment_at = Column(DateTime(timezone=True), nullable=False)
      last_payment_at = Column(DateTime(timezone=True), nullable=True)
      total_paid = Column(Numeric(20, 8), default=Decimal('0'))
      max_payments = Column(Integer, nullable=True)
      payments_made = Column(Integer, default=0)
      is_active = Column(Boolean, default=True)
      created_at = Column(DateTime(timezone=True), default=func.now())
      cancelled_at = Column(DateTime(timezone=True), nullable=True)
      from_agent = relationship("Agent", foreign_keys=[from_agent_id])
      to_agent = relationship("Agent", foreign_keys=[to_agent_id])
      __table_args__ = (
          CheckConstraint("amount > 0", name="ck_recurring_amount_positive"),
      )
  ```

#### Step 5.2: Write recurring repo + service tests (RED)
- [ ] **File**: `tests/test_recurring.py`
- [ ] **Action**: Write tests:
  - `test_create_subscription`
  - `test_get_due_payments` -- returns subscriptions where `next_payment_at <= now`
  - `test_execute_payment` -- deducts from sender, credits receiver, updates totals
  - `test_execute_insufficient_balance` -- skips, marks for retry
  - `test_max_payments_reached` -- auto-cancels after max_payments
  - `test_cancel_subscription`
  - `test_spending_policy_respected` -- daily_limit blocks recurring payment
  - `test_interval_calculation` -- hourly/daily/weekly/monthly advance correctly
  - `test_api_create_subscription` -- POST `/v2/subscriptions`
  - `test_api_list_subscriptions` -- GET `/v2/subscriptions`
  - `test_api_cancel_subscription` -- DELETE `/v2/subscriptions/{id}`
  - `test_api_update_subscription` -- PATCH `/v2/subscriptions/{id}`

#### Step 5.3: Implement recurring repo (GREEN)
- [ ] **File**: `sthrip/db/recurring_repo.py`
- [ ] **Action**: Create `RecurringPaymentRepository`:
  - `create(from_agent_id, to_agent_id, amount, interval, max_payments, next_payment_at) -> RecurringPayment`
  - `get_by_id(payment_id) -> Optional[RecurringPayment]`
  - `get_due_payments() -> List[RecurringPayment]` (next_payment_at <= now, is_active)
  - `list_by_agent(agent_id, limit, offset) -> Tuple[List, int]`
  - `record_payment(payment_id, next_payment_at) -> int` (updates totals)
  - `cancel(payment_id) -> int`
  - `update(payment_id, amount, interval) -> int`

#### Step 5.4: Implement recurring service
- [ ] **File**: `sthrip/services/recurring_service.py`
- [ ] **Action**: Create `RecurringService`:
  - `create_subscription(db, from_agent_id, to_agent_id, amount, interval, max_payments) -> dict`
  - `execute_due_payments(db) -> int`:
    1. Get due payments
    2. For each: check balance, check spending policy, execute transfer via hub route
    3. Calculate next_payment_at based on interval
    4. If max_payments reached: cancel
    5. Queue webhooks for both parties
    6. Return count executed
  - `cancel_subscription(db, payment_id, agent_id) -> dict`
  - `update_subscription(db, payment_id, agent_id, amount, interval) -> dict`
  - `_calculate_next_payment(interval, from_time) -> datetime`

#### Step 5.5: Implement subscription router + cron
- [ ] **File**: `api/routers/subscriptions.py`
- [ ] **Action**: Create router with CRUD endpoints
- [ ] **File**: `api/main_v2.py`
- [ ] **Action**: Add `_recurring_payment_loop()` (every 5 minutes, like escrow resolution)
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_recurring.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 6: Recurring Payments API + Tests

(Covered in Task 5 steps -- router + API tests are included above)

---

### Task 7: Payment Streams Model + Service + Tests

#### Step 7.1: Add PaymentStream model
- [ ] **File**: `sthrip/db/models.py`
- [ ] **Action**: Add model:
  ```python
  class PaymentStream(Base):
      __tablename__ = "payment_streams"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      channel_id = Column(UUID(as_uuid=True), ForeignKey("payment_channels.id", ondelete="CASCADE"), nullable=False, index=True)
      from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
      to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
      rate_per_second = Column(Numeric(20, 12), nullable=False)
      started_at = Column(DateTime(timezone=True), default=func.now())
      paused_at = Column(DateTime(timezone=True), nullable=True)
      stopped_at = Column(DateTime(timezone=True), nullable=True)
      total_streamed = Column(Numeric(20, 8), default=Decimal('0'))
      state = Column(SQLEnum(StreamStatus), default=StreamStatus.ACTIVE)
      channel = relationship("PaymentChannel")
      __table_args__ = (
          CheckConstraint("rate_per_second > 0", name="ck_stream_rate_positive"),
      )
  ```

#### Step 7.2: Write stream tests (RED)
- [ ] **File**: `tests/test_streams.py`
- [ ] **Action**: Write tests:
  - `test_start_stream` -- creates stream on open channel
  - `test_start_stream_no_channel` -- raises ValueError
  - `test_accrue_calculation` -- rate * elapsed_seconds
  - `test_pause_stream` -- sets paused_at, stops accrual
  - `test_resume_stream` -- clears paused_at
  - `test_stop_stream` -- sets stopped_at, final accrual
  - `test_rate_exceeds_balance` -- rejects rate that would drain channel in < MIN_STREAM_DURATION
  - `test_api_start_stream` -- POST `/v2/streams`
  - `test_api_get_stream` -- GET `/v2/streams/{id}`
  - `test_api_pause_stream` -- POST `/v2/streams/{id}/pause`
  - `test_api_stop_stream` -- POST `/v2/streams/{id}/stop`

#### Step 7.3: Implement stream repo + service (GREEN)
- [ ] **File**: `sthrip/db/stream_repo.py`
- [ ] **File**: `sthrip/services/stream_service.py`
- [ ] **Action**: Implement CRUD + lifecycle:
  - `start_stream(db, channel_id, from_agent_id, rate_per_second) -> dict`
  - `get_accrued(db, stream_id) -> dict` (calculates elapsed * rate)
  - `pause_stream(db, stream_id, agent_id) -> dict`
  - `resume_stream(db, stream_id, agent_id) -> dict`
  - `stop_stream(db, stream_id, agent_id) -> dict`
- [ ] **Constraint**: `rate_per_second * MIN_STREAM_DURATION(60s) <= channel.balance_a`

---

### Task 8: Payment Streams API + Tests

#### Step 8.1: Implement stream router
- [ ] **File**: `api/routers/streams.py`
- [ ] **Action**: Create router `APIRouter(prefix="/v2/streams", tags=["streams"])`:
  - `POST /` -- start stream (requires channel_id)
  - `GET /{id}` -- get stream state + accrued amount
  - `POST /{id}/pause` -- pause stream
  - `POST /{id}/resume` -- resume stream
  - `POST /{id}/stop` -- stop stream
- [ ] **Action**: Register in `api/main_v2.py`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_streams.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 9: SDK Methods + Tests

#### Step 9.1: Write SDK tests (RED)
- [ ] **File**: `tests/test_sdk_payment_scaling.py`
- [ ] **Action**: Write tests:
  - `test_channel_open` -- POST `/v2/channels`
  - `test_channel_settle` -- POST `/v2/channels/{id}/settle`
  - `test_channel_close` -- POST `/v2/channels/{id}/close`
  - `test_subscribe` -- POST `/v2/subscriptions`
  - `test_unsubscribe` -- DELETE `/v2/subscriptions/{id}`
  - `test_subscriptions_list` -- GET `/v2/subscriptions`
  - `test_stream_start` -- POST `/v2/streams`
  - `test_stream_stop` -- POST `/v2/streams/{id}/stop`

#### Step 9.2: Implement SDK methods (GREEN)
- [ ] **File**: `sdk/sthrip/client.py`
- [ ] **Action**: Add methods:
  ```python
  def channel_open(self, agent_name, deposit, settlement_period=3600):
      payload = {"counterparty_agent_name": agent_name, "deposit": str(deposit), "settlement_period": settlement_period}
      return self._raw_post("/v2/channels", json_body=payload)

  def channel_settle(self, channel_id, nonce, balance_a, balance_b, signature_a, signature_b):
      payload = {"nonce": nonce, "balance_a": str(balance_a), "balance_b": str(balance_b), "signature_a": signature_a, "signature_b": signature_b}
      return self._raw_post("/v2/channels/{}/settle".format(channel_id), json_body=payload)

  def channel_close(self, channel_id):
      return self._raw_post("/v2/channels/{}/close".format(channel_id))

  def channels(self):
      return self._raw_get("/v2/channels")

  def subscribe(self, to_agent, amount, interval, max_payments=None):
      payload = {"to_agent_name": to_agent, "amount": str(amount), "interval": interval}
      if max_payments: payload["max_payments"] = max_payments
      return self._raw_post("/v2/subscriptions", json_body=payload)

  def unsubscribe(self, subscription_id):
      return self._raw_request("DELETE", "/v2/subscriptions/{}".format(subscription_id))

  def subscriptions(self):
      return self._raw_get("/v2/subscriptions")

  def stream_start(self, channel_id, rate_per_second):
      payload = {"channel_id": channel_id, "rate_per_second": str(rate_per_second)}
      return self._raw_post("/v2/streams", json_body=payload)

  def stream_stop(self, stream_id):
      return self._raw_post("/v2/streams/{}/stop".format(stream_id))
  ```
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sdk_payment_scaling.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 10: Alembic Migration

#### Step 10.1: Create migration
- [ ] **File**: `migrations/versions/h9i0j1k2l3m4_payment_scaling.py`
- [ ] **Action**: Migration for:
  - Add new columns to `payment_channels` (deposit_a, deposit_b, balance_a, balance_b, nonce, last_update_sig_a, last_update_sig_b, settlement_period, settled_at) -- `ALTER TABLE ADD COLUMN IF NOT EXISTS`
  - Create `channel_updates` table
  - Add `settled` to `channelstatus` enum (PostgreSQL)
  - Create `recurring_payments` table with `recurringinterval` enum
  - Create `payment_streams` table with `streamstatus` enum
- [ ] **Why**: Idempotent, backward-compatible

---

## Testing Strategy

- **Unit tests**: Channel signing, fee calculations, interval calculations, scoring
- **Integration tests**: Full channel lifecycle, subscription CRUD + execution, stream lifecycle
- **SDK tests**: Method signatures, request formatting
- **Target coverage**: 85%+ on new code

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Channel balance conservation violated | Critical | Assert `balance_a + balance_b == deposit_a + deposit_b` on every state update |
| Signature verification bypass | Critical | Reject any settle/dispute without both valid Ed25519 signatures |
| Recurring payment double-execution | High | Row-level lock on RecurringPayment, update next_payment_at atomically |
| Stream accrual exceeds channel balance | Medium | Check `rate * MIN_DURATION <= balance` on start, pause if would exceed |
| Settlement period race condition | Medium | Status-guarded UPDATEs, nonce monotonicity check |

## Success Criteria

- [ ] Channel open/settle/close lifecycle works end-to-end
- [ ] Ed25519 signatures verified on all state transitions
- [ ] Fee is 1% on NET transfer, not gross
- [ ] Dispute replaces state with higher-nonce during settlement
- [ ] Recurring payments execute on schedule respecting spending policies
- [ ] Subscriptions auto-cancel after max_payments reached
- [ ] Streams accrue at configured rate, pause/resume works
- [ ] SDK v0.4.0 has all new methods
- [ ] All tests pass (80%+ coverage)
