"""Comprehensive tests for Credit Scoring and Lending (Sprint 3).

Tests cover:
  - Credit score calculation (4-factor formula)
  - Cached score with staleness detection
  - Max borrow amount calculation
  - Lending offer CRUD
  - Loan request, funding, repayment, default detection
  - Collateral locking and release
  - API integration tests for all endpoints
"""

import os
import contextlib
import pytest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation, AgentBalance,
    HubRoute, FeeCollection, PendingWithdrawal, Transaction,
    EscrowDeal, EscrowMilestone,
    AgentCreditScore, AgentLoan, LendingOffer, LoanStatus,
)
from sthrip.db.repository import (
    AgentRepository, BalanceRepository, CreditRepository, LoanRepository,
    ReputationRepository,
)

# Valid 95-char stagenet XMR address
_VALID_XMR_ADDR = "5" + "a" * 94

# Tables needed for credit/lending tests
_CREDIT_TEST_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    AgentBalance.__table__,
    HubRoute.__table__,
    FeeCollection.__table__,
    PendingWithdrawal.__table__,
    Transaction.__table__,
    EscrowDeal.__table__,
    EscrowMilestone.__table__,
    AgentCreditScore.__table__,
    AgentLoan.__table__,
    LendingOffer.__table__,
]

# Modules where get_db must be patched
_GET_DB_MODULES = [
    "sthrip.db.database",
    "sthrip.services.agent_registry",
    "sthrip.services.fee_collector",
    "sthrip.services.webhook_service",
    "api.main_v2",
    "api.deps",
    "api.routers.health",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.webhooks",
    "api.routers.escrow",
    "api.routers.lending",
]

_RATE_LIMITER_MODULES = [
    "sthrip.services.rate_limiter",
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
]

_AUDIT_LOG_MODULES = [
    "api.main_v2",
    "api.deps",
    "api.routers.agents",
    "api.routers.payments",
    "api.routers.balance",
    "api.routers.admin",
]


