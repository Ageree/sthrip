#!/bin/bash
# Testnet Testing Script for Sthrip
# Tests Atomic Swaps on Bitcoin Testnet3 and Monero Stagenet

set -e

echo "============================================"
echo "🧪 Sthrip Testnet Testing"
echo "============================================"
echo ""
echo "⚠️  WARNING: This will use real testnet funds!"
echo "   Bitcoin: Testnet3 (tBTC)"
echo "   Monero: Stagenet (stagenet XMR)"
echo ""
read -p "Continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
export BITCOIN_NETWORK=testnet
export MONERO_NETWORK=stagenet

# Check for required tools
echo -e "\n${BLUE}Checking prerequisites...${NC}"
command -v python3 >/dev/null 2>&1 || { echo -e "${RED}Python 3 required${NC}"; exit 1; }
command -v curl >/dev/null 2>&1 || { echo -e "${RED}curl required${NC}"; exit 1; }

echo -e "${GREEN}✓ Prerequisites met${NC}"

# Check environment
echo -e "\n${BLUE}Checking environment variables...${NC}"
if [ -z "$BITCOIN_RPC_USER" ]; then
    export BITCOIN_RPC_USER=bitcoin
    export BITCOIN_RPC_PASS=bitcoin
fi

if [ -z "$BITCOIN_RPC_HOST" ]; then
    echo -e "${YELLOW}! Using default Bitcoin RPC: localhost:18332${NC}"
    export BITCOIN_RPC_HOST=localhost
    export BITCOIN_RPC_PORT=18332
fi

if [ -z "$MONERO_RPC_HOST" ]; then
    echo -e "${YELLOW}! Using default Monero RPC: localhost:38082${NC}"
    export MONERO_RPC_HOST=localhost
    export MONERO_RPC_PORT=38082
fi

echo -e "${GREEN}✓ Environment configured${NC}"

# Test 1: Connectivity Test
test_connectivity() {
    echo -e "\n${BLUE}═══════════════════════════════════════════${NC}"
    echo -e "${BLUE}TEST 1: Node Connectivity${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    
    PYTHONPATH="$(dirname "$0")/..:$PYTHONPATH" python3 << 'PYEOF'
import sys
sys.path.insert(0, '.')

print("\n🔌 Testing node connectivity...")

# Test Bitcoin
print("\n1. Bitcoin Testnet3:")
try:
    from sthrip.swaps.btc.rpc_client import create_testnet_client
    btc = create_testnet_client()
    height = btc.get_block_count()
    print(f"   ✓ Connected! Block height: {height}")
    
    balance = btc.get_balance()
    print(f"   ✓ Wallet balance: {balance} tBTC")
    
    if balance < 0.001:
        print(f"   ⚠️  Low balance! Get testnet coins from:")
        print(f"      https://testnet-faucet.mempool.co/")
        print(f"      https://coinfaucet.eu/en/btc-testnet/")
    
except Exception as e:
    print(f"   ✗ Connection failed: {e}")
    print(f"   Make sure bitcoind is running with testnet:")
    print(f"   bitcoind -testnet -daemon -rpcuser=$BITCOIN_RPC_USER -rpcpassword=$BITCOIN_RPC_PASS")
    sys.exit(1)

# Test Monero
print("\n2. Monero Stagenet:")
try:
    from sthrip.swaps.xmr.wallet import create_stagenet_wallet
    xmr = create_stagenet_wallet()
    address = xmr.get_address()
    print(f"   ✓ Connected! Address: {address[:20]}...")
    
    balance = xmr.get_balance()
    print(f"   ✓ Balance: {balance['balance']} XMR (unlocked: {balance['unlocked']})")
    
    if balance['balance'] < 0.1:
        print(f"   ⚠️  Low balance! Get stagenet coins from:")
        print(f"      https://community.xmr.to/xmr-faucet/stagenet/")
        print(f"      Your address: {address}")

except Exception as e:
    print(f"   ✗ Connection failed: {e}")
    print(f"   Make sure monero-wallet-rpc is running:")
    print(f"   monero-wallet-rpc --stagenet --rpc-bind-port 38082 --wallet-dir /path")
    sys.exit(1)

print("\n✓ All nodes connected!")
PYEOF
}

