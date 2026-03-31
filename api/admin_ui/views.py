"""Admin dashboard views — server-side rendered with Jinja2 + Tailwind."""

import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Response, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from sthrip.db.database import get_db
from sthrip.db.models import (
    Agent, AgentBalance, AgentReputation, HubRoute, AgentTier, HubRouteStatus,
    EscrowDeal, EscrowStatus, SpendingPolicy, MultisigEscrow, MessageRelay,
    WebhookEndpoint,
)
from sthrip.config import get_settings
from sthrip.utils import escape_ilike
from api.helpers import get_client_ip
from api.session_store import get_session_store

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin-ui"])


# ═══════════════════════════════════════════════════════════════════════════════
# ORM → DICT SERIALIZERS
# ═══════════════════════════════════════════════════════════════════════════════
# All views must convert ORM objects to plain dicts *inside* the session scope
# to avoid DetachedInstanceError on lazy-loaded relationships.

def _serialize_agent(agent: Agent) -> dict:
    """Convert Agent ORM instance to a plain dict for template use.

    Keeps native Python types (datetime, enum, UUID, Decimal) so Jinja2
    templates can call .strftime(), .value, etc. without changes.
    """
    return {
        "id": agent.id,
        "agent_name": agent.agent_name,
        "tier": agent.tier,
        "xmr_address": agent.xmr_address,
        "webhook_url": agent.webhook_url,
        "is_active": getattr(agent, "is_active", True),
        "created_at": agent.created_at,
        "last_seen_at": agent.last_seen_at,
        "privacy_level": agent.privacy_level,
    }


def _serialize_balance(balance: Optional[AgentBalance]) -> Optional[dict]:
    """Convert AgentBalance ORM instance to a plain dict."""
    if not balance:
        return None
    return {
        "available": balance.available or 0,
        "pending": balance.pending or 0,
        "total_deposited": balance.total_deposited or 0,
        "total_withdrawn": balance.total_withdrawn or 0,
        "deposit_address": balance.deposit_address,
    }


def _serialize_reputation(reputation: Optional[AgentReputation]) -> Optional[dict]:
    """Convert AgentReputation ORM instance to a plain dict."""
    if not reputation:
        return None
    return {
        "trust_score": reputation.trust_score,
        "total_transactions": reputation.total_transactions,
        "average_rating": float(reputation.average_rating) if reputation.average_rating else 0.0,
    }


def _serialize_hub_route(tx: HubRoute) -> dict:
    """Convert HubRoute ORM instance to a plain dict."""
    return {
        "payment_id": tx.payment_id,
        "from_agent_id": tx.from_agent_id,
        "to_agent_id": tx.to_agent_id,
        "amount": tx.amount,
        "fee_amount": tx.fee_amount,
        "status": tx.status,
        "created_at": tx.created_at,
    }

def _serialize_escrow(deal: "EscrowDeal") -> dict:
    """Convert EscrowDeal ORM instance to a plain dict."""
    return {
        "id": deal.id,
        "deal_hash": deal.deal_hash,
        "buyer_id": deal.buyer_id,
        "seller_id": deal.seller_id,
        "amount": deal.amount,
        "token": deal.token,
        "description": deal.description,
        "fee_percent": deal.fee_percent,
        "fee_amount": deal.fee_amount,
        "release_amount": deal.release_amount,
        "status": deal.status,
        "accept_timeout_hours": deal.accept_timeout_hours,
        "delivery_timeout_hours": deal.delivery_timeout_hours,
        "review_timeout_hours": deal.review_timeout_hours,
        "accept_deadline": deal.accept_deadline,
        "delivery_deadline": deal.delivery_deadline,
        "review_deadline": deal.review_deadline,
        "deal_metadata": deal.deal_metadata,
        "created_at": deal.created_at,
        "accepted_at": deal.accepted_at,
        "delivered_at": deal.delivered_at,
        "completed_at": deal.completed_at,
        "cancelled_at": deal.cancelled_at,
        "expires_at": deal.expires_at,
    }


