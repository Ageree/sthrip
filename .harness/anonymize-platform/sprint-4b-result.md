# Sprint 4b Evaluator Result

## VERDICT: PASS

Sprint 4b ships clean, safe-by-default CODE that prepares the payment-graph
privacy cutover without arming it. The destructive migration is
demonstrably impossible to run accidentally; the RemoteKeystore implementation
is correct; no Sprint 1–7 regression introduced. Operator owns the actual
deploy + flag flips per the contract.

## Lead summary (≤200 words)

VERDICT: **PASS**.

**Safe-by-default migration confirmed.** I imported `v3w4x5y6z7a8` and called
`upgrade()` directly with `STHRIP_DROP_LEGACY_FK` unset → `RuntimeError`
raised on line 1 of `upgrade()`, before `op.get_bind()` or any DDL. The
error message names the env var, the keystore prereq, the backfill, and
the 24h soak (596 chars, all required strings present). Falsy variants
(`""`, `"false"`, `"no"`, `"0"`, `"off"`) all parametrically tested as
"refuse". Only `1/true/yes/on` pass `_flag_enabled()`. Idempotent via
inspector probe per column; SQLite handled via `batch_alter_table`.

**RemoteKeystore correctness.** Bearer token in `Authorization` header
(httpx default), missing token → clear `RuntimeError`, default URL points
at Railway internal network (port 8000), 5s timeout, non-200 raises with
truncated body, `get_kek_for_envelope()` raises by design (hub never sees
`KEK_OP` plaintext). `OP_KEYSTORE_MODE=stub` remains default. KEK_OP only
appears in docstrings on hub side.

**op-keystore server.** Constant-time bearer compare via `hmac.compare_digest`,
fresh 12-byte nonce per wrap, generic "unwrap failed" on auth-tag failure,
unprivileged user, no DB attached.

**Tests.** 34/34 Sprint 4b passing, 89% coverage (>80%). 24 regression
failures verified pre-existing on Sprint 7 baseline (`e6ef31b`) by stashing.

## Verification details

### Migration safety pen-test (the catastrophic-fail check)

Direct invocation in process with flag unset:

```
flag enabled? False
FLAG_ENV: STHRIP_DROP_LEGACY_FK
OK: RuntimeError raised
  mentions STHRIP_DROP_LEGACY_FK: True
  mentions keystore prereq: True
  mentions backfill: True
  mentions soak/24h: True
  message length: 596
```

`upgrade()` source (literal):

```python
def upgrade() -> None:
    if not _flag_enabled():
        raise RuntimeError(_PREREQ_MESSAGE)

    conn = op.get_bind()
    insp = sa_inspect(conn)
    ...
```

The flag check is the first statement. `op.get_bind()` is never called
without the flag, so even import side effects of alembic cannot trigger
DDL. Falsy strings tested parametrically (`test_fk_drop_migration_treats_falsy_as_unset` on `""`, `false`, `no`, `0`, `off`) — all PASS.

`_DROP_PLAN` covers all 4 tables exactly per contract:
- `transactions.from_agent_id, to_agent_id, amount`
- `escrow_deals.buyer_id, seller_id, amount`
- `escrow_milestones.amount`
- `message_relays.from_agent_id, to_agent_id`

`_RESTORE_PLAN` re-adds nullable columns best-effort on downgrade.

### RemoteKeystore code review

| Requirement | Status |
| --- | --- |
| `Authorization: Bearer <token>` header (not in URL/body) | OK — set in `httpx.Client(headers=...)` line 145–148 |
| Missing token → clear `RuntimeError` | OK — line 138–143, message names both ends |
| `OP_KEYSTORE_URL` configurable, defaults to internal network | OK — line 108, 136 |
| 5s timeout | OK — `_REMOTE_TIMEOUT_SECONDS = 5.0`, line 112 |
| Non-200 raises with status code + truncated body | OK — line 161–166, body sliced to `[:200]` |
| `get_keystore()` factory honors `OP_KEYSTORE_MODE` | OK — `stub` default at line 214, `remote` returns `RemoteKeystore` |
| `get_kek_for_envelope()` on remote raises by design | OK — line 196–202 |
| `KEK_OP` not read from hub env | OK — only docstring matches in `sthrip/`, `api/` |
| No hardcoded auth tokens | OK — only `os.environ.get` reads + docstrings |

### Reader graceful field access

`_row_fallback` uses `getattr(row, "from_agent_id", None)` etc. for every
column the post-cutover schema may lack (line 170–183). When all sources
empty, `_is_empty_fallback` triggers `fallback_no_data` — correctly used
in all four branches of `read_with_fallback`. `apply_envelope_to_row`
guards every assignment with `hasattr` — safe on schemas where columns
have been dropped.

### op-keystore-deploy artifacts

| Requirement | Status |
| --- | --- |
| `KEK_OP_BASE64` + `AUTH_TOKEN` required | OK — `_load_kek` and `_load_auth_token` raise `RuntimeError` if unset |
| KEK length validated (32 bytes) | OK — `len(kek) != _DEK_LEN` check |
| AESGCM 12-byte nonce, fresh per call | OK — `os.urandom(_NONCE_LEN)` per `/wrap` |
| Bearer auth on `/wrap` and `/unwrap` | OK — `_verify_auth` called first |
| Constant-time token compare | OK — `hmac.compare_digest` line 105 |
| `/health` available without auth | OK — no `_verify_auth` call |
| Generic error on unwrap auth-tag fail | OK — "unwrap failed" without echoing tag detail |
| Body bytes never logged | OK — only lengths logged |
| Unprivileged container user | OK — `useradd keystore` + `USER keystore` in Dockerfile |
| Minimal image | OK — slim base, pinned versions, no compilers |

