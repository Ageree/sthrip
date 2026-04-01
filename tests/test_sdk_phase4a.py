"""Unit tests for Sthrip SDK Phase 4a methods.

Tests cover:
- Treasury: set_treasury, treasury_policy, delete_treasury, treasury_status,
            treasury_rebalance, treasury_history
- Credit/Lending: credit_score, lend_offer, lending_offers, borrow,
                  repay_loan, my_loans
- Conditional: pay_when, conditional_payments, cancel_conditional
- Split: pay_split
- Multi-Party: pay_multi, accept_multi, reject_multi

All HTTP calls are intercepted by patching _raw_* helper methods
directly on the client instance -- no real network traffic.
"""

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: load SDK modules under sthrip_sdk namespace
# ---------------------------------------------------------------------------

_SDK_STHRIP_DIR = Path(__file__).parent.parent / "sdk" / "sthrip"


def _load_sdk_module(alias: str, filename: str):
    """Load an SDK source file under a custom module alias."""
    path = _SDK_STHRIP_DIR / filename
    spec = importlib.util.spec_from_file_location(alias, str(path))
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "sthrip_sdk"
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


def _bootstrap_sdk():
    """Register a fake sthrip_sdk package so relative imports in client.py work."""
    if "sthrip_sdk" in sys.modules:
        return

    pkg = types.ModuleType("sthrip_sdk")
    pkg.__path__ = [str(_SDK_STHRIP_DIR)]
    pkg.__package__ = "sthrip_sdk"
    sys.modules["sthrip_sdk"] = pkg

    exc_mod = _load_sdk_module("sthrip_sdk.exceptions", "exceptions.py")
    auth_mod = _load_sdk_module("sthrip_sdk.auth", "auth.py")
    client_mod = _load_sdk_module("sthrip_sdk.client", "client.py")

    pkg.exceptions = exc_mod
    pkg.auth = auth_mod
    pkg.client = client_mod


_bootstrap_sdk()

from sthrip_sdk.client import Sthrip  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_client(api_key: str = "test-key-abc", api_url: str = "http://test"):
    """Create a Sthrip client with all network I/O suppressed."""
    with (
        patch("sthrip_sdk.client.load_credentials", return_value=None),
        patch.object(Sthrip, "_auto_register", return_value=api_key),
        patch.dict(os.environ, {"STHRIP_API_KEY": api_key}, clear=False),
    ):
        client = Sthrip(api_key=api_key, api_url=api_url)

    # Replace the underlying session so no real HTTP calls can escape.
    mock_session = MagicMock()
    client._session = mock_session
    return client


# ===========================================================================
# Treasury tests
# ===========================================================================


class TestSetTreasury:

    def test_calls_put_treasury_policy_endpoint(self):
        client = _build_client()
        allocation = {"operations": 40, "reserves": 30, "growth": 30}
        with patch.object(client, "_raw_put", return_value={"status": "ok"}) as mock:
            client.set_treasury(allocation)
        mock.assert_called_once()
        assert mock.call_args[0][0] == "/v2/me/treasury/policy"

    def test_payload_contains_allocation(self):
        client = _build_client()
        allocation = {"operations": 40, "reserves": 30, "growth": 30}
        with patch.object(client, "_raw_put", return_value={}) as mock:
            client.set_treasury(allocation)
        payload = mock.call_args[1]["json_body"]
        assert payload["allocation"] == allocation

    def test_default_rebalance_threshold(self):
        client = _build_client()
        with patch.object(client, "_raw_put", return_value={}) as mock:
            client.set_treasury({"ops": 100})
        payload = mock.call_args[1]["json_body"]
        assert payload["rebalance_threshold_pct"] == 5

    def test_custom_rebalance_threshold(self):
        client = _build_client()
        with patch.object(client, "_raw_put", return_value={}) as mock:
            client.set_treasury({"ops": 100}, rebalance_threshold_pct=10)
        payload = mock.call_args[1]["json_body"]
        assert payload["rebalance_threshold_pct"] == 10

    def test_default_cooldown_minutes(self):
        client = _build_client()
        with patch.object(client, "_raw_put", return_value={}) as mock:
            client.set_treasury({"ops": 100})
        payload = mock.call_args[1]["json_body"]
        assert payload["cooldown_minutes"] == 60

    def test_custom_cooldown_minutes(self):
        client = _build_client()
        with patch.object(client, "_raw_put", return_value={}) as mock:
            client.set_treasury({"ops": 100}, cooldown_minutes=30)
        payload = mock.call_args[1]["json_body"]
        assert payload["cooldown_minutes"] == 30

    def test_default_emergency_reserve_pct(self):
        client = _build_client()
        with patch.object(client, "_raw_put", return_value={}) as mock:
            client.set_treasury({"ops": 100})
        payload = mock.call_args[1]["json_body"]
        assert payload["emergency_reserve_pct"] == 10

    def test_custom_emergency_reserve_pct(self):
        client = _build_client()
        with patch.object(client, "_raw_put", return_value={}) as mock:
            client.set_treasury({"ops": 100}, emergency_reserve_pct=20)
        payload = mock.call_args[1]["json_body"]
        assert payload["emergency_reserve_pct"] == 20

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"status": "ok", "policy_id": "p-1"}
        with patch.object(client, "_raw_put", return_value=expected):
            result = client.set_treasury({"ops": 100})
        assert result == expected


