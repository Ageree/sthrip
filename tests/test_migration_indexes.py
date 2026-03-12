"""Verify the new indexes exist on models."""
from sthrip.db.models import Transaction, HubRoute, PendingWithdrawal


def test_transaction_has_status_index():
    indexes = {idx.name for idx in Transaction.__table__.indexes}
    assert "ix_transactions_status" in indexes


def test_transaction_has_from_agent_created_index():
    indexes = {idx.name for idx in Transaction.__table__.indexes}
    assert "ix_transactions_from_agent_created" in indexes


def test_transaction_has_to_agent_created_index():
    indexes = {idx.name for idx in Transaction.__table__.indexes}
    assert "ix_transactions_to_agent_created" in indexes


def test_hub_route_has_status_index():
    indexes = {idx.name for idx in HubRoute.__table__.indexes}
    assert "ix_hub_routes_status" in indexes


def test_pending_withdrawal_has_status_created_index():
    indexes = {idx.name for idx in PendingWithdrawal.__table__.indexes}
    assert "ix_pending_withdrawals_status_created" in indexes
