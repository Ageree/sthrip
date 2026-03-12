"""
URL validation with SSRF protection.
Used at registration time AND before each webhook send.
"""

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

from sthrip.config import get_settings


class SSRFBlockedError(Exception):
    """Raised when a URL targets a private/internal IP."""
    pass


def validate_url_target(url: str, *, enforce_https: Optional[bool] = None, block_on_dns_failure: bool = False) -> str:
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
        enforce_https = get_settings().environment != "dev"

    allowed_schemes = ("https",) if enforce_https else ("http", "https")
    if parsed.scheme not in allowed_schemes:
        raise ValueError(f"URL must use {'/'.join(allowed_schemes)}, got {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a valid hostname")

    _check_hostname_safe(hostname, block_on_dns_failure=block_on_dns_failure)
    return url


def _check_hostname_safe(hostname: str, *, block_on_dns_failure: bool = False) -> None:
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
        if block_on_dns_failure:
            raise SSRFBlockedError(f"DNS resolution failed for hostname '{hostname}'")
        # Allow at registration time; webhook sender should use block_on_dns_failure=True


from typing import Tuple, Union


def resolve_and_validate(url: str, *, enforce_https: Optional[bool] = None) -> Tuple[str, str]:
    """Validate URL and return (validated_url, resolved_ip).

    The caller should pin the connection to resolved_ip to prevent DNS rebinding.
    """
    validated = validate_url_target(url, enforce_https=enforce_https)
    parsed = urlparse(url)
    hostname = parsed.hostname

    # If IP literal, use directly
    try:
        addr = ipaddress.ip_address(hostname)
        return validated, str(addr)
    except ValueError:
        pass

    # Resolve DNS and return first safe IP
    results = socket.getaddrinfo(hostname, None)
    for _, _, _, _, sockaddr in results:
        addr = ipaddress.ip_address(sockaddr[0])
        if not _is_dangerous_ip(addr):
            return validated, sockaddr[0]

    raise SSRFBlockedError(f"No safe IP found for hostname '{hostname}'")


def _is_dangerous_ip(addr: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """Check if an IP address is private, loopback, reserved, link-local, multicast, or unspecified."""
    return (
        addr.is_private or addr.is_loopback or addr.is_reserved
        or addr.is_link_local or addr.is_multicast or addr.is_unspecified
    )
