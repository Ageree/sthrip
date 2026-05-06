# Lead Decision: Split Sprint 4

Sprint 4 as originally planned bundles "read cutover" + "drop plaintext FK columns" + "real keystore deploy" into one CRITICAL destructive change against live mainnet. That violates the constraint "не сломать prod" if any one part has a bug.

## Decision: split into 4a (this loop) and 4b (later)

### Sprint 4a (THIS sprint — non-destructive)
- Add **dual-read** code path: read paths try envelope first; if envelope is null/decrypt-fails, fall back to plaintext FKs. No data loss possible if envelope is bad.
- Implement **backfill cron + script**: `scripts/backfill_payment_envelope.py` that reads existing rows missing envelope, computes envelope, writes back. Rerun-safe. Skips rows where envelope already present.
- Add **feature flag** `STHRIP_READ_FROM_ENVELOPE` (default `false`). When `false`, reads use plaintext FKs (existing behaviour). When `true`, reads use envelope-with-fallback. Operators flip the flag in staging first, then prod after smoke test.
- Admin views (`api/admin_ui/views.py`) gain a "decrypt with operator KEK" button; without operator KEK they show `participant=encrypted, amount=bucket`.
- Stub keystore continues to be the default. **Do not** deploy real `sthrip-op-keystore` Railway service in this sprint.
- **No FK column drop. No destructive migrations.**

### Sprint 4b (future sprint — destructive, blocked on 4a verification)
- Real `RemoteKeystore` implementation (replace `NotImplementedError`).
- Deploy `sthrip-op-keystore` Railway service.
- Run backfill cron in prod, verify all rows have envelope.
- Flip `STHRIP_READ_FROM_ENVELOPE=true` in prod, monitor 24h.
- Only then drop plaintext FK columns (`from_agent_id`, `to_agent_id`, `buyer_id`, `seller_id`, `amount`).

## Why split

The `feat/anonymity-hardening` branch must be mergeable per-commit without breaking prod. Sprint 4a satisfies that — every change is additive or feature-flagged off. Sprint 4b is a destructive operation that requires staging dry-run + operator coordination, which is a separate engineering action, not a code-only change.

## Implications for Generator

This sprint is now **MEDIUM risk, not CRITICAL** — because nothing is destroyed.

Generator's scope: feature-flag-gated dual-read + backfill cron + admin redacted view. Tests: dual-read fallback works both ways, backfill is rerun-safe, feature flag respected.
