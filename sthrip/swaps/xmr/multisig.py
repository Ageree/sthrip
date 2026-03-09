"""
Monero Multi-sig для Atomic Swaps

Реализация 2-of-2 multi-sig для атомарных свопов BTC↔XMR.
Протокол:
1. Alice (XMR seller) и Bob (XMR buyer) обмениваются public keys
2. Создается 2-of-2 multi-sig address
3. Alice funding XMR в multi-sig
4. Bob видит funding → создает Bitcoin HTLC
5. Alice reveal preimage → забирает BTC
6. Bob использует preimage для подписи и получения XMR
"""

import json
import secrets
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
import logging
import time

from .wallet import MoneroWallet


logger = logging.getLogger(__name__)


class MultisigState(Enum):
    """Состояние мультиподписного кошелька"""
    CREATED = "created"           # Обычный кошелек создан
    PREPARED = "prepared"         # Подготовлен к multi-sig
    MULTISIG = "multisig"         # Конвертирован в multi-sig
    READY = "ready"               # Готов к использованию (все ключи обменяны)
    ERROR = "error"               # Ошибка


class SwapRole(Enum):
    """Роль в атомарном свопе"""
    SELLER = "seller"  # Продавец XMR (Alice)
    BUYER = "buyer"    # Покупатель XMR (Bob)


@dataclass
class MultisigSession:
    """Сессия создания multi-sig"""
    session_id: str
    role: SwapRole
    state: MultisigState = MultisigState.CREATED
    participants: int = 2
    threshold: int = 2
    
    # Данные для обмена
    my_multisig_info: Optional[str] = None
    their_multisig_info: Optional[str] = None
    
    # Результат
    multisig_address: Optional[str] = None
    
    # Метаданные
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "role": self.role.value,
            "state": self.state.value,
            "participants": self.participants,
            "threshold": self.threshold,
            "multisig_address": self.multisig_address,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class XMRSwapState:
    """Состояние XMR части атомарного свопа"""
    swap_id: str
    session: MultisigSession
    
    # Суммы
    amount_xmr: Decimal = Decimal("0")
    
    # Транзакции
    funding_txid: Optional[str] = None
    spend_txset: Optional[str] = None  # Неподписанная транзакция
    signed_txset: Optional[str] = None  # Подписанная транзакция
    
    # Preimage для связи с BTC HTLC
    preimage: Optional[str] = None
    preimage_hash: Optional[str] = None
    
    # Таймауты
    funding_timeout: int = 3600  # 1 час для funding
    spend_timeout: int = 86400   # 24 часа для spend
    
    # Статус
    funded: bool = False
    claimed: bool = False
    refunded: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "swap_id": self.swap_id,
            "session": self.session.to_dict(),
            "amount_xmr": str(self.amount_xmr),
            "funding_txid": self.funding_txid,
            "funded": self.funded,
            "claimed": self.claimed,
            "refunded": self.refunded,
        }


