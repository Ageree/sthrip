# User Criteria: Anonymize Sthrip Platform

## Goal

Превратить Sthrip из псевдонимной (Monero on-chain anonymity ломается централизованным хабом) в по-настоящему анонимную для участников. Снизить деанонимизационную поверхность до уровня, при котором утечка БД хаба или subpoena к Railway НЕ выдают платёжный граф.

## Acceptance Criteria

1. **audit_log не хранит сырые PII**
   - `audit_log.ip_address` либо удалён, либо хеширован keyed-HMAC с rotating salt (salt ротируется не реже раза в неделю и старый удаляется)
   - `audit_log.request_body` пишется через allowlist полей, а не как произвольный JSON
   - HMAC-цепочка остаётся валидной после изменений

2. **Платёжный граф не виден администратору в открытом виде**
   - `transactions.payer_id`/`payee_id`, `escrow_deals.buyer_id`/`seller_id` и связанные поля либо зашифрованы encrypted-at-rest ключом, к которому хаб не имеет постоянного доступа, либо заменены blinded-токенами, расшифровываемыми только участниками
   - Минимум: ADMIN_API_KEY один не должен давать чтения этого графа без второго фактора (HSM/operator key)
   - Указать в THREAT_MODEL.md, какой сценарий компрометации остаётся (например, runtime-память хаба видит план в момент роутинга)

3. **Marketplace — opt-in и минимальный fingerprint**
   - Поле `agents.is_public` (по умолчанию `false`) фильтрует discovery
   - Дефолтный `description`, `pricing`, `capabilities` — пусто, не утечь стилометрию
   - GET `/v2/agents/marketplace` отдаёт только `is_public=true` агентов

4. **Tor .onion endpoint реально работает**
   - Запущен hidden service v3 (Railway sidecar или отдельный VPS — на выбор)
   - В `/.well-known/agent-payments.json` опубликован `onion_endpoint`
   - SDK поддерживает SOCKS5 connect через Tor

5. **webhook_url не привязывает агента к домену**
   - Прямой `agents.webhook_url` либо удалён, либо вынесен в отдельную приватную таблицу с шифрованием
   - Для публичного marketplace webhook не светится никогда
   - Опционально: blinded webhook relay (хаб шлёт на онион-адрес агента, не на clearnet домен)

6. **THREAT_MODEL.md описывает угрозы, защиту, остаточные риски**
   - Таблица: "угроза → текущая защита → остаточный риск"
   - Минимум 8 сценариев (внешний blockchain-анализатор, скрейпинг marketplace, subpoena Railway, утечка ADMIN_API_KEY, корреляция через webhook, сетевой наблюдатель, runtime-компрометация хаба, инсайдер)

## Constraints

- **Production live на mainnet** — `sthrip-api-production.up.railway.app`. Не сломать.
- **2221 тестов проходят** — финальный набор должен быть зелёным; новые фичи добавляют тесты, не уменьшают coverage.
- Стек: Python 3.9, FastAPI, SQLAlchemy, PostgreSQL, Alembic
- **Alembic миграции**: IF NOT EXISTS / IF EXISTS, идемпотентность; PostgreSQL enum изменения с VARCHAR cast перед string comparison
- **GitNexus**: ОБЯЗАТЕЛЬНО `gitnexus_impact` перед редактированием функций; HIGH/CRITICAL риск — флагать
- **TDD**: тесты сначала, минимум 80% coverage на новый код
- **Railway CLI**: деплои через `railway up` / env vars через `railway variables --set`
- **Branch**: всё в `feat/anonymity-hardening`, не пушить в main без полного зелёного теста-сьюта
- **Workflow артефакты**: `.harness/anonymize-platform/`, контракты спринтов в этой папке

## Agent Architecture (от пользователя)

- Planner / Generator / Evaluator
- ПОСЛЕ КАЖДОГО СПРИНТА команда пересоздаётся (новый `Agent({subagent_type:...})` вызов с чистым контекстом) — против context limit reached
- Evaluator всегда в НЕЗАВИСИМОМ контексте (никогда не получает историю Generator, только: путь к спринт-контракту + diff/файлы для проверки)
- /loop каждые 30 минут поднимает Lead для следующего спринта

## Out of Scope (на этом этапе)

- ZK proofs / zk-SNARKs (упомянуты в PRIVACY_FEATURES.md, но это исследовательская работа)
- CoinJoin/Submarine Swaps mixing (там же — план, не код)
- MPC-based mixing без координатора
- Полный rewrite roadmap-овых обещаний из PRIVACY_FEATURES.md (его честно переписать в roadmap)
