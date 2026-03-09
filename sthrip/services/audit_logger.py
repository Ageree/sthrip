"""
Audit logging service.
Writes structured events to the audit_log table.
"""

import logging
from typing import Optional, Any
from uuid import UUID

from sthrip.db.database import get_db
from sthrip.db.models import AuditLog

logger = logging.getLogger("sthrip.audit")


def log_event(
    action: str,
    agent_id: Optional[UUID] = None,
    ip_address: Optional[str] = None,
    request_method: Optional[str] = None,
    request_path: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[UUID] = None,
    details: Optional[dict] = None,
    success: bool = True,
    error_message: Optional[str] = None,
) -> None:
    """
    Log an audit event to the database.

    Actions:
        agent.registered, agent.verified, payment.hub_routing,
        balance.deposit, balance.withdraw, admin.stats_viewed, auth.failed
    """
    try:
        with get_db() as db:
            entry = AuditLog(
                agent_id=agent_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                ip_address=ip_address,
                request_method=request_method,
                request_path=request_path,
                request_body=details,
                success=success,
                error_message=error_message,
            )
            db.add(entry)
    except Exception:
        # Audit logging must never break the main request
        logger.warning("Failed to write audit log for action=%s", action, exc_info=True)
