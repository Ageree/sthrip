"""
P2P Node with Mutual TLS Authentication

Provides secure WebSocket communication between MPC nodes
with certificate pinning and mutual authentication.
"""

import asyncio
import ssl
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Callable, Set
from dataclasses import dataclass

import websockets
from websockets.server import WebSocketServerProtocol
from websockets.client import WebSocketClientProtocol


logger = logging.getLogger(__name__)


@dataclass
class PeerInfo:
    """Information about a peer node"""
    node_id: str
    address: str
    port: int
    certificate_cn: str
    is_connected: bool = False
    last_seen: float = 0


class MTLSNode:
    """
    WebSocket node with mutual TLS authentication
    
    Features:
    - Mutual TLS (client + server certificates)
    - Certificate pinning
    - TLS 1.3 only
    - Async message handling
    - Automatic reconnection
    
    Example:
        node = MTLSNode(
            node_id="mpc-node-1",
            cert_path="certs/node1.crt",
            key_path="certs/node1.key",
            ca_path="certs/ca.crt"
        )
        
        # Start server
        await node.start_server("0.0.0.0", 8443)
        
        # Connect to peer
        peer = await node.connect("wss://peer.example.com:8443", "mpc-node-2")
    """
    
    def __init__(
        self,
        node_id: str,
        cert_path: str,
        key_path: str,
        ca_path: str,
        message_handler: Optional[Callable] = None
    ):
        """
        Initialize mTLS node
        
        Args:
            node_id: Unique node identifier
            cert_path: Path to node certificate
            key_path: Path to node private key
            ca_path: Path to CA certificate
            message_handler: Callback for incoming messages
        """
        self.node_id = node_id
        self.cert_path = Path(cert_path)
        self.key_path = Path(key_path)
        self.ca_path = Path(ca_path)
        self.message_handler = message_handler
        
        # SSL context for server
        self.server_ssl: Optional[ssl.SSLContext] = None
        
        # Connected peers
        self.peers: Dict[str, WebSocketClientProtocol] = {}
        self.peer_info: Dict[str, PeerInfo] = {}
        
        # Server reference
        self.server = None
        
        # Running state
        self._running = False
        self._reconnect_tasks: Dict[str, asyncio.Task] = {}
        
        # Validate certificates exist
        if not all(p.exists() for p in [self.cert_path, self.key_path, self.ca_path]):
            raise FileNotFoundError("Certificate files not found")
    
    def _create_server_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context for server with mutual authentication"""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        
        # Load server certificate and private key
        context.load_cert_chain(
            certfile=str(self.cert_path),
            keyfile=str(self.key_path)
        )
        
        # Load CA for client verification
        context.load_verify_locations(str(self.ca_path))
        
        # Require client certificate
        context.verify_mode = ssl.CERT_REQUIRED
        
        # TLS 1.3 only
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        
        # Strong cipher suites
        context.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS')
        
        return context
    
    def _create_client_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context for client connections"""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        
        # Load client certificate
        context.load_cert_chain(
            certfile=str(self.cert_path),
            keyfile=str(self.key_path)
        )
        
        # Load CA
        context.load_verify_locations(str(self.ca_path))
        
        # Verify server certificate
        context.verify_mode = ssl.CERT_REQUIRED
        
        # TLS 1.3 only
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        
        return context
    
    async def start_server(self, host: str, port: int) -> None:
        """
        Start secure WebSocket server
        
        Args:
            host: Bind address
            port: Listen port
        """
        self.server_ssl = self._create_server_ssl_context()
        
        self.server = await websockets.serve(
            self._handle_connection,
            host,
            port,
            ssl=self.server_ssl,
            ping_interval=30,
            ping_timeout=10
        )
        
        self._running = True
        logger.info(f"mTLS server started on {host}:{port}")
    
    async def _handle_connection(
        self,
        websocket: WebSocketServerProtocol,
        path: str
    ) -> None:
        """Handle incoming WebSocket connection"""
        try:
            # Get client certificate info
            ssl_obj = websocket.transport.get_extra_info('ssl_object')
            if not ssl_obj:
                logger.warning("No SSL object for connection")
                await websocket.close(1011, "TLS required")
                return
            
            cert = ssl_obj.getpeercert()
            if not cert:
                logger.warning("No client certificate presented")
                await websocket.close(1011, "Client certificate required")
                return
            
            # Extract CN from certificate
            subject = cert.get('subject', ())
            cn = None
            for item in subject:
                for key, value in item:
                    if key == 'commonName':
                        cn = value
                        break
            
            if not cn:
                logger.warning("No CN in client certificate")
                await websocket.close(1011, "Invalid certificate")
                return
            
            logger.info(f"Client connected: {cn}")
            
            # Store peer info
            self.peer_info[cn] = PeerInfo(
                node_id=cn,
                address=websocket.remote_address[0],
                port=websocket.remote_address[1],
                certificate_cn=cn,
                is_connected=True,
                last_seen=asyncio.get_event_loop().time()
            )
            
            # Handle messages
            async for message in websocket:
                await self._process_message(cn, message, websocket)
                
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed: {cn if 'cn' in locals() else 'unknown'}")
        except Exception as e:
            logger.error(f"Connection error: {e}")
        finally:
            # Cleanup
            if 'cn' in locals() and cn in self.peer_info:
                self.peer_info[cn].is_connected = False
    
    async def _process_message(
        self,
        sender: str,
        message: str,
        websocket: WebSocketServerProtocol
    ) -> None:
        """Process incoming message"""
        try:
            data = json.loads(message)
            
            # Update last seen
            if sender in self.peer_info:
                self.peer_info[sender].last_seen = asyncio.get_event_loop().time()
            
            # Call handler if provided
            if self.message_handler:
                response = await self.message_handler(sender, data)
                if response:
                    await websocket.send(json.dumps(response))
            else:
                # Default echo
                await websocket.send(json.dumps({
                    "type": "ack",
                    "from": self.node_id,
                    "original": data
                }))
                
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from {sender}")
            await websocket.send(json.dumps({"error": "Invalid JSON"}))
        except Exception as e:
            logger.error(f"Message processing error: {e}")
    
    async def connect(
        self,
        uri: str,
        expected_cn: str,
        retry: bool = True
    ) -> WebSocketClientProtocol:
        """
        Connect to peer with certificate pinning
        
        Args:
            uri: WebSocket URI (wss://host:port)
            expected_cn: Expected Common Name in server certificate
            retry: Auto-retry on disconnect
            
        Returns:
            WebSocket client protocol
        """
        ssl_context = self._create_client_ssl_context()
        
        # Connect with certificate verification
        websocket = await websockets.connect(
            uri,
            ssl=ssl_context,
            ping_interval=30,
            ping_timeout=10
        )
        
        # Verify server certificate
        ssl_obj = websocket.transport.get_extra_info('ssl_object')
        cert = ssl_obj.getpeercert()
        subject = cert.get('subject', ())
        server_cn = None
        
        for item in subject:
            for key, value in item:
                if key == 'commonName':
                    server_cn = value
                    break
        
        if server_cn != expected_cn:
            await websocket.close()
            raise SecurityError(
                f"Certificate mismatch: expected {expected_cn}, got {server_cn}"
            )
        
        # Store connection
        self.peers[expected_cn] = websocket
        self.peer_info[expected_cn] = PeerInfo(
            node_id=expected_cn,
            address=uri,
            port=0,  # Extract from URI
            certificate_cn=expected_cn,
            is_connected=True
        )
        
        logger.info(f"Connected to {expected_cn} at {uri}")
        
        # Start listener for incoming messages
        asyncio.create_task(self._listen_to_peer(expected_cn, websocket))
        
        # Setup auto-reconnect if enabled
        if retry:
            self._reconnect_tasks[expected_cn] = asyncio.create_task(
                self._reconnect_loop(uri, expected_cn)
            )
        
        return websocket
    
    async def _listen_to_peer(
        self,
        peer_id: str,
        websocket: WebSocketClientProtocol
    ) -> None:
        """Listen for messages from connected peer"""
        try:
            async for message in websocket:
                await self._process_message(peer_id, message, websocket)
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection to {peer_id} closed")
        except Exception as e:
            logger.error(f"Error listening to {peer_id}: {e}")
        finally:
            self.peer_info[peer_id].is_connected = False
    
    async def _reconnect_loop(self, uri: str, expected_cn: str) -> None:
        """Auto-reconnect loop for peer connections"""
        while self._running:
            await asyncio.sleep(5)
            
            if expected_cn not in self.peers:
                try:
                    logger.info(f"Attempting to reconnect to {expected_cn}")
                    await self.connect(uri, expected_cn, retry=False)
                except Exception as e:
                    logger.debug(f"Reconnect to {expected_cn} failed: {e}")
    
    async def send(
        self,
        peer_id: str,
        message: dict,
        timeout: float = 30.0
    ) -> Optional[dict]:
        """
        Send message to peer
        
        Args:
            peer_id: Target peer ID
            message: Message dict
            timeout: Response timeout
            
        Returns:
            Response dict or None
        """
        if peer_id not in self.peers:
            raise ConnectionError(f"Not connected to {peer_id}")
        
        websocket = self.peers[peer_id]
        
        # Add metadata
        message["from"] = self.node_id
        message["timestamp"] = asyncio.get_event_loop().time()
        
        # Send
        await websocket.send(json.dumps(message))
        
        # Wait for response
        # In real implementation, use request/response correlation
        return None
    
    async def broadcast(self, message: dict) -> Dict[str, bool]:
        """Broadcast message to all connected peers"""
        results = {}
        
        for peer_id, websocket in list(self.peers.items()):
            try:
                message["from"] = self.node_id
                await websocket.send(json.dumps(message))
                results[peer_id] = True
            except Exception as e:
                logger.error(f"Failed to send to {peer_id}: {e}")
                results[peer_id] = False
        
        return results
    
    def get_connected_peers(self) -> Set[str]:
        """Get set of connected peer IDs"""
        return {
            peer_id for peer_id, info in self.peer_info.items()
            if info.is_connected
        }
    
    async def disconnect(self, peer_id: str) -> None:
        """Disconnect from specific peer"""
        if peer_id in self._reconnect_tasks:
            self._reconnect_tasks[peer_id].cancel()
            del self._reconnect_tasks[peer_id]
        
        if peer_id in self.peers:
            await self.peers[peer_id].close()
            del self.peers[peer_id]
        
        if peer_id in self.peer_info:
            self.peer_info[peer_id].is_connected = False
    
    async def stop(self) -> None:
        """Stop server and disconnect all peers"""
        self._running = False
        
        # Cancel reconnect tasks
        for task in self._reconnect_tasks.values():
            task.cancel()
        self._reconnect_tasks.clear()
        
        # Close all peer connections
        for peer_id in list(self.peers.keys()):
            await self.disconnect(peer_id)
        
        # Stop server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        
        logger.info("mTLS node stopped")


