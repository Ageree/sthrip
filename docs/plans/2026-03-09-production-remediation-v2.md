# Implementation Plan: Production Remediation v2

**Date:** 2026-03-09
**Status:** PENDING CONFIRMATION
**Total issues:** 40 (5 CRITICAL, 13 HIGH, 13 MEDIUM, 9 LOW, 7 TEST gaps)
**Estimated effort:** ~15 hours across 10 phases

## Overview

Comprehensive remediation of 40 production readiness issues found in the Sthrip
project, organized into 10 independently-deliverable phases. Each phase targets
a logical grouping of related fixes and includes its own test strategy.

Current state: 600 tests passing, 84% coverage. Target: maintain 80%+ after all changes.

## Requirements

- Fix all 5 CRITICAL, 13 HIGH, 13 MEDIUM, 9 LOW, and 7 TEST issues
- Each phase mergeable independently
- TDD approach: write test first, then fix
- No regressions in existing 600 tests
- Maintain 80%+ test coverage

## Phase Dependency Graph

```
Phase 1 (CRIT-1: withdrawal recovery) ─────────────────┐
Phase 2 (CRIT-2,3: config/creds) ──── independent      │
Phase 3 (CRIT-4,5: admin auth/audit) ── independent     │
Phase 4 (HIGH: data integrity) ──────── independent     │
Phase 5 (HIGH: sessions/network) ────── independent     │
Phase 6 (HIGH: access control) ──────── independent     │
Phase 7 (MED: headers/validation) ───── independent     │
Phase 8 (MED: data/code quality) ───── depends on P1 ──┘
Phase 9 (LOW: misc) ─────────────────── independent
Phase 10 (TESTS) ───────────────────── depends on P1-9
```

Phases 1-7 and 9 can be executed in parallel by different developers.
Phase 8 depends on Phase 1 only for MED-1 (subset of CRIT-1).
Phase 10 should be done last to test the final state.

---

## Phase 1: CRITICAL — Financial Safety (CRIT-1)

**Estimated complexity: HIGH | Session: ~2 hours**
**Dependencies: None**

This is the highest-risk issue in the codebase. A wallet restart or sync lag
causes automatic balance restoration to agents who may have already received
on-chain XMR.

### Step 1.1: Write tests for safe withdrawal recovery

**File:** `tests/test_withdrawal_recovery.py`

- Add tests for:
  - Empty `outgoing` list does NOT auto-credit (new behavior)
  - Stale withdrawal with no match is marked `needs_review`, NOT `failed` + credited
  - Stale withdrawal with a match by `(address, amount, timestamp)` is completed
  - `_find_matching_transfer` rejects match where timestamp delta > threshold
  - Alert/log emitted for unmatched stale withdrawals
- The current test file likely tests the old auto-credit behavior; must invert expectations before fixing code

### Step 1.2: Fix withdrawal recovery — remove auto-credit

**File:** `sthrip/services/withdrawal_recovery.py` (lines 43-51)

Replace the auto-credit logic with:

```python
else:
    # No matching on-chain tx — do NOT auto-credit.
    # Mark as needs_review for manual investigation.
    pw_repo.mark_needs_review(
        pw.id,
        reason="No matching on-chain tx after max_age_minutes",
    )
    logger.critical(
        "HUMAN_ACTION_REQUIRED: pw=%s agent=%s amount=%.12f has no "
        "matching on-chain tx. DO NOT auto-credit.",
        pw.id, pw.agent_id, pw.amount,
    )
```

### Step 1.3: Add timestamp guard to `_find_matching_transfer`

**File:** `sthrip/services/withdrawal_recovery.py` (lines 57-64)

Add `timestamp` parameter and reject matches where
`abs(tx_timestamp - pw.created_at) > timedelta(minutes=30)`. Match on
`(address, amount, timestamp_window)` instead of just `(address, amount)`.

### Step 1.4: Add `mark_needs_review` to PendingWithdrawalRepository

