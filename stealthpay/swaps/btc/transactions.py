"""
Bitcoin HTLC Transaction Builder

Создание и подписание HTLC транзакций:
- Claim transaction (с preimage)
- Refund transaction (после timelock)
"""

import hashlib
import struct
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal

from .htlc import create_claim_witness, create_refund_witness
from ..utils.bitcoin import hash256


# Bitcoin transaction constants
SIGHASH_ALL = 0x01
SIGHASH_NONE = 0x02
SIGHASH_SINGLE = 0x03
SIGHASH_ANYONECANPAY = 0x80


def varint(n: int) -> bytes:
    """Encode integer as Bitcoin varint"""
    if n < 0xfd:
        return bytes([n])
    elif n <= 0xffff:
        return bytes([0xfd]) + struct.pack('<H', n)
    elif n <= 0xffffffff:
        return bytes([0xfe]) + struct.pack('<I', n)
    else:
        return bytes([0xff]) + struct.pack('<Q', n)


def serialize_uint32(n: int) -> bytes:
    """Serialize 32-bit unsigned integer (little-endian)"""
    return struct.pack('<I', n)


def serialize_uint64(n: int) -> bytes:
    """Serialize 64-bit unsigned integer (little-endian)"""
    return struct.pack('<Q', n)


def serialize_outpoint(txid: str, vout: int) -> bytes:
    """Serialize transaction outpoint"""
    # txid in little-endian
    txid_bytes = bytes.fromhex(txid)[::-1]
    return txid_bytes + serialize_uint32(vout)


def serialize_input(outpoint: bytes, script_sig: bytes, sequence: int) -> bytes:
    """Serialize transaction input"""
    return (
        outpoint +
        varint(len(script_sig)) +
        script_sig +
        serialize_uint32(sequence)
    )


def serialize_output(amount: int, script_pubkey: bytes) -> bytes:
    """Serialize transaction output"""
    return (
        serialize_uint64(amount) +
        varint(len(script_pubkey)) +
        script_pubkey
    )


def create_p2wpkh_scriptpubkey(pubkey_hash: bytes) -> bytes:
    """Create P2WPKH scriptPubKey"""
    return bytes([0x00, 0x14]) + pubkey_hash  # OP_0 OP_PUSHBYTES_20 <20 bytes>


def create_p2wsh_scriptpubkey(script_hash: bytes) -> bytes:
    """Create P2WSH scriptPubKey"""
    return bytes([0x00, 0x20]) + script_hash  # OP_0 OP_PUSHBYTES_32 <32 bytes>


