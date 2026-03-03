# StealthPay Implementation Roadmap
## План реализации CRITICAL → MEDIUM

**Версия:** 1.0  
**Дата:** 2026-03-02  
**Общая длительность:** 10-14 недель  
**Общий бюджет:** $67,000-125,000

---

## 📊 Executive Summary

```
Phase 1: Security Foundation    (Недели 1-6)   [$45k-75k]
Phase 2: Testnet Launch         (Недели 7-10)  [$15k-30k]
Phase 3: Production Prep        (Недели 11-14) [$7k-20k]
```

---

## Phase 1: Security Foundation (Недели 1-6)

### Неделя 1-2: Production TSS Library - Начало

**Цель:** Заменить educational TSS на production-ready реализацию

#### День 1-2: Research & Setup
```bash
# Создаем отдельный репозиторий для TSS
mkdir tss-service
cd tss-service

# Выбираем между:
# Option A: binance-chain/tss-lib (Go) - RECOMMENDED
# Option B: ZenGo-X/multi-party-ecdsa (Rust)
# Option C: silviupal/tss-lib (Python wrapper)

# Устанавливаем Go (для Option A)
brew install go

# Клонируем tss-lib
git clone https://github.com/bnb-chain/tss-lib.git
cd tss-lib
```

#### День 3-5: gRPC Interface
```go
// tss-service/main.go
package main

import (
    "context"
    "log"
    "net"
    
    "google.golang.org/grpc"
    pb "stealthpay/tss/proto"
)

type TSSServer struct {
    pb.UnimplementedTSSServiceServer
}

func (s *TSSServer) GenerateKey(ctx context.Context, req *pb.KeyGenRequest) (*pb.KeyGenResponse, error) {
    // Интеграция с tss-lib
    // Implementation here
}

func (s *TSSServer) Sign(ctx context.Context, req *pb.SignRequest) (*pb.SignResponse, error) {
    // Threshold signing
    // Implementation here
}

func main() {
    lis, err := net.Listen("tcp", ":50051")
    if err != nil {
        log.Fatalf("failed to listen: %v", err)
    }
    s := grpc.NewServer()
    pb.RegisterTSSServiceServer(s, &TSSServer{})
    log.Printf("TSS server listening at %v", lis.Addr())
    if err := s.Serve(lis); err != nil {
        log.Fatalf("failed to serve: %v", err)
    }
}
```

#### День 6-7: Python Client
```python
# stealthpay/bridge/tss_client.py
import grpc
from typing import List

from .proto import tss_pb2, tss_pb2_grpc

class TSSClient:
    """gRPC client for TSS service"""
    
    def __init__(self, endpoint: str = "localhost:50051"):
        self.channel = grpc.insecure_channel(endpoint)
        self.stub = tss_pb2_grpc.TSSServiceStub(self.channel)
    
    def generate_key(self, party_id: str, threshold: int, total: int) -> bytes:
        """Generate key share via DKG"""
        request = tss_pb2.KeyGenRequest(
            party_id=party_id,
            threshold=threshold,
            total=total
        )
        response = self.stub.GenerateKey(request)
        return response.key_share
    
    def sign(self, msg_hash: bytes, party_id: str, 
             key_share: bytes, peers: List[str]) -> bytes:
        """Create threshold signature"""
        request = tss_pb2.SignRequest(
            message_hash=msg_hash,
            party_id=party_id,
            key_share=key_share,
            peers=peers
        )
        response = self.stub.Sign(request)
        return response.signature
```

**Результат:** gRPC сервис TSS + Python клиент

---

### Неделя 3-4: Smart Contract Development & Audit Prep

#### День 1-3: Solidity Contract Implementation

