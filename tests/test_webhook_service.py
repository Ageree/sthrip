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
    @patch("sthrip.services.webhook_service.resolve_and_validate", side_effect=Exception("SSRF"))
    async def test_ssrf_blocked(self, mock_validate):
        """SSRF blocked URLs should return failure without sending request."""
        from sthrip.services.url_validator import SSRFBlockedError

        mock_validate.side_effect = SSRFBlockedError("blocked")
        svc = WebhookService()
        result = await svc._send_webhook("http://169.254.169.254/metadata", {"e": 1})

        assert result.success is False
        assert "SSRF" in result.error

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.resolve_and_validate",
           return_value=("https://example.com/hook", "93.184.216.34"))
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
    @patch("sthrip.services.webhook_service.resolve_and_validate",
           return_value=("https://example.com/hook", "93.184.216.34"))
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
    @patch("sthrip.services.webhook_service.resolve_and_validate",
           return_value=("https://example.com/hook", "93.184.216.34"))
    async def test_timeout_returns_failure(self, mock_validate):
        svc = WebhookService()

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=asyncio.TimeoutError())

        with patch.object(svc, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = await svc._send_webhook("https://example.com/hook", {"event_id": "e"})

        assert result.success is False
        assert "timeout" in result.error.lower()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.resolve_and_validate",
           return_value=("https://example.com/hook", "93.184.216.34"))
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


# ─────────────────────────────────────────────────────────────────────────────
# _send_webhook — DNS rebinding prevention (IP pinning)
# ─────────────────────────────────────────────────────────────────────────────


class TestSendWebhookDNSPinning:
    """Verify that _send_webhook pins the HTTP request to the resolved IP,
    preventing DNS rebinding attacks between validation and connection."""

    @pytest.mark.asyncio
    async def test_request_uses_resolved_ip_with_host_header(self):
        """The HTTP POST must target the resolved IP (not hostname) and set
        the Host header to the original hostname."""
        from sthrip.services.url_validator import resolve_and_validate

        svc = WebhookService()
        resolved_ip = "93.184.216.34"

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        with patch(
            "sthrip.services.webhook_service.resolve_and_validate",
            return_value=("https://example.com/hook", resolved_ip),
        ), patch.object(
            svc, "_get_session", new_callable=AsyncMock, return_value=mock_session
        ):
            result = await svc._send_webhook(
                "https://example.com/hook",
                {"event_id": "evt_pin", "type": "test"},
                secret="sec",
            )

        assert result.success is True

        # Verify the actual URL passed to session.post uses the resolved IP
        call_args = mock_session.post.call_args
        actual_url = call_args[0][0] if call_args[0] else call_args[1]["url"]
        assert resolved_ip in actual_url
        assert "example.com" not in actual_url

        # Verify Host header is set to original hostname
        actual_headers = call_args[1].get("headers") or call_args[0][2] if len(call_args[0]) > 2 else call_args[1].get("headers")
        assert actual_headers["Host"] == "example.com"

    @pytest.mark.asyncio
    async def test_ssrf_blocked_by_resolve_and_validate(self):
        """When resolve_and_validate raises SSRFBlockedError, request is not sent."""
        from sthrip.services.url_validator import SSRFBlockedError

        svc = WebhookService()

        with patch(
            "sthrip.services.webhook_service.resolve_and_validate",
            side_effect=SSRFBlockedError("resolves to private IP"),
        ):
            result = await svc._send_webhook(
                "https://evil.com/hook", {"event_id": "evt_bad"}
            )

        assert result.success is False
        assert "SSRF" in result.error