**File:** `sthrip/db/repository.py` (~line 810)

```python
def mark_needs_review(self, pw_id: str, reason: str) -> None:
    self.db.query(PendingWithdrawal).filter_by(id=pw_id).update({
        "status": "needs_review",
        "error": reason,
    })
    self.db.flush()
```

---

## Phase 2: CRITICAL — Security Configuration (CRIT-2, CRIT-3, MED-10)

**Estimated complexity: LOW | Session: ~45 minutes**
**Dependencies: None**

### Step 2.1: Write tests for config validation

**File:** `tests/test_config_centralized.py`

- `ENVIRONMENT=stagenet` with default HMAC secret raises `ValueError`
- `ENVIRONMENT=stagenet` with empty `webhook_encryption_key` raises `ValueError`
- `ENVIRONMENT=dev` with defaults still passes

### Step 2.2: Remove `stagenet` from allowed-default environments

**File:** `sthrip/config.py`

- Line 64: Change `if env not in ("dev", "staging", "stagenet")` → `if env not in ("dev",)`
- Line 84: Same change for `webhook_encryption_key`

> **IMPORTANT:** Set `API_KEY_HMAC_SECRET` and `WEBHOOK_ENCRYPTION_KEY` env vars on Railway BEFORE deploying this change.

### Step 2.3: Remove hardcoded Bitcoin RPC credentials

**File:** `sthrip/swaps/btc/rpc_client.py` (lines 240-245)

Replace hardcoded values with `os.getenv()` calls:

```python
def create_regtest_client() -> BitcoinRPCClient:
    import os
    return BitcoinRPCClient(
        host=os.getenv("BTC_REGTEST_HOST", "localhost"),
        port=int(os.getenv("BTC_REGTEST_PORT", "18443")),
        username=os.getenv("BTC_REGTEST_USER", ""),
        password=os.getenv("BTC_REGTEST_PASS", ""),
        network="regtest",
    )
```

---

## Phase 3: CRITICAL — Admin Auth & Audit Sanitization (CRIT-4, CRIT-5)

**Estimated complexity: MEDIUM | Session: ~1.5 hours**
**Dependencies: None**

### Step 3.1: Write tests for admin session-token auth

**File:** `tests/test_admin_auth.py` (new)

- POST `/v2/admin/auth` with correct admin key → returns bearer token
- GET `/v2/admin/stats` with bearer token → succeeds
- GET `/v2/admin/stats` with raw admin key in header → 401
- Expired token → 401
- GET `/metrics` with bearer token → succeeds

### Step 3.2: Implement admin session-token pattern

**File:** `api/routers/admin.py`

- Add `POST /v2/admin/auth` endpoint: verifies `admin_key`, returns signed token (8h TTL)
- Change `get_admin_stats` and `verify_agent` to use `Depends(get_admin_session)`
- Create `get_admin_session` dependency in `api/deps.py`
- Apply same pattern to `api/routers/health.py` line 82 (`/metrics` endpoint)

> **Risk mitigation:** Accept both raw header AND bearer token for 30-day deprecation period, log warnings for raw header usage.

### Step 3.3: Write tests for audit log sanitization

**File:** `tests/test_audit_logger.py`

- `details` dict with keys `api_key`, `password`, `secret`, `mnemonic`, `seed` → redacted to `"***"`

### Step 3.4: Add sanitize function to audit logger

**File:** `sthrip/services/audit_logger.py` (before line 52)

```python
_SENSITIVE_KEYS = frozenset({
    "api_key", "password", "secret", "mnemonic", "seed",
    "webhook_secret", "admin_key", "token", "credentials",
})

def _sanitize(data: Optional[dict]) -> Optional[dict]:
    if data is None:
        return None
    return {
        k: "***" if k.lower() in _SENSITIVE_KEYS else v
        for k, v in data.items()
    }
```

Then: `request_body=_sanitize(details)`

---

