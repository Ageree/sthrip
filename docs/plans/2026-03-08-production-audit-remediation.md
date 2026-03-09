# Production Audit Remediation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all CRITICAL, HIGH, and key MEDIUM issues found in the 2026-03-08 production readiness audit to bring Sthrip to production-grade quality.

**Architecture:** Incremental fixes organized into 4 phases — security hotfixes first (zero new features), then data integrity, then architecture refactor, then coverage/polish. Each phase is independently deployable.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy, PostgreSQL, Redis, Monero wallet RPC, pytest

---

## Phase 1: Security Hotfixes

> Priority: CRITICAL. Deploy immediately after completing.

---

### Task 1: Delete legacy `api/main.py`

**Files:**
- Delete: `api/main.py`

**Why:** Legacy v1 API has no auth, no DB, no rate limiting, leaks `str(e)` in responses, stores agents in a plain `dict`. Could be accidentally deployed.

**Step 1: Verify nothing imports main.py**

Run: `grep -r "from api.main import\|from api import main\|api\.main:app\|api/main:" --include="*.py" --include="*.toml" --include="*.yml" --include="*.yaml" .`
Expected: Only `railway.toml` should reference `api.main_v2:app`. If `api.main` appears anywhere, update those references first.

**Step 2: Delete the file**

```bash
rm api/main.py
```

**Step 3: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All 220 tests pass. No test imports from `api.main`.

**Step 4: Commit**

```bash
git add -A api/main.py
git commit -m "fix: delete legacy api/main.py — no auth, no rate limiting, exception leaking"
```

---

### Task 2: Remove hardcoded credentials from docker-compose.dev.yml

**Files:**
- Modify: `docker-compose.dev.yml`

**Step 1: Replace hardcoded values with env var references**

Replace the entire `docker-compose.dev.yml` content. All credentials must come from environment variables with required markers (`?err`):

```yaml
version: '3.8'

# Development Docker Compose
# Usage: cp .env.example .env.dev && docker-compose -f docker-compose.dev.yml --env-file .env.dev up -d

services:
  postgres:
    image: postgres:15-alpine
    container_name: sthrip-postgres-dev
    environment:
      POSTGRES_USER: sthrip
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env.dev}
      POSTGRES_DB: sthrip
    ports:
      - "5432:5432"
    volumes:
      - postgres_dev_data:/var/lib/postgresql/data
      - ./sthrip/db/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sthrip -d sthrip"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    container_name: sthrip-redis-dev
    ports:
      - "6379:6379"
    command: redis-server --requirepass ${REDIS_PASSWORD:?Set REDIS_PASSWORD in .env.dev}
    volumes:
      - redis_dev_data:/data

  api:
    build:
      context: .
      dockerfile: railway/Dockerfile.railway
    container_name: sthrip-api-dev
    environment:
      DATABASE_URL: postgresql://sthrip:${POSTGRES_PASSWORD}@postgres:5432/sthrip
      REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0
      MONERO_RPC_HOST: ${MONERO_RPC_HOST:-host.docker.internal}
      MONERO_RPC_PORT: ${MONERO_RPC_PORT:-18082}
      MONERO_RPC_USER: ${MONERO_RPC_USER:-}
      MONERO_RPC_PASS: ${MONERO_RPC_PASS:-}
      ADMIN_API_KEY: ${ADMIN_API_KEY:?Set ADMIN_API_KEY in .env.dev}
      ENVIRONMENT: dev
      LOG_LEVEL: DEBUG
      PORT: 8000
    ports:
      - "8000:8000"
    volumes:
      - .:/app
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
    command: uvicorn api.main_v2:app --host 0.0.0.0 --port 8000 --reload

volumes:
  postgres_dev_data:
  redis_dev_data:
```

**Step 2: Fix deploy/.env.example — remove placeholder MONERO_RPC_PASS**

In `deploy/.env.example`, change:
```
MONERO_RPC_PASS=rpc_password
```
to:
```
MONERO_RPC_PASS=  # REQUIRED: Generate with `openssl rand -hex 32`
```

**Step 3: Remove SECRET_KEY from all config files**

Search for `SECRET_KEY` in `docker-compose.dev.yml` and `deploy/.env.example`. Since it's not used anywhere in the codebase, remove it entirely to avoid false sense of security. If present in `.env.example` or `.env.railway.example`, remove those lines too.

**Step 4: Commit**

```bash
git add docker-compose.dev.yml deploy/.env.example
git commit -m "fix: remove hardcoded credentials from docker-compose and env examples"
```

---

### Task 3: Protect `WalletService.export_seed()`

**Files:**
- Modify: `sthrip/services/wallet_service.py:161-168`

**Step 1: Delete the method entirely**

Remove lines 161-168 from `wallet_service.py` (the `export_seed` method). It serves no production purpose and is a catastrophic risk if accidentally wired to an endpoint.

```python
# DELETE THIS ENTIRE METHOD:
    def export_seed(self) -> str:
        """Export wallet mnemonic seed via query_key RPC call.

        Returns the 25-word mnemonic seed phrase.
        Raises WalletRPCError if the wallet is view-only or RPC fails.
        """
        result = self.wallet.query_key("mnemonic")
        return result["key"]
```

**Step 2: Run tests**

Run: `pytest tests/ -v --tb=short -k "wallet"`
Expected: All wallet tests pass. No test calls `export_seed`.

