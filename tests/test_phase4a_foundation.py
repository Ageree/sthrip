"""
Phase 4a Sprint 1: Foundation tests -- enums, models, and repositories.

Tests CRUD operations, state transitions, and data integrity for all 9
new tables and 5 new repos introduced in Phase 4a.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base, Agent, AgentReputation,
    TreasuryPolicy, TreasuryForecast, TreasuryRebalanceLog,
    AgentCreditScore, AgentLoan, LendingOffer,
    ConditionalPayment,
    MultiPartyPayment, MultiPartyRecipient,
)
from sthrip.db.enums import (
    LoanStatus, ConditionalPaymentState, MultiPartyPaymentState,
)
from sthrip.db.treasury_repo import TreasuryRepository
from sthrip.db.credit_repo import CreditRepository
from sthrip.db.loan_repo import LoanRepository
from sthrip.db.conditional_payment_repo import ConditionalPaymentRepository
from sthrip.db.multi_party_repo import MultiPartyRepository


# ── Fixtures ──────────────────────────────────────────────────────────────

_PHASE4A_TABLES = [
    Agent.__table__,
    AgentReputation.__table__,
    TreasuryPolicy.__table__,
    TreasuryForecast.__table__,
    TreasuryRebalanceLog.__table__,
    AgentCreditScore.__table__,
    AgentLoan.__table__,
    LendingOffer.__table__,
    ConditionalPayment.__table__,
    MultiPartyPayment.__table__,
    MultiPartyRecipient.__table__,
]


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=_PHASE4A_TABLES)
    return eng


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


@pytest.fixture
def agent_id(session) -> uuid.UUID:
    """Create a test agent and return its ID."""
    aid = uuid.uuid4()
    agent = Agent(id=aid, agent_name=f"test-agent-{aid.hex[:8]}")
    session.add(agent)
    session.flush()
    return aid


@pytest.fixture
def agent_ids(session):
    """Create 3 test agents and return their IDs."""
    ids = []
    for i in range(3):
        aid = uuid.uuid4()
        session.add(Agent(id=aid, agent_name=f"agent-{i}-{aid.hex[:8]}"))
        ids.append(aid)
    session.flush()
    return ids


# ═══════════════════════════════════════════════════════════════════════════
# ENUM TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestEnums:
    def test_enum_loan_status(self):
        assert issubclass(LoanStatus, str)
        expected = {"requested", "active", "repaid", "defaulted", "liquidated", "cancelled"}
        actual = {s.value for s in LoanStatus}
        assert actual == expected

    def test_enum_conditional_payment_state(self):
        assert issubclass(ConditionalPaymentState, str)
        expected = {"pending", "triggered", "executed", "expired", "cancelled"}
        actual = {s.value for s in ConditionalPaymentState}
        assert actual == expected

    def test_enum_multi_party_payment_state(self):
        assert issubclass(MultiPartyPaymentState, str)
        expected = {"pending", "accepted", "completed", "rejected", "expired"}
        actual = {s.value for s in MultiPartyPaymentState}
        assert actual == expected


# ═══════════════════════════════════════════════════════════════════════════
# TREASURY MODEL + REPO TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestTreasuryModels:
    def test_treasury_policy_create(self, session, agent_id):
        policy = TreasuryPolicy(
            agent_id=agent_id,
            target_allocation={"XMR": 40, "xUSD": 50, "xEUR": 10},
            rebalance_threshold_pct=10,
            emergency_reserve_pct=15,
        )
        session.add(policy)
        session.flush()

        fetched = session.query(TreasuryPolicy).filter_by(agent_id=agent_id).first()
        assert fetched is not None
        assert fetched.target_allocation == {"XMR": 40, "xUSD": 50, "xEUR": 10}
        assert fetched.rebalance_threshold_pct == 10
        assert fetched.emergency_reserve_pct == 15
        assert fetched.is_active is True

    def test_treasury_policy_unique_agent(self, session, agent_id):
        session.add(TreasuryPolicy(
            agent_id=agent_id,
            target_allocation={"XMR": 100},
        ))
        session.flush()

        session.add(TreasuryPolicy(
            agent_id=agent_id,
            target_allocation={"xUSD": 100},
        ))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_treasury_forecast_create(self, session, agent_id):
        source = uuid.uuid4()
        forecast = TreasuryForecast(
            agent_id=agent_id,
            forecast_type="subscription_due",
            source_id=source,
            expected_amount=Decimal("0.5"),
            expected_currency="XMR",
            direction="outflow",
            expected_at=datetime.now(timezone.utc) + timedelta(hours=24),
            confidence=Decimal("0.95"),
        )
        session.add(forecast)
        session.flush()

        fetched = session.query(TreasuryForecast).filter_by(agent_id=agent_id).first()
        assert fetched is not None
        assert fetched.forecast_type == "subscription_due"
        assert fetched.direction == "outflow"
        assert fetched.expected_amount == Decimal("0.5")

    def test_treasury_rebalance_log_create(self, session, agent_id):
        log = TreasuryRebalanceLog(
            agent_id=agent_id,
            trigger="manual",
            conversions=[{"from": "XMR", "to": "xUSD", "amount": "0.5"}],
            pre_allocation={"XMR": 60, "xUSD": 30, "xEUR": 10},
            post_allocation={"XMR": 40, "xUSD": 50, "xEUR": 10},
            total_value_xusd=Decimal("150.00"),
        )
        session.add(log)
        session.flush()

        fetched = session.query(TreasuryRebalanceLog).filter_by(agent_id=agent_id).first()
        assert fetched is not None
        assert fetched.trigger == "manual"
        assert fetched.total_value_xusd == Decimal("150.00")


class TestTreasuryRepo:
    def test_treasury_repo_set_get_policy(self, session, agent_id):
        repo = TreasuryRepository(session)
        allocation = {"XMR": 40, "xUSD": 50, "xEUR": 10}

        policy = repo.set_policy(
            agent_id=agent_id,
            target_allocation=allocation,
            rebalance_threshold_pct=5,
            emergency_reserve_pct=20,
        )
        assert policy.target_allocation == allocation
        assert policy.rebalance_threshold_pct == 5

        # Update via set_policy (upsert)
        updated = repo.set_policy(
            agent_id=agent_id,
            target_allocation={"XMR": 100},
            emergency_reserve_pct=5,
        )
        assert updated.id == policy.id
        assert updated.target_allocation == {"XMR": 100}
        assert updated.emergency_reserve_pct == 5

        fetched = repo.get_policy(agent_id)
        assert fetched.target_allocation == {"XMR": 100}

    def test_treasury_repo_add_forecast(self, session, agent_id):
        repo = TreasuryRepository(session)
        source = uuid.uuid4()
        future = datetime.now(timezone.utc) + timedelta(hours=12)

        forecast = repo.add_forecast(
            agent_id=agent_id,
            forecast_type="escrow_release",
            source_id=source,
            expected_amount=Decimal("1.5"),
            expected_currency="XMR",
            direction="inflow",
            expected_at=future,
        )
        assert forecast.id is not None

        forecasts = repo.list_forecasts(agent_id)
        assert len(forecasts) == 1
        assert forecasts[0].forecast_type == "escrow_release"
        assert forecasts[0].direction == "inflow"

        # Filter by direction
        inflows = repo.list_forecasts(agent_id, direction="inflow")
        assert len(inflows) == 1
        outflows = repo.list_forecasts(agent_id, direction="outflow")
        assert len(outflows) == 0

    def test_treasury_repo_add_rebalance_log(self, session, agent_id):
        repo = TreasuryRepository(session)

        log = repo.add_rebalance_log(
            agent_id=agent_id,
            trigger="threshold_breach",
            conversions=[{"from": "XMR", "to": "xUSD", "amount": "0.5", "rate": "150.00"}],
            pre_allocation={"XMR": 60, "xUSD": 30, "xEUR": 10},
            post_allocation={"XMR": 40, "xUSD": 50, "xEUR": 10},
            total_value_xusd=Decimal("200.00"),
        )
        assert log.id is not None

        history = repo.list_rebalance_history(agent_id)
        assert len(history) == 1
        assert history[0].trigger == "threshold_breach"


# ═══════════════════════════════════════════════════════════════════════════
# CREDIT SCORE MODEL + REPO TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestCreditModels:
    def test_credit_score_create(self, session, agent_id):
        score = AgentCreditScore(agent_id=agent_id)
        session.add(score)
        session.flush()

        fetched = session.query(AgentCreditScore).filter_by(agent_id=agent_id).first()
        assert fetched is not None
        assert fetched.credit_score == 0
        assert fetched.total_loans_taken == 0
        assert fetched.total_loans_repaid == 0
        assert fetched.total_loans_defaulted == 0
        assert fetched.max_borrow_amount == Decimal("0")
        assert fetched.max_concurrent_loans == 0


class TestCreditRepo:
    def test_credit_repo_get_or_create(self, session, agent_id):
        repo = CreditRepository(session)

        # First call creates
        record = repo.get_or_create(agent_id)
        assert record.agent_id == agent_id
        assert record.credit_score == 0

        # Second call returns existing
        record2 = repo.get_or_create(agent_id)
        assert record2.agent_id == record.agent_id

    def test_credit_repo_update_score(self, session, agent_id):
        repo = CreditRepository(session)
        repo.get_or_create(agent_id)

        rows = repo.update_score(
            agent_id=agent_id,
            credit_score=650,
            max_borrow_amount=Decimal("3.25"),
            max_concurrent_loans=3,
        )
        assert rows == 1

        record = repo.get(agent_id)
        assert record.credit_score == 650
        assert record.max_borrow_amount == Decimal("3.25")
        assert record.max_concurrent_loans == 3


# ═══════════════════════════════════════════════════════════════════════════
# LOAN MODEL + REPO TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestLoanModels:
    def test_loan_create(self, session, agent_ids):
        lender_id, borrower_id, _ = agent_ids
        loan = AgentLoan(
            loan_hash="abc123" * 5 + "abcd",
            lender_id=lender_id,
            borrower_id=borrower_id,
            principal=Decimal("1.0"),
            interest_rate_bps=100,
            duration_secs=3600,
            repayment_amount=Decimal("1.01"),
            state=LoanStatus.REQUESTED,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session.add(loan)
        session.flush()

        fetched = session.query(AgentLoan).filter_by(id=loan.id).first()
        assert fetched is not None
        assert fetched.principal == Decimal("1.0")
        assert fetched.state == LoanStatus.REQUESTED


class TestLoanRepo:
    def _make_loan(self, repo: LoanRepository, lender_id, borrower_id) -> AgentLoan:
        return repo.create(
            loan_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            lender_id=lender_id,
            borrower_id=borrower_id,
            principal=Decimal("1.0"),
            interest_rate_bps=100,
            duration_secs=3600,
            repayment_amount=Decimal("1.01"),
        )

    def test_loan_state_transitions(self, session, agent_ids):
        lender_id, borrower_id, _ = agent_ids
        repo = LoanRepository(session)

        loan = self._make_loan(repo, lender_id, borrower_id)
        assert loan.state == LoanStatus.REQUESTED

        # REQUESTED -> ACTIVE
        rows = repo.fund(loan.id)
        assert rows == 1
        session.expire_all()
        loan = repo.get_by_id(loan.id)
        assert loan.state == LoanStatus.ACTIVE
        assert loan.funded_at is not None

        # ACTIVE -> REPAID
        rows = repo.repay(loan.id, Decimal("1.01"))
        assert rows == 1
        session.expire_all()
        loan = repo.get_by_id(loan.id)
        assert loan.state == LoanStatus.REPAID
        assert loan.repaid_at is not None

    def test_loan_default_transition(self, session, agent_ids):
        lender_id, borrower_id, _ = agent_ids
        repo = LoanRepository(session)

        loan = self._make_loan(repo, lender_id, borrower_id)
        repo.fund(loan.id)

        # ACTIVE -> DEFAULTED
        rows = repo.default(loan.id)
        assert rows == 1
        session.expire_all()
        loan = repo.get_by_id(loan.id)
        assert loan.state == LoanStatus.DEFAULTED
        assert loan.defaulted_at is not None

        # DEFAULTED -> LIQUIDATED
        rows = repo.liquidate(loan.id)
        assert rows == 1
        session.expire_all()
        loan = repo.get_by_id(loan.id)
        assert loan.state == LoanStatus.LIQUIDATED

    def test_loan_cancel(self, session, agent_ids):
        lender_id, borrower_id, _ = agent_ids
        repo = LoanRepository(session)

        loan = self._make_loan(repo, lender_id, borrower_id)

        # REQUESTED -> CANCELLED
        rows = repo.cancel(loan.id)
        assert rows == 1
        session.expire_all()
        loan = repo.get_by_id(loan.id)
        assert loan.state == LoanStatus.CANCELLED

    def test_loan_repo_list_by_agent(self, session, agent_ids):
        lender_id, borrower_id, _ = agent_ids
        repo = LoanRepository(session)

        self._make_loan(repo, lender_id, borrower_id)
        self._make_loan(repo, lender_id, borrower_id)

        # List as lender
        items, total = repo.list_by_agent(lender_id, role="lender")
        assert total == 2
        assert len(items) == 2

        # List as borrower
        items, total = repo.list_by_agent(borrower_id, role="borrower")
        assert total == 2

        # List all for lender (includes as lender)
        items, total = repo.list_by_agent(lender_id)
        assert total == 2


class TestLendingOffer:
    def test_lending_offer_create(self, session, agent_id):
        repo = LoanRepository(session)
        offer = repo.create_offer(
            lender_id=agent_id,
            max_amount=Decimal("5.0"),
            interest_rate_bps=50,
            max_duration_secs=3600,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            min_borrower_credit_score=500,
        )
        assert offer.id is not None
        assert offer.is_active is True
        assert offer.remaining_amount == Decimal("5.0")

    def test_lending_offer_deactivate(self, session, agent_id):
        repo = LoanRepository(session)
        offer = repo.create_offer(
            lender_id=agent_id,
            max_amount=Decimal("5.0"),
            interest_rate_bps=50,
            max_duration_secs=3600,
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )

        rows = repo.deactivate_offer(offer.id, agent_id)
        assert rows == 1

        session.expire_all()
        offers = repo.list_active_offers()
        assert len(offers) == 0


# ═══════════════════════════════════════════════════════════════════════════
# CONDITIONAL PAYMENT MODEL + REPO TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestConditionalPaymentModels:
    def test_conditional_payment_create(self, session, agent_ids):
        sender_id, recipient_id, _ = agent_ids
        payment = ConditionalPayment(
            payment_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            from_agent_id=sender_id,
            to_agent_id=recipient_id,
            amount=Decimal("0.1"),
            condition_type="time_lock",
            condition_config={"release_at": "2026-04-02T12:00:00Z"},
            locked_amount=Decimal("0.1"),
            state=ConditionalPaymentState.PENDING,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        session.add(payment)
        session.flush()

        fetched = session.query(ConditionalPayment).filter_by(id=payment.id).first()
        assert fetched is not None
        assert fetched.condition_type == "time_lock"
        assert fetched.state == ConditionalPaymentState.PENDING


class TestConditionalPaymentRepo:
    def _make_payment(
        self, repo: ConditionalPaymentRepository, sender_id, recipient_id, **kwargs
    ) -> ConditionalPayment:
        defaults = dict(
            payment_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            from_agent_id=sender_id,
            to_agent_id=recipient_id,
            amount=Decimal("0.1"),
            condition_type="time_lock",
            condition_config={"release_at": "2026-04-02T12:00:00Z"},
            locked_amount=Decimal("0.1"),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        defaults.update(kwargs)
        return repo.create(**defaults)

    def test_conditional_payment_trigger(self, session, agent_ids):
        sender_id, recipient_id, _ = agent_ids
        repo = ConditionalPaymentRepository(session)
        payment = self._make_payment(repo, sender_id, recipient_id)

        rows = repo.trigger(payment.id)
        assert rows == 1
        session.expire_all()
        payment = repo.get_by_id(payment.id)
        assert payment.state == ConditionalPaymentState.TRIGGERED
        assert payment.triggered_at is not None

    def test_conditional_payment_execute(self, session, agent_ids):
        sender_id, recipient_id, _ = agent_ids
        repo = ConditionalPaymentRepository(session)
        payment = self._make_payment(repo, sender_id, recipient_id)

        repo.trigger(payment.id)
        rows = repo.execute(payment.id)
        assert rows == 1
        session.expire_all()
        payment = repo.get_by_id(payment.id)
        assert payment.state == ConditionalPaymentState.EXECUTED
        assert payment.executed_at is not None

    def test_conditional_payment_expire(self, session, agent_ids):
        sender_id, recipient_id, _ = agent_ids
        repo = ConditionalPaymentRepository(session)
        payment = self._make_payment(repo, sender_id, recipient_id)

        rows = repo.expire(payment.id)
        assert rows == 1
        session.expire_all()
        payment = repo.get_by_id(payment.id)
        assert payment.state == ConditionalPaymentState.EXPIRED

    def test_conditional_payment_cancel(self, session, agent_ids):
        sender_id, recipient_id, _ = agent_ids
        repo = ConditionalPaymentRepository(session)
        payment = self._make_payment(repo, sender_id, recipient_id)

        rows = repo.cancel(payment.id)
        assert rows == 1
        session.expire_all()
        payment = repo.get_by_id(payment.id)
        assert payment.state == ConditionalPaymentState.CANCELLED

    def test_conditional_payment_repo_list(self, session, agent_ids):
        sender_id, recipient_id, _ = agent_ids
        repo = ConditionalPaymentRepository(session)

        self._make_payment(repo, sender_id, recipient_id)
        self._make_payment(repo, sender_id, recipient_id)

        # List as sender
        items, total = repo.list_by_agent(sender_id, role="sender")
        assert total == 2

        # List as recipient
        items, total = repo.list_by_agent(recipient_id, role="recipient")
        assert total == 2

        # List all for sender
        items, total = repo.list_by_agent(sender_id)
        assert total == 2


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-PARTY PAYMENT MODEL + REPO TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestMultiPartyModels:
    def test_multi_party_create(self, session, agent_ids):
        sender_id, r1_id, r2_id = agent_ids
        payment = MultiPartyPayment(
            payment_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            sender_id=sender_id,
            total_amount=Decimal("1.0"),
            require_all_accept=True,
            state=MultiPartyPaymentState.PENDING,
            accept_deadline=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        session.add(payment)
        session.flush()

        r1 = MultiPartyRecipient(
            payment_id=payment.id, recipient_id=r1_id, amount=Decimal("0.6"),
        )
        r2 = MultiPartyRecipient(
            payment_id=payment.id, recipient_id=r2_id, amount=Decimal("0.4"),
        )
        session.add_all([r1, r2])
        session.flush()

        fetched = session.query(MultiPartyPayment).filter_by(id=payment.id).first()
        assert fetched is not None
        assert len(fetched.recipients) == 2
        assert fetched.state == MultiPartyPaymentState.PENDING


class TestMultiPartyRepo:
    def _make_payment(self, repo: MultiPartyRepository, sender_id, r1_id, r2_id):
        return repo.create(
            payment_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            sender_id=sender_id,
            total_amount=Decimal("1.0"),
            recipients=[
                {"recipient_id": r1_id, "amount": Decimal("0.6")},
                {"recipient_id": r2_id, "amount": Decimal("0.4")},
            ],
            accept_deadline=datetime.now(timezone.utc) + timedelta(hours=24),
        )

    def test_multi_party_accept_all(self, session, agent_ids):
        sender_id, r1_id, r2_id = agent_ids
        repo = MultiPartyRepository(session)
        payment = self._make_payment(repo, sender_id, r1_id, r2_id)

        # Both accept
        rows = repo.accept_recipient(payment.id, r1_id)
        assert rows == 1
        rows = repo.accept_recipient(payment.id, r2_id)
        assert rows == 1

        # Complete the payment
        rows = repo.complete(payment.id)
        assert rows == 1

        session.expire_all()
        payment = repo.get_by_id(payment.id)
        assert payment.state == MultiPartyPaymentState.COMPLETED
        assert payment.completed_at is not None

        # Verify recipients
        recipients = repo.get_recipients(payment.id)
        assert all(r.accepted is True for r in recipients)

    def test_multi_party_reject(self, session, agent_ids):
        sender_id, r1_id, r2_id = agent_ids
        repo = MultiPartyRepository(session)
        payment = self._make_payment(repo, sender_id, r1_id, r2_id)

        # One rejects
        rows = repo.reject_recipient(payment.id, r1_id)
        assert rows == 1

        # Reject the whole payment
        rows = repo.reject(payment.id)
        assert rows == 1

        session.expire_all()
        payment = repo.get_by_id(payment.id)
        assert payment.state == MultiPartyPaymentState.REJECTED

    def test_multi_party_expire(self, session, agent_ids):
        sender_id, r1_id, r2_id = agent_ids
        repo = MultiPartyRepository(session)
        payment = self._make_payment(repo, sender_id, r1_id, r2_id)

        rows = repo.expire(payment.id)
        assert rows == 1

        session.expire_all()
        payment = repo.get_by_id(payment.id)
        assert payment.state == MultiPartyPaymentState.EXPIRED

    def test_multi_party_repo_list(self, session, agent_ids):
        sender_id, r1_id, r2_id = agent_ids
        repo = MultiPartyRepository(session)

        self._make_payment(repo, sender_id, r1_id, r2_id)

        # List as sender
        items, total = repo.list_by_agent(sender_id, role="sender")
        assert total == 1

        # List as recipient
        items, total = repo.list_by_agent(r1_id, role="recipient")
        assert total == 1

        # List all for sender
        items, total = repo.list_by_agent(sender_id)
        assert total == 1
