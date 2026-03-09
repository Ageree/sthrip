# Sthrip - Остаток работ (TODO)

## 🚨 CRITICAL (Блокеры для production)

### 1. Production TSS Library
**Статус:** ⚠️ Educational implementation  
**Приоритет:** CRITICAL  
**Оценка:** 2-3 недели

**Что нужно:**
- [ ] Интегрировать `binance-chain/tss-lib` (Go) через gRPC
- [ ] ИЛИ `silviupal/tss-lib` (Python wrapper)
- [ ] ИЛИ `ZenGo-X/multi-party-ecdsa` (Rust)
- [ ] Реализовать настоящий DKG (Distributed Key Generation)
- [ ] Добавить zero-knowledge proofs для верификации
- [ ] Защита от side-channel attacks

**Почему важно:** Текущая TSS реализация - образовательная, не безопасна для production.

---

### 2. Smart Contract Security Audit
**Статус:** ❌ Не проведен  
**Приоритет:** CRITICAL  
**Оценка:** $15,000-30,000 + 2-4 недели

**Что нужно:**
- [ ] Аудит Solidity кода от CertiK / OpenZeppelin / Trail of Bits
- [ ] Formal verification критичных функций
- [ ] Bug bounty program ($100,000+)
- [ ] Insurance fund для компенсаций

**Контракты для аудита:**
- `SthripBridge.sol` - ETH bridge
- HTLC factory (если деплоится на Ethereum)

---

### 3. HSM Integration
**Статус:** ⚠️ Заглушки  
**Приоритет:** CRITICAL  
**Оценка:** 1-2 недели + стоимость HSM

**Что нужно:**
- [ ] AWS KMS integration
- [ ] Azure Key Vault integration  
- [ ] Hashicorp Vault production setup
- [ ] YubiHSM 2 support (on-premise)
- [ ] Key ceremony procedures

**Почему важно:** MPC key shares должны храниться в HSM, не в файлах.

---

## 🔴 HIGH PRIORITY (Для testnet launch)

### 4. Oracle Price Feeds
**Статус:** ⚠️ Hardcoded rates  
**Приоритет:** HIGH  
**Оценка:** 3-5 дней

**Что нужно:**
- [ ] Chainlink Price Feeds integration (ETH/XMR)
- [ ] DEX liquidity oracles (Uniswap, Curve)
- [ ] Multiple oracle consensus (не доверять одному)
- [ ] Slippage protection (> 1% deviation = revert)
- [ ] Circuit breakers при extreme volatility

**Текущий код:**
```python
# Сейчас:
return eth_amount * Decimal("10")  # Hardcoded!

# Нужно:
return eth_amount * oracle.get_price("ETH/XMR")
```

---

### 5. P2P Security (mTLS)
**Статус:** ⚠️ Plain WebSocket  
**Приоритет:** HIGH  
**Оценка:** 1 неделя

**Что нужно:**
- [ ] mTLS для всех P2P соединений
- [ ] Certificate pinning
- [ ] Node identity verification
- [ ] Message signing с node keys
- [ ] Protection against MITM

---

### 6. Real Testnet Deployment
**Статус:** ❌ Не деплоено  
**Приоритет:** HIGH  
**Оценка:** 3-5 дней

**Что нужно:**
- [ ] Deploy bridge contract to Sepolia
- [ ] Deploy to Mumbai (Polygon testnet)
- [ ] Run 5 MPC nodes on testnet
- [ ] Testnet monitoring dashboard
- [ ] Faucet integration

---

## 🟡 MEDIUM PRIORITY (Улучшения)

### 7. Rate Limiting & DoS Protection
**Статус:** ❌ Отсутствует  
**Приоритет:** MEDIUM  
**Оценка:** 2-3 дня

**Что нужно:**
- [ ] Redis-based rate limiting
- [ ] Per-peer message limits
- [ ] IP banning for malicious nodes
- [ ] Resource usage quotas

---

### 8. Monitoring & Alerting
**Статус:** ⚠️ Prometheus/Grafana skeleton  
**Приоритет:** MEDIUM  
**Оценка:** 1 неделя

