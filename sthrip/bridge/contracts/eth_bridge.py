"""
Ethereum Bridge Smart Contract Interface

Solidity contract for locking ETH and managing bridge operations.
This is the Python interface to interact with the contract.
"""

import json
import secrets
from typing import Optional, Dict, Any, List
from decimal import Decimal
from dataclasses import dataclass
from enum import Enum


class BridgeStatus(Enum):
    """Status of bridge lock"""
    PENDING = "pending"
    ACTIVE = "active"
    CLAIMED = "claimed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


@dataclass
class BridgeLock:
    """Bridge lock information"""
    lock_id: str
    sender: str
    amount: Decimal
    xmr_address: str
    unlock_time: int
    claimed: bool
    mpc_threshold: int
    mpc_participants: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "lock_id": self.lock_id,
            "sender": self.sender,
            "amount": str(self.amount),
            "xmr_address": self.xmr_address,
            "unlock_time": self.unlock_time,
            "claimed": self.claimed,
            "mpc_threshold": self.mpc_threshold,
            "mpc_participants": self.mpc_participants,
        }


# Solidity contract ABI (simplified)
BRIDGE_ABI = json.dumps([
    {
        "inputs": [
            {"name": "xmrAddress", "type": "string"},
            {"name": "duration", "type": "uint256"}
        ],
        "name": "lock",
        "outputs": [{"name": "lockId", "type": "bytes32"}],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "lockId", "type": "bytes32"},
            {"name": "mpcSignature", "type": "bytes"}
        ],
        "name": "claim",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "lockId", "type": "bytes32"}],
        "name": "refund",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "lockId", "type": "bytes32"}],
        "name": "getLock",
        "outputs": [
            {"name": "sender", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "xmrAddress", "type": "string"},
            {"name": "unlockTime", "type": "uint256"},
            {"name": "claimed", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "lockId", "type": "bytes32"},
            {"indexed": True, "name": "sender", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "xmrAddress", "type": "string"},
            {"name": "unlockTime", "type": "uint256"}
        ],
        "name": "Locked",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "lockId", "type": "bytes32"},
            {"name": "recipient", "type": "address"}
        ],
        "name": "Claimed",
        "type": "event"
    }
])


