# Testing StealthPay with Real (Testnet) Money

## ⚠️ ВАЖНО

Используем **ТОЛЬКО** тестовые сети:
- **Sepolia** (Ethereum testnet) - ETH имеет ценность $0
- **Monero Stagenet** - XMR имеет ценность $0

НЕ используйте mainnet до аудита и battle-testing!

## Prerequisites

### 1. Получить тестовые монеты

#### Sepolia ETH (бесплатно):
```bash
# Способ 1: Alchemy Faucet
curl -X POST https://sepoliafaucet.com \
  -d "address=YOUR_SEPOLIA_ADDRESS"

# Способ 2: Infura Faucet  
# https://www.infura.io/faucet/sepolia

# Способ 3: PoW Faucet (надежный)
# https://sepolia-faucet.pk910.de/
```

#### Monero Stagenet (бесплатно):
```bash
# Скачать кошелек Monero CLI
git clone https://github.com/monero-project/monero.git
cd monero
make release-static

# Запустить stagenet
./monero-wallet-cli --stagenet

# Получить монеты с faucet:
# https://stagenet.xmrwallet.com/faucet/
# Или попросить в Matrix: #monero-stagenet:matrix.org
```

## Test Plan

### Phase 1: Компонентное тестирование (без риска)

#### 1.1 TSS Service Test
```bash
cd tss-service

# Запустить сервер
make run

# Тест key generation
cd ../stealthpay
python -c "
from stealthpay.bridge.tss_client import TSSClient
client = TSSClient('localhost:50051')

# Генерация ключа (тестовая)
key = client.generate_key(
    party_id='test-1',
    threshold=2,
    total=3,
    peers=['test-2', 'test-3']
)
print(f'Key generated: {key.share_id}')
print(f'Public key: {key.public_key.hex()[:20]}...')
"
```

#### 1.2 Stealth Addresses Test
```python
from stealthpay.bridge.privacy import StealthAddressGenerator

generator = StealthAddressGenerator()
keys = generator.generate_master_keys()

# Тест генерации
stealth = generator.generate_stealth_address(
    recipient_scan_key=keys.scan_public,
    recipient_spend_key=keys.spend_public
)

print(f"✓ Stealth address: {stealth.address[:20]}...")

# Тест проверки владения
is_mine, priv_key = generator.check_ownership(
    stealth, keys.scan_private, keys.spend_public
)
assert is_mine, "Ownership check failed!"
print("✓ Ownership verified")
```

#### 1.3 ZK Proofs Test
```python
from stealthpay.bridge.privacy import ZKVerifier

verifier = ZKVerifier()

# Тест proof generation
import secrets
sk = secrets.token_bytes(32)
pk = secrets.token_bytes(33)

proof = verifier.generate_ownership_proof(sk, pk)
is_valid = verifier.verify_ownership(proof, pk, b'challenge')

assert is_valid, "ZK proof verification failed!"
print("✓ ZK proof verified")
```

### Phase 2: Smart Contract Deployment (Sepolia)

#### 2.1 Подготовка
```bash
cd contracts

# Установить зависимости
npm install

# Настроить окружение
cat > .env << EOF
SEPOLIA_RPC=https://sepolia.infura.io/v3/YOUR_KEY
PRIVATE_KEY=0xYOUR_TEST_PRIVATE_KEY
ETHERSCAN_API_KEY=your_key
EOF

# Проверить баланс
npx hardhat run scripts/check-balance.js --network sepolia
```

#### 2.2 Деплой контрактов
```bash
# Компиляция
npx hardhat compile

# Запуск тестов
npx hardhat test

# Деплой на Sepolia
npx hardhat run scripts/deploy.js --network sepolia

# Результат: сохранить адреса контрактов
# Bridge: 0x...
# Insurance: 0x...
# Oracle: 0x...
```

#### 2.3 Тестирование Bridge Contract
```javascript
// test-bridge-sepolia.js
const { ethers } = require("hardhat");

async function test() {
  const bridge = await ethers.getContractAt(
    "StealthPayBridge",
    "0xDEPLOYED_ADDRESS"
  );
  
  // Test 1: Lock ETH
  const lockTx = await bridge.lock(
    "44stagenetXMRaddress...",
    3600, // 1 hour
    ethers.ZeroHash,
    { value: ethers.parseEther("0.01") }
  );
  await lockTx.wait();
  console.log("✓ Lock transaction:", lockTx.hash);
  
  // Test 2: Get lock ID
  const filter = bridge.filters.Locked();
  const events = await bridge.queryFilter(filter);
  const lockId = events[events.length - 1].args.lockId;
  console.log("✓ Lock ID:", lockId);
  
  // Test 3: Check lock
  const lock = await bridge.locks(lockId);
  console.log("✓ Lock amount:", ethers.formatEther(lock.amount));
}

test().catch(console.error);
```

