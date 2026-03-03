#!/bin/bash
# Run all tests for StealthPay

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo "═══════════════════════════════════════════════════════════════════"
echo "  STEALTHPAY COMPLETE TEST SUITE"
echo "═══════════════════════════════════════════════════════════════════"
echo

TESTS_PASSED=0
TESTS_FAILED=0

run_test() {
    local name="$1"
    local cmd="$2"
    
    echo
    echo -e "${BLUE}Running: $name${NC}"
    echo "────────────────────────────────────────────────────────────────"
    
    if eval "$cmd"; then
        echo -e "${GREEN}✓ $name PASSED${NC}"
        ((TESTS_PASSED++))
    else
        echo -e "${RED}✗ $name FAILED${NC}"
        ((TESTS_FAILED++))
        
        read -p "Continue despite failure? [y/N]: " continue
        if [[ $continue != "y" && $continue != "Y" ]]; then
            echo "Stopping tests..."
            exit 1
        fi
    fi
}

# Phase 1: Component tests (no money)
run_test "Component Tests" "python3 scripts/test_components.py"

# Phase 2: Contract tests
cd contracts
run_test "Contract Unit Tests" "npm test"
cd ..

# Phase 3: Check environment
echo
read -p "Run E2E test with real Sepolia ETH? (costs ~0.001 test ETH) [y/N]: " run_e2e

if [[ $run_e2e == "y" || $run_e2e == "Y" ]]; then
    # Check if environment configured
    if [ ! -f .env ]; then
        echo -e "${RED}ERROR: .env file not found${NC}"
        echo "Run: ./scripts/setup_test_env.sh first"
        exit 1
    fi
    
    source .env
    
    if [ "$TEST_PRIVATE_KEY" == "0xYOUR_PRIVATE_KEY_HERE" ]; then
        echo -e "${RED}ERROR: TEST_PRIVATE_KEY not configured${NC}"
        echo "Edit .env file with your test private key"
        exit 1
    fi
    
    if [ "$BRIDGE_CONTRACT" == "0xDEPLOYED_BRIDGE_ADDRESS" ]; then
        echo -e "${YELLOW}WARNING: BRIDGE_CONTRACT not configured${NC}"
        read -p "Deploy contracts first? [y/N]: " deploy
        if [[ $deploy == "y" || $deploy == "Y" ]]; then
            cd contracts
            npx hardhat run scripts/deploy.js --network sepolia
            cd ..
            echo -e "${YELLOW}Update BRIDGE_CONTRACT in .env and re-run${NC}"
            exit 0
        fi
    fi
    
    # Phase 3: E2E test
    run_test "E2E Integration Test" "python3 scripts/test_e2e_sepolia.py"
else
    echo -e "${YELLOW}Skipping E2E tests (user choice)${NC}"
fi

# Summary
echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  TEST SUMMARY"
echo "═══════════════════════════════════════════════════════════════════"
echo -e "${GREEN}Passed: $TESTS_PASSED${NC}"
echo -e "${RED}Failed: $TESTS_FAILED${NC}"
echo

if [ $TESTS_FAILED -eq 0 ]; then
    echo -e "${GREEN}✅ ALL TESTS PASSED!${NC}"
    echo
    echo "Next steps:"
    echo "  - Review test results above"
    echo "  - Check transaction hashes on Sepolia explorer"
    echo "  - If all good, consider security audit"
    exit 0
else
    echo -e "${RED}❌ SOME TESTS FAILED${NC}"
    echo
    echo "Please fix issues before proceeding."
    exit 1
fi
