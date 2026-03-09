# Production Readiness Plan — Phase 1

**Date**: 2026-03-03
**Scope**: Tasks that can be done NOW without Monero node, paid Railway tier, or additional infrastructure
**Goal**: Harden the existing deployed API so it's ready for real users when settlement layer is added

---

## 1. Security Hardening

### 1.1 CORS Configuration
**File**: `api/main_v2.py:82`
**Problem**: CORS allows `*` by default — any website can make API requests
**Fix**:
- Change default CORS to reject all origins
- Accept only configured origins from `CORS_ORIGINS` env var
- Add `CORS_ORIGINS=https://sthrip-api-production.up.railway.app` on Railway
- Allow `http://localhost:*` only when `ENVIRONMENT=dev`

### 1.2 Constant-Time Admin Key Comparison
**File**: `api/main_v2.py` (admin endpoints)
**Problem**: Admin key compared with `==` — vulnerable to timing attacks
**Fix**: Use `hmac.compare_digest()` for all secret comparisons (admin key, API key verification)

### 1.3 Rate Limit on Registration
**File**: `api/main_v2.py` — `POST /v2/agents/register`
**Problem**: No rate limiting — anyone can spam registrations and exhaust DB
**Fix**:
- Add IP-based rate limit: 5 registrations per hour per IP
- Add global rate limit: 100 registrations per hour total
- Return 429 with `Retry-After` header

### 1.4 Idempotency Keys on Payments
**File**: `api/main_v2.py` — `POST /v2/payments/hub-routing`, `POST /v2/balance/deposit`, `POST /v2/balance/withdraw`
**Problem**: If client retries a failed request, payment may be duplicated
**Fix**:
- Accept `Idempotency-Key` header (UUID)
- Store idempotency key → response mapping in Redis (TTL 24h)
- If same key seen again, return cached response without re-executing
- Add `idempotency_keys` table or use Redis hash

### 1.5 Log Sanitization
**Files**: All files that log
**Problem**: Ensure API keys, wallet addresses, and admin keys never appear in full in logs
**Fix**:
- Audit all `logger.info/warning/error` calls
- Truncate sensitive fields: `sk_abc...xyz` format
- Ensure `ADMIN_API_KEY` is never logged

---

## 2. Data Integrity

### 2.1 Audit Logging
**Files**: `sthrip/db/models.py` (AuditLog model exists), `api/main_v2.py`
**Problem**: `audit_log` table exists in schema but nothing writes to it
**Fix**:
- Create `audit_logger.py` service with `log_event(agent_id, action, details, ip_address)`
- Log these events:
  - `agent.registered` — new agent created
  - `agent.verified` — admin verified an agent
  - `payment.hub_routing` — hub payment executed
  - `balance.deposit` — deposit requested
  - `balance.withdraw` — withdrawal requested
  - `admin.stats_viewed` — admin accessed stats
  - `auth.failed` — failed authentication attempt
- Include: timestamp, agent_id, action, IP address, request details

### 2.2 Alembic Migrations
**Problem**: No migration versioning — `create_tables()` runs raw CREATE on startup, dangerous for schema changes
**Fix**:
- Initialize Alembic in project root
- Generate initial migration from current models
- Replace `create_tables()` in lifespan with `alembic upgrade head`
- Add `alembic.ini` and `migrations/` directory
- Document migration workflow in README

### 2.3 Transaction Idempotency
**File**: `api/main_v2.py` — hub routing handler
**Problem**: No unique constraint prevents the same payment from being processed twice
**Fix**:
- Add `payment_id` as unique constraint in `hub_routes` table
- Generate deterministic payment_id from (sender + recipient + amount + idempotency_key)
- Reject duplicate payment_id with 409 Conflict

---

## 3. Monitoring & Alerts

### 3.1 Alert Dispatch to Telegram/Discord
**File**: `sthrip/services/monitoring.py`
**Problem**: Alert system creates alerts but doesn't send them anywhere
**Fix**:
- Add `ALERT_WEBHOOK_URL` env var (Discord or Telegram bot webhook)
- Implement `dispatch_alert(alert)` that POSTs to webhook
- Format: severity, check name, error message, timestamp
- For Telegram: use Bot API `sendMessage`
- For Discord: use webhook with embed

### 3.2 Critical Event Alerts
**File**: `sthrip/services/monitoring.py`
**Problem**: No alerts for business-critical events
**Fix**:
- Alert on: 3+ consecutive health check failures
- Alert on: payment processing error
- Alert on: rate limit exceeded by 10x (possible attack)
- Alert on: database connection lost
- Alert on: Redis connection lost
- Debounce: max 1 alert per event type per 5 minutes

---

## 4. Tests & Quality

### 4.1 Security Tests
**File**: `tests/test_security.py` (new)
**Tests to add**:
- Auth failure returns 401, not 500
- Invalid API key format rejected
- Rate limit returns 429 with proper headers
- SQL injection attempts in agent name rejected
- XSS payloads in webhook URL rejected
- Oversized request body rejected
- Missing required fields return 422

### 4.2 E2E Hub Payment Flow Test
**File**: `tests/test_e2e_hub_flow.py` (new)
**Test flow**:
1. Register agent A
2. Register agent B
3. Agent A deposits 10 XMR (mock)
4. Agent A sends 5 XMR to Agent B via hub routing
5. Verify: Agent A balance = 10 - 5 - fee
6. Verify: Agent B balance = 5
7. Verify: fee_collections has 1 entry
8. Verify: payment history has 1 entry for each agent

### 4.3 Test Coverage Report
**Fix**:
- Add `pytest-cov` to dev dependencies
- Configure `.coveragerc` for sthrip package
- Target: 70%+ coverage on `api/main_v2.py` and `services/`
- Add coverage report to CI (GitHub Actions already has workflow dir)

---

## Task Priority & Estimates

| # | Task | Priority | Effort | Impact |
|---|------|----------|--------|--------|
| 1 | CORS configuration | P0 | 15 min | Prevents XSS/CSRF |
| 2 | Constant-time comparison | P0 | 10 min | Prevents timing attack |
| 3 | Rate limit on register | P0 | 30 min | Prevents spam |
| 4 | Audit logging | P1 | 1-2 hrs | Compliance, debugging |
| 5 | Idempotency keys | P1 | 1-2 hrs | Prevents double payments |
| 6 | Alert dispatch | P1 | 1 hr | Know when things break |
| 7 | Log sanitization | P1 | 30 min | Prevents key leaks |
| 8 | Security tests | P1 | 1-2 hrs | Catch regressions |
| 9 | E2E hub flow test | P2 | 1 hr | Validates core flow |
| 10 | Alembic migrations | P2 | 1-2 hrs | Safe schema updates |
| 11 | Test coverage report | P2 | 30 min | Visibility |
| 12 | Transaction idempotency | P2 | 1 hr | Data integrity |

**Total estimated effort**: ~10-14 hours

---

## Definition of Done

- [ ] All P0 items implemented and deployed to Railway
- [ ] All P1 items implemented with tests
- [ ] Security tests pass in CI
- [ ] E2E hub flow test passes
- [ ] Alert webhook configured and tested
- [ ] No API keys or secrets appear in Railway logs
- [ ] Coverage report generated (target 70%+)

---

## What This Does NOT Cover (Phase 2+)

These require paid Railway tier / Monero node / additional infrastructure:
- Monero wallet RPC integration (deposit/withdrawal settlement)
- On-chain settlement layer
- Fee withdrawal to operator wallet
- Balance reconciliation (DB vs blockchain)
- Backup automation for PostgreSQL
- Custom domain + SSL
- Mainnet deployment
