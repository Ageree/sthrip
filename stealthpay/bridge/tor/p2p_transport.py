"""
Tor P2P Transport Layer

Routes P2P communication through Tor network.
"""

import asyncio
from typing import Optional, Dict
from pathlib import Path

import aiohttp


class TorP2PTransport:
    """
    P2P transport layer over Tor
    
    Wraps regular P2P communication to route through Tor.
    """
    
    def __init__(self, socks_host: str = "127.0.0.1", socks_port: int = 9050):
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get aiohttp session with Tor proxy"""
        if not self.session:
            # Note: requires aiohttp_socks for actual SOCKS proxy support
            connector = aiohttp.TCPConnector()
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session
    
    async def connect_onion(self, onion_address: str, port: int = 80) -> str:
        """
        Connect to .onion address
        
        Returns:
            Connection status
        """
        session = await self._get_session()
        url = f"http://{onion_address}:{port}"
        
        try:
            async with session.get(url, proxy=f"socks5://{self.socks_host}:{self.socks_port}") as resp:
                return f"Connected: {resp.status}"
        except Exception as e:
            return f"Failed: {e}"
    
    async def close(self):
        """Close transport"""
        if self.session:
            await self.session.close()
            self.session = None


class OnionAddressBook:
    """Address book for MPC node .onion addresses"""
    
    def __init__(self, storage_path: str = ".onion_addresses.json"):
        self.storage_path = Path(storage_path)
        self.addresses: Dict[str, Dict] = {}
        self._load()
    
    def _load(self):
        """Load addresses from storage"""
        import json
        if self.storage_path.exists():
            self.addresses = json.loads(self.storage_path.read_text())
    
    def _save(self):
        """Save addresses to storage"""
        import json
        self.storage_path.write_text(json.dumps(self.addresses, indent=2))
    
    def register(self, node_id: str, onion_address: str, public_key: str = None):
        """Register node's onion address"""
        self.addresses[node_id] = {
            "onion": onion_address,
            "public_key": public_key,
            "added_at": asyncio.get_event_loop().time()
        }
        self._save()
    
    def get(self, node_id: str) -> Optional[str]:
        """Get onion address for node"""
        info = self.addresses.get(node_id)
        return info["onion"] if info else None
    
    def list_nodes(self) -> list:
        """List all known nodes"""
        return list(self.addresses.keys())