class EthereumBridgeContract:
    """
    Interface to Ethereum Bridge smart contract.
    
    Handles:
    - Locking ETH for XMR swaps
    - Claiming with MPC signatures
    - Refunds after timeout
    """
    
    def __init__(
        self,
        web3_provider: str,
        contract_address: str,
        private_key: Optional[str] = None
    ):
        self.web3_provider = web3_provider
        self.contract_address = contract_address
        self.private_key = private_key
        
        # In real implementation, initialize web3 here
        self._web3 = None
        self._contract = None
        
    def _init_web3(self):
        """Initialize web3 connection"""
        if self._web3 is None:
            try:
                from web3 import Web3
                self._web3 = Web3(Web3.HTTPProvider(self.web3_provider))
                
                if not self._web3.is_connected():
                    raise BridgeError("Cannot connect to Ethereum node")
                
                self._contract = self._web3.eth.contract(
                    address=self._web3.to_checksum_address(self.contract_address),
                    abi=json.loads(BRIDGE_ABI)
                )
            except ImportError:
                raise BridgeError("web3.py required. Install: pip install web3")
        
        return self._web3, self._contract
    
    def lock(
        self,
        xmr_address: str,
        eth_amount: Decimal,
        duration_seconds: int = 86400,  # 24 hours
        sender_address: Optional[str] = None
    ) -> str:
        """
        Lock ETH for XMR swap.
        
        Args:
            xmr_address: Monero address to receive XMR
            eth_amount: Amount of ETH to lock
            duration_seconds: Lock duration in seconds
            sender_address: Ethereum sender address (if not using private key)
            
        Returns:
            lock_id: Unique lock identifier
        """
        web3, contract = self._init_web3()
        
        # Generate lock ID
        lock_id = '0x' + secrets.token_hex(32)
        
        # Convert ETH to wei
        amount_wei = web3.to_wei(eth_amount, 'ether')
        
        # Build transaction
        tx = contract.functions.lock(
            xmr_address,
            duration_seconds
        ).build_transaction({
            'from': sender_address or self._get_account_address(),
            'value': amount_wei,
            'gas': 200000,
            'gasPrice': web3.to_wei('20', 'gwei'),
            'nonce': web3.eth.get_transaction_count(
                sender_address or self._get_account_address()
            ),
        })
        
        # Sign and send
        if self.private_key:
            signed_tx = web3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        else:
            # Requires manual signing
            tx_hash = web3.eth.send_transaction(tx)
        
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
        
        if receipt['status'] != 1:
            raise BridgeError(f"Transaction failed: {tx_hash.hex()}")
        
        return lock_id
    
    def claim(
        self,
        lock_id: str,
        mpc_signature: bytes,
        recipient_address: str
    ) -> str:
        """
        Claim locked ETH with MPC signature.
        
        Args:
            lock_id: Lock identifier
            mpc_signature: Threshold signature from MPC network
            recipient_address: Address to receive ETH
            
        Returns:
            tx_hash: Claim transaction hash
        """
        web3, contract = self._init_web3()
        
        tx = contract.functions.claim(
            lock_id,
            mpc_signature
        ).build_transaction({
            'from': recipient_address,
            'gas': 150000,
            'gasPrice': web3.to_wei('20', 'gwei'),
            'nonce': web3.eth.get_transaction_count(recipient_address),
        })
        
        if self.private_key:
            signed_tx = web3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        else:
            tx_hash = web3.eth.send_transaction(tx)
        
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
        
        if receipt['status'] != 1:
            raise BridgeError(f"Claim failed: {tx_hash.hex()}")
        
        return tx_hash.hex()
    
    def refund(self, lock_id: str, sender_address: str) -> str:
        """
        Refund locked ETH after timeout.
        
        Args:
            lock_id: Lock identifier
            sender_address: Original sender address
            
        Returns:
            tx_hash: Refund transaction hash
        """
        web3, contract = self._init_web3()
        
        tx = contract.functions.refund(lock_id).build_transaction({
            'from': sender_address,
            'gas': 100000,
            'gasPrice': web3.to_wei('20', 'gwei'),
            'nonce': web3.eth.get_transaction_count(sender_address),
        })
        
        if self.private_key:
            signed_tx = web3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = web3.eth.send_raw_transaction(signed_tx.rawTransaction)
        else:
            tx_hash = web3.eth.send_transaction(tx)
        
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash)
        
        if receipt['status'] != 1:
            raise BridgeError(f"Refund failed: {tx_hash.hex()}")
        
        return tx_hash.hex()
    
    def get_lock(self, lock_id: str) -> Optional[BridgeLock]:
        """Get lock information"""
        web3, contract = self._init_web3()
        
        try:
            result = contract.functions.getLock(lock_id).call()
            
            return BridgeLock(
                lock_id=lock_id,
                sender=result[0],
                amount=Decimal(web3.from_wei(result[1], 'ether')),
                xmr_address=result[2],
                unlock_time=result[3],
                claimed=result[4],
                mpc_threshold=3,  # Default 3-of-5
                mpc_participants=[]
            )
        except Exception as e:
            return None
    
    def watch_events(
        self,
        from_block: int = 0,
        event_filter: Optional[Dict] = None
    ) -> List[Dict]:
        """
        Watch for bridge events.
        
        Returns list of events:
        - Locked: New lock created
        - Claimed: Lock claimed
        """
        web3, contract = self._init_web3()
        
        events = []
        
        # Get Locked events
        locked_events = contract.events.Locked().get_logs(
            fromBlock=from_block
        )
        
        for event in locked_events:
            events.append({
                "event": "Locked",
                "lock_id": event['args']['lockId'].hex(),
                "sender": event['args']['sender'],
                "amount": Decimal(web3.from_wei(event['args']['amount'], 'ether')),
                "xmr_address": event['args']['xmrAddress'],
                "unlock_time": event['args']['unlockTime'],
                "block_number": event['blockNumber'],
                "tx_hash": event['transactionHash'].hex()
            })
        
        # Get Claimed events
        claimed_events = contract.events.Claimed().get_logs(
            fromBlock=from_block
        )
        
        for event in claimed_events:
            events.append({
                "event": "Claimed",
                "lock_id": event['args']['lockId'].hex(),
                "recipient": event['args']['recipient'],
                "block_number": event['blockNumber'],
                "tx_hash": event['transactionHash'].hex()
            })
        
        return events
    
    def _get_account_address(self) -> str:
        """Get address from private key"""
        if not self.private_key:
            raise BridgeError("Private key required")
        
        web3, _ = self._init_web3()
        account = web3.eth.account.from_key(self.private_key)
        return account.address