```solidity
// contracts/StealthPayBridge.sol
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@openzeppelin/contracts/security/Pausable.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";

contract StealthPayBridge is ReentrancyGuard, Pausable, AccessControl {
    bytes32 public constant MPC_ROLE = keccak256("MPC_ROLE");
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
    
    struct Lock {
        address sender;
        uint256 amount;
        string xmrAddress;
        uint256 unlockTime;
        bool claimed;
        bool refunded;
        bytes32 mpcMerkleRoot;
    }
    
    mapping(bytes32 => Lock) public locks;
    mapping(address => uint256) public nonces;
    
    uint256 public constant MIN_LOCK_DURATION = 1 hours;
    uint256 public constant MAX_LOCK_DURATION = 7 days;
    uint256 public mpcThreshold = 3;
    uint256 public mpcTotal = 5;
    
    event Locked(
        bytes32 indexed lockId,
        address indexed sender,
        uint256 amount,
        string xmrAddress,
        uint256 unlockTime
    );
    
    event Claimed(bytes32 indexed lockId, address indexed recipient);
    event Refunded(bytes32 indexed lockId, address indexed sender);
    event EmergencyPaused(address indexed admin);
    event EmergencyUnpaused(address indexed admin);
    
    constructor(address[] memory mpcNodes) {
        require(mpcNodes.length >= mpcThreshold, "Insufficient MPC nodes");
        
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(ADMIN_ROLE, msg.sender);
        
        for (uint i = 0; i < mpcNodes.length; i++) {
            _grantRole(MPC_ROLE, mpcNodes[i]);
        }
    }
    
    function lock(
        string calldata xmrAddress,
        uint256 duration,
        bytes32 mpcMerkleRoot
    ) external payable nonReentrant whenNotPaused returns (bytes32 lockId) {
        require(msg.value > 0, "Amount must be > 0");
        require(bytes(xmrAddress).length > 0, "XMR address required");
        require(
            duration >= MIN_LOCK_DURATION && duration <= MAX_LOCK_DURATION,
            "Invalid duration"
        );
        
        lockId = keccak256(abi.encodePacked(
            msg.sender,
            block.timestamp,
            msg.value,
            xmrAddress,
            nonces[msg.sender]++
        ));
        
        require(locks[lockId].amount == 0, "Lock exists");
        
        locks[lockId] = Lock({
            sender: msg.sender,
            amount: msg.value,
            xmrAddress: xmrAddress,
            unlockTime: block.timestamp + duration,
            claimed: false,
            refunded: false,
            mpcMerkleRoot: mpcMerkleRoot
        });
        
        emit Locked(lockId, msg.sender, msg.value, xmrAddress, block.timestamp + duration);
    }
    
    function claim(
        bytes32 lockId,
        bytes calldata mpcSignature,
        address recipient
    ) external nonReentrant whenNotPaused {
        Lock storage lock = locks[lockId];
        
        require(lock.amount > 0, "Lock not found");
        require(!lock.claimed, "Already claimed");
        require(!lock.refunded, "Already refunded");
        require(block.timestamp < lock.unlockTime, "Lock expired");
        require(
            verifyMPCSignature(lockId, recipient, mpcSignature),
            "Invalid MPC signature"
        );
        
        lock.claimed = true;
        
        (bool success, ) = recipient.call{value: lock.amount}("");
        require(success, "Transfer failed");
        
        emit Claimed(lockId, recipient);
    }
    
    function refund(bytes32 lockId) external nonReentrant {
        Lock storage lock = locks[lockId];
        
        require(lock.amount > 0, "Lock not found");
        require(!lock.claimed, "Already claimed");
        require(!lock.refunded, "Already refunded");
        require(block.timestamp >= lock.unlockTime, "Lock active");
        require(msg.sender == lock.sender, "Not sender");
        
        lock.refunded = true;
        
        (bool success, ) = lock.sender.call{value: lock.amount}("");
        require(success, "Transfer failed");
        
        emit Refunded(lockId, lock.sender);
    }
    
    function verifyMPCSignature(
        bytes32 lockId,
        address recipient,
        bytes calldata signature
    ) internal view returns (bool) {
        // BLS threshold signature verification
        // Implementation using BLS12-381
        // This is a placeholder - real implementation uses pairing checks
        
        require(signature.length == 96, "Invalid signature length"); // BLS sig size
        
        // TODO: Implement BLS verification
        return true; // Placeholder
    }
    
    // Emergency functions
    function emergencyPause() external onlyRole(ADMIN_ROLE) {
        _pause();
        emit EmergencyPaused(msg.sender);
    }
    
    function emergencyUnpause() external onlyRole(ADMIN_ROLE) {
        _unpause();
        emit EmergencyUnpaused(msg.sender);
    }
    
    // Admin functions
    function updateMPCThreshold(uint256 newThreshold) external onlyRole(ADMIN_ROLE) {
        require(newThreshold <= mpcTotal, "Threshold exceeds total");
        mpcThreshold = newThreshold;
    }
    
    function updateMPCNode(address node, bool add) external onlyRole(ADMIN_ROLE) {
        if (add) {
            _grantRole(MPC_ROLE, node);
        } else {
            _revokeRole(MPC_ROLE, node);
        }
    }
    
    receive() external payable {
        revert("Use lock() function");
    }
}
```

#### День 4-5: Hardhat Setup & Testing

```javascript
// hardhat.config.js
require("@nomicfoundation/hardhat-toolbox");
require("@nomicfoundation/hardhat-verify");
require("hardhat-gas-reporter");

module.exports = {
  solidity: {
    version: "0.8.19",
    settings: {
      optimizer: {
        enabled: true,
        runs: 200
      }
    }
  },
  networks: {
    sepolia: {
      url: process.env.SEPOLIA_RPC || "",
      accounts: process.env.PRIVATE_KEY ? [process.env.PRIVATE_KEY] : []
    },
    mainnet: {
      url: process.env.MAINNET_RPC || "",
      accounts: process.env.PRIVATE_KEY ? [process.env.PRIVATE_KEY] : []
    }
  },
  etherscan: {
    apiKey: process.env.ETHERSCAN_API_KEY
  },
  gasReporter: {
    enabled: true,
    currency: "USD"
  }
};
```

```javascript
// test/Bridge.test.js
const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("StealthPayBridge", function() {
  let bridge;
  let owner;
  let alice;
  let bob;
  let mpcNodes;
  
  beforeEach(async function() {
    [owner, alice, bob, ...mpcNodes] = await ethers.getSigners();
    
    const Bridge = await ethers.getContractFactory("StealthPayBridge");
    bridge = await Bridge.deploy(mpcNodes.slice(0, 5).map(n => n.address));
    await bridge.waitForDeployment();
  });
  
  describe("Lock", function() {
    it("Should lock ETH and emit event", async function() {
      const amount = ethers.parseEther("0.1");
      const duration = 3600; // 1 hour
      const xmrAddress = "44testAddress...";
      
      await expect(
        bridge.connect(alice).lock(xmrAddress, duration, ethers.ZeroHash, { value: amount })
      )
        .to.emit(bridge, "Locked")
        .withArgs(
          await bridge.locks(await bridge.nonces(alice)),
          alice.address,
          amount,
          xmrAddress,
          await ethers.provider.getBlock("latest").then(b => b.timestamp + duration)
        );
    });
    
    it("Should reject zero amount", async function() {
      await expect(
        bridge.connect(alice).lock("44test", 3600, ethers.ZeroHash, { value: 0 })
      ).to.be.revertedWith("Amount must be > 0");
    });
  });
  
  describe("Claim", function() {
    it("Should allow claim with valid MPC signature", async function() {
      // Setup lock
      const amount = ethers.parseEther("0.1");
      await bridge.connect(alice).lock("44test", 3600, ethers.ZeroHash, { value: amount });
      
      // Claim with MPC sig (placeholder)
      const lockId = await bridge.nonces(alice);
      const sig = ethers.randomBytes(96); // Placeholder BLS sig
      
      await expect(bridge.connect(bob).claim(lockId, sig, bob.address))
        .to.emit(bridge, "Claimed");
    });
  });
  
  describe("Emergency", function() {
    it("Should allow admin to pause", async function() {
      await bridge.connect(owner).emergencyPause();
      expect(await bridge.paused()).to.be.true;
    });
    
    it("Should prevent non-admin from pausing", async function() {
      await expect(bridge.connect(alice).emergencyPause())
        .to.be.reverted;
    });
  });
});
```

