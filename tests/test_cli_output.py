import json
import pytest
from cli.agent_cli.output import (
    EXIT_SUCCESS, EXIT_API_ERROR, EXIT_AUTH_ERROR,
    EXIT_NETWORK_ERROR, EXIT_VALIDATION_ERROR,
    format_success, format_error,
)


def test_exit_code_values():
    assert EXIT_SUCCESS == 0
    assert EXIT_API_ERROR == 1
    assert EXIT_AUTH_ERROR == 2
    assert EXIT_NETWORK_ERROR == 3
    assert EXIT_VALIDATION_ERROR == 4


def test_format_success_wraps_data():
    result = format_success({"balance": "12.5"})
    parsed = json.loads(result)
    assert parsed == {"ok": True, "data": {"balance": "12.5"}}


def test_format_success_with_empty_data():
    result = format_success({})
    parsed = json.loads(result)
    assert parsed == {"ok": True, "data": {}}


def test_format_error_wraps_message_and_code():
    result = format_error("Not found", EXIT_API_ERROR)
    parsed = json.loads(result)
    assert parsed == {"ok": False, "error": "Not found", "code": 1}


def test_format_error_defaults_to_api_error():
    result = format_error("Something broke")
    parsed = json.loads(result)
    assert parsed["code"] == 1


def test_format_success_returns_valid_json():
    result = format_success({"key": "value with \"quotes\""})
    json.loads(result)  # should not raise
