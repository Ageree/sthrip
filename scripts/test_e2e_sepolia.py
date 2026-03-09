#!/usr/bin/env python3
"""
End-to-End Test: Sepolia ETH -> XMR Stagenet

IMPORTANT: Uses REAL testnet funds (worth $0) 
Test with small amounts first (0.001 ETH)
"""

import os
import sys
import asyncio
import json
from pathlib import Path
from getpass import getpass

try:
    from web3 import Web3
    from eth_account import Account
except ImportError:
    print("❌ Install requirements: pip install web3 eth-account")
    sys.exit(1)

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sthrip.bridge.privacy import StealthAddressGenerator

# Configuration
SEPOLIA_RPC = os.getenv("SEPOLIA_RPC", "https://rpc.sepolia.org")
BRIDGE_ADDRESS = os.getenv("BRIDGE_CONTRACT")
TEST_AMOUNT = 0.001  # ETH - small test amount

# Bridge ABI (simplified - full ABI should be loaded from artifacts)
BRIDGE_ABI = json.loads("""[
    {"inputs":[{"internalType":"string","name":"xmrAddress","type":"string"},{"internalType":"uint256","name":"duration","type":"uint256"},{"internalType":"bytes32","name":"mpcMerkleRoot","type":"bytes32"}],"name":"lock","outputs":[{"internalType":"bytes32","name":"lockId","type":"bytes32"}],"stateMutability":"payable","type":"function"},
    {"inputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"name":"locks","outputs":[{"internalType":"address","name":"sender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"},{"internalType":"string","name":"xmrAddress","type":"string"},{"internalType":"uint256","name":"unlockTime","type":"uint256"},{"internalType":"bool","name":"claimed","type":"bool"},{"internalType":"bool","name":"refunded","type":"bool"}],"stateMutability":"view","type":"function"},
    {"anonymous":false,"inputs":[{"indexed":true,"internalType":"bytes32","name":"lockId","type":"bytes32"},{"indexed":true,"internalType":"address","name":"sender","type":"address"},{"indexed":false,"internalType":"uint256","name":"amount","type":"uint256"},{"indexed":false,"internalType":"string","name":"xmrAddress","type":"string"},{"indexed":false,"internalType":"uint256","name":"unlockTime","type":"uint256"}],"name":"Locked","type":"event"}
]
""")


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    END = '\033[0m'


def print_header(text):
    print(f"\n{Colors.BLUE}{'='*60}{Colors.END}")
    print(f"{Colors.BLUE}{text}{Colors.END}")
    print(f"{Colors.BLUE}{'='*60}{Colors.END}\n")


def print_success(text):
    print(f"{Colors.GREEN}✓ {text}{Colors.END}")


def print_error(text):
    print(f"{Colors.RED}✗ {text}{Colors.END}")


def print_warning(text):
    print(f"{Colors.YELLOW}⚠ {text}{Colors.END}")


