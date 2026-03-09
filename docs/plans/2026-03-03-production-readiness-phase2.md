# Production Readiness Plan — Phase 2: Data Safety & Reliability

**Date**: 2026-03-03
**Scope**: Tasks that can be done NOW without Monero node, paid Railway tier, or additional infrastructure
**Goal**: Prevent double-spend, fix broken IP tracking, harden inputs, ensure reliable shutdown

---

## 1. Atomic Balance Operations (P0)
**File**: `sthrip/db/repository.py` — `BalanceRepository.deduct()`, `credit()`
**Problem**: No `SELECT FOR UPDATE` — concurrent payments can double-spend
**Fix**: Use `with_for_update()` on balance reads before deduct/credit

## 2. DB Indexes for Hot Paths (P0)
**File**: `sthrip/db/models.py`
**Problem**: `api_key_hash` has no index — full table scan on every auth request
**Fix**: Add Index on `agents.api_key_hash`, `webhook_events.(status, next_attempt_at)`, `hub_routes.from_agent_id`

## 3. Generate Initial Alembic Migration (P0)
**Problem**: `migrations/versions/` is empty — alembic startup path is a no-op
**Fix**: Run `alembic revision --autogenerate` to create initial migration

## 4. Request Body Size Limit (P0)
**File**: `api/main_v2.py`
**Problem**: No middleware capping request bytes — clients can POST arbitrarily large JSON
**Fix**: Add middleware to reject requests >1MB

## 5. X-Forwarded-For / ProxyHeaders (P0)
**File**: `api/main_v2.py`
**Problem**: Behind Railway proxy, `request.client.host` is always the proxy IP
**Fix**: Add `ProxyHeadersMiddleware` from uvicorn, read real IP from X-Forwarded-For

## 6. Query Limit Caps (P1)
**File**: `api/main_v2.py`
**Problem**: `limit` params have no upper bound — `?limit=1000000` causes full table scans
**Fix**: Add `le=500` to all limit params in Pydantic

## 7. Webhook URL Validation (P1)
**File**: `api/main_v2.py`, `sthrip/services/webhook_service.py`
**Problem**: No SSRF protection — webhook_url can target internal IPs
**Fix**: Validate URL scheme (https only in prod), block private/internal IP ranges

## 8. Admin Tier Enum Validation (P1)
**File**: `api/main_v2.py` — verify_agent endpoint
**Problem**: `tier` param is raw string, invalid values corrupt DB
**Fix**: Validate against AgentTier enum

## 9. Direction Param Validation (P1)
**File**: `api/main_v2.py` — payment history endpoint
**Problem**: `direction` accepts any string, silently returns all transactions
**Fix**: Constrain to Literal["in", "out"] or None

## 10. Graceful Shutdown (P1)
**File**: `api/main_v2.py`, `railway.toml`
**Problem**: No in-flight request draining, no DB pool disposal on shutdown
**Fix**: Add `--timeout-graceful-shutdown 30` to uvicorn, dispose engine in lifespan

## 11. Startup Env Validation (P2)
**File**: `api/main_v2.py` lifespan
**Problem**: Missing env vars cause silent failures (e.g. no ADMIN_API_KEY = admin endpoints always reject)
**Fix**: Check required vars on startup, log warnings for missing optional ones

## 12. Generate webhook_secret on Registration (P2)
**File**: `sthrip/services/agent_registry.py`
**Problem**: webhook_secret is never set — all webhooks are sent unsigned
**Fix**: Generate random secret on registration, return it once with API key

## 13. Fix railway.toml / railway.json Conflict (P2)
**Files**: `railway.toml`, `railway.json`
**Problem**: Conflicting start commands and worker counts
**Fix**: Single source of truth in railway.toml, remove conflicting railway.json fields

---

## Task Priority

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 1 | Atomic balance operations | P0 | 30 min |
| 2 | DB indexes | P0 | 15 min |
| 3 | Initial Alembic migration | P0 | 15 min |
| 4 | Request body size limit | P0 | 15 min |
| 5 | X-Forwarded-For | P0 | 15 min |
| 6 | Query limit caps | P1 | 10 min |
| 7 | Webhook URL validation | P1 | 30 min |
| 8 | Admin tier validation | P1 | 10 min |
| 9 | Direction param validation | P1 | 5 min |
| 10 | Graceful shutdown | P1 | 20 min |
| 11 | Startup env validation | P2 | 15 min |
| 12 | webhook_secret generation | P2 | 20 min |
| 13 | Fix railway config conflict | P2 | 10 min |
