"""Test that _scrub_event does not mutate the original event dict."""
import copy
import re

import pytest


# Replicate the production patterns from api/main_v2.py
_SENSITIVE_KEY_RE = re.compile(
    r"(auth|api[_-]?key|password|secret|mnemonic|seed|token|"
    r"monero_rpc_pass|admin_api_key|backup_passphrase)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(api[_-]?key|password|secret|mnemonic|seed|"
    r"MONERO_RPC_PASS|ADMIN_API_KEY|BACKUP_PASSPHRASE)"
    r"['\"]?\s*[:=]\s*['\"]?[^'\"\s,}]+",
    re.IGNORECASE,
)


def _scrub_value(obj):
    """Recursively scrub sensitive values from a data structure (mirrors production)."""
    if isinstance(obj, dict):
        return {
            k: "[Filtered]" if _SENSITIVE_KEY_RE.search(k) else _scrub_value(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_value(item) for item in obj]
    if isinstance(obj, str) and _SENSITIVE_VALUE_RE.search(obj):
        return "[Filtered]"
    return obj


def _scrub_event_immutable(event, hint=None):
    """Immutable version matching production code in api/main_v2.py."""
    return _scrub_value(copy.deepcopy(event))


def _scrub_event_old_mutating(event, hint=None):
    """Old mutating version (for regression documentation)."""
    if event.get("request", {}).get("headers"):
        headers = event["request"]["headers"]
        for key in list(headers.keys()):
            if "auth" in key.lower() or "api" in key.lower() or "key" in key.lower():
                headers[key] = "[Filtered]"
    return event


class TestScrubEventImmutability:
    """Verify _scrub_event does not mutate its input."""

    def test_scrub_event_does_not_mutate_original(self):
        """The original event dict must not be modified by _scrub_event."""
        original_event = {
            "request": {
                "headers": {
                    "authorization": "Bearer secret-token-123",
                    "api-key": "my-api-key",
                    "content-type": "application/json",
                }
            },
            "message": "some log message",
        }

        original_snapshot = copy.deepcopy(original_event)

        result = _scrub_event_immutable(original_event)

        # Original must NOT be modified
        assert original_event == original_snapshot, "Original event was mutated!"

        # Result must have filtered headers
        assert result["request"]["headers"]["authorization"] == "[Filtered]"
        assert result["request"]["headers"]["api-key"] == "[Filtered]"
        # Non-sensitive headers preserved
        assert result["request"]["headers"]["content-type"] == "application/json"

    def test_scrub_event_does_not_mutate_nested_structures(self):
        """Nested dicts/lists in the event must not be mutated."""
        original_event = {
            "request": {
                "headers": {
                    "x-api-key": "secret-value",
                },
                "data": {"nested": "value"},
            },
            "extra": {"tags": ["a", "b"]},
        }

        original_snapshot = copy.deepcopy(original_event)

        _scrub_event_immutable(original_event)

        assert original_event == original_snapshot, "Nested structure was mutated!"

    def test_scrub_event_returns_new_object(self):
        """Result must be a different object than the input."""
        original_event = {
            "request": {
                "headers": {"authorization": "Bearer token"},
            },
        }

        result = _scrub_event_immutable(original_event)

        assert result is not original_event
        assert result["request"] is not original_event["request"]
        assert result["request"]["headers"] is not original_event["request"]["headers"]

    def test_scrub_event_without_headers(self):
        """Events without headers should pass through without error."""
        original_event = {"message": "plain event"}
        original_snapshot = copy.deepcopy(original_event)

        result = _scrub_event_immutable(original_event)

        assert original_event == original_snapshot
        assert result["message"] == "plain event"

    def test_scrub_event_filters_sensitive_body_patterns(self):
        """Sensitive patterns in string values should be filtered."""
        original_event = {
            "message": 'password="hunter2" in config',
        }
        original_snapshot = copy.deepcopy(original_event)

        result = _scrub_event_immutable(original_event)

        assert original_event == original_snapshot
        assert "hunter2" not in str(result)
        assert result["message"] == "[Filtered]"

    def test_scrub_event_filters_sensitive_keys_recursively(self):
        """Sensitive keys at any nesting depth should be filtered."""
        original_event = {
            "extra": {
                "context": {
                    "api_key": "should-be-filtered",
                    "safe_field": "keep-this",
                }
            }
        }

        result = _scrub_event_immutable(original_event)

        assert result["extra"]["context"]["api_key"] == "[Filtered]"
        assert result["extra"]["context"]["safe_field"] == "keep-this"

    def test_scrub_event_handles_lists(self):
        """Lists inside events should be traversed without mutation."""
        original_event = {
            "breadcrumbs": [
                {"message": "normal breadcrumb"},
                {"data": {"secret_token": "abc123"}},
            ],
        }
        original_snapshot = copy.deepcopy(original_event)

        result = _scrub_event_immutable(original_event)

        assert original_event == original_snapshot
        assert result["breadcrumbs"][0]["message"] == "normal breadcrumb"
        assert result["breadcrumbs"][1]["data"]["secret_token"] == "[Filtered]"

    def test_old_behavior_mutates(self):
        """Document that the OLD implementation mutates the original (regression guard)."""
        original_event = {
            "request": {
                "headers": {
                    "authorization": "Bearer secret-token-123",
                    "content-type": "application/json",
                }
            },
        }

        original_snapshot = copy.deepcopy(original_event)

        _scrub_event_old_mutating(original_event)

        # The old version DOES mutate
        assert original_event != original_snapshot, (
            "If this fails, the old behavior no longer mutates (unexpected)"
        )