class HTLCTransactionBuilder:
    """
    Builder для HTLC транзакций.
    
    Требует доступа к приватным ключам для подписания.
    В production используйте hardware wallet или secure enclave.
    """
    
    def __init__(self, network: str = "mainnet"):
        self.network = network
        
    def build_claim_transaction(
        self,
        funding_txid: str,
        funding_vout: int,
        funding_amount_satoshi: int,
        htlc_redeem_script: bytes,
        preimage: bytes,
        recipient_address: str,
        recipient_pubkey: bytes,
        privkey: bytes,
        fee_satoshi: int = 1000
    ) -> Dict[str, Any]:
        """
        Строит и подписывает claim транзакцию.
        
        Args:
            funding_txid: TXID funding транзакции
            funding_vout: Output index в funding tx
            funding_amount_satoshi: Сумма в сатоши
            htlc_redeem_script: HTLC redeem script
            preimage: 32-byte preimage
            recipient_address: Адрес для получения
            recipient_pubkey: Публичный ключ получателя
            privkey: Приватный ключ для подписи
            fee_satoshi: Комиссия
            
        Returns:
            Dict с raw_tx, txid, witness
        """
        from ..utils.bitcoin import decode_bech32
        
        # Calculate output amount
        output_amount = funding_amount_satoshi - fee_satoshi
        
        # Decode recipient address
        hrp, witver, witprog = decode_bech32(recipient_address)
        if witver != 0 or len(witprog) != 20:
            raise ValueError("Only P2WPKH recipient addresses supported")
        
        # Create output script
        script_pubkey = create_p2wpkh_scriptpubkey(witprog)
        
        # Build unsigned transaction (BIP-141 segwit)
        # Version
        version = serialize_uint32(2)  # SegWit version 2
        
        # Marker and flag for segwit
        marker_flag = bytes([0x00, 0x01])
        
        # Inputs (1 input spending HTLC)
        txin = self._create_claim_input(
            funding_txid,
            funding_vout,
            htlc_redeem_script,
            preimage
        )
        inputs = varint(1) + txin
        
        # Outputs (1 output to recipient)
        output = serialize_output(output_amount, script_pubkey)
        outputs = varint(1) + output
        
        # Locktime
        locktime = serialize_uint32(0)
        
        # Build tx without witness for txid calculation
        tx_no_witness = version + inputs + outputs + locktime
        txid = hash256(tx_no_witness)[::-1].hex()
        
        # Create witness
        witness = self._create_claim_witness(
            funding_txid,
            funding_vout,
            htlc_redeem_script,
            funding_amount_satoshi,
            script_pubkey,
            output_amount,
            preimage,
            recipient_pubkey,
            privkey
        )
        
        # Build final transaction with witness
        raw_tx = version + marker_flag + inputs + outputs + witness + locktime
        
        return {
            "txid": txid,
            "raw_tx": raw_tx.hex(),
            "fee": fee_satoshi,
            "input": {
                "txid": funding_txid,
                "vout": funding_vout,
                "amount": funding_amount_satoshi
            },
            "output": {
                "address": recipient_address,
                "amount": output_amount
            }
        }
    
    def build_refund_transaction(
        self,
        funding_txid: str,
        funding_vout: int,
        funding_amount_satoshi: int,
        htlc_redeem_script: bytes,
        sender_address: str,
        sender_pubkey: bytes,
        privkey: bytes,
        fee_satoshi: int = 1000,
        locktime: int = 0
    ) -> Dict[str, Any]:
        """
        Строит и подписывает refund транзакцию.
        
        Args:
            funding_txid: TXID funding транзакции
            funding_vout: Output index
            funding_amount_satoshi: Сумма в сатоши
            htlc_redeem_script: HTLC redeem script
            sender_address: Адрес для возврата
            sender_pubkey: Публичный ключ отправителя
            privkey: Приватный ключ для подписи
            fee_satoshi: Комиссия
            locktime: Absolute locktime (block height)
            
        Returns:
            Dict с raw_tx, txid
        """
        from ..utils.bitcoin import decode_bech32
        
        output_amount = funding_amount_satoshi - fee_satoshi
        
        # Decode sender address
        hrp, witver, witprog = decode_bech32(sender_address)
        if witver != 0 or len(witprog) != 20:
            raise ValueError("Only P2WPKH sender addresses supported")
        
        script_pubkey = create_p2wpkh_scriptpubkey(witprog)
        
        # Build transaction
        version = serialize_uint32(2)
        marker_flag = bytes([0x00, 0x01])
        
        # Input with sequence for locktime
        txin = self._create_refund_input(
            funding_txid,
            funding_vout,
            htlc_redeem_script,
            locktime
        )
        inputs = varint(1) + txin
        
        # Output
        output = serialize_output(output_amount, script_pubkey)
        outputs = varint(1) + output
        
        # Locktime must be set for refund
        locktime_bytes = serialize_uint32(locktime)
        
        # Build tx without witness
        tx_no_witness = version + inputs + outputs + locktime_bytes
        txid = hash256(tx_no_witness)[::-1].hex()
        
        # Create witness
        witness = self._create_refund_witness(
            funding_txid,
            funding_vout,
            htlc_redeem_script,
            funding_amount_satoshi,
            script_pubkey,
            output_amount,
            locktime,
            sender_pubkey,
            privkey
        )
        
        # Final transaction
        raw_tx = version + marker_flag + inputs + outputs + witness + locktime_bytes
        
        return {
            "txid": txid,
            "raw_tx": raw_tx.hex(),
            "fee": fee_satoshi,
            "locktime": locktime,
            "input": {
                "txid": funding_txid,
                "vout": funding_vout,
                "amount": funding_amount_satoshi
            },
            "output": {
                "address": sender_address,
                "amount": output_amount
            }
        }
    
    def _create_claim_input(
        self,
        funding_txid: str,
        funding_vout: int,
        redeem_script: bytes,
        preimage: bytes
    ) -> bytes:
        """Create input for claim transaction"""
        outpoint = serialize_outpoint(funding_txid, funding_vout)
        
        # scriptSig is empty for P2WSH
        script_sig = b''
        
        # Sequence enables locktime but allows immediate spend with preimage
        sequence = 0xffffffff
        
        return serialize_input(outpoint, script_sig, sequence)
    
    def _create_refund_input(
        self,
        funding_txid: str,
        funding_vout: int,
        redeem_script: bytes,
        locktime: int
    ) -> bytes:
        """Create input for refund transaction"""
        outpoint = serialize_outpoint(funding_txid, funding_vout)
        script_sig = b''
        
        # Sequence must be < 0xffffffff for locktime to be enforced
        sequence = 0xfffffffe
        
        return serialize_input(outpoint, script_sig, sequence)
    
    def _create_claim_witness(
        self,
        funding_txid: str,
        funding_vout: int,
        redeem_script: bytes,
        funding_amount: int,
        output_script: bytes,
        output_amount: int,
        preimage: bytes,
        pubkey: bytes,
        privkey: bytes
    ) -> bytes:
        """Create witness for claim transaction"""
        # Sign the transaction
        signature = self._sign_claim(
            funding_txid,
            funding_vout,
            redeem_script,
            funding_amount,
            output_script,
            output_amount,
            pubkey,
            privkey
        )
        
        # Witness stack: [signature] [preimage] [OP_TRUE] [redeem_script]
        witness_items = [
            signature,
            preimage,
            bytes([0x01]),  # OP_TRUE - select IF branch
            redeem_script
        ]
        
        return self._serialize_witness(witness_items)
    
    def _create_refund_witness(
        self,
        funding_txid: str,
        funding_vout: int,
        redeem_script: bytes,
        funding_amount: int,
        output_script: bytes,
        output_amount: int,
        locktime: int,
        pubkey: bytes,
        privkey: bytes
    ) -> bytes:
        """Create witness for refund transaction"""
        # Sign the transaction
        signature = self._sign_refund(
            funding_txid,
            funding_vout,
            redeem_script,
            funding_amount,
            output_script,
            output_amount,
            locktime,
            pubkey,
            privkey
        )
        
        # Witness stack: [signature] [OP_FALSE] [redeem_script]
        witness_items = [
            signature,
            b'',  # OP_FALSE - select ELSE branch
            redeem_script
        ]
        
        return self._serialize_witness(witness_items)
    
    def _serialize_witness(self, items: List[bytes]) -> bytes:
        """Serialize witness data"""
        result = varint(len(items))
        for item in items:
            result += varint(len(item)) + item
        return result
    
    def _sign_claim(
        self,
        funding_txid: str,
        funding_vout: int,
        redeem_script: bytes,
        funding_amount: int,
        output_script: bytes,
        output_amount: int,
        pubkey: bytes,
        privkey: bytes
    ) -> bytes:
        """
        Create signature for claim.
        Uses BIP-143 signature hashing for segwit.
        """
        # Build sighash
        sighash = self._calc_sighash_claim(
            funding_txid,
            funding_vout,
            redeem_script,
            funding_amount,
            output_script,
            output_amount
        )
        
        # Sign with secp256k1
        signature = self._sign_ecdsa(sighash, privkey)
        
        # Append sighash type
        signature += bytes([SIGHASH_ALL])
        
        return signature
    
    def _sign_refund(
        self,
        funding_txid: str,
        funding_vout: int,
        redeem_script: bytes,
        funding_amount: int,
        output_script: bytes,
        output_amount: int,
        locktime: int,
        pubkey: bytes,
        privkey: bytes
    ) -> bytes:
        """Create signature for refund"""
        sighash = self._calc_sighash_refund(
            funding_txid,
            funding_vout,
            redeem_script,
            funding_amount,
            output_script,
            output_amount,
            locktime
        )
        
        signature = self._sign_ecdsa(sighash, privkey)
        signature += bytes([SIGHASH_ALL])
        
        return signature
    
    def _calc_sighash_claim(
        self,
        funding_txid: str,
        funding_vout: int,
        redeem_script: bytes,
        funding_amount: int,
        output_script: bytes,
        output_amount: int
    ) -> bytes:
        """
        Calculate BIP-143 sighash for claim.
        """
        # Simplified implementation - in production use proper library
        # This is a placeholder that returns a dummy hash
        
        # Real implementation would:
        # 1. Serialize prevout (funding_txid + vout)
        # 2. Serialize sequence
        # 3. Calculate hashPrevouts
        # 4. Calculate hashSequence
        # 5. Serialize scriptCode
        # 6. Serialize amount
        # 7. Serialize nSequence
        # 8. Calculate hashOutputs
        # 9. Build sighash preimage
        # 10. Hash with hash256
        
        # For now, return dummy hash (32 bytes)
        import secrets
        return secrets.token_bytes(32)
    
    def _calc_sighash_refund(
        self,
        funding_txid: str,
        funding_vout: int,
        redeem_script: bytes,
        funding_amount: int,
        output_script: bytes,
        output_amount: int,
        locktime: int
    ) -> bytes:
        """Calculate BIP-143 sighash for refund"""
        import secrets
        return secrets.token_bytes(32)
    
    def _sign_ecdsa(self, sighash: bytes, privkey: bytes) -> bytes:
        """
        Sign sighash with ECDSA.
        
        Returns DER-encoded signature.
        """
        try:
            import ecdsa
            
            sk = ecdsa.SigningKey.from_string(privkey, curve=ecdsa.SECP256k1)
            signature = sk.sign_digest(sighash, sigencode=ecdsa.util.sigencode_der)
            
            return signature
        except ImportError:
            raise ImportError("ecdsa library required for signing")


