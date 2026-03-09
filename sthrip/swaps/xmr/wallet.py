"""
Monero Wallet RPC Client

Подключение к Monero wallet RPC для:
- Создания и управления кошельками
- Работы с multi-sig
- Мониторинга транзакций
- Отправки/получения XMR
"""

import json
import secrets
from typing import Optional, Dict, Any, List, Tuple
import requests
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class MoneroTransaction:
    """Monero транзакция"""
    txid: str
    amount: Decimal
    fee: Decimal
    confirmations: int
    timestamp: int
    incoming: bool
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MoneroTransaction":
        return cls(
            txid=data["txid"],
            amount=Decimal(str(data["amount"])) / Decimal("1e12"),  # atomic to XMR
            fee=Decimal(str(data.get("fee", 0))) / Decimal("1e12"),
            confirmations=data.get("confirmations", 0),
            timestamp=data.get("timestamp", 0),
            incoming=data.get("type", "in") == "in"
        )


@dataclass
class MoneroTransfer:
    """Monero transfer данные"""
    address: str
    amount: Decimal
    
    def to_rpc_dict(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "amount": int(self.amount * Decimal("1e12"))  # XMR to atomic
        }


class MoneroWallet:
    """
    Клиент для Monero wallet RPC.
    
    Требует запущенного monero-wallet-rpc с:
    - --wallet-dir
    - --rpc-bind-port
    - --rpc-login user:pass (опционально)
    - --daemon-address (для подключения к monerod)
    """
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 18082,  # Default mainnet wallet RPC port
        username: Optional[str] = None,
        password: Optional[str] = None,
        wallet_name: Optional[str] = None
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.wallet_name = wallet_name
        self.url = f"http://{host}:{port}/json_rpc"
        self.session = requests.Session()
        
        if username and password:
            self.session.auth = (username, password)
            
        self.session.headers.update({
            "Content-Type": "application/json"
        })
    
    def _call(self, method: str, params: Optional[Dict] = None) -> Any:
        """Выполняет JSON-RPC вызов"""
        payload = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(4),
            "method": method,
            "params": params or {}
        }
        
        try:
            response = self.session.post(self.url, json=payload)
            response.raise_for_status()
            result = response.json()
            
            if "error" in result:
                raise MoneroRPCError(
                    f"RPC Error: {result['error']}"
                )
            
            return result.get("result", {})
            
        except requests.RequestException as e:
            raise MoneroRPCError(f"Request failed: {e}")
    
    # ============ Wallet Management ============
    
    def create_wallet(
        self,
        name: str,
        password: str,
        language: str = "English"
    ) -> None:
        """Создает новый кошелек"""
        self._call("create_wallet", {
            "filename": name,
            "password": password,
            "language": language
        })
        self.wallet_name = name
    
    def open_wallet(self, name: str, password: str) -> None:
        """Открывает существующий кошелек"""
        self._call("open_wallet", {
            "filename": name,
            "password": password
        })
        self.wallet_name = name
    
    def close_wallet(self) -> None:
        """Закрывает текущий кошелек"""
        self._call("close_wallet")
        self.wallet_name = None
    
    def get_address(self, account_index: int = 0) -> str:
        """Получает основной адрес кошелька"""
        result = self._call("get_address", {
            "account_index": account_index
        })
        return result["address"]
    
    def get_balance(self, account_index: int = 0) -> Dict[str, Decimal]:
        """Получает баланс"""
        result = self._call("get_balance", {
            "account_index": account_index
        })
        return {
            "balance": Decimal(str(result["balance"])) / Decimal("1e12"),
            "unlocked": Decimal(str(result["unlocked_balance"])) / Decimal("1e12")
        }
    
    # ============ Transactions ============
    
    def transfer(
        self,
        destinations: List[MoneroTransfer],
        priority: int = 0,  # 0=default, 1=unimportant, 2=normal, 3=elevated, 4=priority
        mixin: int = 10,    # Ring size - 1
        account_index: int = 0
    ) -> Dict[str, Any]:
        """
        Отправляет XMR.
        
        Returns:
            Dict с tx_hash, tx_key, amount, fee
        """
        dests = [d.to_rpc_dict() for d in destinations]
        
        result = self._call("transfer", {
            "destinations": dests,
            "priority": priority,
            "ring_size": mixin + 1,
            "account_index": account_index
        })
        
        return {
            "tx_hash": result["tx_hash"],
            "tx_key": result["tx_key"],
            "amount": Decimal(str(result["amount"])) / Decimal("1e12"),
            "fee": Decimal(str(result["fee"])) / Decimal("1e12")
        }
    
    def transfer_split(
        self,
        destinations: List[MoneroTransfer],
        priority: int = 0,
        mixin: int = 10,
        account_index: int = 0
    ) -> List[Dict[str, Any]]:
        """Отправляет XMR с разделением на несколько транзакций если нужно"""
        dests = [d.to_rpc_dict() for d in destinations]
        
        result = self._call("transfer_split", {
            "destinations": dests,
            "priority": priority,
            "ring_size": mixin + 1,
            "account_index": account_index
        })
        
        return result.get("tx_hash_list", [])
    
    def get_transfers(
        self,
        inbound: bool = True,
        outbound: bool = True,
        pending: bool = False,
        failed: bool = False,
        pool: bool = False,
        account_index: int = 0
    ) -> List[MoneroTransaction]:
        """Получает список транзакций"""
        result = self._call("get_transfers", {
            "in": inbound,
            "out": outbound,
            "pending": pending,
            "failed": failed,
            "pool": pool,
            "account_index": account_index
        })
        
        transfers = []
        for tx_type in ["in", "out", "pending", "failed", "pool"]:
            if tx_type in result:
                for tx_data in result[tx_type]:
                    transfers.append(MoneroTransaction.from_dict(tx_data))
                    
        return transfers
    
    def get_transfer_by_txid(self, txid: str) -> Optional[MoneroTransaction]:
        """Получает транзакцию по ID"""
        try:
            result = self._call("get_transfer_by_txid", {"txid": txid})
            if "transfer" in result:
                return MoneroTransaction.from_dict(result["transfer"])
        except MoneroRPCError:
            pass
        return None
    
    # ============ Multi-sig Preparation ============
    
    def is_multisig(self) -> Dict[str, Any]:
        """Проверяет, является ли кошелек multi-sig"""
        return self._call("is_multisig")
    
    def prepare_multisig(self) -> str:
        """
        Подготавливает кошелек к созданию multi-sig.
        Returns multisig info string для обмена с другими участниками.
        """
        result = self._call("prepare_multisig")
        return result["multisig_info"]
    
    def make_multisig(
        self,
        multisig_info: List[str],
        threshold: int,
        password: str
    ) -> Dict[str, Any]:
        """
        Создает multi-sig кошелек.
        
        Args:
            multisig_info: Список multisig_info от всех участников
            threshold: Требуемое количество подписей (M в M-of-N)
            password: Пароль для кошелька
            
        Returns:
            Dict с address и multisig_info (для отправки другим)
        """
        result = self._call("make_multisig", {
            "multisig_info": multisig_info,
            "threshold": threshold,
            "password": password
        })
        return result
    
    def exchange_multisig_keys(
        self,
        multisig_info: List[str],
        password: str,
        force_update_use_with_caution: bool = False
    ) -> Dict[str, Any]:
        """
        Обменивается ключами для завершения настройки multi-sig.
        Может потребоваться несколько раундов.
        """
        result = self._call("exchange_multisig_keys", {
            "multisig_info": multisig_info,
            "password": password,
            "force_update_use_with_caution": force_update_use_with_caution
        })
        return result
    
    def finalize_multisig(self, multisig_info: List[str], password: str) -> str:
        """Завершает создание multi-sig кошелька"""
        result = self._call("finalize_multisig", {
            "multisig_info": multisig_info,
            "password": password
        })
        return result["address"]
    
    def export_multisig_info(self) -> str:
        """Экспортирует info для синхронизации с другими участниками"""
        result = self._call("export_multisig_info")
        return result["info"]
    
    def import_multisig_info(self, info: List[str]) -> Dict[str, int]:
        """Импортирует info от других участников"""
        result = self._call("import_multisig_info", {"info": info})
        return result
    
    def sign_multisig(self, tx_data_hex: str) -> Dict[str, Any]:
        """Подписывает multi-sig транзакцию"""
        result = self._call("sign_multisig", {"tx_data_hex": tx_data_hex})
        return result
    
    def submit_multisig(self, tx_data_hex: str) -> str:
        """Отправляет подписанную multi-sig транзакцию"""
        result = self._call("submit_multisig", {"tx_data_hex": tx_data_hex})
        return result["tx_hash_list"][0]
    
    def describe_transfer(self, unsigned_txset: str = "", multisig_txset: str = "") -> Dict[str, Any]:
        """Описывает содержимое транзакции перед подписанием"""
        params = {}
        if unsigned_txset:
            params["unsigned_txset"] = unsigned_txset
        if multisig_txset:
            params["multisig_txset"] = multisig_txset
        return self._call("describe_transfer", params)


class MoneroRPCError(Exception):
    """Ошибка RPC подключения к Monero"""
    pass


def create_stagenet_wallet(
    host: str = "localhost",
    port: int = 38082,  # Default stagenet wallet RPC port
    username: Optional[str] = None,
    password: Optional[str] = None
) -> MoneroWallet:
    """Создает клиент для Monero stagenet (для тестирования)"""
    return MoneroWallet(host, port, username, password)


def create_mainnet_wallet(
    host: str = "localhost",
    port: int = 18082,  # Default mainnet wallet RPC port
    username: Optional[str] = None,
    password: Optional[str] = None
) -> MoneroWallet:
    """Создает клиент для Monero mainnet"""
    return MoneroWallet(host, port, username, password)
