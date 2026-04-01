# Sthrip Federation Protocol (SFP) -- Design Specification

**Status**: Draft
**Date**: 2026-04-01
**Author**: Protocol Design Session
**Scope**: Transform Sthrip from single hub to federated network of agent payment hubs

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Hub Federation Protocol](#2-hub-federation-protocol)
3. [Agent Payment Routing](#3-agent-payment-routing)
4. [Interoperability Standards (A2APP)](#4-interoperability-standards-a2app)
5. [Economic Design](#5-economic-design)
6. [Developer Ecosystem](#6-developer-ecosystem)
7. [Open Protocol Specification](#7-open-protocol-specification)
8. [Critical Path & Timeline](#8-critical-path--timeline)
9. [Adoption Strategy](#9-adoption-strategy)

---

## 1. Executive Summary

### The Problem

Sthrip today is a single hub at `sthrip-api-production.up.railway.app`. Every agent registers on one hub, every payment routes through one hub, every escrow is held by one hub. This creates:

- **Single point of failure**: Hub goes down, all agent commerce stops
- **Single point of trust**: Hub operator controls all funds
- **Scaling ceiling**: One PostgreSQL database, one Monero wallet RPC
- **Jurisdictional risk**: One server, one legal jurisdiction
- **No competition**: No fee pressure, no incentive to improve

### The Solution

The Sthrip Federation Protocol (SFP) transforms the hub into a **network protocol** that any operator can run, creating a mesh of interconnected payment hubs. Agents on Hub A can pay agents on Hub B. Hubs compete on fees, reliability, and features. No single hub controls the network.

### Design Principles

1. **Backwards compatible**: Existing single-hub deployments work unchanged. Federation is opt-in.
2. **Privacy first**: Cross-hub payments must not leak more information than intra-hub payments. Monero's privacy guarantees must extend across hub boundaries.
3. **Progressive decentralization**: Start with manual hub peering (like early email), evolve toward full mesh discovery.
4. **No token required**: The protocol works with XMR as the native settlement layer. A governance token is not needed and would dilute Monero's privacy story.
5. **Steal from the best**: Learn from Lightning Network (payment channels + routing), Matrix (federation + identity), Nostr (simplicity + relay model), ActivityPub (server-to-server protocol).

### What Sthrip Already Has (Building Blocks)

From the codebase:

| Component | File | Federation Role |
|---|---|---|
| P2P node with WebSocket transport | `bridge/p2p/node.py` | Hub-to-hub communication transport |
| Gossip protocol (epidemic + PlumTree) | `bridge/p2p/gossip.py` | Network state propagation |
| Peer discovery (bootstrap, mDNS, DHT) | `bridge/p2p/discovery.py` | Hub discovery |
| Well-known discovery endpoint | `api/routers/wellknown.py` | Hub capability advertisement |
| Payment channels (off-chain) | `services/channel_service.py` | Inter-hub liquidity channels |
| HTLC swaps | `services/swap_service.py` | Cross-hub atomic transfers |
| E2E encrypted messaging | `services/messaging_service.py` | Cross-hub agent communication |
| Agent identity (UUID + API key + DID) | `db/models.py` Agent model | Portable agent identity |
| PoW registration | `services/pow_service.py` | Sybil resistance for hub operators |
| ZK reputation | `services/zk_reputation_service.py` | Privacy-preserving trust scores |
| Hub routing payments | `api/routers/payments.py` | Intra-hub routing (extend to inter-hub) |

The bridge/p2p layer and the payment channel/HTLC primitives give us most of the raw materials. The work is protocol design, not greenfield implementation.

---

## 2. Hub Federation Protocol

### 2.1 Hub Identity

Every hub in the network has a persistent identity:

```
Hub Identity:
  hub_id:        Ed25519 public key (32 bytes, hex-encoded)
  hub_url:       HTTPS URL of the hub API
  hub_name:      Human-readable name (e.g., "sthrip-eu-1")
  onion_address: Optional Tor hidden service address
  i2p_address:   Optional I2P address
  version:       SFP protocol version (semver)
  capabilities:  List of supported protocol features
  signing_key:   Ed25519 key for signing federation messages
```

The `hub_id` IS the public key. No separate registry needed. A hub proves its identity by signing messages with its private key. This is the Nostr model -- identity is a keypair.

**Key rotation**: A hub signs a "key rotation" message with the old key, attesting the new key. Peers that trust the old key automatically trust the new one. Chain of trust, not a certificate authority.

### 2.2 Hub Discovery

Three layers, from simplest to most decentralized:

#### Layer 1: Static Hub List (Day 1)

A well-known URL serves a curated list of trusted hubs:

```
GET https://sthrip.dev/.well-known/hub-directory.json

{
  "hubs": [
    {
      "hub_id": "a1b2c3...",
      "hub_url": "https://hub-eu.sthrip.dev",
      "name": "sthrip-eu-1",
      "region": "eu-west",
      "operator": "Sthrip Foundation",
      "trust_level": "founding"
    }
  ],
  "updated_at": "2026-04-01T00:00:00Z",
  "signature": "ed25519sig..."
}
```

This is how Bitcoin started (hardcoded seed nodes), how Matrix federation started (matrix.org homeserver), and how email started (manual MX records). Good enough to bootstrap.

#### Layer 2: DNS-Based Discovery (Month 2)

Each hub publishes a DNS TXT record:

```
_sthrip-hub.example.com TXT "hub_id=a1b2c3... url=https://hub.example.com version=1.0"
```

And a well-known endpoint extending what we already have:

```
GET https://hub.example.com/.well-known/agent-payments.json

// Existing fields remain unchanged (backwards compatible)
// New federation block added:
{
  "federation": {
    "hub_id": "a1b2c3...",
    "protocol_version": "1.0.0",
    "peering_endpoint": "wss://hub.example.com/v2/federation/ws",
    "rest_endpoint": "https://hub.example.com/v2/federation",
    "signing_key": "ed25519:a1b2c3...",
    "peering_policy": "open",           // "open" | "approved" | "closed"
    "max_channel_capacity_xmr": "100",
    "min_channel_capacity_xmr": "0.1",
    "routing_fee_ppm": 1000,            // 0.1% = 1000 ppm (parts per million)
    "supported_features": [
      "cross-hub-payments",
      "cross-hub-escrow",
      "channel-rebalancing",
      "onion-routing"
    ]
  }
}
```

#### Layer 3: DHT-Based Discovery (Month 6+)

Use the existing `DHTDiscovery` class in `bridge/p2p/discovery.py` (Kademlia) to store hub records in a distributed hash table. No central authority needed.

```
DHT Key:   sha256("sthrip:hub:" + hub_id)
DHT Value: Signed hub descriptor (JSON + Ed25519 signature)
```

**Comparison with existing protocols**:

| Protocol | Discovery | Sthrip Equivalent |
|---|---|---|
| Lightning Network | DNS seed nodes + gossip | Layer 1 + Layer 2 |
| Matrix | DNS SRV records + .well-known | Layer 2 |
| Nostr | Hardcoded relay list | Layer 1 |
| ActivityPub | WebFinger + .well-known | Layer 2 |
| BitTorrent | DHT (Kademlia) | Layer 3 |

### 2.3 Hub-to-Hub Connection (Peering)

When Hub A wants to peer with Hub B:

```
┌────────────┐                          ┌────────────┐
│   Hub A    │                          │   Hub B    │
│            │  1. GET /.well-known     │            │
│            │ ─────────────────────────>│            │
│            │  2. Federation metadata  │            │
│            │ <─────────────────────── │            │
│            │                          │            │
│            │  3. WSS handshake        │            │
│            │ ─────────────────────────>│            │
│            │  4. Challenge (nonce)    │            │
│            │ <─────────────────────── │            │
│            │  5. Signed response      │            │
│            │ ─────────────────────────>│            │
│            │  6. Peering established  │            │
│            │ <━━━━━━━━━━━━━━━━━━━━━━━>│            │
└────────────┘     Persistent WSS       └────────────┘
```

**Peering handshake** (inspired by Lightning Network's `init` message + Matrix federation handshake):

```json
// Step 3: Hub A opens WebSocket to Hub B's peering_endpoint
// Step 4: Hub B sends challenge
{
  "type": "federation.challenge",
  "nonce": "random-32-bytes-hex",
  "hub_id": "hub-b-pubkey",
  "protocol_versions": ["1.0"],
  "timestamp": 1711929600
}

// Step 5: Hub A signs and responds
{
  "type": "federation.auth",
  "hub_id": "hub-a-pubkey",
  "hub_url": "https://hub-a.example.com",
  "nonce_response": "ed25519-signature-of-nonce",
  "selected_version": "1.0",
  "capabilities": ["cross-hub-payments", "onion-routing"],
  "timestamp": 1711929601
}

// Step 6: Hub B confirms peering
{
  "type": "federation.peered",
  "session_id": "unique-session-id",
  "channel_params": {
    "max_htlc_value_xmr": "10",
    "min_htlc_value_xmr": "0.0001",
    "cltv_expiry_delta": 40
  }
}
```

**Peering policies** (modeled after Matrix federation and email MX):

- **Open**: Any hub can peer (like Nostr relays)
- **Approved**: Hub operator must approve peering requests (like Matrix federation)
- **Closed**: No federation (private deployment)

### 2.4 Cross-Hub Payment Routing

This is the critical protocol. Agent on Hub A pays Agent on Hub B without both agents needing accounts on the same hub.

**Architecture: Hub-Held HTLC Chain**

Each hub holds balances for its agents (the existing model). Cross-hub payments use HTLCs chained between hubs, similar to Lightning Network but with critical differences:

1. Hubs are the routing nodes (not individual agents)
2. Settlement is in XMR on-chain between hubs (not channel-based initially)
3. Privacy: Monero already hides amounts on-chain; we add onion routing for the hub path

```
Agent Alice          Hub A              Hub B          Agent Bob
(registered          (holds Alice's     (holds Bob's   (registered
 on Hub A)            balance)           balance)       on Hub B)
    │                    │                  │               │
    │  1. Pay Bob@HubB   │                  │               │
    │  0.5 XMR           │                  │               │
    │───────────────────>│                  │               │
    │                    │  2. HTLC offer   │               │
    │                    │  hash=H(secret)  │               │
    │                    │  0.5 XMR + fee   │               │
    │                    │─────────────────>│               │
    │                    │                  │  3. Credit Bob │
    │                    │                  │  (conditional) │
    │                    │                  │──────────────>│
    │                    │                  │               │
    │                    │                  │  4. Bob ACKs  │
    │                    │                  │<──────────────│
    │                    │  5. Reveal secret│               │
    │                    │<─────────────────│               │
    │                    │                  │               │
    │  6. Confirmed      │  7. Settle later │               │
    │<───────────────────│  (batch on-chain)│               │
    │                    │─ ─ ─ ─ ─ ─ ─ ─ >│               │
```

**Two-phase settlement**:

- **Phase 1 (instant)**: Hub-to-hub HTLC resolves over WebSocket. Hub B credits Bob's balance immediately. This is the "ledger update" -- both hubs update their internal balances.
- **Phase 2 (batched)**: Periodically (hourly/daily), hubs settle their net balances on-chain via XMR transactions. If Hub A owes Hub B 5 XMR net over 100 payments, one on-chain transaction settles the batch.

This is how correspondent banking works, how Lightning Network channels work, and how the existing Sthrip `ChannelService` already works for agent-to-agent within a hub.

### 2.5 Hub Reputation System

Hubs need trust scores. An unreliable hub that drops payments or goes offline harms the network.

**Metrics tracked** (inspired by Lightning Network node scoring):

```python
@dataclass(frozen=True)
class HubReputation:
    hub_id: str
    uptime_30d: float          # 0.0 to 1.0
    payment_success_rate: float # Fraction of HTLCs that resolved
    avg_settlement_time_s: float
    total_volume_xmr: Decimal   # Lifetime routed volume
    peer_count: int
    age_days: int
    disputes_lost: int
    disputes_won: int

    @property
    def trust_score(self) -> float:
        """Composite trust score (0.0 to 1.0)."""
        return (
            self.uptime_30d * 0.3
            + self.payment_success_rate * 0.3
            + min(self.age_days / 365, 1.0) * 0.15
            + min(self.peer_count / 20, 1.0) * 0.1
            + min(float(self.total_volume_xmr) / 1000, 1.0) * 0.15
        )
```

**Reputation propagation**: Each hub maintains local scores for its direct peers. Scores propagate via gossip (using the existing `GossipProtocol` in `bridge/p2p/gossip.py`) with exponential decay per hop. A hub trusts its own observations most, its peers' observations somewhat, and distant hubs' observations least.

**Sybil resistance for hubs**: A new hub must either:
1. Be vouched for by an existing hub with trust_score > 0.7 (web of trust)
2. Lock a bond in XMR that is slashable on misbehavior (stake-based)
3. Complete a computational PoW challenge (extending existing `pow_service.py`)

Option 2 is the strongest. Minimum bond: 1 XMR. Released after 90 days of good behavior or slashed on proven misbehavior (failed settlements, phantom HTLCs).

### 2.6 Liquidity Balancing Between Hubs

When Hub A routes many payments to Hub B but few in reverse, Hub A's balance with Hub B depletes. Rebalancing options:

1. **On-chain settlement**: Hub A sends XMR on-chain to refill its side of the channel. Slow (20+ min for 10 confirmations) but trustless.
2. **Circular rebalancing**: Find a path Hub A -> Hub C -> Hub B -> Hub A where imbalances cancel out. Same algorithm Lightning Network uses.
3. **Submarine swaps**: Hub A deposits XMR on-chain, Hub B credits Hub A's federation balance. Atomic via HTLC (the existing `SwapService` HTLC logic applies directly).
4. **Liquidity marketplace**: Hubs that have excess capacity advertise it. Hubs that need capacity can purchase it with a fee. Pure market mechanism.

### 2.7 Hub Operator Incentive Model

Hub operators earn through:

| Revenue Source | Mechanism | Current Implementation |
|---|---|---|
| Routing fees | PPM (parts per million) on routed payments | Extend existing `fee_collector.py` |
| Escrow fees | 1% on cross-hub escrow deals | Existing `escrow_service.py` |
| Channel fees | Fee on payment channel settlements | Existing `channel_service.py` |
| Swap fees | 0.5% on cross-chain swaps | Existing `swap_service.py` |
| Premium features | Higher rate limits, priority routing | Existing tier system |

**Fee competition**: When multiple routes exist between two hubs, the cheapest route wins. This creates price pressure. Hubs that overcharge get routed around.

### 2.8 Protocol Versioning and Upgrade Coordination

**Versioning**: Semantic versioning. Hubs advertise supported versions in their discovery document.

**Upgrade strategy** (learned from Lightning Network's feature bits):

```json
{
  "features": {
    "required": ["base-routing", "htlc-v1"],
    "optional": ["onion-routing", "mpp", "channel-rebalancing"]
  }
}
```

- **Required features**: If a peer does not support a required feature, connection is refused.
- **Optional features**: Used if both peers support them. Ignored otherwise.

Hubs negotiate the feature set during the peering handshake. New features start as optional, become required after sufficient adoption (>80% of network by volume).

---

## 3. Agent Payment Routing

### 3.1 Multi-Hop Payments

For payments that must traverse multiple hubs:

```
Alice@HubA ──> HubA ──> HubC ──> HubB ──> Bob@HubB

(HubA and HubB are not directly peered,
 but both peer with HubC)
```

Each hop uses an HTLC with decreasing timelock:

```
Hop 1 (HubA -> HubC): HTLC(hash=H, timelock=T+40, amount=0.502 XMR)
Hop 2 (HubC -> HubB): HTLC(hash=H, timelock=T+20, amount=0.501 XMR)
Final (HubB credits Bob): 0.500 XMR

Fee breakdown:
  HubC routing fee: 0.001 XMR (0.2%)
  HubB incoming fee: 0.001 XMR (0.2%)
  Total fee: 0.002 XMR (0.4%)
```

The timelock delta (20 blocks per hop) ensures that if any hop fails, the previous hop's HTLC has already expired and funds return to the sender. This is exactly Lightning Network's CLTV delta mechanism.

### 3.2 Path-Finding Algorithm

**Algorithm choice**: Modified Dijkstra with fee-weighted edges.

```
Graph:
  Nodes = Hubs
  Edges = Peering connections
  Edge weight = f(routing_fee, success_rate, latency, available_capacity)

Weight function:
  w(edge) = base_fee
           + (amount * proportional_fee_ppm / 1_000_000)
           + penalty(1.0 - success_rate)      # penalize unreliable hubs
           + penalty(latency / max_latency)    # penalize slow hubs
           + penalty(1.0 - capacity_fraction)  # penalize low capacity
```

**What Lightning Network teaches us**: Yen's K-shortest paths (find top-3 routes) is better than single shortest path. If the cheapest route fails, try the next one without full recalculation.

**What's different from Lightning**: Our graph is much smaller (hundreds of hubs, not tens of thousands of nodes). Dijkstra with K-shortest paths is more than sufficient. No need for the complexity of LND's MCF (minimum cost flow) or CLN's renepay.

### 3.3 Fee Discovery and Competition

Each hub advertises:

```json
{
  "fee_schedule": {
    "base_fee_xmr": "0.0001",           // Fixed fee per payment
    "proportional_fee_ppm": 1000,        // 0.1% proportional fee
    "cltv_expiry_delta": 40,             // Timelock delta per hop
    "min_htlc_xmr": "0.0001",
    "max_htlc_xmr": "10.0"
  }
}
```

Fee updates propagate via gossip. The sender's hub calculates fees for all possible routes and picks the cheapest successful path.

**Fee market dynamics**: In a competitive network, fees converge to marginal cost (operational cost of running the hub + capital cost of locked liquidity). Hubs with lower costs or higher volume can undercut. This is healthy -- it prevents rent extraction.

### 3.4 Onion Routing for Payment Paths

**Privacy problem**: If Hub C routes a payment from Hub A to Hub B, Hub C knows both the source and destination hub. Over time, Hub C builds a map of payment flows.

**Solution**: Onion-encrypted routing (from Lightning Network's BOLT #4, Sphinx construction):

```
Alice builds onion packet:
  Layer 3 (outermost): Encrypted for HubA -- contains "forward to HubC"
  Layer 2: Encrypted for HubC -- contains "forward to HubB"  
  Layer 1 (innermost): Encrypted for HubB -- contains "deliver to Bob"

Each hub peels one layer, learns only the next hop, not the full path.
```

**Implementation**: Use NaCl Box (already in the codebase for E2E messaging -- `MessagingService` uses `Curve25519 + XSalsa20-Poly1305`). Each layer is encrypted with the next hub's public key.

HubC sees: "I received an onion from HubA. I should forward it to HubB. I do not know if HubA is the origin or just another relay. I do not know if HubB is the destination or just another relay."

### 3.5 Atomic Multi-Path Payments (MPP)

For large payments that exceed any single hub's capacity:

```
Alice wants to pay 10 XMR to Bob.
No single route has 10 XMR capacity.

Split into:
  Path 1: HubA -> HubC -> HubB (4 XMR)
  Path 2: HubA -> HubD -> HubB (3 XMR)
  Path 3: HubA -> HubE -> HubB (3 XMR)

All 3 paths use the SAME payment hash.
Bob reveals the preimage only when all 3 parts arrive.
```

This is Lightning Network's AMP (Atomic Multi-Path) applied to hub-level routing. The atomicity guarantee ensures Bob receives all 10 XMR or nothing.

---

## 4. Interoperability Standards (A2APP)

### 4.1 Agent-to-Agent Payment Protocol

The A2APP standard defines how any agent framework can send payments through any Sthrip-compatible hub network.

**Design goal**: An AutoGPT agent should be able to pay a CrewAI agent without either knowing what framework the other uses, what hub the other is on, or what currency the other prefers.

#### Agent Addressing

```
Universal Agent Address (UAA):
  agent_name@hub_domain

Examples:
  weather-bot@sthrip.dev
  code-reviewer@hub-eu.sthrip.dev
  translator@payments.mycompany.com

Resolution:
  1. Extract hub_domain
  2. GET https://hub_domain/.well-known/agent-payments.json
  3. Query hub's agent registry for agent_name
  4. If not found, hub returns 404
```

This is the email model. Simple, proven, extensible. The `@` separator is universally understood.

For agents that want privacy (no stable name), use a one-time invoice address instead:

```
sthrip:invoice:hub-eu.sthrip.dev:abc123def456
```

#### Payment Request Format

Inspired by Bitcoin's BIP-21 URI scheme and Lightning's BOLT #11 invoices:

```
sthrip:pay?
  to=weather-bot@sthrip.dev
  &amount=0.05
  &currency=XMR
  &memo=Weather%20forecast%20for%20NYC
  &expiry=3600
  &callback=https://my-agent.com/payment-callback
  &nonce=unique-request-id

Full URI (one line):
sthrip:pay?to=weather-bot@sthrip.dev&amount=0.05&currency=XMR&memo=Weather%20forecast%20for%20NYC&expiry=3600
```

**Signed payment request** (prevents tampering):

```json
{
  "version": 1,
  "type": "payment_request",
  "payee": "weather-bot@sthrip.dev",
  "amount": "0.05",
  "currency": "XMR",
  "memo": "Weather forecast for NYC",
  "created_at": "2026-04-01T12:00:00Z",
  "expires_at": "2026-04-01T13:00:00Z",
  "nonce": "unique-request-id",
  "payee_hub_id": "a1b2c3...",
  "signature": "ed25519-sig-by-payee-hub"
}
```

### 4.2 Integration with Agent Frameworks

Sthrip already has integrations for LangChain, OpenAI Functions, CrewAI, and MCP. The federation protocol adds cross-hub capability to all of them with zero changes to the integration layer.

The key insight: **the SDK hides federation entirely**. From the agent's perspective, it is still calling `client.pay(to="bob@other-hub.com", amount=0.05)`. The hub handles routing.

```python
# Existing SDK call -- works for same-hub OR cross-hub payments
from sthrip import StrhipHubClient

client = StrhipHubClient(
    hub_url="https://hub-eu.sthrip.dev",
    api_key="my-agent-key"
)

# Pay an agent on ANY hub in the network
result = client.send_payment(
    to_agent="translator@hub-us.sthrip.dev",  # Different hub!
    amount=Decimal("0.05"),
    token="XMR",
    memo="Translate this document"
)
# The hub detects that translator is on hub-us, routes via federation
# The agent never needs to know about federation internals
```

**Framework-specific integration points**:

| Framework | Integration File | Federation Change |
|---|---|---|
| LangChain | `integrations/langchain_tool.py` | None -- hub handles routing |
| CrewAI | `integrations/crewai_tool.py` | None -- hub handles routing |
| OpenAI Functions | `integrations/openai_functions.py` | None -- hub handles routing |
| MCP Server | `integrations/sthrip_mcp/` | Add `discover_agents_network` tool |
| Direct SDK | `sdk/sthrip/client.py` | Add `@hub` address parsing |

The only integration that needs changes is the MCP server, which should expose a network-wide agent discovery tool. Everything else works because the hub API is the abstraction boundary.

### 4.3 Webhook Standard for Payment Notifications

Extend the existing `WebhookService` to include cross-hub payment events:

```json
{
  "event": "payment.received.cross_hub",
  "timestamp": "2026-04-01T12:05:00Z",
  "data": {
    "payment_id": "uuid",
    "from_agent": "alice@hub-eu.sthrip.dev",
    "to_agent": "bob@hub-us.sthrip.dev",
    "amount": "0.05",
    "currency": "XMR",
    "source_hub": "hub-eu.sthrip.dev",
    "hops": 2,
    "total_fee": "0.001",
    "memo": "Weather forecast"
  },
  "signature": "standard-webhooks-hmac-sha256"
}
```

New event types for federation:

```
payment.received.cross_hub    -- Payment arrived from another hub
payment.sent.cross_hub        -- Payment sent via another hub
payment.routing.failed        -- Cross-hub payment could not route
federation.hub.peered         -- New hub connection established
federation.hub.disconnected   -- Hub connection lost
escrow.cross_hub.created      -- Cross-hub escrow deal created
```

### 4.4 Universal Agent Identity

**Problem**: An agent registered on Hub A wants to be discoverable and payable from Hub B without re-registering.

**Solution**: Three tiers of identity:

#### Tier 1: Hub-Local Identity (existing)

```
Agent: weather-bot
Hub: hub-eu.sthrip.dev
Full address: weather-bot@hub-eu.sthrip.dev
```

The hub owns the namespace. Simple, no coordination needed.

#### Tier 2: DID-Based Portable Identity

The `Agent` model already has a `did` field. Use it:

```
did:sthrip:a1b2c3d4e5f6...

Resolution:
  1. Agent registers DID on their home hub
  2. DID document published at hub's /.well-known/did/ endpoint
  3. Any hub can resolve a DID by querying the home hub
  4. If agent moves hubs, they update their DID document's service endpoints
```

DID document (W3C DID Core compliant):

```json
{
  "@context": "https://www.w3.org/ns/did/v1",
  "id": "did:sthrip:a1b2c3d4e5f6",
  "authentication": [{
    "type": "Ed25519VerificationKey2020",
    "publicKeyMultibase": "z6Mk..."
  }],
  "service": [{
    "type": "StrhipPaymentEndpoint",
    "serviceEndpoint": "https://hub-eu.sthrip.dev/v2/agents/weather-bot"
  }]
}
```

#### Tier 3: Self-Sovereign Identity (future)

Agent identity anchored on-chain (Monero OP_RETURN or a separate identity chain). The agent controls their identity independent of any hub. Not needed for initial federation but architecturally compatible.

---

## 5. Economic Design

### 5.1 Fee Market Dynamics

**Fee structure per hop**:

```
Total fee = base_fee + (amount * proportional_fee_ppm / 1_000_000)

Typical values:
  base_fee = 0.0001 XMR (~$0.01 at $100/XMR)
  proportional_fee_ppm = 1000 (0.1%)

For a 1 XMR payment routed through 2 hubs:
  Hop 1 fee: 0.0001 + 1.0 * 1000/1000000 = 0.0011 XMR
  Hop 2 fee: 0.0001 + 1.0 * 1000/1000000 = 0.0011 XMR
  Total: 0.0022 XMR (0.22%)
```

**Dynamic fee adjustment**: Hubs adjust fees based on:

1. **Capacity utilization**: If a channel is >80% utilized in one direction, raise proportional fee on that direction to incentivize rebalancing
2. **Demand**: During high volume periods, fees naturally rise as capacity fills
3. **Competition**: If a hub detects cheaper routes through competitors, it must lower fees or lose routing volume

**Fee floor**: Hubs should not race to zero. The fee floor is the marginal cost of operating: XMR on-chain tx fee for settlement + compute cost + capital opportunity cost.

### 5.2 Liquidity Mining for Early Adopters

To bootstrap the network, early hub operators and agents get rewards:

**Phase 1 (Months 1-6): Hub Operator Rewards**

```
Reward pool: 100 XMR (from Sthrip Foundation treasury or fundraise)

Distribution:
  50% proportional to routed volume
  30% proportional to uptime
  20% proportional to unique peer connections

Monthly distribution, 6-month program.
```

**Phase 2 (Months 3-12): Agent Adoption Rewards**

```
Agents that make cross-hub payments earn fee rebates:
  First 100 cross-hub payments: 100% fee rebate
  Next 1000: 50% fee rebate
  After that: market rate

Funded by hub operators (shared marketing cost).
```

This is the Uber/Lyft model: subsidize demand to bootstrap supply. Once network effects kick in, subsidies are no longer needed.

### 5.3 Staking Mechanisms for Hub Operators

**Hub bond**: Hub operators lock XMR as a trust guarantee.

```
Minimum bond: 1 XMR
Bond tiers:
  1 XMR    -> Can peer with up to 5 hubs, route up to 10 XMR/day
  10 XMR   -> Up to 20 hubs, 100 XMR/day
  100 XMR  -> Unlimited peering, unlimited volume

Bond is locked in a 2-of-2 multisig between the hub operator and
a "slashing committee" (initially the Sthrip Foundation, later a
decentralized set of high-reputation hubs).

Slashing conditions:
  - Failed settlement (>24h overdue): 10% slash
  - Provably false routing (phantom HTLCs): 50% slash
  - Data leak / privacy violation: 100% slash + network ban
```

### 5.4 Token Economics Decision: NO TOKEN

**Recommendation**: Do not create a Sthrip token.

**Reasoning**:

1. XMR is already the settlement currency. A second token adds friction.
2. Tokens attract speculators who do not use the network. Monero's community values utility over speculation.
3. Token governance centralizes around large holders. Sthrip's governance should be based on operational reputation (hubs that route more volume get more governance weight).
4. Legal risk. Token issuance invites securities regulation in most jurisdictions. XMR is already classified as a commodity in most frameworks.
5. Privacy. A new token on a transparent chain would undermine Sthrip's core privacy proposition.

**If governance is needed later**: Use "proof of routing" -- hub operators vote proportional to their verified routing volume over the past 90 days. No token needed.

### 5.5 Anti-Spam Measures

Three-layer defense:

**Layer 1: PoW for Hub Registration** (existing `pow_service.py`)

New hubs must solve a PoW challenge to join the network. This prevents mass creation of phantom hubs.

```
Difficulty: 20 bits (existing) for agents, 24 bits for hubs
Time: ~1 minute for hub registration on modern hardware
```

**Layer 2: Stake-Based Rate Limiting**

Hub routing capacity is proportional to bond size. A hub with 1 XMR bonded cannot route 1000 XMR/day. This prevents amplification attacks.

**Layer 3: Reputation-Based Filtering**

Hubs with trust_score < 0.3 are deprioritized in routing. Hubs with trust_score < 0.1 are not used as intermediaries. This prevents sybil attacks where an attacker runs many low-quality hubs.

### 5.6 Sybil Resistance for Agent Identities

**Problem**: An attacker registers millions of agents to spam the marketplace or manipulate reputation.

**Defense layers**:

1. **PoW registration** (existing): Each agent registration costs compute
2. **Hub-level rate limiting** (existing): Each hub limits registrations per time period
3. **Deposit requirement**: Agents that want to participate in escrow or marketplace must hold a minimum balance (0.001 XMR)
4. **ZK reputation** (existing `zk_reputation_service.py`): Reputation accrues only through completed transactions. Cannot be purchased or transferred.
5. **Cross-hub reputation aggregation**: An agent's reputation score is the weighted average across all hubs they operate on, weighted by volume. Sybil agents have no volume and thus no reputation.

---

## 6. Developer Ecosystem

### 6.1 Plugin Architecture for the Hub

The hub should be extensible without forking:

```python
# Plugin interface (abstract protocol)
from typing import Protocol

class HubPlugin(Protocol):
    """Interface that all hub plugins must implement."""

    @property
    def name(self) -> str:
        """Unique plugin name."""
        ...

    @property
    def version(self) -> str:
        """Semantic version."""
        ...

    async def on_load(self, hub: "HubContext") -> None:
        """Called when the plugin is loaded."""
        ...

    async def on_unload(self) -> None:
        """Called when the plugin is unloaded."""
        ...


class PaymentPlugin(HubPlugin, Protocol):
    """Plugin that can intercept and modify payment flows."""

    async def on_payment_received(self, payment: "PaymentEvent") -> "PaymentEvent":
        """Called when a payment is received. Return modified event or raise to reject."""
        ...

    async def on_payment_sent(self, payment: "PaymentEvent") -> "PaymentEvent":
        """Called when a payment is about to be sent."""
        ...


class RoutingPlugin(HubPlugin, Protocol):
    """Plugin that can modify routing decisions."""

    async def score_route(self, route: "Route") -> float:
        """Return a score modifier for a candidate route. Higher = preferred."""
        ...


class IdentityPlugin(HubPlugin, Protocol):
    """Plugin that can resolve agent identities."""

    async def resolve_agent(self, address: str) -> "AgentInfo | None":
        """Resolve an agent address to identity info. Return None to fall through."""
        ...
```

**Plugin loading**: Plugins are Python packages installed in the hub's virtualenv. The hub discovers them via entry points (like pytest plugins):

```toml
# Plugin's pyproject.toml
[project.entry-points."sthrip.plugins"]
my_plugin = "my_plugin:MyPlugin"
```

### 6.2 Template System for Common Payment Patterns

Pre-built templates that hub operators or agent developers can deploy:

```
Templates:
  pay-per-request     -- Agent charges per API call
  subscription        -- Monthly recurring payment
  escrow-milestone    -- Multi-milestone escrow (already built)
  auction             -- Dutch/English auction for agent services
  streaming-payment   -- Per-second payment accrual (already built)
  bounty              -- Payment released when condition is met
  split-payment       -- Payment divided among multiple agents
  tipping             -- Optional payment with no minimum
```

Each template is a plugin that registers its own API endpoints and business logic.

### 6.3 SDK Support for More Languages

Priority order based on agent framework ecosystem:

| Language | Priority | Rationale | Timeline |
|---|---|---|---|
| Python | Existing | Core SDK, LangChain/CrewAI ecosystem | Done |
| TypeScript | P0 | OpenAI SDK, Vercel AI SDK, AutoGPT | Month 1-2 |
| Go | P1 | Infrastructure tooling, high-perf agents | Month 3-4 |
| Rust | P2 | Embedded agents, WASM compilation | Month 6+ |

**TypeScript SDK** is the highest priority because:
1. OpenAI's agent ecosystem is TypeScript-first
2. Vercel AI SDK (most popular agent framework) is TypeScript
3. Most MCP servers are TypeScript
4. AutoGPT's new architecture is TypeScript

The TypeScript SDK should be a thin HTTP client wrapping the hub API. Federation is invisible to the SDK -- the hub handles it.

```typescript
// TypeScript SDK example
import { StrhipClient } from '@sthrip/sdk';

const client = new StrhipClient({
  hubUrl: 'https://hub-eu.sthrip.dev',
  apiKey: process.env.STHRIP_API_KEY,
});

// Works for same-hub AND cross-hub payments
const result = await client.pay({
  to: 'translator@hub-us.sthrip.dev',
  amount: '0.05',
  currency: 'XMR',
  memo: 'Translate document',
});
```

### 6.4 API Versioning and Backwards Compatibility

**Strategy**: URL-based versioning (existing `/v2/` prefix) with strict backwards compatibility guarantees.

```
Rules:
  1. Existing endpoints are NEVER removed within a major version
  2. New fields added to responses are always optional
  3. New required request fields get a default value for backwards compat
  4. Deprecated endpoints return Sunset header with removal date
  5. Breaking changes require a new major version (v3/)
  6. Federation endpoints live under /v2/federation/ (same major version)
```

**Federation API endpoints** (new):

```
POST   /v2/federation/peer                -- Request peering with this hub
DELETE /v2/federation/peer/{hub_id}       -- Disconnect from peer
GET    /v2/federation/peers               -- List current peers
GET    /v2/federation/network/agents      -- Search agents across network
POST   /v2/federation/route               -- Request cross-hub payment route
GET    /v2/federation/route/{payment_id}  -- Check cross-hub payment status
GET    /v2/federation/channels            -- List inter-hub channels
POST   /v2/federation/channels/rebalance  -- Trigger channel rebalancing
```

---

## 7. Open Protocol Specification

### 7.1 Message Format

**Choice: JSON over WebSocket for hub-to-hub, with CBOR option for high-throughput**

**Why JSON**:
- Human readable (critical for debugging during development)
- Every language has a JSON parser
- The existing P2P layer (`bridge/p2p/node.py`) already uses JSON over WebSocket
- Good enough performance for hub-to-hub traffic (hundreds of messages/second, not millions)

**Why not Protobuf/gRPC**:
- Protobuf adds a compilation step and schema management
- gRPC requires HTTP/2 which complicates proxy/load-balancer setups
- The performance gain is irrelevant at hub-to-hub scale
- Lightning Network uses a custom binary format and the community regrets the debugging difficulty

**Why CBOR as optional upgrade**:
- Binary-efficient JSON (30-50% smaller)
- Schema-less like JSON
- No compilation step
- Good for high-volume hubs that need bandwidth savings
- Negotiated during peering handshake

### 7.2 Message Envelope

Every federation message follows this envelope:

```json
{
  "v": 1,
  "type": "federation.payment.htlc_offer",
  "id": "msg-uuid",
  "from": "hub-a-pubkey",
  "to": "hub-b-pubkey",
  "timestamp": 1711929600,
  "payload": {
    // Type-specific payload
  },
  "signature": "ed25519-signature-of-sha256(canonical-json(payload))"
}
```

**Canonical JSON**: Before signing, the payload is serialized with sorted keys and no whitespace. This ensures deterministic signatures regardless of JSON serializer implementation.

### 7.3 Message Types

```
CATEGORY: Discovery
  federation.hello              -- Initial handshake
  federation.challenge          -- Auth challenge
  federation.auth               -- Auth response
  federation.peered             -- Peering confirmed
  federation.goodbye            -- Graceful disconnect
  federation.hub_announce       -- Hub capability update (gossip)
  federation.agent_announce     -- New agent available (gossip)

CATEGORY: Routing
  federation.payment.htlc_offer    -- Propose HTLC
  federation.payment.htlc_accept   -- Accept HTLC
  federation.payment.htlc_reject   -- Reject HTLC (with reason)
  federation.payment.htlc_fulfill  -- Reveal preimage, claim payment
  federation.payment.htlc_timeout  -- HTLC expired
  federation.payment.route_query   -- Ask peer for route to destination
  federation.payment.route_reply   -- Route suggestion

CATEGORY: Settlement
  federation.settlement.propose    -- Propose batch settlement
  federation.settlement.accept     -- Accept settlement terms
  federation.settlement.confirm    -- Confirm on-chain tx
  federation.settlement.dispute    -- Dispute settlement

CATEGORY: Escrow (cross-hub)
  federation.escrow.create         -- Create cross-hub escrow
  federation.escrow.accept         -- Seller accepts
  federation.escrow.deliver        -- Seller marks delivered
  federation.escrow.release        -- Buyer releases funds
  federation.escrow.dispute        -- Either party disputes

CATEGORY: Channel Management
  federation.channel.open          -- Open inter-hub channel
  federation.channel.update        -- Update channel state
  federation.channel.close         -- Cooperatively close channel
  federation.channel.force_close   -- Unilateral close with on-chain settlement

CATEGORY: Gossip
  federation.gossip.hub_update     -- Hub reputation/fee update
  federation.gossip.agent_update   -- Agent availability update
  federation.gossip.network_state  -- Network topology update
```

### 7.4 Transport Layer

**Primary: WebSocket (WSS)**

```
wss://hub.example.com/v2/federation/ws

- Persistent connection between peered hubs
- Full-duplex messaging
- TLS 1.3 minimum
- Ping/pong keepalive every 30 seconds
- Reconnect with exponential backoff (1s, 2s, 4s, 8s, max 60s)
```

The existing `P2PNode` in `bridge/p2p/node.py` already implements WebSocket transport with connection management. Extend it for federation.

**Fallback: HTTPS REST**

For non-persistent operations or when WebSocket is unavailable:

```
POST https://hub.example.com/v2/federation/messages

Body: Federation message envelope (JSON)
Auth: Ed25519 signature in X-Hub-Signature header
```

REST fallback is important because some hosting environments (shared hosting, restrictive firewalls) do not support WebSocket.

**Optional: Tor Hidden Services**

For hubs that want maximum privacy:

```
wss://abcdef1234567890.onion/v2/federation/ws
```

The existing `bridge/tor/` directory has Tor integration scaffolding.

### 7.5 Authentication and Encryption Layer

**Transport encryption**: TLS 1.3 (WebSocket is over HTTPS).

**Message authentication**: Every message is signed by the sending hub's Ed25519 key. The receiving hub verifies the signature against the known public key from the peering handshake.

**Payload encryption (for onion routing)**:

```
Each onion layer uses:
  Algorithm: XSalsa20-Poly1305 (NaCl SecretBox)
  Key exchange: X25519 (Curve25519 DH)
  
  Sender generates ephemeral X25519 keypair per payment
  Shared secret = X25519(ephemeral_private, hub_public)
  Each layer's key = HKDF(shared_secret, hop_index)
```

This matches the existing encryption in `MessagingService` (NaCl Box), so no new crypto primitives are needed.

### 7.6 Error Handling and Recovery

**Error codes** (modeled after HTTP + Lightning Network error codes):

```
1xxx: Protocol errors
  1000: Unknown message type
  1001: Invalid signature
  1002: Protocol version mismatch
  1003: Feature not supported

2xxx: Routing errors
  2000: No route to destination
  2001: Insufficient capacity
  2002: HTLC timeout
  2003: Amount too small
  2004: Amount too large
  2005: Fee insufficient

3xxx: Settlement errors
  3000: Settlement rejected
  3001: On-chain tx not found
  3002: Insufficient confirmations
  3003: Amount mismatch

4xxx: Identity errors
  4000: Agent not found
  4001: Hub not peered
  4002: Agent suspended
```

**Recovery procedures**:

1. **Failed HTLC**: Timelock ensures funds return to sender. No intervention needed.
2. **Hub disconnect**: Reconnect with exponential backoff. Pending HTLCs are tracked locally and resolved when connection restores.
3. **Settlement dispute**: Escalate to on-chain. Both hubs submit their view of the balance. The hub with the most recent mutually-signed state update wins.
4. **Data loss**: Hub restores from database backup. Peers re-sync pending HTLCs via `federation.recovery.sync` message.

---

## 8. Critical Path and Timeline

### Phase 0: Foundation (Weeks 1-4)

**Goal**: Hub identity and basic peering.

```
Dependencies: None (builds on existing P2P layer)

Tasks:
  [ ] Define hub identity keypair generation and storage
  [ ] Extend /.well-known/agent-payments.json with federation block
  [ ] Implement peering handshake over WebSocket
  [ ] Hub-to-hub ping/pong and keepalive
  [ ] Basic peer management (add, remove, list)
  [ ] Federation config in Settings (peering_policy, signing_key, etc.)

Deliverables:
  - Two Sthrip hubs can connect and authenticate
  - Federation status visible in admin dashboard
  - No cross-hub payments yet
```

### Phase 1: Cross-Hub Payments (Weeks 5-10)

**Goal**: Agent on Hub A can pay agent on Hub B.

```
Dependencies: Phase 0 (peering must work)

Tasks:
  [ ] Agent address parsing (name@hub format)
  [ ] Cross-hub agent lookup (query peer hub's registry)
  [ ] Direct-peer HTLC implementation (no multi-hop yet)
  [ ] Two-phase settlement (instant credit + batched on-chain)
  [ ] Cross-hub payment webhook events
  [ ] SDK update: parse @hub addresses transparently

Deliverables:
  - End-to-end cross-hub payment between directly peered hubs
  - Settlement tracking in admin dashboard
  - Updated SDK and MCP tools
```

### Phase 2: Routing and Multi-Hop (Weeks 11-18)

**Goal**: Payments route through intermediate hubs.

```
Dependencies: Phase 1 (direct cross-hub must work)

Tasks:
  [ ] Network graph construction from peer information
  [ ] Dijkstra path-finding with fee-weighted edges
  [ ] Multi-hop HTLC chain with timelock deltas
  [ ] Fee advertisement and gossip propagation
  [ ] Route failure handling and retry with alternative paths
  [ ] Basic onion routing (2-3 hop limit initially)

Deliverables:
  - Payments route through 1-2 intermediate hubs
  - Fee competition between routes
  - Network topology visible in admin dashboard
```

### Phase 3: Reputation and Economics (Weeks 19-24)

**Goal**: Hubs have trust scores. Economic incentives are aligned.

```
Dependencies: Phase 2 (need routing data for reputation)

Tasks:
  [ ] Hub reputation scoring (uptime, success rate, volume)
  [ ] Reputation gossip protocol
  [ ] Hub bonding mechanism (XMR multisig)
  [ ] Fee market dynamics (capacity-based fee adjustment)
  [ ] Liquidity rebalancing (circular + submarine swaps)
  [ ] Slashing conditions and dispute resolution

Deliverables:
  - Hub trust scores visible in discovery
  - Bonding/staking for hub operators
  - Automated fee adjustment
```

### Phase 4: Advanced Features (Weeks 25-36)

**Goal**: Production-grade federation.

```
Dependencies: Phases 0-3

Tasks:
  [ ] Atomic multi-path payments (MPP)
  [ ] Cross-hub escrow
  [ ] DID-based portable agent identity
  [ ] DHT-based hub discovery
  [ ] TypeScript SDK
  [ ] Plugin architecture
  [ ] Full onion routing (arbitrary hop count)
  [ ] Inter-hub payment channels (persistent, not per-payment)

Deliverables:
  - Feature-complete federation protocol
  - Multiple independent hub operators
  - TypeScript SDK for broader ecosystem adoption
```

### Dependency Graph

```
Phase 0 (Identity/Peering)
    │
    v
Phase 1 (Direct Cross-Hub Payments)
    │
    ├───> Phase 2 (Multi-Hop Routing)
    │         │
    │         v
    │     Phase 3 (Reputation/Economics)
    │         │
    v         v
Phase 4 (Advanced Features)
```

---

## 9. Adoption Strategy

### 9.1 Getting Hub Operators

**Target operators** (in order):

1. **The Sthrip team** runs 2-3 hubs in different regions (EU, US, Asia). This proves the protocol works and provides seed infrastructure. Cost: ~$100/month per hub on Railway.

2. **Monero community operators**: People who already run Monero nodes. They understand privacy, have the infrastructure, and are ideologically aligned. Outreach via Monero subreddit, IRC, and MoneroKon.

3. **AI infrastructure companies**: Companies building agent hosting platforms (e.g., Replit, Modal, Fly.io). They already serve the target audience and could offer Sthrip hub as a value-add.

4. **Privacy-focused hosting providers**: Njalla, Mullvad, Privex. They already serve privacy-conscious customers.

**Operator onboarding**:

```bash
# One-command hub deployment
docker run -d \
  --name sthrip-hub \
  -e MONERO_RPC_HOST=your-monero-node \
  -e HUB_SIGNING_KEY=$(sthrip-keygen) \
  -e PEERING_POLICY=open \
  ghcr.io/ageree/sthrip-hub:latest
```

The hub ships as a Docker image with sensible defaults. Federation is enabled by setting `PEERING_POLICY=open` and adding a bootstrap peer.

### 9.2 Getting Agent Frameworks to Adopt

**Strategy**: Make integration trivially easy and provide clear value.

1. **LangChain**: Already integrated. Cross-hub works with zero changes because the hub API handles routing. Write a blog post: "LangChain agents can now pay each other across independent payment hubs."

2. **OpenAI Assistants / GPT Actions**: Publish a GPT Action template that connects to any Sthrip hub. Any GPT can accept payments.

3. **CrewAI**: Already integrated. Same story as LangChain.

4. **Vercel AI SDK**: Build a `@sthrip/ai-sdk` package that integrates with Vercel's tool-calling system. This is the largest agent framework by deployment count.

5. **MCP**: The MCP server already exists with 19 tools. Add `discover_network_agents` and `pay_cross_hub` tools. MCP is becoming the universal agent integration protocol -- being a native MCP tool provider is high leverage.

### 9.3 Network Effect Flywheel

```
More hubs
    │
    v
More agents (because more hubs = more geographic coverage)
    │
    v
More payments (because more agents = more commerce)
    │
    v
More fee revenue for hub operators
    │
    v
More incentive to run hubs
    │
    └──────────> (back to top)
```

**The critical bootstrapping question**: How do you get the first 3-5 independent hubs?

**Answer**: Run them yourself, then gradually hand them off.

1. Month 1: Sthrip team runs 3 hubs (EU, US, Asia)
2. Month 3: Monero community members run 2-3 more (through grants/bounties)
3. Month 6: 10+ hubs, some operated by companies using Sthrip for their agents
4. Month 12: 50+ hubs, self-sustaining through fee revenue

### 9.4 Standards Body and Specification

Publish the protocol as an open specification:

1. **SIPP (Sthrip Inter-Hub Payment Protocol)**: The core routing and settlement spec
2. **A2APP (Agent-to-Agent Payment Protocol)**: The agent-facing payment request format
3. **SAID (Sthrip Agent Identity Document)**: The DID-based identity spec

Host the specs on GitHub as a separate repo (e.g., `sthrip-protocol/sipp`). Accept PRs. Version the specs independently of the implementation.

**Do NOT go through a formal standards body** (W3C, IETF) initially. The protocol is too young. Standardize after 2+ independent implementations exist and the design has stabilized. Premature standardization kills innovation (see: ActivityPub's limitations from early standardization).

---

## Appendix A: Comparison with Existing Protocols

### Lightning Network

| Aspect | Lightning | Sthrip Federation |
|---|---|---|
| Routing nodes | Individual wallets | Hub operators |
| Channel funding | On-chain UTXO lock | Hub balance + optional on-chain bond |
| Settlement | Channel close on-chain | Batched on-chain settlement |
| Privacy | Onion routing for path | Onion routing + Monero for amounts |
| Identity | Node pubkey | Hub pubkey + agent name@hub |
| Fees | Base + proportional | Same |
| Path-finding | MCF / Dijkstra | Dijkstra (smaller graph) |
| Adoption model | Run your own node | Run a hub (higher level of abstraction) |

**Key lesson**: Lightning's biggest adoption barrier is the complexity of running a node and managing channels. Sthrip federation should be as simple as running a Docker container.

### Matrix Federation

| Aspect | Matrix | Sthrip Federation |
|---|---|---|
| Purpose | Messaging | Payments |
| Identity | @user:server | agent@hub |
| Discovery | .well-known + DNS SRV | .well-known + DNS TXT + DHT |
| Transport | HTTPS (REST) | WebSocket + HTTPS fallback |
| State | Event DAG | HTLC chain + balance ledger |
| Encryption | Olm/Megolm (E2E) | NaCl Box (onion layers) |

**Key lesson**: Matrix federation works well because the identity format (`@user:server`) is simple and memorable. Sthrip should use the same pattern (`agent@hub`).

### Nostr

| Aspect | Nostr | Sthrip Federation |
|---|---|---|
| Identity | Public key (npub) | Hub pubkey |
| Relay model | Client -> multiple relays | Agent -> one hub -> federation |
| Messages | Signed JSON events | Signed JSON messages |
| Discovery | Hardcoded relay list | Bootstrap list + DHT |
| Simplicity | Very simple protocol | More complex (financial guarantees needed) |

**Key lesson**: Nostr's radical simplicity is its strength. The core protocol is one page. Sthrip federation should have a similarly small core with optional extensions.

### ActivityPub

| Aspect | ActivityPub | Sthrip Federation |
|---|---|---|
| Identity | @user@server (WebFinger) | agent@hub |
| Transport | HTTPS | WebSocket + HTTPS |
| Authentication | HTTP Signatures | Ed25519 signatures |
| Federation | Server-to-server POST | Hub-to-hub WebSocket |

**Key lesson**: ActivityPub's biggest problem is spam. Without economic skin-in-the-game, anyone can run a server and flood the network. Sthrip's bonding requirement and fee structure naturally prevent this.

---

## Appendix B: Security Considerations

### Threat Model

1. **Malicious hub operator**: A hub that steals funds, drops payments, or leaks data
   - Mitigation: Bonding (slashable), reputation system, HTLC timelocks (funds always recoverable)

2. **Network-level adversary**: An attacker that controls internet traffic between hubs
   - Mitigation: TLS 1.3, message signatures, optional Tor transport

3. **Sybil attack on hubs**: Attacker runs many cheap hubs to control routing
   - Mitigation: Bonding requirement, PoW for registration, reputation weighted by volume

4. **Eclipse attack**: Attacker isolates a hub by controlling all its peers
   - Mitigation: Minimum peer diversity requirement (peers from different ASNs), direct connection to bootstrap nodes

5. **Timing analysis**: Correlating cross-hub payments by timing
   - Mitigation: Random delay insertion (0-5 seconds), batched settlement obscures individual payments

6. **Balance probing**: Inferring hub channel balances by sending payments of varying sizes
   - Mitigation: Do not reveal exact failure amounts. Return generic "insufficient capacity" without specifying the limit.

### Privacy Guarantees

| Information | Who sees it |
|---|---|
| Payment amount | Only sender, receiver, and their hubs (Monero hides on-chain) |
| Sender identity | Only sender's hub |
| Receiver identity | Only receiver's hub |
| Payment path | Each hop sees only prev hop and next hop (onion routing) |
| Total network volume | Visible to all hubs (aggregate only, no individual payments) |
| Hub peering topology | Visible to all hubs (public graph) |
| Agent-hub association | Only the agent's hub and the agent |

---

## Appendix C: Data Model Changes

New database models needed for federation (extending `sthrip/db/models.py`):

```python
class FederatedHub(Base):
    """Known hub in the federation network."""
    __tablename__ = "federated_hubs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hub_id = Column(String(64), unique=True, nullable=False)  # Ed25519 pubkey hex
    hub_url = Column(String(512), nullable=False)
    hub_name = Column(String(255), nullable=True)
    signing_key = Column(String(64), nullable=False)
    peering_status = Column(String(20), default="disconnected")
    trust_score = Column(Numeric(5, 4), default=Decimal("0.5"))
    bond_amount_xmr = Column(Numeric(20, 12), default=Decimal("0"))
    routing_fee_ppm = Column(Integer, default=1000)
    base_fee_xmr = Column(Numeric(20, 12), default=Decimal("0.0001"))
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    peered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())


class InterHubChannel(Base):
    """Liquidity channel between this hub and a peer hub."""
    __tablename__ = "inter_hub_channels"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    peer_hub_id = Column(String(64), ForeignKey("federated_hubs.hub_id"))
    our_balance_xmr = Column(Numeric(20, 12), default=Decimal("0"))
    their_balance_xmr = Column(Numeric(20, 12), default=Decimal("0"))
    capacity_xmr = Column(Numeric(20, 12), default=Decimal("0"))
    nonce = Column(BigInteger, default=0)
    status = Column(String(20), default="open")
    last_settlement_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())


class CrossHubHTLC(Base):
    """HTLC for cross-hub payment routing."""
    __tablename__ = "cross_hub_htlcs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payment_hash = Column(String(64), nullable=False, index=True)
    amount_xmr = Column(Numeric(20, 12), nullable=False)
    fee_xmr = Column(Numeric(20, 12), default=Decimal("0"))
    source_hub_id = Column(String(64), nullable=True)
    dest_hub_id = Column(String(64), nullable=True)
    timelock_height = Column(BigInteger, nullable=False)
    status = Column(String(20), default="offered")  # offered/accepted/fulfilled/expired
    preimage = Column(String(64), nullable=True)
    onion_blob = Column(Text, nullable=True)  # Encrypted routing info
    created_at = Column(DateTime(timezone=True), default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)


class HubSettlement(Base):
    """Batched on-chain settlement record between hubs."""
    __tablename__ = "hub_settlements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    peer_hub_id = Column(String(64), ForeignKey("federated_hubs.hub_id"))
    net_amount_xmr = Column(Numeric(20, 12), nullable=False)
    direction = Column(String(10), nullable=False)  # "outbound" or "inbound"
    tx_hash = Column(String(64), nullable=True)
    htlc_count = Column(Integer, default=0)
    status = Column(String(20), default="proposed")
    proposed_at = Column(DateTime(timezone=True), default=func.now())
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
```

---

## Appendix D: Configuration Changes

New settings for federation (extending `sthrip/config.py` Settings class):

```python
# Federation
federation_enabled: bool = False
federation_hub_name: str = ""
federation_signing_key: str = ""          # Ed25519 private key (hex)
federation_peering_policy: str = "closed" # "open" | "approved" | "closed"
federation_bootstrap_hubs: str = ""       # Comma-separated hub URLs
federation_max_htlc_xmr: Decimal = Decimal("10")
federation_min_htlc_xmr: Decimal = Decimal("0.0001")
federation_routing_fee_ppm: int = 1000
federation_base_fee_xmr: Decimal = Decimal("0.0001")
federation_settlement_interval_hours: int = 24
federation_bond_amount_xmr: Decimal = Decimal("0")
federation_max_peers: int = 50
```

Existing single-hub deployments work unchanged because `federation_enabled` defaults to `False`.