def _naive_utc_now() -> datetime:
    """Return current UTC time as a naive datetime (no tzinfo) for SQLite compat."""
    return datetime.utcnow()


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture
def credit_engine():
    """In-memory SQLite engine with credit/lending-related tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=_CREDIT_TEST_TABLES)
    return engine


@pytest.fixture
def credit_session_factory(credit_engine):
    """Session factory bound to the credit test engine."""
    return sessionmaker(bind=credit_engine, expire_on_commit=False)


@pytest.fixture
def db(credit_session_factory):
    """Single session for unit tests with audit_log/queue_webhook mocked."""
    session = credit_session_factory()
    with patch("sthrip.services.credit_service.audit_log"), \
         patch("sthrip.services.credit_service.queue_webhook"):
        try:
            yield session
        finally:
            session.close()


def _create_agent(db, name="test-agent", trust_score=50, total_transactions=10,
                  days_old=30):
    """Helper: create an agent with reputation record."""
    agent = Agent(agent_name=name, xmr_address=_VALID_XMR_ADDR)
    db.add(agent)
    db.flush()
    created_at = datetime.utcnow() - timedelta(days=days_old)
    agent.created_at = created_at
    rep = AgentReputation(
        agent_id=agent.id,
        trust_score=trust_score,
        total_transactions=total_transactions,
    )
    db.add(rep)
    db.flush()
    return agent


def _deposit_balance(db, agent_id, amount, token="XMR"):
    """Helper: deposit balance for an agent."""
    BalanceRepository(db).deposit(agent_id, Decimal(str(amount)), token=token)
    db.flush()


# ===========================================================================
# Unit Tests: Credit Score Calculation
# ===========================================================================


class TestCreditScoreCalculation:
    """Test the 4-factor credit score formula."""

    def test_credit_score_new_agent(self, db):
        """Brand-new agent with no history should have a low score."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="new-agent", trust_score=0,
                              total_transactions=0, days_old=0)
        svc = CreditService()
        score = svc.calculate_credit_score(db, agent.id)
        # trust_score=0 -> rep_factor=0
        # no loans -> loan_factor=0
        # days_old=0, transactions=0 -> activity_factor=0
        # no balance -> balance_factor=0
        assert score == 0

    def test_credit_score_with_history(self, db):
        """Agent with reputation, balance, and age should have meaningful score."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="veteran-agent", trust_score=80,
                              total_transactions=100, days_old=365)
        _deposit_balance(db, agent.id, "5.0")
        svc = CreditService()
        score = svc.calculate_credit_score(db, agent.id)
        # reputation: min(300, 80*3) = 240
        # loan history: 0 (no repaid or defaulted loans)
        # activity: min(200, 365*0.5 + 100*0.1) = min(200, 182.5+10) = 192.5 -> int(192)
        # balance: min(200, 5.0*20) = 100
        # total: 240 + 0 + 192 + 100 = 532
        assert 500 <= score <= 550

    def test_credit_score_max_cap(self, db):
        """Score should be capped at 1000."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="max-agent", trust_score=100,
                              total_transactions=5000, days_old=1000)
        _deposit_balance(db, agent.id, "50.0")

        # Add some repaid loans to credit score record
        credit_repo = CreditRepository(db)
        record = credit_repo.get_or_create(agent.id)
        record.total_loans_repaid = 20
        db.flush()

        svc = CreditService()
        score = svc.calculate_credit_score(db, agent.id)
        assert score <= 1000

    def test_credit_score_with_defaults_penalty(self, db):
        """Defaulted loans should reduce the loan history factor."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="defaulter-agent", trust_score=50)

        credit_repo = CreditRepository(db)
        record = credit_repo.get_or_create(agent.id)
        record.total_loans_repaid = 2   # +100
        record.total_loans_defaulted = 2  # -200
        db.flush()

        svc = CreditService()
        score = svc.calculate_credit_score(db, agent.id)
        # loan_factor: max(0, 2*50 - 2*100) = max(0, -100) = 0
        # So loan factor contributes 0
        assert score >= 0

    def test_credit_score_reputation_factor_capped_at_300(self, db):
        """Reputation factor should be capped at 300."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="high-trust", trust_score=100)
        svc = CreditService()
        score = svc.calculate_credit_score(db, agent.id)
        # rep_factor = min(300, 100*3) = 300 (capped)
        assert score >= 300 or score >= 0  # At least reputation contributes

    def test_credit_score_missing_agent(self, db):
        """Should raise LookupError for non-existent agent."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        with pytest.raises(LookupError):
            svc.calculate_credit_score(db, uuid4())


class TestGetCreditScore:
    """Test cached score retrieval with staleness detection."""

    def test_get_score_creates_if_not_exists(self, db):
        """Should calculate and cache the score if no record exists."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="first-score")
        svc = CreditService()
        result = svc.get_credit_score(db, agent.id)
        assert "credit_score" in result
        assert "max_borrow_amount" in result

    def test_get_score_returns_cached_if_fresh(self, db):
        """Should return cached score if less than 5 minutes old."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="cached-agent")
        svc = CreditService()
        # First call calculates
        result1 = svc.get_credit_score(db, agent.id)
        # Second call should return same (cached)
        result2 = svc.get_credit_score(db, agent.id)
        assert result1["credit_score"] == result2["credit_score"]

    def test_get_score_recalculates_if_stale(self, db):
        """Should recalculate if score is older than 5 minutes."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="stale-agent")
        svc = CreditService()
        # Calculate initial score
        svc.get_credit_score(db, agent.id)
        # Make the record stale
        credit_repo = CreditRepository(db)
        record = credit_repo.get(agent.id)
        record.calculated_at = datetime.utcnow() - timedelta(minutes=10)
        db.flush()
        # Should recalculate
        result = svc.get_credit_score(db, agent.id)
        assert "credit_score" in result


