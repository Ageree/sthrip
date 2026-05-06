# Sprint 4a Generator Report

## Status: READY FOR REVIEW

All contract acceptance criteria pass. Zero regressions on the Sprint 3 baseline.
NOT committed yet — awaiting Evaluator pass.

## What I built

**New module**
- `sthrip/services/payment_envelope_reader.py` (~242 lines)
  - `feature_flag_enabled()` — reads `STHRIP_READ_FROM_ENVELOPE` (default false)
  - `read_payload_or_none(row)` — pure decrypt, never raises
  - `read_with_fallback(row)` — returns frozen `ReadResult` with one of
    `flag_off | fallback_envelope_null | fallback_decrypt_error | envelope`
  - `apply_envelope_to_row(row)` — in-place mutation when envelope wins,
    no-op otherwise

**Repo modifications (additive, signature-preserving)**
- `sthrip/db/transaction_repo.py` — `get_by_hash`, `list_by_agent`
- `sthrip/db/escrow_repo.py` — `get_by_id`, `get_by_hash`, `list_by_agent`,
  `get_pending_expiry`
- `sthrip/db/milestone_repo.py` — `get_by_escrow_and_sequence`, `get_by_escrow`

Each read method now post-processes returned rows through
`apply_envelope_to_row`. With the flag off (default), rows are returned
verbatim — observable behaviour is byte-identical to Sprint 3.

**Backfill script**
- `scripts/backfill_payment_envelope.py` (~265 lines)
  - 4 table specs: `transactions`, `escrow_deals`, `escrow_milestones`,
    `message_relays`
  - SQL filter `WHERE participant_envelope IS NULL` keeps it idempotent
  - Per-batch commits (default 500), `--batch-size`, `--table`, `--dry-run`
  - Milestones resolve buyer/seller from the parent EscrowDeal in a
    bounded pre-fetch (one query per batch)

**Admin redaction**
- `api/admin_ui/views.py`:
  - `_keystore_available()` — probes the keystore; `False` when
    RemoteKeystore raises (Sprint 4b prod mode pre-deploy)
  - `_redact_participant`, `_redact_amount` helpers
  - `_serialize_escrow(deal, redacted=...)` — when redacted,
    `buyer_id`/`seller_id`/`description` → `"encrypted"`,
    `amount` → `amount_bucket` or `"redacted"`. Auto-probe when
    `redacted` is None.

## Test results

| File | Count | Status |
|---|---|---|
| `tests/test_payment_envelope_reader.py` | 22 | PASS |
| `tests/test_backfill_envelope.py` | 7 | PASS |
| `tests/test_admin_redacted_view.py` | 11 | PASS |
| `tests/test_repo_dual_read.py` | 9 | PASS |
| **Total NEW tests** | **49** | **49/49 PASS** |
| Pre-existing `tests/test_payment_envelope.py` | 8 | PASS (unchanged) |

**Coverage**: `sthrip/services/payment_envelope_reader.py` = **91%**
(96 stmts, 9 missed; well above the 80% bar).

**Full repo run** (excluding `test_channels.py`, `test_mcp_auth.py`,
`test_cli_client.py`, `test_cli_commands.py` which require a missing
`respx` test dep that pre-dates this sprint):
- 2682 passed
- 24 failed — all 24 verified to fail on the Sprint 3 baseline (commit
  9eb2eca) with the same error signatures, confirmed via `git stash`
  before/after comparison. **Zero new regressions introduced by 4a.**

## Acceptance criteria coverage

| # | Criterion | Test |
|---|---|---|
| 1 | Reads unchanged when flag off | `test_transaction_reads_unchanged_when_flag_off`, `test_escrow_reads_unchanged_when_flag_off`, `test_milestone_reads_unchanged_when_flag_off` |
| 2 | Reads use envelope when flag on | `test_*_reads_use_envelope_when_flag_on` (3 tests) |
| 3 | Fallback when envelope null | `test_transaction_reads_fallback_when_envelope_null`, `test_escrow_reads_fallback_when_envelope_null` |
| 4 | Fallback when decrypt fails | `test_transaction_reads_fallback_when_decrypt_fails`, `test_read_with_fallback_wrong_key_falls_back` |
| 5 | Backfill rerun-safe | `test_backfill_idempotent` |
| 6 | Backfill skips existing | `test_backfill_skips_existing` |
| 7 | Backfill 4 tables | `test_backfill_covers_all_tables` |
| 8 | Admin redacted when no KEK | `test_admin_view_redacted_when_keystore_unavailable`, `test_admin_view_auto_probes_keystore` |
| 9 | Admin full when KEK present | `test_admin_view_full_when_keystore_available`, `test_admin_view_auto_probes_stub_full` |
| 10 | ≥80% reader coverage | 91% measured |
| 11 | No regressions | 2682 vs baseline; 24 pre-existing failures unchanged |

