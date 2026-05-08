# User Criteria: Phase 2 — Revenue + TEE Hardening

## Goal

Превратить Sthrip (после anonymity-hardening Phase 1, ветка `feat/anonymity-hardening`) в **зарабатывающую платформу с runtime-protected privacy**. Custodial остаётся (per Lead decision — non-custodial pivot из обсуждения отвергнут как overkill для AI agent payment use case).

Три цели в одном проекте:

1. **Auto-data-minimization** — старые данные удаляются автоматически, что закрывает 80% subpoena exposure без architectural changes.
2. **Revenue streams** — commission на transfers + subscription tier + crypto-native billing (без Stripe). Money flows from week 4.
3. **TEE migration** — payment hot path в GCP Confidential VMs. Закрывает runtime memory leak (последний остаточный риск из THREAT_MODEL.md после Sprint 1-7+4b).

## Acceptance Criteria

### Phase 1 — Data minimization

1. **Auto-purge cron**: записи `transactions`, `escrow_deals`, `escrow_milestones`, `message_relays`, `audit_log` старше 60 дней автоудаляются ежедневным cron'ом. Срок настраивается через `STHRIP_DATA_RETENTION_DAYS` env (default 60, range 7-365).
2. **Purge respects HMAC chain integrity** — `audit_log` purge не ломает chain (используем подход "rolling chain reset": когда purgе'им head — пишем new genesis link).
3. **Warrant canary** publishes ежедневно подписанное "as of {date}: no subpoena, no key compromise" на `/.well-known/canary.txt`. JSON + detached PGP signature.
4. **Canary auto-stops** если cron не run за 48h (signaling: либо oper subpoenaed, либо infra failure — в обоих случаях users learn).

### Phase 2 — Revenue

5. **Commission**: на каждом internal transfer (hub-routed payment) deduct **0.3%** от суммы в `fee_collections` table. Floor: 1 atomic unit (1 piconero). Минимум 1 atomic unit fee гарантирован даже на dust amounts.
6. **Subscription tiers**: Free / Pro $29/мес / Enterprise $999/мес. Все existing agents grandfathered to Free на migration.
7. **Tier enforcement**: Free tier лимит 100 платежей/мес. Pro/Enterprise unlimited. Превышение Free → 429 Too Many Requests с upgrade hint.
8. **Crypto-native billing**: subscription оплачивается в XMR с balance агента. Cron каждый день в начале месяца deduct'ает $29 эквивалент XMR (используем live exchange rate cache). Если insufficient balance → grace period 7 дней → auto-downgrade на Free.
9. **Pro subscriber commission discount**: Pro/Enterprise pays 0.1% commission (vs 0.3% Free). Stimulates upgrade для high-volume agents.
10. **Revenue dashboard**: admin endpoint `/admin/revenue` показывает MTD commission revenue, MTD subscription revenue, agents per tier, churn metrics. Self-only access (operator).

### Phase 3 — TEE migration

11. **GCP Confidential VM**: payment hot path (`/v2/payments/hub-routing` endpoint + dependencies) запускается в GCP Confidential VM (AMD SEV-SNP). Service deployable через `gcloud` CLI.
12. **Hybrid architecture**: Railway остаётся для marketplace/admin/discovery/Tor sidecar/op-keystore. GCP принимает только payment requests. Cross-network через mTLS-authenticated gRPC или signed HTTP.
13. **Remote attestation**: `/.well-known/attestation.json` публикует current TEE quote. Agents (через SDK) verify attestation перед sending payment. Mismatch → SDK refuses.
14. **Zero downtime cutover**: feature flag `STHRIP_PAYMENT_VIA_TEE` (default false) — когда true, Railway proxy'ит payment requests на GCP TEE. Можно flip back instantly.
15. **Cost overhead < 15%**: GCP infra $50-100/мес acceptable, выше — review.

## Constraints

- **Production live** на mainnet (`sthrip-api-production.up.railway.app`). Не сломать.
- **Branch**: `feat/revenue-and-tee` создаётся от **main** после merge'а `feat/anonymity-hardening` ИЛИ от `feat/anonymity-hardening` если ещё не merged. Lead решает первым шагом.
- Стек: Python 3.9, FastAPI, SQLAlchemy, PostgreSQL, Alembic — без changes.
- **Тесты**: TDD, 80%+ coverage на новом коде. Final repo suite green.
- **Crypto billing — без Stripe**, без external billing provider. Используем internal XMR balances + cron subscription.
- **GCP миграция — гибрид**: только payment hot path. НЕ переезжаем целиком.
- **Lead решения уже сделаны** (см. `lead-decisions.md`) — НЕ задавать вопросы которые там покрыты.

## Agent Architecture

- Lead / Planner / Generator / Evaluator (как в Phase 1)
- Свежий контекст после каждого спринта
- Independent Evaluator
- Per-sprint contract files: `sprint-N-contract.md`, `sprint-N-generator-report.md`, `sprint-N-result.md`
- `state.json` для cross-sprint state

## Out of Scope

- Stripe / fiat billing — позже как opt-in feature если будет спрос
- Non-custodial pivot — отвергнут per discussion 2026-05-07
- Lightning / channels — отвергнут per discussion
- Свой токен — отвергнут per discussion
- Whistleblower-tier protection — out of positioning per discussion
- Multi-jurisdiction operator threshold sigs — future work, не для этого цикла
- AWS Nitro Enclaves — alternate path, не выбран. GCP Confidential VMs.

## Success Definition

После всех 3 phases:

1. Платформа автоматически удаляет старые данные → значительно сокращается subpoena exposure
2. Платформа зарабатывает первые $1000+ ARR через 1-2 месяца после Phase 2 deploy
3. Payment hot path защищён от runtime memory attacks даже при компрометации хоста
4. THREAT_MODEL.md обновляется с новым residual-risk profile (TEE supply-chain trust остаётся, но user-coercion и Railway runtime exposure снимаются)
