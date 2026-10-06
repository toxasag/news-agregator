"""Data model for tracking SEO snippet and content optimizations and learning outcomes."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import BIGINT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.types import MutableJSONB

if TYPE_CHECKING:
    from app.models.article import Article


class SeoOptimization(Base):
    """Tracks a snippet or on-page SEO modification, recording before/after performance.

    Acts as the feedback loop for the self-learning SEO engine:
    1. Records snapshot before change (position, CTR, impressions, clicks).
    2. Records the applied change (title, description, model, prompt hypothesis).
    3. Evaluates 7-day, 14-day, 28-day performance to classify outcome (win/loss).
    4. Extracts high-performing patterns to inject into future generation prompts.
    """

    __tablename__ = "seo_optimizations"
    __table_args__ = (
        Index("ix_seo_opt_url_applied", "url", "applied_at"),
        Index("ix_seo_opt_category_outcome", "category", "outcome"),
        Index("ix_seo_opt_status_applied", "status", "applied_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    article_id: Mapped[int | None] = mapped_column(
        BIGINT,
        ForeignKey("articles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    url: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    category: Mapped[str] = mapped_column(
        String(128), nullable=False, default="general", server_default="general"
    )
    focus_keyword: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    strategy: Mapped[str] = mapped_column(
        String(64), nullable=False, default="ctr_booster", server_default="ctr_booster"
    )

    # Before snapshot (14-day window prior to change)
    before_title: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    before_description: Mapped[str] = mapped_column(
        String(1024), nullable=False, default="", server_default=""
    )
    before_position: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0"
    )
    before_ctr: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0"
    )
    before_impressions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    before_clicks: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    # Applied change
    after_title: Mapped[str] = mapped_column(
        String(512), nullable=False, default="", server_default=""
    )
    after_description: Mapped[str] = mapped_column(
        String(1024), nullable=False, default="", server_default=""
    )
    after_focus_keyword: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=""
    )
    model_used: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=""
    )
    reasoning_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    wp_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    applied_by: Mapped[str] = mapped_column(
        String(128), nullable=False, default="admin", server_default="admin"
    )

    # Performance evaluation
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="applied",
        server_default="applied",
        index=True,
    )
    outcome: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="pending",
        server_default="pending",
        index=True,
    )
    metrics_7d: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONB, nullable=False, default=dict, server_default="{}"
    )
    metrics_14d: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONB, nullable=False, default=dict, server_default="{}"
    )
    metrics_28d: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONB, nullable=False, default=dict, server_default="{}"
    )
    ctr_lift: Mapped[float | None] = mapped_column(Float, nullable=True)
    clicks_lift: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    learned_insight: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    article: Mapped[Article | None] = relationship(lazy="selectin")

    def to_dict(self) -> dict[str, Any]:
        """Serialize for dashboard templates and API responses."""
        return {
            "id": self.id,
            "article_id": self.article_id,
            "url": self.url,
            "category": self.category,
            "focus_keyword": self.focus_keyword,
            "strategy": self.strategy,
            "before_title": self.before_title,
            "before_description": self.before_description,
            "before_position": round(self.before_position, 1),
            "before_ctr": round(self.before_ctr, 4),
            "before_impressions": self.before_impressions,
            "before_clicks": self.before_clicks,
            "after_title": self.after_title,
            "after_description": self.after_description,
            "after_focus_keyword": self.after_focus_keyword,
            "model_used": self.model_used,
            "wp_post_id": self.wp_post_id,
            "applied_at": self.applied_at.isoformat() if self.applied_at else "",
            "applied_by": self.applied_by,
            "status": self.status,
            "outcome": self.outcome,
            "metrics_7d": self.metrics_7d,
            "metrics_14d": self.metrics_14d,
            "metrics_28d": self.metrics_28d,
            "ctr_lift": round(self.ctr_lift, 4) if self.ctr_lift is not None else None,
            "clicks_lift": self.clicks_lift,
            "position_delta": (
                round(self.position_delta, 1) if self.position_delta is not None else None
            ),
            "learned_insight": self.learned_insight or "",
        }


__all__ = ["SeoOptimization"]