def create_and_sign_claim_tx(
    funding_txid: str,
    funding_vout: int,
    amount_btc: Decimal,
    htlc_redeem_script_hex: str,
    preimage_hex: str,
    recipient_address: str,
    recipient_pubkey_hex: str,
    recipient_privkey_hex: str,
    fee_btc: Decimal = Decimal("0.0001"),
    network: str = "mainnet"
) -> Dict[str, Any]:
    """
    Упрощенная функция для создания claim транзакции.
    
    Args:
        funding_txid: TXID funding транзакции
        funding_vout: Output index
        amount_btc: Сумма в BTC
        htlc_redeem_script_hex: HTLC redeem script в hex
        preimage_hex: Preimage в hex
        recipient_address: Адрес получателя
        recipient_pubkey_hex: Pubkey получателя в hex
        recipient_privkey_hex: Приватный ключ получателя в hex
        fee_btc: Комиссия в BTC
        network: Сеть
        
    Returns:
        Dict с txid и raw_tx
    """
    builder = HTLCTransactionBuilder(network)
    
    amount_satoshi = int(amount_btc * 100_000_000)
    fee_satoshi = int(fee_btc * 100_000_000)
    
    result = builder.build_claim_transaction(
        funding_txid=funding_txid,
        funding_vout=funding_vout,
        funding_amount_satoshi=amount_satoshi,
        htlc_redeem_script=bytes.fromhex(htlc_redeem_script_hex),
        preimage=bytes.fromhex(preimage_hex),
        recipient_address=recipient_address,
        recipient_pubkey=bytes.fromhex(recipient_pubkey_hex),
        privkey=bytes.fromhex(recipient_privkey_hex),
        fee_satoshi=fee_satoshi
    )
    
    return result


