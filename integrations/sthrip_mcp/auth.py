"""3-tier authentication: env var → credentials file → unauthenticated."""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


CREDENTIALS_DIR = Path.home() / ".sthrip"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"
ENV_VAR_NAME = "STHRIP_API_KEY"


class AuthError(Exception):
    """Raised when authentication is required but no API key is available."""

    def __init__(self) -> None:
        super().__init__(
            "No API key found. Set STHRIP_API_KEY env var, "
            "run 'register_agent' tool, or place key in "
            f"{CREDENTIALS_FILE}"
        )


def load_api_key() -> Optional[str]:
    """Load API key with 3-tier fallback.

    Priority:
    1. STHRIP_API_KEY environment variable
    2. ~/.sthrip/credentials.json
    3. None (discovery tools still work)
    """
    env_key = os.environ.get(ENV_VAR_NAME)
    if env_key:
        return env_key

    if CREDENTIALS_FILE.exists():
        try:
            data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            file_key = data.get("api_key")
            if file_key:
                return file_key
        except (json.JSONDecodeError, OSError):
            pass

    return None


def save_api_key(api_key: str) -> Path:
    """Save API key to ~/.sthrip/credentials.json (read-merge-write).

    Preserves existing fields (agent_name, base_url) written by CLI or other tools.
    Returns the path where credentials were saved.
    """
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)

    existing = {}
    try:
        existing = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    existing["api_key"] = api_key
    CREDENTIALS_FILE.write_text(
        json.dumps(existing, indent=2),
        encoding="utf-8",
    )

    # Restrict permissions (owner-only read/write)
    try:
        CREDENTIALS_FILE.chmod(0o600)
    except OSError:
        if sys.platform != "win32":
            logger.warning("Could not restrict permissions on %s", CREDENTIALS_FILE)

    return CREDENTIALS_FILE


def require_auth(api_key: Optional[str]) -> str:
    """Validate that an API key exists, raise AuthError if not."""
    if not api_key:
        raise AuthError()
    return api_key