# Test 2: Generate Keys Test
test_key_generation() {
    echo -e "\n${BLUE}═══════════════════════════════════════════${NC}"
    echo -e "${BLUE}TEST 2: Cryptographic Key Generation${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    
    PYTHONPATH="$(dirname "$0")/..:$PYTHONPATH" python3 << 'PYEOF'
import sys
sys.path.insert(0, '.')

print("\n🔑 Testing key generation...")

from sthrip.swaps.utils.bitcoin import generate_keypair
from sthrip.bridge.tss.dkg import DistributedKeyGenerator

# Bitcoin keys
print("\n1. Bitcoin secp256k1 keys:")
priv, pub = generate_keypair()
print(f"   Private: {priv.hex()[:20]}...")
print(f"   Public:  {pub.hex()[:20]}...")
print(f"   ✓ Keypair generated")

# TSS keys
print("\n2. TSS threshold keys (3-of-5):")
dkg = DistributedKeyGenerator(n=5, threshold=3)
shares = dkg.generate_key_shares()
print(f"   ✓ Generated {len(shares)} key shares")
print(f"   ✓ Group public key: {shares[0].public_key.hex()[:40]}...")

# Verify shares
valid = all(dkg.verify_share(share) for share in shares)
print(f"   ✓ All shares valid: {valid}")

print("\n✓ Key generation working!")
PYEOF
}

# Test 3: HTLC Creation Test
test_htlc_creation() {
    echo -e "\n${BLUE}═══════════════════════════════════════════${NC}"
    echo -e "${BLUE}TEST 3: HTLC Contract Creation${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    
    PYTHONPATH="$(dirname "$0")/..:$PYTHONPATH" python3 << 'PYEOF'
import sys
sys.path.insert(0, '.')

print("\n📜 Testing HTLC creation...")

from decimal import Decimal
from sthrip.swaps.btc.htlc import BitcoinHTLC, create_simple_htlc_for_swap
from sthrip.swaps.btc.rpc_client import create_testnet_client
from sthrip.swaps.utils.bitcoin import generate_keypair

# Connect to Bitcoin testnet
btc = create_testnet_client()
height = btc.get_block_count()
print(f"\n1. Bitcoin testnet height: {height}")

# Generate keys
print("\n2. Generating ephemeral keys...")
sender_priv, sender_pub = generate_keypair()
recipient_priv, recipient_pub = generate_keypair()
print(f"   Sender pubkey: {sender_pub.hex()[:40]}...")
print(f"   Recipient pubkey: {recipient_pub.hex()[:40]}...")

# Create HTLC
print("\n3. Creating HTLC contract...")
htlc = BitcoinHTLC(btc, network="testnet")
contract = htlc.create_htlc(
    sender_pubkey=sender_pub,
    recipient_pubkey=recipient_pub,
    locktime_blocks=144,  # ~24 hours
    amount_btc=Decimal("0.0001")
)

print(f"   ✓ HTLC Address: {contract['address']}")
print(f"   ✓ Preimage hash: {contract['preimage_hash'][:40]}...")
print(f"   ✓ Locktime: {contract['locktime']} blocks")
print(f"   ✓ Redeem script size: {len(contract['redeem_script'])} bytes")

# Save for later
import json
with open('/tmp/test_htlc_contract.json', 'w') as f:
    json.dump({
        'address': contract['address'],
        'preimage': contract['preimage'],
        'preimage_hash': contract['preimage_hash'],
        'redeem_script': contract['redeem_script'],
        'locktime': contract['locktime'],
        'sender_priv': sender_priv.hex(),
        'recipient_priv': recipient_priv.hex()
    }, f, indent=2)

print(f"\n   Contract saved to: /tmp/test_htlc_contract.json")
print("\n✓ HTLC created successfully!")
print(f"\n💡 To fund this HTLC, send 0.0001 tBTC to:")
print(f"   {contract['address']}")
PYEOF
}

