# Production Readiness Plan — Phase 3: Final Pre-Launch

**Date**: 2026-03-03
**Scope**: Last blockers before production launch (Monero integration handled separately)
**Goal**: Close remaining gaps — SSRF at send time, missing endpoint, concurrency safety, observability

---

## 1. Webhook SSRF Re-validation at Send Time (P0)
**File**: `sthrip/services/webhook_service.py` — `_send_webhook()`
**Problem**: URL validated only at registration. DNS rebinding attack: domain resolves to public IP at registration, then to 169.254.x.x at delivery time.
**Fix**:
- Before `session.post()`, resolve hostname and check IP against private/loopback/link-local ranges
- Extract validation logic from `AgentRegistration.validate_webhook_url` into shared `sthrip/services/url_validator.py`
- Call `validate_url_target(url)` both at registration AND before each webhook send
- On failure, mark webhook event as FAILED with `ssrf_blocked` error

## 2. Payment Lookup Endpoint (P0)
**File**: `api/main_v2.py`
**Problem**: No `GET /v2/payments/{payment_id}` — clients can't check payment status after sending
**Fix**:
- Add `GET /v2/payments/{payment_id}` authenticated endpoint
- Return hub route details (status, fee, timestamps) if caller is sender or receiver
- Return 404 if payment doesn't exist or caller is unauthorized

## 3. Concurrent Balance Tests (P0)
**File**: `tests/test_concurrent_payments.py` (new)
**Problem**: `SELECT FOR UPDATE` added but never tested under concurrency
**Fix**:
- Test 1: Two concurrent deductions from same balance — only one should succeed
- Test 2: Concurrent deposit + deduct — final balance must be consistent
- Test 3: Rapid duplicate hub-routing with same idempotency key — exactly one route created
- Use `threading.Thread` + PostgreSQL test DB (or verify SQLite serialized fallback)

## 4. Secure .env Defaults (P0)
**Files**: `.env.railway.example`, `deploy/.env.example`
**Problem**: `CORS_ORIGINS=*`, placeholder passwords left in examples
**Fix**:
- `.env.railway.example`: Change `CORS_ORIGINS=*` → `CORS_ORIGINS=` (empty), add comment explaining format
- Remove `change_me` placeholder values, replace with `# REQUIRED: generate with openssl rand -hex 32`
- `deploy/.env.example`: Same treatment for `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `ADMIN_API_KEY`, `SECRET_KEY`
- Add startup check: if `ADMIN_API_KEY` matches known placeholder strings, log CRITICAL and refuse to start

## 5. Agent Settings Update Endpoint (P1)
**File**: `api/main_v2.py`
**Problem**: No way to change webhook_url or privacy_level after registration — forces re-registration
**Fix**:
- Add `PATCH /v2/me/settings` authenticated endpoint
- Allow updating: `webhook_url` (with SSRF validation), `privacy_level`, wallet addresses
- Audit log the change with old + new values

## 6. Structured JSON Logging (P1)
**File**: `api/main_v2.py`, new `sthrip/logging_config.py`
**Problem**: `basicConfig()` outputs unstructured text — hard to parse in Railway/Datadog/CloudWatch
**Fix**:
- Create JSON log formatter: `{"timestamp", "level", "message", "request_id", "agent_id", ...}`
- Add `X-Request-ID` middleware — generate UUID per request, include in all log lines
- Configure via `LOG_FORMAT=json|text` env var (text for local dev, json for production)

## 7. Prometheus Metrics Endpoint (P1)
**File**: `api/main_v2.py`, `sthrip/services/monitoring.py`
**Problem**: `prometheus-client` in requirements but no `/metrics` endpoint
**Fix**:
- Add counters: `http_requests_total`, `http_request_duration_seconds`, `hub_payments_total`, `balance_operations_total`
- Add gauges: `active_agents`, `pending_webhooks`, `db_pool_size`
- Expose `GET /metrics` (admin-key protected or separate port)
- Add middleware to auto-track request count + latency

## 8. API Key Rotation (P2)
**File**: `api/main_v2.py`, `sthrip/db/repository.py`
**Problem**: No way to rotate compromised API keys
**Fix**:
- Add `POST /v2/me/rotate-key` — generates new key, returns it once, invalidates old key
- Old key remains valid for 1 hour grace period (configurable)
- Audit log the rotation event

## 9. Webhook Delivery Status Endpoint (P2)
**File**: `api/main_v2.py`
**Problem**: No visibility into webhook delivery — agent can't diagnose missed events
**Fix**:
- Add `GET /v2/webhooks/events` — list recent webhook events for authenticated agent
- Include: event_type, status, attempt_count, last_error, delivered_at
- Add `POST /v2/webhooks/events/{id}/retry` — manually retry a failed webhook

---

## Task Priority

| # | Task | Priority | Effort |
|---|------|----------|--------|
| 1 | Webhook SSRF re-validation | P0 | 30 min |
| 2 | Payment lookup endpoint | P0 | 20 min |
| 3 | Concurrent balance tests | P0 | 30 min |
| 4 | Secure .env defaults | P0 | 15 min |
| 5 | Agent settings update | P1 | 25 min |
| 6 | Structured JSON logging | P1 | 30 min |
| 7 | Prometheus metrics | P1 | 30 min |
| 8 | API key rotation | P2 | 25 min |
| 9 | Webhook delivery status | P2 | 20 min |
