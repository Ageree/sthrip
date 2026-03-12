"""Pydantic request/response models for the Sthrip API."""

import re
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from sthrip.services.url_validator import validate_url_target, SSRFBlockedError


class AgentRegistration(BaseModel):
    agent_name: str = Field(..., min_length=3, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$")
    webhook_url: Optional[str] = None
    privacy_level: str = Field(default="medium", pattern=r"^(low|medium|high|paranoid)$")
    xmr_address: Optional[str] = None
    base_address: Optional[str] = None
    solana_address: Optional[str] = None

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            validate_url_target(v)
        except SSRFBlockedError as e:
            raise ValueError(f"webhook_url blocked: {e}")
        return v

    @field_validator("xmr_address")
    @classmethod
    def validate_xmr_addr(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return validate_monero_address(v)


class AgentResponse(BaseModel):
    agent_id: str
    agent_name: str
    tier: str
    api_key: str  # Shown once!
    webhook_secret: str  # Shown once!
    created_at: str


class AgentSettingsUpdate(BaseModel):
    webhook_url: Optional[str] = None
    privacy_level: Optional[str] = Field(default=None, pattern=r"^(low|medium|high|paranoid)$")
    xmr_address: Optional[str] = None
    base_address: Optional[str] = None
    solana_address: Optional[str] = None

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url_setting(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            validate_url_target(v)
        except SSRFBlockedError as e:
            raise ValueError(f"webhook_url blocked: {e}")
        return v

    @field_validator("xmr_address")
    @classmethod
    def validate_xmr_addr(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return validate_monero_address(v)

    @field_validator("base_address", "solana_address")
    @classmethod
    def validate_wallet_address_not_empty(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip() == "":
            raise ValueError("Wallet address must not be empty")
        return v


class AgentProfileResponse(BaseModel):
    agent_name: str
    did: Optional[str]
    tier: str
    trust_score: int
    total_transactions: int
    xmr_address: Optional[str]
    base_address: Optional[str]
    verified_at: Optional[str]


class PaymentRequest(BaseModel):
    to_address: str = Field(..., description="Recipient Monero address")
    amount: Decimal = Field(..., gt=0, description="Amount in XMR")
    memo: Optional[str] = Field(None, max_length=1000)
    privacy_level: Optional[str] = Field(None, pattern=r"^(low|medium|high|paranoid)$")
    use_hub_routing: bool = Field(False, description="Use hub routing for instant confirmation")


class HubPaymentRequest(BaseModel):
    to_agent_name: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$', description="Recipient agent name")
    amount: Decimal = Field(..., gt=0, le=9980, description="Amount in XMR (max 9980 to account for fees)")
    memo: Optional[str] = Field(default=None, max_length=500)
    urgency: str = Field(default="normal", pattern=r"^(normal|urgent)$")


class EscrowCreateRequest(BaseModel):
    seller_address: str
    arbiter_address: Optional[str] = None
    amount: Decimal = Field(..., gt=0)
    description: str = Field(..., min_length=1, max_length=1000)
    timeout_hours: int = Field(default=48, ge=1, le=720)


class DepositRequest(BaseModel):
    amount: Optional[Decimal] = Field(default=None, gt=0, le=10000, description="Amount to deposit (required in ledger mode)")


class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: str
    checks: dict


# Monero address validation
_BASE58_ALPHABET_STR = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_ALPHABET = re.compile(r"^[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]+$")
_MAINNET_PREFIXES = ("4", "8")
_STAGENET_PREFIXES = ("5", "7")
_TESTNET_PREFIXES = ("9", "B")
_NETWORK_PREFIXES = {
    "mainnet": _MAINNET_PREFIXES,
    "stagenet": _STAGENET_PREFIXES,
    "testnet": _TESTNET_PREFIXES,
}

# Monero base58 block sizes: input bytes -> encoded chars
_FULL_BLOCK_SIZE = 8
_FULL_ENCODED_BLOCK_SIZE = 11
_ENCODED_BLOCK_SIZES = [0, 2, 3, 5, 6, 7, 9, 10, 11]


def _monero_base58_decode(address: str) -> bytes:
    """Decode a Monero base58-encoded address to raw bytes.

    Monero uses a custom base58 encoding that processes the input
    in 11-character blocks (mapping to 8 bytes each), with a shorter
    final block.
    """
    alphabet = _BASE58_ALPHABET_STR

    def _decode_block(block: str, target_size: int) -> bytes:
        num = 0
        for ch in block:
            idx = alphabet.index(ch)
            num = num * 58 + idx
        result = num.to_bytes(target_size, byteorder="big")
        return result

    full_blocks = len(address) // _FULL_ENCODED_BLOCK_SIZE
    last_block_size = len(address) % _FULL_ENCODED_BLOCK_SIZE

    result = bytearray()
    for i in range(full_blocks):
        start = i * _FULL_ENCODED_BLOCK_SIZE
        block = address[start:start + _FULL_ENCODED_BLOCK_SIZE]
        result.extend(_decode_block(block, _FULL_BLOCK_SIZE))

    if last_block_size > 0:
        last_block = address[full_blocks * _FULL_ENCODED_BLOCK_SIZE:]
        if last_block_size not in _ENCODED_BLOCK_SIZES:
            raise ValueError("Invalid Monero address: bad trailing block size")
        target_bytes = _ENCODED_BLOCK_SIZES.index(last_block_size)
        result.extend(_decode_block(last_block, target_bytes))

    return bytes(result)


def _keccak256(data: bytes) -> bytes:
    """Compute Keccak-256 hash (used by Monero, NOT FIPS-202 SHA3-256).

    Tries pycryptodome first (preferred, already in requirements), then pysha3.
    Raises RuntimeError if neither library is available so that a broken
    environment is detected loudly rather than silently producing wrong checksums.
    """
    try:
        from Crypto.Hash import keccak
        k = keccak.new(digest_bits=256)
        k.update(data)
        return k.digest()
    except ImportError:
        pass

    try:
        import sha3
        k = sha3.keccak_256()
        k.update(data)
        return k.digest()
    except ImportError:
        pass

    raise RuntimeError(
        "Keccak-256 requires pycryptodome or pysha3. "
        "Install pycryptodome: pip install pycryptodome"
    )


def validate_monero_address(address: str) -> str:
    """Validate Monero address: prefix, length, base58 alphabet, network, and checksum."""
    from sthrip.config import get_settings
    network = get_settings().monero_network
    allowed = _NETWORK_PREFIXES.get(network, _MAINNET_PREFIXES + _STAGENET_PREFIXES + _TESTNET_PREFIXES)

    if not address.startswith(allowed):
        raise ValueError(f"Invalid address prefix for {network} network")
    if len(address) not in (95, 106):
        raise ValueError("Invalid Monero address length")
    if not _BASE58_ALPHABET.match(address):
        raise ValueError("Address contains invalid characters")

    # Checksum validation: last 4 bytes are Keccak-256 of the payload
    # Skip in dev environment to allow synthetic test addresses
    if get_settings().environment != "dev":
        try:
            decoded = _monero_base58_decode(address)
            payload = decoded[:-4]
            checksum = decoded[-4:]
            expected_checksum = _keccak256(payload)[:4]
            if checksum != expected_checksum:
                raise ValueError("Invalid Monero address checksum")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Invalid Monero address: {e}")

    return address


class WithdrawRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=10000, description="Amount to withdraw")
    address: str = Field(min_length=10, max_length=200, description="XMR address to withdraw to")

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return validate_monero_address(v)