## Phase 4: HIGH — Data Integrity (HIGH-1, HIGH-2, HIGH-4, HIGH-5, HIGH-6)

**Estimated complexity: MEDIUM | Session: ~1.5 hours**
**Dependencies: None**

### Step 4.1: Fix `get_or_create` race condition

**File:** `sthrip/db/repository.py` (lines 691-701)

Wrap in try/except `IntegrityError`:

```python
def get_or_create(self, agent_id: UUID, token: str = "XMR") -> AgentBalance:
    balance = self.db.query(AgentBalance).filter(
        AgentBalance.agent_id == agent_id,
        AgentBalance.token == token,
    ).first()
    if balance:
        return balance
    try:
        balance = AgentBalance(agent_id=agent_id, token=token)
        self.db.add(balance)
        self.db.flush()
        return balance
    except IntegrityError:
        self.db.rollback()
        return self.db.query(AgentBalance).filter(
            AgentBalance.agent_id == agent_id,
            AgentBalance.token == token,
        ).first()
```

### Step 4.2: Fix `SystemStateRepository.set` TOCTOU

**File:** `sthrip/db/repository.py` (lines 829-840)

Use PostgreSQL `INSERT ... ON CONFLICT` with SQLite fallback for tests.

### Step 4.3: Fix `float(Decimal)` precision loss in withdrawals

**File:** `sthrip/services/wallet_service.py` (line 97)

Remove `float()` conversion — pass `Decimal` directly. Update `wallet.py` `transfer()` to accept `Decimal`.

### Step 4.4: Fix `stealth.py:mark_used` swallowing all exceptions

**File:** `sthrip/stealth.py` (lines 85-86)

```python
# Before:
except Exception:
    pass

# After:
except WalletRPCError:
    pass  # Address not found or not ours
```

### Step 4.5: Fix idempotency singleton thread safety

**File:** `sthrip/services/idempotency.py` (lines 190-197)

Add `threading.Lock()` with double-checked locking pattern (matching `get_rate_limiter()`).

---

## Phase 5: HIGH — Session & Network Safety (HIGH-3, HIGH-7, HIGH-8, HIGH-9)

**Estimated complexity: MEDIUM | Session: ~1.5 hours**
**Dependencies: None**

### Step 5.1: Fix DB session held across webhook HTTP calls

**File:** `sthrip/services/webhook_service.py` (lines 164-203)

Split into: **Session 1** (read event + agent config) → **HTTP call** (no DB session) → **Session 2** (write result).

### Step 5.2: Add public API methods to BalanceRepository

**File:** `sthrip/db/repository.py`

Add `add_pending(agent_id, amount)` and `clear_pending_on_confirm(agent_id, amount)`.

**File:** `sthrip/services/deposit_monitor.py`

Replace `bal_repo._get_for_update()` calls with new public methods.

### Step 5.3: Fix DNS rebinding in SSRF validation

**File:** `sthrip/services/url_validator.py`

Resolve DNS once, validate IP, then pass resolved IP to HTTP client (pin connection).

### Step 5.4: Add max_length to idempotency key headers

**Files:** `api/routers/payments.py:48`, `api/routers/balance.py:48,109`

Change `Header(None)` → `Header(None, max_length=255)` in all three locations.

---

## Phase 6: HIGH — Access Control (HIGH-10, HIGH-11, HIGH-12, HIGH-13)

**Estimated complexity: MEDIUM | Session: ~1.5 hours**
**Dependencies: None**

### Step 6.1: Rate-limit discovery endpoints

**File:** `api/routers/agents.py` (lines 77-131)

Apply `check_ip_rate_limit` to `get_agent_profile`, `discover_agents`, `get_leaderboard`. Gate `xmr_address` on `privacy_level` (hide for `high`/`paranoid`).

### Step 6.2: Restrict `wallet.query_key()` to `view_key` only

**File:** `sthrip/wallet.py` (line 184)

Add allowlist validation: `_ALLOWED_KEY_TYPES = {"view_key"}`.

