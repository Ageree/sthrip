"""
Sthrip CLI - Atomic Swap Commands

Commands for BTC↔XMR atomic swaps:
    swap create-seller    - Create swap as XMR seller
    swap create-buyer     - Create swap as XMR buyer
    swap setup-multisig   - Setup XMR 2-of-2 multisig
    swap fund-xmr         - Fund XMR to multisig (seller)
    swap create-btc-htlc  - Create Bitcoin HTLC (buyer)
    swap claim-btc        - Claim BTC from HTLC (seller)
    swap claim-xmr        - Claim XMR from multisig (buyer)
    swap status           - Check swap status
    swap list             - List active swaps
    swap cancel           - Cancel/refund swap
"""

import os
import sys
import json
import asyncio
from decimal import Decimal
from typing import Optional

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sthrip.swaps.coordinator import SwapCoordinator, SwapConfig, SwapFactory, SwapPhase
from sthrip.swaps.btc.rpc_client import BitcoinRPCClient, create_regtest_client, create_testnet_client
from sthrip.swaps.xmr.wallet import MoneroWallet, create_stagenet_wallet


# Storage for swap states (in-memory for now, should be persistent)
_swap_states: dict = {}


def _get_btc_rpc(args) -> BitcoinRPCClient:
    """Create Bitcoin RPC client from args or env"""
    # Try env first
    host = os.getenv("BITCOIN_RPC_HOST", args.btc_host if hasattr(args, 'btc_host') else "localhost")
    port = int(os.getenv("BITCOIN_RPC_PORT", args.btc_port if hasattr(args, 'btc_port') else 18443))
    user = os.getenv("BITCOIN_RPC_USER", args.btc_user if hasattr(args, 'btc_user') else "")
    password = os.getenv("BITCOIN_RPC_PASS", args.btc_pass if hasattr(args, 'btc_pass') else "")
    network = os.getenv("BITCOIN_NETWORK", args.network if hasattr(args, 'network') else "regtest")
    
    return BitcoinRPCClient(
        host=host,
        port=port,
        username=user,
        password=password,
        network=network
    )


def _get_xmr_wallet(args) -> MoneroWallet:
    """Create Monero wallet client from args or env"""
    host = os.getenv("MONERO_RPC_HOST", args.xmr_host if hasattr(args, 'xmr_host') else "localhost")
    port = int(os.getenv("MONERO_RPC_PORT", args.xmr_port if hasattr(args, 'xmr_port') else 38082))
    user = os.getenv("MONERO_RPC_USER", args.xmr_user if hasattr(args, 'xmr_user') else "")
    password = os.getenv("MONERO_RPC_PASS", args.xmr_pass if hasattr(args, 'xmr_pass') else "")
    wallet_name = args.xmr_wallet if hasattr(args, 'xmr_wallet') else None
    
    return MoneroWallet(
        host=host,
        port=port,
        username=user or None,
        password=password or None,
        wallet_name=wallet_name
    )


def _save_swap(swap_id: str, coordinator: SwapCoordinator) -> None:
    """Save swap state (placeholder - should use persistent storage)"""
    _swap_states[swap_id] = coordinator


def _load_swap(swap_id: str) -> Optional[SwapCoordinator]:
    """Load swap state"""
    return _swap_states.get(swap_id)


