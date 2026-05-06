"""Sprint 4a: tests for admin UI redaction when operator KEK is unavailable.

When the operator keystore is unreachable (RemoteKeystore mode, prod-only
until Sprint 4b deploys sthrip-op-keystore), the admin UI must NOT leak
participant IDs or precise amounts. Instead it shows ``"encrypted"`` and
the coarse amount bucket.

ADMIN_API_KEY alone must not decrypt — that's the threat-model invariant
(see lead-decisions Q1).
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from sthrip.db import models
from sthrip.db.models import EscrowStatus


# ---------------------------------------------------------------------------
# Helpers — minimal escrow-like row, no DB needed
# ---------------------------------------------------------------------------


class _FakeEscrow:
    """Stand-in for EscrowDeal, exposing only attrs the serializer reads."""

    def __init__(self, **fields):
        defaults = {
            "id": uuid.uuid4(),
            "deal_hash": "h" * 32,
            "buyer_id": uuid.uuid4(),
            "seller_id": uuid.uuid4(),
            "amount": Decimal("250.5"),
            "token": "XMR",
            "description": "research delivery",
            "fee_percent": Decimal("0.001"),
            "fee_amount": Decimal("0"),
            "release_amount": None,
            "status": EscrowStatus.CREATED,
            "accept_timeout_hours": 24,
            "delivery_timeout_hours": 48,
            "review_timeout_hours": 24,
            "accept_deadline": None,
            "delivery_deadline": None,
            "review_deadline": None,
            "deal_metadata": {},
            "created_at": None,
            "accepted_at": None,
            "delivered_at": None,
            "completed_at": None,
            "cancelled_at": None,
            "expires_at": None,
            "amount_bucket": "100-1k XMR",
        }
        defaults.update(fields)
        for k, v in defaults.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# _keystore_available
# ---------------------------------------------------------------------------


def test_keystore_available_in_stub_mode(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_MODE", "stub")
    # Reset cache so the env change takes effect.
    import sthrip.services.operator_keystore as ks
    ks.get_keystore.cache_clear()

    from api.admin_ui import views
    assert views._keystore_available() is True


def test_keystore_unavailable_in_remote_mode(monkeypatch):
    """Remote keystore raises NotImplementedError until Sprint 4b deploys."""
    monkeypatch.setenv("OP_KEYSTORE_MODE", "remote")
    import sthrip.services.operator_keystore as ks
    ks.get_keystore.cache_clear()

    from api.admin_ui import views
    assert views._keystore_available() is False


# ---------------------------------------------------------------------------
# _serialize_escrow with redacted=True
# ---------------------------------------------------------------------------


def test_admin_view_redacted_when_keystore_unavailable():
    """redacted=True forces the redacted shape regardless of the env."""
    from api.admin_ui.views import _serialize_escrow

    deal = _FakeEscrow()
    out = _serialize_escrow(deal, redacted=True)

    assert out["buyer_id"] == "encrypted"
    assert out["seller_id"] == "encrypted"
    assert out["amount"] == "100-1k XMR"  # bucket replaces precise amount
    assert out["description"] == "encrypted"
    assert out["redacted"] is True
    # Non-sensitive fields are still rendered for the admin.
    assert out["status"] == EscrowStatus.CREATED
    assert out["token"] == "XMR"


def test_amount_bucket_shown_when_redacted():
    """Bucket label is preferred over the generic 'redacted' fallback."""
    from api.admin_ui.views import _serialize_escrow

    deal = _FakeEscrow(amount_bucket="1-10 XMR")
    out = _serialize_escrow(deal, redacted=True)
    assert out["amount"] == "1-10 XMR"


def test_amount_bucket_fallback_when_missing():
    """When amount_bucket is null (legacy row pre-Sprint-3), use 'redacted'."""
    from api.admin_ui.views import _serialize_escrow

    deal = _FakeEscrow(amount_bucket=None)
    out = _serialize_escrow(deal, redacted=True)
    assert out["amount"] == "redacted"


# ---------------------------------------------------------------------------
# _serialize_escrow with redacted=False
# ---------------------------------------------------------------------------


def test_admin_view_full_when_keystore_available():
    """Stub keystore = full visibility for the admin operator."""
    from api.admin_ui.views import _serialize_escrow

    deal = _FakeEscrow()
    out = _serialize_escrow(deal, redacted=False)

    assert out["buyer_id"] == deal.buyer_id
    assert out["seller_id"] == deal.seller_id
    assert out["amount"] == Decimal("250.5")
    assert out["description"] == "research delivery"
    assert out["redacted"] is False


# ---------------------------------------------------------------------------
# Auto-probe path (redacted=None)
# ---------------------------------------------------------------------------


def test_admin_view_auto_probes_keystore(monkeypatch):
    """Without an explicit redacted= kwarg, the serializer probes."""
    monkeypatch.setenv("OP_KEYSTORE_MODE", "remote")
    import sthrip.services.operator_keystore as ks
    ks.get_keystore.cache_clear()

    from api.admin_ui.views import _serialize_escrow
    deal = _FakeEscrow()
    out = _serialize_escrow(deal)
    assert out["redacted"] is True
    assert out["buyer_id"] == "encrypted"


def test_admin_view_auto_probes_stub_full(monkeypatch):
    monkeypatch.setenv("OP_KEYSTORE_MODE", "stub")
    import sthrip.services.operator_keystore as ks
    ks.get_keystore.cache_clear()

    from api.admin_ui.views import _serialize_escrow
    deal = _FakeEscrow()
    out = _serialize_escrow(deal)
    assert out["redacted"] is False
    assert out["buyer_id"] == deal.buyer_id


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_redact_participant_helper():
    from api.admin_ui.views import _redact_participant
    assert _redact_participant(uuid.uuid4()) == "encrypted"
    assert _redact_participant(None) is None


def test_redact_amount_uses_bucket():
    from api.admin_ui.views import _redact_amount
    deal = _FakeEscrow(amount_bucket="10-100 XMR")
    assert _redact_amount(deal) == "10-100 XMR"


def test_redact_amount_falls_back_when_no_bucket():
    from api.admin_ui.views import _redact_amount
    deal = _FakeEscrow(amount_bucket=None)
    assert _redact_amount(deal) == "redacted"