**Что нужно:**
- [ ] Custom metrics (swap volume, success rate)
- [ ] Alert manager rules
- [ ] PagerDuty/OpsGenie integration
- [ ] Real-time swap monitoring
- [ ] Anomaly detection (ML-based)

**Метрики:**
- Swap success/failure rate
- Average swap time
- MPC node health
- Bridge liquidity
- Gas costs

---

### 9. Database Layer
**Статус:** ⚠️ In-memory only  
**Приоритет:** MEDIUM  
**Оценка:** 3-5 дней

**Что нужно:**
- [ ] PostgreSQL для persistent storage
- [ ] Redis для кэширования
- [ ] Migration scripts
- [ ] Backup/restore procedures

---

### 10. CLI Improvements
**Статус:** ✅ Basic commands  
**Приоритет:** MEDIUM  
**Оценка:** 3-5 дней

**Что нужно:**
- [ ] Interactive swap wizard
- [ ] Progress bars для длительных операций
- [ ] Better error messages
- [ ] Config file support (YAML)
- [ ] Shell completions (bash/zsh)

---

## 🟢 LOW PRIORITY (Nice to have)

### 11. L2 Support
**Статус:** ❌ Не реализовано  
**Приоритет:** LOW  
**Оценка:** 2-3 недели

**Что нужно:**
- [ ] Arbitrum bridge
- [ ] Optimism bridge
- [ ] Base bridge
- [ ] Lower gas costs

---

### 12. Governance DAO
**Статус:** ❌ Не реализовано  
**Приоритет:** LOW  
**Оценка:** 2-3 недели

**Что нужно:**
- [ ] Governance token
- [ ] Voting mechanism
- [ ] Parameter changes (fees, thresholds)
- [ ] Emergency procedures

---

### 13. Mobile App
**Статус:** ❌ Не реализовано  
**Приоритет:** LOW  
**Оценка:** 4-6 недель

**Что нужно:**
- [ ] React Native или Flutter
- [ ] QR code scanning
- [ ] Push notifications
- [ ] Biometric auth

---

### 14. Analytics Dashboard
**Статус:** ❌ Не реализовано  
**Приоритет:** LOW  
**Оценка:** 1-2 недели

**Что нужно:**
- [ ] Web interface (React/Vue)
- [ ] Volume charts
- [ ] Recent swaps feed
- [ ] MPC node status
- [ ] Bridge statistics

---

## 📊 Общая оценка

| Категория | Задач | Оценка времени | Оценка бюджета |
|-----------|-------|----------------|----------------|
| CRITICAL | 3 | 4-6 недель | $50,000-100,000 |
| HIGH | 3 | 2-3 недели | $5,000-10,000 |
| MEDIUM | 4 | 2-3 недели | $2,000-5,000 |
| LOW | 4 | 8-12 недель | $20,000-40,000 |
| **ИТОГО** | **14** | **16-24 недели** | **$77,000-155,000** |

---

## 🎯 Рекомендуемый порядок

### Phase 1: Security (4-6 недель)
1. Production TSS library
2. Smart contract audit  
3. HSM integration
4. Bug bounty launch

### Phase 2: Testnet Launch (2-3 недели)
5. Oracle integration
6. P2P mTLS
7. Testnet deployment
8. Monitoring setup

### Phase 3: Mainnet Prep (2-3 недели)
9. Database layer
10. Rate limiting
11. Insurance fund
12. Final security review

### Phase 4: Scale (постепенно)
13. L2 support
14. Governance DAO
15. Mobile app
16. Analytics

---

## ✅ Что УЖЕ сделано

- [x] Atomic Swaps core logic
- [x] HTLC implementation
- [x] Monero multisig
- [x] TSS educational implementation
- [x] P2P network
- [x] CLI interface
- [x] Docker infrastructure
- [x] Unit tests (36 tests)
- [x] Testnet simulation
- [x] Security audit documentation
- [x] API structure

---

## 💡 Итог

**Минимум для production:**
- Production TSS library
- Smart contract audit
- HSM integration

**Минимум для testnet:**
- Oracle integration
- P2P mTLS
- Real deployment

**Готово сейчас:**
- ✅ Core logic
- ✅ Tests
- ✅ Documentation
- ✅ Docker setup
