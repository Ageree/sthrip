"""
StealthPay CLI - Bridge Commands

Commands for ETH↔XMR cross-chain bridge:
    bridge eth-to-xmr  - Bridge ETH to XMR
    bridge xmr-to-eth  - Bridge XMR to ETH
    bridge status      - Check transfer status
    bridge list        - List transfers
    bridge run-node    - Run MPC relayer node
"""

import os
import sys
import asyncio
from decimal import Decimal

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stealthpay.bridge.relayers.coordinator import BridgeCoordinator, BridgeTransferStatus
from stealthpay.bridge.contracts.eth_bridge import EthereumBridgeContract


def cmd_eth_to_xmr(args):
    """Bridge ETH to XMR"""
    print(f"\n🌉 Bridge ETH → XMR")
    print(f"   Amount: {args.amount} ETH")
    print(f"   XMR Address: {args.xmr_address[:20]}...")
    
    if args.network == "mainnet":
        print(f"\n   ⚠️  WARNING: Using MAINNET!")
        confirm = input("   Type 'yes' to confirm: ")
        if confirm != "yes":
            print("   Cancelled.")
            return
    
    try:
        # Create bridge contract
        private_key = os.getenv("ETH_PRIVATE_KEY")
        if not private_key:
            print("❌ ETH_PRIVATE_KEY not set")
            return
        
        bridge = EthereumBridgeContract(
            web3_provider=args.eth_rpc or os.getenv("ETH_RPC_URL", "http://localhost:8545"),
            contract_address=args.contract or os.getenv("BRIDGE_CONTRACT"),
            private_key=private_key
        )
        
        # Create coordinator
        coordinator = BridgeCoordinator(bridge, [])
        
        # Execute bridge
        async def bridge_async():
            sender_address = bridge._get_account_address()
            
            transfer = await coordinator.bridge_eth_to_xmr(
                eth_amount=Decimal(str(args.amount)),
                xmr_address=args.xmr_address,
                sender_eth_address=sender_address,
                duration_hours=args.duration
            )
            
            print(f"\n✅ Bridge initiated!")
            print(f"   Transfer ID: {transfer.transfer_id}")
            print(f"   Lock TX: {transfer.eth_lock_tx}")
            print(f"   Status: {transfer.status.value}")
            print(f"\n   Monitor with:")
            print(f"   stealthpay bridge status --transfer-id {transfer.transfer_id}")
            
            return transfer.transfer_id
        
        return asyncio.run(bridge_async())
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def cmd_xmr_to_eth(args):
    """Bridge XMR to ETH"""
    print(f"\n🌉 Bridge XMR → ETH")
    print(f"   Amount: {args.amount} XMR")
    print(f"   ETH Address: {args.eth_address[:20]}...")
    
    try:
        from stealthpay.swaps.xmr.wallet import MoneroWallet
        
        # Connect to XMR wallet
        xmr_wallet = MoneroWallet(
            host=args.xmr_host or "localhost",
            port=args.xmr_port or 18082
        )
        
        sender_address = xmr_wallet.get_address()
        
        print(f"\n   Your XMR address: {sender_address}")
        print(f"\n   To complete the bridge:")
        print(f"   1. Send {args.amount} XMR to the bridge address")
        print(f"   2. Wait for confirmations")
        print(f"   3. ETH will be sent to {args.eth_address}")
        
        print(f"\n   ⚠️  This is a simplified flow.")
        print(f"   Production requires MPC coordination.")
        
    except Exception as e:
        print(f"❌ Error: {e}")


def cmd_bridge_status(args):
    """Check bridge transfer status"""
    print(f"\n📊 Bridge Transfer Status: {args.transfer_id}")
    
    # In production, query from persistent storage
    print("   Status query not implemented in demo")
    print("   Use bridge explorer or query contract directly")


def cmd_bridge_list(args):
    """List bridge transfers"""
    print(f"\n📋 Bridge Transfers")
    print(f"   Status filter: {args.status or 'all'}")
    
    # In production, query from database
    print("   Transfer list not implemented in demo")


