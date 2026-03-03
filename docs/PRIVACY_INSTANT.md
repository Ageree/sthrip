# StealthPay: INSTANT Maximum Privacy

## Принцип: Математика, а не ожидание

❌ **НЕ** используем:
- Случайные задержки (бесполезны против продвинутого анализа)
- Time-based mixing (только раздражает пользователя)
- Dummy traffic (неэффективен)

✅ **ИСПОЛЬЗУЕМ** криптографию:
- Stealth addresses (невозможно связать на chain)
- Zero-knowledge proofs (доказательство без раскрытия)
- CoinJoin (реальное смешивание с другими пользователями)
- Submarine swaps (атомарные, мгновенные)
- Tor (скрытие IP)

## Архитектура: Instant Privacy

```
Пользователь
    │
    ├─► Tor Hidden Service (мгновенно)
    │
    ├─► Stealth Address (генерация <1 сек)
    │       └─ Одноразовый адрес, невозможно отследить
    │
    ├─► CoinJoin (1-2 минуты)
    │       └─ Реальное смешивание с 50+ участниками
    │
    ├─► Submarine Swap (мгновенно)
    │       └─ On-chain ↔ Lightning, атомарно
    │
    └─► ZK Proof (верификация <1 сек)
            └─ Доказательство без раскрытия данных
```

## Компоненты

### 1. Stealth Addresses (Мгновенно)
```python
from stealthpay.bridge.privacy import StealthAddressGenerator

generator = StealthAddressGenerator()
keys = generator.generate_master_keys()

# Каждый адрес - уникальный, одноразовый
stealth = generator.generate_stealth_address(
    recipient_scan_key=keys.scan_public,
    recipient_spend_key=keys.spend_public
)
# ⏱️ Время: <100ms
# 🔒 Приватность: Невозможно связать транзакции
```

### 2. CoinJoin (1-2 минуты)
```python
from stealthpay.bridge.mixing import CoinJoinCoordinator, ChaumianCoinJoin

coordinator = ChaumianCoinJoin(
    denomination=100000,  # 0.001 BTC
    min_anonymity_set=50  # Минимум 50 участников!
)

# Регистрация мгновенная через blind signatures
round_id = await coordinator.start_round()
await coordinator.register_input(round_id, input_utxo, peer_id)
await coordinator.register_output(round_id, stealth_address, peer_id)

# ⏱️ Время: 1-2 минуты (пока наберётся 50+ участников)
# 🔒 Приватность: Anonymity set = 50+
```

### 3. Submarine Swaps (Мгновенно)
```python
from stealthpay.bridge.mixing import SubmarineSwapService

service = SubmarineSwapService()

# On-chain → Lightning (атомарно)
swap = await service.create_swap_in(
    invoice_amount=50000000,
    refund_address="bc1q..."
)

# ⏱️ Время: 1-30 секунд (атомарный swap)
# 🔒 Приватность: Разрывает цепочку анализа
```

### 4. Zero-Knowledge Proofs (Мгновенно)
```python
from stealthpay.bridge.privacy import ZKVerifier

verifier = ZKVerifier()

# Доказательство владения без раскрытия ключа
proof = verifier.generate_ownership_proof(private_key, public_key)
is_valid = verifier.verify_ownership(proof, public_key)

# ⏱️ Время: <500ms
# 🔒 Приватность: Нулевое разглашение
```

## Сравнение: Delay-based vs Crypto-based

| Метод | Время | Эффективность | UX |
|-------|-------|---------------|-----|
| **Time delays** | 1-48 часов | ❌ Низкая | 😡 Ужасная |
| **Stealth addresses** | <1 сек | ✅ Высокая | 😊 Отличная |
| **CoinJoin** | 1-2 мин | ✅ Высокая | 😊 Хорошая |
| **Submarine swaps** | 1-30 сек | ✅ Высокая | 😊 Отличная |
| **ZK proofs** | <1 сек | ✅ Максимальная | 😊 Отличная |

## Полный сценарий: Отправка средств

```python
from stealthpay.bridge.privacy import StealthAddressGenerator
from stealthpay.bridge.mixing import ChaumianCoinJoin

# 1. Генерируем stealth address для получателя (<1 сек)
generator = StealthAddressGenerator()
recipient_address = generator.get_payment_address()

# 2. CoinJoin для смешивания (1-2 мин)
coinjoin = ChaumianCoinJoin(min_anonymity_set=50)
job = await coinjoin.submit(
    amount=100000000,  # 1 BTC
    destination=recipient_address
)

# ⏱️ Общее время: 1-2 минуты
# 🔒 Anonymity set: 50+ участников
# 🔗 Невозможно отследить связь отправитель→получатель
```

## Почему НЕ time delays?

### Проблема time-based mixing:
1. **Не работает против корреляции** - аналитик всё равно видит паттерны
2. **UX катастрофа** - пользователь не будет ждать 24 часа
3. **Ложное чувство безопасности** - задержка ≠ анонимность
4. **Liquidity проблемы** - нужны реальные участники, не ожидание

### Почему криптография работает:
1. **Математическая гарантия** - невозможно взломать
2. **Мгновенно** - никакого ожидания
3. **Проверяемо** - любой может верифицировать
4. **Масштабируемо** - работает с любым количеством пользователей

## Метрики приватности

### Anonymity Set
- **Stealth addresses**: ∞ (уникальный адрес каждый раз)
- **CoinJoin**: 50-100+ (зависит от участников)
- **Submarine swaps**: Разрыв цепочки
- **Combined**: Multiplicative!

### Time to Privacy
- **Stealth**: <1 сек
- **CoinJoin**: 1-2 мин
- **Submarine**: 1-30 сек
- **Total pipeline**: <3 минуты

## Будущее: Ещё быстрее, ещё анонимнее

- [ ] **zk-SNARKs** для верификации в 10ms
- [ ] **Ring signatures** (Monero-style)
- [ ] **Confidential Transactions** (скрытие сумм)
- [ ] **MPC-based mixing** (без trusted coordinator)

## Вывод

> Privacy through cryptography, not obscurity.
> Приватность через математику, а не через скрытность.
