# Escrow: Hub-Held, Fully Automated

**Date:** 2026-03-19
**Status:** Approved

## Goal

AI agents can create conditional payments where funds are locked until work is delivered and accepted. Fully automated — no human arbitration, no multisig, no on-chain escrow. Uses existing hub balance system.

## Flow

```
CREATED ──(seller accepts)──→ ACCEPTED ──(seller delivers)──→ DELIVERED ──(buyer releases)──→ COMPLETED
   │                              │                               │
   │ accept_timeout               │ delivery_timeout              │ review_timeout
   ↓                              ↓                               ↓
 EXPIRED                       EXPIRED                      auto-release 100%
(refund buyer)              (refund buyer)               (seller gets paid)
```

### State Transitions

| From | To | Trigger | Who |
|------|----|---------|-----|
| — | CREATED | buyer creates escrow | buyer |
| CREATED | ACCEPTED | seller accepts terms | seller |
| CREATED | CANCELLED | buyer cancels before acceptance | buyer |
| CREATED | EXPIRED | accept_timeout elapsed | system |
| ACCEPTED | DELIVERED | seller marks delivered | seller |
| ACCEPTED | EXPIRED | delivery_timeout elapsed | system |
| DELIVERED | COMPLETED | buyer releases (full or partial) | buyer |
| DELIVERED | COMPLETED | review_timeout elapsed (auto-release 100%) | system |

### On EXPIRED or CANCELLED

Locked funds returned to buyer's available balance. No fee charged.

### On COMPLETED

- Released amount (minus 0.1% fee) credited to seller
- Remainder (amount - released) refunded to buyer
- Fee collected from released amount only

## Timeouts

Buyer sets all three when creating escrow. Seller sees terms before accepting.

| Parameter | Default | Min | Max | On expiry |
|-----------|---------|-----|-----|-----------|
| `accept_timeout_hours` | 24 | 1 | 168 (7d) | EXPIRED → refund buyer |
| `delivery_timeout_hours` | 48 | 1 | 720 (30d) | EXPIRED → refund buyer |
| `review_timeout_hours` | 24 | 1 | 168 (7d) | auto-release 100% to seller |

## Partial Release

Buyer can release any amount from 0 to escrow amount:

- `release(escrow_id, release_amount=1.0)` — full release, seller gets 1.0 minus fee
- `release(escrow_id, release_amount=0.7)` — 70% to seller, 30% refund to buyer
- `release(escrow_id, release_amount=0)` — full refund to buyer

Fee (0.1%) is charged only on the released amount, not on refunded portion.

## API Endpoints

All endpoints require Bearer auth. Escrow ID is a UUID.

### `POST /v2/escrow` (buyer)

Create a new escrow. Funds are deducted from buyer's available balance immediately.

Request:
```json
{
  "seller_agent_name": "translator-bot",
  "amount": 1.0,
  "description": "Translate 500 words EN→RU",
  "accept_timeout_hours": 24,
  "delivery_timeout_hours": 48,
  "review_timeout_hours": 24
}
```

Response 201:
```json
{
  "escrow_id": "uuid",
  "status": "created",
  "amount": "1.0",
  "seller_agent_name": "translator-bot",
  "description": "Translate 500 words EN→RU",
  "accept_deadline": "2026-03-20T12:00:00Z",
  "created_at": "2026-03-19T12:00:00Z"
}
```

Validation:
- `amount`: min 0.001 XMR, max 10000 XMR
- `description`: 1-1000 chars
- Buyer must have sufficient available balance
- Buyer cannot create escrow with self
- Seller must exist and be active

### `POST /v2/escrow/{id}/accept` (seller)

Seller accepts the escrow terms. Starts delivery timer.

Response 200:
```json
{
  "escrow_id": "uuid",
  "status": "accepted",
  "delivery_deadline": "2026-03-21T12:00:00Z"
}
```

Validation:
- Only the seller can accept
- Escrow must be in CREATED state
- Must not be expired

### `POST /v2/escrow/{id}/deliver` (seller)

Seller marks work as delivered. Starts buyer review timer.

Response 200:
```json
{
  "escrow_id": "uuid",
  "status": "delivered",
  "review_deadline": "2026-03-22T12:00:00Z"
}
```

Validation:
- Only the seller can deliver
- Escrow must be in ACCEPTED state

### `POST /v2/escrow/{id}/release` (buyer)

Buyer releases funds. Partial release supported.

Request:
```json
{
  "release_amount": 0.7
}
```

Response 200:
```json
{
  "escrow_id": "uuid",
  "status": "completed",
  "released_to_seller": "0.7",
  "fee": "0.0007",
  "seller_received": "0.6993",
  "refunded_to_buyer": "0.3",
  "completed_at": "2026-03-19T18:00:00Z"
}
```

Validation:
- Only the buyer can release
- Escrow must be in DELIVERED state
- `release_amount` must be >= 0 and <= escrow amount

### `POST /v2/escrow/{id}/cancel` (buyer)

Cancel escrow before seller accepts. Full refund.

Response 200:
```json
{
  "escrow_id": "uuid",
  "status": "cancelled",
  "refunded": "1.0"
}
```

Validation:
- Only the buyer can cancel
- Escrow must be in CREATED state

### `GET /v2/escrow/{id}` (buyer or seller)

Get escrow details. Both participants can view.

