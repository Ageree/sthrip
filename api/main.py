"""
StealthPay REST API for AI Agents
HTTP interface for agents that can't use Python/TS SDK directly
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
import os
import uuid
from datetime import datetime

from stealthpay import StealthPay
from stealthpay.types import PaymentStatus, EscrowStatus, ChannelStatus
from stealthpay.privacy import PrivacyConfig, TransactionTiming

app = FastAPI(
    title="StealthPay API",
    description="Anonymous payments API for AI Agents",
    version="0.1.0"
)

security = HTTPBearer()

# In-memory store (use Redis in production)
agents: dict = {}
sessions: dict = {}

# Agent authentication (simple API key for now)
def verify_agent(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    if token not in agents:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return agents[token]


# ============ MODELS ============

class AgentRegistration(BaseModel):
    agent_name: str = Field(..., description="Unique agent identifier")
    webhook_url: Optional[str] = Field(None, description="Webhook for notifications")
    privacy_level: Literal["low", "medium", "high", "paranoid"] = "medium"

class PaymentRequest(BaseModel):
    to_address: str = Field(..., description="Recipient Monero address")
    amount: float = Field(..., gt=0, description="Amount in XMR")
    memo: Optional[str] = Field(None, description="Private memo")
    privacy_level: Optional[Literal["low", "medium", "high", "paranoid"]] = None

class StealthAddressResponse(BaseModel):
    address: str
    index: int
    created_at: datetime

class PaymentResponse(BaseModel):
    tx_hash: str
    amount: float
    to_address: str
    status: str
    fee: float
    timestamp: datetime

class BalanceResponse(BaseModel):
    address: str
    balance: float
    unlocked_balance: float
    height: int

class EscrowCreateRequest(BaseModel):
    seller_address: str
    arbiter_address: str
    amount: float
    description: str
    timeout_hours: int = 48

class EscrowActionRequest(BaseModel):
    action: Literal["fund", "release", "dispute", "arbitrate"]
    reason: Optional[str] = None
    decision: Optional[Literal["release", "refund"]] = None


# ============ ENDPOINTS ============

@app.post("/agents/register", response_model=dict)
async def register_agent(reg: AgentRegistration):
    """Register new agent and get API key"""
    api_key = f"sk_{uuid.uuid4().hex}"
    
    # Initialize agent wallet
    agent = StealthPay.from_env()
    
    agents[api_key] = {
        "name": reg.agent_name,
        "webhook": reg.webhook_url,
        "privacy_level": reg.privacy_level,
        "wallet": agent,
        "created_at": datetime.utcnow()
    }
    
    return {
        "api_key": api_key,
        "agent_name": reg.agent_name,
        "address": agent.address,
        "privacy_level": reg.privacy_level,
        "message": "Store this API key securely - it cannot be retrieved again"
    }

@app.get("/balance", response_model=BalanceResponse)
async def get_balance(agent: dict = Depends(verify_agent)):
    """Get agent's wallet balance"""
    wallet = agent["wallet"]
    info = wallet.get_info()
    
    return BalanceResponse(
        address=info.address,
        balance=info.balance,
        unlocked_balance=info.unlocked_balance,
        height=info.height
    )

@app.post("/addresses/stealth", response_model=StealthAddressResponse)
async def create_stealth_address(
    purpose: Optional[str] = "api-payment",
    agent: dict = Depends(verify_agent)
):
    """Generate new stealth address for receiving payment"""
    wallet = agent["wallet"]
    stealth = wallet.create_stealth_address(purpose=purpose)
    
    return StealthAddressResponse(
        address=stealth.address,
        index=stealth.index,
        created_at=stealth.created_at or datetime.utcnow()
    )