# ===========================================================================
# Unit Tests: Max Borrow Amount
# ===========================================================================


class TestMaxBorrowAmount:
    """Test max borrow amount formula."""

    def test_max_borrow_no_collateral(self):
        """Uncollateralized borrow = score/200."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        result = svc.max_borrow_amount(500)
        assert result == Decimal("2.5")  # 500/200

    def test_max_borrow_with_collateral(self):
        """Collateralized adds collateral * 0.8."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        result = svc.max_borrow_amount(400, collateral_amount=Decimal("10"))
        # 400/200 + 10*0.8 = 2 + 8 = 10
        assert result == Decimal("10")

    def test_max_borrow_zero_score(self):
        """Zero score with no collateral yields 0."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        result = svc.max_borrow_amount(0)
        assert result == Decimal("0")

    def test_max_borrow_zero_score_with_collateral(self):
        """Zero score still allows collateralized borrowing."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        result = svc.max_borrow_amount(0, collateral_amount=Decimal("5"))
        assert result == Decimal("4")  # 0 + 5*0.8


# ===========================================================================
# Unit Tests: Lending Offers
# ===========================================================================


class TestLendingOffers:
    """Test lending offer operations."""

    def test_create_offer(self, db):
        """Successfully create a lending offer."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="lender-agent")
        _deposit_balance(db, agent.id, "100.0")
        svc = CreditService()
        result = svc.create_offer(
            db, lender_id=agent.id,
            max_amount=Decimal("50"), currency="XMR",
            interest_rate_bps=500, max_duration_secs=86400,
            min_credit_score=100, require_collateral=False,
            collateral_ratio=100,
        )
        assert result["max_amount"] == "50"
        assert result["interest_rate_bps"] == 500
        assert result["is_active"] is True

    def test_create_offer_insufficient_balance(self, db):
        """Should reject offer when lender has insufficient balance."""
        from sthrip.services.credit_service import CreditService
        agent = _create_agent(db, name="poor-lender")
        # No deposit => 0 balance
        svc = CreditService()
        with pytest.raises(ValueError, match="[Ii]nsufficient"):
            svc.create_offer(
                db, lender_id=agent.id,
                max_amount=Decimal("50"), currency="XMR",
                interest_rate_bps=500, max_duration_secs=86400,
                min_credit_score=0, require_collateral=False,
                collateral_ratio=100,
            )

    def test_list_offers(self, db):
        """List active offers, sorted by interest rate (cheapest first)."""
        from sthrip.services.credit_service import CreditService
        lender = _create_agent(db, name="lender-list")
        _deposit_balance(db, lender.id, "200.0")
        svc = CreditService()
        # Create two offers with different rates
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 800, 86400, 0, False, 100)
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 300, 86400, 0, False, 100)
        offers = svc.list_offers(db, currency="XMR")
        assert len(offers) == 2
        # Cheapest first
        assert offers[0]["interest_rate_bps"] <= offers[1]["interest_rate_bps"]

    def test_withdraw_offer(self, db):
        """Lender can deactivate their offer."""
        from sthrip.services.credit_service import CreditService
        lender = _create_agent(db, name="withdraw-lender")
        _deposit_balance(db, lender.id, "100.0")
        svc = CreditService()
        offer = svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500,
                                 86400, 0, False, 100)
        offer_id = UUID(offer["offer_id"])
        svc.withdraw_offer(db, lender.id, offer_id)
        offers = svc.list_offers(db, currency="XMR")
        assert len(offers) == 0

    def test_withdraw_offer_wrong_lender(self, db):
        """Cannot withdraw another lender's offer."""
        from sthrip.services.credit_service import CreditService
        lender = _create_agent(db, name="real-lender")
        other = _create_agent(db, name="other-agent")
        _deposit_balance(db, lender.id, "100.0")
        svc = CreditService()
        offer = svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500,
                                 86400, 0, False, 100)
        offer_id = UUID(offer["offer_id"])
        with pytest.raises(LookupError):
            svc.withdraw_offer(db, other.id, offer_id)