## Manual verification

```
$ DATABASE_URL="sqlite:////tmp/sthrip_smoke.db" \
  STHRIP_HUB_KEK="..." OP_KEYSTORE_MODE=stub \
  python scripts/backfill_payment_envelope.py --dry-run

backfill complete:
  transactions: processed=0 skipped=0
  escrow_deals: processed=0 skipped=0
  escrow_milestones: processed=0 skipped=0
  message_relays: processed=0 skipped=0
```

## GitNexus impact

`mcp__gitnexus__impact` confirms LOW risk for every modified read path:
- `list_by_agent` (Transaction): 1 d=1 caller, LOW
- `get_pending_expiry` (Escrow): 1 d=1 caller (`EscrowService.resolve_expired`),
  LOW (only mutates returned row contents, return type unchanged)
- `get_by_id` repo methods: ≤3 transitive callers, LOW

All edits preserve signatures and only mutate row contents when the new
flag is on AND envelope decrypts. With flag off (production default for
this sprint), behaviour is identical to commit 9eb2eca.

## Files touched

```
sthrip/services/payment_envelope_reader.py     NEW (242 lines)
scripts/backfill_payment_envelope.py            NEW (265 lines)
tests/test_payment_envelope_reader.py           NEW (286 lines)
tests/test_backfill_envelope.py                 NEW (250 lines)
tests/test_admin_redacted_view.py               NEW (180 lines)
tests/test_repo_dual_read.py                    NEW (231 lines)
.harness/anonymize-platform/sprint-4a-contract.md NEW
.harness/anonymize-platform/sprint-4a-generator-report.md NEW (this file)
.harness/anonymize-platform/state.json          MODIFIED
sthrip/db/transaction_repo.py                   MODIFIED (+15 lines)
sthrip/db/escrow_repo.py                        MODIFIED (+24 lines)
sthrip/db/milestone_repo.py                     MODIFIED (+14 lines)
api/admin_ui/views.py                           MODIFIED (+45 lines)
```

## Risks for Sprint 4b

Sprint 4a is dual-read with feature-flag-off-by-default. To get the AC #2
guarantee (admin can NOT decrypt with ADMIN_API_KEY alone), Sprint 4b
must:

1. **Deploy `sthrip-op-keystore` Railway service** with its own ACL and
   no `DATABASE_URL` injection. Implement `RemoteKeystore.unwrap_dek`
   over a private-network HTTPS round-trip using a service-only API key.
2. **Run backfill in prod** as a one-shot Railway cron. Verify
   `SELECT count(*) FROM transactions WHERE participant_envelope IS NULL`
   returns 0 before proceeding to step 3 (analogous query for the other
   3 tables).
3. **Flip `STHRIP_READ_FROM_ENVELOPE=true`** in prod, monitor 24h with
   alerting on `fallback_decrypt_error` log lines (we already log a
   warning per failed decrypt; ops must wire that into Loki/Grafana).
4. **Drop FK columns** (`from_agent_id`, `to_agent_id`, `buyer_id`,
   `seller_id`, plaintext `amount`, plaintext `description`/`memo`) only
   after step 3 has been clean for 24h. Rollback path: restore from
   backup; the schema migration must be reversible.
5. **Switch `OP_KEYSTORE_MODE=remote`** in hub Railway env. Until this
   step the admin redacted view will still show full data because the
   stub KEK is in-process.

If 4b reverses any of those steps the AC #2 invariant is at risk:
- Skipping step 2 → flag flip surfaces "row missing envelope" warnings
  in prod, but reader degrades to FK fallback so users don't notice; ops
  alert fires.
- Skipping step 5 → admin view stays "full" even after FK drop because
  the stub KEK is still present; user-criteria #2 is technically violated
  until remote mode is wired.
- Skipping step 3 → the 24h soak is the only opportunity to catch
  envelope-vs-FK divergence (Sprint 3 dual-write was best-effort; some
  legacy rows could have a corrupt envelope that decrypt_envelope
  silently rejects).

## Out of scope for 4a (delivered to 4b)

- No FK drops, no destructive migrations
- No real `RemoteKeystore` implementation
- No Railway service deploy
- No production flag flip
- No mainnet backfill execution
