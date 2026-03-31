"""Pydantic request/response models for the Sthrip API."""

import re
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from sthrip.services.url_validator import validate_url_target, SSRFBlockedError


class POWChallengeResponse(BaseModel):
    """Proof-of-work challenge returned by POST /v2/agents/register/challenge."""
    algorithm: str
    difficulty_bits: int
    nonce: str
    expires_at: str


class POWSubmission(BaseModel):
    """Inline PoW proof attached to a registration request."""
    nonce: str
    difficulty_bits: int
    expires_at: str
    solution: str


class AgentRegistration(BaseModel):
    agent_name: str = Field(..., min_length=3, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$")
    webhook_url: Optional[str] = None
    privacy_level: str = Field(default="medium", pattern=r"^(low|medium|high|paranoid)$")
    xmr_address: Optional[str] = None
    base_address: Optional[str] = None
    solana_address: Optional[str] = None

    # Marketplace fields (optional at registration)
    capabilities: Optional[List[str]] = Field(default=None, max_length=20)
    pricing: Optional[Dict[str, str]] = None
    description: Optional[str] = Field(default=None, max_length=500)
    accepts_escrow: Optional[bool] = None

    # Proof-of-work (optional for backward compat; can be made mandatory later)
    pow_challenge: Optional[POWSubmission] = None

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

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("Maximum 20 capabilities allowed")
        for cap in v:
            if not cap or len(cap) > 50:
                raise ValueError("Each capability must be 1-50 characters")
        return v

    @field_validator("pricing")
    @classmethod
    def validate_pricing(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("Maximum 20 pricing entries allowed")
        for key, val in v.items():
            if not key or len(key) > 50:
                raise ValueError("Pricing key must be 1-50 characters")
            if not val or len(val) > 100:
                raise ValueError("Pricing value must be 1-100 characters")
        return v


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

    # Marketplace fields
    capabilities: Optional[List[str]] = None
    pricing: Optional[Dict[str, str]] = None
    description: Optional[str] = Field(default=None, max_length=500)
    accepts_escrow: Optional[bool] = None

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

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("Maximum 20 capabilities allowed")
        for cap in v:
            if not cap or len(cap) > 50:
                raise ValueError("Each capability must be 1-50 characters")
        return v

    @field_validator("pricing")
    @classmethod
    def validate_pricing(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("Maximum 20 pricing entries allowed")
        for key, val in v.items():
            if not key or len(key) > 50:
                raise ValueError("Pricing key must be 1-50 characters")
            if not val or len(val) > 100:
                raise ValueError("Pricing value must be 1-100 characters")
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

    # Marketplace fields
    capabilities: List[str] = Field(default_factory=list)
    pricing: Dict[str, str] = Field(default_factory=dict)
    description: Optional[str] = None
    accepts_escrow: bool = True


class AgentMarketplaceResponse(BaseModel):
    """Marketplace-focused agent response with capabilities and pricing."""
    agent_name: str
    description: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)
    pricing: Dict[str, str] = Field(default_factory=dict)
    accepts_escrow: bool = True
    tier: str
    trust_score: int
    verified_at: Optional[str] = None


class PaymentRequest(BaseModel):
    to_address: str = Field(..., description="Recipient Monero address")
    amount: Decimal = Field(..., gt=0, description="Amount in XMR")
    memo: Optional[str] = Field(None, max_length=1000)
    privacy_level: Optional[str] = Field(None, pattern=r"^(low|medium|high|paranoid)$")
    use_hub_routing: bool = Field(False, description="Use hub routing for instant confirmation")


class HubPaymentRequest(BaseModel):
    to_agent_name: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$', description="Recipient agent name")
    amount: Decimal = Field(..., ge=Decimal("0.0001"), le=9980, description="Amount in XMR (min 0.0001 to cover minimum fee)")
    memo: Optional[str] = Field(default=None, max_length=500)
    urgency: str = Field(default="normal", pattern=r"^(normal|urgent)$")


class MilestoneDefinition(BaseModel):
    """Definition of a single milestone within a multi-milestone escrow."""
    description: str = Field(..., min_length=1, max_length=500)
    amount: Decimal = Field(..., gt=Decimal("0"), le=Decimal("10000"))
    delivery_timeout_hours: int = Field(..., ge=1, le=720)
    review_timeout_hours: int = Field(..., ge=1, le=168)


class EscrowCreateRequest(BaseModel):
    seller_agent_name: str = Field(..., min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9_-]+$")
    amount: Decimal = Field(..., ge=Decimal("0.001"), le=Decimal("10000"))
    description: str = Field(..., min_length=1, max_length=1000)
    accept_timeout_hours: int = Field(default=24, ge=1, le=168)
    delivery_timeout_hours: int = Field(default=48, ge=1, le=720)
    review_timeout_hours: int = Field(default=24, ge=1, le=168)
    milestones: Optional[List[MilestoneDefinition]] = Field(
        default=None, min_length=1, max_length=10,
    )
    mode: Optional[str] = Field(
        default="hub-held",
        pattern=r"^(hub-held|multisig)$",
        description="Escrow mode: hub-held (default) or multisig (2-of-3)",
    )

    @model_validator(mode="after")
    def validate_milestone_amounts(self) -> "EscrowCreateRequest":
        if self.milestones is None:
            return self
        total = sum(m.amount for m in self.milestones)
        if total != self.amount:
            raise ValueError(
                f"Sum of milestone amounts ({total}) must equal "
                f"the deal amount ({self.amount})"
            )
        return self

    @model_validator(mode="after")
    def validate_multisig_no_milestones(self) -> "EscrowCreateRequest":
        if self.mode == "multisig" and self.milestones is not None:
            raise ValueError(
                "Multisig escrow does not support milestones"
            )
        return self


class EscrowCreateResponse(BaseModel):
    escrow_id: str
    status: str
    amount: str
    seller_agent_name: str
    description: str
    accept_deadline: str
    created_at: str


class EscrowAcceptResponse(BaseModel):
    escrow_id: str
    status: str
    delivery_deadline: str


class EscrowDeliverResponse(BaseModel):
    escrow_id: str
    status: str
    review_deadline: str


class EscrowReleaseRequest(BaseModel):
    release_amount: Decimal = Field(..., ge=Decimal("0"), le=Decimal("10000"))


class MilestoneReleaseRequest(BaseModel):
    release_amount: Decimal = Field(..., ge=Decimal("0"), le=Decimal("10000"))


class EscrowReleaseResponse(BaseModel):
    escrow_id: str
    status: str
    released_to_seller: str
    fee: str
    seller_received: str
    refunded_to_buyer: str
    completed_at: str


class EscrowCancelResponse(BaseModel):
    escrow_id: str
    status: str
    refunded: str


class EscrowDetailResponse(BaseModel):
    escrow_id: str
    status: str
    amount: str
    description: Optional[str]
    buyer_agent_name: str
    seller_agent_name: str
    accept_deadline: Optional[str]
    delivery_deadline: Optional[str]
    review_deadline: Optional[str]
    created_at: str
    accepted_at: Optional[str]
    delivered_at: Optional[str]
    completed_at: Optional[str]


class EscrowListResponse(BaseModel):
    items: list
    total: int
    limit: int
    offset: int


class MilestoneReleaseRequest(BaseModel):
    """Request body for releasing a specific milestone."""
    release_amount: Decimal = Field(..., ge=Decimal("0"), le=Decimal("10000"))


class MilestoneResponse(BaseModel):
    """Response model for a single escrow milestone."""
    sequence: int
    description: str
    amount: str
    status: str
    delivery_timeout_hours: int
    review_timeout_hours: int
    delivery_deadline: Optional[str] = None
    review_deadline: Optional[str] = None
    release_amount: Optional[str] = None
    fee_amount: Optional[str] = None
    activated_at: Optional[str] = None
    delivered_at: Optional[str] = None
    completed_at: Optional[str] = None


class MilestoneDeliverResponse(BaseModel):
    """Response after marking a milestone as delivered."""
    escrow_id: str
    milestone_sequence: int
    status: str
    review_deadline: str


class MilestoneReleaseResponse(BaseModel):
    """Response after releasing funds for a milestone."""
    escrow_id: str
    milestone_sequence: int
    status: str
    released_to_seller: str
    fee: str
    seller_received: str
    deal_status: str
    deal_total_released: str
    deal_total_fees: str


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
    amount: Decimal = Field(ge=Decimal("0.001"), le=10000, description="Amount to withdraw (minimum 0.001 XMR)")
    address: str = Field(min_length=10, max_length=200, description="XMR address to withdraw to")

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return validate_monero_address(v)


# ---------------------------------------------------------------------------
# Spending policy schemas
# ---------------------------------------------------------------------------

class SpendingPolicyRequest(BaseModel):
    """Upsert body for PUT /v2/me/spending-policy."""
    max_per_tx: Optional[Decimal] = Field(default=None, ge=0, le=100000)
    max_per_session: Optional[Decimal] = Field(default=None, ge=0, le=100000)
    daily_limit: Optional[Decimal] = Field(default=None, ge=0, le=1000000)
    allowed_agents: Optional[List[str]] = Field(default=None, max_length=50)
    blocked_agents: Optional[List[str]] = Field(default=None, max_length=50)
    require_escrow_above: Optional[Decimal] = Field(default=None, ge=0, le=100000)

    @field_validator("allowed_agents", "blocked_agents")
    @classmethod
    def validate_glob_patterns(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        for pat in v:
            if not pat or len(pat) > 100:
                raise ValueError("Each pattern must be 1-100 characters")
        return v


class SpendingPolicyResponse(BaseModel):
    """Response body for GET /v2/me/spending-policy."""
    max_per_tx: Optional[str] = None
    max_per_session: Optional[str] = None
    daily_limit: Optional[str] = None
    allowed_agents: Optional[List[str]] = None
    blocked_agents: Optional[List[str]] = None
    require_escrow_above: Optional[str] = None
    is_active: bool = True


class SpendingStatusResponse(BaseModel):
    """Response body for GET /v2/me/spending-status (future endpoint)."""
    daily_spent: Optional[str] = None
    daily_limit: Optional[str] = None
    daily_remaining: Optional[str] = None
    session_spent: Optional[str] = None
    session_limit: Optional[str] = None
    session_remaining: Optional[str] = None


# ---------------------------------------------------------------------------
# Webhook endpoint schemas
# ---------------------------------------------------------------------------

class WebhookEndpointCreate(BaseModel):
    """Request body for registering a new webhook endpoint."""
    url: str = Field(..., min_length=10, max_length=2048)
    event_filters: Optional[List[str]] = Field(
        default=None,
        max_length=20,
        description="Event patterns to subscribe to (e.g. ['payment.*', 'escrow.*']). null = all events.",
    )
    description: Optional[str] = Field(default=None, max_length=256)

    @field_validator("url")
    @classmethod
    def validate_webhook_endpoint_url(cls, v: str) -> str:
        try:
            validate_url_target(v)
        except SSRFBlockedError as e:
            raise ValueError(f"Webhook URL blocked: {e}")
        return v

    @field_validator("event_filters")
    @classmethod
    def validate_event_filters(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        for pattern in v:
            if not pattern or len(pattern) > 100:
                raise ValueError("Each event filter must be 1-100 characters")
        return v


class WebhookEndpointResponse(BaseModel):
    """Public representation of a webhook endpoint (no secret)."""
    id: str
    url: str
    description: Optional[str] = None
    event_filters: Optional[List[str]] = None
    is_active: bool
    failure_count: int
    created_at: str


class WebhookEndpointCreateResponse(WebhookEndpointResponse):
    """Response returned on create/rotate -- includes the plaintext secret."""
    secret: str


# ---------------------------------------------------------------------------
# ZK Reputation Proof schemas (Task 7)
# ---------------------------------------------------------------------------

class ReputationProofRequest(BaseModel):
    """Request body for generating a ZK reputation proof."""
    threshold: int = Field(..., ge=0, le=100, description="Minimum trust score to prove")


class ReputationProofResponse(BaseModel):
    """Response containing the commitment and proof payload."""
    commitment: str
    proof: str
    threshold: int


class ReputationVerifyRequest(BaseModel):
    """Request body for verifying a ZK reputation proof."""
    commitment: str = Field(..., min_length=1, max_length=256)
    proof: str = Field(..., min_length=1, description="Base64-encoded proof payload")
    threshold: int = Field(..., ge=0, le=100)


class ReputationVerifyResponse(BaseModel):
    """Result of ZK reputation proof verification."""
    valid: bool


# ---------------------------------------------------------------------------
# E2E Encrypted Messaging schemas
# ---------------------------------------------------------------------------

class EncryptionKeyRequest(BaseModel):
    """Request body for registering an agent's Curve25519 public key."""
    public_key: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Base64-encoded Curve25519 public key (32 bytes)",
    )


class MessageSendRequest(BaseModel):
    """Request body for sending an encrypted message via the hub relay."""
    to_agent_id: str = Field(..., min_length=1, max_length=64)
    ciphertext: str = Field(..., min_length=1, description="Base64-encoded NaCl Box ciphertext")
    nonce: str = Field(..., min_length=1, max_length=64, description="Base64-encoded 24-byte nonce")
    sender_public_key: str = Field(..., min_length=1, max_length=64, description="Base64-encoded Curve25519 public key")
    payment_id: Optional[str] = Field(default=None, max_length=64)


class MessageResponse(BaseModel):
    """A single relayed encrypted message."""
    id: str
    from_agent_id: str
    ciphertext: str
    nonce: str
    sender_public_key: str
    payment_id: Optional[str] = None
    created_at: str


# ---------------------------------------------------------------------------
# Multisig Escrow schemas (Task 9)
# ---------------------------------------------------------------------------

class MultisigRoundRequest(BaseModel):
    """Submit key exchange data for a multisig setup round."""
    participant: str = Field(
        ...,
        pattern=r"^(buyer|seller|hub)$",
        description="Participant role: buyer, seller, or hub",
    )
    round_number: int = Field(..., ge=1, le=3)
    multisig_info: str = Field(
        ...,
        min_length=1,
        max_length=10000,
        description="Multisig info string from wallet RPC",
    )


class MultisigStateResponse(BaseModel):
    """Current state of a multisig escrow."""
    id: str
    escrow_deal_id: str
    state: str
    fee_collected: str
    funded_amount: Optional[str] = None
    multisig_address: Optional[str] = None
    timeout_at: Optional[str] = None
    created_at: Optional[str] = None
    buyer_wallet_id: Optional[str] = None
    seller_wallet_id: Optional[str] = None
    hub_wallet_id: Optional[str] = None
    release_initiator: Optional[str] = None
    dispute_reason: Optional[str] = None
    disputed_by: Optional[str] = None


class CosignRequest(BaseModel):
    """Cosign a partially-signed release transaction."""
    signer: str = Field(
        ...,
        pattern=r"^(buyer|seller|hub)$",
        description="Participant role cosigning the release",
    )
    signed_tx: str = Field(
        ...,
        min_length=1,
        max_length=100000,
        description="Fully-signed transaction hex",
    )


class DisputeRequest(BaseModel):
    """Raise a dispute on a multisig escrow."""
    disputer: str = Field(
        ...,
        pattern=r"^(buyer|seller|hub)$",
        description="Participant raising the dispute",
    )
    reason: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Reason for dispute",
    )