class TestTreasuryPolicy:

    def test_calls_get_treasury_policy_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.treasury_policy()
        mock.assert_called_once_with("/v2/me/treasury/policy")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"allocation": {"ops": 40}, "rebalance_threshold_pct": 5}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.treasury_policy()
        assert result == expected


class TestDeleteTreasury:

    def test_calls_delete_treasury_policy_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_request", return_value={"deleted": True}) as mock:
            client.delete_treasury()
        mock.assert_called_once_with("DELETE", "/v2/me/treasury/policy")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"deleted": True}
        with patch.object(client, "_raw_request", return_value=expected):
            result = client.delete_treasury()
        assert result == expected


class TestTreasuryStatus:

    def test_calls_get_treasury_status_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.treasury_status()
        mock.assert_called_once_with("/v2/me/treasury/status")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"total": "10.0", "buckets": {"ops": "4.0"}}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.treasury_status()
        assert result == expected


class TestTreasuryRebalance:

    def test_calls_post_treasury_rebalance_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.treasury_rebalance()
        mock.assert_called_once_with("/v2/me/treasury/rebalance")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"rebalanced": True, "moves": []}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.treasury_rebalance()
        assert result == expected


class TestTreasuryHistory:

    def test_calls_get_treasury_history_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.treasury_history()
        mock.assert_called_once()
        assert mock.call_args[0][0] == "/v2/me/treasury/history"

    def test_default_limit_is_50(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.treasury_history()
        params = mock.call_args[1]["params"]
        assert params["limit"] == 50

    def test_custom_limit(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.treasury_history(limit=10)
        params = mock.call_args[1]["params"]
        assert params["limit"] == 10

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"history": [{"action": "rebalance"}]}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.treasury_history(limit=5)
        assert result == expected


# ===========================================================================
# Credit / Lending tests
# ===========================================================================


class TestCreditScore:

    def test_calls_get_credit_score_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.credit_score()
        mock.assert_called_once_with("/v2/me/credit-score")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"score": 750, "factors": []}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.credit_score()
        assert result == expected


