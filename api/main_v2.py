"""
StealthPay API v2 - Production Ready
PostgreSQL, Redis, proper authentication
"""

import os
import asyncio
from datetime import datetime
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, Request, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

# Import our services
from stealthpay.db.database import create_tables, get_db
from stealthpay.db.models import Agent
from stealthpay.db.repository import AgentRepository, TransactionRepository, BalanceRepository
from stealthpay.services.rate_limiter import get_rate_limiter, RateLimitExceeded
from stealthpay.services.fee_collector import get_fee_collector
from stealthpay.services.agent_registry import get_registry, AgentRegistry
from stealthpay.services.monitoring import get_monitor, setup_default_monitoring
from stealthpay.services.webhook_service import get_webhook_service, queue_webhook


# ═══════════════════════════════════════════════════════════════════════════════
# LIFESPAN & INIT
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler"""
    # Startup
    print("🚀 Starting StealthPay API v2...")
    
    # Create database tables
    create_tables()
    print("✅ Database tables ready")
    
    # Start health monitoring
    monitor = setup_default_monitoring()
    monitor.start_monitoring()
    print("✅ Health monitoring started")
    
    # Start webhook worker
    webhook_service = get_webhook_service()
    webhook_task = asyncio.create_task(webhook_service.start_worker())
    print("✅ Webhook worker started")
    
    yield
    
    # Shutdown
    print("🛑 Shutting down...")
    monitor.stop_monitoring()
    webhook_service.stop_worker()
    webhook_task.cancel()
    try:
        await webhook_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="StealthPay API",
    description="Production-ready anonymous payments for AI Agents",
    version="2.0.0",
    lifespan=lifespan
)

# Middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

security = HTTPBearer(auto_error=False)


# ═══════════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class AgentRegistration(BaseModel):
    agent_name: str = Field(..., min_length=3, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$")
    webhook_url: Optional[str] = None
    privacy_level: str = Field(default="medium", pattern=r"^(low|medium|high|paranoid)$")
    xmr_address: Optional[str] = None
    base_address: Optional[str] = None
    solana_address: Optional[str] = None


class AgentResponse(BaseModel):
    agent_id: str
    agent_name: str
    tier: str
    api_key: str  # Shown once!
    created_at: str


class PaymentRequest(BaseModel):
    to_address: str = Field(..., description="Recipient Monero address")
    amount: float = Field(..., gt=0, description="Amount in XMR")
    memo: Optional[str] = Field(None, max_length=1000)
    privacy_level: Optional[str] = Field(None, pattern=r"^(low|medium|high|paranoid)$")
    use_hub_routing: bool = Field(False, description="Use hub routing for instant confirmation")


class HubPaymentRequest(BaseModel):
    to_agent_name: str = Field(..., description="Recipient agent name")
    amount: float = Field(..., gt=0)
    memo: Optional[str] = None
    urgency: str = Field(default="normal", pattern=r"^(normal|urgent)$")


class EscrowCreateRequest(BaseModel):
    seller_address: str
    arbiter_address: Optional[str] = None
    amount: float = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=1000)
    timeout_hours: int = Field(default=48, ge=1, le=720)


class AgentProfileResponse(BaseModel):
    agent_name: str
    did: Optional[str]
    tier: str
    trust_score: int
    total_transactions: int
    xmr_address: Optional[str]
    base_address: Optional[str]
    verified_at: Optional[str]


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: str
    checks: dict


# ═══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════════

async def get_current_agent(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    request: Request = None
) -> Agent:
    """Authenticate agent and check rate limits"""
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing API key")
    
    api_key = credentials.credentials
    
    with get_db() as db:
        repo = AgentRepository(db)
        agent = repo.get_by_api_key(api_key)
        
        if not agent:
            raise HTTPException(status_code=401, detail="Invalid API key")
        
        if not agent.is_active:
            raise HTTPException(status_code=403, detail="Agent account disabled")
        
        # Update last seen
        repo.update_last_seen(agent.id)
        
        # Check rate limit
        try:
            limiter = get_rate_limiter()
            path = request.url.path if request else "/"
            limiter.check_rate_limit(
                agent_id=str(agent.id),
                tier=agent.rate_limit_tier.value,
                endpoint=path
            )
        except RateLimitExceeded as e:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": "Rate limit exceeded",
                    "limit": e.limit,
                    "reset_at": e.reset_at
                }
            )
        
        return agent


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_model=dict)
async def root():
    """API info"""
    registry = get_registry()
    stats = registry.get_stats()
    
    return {
        "name": "StealthPay API",
        "version": "2.0.0",
        "description": "Anonymous payments for AI Agents",
        "agents_registered": stats["total_agents"],
        "endpoints": {
            "docs": "/docs",
            "health": "/health",
            "agents": "/v2/agents",
            "payments": "/v2/payments"
        }
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    monitor = get_monitor()
    report = monitor.get_health_report()
    
    return HealthResponse(
        status=report["status"],
        version="2.0.0",
        timestamp=report["timestamp"],
        checks=report["checks"]
    )


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT REGISTRY (Public)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/v2/agents/register", response_model=AgentResponse, status_code=201)
async def register_agent(reg: AgentRegistration):
    """Register new agent"""
    registry = get_registry()
    
    try:
        result = registry.register_agent(
            agent_name=reg.agent_name,
            webhook_url=reg.webhook_url,
            privacy_level=reg.privacy_level,
            xmr_address=reg.xmr_address,
            base_address=reg.base_address,
            solana_address=reg.solana_address
        )
        
        return AgentResponse(
            agent_id=result["agent_id"],
            agent_name=result["agent_name"],
            tier=result["tier"],
            api_key=result["api_key"],
            created_at=result["created_at"]
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/v2/agents/{agent_name}", response_model=AgentProfileResponse)
async def get_agent_profile(agent_name: str):
    """Get public agent profile"""
    registry = get_registry()
    profile = registry.get_profile(agent_name)
    
    if not profile:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    return AgentProfileResponse(
        agent_name=profile.agent_name,
        did=profile.did,
        tier=profile.tier,
        trust_score=profile.trust_score,
        total_transactions=profile.total_transactions,
        xmr_address=profile.xmr_address,
        base_address=profile.base_address,
        verified_at=profile.verified_at
    )


@app.get("/v2/agents", response_model=List[AgentProfileResponse])
async def discover_agents(
    min_trust_score: Optional[int] = None,
    tier: Optional[str] = None,
    verified_only: bool = False,
    limit: int = 100,
    offset: int = 0
):
    """Discover agents with filters"""
    registry = get_registry()
    
    profiles = registry.discover_agents(
        min_trust_score=min_trust_score,
        tier=tier,
        verified_only=verified_only,
        limit=limit,
        offset=offset
    )
    
    return [
        AgentProfileResponse(
            agent_name=p.agent_name,
            did=p.did,
            tier=p.tier,
            trust_score=p.trust_score,
            total_transactions=p.total_transactions,
            xmr_address=p.xmr_address,
            base_address=p.base_address,
            verified_at=p.verified_at
        )
        for p in profiles
    ]


@app.get("/v2/leaderboard")
async def get_leaderboard(limit: int = 100):
    """Get top agents by trust score"""
    registry = get_registry()
    return registry.get_leaderboard(limit=limit)


# ═══════════════════════════════════════════════════════════════════════════════
# AUTHENTICATED ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/v2/me")
async def get_current_agent_info(agent: Agent = Depends(get_current_agent)):
    """Get current agent info"""
    return {
        "agent_id": str(agent.id),
        "agent_name": agent.agent_name,
        "tier": agent.tier.value,
        "privacy_level": agent.privacy_level.value,
        "xmr_address": agent.xmr_address,
        "created_at": agent.created_at.isoformat()
    }


@app.get("/v2/me/rate-limit")
async def get_rate_limit_status(agent: Agent = Depends(get_current_agent)):
    """Get current rate limit status"""
    limiter = get_rate_limiter()
    status = limiter.get_limit_status(
        agent_id=str(agent.id),
        tier=agent.rate_limit_tier.value
    )
    return status


# ═══════════════════════════════════════════════════════════════════════════════
# PAYMENTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/v2/payments/send")
async def send_payment():
    """Direct P2P not available in hub-only mode. Use /v2/payments/hub-routing instead."""
    raise HTTPException(status_code=501, detail="Direct P2P not available. Use /v2/payments/hub-routing for payments.")


@app.post("/v2/payments/hub-routing")
async def send_hub_routed_payment(
    req: HubPaymentRequest,
    background_tasks: BackgroundTasks,
    agent: Agent = Depends(get_current_agent)
):
    """
    Send payment via hub routing

    Fee: 0.1% (or higher for urgent)
    Benefit: Instant confirmation, reputation verification
    """
    from decimal import Decimal

    registry = get_registry()
    collector = get_fee_collector()

    # Find recipient
    recipient = registry.get_profile(req.to_agent_name)
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient agent not found")

    if not recipient.xmr_address:
        raise HTTPException(status_code=400, detail="Recipient has no XMR address configured")

    amount = Decimal(str(req.amount))

    # Calculate fee
    fee_info = collector.calculate_hub_routing_fee(
        amount=amount,
        from_agent_tier=agent.tier.value,
        urgency=req.urgency
    )
    total_deduction = fee_info["total_deduction"]

    # Check and deduct sender balance, credit recipient
    with get_db() as db:
        balance_repo = BalanceRepository(db)
        available = balance_repo.get_available(agent.id)
        if available < total_deduction:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient balance: {float(available)} XMR available, {float(total_deduction)} XMR needed (amount + fee)"
            )
        balance_repo.deduct(agent.id, total_deduction)
        balance_repo.credit(recipient.id, amount)

    # Create hub route record
    route = collector.create_hub_route(
        from_agent_id=str(agent.id),
        to_agent_id=recipient.id,
        amount=amount,
        from_agent_tier=agent.tier.value,
        urgency=req.urgency
    )

    # Confirm immediately (hub takes the risk)
    collector.confirm_hub_route(route["payment_id"])

    # Queue webhook
    background_tasks.add_task(
        queue_webhook,
        str(agent.id),
        "payment.sent",
        {
            "payment_id": route["payment_id"],
            "amount": float(amount),
            "to_agent": req.to_agent_name,
            "fee": float(fee_info["fee_amount"])
        }
    )

    return {
        "payment_id": route["payment_id"],
        "status": "confirmed",
        "payment_type": "hub_routing",
        "recipient": {
            "agent_name": recipient.agent_name,
            "address": recipient.xmr_address,
            "trust_score": recipient.trust_score
        },
        "amount": float(amount),
        "fee": float(fee_info["fee_amount"]),
        "fee_percent": float(fee_info["fee_percent"]),
        "total_deducted": float(total_deduction),
        "confirmed_at": datetime.utcnow().isoformat()
    }


@app.get("/v2/payments/history")
async def get_payment_history(
    direction: Optional[str] = None,
    limit: int = 50,
    agent: Agent = Depends(get_current_agent)
):
    """Get payment history"""
    with get_db() as db:
        repo = TransactionRepository(db)
        txs = repo.list_by_agent(
            agent_id=agent.id,
            direction=direction,
            limit=limit
        )
        
        return [
            {
                "tx_hash": tx.tx_hash,
                "network": tx.network,
                "amount": float(tx.amount),
                "fee": float(tx.fee),
                "fee_collected": float(tx.fee_collected),
                "payment_type": tx.payment_type.value,
                "status": tx.status.value,
                "memo": tx.memo,
                "created_at": tx.created_at.isoformat()
            }
            for tx in txs
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# BALANCE
# ═══════════════════════════════════════════════════════════════════════════════


class DepositRequest(BaseModel):
    amount: float = Field(gt=0, le=10000, description="Amount to deposit")


class WithdrawRequest(BaseModel):
    amount: float = Field(gt=0, le=10000, description="Amount to withdraw")
    address: str = Field(min_length=10, max_length=200, description="XMR address to withdraw to")


@app.get("/v2/balance")
async def get_balance(agent: Agent = Depends(get_current_agent)):
    """Get agent's hub balance"""
    with get_db() as db:
        repo = BalanceRepository(db)
        balance = repo.get_or_create(agent.id)
        return {
            "available": float(balance.available or 0),
            "pending": float(balance.pending or 0),
            "total_deposited": float(balance.total_deposited or 0),
            "total_withdrawn": float(balance.total_withdrawn or 0),
            "deposit_address": balance.deposit_address,
            "token": "XMR"
        }


