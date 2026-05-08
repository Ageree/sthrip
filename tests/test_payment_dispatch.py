"""Phase 3 Sprint 6 — payment_dispatch contract tests (12).

The dispatcher routes hub-routing requests either to the local handler
(``api.routers.payments._execute_hub_transfer``) or to the GCP TEE proxy
based on ``STHRIP_PAYMENT_VIA_TEE``.  These tests verify the 12 contract
acceptance criteria from ``.harness/phase2-money-and-tee/sprint-6-contract.md``.

The TEE HTTP layer is mocked at the ``payment_tee_client`` boundary; no
real GCP IP is contacted.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Optional
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.enums import AgentTier
from sthrip.db.models import (
    Agent,
    AgentBalance,
    AgentMonthlyStats,
    AgentReputation,
    Base,
    FeeCollection,
    HubRoute,
    HubRouteStatus,
    Transaction,
)
from sthrip.services import payment_dispatch, payment_tee_client


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


_PICO = Decimal(10) ** 12  # 1 XMR.


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
            Transaction.__table__,
            FeeCollection.__table__,
            HubRoute.__table__,
            AgentMonthlyStats.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_agent(
    db, name: str, tier: AgentTier = AgentTier.FREE, balance_xmr: Decimal = Decimal("100")
) -> Agent:
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        tier=tier,
        is_active=True,
        xmr_address=f"4{name}_xmr_addr_padding_long_enough",
    )
    db.add(agent)
    db.flush()
    bal = AgentBalance(agent_id=agent.id, token="XMR", available=balance_xmr)
    db.add(bal)
    db.commit()
    return agent


@dataclass
class _RecipientProfile:
    id: str
    agent_name: str
    xmr_address: str
    trust_score: int = 0


@dataclass
class _Req:
    to_agent_name: str
    urgency: str = "normal"
    memo: Optional[str] = None
    amount: Decimal = Decimal("1")


@pytest.fixture
def world(db_session):
    """A sender + recipient + the call args the dispatcher needs."""
    sender = _make_agent(db_session, "sender_a", balance_xmr=Decimal("100"))
    recv = _make_agent(db_session, "receiver_b", balance_xmr=Decimal("0"))

    recipient = _RecipientProfile(
        id=str(recv.id),
        agent_name=recv.agent_name,
        xmr_address=recv.xmr_address,
    )

    amount = Decimal("1.5")
    fee_info = {
        "fee_amount": Decimal("0.0045"),
        "fee_percent": Decimal("0.003"),
        "total_deduction": amount + Decimal("0.0045"),
        "tier_discount": "free",
        "seller_receives": amount - Decimal("0.0045"),
    }
    req = _Req(to_agent_name=recv.agent_name, urgency="normal")

    return {
        "db": db_session,
        "agent": sender,
        "recipient": recipient,
        "amount": amount,
        "fee_info": fee_info,
        "req": req,
        "idempotency_key": "idem_test_key_abc123",
    }


@pytest.fixture(autouse=True)
def _reset_env():
    """Clear flag/env vars between tests so leakage cannot mask bugs."""
    keys = [
        "STHRIP_PAYMENT_VIA_TEE",
        "STHRIP_TEE_ENDPOINT",
        "STHRIP_TEE_CLIENT_CERT_PATH",
        "STHRIP_TEE_CLIENT_KEY_PATH",
        "STHRIP_TEE_CA_PATH",
        "STHRIP_TEE_PROXY_TOKEN",
    ]
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# Test #1: flag off → local handler
# ─────────────────────────────────────────────────────────────────────────────


def test_flag_off_uses_local_handler(world):
    captured = {}

    def fake_local(db, agent, recipient, amount, fee_info, req, idem, fee_collector=None):
        captured["called"] = True
        return {"payment_id": "hp_local_xyz", "status": "confirmed", "amount": amount}

    with patch.object(payment_dispatch, "_local_dispatch", side_effect=fake_local) as local_mock, \
         patch.object(payment_tee_client, "dispatch_hub_routing") as tee_mock:
        result = payment_dispatch.dispatch_hub_routing(
            world["db"], world["agent"], world["recipient"], world["amount"],
            world["fee_info"], world["req"], world["idempotency_key"],
        )

    assert captured["called"] is True
    assert local_mock.called
    assert not tee_mock.called
    assert result["payment_id"] == "hp_local_xyz"


# ─────────────────────────────────────────────────────────────────────────────
# Test #2: flag on → proxies to TEE client
# ─────────────────────────────────────────────────────────────────────────────


def test_flag_on_proxies_to_tee_client_mock(world, monkeypatch):
    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    tee_response = {
        "payment_id": "hp_tee_001",
        "status": "confirmed",
        "amount": str(world["amount"]),
        "fee": "0.0045",
        "fee_percent": "0.003",
    }

    with patch.object(payment_tee_client, "dispatch_hub_routing", return_value=tee_response) as tee_mock:
        result = payment_dispatch.dispatch_hub_routing(
            world["db"], world["agent"], world["recipient"], world["amount"],
            world["fee_info"], world["req"], world["idempotency_key"],
        )

    assert tee_mock.call_count == 1
    envelope, idem = tee_mock.call_args[0][0], tee_mock.call_args[0][1]
    assert envelope["from_agent_id"] == str(world["agent"].id)
    assert envelope["to_agent_id"] == str(world["recipient"].id)
    assert envelope["amount"] == world["amount"]
    assert idem == world["idempotency_key"]
    assert result["payment_id"] == "hp_tee_001"
    assert result["status"] == "confirmed"


# ─────────────────────────────────────────────────────────────────────────────
# Test #3: TEE unreachable → fall back to local
# ─────────────────────────────────────────────────────────────────────────────


def test_tee_unreachable_falls_back_to_local(world, monkeypatch):
    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    fallback_called = {"v": False}

    def fake_local(db, agent, recipient, amount, fee_info, req, idem, fee_collector=None):
        fallback_called["v"] = True
        return {"payment_id": "hp_fallback", "status": "confirmed", "amount": amount}

    metric_before = _read_counter(
        payment_dispatch.tee_unreachable_total, reason="network"
    )
    with patch.object(
        payment_tee_client, "dispatch_hub_routing",
        side_effect=payment_tee_client.TEEUnreachableError("dns fail"),
    ), patch.object(payment_dispatch, "_local_dispatch", side_effect=fake_local):
        result = payment_dispatch.dispatch_hub_routing(
            world["db"], world["agent"], world["recipient"], world["amount"],
            world["fee_info"], world["req"], world["idempotency_key"],
        )

    metric_after = _read_counter(
        payment_dispatch.tee_unreachable_total, reason="network"
    )
    assert fallback_called["v"] is True
    assert result["payment_id"] == "hp_fallback"
    assert metric_after >= metric_before + 1


# ─────────────────────────────────────────────────────────────────────────────
# Test #4: TEE 5xx → fall back to local
# ─────────────────────────────────────────────────────────────────────────────


def test_tee_server_error_falls_back(world, monkeypatch):
    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    fallback_called = {"v": False}

    def fake_local(db, agent, recipient, amount, fee_info, req, idem, fee_collector=None):
        fallback_called["v"] = True
        return {"payment_id": "hp_fallback_5xx", "status": "confirmed", "amount": amount}

    with patch.object(
        payment_tee_client, "dispatch_hub_routing",
        side_effect=payment_tee_client.TEEServerError(503, "service unavailable"),
    ), patch.object(payment_dispatch, "_local_dispatch", side_effect=fake_local):
        result = payment_dispatch.dispatch_hub_routing(
            world["db"], world["agent"], world["recipient"], world["amount"],
            world["fee_info"], world["req"], world["idempotency_key"],
        )

    assert fallback_called["v"] is True
    assert result["payment_id"] == "hp_fallback_5xx"


# ─────────────────────────────────────────────────────────────────────────────
# Test #5: TEE 4xx (legitimate user-error) → NO fall-back, propagate
# ─────────────────────────────────────────────────────────────────────────────


def test_tee_4xx_response_NOT_fallback(world, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    user_error_response = {
        "_status_code": 400,
        "detail": "Insufficient balance",
    }
    fallback_called = {"v": False}

    def fake_local(*a, **kw):
        fallback_called["v"] = True
        return {"payment_id": "should_not_run"}

    with patch.object(
        payment_tee_client, "dispatch_hub_routing", return_value=user_error_response,
    ), patch.object(payment_dispatch, "_local_dispatch", side_effect=fake_local):
        with pytest.raises(HTTPException) as excinfo:
            payment_dispatch.dispatch_hub_routing(
                world["db"], world["agent"], world["recipient"], world["amount"],
                world["fee_info"], world["req"], world["idempotency_key"],
            )

    assert excinfo.value.status_code == 400
    assert "insufficient" in str(excinfo.value.detail).lower()
    assert fallback_called["v"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Test #6: idempotency_key propagates
# ─────────────────────────────────────────────────────────────────────────────


def test_idempotency_key_propagates(world, monkeypatch):
    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    captured_idem = {}

    def spy(envelope, idempotency_key, **kw):
        captured_idem["env"] = envelope.get("idempotency_key", "<missing>")
        captured_idem["kw"] = idempotency_key
        return {
            "payment_id": "hp_x",
            "status": "confirmed",
            "amount": "1.5",
            "fee": "0.0045",
            "fee_percent": "0.003",
        }

    # Make dispatch_hub_routing forward to a fake that exercises the real
    # client serialiser too.
    def real_with_spy(envelope, idempotency_key, timeout_s=30.0):
        # Mimic what the real client does with idempotency_key: place it
        # in the body. We assert both sides see the same value.
        body = dict(envelope)
        body["idempotency_key"] = idempotency_key
        captured_idem["body"] = body
        return spy(body, idempotency_key)

    with patch.object(payment_tee_client, "dispatch_hub_routing", side_effect=real_with_spy):
        payment_dispatch.dispatch_hub_routing(
            world["db"], world["agent"], world["recipient"], world["amount"],
            world["fee_info"], world["req"], world["idempotency_key"],
        )

    assert captured_idem["kw"] == world["idempotency_key"]
    assert captured_idem["body"]["idempotency_key"] == world["idempotency_key"]


# ─────────────────────────────────────────────────────────────────────────────
# Test #7: response shape identical local vs proxy
# ─────────────────────────────────────────────────────────────────────────────


def test_response_shape_identical_local_vs_proxy(world, monkeypatch):
    """Both modes return a dict with the same keys + types the router
    consumes."""

    expected_keys = {
        "payment_id", "from_agent_id", "to_agent_id",
        "amount", "token", "fee", "status",
    }

    # ─── local path ────────────────────────────────────────────────────
    local_route = _local_route_dict_factory(
        payment_id="hp_local",
        from_agent_id=str(world["agent"].id),
        to_agent_id=world["recipient"].id,
        amount=world["amount"],
    )

    def fake_local(*a, **kw):
        return local_route

    with patch.object(payment_dispatch, "_local_dispatch", side_effect=fake_local):
        local_result = payment_dispatch.dispatch_hub_routing(
            world["db"], world["agent"], world["recipient"], world["amount"],
            dict(world["fee_info"]), world["req"], world["idempotency_key"],
        )

    # ─── tee path ──────────────────────────────────────────────────────
    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    tee_response = {
        "payment_id": "hp_tee",
        "status": "confirmed",
        "amount": str(world["amount"]),
        "fee": "0.0045",
        "fee_percent": "0.003",
    }

    with patch.object(payment_tee_client, "dispatch_hub_routing", return_value=tee_response):
        tee_result = payment_dispatch.dispatch_hub_routing(
            world["db"], world["agent"], world["recipient"], world["amount"],
            dict(world["fee_info"]), world["req"], world["idempotency_key"],
        )

    assert expected_keys.issubset(set(local_result.keys()))
    assert expected_keys.issubset(set(tee_result.keys()))
    assert type(local_result["fee"]) is type(tee_result["fee"])
    assert isinstance(local_result["amount"], type(tee_result["amount"]))


def _local_route_dict_factory(*, payment_id, from_agent_id, to_agent_id, amount):
    return {
        "payment_id": payment_id,
        "from_agent_id": from_agent_id,
        "to_agent_id": to_agent_id,
        "amount": amount,
        "token": "XMR",
        "fee": {
            "fee_amount": Decimal("0.0045"),
            "fee_percent": Decimal("0.003"),
            "tier_discount": "free",
            "seller_receives": amount - Decimal("0.0045"),
        },
        "status": "confirmed",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test #8 (carry-over M-1): HubRoute row written after TEE success
# ─────────────────────────────────────────────────────────────────────────────


def test_hub_route_row_written_after_tee_success(world, monkeypatch):
    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    db = world["db"]
    pre = db.query(HubRoute).count()
    assert pre == 0, "DB should start clean"

    tee_response = {
        "payment_id": "hp_tee_writes_route",
        "status": "confirmed",
        "amount": str(world["amount"]),
        "fee": "0.0045",
        "fee_percent": "0.003",
    }

    with patch.object(payment_tee_client, "dispatch_hub_routing", return_value=tee_response):
        payment_dispatch.dispatch_hub_routing(
            db, world["agent"], world["recipient"], world["amount"],
            world["fee_info"], world["req"], world["idempotency_key"],
        )
    db.commit()

    rows = db.query(HubRoute).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.payment_id == "hp_tee_writes_route"
    assert row.status == HubRouteStatus.CONFIRMED
    assert row.fee_collected is True
    assert row.fee_amount == Decimal("0.0045")
    assert row.from_agent_id == world["agent"].id
    # to_agent_id is UUID column
    assert str(row.to_agent_id) == str(world["recipient"].id)


# ─────────────────────────────────────────────────────────────────────────────
# Test #9: health-check loop calls the endpoint
# ─────────────────────────────────────────────────────────────────────────────


def test_health_check_loop_calls_endpoint(monkeypatch):
    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    calls = {"n": 0}

    def fake_health() -> bool:
        calls["n"] += 1
        return True

    with patch.object(payment_tee_client, "health_check", side_effect=fake_health):
        _drive_loop(
            payment_dispatch.health_check_loop(
                interval_s=0.0, threshold=3, iterations=2,
            )
        )

    assert calls["n"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# Test #10: 3 consecutive failures increments alert metric
# ─────────────────────────────────────────────────────────────────────────────


def test_health_check_3_failures_increments_alert_metric(monkeypatch):
    monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", "true")
    monkeypatch.setenv("STHRIP_TEE_ENDPOINT", "https://tee.example.invalid:8080")

    before = _read_counter(payment_dispatch.tee_unreachable_total, reason="health")

    with patch.object(payment_tee_client, "health_check", return_value=False):
        _drive_loop(
            payment_dispatch.health_check_loop(
                interval_s=0.0, threshold=3, iterations=3,
            )
        )

    after = _read_counter(payment_dispatch.tee_unreachable_total, reason="health")
    assert after - before == 3


# ─────────────────────────────────────────────────────────────────────────────
# Test #11: dispatch atomic on local path
# ─────────────────────────────────────────────────────────────────────────────


def test_dispatch_atomic_on_local_path(world):
    """Flag off, simulate local failure, verify the dispatcher propagates
    the exception so the surrounding ``with get_db()`` block rolls back —
    no half-state.
    """

    class BoomError(RuntimeError):
        pass

    def fake_local(*a, **kw):
        raise BoomError("simulated mid-handler failure")

    with patch.object(payment_dispatch, "_local_dispatch", side_effect=fake_local):
        with pytest.raises(BoomError):
            payment_dispatch.dispatch_hub_routing(
                world["db"], world["agent"], world["recipient"], world["amount"],
                world["fee_info"], world["req"], world["idempotency_key"],
            )

    # No HubRoute / Transaction rows were written by the dispatcher itself.
    assert world["db"].query(HubRoute).count() == 0
    assert world["db"].query(Transaction).count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test #12: STHRIP_PAYMENT_VIA_TEE default is false
# ─────────────────────────────────────────────────────────────────────────────


def test_payment_via_tee_env_default_is_false(monkeypatch):
    monkeypatch.delenv("STHRIP_PAYMENT_VIA_TEE", raising=False)
    assert payment_dispatch.is_tee_enabled() is False

    for falsy in ("", "0", "false", "FALSE", "no", "off", "weird"):
        monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", falsy)
        assert payment_dispatch.is_tee_enabled() is False, falsy

    for truthy in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("STHRIP_PAYMENT_VIA_TEE", truthy)
        assert payment_dispatch.is_tee_enabled() is True, truthy


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _drive_loop(coro) -> None:
    """Run an async coroutine without polluting the default event loop.

    ``asyncio.run`` closes the loop and clears it from the current thread,
    which breaks subsequent tests that legacy-style call
    ``asyncio.get_event_loop()`` (e.g. tests/test_webhook_tor_routing.py).

    Instead we create a private loop, drive the coro to completion, close
    that loop, and restore whatever the thread's default loop pointer was.
    """
    saved_loop = None
    try:
        saved_loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        saved_loop = None

    new_loop = asyncio.new_event_loop()
    try:
        new_loop.run_until_complete(coro)
    finally:
        new_loop.close()
        asyncio.set_event_loop(saved_loop)


def _read_counter(counter, **labels) -> float:
    """Read a Prometheus counter's current value (label-aware) or 0."""
    try:
        return counter.labels(**labels)._value.get()  # type: ignore[attr-defined]
    except Exception:
        # Either prometheus-client isn't installed (no-op stub) or label
        # combination doesn't exist yet — treat as zero.
        return 0.0
