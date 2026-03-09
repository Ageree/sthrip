"""Tests for structured logging configuration"""
import json
import logging
import pytest
from unittest.mock import patch, MagicMock

from sthrip.logging_config import (
    JSONFormatter,
    setup_logging,
    generate_request_id,
    request_id_var,
    agent_id_var,
)


# ─────────────────────────────────────────────────────────────────────────────
# JSONFormatter (lines 24-34)
# ─────────────────────────────────────────────────────────────────────────────


class TestJSONFormatter:
    def test_formats_basic_record(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="sthrip.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello world",
            args=None,
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "sthrip.test"
        assert parsed["message"] == "hello world"
        assert "timestamp" in parsed
        assert parsed["timestamp"].endswith("Z")
        assert "request_id" in parsed
        assert "agent_id" in parsed

    def test_includes_context_vars(self):
        formatter = JSONFormatter()
        token_req = request_id_var.set("req_abc123")
        token_agent = agent_id_var.set("agent_xyz")
        try:
            record = logging.LogRecord(
                name="test", level=logging.WARNING, pathname="t.py",
                lineno=1, msg="ctx test", args=None, exc_info=None,
            )
            output = formatter.format(record)
            parsed = json.loads(output)
            assert parsed["request_id"] == "req_abc123"
            assert parsed["agent_id"] == "agent_xyz"
        finally:
            request_id_var.reset(token_req)
            agent_id_var.reset(token_agent)

    def test_includes_exception_info(self):
        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="t.py",
            lineno=1, msg="error occurred", args=None, exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]
        assert "test error" in parsed["exception"]

    def test_no_exception_when_exc_info_none(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="t.py",
            lineno=1, msg="no error", args=None, exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exception" not in parsed


# ─────────────────────────────────────────────────────────────────────────────
# setup_logging (lines 37-69)
# ─────────────────────────────────────────────────────────────────────────────


class TestSetupLogging:
    def test_json_format(self):
        with patch.dict("os.environ", {"LOG_FORMAT": "json", "LOG_LEVEL": "DEBUG"}):
            setup_logging()

        root = logging.getLogger()
        assert root.level == logging.DEBUG
        # Should have exactly one handler with JSONFormatter
        json_handlers = [
            h for h in root.handlers if isinstance(h.formatter, JSONFormatter)
        ]
        assert len(json_handlers) >= 1

    def test_text_format(self):
        with patch.dict("os.environ", {"LOG_FORMAT": "text", "LOG_LEVEL": "WARNING"}):
            setup_logging()

        root = logging.getLogger()
        assert root.level == logging.WARNING
        # Should NOT have JSONFormatter
        json_handlers = [
            h for h in root.handlers if isinstance(h.formatter, JSONFormatter)
        ]
        assert len(json_handlers) == 0

    def test_default_format_is_text(self):
        with patch.dict("os.environ", {}, clear=True):
            setup_logging()

        root = logging.getLogger()
        assert root.level == logging.INFO
        json_handlers = [
            h for h in root.handlers if isinstance(h.formatter, JSONFormatter)
        ]
        assert len(json_handlers) == 0

    def test_betterstack_handler_added_when_token_set(self):
        mock_handler = MagicMock()
        mock_logtail_cls = MagicMock(return_value=mock_handler)

        with patch.dict("os.environ", {"BETTERSTACK_SOURCE_TOKEN": "test_token", "LOG_FORMAT": "text"}):
            with patch.dict("sys.modules", {"logtail": MagicMock(LogtailHandler=mock_logtail_cls)}):
                setup_logging()

        mock_logtail_cls.assert_called_once_with(source_token="test_token")

    def test_betterstack_import_error_handled(self):
        with patch.dict("os.environ", {"BETTERSTACK_SOURCE_TOKEN": "test_token", "LOG_FORMAT": "text"}):
            with patch("builtins.__import__", side_effect=_import_raiser("logtail")):
                # Should not raise
                setup_logging()


# ─────────────────────────────────────────────────────────────────────────────
# generate_request_id
# ─────────────────────────────────────────────────────────────────────────────


class TestGenerateRequestId:
    def test_returns_hex_string(self):
        rid = generate_request_id()
        assert len(rid) == 16
        int(rid, 16)  # Should not raise if valid hex

    def test_unique(self):
        ids = {generate_request_id() for _ in range(100)}
        assert len(ids) == 100


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

import builtins

_original_import = builtins.__import__


def _import_raiser(blocked_module):
    """Return an __import__ replacement that raises ImportError for blocked_module."""
    def _import(name, *args, **kwargs):
        if name == blocked_module:
            raise ImportError(f"No module named '{blocked_module}'")
        return _original_import(name, *args, **kwargs)
    return _import
