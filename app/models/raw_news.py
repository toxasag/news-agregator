"""Raw ingested posts, before dedup/enrichment."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BIGINT, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import DedupStatus, pg_enum
from app.models.types import MutableJSONB, MutableJSONBList

if TYPE_CHECKING:
    from app.models.cluster import NewsCluster
    from app.models.source import Source


class RawNews(Base):
    """A single ingested post.

    `minhash_signature` stores the 128 (configurable) 32-bit MinHash permutation
    values used by the Redis LSH index, so signatures survive a Redis flush and
    can be rebuilt without re-fetching source content.
    """

    __tablename__ = "raw_news"
    __table_args__ = (
        # This unique index also covers `source_id` lookups and the FK cascade
        # check, so no separate index on `source_id` is declared: raw_news is the
        # highest-insert-rate table and a duplicate index is pure write cost.
        UniqueConstraint(
            "source_id", "external_post_id", name="uq_raw_news_source_external_post"
        ),
        # Partial index: workers only poll the unprocessed backlog, so indexing
        # processed history would grow without bound for no query benefit.
        Index(
            "ix_raw_news_unprocessed",
            "created_at",
            postgresql_where=text("is_processed = false"),
        ),
        Index("ix_raw_news_cluster_id", "cluster_id"),
        Index("ix_raw_news_entities_gin", "entities", postgresql_using="gin"),
        # Dashboard filters and dedup counters group by outcome, which is a
        # low-cardinality column the planner can only use with its own index.
        Index("ix_raw_news_dedup_status", "dedup_status"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    external_post_id: Mapped[str] = mapped_column(String(255), nullable=False)
    post_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    clean_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONB, nullable=False, default=dict, server_default="{}"
    )
    media_data: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableJSONBList, nullable=False, default=list, server_default="[]"
    )
    minhash_signature: Mapped[list[int] | None] = mapped_column(
        ARRAY(BIGINT), nullable=True
    )
    is_digest: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_ad: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    dedup_status: Mapped[DedupStatus] = mapped_column(
        pg_enum(DedupStatus, "dedup_status"),
        nullable=False,
        default=DedupStatus.PENDING,
        server_default=DedupStatus.PENDING.value,
    )
    filter_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default="{}"
    )
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    dedup_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("news_clusters.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    source: Mapped[Source] = relationship(back_populates="raw_news", lazy="selectin")
    cluster: Mapped[NewsCluster | None] = relationship(
        back_populates="raw_news", lazy="raise"
    )


__all__ = ["RawNews"]
