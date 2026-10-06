"""Local mirror of WordPress tag IDs to avoid redundant REST lookups."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WPTagCache(Base):
    """Maps a normalized tag name to its WordPress term ID.

    The functional unique index on `lower(tag_name)` makes tag resolution
    case-insensitive and idempotent: a concurrent worker that loses the race
    fails the insert instead of creating a duplicate WordPress term.
    """

    __tablename__ = "wp_tags_cache"
    __table_args__ = (
        Index(
            "uq_wp_tags_cache_tag_name_lower",
            text("lower(tag_name)"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tag_name: Mapped[str] = mapped_column(String(255), nullable=False)
    wp_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["WPTagCache"]
