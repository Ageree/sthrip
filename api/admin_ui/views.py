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
from sthrip.db.models import Agent, AgentBalance, AgentReputation, HubRoute
from sthrip.config import get_settings
from sthrip.utils import escape_ilike
from api.helpers import get_client_ip
from api.session_store import get_session_store

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin-ui"])

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
    return _session_store.get_session(token)


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
    return templates.TemplateResponse("login.html", {"request": request, "error": None, "csrf_token": csrf_token})


@router.post("/login")
async def login_submit(request: Request, admin_key: str = Form(...), csrf_token: str = Form("")):
    """Validate admin key and set session cookie."""
    if not _session_store.verify_csrf_token(csrf_token):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid form submission", "csrf_token": _session_store.create_csrf_token()},
            status_code=403,
        )

    from sthrip.services.rate_limiter import get_rate_limiter, RateLimitExceeded

    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    rate_limit_kwargs = dict(
        ip_address=client_ip,
        action="admin_login",
        per_ip_limit=5,
        global_limit=50,
        window_seconds=300,
    )

    # Read-only check: reject if already over the limit (do NOT increment yet)
    try:
        limiter.check_ip_rate_limit(**rate_limit_kwargs, check_only=True)
    except RateLimitExceeded:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Too many login attempts. Try again later.", "csrf_token": _session_store.create_csrf_token()},
            status_code=429,
        )

    if not _verify_admin_key(admin_key):
        # Increment counter only on failed authentication
        try:
            limiter.check_ip_rate_limit(**rate_limit_kwargs)
        except RateLimitExceeded:
            pass  # already counted; will be blocked on next request via check_only
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid admin key", "csrf_token": _session_store.create_csrf_token()},
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
    response.delete_cookie("admin_session")
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
    return templates.TemplateResponse("overview.html", {"request": request, "stats": stats})


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

    with get_db() as db:
        query = db.query(Agent)
        if search:
            query = query.filter(Agent.agent_name.ilike(f"%{escape_ilike(search)}%"))
        if tier:
            query = query.filter(Agent.tier == tier)
        total = query.count()
        agents = query.order_by(Agent.created_at.desc()).offset(offset).limit(per_page).all()

    return templates.TemplateResponse("agents.html", {
        "request": request,
        "agents": agents,
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

        balance = db.query(AgentBalance).filter(AgentBalance.agent_id == agent.id).first()
        reputation = db.query(AgentReputation).filter(AgentReputation.agent_id == agent.id).first()

        transactions = db.query(HubRoute).filter(
            or_(HubRoute.from_agent_id == agent.id, HubRoute.to_agent_id == agent.id)
        ).order_by(HubRoute.created_at.desc()).limit(20).all()

        return templates.TemplateResponse("agent_detail.html", {
            "request": request,
            "agent": agent,
            "balance": balance,
            "reputation": reputation,
            "transactions": transactions,
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

    with get_db() as db:
        query = db.query(HubRoute)
        if status:
            query = query.filter(HubRoute.status == status)
        total = query.count()
        transactions = query.order_by(HubRoute.created_at.desc()).offset(offset).limit(per_page).all()

        # Eager-load agent names into presentation dicts (no ORM mutation)
        agent_ids = set()
        for tx in transactions:
            agent_ids.add(tx.from_agent_id)
            agent_ids.add(tx.to_agent_id)
        agents_map = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = a

        tx_data = []
        for tx in transactions:
            tx_data.append({
                "tx": tx,
                "from_agent": agents_map.get(tx.from_agent_id),
                "to_agent": agents_map.get(tx.to_agent_id),
            })

    return templates.TemplateResponse("transactions.html", {
        "request": request,
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
                agents_map[a.id] = a

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

    return templates.TemplateResponse("balances.html", {
        "request": request,
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
        response.delete_cookie("admin_session")
        return response
