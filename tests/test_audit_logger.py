from unittest.mock import MagicMock, patch


def test_audit_log_uses_provided_session():
    """I5: When db session is provided, audit log writes to same transaction."""
    from sthrip.services.audit_logger import log_event
    mock_db = MagicMock()
    log_event("test.action", db=mock_db)
    mock_db.add.assert_called_once()


def test_audit_log_creates_own_session_when_none():
    """I5: Without db param, audit log still works (backward compat)."""
    from sthrip.services.audit_logger import log_event
    with patch("sthrip.services.audit_logger.get_db") as mock_get_db:
        mock_session = MagicMock()
        mock_get_db.return_value.__enter__ = MagicMock(return_value=mock_session)
        mock_get_db.return_value.__exit__ = MagicMock(return_value=False)
        log_event("test.action")
        mock_session.add.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# CRIT-5: Audit log sanitization
# ═══════════════════════════════════════════════════════════════════════════════


def test_sanitize_redacts_sensitive_top_level_keys():
    """CRIT-5: Sensitive keys in details dict are redacted to '***'."""
    from sthrip.services.audit_logger import _sanitize

    data = {
        "api_key": "sk-secret-123",
        "password": "hunter2",
        "secret": "my-secret",
        "mnemonic": "word1 word2 word3",
        "seed": "0xdeadbeef",
        "webhook_secret": "whsec_abc",
        "admin_key": "admin-key-value",
        "token": "tok_xyz",
        "credentials": "cred-data",
        "agent_name": "test-agent",
    }
    result = _sanitize(data)

    # Sensitive keys redacted
    for key in ["api_key", "password", "secret", "mnemonic", "seed",
                "webhook_secret", "admin_key", "token", "credentials"]:
        assert result[key] == "***", f"Expected {key} to be redacted"

    # Non-sensitive keys preserved
    assert result["agent_name"] == "test-agent"


def test_sanitize_is_case_insensitive():
    """CRIT-5: Key matching is case-insensitive."""
    from sthrip.services.audit_logger import _sanitize

    data = {"API_KEY": "secret", "Password": "secret", "TOKEN": "secret"}
    result = _sanitize(data)
    for key in data:
        assert result[key] == "***", f"Expected {key} to be redacted (case-insensitive)"


def test_sanitize_none_returns_none():
    """CRIT-5: Passing None returns None."""
    from sthrip.services.audit_logger import _sanitize
    assert _sanitize(None) is None


def test_sanitize_recurses_into_nested_dicts():
    """Sanitize recursively redacts sensitive keys in nested dicts."""
    from sthrip.services.audit_logger import _sanitize

    data = {
        "nested": {"api_key": "should-redact", "password": "should-redact"},
        "api_key": "should-redact",
    }
    result = _sanitize(data)
    assert result["api_key"] == "***"
    assert result["nested"]["api_key"] == "***"
    assert result["nested"]["password"] == "***"


def test_sanitize_empty_dict():
    """CRIT-5: Empty dict returns empty dict."""
    from sthrip.services.audit_logger import _sanitize
    assert _sanitize({}) == {}


def test_log_event_sanitizes_details():
    """CRIT-5: log_event applies sanitization to details before storing."""
    from sthrip.services.audit_logger import log_event
    mock_db = MagicMock()
    log_event(
        "test.action",
        details={"api_key": "secret-value", "action": "test"},
        db=mock_db,
    )
    mock_db.add.assert_called_once()
    entry = mock_db.add.call_args[0][0]
    assert entry.request_body["api_key"] == "***"
    assert entry.request_body["action"] == "test"
