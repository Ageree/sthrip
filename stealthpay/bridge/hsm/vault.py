"""
Hashicorp Vault Integration for MPC Key Shares
"""

import hvac
import base64
import json
import time
from typing import Optional, List

from .base import HSMBackend, KeyShareHSM


class VaultManager(HSMBackend):
    """Hashicorp Vault integration for secure key storage"""
    
    def __init__(self, url: str, token: str = None, namespace: str = None):
        """
        Initialize Vault client
        
        Args:
            url: Vault URL (e.g., https://vault.example.com)
            token: Vault token (or use VAULT_TOKEN env var)
            namespace: Vault namespace (for Vault Enterprise)
        """
        client_kwargs = {"url": url}
        if token:
            client_kwargs["token"] = token
        if namespace:
            client_kwargs["namespace"] = namespace
            
        self.client = hvac.Client(**client_kwargs)
        self.url = url
        self.namespace = namespace
        
        if not self.client.is_authenticated():
            raise RuntimeError("Failed to authenticate with Vault")
    
    def store_key_share(
        self,
        party_id: str,
        key_share: bytes,
        alias: str = None,
        mount_point: str = "secret"
    ) -> KeyShareHSM:
        """
        Store key share in Vault KV v2
        
        Args:
            party_id: Party identifier
            key_share: Key share bytes
            alias: Optional alias
            mount_point: KV secrets engine mount point
            
        Returns:
            KeyShareHSM object
        """
        path = f"mpc/party-{party_id}"
        
        # Encode key share as base64
        encoded_share = base64.b64encode(key_share).decode()
        
        secret_data = {
            "key_share": encoded_share,
            "party_id": party_id,
            "created_at": str(time.time()),
            "version": "1.0"
        }
        
        try:
            self.client.secrets.kv.v2.create_or_update_secret(
                path=path,
                secret=secret_data,
                mount_point=mount_point
            )
            
            return KeyShareHSM(
                key_id=path,
                alias=alias or path,
                party_id=str(party_id),
                backend='vault',
                metadata={
                    'path': path,
                    'mount_point': mount_point,
                    'url': self.url
                }
            )
            
        except hvac.exceptions.VaultError as e:
            raise RuntimeError(f"Failed to store key share: {e}") from e
    
    def retrieve_key_share(
        self,
        key_id: str,
        mount_point: str = "secret"
    ) -> Optional[bytes]:
        """
        Retrieve key share from Vault
        
        Args:
            key_id: Path to the secret
            mount_point: KV secrets engine mount point
            
        Returns:
            Key share bytes or None if not found
        """
        try:
            response = self.client.secrets.kv.v2.read_secret_version(
                path=key_id,
                mount_point=mount_point
            )
            
            encoded_share = response['data']['data']['key_share']
            return base64.b64decode(encoded_share)
            
        except hvac.exceptions.InvalidPath:
            return None
        except hvac.exceptions.VaultError as e:
            raise RuntimeError(f"Failed to retrieve key share: {e}") from e
    
    def delete_key_share(self, key_id: str, mount_point: str = "secret") -> bool:
        """Delete key share from Vault"""
        try:
            self.client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=key_id,
                mount_point=mount_point
            )
            return True
        except hvac.exceptions.VaultError:
            return False
    
    def list_keys(self, mount_point: str = "secret") -> List[KeyShareHSM]:
        """List all MPC keys in Vault"""
        keys = []
        
        try:
            # List keys under mpc/ path
            response = self.client.secrets.kv.v2.list_secrets(
                path="mpc",
                mount_point=mount_point
            )
            
            for key_name in response['data']['keys']:
                if key_name.startswith("party-"):
                    party_id = key_name.replace("party-", "").rstrip("/")
                    keys.append(KeyShareHSM(
                        key_id=f"mpc/{key_name}",
                        alias=f"mpc/{key_name}",
                        party_id=party_id,
                        backend='vault'
                    ))
                    
        except hvac.exceptions.InvalidPath:
            # Path doesn't exist yet
            pass
        except hvac.exceptions.VaultError as e:
            raise RuntimeError(f"Failed to list keys: {e}") from e
        
        return keys
    
    def setup_transit_engine(self, mount_point: str = "mpc-transit"):
        """
        Setup Vault transit engine for encryption operations
        
        Args:
            mount_point: Transit engine mount point
        """
        # Enable transit secrets engine
        try:
            self.client.sys.enable_secrets_engine(
                backend_type='transit',
                path=mount_point
            )
            print(f"✓ Enabled transit engine at {mount_point}")
        except hvac.exceptions.InvalidRequest as e:
            if "already enabled" in str(e):
                print(f"Transit engine already enabled at {mount_point}")
            else:
                raise
        
        # Create encryption key
        key_name = "mpc-master-key"
        try:
            self.client.secrets.transit.create_key(
                name=key_name,
                key_type='aes-256-gcm',
                mount_point=mount_point
            )
            print(f"✓ Created transit key: {key_name}")
        except hvac.exceptions.InvalidRequest as e:
            if "already exists" in str(e):
                print(f"Transit key already exists: {key_name}")
            else:
                raise
    
    def encrypt_with_transit(
        self,
        plaintext: bytes,
        key_name: str = "mpc-master-key",
        mount_point: str = "mpc-transit"
    ) -> str:
        """Encrypt data using Vault transit engine"""
        try:
            response = self.client.secrets.transit.encrypt(
                name=key_name,
                plaintext=base64.b64encode(plaintext).decode(),
                mount_point=mount_point
            )
            return response['data']['ciphertext']
        except hvac.exceptions.VaultError as e:
            raise RuntimeError(f"Encryption failed: {e}") from e
    
    def decrypt_with_transit(
        self,
        ciphertext: str,
        key_name: str = "mpc-master-key",
        mount_point: str = "mpc-transit"
    ) -> bytes:
        """Decrypt data using Vault transit engine"""
        try:
            response = self.client.secrets.transit.decrypt(
                name=key_name,
                ciphertext=ciphertext,
                mount_point=mount_point
            )
            return base64.b64decode(response['data']['plaintext'])
        except hvac.exceptions.VaultError as e:
            raise RuntimeError(f"Decryption failed: {e}") from e
    
    def health_check(self) -> bool:
        """Check Vault health"""
        try:
            health = self.client.sys.read_health()
            return health.get('sealed') is False
        except Exception:
            return False
    
    def seal_status(self) -> dict:
        """Get Vault seal status"""
        return self.client.sys.read_seal_status()
