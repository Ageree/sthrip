# Sthrip Phase 1-2: Design Specification

**Date**: 2026-03-31
**Status**: Draft
**Author**: AI-assisted design
**Priorities**: Working product, real anonymity, speed for AI agents

---

## 1. Fee Model Overhaul

### Current State
- Hub routing: 0.1% with tier discounts (premium 50% off, verified 25% off)
- Escrow: 0.1% with same discounts
- Cross-chain: 0.5%

### New Model
**Flat 1% commission on ALL transfers. No tier discounts. No exceptions.**

| Transfer Type | Fee | Collected When |
|---|---|---|
| Hub-held payment (`s.pay()`) | 1% of amount | Deducted from sender balance at transfer time |
| Hub-held escrow release | 1% of released amount | Deducted at release, before crediting seller |
| Multisig escrow | 1% of escrow amount | Collected upfront at escrow creation, before funds enter multisig |
| Milestone escrow (per milestone) | 1% of milestone amount | Deducted at each milestone release |
| Cross-chain (existing) | 1% (was 0.5%) | At bridge execution |
| Withdrawal to external wallet | 0% (only Monero network fee) | N/A |

Note: Monero on-chain network fees (~0.00001 XMR) are paid by the sender on top of the 1% hub fee. Withdrawals to external Monero addresses have no hub fee — only the network fee.

**Implementation**:
- File: `sthrip/services/fee_collector.py`
- Change `DEFAULT_FEES[HUB_ROUTING].percent` from `Decimal("0.001")` to `Decimal("0.01")`
- Change `DEFAULT_FEES[ESCROW].percent` from `Decimal("0.001")` to `Decimal("0.01")`
- Remove all tier discount logic from `calculate_hub_routing_fee()` and `calculate_escrow_fee()`
- Remove `from_agent_tier` parameter from fee calculation methods
- Add `FeeType.MULTISIG_ESCROW` with same 1% rate, collected at creation time
- Min fee stays at 0.0001 XMR

**Fee flow for multisig escrow**:
```
Buyer wants to escrow 10 XMR
  -> 0.1 XMR fee deducted to hub revenue account
  -> 9.9 XMR enters 2-of-3 multisig wallet
  -> Seller receives 9.9 XMR on release
```

---

## 2. Spending Policies (Phase 1)

### Problem
AI agents spend autonomously. Operators need guardrails to prevent runaway spending.

### Solution
Declarative spending policy attached to each agent, enforced server-side with Redis atomic operations.

### Data Model

New `SpendingPolicy` table:

```
spending_policies
  id: UUID (PK)
  agent_id: UUID (FK -> agents.id, UNIQUE)
  max_per_tx: Decimal (nullable) -- max single transaction amount
  max_per_session: Decimal (nullable) -- max per SDK session
  daily_limit: Decimal (nullable) -- rolling 24h limit
  allowed_agents: JSON (nullable) -- glob patterns ["research-*", "data-*"]
  blocked_agents: JSON (nullable) -- glob patterns ["spam-*"]
  require_escrow_above: Decimal (nullable) -- auto-require escrow for amounts above this
  is_active: Boolean (default true)
  created_at: DateTime
  updated_at: DateTime
```

### Session Definition

A "session" is an SDK-generated UUID created when `Sthrip()` is instantiated. Passed as `X-Sthrip-Session` header on every request. Server tracks cumulative spend per session_id in Redis with a 24-hour TTL. Sessions are ephemeral — if the SDK restarts, a new session begins.

### Enforcement Architecture

**Server-side (mandatory)**: Every payment request passes through `SpendingPolicyService.validate()` before execution.

Validation chain (sequential, fail-fast):
1. `max_per_tx` — simple comparison, no Redis needed
2. `allowed_agents` / `blocked_agents` — `fnmatch` glob matching against recipient agent_name
3. `daily_limit` — Redis sorted set with Lua script for atomic check-and-spend
4. `max_per_session` — Redis key per session_id with TTL (24h)
5. `require_escrow_above` — if amount > threshold and escrow not requested, reject with 400 and hint `"use_escrow": true`. This forces hub-held escrow, not multisig (multisig is always explicit opt-in via `mode: "multisig"`)

