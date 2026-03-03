# Cross-Chain Bridge: ETH↔XMR

Phase 2 реализации: MPC-based bridge для переводов между Ethereum и Monero.

## Архитектура

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│   Ethereum   │ ←─────→ │   MPC Pool   │ ←─────→ │    Monero    │
│   Contract   │         │  (3-of-5)    │         │   Multi-sig  │
└──────────────┘         └──────────────┘         └──────────────┘
        │                       │                        │
        │  1. User locks ETH    │                        │
        │ ─────────────────────>│                        │
        │                       │  2. MPC detects lock   │
        │                       │                        │
        │                       │  3. MPC sends XMR      │
        │                       │ ──────────────────────>│
        │                       │                        │
        │                       │  4. User receives XMR  │
        │                       │                        │
        │                       │  5. MPC claims ETH     │
        │ <─────────────────────│                        │
```

## Компоненты

### Ethereum Smart Contract
- **Lock ETH**: Блокировка ETH для получения XMR
- **Claim**: Получение ETH с MPC подписью
- **Refund**: Возврат ETH после таймаута

### MPC Relayer Network
- **5 нод** с 3-of-5 threshold signature
- Каждая нода держит share приватного ключа
- Консенсус для подписи транзакций

### Monero Multi-sig
- **5-of-5 multi-sig** wallet
- Контролируется MPC нодами
- Требуется 3 подписи для траты

## Установка

```bash
# Установка web3 для Ethereum
pip install web3

# Установка TSS library (опционально, для production)
pip install tss-lib
```

## Конфигурация нод

### 1. Настройка Ethereum ноды

```yaml
# config/eth_node.yaml
ethereum:
  rpc_url: "https://mainnet.infura.io/v3/YOUR_KEY"
  contract_address: "0x..."
  private_key: "${ETH_PRIVATE_KEY}"
  
mpc:
  node_id: "mpc_node_1"
  threshold: 3
  peers:
    - "mpc_node_2"
    - "mpc_node_3"
    - "mpc_node_4"
    - "mpc_node_5"
```

### 2. Настройка Monero ноды

```yaml
# config/xmr_node.yaml
monero:
  daemon_host: "localhost"
  daemon_port: 18081
  wallet_host: "localhost"
  wallet_port: 18082
  wallet_name: "mpc_wallet_1"
```

## Использование

### Как пользователь

```python
from stealthpay.bridge import BridgeCoordinator
from stealthpay.bridge.contracts import EthereumBridgeContract

# Подключение к bridge
bridge = EthereumBridgeContract(
    web3_provider="https://mainnet.infura.io/v3/...",
    contract_address="0x...",
    private_key="your_private_key"
)

# Bridge ETH -> XMR
transfer = await bridge_coordinator.bridge_eth_to_xmr(
    eth_amount=Decimal("0.1"),
    xmr_address="44...",
    sender_eth_address="0x...",
    duration_hours=24
)

print(f"Transfer ID: {transfer.transfer_id}")
print(f"Status: {transfer.status}")

# Проверка статуса
status = await bridge_coordinator.get_transfer_status(transfer.transfer_id)
```

### Запуск MPC ноды

```python
from stealthpay.bridge.relayers import MPCRelayerNode
from stealthpay.bridge.contracts import EthereumBridgeContract
from stealthpay.swaps.xmr.wallet import MoneroWallet

# Создание компонентов
eth_bridge = EthereumBridgeContract(...)
xmr_wallet = MoneroWallet(...)

# Создание MPC ноды
node = MPCRelayerNode(
    node_id="mpc_node_1",
    eth_bridge_contract=eth_bridge,
    xmr_wallet=xmr_wallet
)

# Запуск
await node.start()

# Статус
print(node.get_status())
```

## Деплой контракта

```solidity
// Deploy Bridge.sol to Ethereum
const Bridge = await ethers.getContractFactory("StealthPayBridge");
const bridge = await Bridge.deploy();
await bridge.deployed();

console.log("Bridge deployed to:", bridge.address);
```

## Генерация ключей MPC

```python
from stealthpay.bridge.relayers.mpc_node import TSSKeyGenerator

# Generate 5 key shares (3-of-5 threshold)
key_shares = TSSKeyGenerator.generate_key_shares(n=5, threshold=3)

# Distribute to nodes
for share in key_shares:
    print(f"Node {share.node_id}: {share.to_dict()}")
    # Save securely!
```

## CLI Commands

```bash
# Bridge ETH to XMR
stealthpay bridge eth-to-xmr \
    --amount 0.1 \
    --xmr-address 44... \
    --network mainnet

# Bridge XMR to ETH
stealthpay bridge xmr-to-eth \
    --amount 1.0 \
    --eth-address 0x... \
    --network mainnet

# Check transfer status
stealthpay bridge status --transfer-id <id>

# List transfers
stealthpay bridge list --status pending

# Run MPC node
stealthpay bridge run-node \
    --config /path/to/config.yaml \
    --node-id mpc_node_1
```

## Тестирование

```bash
# Unit тесты
python -m pytest tests/bridge/ -v

# Интеграционные тесты (требует нод)
python -m pytest tests/bridge/integration -v --integration

# Тестовый деплой (Sepolia + Stagenet)
python scripts/deploy_testnet.py
```

## Безопасность

### MPC Security
- **Key shares** never leave the node
- **Threshold signing** - нужно 3 из 5 нод
- **No single point of failure**
- **Byzantine fault tolerance** - до 2 злонамеренных нод

### Smart Contract Security
- **Timelock** - возврат если MPC не отвечает
- **Access control** - только MPC может claim
- **Emergency pause** - на случай атаки
- **Audit required** перед mainnet

### Операционная безопасность
- **Hardware Security Modules (HSM)** для ключей
- **Multi-datacenter** размещение нод
- **24/7 monitoring** и alerts
- **Insurance fund** для компенсаций

## Roadmap

- [x] Базовая архитектура MPC
- [x] Ethereum smart contract interface
- [x] Monero multi-sig integration
- [ ] Production TSS library integration
- [ ] P2P network между нодами
- [ ] Oracle для цен (Chainlink)
- [ ] L2 поддержка (Arbitrum, Optimism)
- [ ] Governance token
- [ ] Security audit

## Лицензия

MIT
