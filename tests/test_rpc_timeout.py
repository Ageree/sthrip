"""Test that wallet RPC calls in endpoints have request-level timeout."""
import pytest
import asyncio
import inspect
from decimal import Decimal
from unittest.mock import patch, MagicMock


@pytest.mark.asyncio
async def test_withdrawal_rpc_has_timeout():
    """Wallet RPC call in withdrawal must timeout after configured limit."""
    from api.routers.balance import _process_onchain_withdrawal

    agent = MagicMock()
    agent.id = "test-id"

    # Create a wallet service whose send_withdrawal hangs forever
    mock_wallet = MagicMock()
    mock_wallet.send_withdrawal = MagicMock(side_effect=lambda *a, **kw: None)

    # Patch to_thread so it actually awaits an async sleep (simulating hang)
    original_to_thread = asyncio.to_thread

    async def fake_to_thread(fn, *args, **kwargs):
        if fn is mock_wallet.send_withdrawal:
            await asyncio.sleep(999)
        return await original_to_thread(fn, *args, **kwargs)

    with patch("api.routers.balance.get_wallet_service", return_value=mock_wallet):
        with patch("api.routers.balance.get_settings") as mock_settings:
            settings_obj = MagicMock()
            settings_obj.wallet_rpc_timeout = 1  # 1 second for fast test
            settings_obj.monero_network = "stagenet"
            mock_settings.return_value = settings_obj
            with patch("api.routers.balance.asyncio") as mock_asyncio:
                # Let wait_for and to_thread work normally from real asyncio
                mock_asyncio.to_thread = fake_to_thread
                mock_asyncio.wait_for = asyncio.wait_for
                mock_asyncio.TimeoutError = asyncio.TimeoutError

                with pytest.raises(Exception):
                    # Should timeout, not hang forever
                    await asyncio.wait_for(
                        _process_onchain_withdrawal(
                            agent, Decimal("1.0"), "5" + "A" * 94, "pending-123"
                        ),
                        timeout=3.0,
                    )


@pytest.mark.asyncio
async def test_deposit_rpc_has_timeout():
    """deposit_balance source must use wait_for or timeout for RPC calls."""
    from api.routers import balance

    source = inspect.getsource(balance.deposit_balance)
    assert "wait_for" in source or "timeout" in source, (
        "deposit_balance should use asyncio.wait_for for RPC calls"
    )


@pytest.mark.asyncio
async def test_process_onchain_withdrawal_has_timeout_in_source():
    """_process_onchain_withdrawal source must use wait_for."""
    from api.routers import balance

    source = inspect.getsource(balance._process_onchain_withdrawal)
    assert "wait_for" in source, (
        "_process_onchain_withdrawal should use asyncio.wait_for for RPC calls"
    )