async def test_e2e():
    """Run E2E test"""
    print_header("STHRIP E2E TEST - SEPOLIA")
    
    # Check environment
    private_key = os.getenv("TEST_PRIVATE_KEY")
    if not private_key:
        print_warning("TEST_PRIVATE_KEY not set in environment")
        print("Get test key from: https://sepolia-faucet.pk910.de/")
        private_key = getpass("Enter Sepolia test private key (0x...): ").strip()
    
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    if not BRIDGE_ADDRESS:
        print_warning("BRIDGE_CONTRACT not set")
        bridge_addr = input("Enter deployed bridge address: ").strip()
    else:
        bridge_addr = BRIDGE_ADDRESS
    
    # Setup Web3
    print("🔌 Connecting to Sepolia...")
    w3 = Web3(Web3.HTTPProvider(SEPOLIA_RPC))
    
    if not w3.is_connected():
        print_error(f"Cannot connect to Sepolia at {SEPOLIA_RPC}")
        return False
    print_success(f"Connected to Sepolia (chain ID: {w3.eth.chain_id})")
    
    # Setup account
    account = Account.from_key(private_key)
    print_success(f"Account: {account.address}")
    
    # Check balance
    balance = w3.eth.get_balance(account.address)
    balance_eth = w3.from_wei(balance, 'ether')
    print(f"💰 Balance: {balance_eth:.4f} Sepolia ETH")
    
    if balance < w3.to_wei(TEST_AMOUNT, 'ether'):
        print_error(f"Insufficient balance. Need {TEST_AMOUNT} ETH for test")
        print("Get free Sepolia ETH from:")
        print("  - https://sepolia-faucet.pk910.de/")
        print("  - https://www.infura.io/faucet/sepolia")
        return False
    
    # Generate stealth address for XMR
    print_header("GENERATING STEALTH ADDRESS")
    generator = StealthAddressGenerator()
    xmr_keys = generator.generate_master_keys()
    xmr_stealth = generator.generate_stealth_address(
        xmr_keys.scan_public,
        xmr_keys.spend_public
    )
    
    print(f"🔐 XMR View Key: {xmr_keys.scan_public.hex()[:30]}...")
    print(f"🔐 XMR Spend Key: {xmr_keys.spend_public.hex()[:30]}...")
    print(f"📫 XMR Stealth Address: {xmr_stealth.address[:40]}...")
    print_success("Stealth address generated")
    
    # Setup bridge contract
    print_header("LOCKING ETH IN BRIDGE")
    bridge = w3.eth.contract(address=bridge_addr, abi=BRIDGE_ABI)
    
    # Check contract
    try:
        code = w3.eth.get_code(bridge_addr)
        if code == b'':
            print_error("No contract at this address!")
            return False
        print_success("Bridge contract found")
    except Exception as e:
        print_error(f"Contract check failed: {e}")
        return False
    
    # Prepare transaction
    amount_wei = w3.to_wei(TEST_AMOUNT, 'ether')
    duration = 3600  # 1 hour
    merkle_root = w3.keccak(text="test_merkle_root")
    
    print(f"\n📋 Transaction details:")
    print(f"   Amount: {TEST_AMOUNT} ETH ({amount_wei} wei)")
    print(f"   Duration: {duration} seconds (1 hour)")
    print(f"   XMR Address: {xmr_stealth.address[:50]}...")
    
    confirm = input(f"\n⚠️  Send {TEST_AMOUNT} Sepolia ETH to bridge? [y/N]: ")
    if confirm.lower() != 'y':
        print_warning("Test cancelled by user")
        return False
    
    # Build and send transaction
    try:
        print("\n🔨 Building transaction...")
        tx = bridge.functions.lock(
            xmr_stealth.address,
            duration,
            merkle_root
        ).build_transaction({
            'from': account.address,
            'value': amount_wei,
            'gas': 200000,
            'maxFeePerGas': w3.to_wei('50', 'gwei'),
            'maxPriorityFeePerGas': w3.to_wei('2', 'gwei'),
            'nonce': w3.eth.get_transaction_count(account.address),
            'chainId': 11155111,  # Sepolia
        })
        
        print("✍️  Signing transaction...")
        signed = w3.eth.account.sign_transaction(tx, private_key)
        
        print("📤 Sending transaction...")
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        
        print(f"⏳ Waiting for confirmation...")
        print(f"   TX Hash: {tx_hash.hex()}")
        print(f"   Explorer: https://sepolia.etherscan.io/tx/{tx_hash.hex()}")
        
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status == 1:
            print_success(f"Transaction confirmed!")
            print(f"   Gas used: {receipt.gasUsed}")
            print(f"   Block: {receipt.blockNumber}")
        else:
            print_error("Transaction failed!")
            return False
        
    except Exception as e:
        print_error(f"Transaction failed: {e}")
        return False
    
    # Parse event for lock ID
    print_header("EXTRACTING LOCK ID")
    try:
        logs = bridge.events.Locked().process_receipt(receipt)
        if logs:
            lock_id = logs[0]['args']['lockId']
            print_success(f"Lock ID: {lock_id.hex()}")
            
            # Verify lock
            lock = bridge.functions.locks(lock_id).call()
            print(f"\n📊 Lock details:")
            print(f"   Sender: {lock[0]}")
            print(f"   Amount: {w3.from_wei(lock[1], 'ether')} ETH")
            print(f"   XMR Address: {lock[2][:50]}...")
            print(f"   Unlock time: {lock[3]} (in {lock[3] - w3.eth.get_block('latest')['timestamp']} seconds)")
        else:
            print_warning("No Locked event found in receipt")
    except Exception as e:
        print_error(f"Failed to parse lock: {e}")
    
    # Final summary
    print_header("TEST SUMMARY")
    print_success("E2E test initiated successfully!")
    print(f"\n📋 Next steps:")
    print(f"   1. Monitor Sepolia TX: https://sepolia.etherscan.io/tx/{tx_hash.hex()}")
    print(f"   2. Check MPC node logs for processing")
    print(f"   3. Verify XMR received at: {xmr_stealth.address[:40]}...")
    print(f"   4. Use XMR view key to verify: {xmr_keys.scan_public.hex()[:30]}...")
    
    print(f"\n⚠️  IMPORTANT:")
    print(f"   - This was testnet (Sepolia) ETH - worth $0")
    print(f"   - For mainnet: wait for security audit")
    print(f"   - Never test with real money until audited")
    
    return True


def main():
    """Main entry"""
    try:
        result = asyncio.run(test_e2e())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\n❌ Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
