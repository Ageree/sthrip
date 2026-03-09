"""Tests for webhook delivery service"""
import json
import hashlib
import hmac
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock

from sthrip.services.webhook_service import (
    WebhookResult,
    WebhookService,
    queue_webhook,
    get_webhook_service,
)


# ─────────────────────────────────────────────────────────────────────────────
# WebhookResult dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestWebhookResult:
    def test_defaults(self):
        r = WebhookResult(success=True)
        assert r.success is True
        assert r.response_code is None
        assert r.response_body is None
        assert r.error is None

    def test_full_creation(self):
        r = WebhookResult(
            success=False,
            response_code=500,
            response_body="Internal Server Error",
            error="HTTP 500",
        )
        assert r.success is False
        assert r.response_code == 500
        assert r.error == "HTTP 500"


# ─────────────────────────────────────────────────────────────────────────────
# WebhookService init
# ─────────────────────────────────────────────────────────────────────────────


class TestWebhookServiceInit:
    def test_default_max_retries(self):
        svc = WebhookService()
        assert svc.max_retries == 5
        assert svc._running is False
        assert svc._session is None

    def test_custom_max_retries(self):
        svc = WebhookService(max_retries=10)
        assert svc.max_retries == 10


# ─────────────────────────────────────────────────────────────────────────────
# _sign_payload
# ─────────────────────────────────────────────────────────────────────────────


