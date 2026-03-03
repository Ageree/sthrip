"""
Demo: Atomic Swap BTC↔XMR

Пример использования StealthPay для атомарного свопа.
Этот скрипт демонстрирует полный цикл свопа.

⚠️  WARNING: Это демо-скрипт. Для реального использования:
- Настройте Bitcoin Core и Monero ноды
- Используйте testnet/stagenet для тестирования
- Никогда не используйте mainnet без аудита
"""

import asyncio
from decimal import Decimal
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def demo_swap_flow():
    """
    Демонстрация потока атомарного свопа.
    """
    print("=" * 60)
    print("Atomic Swap BTC↔XMR Demo")
    print("=" * 60)
    
    # === Настройка ===
    print("\n1. Инициализация участников...")
    
    # Alice - продает XMR, покупает BTC
    print("   Alice (Seller): Отдает 1 XMR, получает 0.01 BTC")
    
    # Bob - покупает XMR, продает BTC
    print("   Bob (Buyer): Отдает 0.01 BTC, получает 1 XMR")
    
    # === Phase 1: XMR Multi-sig Setup ===
    print("\n2. Настройка XMR 2-of-2 Multi-sig...")
    print("   - Alice создает кошелек и готовит multisig_info_A")
    print("   - Bob создает кошелек и готовит multisig_info_B")
    print("   - Обмен multisig_info")
    print("   - Оба создают общий multisig адрес")
    print("   ✓ Multi-sig готов")
    
    # === Phase 2: XMR Funding ===
    print("\n3. Alice funding XMR в multi-sig...")
    print("   - Alice отправляет 1 XMR на multisig адрес")
    print("   - Ждем подтверждения (1-2 минуты)")
    print("   ✓ XMR профинансирован")
    
    # === Phase 3: Bitcoin HTLC ===
    print("\n4. Bob создает Bitcoin HTLC...")
    print("   - Bob генерирует preimage (secret)")
    print("   - Создает HTLC: hashlock(preimage) + timelock(24h)")
    print("   - Alice может claim: зная preimage")
    print("   - Bob может refund: после 24h")
    print("   - Bob funding 0.01 BTC в HTLC")
    print("   ✓ Bitcoin HTLC создан")
    
    # === Phase 4: Atomic Execution ===
    print("\n5. Атомарное исполнение...")
    print("   - Alice видит BTC в HTLC")
    print("   - Alice claim BTC, раскрывая preimage в Bitcoin tx")
    print("   - Bob видит preimage в Bitcoin blockchain")
    print("   - Bob использует preimage для подписи XMR spend")
    print("   - Bob забирает XMR из multi-sig")
    print("   ✓ Своп завершен!")
    
    # === Summary ===
    print("\n" + "=" * 60)
    print("Результат:")
    print("=" * 60)
    print("   Alice: -1 XMR, +0.01 BTC")
    print("   Bob:   -0.01 BTC, +1 XMR")
    print("\nАтомарность: либо оба получили, либо оба вернули свои средства")
    print("=" * 60)


async def demo_with_mock():
    """
    Демо с мок-объектами (без реальных нод).
    """
    print("\n" + "=" * 60)
    print("Demo with Mock Objects")
    print("=" * 60)
    
    from unittest.mock import Mock
    from stealthpay.swaps.coordinator import SwapCoordinator, SwapConfig, SwapFactory
    from stealthpay.swaps.btc.rpc_client import create_regtest_client
    from stealthpay.swaps.xmr.wallet import create_stagenet_wallet
    
    # Создаем мок-координаторы
    print("\nCreating coordinators...")
    
    # Конфигурация
    config = SwapConfig(
        btc_amount=Decimal("0.01"),
        xmr_amount=Decimal("1.0"),
        btc_network="regtest",
        xmr_network="stagenet"
    )
    
    # Alice (Seller)
    alice_btc_rpc = Mock()
    alice_btc_rpc.get_block_count.return_value = 1000
    
    alice_xmr = Mock()
    alice_xmr.get_address.return_value = "44...alice_xmr"
    
    alice = SwapFactory.create_seller_swap(
        alice_btc_rpc,
        alice_xmr,
        config.btc_amount,
        config.xmr_amount,
        receive_btc_address="bc1q...alice_btc",
        config=config
    )
    
    print(f"   Alice swap ID: {alice.state.swap_id[:16]}...")
    print(f"   Role: {alice.state.role.value}")
    print(f"   Selling: {alice.config.xmr_amount} XMR")
    print(f"   Buying: {alice.config.btc_amount} BTC")
    
    # Bob (Buyer)
    bob_btc_rpc = Mock()
    bob_btc_rpc.get_block_count.return_value = 1000
    bob_btc_rpc.fund_htlc_address.return_value = "funding_txid_123"
    
    bob_xmr = Mock()
    bob_xmr.get_address.return_value = "44...bob_xmr"
    
    bob = SwapFactory.create_buyer_swap(
        bob_btc_rpc,
        bob_xmr,
        config.btc_amount,
        config.xmr_amount,
        receive_xmr_address="44...bob_xmr_receive",
        config=config
    )
    
    print(f"\n   Bob swap ID: {bob.state.swap_id[:16]}...")
    print(f"   Role: {bob.state.role.value}")
    print(f"   Selling: {bob.config.btc_amount} BTC")
    print(f"   Buying: {bob.config.xmr_amount} XMR")
    
    # Демонстрация ключей
    print(f"\n   Alice BTC pubkey: {alice.state.btc_pubkey[:20]}...")
    print(f"   Bob BTC pubkey: {bob.state.btc_pubkey[:20]}...")
    
    print("\n✓ Coordinators initialized successfully")
    print("\nNote: Full swap execution requires running Bitcoin and Monero nodes.")
    print("      See setup instructions in documentation.")


def demo_swap_cli():
    """
    Демо CLI команды для свопа.
    """
    print("\n" + "=" * 60)
    print("CLI Commands Preview")
    print("=" * 60)
    
    print("""
# Создать своп как продавец XMR
stealthpay swap create-seller \\
    --btc-amount 0.01 \\
    --xmr-amount 1.0 \\
    --receive-btc bc1q...

# Создать своп как покупатель XMR  
stealthpay swap create-buyer \\
    --btc-amount 0.01 \\
    --xmr-amount 1.0 \\
    --receive-xmr 44...

# Настроить мультисиг
stealthpay swap setup-multisig --swap-id <id> --counterparty-info <info>

# Профинансировать XMR (продавец)
stealthpay swap fund-xmr --swap-id <id>

# Создать BTC HTLC (покупатель)
stealthpay swap create-btc-htlc --swap-id <id> --counterparty-pubkey <pubkey>

# Забрать BTC (продавец, раскрывает preimage)
stealthpay swap claim-btc --swap-id <id> --preimage <preimage>

# Забрать XMR (покупатель, использует preimage)
stealthpay swap claim-xmr --swap-id <id> --preimage <preimage>

# Статус свопа
stealthpay swap status --swap-id <id>
""")


if __name__ == "__main__":
    # Демо потока
    demo_swap_flow()
    
    # Демо с мок-объектами
    asyncio.run(demo_with_mock())
    
    # Демо CLI
    demo_swap_cli()