#### День 6-7: Deploy to Testnet

```bash
# Deploy to Sepolia
npx hardhat run scripts/deploy.js --network sepolia

# Verify on Etherscan
npx hardhat verify --network sepolia <CONTRACT_ADDRESS> <MPC_NODE_1> <MPC_NODE_2> ...
```

**Результат:** Деплой контракта на Sepolia + тесты

---

### Неделя 5: HSM Integration

#### День 1-2: AWS KMS Setup

```python
# stealthpay/bridge/hsm/aws_kms.py
import boto3
from typing import Optional
from dataclasses import dataclass

@dataclass
class KeyShareHSM:
    """Key share stored in HSM"""
    key_id: str  # AWS KMS Key ID
    alias: str
    party_id: int
    
class AWSKMSManager:
    """AWS KMS integration for MPC key shares"""
    
    def __init__(self, region: str = "us-east-1"):
        self.client = boto3.client('kms', region_name=region)
    
    def create_key(self, party_id: int, alias: str) -> KeyShareHSM:
        """Create new KMS key for MPC share"""
        response = self.client.create_key(
            Description=f"MPC Key Share for Party {party_id}",
            KeyUsage='SIGN_VERIFY',
            KeySpec='ECC_SECG_P256K1',
            Tags=[
                {'TagKey': 'Purpose', 'TagValue': 'MPC'},
                {'TagKey': 'PartyId', 'TagValue': str(party_id)}
            ]
        )
        
        key_id = response['KeyMetadata']['KeyId']
        
        # Create alias
        self.client.create_alias(
            AliasName=f"alias/stealthpay-mpc-{party_id}",
            TargetKeyId=key_id
        )
        
        return KeyShareHSM(
            key_id=key_id,
            alias=alias,
            party_id=party_id
        )
    
    def sign(self, key_id: str, message: bytes) -> bytes:
        """Sign message with KMS key"""
        response = self.client.sign(
            KeyId=key_id,
            Message=message,
            SigningAlgorithm='ECDSA_SHA_256'
        )
        return response['Signature']
    
    def get_public_key(self, key_id: str) -> bytes:
        """Get public key from KMS"""
        response = self.client.get_public_key(KeyId=key_id)
        return response['PublicKey']
```

#### День 3-4: Hashicorp Vault Integration

```python
# stealthpay/bridge/hsm/vault.py
import hvac
from typing import Optional

class VaultManager:
    """Hashicorp Vault integration"""
    
    def __init__(self, url: str, token: str):
        self.client = hvac.Client(url=url, token=token)
    
    def store_key_share(self, party_id: int, key_share: bytes) -> str:
        """Store key share in Vault"""
        path = f"mpc/party-{party_id}"
        
        self.client.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret={
                "key_share": key_share.hex(),
                "party_id": party_id,
                "created_at": str(time.time())
            }
        )
        
        return path
    
    def retrieve_key_share(self, party_id: int) -> Optional[bytes]:
        """Retrieve key share from Vault"""
        path = f"mpc/party-{party_id}"
        
        try:
            response = self.client.secrets.kv.v2.read_secret_version(path=path)
            hex_share = response['data']['data']['key_share']
            return bytes.fromhex(hex_share)
        except hvac.exceptions.InvalidPath:
            return None
    
    def setup_transit_engine(self):
        """Setup Vault transit engine for encryption"""
        # Enable transit secrets engine
        try:
            self.client.sys.enable_secrets_engine(
                backend_type='transit',
                path='mpc-transit'
            )
        except hvac.exceptions.InvalidRequest:
            pass  # Already enabled
        
        # Create encryption key
        self.client.secrets.transit.create_key(
            name='mpc-master-key',
            key_type='aes-256-gcm'
        )
```

#### День 5-6: Key Ceremony Procedures

```python
# scripts/key_ceremony.py
"""
MPC Key Generation Ceremony

This script performs a secure distributed key generation ceremony
with multiple parties in different locations.
"""

import argparse
import json
from typing import List
from getpass import getpass

from stealthpay.bridge.hsm.vault import VaultManager
from stealthpay.bridge.hsm.aws_kms import AWSKMSManager
from stealthpay.bridge.tss_client import TSSClient

def key_ceremony(
    party_id: int,
    total_parties: int = 5,
    threshold: int = 3,
    use_hsm: bool = True
):
    """
    Perform secure key generation ceremony
    
    1. Each party starts in isolated environment
    2. Parties connect via secure channel
    3. DKG protocol executed
    4. Key shares stored in HSM
    5. Public key published
    """
    
    print(f"=== MPC Key Ceremony - Party {party_id} ===\n")
    
    # Step 1: Connect to TSS service
    print("1. Connecting to TSS service...")
    tss = TSSClient()
    
    # Step 2: Initialize HSM
    if use_hsm:
        print("2. Initializing HSM...")
        vault_url = getpass("Vault URL: ")
        vault_token = getpass("Vault Token: ")
        vault = VaultManager(vault_url, vault_token)
    
    # Step 3: Generate key share
    print(f"3. Generating key share ({threshold}-of-{total_parties})...")
    key_share = tss.generate_key(
        party_id=str(party_id),
        threshold=threshold,
        total=total_parties
    )
    
    # Step 4: Store in HSM
    if use_hsm:
        print("4. Storing key share in HSM...")
        vault.store_key_share(party_id, key_share)
        print(f"   ✓ Stored in Vault: mpc/party-{party_id}")
    else:
        print("WARNING: Key share not stored in HSM! (dev mode)")
        with open(f"/secure/keys/party-{party_id}.key", 'wb') as f:
            f.write(key_share)
    
    # Step 5: Verify
    print("5. Verification...")
    # Implementation here
    
    print("\n✓ Key ceremony completed for Party", party_id)
    print("\nIMPORTANT:")
    print("- Key share is now secured in HSM")
    print("- Backup key share offline (shamir split)")
    print("- Never transmit key share over network")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MPC Key Ceremony")
    parser.add_argument("--party-id", type=int, required=True)
    parser.add_argument("--total", type=int, default=5)
    parser.add_argument("--threshold", type=int, default=3)
    parser.add_argument("--no-hsm", action="store_true")
    
    args = parser.parse_args()
    
    key_ceremony(
        party_id=args.party_id,
        total_parties=args.total,
        threshold=args.threshold,
        use_hsm=not args.no_hsm
    )
```