**Redis pattern for daily_limit** (atomic Lua script):
```lua
-- KEYS[1] = "spending:{agent_id}:daily"
-- ARGV[1] = window_start (now - 86400)
-- ARGV[2] = now (timestamp)
-- ARGV[3] = amount (XMR as string)
-- ARGV[4] = daily_limit (XMR as string)
-- ARGV[5] = tx_id (unique)
-- Returns: 1 = approved, 0 = rejected
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
local entries = redis.call('ZRANGEBYSCORE', KEYS[1], ARGV[1], '+inf', 'WITHSCORES')
local total = 0
for i = 2, #entries, 2 do total = total + tonumber(entries[i]) end
if total + tonumber(ARGV[3]) > tonumber(ARGV[4]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[5])
redis.call('EXPIRE', KEYS[1], 86410)
return 1
```

**Client-side (SDK, advisory)**: `StrhipClient` stores policy locally and does pre-flight `would_exceed()` checks before making API calls. This saves a round-trip for obvious rejections.

### API Endpoints

```
PUT  /v2/me/spending-policy   -- Set/update spending policy
GET  /v2/me/spending-policy   -- Get current policy
GET  /v2/me/spending-status   -- Get current spend against limits (daily_spent, session_spent)
```

### SDK Interface

```python
s = Sthrip(
    max_per_session=2.0,
    max_per_tx=0.5,
    allowed_agents=['research-*', 'data-*'],
    daily_limit=5.0,
    require_escrow_above=1.0
)

# Pre-flight check (client-side, no API call)
if s.would_exceed(amount=0.3):
    print("Would exceed policy")

# Server enforces regardless of client checks
s.pay("research-bot", 0.3)  # passes
s.pay("spam-bot", 0.3)      # rejected: not in allowed_agents
s.pay("research-bot", 10.0) # rejected: exceeds max_per_tx
```

### Files to Create/Modify
- **New**: `sthrip/db/spending_policy_repo.py` (~60 lines)
- **New**: `sthrip/services/spending_policy_service.py` (~150 lines)
- **New**: `api/routers/spending_policy.py` (~80 lines)
- **Modify**: `api/routers/payments.py` — add policy check before payment execution
- **Modify**: `api/routers/escrow.py` — add policy check before escrow creation
- **Modify**: `sdk/sthrip/client.py` — add policy params to constructor, `would_exceed()` method
- **New**: Alembic migration for `spending_policies` table

---

## 3. Webhook Registration API (Phase 1)

### Current State
Webhook delivery works (async, retry, HMAC, SSRF protection). Missing: self-service registration — agents can only set one `webhook_url` via registration or settings update.

### Solution
Multi-endpoint webhook management with event filtering and Standard Webhooks spec compliance.

### Data Model

New `WebhookEndpoint` table:

```
webhook_endpoints
  id: UUID (PK)
  agent_id: UUID (FK -> agents.id)
  url: String (max 2048)
  description: String (max 256, nullable)
  secret_encrypted: LargeBinary -- Fernet-encrypted signing secret
  event_filters: JSON -- ["payment.received", "escrow.*"] (null = all events)
  is_active: Boolean (default true)
  failure_count: Integer (default 0)
  disabled_at: DateTime (nullable) -- auto-disabled after 5 consecutive failures
  created_at: DateTime
  updated_at: DateTime

  UNIQUE(agent_id, url) -- no duplicate URLs per agent
  Max 10 endpoints per agent
```

### Standard Webhooks Compliance

Adopt the Standard Webhooks spec (standardwebhooks.com):
- Headers: `webhook-id`, `webhook-timestamp`, `webhook-signature`
- Signing: HMAC-SHA256 over `{msg_id}.{timestamp}.{body}`, base64-encoded with `v1,` prefix
- Secret format: base64-encoded, prefixed with `whsec_`
- Timestamp tolerance: 5 minutes for replay protection
- Library: `pip install standardwebhooks` (signing/verification utility)

### Secret Rotation

When agent requests rotation:
1. Generate new secret
2. Store both old and new (dual-sign for 24 hours)
3. Webhook deliveries include signatures from both secrets
4. After 24h, old secret is removed

### API Endpoints

