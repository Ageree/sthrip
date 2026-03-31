"""
Reliable webhook delivery service
Retries with exponential backoff, fan-out to multiple registered endpoints
"""

import json
import hashlib
import threading
import hmac
import asyncio
import logging
import aiohttp
from fnmatch import fnmatch
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("sthrip.webhook")
from typing import Dict, Optional, List
from dataclasses import dataclass, field

from ..db.database import get_db
from ..db.repository import WebhookRepository, AgentRepository
from ..db.webhook_endpoint_repo import WebhookEndpointRepository
from ..db.models import WebhookEvent, WebhookStatus, WebhookEndpoint
from ..crypto import decrypt_value
from .url_validator import validate_url_target, resolve_and_validate, SSRFBlockedError

@dataclass
class WebhookResult:
    """Webhook delivery result"""
    success: bool
    response_code: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None


@dataclass
class EndpointDeliveryResult:
    """Result for a single endpoint delivery within a fan-out."""
    endpoint_url: str
    success: bool
    response_code: Optional[int] = None
    error: Optional[str] = None


@dataclass
class FanoutResult:
    """Aggregate result for fan-out delivery to multiple endpoints."""
    success: bool
    total_endpoints: int = 0
    successful: int = 0
    failed: int = 0
    endpoint_results: List[EndpointDeliveryResult] = field(default_factory=list)