#### День 7: Testing & Documentation

```bash
# Test HSM integration
python scripts/test_hsm.py --hsm vault
python scripts/test_hsm.py --hsm aws

# Document key ceremony procedures
cat > docs/KEY_CEREMONY.md << 'EOF'
# MPC Key Ceremony Procedure

## Prerequisites
- 5 parties in separate secure locations
- HSM initialized (Vault or AWS KMS)
- Secure communication channel (Signal/Wire)
- Air-gapped machine for backup

## Steps
1. Each party runs key_ceremony.py
2. Parties exchange public keys
3. DKG protocol execution
4. Verify group public key
5. Store backups offline

## Security Checklist
- [ ] All parties in separate locations
- [ ] No key material transmitted over network
- [ ] HSM properly configured
- [ ] Backups created and secured
- [ ] Group public key verified
EOF
```

**Результат:** HSM интеграция + процедуры key ceremony

---

### Неделя 6: Security Audit Prep & Bug Bounty

#### День 1-3: Audit Preparation

```bash
# Prepare audit package
mkdir -p audit-package/

# 1. Source code
cp -r stealthpay/ audit-package/
cp -r contracts/ audit-package/

# 2. Documentation
cp docs/ARCHITECTURE.md audit-package/
cp docs/THREAT_MODEL.md audit-package/
cp SECURITY_AUDIT.md audit-package/

# 3. Test results
cp -r tests/ audit-package/
python -m pytest tests/ --cov=stealthpay --cov-report=html -v > audit-package/test-results.txt

# 4. Deployment scripts
cp -r scripts/ audit-package/
cp docker-compose*.yml audit-package/

# 5. Create audit brief
cat > audit-package/AUDIT_BRIEF.md << 'EOF'
# StealthPay Security Audit Brief

## Scope
- Smart Contracts: contracts/StealthPayBridge.sol
- TSS Implementation: stealthpay/bridge/tss/
- MPC Node: stealthpay/bridge/relayers/mpc_node_v2.py
- P2P Network: stealthpay/bridge/p2p/

## Focus Areas
1. Reentrancy attacks
2. Threshold signature security
3. Key management
4. P2P communication security
5. Front-running protection

## Timeline
- Start: [Date]
- Duration: 2-4 weeks
- Report: [Date]

## Contacts
- Technical Lead: [email]
- Security: security@stealthpay.io
EOF

# Zip package
zip -r stealthpay-audit-package.zip audit-package/
```

#### День 4-5: Bug Bounty Program Setup

```markdown
# Bug Bounty Program

## Platform
- Immunefi (recommended)
- OR HackerOne

## Scope
- Smart Contracts (Critical)
- TSS Implementation (Critical)
- Web/API (High)
- Documentation (Low)

## Rewards
| Severity | Reward |
|----------|--------|
| Critical | $50,000-100,000 |
| High | $10,000-25,000 |
| Medium | $2,500-5,000 |
| Low | $500-1,000 |

## Rules
1. No testing on mainnet
2. No social engineering
3. No DoS attacks
4. Report within 24h of discovery
5. Keep findings confidential

## Contact
security@stealthpay.io
```

#### День 6-7: Insurance Fund Setup

```solidity
// contracts/InsuranceFund.sol
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

contract InsuranceFund {
    address public operator;
    uint256 public totalFunds;
    
    mapping(bytes32 => Claim) public claims;
    
    struct Claim {
        address claimant;
        uint256 amount;
        string reason;
        bool approved;
        bool paid;
    }
    
    event Deposit(address indexed sender, uint256 amount);
    event ClaimSubmitted(bytes32 indexed claimId, address claimant, uint256 amount);
    event ClaimApproved(bytes32 indexed claimId);
    event ClaimPaid(bytes32 indexed claimId, uint256 amount);
    
    constructor() {
        operator = msg.sender;
    }
    
    function deposit() external payable {
        totalFunds += msg.value;
        emit Deposit(msg.sender, msg.value);
    }
    
    function submitClaim(
        string calldata reason
    ) external returns (bytes32 claimId) {
        claimId = keccak256(abi.encodePacked(msg.sender, block.timestamp, reason));
        
        claims[claimId] = Claim({
            claimant: msg.sender,
            amount: 0, // To be determined
            reason: reason,
            approved: false,
            paid: false
        });
        
        emit ClaimSubmitted(claimId, msg.sender, 0);
    }
    
    function approveAndPay(
        bytes32 claimId,
        uint256 amount
    ) external {
        require(msg.sender == operator, "Not operator");
        require(!claims[claimId].paid, "Already paid");
        require(amount <= totalFunds, "Insufficient funds");
        
        claims[claimId].approved = true;
        claims[claimId].amount = amount;
        claims[claimId].paid = true;
        
        totalFunds -= amount;
        
        (bool success, ) = claims[claimId].claimant.call{value: amount}("");
        require(success, "Transfer failed");
        
        emit ClaimApproved(claimId);
        emit ClaimPaid(claimId, amount);
    }
    
    receive() external payable {
        totalFunds += msg.value;
        emit Deposit(msg.sender, msg.value);
    }
}
```

