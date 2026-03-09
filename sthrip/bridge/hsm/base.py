"""
Base HSM Interface
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class KeyShareHSM:
    """Key share stored in HSM"""
    key_id: str
    alias: str
    party_id: str
    backend: str  # 'aws_kms', 'vault', etc.
    metadata: dict = None


class HSMBackend(ABC):
    """Abstract base class for HSM backends"""
    
    @abstractmethod
    def store_key_share(self, party_id: str, key_share: bytes, alias: str = None) -> KeyShareHSM:
        """Store key share in HSM"""
        pass
    
    @abstractmethod
    def retrieve_key_share(self, key_id: str) -> Optional[bytes]:
        """Retrieve key share from HSM"""
        pass
    
    @abstractmethod
    def delete_key_share(self, key_id: str) -> bool:
        """Delete key share from HSM"""
        pass
    
    @abstractmethod
    def list_keys(self) -> List[KeyShareHSM]:
        """List all stored key shares"""
        pass
    
    @abstractmethod
    def health_check(self) -> bool:
        """Check HSM connectivity"""
        pass
