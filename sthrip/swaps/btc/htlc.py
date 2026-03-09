"""
Bitcoin HTLC (Hash Time Locked Contract) Implementation

HTLC Script для атомарных свопов:
- Hash lock: разблокировка по preimage (для получателя)
- Time lock: возврат средств по истечении времени (для отправителя)

Script structure (P2WSH - Pay to Witness Script Hash):
    OP_SHA256 <32-byte-hash> OP_EQUAL
    OP_IF
        <recipient-pubkey>
    OP_ELSE
        <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
        <sender-pubkey>
    OP_ENDIF
    OP_CHECKSIG
"""

import hashlib
import struct
from typing import Optional, Tuple, Dict, Any
from decimal import Decimal
import secrets


# Bitcoin Opcodes
OP_0 = 0x00
OP_EQUAL = 0x87
OP_EQUALVERIFY = 0x88
OP_SHA256 = 0xa8
OP_CHECKSIG = 0xac
OP_CHECKLOCKTIMEVERIFY = 0xb1
OP_DROP = 0x75
OP_IF = 0x63
OP_ELSE = 0x67
OP_ENDIF = 0x68
OP_DUP = 0x76
OP_HASH160 = 0xa9


def encode_num(n: int) -> bytes:
    """Кодирует число для Bitcoin script"""
    if n == 0:
        return b''
    
    abs_n = abs(n)
    negative = n < 0
    result = bytearray()
    
    while abs_n:
        result.append(abs_n & 0xff)
        abs_n >>= 8
    
    # If the most significant byte is >= 0x80 and the value is positive,
    # push a new zero-byte to make the significant byte < 0x80 again
    if result[-1] & 0x80:
        if negative:
            result.append(0x80)
        else:
            result.append(0)
    elif negative:
        result[-1] |= 0x80
    
    return bytes(result)


def push_bytes(data: bytes) -> bytes:
    """Создает push operation для данных в script"""
    length = len(data)
    
    if length < 76:
        return bytes([length]) + data
    elif length < 256:
        return bytes([0x4c, length]) + data
    elif length < 65536:
        return bytes([0x4d]) + struct.pack('<H', length) + data
    else:
        return bytes([0x4e]) + struct.pack('<I', length) + data


def create_htlc_redeem_script(
    preimage_hash: bytes,
    recipient_pubkey: bytes,
    sender_pubkey: bytes,
    locktime: int
) -> bytes:
    """
    Создает HTLC redeem script.
    
    Args:
        preimage_hash: SHA256 hash of preimage (32 bytes)
        recipient_pubkey: Public key получателя (33 bytes compressed)
        sender_pubkey: Public key отправителя (33 bytes compressed)
        locktime: Block height или timestamp для timelock
        
    Returns:
        Redeem script bytes
        
    Script:
        OP_SHA256 <preimage_hash> OP_EQUAL
        OP_IF
            <recipient_pubkey>
        OP_ELSE
            <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
            <sender_pubkey>
        OP_ENDIF
        OP_CHECKSIG
    """
    locktime_bytes = encode_num(locktime)
    
    script = bytes([
        OP_SHA256,
        32  # Push 32 bytes
    ]) + preimage_hash + bytes([
        OP_EQUAL,
        OP_IF
    ]) + push_bytes(recipient_pubkey) + bytes([
        OP_ELSE
    ]) + push_bytes(locktime_bytes) + bytes([
        OP_CHECKLOCKTIMEVERIFY,
        OP_DROP
    ]) + push_bytes(sender_pubkey) + bytes([
        OP_ENDIF,
        OP_CHECKSIG
    ])
    
    return script


def create_htlc_address(
    preimage_hash: bytes,
    recipient_pubkey: bytes,
    sender_pubkey: bytes,
    locktime: int,
    network: str = "mainnet"
) -> Tuple[str, bytes]:
    """
    Создает P2WSH адрес для HTLC.
    
    Args:
        preimage_hash: SHA256 hash of preimage (32 bytes)
        recipient_pubkey: Public key получателя
        sender_pubkey: Public key отправителя
        locktime: Block height для timelock
        network: mainnet, testnet, или regtest
        
    Returns:
        Tuple of (address, redeem_script)
    """
    from ..utils.bitcoin import hash160, sha256, encode_bech32
    
    redeem_script = create_htlc_redeem_script(
        preimage_hash, recipient_pubkey, sender_pubkey, locktime
    )
    
    # P2WSH: witness script hash
    script_hash = sha256(redeem_script)
    
    # Encode as bech32 address
    hrp = "bc" if network == "mainnet" else "tb"
    address = encode_bech32(hrp, 0, script_hash)
    
    return address, redeem_script


def create_claim_witness(
    preimage: bytes,
    signature: bytes,
    redeem_script: bytes
) -> list:
    """
    Создает witness для claim HTLC (по preimage).
    
    Args:
        preimage: 32-byte preimage
        signature: DER-encoded signature + sighash byte
        redeem_script: HTLC redeem script
        
    Returns:
        List of witness items
    """
    return [
        signature,
        preimage,
        bytes([1]),  # OP_TRUE - выбираем IF branch
        redeem_script
    ]


def create_refund_witness(
    signature: bytes,
    redeem_script: bytes
) -> list:
    """
    Создает witness для refund HTLC (по timelock).
    
    Args:
        signature: DER-encoded signature + sighash byte
        redeem_script: HTLC redeem script
        
    Returns:
        List of witness items
    """
    return [
        signature,
        b'',  # OP_FALSE - выбираем ELSE branch
        redeem_script
    ]