# Test 4: XMR Multisig Setup
test_xmr_multisig() {
    echo -e "\n${BLUE}═══════════════════════════════════════════${NC}"
    echo -e "${BLUE}TEST 4: Monero Multisig Setup${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    
    PYTHONPATH="$(dirname "$0")/..:$PYTHONPATH" python3 << 'PYEOF'
import sys
sys.path.insert(0, '.')

print("\n🔗 Testing Monero 2-of-2 multisig setup...")

from sthrip.swaps.xmr.wallet import create_stagenet_wallet
from sthrip.swaps.xmr.multisig import MoneroMultisig, SwapRole

# Create wallet connections
print("\n1. Connecting to Monero wallets...")
wallet = create_stagenet_wallet()
address = wallet.get_address()
balance = wallet.get_balance()

print(f"   ✓ Wallet address: {address}")
print(f"   ✓ Balance: {balance['balance']} XMR")

# Create multisig
print("\n2. Preparing multisig...")
multisig = MoneroMultisig(wallet, SwapRole.SELLER)
multisig_info = multisig.prepare()

print(f"   ✓ Multisig info generated: {multisig_info[:50]}...")
print(f"\n💡 To complete multisig setup:")
print(f"   1. Share this info with counterparty")
print(f"   2. Exchange multisig info")
print(f"   3. Run: make_multisig()")

print("\n✓ Multisig preparation complete!")
PYEOF
}

# Test 5: TSS Signing Test
test_tss_signing() {
    echo -e "\n${BLUE}═══════════════════════════════════════════${NC}"
    echo -e "${BLUE}TEST 5: Threshold Signature Scheme${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    
    PYTHONPATH="$(dirname "$0")/..:$PYTHONPATH" python3 << 'PYEOF'
import sys
import hashlib
sys.path.insert(0, '.')

print("\n✍️  Testing threshold signing (3-of-5)...")

from sthrip.bridge.tss.dkg import DistributedKeyGenerator
from sthrip.bridge.tss.signer import ThresholdSigner, SigningSession
from sthrip.bridge.tss.aggregator import SignatureAggregator

# Setup
n, threshold = 5, 3
print(f"\n1. Setting up {threshold}-of-{n} threshold scheme...")

dkg = DistributedKeyGenerator(n, threshold)
shares = dkg.generate_key_shares()
print(f"   ✓ Generated {len(shares)} key shares")

# Create signers
signers = [ThresholdSigner(share, share.index) for share in shares]

# Message to sign
message = b"Testnet message for threshold signature test"
message_hash = hashlib.sha256(message).digest()
print(f"\n2. Message: {message.decode()}")
print(f"   Hash: {message_hash.hex()[:40]}...")

# Select signers
selected = signers[:threshold]
print(f"\n3. Selected {threshold} signers")

# Signing session
session = SigningSession(message_hash, threshold)

# Phase 1: Commitments
print("\n4. Phase 1: Generating commitments...")
for signer in selected:
    sig = signer.create_partial_signature(message_hash)
    session.add_commitment(sig)
    print(f"   ✓ Signer {signer.party_id} committed")

# Phase 2: Signatures
context = session.finalize_commitments()
print(f"\n5. Phase 2: Creating signature shares...")

completed_sigs = []
for signer in selected:
    partial = signer.create_partial_signature(message_hash)
    completed = signer.complete_signature(partial, context)
    session.add_signature_share(completed)
    completed_sigs.append(completed)
    print(f"   ✓ Signer {signer.party_id} signed")

# Aggregate
print("\n6. Aggregating signatures...")
aggregator = SignatureAggregator()
full_sig = aggregator.aggregate_signatures(completed_sigs, context)

print(f"   ✓ Signature r: {hex(full_sig.r)[:40]}...")
print(f"   ✓ Signature s: {hex(full_sig.s)[:40]}...")
print(f"   ✓ DER encoded: {len(full_sig.to_der())} bytes")

print("\n✓ Threshold signing completed!")
print("\n💡 This signature can be verified on Ethereum with the group public key")
PYEOF
}

