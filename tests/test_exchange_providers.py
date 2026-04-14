"""
Unit tests for exchange_providers — ChangeNowProvider, SideShiftProvider,
and the fallback helper create_order_with_fallback.

All HTTP calls are mocked; no network traffic is made.
"""

import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from sthrip.services.exchange_providers import (
    ChangeNowProvider,
    ExchangeProviderError,
    SideShiftProvider,
    STATUS_FINISHED,
    STATUS_WAITING,
    STATUS_FAILED,
    STATUS_EXPIRED,
    create_order_with_fallback,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_API_KEY = "test-api-key-123"
_FAKE_ORDER_ID = "abc123xyz"
_FAKE_DEPOSIT_ADDR = "bc1qdeposit000test"
_FAKE_XMR_ADDR = "4AbCdEfGhIjKlMnOpQrStUvWxYz1234567890abcdef"


def _mock_http_response(status_code: int, json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


# ---------------------------------------------------------------------------
# ChangeNowProvider — create_order
# ---------------------------------------------------------------------------


class TestChangeNowProviderCreateOrder:
    def test_success_returns_dict(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(200, {
            "id": _FAKE_ORDER_ID,
            "payinAddress": _FAKE_DEPOSIT_ADDR,
            "amount": "0.01",
        })
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.create_order("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

        assert result["external_order_id"] == _FAKE_ORDER_ID
        assert result["deposit_address"] == _FAKE_DEPOSIT_ADDR
        assert result["expected_amount"] == "0.01"
        assert result["provider"] == "changenow"

    def test_http_error_raises_provider_error(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(500, {"error": "internal"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            with pytest.raises(ExchangeProviderError, match="HTTP 500"):
                provider.create_order("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

    def test_missing_api_key_raises(self):
        provider = ChangeNowProvider(api_key="")
        with pytest.raises(ExchangeProviderError, match="CHANGENOW_API_KEY"):
            provider.create_order("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

    def test_missing_order_id_in_response_raises(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        # Response missing "id" and "transactionId"
        fake_resp = _mock_http_response(200, {"payinAddress": _FAKE_DEPOSIT_ADDR})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            with pytest.raises(ExchangeProviderError, match="unexpected response shape"):
                provider.create_order("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

    def test_network_error_raises_provider_error(self):
        import httpx as _httpx
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _httpx.RequestError("timeout")

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            with pytest.raises(ExchangeProviderError, match="network error"):
                provider.create_order("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("CHANGENOW_API_KEY", "env-key-xyz")
        provider = ChangeNowProvider()
        assert provider._api_key == "env-key-xyz"

    def test_uses_transaction_id_field_as_fallback(self):
        """Some ChangeNOW responses use transactionId instead of id."""
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(200, {
            "transactionId": "txn-456",
            "payinAddress": _FAKE_DEPOSIT_ADDR,
        })
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.create_order("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

        assert result["external_order_id"] == "txn-456"


# ---------------------------------------------------------------------------
# ChangeNowProvider — get_order_status
# ---------------------------------------------------------------------------


class TestChangeNowProviderGetOrderStatus:
    def test_finished_status_normalised(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(200, {
            "status": "finished",
            "amountTo": "1.23",
        })
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.get_order_status(_FAKE_ORDER_ID)

        assert result["status"] == STATUS_FINISHED
        assert result["to_amount"] == "1.23"
        assert result["provider"] == "changenow"

    def test_unknown_status_falls_back_to_waiting(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(200, {"status": "brand_new_status"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.get_order_status(_FAKE_ORDER_ID)

        assert result["status"] == STATUS_WAITING

    def test_http_404_raises(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(404, {"message": "not found"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            with pytest.raises(ExchangeProviderError, match="HTTP 404"):
                provider.get_order_status(_FAKE_ORDER_ID)

    def test_expired_status(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(200, {"status": "expired"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.get_order_status(_FAKE_ORDER_ID)

        assert result["status"] == STATUS_EXPIRED

    def test_refunded_maps_to_failed(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(200, {"status": "refunded"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.get_order_status(_FAKE_ORDER_ID)

        assert result["status"] == STATUS_FAILED

    def test_to_amount_none_when_missing(self):
        provider = ChangeNowProvider(api_key=_FAKE_API_KEY)
        fake_resp = _mock_http_response(200, {"status": "waiting"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.get_order_status(_FAKE_ORDER_ID)

        assert result["to_amount"] is None


# ---------------------------------------------------------------------------
# SideShiftProvider — create_order
# ---------------------------------------------------------------------------


class TestSideShiftProviderCreateOrder:
    def test_success_returns_dict(self):
        provider = SideShiftProvider(affiliate_id="aff-123")
        fake_resp = _mock_http_response(201, {
            "id": "ss-order-789",
            "depositAddress": _FAKE_DEPOSIT_ADDR,
            "depositAmount": "0.02",
        })
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.create_order("ETH", "0.02", "XMR", _FAKE_XMR_ADDR)

        assert result["external_order_id"] == "ss-order-789"
        assert result["deposit_address"] == _FAKE_DEPOSIT_ADDR
        assert result["expected_amount"] == "0.02"
        assert result["provider"] == "sideshift"

    def test_http_400_raises(self):
        provider = SideShiftProvider(affiliate_id="aff-123")
        fake_resp = _mock_http_response(400, {"error": {"message": "bad request"}})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            with pytest.raises(ExchangeProviderError, match="HTTP 400"):
                provider.create_order("ETH", "0.02", "XMR", _FAKE_XMR_ADDR)

    def test_network_error_raises(self):
        import httpx as _httpx
        provider = SideShiftProvider()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = _httpx.RequestError("connection refused")

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            with pytest.raises(ExchangeProviderError, match="network error"):
                provider.create_order("ETH", "0.02", "XMR", _FAKE_XMR_ADDR)

    def test_missing_id_raises(self):
        provider = SideShiftProvider()
        fake_resp = _mock_http_response(200, {"depositAddress": _FAKE_DEPOSIT_ADDR})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            with pytest.raises(ExchangeProviderError, match="unexpected response shape"):
                provider.create_order("ETH", "0.02", "XMR", _FAKE_XMR_ADDR)

    def test_affiliate_id_from_env(self, monkeypatch):
        monkeypatch.setenv("SIDESHIFT_AFFILIATE_ID", "env-aff-xyz")
        provider = SideShiftProvider()
        assert provider._affiliate_id == "env-aff-xyz"

    def test_works_without_affiliate_id(self):
        provider = SideShiftProvider(affiliate_id="")
        fake_resp = _mock_http_response(200, {
            "id": "no-aff-order",
            "depositAddress": _FAKE_DEPOSIT_ADDR,
        })
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.create_order("SOL", "1.0", "XMR", _FAKE_XMR_ADDR)

        assert result["external_order_id"] == "no-aff-order"


# ---------------------------------------------------------------------------
# SideShiftProvider — get_order_status
# ---------------------------------------------------------------------------


class TestSideShiftProviderGetOrderStatus:
    def test_settled_maps_to_finished(self):
        provider = SideShiftProvider()
        fake_resp = _mock_http_response(200, {
            "status": "settled",
            "settleAmount": "2.5",
        })
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.get_order_status("ss-order-789")

        assert result["status"] == STATUS_FINISHED
        assert result["to_amount"] == "2.5"
        assert result["provider"] == "sideshift"

    def test_refunding_maps_to_failed(self):
        provider = SideShiftProvider()
        fake_resp = _mock_http_response(200, {"status": "refunding"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.get_order_status("ss-order-789")

        assert result["status"] == STATUS_FAILED

    def test_http_error_raises(self):
        provider = SideShiftProvider()
        fake_resp = _mock_http_response(503, {"error": "service unavailable"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            with pytest.raises(ExchangeProviderError, match="HTTP 503"):
                provider.get_order_status("ss-order-789")

    def test_to_amount_none_when_pending(self):
        provider = SideShiftProvider()
        fake_resp = _mock_http_response(200, {"status": "waiting"})
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = fake_resp

        with patch("sthrip.services.exchange_providers.httpx.Client", return_value=mock_client):
            result = provider.get_order_status("ss-order-789")

        assert result["to_amount"] is None


# ---------------------------------------------------------------------------
# create_order_with_fallback
# ---------------------------------------------------------------------------


class TestCreateOrderWithFallback:
    def test_uses_changenow_on_success(self):
        changenow_result = {
            "external_order_id": "cn-001",
            "deposit_address": _FAKE_DEPOSIT_ADDR,
            "expected_amount": "0.01",
            "provider": "changenow",
        }
        mock_cn = MagicMock()
        mock_cn.create_order.return_value = changenow_result

        mock_ss = MagicMock()

        with patch(
            "sthrip.services.exchange_providers.get_provider_chain",
            return_value=[mock_cn, mock_ss],
        ):
            result = create_order_with_fallback("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

        assert result["provider"] == "changenow"
        mock_ss.create_order.assert_not_called()

    def test_falls_back_to_sideshift_on_changenow_failure(self):
        sideshift_result = {
            "external_order_id": "ss-001",
            "deposit_address": _FAKE_DEPOSIT_ADDR,
            "expected_amount": "0.01",
            "provider": "sideshift",
        }
        mock_cn = MagicMock()
        mock_cn.create_order.side_effect = ExchangeProviderError("changenow down")
        mock_cn._PROVIDER_NAME = "changenow"

        mock_ss = MagicMock()
        mock_ss.create_order.return_value = sideshift_result
        mock_ss._PROVIDER_NAME = "sideshift"

        with patch(
            "sthrip.services.exchange_providers.get_provider_chain",
            return_value=[mock_cn, mock_ss],
        ):
            result = create_order_with_fallback("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

        assert result["provider"] == "sideshift"

    def test_raises_when_all_providers_fail(self):
        mock_cn = MagicMock()
        mock_cn.create_order.side_effect = ExchangeProviderError("cn fail")
        mock_cn._PROVIDER_NAME = "changenow"

        mock_ss = MagicMock()
        mock_ss.create_order.side_effect = ExchangeProviderError("ss fail")
        mock_ss._PROVIDER_NAME = "sideshift"

        with patch(
            "sthrip.services.exchange_providers.get_provider_chain",
            return_value=[mock_cn, mock_ss],
        ):
            with pytest.raises(ExchangeProviderError, match="All exchange providers failed"):
                create_order_with_fallback("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)

    def test_empty_provider_list_raises(self):
        with patch(
            "sthrip.services.exchange_providers.get_provider_chain",
            return_value=[],
        ):
            with pytest.raises(ExchangeProviderError, match="All exchange providers failed"):
                create_order_with_fallback("BTC", "0.01", "XMR", _FAKE_XMR_ADDR)


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_changenow_satisfies_protocol(self):
        from sthrip.services.exchange_providers import ExchangeProvider
        provider = ChangeNowProvider(api_key="key")
        assert isinstance(provider, ExchangeProvider)

    def test_sideshift_satisfies_protocol(self):
        from sthrip.services.exchange_providers import ExchangeProvider
        provider = SideShiftProvider()
        assert isinstance(provider, ExchangeProvider)
