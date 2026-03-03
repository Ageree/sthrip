"""
Tests for Monero multisig implementation
"""

import pytest
from decimal import Decimal
from unittest.mock import Mock, MagicMock

from stealthpay.swaps.xmr.multisig import (
    MoneroMultisig,
    SwapRole,
    MultisigState,
    MoneroMultisigManager,
    XMRSwapState
)
from stealthpay.swaps.xmr.wallet import MoneroWallet


class TestMultisigSession:
    """Tests for multisig session management"""
    
    def test_create_session(self):
        """Тест создания сессии"""
        mock_wallet = Mock(spec=MoneroWallet)
        
        multisig = MoneroMultisig(mock_wallet, SwapRole.SELLER)
        session = multisig.create_session()
        
        assert session is not None
        assert session.role == SwapRole.SELLER
        assert session.participants == 2
        assert session.threshold == 2
        assert session.state == MultisigState.CREATED
    
    def test_prepare(self):
        """Тест подготовки multisig"""
        mock_wallet = Mock(spec=MoneroWallet)
        mock_wallet.is_multisig.return_value = {"multisig": False}
        mock_wallet.prepare_multisig.return_value = "multisig_info_data"
        
        multisig = MoneroMultisig(mock_wallet, SwapRole.SELLER)
        info = multisig.prepare()
        
        assert info == "multisig_info_data"
        assert multisig.session is not None
        assert multisig.session.state == MultisigState.PREPARED
        mock_wallet.prepare_multisig.assert_called_once()
    
    def test_make_multisig(self):
        """Тест создания multisig кошелька"""
        mock_wallet = Mock(spec=MoneroWallet)
        mock_wallet.is_multisig.return_value = {"multisig": False}
        mock_wallet.prepare_multisig.return_value = "my_info"
        mock_wallet.make_multisig.return_value = {
            "address": "44...multisig_address",
            "multisig_info": "result_info"
        }
        
        multisig = MoneroMultisig(mock_wallet, SwapRole.SELLER)
        multisig.prepare()
        
        address = multisig.make_multisig("their_info")
        
        assert address == "44...multisig_address"
        assert multisig.session.state == MultisigState.MULTISIG
        mock_wallet.make_multisig.assert_called_once_with(
            ["my_info", "their_info"],
            threshold=2,
            password=""
        )


class TestSellerFlow:
    """Tests for seller (Alice) flow"""
    
    def test_seller_fund(self):
        """Тест funding от продавца"""
        mock_wallet = Mock(spec=MoneroWallet)
        mock_wallet.is_multisig.return_value = {"multisig": False}
        mock_wallet.prepare_multisig.return_value = "my_info"
        mock_wallet.make_multisig.return_value = {
            "address": "44...multisig_address"
        }
        mock_wallet.transfer.return_value = {
            "tx_hash": "abc123...",
            "tx_key": "key123...",
            "amount": Decimal("1000000000000"),  # 1 XMR in atomic units
            "fee": Decimal("10000000000")
        }
        
        multisig = MoneroMultisig(mock_wallet, SwapRole.SELLER)
        multisig.prepare()
        multisig.make_multisig("buyer_info")
        multisig.exchange_keys()
        
        txid = multisig.fund(Decimal("1.0"))
        
        assert txid == "abc123..."
        mock_wallet.transfer.assert_called_once()
    
    def test_seller_cannot_fund_if_not_seller(self):
        """Тест что только seller может fund"""
        mock_wallet = Mock(spec=MoneroWallet)
        
        multisig = MoneroMultisig(mock_wallet, SwapRole.BUYER)
        multisig.session = Mock()
        multisig.session.state = MultisigState.READY
        
        with pytest.raises(ValueError, match="Only seller can fund"):
            multisig.fund(Decimal("1.0"))


class TestBuyerFlow:
    """Tests for buyer (Bob) flow"""
    
    def test_buyer_verify_funding(self):
        """Тест проверки funding покупателем"""
        mock_wallet = Mock(spec=MoneroWallet)
        mock_wallet.get_transfers.return_value = [
            Mock(
                txid="abc123",
                amount=Decimal("1.0"),
                confirmations=10,
                incoming=True
            )
        ]
        
        multisig = MoneroMultisig(mock_wallet, SwapRole.BUYER)
        multisig.session = Mock()
        
        result = multisig.verify_funding(Decimal("1.0"), min_confirms=1)
        
        assert result is True
        mock_wallet.get_transfers.assert_called_once()
    
    def test_buyer_verify_funding_wrong_amount(self):
        """Тест проверки funding с неверной суммой"""
        mock_wallet = Mock(spec=MoneroWallet)
        mock_wallet.get_transfers.return_value = [
            Mock(
                txid="abc123",
                amount=Decimal("0.5"),  # Wrong amount
                confirmations=10,
                incoming=True
            )
        ]
        
        multisig = MoneroMultisig(mock_wallet, SwapRole.BUYER)
        multisig.session = Mock()
        
        result = multisig.verify_funding(Decimal("1.0"), min_confirms=1)
        
        assert result is False


class TestMultisigManager:
    """Tests for MultisigManager"""
    
    def test_create_swap(self):
        """Тест создания свопа через менеджер"""
        seller_wallet = Mock(spec=MoneroWallet)
        buyer_wallet = Mock(spec=MoneroWallet)
        
        seller_wallet.is_multisig.return_value = {"multisig": False}
        seller_wallet.prepare_multisig.return_value = "seller_info"
        seller_wallet.make_multisig.return_value = {"address": "44..."}
        
        buyer_wallet.is_multisig.return_value = {"multisig": False}
        buyer_wallet.prepare_multisig.return_value = "buyer_info"
        buyer_wallet.make_multisig.return_value = {"address": "44..."}
        
        manager = MoneroMultisigManager()
        
        swap = manager.create_swap(
            seller_wallet,
            buyer_wallet,
            Decimal("1.0"),
            "preimage_hash_here"
        )
        
        assert swap is not None
        assert swap.swap_id is not None
        assert swap.amount_xmr == Decimal("1.0")
        assert swap.preimage_hash == "preimage_hash_here"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
