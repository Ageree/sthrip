#!/bin/bash
# Prepare Sthrip for security audit

set -e

echo "═══════════════════════════════════════════════════════════"
echo "  Sthrip Security Audit Preparation"
echo "═══════════════════════════════════════════════════════════"
echo

AUDIT_DIR="audit-package"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PACKAGE_NAME="sthrip-audit-${TIMESTAMP}.zip"

# Clean previous audit packages
rm -rf ${AUDIT_DIR} *.zip

# Create directory structure
echo "Creating audit package structure..."
mkdir -p ${AUDIT_DIR}/{01-source-code/{contracts,tss-service,python-client},02-documentation,03-test-results,04-deployment,05-scripts}

# Copy source code
echo "Copying source code..."
cp -r ../contracts/*.sol ${AUDIT_DIR}/01-source-code/contracts/ 2>/dev/null || true
cp -r ../tss-service/*.go ../tss-service/**/*.go ${AUDIT_DIR}/01-source-code/tss-service/ 2>/dev/null || true
cp -r ../sthrip/bridge/tss_client/*.py ${AUDIT_DIR}/01-source-code/python-client/ 2>/dev/null || true

# Copy documentation
echo "Copying documentation..."
cp ../docs/*.md ${AUDIT_DIR}/02-documentation/ 2>/dev/null || true
cp ../*.md ${AUDIT_DIR}/02-documentation/ 2>/dev/null || true

# Run tests and copy results
echo "Running test suite..."
cd ..

# Python tests
if [ -d "tests" ]; then
    echo "Running Python tests..."
    python -m pytest tests/ -v --cov=sthrip --cov-report=html --cov-report=term -x > ${AUDIT_DIR}/03-test-results/pytest-output.txt 2>&1 || true
    cp -r htmlcov ${AUDIT_DIR}/03-test-results/ 2>/dev/null || true
fi

# Contract tests
if [ -d "contracts" ]; then
    echo "Running contract tests..."
    cd contracts
    npx hardhat test 2>&1 | tee ../${AUDIT_DIR}/03-test-results/hardhat-output.txt || true
    cd ..
fi

# Copy deployment configs
echo "Copying deployment configurations..."
cp docker-compose*.yml Dockerfile* ${AUDIT_DIR}/04-deployment/ 2>/dev/null || true
cp -r monitoring/ ${AUDIT_DIR}/04-deployment/ 2>/dev/null || true

# Copy scripts
echo "Copying scripts..."
cp -r scripts/*.py scripts/*.sh ${AUDIT_DIR}/05-scripts/ 2>/dev/null || true

# Create audit brief
cat > ${AUDIT_DIR}/05-audit-brief.md << 'EOF'
# Sthrip Security Audit Brief

## Project Overview
**Name**: Sthrip  
**Type**: Cross-chain bridge (ETH ↔ XMR)  
**MPC**: 3-of-5 threshold signature  
**Contracts**: Solidity 0.8.19  
**TSS**: Go (bnb-chain/tss-lib)

## Scope

### In Scope
1. Smart Contracts
   - SthripBridge.sol
   - InsuranceFund.sol
   - PriceOracle.sol

2. TSS Implementation
   - Go gRPC service
   - DKG protocol
   - Signing protocol
   - Python client

3. Infrastructure
   - P2P communication
   - HSM integration
   - Deployment configuration

### Out of Scope
- Ethereum/Monero protocol layer
- Third-party dependencies (tss-lib, OpenZeppelin)
- Frontend applications
- User wallet security

## Focus Areas

### Critical
1. Fund safety mechanisms
2. Access control
3. Signature verification
4. Key management

### High
1. Reentrancy protection
2. Price oracle manipulation
3. Front-running
4. P2P security

### Medium
1. Gas optimization
2. Error handling
3. Upgrade mechanisms
4. Monitoring

## Known Issues
1. BLS verification is placeholder (marked with TODO)
2. P2P network needs formal verification
3. Limited hardware security testing

## Timeline
- **Start**: [To be determined]
- **Duration**: 6-8 weeks
- **Report Due**: [To be determined]

## Contacts
- **Technical Lead**: [Email]
- **Security**: security@sthrip.io
- **Emergency**: +[Phone]

## Deliverables Expected
1. Executive summary
2. Detailed findings with severity
3. Proof of concept code (where applicable)
4. Remediation guidance
5. Verification of fixes

## Compensation
- **Budget**: $65,000-105,000
- **Payment**: 50% upfront, 50% on report delivery
- **Bug Bounty**: Additional rewards for critical finds
EOF

# Create zip archive
echo
echo "Creating archive..."
zip -r ${PACKAGE_NAME} ${AUDIT_DIR}/

# Print summary
echo
echo "═══════════════════════════════════════════════════════════"
echo "  Audit Package Complete!"
echo "═══════════════════════════════════════════════════════════"
echo
echo "Package: ${PACKAGE_NAME}"
echo "Size: $(du -h ${PACKAGE_NAME} | cut -f1)"
echo
echo "Contents:"
find ${AUDIT_DIR} -type f | wc -l | xargs echo "  Files:"
find ${AUDIT_DIR} -type d | wc -l | xargs echo "  Directories:"
echo
echo "Next steps:"
echo "  1. Review ${AUDIT_DIR}/05-audit-brief.md"
echo "  2. Verify all source code is included"
echo "  3. Check test results in 03-test-results/"
echo "  4. Send ${PACKAGE_NAME} to auditors"
echo