def cmd_run_node(args):
    """Run MPC relayer node"""
    print(f"\n🖥️  Starting MPC Relayer Node")
    print(f"   Node ID: {args.node_id}")
    print(f"   Config: {args.config}")
    
    try:
        from stealthpay.bridge.relayers import MPCRelayerNode
        from stealthpay.bridge.contracts.eth_bridge import EthereumBridgeContract
        from stealthpay.swaps.xmr.wallet import MoneroWallet
        
        # Load config
        import yaml
        with open(args.config) as f:
            config = yaml.safe_load(f)
        
        # Create components
        eth_bridge = EthereumBridgeContract(
            web3_provider=config['ethereum']['rpc_url'],
            contract_address=config['ethereum']['contract_address'],
            private_key=os.getenv("ETH_PRIVATE_KEY")
        )
        
        xmr_wallet = MoneroWallet(
            host=config['monero']['wallet_host'],
            port=config['monero']['wallet_port'],
            wallet_name=config['monero']['wallet_name']
        )
        
        # Create and start node
        node = MPCRelayerNode(
            node_id=args.node_id,
            eth_bridge_contract=eth_bridge,
            xmr_wallet=xmr_wallet
        )
        
        async def run():
            await node.start()
            
            print(f"\n✅ Node {args.node_id} is running")
            print(f"   Press Ctrl+C to stop")
            
            try:
                while True:
                    status = node.get_status()
                    print(f"\r   Status: {status['status']} | "
                          f"Pending: {status['pending_requests']} | "
                          f"Signed: {status['signed_requests']}", end="")
                    await asyncio.sleep(5)
            except KeyboardInterrupt:
                print(f"\n\n   Stopping node...")
                await node.stop()
        
        asyncio.run(run())
        
    except Exception as e:
        print(f"❌ Error: {e}")


def add_bridge_subparser(subparsers):
    """Add bridge subcommands to main parser"""
    bridge_parser = subparsers.add_parser('bridge', help='Cross-chain bridge commands')
    bridge_subparsers = bridge_parser.add_subparsers(dest='bridge_command', help='Bridge commands')
    
    # ETH to XMR
    eth_xmr_parser = bridge_subparsers.add_parser('eth-to-xmr', help='Bridge ETH to XMR')
    eth_xmr_parser.add_argument('--amount', type=float, required=True, help='ETH amount')
    eth_xmr_parser.add_argument('--xmr-address', required=True, help='XMR recipient address')
    eth_xmr_parser.add_argument('--eth-rpc', help='Ethereum RPC URL')
    eth_xmr_parser.add_argument('--contract', help='Bridge contract address')
    eth_xmr_parser.add_argument('--duration', type=int, default=24, help='Lock duration (hours)')
    eth_xmr_parser.add_argument('--network', default='regtest', choices=['regtest', 'testnet', 'mainnet'])
    
    # XMR to ETH
    xmr_eth_parser = bridge_subparsers.add_parser('xmr-to-eth', help='Bridge XMR to ETH')
    xmr_eth_parser.add_argument('--amount', type=float, required=True, help='XMR amount')
    xmr_eth_parser.add_argument('--eth-address', required=True, help='ETH recipient address')
    xmr_eth_parser.add_argument('--xmr-host', default='localhost', help='Monero wallet host')
    xmr_eth_parser.add_argument('--xmr-port', type=int, default=18082, help='Monero wallet port')
    xmr_eth_parser.add_argument('--network', default='stagenet', choices=['stagenet', 'testnet', 'mainnet'])
    
    # Status
    status_parser = bridge_subparsers.add_parser('status', help='Check transfer status')
    status_parser.add_argument('--transfer-id', required=True, help='Transfer ID')
    
    # List
    list_parser = bridge_subparsers.add_parser('list', help='List transfers')
    list_parser.add_argument('--status', choices=['pending', 'completed', 'failed'], help='Filter by status')
    
    # Run node
    node_parser = bridge_subparsers.add_parser('run-node', help='Run MPC relayer node')
    node_parser.add_argument('--config', required=True, help='Path to config file')
    node_parser.add_argument('--node-id', required=True, help='Node ID')
    
    return bridge_parser


def handle_bridge_command(args):
    """Handle bridge subcommands"""
    if not hasattr(args, 'bridge_command') or not args.bridge_command:
        print("❌ No bridge command specified. Use: stealthpay bridge --help")
        return
    
    commands = {
        'eth-to-xmr': cmd_eth_to_xmr,
        'xmr-to-eth': cmd_xmr_to_eth,
        'status': cmd_bridge_status,
        'list': cmd_bridge_list,
        'run-node': cmd_run_node,
    }
    
    handler = commands.get(args.bridge_command)
    if handler:
        handler(args)
    else:
        print(f"❌ Unknown bridge command: {args.bridge_command}")
