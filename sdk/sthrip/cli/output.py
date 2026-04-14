import json

EXIT_SUCCESS = 0
EXIT_API_ERROR = 1
EXIT_AUTH_ERROR = 2
EXIT_NETWORK_ERROR = 3
EXIT_VALIDATION_ERROR = 4


def format_success(data: dict) -> str:
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False)


def format_error(message: str, code: int = EXIT_API_ERROR) -> str:
    return json.dumps(
        {"ok": False, "error": message, "code": code},
        ensure_ascii=False,
    )