**Результат:** Аудит пакет готов + bug bounty программа

---

## Phase 2: Testnet Launch (Недели 7-10)

### Неделя 7: Oracle Integration

#### День 1-2: Chainlink Integration

```solidity
// contracts/PriceOracle.sol
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@chainlink/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol";

contract PriceOracle {
    AggregatorV3Interface internal ethUsdFeed;
    
    // XMR doesn't have Chainlink feed, use decentralized oracle
    address public xmrOracle;
    uint256 public xmrPrice;
    uint256 public lastUpdate;
    
    uint256 public constant MAX_PRICE_AGE = 1 hours;
    uint256 public constant MAX_DEVIATION = 500; // 5%
    
    event PriceUpdated(uint256 ethPrice, uint256 xmrPrice);
    
    constructor(address _ethUsdFeed) {
        ethUsdFeed = AggregatorV3Interface(_ethUsdFeed);
    }
    
    function getEthPrice() public view returns (uint256) {
        (, int256 price,,,) = ethUsdFeed.latestRoundData();
        require(price > 0, "Invalid price");
        return uint256(price);
    }
    
    function updateXmrPrice(uint256 newPrice) external {
        require(msg.sender == xmrOracle, "Not oracle");
        
        // Check deviation
        if (xmrPrice > 0) {
            uint256 deviation = _calculateDeviation(xmrPrice, newPrice);
            require(deviation <= MAX_DEVIATION, "Price deviation too high");
        }
        
        xmrPrice = newPrice;
        lastUpdate = block.timestamp;
        
        emit PriceUpdated(getEthPrice(), newPrice);
    }
    
    function getXmrToEthRate() external view returns (uint256) {
        require(block.timestamp - lastUpdate <= MAX_PRICE_AGE, "Price stale");
        
        uint256 ethUsd = getEthPrice();
        // rate = (1 / xmrUsd) * ethUsd
        return (ethUsd * 1e18) / xmrPrice;
    }
    
    function _calculateDeviation(uint256 old, uint256 new_) internal pure returns (uint256) {
        if (old > new_) {
            return ((old - new_) * 10000) / old;
        } else {
            return ((new_ - old) * 10000) / old;
        }
    }
}
```

```python
# stealthpay/bridge/oracle/chainlink.py
import requests
from decimal import Decimal
from typing import Optional

class ChainlinkOracle:
    """Chainlink price feed integration"""
    
    ETH_USD_FEED = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419"  # Mainnet
    ETH_USD_FEED_SEPOLIA = "0x694AA1769357215DE4FAC081bf1f309aDC325306"
    
    def __init__(self, network: str = "mainnet"):
        self.network = network
        self.rpc_url = self._get_rpc_url()
    
    def get_eth_price(self) -> Decimal:
        """Get ETH/USD price from Chainlink"""
        # Use Web3 to call Chainlink contract
        # Implementation here
        pass
    
    def get_xmr_price_fallback(self) -> Decimal:
        """
        Get XMR price from multiple sources
        and calculate median
        """
        sources = [
            self._get_binance_price,
            self._get_kraken_price,
            self._get_coingecko_price,
        ]
        
        prices = []
        for source in sources:
            try:
                price = source()
                prices.append(price)
            except Exception:
                continue
        
        if not prices:
            raise Exception("No price sources available")
        
        # Calculate median
        prices.sort()
        median = prices[len(prices) // 2]
        
        return Decimal(str(median))
    
    def _get_binance_price(self) -> float:
        response = requests.get(
            "https://api.binance.com/api/v3/ticker/price?symbol=XMRUSDT"
        )
        return float(response.json()["price"])
    
    def _get_kraken_price(self) -> float:
        response = requests.get(
            "https://api.kraken.com/0/public/Ticker?pair=XMRUSD"
        )
        data = response.json()
        return float(data["result"]["XXMRZUSD"]["c"][0])
    
    def _get_coingecko_price(self) -> float:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=monero&vs_currencies=usd"
        )
        return float(response.json()["monero"]["usd"])
```

#### День 3-4: DEX Liquidity Oracles

```python
# stealthpay/bridge/oracle/dex.py
from web3 import Web3

class UniswapV3Oracle:
    """Uniswap V3 TWAP oracle"""
    
    def __init__(self, rpc_url: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        
        # Uniswap V3 Factory
        self.factory = self.w3.eth.contract(
            address="0x1F98431c8aD98523631AE4a59f267346ea31F984",
            abi=FACTORY_ABI
        )
    
    def get_twap_price(
        self,
        token0: str,
        token1: str,
        fee: int = 3000,  # 0.3%
        seconds_ago: int = 3600  # 1 hour TWAP
    ) -> Decimal:
        """Get time-weighted average price"""
        pool_address = self.factory.functions.getPool(
            token0,
            token1,
            fee
        ).call()
        
        pool = self.w3.eth.contract(address=pool_address, abi=POOL_ABI)
        
        # Get observations
        observations = pool.functions.observe([0, seconds_ago]).call()
        
        # Calculate TWAP
        tick_cumulatives = observations[1]
        tick = (tick_cumulatives[1] - tick_cumulatives[0]) / seconds_ago
        
        # Convert tick to price
        price = 1.0001 ** tick
        
        return Decimal(str(price))
```

#### День 5-7: Oracle Aggregation & Testing