class BitcoinHTLC:
    """
    HTLC контракт для Bitcoin.
    
    Использует P2WSH (Pay-to-Witness-Script-Hash) для минимизации комиссий.
    """
    
    def __init__(
        self,
        rpc_client: Any,  # BitcoinRPCClient
        network: str = "mainnet"
    ):
        self.rpc = rpc_client
        self.network = network
        self.preimage: Optional[bytes] = None
        self.preimage_hash: Optional[bytes] = None
        
    def generate_preimage(self) -> bytes:
        """Генерирует случайный 32-byte preimage"""
        self.preimage = secrets.token_bytes(32)
        self.preimage_hash = hashlib.sha256(self.preimage).digest()
        return self.preimage
    
    def set_preimage(self, preimage: bytes) -> None:
        """Устанавливает preimage (для получателя)"""
        if len(preimage) != 32:
            raise ValueError("Preimage must be 32 bytes")
        self.preimage = preimage
        self.preimage_hash = hashlib.sha256(preimage).digest()
    
    def get_preimage_hash(self) -> bytes:
        """Возвращает hash preimage"""
        if not self.preimage_hash:
            raise ValueError("Preimage not set")
        return self.preimage_hash
    
    def create_htlc(
        self,
        sender_pubkey: bytes,
        recipient_pubkey: bytes,
        locktime_blocks: int,
        amount_btc: Decimal
    ) -> Dict[str, Any]:
        """
        Создает HTLC контракт.
        
        Args:
            sender_pubkey: Public key отправителя (33 bytes compressed)
            recipient_pubkey: Public key получателя (33 bytes compressed)
            locktime_blocks: Количество блоков до refund
            amount_btc: Сумма в BTC
            
        Returns:
            Dict с address, redeem_script, locktime
        """
        if not self.preimage_hash:
            self.generate_preimage()
        
        # Calculate absolute locktime
        current_height = self.rpc.get_block_count()
        locktime = current_height + locktime_blocks
        
        # Create HTLC address
        address, redeem_script = create_htlc_address(
            self.preimage_hash,
            recipient_pubkey,
            sender_pubkey,
            locktime,
            self.network
        )
        
        result = {
            "address": address,
            "redeem_script": redeem_script.hex(),
            "redeem_script_bytes": redeem_script,
            "preimage_hash": self.preimage_hash.hex(),
            "locktime": locktime,
            "amount": amount_btc,
            "sender_pubkey": sender_pubkey.hex(),
            "recipient_pubkey": recipient_pubkey.hex(),
        }
        
        # Include preimage if we generated it
        if self.preimage:
            result["preimage"] = self.preimage.hex()
            
        return result
    
    def fund_htlc(self, htlc_address: str, amount_btc: Decimal) -> str:
        """
        Фандит HTLC адрес.
        
        Returns:
            txid funding транзакции
        """
        return self.rpc.send_to_address(htlc_address, amount_btc)
    
    def build_claim_transaction(
        self,
        funding_txid: str,
        funding_vout: int,
        amount: Decimal,
        recipient_address: str,
        preimage: bytes,
        redeem_script: bytes,
        fee_btc: Decimal = Decimal("0.0001")
    ) -> str:
        """
        Строит транзакцию для claim HTLC.
        
        Note: Требуется подпись получателя (реализуется отдельно).
        """
        # Это будет реализовано вместе с wallet integration
        # Требует доступа к приватному ключу для подписи
        raise NotImplementedError("Requires wallet integration")
    
    def build_refund_transaction(
        self,
        funding_txid: str,
        funding_vout: int,
        amount: Decimal,
        sender_address: str,
        redeem_script: bytes,
        fee_btc: Decimal = Decimal("0.0001")
    ) -> str:
        """
        Строит транзакцию для refund HTLC.
        
        Note: Требуется подпись отправителя + timelock должен истечь.
        """
        raise NotImplementedError("Requires wallet integration")


def create_simple_htlc_for_swap(
    rpc_client: Any,
    sender_pubkey_hex: str,
    recipient_pubkey_hex: str,
    amount_btc: Decimal,
    locktime_hours: int = 24,
    network: str = "mainnet"
) -> Dict[str, Any]:
    """
    Упрощенная функция создания HTLC для атомарного свопа.
    
    Args:
        rpc_client: BitcoinRPCClient instance
        sender_pubkey_hex: Sender's compressed public key (hex)
        recipient_pubkey_hex: Recipient's compressed public key (hex)
        amount_btc: Amount in BTC
        locktime_hours: Hours until refund (converted to blocks, ~6 blocks/hour)
        network: Bitcoin network
        
    Returns:
        HTLC contract details
    """
    # Convert hours to blocks (approximately 6 blocks per hour)
    locktime_blocks = locktime_hours * 6
    
    htlc = BitcoinHTLC(rpc_client, network)
    
    sender_pubkey = bytes.fromhex(sender_pubkey_hex)
    recipient_pubkey = bytes.fromhex(recipient_pubkey_hex)
    
    contract = htlc.create_htlc(
        sender_pubkey,
        recipient_pubkey,
        locktime_blocks,
        amount_btc
    )
    
    # Генерируем preimage для отправителя
    contract["preimage"] = htlc.preimage.hex()
    
    return contract
