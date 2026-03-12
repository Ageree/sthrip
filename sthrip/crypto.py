"""Symmetric encryption for secrets at rest."""
import threading

from cryptography.fernet import Fernet

from sthrip.config import get_settings

_fernet_instance = None
_fernet_lock = threading.Lock()


def _get_fernet() -> Fernet:
    """Get or create Fernet instance from config key (thread-safe, double-checked locking)."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance

    with _fernet_lock:
        if _fernet_instance is not None:
            return _fernet_instance

        key = get_settings().webhook_encryption_key
        if not key:
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