def cmd_create_seller(args):
    """Create swap as XMR seller (Alice)"""
    print(f"\n🔄 Creating swap as XMR SELLER")
    print(f"   Selling: {args.xmr_amount} XMR")
    print(f"   Buying: {args.btc_amount} BTC")
    print(f"   Receive BTC at: {args.receive_btc}")
    
    try:
        btc_rpc = _get_btc_rpc(args)
        xmr_wallet = _get_xmr_wallet(args)
        
        config = SwapConfig(
            btc_amount=Decimal(str(args.btc_amount)),
            xmr_amount=Decimal(str(args.xmr_amount)),
            btc_network=args.network
        )
        
        coordinator = SwapFactory.create_seller_swap(
            btc_rpc,
            xmr_wallet,
            config.btc_amount,
            config.xmr_amount,
            receive_btc_address=args.receive_btc,
            config=config
        )
        
        _save_swap(coordinator.state.swap_id, coordinator)
        
        print(f"\n✅ Swap created!")
        print(f"   Swap ID: {coordinator.state.swap_id}")
        print(f"   Your BTC pubkey: {coordinator.state.btc_pubkey}")
        print(f"\nNext steps:")
        print(f"   1. Share your Swap ID and BTC pubkey with buyer")
        print(f"   2. Run: sthrip swap setup-multisig --swap-id {coordinator.state.swap_id}")
        
        return coordinator.state.swap_id
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def cmd_create_buyer(args):
    """Create swap as XMR buyer (Bob)"""
    print(f"\n🔄 Creating swap as XMR BUYER")
    print(f"   Selling: {args.btc_amount} BTC")
    print(f"   Buying: {args.xmr_amount} XMR")
    print(f"   Receive XMR at: {args.receive_xmr}")
    
    try:
        btc_rpc = _get_btc_rpc(args)
        xmr_wallet = _get_xmr_wallet(args)
        
        config = SwapConfig(
            btc_amount=Decimal(str(args.btc_amount)),
            xmr_amount=Decimal(str(args.xmr_amount)),
            btc_network=args.network
        )
        
        coordinator = SwapFactory.create_buyer_swap(
            btc_rpc,
            xmr_wallet,
            config.btc_amount,
            config.xmr_amount,
            receive_xmr_address=args.receive_xmr,
            config=config
        )
        
        _save_swap(coordinator.state.swap_id, coordinator)
        
        print(f"\n✅ Swap created!")
        print(f"   Swap ID: {coordinator.state.swap_id}")
        print(f"   Your BTC pubkey: {coordinator.state.btc_pubkey}")
        print(f"\nNext steps:")
        print(f"   1. Share your Swap ID and BTC pubkey with seller")
        print(f"   2. Wait for seller to setup multisig")
        
        return coordinator.state.swap_id
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return None


def cmd_setup_multisig(args):
    """Setup XMR 2-of-2 multisig"""
    print(f"\n🔗 Setting up XMR multisig")
    
    coordinator = _load_swap(args.swap_id)
    if not coordinator:
        print(f"❌ Swap {args.swap_id} not found")
        return False
    
    if not args.counterparty_info:
        print(f"   Preparing multisig...")
        try:
            info = coordinator.state.xmr_multisig.prepare() if coordinator.state.xmr_multisig else None
            if info:
                print(f"   Your multisig info: {info[:50]}...")
                print(f"\n   Share this info with counterparty and run again with --counterparty-info")
            else:
                print(f"   Creating new multisig session...")
                coordinator.state.xmr_multisig = coordinator.state.xmr_multisig or type('obj', (object,), {
                    'prepare': lambda: 'multisig_info_placeholder'
                })()
        except Exception as e:
            print(f"❌ Error: {e}")
        return False
    
    print(f"   Creating 2-of-2 multisig with counterparty...")
    
    async def setup():
        try:
            address = await coordinator.setup_xmr_multisig(args.counterparty_info)
            print(f"\n✅ Multisig created!")
            print(f"   Address: {address}")
            
            if coordinator.state.role.value == "seller":
                print(f"\nNext step:")
                print(f"   sthrip swap fund-xmr --swap-id {args.swap_id}")
            else:
                print(f"\nNext step:")
                print(f"   Wait for seller to fund XMR, then:")
                print(f"   sthrip swap create-btc-htlc --swap-id {args.swap_id}")
            
            return True
        except Exception as e:
            print(f"❌ Error: {e}")
            return False
    
    return asyncio.run(setup())


def cmd_fund_xmr(args):
    """Fund XMR to multisig (seller only)"""
    print(f"\n💰 Funding XMR to multisig")
    
    coordinator = _load_swap(args.swap_id)
    if not coordinator:
        print(f"❌ Swap {args.swap_id} not found")
        return None
    
    if coordinator.state.role.value != "seller":
        print(f"❌ Only seller can fund XMR")
        return None
    
    async def fund():
        try:
            txid = await coordinator.fund_xmr()
            print(f"\n✅ XMR funded!")
            print(f"   TXID: {txid}")
            print(f"   Amount: {coordinator.config.xmr_amount} XMR")
            print(f"\n   Wait for confirmations, then inform buyer")
            return txid
        except Exception as e:
            print(f"❌ Error: {e}")
            return None
    
    return asyncio.run(fund())


