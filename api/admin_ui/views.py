"""Admin dashboard views — server-side rendered with Jinja2 + Tailwind."""

import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Response, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from sthrip.db.database import get_db
from sthrip.db.models import Agent, AgentBalance, AgentReputation, HubRoute, AgentTier, HubRouteStatus
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

    stats = {
        "total_agents": total_agents,
        "active_24h": active_24h,
        "total_transactions": total_transactions,
        "total_volume": f"{total_volume:.4f}",
        "by_tier": by_tier,
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
