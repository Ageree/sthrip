# Product Spec: Phase 2 — Revenue + TEE Hardening

> Plan to (a) automatically minimize stored data, (b) start revenue streams,
> and (c) protect the payment hot path with hardware-backed runtime
> encryption. Branch: `feat/revenue-and-tee` (or continuation of
> `feat/anonymity-hardening` per Lead decision in sprint 1).

## Architecture Decisions

### AD-1. Auto-purge: hard-delete with HMAC chain rolling reset

- **Decision**: ежедневный cron deletes records older than `STHRIP_DATA_RETENTION_DAYS` (default 60). Записей старше срока нет в БД — субпена против них = "no responsive records exist".
- **HMAC chain handling**: при удалении head'а audit_log chain — current strategy для F-11 chain'а ломается. Решение: **rolling chain reset**. Cron ставит "reset marker" — синтетическая audit_log row которая становится новой genesis для оставшихся. Old chain до reset'а удалена; новые links валидны от reset row forward.
- **FK protection**: `transactions` и `escrow_deals` могут ссылаться друг на друга. Purge только rows где status в {COMPLETED, EXPIRED, CANCELLED} И нет outstanding references из active rows. Active rows — never deleted.
- **Trade-off accepted**: chain integrity guarantee теряется при reset. Это intentional — privacy > forensic continuity. Document this in THREAT_MODEL.md.

### AD-2. Warrant canary: signed JSON + cron-driven freshness

- **Decision**: `/.well-known/canary.txt` returns JSON `{date, status, signature}`. Cron каждый день обновляет timestamp, signs с `CANARY_SIGNING_KEY` (dedicated Ed25519 key, отдельный от audit/webhook keys). Если cron не run за 48h — endpoint начинает 503'ить (сигнал "что-то пошло не так").
- **Signing key**: stored в Railway secret (not in TEE). Compromise of canary key alone не дешифрует payment graph; worst case — fake canary показывает OK когда реально subpoena.
- **Trade-off accepted**: canary в clearnet, не в Tor. Signal должен быть cheaply discoverable. Tor-only canary никто не проверяет.

### AD-3. Commission: deduct at write, accumulate to fee_collections

- **Decision**: в `transaction_repo.create_transaction(...)` после envelope encryption но до commit — compute fee = `amount * rate(tier)`, deduct from sender balance, добавить row в `fee_collections(agent_id_payer, amount, rate_applied, transaction_ref)`. Aggregation запросы для admin dashboard `/admin/revenue` суммируют MTD/YTD.
- **Tier rate lookup**: cached `agent.tier` в request context. Free → 0.003. Pro/Enterprise → 0.001.
- **Floor**: max(computed_fee, 1 piconero). Гарантирует ноль "free transactions" даже на dust.
- **Why deduct from sender, not skim from receiver**: Senders are the ones who initiate; receivers are passive. Skimming receiver looks like loss to receiving agent and discourages adoption. Sender deduct = "transparent fee", receiver gets full advertised amount.

### AD-4. Subscription billing: monthly cron with grace period

- **Decision**: cron на 1-е число каждого месяца reads agents with `tier=PRO` или `tier=ENTERPRISE`. For each — compute USD→XMR at current rate, attempt deduct from balance.
- **Insufficient balance**: don't fail-immediately. Set `tier_grace_until = now() + 7 days`, retain Pro features. Daily cron retries deduct. После 7 дней — `tier=FREE`, log audit event.
- **Mid-month upgrade**: pro-rate. Agent upgrades on day 15 → charge 50% of monthly. Reverse for downgrade с refund в balance.
- **Live rate**: cache 5-minute TTL from coingecko XMR/USD. Если rate API fails → use last cached rate up to 24h, then alert.

### AD-5. Tier enforcement: middleware-level rate limit

- **Decision**: FastAPI middleware checks `agent.tier` and current month transaction count. Free tier 100 → middleware returns 429 with body `{"error": "tier_limit_reached", "current_count": 101, "limit": 100, "upgrade_url": "/v2/me/upgrade"}`. Pro/Enterprise — bypass.
- **Counting**: `agent_monthly_stats` summary table updated by trigger (or repo wrap). Reset on month rollover.
- **Why middleware-level**: enforces uniformly across all payment endpoints; agent can't bypass by hitting different routes.