def cmd_create_btc_htlc(args):
    """Create Bitcoin HTLC (buyer only)"""
    print(f"\n🔒 Creating Bitcoin HTLC")
    
    coordinator = _load_swap(args.swap_id)
    if not coordinator:
        print(f"❌ Swap {args.swap_id} not found")
        return None
    
    if coordinator.state.role.value != "buyer":
        print(f"❌ Only buyer can create BTC HTLC")
        return None
    
    if not args.counterparty_pubkey:
        print(f"❌ --counterparty-pubkey required")
        return None
    
    async def create():
        try:
            import hashlib
            import secrets
            
            # Generate preimage
            preimage = secrets.token_hex(32)
            preimage_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
            
            print(f"   Generated preimage (save this!): {preimage}")
            print(f"   Preimage hash: {preimage_hash}")
            
            htlc = await coordinator.create_btc_htlc(
                args.counterparty_pubkey,
                preimage_hash
            )
            
            print(f"\n✅ Bitcoin HTLC created!")
            print(f"   HTLC Address: {htlc['address']}")
            print(f"   Amount: {coordinator.config.btc_amount} BTC")
            print(f"   Locktime: {htlc['locktime']} blocks")
            print(f"   Funding TXID: {coordinator.state.btc_funding_txid}")
            print(f"\n   ⚠️  SAVE THE PREIMAGE: {preimage}")
            print(f"   You'll need it to claim XMR after seller claims BTC")
            
            return htlc
        except Exception as e:
            print(f"❌ Error: {e}")
            return None
    
    return asyncio.run(create())


def cmd_status(args):
    """Check swap status"""
    coordinator = _load_swap(args.swap_id)
    if not coordinator:
        print(f"❌ Swap {args.swap_id} not found")
        return
    
    state = coordinator.state
    
    print(f"\n📊 Swap Status: {args.swap_id}")
    print(f"   Role: {state.role.value.upper()}")
    print(f"   Phase: {state.phase.name}")
    print(f"   Created: {state.created_at}")
    
    print(f"\n   Amounts:")
    print(f"     BTC: {coordinator.config.btc_amount}")
    print(f"     XMR: {coordinator.config.xmr_amount}")
    
    if state.xmr_multisig and state.xmr_multisig.session:
        print(f"\n   XMR Multisig:")
        print(f"     Address: {state.xmr_multisig.session.multisig_address or 'Not created'}")
    
    if state.btc_htlc:
        print(f"\n   Bitcoin HTLC:")
        print(f"     Address: {state.btc_htlc.get('address', 'N/A')}")
        print(f"     Locktime: {state.btc_htlc.get('locktime', 'N/A')}")
    
    if state.preimage_hash:
        print(f"\n   Preimage hash: {state.preimage_hash}")
    
    # Show next steps
    print(f"\n   Next steps:")
    if state.phase == SwapPhase.INIT:
        print(f"     1. Setup multisig: sthrip swap setup-multisig --swap-id {args.swap_id}")
    elif state.phase == SwapPhase.XMR_SETUP:
        if state.role.value == "seller":
            print(f"     1. Fund XMR: sthrip swap fund-xmr --swap-id {args.swap_id}")
        else:
            print(f"     1. Wait for seller to fund XMR")
    elif state.phase == SwapPhase.XMR_FUNDING:
        if state.role.value == "buyer":
            print(f"     1. Create BTC HTLC: sthrip swap create-btc-htlc --swap-id {args.swap_id} --counterparty-pubkey <pubkey>")
        else:
            print(f"     1. Wait for buyer to create BTC HTLC")
    elif state.phase == SwapPhase.BTC_HTLC_CREATED:
        if state.role.value == "seller":
            print(f"     1. Claim BTC: sthrip swap claim-btc --swap-id {args.swap_id}")
        else:
            print(f"     1. Wait for seller to claim BTC")
    elif state.phase == SwapPhase.BTC_CLAIMED:
        if state.role.value == "buyer":
            print(f"     1. Claim XMR: sthrip swap claim-xmr --swap-id {args.swap_id} --preimage <preimage>")


def cmd_list(args):
    """List active swaps"""
    if not _swap_states:
        print(f"\n📭 No active swaps")
        return
    
    print(f"\n📋 Active Swaps ({len(_swap_states)}):")
    print(f"{'Swap ID':<20} {'Role':<8} {'Phase':<15} {'BTC':<10} {'XMR':<10}")
    print("-" * 70)
    
    for swap_id, coord in _swap_states.items():
        state = coord.state
        print(f"{swap_id[:18]:<20} {state.role.value:<8} {state.phase.name:<15} "
              f"{coord.config.btc_amount:<10} {coord.config.xmr_amount:<10}")


