# Sprint 1 Contract: Audit-log IP scrubbing + request_body allowlist

Spec: `product-spec.md` §Sprint 1 (lines 266-294), AD-1 (lines 77-93), AD-6 (lines 163-178).
AC source: `user-criteria.md` AC #1.

## What I will build

### New files
- `sthrip/services/ip_salt_service.py` — module providing:
  - `current_ip_salt(db)` — return `(salt_id: UUID, secret: bytes)` for the active (non-retired) salt; bootstrap if empty
  - `rotate_ip_salt(db)` — create new active salt, retire any salt older than `2 * IP_SALT_ROTATION_DAYS`
  - `compute_ip_hmac(ip: str, salt_secret: bytes) -> bytes` — HMAC-SHA256 over UTF-8 IP, returns 32-byte digest
  - `IP_SALT_ROTATION_DAYS` resolved from env (default 7, range 1-30)
- `tests/test_audit_log_ip_hmac.py` — unit + migration tests for the new module + audit_logger ip_hmac path

### Modified files
- `sthrip/db/models.py` — add `IpSalt` model + `AuditLog.ip_hmac` (LargeBinary 32) + `AuditLog.ip_salt_id` (FK→ip_salts.id) columns. The legacy `ip_address` column is dropped in this same migration (AD-1).
- `sthrip/services/audit_logger.py`:
  - `log_event(...)` keeps existing signature (`ip_address: Optional[str]`); now hashes the IP into ip_hmac+ip_salt_id internally before persistence
  - `_hash_chain_link(... ip=...)` is renamed argument-wise to feed the **hex-encoded ip_hmac string** (preserves chain canonicalization without a structural break)
  - `_SENSITIVE_KEYS` blocklist removed → `_AUDIT_REQUEST_BODY_ALLOWLIST: dict[str, frozenset[str]]` allowlist (default deny). Unknown actions → `request_body=None`.
  - `_sanitize` retained for nested redaction inside allowlisted values, but only allowlisted keys per action survive.
  - `verify_chain` reads `ip_hmac` (hex-encoded) when recomputing.
- `migrations/versions/q8r9s0t1u2v3_audit_ip_hmac.py` — new migration (described below).

### Migration (`q8r9s0t1u2v3_audit_ip_hmac.py`)
1. Create `ip_salts(salt_id UUID PK default gen_random_uuid(), secret BYTEA NOT NULL, created_at TIMESTAMPTZ default now(), retired_at TIMESTAMPTZ NULL)` — IF NOT EXISTS for idempotency.
2. Insert one bootstrap salt row (deterministic UUID `00000000-0000-0000-0000-000000000001` so re-running migration is idempotent; secret = `os.urandom(32)` only on first run).
3. Add `audit_log.ip_hmac BYTEA NULL`, `audit_log.ip_salt_id UUID NULL` (FK to ip_salts.salt_id).
4. Acquire `pg_advisory_lock(0x5374687269705F49)` ("Sthrip_I").
5. Backfill: for each `audit_log` row with non-null `ip_address` AND non-null `entry_hmac` → compute `ip_hmac = HMAC(bootstrap_secret, ip_address.encode())`; recompute `entry_hmac` using `ip_hmac.hex()` instead of raw IP; UPDATE row.
6. Drop `audit_log.ip_address` column (IF EXISTS).
7. Set bootstrap salt `retired_at = now()` so future writes pick a fresh one (which `current_ip_salt` will lazily create).
8. Release advisory lock.
9. Run `verify_chain` smoke check at end (raises and aborts deploy on failure).

`downgrade()` drops the new columns and table; cannot restore raw IPs (data destroyed by design).

## Specific testable acceptance criteria

1. **Schema: ip_hmac column exists, ip_address column removed**
   - Verified by `pytest tests/test_audit_log_ip_hmac.py::test_audit_log_schema_has_ip_hmac_not_ip_address`
2. **HMAC determinism within salt window**
   - Same IP + same salt secret → byte-identical hmac (32 bytes)
   - `pytest tests/test_audit_log_ip_hmac.py::test_compute_ip_hmac_is_deterministic_with_same_salt`
3. **HMAC separation across rotation**
   - Same IP + different salt secret → different hmac
   - `pytest tests/test_audit_log_ip_hmac.py::test_rotation_produces_different_hmac_for_same_ip`
4. **log_event no longer persists raw IP**
   - After `log_event(..., ip_address="203.0.113.42")` the resulting AuditLog row has `ip_hmac is not None` and the old `ip_address` attribute is absent (or never set on model)
   - `pytest tests/test_audit_log_ip_hmac.py::test_log_event_writes_hmac_not_raw_ip`