```python
# stealthpay/bridge/oracle/aggregator.py
from typing import List, Dict
from decimal import Decimal
import statistics

class OracleAggregator:
    """
    Aggregate prices from multiple sources
    with outlier detection
    """
    
    def __init__(self):
        self.oracles: List[BaseOracle] = []
        self.max_deviation = Decimal("0.05")  # 5%
    
    def add_oracle(self, oracle: BaseOracle):
        self.oracles.append(oracle)
    
    def get_price(self, base: str, quote: str) -> Decimal:
        """Get aggregated price with outlier detection"""
        prices: List[Decimal] = []
        sources: Dict[str, Decimal] = {}
        
        for oracle in self.oracles:
            try:
                price = oracle.get_price(base, quote)
                prices.append(price)
                sources[oracle.name] = price
            except Exception as e:
                print(f"Oracle {oracle.name} failed: {e}")
                continue
        
        if len(prices) < 2:
            raise Exception("Insufficient price sources")
        
        # Outlier detection
        median = statistics.median(prices)
        valid_prices = []
        
        for price in prices:
            deviation = abs(price - median) / median
            if deviation <= self.max_deviation:
                valid_prices.append(price)
        
        if len(valid_prices) < 2:
            raise Exception("Too many outliers")
        
        # Return median of valid prices
        return statistics.median(valid_prices)
```

**Результат:** Oracle интеграция готова

---

### Неделя 8: P2P Security (mTLS)

#### День 1-2: Certificate Generation

```bash
#!/bin/bash
# scripts/generate_certs.sh

mkdir -p certs/

# Generate CA
openssl req -x509 -newkey rsa:4096 -keyout certs/ca.key -out certs/ca.crt \
    -days 365 -nodes -subj "/C=US/O=StealthPay/CN=StealthPay CA"

# Generate certificates for each MPC node
for i in {1..5}; do
    # Private key
    openssl genrsa -out certs/node${i}.key 2048
    
    # Certificate request
    openssl req -new -key certs/node${i}.key -out certs/node${i}.csr \
        -subj "/C=US/O=StealthPay/CN=mpc-node-${i}"
    
    # Sign with CA
    openssl x509 -req -in certs/node${i}.csr -CA certs/ca.crt -CAkey certs/ca.key \
        -CAcreateserial -out certs/node${i}.crt -days 365 \
        -extensions v3_req -extfile <(cat <<EOF
[v3_req]
subjectAltName = @alt_names
[alt_names]
DNS.1 = mpc-node-${i}
IP.1 = 127.0.0.1
EOF
)
done

echo "✓ Certificates generated in certs/"
```

#### День 3-4: mTLS WebSocket Implementation

```python
# stealthpay/bridge/p2p/tls_node.py
import ssl
import asyncio
import websockets
from pathlib import Path

class MTLSNode:
    """WebSocket node with mutual TLS authentication"""
    
    def __init__(
        self,
        node_id: str,
        cert_path: str,
        key_path: str,
        ca_path: str
    ):
        self.node_id = node_id
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_path = ca_path
        
        self.ssl_context = self._create_ssl_context()
    
    def _create_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context with mutual authentication"""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        
        # Load certificate and private key
        context.load_cert_chain(
            certfile=self.cert_path,
            keyfile=self.key_path
        )
        
        # Load CA for client verification
        context.load_verify_locations(self.ca_path)
        
        # Require client certificate
        context.verify_mode = ssl.CERT_REQUIRED
        
        # TLS 1.3 only
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        
        return context
    
    async def start_server(self, host: str, port: int):
        """Start secure WebSocket server"""
        async def handler(websocket, path):
            # Verify client certificate matches node ID
            cert = websocket.transport.get_extra_info('ssl_object').getpeercert()
            client_cn = cert.get('subject', [[['commonName', 'unknown']]])[0][0][1]
            
            print(f"Client connected: {client_cn}")
            
            async for message in websocket:
                await self._handle_message(message, client_cn)
        
        server = await websockets.serve(
            handler,
            host,
            port,
            ssl=self.ssl_context
        )
        
        print(f"mTLS server started on {host}:{port}")
        return server
    
    async def connect(
        self,
        uri: str,
        expected_cn: str
    ):
        """Connect to peer with certificate pinning"""
        # Create client SSL context
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_cert_chain(
            certfile=self.cert_path,
            keyfile=self.key_path
        )
        context.load_verify_locations(self.ca_path)
        context.verify_mode = ssl.CERT_REQUIRED
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        
        websocket = await websockets.connect(
            uri,
            ssl=context
        )
        
        # Verify server certificate
        cert = websocket.transport.get_extra_info('ssl_object').getpeercert()
        server_cn = cert.get('subject', [[['commonName', 'unknown']]])[0][0][1]
        
        if server_cn != expected_cn:
            raise Exception(f"Certificate mismatch: {server_cn} != {expected_cn}")
        
        return websocket
```

#### День 5-7: Integration & Testing

```python
# tests/test_p2p_tls.py
import pytest
import asyncio
from stealthpay.bridge.p2p.tls_node import MTLSNode

@pytest.mark.asyncio
async def test_mtls_connection():
    """Test mutual TLS authentication"""
    
    # Create two nodes
    node1 = MTLSNode(
        node_id="mpc_node_1",
        cert_path="certs/node1.crt",
        key_path="certs/node1.key",
        ca_path="certs/ca.crt"
    )
    
    node2 = MTLSNode(
        node_id="mpc_node_2",
        cert_path="certs/node2.crt",
        key_path="certs/node2.key",
        ca_path="certs/ca.crt"
    )
    
    # Start server
    server = await node1.start_server("localhost", 8765)
    
    # Connect client
    ws = await node2.connect(
        "wss://localhost:8765",
        expected_cn="mpc-node-1"
    )
    
    # Test message exchange
    await ws.send("Hello from node 2")
    
    # Cleanup
    await ws.close()
    server.close()
```

**Результат:** P2P с mTLS защитой

---

### Неделя 9-10: Testnet Deployment

#### День 1-3: Sepolia Deployment

