"""
ReviewRepository — data-access layer for AgentReview and AgentRatingSummary.
"""

from decimal import Decimal
from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from . import models
from ._repo_base import _MAX_QUERY_LIMIT


class ReviewRepository:
    """Agent review and rating summary data access."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        reviewer_id: UUID,
        reviewed_id: UUID,
        transaction_id: UUID,
        transaction_type: str,
        overall_rating: int,
        speed_rating: Optional[int] = None,
        quality_rating: Optional[int] = None,
        reliability_rating: Optional[int] = None,
        comment_encrypted: Optional[str] = None,
    ) -> models.AgentReview:
        """Persist a new review and return it.

        Raises IntegrityError when the same reviewer submits more than one
        review for the same transaction (uq_review_per_transaction).
        """
        review = models.AgentReview(
            reviewer_id=reviewer_id,
            reviewed_id=reviewed_id,
            transaction_id=transaction_id,
            transaction_type=transaction_type,
            overall_rating=overall_rating,
            speed_rating=speed_rating,
            quality_rating=quality_rating,
            reliability_rating=reliability_rating,
            comment_encrypted=comment_encrypted,
        )
        self.db.add(review)
        self.db.flush()
        return review

    def get_by_id(self, review_id: UUID) -> Optional[models.AgentReview]:
        """Return review by primary key, or None."""
        return (
            self.db.query(models.AgentReview)
            .filter(models.AgentReview.id == review_id)
            .first()
        )

    def get_by_transaction(
        self,
        reviewer_id: UUID,
        transaction_id: UUID,
    ) -> Optional[models.AgentReview]:
        """Return the review left by reviewer_id for transaction_id, or None."""
        return (
            self.db.query(models.AgentReview)
            .filter(
                models.AgentReview.reviewer_id == reviewer_id,
                models.AgentReview.transaction_id == transaction_id,
            )
            .first()
        )

    def list_by_reviewed(
        self,
        reviewed_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> Tuple[List[models.AgentReview], int]:
        """List reviews received by an agent, newest first.

        Returns (items, total_count).
        """
        limit = min(limit, _MAX_QUERY_LIMIT)
        query = self.db.query(models.AgentReview).filter(
            models.AgentReview.reviewed_id == reviewed_id
        )
        total = query.count()
        items = (
            query.order_by(desc(models.AgentReview.created_at))
            .offset(offset)
            .limit(limit)
            .all()
        )
        return items, total

    def update_rating_summary(
        self,
        reviewed_id: UUID,
    ) -> models.AgentRatingSummary:
        """Recompute and upsert the AgentRatingSummary for reviewed_id.

        Sub-ratings (speed, quality, reliability) only average over non-null
        values; if no reviews provide a given sub-rating the stored average is 0.
        Returns the updated summary row.
        """
        reviews: List[models.AgentReview] = (
            self.db.query(models.AgentReview)
            .filter(models.AgentReview.reviewed_id == reviewed_id)
            .all()
        )

        total_reviews = len(reviews)

        if total_reviews == 0:
            avg_overall = Decimal("0")
            avg_speed = Decimal("0")
            avg_quality = Decimal("0")
            avg_reliability = Decimal("0")
            five_star_count = 0
            one_star_count = 0
            last_review_at = None
        else:
            avg_overall = Decimal(str(sum(r.overall_rating for r in reviews) / total_reviews))

            speed_vals = [r.speed_rating for r in reviews if r.speed_rating is not None]
            avg_speed = (
                Decimal(str(sum(speed_vals) / len(speed_vals)))
                if speed_vals
                else Decimal("0")
            )

            quality_vals = [r.quality_rating for r in reviews if r.quality_rating is not None]
            avg_quality = (
                Decimal(str(sum(quality_vals) / len(quality_vals)))
                if quality_vals
                else Decimal("0")
            )

            reliability_vals = [r.reliability_rating for r in reviews if r.reliability_rating is not None]
            avg_reliability = (
                Decimal(str(sum(reliability_vals) / len(reliability_vals)))
                if reliability_vals
                else Decimal("0")
            )

            five_star_count = sum(1 for r in reviews if r.overall_rating == 5)
            one_star_count = sum(1 for r in reviews if r.overall_rating == 1)

            last_review_at = max(r.created_at for r in reviews if r.created_at is not None)

        # Upsert: fetch existing row or create new one
        summary = (
            self.db.query(models.AgentRatingSummary)
            .filter(models.AgentRatingSummary.agent_id == reviewed_id)
            .first()
        )
        if summary is None:
            summary = models.AgentRatingSummary(agent_id=reviewed_id)
            self.db.add(summary)

        summary.total_reviews = total_reviews
        summary.avg_overall = avg_overall
        summary.avg_speed = avg_speed
        summary.avg_quality = avg_quality
        summary.avg_reliability = avg_reliability
        summary.five_star_count = five_star_count
        summary.one_star_count = one_star_count
        summary.last_review_at = last_review_at

        self.db.flush()
        return summary

    def get_rating_summary(
        self,
        agent_id: UUID,
    ) -> Optional[models.AgentRatingSummary]:
        """Return the persisted rating summary for agent_id, or None."""
        return (
            self.db.query(models.AgentRatingSummary)
            .filter(models.AgentRatingSummary.agent_id == agent_id)
            .first()
        )