### AD-6. TEE migration: GCP Confidential VM with mTLS bridge

- **Decision**: новый GCP project `sthrip-tee`. One Confidential VM running payment service container (FastAPI subset с только payment endpoints). Railway main service proxies payment requests to GCP via mTLS gRPC.
- **Why VM, не Cloud Run**: Cloud Run Confidential — newer, less mature; VM gives full control + persistent memory state.
- **Why hybrid, not full migration**: most non-payment code (marketplace, admin, discovery) doesn't benefit from TEE; migration cost too high for marginal gain.
- **Attestation**: AMD SEV-SNP attestation report on each TEE boot signed by hub's static key, posted to `/.well-known/attestation.json`. SDK includes pinned image hash; mismatch → SDK refuses to send payment.
- **Cost estimate**: n2d-standard-2 Confidential VM ~$50-70/mo. Acceptable.

### AD-7. Feature flag for TEE cutover

- **Decision**: `STHRIP_PAYMENT_VIA_TEE=false` default. When false, payments handled in-process на Railway as today. When true, Railway proxies to GCP. Flip via env var, no redeploy.
- **Rollback**: instant. Set false, traffic resumes locally.
- **Trade-off**: while flag is `false`, TEE benefits don't apply. Operator must explicitly enable after staging dry run.

---

## Sprints

> 7 спринтов, разделённых по phases. Quick wins сначала, infra последним.

### Phase 1: Data minimization (1 sprint)

#### Sprint 1: Auto-purge + warrant canary

- **Цель**: старые записи удаляются автоматически; canary publishes daily.
- **Скоп**:
  - Add `purge_service.py` с функциями `purge_transactions(retention_days)`, `purge_audit_log(retention_days)`, `rolling_chain_reset()`.
  - Cron в `sthrip/services/scheduler.py` (или новый файл если нет существующего scheduler) — daily 03:00 UTC.
  - `STHRIP_DATA_RETENTION_DAYS` env (default 60, validated 7-365).
  - `canary_service.py` — daily publishes signed canary to `/.well-known/canary.txt`.
  - `CANARY_SIGNING_KEY` env (Ed25519 private key base64).
  - Migration `w4x5y6z7a8b9_purge_metadata.py` — adds `purge_metadata` table tracking last purge run + purged row counts.
- **Файлы**:
  - new `sthrip/services/purge_service.py`
  - new `sthrip/services/canary_service.py`
  - existing `sthrip/services/scheduler.py` (или новый)
  - new `api/routers/wellknown.py` — add canary endpoint
  - new `migrations/versions/w4x5y6z7a8b9_purge_metadata.py`
  - new `tests/test_purge_service.py`
  - new `tests/test_canary_service.py`
- **Тесты**:
  - `test_purge_deletes_old_transactions` — seed 100 rows, 50 older than 60d, run purge, assert 50 deleted
  - `test_purge_respects_active_references` — completed+referenced row not deleted
  - `test_chain_rolling_reset_keeps_new_chain_valid` — verify_chain returns ok после purge
  - `test_canary_signature_verifies` — verify ed25519 sig матчит published JSON
  - `test_canary_503_after_48h_stale` — mock time advance, endpoint returns 503
- **Risk**: MEDIUM. HMAC chain rolling reset — touchy. Independent Evaluator должен особо проверить chain integrity.
- **Migration round-trip**: yes, alembic up-down-up на test fixture.

### Phase 2: Revenue (3 sprints)

#### Sprint 2: Commission on transfers

- **Цель**: 0.3% Free / 0.1% Pro fee deducted at transfer time. Aggregation table populated.
- **Скоп**:
  - Migration `x5y6z7a8b9c0_fee_collections.py` — add `fee_collections` table если не существует (она уже частично есть согласно `fee_collector` tests; verify schema and extend).
  - Update `sthrip/db/transaction_repo.create_transaction(...)` — compute fee, deduct from sender, insert fee row.
  - Logic в `sthrip/services/fee_calculator.py` (new) — `compute_fee(amount, agent_tier) -> Decimal`.
  - Floor enforcement: `max(amount * rate, 1)` (1 piconero minimum).
  - Tier lookup cached в request context (avoid redundant DB hits).
