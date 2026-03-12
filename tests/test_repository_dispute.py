"""Test that record_dispute uses atomic SQL UPDATE."""

import uuid

from unittest.mock import MagicMock, patch

from sthrip.db.repository import ReputationRepository


def test_record_dispute_uses_atomic_update():
    """record_dispute must use SQL UPDATE, not read-modify-write."""
    mock_db = MagicMock()
    repo = ReputationRepository(mock_db)
    agent_id = uuid.uuid4()

    repo.record_dispute(agent_id)

    # Must call db.execute (atomic UPDATE), NOT db.query (read-modify-write)
    assert mock_db.execute.called, "record_dispute should use db.execute for atomic update"
    assert not mock_db.query.called, "record_dispute should NOT use db.query (non-atomic)"


def test_record_dispute_does_not_call_get_by_agent():
    """record_dispute must not read the record before updating."""
    mock_db = MagicMock()
    repo = ReputationRepository(mock_db)
    agent_id = uuid.uuid4()

    with patch.object(repo, "get_by_agent") as mock_get:
        repo.record_dispute(agent_id)
        assert not mock_get.called, "record_dispute should not call get_by_agent"
