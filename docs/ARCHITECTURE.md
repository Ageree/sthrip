# Sthrip Architecture

## System Overview

Sthrip is a decentralized bridge between Ethereum and Monero, using Multi-Party Computation (MPC) for secure cross-chain transactions with **INSTANT maximum privacy**.

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   User Wallet   │────▶│  Sthrip     │────▶│  Monero Network │
│   (Ethereum)    │◀────│  Bridge (MPC)   │◀────│   (Stagenet)    │
└─────────────────┘     └─────────────────┘     └─────────────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         ▼                     ▼                     ▼
   ┌──────────┐         ┌──────────┐         ┌──────────┐
   │ MPC Node │◀───────▶│ MPC Node │◀───────▶│ MPC Node │
   │   (1)    │   P2P   │   (2)    │   P2P   │   (3)    │
   └──────────┘         └──────────┘         └──────────┘
         │                     │                     │
         └─────────────────────┼─────────────────────┘
                               ▼
                        ┌──────────┐
                        │   HSM    │
                        │ (Vault/  │
                        │ AWS KMS) │
                        └──────────┘
```

## Components

### 1. Smart Contracts (Ethereum)

- **SthripBridge.sol**: Main bridge contract with HTLC pattern
- **InsuranceFund.sol**: Security insurance for bridge users
- **PriceOracle.sol**: ETH/XMR price feeds

### 2. TSS Service (Go)

- **binance-chain/tss-lib**: Production TSS library
- **gRPC Interface**: Communication with Python client
- **DKG**: Distributed key generation
- **Signing**: Threshold signature creation

### 3. MPC Nodes (Python)

- Monitor Ethereum and Monero chains
- Coordinate cross-chain transactions
- Execute threshold signatures
- Communicate via secure P2P (mTLS)

### 4. HSM Integration

- **Hashicorp Vault**: Secret storage and encryption
- **AWS KMS**: Cloud key management
- **Key Ceremony**: Secure distributed key generation

### 5. INSTANT Privacy Stack

```
┌─────────────────────────────────────────────────────────────────┐
│                    INSTANT PRIVACY LAYER                        │
├─────────────────────────────────────────────────────────────────┤
│  🕵️ Tor Hidden Services  │  IP hidden, .onion routing           │
├─────────────────────────────────────────────────────────────────┤
│  🔐 Stealth Addresses    │  One-time, unlinkable (<1 sec)       │
├─────────────────────────────────────────────────────────────────┤
│  🌪️ CoinJoin            │  50+ participants (1-2 min)          │
├─────────────────────────────────────────────────────────────────┤
│  ⚡ Submarine Swaps      │  Atomic, Lightning-fast (1-30 sec)   │
├─────────────────────────────────────────────────────────────────┤
│  🛡️ ZK Proofs           │  Zero-knowledge verification         │
└─────────────────────────────────────────────────────────────────┘

