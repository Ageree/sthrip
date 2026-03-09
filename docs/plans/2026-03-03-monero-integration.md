/# Monero On-Chain Integration Plan

**Date**: 2026-03-03
**Goal**: Connect hub ledger to real Monero blockchain — deposits, withdrawals, confirmation tracking
**Prerequisite**: Running `monero-wallet-rpc` (stagenet or mainnet)

---

## Что уже есть

| Компонент | Файл | Статус |
|-----------|------|--------|
| MoneroWalletRPC клиент | `sthrip/wallet.py` | ✅ Полный JSON-RPC |
| StealthAddressManager | `sthrip/stealth.py` | ✅ Генерация субадресов |
| DB: `deposit_address` поле | `db/models.py` AgentBalance | ✅ Есть, не заполняется |
| DB: `tx_hash, confirmations, block_number` | `db/models.py` Transaction | ✅ Есть, не обновляется |
| `BalanceRepository.set_deposit_address()` | `db/repository.py` | ✅ Есть, не вызывается |
| `BalanceRepository.deposit()` | `db/repository.py` | ⚠️ Кредитует напрямую без проверки |
| Wallet health check | `services/monitoring.py` | ✅ Есть, отключен (`include_wallet=False`) |
| Deposit endpoint | `api/main_v2.py` | ⚠️ Stub — auto-credit |
| Withdraw endpoint | `api/main_v2.py` | ⚠️ Deduct без создания tx |

---

## Архитектура

```
Agent → POST /v2/balance/deposit → Hub генерирует субадрес → Agent отправляет XMR
                                                                    ↓
                                          DepositMonitor (фон) ← monero-wallet-rpc
                                                    ↓
                                          10 confirmations → credit balance
                                                    ↓
                                          Webhook: payment.deposit_confirmed

Agent → POST /v2/balance/withdraw → Hub проверяет баланс → wallet.transfer()
                                                                ↓
                                                          tx_hash → Agent
                                                                ↓
                                          DepositMonitor → track confirmations
```

---

## Задача 1: WalletService — обёртка для hub-кошелька (P0)

**Файл**: `sthrip/services/wallet_service.py` (новый)

Создать сервис, который управляет hub-кошельком через существующий `MoneroWalletRPC`.

```python
class WalletService:
    def __init__(self):
        self.wallet = MoneroWalletRPC.from_env()
        self._subaddress_cache: Dict[UUID, str] = {}

    def get_or_create_deposit_address(self, agent_id: UUID) -> str:
        """
        Получить или создать уникальный субадрес для агента.
        1. Проверить AgentBalance.deposit_address в БД
        2. Если нет — wallet.create_address(account_index=0, label=str(agent_id))
        3. Сохранить через BalanceRepository.set_deposit_address()
        4. Вернуть субадрес
        """

    def send_withdrawal(self, to_address: str, amount: Decimal) -> Dict:
        """
        Отправить XMR из hub-кошелька.
        1. wallet.transfer(destination=to_address, amount=piconero_amount)
        2. Вернуть {tx_hash, fee, amount}
        """

    def get_incoming_transfers(self, min_height: int = 0) -> List[Dict]:
        """
        Получить входящие переводы от wallet RPC.
        wallet.get_transfers(in=True, filter_by_height=True, min_height=...)
        """

    def get_wallet_info(self) -> Dict:
        """Баланс, высота, адрес — для health check и admin."""
```

**Ключевые решения:**
- Один `account_index=0` для всех операций hub
- Каждый агент получает уникальный субадрес (subaddress) в этом аккаунте
- Label субадреса = `agent_id` (для маппинга входящих транзакций)
- Суммы в piconero (1 XMR = 1e12 piconero) при общении с RPC

**Env vars:**
```
MONERO_RPC_HOST=127.0.0.1
MONERO_RPC_PORT=18082
MONERO_RPC_USER=
MONERO_RPC_PASS=
MONERO_NETWORK=stagenet          # stagenet | mainnet
MONERO_MIN_CONFIRMATIONS=10      # 10 для mainnet, 1 для stagenet
```

---

## Задача 2: DepositMonitor — фоновый воркер (P0)

**Файл**: `sthrip/services/deposit_monitor.py` (новый)

