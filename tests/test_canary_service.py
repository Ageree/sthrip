"""Phase 2 Sprint 1 — warrant canary service + endpoint tests.

Covers contract acceptance criteria #5–#8:

5. test_canary_signature_verifies
6. test_canary_signature_rejects_tampering
7. test_canary_endpoint_503_when_stale
8. test_canary_endpoint_200_when_fresh
"""
from __future__ import annotations

import base64
import contextlib
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from nacl.signing import SigningKey
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base,
    CanaryState,
)
from sthrip.services import canary_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_signing_key_b64() -> str:
    """Fresh Ed25519 seed (32 bytes), base64-encoded."""
    return base64.b64encode(SigningKey.generate().encode()).decode()


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[CanaryState.__table__])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Sign / verify unit tests
# ---------------------------------------------------------------------------

def test_canary_signature_verifies():
    """Contract #5: a freshly signed canary verifies against its public key."""
    sk_b64 = _generate_signing_key_b64()
    pk_b64 = canary_service.derive_public_key_b64(sk_b64)

    payload = canary_service.generate_canary_payload(_naive_utc_now())
    signed = canary_service.sign_canary(payload, sk_b64)

    assert canary_service.verify_canary(signed, pk_b64) is True


def test_canary_signature_rejects_tampering():
    """Contract #6: flipping a payload byte invalidates the signature."""
    sk_b64 = _generate_signing_key_b64()
    pk_b64 = canary_service.derive_public_key_b64(sk_b64)

    payload = canary_service.generate_canary_payload(_naive_utc_now())
    signed = canary_service.sign_canary(payload, sk_b64)

    tampered = dict(signed)
    tampered["status"] = "compromised"  # flipped status
    assert canary_service.verify_canary(tampered, pk_b64) is False

    # Date tamper rejected too.
    tampered2 = dict(signed)
    tampered2["date"] = "1999-01-01"
    assert canary_service.verify_canary(tampered2, pk_b64) is False


def test_publish_daily_canary_persists_single_row(db_session):
    """publish_daily_canary upserts row id=1 and overwrites on rerun."""
    sk_b64 = _generate_signing_key_b64()

    canary_service.publish_daily_canary(
        db=db_session,
        signing_key_b64=sk_b64,
        now=_naive_utc_now(),
    )
    db_session.commit()

    rows = db_session.query(CanaryState).all()
    assert len(rows) == 1
    assert rows[0].id == 1

    # Second run overwrites in place — still one row.
    canary_service.publish_daily_canary(
        db=db_session,
        signing_key_b64=sk_b64,
        now=_naive_utc_now() + timedelta(hours=1),
    )
    db_session.commit()
    rows = db_session.query(CanaryState).all()
    assert len(rows) == 1


def test_get_current_canary_returns_payload_when_fresh(db_session):
    sk_b64 = _generate_signing_key_b64()
    canary_service.publish_daily_canary(
        db=db_session,
        signing_key_b64=sk_b64,
        now=_naive_utc_now(),
    )
    db_session.commit()

    current = canary_service.get_current_canary(db=db_session)
    assert current is not None
    assert current["status"] == "no_subpoena_no_compromise"
    assert "signature_b64" in current


def test_get_current_canary_returns_none_when_stale(db_session):
    sk_b64 = _generate_signing_key_b64()
    canary_service.publish_daily_canary(
        db=db_session,
        signing_key_b64=sk_b64,
        now=_naive_utc_now() - timedelta(hours=49),
    )
    db_session.commit()

    current = canary_service.get_current_canary(db=db_session)
    assert current is None


def test_canary_signing_requires_key():
    """publish_daily_canary refuses to run without a configured key."""
    with pytest.raises(ValueError):
        canary_service.sign_canary(
            canary_service.generate_canary_payload(_naive_utc_now()),
            "",
        )


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _client_with_db(session_factory):
    """Spin up TestClient with get_db patched to the in-memory session."""
    from contextlib import contextmanager as _cm

    @_cm
    def get_test_db():
        s = session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy", "timestamp": "2026-05-07T00:00:00", "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    # The two modules where canary_endpoint reaches into get_db live in
    # api.routers.wellknown and sthrip.db.database (for the Depends fallback).
    db_modules = [
        "sthrip.db.database",
        "api.routers.wellknown",
        "api.main_v2",
        "api.deps",
    ]
    rl_modules = ["sthrip.services.rate_limiter", "api.main_v2", "api.deps"]
    audit_modules = ["api.main_v2", "api.deps"]

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))
        for mod in db_modules:
            try:
                stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
            except (AttributeError, ModuleNotFoundError):
                pass
        stack.enter_context(patch("sthrip.db.database.create_tables"))
        for mod in rl_modules:
            try:
                stack.enter_context(
                    patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
                )
            except (AttributeError, ModuleNotFoundError):
                pass
        for mod in audit_modules:
            try:
                stack.enter_context(patch(f"{mod}.audit_log"))
            except (AttributeError, ModuleNotFoundError):
                pass
        stack.enter_context(
            patch(
                "sthrip.services.monitoring.get_monitor",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.monitoring.setup_default_monitoring",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.webhook_service.get_webhook_service",
                return_value=mock_webhook,
            )
        )
        stack.enter_context(
            patch("sthrip.services.webhook_service.queue_webhook")
        )
        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


def test_canary_endpoint_200_when_fresh():
    """Contract #8: fresh canary returns 200 with full signed payload."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[CanaryState.__table__])
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    # Seed a fresh canary (signed 1h ago).
    seed = Session()
    sk_b64 = _generate_signing_key_b64()
    canary_service.publish_daily_canary(
        db=seed,
        signing_key_b64=sk_b64,
        now=_naive_utc_now() - timedelta(hours=1),
    )
    seed.commit()
    seed.close()

    with _client_with_db(Session) as client:
        resp = client.get("/.well-known/canary.txt")
    assert resp.status_code == 200
    body = resp.json()
    assert "date" in body
    assert "status" in body
    assert "signature_b64" in body
    assert body["status"] == "no_subpoena_no_compromise"


def test_canary_endpoint_503_when_stale():
    """Contract #7: stale (49h+) canary returns 503 with structured error."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[CanaryState.__table__])
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    seed = Session()
    sk_b64 = _generate_signing_key_b64()
    canary_service.publish_daily_canary(
        db=seed,
        signing_key_b64=sk_b64,
        now=_naive_utc_now() - timedelta(hours=49),
    )
    seed.commit()
    seed.close()

    with _client_with_db(Session) as client:
        resp = client.get("/.well-known/canary.txt")
    assert resp.status_code == 503
    body = resp.json()
    assert body.get("error") == "canary_stale"


def test_canary_endpoint_503_when_missing():
    """No canary at all → 503 with canary_stale error (last_signed_at=None)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[CanaryState.__table__])
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with _client_with_db(Session) as client:
        resp = client.get("/.well-known/canary.txt")
    assert resp.status_code == 503
    body = resp.json()
    assert body.get("error") == "canary_stale"
    assert body.get("last_signed_at") is None