**Step 3: Commit**

```bash
git add sthrip/services/wallet_service.py
git commit -m "fix: remove WalletService.export_seed() — catastrophic risk if exposed via API"
```

---

### Task 4: Add HTTP security headers middleware

**Files:**
- Modify: `api/main_v2.py` — add middleware after the CORS middleware block (after line 326)

**Step 1: Write the test**

Create or add to an existing test file:

```python
# In tests/test_security.py (or new tests/test_security_headers.py)
def test_security_headers_present(client):
    """All responses must include security headers."""
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in response.headers["Strict-Transport-Security"]
```

**Step 2: Run test — verify it fails**

Run: `pytest tests/test_security.py::test_security_headers_present -v`
Expected: FAIL — headers not present.

**Step 3: Add middleware in `api/main_v2.py`**

After the CORS middleware block (after line 326), add:

```python
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response
```

**Step 4: Fix CORS — remove `allow_credentials=True`**

The API uses Bearer tokens, not cookies. Change line 323:
```python
    allow_credentials=True,
```
to:
```python
    allow_credentials=False,
```

Also remove `"admin-key"` from `allow_headers` (line 325). Admin key should not be advertised in CORS preflight.

**Step 5: Run test — verify it passes**

Run: `pytest tests/test_security.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add api/main_v2.py tests/test_security.py
git commit -m "fix: add HTTP security headers, remove allow_credentials and admin-key from CORS"
```

---

### Task 5: Disable OpenAPI docs in production

**Files:**
- Modify: `api/main_v2.py:253-258`

**Step 1: Modify FastAPI app creation**

Change the `FastAPI(...)` constructor to:

```python
_is_dev = os.getenv("ENVIRONMENT", "production") == "dev"

app = FastAPI(
    title="Sthrip API",
    description="Production-ready anonymous payments for AI Agents",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _is_dev else None,
    redoc_url="/redoc" if _is_dev else None,
    openapi_url="/openapi.json" if _is_dev else None,
)
```

**Step 2: Run tests**

Tests run with no `ENVIRONMENT` set (defaults to "production"), so verify docs are disabled:

```python
def test_docs_disabled_in_production(client):
    response = client.get("/docs")
    assert response.status_code == 404
```

**Step 3: Commit**

```bash
git add api/main_v2.py tests/test_security.py
git commit -m "fix: disable OpenAPI docs in production, only enabled when ENVIRONMENT=dev"
```

---

### Task 6: Validate MONERO_RPC_PASS at startup in onchain mode

**Files:**
- Modify: `api/main_v2.py` — in the lifespan startup validation block

**Step 1: Find the startup validation block**

Look for the section in `lifespan()` that checks `ADMIN_API_KEY` against placeholder values (around lines 155-200).

**Step 2: Add MONERO_RPC_PASS validation**

After the admin key validation, add:

```python
    # Validate Monero RPC password in onchain mode
    if os.getenv("HUB_MODE", "onchain") == "onchain":
        rpc_pass = os.getenv("MONERO_RPC_PASS", "")
        if not rpc_pass or rpc_pass in ("rpc_password", "change_me", "password"):
            if os.getenv("ENVIRONMENT", "production") != "dev":
                logger.critical("MONERO_RPC_PASS is empty or placeholder — wallet is unprotected!")
                raise SystemExit(1)
```

Also add `ENVIRONMENT` validation:

```python
    env = os.getenv("ENVIRONMENT", "production")
    if env not in ("dev", "staging", "production"):
        logger.critical("ENVIRONMENT must be one of: dev, staging, production. Got: %s", env)
        raise SystemExit(1)
```

**Step 3: Commit**

```bash
git add api/main_v2.py
git commit -m "fix: validate MONERO_RPC_PASS at startup in onchain mode, validate ENVIRONMENT"
```

---

## Phase 2: Data Integrity Fixes

> Priority: HIGH. Prevents money loss under concurrent load.

---

### Task 7: Fix hub-routing payment — single transaction for balance + route

**Files:**
- Modify: `api/main_v2.py:830-858`
- Modify: `sthrip/services/fee_collector.py` — `create_hub_route()` must accept a `db` session parameter

**Why:** Currently, balance deduction (line 831-841) and route creation (line 843-850) happen in separate DB sessions. A crash between them = money deducted but no route record.

**Step 1: Write the test**

```python
# tests/test_payment_atomicity.py
import pytest
from unittest.mock import patch, MagicMock
from decimal import Decimal

def test_hub_payment_rolls_back_on_route_creation_failure(client, auth_headers, registered_agent):
    """If route creation fails, balance must not be deducted."""
    # Deposit some balance first
    # ... (setup code for depositing balance)

    with patch("api.main_v2.get_fee_collector") as mock_fc:
        mock_fc.return_value.calculate_hub_routing_fee.return_value = {
            "fee_amount": Decimal("0.001"),
            "fee_percent": Decimal("0.1"),
            "total_deduction": Decimal("1.001"),
        }
        mock_fc.return_value.create_hub_route.side_effect = Exception("DB error")

        response = client.post("/v2/payments/hub-routing", json={
            "to_agent_name": "recipient",
            "amount": 1.0,
        }, headers=auth_headers)

        assert response.status_code == 500

    # Balance must be unchanged (rolled back)
    balance_response = client.get("/v2/balance", headers=auth_headers)
    # assert original balance is restored
```

