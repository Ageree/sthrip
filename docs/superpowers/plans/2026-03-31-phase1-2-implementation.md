# Sthrip Phase 1-2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform Sthrip from a 0.1%-fee custodial hub into a 1%-fee production platform with spending policies, webhook management, encrypted messaging, ZK reputation, and opt-in multisig escrow.

**Architecture:** 8 independent features across 2 phases. Phase 1 (Tasks 1-5) focuses on SDK hardening and monetization. Phase 2 (Tasks 6-10) adds real anonymity and trust-minimization. Each task produces a working, testable increment. All DB changes via Alembic with IF NOT EXISTS/IF EXISTS for idempotency.

**Tech Stack:** Python 3.9, FastAPI, SQLAlchemy 2.0, Redis, Pydantic v2, PyNaCl, zksk, standardwebhooks

**Spec:** `docs/superpowers/specs/2026-03-31-phase1-2-design.md`

---

## File Map

### New Files
| File | Responsibility |
|---|---|
| `sthrip/services/spending_policy_service.py` | Spending policy enforcement (Redis Lua + fnmatch) |
| `sthrip/db/spending_policy_repo.py` | SpendingPolicy CRUD |
| `api/routers/spending_policy.py` | PUT/GET spending-policy, GET spending-status |
| `sthrip/db/webhook_endpoint_repo.py` | WebhookEndpoint CRUD |
| `api/routers/webhook_endpoints.py` | Webhook registration CRUD + rotation + test |
| `sthrip/services/messaging_service.py` | E2E encrypted message relay |
| `api/routers/messages.py` | Send/inbox/public-key endpoints |
| `sthrip/services/zk_reputation_service.py` | Pedersen commitments + zksk range proofs |
| `api/routers/reputation.py` | Generate/verify ZK reputation proofs |
| `sthrip/services/pow_service.py` | Hashcash-style PoW for registration |
| `sthrip/services/multisig_coordinator.py` | 2-of-3 multisig escrow round management |
| `api/routers/multisig_escrow.py` | Multisig-specific escrow endpoints |
| `tests/test_spending_policy.py` | Spending policy tests |
| `tests/test_webhook_endpoints.py` | Webhook registration tests |
| `tests/test_messaging.py` | Encrypted messaging tests |
| `tests/test_zk_reputation.py` | ZK proof tests |
| `tests/test_pow.py` | PoW tests |
| `tests/test_multisig_escrow.py` | Multisig escrow tests |

### Modified Files
| File | Change |
|---|---|
| `sthrip/services/fee_collector.py` | Flat 1%, remove tier discounts |
| `sthrip/services/escrow_service.py` | Flat 1%, remove tier discounts |
| `sthrip/db/models.py` | New models + new columns on Agent |
| `api/routers/payments.py` | Add spending policy check |
| `api/routers/escrow.py` | Add spending policy check + mode param |
| `api/routers/agents.py` | Add PoW challenge/verify |
| `api/main_v2.py` | Re-enable openapi_url, include new routers |
| `sthrip/services/webhook_service.py` | Fan-out + Standard Webhooks signing |
| `sdk/sthrip/client.py` | Spending policy params, would_exceed(), messaging, PoW |
| `requirements.txt` | Add standardwebhooks, PyNaCl, zksk |

---

## PHASE 1: SDK Hardening & Monetization

### Task 1: Fee Model — Flat 1% Everywhere

**Files:**
- Modify: `sthrip/services/fee_collector.py`
- Modify: `sthrip/services/escrow_service.py`
- Test: `tests/test_fee_model_1pct.py`

- [ ] **Step 1: Write failing test for flat 1% hub routing fee**

