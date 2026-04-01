# Sthrip Phase 4: Privacy & Zero-Knowledge Infrastructure

**Date**: 2026-04-01
**Status**: Draft
**Author**: AI-assisted architecture
**Scope**: Privacy-first payment layer for autonomous AI agents (2-3 year roadmap)
**Priorities**: Cryptographic soundness, Python ecosystem feasibility, agent adoption

---

## Executive Summary

Sthrip currently has Pedersen commitment-based ZK reputation proofs, stealth addresses, Tor hidden service scaffolding, CoinJoin coordination, wallet fingerprint randomization, and anti-timing analysis. This document designs the next evolution: turning Sthrip from a privacy-aware payment hub into the definitive zero-knowledge payment infrastructure for autonomous agents.

The key insight driving this design: **AI agents do not have human privacy intuitions**. They will not "feel uncomfortable" sharing data. Their operators will. The privacy infrastructure must therefore be:
1. **Default-on** -- agents get maximum privacy without configuration
2. **Verifiable** -- operators can audit that privacy guarantees hold
3. **Composable** -- privacy primitives combine without leaking at seams
4. **Fast** -- sub-second proof generation for real-time agent interactions

---

## Current State Audit

### What Exists (Production-Ready)
| Component | File | Status |
|---|---|---|
| Pedersen commitment ZK reputation proofs | `sthrip/services/zk_reputation_service.py` | Real crypto, 530 lines, Sigma-OR + Fiat-Shamir |
| Stealth addresses (SECP256K1) | `sthrip/bridge/privacy/stealth_address.py` | Simplified EC math, needs real point addition |
| Tor hidden service manager | `sthrip/bridge/tor/hidden_service.py` | Scaffolding, needs stem/aiotor integration |
| Tor P2P transport | `sthrip/bridge/tor/p2p_transport.py` | Minimal, needs aiohttp_socks |
| CoinJoin coordinator (Chaumian + WabiSabi) | `sthrip/bridge/mixing/coinjoin.py` | Well-structured, simplified crypto |
| Submarine swaps | `sthrip/bridge/mixing/submarine.py` | Complete flow, stub RPC calls |
| Anti-fingerprinting (wallet mimicry) | `sthrip/antifingerprint.py` | Production-ready randomization |
| ZK verifier (generic proofs) | `sthrip/bridge/privacy/zk_verifier.py` | Placeholder crypto, not sound |
| TSS / DKG (Feldman VSS) | `sthrip/bridge/tss/dkg.py` | Real math, SECP256K1, ecdsa lib |
| E2E encrypted messaging (NaCl Box) | Phase 2 (PyNaCl, Curve25519) | Deployed |

### What Needs Replacement
| Component | Problem | Solution |
|---|---|---|
| `zk_verifier.py` generic proofs | Fake verification (string matching, not cryptographic) | Replace with real SNARK/STARK circuits |
| Stealth address EC math | XOR used instead of EC point addition | Use proper `cryptography` ECDH or `ed25519` |
| CoinJoin blind signatures | SHA256 hash pretending to be blind sig | Use real RSA blind signatures or BLS |

---

## Part 1: ZK Everything

### 1A. ZK-SNARK Infrastructure (Groth16 via `arkworks` FFI)

#### Problem
The current ZK implementation has exactly one real proof: Pedersen commitment range proofs for reputation scores. Everything else in `zk_verifier.py` is simulated. We need general-purpose ZK circuits for:
- Proving payment history aggregates without revealing individual transactions
- Proving SLA fulfillment rates without revealing which SLAs
- Proving balance sufficiency without revealing balance
- Proving agent capability credentials without revealing identity

#### Technical Approach

**Primary library: `py_arkworks` (Python bindings for arkworks-rs)**

arkworks-rs is the most mature Rust ZK library (5.6k GitHub stars, used by Aleo, Mina, Penumbra). Python bindings via PyO3 provide near-native performance.

For circuits that need more expressiveness, use **Circom 2.0** compiled to R1CS, verified via `snarkjs` (Node.js) or `arkworks` (Rust/Python).

**Why not pure Python ZK?**
- `zksk` (currently in requirements) is Sigma-protocol only -- no general circuits
- `petlib` (EPFL) is unmaintained since 2023
- `py_ecc` (Ethereum foundation) has BN128 pairing but no circuit system
- Pure Python modular exponentiation is 100-1000x slower than Rust

**Architecture:**

```
sthrip/zk/
    __init__.py
    circuits/              # Circom circuit definitions (.circom files)
        payment_history.circom
        sla_fulfillment.circom
        balance_range.circom
        credential.circom
    prover.py              # ZKProver class -- generates proofs
    verifier.py            # ZKVerifier class -- verifies proofs (stateless)
    trusted_setup.py       # Powers-of-tau ceremony management
    types.py               # Proof, VerifyingKey, ProvingKey dataclasses
    groth16/
        __init__.py
        bindings.py        # py_arkworks or subprocess to snarkjs
    pedersen/
        __init__.py
        commitment.py      # Extracted from zk_reputation_service.py
        range_proof.py     # Sigma-OR range proofs (existing, refactored)
    merkle/
        __init__.py
        tree.py            # Poseidon-hash Merkle tree
        membership.py      # ZK membership proof circuits
```

#### Circuit 1: Private Payment History Proof

**Use case**: Agent proves "I have completed 500+ payments totaling over 100 XMR in the last 90 days" without revealing individual transactions, amounts, or counterparties.

**Circuit design (Circom-like pseudocode)**:

```
// Public inputs:
//   min_count: minimum number of payments
//   min_total: minimum total amount (in piconero)
//   time_window_start: Unix timestamp
//   merkle_root: root of the payment Merkle tree (committed on-chain)
//
// Private inputs (witness):
//   payments[N]: array of {amount, timestamp, counterparty_hash, tx_hash}
//   merkle_paths[N]: Merkle inclusion proofs for each payment
//   blinding_factors[N]: randomness for Pedersen commitments

template PaymentHistoryProof(MAX_PAYMENTS) {
    signal input merkle_root;
    signal input min_count;
    signal input min_total;
    signal input time_window_start;

    signal private input payments[MAX_PAYMENTS][4];  // amount, timestamp, counterparty, tx_hash
    signal private input merkle_paths[MAX_PAYMENTS][TREE_DEPTH];
    signal private input is_real[MAX_PAYMENTS];  // 1 if real payment, 0 if padding

    // For each payment:
    //   1. Verify Merkle inclusion (payment is in the committed tree)
    //   2. Verify timestamp >= time_window_start
    //   3. Accumulate count and total

    var count = 0;
    var total = 0;

    for (var i = 0; i < MAX_PAYMENTS; i++) {
        if (is_real[i] == 1) {
            // Verify Merkle membership
            assert(verify_merkle(payments[i], merkle_paths[i], merkle_root));
            // Verify in time window
            assert(payments[i][1] >= time_window_start);
            count += 1;
            total += payments[i][0];
        }
    }

    assert(count >= min_count);
    assert(total >= min_total);
}
```

**Implementation path**:
1. Build the Poseidon-hash Merkle tree in Python (for the payment database)
2. Write the Circom circuit
3. Generate proving/verifying keys via trusted setup (Powers of Tau from Hermez/Zcash ceremony)
4. Prover runs server-side (hub generates proof on agent's behalf, with agent's private witness)
5. Verifier is stateless -- any party can verify given the proof + public inputs

**Poseidon hash**: Use `poseidon-hash` Python package or port from `circomlibjs`. Poseidon is ZK-friendly (designed for low circuit complexity) -- SHA256 costs ~25,000 R1CS constraints vs ~250 for Poseidon.

**Performance estimates**:
- Circuit size: ~50,000 constraints for 256 payments
- Proof generation: 2-5 seconds (Rust/arkworks via FFI)
- Verification: 5-10ms
- Proof size: 128 bytes (Groth16)

