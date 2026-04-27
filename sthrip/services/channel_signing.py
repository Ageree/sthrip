"""Ed25519 channel state signing.

Provides deterministic signing and verification of payment channel state
using the Ed25519 algorithm via PyNaCl.

Canonical state message format: ``channel_id:nonce:balance_a:balance_b``
All keys and signatures are returned as base64-encoded strings.
"""

import base64
import logging
from typing import Tuple

from nacl.exceptions import BadSignatureError, CryptoError
from nacl.signing import SigningKey, VerifyKey

logger = logging.getLogger("sthrip.channel_signing")


def generate_channel_keypair() -> Tuple[str, str]:
    """Generate a new Ed25519 keypair for channel state signing.

    Returns:
        A tuple of (public_key_b64, private_key_b64) where both values are
        base64-encoded byte strings.
    """
    signing_key = SigningKey.generate()
    verify_key = signing_key.verify_key
    public_key_b64 = base64.b64encode(verify_key.encode()).decode()
    private_key_b64 = base64.b64encode(signing_key.encode()).decode()
    return (public_key_b64, private_key_b64)


def sign_channel_state(
    private_key_b64: str,
    channel_id: str,
    nonce: int,
    balance_a: str,
    balance_b: str,
) -> str:
    """Sign a channel state with the given private key.

    The message is the canonical UTF-8 encoding of
    ``channel_id:nonce:balance_a:balance_b``.

    Args:
        private_key_b64: Base64-encoded Ed25519 private (signing) key.
        channel_id: Unique identifier for the payment channel.
        nonce: Monotonically increasing state counter preventing replays.
        balance_a: Balance of party A as a decimal string.
        balance_b: Balance of party B as a decimal string.

    Returns:
        Base64-encoded Ed25519 signature.
    """
    key_bytes = base64.b64decode(private_key_b64)
    signing_key = SigningKey(key_bytes)
    message = _build_message(channel_id, nonce, balance_a, balance_b)
    signed = signing_key.sign(message)
    return base64.b64encode(signed.signature).decode()


def verify_channel_state(
    public_key_b64: str,
    signature_b64: str,
    channel_id: str,
    nonce: int,
    balance_a: str,
    balance_b: str,
) -> bool:
    """Verify an Ed25519 signature against the given channel state.

    Args:
        public_key_b64: Base64-encoded Ed25519 public (verify) key.
        signature_b64: Base64-encoded signature to verify.
        channel_id: Unique identifier for the payment channel.
        nonce: State counter that was signed.
        balance_a: Balance of party A as a decimal string.
        balance_b: Balance of party B as a decimal string.

    Returns:
        True if the signature is valid for the given state and key, False
        otherwise.  Never raises.
    """
    try:
        key_bytes = base64.b64decode(public_key_b64)
        verify_key = VerifyKey(key_bytes)
        sig_bytes = base64.b64decode(signature_b64)
        message = _build_message(channel_id, nonce, balance_a, balance_b)
        verify_key.verify(message, sig_bytes)
        return True
    except BadSignatureError:
        # Expected reject path; logged at DEBUG to avoid alarm-fatigue.
        logger.debug("Channel signature rejected: BadSignatureError")
        return False
    except CryptoError as e:
        # Catches all PyNaCl C-level failures. BadSignatureError is a subclass
        # of CryptoError, so the BadSignatureError branch above takes priority.
        # Without this branch a corrupt key would propagate as an unhandled 500.
        logger.warning("Channel signature crypto error: %s", type(e).__name__)
        return False
    except (ValueError, TypeError) as e:
        # Note: binascii.Error is a subclass of ValueError in Python 3, so this
        # also catches base64 decode failures.
        logger.warning("Channel signature input rejected: %s", type(e).__name__)
        return False


def _build_message(
    channel_id: str,
    nonce: int,
    balance_a: str,
    balance_b: str,
) -> bytes:
    """Build the canonical message bytes for signing/verification.

    Format: ``channel_id:nonce:balance_a:balance_b`` encoded as UTF-8.
    """
    return f"{channel_id}:{nonce}:{balance_a}:{balance_b}".encode()