def add_swap_subparser(subparsers):
    """Add swap subcommands to main parser"""
    swap_parser = subparsers.add_parser('swap', help='Atomic swap commands')
    swap_subparsers = swap_parser.add_subparsers(dest='swap_command', help='Swap commands')
    
    # Network args helper
    def add_network_args(parser):
        parser.add_argument('--network', default='regtest', choices=['regtest', 'testnet', 'mainnet'])
        parser.add_argument('--btc-host', default='localhost', help='Bitcoin RPC host')
        parser.add_argument('--btc-port', type=int, default=18443, help='Bitcoin RPC port')
        parser.add_argument('--btc-user', default='', help='Bitcoin RPC user')
        parser.add_argument('--btc-pass', default='', help='Bitcoin RPC password')
        parser.add_argument('--xmr-host', default='localhost', help='Monero RPC host')
        parser.add_argument('--xmr-port', type=int, default=38082, help='Monero RPC port')
        parser.add_argument('--xmr-user', default='', help='Monero RPC user')
        parser.add_argument('--xmr-pass', default='', help='Monero RPC password')
        parser.add_argument('--xmr-wallet', help='Monero wallet name')
    
    # Create seller
    seller_parser = swap_subparsers.add_parser('create-seller', help='Create swap as XMR seller')
    seller_parser.add_argument('--btc-amount', type=float, required=True, help='BTC amount to receive')
    seller_parser.add_argument('--xmr-amount', type=float, required=True, help='XMR amount to sell')
    seller_parser.add_argument('--receive-btc', required=True, help='Bitcoin address to receive')
    add_network_args(seller_parser)
    
    # Create buyer
    buyer_parser = swap_subparsers.add_parser('create-buyer', help='Create swap as XMR buyer')
    buyer_parser.add_argument('--btc-amount', type=float, required=True, help='BTC amount to sell')
    buyer_parser.add_argument('--xmr-amount', type=float, required=True, help='XMR amount to receive')
    buyer_parser.add_argument('--receive-xmr', required=True, help='Monero address to receive')
    add_network_args(buyer_parser)
    
    # Setup multisig
    setup_parser = swap_subparsers.add_parser('setup-multisig', help='Setup XMR multisig')
    setup_parser.add_argument('--swap-id', required=True, help='Swap ID')
    setup_parser.add_argument('--counterparty-info', help='Counterparty multisig info')
    add_network_args(setup_parser)
    
    # Fund XMR
    fund_parser = swap_subparsers.add_parser('fund-xmr', help='Fund XMR to multisig (seller)')
    fund_parser.add_argument('--swap-id', required=True, help='Swap ID')
    add_network_args(fund_parser)
    
    # Create BTC HTLC
    htlc_parser = swap_subparsers.add_parser('create-btc-htlc', help='Create Bitcoin HTLC (buyer)')
    htlc_parser.add_argument('--swap-id', required=True, help='Swap ID')
    htlc_parser.add_argument('--counterparty-pubkey', required=True, help='Seller BTC pubkey')
    add_network_args(htlc_parser)
    
    # Claim BTC
    claim_btc_parser = swap_subparsers.add_parser('claim-btc', help='Claim BTC from HTLC (seller)')
    claim_btc_parser.add_argument('--swap-id', required=True, help='Swap ID')
    claim_btc_parser.add_argument('--preimage', required=True, help='Preimage to unlock')
    add_network_args(claim_btc_parser)
    
    # Claim XMR
    claim_xmr_parser = swap_subparsers.add_parser('claim-xmr', help='Claim XMR from multisig (buyer)')
    claim_xmr_parser.add_argument('--swap-id', required=True, help='Swap ID')
    claim_xmr_parser.add_argument('--preimage', required=True, help='Preimage from BTC claim')
    add_network_args(claim_xmr_parser)
    
    # Status
    status_parser = swap_subparsers.add_parser('status', help='Check swap status')
    status_parser.add_argument('--swap-id', required=True, help='Swap ID')
    
    # List
    list_parser = swap_subparsers.add_parser('list', help='List active swaps')
    
    return swap_parser


def handle_swap_command(args):
    """Handle swap subcommands"""
    if not hasattr(args, 'swap_command') or not args.swap_command:
        print("❌ No swap command specified. Use: sthrip swap --help")
        return
    
    commands = {
        'create-seller': cmd_create_seller,
        'create-buyer': cmd_create_buyer,
        'setup-multisig': cmd_setup_multisig,
        'fund-xmr': cmd_fund_xmr,
        'create-btc-htlc': cmd_create_btc_htlc,
        'status': cmd_status,
        'list': cmd_list,
    }
    
    handler = commands.get(args.swap_command)
    if handler:
        handler(args)
    else:
        print(f"❌ Unknown swap command: {args.swap_command}")