@app.post("/payments/send", response_model=PaymentResponse)
async def send_payment(
    req: PaymentRequest,
    agent: dict = Depends(verify_agent)
):
    """Send anonymous payment"""
    wallet = agent["wallet"]
    
    # Use agent's default privacy level if not specified
    privacy = req.privacy_level or agent.get("privacy_level", "medium")
    
    try:
        payment = wallet.pay(
            to_address=req.to_address,
            amount=req.amount,
            memo=req.memo,
            privacy_level=privacy
        )
        
        return PaymentResponse(
            tx_hash=payment.tx_hash,
            amount=payment.amount,
            to_address=payment.to_address,
            status=payment.status.value,
            fee=payment.fee,
            timestamp=payment.timestamp
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/payments/history", response_model=List[PaymentResponse])
async def get_payment_history(
    limit: int = 10,
    incoming: bool = True,
    outgoing: bool = True,
    agent: dict = Depends(verify_agent)
):
    """Get payment history"""
    wallet = agent["wallet"]
    payments = wallet.get_payments(
        incoming=incoming,
        outgoing=outgoing,
        limit=limit
    )
    
    return [
        PaymentResponse(
            tx_hash=p.tx_hash,
            amount=p.amount,
            to_address=p.to_address,
            status=p.status.value,
            fee=p.fee,
            timestamp=p.timestamp
        )
        for p in payments
    ]

# ============ ESCROW ============

@app.post("/escrow/create", response_model=dict)
async def create_escrow(
    req: EscrowCreateRequest,
    agent: dict = Depends(verify_agent)
):
    """Create 2-of-3 escrow deal"""
    wallet = agent["wallet"]
    
    escrow = wallet.create_escrow(
        seller_address=req.seller_address,
        arbiter_address=req.arbiter_address,
        amount=req.amount,
        description=req.description,
        timeout_hours=req.timeout_hours
    )
    
    return {
        "escrow_id": escrow.id,
        "status": escrow.status.value,
        "amount": escrow.amount,
        "buyer": escrow.buyer.address,
        "seller": escrow.seller.address,
        "arbiter": escrow.arbiter.address,
        "created_at": escrow.created_at
    }

@app.post("/escrow/{escrow_id}/action", response_model=dict)
async def escrow_action(
    escrow_id: str,
    req: EscrowActionRequest,
    agent: dict = Depends(verify_agent)
):
    """Perform action on escrow"""
    wallet = agent["wallet"]
    
    try:
        if req.action == "fund":
            # In production: handle multisig funding
            result = {"status": "funded", "note": "Implement multisig funding"}
        elif req.action == "release":
            escrow = wallet.release_escrow(escrow_id)
            result = {"status": escrow.status.value}
        elif req.action == "dispute":
            escrow = wallet.dispute_escrow(escrow_id, req.reason or "Dispute opened")
            result = {"status": escrow.status.value, "reason": req.reason}
        elif req.action == "arbitrate":
            result = {"status": "arbitrated", "decision": req.decision}
        else:
            raise HTTPException(status_code=400, detail="Invalid action")
        
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/escrow/{escrow_id}", response_model=dict)
async def get_escrow(
    escrow_id: str,
    agent: dict = Depends(verify_agent)
):
    """Get escrow details"""
    wallet = agent["wallet"]
    escrow = wallet.get_escrow(escrow_id)
    
    if not escrow:
        raise HTTPException(status_code=404, detail="Escrow not found")
    
    return {
        "id": escrow.id,
        "status": escrow.status.value,
        "amount": escrow.amount,
        "description": escrow.description,
        "buyer": escrow.buyer.address,
        "seller": escrow.seller.address,
        "arbiter": escrow.arbiter.address
    }

# ============ WEBHOOKS ============

@app.post("/webhooks/configure")
async def configure_webhook(
    webhook_url: str,
    events: List[str],
    agent: dict = Depends(verify_agent)
):
    """Configure webhook for agent notifications"""
    agent["webhook"] = webhook_url
    agent["webhook_events"] = events
    
    return {
        "webhook_url": webhook_url,
        "events": events,
        "status": "configured"
    }


# ============ HEALTH ============

@app.get("/health")
async def health_check():
    """API health check"""
    return {
        "status": "healthy",
        "version": "0.1.0",
        "agents_registered": len(agents)
    }

@app.get("/")
async def root():
    return {
        "name": "StealthPay API",
        "version": "0.1.0",
        "description": "Anonymous payments for AI Agents",
        "docs": "/docs",
        "endpoints": {
            "agents": "/agents/register",
            "balance": "/balance",
            "payments": "/payments/send",
            "escrow": "/escrow/create",
            "stealth": "/addresses/stealth"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
