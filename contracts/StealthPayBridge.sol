// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import "@openzeppelin/contracts/security/Pausable.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * @title StealthPayBridge
 * @notice Cross-chain bridge between Ethereum and Monero using MPC
 * @dev Implements HTLC pattern with threshold signatures
 */
contract StealthPayBridge is ReentrancyGuard, Pausable, AccessControl {
    // ============ Roles ============
    bytes32 public constant MPC_ROLE = keccak256("MPC_ROLE");
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");
    bytes32 public constant OPERATOR_ROLE = keccak256("OPERATOR_ROLE");

    // ============ Structs ============
    struct Lock {
        address sender;
        uint256 amount;
        string xmrAddress;
        uint256 unlockTime;
        bool claimed;
        bool refunded;
        bytes32 mpcMerkleRoot;
        uint256 createdAt;
    }

    struct BridgeRequest {
        address sender;
        uint256 ethAmount;
        uint256 xmrAmount;
        string xmrAddress;
        bytes32 txHash;
        bool completed;
        uint256 createdAt;
    }

    // ============ State Variables ============
    mapping(bytes32 => Lock) public locks;
    mapping(address => uint256) public nonces;
    mapping(bytes32 => BridgeRequest) public bridgeRequests;
    mapping(bytes32 => bool) public usedHashes;
    
    uint256 public constant MIN_LOCK_DURATION = 1 hours;
    uint256 public constant MAX_LOCK_DURATION = 7 days;
    uint256 public constant MIN_AMOUNT = 0.001 ether;
    uint256 public constant MAX_AMOUNT = 100 ether;
    
    uint256 public mpcThreshold = 3;
    uint256 public mpcTotal = 5;
    uint256 public protocolFeeBps = 50; // 0.5%
    uint256 public totalFees;
    
    address public feeCollector;
    address public insuranceFund;

    // ============ Events ============
    event Locked(
        bytes32 indexed lockId,
        address indexed sender,
        uint256 amount,
        string xmrAddress,
        uint256 unlockTime,
        bytes32 mpcMerkleRoot
    );
    
    event Claimed(
        bytes32 indexed lockId,
        address indexed recipient,
        uint256 amount,
        bytes32 indexed txHash
    );
    
    event Refunded(
        bytes32 indexed lockId,
        address indexed sender,
        uint256 amount
    );
    
    event BridgeInitiated(
        bytes32 indexed requestId,
        address indexed sender,
        uint256 ethAmount,
        uint256 xmrAmount,
        string xmrAddress
    );
    
    event BridgeCompleted(
        bytes32 indexed requestId,
        bytes32 indexed txHash,
        uint256 ethAmount
    );
    
    event EmergencyPaused(address indexed admin);
    event EmergencyUnpaused(address indexed admin);
    event MPCNodeUpdated(address indexed node, bool added);
    event ThresholdUpdated(uint256 newThreshold);
    event FeeUpdated(uint256 newFeeBps);

    // ============ Modifiers ============
    modifier onlyMPC() {
        require(hasRole(MPC_ROLE, msg.sender), "Not MPC node");
        _;
    }
    
    modifier onlyAdmin() {
        require(hasRole(ADMIN_ROLE, msg.sender), "Not admin");
        _;
    }

    // ============ Constructor ============
    constructor(
        address[] memory mpcNodes,
        address _feeCollector,
        address _insuranceFund
    ) {
        require(mpcNodes.length >= mpcThreshold, "Insufficient MPC nodes");
        require(_feeCollector != address(0), "Invalid fee collector");
        
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(ADMIN_ROLE, msg.sender);
        _grantRole(OPERATOR_ROLE, msg.sender);
        
        for (uint i = 0; i < mpcNodes.length; i++) {
            require(mpcNodes[i] != address(0), "Invalid MPC node");
            _grantRole(MPC_ROLE, mpcNodes[i]);
        }
        
        feeCollector = _feeCollector;
        insuranceFund = _insuranceFund;
    }

    // ============ External Functions ============
    
    /**
     * @notice Lock ETH to receive XMR
     * @param xmrAddress Monero address to receive funds
     * @param duration Lock duration (1 hour to 7 days)
     * @param mpcMerkleRoot Merkle root of MPC committee
     */
    function lock(
        string calldata xmrAddress,
        uint256 duration,
        bytes32 mpcMerkleRoot
    ) external payable nonReentrant whenNotPaused returns (bytes32 lockId) {
        // Validate inputs
        require(msg.value >= MIN_AMOUNT, "Amount below minimum");
        require(msg.value <= MAX_AMOUNT, "Amount above maximum");
        require(bytes(xmrAddress).length >= 95 && bytes(xmrAddress).length <= 106, 
                "Invalid XMR address length");
        require(
            duration >= MIN_LOCK_DURATION && duration <= MAX_LOCK_DURATION,
            "Invalid duration"
        );
        require(mpcMerkleRoot != bytes32(0), "Invalid Merkle root");
        
        // Calculate fee
        uint256 fee = (msg.value * protocolFeeBps) / 10000;
        uint256 netAmount = msg.value - fee;
        totalFees += fee;
        
        // Generate unique lock ID
        lockId = keccak256(abi.encodePacked(
            msg.sender,
            block.timestamp,
            msg.value,
            xmrAddress,
            nonces[msg.sender]++
        ));
        
        require(locks[lockId].amount == 0, "Lock exists");
        
        // Store lock
        locks[lockId] = Lock({
            sender: msg.sender,
            amount: netAmount,
            xmrAddress: xmrAddress,
            unlockTime: block.timestamp + duration,
            claimed: false,
            refunded: false,
            mpcMerkleRoot: mpcMerkleRoot,
            createdAt: block.timestamp
        });
        
        emit Locked(
            lockId,
            msg.sender,
            netAmount,
            xmrAddress,
            block.timestamp + duration,
            mpcMerkleRoot
        );
        
        return lockId;
    }
    
    /**
     * @notice Claim locked ETH with MPC signature
     * @param lockId Lock identifier
     * @param mpcSignature Threshold signature from MPC committee
     * @param recipient Address to receive ETH
     * @param txHash Monero transaction hash (for verification)
     */
    function claim(
        bytes32 lockId,
        bytes calldata mpcSignature,
        address recipient,
        bytes32 txHash
    ) external nonReentrant whenNotPaused {
        Lock storage lock = locks[lockId];
        
        // Validate state
        require(lock.amount > 0, "Lock not found");
        require(!lock.claimed, "Already claimed");
        require(!lock.refunded, "Already refunded");
        require(block.timestamp < lock.unlockTime, "Lock expired");
        require(recipient != address(0), "Invalid recipient");
        require(!usedHashes[txHash], "Tx hash already used");
        
        // Verify MPC signature
        require(
            verifyMPCSignature(lockId, recipient, txHash, mpcSignature),
            "Invalid MPC signature"
        );
        
        // Mark as claimed
        lock.claimed = true;
        usedHashes[txHash] = true;
        
        // Transfer funds
        (bool success, ) = recipient.call{value: lock.amount}("");
        require(success, "Transfer failed");
        
        emit Claimed(lockId, recipient, lock.amount, txHash);
    }
    
    /**
     * @notice Refund expired lock
     * @param lockId Lock identifier
     */
    function refund(bytes32 lockId) external nonReentrant {
        Lock storage lock = locks[lockId];
        
        // Validate state
        require(lock.amount > 0, "Lock not found");
        require(!lock.claimed, "Already claimed");
        require(!lock.refunded, "Already refunded");
        require(block.timestamp >= lock.unlockTime, "Lock active");
        require(msg.sender == lock.sender, "Not sender");
        
        // Mark as refunded
        lock.refunded = true;
        
        // Transfer funds back
        (bool success, ) = lock.sender.call{value: lock.amount}("");
        require(success, "Transfer failed");
        
        emit Refunded(lockId, lock.sender, lock.amount);
    }
    
    /**
     * @notice Initiate bridge from XMR to ETH
     * @param ethAmount Amount of ETH to receive
     * @param xmrAmount Amount of XMR sent
     * @param xmrAddress Sender's XMR address
     */
    function initiateBridge(
        uint256 ethAmount,
        uint256 xmrAmount,
        string calldata xmrAddress
    ) external onlyMPC whenNotPaused returns (bytes32 requestId) {
        require(ethAmount > 0, "Invalid ETH amount");
        require(xmrAmount > 0, "Invalid XMR amount");
        
        requestId = keccak256(abi.encodePacked(
            msg.sender,
            block.timestamp,
            ethAmount,
            xmrAmount,
            xmrAddress
        ));
        
        bridgeRequests[requestId] = BridgeRequest({
            sender: msg.sender,
            ethAmount: ethAmount,
            xmrAmount: xmrAmount,
            xmrAddress: xmrAddress,
            txHash: bytes32(0),
            completed: false,
            createdAt: block.timestamp
        });
        
        emit BridgeInitiated(requestId, msg.sender, ethAmount, xmrAmount, xmrAddress);
        
        return requestId;
    }
    
    /**
     * @notice Complete bridge with MPC approval
     */
    function completeBridge(
        bytes32 requestId,
        bytes32 txHash,
        bytes calldata mpcSignature
    ) external onlyMPC nonReentrant {
        BridgeRequest storage request = bridgeRequests[requestId];
        
        require(request.ethAmount > 0, "Request not found");
        require(!request.completed, "Already completed");
        require(!usedHashes[txHash], "Tx hash already used");
        require(
            verifyMPCSignature(requestId, request.sender, txHash, mpcSignature),
            "Invalid signature"
        );
        
        request.completed = true;
        request.txHash = txHash;
        usedHashes[txHash] = true;
        
        emit BridgeCompleted(requestId, txHash, request.ethAmount);
    }

    // ============ View Functions ============
    
    /**
     * @notice Get lock details
     */
    function getLock(bytes32 lockId) external view returns (Lock memory) {
        return locks[lockId];
    }
    
    /**
     * @notice Check if lock is claimable
     */
    function isClaimable(bytes32 lockId) external view returns (bool) {
        Lock storage lock = locks[lockId];
        return lock.amount > 0 && 
               !lock.claimed && 
               !lock.refunded && 
               block.timestamp < lock.unlockTime;
    }
    
    /**
     * @notice Check if lock is refundable
     */
    function isRefundable(bytes32 lockId) external view returns (bool) {
        Lock storage lock = locks[lockId];
        return lock.amount > 0 && 
               !lock.claimed && 
               !lock.refunded && 
               block.timestamp >= lock.unlockTime;
    }

    // ============ Admin Functions ============
    
    function emergencyPause() external onlyAdmin {
        _pause();
        emit EmergencyPaused(msg.sender);
    }
    
    function emergencyUnpause() external onlyAdmin {
        _unpause();
        emit EmergencyUnpaused(msg.sender);
    }
    
    function updateMPCNode(address node, bool add) external onlyAdmin {
        require(node != address(0), "Invalid address");
        if (add) {
            _grantRole(MPC_ROLE, node);
        } else {
            _revokeRole(MPC_ROLE, node);
        }
        emit MPCNodeUpdated(node, add);
    }
    
    function updateThreshold(uint256 newThreshold) external onlyAdmin {
        require(newThreshold <= mpcTotal, "Threshold exceeds total");
        require(newThreshold >= 2, "Threshold too low");
        mpcThreshold = newThreshold;
        emit ThresholdUpdated(newThreshold);
    }
    
    function updateProtocolFee(uint256 newFeeBps) external onlyAdmin {
        require(newFeeBps <= 500, "Fee too high"); // Max 5%
        protocolFeeBps = newFeeBps;
        emit FeeUpdated(newFeeBps);
    }
    
    function collectFees() external onlyAdmin {
        require(feeCollector != address(0), "Invalid fee collector");
        uint256 amount = totalFees;
        totalFees = 0;
        
        (bool success, ) = feeCollector.call{value: amount}("");
        require(success, "Fee transfer failed");
    }
    
    function rescueTokens(
        address token,
        address to,
        uint256 amount
    ) external onlyAdmin {
        require(to != address(0), "Invalid recipient");
        // ERC20 rescue implementation
        (bool success, ) = token.call(
            abi.encodeWithSelector(bytes4(keccak256("transfer(address,uint256)")), to, amount)
        );
        require(success, "Token rescue failed");
    }

    // ============ Internal Functions ============
    
    /**
     * @notice Verify MPC threshold signature
     * @dev Placeholder - implement BLS or Schnorr threshold verification
     */
    function verifyMPCSignature(
        bytes32 lockId,
        address recipient,
        bytes32 txHash,
        bytes calldata signature
    ) internal view returns (bool) {
        // TODO: Implement BLS12-381 threshold signature verification
        // For now, verify length and format
        require(signature.length == 96, "Invalid signature length"); // BLS sig size
        
        // In production:
        // 1. Aggregate public keys of signing parties
        // 2. Verify signature against aggregated key
        // 3. Check threshold is met
        
        // Placeholder - always returns true for testing
        return true;
    }
    
    receive() external payable {
        revert("Use lock() function");
    }
}
