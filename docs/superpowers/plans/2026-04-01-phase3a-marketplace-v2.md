# Implementation Plan: Phase 3a -- Marketplace v2 (SLA, Reviews, Discovery, Matchmaking)

## Goal

Build the full Marketplace v2 feature set (Section C from the Phase 3 design spec): SLA templates and contracts with automatic enforcement, agent reviews with ZK proofs, enhanced discovery API, and automatic matchmaking. This drives adoption by giving agents structured service agreements, trust signals, and automated provider selection.

## Architecture

- **Enums**: `SLAStatus`, `MatchRequestStatus` added to `sthrip/db/enums.py`
- **Models**: `SLATemplate`, `SLAContract`, `AgentReview`, `AgentRatingSummary`, `MatchRequest` in `sthrip/db/models.py`
- **Repos**: `sla_repo.py`, `review_repo.py`, `matchmaking_repo.py` in `sthrip/db/`
- **Services**: `sla_service.py`, `review_service.py`, `matchmaking_service.py` in `sthrip/services/`
- **Router**: `sla.py`, `reviews.py`, `matchmaking.py` in `api/routers/`
- **Schemas**: SLA, review, and matchmaking Pydantic models in `api/schemas.py`
- **Migration**: `migrations/versions/g8h9i0j1k2l3_marketplace_v2.py`
- **Background task**: SLA enforcement cron (30-second interval) in `api/main_v2.py`
- **SDK**: New methods on `Sthrip` class in `sdk/sthrip/client.py`
- **Tests**: `tests/test_sla.py`, `tests/test_reviews.py`, `tests/test_matchmaking.py`, `tests/test_discovery_v2.py`

## Tech Stack

- Python 3.9, FastAPI, SQLAlchemy 2.0 (ORM), PostgreSQL (prod), SQLite (tests)
- Pydantic v2 for request/response validation
- pytest for TDD
- Existing ZK reputation service pattern (`sthrip/services/zk_reputation_service.py`) extended for review proofs

## File Map

### New Files

| File | Responsibility |
|------|---------------|
| `sthrip/db/sla_repo.py` | SLATemplate + SLAContract CRUD, state transitions |
| `sthrip/db/review_repo.py` | AgentReview CRUD, rating summary upserts |
| `sthrip/db/matchmaking_repo.py` | MatchRequest CRUD |
| `sthrip/services/sla_service.py` | SLA business logic, escrow integration, auto-enforcement |
| `sthrip/services/review_service.py` | Review creation, transaction verification, ZK proofs |
| `sthrip/services/matchmaking_service.py` | Scoring algorithm, match execution |
| `api/routers/sla.py` | SLA template + contract endpoints |
| `api/routers/reviews.py` | Review + rating endpoints |
| `api/routers/matchmaking.py` | Matchmaking endpoints |
| `api/schemas_sla.py` | Pydantic models for SLA (keeps schemas.py from growing too large) |
| `api/schemas_reviews.py` | Pydantic models for reviews |
| `api/schemas_matchmaking.py` | Pydantic models for matchmaking |
| `migrations/versions/g8h9i0j1k2l3_marketplace_v2.py` | Alembic migration for all Phase 3a tables |
| `tests/test_sla_repo.py` | Unit tests for SLA repository |
| `tests/test_sla_service.py` | Unit tests for SLA service |
| `tests/test_sla_api.py` | Integration tests for SLA endpoints |
| `tests/test_review_repo.py` | Unit tests for review repository |
| `tests/test_review_api.py` | Integration tests for review endpoints |
| `tests/test_matchmaking.py` | Unit + integration tests for matchmaking |
| `tests/test_discovery_v2.py` | Integration tests for enhanced discovery |

### Modified Files

| File | Changes |
|------|---------|
| `sthrip/db/enums.py` | Add `SLAStatus`, `MatchRequestStatus` enums |
| `sthrip/db/models.py` | Add 5 new model classes, update Agent relationships |
| `sthrip/db/repository.py` | Re-export new repos |
| `api/main_v2.py` | Register 3 new routers, add SLA enforcement cron task |
| `api/routers/agents.py` | Extend marketplace endpoint with new query params |
| `sdk/sthrip/client.py` | Add `sla_*`, `review_*`, `matchmake` methods |
| `tests/conftest.py` | Add new tables to `_COMMON_TEST_TABLES`, new router modules to `_GET_DB_MODULES` |

---

## Implementation Steps

### Task 1: SLA Templates Model + Repo + Tests