```python
# tests/test_fee_model_1pct.py
from decimal import Decimal
import pytest
from sthrip.services.fee_collector import FeeCollector, FeeType, DEFAULT_FEES


def test_hub_routing_fee_is_1_percent():
    collector = FeeCollector()
    result = collector.calculate_hub_routing_fee(amount=Decimal("10.0"))
    assert result["fee_percent"] == Decimal("0.01")
    assert result["fee_amount"] == Decimal("0.1")


def test_hub_routing_fee_no_tier_discount_for_premium():
    collector = FeeCollector()
    result = collector.calculate_hub_routing_fee(
        amount=Decimal("10.0"), from_agent_tier="premium"
    )
    # No discount — still 1%
    assert result["fee_percent"] == Decimal("0.01")
    assert result["fee_amount"] == Decimal("0.1")


def test_hub_routing_fee_no_tier_discount_for_verified():
    collector = FeeCollector()
    result = collector.calculate_hub_routing_fee(
        amount=Decimal("10.0"), from_agent_tier="verified"
    )
    assert result["fee_percent"] == Decimal("0.01")


def test_escrow_fee_is_1_percent():
    collector = FeeCollector()
    result = collector.calculate_escrow_fee(amount=Decimal("10.0"))
    assert result["fee_percent"] == Decimal("0.01")
    assert result["fee_amount"] == Decimal("0.1")
    assert result["seller_receives"] == Decimal("9.9")


def test_escrow_fee_no_tier_discount():
    collector = FeeCollector()
    result = collector.calculate_escrow_fee(
        amount=Decimal("10.0"), from_agent_tier="premium"
    )
    assert result["fee_percent"] == Decimal("0.01")


def test_default_fees_config():
    assert DEFAULT_FEES[FeeType.HUB_ROUTING].percent == Decimal("0.01")
    assert DEFAULT_FEES[FeeType.ESCROW].percent == Decimal("0.01")
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `cd sthrip && python -m pytest tests/test_fee_model_1pct.py -v`
Expected: FAIL — current fee is 0.001 (0.1%), tests expect 0.01 (1%)

- [ ] **Step 3: Update fee_collector.py — flat 1%, no discounts**

In `sthrip/services/fee_collector.py`:

1. Change `DEFAULT_FEES[FeeType.HUB_ROUTING].percent` from `Decimal("0.001")` to `Decimal("0.01")`
2. Change `DEFAULT_FEES[FeeType.ESCROW].percent` from `Decimal("0.001")` to `Decimal("0.01")`
3. Change `DEFAULT_FEES[FeeType.CROSS_CHAIN].percent` from `Decimal("0.005")` to `Decimal("0.01")`
4. In `calculate_hub_routing_fee()`: remove the tier discount block (lines 114-117) and the urgency premium block (lines 119-120). Keep the `from_agent_tier` parameter for backward compat but ignore it.
5. In `calculate_escrow_fee()`: remove the tier discount block (lines 150-153). Keep `from_agent_tier` parameter for backward compat but ignore it.

- [ ] **Step 4: Update escrow_service.py — flat 1%, no tier multipliers**

In `sthrip/services/escrow_service.py`:

1. Change `_DEFAULT_FEE_PERCENT = Decimal("0.001")` to `_DEFAULT_FEE_PERCENT = Decimal("0.01")`
2. Remove `_TIER_FEE_MULTIPLIERS` dict entirely
3. In every function that references `_TIER_FEE_MULTIPLIERS`: use `_DEFAULT_FEE_PERCENT` directly, ignoring buyer tier

- [ ] **Step 5: Run tests — expect PASS**

Run: `cd sthrip && python -m pytest tests/test_fee_model_1pct.py -v`
Expected: all 6 tests PASS

- [ ] **Step 6: Run existing test suite to check for regressions**

Run: `cd sthrip && python -m pytest tests/ -x --timeout=60 -q`
Expected: Some existing fee tests may fail because they expected 0.1%. Update those tests to expect 1%.

- [ ] **Step 7: Commit**

```bash
cd sthrip && git add sthrip/services/fee_collector.py sthrip/services/escrow_service.py tests/test_fee_model_1pct.py
git commit -m "feat: flat 1% fee on all transfers, remove tier discounts"
```

---

### Task 2: Spending Policies — Data Model & Service

**Files:**
- Create: `sthrip/db/spending_policy_repo.py`
- Create: `sthrip/services/spending_policy_service.py`
- Modify: `sthrip/db/models.py`
- Test: `tests/test_spending_policy.py`

- [ ] **Step 1: Add SpendingPolicy model to models.py**

Add after the `AgentReputation` class in `sthrip/db/models.py`:

```python
class SpendingPolicy(Base):
    """Operator-defined spending limits for an agent."""
    __tablename__ = "spending_policies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    max_per_tx = Column(Numeric(20, 8), nullable=True)
    max_per_session = Column(Numeric(20, 8), nullable=True)
    daily_limit = Column(Numeric(20, 8), nullable=True)
    allowed_agents = Column(JSON, nullable=True)   # ["research-*", "data-*"]
    blocked_agents = Column(JSON, nullable=True)    # ["spam-*"]
    require_escrow_above = Column(Numeric(20, 8), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    agent = relationship("Agent", backref="spending_policy", uselist=False)
```

- [ ] **Step 2: Create Alembic migration**

Run: `cd sthrip && alembic revision --autogenerate -m "add_spending_policies_table"`

Verify the migration uses `IF NOT EXISTS` for the table and indexes.

- [ ] **Step 3: Write failing test for spending policy repo**

```python
# tests/test_spending_policy.py
from decimal import Decimal
from uuid import uuid4
import pytest
from sthrip.db.spending_policy_repo import SpendingPolicyRepository
from sthrip.db.models import SpendingPolicy


def test_upsert_creates_new_policy(db_session, test_agent):
    repo = SpendingPolicyRepository(db_session)
    policy = repo.upsert(
        agent_id=test_agent.id,
        max_per_tx=Decimal("0.5"),
        daily_limit=Decimal("5.0"),
        allowed_agents=["research-*"],
    )
    assert policy.max_per_tx == Decimal("0.5")
    assert policy.daily_limit == Decimal("5.0")
    assert policy.allowed_agents == ["research-*"]


def test_upsert_updates_existing_policy(db_session, test_agent):
    repo = SpendingPolicyRepository(db_session)
    repo.upsert(agent_id=test_agent.id, max_per_tx=Decimal("0.5"))
    updated = repo.upsert(agent_id=test_agent.id, max_per_tx=Decimal("1.0"))
    assert updated.max_per_tx == Decimal("1.0")


def test_get_by_agent_id(db_session, test_agent):
    repo = SpendingPolicyRepository(db_session)
    repo.upsert(agent_id=test_agent.id, daily_limit=Decimal("10.0"))
    policy = repo.get_by_agent_id(test_agent.id)
    assert policy is not None
    assert policy.daily_limit == Decimal("10.0")


def test_get_by_agent_id_returns_none(db_session):
    repo = SpendingPolicyRepository(db_session)
    assert repo.get_by_agent_id(uuid4()) is None
```

- [ ] **Step 4: Run tests — expect FAIL**

Run: `cd sthrip && python -m pytest tests/test_spending_policy.py -v`
Expected: FAIL — `spending_policy_repo` module doesn't exist

- [ ] **Step 5: Implement spending_policy_repo.py**

```python
# sthrip/db/spending_policy_repo.py
"""SpendingPolicy CRUD repository."""

from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import SpendingPolicy


class SpendingPolicyRepository:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_by_agent_id(self, agent_id: UUID) -> Optional[SpendingPolicy]:
        return (
            self._db.query(SpendingPolicy)
            .filter(SpendingPolicy.agent_id == agent_id)
            .first()
        )

    def upsert(
        self,
        agent_id: UUID,
        max_per_tx: Optional[Decimal] = None,
        max_per_session: Optional[Decimal] = None,
        daily_limit: Optional[Decimal] = None,
        allowed_agents: Optional[List[str]] = None,
        blocked_agents: Optional[List[str]] = None,
        require_escrow_above: Optional[Decimal] = None,
    ) -> SpendingPolicy:
        policy = self.get_by_agent_id(agent_id)
        if policy is None:
            policy = SpendingPolicy(agent_id=agent_id)
            self._db.add(policy)

        if max_per_tx is not None:
            policy.max_per_tx = max_per_tx
        if max_per_session is not None:
            policy.max_per_session = max_per_session
        if daily_limit is not None:
            policy.daily_limit = daily_limit
        if allowed_agents is not None:
            policy.allowed_agents = allowed_agents
        if blocked_agents is not None:
            policy.blocked_agents = blocked_agents
        if require_escrow_above is not None:
            policy.require_escrow_above = require_escrow_above

        self._db.flush()
        return policy
```

- [ ] **Step 6: Run tests — expect PASS**

Run: `cd sthrip && python -m pytest tests/test_spending_policy.py -v`
Expected: PASS

- [ ] **Step 7: Write failing test for spending policy service (validation)**

```python
# tests/test_spending_policy.py (append)
from unittest.mock import MagicMock, patch
from sthrip.services.spending_policy_service import SpendingPolicyService, PolicyViolation


def test_validate_max_per_tx_rejects():
    policy = SpendingPolicy(max_per_tx=Decimal("0.5"))
    svc = SpendingPolicyService(redis_client=None)
    with pytest.raises(PolicyViolation, match="max_per_tx"):
        svc.validate(
            policy=policy,
            amount=Decimal("1.0"),
            recipient_name="any-agent",
            session_id="sess-1",
        )


def test_validate_max_per_tx_passes():
    policy = SpendingPolicy(max_per_tx=Decimal("0.5"))
    svc = SpendingPolicyService(redis_client=None)
    svc.validate(
        policy=policy,
        amount=Decimal("0.3"),
        recipient_name="any-agent",
        session_id="sess-1",
    )  # should not raise


def test_validate_allowed_agents_rejects():
    policy = SpendingPolicy(allowed_agents=["research-*", "data-*"])
    svc = SpendingPolicyService(redis_client=None)
    with pytest.raises(PolicyViolation, match="allowed_agents"):
        svc.validate(
            policy=policy,
            amount=Decimal("0.1"),
            recipient_name="spam-bot",
            session_id="sess-1",
        )


def test_validate_allowed_agents_passes_glob():
    policy = SpendingPolicy(allowed_agents=["research-*"])
    svc = SpendingPolicyService(redis_client=None)
    svc.validate(
        policy=policy,
        amount=Decimal("0.1"),
        recipient_name="research-bot-42",
        session_id="sess-1",
    )  # should not raise


def test_validate_blocked_agents_rejects():
    policy = SpendingPolicy(blocked_agents=["spam-*"])
    svc = SpendingPolicyService(redis_client=None)
    with pytest.raises(PolicyViolation, match="blocked_agents"):
        svc.validate(
            policy=policy,
            amount=Decimal("0.1"),
            recipient_name="spam-bot",
            session_id="sess-1",
        )


def test_validate_require_escrow_above():
    policy = SpendingPolicy(require_escrow_above=Decimal("1.0"))
    svc = SpendingPolicyService(redis_client=None)
    with pytest.raises(PolicyViolation, match="require_escrow_above"):
        svc.validate(
            policy=policy,
            amount=Decimal("2.0"),
            recipient_name="any-agent",
            session_id="sess-1",
            is_escrow=False,
        )
```

- [ ] **Step 8: Run tests — expect FAIL**

Run: `cd sthrip && python -m pytest tests/test_spending_policy.py::test_validate_max_per_tx_rejects -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 9: Implement spending_policy_service.py**

```python
# sthrip/services/spending_policy_service.py
"""Server-side spending policy enforcement."""

import fnmatch
import logging
import time
from decimal import Decimal
from typing import Optional

from sthrip.db.models import SpendingPolicy

logger = logging.getLogger("sthrip.spending_policy")

_DAILY_WINDOW = 86400  # 24 hours in seconds
_SESSION_TTL = 86400

# Lua script for atomic daily-limit check-and-spend
_DAILY_LIMIT_LUA = """
local key = KEYS[1]
local window_start = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local amount = tonumber(ARGV[3])
local limit = tonumber(ARGV[4])
local tx_id = ARGV[5]

redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
local entries = redis.call('ZRANGEBYSCORE', key, window_start, '+inf', 'WITHSCORES')
local total = 0
for i = 2, #entries, 2 do total = total + tonumber(entries[i]) end
if total + amount > limit then return 0 end
redis.call('ZADD', key, amount, tx_id)
redis.call('EXPIRE', key, 86410)
return 1
"""

# Lua script for atomic session-limit check-and-spend
_SESSION_LIMIT_LUA = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local current = tonumber(redis.call('GET', key) or '0')
if current + amount > limit then return 0 end
redis.call('INCRBYFLOAT', key, ARGV[1])
redis.call('EXPIRE', key, ttl)
return 1
"""


class PolicyViolation(Exception):
    """Raised when a payment violates spending policy."""

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


class SpendingPolicyService:
    def __init__(self, redis_client) -> None:
        self._redis = redis_client
        self._daily_script = None
        self._session_script = None

    def _get_daily_script(self):
        if self._daily_script is None and self._redis is not None:
            self._daily_script = self._redis.register_script(_DAILY_LIMIT_LUA)
        return self._daily_script

    def _get_session_script(self):
        if self._session_script is None and self._redis is not None:
            self._session_script = self._redis.register_script(_SESSION_LIMIT_LUA)
        return self._session_script

    def validate(
        self,
        policy: SpendingPolicy,
        amount: Decimal,
        recipient_name: str,
        session_id: str,
        is_escrow: bool = False,
        tx_id: Optional[str] = None,
    ) -> None:
        """Validate payment against spending policy. Raises PolicyViolation on failure."""
        if not policy.is_active:
            return

        # 1. max_per_tx
        if policy.max_per_tx is not None and amount > policy.max_per_tx:
            raise PolicyViolation(
                "max_per_tx",
                f"Amount {amount} exceeds max per transaction {policy.max_per_tx}",
            )

        # 2. allowed_agents
        if policy.allowed_agents:
            if not any(fnmatch.fnmatch(recipient_name, pat) for pat in policy.allowed_agents):
                raise PolicyViolation(
                    "allowed_agents",
                    f"Recipient '{recipient_name}' not in allowed agents list",
                )

        # 3. blocked_agents
        if policy.blocked_agents:
            if any(fnmatch.fnmatch(recipient_name, pat) for pat in policy.blocked_agents):
                raise PolicyViolation(
                    "blocked_agents",
                    f"Recipient '{recipient_name}' is blocked",
                )

        # 4. daily_limit (Redis)
        if policy.daily_limit is not None and self._redis is not None:
            now = time.time()
            window_start = now - _DAILY_WINDOW
            result = self._get_daily_script()(
                keys=[f"spending:{policy.agent_id}:daily"],
                args=[
                    str(window_start),
                    str(now),
                    str(float(amount)),
                    str(float(policy.daily_limit)),
                    tx_id or f"tx_{now}",
                ],
            )
            if result == 0:
                raise PolicyViolation(
                    "daily_limit",
                    f"Would exceed daily limit of {policy.daily_limit}",
                )

        # 5. max_per_session (Redis)
        if policy.max_per_session is not None and self._redis is not None:
            result = self._get_session_script()(
                keys=[f"spending:{policy.agent_id}:session:{session_id}"],
                args=[
                    str(float(amount)),
                    str(float(policy.max_per_session)),
                    str(_SESSION_TTL),
                ],
            )
            if result == 0:
                raise PolicyViolation(
                    "max_per_session",
                    f"Would exceed session limit of {policy.max_per_session}",
                )

        # 6. require_escrow_above
        if (
            policy.require_escrow_above is not None
            and amount > policy.require_escrow_above
            and not is_escrow
        ):
            raise PolicyViolation(
                "require_escrow_above",
                f"Amount {amount} requires escrow (threshold: {policy.require_escrow_above})",
            )
```

- [ ] **Step 10: Run tests — expect PASS**

Run: `cd sthrip && python -m pytest tests/test_spending_policy.py -v`
Expected: all tests PASS

- [ ] **Step 11: Commit**

```bash
cd sthrip && git add sthrip/db/models.py sthrip/db/spending_policy_repo.py sthrip/services/spending_policy_service.py tests/test_spending_policy.py
git commit -m "feat: spending policy model, repo, and enforcement service"
```

---

### Task 3: Spending Policies — API Endpoints & Payment Integration

**Files:**
- Create: `api/routers/spending_policy.py`
- Modify: `api/routers/payments.py`
- Modify: `api/routers/escrow.py`
- Modify: `api/main_v2.py`
- Modify: `api/schemas.py`
- Test: `tests/test_spending_policy_api.py`

- [ ] **Step 1: Add Pydantic schemas to api/schemas.py**

```python
# Append to api/schemas.py
class SpendingPolicyRequest(BaseModel):
    max_per_tx: Optional[Decimal] = None
    max_per_session: Optional[Decimal] = None
    daily_limit: Optional[Decimal] = None
    allowed_agents: Optional[List[str]] = None
    blocked_agents: Optional[List[str]] = None
    require_escrow_above: Optional[Decimal] = None

class SpendingPolicyResponse(BaseModel):
    max_per_tx: Optional[Decimal] = None
    max_per_session: Optional[Decimal] = None
    daily_limit: Optional[Decimal] = None
    allowed_agents: Optional[List[str]] = None
    blocked_agents: Optional[List[str]] = None
    require_escrow_above: Optional[Decimal] = None
    is_active: bool = True

class SpendingStatusResponse(BaseModel):
    daily_spent: Decimal = Decimal("0")
    daily_limit: Optional[Decimal] = None
    daily_remaining: Optional[Decimal] = None
    session_spent: Decimal = Decimal("0")
    session_limit: Optional[Decimal] = None
    session_remaining: Optional[Decimal] = None
```

- [ ] **Step 2: Create api/routers/spending_policy.py**

```python
# api/routers/spending_policy.py
"""Spending policy management endpoints."""

import logging
from fastapi import APIRouter, Depends, Header
from typing import Optional

from sthrip.db.database import get_db
from sthrip.db.models import Agent
from sthrip.db.spending_policy_repo import SpendingPolicyRepository
from api.deps import get_current_agent
from api.schemas import SpendingPolicyRequest, SpendingPolicyResponse, SpendingStatusResponse

logger = logging.getLogger("sthrip")

router = APIRouter(prefix="/v2/me", tags=["spending-policy"])


@router.put("/spending-policy", response_model=SpendingPolicyResponse)
async def set_spending_policy(
    req: SpendingPolicyRequest,
    agent: Agent = Depends(get_current_agent),
):
    with get_db() as db:
        repo = SpendingPolicyRepository(db)
        policy = repo.upsert(
            agent_id=agent.id,
            max_per_tx=req.max_per_tx,
            max_per_session=req.max_per_session,
            daily_limit=req.daily_limit,
            allowed_agents=req.allowed_agents,
            blocked_agents=req.blocked_agents,
            require_escrow_above=req.require_escrow_above,
        )
        return SpendingPolicyResponse(
            max_per_tx=policy.max_per_tx,
            max_per_session=policy.max_per_session,
            daily_limit=policy.daily_limit,
            allowed_agents=policy.allowed_agents,
            blocked_agents=policy.blocked_agents,
            require_escrow_above=policy.require_escrow_above,
            is_active=policy.is_active,
        )


@router.get("/spending-policy", response_model=SpendingPolicyResponse)
async def get_spending_policy(agent: Agent = Depends(get_current_agent)):
    with get_db() as db:
        repo = SpendingPolicyRepository(db)
        policy = repo.get_by_agent_id(agent.id)
        if policy is None:
            return SpendingPolicyResponse()
        return SpendingPolicyResponse(
            max_per_tx=policy.max_per_tx,
            max_per_session=policy.max_per_session,
            daily_limit=policy.daily_limit,
            allowed_agents=policy.allowed_agents,
            blocked_agents=policy.blocked_agents,
            require_escrow_above=policy.require_escrow_above,
            is_active=policy.is_active,
        )
```

- [ ] **Step 3: Integrate policy check into payments router**

In `api/routers/payments.py`, inside `send_hub_routed_payment()`, add after the `_check_not_self_payment` call (around line 205):

```python
# Spending policy enforcement
from sthrip.db.spending_policy_repo import SpendingPolicyRepository
from sthrip.services.spending_policy_service import SpendingPolicyService, PolicyViolation

policy_repo = SpendingPolicyRepository(db)
policy = policy_repo.get_by_agent_id(agent.id)
if policy is not None:
    from sthrip.services.rate_limiter import get_rate_limiter
    redis_client = getattr(get_rate_limiter(), '_redis', None)
    policy_svc = SpendingPolicyService(redis_client=redis_client)
    session_id = request.headers.get("x-sthrip-session", "default")
    try:
        policy_svc.validate(
            policy=policy,
            amount=amount,
            recipient_name=req.to_agent_name,
            session_id=session_id,
            is_escrow=False,
            tx_id=idempotency_key,
        )
    except PolicyViolation as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "spending_policy_violation", "field": e.field, "message": e.message},
        )
```

Add `request: Request` to the function parameters (from `fastapi import Request`).

- [ ] **Step 4: Include router in main_v2.py**

In `api/main_v2.py`, add the import and `app.include_router(spending_policy_router)`.

- [ ] **Step 5: Write integration test**

```python
# tests/test_spending_policy_api.py
import pytest
from decimal import Decimal


def test_set_and_get_spending_policy(client, auth_headers):
    resp = client.put("/v2/me/spending-policy", json={
        "max_per_tx": "0.5",
        "daily_limit": "5.0",
        "allowed_agents": ["research-*"],
    }, headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["max_per_tx"] == "0.5"

    resp = client.get("/v2/me/spending-policy", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["daily_limit"] == "5.0"


def test_payment_blocked_by_policy(client, auth_headers, funded_agent):
    # Set restrictive policy
    client.put("/v2/me/spending-policy", json={
        "max_per_tx": "0.01",
    }, headers=auth_headers)

    # Try to pay more than allowed
    resp = client.post("/v2/payments/hub-routing", json={
        "to_agent_name": "recipient-agent",
        "amount": "0.5",
    }, headers=auth_headers)
    assert resp.status_code == 400
    assert resp.json()["detail"]["field"] == "max_per_tx"
```

- [ ] **Step 6: Run tests — expect PASS**

Run: `cd sthrip && python -m pytest tests/test_spending_policy_api.py -v`

- [ ] **Step 7: Commit**

```bash
cd sthrip && git add api/routers/spending_policy.py api/schemas.py api/routers/payments.py api/main_v2.py tests/test_spending_policy_api.py
git commit -m "feat: spending policy API endpoints and payment integration"
```

---

### Task 4: Webhook Registration API

**Files:**
- Create: `sthrip/db/webhook_endpoint_repo.py`
- Create: `api/routers/webhook_endpoints.py`
- Modify: `sthrip/db/models.py`
- Modify: `sthrip/services/webhook_service.py`
- Modify: `api/main_v2.py`
- Modify: `requirements.txt`
- Test: `tests/test_webhook_endpoints.py`

- [ ] **Step 1: Add WebhookEndpoint model to models.py**

```python
class WebhookEndpoint(Base):
    """Self-service webhook endpoint registration."""
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    url = Column(String(2048), nullable=False)
    description = Column(String(256), nullable=True)
    secret_encrypted = Column(Text, nullable=False)  # Fernet-encrypted signing secret
    event_filters = Column(JSON, nullable=True)  # ["payment.*", "escrow.*"] or null=all
    is_active = Column(Boolean, default=True)
    failure_count = Column(Integer, default=0)
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())

    agent = relationship("Agent", backref="webhook_endpoints")

    __table_args__ = (
        UniqueConstraint("agent_id", "url", name="uq_agent_webhook_url"),
    )
```

- [ ] **Step 2: Add `standardwebhooks` to requirements.txt**

```
standardwebhooks
```

- [ ] **Step 3: Write failing test for webhook endpoint CRUD**

```python
# tests/test_webhook_endpoints.py
import pytest


def test_register_webhook(client, auth_headers):
    resp = client.post("/v2/webhooks", json={
        "url": "https://example.com/webhook",
        "event_filters": ["payment.received", "escrow.*"],
        "description": "My test webhook",
    }, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["url"] == "https://example.com/webhook"
    assert "secret" in data  # secret returned only once
    assert data["secret"].startswith("whsec_")


def test_list_webhooks(client, auth_headers):
    client.post("/v2/webhooks", json={"url": "https://a.com/wh"}, headers=auth_headers)
    client.post("/v2/webhooks", json={"url": "https://b.com/wh"}, headers=auth_headers)
    resp = client.get("/v2/webhooks", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_delete_webhook(client, auth_headers):
    resp = client.post("/v2/webhooks", json={"url": "https://del.com/wh"}, headers=auth_headers)
    wh_id = resp.json()["id"]
    resp = client.delete(f"/v2/webhooks/{wh_id}", headers=auth_headers)
    assert resp.status_code == 200


def test_max_10_webhooks(client, auth_headers):
    for i in range(10):
        resp = client.post("/v2/webhooks", json={"url": f"https://ex{i}.com/wh"}, headers=auth_headers)
        assert resp.status_code == 201
    resp = client.post("/v2/webhooks", json={"url": "https://ex10.com/wh"}, headers=auth_headers)
    assert resp.status_code == 400


def test_rotate_secret(client, auth_headers):
    resp = client.post("/v2/webhooks", json={"url": "https://rot.com/wh"}, headers=auth_headers)
    wh_id = resp.json()["id"]
    old_secret = resp.json()["secret"]
    resp = client.post(f"/v2/webhooks/{wh_id}/rotate", headers=auth_headers)
    assert resp.status_code == 200
    new_secret = resp.json()["secret"]
    assert new_secret != old_secret
    assert new_secret.startswith("whsec_")
```

- [ ] **Step 4: Implement webhook_endpoint_repo.py**

Create `sthrip/db/webhook_endpoint_repo.py` with `create()`, `list_by_agent()`, `get_by_id()`, `update()`, `delete()`, `count_by_agent()` methods. Use Fernet from `cryptography` to encrypt/decrypt secrets (key from `settings.webhook_encryption_key`).

- [ ] **Step 5: Implement api/routers/webhook_endpoints.py**

Create the router with POST/GET/PATCH/DELETE/rotate/test endpoints. Generate secrets as `whsec_` + base64-encoded 32 random bytes. Enforce max 10 endpoints per agent.

- [ ] **Step 6: Include router in main_v2.py**

- [ ] **Step 7: Create Alembic migration**

Run: `cd sthrip && alembic revision --autogenerate -m "add_webhook_endpoints_table"`

- [ ] **Step 8: Run tests — expect PASS**

Run: `cd sthrip && python -m pytest tests/test_webhook_endpoints.py -v`

- [ ] **Step 9: Commit**

```bash
cd sthrip && git add sthrip/db/models.py sthrip/db/webhook_endpoint_repo.py api/routers/webhook_endpoints.py sthrip/services/webhook_service.py api/main_v2.py requirements.txt tests/test_webhook_endpoints.py
git commit -m "feat: webhook registration API with Standard Webhooks signing"
```

---

### Task 5: OpenAPI Spec Re-enable

**Files:**
- Modify: `api/main_v2.py`

- [ ] **Step 1: Re-enable OpenAPI URL**

In `api/main_v2.py`, find the line `openapi_url=None` (around line 399) and change to `openapi_url="/openapi.json"`.

- [ ] **Step 2: Verify spec is accessible**

Run: `cd sthrip && python -c "from api.main_v2 import create_app; import json; app=create_app(); print(len(json.dumps(app.openapi())))"` 
Expected: prints a number (the spec size in chars)

- [ ] **Step 3: Commit**

```bash
cd sthrip && git add api/main_v2.py
git commit -m "feat: re-enable OpenAPI spec at /openapi.json"
```

---

## PHASE 2: Anonymity & Trust

### Task 6: Encrypted Agent Messaging

**Files:**
- Create: `sthrip/services/messaging_service.py`
- Create: `api/routers/messages.py`
- Modify: `sthrip/db/models.py`
- Modify: `api/main_v2.py`
- Modify: `requirements.txt`
- Test: `tests/test_messaging.py`

- [ ] **Step 1: Add PyNaCl to requirements.txt**

```
PyNaCl>=1.6.0
```

- [ ] **Step 2: Add encryption_public_key column and MessageRelay model**

In `sthrip/db/models.py`, add to `Agent`:
```python
encryption_public_key = Column(Text, nullable=True)  # base64-encoded Curve25519 public key
```

Add new model:
```python
class MessageRelay(Base):
    """Ephemeral encrypted message relay. Hub stores ciphertext temporarily, never plaintext."""
    __tablename__ = "message_relays"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    to_agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    payment_id = Column(String(64), nullable=True)
    ciphertext = Column(Text, nullable=False)  # base64-encoded NaCl Box ciphertext
    nonce = Column(String(64), nullable=False)  # base64-encoded nonce
    sender_public_key = Column(String(64), nullable=False)  # base64-encoded
    size_bytes = Column(Integer, nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
```

- [ ] **Step 3: Write failing test**

```python
# tests/test_messaging.py
import base64
import pytest
from nacl.public import PrivateKey


def test_register_encryption_key(client, auth_headers):
    sk = PrivateKey.generate()
    pk_b64 = base64.b64encode(bytes(sk.public_key)).decode()
    resp = client.put("/v2/me/encryption-key", json={"public_key": pk_b64}, headers=auth_headers)
    assert resp.status_code == 200


def test_send_and_receive_message(client, auth_headers_a, auth_headers_b, agent_a, agent_b):
    # Both agents register encryption keys
    sk_a = PrivateKey.generate()
    sk_b = PrivateKey.generate()
    pk_a_b64 = base64.b64encode(bytes(sk_a.public_key)).decode()
    pk_b_b64 = base64.b64encode(bytes(sk_b.public_key)).decode()

    client.put("/v2/me/encryption-key", json={"public_key": pk_a_b64}, headers=auth_headers_a)
    client.put("/v2/me/encryption-key", json={"public_key": pk_b_b64}, headers=auth_headers_b)

    # Agent A encrypts and sends
    from nacl.public import Box
    box = Box(sk_a, sk_b.public_key)
    plaintext = b"delivery instructions for order 42"
    encrypted = box.encrypt(plaintext)
    ct_b64 = base64.b64encode(encrypted.ciphertext).decode()
    nonce_b64 = base64.b64encode(encrypted.nonce).decode()

    resp = client.post("/v2/messages/send", json={
        "to_agent_id": str(agent_b.id),
        "ciphertext": ct_b64,
        "nonce": nonce_b64,
        "sender_public_key": pk_a_b64,
    }, headers=auth_headers_a)
    assert resp.status_code == 201

    # Agent B retrieves and decrypts
    resp = client.get("/v2/messages/inbox", headers=auth_headers_b)
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 1

    msg = messages[0]
    ct = base64.b64decode(msg["ciphertext"])
    nonce = base64.b64decode(msg["nonce"])
    sender_pk_bytes = base64.b64decode(msg["sender_public_key"])

    from nacl.public import PublicKey
    box_b = Box(sk_b, PublicKey(sender_pk_bytes))
    decrypted = box_b.decrypt(ct, nonce)
    assert decrypted == plaintext


def test_message_size_limit(client, auth_headers_a, agent_b):
    # 64KB limit
    resp = client.post("/v2/messages/send", json={
        "to_agent_id": str(agent_b.id),
        "ciphertext": base64.b64encode(b"x" * 65537).decode(),
        "nonce": base64.b64encode(b"n" * 24).decode(),
        "sender_public_key": base64.b64encode(b"k" * 32).decode(),
    }, headers=auth_headers_a)
    assert resp.status_code == 400
```

- [ ] **Step 4: Implement messaging_service.py**

```python
# sthrip/services/messaging_service.py
"""E2E encrypted message relay. Hub never sees plaintext."""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from sthrip.db.models import Agent, MessageRelay

logger = logging.getLogger("sthrip.messaging")

_MAX_MESSAGE_SIZE = 65536  # 64 KB
_MAX_PENDING = 100
_MESSAGE_TTL_HOURS = 24


class MessageSizeError(Exception):
    pass


class InboxFullError(Exception):
    pass


class RecipientNotFoundError(Exception):
    pass


class MessagingService:
    def relay_message(
        self,
        db: Session,
        from_agent_id: UUID,
        to_agent_id: UUID,
        ciphertext: str,
        nonce: str,
        sender_public_key: str,
        payment_id: Optional[str] = None,
    ) -> MessageRelay:
        # Size check (base64 decoded)
        import base64
        ct_bytes = base64.b64decode(ciphertext)
        if len(ct_bytes) > _MAX_MESSAGE_SIZE:
            raise MessageSizeError(f"Message exceeds {_MAX_MESSAGE_SIZE} bytes")

        # Recipient exists?
        recipient = db.query(Agent).filter(Agent.id == to_agent_id, Agent.is_active == True).first()
        if not recipient:
            raise RecipientNotFoundError(f"Agent {to_agent_id} not found")

        # Inbox capacity
        pending_count = db.query(MessageRelay).filter(
            MessageRelay.to_agent_id == to_agent_id,
            MessageRelay.delivered_at.is_(None),
        ).count()
        if pending_count >= _MAX_PENDING:
            raise InboxFullError(f"Recipient inbox full ({_MAX_PENDING} messages)")

        msg = MessageRelay(
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            payment_id=payment_id,
            ciphertext=ciphertext,
            nonce=nonce,
            sender_public_key=sender_public_key,
            size_bytes=len(ct_bytes),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=_MESSAGE_TTL_HOURS),
        )
        db.add(msg)
        db.flush()
        return msg

    def get_inbox(self, db: Session, agent_id: UUID) -> List[MessageRelay]:
        now = datetime.now(timezone.utc)
        messages = (
            db.query(MessageRelay)
            .filter(
                MessageRelay.to_agent_id == agent_id,
                MessageRelay.delivered_at.is_(None),
                MessageRelay.expires_at > now,
            )
            .order_by(MessageRelay.created_at.asc())
            .all()
        )
        # Mark as delivered
        for msg in messages:
            msg.delivered_at = now
        db.flush()
        return messages
```

- [ ] **Step 5: Implement api/routers/messages.py**

Create router with `POST /v2/messages/send`, `GET /v2/messages/inbox`, `GET /v2/agents/{id}/public-key`, `PUT /v2/me/encryption-key`.

- [ ] **Step 6: Include router, create migration, run tests**

- [ ] **Step 7: Commit**

```bash
cd sthrip && git add sthrip/services/messaging_service.py api/routers/messages.py sthrip/db/models.py requirements.txt tests/test_messaging.py
git commit -m "feat: E2E encrypted agent messaging via NaCl Box"
```

---

### Task 7: ZK Reputation Proofs

**Files:**
- Create: `sthrip/services/zk_reputation_service.py`
- Create: `api/routers/reputation.py`
- Modify: `sthrip/db/models.py`
- Modify: `api/main_v2.py`
- Modify: `requirements.txt`
- Test: `tests/test_zk_reputation.py`

- [ ] **Step 1: Add zksk to requirements.txt**

```
zksk @ git+https://github.com/spring-epfl/zksk@master
```

- [ ] **Step 2: Add commitment columns to Agent model**

In `sthrip/db/models.py`, add to `AgentReputation`:
```python
reputation_commitment = Column(Text, nullable=True)   # serialized Pedersen commitment
reputation_blinding = Column(Text, nullable=True)      # blinding factor (private, never exposed)
```

- [ ] **Step 3: Write failing test**

```python
# tests/test_zk_reputation.py
import pytest
from sthrip.services.zk_reputation_service import ZKReputationService


def test_generate_and_verify_proof():
    svc = ZKReputationService()
    commitment, blinding = svc.create_commitment(score=75)

    proof = svc.generate_proof(score=75, blinding=blinding, threshold=50)
    assert proof is not None

    is_valid = svc.verify_proof(
        commitment=commitment,
        proof=proof,
        threshold=50,
    )
    assert is_valid is True


def test_proof_fails_for_insufficient_score():
    svc = ZKReputationService()
    commitment, blinding = svc.create_commitment(score=40)

    # Cannot prove score >= 50 when score is 40
    with pytest.raises(Exception):
        svc.generate_proof(score=40, blinding=blinding, threshold=50)


def test_different_thresholds():
    svc = ZKReputationService()
    commitment, blinding = svc.create_commitment(score=60)

    # Can prove >= 50
    proof_50 = svc.generate_proof(score=60, blinding=blinding, threshold=50)
    assert svc.verify_proof(commitment, proof_50, 50) is True

    # Can prove >= 60
    proof_60 = svc.generate_proof(score=60, blinding=blinding, threshold=60)
    assert svc.verify_proof(commitment, proof_60, 60) is True

    # Cannot prove >= 61
    with pytest.raises(Exception):
        svc.generate_proof(score=60, blinding=blinding, threshold=61)
```

- [ ] **Step 4: Implement zk_reputation_service.py**

Use `zksk.Secret`, `zksk.primitives.rangeproof.RangeOnlyStmt`, and Pedersen commitments. Serialize proofs as base64 for API transport.

- [ ] **Step 5: Implement api/routers/reputation.py**

Endpoints: `POST /v2/me/reputation-proof` and `POST /v2/verify-reputation`.

- [ ] **Step 6: Run tests, create migration, commit**

```bash
cd sthrip && git add sthrip/services/zk_reputation_service.py api/routers/reputation.py sthrip/db/models.py requirements.txt tests/test_zk_reputation.py
git commit -m "feat: ZK reputation proofs via zksk Pedersen commitments"
```

---

### Task 8: Sybil Prevention — Proof of Work

**Files:**
- Create: `sthrip/services/pow_service.py`
- Modify: `api/routers/agents.py`
- Modify: `sdk/sthrip/client.py`
- Test: `tests/test_pow.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_pow.py
import pytest
from sthrip.services.pow_service import POWService


def test_create_challenge():
    svc = POWService(difficulty_bits=16)  # easy for tests
    challenge = svc.create_challenge()
    assert challenge["algorithm"] == "sha256"
    assert challenge["difficulty_bits"] == 16
    assert "nonce" in challenge
    assert "expires_at" in challenge


def test_solve_and_verify():
    svc = POWService(difficulty_bits=16)
    challenge = svc.create_challenge()
    solution = svc.solve(challenge)
    assert svc.verify(challenge, solution) is True


def test_verify_rejects_wrong_solution():
    svc = POWService(difficulty_bits=16)
    challenge = svc.create_challenge()
    assert svc.verify(challenge, "wrong_nonce") is False


def test_verify_rejects_expired_challenge():
    from datetime import datetime, timezone, timedelta
    svc = POWService(difficulty_bits=16)
    challenge = svc.create_challenge()
    challenge["expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    solution = svc.solve(challenge)
    assert svc.verify(challenge, solution) is False
```

- [ ] **Step 2: Implement pow_service.py**

```python
# sthrip/services/pow_service.py
"""Hashcash-style proof-of-work for Sybil prevention at registration."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict


class POWService:
    def __init__(self, difficulty_bits: int = 20) -> None:
        self._difficulty_bits = difficulty_bits

    def create_challenge(self) -> Dict:
        nonce = secrets.token_hex(16)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        return {
            "algorithm": "sha256",
            "difficulty_bits": self._difficulty_bits,
            "nonce": nonce,
            "expires_at": expires_at.isoformat(),
        }

    def solve(self, challenge: Dict) -> str:
        prefix = challenge["nonce"]
        target_bits = challenge["difficulty_bits"]
        counter = 0
        while True:
            candidate = f"{prefix}:{counter}"
            digest = hashlib.sha256(candidate.encode()).hexdigest()
            # Check leading zero bits
            bits = bin(int(digest, 16))[2:].zfill(256)
            if bits[:target_bits] == "0" * target_bits:
                return str(counter)
            counter += 1

    def verify(self, challenge: Dict, solution: str) -> bool:
        # Check expiry
        expires_at = datetime.fromisoformat(challenge["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            return False

        candidate = f"{challenge['nonce']}:{solution}"
        digest = hashlib.sha256(candidate.encode()).hexdigest()
        bits = bin(int(digest, 16))[2:].zfill(256)
        return bits[: challenge["difficulty_bits"]] == "0" * challenge["difficulty_bits"]
```

- [ ] **Step 3: Run tests — expect PASS**

Run: `cd sthrip && python -m pytest tests/test_pow.py -v`

- [ ] **Step 4: Integrate into registration endpoint**

In `api/routers/agents.py`, add `POST /v2/agents/register/challenge` endpoint that returns a challenge. Modify `POST /v2/agents/register` to accept and verify `pow_challenge` and `pow_nonce` fields.

- [ ] **Step 5: Update SDK client to auto-solve PoW**

In `sdk/sthrip/client.py`, modify `_auto_register()` to: (1) fetch challenge, (2) solve PoW, (3) submit registration with solution.

- [ ] **Step 6: Commit**

```bash
cd sthrip && git add sthrip/services/pow_service.py api/routers/agents.py sdk/sthrip/client.py tests/test_pow.py
git commit -m "feat: Hashcash PoW for Sybil prevention at registration"
```

---

### Task 9: Dual-Mode Escrow — Multisig Coordinator

**Files:**
- Create: `sthrip/services/multisig_coordinator.py`
- Create: `api/routers/multisig_escrow.py`
- Modify: `sthrip/db/models.py`
- Modify: `api/routers/escrow.py`
- Modify: `api/main_v2.py`
- Test: `tests/test_multisig_escrow.py`

- [ ] **Step 1: Add MultisigEscrow and MultisigRound models**

```python
# In sthrip/db/models.py

class MultisigEscrow(Base):
    """2-of-3 Monero multisig escrow deal."""
    __tablename__ = "multisig_escrows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    escrow_deal_id = Column(UUID(as_uuid=True), ForeignKey("escrow_deals.id"), nullable=False, unique=True)
    multisig_address = Column(String(255), nullable=True)  # Set after setup completes
    buyer_wallet_id = Column(String(255), nullable=True)
    seller_wallet_id = Column(String(255), nullable=True)
    hub_wallet_id = Column(String(255), nullable=True)
    state = Column(String(50), default="setup_round_1")
    # States: setup_round_1, setup_round_2, setup_round_3, funded, active, releasing, completed, cancelled
    fee_collected = Column(Numeric(20, 8), default=Decimal("0"))
    funded_amount = Column(Numeric(20, 8), nullable=True)
    funded_tx_hash = Column(String(255), nullable=True)
    timeout_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())


class MultisigRound(Base):
    """Key exchange round data for multisig setup."""
    __tablename__ = "multisig_rounds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    multisig_escrow_id = Column(UUID(as_uuid=True), ForeignKey("multisig_escrows.id"), nullable=False, index=True)
    round_number = Column(Integer, nullable=False)  # 1, 2, or 3
    participant = Column(String(20), nullable=False)  # "buyer", "seller", "hub"
    multisig_info = Column(Text, nullable=False)  # encrypted multisig_info blob
    created_at = Column(DateTime(timezone=True), default=func.now())

    __table_args__ = (
        UniqueConstraint("multisig_escrow_id", "round_number", "participant",
                         name="uq_multisig_round_participant"),
    )
```

- [ ] **Step 2: Write failing test (mocked wallet RPC)**

```python
# tests/test_multisig_escrow.py
from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest


def test_create_multisig_escrow_collects_fee_upfront(db_session, buyer_agent, seller_agent):
    """Fee is deducted BEFORE funds enter multisig."""
    from sthrip.services.multisig_coordinator import MultisigCoordinator

    mock_wallet = MagicMock()
    mock_wallet.prepare_multisig.return_value = {"multisig_info": "fake_info"}

    coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)
    result = coordinator.create(
        db=db_session,
        buyer_id=buyer_agent.id,
        seller_id=seller_agent.id,
        amount=Decimal("10.0"),
    )
    assert result["fee_collected"] == Decimal("0.1")  # 1% of 10.0
    assert result["funded_amount"] == Decimal("9.9")  # 10.0 - 0.1
    assert result["state"] == "setup_round_1"


def test_multisig_round_progression(db_session, multisig_escrow):
    from sthrip.services.multisig_coordinator import MultisigCoordinator

    mock_wallet = MagicMock()
    mock_wallet.make_multisig.return_value = {"multisig_info": "round2_info"}
    mock_wallet.exchange_multisig_keys.return_value = {"address": "5...multisig_addr"}

    coordinator = MultisigCoordinator(wallet_rpc=mock_wallet)

    # Submit round 1 data for all participants
    coordinator.submit_round(db_session, multisig_escrow.id, "buyer", 1, "buyer_info")
    coordinator.submit_round(db_session, multisig_escrow.id, "seller", 1, "seller_info")
    coordinator.submit_round(db_session, multisig_escrow.id, "hub", 1, "hub_info")

    # After all 3 submit, state advances
    escrow = coordinator.get_state(db_session, multisig_escrow.id)
    assert escrow.state == "setup_round_2"
```

- [ ] **Step 3: Implement multisig_coordinator.py**

Build the `MultisigCoordinator` class with `create()`, `submit_round()`, `get_state()`, `initiate_release()`, `cosign_release()`, `dispute()` methods. Use existing `sthrip/swaps/xmr/wallet.py` for RPC calls. Collect 1% fee upfront via `FeeCollector`.

- [ ] **Step 4: Implement api/routers/multisig_escrow.py**

Endpoints: `POST /v2/escrow/{id}/round`, `GET /v2/escrow/{id}/round`, `POST /v2/escrow/{id}/cosign`, `POST /v2/escrow/{id}/dispute`.

- [ ] **Step 5: Add mode parameter to existing escrow create**

In `api/routers/escrow.py`, add `mode: str = "hub-held"` to the create endpoint schema. If `mode == "multisig"`, delegate to MultisigCoordinator.

- [ ] **Step 6: Create migration, include router, run tests**

- [ ] **Step 7: Commit**

```bash
cd sthrip && git add sthrip/services/multisig_coordinator.py api/routers/multisig_escrow.py sthrip/db/models.py api/routers/escrow.py tests/test_multisig_escrow.py
git commit -m "feat: 2-of-3 Monero multisig escrow with upfront 1% fee"
```

---

### Task 10: SDK Update — Spending Policies, Messaging, PoW

**Files:**
- Modify: `sdk/sthrip/client.py`
- Test: `tests/test_sdk.py`

- [ ] **Step 1: Add spending policy parameters to Sthrip constructor**

In `sdk/sthrip/client.py`, add to `__init__`:
```python
def __init__(
    self,
    api_key=None,
    api_url=None,
    agent_name=None,
    max_per_session=None,
    max_per_tx=None,
    daily_limit=None,
    allowed_agents=None,
    require_escrow_above=None,
):
```

On init, if any policy params are set, call `PUT /v2/me/spending-policy` to sync them server-side. Generate a session UUID and send it as `X-Sthrip-Session` header on all requests.

- [ ] **Step 2: Add `would_exceed()` method**

```python
def would_exceed(self, amount):
    """Client-side pre-flight check against local policy copy."""
    if self._max_per_tx and amount > self._max_per_tx:
        return True
    if self._session_spent + amount > self._max_per_session:
        return True
    return False
```

- [ ] **Step 3: Add messaging methods**

```python
def send_message(self, to_agent, message, payment_id=None):
    """Encrypt and send a message to another agent."""
    # ... fetch recipient public key, encrypt with NaCl Box, POST /v2/messages/send

def get_messages(self):
    """Fetch and decrypt pending messages."""
    # ... GET /v2/messages/inbox, decrypt each with local private key
```

- [ ] **Step 4: Write tests for new SDK methods**

- [ ] **Step 5: Bump SDK version to 0.3.0**

In `sdk/pyproject.toml`, change version to `0.3.0`.

- [ ] **Step 6: Commit**

```bash
cd sthrip && git add sdk/ tests/test_sdk.py
git commit -m "feat: SDK v0.3.0 — spending policies, encrypted messaging, PoW"
```

---

## Post-Implementation

- [ ] **Run full test suite**: `cd sthrip && python -m pytest tests/ --timeout=120 -q`
- [ ] **Update MCP server** (`integrations/sthrip_mcp/`) with new tools: `spending_policy`, `send_message`, `get_messages`, `reputation_proof`
- [ ] **Update /.well-known/agent-payments.json** with new capabilities
- [ ] **Deploy to Railway staging** and verify
- [ ] **Publish sthrip v0.3.0 to PyPI**
