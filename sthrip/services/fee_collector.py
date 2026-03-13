"""
Fee collection service for hub-routed payments
Supports multiple fee models and revenue tracking
"""

import hashlib
import secrets
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Dict, List
from uuid import UUID
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import func

from ..db.database import get_db
from ..db.models import HubRoute, HubRouteStatus, FeeCollection, FeeCollectionStatus
from ..db.repository import AgentRepository

class FeeType(Enum):
    """Types of fees we collect"""
    HUB_ROUTING = "hub_routing"       # 0.1-0.3% for instant routing
    ESCROW = "escrow"                  # 0.5-1% for escrow service
    CROSS_CHAIN = "cross_chain"        # 0.3-0.5% for bridge
    VERIFIED_BADGE = "verified_badge"  # $29/month subscription
    PREMIUM_DISCOVERY = "premium_discovery"  # $99/month
    API_CALLS = "api_calls"            # $0.001 per reputation check


@dataclass
class FeeConfig:
    """Fee configuration"""
    fee_type: FeeType
    percent: Decimal  # For percentage-based fees
    fixed_amount: Decimal  # For fixed fees
    min_fee: Decimal
    max_fee: Decimal


# Default fee configurations
DEFAULT_FEES: Dict[FeeType, FeeConfig] = {
    FeeType.HUB_ROUTING: FeeConfig(
        fee_type=FeeType.HUB_ROUTING,
        percent=Decimal("0.001"),  # 0.1%
        fixed_amount=Decimal("0"),
        min_fee=Decimal("0.0001"),  # Minimum 0.0001 XMR
        max_fee=Decimal("1.0")      # Maximum 1 XMR
    ),
    FeeType.ESCROW: FeeConfig(
        fee_type=FeeType.ESCROW,
        percent=Decimal("0.01"),   # 1%
        fixed_amount=Decimal("0"),
        min_fee=Decimal("0.001"),
        max_fee=Decimal("10.0")
    ),
    FeeType.CROSS_CHAIN: FeeConfig(
        fee_type=FeeType.CROSS_CHAIN,
        percent=Decimal("0.005"),  # 0.5%
        fixed_amount=Decimal("0"),
        min_fee=Decimal("0.0001"),
        max_fee=Decimal("5.0")
    ),
    FeeType.API_CALLS: FeeConfig(
        fee_type=FeeType.API_CALLS,
        percent=Decimal("0"),
        fixed_amount=Decimal("0.001"),  # $0.001 per call
        min_fee=Decimal("0.001"),
        max_fee=Decimal("0.001")
    ),
}


