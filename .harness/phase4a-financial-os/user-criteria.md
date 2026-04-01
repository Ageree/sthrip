# User Criteria: Phase 4a — Agent Financial OS

## Goal
Implement the first phase of the Agent Financial OS — treasury management, credit/lending, conditional payments, split payments, and atomic multi-party payments. These features transform Sthrip from a payment hub into a full financial operating system for autonomous AI agents.

## Acceptance Criteria
- Treasury management: agents can set target allocation policies (e.g., 60% XMR / 30% xUSD / 10% xEUR), auto-rebalancing executes when drift exceeds threshold
- Credit scoring: reputation-based score (0-1000) computed from on-platform behavior (payment history, escrow completion, SLA fulfillment)
- Lending: overcollateralized loans (lock XMR, borrow xUSD), micro-loans, flash loans (borrow+use+repay atomically)
- Conditional payments: webhook trigger, time-lock, balance-threshold, escrow-completion triggers
- Split payments: one payment auto-splits to multiple recipients atomically
- Atomic multi-party payments: all-or-nothing group pay (A pays B, C, D simultaneously)
- All features have full API endpoints, SDK methods, and tests
- TDD: tests written first, 80%+ coverage on new code
- All existing 2233 tests continue to pass (zero regressions)
- Deploy to Railway when complete

## Constraints
- Python 3.9, FastAPI, SQLAlchemy 2.0, PostgreSQL (prod), SQLite (tests)
- Use python3 not python
- Follow existing patterns: repo pattern, service layer, router layer, Pydantic schemas
- AgentBalance uses `available` field (not `balance`)
- BalanceRepository.get_or_create(agent_id, token) for multi-currency
- Row-level locking for concurrent balance operations
- Mock audit_log and queue_webhook in service tests
- Immutable dict returns from services (never return ORM objects)
- Detailed spec at: docs/superpowers/specs/2026-04-01-agent-financial-os-design.md
