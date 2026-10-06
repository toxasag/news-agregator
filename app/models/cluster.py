"""News clusters: one row per distinct news event."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import RiskLevel, pg_enum
from app.models.types import MutableJSONB
from app.models.vector import embedding_column, hnsw_index_kwargs

if TYPE_CHECKING:
    from app.models.article import Article
    from app.models.raw_news import RawNews


class NewsCluster(Base):
    """A deduplicated news event aggregating one or more `raw_news` rows.

    `embedding` is the vector(1536) representation used for second-stage
    semantic dedup after the MinHash LSH prefilter. `enrichment_data` holds the
    Playwright web-research payload (top-3 primary sources, extracted facts,
    quotes and timestamps).
    """

    __tablename__ = "news_clusters"
    __table_args__ = (
        # Partial index: the publishing worker only ever scans the unpublished
        # backlog, so indexing published history would grow without bound.
        Index(
            "ix_news_clusters_unpublished",
            "created_at",
            postgresql_where=text("is_published = false"),
        ),
        Index("ix_news_clusters_category", "category"),
        Index(
            "ix_news_clusters_embedding_hnsw",
            "embedding",
            **hnsw_index_kwargs("embedding"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
        default=uuid.uuid4,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[RiskLevel] = mapped_column(
        pg_enum(RiskLevel, "risk_level"),
        nullable=False,
        # GREEN is the default: a cluster is publishable until a moderator says
        # otherwise, and defaulting to a blocking value would stall the pipeline
        # whenever moderation is unavailable.
        default=RiskLevel.GREEN,
        server_default=RiskLevel.GREEN.value,
    )
    enrichment_data: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONB, nullable=False, default=dict, server_default="{}"
    )
    embedding: Mapped[list[float] | None] = embedding_column()
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    raw_news: Mapped[list[RawNews]] = relationship(
        back_populates="cluster", lazy="raise", passive_deletes=True
    )
    articles: Mapped[list[Article]] = relationship(
        back_populates="cluster",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


__all__ = ["NewsCluster"]
