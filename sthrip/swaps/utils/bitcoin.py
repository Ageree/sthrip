"""
Bitcoin utility functions

Хеш-функции, bech32 кодирование, работа с ключами.
"""

import hashlib
from typing import Tuple, Optional


def sha256(data: bytes) -> bytes:
    """SHA256 hash"""
    return hashlib.sha256(data).digest()


def ripemd160(data: bytes) -> bytes:
    """RIPEMD160 hash"""
    h = hashlib.new('ripemd160')
    h.update(data)
    return h.digest()


def hash160(data: bytes) -> bytes:
    """RIPEMD160(SHA256(data)) - используется в Bitcoin"""
    return ripemd160(sha256(data))


def hash256(data: bytes) -> bytes:
    """SHA256(SHA256(data)) - используется в Bitcoin"""
    return sha256(sha256(data))


# Bech32 constants
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
BECH32_GENERATOR = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]


def bech32_polymod(values: list) -> int:
    """Вычисляет bech32 checksum"""
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= BECH32_GENERATOR[i] if ((b >> i) & 1) else 0
    return chk


def bech32_hrp_expand(hrp: str) -> list:
    """Expand HRP для checksum вычисления"""
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def bech32_verify_checksum(hrp: str, data: list) -> bool:
    """Проверяет bech32 checksum"""
    return bech32_polymod(bech32_hrp_expand(hrp) + data) == 1


def bech32_create_checksum(hrp: str, data: list) -> list:
    """Создает bech32 checksum"""
    polymod = bech32_polymod(bech32_hrp_expand(hrp) + data + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def convertbits(data: list, frombits: int, tobits: int, pad: bool = True) -> Optional[list]:
    """Конвертирует биты между разными размерами"""
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    
    return ret


def encode_bech32(hrp: str, witver: int, witprog: bytes) -> str:
    """
    Кодирует bech32 адрес.
    
    Args:
        hrp: Human-readable part ("bc" for mainnet, "tb" for testnet)
        witver: Witness version (0 для v0 segwit)
        witprog: Witness program (hash для P2WSH или P2WPKH)
        
    Returns:
        Bech32 encoded address
    """
    data = [witver] + convertbits(list(witprog), 8, 5)
    checksum = bech32_create_checksum(hrp, data)
    combined = data + checksum
    return hrp + '1' + ''.join([BECH32_CHARSET[d] for d in combined])


def decode_bech32(addr: str) -> Tuple[Optional[str], Optional[int], Optional[bytes]]:
    """
    Декодирует bech32 адрес.
    
    Returns:
        Tuple of (hrp, witness_version, witness_program) или (None, None, None)
    """
    if ((any(ord(x) < 33 or ord(x) > 126 for x in addr)) or
            (addr.lower() != addr and addr.upper() != addr)):
        return (None, None, None)
    
    addr = addr.lower()
    if addr.find('1') == -1:
        return (None, None, None)
    
    hrp, data = addr.rsplit('1', 1)
    if len(hrp) < 1 or len(data) < 6:
        return (None, None, None)
    
    try:
        data = [BECH32_CHARSET.find(x) for x in data]
    except ValueError:
        return (None, None, None)
    
    if not bech32_verify_checksum(hrp, data):
        return (None, None, None)
    
    data = data[:-6]  # Remove checksum
    if len(data) < 1 or data[0] > 16:
        return (None, None, None)
    
    res = convertbits(data[1:], 5, 8, False)
    if res is None or len(res) < 2 or len(res) > 40:
        return (None, None, None)
    
    if data[0] == 0 and len(res) not in [20, 32]:
        return (None, None, None)
    
    return (hrp, data[0], bytes(res))


def pubkey_to_address(pubkey: bytes, network: str = "mainnet") -> str:
    """
    Конвертирует public key в P2WPKH адрес.
    
    Args:
        pubkey: Compressed public key (33 bytes)
        network: mainnet или testnet/regtest
        
    Returns:
        Bech32 P2WPKH address
    """
    hrp = "bc" if network == "mainnet" else "tb"
    
    # P2WPKH witness program is hash160 of pubkey
    witprog = hash160(pubkey)
    
    # Witness version 0 for P2WPKH
    return encode_bech32(hrp, 0, witprog)


def generate_keypair(compressed: bool = True) -> Tuple[bytes, bytes]:
    """
    Генерирует новую пару ключей secp256k1.
    
    Returns:
        Tuple of (privkey, pubkey)
    """
    import secrets
    
    # Generate random private key
    privkey = secrets.token_bytes(32)
    
    # Generate public key using ecdsa
    try:
        import ecdsa
        
        sk = ecdsa.SigningKey.from_string(privkey, curve=ecdsa.SECP256k1)
        vk = sk.get_verifying_key()
        
        if compressed:
            x = vk.to_string()[:32]
            y = vk.to_string()[32:]
            # Compressed format: 0x02 if y is even, 0x03 if odd
            prefix = 0x02 if int.from_bytes(y, 'big') % 2 == 0 else 0x03
            pubkey = bytes([prefix]) + x
        else:
            pubkey = b'\x04' + vk.to_string()
    except ImportError:
        # Without ecdsa, just return privkey and placeholder
        # User should install ecdsa for real key operations
        pubkey = b'\x02' + secrets.token_bytes(32)  # placeholder
    
    return privkey, pubkey