class TestSendWebhookGenericException:
    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.resolve_and_validate",
           return_value=("https://example.com/hook", "93.184.216.34"))
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
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_event_not_found(self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db):
        """Returns failure when event_id does not exist."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_webhook_repo = MagicMock()
        # Phase 1 now uses the locking read
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
    async def test_no_webhook_url_marks_delivered(self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db):
        """When agent has no webhook_url, event is marked as delivered."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_1"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = None

        mock_webhook_repo = MagicMock()
        # Phase 1 now uses the locking read
        mock_webhook_repo.get_by_id_for_update.return_value = mock_event
        mock_webhook_repo_cls.return_value = mock_webhook_repo

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        result = await svc.process_event("evt_1")
        assert result.success is True
        mock_webhook_repo.mark_delivered.assert_called_once()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_successful_delivery(self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db):
        """Successful webhook delivery marks event as delivered."""
        from sthrip.db.models import WebhookStatus

        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        ctx1 = MagicMock()
        ctx1.__enter__ = MagicMock(return_value=mock_db1)
        ctx1.__exit__ = MagicMock(return_value=False)
        ctx2 = MagicMock()
        ctx2.__enter__ = MagicMock(return_value=mock_db2)
        ctx2.__exit__ = MagicMock(return_value=False)
        mock_get_db.side_effect = [ctx1, ctx2]

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_1"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.webhook_secret = "secret"
        mock_agent.id = "agent_1"

        # AgentRepository.get_webhook_secret returns decrypted plaintext
        mock_agent_repo_instance = MagicMock()
        mock_agent_repo_instance.get_by_id.return_value = mock_agent
        mock_agent_repo_instance.get_webhook_secret.return_value = "whsec_plaintext"
        mock_agent_repo_cls.return_value = mock_agent_repo_instance

        # Phase 1 repo: locking read returns the event
        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        # Phase 3 repo: status check returns pending so write proceeds
        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_current_event

        mock_webhook_repo_cls.side_effect = [mock_webhook_repo_phase1, mock_webhook_repo_phase3]

        svc = WebhookService()
        with patch.object(svc, "_send_webhook", new_callable=AsyncMock,
                          return_value=WebhookResult(success=True, response_code=200, response_body="OK")):
            result = await svc.process_event("evt_1")

        assert result.success is True
        mock_webhook_repo_phase3.mark_delivered.assert_called_once()

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_failed_delivery_schedules_retry(self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db):
        """Failed webhook delivery schedules a retry."""
        from sthrip.db.models import WebhookStatus

        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        ctx1 = MagicMock()
        ctx1.__enter__ = MagicMock(return_value=mock_db1)
        ctx1.__exit__ = MagicMock(return_value=False)
        ctx2 = MagicMock()
        ctx2.__enter__ = MagicMock(return_value=mock_db2)
        ctx2.__exit__ = MagicMock(return_value=False)
        mock_get_db.side_effect = [ctx1, ctx2]

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_1"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.webhook_secret = None
        mock_agent.id = "agent_1"

        # AgentRepository.get_webhook_secret returns None (no secret configured)
        mock_agent_repo_instance = MagicMock()
        mock_agent_repo_instance.get_by_id.return_value = mock_agent
        mock_agent_repo_instance.get_webhook_secret.return_value = None
        mock_agent_repo_cls.return_value = mock_agent_repo_instance

        # Phase 1 repo: locking read returns the event
        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        # Phase 3 repo: status check returns pending so write proceeds
        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_current_event

        mock_webhook_repo_cls.side_effect = [mock_webhook_repo_phase1, mock_webhook_repo_phase3]

        svc = WebhookService()
        with patch.object(svc, "_send_webhook", new_callable=AsyncMock,
                          return_value=WebhookResult(success=False, error="HTTP 500")):
            result = await svc.process_event("evt_1")

        assert result.success is False
        mock_webhook_repo_phase3.schedule_retry.assert_called_once()


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