#### Step 1.1: Add SLA enums
- [ ] **File**: `sthrip/db/enums.py`
- [ ] **Action**: Add `SLAStatus` enum with values: `proposed`, `accepted`, `active`, `delivered`, `completed`, `breached`, `disputed`
- [ ] **Action**: Add `MatchRequestStatus` enum with values: `searching`, `matched`, `assigned`, `expired`
- [ ] **Action**: Add both to `__all__` list
- [ ] **Why**: All state machine values must be defined before models reference them
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -c "from sthrip.db.enums import SLAStatus, MatchRequestStatus; print('OK')"`
- [ ] **Expected**: Prints `OK`

#### Step 1.2: Add SLATemplate and SLAContract models
- [ ] **File**: `sthrip/db/models.py`
- [ ] **Action**: Add import of `SLAStatus, MatchRequestStatus` from enums re-export block (line ~36)
- [ ] **Action**: Add `SLATemplate` model class:
  ```python
  class SLATemplate(Base):
      __tablename__ = "sla_templates"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      provider_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
      name = Column(String(255), nullable=False)
      service_description = Column(Text, nullable=False)
      deliverables = Column(JSON, default=list)
      response_time_secs = Column(Integer, nullable=False)
      delivery_time_secs = Column(Integer, nullable=False)
      base_price = Column(Numeric(20, 8), nullable=False)
      currency = Column(String(10), default="XMR")
      penalty_percent = Column(Integer, default=10)
      is_active = Column(Boolean, default=True)
      created_at = Column(DateTime(timezone=True), default=func.now())
      provider = relationship("Agent", backref="sla_templates")
  ```
- [ ] **Action**: Add `SLAContract` model class:
  ```python
  class SLAContract(Base):
      __tablename__ = "sla_contracts"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      provider_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      consumer_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      template_id = Column(UUID(as_uuid=True), ForeignKey("sla_templates.id"), nullable=True)
      service_description = Column(Text, nullable=False)
      deliverables = Column(JSON, default=list)
      response_time_secs = Column(Integer, nullable=False)
      delivery_time_secs = Column(Integer, nullable=False)
      price = Column(Numeric(20, 8), nullable=False)
      currency = Column(String(10), default="XMR")
      penalty_percent = Column(Integer, default=10)
      state = Column(SQLEnum(SLAStatus), default=SLAStatus.PROPOSED)
      escrow_deal_id = Column(UUID(as_uuid=True), ForeignKey("escrow_deals.id"), nullable=True)
      started_at = Column(DateTime(timezone=True), nullable=True)
      delivered_at = Column(DateTime(timezone=True), nullable=True)
      response_time_actual = Column(Integer, nullable=True)
      delivery_time_actual = Column(Integer, nullable=True)
      sla_met = Column(Boolean, nullable=True)
      result_hash = Column(String(128), nullable=True)
      created_at = Column(DateTime(timezone=True), default=func.now())
      provider = relationship("Agent", foreign_keys=[provider_id])
      consumer = relationship("Agent", foreign_keys=[consumer_id])
      template = relationship("SLATemplate")
      escrow_deal = relationship("EscrowDeal")
  ```
- [ ] **Why**: Models must exist before repos can reference them
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -c "from sthrip.db.models import SLATemplate, SLAContract; print('OK')"`
- [ ] **Expected**: Prints `OK`

#### Step 1.3: Write SLA template repo tests (RED)
- [ ] **File**: `tests/test_sla_repo.py`
- [ ] **Action**: Write test class `TestSLATemplateRepo` with tests:
  - `test_create_template` -- creates a template, asserts all fields stored, auto-generated UUID
  - `test_list_by_provider` -- creates 2 templates for provider A and 1 for provider B, asserts filtering
  - `test_get_by_id` -- creates template, retrieves by ID, asserts equal
  - `test_deactivate` -- creates template, deactivates, asserts `is_active=False`
- [ ] **Action**: Use the same fixture pattern as `tests/test_escrow.py`: in-memory SQLite engine with `StaticPool`, create tables including `SLATemplate.__table__`, `SLAContract.__table__`, `Agent.__table__`, etc.
- [ ] **Why**: TDD -- write tests first
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_repo.py -x -v 2>&1 | head -30`
- [ ] **Expected**: Tests fail with `ImportError` (repo does not exist yet)

#### Step 1.4: Implement SLA template repo (GREEN)
- [ ] **File**: `sthrip/db/sla_repo.py`
- [ ] **Action**: Create `SLATemplateRepository` class:
  - `__init__(self, db: Session)`
  - `create(provider_id, name, service_description, deliverables, response_time_secs, delivery_time_secs, base_price, currency, penalty_percent) -> SLATemplate` -- creates and flushes
  - `get_by_id(template_id) -> Optional[SLATemplate]`
  - `list_by_provider(provider_id, active_only=True, limit=50, offset=0) -> Tuple[List[SLATemplate], int]`
  - `deactivate(template_id) -> int` -- sets `is_active=False`, returns rows affected
- [ ] **Action**: Create `SLAContractRepository` class (stub for now, full implementation in Task 2):
  - `__init__(self, db: Session)`
- [ ] **Why**: Minimal implementation to pass template tests
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_repo.py -x -v`
- [ ] **Expected**: All 4 tests pass

#### Step 1.5: Register repo in repository facade
- [ ] **File**: `sthrip/db/repository.py`
- [ ] **Action**: Add imports:
  ```python
  from .sla_repo import SLATemplateRepository, SLAContractRepository
  ```
- [ ] **Action**: Add to `__all__`: `"SLATemplateRepository"`, `"SLAContractRepository"`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -c "from sthrip.db.repository import SLATemplateRepository; print('OK')"`
- [ ] **Expected**: Prints `OK`

---

### Task 2: SLA Contracts Model + Service + Tests

#### Step 2.1: Write SLA contract repo tests (RED)
- [ ] **File**: `tests/test_sla_repo.py` (append to existing)
- [ ] **Action**: Add `TestSLAContractRepo` class with tests:
  - `test_create_contract` -- creates contract, asserts state=`proposed`
  - `test_create_from_template` -- creates template first, then contract referencing it
  - `test_accept` -- transitions `proposed` -> `accepted`, asserts state and rowcount
  - `test_activate` -- transitions `accepted` -> `active`, sets `started_at`
  - `test_deliver` -- transitions `active` -> `delivered`, records delivery time
  - `test_complete` -- transitions `delivered` -> `completed`, sets `sla_met`
  - `test_breach` -- transitions `active` -> `breached`
  - `test_list_by_agent` -- lists contracts where agent is provider or consumer
  - `test_get_active_past_deadline` -- returns contracts needing auto-enforcement
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_repo.py::TestSLAContractRepo -x -v 2>&1 | head -30`
- [ ] **Expected**: Tests fail (contract methods not implemented)

