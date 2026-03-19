# Multi-Milestone Escrow Extension

**Date:** 2026-03-19
**Status:** Draft
**Depends on:** [2026-03-19-escrow-design.md](./2026-03-19-escrow-design.md) (Approved, implemented)

## Goal

Extend the existing hub-held escrow system to support multi-milestone deals. A single escrow can be split into 1-10 sequential milestones, each with its own amount, delivery timeout, and review timeout. Funds are released incrementally as milestones complete. If any milestone fails or expires, remaining milestones are cancelled and unspent funds refunded.

Backward compatible: single-milestone escrow works exactly as before. Multi-milestone is opt-in via a `milestones` array in the create request.

## What Changes vs. Current Escrow

| Aspect | Current | Multi-Milestone |
|--------|---------|----------------|
| Funds locked | Full amount at creation | Full amount at creation (sum of all milestones) |
| Release | One release event | One release per milestone |
| State machine | Deal-level only | Deal-level + per-milestone |
| Timeouts | Per-deal | accept_timeout per-deal, delivery/review per-milestone |
| Fee | 0.1% of released amount | 0.1% of each milestone's released amount (same effective rate) |
| Trust score | Per-deal | Per-deal (computed at final milestone or cancellation) |
| Cancel | Buyer cancels before acceptance | Buyer cancels before acceptance (same) |
| Auto-resolution | Per-deal deadlines | Per-milestone deadlines |

**What stays the same:** Deal creation flow, accept flow, fee structure, trust score formula, webhook delivery, SDK auth, all single-milestone behavior.

## Flow

### Deal-Level State Machine (unchanged for single-milestone)

```
CREATED ──(seller accepts)──> ACCEPTED ──(all milestones complete)──> COMPLETED
   |                              |
   | accept_timeout               | any milestone expires/fails
   v                              v
 EXPIRED                    PARTIALLY_COMPLETED
(refund all)               (completed milestones keep their release,
                            remaining funds refunded to buyer)
```

New terminal state: `PARTIALLY_COMPLETED` -- at least one milestone was released before a subsequent milestone expired. Distinguishes from full completion.

For single-milestone deals (the default), `PARTIALLY_COMPLETED` is never reached -- behavior is identical to today.

### Milestone-Level State Machine

Each milestone follows the same pattern as the current deal-level ACCEPTED->DELIVERED->COMPLETED flow:

```
PENDING ──(previous milestone completes)──> ACTIVE ──(seller delivers)──> DELIVERED ──(buyer releases)──> COMPLETED
   |                                           |                              |
   |                                           | delivery_timeout             | review_timeout
   |                                           v                              v
   |                                        EXPIRED                     auto-release 100%
   |                                      (cancel rest)               (milestone completed)
   v
CANCELLED (parent deal cancelled/expired, or previous milestone failed)
```

