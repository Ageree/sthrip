# Sprint 1 Contract — Auto-purge + Warrant Canary

> Phase 1 — Data minimization. Pre-filled by Lead from product-spec.md.
> Generator implements; independent Evaluator verifies against this contract.

## What Generator will build

### A. Purge service

1. **`sthrip/services/purge_service.py`** with functions:
   - `purge_transactions(retention_days: int) -> int` — deletes `transactions` rows where `created_at < now - retention_days` AND `status in {COMPLETED, EXPIRED, CANCELLED}` AND no FK reference from active rows. Returns deleted count.
   - `purge_escrow_deals(retention_days: int) -> int` — same logic for `escrow_deals` (terminal status only).
   - `purge_escrow_milestones(retention_days: int) -> int` — same for `escrow_milestones`.
   - `purge_message_relays(retention_days: int) -> int` — relay records older than retention, no status guard (relays don't have terminal state).
   - `purge_audit_log(retention_days: int) -> dict` — see B for HMAC chain rolling reset.
   - `run_full_purge(retention_days: int) -> dict` — orchestrator that runs all of the above, writes `purge_metadata` row, returns summary.

2. **Env validation**: `STHRIP_DATA_RETENTION_DAYS` (default 60), validated 7-365 at startup. Out-of-range → ValueError on startup.

### B. HMAC chain rolling reset

When purging audit_log:
   - Identify the HEAD (latest non-purged) row that survives purge.
   - Insert a synthetic "RESET_GENESIS" event row with `prev_hmac = NULL` and `event_type = "chain_reset"`.
   - All purged rows are deleted.
   - New events compute HMAC chained from this new genesis.
   - Verify chain: `verify_chain()` must return `valid=True` after reset (the chain restarts from the reset row, prior history is intentionally inaccessible).

### C. Warrant canary

1. **`sthrip/services/canary_service.py`**:
   - `generate_canary_payload(now: datetime) -> dict` — `{date, status: "no_subpoena_no_compromise", message, last_signed_at}`.
   - `sign_canary(payload: dict, signing_key: bytes) -> dict` — Ed25519 detached signature over canonical JSON, attaches `signature_b64`.
   - `publish_daily_canary()` — cron entry point: generate, sign, persist to `canary_state` table or `purge_metadata`-like store with current timestamp.
   - `get_current_canary() -> dict | None` — reads latest canary; if `last_signed_at` > 48h ago → returns None (signals staleness).

2. **`CANARY_SIGNING_KEY`** env: base64-encoded Ed25519 private key (32 bytes raw). On startup, decode and validate length.

### D. Endpoint

- **`api/routers/wellknown.py`** (or extend existing if present): `GET /.well-known/canary.txt`
  - If canary fresh (< 48h) → `200 application/json` returning signed canary JSON.
  - If stale → `503 Service Unavailable` with explicit `{"error": "canary_stale", "last_signed_at": ...}`.

### E. Scheduler entry

- Add daily 03:00 UTC entry to existing `scheduler.py` (APScheduler) or wherever `escrow_resolution` cron lives. Purge runs at 03:00 UTC, canary at 03:05 UTC (sequential to avoid contention).

### F. Migration

- **`migrations/versions/w4x5y6z7a8b9_purge_metadata.py`** — adds `purge_metadata` table:
  - `id BIGSERIAL PK`
  - `run_at TIMESTAMPTZ NOT NULL`
  - `transactions_deleted INTEGER NOT NULL DEFAULT 0`
  - `escrow_deals_deleted INTEGER NOT NULL DEFAULT 0`
  - `escrow_milestones_deleted INTEGER NOT NULL DEFAULT 0`
  - `message_relays_deleted INTEGER NOT NULL DEFAULT 0`
  - `audit_log_deleted INTEGER NOT NULL DEFAULT 0`
  - `audit_chain_reset_at_id BIGINT NULL` — references the synthetic reset row id
  - Index on `run_at DESC`.
- Adds `canary_state` table: `id`, `signed_at`, `payload_json`, `signature_b64`. Single-row pattern (upsert on id=1) OK.
- Migration round-trip: `alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head` succeeds.

## Specific testable acceptance criteria

Tests in `tests/test_purge_service.py` and `tests/test_canary_service.py`:

1. **`test_purge_deletes_old_transactions`** — seed 100 transactions, 50 with `created_at = now - 70 days` and terminal status, 50 with `created_at = now - 10 days`. Run `purge_transactions(60)`. Assert exactly 50 deleted, the recent 50 remain.

2. **`test_purge_respects_active_references`** — seed 1 old transaction in COMPLETED status BUT referenced by an active (non-terminal) escrow_deal. Run purge. Assert transaction NOT deleted (FK protection).

3. **`test_purge_skips_non_terminal_status`** — seed an old PENDING transaction (>60 days). Run purge. Assert NOT deleted.

4. **`test_chain_rolling_reset_keeps_new_chain_valid`** — seed 10 audit_log rows old enough to be purged. Run `purge_audit_log(60)`. Then write 3 new audit events. Call `verify_chain()` → returns `valid=True`. The 3 new events chain from the reset row.

5. **`test_canary_signature_verifies`** — generate canary, sign, then verify with the public key from same Ed25519 key. Result True.

6. **`test_canary_signature_rejects_tampering`** — flip a byte in payload, verify with original signature → False.

7. **`test_canary_endpoint_503_when_stale`** — mock `last_signed_at = now - 49h`, GET `/.well-known/canary.txt` → 503.

8. **`test_canary_endpoint_200_when_fresh`** — mock `last_signed_at = now - 1h`, GET → 200, body has `date`, `status`, `signature_b64`.

9. **`test_run_full_purge_writes_metadata_row`** — call `run_full_purge(60)`, assert one row in `purge_metadata` with non-zero summed deletes.

10. **`test_retention_days_env_validation`** — set `STHRIP_DATA_RETENTION_DAYS=5`, expect ValueError at startup. Set `=400`, expect ValueError. Set `=60`, OK.

## How success is verified

Evaluator runs:

```bash
cd /Users/saveliy/Documents/Agent\ Payments/sthrip
source .venv/bin/activate
alembic upgrade head
alembic downgrade -1
alembic upgrade head
pytest tests/test_purge_service.py tests/test_canary_service.py -v
pytest tests/ -x -q   # full suite must remain green
```

All criteria above must pass. No regressions in existing 1518+ test suite.

## Risk callouts (Generator must address)

- **HMAC chain rolling reset**: Generator MUST run `gitnexus_impact` on existing chain functions in `audit_log` before editing. Read existing chain implementation (likely in `sthrip/services/audit_service.py` or similar). Document chain reset rationale in code comments.
- **FK protection**: SQL must check active references before delete. Use `NOT EXISTS` subquery or explicit JOIN. Test (#2) verifies.
- **Migration idempotency**: per memory `feedback_postiz_railway_port_trap.md`-style lessons, use `IF NOT EXISTS` / `IF EXISTS` for tables.
- **Scheduler integration**: existing `scheduler.py` likely uses APScheduler. Don't break existing escrow resolution job.

## Out of scope (Sprint 1)

- Commission, subscription, billing — those are Sprints 2/3/4.
- Admin revenue dashboard — Sprint 4.
- TEE — Sprints 5-7.

## Branch and commit

- Branch: `feat/revenue-and-tee` (already created).
- Single commit at end of sprint: `feat(privacy): auto-purge + warrant canary (Phase 1 Sprint 1)`.
- Commit MUST include: code, migration, tests, updates to `THREAT_MODEL.md` noting "Phase 1 Sprint 1: data minimization via 60-day rolling purge".
