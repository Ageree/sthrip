"""
Tests for Cross-Chain Bridge
"""

import pytest
from decimal import Decimal
from unittest.mock import Mock, MagicMock

from stealthpay.bridge.contracts.eth_bridge import EthereumBridgeContract, BridgeLock
from stealthpay.bridge.relayers.coordinator import (
    BridgeCoordinator,
    BridgeTransfer,
    BridgeDirection,
    BridgeTransferStatus,
    BridgeFeeCalculator
)
from stealthpay.bridge.relayers.mpc_node import MPCRelayerNode, TSSKeyGenerator, TSSKeyShare


class TestBridgeFeeCalculator:
    """Tests for fee calculation"""
    
    def test_eth_to_xmr_conversion(self):
        """Test ETH to XMR conversion with fees"""
        calculator = BridgeFeeCalculator(
            base_fee_eth=Decimal("0.001"),
            base_fee_xmr=Decimal("0.01"),
            percentage_fee=Decimal("0.001")  # 0.1%
        )
        
        # 1 ETH = 10 XMR
        xmr_amount, fee = calculator.calculate_eth_to_xmr(
            Decimal("1.0"),
            xmr_price_ratio=Decimal("10")
        )
        
        # Expected: 10 XMR - 0.1% fee
        assert xmr_amount == Decimal("9.99")
        assert fee == Decimal("0.01")
    
    def test_xmr_to_eth_conversion(self):
        """Test XMR to ETH conversion with fees"""
        calculator = BridgeFeeCalculator(
            base_fee_eth=Decimal("0.001"),
            base_fee_xmr=Decimal("0.01"),
            percentage_fee=Decimal("0.001")
        )
        
        # 1 XMR = 0.1 ETH
        eth_amount, fee = calculator.calculate_xmr_to_eth(
            Decimal("1.0"),
            eth_price_ratio=Decimal("0.1")
        )
        
        # Expected: 0.1 ETH - 0.1% fee = 0.0999 ETH
        assert eth_amount == Decimal("0.099")
        assert fee == Decimal("0.001")  # Base fee is higher than 0.1%
    
    def test_minimum_fee(self):
        """Test that minimum fee is applied"""
        calculator = BridgeFeeCalculator(
            base_fee_eth=Decimal("0.001"),
            base_fee_xmr=Decimal("0.01"),
            percentage_fee=Decimal("0.001")
        )
        
        # Small amount - should use base fee
        xmr_amount, fee = calculator.calculate_eth_to_xmr(
            Decimal("0.01"),  # 0.01 ETH
            xmr_price_ratio=Decimal("10")  # = 0.1 XMR
        )
        
        # Should use base fee (0.01 XMR) instead of percentage
        assert fee == Decimal("0.01")


class TestBridgeTransfer:
    """Tests for bridge transfers"""
    
    def test_create_transfer(self):
        """Test creating a bridge transfer"""
        transfer = BridgeTransfer(
            transfer_id="test_transfer_123",
            direction=BridgeDirection.ETH_TO_XMR,
            eth_amount=Decimal("0.1"),
            xmr_amount=Decimal("1.0"),
            eth_address="0x123...",
            xmr_address="44abc...",
            status=BridgeTransferStatus.PENDING
        )
        
        assert transfer.transfer_id == "test_transfer_123"
        assert transfer.direction == BridgeDirection.ETH_TO_XMR
        assert transfer.status == BridgeTransferStatus.PENDING
        assert transfer.created_at > 0


class TestTSSKeyGeneration:
    """Tests for TSS key generation"""
    
    def test_generate_key_shares(self):
        """Test generating TSS key shares"""
        shares = TSSKeyGenerator.generate_key_shares(n=5, threshold=3)
        
        assert len(shares) == 5
        
        for i, share in enumerate(shares, 1):
            assert share.node_id == f"mpc_node_{i}"
            assert share.index == i
            assert len(share.private_share) == 32
            assert len(share.public_key) == 32  # Hash output is 32 bytes
            assert len(share.group_public_key) == 32
    
    def test_key_share_serialization(self):
        """Test key share serialization"""
        shares = TSSKeyGenerator.generate_key_shares(n=3, threshold=2)
        
        share = shares[0]
        data = share.to_dict()
        
        assert data["node_id"] == "mpc_node_1"
        assert data["index"] == 1
        assert "public_key" in data
        assert "group_public_key" in data
        assert "private_share" not in data  # Should not expose private key


