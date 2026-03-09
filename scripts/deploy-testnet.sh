#!/bin/bash
# Deploy Sthrip to Sepolia Testnet

set -e

NETWORK="${1:-sepolia}"
CONTRACTS_DIR="$(dirname "$0")/../contracts"
ENV_FILE="$(dirname "$0")/../.env"

echo "═══════════════════════════════════════════════════════════"
echo "  Sthrip Testnet Deployment"
echo "═══════════════════════════════════════════════════════════"
echo "Network: ${NETWORK}"
echo

# Load environment variables
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

# Check prerequisites
echo "Checking prerequisites..."

if ! command -v npx &> /dev/null; then
    echo "❌ Hardhat/npx not found. Install with: npm install"
    exit 1
fi

if [ -z "$PRIVATE_KEY" ]; then
    echo "❌ PRIVATE_KEY not set in environment"
    echo "   Set it in .env file or export PRIVATE_KEY=0x..."
    exit 1
fi

if [ -z "$SEPOLIA_RPC" ]; then
    echo "❌ SEPOLIA_RPC not set in environment"
    echo "   Get a free RPC from: https://www.alchemy.com or https://infura.io"
    exit 1
fi

echo "✓ Prerequisites met"
echo

# Build contracts
echo "Building contracts..."
cd "$CONTRACTS_DIR"
npm run compile
echo "✓ Contracts compiled"
echo

# Run tests
echo "Running contract tests..."
npm test
echo "✓ Tests passed"
echo

# Deploy
echo "Deploying to ${NETWORK}..."
npx hardhat run scripts/deploy.js --network "$NETWORK"
echo

# Check if deployment info was created
if [ -f "deployment-${NETWORK}.json" ]; then
    echo "✓ Deployment complete!"
    echo
    echo "Deployment Info:"
    cat "deployment-${NETWORK}.json"
    echo
    
    # Extract addresses for docker-compose
    BRIDGE_CONTRACT=$(cat "deployment-${NETWORK}.json" | grep '"bridge"' | sed 's/.*: "\(.*\)".*/\1/')
    
    echo "═══════════════════════════════════════════════════════════"
    echo "  Update docker-compose.testnet.yml:"
    echo "═══════════════════════════════════════════════════════════"
    echo "BRIDGE_CONTRACT=${BRIDGE_CONTRACT}"
    echo
    echo "To start MPC nodes:"
    echo "  BRIDGE_CONTRACT=${BRIDGE_CONTRACT} docker-compose -f docker-compose.testnet.yml up -d"
else
    echo "❌ Deployment may have failed - no deployment info found"
    exit 1
fi