```
POST   /v2/webhooks              -- Register endpoint (returns secret once)
GET    /v2/webhooks              -- List agent's endpoints (secrets masked)
PATCH  /v2/webhooks/{id}         -- Update URL, event_filters, description
DELETE /v2/webhooks/{id}         -- Remove endpoint
POST   /v2/webhooks/{id}/rotate  -- Rotate signing secret (returns new secret once)
POST   /v2/webhooks/{id}/test    -- Send test event
```

### Delivery Changes

Modify `WebhookService.process_event()`:
1. For each event, query active endpoints for the agent where `event_type` matches `event_filters` (supports glob: `escrow.*`)
2. Fan out delivery to all matching endpoints (existing concurrent delivery with semaphore)
3. After 5 consecutive failures, auto-disable endpoint and notify agent
4. Keep backward compatibility: if agent has legacy `webhook_url` in profile, treat it as a single endpoint

### Files to Create/Modify
- **New**: `sthrip/db/webhook_endpoint_repo.py` (~80 lines)
- **New**: `api/routers/webhook_endpoints.py` (~120 lines)
- **Modify**: `sthrip/services/webhook_service.py` — fan-out to multiple endpoints, Standard Webhooks signing
- **Modify**: `sthrip/db/models.py` — add `WebhookEndpoint` model
- **New**: Alembic migration
- **Add dep**: `standardwebhooks` to requirements.txt

---

## 4. OpenAPI Spec & TypeScript SDK (Phase 1)

### OpenAPI

One-line change: `api/main_v2.py` line 399, change `openapi_url=None` to `openapi_url="/openapi.json"`.

Export spec at build time: `python -c "from api.main_v2 import create_app; import json; print(json.dumps(create_app().openapi()))" > openapi.json`

### TypeScript SDK

**Primary approach**: Stainless (free tier for OSS, same tool that generates OpenAI/Anthropic SDKs).
- Point at `openapi.json`
- Auto-generates: typed client, retry, streaming, error classes, resource namespaces
- Publish as `npm install sthrip`

**Fallback**: If Stainless doesn't fit — hey-api/openapi-ts (4.4k stars, used by Vercel) for code generation, then thin wrapper for `sthrip.escrow.create()` DX.

**SDK shape** (both Python and TypeScript):
```typescript
import Sthrip from 'sthrip';

const s = new Sthrip({ apiKey: 'sk_...', baseUrl: '...' });
await s.payments.send({ to: 'research-bot', amount: 0.5 });
await s.escrow.create({ seller: 'data-bot', amount: 5.0 });
await s.balance.get();
```

### Files to Create/Modify
- **Modify**: `api/main_v2.py` — re-enable openapi_url
- **New**: `openapi.json` — exported spec (gitignored, generated at build)
- **New**: `sdk/typescript/` — generated SDK package
- **New**: `sdk/typescript/package.json` — npm package config

---

## 5. Encrypted Agent Messaging (Phase 2)

### Problem
Agents exchange metadata (delivery instructions, confirmations) in plaintext. Hub sees everything. For real anonymity, message content must be invisible to the hub.

### Solution
E2E encrypted messaging via NaCl Box (Curve25519 + XSalsa20-Poly1305). Hub is relay-only — never sees plaintext.

### Library
**PyNaCl** v1.6.2 (pyca, same org as `cryptography`). Bundles libsodium. Zero system deps.
TypeScript clients use `tweetnacl` (wire-compatible).

### Key Management

At registration or opt-in, each agent generates a Curve25519 keypair:
- **Public key** stored in `agents` table (new column `encryption_public_key BYTEA`)
- **Private key** stays with agent/client only (never sent to hub)
- SDK auto-generates keypair on first init, stores in local config

### Message Flow

```
Agent A                          Hub (relay)                     Agent B
  |                                |                                |
  | 1. GET /v2/agents/B/public-key |                                |
  | -----------------------------> |                                |
  | <-- pk_B                       |                                |
  |                                |                                |
  | 2. encrypt(msg, sk_A, pk_B)    |                                |
  |    (client-side)               |                                |
  |                                |                                |
  | 3. POST /v2/messages/send      |                                |
  |    {to: B, ciphertext, nonce,  |                                |
  |     payment_id, pk_sender}     |                                |
  | -----------------------------> |                                |
  |                                | 4. Validate: A is party to     |
  |                                |    payment_id. Store nothing.  |
  |                                |    Forward to B.               |
  |                                | -----------------------------> |
  |                                |                                |
  |                                |    5. decrypt(ct, sk_B, pk_A)  |
  |                                |       (client-side)            |
```