Фоновая задача, которая опрашивает `monero-wallet-rpc` каждые N секунд.

```python
class DepositMonitor:
    def __init__(self, wallet_service: WalletService, poll_interval: int = 30):
        self.wallet = wallet_service
        self.poll_interval = poll_interval
        self._running = False
        self._last_height = 0  # Последняя обработанная высота

    async def start(self):
        """Запуск бесконечного цикла опроса."""

    async def poll_once(self):
        """
        Один цикл опроса:
        1. wallet.get_incoming_transfers(min_height=self._last_height)
        2. Для каждого перевода:
           a. Найти agent_id по subaddress (label или deposit_address в БД)
           b. Если tx_hash уже в transactions — обновить confirmations
           c. Если новый — создать Transaction(status=PENDING)
           d. Если confirmations >= MIN_CONFIRMATIONS и ещё не зачислен:
              - BalanceRepository.deposit(agent_id, amount)
              - Transaction.status = CONFIRMED
              - queue_webhook(agent_id, "payment.deposit_confirmed", {...})
        3. Обновить self._last_height
        """

    def _match_subaddress_to_agent(self, subaddress: str) -> Optional[UUID]:
        """Маппинг субадреса → agent_id через БД."""
```

**Важные моменты:**
- Хранить `_last_height` в БД или Redis чтобы не потерять при рестарте
- Использовать `pending` поле в AgentBalance для ещё не подтверждённых депозитов
- Идемпотентность: проверять `tx_hash` перед созданием Transaction
- Обновлять `confirmations` для уже известных транзакций при каждом poll

**Жизненный цикл:**
- Запуск в `lifespan` рядом с webhook worker
- Graceful shutdown: `self._running = False`

---

## Задача 3: Переработать POST /v2/balance/deposit (P0)

**Файл**: `api/main_v2.py`

Текущее поведение: принимает `{amount}`, сразу кредитует баланс.
Новое поведение: возвращает субадрес для отправки XMR, баланс кредитуется автоматически после подтверждения.

```python
@app.post("/v2/balance/deposit")
async def deposit_balance(agent: Agent = Depends(get_current_agent)):
    """
    Запрос на депозит. Возвращает субадрес для отправки XMR.
    Баланс будет зачислен автоматически после 10 подтверждений.
    """
    wallet_svc = get_wallet_service()
    deposit_address = wallet_svc.get_or_create_deposit_address(agent.id)

    return {
        "deposit_address": deposit_address,
        "token": "XMR",
        "network": os.getenv("MONERO_NETWORK", "stagenet"),
        "min_confirmations": int(os.getenv("MONERO_MIN_CONFIRMATIONS", "10")),
        "message": "Send XMR to this address. Balance will be credited after confirmations."
    }
```

**Убрать**: параметр `amount` (агент отправляет сколько хочет).
**Убрать**: прямой вызов `repo.deposit()` — это теперь делает DepositMonitor.

**Обратная совместимость:**
- Добавить env var `HUB_MODE=ledger|onchain` (default `onchain`)
- В `ledger` режиме — старое поведение (auto-credit) для тестирования без ноды
- В `onchain` режиме — новое поведение (субадрес + monitor)

---

## Задача 4: Переработать POST /v2/balance/withdraw (P0)

**Файл**: `api/main_v2.py`

Текущее поведение: дебитует баланс, не создаёт транзакцию.
Новое поведение: дебитует баланс → отправляет XMR → возвращает tx_hash.

