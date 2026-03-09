# Sthrip Privacy Features

## 🔒 INSTANT Maximum Privacy

> **Privacy through cryptography, not obscurity.**
> Приватность через математику, а не через ожидание.

## Принцип

❌ **НЕТ** бесполезных задержек - они не дают реальной безопасности
✅ **ЕСТЬ** криптографическая защита - математически не взламываемая

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                    INSTANT PRIVACY STACK                        │
├─────────────────────────────────────────────────────────────────┤
│  🔐 Stealth Addresses    <1 sec   │  One-time, unlinkable       │
├─────────────────────────────────────────────────────────────────┤
│  🌪️ CoinJoin            1-2 min   │  50+ participants, real mix │
├─────────────────────────────────────────────────────────────────┤
│  ⚡ Submarine Swaps      1-30 sec  │  Atomic, Lightning-fast     │
├─────────────────────────────────────────────────────────────────┤
│  🛡️ ZK Proofs           <1 sec    │  Zero-knowledge verification│
├─────────────────────────────────────────────────────────────────┤
│  🕵️ Tor Hidden Service  instant   │  IP hidden, .onion routing  │
└─────────────────────────────────────────────────────────────────┘

Total time: 1-3 minutes for MAXIMUM privacy
```

## Компоненты

### 1. Stealth Addresses (Мгновенно)
```python
from sthrip.bridge.privacy import StealthAddressGenerator

generator = StealthAddressGenerator()
stealth = generator.generate_stealth_address(
    scan_key=recipient_scan,
    spend_key=recipient_spend
)
# ⏱️ <100ms
# 🔒 Уникальный адрес каждый раз
```

### 2. CoinJoin (1-2 минуты)
```python
from sthrip.bridge.mixing import ChaumianCoinJoin

coinjoin = ChaumianCoinJoin(min_anonymity_set=50)
round_id = await coinjoin.start_round()
# ⏱️ 1-2 min (пока наберётся 50 участников)
# 🔒 Anonymity set = 50+
```

### 3. Submarine Swaps (Мгновенно)
```python
from sthrip.bridge.mixing import SubmarineSwapService

service = SubmarineSwapService()
swap = await service.create_swap_in(amount, refund_addr)
# ⏱️ 1-30 sec (atomic)
# 🔒 Разрыв цепочки анализа
```

### 4. Zero-Knowledge Proofs (Мгновенно)
```python
from sthrip.bridge.privacy import ZKVerifier

proof = verifier.generate_ownership_proof(sk, pk)
# ⏱️ <500ms
# 🔒 Нулевое разглашение
```

## Почему НЕТ time delays?

| Проблема time delays | Решение криптографией |
|---------------------|----------------------|
| ⏰ Ждать 24-48 часов | ⚡ <3 минуты |
| ❌ Ложная безопасность | ✅ Математическая гарантия |
| 😡 Ужасный UX | 😊 Отличный UX |
| 🔍 Корреляция видна | 🛡️ Невозможно отследить |

## Сценарии использования

### Отправка $1000 (Максимальная приватность)
```python
# 1. Stealth address (<1 sec)
# 2. CoinJoin (1-2 min, 50 участников)
# 3. Submarine swap (optional, 10 sec)
# 
# Итого: 2-3 минуты
# Результат: Невозможно отследить
```

### Трейдинг (Скорость + Приватность)
```python
# 1. Stealth address only (<1 sec)
#
# Итого: Мгновенно
# Результат: IP скрыт, адрес уникальный
```

## Метрики

| Компонент | Время | Anonymity Set |
|-----------|-------|---------------|
| Stealth | <1 sec | ∞ (unique) |
| CoinJoin | 1-2 min | 50-100 |
| Submarine | 1-30 sec | Chain break |
| **Combined** | **<3 min** | **50+ × unlinkable** |

## Документация

- [Instant Privacy Guide](docs/PRIVACY_INSTANT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Threat Model](docs/THREAT_MODEL.md)

## Будущее

- [ ] Ring signatures (Monero-style)
- [ ] Confidential Transactions
- [ ] zk-SNARKs для 10ms verification
- [ ] MPC-based mixing без coordinator
