"""Tests for sthrip/services/url_validator.py — SSRF protection."""

import ipaddress
import socket
from unittest.mock import patch

import pytest

from sthrip.services.url_validator import (
    SSRFBlockedError,
    _check_hostname_safe,
    _is_dangerous_ip,
    resolve_and_validate,
    validate_url_target,
)


# ── SSRFBlockedError ──────────────────────────────────────────────────────


class TestSSRFBlockedError:
    def test_is_exception(self):
        err = SSRFBlockedError("blocked")
        assert isinstance(err, Exception)
        assert str(err) == "blocked"


# ── _is_dangerous_ip ─────────────────────────────────────────────────────


class TestIsDangerousIp:
    @pytest.mark.parametrize("ip", ["10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1"])
    def test_private_ipv4(self, ip):
        assert _is_dangerous_ip(ipaddress.ip_address(ip)) is True

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
    def test_public_ipv4(self, ip):
        assert _is_dangerous_ip(ipaddress.ip_address(ip)) is False

    def test_loopback_ipv6(self):
        assert _is_dangerous_ip(ipaddress.ip_address("::1")) is True

    def test_link_local_ipv4(self):
        assert _is_dangerous_ip(ipaddress.ip_address("169.254.1.1")) is True

    def test_link_local_ipv6(self):
        assert _is_dangerous_ip(ipaddress.ip_address("fe80::1")) is True

    def test_multicast_ipv4(self):
        assert _is_dangerous_ip(ipaddress.ip_address("224.0.0.1")) is True

    def test_multicast_ipv6(self):
        assert _is_dangerous_ip(ipaddress.ip_address("ff02::1")) is True

    def test_unspecified_ipv4(self):
        assert _is_dangerous_ip(ipaddress.ip_address("0.0.0.0")) is True

    def test_unspecified_ipv6(self):
        assert _is_dangerous_ip(ipaddress.ip_address("::")) is True


# ── validate_url_target — valid URLs ─────────────────────────────────────


class TestValidateUrlTargetValid:
    @patch("sthrip.services.url_validator._check_hostname_safe")
    def test_https_url_accepted(self, mock_check):
        result = validate_url_target("https://example.com/hook", enforce_https=True)
        assert result == "https://example.com/hook"
        mock_check.assert_called_once_with("example.com", block_on_dns_failure=False)

    @patch("sthrip.services.url_validator._check_hostname_safe")
    def test_http_allowed_in_dev(self, mock_check):
        result = validate_url_target("http://example.com/hook", enforce_https=False)
        assert result == "http://example.com/hook"

    @patch("sthrip.services.url_validator._check_hostname_safe")
    @patch.dict("os.environ", {"ENVIRONMENT": "dev"})
    def test_http_allowed_when_env_is_dev(self, mock_check):
        result = validate_url_target("http://example.com/hook")
        assert result == "http://example.com/hook"

    @patch("sthrip.services.url_validator._check_hostname_safe")
    @patch.dict("os.environ", {"ENVIRONMENT": "production", "MONERO_NETWORK": "mainnet", "MONERO_RPC_PASS": "secure-rpc-pass-123", "API_KEY_HMAC_SECRET": "test-hmac-secret-long-enough-32chars!!", "MONERO_RPC_HOST": "monero-rpc.internal"})
    def test_https_enforced_in_production(self, mock_check):
        from sthrip.config import get_settings
        get_settings.cache_clear()
        with pytest.raises(ValueError, match="must use https"):
            validate_url_target("http://example.com/hook")


# ── validate_url_target — scheme errors ──────────────────────────────────


class TestValidateUrlTargetScheme:
    def test_ftp_scheme_rejected(self):
        with pytest.raises(ValueError, match="must use"):
            validate_url_target("ftp://example.com/file", enforce_https=True)

    def test_file_scheme_rejected(self):
        with pytest.raises(ValueError, match="must use"):
            validate_url_target("file:///etc/passwd", enforce_https=False)

    def test_empty_string(self):
        with pytest.raises(ValueError):
            validate_url_target("", enforce_https=False)

    def test_none_raises(self):
        with pytest.raises(Exception):
            validate_url_target(None, enforce_https=False)  # type: ignore[arg-type]

    def test_no_scheme(self):
        with pytest.raises(ValueError):
            validate_url_target("example.com/path", enforce_https=False)


# ── validate_url_target — hostname errors ────────────────────────────────


class TestValidateUrlTargetHostname:
    def test_missing_hostname(self):
        with pytest.raises(ValueError, match="valid hostname"):
            validate_url_target("https:///path", enforce_https=True)


# ── validate_url_target — private IPs (SSRF) ────────────────────────────