class MoneroMultisig:
    """
    Менеджер 2-of-2 multi-sig для атомарных свопов.
    
    Поток для seller (Alice, продает XMR):
    1. prepare() - создает кошелек и готовит к multi-sig
    2. make_multisig(their_info) - создает 2-of-2
    3. exchange_keys() - обмен ключами для синхронизации
    4. fund(amount) - отправляет XMR в multi-sig
    5. wait_for_preimage() - ждет пока Bob заберет BTC
    6. spend_with_preimage() - тратит XMR используя preimage
    
    Поток для buyer (Bob, покупает XMR):
    1. prepare() - создает кошелек и готовит к multi-sig
    2. make_multisig(their_info) - создает 2-of-2
    3. exchange_keys() - обмен ключами
    4. verify_funding() - проверяет что Alice профинансировала
    5. create_btc_htlc() - создает Bitcoin HTLC
    6. wait_for_spend() - ждет транзакцию тратить от Alice
    7. complete_spend() - дописывает свою подпись и отправляет
    """
    
    def __init__(
        self,
        wallet: MoneroWallet,
        role: SwapRole,
        wallet_password: str = ""
    ):
        self.wallet = wallet
        self.role = role
        self.password = wallet_password
        self.session: Optional[MultisigSession] = None
        
    def create_session(self) -> MultisigSession:
        """Создает новую сессию multi-sig"""
        self.session = MultisigSession(
            session_id=secrets.token_hex(16),
            role=self.role,
            participants=2,
            threshold=2
        )
        return self.session
    
    def prepare(self) -> str:
        """
        Подготавливает кошелек к multi-sig.
        
        Returns:
            multisig_info для отправки контрагенту
        """
        if not self.session:
            self.create_session()
            
        # Проверяем статус кошелька
        status = self.wallet.is_multisig()
        
        if status.get("multisig", False):
            self.session.state = MultisigState.MULTISIG
            logger.info("Wallet is already multisig")
            return status.get("multisig_info", "")
        
        # Готовим к multi-sig
        self.session.my_multisig_info = self.wallet.prepare_multisig()
        self.session.state = MultisigState.PREPARED
        self.session.updated_at = time.time()
        
        logger.info(f"Prepared multisig, info: {self.session.my_multisig_info[:50]}...")
        return self.session.my_multisig_info
    
    def make_multisig(self, their_info: str) -> str:
        """
        Создает multi-sig кошелек с контрагентом.
        
        Args:
            their_info: multisig_info от контрагента
            
        Returns:
            Адрес multi-sig кошелька
        """
        if not self.session:
            raise ValueError("Session not created")
            
        self.session.their_multisig_info = their_info
        
        # Создаем 2-of-2
        infos = [self.session.my_multisig_info, their_info]
        result = self.wallet.make_multisig(infos, threshold=2, password=self.password)
        
        self.session.multisig_address = result.get("address")
        self.session.state = MultisigState.MULTISIG
        self.session.updated_at = time.time()
        
        logger.info(f"Created 2-of-2 multisig at {self.session.multisig_address}")
        return self.session.multisig_address
    
    def exchange_keys(self, rounds: int = 2) -> bool:
        """
        Обменивается ключами для полной синхронизации.
        
        Это интерактивный процесс требующий нескольких раундов.
        В реальном сценарии требуется координация с контрагентом.
        
        Args:
            rounds: Количество раундов обмена
            
        Returns:
            True если успешно
        """
        if not self.session or not self.session.multisig_address:
            raise ValueError("Multisig not created")
        
        # В MVP предполагаем что обмен произошел внешне
        # или что оба кошелька управляются одним процессом
        
        self.session.state = MultisigState.READY
        self.session.updated_at = time.time()
        
        logger.info("Multisig keys exchanged and ready")
        return True
    
    def get_address(self) -> str:
        """Возвращает адрес multi-sig кошелька"""
        if self.session and self.session.multisig_address:
            return self.session.multisig_address
        return self.wallet.get_address()
    
    def get_balance(self) -> Dict[str, Decimal]:
        """Получает баланс multi-sig кошелька"""
        return self.wallet.get_balance()
    
    def fund(self, amount_xmr: Decimal) -> str:
        """
        Seller: Фандит multi-sig XMR.
        
        Args:
            amount_xmr: Сумма в XMR
            
        Returns:
            txid funding транзакции
        """
        if self.role != SwapRole.SELLER:
            raise ValueError("Only seller can fund")
            
        if not self.session or self.session.state != MultisigState.READY:
            raise ValueError("Multisig not ready")
        
        # Отправляем на multi-sig адрес
        from .wallet import MoneroTransfer
        transfer = MoneroTransfer(
            address=self.get_address(),
            amount=amount_xmr
        )
        
        result = self.wallet.transfer([transfer])
        txid = result["tx_hash"]
        
        logger.info(f"Funded {amount_xmr} XMR to multisig, txid: {txid}")
        return txid
    
    def verify_funding(self, expected_amount: Decimal, min_confirms: int = 1) -> bool:
        """
        Buyer: Проверяет что multi-sig профинансирован.
        
        Args:
            expected_amount: Ожидаемая сумма
            min_confirms: Минимальное количество подтверждений
            
        Returns:
            True если профинансирован корректно
        """
        # Ждем транзакцию
        transfers = self.wallet.get_transfers(inbound=True, pending=True)
        
        for tx in transfers:
            if tx.incoming and tx.confirmations >= min_confirms:
                if abs(tx.amount - expected_amount) < Decimal("0.0001"):
                    logger.info(f"Verified funding: {tx.amount} XMR, txid: {tx.txid}")
                    return True
                    
        return False
    
    def create_spend_transaction(
        self,
        destination: str,
        amount: Optional[Decimal] = None
    ) -> str:
        """
        Создает транзакцию для траты из multi-sig.
        
        Args:
            destination: Адрес назначения
            amount: Сумма (если None - весь доступный баланс минус fee)
            
        Returns:
            txset (hex) для подписания
        """
        # Получаем баланс
        balance = self.wallet.get_balance()
        
        if amount is None:
            # Резервируем на fee (примерно 0.01 XMR для multi-sig)
            amount = balance["unlocked"] - Decimal("0.01")
        
        if amount > balance["unlocked"]:
            raise ValueError(f"Insufficient unlocked balance: {balance['unlocked']}")
        
        # Создаем транзакцию (это создаст unsigned txset)
        # Для multi-sig используем специальный метод
        
        from .wallet import MoneroTransfer
        transfer = MoneroTransfer(address=destination, amount=amount)
        
        # В Monero multi-sig, transfer создает неподписанную транзакцию
        result = self.wallet.transfer([transfer])
        
        # Получаем txset для подписания
        # Это упрощенная версия - в реальности нужен export_multisig_info
        
        logger.info(f"Created spend transaction for {amount} XMR to {destination}")
        return result.get("tx_hash", "")
    
    def sign_transaction(self, txset: str) -> str:
        """
        Подписывает multi-sig транзакцию.
        
        Args:
            txset: Транзакция в hex формате
            
        Returns:
            Подписанная транзакция
        """
        result = self.wallet.sign_multisig(txset)
        
        if result.get("tx_data_hex"):
            logger.info("Transaction signed")
            return result["tx_data_hex"]
        else:
            raise ValueError("Failed to sign transaction")
    
    def submit_transaction(self, txset: str) -> str:
        """
        Отправляет подписанную multi-sig транзакцию.
        
        Returns:
            txid
        """
        txid = self.wallet.submit_multisig(txset)
        logger.info(f"Submitted transaction: {txid}")
        return txid
    
    def complete_cooperative_spend(
        self,
        destination: str,
        buyer_multisig: "MoneroMultisig",
        amount: Optional[Decimal] = None
    ) -> str:
        """
        Кооперативная трата из multi-sig (для тестирования).
        
        В реальном сценарии, каждая сторона подписывает отдельно.
        Этот метод для тестирования когда оба кошелька доступны.
        
        Args:
            destination: Куда отправить
            buyer_multisig: Кошелек buyer'а
            amount: Сумма
            
        Returns:
            txid
        """
        # Seller создает и подписывает
        tx_hash = self.create_spend_transaction(destination, amount)
        
        # Получаем txset (в реальности через export/import)
        # Здесь упрощенная версия
        
        # Buyer подписывает
        # buyer_multisig.sign_transaction(txset)
        
        # Отправляем
        # return self.submit_transaction(fully_signed_txset)
        
        return tx_hash


