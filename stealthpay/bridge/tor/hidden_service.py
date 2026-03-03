"""
Tor Hidden Service for MPC Nodes

Allows MPC nodes to communicate anonymously without
revealing their IP addresses.
"""

import asyncio
import base64
import hashlib
import socket
from dataclasses import dataclass
from typing import Optional, Dict
from pathlib import Path


@dataclass
class HiddenServiceConfig:
    """Configuration for Tor hidden service"""
    service_dir: str
    virtual_port: int = 80
    target_port: int = 8080
    target_host: str = "127.0.0.1"
    version: int = 3


class TorHiddenService:
    """
    Tor Hidden Service for anonymous MPC node operation
    
    Example:
        config = HiddenServiceConfig(
            service_dir="/var/lib/tor/mpc-node-1",
            virtual_port=443,
            target_port=8443
        )
        
        service = TorHiddenService(config)
        onion_address = await service.start()
        print(f"Node reachable at: {onion_address}")
    """
    
    def __init__(self, config: HiddenServiceConfig, tor_control_port: int = 9051):
        self.config = config
        self.tor_control_port = tor_control_port
        self.onion_address: Optional[str] = None
        self.private_key: Optional[bytes] = None
        self.client_keys: Dict[str, str] = {}
        self._controller: Optional[socket.socket] = None
    
    async def start(self) -> str:
        """Start hidden service, return .onion address"""
        await self._connect_controller()
        
        service_dir = Path(self.config.service_dir)
        hostname_file = service_dir / "hostname"
        
        if hostname_file.exists():
            self.onion_address = hostname_file.read_text().strip()
        else:
            self.onion_address = await self._create_service()
        
        return self.onion_address
    
    async def _connect_controller(self):
        """Connect to Tor control port"""
        loop = asyncio.get_event_loop()
        self._controller = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        await loop.sock_connect(self._controller, ("127.0.0.1", self.tor_control_port))
        await self._send_command("AUTHENTICATE")
    
    async def _send_command(self, command: str) -> str:
        """Send command to Tor controller"""
        loop = asyncio.get_event_loop()
        cmd_bytes = f"{command}\r\n".encode()
        await loop.sock_sendall(self._controller, cmd_bytes)
        response = await loop.sock_recv(self._controller, 4096)
        return response.decode()
    
    async def _create_service(self) -> str:
        """Create new hidden service via Tor controller"""
        cmd = (
            f"ADD_ONION NEW:ED25519-V3 "
            f"Flags=Detach "
            f"Port={self.config.virtual_port},{self.config.target_host}:{self.config.target_port}"
        )
        
        response = await self._send_command(cmd)
        
        for line in response.split("\n"):
            if line.startswith("250-ServiceID="):
                service_id = line.split("=")[1]
                return f"{service_id}.onion"
        
        raise RuntimeError("Failed to create hidden service")
    
    async def stop(self):
        """Stop hidden service"""
        if self._controller:
            if self.onion_address:
                service_id = self.onion_address.replace(".onion", "")
                await self._send_command(f"DEL_ONION {service_id}")
            await self._send_command("QUIT")
            self._controller.close()
