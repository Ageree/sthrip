"""Tests for ReviewRepository — Agent Reviews & Ratings.

TDD: Tests written BEFORE implementation. Run these to confirm RED state,
then implement to reach GREEN.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sthrip.db.models import (
    Base,
    Agent,
    AgentReputation,
    AgentBalance,
    AgentReview,
    AgentRatingSummary,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_session():
    """In-memory SQLite session with review tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [
        Agent.__table__,
        AgentReputation.__table__,
        AgentBalance.__table__,
        AgentReview.__table__,
        AgentRatingSummary.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(db_session, name: str) -> Agent:
    """Create a test agent with balance and reputation."""
    agent = Agent(
        id=uuid.uuid4(),
        agent_name=name,
        api_key_hash="hash_" + name,
        is_active=True,
    )
    db_session.add(agent)
    db_session.flush()
    rep = AgentReputation(agent_id=agent.id)
    bal = AgentBalance(agent_id=agent.id, available=Decimal("100"))
    db_session.add_all([rep, bal])
    db_session.flush()
    return agent


# ---------------------------------------------------------------------------
# Unit tests: ReviewRepository
# ---------------------------------------------------------------------------

class TestReviewRepositoryCreate:
    """Tests for creating reviews."""

    def test_create_review(self, db_session):
        """Creates a review and asserts all fields are persisted correctly."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "reviewer-1")
        reviewed = _make_agent(db_session, "reviewed-1")
        transaction_id = uuid.uuid4()

        repo = ReviewRepository(db_session)
        review = repo.create(
            reviewer_id=reviewer.id,
            reviewed_id=reviewed.id,
            transaction_id=transaction_id,
            transaction_type="escrow",
            overall_rating=5,
            speed_rating=4,
            quality_rating=5,
            reliability_rating=4,
            comment_encrypted="enc:hello",
        )

        assert review.id is not None
        assert review.reviewer_id == reviewer.id
        assert review.reviewed_id == reviewed.id
        assert review.transaction_id == transaction_id
        assert review.transaction_type == "escrow"
        assert review.overall_rating == 5
        assert review.speed_rating == 4
        assert review.quality_rating == 5
        assert review.reliability_rating == 4
        assert review.comment_encrypted == "enc:hello"
        assert review.is_verified is True
        assert review.created_at is not None

    def test_create_review_optional_fields_null(self, db_session):
        """Creates a review with only required fields; optional fields are null."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "reviewer-2")
        reviewed = _make_agent(db_session, "reviewed-2")
        transaction_id = uuid.uuid4()

        repo = ReviewRepository(db_session)
        review = repo.create(
            reviewer_id=reviewer.id,
            reviewed_id=reviewed.id,
            transaction_id=transaction_id,
            transaction_type="payment",
            overall_rating=3,
        )

        assert review.speed_rating is None
        assert review.quality_rating is None
        assert review.reliability_rating is None
        assert review.comment_encrypted is None

    def test_create_review_payment_type(self, db_session):
        """Creates a review with transaction_type 'payment'."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "reviewer-pay")
        reviewed = _make_agent(db_session, "reviewed-pay")

        repo = ReviewRepository(db_session)
        review = repo.create(
            reviewer_id=reviewer.id,
            reviewed_id=reviewed.id,
            transaction_id=uuid.uuid4(),
            transaction_type="payment",
            overall_rating=4,
        )

        assert review.transaction_type == "payment"

    def test_create_review_sla_type(self, db_session):
        """Creates a review with transaction_type 'sla'."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "reviewer-sla")
        reviewed = _make_agent(db_session, "reviewed-sla")

        repo = ReviewRepository(db_session)
        review = repo.create(
            reviewer_id=reviewer.id,
            reviewed_id=reviewed.id,
            transaction_id=uuid.uuid4(),
            transaction_type="sla",
            overall_rating=2,
        )

        assert review.transaction_type == "sla"


class TestReviewRepositoryDuplicateRejection:
    """Tests for unique constraint enforcement."""

    def test_duplicate_review_rejected(self, db_session):
        """Same reviewer + transaction_id raises IntegrityError."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "dup-reviewer")
        reviewed_a = _make_agent(db_session, "dup-reviewed-a")
        reviewed_b = _make_agent(db_session, "dup-reviewed-b")
        transaction_id = uuid.uuid4()

        repo = ReviewRepository(db_session)
        repo.create(
            reviewer_id=reviewer.id,
            reviewed_id=reviewed_a.id,
            transaction_id=transaction_id,
            transaction_type="escrow",
            overall_rating=5,
        )
        db_session.flush()

        with pytest.raises(IntegrityError):
            # Same reviewer + same transaction_id — should violate unique constraint
            repo.create(
                reviewer_id=reviewer.id,
                reviewed_id=reviewed_b.id,
                transaction_id=transaction_id,
                transaction_type="escrow",
                overall_rating=3,
            )
            db_session.flush()

    def test_different_reviewer_same_transaction_allowed(self, db_session):
        """Different reviewers CAN review the same transaction."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer_a = _make_agent(db_session, "diff-rev-a")
        reviewer_b = _make_agent(db_session, "diff-rev-b")
        reviewed = _make_agent(db_session, "diff-reviewed")
        transaction_id = uuid.uuid4()

        repo = ReviewRepository(db_session)
        r1 = repo.create(
            reviewer_id=reviewer_a.id,
            reviewed_id=reviewed.id,
            transaction_id=transaction_id,
            transaction_type="payment",
            overall_rating=5,
        )
        r2 = repo.create(
            reviewer_id=reviewer_b.id,
            reviewed_id=reviewed.id,
            transaction_id=transaction_id,
            transaction_type="payment",
            overall_rating=4,
        )
        db_session.flush()

        assert r1.id != r2.id


class TestReviewRepositoryFetch:
    """Tests for retrieval methods."""

    def test_get_by_id(self, db_session):
        """Retrieves a review by primary key."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "get-reviewer")
        reviewed = _make_agent(db_session, "get-reviewed")

        repo = ReviewRepository(db_session)
        review = repo.create(
            reviewer_id=reviewer.id,
            reviewed_id=reviewed.id,
            transaction_id=uuid.uuid4(),
            transaction_type="escrow",
            overall_rating=4,
        )

        found = repo.get_by_id(review.id)
        assert found is not None
        assert found.id == review.id

    def test_get_by_id_missing_returns_none(self, db_session):
        """Returns None for a non-existent review ID."""
        from sthrip.db.review_repo import ReviewRepository

        repo = ReviewRepository(db_session)
        result = repo.get_by_id(uuid.uuid4())
        assert result is None

    def test_get_by_transaction(self, db_session):
        """Finds review by reviewer_id + transaction_id."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "tx-reviewer")
        reviewed = _make_agent(db_session, "tx-reviewed")
        transaction_id = uuid.uuid4()

        repo = ReviewRepository(db_session)
        created = repo.create(
            reviewer_id=reviewer.id,
            reviewed_id=reviewed.id,
            transaction_id=transaction_id,
            transaction_type="payment",
            overall_rating=3,
        )

        found = repo.get_by_transaction(reviewer.id, transaction_id)
        assert found is not None
        assert found.id == created.id

    def test_get_by_transaction_missing_returns_none(self, db_session):
        """Returns None when no review matches reviewer+transaction."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "miss-reviewer")
        repo = ReviewRepository(db_session)

        result = repo.get_by_transaction(reviewer.id, uuid.uuid4())
        assert result is None

    def test_list_by_reviewed(self, db_session):
        """Lists reviews for a specific reviewed agent."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer_a = _make_agent(db_session, "list-rev-a")
        reviewer_b = _make_agent(db_session, "list-rev-b")
        reviewed = _make_agent(db_session, "list-reviewed")
        other = _make_agent(db_session, "list-other")

        repo = ReviewRepository(db_session)
        repo.create(reviewer_a.id, reviewed.id, uuid.uuid4(), "payment", 5)
        repo.create(reviewer_b.id, reviewed.id, uuid.uuid4(), "escrow", 4)
        # Review for a different agent — should not appear in results
        repo.create(reviewer_a.id, other.id, uuid.uuid4(), "payment", 3)

        items, total = repo.list_by_reviewed(reviewed.id)

        assert total == 2
        assert len(items) == 2
        assert all(r.reviewed_id == reviewed.id for r in items)

    def test_list_by_reviewed_empty(self, db_session):
        """Returns empty list and zero count when agent has no reviews."""
        from sthrip.db.review_repo import ReviewRepository

        agent = _make_agent(db_session, "no-reviews")
        repo = ReviewRepository(db_session)

        items, total = repo.list_by_reviewed(agent.id)
        assert total == 0
        assert items == []

    def test_list_by_reviewed_pagination(self, db_session):
        """Pagination with limit/offset works correctly."""
        from sthrip.db.review_repo import ReviewRepository

        reviewed = _make_agent(db_session, "page-reviewed")
        repo = ReviewRepository(db_session)

        # Create 5 reviews from different reviewers
        for i in range(5):
            reviewer = _make_agent(db_session, f"page-reviewer-{i}")
            repo.create(reviewer.id, reviewed.id, uuid.uuid4(), "payment", (i % 5) + 1)

        page1, total = repo.list_by_reviewed(reviewed.id, limit=3, offset=0)
        page2, _ = repo.list_by_reviewed(reviewed.id, limit=3, offset=3)

        assert total == 5
        assert len(page1) == 3
        assert len(page2) == 2
        # Pages must not overlap
        ids1 = {r.id for r in page1}
        ids2 = {r.id for r in page2}
        assert ids1.isdisjoint(ids2)


class TestReviewRepositoryRatingSummary:
    """Tests for rating summary upsert and retrieval."""

    def test_update_rating_summary_averages(self, db_session):
        """After creating reviews, summary averages are calculated correctly."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer_a = _make_agent(db_session, "sum-rev-a")
        reviewer_b = _make_agent(db_session, "sum-rev-b")
        reviewed = _make_agent(db_session, "sum-reviewed")

        repo = ReviewRepository(db_session)
        repo.create(reviewer_a.id, reviewed.id, uuid.uuid4(), "payment", 5,
                    speed_rating=4, quality_rating=5, reliability_rating=5)
        repo.create(reviewer_b.id, reviewed.id, uuid.uuid4(), "escrow", 3,
                    speed_rating=2, quality_rating=3, reliability_rating=4)

        summary = repo.update_rating_summary(reviewed.id)

        assert summary.agent_id == reviewed.id
        assert summary.total_reviews == 2
        # avg_overall = (5 + 3) / 2 = 4.00
        assert float(summary.avg_overall) == pytest.approx(4.0, abs=0.01)
        # avg_speed = (4 + 2) / 2 = 3.00
        assert float(summary.avg_speed) == pytest.approx(3.0, abs=0.01)
        # avg_quality = (5 + 3) / 2 = 4.00
        assert float(summary.avg_quality) == pytest.approx(4.0, abs=0.01)
        # avg_reliability = (5 + 4) / 2 = 4.50
        assert float(summary.avg_reliability) == pytest.approx(4.5, abs=0.01)

    def test_update_rating_summary_star_counts(self, db_session):
        """Five-star and one-star counts are tracked separately."""
        from sthrip.db.review_repo import ReviewRepository

        reviewed = _make_agent(db_session, "star-reviewed")
        repo = ReviewRepository(db_session)

        for overall in [5, 5, 1, 3, 5, 1]:
            reviewer = _make_agent(db_session, f"star-rev-{uuid.uuid4().hex[:6]}")
            repo.create(reviewer.id, reviewed.id, uuid.uuid4(), "payment", overall)

        summary = repo.update_rating_summary(reviewed.id)

        assert summary.five_star_count == 3
        assert summary.one_star_count == 2

    def test_update_rating_summary_null_sub_ratings_excluded(self, db_session):
        """Null speed/quality/reliability ratings are excluded from averages."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer_a = _make_agent(db_session, "null-rev-a")
        reviewer_b = _make_agent(db_session, "null-rev-b")
        reviewed = _make_agent(db_session, "null-reviewed")

        repo = ReviewRepository(db_session)
        # Only reviewer_a provides speed_rating
        repo.create(reviewer_a.id, reviewed.id, uuid.uuid4(), "payment", 4,
                    speed_rating=4)
        repo.create(reviewer_b.id, reviewed.id, uuid.uuid4(), "payment", 2)

        summary = repo.update_rating_summary(reviewed.id)

        # avg_speed should only count reviewer_a's rating (4/1 = 4.0)
        assert float(summary.avg_speed) == pytest.approx(4.0, abs=0.01)
        # avg_quality and avg_reliability have no data — stored as 0
        assert float(summary.avg_quality) == pytest.approx(0.0, abs=0.01)
        assert float(summary.avg_reliability) == pytest.approx(0.0, abs=0.01)

    def test_update_rating_summary_upsert(self, db_session):
        """Calling update_rating_summary twice updates instead of inserting a duplicate."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer_a = _make_agent(db_session, "ups-rev-a")
        reviewer_b = _make_agent(db_session, "ups-rev-b")
        reviewed = _make_agent(db_session, "ups-reviewed")

        repo = ReviewRepository(db_session)
        repo.create(reviewer_a.id, reviewed.id, uuid.uuid4(), "payment", 5)
        repo.update_rating_summary(reviewed.id)

        # Add a second review and re-calculate
        repo.create(reviewer_b.id, reviewed.id, uuid.uuid4(), "escrow", 1)
        summary = repo.update_rating_summary(reviewed.id)

        # There must still be only one summary row for this agent
        assert summary.total_reviews == 2
        assert float(summary.avg_overall) == pytest.approx(3.0, abs=0.01)

    def test_update_rating_summary_last_review_at(self, db_session):
        """last_review_at is set to the most recent review's created_at."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "lat-reviewer")
        reviewed = _make_agent(db_session, "lat-reviewed")

        repo = ReviewRepository(db_session)
        repo.create(reviewer.id, reviewed.id, uuid.uuid4(), "payment", 4)

        summary = repo.update_rating_summary(reviewed.id)
        assert summary.last_review_at is not None

    def test_get_rating_summary(self, db_session):
        """Retrieves the persisted summary for an agent."""
        from sthrip.db.review_repo import ReviewRepository

        reviewer = _make_agent(db_session, "get-sum-reviewer")
        reviewed = _make_agent(db_session, "get-sum-reviewed")

        repo = ReviewRepository(db_session)
        repo.create(reviewer.id, reviewed.id, uuid.uuid4(), "payment", 5)
        repo.update_rating_summary(reviewed.id)

        summary = repo.get_rating_summary(reviewed.id)
        assert summary is not None
        assert summary.agent_id == reviewed.id
        assert summary.total_reviews == 1

    def test_get_rating_summary_missing_returns_none(self, db_session):
        """Returns None when no summary exists for an agent."""
        from sthrip.db.review_repo import ReviewRepository

        agent = _make_agent(db_session, "no-sum-agent")
        repo = ReviewRepository(db_session)

        result = repo.get_rating_summary(agent.id)
        assert result is None

    def test_update_rating_summary_no_reviews(self, db_session):
        """Summary for agent with no reviews has zero counts and averages."""
        from sthrip.db.review_repo import ReviewRepository

        agent = _make_agent(db_session, "empty-sum-agent")
        repo = ReviewRepository(db_session)

        summary = repo.update_rating_summary(agent.id)

        assert summary.total_reviews == 0
        assert float(summary.avg_overall) == pytest.approx(0.0, abs=0.01)
        assert summary.five_star_count == 0
        assert summary.one_star_count == 0
