"""Data model for tracking audience preferences by platform and category.

Stores traffic distribution, engagement (time on page, bounce rate),
top interest topics, and calculated ad conversion potential for both
the WordPress website and Yandex Dzen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.types import MutableJSONBList


class AudienceCategoryMetric(Base):
    """Snapshot of audience reading behavior and ad potential per category/platform."""

    __tablename__ = "audience_category_metrics"
    __table_args__ = (
        Index("ix_aud_metric_platform_slug", "platform", "category_slug"),
        Index("ix_aud_metric_snapshot", "snapshot_date"),
        Index("ix_aud_metric_ad_score", "ad_potential_score"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(
        String(32), nullable=False, default="website", server_default="website"
    )
    """Platform: 'website', 'dzen', or 'all'."""

    category_slug: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    category_title: Mapped[str] = mapped_column(String(255), nullable=False)

    pageviews: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    visitors: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    avg_time_on_page: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0"
    )
    bounce_rate: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0"
    )
    audience_share: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0.0"
    )

    top_keywords: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableJSONBList, nullable=False, default=list, server_default="[]"
    )
    """Top keywords/topics driving audience to this category."""

    ad_potential_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=50.0, server_default="50.0"
    )
    """Calculated ad conversion index (0.0 to 100.0)."""

    ad_readiness_level: Mapped[str] = mapped_column(
        String(32), nullable=False, default="medium", server_default="medium"
    )
    """'high', 'medium', or 'low'."""

    ad_recommendation: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    """AI and algorithmic recommendation on best ad format & topics for this rubric."""

    window_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    snapshot_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary for dashboard templates."""
        return {
            "id": self.id,
            "platform": self.platform,
            "category_slug": self.category_slug,
            "category_title": self.category_title,
            "pageviews": self.pageviews,
            "visitors": self.visitors,
            "avg_time_on_page": round(self.avg_time_on_page, 1),
            "avg_time_minutes": round(self.avg_time_on_page / 60.0, 1),
            "bounce_rate": round(self.bounce_rate, 3),
            "bounce_rate_pct": round(self.bounce_rate * 100, 1),
            "audience_share": round(self.audience_share, 1),
            "top_keywords": self.top_keywords or [],
            "ad_potential_score": round(self.ad_potential_score, 1),
            "ad_readiness_level": self.ad_readiness_level,
            "ad_recommendation": self.ad_recommendation,
            "window_days": self.window_days,
            "snapshot_date": self.snapshot_date.isoformat() if self.snapshot_date else "",
        }