| From | To | Trigger | Who |
|------|----|---------|-----|
| -- | PENDING | milestone created with deal | system |
| PENDING | ACTIVE | previous milestone completed (or deal accepted for milestone #1) | system |
| ACTIVE | DELIVERED | seller marks delivered | seller |
| ACTIVE | EXPIRED | delivery_timeout elapsed | system |
| DELIVERED | COMPLETED | buyer releases funds | buyer |
| DELIVERED | COMPLETED | review_timeout elapsed (auto-release 100%) | system |
| PENDING | CANCELLED | deal cancelled/expired, or earlier milestone failed | system |
| ACTIVE | CANCELLED | deal cancelled by buyer before delivery (not allowed after accept in current design -- reserved for future dispute flow) | -- |

### Milestone Sequencing

Milestones execute strictly in order. When milestone N completes (buyer releases or auto-release), milestone N+1 transitions from PENDING to ACTIVE and its delivery deadline timer starts. The deal's `expires_at` field is updated to track the currently active milestone's deadline.

### Terminal Conditions

1. **All milestones COMPLETED** -> Deal status = COMPLETED
2. **Any milestone EXPIRED (delivery timeout)** -> Remaining milestones CANCELLED, deal status = PARTIALLY_COMPLETED (or EXPIRED if milestone #1)
3. **Deal accept timeout** -> All milestones CANCELLED, deal status = EXPIRED
4. **Buyer cancels** -> Only in CREATED state (before accept), all milestones CANCELLED, deal status = CANCELLED

## Database Schema

### New Model: `EscrowMilestone`

```python
class MilestoneStatus(str, Enum):
    PENDING = "pending"        # Waiting for previous milestone
    ACTIVE = "active"          # Delivery timer running
    DELIVERED = "delivered"    # Seller marked delivered, review timer running
    COMPLETED = "completed"   # Buyer released (or auto-released)
    EXPIRED = "expired"        # Delivery timeout elapsed
    CANCELLED = "cancelled"   # Deal cancelled or previous milestone failed
```

```python
class EscrowMilestone(Base):
    __tablename__ = "escrow_milestones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    escrow_id = Column(UUID(as_uuid=True), ForeignKey("escrow_deals.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)  # 1-indexed: 1, 2, 3, ...

    # Terms
    description = Column(Text, nullable=False)
    amount = Column(Numeric(20, 12), nullable=False)

    # Per-milestone timeouts (hours)
    delivery_timeout_hours = Column(Integer, nullable=False)
    review_timeout_hours = Column(Integer, nullable=False)

    # Deadlines (set when milestone becomes ACTIVE / DELIVERED)
    delivery_deadline = Column(DateTime(timezone=True), nullable=True)
    review_deadline = Column(DateTime(timezone=True), nullable=True)

    # Release
    release_amount = Column(Numeric(20, 12), nullable=True)
    fee_amount = Column(Numeric(20, 12), default=Decimal("0"))

    status = Column(SQLEnum(MilestoneStatus), default=MilestoneStatus.PENDING)

    # Timestamps
    activated_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    escrow = relationship("EscrowDeal", back_populates="milestones")

    __table_args__ = (
        UniqueConstraint("escrow_id", "sequence", name="uq_milestone_sequence"),
        CheckConstraint("sequence >= 1 AND sequence <= 10", name="ck_milestone_sequence_range"),
        CheckConstraint("amount > 0", name="ck_milestone_amount_positive"),
    )
```

### Changes to `EscrowDeal`

Minimal additions -- no breaking changes:

```python
# New columns on EscrowDeal
is_multi_milestone = Column(Boolean, default=False)
milestone_count = Column(Integer, default=1)
current_milestone = Column(Integer, default=1)  # Sequence number of active milestone
total_released = Column(Numeric(20, 12), default=Decimal("0"))  # Running total across milestones
total_fees = Column(Numeric(20, 12), default=Decimal("0"))       # Running total fees

# New relationship
milestones = relationship("EscrowMilestone", back_populates="escrow",
                         order_by="EscrowMilestone.sequence",
                         cascade="all, delete-orphan")
```

New enum value on `EscrowStatus`:

```python
class EscrowStatus(str, Enum):
    CREATED = "created"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"          # Only used for single-milestone deals
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    PARTIALLY_COMPLETED = "partially_completed"  # NEW
```

### Migration Strategy

1. Add `MilestoneStatus` enum type
2. Add `escrow_milestones` table
3. Add new columns to `escrow_deals`: `is_multi_milestone`, `milestone_count`, `current_milestone`, `total_released`, `total_fees`
4. Add `partially_completed` to `EscrowStatus` enum
5. Backfill: existing completed deals get `total_released = release_amount`, `total_fees = fee_amount`
6. No data loss, no column drops, fully additive migration

## API Endpoints

### Modified: `POST /v2/escrow` (buyer)

Add optional `milestones` array. When absent, behaves exactly as today (single-milestone).

```json
{
  "seller_agent_name": "dev-bot",
  "amount": 2.0,
  "description": "Build a CLI tool in 3 phases",
  "accept_timeout_hours": 48,
  "milestones": [
    {
      "description": "Phase 1: Core parser",
      "amount": 0.5,
      "delivery_timeout_hours": 72,
      "review_timeout_hours": 24
    },
    {
      "description": "Phase 2: Plugin system",
      "amount": 0.8,
      "delivery_timeout_hours": 96,
      "review_timeout_hours": 24
    },
    {
      "description": "Phase 3: Documentation + polish",
      "amount": 0.7,
      "delivery_timeout_hours": 48,
      "review_timeout_hours": 24
    }
  ]
}
```

Validation:
- `milestones` array: 1-10 items
- Sum of milestone amounts MUST equal `amount` (exact match, no tolerance)
- Each milestone: `amount` > 0, `description` 1-500 chars
- Each milestone: `delivery_timeout_hours` 1-720, `review_timeout_hours` 1-168
- If `milestones` is absent, deal-level `delivery_timeout_hours` and `review_timeout_hours` are used (current behavior)
- If `milestones` is present, deal-level `delivery_timeout_hours` and `review_timeout_hours` are ignored

Response 201 (extended):
```json
{
  "escrow_id": "uuid",
  "status": "created",
  "amount": "2.0",
  "is_multi_milestone": true,
  "milestone_count": 3,
  "milestones": [
    {
      "sequence": 1,
      "description": "Phase 1: Core parser",
      "amount": "0.5",
      "delivery_timeout_hours": 72,
      "review_timeout_hours": 24,
      "status": "pending"
    },
    {
      "sequence": 2,
      "description": "Phase 2: Plugin system",
      "amount": "0.8",
      "delivery_timeout_hours": 96,
      "review_timeout_hours": 24,
      "status": "pending"
    },
    {
      "sequence": 3,
      "description": "Phase 3: Documentation + polish",
      "amount": "0.7",
      "delivery_timeout_hours": 48,
      "review_timeout_hours": 24,
      "status": "pending"
    }
  ],
  "accept_deadline": "2026-03-21T12:00:00Z",
  "created_at": "2026-03-19T12:00:00Z"
}
```

### New: `POST /v2/escrow/{id}/milestones/{n}/deliver` (seller)

Seller marks milestone N as delivered. Only works if milestone N is ACTIVE.

Response 200:
```json
{
  "escrow_id": "uuid",
  "milestone_sequence": 2,
  "milestone_status": "delivered",
  "review_deadline": "2026-03-22T12:00:00Z"
}
```

Validation:
- Milestone N must exist and be in ACTIVE status
- Only the seller can deliver
- Deal must be in ACCEPTED status

### New: `POST /v2/escrow/{id}/milestones/{n}/release` (buyer)

Buyer releases funds for milestone N. Partial release within a milestone is supported (same as current deal-level release).

Request:
```json
{
  "release_amount": 0.5
}
```

Response 200:
```json
{
  "escrow_id": "uuid",
  "milestone_sequence": 2,
  "milestone_status": "completed",
  "released_to_seller": "0.5",
  "fee": "0.0005",
  "seller_received": "0.4995",
  "refunded_to_buyer": "0.0",
  "deal_status": "accepted",
  "deal_total_released": "1.3",
  "next_milestone": 3
}
```

Validation:
- Milestone N must exist and be in DELIVERED status
- Only the buyer can release
- `release_amount` >= 0 and <= milestone amount
- Deal must be in ACCEPTED status

Side effects on release:
1. Credit seller: `release_amount - fee`
2. Refund buyer: `milestone_amount - release_amount`
3. Record fee
4. Update deal: `total_released += release_amount`, `total_fees += fee_amount`
5. If this is the last milestone: deal status -> COMPLETED
6. If not last: activate next milestone (set delivery deadline, status -> ACTIVE)

### Existing endpoints: backward-compatible behavior

| Endpoint | Multi-milestone behavior |
|----------|------------------------|
| `POST /v2/escrow/{id}/accept` | Same. On accept, milestone #1 becomes ACTIVE. |
| `POST /v2/escrow/{id}/deliver` | For multi-milestone deals: returns 400 with message "Use /milestones/{n}/deliver for multi-milestone escrows" |
| `POST /v2/escrow/{id}/release` | For multi-milestone deals: returns 400 with message "Use /milestones/{n}/release for multi-milestone escrows" |
| `POST /v2/escrow/{id}/cancel` | Same. Cancels deal + all PENDING milestones. Only in CREATED state. |
| `GET /v2/escrow/{id}` | Response includes `milestones` array (empty for single-milestone deals). |
| `GET /v2/escrow` | Response items include `is_multi_milestone` and `milestone_count` fields. |

## Fee Structure

**Per-milestone, same rate.** The 0.1% fee is charged on each milestone's released amount individually. This is mathematically equivalent to charging 0.1% on the total released at the end, but it is collected incrementally so the seller receives funds immediately per milestone.

```
Milestone 1: amount=0.5, buyer releases 0.5 -> fee=0.0005, seller gets 0.4995
Milestone 2: amount=0.8, buyer releases 0.6 -> fee=0.0006, seller gets 0.5994, buyer refund=0.2
Milestone 3: amount=0.7, buyer releases 0.7 -> fee=0.0007, seller gets 0.6993
```

Tier discounts (premium 50% off, verified 25% off) apply per-milestone at the deal's fee_percent rate, set at creation time.

No additional fee for using multi-milestone (same 0.1% rate as single-milestone).

## Auto-Resolution

The existing `resolve_expired` background task (runs every 5 minutes) is extended to handle milestone deadlines.

### Resolution Logic

For multi-milestone deals in ACCEPTED status:

1. Find the currently active milestone (status = ACTIVE or DELIVERED)
2. If ACTIVE and `delivery_deadline` passed:
   - Milestone -> EXPIRED
   - All subsequent PENDING milestones -> CANCELLED
   - Refund remaining funds to buyer: `deal.amount - deal.total_released`
   - Deal -> PARTIALLY_COMPLETED (if any milestone was completed) or EXPIRED (if milestone #1)
   - Seller trust: -3 (same as current delivery timeout penalty)
3. If DELIVERED and `review_deadline` passed:
   - Auto-release 100% of milestone amount to seller (same as current)
   - If last milestone: deal -> COMPLETED
   - If not last: activate next milestone

### Implementation

The `get_pending_expiry` query is extended to also check `escrow_milestones.expires_at` for milestones in ACTIVE or DELIVERED state. The `_resolve_single` method dispatches to milestone-level resolution when `deal.is_multi_milestone` is true.

```python
def get_pending_milestone_expiry(self) -> List[EscrowMilestone]:
    now = datetime.now(timezone.utc)
    return self.db.query(EscrowMilestone).filter(
        EscrowMilestone.status.in_([MilestoneStatus.ACTIVE, MilestoneStatus.DELIVERED]),
        EscrowMilestone.expires_at <= now,
    ).all()
```

## Trust Score Impact

Trust impact is calculated **per-deal at completion**, not per-milestone. This prevents gaming (e.g., delivering trivial milestone #1 to gain trust, then abandoning).

| Event | Seller | Buyer | Notes |
|-------|--------|-------|-------|
| All milestones completed (total_released = amount) | +1 | +1 | Full delivery |
| All milestones completed (total_released > 50% of amount) | +1 | 0 | Mostly delivered |
| All milestones completed (total_released <= 50% of amount) | -1 | 0 | Mostly refunded |
| All milestones completed (total_released = 0) | -2 | 0 | Full refund across all milestones |
| Milestone expired (seller didn't deliver) | -3 | 0 | Same as current delivery timeout |
| Deal expired (seller didn't accept) | 0 | 0 | Same as current |
| Cancelled by buyer | 0 | 0 | Same as current |

Trust is applied once when the deal reaches a terminal state (COMPLETED, PARTIALLY_COMPLETED, EXPIRED, CANCELLED). The formula uses `deal.total_released / deal.amount` ratio, same as the current `release_amount / escrow_amount` logic.

## SDK Methods

New methods on the `Sthrip` client class:

```python
# Create multi-milestone escrow
escrow = s.escrow_create(
    "dev-bot", 2.0,
    description="Build CLI tool in 3 phases",
    accept_hours=48,
    milestones=[
        {"description": "Phase 1: Core parser", "amount": 0.5,
         "delivery_hours": 72, "review_hours": 24},
        {"description": "Phase 2: Plugin system", "amount": 0.8,
         "delivery_hours": 96, "review_hours": 24},
        {"description": "Phase 3: Documentation", "amount": 0.7,
         "delivery_hours": 48, "review_hours": 24},
    ],
)

# Seller delivers milestone 1
s.escrow_milestone_deliver(escrow["escrow_id"], milestone=1)

# Buyer releases milestone 1
s.escrow_milestone_release(escrow["escrow_id"], milestone=1, amount=0.5)

# Seller delivers milestone 2
s.escrow_milestone_deliver(escrow["escrow_id"], milestone=2)

# Buyer releases milestone 2 (partial -- 75% of milestone amount)
s.escrow_milestone_release(escrow["escrow_id"], milestone=2, amount=0.6)

# ... and so on
```

SDK method signatures:

```python
def escrow_milestone_deliver(self, escrow_id, milestone):
    """Seller marks milestone N as delivered."""
    return self._raw_post(
        "/v2/escrow/{}/milestones/{}/deliver".format(escrow_id, milestone)
    )

def escrow_milestone_release(self, escrow_id, milestone, amount):
    """Buyer releases funds for milestone N."""
    payload = {"release_amount": str(amount)}
    return self._raw_post(
        "/v2/escrow/{}/milestones/{}/release".format(escrow_id, milestone),
        json_body=payload,
    )
```

The existing `escrow_create` method is extended to accept an optional `milestones` kwarg. When present, the `milestones` array is included in the request body and `delivery_hours`/`review_hours` params are ignored.

## Webhooks

New webhook events for milestones:

| Event | Recipient | Payload |
|-------|-----------|---------|
| `escrow.milestone.activated` | seller | escrow_id, milestone_sequence, description, amount, delivery_deadline |
| `escrow.milestone.delivered` | buyer | escrow_id, milestone_sequence, review_deadline |
| `escrow.milestone.completed` | both | escrow_id, milestone_sequence, released, refunded, fee, next_milestone |
| `escrow.milestone.expired` | both | escrow_id, milestone_sequence, remaining_refunded |

Existing deal-level webhooks still fire at deal-level transitions:
- `escrow.created` -- includes `milestones` array if multi-milestone
- `escrow.accepted` -- includes `milestone_count`
- `escrow.completed` -- includes `total_released`, `total_fees`
- `escrow.expired` / `escrow.cancelled` -- unchanged

## Pydantic Schemas

### New request models

```python
class MilestoneDefinition(BaseModel):
    description: str = Field(..., min_length=1, max_length=500)
    amount: Decimal = Field(..., gt=Decimal("0"), le=Decimal("10000"))
    delivery_timeout_hours: int = Field(..., ge=1, le=720)
    review_timeout_hours: int = Field(..., ge=1, le=168)

class EscrowCreateRequest(BaseModel):  # Extended
    seller_agent_name: str = Field(...)
    amount: Decimal = Field(...)
    description: str = Field(...)
    accept_timeout_hours: int = Field(default=24, ge=1, le=168)
    delivery_timeout_hours: int = Field(default=48, ge=1, le=720)   # Ignored if milestones present
    review_timeout_hours: int = Field(default=24, ge=1, le=168)     # Ignored if milestones present
    milestones: Optional[List[MilestoneDefinition]] = Field(default=None, min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_milestone_amounts(self):
        if self.milestones is not None:
            total = sum(m.amount for m in self.milestones)
            if total != self.amount:
                raise ValueError(
                    f"Milestone amounts sum to {total}, must equal deal amount {self.amount}"
                )
        return self

class MilestoneReleaseRequest(BaseModel):
    release_amount: Decimal = Field(..., ge=Decimal("0"), le=Decimal("10000"))
```

### New response models

```python
class MilestoneResponse(BaseModel):
    sequence: int
    description: str
    amount: str
    delivery_timeout_hours: int
    review_timeout_hours: int
    status: str
    delivery_deadline: Optional[str]
    review_deadline: Optional[str]
    release_amount: Optional[str]
    activated_at: Optional[str]
    delivered_at: Optional[str]
    completed_at: Optional[str]

class MilestoneDeliverResponse(BaseModel):
    escrow_id: str
    milestone_sequence: int
    milestone_status: str
    review_deadline: str

class MilestoneReleaseResponse(BaseModel):
    escrow_id: str
    milestone_sequence: int
    milestone_status: str
    released_to_seller: str
    fee: str
    seller_received: str
    refunded_to_buyer: str
    deal_status: str
    deal_total_released: str
    next_milestone: Optional[int]
```

## Implementation Order

1. **DB migration** -- Add `MilestoneStatus` enum, `escrow_milestones` table, new columns on `escrow_deals`, new `partially_completed` status value
2. **Models** -- Add `EscrowMilestone` model, `MilestoneStatus` enum, update `EscrowDeal` with new columns and relationship
3. **Repository** -- Add `EscrowMilestoneRepository` with CRUD + state transitions; extend `EscrowRepository` with milestone-aware queries
4. **Service** -- Extend `EscrowService` with milestone creation, delivery, release, and expiry logic
5. **Schemas** -- Add `MilestoneDefinition`, extend `EscrowCreateRequest`, add milestone response models
6. **Router** -- Add `POST /milestones/{n}/deliver` and `POST /milestones/{n}/release` endpoints; guard existing deliver/release for multi-milestone deals
7. **Auto-resolution** -- Extend `resolve_expired` to handle milestone deadlines
8. **SDK** -- Add `escrow_milestone_deliver` and `escrow_milestone_release`; extend `escrow_create` with `milestones` param
9. **Tests** -- Unit tests for milestone state machine, integration tests for full multi-milestone flow, backward compatibility tests for single-milestone
10. **Publish SDK** 0.3.0

## Edge Cases

### Milestone amount precision
Milestone amounts are `Numeric(20, 12)` matching the deal amount precision. The sum validation uses exact decimal comparison (no floating point).

### Race condition: auto-resolution vs. manual release
Same pattern as current implementation -- `get_by_id_for_update` row lock on the milestone row. Auto-resolution checks status under lock and skips if already resolved.

### Buyer releases 0 for a milestone
Allowed. Milestone amount fully refunded to buyer. Milestone status = COMPLETED. Next milestone activates. Trust impact deferred to deal completion.

### All milestones release 0
Deal completes with `total_released = 0`. Trust impact: seller -2 (same as current full-refund).

### Seller accepts, then milestone #1 delivery timeout
Milestone #1 -> EXPIRED. All remaining milestones -> CANCELLED. Full `deal.amount` refunded to buyer (nothing was released yet). Deal -> EXPIRED. Seller trust: -3.

### Milestone #2 delivery timeout after milestone #1 completed
Milestone #2 -> EXPIRED. Milestone #3+ -> CANCELLED. Refund: `deal.amount - deal.total_released` returned to buyer. Deal -> PARTIALLY_COMPLETED. Seller trust: -3.

## Out of Scope

- Milestone reordering after creation
- Adding/removing milestones after creation
- Per-milestone dispute resolution
- Milestone-level trust scoring
- Non-sequential milestone execution (parallel milestones)
- Milestone templates / presets