**Step 2: Refactor `FeeCollector.create_hub_route()` to accept a session**

In `sthrip/services/fee_collector.py`, change `create_hub_route` signature to accept an optional `db` parameter. If provided, use it instead of calling `get_db()` internally.

**Step 3: Refactor the endpoint**

In `api/main_v2.py`, wrap everything in a single `with get_db() as db:` block:

```python
        with get_db() as db:
            balance_repo = BalanceRepository(db)

            # Check and deduct (under row lock via deduct())
            balance_repo.deduct(agent.id, total_deduction)
            balance_repo.credit(_UUID(recipient.id), amount)

            # Create route in same transaction
            route = collector.create_hub_route(
                from_agent_id=str(agent.id),
                to_agent_id=recipient.id,
                amount=amount,
                from_agent_tier=agent.tier.value,
                urgency=req.urgency,
                idempotency_key=idempotency_key,
                db=db,  # pass session
            )

            # Confirm immediately
            collector.confirm_hub_route(route["payment_id"], db=db)

            # All committed together on context exit
```

**Step 4: Remove the separate get_available() check**

The `deduct()` method already has an internal check with `_get_for_update()`. Remove the separate `get_available()` call (lines 833-838) — it's a TOCTOU vulnerability. Let `deduct()` raise `ValueError` and catch it:

```python
        try:
            balance_repo.deduct(agent.id, total_deduction)
        except ValueError:
            raise HTTPException(status_code=400, detail="Insufficient balance")
```

This eliminates the TOCTOU window and stops leaking exact balance in error messages.

**Step 5: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass.

**Step 6: Commit**

```bash
git add api/main_v2.py sthrip/services/fee_collector.py tests/test_payment_atomicity.py
git commit -m "fix: atomic hub-routing payment — balance + route in single transaction, eliminate TOCTOU"
```

---

### Task 8: Fix withdrawal TOCTOU and rollback atomicity

**Files:**
- Modify: `api/main_v2.py:1119-1200`

**Why:** Same TOCTOU as hub-routing: `get_available()` without lock, then `deduct()` with lock. Also, rollback on RPC failure is in a separate DB session.

**Step 1: Refactor withdrawal endpoint**

```python
@app.post("/v2/balance/withdraw")
async def withdraw_balance(
    req: WithdrawRequest,
    agent: Agent = Depends(get_current_agent),
    idempotency_key: Optional[str] = Header(None),
):
    hub_mode = os.getenv("HUB_MODE", "onchain")

    store = get_idempotency_store() if idempotency_key else None
    if idempotency_key:
        cached = store.try_reserve(str(agent.id), "withdraw", idempotency_key)
        if cached is not None:
            return cached

    try:
        from decimal import Decimal
        amount = Decimal(str(req.amount))

        # Deduct balance atomically (deduct() uses _get_for_update internally)
        with get_db() as db:
            repo = BalanceRepository(db)
            try:
                repo.deduct(agent.id, amount)
            except ValueError:
                raise HTTPException(status_code=400, detail="Insufficient balance for this withdrawal")
            balance = repo.get_or_create(agent.id)
            balance.total_withdrawn = (balance.total_withdrawn or Decimal("0")) + amount

        if hub_mode == "onchain":
            wallet_svc = get_wallet_service()
            try:
                tx_result = await asyncio.to_thread(wallet_svc.send_withdrawal, req.address, amount)
            except Exception as e:
                # Rollback — credit back
                with get_db() as db:
                    repo = BalanceRepository(db)
                    repo.credit(agent.id, amount)
                    bal = repo.get_or_create(agent.id)
                    bal.total_withdrawn = (bal.total_withdrawn or Decimal("0")) - amount
                logger.error("Withdrawal RPC failed for agent=%s: %s", agent.id, e)
                raise HTTPException(status_code=502, detail="Withdrawal processing failed. Please try again later.")
            # ... rest unchanged
```

Key changes:
1. Remove `get_available()` check — let `deduct()` handle it
2. Wrap `wallet_svc.send_withdrawal` in `asyncio.to_thread()` (fixes blocking I/O)
3. Error message no longer leaks exact balance

**Step 2: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass.

**Step 3: Commit**

```bash
git add api/main_v2.py
git commit -m "fix: withdrawal TOCTOU — remove separate balance check, wrap RPC in asyncio.to_thread"
```

---

### Task 9: Fix ReputationRepository lost-update race

**Files:**
- Modify: `sthrip/db/repository.py:601-619`

**Step 1: Write the test**

```python
# tests/test_reputation_atomic.py
from decimal import Decimal
from sthrip.db.repository import ReputationRepository

def test_record_transaction_uses_atomic_increment(db_session, agent_with_reputation):
    """record_transaction must use SQL-level increment, not Python +="""
    repo = ReputationRepository(db_session)
    repo.record_transaction(agent_with_reputation.id, success=True, amount_usd=Decimal("100"))
    db_session.commit()

    rep = repo.get_by_agent(agent_with_reputation.id)
    assert rep.total_transactions == 1
    assert rep.successful_transactions == 1
    assert rep.total_volume_usd == Decimal("100")
```

**Step 2: Refactor to SQL-level arithmetic**