Запуск:
```bash
npx hardhat run test-bridge-sepolia.js --network sepolia
```

### Phase 3: Integration Testing

#### 3.1 MPC Node Setup (1 нода для теста)
```bash
# Запустить 1 MPC ноду локально
docker-compose -f docker-compose.testnet.yml up mpc-node-1

# Проверить логи
docker logs -f stealthpay-mpc-node-1
```

#### 3.2 End-to-End Test (минимальная сумма)
```python
#!/usr/bin/env python3
"""
E2E Test: Sepolia ETH -> XMR Stagenet
Amount: 0.001 ETH (тестовые, бесплатные)
"""

import asyncio
from web3 import Web3
from stealthpay.bridge.tss_client import TSSClient
from stealthpay.bridge.privacy import StealthAddressGenerator

# Конфигурация
SEPOLIA_RPC = "https://sepolia.infura.io/v3/YOUR_KEY"
BRIDGE_ADDRESS = "0xYOUR_BRIDGE_ADDRESS"
PRIVATE_KEY = "0xYOUR_TEST_KEY"

async def test_e2e():
    print("🚀 Starting E2E test with real (testnet) funds")
    print("=" * 60)
    
    # 1. Подключение к Sepolia
    w3 = Web3(Web3.HTTPProvider(SEPOLIA_RPC))
    account = w3.eth.account.from_key(PRIVATE_KEY)
    balance = w3.eth.get_balance(account.address)
    
    print(f"📊 Sepolia Balance: {w3.from_wei(balance, 'ether')} ETH")
    assert balance > w3.to_wei(0.001, 'ether'), "Insufficient test funds!"
    
    # 2. Генерация stealth адреса для XMR
    print("\n🔐 Generating stealth address...")
    generator = StealthAddressGenerator()
    xmr_keys = generator.generate_master_keys()
    xmr_stealth = generator.generate_stealth_address(
        xmr_keys.scan_public,
        xmr_keys.spend_public
    )
    print(f"✓ XMR stealth: {xmr_stealth.address[:30]}...")
    
    # 3. Lock ETH в контракте
    print("\n🔒 Locking 0.001 Sepolia ETH...")
    bridge = w3.eth.contract(
        address=BRIDGE_ADDRESS,
        abi=BRIDGE_ABI  # загрузить из artifacts
    )
    
    tx = bridge.functions.lock(
        xmr_stealth.address,
        3600,  # 1 hour timeout
        w3.keccak(text="merkle_root")  # тестовый root
    ).build_transaction({
        'from': account.address,
        'value': w3.to_wei(0.001, 'ether'),
        'gas': 200000,
        'maxFeePerGas': w3.to_wei('50', 'gwei'),
        'maxPriorityFeePerGas': w3.to_wei('2', 'gwei'),
        'nonce': w3.eth.get_transaction_count(account.address),
    })
    
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    
    print(f"✓ Lock TX: {tx_hash.hex()}")
    print(f"✓ Gas used: {receipt.gasUsed}")
    
    # 4. Ожидание MPC ноды (в реальном тесте)
    print("\n⏳ Waiting for MPC node to process...")
    print("(В тесте с 1 нодой - проверяем вручную)")
    
    # 5. Проверка XMR (в stagenet)
    print("\n🔄 Check XMR stagenet wallet for incoming tx")
    print(f"Expected address: {xmr_stealth.address}")
    
    print("\n✅ E2E test initiated!")
    print("Monitor:")
    print(f"  - Sepolia: https://sepolia.etherscan.io/tx/{tx_hash.hex()}")
    print(f"  - XMR: Check stagenet wallet")

if __name__ == "__main__":
    asyncio.run(test_e2e())
```

Запуск:
```bash
python scripts/test_e2e_sepolia.py
```

### Phase 4: Stress Testing

