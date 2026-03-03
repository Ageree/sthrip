#!/bin/bash
# Integration Test Script with Real Nodes
# Tests Atomic Swaps on regtest/stagenet

set -e

echo "============================================"
echo "StealthPay Integration Test"
echo "============================================"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if nodes are running
check_nodes() {
    echo -e "\n${YELLOW}Checking node connectivity...${NC}"
    
    # Bitcoin
    if curl -s -u bitcoin:bitcoin --data-binary '{"jsonrpc":"1.0","id":"test","method":"getblockcount","params":[]}' -H 'content-type: text/plain;' http://localhost:18443/ > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Bitcoin node connected${NC}"
        BITCOIN_READY=1
    else
        echo -e "${RED}✗ Bitcoin node not available${NC}"
        BITCOIN_READY=0
    fi
    
    # Monero
    if curl -s http://localhost:38081/get_height > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Monero node connected${NC}"
        MONERO_READY=1
    else
        echo -e "${RED}✗ Monero node not available${NC}"
        MONERO_READY=0
    fi
    
    if [ $BITCOIN_READY -eq 0 ] || [ $MONERO_READY -eq 0 ]; then
        echo -e "\n${RED}ERROR: Required nodes not running${NC}"
        echo "Start nodes with: docker-compose -f docker-compose.full.yml up -d"
        exit 1
    fi
}

# Generate test funds
setup_funds() {
    echo -e "\n${YELLOW}Setting up test funds...${NC}"
    
    # Generate Bitcoin blocks for coinbase
    echo "Generating Bitcoin blocks..."
    BTC_ADDRESS=$(curl -s -u bitcoin:bitcoin --data-binary '{"jsonrpc":"1.0","id":"test","method":"getnewaddress","params":[]}' -H 'content-type: text/plain;' http://localhost:18443/ | python3 -c "import sys,json; print(json.load(sys.stdin)['result'])")
    curl -s -u bitcoin:bitcoin --data-binary "{\"jsonrpc\":\"1.0\",\"id\":\"test\",\"method\":\"generatetoaddress\",\"params\":[101,\"$BTC_ADDRESS\"]}" -H 'content-type: text/plain;' http://localhost:18443/ > /dev/null
    
    BTC_BALANCE=$(curl -s -u bitcoin:bitcoin --data-binary '{"jsonrpc":"1.0","id":"test","method":"getbalance","params":[]}' -H 'content-type: text/plain;' http://localhost:18443/ | python3 -c "import sys,json; print(json.load(sys.stdin)['result'])")
    echo -e "${GREEN}✓ Bitcoin balance: $BTC_BALANCE BTC${NC}"
    
    echo -e "${YELLOW}Note: Monero stagenet needs external funding${NC}"
}

# Run Python integration tests
run_tests() {
    echo -e "\n${YELLOW}Running integration tests...${NC}"
    
    export BITCOIN_RPC_HOST=localhost
    export BITCOIN_RPC_PORT=18443
    export BITCOIN_RPC_USER=bitcoin
    export BITCOIN_RPC_PASS=bitcoin
    export MONERO_RPC_HOST=localhost
    export MONERO_RPC_PORT=38082
    
    cd "$(dirname "$0")/.."
    
    python3 -m pytest tests/swaps/integration/ -v --integration -s 2>&1 | tee /tmp/test_output.log
    
    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        echo -e "\n${GREEN}✓ All integration tests passed${NC}"
        return 0
    else
        echo -e "\n${RED}✗ Integration tests failed${NC}"
        echo "Check /tmp/test_output.log for details"
        return 1
    fi
}

# Full swap simulation
simulate_swap() {
    echo -e "\n${YELLOW}Running full swap simulation...${NC}"
    
    PYTHONPATH="$(dirname "$0")/..:$PYTHONPATH" python3 << 'EOF'
import asyncio
import sys
sys.path.insert(0, '.')

from decimal import Decimal
from stealthpay.swaps.coordinator import SwapFactory, SwapConfig
from stealthpay.swaps.btc.rpc_client import create_regtest_client
from stealthpay.swaps.xmr.wallet import create_stagenet_wallet
from stealthpay.swaps.utils.bitcoin import generate_keypair

async def main():
    print("\n=== Full Swap Simulation ===\n")
    
    try:
        # Connect to nodes
        print("1. Connecting to nodes...")
        btc_rpc = create_regtest_client()
        xmr_wallet = create_stagenet_wallet()
        
        btc_height = btc_rpc.get_block_count()
        xmr_address = xmr_wallet.get_address()
        
        print(f"   Bitcoin height: {btc_height}")
        print(f"   XMR address: {xmr_address}")
        
        # Generate addresses
        print("\n2. Generating addresses...")
        alice_btc = btc_rpc.get_new_address()
        bob_btc = btc_rpc.get_new_address()
        
        print(f"   Alice BTC: {alice_btc}")
        print(f"   Bob BTC: {bob_btc}")
        
        # Setup config
        config = SwapConfig(
            btc_amount=Decimal("0.001"),
            xmr_amount=Decimal("0.1"),
            btc_network="regtest",
            xmr_network="stagenet"
        )
        
        print("\n3. Creating swap coordinators...")
        
        # Alice (Seller)
        alice = SwapFactory.create_seller_swap(
            btc_rpc, xmr_wallet,
            config.btc_amount, config.xmr_amount,
            receive_btc_address=alice_btc,
            config=config
        )
        
        # Bob (Buyer)  
        bob = SwapFactory.create_buyer_swap(
            btc_rpc, xmr_wallet,
            config.btc_amount, config.xmr_amount,
            receive_xmr_address="44...bob_xmr...",
            config=config
        )
        
        print(f"   Alice swap ID: {alice.state.swap_id}")
        print(f"   Bob swap ID: {bob.state.swap_id}")
        
        # Generate keys
        print("\n4. Generating cryptographic keys...")
        _, alice_pub = generate_keypair()
        _, bob_pub = generate_keypair()
        
        print(f"   Alice pubkey: {alice_pub.hex()[:40]}...")
        print(f"   Bob pubkey: {bob_pub.hex()[:40]}...")
        
        print("\n5. Creating HTLC...")
        from stealthpay.swaps.btc.htlc import create_simple_htlc_for_swap
        import hashlib
        import secrets
        
        preimage = secrets.token_hex(32)
        preimage_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        
        htlc = create_simple_htlc_for_swap(
            btc_rpc, bob_pub.hex(), alice_pub.hex(),
            config.btc_amount, locktime_hours=24,
            network="regtest"
        )
        
        print(f"   HTLC address: {htlc['address']}")
        print(f"   Preimage hash: {preimage_hash[:40]}...")
        
        print("\n6. Funding HTLC...")
        funding_txid = btc_rpc.fund_htlc_address(htlc['address'], config.btc_amount)
        print(f"   Funding TXID: {funding_txid}")
        
        # Mine block
        btc_rpc._call("generatetoaddress", [1, bob_btc])
        
        print("\n✓ Swap simulation completed successfully!")
        return 0
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

result = asyncio.run(main())
sys.exit(result)
EOF
}

# Main
main() {
    check_nodes
    setup_funds
    
    echo -e "\n${YELLOW}Choose test mode:${NC}"
    echo "1) Full pytest suite"
    echo "2) Quick simulation only"
    echo "3) Both"
    read -p "Choice [1-3]: " choice
    
    case $choice in
        1) run_tests ;;
        2) simulate_swap ;;
        3) run_tests && simulate_swap ;;
        *) echo "Invalid choice"; exit 1 ;;
    esac
}

main "$@"