class TestLendOffer:

    def test_calls_post_lending_offers_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={"id": "off-1"}) as mock:
            client.lend_offer(
                max_amount=10.0,
                currency="XMR",
                interest_rate_bps=500,
                max_duration_secs=86400,
            )
        mock.assert_called_once()
        assert mock.call_args[0][0] == "/v2/lending/offers"

    def test_payload_contains_required_fields(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(
                max_amount=10.0,
                currency="XMR",
                interest_rate_bps=500,
                max_duration_secs=86400,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["max_amount"] == "10.0"
        assert payload["currency"] == "XMR"
        assert payload["interest_rate_bps"] == 500
        assert payload["max_duration_secs"] == 86400

    def test_max_amount_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(
                max_amount=5.5,
                currency="XMR",
                interest_rate_bps=300,
                max_duration_secs=3600,
            )
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["max_amount"], str)

    def test_default_min_credit_score(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(1.0, "XMR", 100, 3600)
        payload = mock.call_args[1]["json_body"]
        assert payload["min_credit_score"] == 0

    def test_custom_min_credit_score(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(1.0, "XMR", 100, 3600, min_credit_score=600)
        payload = mock.call_args[1]["json_body"]
        assert payload["min_credit_score"] == 600

    def test_default_require_collateral_false(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(1.0, "XMR", 100, 3600)
        payload = mock.call_args[1]["json_body"]
        assert payload["require_collateral"] is False

    def test_custom_require_collateral(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(1.0, "XMR", 100, 3600, require_collateral=True)
        payload = mock.call_args[1]["json_body"]
        assert payload["require_collateral"] is True

    def test_default_collateral_ratio(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(1.0, "XMR", 100, 3600)
        payload = mock.call_args[1]["json_body"]
        assert payload["collateral_ratio"] == 150

    def test_custom_collateral_ratio(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(1.0, "XMR", 100, 3600, collateral_ratio=200)
        payload = mock.call_args[1]["json_body"]
        assert payload["collateral_ratio"] == 200

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"id": "off-abc", "status": "active"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.lend_offer(1.0, "XMR", 100, 3600)
        assert result == expected


class TestLendingOffers:

    def test_calls_get_lending_offers_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.lending_offers()
        mock.assert_called_once()
        assert mock.call_args[0][0] == "/v2/lending/offers"

    def test_no_currency_filter_sends_no_params(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.lending_offers()
        params = mock.call_args[1]["params"]
        assert "currency" not in params

    def test_currency_filter_is_forwarded(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.lending_offers(currency="XMR")
        params = mock.call_args[1]["params"]
        assert params["currency"] == "XMR"

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"offers": [{"id": "off-1"}]}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.lending_offers()
        assert result == expected


class TestBorrow:

    def test_calls_post_loans_request_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.borrow(amount=5.0, currency="XMR", duration_secs=86400)
        mock.assert_called_once()
        assert mock.call_args[0][0] == "/v2/loans/request"

    def test_payload_contains_required_fields(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.borrow(amount=5.0, currency="XMR", duration_secs=86400)
        payload = mock.call_args[1]["json_body"]
        assert payload["amount"] == "5.0"
        assert payload["currency"] == "XMR"
        assert payload["duration_secs"] == 86400

    def test_amount_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.borrow(2.5, "XMR", 3600)
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["amount"], str)

    def test_default_collateral_amount(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.borrow(1.0, "XMR", 3600)
        payload = mock.call_args[1]["json_body"]
        assert payload["collateral_amount"] == "0"

    def test_custom_collateral_amount(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.borrow(1.0, "XMR", 3600, collateral_amount=1.5)
        payload = mock.call_args[1]["json_body"]
        assert payload["collateral_amount"] == "1.5"

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"loan_id": "loan-1", "status": "pending"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.borrow(1.0, "XMR", 3600)
        assert result == expected


class TestRepayLoan:

    def test_calls_post_loans_repay_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.repay_loan("loan-abc")
        mock.assert_called_once_with("/v2/loans/{}/repay".format("loan-abc"))

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"status": "repaid", "loan_id": "loan-abc"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.repay_loan("loan-abc")
        assert result == expected


class TestMyLoans:

    def test_calls_get_loans_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.my_loans()
        mock.assert_called_once_with("/v2/loans")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"loans": [{"id": "loan-1", "status": "active"}]}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.my_loans()
        assert result == expected


# ===========================================================================
# Conditional payments tests
# ===========================================================================


class TestPayWhen:

    def test_calls_post_conditional_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_when(
                to_agent="agent-bob",
                amount=1.0,
                condition_type="on_completion",
                condition_config={"task_id": "t-1"},
            )
        mock.assert_called_once()
        assert mock.call_args[0][0] == "/v2/payments/conditional"

    def test_payload_contains_required_fields(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_when(
                to_agent="agent-bob",
                amount=1.0,
                condition_type="on_completion",
                condition_config={"task_id": "t-1"},
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["to_agent_name"] == "agent-bob"
        assert payload["amount"] == "1.0"
        assert payload["condition_type"] == "on_completion"
        assert payload["condition_config"] == {"task_id": "t-1"}

    def test_amount_converted_to_string(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_when("bob", 2.5, "threshold", {})
        payload = mock.call_args[1]["json_body"]
        assert isinstance(payload["amount"], str)

    def test_default_currency_is_xmr(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_when("bob", 1.0, "threshold", {})
        payload = mock.call_args[1]["json_body"]
        assert payload["currency"] == "XMR"

    def test_custom_currency(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_when("bob", 1.0, "threshold", {}, currency="xUSD")
        payload = mock.call_args[1]["json_body"]
        assert payload["currency"] == "xUSD"

    def test_default_expires_hours(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_when("bob", 1.0, "threshold", {})
        payload = mock.call_args[1]["json_body"]
        assert payload["expires_hours"] == 24

    def test_custom_expires_hours(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_when("bob", 1.0, "threshold", {}, expires_hours=48)
        payload = mock.call_args[1]["json_body"]
        assert payload["expires_hours"] == 48

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"id": "cp-1", "status": "pending"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.pay_when("bob", 1.0, "threshold", {})
        assert result == expected


class TestConditionalPayments:

    def test_calls_get_conditional_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.conditional_payments()
        mock.assert_called_once_with("/v2/payments/conditional")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"payments": [{"id": "cp-1"}]}
        with patch.object(client, "_raw_get", return_value=expected):
            result = client.conditional_payments()
        assert result == expected


class TestCancelConditional:

    def test_calls_delete_conditional_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_request", return_value={}) as mock:
            client.cancel_conditional("cp-abc")
        mock.assert_called_once_with("DELETE", "/v2/payments/conditional/cp-abc")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"cancelled": True}
        with patch.object(client, "_raw_request", return_value=expected):
            result = client.cancel_conditional("cp-abc")
        assert result == expected


# ===========================================================================
# Split payments tests
# ===========================================================================


class TestPaySplit:

    def test_calls_post_split_endpoint(self):
        client = _build_client()
        recipients = [
            {"agent_name": "alice", "amount": 1.0},
            {"agent_name": "bob", "amount": 2.0},
        ]
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_split(recipients)
        mock.assert_called_once()
        assert mock.call_args[0][0] == "/v2/payments/split"

    def test_payload_contains_recipients(self):
        client = _build_client()
        recipients = [
            {"agent_name": "alice", "amount": 1.0},
            {"agent_name": "bob", "amount": 2.0},
        ]
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_split(recipients)
        payload = mock.call_args[1]["json_body"]
        assert payload["recipients"] == recipients

    def test_default_currency_is_xmr(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_split([{"agent_name": "a", "amount": 1.0}])
        payload = mock.call_args[1]["json_body"]
        assert payload["currency"] == "XMR"

    def test_custom_currency(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_split([{"agent_name": "a", "amount": 1.0}], currency="xUSD")
        payload = mock.call_args[1]["json_body"]
        assert payload["currency"] == "xUSD"

    def test_default_memo_is_none(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_split([{"agent_name": "a", "amount": 1.0}])
        payload = mock.call_args[1]["json_body"]
        assert "memo" not in payload

    def test_custom_memo(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_split(
                [{"agent_name": "a", "amount": 1.0}],
                memo="split payment for project",
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["memo"] == "split payment for project"

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"split_id": "sp-1", "payments": []}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.pay_split([{"agent_name": "a", "amount": 1.0}])
        assert result == expected


# ===========================================================================
# Multi-party payments tests
# ===========================================================================


class TestPayMulti:

    def test_calls_post_multi_endpoint(self):
        client = _build_client()
        recipients = [
            {"agent_name": "alice", "amount": 1.0},
            {"agent_name": "bob", "amount": 2.0},
        ]
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi(recipients)
        mock.assert_called_once()
        assert mock.call_args[0][0] == "/v2/payments/multi"

    def test_payload_contains_recipients(self):
        client = _build_client()
        recipients = [
            {"agent_name": "alice", "amount": 1.0},
            {"agent_name": "bob", "amount": 2.0},
        ]
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi(recipients)
        payload = mock.call_args[1]["json_body"]
        assert payload["recipients"] == recipients

    def test_default_currency_is_xmr(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi([{"agent_name": "a", "amount": 1.0}])
        payload = mock.call_args[1]["json_body"]
        assert payload["currency"] == "XMR"

    def test_custom_currency(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi(
                [{"agent_name": "a", "amount": 1.0}],
                currency="xUSD",
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["currency"] == "xUSD"

    def test_default_require_all_accept_true(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi([{"agent_name": "a", "amount": 1.0}])
        payload = mock.call_args[1]["json_body"]
        assert payload["require_all_accept"] is True

    def test_custom_require_all_accept(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi(
                [{"agent_name": "a", "amount": 1.0}],
                require_all_accept=False,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["require_all_accept"] is False

    def test_default_accept_hours(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi([{"agent_name": "a", "amount": 1.0}])
        payload = mock.call_args[1]["json_body"]
        assert payload["accept_hours"] == 2

    def test_custom_accept_hours(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi(
                [{"agent_name": "a", "amount": 1.0}],
                accept_hours=12,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["accept_hours"] == 12

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"id": "mp-1", "status": "pending_acceptance"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.pay_multi([{"agent_name": "a", "amount": 1.0}])
        assert result == expected


class TestAcceptMulti:

    def test_calls_post_multi_accept_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.accept_multi("mp-abc")
        mock.assert_called_once_with("/v2/payments/multi/mp-abc/accept")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"status": "accepted"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.accept_multi("mp-abc")
        assert result == expected


class TestRejectMulti:

    def test_calls_post_multi_reject_endpoint(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.reject_multi("mp-abc")
        mock.assert_called_once_with("/v2/payments/multi/mp-abc/reject")

    def test_returns_api_response(self):
        client = _build_client()
        expected = {"status": "rejected"}
        with patch.object(client, "_raw_post", return_value=expected):
            result = client.reject_multi("mp-abc")
        assert result == expected


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:

    def test_set_treasury_empty_allocation(self):
        client = _build_client()
        with patch.object(client, "_raw_put", return_value={}) as mock:
            client.set_treasury({})
        payload = mock.call_args[1]["json_body"]
        assert payload["allocation"] == {}

    def test_pay_split_single_recipient(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_split([{"agent_name": "solo", "amount": 5.0}])
        payload = mock.call_args[1]["json_body"]
        assert len(payload["recipients"]) == 1

    def test_pay_multi_single_recipient(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_multi([{"agent_name": "solo", "amount": 5.0}])
        payload = mock.call_args[1]["json_body"]
        assert len(payload["recipients"]) == 1

    def test_borrow_zero_collateral(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.borrow(1.0, "XMR", 3600, collateral_amount=0)
        payload = mock.call_args[1]["json_body"]
        assert payload["collateral_amount"] == "0"

    def test_pay_when_unicode_condition_config(self):
        client = _build_client()
        config = {"description": "Deliver report on time"}
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.pay_when("bob", 1.0, "custom", config)
        payload = mock.call_args[1]["json_body"]
        assert payload["condition_config"] == config

    def test_lend_offer_all_optional_params(self):
        client = _build_client()
        with patch.object(client, "_raw_post", return_value={}) as mock:
            client.lend_offer(
                max_amount=100.0,
                currency="xUSD",
                interest_rate_bps=1000,
                max_duration_secs=604800,
                min_credit_score=700,
                require_collateral=True,
                collateral_ratio=200,
            )
        payload = mock.call_args[1]["json_body"]
        assert payload["max_amount"] == "100.0"
        assert payload["currency"] == "xUSD"
        assert payload["interest_rate_bps"] == 1000
        assert payload["max_duration_secs"] == 604800
        assert payload["min_credit_score"] == 700
        assert payload["require_collateral"] is True
        assert payload["collateral_ratio"] == 200

    def test_treasury_history_zero_limit(self):
        client = _build_client()
        with patch.object(client, "_raw_get", return_value={}) as mock:
            client.treasury_history(limit=0)
        params = mock.call_args[1]["params"]
        assert params["limit"] == 0

    def test_cancel_conditional_special_characters_in_id(self):
        client = _build_client()
        payment_id = "cp-abc-123-def"
        with patch.object(client, "_raw_request", return_value={}) as mock:
            client.cancel_conditional(payment_id)
        mock.assert_called_once_with(
            "DELETE", "/v2/payments/conditional/{}".format(payment_id)
        )