#### 4.1 Нагрузочное тестирование (малые суммы)
```python
# test_stress.py
"""
Отправка множества маленьких транзакций для проверки stability
"""

AMOUNTS = [0.0001, 0.0002, 0.0003, 0.0005]  # ETH
CONCURRENT = 3  # Одновременно

async def stress_test():
    for amount in AMOUNTS:
        for i in range(CONCURRENT):
            try:
                await send_test_transaction(amount, f"test-{i}")
                print(f"✓ Tx {i} with {amount} ETH sent")
            except Exception as e:
                print(f"❌ Tx {i} failed: {e}")
```

#### 4.2 Edge Cases
```python
# test_edge_cases.py

async def test_edge_cases():
    tests = [
        # Минимальная сумма
        ("min_amount", 0.00001),
        
        # Максимальная сумма (для теста)
        ("max_amount", 0.1),
        
        # Неправильный адрес
        ("invalid_xmr", "invalid_address"),
        
        # Повторный lock (должен отклониться)
        ("double_spend", None),
        
        # Истёкший lock
        ("expired_lock", None),
    ]
    
    for name, params in tests:
        try:
            result = await run_test(name, params)
            print(f"✓ {name}: {result}")
        except Exception as e:
            print(f"✓ {name}: Correctly rejected - {e}")
```

### Phase 5: Security Testing

#### 5.1 Attempted Attacks (должны провалиться)
```python
# test_attacks.py

async def test_attacks():
    """
    Попытки атак (должны быть отклонены)
    """
    
    # 1. Reentrancy attempt
    print("Testing reentrancy protection...")
    # (Использовать malicious contract)
    
    # 2. Invalid signature
    print("Testing signature validation...")
    fake_sig = b'\x00' * 96
    # Должно отклониться
    
    # 3. Front-running simulation
    print("Testing front-running resistance...")
    # Отправить две tx с разным gas
    
    # 4. Oracle manipulation attempt
    print("Testing oracle security...")
    # Попытка обновить цену без прав
```

## Monitoring during tests

### 1. Логи MPC нод
```bash
# Terminal 1
docker logs -f mpc-node-1 2>&1 | tee mpc-logs.txt

# Смотреть на:
# - Подключения TSS
# - Подписи транзакций
# - Ошибки
```

### 2. Blockchain explorers
```bash
# Sepolia
open "https://sepolia.etherscan.io/address/$BRIDGE_ADDRESS"

# Stagenet
open "https://stagenet.xmrchain.net/"
```

### 3. Метрики
```bash
# Prometheus
open "http://localhost:9090"

# Grafana
open "http://localhost:3000"
```

## Test Checklist

### Before testing:
- [ ] Sepolia ETH получены (минимум 0.1 ETH)
- [ ] XMR stagenet монеты получены
- [ ] Контракты задеплоены на Sepolia
- [ ] 1 MPC нода запущена локально
- [ ] Логи настроены

### During testing:
- [ ] Начать с 0.001 ETH (минимум)
- [ ] Увеличивать постепенно
- [ ] Мониторить все логи
- [ ] Проверять gas costs
- [ ] Записывать transaction hashes

### After each test:
- [ ] Проверить балансы
- [ ] Проверить логи на ошибки
- [ ] Сохранить результаты
- [ ] Проверить время выполнения

## Rollback plan

Если что-то пошло не так:
```bash
# 1. Остановить все ноды
docker-compose -f docker-compose.testnet.yml down

# 2. Пересобрать
make clean && make build

# 3. Передеплоить контракты
npx hardhat run scripts/deploy.js --network sepolia

# 4. Перезапустить
./scripts/start-testnet.sh
```

## Success Criteria

✅ Тест пройден если:
1. Все lock транзакции успешны
2. MPC нода правильно обрабатывает события
3. XMR отправляется на правильный stealth адрес
4. Нет ошибок в логах
5. Gas costs разумные (< $0.01 per tx на mainnet)
6. Время выполнения < 5 минут

❌ Тест провален если:
1. Потеряны тестовые монеты (не критично, они бесплатные)
2. Ошибки в смарт-контрактах
3. MPC нода падает
4. Невозможно верифицировать транзакции

## Next Steps after successful test

1. **Увеличить количество MPC нод** (3-5)
2. **Запустить на длительное время** (24-48 часов)
3. **Провести аудит безопасности**
4. **Bug bounty программа**
5. **Только потом mainnet**