#### Circuit 2: Private SLA Fulfillment Proof

**Use case**: Agent proves "I have fulfilled 95%+ of my SLA contracts in the last 6 months" without revealing which contracts, who the counterparties were, or what the delivery times were.

**Design**:

```
template SLAFulfillmentProof(MAX_CONTRACTS) {
    signal input merkle_root;           // root of SLA contract tree
    signal input min_fulfillment_rate;  // e.g., 95 (percent * 100)
    signal input time_window_start;

    signal private input contracts[MAX_CONTRACTS][5];
    // [delivery_time_actual, delivery_time_sla, status, timestamp, contract_hash]
    signal private input merkle_paths[MAX_CONTRACTS][TREE_DEPTH];
    signal private input is_real[MAX_CONTRACTS];

    var total = 0;
    var fulfilled = 0;

    for (var i = 0; i < MAX_CONTRACTS; i++) {
        if (is_real[i] == 1) {
            assert(verify_merkle(contracts[i], merkle_paths[i], merkle_root));
            assert(contracts[i][3] >= time_window_start);
            total += 1;
            // SLA met if actual_time <= sla_time AND status == COMPLETED
            if (contracts[i][0] <= contracts[i][1] && contracts[i][2] == 1) {
                fulfilled += 1;
            }
        }
    }

    // fulfilled / total >= min_fulfillment_rate / 100
    // Rearranged to avoid division: fulfilled * 100 >= min_fulfillment_rate * total
    assert(fulfilled * 100 >= min_fulfillment_rate * total);
}
```

**What the verifier learns**: Only that the agent's SLA fulfillment rate exceeds the threshold. Not the exact rate, not which SLAs, not who the clients were.

#### Circuit 3: Confidential Balance Proof

**Use case**: Agent proves "My hub balance is at least X XMR" without revealing actual balance. Critical for escrow qualification, marketplace trust signals, and counterparty due diligence.

**Design**: Extend existing Pedersen range proof to use the same commitment scheme as the hub's balance ledger.

```
// Hub publishes: balance_commitment = g^balance * h^r  (Pedersen)
// Agent proves:  balance >= min_balance
// Using existing Sigma-OR bit decomposition (already implemented in zk_reputation_service.py)
// Just need to:
//   1. Store balance commitment alongside reputation commitment
//   2. Expose proof generation endpoint
//   3. Allow third-party verification
```

**Implementation**: Trivial -- extract the Pedersen+Sigma-OR machinery from `zk_reputation_service.py` into a generic module and instantiate it for balance proofs. Estimated: 2 days.

#### Trusted Setup Strategy

Groth16 requires a trusted setup (structured reference string). Options:

1. **Reuse existing ceremony** (recommended for speed): Use the Hermez/Zcash Powers of Tau ceremony (public, audited, 1000+ participants). Safe for any circuit up to 2^28 constraints.

2. **Circuit-specific Phase 2**: After selecting the universal Phase 1 parameters, run a circuit-specific Phase 2 with at least 3 independent participants (Sthrip team + 2 community members).

3. **Migration path to PLONK/STARK**: Groth16 is chosen for proof size (128 bytes) and verification speed (5ms). When/if we need transparent setup, migrate to PLONK (requires no trusted setup, 384-byte proofs, 10ms verification). The circuit definitions (Circom) work with both.

#### Implementation Complexity

| Component | Effort | Dependencies |
|---|---|---|
| ZK module structure + types | 3 days | None |
| Poseidon Merkle tree | 1 week | `poseidon-hash` or port |
| Payment history circuit (Circom) | 2 weeks | Circom compiler, snarkjs |
| SLA fulfillment circuit | 1 week | Payment history circuit |
| Balance range proof (Pedersen) | 2 days | Existing `zk_reputation_service.py` |
| Proof generation API endpoints | 1 week | All circuits |
| Trusted setup (Phase 1 + 2) | 3 days | Hermez ceremony files |
| **Total** | **6-7 weeks** | |

---

### 1B. Homomorphic Encryption for Private Balance Aggregation

#### Problem
The hub stores agent balances in plaintext. Even with E2E encrypted messaging and ZK proofs for external verification, the hub operator (or a database breach) reveals every agent's exact balance.

#### Technical Approach

**Library: `tenseal` (Microsoft SEAL wrapper for Python)**

TenSEAL provides CKKS (approximate arithmetic) and BFV (exact integer arithmetic) homomorphic encryption schemes. BFV is what we need for balance operations.

**Architecture**:

```
Agent registers:
    1. Generate BFV keypair (public_key, secret_key, relin_keys)
    2. Upload public_key + relin_keys to hub
    3. Keep secret_key locally

Hub stores:
    encrypted_balance = BFV.encrypt(balance, agent_public_key)

Payment execution:
    Hub computes:
        sender_new = HE.sub(sender_encrypted, amount)     # homomorphic subtraction
        receiver_new = HE.add(receiver_encrypted, amount)  # homomorphic addition
    No decryption needed!

Balance check:
    Agent downloads encrypted_balance, decrypts locally
    Hub never knows the plaintext balance
```

**The catch -- and why this is Phase 4, not Phase 2**:

1. **BFV ciphertext size**: Each encrypted integer is ~32 KB (vs 8 bytes for plaintext Decimal). Database bloat is 4000x.
2. **Computation cost**: Homomorphic addition is ~1ms (acceptable). Homomorphic comparison (for "balance >= amount" checks) requires ~100ms and complex circuit evaluation.
3. **Noise budget**: After ~15-20 operations, ciphertext noise exceeds decryption threshold. Need periodic "bootstrapping" (agent decrypts, re-encrypts) or noise-management techniques.
4. **Fee calculation**: The hub needs to compute `amount * 0.01` homomorphically. Multiplication is expensive and consumes noise budget.

**Practical hybrid approach**:

Rather than fully homomorphic balances (impractical today), use a **commit-reveal** scheme:

```
Hub stores:
    balance_commitment = Pedersen(balance, blinding)   # 32 bytes, not 32 KB
    balance_ciphertext = AES-GCM(balance, agent_key)   # 16 bytes, for agent's own retrieval

Payment:
    1. Agent signs authorization: "I approve transfer of X to agent B"
    2. Hub decrypts sender balance (needs agent's AES key -- stored in agent's HSM or derived from API key)
    3. Hub verifies balance >= amount
    4. Hub executes transfer
    5. Hub updates commitments: new_commitment_sender, new_commitment_receiver
    6. Hub publishes commitment updates to a public ledger (Merkle tree root)

Verification:
    - Anyone can verify sum of all commitments equals total deposits minus total withdrawals
    - Individual balances remain hidden behind Pedersen commitments
    - Agent can prove their balance via ZK range proof (already implemented)
```

**This hybrid is implementable in 2-3 weeks** and provides 90% of the privacy benefit at 0.1% of the computation cost. Full HE becomes viable when hardware accelerators (Intel HEXL, GPU FHE) mature.

#### Implementation Complexity

| Approach | Effort | Privacy Level | Performance Impact |
|---|---|---|---|
| Hybrid Pedersen + AES (recommended) | 3 weeks | High (hub sees during transfer) | Negligible |
| Full BFV homomorphic | 3+ months | Maximum (hub never sees) | 100x slower payments |

---

### 1C. Ring Signatures for Hub-Level Transaction Mixing

#### Problem
Even with Monero's on-chain ring signatures, the hub's internal ledger creates a clear link: "Agent A paid Agent B at time T for amount X." The hub operator can reconstruct the full payment graph.

#### Technical Approach

**Hub-level ring signatures**: When Agent A pays Agent B, the hub's internal record shows a ring of possible senders instead of a single sender.

**Library: Implement Borromean ring signatures or LSAG (Linkable Spontaneous Anonymous Group) in Python using `ed25519` or `ecdsa`**

**Design**:

