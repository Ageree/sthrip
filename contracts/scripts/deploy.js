const { ethers } = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
  const [deployer] = await ethers.getSigners();
  const network = await ethers.provider.getNetwork();
  
  console.log("═══════════════════════════════════════════");
  console.log("  Sthrip Bridge Deployment");
  console.log("═══════════════════════════════════════════");
  console.log("Network:", network.name);
  console.log("Chain ID:", network.chainId.toString());
  console.log("Deployer:", deployer.address);
  console.log("Balance:", ethers.formatEther(await deployer.getBalance()), "ETH");
  console.log();

  // MPC nodes (5 nodes, 3-of-5 threshold)
  // Replace with actual node addresses for production
  const mpcNodes = [
    process.env.MPC_NODE_1 || deployer.address,
    process.env.MPC_NODE_2 || deployer.address,
    process.env.MPC_NODE_3 || deployer.address,
    process.env.MPC_NODE_4 || deployer.address,
    process.env.MPC_NODE_5 || deployer.address,
  ];

  console.log("MPC Nodes:");
  mpcNodes.forEach((node, i) => console.log(`  ${i + 1}. ${node}`));
  console.log();

  // Deploy Insurance Fund
  console.log("Deploying Insurance Fund...");
  const InsuranceFund = await ethers.getContractFactory("InsuranceFund");
  const insuranceFund = await InsuranceFund.deploy(
    ethers.parseEther("100") // max claim amount
  );
  await insuranceFund.waitForDeployment();
  console.log("✓ InsuranceFund:", await insuranceFund.getAddress());

  // Deploy Price Oracle (if on mainnet/testnet with Chainlink)
  let priceOracle;
  const chainlinkEthUsd = network.chainId === 11155111n 
    ? "0x694AA1769357215DE4FAC081bf1f309aDC325306" // Sepolia
    : network.chainId === 1n
    ? "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419" // Mainnet
    : ethers.ZeroAddress;

  if (chainlinkEthUsd !== ethers.ZeroAddress) {
    console.log("Deploying Price Oracle...");
    const PriceOracle = await ethers.getContractFactory("PriceOracle");
    priceOracle = await PriceOracle.deploy(chainlinkEthUsd);
    await priceOracle.waitForDeployment();
    console.log("✓ PriceOracle:", await priceOracle.getAddress());
  }

  // Deploy Bridge
  console.log("Deploying Sthrip Bridge...");
  const feeCollector = process.env.FEE_COLLECTOR || deployer.address;
  
  const SthripBridge = await ethers.getContractFactory("SthripBridge");
  const bridge = await SthripBridge.deploy(
    mpcNodes,
    feeCollector,
    await insuranceFund.getAddress()
  );
  await bridge.waitForDeployment();
  console.log("✓ SthripBridge:", await bridge.getAddress());

  // Configure contracts
  console.log("Configuring contracts...");
  
  // Set bridge address in insurance fund
  await insuranceFund.setBridge(await bridge.getAddress());
  console.log("✓ InsuranceFund bridge set");

  // Fund insurance with initial deposit (optional)
  if (process.env.FUND_INSURANCE === "true") {
    const fundAmount = ethers.parseEther("1");
    await insuranceFund.deposit({ value: fundAmount });
    console.log("✓ InsuranceFund funded with", ethers.formatEther(fundAmount), "ETH");
  }

  // Save deployment info
  const deploymentInfo = {
    network: network.name,
    chainId: network.chainId.toString(),
    timestamp: new Date().toISOString(),
    deployer: deployer.address,
    contracts: {
      bridge: await bridge.getAddress(),
      insuranceFund: await insuranceFund.getAddress(),
      priceOracle: priceOracle ? await priceOracle.getAddress() : null,
    },
    configuration: {
      mpcNodes,
      threshold: 3,
      total: 5,
      feeCollector,
    }
  };

  const deploymentPath = path.join(__dirname, "..", `deployment-${network.name}.json`);
  fs.writeFileSync(deploymentPath, JSON.stringify(deploymentInfo, null, 2));
  console.log("\nDeployment info saved to:", deploymentPath);

  // Print verification commands
  console.log("\n═══════════════════════════════════════════");
  console.log("  Verification Commands");
  console.log("═══════════════════════════════════════════");
  console.log(`npx hardhat verify --network ${network.name} ${await insuranceFund.getAddress()} "${ethers.parseEther("100")}"`);
  if (priceOracle) {
    console.log(`npx hardhat verify --network ${network.name} ${await priceOracle.getAddress()} "${chainlinkEthUsd}"`);
  }
  console.log(`npx hardhat verify --network ${network.name} ${await bridge.getAddress()} "[${mpcNodes.join(",")}]" "${feeCollector}" "${await insuranceFund.getAddress()}"`);

  console.log("\n✅ Deployment complete!");
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
