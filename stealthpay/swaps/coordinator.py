"""
Atomic Swap Coordinator

Координация полного цикла атомарного свопа BTC↔XMR:

┌──────────┐                    ┌──────────┐
│   Alice  │  ←── продает XMR   │   Bob    │
│ (Seller) │                    │  (Buyer) │
└────┬─────┘                    └────┬─────┘
     │                               │
     │  1. XMR Setup                 │
     │ ─────────────────────────────>│
     │  (2-of-2 multisig)            │
     │                               │
     │  2. XMR Funding               │
     │ ─────────────────────────────>│
     │  (Alice deposit XMR)          │
     │                               │
     │  3. Bitcoin HTLC              │
     │ <─────────────────────────────│
     │  (Bob creates HTLC)           │
     │                               │
     │  4. Preimage Reveal           │
     │ <─────────────────────────────│
     │  (Alice claims BTC)           │
     │                               │
     │  5. XMR Spend                 │
     │ ─────────────────────────────>│
     │  (Bob claims XMR)             │
     │                               │
"""

import asyncio
import logging
import secrets
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
import time

from .btc.htlc import BitcoinHTLC, create_simple_htlc_for_swap
from .btc.rpc_client import BitcoinRPCClient
from .btc.watcher import BitcoinWatcher, HTLCStatus as BtcHTLCStatus
from .xmr.multisig import MoneroMultisig, SwapRole, XMRSwapState, MoneroMultisigManager
from .xmr.wallet import MoneroWallet
from .utils.bitcoin import generate_keypair


logger = logging.getLogger(__name__)


class SwapPhase(Enum):
    """Фазы атомарного свопа"""
    INIT = auto()
    XMR_SETUP = auto()           # Настройка 2-of-2 multisig
    XMR_FUNDING = auto()         # Alice funding XMR
    BTC_HTLC_CREATED = auto()    # Bob создал Bitcoin HTLC
    BTC_CLAIMED = auto()         # Alice забрала BTC (revealed preimage)
    XMR_CLAIMED = auto()         # Bob забрал XMR
    COMPLETED = auto()
    REFUNDED = auto()
    FAILED = auto()


class SwapError(Exception):
    """Ошибка атомарного свопа"""
    pass


@dataclass
class SwapConfig:
    """Конфигурация свопа"""
    # Суммы
    btc_amount: Decimal = Decimal("0.01")
    xmr_amount: Decimal = Decimal("1.0")
    
    # Таймауты (в часах)
    xmr_funding_timeout: int = 1      # Alice должна профинансировать XMR
    btc_htlc_timeout: int = 1         # Bob должен создать HTLC
    btc_claim_timeout: int = 24       # Alice должна забрать BTC
    xmr_claim_timeout: int = 2        # Bob должен забрать XMR после preimage
    
    # Сети
    btc_network: str = "regtest"      # mainnet, testnet, regtest
    xmr_network: str = "stagenet"     # mainnet, stagenet, testnet


@dataclass
class AtomicSwapState:
    """Состояние атомарного свопа"""
    swap_id: str
    phase: SwapPhase
    
    # Участники
    role: SwapRole                  # Наша роль (SELLER или BUYER)
    
    # XMR часть
    xmr_state: Optional[XMRSwapState] = None
    xmr_multisig: Optional[MoneroMultisig] = None
    
    # BTC часть
    btc_htlc: Optional[Dict[str, Any]] = None
    btc_funding_txid: Optional[str] = None
    
    # Preimage (ключ к свопу)
    preimage: Optional[str] = None
    preimage_hash: Optional[str] = None
    
    # Адреса для получения
    our_btc_address: Optional[str] = None        # Куда получаем BTC
    our_xmr_address: Optional[str] = None        # Куда получаем XMR
    
    # Публичные ключи
    btc_pubkey: Optional[str] = None             # Наш BTC pubkey
    counterparty_btc_pubkey: Optional[str] = None
    counterparty_xmr_pubkey: Optional[str] = None
    
    # Таймауты
    created_at: float = field(default_factory=time.time)
    xmr_funding_deadline: Optional[float] = None
    btc_htlc_deadline: Optional[float] = None
    btc_claim_deadline: Optional[float] = None
    
    # Callbacks
    on_phase_change: Optional[Callable[[SwapPhase, SwapPhase], None]] = None
    
    def set_phase(self, new_phase: SwapPhase) -> None:
        """Устанавливает новую фазу с callback"""
        old_phase = self.phase
        self.phase = new_phase
        if self.on_phase_change:
            try:
                self.on_phase_change(old_phase, new_phase)
            except Exception as e:
                logger.error(f"Phase change callback error: {e}")