#### Step 2.2: Implement SLA contract repo (GREEN)
- [ ] **File**: `sthrip/db/sla_repo.py`
- [ ] **Action**: Flesh out `SLAContractRepository` with methods:
  - `create(provider_id, consumer_id, template_id, service_description, deliverables, response_time_secs, delivery_time_secs, price, currency, penalty_percent, escrow_deal_id) -> SLAContract`
  - `get_by_id(contract_id) -> Optional[SLAContract]`
  - `get_by_id_for_update(contract_id) -> Optional[SLAContract]` (row lock, SQLite fallback)
  - `accept(contract_id) -> int` (proposed -> accepted)
  - `activate(contract_id) -> int` (accepted -> active, sets `started_at`)
  - `deliver(contract_id, result_hash) -> int` (active -> delivered, records actual times)
  - `complete(contract_id, sla_met) -> int` (delivered -> completed)
  - `breach(contract_id) -> int` (active -> breached)
  - `dispute(contract_id) -> int` (active/delivered -> disputed)
  - `list_by_agent(agent_id, role, state, limit, offset) -> Tuple[List, int]`
  - `get_active_past_deadline() -> List[SLAContract]` -- returns active contracts past `response_time_secs` or `delivery_time_secs`
- [ ] **Why**: Full contract lifecycle support
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_repo.py -x -v`
- [ ] **Expected**: All tests pass

#### Step 2.3: Write SLA service tests (RED)
- [ ] **File**: `tests/test_sla_service.py`
- [ ] **Action**: Write test class `TestSLAService`:
  - `test_create_contract_creates_escrow` -- verifies escrow deal is auto-created for the contract price
  - `test_create_contract_insufficient_balance` -- raises ValueError when consumer has no balance
  - `test_accept_contract` -- provider accepts, transitions to active
  - `test_accept_wrong_agent` -- raises PermissionError
  - `test_deliver_contract` -- provider delivers with result hash
  - `test_verify_contract_sla_met` -- consumer verifies, SLA within time limits
  - `test_verify_contract_sla_breached` -- consumer verifies but delivery was late, penalty applied
  - `test_breach_auto_detected` -- SLA enforcement finds overdue contract
- [ ] **Action**: Mock `BalanceRepository` and `EscrowService` as in escrow service tests
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_service.py -x -v 2>&1 | head -30`
- [ ] **Expected**: Tests fail (service does not exist)

#### Step 2.4: Implement SLA service (GREEN)
- [ ] **File**: `sthrip/services/sla_service.py`
- [ ] **Action**: Create `SLAService` class:
  - `create_template(db, provider_id, ...) -> dict` -- delegates to template repo
  - `create_contract(db, consumer_id, provider_id, ...) -> dict`:
    1. Validate consumer != provider
    2. Create escrow deal via `EscrowService.create_escrow()` (price + penalty deposit)
    3. Create SLA contract with `escrow_deal_id`
    4. Return contract dict
  - `accept_contract(db, contract_id, provider_id) -> dict`:
    1. Verify agent is the provider
    2. Accept escrow deal
    3. Activate SLA contract (starts clock)
    4. Return dict with `started_at`
  - `deliver_contract(db, contract_id, provider_id, result_hash) -> dict`:
    1. Verify agent is provider
    2. Deliver escrow deal
    3. Mark SLA contract as delivered
    4. Calculate `delivery_time_actual`
  - `verify_contract(db, contract_id, consumer_id) -> dict`:
    1. Verify agent is consumer
    2. Check if SLA was met (delivery within time limits)
    3. If met: release full escrow to provider
    4. If breached: deduct penalty from escrow, release remainder
    5. Complete SLA contract
  - `enforce_sla(db) -> int`:
    1. Query active contracts past deadline
    2. For each: auto-breach, apply penalty via escrow
    3. Return count resolved
- [ ] **Why**: Core business logic linking SLA to escrow
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_service.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 3: SLA API Endpoints + Escrow Integration + Tests

#### Step 3.1: Create SLA schemas
- [ ] **File**: `api/schemas_sla.py`
- [ ] **Action**: Create Pydantic models:
  - `SLATemplateCreateRequest` -- name (str, max 255), service_description (str, max 2000), deliverables (List[dict], max 10), response_time_secs (int, 1-86400), delivery_time_secs (int, 1-604800), base_price (Decimal, gt 0, le 10000), currency (str, default "XMR"), penalty_percent (int, 0-50, default 10)
  - `SLATemplateResponse` -- all fields + id, provider_id, is_active, created_at (as str)
  - `SLAContractCreateRequest` -- provider_agent_name (str), template_id (Optional[UUID]), service_description (Optional[str]), deliverables (Optional[List[dict]]), response_time_secs (Optional[int]), delivery_time_secs (Optional[int]), price (Decimal), currency (str, default "XMR"), penalty_percent (Optional[int])
  - `SLAContractResponse` -- all fields + id, state, escrow_deal_id, started_at, delivered_at, sla_met
  - `SLADeliverRequest` -- result_hash (str, max 128)
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -c "from api.schemas_sla import SLATemplateCreateRequest; print('OK')"`
- [ ] **Expected**: Prints `OK`

#### Step 3.2: Write SLA API tests (RED)
- [ ] **File**: `tests/test_sla_api.py`
- [ ] **Action**: Write integration tests using `client` fixture pattern from conftest.py:
  - `test_create_template_201` -- POST `/v2/sla/templates`, verify 201 + response fields
  - `test_create_template_validation` -- invalid fields return 422
  - `test_list_templates` -- GET `/v2/sla/templates`, verify pagination
  - `test_create_contract_201` -- POST `/v2/sla/contracts` with template_id
  - `test_create_contract_custom` -- POST `/v2/sla/contracts` without template
  - `test_accept_contract` -- PATCH `/v2/sla/contracts/{id}/accept`
  - `test_deliver_contract` -- PATCH `/v2/sla/contracts/{id}/deliver`
  - `test_verify_contract` -- PATCH `/v2/sla/contracts/{id}/verify`
  - `test_dispute_contract` -- POST `/v2/sla/contracts/{id}/dispute`
  - `test_list_contracts` -- GET `/v2/sla/contracts` with filters
- [ ] **Action**: Use same patching pattern as `tests/test_escrow.py` but add `"api.routers.sla"` to `_GET_DB_MODULES`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_api.py -x -v 2>&1 | head -30`
- [ ] **Expected**: Tests fail (router does not exist)