class BridgeError(Exception):
    """Bridge operation error"""
    pass


# Solidity contract source (for reference)
BRIDGE_CONTRACT_SOL = """
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract SthripBridge {
    struct Lock {
        address sender;
        uint256 amount;
        string xmrAddress;
        uint256 unlockTime;
        bool claimed;
        uint256 mpcThreshold;
        bytes32 mpcMerkleRoot;
    }
    
    mapping(bytes32 => Lock) public locks;
    mapping(address => bool) public mpcNodes;
    
    event Locked(
        bytes32 indexed lockId,
        address indexed sender,
        uint256 amount,
        string xmrAddress,
        uint256 unlockTime
    );
    
    event Claimed(bytes32 indexed lockId, address recipient);
    event Refunded(bytes32 indexed lockId, address sender);
    
    uint256 public constant MPC_THRESHOLD = 3;
    uint256 public constant MPC_TOTAL = 5;
    
    function lock(string calldata xmrAddress, uint256 duration) 
        external 
        payable 
        returns (bytes32 lockId 
    {
        require(msg.value > 0, "Amount must be > 0");
        require(bytes(xmrAddress).length > 0, "XMR address required");
        
        lockId = keccak256(abi.encodePacked(
            msg.sender,
            block.timestamp,
            msg.value,
            xmrAddress
        ));
        
        require(locks[lockId].amount == 0, "Lock exists");
        
        locks[lockId] = Lock({
            sender: msg.sender,
            amount: msg.value,
            xmrAddress: xmrAddress,
            unlockTime: block.timestamp + duration,
            claimed: false,
            mpcThreshold: MPC_THRESHOLD,
            mpcMerkleRoot: bytes32(0)
        });
        
        emit Locked(lockId, msg.sender, msg.value, xmrAddress, block.timestamp + duration);
    }
    
    function claim(bytes32 lockId, bytes calldata mpcSignature) external {
        Lock storage lock = locks[lockId];
        
        require(lock.amount > 0, "Lock not found");
        require(!lock.claimed, "Already claimed");
        require(block.timestamp < lock.unlockTime, "Lock expired");
        require(
            verifyMPCSignature(lockId, mpcSignature),
            "Invalid MPC signature"
        );
        
        lock.claimed = true;
        
        (bool success, ) = msg.sender.call{value: lock.amount}("");
        require(success, "Transfer failed");
        
        emit Claimed(lockId, msg.sender);
    }
    
    function refund(bytes32 lockId) external {
        Lock storage lock = locks[lockId];
        
        require(lock.amount > 0, "Lock not found");
        require(!lock.claimed, "Already claimed");
        require(block.timestamp >= lock.unlockTime, "Lock active");
        require(msg.sender == lock.sender, "Not sender");
        
        lock.claimed = true;
        
        (bool success, ) = msg.sender.call{value: lock.amount}("");
        require(success, "Transfer failed");
        
        emit Refunded(lockId, msg.sender);
    }
    
    function verifyMPCSignature(bytes32 lockId, bytes calldata signature) 
        internal 
        pure 
        returns (bool) 
    {
        // Simplified - real implementation uses BLS or threshold signatures
        return signature.length == 65; // ECDSA signature size
    }
    
    receive() external payable {
        revert("Use lock() function");
    }
}
"""
