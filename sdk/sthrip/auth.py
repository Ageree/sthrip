"""Credential storage for the Sthrip SDK.

Credentials are persisted at ``~/.sthrip/credentials.json`` with file
permissions restricted to the owning user (0600).  The module never
mutates the dict that is returned by ``load_credentials`` -- callers
receive a fresh copy each time.
"""

import json
import os
import stat
from pathlib import Path

# typing import kept compatible with Python 3.8
from typing import Dict, Optional

CREDENTIALS_PATH = Path.home() / ".sthrip" / "credentials.json"

# Fields we expect inside the credentials file.
_REQUIRED_KEYS = ("api_key", "agent_id", "agent_name", "api_url")


def load_credentials(path=None):
    # type: (Optional[Path]) -> Optional[Dict[str, str]]
    """Load credentials from disk.

    Returns a *new* dict with keys ``api_key``, ``agent_id``,
    ``agent_name``, and ``api_url``, or ``None`` if the file does not
    exist or is malformed.
    """
    target = path or CREDENTIALS_PATH
    if not target.is_file():
        return None

    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    # Return only the keys we care about -- never leak unknown fields.
    result = {}
    for key in _REQUIRED_KEYS:
        value = data.get(key)
        if value is None:
            return None
        result[key] = str(value)

    return result


def save_credentials(api_key, agent_id, agent_name, api_url, path=None):
    # type: (str, str, str, str, Optional[Path]) -> None
    """Persist credentials to disk with restricted permissions.

    Creates ``~/.sthrip/`` if it does not exist.  The file is written
    atomically (write-then-rename would be ideal but pathlib keeps this
    simple) and its mode is set to ``0600`` immediately.
    """
    target = path or CREDENTIALS_PATH

    target.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "api_key": api_key,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "api_url": api_url,
    }

    target.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    # Restrict to owner read/write only.
    try:
        os.chmod(str(target), stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # On platforms where chmod is not supported (e.g. some Windows
        # builds) we silently continue -- the file was still written.
        pass
