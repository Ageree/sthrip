import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_BASE_URL = "https://sthrip-api-production.up.railway.app"
CREDENTIALS_PATH = str(Path.home() / ".sthrip" / "credentials.json")


def load_config() -> Dict[str, Any]:
    try:
        with open(CREDENTIALS_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(updates: Dict[str, Any]) -> None:
    existing = load_config()
    merged = {**existing, **updates}
    path = Path(CREDENTIALS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(merged, f, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        if os.name != "nt":
            raise


def resolve_api_key() -> Optional[str]:
    env_key = os.environ.get("STHRIP_API_KEY")
    if env_key:
        return env_key
    return load_config().get("api_key")


def resolve_base_url(flag_url: Optional[str] = None) -> str:
    if flag_url:
        return flag_url
    env_url = os.environ.get("STHRIP_BASE_URL")
    if env_url:
        return env_url
    file_url = load_config().get("base_url")
    if file_url:
        return file_url
    return DEFAULT_BASE_URL