def _serialize_spending_policy(policy: "SpendingPolicy") -> dict:
    """Convert SpendingPolicy ORM instance to a plain dict."""
    return {
        "id": policy.id,
        "agent_id": policy.agent_id,
        "max_per_tx": policy.max_per_tx,
        "max_per_session": policy.max_per_session,
        "daily_limit": policy.daily_limit,
        "allowed_agents": policy.allowed_agents,
        "blocked_agents": policy.blocked_agents,
        "require_escrow_above": policy.require_escrow_above,
        "is_active": policy.is_active,
        "created_at": policy.created_at,
        "updated_at": policy.updated_at,
    }


def _serialize_multisig_escrow(ms: "MultisigEscrow") -> dict:
    """Convert MultisigEscrow ORM instance to a plain dict."""
    return {
        "id": ms.id,
        "escrow_deal_id": ms.escrow_deal_id,
        "multisig_address": ms.multisig_address,
        "state": ms.state,
        "fee_collected": ms.fee_collected,
        "funded_amount": ms.funded_amount,
        "funded_tx_hash": ms.funded_tx_hash,
        "release_initiator": ms.release_initiator,
        "dispute_reason": ms.dispute_reason,
        "disputed_by": ms.disputed_by,
        "timeout_at": ms.timeout_at,
        "created_at": ms.created_at,
        "updated_at": ms.updated_at,
    }


def _serialize_webhook_endpoint(ep: "WebhookEndpoint") -> dict:
    """Convert WebhookEndpoint ORM instance to a plain dict."""
    return {
        "id": ep.id,
        "agent_id": ep.agent_id,
        "url": ep.url,
        "description": ep.description,
        "event_filters": ep.event_filters,
        "is_active": ep.is_active,
        "failure_count": ep.failure_count,
        "disabled_at": ep.disabled_at,
        "created_at": ep.created_at,
        "updated_at": ep.updated_at,
    }


_SESSION_TTL = 8 * 3600  # 8 hours
_session_logger = logging.getLogger("sthrip.admin_sessions")

# Dashboard session store — shared singleton (Redis key prefix ``admin_session:``)
_session_store = get_session_store()


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _verify_admin_key(key: str) -> bool:
    """Check admin key against env var using constant-time comparison."""
    expected = get_settings().admin_api_key
    if not expected or not key:
        return False
    return hmac.compare_digest(key.encode(), expected.encode())


def _get_session_token(request: Request) -> Optional[str]:
    """Extract admin_session cookie."""
    return request.cookies.get("admin_session")


def _is_authenticated(request: Request) -> bool:
    """Check if current request has a valid admin session."""
    token = _get_session_token(request)
    if not token:
        return False
    return bool(_session_store.get_session(token))


class _AuthRequired(Exception):
    """Raised when admin session is missing/expired."""
    pass