### Step 6.3: Validate `transfer_type` before JSON-RPC

**File:** `sthrip/wallet.py` (lines 161-172)

Validate against `{"all", "available", "unavailable"}` before passing to RPC.

### Step 6.4: Fix `update_agent_settings` session handling

**File:** `api/routers/agents.py` (lines 151-191)

Use injected `Depends(get_db_session)` instead of opening second session. Re-check `is_active`.

---

## Phase 7: MEDIUM — Security Headers & Validation (MED-7, MED-8, MED-9, MED-13)

**Estimated complexity: LOW | Session: ~45 minutes**
**Dependencies: None**

### Step 7.1: Make logout POST with CSRF protection

**File:** `api/admin_ui/views.py` (line 201)

Change `@router.get("/logout")` → `@router.post("/logout")` with CSRF token.

### Step 7.2: Add XMR address validator to registration/settings schemas

**File:** `api/schemas.py`

Add `@field_validator("xmr_address")` calling `validate_monero_address` to `AgentRegistration` and `AgentSettingsUpdate`.

### Step 7.3: Add missing security headers

**File:** `api/middleware.py` (lines 131-141)

```python
response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
response.headers["X-XSS-Protection"] = "0"  # Modern: disable buggy filter, CSP protects
```

---

## Phase 8: MEDIUM — Data & Code Quality (MED-1 thru MED-6, MED-11, MED-12)

**Estimated complexity: MEDIUM | Session: ~2 hours**
**Dependencies: Phase 1 (MED-1 overlaps with CRIT-1 matching logic)**

### Step 8.1: MED-1 — already addressed by Phase 1 Step 1.3

Timestamp matching in `_find_matching_transfer` is part of Phase 1.

### Step 8.2: Consolidate duplicate `_do_poll` methods

**File:** `sthrip/services/deposit_monitor.py` (lines 199-234)

Remove `_do_poll`; have `_poll_with_redis_lock` call `_do_poll_with_session` with its own session.

### Step 8.3: Create Alembic migration for DateTime timezone consistency

**File:** New migration + `sthrip/db/models.py` (line 147)

Alter `agents.created_at`, `agents.updated_at`, `agents.last_seen_at` from `DateTime` → `DateTime(timezone=True)`. Instant on PostgreSQL (no table rewrite).

### Step 8.4: Fix fee collector N+1 query

**File:** `sthrip/services/fee_collector.py` (lines 385-405)

Replace per-ID loop with `filter(id.in_(fee_ids))` bulk query.

### Step 8.5: Fix SSRF DNS failure behavior

**File:** `sthrip/services/url_validator.py` (lines 79-81)

Add `block_on_dns_failure: bool = False` parameter. Set `True` when called from `_send_webhook`.

### Step 8.6: Fix agent name uniqueness TOCTOU

**File:** `sthrip/services/agent_registry.py` (lines 80-86)

Catch `IntegrityError` on commit and return clean 409.

### Step 8.7: Fix idempotency Redis key collision

**File:** `sthrip/services/idempotency.py` (line 61)

Hash the key component with SHA-256 before embedding in Redis key.

### Step 8.8: Redact webhook response body in storage

**File:** `sthrip/db/repository.py` (line 588)

Store only `f"status={response_code}"` instead of full response body.

---

## Phase 9: LOW Priority Fixes (LOW-1 through LOW-9)

**Estimated complexity: LOW-MEDIUM | Session: ~1.5 hours**
**Dependencies: None**