#### Step 3.3: Implement SLA router (GREEN)
- [ ] **File**: `api/routers/sla.py`
- [ ] **Action**: Create router `APIRouter(prefix="/v2/sla", tags=["sla"])` with endpoints:
  - `POST /templates` -- create SLA template (auth required, provider = current agent)
  - `GET /templates` -- list templates for current agent (or all active if `public=true` query param)
  - `GET /templates/{id}` -- get template details
  - `POST /contracts` -- create SLA contract (auth required, consumer = current agent)
  - `GET /contracts` -- list contracts for current agent
  - `GET /contracts/{id}` -- get contract details
  - `PATCH /contracts/{id}/accept` -- provider accepts
  - `PATCH /contracts/{id}/deliver` -- provider delivers
  - `PATCH /contracts/{id}/verify` -- consumer verifies
  - `POST /contracts/{id}/dispute` -- either party disputes
- [ ] **Action**: Follow escrow router pattern: `_handle_service_error`, `BackgroundTasks` for webhooks
- [ ] **Why**: REST API for SLA lifecycle

#### Step 3.4: Register SLA router in app
- [ ] **File**: `api/main_v2.py`
- [ ] **Action**: Add import: `from api.routers import sla` (line ~41)
- [ ] **Action**: Add: `application.include_router(sla.router)` (after escrow router, line ~437)
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_api.py -x -v`
- [ ] **Expected**: All API tests pass

#### Step 3.5: Update conftest.py
- [ ] **File**: `tests/conftest.py`
- [ ] **Action**: Add `SLATemplate, SLAContract` to imports from `sthrip.db.models`
- [ ] **Action**: Add `SLATemplate.__table__`, `SLAContract.__table__` to `_COMMON_TEST_TABLES`
- [ ] **Action**: Add `"api.routers.sla"` to `_GET_DB_MODULES`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_api.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 4: SLA Auto-Enforcement Cron + Tests

#### Step 4.1: Write enforcement tests (RED)
- [ ] **File**: `tests/test_sla_service.py` (append)
- [ ] **Action**: Add `TestSLAEnforcement` class:
  - `test_enforce_response_timeout` -- active contract, `started_at` + `response_time_secs` has passed, no delivery -> auto-breach
  - `test_enforce_delivery_timeout` -- active contract, `started_at` + `delivery_time_secs` has passed -> auto-breach, penalty refunded
  - `test_enforce_no_false_positives` -- active contract within deadlines is not breached
  - `test_enforce_penalty_applied` -- verifies penalty amount = `price * penalty_percent / 100`
- [ ] **Action**: Use `_naive_utc_now()` pattern for SQLite datetime compat (set `started_at` in the past)
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_service.py::TestSLAEnforcement -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 4.2: Implement enforcement logic (GREEN)
- [ ] **File**: `sthrip/services/sla_service.py`
- [ ] **Action**: Implement `enforce_sla(db) -> int`:
  1. `repo.get_active_past_deadline()` returns contracts where `now - started_at > response_time_secs` or `now - started_at > delivery_time_secs`
  2. For each: calculate penalty = `price * penalty_percent / 100`
  3. Release escrow with reduced amount (full price minus penalty back to consumer)
  4. Mark contract as `breached`
  5. Update trust score via `ReputationRepository.record_transaction(success=False)`
  6. Queue webhook: `sla.breached`
  7. Return count
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_service.py -x -v`
- [ ] **Expected**: All tests pass

#### Step 4.3: Add SLA enforcement background task
- [ ] **File**: `api/main_v2.py`
- [ ] **Action**: Add `_sla_enforcement_loop()` async function (modeled after `_escrow_resolution_loop`):
  ```python
  async def _sla_enforcement_loop():
      from sthrip.services.sla_service import SLAService
      svc = SLAService()
      while True:
          try:
              await asyncio.sleep(30)  # 30 seconds per spec
              with get_db() as db:
                  resolved = svc.enforce_sla(db)
                  if resolved > 0:
                      logger.info("SLA auto-enforcement: resolved %d contracts", resolved)
          except asyncio.CancelledError:
              break
          except Exception:
              logger.exception("SLA auto-enforcement error")
  ```
- [ ] **Action**: Start task in `_startup_services()`, add to returned dict
- [ ] **Action**: Cancel task in `_shutdown_services()`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sla_service.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 5: Agent Reviews Model + Repo + Tests