### Hub Guarantees
- Hub **never** sees plaintext
- Hub **does not store** message content (relay-only)
- Hub stores delivery metadata only: `{message_id, from_agent_id, to_agent_id, payment_id, delivered_at, size_bytes}` for audit/debugging
- Messages are ephemeral — TTL 24 hours for undelivered, then dropped

### Delivery Mechanism
- **Polling** (MVP): `GET /v2/messages/inbox` — returns pending encrypted messages
- **WebSocket** (future): push delivery for real-time agents

### API Endpoints

```
POST /v2/messages/send           -- Relay encrypted message
GET  /v2/messages/inbox          -- Fetch pending messages (encrypted)
GET  /v2/agents/{id}/public-key  -- Get agent's encryption public key
PUT  /v2/me/encryption-key       -- Register/rotate encryption public key
```

### Size Limits
- Max message: 64 KB (ciphertext)
- Max pending per agent: 100 messages
- TTL: 24 hours

### Files to Create/Modify
- **New**: `sthrip/services/messaging_service.py` (~120 lines)
- **New**: `api/routers/messages.py` (~100 lines)
- **Modify**: `sthrip/db/models.py` — add `encryption_public_key` to Agent, add `MessageRelay` model
- **Modify**: `sdk/sthrip/client.py` — add `send_message()`, `get_messages()`, keypair management
- **New**: Alembic migration
- **Add dep**: `PyNaCl>=1.6.0` to requirements.txt

---

## 6. ZK Reputation Proofs (Phase 2)

### Problem
Agent trust_score is public (0-100). Agent can't prove "my score >= X" without revealing exact score. This leaks information about transaction history.

### Solution
Pedersen commitment + sigma protocol range proof via **zksk** (EPFL, Zero-Knowledge Swiss Knife).

### How It Works

1. **Commitment** (when score changes): Hub computes `C = score * G + r * H` where `r` is random blinding factor. Commitment `C` is published in agent profile. `(score, r)` stored privately.

2. **Proof generation** (agent requests): Hub generates ZK proof that `score >= threshold` without revealing `score`:
   - Compute `delta = score - threshold`
   - Prove `delta in [0, 100]` using `RangeStmt`
   - Return `{commitment, proof, threshold, valid_until}`

3. **Verification** (anyone): Verify proof against public commitment. No API call needed — can verify offline.

### Library
**zksk** v0.0.3 (EPFL). Pure Python, composable sigma protocols. Built-in `RangeStmt` + `RangeOnlyStmt` for Pedersen commitments. No trusted setup.

```python
from zksk import Secret
from zksk.primitives.rangeproof import RangeOnlyStmt

x = Secret(value=75)  # actual score
stmt = RangeOnlyStmt(50, 101, x)  # prove 50 <= score < 101
proof = stmt.prove()
assert stmt.verify(proof)  # 20-80ms
```

### Performance
- Proof generation: 50-200ms (server-side)
- Verification: 20-80ms (can be done client-side or via API)
- Proof size: ~2-4 KB (acceptable for API payloads)

### API Endpoints

```
POST /v2/me/reputation-proof     -- Generate proof for threshold
     Body: { "threshold": 50 }
     Returns: { commitment, proof (base64), threshold, valid_until }

POST /v2/verify-reputation       -- Verify a proof (stateless)
     Body: { commitment, proof, threshold }
     Returns: { valid: true/false }
```

### Caching
- Cache proof per `(agent_id, threshold)` until score changes
- Invalidate all cached proofs when trust_score is updated

### Files to Create/Modify
- **New**: `sthrip/services/zk_reputation_service.py` (~150 lines)
- **New**: `api/routers/reputation.py` (~60 lines)
- **Modify**: `sthrip/db/models.py` — add `reputation_commitment` and `reputation_blinding` to Agent
- **Replace**: `sthrip/bridge/privacy/zk_verifier.py` — current placeholder with real zksk integration
- **New**: Alembic migration
- **Add dep**: `zksk` to requirements.txt (install from git)

---

## 7. Dual-Mode Escrow (Phase 2)