class TestSignPayload:
    def test_correct_hmac(self):
        svc = WebhookService()
        payload = {"event": "test"}
        secret = "mysecret"
        timestamp = "1700000000"

        sig = svc._sign_payload(payload, secret, timestamp)

        # Verify manually
        payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        message = f"{timestamp}.{payload_str}"
        expected = hmac.new(
            secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()

        assert sig == f"sha256={expected}"

    def test_different_payloads_different_signatures(self):
        svc = WebhookService()
        ts = "1700000000"
        sig1 = svc._sign_payload({"a": 1}, "secret", ts)
        sig2 = svc._sign_payload({"b": 2}, "secret", ts)
        assert sig1 != sig2

    def test_same_inputs_same_signature(self):
        svc = WebhookService()
        payload = {"x": "y"}
        sig1 = svc._sign_payload(payload, "s", "100")
        sig2 = svc._sign_payload(payload, "s", "100")
        assert sig1 == sig2

    def test_different_secrets_different_signatures(self):
        svc = WebhookService()
        payload = {"x": 1}
        sig1 = svc._sign_payload(payload, "secret1", "100")
        sig2 = svc._sign_payload(payload, "secret2", "100")
        assert sig1 != sig2


# ─────────────────────────────────────────────────────────────────────────────
# _send_webhook
# ─────────────────────────────────────────────────────────────────────────────


class TestSendWebhook:
    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.validate_url_target", side_effect=Exception("SSRF"))
    async def test_ssrf_blocked(self, mock_validate):
        """SSRF blocked URLs should return failure without sending request."""
        from sthrip.services.url_validator import SSRFBlockedError

        mock_validate.side_effect = SSRFBlockedError("blocked")
        svc = WebhookService()
        result = await svc._send_webhook("http://169.254.169.254/metadata", {"e": 1})

        assert result.success is False
        assert "SSRF" in result.error

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.validate_url_target")
    async def test_successful_send(self, mock_validate):
        svc = WebhookService()

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        with patch.object(svc, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = await svc._send_webhook(
                "https://example.com/hook",
                {"event_id": "evt_123", "type": "test"},
                secret="sec",
            )

        assert result.success is True
        assert result.response_code == 200

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.validate_url_target")
    async def test_non_2xx_returns_failure(self, mock_validate):
        svc = WebhookService()

        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.text = AsyncMock(return_value="Server Error")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        with patch.object(svc, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = await svc._send_webhook(
                "https://example.com/hook", {"event_id": "evt_1"}
            )

        assert result.success is False
        assert result.response_code == 500
        assert "500" in result.error

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.validate_url_target")
    async def test_timeout_returns_failure(self, mock_validate):
        svc = WebhookService()

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=asyncio.TimeoutError())

        with patch.object(svc, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = await svc._send_webhook("https://example.com/hook", {"event_id": "e"})

        assert result.success is False
        assert "timeout" in result.error.lower()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.validate_url_target")
    async def test_client_error_returns_failure(self, mock_validate):
        import aiohttp

        svc = WebhookService()
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            side_effect=aiohttp.ClientError("connection refused")
        )

        with patch.object(svc, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = await svc._send_webhook("https://example.com/hook", {"event_id": "e"})

        assert result.success is False
        assert "Client error" in result.error


# ─────────────────────────────────────────────────────────────────────────────
# queue_event
# ─────────────────────────────────────────────────────────────────────────────


class TestQueueEvent:
    @patch("sthrip.services.webhook_service.get_db")
    def test_creates_event_and_returns_id(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_event = MagicMock()
        mock_event.id = 42

        mock_repo_cls = MagicMock()
        mock_repo_instance = MagicMock()
        mock_repo_instance.create_event.return_value = mock_event

        with patch("sthrip.services.webhook_service.WebhookRepository", return_value=mock_repo_instance):
            svc = WebhookService()
            event_id = svc.queue_event("agent_1", "payment.received", {"amount": "1.0"})

        assert event_id == "42"
        mock_repo_instance.create_event.assert_called_once()
        call_args = mock_repo_instance.create_event.call_args
        assert call_args[0][0] == "agent_1"
        assert call_args[0][1] == "payment.received"


# ─────────────────────────────────────────────────────────────────────────────
# get_delivery_stats
# ─────────────────────────────────────────────────────────────────────────────


class TestGetDeliveryStats:
    @patch("sthrip.services.webhook_service.get_db")
    def test_returns_stats_dict(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        # Set up query chain mocks
        mock_query = MagicMock()
        mock_db.query.return_value = mock_query
        mock_filter = MagicMock()
        mock_query.filter.return_value = mock_filter
        mock_filter.count.return_value = 10
        mock_filter.scalar.return_value = 1.5

        svc = WebhookService()
        stats = svc.get_delivery_stats(days=7)

        assert stats["period_days"] == 7
        assert "total_events" in stats
        assert "delivered" in stats
        assert "failed" in stats
        assert "pending" in stats
        assert "success_rate" in stats
        assert "average_attempts" in stats


# ─────────────────────────────────────────────────────────────────────────────
# close
# ─────────────────────────────────────────────────────────────────────────────


class TestClose:
    @pytest.mark.asyncio
    async def test_closes_open_session(self):
        svc = WebhookService()
        mock_session = AsyncMock()
        mock_session.closed = False
        svc._session = mock_session

        await svc.close()
        mock_session.close.assert_called_once()
        assert svc._session is None

    @pytest.mark.asyncio
    async def test_noop_when_no_session(self):
        svc = WebhookService()
        await svc.close()  # should not raise
        assert svc._session is None

    @pytest.mark.asyncio
    async def test_noop_when_already_closed(self):
        svc = WebhookService()
        mock_session = AsyncMock()
        mock_session.closed = True
        svc._session = mock_session

        await svc.close()
        mock_session.close.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# _get_session
# ─────────────────────────────────────────────────────────────────────────────


class TestGetSession:
    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.aiohttp.ClientSession")
    async def test_creates_session_when_none(self, mock_cls):
        svc = WebhookService()
        mock_instance = MagicMock()
        mock_instance.closed = False
        mock_cls.return_value = mock_instance

        session = await svc._get_session()
        mock_cls.assert_called_once()
        assert session is mock_instance

    @pytest.mark.asyncio
    async def test_returns_existing_open_session(self):
        svc = WebhookService()
        mock_session = MagicMock()
        mock_session.closed = False
        svc._session = mock_session

        session = await svc._get_session()
        assert session is mock_session

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.aiohttp.ClientSession")
    async def test_recreates_closed_session(self, mock_cls):
        svc = WebhookService()
        old_session = MagicMock()
        old_session.closed = True
        svc._session = old_session

        new_session = MagicMock()
        new_session.closed = False
        mock_cls.return_value = new_session

        session = await svc._get_session()
        assert session is new_session


# ─────────────────────────────────────────────────────────────────────────────
# start_worker / stop_worker lifecycle
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkerLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_running_stop_clears(self):
        svc = WebhookService()

        with patch.object(svc, "process_pending_events", new_callable=AsyncMock) as mock_pp:
            async def run_worker():
                await svc.start_worker(interval_seconds=0)

            task = asyncio.create_task(run_worker())
            await asyncio.sleep(0.05)

            assert svc._running is True

            svc.stop_worker()
            assert svc._running is False

            # Give the loop time to see _running=False
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def test_stop_worker_sets_flag(self):
        svc = WebhookService()
        svc._running = True
        svc.stop_worker()
        assert svc._running is False


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: queue_webhook
# ─────────────────────────────────────────────────────────────────────────────


class TestQueueWebhookConvenience:
    @patch("sthrip.services.webhook_service._service", None)
    @patch("sthrip.services.webhook_service.get_db")
    def test_queue_webhook_delegates(self, mock_get_db):
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_event = MagicMock()
        mock_event.id = 99

        with patch("sthrip.services.webhook_service.WebhookRepository") as mock_repo_cls:
            mock_repo_cls.return_value.create_event.return_value = mock_event
            result = queue_webhook("agent_x", "payment.sent", {"amt": "5"})

        assert result == "99"


# ─────────────────────────────────────────────────────────────────────────────
# _send_webhook — generic exception branch (lines 130-131)
# ─────────────────────────────────────────────────────────────────────────────


class TestSendWebhookGenericException:
    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.validate_url_target")
    async def test_generic_exception_returns_failure(self, mock_validate):
        """A non-aiohttp, non-timeout exception is caught as 'Unexpected error'."""
        svc = WebhookService()
        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=TypeError("bad type"))

        with patch.object(svc, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = await svc._send_webhook("https://example.com/hook", {"event_id": "e"})

        assert result.success is False
        assert "Unexpected error" in result.error


# ─────────────────────────────────────────────────────────────────────────────
# process_event (lines 163-199)
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessEvent:
    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    async def test_event_not_found(self, mock_get_db):
        """Returns failure when event_id does not exist."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)
        mock_db.query.return_value.filter.return_value.first.return_value = None

        svc = WebhookService()
        result = await svc.process_event("nonexistent")
        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_no_webhook_url_marks_delivered(self, mock_repo_cls, mock_get_db):
        """When agent has no webhook_url, event is marked as delivered."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_1"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = None

        # First query returns event, second returns agent
        mock_db.query.return_value.filter.return_value.first.side_effect = [mock_event, mock_agent]

        mock_repo_instance = MagicMock()
        mock_repo_cls.return_value = mock_repo_instance

        svc = WebhookService()
        result = await svc.process_event("evt_1")
        assert result.success is True
        mock_repo_instance.mark_delivered.assert_called_once()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_successful_delivery(self, mock_repo_cls, mock_get_db):
        """Successful webhook delivery marks event as delivered."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_1"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.webhook_secret = "secret"

        mock_db.query.return_value.filter.return_value.first.side_effect = [mock_event, mock_agent]

        mock_repo_instance = MagicMock()
        mock_repo_cls.return_value = mock_repo_instance

        svc = WebhookService()
        with patch.object(svc, "_send_webhook", new_callable=AsyncMock,
                          return_value=WebhookResult(success=True, response_code=200, response_body="OK")):
            result = await svc.process_event("evt_1")

        assert result.success is True
        mock_repo_instance.mark_delivered.assert_called_once()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_failed_delivery_schedules_retry(self, mock_repo_cls, mock_get_db):
        """Failed webhook delivery schedules a retry."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_1"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.webhook_secret = None

        mock_db.query.return_value.filter.return_value.first.side_effect = [mock_event, mock_agent]

        mock_repo_instance = MagicMock()
        mock_repo_cls.return_value = mock_repo_instance

        svc = WebhookService()
        with patch.object(svc, "_send_webhook", new_callable=AsyncMock,
                          return_value=WebhookResult(success=False, error="HTTP 500")):
            result = await svc.process_event("evt_1")

        assert result.success is False
        mock_repo_instance.schedule_retry.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# process_pending_events (lines 203-220)
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessPendingEvents:
    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_processes_batch(self, mock_repo_cls, mock_get_db):
        """Processes batch of pending events and returns counts."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_event_1 = MagicMock()
        mock_event_1.id = "e1"
        mock_event_2 = MagicMock()
        mock_event_2.id = "e2"

        mock_repo_instance = MagicMock()
        mock_repo_instance.get_pending_events.return_value = [mock_event_1, mock_event_2]
        mock_repo_cls.return_value = mock_repo_instance

        svc = WebhookService()
        with patch.object(svc, "process_event", new_callable=AsyncMock,
                          side_effect=[
                              WebhookResult(success=True),
                              WebhookResult(success=False, error="fail"),
                          ]):
            stats = await svc.process_pending_events(batch_size=10)

        assert stats["processed"] == 2
        assert stats["successful"] == 1
        assert stats["failed"] == 1

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_empty_batch(self, mock_repo_cls, mock_get_db):
        """Returns zeros when no pending events."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_repo_instance = MagicMock()
        mock_repo_instance.get_pending_events.return_value = []
        mock_repo_cls.return_value = mock_repo_instance

        svc = WebhookService()
        stats = await svc.process_pending_events()
        assert stats["processed"] == 0
        assert stats["successful"] == 0
        assert stats["failed"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# start_worker error handling (lines 233-234)
# ─────────────────────────────────────────────────────────────────────────────


class TestStartWorkerErrorHandling:
    @pytest.mark.asyncio
    async def test_worker_continues_on_error(self):
        """Worker catches exceptions and keeps running."""
        svc = WebhookService()
        call_count = 0

        async def mock_process():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")
            # Stop after second call
            svc.stop_worker()

        with patch.object(svc, "process_pending_events", side_effect=mock_process):
            await svc.start_worker(interval_seconds=0)

        assert call_count == 2