#### Step 5.1: Add review models
- [ ] **File**: `sthrip/db/models.py`
- [ ] **Action**: Add `AgentReview` model:
  ```python
  class AgentReview(Base):
      __tablename__ = "agent_reviews"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      reviewer_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      reviewed_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      transaction_id = Column(UUID(as_uuid=True), nullable=False)
      transaction_type = Column(String(20), nullable=False)  # "payment", "escrow", "sla"
      overall_rating = Column(Integer, nullable=False)
      speed_rating = Column(Integer, nullable=True)
      quality_rating = Column(Integer, nullable=True)
      reliability_rating = Column(Integer, nullable=True)
      comment_encrypted = Column(Text, nullable=True)
      is_verified = Column(Boolean, default=True)
      created_at = Column(DateTime(timezone=True), default=func.now())
      reviewer = relationship("Agent", foreign_keys=[reviewer_id])
      reviewed = relationship("Agent", foreign_keys=[reviewed_id])
      __table_args__ = (
          UniqueConstraint("reviewer_id", "transaction_id", name="uq_review_per_transaction"),
          CheckConstraint("overall_rating >= 1 AND overall_rating <= 5", name="ck_overall_rating_range"),
          CheckConstraint("speed_rating IS NULL OR (speed_rating >= 1 AND speed_rating <= 5)", name="ck_speed_rating_range"),
          CheckConstraint("quality_rating IS NULL OR (quality_rating >= 1 AND quality_rating <= 5)", name="ck_quality_rating_range"),
          CheckConstraint("reliability_rating IS NULL OR (reliability_rating >= 1 AND reliability_rating <= 5)", name="ck_reliability_rating_range"),
      )
  ```
