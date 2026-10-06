"""Ledger of paid model calls: what was asked, from which model, at what cost.

Rows are written after every chat completion that reports a ``usage`` block.
Deliberately free of foreign keys: the spend it records stays true even when
the cluster or article it served is later deleted by a queue reset.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AIUsage(Base):
    """One paid model call."""

    __tablename__ = "ai_usage"
    __table_args__ = (
        Index("ix_ai_usage_created_at", "created_at"),
        Index("ix_ai_usage_cluster", "cluster_id"),
        Index("ix_ai_usage_provider", "provider_name"),
        Index("ix_ai_usage_article", "article_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, autoincrement=True, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Pipeline role that issued the call: ``arbiter``, ``writer``,
    #: ``seo``, ``image_prompt``.
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Gateway-reported cost in USD; NULL when the gateway did not report one.
    cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="success"
    )
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(Uuid, nullable=True)
    article_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)



__all__ = ["AIUsage"]