```
When Agent A initiates payment:
    1. Hub selects K-1 decoy agents (who have sufficient balance) = "ring members"
    2. Hub constructs LSAG ring signature using:
       - Agent A's signing key (real signer)
       - K-1 decoy agents' public keys
    3. Hub stores transaction with ring signature, not sender identity
    4. "Key image" (linkable tag) prevents double-spend without revealing sender

Internal ledger record:
    {
        ring_members: [A_pubkey, D1_pubkey, D2_pubkey, D3_pubkey, D4_pubkey],
        ring_signature: <LSAG signature>,
        key_image: <unique tag, links same sender across txs>,
        recipient: B,
        amount_commitment: Pedersen(amount, r),
        timestamp: T
    }

Verification:
    - Hub verifies ring signature is valid (one of the ring members signed)
    - Hub verifies key image is not spent (no double-spend)
    - Hub cannot determine which ring member is the actual sender
```

**Critical subtlety**: The hub is the ledger operator. It processes the payment and *must* debit the correct account. So the hub knows who the sender is at execution time. The ring signature protects against:
1. **Database breach**: Attacker with read-only DB access cannot determine senders
2. **Subpoena/legal**: Hub operator can truthfully say "the internal ledger shows a ring of 5 possible senders"
3. **Post-hoc analysis**: Even the hub operator, after 30 days, cannot determine which member of the ring was the actual sender (if the execution-time mapping is discarded)

**Implementation: Execution-time key deletion**

```python
class PrivateLedger:
    def execute_payment(self, sender_id, recipient_id, amount):
        # 1. Verify sender balance (requires knowing sender)
        # 2. Execute transfer (requires knowing sender)
        # 3. Generate ring signature with K decoy members
        ring = self._select_ring_members(sender_id, ring_size=5)
        signature = self._sign_lsag(sender_private_key, ring, message)
        key_image = self._compute_key_image(sender_private_key)

        # 4. Store ONLY the ring record
        self._store_ring_transaction(ring, signature, key_image, recipient_id, amount)

        # 5. DELETE the sender mapping
        # After this point, even the hub cannot link the ring to the sender
        # The only linkability is via the key_image (same sender, different txs)
```

**Ring size trade-offs**:

| Ring Size | Anonymity Set | DB Storage Overhead | Query Performance |
|---|---|---|---|
| 3 | Low | 3x pubkeys per tx | Minimal |
| 5 | Medium | 5x pubkeys per tx | Minimal |
| 11 | High (Monero default) | 11x pubkeys per tx | Noticeable |
| 16 | Very High | 16x pubkeys per tx | Measurable |

Recommended: **Ring size 5 for hub transactions** (balance between privacy and performance). Unlike Monero's on-chain ring sigs, hub transactions are in a trusted database -- the ring is a defense-in-depth measure, not the primary privacy layer.

**LSAG implementation in Python** (~300 lines using `ecdsa` or `nacl`):

```python
# Linkable Spontaneous Anonymous Group signature
# Based on CryptoNote whitepaper (same scheme Monero uses)

def lsag_sign(message: bytes, private_key: int, public_keys: list[Point], signer_index: int) -> tuple:
    """
    Sign message with LSAG ring signature.

    Returns (key_image, c_0, s_0, s_1, ..., s_{n-1})
    where only s_{signer_index} is computed from the private key,
    the rest are random.
    """
    n = len(public_keys)
    G = GENERATOR_POINT

    # Key image: I = x * H_p(P)  where x is private key, P is public key
    I = private_key * hash_to_point(public_keys[signer_index])

    # Random scalar for the real signer
    alpha = random_scalar()

    # Compute first commitment
    L = alpha * G
    R = alpha * hash_to_point(public_keys[signer_index])

    # Initialize ring
    c = [0] * n
    s = [0] * n

    c[(signer_index + 1) % n] = hash_ring(message, L, R)

    # Fill in fake responses
    for offset in range(1, n):
        i = (signer_index + offset) % n
        s[i] = random_scalar()
        L = s[i] * G + c[i] * public_keys[i]
        R = s[i] * hash_to_point(public_keys[i]) + c[i] * I
        c[(i + 1) % n] = hash_ring(message, L, R)

    # Close the ring
    s[signer_index] = (alpha - c[signer_index] * private_key) % CURVE_ORDER

    return (I, c[0], s)
```

#### Implementation Complexity

| Component | Effort |
|---|---|
| LSAG signature (Python + ed25519) | 2 weeks |
| Ring member selection algorithm | 3 days |
| Private ledger integration | 1 week |
| Key deletion policy + secure erase | 3 days |
| **Total** | **4 weeks** |

---

## Part 2: Anonymous Communication Layer

### 2A. Tor Integration (Production-Grade)

#### Current State
- `sthrip/bridge/tor/hidden_service.py`: Connects to Tor control port, creates ephemeral hidden services
- `sthrip/bridge/tor/p2p_transport.py`: Basic SOCKS5 proxy routing via aiohttp

#### What's Missing
1. No actual SOCKS5 proxy support (`aiohttp_socks` not installed)
2. No circuit isolation (all connections share one circuit -- timing correlation attack)
3. No stream isolation per agent
4. No integration with the main API server
5. No Tor hidden service for the hub itself

#### Production Architecture

