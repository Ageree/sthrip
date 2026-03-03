# Testing StealthPay

> ⚠️ **ВАЖНО**: Тестируем ТОЛЬКО на testnet (Sepolia + Stagenet) - монеты бесплатные и бесполезные ($0)

## Быстрый старт тестирования

```bash
# 1. Настройка окружения (один раз)
./scripts/setup_test_env.sh

# 2. Запустить все тесты
./scripts/run_all_tests.sh
```

## Детальное тестирование

### Phase 1: Component Tests (без денег)
```bash
python3 scripts/test_components.py
```

**Что тестируется:**
- Stealth address generation (<1 сек)
- ZK proofs (<1 сек)
- Ownership verification
- Key recovery

**Время:** ~30 секунд  
**Стоимость:** $0

---

### Phase 2: Contract Tests (локально)
```bash
cd contracts
npm test
```

**Что тестируется:**
- Lock/Claim/Refund
- Access control
- Reentrancy protection
- Edge cases

**Время:** ~2 минуты  
**Стоимость:** $0 (Hardhat network)

---

### Phase 3: E2E с реальными тестовыми деньгами

#### 3.1 Получить Sepolia ETH (бесплатно)
```bash
# Способ 1: PoW Faucet (надежный)
https://sepolia-faucet.pk910.de/

# Способ 2: Infura (если есть аккаунт)
https://www.infura.io/faucet/sepolia

# Способ 3: Alchemy (если есть аккаунт)
https://sepoliafaucet.com
```

#### 3.2 Настроить окружение
```bash
# Создать .env
cat > .env << EOF
SEPOLIA_RPC=https://rpc.sepolia.org
TEST_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
BRIDGE_CONTRACT=0xDEPLOYED_BRIDGE_ADDRESS
EOF
```

#### 3.3 Задеплоить контракты (один раз)
```bash
cd contracts
npx hardhat run scripts/deploy.js --network sepolia

# Сохранить адрес из вывода в .env
```

#### 3.4 Запустить E2E тест
```bash
# Минимальная сумма: 0.001 Sepolia ETH
python3 scripts/test_e2e_sepolia.py
```

**Что происходит:**
1. Генерация stealth адреса для XMR
2. Отправка 0.001 ETH в bridge контракт
3. Ожидание MPC ноды
4. Получение XMR на stealth адрес

**Время:** ~3-5 минут  
**Стоимость:** 0.001 Sepolia ETH (бесплатно)

---

## Тестовый сценарий пошагово

### Шаг 1: Проверка баланса
```bash
source .env

python3 << 'PYEOF'
from web3 import Web3
from eth_account import Account

w3 = Web3(Web3.HTTPProvider("$SEPOLIA_RPC"))
acc = Account.from_key("$TEST_PRIVATE_KEY")
balance = w3.eth.get_balance(acc.address)

print(f"Address: {acc.address}")
print(f"Balance: {w3.from_wei(balance, 'ether')} Sepolia ETH")
print(f"Enough for testing: {balance > w3.to_wei(0.01, 'ether')}")
PYEOF
```

### Шаг 2: Запуск одного теста
```python
# test_single.py
from web3 import Web3
import os

w3 = Web3(Web3.HTTPProvider(os.getenv("SEPOLIA_RPC")))

# Ваш тест здесь
print("✅ Подключение работает!")
print(f"Блок: {w3.eth.block_number}")
print(f"Gas price: {w3.from_wei(w3.eth.gas_price, 'gwei')} gwei")
```

### Шаг 3: Мониторинг транзакций
```bash
# После отправки TX, отслеживать:
echo "https://sepolia.etherscan.io/tx/$TX_HASH"

# Для XMR stagenet:
echo "https://stagenet.xmrchain.net/"
```

---

## Checklist перед тестом

- [ ] Получены Sepolia ETH (минимум 0.01)
- [ ] Контракты задеплоены
- [ ] `.env` настроен
- [ ] Component tests прошли
- [ ] MPC нода запущена (опционально)

---

## Чек-лист после теста

- [ ] Транзакция подтверждена
- [ ] Gas cost разумный (< $0.01 на mainnet)
- [ ] Нет ошибок в логах
- [ ] Время выполнения < 5 минут

---

## Troubleshooting

### "Insufficient balance"
```bash
# Получить больше Sepolia ETH
# На одном faucet можно получить 0.5 ETH/день
# Используйте несколько faucet-ов
```

### "Transaction failed"
```bash
# Проверить gas price
# Увеличить maxFeePerGas в коде
# Подождать, пока сеть не перегружена
```

### "Bridge contract not found"
```bash
# Задеплоить контракты
cd contracts
npx hardhat run scripts/deploy.js --network sepolia
```

### "MPC node not responding"
```bash
# Запустить локально
docker-compose -f docker-compose.testnet.yml up mpc-node-1

# Или без докера
python -m stealthpay.bridge.relayers.mpc_node_v2
```

---

## Уровни тестирования

### Level 1: Компоненты (безопасно)
- ✅ Никаких денег
- ✅ Быстро (< 1 мин)
- ✅ Локально

### Level 2: E2E с минимальной суммой (0.001 ETH)
- ✅ Реальная транзакция
- ✅ Минимальный риск ($0)
- ✅ Проверка интеграции

### Level 3: E2E с увеличением (до 0.1 ETH)
- ⚠️ Только после Level 2
- ⚠️ Проверка edge cases
- ⚠️ Мониторинг stability

### Level 4: Длительное тестирование
- ⚠️ 24+ часов работы
- ⚠️ Множество транзакций
- ⚠️ Stress testing

---

## Документация

- [Полный план тестирования](TESTING_REAL.md)
- [Чек-лист тестов](TESTING_CHECKLIST.md)
- [Компонентные тесты](scripts/test_components.py)
- [E2E тесты](scripts/test_e2e_sepolia.py)

---

## ⚠️ Правила безопасности

1. **НИКОГДА** не используйте mainnet
2. **НИКОГДА** не используйте приватные ключи с реальными деньгами
3. **ВСЕГДА** проверяйте, что используете testnet
4. **ВСЕГДА** начинайте с минимальной суммы (0.001 ETH)

## Куда идут ошибки?

Если нашли баг:
1. Сохранить transaction hash
2. Сохранить логи
3. Создать issue с подробностями
4. Не паниковать - это testnet!

---

**Готовы тестировать?** 🚀
```bash
./scripts/setup_test_env.sh && ./scripts/run_all_tests.sh
```