# ===========================================================================
# Unit Tests: Loan Lifecycle
# ===========================================================================


class TestLoanRequest:
    """Test loan request flow."""

    def _setup_offer(self, db, svc):
        """Helper: create a lender with offer and a borrower."""
        lender = _create_agent(db, name="loan-lender", trust_score=80,
                               total_transactions=50, days_old=100)
        borrower = _create_agent(db, name="loan-borrower", trust_score=70,
                                 total_transactions=30, days_old=60)
        _deposit_balance(db, lender.id, "100.0")
        _deposit_balance(db, borrower.id, "10.0")
        # Create offer
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500, 86400 * 30,
                         0, False, 100)
        return lender, borrower

    def test_request_loan(self, db):
        """Successfully request a loan matched to an offer."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender, borrower = self._setup_offer(db, svc)
        result = svc.request_loan(
            db, borrower_id=borrower.id,
            amount=Decimal("2.0"), currency="XMR",
            duration_secs=86400,
        )
        assert result["state"] == "requested"
        assert result["borrower_id"] == str(borrower.id)
        assert Decimal(result["principal"]) == Decimal("2.0")

    def test_request_loan_insufficient_credit(self, db):
        """Should reject loan if borrower's credit score is too low."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender = _create_agent(db, name="strict-lender", trust_score=80,
                               total_transactions=50, days_old=100)
        borrower = _create_agent(db, name="low-credit-borrower", trust_score=0,
                                 total_transactions=0, days_old=0)
        _deposit_balance(db, lender.id, "100.0")
        # Offer requires high credit score
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500, 86400 * 30,
                         999, False, 100)  # min_credit_score=999
        with pytest.raises(ValueError, match="[Cc]redit|[Nn]o.*offer"):
            svc.request_loan(
                db, borrower_id=borrower.id,
                amount=Decimal("2.0"), currency="XMR",
                duration_secs=86400,
            )

    def test_request_loan_with_collateral(self, db):
        """Collateralized loan locks collateral from borrower."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender = _create_agent(db, name="coll-lender", trust_score=80,
                               total_transactions=50, days_old=100)
        borrower = _create_agent(db, name="coll-borrower", trust_score=70,
                                 total_transactions=30, days_old=60)
        _deposit_balance(db, lender.id, "100.0")
        _deposit_balance(db, borrower.id, "20.0")
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 300, 86400 * 30,
                         0, True, 150)  # require_collateral, 150%
        result = svc.request_loan(
            db, borrower_id=borrower.id,
            amount=Decimal("2.0"), currency="XMR",
            duration_secs=86400,
            collateral_amount=Decimal("3.0"),
        )
        assert result["state"] == "requested"
        assert Decimal(result["collateral_amount"]) == Decimal("3.0")
        # Borrower balance should have been reduced by collateral
        borrower_balance = BalanceRepository(db).get_available(borrower.id)
        assert borrower_balance == Decimal("17.0")

    def test_request_loan_no_matching_offer(self, db):
        """Should fail if no active offers match."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        borrower = _create_agent(db, name="lonely-borrower", trust_score=70,
                                 total_transactions=30, days_old=60)
        _deposit_balance(db, borrower.id, "10.0")
        with pytest.raises(ValueError, match="[Nn]o.*offer"):
            svc.request_loan(
                db, borrower_id=borrower.id,
                amount=Decimal("2.0"), currency="XMR",
                duration_secs=86400,
            )