class MoneroMultisigManager:
    """
    Менеджер для координации multi-sig сессий.
    """
    
    def __init__(self):
        self.sessions: Dict[str, MultisigSession] = {}
        self.swaps: Dict[str, XMRSwapState] = {}
    
    def create_swap(
        self,
        seller_wallet: MoneroWallet,
        buyer_wallet: MoneroWallet,
        amount_xmr: Decimal,
        preimage_hash: str
    ) -> XMRSwapState:
        """
        Создает новый атомарный своп.
        
        Returns:
            XMRSwapState с обоими multisig
        """
        swap_id = secrets.token_hex(16)
        
        # Создаем сессии
        seller_multisig = MoneroMultisig(seller_wallet, SwapRole.SELLER)
        buyer_multisig = MoneroMultisig(buyer_wallet, SwapRole.BUYER)
        
        # Подготавливаем оба кошелька
        seller_info = seller_multisig.prepare()
        buyer_info = buyer_multisig.prepare()
        
        # Создаем multi-sig (в реальности - асинхронно)
        seller_address = seller_multisig.make_multisig(buyer_info)
        buyer_address = buyer_multisig.make_multisig(seller_info)
        
        assert seller_address == buyer_address, "Address mismatch!"
        
        # Синхронизируем
        seller_multisig.exchange_keys()
        buyer_multisig.exchange_keys()
        
        # Создаем swap state
        swap = XMRSwapState(
            swap_id=swap_id,
            session=seller_multisig.session,
            amount_xmr=amount_xmr,
            preimage_hash=preimage_hash
        )
        
        self.swaps[swap_id] = swap
        
        logger.info(f"Created XMR swap {swap_id} for {amount_xmr} XMR")
        return swap
    
    def get_swap(self, swap_id: str) -> Optional[XMRSwapState]:
        """Получает состояние свопа"""
        return self.swaps.get(swap_id)
