"""Telegram accounts used for MTProto ingestion.

Ingestion runs under real user accounts rather than a bot, because a bot cannot
read arbitrary public channels it has not been added to. Telegram rate-limits per
account, so several accounts are pooled and rotated: one account hitting a
``FloodWait`` must not stop the whole pipeline.

The session string is the sensitive part. It is a bearer credential — anyone
holding it has full access to the account without the phone or the 2FA password —
so it is stored Fernet-encrypted and never rendered in the dashboard.
"""

from __future__ import annotations

from datetime import datetime

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
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TelegramAccount(Base):
    """One Telegram user account available to the ingestion pool.

    ``api_id``/``api_hash`` are per-account rather than global: Telegram issues
    them per developer application, and reusing one application's credentials
    across many accounts is what gets an application flagged.
    """

    __tablename__ = "telegram_accounts"
    __table_args__ = (
        # Named so the upsert in `TelegramAccountPool.store_session` can rely on
        # it, and so a rename breaks loudly instead of silently changing which
        # constraint the conflict target resolves to. This also provides the
        # lookup index for a phone number, so no separate one is declared.
        UniqueConstraint("phone_number", name="uq_telegram_accounts_phone"),
        # The pool's selection query filters on `is_active` and orders by
        # `last_used_at`, so both belong in one covering index.
        Index("ix_telegram_accounts_active_last_used", "is_active", "last_used_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: E.164 phone number. The uniqueness is declared as a named constraint in
    #: ``__table_args__`` rather than with ``unique=True`` here: the constraint the
    #: upsert relies on has to have a stable name, and a column-level flag would
    #: also emit a second, redundant index alongside it.
    phone_number: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Optional operator-facing label. Empty falls back to the phone number.
    label: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    #: Telegram application id. BigInteger because Telegram's own type is 64-bit,
    #: even though current values fit in 32.
    api_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    api_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Fernet-encrypted Telethon ``StringSession``. Empty means the account has
    #: never completed sign-in, which the dashboard shows as "auth required".
    #: `Text` rather than a bounded String: Fernet output grows with the payload
    #: and a truncated session string is unrecoverable.
    session_string: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: Last time the pool handed this account out. Drives least-recently-used
    #: rotation, so load spreads instead of hammering whichever account sorts
    #: first.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Set when Telegram returns a ``FloodWaitError``. The account is skipped
    #: until this passes. Persisted rather than held in memory because a worker
    #: restart must not forget the penalty and immediately re-trigger it.
    flood_wait_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Consecutive failures. Used to disable an account that keeps failing for a
    #: reason other than rate limiting, such as a revoked session.
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Why the account last failed, for the dashboard. Never holds the session
    #: string or the 2FA password.
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    @property
    def display_name(self) -> str:
        """Operator-facing name: the label when set, else the phone number."""
        return self.label.strip() or self.phone_number

    @property
    def is_authorised(self) -> bool:
        """Whether a session string has been stored for this account."""
        return bool(self.session_string.strip())


__all__ = ["TelegramAccount"]