class SecurityError(Exception):
    """Security-related error"""
    pass


# Certificate generation script
generate_certs_script = """#!/bin/bash
# Generate certificates for MPC nodes

set -e

OUTPUT_DIR="${1:-./certs}"
NUM_NODES="${2:-5}"

echo "Generating certificates in ${OUTPUT_DIR} for ${NUM_NODES} nodes..."

mkdir -p "${OUTPUT_DIR}"

# Generate CA
openssl req -x509 -newkey rsa:4096 -keyout "${OUTPUT_DIR}/ca.key" -out "${OUTPUT_DIR}/ca.crt" \\
    -days 3650 -nodes -subj "/C=US/O=Sthrip/CN=Sthrip Root CA"

echo "✓ CA generated"

# Generate certificates for each node
for i in $(seq 1 ${NUM_NODES}); do
    NODE_NAME="mpc-node-${i}"
    
    # Private key
    openssl genrsa -out "${OUTPUT_DIR}/node${i}.key" 2048
    
    # Certificate request
    openssl req -new -key "${OUTPUT_DIR}/node${i}.key" -out "${OUTPUT_DIR}/node${i}.csr" \\
        -subj "/C=US/O=Sthrip/CN=${NODE_NAME}"
    
    # Sign with CA
    cat > "${OUTPUT_DIR}/node${i}.ext" << EOF
subjectAltName = @alt_names
[alt_names]
DNS.1 = ${NODE_NAME}
DNS.2 = localhost
IP.1 = 127.0.0.1
EOF
    
    openssl x509 -req -in "${OUTPUT_DIR}/node${i}.csr" \\
        -CA "${OUTPUT_DIR}/ca.crt" -CAkey "${OUTPUT_DIR}/ca.key" \\
        -CAcreateserial -out "${OUTPUT_DIR}/node${i}.crt" -days 365 \\
        -extfile "${OUTPUT_DIR}/node${i}.ext"
    
    rm "${OUTPUT_DIR}/node${i}.csr" "${OUTPUT_DIR}/node${i}.ext"
    
    echo "✓ Node ${i} certificate generated"
done

echo "Certificate generation complete!"
echo "Directory: ${OUTPUT_DIR}"
echo ""
echo "Files:"
ls -la "${OUTPUT_DIR}/"
"""


def generate_certificates(output_dir: str, num_nodes: int = 5) -> None:
    """Generate certificates for MPC nodes"""
    import subprocess
    
    script_path = Path(output_dir) / "generate_certs.sh"
    script_path.write_text(generate_certs_script)
    script_path.chmod(0o755)
    
    result = subprocess.run(
        [str(script_path), output_dir, str(num_nodes)],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"Certificate generation failed: {result.stderr}")
    
    print(result.stdout)
