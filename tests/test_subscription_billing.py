"""Phase 2 Sprint 4 — XMR subscription billing cron + grace handling tests.

Covers the 15 acceptance criteria in
``.harness/phase2-money-and-tee/sprint-4-contract.md``:

1.  Pro agent charged $29 USD-equivalent in XMR at the live rate
2.  Enterprise agent charged $999
3.  Insufficient balance -> 7-day grace period
4.  Grace expiry -> auto downgrade to FREE
5.  Top-up during grace -> next retry succeeds, grace cleared
6.  Mid-month upgrade prorates (15-day mark, 30-day month)
7.  Mid-month downgrade refunds the unused portion
8.  Upgrade endpoint actually charges XMR
9.  Upgrade endpoint returns 402 when insufficient
10. Downgrade endpoint refunds XMR
11. Cron is idempotent within the same day
12. Rate cache hit avoids second API call
13. Rate unavailable >24h -> RateUnavailableError
14. Billing writes use a single atomic transaction
15. Cron skips FREE agents
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.enums import AgentTier
from sthrip.db.models import (
    Agent,
    AgentBalance,
    AgentBillingHistory,
    AgentReputation,
    Base,
)
from sthrip.services.subscription_billing_service import (
    GRACE_PERIOD_DAYS,
    PREMIUM_USD_MONTHLY,
    VERIFIED_USD_MONTHLY,
    bill_pro_subscriptions,
    compute_refund,
    handle_grace_expiry,
    prorate_charge,
    start_grace_period,
)
from sthrip.services.xmr_rate_service import (
    RateUnavailableError,
    XmrRateCache,
    get_xmr_usd_rate,
    reset_rate_cache,
    usd_to_xmr_piconero,
)


PICO = Decimal(10) ** 12


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Agent.__table__,
            AgentReputation.__table__,
            AgentBalance.__table__,
            AgentBillingHistory.__table__,
        ],
    )
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _reset_rate_cache():
    """Clear the in-memory rate cache before each test to guarantee isolation."""
    reset_rate_cache()
    yield
    reset_rate_cache()


def _make_agent(
    db,
    name: str,
    tier: AgentTier,
    balance_xmr: Decimal = Decimal("10"),
    tier_grace_until: Optional[datetime] = None,
) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        tier=tier,
        is_active=True,
        xmr_address=f"4{name}xmr_addr_padding_to_long",
        tier_grace_until=tier_grace_until,
    )
    db.add(agent)
    db.flush()
    bal = AgentBalance(
        agent_id=agent.id,
        token="XMR",
        available=balance_xmr,
        pending=Decimal("0"),
    )
    db.add(bal)
    db.flush()
    return agent


# ─────────────────────────────────────────────────────────────────────────────
# 1. Pro agent charged $29 USD-equivalent in XMR
# ─────────────────────────────────────────────────────────────────────────────


def test_pro_agent_charged_29_usd_in_xmr_at_rate(db_session):
    agent = _make_agent(
        db_session, "pro1", AgentTier.VERIFIED, balance_xmr=Decimal("1")
    )
    db_session.commit()

    now = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)

    with patch(
        "sthrip.services.subscription_billing_service.get_xmr_usd_rate",
        return_value=Decimal("200"),
    ):
        summary = bill_pro_subscriptions(now, db_session)

    db_session.commit()

    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == agent.id, AgentBalance.token == "XMR")
        .first()
    )
    # 29 / 200 = 0.145 XMR ⇒ remaining 0.855 XMR
    assert bal.available == Decimal("0.855")

    history = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == agent.id)
        .all()
    )
    assert len(history) == 1
    row = history[0]
    assert row.status == "monthly_charge"
    assert row.amount_usd == Decimal("29.00")
    assert row.amount_piconero == 145_000_000_000
    assert row.tier_at_event == AgentTier.VERIFIED.value
    assert summary["charged"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 2. Enterprise agent charged $999
# ─────────────────────────────────────────────────────────────────────────────


def test_enterprise_agent_charged_999_usd_in_xmr(db_session):
    agent = _make_agent(
        db_session, "ent1", AgentTier.PREMIUM, balance_xmr=Decimal("10")
    )
    db_session.commit()

    now = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)

    with patch(
        "sthrip.services.subscription_billing_service.get_xmr_usd_rate",
        return_value=Decimal("200"),
    ):
        bill_pro_subscriptions(now, db_session)

    db_session.commit()

    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == agent.id, AgentBalance.token == "XMR")
        .first()
    )
    # 999 / 200 = 4.995 XMR ⇒ remaining 5.005
    assert bal.available == Decimal("5.005")

    history = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == agent.id)
        .all()
    )
    assert len(history) == 1
    assert history[0].amount_usd == Decimal("999.00")
    assert history[0].amount_piconero == 4_995_000_000_000
    assert history[0].tier_at_event == AgentTier.PREMIUM.value


# ─────────────────────────────────────────────────────────────────────────────
# 3. Insufficient balance starts grace period
# ─────────────────────────────────────────────────────────────────────────────


def test_insufficient_balance_starts_grace_period(db_session):
    agent = _make_agent(
        db_session, "broke", AgentTier.VERIFIED, balance_xmr=Decimal("0.01")
    )
    db_session.commit()
    now = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)

    with patch(
        "sthrip.services.subscription_billing_service.get_xmr_usd_rate",
        return_value=Decimal("200"),
    ):
        summary = bill_pro_subscriptions(now, db_session)
    db_session.commit()

    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == agent.id)
        .first()
    )
    # Balance untouched
    assert bal.available == Decimal("0.01")

    db_agent = db_session.query(Agent).filter(Agent.id == agent.id).first()
    assert db_agent.tier == AgentTier.VERIFIED  # tier preserved during grace
    assert db_agent.tier_grace_until is not None
    expected_grace_end = now + timedelta(days=GRACE_PERIOD_DAYS)
    actual = db_agent.tier_grace_until
    if actual.tzinfo is None:
        actual = actual.replace(tzinfo=timezone.utc)
    assert abs((actual - expected_grace_end).total_seconds()) < 60

    rows = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == agent.id)
        .all()
    )
    statuses = sorted(r.status for r in rows)
    assert "monthly_grace_started" in statuses
    assert summary["grace_started"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. Grace expiry downgrades to FREE
# ─────────────────────────────────────────────────────────────────────────────


def test_grace_expiry_downgrades_to_free(db_session):
    now = datetime(2026, 6, 10, 4, 30, tzinfo=timezone.utc)
    grace_past = now - timedelta(hours=1)
    agent = _make_agent(
        db_session,
        "expired",
        AgentTier.VERIFIED,
        balance_xmr=Decimal("0"),
        tier_grace_until=grace_past,
    )
    db_session.commit()

    summary = handle_grace_expiry(now, db_session)
    db_session.commit()

    db_agent = db_session.query(Agent).filter(Agent.id == agent.id).first()
    assert db_agent.tier == AgentTier.FREE
    assert db_agent.tier_grace_until is None

    rows = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == agent.id)
        .all()
    )
    assert any(r.status == "monthly_grace_expired_downgrade" for r in rows)
    assert summary["downgraded"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 5. Top-up during grace -> next retry succeeds, grace cleared
# ─────────────────────────────────────────────────────────────────────────────


def test_balance_topped_up_during_grace_resumes_pro(db_session):
    now = datetime(2026, 6, 3, 4, 0, tzinfo=timezone.utc)
    grace_future = now + timedelta(days=4)
    agent = _make_agent(
        db_session,
        "topup",
        AgentTier.VERIFIED,
        balance_xmr=Decimal("0.5"),
        tier_grace_until=grace_future,
    )
    db_session.commit()

    with patch(
        "sthrip.services.subscription_billing_service.get_xmr_usd_rate",
        return_value=Decimal("200"),
    ):
        bill_pro_subscriptions(now, db_session)
    db_session.commit()

    db_agent = db_session.query(Agent).filter(Agent.id == agent.id).first()
    assert db_agent.tier == AgentTier.VERIFIED
    assert db_agent.tier_grace_until is None

    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == agent.id)
        .first()
    )
    # 0.5 - 0.145 = 0.355
    assert bal.available == Decimal("0.355")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Proration on mid-month upgrade
# ─────────────────────────────────────────────────────────────────────────────


def test_proration_on_mid_month_upgrade():
    """day 15 of 30 ⇒ 16 remaining days inclusive of day 15."""
    result = prorate_charge(Decimal("29"), day_of_month=15, days_in_month=30)
    expected = (Decimal("29") * Decimal("16") / Decimal("30")).quantize(Decimal("0.01"))
    assert result == expected
    assert result == Decimal("15.47")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Refund on mid-month downgrade equals proration
# ─────────────────────────────────────────────────────────────────────────────


def test_refund_on_mid_month_downgrade():
    result = compute_refund(Decimal("29"), day_of_month=15, days_in_month=30)
    expected = prorate_charge(Decimal("29"), day_of_month=15, days_in_month=30)
    assert result == expected


# ─────────────────────────────────────────────────────────────────────────────
# 8 + 9 + 10 — endpoint integration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(db_session, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.deps import get_current_agent, get_db_session

    app = FastAPI()
    from api.routers.agents import router as agents_router

    app.include_router(agents_router)

    seeded = _make_agent(
        db_session, "tester_billing", AgentTier.FREE, balance_xmr=Decimal("1")
    )
    db_session.commit()

    def _override_db():
        yield db_session

    async def _override_agent():
        return db_session.query(Agent).filter(Agent.id == seeded.id).first()

    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_current_agent] = _override_agent

    import api.routers.agents as agents_module

    audit_calls = []

    def _fake_audit_log(action, **kwargs):
        audit_calls.append((action, kwargs))

    monkeypatch.setattr(agents_module, "audit_log", _fake_audit_log)

    # Pin rate at $200/XMR for predictable arithmetic. The endpoint passes
    # ``now=`` so accept arbitrary kwargs.
    monkeypatch.setattr(
        agents_module,
        "get_xmr_usd_rate",
        lambda *a, **kw: Decimal("200"),
    )

    # Pin "now" inside endpoint so the proration is deterministic.
    fixed_now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)

    def _fake_now() -> datetime:
        return fixed_now

    monkeypatch.setattr(agents_module, "_billing_now", _fake_now)

    with TestClient(app) as client:
        client.audit_calls = audit_calls
        client.seeded_agent_id = seeded.id
        yield client


def test_upgrade_endpoint_charges_xmr(app_client, db_session):
    resp = app_client.post(
        "/v2/me/upgrade",
        json={"tier": "pro"},
        headers={"Authorization": "Bearer test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == AgentTier.VERIFIED.value
    assert "amount_charged_usd" in body
    # day 15 of 30 ⇒ 16/30 of $29 = $15.47
    assert body["amount_charged_usd"] == "15.47"
    assert body["amount_charged_piconero"] > 0

    db_agent = (
        db_session.query(Agent)
        .filter(Agent.id == app_client.seeded_agent_id)
        .first()
    )
    assert db_agent.tier == AgentTier.VERIFIED
    assert db_agent.tier_grace_until is None

    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == app_client.seeded_agent_id)
        .first()
    )
    # 1 XMR - 15.47/200 = 1 - 0.07735 = 0.92265
    assert bal.available == Decimal("0.92265")

    rows = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == app_client.seeded_agent_id)
        .all()
    )
    assert any(r.status == "upgrade_charge" for r in rows)
    actions = [c[0] for c in app_client.audit_calls]
    assert "tier_upgrade" in actions


def test_upgrade_endpoint_402_when_insufficient(app_client, db_session):
    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == app_client.seeded_agent_id)
        .first()
    )
    bal.available = Decimal("0.0001")
    db_session.flush()
    db_session.commit()

    resp = app_client.post(
        "/v2/me/upgrade",
        json={"tier": "pro"},
        headers={"Authorization": "Bearer test"},
    )
    assert resp.status_code == 402, resp.text

    db_agent = (
        db_session.query(Agent)
        .filter(Agent.id == app_client.seeded_agent_id)
        .first()
    )
    assert db_agent.tier == AgentTier.FREE  # untouched


def test_downgrade_endpoint_refunds_xmr(app_client, db_session):
    db_agent = (
        db_session.query(Agent)
        .filter(Agent.id == app_client.seeded_agent_id)
        .first()
    )
    db_agent.tier = AgentTier.VERIFIED
    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == app_client.seeded_agent_id)
        .first()
    )
    bal.available = Decimal("1")
    db_session.flush()
    db_session.commit()

    resp = app_client.post(
        "/v2/me/downgrade",
        json={"tier": "free"},
        headers={"Authorization": "Bearer test"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == AgentTier.FREE.value
    assert "amount_refunded_usd" in body

    db_agent_after = (
        db_session.query(Agent)
        .filter(Agent.id == app_client.seeded_agent_id)
        .first()
    )
    assert db_agent_after.tier == AgentTier.FREE

    bal_after = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == app_client.seeded_agent_id)
        .first()
    )
    # Refund of 16/30 * $29 = $15.47 ⇒ 0.07735 XMR
    assert bal_after.available == Decimal("1.07735")

    rows = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == app_client.seeded_agent_id)
        .all()
    )
    assert any(r.status == "downgrade_refund" for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Cron idempotent within same day (month)
# ─────────────────────────────────────────────────────────────────────────────


def test_idempotent_cron_run_same_day(db_session):
    agent = _make_agent(
        db_session, "idem", AgentTier.VERIFIED, balance_xmr=Decimal("1")
    )
    db_session.commit()

    now = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)

    with patch(
        "sthrip.services.subscription_billing_service.get_xmr_usd_rate",
        return_value=Decimal("200"),
    ):
        bill_pro_subscriptions(now, db_session)
        db_session.commit()
        bill_pro_subscriptions(now, db_session)
        db_session.commit()

    history = (
        db_session.query(AgentBillingHistory)
        .filter(
            AgentBillingHistory.agent_id == agent.id,
            AgentBillingHistory.status == "monthly_charge",
        )
        .all()
    )
    assert len(history) == 1, [(r.status, r.month_start) for r in history]

    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == agent.id)
        .first()
    )
    # Charged ONCE (not twice)
    assert bal.available == Decimal("0.855")


# ─────────────────────────────────────────────────────────────────────────────
# 12. Rate cache hit avoids second API call
# ─────────────────────────────────────────────────────────────────────────────


@respx.mock
def test_rate_cache_hit_avoids_api_call():
    route = respx.get(
        "https://api.coingecko.com/api/v3/simple/price"
    ).mock(
        return_value=httpx.Response(
            200, json={"monero": {"usd": 165.0}}
        )
    )

    rate1 = get_xmr_usd_rate()
    rate2 = get_xmr_usd_rate()

    assert rate1 == Decimal("165.0")
    assert rate2 == Decimal("165.0")
    # Second call should use cache.
    assert route.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 13. Rate unavailable >24h raises
# ─────────────────────────────────────────────────────────────────────────────


@respx.mock
def test_rate_unavailable_24h_raises():
    # Pre-populate the cache with an entry older than 24h.
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    XmrRateCache.set_for_test(rate=Decimal("100"), fetched_at=stale)

    # Simulate API down.
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(RateUnavailableError):
        get_xmr_usd_rate()


# ─────────────────────────────────────────────────────────────────────────────
# 14. Atomic billing — failure rolls back balance + history
# ─────────────────────────────────────────────────────────────────────────────


def test_billing_uses_atomic_transaction(db_session):
    agent = _make_agent(
        db_session, "atomic", AgentTier.VERIFIED, balance_xmr=Decimal("1")
    )
    db_session.commit()

    now = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)

    # Inject a failure right after the charge row insert. Implementation calls
    # ``_record_billing_event`` AFTER deduct; we patch the audit hook to raise
    # so we exercise the whole-tx rollback contract.
    with patch(
        "sthrip.services.subscription_billing_service.get_xmr_usd_rate",
        return_value=Decimal("200"),
    ), patch(
        "sthrip.services.subscription_billing_service._post_charge_audit",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            bill_pro_subscriptions(now, db_session)

    db_session.rollback()

    bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == agent.id)
        .first()
    )
    assert bal.available == Decimal("1")  # rollback preserved balance

    rows = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == agent.id)
        .all()
    )
    assert rows == []  # nothing committed


# ─────────────────────────────────────────────────────────────────────────────
# 15. Cron skips FREE agents
# ─────────────────────────────────────────────────────────────────────────────


def test_billing_skips_FREE_agents(db_session):
    free = _make_agent(
        db_session, "free1", AgentTier.FREE, balance_xmr=Decimal("1")
    )
    pro = _make_agent(
        db_session, "pro2", AgentTier.VERIFIED, balance_xmr=Decimal("1")
    )
    db_session.commit()
    now = datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc)

    with patch(
        "sthrip.services.subscription_billing_service.get_xmr_usd_rate",
        return_value=Decimal("200"),
    ):
        summary = bill_pro_subscriptions(now, db_session)
    db_session.commit()

    free_rows = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == free.id)
        .all()
    )
    assert free_rows == []
    free_bal = (
        db_session.query(AgentBalance)
        .filter(AgentBalance.agent_id == free.id)
        .first()
    )
    assert free_bal.available == Decimal("1")  # untouched

    pro_rows = (
        db_session.query(AgentBillingHistory)
        .filter(AgentBillingHistory.agent_id == pro.id)
        .all()
    )
    assert len(pro_rows) == 1
    assert summary["charged"] == 1
