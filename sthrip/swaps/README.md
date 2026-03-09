# Sthrip Atomic Swaps

Модуль для атомарных свопов между Bitcoin и Monero (BTC↔XMR).

## Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│                    Atomic Swap Flow                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Alice (Seller)                    Bob (Buyer)             │
│  ──────────────                    ───────────             │
│                                                             │
│  1. Create XMR wallet              1. Create XMR wallet    │
│     (multisig prepare)                (multisig prepare)   │
│                                                             │
│  2. Share multisig_info    <───>   2. Share multisig_info  │
│                                                             │
│  3. Create 2-of-2 multisig         3. Create 2-of-2 multisig│
│                                                             │
│  4. Fund XMR ──────────────────>   4. Verify funding       │
│                                                             │
│  5. Wait for BTC HTLC    <───────  5. Create BTC HTLC      │
│       (with preimage hash)                                  │
│                                                             │
│  6. Claim BTC ─────────────────>   6. See claim tx         │
│     (reveals preimage)                 (get preimage)      │
│                                                             │
│  7. XMR spent            <───────  7. Claim XMR            │
│     (using preimage)                                        │
│                                                             │
│  Result: -1 XMR, +0.01 BTC        Result: -0.01 BTC, +1 XMR│
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Компоненты

### Bitcoin HTLC (`btc/`)
- `rpc_client.py` - Подключение к Bitcoin Core
- `htlc.py` - Создание HTLC скриптов и адресов
- `watcher.py` - Мониторинг транзакций

### Monero Multi-sig (`xmr/`)
- `wallet.py` - RPC клиент для Monero wallet
- `multisig.py` - Управление 2-of-2 multisig

### Coordinator (`coordinator.py`)
Оркестрация полного цикла свопа

## Установка

```bash
# Bitcoin Core
# Установите и настройте bitcoind с RPC доступом

# Monero
# Установите monerod и monero-wallet-rpc

# Python зависимости
pip install ecdsa requests
```

## Быстрый старт

### Как продавец XMR (Alice)

```python
from decimal import Decimal
from sthrip.swaps.coordinator import SwapFactory
from sthrip.swaps.btc.rpc_client import BitcoinRPCClient
from sthrip.swaps.xmr.wallet import MoneroWallet

# Подключение к нодам
btc_rpc = BitcoinRPCClient(
    host="localhost",
    port=18443,
    username="bitcoin",
    password="bitcoin",
    network="regtest"
)

xmr_wallet = MoneroWallet(
    host="localhost",
    port=38082,  # Stagenet
    username="monero",
    password="monero",
    wallet_name="alice_swap"
)

# Создаем своп
swap = SwapFactory.create_seller_swap(
    btc_rpc,
    xmr_wallet,
    btc_amount=Decimal("0.01"),
    xmr_amount=Decimal("1.0"),
    receive_btc_address="bc1q..."
)

# Выполняем своп
async def run():
    # 1. Настраиваем multisig с Bob
    our_info = await swap.setup_xmr_multisig(bob_multisig_info)
    
    # 2. Фандим XMR
    funding_txid = await swap.fund_xmr()
    
    # 3. Ждем Bitcoin HTLC от Bob
    htlc = await swap.wait_for_btc_htlc()
    
    # 4. Забираем BTC (раскрываем preimage)
    claim_txid = await swap.claim_btc(swap.state.preimage, htlc)
    
    print(f"Swap completed! Claimed BTC in {claim_txid}")

asyncio.run(run())
```

### Как покупатель XMR (Bob)

```python
from sthrip.swaps.coordinator import SwapFactory

# Создаем своп
swap = SwapFactory.create_buyer_swap(
    btc_rpc,
    xmr_wallet,
    btc_amount=Decimal("0.01"),
    xmr_amount=Decimal("1.0"),
    receive_xmr_address="44..."
)

async def run():
    # 1. Настраиваем multisig с Alice
    await swap.setup_xmr_multisig(alice_multisig_info)
    
    # 2. Проверяем funding от Alice
    assert await swap.verify_xmr_funding()
    
    # 3. Создаем Bitcoin HTLC
    import secrets
    import hashlib
    
    preimage = secrets.token_hex(32)
    preimage_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    
    htlc = await swap.create_btc_htlc(
        alice_pubkey,
        preimage_hash
    )
    
    # 4. Ждем пока Alice заберет BTC
    # (получаем preimage из Bitcoin blockchain)
    
    # 5. Забираем XMR
    claim_txid = await swap.claim_xmr(preimage)
    
    print(f"Swap completed! Claimed XMR in {claim_txid}")

asyncio.run(run())
```

## Тестирование

### Unit тесты

```bash
cd /Users/saveliy/Documents/Agent Payments/sthrip
python -m pytest tests/swaps/ -v
```

### Интеграционное тестирование (требует нод)

```bash
# Запустите Bitcoin regtest
bitcoind -regtest -daemon

# Запустите Monero stagenet
monerod --stagenet --detach

# Запустите тесты
python -m pytest tests/swaps/integration/ -v --network=regtest
```

### Демо без нод

```bash
python examples/atomic_swap_demo.py
```

## Технические детали

### Bitcoin HTLC Script

```
OP_SHA256 <32-byte-hash> OP_EQUAL
OP_IF
    <recipient-pubkey>
OP_ELSE
    <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
    <sender-pubkey>
OP_ENDIF
OP_CHECKSIG
```

- **Claim path**: Получатель предоставляет preimage + подпись
- **Refund path**: Отправитель ждет timelock + подпись

### Monero 2-of-2 Multi-sig

- Оба участника должны подписать для траты
- Используется Monero's native multisig через wallet RPC
- Требует несколько раундов обмена ключами

## Безопасность

⚠️ **Важные замечания**:

1. **Тестируйте в testnet/stagenet** перед mainnet
2. **Начинайте с маленьких сумм** (< $100)
3. **Проверяйте timelock** - достаточно ли времени для refund
4. **Мониторьте сеть** - не пропустите действия контрагента
5. **Используйте свежие ключи** для каждого свопа

## Ограничения MVP

- Только cooperative parties (нужен refund если контрагент не отвечает)
- Ручной процесс (не fully automated)
- Требуется доверие к timelock (сетевые задержки)
- Нет встроенного rate discovery

## Roadmap

- [ ] Интеграция с COMIT protocol
- [ ] Automated preimage handling
- [ ] Refund automation
- [ ] Rate discovery (Oracles/DEX)
- [ ] GUI интерфейс

## Лицензия

MIT