# ─────────────────────────────────────────────────────────────────────────────
# process_event — session split (HIGH-3)
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessEventSessionSplit:
    """Verify that process_event uses separate DB sessions for read and write,
    so the HTTP call happens without holding a DB connection."""

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_uses_two_separate_sessions(self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db):
        """get_db() should be called twice: once for read, once for write."""
        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        call_count = [0]

        class _FakeCtx:
            def __init__(self, db):
                self._db = db
            def __enter__(self):
                return self._db
            def __exit__(self, *a):
                return False

        def _side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeCtx(mock_db1)
            return _FakeCtx(mock_db2)

        mock_get_db.side_effect = _side_effect

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_split"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        from sthrip.db.models import WebhookStatus

        # Phase 1 repo: locking read
        mock_webhook_repo_phase1 = MagicMock()
        mock_webhook_repo_phase1.get_by_id_for_update.return_value = mock_event

        # Phase 3 repo: status still pending
        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_webhook_repo_phase3 = MagicMock()
        mock_webhook_repo_phase3.get_by_id.return_value = mock_current_event

        mock_webhook_repo_cls.side_effect = [mock_webhook_repo_phase1, mock_webhook_repo_phase3]

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = "whsec_test"
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(svc, "_send_webhook", new_callable=AsyncMock,
                          return_value=WebhookResult(success=True, response_code=200, response_body="OK")):
            result = await svc.process_event("evt_split")

        assert result.success is True
        # get_db was called exactly twice (read + write)
        assert call_count[0] == 2

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_no_agent_uses_single_session(self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db):
        """When agent has no webhook_url, only one session needed (mark_delivered in phase 1)."""
        mock_db = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_1"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = None

        mock_webhook_repo = MagicMock()
        # Phase 1 uses locking read
        mock_webhook_repo.get_by_id_for_update.return_value = mock_event
        mock_webhook_repo_cls.return_value = mock_webhook_repo

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        result = await svc.process_event("evt_1")
        assert result.success is True
        # get_db called only once (no HTTP call, no second session needed)
        assert mock_get_db.call_count == 1

    @pytest.mark.asyncio
    @patch("sthrip.services.webhook_service.get_db")
    @patch("sthrip.services.webhook_service.AgentRepository")
    @patch("sthrip.services.webhook_service.WebhookRepository")
    async def test_failed_delivery_writes_retry_in_phase3(self, mock_webhook_repo_cls, mock_agent_repo_cls, mock_get_db):
        """Failed delivery should schedule_retry in the second (write) session."""
        from sthrip.db.models import WebhookStatus

        mock_db1 = MagicMock()
        mock_db2 = MagicMock()
        call_count = [0]

        class _FakeCtx:
            def __init__(self, db):
                self._db = db
            def __enter__(self):
                return self._db
            def __exit__(self, *a):
                return False

        def _side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return _FakeCtx(mock_db1)
            return _FakeCtx(mock_db2)

        mock_get_db.side_effect = _side_effect

        mock_event = MagicMock()
        mock_event.agent_id = "agent_1"
        mock_event.payload = {"event_id": "evt_fail"}

        mock_agent = MagicMock()
        mock_agent.webhook_url = "https://example.com/hook"
        mock_agent.id = "agent_1"

        # Phase 1 webhook repo (read) — locking read
        mock_webhook_repo_read = MagicMock()
        mock_webhook_repo_read.get_by_id_for_update.return_value = mock_event

        # Phase 3 webhook repo (write) — status still pending so write proceeds
        mock_current_event = MagicMock()
        mock_current_event.status = WebhookStatus.PENDING
        mock_webhook_repo_write = MagicMock()
        mock_webhook_repo_write.get_by_id.return_value = mock_current_event

        webhook_repos = [mock_webhook_repo_read, mock_webhook_repo_write]
        mock_webhook_repo_cls.side_effect = webhook_repos

        mock_agent_repo = MagicMock()
        mock_agent_repo.get_by_id.return_value = mock_agent
        mock_agent_repo.get_webhook_secret.return_value = None
        mock_agent_repo_cls.return_value = mock_agent_repo

        svc = WebhookService()
        with patch.object(svc, "_send_webhook", new_callable=AsyncMock,
                          return_value=WebhookResult(success=False, error="HTTP 500")):
            result = await svc.process_event("evt_fail")

        assert result.success is False
        mock_webhook_repo_write.schedule_retry.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# WebhookRepository.get_pending_events uses FOR UPDATE SKIP LOCKED
# ─────────────────────────────────────────────────────────────────────────────

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sthrip.db.models import Base, WebhookEvent, WebhookStatus, Agent, AgentReputation
from sthrip.db.repository import WebhookRepository


class TestGetPendingEventsLocking:
    @pytest.fixture
    def db_session(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine, tables=[
            Agent.__table__,
            AgentReputation.__table__,
            WebhookEvent.__table__,
        ])
        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()

    @pytest.fixture
    def agent(self, db_session):
        from sthrip.db.models import AgentTier, RateLimitTier, PrivacyLevel
        agent = Agent(
            agent_name="webhook-test-agent",
            api_key_hash="webhooktesthash",
            tier=AgentTier.FREE,
            rate_limit_tier=RateLimitTier.STANDARD,
            privacy_level=PrivacyLevel.MEDIUM,
            is_active=True,
        )
        db_session.add(agent)
        db_session.flush()
        return agent

    def test_get_pending_events_uses_skip_locked(self, db_session, agent):
        """get_pending_events must apply with_for_update(skip_locked=True)."""
        repo = WebhookRepository(db_session)

        # Create 3 pending events
        for i in range(3):
            repo.create_event(agent.id, "test.event", {"i": i})
        db_session.flush()

        # Monkey-patch the query to track with_for_update calls
        original_query = db_session.query

        for_update_called = {"skip_locked": None}

        class _TrackingQuery:
            def __init__(self, *args, **kwargs):
                self._q = original_query(*args, **kwargs)

            def __getattr__(self, name):
                result = getattr(self._q, name)
                if callable(result):
                    def wrapper(*a, **kw):
                        r = result(*a, **kw)
                        if name == "with_for_update":
                            for_update_called["skip_locked"] = kw.get("skip_locked", False)
                        # Wrap returned query-like objects
                        if hasattr(r, "filter") or hasattr(r, "order_by") or hasattr(r, "limit"):
                            return _TrackingQuery._wrap(r, for_update_called)
                        return r
                    return wrapper
                return result

            @staticmethod
            def _wrap(q, tracker):
                """Wrap a query to track with_for_update."""
                original_wfu = getattr(q, "with_for_update", None)
                if original_wfu:
                    def tracked_wfu(*a, **kw):
                        tracker["skip_locked"] = kw.get("skip_locked", False)
                        return original_wfu(*a, **kw)
                    q.with_for_update = tracked_wfu
                return q

        db_session.query = lambda *a, **kw: _TrackingQuery(*a, **kw)

        events = repo.get_pending_events(limit=2)
        assert len(events) == 2
        assert for_update_called["skip_locked"] is True
