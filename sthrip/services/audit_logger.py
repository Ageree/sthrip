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

_SENSITIVE_KEYS = frozenset({
    "api_key", "password", "secret", "mnemonic", "seed",
    "webhook_secret", "admin_key", "token", "credentials",
})


def _sanitize(data: Optional[dict]) -> Optional[dict]:
    """Recursively redact sensitive keys in a details dict."""
    if data is None:
        return None
    result = {}
    for k, v in data.items():
        if k.lower() in _SENSITIVE_KEYS:
            result[k] = "***"
        elif isinstance(v, dict):
            result[k] = _sanitize(v)
        elif isinstance(v, list):
            result[k] = [_sanitize(item) if isinstance(item, dict) else item for item in v]
        else:
            result[k] = v
    return result


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
    db: Optional[Any] = None,
) -> None:
    """
    Log an audit event to the database.

    Actions:
        agent.registered, agent.verified, payment.hub_routing,
        balance.deposit, balance.withdraw, admin.stats_viewed, auth.failed

    Args:
        db: Optional SQLAlchemy session. When provided, the audit entry is
            written to the caller's existing transaction so the log and the
            triggering operation commit (or roll back) atomically. When
            omitted, a new session is opened internally (backward-compatible
            behaviour).
    """
    try:
        entry = AuditLog(
            agent_id=agent_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=ip_address,
            request_method=request_method,
            request_path=request_path,
            request_body=_sanitize(details),
            success=success,
            error_message=error_message,
        )
        if db is not None:
            db.add(entry)
        else:
            with get_db() as session:
                session.add(entry)
    except Exception:
        # Audit logging must never break the main request
        logger.warning("Failed to write audit log for action=%s", action, exc_info=True)