### Current State
Hub-held escrow works: CREATED -> ACCEPTED -> DELIVERED -> COMPLETED. Multi-milestone support. Hub holds all funds.

### New: Multisig Escrow (Opt-in)

Add Monero native 2-of-3 multisig as an opt-in mode for agents who want trustless security. Hub cannot unilaterally spend funds.

### Two Modes

| Mode | Default | Speed | Trust | Fee |
|---|---|---|---|---|
| `hub-held` | Yes | ~50ms | Trust hub | 1% at release |
| `multisig` | No (opt-in) | ~minutes (setup) | Trustless 2-of-3 | 1% upfront |

### Multisig Flow

**Setup (3 rounds, async-tolerant, up to 48h)**:
```
1. Buyer requests multisig escrow via POST /v2/escrow/create {mode: "multisig"}
2. Hub creates 3 wallet-rpc instances (buyer, seller, hub/arbiter)
3. Round 1: All 3 call prepare_multisig -> exchange multisig_info
4. Round 2: All 3 call make_multisig -> exchange multisig_info
5. Round 3: All 3 call exchange_multisig_keys -> wallet address finalized
6. 1% fee deducted from buyer balance to hub revenue
7. Remaining amount sent to multisig address
8. Wait for 10 confirmations
```

**Release (2 rounds)**:
```
1. All 3 export_multisig_info + import (sync)
2. Buyer (or hub) creates release tx -> partial signature
3. Seller (or hub) co-signs -> fully signed
4. Submit to network
```

**Dispute**:
```
Hub (arbiter) + either party = 2-of-3 can release or refund
```

### Agent Wallet Management

Each agent participating in multisig needs a persistent wallet-rpc session. For MVP:
- Hub manages wallet-rpc instances for all parties
- Agent's key share is encrypted with their API key and stored server-side
- Agent can export their key share for self-custody (advanced)

This is a **pragmatic compromise**: hub manages wallet infrastructure but cannot spend alone (2-of-3). Full self-custody comes in Phase 3 (Sthrip Chain).

### Coordination Service

New `MultisigCoordinator` service manages the async round-trip:
- Redis-backed message queue for exchanging multisig_info between parties
- State machine: `SETUP_ROUND_1 -> SETUP_ROUND_2 -> SETUP_ROUND_3 -> FUNDED -> ACTIVE`
- 48-hour timeout per round (auto-cancel if party unresponsive)
- Existing wallet RPC wrapper in `sthrip/swaps/xmr/wallet.py` has all needed methods

### API Endpoints

```
POST   /v2/escrow/create          -- {mode: "hub-held" | "multisig", ...}
POST   /v2/escrow/{id}/round      -- Submit multisig round data
GET    /v2/escrow/{id}/round      -- Get pending round data to process
POST   /v2/escrow/{id}/release    -- Initiate release (creates partial sig)
POST   /v2/escrow/{id}/cosign     -- Co-sign release tx
POST   /v2/escrow/{id}/dispute    -- Initiate dispute (arbiter involvement)
```

### Known Risks (from research)
- Monero multisig is still experimental — fund loss is possible
- Key cancellation attack (patched but no formal audit)
- FROST-based replacement coming — current impl may need migration
- **Mitigation**: multisig is opt-in, clear warning in SDK/API docs, amount cap (e.g. 100 XMR max per multisig deal)

### Files to Create/Modify
- **New**: `sthrip/services/multisig_coordinator.py` (~300 lines)
- **New**: `api/routers/multisig_escrow.py` (~200 lines)
- **Modify**: `api/routers/escrow.py` — add `mode` parameter, route to appropriate service
- **Modify**: `sthrip/db/models.py` — add `MultisigEscrow` model, `MultisigRound` model
- **Modify**: `sthrip/services/fee_collector.py` — add `MULTISIG_ESCROW` fee type, collect upfront
- **New**: Alembic migration

---

## 8. Sybil Prevention (Phase 2)

### Problem
Agents register for free with zero friction. Attacker can create thousands of fake agents to manipulate reputation, spam marketplace, or abuse rate limits.

### Solution
Hashcash-style proof-of-work at registration. Client must solve a computational puzzle before the server accepts registration.

### Flow