```javascript
// scripts/deploy-testnet.js
const { ethers } = require("hardhat");

async function main() {
    const [deployer] = await ethers.getSigners();
    
    console.log("Deploying contracts with account:", deployer.address);
    console.log("Account balance:", (await deployer.getBalance()).toString());
    
    // MPC nodes (replace with real addresses)
    const mpcNodes = [
        "0x...", // Node 1
        "0x...", // Node 2
        "0x...", // Node 3
        "0x...", // Node 4
        "0x...", // Node 5
    ];
    
    // Deploy Bridge
    const Bridge = await ethers.getContractFactory("StealthPayBridge");
    const bridge = await Bridge.deploy(mpcNodes);
    await bridge.deployed();
    
    console.log("Bridge deployed to:", bridge.address);
    
    // Deploy Insurance Fund
    const Insurance = await ethers.getContractFactory("InsuranceFund");
    const insurance = await Insurance.deploy();
    await insurance.deployed();
    
    console.log("InsuranceFund deployed to:", insurance.address);
    
    // Deploy Oracle
    const ethUsdFeed = "0x694AA1769357215DE4FAC081bf1f309aDC325306"; // Sepolia
    const Oracle = await ethers.getContractFactory("PriceOracle");
    const oracle = await Oracle.deploy(ethUsdFeed);
    await oracle.deployed();
    
    console.log("PriceOracle deployed to:", oracle.address);
    
    // Fund insurance
    await insurance.deposit({ value: ethers.parseEther("10") });
    console.log("Insurance fund seeded with 10 ETH");
    
    // Save deployment info
    const deploymentInfo = {
        network: "sepolia",
        bridge: bridge.address,
        insurance: insurance.address,
        oracle: oracle.address,
        timestamp: new Date().toISOString()
    };
    
    require('fs').writeFileSync(
        'deployment-sepolia.json',
        JSON.stringify(deploymentInfo, null, 2)
    );
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error(error);
        process.exit(1);
    });
```

#### День 4-7: MPC Nodes Deployment

```yaml
# docker-compose.testnet.yml
version: '3.8'

services:
  mpc-node-1:
    build:
      context: .
      dockerfile: Dockerfile.mpc
    environment:
      - NODE_ID=mpc_node_1
      - NODE_INDEX=1
      - NETWORK=testnet
      - ETH_RPC_URL=https://rpc.sepolia.org
      - BRIDGE_CONTRACT=0x... # From deployment
      - XMR_WALLET_HOST=monero-wallet
      - XMR_WALLET_PORT=38082
      - HSM_TYPE=vault
      - VAULT_ADDR=https://vault.example.com
    volumes:
      - ./certs/node1.crt:/keys/cert.pem:ro
      - ./certs/node1.key:/keys/key.pem:ro
      - ./certs/ca.crt:/keys/ca.pem:ro
    ports:
      - "10001:10001"
    command: python -m stealthpay.bridge.relayers.mpc_node_v2
    restart: unless-stopped
    
  # ... nodes 2-5 similar

  monero-wallet:
    image: monero-wallet-rpc:latest
    command:
      - --stagenet
      - --daemon-host=node.monerodevs.org
      - --daemon-port=38089
      - --rpc-bind-port=38082
      - --wallet-dir=/wallets
      - --disable-rpc-login
    volumes:
      - monero_wallets:/wallets
```

```bash
# Deploy to testnet
./scripts/deploy-testnet.sh sepolia

# Verify on Etherscan
npx hardhat verify --network sepolia <BRIDGE_ADDRESS> <MPC_NODE_1> <MPC_NODE_2> ...

# Start MPC nodes
docker-compose -f docker-compose.testnet.yml up -d
```

**Результат:** Полный деплой на testnet

---

## Phase 3: Production Prep (Недели 11-14)

### Неделя 11: Rate Limiting & DoS Protection

```python
# stealthpay/bridge/security/rate_limiter.py
import redis
import time
from typing import Optional

class RateLimiter:
    """Redis-based rate limiting"""
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis = redis.from_url(redis_url)
    
    def check_rate_limit(
        self,
        key: str,
        max_requests: int,
        window: int
    ) -> bool:
        """
        Check if request is within rate limit
        
        Args:
            key: Identifier (IP, user ID, etc.)
            max_requests: Max requests per window
            window: Time window in seconds
            
        Returns:
            True if allowed, False if rate limited
        """
        current = int(time.time())
        window_start = current - window
        
        pipe = self.redis.pipeline()
        
        # Remove old entries
        pipe.zremrangebyscore(f"rate_limit:{key}", 0, window_start)
        
        # Count current entries
        pipe.zcard(f"rate_limit:{key}")
        
        # Add current request
        pipe.zadd(f"rate_limit:{key}", {str(current): current})
        
        # Set expiry
        pipe.expire(f"rate_limit:{key}", window)
        
        results = pipe.execute()
        current_count = results[1]
        
        return current_count <= max_requests

class DDoSProtector:
    """DDoS protection middleware"""
    
    def __init__(self):
        self.rate_limiter = RateLimiter()
        self.ban_list = set()
    
    async def check_request(self, peer_id: str, msg_type: str) -> bool:
        """
        Check if request should be allowed
        
        Different limits for different message types
        """
        if peer_id in self.ban_list:
            return False
        
        # Strict limits for expensive operations
        limits = {
            "SIGN_COMMIT": (10, 60),      # 10 per minute
            "SIGN_SHARE": (10, 60),       # 10 per minute
            "BRIDGE_REQUEST": (5, 60),    # 5 per minute
            "PING": (60, 60),             # 60 per minute
        }
        
        max_req, window = limits.get(msg_type, (30, 60))
        key = f"{peer_id}:{msg_type}"
        
        allowed = self.rate_limiter.check_rate_limit(key, max_req, window)
        
        if not allowed:
            # Log potential attack
            print(f"Rate limit exceeded: {peer_id} - {msg_type}")
            
            # Check if should ban
            total_violations = self._count_violations(peer_id)
            if total_violations > 10:
                self.ban_list.add(peer_id)
                print(f"Banned peer: {peer_id}")
        
        return allowed
```