- [ ] **Action**: Add `AgentRatingSummary` model:
  ```python
  class AgentRatingSummary(Base):
      __tablename__ = "agent_rating_summary"
      agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True)
      total_reviews = Column(Integer, default=0)
      avg_overall = Column(Numeric(3, 2), default=Decimal('0'))
      avg_speed = Column(Numeric(3, 2), default=Decimal('0'))
      avg_quality = Column(Numeric(3, 2), default=Decimal('0'))
      avg_reliability = Column(Numeric(3, 2), default=Decimal('0'))
      five_star_count = Column(Integer, default=0)
      one_star_count = Column(Integer, default=0)
      last_review_at = Column(DateTime(timezone=True), nullable=True)
      updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
      agent = relationship("Agent", backref="rating_summary", uselist=False)
  ```
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -c "from sthrip.db.models import AgentReview, AgentRatingSummary; print('OK')"`
- [ ] **Expected**: Prints `OK`

#### Step 5.2: Write review repo tests (RED)
- [ ] **File**: `tests/test_review_repo.py`
- [ ] **Action**: Write `TestReviewRepository`:
  - `test_create_review` -- creates review, asserts all fields
  - `test_duplicate_review_rejected` -- same reviewer + transaction_id raises IntegrityError
  - `test_list_by_reviewed` -- lists reviews for a specific agent
  - `test_get_by_transaction` -- finds review by transaction ID
  - `test_update_rating_summary` -- after creating reviews, summary is recalculated
  - `test_get_rating_summary` -- retrieves summary for agent
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_review_repo.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail (repo does not exist)

#### Step 5.3: Implement review repo (GREEN)
- [ ] **File**: `sthrip/db/review_repo.py`
- [ ] **Action**: Create `ReviewRepository` class:
  - `create(reviewer_id, reviewed_id, transaction_id, transaction_type, overall_rating, speed_rating, quality_rating, reliability_rating, comment_encrypted) -> AgentReview`
  - `get_by_id(review_id) -> Optional[AgentReview]`
  - `get_by_transaction(reviewer_id, transaction_id) -> Optional[AgentReview]`
  - `list_by_reviewed(reviewed_id, limit=50, offset=0) -> Tuple[List[AgentReview], int]`
  - `update_rating_summary(reviewed_id) -> AgentRatingSummary`:
    1. Query all reviews for agent
    2. Calculate avg_overall, avg_speed, avg_quality, avg_reliability
    3. Count five_star, one_star
    4. Upsert AgentRatingSummary
  - `get_rating_summary(agent_id) -> Optional[AgentRatingSummary]`
- [ ] **Action**: Register in `sthrip/db/repository.py`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_review_repo.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 6: Reviews API + ZK Review Proofs + Tests

#### Step 6.1: Create review schemas
- [ ] **File**: `api/schemas_reviews.py`
- [ ] **Action**: Create Pydantic models:
  - `ReviewCreateRequest` -- transaction_id (str/UUID), transaction_type (str, pattern `^(payment|escrow|sla)$`), overall_rating (int, 1-5), speed_rating (Optional[int], 1-5), quality_rating (Optional[int], 1-5), reliability_rating (Optional[int], 1-5), comment (Optional[str], max 2000)
  - `ReviewResponse` -- all fields + id, reviewer_id, reviewed_id, is_verified, created_at
  - `RatingSummaryResponse` -- all summary fields
  - `ReviewProofRequest` -- min_reviews (int, 1-1000), min_avg (Decimal, 1.0-5.0)
  - `ReviewProofResponse` -- commitment (str), proof (str), min_reviews (int), min_avg (str)
  - `ReviewProofVerifyRequest` -- commitment, proof, min_reviews, min_avg

#### Step 6.2: Write review API tests (RED)
- [ ] **File**: `tests/test_review_api.py`
- [ ] **Action**: Write integration tests:
  - `test_create_review_201` -- POST `/v2/agents/{id}/reviews` with valid data
  - `test_create_review_no_transaction` -- 400 when transaction_id does not exist
  - `test_create_review_self_review` -- 400 when reviewing self
  - `test_duplicate_review_409` -- 409 when same transaction reviewed twice
  - `test_get_reviews` -- GET `/v2/agents/{id}/reviews`
  - `test_get_ratings` -- GET `/v2/agents/{id}/ratings`
  - `test_zk_review_proof` -- POST `/v2/me/review-proof`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_review_api.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 6.3: Implement review service
- [ ] **File**: `sthrip/services/review_service.py`
- [ ] **Action**: Create `ReviewService` class:
  - `create_review(db, reviewer_id, reviewed_id, transaction_id, transaction_type, ...) -> dict`:
    1. Verify transaction exists and involves both agents
    2. Verify reviewer != reviewed
    3. Create review via repo
    4. Update rating summary
    5. Update AgentReputation.average_rating and total_reviews
    6. Return review dict
  - `get_reviews(db, agent_id, limit, offset) -> dict`
  - `get_rating_summary(db, agent_id) -> dict`
  - `generate_review_proof(db, agent_id, min_reviews, min_avg) -> dict`:
    1. Get rating summary
    2. If total_reviews < min_reviews or avg_overall < min_avg: raise ValueError
    3. Extend existing ZK proof pattern: commit to (total_reviews, avg_overall)
    4. Prove total_reviews >= min_reviews AND avg_overall >= min_avg
  - `verify_review_proof(commitment, proof, min_reviews, min_avg) -> bool`

#### Step 6.4: Implement review router (GREEN)
- [ ] **File**: `api/routers/reviews.py`
- [ ] **Action**: Create router `APIRouter(prefix="/v2", tags=["reviews"])`:
  - `POST /agents/{agent_id}/reviews` -- leave review (auth required)
  - `GET /agents/{agent_id}/reviews` -- get reviews for agent (public)
  - `GET /agents/{agent_id}/ratings` -- get rating summary (public)
  - `POST /me/review-proof` -- generate ZK review proof (auth required)
  - `POST /review-proof/verify` -- verify ZK review proof (public)
- [ ] **Action**: Register in `api/main_v2.py`
- [ ] **Action**: Update `tests/conftest.py`: add tables and module to `_GET_DB_MODULES`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_review_api.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 7: Rating Summary (Materialized) + Tests

#### Step 7.1: Write rating summary tests (RED)
- [ ] **File**: `tests/test_review_repo.py` (append)
- [ ] **Action**: Add `TestRatingSummary`:
  - `test_summary_computed_on_review` -- after 3 reviews, summary has correct averages
  - `test_summary_five_star_count` -- verify five_star_count incremented
  - `test_summary_one_star_count` -- verify one_star_count incremented
  - `test_summary_partial_ratings` -- speed_rating null on some reviews, avg_speed only includes non-null
  - `test_summary_empty` -- agent with no reviews returns None or default
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_review_repo.py::TestRatingSummary -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 7.2: Implement (GREEN)
- [ ] **File**: `sthrip/db/review_repo.py`
- [ ] **Action**: Enhance `update_rating_summary` to handle partial ratings:
  - Count non-null speed/quality/reliability ratings separately
  - Compute averages only over non-null values
  - Upsert pattern: try insert, on IntegrityError update
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_review_repo.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 8: Discovery API v2 (Extend Marketplace Endpoint) + Tests

#### Step 8.1: Write discovery v2 tests (RED)
- [ ] **File**: `tests/test_discovery_v2.py`
- [ ] **Action**: Write integration tests:
  - `test_filter_min_rating` -- `?min_rating=4.0` filters agents with avg_overall < 4.0
  - `test_filter_min_reviews` -- `?min_reviews=10` filters agents with < 10 reviews
  - `test_filter_max_price` -- `?max_price=1.0` filters based on SLA template base_price
  - `test_filter_has_sla` -- `?has_sla=true` only returns agents with active SLA templates
  - `test_sort_by_rating` -- `?sort=rating` orders by avg_overall descending
  - `test_sort_by_price` -- `?sort=price` orders by min SLA template price ascending
  - `test_response_includes_rating` -- response includes `rating` object with overall, total_reviews, speed, quality
  - `test_response_includes_sla_templates` -- response includes `sla_templates` array
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_discovery_v2.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 8.2: Extend marketplace endpoint (GREEN)
- [ ] **File**: `api/routers/agents.py`
- [ ] **Action**: Extend `GET /v2/agents/marketplace` endpoint:
  - Add query parameters: `min_rating`, `min_reviews`, `max_price`, `currency`, `has_sla`, `accepts_channels`, `sort` (enum: `rating`, `price`, `reviews`, `trust_score`)
  - Join with `AgentRatingSummary` for rating filters and sort
  - Join with `SLATemplate` for price filters and `has_sla` filter
  - Enhance response to include `rating` dict and `sla_templates` list
- [ ] **Action**: Update `AgentMarketplaceResponse` in `api/schemas.py` to include:
  ```python
  rating: Optional[dict] = None
  sla_templates: List[dict] = Field(default_factory=list)
  accepts_channels: bool = False
  last_active: Optional[str] = None
  ```
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_discovery_v2.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 9: Matchmaking Service + API + Tests

#### Step 9.1: Add MatchRequest model
- [ ] **File**: `sthrip/db/models.py`
- [ ] **Action**: Add `MatchRequest` model:
  ```python
  class MatchRequest(Base):
      __tablename__ = "match_requests"
      id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
      requester_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
      task_description = Column(Text, nullable=False)
      required_capabilities = Column(JSON, default=list)
      budget = Column(Numeric(20, 8), nullable=False)
      currency = Column(String(10), default="XMR")
      deadline_secs = Column(Integer, nullable=False)
      min_rating = Column(Numeric(3, 2), default=Decimal('0'))
      auto_assign = Column(Boolean, default=False)
      matched_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=True)
      sla_contract_id = Column(UUID(as_uuid=True), ForeignKey("sla_contracts.id"), nullable=True)
      state = Column(SQLEnum(MatchRequestStatus), default=MatchRequestStatus.SEARCHING)
      created_at = Column(DateTime(timezone=True), default=func.now())
      expires_at = Column(DateTime(timezone=True), nullable=False)
      requester = relationship("Agent", foreign_keys=[requester_id])
      matched_agent = relationship("Agent", foreign_keys=[matched_agent_id])
  ```

#### Step 9.2: Write matchmaking tests (RED)
- [ ] **File**: `tests/test_matchmaking.py`
- [ ] **Action**: Write tests:
  - `test_score_calculation` -- unit test for scoring algorithm
  - `test_capability_filter` -- only agents with matching capabilities
  - `test_budget_filter` -- only agents within budget
  - `test_rating_weight` -- higher-rated agents score higher
  - `test_price_weight` -- cheaper agents score higher
  - `test_create_match_request` -- API test, POST `/v2/matchmaking/request`
  - `test_get_match_result` -- API test, GET `/v2/matchmaking/{id}`
  - `test_auto_assign` -- when `auto_assign=true`, SLA contract auto-created
  - `test_accept_match` -- API test, POST `/v2/matchmaking/{id}/accept`
  - `test_no_match_found` -- returns state=`searching` when no agents qualify
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_matchmaking.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 9.3: Implement matchmaking repo
- [ ] **File**: `sthrip/db/matchmaking_repo.py`
- [ ] **Action**: Create `MatchmakingRepository`:
  - `create(requester_id, task_description, required_capabilities, budget, currency, deadline_secs, min_rating, auto_assign, expires_at) -> MatchRequest`
  - `get_by_id(request_id) -> Optional[MatchRequest]`
  - `update_match(request_id, matched_agent_id, sla_contract_id, state) -> int`
  - `list_by_requester(requester_id, limit, offset) -> Tuple[List, int]`
  - `get_expired_searching() -> List[MatchRequest]`
- [ ] **Action**: Register in `sthrip/db/repository.py`

#### Step 9.4: Implement matchmaking service (GREEN)
- [ ] **File**: `sthrip/services/matchmaking_service.py`
- [ ] **Action**: Create `MatchmakingService`:
  - `create_request(db, requester_id, ...) -> dict`:
    1. Create MatchRequest record
    2. Run matching algorithm immediately
    3. If match found and auto_assign: create SLA contract via SLAService
    4. Return result
  - `_find_best_match(db, request) -> Optional[tuple[Agent, float]]`:
    1. Query agents with matching capabilities (JSONB `@>` on PG, Python filter on SQLite)
    2. Filter by budget (SLA template base_price <= budget)
    3. Filter by min_rating (from AgentRatingSummary)
    4. Score remaining candidates:
       - `rating_score = avg_overall / 5.0` (weight 0.4)
       - `price_score = 1.0 - (base_price / budget)` (weight 0.3)
       - `speed_score = 1.0 - (delivery_time_secs / deadline_secs)` (weight 0.2)
       - `availability_score` = 1.0 if last_seen < 5min, decay (weight 0.1)
    5. Return highest-scoring agent
  - `accept_match(db, request_id, requester_id) -> dict`:
    1. Get match request, verify requester
    2. Create SLA contract with matched agent
    3. Update state to `assigned`
  - `expire_stale(db) -> int`:
    1. Find searching requests past expires_at
    2. Mark as expired
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_matchmaking.py -x -v`
- [ ] **Expected**: All tests pass

