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

import uuid

import pytest
from unittest.mock import MagicMock, AsyncMock, patch, call

from sthrip.services.webhook_service import WebhookResult, WebhookService
from sthrip.db.models import WebhookStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Stable UUIDs for deterministic test assertions
EVT_LOCK     = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))
EVT_LOCK2    = str(uuid.UUID("00000000-0000-0000-0000-000000000002"))
EVT_RACE     = str(uuid.UUID("00000000-0000-0000-0000-000000000003"))
EVT_RACE2    = str(uuid.UUID("00000000-0000-0000-0000-000000000004"))
EVT_FAILED   = str(uuid.UUID("00000000-0000-0000-0000-000000000005"))
EVT_GONE     = str(uuid.UUID("00000000-0000-0000-0000-000000000006"))
EVT_OK       = str(uuid.UUID("00000000-0000-0000-0000-000000000007"))
EVT_RETRY    = str(uuid.UUID("00000000-0000-0000-0000-000000000008"))
EVT_NONEXIST = str(uuid.UUID("00000000-0000-0000-0000-000000000009"))
EVT_NOWH     = str(uuid.UUID("00000000-0000-0000-0000-00000000000a"))
EVT_RETRYING = str(uuid.UUID("00000000-0000-0000-0000-00000000000b"))


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


def _make_mock_event(event_id: str, agent_id: str = "agent_1",
                     status: WebhookStatus = WebhookStatus.PENDING,
                     event_type: str = "payment.received") -> MagicMock:
    """Build a mock WebhookEvent with all attributes the fan-out code accesses."""
    mock = MagicMock()
    mock.id = event_id
    mock.agent_id = agent_id
    mock.event_type = event_type
    mock.payload = {"event_id": event_id}
    mock.status = status
    return mock


def _make_mock_agent(agent_id: str = "agent_1",
                     webhook_url: str = "https://example.com/hook") -> MagicMock:
    """Build a mock Agent with all attributes the fan-out code accesses."""
    mock = MagicMock()
    mock.id = agent_id
    mock.webhook_url = webhook_url
    return mock


def _empty_endpoint_repo() -> MagicMock:
    """Return a WebhookEndpointRepository mock that returns no registered endpoints."""
    repo = MagicMock()
    repo.list_by_agent.return_value = []
    return repo