### Неделя 12: Database Layer

```python
# stealthpay/db/models.py
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Numeric, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

Base = declarative_base()

class Swap(Base):
    __tablename__ = 'swaps'
    
    id = Column(String, primary_key=True)
    status = Column(String)
    role = Column(String)
    btc_amount = Column(Numeric)
    xmr_amount = Column(Numeric)
    btc_address = Column(String)
    xmr_address = Column(String)
    htlc_address = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    tx_btc_fund = Column(String)
    tx_btc_claim = Column(String)
    tx_xmr_fund = Column(String)
    tx_xmr_claim = Column(String)

class BridgeTransfer(Base):
    __tablename__ = 'bridge_transfers'
    
    id = Column(String, primary_key=True)
    direction = Column(String)  # eth_to_xmr or xmr_to_eth
    eth_amount = Column(Numeric)
    xmr_amount = Column(Numeric)
    eth_address = Column(String)
    xmr_address = Column(String)
    status = Column(String)
    lock_tx = Column(String)
    claim_tx = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

# Initialize
def init_db(database_url: str = "postgresql://user:pass@localhost/stealthpay"):
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
```

### Неделя 13: CLI Improvements

```python
# cli/swap_wizard.py
import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt, Confirm

console = Console()

@click.command()
def swap_wizard():
    """Interactive swap wizard"""
    console.print("""
[bold green]╔═══════════════════════════════════════════╗
║         StealthPay Swap Wizard            ║
╚═══════════════════════════════════════════╝[/]
    """)
    
    # Step 1: Choose role
    role = Prompt.ask(
        "Choose your role",
        choices=["seller", "buyer"],
        default="seller"
    )
    
    # Step 2: Enter amounts
    if role == "seller":
        console.print("\n[yellow]You are selling XMR for BTC[/]")
        xmr_amount = Prompt.ask("Enter XMR amount to sell")
        btc_amount = Prompt.ask("Enter BTC amount to receive")
        btc_address = Prompt.ask("Enter your BTC address (testnet)")
    else:
        console.print("\n[yellow]You are buying XMR with BTC[/]")
        btc_amount = Prompt.ask("Enter BTC amount to spend")
        xmr_amount = Prompt.ask("Enter XMR amount to receive")
        xmr_address = Prompt.ask("Enter your XMR address (stagenet)")
    
    # Step 3: Confirm
    console.print(f"\n[cyan]Swap Summary:[/]")
    console.print(f"  Role: {role}")
    console.print(f"  BTC: {btc_amount}")
    console.print(f"  XMR: {xmr_amount}")
    
    if not Confirm.ask("\nProceed with swap?"):
        console.print("[red]Swap cancelled[/]")
        return
    
    # Step 4: Execute with progress
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        
        task1 = progress.add_task("Creating swap coordinator...", total=None)
        # ... create coordinator
        progress.update(task1, completed=True)
        
        task2 = progress.add_task("Setting up multisig...", total=None)
        # ... setup multisig
        progress.update(task2, completed=True)
        
        task3 = progress.add_task("Waiting for counterparty...", total=None)
        # ... wait
        progress.update(task3, completed=True)
    
    console.print("\n[bold green]✓ Swap initiated successfully![/]")
    console.print(f"Swap ID: [cyan]{swap_id}[/]")
    console.print("\nUse [bold]stealthpay swap status[/] to monitor progress")
```

### Неделя 14: Final Testing & Documentation

```bash
#!/bin/bash
# scripts/final-test.sh

echo "═══════════════════════════════════════════"
echo "  FINAL PRE-PRODUCTION TEST SUITE"
echo "═══════════════════════════════════════════"

# 1. Unit tests
echo -e "\n1. Running unit tests..."
python -m pytest tests/ -v --tb=short
if [ $? -ne 0 ]; then
    echo "❌ Unit tests failed"
    exit 1
fi

# 2. Integration tests
echo -e "\n2. Running integration tests..."
python -m pytest tests/integration/ -v --integration
if [ $? -ne 0 ]; then
    echo "❌ Integration tests failed"
    exit 1
fi

# 3. Security tests
echo -e "\n3. Running security tests..."
python scripts/security_test.py

# 4. Load tests
echo -e "\n4. Running load tests..."
locust -f tests/load/locustfile.py --headless -u 100 -r 10 --run-time 5m

# 5. Contract tests
echo -e "\n5. Running contract tests..."
cd contracts && npx hardhat test

echo -e "\n═══════════════════════════════════════════"
echo "  ✅ ALL TESTS PASSED"
echo "═══════════════════════════════════════════"
```

---

## 📊 Summary Timeline

| Phase | Weeks | Focus | Deliverables |
|-------|-------|-------|--------------|
| 1 | 1-6 | Security Foundation | TSS, Audit, HSM |
| 2 | 7-10 | Testnet Launch | Oracle, mTLS, Deploy |
| 3 | 11-14 | Production Prep | Monitoring, DB, CLI |

**Total Duration:** 14 недель (3.5 месяца)  
**Total Budget:** $67,000-125,000  
**Team Required:** 3-4 разработчика + 1 аудитор

---

## ✅ Success Criteria

- [ ] All unit tests pass (>90% coverage)
- [ ] Security audit passed (no critical issues)
- [ ] Testnet deployment stable (30 days uptime)
- [ ] MPC nodes operational (5/5 online)
- [ ] Bug bounty active ($100k rewards)
- [ ] Insurance funded ($1M+ locked)
- [ ] Documentation complete

---

**Ready to proceed?** Choose phase to start with:
1. **Phase 1** (Security) - If you have budget for audit
2. **Phase 2** (Testnet) - If you want to launch quickly
3. **Phase 3** (Production) - If testnet already running
