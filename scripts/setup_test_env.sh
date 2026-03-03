#!/bin/bash
# Setup test environment for StealthPay

set -e

echo "═══════════════════════════════════════════════════════════════════"
echo "  StealthPay Test Environment Setup"
echo "═══════════════════════════════════════════════════════════════════"
echo

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

# Check prerequisites
echo "🔍 Checking prerequisites..."

# Check Python
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 not found. Please install Python 3.10+"
    exit 1
fi
PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
print_success "Python $PYTHON_VERSION"

# Check Node.js
if ! command -v node &> /dev/null; then
    print_error "Node.js not found. Please install Node.js 18+"
    exit 1
fi
NODE_VERSION=$(node --version)
print_success "Node.js $NODE_VERSION"

# Check Docker (optional)
if command -v docker &> /dev/null; then
    print_success "Docker found"
    HAS_DOCKER=true
else
    print_warning "Docker not found (optional, for MPC nodes)"
    HAS_DOCKER=false
fi

echo

# Install Python dependencies
echo "📦 Installing Python dependencies..."
pip3 install -q web3 eth-account cryptography aiohttp 2>/dev/null || pip install -q web3 eth-account cryptography aiohttp
print_success "Python packages installed"

# Install Node dependencies
echo
if [ -d "contracts" ]; then
    echo "📦 Installing Node.js dependencies..."
    cd contracts
    npm install --silent 2>/dev/null || npm install
    print_success "Node packages installed"
    cd ..
fi

echo

# Create .env file
echo "📝 Creating environment configuration..."

if [ ! -f .env ]; then
    cat > .env << 'EOF'
# StealthPay Test Environment
# Get free Sepolia ETH from: https://sepolia-faucet.pk910.de/

# Sepolia Configuration
SEPOLIA_RPC=https://rpc.sepolia.org
TEST_PRIVATE_KEY=0xYOUR_PRIVATE_KEY_HERE
BRIDGE_CONTRACT=0xDEPLOYED_BRIDGE_ADDRESS

# Optional: Infura/Alchemy for better reliability
# SEPOLIA_RPC=https://sepolia.infura.io/v3/YOUR_KEY

# TSS Service
TSS_ENDPOINT=localhost:50051

# MPC Node (for testing)
NODE_ID=test-node-1
NETWORK=testnet
EOF
    print_warning "Created .env file - please edit with your values!"
else
    print_success ".env file already exists"
fi

echo

# Check for test funds
echo "💰 Checking test funds..."
if [ -f .env ]; then
    source .env
    if [ -n "$TEST_PRIVATE_KEY" ] && [ "$TEST_PRIVATE_KEY" != "0xYOUR_PRIVATE_KEY_HERE" ]; then
        print_success "Private key configured"
        
        # Try to check balance (optional)
        python3 << 'PYEOF'
import os
import sys

try:
    from web3 import Web3
    
    rpc = os.getenv("SEPOLIA_RPC", "https://rpc.sepolia.org")
    key = os.getenv("TEST_PRIVATE_KEY", "")
    
    if key and key != "0xYOUR_PRIVATE_KEY_HERE":
        w3 = Web3(Web3.HTTPProvider(rpc))
        from eth_account import Account
        acc = Account.from_key(key)
        balance = w3.eth.get_balance(acc.address)
        print(f"   Sepolia balance: {w3.from_wei(balance, 'ether'):.4f} ETH")
        
        if balance > 0:
            print("   ✅ You have test funds!")
        else:
            print("   ⚠️  No test funds yet. Get from: https://sepolia-faucet.pk910.de/")
except Exception as e:
    print(f"   Could not check balance: {e}")
PYEOF
    else
        print_warning "No private key configured"
        echo "   Get free Sepolia ETH and add private key to .env"
    fi
else
    print_warning ".env file not found"
fi

echo

# Create test directories
echo "📁 Creating test directories..."
mkdir -p test-results
mkdir -p test-logs
mkdir -p ceremony-output
print_success "Directories created"

echo

# Build TSS service (if Go available)
if command -v go &> /dev/null; then
    echo "🔨 Building TSS service..."
    if [ -d "tss-service" ]; then
        cd tss-service
        if make build 2>/dev/null; then
            print_success "TSS service built"
        else
            print_warning "Could not build TSS service (may need Go modules setup)"
        fi
        cd ..
    fi
else
    print_warning "Go not found - TSS service won't be built"
fi

echo

# Summary
echo "═══════════════════════════════════════════════════════════════════"
echo "  Setup Complete!"
echo "═══════════════════════════════════════════════════════════════════"
echo
echo "Next steps:"
echo
echo "1. Get Sepolia test ETH:"
echo "   https://sepolia-faucet.pk910.de/"
echo "   https://www.infura.io/faucet/sepolia"
echo
echo "2. Edit .env file with your:"
echo "   - TEST_PRIVATE_KEY (with Sepolia ETH)"
echo "   - SEPOLIA_RPC (optional, for better reliability)"
echo
echo "3. Run component tests (no money):"
echo "   python3 scripts/test_components.py"
echo
echo "4. Deploy contracts to Sepolia:"
echo "   cd contracts && npx hardhat run scripts/deploy.js --network sepolia"
echo
echo "5. Run E2E test (small amount):"
echo "   python3 scripts/test_e2e_sepolia.py"
echo
echo "⚠️  IMPORTANT:"
echo "   - Only use testnet (Sepolia) - funds are worthless"
echo "   - Never use mainnet private keys!"
echo "   - Wait for security audit before mainnet"
echo

print_success "Environment ready for testing!"