@app.post("/v2/balance/deposit")
async def deposit_balance(
    req: DepositRequest,
    agent: Agent = Depends(get_current_agent)
):
    """
    Deposit XMR to hub balance.
    On stagenet: direct credit for testing.
    On mainnet: would verify on-chain deposit to agent's subaddress.
    """
    from decimal import Decimal
    amount = Decimal(str(req.amount))

    with get_db() as db:
        repo = BalanceRepository(db)
        balance = repo.deposit(agent.id, amount)
        return {
            "status": "deposited",
            "amount": float(amount),
            "new_balance": float(balance.available),
            "token": "XMR"
        }


@app.post("/v2/balance/withdraw")
async def withdraw_balance(
    req: WithdrawRequest,
    agent: Agent = Depends(get_current_agent)
):
    """Withdraw XMR from hub balance to external address"""
    from decimal import Decimal
    amount = Decimal(str(req.amount))

    with get_db() as db:
        repo = BalanceRepository(db)
        available = repo.get_available(agent.id)
        if available < amount:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient balance: {float(available)} XMR available"
            )
        repo.deduct(agent.id, amount)
        balance = repo.get_or_create(agent.id)
        balance.total_withdrawn = (balance.total_withdrawn or Decimal("0")) + amount

    return {
        "status": "withdrawn",
        "amount": float(amount),
        "to_address": req.address,
        "remaining_balance": float(balance.available),
        "token": "XMR"
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ESCROW (DISABLED)
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/v2/escrow/create")
async def create_escrow():
    """Escrow is not available in this version"""
    raise HTTPException(status_code=501, detail="Escrow not available. Use hub routing for payments.")


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/v2/admin/stats")
async def get_admin_stats(admin_key: str = Header(None)):
    """Get admin statistics"""
    expected_key = os.getenv("ADMIN_API_KEY")
    if not expected_key or admin_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")
    
    registry = get_registry()
    collector = get_fee_collector()
    webhook_service = get_webhook_service()
    monitor = get_monitor()
    
    return {
        "agents": registry.get_stats(),
        "revenue": collector.get_revenue_stats(days=30),
        "webhooks": webhook_service.get_delivery_stats(days=7),
        "health": monitor.get_health_report(),
        "alerts": [
            {
                "id": a.id,
                "severity": a.severity.value,
                "title": a.title,
                "timestamp": a.timestamp.isoformat()
            }
            for a in monitor.get_alerts(unacknowledged_only=True)[:10]
        ]
    }


@app.post("/v2/admin/agents/{agent_id}/verify")
async def verify_agent(
    agent_id: str,
    tier: str = "verified",
    admin_key: str = Header(None)
):
    """Verify agent (admin only)"""
    expected_key = os.getenv("ADMIN_API_KEY")
    if not expected_key or admin_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")
    
    registry = get_registry()
    
    try:
        result = registry.verify_agent(
            agent_id=agent_id,
            verified_by="admin",
            tier=tier
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