```python
@app.post("/v2/balance/withdraw")
async def withdraw_balance(
    req: WithdrawRequest,
    agent: Agent = Depends(get_current_agent),
):
    """Withdraw XMR to external address."""
    amount = Decimal(str(req.amount))

    with get_db() as db:
        repo = BalanceRepository(db)
        available = repo.get_available(agent.id)
        if available < amount:
            raise HTTPException(status_code=400, detail="Insufficient balance")

        # 1. Дебитуем баланс (с FOR UPDATE lock)
        repo.deduct(agent.id, amount)
        balance = repo.get_or_create(agent.id)
        balance.total_withdrawn += amount

    # 2. Отправляем XMR через wallet RPC
    wallet_svc = get_wallet_service()
    try:
        tx_result = wallet_svc.send_withdrawal(req.address, amount)
    except Exception as e:
        # Откат баланса если RPC упал
        with get_db() as db:
            repo = BalanceRepository(db)
            repo.credit(agent.id, amount)
        raise HTTPException(status_code=502, detail=f"Withdrawal failed: {e}")

    # 3. Записываем транзакцию в БД
    with get_db() as db:
        tx_repo = TransactionRepository(db)
        tx_repo.create(
            tx_hash=tx_result["tx_hash"],
            network=os.getenv("MONERO_NETWORK", "stagenet"),
            from_agent_id=agent.id,
            amount=amount,
            fee=Decimal(str(tx_result.get("fee", 0))),
            payment_type="HUB_ROUTING",
            status="PENDING",
        )

    # 4. Webhook
    queue_webhook(str(agent.id), "payment.withdrawal_sent", {
        "tx_hash": tx_result["tx_hash"],
        "amount": float(amount),
        "to_address": req.address[:8] + "...",
    })

    return {
        "status": "sent",
        "tx_hash": tx_result["tx_hash"],
        "amount": float(amount),
        "fee": float(tx_result.get("fee", 0)),
        "to_address": req.address,
        "remaining_balance": float(balance.available),
        "token": "XMR",
    }
```

**Критично**: если `wallet.transfer()` упал — откатить баланс через `credit()`.

**Обратная совместимость**: в `HUB_MODE=ledger` — старое поведение без RPC.

---

## Задача 5: Endpoint для статуса депозитов (P0)

**Файл**: `api/main_v2.py`

Агент должен видеть свои pending и confirmed депозиты.

```python
@app.get("/v2/balance/deposits")
async def list_deposits(
    limit: int = Query(default=20, ge=1, le=100),
    agent: Agent = Depends(get_current_agent),
):
    """List deposit transactions for current agent."""
    # Query Transaction table where to_agent_id == agent.id
    # Return: tx_hash, amount, confirmations, status, created_at
```

---

## Задача 6: Включить wallet health check (P1)

**Файл**: `api/main_v2.py` (lifespan), `sthrip/services/monitoring.py`

```python
# В lifespan:
hub_mode = os.getenv("HUB_MODE", "onchain")
monitor = setup_default_monitoring(include_wallet=(hub_mode == "onchain"))
```

Также добавить в `/v2/admin/stats`:
```python
wallet_info = wallet_svc.get_wallet_info()
# balance, height, unlocked_balance, address
```

---

## Задача 7: Хранение last_scanned_height (P1)

**Файл**: `sthrip/services/deposit_monitor.py`

Чтобы DepositMonitor не пересканировал всю историю при рестарте:

**Вариант A (простой)**: Redis ключ `monero:last_scanned_height`
**Вариант B (надёжный)**: Новая таблица `system_state` с key-value парами

```python
class SystemState(Base):
    __tablename__ = "system_state"
    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)
```

---

## Задача 8: Piconero ↔ XMR конвертация (P1)

**Файл**: `sthrip/services/wallet_service.py`

Monero RPC работает в atomic units (piconero, 1 XMR = 1e12).
API и БД работают в XMR (Decimal).

```python
PICONERO = Decimal("1000000000000")  # 1e12

def xmr_to_piconero(xmr: Decimal) -> int:
    return int(xmr * PICONERO)

def piconero_to_xmr(piconero: int) -> Decimal:
    return Decimal(str(piconero)) / PICONERO
```

Все вызовы `wallet.transfer()` и `wallet.get_transfers()` должны конвертировать.

---

## Задача 9: Pending balance tracking (P1)

**Файл**: `sthrip/services/deposit_monitor.py`, `api/main_v2.py`

Использовать `AgentBalance.pending` поле:

1. Когда DepositMonitor видит новый входящий перевод (0 < confirmations < MIN):
   - `balance.pending += amount`
2. Когда confirmations >= MIN:
   - `balance.pending -= amount`
   - `balance.available += amount`
   - `balance.total_deposited += amount`

Это позволит агенту видеть в `GET /v2/balance`:
```json
{
  "available": 5.0,
  "pending": 2.5,    // ← видно что 2.5 XMR на подходе
  "total_deposited": 5.0,
  "deposit_address": "5..."
}
```