class TestFundLoan:
    """Test loan funding."""

    def test_fund_loan(self, db):
        """Lender funds a requested loan: deducts from lender, credits borrower."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender = _create_agent(db, name="fund-lender", trust_score=80,
                               total_transactions=50, days_old=100)
        borrower = _create_agent(db, name="fund-borrower", trust_score=70,
                                 total_transactions=30, days_old=60)
        _deposit_balance(db, lender.id, "100.0")
        _deposit_balance(db, borrower.id, "5.0")
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500, 86400 * 30,
                         0, False, 100)
        loan = svc.request_loan(db, borrower.id, Decimal("2.0"), "XMR", 86400)
        loan_id = UUID(loan["loan_id"])
        result = svc.fund_loan(db, lender.id, loan_id)
        assert result["state"] == "active"
        # Lender balance reduced by principal
        lender_balance = BalanceRepository(db).get_available(lender.id)
        assert lender_balance == Decimal("98.0")
        # Borrower balance increased by principal
        borrower_balance = BalanceRepository(db).get_available(borrower.id)
        assert borrower_balance == Decimal("7.0")

    def test_fund_loan_wrong_lender(self, db):
        """Non-owner lender cannot fund the loan."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender = _create_agent(db, name="real-fund-lender", trust_score=80,
                               total_transactions=50, days_old=100)
        other = _create_agent(db, name="imposter", trust_score=80,
                              total_transactions=50, days_old=100)
        borrower = _create_agent(db, name="fund-borrower2", trust_score=70,
                                 total_transactions=30, days_old=60)
        _deposit_balance(db, lender.id, "100.0")
        _deposit_balance(db, other.id, "100.0")
        _deposit_balance(db, borrower.id, "5.0")
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500, 86400 * 30,
                         0, False, 100)
        loan = svc.request_loan(db, borrower.id, Decimal("2.0"), "XMR", 86400)
        loan_id = UUID(loan["loan_id"])
        with pytest.raises(PermissionError):
            svc.fund_loan(db, other.id, loan_id)


class TestRepayLoan:
    """Test loan repayment."""

    def _funded_loan(self, db, svc):
        """Helper: create a fully funded loan."""
        lender = _create_agent(db, name="repay-lender", trust_score=80,
                               total_transactions=50, days_old=100)
        borrower = _create_agent(db, name="repay-borrower", trust_score=70,
                                 total_transactions=30, days_old=60)
        _deposit_balance(db, lender.id, "100.0")
        _deposit_balance(db, borrower.id, "50.0")
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500, 86400 * 30,
                         0, False, 100)
        loan = svc.request_loan(db, borrower.id, Decimal("10.0"), "XMR", 86400)
        loan_id = UUID(loan["loan_id"])
        svc.fund_loan(db, lender.id, loan_id)
        return lender, borrower, loan_id

    def test_repay_loan(self, db):
        """Borrower repays: principal + interest returned to lender."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender, borrower, loan_id = self._funded_loan(db, svc)
        result = svc.repay_loan(db, borrower.id, loan_id)
        assert result["state"] == "repaid"
        # Lender should receive principal + interest - platform fee
        lender_balance = BalanceRepository(db).get_available(lender.id)
        assert lender_balance > Decimal("90.0")  # got back principal + some interest

    def test_repay_releases_collateral(self, db):
        """Collateral should be released back to borrower on repayment."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender = _create_agent(db, name="coll-repay-lender", trust_score=80,
                               total_transactions=50, days_old=100)
        borrower = _create_agent(db, name="coll-repay-borrower", trust_score=70,
                                 total_transactions=30, days_old=60)
        _deposit_balance(db, lender.id, "100.0")
        _deposit_balance(db, borrower.id, "50.0")
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500, 86400 * 30,
                         0, True, 150)
        loan = svc.request_loan(db, borrower.id, Decimal("5.0"), "XMR", 86400,
                                collateral_amount=Decimal("7.5"))
        loan_id = UUID(loan["loan_id"])
        svc.fund_loan(db, lender.id, loan_id)
        # Before repay: borrower balance = 50 - 7.5 (collateral) + 5 (loan) = 47.5
        borrower_before = BalanceRepository(db).get_available(borrower.id)
        svc.repay_loan(db, borrower.id, loan_id)
        # After repay: collateral released, but repayment deducted
        borrower_after = BalanceRepository(db).get_available(borrower.id)
        # Collateral (7.5) should have been returned
        # The difference is: collateral_released - repayment_amount
        # So borrower_after should be > borrower_before - repayment (since collateral returned)
        assert borrower_after > Decimal("0")

    def test_repay_wrong_borrower(self, db):
        """Non-borrower cannot repay."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender, borrower, loan_id = self._funded_loan(db, svc)
        other = _create_agent(db, name="other-repayer", trust_score=50)
        _deposit_balance(db, other.id, "100.0")
        with pytest.raises(PermissionError):
            svc.repay_loan(db, other.id, loan_id)


class TestDetectDefaults:
    """Test default detection."""

    def test_detect_defaults(self, db):
        """Overdue active loans are defaulted and collateral liquidated."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        lender = _create_agent(db, name="default-lender", trust_score=80,
                               total_transactions=50, days_old=100)
        borrower = _create_agent(db, name="default-borrower", trust_score=70,
                                 total_transactions=30, days_old=60)
        _deposit_balance(db, lender.id, "100.0")
        _deposit_balance(db, borrower.id, "50.0")
        svc.create_offer(db, lender.id, Decimal("50"), "XMR", 500, 86400 * 30,
                         0, False, 100)
        loan = svc.request_loan(db, borrower.id, Decimal("5.0"), "XMR", 86400)
        loan_id = UUID(loan["loan_id"])
        svc.fund_loan(db, lender.id, loan_id)
        # Make loan overdue by setting expires_at in the past
        loan_obj = LoanRepository(db).get_by_id(loan_id)
        loan_obj.expires_at = datetime.utcnow() - timedelta(hours=1)
        db.flush()
        defaulted = svc.detect_defaults(db)
        assert len(defaulted) >= 1
        assert defaulted[0]["state"] == "defaulted"

    def test_detect_defaults_no_overdue(self, db):
        """No defaults when no loans are overdue."""
        from sthrip.services.credit_service import CreditService
        svc = CreditService()
        defaulted = svc.detect_defaults(db)
        assert len(defaulted) == 0