class WebhookService:
    """
    Reliable webhook delivery service
    
    Features:
    - Exponential backoff retries
    - HMAC signature verification
    - Delivery tracking
    - Dead letter queue for failed events
    """
    
    def __init__(self, max_retries: int = 5):
        self.max_retries = max_retries
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock: Optional[asyncio.Lock] = None  # lazy init for Python 3.9

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
    
    def _sign_payload(self, payload: Dict, secret: str, timestamp: str) -> str:
        """Sign webhook payload with HMAC (Stripe model: timestamp.payload)."""
        payload_str = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        message = f"{timestamp}.{payload_str}"
        signature = hmac.new(
            secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"
    
    async def _send_webhook(
        self,
        url: str,
        payload: Dict,
        secret: Optional[str] = None,
        timeout: int = 30
    ) -> WebhookResult:
        """Send single webhook request, pinning to resolved IP to prevent DNS rebinding."""
        # SSRF validation + DNS resolution: pin to resolved IP
        try:
            validated_url, resolved_ip = resolve_and_validate(url)
        except (SSRFBlockedError, ValueError) as e:
            return WebhookResult(
                success=False,
                error=f"SSRF blocked: {e}"
            )

        # Build IP-pinned URL: replace hostname with resolved IP
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(validated_url)
        original_hostname = parsed.hostname
        # Reconstruct netloc with IP instead of hostname (preserve port if any)
        # IPv6 addresses must be wrapped in brackets for valid URLs
        if ":" in resolved_ip:  # IPv6
            ip_part = f"[{resolved_ip}]"
        else:
            ip_part = resolved_ip

        if parsed.port:
            pinned_netloc = f"{ip_part}:{parsed.port}"
        else:
            pinned_netloc = ip_part
        pinned_url = urlunparse((
            parsed.scheme,
            pinned_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ))

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Sthrip-Webhook/1.0",
            "Host": original_hostname,
        }

        # Add timestamp and signature
        import time as _time
        timestamp = str(int(_time.time()))
        headers["X-Sthrip-Timestamp"] = timestamp
        if secret:
            headers["X-Sthrip-Signature"] = self._sign_payload(payload, secret, timestamp)
        headers["X-Sthrip-Event-ID"] = payload.get("event_id", "unknown")

        try:
            import ssl as _ssl
            ssl_ctx = None
            if parsed.scheme == "https":
                ssl_ctx = _ssl.create_default_context()

            session = await self._get_session()
            async with session.post(
                pinned_url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                ssl=ssl_ctx,
                server_hostname=original_hostname if ssl_ctx else None,
            ) as response:
                    # Read at most 2048 bytes to prevent memory exhaustion
                    # from malicious endpoints returning huge responses
                    body_bytes = await response.content.read(2048)
                    body = body_bytes.decode("utf-8", errors="replace")
                    
                    # Success: 2xx status
                    success = 200 <= response.status < 300
                    
                    return WebhookResult(
                        success=success,
                        response_code=response.status,
                        response_body=body[:1000] if body else None,
                        error=None if success else f"HTTP {response.status}"
                    )
                    
        except asyncio.TimeoutError:
            return WebhookResult(
                success=False,
                error="Request timeout"
            )
        except aiohttp.ClientError:
            return WebhookResult(
                success=False,
                error="Client error: connection failed"
            )
        except Exception:
            logger.exception("Unexpected error delivering webhook")
            return WebhookResult(
                success=False,
                error="Unexpected error during webhook delivery"
            )
    
    def _build_event_payload(
        self,
        agent_id: str,
        event_type: str,
        payload: Dict,
    ) -> Dict:
        """Build full webhook payload with unique event_id (uuid4-based)."""
        import uuid as _uuid

        return {
            "event_id": f"evt_{_uuid.uuid4().hex}",
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }

    def queue_event(
        self,
        agent_id: str,
        event_type: str,
        payload: Dict
    ) -> str:
        """
        Queue webhook event for delivery

        Returns:
            Event ID
        """
        full_payload = self._build_event_payload(agent_id, event_type, payload)

        with get_db() as db:
            repo = WebhookRepository(db)
            event = repo.create_event(agent_id, event_type, full_payload)
            return str(event.id)
    
    @staticmethod
    def _matches_event_filter(event_type: str, event_filters: Optional[List[str]]) -> bool:
        """Check if an event type matches the endpoint's filter patterns.

        If event_filters is None or empty, all events match (no filtering).
        Otherwise, each filter is matched via fnmatch glob semantics
        (e.g., "payment.*" matches "payment.received").
        """
        if not event_filters:
            return True
        return any(fnmatch(event_type, pattern) for pattern in event_filters)

    _MAX_ENDPOINT_FAILURES = 10

    async def process_event(self, event_id: str) -> WebhookResult:
        """Process single webhook event with fan-out to registered endpoints.

        Split into 3 phases so the DB session is NOT held during the HTTP call:
        1. Read event + agent config + registered endpoints (short-lived session)
        2. HTTP calls to all matching endpoints concurrently (no DB session)
        3. Write event result + update per-endpoint failure counters (short-lived session)

        The FOR UPDATE lock in Phase 1 prevents two concurrent workers from both
        reading the same event as 'pending' and both attempting delivery (TOCTOU).
        The Phase 3 status re-check is a secondary safety net: if the lock was
        unavailable (e.g. SQLite in tests) and another worker slipped through,
        we skip writing rather than double-marking delivered or overwriting a retry.
        """
        import uuid as _uuid

        # Normalize event_id to UUID for SQLAlchemy UUID columns
        if isinstance(event_id, str):
            try:
                event_id = _uuid.UUID(event_id)
            except ValueError:
                return WebhookResult(success=False, error="Invalid event_id format")

        # Phase 1: Read event, agent config, and registered endpoints
        with get_db() as db:
            webhook_repo = WebhookRepository(db)
            agent_repo = AgentRepository(db)
            endpoint_repo = WebhookEndpointRepository(db)

            event = webhook_repo.get_by_id_for_update(event_id)

            if not event:
                return WebhookResult(success=False, error="Event not found")

            # Get agent webhook config
            agent = agent_repo.get_by_id(event.agent_id)
            if not agent:
                webhook_repo.mark_delivered(event_id, 0, "Agent not found")
                return WebhookResult(success=True)

            # Capture legacy single-URL config (backward compat)
            legacy_url = agent.webhook_url
            legacy_secret = (
                agent_repo.get_webhook_secret(agent.id) if legacy_url else None
            )

            # Gather registered endpoints: active, under failure threshold, matching event type
            registered_endpoints = endpoint_repo.list_by_agent(agent.id)
            delivery_targets: List[Dict] = []

            for ep in registered_endpoints:
                if not ep.is_active:
                    continue
                if ep.failure_count >= self._MAX_ENDPOINT_FAILURES:
                    continue
                if not self._matches_event_filter(event.event_type, ep.event_filters):
                    continue
                try:
                    ep_secret = decrypt_value(ep.secret_encrypted)
                except Exception:
                    logger.error(
                        "Failed to decrypt secret for endpoint %s (agent %s), skipping",
                        ep.id, agent.id,
                    )
                    continue
                delivery_targets.append({
                    "endpoint_id": ep.id,
                    "url": ep.url,
                    "secret": ep_secret,
                    "is_legacy": False,
                })

            # Add legacy URL if present (only when it is not already covered by a registered endpoint)
            registered_urls = {t["url"] for t in delivery_targets}
            if legacy_url and legacy_url not in registered_urls:
                delivery_targets.append({
                    "endpoint_id": None,
                    "url": legacy_url,
                    "secret": legacy_secret,
                    "is_legacy": True,
                })

            # If nothing to deliver to, mark as delivered
            if not delivery_targets:
                webhook_repo.mark_delivered(event_id, 0, "No webhook targets configured")
                return WebhookResult(success=True)

            event_payload = event.payload
            event_type = event.event_type

        # Phase 2: Deliver to all targets concurrently (no DB session held)
        semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_WEBHOOKS)

        async def _deliver_one(target: Dict) -> Dict:
            async with semaphore:
                result = await self._send_webhook(
                    url=target["url"],
                    payload=event_payload,
                    secret=target["secret"],
                )
                return {
                    "endpoint_id": target["endpoint_id"],
                    "url": target["url"],
                    "is_legacy": target["is_legacy"],
                    "result": result,
                }

        delivery_outcomes = await asyncio.gather(
            *[_deliver_one(t) for t in delivery_targets],
            return_exceptions=True,
        )

        # Separate successes and failures
        any_success = False
        all_failed = True
        endpoint_updates: List[Dict] = []

        for outcome in delivery_outcomes:
            if isinstance(outcome, Exception):
                logger.error("Unexpected error in fan-out delivery: %s", outcome)
                continue
            result = outcome["result"]
            if result.success:
                any_success = True
                all_failed = False
            if outcome["endpoint_id"] is not None:
                endpoint_updates.append({
                    "endpoint_id": outcome["endpoint_id"],
                    "success": result.success,
                })

        # Phase 3: Write results (short-lived session)
        with get_db() as db:
            webhook_repo = WebhookRepository(db)
            current_event = webhook_repo.get_by_id(event_id)

            if current_event is None:
                logger.warning(
                    "process_event: event %s disappeared before Phase 3 write",
                    event_id,
                )
                return WebhookResult(success=any_success)

            active_statuses = {WebhookStatus.PENDING, WebhookStatus.RETRYING}
            if current_event.status not in active_statuses:
                logger.info(
                    "process_event: skipping Phase 3 write for event %s "
                    "(status is already '%s')",
                    event_id,
                    current_event.status,
                )
                return WebhookResult(success=any_success)

            # Mark event based on aggregate result
            if any_success:
                webhook_repo.mark_delivered(event_id, 200, "Fan-out delivery")
            else:
                first_error = "All endpoints failed"
                for outcome in delivery_outcomes:
                    if not isinstance(outcome, Exception) and outcome["result"].error:
                        first_error = outcome["result"].error
                        break
                webhook_repo.schedule_retry(event_id, first_error)

            # Update per-endpoint failure counters
            endpoint_repo = WebhookEndpointRepository(db)
            now = datetime.now(timezone.utc)
            for update in endpoint_updates:
                ep = endpoint_repo.get_by_id(
                    update["endpoint_id"],
                    agent_id=current_event.agent_id,
                )
                if ep is None:
                    continue
                if update["success"]:
                    ep.failure_count = 0
                else:
                    ep.failure_count = ep.failure_count + 1
                    if ep.failure_count >= self._MAX_ENDPOINT_FAILURES:
                        ep.is_active = False
                        ep.disabled_at = now
                        logger.warning(
                            "Endpoint %s (url=%s) disabled after %d consecutive failures",
                            ep.id, ep.url, ep.failure_count,
                        )

        return WebhookResult(success=any_success)
    
    _MAX_CONCURRENT_WEBHOOKS = 10

    async def process_pending_events(self, batch_size: int = 100) -> Dict:
        """Process pending webhook events concurrently (up to 10 at a time)."""
        with get_db() as db:
            repo = WebhookRepository(db)
            pending = repo.get_pending_events(limit=batch_size)

        if not pending:
            return {"processed": 0, "successful": 0, "failed": 0}

        semaphore = asyncio.Semaphore(self._MAX_CONCURRENT_WEBHOOKS)

        async def _process_one(event_id: str) -> bool:
            async with semaphore:
                result = await self.process_event(event_id)
                return result.success

        tasks = [_process_one(str(event.id)) for event in pending]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        successful = sum(1 for r in results if r is True)
        failed = len(results) - successful

        return {
            "processed": len(results),
            "successful": successful,
            "failed": failed,
        }
    
    async def start_worker(self, interval_seconds: int = 10):
        """Start background webhook worker"""
        self._running = True
        
        while self._running:
            try:
                await self.process_pending_events()
            except Exception:
                logger.error("Webhook worker error", exc_info=True)
            
            await asyncio.sleep(interval_seconds)
    
    def stop_worker(self):
        """Stop webhook worker"""
        self._running = False
    
    def get_delivery_stats(self, days: int = 7) -> Dict:
        """Get webhook delivery statistics"""
        from sqlalchemy import func
        
        since = datetime.now(timezone.utc) - timedelta(days=days)
        
        with get_db() as db:
            total = db.query(WebhookEvent).filter(
                WebhookEvent.created_at >= since
            ).count()
            
            delivered = db.query(WebhookEvent).filter(
                WebhookEvent.status == WebhookStatus.DELIVERED,
                WebhookEvent.created_at >= since
            ).count()

            failed = db.query(WebhookEvent).filter(
                WebhookEvent.status == WebhookStatus.FAILED,
                WebhookEvent.created_at >= since
            ).count()

            pending = db.query(WebhookEvent).filter(
                WebhookEvent.status.in_([WebhookStatus.PENDING, WebhookStatus.RETRYING]),
                WebhookEvent.created_at >= since
            ).count()
            
            avg_attempts = db.query(func.avg(WebhookEvent.attempt_count)).filter(
                WebhookEvent.created_at >= since
            ).scalar() or 0
            
            return {
                "period_days": days,
                "total_events": total,
                "delivered": delivered,
                "failed": failed,
                "pending": pending,
                "success_rate": delivered / total if total > 0 else 0,
                "average_attempts": round(float(avg_attempts), 2)
            }


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

_service: Optional[WebhookService] = None
_service_lock = threading.Lock()


def get_webhook_service() -> WebhookService:
    """Get global webhook service"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = WebhookService()
    return _service


def queue_webhook(agent_id: str, event_type: str, payload: Dict) -> str:
    """Queue webhook event (convenience function)"""
    service = get_webhook_service()
    return service.queue_event(agent_id, event_type, payload)


# Common event types
EVENT_PAYMENT_RECEIVED = "payment.received"
EVENT_PAYMENT_SENT = "payment.sent"
EVENT_ESCROW_CREATED = "escrow.created"
EVENT_ESCROW_ACCEPTED = "escrow.accepted"
EVENT_ESCROW_DELIVERED = "escrow.delivered"
EVENT_ESCROW_COMPLETED = "escrow.completed"
EVENT_ESCROW_EXPIRED = "escrow.expired"
EVENT_ESCROW_CANCELLED = "escrow.cancelled"
EVENT_CHANNEL_OPENED = "channel.opened"
EVENT_CHANNEL_PAYMENT = "channel.payment"
EVENT_CHANNEL_CLOSED = "channel.closed"
EVENT_AGENT_VERIFIED = "agent.verified"