---

## Задача 10: Тесты с мок-кошельком (P1)

**Файл**: `tests/test_deposit_monitor.py` (новый)

```python
class MockWalletRPC:
    """Мок monero-wallet-rpc для тестов."""
    def create_address(self, account_index, label):
        return {"address": f"5fake{label[:20]}...", "address_index": 1}

    def get_transfers(self, **kwargs):
        return {"in": [...]}  # Подготовленные транзакции

    def transfer(self, destination, amount, **kwargs):
        return {"tx_hash": "abc123...", "fee": 1000000}
```

**Тесты:**
1. `test_deposit_address_generation` — субадрес создаётся и сохраняется в БД
2. `test_deposit_monitor_credits_after_confirmations` — баланс зачисляется после N confirmations
3. `test_deposit_monitor_idempotent` — повторный poll не дублирует зачисление
4. `test_withdrawal_creates_transaction` — withdraw вызывает `wallet.transfer()` и пишет в БД
5. `test_withdrawal_rollback_on_rpc_failure` — при ошибке RPC баланс откатывается
6. `test_pending_balance_updates` — pending отображается корректно
7. `test_ledger_mode_fallback` — в `HUB_MODE=ledger` работает auto-credit

---

## Задача 11: Alembic миграция для system_state (P2)

```bash
alembic revision --autogenerate -m "add system_state table"
```

---

## Задача 12: Stagenet End-to-End тест (P2)

**Файл**: `tests/integration/test_stagenet_deposit.py`

Реальный тест на stagenet:
1. Запустить `monero-wallet-rpc` на stagenet
2. Зарегистрировать агента
3. Получить субадрес
4. Отправить XMR из тестового кошелька на субадрес
5. Подождать confirmations
6. Проверить что баланс зачислен
7. Вывести часть на другой адрес
8. Проверить tx_hash в ответе

---

## Порядок выполнения

| # | Задача | Приоритет | Зависимости | Оценка |
|---|--------|-----------|-------------|--------|
| 1 | WalletService | P0 | — | 1 час |
| 8 | Piconero конвертация | P1 | Задача 1 | 15 мин |
| 2 | DepositMonitor | P0 | Задача 1, 8 | 2 часа |
| 3 | Переработать deposit endpoint | P0 | Задача 1 | 30 мин |
| 4 | Переработать withdraw endpoint | P0 | Задача 1, 8 | 1 час |
| 5 | Deposits list endpoint | P0 | Задача 2 | 20 мин |
| 9 | Pending balance tracking | P1 | Задача 2 | 30 мин |
| 6 | Включить wallet health check | P1 | Задача 1 | 10 мин |
| 7 | last_scanned_height storage | P1 | Задача 2 | 30 мин |
| 10 | Тесты с мок-кошельком | P1 | Задача 1-5 | 1.5 часа |
| 11 | Alembic миграция | P2 | Задача 7 | 10 мин |
| 12 | Stagenet E2E тест | P2 | Всё выше + нода | 2 часа |

**Итого: ~10 часов работы**

---

## Конфигурация для запуска

### Stagenet (тестирование)

```bash
# 1. Запустить monero daemon (stagenet)
monerod --stagenet --rpc-bind-port 38081

# 2. Запустить wallet RPC
monero-wallet-rpc --stagenet \
  --rpc-bind-port 18082 \
  --wallet-file hub-wallet \
  --password "wallet-pass" \
  --daemon-address 127.0.0.1:38081 \
  --disable-rpc-login

# 3. Env vars
export MONERO_RPC_HOST=127.0.0.1
export MONERO_RPC_PORT=18082
export MONERO_NETWORK=stagenet
export MONERO_MIN_CONFIRMATIONS=1
export HUB_MODE=onchain
```

### Mainnet (продакшн)

```bash
export MONERO_RPC_HOST=<secured-host>
export MONERO_RPC_PORT=18082
export MONERO_RPC_USER=rpc_user
export MONERO_RPC_PASS=<strong-password>
export MONERO_NETWORK=mainnet
export MONERO_MIN_CONFIRMATIONS=10
export HUB_MODE=onchain
```