- **Файлы**:
  - existing `sthrip/db/transaction_repo.py`
  - new `sthrip/services/fee_calculator.py`
  - existing `sthrip/db/models.py` (verify FeeCollection model adequate)
  - new `migrations/versions/x5y6z7a8b9c0_fee_collections.py`
  - new `tests/test_fee_calculator.py`
  - new `tests/test_commission_on_transfer.py`
  - existing `tests/test_fee_collector.py` (verify still green с новой логикой)
- **Тесты**:
  - `test_free_tier_pays_03_percent` — $100 transfer, $0.30 fee
  - `test_pro_tier_pays_01_percent` — $100 transfer, $0.10 fee
  - `test_dust_transfer_pays_floor` — 100 piconero, fee = 1 piconero (not 0)
  - `test_commission_deducted_from_sender_not_receiver` — receiver gets full advertised amount
  - `test_fee_collection_row_inserted` — після insert проверка row presence в fee_collections
- **Risk**: HIGH — touches every payment write. gitnexus_impact на `transaction_repo.create_transaction` обязательно.

#### Sprint 3: Subscription tier + enforcement

- **Цель**: Free/Pro/Enterprise tiers с rate limiting на Free.
- **Скоп**:
  - Verify `agents.tier` field exists (it does per existing models). Add `tier_grace_until: TIMESTAMPTZ NULL` для Phase 2 grace logic.
  - `agent_monthly_stats(agent_id, month_start, transaction_count, last_updated)` — aggregation table.
  - Trigger или repo wrap: increment `transaction_count` on each successful transfer.
  - FastAPI middleware `tier_limit_middleware.py` — checks count vs tier limit, returns 429 if exceeded.
  - Endpoint `POST /v2/me/upgrade` — accepts `{tier: "PRO"|"ENTERPRISE"}`, sets agent.tier, schedules first billing on next 1st.
  - Endpoint `GET /v2/me/tier` — returns current tier + usage stats.
  - `migrations/versions/y6z7a8b9c0d1_tier_grace_and_stats.py`.
- **Файлы**:
  - existing `sthrip/db/models.py`
  - new `api/middleware/tier_limit.py`
  - existing `api/routers/agents.py` или new `api/routers/me.py`
  - existing `sthrip/services/agent_registry.py` (tier lookup helpers)
  - new `migrations/versions/y6z7a8b9c0d1_tier_grace_and_stats.py`
  - new `tests/test_tier_enforcement.py`
- **Тесты**:
  - `test_free_tier_blocked_at_101st_transfer`
  - `test_pro_tier_unlimited`
  - `test_upgrade_endpoint_changes_tier`
  - `test_month_rollover_resets_count`
  - `test_429_response_includes_upgrade_hint`
- **Risk**: MEDIUM — middleware на hot path. Carefully test does not block legitimate Pro/Enterprise traffic.

#### Sprint 4: XMR billing cron + grace handling

- **Цель**: подписка списывается с XMR balance ежемесячно, grace period 7 дней при insufficient.
- **Скоп**:
  - `subscription_billing_service.py` — `bill_pro_subscriptions()`, `handle_grace_expiry()`.
  - Use existing `conversion_service.py` (или create) для XMR/USD rate с 5-min cache.
  - Cron entry в scheduler: 1-е число месяца, 04:00 UTC.
  - Daily cron: process expired grace periods.
  - Audit events for billing successes, failures, downgrades.
  - Migration: `agent_billing_history(agent_id, month, amount_usd, amount_xmr, rate_applied, status, processed_at)`.
- **Файлы**:
  - new `sthrip/services/subscription_billing_service.py`
  - existing `sthrip/services/conversion_service.py` (verify or create)
  - existing `sthrip/services/scheduler.py`
  - new `migrations/versions/z7a8b9c0d1e2_billing_history.py`
  - new `tests/test_subscription_billing.py`
