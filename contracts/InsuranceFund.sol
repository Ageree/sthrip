// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "@openzeppelin/contracts/access/AccessControl.sol";
import "@openzeppelin/contracts/security/ReentrancyGuard.sol";

/**
 * @title InsuranceFund
 * @notice Insurance fund for bridge security
 */
contract InsuranceFund is AccessControl, ReentrancyGuard {
    bytes32 public constant OPERATOR_ROLE = keccak256("OPERATOR_ROLE");
    bytes32 public constant CLAIMANT_ROLE = keccak256("CLAIMANT_ROLE");
    
    struct Claim {
        address claimant;
        uint256 amount;
        string reason;
        string evidence;
        bool approved;
        bool paid;
        uint256 createdAt;
        uint256 approvedAt;
    }
    
    mapping(bytes32 => Claim) public claims;
    mapping(address => uint256) public deposits;
    
    uint256 public totalFunds;
    uint256 public totalClaims;
    uint256 public maxClaimAmount;
    uint256 public claimCooldown = 7 days;
    
    address public bridge;
    
    event Deposit(address indexed sender, uint256 amount, uint256 newTotal);
    event ClaimSubmitted(bytes32 indexed claimId, address claimant, uint256 amount, string reason);
    event ClaimApproved(bytes32 indexed claimId, uint256 amount);
    event ClaimPaid(bytes32 indexed claimId, address claimant, uint256 amount);
    event ClaimRejected(bytes32 indexed claimId, string reason);
    
    modifier onlyBridge() {
        require(msg.sender == bridge, "Not bridge");
        _;
    }
    
    constructor(uint256 _maxClaimAmount) {
        _grantRole(DEFAULT_ADMIN_ROLE, msg.sender);
        _grantRole(OPERATOR_ROLE, msg.sender);
        maxClaimAmount = _maxClaimAmount;
    }
    
    function deposit() external payable {
        require(msg.value > 0, "Amount must be > 0");
        deposits[msg.sender] += msg.value;
        totalFunds += msg.value;
        emit Deposit(msg.sender, msg.value, totalFunds);
    }
    
    function submitClaim(
        uint256 amount,
        string calldata reason,
        string calldata evidence
    ) external returns (bytes32 claimId) {
        require(amount <= maxClaimAmount, "Amount too high");
        require(amount <= totalFunds, "Insufficient funds");
        require(bytes(reason).length > 0, "Reason required");
        
        claimId = keccak256(abi.encodePacked(
            msg.sender,
            block.timestamp,
            amount,
            reason
        ));
        
        claims[claimId] = Claim({
            claimant: msg.sender,
            amount: amount,
            reason: reason,
            evidence: evidence,
            approved: false,
            paid: false,
            createdAt: block.timestamp,
            approvedAt: 0
        });
        
        totalClaims++;
        emit ClaimSubmitted(claimId, msg.sender, amount, reason);
    }
    
    function approveClaim(bytes32 claimId) external onlyRole(OPERATOR_ROLE) {
        Claim storage claim = claims[claimId];
        require(claim.amount > 0, "Claim not found");
        require(!claim.approved, "Already approved");
        require(!claim.paid, "Already paid");
        
        claim.approved = true;
        claim.approvedAt = block.timestamp;
        
        emit ClaimApproved(claimId, claim.amount);
    }
    
    function executeClaim(bytes32 claimId) external nonReentrant {
        Claim storage claim = claims[claimId];
        require(claim.approved, "Not approved");
        require(!claim.paid, "Already paid");
        require(
            block.timestamp >= claim.approvedAt + 1 days,
            "Timelock active"
        );
        
        claim.paid = true;
        totalFunds -= claim.amount;
        
        (bool success, ) = claim.claimant.call{value: claim.amount}("");
        require(success, "Transfer failed");
        
        emit ClaimPaid(claimId, claim.claimant, claim.amount);
    }
    
    function rejectClaim(bytes32 claimId, string calldata reason) 
        external 
        onlyRole(OPERATOR_ROLE) 
    {
        Claim storage claim = claims[claimId];
        require(claim.amount > 0, "Claim not found");
        require(!claim.paid, "Already paid");
        
        claim.approved = false;
        emit ClaimRejected(claimId, reason);
    }
    
    function setBridge(address _bridge) external onlyRole(DEFAULT_ADMIN_ROLE) {
        bridge = _bridge;
    }
    
    function setMaxClaimAmount(uint256 _maxAmount) 
        external 
        onlyRole(DEFAULT_ADMIN_ROLE) 
    {
        maxClaimAmount = _maxAmount;
    }
    
    receive() external payable {
        totalFunds += msg.value;
        emit Deposit(msg.sender, msg.value, totalFunds);
    }
}