```python
from sqlalchemy import update

def record_transaction(
    self,
    agent_id: UUID,
    success: bool = True,
    amount_usd: Decimal = Decimal('0')
):
    """Record transaction for reputation using atomic SQL updates."""
    values = {
        models.AgentReputation.total_transactions: models.AgentReputation.total_transactions + 1,
        models.AgentReputation.total_volume_usd: models.AgentReputation.total_volume_usd + amount_usd,
        models.AgentReputation.calculated_at: datetime.now(timezone.utc),
    }
    if success:
        values[models.AgentReputation.successful_transactions] = models.AgentReputation.successful_transactions + 1
    else:
        values[models.AgentReputation.failed_transactions] = models.AgentReputation.failed_transactions + 1

    self.db.execute(
        update(models.AgentReputation)
        .where(models.AgentReputation.agent_id == agent_id)
        .values(**values)
    )
```

**Step 3: Run tests**

Run: `pytest tests/ -v --tb=short`

**Step 4: Commit**

```bash
git add sthrip/db/repository.py tests/test_reputation_atomic.py
git commit -m "fix: reputation counter race — use SQL-level atomic increments instead of Python +="
```

---

### Task 10: Fix `_get_for_update()` silent fallback

**Files:**
- Modify: `sthrip/db/repository.py:658-675`

**Step 1: Replace broad `except Exception` with dialect check**

```python
def _get_for_update(self, agent_id, token="XMR"):
    """Get balance with row-level lock for safe mutations."""
    is_sqlite = self.db.bind and self.db.bind.dialect.name == "sqlite"

    if is_sqlite:
        balance = self.db.query(AgentBalance).filter(
            AgentBalance.agent_id == agent_id,
            AgentBalance.token == token
        ).first()
    else:
        balance = self.db.query(AgentBalance).filter(
            AgentBalance.agent_id == agent_id,
            AgentBalance.token == token
        ).with_for_update().first()

    if not balance:
        balance = AgentBalance(agent_id=agent_id, token=token)
        self.db.add(balance)
        self.db.flush()
    return balance
```

**Step 2: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass (tests use SQLite, so they hit the `is_sqlite` branch).

**Step 3: Commit**

```bash
git add sthrip/db/repository.py
git commit -m "fix: _get_for_update checks dialect instead of catching all exceptions"
```

---

### Task 11: Add missing index on `AgentBalance.deposit_address`

**Files:**
- Create: `migrations/versions/XXXX_add_deposit_address_index.py`

**Step 1: Generate migration**

```bash
cd /Users/saveliy/Documents/Agent\ Payments/sthrip
alembic revision --autogenerate -m "add index on agent_balance.deposit_address"
```

If Alembic is not configured for local dev, create manually:

```python
"""add index on agent_balance.deposit_address"""
from alembic import op

revision = 'b1c2d3e4f5g6'
down_revision = 'a1b2c3d4e5f6'

def upgrade():
    op.create_index('ix_agent_balance_deposit_address', 'agent_balance', ['deposit_address'])

def downgrade():
    op.drop_index('ix_agent_balance_deposit_address', 'agent_balance')
```

**Step 2: Also add the index to the model**

In `sthrip/db/models.py`, on the `AgentBalance` class, add `index=True` to the `deposit_address` column definition.

**Step 3: Commit**

```bash
git add migrations/ sthrip/db/models.py
git commit -m "perf: add index on agent_balance.deposit_address for deposit monitor lookups"
```

---

## Phase 3: Architecture Refactor

> Priority: HIGH. Required for maintainability and horizontal scaling.

---

### Task 12: Centralize configuration with `pydantic-settings`

**Files:**
- Create: `sthrip/config.py`
- Modify: `requirements.txt` — add `pydantic-settings>=2.0.0`

**Step 1: Install pydantic-settings**

```bash
pip install pydantic-settings
echo "pydantic-settings>=2.0.0" >> requirements.txt
```

**Step 2: Create `sthrip/config.py`**

```python
"""Centralized configuration — single source of truth for all env vars."""

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Core
    environment: Literal["dev", "staging", "production"] = "production"
    log_level: str = "INFO"
    port: int = 8000

    # Database
    database_url: str = "postgresql://localhost/sthrip"
    db_pool_size: int = 10
    db_pool_overflow: int = 20

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    admin_api_key: str = Field(...)

    # Monero
    hub_mode: Literal["onchain", "ledger"] = "onchain"
    monero_rpc_host: str = "127.0.0.1"
    monero_rpc_port: int = 18082
    monero_rpc_user: str = ""
    monero_rpc_pass: str = ""
    monero_network: Literal["mainnet", "stagenet", "testnet"] = "stagenet"
    monero_min_confirmations: int = 10
    deposit_poll_interval: int = 30

    # CORS
    cors_origins: str = ""

    # Proxy
    trusted_proxy_hosts: str = "127.0.0.1"

    # Monitoring (optional)
    sentry_dsn: str = ""
    betterstack_token: str = ""

    @field_validator("admin_api_key")
    @classmethod
    def validate_admin_key(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        if env != "dev" and v in ("change_me", "dev-admin-key", "test", ""):
            raise ValueError("ADMIN_API_KEY must be set to a secure value in non-dev environments")
        return v

    @field_validator("monero_rpc_pass")
    @classmethod
    def validate_rpc_pass(cls, v: str, info) -> str:
        env = info.data.get("environment", "production")
        hub_mode = info.data.get("hub_mode", "onchain")
        if hub_mode == "onchain" and env != "dev" and v in ("", "rpc_password", "change_me"):
            raise ValueError("MONERO_RPC_PASS must be set when HUB_MODE=onchain in non-dev")
        return v

    model_config = {"env_prefix": "", "case_sensitive": False}


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

**Step 3: Write test for config validation**

```python
# tests/test_config.py
import pytest
from sthrip.config import Settings

