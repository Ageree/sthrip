# User Criteria: cross-chain-swaps

## Goal
Replace the current HTLC stub swap implementation with real cross-chain swap functionality using ChangeNOW API (primary) and SideShift.ai API (fallback). Users should be able to deposit BTC, ETH, or SOL and receive XMR credit on their Sthrip balance.

## Acceptance Criteria
- Users can create a swap order specifying source currency (BTC/ETH/SOL) and amount
- Platform returns a deposit address (from ChangeNOW/SideShift) where user sends crypto
- Background poller checks swap status every 60s
- On completion, XMR is credited to user's hub balance automatically
- If ChangeNOW is unavailable, system falls back to SideShift
- Stale/expired swaps are cleaned up automatically
- All existing tests continue to pass (2221 baseline)
- New tests achieve 80%+ coverage on new code
- SDK methods work with the new flow

## Constraints
- Python 3.9, FastAPI, SQLAlchemy, PostgreSQL
- Immutable data patterns (no mutation of existing objects)
- Repository pattern (already established in codebase)
- Tests use SQLite in-memory with StaticPool
- Existing SwapOrder model, SwapRepository, SwapService, swap router must be evolved (not replaced from scratch)
- httpx for HTTP calls (already in requirements)
- New env vars: CHANGENOW_API_KEY, SIDESHIFT_AFFILIATE_ID (optional)
- Alembic migrations must be idempotent (IF NOT EXISTS)
- Production deployment on Railway