### Tests run

```
pytest tests/test_remote_keystore.py tests/test_fk_drop_migration.py \
       tests/test_reader_after_fk_drop.py -v
→ 34 passed in 0.40s

pytest tests/test_op_keystore_server.py tests/test_operator_keystore.py -v
→ 18 passed in 0.25s

pytest --cov=sthrip.services.operator_keystore \
       --cov=sthrip.services.payment_envelope_reader \
       --cov-fail-under=80 \
       tests/test_remote_keystore.py tests/test_reader_after_fk_drop.py \
       tests/test_operator_keystore.py
→ 33 passed
   operator_keystore.py: 98% (91 stmts, 2 miss)
   payment_envelope_reader.py: 82% (107 stmts, 19 miss)
   TOTAL: 89.39% (above 80 floor)

pytest tests/ --ignore=tests/test_channels.py --ignore=tests/test_mcp_auth.py \
              --ignore=tests/test_cli_client.py --ignore=tests/test_cli_commands.py
→ 2785 passed, 24 failed, 21 skipped
```

The 24 failures were verified PRE-EXISTING on Sprint 7 baseline by `git
stash --include-untracked` then re-running the same ignore set — same
24 failed. Zero new regressions from Sprint 4b.

`tests/test_cli_client.py` and `tests/test_cli_commands.py` collection
errors are pre-existing `ModuleNotFoundError: respx` (missing test dep),
not related to Sprint 4b.

### Acceptance criteria coverage (contract section "Specific testable")

All 13 ACs pass:

| AC | Test | Result |
| --- | --- | --- |
| 1. ctor without auth token raises | `test_remote_keystore_requires_auth_token` | PASS |
| 2. unwrap POSTs to `/unwrap` | `test_remote_unwrap_calls_endpoint` | PASS |
| 3. Bearer header set | `test_remote_auth_header_set` | PASS |
| 4. Non-200 → RuntimeError with status | `test_remote_unwrap_error_handling_non_200` | PASS |
| 5. mode=stub default | `test_get_keystore_returns_stub_by_default` | PASS |
| 6. mode=remote returns RemoteKeystore | `test_get_keystore_returns_remote_when_mode_remote` | PASS |
| 7. Migration aborts when flag unset | `test_fk_drop_migration_requires_flag` | PASS |
| 8. Migration drops all 4 tables' columns | `test_fk_drop_migration_drops_columns` | PASS |
| 9. Migration idempotent | `test_fk_drop_migration_idempotent` | PASS |
| 10. Reader copes after drop | `test_reader_handles_missing_fk_columns_via_getattr` | PASS |
| 11. fallback_no_data when both missing | `test_reader_no_envelope_no_fk_returns_fallback_no_data` | PASS |
| 12. Server wrap/unwrap roundtrip | `test_keystore_server_wrap_unwrap_roundtrip` | PASS |
| 13. Server enforces bearer auth | `test_keystore_server_requires_auth` | PASS |

## Findings

### 🟢 None blocking.

### 💭 Minor observations (nits, not blocking)

1. `RemoteKeystore.get_kek_for_envelope()` raising in remote mode means
   the `read_payload_or_none` path (`get_keystore().get_kek_for_envelope()`
   at `payment_envelope_reader.py:109`) cannot succeed when
   `OP_KEYSTORE_MODE=remote`. This is the documented out-of-scope item
   ("rework `envelope_crypto` to use `wrap_dek`/`unwrap_dek`"); the
   exception is caught by the existing `except Exception` and degrades
   to `fallback_decrypt_error`. Worth a one-liner alert in operator
   runbook to NOT flip `OP_KEYSTORE_MODE=remote` until the helper rework
   ships — README already mentions this. No code change needed for 4b.
2. `RuntimeError` in `RemoteKeystore.__init__` is raised lazily on first
   `get_keystore()` call because of `@lru_cache`. If hot-loading a worker
   with `OP_KEYSTORE_MODE=remote` but no token, the failure surfaces at
   first request rather than startup. Acceptable for stub-default rollout
   but a follow-up "fail at boot" check would catch operator misconfig
   earlier. Not a blocker.
3. `payment_envelope_reader.py` line 178 uses `_coerce_decimal(getattr(row, "amount", None))`
   — `_coerce_decimal(None)` returns `None`, so safe. Verified in test
   `test_reader_handles_missing_fk_columns_via_getattr`.

### AC #2 (user-criteria) status

Sprint 4b is the **enabling step** for full AC #2 satisfaction. CODE
ships in disabled state:

- ENV invariants on hub: `OP_KEYSTORE_MODE=stub` by default, no `KEK_OP`
  read.
- Migration: gated on `STHRIP_DROP_LEGACY_FK` flag, refuses without it.
- Server: separate Railway artifact, no DB ACL needed.

Operator must (per contract, out-of-scope for Generator):
1. Deploy `sthrip-op-keystore` with `KEK_OP_BASE64` + `AUTH_TOKEN`.
2. Set `OP_KEYSTORE_AUTH_TOKEN` on API service.
3. Run backfill until `participant_envelope` is fully populated.
4. Set `STHRIP_READ_FROM_ENVELOPE=true`, soak 24h, verify zero
   `fallback_decrypt_error`.
5. Set `STHRIP_DROP_LEGACY_FK=true` and `alembic upgrade head`.

Until step 5, AC #2 reads "encrypted-at-rest done; cutover ready" — same
as before Sprint 4b code-wise, but now the cutover is one flag flip away
instead of three sprints away.

## Recommendation

Merge. CODE is safe-by-default and tested. Operator activation deferred
per Sprint 4b contract.