5. **Request-body allowlist: known action filters keys**
   - For action `"agent.registered"` (allowlist `{agent_name}`), passing `details={"agent_name":"a","webhook_url":"b","amount":"1"}` → persisted `request_body == {"agent_name":"a"}`
   - `pytest tests/test_audit_log_ip_hmac.py::test_allowlist_filters_disallowed_keys`
6. **Request-body allowlist: unknown action defaults to None**
   - Passing `action="unmapped.action"` with non-empty details → persisted `request_body is None` (no raise)
   - `pytest tests/test_audit_log_ip_hmac.py::test_allowlist_unknown_action_defaults_to_none`
7. **Salt rotation: new active salt, old retired**
   - After `rotate_ip_salt`: previously active salt is retired (retired_at set), `current_ip_salt` returns new salt, only one row has `retired_at IS NULL`
   - `pytest tests/test_audit_log_ip_hmac.py::test_rotate_ip_salt_retires_old_creates_new`
8. **Old salts beyond 2× rotation window are destroyed (secret zeroed)**
   - `rotate_ip_salt` deletes (or zeroes secret of) salts older than `2 * IP_SALT_ROTATION_DAYS`
   - `pytest tests/test_audit_log_ip_hmac.py::test_rotate_destroys_salts_beyond_double_window`
9. **Migration upgrade then downgrade works**
   - `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` exits 0
   - `pytest tests/test_audit_log_ip_hmac.py::test_migration_round_trip`
10. **Chain integrity preserved post-migration**
    - Seed audit_log row pre-migration, run upgrade, call `verify_chain` → `ChainStatus.ok == True`
    - `pytest tests/test_audit_log_ip_hmac.py::test_chain_remains_valid_after_migration_backfill`
11. **HMAC chain link uses ip_hmac (hex) in canonical message**
    - `pytest tests/test_audit_log_ip_hmac.py::test_hash_chain_link_consumes_hex_hmac`
12. **Coverage on changed modules ≥80%**
    - `pytest --cov=sthrip.services.audit_logger --cov=sthrip.services.ip_salt_service --cov-fail-under=80 tests/test_audit_log_ip_hmac.py tests/test_audit_logger.py`

## How success is verified

- `pytest tests/ -x` — exit 0 (existing 2221+ tests stay green)
- `pytest --cov=sthrip.services.audit_logger --cov=sthrip.services.ip_salt_service --cov-fail-under=80 tests/test_audit_log_ip_hmac.py tests/test_audit_logger.py` — exit 0
- `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` — exit 0 (round-trip)
- `verify_chain` returns `ok=True` after migration backfill (asserted in test #10)
- Manual: `grep -rn "ip_address" sthrip/services/ migrations/` shows only legacy backfill code in the new migration; no live writes to `ip_address`.

## Out of scope for this sprint

- **Salt rotation cron/scheduler wiring in production** — the rotation function exists with tests; scheduler hookup is deferred to Sprint 1b or Sprint 7 ops PR.
- **Removing `ip_address` argument from public `log_event` signature** — kept as `ip_address: Optional[str]` to preserve the ~52 call-sites' API; the function hashes it internally. Renaming the parameter would touch every router and service file (CRITICAL impact per gitnexus).
- **Per-action allowlist coverage audit** — we ship allowlists for the actions actually used today (enumerated by grepping `audit_log\(` in api/ and sthrip/services/); a future sprint can add the "every action must have an allowlist or a unit test fails" lint.
- **Operator KEK custody / payment-graph encryption** — Sprints 3-4.

## GitNexus impact analysis

### `log_event` (upstream)
- **Risk: CRITICAL** — 52 direct callers, 25 affected processes, 4 modules.
- **Mitigation:** keep public signature stable (`ip_address: Optional[str]`). Hashing happens inside the function before persistence. No call-site changes required.

### `_hash_chain_link` (upstream)
- **Risk: CRITICAL** — 2 direct callers (`_write_with_chain`, `verify_chain`), but 26 processes affected transitively.
- **Mitigation:** keep parameter shape (`ip: str`). New code passes `ip_hmac.hex()` instead of the raw IP. Computation is identical, just over a hash digest.

### `AuditLog` (class)
- **Risk: CRITICAL** — 71 direct importers (every router + service + repo).
- **Mitigation:** column-level change only (rename/replace `ip_address` → `ip_hmac`). No imports break; only the migration touches stored data. Code that did `entry.ip_address = ...` exists only in `_write_with_chain` (audit_logger.py) and the migration backfill — both are updated in this sprint.

### Risk acknowledgement
- WILL BREAK at d=1 if signature changes — explicitly avoided.
- Test path: `pytest tests/ -x` after each implementation step, plus targeted module test.
- Rollback: `alembic downgrade -1` reverses the schema; raw IPs are not restored (data destruction is by design per AD-1).