| Step | Issue | File | Fix |
|------|-------|------|-----|
| 9.1 | Crypto singleton lock | `sthrip/crypto.py:8-25` | Add `threading.Lock()` double-checked locking |
| 9.2 | Stealth cache ephemeral | `sthrip/stealth.py:24` | Document as by-design (wallet RPC is authoritative) |
| 9.3 | PendingWithdrawal UUID type | `sthrip/db/models.py:463` | Defer to Phase 8.3 migration batch |
| 9.4 | ORM mutation in fee_collector | `sthrip/services/fee_collector.py:315-317` | Use `.update()` query instead of `setattr` |
| 9.5 | Non-atomic failed-auth | `api/deps.py:106-145` | Use atomic `HINCRBY` for Redis, combined lock for local |
| 9.6 | Privacy-level gating | `api/routers/agents.py:77-92` | Hide addresses when `privacy_level` is `high`/`paranoid` |
| 9.7 | HMAC secret rotation | `db/repository.py:38-41` | Support `API_KEY_HMAC_SECRET_PREVIOUS` env var |
| 9.8 | Monero RPC host validation | `sthrip/config.py:36-37` | Validate against private IP ranges at startup |
| 9.9 | Audit log address redaction | `api/routers/agents.py:182-189` | Truncate addresses to `addr[:8] + "..."` |

---

## Phase 10: Test Coverage Gaps (TEST-1 through TEST-7)

**Estimated complexity: HIGH | Session: ~3 hours**
**Dependencies: Phases 1-9**

| Step | Issue | Fix |
|------|-------|-----|
| 10.1 | MCP test import crash | Add `pytest.importorskip("mcp")` to `tests/test_mcp_tools.py` |
| 10.2 | Concurrent tests skipped | Add PostgreSQL CI job with `postgres:15` service |
| 10.3 | Webhook router 41% | New `tests/test_webhook_router.py` — list, retry, auth, 404 |
| 10.4 | Agent endpoints 0 tests | New `tests/test_agent_endpoints.py` — profile, discover, leaderboard, settings |
| 10.5 | Lifespan 29% | Extend `tests/test_lifespan.py` — startup modes, graceful shutdown |
| 10.6 | Fixture duplication | Extract shared fixtures to `tests/conftest.py` |
| 10.7 | Targeted gap tests | New `tests/test_repository_gaps.py` — self-payment, admin stats, uncovered methods |

---

## Testing Strategy

| Phase | Test Files | Type |
|-------|-----------|------|
| 1 | `tests/test_withdrawal_recovery.py` | Unit |
| 2 | `tests/test_config_centralized.py` | Unit |
| 3 | `tests/test_admin_auth.py` (new), `tests/test_audit_logger.py` | Integration |
| 4 | `tests/test_balance.py`, `tests/test_idempotency.py` | Unit |
| 5 | `tests/test_webhook_service.py`, `tests/test_deposit_monitor.py` | Integration |
| 6 | `tests/test_agent_endpoints.py` (new) | Integration |
| 7 | `tests/test_middleware.py`, `tests/test_schemas.py` | Unit |
| 8 | `tests/test_url_validator.py`, `tests/test_fee_collector.py` | Unit |
| 9 | `tests/test_singleton_thread_safety.py` | Unit |
| 10 | Multiple new test files | Unit + Integration |

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Phase 1 withdrawal recovery changes break existing flow | Add `needs_review` status alongside existing; no data migration |
| Phase 2 stagenet config breaks Railway deployment | Set env vars on Railway BEFORE deploying config change |
| Phase 3 admin auth breaks existing API consumers | 30-day deprecation: accept both raw header and bearer token |
| Phase 5 webhook session split race conditions | Event ID is correlation key; splitting read/write is safe |
| Phase 8 DateTime migration causes downtime | `ALTER COLUMN TYPE TIMESTAMPTZ` is instant on PostgreSQL |

## Success Criteria

- [ ] All 5 CRITICAL issues fixed and tested
- [ ] All 13 HIGH issues fixed and tested
- [ ] All 13 MEDIUM issues fixed and tested
- [ ] All 9 LOW issues addressed
- [ ] All 7 TEST gaps filled
- [ ] Existing 600 tests still pass
- [ ] Coverage remains >= 80%
- [ ] No new security findings in subsequent audit
- [ ] Railway deployment succeeds with new config requirements
