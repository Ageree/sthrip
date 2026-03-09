"""
Reliable webhook delivery service
Retries with exponential backoff
"""

import json
import hashlib
import hmac
import asyncio
import logging
import aiohttp
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("sthrip.webhook")
from typing import Dict, Optional, List
from dataclasses import dataclass

from ..db.database import get_db
from ..db.repository import WebhookRepository
from ..db.models import WebhookEvent, Agent
from .url_validator import validate_url_target, SSRFBlockedError


@dataclass
class WebhookResult:
    """Webhook delivery result"""
    success: bool
    response_code: Optional[int] = None
    response_body: Optional[str] = None
    error: Optional[str] = None


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

    async def _get_session(self) -> aiohttp.ClientSession:
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
        """Send single webhook request"""
        # SSRF re-validation: check resolved IP right before sending
        try:
            validate_url_target(url)
        except (SSRFBlockedError, ValueError) as e:
            return WebhookResult(
                success=False,
                error=f"SSRF blocked: {e}"
            )

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Sthrip-Webhook/1.0"
        }
        
        # Add timestamp and signature
        import time as _time
        timestamp = str(int(_time.time()))
        headers["X-Sthrip-Timestamp"] = timestamp
        if secret:
            headers["X-Sthrip-Signature"] = self._sign_payload(payload, secret, timestamp)
        headers["X-Sthrip-Event-ID"] = payload.get("event_id", "unknown")
        
        try:
            session = await self._get_session()
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                    body = await response.text()
                    
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
        except aiohttp.ClientError as e:
            return WebhookResult(
                success=False,
                error=f"Client error: {str(e)}"
            )
        except Exception as e:
            return WebhookResult(
                success=False,
                error=f"Unexpected error: {str(e)}"
            )
    
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
        # Add metadata
        full_payload = {
            "event_id": f"evt_{hashlib.sha256(f'{agent_id}:{event_type}:{datetime.now(timezone.utc).isoformat()}'.encode()).hexdigest()[:16]}",
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload
        }
        
        with get_db() as db:
            repo = WebhookRepository(db)
            event = repo.create_event(agent_id, event_type, full_payload)
            return str(event.id)
    
    async def process_event(self, event_id: str) -> WebhookResult:
        """Process single webhook event"""
        with get_db() as db:
            event = db.query(WebhookEvent).filter(
                WebhookEvent.id == event_id
            ).first()
            
            if not event:
                return WebhookResult(success=False, error="Event not found")
            
            # Get agent webhook config
            agent = db.query(Agent).filter(Agent.id == event.agent_id).first()
            
            if not agent or not agent.webhook_url:
                # No webhook configured, mark as delivered
                repo = WebhookRepository(db)
                repo.mark_delivered(event_id, 0, "No webhook URL configured")
                return WebhookResult(success=True)
            
            # Send webhook
            result = await self._send_webhook(
                url=agent.webhook_url,
                payload=event.payload,
                secret=agent.webhook_secret
            )
            
            # Update event status
            repo = WebhookRepository(db)
            
            if result.success:
                repo.mark_delivered(
                    event_id,
                    result.response_code or 200,
                    result.response_body or ""
                )
            else:
                repo.schedule_retry(event_id, result.error or "Unknown error")
            
            return result
    
    async def process_pending_events(self, batch_size: int = 100) -> Dict:
        """Process all pending webhook events"""
        with get_db() as db:
            repo = WebhookRepository(db)
            pending = repo.get_pending_events(limit=batch_size)
        
        processed = 0
        successful = 0
        failed = 0
        
        for event in pending:
            result = await self.process_event(str(event.id))
            processed += 1
            
            if result.success:
                successful += 1
            else:
                failed += 1
        
        return {
            "processed": processed,
            "successful": successful,
            "failed": failed
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
                WebhookEvent.status == "delivered",
                WebhookEvent.created_at >= since
            ).count()
            
            failed = db.query(WebhookEvent).filter(
                WebhookEvent.status == "failed",
                WebhookEvent.created_at >= since
            ).count()
            
            pending = db.query(WebhookEvent).filter(
                WebhookEvent.status.in_(["pending", "retrying"]),
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


def get_webhook_service() -> WebhookService:
    """Get global webhook service"""
    global _service
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
EVENT_ESCROW_FUNDED = "escrow.funded"
EVENT_ESCROW_RELEASED = "escrow.released"
EVENT_ESCROW_DISPUTED = "escrow.disputed"
EVENT_CHANNEL_OPENED = "channel.opened"
EVENT_CHANNEL_PAYMENT = "channel.payment"
EVENT_CHANNEL_CLOSED = "channel.closed"
EVENT_AGENT_VERIFIED = "agent.verified"
