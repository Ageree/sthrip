# Sthrip Threat Model

## Scope

### In Scope
- Smart contracts (Bridge, Insurance, Oracle)
- TSS implementation and key management
- MPC node communication (P2P)
- User-facing API and CLI

### Out of Scope
- Ethereum consensus layer
- Monero protocol
- User wallet security
- Physical security of node operators

## Threat Actors

| Actor | Capability | Motivation |
|-------|-----------|------------|
| External Attacker | Network access, limited funds | Financial gain |
| Malicious User | Normal usage access | Free money, disruption |
| Compromised Node | One MPC node | Steal funds, disrupt |
| Insider (Operator) | One MPC node + infrastructure | Financial gain |
| Advanced Persistent | Multiple nodes, long-term | Mass theft |

## Threat Scenarios

### 1. Smart Contract Exploits

#### Reentrancy Attack
- **Risk**: High
- **Impact**: Fund drainage
- **Mitigation**: ReentrancyGuard, checks-effects-interactions

#### Integer Overflow
- **Risk**: Medium
- **Impact**: Incorrect calculations
- **Mitigation**: Solidity 0.8.x built-in overflow checks

#### Access Control Bypass
- **Risk**: High
- **Impact**: Unauthorized operations
- **Mitigation**: OpenZeppelin AccessControl, multi-sig admin

### 2. TSS/Key Management Attacks

#### Key Share Theft
- **Risk**: Critical
- **Impact**: Signature forgery
- **Mitigation**: 
  - HSM storage
  - Network isolation
  - No key share transmission over network

#### Threshold Bypass
- **Risk**: Critical
- **Impact**: Single party control
- **Mitigation**:
  - 3-of-5 threshold
  - Geographic distribution
  - Independent operators

#### Side-Channel Attacks
- **Risk**: Medium
- **Impact**: Key extraction
- **Mitigation**:
  - Constant-time operations
  - HSM protection
  - Regular resharing

### 3. P2P Network Attacks

#### Man-in-the-Middle
- **Risk**: High
- **Impact**: Message tampering
- **Mitigation**: mTLS with certificate pinning

#### Eclipse Attack
- **Risk**: Medium
- **Impact**: Partition network
- **Mitigation**:
  - Bootstrap nodes
  - Connection diversity
  - Health monitoring

#### DDoS
- **Risk**: Medium
- **Impact**: Service disruption
- **Mitigation**:
  - Rate limiting
  - Resource quotas
  - Multiple regions

### 4. Oracle Attacks

#### Price Manipulation
- **Risk**: High
- **Impact**: Unfair rates
- **Mitigation**:
  - Multi-source aggregation
  - Outlier detection
  - TWAP

#### Oracle Compromise
- **Risk**: Critical
- **Impact**: Arbitrary price setting
- **Mitigation**:
  - Decentralized oracle network
  - Consensus requirement
  - Circuit breakers

### 5. Bridge-Specific Attacks

#### Front-Running
- **Risk**: Medium
- **Impact**: MEV extraction
- **Mitigation**:
  - Commit-reveal pattern
  - Batch processing
  - Private mempool

#### Double Spend
- **Risk**: Critical
- **Impact**: Infinite money
- **Mitigation**:
  - Transaction confirmation requirements
  - Hash uniqueness checks
  - Insurance fund

#### Liquidity Drain
- **Risk**: High
- **Impact**: Bridge insolvency
- **Mitigation**:
  - Rate limits
  - Balance monitoring
  - Circuit breakers

## Risk Assessment Matrix

| Threat | Likelihood | Impact | Risk Level | Priority |
|--------|-----------|--------|------------|----------|
| Smart contract bug | Low | Critical | High | P0 |
| Key share theft | Low | Critical | High | P0 |
| P2P MITM | Medium | High | High | P0 |
| Price manipulation | Medium | Medium | Medium | P1 |
| DDoS | High | Low | Medium | P1 |
| Front-running | Medium | Low | Low | P2 |

## Security Controls

### Preventive
- Formal verification (where possible)
- Comprehensive testing (>90% coverage)
- Multi-signature admin
- Rate limiting
- Input validation

### Detective
- Monitoring and alerting
- Anomaly detection
- Audit logging
- Balance checks

### Corrective
- Emergency pause
- Insurance fund
- Upgrade mechanisms
- Incident response plan

## Audit Requirements

### Smart Contracts
- [ ] Reentrancy analysis
- [ ] Access control review
- [ ] Gas optimization
- [ ] Front-running analysis

### TSS Implementation
- [ ] Cryptographic review
- [ ] Side-channel analysis
- [ ] Key management audit

### Infrastructure
- [ ] Network security
- [ ] HSM configuration
- [ ] Deployment security

## Incident Response

### Severity Levels
1. **Critical**: Active fund drainage
2. **High**: Potential fund loss
3. **Medium**: Service disruption
4. **Low**: Minor issues

### Response Procedures
1. Detect anomaly via monitoring
2. Assess severity
3. Execute response:
   - Critical: Emergency pause
   - High: Restrict operations
   - Medium: Investigate
4. Post-incident review
5. Update threat model
