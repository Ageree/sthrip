"""Admin dashboard views — server-side rendered with Jinja2 + Tailwind."""

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Response, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from sthrip.db.database import get_db
from sthrip.db.models import Agent, AgentBalance, AgentReputation, HubRoute

_TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

router = APIRouter(prefix="/admin", tags=["admin-ui"])

# Session store: token -> {"expires": timestamp}
_sessions: dict = {}

_SESSION_TTL = 8 * 3600  # 8 hours


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _verify_admin_key(key: str) -> bool:
    """Check admin key against env var using constant-time comparison."""
    expected = os.getenv("ADMIN_API_KEY", "")
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
    session = _sessions.get(token)
    if not session:
        return False
    if session["expires"] < time.time():
        _sessions.pop(token, None)
        return False
    return True


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
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login_submit(request: Request, admin_key: str = Form(...)):
    """Validate admin key and set session cookie."""
    if not _verify_admin_key(admin_key):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid admin key"},
            status_code=401,
        )
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"expires": time.time() + _SESSION_TTL}
    response = RedirectResponse(url="/admin/", status_code=303)
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=_SESSION_TTL,
    )
    return response


@router.get("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    token = _get_session_token(request)
    if token:
        _sessions.pop(token, None)
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
        from datetime import datetime, timedelta
        cutoff = datetime.utcnow() - timedelta(hours=24)
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
):
    """List agents with optional search and tier filter."""
    _require_auth(request)

    with get_db() as db:
        query = db.query(Agent)
        if search:
            query = query.filter(Agent.agent_name.ilike(f"%{search}%"))
        if tier:
            query = query.filter(Agent.tier == tier)
        agents = query.order_by(Agent.created_at.desc()).limit(100).all()

    return templates.TemplateResponse("agents.html", {
        "request": request,
        "agents": agents,
        "search": search,
        "tier": tier,
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
):
    """List recent transactions."""
    _require_auth(request)

    with get_db() as db:
        query = db.query(HubRoute)
        if status:
            query = query.filter(HubRoute.status == status)
        transactions = query.order_by(HubRoute.created_at.desc()).limit(100).all()

        # Eager-load agent names
        agent_ids = set()
        for tx in transactions:
            agent_ids.add(tx.from_agent_id)
            agent_ids.add(tx.to_agent_id)
        agents_map = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = a
        for tx in transactions:
            tx.from_agent = agents_map.get(tx.from_agent_id)
            tx.to_agent = agents_map.get(tx.to_agent_id)

    return templates.TemplateResponse("transactions.html", {
        "request": request,
        "transactions": transactions,
        "status": status,
    })


@router.get("/balances", response_class=HTMLResponse)
async def balances_list(request: Request):
    """List all agent balances."""
    _require_auth(request)

    with get_db() as db:
        balances = db.query(AgentBalance).order_by(
            AgentBalance.available.desc()
        ).limit(100).all()

        # Load agent names
        agent_ids = [b.agent_id for b in balances]
        agents_map = {}
        if agent_ids:
            for a in db.query(Agent).filter(Agent.id.in_(agent_ids)).all():
                agents_map[a.id] = a
        for b in balances:
            b.agent = agents_map.get(b.agent_id)

    return templates.TemplateResponse("balances.html", {
        "request": request,
        "balances": balances,
    })


def setup_admin_ui(app) -> None:
    """Register admin UI router and exception handler on the app."""
    app.include_router(router)

    @app.exception_handler(_AuthRequired)
    async def _auth_redirect(request: Request, exc: _AuthRequired):
        return RedirectResponse(url="/admin/login", status_code=303)
