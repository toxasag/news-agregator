"""Generated articles and their per-platform publication state."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, BIGINT, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import (
    DEFAULT_PLATFORM_STATUSES_JSON,
    ArticleStatus,
    default_platform_statuses,
    pg_enum,
)
from app.models.types import MutableJSONB, MutableJSONBList
from app.models.vector import embedding_column, hnsw_index_kwargs

if TYPE_CHECKING:
    from app.models.article_journal import ArticleJournal
    from app.models.cluster import NewsCluster


class Article(Base):
    """The canonical longread produced for a news cluster.

    Fields mirror docs/02_DATABASE_SPEC.md. `platform_statuses` is a JSONB map
    keyed by platform code (`tg`, `vk`, `ok`, `dzen`) storing status, external
    id, url, attempt counter and last error, which keeps redistribution
    idempotent across retries.
    """

    __tablename__ = "articles"
    __table_args__ = (
        # Leading column `status` also serves single-column status lookups, so
        # no separate index on `status` is declared: articles are updated on
        # every pipeline transition and a duplicate index is pure write cost.
        Index("ix_articles_status_created_at", "status", "created_at"),
        Index(
            "ix_articles_platform_statuses_gin",
            "platform_statuses",
            postgresql_using="gin",
        ),
        Index(
            "ix_articles_final_embedding_hnsw",
            "final_embedding",
            **hnsw_index_kwargs("final_embedding"),
        ),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    cluster_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("news_clusters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    category: Mapped[str] = mapped_column(String(128), nullable=False)
    wp_category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    content_html: Mapped[str] = mapped_column(Text, nullable=False)

    seo_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    seo_description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    seo_keywords: Mapped[list[str]] = mapped_column(
        ARRAY(String(128)), nullable=False, default=list, server_default="{}"
    )

    wp_tag_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list, server_default="{}"
    )
    url_slug: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )

    wp_post_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    wp_image_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wp_image_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list, server_default="{}"
    )
    video_links: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableJSONBList, nullable=False, default=list, server_default="[]"
    )

    final_embedding: Mapped[list[float] | None] = embedding_column()

    status: Mapped[ArticleStatus] = mapped_column(
        pg_enum(ArticleStatus, "article_status"),
        nullable=False,
        default=ArticleStatus.DRAFT,
        server_default=ArticleStatus.DRAFT.value,
    )
    platform_statuses: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONB,
        nullable=False,
        default=default_platform_statuses,
        server_default=text(f"'{DEFAULT_PLATFORM_STATUSES_JSON}'::jsonb"),
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    cluster: Mapped[NewsCluster] = relationship(
        back_populates="articles", lazy="raise"
    )
    journals: Mapped[list[ArticleJournal]] = relationship(
        back_populates="article",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


__all__ = ["Article"]