- **Тесты**:
  - `test_pro_agent_charged_29_usd_in_xmr_at_rate`
  - `test_insufficient_balance_starts_grace_period`
  - `test_grace_expiry_downgrades_to_free`
  - `test_balance_topped_up_during_grace_resumes_pro`
  - `test_proration_on_mid_month_upgrade`
  - `test_idempotent_cron_run` — re-run same day doesn't double-charge
- **Risk**: MEDIUM. Live exchange rate dependency adds external failure mode — use cached fallback up to 24h, then alert.

### Phase 3: TEE migration (3 sprints)

#### Sprint 5: GCP project + Confidential VM template

- **Цель**: GCP project up, Confidential VM template ready, payment service Dockerfile prepared.
- **Скоп**:
  - `gcp/payment-tee-deploy/` directory: Dockerfile (slim Python + payment service code), Terraform или gcloud scripts для VM provisioning, README.
  - Identify minimum payment service surface — only `/v2/payments/hub-routing` endpoint plus dependencies (`envelope_crypto`, `payment_envelope_writer`, `transaction_repo` write path).
  - Build self-contained service image — `sthrip-payment-tee:latest`. Should NOT include marketplace, admin, discovery code.
  - mTLS certs setup — Railway client cert auth to GCP VM. CA store on both sides.
  - **No deploy yet** — just artefacts ready for operator action.
- **Файлы**:
  - new `gcp/payment-tee-deploy/Dockerfile`
  - new `gcp/payment-tee-deploy/payment_service.py` (subset of payment routes + dependencies)
  - new `gcp/payment-tee-deploy/setup-vm.sh` (gcloud-driven provisioning)
  - new `gcp/payment-tee-deploy/README.md` (operator runbook)
  - new `tests/test_payment_service_self_contained.py` — verify import-graph не тянет marketplace/admin
- **Тесты**:
  - `test_payment_service_imports_only_payment_deps` — explicitly assert no transitive import of `agent_registry.discover_agents` etc
  - `test_dockerfile_builds` — local docker build smoke test (skip in CI if docker not available)
  - `test_mtls_cert_generation_script` — mock cert generation
- **Risk**: MEDIUM. Self-contained boundary tricky — payment depends on envelope_crypto which depends on operator_keystore. Need careful pruning.

#### Sprint 6: Payment proxy from Railway to GCP

- **Цель**: Railway can proxy payment requests to GCP TEE behind feature flag.
- **Скоп**:
  - `payment_tee_client.py` — gRPC client (с mTLS) к GCP VM.
  - `payment_dispatch.py` — wraps existing `/v2/payments/hub-routing` handler. If `STHRIP_PAYMENT_VIA_TEE=true` AND TEE reachable → proxy. Else → local handle (preserves rollback safety).
  - Health check от Railway к GCP VM каждые 60 сек. If unreachable while flag on → fall back to local + alert.
  - Idempotency-key passthrough so retries don't double-process.
- **Файлы**:
  - new `sthrip/services/payment_tee_client.py`
  - existing `api/routers/payments.py` (or wherever hub-routing handler lives)
  - new `tests/test_payment_dispatch.py`
- **Тесты**:
  - `test_flag_off_uses_local_handler`
  - `test_flag_on_proxies_to_tee_client_mock`
  - `test_tee_unreachable_falls_back_to_local`
  - `test_idempotency_key_propagates`
  - `test_response_shape_identical_local_vs_proxy`
- **Risk**: HIGH — payment hot path. Independent Evaluator должен verify behavior identical в both modes.

#### Sprint 7: Remote attestation + cutover docs

- **Цель**: SDK verifies TEE attestation; operator runbook for cutover finalized.
- **Скоп**:
  - `attestation_service.py` — collects AMD SEV-SNP report at TEE boot, posts to `/.well-known/attestation.json` with `{quote_b64, image_hash, timestamp, signature}`.
  - SDK update — `Sthrip(verify_tee=True)` fetches attestation, validates against pinned image hash. Mismatch → raise `TEEMismatchError`.
  - Update `THREAT_MODEL.md` Sprint 7 — runtime hub compromise row updated to "TEE-protected; residual: TEE primitive bug or AMD/AWS supply-chain compromise".
  - Update `PRIVACY_FEATURES.md` — add Phase 2/3 commit refs, mark TEE as shipped.
  - Operator runbook in `gcp/payment-tee-deploy/CUTOVER.md`: 12-step migration plan, including staging dry-run, monitoring checklist, rollback procedure.