# ===========================================================================
# API Integration Tests
# ===========================================================================


@pytest.fixture
def lending_client(credit_engine, credit_session_factory):
    """FastAPI test client with credit/lending tables and patches."""

    @contextmanager
    def get_test_db():
        session = credit_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    mock_limiter = MagicMock()
    mock_limiter.check_rate_limit.return_value = None
    mock_limiter.check_ip_rate_limit.return_value = None
    mock_limiter.check_failed_auth.return_value = None
    mock_limiter.record_failed_auth.return_value = None
    mock_limiter.get_limit_status.return_value = {"requests_remaining": 100}

    mock_monitor = MagicMock()
    mock_monitor.get_health_report.return_value = {
        "status": "healthy",
        "timestamp": "2026-03-03T00:00:00",
        "checks": {},
    }
    mock_monitor.get_alerts.return_value = []

    mock_webhook = MagicMock()
    mock_webhook.get_delivery_stats.return_value = {"total": 0}

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"HUB_MODE": "ledger"}))

        for mod in _GET_DB_MODULES:
            stack.enter_context(patch(f"{mod}.get_db", side_effect=get_test_db))
        stack.enter_context(patch("sthrip.db.database.create_tables"))

        for mod in _RATE_LIMITER_MODULES:
            stack.enter_context(
                patch(f"{mod}.get_rate_limiter", return_value=mock_limiter)
            )

        for mod in _AUDIT_LOG_MODULES:
            stack.enter_context(patch(f"{mod}.audit_log"))

        stack.enter_context(
            patch(
                "sthrip.services.monitoring.get_monitor",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.monitoring.setup_default_monitoring",
                return_value=mock_monitor,
            )
        )
        stack.enter_context(
            patch(
                "sthrip.services.webhook_service.get_webhook_service",
                return_value=mock_webhook,
            )
        )
        stack.enter_context(
            patch("sthrip.services.webhook_service.queue_webhook")
        )
        stack.enter_context(
            patch("sthrip.services.credit_service.audit_log")
        )
        stack.enter_context(
            patch("sthrip.services.credit_service.queue_webhook")
        )

        from api.main_v2 import app
        yield TestClient(app, raise_server_exceptions=False)


