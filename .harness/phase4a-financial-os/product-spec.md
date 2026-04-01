# Product Spec: Phase 4a -- Agent Financial OS

## Scope

Phase 4a implements the six MUST-HAVE features from the Agent Financial OS design:

1. Treasury Management (Feature 1)
2. Credit Scoring (Feature 2a)
3. Lending (Feature 2b)
4. Conditional Payments (Feature 3a)
5. Split Payments (Feature 3c)
6. Atomic Multi-Party Payments (Feature 5a)

Explicitly OUT OF SCOPE for Phase 4a: Payment DAGs (3b), Agent Collectives (4), Flash Loans (2c), Service Order Book (5b), Revenue Sharing (6a), API Access Metering (6b), Prediction Markets (6c).

## Architecture Decisions

### AD-1: New Enums
- `LoanStatus`: requested, active, repaid, defaulted, liquidated, cancelled
- `ConditionalPaymentState`: pending, triggered, executed, expired, cancelled
- `MultiPartyPaymentState`: pending, accepted, completed, rejected, expired

### AD-2: New Models (9 tables)
- Treasury: TreasuryPolicy, TreasuryForecast, TreasuryRebalanceLog
- Credit/Lending: AgentCreditScore, AgentLoan, LendingOffer
- Conditional/Split/Multi-Party: ConditionalPayment, MultiPartyPayment, MultiPartyRecipient

### AD-3: New Repos (5 files)
- treasury_repo.py, credit_repo.py, loan_repo.py, conditional_payment_repo.py, multi_party_repo.py

### AD-4: New Services (5 files)
- treasury_service.py, credit_service.py, conditional_payment_service.py, split_payment_service.py, multi_party_service.py

### AD-5: New Routers (4 files)
- api/routers/treasury.py, api/routers/lending.py, api/routers/conditional_payments.py, api/routers/multi_party.py
- Split payments added to existing api/routers/payments.py

### AD-6: SDK (~20 new methods)

## Sprint Breakdown

### Sprint 1: Foundation -- Enums + Models + Repos
All database tables, enums, and repository classes. No business logic. Tests for CRUD and state transitions.

### Sprint 2: Treasury Management Service + API + Tests
Set/get policy, forecast, manual rebalance, history. Uses ConversionService for rebalancing.

### Sprint 3: Credit Scoring + Lending Service + API + Tests
4-factor credit score (0-1000), lending offers marketplace, loan lifecycle, default detection.

### Sprint 4: Conditional Payments + Split Payments + Tests
Condition types: time_lock, escrow_completed, balance_threshold, webhook. Atomic split payments.

### Sprint 5: Atomic Multi-Party Payments + Tests
All-or-nothing group payments with accept/reject flow. Two modes: require_all_accept=True/False.

### Sprint 6: SDK Methods + Migration + Final Verification
~20 SDK methods, conftest updates, full regression run.

## Cross-Cutting Concerns
- Audit logging on every state transition
- Webhooks for inter-agent notifications
- Balance locking via BalanceRepository.deduct/credit
- Row-level locking for concurrent operations