class TestValidateUrlTargetSSRF:
    @pytest.mark.parametrize(
        "ip",
        ["10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1"],
    )
    def test_private_ip_literal_blocked(self, ip):
        with pytest.raises(SSRFBlockedError, match="private/internal IP"):
            validate_url_target(f"https://{ip}/hook", enforce_https=True)

    def test_ipv6_loopback_blocked(self):
        with pytest.raises(SSRFBlockedError):
            validate_url_target("https://[::1]/hook", enforce_https=True)

    def test_ipv6_link_local_blocked(self):
        with pytest.raises(SSRFBlockedError):
            validate_url_target("https://[fe80::1]/hook", enforce_https=True)


# ── _check_hostname_safe — DNS resolution ────────────────────────────────


class TestCheckHostnameSafeDNS:
    def test_dns_resolves_to_private_ip_blocked(self):
        fake_result = [(socket.AF_INET, 0, 0, "", ("10.0.0.1", 0))]
        with patch("sthrip.services.url_validator.socket.getaddrinfo", return_value=fake_result):
            with pytest.raises(SSRFBlockedError, match="resolves to private"):
                _check_hostname_safe("evil.example.com")

    def test_dns_resolves_to_public_ip_ok(self):
        fake_result = [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]
        with patch("sthrip.services.url_validator.socket.getaddrinfo", return_value=fake_result):
            _check_hostname_safe("example.com")  # should not raise

    def test_dns_failure_allowed(self):
        with patch("sthrip.services.url_validator.socket.getaddrinfo", side_effect=socket.gaierror("DNS fail")):
            _check_hostname_safe("nonexistent.test")  # should not raise

    def test_multiple_ips_one_private_blocked(self):
        fake_results = [
            (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
            (socket.AF_INET, 0, 0, "", ("10.0.0.1", 0)),
        ]
        with patch("sthrip.services.url_validator.socket.getaddrinfo", return_value=fake_results):
            with pytest.raises(SSRFBlockedError):
                _check_hostname_safe("multi.example.com")

    def test_ipv6_private_via_dns(self):
        fake_result = [(socket.AF_INET6, 0, 0, "", ("::1", 0, 0, 0))]
        with patch("sthrip.services.url_validator.socket.getaddrinfo", return_value=fake_result):
            with pytest.raises(SSRFBlockedError):
                _check_hostname_safe("ipv6evil.example.com")


# ── resolve_and_validate (HIGH-8: DNS rebinding protection) ──────────────


class TestResolveAndValidate:
    """Tests for resolve_and_validate which returns (url, resolved_ip)."""

    @patch("sthrip.services.url_validator._check_hostname_safe")
    def test_ip_literal_returns_ip_directly(self, mock_check):
        url, ip = resolve_and_validate("https://93.184.216.34/hook", enforce_https=True)
        assert url == "https://93.184.216.34/hook"
        assert ip == "93.184.216.34"

    @patch("sthrip.services.url_validator._check_hostname_safe")
    @patch("sthrip.services.url_validator.socket.getaddrinfo")
    def test_hostname_resolves_to_safe_ip(self, mock_dns, mock_check):
        mock_dns.return_value = [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))]
        url, ip = resolve_and_validate("https://example.com/hook", enforce_https=True)
        assert url == "https://example.com/hook"
        assert ip == "93.184.216.34"

    @patch("sthrip.services.url_validator._check_hostname_safe")
    @patch("sthrip.services.url_validator.socket.getaddrinfo")
    def test_skips_dangerous_ips_returns_safe_one(self, mock_dns, mock_check):
        mock_dns.return_value = [
            (socket.AF_INET, 0, 0, "", ("10.0.0.1", 0)),
            (socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)),
        ]
        url, ip = resolve_and_validate("https://example.com/hook", enforce_https=True)
        assert ip == "93.184.216.34"

    @patch("sthrip.services.url_validator._check_hostname_safe")
    @patch("sthrip.services.url_validator.socket.getaddrinfo")
    def test_all_dangerous_ips_raises(self, mock_dns, mock_check):
        mock_dns.return_value = [
            (socket.AF_INET, 0, 0, "", ("10.0.0.1", 0)),
            (socket.AF_INET, 0, 0, "", ("192.168.1.1", 0)),
        ]
        with pytest.raises(SSRFBlockedError, match="No safe IP"):
            resolve_and_validate("https://evil.example.com/hook", enforce_https=True)

    def test_private_ip_literal_blocked_by_validate(self):
        with pytest.raises(SSRFBlockedError):
            resolve_and_validate("https://127.0.0.1/hook", enforce_https=True)
