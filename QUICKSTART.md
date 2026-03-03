# StealthPay Quick Start

Быстрый старт с StealthPay - Atomic Swaps и Cross-Chain Bridge.

## 🚀 Быстрый старт

### 1. Установка

```bash
# Клонирование
git clone https://github.com/yourusername/stealthpay.git
cd stealthpay

# Установка зависимостей
pip install -r requirements.txt

# Или с bridge extras
pip install -r requirements.txt -e ".[bridge]"
```

### 2. Запуск инфраструктуры

```bash
# Полная инфраструктура с Docker
./scripts/setup.sh dev

# Или вручную:
docker-compose -f docker-compose.full.yml up -d
```

### 3. Atomic Swap (BTC↔XMR)

**Как продавец XMR (Alice):**
```bash
# Создать своп
stealthpay swap create-seller \
    --btc-amount 0.01 \
    --xmr-amount 1.0 \
    --receive-btc bc1q...

# Настроить multisig
stealthpay swap setup-multisig \
    --swap-id <id> \
    --counterparty-info <bob_multisig_info>

# Фандить XMR
stealthpay swap fund-xmr --swap-id <id>

# Забрать BTC (после того как Bob создал HTLC)
stealthpay swap claim-btc \
    --swap-id <id> \
    --preimage <preimage>
```

**Как покупатель XMR (Bob):**
```bash
# Создать своп
stealthpay swap create-buyer \
    --btc-amount 0.01 \
    --xmr-amount 1.0 \
    --receive-xmr 44...

# Настроить multisig
stealthpay swap setup-multisig \
    --swap-id <id> \
    --counterparty-info <alice_multisig_info>

# Создать BTC HTLC
stealthpay swap create-btc-htlc \
    --swap-id <id> \
    --counterparty-pubkey <alice_pubkey>

# Забрать XMR (используя preimage из BTC claim)
stealthpay swap claim-xmr \
    --swap-id <id> \
    --preimage <preimage>
```

### 4. Cross-Chain Bridge (ETH↔XMR)

**Bridge ETH → XMR:**
```bash
stealthpay bridge eth-to-xmr \
    --amount 0.1 \
    --xmr-address 44... \
    --network testnet
```

**Bridge XMR → ETH:**
```bash
stealthpay bridge xmr-to-eth \
    --amount 1.0 \
    --eth-address 0x... \
    --network stagenet
```

**Запуск MPC ноды:**
```bash
# Сгенерировать ключи
python scripts/generate_mpc_keys.py --nodes 5 --threshold 3 --output ./keys/

# Запустить ноду
stealthpay bridge run-node \
    --config config/node1.yaml \
    --node-id mpc_node_1
```

## 🧪 Тестирование

```bash
# Unit тесты
python -m pytest tests/swaps tests/bridge -v

# Интеграционные тесты (требует ноды)
python -m pytest tests/swaps/integration -v --integration

# Демо без нод
python examples/atomic_swap_demo.py
```

## 📊 Мониторинг

- **Prometheus**: http://localhost:9090
- **Grafana**: http://localhost:3000 (admin/admin)
- **API Docs**: http://localhost:8000/docs

## 🔐 Безопасность

- Никогда не коммитьте приватные ключи
- Используйте `.env` для конфигурации
- Для production используйте HSM/Vault
- Начинайте с маленьких сумм

## 📚 Документация

- [Atomic Swaps](stealthpay/swaps/README.md)
- [Cross-Chain Bridge](stealthpay/bridge/README.md)
- [API Reference](docs/API.md)

## 🆘 Поддержка

- Issues: GitHub Issues
- Discord: [link]
- Email: support@stealthpay.io