class FeeCollector:
    """
    Fee collection for hub-routed payments
    
    Fee Model:
    - P2P Direct: 0% (free)
    - Hub Routing: 0.1% (for speed)
    - Escrow: 1% (for protection)
    - Cross-chain: 0.5%
    - API calls: $0.001 per call
    """
    
    def __init__(self, db_session=None):
        self.db = db_session
        self.fee_wallet_address = None  # Set from config
    
    def calculate_hub_routing_fee(
        self,
        amount: Decimal,
        from_agent_tier: str = "free",
        urgency: str = "normal"
    ) -> Dict:
        """
        Calculate fee for hub-routed payment
        
        Args:
            amount: Payment amount
            from_agent_tier: Agent tier (premium gets discounts)
            urgency: 'normal' or 'urgent' (urgent = higher fee)
        
        Returns:
            Fee breakdown
        """
        config = DEFAULT_FEES[FeeType.HUB_ROUTING]
        
        # Base fee calculation
        fee_percent = config.percent
        
        # Tier discounts
        if from_agent_tier == "premium":
            fee_percent = fee_percent * Decimal("0.5")  # 50% off
        elif from_agent_tier == "verified":
            fee_percent = fee_percent * Decimal("0.75")  # 25% off
        
        # Urgency premium
        if urgency == "urgent":
            fee_percent = fee_percent * Decimal("2.0")
        
        # Calculate fee
        fee_amount = amount * fee_percent
        
        # Apply min/max, but never let fee exceed the payment amount
        fee_amount = max(fee_amount, config.min_fee)
        fee_amount = min(fee_amount, config.max_fee)
        fee_amount = min(fee_amount, amount)
        
        return {
            "base_amount": amount,
            "fee_amount": fee_amount,
            "fee_percent": fee_percent,
            "tier_discount": from_agent_tier,
            "urgency": urgency,
            "total_deduction": amount + fee_amount,  # Total from sender
            "recipient_receives": amount  # What recipient gets
        }
    
    def calculate_escrow_fee(
        self,
        amount: Decimal,
        use_arbiter: bool = True
    ) -> Dict:
        """Calculate escrow fees"""
        config = DEFAULT_FEES[FeeType.ESCROW]
        
        platform_fee = amount * config.percent
        platform_fee = max(platform_fee, config.min_fee)
        platform_fee = min(platform_fee, config.max_fee)
        
        # Arbiter fee (if used)
        arbiter_fee = Decimal("0")
        if use_arbiter:
            arbiter_fee = amount * Decimal("0.005")  # 0.5%
            arbiter_fee = min(arbiter_fee, Decimal("1.0"))
        
        total_fee = platform_fee + arbiter_fee
        
        return {
            "escrow_amount": amount,
            "platform_fee": platform_fee,
            "platform_fee_percent": config.percent,
            "arbiter_fee": arbiter_fee,
            "total_fee": total_fee,
            "buyer_deposits": amount + total_fee,
            "seller_receives": amount
        }
    
    def create_hub_route(
        self,
        from_agent_id: str,
        to_agent_id: str,
        amount: Decimal,
        token: str = "XMR",
        from_agent_tier: str = "free",
        urgency: str = "normal",
        idempotency_key: Optional[str] = None,
        db=None,
    ) -> Dict:
        """
        Create hub-routed payment with fee

        Returns:
            Route details with fee breakdown
        """
        # Calculate fee
        fee_breakdown = self.calculate_hub_routing_fee(
            amount, from_agent_tier, urgency
        )

        # Generate payment ID — deterministic if idempotency key provided
        if idempotency_key:
            import hashlib
            raw = f"{from_agent_id}:{to_agent_id}:{amount}:{idempotency_key}"
            payment_id = f"hp_{hashlib.sha256(raw.encode()).hexdigest()[:32]}"
        else:
            payment_id = f"hp_{secrets.token_hex(16)}"

        # Use provided session or create new one
        def _execute(session):
            # Check for duplicate (idempotent replay)
            existing = session.query(HubRoute).filter(HubRoute.payment_id == payment_id).first()
            if existing:
                return {
                    "route_id": str(existing.id),
                    "payment_id": payment_id,
                    "from_agent_id": from_agent_id,
                    "to_agent_id": to_agent_id,
                    "amount": existing.amount,
                    "token": existing.token,
                    "fee": fee_breakdown,
                    "status": existing.status.value,
                    "created_at": existing.created_at.isoformat() if existing.created_at else "",
                    "duplicate": True,
                }

            _from = UUID(from_agent_id) if isinstance(from_agent_id, str) else from_agent_id
            _to = UUID(to_agent_id) if isinstance(to_agent_id, str) else to_agent_id
            route = HubRoute(
                payment_id=payment_id,
                from_agent_id=_from,
                to_agent_id=_to,
                amount=amount,
                token=token,
                fee_percent=fee_breakdown["fee_percent"],
                fee_amount=fee_breakdown["fee_amount"],
                fee_collected=False,
                instant_confirmation=(urgency == "urgent"),
                status=HubRouteStatus.PENDING
            )
            session.add(route)
            session.flush()

            return {
                "route_id": str(route.id),
                "payment_id": payment_id,
                "from_agent_id": from_agent_id,
                "to_agent_id": to_agent_id,
                "amount": amount,
                "token": token,
                "fee": fee_breakdown,
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat()
            }

        if db is not None:
            return _execute(db)
        with get_db() as session:
            return _execute(session)
    
    def confirm_hub_route(self, payment_id: str, settlement_tx_hash: Optional[str] = None, db=None) -> Dict:
        """
        Confirm hub route and collect fee.

        Accepts optional db session for transactional use.
        """
        def _execute(db):
            route = db.query(HubRoute).filter(
                HubRoute.payment_id == payment_id
            ).with_for_update().first()
            
            if not route:
                raise ValueError(f"Route not found: {payment_id}")
            
            if route.status != HubRouteStatus.PENDING:
                raise ValueError(f"Route already {route.status.value}")
            
            # Update route status
            route.status = HubRouteStatus.CONFIRMED
            route.confirmed_at = datetime.now(timezone.utc)
            route.settlement_tx_hash = settlement_tx_hash
            
            # Mark fee as collected
            route.fee_collected = True
            route.fee_collected_at = datetime.now(timezone.utc)
            
            # Record fee collection
            fee_record = FeeCollection(
                source_type=FeeType.HUB_ROUTING.value,
                source_id=route.id,
                amount=route.fee_amount,
                token=route.token,
                status=FeeCollectionStatus.PENDING
            )
            db.add(fee_record)
            
            return {
                "payment_id": payment_id,
                "status": "confirmed",
                "fee_collected": route.fee_amount,
                "confirmed_at": route.confirmed_at.isoformat()
            }

        if db is not None:
            return _execute(db)
        with get_db() as session:
            return _execute(session)

    def settle_hub_route(self, payment_id: str, settlement_tx_hash: str, db=None) -> Dict:
        """
        Final settlement of hub route on-chain.

        Uses with_for_update() to prevent double settlement from
        concurrent calls.

        Accepts optional db session for transactional use.
        """
        def _execute(db):
            route = db.query(HubRoute).filter(
                HubRoute.payment_id == payment_id
            ).with_for_update().first()

            if not route:
                raise ValueError(f"Route not found: {payment_id}")

            if route.status == HubRouteStatus.SETTLED:
                raise ValueError(f"Route already settled: {payment_id}")

            if route.status != HubRouteStatus.CONFIRMED:
                raise ValueError(
                    f"Route must be confirmed before settlement, "
                    f"current status: {route.status.value}"
                )

            route.status = HubRouteStatus.SETTLED
            route.settled_at = datetime.now(timezone.utc)
            route.settlement_tx_hash = settlement_tx_hash

            return {
                "payment_id": payment_id,
                "status": "settled",
                "settlement_tx": settlement_tx_hash,
                "settled_at": route.settled_at.isoformat()
            }

        if db is not None:
            return _execute(db)
        with get_db() as session:
            return _execute(session)
    
    def get_revenue_stats(self, days: int = 30) -> Dict:
        """Get revenue statistics"""
        from sqlalchemy import func
        from datetime import timedelta
        
        since = datetime.now(timezone.utc) - timedelta(days=days)
        
        with get_db() as db:
            # Hub routing revenue
            hub_revenue = db.query(func.sum(HubRoute.fee_amount)).filter(
                HubRoute.fee_collected == True,
                HubRoute.fee_collected_at >= since
            ).scalar() or Decimal('0')
            
            # Escrow revenue
            escrow_revenue = db.query(func.sum(FeeCollection.amount)).filter(
                FeeCollection.source_type == FeeType.ESCROW.value,
                FeeCollection.created_at >= since
            ).scalar() or Decimal('0')
            
            # API calls revenue
            api_revenue = db.query(func.sum(FeeCollection.amount)).filter(
                FeeCollection.source_type == FeeType.API_CALLS.value,
                FeeCollection.created_at >= since
            ).scalar() or Decimal('0')
            
            # Total routes
            total_routes = db.query(HubRoute).filter(
                HubRoute.created_at >= since
            ).count()
            
            return {
                "period_days": days,
                "hub_routing_revenue_xmr": str(hub_revenue),
                "escrow_revenue_xmr": str(escrow_revenue),
                "api_calls_revenue_usd": str(api_revenue),
                "total_routes": total_routes,
                "average_fee_per_route": str(hub_revenue / total_routes) if total_routes > 0 else 0
            }
    
    def get_pending_fees(self, token: str = "XMR") -> List[Dict]:
        """Get pending fees ready for withdrawal"""
        with get_db() as db:
            pending = db.query(FeeCollection).filter(
                FeeCollection.status == FeeCollectionStatus.PENDING,
                FeeCollection.token == token
            ).all()
            
            return [
                {
                    "id": str(fee.id),
                    "source_type": fee.source_type,
                    "amount": str(fee.amount),
                    "token": fee.token,
                    "created_at": fee.created_at.isoformat()
                }
                for fee in pending
            ]
    
    def withdraw_fees(self, fee_ids: List[str], tx_hash: str) -> Dict:
        """Mark fees as withdrawn. Locks rows first to prevent TOCTOU."""
        with get_db() as db:
            now = datetime.now(timezone.utc)
            # Lock rows with FOR UPDATE before computing total
            rows = db.query(FeeCollection).filter(
                FeeCollection.id.in_(fee_ids),
                FeeCollection.status == FeeCollectionStatus.PENDING,
            ).with_for_update().all()
            total = sum(r.amount for r in rows)
            count = len(rows)
            for r in rows:
                r.status = FeeCollectionStatus.WITHDRAWN
                r.collection_tx_hash = tx_hash
                r.withdrawn_at = now
            return {
                "withdrawn_fees": count,
                "total_amount": str(total),
                "tx_hash": tx_hash,
            }


# Global instance
_collector: Optional[FeeCollector] = None
_collector_lock = threading.Lock()


def get_fee_collector() -> FeeCollector:
    """Get global fee collector"""
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = FeeCollector()
    return _collector
