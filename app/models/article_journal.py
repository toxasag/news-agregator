"""Persistent index of the artifact runs that produced an article."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.article import Article


class ArticleJournal(Base):
    """A durable, small index pointing an article to its filesystem evidence.

    The detailed prompt and stage payloads remain in the artifact directory. This
    table keeps the relationship queryable after a process restart without making
    normal article reads carry large JSON documents.
    """

    __tablename__ = "article_journals"
    __table_args__ = (
        Index("ix_article_journals_article_started", "article_id", "started_at"),
        Index("ix_article_journals_cluster_started", "cluster_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("articles.id", ondelete="CASCADE"),
        nullable=False,
    )
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    run_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    artifact_path: Mapped[str] = mapped_column(String(512), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    article: Mapped[Article] = relationship(back_populates="journals", lazy="raise")


__all__ = ["ArticleJournal"]
