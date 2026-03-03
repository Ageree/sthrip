# Testing Checklist for StealthPay

## Pre-flight Checks ✅

### Environment Setup
- [ ] Python 3.10+ installed
- [ ] Node.js 18+ installed
- [ ] `.env` file configured
- [ ] Sepolia test ETH obtained (минимум 0.1 ETH)
- [ ] Contracts deployed on Sepolia
- [ ] TSS service built (optional)

### Safety Check
- [ ] Using testnet ONLY (Sepolia, not mainnet)
- [ ] Private key has test funds only (worth $0)
- [ ] Never committing keys to git
- [ ] `.env` in `.gitignore`

---

## Phase 1: Component Tests (No Money) ✅

```bash
# Run all component tests
python3 scripts/test_components.py
```

### Expected Results:
- [ ] Stealth Address Generation: PASS
- [ ] Stealth Address Ownership: PASS
- [ ] ZK Proof Generation: PASS
- [ ] ZK Range Proof: PASS
- [ ] Onion Address Book: PASS
- [ ] Multiple Stealth Addresses: PASS
- [ ] Key Recovery: PASS

**Time:** ~30 seconds  
**Cost:** $0

---

## Phase 2: Contract Deployment (Sepolia) ✅

```bash
cd contracts
npx hardhat run scripts/deploy.js --network sepolia
```

### Checklist:
- [ ] Contracts compile without errors
- [ ] Tests pass: `npx hardhat test`
- [ ] Deployment successful
- [ ] Addresses saved to `deployment-sepolia.json`
- [ ] Verified on Etherscan (optional)

**Time:** ~5 minutes  
**Cost:** ~0.001 Sepolia ETH (free)

---

## Phase 3: Single E2E Test (Small Amount) ⚠️

```bash
# Minimum test amount: 0.001 Sepolia ETH
python3 scripts/test_e2e_sepolia.py
```

### Before Test:
- [ ] Component tests passed
- [ ] Contracts deployed
- [ ] At least 0.01 Sepolia ETH available
- [ ] MPC node running (local or docker)

### During Test:
- [ ] Transaction submitted successfully
- [ ] Event emitted correctly
- [ ] Lock ID generated
- [ ] Gas cost reasonable (< 0.001 ETH)

### After Test:
- [ ] Sepolia TX confirmed
- [ ] MPC node processed event
- [ ] XMR (stagenet) received (if setup)

**Time:** ~3-5 minutes  
**Cost:** 0.001 Sepolia ETH (free)

---

## Phase 4: Multiple Tests (Increasing Amounts) ⚠️

| Test | Amount | Expected Time | Pass Criteria |
|------|--------|---------------|---------------|
| #1 | 0.001 ETH | 3 min | ✅ TX confirmed |
| #2 | 0.005 ETH | 3 min | ✅ Event parsed |
| #3 | 0.01 ETH | 3 min | ✅ MPC processed |
| #4 | 0.05 ETH | 3 min | ✅ All logs clean |
| #5 | 0.1 ETH | 5 min | ✅ End-to-end success |

### For Each Test:
- [ ] Record transaction hash
- [ ] Record gas used
- [ ] Record time to confirmation
- [ ] Check MPC node logs
- [ ] Verify no errors

---

## Phase 5: Edge Cases & Error Handling ⚠️

### Test Cases:
- [ ] Invalid XMR address (should reject)
- [ ] Insufficient balance (should reject)
- [ ] Double spend attempt (should reject)
- [ ] Expired lock refund (should work after timeout)
- [ ] Very small amount (0.0001 ETH)
- [ ] Multiple rapid transactions

---

## Phase 6: Long-running Test (Optional) ⚠️

```bash
# Run for 24 hours with small amounts
docker-compose -f docker-compose.testnet.yml up
```

- [ ] System stable for 24h
- [ ] No memory leaks
- [ ] No crashes
- [ ] All transactions processed

---

## Success Criteria ✅

### Must Pass:
- [ ] All component tests pass
- [ ] Contract deployment successful
- [ ] E2E test with 0.001 ETH successful
- [ ] Gas costs reasonable
- [ ] No critical errors in logs

### Should Pass:
- [ ] E2E tests up to 0.1 ETH
- [ ] Edge case handling correct
- [ ] Error messages clear
- [ ] Documentation accurate

### Nice to Have:
- [ ] 24-hour stability test
- [ ] Multiple MPC nodes tested
- [ ] Stress test (many concurrent TX)

---

## Failure Protocol 🚨

### If Test Fails:
1. **Stop immediately** - don't continue spending
2. Save all logs
3. Document exact failure
4. Create issue with:
   - Transaction hash
   - Error message
   - Logs
   - Steps to reproduce

### Emergency Stop:
```bash
# Stop all services
docker-compose down

# Check no processes running
ps aux | grep stealthpay

# Verify no pending transactions
# (Check Sepolia explorer)
```

---

## Post-Test Actions ✅

### After All Tests Pass:
1. **Document results**
   - Save all transaction hashes
   - Record gas costs
   - Note any warnings

2. **Clean up**
   - Remove test files
   - Clear logs (keep backups)
   - Reset environment

3. **Prepare for next phase**
   - Code audit
   - Bug bounty
   - Mainnet preparation

---

## Sign-off

**Tester:** _________________  
**Date:** _________________  
**Tests Passed:** ___/___  
**Total Sepolia ETH Spent:** _______  
**Notes:** ___________________________________________

---

**⚠️ REMEMBER:**
- Sepolia ETH is free and worthless
- Never use mainnet until audit
- Report all bugs immediately
- Document everything
