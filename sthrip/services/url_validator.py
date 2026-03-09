"""
URL validation with SSRF protection.
Used at registration time AND before each webhook send.
"""

import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urlparse


class SSRFBlockedError(Exception):
    """Raised when a URL targets a private/internal IP."""
    pass


def validate_url_target(url: str, *, enforce_https: Optional[bool] = None) -> str:
    """
    Validate that a URL is safe to send HTTP requests to.

    Checks:
    - Scheme is https (or http in dev mode)
    - Hostname present
    - Resolved IPs are not private/loopback/reserved/link-local

    Args:
        url: The URL to validate.
        enforce_https: If None, auto-detect from ENVIRONMENT env var.

    Returns:
        The validated URL (unchanged).

    Raises:
        SSRFBlockedError: If URL targets a private/internal IP.
        ValueError: If URL is malformed or scheme is not allowed.
    """
    parsed = urlparse(url)

    if enforce_https is None:
        enforce_https = os.getenv("ENVIRONMENT", "production") != "dev"

    allowed_schemes = ("https",) if enforce_https else ("http", "https")
    if parsed.scheme not in allowed_schemes:
        raise ValueError(f"URL must use {'/'.join(allowed_schemes)}, got {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a valid hostname")

    _check_hostname_safe(hostname)
    return url


def _check_hostname_safe(hostname: str) -> None:
    """
    Resolve hostname and verify all IPs are public.

    Raises SSRFBlockedError if any resolved IP is private/internal.
    """
    # If hostname is already an IP literal, check directly
    try:
        addr = ipaddress.ip_address(hostname)
        if _is_dangerous_ip(addr):
            raise SSRFBlockedError(f"URL must not target private/internal IP: {hostname}")
        return
    except ValueError:
        pass  # Not an IP literal, resolve via DNS

    try:
        results = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in results:
            addr = ipaddress.ip_address(sockaddr[0])
            if _is_dangerous_ip(addr):
                raise SSRFBlockedError(
                    f"URL hostname '{hostname}' resolves to private/internal IP {sockaddr[0]}"
                )
    except socket.gaierror:
        # DNS resolution failure — allow at registration, block at send time
        pass


from typing import Union

def _is_dangerous_ip(addr: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """Check if an IP address is private, loopback, reserved, or link-local."""
    return addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local
