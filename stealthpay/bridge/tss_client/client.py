"""
TSS gRPC Client Implementation
"""

import grpc
from typing import List, Optional, Tuple
from dataclasses import dataclass

from .proto import tss_pb2, tss_pb2_grpc
from .exceptions import TSSConnectionError, TSSOperationError


@dataclass
class KeyShare:
    """Represents a key share"""
    data: bytes
    share_id: str
    public_key: bytes


@dataclass
class Signature:
    """Represents an ECDSA signature"""
    r: bytes
    s: bytes
    recovery_id: bytes
    raw: bytes


class TSSClient:
    """
    gRPC client for TSS service
    
    This client communicates with the Go-based TSS server for
    distributed key generation and threshold signing.
    
    Example:
        client = TSSClient("localhost:50051")
        
        # Generate key
        key_share = client.generate_key(
            party_id="node-1",
            threshold=3,
            total=5,
            peers=["node-2", "node-3", "node-4", "node-5"]
        )
        
        # Create signature
        sig = client.sign(
            message_hash=msg_hash,
            party_id="node-1",
            key_share=key_share.data,
            peers=["node-2", "node-3"],
            participants=["node-1", "node-2", "node-3"]
        )
    """
    
    def __init__(self, endpoint: str = "localhost:50051", timeout: int = 300):
        """
        Initialize TSS client
        
        Args:
            endpoint: TSS server endpoint (host:port)
            timeout: Default timeout in seconds
        """
        self.endpoint = endpoint
        self.timeout = timeout
        self._channel: Optional[grpc.Channel] = None
        self._stub: Optional[tss_pb2_grpc.TSSServiceStub] = None
        self._connect()
    
    def _connect(self) -> None:
        """Establish gRPC connection"""
        try:
            # Create insecure channel (use secure channel in production)
            self._channel = grpc.insecure_channel(
                self.endpoint,
                options=[
                    ('grpc.max_send_message_length', 50 * 1024 * 1024),
                    ('grpc.max_receive_message_length', 50 * 1024 * 1024),
                ]
            )
            grpc.channel_ready_future(self._channel).result(timeout=5)
            self._stub = tss_pb2_grpc.TSSServiceStub(self._channel)
        except grpc.FutureTimeoutError as e:
            raise TSSConnectionError(f"Failed to connect to TSS server at {self.endpoint}") from e
        except Exception as e:
            raise TSSConnectionError(f"Connection error: {e}") from e
    
    def close(self) -> None:
        """Close gRPC connection"""
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    def generate_key(
        self,
        party_id: str,
        threshold: int,
        total: int,
        peers: List[str],
        seed: Optional[bytes] = None
    ) -> KeyShare:
        """
        Generate key share via DKG
        
        Args:
            party_id: Unique ID for this party
            threshold: Minimum parties needed for signing
            total: Total number of parties
            peers: List of peer party IDs
            seed: Optional seed for deterministic generation
            
        Returns:
            KeyShare object containing the key share data
            
        Raises:
            TSSOperationError: If key generation fails
        """
        if self._stub is None:
            raise TSSConnectionError("Not connected to TSS server")
        
        request = tss_pb2.KeyGenRequest(
            party_id=party_id,
            threshold=threshold,
            total_parties=total,
            peer_ids=peers,
            seed=seed or b""
        )
        
        try:
            response = self._stub.GenerateKey(
                request,
                timeout=self.timeout
            )
            
            if not response.success:
                raise TSSOperationError(f"Key generation failed: {response.error}")
            
            return KeyShare(
                data=response.key_share,
                share_id=response.share_id,
                public_key=response.public_key
            )
            
        except grpc.RpcError as e:
            raise TSSOperationError(f"RPC error during key generation: {e}") from e
    
    def sign(
        self,
        message_hash: bytes,
        party_id: str,
        key_share: bytes,
        peers: List[str],
        participants: List[str]
    ) -> Signature:
        """
        Create threshold signature
        
        Args:
            message_hash: 32-byte message hash
            party_id: This party's ID
            key_share: Key share data from generate_key
            peers: List of peer IDs
            participants: List of parties participating in signing
            
        Returns:
            Signature object
            
        Raises:
            TSSOperationError: If signing fails
        """
        if self._stub is None:
            raise TSSConnectionError("Not connected to TSS server")
        
        if len(message_hash) != 32:
            raise ValueError("Message hash must be 32 bytes")
        
        request = tss_pb2.SignRequest(
            message_hash=message_hash,
            party_id=party_id,
            key_share=key_share,
            peer_ids=peers,
            participants=participants
        )
        
        try:
            response = self._stub.Sign(
                request,
                timeout=self.timeout
            )
            
            if not response.success:
                raise TSSOperationError(f"Signing failed: {response.error}")
            
            return Signature(
                r=response.signature[:32],
                s=response.signature[32:],
                recovery_id=response.recovery_id,
                raw=response.signature
            )
            
        except grpc.RpcError as e:
            raise TSSOperationError(f"RPC error during signing: {e}") from e
    
    def reshare(
        self,
        party_id: str,
        old_key_share: bytes,
        new_threshold: int,
        new_total: int,
        peers: List[str]
    ) -> bytes:
        """
        Perform proactive secret sharing
        
        Args:
            party_id: This party's ID
            old_key_share: Current key share
            new_threshold: New threshold value
            new_total: New total number of parties
            peers: List of peer IDs
            
        Returns:
            New key share bytes
        """
        if self._stub is None:
            raise TSSConnectionError("Not connected to TSS server")
        
        request = tss_pb2.ReshareRequest(
            party_id=party_id,
            old_key_share=old_key_share,
            new_threshold=new_threshold,
            new_total=new_total,
            peer_ids=peers
        )
        
        try:
            response = self._stub.Reshare(
                request,
                timeout=self.timeout
            )
            
            if not response.success:
                raise TSSOperationError(f"Resharing failed: {response.error}")
            
            return response.new_key_share
            
        except grpc.RpcError as e:
            raise TSSOperationError(f"RPC error during resharing: {e}") from e
    
    def get_public_key(
        self,
        party_id: str,
        key_share: bytes
    ) -> Tuple[bytes, str]:
        """
        Get public key and Ethereum address from key share
        
        Args:
            party_id: This party's ID
            key_share: Key share data
            
        Returns:
            Tuple of (public_key, ethereum_address)
        """
        if self._stub is None:
            raise TSSConnectionError("Not connected to TSS server")
        
        request = tss_pb2.PublicKeyRequest(
            party_id=party_id,
            key_share=key_share
        )
        
        try:
            response = self._stub.GetPublicKey(
                request,
                timeout=30
            )
            
            if not response.success:
                raise TSSOperationError(f"Failed to get public key: {response.error}")
            
            address = "0x" + response.address.hex()
            return response.public_key, address
            
        except grpc.RpcError as e:
            raise TSSOperationError(f"RPC error: {e}") from e
    
    def health_check(self) -> bool:
        """Check if TSS server is healthy"""
        try:
            # Try to get public key with empty data - will fail but shows server is responsive
            self.get_public_key("test", b"")
            return True
        except TSSOperationError:
            return True  # Server responded
        except Exception:
            return False