# ─────────────────────────────────────────────────────────────────────────────
# RED 1: Phase 1 read must use with_for_update()
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessEventUsesForUpdate:
    """process_event() must lock the event row in Phase 1 via with_for_update()."""

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_phase1_get_by_id_uses_with_for_update(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """get_by_id() in Phase 1 must call with_for_update() on the query."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = _make_mock_event(EVT_LOCK)
        mock_agent = _make_mock_agent()

        mock_webhook_repo = MagicMock()
        mock_webhook_repo.get_by_id_for_update.return_value = mock_event
        mock_webhook_repo_cls.return_value = mock_webhook_repo

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = "whsec_test"
        mock_agent_repo_cls.return_value = mock_agent_repo

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event(EVT_LOCK)

        assert result.success is True
        # The repo must expose a locking read method that was called
        mock_webhook_repo.get_by_id_for_update.assert_called_once()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_phase1_does_not_fall_back_to_unlocked_read(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """process_event() Phase 1 must NOT call the non-locking get_by_id().
        The Phase 1 repo (first WebhookRepository instance) must only call
        get_by_id_for_update(), not the plain get_by_id().
        Phase 3 is permitted to call get_by_id() on its own separate repo instance.
        """
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = _make_mock_event(EVT_LOCK2)
        mock_agent = _make_mock_agent()

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

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            await svc.process_event(EVT_LOCK2)

        # The Phase 1 repo instance must NOT have called the plain get_by_id
        mock_webhook_repo_phase1.get_by_id.assert_not_called()
        # The Phase 1 repo must have called the locking variant
        mock_webhook_repo_phase1.get_by_id_for_update.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# RED 2: Phase 3 must skip write when event is no longer pending/retrying
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessEventPhase3StatusCheck:
    """Phase 3 must re-check event status before writing; skip if already delivered."""

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_skips_mark_delivered_when_already_delivered(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """If another worker delivered the event between Phase 1 and Phase 3,
        Phase 3 must not call mark_delivered again (no double write)."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = _make_mock_event(EVT_RACE)
        mock_agent = _make_mock_agent()

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

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event(EVT_RACE)

        # The method must return success (it did the HTTP work) without crashing
        assert result.success is True
        # But Phase 3 must NOT call mark_delivered a second time
        mock_webhook_repo_phase3.mark_delivered.assert_not_called()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_skips_schedule_retry_when_already_delivered(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """If the HTTP call fails but the event was already delivered by a concurrent
        worker (status='delivered'), Phase 3 must skip schedule_retry."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = _make_mock_event(EVT_RACE2)
        mock_agent = _make_mock_agent()

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

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=False, error="HTTP 500"),
        ):
            result = await svc.process_event(EVT_RACE2)

        assert result.success is False
        # Phase 3 must skip retry when event is already in terminal state
        mock_webhook_repo_phase3.schedule_retry.assert_not_called()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_skips_write_when_event_is_failed(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """Status 'failed' (max retries exhausted) is also a terminal state;
        Phase 3 must skip writing."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = _make_mock_event(EVT_FAILED)
        mock_agent = _make_mock_agent()

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

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event(EVT_FAILED)

        assert result.success is True
        mock_webhook_repo_phase3.mark_delivered.assert_not_called()
        mock_webhook_repo_phase3.schedule_retry.assert_not_called()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_phase3_get_by_id_returns_none_is_handled(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """If the event row has disappeared (e.g. deleted) between Phase 1 and Phase 3,
        Phase 3 must not raise; it simply skips the write."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = _make_mock_event(EVT_GONE)
        mock_agent = _make_mock_agent()

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

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            # Must not raise
            result = await svc.process_event(EVT_GONE)

        assert result.success is True
        mock_webhook_repo_phase3.mark_delivered.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# RED 3: Happy path regression — still works correctly after the fix
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessEventHappyPathRegression:
    """Existing happy-path behaviour must be preserved after the TOCTOU fix."""

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_successful_delivery_still_marks_delivered(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """When HTTP succeeds and event is still pending, mark_delivered is called."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        evt_uuid = uuid.UUID(EVT_OK)
        mock_event = _make_mock_event(EVT_OK)
        mock_agent = _make_mock_agent()

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        # Phase 3: event still pending (normal case)
        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_current_event.agent_id = "agent_1"
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

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event(EVT_OK)

        assert result.success is True
        # Fan-out marks delivered with aggregate message
        mock_webhook_repo_phase3.mark_delivered.assert_called_once_with(
            evt_uuid, 200, "Fan-out delivery"
        )

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_failed_delivery_still_schedules_retry(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """When HTTP fails and event is still pending/retrying, schedule_retry is called."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        evt_uuid = uuid.UUID(EVT_RETRY)
        mock_event = _make_mock_event(EVT_RETRY)
        mock_agent = _make_mock_agent()

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_current_event.agent_id = "agent_1"
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

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=False, error="HTTP 503"),
        ):
            result = await svc.process_event(EVT_RETRY)

        assert result.success is False
        # Fan-out propagates the first endpoint error
        mock_webhook_repo_phase3.schedule_retry.assert_called_once_with(
            evt_uuid, "HTTP 503"
        )

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_event_not_found_returns_failure(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """Missing event still returns failure result (unchanged behaviour)."""
        mock_db = MagicMock()
        mock_get_db.return_value = _make_get_db_mock(mock_db)

        mock_webhook_repo = MagicMock()
        mock_webhook_repo.get_by_id_for_update.return_value = None
        mock_webhook_repo_cls.return_value = mock_webhook_repo

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        result = await svc.process_event(EVT_NONEXIST)

        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_no_webhook_url_marks_delivered_in_phase1(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """When agent has no webhook_url and no registered endpoints, the event is
        marked delivered immediately in Phase 1 (single session)."""
        mock_db = MagicMock()
        mock_get_db.return_value = _make_get_db_mock(mock_db)

        mock_event = _make_mock_event(EVT_NOWH)
        mock_agent = _make_mock_agent(webhook_url=None)

        mock_webhook_repo = MagicMock()
        mock_webhook_repo.get_by_id_for_update.return_value = mock_event
        mock_webhook_repo_cls.return_value = mock_webhook_repo

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo_cls.return_value = mock_agent_repo

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        result = await svc.process_event(EVT_NOWH)

        assert result.success is True
        mock_webhook_repo.mark_delivered.assert_called_once()
        # get_db called only once — Phase 3 never ran
        assert mock_get_db.call_count == 1

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.WebhookEndpointRepository")
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_retrying_event_is_also_processed_normally(
        self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db,
        mock_endpoint_repo_cls,
    ):
        """Events with status 'retrying' must also be processed (not skipped as stale)."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        mock_get_db.side_effect = _make_get_db_sequence(mock_db1, mock_db2)

        mock_event = _make_mock_event(EVT_RETRYING, status=WebhookStatus.RETRYING)
        mock_agent = _make_mock_agent()

        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.RETRYING
        mock_current_event.agent_id = "agent_1"
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

        mock_endpoint_repo_cls.return_value = _empty_endpoint_repo()

        svc = WebhookService()
        with patch.object(
            svc,
            "_send_webhook",
            new_callable=AsyncMock,
            return_value=WebhookResult(success=True, response_code=200, response_body="OK"),
        ):
            result = await svc.process_event(EVT_RETRYING)

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
