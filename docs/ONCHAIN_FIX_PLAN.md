# План: Починка Onchain Deposit + Withdrawal

**Дата:** 2026-04-14
**Статус:** В работе

## Результаты тестирования

### ✅ Withdrawal (система → внешний кошелёт) — РАБОТАЕТ
- Отправили 0.001 XMR с hub-кошелька на внешний Monero GUI кошелёт
- TX hash: `a86915e841c51a942034eceea9fb7be6ba8fedc1d02a95ee9130a18c378840f2`
- Подтверждено 10/10

### ❌ Deposit (внешний → система) — НЕ РАБОТАЕТ
- Пользователь отправил 0.0005 XMR с Monero GUI на deposit subaddress
- TX hash: `0d268e7ec1c1f489acefad6fe512f17b34727771694ce62124bfd5764711fb78`
- Транзакция подтвердилась (13+ confirmations)
- Wallet RPC видит трансфер через `incoming_transfers`
- **Но DepositMonitor НЕ стартует** — alembic миграция зависает при старте
- `last_scanned_height` застрял на `2070525` (не меняется)
- Баланс агента НЕ зачислен

## Корневая проблема

**Alembic миграция зависает** при запуске приложения:
- Логи показывают `Context impl PostgresqlImpl` + `Will assume transactional DDL` и дальше тишина
- Из-за этого FastAPI lifespan никогда не завершается
- DepositMonitor, webhook worker, health monitoring — **ничего не стартует**
- API при этом отвечает на запросы (uvicorn поднимается до lifespan)

## Что починить

| # | Задача | Время | Статус |
|---|--------|-------|--------|
| 1 | Починить зависание alembic миграции | 2-3 часа | TODO |
| 2 | Запустить DepositMonitor | 30 мин | Блокируется шагом 1 |
| 3 | Проверить что 0.0005 XMR зачислены агенту | 5 мин | Блокируется шагом 2 |
| 4 | Удалить debug endpoints | 30 мин | TODO |
| 5 | Восстановить production настройки | 10 мин | TODO |

## Что нужно для продолжения

Починить alembic — скорее всего одно из:
1. Миграция пытается создать уже существующий индекс/таблицу и ждёт лок
2. Несколько workers одновременно выполняют миграцию (даже с WEB_CONCURRENCY=1)
3. Нужно добавить `--timeout` к alembic или обернуть в `run_async`

После починки — DepositMonitor подхватит 0.0005 XMR которые уже в кошельке.

## Деньги в системе

- **Hub balance:** 0.11546072 XMR (0.001 sent to user, 0.0005 received from user, +fees)
- **User withdrawal received:** 0.001 XMR ✅
- **User deposit pending:** 0.0005 XMR ❌ (not credited, monitor not running)
- **Test agents have:** ~0.002 XMR from ledger credits

## Файлы

- `sthrip/services/deposit_monitor.py` — мониторинг входящих депозитов
- `sthrip/services/wallet_service.py` — работа с wallet RPC
- `api/routers/balance.py` — endpoints deposit/withdraw
- `api/routers/payments.py` — hub-routing переводы
- `api/routers/debug_wallet.py` — debug endpoints (УДАЛИТЬ после починки)
- `api/main_v2.py` — startup/lifespan (alembic hang here)