#### Step 9.5: Implement matchmaking router
- [ ] **File**: `api/routers/matchmaking.py`
- [ ] **Action**: Create router `APIRouter(prefix="/v2/matchmaking", tags=["matchmaking"])`:
  - `POST /request` -- create match request
  - `GET /{id}` -- get match result
  - `POST /{id}/accept` -- accept matched agent, create SLA
- [ ] **Action**: Create schemas in `api/schemas_matchmaking.py`
- [ ] **Action**: Register router in `api/main_v2.py`
- [ ] **Action**: Update `tests/conftest.py`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_matchmaking.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 10: SDK Methods + Tests

#### Step 10.1: Write SDK tests (RED)
- [ ] **File**: `tests/test_sdk_marketplace_v2.py`
- [ ] **Action**: Write tests using `unittest.mock.patch` on `Sthrip._raw_post`, `_raw_get`:
  - `test_sla_template_create` -- calls `POST /v2/sla/templates`
  - `test_sla_create` -- calls `POST /v2/sla/contracts`
  - `test_sla_accept` -- calls `PATCH /v2/sla/contracts/{id}/accept`
  - `test_sla_deliver` -- calls `PATCH /v2/sla/contracts/{id}/deliver`
  - `test_sla_verify` -- calls `PATCH /v2/sla/contracts/{id}/verify`
  - `test_review_create` -- calls `POST /v2/agents/{id}/reviews`
  - `test_matchmake` -- calls `POST /v2/matchmaking/request`
  - `test_find_agents_extended` -- calls `GET /v2/agents/marketplace` with new params
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sdk_marketplace_v2.py -x -v 2>&1 | head -20`
- [ ] **Expected**: Tests fail

#### Step 10.2: Implement SDK methods (GREEN)
- [ ] **File**: `sdk/sthrip/client.py`
- [ ] **Action**: Add methods to `Sthrip` class:
  ```python
  def sla_template_create(self, name, deliverables, response_time_secs, delivery_time_secs, base_price, penalty_percent=10, service_description=""):
      payload = {
          "name": name, "service_description": service_description,
          "deliverables": deliverables, "response_time_secs": response_time_secs,
          "delivery_time_secs": delivery_time_secs, "base_price": str(base_price),
          "penalty_percent": penalty_percent,
      }
      return self._raw_post("/v2/sla/templates", json_body=payload)

  def sla_create(self, provider, template_id=None, price=None, **kwargs):
      payload = {"provider_agent_name": provider}
      if template_id: payload["template_id"] = template_id
      if price: payload["price"] = str(price)
      payload.update(kwargs)
      return self._raw_post("/v2/sla/contracts", json_body=payload)

  def sla_accept(self, contract_id):
      return self._raw_patch("/v2/sla/contracts/{}/accept".format(contract_id))

  def sla_deliver(self, contract_id, result_hash=None):
      payload = {}
      if result_hash: payload["result_hash"] = result_hash
      return self._raw_patch("/v2/sla/contracts/{}/deliver".format(contract_id), json_body=payload)

  def sla_verify(self, contract_id):
      return self._raw_patch("/v2/sla/contracts/{}/verify".format(contract_id))

  def review(self, agent_id, transaction_id, transaction_type, overall_rating, **kwargs):
      payload = {
          "transaction_id": transaction_id, "transaction_type": transaction_type,
          "overall_rating": overall_rating,
      }
      payload.update(kwargs)
      return self._raw_post("/v2/agents/{}/reviews".format(agent_id), json_body=payload)

  def matchmake(self, capabilities, budget, deadline_secs, min_rating=0, auto_assign=False):
      payload = {
          "required_capabilities": capabilities, "budget": str(budget),
          "deadline_secs": deadline_secs, "min_rating": str(min_rating),
          "auto_assign": auto_assign, "task_description": "Auto-matchmake",
      }
      return self._raw_post("/v2/matchmaking/request", json_body=payload)
  ```
- [ ] **Action**: Extend `find_agents` to accept `min_rating`, `max_price`, `has_sla`, `sort` kwargs
- [ ] **Action**: Bump `_VERSION` to `"0.4.0"`
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -m pytest tests/test_sdk_marketplace_v2.py -x -v`
- [ ] **Expected**: All tests pass

