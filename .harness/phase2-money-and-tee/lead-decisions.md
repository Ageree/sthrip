# Lead Decisions (pre-baked, do NOT re-ask)

Все ключевые решения уже приняты. Generator-у не нужно задавать эти вопросы — они здесь.

## Pricing & Commission

- **Commission rate**: 0.3% для Free tier, 0.1% для Pro/Enterprise (discount stimulates upgrade)
- **Floor**: 1 atomic unit (piconero) — даже dust transfers платят минимальную fee
- **Subscription pricing**: Free / Pro $29/мес / Enterprise $999/мес
- **Pricing currency**: USD denominated, charged в XMR equivalent at live rate

## Billing infrastructure

- **No Stripe**, no Paddle, no Lemon Squeezy
- **Crypto-native**: subscription deducted from agent's hub balance в XMR
- **Live rate source**: используем существующий cache в `sthrip/services/conversion_service.py` (если есть; иначе coingecko free tier)
- **Insufficient balance**: 7-day grace period → auto-downgrade to Free, no penalties beyond loss of Pro features
- **Existing agents**: ВСЕ grandfathered to Free tier на migration. Пусть upgrade-ятся вручную если нужно.

## Cloud / Infra

- **Cloud target**: GCP Confidential VMs (AMD SEV-SNP)
- **Reasoning**: gcloud CLI sufficient для autonomous management; ~10% cost overhead; mature enough; Railway-like UX в Cloud Run
- **Migration scope**: hybrid — только payment hot path, остальное на Railway
- **AWS Nitro и Hetzner SEV-SNP отвергнуты** — Nitro слишком enterprise-y, Hetzner слишком self-managed

## Architecture invariants

- Custodial архитектура **сохраняется**. Не пытайся pivot'ить на non-custodial — это отдельный 6-10 месячный проект, не в скоупе.
- Hub видит plaintext в RAM во время routing — это закрывается **TEE migration в Phase 3**, не architectural change
- Auto-purge respect-ит HMAC chain (rolling chain reset, не break)

## Branching & Workflow

- **Base branch**: `feat/revenue-and-tee` от main (если `feat/anonymity-hardening` уже merged) или от `feat/anonymity-hardening` (если не merged — тогда продолжаем там же)
- Lead на первом sprint первым делом проверяет `git log origin/main..HEAD` — если есть unmerged commits Phase 1, спрашивает оператора (через PushNotification если можно) что делать
- Per-sprint commits, не batch
- Финальный merge только после прогона всего test suite

## Privacy/Security invariants

- Auto-purge **не должен** удалять записи которые active references (e.g., transaction внутри undelivered escrow). Нужен FK respect — soft purge на active rows, hard на closed ones.
- Warrant canary signing — используем существующий `WEBHOOK_ENCRYPTION_KEY` или генерируем dedicated `CANARY_SIGNING_KEY`. Lead decision: dedicated key, чтобы canary compromise не affected webhook integrity.
- TEE attestation — публикуется в `/.well-known/attestation.json` с **AMD SEV report** + image hash. SDK verify-ит против known-good hash в `sthrip_sdk/attestation_anchors.py`.

## Subscription edge cases (Lead resolves)

- **What happens at month boundary?** Cron на 1-е число каждого месяца. Если агент upgrade'нулся mid-month — pro-rate. Если downgrade'нулся mid-month — Free лимиты вступают в силу немедленно, refund unused portion в balance.
- **What about transactions exactly at midnight UTC?** Use `created_at < (today + interval '1 second')` для inclusive boundary, без race conditions.
- **Trial period?** Нет. Free tier itself — это trial. 100 платежей/мес позволяет evaluate'ить.
- **Annual prepay discount?** Не в этом цикле. Поговорим через 6 месяцев когда будет signal.

## Phase ordering

Strict sequential, no parallel:

1. Phase 1 (1 спринт) — auto-purge + canary. Self-contained, no infra changes.
2. Phase 2 (3 спринта) — commission, subscription tier + enforcement, XMR billing cron.
3. Phase 3 (3 спринта) — GCP project, payment service migration, attestation + cutover.

Каждая phase полностью committed and tested перед следующей. /loop-style harness, fresh team каждый sprint.

## Что Generator-у НЕ задавать

- "Какая commission rate?" — 0.3% Free, 0.1% Pro/Enterprise
- "Какой billing provider?" — XMR-native, никакого Stripe
- "Куда мигрировать?" — GCP Confidential VMs
- "Что с existing agents?" — все на Free
- "Грейс период?" — 7 дней
- "Trial?" — нет, Free tier и есть trial
