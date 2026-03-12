"""
TDD tests for TOCTOU race condition fix in WebhookService.process_event().

The bug: process_event() reads the event row in Phase 1 without with_for_update(),
so two concurrent workers can both read the same row as 'pending' and both process it.

Fix under test:
  1. Phase 1 read must use with_for_update() (plain exclusive row lock, no skip_locked)
     so that a second worker blocks rather than double-processing.
  2. Phase 3 write must check that the event status is still pending/retrying before
     writing; if another worker already delivered it the status will be 'delivered',
     and this worker should skip the write rather than crash or overwrite.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call

from sthrip.services.webhook_service import WebhookResult, WebhookService
from sthrip.db.models import WebhookStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_get_db_mock(db):
    """Return a get_db patch target that yields *db* as context manager value."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _make_get_db_sequence(*dbs):
    """Return a side_effect list of context managers, one per call."""
    results = []
    for db in dbs:
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=db)
        ctx.__exit__ = MagicMock(return_value=False)
        results.append(ctx)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# RED 1: Phase 1 read must use with_for_update()
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessEventUsesForUpdate:
    """process_event() must lock the event row in Phase 1 via with_for_update()."""

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_phase1_get_by_id_uses_with_for_update(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """get_by_id() in Phase 1 must call with_for_update() on the query."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        # Track whether with_for_update was called on the query chain
        for_update_called = {"called": False}

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_lock"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        mock_webhook_repo = MagicMock()
        # get_by_id_for_update is the new locking read we are driving
        mock_webhook_repo.get_by_id_for_update.return_value = mock_event
        mock_webhook_repo_cls.return_value = mock_webhook_repo

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = "whsec_test"
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event("evt_lock")

        assert result.success is True
        # The repo must expose a locking read method that was called
        mock_webhook_repo.get_by_id_for_update.assert_called_once_with("evt_lock")

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_phase1_does_not_fall_back_to_unlocked_read(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """process_event() Phase 1 must NOT call the non-locking get_by_id().
        The Phase 1 repo (first WebhookRepository instance) must only call
        get_by_id_for_update(), not the plain get_by_id().
        Phase 3 is permitted to call get_by_id() on its own separate repo instance.
        """
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_lock2"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        # Separate repo mocks for Phase 1 and Phase 3
        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_current_event

        mock_webhook_repo_cls.side_effect = [
            mock_webhook_repo_phase1,
            mock_webhook_repo_phase3,
        ]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = None
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            await svc.process_event("evt_lock2")

        # The Phase 1 repo instance must NOT have called the plain get_by_id
        mock_webhook_repo_phase1.get_by_id.assert_not_called()
        # The Phase 1 repo must have called the locking variant
        mock_webhook_repo_phase1.get_by_id_for_update.assert_called_once_with("evt_lock2")


# ─────────────────────────────────────────────────────────────────────────────
# RED 2: Phase 3 must skip write when event is no longer pending/retrying
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessEventPhase3StatusCheck:
    """Phase 3 must re-check event status before writing; skip if already delivered."""

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_skips_mark_delivered_when_already_delivered(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """If another worker delivered the event between Phase 1 and Phase 3,
        Phase 3 must not call mark_delivered again (no double write)."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_race"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        # Phase 1 repo returns the event (we got the lock briefly)
        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        # Phase 3 repo: when we re-fetch to check status, it's already delivered
        mock_stale_event = MagicMock()
        mock_stale_event.status = WebhookStatus.DELIVERED
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_stale_event

        mock_webhook_repo_cls.side_effect = [
            mock_webhook_repo_phase1,
            mock_webhook_repo_phase3,
        ]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = "whsec_test"
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event("evt_race")

        # The method must return success (it did the HTTP work) without crashing
        assert result.success is True
        # But Phase 3 must NOT call mark_delivered a second time
        mock_webhook_repo_phase3.mark_delivered.assert_not_called()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_skips_schedule_retry_when_already_delivered(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """If the HTTP call fails but the event was already delivered by a concurrent
        worker (status='delivered'), Phase 3 must skip schedule_retry."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_race2"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        # Another worker delivered it while our HTTP call was in-flight
        mock_stale_event = MagicMock()
        mock_stale_event.status = WebhookStatus.DELIVERED
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_stale_event

        mock_webhook_repo_cls.side_effect = [
            mock_webhook_repo_phase1,
            mock_webhook_repo_phase3,
        ]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = None
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=False, error="HTTP 500"),
        ):
            result = await svc.process_event("evt_race2")

        assert result.success is False
        # Phase 3 must skip retry when event is already in terminal state
        mock_webhook_repo_phase3.schedule_retry.assert_not_called()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_skips_write_when_event_is_failed(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """Status 'failed' (max retries exhausted) is also a terminal state;
        Phase 3 must skip writing."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_failed"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        mock_stale_event = MagicMock()
        mock_stale_event.status = WebhookStatus.FAILED
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_stale_event

        mock_webhook_repo_cls.side_effect = [
            mock_webhook_repo_phase1,
            mock_webhook_repo_phase3,
        ]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = None
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event("evt_failed")

        assert result.success is True
        mock_webhook_repo_phase3.mark_delivered.assert_not_called()
        mock_webhook_repo_phase3.schedule_retry.assert_not_called()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_phase3_get_by_id_returns_none_is_handled(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """If the event row has disappeared (e.g. deleted) between Phase 1 and Phase 3,
        Phase 3 must not raise; it simply skips the write."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_gone"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        mock_webhook_repo_phase3 = MagicMock()
        # Row gone
        mock_webhook_repo_phase3.get_by_id.return_value = None

        mock_webhook_repo_cls.side_effect = [
            mock_webhook_repo_phase1,
            mock_webhook_repo_phase3,
        ]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = "whsec_x"
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            # Must not raise
            result = await svc.process_event("evt_gone")

        assert result.success is True
        mock_webhook_repo_phase3.mark_delivered.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# RED 3: Happy path regression — still works correctly after the fix
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessEventHappyPathRegression:
    """Existing happy-path behaviour must be preserved after the TOCTOU fix."""

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_successful_delivery_still_marks_delivered(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """When HTTP succeeds and event is still pending, mark_delivered is called."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_ok"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        # Phase 3: event still pending (normal case)
        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_current_event

        mock_webhook_repo_cls.side_effect = [
            mock_webhook_repo_phase1,
            mock_webhook_repo_phase3,
        ]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = "whsec_ok"
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event("evt_ok")

        assert result.success is True
        mock_webhook_repo_phase3.mark_delivered.assert_called_once_with(
            "evt_ok", 200, "OK"
        )

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_failed_delivery_still_schedules_retry(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """When HTTP fails and event is still pending/retrying, schedule_retry is called."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_retry"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_current_event

        mock_webhook_repo_cls.side_effect = [
            mock_webhook_repo_phase1,
            mock_webhook_repo_phase3,
        ]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = None
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=False, error="HTTP 503"),
        ):
            result = await svc.process_event("evt_retry")

        assert result.success is False
        mock_webhook_repo_phase3.schedule_retry.assert_called_once_with(
            "evt_retry", "HTTP 503"
        )

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_event_not_found_returns_failure(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """Missing event still returns failure result (unchanged behaviour)."""
        mock_db = MagicMock()
        mock_get_db.return_value = _make_get_db_mock(mock_db)

        mock_webhook_repo = MagicMock()
        mock_webhook_repo.get_by_id_for_update.return_value = None
        mock_webhook_repo_cls.return_value = mock_webhook_repo

        svc = WebhookService()
        result = await svc.process_event("nonexistent")

        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_no_webhook_url_marks_delivered_in_phase1(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """When agent has no webhook_url, event is marked delivered immediately in Phase 1
        (single session — Phase 3 never runs)."""
        mock_db = MagicMock()
        mock_get_db.return_value = _make_get_db_mock(mock_db)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_nowh"}
        mock_event.status = WebhookStatus.PENDING

        mock_agent = MagicMock()
        mock_agent.webhook_url = None

        mock_webhook_repo = MagicMock()
        mock_webhook_repo.get_by_id_for_update.return_value = mock_event
        mock_webhook_repo_cls.return_value = mock_webhook_repo

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        result = await svc.process_event("evt_nowh")

        assert result.success is True
        mock_webhook_repo.mark_delivered.assert_called_once()
        # get_db called only once — Phase 3 never ran
        assert mock_get_db.call_count == 1

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_retrying_event_is_also_processed_normally(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db
    ):
        """Events with status 'retrying' must also be processed (not skipped as stale)."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_retrying"}
        mock_event.status = WebhookStatus.RETRYING

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.RETRYING
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_current_event

        mock_webhook_repo_cls.side_effect = [
            mock_webhook_repo_phase1,
            mock_webhook_repo_phase3,
        ]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = "whsec_r"
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event("evt_retrying")

        assert result.success is True
        mock_webhook_repo_phase3.mark_delivered.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# RED 4: WebhookRepository.get_by_id_for_update uses with_for_update()
# ─────────────────────────────────────────────────────────────────────────────


class TestWebhookRepositoryGetByIdForUpdate:
    """The new get_by_id_for_update() repo method must issue with_for_update()."""

    def test_get_by_id_for_update_calls_with_for_update(self):
        """get_by_id_for_update() must call with_for_update() on the query."""
        from sthrip.db.repository import WebhookRepository

        mock_db = MagicMock()

        # Build a query chain that tracks with_for_update()
        mock_query = MagicMock()
        mock_filter = MagicMock()
        mock_for_update = MagicMock()
        mock_event = MagicMock()

        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_filter
        mock_filter.with_for_update.return_value = mock_for_update
        mock_for_update.first.return_value = mock_event

        repo = WebhookRepository(mock_db)
        result = repo.get_by_id_for_update("some-uuid")

        mock_filter.with_for_update.assert_called_once_with()
        assert result is mock_event

    def test_get_by_id_for_update_filters_by_id(self):
        """get_by_id_for_update() must filter by the given event_id."""
        from sthrip.db.repository import WebhookRepository
        from sthrip.db.models import WebhookEvent

        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_filter = MagicMock()
        mock_for_update = MagicMock()

        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_filter
        mock_filter.with_for_update.return_value = mock_for_update
        mock_for_update.first.return_value = None

        repo = WebhookRepository(mock_db)
        repo.get_by_id_for_update("target-uuid")

        mock_db.query.assert_called_once_with(WebhookEvent)