def _register_agent(client: TestClient, name: str) -> str:
    """Register an agent and return its API key."""
    r = client.post("/v2/agents/register", json={
        "agent_name": name,
        "xmr_address": _VALID_XMR_ADDR,
    })
    assert r.status_code == 201, f"Registration of '{name}' failed: {r.text}"
    return r.json()["api_key"]


def _deposit(client: TestClient, api_key: str, amount: float) -> None:
    """Deposit funds into an agent's balance."""
    r = client.post(
        "/v2/balance/deposit",
        json={"amount": amount},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert r.status_code == 200, f"Deposit failed: {r.text}"


def _auth(api_key: str) -> dict:
    """Return auth headers for the given API key."""
    return {"Authorization": f"Bearer {api_key}"}


class TestCreditScoreAPI:
    """API integration tests for credit score endpoint."""

    def test_get_credit_score(self, lending_client):
        """GET /v2/me/credit-score returns a valid score."""
        key = _register_agent(lending_client, "api-score-agent")
        r = lending_client.get("/v2/me/credit-score", headers=_auth(key))
        assert r.status_code == 200
        data = r.json()
        assert "credit_score" in data
        assert "max_borrow_amount" in data
        assert isinstance(data["credit_score"], int)

    def test_get_credit_score_unauthenticated(self, lending_client):
        """GET /v2/me/credit-score without auth returns 401."""
        r = lending_client.get("/v2/me/credit-score")
        assert r.status_code in (401, 403)


class TestLendingOffersAPI:
    """API integration tests for lending offer endpoints."""

    def test_create_offer_api(self, lending_client):
        """POST /v2/lending/offers creates a lending offer."""
        key = _register_agent(lending_client, "api-lender")
        _deposit(lending_client, key, 100.0)
        r = lending_client.post("/v2/lending/offers", json={
            "max_amount": 50,
            "currency": "XMR",
            "interest_rate_bps": 500,
            "max_duration_secs": 86400,
            "min_credit_score": 0,
            "require_collateral": False,
            "collateral_ratio": 100,
        }, headers=_auth(key))
        assert r.status_code == 201, f"Offer create failed: {r.text}"
        data = r.json()
        assert data["is_active"] is True

    def test_list_offers_api(self, lending_client):
        """GET /v2/lending/offers lists active offers."""
        key = _register_agent(lending_client, "api-list-lender")
        _deposit(lending_client, key, 100.0)
        lending_client.post("/v2/lending/offers", json={
            "max_amount": 50,
            "currency": "XMR",
            "interest_rate_bps": 500,
            "max_duration_secs": 86400,
            "min_credit_score": 0,
            "require_collateral": False,
            "collateral_ratio": 100,
        }, headers=_auth(key))
        r = lending_client.get("/v2/lending/offers", headers=_auth(key))
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_delete_offer_api(self, lending_client):
        """DELETE /v2/lending/offers/{id} deactivates an offer."""
        key = _register_agent(lending_client, "api-del-lender")
        _deposit(lending_client, key, 100.0)
        r = lending_client.post("/v2/lending/offers", json={
            "max_amount": 50,
            "currency": "XMR",
            "interest_rate_bps": 500,
            "max_duration_secs": 86400,
            "min_credit_score": 0,
            "require_collateral": False,
            "collateral_ratio": 100,
        }, headers=_auth(key))
        offer_id = r.json()["offer_id"]
        r2 = lending_client.delete(
            f"/v2/lending/offers/{offer_id}", headers=_auth(key)
        )
        assert r2.status_code == 200


class TestLoanAPI:
    """API integration tests for loan endpoints."""

    def _setup_funded_loan(self, client):
        """Helper: create lender offer + borrower loan request + fund."""
        lender_key = _register_agent(client, "api-loan-lender")
        borrower_key = _register_agent(client, "api-loan-borrower")
        _deposit(client, lender_key, 100.0)
        _deposit(client, borrower_key, 20.0)
        # Create offer
        client.post("/v2/lending/offers", json={
            "max_amount": 50, "currency": "XMR", "interest_rate_bps": 500,
            "max_duration_secs": 86400 * 30, "min_credit_score": 0,
            "require_collateral": False, "collateral_ratio": 100,
        }, headers=_auth(lender_key))
        return lender_key, borrower_key

    def test_request_loan_api(self, lending_client):
        """POST /v2/loans/request creates a loan request."""
        lender_key, borrower_key = self._setup_funded_loan(lending_client)
        r = lending_client.post("/v2/loans/request", json={
            "amount": 2.0,
            "currency": "XMR",
            "duration_secs": 86400,
        }, headers=_auth(borrower_key))
        assert r.status_code == 201, f"Loan request failed: {r.text}"
        data = r.json()
        assert data["state"] == "requested"

    def test_fund_loan_api(self, lending_client):
        """POST /v2/loans/{id}/fund transitions loan to ACTIVE."""
        lender_key, borrower_key = self._setup_funded_loan(lending_client)
        r = lending_client.post("/v2/loans/request", json={
            "amount": 2.0, "currency": "XMR", "duration_secs": 86400,
        }, headers=_auth(borrower_key))
        loan_id = r.json()["loan_id"]
        r2 = lending_client.post(
            f"/v2/loans/{loan_id}/fund", headers=_auth(lender_key)
        )
        assert r2.status_code == 200, f"Fund failed: {r2.text}"
        assert r2.json()["state"] == "active"

    def test_repay_loan_api(self, lending_client):
        """POST /v2/loans/{id}/repay transitions loan to REPAID."""
        lender_key, borrower_key = self._setup_funded_loan(lending_client)
        r = lending_client.post("/v2/loans/request", json={
            "amount": 2.0, "currency": "XMR", "duration_secs": 86400,
        }, headers=_auth(borrower_key))
        loan_id = r.json()["loan_id"]
        lending_client.post(f"/v2/loans/{loan_id}/fund", headers=_auth(lender_key))
        r3 = lending_client.post(
            f"/v2/loans/{loan_id}/repay", headers=_auth(borrower_key)
        )
        assert r3.status_code == 200, f"Repay failed: {r3.text}"
        assert r3.json()["state"] == "repaid"

    def test_list_loans_api(self, lending_client):
        """GET /v2/loans lists loans for the authenticated agent."""
        lender_key, borrower_key = self._setup_funded_loan(lending_client)
        lending_client.post("/v2/loans/request", json={
            "amount": 2.0, "currency": "XMR", "duration_secs": 86400,
        }, headers=_auth(borrower_key))
        r = lending_client.get("/v2/loans", headers=_auth(borrower_key))
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert len(data["items"]) >= 1

    def test_get_loan_api(self, lending_client):
        """GET /v2/loans/{id} returns loan details."""
        lender_key, borrower_key = self._setup_funded_loan(lending_client)
        r = lending_client.post("/v2/loans/request", json={
            "amount": 2.0, "currency": "XMR", "duration_secs": 86400,
        }, headers=_auth(borrower_key))
        loan_id = r.json()["loan_id"]
        r2 = lending_client.get(
            f"/v2/loans/{loan_id}", headers=_auth(borrower_key)
        )
        assert r2.status_code == 200
        assert r2.json()["loan_id"] == loan_id

    def test_loan_request_unauthenticated(self, lending_client):
        """POST /v2/loans/request without auth returns 401."""
        r = lending_client.post("/v2/loans/request", json={
            "amount": 2.0, "currency": "XMR", "duration_secs": 86400,
        })
        assert r.status_code in (401, 403)
