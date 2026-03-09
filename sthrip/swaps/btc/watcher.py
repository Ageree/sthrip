"""
Bitcoin Blockchain Watcher

Мониторинг HTLC транзакций:
- Отслеживание funding transactions
- Обнаружение claim/refund операций
- Уведомления о подтверждениях
"""

import asyncio
import logging
from typing import Optional, Callable, Dict, Any, List
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
import time


logger = logging.getLogger(__name__)


class HTLCStatus(Enum):
    """Статус HTLC контракта"""
    PENDING = "pending"           # Создан, ожидает funding
    FUNDED = "funded"             # Профинансирован, ожидает действия
    CLAIMED = "claimed"           # Claimed получателем
    REFUNDED = "refunded"         # Refunded отправителем
    EXPIRED = "expired"           # Timelock истек, доступен refund
    FAILED = "failed"             # Ошибка


@dataclass
class HTLCMonitorConfig:
    """Конфигурация мониторинга HTLC"""
    check_interval: int = 30      # Интервал проверки в секундах
    confirmations_required: int = 1  # Подтверждений для считания funded
    confirmations_final: int = 6     # Подтверждений для finality


@dataclass
class HTLCState:
    """Состояние HTLC контракта"""
    contract_id: str
    htlc_address: str
    status: HTLCStatus
    funding_txid: Optional[str] = None
    funding_vout: Optional[int] = None
    funding_amount: Optional[Decimal] = None
    claim_txid: Optional[str] = None
    refund_txid: Optional[str] = None
    confirmations: int = 0
    locktime: Optional[int] = None
    created_at: float = 0
    updated_at: float = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "htlc_address": self.htlc_address,
            "status": self.status.value,
            "funding_txid": self.funding_txid,
            "funding_vout": self.funding_vout,
            "funding_amount": str(self.funding_amount) if self.funding_amount else None,
            "claim_txid": self.claim_txid,
            "refund_txid": self.refund_txid,
            "confirmations": self.confirmations,
            "locktime": self.locktime,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class BitcoinWatcher:
    """
    Watcher для мониторинга Bitcoin HTLC контрактов.
    
    Поддерживает:
    - Асинхронный мониторинг через polling
    - Callback'и на изменение статуса
    - Сохранение состояния в памяти
    """
    
    def __init__(
        self,
        rpc_client: Any,  # BitcoinRPCClient
        config: Optional[HTLCMonitorConfig] = None
    ):
        self.rpc = rpc_client
        self.config = config or HTLCMonitorConfig()
        self.htlcs: Dict[str, HTLCState] = {}
        self._callbacks: Dict[HTLCStatus, List[Callable]] = {
            status: [] for status in HTLCStatus
        }
        self._running = False
        self._task: Optional[asyncio.Task] = None
        
    def register_callback(
        self,
        status: HTLCStatus,
        callback: Callable[[HTLCState], None]
    ) -> None:
        """Регистрирует callback на изменение статуса"""
        self._callbacks[status].append(callback)
        
    def unregister_callback(
        self,
        status: HTLCStatus,
        callback: Callable[[HTLCState], None]
    ) -> None:
        """Удаляет callback"""
        if callback in self._callbacks[status]:
            self._callbacks[status].remove(callback)
    
    def _trigger_callbacks(self, state: HTLCState) -> None:
        """Вызывает все callback'и для статуса"""
        for callback in self._callbacks.get(state.status, []):
            try:
                callback(state)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def add_htlc(
        self,
        contract_id: str,
        htlc_address: str,
        locktime: Optional[int] = None
    ) -> HTLCState:
        """Добавляет HTLC для мониторинга"""
        now = time.time()
        state = HTLCState(
            contract_id=contract_id,
            htlc_address=htlc_address,
            status=HTLCStatus.PENDING,
            locktime=locktime,
            created_at=now,
            updated_at=now
        )
        self.htlcs[contract_id] = state
        logger.info(f"Added HTLC {contract_id} for watching")
        return state
    
    def get_htlc(self, contract_id: str) -> Optional[HTLCState]:
        """Получает состояние HTLC"""
        return self.htlcs.get(contract_id)
    
    def _check_htlc_funding(self, state: HTLCState) -> bool:
        """Проверяет funding HTLC через address history"""
        try:
            # Получаем UTXOs для адреса
            # В реальности нужен индексер или scantxoutset
            # Здесь упрощенная версия
            
            # Пытаемся найти funding через scantxoutset
            result = self.rpc._call("scantxoutset", [
                "start",
                [f"addr({state.htlc_address})"]
            ])
            
            if result and result.get("success") and result.get("unspents"):
                unspent = result["unspents"][0]
                
                state.funding_txid = unspent["txid"]
                state.funding_vout = unspent["vout"]
                state.funding_amount = Decimal(str(unspent["amount"]))
                state.confirmations = unspent.get("confirmations", 0)
                
                if state.confirmations >= self.config.confirmations_required:
                    state.status = HTLCStatus.FUNDED
                    return True
                    
            return False
            
        except Exception as e:
            logger.error(f"Error checking funding: {e}")
            return False
    
    def _check_htlc_spent(self, state: HTLCState) -> bool:
        """Проверяет, был ли потрачен HTLC (claim/refund)"""
        if not state.funding_txid:
            return False
            
        try:
            # Проверяем, есть ли UTXO еще
            result = self.rpc._call("scantxoutset", [
                "start",
                [f"addr({state.htlc_address})"]
            ])
            
            if result and result.get("success"):
                if not result.get("unspents"):
                    # HTLC потрачен - нужно определить как
                    # Это требует анализа транзакций
                    pass
                    
            return False
            
        except Exception as e:
            logger.error(f"Error checking spent: {e}")
            return False
    
    def _check_timelock(self, state: HTLCState) -> bool:
        """Проверяет, истек ли timelock"""
        if not state.locktime:
            return False
            
        try:
            current_height = self.rpc.get_block_count()
            return current_height >= state.locktime
        except Exception as e:
            logger.error(f"Error checking timelock: {e}")
            return False
    
    def _update_htlc(self, state: HTLCState) -> None:
        """Обновляет состояние HTLC"""
        old_status = state.status
        
        # Проверяем разные статусы
        if state.status == HTLCStatus.PENDING:
            if self._check_htlc_funding(state):
                logger.info(f"HTLC {state.contract_id} funded")
                
        elif state.status == HTLCStatus.FUNDED:
            # Проверяем timelock
            if self._check_timelock(state):
                state.status = HTLCStatus.EXPIRED
                logger.info(f"HTLC {state.contract_id} expired")
                
            # Проверяем spent (claim/refund)
            elif self._check_htlc_spent(state):
                # Определяем тип траты требует доп. анализа
                pass
                
        state.updated_at = time.time()
        
        # Триггерим callback если статус изменился
        if state.status != old_status:
            self._trigger_callbacks(state)
    
    async def _watch_loop(self) -> None:
        """Главный цикл мониторинга"""
        while self._running:
            for state in list(self.htlcs.values()):
                try:
                    self._update_htlc(state)
                except Exception as e:
                    logger.error(f"Error updating HTLC {state.contract_id}: {e}")
                    
            await asyncio.sleep(self.config.check_interval)
    
    async def start(self) -> None:
        """Запускает мониторинг"""
        if self._running:
            return
            
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("Bitcoin watcher started")
    
    async def stop(self) -> None:
        """Останавливает мониторинг"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Bitcoin watcher stopped")
    
    def wait_for_funding(
        self,
        contract_id: str,
        timeout: int = 3600
    ) -> Optional[HTLCState]:
        """
        Синхронное ожидание funding (блокирующий вызов).
        
        Args:
            contract_id: ID контракта
            timeout: Таймаут в секундах
            
        Returns:
            HTLCState или None если таймаут
        """
        start = time.time()
        
        while time.time() - start < timeout:
            state = self.get_htlc(contract_id)
            if state and state.status in [HTLCStatus.FUNDED, HTLCStatus.CLAIMED]:
                return state
                
            time.sleep(self.config.check_interval)
            
        return None


class SimpleHTLCWatcher:
    """
    Упрощенный watcher для одного HTLC.
    Блокирующий интерфейс для простых случаев.
    """
    
    def __init__(self, rpc_client: Any):
        self.rpc = rpc_client
        
    def wait_for_funding(
        self,
        htlc_address: str,
        expected_amount: Optional[Decimal] = None,
        confirmations: int = 1,
        timeout: int = 3600,
        check_interval: int = 30
    ) -> Optional[Dict[str, Any]]:
        """
        Ожидает funding HTLC.
        
        Returns:
            Dict с txid, amount, confirmations или None
        """
        logger.info(f"Waiting for funding on {htlc_address}")
        start = time.time()
        
        while time.time() - start < timeout:
            try:
                result = self.rpc._call("scantxoutset", [
                    "start",
                    [f"addr({htlc_address})"]
                ])
                
                if result and result.get("success") and result.get("unspents"):
                    unspent = result["unspents"][0]
                    confs = unspent.get("confirmations", 0)
                    amount = Decimal(str(unspent["amount"]))
                    
                    if confs >= confirmations:
                        if expected_amount is None or abs(amount - expected_amount) < Decimal("0.00001"):
                            return {
                                "txid": unspent["txid"],
                                "vout": unspent["vout"],
                                "amount": amount,
                                "confirmations": confs,
                                "height": unspent.get("height")
                            }
                            
            except Exception as e:
                logger.error(f"Error checking funding: {e}")
                
            time.sleep(check_interval)
            
        logger.warning(f"Timeout waiting for funding on {htlc_address}")
        return None