def _require_auth(request: Request) -> None:
    """Raise redirect if not authenticated."""
    if not _is_authenticated(request):
        raise _AuthRequired()


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Show login form."""
    csrf_token = _session_store.create_csrf_token()
    return templates.TemplateResponse(request, "login.html", {"error": None, "csrf_token": csrf_token})


@router.post("/login")
async def login_submit(request: Request, admin_key: str = Form(...), csrf_token: str = Form("")):
    """Validate admin key and set session cookie."""
    if not _session_store.verify_csrf_token(csrf_token):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid form submission", "csrf_token": _session_store.create_csrf_token()},
            status_code=403,
        )

    from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded

    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()

    # Check if already rate-limited before verifying credentials
    try:
        limiter.check_failed_auth(client_ip, limit=5, window=300)
    except RateLimitExceeded:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many login attempts. Try again later.", "csrf_token": _session_store.create_csrf_token()},
            status_code=429,
        )

    if not _verify_admin_key(admin_key):
        # Atomically increment counter on failed authentication
        limiter.record_failed_auth(client_ip, window=300)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid admin key", "csrf_token": _session_store.create_csrf_token()},
            status_code=401,
        )
    token = secrets.token_urlsafe(32)
    _session_store.set_session(token, _SESSION_TTL)
    is_secure = get_settings().environment != "dev"
    response = RedirectResponse(url="/admin/", status_code=303)
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        secure=is_secure,
        samesite="strict",
        max_age=_SESSION_TTL,
        path="/admin",
    )
    return response


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    """Clear session and redirect to login."""
    if not _session_store.verify_csrf_token(csrf_token):
        # Invalid CSRF — redirect to login without destroying session
        return RedirectResponse(url="/admin/login", status_code=303)

    token = _get_session_token(request)
    if token:
        _session_store.delete_session(token)
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_session", path="/admin")
    return response


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD PAGES
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    """Dashboard overview with aggregate stats."""
    _require_auth(request)

    with get_db() as db:
        total_agents = db.query(func.count(Agent.id)).scalar() or 0
        total_transactions = db.query(func.count(HubRoute.id)).scalar() or 0
        total_volume = db.query(func.coalesce(func.sum(HubRoute.amount), 0)).scalar()

        by_tier = {}
        for row in db.query(Agent.tier, func.count(Agent.id)).group_by(Agent.tier).all():
            by_tier[row[0].value if row[0] else "unknown"] = row[1]

        # Active 24h: count agents with last_seen in last 24 hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        active_24h = db.query(func.count(Agent.id)).filter(
            Agent.last_seen_at >= cutoff
        ).scalar() or 0

        # Escrow stats (graceful if table doesn't exist yet)
        total_escrows = active_escrows = completed_escrows = 0
        escrow_volume = escrow_fee_revenue = Decimal("0")
        try:
            _active_statuses = [EscrowStatus.CREATED, EscrowStatus.ACCEPTED, EscrowStatus.DELIVERED]
            total_escrows = db.query(func.count(EscrowDeal.id)).scalar() or 0
            active_escrows = db.query(func.count(EscrowDeal.id)).filter(
                EscrowDeal.status.in_(_active_statuses)
            ).scalar() or 0
            completed_escrows = db.query(func.count(EscrowDeal.id)).filter(
                EscrowDeal.status == EscrowStatus.COMPLETED
            ).scalar() or 0
            escrow_volume = db.query(
                func.coalesce(func.sum(EscrowDeal.amount), 0)
            ).scalar()
            escrow_fee_revenue = db.query(
                func.coalesce(func.sum(EscrowDeal.fee_amount), 0)
            ).filter(EscrowDeal.status == EscrowStatus.COMPLETED).scalar()
        except Exception:
            pass

        # Phase 1-2 feature stats (graceful)
        spending_policy_count = 0
        multisig_count = multisig_active = 0
        message_count = message_pending = 0
        webhook_endpoint_count = webhook_unhealthy = 0
        try:
            spending_policy_count = db.query(func.count(SpendingPolicy.id)).scalar() or 0
        except Exception:
            pass
        try:
            multisig_count = db.query(func.count(MultisigEscrow.id)).scalar() or 0
            _ms_active_states = ["setup_round_1", "setup_round_2", "setup_round_3", "funded", "active", "releasing"]
            multisig_active = db.query(func.count(MultisigEscrow.id)).filter(
                MultisigEscrow.state.in_(_ms_active_states)
            ).scalar() or 0
        except Exception:
            pass
        try:
            now = datetime.now(timezone.utc)
            message_count = db.query(func.count(MessageRelay.id)).scalar() or 0
            message_pending = db.query(func.count(MessageRelay.id)).filter(
                MessageRelay.delivered_at.is_(None),
                MessageRelay.expires_at >= now,
            ).scalar() or 0
        except Exception:
            pass
        try:
            webhook_endpoint_count = db.query(func.count(WebhookEndpoint.id)).scalar() or 0
            webhook_unhealthy = db.query(func.count(WebhookEndpoint.id)).filter(
                WebhookEndpoint.failure_count >= 3
            ).scalar() or 0
        except Exception:
            pass

    stats = {
        "total_agents": total_agents,
        "active_24h": active_24h,
        "total_transactions": total_transactions,
        "total_volume": f"{total_volume:.4f}",
        "by_tier": by_tier,
        "total_escrows": total_escrows,
        "active_escrows": active_escrows,
        "completed_escrows": completed_escrows,
        "escrow_volume": f"{escrow_volume:.4f}",
        "escrow_fee_revenue": f"{escrow_fee_revenue:.4f}",
        "spending_policy_count": spending_policy_count,
        "multisig_count": multisig_count,
        "multisig_active": multisig_active,
        "message_count": message_count,
        "message_pending": message_pending,
        "webhook_endpoint_count": webhook_endpoint_count,
        "webhook_unhealthy": webhook_unhealthy,
    }
    return templates.TemplateResponse(request, "overview.html", {"stats": stats})


@router.get("/agents", response_class=HTMLResponse)
async def agents_list(
    request: Request,
    search: str = Query(default="", max_length=100),
    tier: str = Query(default="", max_length=20),
    page: int = Query(default=1, ge=1),
):
    """List agents with optional search and tier filter."""
    _require_auth(request)
    per_page = 100
    offset = (page - 1) * per_page

    # Validate tier against allowed AgentTier values
    valid_tiers = {t.value for t in AgentTier}
    if tier and tier not in valid_tiers:
        tier = ""

    with get_db() as db:
        query = db.query(Agent)
        if search:
            query = query.filter(Agent.agent_name.ilike(f"%{escape_ilike(search)}%"))
        if tier:
            query = query.filter(Agent.tier == tier)
        total = query.count()
        agents = query.order_by(Agent.created_at.desc()).offset(offset).limit(per_page).all()
        agents_data = [_serialize_agent(a) for a in agents]

    return templates.TemplateResponse(request, "agents.html", {
        "agents": agents_data,
        "search": search,
        "tier": tier,
        "page": page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    })


@router.get("/agents/{agent_id}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_id: str):
    """Show agent detail page."""
    _require_auth(request)

    import uuid as _uuid
    try:
        parsed_id = _uuid.UUID(agent_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Agent not found")

    with get_db() as db:
        agent = db.query(Agent).filter(Agent.id == parsed_id).first()
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent_data = _serialize_agent(agent)
        balance_data = _serialize_balance(
            db.query(AgentBalance).filter(AgentBalance.agent_id == agent.id).first()
        )
        reputation_data = _serialize_reputation(
            db.query(AgentReputation).filter(AgentReputation.agent_id == agent.id).first()
        )
        transactions_data = [
            _serialize_hub_route(tx)
            for tx in db.query(HubRoute).filter(
                or_(HubRoute.from_agent_id == agent.id, HubRoute.to_agent_id == agent.id)
            ).order_by(HubRoute.created_at.desc()).limit(20).all()
        ]

    return templates.TemplateResponse(request, "agent_detail.html", {
        "agent": agent_data,
        "balance": balance_data,
        "reputation": reputation_data,
        "transactions": transactions_data,
    })


@router.get("/transactions", response_class=HTMLResponse)
async def transactions_list(
    request: Request,
    status: str = Query(default="", max_length=20),
    page: int = Query(default=1, ge=1),
):
    """List recent transactions."""
    _require_auth(request)
    per_page = 100
    offset = (page - 1) * per_page

    # Validate status against allowed HubRouteStatus values
    valid_statuses = {s.value for s in HubRouteStatus}
    if status and status not in valid_statuses:
        status = ""

    with get_db() as db:
        query = db.query(HubRoute)
        if status:
            query = query.filter(HubRoute.status == status)
        total = query.count()
        transactions = query.order_by(HubRoute.created_at.desc()).offset(offset).limit(per_page).all()

        # Resolve agent names inside the session, serialize everything to dicts
        agent_ids = set()
        for tx in transactions:
            agent_ids.add(tx.from_agent_id)
            agent_ids.add(tx.to_agent_id)
        agent_ids.discard(None)
        agents_map = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = _serialize_agent(a)

        tx_data = []
        for tx in transactions:
            tx_data.append({
                "tx": _serialize_hub_route(tx),
                "from_agent": agents_map.get(tx.from_agent_id),
                "to_agent": agents_map.get(tx.to_agent_id),
            })

    return templates.TemplateResponse(request, "transactions.html", {
        "transactions": tx_data,
        "status": status,
        "page": page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    })


@router.get("/balances", response_class=HTMLResponse)
async def balances_list(
    request: Request,
    page: int = Query(default=1, ge=1),
):
    """List all agent balances."""
    _require_auth(request)
    per_page = 100
    offset = (page - 1) * per_page

    with get_db() as db:
        total = db.query(func.count(AgentBalance.id)).scalar() or 0
        balances = db.query(AgentBalance).order_by(
            AgentBalance.available.desc()
        ).offset(offset).limit(per_page).all()

        agent_ids = [b.agent_id for b in balances]
        agents_map = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = _serialize_agent(a)

        balance_rows = [
            {
                "agent_id": b.agent_id,
                "agent": agents_map.get(b.agent_id),
                "available": b.available,
                "pending": b.pending,
                "total_deposited": b.total_deposited,
                "total_withdrawn": b.total_withdrawn,
                "updated_at": b.updated_at,
            }
            for b in balances
        ]

    return templates.TemplateResponse(request, "balances.html", {
        "balances": balance_rows,
        "page": page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# ESCROW PAGES
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/escrows", response_class=HTMLResponse)
async def escrows_list(
    request: Request,
    status: str = Query(default="", max_length=20),
    page: int = Query(default=1, ge=1),
):
    """List all escrow deals with optional status filter."""
    _require_auth(request)
    per_page = 100
    offset = (page - 1) * per_page

    valid_statuses = {s.value for s in EscrowStatus}
    if status and status not in valid_statuses:
        status = ""

    with get_db() as db:
        query = db.query(EscrowDeal)
        if status:
            query = query.filter(EscrowDeal.status == status)
        total = query.count()
        deals = query.order_by(EscrowDeal.created_at.desc()).offset(offset).limit(per_page).all()

        # Resolve agent names for buyers and sellers
        agent_ids = set()
        for deal in deals:
            agent_ids.add(deal.buyer_id)
            agent_ids.add(deal.seller_id)
        agent_ids.discard(None)
        agents_map = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = _serialize_agent(a)

        deal_rows = [
            {
                "deal": _serialize_escrow(deal),
                "buyer": agents_map.get(deal.buyer_id),
                "seller": agents_map.get(deal.seller_id),
            }
            for deal in deals
        ]

    return templates.TemplateResponse(request, "escrows.html", {
        "escrows": deal_rows,
        "status": status,
        "page": page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    })


@router.get("/escrows/{deal_id}", response_class=HTMLResponse)
async def escrow_detail(request: Request, deal_id: str):
    """Show escrow deal detail page."""
    _require_auth(request)

    import uuid as _uuid
    try:
        parsed_id = _uuid.UUID(deal_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Escrow deal not found")

    with get_db() as db:
        deal = db.query(EscrowDeal).filter(EscrowDeal.id == parsed_id).first()
        if not deal:
            raise HTTPException(status_code=404, detail="Escrow deal not found")

        deal_data = _serialize_escrow(deal)
        buyer_data = _serialize_agent(
            db.query(Agent).filter(Agent.id == deal.buyer_id).first()
        ) if deal.buyer_id else None
        seller_data = _serialize_agent(
            db.query(Agent).filter(Agent.id == deal.seller_id).first()
        ) if deal.seller_id else None

    return templates.TemplateResponse(request, "escrow_detail.html", {
        "deal": deal_data,
        "buyer": buyer_data,
        "seller": seller_data,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1-2 MONITORING PAGES
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/spending-policies", response_class=HTMLResponse)
async def spending_policies_list(
    request: Request,
    page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    """List all agents with spending policies and their current daily usage."""
    _require_auth(request)
    per_page = 100
    offset = (page - 1) * per_page

    with get_db() as db:
        total = db.query(func.count(SpendingPolicy.id)).scalar() or 0
        policies = (
            db.query(SpendingPolicy)
            .order_by(SpendingPolicy.created_at.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )

        # Resolve agent names
        agent_ids = [p.agent_id for p in policies]
        agents_map: dict = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = _serialize_agent(a)

        # Compute daily spent per agent (hub-route payments in last 24h)
        cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        daily_spent_map: dict = {}
        if agent_ids:
            daily_rows = (
                db.query(
                    HubRoute.from_agent_id,
                    func.coalesce(func.sum(HubRoute.amount), 0),
                )
                .filter(
                    HubRoute.from_agent_id.in_(agent_ids),
                    HubRoute.created_at >= cutoff_24h,
                    HubRoute.status != HubRouteStatus.FAILED,
                )
                .group_by(HubRoute.from_agent_id)
                .all()
            )
            for agent_id, total_spent in daily_rows:
                daily_spent_map[agent_id] = total_spent

        policy_rows = [
            {
                "policy": _serialize_spending_policy(p),
                "agent": agents_map.get(p.agent_id),
                "daily_spent": daily_spent_map.get(p.agent_id, Decimal("0")),
            }
            for p in policies
        ]

    return templates.TemplateResponse(request, "spending_policies.html", {
        "policies": policy_rows,
        "page": page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    })


@router.get("/multisig", response_class=HTMLResponse)
async def multisig_list(
    request: Request,
    state: str = Query(default="", max_length=30),
    page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    """List all multisig escrow deals with state machine status."""
    _require_auth(request)
    per_page = 100
    offset = (page - 1) * per_page

    # Validate state filter
    valid_states = {
        "setup_round_1", "setup_round_2", "setup_round_3",
        "funded", "active", "releasing", "completed", "cancelled", "disputed",
    }
    if state and state not in valid_states:
        state = ""

    with get_db() as db:
        query = db.query(MultisigEscrow)
        if state:
            query = query.filter(MultisigEscrow.state == state)
        total = query.count()
        multisigs = (
            query.order_by(MultisigEscrow.created_at.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )

        # Resolve escrow deal details to get buyer/seller
        deal_ids = [ms.escrow_deal_id for ms in multisigs]
        deals_map: dict = {}
        agent_ids: set = set()
        if deal_ids:
            for deal in db.query(EscrowDeal).filter(EscrowDeal.id.in_(deal_ids)).all():
                deals_map[deal.id] = _serialize_escrow(deal)
                agent_ids.add(deal.buyer_id)
                agent_ids.add(deal.seller_id)

        agent_ids.discard(None)
        agents_map: dict = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = _serialize_agent(a)

        ms_rows = []
        for ms in multisigs:
            deal_data = deals_map.get(ms.escrow_deal_id, {})
            ms_rows.append({
                "ms": _serialize_multisig_escrow(ms),
                "deal": deal_data,
                "buyer": agents_map.get(deal_data.get("buyer_id")) if deal_data else None,
                "seller": agents_map.get(deal_data.get("seller_id")) if deal_data else None,
            })

    return templates.TemplateResponse(request, "multisig.html", {
        "multisigs": ms_rows,
        "state": state,
        "page": page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    })


@router.get("/messages", response_class=HTMLResponse)
async def messages_stats(request: Request) -> HTMLResponse:
    """Aggregate stats on encrypted message relay."""
    _require_auth(request)

    now = datetime.now(timezone.utc)

    with get_db() as db:
        total_messages = db.query(func.count(MessageRelay.id)).scalar() or 0
        delivered = db.query(func.count(MessageRelay.id)).filter(
            MessageRelay.delivered_at.isnot(None)
        ).scalar() or 0
        expired = db.query(func.count(MessageRelay.id)).filter(
            MessageRelay.delivered_at.is_(None),
            MessageRelay.expires_at < now,
        ).scalar() or 0
        pending = db.query(func.count(MessageRelay.id)).filter(
            MessageRelay.delivered_at.is_(None),
            MessageRelay.expires_at >= now,
        ).scalar() or 0
        avg_size = db.query(
            func.coalesce(func.avg(MessageRelay.size_bytes), 0)
        ).scalar()

        # Messages in the last 24h
        cutoff_24h = now - timedelta(hours=24)
        messages_24h = db.query(func.count(MessageRelay.id)).filter(
            MessageRelay.created_at >= cutoff_24h
        ).scalar() or 0

        # Top 10 recent messages (for a small activity table)
        recent = (
            db.query(MessageRelay)
            .order_by(MessageRelay.created_at.desc())
            .limit(20)
            .all()
        )

        # Resolve agent names for recent messages
        agent_ids: set = set()
        for msg in recent:
            agent_ids.add(msg.from_agent_id)
            agent_ids.add(msg.to_agent_id)
        agent_ids.discard(None)
        agents_map: dict = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = _serialize_agent(a)

        recent_rows = [
            {
                "id": msg.id,
                "from_agent": agents_map.get(msg.from_agent_id),
                "to_agent": agents_map.get(msg.to_agent_id),
                "size_bytes": msg.size_bytes,
                "delivered": msg.delivered_at is not None,
                "expired": msg.delivered_at is None and msg.expires_at < now,
                "created_at": msg.created_at,
            }
            for msg in recent
        ]

    stats = {
        "total_messages": total_messages,
        "delivered": delivered,
        "pending": pending,
        "expired": expired,
        "avg_size": int(avg_size),
        "messages_24h": messages_24h,
    }
    return templates.TemplateResponse(request, "messages.html", {
        "stats": stats,
        "recent": recent_rows,
    })


@router.get("/webhook-endpoints", response_class=HTMLResponse)
async def webhook_endpoints_list(
    request: Request,
    page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    """List all registered webhook endpoints with failure counts."""
    _require_auth(request)
    per_page = 100
    offset = (page - 1) * per_page

    with get_db() as db:
        total = db.query(func.count(WebhookEndpoint.id)).scalar() or 0
        endpoints = (
            db.query(WebhookEndpoint)
            .order_by(WebhookEndpoint.failure_count.desc(), WebhookEndpoint.created_at.desc())
            .offset(offset)
            .limit(per_page)
            .all()
        )

        # Resolve agent names
        agent_ids = [ep.agent_id for ep in endpoints]
        agents_map: dict = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = _serialize_agent(a)

        endpoint_rows = [
            {
                "endpoint": _serialize_webhook_endpoint(ep),
                "agent": agents_map.get(ep.agent_id),
            }
            for ep in endpoints
        ]

    # Summary stats
    with get_db() as db:
        total_active = db.query(func.count(WebhookEndpoint.id)).filter(
            WebhookEndpoint.is_active.is_(True)
        ).scalar() or 0
        total_disabled = db.query(func.count(WebhookEndpoint.id)).filter(
            WebhookEndpoint.is_active.is_(False)
        ).scalar() or 0
        high_failure = db.query(func.count(WebhookEndpoint.id)).filter(
            WebhookEndpoint.failure_count >= 3
        ).scalar() or 0

    summary = {
        "total": total,
        "active": total_active,
        "disabled": total_disabled,
        "high_failure": high_failure,
    }

    return templates.TemplateResponse(request, "webhook_endpoints.html", {
        "endpoints": endpoint_rows,
        "summary": summary,
        "page": page,
        "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    })


@router.get("/pow", response_class=HTMLResponse)
async def pow_stats(request: Request) -> HTMLResponse:
    """Simple stats on PoW registration challenges."""
    _require_auth(request)

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    with get_db() as db:
        total_agents = db.query(func.count(Agent.id)).scalar() or 0
        registered_today = db.query(func.count(Agent.id)).filter(
            Agent.created_at >= today_start
        ).scalar() or 0

        # Registrations per day over the last 7 days
        seven_days_ago = now - timedelta(days=7)
        recent_agents = (
            db.query(Agent.created_at)
            .filter(Agent.created_at >= seven_days_ago)
            .order_by(Agent.created_at.asc())
            .all()
        )

        # Group by date
        daily_counts: dict = {}
        for (created_at,) in recent_agents:
            if created_at:
                day_key = created_at.strftime("%Y-%m-%d")
                daily_counts[day_key] = daily_counts.get(day_key, 0) + 1

        # Agents with encryption keys (indicates they completed PoW + key setup)
        agents_with_keys = db.query(func.count(Agent.id)).filter(
            Agent.encryption_public_key.isnot(None)
        ).scalar() or 0

        # Tier breakdown
        tier_counts: dict = {}
        for row in db.query(Agent.tier, func.count(Agent.id)).group_by(Agent.tier).all():
            tier_counts[row[0].value if row[0] else "unknown"] = row[1]

    stats = {
        "total_agents": total_agents,
        "registered_today": registered_today,
        "agents_with_keys": agents_with_keys,
        "daily_counts": daily_counts,
        "tier_counts": tier_counts,
    }
    return templates.TemplateResponse(request, "pow.html", {"stats": stats})


def setup_admin_ui(app) -> None:
    """Register admin UI router and exception handler on the app."""
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.exists():
        app.mount("/admin/static", StaticFiles(directory=str(_static_dir)), name="admin_static")
    app.include_router(router)

    # Make csrf_token() available in all templates as a Jinja2 global
    templates.env.globals["csrf_token"] = _session_store.create_csrf_token

    @app.exception_handler(_AuthRequired)
    async def _auth_redirect(request: Request, exc: _AuthRequired):
        response = RedirectResponse(url="/admin/login", status_code=303)
        response.delete_cookie("admin_session", path="/admin")
        return response
