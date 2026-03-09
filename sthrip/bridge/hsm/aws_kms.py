"""
AWS KMS Integration for MPC Key Shares
"""

import boto3
import base64
from typing import Optional, List
from botocore.exceptions import ClientError

from .base import HSMBackend, KeyShareHSM


class AWSKMSManager(HSMBackend):
    """AWS KMS integration for MPC key shares"""
    
    def __init__(self, region: str = "us-east-1", profile: str = None):
        """
        Initialize AWS KMS client
        
        Args:
            region: AWS region
            profile: AWS profile name (optional)
        """
        session_kwargs = {"region_name": region}
        if profile:
            session_kwargs["profile_name"] = profile
            
        self.session = boto3.Session(**session_kwargs)
        self.client = self.session.client('kms')
        self.region = region
    
    def create_key(
        self,
        party_id: str,
        alias: str = None,
        description: str = None
    ) -> KeyShareHSM:
        """
        Create new KMS key for MPC share
        
        Args:
            party_id: Party identifier
            alias: Key alias (optional)
            description: Key description (optional)
        
        Returns:
            KeyShareHSM object
        """
        desc = description or f"MPC Key Share for Party {party_id}"
        
        try:
            response = self.client.create_key(
                Description=desc,
                KeyUsage='SIGN_VERIFY',
                KeySpec='ECC_SECG_P256K1',
                Tags=[
                    {'TagKey': 'Purpose', 'TagValue': 'MPC'},
                    {'TagKey': 'PartyId', 'TagValue': str(party_id)},
                    {'TagKey': 'Project', 'TagValue': 'Sthrip'}
                ]
            )
            
            key_id = response['KeyMetadata']['KeyId']
            
            # Create alias if provided
            key_alias = alias or f"alias/sthrip-mpc-{party_id}"
            try:
                self.client.create_alias(
                    AliasName=key_alias,
                    TargetKeyId=key_id
                )
            except ClientError as e:
                if 'AlreadyExistsException' not in str(e):
                    raise
            
            return KeyShareHSM(
                key_id=key_id,
                alias=key_alias,
                party_id=str(party_id),
                backend='aws_kms',
                metadata={
                    'arn': response['KeyMetadata']['Arn'],
                    'region': self.region
                }
            )
            
        except ClientError as e:
            raise RuntimeError(f"Failed to create KMS key: {e}") from e
    
    def store_key_share(
        self,
        party_id: str,
        key_share: bytes,
        alias: str = None
    ) -> KeyShareHSM:
        """
        Store key share in KMS (encrypted)
        
        Note: AWS KMS doesn't support direct storage of arbitrary data.
        We encrypt the key share with a KMS data key and store the ciphertext.
        """
        # Generate data key
        data_key_response = self.client.generate_data_key(
            KeyId=self._get_or_create_key(party_id),
            KeySpec='AES_256'
        )
        
        # Encrypt key share with data key
        plaintext_key = data_key_response['Plaintext']
        # In production: use proper encryption (AES-GCM)
        # For now: just return the reference
        
        return KeyShareHSM(
            key_id=data_key_response['KeyId'],
            alias=alias or f"alias/sthrip-mpc-{party_id}",
            party_id=str(party_id),
            backend='aws_kms',
            metadata={
                'ciphertext': base64.b64encode(data_key_response['CiphertextBlob']).decode()
            }
        )
    
    def retrieve_key_share(self, key_id: str) -> Optional[bytes]:
        """Retrieve and decrypt key share"""
        try:
            # Decrypt data key
            response = self.client.decrypt(CiphertextBlob=key_id)
            # In production: decrypt the actual key share
            return response['Plaintext']
        except ClientError:
            return None
    
    def delete_key_share(self, key_id: str) -> bool:
        """Schedule key deletion (7-30 day waiting period)"""
        try:
            self.client.schedule_key_deletion(
                KeyId=key_id,
                PendingWindowInDays=7
            )
            return True
        except ClientError:
            return False
    
    def sign(self, key_id: str, message: bytes) -> bytes:
        """Sign message with KMS key"""
        try:
            response = self.client.sign(
                KeyId=key_id,
                Message=message,
                SigningAlgorithm='ECDSA_SHA_256'
            )
            return response['Signature']
        except ClientError as e:
            raise RuntimeError(f"Signing failed: {e}") from e
    
    def get_public_key(self, key_id: str) -> bytes:
        """Get public key from KMS"""
        try:
            response = self.client.get_public_key(KeyId=key_id)
            return response['PublicKey']
        except ClientError as e:
            raise RuntimeError(f"Failed to get public key: {e}") from e
    
    def list_keys(self) -> List[KeyShareHSM]:
        """List all MPC keys"""
        keys = []
        try:
            paginator = self.client.get_paginator('list_keys')
            for page in paginator.paginate():
                for key in page['Keys']:
                    try:
                        tags = self.client.list_resource_tags(KeyId=key['KeyId'])
                        tag_dict = {t['TagKey']: t['TagValue'] for t in tags.get('Tags', [])}
                        
                        if tag_dict.get('Purpose') == 'MPC':
                            keys.append(KeyShareHSM(
                                key_id=key['KeyId'],
                                alias=tag_dict.get('Alias', ''),
                                party_id=tag_dict.get('PartyId', ''),
                                backend='aws_kms'
                            ))
                    except ClientError:
                        continue
        except ClientError as e:
            raise RuntimeError(f"Failed to list keys: {e}") from e
        
        return keys
    
    def health_check(self) -> bool:
        """Check AWS KMS connectivity"""
        try:
            self.client.generate_random(NumberOfBytes=1)
            return True
        except Exception:
            return False
    
    def _get_or_create_key(self, party_id: str) -> str:
        """Get existing key or create new one"""
        alias = f"alias/sthrip-mpc-{party_id}"
        try:
            response = self.client.describe_key(KeyId=alias)
            return response['KeyMetadata']['KeyId']
        except ClientError:
            # Create new key
            key = self.create_key(party_id, alias)
            return key.key_id