def test_rejects_placeholder_admin_key():
    with pytest.raises(Exception):
        Settings(admin_api_key="change_me", environment="production")

def test_accepts_valid_config():
    s = Settings(admin_api_key="sk_real_key_here", environment="dev")
    assert s.environment == "dev"

def test_rejects_invalid_environment():
    with pytest.raises(Exception):
        Settings(admin_api_key="key", environment="invalid")
```

**Step 4: Run tests**

Run: `pytest tests/test_config.py -v`

**Step 5: Commit**

```bash
git add sthrip/config.py tests/test_config.py requirements.txt
git commit -m "feat: centralized config with pydantic-settings, startup validation for all env vars"
```

> **Note:** Wiring `get_settings()` into all files that use `os.getenv()` is a separate task. For now, the config module exists and new code should use it. Gradual migration of existing `os.getenv()` calls happens in Task 16.

---

### Task 13: Split `api/main_v2.py` into routers

**Files:**
- Create: `api/schemas.py` — Pydantic request/response models
- Create: `api/deps.py` — auth dependencies, `get_db`, `get_current_agent`
- Create: `api/middleware.py` — all middleware functions
- Create: `api/routers/__init__.py`
- Create: `api/routers/agents.py` — `/v2/agents/*` endpoints
- Create: `api/routers/payments.py` — `/v2/payments/*` endpoints
- Create: `api/routers/balance.py` — `/v2/balance/*` endpoints
- Create: `api/routers/webhooks.py` — `/v2/webhooks/*` endpoints
- Create: `api/routers/admin.py` — `/v2/admin/*` endpoints
- Create: `api/routers/health.py` — `/health`, `/ready`, `/metrics`
- Modify: `api/main_v2.py` — reduce to ~50 lines (app factory + router includes)

**Step 1: Extract Pydantic models to `api/schemas.py`**

Move all `class ...Model(BaseModel)` definitions from `main_v2.py` lines 335-430 to `api/schemas.py`. Keep imports minimal.

**Step 2: Extract auth dependencies to `api/deps.py`**

Move `security = HTTPBearer(...)`, `get_current_agent()`, `_verify_admin_key()` to `api/deps.py`.

**Step 3: Extract middleware to `api/middleware.py`**

Move `add_request_id`, `track_metrics`, `limit_request_body`, `add_security_headers`, `_get_cors_origins` to `api/middleware.py`. Export a `configure_middleware(app)` function.

**Step 4: Create routers**

Each router file follows this pattern:

```python
# api/routers/payments.py
from fastapi import APIRouter, Depends, Header, BackgroundTasks
from ..deps import get_current_agent
from ..schemas import HubPaymentRequest

router = APIRouter(prefix="/v2/payments", tags=["payments"])

@router.post("/hub-routing")
async def send_hub_routed_payment(...):
    ...
```

**Step 5: Reduce `main_v2.py` to app factory**

```python
"""Sthrip API v2 — app factory"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .middleware import configure_middleware
from .routers import agents, payments, balance, webhooks, admin, health

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... startup/shutdown logic stays here
    yield

def create_app() -> FastAPI:
    is_dev = os.getenv("ENVIRONMENT", "production") == "dev"
    app = FastAPI(
        title="Sthrip API",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs" if is_dev else None,
        redoc_url=None,
        openapi_url="/openapi.json" if is_dev else None,
    )
    configure_middleware(app)
    app.include_router(health.router)
    app.include_router(agents.router)
    app.include_router(payments.router)
    app.include_router(balance.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    return app

app = create_app()
```

**Step 6: Fix route ordering**

In `api/routers/payments.py`, put `/v2/payments/history` BEFORE `/v2/payments/{payment_id}`:

```python
@router.get("/history")
async def get_payment_history(...):
    ...

@router.get("/{payment_id}")
async def get_payment(...):
    ...
```

**Step 7: Update `railway.toml` start command if needed**

If the import path changed, update `railway.toml`. The current `api.main_v2:app` should still work since `app = create_app()` is at module level.

**Step 8: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All 220+ tests pass. Some tests may need import path updates.

**Step 9: Commit**

```bash
git add api/
git commit -m "refactor: split main_v2.py (1400 lines) into routers, schemas, deps, middleware"
```

---

### Task 14: Replace global singletons with FastAPI `Depends()`

**Files:**
- Modify: `api/deps.py` — add dependency providers
- Modify: `api/routers/*.py` — inject via `Depends()`

**Step 1: Create dependency providers in `api/deps.py`**

```python
from fastapi import Depends
from sqlalchemy.orm import Session
from sthrip.db.database import get_db
from sthrip.db.repository import BalanceRepository, TransactionRepository

def get_db_session():
    with get_db() as db:
        yield db

def get_balance_repo(db: Session = Depends(get_db_session)) -> BalanceRepository:
    return BalanceRepository(db)

def get_transaction_repo(db: Session = Depends(get_db_session)) -> TransactionRepository:
    return TransactionRepository(db)
```

**Step 2: Replace `get_db()` calls in router handlers with `Depends(get_db_session)`**

Example:
```python
# Before:
async def withdraw_balance(...):
    with get_db() as db:
        repo = BalanceRepository(db)
        ...

# After:
async def withdraw_balance(
    ...,
    db: Session = Depends(get_db_session),
):
    repo = BalanceRepository(db)
    ...
```

**Step 3: Run tests, fix import errors**

Run: `pytest tests/ -v --tb=short`

**Step 4: Commit**

```bash
git add api/
git commit -m "refactor: replace global singletons with FastAPI Depends() for proper DI"
```

---

### Task 15: Fix `declarative_base` deprecated import

**Files:**
- Modify: `sthrip/db/models.py:17,21`

**Step 1: Update import**

Change:
```python
from sqlalchemy.ext.declarative import declarative_base
Base = declarative_base()
```
to:
```python
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

**Step 2: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass. The warning `MovedIn20Warning` should disappear.

**Step 3: Commit**

```bash
git add sthrip/db/models.py
git commit -m "fix: migrate to sqlalchemy.orm.DeclarativeBase (deprecated declarative_base removed)"
```

---

### Task 16: Replace all `datetime.utcnow()` with `datetime.now(timezone.utc)`

**Files:**
- Modify: All files containing `datetime.utcnow()` (~54 occurrences across ~10 files)

**Step 1: Find all occurrences**

```bash
grep -rn "datetime.utcnow()" --include="*.py" .
```

**Step 2: Global replacement**

In every file, add `from datetime import timezone` to imports (if not present), then replace:
```python
datetime.utcnow()  →  datetime.now(timezone.utc)
```

Key files: `repository.py`, `fee_collector.py`, `agent_registry.py`, `webhook_service.py`, `deposit_monitor.py`, `main_v2.py`, `monitoring.py`, `idempotency.py`.

**Step 3: Run tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass.

**Step 4: Commit**

```bash
git add -u
git commit -m "fix: replace 54x datetime.utcnow() with datetime.now(timezone.utc) — deprecated since 3.12"
```

---

### Task 17: Replace `print()` with `logger.info()` in lifespan

**Files:**
- Modify: `api/main_v2.py` (or `api/main_v2.py` after Task 13 refactor, whichever applies)

**Step 1: Replace all `print()` calls**

Find all `print(` calls in the lifespan function and replace with `logger.info()`:

```python
# Before:
print("🚀 Starting Sthrip API v2...")

# After:
logger.info("Starting Sthrip API v2")
```

Remove emoji from log messages (emojis break structured JSON logging).

**Step 2: Also fix `webhook_service.py:218`**

```python
# Before:
print(f"Webhook worker error: {e}")

# After:
logger.error("Webhook worker error", exc_info=True)
```

**Step 3: Run tests**

Run: `pytest tests/ -v --tb=short`

**Step 4: Commit**

```bash
git add api/ sthrip/services/webhook_service.py
git commit -m "fix: replace print() with logger.info/error in lifespan and webhook worker"
```

---

### Task 18: Wire Prometheus metrics counters

**Files:**
- Modify: `api/routers/payments.py` (or `api/main_v2.py` if Task 13 not yet done)
- Modify: `api/routers/balance.py`

**Step 1: Add `.inc()` calls**

In hub-routing payment handler, after successful payment:
```python
hub_payments_total.labels(agent.tier.value, req.urgency).inc()
```

In deposit handler, after successful deposit:
```python
balance_ops_total.labels("deposit", "XMR").inc()
```

In withdrawal handler, after successful withdrawal:
```python
balance_ops_total.labels("withdrawal", "XMR").inc()
```

**Step 2: Run tests**

Run: `pytest tests/ -v --tb=short`

**Step 3: Commit**

```bash
git add api/
git commit -m "fix: wire Prometheus hub_payments_total and balance_ops_total counters"
```

---

### Task 19: Add `asyncio.to_thread()` wrapper for blocking wallet RPC calls

**Files:**
- Modify: `api/routers/balance.py` (deposit endpoint)
- Already done for withdrawal in Task 8

**Step 1: Wrap deposit address creation**

In the deposit endpoint (onchain mode), the call `wallet_svc.get_or_create_deposit_address(agent.id)` uses sync `requests` internally. Wrap it:

```python
# Before:
deposit_address = wallet_svc.get_or_create_deposit_address(agent.id)

# After:
deposit_address = await asyncio.to_thread(wallet_svc.get_or_create_deposit_address, agent.id)
```

**Step 2: Search for any other direct sync wallet calls in async handlers**

```bash
grep -n "wallet_svc\.\|get_wallet_service()" api/main_v2.py api/routers/*.py
```

Wrap all of them in `asyncio.to_thread()`.

**Step 3: Run tests**

Run: `pytest tests/ -v --tb=short`

**Step 4: Commit**

```bash
git add api/
git commit -m "fix: wrap blocking wallet RPC calls in asyncio.to_thread to avoid event loop blocking"
```

---

### Task 20: Add rate limiting on failed authentication attempts

**Files:**
- Modify: `api/deps.py` (or wherever `get_current_agent` lives)

**Step 1: Track failed auth attempts by IP**

```python
async def get_current_agent(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing authorization")

    # Rate limit failed auth by IP
    client_ip = request.client.host if request.client else "unknown"
    rate_limiter = get_rate_limiter()

    try:
        rate_limiter.check_ip_rate_limit(client_ip, endpoint="auth", limit=20, window=60)
    except RateLimitExceeded:
        raise HTTPException(status_code=429, detail="Too many authentication attempts")

    # ... existing auth logic ...

    if not agent:
        # Increment failed auth counter
        rate_limiter.record_failed_attempt(client_ip, endpoint="auth")
        raise HTTPException(status_code=401, detail="Invalid API key")

    return agent
```

**Step 2: Implement `check_ip_rate_limit` and `record_failed_attempt` in RateLimiter**

Add two methods to `sthrip/services/rate_limiter.py`:

```python
def check_ip_rate_limit(self, ip: str, endpoint: str, limit: int, window: int):
    """Check IP-based rate limit (for unauthenticated endpoints)."""
    key = f"ratelimit:ip:{ip}:{endpoint}"
    if self.use_redis:
        count = self.redis.incr(key)
        if count == 1:
            self.redis.expire(key, window)
        if count > limit:
            raise RateLimitExceeded(limit=limit, reset_at=time.time() + window)
    else:
        # Local fallback
        now = time.time()
        entry = self._local_cache.get(key, {"count": 0, "reset_at": now + window})
        if now > entry["reset_at"]:
            entry = {"count": 0, "reset_at": now + window}
        entry["count"] += 1
        self._local_cache[key] = entry
        if entry["count"] > limit:
            raise RateLimitExceeded(limit=limit, reset_at=entry["reset_at"])
```

**Step 3: Write test**

```python
def test_auth_rate_limiting_after_failed_attempts(client):
    """After 20 failed auth attempts from same IP, should return 429."""
    for i in range(21):
        response = client.get("/v2/balance", headers={"Authorization": "Bearer invalid_key"})
    assert response.status_code == 429
```

**Step 4: Run tests**

Run: `pytest tests/ -v --tb=short`

**Step 5: Commit**

```bash
git add api/deps.py sthrip/services/rate_limiter.py tests/
git commit -m "feat: rate limit failed auth attempts — 20 per minute per IP"
```

---

### Task 21: Add `threading.Lock` to RateLimiter `_local_cache`

**Files:**
- Modify: `sthrip/services/rate_limiter.py:52-80`

**Step 1: Add lock to `__init__`**

```python
import threading

class RateLimiter:
    def __init__(self, ...):
        ...
        self._local_cache: Dict[str, Dict] = {}
        self._cache_lock = threading.Lock()
```

**Step 2: Wrap all `_local_cache` access in `with self._cache_lock:`**

In `_check_local` and any method that reads/writes `_local_cache`.

**Step 3: Run tests**

Run: `pytest tests/ -v --tb=short`

**Step 4: Commit**

```bash
git add sthrip/services/rate_limiter.py
git commit -m "fix: add threading.Lock to RateLimiter._local_cache for thread safety"
```

---

### Task 22: Log alert when rate limiter falls back to in-process dict

**Files:**
- Modify: `sthrip/services/rate_limiter.py:62-80`

**Step 1: Add critical log on fallback**

```python
import logging
logger = logging.getLogger("sthrip.rate_limiter")

class RateLimiter:
    def __init__(self, ...):
        ...
        if REDIS_AVAILABLE:
            try:
                self.redis = redis.from_url(redis_url, decode_responses=True)
                self.redis.ping()
                self.use_redis = True
            except (redis.ConnectionError, redis.ResponseError):
                self.use_redis = False
                logger.critical(
                    "Rate limiter falling back to in-process dict — "
                    "multi-replica rate limiting is DISABLED"
                )
        else:
            self.use_redis = False
            self.redis = None
            logger.critical("Redis not available — rate limiter using in-process dict")
```

**Step 2: Commit**

```bash
git add sthrip/services/rate_limiter.py
git commit -m "fix: log CRITICAL when rate limiter falls back to in-process dict"
```

---

## Phase 4: Coverage & Polish

> Priority: MEDIUM. Brings coverage from 58% to 80%+.

---

### Task 23: Add type hints to `BalanceRepository`

**Files:**
- Modify: `sthrip/db/repository.py:642-711`

**Step 1: Add type annotations to all methods**

```python
from uuid import UUID

class BalanceRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create(self, agent_id: UUID, token: str = "XMR") -> AgentBalance:
        ...

    def _get_for_update(self, agent_id: UUID, token: str = "XMR") -> AgentBalance:
        ...

    def get_available(self, agent_id: UUID, token: str = "XMR") -> Decimal:
        ...

    def deposit(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        ...

    def deduct(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        ...

    def credit(self, agent_id: UUID, amount: Decimal, token: str = "XMR") -> AgentBalance:
        ...

    def set_deposit_address(self, agent_id: UUID, address: str, token: str = "XMR") -> AgentBalance:
        ...
```

**Step 2: Run mypy**

```bash
mypy sthrip/db/repository.py --ignore-missing-imports
```

**Step 3: Commit**

```bash
git add sthrip/db/repository.py
git commit -m "fix: add type hints to BalanceRepository — most financially critical code"
```

---

### Task 24: Increase test coverage to 80%

**Files:**
- Create/modify tests for modules with <70% coverage

**Step 1: Identify coverage gaps**

From the audit, these modules have lowest coverage:
- `sthrip/services/rate_limiter.py` — 23%
- `sthrip/services/url_validator.py` — 29%
- `sthrip/services/monitoring.py` — 35%
- `sthrip/services/webhook_service.py` — 35%
- `sthrip/db/database.py` — 37%
- `sthrip/services/metrics.py` — 38%
- `sthrip/services/idempotency.py` — 53%

**Step 2: Write tests for each module**

Priority order (most impactful for coverage):
1. `rate_limiter.py` — test tier configs, Redis fallback, IP limiting, edge cases
2. `monitoring.py` — test health checks, alert dispatch, system resource checks
3. `webhook_service.py` — test delivery, retries, HMAC signing, SSRF re-validation
4. `url_validator.py` — test private IP blocking, DNS resolution, edge cases
5. `idempotency.py` — test reserve/store/release/expiry cycles
6. `database.py` — test pool creation, health check, error handling

Target: ~50-80 new test cases across these modules.

**Step 3: Run coverage report**

```bash
pytest tests/ --cov=sthrip --cov=api --cov-report=term-missing | tail -40
```

Target: 80%+ total coverage.

**Step 4: Commit incrementally**

One commit per test module:
```bash
git commit -m "test: add rate_limiter tests — coverage 23% → 85%"
git commit -m "test: add monitoring tests — coverage 35% → 80%"
# etc.
```

---

### Task 25: Sanitize `X-Request-ID` header input

**Files:**
- Modify: `api/middleware.py` (or `api/main_v2.py` if Task 13 not done)

**Step 1: Add sanitization**

```python
import re

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    raw_rid = request.headers.get("x-request-id", "")
    sanitized = re.sub(r'[^a-zA-Z0-9\-_]', '', raw_rid)[:64]
    rid = sanitized or generate_request_id()
    request_id_var.set(rid)
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response
```

**Step 2: Commit**

```bash
git add api/
git commit -m "fix: sanitize X-Request-ID input to prevent log injection"
```

---

### Task 26: Include timestamp in webhook HMAC signature

**Files:**
- Modify: `sthrip/services/webhook_service.py:45-53,81`

**Step 1: Modify `_sign_payload` to include timestamp**

```python
def _sign_payload(self, payload: dict, secret: str, timestamp: str) -> str:
    """Sign webhook payload with HMAC (Stripe model: timestamp.payload)."""
    payload_str = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    message = f"{timestamp}.{payload_str}"
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return f"sha256={signature}"
```

**Step 2: Update caller**

```python
timestamp = str(int(time.time()))
headers["X-Sthrip-Timestamp"] = timestamp
if secret:
    headers["X-Sthrip-Signature"] = self._sign_payload(payload, secret, timestamp)
```

Add `import time` at the top of the file.

**Step 3: Update tests**

Update `test_mcp_tools.py` or webhook tests to verify new signature format.

**Step 4: Commit**

```bash
git add sthrip/services/webhook_service.py tests/
git commit -m "fix: include timestamp in webhook HMAC signature to prevent replay attacks"
```

---

### Task 27: Create shared `aiohttp.ClientSession` in WebhookService

**Files:**
- Modify: `sthrip/services/webhook_service.py:30-118`

**Step 1: Add session lifecycle to WebhookService**

```python
class WebhookService:
    def __init__(self, max_retries: int = 5):
        self.max_retries = max_retries
        self._running = False
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
```

**Step 2: Update `_send_webhook` to use shared session**

```python
async def _send_webhook(self, url, payload, secret=None, timeout=30):
    ...
    session = await self._get_session()
    async with session.post(url, json=payload, headers=headers, timeout=...) as response:
        ...
```

**Step 3: Call `close()` in app shutdown**

In the lifespan handler's shutdown section:
```python
webhook_svc = get_webhook_service()
await webhook_svc.close()
```

**Step 4: Run tests**

Run: `pytest tests/ -v --tb=short`

**Step 5: Commit**

```bash
git add sthrip/services/webhook_service.py api/
git commit -m "perf: reuse aiohttp.ClientSession in WebhookService instead of creating per-request"
```

---

## Summary

| Phase | Tasks | Estimated Effort | Deploys |
|-------|-------|-----------------|---------|
| 1. Security Hotfixes | 1-6 | 1-2 hours | Deploy after Task 6 |
| 2. Data Integrity | 7-11 | 2-3 hours | Deploy after Task 11 |
| 3. Architecture Refactor | 12-22 | 4-6 hours | Deploy after Task 22 |
| 4. Coverage & Polish | 23-27 | 3-4 hours | Deploy after Task 27 |

**Total: 27 tasks, ~10-15 hours**

### Post-Completion Verification

After all tasks are done, run:

```bash
# Full test suite with coverage
pytest tests/ -v --cov=sthrip --cov=api --cov=integrations --cov-report=term-missing

# Type checking
mypy sthrip/ --ignore-missing-imports

# Lint
ruff check sthrip/ api/

# Dependency audit
pip-audit
```

Expected:
- 250+ tests passing
- 80%+ coverage
- 0 mypy errors on modified files
- 0 known vulnerabilities
