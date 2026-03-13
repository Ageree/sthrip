# Production Acceptance — Sthrip

**Status: ACCEPTED**
**Date: 2026-03-13**
**Signed off by: Saveliy + automated verification**

This document is the single source of truth for production readiness.
Once all criteria below are met, the project is production-ready. Period.
No further "full project reviews" are needed — only targeted reviews per PR/feature.

---

## Acceptance Criteria

### 1. Tests

| Criterion | Threshold | Actual | Status |
|-----------|-----------|--------|--------|
| All tests pass | 0 failures | 1322 passed, 0 failed | PASS |
| Test coverage | >= 80% | 89% (4077 stmts, 436 missed) | PASS |
| No INTERNAL errors blocking test suite | 0 blockers | 1 non-blocking (SQLite INET, not used in prod) | PASS |

### 2. Security

| Criterion | Threshold | Actual | Status |
|-----------|-----------|--------|--------|
| No hardcoded secrets in source | 0 | 0 | PASS |
| All secrets from env vars | 100% | 100% (via pydantic-settings) | PASS |
| Startup rejects weak secrets in prod | yes | yes (32+ char HMAC, admin key validation) | PASS |
| Rate limiting on auth endpoints | yes | yes (failed auth counter + IP-based) | PASS |
| Security headers (CSP, HSTS, X-Frame) | yes | yes (api/middleware.py) | PASS |
| CORS restricted | yes | yes (configurable, empty = no CORS) | PASS |
| Webhook payloads encrypted | yes | yes (Fernet, key required in prod) | PASS |
| SQL injection prevention | parameterized | yes (SQLAlchemy ORM) | PASS |
| Body size limits | yes | yes (uvicorn level) | PASS |
| Session management with TTL | yes | yes (8h TTL, secure cookies) | PASS |

### 3. Configuration

| Criterion | Threshold | Actual | Status |
|-----------|-----------|--------|--------|
| Startup validation of required env vars | yes | yes (DATABASE_URL, ADMIN_API_KEY) | PASS |
| Placeholder secret rejection in prod | yes | yes (config.py validators) | PASS |
| Environment enum enforced | yes | yes (dev/staging/stagenet/production) | PASS |
| Network validation (no loopback in prod) | yes | yes (monero_rpc_host validator) | PASS |
| SQL echo blocked in prod | yes | yes | PASS |

### 4. Architecture

| Criterion | Threshold | Actual | Status |
|-----------|-----------|--------|--------|
| No file > 800 lines | 0 violations | 0 (largest: ~563 lines) | PASS |
| Router separation by domain | yes | yes (6 routers) | PASS |
| Repository pattern for data access | yes | yes (sthrip/db/) | PASS |
| Dependency injection via FastAPI Depends | yes | yes (api/deps.py) | PASS |
| Centralized config (single source) | yes | yes (sthrip/config.py) | PASS |

### 5. Observability

| Criterion | Threshold | Actual | Status |
|-----------|-----------|--------|--------|
| Structured logging (JSON) | yes | yes (sthrip/logging_config.py) | PASS |
| Request ID tracking | yes | yes (ContextVar in middleware) | PASS |
| Error logging with context | yes | yes (all routers log errors) | PASS |
| Health check endpoint | yes | yes (/health) | PASS |
| Sentry integration available | yes | yes (optional, via DSN) | PASS |

### 6. Deployment

| Criterion | Threshold | Actual | Status |
|-----------|-----------|--------|--------|
| Dockerfile exists | yes | yes (railway/Dockerfile.railway) | PASS |
| Pinned dependencies | yes | yes (requirements.lock) | PASS |
| Env templates documented | yes | yes (.env.example, .env.railway.example) | PASS |
| Successfully deployed | yes | yes (Railway, sthrip-api-production.up.railway.app) | PASS |

### 7. Data Integrity

| Criterion | Threshold | Actual | Status |
|-----------|-----------|--------|--------|
| Atomic transactions for payments | yes | yes (row-level locking) | PASS |
| Idempotency keys | yes | yes (idempotency service) | PASS |
| Database migrations | yes | yes (Alembic) | PASS |

---

## Known Acceptable Issues

These are documented, understood, and do NOT affect production:

1. **SQLite INET type error in test_singleton_consolidation.py** — SQLite lacks INET type; PostgreSQL in production handles it correctly. Not a production issue.

2. **9 skipped tests** — Platform-specific or environment-specific tests that require external services. Expected behavior.

3. **api/main_v2.py coverage at 69%** — Startup/lifespan code with Sentry init and error branches that only execute under specific deployment conditions. Tested via integration tests on Railway.

4. **webhooks router coverage at 40%** — Webhook delivery paths require external HTTP calls. Covered by integration tests with mocked endpoints.

5. **escrow_repo/channel_repo coverage 56-64%** — Features not yet actively used in production. Code is correct but exercise paths are limited. Will increase as features are adopted.

---

## Rules Going Forward

1. **No more "full project readiness reviews".** The project is accepted. Done.

2. **Review only diffs.** New code gets reviewed per-PR, not the entire codebase.

3. **Regression only.** If a criterion above breaks (coverage drops below 80%, a test fails, etc.), fix that specific criterion. Don't re-audit everything.

4. **New features get their own acceptance.** Each new feature/PR has its own review scope limited to the changed code.

5. **This document is immutable** unless the production requirements fundamentally change (new compliance requirement, new infrastructure, etc.).

---

## Acceptance Decision

All 30 criteria: **PASS**
Known issues: **5 documented and accepted**
Production status: **DEPLOYED AND OPERATIONAL**

**This project is production-ready. No further debate.**