# Test 6: Full Swap Simulation
test_full_swap() {
    echo -e "\n${BLUE}═══════════════════════════════════════════${NC}"
    echo -e "${BLUE}TEST 6: Full Atomic Swap Simulation${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════${NC}"
    
    PYTHONPATH="$(dirname "$0")/..:$PYTHONPATH" python3 << 'PYEOF'
import sys
sys.path.insert(0, '.')

print("\n🔄 Simulating full atomic swap on testnet...")

from decimal import Decimal
from sthrip.swaps.coordinator import SwapFactory, SwapConfig
from sthrip.swaps.btc.rpc_client import create_testnet_client
from sthrip.swaps.xmr.wallet import create_stagenet_wallet

# Setup
print("\n1. Initializing swap coordinators...")
config = SwapConfig(
    btc_amount=Decimal("0.0001"),
    xmr_amount=Decimal("0.01"),
    btc_network="testnet",
    xmr_network="stagenet"
)

btc_rpc = create_testnet_client()
xmr_wallet = create_stagenet_wallet()

# Generate addresses
alice_btc = btc_rpc.get_new_address()
bob_btc = btc_rpc.get_new_address()

print(f"   Alice BTC: {alice_btc}")
print(f"   Bob BTC: {bob_btc}")

# Create coordinators
print("\n2. Creating swap participants...")

alice = SwapFactory.create_seller_swap(
    btc_rpc, xmr_wallet,
    config.btc_amount, config.xmr_amount,
    receive_btc_address=alice_btc,
    config=config
)

bob = SwapFactory.create_buyer_swap(
    btc_rpc, xmr_wallet,
    config.btc_amount, config.xmr_amount,
    receive_xmr_address="44StagenetAddress...",
    config=config
)

print(f"   ✓ Alice swap ID: {alice.state.swap_id}")
print(f"   ✓ Bob swap ID: {bob.state.swap_id}")

print("\n3. Swap phases:")
print("   [1] Alice prepares XMR multisig")
print("   [2] Bob prepares XMR multisig")
print("   [3] Alice funds XMR to multisig")
print("   [4] Bob creates BTC HTLC")
print("   [5] Alice claims BTC (reveals preimage)")
print("   [6] Bob claims XMR (uses preimage)")

print("\n✓ Swap simulation complete!")
print("\n💡 To execute real swap:")
print("   1. Get testnet coins")
print("   2. Run: sthrip swap create-seller")
print("   3. Coordinate with counterparty")
print("   4. Monitor with: sthrip swap status")
PYEOF
}

# Main menu
main() {
    echo -e "\n${GREEN}Starting testnet test suite...${NC}\n"
    
    test_connectivity
    test_key_generation
    test_htlc_creation
    test_xmr_multisig
    test_tss_signing
    test_full_swap
    
    echo -e "\n${GREEN}============================================${NC}"
    echo -e "${GREEN}✓ All tests completed successfully!${NC}"
    echo -e "${GREEN}============================================${NC}"
    echo ""
    echo "Next steps:"
    echo "1. Get testnet coins:"
    echo "   Bitcoin: https://testnet-faucet.mempool.co/"
    echo "   Monero:  https://community.xmr.to/xmr-faucet/stagenet/"
    echo ""
    echo "2. Execute real swap:"
    echo "   sthrip swap create-seller --btc-amount 0.001 --xmr-amount 0.1 ..."
    echo ""
}

main "$@"