class SwapCoordinator:
    """
    Координатор атомарного свопа BTC↔XMR.
    
    Реализует полный цикл свопа для одной из сторон.
    
    Для продавца XMR (Alice):
    1. Инициализировать своп
    2. Настроить XMR multisig с покупателем
    3. Профинансировать XMR в multisig
    4. Ждать Bitcoin HTLC от покупателя
    5. Забрать BTC (revealing preimage)
    6. Покупатель забирает XMR
    
    Для покупателя XMR (Bob):
    1. Инициализировать своп
    2. Настроить XMR multisig с продавцом
    3. Ждать funding от продавца
    4. Создать Bitcoin HTLC
    5. Ждать пока продавец заберет BTC
    6. Забрать XMR из multisig
    """
    
    def __init__(
        self,
        btc_rpc: BitcoinRPCClient,
        xmr_wallet: MoneroWallet,
        config: SwapConfig = None
    ):
        self.btc_rpc = btc_rpc
        self.xmr_wallet = xmr_wallet
        self.config = config or SwapConfig()
        
        self.state: Optional[AtomicSwapState] = None
        self.btc_watcher: Optional[BitcoinWatcher] = None
        
        # Генерируем ключи для свопа
        self.btc_privkey, self.btc_pubkey = generate_keypair()
        
    def init_as_seller(
        self,
        btc_amount: Decimal,
        xmr_amount: Decimal,
        our_btc_address: str
    ) -> AtomicSwapState:
        """
        Инициализирует своп как продавец XMR (Alice).
        
        Args:
            btc_amount: Сколько BTC хотим получить
            xmr_amount: Сколько XMR отдаем
            our_btc_address: Куда получить BTC
            
        Returns:
            SwapState
        """
        self.state = AtomicSwapState(
            swap_id=secrets.token_hex(16),
            phase=SwapPhase.INIT,
            role=SwapRole.SELLER,
            our_btc_address=our_btc_address,
            btc_pubkey=self.btc_pubkey.hex()
        )
        
        logger.info(f"Initialized swap {self.state.swap_id} as SELLER")
        logger.info(f"  Giving: {xmr_amount} XMR")
        logger.info(f"  Want: {btc_amount} BTC")
        
        return self.state
    
    def init_as_buyer(
        self,
        btc_amount: Decimal,
        xmr_amount: Decimal,
        our_xmr_address: str
    ) -> AtomicSwapState:
        """
        Инициализирует своп как покупатель XMR (Bob).
        
        Args:
            btc_amount: Сколько BTC отдаем
            xmr_amount: Сколько XMR хотим получить
            our_xmr_address: Куда получить XMR
            
        Returns:
            SwapState
        """
        self.state = AtomicSwapState(
            swap_id=secrets.token_hex(16),
            phase=SwapPhase.INIT,
            role=SwapRole.BUYER,
            our_xmr_address=our_xmr_address,
            btc_pubkey=self.btc_pubkey.hex()
        )
        
        logger.info(f"Initialized swap {self.state.swap_id} as BUYER")
        logger.info(f"  Giving: {btc_amount} BTC")
        logger.info(f"  Want: {xmr_amount} XMR")
        
        return self.state
    
    async def setup_xmr_multisig(
        self,
        counterparty_multisig_info: str
    ) -> str:
        """
        Настраивает 2-of-2 XMR multisig с контрагентом.
        
        Args:
            counterparty_multisig_info: Информация о multisig от контрагента
            
        Returns:
            Наш multisig_info для отправки контрагенту
        """
        if not self.state:
            raise SwapError("Swap not initialized")
        
        self.state.set_phase(SwapPhase.XMR_SETUP)
        
        # Создаем multisig
        self.state.xmr_multisig = MoneroMultisig(
            self.xmr_wallet,
            self.state.role
        )
        
        # Подготавливаем
        our_info = self.state.xmr_multisig.prepare()
        
        # Создаем кошелек (в реальности нужен обмен)
        address = self.state.xmr_multisig.make_multisig(counterparty_multisig_info)
        
        # Синхронизируем
        self.state.xmr_multisig.exchange_keys()
        
        logger.info(f"XMR multisig setup at {address}")
        return our_info
    
    async def fund_xmr(self) -> str:
        """
        Продавец: Фандит XMR в multisig.
        
        Returns:
            txid funding транзакции
        """
        if not self.state or self.state.role != SwapRole.SELLER:
            raise SwapError("Only seller can fund XMR")
        
        if not self.state.xmr_multisig:
            raise SwapError("XMR multisig not set up")
        
        self.state.set_phase(SwapPhase.XMR_FUNDING)
        
        # Фандим
        txid = self.state.xmr_multisig.fund(self.config.xmr_amount)
        
        self.state.xmr_funding_deadline = time.time() + self.config.xmr_funding_timeout * 3600
        
        logger.info(f"Funded {self.config.xmr_amount} XMR, txid: {txid}")
        return txid
    
    async def verify_xmr_funding(
        self,
        min_confirms: int = 1,
        timeout: int = 3600
    ) -> bool:
        """
        Покупатель: Проверяет funding XMR.
        
        Returns:
            True если funding подтвержден
        """
        if not self.state or self.state.role != SwapRole.BUYER:
            raise SwapError("Only buyer can verify funding")
        
        start = time.time()
        while time.time() - start < timeout:
            if self.state.xmr_multisig.verify_funding(
                self.config.xmr_amount,
                min_confirms
            ):
                logger.info("XMR funding verified")
                return True
            await asyncio.sleep(30)
        
        return False
    
    async def create_btc_htlc(
        self,
        counterparty_pubkey: str,
        preimage_hash: str
    ) -> Dict[str, Any]:
        """
        Покупатель: Создает Bitcoin HTLC.
        
        Args:
            counterparty_pubkey: Pubkey продавца (который может claim по preimage)
            preimage_hash: Hash preimage
            
        Returns:
            HTLC contract details
        """
        if not self.state or self.state.role != SwapRole.BUYER:
            raise SwapError("Only buyer can create BTC HTLC")
        
        self.state.preimage_hash = preimage_hash
        self.state.counterparty_btc_pubkey = counterparty_pubkey
        
        # Создаем HTLC
        htlc = create_simple_htlc_for_swap(
            self.btc_rpc,
            self.state.btc_pubkey.hex(),  # Мы можем забрать refund
            counterparty_pubkey,           # Продавец может claim
            self.config.btc_amount,
            locktime_hours=self.config.btc_claim_timeout,
            network=self.config.btc_network
        )
        
        self.state.btc_htlc = htlc
        
        # Фандим HTLC
        txid = self.btc_rpc.fund_htlc_address(htlc["address"], self.config.btc_amount)
        self.state.btc_funding_txid = txid
        
        self.state.set_phase(SwapPhase.BTC_HTLC_CREATED)
        self.state.btc_htlc_deadline = time.time() + self.config.btc_htlc_timeout * 3600
        
        logger.info(f"Created BTC HTLC at {htlc['address']}")
        logger.info(f"  Amount: {self.config.btc_amount} BTC")
        logger.info(f"  Locktime: {htlc['locktime']} blocks")
        logger.info(f"  Funding txid: {txid}")
        
        return htlc
    
    async def wait_for_btc_htlc(
        self,
        timeout: int = 3600
    ) -> Optional[Dict[str, Any]]:
        """
        Продавец: Ожидает создание Bitcoin HTLC.
        
        Returns:
            HTLC details или None
        """
        if not self.state or self.state.role != SwapRole.SELLER:
            raise SwapError("Only seller can wait for BTC HTLC")
        
        # В MVP - просто polling или callback
        # В реальности - слушаем сеть или ждем сообщение от координатора
        
        logger.info("Waiting for BTC HTLC...")
        # TODO: Implement actual monitoring
        
        return None
    
    async def claim_btc(
        self,
        preimage: str,
        htlc_details: Dict[str, Any]
    ) -> str:
        """
        Продавец: Забирает BTC из HTLC, раскрывая preimage.
        
        Args:
            preimage: Preimage для разблокировки
            htlc_details: Детали HTLC
            
        Returns:
            txid claim транзакции
        """
        if not self.state or self.state.role != SwapRole.SELLER:
            raise SwapError("Only seller can claim BTC")
        
        self.state.preimage = preimage
        self.state.set_phase(SwapPhase.BTC_CLAIMED)
        
        # Здесь должна быть логика создания claim транзакции
        # Требует подписи и отправки в сеть
        
        logger.info(f"Claiming BTC with preimage: {preimage[:20]}...")
        
        # TODO: Implement actual claim
        return "txid_placeholder"
    
    async def claim_xmr(
        self,
        preimage: str
    ) -> str:
        """
        Покупатель: Забирает XMR из multisig.
        
        Args:
            preimage: Preimage (получен из Bitcoin claim)
            
        Returns:
            txid spend транзакции
        """
        if not self.state or self.state.role != SwapRole.BUYER:
            raise SwapError("Only buyer can claim XMR")
        
        self.state.preimage = preimage
        self.state.set_phase(SwapPhase.XMR_CLAIMED)
        
        # Используем preimage для авторизации траты
        # В реальном протоколе - более сложная криптография
        
        logger.info("Claiming XMR from multisig...")
        
        # TODO: Implement actual XMR claim
        return "txid_placeholder"
    
    async def execute_full_swap_as_seller(
        self,
        counterparty_multisig_info: str,
        counterparty_btc_pubkey: str
    ) -> bool:
        """
        Выполняет полный цикл свопа как продавец.
        
        Returns:
            True если успешно
        """
        try:
            # 1. Setup XMR multisig
            our_info = await self.setup_xmr_multisig(counterparty_multisig_info)
            # Отправляем our_info контрагенту...
            
            # 2. Fund XMR
            funding_txid = await self.fund_xmr()
            # Ждем подтверждения...
            
            # 3. Wait for BTC HTLC
            htlc = await self.wait_for_btc_htlc()
            if not htlc:
                raise SwapError("BTC HTLC not created in time")
            
            # 4. Claim BTC (reveals preimage)
            claim_txid = await self.claim_btc(self.state.preimage, htlc)
            
            # 5. Done - Bob will claim XMR
            self.state.set_phase(SwapPhase.COMPLETED)
            
            return True
            
        except Exception as e:
            logger.error(f"Swap failed: {e}")
            self.state.set_phase(SwapPhase.FAILED)
            return False
    
    async def execute_full_swap_as_buyer(
        self,
        counterparty_multisig_info: str
    ) -> bool:
        """
        Выполняет полный цикл свопа как покупатель.
        
        Returns:
            True если успешно
        """
        try:
            # 1. Setup XMR multisig
            our_info = await self.setup_xmr_multisig(counterparty_multisig_info)
            # Отправляем our_info контрагенту...
            
            # 2. Verify XMR funding
            if not await self.verify_xmr_funding():
                raise SwapError("XMR not funded in time")
            
            # 3. Create BTC HTLC
            # Генерируем preimage
            preimage = secrets.token_hex(32)
            preimage_hash = self._hash_preimage(preimage)
            self.state.preimage = preimage  # Сохраняем для позже
            
            htlc = await self.create_btc_htlc(
                counterparty_pubkey=self.state.counterparty_btc_pubkey,
                preimage_hash=preimage_hash
            )
            
            # 4. Wait for Alice to claim BTC (reveals preimage)
            # Мониторим Bitcoin сеть...
            
            # 5. Claim XMR with revealed preimage
            claim_txid = await self.claim_xmr(preimage)
            
            self.state.set_phase(SwapPhase.COMPLETED)
            return True
            
        except Exception as e:
            logger.error(f"Swap failed: {e}")
            self.state.set_phase(SwapPhase.FAILED)
            return False
    
    def _hash_preimage(self, preimage: str) -> str:
        """Хеширует preimage"""
        import hashlib
        return hashlib.sha256(bytes.fromhex(preimage)).hexdigest()


class SwapFactory:
    """
    Фабрика для создания сконфигурированных свопов.
    """
    
    @staticmethod
    def create_seller_swap(
        btc_rpc: BitcoinRPCClient,
        xmr_wallet: MoneroWallet,
        btc_amount: Decimal,
        xmr_amount: Decimal,
        receive_btc_address: str,
        config: Optional[SwapConfig] = None
    ) -> SwapCoordinator:
        """Создает координатор для продавца XMR"""
        coord = SwapCoordinator(btc_rpc, xmr_wallet, config)
        coord.init_as_seller(btc_amount, xmr_amount, receive_btc_address)
        return coord
    
    @staticmethod
    def create_buyer_swap(
        btc_rpc: BitcoinRPCClient,
        xmr_wallet: MoneroWallet,
        btc_amount: Decimal,
        xmr_amount: Decimal,
        receive_xmr_address: str,
        config: Optional[SwapConfig] = None
    ) -> SwapCoordinator:
        """Создает координатор для покупателя XMR"""
        coord = SwapCoordinator(btc_rpc, xmr_wallet, config)
        coord.init_as_buyer(btc_amount, xmr_amount, receive_xmr_address)
        return coord
