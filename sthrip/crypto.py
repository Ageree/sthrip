"""Symmetric encryption for secrets at rest."""
import threading

from cryptography.fernet import Fernet

from sthrip.config import get_settings

_fernet_instance = None
_fernet_lock = threading.Lock()


def _get_fernet() -> Fernet:
    """Get or create Fernet instance from config key (thread-safe, double-checked locking).

    NOTE: The Fernet key is cached for the lifetime of the process.
    Key rotation for WEBHOOK_ENCRYPTION_KEY requires a process restart.
    See get_settings() docstring in sthrip/config.py for rationale.
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    with _fernet_lock:
        if _fernet_instance is not None:
            return _fernet_instance

        settings = get_settings()
        key = settings.webhook_encryption_key
        if not key:
            if settings.environment == "dev":
                # Dev-only fallback: deterministic key so dev doesn't need env var
                import base64
                key = base64.urlsafe_b64encode(b"dev-only-webhook-key-32bytes!!" + b"\x00\x00").decode()
            else:
                raise RuntimeError(
                    "WEBHOOK_ENCRYPTION_KEY not configured. "
                    "Set it or run in dev/staging environment."
                )

        _fernet_instance = Fernet(key.encode() if isinstance(key, str) else key)
        return _fernet_instance


def encrypt_value(plaintext: str) -> str:
    """Encrypt a string value. Returns base64-encoded ciphertext."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext back to plaintext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()
