"""
Structured logging configuration.

LOG_FORMAT=json  -> JSON lines (production / Railway / Datadog)
LOG_FORMAT=text  -> Human-readable (local dev)
"""

import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from sthrip.config import get_settings

# Per-request context
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
agent_id_var: ContextVar[str] = ContextVar("agent_id", default="-")


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get("-"),
            "agent_id": agent_id_var.get("-"),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging() -> None:
    """Configure root logger based on LOG_FORMAT env var."""
    try:
        settings = get_settings()
        log_format = settings.log_format
        log_level = settings.log_level
        betterstack_token = settings.betterstack_token
    except Exception:
        log_format = "text"
        log_level = "INFO"
        betterstack_token = ""

    log_format = log_format.lower()
    log_level = log_level.upper()

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove existing handlers
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )

    root.addHandler(handler)

    # Betterstack Logs (Logtail) — send logs to cloud when token is set
    if betterstack_token:
        try:
            from logtail import LogtailHandler
            logtail_handler = LogtailHandler(source_token=betterstack_token)
            root.addHandler(logtail_handler)
        except ImportError:
            logging.getLogger(__name__).debug(
                "logtail-python not installed, Betterstack logging disabled"
            )


def generate_request_id() -> str:
    return uuid.uuid4().hex[:16]