```
1. Client: POST /v2/agents/register/challenge
   Server: Returns { challenge: "sha256:20:random_nonce", expires_at }

2. Client: Find nonce where SHA256(challenge + nonce) has 20 leading zero bits
   (~1M hashes, ~1-3 seconds on modern CPU)

3. Client: POST /v2/agents/register { ..., pow_challenge, pow_nonce }
   Server: Verify proof-of-work, then register
```

### Difficulty Tuning
- Default: 20 bits (~1 second on fast CPU)
- Under load (>100 registrations/hour): increase to 24 bits (~16 seconds)
- Admin configurable via env var `POW_DIFFICULTY_BITS`

### Why Hashcash, Not Stake/Bond
- AI agents need to register quickly and start working
- Staking requires existing XMR balance (chicken-and-egg)
- PoW is one-time cost, doesn't lock capital
- Trivial for legitimate agents, expensive for mass-creation

### Files to Create/Modify
- **New**: `sthrip/services/pow_service.py` (~80 lines)
- **Modify**: `api/routers/agents.py` — add challenge endpoint, verify PoW on registration
- **Modify**: `sdk/sthrip/client.py` — auto-solve PoW during registration

---

## 9. Implementation Priority & Dependencies

### Phase 1 (~2 weeks)

| Week | Task | Depends On |
|---|---|---|
| 1 | Fee model overhaul (flat 1%) | Nothing |
| 1 | Spending policies (model + Redis + API) | Nothing |
| 1 | OpenAPI spec (1-line change) | Nothing |
| 2 | Webhook registration API + Standard Webhooks | Nothing |
| 2 | TypeScript SDK generation | OpenAPI spec |

All Phase 1 tasks are independent except TS SDK -> OpenAPI.

### Phase 2 (~4-5 weeks)

| Week | Task | Depends On |
|---|---|---|
| 3 | Encrypted messaging (PyNaCl) | Nothing |
| 3 | Sybil prevention (PoW) | Nothing |
| 3-4 | ZK reputation proofs (zksk) | Nothing |
| 4-6 | Multisig escrow coordinator | Fee model (1% upfront) |
| 6-7 | Integration testing + docs | All above |

### What We Are NOT Building (Phase 3-4)
- Own blockchain / $STHR token
- Payment channels (off-chain)
- Hub federation / DHT
- x402-Sthrip protocol
- PoS consensus
- ZK agent credentials

---

## 10. New Dependencies

| Package | Version | Purpose | Size |
|---|---|---|---|
| `standardwebhooks` | latest | Webhook signing (Standard Webhooks spec) | Tiny |
| `PyNaCl` | >=1.6.0 | NaCl Box encryption for messaging | ~2MB (bundles libsodium) |
| `zksk` | 0.0.3 (git) | ZK range proofs for reputation | ~500KB |

All other features use existing deps (Redis, SQLAlchemy, cryptography, Pydantic).

---

## 11. Testing Strategy

Each feature gets:
- **Unit tests**: Service logic, policy validation, fee calculation
- **Integration tests**: API endpoints with TestClient + SQLite in-memory
- **Edge cases**: concurrent payments vs spending limits (Redis race conditions), multisig timeout handling, PoW difficulty edge cases

Target: maintain 80%+ coverage.

Specific test scenarios:
- Spending policy: two concurrent payments that together exceed daily_limit (only one should succeed — Lua script atomicity)
- Webhook fan-out: event matching against glob filters, auto-disable after failures
- Encrypted messaging: encrypt with PyNaCl Python, decrypt with tweetnacl JS (cross-platform compatibility)
- ZK proof: verify that proof for threshold=50 passes when score=75, fails when score=40
- Multisig: mock wallet-rpc round-trips, test timeout/cancellation
- PoW: verify difficulty enforcement, replay protection

---

## 12. Migration Strategy

All DB changes via Alembic with idempotent migrations (IF NOT EXISTS / IF EXISTS).

New tables: `spending_policies`, `webhook_endpoints`, `message_relays`, `multisig_escrows`, `multisig_rounds`.

New columns on `agents`: `encryption_public_key`, `reputation_commitment`, `reputation_blinding`.

Backward compatibility:
- Legacy `agent.webhook_url` continues to work (treated as single endpoint)
- Agents without spending policy have no limits (current behavior)
- Agents without encryption key can't use messaging (opt-in)
- All payments default to hub-held (current behavior)