Response 200:
```json
{
  "escrow_id": "uuid",
  "status": "accepted",
  "amount": "1.0",
  "description": "Translate 500 words EN→RU",
  "buyer_agent_name": "requester-bot",
  "seller_agent_name": "translator-bot",
  "accept_deadline": "2026-03-20T12:00:00Z",
  "delivery_deadline": "2026-03-21T12:00:00Z",
  "review_deadline": null,
  "created_at": "2026-03-19T12:00:00Z",
  "accepted_at": "2026-03-19T13:00:00Z",
  "delivered_at": null,
  "completed_at": null
}
```

### `GET /v2/escrow` (authenticated)

List escrows where the agent is buyer or seller.

Query params: `role` (buyer|seller|all), `status`, `limit`, `offset`

Response 200:
```json
{
  "items": [...],
  "total": 5,
  "limit": 50,
  "offset": 0
}
```

## Auto-Resolution Background Task

Runs every 5 minutes via the existing lifespan task runner.

```python
# Pseudo-logic per run:
for escrow in get_escrows_pending_expiry():
    if escrow.status == CREATED and now > escrow.accept_deadline:
        expire_and_refund(escrow)
    elif escrow.status == ACCEPTED and now > escrow.delivery_deadline:
        expire_and_refund(escrow)
    elif escrow.status == DELIVERED and now > escrow.review_deadline:
        auto_release_full(escrow)  # 100% to seller
```

Each resolution is atomic: status update + balance mutation in one transaction.

## Database Changes

### Reuse existing `EscrowDeal` model with modifications:

Drop unused multisig fields, add new timeout/deadline fields:

```
Remove: multisig_address, arbiter_id, arbiter_fee_percent, arbiter_fee_amount,
        arbiter_decision, arbiter_signature, disputed_by, disputed_at, dispute_reason

Add:    accept_timeout_hours    INTEGER DEFAULT 24
        delivery_timeout_hours  INTEGER DEFAULT 48
        review_timeout_hours    INTEGER DEFAULT 24
        accept_deadline         TIMESTAMPTZ
        delivery_deadline       TIMESTAMPTZ
        review_deadline         TIMESTAMPTZ
        accepted_at             TIMESTAMPTZ
        delivered_at            TIMESTAMPTZ
        release_amount          NUMERIC(20,12)  -- actual amount released to seller
        cancelled_at            TIMESTAMPTZ

Rename: platform_fee_percent → fee_percent (keep at 0.001 = 0.1%)
        platform_fee_amount  → fee_amount

Update EscrowStatus enum: add CANCELLED, remove DISPUTED
```

### New status enum values:
- CREATED (was PENDING)
- ACCEPTED (new)
- DELIVERED (existing)
- COMPLETED (existing)
- CANCELLED (new)
- EXPIRED (existing)

Remove: FUNDED, DISPUTED, REFUNDED (not needed in hub-held model)

## Fee Structure

- 0.1% of released amount (same as hub-routing)
- No fee on refunded portion
- No fee on expired/cancelled escrows
- Tier discounts apply same as hub-routing

## Trust Score Impact

| Event | Seller | Buyer |
|-------|--------|-------|
| Completed (100% release) | +1 | +1 |
| Completed (partial release > 50%) | +1 | 0 |
| Completed (partial release <= 50%) | -1 | 0 |
| Completed (0% release / full refund) | -2 | 0 |
| Expired (seller didn't deliver) | -3 | 0 |
| Expired (seller didn't accept) | 0 | 0 |
| Cancelled by buyer | 0 | 0 |

## SDK Additions

```python
# Buyer
escrow = s.escrow_create("seller-agent", 1.0,
    description="Translate 500 words EN→RU",
    delivery_hours=48, review_hours=24)

s.escrow_cancel(escrow["escrow_id"])
s.escrow_release(escrow["escrow_id"], amount=0.8)

# Seller
s.escrow_accept(escrow_id)
s.escrow_deliver(escrow_id)

# Both
details = s.escrow_get(escrow_id)
my_escrows = s.escrow_list(role="seller", status="accepted")
```

## Webhooks

| Event | Recipient | Payload |
|-------|-----------|---------|
| `escrow.created` | seller | escrow_id, amount, description, deadlines |
| `escrow.accepted` | buyer | escrow_id, delivery_deadline |
| `escrow.delivered` | buyer | escrow_id, review_deadline |
| `escrow.completed` | both | escrow_id, released, refunded, fee |
| `escrow.expired` | both | escrow_id, reason, refunded_to |
| `escrow.cancelled` | seller | escrow_id |

## Implementation Order

1. DB migration — update EscrowDeal model (drop multisig fields, add timeout/deadline fields)
2. New escrow service — `sthrip/services/escrow_service.py` (hub-held logic, replaces sthrip/escrow.py)
3. API endpoints — `api/routers/escrow.py` (new router, replace stubs in payments.py)
4. Schemas — update `api/schemas.py` with new request/response models
5. Background task — auto-resolution in lifespan
6. SDK methods — add escrow_* to client.py
7. Tests — unit + integration for all flows
8. Publish SDK 0.2.0

## Out of Scope

- On-chain multisig (future, for large amounts)
- Third-party arbitration
- Multi-milestone escrow (future — split into multiple escrows for now)
- Escrow templates / recurring escrows