class TestMPCNode:
    """Tests for MPC relayer node"""
    
    def test_node_initialization(self):
        """Test MPC node initialization"""
        mock_bridge = Mock()
        mock_xmr = Mock()
        
        node = MPCRelayerNode(
            node_id="test_node",
            eth_bridge_contract=mock_bridge,
            xmr_wallet=mock_xmr
        )
        
        assert node.node_id == "test_node"
        assert node.status.value == "offline"
        assert node.threshold == 3
        assert node.total_nodes == 5
    
    def test_node_status(self):
        """Test getting node status"""
        mock_bridge = Mock()
        mock_xmr = Mock()
        
        node = MPCRelayerNode(
            node_id="test_node",
            eth_bridge_contract=mock_bridge,
            xmr_wallet=mock_xmr
        )
        
        status = node.get_status()
        
        assert status["node_id"] == "test_node"
        assert status["status"] == "offline"
        assert status["pending_requests"] == 0
        assert status["signed_requests"] == 0


class TestBridgeCoordinator:
    """Tests for bridge coordinator"""
    
    def test_coordinator_initialization(self):
        """Test coordinator initialization"""
        mock_bridge = Mock()
        
        coordinator = BridgeCoordinator(
            eth_bridge=mock_bridge,
            mpc_nodes=[]
        )
        
        assert coordinator.eth_bridge == mock_bridge
        assert len(coordinator.mpc_nodes) == 0
        assert len(coordinator.transfers) == 0
    
    def test_get_stats_empty(self):
        """Test getting stats with no transfers"""
        mock_bridge = Mock()
        
        coordinator = BridgeCoordinator(
            eth_bridge=mock_bridge,
            mpc_nodes=[]
        )
        
        stats = coordinator.get_stats()
        
        assert stats["total_transfers"] == 0
        assert stats["completed"] == 0
        assert stats["pending"] == 0
        assert stats["failed"] == 0
        assert stats["mpc_nodes_online"] == 0


@pytest.mark.asyncio
class TestBridgeOperations:
    """Tests for bridge operations"""
    
    async def test_bridge_eth_to_xmr(self):
        """Test bridging ETH to XMR"""
        mock_bridge = Mock()
        mock_bridge.lock.return_value = "lock_tx_123"
        
        coordinator = BridgeCoordinator(
            eth_bridge=mock_bridge,
            mpc_nodes=[]
        )
        
        transfer = await coordinator.bridge_eth_to_xmr(
            eth_amount=Decimal("0.1"),
            xmr_address="44test...",
            sender_eth_address="0xsender...",
            duration_hours=24
        )
        
        assert transfer.direction == BridgeDirection.ETH_TO_XMR
        assert transfer.eth_amount == Decimal("0.1")
        assert transfer.status == BridgeTransferStatus.ETH_LOCKED
        assert transfer.eth_lock_tx == "lock_tx_123"
        
        mock_bridge.lock.assert_called_once()
    
    async def test_bridge_xmr_to_eth(self):
        """Test bridging XMR to ETH"""
        mock_bridge = Mock()
        
        coordinator = BridgeCoordinator(
            eth_bridge=mock_bridge,
            mpc_nodes=[]
        )
        
        transfer = await coordinator.bridge_xmr_to_eth(
            xmr_amount=Decimal("1.0"),
            eth_address="0xreceiver...",
            sender_xmr_address="44sender..."
        )
        
        assert transfer.direction == BridgeDirection.XMR_TO_ETH
        assert transfer.xmr_amount == Decimal("1.0")
        assert transfer.eth_address == "0xreceiver..."
    
    async def test_list_transfers(self):
        """Test listing transfers"""
        mock_bridge = Mock()
        mock_bridge.lock.return_value = "lock_tx_123"
        
        coordinator = BridgeCoordinator(
            eth_bridge=mock_bridge,
            mpc_nodes=[]
        )
        
        # Create some transfers
        await coordinator.bridge_eth_to_xmr(
            eth_amount=Decimal("0.1"),
            xmr_address="44test...",
            sender_eth_address="0xsender..."
        )
        
        await coordinator.bridge_xmr_to_eth(
            xmr_amount=Decimal("1.0"),
            eth_address="0xreceiver...",
            sender_xmr_address="44sender..."
        )
        
        all_transfers = await coordinator.list_transfers()
        assert len(all_transfers) == 2
        
        pending = await coordinator.list_transfers(BridgeTransferStatus.PENDING)
        assert len(pending) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
