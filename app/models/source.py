"""Ingestion sources: Telegram channels and scraped websites."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.enums import MediaStrategy, SourceType, pg_enum
from app.models.types import MutableJSONB

if TYPE_CHECKING:
    from app.models.raw_news import RawNews


class Source(Base):
    """A monitored feed.

    `identifier` is the channel username / numeric id for Telegram sources and
    the absolute feed or listing URL for website sources. `crawl_rules` holds
    the per-source Playwright selectors and pagination hints.
    """

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("source_type", "identifier", name="uq_sources_type_identifier"),
        Index("ix_sources_active_type", "is_active", "source_type"),
        Index("ix_sources_last_scraped_at", "last_scraped_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_type: Mapped[SourceType] = mapped_column(
        pg_enum(SourceType, "source_type"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    identifier: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    skip_media: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: Whether media may be downloaded for this source. The positive form of the
    #: operator's intent, shown as the media-collection checkbox on /sources.
    #:
    #: Deliberately separate from ``skip_media`` rather than replacing it:
    #: ``skip_media`` means "this channel's own photos are unusable, generate a
    #: cover instead", while ``fetch_media`` means "do not download anything at
    #: all". Collapsing the two would lose the distinction between a channel with
    #: watermarked images and one whose media is simply not wanted.
    fetch_media: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: Telegram's numeric peer id, captured at import so a channel that changes
    #: its username can still be matched.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Cached avatar as a data URI. Stored rather than proxied so the sources table
    #: renders without one Telegram round-trip per row.
    avatar_data_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Dialog kind for Telegram sources (channel, group, bot, …). ``None`` on
    #: website rows. A string rather than a native enum: see
    #: :class:`~app.models.enums.TelegramPeerKind`.
    peer_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Public username without ``@``. Empty for private dialogs.
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Channel/group/bot about-text, shown so the operator can tell similarly
    #: named dialogs apart without opening Telegram.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Clickable URL for a manual check (``https://t.me/…`` or ``tg://user?id=``).
    #: Empty when Telegram does not expose a public handle and we refuse to
    #: mint an invite link (that would be an admin-only side effect).
    invite_link: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Running count of posts stored from this source, shown in the sources table.
    #: Maintained by the worker so the table needs no per-row aggregate query.
    posts_collected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    media_strategy: Mapped[MediaStrategy] = mapped_column(
        pg_enum(MediaStrategy, "media_strategy"),
        nullable=False,
        default=MediaStrategy.OCR_CHECK,
        server_default=MediaStrategy.OCR_CHECK.value,
    )
    crawl_rules: Mapped[dict[str, Any]] = mapped_column(
        MutableJSONB, nullable=False, default=dict, server_default="{}"
    )
    last_scraped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    raw_news: Mapped[list[RawNews]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )


__all__ = ["Source"]