Total time: 1-3 minutes for MAXIMUM privacy
NO useless time delays - pure cryptography!
```

#### Privacy Components

| Component | Time | Anonymity | Location |
|-----------|------|-----------|----------|
| Tor Hidden Services | Instant | IP hidden | `bridge/tor/` |
| Stealth Addresses | <1 sec | Unlinkable | `bridge/privacy/stealth_address.py` |
| CoinJoin | 1-2 min | 50+ set | `bridge/mixing/coinjoin.py` |
| Submarine Swaps | 1-30 sec | Chain break | `bridge/mixing/submarine.py` |
| ZK Proofs | <1 sec | Zero disclosure | `bridge/privacy/zk_verifier.py` |

## Privacy Philosophy

> **Privacy through cryptography, not obscurity.**

❌ **NO** time delays (useless, bad UX)  
✅ **YES** cryptographic guarantees (mathematically secure)

## Security Model

### Threats Mitigated

1. **Single Point of Failure**: 3-of-5 threshold signature
2. **Key Compromise**: HSM storage + proactive resharing
3. **Rug Pull**: No admin access to user funds
4. **Front-running**: Commit-reveal pattern
5. **Oracle Manipulation**: Multi-source price aggregation
6. **Transaction Tracing**: Stealth addresses + CoinJoin
7. **IP Tracking**: Tor hidden services

### Trust Assumptions

1. Honest majority of MPC nodes (≥3 of 5)
2. HSM provides secure key storage
3. P2P communication is authenticated (mTLS)
4. Smart contracts are bug-free (audited)

## Data Flow

### ETH → XMR Swap (Private)

1. User generates **stealth address** (<1 sec)
2. User calls `lock()` on bridge contract
3. MPC nodes detect the lock event via Tor
4. Nodes execute **CoinJoin** (1-2 min, 50+ participants)
5. Monero sent to user's **stealth XMR address**
6. User receives XMR (unlinked to source)

### XMR → ETH Swap (Private)

1. User generates **stealth XMR address**
2. User sends XMR to MPC-controlled stealth address
3. MPC nodes detect via Tor
4. Nodes create threshold signature
5. User calls `claim()` with **ZK proof**
6. User receives ETH at **stealth address**

## Network Topology

```
                    Internet (Tor)
                       │
         ┌─────────────┼─────────────┐
         │             │             │
         ▼             ▼             ▼
    ┌─────────┐  ┌─────────┐  ┌─────────┐
    │Node-1   │  │Node-2   │  │Node-3   │
    │.onion   │  │.onion   │  │.onion   │
    ├─────────┤  ├─────────┤  ├─────────┤
    │HSM:Vault│  │HSM:Vault│  │HSM:AWS  │
    └────┬────┘  └────┬────┘  └────┬────┘
         │            │            │
         └────────────┼────────────┘
                      │
              ┌───────┴───────┐
              │   Consensus    │
              │  (3-of-5 TSS)  │
              └───────────────┘
```

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Blockchain | Ethereum, Monero |
| Smart Contracts | Solidity 0.8.19, OpenZeppelin |
| TSS | bnb-chain/tss-lib (Go) |
| Backend | Python 3.10+, asyncio |
| P2P | Tor Hidden Services + mTLS |
| Privacy | Stealth addresses, ZK-SNARKs, CoinJoin |
| HSM | Hashicorp Vault, AWS KMS |
| Deployment | Docker, Kubernetes |

## Deployment Architecture

```
┌─────────────────────────────────────────┐
│           Kubernetes Cluster            │
│  ┌─────────────────────────────────┐    │
│  │    Tor Hidden Service Proxy     │    │
│  └─────────────────────────────────┘    │
│              │                          │
│  ┌───────────┼───────────┐              │
│  ▼           ▼           ▼              │
│ ┌────┐    ┌────┐    ┌────┐             │
│ │MPC │◀──▶│MPC │◀──▶│MPC │             │
│ │Pod │    │Pod │    │Pod │             │
│ └────┘    └────┘    └────┘             │
│   │         │         │                 │
│   ▼         ▼         ▼                 │
│ ┌────┐    ┌────┐    ┌────┐             │
│ │HSM │    │HSM │    │HSM │             │
│ │Side│    │Side│    │Side│             │
│ │car │    │car │    │car │             │
│ └────┘    └────┘    └────┘             │
└─────────────────────────────────────────┘
```

## Privacy Metrics

### Time to Privacy

| Operation | Time | Method |
|-----------|------|--------|
| Stealth address | <1 sec | Cryptography |
| CoinJoin | 1-2 min | 50+ real participants |
| Submarine swap | 1-30 sec | Atomic HTLC |
| **Total** | **<3 min** | **MAXIMUM privacy** |

### Anonymity Guarantees

- **Stealth addresses**: Mathematical unlinkability
- **CoinJoin**: 50+ participants = 1/50 probability
- **Submarine**: Chain break via Lightning
- **Combined**: Multiplicative effect

## Documentation

- [Instant Privacy](PRIVACY_INSTANT.md)
- [Threat Model](THREAT_MODEL.md)
- [Security Audit Prep](SECURITY_AUDIT_PREP.md)