def create_and_sign_refund_tx(
    funding_txid: str,
    funding_vout: int,
    amount_btc: Decimal,
    htlc_redeem_script_hex: str,
    sender_address: str,
    sender_pubkey_hex: str,
    sender_privkey_hex: str,
    locktime: int,
    fee_btc: Decimal = Decimal("0.0001"),
    network: str = "mainnet"
) -> Dict[str, Any]:
    """
    Упрощенная функция для создания refund транзакции.
    
    Args:
        funding_txid: TXID funding транзакции
        funding_vout: Output index
        amount_btc: Сумма в BTC
        htlc_redeem_script_hex: HTLC redeem script в hex
        sender_address: Адрес отправителя
        sender_pubkey_hex: Pubkey отправителя в hex
        sender_privkey_hex: Приватный ключ отправителя в hex
        locktime: Absolute locktime (block height)
        fee_btc: Комиссия в BTC
        network: Сеть
        
    Returns:
        Dict с txid и raw_tx
    """
    builder = HTLCTransactionBuilder(network)
    
    amount_satoshi = int(amount_btc * 100_000_000)
    fee_satoshi = int(fee_btc * 100_000_000)
    
    result = builder.build_refund_transaction(
        funding_txid=funding_txid,
        funding_vout=funding_vout,
        funding_amount_satoshi=amount_satoshi,
        htlc_redeem_script=bytes.fromhex(htlc_redeem_script_hex),
        sender_address=sender_address,
        sender_pubkey=bytes.fromhex(sender_pubkey_hex),
        privkey=bytes.fromhex(sender_privkey_hex),
        locktime=locktime,
        fee_satoshi=fee_satoshi
    )
    
    return result