---

### Task 11: Alembic Migration

#### Step 11.1: Create migration file
- [ ] **File**: `migrations/versions/g8h9i0j1k2l3_marketplace_v2.py`
- [ ] **Action**: Create migration that creates 5 new tables (sla_templates, sla_contracts, agent_reviews, agent_rating_summary, match_requests) with IF NOT EXISTS guards
- [ ] **Action**: Follow pattern from `e6f7a8b9c0d1_multi_milestone_escrow.py`: check `is_pg` for PostgreSQL-specific enum creation
- [ ] **Action**: Create enums `slastatus` and `matchrequeststatus` with `DO $$ BEGIN ... EXCEPTION WHEN duplicate_object THEN NULL; END $$`
- [ ] **Why**: Idempotent migration per project convention
- [ ] **Test command**: `cd /Users/saveliy/Documents/Agent\ Payments/sthrip && python -c "from migrations.versions.g8h9i0j1k2l3_marketplace_v2 import upgrade; print('OK')"`
- [ ] **Expected**: Prints `OK`

---

## Testing Strategy

- **Unit tests**: `test_sla_repo.py`, `test_review_repo.py`, `test_sla_service.py` -- 25+ tests covering repo CRUD, state machines, scoring
- **Integration tests**: `test_sla_api.py`, `test_review_api.py`, `test_matchmaking.py`, `test_discovery_v2.py` -- 30+ tests covering full API flows
- **SDK tests**: `test_sdk_marketplace_v2.py` -- 8 tests verifying SDK method signatures and request construction
- **Target coverage**: 85%+ on new code

## Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| SLA enforcement races with manual verify | Medium | Row-level locking on SLAContract, status-guarded UPDATEs |
| Rating summary stale after review | Low | Recalculate in same transaction as review creation |
| Matchmaking query slow with many agents | Medium | Index on capabilities (JSONB GIN), rating (B-tree); limit candidates to 100 |
| ZK review proofs complexity | Medium | Extend existing proven ZK pattern, keep bit-decomposition approach |
| SQLite test compat for JSONB operators | Medium | Python-side filtering fallback for `@>` operator in tests |

## Success Criteria

- [ ] SLA template CRUD works end-to-end
- [ ] SLA contract lifecycle (propose -> accept -> deliver -> verify) works
- [ ] SLA auto-enforcement breaches overdue contracts within 30 seconds
- [ ] Reviews tied to real transactions, duplicates rejected
- [ ] Rating summaries computed correctly with partial ratings
- [ ] ZK review proofs generated and verified
- [ ] Discovery API filters by rating, price, SLA availability
- [ ] Matchmaking returns scored candidates and auto-creates SLA when requested
- [ ] SDK v0.4.0 has all new methods
- [ ] All tests pass (80%+ coverage on new code)
- [ ] Migration is idempotent