- **Файлы**:
  - new `gcp/payment-tee-deploy/attestation_service.py`
  - existing `sdk/sthrip/client.py` (add verify_tee param)
  - existing `docs/THREAT_MODEL.md`
  - existing `PRIVACY_FEATURES.md`
  - new `gcp/payment-tee-deploy/CUTOVER.md`
  - new `tests/test_sdk_tee_attestation.py`
- **Тесты**:
  - `test_attestation_endpoint_includes_required_fields`
  - `test_sdk_verify_tee_accepts_pinned_hash`
  - `test_sdk_verify_tee_rejects_mismatch_hash`
  - `test_sdk_verify_tee_disabled_skips_check`
- **Risk**: MEDIUM. Attestation cryptography subtle — verify against AMD's documented SEV-SNP attestation flow.

---

## Edge Cases

- **Purge interaction with envelope decrypt**: deleted rows can't be decrypted — that's the point. Audit log records what was purged (count, date range), not which specific records. Acceptable per AD-1.
- **Subscription billing under network outage**: cron retries up to 3x with exponential backoff. After failure window, marks billing as `pending_retry` and admin alert sent.
- **Pro tier downgrade mid-payment**: tier captured at request start, used throughout single request. No mid-flight tier flip.
- **TEE attestation on cold boot**: VM may take 60-120s после restart to produce stable attestation. SDK should retry attestation fetch with exponential backoff up to 5 minutes.
- **Currency rate during high volatility**: if XMR/USD moves >10% within 5-min cache window, billing uses cached rate (locked in). User benefit if rate spikes after billing; user loss if drops. Acceptable tradeoff for predictability.
- **Existing fee_collector tests**: verify не сломаны Sprint 2 changes. Existing fee logic might need migration — Generator должен diff carefully.
- **Free tier counter near boundary**: race condition on 100→101 transfer. Use SELECT FOR UPDATE or PostgreSQL advisory lock per agent для idempotent counting.
- **GCP outage during Phase 3 cutover**: feature flag fall-back to local handler covers this. Document in CUTOVER.md.

---

## Out of Scope

Repeated from `user-criteria.md`:

- Stripe / fiat billing
- Non-custodial pivot (channels, Lightning, atomic swaps)
- Own coin / token
- Whistleblower-tier protection
- AWS Nitro Enclaves (alternate path; не выбран)
- Multi-jurisdiction operator threshold sigs (future)
- Annual prepay subscription discount (review через 6 мес)

---

## Operator Action Items (after all 7 sprints land)

Sequential — order matters:

1. Merge `feat/revenue-and-tee` to main (after staging soak)
2. Set `CANARY_SIGNING_KEY` on Railway (generate Ed25519, base64 encode)
3. Set `STHRIP_DATA_RETENTION_DAYS` (default 60 OK или customize)
4. Verify Phase 1 deploy: check `/.well-known/canary.txt` returns signed JSON, verify cron logs показывают daily purge run
5. Create GCP project `sthrip-tee`
6. Provision Confidential VM via `gcp/payment-tee-deploy/setup-vm.sh`
7. Deploy `sthrip-payment-tee:latest` image to VM
8. Capture AMD SEV-SNP attestation report, set `STHRIP_TEE_IMAGE_HASH` on Railway service
9. Set `STHRIP_PAYMENT_VIA_TEE=false` initially. Soak Railway with TEE health checks for 48h
10. Enable `STHRIP_PAYMENT_VIA_TEE=true` in staging environment first, run smoke tests
11. Enable in production. Monitor `/admin/revenue` and payment latency dashboards 24h
12. If issues: flip flag back to false instantly. No code rollback needed.
13. After 7-day soak with flag on: announce TEE protection в `PRIVACY_FEATURES.md` update + blog post

---

## Open Questions for Lead

None — see `lead-decisions.md`. All decisions pre-resolved.