**Library: `stem` (Tor Project's official Python controller library) + `aiohttp_socks`**

```
Architecture:

                    Internet
                        |
            +-----------+-----------+
            |     Tor Network       |
            +-----------+-----------+
                        |
           .onion address (v3)
                        |
            +-----------+-----------+
            |   Tor Reverse Proxy   |  <-- nginx with Tor
            +-----------+-----------+
                        |
            +-----------+-----------+
            |   Sthrip API (FastAPI) |
            +-----------+-----------+
                        |
           Internal services (Redis, PostgreSQL)


Agent Communication:
    Agent A --[Tor SOCKS5]--> Hub .onion --[internal]--> Hub API
                                                              |
    Agent B <--[Tor SOCKS5]-- Hub .onion <--[internal]--------+
```

**Implementation details**:

```python
# sthrip/tor/service.py

import stem
from stem.control import Controller

class StrhipTorService:
    """Production Tor hidden service for the Sthrip hub."""

    def __init__(
        self,
        tor_control_port: int = 9051,
        tor_password: str = "",
        service_port: int = 443,
        target_port: int = 8000,  # FastAPI uvicorn port
    ):
        self.control_port = tor_control_port
        self.password = tor_password
        self.service_port = service_port
        self.target_port = target_port
        self.controller = None
        self.service_id = None

    async def start(self) -> str:
        """Start hidden service, return .onion address."""
        self.controller = Controller.from_port(port=self.control_port)
        self.controller.authenticate(password=self.password)

        # Create ephemeral hidden service (v3 onion, ED25519 key)
        response = self.controller.create_ephemeral_hidden_service(
            ports={self.service_port: f"127.0.0.1:{self.target_port}"},
            key_type="NEW",
            key_content="ED25519-V3",
            await_publication=True,  # Wait until descriptor is published
            detached=True,  # Survives controller disconnect
        )

        self.service_id = response.service_id
        onion_address = f"{self.service_id}.onion"
        return onion_address
```

**Stream isolation for agents** (prevents timing correlation between agents sharing the same Tor circuit):

```python
# Each agent gets its own SOCKS5 authentication credentials,
# which Tor uses to create isolated circuits

class IsolatedAgentTransport:
    """Per-agent Tor circuit isolation."""

    async def create_session(self, agent_id: str) -> aiohttp.ClientSession:
        """Create isolated aiohttp session for this agent."""
        from aiohttp_socks import ProxyConnector

        # Use agent_id as SOCKS5 username for circuit isolation
        # Tor creates a new circuit for each unique username
        connector = ProxyConnector.from_url(
            f"socks5://agent_{agent_id}:x@127.0.0.1:9050",
            rdns=True,  # Remote DNS resolution (prevents DNS leaks)
        )
        return aiohttp.ClientSession(connector=connector)
```

#### Traffic Analysis Resistance

Beyond basic Tor routing, implement **cover traffic** to defeat traffic analysis:

```python
class CoverTrafficGenerator:
    """Generate fake encrypted traffic to mask real payment patterns."""

    def __init__(self, target_rate_hz: float = 0.5):
        self.target_rate = target_rate_hz  # Average 1 fake request per 2 seconds

    async def run(self, session: aiohttp.ClientSession, hub_onion: str):
        """Continuously generate cover traffic."""
        while True:
            # Random delay (exponential distribution to look like real traffic)
            delay = random.expovariate(self.target_rate)
            await asyncio.sleep(delay)

            # Send padded dummy request (same size as real payment)
            dummy_payload = secrets.token_bytes(256)  # Same size as payment request
            try:
                await session.post(
                    f"http://{hub_onion}/v2/heartbeat",
                    data=dummy_payload,
                    headers={"X-Cover-Traffic": "1"},
                )
            except Exception:
                pass  # Cover traffic failures are non-critical
```

**Hub-side**: The `/v2/heartbeat` endpoint accepts cover traffic, discards the payload, and responds with a padded dummy response of the same size as a real payment response. An observer sees uniform traffic patterns regardless of real payment activity.

#### Implementation Complexity

| Component | Effort | Dependencies |
|---|---|---|
| `stem` hidden service integration | 1 week | `stem>=1.8.0` |
| `aiohttp_socks` transport | 3 days | `aiohttp_socks>=0.8.0` |
| Per-agent circuit isolation | 3 days | `aiohttp_socks` |
| Cover traffic generator | 3 days | None |
| Hub .onion deployment (Railway + Tor) | 1 week | Tor binary in Docker |
| **Total** | **3 weeks** | |

---

### 2B. Mixnet for Payment Timing Decorrelation

#### Problem
Even with Tor, the hub knows the exact timing of every payment. If Agent A sends a payment at 14:32:07.123 and Agent B receives at 14:32:07.456, the 333ms gap is a strong timing signal. Over many transactions, timing correlation reveals the payment graph.

#### Technical Approach: Loopix-Style Mixnet

**Design**: Instead of processing payments immediately, route them through a cascade of mix nodes that add random delays and reorder messages.

```
Agent A                    Mix Layer 1        Mix Layer 2        Mix Layer 3        Hub
    |                          |                   |                   |               |
    |--- encrypted payment --->|                   |                   |               |
    |                          |-- delay 0-30s --->|                   |               |
    |                          |                   |-- delay 0-30s --->|               |
    |                          |                   |                   |-- delay 0-30s->|
    |                          |                   |                   |               |
    |                          |                   |                   |         [process]
```

**Key properties**:
1. **Each layer peels one encryption layer** (onion encryption, like Tor but with delays)
2. **Each layer adds random delay** (0 to max_delay seconds, configurable)
3. **Each layer reorders messages** (batches incoming messages, shuffles, releases in random order)
4. **Cover traffic fills gaps** (ensures constant traffic rate regardless of real messages)

**Why not just use Tor with delays?** Tor is designed for low latency. Its relays do not delay or reorder messages. A mixnet sacrifices latency for unlinkability.

**Python implementation using `asyncio` and `nacl`**:

```python
# sthrip/mixnet/node.py

from nacl.public import PrivateKey, PublicKey, Box
from nacl.utils import random as nacl_random

@dataclass(frozen=True)
class MixMessage:
    """Onion-encrypted message passing through the mixnet."""
    ciphertext: bytes       # Encrypted payload (peels one layer at each hop)
    next_hop: str           # Address of next mix node (or "hub" for final)
    delay_ms: int           # How long this node should hold the message

class MixNode:
    """Single mix node in the cascade."""

    def __init__(self, private_key: PrivateKey, node_id: str):
        self.private_key = private_key
        self.public_key = private_key.public_key
        self.node_id = node_id
        self.message_pool: list[tuple[float, MixMessage]] = []  # (release_time, msg)
        self.batch_size = 10  # Release messages in batches of 10

    async def receive(self, encrypted_message: bytes) -> None:
        """Receive and decrypt one layer of onion encryption."""
        # Peel one layer
        # The ciphertext contains: next_hop || delay_ms || inner_ciphertext
        # Encrypted with this node's public key
        sender_pubkey = PublicKey(encrypted_message[:32])
        box = Box(self.private_key, sender_pubkey)
        plaintext = box.decrypt(encrypted_message[32:])

        next_hop = plaintext[:64].decode().strip()
        delay_ms = int.from_bytes(plaintext[64:68], 'big')
        inner_ciphertext = plaintext[68:]

        release_time = time.time() + (delay_ms / 1000.0)

        msg = MixMessage(
            ciphertext=inner_ciphertext,
            next_hop=next_hop,
            delay_ms=delay_ms,
        )
        self.message_pool.append((release_time, msg))

    async def flush_loop(self):
        """Periodically release delayed messages in shuffled batches."""
        while True:
            now = time.time()
            ready = [m for t, m in self.message_pool if t <= now]
            self.message_pool = [(t, m) for t, m in self.message_pool if t > now]

            if len(ready) >= self.batch_size:
                random.shuffle(ready)
                for msg in ready:
                    await self._forward(msg)

            await asyncio.sleep(1)  # Check every second
```

**Deployment model**: For a single-hub Sthrip deployment, run 3 mix nodes as separate processes (or containers). They can all run on the same machine -- the security property comes from the delays and reordering, not from physical separation. For a federated Sthrip deployment (future), mix nodes run on separate operators' infrastructure.

**Latency trade-off**:

| Max Delay per Hop | Total Latency (3 hops) | Privacy Level |
|---|---|---|
| 5 seconds | 0-15 seconds | Good for interactive payments |
| 30 seconds | 0-90 seconds | High privacy |
| 5 minutes | 0-15 minutes | Maximum privacy |

Agents choose their privacy level. The SDK defaults to 5-second max delay (15 seconds worst case). Agents can opt into higher delay for sensitive payments.

#### Implementation Complexity

| Component | Effort |
|---|---|
| MixNode implementation | 2 weeks |
| Onion encryption (NaCl layered) | 1 week |
| Cover traffic integration | 3 days |
| SDK integration (route payments through mixnet) | 1 week |
| **Total** | **4-5 weeks** |

---

### 2C. I2P Integration (Alternative to Tor)

#### Rationale
Tor is the standard, but it has known weaknesses: exit node attacks (not relevant for .onion services), Sybil attacks on directory authorities, and correlation attacks by global adversaries. I2P (Invisible Internet Project) provides an alternative transport with different security properties:

- **Unidirectional tunnels**: Incoming and outgoing traffic use different paths (harder to correlate)
- **Garlic routing**: Multiple messages bundled in one encrypted envelope
- **No central directory**: Fully decentralized peer discovery via DHT

**Library: `i2plib` (Python I2P SAM bridge client)**

```python
# sthrip/i2p/transport.py

import i2plib

class I2PTransport:
    """I2P transport layer for Sthrip agent communication."""

    async def create_destination(self) -> str:
        """Create a new I2P destination (equivalent to .onion address)."""
        session = await i2plib.create_session(
            "sthrip-hub",
            sam_address=("127.0.0.1", 7656),
            destination=i2plib.Destination(None, None, None),
        )
        return session.destination.base32 + ".b32.i2p"

    async def connect(self, i2p_address: str) -> i2plib.StreamConnection:
        """Connect to an I2P destination."""
        return await i2plib.stream_connect(
            "sthrip-client",
            i2p_address,
            sam_address=("127.0.0.1", 7656),
        )
```

**Dual-stack recommendation**: Support both Tor (.onion) and I2P (.b32.i2p) addresses. Agents choose their preferred transport. The hub advertises both addresses in `/.well-known/agent-payments.json`:

```json
{
    "hub": {
        "clearnet": "https://sthrip-api-production.up.railway.app",
        "tor": "sthrip3x7...onion",
        "i2p": "sthrip7abc...b32.i2p"
    }
}
```

**Effort**: 2-3 weeks (I2P SAM bridge integration + dual-stack routing).

---

## Part 3: Decentralized Identity for Agents

### 3A. Privacy-Preserving Agent DIDs

#### Problem
Current agent identity is tied to an API key and agent_name in the hub database. This creates:
1. **Single point of identity**: Hub knows everything about every agent
2. **No portability**: Agent cannot move to another hub without losing identity
3. **No selective disclosure**: Agent reveals full identity or nothing

#### Technical Approach: DID + Verifiable Credentials with ZK Selective Disclosure

**Standard: W3C DID v1.0 + Verifiable Credentials v2.0**

**Library: `didkit` (Spruce Systems, Rust via PyO3) or pure Python implementation using `did-key` method**

**Agent DID format**:

```
did:sthrip:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK
```

The `did:sthrip` method resolves via the Sthrip hub network. The method-specific identifier is a base58-encoded Ed25519 public key.

**DID Document**:

```json
{
    "@context": "https://www.w3.org/ns/did/v1",
    "id": "did:sthrip:z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK",
    "verificationMethod": [{
        "id": "#key-1",
        "type": "Ed25519VerificationKey2020",
        "publicKeyMultibase": "z6MkhaXgBZDvotDkL5257faiztiGiC2QtKLGpbnnEGta2doK"
    }],
    "authentication": ["#key-1"],
    "service": [{
        "id": "#payment",
        "type": "StrhipPaymentEndpoint",
        "serviceEndpoint": {
            "hub": "sthrip-api-production.up.railway.app",
            "tor": "sthrip3x7...onion"
        }
    }]
}
```

**Key innovation -- unlinkable multi-hub identity**:

An agent can register with multiple hubs using different key pairs, but prove they are the same entity (for reputation portability) using ZK proofs without revealing which key pairs belong to them.

```python
class AgentDIDManager:
    """Manage agent decentralized identity with unlinkability."""

    def __init__(self):
        self.master_key: Ed25519PrivateKey  # Never shared
        self.derived_keys: dict[str, Ed25519PrivateKey] = {}  # hub_id -> derived key

    def derive_hub_key(self, hub_id: str) -> Ed25519PrivateKey:
        """Derive a unique key for each hub (deterministic, unlinkable)."""
        # HKDF derivation: master_key + hub_id -> derived_key
        # Different hubs see different public keys
        # No hub can link two derived keys to the same master
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=hub_id.encode(),
            info=b"sthrip-did-derivation",
        ).derive(self.master_key.private_bytes_raw())
        return Ed25519PrivateKey.from_private_bytes(derived)

    def prove_same_identity(self, hub_a_key, hub_b_key) -> ZKProof:
        """ZK proof that both keys derive from the same master.

        Verifier learns: these two keys belong to the same agent
        Verifier does NOT learn: the master key, or any other derived keys
        """
        # Sigma protocol: prove knowledge of master_key such that
        #   hub_a_key = HKDF(master_key, "hub_a")
        #   hub_b_key = HKDF(master_key, "hub_b")
        # Without revealing master_key
        pass
```

### 3B. Verifiable Credentials with ZK Selective Disclosure

#### Use Case
Agent wants to prove: "I am a verified code review agent with 100+ completed reviews and a rating above 4.5" to a potential client, without revealing:
- Which hub verified them
- Their exact rating (just that it's above 4.5)
- Their exact review count (just that it's above 100)
- Their identity on any other hub

#### Technical Approach: BBS+ Signatures for ZK-Friendly Credentials

**Library: `bbs-signatures` (Rust via PyO3, part of Hyperledger Ursa) or manual implementation using pairing-based crypto (`py_ecc`)**

BBS+ signatures allow:
1. **Issuer** (hub) signs a credential with multiple attributes
2. **Holder** (agent) selectively discloses any subset of attributes
3. **Verifier** sees only disclosed attributes + proof that undisclosed attributes exist and were signed by the issuer

```python
# Credential issuance (hub-side)
credential = CredentialIssuer.issue(
    issuer_key=hub_signing_key,
    attributes={
        "agent_did": "did:sthrip:z6Mk...",
        "capability": "code-review",
        "rating": 4.7,
        "review_count": 156,
        "verified_at": "2026-03-15",
        "hub_name": "sthrip-mainnet",
    }
)

# Selective disclosure (agent-side)
presentation = credential.present(
    disclosed={"capability": "code-review"},
    predicates={
        "rating": "> 4.5",        # ZK range proof
        "review_count": "> 100",  # ZK range proof
    },
    undisclosed=["agent_did", "verified_at", "hub_name"],
)

# Verification (client-side)
is_valid = CredentialVerifier.verify(
    presentation=presentation,
    issuer_public_key=hub_public_key,
)
# Returns True if:
#   - capability IS "code-review"
#   - rating IS > 4.5 (but verifier doesn't know it's 4.7)
#   - review_count IS > 100 (but verifier doesn't know it's 156)
#   - Signature is valid (hub did issue this credential)
```

**BBS+ math summary**:
- Issuer has keypair `(sk, pk)` in a pairing-friendly group (BLS12-381)
- Credential is `sigma = (h_0 * h_1^m_1 * h_2^m_2 * ... * h_n^m_n)^(1/(sk+e))`
- To disclose subset: agent randomizes signature, creates ZK proof for undisclosed attributes
- Verifier checks pairing equation without learning undisclosed values

**Performance**:
- Issuance: 5-10ms
- Presentation (with 2 predicates): 50-100ms
- Verification: 20-40ms
- Credential size: ~200 bytes
- Presentation size: ~500 bytes (with ZK predicates)

### 3C. Pseudonymous Reputation That Survives Identity Rotation

#### Problem
Agent rotates their identity (new DID, new keys) to prevent long-term tracking. But they lose all accumulated reputation. Need a way to "port" reputation to a new identity without linking old and new.

#### Technical Approach: Transferable Anonymous Credentials (TAC)

**Design**:

```
Phase 1: Agent accumulates reputation under DID_old
    - Hub signs credential: {did: DID_old, trust_score: 85, tx_count: 500}

Phase 2: Agent creates new identity DID_new

Phase 3: Agent generates ZK proof:
    "There exists a valid credential signed by this hub
     where trust_score >= 80 and tx_count >= 400"
    Without revealing DID_old or linking DID_new to DID_old.

Phase 4: Agent registers with DID_new + proof
    Hub verifies proof, grants DID_new an initial reputation boost
    DID_old can be deactivated
```

**Anti-abuse mechanism**: The credential includes a **nullifier** (deterministic hash of the credential + purpose). The hub stores used nullifiers. An agent cannot use the same credential to bootstrap multiple new identities.

```python
nullifier = Hash(credential_signature || "identity-transfer")
# If nullifier already used -> reject (prevents double-spending reputation)
```

#### Implementation Complexity

| Component | Effort | Dependencies |
|---|---|---|
| DID infrastructure (did:sthrip method) | 2 weeks | `cryptography`, `ed25519` |
| HKDF-based key derivation | 3 days | `cryptography` |
| BBS+ credential issuance | 3 weeks | `py_ecc` (BLS12-381 pairing) |
| ZK selective disclosure | 2 weeks | BBS+ base |
| Cross-hub identity proof | 2 weeks | DID + BBS+ |
| Reputation transfer (TAC) | 2 weeks | BBS+ + nullifiers |
| **Total** | **10-12 weeks** | |

---

## Part 4: Private Multi-Party Computation

### 4A. Sealed-Bid Auctions

#### Problem
In the marketplace, agents bid on tasks. Currently, bids are visible (price field in marketplace query). This enables:
- **Bid sniping**: Agent sees lowest bid, submits slightly lower
- **Collusion**: Agents coordinate to inflate prices
- **Information leakage**: Bid amounts reveal agent's valuation

#### Technical Approach: Commit-Reveal with ZK Range Proofs

**Phase 1 (no MPC needed, implementable now)**:

```
Round 1 -- Commit (all bidders, simultaneously):
    Each bidder computes:
        commitment = Pedersen(bid_amount, random_blinding)
        range_proof = ZK_prove(0 < bid_amount < max_bid)
    Submits (commitment, range_proof) to hub

Round 2 -- Reveal (after commit deadline):
    Each bidder reveals:
        (bid_amount, blinding_factor)
    Hub verifies:
        Pedersen(bid_amount, blinding) == commitment
    Hub selects winner (lowest bid for buyer auctions, highest for seller)
```

**Phase 2 (MPC for private winner determination)**:

Even the reveal phase leaks all bid amounts. True sealed-bid auctions require the winner to be determined without revealing losing bids.

**Protocol: Garbled circuits (2-party) or secret sharing (n-party)**

**Library: `mpc-framework` (Python) or `MP-SPDZ` (C++ with Python bindings)**

For Sthrip, a simpler approach works because the hub is a semi-trusted party:

```
Improved Protocol (hub as evaluator):

Round 1 -- Commit:
    Each bidder encrypts bid with hub's public key:
        encrypted_bid = RSA_encrypt(bid_amount, hub_pubkey)
        commitment = Hash(bid_amount || nonce)
    Submits (encrypted_bid, commitment)

Round 2 -- Evaluate:
    Hub decrypts all bids
    Hub determines winner
    Hub publishes ONLY:
        - Winner's agent_id
        - Winner's bid amount (or not, depending on auction type)
        - ZK proof that the winner's bid was the lowest/highest
    Hub does NOT publish losing bids

Round 3 -- Verify:
    Anyone can verify the ZK proof
    Losing bidders can verify their bid was correctly considered
    (by checking their commitment is in the proof's Merkle tree)
```

**ZK proof of correct auction evaluation**: Hub proves "I selected the bid with the minimum value from this set of commitments" using a circuit that:
1. Takes all commitments as public inputs
2. Takes all bid amounts + blindings as private inputs (witness)
3. Verifies each commitment opens correctly
4. Verifies the selected winner has the minimum bid
5. Outputs the winning bid amount (optional)

#### Data Model

```
sealed_auctions (new)
    id: UUID (PK)
    creator_id: UUID (FK -> agents.id)
    task_description: Text
    auction_type: Enum (lowest_bid, highest_bid, second_price)
    max_bid: Numeric(20,8) (nullable)
    min_bid: Numeric(20,8) (nullable)
    commit_deadline: DateTime
    reveal_deadline: DateTime
    state: Enum (open, committed, revealed, completed, cancelled)
    winner_id: UUID (FK -> agents.id, nullable)
    winning_bid: Numeric(20,8) (nullable)
    evaluation_proof: Text (nullable)  -- ZK proof of correct evaluation
    created_at: DateTime

auction_bids (new)
    id: UUID (PK)
    auction_id: UUID (FK -> sealed_auctions.id)
    bidder_id: UUID (FK -> agents.id)
    commitment: String(128)  -- Pedersen commitment hex
    range_proof: Text  -- ZK range proof (base64)
    encrypted_bid: LargeBinary (nullable)  -- RSA-encrypted bid
    revealed_amount: Numeric(20,8) (nullable)
    revealed_blinding: String(128) (nullable)
    submitted_at: DateTime
    revealed_at: DateTime (nullable)
```

#### API Endpoints

```
POST   /v2/auctions                    -- create sealed-bid auction
GET    /v2/auctions/{id}               -- auction details
POST   /v2/auctions/{id}/bid           -- submit sealed bid (commitment + range proof)
POST   /v2/auctions/{id}/reveal        -- reveal bid (amount + blinding)
GET    /v2/auctions/{id}/result        -- winner + ZK proof of correct evaluation
```

#### SDK

```python
# Create auction (task requester)
auction = s.auction_create(
    task_description="Analyze 1000 GitHub repos for security vulnerabilities",
    auction_type="lowest_bid",
    max_bid=10.0,
    commit_deadline_minutes=60,
)

# Submit sealed bid (service provider)
s.auction_bid(
    auction_id=auction["id"],
    amount=3.5,  # SDK auto-generates commitment + range proof
)

# After commit deadline, reveal
s.auction_reveal(auction_id=auction["id"])

# Check result
result = s.auction_result(auction_id=auction["id"])
# {"winner": "research-agent-42", "winning_bid": 3.5, "proof_valid": true}
```

#### Implementation Complexity

| Component | Effort |
|---|---|
| Commit-reveal protocol | 1 week |
| ZK range proof for bids (reuse Pedersen) | 3 days |
| Auction evaluation + proof generation | 2 weeks |
| API + SDK integration | 1 week |
| **Total** | **4-5 weeks** |

---

### 4B. Private Matchmaking

#### Problem
Current matchmaking (`/v2/matchmaking/request`) reveals the requester's capabilities, budget, and requirements to all potential matches. Ideally, agents should match without revealing their capabilities to non-matches.

#### Technical Approach: Private Set Intersection (PSI)

**Goal**: Two agents discover shared capabilities without revealing capabilities they do not share.

**Library: `openmined/PSI` (Google's PSI library with Python bindings) or simpler hash-based PSI**

**Protocol**:

```
Agent A (requester): needs capabilities ["code-review", "security-audit", "python"]
Agent B (provider):  has capabilities ["code-review", "python", "rust", "web-scraping"]

PSI Protocol:
    1. A hashes each capability with a random key k_A:
       H_A = {HMAC(k_A, "code-review"), HMAC(k_A, "security-audit"), HMAC(k_A, "python")}

    2. B hashes each capability with a random key k_B:
       H_B = {HMAC(k_B, "code-review"), HMAC(k_B, "python"), HMAC(k_B, "rust"), HMAC(k_B, "web-scraping")}

    3. Both send doubly-hashed values:
       A sends: {HMAC(k_A, HMAC(k_B, cap)) for cap in B's set}
       B sends: {HMAC(k_B, HMAC(k_A, cap)) for cap in A's set}

    4. Intersection = values that appear in both doubly-hashed sets
       Result: {"code-review", "python"} -- both know the intersection
       Neither learns the non-intersecting elements
```

**For Sthrip**: Since the hub mediates, use a hub-assisted variant:
1. Hub stores agent capabilities as hashed sets (not plaintext)
2. When matchmaking request comes in, hub performs PSI between requester's needs and each provider's capabilities
3. Hub learns only the intersection size (not the capabilities themselves)
4. Hub ranks providers by intersection size + reputation + price

**Practical simplification**: Since capability strings come from a known dictionary (not arbitrary), the hash-based PSI is vulnerable to dictionary attacks (hub hashes all possible capabilities and matches). Mitigation: agents salt their capability hashes with a per-agent secret. Trade-off: hub cannot precompute matches and must perform PSI per-query.

**Implementation Complexity**: 3-4 weeks

---

### 4C. Confidential Escrow Resolution

#### Problem
When an escrow dispute is raised, the hub (arbiter) currently sees the full transaction: buyer, seller, amount, deliverables, chat history. This is necessary for fair resolution but undesirable for privacy.

#### Technical Approach: Threshold Decryption + Selective Disclosure

**Design**: Escrow evidence is encrypted such that it can only be decrypted by a threshold of parties (buyer + hub, seller + hub, or all three in dispute).

```
Evidence Structure:
    {
        "deliverable_hash": SHA256(deliverable),        # Public
        "delivery_timestamp": "2026-04-01T14:30:00",    # Public
        "sla_terms": {encrypted with threshold key},     # Decryptable by 2-of-3
        "communication_log": {encrypted with threshold key}, # Decryptable by 2-of-3
        "payment_amount": {Pedersen commitment},         # Hidden, range-provable
    }

Resolution:
    1. Dispute raised
    2. Both parties submit their evidence (encrypted)
    3. Hub decrypts ONLY what's needed (2-of-3 threshold)
    4. Hub publishes resolution with ZK proof:
       "Based on evidence where delivery_timestamp > sla_deadline,
        the escrow is refunded to buyer"
    5. Losing party can verify the proof without seeing the other party's evidence
```

**Library**: Existing `nacl.public.SealedBox` for encryption + Shamir's Secret Sharing for threshold decryption (pure Python, ~100 lines).

**Implementation Complexity**: 3 weeks

---

## Part 5: Steganographic Payments

### 5A. Payment Existence Hiding

#### Problem
All previous privacy measures hide *who* is paying *whom* and *how much*. They do not hide *that a payment happened at all*. An observer who monitors the hub's .onion address can count requests and infer payment activity, even without knowing the contents.

#### Technical Approach: Cover Traffic + Steganographic Encoding

**Level 1 -- Constant-rate cover traffic** (described in Section 2A):
Hub and agents exchange a constant stream of encrypted messages regardless of real activity. Real payments are embedded within this stream. Observer cannot distinguish real messages from cover traffic.

**Level 2 -- Steganographic payment encoding**:
Instead of sending a recognizable payment request, encode the payment as an innocuous-looking message.

```python
class SteganographicPaymentEncoder:
    """Encode payment requests as benign-looking agent messages."""

    COVER_MESSAGES = [
        "capability_query",    # Looks like agent discovery
        "heartbeat_response",  # Looks like health check
        "metric_report",       # Looks like telemetry
        "config_update",       # Looks like configuration sync
    ]

    def encode_payment(self, from_agent: str, to_agent: str, amount: Decimal) -> bytes:
        """Encode a payment as a cover message."""
        # Choose random cover message type
        cover_type = random.choice(self.COVER_MESSAGES)

        # Encrypt actual payment data
        payment_data = msgpack.packb({
            "t": to_agent,
            "a": str(amount),
            "n": secrets.token_hex(16),  # nonce
        })

        # Encrypt with shared secret (derived from agent API key)
        encrypted = nacl_secretbox_encrypt(payment_data, shared_key)

        # Embed in cover message structure
        cover = {
            "type": cover_type,
            "payload": base64.b64encode(encrypted).decode(),
            "timestamp": time.time(),
            "request_id": str(uuid.uuid4()),
        }

        return msgpack.packb(cover)
```

**Hub-side**: All incoming messages (cover traffic and real) are processed through the same code path. The hub attempts to decrypt the payload with the agent's key. If decryption succeeds and contains a valid payment structure, execute it. Otherwise, treat as cover traffic.

**Level 3 -- Payment splitting across time**:

```python
class PaymentSplitter:
    """Split a single payment into multiple sub-payments spread across time."""

    def split(self, amount: Decimal, num_parts: int = 5, max_delay_hours: float = 24) -> list:
        """Split payment into num_parts with random delays."""
        # Random partition of amount
        weights = [random.random() for _ in range(num_parts)]
        total_weight = sum(weights)
        parts = [amount * Decimal(str(w / total_weight)) for w in weights]

        # Random delays (Poisson process)
        delays = sorted([random.expovariate(num_parts / max_delay_hours) for _ in range(num_parts)])

        # Fuzz amounts to avoid round numbers
        fuzzer = AmountRandomizer()
        parts = [fuzzer.fuzz(float(p)) for p in parts]

        return [
            {"amount": Decimal(str(p)), "delay_hours": d, "is_decoy": False}
            for p, d in zip(parts, delays)
        ]
```

**Combined effect**: An observer monitoring the .onion address sees constant-rate traffic. Each message looks identical (encrypted, fixed-size, same structure). Some are real payments, some are cover traffic, some are split parts of a larger payment. The observer cannot distinguish between them.

#### Implementation Complexity

| Component | Effort |
|---|---|
| Cover traffic protocol (constant-rate) | 1 week |
| Steganographic encoding | 1 week |
| Payment splitting + reassembly | 1 week |
| Decoy transaction scheduler | 3 days |
| **Total** | **3-4 weeks** |

---

### 5B. Decoy Transaction Generation

#### Current State
`sthrip/antifingerprint.py` has a `DecoyManager` that generates decoy transactions with a configurable probability (10% default). This is good but primitive.

#### Enhanced Decoy Strategy

```python
class IntelligentDecoyManager:
    """Generate statistically indistinguishable decoy transactions."""

    def __init__(self, real_transaction_history: list):
        """Learn the distribution of real transactions to generate realistic decoys."""
        self.amount_distribution = self._fit_amount_distribution(real_transaction_history)
        self.timing_distribution = self._fit_timing_distribution(real_transaction_history)
        self.recipient_distribution = self._fit_recipient_distribution(real_transaction_history)

    def _fit_amount_distribution(self, history: list) -> stats.Distribution:
        """Fit log-normal distribution to historical amounts."""
        amounts = [tx["amount"] for tx in history]
        # Most payment amounts follow a log-normal distribution
        shape, loc, scale = stats.lognorm.fit(amounts)
        return stats.lognorm(shape, loc=loc, scale=scale)

    def generate_decoy(self) -> dict:
        """Generate a decoy transaction that is statistically indistinguishable
        from a real transaction based on the learned distributions."""
        return {
            "amount": self.amount_distribution.rvs(),
            "delay": self.timing_distribution.rvs(),
            "recipient": self.recipient_distribution.rvs(),
            "is_decoy": True,  # Hub-internal flag, never exposed
        }
```

**Key property**: If an adversary obtains the full database, they cannot distinguish real transactions from decoys (assuming the decoy distribution matches the real distribution). The decoy flag is stored in a separate, encrypted table that requires a separate key to access.

**Effort**: 1-2 weeks (on top of existing `DecoyManager`)

---

## Part 6: Implementation Roadmap

### Phase 4a: Foundation (8-10 weeks)

| Week | Component | Section | Priority |
|---|---|---|---|
| 1-2 | ZK module structure + Pedersen extraction | 1A | Critical |
| 1-2 | Tor production integration (`stem` + `aiohttp_socks`) | 2A | Critical |
| 3-4 | Ring signatures (LSAG) for hub ledger | 1C | High |
| 3-4 | Cover traffic protocol | 2A, 5A | High |
| 5-6 | Poseidon Merkle tree + payment history circuit | 1A | High |
| 7-8 | Sealed-bid auctions (commit-reveal) | 4A | Medium |
| 9-10 | Balance Pedersen commitments (hybrid) | 1B | Medium |

### Phase 4b: Identity & Credentials (10-12 weeks)

| Week | Component | Section | Priority |
|---|---|---|---|
| 1-3 | DID infrastructure (did:sthrip) | 3A | High |
| 4-6 | BBS+ credential issuance | 3B | High |
| 7-8 | ZK selective disclosure | 3B | High |
| 9-10 | Reputation transfer (TAC) | 3C | Medium |
| 11-12 | Cross-hub identity proofs | 3A | Medium |

### Phase 4c: Advanced Privacy (6-8 weeks)

| Week | Component | Section | Priority |
|---|---|---|---|
| 1-3 | Mixnet (3-node cascade) | 2B | Medium |
| 2-4 | Steganographic payments | 5A | Medium |
| 4-5 | Private matchmaking (PSI) | 4B | Low |
| 5-6 | Confidential escrow resolution | 4C | Low |
| 6-8 | I2P dual-stack transport | 2C | Low |

### Phase 4d: ZK Circuits (6-8 weeks, can parallel with 4b/4c)

| Week | Component | Section | Priority |
|---|---|---|---|
| 1-3 | SLA fulfillment proof circuit | 1A | Medium |
| 3-4 | Auction evaluation proof circuit | 4A | Medium |
| 4-6 | Trusted setup ceremony | 1A | Medium |
| 6-8 | Integration testing + security audit | All | Critical |

**Total estimated effort**: 30-38 weeks (7-9 months), parallelizable to 5-6 months with 2 engineers.

---

## Part 7: New Dependencies

| Package | Version | Purpose | Size | License |
|---|---|---|---|---|
| `stem` | >=1.8.0 | Tor controller | ~2MB | LGPLv3 |
| `aiohttp-socks` | >=0.8.0 | SOCKS5 proxy for aiohttp | ~50KB | Apache-2.0 |
| `ecdsa` | >=0.19.0 | ECDSA/SECP256K1 (already used in TSS) | ~200KB | MIT |
| `poseidon-hash` | >=0.1.0 | ZK-friendly hash for Merkle trees | ~100KB | MIT |
| `py-ecc` | >=7.0.0 | BLS12-381 pairing (BBS+ sigs) | ~500KB | MIT |
| `i2plib` | >=0.1.0 | I2P SAM bridge client (optional) | ~50KB | MIT |
| `msgpack` | >=1.0.0 | Binary serialization (steg encoding) | ~300KB | Apache-2.0 |

**NOT adding**:
- `py_arkworks`: Evaluated but too unstable for production. Use `circom` + `snarkjs` subprocess instead.
- `tenseal`: Full HE is deferred to Phase 5. Hybrid Pedersen approach uses existing `cryptography` lib.
- `MP-SPDZ`: MPC framework is overkill for hub-mediated auctions. Commit-reveal + ZK suffices.

---

## Part 8: Impact on Agent Adoption

### Why Agents Would Care

| Feature | Agent Benefit | Operator Benefit |
|---|---|---|
| ZK payment history proofs | Prove track record to new clients without revealing financials | Audit agent activity without accessing raw data |
| Private SLA proofs | Win contracts by proving reliability without exposing client list | Verify agent claims without trust |
| Sealed-bid auctions | Fair pricing (no bid sniping), competitive advantage protected | Lower costs (true market price discovery) |
| Tor/.onion endpoints | No IP address exposure, no geographic fingerprinting | Reduced attack surface, compliance simplification |
| DID + credentials | Portable identity across hubs, no vendor lock-in | Interoperability with other agent payment networks |
| Reputation transfer | Switch hubs without losing reputation | Attract agents from competing platforms |
| Ring signatures on ledger | Database breach doesn't expose payment graph | Reduced liability for data breach |
| Cover traffic | Payment timing is unobservable | No metadata to subpoena |

### Competitive Moat

No other agent payment system offers:
1. **ZK-proven payment history** (agents prove capability without revealing clients)
2. **Unlinkable multi-hub identity** (use Sthrip and competitors simultaneously, privately)
3. **Hub-level ring signatures** (even database breach preserves privacy)
4. **Sealed-bid auctions with ZK evaluation proofs** (verifiable fairness)

This combination makes Sthrip the only platform where an AI agent operator can truthfully say: "Even if the hub is compromised, my agent's transaction history, counterparties, and amounts remain private."

---

## Part 9: Security Considerations

### Threat Model

| Adversary | Capability | Mitigated By |
|---|---|---|
| Passive network observer | Sees encrypted traffic volumes/timing | Cover traffic, Tor, mixnet |
| Hub database breach | Reads all stored data | Ring signatures, Pedersen commitments, encrypted balances |
| Malicious hub operator | Full access to running system | ZK proofs (verifiable), DID (portable), Tor (observable) |
| Global adversary (nation-state) | Monitors all network entry/exit | Mixnet delays, I2P garlic routing, steganographic encoding |
| Colluding agents | Coordinate to de-anonymize targets | Ring signature minimum size, cover traffic |

### What This Does NOT Protect Against

1. **Hub operator during execution**: The hub must debit the correct account. At the moment of payment execution, the hub knows the sender and amount. Ring signatures protect the ledger *after* execution, not *during*.
2. **Side-channel attacks**: Timing of database writes, memory access patterns, CPU cache behavior. Mitigated by constant-time operations (not addressed in this spec, deferred to security audit).
3. **Quantum computing**: Groth16 (SNARK), Ed25519, SECP256K1, and BLS12-381 are all vulnerable to quantum attacks. Migration to lattice-based crypto (e.g., CRYSTALS-Dilithium for signatures, CRYSTALS-Kyber for key exchange) is a Phase 5+ concern. Timeline: 5-10 years before quantum threatens 256-bit ECC.

---

## Part 10: Testing Strategy

Each privacy component gets three test categories:

### Correctness Tests
- ZK proofs: Generate proof, verify, tamper with witness, verify rejection
- Ring signatures: Sign, verify, wrong key should fail, key image linkability
- Pedersen commitments: Commit, open, binding (cannot open to different value)
- Cover traffic: Statistical test that cover/real messages are indistinguishable (KS test)

### Security Tests
- ZK soundness: Forge proof for false statement, must fail
- Ring signature unforgeability: Forge signature without private key, must fail
- Timing attacks: Measure proof generation/verification time variance (must be constant-time)
- Replay attacks: Replay old proofs/signatures, must fail (nonces, timestamps)

### Performance Tests
- ZK proof generation: Must complete in < 5 seconds for 256-element circuits
- Ring signature (size 5): Must complete in < 100ms
- Pedersen commitment: Must complete in < 10ms
- Cover traffic overhead: Must not exceed 20% of bandwidth
- Mixnet latency: Must stay within configured max_delay bounds

### Integration Tests
- Full flow: Register with DID -> prove capability -> win auction -> execute SLA -> prove fulfillment -> transfer reputation to new DID
- Tor end-to-end: Agent connects via .onion, sends payment, receives confirmation
- Concurrent ZK proofs: 100 simultaneous proof generation requests, all succeed

---

## Appendix A: Glossary

| Term | Definition |
|---|---|
| BBS+ | Boneh-Boyen-Shacham signature scheme supporting ZK selective disclosure |
| BFV | Brakerski-Fan-Vercauteren homomorphic encryption scheme |
| CKKS | Cheon-Kim-Kim-Song approximate homomorphic encryption scheme |
| DID | Decentralized Identifier (W3C standard) |
| Groth16 | Zero-knowledge proof system with small proofs (128 bytes) and fast verification |
| HKDF | HMAC-based Key Derivation Function (RFC 5869) |
| LSAG | Linkable Spontaneous Anonymous Group signature (CryptoNote) |
| NaCl | Networking and Cryptography library (libsodium) |
| PLONK | ZK proof system with universal setup (no circuit-specific ceremony) |
| Poseidon | ZK-friendly hash function (low R1CS constraint count) |
| PSI | Private Set Intersection |
| R1CS | Rank-1 Constraint System (arithmetic circuit representation) |
| SNARK | Succinct Non-interactive Argument of Knowledge |
| STARK | Scalable Transparent Argument of Knowledge (no trusted setup) |
| TAC | Transferable Anonymous Credentials |
| TSS | Threshold Signature Scheme |

## Appendix B: Library Evaluation Matrix

| Library | Language | Maturity | Python Support | Use Case | Selected |
|---|---|---|---|---|---|
| arkworks-rs | Rust | High (5.6k stars) | PyO3 bindings (unstable) | General SNARK | Deferred |
| circom + snarkjs | JS/Rust | High (1.2k stars) | Subprocess | Circuit definition + proving | Yes |
| py_ecc | Python | Medium (300 stars) | Native | BLS12-381 pairing | Yes |
| zksk | Python | Low (unmaintained) | Native | Sigma protocols | Keep existing |
| petlib | Python | Low (unmaintained) | Native | EC crypto | No |
| tenseal | C++/Python | High (800 stars) | Native | Homomorphic encryption | Deferred |
| stem | Python | High (official) | Native | Tor controller | Yes |
| MP-SPDZ | C++ | High (600 stars) | Bindings | General MPC | No (overkill) |
| Google PSI | C++ | High | Bindings | Private set intersection | Maybe |
