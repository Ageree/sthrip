const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("SthripBridge", function () {
  let bridge, insurance, oracle;
  let owner, alice, bob, feeCollector;
  let mpcNodes;
  
  const MIN_AMOUNT = ethers.parseEther("0.001");
  const MAX_AMOUNT = ethers.parseEther("100");
  
  beforeEach(async function () {
    [owner, alice, bob, feeCollector, ...mpcNodes] = await ethers.getSigners();
    
    // Deploy Insurance Fund
    const Insurance = await ethers.getContractFactory("InsuranceFund");
    insurance = await Insurance.deploy(ethers.parseEther("100"));
    await insurance.waitForDeployment();
    
    // Deploy Bridge
    const Bridge = await ethers.getContractFactory("SthripBridge");
    const nodeAddresses = mpcNodes.slice(0, 5).map(n => n.address);
    bridge = await Bridge.deploy(
      nodeAddresses,
      feeCollector.address,
      await insurance.getAddress()
    );
    await bridge.waitForDeployment();
    
    // Deploy Oracle
    const PriceOracle = await ethers.getContractFactory("PriceOracle");
    // Mock Chainlink feed address (replace with real one for mainnet)
    const mockFeed = "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419";
    oracle = await PriceOracle.deploy(mockFeed);
    await oracle.waitForDeployment();
  });
  
  describe("Deployment", function () {
    it("Should set correct roles", async function () {
      expect(await bridge.hasRole(await bridge.ADMIN_ROLE(), owner.address)).to.be.true;
      expect(await bridge.hasRole(await bridge.MPC_ROLE(), mpcNodes[0].address)).to.be.true;
    });
    
    it("Should set correct parameters", async function () {
      expect(await bridge.mpcThreshold()).to.equal(3);
      expect(await bridge.mpcTotal()).to.equal(5);
      expect(await bridge.protocolFeeBps()).to.equal(50);
    });
  });
  
  describe("Lock", function () {
    it("Should lock ETH and emit event", async function () {
      const amount = ethers.parseEther("0.1");
      const duration = 3600; // 1 hour
      const xmrAddress = "44testMoneroAddressLongEnoughForValidation1234567890123456789012345678901234567890";
      const merkleRoot = ethers.randomBytes(32);
      
      await expect(
        bridge.connect(alice).lock(xmrAddress, duration, merkleRoot, { value: amount })
      )
        .to.emit(bridge, "Locked")
        .withArgs(
          await getLockId(alice, xmrAddress, amount, bridge),
          alice.address,
          amount - (amount * 50n / 10000n), // Amount minus fee
          xmrAddress,
          await getTimestamp() + duration,
          merkleRoot
        );
    });
    
    it("Should reject zero amount", async function () {
      await expect(
        bridge.connect(alice).lock("44test", 3600, ethers.ZeroHash, { value: 0 })
      ).to.be.revertedWith("Amount below minimum");
    });
    
    it("Should reject invalid duration", async function () {
      const amount = ethers.parseEther("0.1");
      
      await expect(
        bridge.connect(alice).lock("44test", 100, ethers.ZeroHash, { value: amount })
      ).to.be.revertedWith("Invalid duration");
    });
    
    it("Should calculate fee correctly", async function () {
      const amount = ethers.parseEther("1");
      const expectedFee = amount * 50n / 10000n; // 0.5%
      
      await bridge.connect(alice).lock(
        "44testMoneroAddressLongEnoughForValidation1234567890123456789012345678901234567890",
        3600,
        ethers.randomBytes(32),
        { value: amount }
      );
      
      expect(await bridge.totalFees()).to.equal(expectedFee);
    });
  });
  
  describe("Claim", function () {
    let lockId;
    const amount = ethers.parseEther("0.1");
    
    beforeEach(async function () {
      const xmrAddress = "44testMoneroAddressLongEnoughForValidation1234567890123456789012345678901234567890";
      const merkleRoot = ethers.randomBytes(32);
      
      const tx = await bridge.connect(alice).lock(xmrAddress, 3600, merkleRoot, { value: amount });
      const receipt = await tx.wait();
      
      const event = receipt.logs.find(l => l.fragment?.name === "Locked");
      lockId = event.args[0];
    });
    
    it("Should allow claim with valid MPC signature", async function () {
      const sig = ethers.randomBytes(96); // Placeholder BLS sig
      const txHash = ethers.randomBytes(32);
      
      await expect(
        bridge.connect(mpcNodes[0]).claim(lockId, sig, bob.address, txHash)
      )
        .to.emit(bridge, "Claimed")
        .withArgs(lockId, bob.address, amount - (amount * 50n / 10000n), txHash);
    });
    
    it("Should reject double claim", async function () {
      const sig = ethers.randomBytes(96);
      const txHash = ethers.randomBytes(32);
      
      await bridge.connect(mpcNodes[0]).claim(lockId, sig, bob.address, txHash);
      
      await expect(
        bridge.connect(mpcNodes[0]).claim(lockId, sig, bob.address, ethers.randomBytes(32))
      ).to.be.revertedWith("Already claimed");
    });
    
    it("Should prevent non-MPC from claiming", async function () {
      const sig = ethers.randomBytes(96);
      const txHash = ethers.randomBytes(32);
      
      await expect(
        bridge.connect(alice).claim(lockId, sig, bob.address, txHash)
      ).to.be.reverted;
    });
  });
  
  describe("Refund", function () {
    let lockId;
    const amount = ethers.parseEther("0.1");
    
    beforeEach(async function () {
      const xmrAddress = "44testMoneroAddressLongEnoughForValidation1234567890123456789012345678901234567890";
      
      const tx = await bridge.connect(alice).lock(
        xmrAddress,
        1, // 1 second duration for testing
        ethers.randomBytes(32),
        { value: amount }
      );
      const receipt = await tx.wait();
      const event = receipt.logs.find(l => l.fragment?.name === "Locked");
      lockId = event.args[0];
    });
    
    it("Should allow refund after expiry", async function () {
      // Wait for lock to expire
      await ethers.provider.send("evm_increaseTime", [2]);
      await ethers.provider.send("evm_mine");
      
      const balanceBefore = await ethers.provider.getBalance(alice.address);
      
      await expect(bridge.connect(alice).refund(lockId))
        .to.emit(bridge, "Refunded")
        .withArgs(lockId, alice.address, amount - (amount * 50n / 10000n));
    });
    
    it("Should reject early refund", async function () {
      await expect(
        bridge.connect(alice).refund(lockId)
      ).to.be.revertedWith("Lock active");
    });
    
    it("Should reject non-sender refund", async function () {
      await ethers.provider.send("evm_increaseTime", [2]);
      await ethers.provider.send("evm_mine");
      
      await expect(
        bridge.connect(bob).refund(lockId)
      ).to.be.revertedWith("Not sender");
    });
  });
  
  describe("Emergency", function () {
    it("Should allow admin to pause", async function () {
      await bridge.connect(owner).emergencyPause();
      expect(await bridge.paused()).to.be.true;
    });
    
    it("Should prevent non-admin from pausing", async function () {
      await expect(bridge.connect(alice).emergencyPause())
        .to.be.reverted;
    });
    
    it("Should prevent operations when paused", async function () {
      await bridge.connect(owner).emergencyPause();
      
      await expect(
        bridge.connect(alice).lock("44test", 3600, ethers.ZeroHash, { value: ethers.parseEther("0.1") })
      ).to.be.revertedWithCustomError(bridge, "EnforcedPause");
    });
  });
  
  describe("Admin Functions", function () {
    it("Should update MPC threshold", async function () {
      await bridge.connect(owner).updateThreshold(4);
      expect(await bridge.mpcThreshold()).to.equal(4);
    });
    
    it("Should reject threshold above total", async function () {
      await expect(
        bridge.connect(owner).updateThreshold(10)
      ).to.be.revertedWith("Threshold exceeds total");
    });
    
    it("Should update protocol fee", async function () {
      await bridge.connect(owner).updateProtocolFee(100); // 1%
      expect(await bridge.protocolFeeBps()).to.equal(100);
    });
    
    it("Should reject fee above 5%", async function () {
      await expect(
        bridge.connect(owner).updateProtocolFee(600)
      ).to.be.revertedWith("Fee too high");
    });
  });
});

// Helper functions
async function getTimestamp() {
  const block = await ethers.provider.getBlock("latest");
  return block.timestamp;
}

async function getLockId(sender, xmrAddress, amount, bridge) {
  const nonce = await bridge.nonces(sender.address);
  const timestamp = await getTimestamp();
  
  return ethers.keccak256(
    ethers.AbiCoder.defaultAbiCoder().encode(
      ["address", "uint256", "uint256", "string", "uint256"],
      [sender.address, timestamp, amount, xmrAddress, nonce]
    )
  );
}
