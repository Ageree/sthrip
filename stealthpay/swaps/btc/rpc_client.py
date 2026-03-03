"""
Bitcoin Core RPC Client

Подключение к Bitcoin Core для создания и мониторинга HTLC транзакций.
Поддерживает mainnet, testnet и regtest.
"""

import hashlib
import json
import secrets
from typing import Optional, Dict, Any, List
import requests
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class HTLCConfig:
    """Конфигурация HTLC контракта"""
    hashlock: str  # SHA256 hash of preimage (hex)
    timelock: int  # Block height for refund
    sender_pubkey: str  # Bitcoin address of sender (Alice)
    recipient_pubkey: str  # Bitcoin address of recipient (Bob)
    amount: Decimal  # Amount in BTC


@dataclass
class SwapPreimage:
    """Preimage для разблокировки HTLC"""
    value: str  # 32 bytes hex
    
    @classmethod
    def generate(cls) -> "SwapPreimage":
        """Генерирует случайный preimage"""
        return cls(value=secrets.token_hex(32))
    
    def hash(self) -> str:
        """Возвращает SHA256 hash preimage"""
        return hashlib.sha256(bytes.fromhex(self.value)).hexdigest()


class BitcoinRPCClient:
    """
    Клиент для подключения к Bitcoin Core RPC.
    
    Требует настроенного bitcoin.conf с:
    - server=1
    - rpcuser=<username>
    - rpcpassword=<password>
    - rpcport=<port>
    """
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8332,
        username: str = "",
        password: str = "",
        network: str = "mainnet"
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.network = network
        self.url = f"http://{host}:{port}"
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update({
            "Content-Type": "application/json"
        })
        
    def _call(self, method: str, params: List[Any] = None) -> Any:
        """Выполняет RPC вызов"""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or [],
            "id": secrets.token_hex(4)
        }
        
        try:
            response = self.session.post(self.url, json=payload)
            response.raise_for_status()
            result = response.json()
            
            if "error" in result and result["error"]:
                raise BitcoinRPCError(
                    f"RPC Error {result['error']['code']}: {result['error']['message']}"
                )
            
            return result.get("result")
        except requests.RequestException as e:
            raise BitcoinRPCError(f"Request failed: {e}")
    
    # ============ Wallet Operations ============
    
    def get_balance(self, min_conf: int = 0) -> Decimal:
        """Получает баланс кошелька"""
        result = self._call("getbalance", ["*", min_conf])
        return Decimal(str(result))
    
    def get_new_address(self, label: str = "", address_type: str = "bech32") -> str:
        """Генерирует новый адрес"""
        return self._call("getnewaddress", [label, address_type])
    
    def send_to_address(
        self,
        address: str,
        amount: Decimal,
        comment: str = "",
        subtract_fee: bool = False
    ) -> str:
        """Отправляет BTC на адрес"""
        return self._call("sendtoaddress", [
            address,
            float(amount),
            comment,
            "",
            subtract_fee
        ])
    
    def get_transaction(self, txid: str) -> Dict[str, Any]:
        """Получает информацию о транзакции"""
        return self._call("gettransaction", [txid])
    
    def get_raw_transaction(self, txid: str, verbose: bool = True) -> Any:
        """Получает raw transaction"""
        return self._call("getrawtransaction", [txid, verbose])
    
    def get_block_count(self) -> int:
        """Получает текущую высоту блока"""
        return self._call("getblockcount")
    
    # ============ Raw Transaction Operations ============
    
    def create_raw_transaction(
        self,
        inputs: List[Dict],
        outputs: List[Dict]
    ) -> str:
        """Создает raw transaction"""
        return self._call("createrawtransaction", [inputs, outputs])
    
    def sign_raw_transaction_with_wallet(
        self,
        hex_string: str
    ) -> Dict[str, Any]:
        """Подписывает raw transaction кошельком"""
        return self._call("signrawtransactionwithwallet", [hex_string])
    
    def send_raw_transaction(self, hex_string: str) -> str:
        """Отправляет raw transaction в сеть"""
        return self._call("sendrawtransaction", [hex_string])
    
    def decode_raw_transaction(self, hex_string: str) -> Dict[str, Any]:
        """Декодирует raw transaction"""
        return self._call("decoderawtransaction", [hex_string])
    
    # ============ HTLC Helpers ============
    
    def fund_htlc_address(self, htlc_address: str, amount: Decimal) -> str:
        """
        Фандит HTLC адрес.
        Возвращает txid funding транзакции.
        """
        return self.send_to_address(htlc_address, amount)
    
    def get_htlc_script_hex(
        self,
        preimage_hash: str,
        sender_pubkey: str,
        recipient_pubkey: str,
        locktime: int
    ) -> str:
        """
        Создает HTLC script в hex формате.
        
        Script structure (P2SH):
        OP_IF
            OP_SHA256 <preimage_hash> OP_EQUALVERIFY
            <recipient_pubkey> OP_CHECKSIG
        OP_ELSE
            <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
            <sender_pubkey> OP_CHECKSIG
        OP_ENDIF
        """
        # Реализация в htlc.py
        from .htlc import create_htlc_redeem_script
        script = create_htlc_redeem_script(
            bytes.fromhex(preimage_hash),
            bytes.fromhex(recipient_pubkey),
            bytes.fromhex(sender_pubkey),
            locktime
        )
        return script.hex()
    
    def wait_for_confirmation(
        self,
        txid: str,
        confirmations: int = 1,
        timeout: int = 3600
    ) -> bool:
        """
        Ожидает подтверждения транзакции.
        
        Args:
            txid: ID транзакции
            confirmations: Количество подтверждений
            timeout: Таймаут в секундах
            
        Returns:
            True если подтверждена, False если таймаут
        """
        import time
        start = time.time()
        
        while time.time() - start < timeout:
            try:
                tx = self.get_transaction(txid)
                if tx.get("confirmations", 0) >= confirmations:
                    return True
            except BitcoinRPCError:
                pass  # Транзакция еще не в мемпуле
            
            time.sleep(10)  # Проверяем каждые 10 секунд
        
        return False


class BitcoinRPCError(Exception):
    """Ошибка RPC подключения"""
    pass


def create_regtest_client() -> BitcoinRPCClient:
    """
    Создает клиент для regtest (для тестирования).
    """
    return BitcoinRPCClient(
        host="localhost",
        port=18443,  # Default regtest RPC port
        username="bitcoin",
        password="bitcoin",
        network="regtest"
    )


def create_testnet_client(
    username: str = "",
    password: str = ""
) -> BitcoinRPCClient:
    """
    Создает клиент для testnet.
    """
    return BitcoinRPCClient(
        host="localhost",
        port=18332,  # Default testnet RPC port
        username=username,
        password=password,
        network="testnet"
    )
